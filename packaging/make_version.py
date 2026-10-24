"""Writes build/version_info.txt: the name, version and company shown in the exe's Properties dialog.
Run by build.ps1 before PyInstaller."""

import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
version = re.search(r'__version__ = "([^"]+)"', (root / "camlink" / "__init__.py").read_text(encoding="utf-8")).group(1)
numbers = [int(part) for part in version.split(".")[:3]] + [0] * (4 - len(version.split(".")[:3]))
tuple_ = tuple(numbers[:4])

text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple_}, prodvers={tuple_}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'The Osmo Client authors'),
      StringStruct('FileDescription', 'Osmo Client: control a DJI Osmo camera from the computer'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'OsmoClient'),
      StringStruct('LegalCopyright', 'MIT License. Not affiliated with DJI.'),
      StringStruct('OriginalFilename', 'OsmoClient.exe'),
      StringStruct('ProductName', 'Osmo Client'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
(root / "build").mkdir(exist_ok=True)
(root / "build" / "version_info.txt").write_text(text, encoding="utf-8")
print(version)
