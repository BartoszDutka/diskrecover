"""Parsowanie wpisów katalogów FAT32 (SFN + LFN), w tym usuniętych."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Callable, Iterable, List, Optional, Tuple

from diskrecover.core.fat32 import Fat32Volume


class Attr(IntFlag):
    READ_ONLY = 0x01
    HIDDEN = 0x02
    SYSTEM = 0x04
    VOLUME_ID = 0x08
    DIRECTORY = 0x10
    ARCHIVE = 0x20
    LFN = 0x0F


DELETED_MARKER = 0xE5
END_MARKER = 0x00


@dataclass
class DirEntry:
    name: str
    short_name: str
    attributes: int
    cluster: int
    size: int
    is_deleted: bool
    is_directory: bool
    is_volume_label: bool
    is_lfn: bool
    raw_offset: int  # offset w obrazie
    create_time: int = 0
    create_date: int = 0
    write_time: int = 0
    write_date: int = 0
    access_date: int = 0
    lfn_parts: List[str] = field(default_factory=list)
    parent_cluster: Optional[int] = None
    classification: str = "confirmed"

    @property
    def is_dot(self) -> bool:
        return self.short_name in (".", "..") or self.name in (".", "..")


@dataclass
class DirectoryParseStats:
    checked_records: int = 0
    rejected_records: int = 0
    valid_entries: int = 0
    lfn_entries: int = 0
    deleted_entries: int = 0
    volume_labels: int = 0
    empty_records: int = 0
    rejection_examples: List[str] = field(default_factory=list)
    accepted_examples: List[str] = field(default_factory=list)

    def reject(self, offset: int, reason: str) -> None:
        self.rejected_records += 1
        if len(self.rejection_examples) < 10:
            self.rejection_examples.append(f"0x{offset:X}: {reason}")

    def accept(self, entry: DirEntry) -> None:
        self.valid_entries += 1
        if len(self.accepted_examples) < 10:
            self.accepted_examples.append(
                f"0x{entry.raw_offset:X}: {entry.name!r} "
                f"cluster={entry.cluster} "
                f"{'DIR' if entry.is_directory else 'FILE'}"
            )


def _decode_lfn_chunk(data: bytes) -> str:
    # LFN: name1[1:11] UCS-2, name2[14:26], name3[28:32]
    chars = []
    for start, end in ((1, 11), (14, 26), (28, 32)):
        for i in range(start, end, 2):
            if i + 1 >= len(data):
                break
            code = int.from_bytes(data[i : i + 2], "little")
            if code == 0x0000 or code == 0xFFFF:
                return "".join(chars)
            chars.append(chr(code))
    return "".join(chars)


def _parse_sfn(name_bytes: bytes) -> str:
    if not name_bytes or name_bytes[0] == END_MARKER:
        return ""
    raw = bytearray(name_bytes[:11])
    deleted = raw[0] == DELETED_MARKER
    if deleted:
        raw[0] = ord("_")
    base = bytes(raw[0:8]).decode("cp437", errors="replace").rstrip(" ")
    ext = bytes(raw[8:11]).decode("cp437", errors="replace").rstrip(" ")
    if ext:
        return f"{base}.{ext}"
    return base


def _entry_cluster(ent: bytes) -> int:
    return int.from_bytes(ent[26:28], "little") | (
        int.from_bytes(ent[20:22], "little") << 16
    )


def _sfn_checksum(name_bytes: bytes) -> int:
    checksum = 0
    for value in name_bytes[:11]:
        checksum = ((checksum & 1) << 7) + (checksum >> 1) + value
        checksum &= 0xFF
    return checksum


def parse_entries_from_buffer(
    buf: bytes,
    base_offset: int,
    parent_cluster: Optional[int] = None,
    max_cluster: Optional[int] = None,
    stats: Optional[DirectoryParseStats] = None,
    diagnostic_cb: Optional[Callable[[str], None]] = None,
) -> List[DirEntry]:
    """Parsuj 32-bajtowe wpisy z bufora klastra/katalogu."""
    entries: List[DirEntry] = []
    pending_lfn: List[Tuple[int, str, int, bool]] = []

    for i in range(0, len(buf) - 31, 32):
        ent = buf[i : i + 32]
        first = ent[0]
        if first == END_MARKER:
            if stats:
                stats.empty_records += 1
            break
        if stats:
            stats.checked_records += 1
        attr = ent[11]
        off = base_offset + i

        if attr == Attr.LFN and first != DELETED_MARKER:
            seq = first & 0x1F
            is_last = bool(first & 0x40)
            if seq == 0 or seq > 20:
                if stats:
                    stats.reject(off, "nieprawidłowa sekwencja LFN")
                pending_lfn.clear()
                continue
            chunk = _decode_lfn_chunk(ent)
            pending_lfn.append((seq, chunk, ent[13], is_last))
            if stats:
                stats.lfn_entries += 1
            continue

        if first == DELETED_MARKER and attr == Attr.LFN:
            if stats:
                stats.reject(off, "usunięty rekord ma atrybut LFN")
            pending_lfn.clear()
            continue

        valid_attr = attr != 0 and attr != Attr.LFN and (attr & ~0x3F) == 0
        if not valid_attr:
            if stats:
                stats.reject(off, f"nieprawidłowe atrybuty 0x{attr:02X}")
            pending_lfn.clear()
            continue

        cluster = _entry_cluster(ent)
        is_deleted = first == DELETED_MARKER
        is_dir = bool(attr & Attr.DIRECTORY) and not bool(attr & Attr.VOLUME_ID)
        is_vol = bool(attr & Attr.VOLUME_ID) and not is_dir
        if is_vol and attr != Attr.VOLUME_ID:
            if stats:
                stats.reject(off, "etykieta woluminu ma dodatkowe atrybuty")
            pending_lfn.clear()
            continue
        if max_cluster is not None and cluster > max_cluster:
            if stats:
                stats.reject(off, f"klaster {cluster} poza zakresem 2..{max_cluster}")
            pending_lfn.clear()
            continue
        size = int.from_bytes(ent[28:32], "little")
        if is_dir and cluster < 2:
            if stats:
                stats.reject(off, "katalog ma nieprawidłowy klaster początkowy")
            pending_lfn.clear()
            continue
        if not is_dir and not is_vol and cluster < 2 and size != 0:
            if stats:
                stats.reject(off, "plik niepusty ma klaster 0/1")
            pending_lfn.clear()
            continue

        if first == 0x2E and ent[1] in (0x20, 0x2E):
            # . lub ..
            short = _parse_sfn(ent[0:11])
            entry = DirEntry(
                name=short,
                short_name=short,
                attributes=attr,
                cluster=cluster,
                size=0,
                is_deleted=False,
                is_directory=True,
                is_volume_label=False,
                is_lfn=False,
                raw_offset=off,
                parent_cluster=parent_cluster,
            )
            entries.append(entry)
            if stats:
                stats.accept(entry)
            pending_lfn.clear()
            continue

        short = _parse_sfn(ent[0:11])
        long_name = ""
        if pending_lfn:
            parts = sorted(pending_lfn, key=lambda x: x[0])
            expected = list(range(1, len(parts) + 1))
            valid_sequence = [p[0] for p in parts] == expected
            valid_last = sum(1 for p in pending_lfn if p[3]) == 1
            long_name = "".join(p[1] for p in parts) if valid_sequence and valid_last else ""
            pending_lfn.clear()
            if long_name and ent[0:11] != b"\x00" * 11:
                checksums = {p[2] for p in parts}
                if len(checksums) != 1 or next(iter(checksums)) != _sfn_checksum(ent[0:11]):
                    long_name = ""
                    if diagnostic_cb:
                        diagnostic_cb(
                            f"0x{off:X}: odrzucono LFN — zła suma kontrolna"
                        )

        name = long_name if long_name else short
        if is_deleted and long_name:
            # Usunięte LFN mogą być niekompletne — użyj co mamy
            name = long_name

        entry = DirEntry(
                name=name or short,
                short_name=short,
                attributes=attr,
                cluster=cluster,
                size=0 if is_dir else size,
                is_deleted=is_deleted,
                is_directory=is_dir,
                is_volume_label=is_vol,
                is_lfn=bool(long_name),
                raw_offset=off,
                create_time=int.from_bytes(ent[14:16], "little"),
                create_date=int.from_bytes(ent[16:18], "little"),
                access_date=int.from_bytes(ent[18:20], "little"),
                write_time=int.from_bytes(ent[22:24], "little"),
                write_date=int.from_bytes(ent[24:26], "little"),
                parent_cluster=parent_cluster,
                lfn_parts=[long_name] if long_name else [],
            )
        entries.append(entry)
        if stats:
            if is_deleted:
                stats.deleted_entries += 1
            if is_vol:
                stats.volume_labels += 1
            stats.accept(entry)
    return entries


def parent_cluster_from_dotdot(entries: List[DirEntry]) -> Optional[int]:
    for e in entries:
        if e.is_dot and e.short_name == "..":
            return e.cluster
    return None


def self_cluster_from_dot(entries: List[DirEntry]) -> Optional[int]:
    for e in entries:
        if e.is_dot and e.short_name == ".":
            return e.cluster
    return None


def read_directory(
    vol: Fat32Volume,
    start_cluster: int,
    *,
    include_deleted: bool = True,
    max_clusters: int = 4096,
    visited_clusters: Optional[set[int]] = None,
    stats: Optional[DirectoryParseStats] = None,
    tracker: object = None,
    use_cache: bool = True,
    emit_activity: bool = True,
) -> List[DirEntry]:
    """Odczyt katalogu z opcjonalnym cache trackera (bez ponownego parsowania)."""
    cached = None
    if use_cache and tracker is not None:
        cached = tracker.get_cached(start_cluster)  # type: ignore[attr-defined]
        if cached is not None:
            if visited_clusters is not None:
                visited_clusters.update(cached.chain)
            if stats is not None:
                stats.checked_records += cached.stats.checked_records
                stats.rejected_records += cached.stats.rejected_records
                stats.valid_entries += cached.stats.valid_entries
            entries = cached.entries
            if not include_deleted:
                entries = [e for e in entries if not e.is_deleted]
            return [e for e in entries if not e.is_volume_label]

    if tracker is not None:
        tracker.fat_chains_checked += 1  # type: ignore[attr-defined]

    chain = vol.follow_chain(start_cluster, max_clusters=max_clusters)
    if not chain and start_cluster >= 2:
        chain = [start_cluster]
    if visited_clusters is not None:
        visited_clusters.update(chain)

    all_entries: List[DirEntry] = []
    aggregate = DirectoryParseStats()
    for c in chain:
        if tracker is not None:
            tracker.note_read(c)  # type: ignore[attr-defined]
        data = vol.read_cluster(c)
        cluster_stats = DirectoryParseStats()
        ents = parse_entries_from_buffer(
            data,
            vol.cluster_offset(c),
            parent_cluster=start_cluster,
            max_cluster=vol.total_clusters + 1,
            stats=cluster_stats,
        )
        aggregate.checked_records += cluster_stats.checked_records
        aggregate.rejected_records += cluster_stats.rejected_records
        aggregate.valid_entries += cluster_stats.valid_entries
        aggregate.lfn_entries += cluster_stats.lfn_entries
        aggregate.deleted_entries += cluster_stats.deleted_entries
        aggregate.volume_labels += cluster_stats.volume_labels
        aggregate.empty_records += cluster_stats.empty_records
        if len(aggregate.rejection_examples) < 10:
            aggregate.rejection_examples.extend(
                cluster_stats.rejection_examples[
                    : 10 - len(aggregate.rejection_examples)
                ]
            )
        if len(aggregate.accepted_examples) < 10:
            aggregate.accepted_examples.extend(
                cluster_stats.accepted_examples[
                    : 10 - len(aggregate.accepted_examples)
                ]
            )
        for e in ents:
            if e.is_volume_label:
                continue
            all_entries.append(e)
            if not include_deleted and e.is_deleted:
                continue
            if emit_activity and vol.activity_cb:
                vol.activity_cb(
                    directory_entries=1,
                    files_found=0 if e.is_directory else 1,
                    directories_found=1 if e.is_directory else 0,
                    entry_offset=e.raw_offset,
                )
        if emit_activity and vol.activity_cb:
            vol.activity_cb(
                records_checked=cluster_stats.checked_records,
                records_rejected=cluster_stats.rejected_records,
                valid_entries=cluster_stats.valid_entries,
                confirmed_directories=sum(1 for e in ents if e.is_directory),
                confirmed_files=sum(
                    1
                    for e in ents
                    if not e.is_directory and not e.is_volume_label
                ),
                operation=(
                    f"Odczyt katalogu klastra {c}: "
                    f"zaakceptowano={cluster_stats.valid_entries}, "
                    f"odrzucono={cluster_stats.rejected_records}"
                ),
            )

    if stats is not None:
        stats.checked_records += aggregate.checked_records
        stats.rejected_records += aggregate.rejected_records
        stats.valid_entries += aggregate.valid_entries
        stats.lfn_entries += aggregate.lfn_entries
        stats.deleted_entries += aggregate.deleted_entries
        stats.volume_labels += aggregate.volume_labels
        stats.empty_records += aggregate.empty_records
        stats.rejection_examples.extend(
            aggregate.rejection_examples[: 10 - len(stats.rejection_examples)]
        )
        stats.accepted_examples.extend(
            aggregate.accepted_examples[: 10 - len(stats.accepted_examples)]
        )

    if tracker is not None and use_cache:
        from diskrecover.core.cluster_tracker import DirectoryCacheEntry

        tracker.put_cache(  # type: ignore[attr-defined]
            DirectoryCacheEntry(
                start_cluster=start_cluster,
                chain=tuple(chain),
                entries=list(all_entries),
                stats=aggregate,
                parent_from_dotdot=parent_cluster_from_dotdot(all_entries),
                self_from_dot=self_cluster_from_dot(all_entries),
            )
        )

    if include_deleted:
        return all_entries
    return [e for e in all_entries if not e.is_deleted]


def normalize_name(name: str) -> str:
    n = name.strip().casefold()
    n = re.sub(r"\s+", " ", n)
    return n


def names_match(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)


def name_contains(haystack: str, needle: str) -> bool:
    return normalize_name(needle) in normalize_name(haystack)


def scan_cluster_as_directory(
    vol: Fat32Volume,
    cluster: int,
    *,
    data: Optional[bytes] = None,
    tracker: object = None,
) -> List[DirEntry]:
    """Traktuj pojedynczy klaster jako potencjalny katalog (rekonstrukcja)."""
    if data is None:
        if tracker is not None:
            tracker.note_read(cluster)  # type: ignore[attr-defined]
        data = vol.read_cluster(cluster)
    return parse_entries_from_buffer(
        data,
        vol.cluster_offset(cluster),
        parent_cluster=cluster,
        max_cluster=vol.total_clusters + 1,
    )


def looks_like_directory_cluster(
    data: bytes,
    *,
    cluster: Optional[int] = None,
    max_cluster: Optional[int] = None,
) -> bool:
    """Return true only for a structurally plausible FAT directory cluster."""
    if len(data) < 64:
        return False
    dot = data[:32]
    dotdot = data[32:64]
    if (
        dot[0:1] != b"."
        or dot[1] != 0x20
        or dot[11] != Attr.DIRECTORY
        or dotdot[0:2] != b".."
        or dotdot[11] != Attr.DIRECTORY
    ):
        return False
    self_cluster = _entry_cluster(dot)
    parent_cluster = _entry_cluster(dotdot)
    if cluster is not None and self_cluster != cluster:
        return False
    if self_cluster < 2 or (
        max_cluster is not None and self_cluster > max_cluster
    ):
        return False
    return parent_cluster == 0 or (
        parent_cluster >= 2
        and (max_cluster is None or parent_cluster <= max_cluster)
    )
