"""Uruchomienie aplikacji GUI."""

from __future__ import annotations

import sys


def run_gui(initial_image: str | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from diskrecover.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("DiskRecover")
    app.setOrganizationName("DiskRecover")
    win = MainWindow(initial_image=initial_image)
    win.show()
    return app.exec()
