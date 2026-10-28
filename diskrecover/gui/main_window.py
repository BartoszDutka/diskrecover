"""Główne okno DiskRecover — polski UI, ciemny motyw."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from diskrecover.core.checkpoint import ResultStore, default_checkpoint_dir
from diskrecover.core.recovery import RecoveryEngine, RecoveryMode, available_checkpoints
from diskrecover.gui.theme import DARK_QSS
from diskrecover.gui.workers import AnalyzeWorker, RecoverWorker
from diskrecover.i18n import UI_TRANSLATIONS

class CheckpointPickerDialog(QDialog):
    def __init__(self, checkpoints: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wznów poprzednie skanowanie")
        self.resize(640, 360)
        self.selected_path: str | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Wybierz zgodny checkpoint:"))
        self.list = QListWidget()
        for ck in checkpoints:
            meta = ck.get("meta") or {}
            ts = datetime.fromtimestamp(ck["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            size_mb = ck["size"] / (1024 * 1024)
            stage = meta.get("stage", "?")
            status = meta.get("status", "?")
            valid = "OK" if ck.get("valid") else "BŁĄD: " + "; ".join(ck.get("errors") or [])
            text = (
                f"{ts} | {size_mb:.2f} MB | etap={stage} | status={status} | {valid}\n"
                f"{ck['path']}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, ck["path"])
            if not ck.get("valid"):
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.list.addItem(item)
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        item = self.list.currentItem()
        if not item:
            QMessageBox.warning(self, "DiskRecover", "Wybierz checkpoint.")
            return
        self.selected_path = item.data(Qt.UserRole)
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, initial_image: str | None = None) -> None:
        super().__init__()
        self._language = "en"
        self.setWindowTitle("DiskRecover — FAT32 recovery")
        self.resize(1100, 760)
        self.setStyleSheet(DARK_QSS)

        self._analyze_worker: AnalyzeWorker | None = None
        self._recover_worker: RecoverWorker | None = None
        self._session = None
        self._last_snapshot = None
        self._scan_started_at = 0.0
        self._resume_checkpoint: str | None = None
        self._last_ckpt_label = "—"

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        brand = QLabel("DiskRecover")
        brand.setObjectName("Brand")
        sub = QLabel("Bezpieczne odzyskiwanie danych z obrazów FAT32 • tryb tylko do odczytu")
        sub.setObjectName("Subtitle")
        layout.addWidget(brand)
        layout.addWidget(sub)

        self.tabs = QTabWidget()
        tabs = self.tabs
        layout.addWidget(tabs, 1)

        tabs.addTab(self._build_source_tab(), "1. Obraz i parametry")
        tabs.addTab(self._build_scan_tab(), "2. Skanowanie")
        tabs.addTab(self._build_results_tab(), "3. Wyniki")
        tabs.addTab(self._build_about_tab(), "Informacje")

        self.status = QLabel("Gotowy. Obraz nie jest modyfikowany.")
        layout.addWidget(self.status)

        if initial_image:
            self.image_edit.setText(initial_image)

        self._retranslate_ui()
        self._refresh_resume_availability()

    def _retranslate_ui(self) -> None:
        """Apply the selected language to the currently visible UI."""
        if self._language == "en":
            translations = UI_TRANSLATIONS
            title = "DiskRecover — FAT32 recovery"
        else:
            translations = {value: key for key, value in UI_TRANSLATIONS.items()}
            title = "DiskRecover — odzyskiwanie FAT32"

        def translated(text: str) -> str:
            return translations.get(text, text)

        self.setWindowTitle(title)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, QAbstractButton):
                widget.setText(translated(widget.text()))
            elif isinstance(widget, QGroupBox):
                widget.setTitle(translated(widget.title()))
            elif isinstance(widget, QLabel):
                widget.setText(translated(widget.text()))
            elif isinstance(widget, QLineEdit):
                widget.setPlaceholderText(translated(widget.placeholderText()))
        for index in range(self.tabs.count()):
            self.tabs.setTabText(index, translated(self.tabs.tabText(index)))
        for index in range(self.mode_combo.count()):
            self.mode_combo.setItemText(index, translated(self.mode_combo.itemText(index)))
        self.table.setHorizontalHeaderLabels(
            [translated(text) for text in ["Ścieżka", "Wyjście", "Rozmiar", "Pewność", "Kategoria"]]
        )
        self.progress.setFormat(
            "Overall progress: %p%" if self._language == "en" else "Postęp całkowity: %p%"
        )
        self.stage_progress.setFormat(
            "Current stage progress" if self._language == "en" else "Postęp bieżącego etapu"
        )
        self.language_label.setText("Language:" if self._language == "en" else "Język:")
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(0 if self._language == "en" else 1)
        self.language_combo.blockSignals(False)
        self.about_text.setHtml(self._about_html())

    def _about_html(self) -> str:
        if self._language == "en":
            return """
            <h2 style="color:#7dd3c0">DiskRecover</h2>
            <p>Universal, non-destructive recovery from FAT32 disk images.</p>
            <ul>
              <li>Fully non-destructive — the image is never written</li>
              <li>FAT parameters (BPB) validated from the image</li>
              <li>Directory reconstruction, confidence levels, RAW carving</li>
              <li>SQLite checkpoints: pause / resume / automatic saving</li>
              <li>Separate confident, orphaned, RAW, and uncertain results</li>
            </ul>
            <p><b>Unsupported:</b> writing to the original media, in-place FAT repair,
            filesystems other than FAT32, and mounting disk images.</p>
            """
        return """
            <h2 style="color:#7dd3c0">DiskRecover</h2>
            <p>Uniwersalne, nieniszczące odzyskiwanie danych z obrazów FAT32.</p>
            <ul>
              <li>Całkowicie nieniszcząca — obraz nigdy nie jest zapisywany</li>
              <li>Parametry FAT (BPB) walidowane z obrazu</li>
              <li>Rekonstrukcja katalogów, poziomy pewności, carving RAW</li>
              <li>Checkpointy SQLite: pauza / wznowienie / auto-zapis</li>
              <li>Rozdzielenie wyników pewnych, osieroconych, RAW i nieustalonych</li>
            </ul>
            <p><b>Nieobsługiwane:</b> zapis na oryginalny nośnik, naprawa tablic FAT
            in-place, systemy plików inne niż FAT32 i montowanie obrazu.</p>
            """

    def _language_changed(self, index: int) -> None:
        self._language = "en" if index == 0 else "pl"
        self._retranslate_ui()

    def _build_source_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        box = QGroupBox("Plik obrazu dysku")
        form = QFormLayout(box)
        row = QHBoxLayout()
        self.image_edit = QLineEdit()
        browse = QPushButton("Przeglądaj…")
        browse.clicked.connect(self._browse_image)
        row.addWidget(self.image_edit, 1)
        row.addWidget(browse)
        form.addRow("Ścieżka:", row)

        self.out_edit = QLineEdit(str(Path.home() / "DiskRecover_output"))
        self.out_edit.textChanged.connect(lambda _=None: self._refresh_resume_availability())
        out_row = QHBoxLayout()
        out_browse = QPushButton("Przeglądaj…")
        out_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_browse)
        form.addRow("Katalog wyjściowy:", out_row)

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("Opcjonalnie: nazwa katalogu docelowego")
        form.addRow("Katalog docelowy:", self.target_edit)

        self.lba_spin = QSpinBox()
        self.lba_spin.setRange(0, 2_000_000_000)
        self.lba_spin.setSpecialValueText("auto (z MBR/BPB)")
        self.lba_spin.setValue(0)
        form.addRow("LBA start partycji:", self.lba_spin)

        self.ckpt_interval = QSpinBox()
        self.ckpt_interval.setRange(60, 3600)
        self.ckpt_interval.setValue(300)
        self.ckpt_interval.setSuffix(" s")
        form.addRow("Auto-checkpoint co:", self.ckpt_interval)

        hint = QLabel(
            "Parametry BPB (sektor, klaster, FAT) są zawsze odczytywane z obrazu. "
            "Pole LBA nadpisuje tylko lokalizację partycji, jeśli MBR jest uszkodzony."
        )
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        form.addRow(hint)
        v.addWidget(box)

        opts = QGroupBox("Opcje")
        ov = QVBoxLayout(opts)
        self.chk_raw = QCheckBox("Włącz odzyskiwanie RAW (sygnatury plików)")
        self.chk_raw.setChecked(True)
        self.chk_deep = QCheckBox("Głęboki skan klastrów (wolniejszy, dokładniejszy)")
        self.chk_deep.setChecked(True)
        ov.addWidget(self.chk_raw)
        ov.addWidget(self.chk_deep)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(
            "TARGET_ONLY — tylko wskazany katalog",
            RecoveryMode.TARGET_ONLY.value,
        )
        self.mode_combo.addItem(
            "FULL_RECOVERY — cały wolumin",
            RecoveryMode.FULL_RECOVERY.value,
        )
        ov.addWidget(QLabel("Tryb odzyskiwania:"))
        ov.addWidget(self.mode_combo)
        note = QLabel(
            "Pliki RAW trafiają wyłącznie do folderu RAW/ i nigdy nie są przypisywane "
            "do wskazanego katalogu bez dowodu."
        )
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        ov.addWidget(note)
        v.addWidget(opts)

        btns = QHBoxLayout()
        self.btn_analyze = QPushButton("Analizuj obraz")
        self.btn_analyze.setObjectName("Primary")
        self.btn_analyze.clicked.connect(self._start_analyze)
        btns.addWidget(self.btn_analyze)
        btns.addStretch()
        v.addLayout(btns)

        self.analyze_log = QTextEdit()
        self.analyze_log.setReadOnly(True)
        self.analyze_log.setPlaceholderText("Wynik analizy MBR/BPB pojawi się tutaj…")
        v.addWidget(self.analyze_log, 1)
        return w

    def _build_scan_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel(
            "Skanowanie jest nieniszczące: obraz otwierany jest wyłącznie w trybie odczytu (rb/mmap). "
            "Checkpointy SQLite pozwalają wstrzymać i wznowić bez powtarzania ukończonych etapów."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Postęp całkowity: %p%")
        v.addWidget(self.progress)

        self.stage_progress = QProgressBar()
        self.stage_progress.setRange(0, 100)
        self.stage_progress.setFormat("Postęp bieżącego etapu")
        v.addWidget(self.stage_progress)

        self.scanner_indicator = QLabel("● GOTOWY")
        self.scanner_indicator.setStyleSheet("color: #7dd3c0; font-weight: bold")
        v.addWidget(self.scanner_indicator)
        self.scan_stats = QLabel("Brak aktywnego skanowania.")
        self.scan_stats.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scan_stats.setWordWrap(True)
        v.addWidget(self.scan_stats)

        self.ckpt_info = QLabel("Ostatni checkpoint: —")
        self.ckpt_info.setObjectName("Subtitle")
        v.addWidget(self.ckpt_info)

        self.scan_status = QLabel("Oczekiwanie…")
        v.addWidget(self.scan_status)

        self.scan_log = QTextEdit()
        self.scan_log.setReadOnly(True)
        v.addWidget(self.scan_log, 1)

        row = QHBoxLayout()
        self.btn_start = QPushButton("Start odzyskiwania")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start_recover)

        self.btn_resume_prev = QPushButton("Wznów poprzednie skanowanie")
        self.btn_resume_prev.clicked.connect(self._resume_previous)
        self.btn_resume_prev.setEnabled(False)

        self.btn_save = QPushButton("Zapisz postęp")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_progress)

        self.btn_pause = QPushButton("Wstrzymaj")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._pause_recover)

        self.btn_resume = QPushButton("Wznów")
        self.btn_resume.setEnabled(False)
        self.btn_resume.clicked.connect(self._resume_recover)

        self.btn_cancel_save = QPushButton("Anuluj i zapisz")
        self.btn_cancel_save.setObjectName("Danger")
        self.btn_cancel_save.setEnabled(False)
        self.btn_cancel_save.clicked.connect(self._cancel_and_save)

        self.btn_cancel = QPushButton("Anuluj")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_recover)

        self.btn_open_ckpt = QPushButton("Otwórz folder checkpointów")
        self.btn_open_ckpt.clicked.connect(self._open_checkpoints)

        self.btn_open_log = QPushButton("Otwórz log diagnostyczny")
        self.btn_open_log.clicked.connect(self._open_log)

        for b in (
            self.btn_start,
            self.btn_resume_prev,
            self.btn_save,
            self.btn_pause,
            self.btn_resume,
            self.btn_cancel_save,
            self.btn_cancel,
            self.btn_open_ckpt,
            self.btn_open_log,
        ):
            row.addWidget(b)
        v.addLayout(row)
        return w

    def _build_results_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.stats_label = QLabel("Brak wyników — uruchom skanowanie.")
        self.stats_label.setWordWrap(True)
        v.addWidget(self.stats_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Ścieżka", "Wyjście", "Rozmiar", "Pewność", "Kategoria"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Odśwież wyniki")
        refresh_btn.clicked.connect(self._refresh_live_results)
        load_btn = QPushButton("Wczytaj wyniki z checkpointu")
        load_btn.clicked.connect(self._load_checkpoint_results)
        export_selected_btn = QPushButton("Odzyskaj zaznaczone")
        export_selected_btn.clicked.connect(self._export_selected_results)
        export_target_btn = QPushButton("Odzyskaj powiązane z katalogiem docelowym")
        export_target_btn.clicked.connect(self._export_target_results)
        open_btn = QPushButton("Otwórz katalog wyjściowy")
        open_btn.clicked.connect(self._open_output)
        report_btn = QPushButton("Otwórz raport HTML")
        report_btn.clicked.connect(self._open_report)
        row.addWidget(refresh_btn)
        row.addWidget(load_btn)
        row.addWidget(export_selected_btn)
        row.addWidget(export_target_btn)
        row.addWidget(open_btn)
        row.addWidget(report_btn)
        row.addStretch()
        v.addLayout(row)
        return w

    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        language_row = QHBoxLayout()
        self.language_label = QLabel("Language:")
        self.language_combo = QComboBox()
        self.language_combo.addItems(["English", "Polski"])
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        language_row.addWidget(self.language_label)
        language_row.addWidget(self.language_combo)
        language_row.addStretch()
        v.addLayout(language_row)
        self.about_text = QTextEdit()
        self.about_text.setReadOnly(True)
        v.addWidget(self.about_text)
        return w

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Wybierz obraz dysku",
            self.image_edit.text(),
            "Obrazy (*.bin *.img *.dd *.raw);;Wszystkie (*.*)",
        )
        if path:
            self.image_edit.setText(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Katalog wyjściowy", self.out_edit.text())
        if path:
            self.out_edit.setText(path)

    def _partition_lba(self) -> int | None:
        v = self.lba_spin.value()
        return None if v == 0 else v

    def _refresh_resume_availability(self) -> None:
        out = self.out_edit.text().strip()
        if not out:
            self.btn_resume_prev.setEnabled(False)
            return
        ckpts = available_checkpoints(out)
        self.btn_resume_prev.setEnabled(any(c.get("valid") for c in ckpts))

    def _start_analyze(self) -> None:
        image = self.image_edit.text().strip()
        if not image:
            QMessageBox.warning(self, "DiskRecover", "Podaj ścieżkę do obrazu.")
            return
        self.btn_analyze.setEnabled(False)
        self.analyze_log.clear()
        self.status.setText("Analiza w toku…")
        self._analyze_worker = AnalyzeWorker(image, self._partition_lba())
        self._analyze_worker.progress.connect(self._on_analyze_progress)
        self._analyze_worker.finished_ok.connect(self._on_analyze_ok)
        self._analyze_worker.failed.connect(self._on_analyze_fail)
        self._analyze_worker.start()

    def _on_analyze_progress(self, pct: int, msg: str) -> None:
        self.analyze_log.append(f"[{pct}%] {msg}")

    def _on_analyze_ok(self, summary: dict) -> None:
        self.btn_analyze.setEnabled(True)
        self.status.setText("Analiza zakończona.")
        lines = ["=== Podsumowanie wolumenu (z obrazu) ==="]
        for k, v in summary.items():
            if k == "partitions":
                lines.append("Partycje:")
                for p in v:
                    lines.append(
                        f"  #{p['index']} {p['type']} LBA={p['lba']} sectors={p['sectors']}"
                    )
            else:
                lines.append(f"{k}: {v}")
        self.analyze_log.append("\n".join(lines))

    def _on_analyze_fail(self, err: str) -> None:
        self.btn_analyze.setEnabled(True)
        self.status.setText("Błąd analizy.")
        self.analyze_log.append(f"BŁĄD: {err}")
        QMessageBox.critical(self, "DiskRecover", f"Analiza nieudana:\n{err}")

    def _set_scan_controls(self, running: bool, paused: bool = False) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_resume_prev.setEnabled(not running and self.btn_resume_prev.isEnabled())
        if not running:
            self._refresh_resume_availability()
        self.btn_save.setEnabled(running)
        self.btn_pause.setEnabled(running and not paused)
        self.btn_resume.setEnabled(running and paused)
        self.btn_cancel.setEnabled(running)
        self.btn_cancel_save.setEnabled(running)

    def _start_recover(self, *, resume_checkpoint: str | None = None) -> None:
        image = self.image_edit.text().strip()
        out = self.out_edit.text().strip()
        if not image or not out:
            QMessageBox.warning(self, "DiskRecover", "Podaj obraz i katalog wyjściowy.")
            return
        self._resume_checkpoint = resume_checkpoint
        self._set_scan_controls(True, paused=False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_progress.setRange(0, 100)
        self.stage_progress.setValue(0)
        self.scan_log.clear()
        self.status.setText("Odzyskiwanie w toku…")
        self._recover_worker = RecoverWorker(
            image,
            out,
            self.target_edit.text().strip(),
            self._partition_lba(),
            self.chk_raw.isChecked(),
            self.chk_deep.isChecked(),
            resume_checkpoint=resume_checkpoint,
            checkpoint_interval_sec=float(self.ckpt_interval.value()),
            mode=RecoveryMode(self.mode_combo.currentData()),
        )
        self._recover_worker.progress.connect(self._on_recover_progress)
        self._recover_worker.telemetry.connect(self._on_telemetry)
        self._recover_worker.checkpoint_saved.connect(self._on_checkpoint_saved)
        self._recover_worker.finished_ok.connect(self._on_recover_ok)
        self._recover_worker.failed.connect(self._on_recover_fail)
        self._recover_worker.start()
        self._scan_started_at = time.monotonic()
        if not hasattr(self, "_heartbeat_timer"):
            self._heartbeat_timer = QTimer(self)
            self._heartbeat_timer.timeout.connect(self._check_heartbeat)
        self._heartbeat_timer.start(1000)

    def _resume_previous(self) -> None:
        out = self.out_edit.text().strip()
        ckpts = [c for c in available_checkpoints(out) if c.get("valid")]
        if not ckpts:
            QMessageBox.information(self, "DiskRecover", "Brak zgodnych checkpointów.")
            return
        dlg = CheckpointPickerDialog(ckpts, self)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        self._start_recover(resume_checkpoint=dlg.selected_path)

    def _save_progress(self) -> None:
        if self._recover_worker:
            self._recover_worker.save_progress()
            self.scan_status.setText("Żądanie zapisu checkpointu…")

    def _pause_recover(self) -> None:
        if self._recover_worker:
            self._recover_worker.pause()
            self._set_scan_controls(True, paused=True)
            self.scanner_indicator.setText("● WSTRZYMANE")
            self.scanner_indicator.setStyleSheet("color: #f0ad4e; font-weight: bold")
            self.scan_status.setText("Skanowanie wstrzymane (możesz zapisać postęp).")

    def _resume_recover(self) -> None:
        if self._recover_worker:
            self._recover_worker.resume()
            self._set_scan_controls(True, paused=False)
            self.scanner_indicator.setText("● SKANOWANIE")
            self.scanner_indicator.setStyleSheet("color: #7dd3c0; font-weight: bold")
            self.scan_status.setText("Wznowiono skanowanie…")

    def _cancel_recover(self) -> None:
        if self._recover_worker:
            self._recover_worker.cancel()
            self.scan_status.setText("Anulowanie…")

    def _cancel_and_save(self) -> None:
        if self._recover_worker:
            self._recover_worker.cancel_and_save()
            self.scan_status.setText("Anulowanie z zapisem checkpointu…")

    def _on_checkpoint_saved(self, path: str, saved_at: float) -> None:
        ts = datetime.fromtimestamp(saved_at).strftime("%Y-%m-%d %H:%M:%S")
        self._last_ckpt_label = f"{ts} → {path}"
        self.ckpt_info.setText(f"Ostatni checkpoint: {self._last_ckpt_label}")
        self.scan_log.append(f"CHECKPOINT zapisany: {path} ({ts})")

    def _on_recover_progress(self, pct: int, msg: str) -> None:
        if pct < 0:
            self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(pct)
        self.scan_status.setText(msg)
        # ogranicz spam w logu
        if not hasattr(self, "_last_log_msg") or self._last_log_msg != msg:
            self.scan_log.append(f"[{pct}%] {msg}")
            self._last_log_msg = msg

    def _on_recover_ok(self, session) -> None:
        if hasattr(self, "_heartbeat_timer"):
            self._heartbeat_timer.stop()
        self._set_scan_controls(False)
        self._session = session
        self.status.setText("Odzyskiwanie zakończone.")
        self._fill_results(session)
        QMessageBox.information(
            self,
            "DiskRecover",
            f"Gotowe.\nPliki: {session.stats.get('plikow_lacznie', 0)}\n"
            f"Podfoldery powiązane z celem: {session.stats.get('target_subdirectories', 0)}\n"
            f"Raport: {session.output_dir}/Raport/",
        )

    def _on_recover_fail(self, err: str) -> None:
        if hasattr(self, "_heartbeat_timer"):
            self._heartbeat_timer.stop()
        self._set_scan_controls(False)
        self.status.setText("Błąd / anulowano.")
        self.scan_log.append(f"BŁĄD: {err}")
        QMessageBox.warning(self, "DiskRecover", err)

    def _on_telemetry(self, snapshot) -> None:
        self._last_snapshot = snapshot
        self._update_scan_stats(snapshot)

    def _update_scan_stats(self, snapshot) -> None:
        # Postęp etapu: kandydaci jeśli znane, inaczej nieokreślony
        if snapshot.stage_pct is not None:
            if self.stage_progress.maximum() == 0:
                self.stage_progress.setRange(0, 100)
            self.stage_progress.setValue(int(min(100, max(0, snapshot.stage_pct))))
            if snapshot.candidates_total:
                done = snapshot.candidates_total - snapshot.candidates_remaining
                candidate_pct = 100.0 * done / max(snapshot.candidates_total, 1)
                self.stage_progress.setFormat(
                    f"Etap: {candidate_pct:.1f}% "
                    f"({done}/"
                    f"{snapshot.candidates_total} kandydatów)"
                )
            else:
                self.stage_progress.setFormat(f"Etap: {int(snapshot.stage_pct)}%")
        elif snapshot.candidates_total > 0:
            if self.stage_progress.maximum() == 0:
                self.stage_progress.setRange(0, 100)
            done = snapshot.candidates_total - snapshot.candidates_remaining
            pct = int(100 * done / max(snapshot.candidates_total, 1))
            self.stage_progress.setValue(pct)
            self.stage_progress.setFormat(
                f"Kandydaci: {done}/{snapshot.candidates_total}"
            )
        else:
            # Nieznana liczba operacji — pasek nieokreślony, nie fikcyjny %
            self.stage_progress.setRange(0, 0)
            self.stage_progress.setFormat("Etap w toku (liczba operacji nieznana)")

        if snapshot.overall_pct is not None:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(int(min(100, max(0, snapshot.overall_pct))))

        elapsed = max(0.0, time.monotonic() - snapshot.started_at)
        age = max(0.0, time.monotonic() - snapshot.last_activity)
        mb = snapshot.bytes_read / (1024 * 1024)
        speed = mb / elapsed if elapsed else 0.0
        rate = snapshot.analysis_rate
        sc = snapshot.stage_counters or {}

        self.scan_stats.setText(
            f"Etap: {snapshot.stage}\n"
            f"Operacja: {snapshot.operation}\n"
            f"Postęp etapu: kandydaci "
            f"{snapshot.candidates_total - snapshot.candidates_remaining}/"
            f"{snapshot.candidates_total or '—'}"
            f" (pozostało {snapshot.candidates_remaining or '—'})\n"
            f"Aktualny klaster: {snapshot.cluster or '-'} | "
            f"LBA: {snapshot.lba or '-'}\n"
            f"Unikalne klastry: {snapshot.unique_clusters_analyzed:,} | "
            f"Odczyty: {snapshot.cluster_reads:,} | "
            f"Ponowne odczyty: {snapshot.rereads:,} | "
            f"Łańcuchy FAT: {snapshot.fat_chains_checked:,}\n"
            f"Przeczytane dane: {mb:,.2f} MB | Prędkość I/O: {speed:,.1f} MB/s | "
            f"Tempo analizy: {rate:,.1f}/s\n"
            f"Kandydaci orphan: {snapshot.orphan_candidates:,} | "
            f"Sprawdzeni: {snapshot.orphan_candidates_checked:,} | "
            f"Odrzuceni: {snapshot.orphan_candidates_rejected:,} | "
            f"Potwierdzeni: {snapshot.orphan_directories_confirmed:,}\n"
            f"Rekordy FAT32: {snapshot.records_checked:,} | "
            f"Odrzucone: {snapshot.records_rejected:,} | "
            f"Poprawne wpisy: {snapshot.valid_entries:,}\n"
            f"Potwierdzone katalogi: {snapshot.confirmed_directories:,} | "
            f"Potwierdzone pliki: {snapshot.confirmed_files:,}\n"
            f"Podfoldery powiązane z celem: {snapshot.target_linked_subdirs:,} | "
            f"Pliki w potwierdzonych podfolderach: {snapshot.target_linked_files:,}\n"
            f"Unikalne obiekty: {snapshot.unique_objects:,} | "
            f"Uszkodzenia: {snapshot.corruptions:,}\n"
            f"Czas: {elapsed:,.1f} s | Ostatnia aktywność: {age:,.1f} s temu\n"
            f"Etap (liczniki lokalne): {sc}"
        )
        if snapshot.status == "wstrzymane":
            self.scanner_indicator.setText("● WSTRZYMANE")
            self.scanner_indicator.setStyleSheet("color: #f0ad4e; font-weight: bold")
        else:
            self.scanner_indicator.setText("● SKANOWANIE")
            self.scanner_indicator.setStyleSheet("color: #7dd3c0; font-weight: bold")

    def _check_heartbeat(self) -> None:
        snapshot = self._last_snapshot
        if snapshot is None:
            return
        if snapshot.status == "wstrzymane":
            return
        age = time.monotonic() - snapshot.last_activity
        if age >= 30:
            self.scanner_indicator.setText("● BRAK AKTYWNOŚCI — SPRAWDŹ LOG")
            self.scanner_indicator.setStyleSheet("color: #f0ad4e; font-weight: bold")
        elif age >= 3:
            self.scanner_indicator.setText("● AKTYWNE, BEZ NOWYCH WYNIKÓW")
            self.scanner_indicator.setStyleSheet("color: #f0ad4e; font-weight: bold")
        elif snapshot.status == "zakończone":
            self.scanner_indicator.setText("● ZAKOŃCZONE")

    def _open_log(self) -> None:
        path = Path(self.out_edit.text().strip()) / "Raport" / "scanner_debug.log"
        if path.is_file():
            self._open_path(str(path))
        else:
            QMessageBox.information(
                self, "DiskRecover", "Log zostanie utworzony po rozpoczęciu skanowania."
            )

    def _open_checkpoints(self) -> None:
        path = default_checkpoint_dir(self.out_edit.text().strip())
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(str(path))

    def _fill_results(self, session) -> None:
        s = session.stats
        self.stats_label.setText(
            f"Pliki: {s.get('plikow_lacznie',0)} | "
            f"Docelowy: {s.get('w_docelowym',0)} | "
            f"Osierocone: {s.get('osierocone',0)} | "
            f"Nieustalone: {s.get('nieustalone',0)} | "
            f"RAW: {s.get('raw',0)} | "
            f"Podfoldery celu: {s.get('target_subdirectories',0)} | "
            f"Pliki w podfolderach: {s.get('target_files_in_subdirectories',0)} | "
            f"Pewność ↑ {s.get('wysokie',0)} / ↔ {s.get('srednie',0)} / ↓ {s.get('niskie',0)}"
        )
        files = [i for i in session.items if not i.is_directory]
        self.table.setRowCount(len(files))
        for row, item in enumerate(files):
            self.table.setItem(row, 0, QTableWidgetItem(item.path))
            self.table.setItem(row, 1, QTableWidgetItem(item.output_path))
            self.table.setItem(row, 2, QTableWidgetItem(str(item.size)))
            self.table.setItem(row, 3, QTableWidgetItem(item.confidence))
            self.table.setItem(row, 4, QTableWidgetItem(item.category))

    def _load_checkpoint_results(self) -> None:
        out = self.out_edit.text().strip()
        checkpoints = [c for c in available_checkpoints(out) if c.get("valid")]
        if not checkpoints:
            QMessageBox.information(
                self, "DiskRecover", "Brak poprawnych checkpointów."
            )
            return
        dlg = CheckpointPickerDialog(checkpoints, self)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        try:
            session = RecoveryEngine.load_partial_session(dlg.selected_path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self, "DiskRecover", f"Nie można wczytać wyników:\n{exc}"
            )
            return
        self._session = session
        self._fill_results(session)
        self.status.setText("Wczytano snapshot wyników z checkpointu.")
        if not session.items:
            self.stats_label.setText(
                self.stats_label.text()
                + "\nCheckpoint zawiera wyłącznie agregaty; brak zapisanych pełnych rekordów."
            )

    def _refresh_live_results(self) -> None:
        path = Path(self.out_edit.text().strip()) / "Raport" / "results.sqlite"
        if not path.is_file():
            QMessageBox.information(self, "DiskRecover", "Brak bazy bieżących wyników.")
            return
        try:
            session = RecoveryEngine.load_live_results(path)
            store = ResultStore(path)
            store.open()
            try:
                deferred = len(store.list_candidates("DEFERRED"))
                stored = len(store.list_objects())
            finally:
                store.close()
            self._session = session
            self._fill_results(session)
            self.stats_label.setText(
                self.stats_label.text()
                + f" | Zapisane obiekty: {stored} | Odroczone katalogi: {deferred}"
            )
            self.status.setText("Odświeżono wyniki zapisane w SQLite.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "DiskRecover", f"Nie można odświeżyć wyników:\n{exc}")

    def _export_selected_results(self) -> None:
        rows = {item.row() for item in self.table.selectedItems()}
        if not rows:
            QMessageBox.information(self, "DiskRecover", "Zaznacz plik na liście.")
            return
        paths = {
            self.table.item(row, 0).text()
            for row in rows
            if self.table.item(row, 0)
        }
        self._export_live(paths=paths)

    def _export_target_results(self) -> None:
        self._export_live(categories={"target"})

    def _export_live(self, *, paths=None, categories=None) -> None:
        image = self.image_edit.text().strip()
        out = self.out_edit.text().strip()
        try:
            engine = RecoveryEngine(image, out, target_name=self.target_edit.text().strip())
            try:
                session = engine.export_live_results(
                    categories=categories, paths=set(paths) if paths else None
                )
            finally:
                engine.close()
            self._session = session
            self._fill_results(session)
            self.status.setText("Zakończono próbę odzyskiwania zapisanych wyników.")
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "DiskRecover", f"Eksport nieudany:\n{exc}")

    def _open_output(self) -> None:
        path = self.out_edit.text().strip()
        if path:
            self._open_path(path)

    def _open_report(self) -> None:
        path = Path(self.out_edit.text().strip()) / "Raport" / "raport.html"
        if path.is_file():
            self._open_path(str(path))
        else:
            QMessageBox.information(
                self, "DiskRecover", "Brak raportu — najpierw uruchom odzyskiwanie."
            )

    def _open_path(self, path: str) -> None:
        import os
        import subprocess
        import sys

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "DiskRecover", f"Nie można otworzyć:\n{e}")
