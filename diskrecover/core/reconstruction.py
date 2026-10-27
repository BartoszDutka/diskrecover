"""Rekonstrukcja uszkodzonych katalogów (np. „bhp szkoły”)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set

from diskrecover.core.directory import (
    DirEntry,
    looks_like_directory_cluster,
    name_contains,
    names_match,
    normalize_name,
    parse_entries_from_buffer,
    read_directory,
    scan_cluster_as_directory,
)
from diskrecover.core.fat32 import Fat32Volume


class Confidence(str, Enum):
    HIGH = "wysokie"  # łańcuch FAT + wpis katalogu
    MEDIUM = "srednie"  # wpis katalogu, łańcuch częściowy / heurystyka
    LOW = "niskie"  # tylko orphan / signature / słabe dowody
    UNKNOWN = "nieustalone"


@dataclass
class RecoveredNode:
    """Węzeł odzyskany z katalogu lub rekonstrukcji."""

    name: str
    path: str  # względna ścieżka względem celu
    cluster: int
    size: int
    is_directory: bool
    is_deleted: bool
    confidence: Confidence
    evidence: List[str] = field(default_factory=list)
    source_entry: Optional[DirEntry] = None
    belongs_to_target: bool = False  # True tylko przy dowodach
    children: List["RecoveredNode"] = field(default_factory=list)


@dataclass
class ReconstructionResult:
    target_name: str
    target_cluster: Optional[int]
    target_found: bool
    nodes: List[RecoveredNode]
    orphan_directories: List[RecoveredNode]
    scanned_clusters: int
    messages: List[str] = field(default_factory=list)


ProgressCb = Optional[Callable[[int, str], None]]
CancelCb = Optional[Callable[[], bool]]


def _check_cancel(cancel_cb: CancelCb) -> None:
    if cancel_cb and cancel_cb():
        raise InterruptedError("Anulowano skanowanie")


def find_named_directories(
    vol: Fat32Volume,
    target_name: str,
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    deep_scan: bool = True,
) -> List[DirEntry]:
    """Szukaj katalogów o danej nazwie — najpierw drzewo, potem skan klastrów."""
    found: List[DirEntry] = []
    visited: Set[int] = set()

    def walk(cluster: int, depth: int = 0) -> None:
        _check_cancel(cancel_cb)
        if cluster in visited or depth > 64:
            return
        visited.add(cluster)
        try:
            entries = read_directory(vol, cluster)
        except Exception:
            return
        for e in entries:
            if e.is_dot or e.cluster < 2:
                continue
            if e.is_directory and names_match(e.name, target_name):
                found.append(e)
            if e.is_directory and not e.is_deleted:
                walk(e.cluster, depth + 1)

    walk(vol.bpb.root_cluster)
    if progress_cb:
        progress_cb(20, f"Przeszukano drzewo katalogów ({len(visited)} kat.)")

    if found and not deep_scan:
        return found

    # Głęboki skan: szukaj wpisów LFN/SFN z nazwą w klastrach katalogowych
    total = vol.total_clusters
    step = max(1, total // 100)
    for i, c in enumerate(range(2, total + 2)):
        _check_cancel(cancel_cb)
        if c in visited:
            continue
        if progress_cb and i % step == 0:
            pct = 20 + int(50 * i / max(total, 1))
            progress_cb(min(70, pct), f"Skan klastrów pod „{target_name}”…")
        try:
            data = vol.read_cluster(c)
        except Exception:
            continue
        if not looks_like_directory_cluster(data):
            continue
        ents = parse_entries_from_buffer(data, vol.cluster_offset(c), parent_cluster=c)
        for e in ents:
            if e.is_directory and names_match(e.name, target_name) and e.cluster >= 2:
                # Unikaj duplikatów po klastrze
                if not any(f.cluster == e.cluster for f in found):
                    found.append(e)
                    visited.add(e.cluster)
    return found


def build_tree_from_directory(
    vol: Fat32Volume,
    start_cluster: int,
    base_path: str = "",
    *,
    belongs_to_target: bool = True,
    max_depth: int = 32,
    visited: Optional[Set[int]] = None,
) -> List[RecoveredNode]:
    if visited is None:
        visited = set()
    if start_cluster in visited or max_depth < 0:
        return []
    visited.add(start_cluster)
    nodes: List[RecoveredNode] = []
    try:
        entries = read_directory(vol, start_cluster, include_deleted=True)
    except Exception:
        return []

    for e in entries:
        if e.is_dot or e.is_volume_label:
            continue
        if e.cluster < 2 and not e.is_directory:
            # Plik zerowy / uszkodzony wpis
            if e.size == 0 and not e.name:
                continue
        rel = f"{base_path}/{e.name}" if base_path else e.name
        conf = Confidence.HIGH
        evidence = [f"wpis katalogu @ klaster {start_cluster}"]
        if e.is_deleted:
            conf = Confidence.MEDIUM
            evidence.append("wpis oznaczony jako usunięty (0xE5)")
        chain = vol.follow_chain(e.cluster) if e.cluster >= 2 else []
        if e.cluster >= 2 and not chain:
            conf = Confidence.MEDIUM
            evidence.append("brak łańcucha FAT — odczyt pojedynczego klastra")
        elif e.cluster >= 2:
            expected = max(1, (e.size + vol.cluster_size - 1) // vol.cluster_size) if e.size else 1
            if not e.is_directory and e.size > 0 and len(chain) < expected:
                conf = Confidence.MEDIUM
                evidence.append(f"łańcuch FAT krótszy niż rozmiar ({len(chain)}/{expected})")
            else:
                evidence.append(f"łańcuch FAT: {len(chain)} klastrów")

        node = RecoveredNode(
            name=e.name,
            path=rel,
            cluster=e.cluster,
            size=e.size,
            is_directory=e.is_directory,
            is_deleted=e.is_deleted,
            confidence=conf,
            evidence=evidence,
            source_entry=e,
            belongs_to_target=belongs_to_target,
        )
        if e.is_directory and e.cluster >= 2:
            node.children = build_tree_from_directory(
                vol,
                e.cluster,
                rel,
                belongs_to_target=belongs_to_target,
                max_depth=max_depth - 1,
                visited=visited,
            )
        nodes.append(node)
    return nodes


def find_orphan_directories(
    vol: Fat32Volume,
    known_clusters: Set[int],
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
) -> List[RecoveredNode]:
    """Katalogi z . / .. niepowiązane ze znanym drzewem."""
    orphans: List[RecoveredNode] = []
    total = vol.total_clusters
    step = max(1, total // 50)
    for i, c in enumerate(range(2, total + 2)):
        _check_cancel(cancel_cb)
        if c in known_clusters:
            continue
        if progress_cb and i % step == 0:
            progress_cb(70 + int(25 * i / max(total, 1)), "Szukanie osieroconych katalogów…")
        try:
            data = vol.read_cluster(c)
        except Exception:
            continue
        if len(data) < 64:
            continue
        # Typowy katalog: pierwszy wpis "." wskazujący na siebie
        if data[0:1] != b"." or data[11] != 0x10:
            continue
        self_cl = int.from_bytes(data[26:28], "little") | (
            int.from_bytes(data[20:22], "little") << 16
        )
        if self_cl != c:
            continue
        ents = scan_cluster_as_directory(vol, c)
        # Nazwa z kontekstu rodzica niedostępna — użyj klastra
        name = f"orphan_dir_cluster_{c}"
        node = RecoveredNode(
            name=name,
            path=name,
            cluster=c,
            size=0,
            is_directory=True,
            is_deleted=False,
            confidence=Confidence.LOW,
            evidence=["wykryty lokalnie po wpisie „.”", "brak dowodu przynależności do katalogu docelowego"],
            belongs_to_target=False,
            children=build_tree_from_directory(
                vol, c, name, belongs_to_target=False, visited=set(known_clusters)
            ),
        )
        orphans.append(node)
        known_clusters.add(c)
    return orphans


def reconstruct_target(
    vol: Fat32Volume,
    target_name: str = "bhp szkoły",
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    deep_scan: bool = True,
) -> ReconstructionResult:
    messages: List[str] = []
    if progress_cb:
        progress_cb(5, f"Szukam katalogu „{target_name}”…")

    matches = find_named_directories(
        vol, target_name, progress_cb=progress_cb, cancel_cb=cancel_cb, deep_scan=deep_scan
    )
    nodes: List[RecoveredNode] = []
    target_cluster: Optional[int] = None
    known: Set[int] = {vol.bpb.root_cluster}

    if matches:
        # Wybierz najlepszy: preferuj nieusunięty z największą liczbą dzieci
        def score(e: DirEntry) -> tuple:
            kids = 0
            try:
                kids = len(read_directory(vol, e.cluster))
            except Exception:
                pass
            return (0 if e.is_deleted else 1, kids, -e.cluster)

        matches.sort(key=score, reverse=True)
        best = matches[0]
        target_cluster = best.cluster
        messages.append(
            f"Znaleziono katalog „{best.name}” @ klaster {best.cluster}"
            + (" (usunięty)" if best.is_deleted else "")
        )
        if progress_cb:
            progress_cb(75, "Buduję drzewo katalogu docelowego…")
        nodes = build_tree_from_directory(
            vol, best.cluster, "", belongs_to_target=True, visited=known
        )
        # Zbierz wszystkie klastry z drzewa
        def collect(nlist: List[RecoveredNode]) -> None:
            for n in nlist:
                if n.cluster >= 2:
                    known.add(n.cluster)
                collect(n.children)

        collect(nodes)
    else:
        messages.append(
            f"Nie znaleziono katalogu „{target_name}” po nazwie — "
            "odzyskane pliki trafią do Nieustalone_pochodzenie / Osierocone_katalogi."
        )

    orphans = find_orphan_directories(
        vol, known, progress_cb=progress_cb, cancel_cb=cancel_cb
    )
    # Orphany z nazwą zawierającą target — oznacz jako możliwe (MEDIUM), ale
    # belongs_to_target tylko przy names_match w dzieciach/ścieżce z dowodem
    for o in orphans:
        if name_contains(o.name, target_name):
            o.evidence.append("nazwa osieroconego katalogu zawiera cel")
            o.confidence = Confidence.MEDIUM

    if progress_cb:
        progress_cb(100, "Rekonstrukcja zakończona")

    return ReconstructionResult(
        target_name=target_name,
        target_cluster=target_cluster,
        target_found=target_cluster is not None,
        nodes=nodes,
        orphan_directories=orphans,
        scanned_clusters=vol.total_clusters,
        messages=messages,
    )


def flatten_nodes(nodes: List[RecoveredNode]) -> List[RecoveredNode]:
    out: List[RecoveredNode] = []

    def walk(n: RecoveredNode) -> None:
        out.append(n)
        for c in n.children:
            walk(c)

    for n in nodes:
        walk(n)
    return out
