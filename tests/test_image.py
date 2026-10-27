"""Testy bezpiecznego odczytu obrazu."""

from pathlib import Path

from diskrecover.core.image import ImageReader


def test_read_only_open(fat32_image):
    path = Path(fat32_image["path"])
    before = path.read_bytes()
    with ImageReader(path) as r:
        assert r.size == fat32_image["size"]
        data = r.read(0, 512)
        assert len(data) == 512
        assert data[510:512] == b"\x55\xaa"
    after = path.read_bytes()
    assert before == after


def test_iter_chunks_streaming(fat32_image):
    with ImageReader(fat32_image["path"]) as r:
        total = 0
        for pos, chunk in r.iter_chunks(0, r.size, chunk_size=1024):
            assert pos == total
            total += len(chunk)
        assert total == r.size


def test_out_of_range(fat32_image):
    with ImageReader(fat32_image["path"]) as r:
        assert r.read(r.size + 10, 100) == b""
