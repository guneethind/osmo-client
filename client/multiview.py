"""Multiview: more cameras next to the active one.

The computer can't be on several cameras' own access points at once, so each extra camera must use a
shared Wi-Fi network (station mode, see client/station.py). Each one gets its own Bluetooth link,
datalink session and decoder, and the page shows them side by side; recording can start and stop on all
of them at once (a shared command, not frame-accurate sync). Removing a camera puts it back on its own
Wi-Fi.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

from camlink import ble, camera as cam, datalink, models
from camlink.duml import Message
from camlink.registry import DEFAULT_TRANSPORT
from camlink.util import log
from camlink.video import VideoDecoder

from .station import back_to_own_wifi, reach
from .watchdog import FeedWatchdog, start_wait

MAX_EXTRA = 3                        # extra cameras: four pictures with the active one
STALL = 3.0


class Tile:
    """One extra camera. run() is a coroutine on the app's event loop until close()."""

    def __init__(self, app, address: str) -> None:
        self.app = app
        self.address = address
        known = app.registry.get(address) or {}
        self.name = known.get("name") or address
        self.state = "connecting"    # connecting | live | error
        self.stage: str | None = None
        self.note: str | None = None         # progress within a step (network join attempts, the search)
        self.error: str | None = None
        self.status: dict | None = None      # last 02/80 push
        self.video_grace = 0.0               # monotonic(): a mode change pauses the video until then
        self.last_status = 0.0
        self.battery: dict | None = None
        self.host: str | None = None
        # live view, read by the page's MJPEG stream like the active camera's
        self.latest_frame = None
        self.frame_seq = 0
        self.last_frame = 0.0
        self.frame_cond = threading.Condition()
        self.closing = False
        self._decoder_stop = threading.Event()
        self.decoder = VideoDecoder(self.on_frame, self._decoder_stop)
        self.decoder.start()
        self.session: datalink.Datalink | None = None
        self.link: ble.BleLink | None = None
        self._waiting: dict[tuple[int, int], list] = {}
        self._lock = threading.Lock()

    # ---- incoming
    def on_frame(self, frame, _first_packet: float = 0.0) -> None:
        with self.frame_cond:
            self.latest_frame = frame
            self.frame_seq += 1
            self.last_frame = time.monotonic()
            self.frame_cond.notify_all()

    def on_message(self, m: Message) -> None:
        if m.is_reply:
            with self._lock:
                waiter = self._waiting.get(m.command)
            if waiter is not None:
                waiter[1] = bytes(m.payload)
                waiter[0].set()
        elif m.command == (0x02, 0x80):
            self.status = cam.parse_status(m.payload) or self.status
            self.last_status = time.monotonic()
        elif m.command == (0x0D, 0x02):
            self.battery = cam.parse_battery(m.payload) or self.battery

    # ---- commands
    def request(self, cmd_set: int, cmd_id: int, payload: bytes, timeout: float = 2.5) -> dict:
        session = self.session
        if session is None or not session.ready.is_set():
            return {"ok": False, "error": "not connected"}
        waiter = [threading.Event(), b""]
        with self._lock:
            if (cmd_set, cmd_id) in self._waiting:
                return {"ok": False, "error": "another command is in progress"}
            self._waiting[(cmd_set, cmd_id)] = waiter
        try:
            session.send(cmd_set, cmd_id, payload)
            if not waiter[0].wait(timeout):
                return {"ok": False, "error": "the camera did not reply"}
        except OSError:
            return {"ok": False, "error": "not connected"}
        finally:
            with self._lock:
                self._waiting.pop((cmd_set, cmd_id), None)
        code = waiter[1][0] if waiter[1] else 0
        if code:
            return {"ok": False, "error": cam.RESULT_TEXT.get(code, f"refused (0x{code:02x})")}
        return {"ok": True}

    def capture_mode(self) -> int | None:
        return self.status["capture_mode"] if self.status else None

    def model_id(self) -> int | None:
        return (self.app.registry.get(self.address) or {}).get("model_id")

    def set_mode(self, mode: int) -> dict:
        self.video_grace = time.monotonic() + 4
        result = self.request(0x02, 0xE1, bytes((mode,)))
        log(f"Multiview {self.name}: mode {cam.mode_name(mode, self.model_id())} -> "
            + ("ok" if result["ok"] else result["error"]))
        return result

    def record(self, start: bool) -> dict:
        """Start or stop recording (timelapse rides take-photo, as on the active camera). A camera left in
        Photo mode is switched to Video first."""
        mode = self.capture_mode()
        if start and mode in cam.PHOTO_MODES:
            switched = self.set_mode(cam.MODE_VIDEO)
            if not switched["ok"]:
                return {"ok": False, "error": f"in Photo mode, and switching to Video failed ({switched['error']})"}
            end = time.monotonic() + 3
            while time.monotonic() < end and self.capture_mode() in cam.PHOTO_MODES:
                time.sleep(0.1)
            mode = cam.MODE_VIDEO
        cmd_id = cam.record_command(self.model_id(), mode)
        result = self.request(0x02, cmd_id, b"\x01" if start else b"\x00")
        log(f"Multiview {self.name}: {'record' if start else 'stop'} -> {'ok' if result['ok'] else result['error']}")
        return result

    # ---- session
    def _in_use(self) -> set[str]:
        """Addresses of the other cameras this computer streams from: never probed while searching (also
        while the active one reconnects: probing it would get in its way)."""
        hosts = {t.host for t in list(self.app.tiles.values()) if t is not self and t.host}
        if self.app.want_link.is_set() and self.app.camera_host:
            hosts.add(self.app.camera_host)
        return hosts

    def _stage(self, name: str) -> None:
        self.stage, self.note = name, None

    def _note(self, text: str | None) -> None:
        self.note = text

    async def _pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.closing:
            await asyncio.sleep(0.25)

    async def run(self) -> None:
        quiet = 0                    # sessions in a row that got no video (each waits longer for it)
        try:
            while not self.closing:
                known = self.app.registry.get(self.address) or {}
                transport = known.get("transport") or dict(DEFAULT_TRANSPORT)
                port = transport.get("port", datalink.DATALINK_PORT)
                network = known.get("network")
                if not network:
                    self.state, self.error = "error", "set a shared Wi-Fi network for this camera first (Cameras)"
                    await self._pause(3)
                    continue
                try:
                    self.state = "connecting"
                    if self.link is None or self.link.disconnected.is_set():
                        if self.link is not None:
                            with contextlib.suppress(Exception):
                                await self.link.close()
                        self.link = ble.BleLink()
                        self.link.on_stage = self._stage
                        await self.link.connect(self.address, 60)
                        self.app.registry.remember(self.link, touch=False)   # not the camera to open next time
                    self.host = await reach(self.link, self.app.registry, self.address, network, port, self._stage,
                                            self._pause, lambda: not self.closing, self._in_use, note=self._note)
                    if transport.get("poke", True):
                        await asyncio.to_thread(datalink.poke, self.host)
                except Exception as error:  # noqa: BLE001 - shown on the tile, retried
                    self.state, self.error = "error", str(error)
                    log(f"Multiview {self.name}: {error}")
                    if isinstance(error, ble.JoinRefused):
                        # the camera refused the shared network (password, WPA3...): retrying can't help
                        self.error += " · fix the network in Cameras, then remove and add it again"
                        while not self.closing:
                            await self._pause(1)
                        break
                    await self._pause(5)
                    continue
                got_video = await self._session(port, known, quiet)
                quiet = 0 if got_video else quiet + 1
                await self._pause(1)
        finally:
            await self._shutdown()

    async def _session(self, port: int, known: dict, quiet: int) -> bool:
        """One datalink session, until it ends or the tile closes. True when video came."""
        self.stage, self.note = "datalink", None
        session = datalink.Datalink(port=port, host=self.host, on_video=self.decoder.feed,
                                    on_message=self.on_message, liveview=models.liveview_mode(known.get("model_id")))
        self.decoder.reset()
        self.session = session
        decoded, errors = self.decoder.frames, self.decoder.errors
        task = asyncio.ensure_future(asyncio.to_thread(session.run))
        watchdog: FeedWatchdog | None = None   # from the live view request (a handshake can take seconds)
        reported = time.monotonic()

        def video() -> str:
            return (f"decoded {self.decoder.frames - decoded}, decode errors {self.decoder.errors - errors}; "
                    + session.pictures_summary())

        try:
            while not task.done() and not self.closing:
                await asyncio.sleep(0.3)
                now = time.monotonic()
                if datalink.DUMP_DIR and now - reported >= 10:
                    reported = now
                    log(f"Diagnostics {self.name}: {video()}")
                if self.last_frame > now - 1:
                    self.state, self.error, self.stage = "live", None, "on"
                if not session.ready.is_set() or session.stopped.is_set():
                    continue
                if watchdog is None:
                    watchdog = FeedWatchdog(STALL, now, start_wait(quiet))
                step = watchdog.check(now, session.last_video, self.last_frame, self.video_grace,
                                      session.frames.truncated if session.frames else 0)
                if step == "keyframe":
                    with contextlib.suppress(OSError):
                        session.request_keyframe()
                elif step == "restart":
                    log(f"Multiview {self.name}: no video, restarting the session ({session.summary()}; "
                        f"decoded {self.decoder.frames - decoded}, decode errors {self.decoder.errors - errors})")
                    session.stop()
        finally:
            self.session = None
            session.stop()
            with contextlib.suppress(Exception):
                await task
        return bool(session.last_video)

    async def _shutdown(self) -> None:
        if self.session is not None:
            self.session.stop()
        await back_to_own_wifi(self.link, self.app.registry, True)
        if self.link is not None:
            with contextlib.suppress(Exception):
                await self.link.close()
        self._decoder_stop.set()
        log(f"Multiview: {self.name} removed")

    def close(self) -> None:
        self.closing = True

    def snapshot(self) -> dict:
        now = time.monotonic()
        status = self.status if self.status and now - self.last_status < 3 else None
        known = self.app.registry.get(self.address) or {}
        return {
            "address": self.address, "name": self.name, "model": known.get("model"), "state": self.state,
            "stage": self.stage, "note": self.note, "error": self.error, "host": self.host,
            "record": None if not status else {"state": cam.RECORD_STATES[status["record_state"]],
                                                "elapsed": status["elapsed"]},
            "mode": None if not status else cam.mode_name(status["capture_mode"], known.get("model_id")),
            "mode_code": None if not status else status["capture_mode"],
            "modes": cam.multiview_modes(known.get("model_id")),
            "battery": self.battery["percent"] if self.battery else None,
            "video_age": round(now - self.last_frame, 1) if self.last_frame else None,
        }
