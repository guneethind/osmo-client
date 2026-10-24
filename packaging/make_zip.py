"""Makes the portable zip: the program folder plus portable.txt (which switches the app to keeping its data
beside itself). File order and timestamps are fixed (SOURCE_DATE_EPOCH, else the last commit), so the same
sources give the same archive.
Usage: python packaging/make_zip.py <program folder> <output zip> <top folder name inside the zip>"""

import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

PORTABLE_TEXT = """This file makes Osmo Client portable: the linked cameras, the logs and the files copied from the
camera are kept in the "data" folder next to OsmoClient.exe, and nothing is written elsewhere on the computer.
Delete this file to use the usual per-user folders instead (%LOCALAPPDATA%\osmo-pc).
Keep the folder's path short (for example C:\OsmoClient): Windows Bluetooth libraries fail from long paths.
"""


def source_date() -> tuple:
    stamp = os.environ.get("SOURCE_DATE_EPOCH")
    if not stamp:
        stamp = subprocess.run(["git", "log", "-1", "--format=%ct"], capture_output=True, text=True).stdout.strip()
    moment = time.gmtime(max(int(stamp or 0), 315532800))       # zip cannot hold dates before 1980
    return moment[:6]


def main(folder: Path, target: Path, top: str) -> None:
    when = source_date()
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def add(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(f"{top}/{name}", when)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
        for path in files:
            add(path.relative_to(folder).as_posix(), path.read_bytes())
        add("portable.txt", PORTABLE_TEXT.encode("utf-8"))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
