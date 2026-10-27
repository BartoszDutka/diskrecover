"""Silnik odzyskiwania plików z poziomami pewności, checkpointami i pauzą."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from diskrecover.core.checkpoint import (
    CheckpointMeta,
    CheckpointStore,
    atomic_checkpoint_write,
    default_checkpoint_dir,
    fingerprint_image,
    list_checkpoints,
)
from diskrecover.core.cluster_tracker import ClusterTracker
from diskrecover.core.directory import names_match, read_directory
from diskrecover.core.fat32 import Fat32Volume
from diskrecover.core.image import ImageReader
from diskrecover.core.mbr import find_fat32_partition, parse_mbr
from diskrecover.core.profiler import Profiler
from diskrecover.core.raw_carver import CarvedFile, carve_unallocated, export_carved
from diskrecover.core.reconstruction import (
    Confidence,
    RecoveredNode,
    ReconstructionResult,
    build_tree_from_directory,
    flatten_nodes,
    reconstruct_target,
)
from diskrecover.core.report import write_reports
from diskrecover.core.telemetry import (
    ADDITIVE_FIELDS,
    ScanSnapshot,
    ScanTelemetry,
    create_scanner_logger,
)


OUTPUT_DIRS = (
    "Odtworzony_bhp_szkoly",
    "Osierocone_katalogi",
    "RAW",
    "Nieustalone_pochodzenie",
    "Raport",
)


def safe_filename(name: str) -> str:
    name = name.strip().rstrip(".")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if not name or name in (".", ".."):
        name = "_unnamed_"
    return name[:200]


@dataclass
class RecoveryItem:
    path: str
    output_path: str
    cluster: int
    size: int
    confidence: str
    category: str  # target | orphan | unknown | raw
    evidence: List[str]
    is_directory: bool = False
    is_deleted: bool = False
    exported: bool = False


@dataclass
class RecoverySession:
    image_path: str
    output_dir: str
    target_name: str
    volume_summary: dict
    items: List[RecoveryItem] = field(default_factory=list)
    carved: List[CarvedFile] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    checkpoint_path: Optional[str] = None
    profile_report: str = ""


ProgressCb = Optional[Callable[[int, str], None]]
TelemetryCb = Optional[Callable[[ScanSnapshot], None]]
CheckpointCb = Optional[Callable[[str, float], None]]


class RecoveryEngine:
    """Orkiestracja: analiza → rekonstrukcja → eksport → RAW → raport."""

    def __init__(
        self,
        image_path: str | Path,
        output_dir: str | Path,
        *,
        target_name: str = "bhp szkoły",
        partition_lba: Optional[int] = None,
        enable_raw: bool = True,
        deep_scan: bool = True,
        checkpoint_interval_sec: float = 300.0,
        checkpoint_every_candidates: int = 500,
        resume_checkpoint: Optional[str | Path] = None,
    ) -> None:
        self.image_path = Path(image_path)
        self.output_dir = Path(output_dir)
        self.target_name = target_name
        self.partition_lba = partition_lba
        self.enable_raw = enable_raw
        self.deep_scan = deep_scan
        self.checkpoint_interval_sec = checkpoint_interval_sec
        self.checkpoint_every_candidates = checkpoint_every_candidates
        self.resume_checkpoint = Path(resume_checkpoint) if resume_checkpoint else None

        self._cancel = False
        self._pause = threading.Event()
        self._pause.set()  # set = running; clear = paused
        self._checkpoint_request = threading.Event()
        self._last_checkpoint_at = 0.0
        self._candidates_since_ckpt = 0
        self._ckpt_lock = threading.Lock()

        self.reader: Optional[ImageReader] = None
        self.volume: Optional[Fat32Volume] = None
        self.session: Optional[RecoverySession] = None
        self.telemetry: Optional[ScanTelemetry] = None
        self.logger: Optional[logging.Logger] = None
        self.tracker = ClusterTracker()
        self.profiler = Profiler(enabled=True)
        self._scan_state: Dict = {
            "stage": "init",
            "stage_index": 0,
            "completed_stages": [],
            "orphan_candidates": [],
            "already_checked": [],
            "known_clusters": [],
            "target_cluster": None,
        }
        self._checkpoint_cb: CheckpointCb = None
        self._latest_recon: Optional[ReconstructionResult] = None
        self._ckpt_store: Optional[CheckpointStore] = None

    def request_cancel(self) -> None:
        self._cancel = True
        self._pause.set()  # odblokuj pauzę, by wyjść

    def request_pause(self) -> None:
        self._pause.clear()
        if self.telemetry:
            self.telemetry.update(status="wstrzymane", operation="Skanowanie wstrzymane")

    def request_resume(self) -> None:
        self._pause.set()
        if self.telemetry:
            self.telemetry.update(status="skanowanie", operation="Wznowiono skanowanie")

    def request_checkpoint(self) -> None:
        self._checkpoint_request.set()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def _cancelled(self) -> bool:
        return self._cancel

    def _wait_if_paused(self) -> None:
        while not self._pause.is_set():
            if self._cancel:
                raise InterruptedError("Anulowano skanowanie")
            time.sleep(0.05)

    def _prepare_output(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for d in OUTPUT_DIRS:
            (self.output_dir / d).mkdir(parents=True, exist_ok=True)
        default_checkpoint_dir(self.output_dir).mkdir(parents=True, exist_ok=True)

    def open_volume(self, progress_cb: ProgressCb = None) -> Fat32Volume:
        if progress_cb:
            progress_cb(2, "Otwieram obraz (tylko odczyt)…")
        self.reader = ImageReader(self.image_path)
        offset = 0
        parts = parse_mbr(self.reader)
        preferred = self.partition_lba
        part = find_fat32_partition(self.reader, preferred_lba=preferred)
        if part is not None:
            offset = part.byte_offset
            if progress_cb:
                progress_cb(
                    5,
                    f"Partycja LBA {part.lba_start} ({part.type_name}), offset {offset}",
                )
        else:
            if progress_cb:
                progress_cb(5, "Brak MBR — zakładam wolumin FAT32 od początku obrazu")
            offset = 0
            if preferred is not None:
                offset = preferred * 512

        self.volume = Fat32Volume.open(self.reader, offset, validate=True)
        if progress_cb:
            s = self.volume.summary()
            progress_cb(
                8,
                f"BPB OK: cluster={s['cluster_size']} B, root={s['root_cluster']}, "
                f"label={s['volume_label']!r}",
            )
        return self.volume

    def _checkpoint_path(self) -> Path:
        return default_checkpoint_dir(self.output_dir) / "scan_state.sqlite"

    def save_checkpoint(self, *, reason: str = "manual") -> Path:
        """Zapisuje pełny stan skanowania (atomowo). Nie zatrzymuje skanowania."""
        with self._ckpt_lock:
            path = self._checkpoint_path()
            vol_summary = self.volume.summary() if self.volume else {}
            fp = fingerprint_image(self.image_path)
            telem = self.telemetry.export_counters() if self.telemetry else {}
            state = dict(self._scan_state)
            state["tracker"] = self.tracker.export_state()
            state["reason"] = reason
            state["telemetry"] = telem
            state["profiler"] = self.profiler.as_dict()

            def writer(store: CheckpointStore) -> None:
                meta = CheckpointMeta(
                    stage=state.get("stage", ""),
                    stage_index=int(state.get("stage_index") or 0),
                    status="paused" if self.is_paused() else ("cancelled" if self._cancel else "running"),
                    target_name=self.target_name,
                    output_dir=str(self.output_dir),
                    deep_scan=self.deep_scan,
                    enable_raw=self.enable_raw,
                    partition_lba=self.partition_lba,
                    image=fp,
                    volume_summary=vol_summary,
                    complete=False,
                )
                store.save_meta(meta)
                store.replace_int_set(
                    "visited_clusters", state.get("known_clusters") or []
                )
                store.replace_int_set(
                    "already_checked", state.get("already_checked") or []
                )
                store.replace_int_set(
                    "orphan_candidates", state.get("orphan_candidates") or []
                )
                store.replace_int_set(
                    "analyzed_dirs", self.tracker.analyzed_starts
                )
                store.replace_stats("global", telem)
                store.save_json_blob("scan_state", state)
                store.save_json_blob("settings", {
                    "target_name": self.target_name,
                    "deep_scan": self.deep_scan,
                    "enable_raw": self.enable_raw,
                    "partition_lba": self.partition_lba,
                    "checkpoint_interval_sec": self.checkpoint_interval_sec,
                    "checkpoint_every_candidates": self.checkpoint_every_candidates,
                })
                for stage in state.get("completed_stages") or []:
                    store.upsert_task(f"stage:{stage}", "stage", {"name": stage}, "done")
                store.upsert_task(
                    f"stage:{state.get('stage')}",
                    "stage",
                    {"name": state.get("stage"), "index": state.get("stage_index")},
                    "pending" if not self._cancel else "cancelled",
                )
                if self._latest_recon:
                    for rel in self._latest_recon.target_linked_orphans:
                        store.upsert_relation(
                            rel.cluster,
                            rel.parent_cluster or -1,
                            (rel.link_confidence or Confidence.HIGH).value,
                            rel.evidence,
                        )

            if self._ckpt_store is None or self._ckpt_store.path != path:
                if self._ckpt_store is not None:
                    self._ckpt_store.close()
                self._ckpt_store = CheckpointStore(path)
            else:
                self._ckpt_store.close()
            atomic_checkpoint_write(self._ckpt_store, writer)
            self._last_checkpoint_at = time.monotonic()
            self._candidates_since_ckpt = 0
            saved_at = time.time()
            if self.telemetry:
                self.telemetry.update(
                    checkpoint_path=str(path),
                    checkpoint_saved_at=saved_at,
                    operation=f"Zapisano checkpoint ({reason}): {path}",
                )
            if self.logger:
                self.logger.info("Checkpoint (%s) → %s", reason, path)
            if self._checkpoint_cb:
                self._checkpoint_cb(str(path), saved_at)
            return path

    def load_checkpoint(self, path: str | Path) -> CheckpointMeta:
        store = CheckpointStore(path)
        store.open()
        try:
            errors = store.validate()
            if errors:
                raise ValueError("Checkpoint niepoprawny: " + "; ".join(errors))
            meta = store.load_meta()
            if meta is None:
                raise ValueError("Brak metadanych checkpointu")
            if meta.image is None:
                raise ValueError("Checkpoint bez odcisku obrazu")
            current = fingerprint_image(self.image_path)
            if not meta.image.matches(current):
                raise ValueError(
                    "Obraz nie zgadza się z checkpointem "
                    f"(oczekiwano size={meta.image.size}, jest {current.size})"
                )
            if meta.target_name and meta.target_name != self.target_name:
                raise ValueError(
                    f"Inny katalog docelowy w checkpoincie: {meta.target_name!r}"
                )
            state = store.load_json_blob("scan_state") or {}
            self._scan_state.update(state)
            self.tracker.import_state(state.get("tracker") or {})
            self.deep_scan = meta.deep_scan
            self.enable_raw = meta.enable_raw
            if meta.partition_lba is not None:
                self.partition_lba = meta.partition_lba
            return meta
        finally:
            store.close()

    def _maybe_auto_checkpoint(self, *, force: bool = False, reason: str = "auto") -> None:
        if self._checkpoint_request.is_set():
            self._checkpoint_request.clear()
            force = True
            reason = "manual"
        now = time.monotonic()
        due_time = (now - self._last_checkpoint_at) >= self.checkpoint_interval_sec
        due_cand = self._candidates_since_ckpt >= self.checkpoint_every_candidates
        if force or due_time or due_cand:
            try:
                self.save_checkpoint(reason=reason)
            except Exception as exc:
                if self.logger:
                    self.logger.exception("Nie udało się zapisać checkpointu: %s", exc)

    def run(
        self,
        progress_cb: ProgressCb = None,
        telemetry_cb: TelemetryCb = None,
        checkpoint_cb: CheckpointCb = None,
    ) -> RecoverySession:
        self._cancel = False
        self._pause.set()
        self._checkpoint_cb = checkpoint_cb
        self._prepare_output()
        self.logger = create_scanner_logger(self.output_dir)
        self.telemetry = ScanTelemetry(
            self.image_path.stat().st_size if self.image_path.is_file() else 0,
            on_update=telemetry_cb,
        )
        self.logger.info("Rozpoczęcie skanowania obrazu: %s", self.image_path)
        self._last_checkpoint_at = time.monotonic()

        if self.resume_checkpoint and self.resume_checkpoint.is_file():
            meta = self.load_checkpoint(self.resume_checkpoint)
            self.logger.info(
                "Wznowiono checkpoint %s (etap=%s)", self.resume_checkpoint, meta.stage
            )
            if progress_cb:
                progress_cb(1, f"Wznowiono z checkpointu: etap {meta.stage}")

        last_log_at = 0.0
        last_log_cluster = -1
        seen_entry_offsets: Set[int] = set()

        def telemetry_event(**changes: object) -> None:
            nonlocal last_log_at, last_log_cluster
            entry_offset = changes.pop("entry_offset", None)
            set_orphan = changes.pop("set_orphan_candidates", None)
            if self.telemetry:
                increments = {}
                for key in list(changes.keys()):
                    if key in ADDITIVE_FIELDS:
                        increments[key] = int(changes.pop(key))
                if isinstance(entry_offset, int):
                    if entry_offset in seen_entry_offsets:
                        increments.pop("directory_entries", None)
                        increments.pop("directories_found", None)
                        increments.pop("files_found", None)
                    else:
                        seen_entry_offsets.add(entry_offset)
                for key, amount in increments.items():
                    self.telemetry.increment(key, amount)
                if set_orphan is not None:
                    changes["orphan_candidates"] = int(set_orphan)
                    changes["candidates"] = int(set_orphan)
                if changes:
                    self.telemetry.update(**changes)
                # auto-checkpoint po kandydacie
                if increments.get("orphan_candidates_checked"):
                    self._candidates_since_ckpt += int(
                        increments["orphan_candidates_checked"]
                    )
                    self._maybe_auto_checkpoint(reason="candidates")
            cluster = changes.get("cluster")
            now = time.monotonic()
            should_log = bool(changes.get("operation")) and (
                now - last_log_at >= 2.0
                or (
                    isinstance(cluster, int)
                    and abs(cluster - last_log_cluster) >= 4096
                )
            )
            if self.logger and should_log:
                self.logger.info(
                    "operation=%s cluster=%s lba=%s",
                    changes.get("operation"),
                    cluster,
                    changes.get("lba"),
                )
                last_log_at = now
                if isinstance(cluster, int):
                    last_log_cluster = cluster
            self._wait_if_paused()
            self._maybe_auto_checkpoint()

        self._telemetry_event = telemetry_event
        if self.telemetry:
            self.telemetry.update(status="skanowanie", stage="Otwarcie i walidacja obrazu")
        self._scan_state["stage"] = "open"
        self._scan_state["stage_index"] = 0

        vol = self.open_volume(progress_cb)
        vol.activity_cb = telemetry_event
        if self.telemetry:
            self.telemetry.update(
                stage="Odczyt BPB i tablic FAT",
                clusters_total=vol.total_clusters,
                operation="BPB zweryfikowane",
            )
        if progress_cb:
            progress_cb(9, "Ładuję tablicę FAT (jeden odczyt strumieniowy)…")
        last_fat_pct = -1

        def fat_progress(pct: int) -> None:
            nonlocal last_fat_pct
            self._wait_if_paused()
            if self._cancelled():
                raise InterruptedError("Anulowano")
            if pct != last_fat_pct and progress_cb:
                last_fat_pct = pct
                progress_cb(
                    9 + int(pct * 0.01),
                    f"Ładuję tablicę FAT (postęp {pct}%)…",
                )

        vol.preload_fat(
            progress_cb=fat_progress if progress_cb else None,
            cancel_cb=self._cancelled,
        )
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"open", "fat"}
        )
        self.save_checkpoint(reason="stage_fat")

        def wrap_progress(pct: int, msg: str) -> None:
            self._wait_if_paused()
            stage = "Wyszukiwanie folderu docelowego"
            overall: Optional[float] = None
            stage_pct: Optional[float] = None
            low = msg.lower()
            if "osieroc" in low:
                stage = "Skanowanie osieroconych katalogów"
                # pct z find_orphan to już % kandydatów
                if pct >= 0:
                    stage_pct = float(pct)
                    overall = 45.0 + 0.25 * stage_pct  # 45–70
            elif "skan klastr" in low or ("klastr" in low and "osieroc" not in low):
                stage = "Głębokie skanowanie klastrów"
                if pct >= 0:
                    stage_pct = float(pct)
                    overall = 10.0 + 0.35 * min(pct, 70) / 70.0 * 100 / 100
                    overall = 10.0 + (min(max(pct, 0), 70) / 70.0) * 35.0
            elif "drzew" in low or "buduję" in low:
                stage = "Rekonstrukcja znalezionych plików"
                overall = 70.0
            elif "zakończona" in low or "zakonczona" in low:
                stage = "Rekonstrukcja znalezionych plików"
                overall = 72.0
                stage_pct = 100.0
            elif pct < 0:
                overall = None
                stage_pct = None
            else:
                overall = 10.0 + min(pct, 100) * 0.35

            if progress_cb:
                if overall is None:
                    progress_cb(-1, msg)
                else:
                    progress_cb(int(min(90, max(0, overall))), msg)
            if self.logger:
                self.logger.info("postęp=%s%% %s", pct, msg)
            if self.telemetry:
                self.telemetry.update(
                    stage=stage,
                    operation=msg,
                    stage_pct=stage_pct,
                    overall_pct=overall,
                )
            self._scan_state["stage"] = stage

        resume_state = None
        if self.resume_checkpoint:
            resume_state = {
                "orphan_candidates": self._scan_state.get("orphan_candidates") or [],
                "already_checked": self._scan_state.get("already_checked") or [],
                "tracker": self.tracker.export_state(),
            }

        self._scan_state["stage"] = "reconstruct"
        self._scan_state["stage_index"] = 1
        recon = reconstruct_target(
            vol,
            self.target_name,
            progress_cb=wrap_progress,
            cancel_cb=self._cancelled,
            pause_cb=self._wait_if_paused,
            deep_scan=self.deep_scan,
            telemetry_cb=self._telemetry_event,
            tracker=self.tracker,
            profiler=self.profiler,
            resume_state=resume_state,
        )
        self._latest_recon = recon
        self._scan_state["target_cluster"] = recon.target_cluster
        self._scan_state["orphan_candidates"] = list(recon.orphan_candidates)
        self._scan_state["already_checked"] = list(recon.already_checked)
        self._scan_state["known_clusters"] = sorted(
            set(self._scan_state.get("known_clusters") or [])
            | {n.cluster for n in flatten_nodes(recon.nodes) if n.cluster >= 2}
            | {
                n.cluster
                for n in flatten_nodes(recon.orphan_directories)
                if n.cluster >= 2
            }
            | {
                n.cluster
                for n in flatten_nodes(recon.target_linked_orphans)
                if n.cluster >= 2
            }
        )
        if recon.tracker:
            self._scan_state["tracker"] = recon.tracker.export_state()
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"reconstruct"}
        )
        self.save_checkpoint(reason="stage_reconstruct")

        items: List[RecoveryItem] = []
        known_offsets: Set[int] = set()

        if progress_cb:
            progress_cb(55, "Rekonstruuję znalezione pliki…")
        if self.telemetry:
            self.telemetry.update(stage="Rekonstrukcja znalezionych plików")
        target_root = self.output_dir / "Odtworzony_bhp_szkoly"
        items.extend(
            self._export_tree(
                vol, recon.nodes, target_root, category="target", known_offsets=known_offsets
            )
        )

        if progress_cb:
            progress_cb(62, "Eksportuję osierocone katalogi…")
        orphan_root = self.output_dir / "Osierocone_katalogi"
        for orphan in recon.orphan_directories:
            items.extend(
                self._export_tree(
                    vol,
                    [orphan],
                    orphan_root,
                    category="orphan",
                    known_offsets=known_offsets,
                    force_unknown_bucket=False,
                )
            )

        if progress_cb:
            progress_cb(66, "Eksportuję pozostałe wpisy z katalogu głównego…")
        items.extend(
            self._export_other_root_entries(
                vol, recon, known_offsets=known_offsets
            )
        )

        if not recon.target_found:
            recon.messages.append(
                "Katalog docelowy nieznaleziony — nie przypisano plików do Odtworzony_bhp_szkoly."
            )
        if recon.target_found and not recon.target_linked_orphans and not recon.target_directory_intact:
            recon.messages.append(
                "Nie znaleziono podfolderów należących do katalogu docelowego "
                f"(„{self.target_name}”)."
            )

        carved: List[CarvedFile] = []
        if self.enable_raw and "raw" not in (self._scan_state.get("completed_stages") or []):
            if progress_cb:
                progress_cb(70, "Carving RAW (sygnatury)…")
            self._scan_state["stage"] = "raw"

            def raw_prog(pct: int, msg: str) -> None:
                self._wait_if_paused()
                if progress_cb:
                    progress_cb(70 + int(pct * 0.2), msg)

            carved = carve_unallocated(
                vol,
                known_file_offsets=known_offsets,
                progress_cb=raw_prog,
                cancel_cb=self._cancelled,
                telemetry_cb=self._telemetry_event,
            )
            raw_dir = self.output_dir / "RAW"
            for cf in carved:
                if self._cancelled():
                    raise InterruptedError("Anulowano")
                out = export_carved(vol, cf, raw_dir)
                items.append(
                    RecoveryItem(
                        path=cf.output_name,
                        output_path=str(out.relative_to(self.output_dir)),
                        cluster=-1,
                        size=cf.size,
                        confidence=Confidence.LOW.value,
                        category="raw",
                        evidence=cf.evidence,
                        exported=True,
                    )
                )
            self._scan_state["completed_stages"] = list(
                set(self._scan_state.get("completed_stages") or []) | {"raw"}
            )
            self.save_checkpoint(reason="stage_raw")

        ckpt = self.save_checkpoint(reason="final")
        # Kompatybilny JSON-podsumowanie
        json_ckpt = self.output_dir / "Raport" / "checkpoint.json"
        session = RecoverySession(
            image_path=str(self.image_path),
            output_dir=str(self.output_dir),
            target_name=self.target_name,
            volume_summary=vol.summary(),
            items=items,
            carved=carved,
            messages=recon.messages,
            stats=self._stats(items, recon),
            checkpoint_path=str(ckpt),
            profile_report=recon.profile_report or self.profiler.format_report(),
        )
        self._save_legacy_checkpoint(session, json_ckpt)
        # Zapisz profil
        profile_path = self.output_dir / "Raport" / "profile.txt"
        profile_path.write_text(session.profile_report, encoding="utf-8")

        if progress_cb:
            progress_cb(95, "Generuję raporty…")
        if self.telemetry:
            self.telemetry.update(stage="Generowanie wyników i raportu")
        write_reports(session, self.output_dir / "Raport")

        if progress_cb:
            progress_cb(100, "Gotowe")
        self.session = session
        if self.telemetry:
            self.telemetry.update(
                status="zakończone",
                stage="Zakończone",
                operation="Gotowe",
                overall_pct=100.0,
                stage_pct=100.0,
            )
        if self.logger:
            self.logger.info("Skanowanie zakończone: %s", session.stats)
            self.logger.info("\n%s", session.profile_report)
        self._scan_state["stage"] = "done"
        self._scan_state["completed_stages"] = list(
            set(self._scan_state.get("completed_stages") or []) | {"done"}
        )
        self.save_checkpoint(reason="completed")
        return session

    def cancel_and_save(self) -> Optional[Path]:
        """Anuluj po zapisaniu checkpointu."""
        path = None
        try:
            path = self.save_checkpoint(reason="cancel")
        except Exception as exc:
            if self.logger:
                self.logger.exception("Checkpoint przy anulowaniu nieudany: %s", exc)
            path = None
        self.request_cancel()
        return path

    def _export_other_root_entries(
        self,
        vol: Fat32Volume,
        recon: ReconstructionResult,
        *,
        known_offsets: Set[int],
    ) -> List[RecoveryItem]:
        unknown_root = self.output_dir / "Nieustalone_pochodzenie"
        items: List[RecoveryItem] = []
        try:
            root_entries = read_directory(
                vol, vol.bpb.root_cluster, include_deleted=True, tracker=self.tracker
            )
        except Exception:
            return items
        target_cl = recon.target_cluster
        for e in root_entries:
            if e.is_dot or e.is_volume_label:
                continue
            if names_match(e.name, self.target_name):
                continue
            if target_cl is not None and e.cluster == target_cl:
                continue
            if e.cluster < 2:
                continue
            if e.is_directory:
                nodes = build_tree_from_directory(
                    vol,
                    e.cluster,
                    e.name,
                    belongs_to_target=False,
                    tracker=self.tracker,
                )
                wrapper = RecoveredNode(
                    name=e.name,
                    path=e.name,
                    cluster=e.cluster,
                    size=0,
                    is_directory=True,
                    is_deleted=e.is_deleted,
                    confidence=Confidence.MEDIUM if e.is_deleted else Confidence.HIGH,
                    evidence=[
                        "wpis w katalogu głównym",
                        "brak dowodu przynależności do katalogu docelowego",
                    ],
                    belongs_to_target=False,
                    children=nodes,
                )
                items.extend(
                    self._export_tree(
                        vol,
                        [wrapper],
                        unknown_root,
                        category="unknown",
                        known_offsets=known_offsets,
                        force_unknown_bucket=True,
                    )
                )
            else:
                node = RecoveredNode(
                    name=e.name,
                    path=e.name,
                    cluster=e.cluster,
                    size=e.size,
                    is_directory=False,
                    is_deleted=e.is_deleted,
                    confidence=Confidence.MEDIUM if e.is_deleted else Confidence.HIGH,
                    evidence=[
                        "wpis w katalogu głównym",
                        "brak dowodu przynależności do katalogu docelowego",
                    ],
                    belongs_to_target=False,
                )
                items.extend(
                    self._export_tree(
                        vol,
                        [node],
                        unknown_root,
                        category="unknown",
                        known_offsets=known_offsets,
                        force_unknown_bucket=True,
                    )
                )
        return items

    def _export_tree(
        self,
        vol: Fat32Volume,
        nodes: List[RecoveredNode],
        dest_root: Path,
        *,
        category: str,
        known_offsets: Set[int],
        force_unknown_bucket: bool = False,
    ) -> List[RecoveryItem]:
        items: List[RecoveryItem] = []
        unknown_base = self.output_dir / "Nieustalone_pochodzenie"

        def export_node(node: RecoveredNode, parent_dir: Path) -> None:
            if self._cancelled():
                raise InterruptedError("Anulowano")
            name = safe_filename(node.name)
            already_under_unknown = unknown_base in parent_dir.parents or parent_dir == unknown_base
            if (
                (force_unknown_bucket and not already_under_unknown)
                or (category == "target" and not node.belongs_to_target)
            ):
                dest = unknown_base / name
                cat = "unknown"
            elif not node.belongs_to_target and category == "orphan":
                dest = parent_dir / name
                cat = "orphan"
            elif force_unknown_bucket or category == "unknown":
                dest = parent_dir / name
                cat = "unknown"
            else:
                dest = parent_dir / name
                cat = category

            if node.is_directory:
                dest.mkdir(parents=True, exist_ok=True)
                rel = str(dest.relative_to(self.output_dir))
                items.append(
                    RecoveryItem(
                        path=node.path,
                        output_path=rel,
                        cluster=node.cluster,
                        size=0,
                        confidence=node.confidence.value,
                        category=cat,
                        evidence=node.evidence,
                        is_directory=True,
                        is_deleted=node.is_deleted,
                        exported=True,
                    )
                )
                for ch in node.children:
                    export_node(ch, dest)
                return

            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stem, suf = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = dest.parent / f"{stem}_{n}{suf}"
                    n += 1

            written = self._write_file_from_clusters(vol, node, dest)
            if node.cluster >= 2:
                known_offsets.add(vol.cluster_offset(node.cluster))
            rel = str(dest.relative_to(self.output_dir))
            items.append(
                RecoveryItem(
                    path=node.path,
                    output_path=rel,
                    cluster=node.cluster,
                    size=written,
                    confidence=node.confidence.value,
                    category=cat,
                    evidence=node.evidence,
                    is_directory=False,
                    is_deleted=node.is_deleted,
                    exported=written > 0 or node.size == 0,
                )
            )

        for n in nodes:
            export_node(n, dest_root)
        return items

    def _write_file_from_clusters(
        self, vol: Fat32Volume, node: RecoveredNode, dest: Path
    ) -> int:
        if node.cluster < 2:
            dest.write_bytes(b"")
            return 0
        chain = vol.follow_chain(node.cluster)
        if not chain:
            chain = [node.cluster]
        remaining = node.size if node.size > 0 else len(chain) * vol.cluster_size
        written = 0
        with open(dest, "wb") as f:
            for c in chain:
                if remaining <= 0:
                    break
                data = vol.read_cluster(c)
                take = min(len(data), remaining)
                f.write(data[:take])
                written += take
                remaining -= take
        return written

    def _stats(self, items: List[RecoveryItem], recon: ReconstructionResult) -> Dict[str, int]:
        files = [i for i in items if not i.is_directory]
        return {
            "plikow_lacznie": len(files),
            "katalogow": sum(1 for i in items if i.is_directory),
            "w_docelowym": sum(1 for i in files if i.category == "target"),
            "osierocone": sum(1 for i in files if i.category == "orphan"),
            "nieustalone": sum(1 for i in files if i.category == "unknown"),
            "raw": sum(1 for i in files if i.category == "raw"),
            "wysokie": sum(1 for i in files if i.confidence == Confidence.HIGH.value),
            "srednie": sum(1 for i in files if i.confidence == Confidence.MEDIUM.value),
            "niskie": sum(1 for i in files if i.confidence == Confidence.LOW.value),
            "target_znaleziony": 1 if recon.target_found else 0,
            "target_intact": 1 if recon.target_directory_intact else 0,
            "podfoldery_bhp_szkoly": len(recon.target_linked_orphans),
            "pliki_w_podfolderach_bhp": recon.stats.get("target_linked_files", 0),
            "unique_clusters": recon.stats.get("unique_clusters_analyzed", 0),
            "cluster_reads": recon.stats.get("cluster_reads", 0),
            "rereads": recon.stats.get("rereads", 0),
        }

    def _save_legacy_checkpoint(self, session: RecoverySession, path: Path) -> None:
        data = {
            "image_path": session.image_path,
            "output_dir": session.output_dir,
            "target_name": session.target_name,
            "volume_summary": session.volume_summary,
            "messages": session.messages,
            "stats": session.stats,
            "sqlite_checkpoint": session.checkpoint_path,
            "items": [
                {
                    "path": i.path,
                    "output_path": i.output_path,
                    "cluster": i.cluster,
                    "size": i.size,
                    "confidence": i.confidence,
                    "category": i.category,
                    "evidence": i.evidence,
                    "is_directory": i.is_directory,
                    "is_deleted": i.is_deleted,
                    "exported": i.exported,
                }
                for i in session.items
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        if self._ckpt_store is not None:
            self._ckpt_store.close()
            self._ckpt_store = None
        if self.reader:
            self.reader.close()
            self.reader = None


def available_checkpoints(output_dir: str | Path) -> List[dict]:
    return list_checkpoints(default_checkpoint_dir(output_dir))
