"""The camera's file list and thumbnails, kept on this computer so the gallery opens at once, also while the
camera is off or asleep. One folder per camera under <data folder>/media; nothing is ever uploaded."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path


class MediaCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def _folder(self, camera: str) -> Path:
        return self.root / "".join(ch if ch.isalnum() else "-" for ch in camera)

    def load(self, camera: str) -> dict | None:
        """{updated, stores, files} as saved after the last full listing, or None."""
        try:
            data = json.loads((self._folder(camera) / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data.get("files"), list) else None

    def save(self, camera: str, updated: float | None, stores: dict, files: list[dict]) -> None:
        folder = self._folder(camera)
        with self._lock, contextlib.suppress(OSError):
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / "index.json.tmp"
            temp.write_text(json.dumps({"updated": updated, "stores": stores, "files": files}), encoding="utf-8")
            os.replace(temp, folder / "index.json")     # never leave a half-written list behind

    def _thumb_path(self, camera: str, key: str) -> Path:
        return self._folder(camera) / "thumbs" / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".jpg")

    def thumb(self, camera: str, key: str) -> bytes | None:
        try:
            return self._thumb_path(camera, key).read_bytes()
        except OSError:
            return None

    def put_thumb(self, camera: str, key: str, data: bytes) -> None:
        path = self._thumb_path(camera, key)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def forget(self, camera: str) -> None:
        shutil.rmtree(self._folder(camera), ignore_errors=True)
