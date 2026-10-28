"""Silnik odzyskiwania plików z poziomami pewności, checkpointami i pauzą."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from diskrecover.core.checkpoint import (
    CheckpointMeta,
    CheckpointStore,
    atomic_checkpoint_write,
    default_checkpoint_dir,
    fingerprint_image,
    list_checkpoints,
    ResultStore,
)
from diskrecover.core.cluster_tracker import ClusterTracker
from diskrecover.core.directory import names_match, read_directory
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.mbr import find_fat32_partition, parse_mbr
from diskrecover.core.profiler import Profiler
from diskrecover.core.raw_carver import CarvedFile, carve_unallocated, export_carved
from diskrecover.core.reconstruction import (
    Confidence,
    RecoveredNode,
    ReconstructionResult,
    build_tree_from_directory,
    flatten_nodes,
    reconstruct_target,
    CandidateBudget,
    diagnose_orphan_candidate,
)
from diskrecover.core.report import write_reports
from diskrecover.core.telemetry import (
    ADDITIVE_FIELDS,
    ScanSnapshot,
    ScanTelemetry,
    create_scanner_logger,
)


OUTPUT_DIRS = (
    "Osierocone_katalogi",
    "RAW",
    "Nieustalone_pochodzenie",
    "Raport",
)


class RecoveryMode(str, Enum):
    TARGET_ONLY = "TARGET_ONLY"
    FULL_RECOVERY = "FULL_RECOVERY"


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
    profile_report: str = ""


def _node_to_dict(node: RecoveredNode) -> dict:
    return {
        "name": node.name,
        "path": node.path,
        "cluster": node.cluster,
        "size": node.size,
        "is_directory": node.is_directory,
        "is_deleted": node.is_deleted,
        "confidence": node.confidence.value,
        "evidence": list(node.evidence),
        "belongs_to_target": node.belongs_to_target,
        "is_candidate": node.is_candidate,
        "parent_cluster": node.parent_cluster,
        "link_confidence": node.link_confidence.value if node.link_confidence else None,
        "children": [_node_to_dict(child) for child in node.children],
    }


def _node_from_dict(raw: dict) -> RecoveredNode:
    link_confidence = raw.get("link_confidence")
    return RecoveredNode(
        name=str(raw.get("name") or ""),
        path=str(raw.get("path") or ""),
        cluster=int(raw.get("cluster") or -1),
        size=int(raw.get("size") or 0),
        is_directory=bool(raw.get("is_directory")),
        is_deleted=bool(raw.get("is_deleted")),
        confidence=Confidence(raw.get("confidence") or Confidence.UNKNOWN.value),
        evidence=[str(value) for value in raw.get("evidence") or []],
        belongs_to_target=bool(raw.get("belongs_to_target")),
        is_candidate=bool(raw.get("is_candidate")),
        parent_cluster=raw.get("parent_cluster"),
        link_confidence=Confidence(link_confidence) if link_confidence else None,
        children=[_node_from_dict(child) for child in raw.get("children") or []],
    )


def _partial_items(nodes: List[RecoveredNode], category: str) -> List[RecoveryItem]:
    items: List[RecoveryItem] = []

    def visit(node: RecoveredNode, parent: str) -> None:
        safe_path = str(Path(parent) / safe_filename(node.name))
        item_category = category if node.belongs_to_target or category != "target" else "unknown"
        items.append(
            RecoveryItem(
                path=node.path,
                output_path=safe_path,
                cluster=node.cluster,
                size=node.size,
                confidence=node.confidence.value,
                category=item_category,
                evidence=list(node.evidence),
                is_directory=node.is_directory,
                is_deleted=node.is_deleted,
                exported=False,
            )
        )
        for child in node.children:
            visit(child, safe_path)

    for node in nodes:
        visit(node, category)
    return items


ProgressCb = Optional[Callable[[int, str], None]]
TelemetryCb = Optional[Callable[[ScanSnapshot], None]]
CheckpointCb = Optional[Callable[[str, float], None]]


class RecoveryEngine:
    """Orkiestracja: analiza → rekonstrukcja → eksport → RAW → raport."""

    def __init__(
        self,
        image_path: str | Path,
        output_dir: str | Path,
        *,
        target_name: str = "",
        partition_lba: Optional[int] = None,
        enable_raw: bool = True,
        deep_scan: bool = True,
        checkpoint_interval_sec: float = 300.0,
        checkpoint_every_candidates: int = 500,
        resume_checkpoint: Optional[str | Path] = None,
        candidate_max_seconds: float = 60.0,
        candidate_max_reads: int = 50_000,
        candidate_max_clusters: int = 50_000,
        candidate_max_records: int = 250_000,
        retry_deferred: bool = False,
        mode: RecoveryMode | str = RecoveryMode.FULL_RECOVERY,
        target_cluster: Optional[int] = None,
    ) -> None:
        self.image_path = Path(image_path)
        self.output_dir = Path(output_dir)
        self.target_name = target_name
        self.partition_lba = partition_lba
        self.enable_raw = enable_raw
        self.deep_scan = deep_scan
        self.checkpoint_interval_sec = checkpoint_interval_sec
        self.checkpoint_every_candidates = checkpoint_every_candidates
        self.resume_checkpoint = Path(resume_checkpoint) if resume_checkpoint else None
        self.candidate_max_seconds = candidate_max_seconds
        self.candidate_max_reads = candidate_max_reads
        self.candidate_max_clusters = candidate_max_clusters
        self.candidate_max_records = candidate_max_records
        self.retry_deferred = retry_deferred
        self.target_cluster = target_cluster
        try:
            self.mode = RecoveryMode(mode)
        except ValueError as exc:
            raise ValueError(
                f"Nieznany tryb odzyskiwania: {mode!r}; "
                f"oczekiwano TARGET_ONLY lub FULL_RECOVERY"
            ) from exc

        self._cancel = False
        self._pause = threading.Event()
        self._pause.set()  # set = running; clear = paused
        self._checkpoint_request = threading.Event()
        self._last_checkpoint_at = 0.0
        self._candidates_since_ckpt = 0
        self._ckpt_lock = threading.Lock()

        self.reader: Optional[ImageReader] = None
        self.volume: Optional[Fat32Volume] = None
        self.session: Optional[RecoverySession] = None
        self.telemetry: Optional[ScanTelemetry] = None
        self.logger: Optional[logging.Logger] = None
        self.tracker = ClusterTracker()
        self.profiler = Profiler(enabled=True)
        self._scan_state: Dict = {
            "stage": "init",
            "stage_index": 0,
            "recovery_mode": self.mode.value,
            "completed_stages": [],
            "orphan_candidates": [],
            "already_checked": [],
            "known_clusters": [],
            "target_cluster": target_cluster,
        }
        self._checkpoint_cb: CheckpointCb = None
        self._latest_recon: Optional[ReconstructionResult] = None
        self._ckpt_store: Optional[CheckpointStore] = None
        self._result_store: Optional[ResultStore] = None
        self._result_batch: List[dict] = []
        self._result_batch_size = 32

    def results_path(self) -> Path:
        return self.output_dir / "Raport" / "results.sqlite"

    def diagnose_candidate(
        self,
        cluster: int,
        *,
        target_cluster: Optional[int] = None,
        budget: Optional[CandidateBudget] = None,
    ) -> dict:
        """Diagnose one candidate after opening the image, without a full scan."""
        self._prepare_output()
        self.open_volume()
        assert self.volume is not None
        self.volume.preload_fat()
        return diagnose_orphan_candidate(
            self.volume,
            cluster,
            target_cluster=target_cluster,
            target_name=self.target_name,
            budget=budget,
        )

    def _ensure_result_store(self) -> ResultStore:
        if self._result_store is None:
            self._result_store = ResultStore(self.results_path())
            self._result_store.open()
        return self._result_store

    def _persist_node(self, node: RecoveredNode) -> None:
        entry = node.source_entry
        object_id = (
            f"entry:{entry.raw_offset}"
            if entry is not None
            else f"{node.path}|{node.cluster}|{int(node.is_directory)}"
        )
        self._result_batch.append({
            "object_id": object_id,
            "name": node.name,
            "path": node.path,
            "size": node.size,
            "object_type": "directory" if node.is_directory else "file",
            "cluster": node.cluster,
            "entry_offset": entry.raw_offset if entry is not None else None,
            "parent_cluster": node.parent_cluster,
            "category": "target" if node.belongs_to_target else (
                "orphan" if node.is_candidate else "unknown"
            ),
            "confidence": node.confidence.value,
            "evidence": node.evidence,
            "integrity": "niezweryfikowany",
        })
        if len(self._result_batch) >= self._result_batch_size:
            self._flush_result_batch()

    def _flush_result_batch(self) -> None:
        if self._result_batch:
            self._ensure_result_store().upsert_objects(self._result_batch)
            self._result_batch.clear()

    def _persist_candidate(self, candidate_id: str, cluster: int,
                           status: str, data: dict) -> None:
        self._ensure_result_store().upsert_candidate(
            candidate_id, cluster, int(data.get("sequence_no", 0)), status,
            safe_stage=str(data.get("safe_stage") or ""),
            reads=int(data.get("reads") or 0),
            clusters=int(data.get("clusters") or 0),
            records=int(data.get("records") or 0),
            progress=float(data.get("progress") or 0),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            reason=str(data.get("reason") or ""),
            state=data.get("state") or {},
        )

    @staticmethod
    def load_live_results(path: str | Path) -> RecoverySession:
        store = ResultStore(path)
        store.open()
        try:
            objects = store.list_objects()
        finally:
            store.close()
        items = [
            RecoveryItem(
                path=o["path"], output_path=o["path"], cluster=o["cluster"],
                size=o["size"], confidence=o["confidence"], category=o["category"],
                evidence=o["evidence"], is_directory=o["object_type"] == "directory",
                exported=o["exported"],
            )
            for o in objects
        ]
        files = [item for item in items if not item.is_directory]
        stats = {
            "zapisane_obiekty": len(items),
            "plikow_lacznie": len(files),
            "katalogow": len(items) - len(files),
            "w_docelowym": sum(i.category == "target" for i in files),
            "osierocone": sum(i.category == "orphan" for i in files),
            "nieustalone": sum(i.category == "unknown" for i in files),
            "raw": sum(i.category == "raw" for i in files),
            "wysokie": sum(i.confidence == Confidence.HIGH.value for i in files),
            "srednie": sum(i.confidence == Confidence.MEDIUM.value for i in files),
            "niskie": sum(i.confidence == Confidence.LOW.value for i in files),
            "target_subdirectories": sum(
                i.is_directory and i.category == "target" for i in items
            ),
            "target_files_in_subdirectories": sum(
                i.category == "target" for i in files
            ),
        }
        return RecoverySession(
            image_path="", output_dir=str(Path(path).parents[1]),
            target_name="", volume_summary={},
            items=items, stats=stats,
            checkpoint_path=str(path),
        )

    def request_cancel(self) -> None:
        self._cancel = True
        self._pause.set()  # odblokuj pauzę, by wyjść

    def request_pause(self) -> None:
        self._pause.clear()
        if self.telemetry:
            self.telemetry.update(status="wstrzymane", operation="Skanowanie wstrzymane")

    def request_resume(self) -> None:
        self._pause.set()
        if self.telemetry:
            self.telemetry.update(status="skanowanie", operation="Wznowiono skanowanie")

    def request_checkpoint(self) -> None:
        self._checkpoint_request.set()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def _cancelled(self) -> bool:
        return self._cancel

    def _wait_if_paused(self) -> None:
        while not self._pause.is_set():
            if self._cancel:
                raise InterruptedError("Anulowano skanowanie")
            time.sleep(0.05)

    def _prepare_output(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for d in OUTPUT_DIRS:
            (self.output_dir / d).mkdir(parents=True, exist_ok=True)
        self._target_root().mkdir(parents=True, exist_ok=True)
        default_checkpoint_dir(self.output_dir).mkdir(parents=True, exist_ok=True)

    def _target_root(self) -> Path:
        if not self.target_name:
            return self.output_dir / "Recovered"
        normalized = unicodedata.normalize("NFKD", self.target_name)
        normalized = normalized.translate(str.maketrans({"ł": "l", "Ł": "L"}))
        slug = normalized.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug).strip("._-") or "target"
        return self.output_dir / f"Odtworzony_{slug[:160]}"

    def _infer_target_name(self, vol: Fat32Volume) -> str:
        """Choose a likely target directory when the user left it unspecified."""
        try:
            entries = read_directory(
                vol, vol.bpb.root_cluster, include_deleted=False, tracker=self.tracker
            )
        except (OSError, ValueError, RuntimeError):
            return ""
        candidates = [
            entry.name
            for entry in entries
            if entry.is_directory and not entry.is_dot and not entry.is_volume_label
        ]
        return candidates[0] if candidates else ""

    def _orphan_root(self) -> Path:
        return self.output_dir / "Osierocone_katalogi"

    def _unknown_root(self) -> Path:
        return self.output_dir / "Nieustalone_pochodzenie"

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

    def _checkpoint_path(self) -> Path:
        return default_checkpoint_dir(self.output_dir) / "scan_state.sqlite"

    def save_checkpoint(self, *, reason: str = "manual") -> Path:
        """Zapisuje pełny stan skanowania (atomowo). Nie zatrzymuje skanowania."""
        with self._ckpt_lock:
            path = self._checkpoint_path()
            vol_summary = self.volume.summary() if self.volume else {}
            fp = fingerprint_image(self.image_path)
            telem = self.telemetry.export_counters() if self.telemetry else {}
            state = dict(self._scan_state)
            state["tracker"] = self.tracker.export_state()
            state["reason"] = reason
            state["telemetry"] = telem
            state["profiler"] = self.profiler.as_dict()

            def writer(store: CheckpointStore) -> None:
                meta = CheckpointMeta(
                    stage=state.get("stage", ""),
                    stage_index=int(state.get("stage_index") or 0),
                    status="paused" if self.is_paused() else ("cancelled" if self._cancel else "running"),
                    target_name=self.target_name,
                    output_dir=str(self.output_dir),
                    deep_scan=self.deep_scan,
                    enable_raw=self.enable_raw,
                    recovery_mode=self.mode.value,
                    partition_lba=self.partition_lba,
                    image=fp,
                    volume_summary=vol_summary,
                    complete=reason == "completed",
                )
                store.save_meta(meta)
                store.replace_int_set(
                    "visited_clusters", state.get("known_clusters") or []
                )
                store.replace_int_set(
                    "already_checked", state.get("already_checked") or []
                )
                store.replace_int_set(
                    "orphan_candidates", state.get("orphan_candidates") or []
                )
                store.replace_int_set(
                    "analyzed_dirs", self.tracker.analyzed_starts
                )
                store.replace_stats("global", telem)
                store.save_json_blob("scan_state", state)
                if self._latest_recon is not None:
                    store.save_json_blob(
                        "reconstruction_snapshot",
                        {
                            "version": 1,
                            "target_name": self._latest_recon.target_name,
                            "target_cluster": self._latest_recon.target_cluster,
                            "target_found": self._latest_recon.target_found,
                            "target_directory_intact": self._latest_recon.target_directory_intact,
                            "nodes": [_node_to_dict(n) for n in self._latest_recon.nodes],
                            "orphan_directories": [
                                _node_to_dict(n)
                                for n in self._latest_recon.orphan_directories
                            ],
                            "target_linked_orphans": [
                                _node_to_dict(n)
                                for n in self._latest_recon.target_linked_orphans
                            ],
                            "stats": dict(self._latest_recon.stats),
                            "messages": list(self._latest_recon.messages),
                        },
                    )
                store.save_json_blob("settings", {
                    "target_name": self.target_name,
                    "deep_scan": self.deep_scan,
                    "enable_raw": self.enable_raw,
                    "partition_lba": self.partition_lba,
                    "checkpoint_interval_sec": self.checkpoint_interval_sec,
                    "checkpoint_every_candidates": self.checkpoint_every_candidates,
                    "recovery_mode": self.mode.value,
                })
                for stage in state.get("completed_stages") or []:
                    store.upsert_task(f"stage:{stage}", "stage", {"name": stage}, "done")
                store.upsert_task(
                    f"stage:{state.get('stage')}",
                    "stage",
                    {"name": state.get("stage"), "index": state.get("stage_index")},
                    "pending" if not self._cancel else "cancelled",
                )
                if self._latest_recon:
                    for rel in self._latest_recon.target_linked_orphans:
                        store.upsert_relation(
                            rel.cluster,
                            rel.parent_cluster or -1,
                            (rel.link_confidence or Confidence.HIGH).value,
                            rel.evidence,
                        )

            if self._ckpt_store is None or self._ckpt_store.path != path:
                if self._ckpt_store is not None:
                    self._ckpt_store.close()
                self._ckpt_store = CheckpointStore(path)
            else:
                self._ckpt_store.close()
            atomic_checkpoint_write(self._ckpt_store, writer)
            self._last_checkpoint_at = time.monotonic()
            self._candidates_since_ckpt = 0
            saved_at = time.time()
            if self.telemetry:
                self.telemetry.update(
                    checkpoint_path=str(path),
                    checkpoint_saved_at=saved_at,
                    operation=f"Zapisano checkpoint ({reason}): {path}",
                )
            if self.logger:
                self.logger.info("Checkpoint (%s) → %s", reason, path)
            if self._checkpoint_cb:
                self._checkpoint_cb(str(path), saved_at)
            return path

    @staticmethod
    def load_partial_session(path: str | Path) -> RecoverySession:
        """Load discovered objects from a checkpoint without resuming a scan."""
        store = CheckpointStore(path)
        store.open()
        try:
            meta = store.load_meta()
            snapshot = store.load_json_blob("reconstruction_snapshot")
            if meta is None or not snapshot:
                stats = store.load_stats("global")
                return RecoverySession(
                    image_path=meta.image.path if meta and meta.image else "",
                    output_dir=meta.output_dir if meta else "",
                    target_name=meta.target_name if meta else "",
                    volume_summary=dict(meta.volume_summary) if meta else {},
                    stats=stats,
                    messages=[
                        "Checkpoint zawiera agregaty, ale nie zawiera "
                        "trwałego snapshotu pełnych obiektów.",
                    ],
                    checkpoint_path=str(path),
                )
            target_nodes = [_node_from_dict(raw) for raw in snapshot.get("nodes") or []]
            orphan_nodes = [
                _node_from_dict(raw)
                for raw in snapshot.get("orphan_directories") or []
            ]
            linked_nodes = [
                _node_from_dict(raw)
                for raw in snapshot.get("target_linked_orphans") or []
            ]
            items = _partial_items(target_nodes, "target")
            items.extend(_partial_items(linked_nodes, "target"))
            items.extend(_partial_items(orphan_nodes, "orphan"))
            stats = dict(snapshot.get("stats") or {})
            stats.setdefault("wyniki_czesciowe", len(items))
            return RecoverySession(
                image_path=meta.image.path if meta.image else "",
                output_dir=meta.output_dir,
                target_name=meta.target_name,
                volume_summary=dict(meta.volume_summary),
                items=items,
                messages=list(snapshot.get("messages") or [])
                + ["Wczytano snapshot częściowych wyników; pliki nie są jeszcze wyeksportowane."],
                stats=stats,
                checkpoint_path=str(path),
            )
        finally:
            store.close()

    def export_checkpoint_results(
        self, path: str | Path, *, output_dir: str | Path | None = None
    ) -> RecoverySession:
        """Export the reconstructed objects stored in a checkpoint.

        This opens the image read-only and never resumes or mutates scan state.
        """
        checkpoint = Path(path)
        store = CheckpointStore(checkpoint)
        store.open()
        try:
            errors = store.validate()
            if errors:
                raise ValueError("Checkpoint niepoprawny: " + "; ".join(errors))
            meta = store.load_meta()
            snapshot = store.load_json_blob("reconstruction_snapshot")
            if meta is None or not snapshot:
                raise ValueError(
                    "Checkpoint nie zawiera trwałego snapshotu znalezionych obiektów"
                )
            target_nodes = [_node_from_dict(raw) for raw in snapshot.get("nodes") or []]
            linked_nodes = [
                _node_from_dict(raw)
                for raw in snapshot.get("target_linked_orphans") or []
            ]
            orphan_nodes = [
                _node_from_dict(raw)
                for raw in snapshot.get("orphan_directories") or []
            ]
        finally:
            store.close()

        if output_dir is not None:
            self.output_dir = Path(output_dir)
        self._prepare_output()
        self._ensure_result_store()
        self._result_store.recover_incomplete_candidates()
        self.open_volume()
        assert self.volume is not None
        if not self.target_name:
            self.target_name = self._infer_target_name(self.volume)
        known_offsets: Set[int] = set()
        self._export_seen_objects: Set[tuple[str, int, bool]] = set()
        self._export_processed = 0
        self._export_total = max(
            1,
            sum(
                1
                for n in flatten_nodes(
                    target_nodes + linked_nodes + orphan_nodes
                )
            ),
        )
        items = self._export_tree(
            self.volume,
            target_nodes + linked_nodes,
            self._target_root(),
            category="target",
            known_offsets=known_offsets,
        )
        for orphan in orphan_nodes:
            items.extend(
                self._export_tree(
                    self.volume,
                    [orphan],
                    self._orphan_root(),
                    category="orphan",
                    known_offsets=known_offsets,
                )
            )
        return RecoverySession(
            image_path=str(self.image_path),
            output_dir=str(self.output_dir),
            target_name=str(snapshot.get("target_name") or self.target_name),
            volume_summary=self.volume.summary(),
            items=items,
            messages=["Wyeksportowano obiekty ze snapshotu checkpointu."],
            stats={"plikow_lacznie": sum(not i.is_directory for i in items)},
            checkpoint_path=str(checkpoint),
        )

    def export_live_results(
        self, *, output_dir: str | Path | None = None,
        categories: Optional[Set[str]] = None,
        object_ids: Optional[Set[str]] = None,
        paths: Optional[Set[str]] = None,
    ) -> RecoverySession:
        """Export records already committed to results.sqlite."""
        store = ResultStore(self.results_path())
        store.open()
        try:
            records = store.list_objects(
                sorted(categories) if categories else None
            )
            if object_ids is not None:
                records = [r for r in records if r["object_id"] in object_ids]
            if paths is not None:
                records = [r for r in records if r["path"] in paths]
        finally:
            store.close()
        if output_dir is not None:
            self.output_dir = Path(output_dir)
        self._prepare_output()
        self.open_volume()
        assert self.volume is not None
        items: List[RecoveryItem] = []
        store = ResultStore(self.results_path())
        store.open()
        try:
            for record in records:
                if record["object_type"] == "directory":
                    continue
                node = RecoveredNode(
                    name=safe_filename(Path(record["path"]).name),
                    path=record["path"],
                    cluster=record["cluster"],
                    size=record["size"],
                    is_directory=False,
                    is_deleted=False,
                    confidence=Confidence(record["confidence"]),
                    evidence=record["evidence"],
                    belongs_to_target=record["category"] == "target",
                )
                destination = (
                    self._target_root()
                    if record["category"] == "target"
                    else self._unknown_root()
                )
                destination.mkdir(parents=True, exist_ok=True)
                dest = destination / safe_filename(node.name)
                if dest.exists():
                    dest = destination / f"{dest.stem}_{record['cluster']}{dest.suffix}"
                integrity = "uszkodzony"
                written = 0
                try:
                    chain = self.volume.follow_chain(node.cluster)
                    if not chain:
                        integrity = "uszkodzony"
                    else:
                        written = self._write_file_from_clusters(
                            self.volume, node, dest
                        )
                        integrity = (
                            "poprawnie odzyskany"
                            if written == node.size
                            else "częściowo odzyskany"
                        )
                except (OSError, ValueError, RuntimeError):
                    integrity = "uszkodzony"
                items.append(
                    RecoveryItem(
                        path=node.path,
                        output_path=str(dest.relative_to(self.output_dir)),
                        cluster=node.cluster,
                        size=written,
                        confidence=node.confidence.value,
                        category=record["category"],
                        evidence=record["evidence"] + [f"integralność: {integrity}"],
                        exported=written > 0 and integrity != "uszkodzony",
                    )
                )
                if record["object_id"]:
                    store.mark_exported(record["object_id"])
        finally:
            store.close()
        return RecoverySession(
            image_path=str(self.image_path),
            output_dir=str(self.output_dir),
            target_name=self.target_name,
            volume_summary=self.volume.summary(),
            items=items,
            messages=["Wyeksportowano zapisane wyniki bieżące."],
            stats={"plikow_lacznie": len(items)},
        )

    def load_checkpoint(self, path: str | Path) -> CheckpointMeta:
        store = CheckpointStore(path)
        store.open()
        try:
            errors = store.validate()
            if errors:
                raise ValueError("Checkpoint niepoprawny: " + "; ".join(errors))
            meta = store.load_meta()
            if meta is None:
                raise ValueError("Brak metadanych checkpointu")
            if meta.image is None:
                raise ValueError("Checkpoint bez odcisku obrazu")
            current = fingerprint_image(self.image_path)
            if not meta.image.matches(current):
                raise ValueError(
                    "Obraz nie zgadza się z checkpointem "
                    f"(oczekiwano size={meta.image.size}, jest {current.size})"
                )
            if meta.target_name and meta.target_name != self.target_name:
                raise ValueError(
                    f"Inny katalog docelowy w checkpoincie: {meta.target_name!r}"
                )
            checkpoint_mode = meta.recovery_mode or RecoveryMode.FULL_RECOVERY.value
            if self.mode != RecoveryMode(checkpoint_mode):
                raise ValueError(
                    f"Inny tryb odzyskiwania w checkpoincie: {checkpoint_mode}"
                )
            state = store.load_json_blob("scan_state") or {}
            self._scan_state.update(state)
            self.tracker.import_state(state.get("tracker") or {})
            self.deep_scan = meta.deep_scan
            self.enable_raw = meta.enable_raw
            if meta.partition_lba is not None:
                self.partition_lba = meta.partition_lba
            return meta
        finally:
            store.close()

    def _maybe_auto_checkpoint(self, *, force: bool = False, reason: str = "auto") -> None:
        if self._checkpoint_request.is_set():
            self._checkpoint_request.clear()
            force = True
            reason = "manual"
        now = time.monotonic()
        due_time = (now - self._last_checkpoint_at) >= self.checkpoint_interval_sec
        due_cand = self._candidates_since_ckpt >= self.checkpoint_every_candidates
        if force or due_time or due_cand:
            try:
                self.save_checkpoint(reason=reason)
            except Exception as exc:
                if self.logger:
                    self.logger.exception("Nie udało się zapisać checkpointu: %s", exc)

    def run(
        self,
        progress_cb: ProgressCb = None,
        telemetry_cb: TelemetryCb = None,
        checkpoint_cb: CheckpointCb = None,
    ) -> RecoverySession:
        self._cancel = False
        self._pause.set()
        self._checkpoint_cb = checkpoint_cb
        self._prepare_output()
        self._ensure_result_store()
        self._result_store.recover_incomplete_candidates()
        self.logger = create_scanner_logger(self.output_dir)
        self.telemetry = ScanTelemetry(
            self.image_path.stat().st_size if self.image_path.is_file() else 0,
            on_update=telemetry_cb,
        )
        self.logger.info("Rozpoczęcie skanowania obrazu: %s", self.image_path)
        self._last_checkpoint_at = time.monotonic()

        if self.resume_checkpoint and self.resume_checkpoint.is_file():
            meta = self.load_checkpoint(self.resume_checkpoint)
            self.logger.info(
                "Wznowiono checkpoint %s (etap=%s)", self.resume_checkpoint, meta.stage
            )
            if progress_cb:
                progress_cb(1, f"Wznowiono z checkpointu: etap {meta.stage}")

        last_log_at = 0.0
        last_log_cluster = -1
        seen_entry_offsets: Set[int] = set()

        def telemetry_event(**changes: object) -> None:
            nonlocal last_log_at, last_log_cluster
            entry_offset = changes.pop("entry_offset", None)
            set_orphan = changes.pop("set_orphan_candidates", None)
            if self.telemetry:
                increments = {}
                for key in list(changes.keys()):
                    if key in ADDITIVE_FIELDS:
                        increments[key] = int(changes.pop(key))
                if isinstance(entry_offset, int):
                    if entry_offset in seen_entry_offsets:
                        increments.pop("directory_entries", None)
                        increments.pop("directories_found", None)
                        increments.pop("files_found", None)
                    else:
                        seen_entry_offsets.add(entry_offset)
                for key, amount in increments.items():
                    self.telemetry.increment(key, amount)
                if set_orphan is not None:
                    changes["orphan_candidates"] = int(set_orphan)
                    changes["candidates"] = int(set_orphan)
                if changes:
                    self.telemetry.update(**changes)
                # auto-checkpoint po kandydacie
                if increments.get("orphan_candidates_checked"):
                    self._candidates_since_ckpt += int(
                        increments["orphan_candidates_checked"]
                    )
                    self._maybe_auto_checkpoint(reason="candidates")
            cluster = changes.get("cluster")
            now = time.monotonic()
            should_log = bool(changes.get("operation")) and (
                now - last_log_at >= 2.0
                or (
                    isinstance(cluster, int)
                    and abs(cluster - last_log_cluster) >= 4096
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
            self._wait_if_paused()
            self._maybe_auto_checkpoint()

        self._telemetry_event = telemetry_event
        if self.telemetry:
            self.telemetry.update(status="skanowanie", stage="Otwarcie i walidacja obrazu")
        self._scan_state["stage"] = "open"
        self._scan_state["stage_index"] = 0

        vol = self.open_volume(progress_cb)
        if not self.target_name:
            self.target_name = self._infer_target_name(vol)
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
            self._wait_if_paused()
            if self._cancelled():
                raise InterruptedError("Anulowano")
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
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"open", "fat"}
        )
        self.save_checkpoint(reason="stage_fat")

        def wrap_progress(pct: int, msg: str) -> None:
            self._wait_if_paused()
            stage = "Wyszukiwanie folderu docelowego"
            overall: Optional[float] = None
            stage_pct: Optional[float] = None
            low = msg.lower()
            if "osieroc" in low:
                stage = "Skanowanie osieroconych katalogów"
                # pct z find_orphan to już % kandydatów
                if pct >= 0:
                    stage_pct = float(pct)
                    overall = 45.0 + 0.25 * stage_pct  # 45–70
            elif "skan klastr" in low or ("klastr" in low and "osieroc" not in low):
                stage = "Głębokie skanowanie klastrów"
                if pct >= 0:
                    stage_pct = float(pct)
                    overall = 10.0 + 0.35 * min(pct, 70) / 70.0 * 100 / 100
                    overall = 10.0 + (min(max(pct, 0), 70) / 70.0) * 35.0
            elif "drzew" in low or "buduję" in low:
                stage = "Rekonstrukcja znalezionych plików"
                overall = 70.0
            elif "zakończona" in low or "zakonczona" in low:
                stage = "Rekonstrukcja znalezionych plików"
                overall = 72.0
                stage_pct = 100.0
            elif pct < 0:
                overall = None
                stage_pct = None
            else:
                overall = 10.0 + min(pct, 100) * 0.35

            if progress_cb:
                if overall is None:
                    progress_cb(-1, msg)
                else:
                    progress_cb(int(min(90, max(0, overall))), msg)
            if self.logger:
                self.logger.info("postęp=%s%% %s", pct, msg)
            if self.telemetry:
                self.telemetry.update(
                    stage=stage,
                    operation=msg,
                    stage_pct=stage_pct,
                    overall_pct=overall,
                )
            self._scan_state["stage"] = stage

        resume_state = None
        if self.resume_checkpoint:
            resume_state = {
                "orphan_candidates": self._scan_state.get("orphan_candidates") or [],
                "already_checked": self._scan_state.get("already_checked") or [],
                "tracker": self.tracker.export_state(),
            }

        self._scan_state["stage"] = "reconstruct"
        self._scan_state["stage_index"] = 1
        recon = reconstruct_target(
            vol,
            self.target_name,
            progress_cb=wrap_progress,
            cancel_cb=self._cancelled,
            pause_cb=self._wait_if_paused,
            deep_scan=self.deep_scan,
            telemetry_cb=self._telemetry_event,
            tracker=self.tracker,
            profiler=self.profiler,
            resume_state=resume_state,
            candidate_cb=self._persist_candidate,
            node_cb=self._persist_node,
            budget_factory=lambda: CandidateBudget(
                max_seconds=self.candidate_max_seconds,
                max_reads=self.candidate_max_reads,
                max_clusters=self.candidate_max_clusters,
                max_records=self.candidate_max_records,
            ),
            retry_deferred=self.retry_deferred,
            target_cluster_hint=self._scan_state.get("target_cluster"),
        )
        self._flush_result_batch()
        self._latest_recon = recon
        self._scan_state["target_cluster"] = recon.target_cluster
        self._scan_state["orphan_candidates"] = list(recon.orphan_candidates)
        self._scan_state["already_checked"] = list(recon.already_checked)
        self._scan_state["known_clusters"] = sorted(
            set(self._scan_state.get("known_clusters") or [])
            | {n.cluster for n in flatten_nodes(recon.nodes) if n.cluster >= 2}
            | {
                n.cluster
                for n in flatten_nodes(recon.orphan_directories)
                if n.cluster >= 2
            }
            | {
                n.cluster
                for n in flatten_nodes(recon.target_linked_orphans)
                if n.cluster >= 2
            }
        )
        if recon.tracker:
            self._scan_state["tracker"] = recon.tracker.export_state()
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"reconstruct"}
        )
        self.save_checkpoint(reason="stage_reconstruct")

        items: List[RecoveryItem] = []
        known_offsets: Set[int] = set()
        self._export_seen_objects = set()
        self._export_processed = 0
        self._export_total = max(
            1, len(flatten_nodes(recon.nodes + recon.orphan_directories))
        )
        if self.mode == RecoveryMode.TARGET_ONLY:
            self._export_total = max(1, len(flatten_nodes(recon.nodes)))

        if progress_cb:
            progress_cb(55, "Rekonstruuję znalezione pliki…")
        if self.telemetry:
            self.telemetry.update(stage="Rekonstrukcja znalezionych plików")
        target_root = self._target_root()
        items.extend(
            self._export_tree(
                vol, recon.nodes, target_root, category="target", known_offsets=known_offsets
            )
        )

        if progress_cb:
            progress_cb(62, "Eksportuję osierocone katalogi…")
        orphan_root = self._orphan_root()
        if self.mode == RecoveryMode.FULL_RECOVERY:
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
        if self.mode == RecoveryMode.FULL_RECOVERY:
            if progress_cb:
                progress_cb(66, "Eksportuję pozostałe wpisy z katalogu głównego…")
            items.extend(
                self._export_other_root_entries(
                    vol, recon, known_offsets=known_offsets, progress_cb=progress_cb
                )
            )
        elif progress_cb:
            progress_cb(
                78,
                "TARGET_ONLY: pomijam eksport pozostałych wpisów katalogu głównego.",
            )
        self._flush_result_batch()

        if not recon.target_found:
            recon.messages.append(
                "Katalog docelowy nie został znaleziony — pliki nie zostały do niego przypisane."
            )
        if recon.target_found and not recon.target_linked_orphans and not recon.target_directory_intact:
            recon.messages.append(
                "Nie znaleziono podfolderów należących do katalogu docelowego "
                f"(„{self.target_name}”)."
            )

        carved: List[CarvedFile] = []
        if (
            self.mode == RecoveryMode.FULL_RECOVERY
            and self.enable_raw
            and "raw" not in (self._scan_state.get("completed_stages") or [])
        ):
            if progress_cb:
                progress_cb(70, "Carving RAW (sygnatury)…")
            self._scan_state["stage"] = "raw"

            def raw_prog(pct: int, msg: str) -> None:
                self._wait_if_paused()
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
            self._scan_state["completed_stages"] = list(
                set(self._scan_state.get("completed_stages") or []) | {"raw"}
            )
            self.save_checkpoint(reason="stage_raw")

        ckpt = self.save_checkpoint(reason="final")
        # Kompatybilny JSON-podsumowanie
        json_ckpt = self.output_dir / "Raport" / "checkpoint.json"
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
            profile_report=recon.profile_report or self.profiler.format_report(),
        )
        self._save_legacy_checkpoint(session, json_ckpt)
        # Zapisz profil
        profile_path = self.output_dir / "Raport" / "profile.txt"
        profile_path.write_text(session.profile_report, encoding="utf-8")

        if progress_cb:
            progress_cb(95, "Generuję raporty…")
        if self.telemetry:
            self.telemetry.update(stage="Generowanie wyników i raportu")
        write_reports(session, self.output_dir / "Raport")

        if progress_cb:
            progress_cb(100, "Gotowe")
        self.session = session
        if self.telemetry:
            self.telemetry.update(
                status="zakończone",
                stage="Zakończone",
                operation="Gotowe",
                overall_pct=100.0,
                stage_pct=100.0,
            )
        if self.logger:
            self.logger.info("Skanowanie zakończone: %s", session.stats)
            self.logger.info("\n%s", session.profile_report)
        self._scan_state["stage"] = "done"
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"done"}
        )
        self.save_checkpoint(reason="completed")
        return session

    def cancel_and_save(self) -> Optional[Path]:
        """Anuluj po zapisaniu checkpointu."""
        path = None
        try:
            path = self.save_checkpoint(reason="cancel")
        except Exception as exc:
            if self.logger:
                self.logger.exception("Checkpoint przy anulowaniu nieudany: %s", exc)
            path = None
        self.request_cancel()
        return path

    def _export_other_root_entries(
        self,
        vol: Fat32Volume,
        recon: ReconstructionResult,
        *,
        known_offsets: Set[int],
        progress_cb: ProgressCb = None,
    ) -> List[RecoveryItem]:
        unknown_root = self._unknown_root()
        items: List[RecoveryItem] = []
        try:
            root_entries = read_directory(
                vol, vol.bpb.root_cluster, include_deleted=True, tracker=self.tracker
            )
        except Exception:
            return items
        target_cl = recon.target_cluster
        shared_visited: Set[int] = {vol.bpb.root_cluster}
        total = len([e for e in root_entries if not e.is_dot and not e.is_volume_label])
        processed = 0
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
                if e.cluster in shared_visited:
                    continue
                nodes = build_tree_from_directory(
                    vol,
                    e.cluster,
                    e.name,
                    belongs_to_target=False,
                    tracker=self.tracker,
                    visited=shared_visited,
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
                        progress_cb=progress_cb,
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
                        progress_cb=progress_cb,
                    )
                )
            processed += 1
            if progress_cb:
                pct = 66 + int(12 * processed / max(total, 1))
                progress_cb(
                    min(78, pct),
                    f"Rekonstrukcja obiektów: {self._export_processed}/"
                    f"{self._export_total} — {e.name}",
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
        progress_cb: ProgressCb = None,
    ) -> List[RecoveryItem]:
        items: List[RecoveryItem] = []
        unknown_base = self._unknown_root()

        def export_node(node: RecoveredNode, parent_dir: Path) -> None:
            if self._cancelled():
                raise InterruptedError("Anulowano")
            name = safe_filename(node.name)
            entry = node.source_entry
            object_key = (
                ("entry", entry.raw_offset, node.is_directory)
                if entry is not None
                else (node.path, node.cluster, node.is_directory)
            )
            if object_key in self._export_seen_objects:
                return
            self._export_seen_objects.add(object_key)
            self._persist_node(node)
            self._export_processed += 1
            if progress_cb:
                pct = 55 + int(
                    23 * self._export_processed / max(self._export_total, 1)
                )
                progress_cb(
                    min(78, pct),
                    f"Rekonstrukcja obiektów: {self._export_processed}/"
                    f"{self._export_total} — {node.name}",
                )
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
        stats = {
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
            "target_intact": 1 if recon.target_directory_intact else 0,
            "target_subdirectories": len(recon.target_linked_orphans),
            "target_files_in_subdirectories": recon.stats.get("target_linked_files", 0),
            "unique_clusters": recon.stats.get("unique_clusters_analyzed", 0),
            "cluster_reads": recon.stats.get("cluster_reads", 0),
            "rereads": recon.stats.get("rereads", 0),
        }
        # Keep names from the v1 results schema readable by older consumers.
        stats["podfoldery_bhp_szkoly"] = stats["target_subdirectories"]
        stats["pliki_w_podfolderach_bhp"] = stats["target_files_in_subdirectories"]
        return stats

    def _save_legacy_checkpoint(self, session: RecoverySession, path: Path) -> None:
        data = {
            "image_path": session.image_path,
            "output_dir": session.output_dir,
            "target_name": session.target_name,
            "volume_summary": session.volume_summary,
            "messages": session.messages,
            "stats": session.stats,
            "sqlite_checkpoint": session.checkpoint_path,
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
        self._flush_result_batch()
        if self._result_store is not None:
            self._result_store.close()
            self._result_store = None
        if self._ckpt_store is not None:
            self._ckpt_store.close()
            self._ckpt_store = None
        if self.reader:
            self.reader.close()
            self.reader = None


def available_checkpoints(output_dir: str | Path) -> List[dict]:
    return list_checkpoints(default_checkpoint_dir(output_dir))
