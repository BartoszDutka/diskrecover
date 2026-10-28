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
RESULTS_SCHEMA_VERSION = 1


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


class ResultStore:
    """WAL-backed live results and candidate queue.

    This database is deliberately separate from the atomically replaced
    scan checkpoint, so the GUI can read it while a worker is scanning.
    """

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
                str(self.path), check_same_thread=False, isolation_level=None
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS result_objects (
                    object_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    object_type TEXT NOT NULL,
                    cluster INTEGER NOT NULL,
                    entry_offset INTEGER,
                    parent_cluster INTEGER,
                    category TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    integrity TEXT NOT NULL,
                    exported INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_queue (
                    candidate_id TEXT PRIMARY KEY,
                    cluster INTEGER NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    safe_stage TEXT NOT NULL,
                    reads INTEGER NOT NULL DEFAULT 0,
                    clusters INTEGER NOT NULL DEFAULT 0,
                    records INTEGER NOT NULL DEFAULT 0,
                    progress REAL NOT NULL DEFAULT 0,
                    started_at REAL,
                    finished_at REAL,
                    reason TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_sequence
                    ON candidate_queue(sequence_no);
                """
            )
            self._conn.execute(
                "INSERT INTO schema_info(key,value) VALUES('version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(RESULTS_SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self.open()
        assert self._conn is not None
        return self._conn.execute(sql, params)

    def upsert_objects(self, records: Sequence[dict]) -> int:
        if not records:
            return 0
        with self._lock:
            self.open()
            assert self._conn is not None
            now = time.time()
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    """
                    INSERT INTO result_objects(
                        object_id,name,path,size,object_type,cluster,entry_offset,
                        parent_cluster,category,confidence,evidence,integrity,
                        exported,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(object_id) DO UPDATE SET
                        name=excluded.name,path=excluded.path,size=excluded.size,
                        object_type=excluded.object_type,cluster=excluded.cluster,
                        entry_offset=excluded.entry_offset,
                        parent_cluster=excluded.parent_cluster,
                        category=excluded.category,confidence=excluded.confidence,
                        evidence=excluded.evidence,integrity=excluded.integrity,
                        updated_at=excluded.updated_at
                    """,
                    [
                        (
                            str(r["object_id"]), str(r.get("name") or ""),
                            str(r.get("path") or ""), int(r.get("size") or 0),
                            str(r.get("object_type") or "file"),
                            int(r.get("cluster") or -1), r.get("entry_offset"),
                            r.get("parent_cluster"), str(r.get("category") or "unknown"),
                            str(r.get("confidence") or "nieustalone"),
                            json.dumps(list(r.get("evidence") or []), ensure_ascii=False),
                            str(r.get("integrity") or "niezweryfikowany"),
                            int(bool(r.get("exported", False))), now,
                        )
                        for r in records
                    ],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(records)

    def list_objects(self, categories: Optional[Sequence[str]] = None) -> List[dict]:
        with self._lock:
            query = "SELECT * FROM result_objects"
            args: list = []
            if categories:
                placeholders = ",".join("?" for _ in categories)
                query += f" WHERE category IN ({placeholders})"
                args.extend(categories)
            query += " ORDER BY object_id"
            rows = self._execute(query, tuple(args)).fetchall()
            return [
                {
                    "object_id": r[0], "name": r[1], "path": r[2], "size": r[3],
                    "object_type": r[4], "cluster": r[5], "entry_offset": r[6],
                    "parent_cluster": r[7], "category": r[8], "confidence": r[9],
                    "evidence": json.loads(r[10]), "integrity": r[11],
                    "exported": bool(r[12]), "updated_at": r[13],
                }
                for r in rows
            ]

    def upsert_candidate(self, candidate_id: str, cluster: int, sequence_no: int,
                         status: str, *, safe_stage: str = "", reads: int = 0,
                         clusters: int = 0, records: int = 0, progress: float = 0,
                         started_at: Optional[float] = None,
                         finished_at: Optional[float] = None,
                         reason: str = "", state: Optional[dict] = None) -> None:
        with self._lock:
            self._execute(
                """
                INSERT INTO candidate_queue(
                    candidate_id,cluster,sequence_no,status,safe_stage,reads,
                    clusters,records,progress,started_at,finished_at,reason,state,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    cluster=excluded.cluster,sequence_no=excluded.sequence_no,
                    status=excluded.status,safe_stage=excluded.safe_stage,
                    reads=excluded.reads,clusters=excluded.clusters,
                    records=excluded.records,progress=excluded.progress,
                    started_at=excluded.started_at,finished_at=excluded.finished_at,
                    reason=excluded.reason,state=excluded.state,updated_at=excluded.updated_at
                """,
                (candidate_id, int(cluster), int(sequence_no), status, safe_stage,
                 int(reads), int(clusters), int(records), float(progress),
                 started_at, finished_at, reason,
                 json.dumps(state or {}, ensure_ascii=False), time.time()),
            )

    def list_candidates(self, status: Optional[str] = None) -> List[dict]:
        with self._lock:
            sql = "SELECT * FROM candidate_queue"
            args: tuple = ()
            if status:
                sql += " WHERE status=?"
                args = (status,)
            sql += " ORDER BY sequence_no"
            return [
                {
                    "candidate_id": r[0], "cluster": r[1], "sequence_no": r[2],
                    "status": r[3], "safe_stage": r[4], "reads": r[5],
                    "clusters": r[6], "records": r[7], "progress": r[8],
                    "started_at": r[9], "finished_at": r[10], "reason": r[11],
                    "state": json.loads(r[12]), "updated_at": r[13],
                }
                for r in self._execute(sql, args).fetchall()
            ]

    def recover_incomplete_candidates(self) -> int:
        """Make a crashed RUNNING candidate explicitly retryable."""
        with self._lock:
            cursor = self._execute(
                """
                UPDATE candidate_queue
                SET status='DEFERRED',
                    reason=CASE WHEN reason='' THEN 'przerwane przed zamknięciem procesu'
                               ELSE reason END,
                    updated_at=?
                WHERE status='RUNNING'
                """,
                (time.time(),),
            )
            return cursor.rowcount

    def mark_exported(self, object_id: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE result_objects SET exported=1, updated_at=? WHERE object_id=?",
                (time.time(), object_id),
            )


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
