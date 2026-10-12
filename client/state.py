"""Everything the camera reports, parsed, with a thread-safe snapshot for the web UI."""

from __future__ import annotations

import threading
import time

from camlink import camera as cam


class CameraState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.status: dict | None = None     # last 02/80 push
        self.storage: dict | None = None    # last storage counters outside playback (playback zeroes them)
        self.raw_status = b""
        self.battery: dict | None = None
        self.topics: dict[str, dict] = {}   # name -> {data, count, t}
        self.settings: dict | None = None   # exposure / format read back from the camera
        self.last_status = 0.0
        self.last_battery = 0.0
        self.last_settings = 0.0

    def update_status(self, payload: bytes) -> None:
        status = cam.parse_status(payload)
        if status is None:
            return
        with self._lock:
            self.status = status
            self.raw_status = bytes(payload)
            if not status["playback"] and status["total_mb"]:
                self.storage = {k: status[k] for k in ("total_mb", "free_mb", "shots_left", "time_left")}
            self.last_status = time.monotonic()

    def update_battery(self, payload: bytes) -> None:
        battery = cam.parse_battery(payload)
        if battery is None:
            return
        with self._lock:
            self.battery = battery
            self.last_battery = time.monotonic()

    def update_topic(self, payload: bytes) -> None:
        parsed = cam.parse_topic(bytes(payload))
        if parsed is None:
            return
        name, data = parsed
        with self._lock:
            topic = self.topics.setdefault(name, {"count": 0})
            topic.update(data=data, t=time.monotonic())
            topic["count"] += 1

    def update_settings(self, settings: dict) -> None:
        with self._lock:
            self.settings = settings
            self.last_settings = time.monotonic()

    def capture_mode(self) -> int | None:
        with self._lock:
            return self.status["capture_mode"] if self.status else None

    def _recent(self, flag: str) -> bool:
        with self._lock:
            return bool(self.status and self.status[flag] and time.monotonic() - self.last_status < 2)

    def in_playback(self) -> bool:
        return self._recent("playback")

    def in_media_mode(self) -> bool:
        return self._recent("media_mode")

    def _exposure(self, now: float) -> dict | None:
        topic = self.topics.get("cam_expo_param")
        if not topic or now - topic["t"] > 5:
            return None
        return cam.exposure_from_topic(topic["data"])

    @staticmethod
    def _quality(mode: int, storage: dict) -> tuple[str | None, float | None]:
        free = storage["free_mb"]
        if mode in cam.PHOTO_MODES:
            shots = storage["shots_left"]
            return (cam.match_rate(free / shots, cam.PHOTO_RATES) if shots else None), None
        table = cam.RATE_TABLES.get(mode)
        if table is None or not storage["time_left"]:
            return None, None
        rate = free / storage["time_left"]
        return cam.match_rate(rate, table), round(rate * 8)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            fresh = self.status is not None and now - self.last_status < 3
            status = self.status if fresh else None
            snap = {
                "connected": self.connected,
                "fresh": fresh,
                "record": None,
                "capture_mode": None,
                "capture_mode_name": None,
                "photo_mode": False,
                "playback": False,
                "media_mode": False,
                "storage": None,
                "battery": self.battery if now - self.last_battery < 10 else None,
                "exposure": self._exposure(now),
                "settings": self.settings if self.connected and now - self.last_settings < 10 else None,
                "raw_status": self.raw_status.hex() if fresh else "",
                "topics": len(self.topics),
            }
            if status:
                mode = status["capture_mode"]
                snap["record"] = {"state": cam.RECORD_STATES[status["record_state"]], "elapsed": status["elapsed"],
                                  "storing": status["storing"], "photo_busy": status["photo_busy"]}
                snap.update(capture_mode=mode, capture_mode_name=cam.MODES.get(mode, f"0x{mode:02x}"),
                            photo_mode=mode in cam.PHOTO_MODES, playback=status["playback"],
                            media_mode=status["media_mode"])
            if status and self.storage:
                mode = status["capture_mode"]
                quality, bitrate = self._quality(mode, self.storage)
                settings = snap["settings"]
                if settings and mode == cam.MODE_SLOWMO and settings.get("fps_code") in (7, 8):
                    quality = cam.slowmo_label(settings["resolution_code"], settings["fps_code"])
                elif settings and mode in cam.RATE_TABLES:
                    # The bitrate tables were measured at 24-30 fps; faster rates use a higher bitrate and
                    # would be mislabelled, so prefer the resolution the camera reports.
                    if settings.get("resolution"):
                        quality = settings["resolution"]
                    elif (settings.get("fps") or 25) > 30:
                        quality = None
                snap["storage"] = dict(self.storage, quality=quality, bitrate=bitrate)
            return snap

    def topics_snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {name: {"data": t["data"].hex(), "count": t["count"], "age": round(now - t["t"], 1)}
                    for name, t in sorted(self.topics.items())}
