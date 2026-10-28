"""Testy CLI."""

from pathlib import Path

from diskrecover.cli import main


def test_analyze_cli(fat32_image, capsys):
    rc = main(["analyze", fat32_image["path"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cluster_size" in out
    assert "FAT32" in out


def test_recover_cli(fat32_image, tmp_path, capsys):
    out = tmp_path / "cli_out"
    rc = main([
        "recover",
        fat32_image["path"],
        "-o",
        str(out),
        "--target",
        "bhp szkoły",
        "--json",
    ])
    assert rc == 0
    assert (out / "Raport" / "raport.json").is_file()


def test_target_only_cli_skips_full_recovery(fat32_image, tmp_path, capsys):
    out = tmp_path / "target_only_cli"
    rc = main([
        "recover",
        fat32_image["path"],
        "-o",
        str(out),
        "--mode",
        "TARGET_ONLY",
        "--no-raw",
    ])
    assert rc == 0
    assert "TARGET_ONLY" in capsys.readouterr().out or (
        out / "Odtworzony_bhp_szkoly"
    ).is_dir()


def test_export_target_cli_uses_existing_results(fat32_image, tmp_path, capsys):
    out = tmp_path / "existing_results"
    rc = main([
        "recover",
        fat32_image["path"],
        "-o",
        str(out),
        "--mode",
        "TARGET_ONLY",
        "--no-raw",
    ])
    assert rc == 0
    rc = main(["export-target", fat32_image["path"], "-o", str(out)])
    assert rc == 0
    assert "Wyeksportowano" in capsys.readouterr().out
