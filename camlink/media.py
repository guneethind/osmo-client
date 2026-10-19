"""The camera's media library over Wi-Fi: list the files (DUML) and download them (HTTP).

The listing follows the media protocol documented by the MIT-licensed osmosis project
(https://github.com/KonradIT/osmosis), checked on an Osmo Nano:

    enter media mode  02/0C [01 01 00 01]   (the live view stops; leave with [01 01 00 00])
    list one page     00/26 [4a 00 2a 10 <counter u32> 00 00 <cursor u32> <page size u16> 0d 01 00
                             ff*8 00 01 00*6 00 00 00]
                      cursor 0x40000001 = newest file in internal memory, 0x00000001 = newest on the
                      dock's microSD card. Next page = cursor + number of records (pages overlap by one).
    answer            00/27 chunks: 10-byte sub-header (4a <type> <len u16> <counter u16> <seq u16> 00 00;
                      type 04 start, 01 data, 03 end) + data. The data chunks in seq order = the manifest.
    download          GET http://192.168.2.1/v2?storage=<1 internal | 0 dock microSD>&path=<path>.<EXT>
                      (Range requests supported). Thumbnail: the .THM path (JPEG); low-resolution
                      preview of a video: the same path with .LRF (H.264 720p MP4).

Manifest: u32 record count, then per record a block anchored on a "19 06" tag (T): T-2 file type
(03 video, 00 photo), T-3 resolution code (video), T-6 duration u16 (s), T-10 handle u32 (bit 30 set =
internal memory), T-14 size u32, T-18 time (FAT date << 16 | FAT time); then TLVs: 0d <len> file name,
1a <len> 00 00 00 01 <path without extension>, 1a <len> 00 00 00 02 <thumbnail path>. A video's frame
rate is a u32 numerator followed by a u32 denominator of 1000 or 1001 within its record. The camera's
favourite mark: 01 after the bytes 1b 0a 00 00 00 02 02 01 14 02 15 03 in the record, or (T-1 = ff/fe)
T+8 = 01.

Favourite and delete (OpenPocketCine's notes; the delete layout is the one osmosis documents), verified on an
Osmo Nano:

    favourite         02/BF [01 01 <handle u32> <counter u32> 00 <0|1> 00 00 00]
    delete            00/28 [01 <handle u32> <counter u32> 00 01 00 00 00 01 01 00 00]   never re-sent
"""

from __future__ import annotations

import re
import struct
from datetime import datetime

from .protocol import CAMERA_HOST

CURSOR_INTERNAL = 0x40000001
CURSOR_SD = 0x00000001
PAGE_SIZE = 0x2D
FILE_TYPES = {0x00: "photo", 0x03: "video"}
EXT_TYPES = {"JPG": "photo", "JPEG": "photo", "DNG": "photo", "MP4": "video", "MOV": "video"}
THUMB_EXTS = (".THM", ".SCR")          # the Osmo Nano's thumbnails are .THM; other models may only keep .SCR
CHUNK_DATA, CHUNK_END = 0x01, 0x03

_NAME = re.compile(rb"\x0d(.)([\x20-\x7e]{5,})", re.S)
_PATH = re.compile(rb"\x1a.\x00\x00\x00([\x01\x02])([\x20-\x7e]{3,})\x00", re.S)
_STAMP = re.compile(r"DJI_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_")
_STAR_SIGNATURE = bytes.fromhex("1b0a00000002020114021503")


def list_payload(counter: int, cursor: int, page: int = PAGE_SIZE) -> bytes:
    return (bytes.fromhex("4a002a10") + struct.pack("<I", counter) + bytes(2) + struct.pack("<I", cursor)
            + struct.pack("<H", page) + bytes.fromhex("0d0100") + b"\xff" * 8
            + bytes.fromhex("0001000000000000") + bytes(3))


def parse_chunk(payload: bytes) -> dict | None:
    """Sub-header of one 00/27 chunk."""
    if len(payload) < 10 or payload[0] != 0x4A:
        return None
    return {"type": payload[1], "counter": int.from_bytes(payload[4:6], "little"),
            "seq": int.from_bytes(payload[6:8], "little"), "data": payload[10:]}


def _fat_time(value: int) -> str | None:
    date, clock = value >> 16, value & 0xFFFF
    try:
        return datetime(1980 + (date >> 9), (date >> 5) & 0xF, date & 0x1F,
                        clock >> 11, (clock >> 5) & 0x3F, (clock & 0x1F) * 2).isoformat(sep=" ")
    except ValueError:
        return None


def capture_time(name: str) -> str | None:
    """Camera-local capture time from a DJI_YYYYMMDDHHMMSS_NNNN_D.EXT name (the FAT time can be an hour off)."""
    m = _STAMP.match(name)
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}" if m else None


def favourite_payload(handle: int, counter: int, on: bool) -> bytes:
    return bytes((1, 1)) + struct.pack("<II", handle, counter) + bytes((0, 1 if on else 0, 0, 0, 0))


def delete_payload(handle: int, counter: int) -> bytes:
    return bytes((1,)) + struct.pack("<II", handle, counter) + bytes((0,)) + struct.pack("<I", 1) + bytes((1, 1, 0, 0))


def _starred(data: bytes, tag: int, end: int) -> bool:
    """Only an explicit 01 counts: on other layouts that byte can be a length."""
    at = data.find(_STAR_SIGNATURE, tag - 10, end)
    if at >= 0 and at + len(_STAR_SIGNATURE) < len(data):
        return data[at + len(_STAR_SIGNATURE)] == 1
    return data[tag - 1] in (0xFF, 0xFE) and tag + 8 < end and data[tag + 8] == 1


def _frame_rate(data: bytes, start: int, end: int) -> float | None:
    """Last numerator / denominator pair (1000 or 1001) in data[start:end] with a plausible rate."""
    rate = None
    for i in range(max(0, start), min(end, len(data)) - 7):
        den = int.from_bytes(data[i + 4:i + 8], "little")
        if den in (1000, 1001):
            num = int.from_bytes(data[i:i + 4], "little")
            if 20_000 <= num <= 250_000:
                rate = round(num / den, 3)
    return rate


def parse_manifest(data: bytes) -> list[dict]:
    """The records of one listing page, newest first."""
    names = [(m.start(), m.group(2)) for m in _NAME.finditer(data) if len(m.group(2)) == m.group(1)[0]]
    tags = [data.rfind(b"\x19\x06", names[i - 1][0] if i else 0, pos) for i, (pos, _) in enumerate(names)]
    files = []
    for i, (pos, raw_name) in enumerate(names):
        end = names[i + 1][0] if i + 1 < len(names) else len(data)
        tag = tags[i]
        if tag < 18:
            continue
        # the record runs up to the next one's header (10 bytes before its tag)
        record_end = tags[i + 1] - 10 if i + 1 < len(tags) and tags[i + 1] >= 18 else len(data)
        handle, = struct.unpack_from("<I", data, tag - 10)
        size, = struct.unpack_from("<I", data, tag - 14)
        mtime, = struct.unpack_from("<I", data, tag - 18)
        duration, = struct.unpack_from("<H", data, tag - 6)
        name = raw_name.decode("ascii", "replace")
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        paths = {m.group(1)[0]: m.group(2).decode("ascii", "replace") for m in _PATH.finditer(data, pos, end)}
        if 1 not in paths:
            continue
        kind = FILE_TYPES.get(data[tag - 2]) or EXT_TYPES.get(ext.upper(), f"type{data[tag - 2]:02x}")
        files.append({
            "name": name,
            "path": f"{paths[1]}.{ext}" if ext else paths[1],
            "thumb": f"{paths[2]}.THM" if 2 in paths else None,
            "storage": 1 if handle & 0x40000000 else 0,
            "handle": handle,
            "size": size,
            "duration": duration if kind == "video" else None,
            "type": kind,
            "date": _fat_time(mtime),
            "starred": _starred(data, tag, record_end),
            "fps": _frame_rate(data, tag - 10, record_end) if kind == "video" else None,
            "res_code": data[tag - 3] if kind == "video" else None,
        })
    return files


def http_url(storage: int, path: str, host: str = CAMERA_HOST) -> str:
    """Download URL of a camera file; `host` is the camera's address (its own access point by default, or
    where it was found on a shared network)."""
    return f"http://{host}/v2?storage={storage}&path={path}"
