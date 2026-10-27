"""Thread-safe telemetry and diagnostics for long-running scans."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass
class ScanSnapshot:
    status: str = "gotowy"
    stage: str = ""
    operation: str = ""
    lba: Optional[int] = None
    cluster: Optional[int] = None
    clusters_checked: int = 0
    clusters_total: int = 0
    bytes_read: int = 0
    bytes_total: int = 0
    directory_entries: int = 0
    directories_found: int = 0
    files_found: int = 0
    corruptions: int = 0
    started_at: float = 0.0
    last_activity: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


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
        self._lock = threading.Lock()
        self._on_update = on_update

    def update(self, **changes: object) -> ScanSnapshot:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._snapshot, key):
                    raise AttributeError(f"Nieznany licznik skanera: {key}")
                setattr(self._snapshot, key, value)
            self._snapshot.last_activity = time.monotonic()
            snapshot = ScanSnapshot(**self._snapshot.as_dict())
        if self._on_update:
            self._on_update(snapshot)
        return snapshot

    def increment(self, field: str, amount: int = 1, **changes: object) -> ScanSnapshot:
        with self._lock:
            value = getattr(self._snapshot, field) + amount
        changes[field] = value
        return self.update(**changes)

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            return ScanSnapshot(**self._snapshot.as_dict())

    def heartbeat_age(self) -> float:
        return max(0.0, time.monotonic() - self.snapshot().last_activity)


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
