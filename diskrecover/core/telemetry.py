"""Thread-safe telemetry and diagnostics for long-running scans."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional


@dataclass
class StageCounters:
    unique_clusters_analyzed: int = 0
    cluster_reads: int = 0
    rereads: int = 0
    fat_chains_checked: int = 0
    orphan_candidates: int = 0
    orphan_candidates_checked: int = 0
    orphan_candidates_rejected: int = 0
    orphan_directories_confirmed: int = 0
    records_checked: int = 0
    records_rejected: int = 0
    valid_entries: int = 0
    confirmed_files: int = 0
    confirmed_directories: int = 0
    target_linked_subdirs: int = 0
    target_linked_files: int = 0
    candidates_remaining: int = 0
    candidates_total: int = 0


@dataclass
class ScanSnapshot:
    status: str = "gotowy"
    stage: str = ""
    operation: str = ""
    lba: Optional[int] = None
    cluster: Optional[int] = None
    # globalne
    clusters_checked: int = 0  # unikalne (kompatybilność)
    clusters_total: int = 0
    unique_clusters_analyzed: int = 0
    cluster_reads: int = 0
    rereads: int = 0
    fat_chains_checked: int = 0
    bytes_read: int = 0
    bytes_total: int = 0
    directory_entries: int = 0
    records_checked: int = 0
    records_rejected: int = 0
    valid_entries: int = 0
    candidates: int = 0
    orphan_candidates: int = 0
    orphan_candidates_checked: int = 0
    orphan_candidates_rejected: int = 0
    orphan_directories_confirmed: int = 0
    confirmed_directories: int = 0
    confirmed_files: int = 0
    directories_found: int = 0
    files_found: int = 0
    corruptions: int = 0
    target_linked_subdirs: int = 0
    target_linked_files: int = 0
    candidates_remaining: int = 0
    candidates_total: int = 0
    stage_pct: Optional[float] = None  # None = nieokreślony
    overall_pct: Optional[float] = None
    analysis_rate: float = 0.0  # kandydaci/s lub klastry/s
    unique_objects: int = 0
    started_at: float = 0.0
    last_activity: float = 0.0
    stage_counters: Dict[str, int] = field(default_factory=dict)
    checkpoint_path: str = ""
    checkpoint_saved_at: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


# Pola inkrementowane (nie nadpisywane absolutnie przy update z additive)
ADDITIVE_FIELDS = frozenset(
    {
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
        "unique_clusters_analyzed",
        "cluster_reads",
        "rereads",
        "fat_chains_checked",
        "orphan_candidates",
        "orphan_candidates_checked",
        "orphan_candidates_rejected",
        "orphan_directories_confirmed",
        "target_linked_subdirs",
        "target_linked_files",
    }
)


class ScanTelemetry:
    """Counters shared by the worker and GUI without touching widgets."""

    def __init__(
        self,
        total_bytes: int = 0,
        *,
        on_update: Optional[Callable[[ScanSnapshot], None]] = None,
    ) -> None:
        now = time.monotonic()
        self._snapshot = ScanSnapshot(
            bytes_total=total_bytes, started_at=now, last_activity=now
        )
        self._stage = StageCounters()
        self._lock = threading.Lock()
        self._on_update = on_update
        self._rate_window_t = now
        self._rate_window_n = 0

    def begin_stage(self, stage: str) -> ScanSnapshot:
        with self._lock:
            self._stage = StageCounters()
            self._snapshot.stage = stage
            self._snapshot.stage_pct = None
            self._snapshot.candidates_remaining = 0
            self._snapshot.candidates_total = 0
            self._snapshot.stage_counters = asdict(self._stage)
            self._snapshot.last_activity = time.monotonic()
            snapshot = ScanSnapshot(**self._snapshot.as_dict())
        if self._on_update:
            self._on_update(snapshot)
        return snapshot

    def update(self, **changes: object) -> ScanSnapshot:
        with self._lock:
            stage_inc = changes.pop("stage_increment", None)
            if isinstance(stage_inc, dict):
                for key, amount in stage_inc.items():
                    if hasattr(self._stage, key):
                        setattr(
                            self._stage,
                            key,
                            getattr(self._stage, key) + int(amount),
                        )
            for key, value in changes.items():
                if not hasattr(self._snapshot, key):
                    raise AttributeError(f"Nieznany licznik skanera: {key}")
                setattr(self._snapshot, key, value)
            self._snapshot.stage_counters = asdict(self._stage)
            self._snapshot.unique_objects = self._snapshot.unique_clusters_analyzed
            self._snapshot.last_activity = time.monotonic()
            snapshot = ScanSnapshot(**self._snapshot.as_dict())
        if self._on_update:
            self._on_update(snapshot)
        return snapshot

    def increment(self, field: str, amount: int = 1, **changes: object) -> ScanSnapshot:
        with self._lock:
            value = getattr(self._snapshot, field) + amount
            stage_amount = amount
        changes[field] = value
        changes.setdefault("stage_increment", {})
        if isinstance(changes["stage_increment"], dict) and hasattr(self._stage, field):
            changes["stage_increment"] = {
                **changes["stage_increment"],
                field: stage_amount,
            }
        return self.update(**changes)

    def note_progress_units(self, n: int = 1) -> None:
        """Do wyliczania prędkości analizy (kandydaci/klastry)."""
        with self._lock:
            now = time.monotonic()
            self._rate_window_n += n
            dt = now - self._rate_window_t
            if dt >= 1.0:
                self._snapshot.analysis_rate = self._rate_window_n / dt
                self._rate_window_t = now
                self._rate_window_n = 0

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            self._snapshot.stage_counters = asdict(self._stage)
            return ScanSnapshot(**self._snapshot.as_dict())

    def heartbeat_age(self) -> float:
        return max(0.0, time.monotonic() - self.snapshot().last_activity)

    def export_counters(self) -> Dict[str, int]:
        snap = self.snapshot()
        return {
            "unique_clusters_analyzed": snap.unique_clusters_analyzed,
            "cluster_reads": snap.cluster_reads,
            "rereads": snap.rereads,
            "fat_chains_checked": snap.fat_chains_checked,
            "orphan_candidates": snap.orphan_candidates,
            "orphan_candidates_checked": snap.orphan_candidates_checked,
            "orphan_candidates_rejected": snap.orphan_candidates_rejected,
            "orphan_directories_confirmed": snap.orphan_directories_confirmed,
            "records_checked": snap.records_checked,
            "records_rejected": snap.records_rejected,
            "valid_entries": snap.valid_entries,
            "confirmed_files": snap.confirmed_files,
            "confirmed_directories": snap.confirmed_directories,
            "target_linked_subdirs": snap.target_linked_subdirs,
            "target_linked_files": snap.target_linked_files,
            "directories_found": snap.directories_found,
            "files_found": snap.files_found,
            "corruptions": snap.corruptions,
            "bytes_read": snap.bytes_read,
        }


def create_scanner_logger(output_dir: Path) -> logging.Logger:
    report_dir = output_dir / "Raport"
    report_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"diskrecover.scanner.{id(output_dir)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(
            report_dir / "scanner_debug.log", encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    return logger


def log_exception(logger: logging.Logger, message: str) -> None:
    logger.error("%s\n%s", message, traceback.format_exc())
