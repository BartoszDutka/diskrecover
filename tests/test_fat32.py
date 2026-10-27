"""Testy parsera MBR/BPB/FAT32 — parametry z obrazu."""

from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.mbr import find_fat32_partition, parse_mbr


def test_mbr_and_bpb_from_image(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        parts = parse_mbr(reader)
        assert len(parts) == 1
        assert parts[0].lba_start == fat32_image["partition_lba"]
        assert parts[0].is_fat32
        part = find_fat32_partition(reader)
        vol = Fat32Volume.open(reader, part.byte_offset)
        s = vol.summary()
        assert s["bytes_per_sector"] == fat32_image["bytes_per_sector"]
        assert s["sectors_per_cluster"] == fat32_image["sectors_per_cluster"]
        assert s["cluster_size"] == fat32_image["cluster_size"]
        assert s["root_cluster"] == fat32_image["root_cluster"]
        assert s["partition_offset"] == fat32_image["partition_offset"]
        assert "FAT32" in s["fs_type"]


def test_fat_chain(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        chain = vol.follow_chain(2)
        assert chain == [2]
        # Plik notatka na klastrze 4
        assert vol.follow_chain(4) == [4]


def test_bpb_not_hardcoded_cluster_size(fat32_image):
    """Upewnij się, że nie zakładamy 8192 z notatek użytkownika."""
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        # Fixture używa 2048 — musi pochodzić z BPB
        assert vol.cluster_size == 2048
        assert vol.cluster_size != 8192
