"""Small helpers shared by the apps: logging, per-user data folder, timer precision (Windows, macOS)."""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import time
from pathlib import Path


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def frozen() -> bool:
    """Running from the packaged Windows build (PyInstaller) and not from the source tree."""
    return bool(getattr(sys, "frozen", False))


def has_console() -> bool:
    """A console window belongs to this process (always true when run from source in a terminal, and for the
    packaged console program; false for the windowed program, even when a terminal started it)."""
    window = getattr(getattr(ctypes, "windll", None), "kernel32", None)
    return True if window is None else bool(window.GetConsoleWindow())


def app_dir() -> Path:
    """The folder of the packaged program, or of the source tree."""
    return Path(sys.executable).resolve().parent if frozen() else Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """A file shipped with the app (the web page), in the source tree or in the packaged build."""
    root = Path(getattr(sys, "_MEIPASS", "")) if frozen() else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)


def portable() -> bool:
    """The packaged build was unpacked with its `portable.txt` beside it: keep everything it writes (linked
    cameras, logs, downloads) in a `data` folder next to the program, and nothing on the computer."""
    return frozen() and (app_dir() / "portable.txt").is_file()


def setup_streams() -> None:
    """The windowed Windows build has no console, so sys.stdout and sys.stderr are None: send them to the log
    file (logs/osmo-client.log in the data folder, restarted when it passes 1 MB, the last one kept as
    .old). With a console, only make the output UTF-8."""
    if sys.stdout is not None and sys.stderr is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return
    folder = data_dir() / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "osmo-client.log"
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".old"))
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)   # noqa: SIM115 - lives as long as the app
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")                              # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def data_dir() -> Path:
    """Per-user folder for settings shared by both apps (linked cameras): %LOCALAPPDATA%\\osmo-pc on
    Windows, ~/Library/Application Support/osmo-pc on macOS, ~/.local/share/osmo-pc on Linux. The portable
    build keeps it in a `data` folder next to the program (see portable())."""
    if portable():
        folder = app_dir() / "data"
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    folder = root / "osmo-pc"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextlib.contextmanager
def fine_timer():
    """Keep short timers on time while the app runs: the video frame flush (5 ms) and the camera link's
    acknowledgements (every 25 ms). Windows: raise the scheduler tick to 1 ms (the default ~15.6 ms rounds
    them up). macOS: declare a latency-critical activity, so App Nap and timer coalescing don't slow the
    app down while its Terminal window is in the background. Linux needs nothing."""
    winmm = getattr(getattr(ctypes, "windll", None), "winmm", None)
    if winmm is not None:
        winmm.timeBeginPeriod(1)
    activity = _begin_macos_activity()
    try:
        yield
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
        if activity is not None:
            activity[0].endActivity_(activity[1])


def _begin_macos_activity():
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSProcessInfo  # pyobjc, installed with bleak on macOS
    except ImportError:
        return None
    # NSActivityUserInitiatedAllowingIdleSystemSleep | NSActivityLatencyCritical: no App Nap, no timer
    # coalescing; the Mac may still go to sleep when idle.
    info = NSProcessInfo.processInfo()
    return info, info.beginActivityWithOptions_reason_(0x00EFFFFF | 0xFF00000000, "Live video from the camera")
