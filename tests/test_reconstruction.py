"""Testy rekonstrukcji katalogu bhp szkoły."""

from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.reconstruction import flatten_nodes, reconstruct_target


def test_reconstruct_finds_target(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        result = reconstruct_target(vol, "bhp szkoły", deep_scan=True)
        assert result.target_found
        assert result.target_cluster == 3
        flat = flatten_nodes(result.nodes)
        files = [n for n in flat if not n.is_directory]
        assert any("notatka" in n.name.lower() for n in files)
        assert all(n.belongs_to_target for n in files)


def test_orphans_not_claimed_as_target(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        result = reconstruct_target(vol, "bhp szkoły", deep_scan=False)
        assert result.orphan_directories
        for o in result.orphan_directories:
            assert o.belongs_to_target is False
