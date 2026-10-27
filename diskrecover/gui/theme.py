"""Ciemny motyw DiskRecover (bez fioletu / „AI purple”)."""

DARK_QSS = """
QWidget {
    background-color: #12141a;
    color: #e6e8ee;
    font-family: "Segoe UI", "Tahoma", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #12141a;
}
QLabel#Brand {
    color: #7dd3c0;
    font-size: 22px;
    font-weight: 700;
}
QLabel#Subtitle {
    color: #8b93a7;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #2a3145;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
    font-weight: 600;
    color: #aeb6c8;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTreeWidget, QTableWidget, QListWidget {
    background-color: #1a1e2a;
    border: 1px solid #2f364a;
    border-radius: 4px;
    padding: 6px;
    selection-background-color: #2f6f64;
}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {
    border: 1px solid #7dd3c0;
}
QPushButton {
    background-color: #243043;
    border: 1px solid #3a455c;
    border-radius: 4px;
    padding: 8px 14px;
    color: #e6e8ee;
}
QPushButton:hover {
    background-color: #2c3a52;
}
QPushButton:pressed {
    background-color: #1c2638;
}
QPushButton#Primary {
    background-color: #2a6b5e;
    border: 1px solid #3d8f7e;
    font-weight: 600;
}
QPushButton#Primary:hover {
    background-color: #328072;
}
QPushButton#Danger {
    background-color: #5a3038;
    border: 1px solid #8a4a55;
}
QPushButton:disabled {
    color: #6a7388;
    background-color: #1a1e28;
}
QProgressBar {
    border: 1px solid #2f364a;
    border-radius: 4px;
    background: #1a1e2a;
    text-align: center;
    color: #d0d5e0;
}
QProgressBar::chunk {
    background-color: #3d8f7e;
    border-radius: 3px;
}
QHeaderView::section {
    background-color: #1c2230;
    color: #9aa3b5;
    border: none;
    border-bottom: 1px solid #2a3145;
    padding: 6px;
}
QTabWidget::pane {
    border: 1px solid #2a3145;
    border-radius: 4px;
}
QTabBar::tab {
    background: #1a1e2a;
    border: 1px solid #2a3145;
    padding: 8px 14px;
    margin-right: 2px;
    color: #9aa3b5;
}
QTabBar::tab:selected {
    background: #243043;
    color: #7dd3c0;
}
QStatusBar {
    background: #0e1016;
    color: #8b93a7;
}
QCheckBox {
    spacing: 8px;
}
QToolTip {
    background-color: #1c2230;
    color: #e6e8ee;
    border: 1px solid #3a455c;
}
"""
