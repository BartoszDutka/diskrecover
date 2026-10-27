"""Parsowanie wpisów katalogów FAT32 (SFN + LFN), w tym usuniętych."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Iterable, List, Optional, Tuple

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

    @property
    def is_dot(self) -> bool:
        return self.short_name in (".", "..") or self.name in (".", "..")


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


def parse_entries_from_buffer(
    buf: bytes,
    base_offset: int,
    parent_cluster: Optional[int] = None,
) -> List[DirEntry]:
    """Parsuj 32-bajtowe wpisy z bufora klastra/katalogu."""
    entries: List[DirEntry] = []
    pending_lfn: List[Tuple[int, str]] = []

    for i in range(0, len(buf) - 31, 32):
        ent = buf[i : i + 32]
        first = ent[0]
        if first == END_MARKER:
            break
        attr = ent[11]
        off = base_offset + i

        if attr == Attr.LFN and first != DELETED_MARKER:
            seq = first & 0x1F
            chunk = _decode_lfn_chunk(ent)
            pending_lfn.append((seq, chunk))
            continue

        if first == DELETED_MARKER and attr == Attr.LFN:
            pending_lfn.clear()
            continue

        if first == 0x2E and ent[1] in (0x20, 0x2E):
            # . lub ..
            short = _parse_sfn(ent[0:11])
            cluster = int.from_bytes(ent[26:28], "little") | (
                int.from_bytes(ent[20:22], "little") << 16
            )
            entries.append(
                DirEntry(
                    name=short,
                    short_name=short,
                    attributes=attr,
                    cluster=cluster,
                    size=0,
                    is_deleted=False,
                    is_directory=bool(attr & Attr.DIRECTORY),
                    is_volume_label=False,
                    is_lfn=False,
                    raw_offset=off,
                    parent_cluster=parent_cluster,
                )
            )
            pending_lfn.clear()
            continue

        is_deleted = first == DELETED_MARKER
        is_vol = bool(attr & Attr.VOLUME_ID) and not bool(attr & Attr.DIRECTORY)
        is_dir = bool(attr & Attr.DIRECTORY) and not is_vol
        # Pomiń całkowicie puste / nieprawidłowe
        if attr == 0 and ent[0:11] == b"\x00" * 11:
            pending_lfn.clear()
            continue

        short = _parse_sfn(ent[0:11])
        long_name = ""
        if pending_lfn:
            parts = sorted(pending_lfn, key=lambda x: x[0])
            long_name = "".join(p[1] for p in parts)
            pending_lfn.clear()

        name = long_name if long_name else short
        if is_deleted and long_name:
            # Usunięte LFN mogą być niekompletne — użyj co mamy
            name = long_name

        cluster = int.from_bytes(ent[26:28], "little") | (
            int.from_bytes(ent[20:22], "little") << 16
        )
        size = int.from_bytes(ent[28:32], "little")

        entries.append(
            DirEntry(
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
        )
    return entries


def read_directory(
    vol: Fat32Volume,
    start_cluster: int,
    *,
    include_deleted: bool = True,
    max_clusters: int = 4096,
) -> List[DirEntry]:
    chain = vol.follow_chain(start_cluster, max_clusters=max_clusters)
    if not chain and start_cluster >= 2:
        # Uszkodzony łańcuch — spróbuj przynajmniej pierwszego klastra
        chain = [start_cluster]
    entries: List[DirEntry] = []
    for c in chain:
        data = vol.read_cluster(c)
        ents = parse_entries_from_buffer(
            data, vol.cluster_offset(c), parent_cluster=start_cluster
        )
        for e in ents:
            if not include_deleted and e.is_deleted:
                continue
            if e.is_volume_label:
                continue
            entries.append(e)
    return entries


def normalize_name(name: str) -> str:
    n = name.strip().casefold()
    n = re.sub(r"\s+", " ", n)
    return n


def names_match(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)


def name_contains(haystack: str, needle: str) -> bool:
    return normalize_name(needle) in normalize_name(haystack)


def scan_cluster_as_directory(
    vol: Fat32Volume, cluster: int
) -> List[DirEntry]:
    """Traktuj pojedynczy klaster jako potencjalny katalog (rekonstrukcja)."""
    data = vol.read_cluster(cluster)
    return parse_entries_from_buffer(
        data, vol.cluster_offset(cluster), parent_cluster=cluster
    )


def looks_like_directory_cluster(data: bytes) -> bool:
    """Heurystyka: klaster wygląda na katalog FAT."""
    if len(data) < 64:
        return False
    # Często zaczyna się od . i ..
    if data[0:1] == b"." and data[11] == Attr.DIRECTORY:
        return True
    # Albo zawiera sensowne wpisy SFN
    valid = 0
    for i in range(0, min(len(data), 512) - 31, 32):
        ent = data[i : i + 32]
        if ent[0] == END_MARKER:
            break
        attr = ent[11]
        if attr == Attr.LFN:
            valid += 1
            continue
        if attr & 0x3F and not (attr & ~0x3F & 0xC0):
            # Rozsądne atrybuty
            cluster = int.from_bytes(ent[26:28], "little") | (
                int.from_bytes(ent[20:22], "little") << 16
            )
            if cluster < 0x0FFFFFF0:
                valid += 1
    return valid >= 2
