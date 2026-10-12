"""Copies files from the camera's HTTP server to the PC, one at a time, resuming after drops."""

from __future__ import annotations

import contextlib
import ctypes
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from camlink import media
from camlink.util import log


def default_download_dir() -> Path:
    """The user's Videos folder (even when it has been moved), subfolder 'DJI Osmo'."""
    folder = None
    with contextlib.suppress(Exception):
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x000E, None, 0, buffer) == 0:  # CSIDL_MYVIDEO
            folder = Path(buffer.value)
    return (folder or Path.home() / "Videos") / "DJI Osmo"


class Downloader:
    """A download queue with progress for the page. The camera can cut long transfers, so each file
    resumes with an HTTP Range request. Every camera gets its own subfolder (file names such as
    DJI_..._0001_D.MP4 repeat across cameras)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.sub = ""                     # active camera's name
        self.items: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[int] = queue.Queue()
        self._next_id = 1
        threading.Thread(target=self._worker, daemon=True, name="downloads").start()

    @property
    def folder(self) -> Path:
        return self.base / self.sub if self.sub else self.base

    def is_downloaded(self, name: str, size: int) -> bool:
        with contextlib.suppress(OSError):
            return (self.folder / Path(name).name).stat().st_size == size
        return False

    def add(self, storage: int, path: str, name: str, size: int) -> dict:
        with self._lock:
            for item in self.items.values():
                if item["path"] == path and item["state"] in ("queued", "downloading"):
                    return item
            item = {"id": self._next_id, "storage": storage, "path": path, "name": Path(name).name,
                    "size": size, "done": 0, "speed": 0, "state": "queued", "error": None, "cancel": False,
                    "folder": str(self.folder)}
            self.items[item["id"]] = item
            self._next_id += 1
        self._queue.put(item["id"])
        return item

    def cancel(self, item_id: int) -> None:
        with self._lock:
            self._cancel(self.items.get(item_id))

    def cancel_all(self) -> None:
        """Another camera was picked: every camera answers on the same address, so queued paths must
        not reach the new one."""
        with self._lock:
            for item in self.items.values():
                self._cancel(item)

    @staticmethod
    def _cancel(item: dict | None) -> None:
        if item and item["state"] in ("queued", "downloading"):
            item["cancel"] = True
            if item["state"] == "queued":
                item["state"] = "cancelled"

    def clear_finished(self) -> None:
        with self._lock:
            for key in [k for k, v in self.items.items() if v["state"] in ("done", "cancelled", "error")]:
                del self.items[key]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in item.items() if k != "cancel"} for item in self.items.values()]

    def active(self) -> int:
        with self._lock:
            return sum(item["state"] in ("queued", "downloading") for item in self.items.values())

    def _worker(self) -> None:
        while True:
            item_id = self._queue.get()
            with self._lock:
                item = self.items.get(item_id)
                if item is None or item["state"] != "queued":
                    continue
                item["state"] = "downloading"
            try:
                self._fetch(item)
            except Exception as error:  # noqa: BLE001 - shown on the page, the queue keeps going
                item["state"], item["error"] = "error", str(error)
                log(f"Download failed: {item['name']}: {error}")

    def _fetch(self, item: dict) -> None:
        folder = Path(item["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / item["name"]
        if final.exists() and final.stat().st_size == item["size"]:
            item["done"], item["state"] = item["size"], "done"
            return
        part = final.with_name(final.name + ".part")
        url = media.http_url(item["storage"], item["path"])
        failures = 0
        started, start_bytes = time.monotonic(), None
        while True:
            have = part.stat().st_size if part.exists() else 0
            item["done"] = have
            if start_bytes is None:
                start_bytes = have
            if item["size"] and have >= item["size"]:
                break
            request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
            try:
                with urllib.request.urlopen(request, timeout=15) as response, open(part, "ab" if have else "wb") as out:
                    if have and response.status != 206:
                        raise OSError("the camera did not accept resuming the download")
                    total = response.headers.get("Content-Range", "").rsplit("/", 1)[-1]
                    if total.isdigit():
                        item["size"] = int(total)
                    elif not have and response.headers.get("Content-Length", "").isdigit():
                        item["size"] = int(response.headers["Content-Length"])
                    while True:
                        if item["cancel"]:
                            item["state"] = "cancelled"
                            return
                        chunk = response.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        item["done"] += len(chunk)
                        elapsed = time.monotonic() - started
                        if elapsed > 0.5:
                            item["speed"] = int((item["done"] - start_bytes) / elapsed)
                if item["size"] and item["done"] >= item["size"]:
                    break
                error = OSError("the camera closed the connection")   # resume where it stopped
            except (urllib.error.URLError, OSError) as caught:
                error = caught
            failures += 1
            if failures > 8:
                raise OSError(f"too many interruptions ({error})")
            time.sleep(min(2 * failures, 10))
        os.replace(part, final)
        item["state"], item["error"] = "done", None
        log(f"Downloaded {item['name']} ({item['done'] / 1e6:.1f} MB) to {final}")
