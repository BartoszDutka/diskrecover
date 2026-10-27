"""Testy pełnego odzyskiwania na syntetycznym obrazie."""

from pathlib import Path

from diskrecover.core.recovery import RecoveryEngine


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
