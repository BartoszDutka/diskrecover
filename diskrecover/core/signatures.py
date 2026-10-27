"""Sygnatury plików do odzyskiwania RAW (carving)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class FileSignature:
    name: str
    extension: str
    header: bytes
    header_offset: int = 0
    footer: Optional[bytes] = None
    max_size: int = 50 * 1024 * 1024
    # Opcjonalna walidacja po nagłówku
    validate: Optional[Callable[[bytes], bool]] = None
    category: str = "inne"


def _is_zip_local(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def _office_openxml_hint(data: bytes) -> Optional[str]:
    """Rozróżnij docx/xlsx/pptx po zawartości ZIP (best-effort)."""
    sample = data[:8192]
    if b"word/" in sample:
        return "docx"
    if b"xl/" in sample:
        return "xlsx"
    if b"ppt/" in sample:
        return "pptx"
    return None


SIGNATURES: List[FileSignature] = [
    FileSignature("PDF", "pdf", b"%PDF-", footer=b"%%EOF", max_size=80 * 1024 * 1024, category="dokument"),
    FileSignature("JPEG", "jpg", b"\xff\xd8\xff", footer=b"\xff\xd9", max_size=40 * 1024 * 1024, category="obraz"),
    FileSignature("PNG", "png", b"\x89PNG\r\n\x1a\n", footer=b"IEND", max_size=40 * 1024 * 1024, category="obraz"),
    FileSignature("GIF87a", "gif", b"GIF87a", footer=b"\x00\x3b", max_size=20 * 1024 * 1024, category="obraz"),
    FileSignature("GIF89a", "gif", b"GIF89a", footer=b"\x00\x3b", max_size=20 * 1024 * 1024, category="obraz"),
    FileSignature("BMP", "bmp", b"BM", max_size=40 * 1024 * 1024, category="obraz"),
    FileSignature("ZIP/OOXML", "zip", b"PK\x03\x04", max_size=100 * 1024 * 1024, category="archiwum"),
    FileSignature("RAR", "rar", b"Rar!\x1a\x07", max_size=100 * 1024 * 1024, category="archiwum"),
    FileSignature("7z", "7z", b"\x37\x7a\xbc\xaf\x27\x1c", max_size=100 * 1024 * 1024, category="archiwum"),
    FileSignature("MP4/ISO", "mp4", b"ftyp", header_offset=4, max_size=500 * 1024 * 1024, category="media"),
    FileSignature("AVI", "avi", b"RIFF", max_size=500 * 1024 * 1024, category="media"),
    FileSignature("WAV", "wav", b"RIFF", max_size=100 * 1024 * 1024, category="media"),
    FileSignature("MP3_ID3", "mp3", b"ID3", max_size=50 * 1024 * 1024, category="media"),
    FileSignature("MP3_FF", "mp3", b"\xff\xfb", max_size=50 * 1024 * 1024, category="media"),
    FileSignature("DOC_OLE", "doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", max_size=50 * 1024 * 1024, category="dokument"),
]


def refine_zip_extension(data: bytes, default: str = "zip") -> str:
    hint = _office_openxml_hint(data)
    return hint if hint else default


def find_signatures_in_chunk(
    chunk: bytes,
    absolute_offset: int,
    signatures: Optional[List[FileSignature]] = None,
) -> List[tuple]:
    """Zwraca listę (abs_offset, signature)."""
    sigs = signatures or SIGNATURES
    hits = []
    for sig in sigs:
        start = 0
        hdr = sig.header
        while True:
            idx = chunk.find(hdr, start)
            if idx < 0:
                break
            # Uwzględnij header_offset (np. ftyp w MP4)
            hit_at = absolute_offset + idx - sig.header_offset
            if hit_at >= 0 and idx >= sig.header_offset:
                # Dla RIFF sprawdź podtyp
                if sig.name == "AVI":
                    sub = chunk[idx : idx + 12]
                    if len(sub) >= 12 and sub[8:12] != b"AVI ":
                        start = idx + 1
                        continue
                if sig.name == "WAV":
                    sub = chunk[idx : idx + 12]
                    if len(sub) >= 12 and sub[8:12] != b"WAVE":
                        start = idx + 1
                        continue
                hits.append((hit_at, sig))
            start = idx + 1
    hits.sort(key=lambda x: x[0])
    return hits
