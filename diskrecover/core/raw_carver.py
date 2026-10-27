"""Odzyskiwanie RAW po sygnaturach — bez przypisywania do katalogu docelowego."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set

from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.signatures import (
    SIGNATURES,
    FileSignature,
    find_signatures_in_chunk,
    refine_zip_extension,
)


@dataclass
class CarvedFile:
    offset: int
    size: int
    signature_name: str
    extension: str
    category: str
    output_name: str
    evidence: List[str] = field(default_factory=list)
    # Zawsze False — RAW nigdy nie należy do bhp szkoły bez dowodu
    belongs_to_target: bool = False


ProgressCb = Optional[Callable[[int, str], None]]
CancelCb = Optional[Callable[[], bool]]


def _find_footer(data: bytes, footer: bytes, start: int = 0) -> int:
    idx = data.find(footer, start)
    if idx < 0:
        return -1
    return idx + len(footer)


def carve_region(
    vol: Fat32Volume,
    start_offset: int,
    length: int,
    *,
    skip_offsets: Optional[Set[int]] = None,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    chunk_size: int = 1024 * 1024,
    telemetry_cb: Optional[Callable[..., None]] = None,
) -> List[CarvedFile]:
    """Skanuj zakres bajtów (w przestrzeni obrazu) pod kątem sygnatur."""
    skip = skip_offsets or set()
    results: List[CarvedFile] = []
    overlap = 64  # zachowaj overlap żeby nie pominąć sygnatury na granicy
    prev_tail = b""
    scanned = 0

    for pos, chunk in vol.reader.iter_chunks(start_offset, length, chunk_size):
        if cancel_cb and cancel_cb():
            raise InterruptedError("Anulowano carving RAW")
        buf = prev_tail + chunk
        abs_base = pos - len(prev_tail)
        hits = find_signatures_in_chunk(buf, abs_base)
        for hit_off, sig in hits:
            # Pomiń jeśli już pokryte znanym plikiem FAT (w przybliżeniu)
            aligned = False
            for s in skip:
                if abs(hit_off - s) < vol.cluster_size:
                    aligned = True
                    break
            if aligned:
                continue
            # Odczytaj potencjalny plik
            max_read = min(sig.max_size, vol.reader.size - hit_off)
            if max_read < len(sig.header) + sig.header_offset:
                continue
            head = vol.reader.read(hit_off, min(max_read, 64 * 1024))
            ext = sig.extension
            if sig.name.startswith("ZIP"):
                ext = refine_zip_extension(head, "zip")
            size = max_read
            if sig.footer:
                # Szukaj stopki w oknie
                window = vol.reader.read(hit_off, min(max_read, sig.max_size))
                # Dla PNG stopka to IEND + CRC (8 bajtów po IEND)
                if sig.extension == "png":
                    idx = window.find(b"IEND")
                    if idx >= 0:
                        size = idx + 8
                    else:
                        size = min(len(window), 2 * vol.cluster_size)
                else:
                    end = _find_footer(window, sig.footer)
                    if end > 0:
                        size = end
                    else:
                        size = min(len(window), 8 * vol.cluster_size)
            else:
                # Bez stopki — ogranicz do rozsądnego okna / klastrów
                if sig.extension == "bmp" and len(head) >= 10:
                    size = int.from_bytes(head[2:6], "little")
                    size = min(size, max_read) if size > 0 else min(max_read, 4 * vol.cluster_size)
                elif sig.name.startswith("ZIP"):
                    size = _estimate_zip_size(vol, hit_off, max_read)
                else:
                    size = min(max_read, 4 * vol.cluster_size)

            if size < 16:
                continue
            # Deduplikacja po offsecie
            if any(abs(r.offset - hit_off) < 8 for r in results):
                continue
            idx = len(results) + 1
            out_name = f"raw_{idx:05d}_{sig.name.replace('/', '_')}.{ext}"
            results.append(
                CarvedFile(
                    offset=hit_off,
                    size=size,
                    signature_name=sig.name,
                    extension=ext,
                    category=sig.category,
                    output_name=out_name,
                    evidence=[
                        f"sygnatura {sig.name} @ offset {hit_off}",
                        "brak dowodu przynależności do katalogu docelowego",
                    ],
                    belongs_to_target=False,
                )
            )
        prev_tail = chunk[-overlap:] if len(chunk) >= overlap else chunk
        scanned += len(chunk)
        if telemetry_cb:
            telemetry_cb(
                bytes_read=len(chunk),
                operation=f"Wyszukiwanie RAW: offset {pos}",
            )
        if progress_cb:
            pct = int(100 * scanned / max(length, 1))
            progress_cb(min(99, pct), f"Carving RAW… {len(results)} plików")

    if progress_cb:
        progress_cb(100, f"Carving zakończony: {len(results)} plików")
    return results


def _estimate_zip_size(vol: Fat32Volume, offset: int, max_read: int) -> int:
    """Best-effort: znajdź EOCD (PK\\x05\\x06) w oknie."""
    window_size = min(max_read, 16 * 1024 * 1024)
    data = vol.reader.read(offset, window_size)
    idx = data.rfind(b"PK\x05\x06")
    if idx >= 0 and idx + 22 <= len(data):
        comment_len = int.from_bytes(data[idx + 20 : idx + 22], "little")
        return idx + 22 + comment_len
    return min(len(data), 4 * vol.cluster_size)


def carve_unallocated(
    vol: Fat32Volume,
    *,
    known_file_offsets: Optional[Set[int]] = None,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    telemetry_cb: Optional[Callable[..., None]] = None,
) -> List[CarvedFile]:
    """Carving obszaru danych — użytkownik może ograniczyć do niezaalokowanych."""
    data_len = vol.bpb.total_sectors * vol.bpb.bytes_per_sector - (
        vol.data_offset - vol.partition_offset
    )
    if data_len < 0:
        data_len = vol.reader.size - vol.data_offset
    return carve_region(
        vol,
        vol.data_offset,
        min(data_len, vol.reader.size - vol.data_offset),
        skip_offsets=known_file_offsets,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        telemetry_cb=telemetry_cb,
    )


def export_carved(
    vol: Fat32Volume,
    carved: CarvedFile,
    dest_dir: Path,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / carved.output_name
    # Unikalna nazwa
    if out.exists():
        stem, suf = out.stem, out.suffix
        n = 1
        while out.exists():
            out = dest_dir / f"{stem}_{n}{suf}"
            n += 1
    data = vol.reader.read(carved.offset, carved.size)
    with open(out, "wb") as f:
        f.write(data)
    return out
