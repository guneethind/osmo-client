"""Camera commands, setting codes and status parsers (command set 0x02 unless noted).

All of this was verified on an Osmo Nano; the full description is in docs/PROTOCOL.md.

    take photo           02/01 [01]                    (photo mode)
    timelapse start/stop 02/01 [01] / [00]             (timelapse rides take-photo)
    record start/stop    02/02 [01] / [00]             (video, slow-mo, hyperlapse, super night)
    capture mode         02/E1 [mode]                  (see MODES)
    exposure mode        02/1E [mode, 00]              01 auto, 04 manual; read 02/1F
    ISO                  02/2A [code]                  manual only; read 02/2B
    shutter speed        02/28 [01, u16 LE, decimal, 00, 00, 00]   manual only; read 02/29
    video format         02/18 [resolution, fps, 00, 00, slow-mo factor]; read 02/19
    aspect 4:3 / 16:9    02/8E [01 01, 46 00, 01, 1|2] (parameter TLV); read [00 01 46 00]
    photo format         02/12 [size, ratio]; read 02/13 (photo mode only)
    white balance        02/2C [mode, kelvin / 100, 00, 00, 00]; read 02/2D

Replies start with a result code: 00 = OK, E0 = command not supported, E3 = invalid parameter (or not
allowed in the current state, e.g. ISO while exposure is on Auto), DF = not available right now.
"""

from __future__ import annotations

import re

# Capture modes: the value of 02/E1, also byte 57 of the 02/80 status push.
MODES = {0x05: "Photo", 0x01: "Video", 0x28: "Super Night", 0x00: "Slow-mo", 0x02: "Timelapse", 0x0A: "Hyperlapse"}
PHOTO_MODES = {0x05}
MODE_VIDEO, MODE_SLOWMO, MODE_TIMELAPSE = 0x01, 0x00, 0x02

RESULT_TEXT = {0xE0: "command not supported", 0xE3: "invalid parameter", 0xDF: "not available right now"}
RECORD_STATES = ("idle", "starting", "recording", "stopping")

# The camera reserves a fixed amount of storage per second of video (free MB / seconds left) and per
# photo (free MB / photos left). Unlike the raw counters these ratios don't drift as the storage fills,
# so they identify the current format. Measured at 25 fps.
VIDEO_RATES = [(11.903, "4K 16:9"), (14.288, "4K 4:3"), (7.135, "2.7K 16:9"), (8.327, "2.7K 4:3"),
               (4.750, "1080p 16:9"), (5.347, "1080p 4:3")]
SLOWMO_RATES = [(12.143, "1080p · 4×"), (14.582, "1080p · 8×"), (14.527, "2.7K · 4×"), (15.537, "4K · 4×")]
PHOTO_RATES = [(1.209, "M 16:9 · 8 MP"), (1.317, "M 4:3 · 9 MP"), (2.714, "L 16:9 · 31 MP"), (2.935, "L 4:3 · 36 MP")]
RATE_TABLES = {0x01: VIDEO_RATES, 0x28: VIDEO_RATES, 0x00: SLOWMO_RATES}

# Exposure. ISO codes 03-0C are one stop apart (01/02 behave like 03, 0D like 0C).
EXPOSURE_MODES = {"auto": 0x01, "manual": 0x04}     # 02/03 (priority modes) are refused by the Nano
ISO_VALUES = {3: 100, 4: 200, 5: 400, 6: 800, 7: 1600, 8: 3200, 9: 6400, 10: 12800, 11: 25600, 12: 51200}
# Shutter speed = u16 (bit 15 set: 1/x s, otherwise whole seconds) + a decimal digit in bits 16-23
# (0x5800C = 1/12.5 s, 0x30001 = 1.3 s). The speeds the camera accepts, fastest first.
SHUTTER_SPEEDS = [0x9F40, 0x9900, 0x9388, 0x8FA0, 0x8C80, 0x89C4, 0x87D0, 0x8640, 0x84E2, 0x83E8, 0x8320,
                  0x8280, 0x81F4, 0x8190, 0x8140, 0x80FA, 0x80C8, 0x80A0, 0x807D, 0x8064, 0x8050, 0x803C,
                  0x8032, 0x8028, 0x801E, 0x8019, 0x8014, 0x800F, 0x5800C, 0x800A, 0x8008, 0x8005, 0x8004,
                  0x8003, 0x58002, 0x8002, 0x0001, 0x30001, 0x60001, 0x0002, 0x50002, 0x0003, 0x20003, 0x0004,
                  0x0005, 0x0006, 0x0008, 0x000A, 0x000F, 0x0014, 0x001E]

# Video format: 02/18 [resolution, fps, 00, 00, slow-mo factor]. Frame-rate codes were checked by
# recording and reading the files back (code 6 gives 59.94 fps). Video mode takes codes 1-6; 120 and
# 240 fps belong to Slow-mo.
FRAME_RATES = {1: 24, 2: 25, 3: 30, 4: 48, 5: 50, 6: 60, 7: 120, 8: 240}
# Resolution codes, identified by bitrate and live view frame shape. The 4:3 codes are only accepted
# with the aspect set to 4:3 first; a 16:9 code switches the picture back by itself.
RESOLUTIONS = {0x10: "4K 16:9", 0x2D: "2.7K 16:9", 0x0A: "1080p 16:9",
               0x67: "4K 4:3", 0x5F: "2.7K 4:3", 0x0C: "1080p 4:3"}
RES_4_3 = {0x67, 0x5F, 0x0C}
ASPECT_TLV = 0x46                       # 02/8E parameter TLV: 1 = 4:3, 2 = 16:9
# What each capture mode accepts (everything else answers E3).
MODE_FORMATS = {
    0x01: {"res": [0x10, 0x2D, 0x0A, 0x67, 0x5F, 0x0C], "fps": [1, 2, 3, 4, 5, 6]},    # Video
    0x28: {"res": [0x10, 0x2D, 0x0A], "fps": [1, 2, 3]},                               # Super Night
    0x02: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},                                  # Timelapse
    0x0A: {"res": [0x10, 0x2D, 0x0A], "fps": [2, 3]},                                  # Hyperlapse
}
LIMITED_FPS = {0x0C: [1, 2, 3]}         # 1080p 4:3 tops out at 30 fps
# Slow-mo: (resolution, fps code, speed factor); the factor goes in the last byte of 02/18.
SLOWMO_FORMATS = [(0x10, 7, 4), (0x2D, 7, 4), (0x0A, 7, 4), (0x0A, 8, 8)]
# Photo format: 02/12 [size, ratio].
PHOTO_SIZE_CODES = {4: "L", 3: "M"}
PHOTO_RATIOS = {0: "4:3", 1: "16:9"}
PHOTO_MP = {(4, 0): 36, (4, 1): 31, (3, 0): 9, (3, 1): 8}
# White balance: 02/2C [mode, kelvin / 100, 00, 00, 00]; mode 0 = auto, 6 = manual colour temperature.
WB_AUTO, WB_MANUAL = 0x00, 0x06
WB_KELVIN = range(2000, 10001, 100)

# Media mode (gallery): the live view stops and files can be listed and downloaded.
ENTER_MEDIA = bytes.fromhex("01010001")
LEAVE_MEDIA = bytes.fromhex("01010000")

_TOPIC_NAME = re.compile(rb"[a-z][a-z0-9_]{2,63}")


def parameter_tlv_set(tlv_type: int, value: int) -> bytes:
    """02/8E payload setting one 1-byte parameter: [01 01][type u16 LE][length 1][value]."""
    return bytes((1, 1)) + tlv_type.to_bytes(2, "little") + bytes((1, value))


def shutter_payload(raw: int) -> bytes:
    return b"\x01" + (raw & 0xFFFF).to_bytes(2, "little") + bytes(((raw >> 16) & 0xFF, 0, 0, 0))


def shutter_text(raw: int) -> str | None:
    whole, decimal = raw & 0x7FFF, (raw >> 16) & 0xFF
    number = f"{whole}.{decimal}" if decimal else str(whole)
    if raw & 0x8000 and whole:
        return f"1/{number}"
    return f'{number}"' if whole else None


def photo_label(size: int, ratio: int) -> str:
    label = f"{PHOTO_SIZE_CODES.get(size, f'size {size}')} {PHOTO_RATIOS.get(ratio, f'ratio {ratio}')}"
    return label + (f" · {PHOTO_MP[size, ratio]} MP" if (size, ratio) in PHOTO_MP else "")


def slowmo_label(res: int, fps: int) -> str:
    rate = FRAME_RATES.get(fps, 0)
    return f"{RESOLUTIONS.get(res, hex(res)).split()[0]} · {rate} fps · {rate // 30}×"


def match_rate(value: float, table: list[tuple[float, str]]) -> str | None:
    """Closest label within 2 %; both labels when the table can't tell two apart."""
    hits = sorted((abs(value - ref) / ref, label) for ref, label in table)
    hits = [h for h in hits if h[0] < 0.02]
    if not hits:
        return None
    if len(hits) > 1 and hits[1][0] - hits[0][0] < 0.006:
        return f"{hits[0][1]} / {hits[1][1]}"
    return hits[0][1]


def parse_status(p: bytes) -> dict | None:
    """02/80 status push (about 10 per second), little-endian fields."""
    if len(p) < 58:
        return None
    u32 = lambda offset: int.from_bytes(p[offset:offset + 4], "little")  # noqa: E731
    return {
        "record_state": (p[0] >> 6) & 3,  # index into RECORD_STATES
        "photo_busy": bool(p[0] & 0x38),  # set while a photo is being taken
        "storing": bool(p[2] & 0x08),     # writing a file
        # Byte 4: 0 photo/timelapse, 1 video, 2 playback on the camera. The app's media mode (02/0C)
        # sets bit 0x40 of byte 3 instead.
        "playback": p[4] == 2 or bool(p[3] & 0x40),
        "media_mode": bool(p[3] & 0x40),
        "total_mb": u32(5),
        "free_mb": u32(9),
        "shots_left": u32(13),
        "time_left": u32(17),             # seconds of video that still fit
        "elapsed": u32(29),               # seconds recorded in the current clip
        "capture_mode": p[57],            # MODES
    }


def parse_battery(p: bytes) -> dict | None:
    """0D/02 battery push (about once a second)."""
    if len(p) < 21:
        return None
    return {
        "percent": p[20],
        "voltage": int.from_bytes(p[1:5], "little"),                # mV
        "current": int.from_bytes(p[5:9], "little", signed=True),   # mA, negative = discharging
        "temp": int.from_bytes(p[17:19], "little") / 10,            # °C
    }


def parse_topic(p: bytes) -> tuple[str, bytes] | None:
    """Named status topic push (00/99): a header, u16 name length, the name, 6 bytes, u16 data length,
    the data. The name length is searched for rather than assumed at a fixed offset."""
    for i in range(11, min(len(p) - 2, 24)):
        n = int.from_bytes(p[i:i + 2], "little")
        if 3 <= n <= 64 and _TOPIC_NAME.fullmatch(p[i + 2:i + 2 + n]):
            j = i + 2 + n
            if j + 8 > len(p):
                return None
            size = int.from_bytes(p[j + 6:j + 8], "little")
            return p[i + 2:j].decode(), p[j + 8:j + 8 + size]
    return None


def exposure_from_topic(data: bytes) -> dict | None:
    """ISO and shutter in use from the 'cam_expo_param' topic (real values, also in Auto)."""
    if len(data) < 22:
        return None
    return {"iso": int.from_bytes(data[16:20], "little") or None,
            "shutter": shutter_text(int.from_bytes(data[20:22], "little"))}


def parse_settings(mode: bytes, iso: bytes, shutter: bytes, video: bytes, photo: bytes = b"",
                   wb: bytes = b"") -> dict | None:
    """Replies to 02/1F, 02/2B, 02/29, 02/19, 02/13 and 02/2D (each one starts with its result code).
    In Auto the camera reports ISO 0 and refuses the shutter read."""
    if len(mode) < 2 or mode[0] or len(iso) < 2 or iso[0]:
        return None
    raw = int.from_bytes(shutter[2:4], "little") | shutter[4] << 16 if len(shutter) >= 5 and not shutter[0] else None
    ok_video = len(video) >= 3 and not video[0]
    ok_photo = len(photo) >= 3 and not photo[0]
    ok_wb = len(wb) >= 3 and not wb[0]
    wb_mode = None
    if ok_wb:
        wb_mode = "auto" if wb[1] == WB_AUTO else "manual" if wb[1] == WB_MANUAL else f"0x{wb[1]:02x}"
    return {
        "mode": next((k for k, v in EXPOSURE_MODES.items() if v == mode[1]), f"0x{mode[1]:02x}"),
        "iso_code": iso[1] or None,
        "iso": ISO_VALUES.get(iso[1]),
        "shutter_raw": raw,
        "shutter": shutter_text(raw) if raw else None,
        "resolution_code": video[1] if ok_video else None,
        "resolution": RESOLUTIONS.get(video[1]) if ok_video else None,
        "fps_code": video[2] if ok_video else None,
        "fps": FRAME_RATES.get(video[2]) if ok_video else None,
        "speed": video[5] if ok_video and len(video) > 5 else None,
        "photo_size_code": photo[1] if ok_photo else None,
        "photo_ratio_code": photo[2] if ok_photo else None,
        "photo": photo_label(photo[1], photo[2]) if ok_photo else None,
        "wb": wb_mode,
        "wb_kelvin": wb[2] * 100 if ok_wb else None,
    }
