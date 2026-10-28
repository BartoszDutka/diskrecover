"""Safe, dependency-light validation of recovered files.

Validation never executes document content.  It only inspects headers and
container metadata and reads files incrementally.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable


class IntegrityStatus(str, Enum):
    VALID_STRUCTURE = "VALID_STRUCTURE"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    CORRUPTED = "CORRUPTED"
    UNVERIFIED = "UNVERIFIED"
    ENCRYPTED = "ENCRYPTED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class ValidationResult:
    path: Path
    size: int
    sha256: str
    extension: str
    detected_format: str
    integrity: IntegrityStatus
    reason: str
    encrypted: bool = False
    duplicate_of: Path | None = None

    @property
    def status(self) -> IntegrityStatus:
        """Return the primary user-facing status."""
        if self.duplicate_of is not None:
            return IntegrityStatus.DUPLICATE
        return self.integrity


def hash_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _format_for(path: Path, head: bytes) -> str:
    suffix = path.suffix.lower()
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "OLE/CFB"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return {
            ".docx": "DOCX",
            ".xlsx": "XLSX",
            ".pptx": "PPTX",
            ".odt": "ODT",
            ".ods": "ODS",
            ".rtf": "ZIP",
        }.get(suffix, "ZIP")
    if head.startswith(b"{\\rtf"):
        return "RTF"
    return {
        ".doc": "DOC",
        ".xls": "XLS",
        ".ppt": "PPT",
    }.get(suffix, "UNKNOWN")


def _validate_structure(path: Path, detected: str, head: bytes) -> tuple[IntegrityStatus, str, bool]:
    if detected == "PDF":
        data_tail = b""
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4096))
            data_tail = stream.read(4096)
        if b"%%EOF" not in data_tail:
            return IntegrityStatus.PARTIALLY_RECOVERED, "Brak znacznika %%EOF.", False
        if b"/Encrypt" in head or b"/Encrypt" in data_tail:
            return IntegrityStatus.ENCRYPTED, "PDF zawiera znacznik szyfrowania.", True
        return IntegrityStatus.VALID_STRUCTURE, "Nagłówek PDF i końcowy znacznik są obecne.", False
    if detected in {"ZIP", "DOCX", "XLSX", "PPTX", "ODT", "ODS"}:
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
                if bad:
                    return IntegrityStatus.CORRUPTED, f"Błędna suma CRC w {bad}.", False
                encrypted = any(info.flag_bits & 0x1 for info in archive.infolist())
                if encrypted:
                    return IntegrityStatus.ENCRYPTED, "Archiwum ZIP jest zaszyfrowane.", True
            return IntegrityStatus.VALID_STRUCTURE, "Kontener ZIP i sumy CRC są poprawne.", False
        except (OSError, zipfile.BadZipFile) as exc:
            return IntegrityStatus.CORRUPTED, f"Niepoprawny kontener ZIP: {exc}.", False
    if detected == "OLE/CFB":
        if len(head) >= 512:
            return IntegrityStatus.VALID_STRUCTURE, "Nagłówek OLE/CFB jest obecny.", False
        return IntegrityStatus.PARTIALLY_RECOVERED, "Niepełny nagłówek OLE/CFB.", False
    if detected == "RTF":
        return IntegrityStatus.VALID_STRUCTURE, "Rozpoznano nagłówek RTF.", False
    return IntegrityStatus.UNVERIFIED, "Brak bezpiecznego walidatora dla tego formatu.", False


def validate_file(path: str | Path) -> ValidationResult:
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Nie znaleziono pliku: {file_path}")
    with file_path.open("rb") as stream:
        head = stream.read(512)
    detected = _format_for(file_path, head)
    integrity, reason, encrypted = _validate_structure(file_path, detected, head)
    return ValidationResult(
        path=file_path,
        size=file_path.stat().st_size,
        sha256=hash_file(file_path),
        extension=file_path.suffix.lower(),
        detected_format=detected,
        integrity=integrity,
        reason=reason,
        encrypted=encrypted,
    )


def iter_files(source: str | Path) -> Iterable[Path]:
    root = Path(source)
    if root.is_file():
        yield root
    elif root.is_dir():
        yield from (path for path in root.rglob("*") if path.is_file())
    else:
        raise FileNotFoundError(f"Nie znaleziono źródła: {root}")


def validate_tree(source: str | Path) -> list[ValidationResult]:
    results = [validate_file(path) for path in iter_files(source)]
    groups: dict[tuple[int, str], list[ValidationResult]] = {}
    for result in results:
        groups.setdefault((result.size, result.sha256), []).append(result)
    marked: list[ValidationResult] = []
    for result in results:
        group = groups[(result.size, result.sha256)]
        original = min(group, key=lambda item: str(item.path).casefold())
        marked.append(
            ValidationResult(
                **{
                    **result.__dict__,
                    "duplicate_of": original.path if result.path != original.path else None,
                }
            )
        )
    return marked
