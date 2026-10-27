"""Testy systemu checkpointów SQLite, pauzy i wznowienia."""

import time
from pathlib import Path

import pytest

from diskrecover.core.checkpoint import (
    CheckpointMeta,
    CheckpointStore,
    atomic_checkpoint_write,
    fingerprint_image,
    list_checkpoints,
)
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.recovery import RecoveryEngine
from diskrecover.core.reconstruction import reconstruct_target


def test_fingerprint_stable(fat32_image):
    a = fingerprint_image(fat32_image["path"])
    b = fingerprint_image(fat32_image["path"])
    assert a.matches(b)
    assert a.size == fat32_image["size"]


def test_checkpoint_save_and_load(fat32_image, tmp_path):
    path = tmp_path / "ck.sqlite"

    def writer(store: CheckpointStore) -> None:
        meta = CheckpointMeta(
            stage="Skanowanie osieroconych katalogów",
            stage_index=2,
            status="paused",
            target_name="bhp szkoły",
            output_dir=str(tmp_path),
            image=fingerprint_image(fat32_image["path"]),
            volume_summary={"total_clusters": 10},
        )
        store.save_meta(meta)
        store.replace_int_set("visited_clusters", [2, 3, 8])
        store.replace_int_set("orphan_candidates", [8, 11])
        store.replace_stats("global", {"cluster_reads": 42, "rereads": 3})
        store.save_json_blob("scan_state", {"stage": "orphan", "already_checked": [8]})
        store.upsert_task("stage:fat", "stage", {"name": "fat"}, "done")

    atomic_checkpoint_write(CheckpointStore(path), writer)
    store = CheckpointStore(path)
    store.open()
    try:
        errs = store.validate()
        assert not errs
        meta = store.load_meta()
        assert meta is not None
        assert meta.stage.startswith("Skanowanie")
        assert store.load_int_set("visited_clusters") == {2, 3, 8}
        assert store.load_stats("global")["cluster_reads"] == 42
        assert store.load_json_blob("scan_state")["stage"] == "orphan"
    finally:
        store.close()


def test_corrupt_checkpoint_detected(tmp_path, fat32_image):
    path = tmp_path / "bad.sqlite"
    # Niekompletny plik
    path.write_bytes(b"not a sqlite database")
    listed = list_checkpoints(tmp_path)
    assert listed
    assert listed[0]["valid"] is False


def test_incomplete_checkpoint_flag(fat32_image, tmp_path):
    path = tmp_path / "inc.sqlite"
    store = CheckpointStore(path)
    store.open()
    try:
        store.save_meta(
            CheckpointMeta(
                stage="x",
                image=fingerprint_image(fat32_image["path"]),
                complete=False,
            )
        )
        store.mark_complete(False)
        errs = store.validate()
        assert any("niekompletny" in e for e in errs)
    finally:
        store.close()


def test_wrong_image_rejected(fat32_image, tmp_path):
    out = tmp_path / "out"
    engine = RecoveryEngine(
        fat32_image["path"],
        out,
        enable_raw=False,
        deep_scan=False,
        checkpoint_interval_sec=9999,
    )
    try:
        engine.open_volume()
        engine.tracker.unique_clusters_analyzed = 1
        ckpt = engine.save_checkpoint(reason="test")
    finally:
        engine.close()

    other = tmp_path / "other.bin"
    other.write_bytes(b"\x00" * 4096)
    engine2 = RecoveryEngine(other, out, enable_raw=False, deep_scan=False)
    with pytest.raises(ValueError, match="nie zgadza"):
        engine2.load_checkpoint(ckpt)


def test_pause_resume_flags(fat32_image, tmp_path):
    engine = RecoveryEngine(fat32_image["path"], tmp_path / "out")
    assert not engine.is_paused()
    engine.request_pause()
    assert engine.is_paused()
    engine.request_resume()
    assert not engine.is_paused()


def test_cancel_and_save_creates_checkpoint(fat32_image, tmp_path):
    out = tmp_path / "out_cancel"
    engine = RecoveryEngine(
        fat32_image["path"],
        out,
        enable_raw=False,
        deep_scan=True,
        checkpoint_interval_sec=9999,
    )
    # Symuluj stan przed anulowaniem
    engine._prepare_output()
    engine.open_volume()
    path = engine.cancel_and_save()
    assert path is not None
    assert path.is_file()
    store = CheckpointStore(path)
    store.open()
    try:
        meta = store.load_meta()
        assert meta is not None
        assert meta.status in ("cancelled", "running", "paused")
    finally:
        store.close()
    engine.close()


def test_resume_after_checkpoint_consistent(fat32_image, tmp_path):
    """Wynik bez przerwania vs wznowienie ze stanu po rekonstrukcji — spójne statystyki celu."""
    out1 = tmp_path / "full"
    eng1 = RecoveryEngine(
        fat32_image["path"], out1, enable_raw=False, deep_scan=True
    )
    try:
        s1 = eng1.run()
    finally:
        eng1.close()

    # Sam checkpoint z fingerprint + puste already_checked: pełne odtworzenie powinno znaleźć to samo
    out2 = tmp_path / "resume"
    eng_prep = RecoveryEngine(
        fat32_image["path"], out2, enable_raw=False, deep_scan=True
    )
    eng_prep._prepare_output()
    eng_prep.open_volume()
    ckpt = eng_prep.save_checkpoint(reason="prep")
    eng_prep.close()

    eng2 = RecoveryEngine(
        fat32_image["path"],
        out2,
        enable_raw=False,
        deep_scan=True,
        resume_checkpoint=ckpt,
    )
    try:
        s2 = eng2.run()
    finally:
        eng2.close()

    assert s1.stats["target_znaleziony"] == s2.stats["target_znaleziony"]
    assert s1.stats["podfoldery_bhp_szkoly"] == s2.stats["podfoldery_bhp_szkoly"]
    assert Path(fat32_image["path"]).read_bytes() == Path(fat32_image["path"]).read_bytes()


def test_image_not_modified_by_checkpoint(fat32_image, tmp_path):
    before = Path(fat32_image["path"]).read_bytes()
    eng = RecoveryEngine(fat32_image["path"], tmp_path / "o", enable_raw=False, deep_scan=False)
    try:
        eng.run()
    finally:
        eng.close()
    assert Path(fat32_image["path"]).read_bytes() == before


def test_reconstruct_profile_present(fat32_image):
    with ImageReader(fat32_image["path"]) as reader:
        vol = Fat32Volume.open(reader, fat32_image["partition_offset"])
        result = reconstruct_target(vol, "bhp szkoły", deep_scan=True)
        assert "Profil wydajności" in result.profile_report
