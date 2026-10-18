"""Load the packaged Qt DLLs before publishing a Windows build.

Run in a fresh Python process without importing the development PySide6:
preloaded development DLLs can hide missing or incompatible bundled DLLs.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def check_bundle(bundle_root: Path) -> None:
    # Match the directories used by the frozen app and PySide6's startup.
    directories = [bundle_root, bundle_root / "PySide6", bundle_root / "shiboken6"]
    handles = [os.add_dll_directory(str(directory)) for directory in directories]
    try:
        libraries = []
        for name in ("QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtMultimedia"):
            path = bundle_root / "PySide6" / f"{name}.pyd"
            try:
                libraries.append(ctypes.WinDLL(str(path)))
            except OSError as exc:
                raise RuntimeError(f"Packaged {name} cannot load: {exc}") from exc
        print("Packaged Qt DLLs loaded successfully.")
    finally:
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    check_bundle(Path(sys.argv[1]).resolve())
