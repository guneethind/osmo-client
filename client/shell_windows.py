"""The packaged Windows app's place on the desktop: one running copy, a tray icon (Open, Downloads, Quit), and
a clean stop when the console is closed or Windows logs off or shuts down. Plain ctypes, no extra library.

Everything here only asks the app to stop (client.lifecycle.request_quit); the clean-up itself, putting the
computer's Wi-Fi back, is the app's own.
"""

from __future__ import annotations

import ctypes
import threading
import traceback
import webbrowser
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from camlink.util import log, resource_path

from . import lifecycle

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_ERROR_ALREADY_EXISTS = 183
_WM_DESTROY, _WM_CLOSE, _WM_COMMAND, _WM_USER = 0x0002, 0x0010, 0x0111, 0x0400
_WM_QUERYENDSESSION, _WM_ENDSESSION = 0x0011, 0x0016
_WM_LBUTTONDBLCLK, _WM_RBUTTONUP, _WM_CONTEXTMENU = 0x0203, 0x0205, 0x007B
_WM_TRAY = _WM_USER + 1
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 1, 2, 4
_MF_STRING, _MF_SEPARATOR = 0, 0x800
_TPM_RIGHTBUTTON, _TPM_RETURNCMD = 0x0002, 0x0100
_IMAGE_ICON, _LR_LOADFROMFILE, _LR_DEFAULTSIZE = 1, 0x10, 0x40
_IDI_APPLICATION = 32512
_MB_OK, _MB_ICONERROR = 0, 0x10
_CTRL_EVENTS = (0, 1, 2, 5, 6)           # Ctrl+C, Ctrl+Break, console closed, logoff, shutdown
_ID_OPEN, _ID_FOLDER, _ID_QUIT = 1, 2, 3

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


class _WndClass(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("proc", _WNDPROC), ("class_extra", ctypes.c_int),
                ("window_extra", ctypes.c_int), ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                ("cursor", wintypes.HANDLE), ("background", wintypes.HBRUSH), ("menu_name", wintypes.LPCWSTR),
                ("class_name", wintypes.LPCWSTR)]


class _NotifyIcon(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("window", wintypes.HWND), ("id", wintypes.UINT),
                ("flags", wintypes.UINT), ("callback_message", wintypes.UINT), ("icon", wintypes.HICON),
                ("tip", ctypes.c_wchar * 128), ("state", wintypes.DWORD), ("state_mask", wintypes.DWORD),
                ("info", ctypes.c_wchar * 256), ("version", wintypes.UINT), ("info_title", ctypes.c_wchar * 64),
                ("info_flags", wintypes.DWORD), ("guid", ctypes.c_byte * 16), ("balloon_icon", wintypes.HICON)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_user32.DefWindowProcW.restype = _LRESULT
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
_user32.LoadIconW.restype = wintypes.HICON
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
_user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.LPVOID]
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIcon)]

_keep: list = []                          # objects Windows keeps pointing to (callbacks, the mutex)


def show_error(title: str, message: str) -> None:
    """A message box for a failure that has no console to be printed on."""
    _user32.MessageBoxW(None, message, title, _MB_OK | _MB_ICONERROR)


def single_instance(port: int) -> bool:
    """False if another copy of the app already runs (for this port): the caller opens its page and exits."""
    handle = _kernel32.CreateMutexW(None, False, f"Local\\OsmoClient.{port}")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return False
    _keep.append(handle)                  # held until the process ends
    return True


def watch_console_events() -> None:
    """Closing the console window, Ctrl+C/Break, logoff and shutdown: stop cleanly. Windows ends the process a
    few seconds after a close event, so wait here for the clean-up (Wi-Fi back) to finish, up to 4.5 s."""
    def handler(event: int) -> bool:
        if event not in _CTRL_EVENTS:
            return False
        log("Stopping (the console was closed or Windows is ending the session)...")
        lifecycle.request_quit()
        lifecycle.wait_stopped(4.5)
        return True

    callback = _HANDLER(handler)
    _keep.append(callback)
    _kernel32.SetConsoleCtrlHandler(callback, True)


def start_tray(port: int, downloads: Callable[[], Path | None], version: str) -> None:
    """Run the tray icon, and the hidden window that hears Windows ending the session, in a thread of their own."""
    def run() -> None:
        try:
            _tray(port, downloads, version)
        except Exception:  # noqa: BLE001 - the app works without its icon: say why and go on
            log("No tray icon:\n" + traceback.format_exc())

    threading.Thread(target=run, daemon=True, name="tray").start()


def _tray(port: int, downloads: Callable[[], Path | None], version: str) -> None:
    from .downloads import open_folder
    url = f"http://127.0.0.1:{port}/"
    taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")   # Explorer restarted: add the icon again
    icon_file = resource_path("assets", "osmo-client.ico")
    icon = None
    if icon_file.is_file():
        icon = _user32.LoadImageW(None, str(icon_file), _IMAGE_ICON, 0, 0, _LR_LOADFROMFILE | _LR_DEFAULTSIZE)
    icon = icon or _user32.LoadIconW(None, _IDI_APPLICATION)
    data = _NotifyIcon()
    data.size = ctypes.sizeof(_NotifyIcon)
    data.flags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
    data.callback_message = _WM_TRAY
    data.icon = icon
    data.tip = f"Osmo Client {version}"[:127]

    def add() -> None:
        _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data))

    def menu(window) -> None:
        popup = _user32.CreatePopupMenu()
        _user32.AppendMenuW(popup, _MF_STRING, _ID_OPEN, "Open Osmo Client")
        _user32.AppendMenuW(popup, _MF_STRING, _ID_FOLDER, "Open the downloads folder")
        _user32.AppendMenuW(popup, _MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(popup, _MF_STRING, _ID_QUIT, "Quit (puts your Wi-Fi back)")
        point = _Point()
        _user32.GetCursorPos(ctypes.byref(point))
        _user32.SetForegroundWindow(window)        # else the menu does not close when clicking elsewhere
        choice = _user32.TrackPopupMenu(popup, _TPM_RIGHTBUTTON | _TPM_RETURNCMD, point.x, point.y, 0, window, None)
        _user32.DestroyMenu(popup)
        if choice == _ID_OPEN:
            webbrowser.open(url)
        elif choice == _ID_FOLDER:
            folder = downloads()
            if folder is not None:
                folder.mkdir(parents=True, exist_ok=True)
                open_folder(folder)
        elif choice == _ID_QUIT:
            lifecycle.request_quit()

    def proc(window, message, wparam, lparam) -> int:
        if message == _WM_TRAY:
            if lparam == _WM_LBUTTONDBLCLK:
                webbrowser.open(url)
            elif lparam in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                menu(window)
            return 0
        if message == taskbar_created:
            add()
            return 0
        if message == _WM_QUERYENDSESSION:
            return 1                               # never hold up logoff or shutdown
        if message == _WM_ENDSESSION:
            if wparam:
                lifecycle.request_quit()
                lifecycle.wait_stopped(4.5)
            return 0
        if message == _WM_DESTROY:
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(window, message, wparam, lparam)

    callback = _WNDPROC(proc)
    _keep.append(callback)
    instance = _kernel32.GetModuleHandleW(None)
    window_class = _WndClass()
    window_class.proc = callback
    window_class.instance = instance
    window_class.class_name = "OsmoClientTray"
    if not _user32.RegisterClassW(ctypes.byref(window_class)):
        log(f"No tray icon (could not register its window: error {ctypes.get_last_error()})")
        return
    window = _user32.CreateWindowExW(0, "OsmoClientTray", "Osmo Client", 0, 0, 0, 0, 0, None, None, instance, None)
    if not window:
        log(f"No tray icon (could not create its window: error {ctypes.get_last_error()})")
        return
    data.window = window
    data.id = 1
    add()
    log("Tray icon ready")

    def close_when_stopped() -> None:
        lifecycle.wait_stopped(10 ** 6)
        _user32.PostMessageW(window, _WM_CLOSE, 0, 0)     # the icon goes away with the app

    threading.Thread(target=close_when_stopped, daemon=True, name="tray-close").start()
    message = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(message))
        _user32.DispatchMessageW(ctypes.byref(message))
