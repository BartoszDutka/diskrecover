"""Wątki QThread — skanowanie bez blokowania GUI."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from diskrecover.core.recovery import RecoveryEngine
from diskrecover.core.telemetry import log_exception


class AnalyzeWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, image_path: str, partition_lba: int | None = None) -> None:
        super().__init__()
        self.image_path = image_path
        self.partition_lba = partition_lba

    def run(self) -> None:
        try:
            from diskrecover.core.image import ImageReader
            from diskrecover.core.mbr import parse_mbr, find_fat32_partition
            from diskrecover.core.fat32 import Fat32Volume

            self.progress.emit(10, "Otwieram obraz…")
            with ImageReader(self.image_path) as reader:
                parts = [
                    {
                        "index": p.index,
                        "type": p.type_name,
                        "type_id": p.type_id,
                        "lba": p.lba_start,
                        "sectors": p.sector_count,
                    }
                    for p in parse_mbr(reader)
                ]
                self.progress.emit(40, "Czytam BPB…")
                part = find_fat32_partition(reader, preferred_lba=self.partition_lba)
                offset = 0
                if self.partition_lba is not None:
                    offset = self.partition_lba * 512
                elif part is not None:
                    offset = part.byte_offset
                vol = Fat32Volume.open(reader, offset)
                summary = vol.summary()
                summary["partitions"] = parts
                summary["image_size"] = reader.size
                self.progress.emit(100, "Analiza zakończona")
                self.finished_ok.emit(summary)
        except Exception as e:
            self.failed.emit(str(e))


class RecoverWorker(QThread):
    progress = Signal(int, str)
    telemetry = Signal(object)
    checkpoint_saved = Signal(str, float)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        image_path: str,
        output_dir: str,
        target_name: str,
        partition_lba: int | None,
        enable_raw: bool,
        deep_scan: bool,
        *,
        resume_checkpoint: str | None = None,
        checkpoint_interval_sec: float = 300.0,
    ) -> None:
        super().__init__()
        self.engine = RecoveryEngine(
            image_path,
            output_dir,
            target_name=target_name,
            partition_lba=partition_lba,
            enable_raw=enable_raw,
            deep_scan=deep_scan,
            resume_checkpoint=resume_checkpoint,
            checkpoint_interval_sec=checkpoint_interval_sec,
        )
        self._last_telemetry_emit = 0.0
        self._last_stage = ""
        self._cancel_with_save = False

    def cancel(self) -> None:
        self.engine.request_cancel()

    def cancel_and_save(self) -> None:
        self._cancel_with_save = True
        self.engine.cancel_and_save()

    def pause(self) -> None:
        self.engine.request_pause()

    def resume(self) -> None:
        self.engine.request_resume()

    def save_progress(self) -> None:
        self.engine.request_checkpoint()
        # jeśli worker stoi na pauzie, zapisz synchronicznie
        if self.engine.is_paused():
            try:
                path = self.engine.save_checkpoint(reason="manual")
                self.checkpoint_saved.emit(str(path), time.time())
            except Exception as e:
                self.failed.emit(f"Zapis checkpointu nieudany: {e}")

    def run(self) -> None:
        try:
            def emit_telemetry(snapshot) -> None:
                now = time.monotonic()
                if (
                    now - self._last_telemetry_emit >= 0.5
                    or snapshot.stage != self._last_stage
                    or snapshot.status in ("zakończone", "błąd", "wstrzymane")
                ):
                    self._last_telemetry_emit = now
                    self._last_stage = snapshot.stage
                    self.telemetry.emit(snapshot)

            def on_ckpt(path: str, saved_at: float) -> None:
                self.checkpoint_saved.emit(path, saved_at)

            session = self.engine.run(
                progress_cb=lambda p, m: self.progress.emit(p, m),
                telemetry_cb=emit_telemetry,
                checkpoint_cb=on_ckpt,
            )
            self.finished_ok.emit(session)
        except InterruptedError:
            msg = "Anulowano przez użytkownika."
            if self._cancel_with_save:
                ckpt = self.engine._checkpoint_path()
                if ckpt.is_file():
                    msg = f"Anulowano. Checkpoint zapisany: {ckpt}"
                else:
                    msg = "Anulowano. Nie udało się potwierdzić zapisu checkpointu."
            self.failed.emit(msg)
        except Exception as e:
            if self.engine.logger:
                log_exception(self.engine.logger, "Błąd skanowania")
            self.failed.emit(str(e))
        finally:
            self.engine.close()
