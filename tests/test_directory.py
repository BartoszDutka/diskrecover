"""Testy wpisów katalogów i LFN."""

from diskrecover.core.directory import names_match, read_directory
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader


def test_root_contains_target_lfn(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        entries = read_directory(vol, vol.bpb.root_cluster)
        names = [e.name for e in entries]
        assert any(names_match(n, "bhp szkoły") for n in names)
        assert any(names_match(n, "INNE") for n in names)


def test_target_children(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        root = read_directory(vol, 2)
        target = next(e for e in root if names_match(e.name, "bhp szkoły"))
        kids = read_directory(vol, target.cluster, include_deleted=True)
        kid_names = [k.name.upper() for k in kids if not k.is_dot]
        assert any("NOTATKA" in n for n in kid_names)
        assert any("FOTO" in n for n in kid_names)
        deleted = [k for k in kids if k.is_deleted]
        assert deleted, "Oczekiwano usuniętego STARY.DOC"
