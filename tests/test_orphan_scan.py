"""Testy deduplikacji, liczników i powiązań z klastrem docelowym."""

from pathlib import Path

from diskrecover.core.cluster_tracker import ClusterMark, ClusterTracker
from diskrecover.core.directory import looks_like_directory_cluster, parent_cluster_from_dotdot
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.reconstruction import (
    CandidateBudget,
    find_orphan_directories,
    flatten_nodes,
    reconstruct_target,
)


def test_cluster_tracker_dedup():
    t = ClusterTracker()
    t.note_read(5)
    t.note_read(5)
    assert t.total_cluster_reads == 2
    assert t.reread_count == 1
    t.mark_chain(10, [10, 11, 12])
    assert t.is_skip_candidate(10)
    assert t.is_skip_candidate(11)
    assert not t.is_skip_candidate(99)
    t.mark(99, ClusterMark.NEEDS_REANALYSIS)
    assert not t.is_skip_candidate(99)


def test_orphan_parent_dotdot_points_to_target(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        data = vol.read_cluster(11)
        assert looks_like_directory_cluster(
            data, cluster=11, max_cluster=vol.total_clusters + 1
        )
        from diskrecover.core.directory import scan_cluster_as_directory

        ents = scan_cluster_as_directory(vol, 11, data=data)
        assert parent_cluster_from_dotdot(ents) == 3


def test_target_linked_subdirs_via_dotdot(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        result = reconstruct_target(vol, "bhp szkoły", deep_scan=True)
        assert result.target_found
        assert result.target_cluster == 3
        assert result.target_linked_orphans, "Oczekiwano podfolderu z .. → 3"
        linked = result.target_linked_orphans[0]
        assert linked.belongs_to_target is True
        assert linked.parent_cluster == 3
        files = [n for n in flatten_nodes([linked]) if not n.is_directory]
        assert any("BHPCHILD" in n.name.upper() for n in files)
        assert result.stats["target_linked_subdirs"] >= 1


def test_orphan_scan_no_redundant_tree_walks(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        tracker = ClusterTracker()
        # Zbierz kandydatów jak deep scan
        candidates = []
        for c in range(2, vol.total_clusters + 2):
            try:
                data = vol.read_cluster(c)
            except Exception:
                continue
            if looks_like_directory_cluster(
                data, cluster=c, max_cluster=vol.total_clusters + 1
            ):
                candidates.append(c)
        known = {vol.bpb.root_cluster, 3, 4, 5, 6, 7}
        reads_before = tracker.total_cluster_reads
        orphans, linked = find_orphan_directories(
            vol,
            set(known),
            candidate_clusters=candidates,
            tracker=tracker,
            target_cluster=3,
        )
        # Drugie uruchomienie z tym samym trackerem nie powinno mnożyć odczytów drzew
        reads_mid = tracker.total_cluster_reads
        orphans2, linked2 = find_orphan_directories(
            vol,
            set(known) | {o.cluster for o in orphans} | {o.cluster for o in linked},
            candidate_clusters=candidates,
            tracker=tracker,
            target_cluster=3,
            already_checked={o.cluster for o in orphans} | {o.cluster for o in linked},
        )
        reads_after = tracker.total_cluster_reads
        # Drugi przebieg: tylko lekkie sprawdzenia skip, bez pełnych drzew
        assert reads_after - reads_mid < reads_mid - reads_before or reads_after == reads_mid
        assert linked
        assert not orphans2 and not linked2


def test_counters_reflect_real_operations(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        result = reconstruct_target(vol, "bhp szkoły", deep_scan=True)
        assert result.tracker is not None
        assert result.tracker.total_cluster_reads > 0
        assert result.tracker.unique_clusters_analyzed > 0
        # Ponowne odczyty mogą istnieć (FAT/deep), ale nie powinny dominować ekstremalnie
        assert result.tracker.reread_count < result.tracker.total_cluster_reads * 5


def test_expensive_candidate_is_deferred_and_retryable(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        events = []

        def candidate_cb(candidate_id, cluster, status, data):
            events.append((candidate_id, cluster, status, data))

        find_orphan_directories(
            vol,
            set(),
            candidate_clusters=[11],
            target_cluster=3,
            candidate_cb=candidate_cb,
            budget_factory=lambda: CandidateBudget(max_records=0),
        )
        assert any(event[2] == "DEFERRED" for event in events)

        events.clear()
        orphans, linked = find_orphan_directories(
            vol,
            set(),
            candidate_clusters=[11],
            target_cluster=3,
            candidate_cb=candidate_cb,
            budget_factory=lambda: CandidateBudget(max_records=1000),
        )
        assert linked
        assert any(event[2] == "COMPLETED" for event in events)
