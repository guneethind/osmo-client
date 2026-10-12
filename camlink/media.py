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
(03 video, 00 photo), T-6 duration u16 (s), T-10 handle u32 (bit 30 set = internal memory), T-14 size
u32, T-18 time (FAT date << 16 | FAT time); then TLVs: 0d <len> file name, 1a <len> 00 00 00 01 <path
without extension>, 1a <len> 00 00 00 02 <thumbnail path>.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime

from .protocol import HTTP_MEDIA

CURSOR_INTERNAL = 0x40000001
CURSOR_SD = 0x00000001
PAGE_SIZE = 0x2D
FILE_TYPES = {0x00: "photo", 0x03: "video"}
CHUNK_DATA, CHUNK_END = 0x01, 0x03

_NAME = re.compile(rb"\x0d(.)([\x20-\x7e]{5,})", re.S)
_PATH = re.compile(rb"\x1a.\x00\x00\x00([\x01\x02])([\x20-\x7e]{3,})\x00", re.S)
_STAMP = re.compile(r"DJI_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_")


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


def parse_manifest(data: bytes) -> list[dict]:
    """The records of one listing page, newest first."""
    names = [(m.start(), m.group(2)) for m in _NAME.finditer(data) if len(m.group(2)) == m.group(1)[0]]
    files = []
    for i, (pos, raw_name) in enumerate(names):
        end = names[i + 1][0] if i + 1 < len(names) else len(data)
        start = names[i - 1][0] if i else 0
        tag = data.rfind(b"\x19\x06", start, pos)
        if tag < 18:
            continue
        handle, = struct.unpack_from("<I", data, tag - 10)
        size, = struct.unpack_from("<I", data, tag - 14)
        mtime, = struct.unpack_from("<I", data, tag - 18)
        duration, = struct.unpack_from("<H", data, tag - 6)
        name = raw_name.decode("ascii", "replace")
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        paths = {m.group(1)[0]: m.group(2).decode("ascii", "replace") for m in _PATH.finditer(data, pos, end)}
        if 1 not in paths:
            continue
        kind = FILE_TYPES.get(data[tag - 2], f"type{data[tag - 2]:02x}")
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
        })
    return files


def http_url(storage: int, path: str) -> str:
    return f"{HTTP_MEDIA}?storage={storage}&path={path}"
