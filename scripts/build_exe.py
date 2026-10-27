#!/usr/bin/env python3
"""Budowa DiskRecover.exe przez PyInstaller (Windows).

Uruchomienie na Windows 11:
    python scripts/build_exe.py

Wynik: dist/DiskRecover/DiskRecover.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Zainstaluj PyInstaller: pip install pyinstaller")
        return 1

    entry = ROOT / "diskrecover" / "__main__.py"
    name = "DiskRecover"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        name,
        "--paths",
        str(ROOT),
        "--hidden-import",
        "PySide6",
        "--collect-all",
        "PySide6",
        str(entry),
    ]
    print("Uruchamiam:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
