# PyInstaller spec for the Windows build: two programs sharing one folder of libraries.
#   OsmoClient.exe          no console window; tray icon; the normal way to start the app
#   OsmoClient-console.exe  same app with a console: --help, --report, support
# Build with packaging\build.ps1 (it makes the version resource and the license file this spec expects).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).resolve().parent
build = root / "build"
icon = str(root / "assets" / "osmo-client.ico")
version = str(build / "version_info.txt")

# bleak picks its Windows backend at run time and the WinRT projections are namespace packages: neither is
# found by import analysis alone (the build runs, the first Bluetooth scan fails).
winrt_datas, winrt_binaries, winrt_hidden = collect_all("winrt")
hidden = collect_submodules("bleak.backends.winrt") + winrt_hidden

datas = [(str(root / "client" / "web"), "client/web"),
         (str(root / "assets" / "osmo-client.ico"), "assets"),
         (str(root / "LICENSE"), "licenses"),
         (str(root / "NOTICE.md"), "licenses")]
if (build / "THIRD_PARTY_LICENSES.txt").is_file():
    datas.append((str(build / "THIRD_PARTY_LICENSES.txt"), "licenses"))

analysis = Analysis(
    [str(root / "osmo_client.py")],
    pathex=[str(root)],
    binaries=winrt_binaries,
    datas=datas + winrt_datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pydoc", "lib2to3", "bleak.backends.corebluetooth", "bleak.backends.bluezdbus",
              "bleak.backends.p4android"],
    noarchive=False,
)
archive = PYZ(analysis.pure)


def program(name, console):
    return EXE(archive, analysis.scripts, [], exclude_binaries=True, name=name, console=console,
               icon=icon, version=version, upx=False, bootloader_ignore_signals=False)


windowed = program("OsmoClient", False)
console = program("OsmoClient-console", True)

collect = COLLECT(windowed, console, analysis.binaries, analysis.datas, upx=False, name="OsmoClient")
