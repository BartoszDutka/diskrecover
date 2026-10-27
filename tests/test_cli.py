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
