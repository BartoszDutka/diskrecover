"""Tryb wiersza poleceń DiskRecover."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diskrecover import __version__
from diskrecover.core.recovery import RecoveryMode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diskrecover",
        description="DiskRecover — bezpieczne odzyskiwanie FAT32 (tylko odczyt obrazu).",
    )
    p.add_argument("--version", action="version", version=f"DiskRecover {__version__}")
    sub = p.add_subparsers(dest="cmd")

    # GUI
    g = sub.add_parser("gui", help="Uruchom interfejs graficzny (PySide6)")
    g.add_argument("--image", default="", help="Ścieżka do obrazu (opcjonalnie)")

    # Analiza
    a = sub.add_parser("analyze", help="Pokaż parametry MBR/BPB z obrazu")
    a.add_argument("image")
    a.add_argument("--partition-lba", type=int, default=None)

    # Odzyskiwanie
    r = sub.add_parser("recover", help="Pełne odzyskiwanie do katalogu wyjściowego")
    r.add_argument("image")
    r.add_argument("-o", "--output", required=True, help="Katalog wyjściowy")
    r.add_argument("--target", default="", help="Nazwa katalogu docelowego (opcjonalna)")
    r.add_argument(
        "--target-cluster",
        type=int,
        default=None,
        help="Znany klaster nadrzędny katalogu docelowego",
    )
    r.add_argument("--partition-lba", type=int, default=None)
    r.add_argument("--no-raw", action="store_true", help="Wyłącz carving RAW")
    r.add_argument("--shallow", action="store_true", help="Bez głębokiego skanu klastrów")
    r.add_argument("--json", action="store_true", help="Podsumowanie JSON na stdout")
    r.add_argument(
        "--mode",
        choices=[mode.value for mode in RecoveryMode],
        default=RecoveryMode.FULL_RECOVERY.value,
        help="TARGET_ONLY pomija eksport innych katalogów; FULL_RECOVERY eksportuje cały wolumin",
    )

    e = sub.add_parser(
        "export-target",
        help="Eksportuj już zapisane rekordy target z results.sqlite bez skanowania",
    )
    e.add_argument("image")
    e.add_argument("-o", "--output", required=True, help="Katalog wyników z Raport/results.sqlite")
    e.add_argument("--target", default="")

    d = sub.add_parser(
        "diagnose-candidate",
        help="Zdiagnozuj jeden kandydat osieroconego katalogu bez pełnego skanu",
    )
    d.add_argument("image")
    d.add_argument("--output", required=True, help="Katalog wyników/logów")
    d.add_argument("--cluster", type=int, required=True)
    d.add_argument("--target", default="")
    d.add_argument("--target-cluster", type=int, default=None)
    d.add_argument("--partition-lba", type=int, default=None)
    d.add_argument("--max-seconds", type=float, default=300.0)
    d.add_argument("--max-reads", type=int, default=100_000)
    d.add_argument("--max-clusters", type=int, default=100_000)
    d.add_argument("--max-records", type=int, default=1_000_000)

    v = sub.add_parser("validate", help="Sprawdź strukturę odzyskanych plików")
    v.add_argument("source", help="Plik lub katalog do sprawdzenia")
    v.add_argument("--json", action="store_true", help="Wynik JSON na stdout")
    v.add_argument("--cache", help="Opcjonalna baza cache SHA-256")

    x = sub.add_parser("export-validated", help="Eksportuj pliki po walidacji")
    x.add_argument("source", help="Plik lub katalog źródłowy")
    x.add_argument("-o", "--output", required=True, help="Katalog docelowy")
    x.add_argument("--flat", action="store_true", help="Jeden katalog bez struktury źródła")
    x.add_argument("--include-unverified", action="store_true")
    x.add_argument("--keep-duplicates", action="store_true")
    x.add_argument("--cache", help="Opcjonalna baza cache SHA-256")

    return p


def cmd_analyze(args: argparse.Namespace) -> int:
    from diskrecover.core.image import ImageReader
    from diskrecover.core.mbr import parse_mbr, find_fat32_partition
    from diskrecover.core.fat32 import Fat32Volume

    path = Path(args.image)
    if not path.is_file():
        print(f"Błąd: nie znaleziono obrazu: {path}", file=sys.stderr)
        return 2
    with ImageReader(path) as reader:
        print(f"Obraz: {path} ({reader.size} bajtów)")
        parts = parse_mbr(reader)
        if parts:
            print("Partycje MBR:")
            for p in parts:
                print(
                    f"  #{p.index} typ={p.type_name} (0x{p.type_id:02X}) "
                    f"LBA={p.lba_start} sektorów={p.sector_count}"
                )
        else:
            print("Brak poprawnego MBR (lub sygnatury 55AA).")
        part = find_fat32_partition(reader, preferred_lba=args.partition_lba)
        offset = part.byte_offset if part else (args.partition_lba or 0) * 512
        if args.partition_lba is not None:
            offset = args.partition_lba * 512
        vol = Fat32Volume.open(reader, offset)
        print("BPB / wolumen:")
        for k, v in vol.summary().items():
            print(f"  {k}: {v}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    from diskrecover.core.recovery import RecoveryEngine

    path = Path(args.image)
    if not path.is_file():
        print(f"Błąd: nie znaleziono obrazu: {path}", file=sys.stderr)
        return 2

    def progress(pct: int, msg: str) -> None:
        print(f"[{pct:3d}%] {msg}")

    engine = RecoveryEngine(
        path,
        args.output,
        target_name=args.target,
        partition_lba=args.partition_lba,
        enable_raw=not args.no_raw,
        deep_scan=not args.shallow,
        mode=RecoveryMode(args.mode),
        target_cluster=args.target_cluster,
    )
    try:
        session = engine.run(progress_cb=progress)
    except InterruptedError:
        print("Anulowano.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 1
    finally:
        engine.close()

    print("Statystyki:", session.stats)
    print("Raporty w:", Path(args.output) / "Raport")
    if args.json:
        print(json.dumps(session.stats, ensure_ascii=False, indent=2))
    return 0


def cmd_export_target(args: argparse.Namespace) -> int:
    from diskrecover.core.recovery import RecoveryEngine

    path = Path(args.image)
    if not path.is_file():
        print(f"Błąd: nie znaleziono obrazu: {path}", file=sys.stderr)
        return 2
    engine = RecoveryEngine(path, args.output, target_name=args.target)
    try:
        session = engine.export_live_results(categories={"target"})
    except Exception as exc:
        print(f"Błąd eksportu: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    print("Wyeksportowano zapisane rekordy target:", session.stats)
    return 0


def cmd_diagnose_candidate(args: argparse.Namespace) -> int:
    from diskrecover.core.reconstruction import CandidateBudget
    from diskrecover.core.recovery import RecoveryEngine

    path = Path(args.image)
    if not path.is_file():
        print(f"Błąd: nie znaleziono obrazu: {path}", file=sys.stderr)
        return 2
    engine = RecoveryEngine(
        path,
        args.output,
        target_name=args.target,
        partition_lba=args.partition_lba,
        enable_raw=False,
        deep_scan=False,
        candidate_max_seconds=args.max_seconds,
        candidate_max_reads=args.max_reads,
        candidate_max_clusters=args.max_clusters,
        candidate_max_records=args.max_records,
    )
    try:
        result = engine.diagnose_candidate(
            args.cluster,
            target_cluster=args.target_cluster,
            budget=CandidateBudget(
                max_seconds=args.max_seconds,
                max_reads=args.max_reads,
                max_clusters=args.max_clusters,
                max_records=args.max_records,
            ),
        )
    except Exception as exc:
        print(f"Błąd diagnostyki: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    from diskrecover.core.validation import validate_tree

    try:
        results = validate_tree(args.source)
    except (OSError, ValueError) as exc:
        print(f"Błąd walidacji: {exc}", file=sys.stderr)
        return 1
    payload = [
        {
            **asdict(result),
            "path": str(result.path),
            "duplicate_of": str(result.duplicate_of) if result.duplicate_of else None,
            "integrity": result.integrity.value,
            "status": result.status.value,
        }
        for result in results
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in payload:
            print(f"{result['status']}: {result['path']} — {result['reason']}")
        print(f"Sprawdzono plików: {len(payload)}")
    return 0


def cmd_export_validated(args: argparse.Namespace) -> int:
    from diskrecover.core.export import export_files
    from diskrecover.core.validation import validate_tree

    try:
        results = validate_tree(args.source)
        records = export_files(
            results,
            args.output,
            flatten=args.flat,
            include_unverified=args.include_unverified,
            skip_duplicates=not args.keep_duplicates,
            source_root=args.source,
        )
    except (OSError, ValueError) as exc:
        print(f"Błąd eksportu: {exc}", file=sys.stderr)
        return 1
    exported = sum(record.status == "EXPORTED" for record in records)
    print(f"Wyeksportowano: {exported}; mapowanie: {Path(args.output) / 'export-map.csv'}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from diskrecover.gui.app import run_gui

    return run_gui(initial_image=args.image or None)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        # Domyślnie GUI
        return cmd_gui(argparse.Namespace(image=""))
    if args.cmd == "gui":
        return cmd_gui(args)
    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "recover":
        return cmd_recover(args)
    if args.cmd == "export-target":
        return cmd_export_target(args)
    if args.cmd == "diagnose-candidate":
        return cmd_diagnose_candidate(args)
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "export-validated":
        return cmd_export_validated(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
