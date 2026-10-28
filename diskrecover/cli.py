"""Tryb wiersza poleceń DiskRecover."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diskrecover import __version__


DEFAULT_IMAGE = r"C:\Users\bdutka\Desktop\Odzyskiwanie_BHP\pendrive_bhp.bin"
DEFAULT_TARGET = "bhp szkoły"


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
    a.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    a.add_argument("--partition-lba", type=int, default=None)

    # Odzyskiwanie
    r = sub.add_parser("recover", help="Pełne odzyskiwanie do katalogu wyjściowego")
    r.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    r.add_argument("-o", "--output", required=True, help="Katalog wyjściowy")
    r.add_argument("--target", default=DEFAULT_TARGET, help="Nazwa katalogu docelowego")
    r.add_argument("--partition-lba", type=int, default=None)
    r.add_argument("--no-raw", action="store_true", help="Wyłącz carving RAW")
    r.add_argument("--shallow", action="store_true", help="Bez głębokiego skanu klastrów")
    r.add_argument("--json", action="store_true", help="Podsumowanie JSON na stdout")

    d = sub.add_parser(
        "diagnose-candidate",
        help="Zdiagnozuj jeden kandydat osieroconego katalogu bez pełnego skanu",
    )
    d.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    d.add_argument("--output", required=True, help="Katalog wyników/logów")
    d.add_argument("--cluster", type=int, required=True)
    d.add_argument("--target", default=DEFAULT_TARGET)
    d.add_argument("--target-cluster", type=int, default=None)
    d.add_argument("--partition-lba", type=int, default=None)
    d.add_argument("--max-seconds", type=float, default=300.0)
    d.add_argument("--max-reads", type=int, default=100_000)
    d.add_argument("--max-clusters", type=int, default=100_000)
    d.add_argument("--max-records", type=int, default=1_000_000)

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
    if args.cmd == "diagnose-candidate":
        return cmd_diagnose_candidate(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
