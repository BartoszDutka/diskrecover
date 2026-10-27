"""Deduplikacja i cache analizy klastrów katalogowych FAT32."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Set, Tuple

from diskrecover.core.directory import DirEntry, DirectoryParseStats


class ClusterMark(IntEnum):
    """Stan klastra względem skanowania katalogów."""

    UNANALYZED = 0
    ANALYZED = 1  # przeanalizowany jako początek / samodzielny katalog
    CHAIN_MEMBER = 2  # część wcześniej przeanalizowanego łańcucha
    NEEDS_REANALYSIS = 3
    UNCERTAIN = 4


@dataclass
class DirectoryCacheEntry:
    start_cluster: int
    chain: Tuple[int, ...]
    entries: List[DirEntry]
    stats: DirectoryParseStats
    parent_from_dotdot: Optional[int] = None
    self_from_dot: Optional[int] = None


@dataclass
class ClusterTracker:
    """Śledzi unikalność odczytów i cache'uje sparsowane katalogi."""

    marks: Dict[int, ClusterMark] = field(default_factory=dict)
    analyzed_starts: Set[int] = field(default_factory=set)
    chain_members: Set[int] = field(default_factory=set)
    uncertain: Set[int] = field(default_factory=set)
    dir_cache: Dict[int, DirectoryCacheEntry] = field(default_factory=dict)
    # liczniki rzeczywistych operacji
    unique_clusters_analyzed: int = 0
    total_cluster_reads: int = 0
    reread_count: int = 0
    fat_chains_checked: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cycles_detected: int = 0
    _seen_reads: Set[int] = field(default_factory=set)

    def mark(self, cluster: int, state: ClusterMark) -> None:
        prev = self.marks.get(cluster, ClusterMark.UNANALYZED)
        if state == ClusterMark.ANALYZED and prev != ClusterMark.ANALYZED:
            if cluster not in self.analyzed_starts:
                self.analyzed_starts.add(cluster)
                self.unique_clusters_analyzed += 1
        if state == ClusterMark.CHAIN_MEMBER:
            self.chain_members.add(cluster)
        if state == ClusterMark.UNCERTAIN:
            self.uncertain.add(cluster)
        # Nie obniżaj ANALYZED do CHAIN_MEMBER
        if prev == ClusterMark.ANALYZED and state == ClusterMark.CHAIN_MEMBER:
            return
        self.marks[cluster] = state

    def mark_chain(self, start: int, chain: Iterable[int]) -> None:
        chain_list = list(chain)
        if not chain_list:
            return
        self.mark(start, ClusterMark.ANALYZED)
        for c in chain_list:
            if c == start:
                continue
            self.mark(c, ClusterMark.CHAIN_MEMBER)

    def is_skip_candidate(self, cluster: int) -> bool:
        """Pomiń kandydatów już przeanalizowanych lub będących środkiem łańcucha."""
        state = self.marks.get(cluster, ClusterMark.UNANALYZED)
        if state == ClusterMark.NEEDS_REANALYSIS:
            return False
        if cluster in self.analyzed_starts:
            return True
        if cluster in self.chain_members:
            return True
        return False

    def note_read(self, cluster: int) -> None:
        self.total_cluster_reads += 1
        if cluster in self._seen_reads:
            self.reread_count += 1
        else:
            self._seen_reads.add(cluster)

    def get_cached(self, start_cluster: int) -> Optional[DirectoryCacheEntry]:
        hit = self.dir_cache.get(start_cluster)
        if hit is not None:
            self.cache_hits += 1
            return hit
        self.cache_misses += 1
        return None

    def put_cache(self, entry: DirectoryCacheEntry) -> None:
        self.dir_cache[entry.start_cluster] = entry
        self.mark_chain(entry.start_cluster, entry.chain)

    def export_state(self) -> dict:
        return {
            "analyzed_starts": sorted(self.analyzed_starts),
            "chain_members": sorted(self.chain_members),
            "uncertain": sorted(self.uncertain),
            "marks": {str(k): int(v) for k, v in self.marks.items()},
            "unique_clusters_analyzed": self.unique_clusters_analyzed,
            "total_cluster_reads": self.total_cluster_reads,
            "reread_count": self.reread_count,
            "fat_chains_checked": self.fat_chains_checked,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cycles_detected": self.cycles_detected,
            "cached_dirs": sorted(self.dir_cache.keys()),
        }

    def import_state(self, data: dict) -> None:
        self.analyzed_starts = set(data.get("analyzed_starts") or [])
        self.chain_members = set(data.get("chain_members") or [])
        self.uncertain = set(data.get("uncertain") or [])
        marks = data.get("marks") or {}
        self.marks = {int(k): ClusterMark(int(v)) for k, v in marks.items()}
        self.unique_clusters_analyzed = int(data.get("unique_clusters_analyzed") or 0)
        self.total_cluster_reads = int(data.get("total_cluster_reads") or 0)
        self.reread_count = int(data.get("reread_count") or 0)
        self.fat_chains_checked = int(data.get("fat_chains_checked") or 0)
        self.cache_hits = int(data.get("cache_hits") or 0)
        self.cache_misses = int(data.get("cache_misses") or 0)
        self.cycles_detected = int(data.get("cycles_detected") or 0)
        self._seen_reads = set(self.analyzed_starts) | set(self.chain_members)
