"""Nondestructive export of validated files."""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from pathlib import Path

from diskrecover.core.validation import IntegrityStatus, ValidationResult, hash_file


@dataclass(frozen=True)
class ExportRecord:
    source: Path
    destination: Path | None
    status: str
    reason: str


def _unique_destination(path: Path, source: Path) -> Path:
    if not path.exists():
        return path
    if path.is_file() and hash_file(path) == hash_file(source):
        return path
    for index in range(1, 100000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
        if candidate.is_file() and hash_file(candidate) == hash_file(source):
            return candidate
    raise OSError(f"Nie można utworzyć bezpiecznej nazwy dla: {path.name}")


def export_files(
    results: list[ValidationResult],
    destination: str | Path,
    *,
    minimum: IntegrityStatus = IntegrityStatus.VALID_STRUCTURE,
    include_unverified: bool = False,
    flatten: bool = False,
    skip_duplicates: bool = True,
    source_root: str | Path | None = None,
) -> list[ExportRecord]:
    target_root = Path(destination)
    target_root.mkdir(parents=True, exist_ok=True)
    root = Path(source_root).resolve() if source_root else None
    exported: list[ExportRecord] = []
    allowed = {IntegrityStatus.VALID_STRUCTURE}
    if include_unverified:
        allowed.add(IntegrityStatus.UNVERIFIED)
    rank = {
        IntegrityStatus.CORRUPTED: 0,
        IntegrityStatus.PARTIALLY_RECOVERED: 1,
        IntegrityStatus.UNVERIFIED: 2,
        IntegrityStatus.VALID_STRUCTURE: 3,
    }
    for result in results:
        if skip_duplicates and result.status == IntegrityStatus.DUPLICATE:
            exported.append(ExportRecord(result.path, None, "SKIPPED_DUPLICATE", "Identyczna kopia."))
            continue
        if result.integrity not in allowed or rank.get(result.integrity, 0) < rank[minimum]:
            exported.append(ExportRecord(result.path, None, "SKIPPED", result.reason))
            continue
        relative = Path(result.path.name)
        if not flatten and root is not None:
            try:
                relative = result.path.resolve().relative_to(root)
            except ValueError:
                pass
        destination_path = _unique_destination(target_root / relative, result.path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(result.path, destination_path)
        exported.append(ExportRecord(result.path, destination_path, "EXPORTED", result.reason))
    _write_mapping(exported, target_root / "export-map.csv")
    return exported


def _write_mapping(records: list[ExportRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["source", "destination", "status", "reason"])
        writer.writerows(
            (str(record.source), str(record.destination or ""), record.status, record.reason)
            for record in records
        )
