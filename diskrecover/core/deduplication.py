"""Streaming SHA-256 deduplication with an optional SQLite cache."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from diskrecover.core.validation import hash_file, iter_files


class HashCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            )"""
        )
        self.connection.commit()

    def get(self, path: Path) -> str | None:
        stat = path.stat()
        row = self.connection.execute(
            "SELECT sha256 FROM file_hashes WHERE path=? AND size=? AND mtime_ns=?",
            (str(path.resolve()), stat.st_size, stat.st_mtime_ns),
        ).fetchone()
        return str(row[0]) if row else None

    def put(self, path: Path, digest: str) -> None:
        stat = path.stat()
        self.connection.execute(
            "INSERT INTO file_hashes(path,size,mtime_ns,sha256) VALUES(?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256",
            (str(path.resolve()), stat.st_size, stat.st_mtime_ns, digest),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def find_duplicates(
    source: str | Path, *, cache: HashCache | None = None
) -> dict[str, list[Path]]:
    by_size: dict[int, list[Path]] = {}
    for path in iter_files(source):
        by_size.setdefault(path.stat().st_size, []).append(path)
    duplicates: dict[str, list[Path]] = {}
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in paths:
            digest = cache.get(path) if cache else None
            if digest is None:
                digest = hash_file(path)
                if cache:
                    cache.put(path, digest)
            by_hash.setdefault(digest, []).append(path)
        duplicates.update({digest: group for digest, group in by_hash.items() if len(group) > 1})
    return duplicates
