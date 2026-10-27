"""Trwały checkpoint skanowania (SQLite, zapis atomowy)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


CHECKPOINT_FORMAT_ID = "diskrecover-checkpoint"
CHECKPOINT_VERSION = 2


@dataclass
class ImageFingerprint:
    path: str
    size: int
    mtime_ns: int
    head_sha256: str
    mid_sha256: str
    tail_sha256: str

    def matches(self, other: "ImageFingerprint") -> bool:
        return (
            self.size == other.size
            and self.head_sha256 == other.head_sha256
            and self.mid_sha256 == other.mid_sha256
            and self.tail_sha256 == other.tail_sha256
        )


def fingerprint_image(path: str | Path, *, sample: int = 64 * 1024) -> ImageFingerprint:
    p = Path(path)
    st = p.stat()
    size = st.st_size
    with open(p, "rb") as fh:
        head = fh.read(sample)
        mid_off = max(0, size // 2 - sample // 2)
        fh.seek(mid_off)
        mid = fh.read(sample)
        tail_off = max(0, size - sample)
        fh.seek(tail_off)
        tail = fh.read(sample)
    return ImageFingerprint(
        path=str(p.resolve()),
        size=size,
        mtime_ns=getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
        head_sha256=hashlib.sha256(head).hexdigest(),
        mid_sha256=hashlib.sha256(mid).hexdigest(),
        tail_sha256=hashlib.sha256(tail).hexdigest(),
    )


@dataclass
class CheckpointMeta:
    format_id: str = CHECKPOINT_FORMAT_ID
    version: int = CHECKPOINT_VERSION
    created_at: float = 0.0
    updated_at: float = 0.0
    stage: str = ""
    stage_index: int = 0
    status: str = "running"  # running | paused | cancelled | completed
    target_name: str = ""
    output_dir: str = ""
    deep_scan: bool = True
    enable_raw: bool = True
    partition_lba: Optional[int] = None
    image: Optional[ImageFingerprint] = None
    volume_summary: Dict[str, Any] = field(default_factory=dict)
    complete: bool = False  # False = zapis niekompletny / przerwany


class CheckpointStore:
    """SQLite store z zapisem przez plik tymczasowy + replace."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self._conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _ensure_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS int_set (
                bucket TEXT NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (bucket, value)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relations (
                child_cluster INTEGER NOT NULL,
                parent_cluster INTEGER NOT NULL,
                confidence TEXT NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (child_cluster, parent_cluster)
            );
            CREATE TABLE IF NOT EXISTS stats (
                scope TEXT NOT NULL,
                name TEXT NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (scope, name)
            );
            """
        )

    def _set_meta(self, key: str, value: Any) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def _get_meta(self, key: str, default: Any = None) -> Any:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def save_meta(self, meta: CheckpointMeta) -> None:
        with self._lock:
            self.open()
            now = time.time()
            if not meta.created_at:
                meta.created_at = now
            meta.updated_at = now
            payload = asdict(meta)
            if meta.image is not None:
                payload["image"] = asdict(meta.image)
            self._set_meta("checkpoint", payload)
            self._set_meta("complete_flag", bool(meta.complete))

    def load_meta(self) -> Optional[CheckpointMeta]:
        with self._lock:
            self.open()
            raw = self._get_meta("checkpoint")
            if not raw:
                return None
            image_raw = raw.get("image")
            image = ImageFingerprint(**image_raw) if image_raw else None
            return CheckpointMeta(
                format_id=raw.get("format_id", ""),
                version=int(raw.get("version", 0)),
                created_at=float(raw.get("created_at") or 0),
                updated_at=float(raw.get("updated_at") or 0),
                stage=raw.get("stage") or "",
                stage_index=int(raw.get("stage_index") or 0),
                status=raw.get("status") or "",
                target_name=raw.get("target_name") or "",
                output_dir=raw.get("output_dir") or "",
                deep_scan=bool(raw.get("deep_scan", True)),
                enable_raw=bool(raw.get("enable_raw", True)),
                partition_lba=raw.get("partition_lba"),
                image=image,
                volume_summary=raw.get("volume_summary") or {},
                complete=bool(raw.get("complete", False)),
            )

    def replace_int_set(self, bucket: str, values: Iterable[int]) -> None:
        with self._lock:
            self.open()
            assert self._conn is not None
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM int_set WHERE bucket=?", (bucket,))
                self._conn.executemany(
                    "INSERT INTO int_set(bucket, value) VALUES(?, ?)",
                    ((bucket, int(v)) for v in values),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load_int_set(self, bucket: str) -> Set[int]:
        with self._lock:
            self.open()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT value FROM int_set WHERE bucket=?", (bucket,)
            ).fetchall()
            return {int(r[0]) for r in rows}

    def upsert_task(self, task_id: str, kind: str, payload: dict, status: str) -> None:
        with self._lock:
            self.open()
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO tasks(task_id, kind, payload, status, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "kind=excluded.kind, payload=excluded.payload, "
                "status=excluded.status, updated_at=excluded.updated_at",
                (task_id, kind, json.dumps(payload, ensure_ascii=False), status, time.time()),
            )

    def list_tasks(self, status: Optional[str] = None) -> List[dict]:
        with self._lock:
            self.open()
            assert self._conn is not None
            if status is None:
                rows = self._conn.execute(
                    "SELECT task_id, kind, payload, status, updated_at FROM tasks"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT task_id, kind, payload, status, updated_at FROM tasks "
                    "WHERE status=?",
                    (status,),
                ).fetchall()
            return [
                {
                    "task_id": r[0],
                    "kind": r[1],
                    "payload": json.loads(r[2]),
                    "status": r[3],
                    "updated_at": r[4],
                }
                for r in rows
            ]

    def replace_stats(self, scope: str, stats: Dict[str, int]) -> None:
        with self._lock:
            self.open()
            assert self._conn is not None
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM stats WHERE scope=?", (scope,))
                self._conn.executemany(
                    "INSERT INTO stats(scope, name, value) VALUES(?,?,?)",
                    ((scope, k, int(v)) for k, v in stats.items()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load_stats(self, scope: str) -> Dict[str, int]:
        with self._lock:
            self.open()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT name, value FROM stats WHERE scope=?", (scope,)
            ).fetchall()
            return {r[0]: int(r[1]) for r in rows}

    def upsert_relation(
        self,
        child_cluster: int,
        parent_cluster: int,
        confidence: str,
        evidence: Sequence[str],
    ) -> None:
        with self._lock:
            self.open()
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO relations(child_cluster, parent_cluster, confidence, evidence) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(child_cluster, parent_cluster) DO UPDATE SET "
                "confidence=excluded.confidence, evidence=excluded.evidence",
                (
                    int(child_cluster),
                    int(parent_cluster),
                    confidence,
                    json.dumps(list(evidence), ensure_ascii=False),
                ),
            )

    def list_relations(self) -> List[dict]:
        with self._lock:
            self.open()
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT child_cluster, parent_cluster, confidence, evidence FROM relations"
            ).fetchall()
            return [
                {
                    "child_cluster": int(r[0]),
                    "parent_cluster": int(r[1]),
                    "confidence": r[2],
                    "evidence": json.loads(r[3]),
                }
                for r in rows
            ]

    def save_json_blob(self, key: str, value: Any) -> None:
        with self._lock:
            self.open()
            self._set_meta(key, value)

    def load_json_blob(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self.open()
            return self._get_meta(key, default)

    def mark_complete(self, complete: bool = True) -> None:
        with self._lock:
            self.open()
            self._set_meta("complete_flag", complete)
            raw = self._get_meta("checkpoint")
            if isinstance(raw, dict):
                raw["complete"] = complete
                raw["updated_at"] = time.time()
                self._set_meta("checkpoint", raw)

    def is_complete(self) -> bool:
        with self._lock:
            self.open()
            return bool(self._get_meta("complete_flag", False))

    def validate(self) -> List[str]:
        """Zwraca listę problemów spójności; pusta = OK."""
        errors: List[str] = []
        meta = self.load_meta()
        if meta is None:
            return ["brak metadanych checkpointu"]
        if meta.format_id != CHECKPOINT_FORMAT_ID:
            errors.append(f"nieznany format: {meta.format_id!r}")
        if meta.version > CHECKPOINT_VERSION:
            errors.append(f"wersja checkpointu nowsza niż obsługiwana: {meta.version}")
        if meta.version < 1:
            errors.append(f"nieobsługiwana wersja: {meta.version}")
        if not self.is_complete():
            errors.append("checkpoint oznaczony jako niekompletny")
        if meta.image is None:
            errors.append("brak odcisku obrazu")
        return errors


def atomic_checkpoint_write(store: CheckpointStore, writer_cb) -> Path:
    """
    Zapisuje checkpoint do pliku tymczasowego w tym samym katalogu,
    a następnie atomowo podmienia docelowy plik.
    writer_cb(temp_store: CheckpointStore) -> None

    Na Windows konieczne jest zamknięcie wszystkich połączeń do ``dest``
    przed ``os.replace``.
    """
    dest = store.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Zamknij docelowe połączenie — inaczej WinError 5 przy replace
    store.close()
    fd, tmp_name = tempfile.mkstemp(
        prefix=dest.stem + ".",
        suffix=".tmp.sqlite",
        dir=str(dest.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        temp_store = CheckpointStore(tmp_path)
        temp_store.open()
        try:
            writer_cb(temp_store)
            temp_store.mark_complete(True)
            # Upewnij się, że wal/shm są domknięte przed replace
            assert temp_store._conn is not None
            temp_store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            temp_store.close()
            temp_store._conn = None
        # Usuń ewentualne pliki WAL starego checkpointu
        for suffix in ("-wal", "-shm"):
            side = Path(str(dest) + suffix)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
        os.replace(tmp_path, dest)
        return dest
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def list_checkpoints(checkpoint_dir: str | Path) -> List[dict]:
    """Lista plików checkpointów z metadanymi (do GUI)."""
    d = Path(checkpoint_dir)
    if not d.is_dir():
        return []
    out: List[dict] = []
    for path in sorted(d.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        info: dict = {
            "path": str(path),
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "valid": False,
            "meta": None,
            "errors": [],
        }
        try:
            store = CheckpointStore(path)
            store.open()
            meta = store.load_meta()
            errs = store.validate()
            store.close()
            info["meta"] = asdict(meta) if meta else None
            info["errors"] = errs
            info["valid"] = meta is not None and not errs
        except Exception as exc:
            info["errors"] = [str(exc)]
        out.append(info)
    return out


def default_checkpoint_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "Raport" / "checkpoints"
