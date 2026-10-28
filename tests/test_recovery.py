"""Testy pełnego odzyskiwania na syntetycznym obrazie."""

from pathlib import Path

from diskrecover.core.checkpoint import CheckpointStore
from diskrecover.core.recovery import RecoveryEngine, RecoveryMode


def test_full_recovery(fat32_image, tmp_path):
    out = tmp_path / "out"
    engine = RecoveryEngine(
        fat32_image["path"],
        out,
        target_name="bhp szkoły",
        enable_raw=True,
        deep_scan=True,
    )
    try:
        session = engine.run()
    finally:
        engine.close()

    assert session.stats["target_znaleziony"] == 1
    assert session.stats["w_docelowym"] >= 2  # notatka + foto (+ deleted)
    assert session.stats["nieustalone"] >= 1  # INNE/inne.txt
    # Sprawdź układ katalogów
    assert (out / "Odtworzony_bhp_szkoly").is_dir()
    assert (out / "Osierocone_katalogi").is_dir()
    assert (out / "RAW").is_dir()
    assert (out / "Nieustalone_pochodzenie").is_dir()
    assert (out / "Raport" / "raport.json").is_file()
    assert (out / "Raport" / "raport.html").is_file()
    assert (out / "Raport" / "raport.csv").is_file()
    assert (out / "Raport" / "checkpoint.json").is_file()
    partial = RecoveryEngine.load_partial_session(session.checkpoint_path)
    assert partial.items
    assert any(item.path.endswith("NOTATKA.TXT") for item in partial.items)

    # Plik z oryginalną treścią
    notes = list((out / "Odtworzony_bhp_szkoly").rglob("NOTATKA.TXT"))
    assert notes
    text = notes[0].read_text(encoding="utf-8")
    assert "BHP" in text

    # INNE nie trafia do Odtworzony_bhp_szkoly
    other = list((out / "Nieustalone_pochodzenie").rglob("INNE.TXT"))
    assert other

    # RAW nigdy jako target
    for item in session.items:
        if item.category == "raw":
            assert item.confidence in ("niskie", "nieustalone")


def test_image_unchanged_after_recovery(fat32_image, tmp_path):
    path = Path(fat32_image["path"])
    before = path.read_bytes()
    engine = RecoveryEngine(path, tmp_path / "out2", enable_raw=False, deep_scan=False)
    try:
        engine.run()
    finally:
        engine.close()
    assert path.read_bytes() == before


def test_reconstruction_export_finishes_without_duplicate_objects(
    fat32_image, tmp_path
):
    progress_messages = []
    engine = RecoveryEngine(
        fat32_image["path"], tmp_path / "export_progress",
        enable_raw=False, deep_scan=True
    )
    try:
        session = engine.run(
            progress_cb=lambda _pct, message: progress_messages.append(message)
        )
    finally:
        engine.close()

    object_keys = [
        (item.path, item.cluster, item.is_directory)
        for item in session.items
    ]
    assert len(object_keys) == len(set(object_keys))
    assert any("Rekonstrukcja obiektów:" in message for message in progress_messages)
    assert progress_messages[-1] == "Gotowe"


def test_target_only_skips_root_export_and_can_export_committed_results(
    fat32_image, tmp_path, monkeypatch
):
    out = tmp_path / "target_only"
    engine = RecoveryEngine(
        fat32_image["path"],
        out,
        target_name="bhp szkoły",
        enable_raw=True,
        deep_scan=True,
        mode=RecoveryMode.TARGET_ONLY,
    )

    def fail_root_export(*_args, **_kwargs):
        raise AssertionError("TARGET_ONLY nie może eksportować katalogu głównego")

    monkeypatch.setattr(engine, "_export_other_root_entries", fail_root_export)
    try:
        session = engine.run()
    finally:
        engine.close()

    assert session.stats["w_docelowym"] >= 2
    assert not any(item.category in {"unknown", "orphan", "raw"} for item in session.items)
    assert session.checkpoint_path
    checkpoint = CheckpointStore(session.checkpoint_path)
    checkpoint.open()
    try:
        meta = checkpoint.load_meta()
    finally:
        checkpoint.close()
    assert meta is not None
    assert meta.recovery_mode == RecoveryMode.TARGET_ONLY.value
    assert meta.complete is True

    exported = RecoveryEngine(
        fat32_image["path"],
        out,
        target_name="bhp szkoły",
        mode=RecoveryMode.TARGET_ONLY,
    )
    try:
        live = exported.export_live_results(categories={"target"})
    finally:
        exported.close()
    assert live.items
    assert all(item.category == "target" for item in live.items)
    assert any(item.path.endswith("NOTATKA.TXT") for item in live.items)
