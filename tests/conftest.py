"""Wspólne fixtures testowe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.make_fat32_image import build_fat32_image


@pytest.fixture
def fat32_image(tmp_path: Path) -> dict:
    path = tmp_path / "test_fat32.bin"
    info = build_fat32_image(path)
    return info


@pytest.fixture
def fat32_image_no_orphan(tmp_path: Path) -> dict:
    path = tmp_path / "test_fat32_min.bin"
    info = build_fat32_image(path, include_orphan=False, include_raw_payload=False)
    return info
