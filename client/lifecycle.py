"""How the app is asked to stop, from any thread: the page's Quit button, the tray icon, the console being
closed, Windows logging off. All of them end in the same place: the main task is cancelled, so the `finally`
of osmo_client.run() puts the computer's Wi-Fi back and lets go of the camera."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

_cancel: Callable[[], None] | None = None
_folder: Callable[[], Path] | None = None
_stopped = threading.Event()
_asked = threading.Event()


def install(cancel: Callable[[], None], downloads: Callable[[], Path]) -> None:
    """Called by the main task, with a function that cancels it from another thread and one that tells
    where the files copied from the camera go (for the tray menu)."""
    global _cancel, _folder
    _cancel, _folder = cancel, downloads


def downloads_folder() -> Path | None:
    return _folder() if _folder is not None else None


def request_quit() -> None:
    """Ask the app to stop (safe from any thread, and to call more than once)."""
    _asked.set()
    if _cancel is not None:
        _cancel()


def mark_stopped() -> None:
    """The main task has finished its clean-up (Wi-Fi restored, camera released)."""
    _stopped.set()


def wait_stopped(timeout: float) -> bool:
    """Wait for the clean-up to finish; False if it took longer than `timeout` seconds."""
    return _stopped.wait(timeout)


def quit_asked() -> bool:
    return _asked.is_set()
