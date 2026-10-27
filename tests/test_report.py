"""Testy generatora raportów."""

from pathlib import Path

from diskrecover.core.recovery import RecoveryEngine, RecoveryItem, RecoverySession
from diskrecover.core.report import write_reports


def test_write_reports_standalone(tmp_path):
    session = RecoverySession(
        image_path="x.bin",
        output_dir=str(tmp_path),
        target_name="bhp szkoły",
        volume_summary={"cluster_size": 2048, "bytes_per_sector": 512, "root_cluster": 2,
                        "volume_label": "TEST", "partition_offset": 0},
        items=[
            RecoveryItem(
                path="a.txt",
                output_path="Odtworzony_bhp_szkoly/a.txt",
                cluster=4,
                size=10,
                confidence="wysokie",
                category="target",
                evidence=["test"],
                exported=True,
            )
        ],
        messages=["ok"],
        stats={"plikow_lacznie": 1, "w_docelowym": 1, "osierocone": 0,
               "nieustalone": 0, "raw": 0, "wysokie": 1, "srednie": 0, "niskie": 0},
    )
    paths = write_reports(session, tmp_path / "Raport")
    assert Path(paths["json"]).is_file()
    assert Path(paths["csv"]).is_file()
    assert Path(paths["html"]).is_file()
    html = Path(paths["html"]).read_text(encoding="utf-8")
    assert "DiskRecover" in html
    assert "a.txt" in html
