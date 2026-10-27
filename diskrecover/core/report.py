"""Raporty HTML / JSON / CSV z wyników odzyskiwania."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from diskrecover.core.recovery import RecoverySession


def write_reports(session: "RecoverySession", report_dir: Path) -> dict:
    report_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": report_dir / "raport.json",
        "csv": report_dir / "raport.csv",
        "html": report_dir / "raport.html",
    }
    write_json(session, paths["json"])
    write_csv(session, paths["csv"])
    write_html(session, paths["html"])
    return {k: str(v) for k, v in paths.items()}


def write_json(session: "RecoverySession", path: Path) -> None:
    payload = {
        "wygenerowano": datetime.now(timezone.utc).isoformat(),
        "obraz": session.image_path,
        "katalog_wyjsciowy": session.output_dir,
        "katalog_docelowy": session.target_name,
        "wolumen": session.volume_summary,
        "statystyki": session.stats,
        "komunikaty": session.messages,
        "pozycje": [
            {
                "sciezka": i.path,
                "wyjscie": i.output_path,
                "klaster": i.cluster,
                "rozmiar": i.size,
                "pewnosc": i.confidence,
                "kategoria": i.category,
                "dowody": i.evidence,
                "katalog": i.is_directory,
                "usuniety": i.is_deleted,
                "wyeksportowany": i.exported,
            }
            for i in session.items
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(session: "RecoverySession", path: Path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(
            [
                "sciezka",
                "wyjscie",
                "klaster",
                "rozmiar",
                "pewnosc",
                "kategoria",
                "katalog",
                "usuniety",
                "dowody",
            ]
        )
        for i in session.items:
            w.writerow(
                [
                    i.path,
                    i.output_path,
                    i.cluster,
                    i.size,
                    i.confidence,
                    i.category,
                    "tak" if i.is_directory else "nie",
                    "tak" if i.is_deleted else "nie",
                    " | ".join(i.evidence),
                ]
            )


def write_html(session: "RecoverySession", path: Path) -> None:
    stats = session.stats
    rows = []
    for i in session.items:
        if i.is_directory:
            continue
        conf_class = {
            "wysokie": "ok",
            "srednie": "mid",
            "niskie": "low",
            "nieustalone": "unk",
        }.get(i.confidence, "unk")
        rows.append(
            "<tr>"
            f"<td>{_esc(i.path)}</td>"
            f"<td>{_esc(i.output_path)}</td>"
            f"<td>{i.size}</td>"
            f"<td class='{conf_class}'>{_esc(i.confidence)}</td>"
            f"<td>{_esc(i.category)}</td>"
            f"<td>{_esc('; '.join(i.evidence))}</td>"
            "</tr>"
        )
    msgs = "".join(f"<li>{_esc(m)}</li>" for m in session.messages)
    vol = session.volume_summary
    html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8"/>
<title>DiskRecover — raport</title>
<style>
body {{ font-family: Segoe UI, Tahoma, sans-serif; background:#12141a; color:#e8eaed; margin:0; padding:24px; }}
h1 {{ color:#7dd3c0; }}
h2 {{ color:#a8b0c0; font-weight:600; }}
.card {{ background:#1c2030; border:1px solid #2a3145; border-radius:8px; padding:16px; margin-bottom:16px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ border-bottom:1px solid #2a3145; padding:8px; text-align:left; vertical-align:top; }}
th {{ color:#9aa3b5; }}
.ok {{ color:#6bcb8b; }} .mid {{ color:#e0b45a; }} .low {{ color:#e07a7a; }} .unk {{ color:#9aa3b5; }}
.stat {{ display:inline-block; margin-right:18px; }}
.stat b {{ color:#7dd3c0; }}
code {{ color:#c3d0e8; }}
</style>
</head>
<body>
<h1>DiskRecover — raport odzyskiwania</h1>
<div class="card">
<p><b>Obraz:</b> <code>{_esc(session.image_path)}</code></p>
<p><b>Katalog docelowy:</b> {_esc(session.target_name)}</p>
<p><b>Wyjście:</b> <code>{_esc(session.output_dir)}</code></p>
<p><b>Wolumen:</b> cluster={vol.get('cluster_size')} B, sektor={vol.get('bytes_per_sector')},
root={vol.get('root_cluster')}, label={_esc(str(vol.get('volume_label','')))},
offset partycji={vol.get('partition_offset')}</p>
</div>
<div class="card">
<h2>Statystyki</h2>
<p>
<span class="stat">Pliki: <b>{stats.get('plikow_lacznie',0)}</b></span>
<span class="stat">W docelowym: <b>{stats.get('w_docelowym',0)}</b></span>
<span class="stat">Osierocone: <b>{stats.get('osierocone',0)}</b></span>
<span class="stat">Nieustalone: <b>{stats.get('nieustalone',0)}</b></span>
<span class="stat">RAW: <b>{stats.get('raw',0)}</b></span>
<span class="stat">Pewność wysoka: <b>{stats.get('wysokie',0)}</b></span>
<span class="stat">Średnia: <b>{stats.get('srednie',0)}</b></span>
<span class="stat">Niska: <b>{stats.get('niskie',0)}</b></span>
</p>
</div>
<div class="card">
<h2>Komunikaty</h2>
<ul>{msgs or '<li>Brak</li>'}</ul>
</div>
<div class="card">
<h2>Pliki</h2>
<table>
<thead><tr><th>Ścieżka</th><th>Wyjście</th><th>Rozmiar</th><th>Pewność</th><th>Kategoria</th><th>Dowody</th></tr></thead>
<tbody>
{''.join(rows) if rows else '<tr><td colspan="6">Brak plików</td></tr>'}
</tbody>
</table>
</div>
<p style="color:#6a7388;font-size:12px">DiskRecover — tryb tylko do odczytu. Pliki RAW nigdy nie są przypisywane do katalogu docelowego bez dowodu.</p>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
