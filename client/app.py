"""State shared by the camera session (asyncio + threads) and the web UI, and the actions the UI calls."""

from __future__ import annotations

import asyncio
import collections
import threading
import time
import urllib.error
import urllib.request

from camlink import ble, camera as cam, media
from camlink.datalink import Datalink
from camlink.duml import Message
from camlink.models import MODELS, TESTED_MODELS
from camlink.registry import CameraRegistry
from camlink.util import log

from .downloads import Downloader, default_download_dir
from .state import CameraState


class Pending:
    """A command waiting for the camera's reply."""

    def __init__(self, cmd_set: int, cmd_id: int) -> None:
        self.key = (cmd_set, cmd_id)
        self.seq: int | None = None
        self.payload = b""
        self.done = threading.Event()


class App:
    def __init__(self) -> None:
        self.state = CameraState()
        self.latest_frame = None              # av.VideoFrame for the live view stream
        self.frame_seq = 0
        self.last_frame = 0.0
        self.frame_cond = threading.Condition()
        self.datalink: Datalink | None = None  # current session, for sending commands
        self._command_lock = threading.Lock()  # one command at a time: a mode switch takes ~0.8 s
        self._pending: list[Pending] = []
        self._pending_lock = threading.Lock()
        self.settings_due = threading.Event()  # read the settings back now (after a mode switch)
        self.video_grace = 0.0                 # monotonic() until which a pause in the video is expected
        # Media library. Listing runs in the camera's media mode (no live view meanwhile).
        self.media = {"state": "idle", "error": None, "updated": None, "stores": {}, "files": []}
        self._media_lock = threading.Lock()
        self._media_chunks: dict[int, dict[tuple[int, int], dict]] = {}   # counter -> {(type, seq): chunk}
        self._media_cond = threading.Condition()
        self._media_counter = 1
        self._thumbs: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.downloader = Downloader(default_download_dir())
        # While the gallery is open the camera stays in media mode: no live view to encode, so it runs
        # cooler and serves files about 6x faster. The page renews the hold; it lapses 30 s after the
        # page goes quiet.
        self.media_hold = False
        self.media_hold_seen = 0.0
        self.want_link = threading.Event()     # the UI's connect / disconnect switch
        self.want_link.set()
        self.link = "connecting"               # select | off | connecting | on
        self.home = ""                         # Wi-Fi network the PC returns to when disconnected
        # Cameras: linked ones are remembered; `target` is the one to connect to (None = let the user
        # pick); `nearby` holds the cameras heard over BLE; `stage` tracks the connection steps.
        self.registry = CameraRegistry()
        self.target: str | None = None
        self.target_changed = threading.Event()
        self.nearby: dict[str, dict] = {}
        self.scanning = False
        self.stage: str | None = None
        self.stage_error: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state_camera: str | None = None   # camera the state and gallery shown belong to

    # ---- incoming data -------------------------------------------------------------------------
    def on_frame(self, frame, _first_packet: float = 0.0) -> None:
        with self.frame_cond:
            self.latest_frame = frame
            self.frame_seq += 1
            self.last_frame = time.monotonic()
            self.frame_cond.notify_all()

    def on_message(self, m: Message) -> None:
        if m.is_reply:
            with self._pending_lock:
                for pending in self._pending:
                    if pending.key == m.command and pending.seq in (None, m.seq):
                        pending.payload = bytes(m.payload)
                        pending.done.set()
                        break
            return
        if m.command == (0x00, 0x27):
            chunk = media.parse_chunk(bytes(m.payload))
            if chunk is not None:
                with self._media_cond:
                    self._media_chunks.setdefault(chunk["counter"], {})[(chunk["type"], chunk["seq"])] = chunk
                    self._media_cond.notify_all()
        elif m.command == (0x02, 0x80):
            self.state.update_status(m.payload)
        elif m.command == (0x0D, 0x02):
            self.state.update_battery(m.payload)
        elif m.command == (0x00, 0x99):
            self.state.update_topic(m.payload)

    # ---- commands ------------------------------------------------------------------------------
    def request(self, cmd_set: int, cmd_id: int, payload: bytes = b"", timeout: float = 2.5,
                quiet: bool = False) -> dict:
        """Send a command and wait for the camera's reply (matched by sequence). Result code 00 = OK."""
        link = self.datalink
        if link is None:
            return {"ok": False, "error": "not connected to the camera"}
        if not self._command_lock.acquire(timeout=timeout):
            return {"ok": False, "error": "another command is in progress"}
        pending = Pending(cmd_set, cmd_id)
        try:
            with self._pending_lock:
                self._pending.append(pending)
            try:
                pending.seq = link.send(cmd_set, cmd_id, payload)
            except OSError:              # the session closed its socket meanwhile (disconnect / restart)
                return {"ok": False, "error": "not connected to the camera"}
            answered = pending.done.wait(timeout)
        finally:
            with self._pending_lock:
                self._pending.remove(pending)
            self._command_lock.release()
        label = f"{cmd_set:02x}/{cmd_id:02x} [{payload.hex()}]"
        if not answered:
            log(f"{label} -> no reply")
            return {"ok": False, "error": "the camera did not reply"}
        reply = pending.payload.hex()
        code = pending.payload[0] if pending.payload else 0
        if not quiet:
            log(f"{label} -> {reply or 'ok'}")
        if code == 0:
            return {"ok": True, "reply": reply}
        return {"ok": False, "code": code, "reply": reply,
                "error": cam.RESULT_TEXT.get(code, f"refused (0x{code:02x})")}

    def read_settings(self) -> None:
        """Read the exposure and format back. Kept as they were when the camera doesn't answer (another
        command in flight), so the page doesn't flicker."""
        answers = [self.request(0x02, cmd_id, timeout=1.5, quiet=True)
                   for cmd_id in (0x1F, 0x2B, 0x29, 0x19, 0x13, 0x2D)]
        settings = cam.parse_settings(*(bytes.fromhex(a.get("reply", "")) for a in answers))
        if settings is not None:
            self.state.update_settings(settings)

    def settings_loop(self, stop: threading.Event) -> None:
        """Keep the settings current: changes made on the camera itself, and each capture mode's own
        values after a mode switch (settings_due wakes it early)."""
        while not stop.is_set():
            self.settings_due.wait(2.0)
            self.settings_due.clear()
            if self.datalink is not None and self.state.connected and not self.state.in_playback():
                try:
                    self.read_settings()
                except Exception as error:  # noqa: BLE001 - never let this thread die
                    log(f"Reading the settings failed: {error}")

    def set_capture_mode(self, mode: int) -> dict:
        result = self.request(0x02, 0xE1, bytes((mode,)))
        self.settings_due.set()           # each capture mode keeps its own exposure
        return result

    def take_photo(self) -> dict:
        return self.request(0x02, 0x01, b"\x01")

    def record(self, start: bool) -> dict:
        """Timelapse is interval shooting: it starts and stops with take-photo (record answers DF there).
        Every other video mode uses record."""
        cmd_id = 0x01 if self.state.capture_mode() == cam.MODE_TIMELAPSE else 0x02
        return self.request(0x02, cmd_id, b"\x01" if start else b"\x00")

    def set_setting(self, cmd_id: int, payload: bytes) -> dict:
        result = self.request(0x02, cmd_id, payload)
        self.read_settings()
        return result

    def set_frame_rate(self, code: int) -> dict:
        """Keep the current resolution, change the frame rate."""
        answer = self.request(0x02, 0x19)
        reply = bytes.fromhex(answer.get("reply", ""))
        if not answer.get("ok") or len(reply) < 3:
            return {"ok": False, "error": "could not read the current video format"}
        return self.set_video_format(reply[1], code)

    def set_video_format(self, res: int, fps: int) -> dict:
        """Resolution + frame rate for the current capture mode (slow-mo adds its speed factor). The camera
        restarts its encoder, so the live view pauses for a few seconds: tell the session watchdog. A 4:3
        resolution needs the aspect switched to 4:3 first."""
        self.video_grace = time.monotonic() + 12
        mode = self.state.capture_mode()
        if mode == cam.MODE_SLOWMO:
            payload = bytes((res, fps, 0, 0, cam.FRAME_RATES.get(fps, 120) // 30))
        else:
            payload = bytes((res, fps, 0, 0, 0))
            # A 16:9 code switches the picture back by itself but leaves the aspect setting at 4:3, so in
            # Video mode set the aspect explicitly either way.
            if res in cam.RES_4_3 or mode == cam.MODE_VIDEO:
                aspect = self.request(0x02, 0x8E, cam.parameter_tlv_set(cam.ASPECT_TLV, 1 if res in cam.RES_4_3 else 2))
                if not aspect.get("ok"):
                    return aspect
        return self.set_setting(0x18, payload)

    def set_white_balance(self, manual: bool, kelvin: int | None) -> dict:
        """Auto keeps the manual temperature the camera remembers; manual sets one (2000-10000 K)."""
        if kelvin is None:
            answer = self.request(0x02, 0x2D)
            reply = bytes.fromhex(answer.get("reply", ""))
            kelvin = reply[2] * 100 if answer.get("ok") and len(reply) > 2 else 5600
        mode = cam.WB_MANUAL if manual else cam.WB_AUTO
        return self.set_setting(0x2C, bytes((mode, kelvin // 100, 0, 0, 0)))

    # ---- media library -------------------------------------------------------------------------
    def _list_page(self, cursor: int) -> tuple[list[dict] | None, str | None]:
        """One 00/26 query; the answer streams back as 00/27 chunks ending with an end chunk. Returns
        (files, None), or (None, reason) when that storage doesn't answer."""
        self._media_counter = self._media_counter % 0xFFF0 + 1
        counter = self._media_counter
        with self._media_cond:
            self._media_chunks[counter] = {}
        answer = self.request(0x00, 0x26, media.list_payload(counter, cursor), timeout=3.0)
        if not answer.get("ok"):
            return None, "not available" if answer.get("code") == 0xD8 else answer.get("error", "no reply")
        deadline = time.monotonic() + 10
        with self._media_cond:
            while time.monotonic() < deadline:
                if any(kind == media.CHUNK_END for kind, _ in self._media_chunks.get(counter, {})):
                    break
                self._media_cond.wait(0.3)
            page = self._media_chunks.pop(counter, {})
        if not any(kind == media.CHUNK_END for kind, _ in page):
            return None, "the camera did not finish sending the list"
        data = b"".join(page[k]["data"] for k in sorted(k for k in page if k[0] == media.CHUNK_DATA))
        return media.parse_manifest(data) if data else [], None

    def list_media(self) -> dict:
        """Read the whole library (internal memory, then the dock's microSD): enter media mode, page
        through it newest first, then leave media mode unless the gallery is still open."""
        snap = self.state.snapshot()
        if snap["record"] and snap["record"]["state"] != "idle":
            return {"ok": False, "error": "the camera is recording"}
        if self.datalink is None:
            return {"ok": False, "error": "not connected to the camera"}
        if not self._media_lock.acquire(blocking=False):
            return {"ok": False, "error": "the gallery is already being read"}
        try:
            self.media.update(state="listing", error=None)
            self.video_grace = time.monotonic() + 15
            if not self.state.in_media_mode():
                entered = self.request(0x02, 0x0C, cam.ENTER_MEDIA)
                if not entered.get("ok"):
                    raise RuntimeError(entered.get("error", "the camera did not enter media mode"))
                for _ in range(40):
                    if self.state.in_media_mode():
                        break
                    time.sleep(0.1)
                time.sleep(1.7)                    # the camera needs a moment before the first query
            files: dict[tuple[int, str], dict] = {}
            stores = {}
            for store, first in (("internal", media.CURSOR_INTERNAL), ("sd", media.CURSOR_SD)):
                cursor = first
                for _ in range(200):
                    page, problem = self._list_page(cursor)
                    if page is None:
                        stores.setdefault(store, problem)
                        break
                    stores[store] = "ok"
                    new = [f for f in page if (f["storage"], f["path"]) not in files]
                    for f in new:
                        files[(f["storage"], f["path"])] = f
                    self.media["files"] = list(files.values())   # show progress while paging
                    if len(page) < media.PAGE_SIZE or not new:
                        break
                    cursor += len(page)            # pages overlap by one file; duplicates are skipped
            result = sorted(files.values(), key=lambda f: (media.capture_time(f["name"]) or "", f["name"]), reverse=True)
            for f in result:
                f["stamp"] = media.capture_time(f["name"]) or f["date"]
            self.media.update(state="idle", files=result, stores=stores, updated=time.time())
            log(f"Media library: {len(result)} files (" + ", ".join(f"{k}: {v}" for k, v in stores.items()) + ")")
            return {"ok": True, "count": len(result), "stores": stores}
        except Exception as error:  # noqa: BLE001 - shown on the page
            self.media.update(state="error", error=str(error))
            return {"ok": False, "error": str(error)}
        finally:
            if not self.media_hold and not self.downloader.active():
                self.request(0x02, 0x0C, cam.LEAVE_MEDIA)
                self.video_grace = time.monotonic() + 8
            self._media_lock.release()

    def refresh_media(self) -> dict:
        if self.media["state"] == "listing":
            return {"ok": True, "already": True}
        threading.Thread(target=self.list_media, daemon=True, name="media-list").start()
        return {"ok": True}

    def set_media_hold(self, on: bool) -> dict:
        self.media_hold_seen = time.monotonic()
        if on == self.media_hold:
            return {"ok": True}
        self.media_hold = on
        if self._media_lock.locked() or (not on and self.downloader.active()):
            return {"ok": True}       # list_media handles the mode itself; downloads keep it
        self.video_grace = time.monotonic() + (10 if on else 8)
        return self.request(0x02, 0x0C, cam.ENTER_MEDIA if on else cam.LEAVE_MEDIA)

    def media_hold_loop(self, stop: threading.Event) -> None:
        """Keep media mode while the gallery is open or files are downloading (enter it again after a
        reconnect), leave it when the page stops renewing and nothing is downloading, and leave a media
        mode left over from an earlier session."""
        last_leave = 0.0
        while not stop.wait(2.0):
            try:
                if self.media_hold and time.monotonic() - self.media_hold_seen > 30:
                    log("Gallery closed: leaving media mode")
                    self.media_hold = False
                if self.datalink is None or not self.state.connected or self._media_lock.locked():
                    continue
                if self.state.snapshot()["record"] is None:
                    continue
                want = self.media_hold or self.downloader.active() > 0
                in_media = self.state.in_media_mode()
                if want and not in_media:
                    # Only an accepted request earns the video watchdog's grace: if the camera is gone (flat
                    # battery, Wi-Fi off) the session must be allowed to time out and reconnect.
                    if self.request(0x02, 0x0C, cam.ENTER_MEDIA, quiet=True).get("ok"):
                        self.video_grace = time.monotonic() + 10
                elif not want and in_media and time.monotonic() - last_leave > 10:
                    last_leave = time.monotonic()
                    self.video_grace = time.monotonic() + 8
                    self.request(0x02, 0x0C, cam.LEAVE_MEDIA)
            except Exception as error:  # noqa: BLE001 - never let this thread die
                log(f"Media mode keeper: {error}")

    def media_snapshot(self) -> dict:
        dl = self.downloader
        files = [dict(f, downloaded=dl.is_downloaded(f["name"], f["size"])) for f in self.media["files"]]
        return {"state": self.media["state"], "error": self.media["error"], "updated": self.media["updated"],
                "stores": self.media["stores"], "files": files, "folder": str(dl.folder)}

    def thumbnail(self, storage: int, path: str) -> bytes | None:
        key = f"{storage}:{path}"
        if key in self._thumbs:
            self._thumbs.move_to_end(key)
            return self._thumbs[key]
        try:
            with urllib.request.urlopen(media.http_url(storage, path), timeout=6) as response:
                data = response.read()
        except (urllib.error.URLError, OSError):
            return None
        self._thumbs[key] = data
        while len(self._thumbs) > 2000:
            self._thumbs.popitem(last=False)
        return data

    # ---- status for the page -------------------------------------------------------------------
    def status(self) -> dict:
        snap = self.state.snapshot()
        snap["video_age"] = round(time.monotonic() - self.last_frame, 1) if self.last_frame else None
        snap["link"] = self.link
        snap["home"] = self.home
        snap["media_state"] = self.media["state"]
        snap["downloads_active"] = self.downloader.active()
        # The active camera: what we know of it, plus its advertisement (only heard while disconnected).
        known = self.registry.get(self.target) or {}
        heard = self.nearby.get(self.target or "")
        if heard and time.monotonic() - heard["t"] < 15:
            snap["camera"] = {"name": heard.get("name") or known.get("name"), "rssi": heard["rssi"],
                              "power": heard.get("power") or "unknown"}
        else:
            snap["camera"] = None
        snap["active"] = None if not self.target else {
            "address": self.target, "name": known.get("name") or (heard or {}).get("name") or self.target,
            "model": known.get("model") or (heard or {}).get("model") or "DJI Osmo",
            "tested": bool(known.get("tested", (heard or {}).get("tested"))), "known": bool(known)}
        snap["stage"] = self.stage
        snap["stage_error"] = self.stage_error
        return snap

    # ---- cameras -------------------------------------------------------------------------------
    def use_camera(self, address: str, name: str) -> None:
        """Connecting to a camera: start from a clean state when it isn't the one shown so far (status,
        topics, settings and gallery belong to a camera), and point downloads at its folder."""
        if self.state_camera != address:
            if self.state_camera is not None:
                self.downloader.cancel_all()
            self.state = CameraState()
            self.media = {"state": "idle", "error": None, "updated": None, "stores": {}, "files": []}
            self._thumbs.clear()
            self.media_hold = False
            self.state_camera = address
        self.downloader.sub = "".join(ch for ch in (name or address) if ch.isalnum() or ch in "-_ ").strip() or "camera"

    def on_seen(self, entry: dict) -> None:
        old = self.nearby.get(entry["address"])
        if old and not entry.get("name"):
            entry["name"] = old.get("name", "")
        self.nearby[entry["address"]] = entry

    async def discover(self, until) -> None:
        """Listen for cameras over BLE (model + awake/asleep from their advertisement) until `until()` is
        true. Runs while no camera is connected: the scanner must stop before a BLE connection starts."""
        self.scanning = True
        try:
            await ble.scan(10 ** 6, on_found=self.on_seen, stop=until)
        except Exception as error:  # noqa: BLE001 - Bluetooth off or busy: the list just stays empty
            log(f"Bluetooth scan unavailable: {error} ({ble.UNAVAILABLE_HINT})")
            while not until():
                await asyncio.sleep(0.5)
        finally:
            self.scanning = False

    async def quick_scan(self, seconds: float = 6.0) -> None:
        """On-demand scan while a camera is connected (the 'Search again' button)."""
        if self.scanning:
            return
        self.scanning = True
        try:
            await ble.scan(seconds, on_found=self.on_seen)
        except Exception as error:  # noqa: BLE001
            log(f"Bluetooth scan unavailable: {error}")
        finally:
            self.scanning = False

    def select(self, address: str) -> dict:
        """Make `address` the active camera (a new one gets paired: the camera asks to approve "OSMO")."""
        address = address.upper()
        seen = self.nearby.get(address)
        if seen and seen.get("unsupported"):
            return {"ok": False, "error": seen["unsupported"]}
        if self.registry.get(address) is None and seen is None:
            return {"ok": False, "error": "camera not found: turn it on, bring it closer and search again"}
        self.stage, self.stage_error = None, None
        if self.target != address:
            self.target = address
            self.target_changed.set()
        self.want_link.set()
        return {"ok": True}

    def forget(self, address: str) -> None:
        address = address.upper()
        self.registry.forget(address)
        if self.target == address:
            self.target = None
            self.target_changed.set()

    def set_stage(self, stage: str | None, error: str | None = None) -> None:
        self.stage, self.stage_error = stage, error

    def cameras_snapshot(self) -> dict:
        now = time.monotonic()

        def seen_info(address: str) -> dict:
            heard = self.nearby.get(address)
            if not heard or now - heard["t"] > 20:
                return {"seen": False}
            return {"seen": True, "rssi": heard["rssi"], "power": heard.get("power")}

        known = []
        for cam_info in self.registry.public():
            info = dict(cam_info, **seen_info(cam_info["address"]))
            info["active"] = cam_info["address"] == self.target
            info["connected"] = info["active"] and self.link == "on"
            known.append(info)
        known_ids = {c["address"] for c in known}
        nearby = [dict(n, **seen_info(a)) for a, n in sorted(self.nearby.items(), key=lambda i: -i[1]["rssi"])
                  if a not in known_ids and (now - n["t"] <= 20 or a == self.target)]
        return {"target": self.target, "known": known, "nearby": nearby, "scanning": self.scanning,
                "stage": self.stage, "stage_error": self.stage_error, "link": self.link,
                "tested_models": [MODELS[m] for m in sorted(TESTED_MODELS)]}
