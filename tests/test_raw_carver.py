"""Testy carvingu RAW."""

from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.raw_carver import carve_unallocated


def test_carve_finds_pdf(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        carved = carve_unallocated(vol)
        pdfs = [c for c in carved if c.extension == "pdf"]
        assert pdfs, "Oczekiwano wycarve'owanego PDF"
        assert all(c.belongs_to_target is False for c in pdfs)
