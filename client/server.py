"""The local web UI: serves the page and a small JSON API on 127.0.0.1 (never on the network)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from camlink import camera as cam, media
from camlink.util import log

from .app import App
from .downloads import open_folder

PAGE_FILE = Path(__file__).resolve().parent / "web" / "index.html"
CONTENT_TYPES = {"LRF": "video/mp4", "MP4": "video/mp4", "THM": "image/jpeg", "JPG": "image/jpeg"}


def _builtin_formats(mode: int | None) -> list[tuple[int, int]]:
    if mode == cam.MODE_SLOWMO:
        return [(r, f) for r, f, _ in cam.SLOWMO_FORMATS]
    formats = cam.MODE_FORMATS.get(mode)
    if not formats:
        return []
    return [(r, f) for r in formats["res"] for f in cam.LIMITED_FPS.get(r, formats["fps"])]


def exposure_options(app: App) -> dict:
    """Everything the page needs to fill its selectors, for the current capture mode: the tables the camera
    publishes when it does, otherwise the built-in ones (an Osmo Nano's). `key` changes whenever the
    answer would, so the page knows when to ask again."""
    version, caps = app.state.capabilities()
    mode = app.state.capture_mode()
    pairs = caps.get("camcap_video_format")
    if pairs is None:
        pairs = _builtin_formats(mode)
    rate = lambda code: cam.FRAME_RATES.get(code, code)  # noqa: E731
    order = {res: i for i, res in reversed(list(enumerate(r for r, _ in pairs)))}   # first appearance
    pairs = sorted(pairs, key=lambda p: (order[p[0]], rate(p[1])))
    formats: dict[int, list[int]] = {}
    slowmo = []
    if mode == cam.MODE_SLOWMO:
        slowmo = [{"res": r, "fps": f, "label": cam.slowmo_label(r, f)} for r, f in pairs]
    else:
        for r, f in pairs:
            formats.setdefault(r, []).append(f)
    photo = caps.get("camcap_photo_size") or [(s, r) for s in cam.PHOTO_SIZE_CODES for r in cam.PHOTO_RATIOS]
    low, high = caps.get("camcap_wb") or (cam.WB_KELVIN.start, cam.WB_KELVIN.stop - 1)
    iso = caps.get("camcap_iso")
    if iso is None:
        iso = cam.MODE_ISO.get(mode, cam.DEFAULT_ISO)
    return {
        "key": f"{version}:{'' if mode is None else mode}",
        "source": "camera" if caps else "built-in",
        "capture_modes": caps.get("camcap_base"),    # None: not published, show every known mode
        "modes": list(cam.EXPOSURE_MODES),
        "iso": [{"code": c, "value": cam.ISO_VALUES.get(c)} for c in iso],
        "shutter": [{"raw": r, "label": cam.shutter_text(r)} for r in caps.get("camcap_shutter") or cam.SHUTTER_SPEEDS
                    if cam.shutter_text(r)],
        "fps": [{"code": c, "value": v} for c, v in sorted(cam.FRAME_RATES.items(), key=lambda i: i[1])],
        "resolutions": {r: cam.RESOLUTIONS.get(r, f"0x{r:02x}") for r in [*cam.RESOLUTIONS, *formats]},
        "formats": [[r, f] for r, f in formats.items()],     # resolution -> frame rates, current mode
        "slowmo": slowmo,
        "photo_formats": [{"size": s, "ratio": r, "label": cam.photo_label(s, r)} for s, r in photo],
        "wb_kelvin": list(range(low, high + 1, 100)),
    }


def start_server(app: App, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def _json(self, obj: dict, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _bad(self, error: str) -> None:
            self._json({"ok": False, "error": error}, 400)

        def _tail_int(self) -> int | None:
            try:
                return int(self.path.rsplit("/", 1)[1], 0)
            except ValueError:
                return None

        def _file_query(self) -> tuple[int, str]:
            q = parse_qs(urlsplit(self.path).query)
            return int(q.get("s", ["1"])[0]), q.get("p", [""])[0]

        def _body(self) -> dict:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def _relay(self, storage: int, path: str, filename: str | None = None) -> None:
            """Relay a camera file (Range passes through, so the <video> preview can seek)."""
            headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
            try:
                upstream = urllib.request.urlopen(urllib.request.Request(media.http_url(storage, path), headers=headers),
                                                  timeout=10)
            except urllib.error.HTTPError as error:
                self.send_error(error.code)
                return
            except (urllib.error.URLError, OSError):
                self.send_error(502, "the camera is not responding")
                return
            with upstream:
                self.send_response(upstream.status)
                ctype = CONTENT_TYPES.get(path.rsplit(".", 1)[-1].upper())
                self.send_header("Content-Type", ctype or upstream.headers.get("Content-Type", "application/octet-stream"))
                for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified"):
                    if upstream.headers.get(name):
                        self.send_header(name, upstream.headers[name])
                if filename:
                    self.send_header("Content-Disposition", f'attachment; filename="{Path(filename).name}"')
                self.end_headers()
                with contextlib.suppress(OSError):
                    while chunk := upstream.read(1 << 16):
                        self.wfile.write(chunk)

        def _stream(self) -> None:
            """Live view as MJPEG."""
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = -1
            while True:
                with app.frame_cond:
                    app.frame_cond.wait_for(lambda: app.frame_seq != last, timeout=2)
                    frame, last = app.latest_frame, app.frame_seq
                if frame is None:
                    continue
                buffer = io.BytesIO()
                frame.to_image().save(buffer, "JPEG", quality=80)
                jpeg = buffer.getvalue()
                try:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(jpeg) + jpeg + b"\r\n")
                except OSError:
                    return

        # ---- POST: actions
        def do_POST(self) -> None:  # noqa: C901 - one flat route table reads best here
            path = self.path
            if path == "/api/record/start":
                self._json(app.record(True))
            elif path == "/api/record/stop":
                self._json(app.record(False))
            elif path == "/api/photo":
                self._json(app.take_photo())
            elif path in ("/api/link/on", "/api/link/off"):
                (app.want_link.set if path.endswith("on") else app.want_link.clear)()
                self._json({"ok": True})
            elif path.startswith("/api/mode/"):
                mode = self._tail_int()
                if mode not in cam.MODES:
                    self._bad("unknown capture mode")
                else:
                    self._json(app.set_capture_mode(mode))
            elif path.startswith("/api/exposure/"):
                mode = cam.EXPOSURE_MODES.get(path.rsplit("/", 1)[1])
                if mode is None:
                    self._bad("expected auto or manual")
                else:
                    self._json(app.set_setting(0x1E, bytes((mode, 0))))
            elif path.startswith("/api/iso/"):
                code = self._tail_int()
                if code not in [o["code"] for o in exposure_options(app)["iso"]]:
                    self._bad("ISO not available in this capture mode")
                else:
                    self._json(app.set_setting(0x2A, bytes((code,))))
            elif path.startswith("/api/shutter/"):
                raw = self._tail_int()
                if raw not in [o["raw"] for o in exposure_options(app)["shutter"]]:
                    self._bad("shutter speed not available in this capture mode")
                else:
                    self._json(app.set_setting(0x28, cam.shutter_payload(raw)))
            elif path.startswith("/api/fps/"):
                code = self._tail_int()
                if code not in cam.FRAME_RATES:
                    self._bad("unknown frame rate")
                else:
                    self._json(app.set_frame_rate(code))
            elif path == "/api/video-format":
                try:
                    body = self._body()
                    res, fps = int(body["res"]), int(body["fps"])
                except (ValueError, KeyError, TypeError):
                    self._bad("expected {res, fps}")
                    return
                options = exposure_options(app)
                valid = ({"res": res, "fps": fps} in [{"res": o["res"], "fps": o["fps"]} for o in options["slowmo"]]
                         or fps in dict(options["formats"]).get(res, []))
                if not valid:
                    self._bad("format not available in this capture mode")
                else:
                    self._json(app.set_video_format(res, fps))
            elif path == "/api/photo-format":
                try:
                    body = self._body()
                    size, ratio = int(body["size"]), int(body["ratio"])
                except (ValueError, KeyError, TypeError):
                    self._bad("expected {size, ratio}")
                    return
                if {"size": size, "ratio": ratio} not in [{"size": o["size"], "ratio": o["ratio"]}
                                                          for o in exposure_options(app)["photo_formats"]]:
                    self._bad("unknown photo format")
                else:
                    self._json(app.set_setting(0x12, bytes((size, ratio))))
            elif path == "/api/wb":
                try:
                    body = self._body()
                    manual = body["mode"] == "manual"
                    kelvin = int(body["kelvin"]) if body.get("kelvin") is not None else None
                except (ValueError, KeyError, TypeError):
                    self._bad("expected {mode: auto|manual, kelvin}")
                    return
                kelvins = exposure_options(app)["wb_kelvin"]
                if kelvin is not None and kelvin not in kelvins:
                    self._bad(f"colour temperature out of range ({kelvins[0]}-{kelvins[-1]} K)")
                else:
                    self._json(app.set_white_balance(manual, kelvin))
            elif path in ("/api/cameras/connect", "/api/cameras/forget"):
                try:
                    address = str(self._body()["address"])
                except (ValueError, KeyError, TypeError):
                    self._bad("expected {address}")
                    return
                if path.endswith("connect"):
                    self._json(app.select(address))
                else:
                    app.forget(address)
                    self._json({"ok": True})
            elif path == "/api/cameras/scan":
                if app.loop is not None and app.link == "on":
                    asyncio.run_coroutine_threadsafe(app.quick_scan(), app.loop)
                self._json({"ok": True})
            elif path in ("/api/media/mode/on", "/api/media/mode/off"):
                self._json(app.set_media_hold(path.endswith("/on")))
            elif path == "/api/media/refresh":
                self._json(app.refresh_media())
            elif path == "/api/media/download":
                try:
                    items = self._body()["items"]
                    added = [app.downloader.add(int(i["storage"]), str(i["path"]), str(i["name"]), int(i["size"]))
                             for i in items]
                except (ValueError, KeyError, TypeError):
                    self._bad("expected {items: [{storage, path, name, size}]}")
                    return
                self._json({"ok": True, "queued": len(added)})
            elif path.startswith("/api/media/cancel/"):
                app.downloader.cancel(self._tail_int() or 0)
                self._json({"ok": True})
            elif path == "/api/media/clear":
                app.downloader.clear_finished()
                self._json({"ok": True})
            elif path == "/api/media/open-folder":
                folder = app.downloader.folder
                folder.mkdir(parents=True, exist_ok=True)
                try:
                    open_folder(folder)
                except OSError as error:      # no file manager to hand it to (xdg-open missing...)
                    self._json({"ok": False, "error": f"could not open {folder}: {error}"})
                    return
                self._json({"ok": True})
            else:
                self.send_error(404)

        # ---- GET: page, state, files, live view
        def do_GET(self) -> None:
            path = self.path
            if path == "/":
                body = PAGE_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/status":
                self._json(app.status())
            elif path == "/api/topics":
                self._json(app.state.topics_snapshot())
            elif path == "/api/cameras":
                self._json(app.cameras_snapshot())
            elif path == "/api/exposure/options":
                self._json(exposure_options(app))
            elif path == "/api/media":
                self._json(app.media_snapshot())
            elif path == "/api/media/downloads":
                self._json({"items": app.downloader.snapshot(), "folder": str(app.downloader.folder)})
            elif path.startswith("/api/media/thumb"):
                data = app.thumbnail(*self._file_query())
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/api/media/view"):
                self._relay(*self._file_query())
            elif path.startswith("/api/media/file"):
                storage, file_path = self._file_query()
                self._relay(storage, file_path, filename=file_path.rsplit("/", 1)[-1])
            elif path.startswith("/stream"):
                self._stream()
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log(f"Osmo Client: http://127.0.0.1:{port}/")
    return server
