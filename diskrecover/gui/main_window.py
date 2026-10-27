"""Główne okno DiskRecover — polski UI, ciemny motyw."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

from diskrecover.gui.theme import DARK_QSS
from diskrecover.gui.workers import AnalyzeWorker, RecoverWorker

DEFAULT_IMAGE = r"C:\Users\bdutka\Desktop\Odzyskiwanie_BHP\pendrive_bhp.bin"
DEFAULT_TARGET = "bhp szkoły"


class MainWindow(QMainWindow):
    def __init__(self, initial_image: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("DiskRecover — odzyskiwanie FAT32")
        self.resize(1100, 720)
        self.setStyleSheet(DARK_QSS)

        self._analyze_worker: AnalyzeWorker | None = None
        self._recover_worker: RecoverWorker | None = None
        self._session = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Brand
        brand = QLabel("DiskRecover")
        brand.setObjectName("Brand")
        sub = QLabel("Bezpieczne odzyskiwanie danych z obrazów FAT32 • tryb tylko do odczytu")
        sub.setObjectName("Subtitle")
        layout.addWidget(brand)
        layout.addWidget(sub)

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        tabs.addTab(self._build_source_tab(), "1. Obraz i parametry")
        tabs.addTab(self._build_scan_tab(), "2. Skanowanie")
        tabs.addTab(self._build_results_tab(), "3. Wyniki")
        tabs.addTab(self._build_about_tab(), "Informacje")

        # Status
        self.status = QLabel("Gotowy. Obraz nie jest modyfikowany.")
        layout.addWidget(self.status)

        if initial_image:
            self.image_edit.setText(initial_image)
        else:
            self.image_edit.setText(DEFAULT_IMAGE)

    # ---- tabs ----
    def _build_source_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        box = QGroupBox("Plik obrazu pendrive")
        form = QFormLayout(box)
        row = QHBoxLayout()
        self.image_edit = QLineEdit()
        browse = QPushButton("Przeglądaj…")
        browse.clicked.connect(self._browse_image)
        row.addWidget(self.image_edit, 1)
        row.addWidget(browse)
        form.addRow("Ścieżka:", row)

        self.out_edit = QLineEdit(str(Path.home() / "DiskRecover_output"))
        out_row = QHBoxLayout()
        out_browse = QPushButton("Przeglądaj…")
        out_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_browse)
        form.addRow("Katalog wyjściowy:", out_row)

        self.target_edit = QLineEdit(DEFAULT_TARGET)
        form.addRow("Katalog docelowy:", self.target_edit)

        self.lba_spin = QSpinBox()
        self.lba_spin.setRange(0, 2_000_000_000)
        self.lba_spin.setSpecialValueText("auto (z MBR/BPB)")
        self.lba_spin.setValue(0)
        form.addRow("LBA start partycji:", self.lba_spin)
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
        note = QLabel(
            "Pliki RAW trafiają wyłącznie do folderu RAW/ i nigdy nie są przypisywane "
            "do „bhp szkoły” bez dowodu. Niepewne → Nieustalone_pochodzenie/."
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
            "Możesz anulować w trakcie — częściowe wyniki i checkpoint zapiszą się w Raport/."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        v.addWidget(self.progress)

        self.scan_status = QLabel("Oczekiwanie…")
        v.addWidget(self.scan_status)

        self.scan_log = QTextEdit()
        self.scan_log.setReadOnly(True)
        v.addWidget(self.scan_log, 1)

        row = QHBoxLayout()
        self.btn_start = QPushButton("Start odzyskiwania")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start_recover)
        self.btn_cancel = QPushButton("Anuluj")
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_recover)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_cancel)
        row.addStretch()
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
        open_btn = QPushButton("Otwórz katalog wyjściowy")
        open_btn.clicked.connect(self._open_output)
        report_btn = QPushButton("Otwórz raport HTML")
        report_btn.clicked.connect(self._open_report)
        row.addWidget(open_btn)
        row.addWidget(report_btn)
        row.addStretch()
        v.addLayout(row)
        return w

    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        t = QTextEdit()
        t.setReadOnly(True)
        t.setHtml(
            """
            <h2 style="color:#7dd3c0">DiskRecover</h2>
            <p>Aplikacja dla Bartosza Dutki — odzyskiwanie uszkodzonego katalogu
            <b>bhp szkoły</b> z obrazu pendrive FAT32 (Windows 11).</p>
            <ul>
              <li>Całkowicie nieniszcząca — obraz nigdy nie jest zapisywany</li>
              <li>Parametry FAT (BPB) walidowane z obrazu, nie z notatek</li>
              <li>Rekonstrukcja katalogów, poziomy pewności, carving RAW</li>
              <li>Układ wyjścia: Odtworzony_bhp_szkoly/, Osierocone_katalogi/,
                  RAW/, Nieustalone_pochodzenie/, Raport/</li>
            </ul>
            <p><b>Nieobsługiwane:</b> zapis na oryginalny nośnik, naprawa tablic FAT in-place,
            systemy plików inne niż FAT32, montowanie obrazu.</p>
            <p>Domyślna ścieżka obrazu (przykład):
            <code>C:\\Users\\bdutka\\Desktop\\Odzyskiwanie_BHP\\pendrive_bhp.bin</code></p>
            """
        )
        v.addWidget(t)
        return w

    # ---- actions ----
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
        # Ustaw spin jeśli znaleziono offset
        off = summary.get("partition_offset", 0)
        if off and self.lba_spin.value() == 0:
            # Nie nadpisuj auto — tylko pokaż w logu
            pass

    def _on_analyze_fail(self, err: str) -> None:
        self.btn_analyze.setEnabled(True)
        self.status.setText("Błąd analizy.")
        self.analyze_log.append(f"BŁĄD: {err}")
        QMessageBox.critical(self, "DiskRecover", f"Analiza nieudana:\n{err}")

    def _start_recover(self) -> None:
        image = self.image_edit.text().strip()
        out = self.out_edit.text().strip()
        if not image or not out:
            QMessageBox.warning(self, "DiskRecover", "Podaj obraz i katalog wyjściowy.")
            return
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self.scan_log.clear()
        self.status.setText("Odzyskiwanie w toku…")
        self._recover_worker = RecoverWorker(
            image,
            out,
            self.target_edit.text().strip() or DEFAULT_TARGET,
            self._partition_lba(),
            self.chk_raw.isChecked(),
            self.chk_deep.isChecked(),
        )
        self._recover_worker.progress.connect(self._on_recover_progress)
        self._recover_worker.finished_ok.connect(self._on_recover_ok)
        self._recover_worker.failed.connect(self._on_recover_fail)
        self._recover_worker.start()

    def _cancel_recover(self) -> None:
        if self._recover_worker:
            self._recover_worker.cancel()
            self.scan_status.setText("Anulowanie…")

    def _on_recover_progress(self, pct: int, msg: str) -> None:
        self.progress.setValue(pct)
        self.scan_status.setText(msg)
        self.scan_log.append(f"[{pct}%] {msg}")

    def _on_recover_ok(self, session) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._session = session
        self.status.setText("Odzyskiwanie zakończone.")
        self._fill_results(session)
        QMessageBox.information(
            self,
            "DiskRecover",
            f"Gotowe.\nPliki: {session.stats.get('plikow_lacznie', 0)}\n"
            f"Raport: {session.output_dir}/Raport/",
        )

    def _on_recover_fail(self, err: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.status.setText("Błąd / anulowano.")
        self.scan_log.append(f"BŁĄD: {err}")
        QMessageBox.warning(self, "DiskRecover", err)

    def _fill_results(self, session) -> None:
        s = session.stats
        self.stats_label.setText(
            f"Pliki: {s.get('plikow_lacznie',0)} | "
            f"Docelowy: {s.get('w_docelowym',0)} | "
            f"Osierocone: {s.get('osierocone',0)} | "
            f"Nieustalone: {s.get('nieustalone',0)} | "
            f"RAW: {s.get('raw',0)} | "
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

    def _open_output(self) -> None:
        path = self.out_edit.text().strip()
        if path:
            self._open_path(path)

    def _open_report(self) -> None:
        path = Path(self.out_edit.text().strip()) / "Raport" / "raport.html"
        if path.is_file():
            self._open_path(str(path))
        else:
            QMessageBox.information(self, "DiskRecover", "Brak raportu — najpierw uruchom odzyskiwanie.")

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
