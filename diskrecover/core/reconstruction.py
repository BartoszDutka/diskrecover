"""Rekonstrukcja uszkodzonych katalogów (np. „bhp szkoły”)."""

from __future__ import annotations

from dataclasses import dataclass, field
import cProfile
import io
import pstats
import time
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

from diskrecover.core.cluster_tracker import ClusterMark, ClusterTracker
from diskrecover.core.directory import (
    DirEntry,
    looks_like_directory_cluster,
    name_contains,
    names_match,
    parent_cluster_from_dotdot,
    parse_entries_from_buffer,
    read_directory,
    scan_cluster_as_directory,
    self_cluster_from_dot,
)
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.profiler import Profiler


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
    is_candidate: bool = False
    parent_cluster: Optional[int] = None
    link_confidence: Optional[Confidence] = None


@dataclass
class ReconstructionResult:
    target_name: str
    target_cluster: Optional[int]
    target_found: bool
    target_directory_intact: bool
    nodes: List[RecoveredNode]
    orphan_directories: List[RecoveredNode]
    target_linked_orphans: List[RecoveredNode]
    scanned_clusters: int
    messages: List[str] = field(default_factory=list)
    tracker: Optional[ClusterTracker] = None
    stats: Dict[str, int] = field(default_factory=dict)
    profile_report: str = ""
    orphan_candidates: List[int] = field(default_factory=list)
    already_checked: List[int] = field(default_factory=list)


@dataclass
class CandidateBudget:
    max_seconds: float = 300.0
    max_reads: int = 100_000
    max_clusters: int = 100_000
    max_records: int = 1_000_000
    started_at: float = field(default_factory=time.monotonic)
    reads: int = 0
    clusters: int = 0
    records: int = 0
    depth: int = 0

    def check(self) -> None:
        if time.monotonic() - self.started_at > self.max_seconds:
            raise CandidateDeferred("time budget exceeded")
        if self.reads > self.max_reads:
            raise CandidateDeferred("read budget exceeded")
        if self.clusters > self.max_clusters:
            raise CandidateDeferred("cluster budget exceeded")
        if self.records > self.max_records:
            raise CandidateDeferred("record budget exceeded")

    def consume_cluster(self) -> None:
        self.clusters += 1
        self.check()


class CandidateDeferred(RuntimeError):
    """A candidate exceeded its budget and remains eligible for retry."""


ProgressCb = Optional[Callable[[int, str], None]]
CancelCb = Optional[Callable[[], bool]]
PauseCb = Optional[Callable[[], None]]
TelemetryCb = Optional[Callable[..., None]]


def _check_cancel(cancel_cb: CancelCb) -> None:
    if cancel_cb and cancel_cb():
        raise InterruptedError("Anulowano skanowanie")


def _wait_if_paused(pause_cb: PauseCb) -> None:
    if pause_cb:
        pause_cb()


def _confirmed_directory_cluster(
    vol: Fat32Volume,
    cluster: int,
    *,
    data: Optional[bytes] = None,
    tracker: Optional[ClusterTracker] = None,
) -> bool:
    if cluster < 2 or cluster > vol.total_clusters + 1:
        return False
    if tracker is not None:
        tracker.fat_chains_checked += 1
    chain = vol.follow_chain(cluster)
    if not chain or chain[0] != cluster:
        return False
    if vol.bpb.num_fats >= 2 and (
        vol.fat_entry_copy(0, cluster) != vol.fat_entry_copy(1, cluster)
    ):
        return False
    try:
        if data is None:
            if tracker is not None:
                tracker.note_read(cluster)
            data = vol.read_cluster(cluster)
    except Exception:
        return False
    return looks_like_directory_cluster(
        data,
        cluster=cluster,
        max_cluster=vol.total_clusters + 1,
    )


def find_named_directories(
    vol: Fat32Volume,
    target_name: str,
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    pause_cb: PauseCb = None,
    deep_scan: bool = True,
    telemetry_cb: TelemetryCb = None,
    orphan_candidates: Optional[List[int]] = None,
    tracker: Optional[ClusterTracker] = None,
    profiler: Optional[Profiler] = None,
    damaged_name_hits: Optional[List[DirEntry]] = None,
) -> List[DirEntry]:
    """Szukaj katalogów o danej nazwie — najpierw drzewo, potem skan klastrów."""
    found: List[DirEntry] = []
    visited: Set[int] = set()
    last_tree_report = 0
    tracker = tracker or ClusterTracker()
    profiler = profiler or Profiler(enabled=False)

    def walk(cluster: int, depth: int = 0) -> None:
        nonlocal last_tree_report
        _check_cancel(cancel_cb)
        _wait_if_paused(pause_cb)
        if cluster in visited or depth > 64:
            return
        visited.add(cluster)
        if telemetry_cb:
            telemetry_cb(
                clusters_checked=len(visited),
                unique_clusters_analyzed=tracker.unique_clusters_analyzed,
                cluster=cluster,
                operation=f"Wyszukiwanie katalogu: klaster {cluster}",
            )
        if progress_cb and len(visited) - last_tree_report >= 4096:
            last_tree_report = len(visited)
            progress_cb(
                -1,
                f"Szukam katalogu „{target_name}”… "
                f"(odwiedzono {len(visited):,} klastrów)",
            )
        try:
            with profiler.span("read_directory"):
                entries = read_directory(
                    vol,
                    cluster,
                    visited_clusters=visited,
                    tracker=tracker,
                )
        except Exception:
            return
        for e in entries:
            if e.is_dot or e.cluster < 2:
                continue
            if e.is_directory and names_match(e.name, target_name):
                with profiler.span("confirm_directory"):
                    ok = _confirmed_directory_cluster(
                        vol, e.cluster, tracker=tracker
                    )
                if ok:
                    found.append(e)
                else:
                    if damaged_name_hits is not None:
                        damaged_name_hits.append(e)
                    if telemetry_cb:
                        telemetry_cb(
                            candidates=1,
                            operation=(
                                f"Kandydat uszkodzonego celu {e.name!r}: "
                                f"klaster {e.cluster} nie jest spójnym katalogiem"
                            ),
                        )
            if e.is_directory and not e.is_deleted:
                if _confirmed_directory_cluster(vol, e.cluster, tracker=tracker):
                    walk(e.cluster, depth + 1)

    with profiler.span("walk_tree"):
        walk(vol.bpb.root_cluster)
    if progress_cb:
        progress_cb(20, f"Przeszukano drzewo katalogów ({len(visited)} kat.)")

    if found and not deep_scan:
        return found

    # Głęboki skan: zbieraj kandydatów orphan + nazwy celu
    total = vol.total_clusters
    step = max(1, total // 100)
    candidate_set: Set[int] = set(orphan_candidates or [])

    for i, c in enumerate(range(2, total + 2)):
        _check_cancel(cancel_cb)
        _wait_if_paused(pause_cb)
        if progress_cb and i % step == 0:
            pct = 20 + int(50 * i / max(total, 1))
            progress_cb(min(70, pct), f"Skan klastrów pod „{target_name}”…")
        if c in visited:
            # nadal może być kandydatem jeśli wygląda na dir z . /
            pass
        if telemetry_cb and i % 4096 == 0:
            telemetry_cb(
                clusters_checked=i + 1,
                unique_clusters_analyzed=tracker.unique_clusters_analyzed,
                cluster=c,
                cluster_reads=tracker.total_cluster_reads,
                rereads=tracker.reread_count,
                stage_pct=100.0 * i / max(total, 1),
                operation=f"Głębokie wyszukiwanie katalogu: klaster {c}",
            )
        try:
            with profiler.span("read_cluster_deep"):
                tracker.note_read(c)
                data = vol.read_cluster(c)
        except Exception:
            continue
        if not looks_like_directory_cluster(
            data,
            cluster=c,
            max_cluster=vol.total_clusters + 1,
        ):
            continue
        # Kandydat orphan (spójne . / ..)
        if orphan_candidates is not None and c not in candidate_set:
            candidate_set.add(c)
            orphan_candidates.append(c)
            if telemetry_cb:
                telemetry_cb(
                    candidates=1,
                    orphan_candidates=1,
                    operation=f"Kandydat katalogu: klaster {c}",
                )
        with profiler.span("parse_deep_cluster"):
            ents = parse_entries_from_buffer(
                data,
                vol.cluster_offset(c),
                parent_cluster=c,
                max_cluster=vol.total_clusters + 1,
            )
        for e in ents:
            if e.is_directory and names_match(e.name, target_name) and e.cluster >= 2:
                ok = _confirmed_directory_cluster(
                    vol, e.cluster, tracker=tracker
                )
                if ok and not any(f.cluster == e.cluster for f in found):
                    found.append(e)
                    visited.add(e.cluster)
                elif not ok and damaged_name_hits is not None:
                    if not any(d.cluster == e.cluster for d in damaged_name_hits):
                        damaged_name_hits.append(e)
    return found


def build_tree_from_directory(
    vol: Fat32Volume,
    start_cluster: int,
    base_path: str = "",
    *,
    belongs_to_target: bool = True,
    max_depth: int = 32,
    visited: Optional[Set[int]] = None,
    tracker: Optional[ClusterTracker] = None,
    emit_activity: bool = True,
    budget: Optional[CandidateBudget] = None,
    node_cb: Optional[Callable[[RecoveredNode], None]] = None,
) -> List[RecoveredNode]:
    if visited is None:
        visited = set()
    if start_cluster in visited or max_depth < 0:
        return []
    visited.add(start_cluster)
    if budget:
        budget.consume_cluster()
    nodes: List[RecoveredNode] = []
    try:
        entries = read_directory(
            vol,
            start_cluster,
            include_deleted=True,
            visited_clusters=visited,
            tracker=tracker,
            emit_activity=emit_activity,
        )
    except Exception:
        return []

    for e in entries:
        if budget:
            budget.records += 1
            budget.check()
        if e.is_dot or e.is_volume_label:
            continue
        if e.cluster < 2 and not e.is_directory:
            if e.size == 0 and not e.name:
                continue
        rel = f"{base_path}/{e.name}" if base_path else e.name
        conf = Confidence.HIGH
        evidence = [f"wpis katalogu @ klaster {start_cluster}"]
        if e.is_deleted:
            conf = Confidence.MEDIUM
            evidence.append("wpis oznaczony jako usunięty (0xE5)")
        chain = _follow_cached_chain(vol, e.cluster, tracker, budget)
        if e.cluster >= 2 and not chain:
            conf = Confidence.MEDIUM
            evidence.append("brak łańcucha FAT — odczyt pojedynczego klastra")
        elif e.cluster >= 2:
            expected = (
                max(1, (e.size + vol.cluster_size - 1) // vol.cluster_size)
                if e.size
                else 1
            )
            if not e.is_directory and e.size > 0 and len(chain) < expected:
                conf = Confidence.MEDIUM
                evidence.append(
                    f"łańcuch FAT krótszy niż rozmiar ({len(chain)}/{expected})"
                )
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
            parent_cluster=start_cluster,
            link_confidence=Confidence.HIGH if belongs_to_target else None,
        )
        if e.is_directory and e.cluster >= 2:
            if e.cluster in visited:
                node.evidence.append("pominięto rekurencję — klaster już odwiedzony")
            else:
                node.children = build_tree_from_directory(
                    vol,
                    e.cluster,
                    rel,
                    belongs_to_target=belongs_to_target,
                    max_depth=max_depth - 1,
                    visited=visited,
                    tracker=tracker,
                    emit_activity=emit_activity,
                    budget=budget,
                    node_cb=node_cb,
                )
        nodes.append(node)
        if node_cb:
            node_cb(node)
    return nodes


def _count_files(nodes: List[RecoveredNode]) -> int:
    n = 0
    for node in nodes:
        if node.is_directory:
            n += _count_files(node.children)
        else:
            n += 1
    return n


def _follow_cached_chain(
    vol: Fat32Volume,
    cluster: int,
    tracker: Optional[ClusterTracker],
    budget: Optional[CandidateBudget],
) -> List[int]:
    if cluster < 2:
        return []
    if tracker is not None:
        cached = tracker.get_chain(cluster)
        if cached is not None:
            return list(cached)
    chain = vol.follow_chain(
        cluster,
        check_cb=budget.consume_cluster if budget else None,
    )
    if tracker is not None:
        tracker.put_chain(cluster, chain)
    return chain


def find_orphan_directories(
    vol: Fat32Volume,
    known_clusters: Set[int],
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    pause_cb: PauseCb = None,
    telemetry_cb: TelemetryCb = None,
    candidate_clusters: Optional[List[int]] = None,
    tracker: Optional[ClusterTracker] = None,
    target_cluster: Optional[int] = None,
    target_name: str = "bhp szkoły",
    profiler: Optional[Profiler] = None,
    already_checked: Optional[Set[int]] = None,
    candidate_cb: Optional[Callable[[str, int, str, dict], None]] = None,
    node_cb: Optional[Callable[[RecoveredNode], None]] = None,
    budget_factory: Optional[Callable[[], CandidateBudget]] = None,
    retry_deferred: bool = False,
) -> Tuple[List[RecoveredNode], List[RecoveredNode]]:
    """
    Katalogi z . / .. niepowiązane ze znanym drzewem.

    Zwraca (orphans_unlinked, orphans_linked_to_target).
    Każdy kandydat jest walidowany co najwyżej raz; drzewo budowane
    ze wspólnym visited/cache — bez ponownego chodzenia po łańcuchach.
    """
    tracker = tracker or ClusterTracker()
    profiler = profiler or Profiler(enabled=False)
    already_checked = already_checked if already_checked is not None else set()

    # Oznacz znane klastry jako już przeanalizowane (bez ponownego budowania)
    for kc in known_clusters:
        if not tracker.is_skip_candidate(kc):
            tracker.mark_chain(kc, [kc])

    if candidate_clusters is not None:
        # Unikalna, posortowana lista kandydatów
        candidates = sorted({int(c) for c in candidate_clusters if c >= 2})
    else:
        candidates = list(range(2, vol.total_clusters + 2))

    total = len(candidates)
    orphans: List[RecoveredNode] = []
    linked: List[RecoveredNode] = []
    # Wspólny zbiór odwiedzonych startów katalogów w tej fazie
    shared_visited: Set[int] = set(known_clusters)

    def _telem(**kw):
        if telemetry_cb:
            clean = {k: v for k, v in kw.items() if v is not None}
            if clean:
                telemetry_cb(**clean)

    _telem(
        candidates_total=total,
        candidates_remaining=total,
        stage_pct=0.0 if total else 100.0,
        set_orphan_candidates=total,
        operation=f"Skanowanie osieroconych: 0/{total} kandydatów",
    )

    checked = 0
    rejected = 0
    confirmed = 0
    linked_dirs = 0
    linked_files = 0

    for i, c in enumerate(candidates):
        _check_cancel(cancel_cb)
        _wait_if_paused(pause_cb)

        remaining = total - i
        stage_pct = 100.0 * i / max(total, 1)
        _telem(
            cluster=c,
            candidates_remaining=remaining,
            candidates_total=total,
            stage_pct=stage_pct,
            clusters_checked=tracker.unique_clusters_analyzed,
            unique_clusters_analyzed=tracker.unique_clusters_analyzed,
            cluster_reads=tracker.total_cluster_reads,
            rereads=tracker.reread_count,
            fat_chains_checked=tracker.fat_chains_checked,
            operation=f"Skanowanie osieroconych katalogów: kandydat {i+1}/{total} (klaster {c})",
        )
        if progress_cb:
            # Postęp etapu = kandydaci, nie fikcyjny % całego skanu
            progress_cb(
                int(stage_pct) if total else 100,
                f"Szukanie osieroconych katalogów… {i}/{total} "
                f"(pozostało {remaining})",
            )

        candidate_id = f"cluster:{c}"
        if candidate_cb and c not in already_checked:
            candidate_cb(candidate_id, c, "PENDING", {"sequence_no": i})

        if c in known_clusters or c in already_checked:
            checked += 1
            already_checked.add(c)
            if candidate_cb:
                candidate_cb(candidate_id, c, "COMPLETED", {
                    "sequence_no": i, "reason": "already checked",
                    "finished_at": time.time(), "progress": 100,
                })
            continue

        budget = budget_factory() if budget_factory else None
        started = time.time()
        if candidate_cb:
            candidate_cb(candidate_id, c, "RUNNING", {
                "sequence_no": i, "started_at": started
            })

        if tracker.is_skip_candidate(c):
            # Już część przeanalizowanego łańcucha / katalogu — nie buduj ponownie
            checked += 1
            rejected += 1
            already_checked.add(c)
            if candidate_cb:
                candidate_cb(candidate_id, c, "COMPLETED", {
                    "sequence_no": i, "reason": "already analyzed",
                    "finished_at": time.time(), "progress": 100
                })
            _telem(
                orphan_candidates_checked=1,
                orphan_candidates_rejected=1,
            )
            continue

        try:
            if budget:
                budget.reads += 1
                budget.check()
            with profiler.span("orphan_read_cluster"):
                tracker.note_read(c)
                data = vol.read_cluster(c)
        except CandidateDeferred as exc:
            if candidate_cb:
                candidate_cb(candidate_id, c, "DEFERRED", {
                    "sequence_no": i, "reason": str(exc),
                    "finished_at": time.time(),
                    "reads": budget.reads if budget else 0,
                    "clusters": budget.clusters if budget else 0,
                    "records": budget.records if budget else 0,
                    "progress": 0,
                })
            # The candidate is intentionally not added to already_checked.
            continue
        except Exception:
            if candidate_cb:
                candidate_cb(candidate_id, c, "FAILED", {
                    "sequence_no": i, "reason": "odczyt klastra nieudany",
                    "finished_at": time.time(),
                })
            checked += 1
            rejected += 1
            already_checked.add(c)
            _telem(orphan_candidates_checked=1, orphan_candidates_rejected=1)
            continue

        if not looks_like_directory_cluster(
            data,
            cluster=c,
            max_cluster=vol.total_clusters + 1,
        ):
            checked += 1
            rejected += 1
            already_checked.add(c)
            tracker.mark(c, ClusterMark.UNCERTAIN)
            _telem(orphan_candidates_checked=1, orphan_candidates_rejected=1)
            continue

        try:
            with profiler.span("orphan_follow_chain"):
                tracker.fat_chains_checked += 1
                chain = _follow_cached_chain(vol, c, tracker, budget)
        except CandidateDeferred as exc:
            if candidate_cb:
                candidate_cb(candidate_id, c, "DEFERRED", {
                    "sequence_no": i, "reason": str(exc),
                    "finished_at": time.time(),
                    "reads": budget.reads if budget else 0,
                    "clusters": budget.clusters if budget else 0,
                    "records": budget.records if budget else 0,
                })
            continue
        if not chain or chain[0] != c:
            checked += 1
            rejected += 1
            already_checked.add(c)
            _telem(orphan_candidates_checked=1, orphan_candidates_rejected=1)
            continue
        if vol.bpb.num_fats >= 2 and (
            vol.fat_entry_copy(0, c) != vol.fat_entry_copy(1, c)
        ):
            checked += 1
            rejected += 1
            already_checked.add(c)
            _telem(orphan_candidates_checked=1, orphan_candidates_rejected=1)
            continue

        # Parsuj raz z już odczytanego bufora — bez ponownego read_cluster
        with profiler.span("orphan_parse"):
            ents = scan_cluster_as_directory(vol, c, data=data, tracker=tracker)
        if budget:
            budget.records += len(ents)
            try:
                budget.check()
            except CandidateDeferred as exc:
                if candidate_cb:
                    candidate_cb(candidate_id, c, "DEFERRED", {
                        "sequence_no": i, "reason": str(exc),
                        "finished_at": time.time(),
                        "reads": budget.reads, "clusters": budget.clusters,
                        "records": budget.records, "progress": 0,
                    })
                continue
        parent = parent_cluster_from_dotdot(ents)
        self_cl = self_cluster_from_dot(ents)

        belongs = False
        link_conf = Confidence.LOW
        evidence = [
            "kandydat wykryty po spójnych wpisach „.” i „..”",
            f"łańcuch FAT: {len(chain)} klastrów",
        ]
        if self_cl == c:
            evidence.append(f"wpis „.” wskazuje siebie ({c})")
        if parent is not None:
            evidence.append(f"wpis „..” → klaster {parent}")

        if target_cluster is not None and parent == target_cluster:
            belongs = True
            link_conf = Confidence.HIGH
            evidence.append(
                f"„..” wskazuje katalog docelowy „{target_name}” "
                f"(klaster {target_cluster})"
            )
            # Dodatkowo FAT1/FAT2 już sprawdzone wyżej
            evidence.append("FAT1 i FAT2 zgodne dla klastra startowego")

        name = f"orphan_dir_cluster_{c}"
        # Buduj drzewo tylko jeśli start nie był jeszcze odwiedzony
        children: List[RecoveredNode] = []
        if c not in shared_visited:
            try:
                with profiler.span("orphan_build_tree"):
                    children = build_tree_from_directory(
                        vol,
                        c,
                        name,
                        belongs_to_target=belongs,
                        visited=shared_visited,
                        tracker=tracker,
                        emit_activity=True,
                        budget=budget,
                        node_cb=node_cb,
                    )
            except CandidateDeferred as exc:
                if candidate_cb:
                    candidate_cb(candidate_id, c, "DEFERRED", {
                        "sequence_no": i, "reason": str(exc),
                        "finished_at": time.time(),
                        "reads": budget.reads if budget else 0,
                        "clusters": budget.clusters if budget else 0,
                        "records": budget.records if budget else 0,
                        "progress": 0,
                    })
                continue
        else:
            # Cache / wcześniejsza wizyta — użyj cache bez rekurencji
            cached = tracker.get_cached(c)
            if cached is not None:
                evidence.append("struktura z cache (bez ponownego odczytu łańcucha)")

        tracker.mark_chain(c, chain)
        already_checked.add(c)
        known_clusters.add(c)
        checked += 1
        confirmed += 1

        file_count = _count_files(children)
        node = RecoveredNode(
            name=name,
            path=name,
            cluster=c,
            size=0,
            is_directory=True,
            is_deleted=False,
            confidence=link_conf if belongs else Confidence.LOW,
            evidence=evidence
            + (
                []
                if belongs
                else ["brak dowodu przynależności do katalogu docelowego"]
            ),
            belongs_to_target=belongs,
            is_candidate=True,
            children=children,
            parent_cluster=parent,
            link_confidence=link_conf if belongs else None,
        )
        if node_cb:
            node_cb(node)

        if belongs:
            linked.append(node)
            linked_dirs += 1
            linked_files += file_count
            _telem(
                orphan_candidates_checked=1,
                orphan_directories_confirmed=1,
                target_linked_subdirs=1,
                target_linked_files=file_count,
            )
        else:
            orphans.append(node)
            _telem(
                orphan_candidates_checked=1,
                orphan_directories_confirmed=1,
            )
        if candidate_cb:
            candidate_cb(candidate_id, c, "COMPLETED", {
                "sequence_no": i, "finished_at": time.time(),
                "progress": 100, "reads": budget.reads if budget else 1,
                "clusters": budget.clusters if budget else 1,
                "records": budget.records if budget else len(ents),
            })
        continue

        if telemetry_cb and hasattr(telemetry_cb, "__call__"):
            # prędkość
            pass

    _telem(
        candidates_remaining=0,
        stage_pct=100.0,
        set_orphan_candidates=total,
        operation=(
            f"Skończono osierocone: potwierdzono={confirmed}, "
            f"odrzucono={rejected}, powiązane z celem={linked_dirs}"
        ),
    )
    if progress_cb:
        progress_cb(
            100,
            f"Osierocone katalogi: {confirmed} potwierdzonych, "
            f"{linked_dirs} powiązanych z „{target_name}”",
        )
    return orphans, linked


def diagnose_orphan_candidate(
    vol: Fat32Volume,
    cluster: int,
    *,
    target_cluster: Optional[int] = None,
    target_name: str = "bhp szkoły",
    budget: Optional[CandidateBudget] = None,
) -> dict:
    """Analyze exactly one orphan candidate without scanning the volume."""
    tracker = ClusterTracker()
    profiler = Profiler(enabled=True)
    active_budget = budget or CandidateBudget()
    events: List[dict] = []
    seen_nodes: List[tuple[int, bool]] = []
    started = time.monotonic()

    def candidate_cb(candidate_id: str, candidate_cluster: int, status: str, data: dict) -> None:
        events.append({
            "candidate_id": candidate_id,
            "cluster": candidate_cluster,
            "status": status,
            **data,
        })

    def node_cb(node: RecoveredNode) -> None:
        if node.is_directory:
            seen_nodes.append((node.cluster, True))

    profile = cProfile.Profile()
    profile.enable()
    try:
        orphan, linked = find_orphan_directories(
            vol,
            set(),
            candidate_clusters=[cluster],
            target_cluster=target_cluster,
            target_name=target_name,
            tracker=tracker,
            profiler=profiler,
            candidate_cb=candidate_cb,
            node_cb=node_cb,
            budget_factory=lambda: active_budget,
        )
    finally:
        profile.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
    stats.print_stats(30)
    repeated = sorted(
        cluster_id
        for cluster_id, _ in set(seen_nodes)
        if seen_nodes.count((cluster_id, True)) > 1
    )
    return {
        "cluster": cluster,
        "elapsed_seconds": time.monotonic() - started,
        "events": events,
        "function_profile": stream.getvalue(),
        "profiler": profiler.as_dict(),
        "unique_clusters": tracker.unique_clusters_analyzed,
        "cluster_reads": tracker.total_cluster_reads,
        "rereads": tracker.reread_count,
        "records": active_budget.records,
        "budget": {
            "reads": active_budget.reads,
            "clusters": active_budget.clusters,
            "records": active_budget.records,
            "max_seconds": active_budget.max_seconds,
            "max_reads": active_budget.max_reads,
            "max_clusters": active_budget.max_clusters,
            "max_records": active_budget.max_records,
        },
        "recursive_depth": max(
            (len(node.path.split("/")) for node in linked + orphan),
            default=0,
        ),
        "repeated_directory_clusters": repeated,
        "result": {
            "orphan": len(orphan),
            "linked": len(linked),
        },
    }


def reconstruct_target(
    vol: Fat32Volume,
    target_name: str = "bhp szkoły",
    *,
    progress_cb: ProgressCb = None,
    cancel_cb: CancelCb = None,
    pause_cb: PauseCb = None,
    deep_scan: bool = True,
    telemetry_cb: TelemetryCb = None,
    tracker: Optional[ClusterTracker] = None,
    profiler: Optional[Profiler] = None,
    resume_state: Optional[dict] = None,
    candidate_cb: Optional[Callable[[str, int, str, dict], None]] = None,
    node_cb: Optional[Callable[[RecoveredNode], None]] = None,
    budget_factory: Optional[Callable[[], CandidateBudget]] = None,
    retry_deferred: bool = False,
) -> ReconstructionResult:
    messages: List[str] = []
    orphan_candidates: List[int] = []
    damaged_hits: List[DirEntry] = []
    tracker = tracker or ClusterTracker()
    profiler = profiler or Profiler(enabled=True)
    already_checked: Set[int] = set()

    if resume_state:
        orphan_candidates = list(resume_state.get("orphan_candidates") or [])
        already_checked = set(resume_state.get("already_checked") or [])
        tracker.import_state(resume_state.get("tracker") or {})

    if progress_cb:
        progress_cb(5, f"Szukam katalogu „{target_name}”…")
    if telemetry_cb:
        telemetry_cb(stage="Wyszukiwanie folderu docelowego")

    matches = find_named_directories(
        vol,
        target_name,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        pause_cb=pause_cb,
        deep_scan=deep_scan,
        telemetry_cb=telemetry_cb,
        orphan_candidates=orphan_candidates,
        tracker=tracker,
        profiler=profiler,
        damaged_name_hits=damaged_hits,
    )
    nodes: List[RecoveredNode] = []
    target_cluster: Optional[int] = None
    target_intact = False
    known: Set[int] = {vol.bpb.root_cluster}

    if matches:
        def score(e: DirEntry) -> tuple:
            kids = 0
            try:
                kids = len(
                    read_directory(vol, e.cluster, tracker=tracker, emit_activity=False)
                )
            except Exception:
                pass
            return (0 if e.is_deleted else 1, kids, -e.cluster)

        matches.sort(key=score, reverse=True)
        best = matches[0]
        target_cluster = best.cluster
        target_intact = True
        messages.append(
            f"Znaleziono katalog „{best.name}” @ klaster {best.cluster}"
            + (" (usunięty)" if best.is_deleted else "")
        )
        if progress_cb:
            progress_cb(75, "Buduję drzewo katalogu docelowego…")
        nodes = build_tree_from_directory(
            vol,
            best.cluster,
            "",
            belongs_to_target=True,
            visited=known,
            tracker=tracker,
            node_cb=node_cb,
        )

        def collect(nlist: List[RecoveredNode]) -> None:
            for n in nlist:
                if n.cluster >= 2:
                    known.add(n.cluster)
                collect(n.children)

        collect(nodes)
    elif damaged_hits:
        # Wpis nazwy istnieje, ale klaster katalogu jest uszkodzony —
        # zachowaj numer klastra do wyszukiwania dzieci po „..”.
        damaged_hits.sort(key=lambda e: (0 if e.is_deleted else 1, -e.cluster), reverse=True)
        best = damaged_hits[0]
        target_cluster = best.cluster
        target_intact = False
        messages.append(
            f"Znaleziono wpis „{best.name}” @ klaster {best.cluster}, "
            "ale zawartość klastra jest uszkodzona / bez struktury katalogu. "
            "Szukam podfolderów po wpisach „..” wskazujących ten klaster."
        )
        known.add(best.cluster)
    else:
        messages.append(
            f"Nie znaleziono katalogu „{target_name}” po nazwie — "
            "odzyskane pliki trafią do Nieustalone_pochodzenie / Osierocone_katalogi."
        )

    if telemetry_cb:
        telemetry_cb(stage="Skanowanie osieroconych katalogów")

    orphans, linked = find_orphan_directories(
        vol,
        known,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        pause_cb=pause_cb,
        telemetry_cb=telemetry_cb,
        candidate_clusters=orphan_candidates if deep_scan else None,
        tracker=tracker,
        target_cluster=target_cluster,
        target_name=target_name,
        profiler=profiler,
        already_checked=already_checked,
        candidate_cb=candidate_cb,
        node_cb=node_cb,
        budget_factory=budget_factory,
        retry_deferred=retry_deferred,
    )

    # Orphany z nazwą zawierającą target — MEDIUM, bez automatycznego belongs
    for o in orphans:
        if name_contains(o.name, target_name):
            o.evidence.append("nazwa osieroconego katalogu zawiera cel")
            o.confidence = Confidence.MEDIUM

    if linked:
        messages.append(
            f"Podfoldery powiązane z „{target_name}” (przez „..” → {target_cluster}): "
            f"{len(linked)}"
        )
        # Dołącz powiązane jako dzieci celu w wyniku
        nodes.extend(linked)
    else:
        if target_cluster is not None:
            messages.append(
                f"Nie odnaleziono podfolderów z wpisem „..” wskazującym "
                f"klaster {target_cluster} („{target_name}”)."
            )

    if progress_cb:
        progress_cb(100, "Rekonstrukcja zakończona")

    stats = {
        "unique_clusters_analyzed": tracker.unique_clusters_analyzed,
        "cluster_reads": tracker.total_cluster_reads,
        "rereads": tracker.reread_count,
        "fat_chains_checked": tracker.fat_chains_checked,
        "orphan_candidates": len(orphan_candidates) if deep_scan else vol.total_clusters,
        "orphan_confirmed": len(orphans) + len(linked),
        "target_linked_subdirs": len(linked),
        "target_linked_files": sum(_count_files(x.children) for x in linked),
        "cache_hits": tracker.cache_hits,
        "cache_misses": tracker.cache_misses,
    }

    return ReconstructionResult(
        target_name=target_name,
        target_cluster=target_cluster,
        target_found=target_cluster is not None,
        target_directory_intact=target_intact,
        nodes=nodes,
        orphan_directories=orphans,
        target_linked_orphans=linked,
        scanned_clusters=tracker.unique_clusters_analyzed,
        messages=messages,
        tracker=tracker,
        stats=stats,
        profile_report=profiler.format_report(),
        orphan_candidates=list(orphan_candidates),
        already_checked=sorted(already_checked),
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
