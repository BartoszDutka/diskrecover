"""Silnik odzyskiwania plików z poziomami pewności i układem katalogów wyjściowych."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set
import logging
import time

from diskrecover.core.directory import names_match, read_directory
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.mbr import find_fat32_partition, parse_mbr
from diskrecover.core.raw_carver import CarvedFile, carve_unallocated, export_carved
from diskrecover.core.reconstruction import (
    Confidence,
    RecoveredNode,
    ReconstructionResult,
    build_tree_from_directory,
    flatten_nodes,
    reconstruct_target,
)
from diskrecover.core.report import write_reports
from diskrecover.core.telemetry import (
    ScanSnapshot,
    ScanTelemetry,
    create_scanner_logger,
    log_exception,
)


OUTPUT_DIRS = (
    "Odtworzony_bhp_szkoly",
    "Osierocone_katalogi",
    "RAW",
    "Nieustalone_pochodzenie",
    "Raport",
)


def safe_filename(name: str) -> str:
    name = name.strip().rstrip(".")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if not name or name in (".", ".."):
        name = "_unnamed_"
    return name[:200]


@dataclass
class RecoveryItem:
    path: str
    output_path: str
    cluster: int
    size: int
    confidence: str
    category: str  # target | orphan | unknown | raw
    evidence: List[str]
    is_directory: bool = False
    is_deleted: bool = False
    exported: bool = False


@dataclass
class RecoverySession:
    image_path: str
    output_dir: str
    target_name: str
    volume_summary: dict
    items: List[RecoveryItem] = field(default_factory=list)
    carved: List[CarvedFile] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    checkpoint_path: Optional[str] = None


ProgressCb = Optional[Callable[[int, str], None]]
CancelCb = Optional[Callable[[], bool]]
TelemetryCb = Optional[Callable[[ScanSnapshot], None]]


class RecoveryEngine:
    """Orkiestracja: analiza → rekonstrukcja → eksport → RAW → raport."""

    def __init__(
        self,
        image_path: str | Path,
        output_dir: str | Path,
        *,
        target_name: str = "bhp szkoły",
        partition_lba: Optional[int] = None,
        enable_raw: bool = True,
        deep_scan: bool = True,
    ) -> None:
        self.image_path = Path(image_path)
        self.output_dir = Path(output_dir)
        self.target_name = target_name
        self.partition_lba = partition_lba
        self.enable_raw = enable_raw
        self.deep_scan = deep_scan
        self._cancel = False
        self.reader: Optional[ImageReader] = None
        self.volume: Optional[Fat32Volume] = None
        self.session: Optional[RecoverySession] = None
        self.telemetry: Optional[ScanTelemetry] = None
        self.logger: Optional[logging.Logger] = None

    def request_cancel(self) -> None:
        self._cancel = True

    def _cancelled(self) -> bool:
        return self._cancel

    def _prepare_output(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for d in OUTPUT_DIRS:
            (self.output_dir / d).mkdir(parents=True, exist_ok=True)

    def open_volume(self, progress_cb: ProgressCb = None) -> Fat32Volume:
        if progress_cb:
            progress_cb(2, "Otwieram obraz (tylko odczyt)…")
        self.reader = ImageReader(self.image_path)
        offset = 0
        parts = parse_mbr(self.reader)
        preferred = self.partition_lba
        part = find_fat32_partition(self.reader, preferred_lba=preferred)
        if part is not None:
            offset = part.byte_offset
            if progress_cb:
                progress_cb(
                    5,
                    f"Partycja LBA {part.lba_start} ({part.type_name}), offset {offset}",
                )
        else:
            # Brak MBR — spróbuj offset 0 jako surowy FAT32
            if progress_cb:
                progress_cb(5, "Brak MBR — zakładam wolumin FAT32 od początku obrazu")
            offset = 0
            if preferred is not None:
                offset = preferred * 512

        self.volume = Fat32Volume.open(self.reader, offset, validate=True)
        if progress_cb:
            s = self.volume.summary()
            progress_cb(
                8,
                f"BPB OK: cluster={s['cluster_size']} B, root={s['root_cluster']}, "
                f"label={s['volume_label']!r}",
            )
        return self.volume

    def run(
        self,
        progress_cb: ProgressCb = None,
        telemetry_cb: TelemetryCb = None,
    ) -> RecoverySession:
        self._cancel = False
        self._prepare_output()
        self.logger = create_scanner_logger(self.output_dir)
        self.telemetry = ScanTelemetry(
            self.image_path.stat().st_size if self.image_path.is_file() else 0,
            on_update=telemetry_cb,
        )
        self.logger.info("Rozpoczęcie skanowania obrazu: %s", self.image_path)
        last_log_at = 0.0
        last_log_cluster = -1
        seen_entry_offsets: Set[int] = set()
        def telemetry_event(**changes: object) -> None:
            nonlocal last_log_at, last_log_cluster
            entry_offset = changes.pop("entry_offset", None)
            if self.telemetry:
                additive = (
                    "bytes_read",
                    "directory_entries",
                    "records_checked",
                    "records_rejected",
                    "valid_entries",
                    "candidates",
                    "confirmed_directories",
                    "confirmed_files",
                    "directories_found",
                    "files_found",
                    "corruptions",
                )
                increments = {
                    key: int(changes.pop(key))
                    for key in additive
                    if key in changes
                }
                if isinstance(entry_offset, int):
                    if entry_offset in seen_entry_offsets:
                        increments.pop("directory_entries", None)
                        increments.pop("directories_found", None)
                        increments.pop("files_found", None)
                    else:
                        seen_entry_offsets.add(entry_offset)
                for key, amount in increments.items():
                    self.telemetry.increment(key, amount)
                if changes:
                    self.telemetry.update(**changes)
            cluster = changes.get("cluster")
            now = time.monotonic()
            should_log = (
                bool(changes.get("operation"))
                and (
                    now - last_log_at >= 2.0
                    or (
                        isinstance(cluster, int)
                        and abs(cluster - last_log_cluster) >= 4096
                    )
                )
            )
            if self.logger and should_log:
                self.logger.info(
                    "operation=%s cluster=%s lba=%s",
                    changes.get("operation"),
                    cluster,
                    changes.get("lba"),
                )
                last_log_at = now
                if isinstance(cluster, int):
                    last_log_cluster = cluster
        self._telemetry_event = telemetry_event
        if self.telemetry:
            self.telemetry.update(status="skanowanie", stage="Otwarcie i walidacja obrazu")
        vol = self.open_volume(progress_cb)
        vol.activity_cb = telemetry_event
        if self.telemetry:
            self.telemetry.update(
                stage="Odczyt BPB i tablic FAT",
                clusters_total=vol.total_clusters,
                operation="BPB zweryfikowane",
            )
        if progress_cb:
            progress_cb(9, "Ładuję tablicę FAT (jeden odczyt strumieniowy)…")
        last_fat_pct = -1
        def fat_progress(pct: int) -> None:
            nonlocal last_fat_pct
            if pct != last_fat_pct and progress_cb:
                last_fat_pct = pct
                progress_cb(
                    9 + int(pct * 0.01),
                    f"Ładuję tablicę FAT (postęp {pct}%)…",
                )
        vol.preload_fat(
            progress_cb=fat_progress if progress_cb else None,
            cancel_cb=self._cancelled,
        )

        def wrap_progress(pct: int, msg: str) -> None:
            # Skaluj fazę rekonstrukcji do 10–55
            if progress_cb:
                progress_cb(
                    -1 if pct < 0 else 10 + int(pct * 0.45),
                    msg,
                )
            if self.logger:
                self.logger.info("postęp=%s%% %s", pct, msg)
            if self.telemetry:
                stage = "Wyszukiwanie folderu docelowego"
                if "osieroc" in msg.lower():
                    stage = "Skanowanie osieroconych katalogów"
                elif "klastr" in msg.lower():
                    stage = "Głębokie skanowanie klastrów"
                elif "drzew" in msg.lower():
                    stage = "Rekonstrukcja znalezionych plików"
                self.telemetry.update(stage=stage, operation=msg)

        recon = reconstruct_target(
            vol,
            self.target_name,
            progress_cb=wrap_progress,
            cancel_cb=self._cancelled,
            deep_scan=self.deep_scan,
            telemetry_cb=self._telemetry_event,
        )

        items: List[RecoveryItem] = []
        known_offsets: Set[int] = set()

        # Eksport drzewa docelowego
        if progress_cb:
            progress_cb(55, "Rekonstruuję znalezione pliki…")
        if self.telemetry:
            self.telemetry.update(stage="Rekonstrukcja znalezionych plików")
        target_root = self.output_dir / "Odtworzony_bhp_szkoly"
        items.extend(
            self._export_tree(
                vol, recon.nodes, target_root, category="target", known_offsets=known_offsets
            )
        )

        # Osierocone katalogi
        if progress_cb:
            progress_cb(62, "Eksportuję osierocone katalogi…")
        orphan_root = self.output_dir / "Osierocone_katalogi"
        for orphan in recon.orphan_directories:
            items.extend(
                self._export_tree(
                    vol,
                    [orphan],
                    orphan_root,
                    category="orphan",
                    known_offsets=known_offsets,
                    force_unknown_bucket=False,
                )
            )

        # Inne wpisy z roota (nie-cel) → Nieustalone_pochodzenie
        if progress_cb:
            progress_cb(66, "Eksportuję pozostałe wpisy z katalogu głównego…")
        items.extend(
            self._export_other_root_entries(
                vol, recon, known_offsets=known_offsets
            )
        )

        if not recon.target_found:
            recon.messages.append(
                "Katalog docelowy nieznaleziony — nie przypisano plików do Odtworzony_bhp_szkoly."
            )

        carved: List[CarvedFile] = []
        if self.enable_raw:
            if progress_cb:
                progress_cb(70, "Carving RAW (sygnatury)…")

            def raw_prog(pct: int, msg: str) -> None:
                if progress_cb:
                    progress_cb(70 + int(pct * 0.2), msg)

            carved = carve_unallocated(
                vol,
                known_file_offsets=known_offsets,
                progress_cb=raw_prog,
                cancel_cb=self._cancelled,
                telemetry_cb=self._telemetry_event,
            )
            raw_dir = self.output_dir / "RAW"
            for cf in carved:
                if self._cancelled():
                    raise InterruptedError("Anulowano")
                out = export_carved(vol, cf, raw_dir)
                items.append(
                    RecoveryItem(
                        path=cf.output_name,
                        output_path=str(out.relative_to(self.output_dir)),
                        cluster=-1,
                        size=cf.size,
                        confidence=Confidence.LOW.value,
                        category="raw",
                        evidence=cf.evidence,
                        exported=True,
                    )
                )

        # Checkpoint
        ckpt = self.output_dir / "Raport" / "checkpoint.json"
        session = RecoverySession(
            image_path=str(self.image_path),
            output_dir=str(self.output_dir),
            target_name=self.target_name,
            volume_summary=vol.summary(),
            items=items,
            carved=carved,
            messages=recon.messages,
            stats=self._stats(items, recon),
            checkpoint_path=str(ckpt),
        )
        self._save_checkpoint(session, ckpt)

        if progress_cb:
            progress_cb(95, "Generuję raporty…")
        if self.telemetry:
            self.telemetry.update(stage="Generowanie wyników i raportu")
        write_reports(session, self.output_dir / "Raport")

        if progress_cb:
            progress_cb(100, "Gotowe")
        self.session = session
        if self.telemetry:
            self.telemetry.update(status="zakończone", stage="Zakończone", operation="Gotowe")
        if self.logger:
            self.logger.info("Skanowanie zakończone: %s", session.stats)
        return session

    def _export_other_root_entries(
        self,
        vol: Fat32Volume,
        recon: ReconstructionResult,
        *,
        known_offsets: Set[int],
    ) -> List[RecoveryItem]:
        """Katalogi/pliki z roota poza celem — bez twierdzenia o bhp szkoły."""
        unknown_root = self.output_dir / "Nieustalone_pochodzenie"
        items: List[RecoveryItem] = []
        try:
            root_entries = read_directory(vol, vol.bpb.root_cluster, include_deleted=True)
        except Exception:
            return items
        target_cl = recon.target_cluster
        for e in root_entries:
            if e.is_dot or e.is_volume_label:
                continue
            if names_match(e.name, self.target_name):
                continue
            if target_cl is not None and e.cluster == target_cl:
                continue
            if e.cluster < 2:
                continue
            if e.is_directory:
                nodes = build_tree_from_directory(
                    vol, e.cluster, e.name, belongs_to_target=False
                )
                wrapper = RecoveredNode(
                    name=e.name,
                    path=e.name,
                    cluster=e.cluster,
                    size=0,
                    is_directory=True,
                    is_deleted=e.is_deleted,
                    confidence=Confidence.MEDIUM if e.is_deleted else Confidence.HIGH,
                    evidence=[
                        "wpis w katalogu głównym",
                        "brak dowodu przynależności do katalogu docelowego",
                    ],
                    belongs_to_target=False,
                    children=nodes,
                )
                items.extend(
                    self._export_tree(
                        vol,
                        [wrapper],
                        unknown_root,
                        category="unknown",
                        known_offsets=known_offsets,
                        force_unknown_bucket=True,
                    )
                )
            else:
                node = RecoveredNode(
                    name=e.name,
                    path=e.name,
                    cluster=e.cluster,
                    size=e.size,
                    is_directory=False,
                    is_deleted=e.is_deleted,
                    confidence=Confidence.MEDIUM if e.is_deleted else Confidence.HIGH,
                    evidence=[
                        "wpis w katalogu głównym",
                        "brak dowodu przynależności do katalogu docelowego",
                    ],
                    belongs_to_target=False,
                )
                items.extend(
                    self._export_tree(
                        vol,
                        [node],
                        unknown_root,
                        category="unknown",
                        known_offsets=known_offsets,
                        force_unknown_bucket=True,
                    )
                )
        return items

    def _export_tree(
        self,
        vol: Fat32Volume,
        nodes: List[RecoveredNode],
        dest_root: Path,
        *,
        category: str,
        known_offsets: Set[int],
        force_unknown_bucket: bool = False,
    ) -> List[RecoveryItem]:
        items: List[RecoveryItem] = []

        unknown_base = self.output_dir / "Nieustalone_pochodzenie"

        def export_node(node: RecoveredNode, parent_dir: Path) -> None:
            if self._cancelled():
                raise InterruptedError("Anulowano")
            name = safe_filename(node.name)
            # Przynależność: tylko przy dowodzie; zachowaj strukturę pod parent_dir
            already_under_unknown = unknown_base in parent_dir.parents or parent_dir == unknown_base
            if (
                (force_unknown_bucket and not already_under_unknown)
                or (category == "target" and not node.belongs_to_target)
            ):
                dest = unknown_base / name
                cat = "unknown"
            elif not node.belongs_to_target and category == "orphan":
                dest = parent_dir / name
                cat = "orphan"
            elif force_unknown_bucket or category == "unknown":
                dest = parent_dir / name
                cat = "unknown"
            else:
                dest = parent_dir / name
                cat = category

            if node.is_directory:
                dest.mkdir(parents=True, exist_ok=True)
                rel = str(dest.relative_to(self.output_dir))
                items.append(
                    RecoveryItem(
                        path=node.path,
                        output_path=rel,
                        cluster=node.cluster,
                        size=0,
                        confidence=node.confidence.value,
                        category=cat,
                        evidence=node.evidence,
                        is_directory=True,
                        is_deleted=node.is_deleted,
                        exported=True,
                    )
                )
                for ch in node.children:
                    export_node(ch, dest)
                return

            # Plik
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stem, suf = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = dest.parent / f"{stem}_{n}{suf}"
                    n += 1

            written = self._write_file_from_clusters(vol, node, dest)
            if node.cluster >= 2:
                known_offsets.add(vol.cluster_offset(node.cluster))
            rel = str(dest.relative_to(self.output_dir))
            items.append(
                RecoveryItem(
                    path=node.path,
                    output_path=rel,
                    cluster=node.cluster,
                    size=written,
                    confidence=node.confidence.value,
                    category=cat,
                    evidence=node.evidence,
                    is_directory=False,
                    is_deleted=node.is_deleted,
                    exported=written > 0 or node.size == 0,
                )
            )

        for n in nodes:
            export_node(n, dest_root)
        return items

    def _write_file_from_clusters(
        self, vol: Fat32Volume, node: RecoveredNode, dest: Path
    ) -> int:
        if node.cluster < 2:
            dest.write_bytes(b"")
            return 0
        chain = vol.follow_chain(node.cluster)
        if not chain:
            chain = [node.cluster]
        remaining = node.size if node.size > 0 else len(chain) * vol.cluster_size
        written = 0
        with open(dest, "wb") as f:
            for c in chain:
                if remaining <= 0:
                    break
                data = vol.read_cluster(c)
                take = min(len(data), remaining)
                f.write(data[:take])
                written += take
                remaining -= take
        return written

    def _stats(self, items: List[RecoveryItem], recon: ReconstructionResult) -> Dict[str, int]:
        files = [i for i in items if not i.is_directory]
        return {
            "plikow_lacznie": len(files),
            "katalogow": sum(1 for i in items if i.is_directory),
            "w_docelowym": sum(1 for i in files if i.category == "target"),
            "osierocone": sum(1 for i in files if i.category == "orphan"),
            "nieustalone": sum(1 for i in files if i.category == "unknown"),
            "raw": sum(1 for i in files if i.category == "raw"),
            "wysokie": sum(1 for i in files if i.confidence == Confidence.HIGH.value),
            "srednie": sum(1 for i in files if i.confidence == Confidence.MEDIUM.value),
            "niskie": sum(1 for i in files if i.confidence == Confidence.LOW.value),
            "target_znaleziony": 1 if recon.target_found else 0,
        }

    def _save_checkpoint(self, session: RecoverySession, path: Path) -> None:
        data = {
            "image_path": session.image_path,
            "output_dir": session.output_dir,
            "target_name": session.target_name,
            "volume_summary": session.volume_summary,
            "messages": session.messages,
            "stats": session.stats,
            "items": [
                {
                    "path": i.path,
                    "output_path": i.output_path,
                    "cluster": i.cluster,
                    "size": i.size,
                    "confidence": i.confidence,
                    "category": i.category,
                    "evidence": i.evidence,
                    "is_directory": i.is_directory,
                    "is_deleted": i.is_deleted,
                    "exported": i.exported,
                }
                for i in session.items
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        if self.reader:
            self.reader.close()
            self.reader = None
