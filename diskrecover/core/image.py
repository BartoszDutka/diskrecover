"""Bezpieczny, tylko-do-odczytu dostęp do obrazu dysku (mmap / strumieniowo)."""

from __future__ import annotations

import mmap
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    size: int
    sector_size_hint: int = 512


class ImageReader:
    """Otwiera obraz wyłącznie w trybie ``rb``. Nigdy nie zapisuje do źródła."""

    def __init__(self, path: str | Path, *, prefer_mmap: bool = True) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"Nie znaleziono obrazu: {self.path}")
        self.size = self.path.stat().st_size
        if self.size <= 0:
            raise ValueError("Obraz ma zerowy rozmiar.")
        self._fh: BinaryIO = open(self.path, "rb")  # noqa: SIM115 — zamykane w close()
        self._mmap: Optional[mmap.mmap] = None
        if prefer_mmap:
            try:
                self._mmap = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                self._mmap = None

    @property
    def info(self) -> ImageInfo:
        return ImageInfo(path=self.path, size=self.size)

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("offset i size muszą być nieujemne")
        if offset >= self.size:
            return b""
        end = min(offset + size, self.size)
        length = end - offset
        if self._mmap is not None:
            return bytes(self._mmap[offset:end])
        self._fh.seek(offset)
        return self._fh.read(length)

    def read_sector(self, sector: int, sector_size: int = 512, count: int = 1) -> bytes:
        return self.read(sector * sector_size, count * sector_size)

    def iter_chunks(self, offset: int, length: int, chunk_size: int = 1024 * 1024):
        """Strumieniowe czytanie zakresu — nie ładuje całego obrazu do RAM."""
        remaining = length
        pos = offset
        while remaining > 0:
            n = min(chunk_size, remaining)
            data = self.read(pos, n)
            if not data:
                break
            yield pos, data
            pos += len(data)
            remaining -= len(data)

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._fh is not None and not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "ImageReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
