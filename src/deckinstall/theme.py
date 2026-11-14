"""Dark and Light theme palettes with automatic Qt6 system detection."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ThemeMode = Literal["auto", "dark", "light"]

DARK_QSS = """
QMainWindow, QWidget {
    background-color: #161920;
    color: #e6ebf5;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #2b3242;
    border-radius: 8px;
    background-color: #1c2029;
}
QTabBar::tab {
    background-color: #1c2029;
    color: #9aa6be;
    padding: 9px 18px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #252b38;
    color: #ffffff;
    border-bottom: 2px solid #3daee9;
}
QGroupBox {
    border: 1px solid #2b3242;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #8ab4f8;
}
QLineEdit, QComboBox, QListWidget, QTextEdit {
    background-color: #12151b;
    border: 1px solid #2f3749;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e6ebf5;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #3daee9;
}
QPushButton {
    background-color: #252b38;
    border: 1px solid #374157;
    border-radius: 6px;
    padding: 7px 14px;
    color: #e6ebf5;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #2f3749;
}
QPushButton#primaryBtn {
    background-color: #1d6fa5;
    border-color: #3daee9;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #2484c4;
}
QPushButton#dangerBtn {
    background-color: #7c242b;
    border-color: #b83b44;
    color: #ffffff;
}
QProgressBar {
    background-color: #12151b;
    border: 1px solid #2f3749;
    border-radius: 6px;
    text-align: center;
    height: 18px;
}
QProgressBar::chunk {
    background-color: #3daee9;
    border-radius: 5px;
}
"""

LIGHT_QSS = """
QMainWindow, QWidget {
    background-color: #f4f6fa;
    color: #1d2330;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #d5dce8;
    border-radius: 8px;
    background-color: #ffffff;
}
QTabBar::tab {
    background-color: #e8edf5;
    color: #4d5870;
    padding: 9px 18px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #ffffff;
    color: #111622;
    border-bottom: 2px solid #1d6fa5;
}
QGroupBox {
    border: 1px solid #d5dce8;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #1d6fa5;
}
QLineEdit, QComboBox, QListWidget, QTextEdit {
    background-color: #ffffff;
    border: 1px solid #c5cfdf;
    border-radius: 6px;
    padding: 6px 10px;
    color: #1d2330;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #1d6fa5;
}
QPushButton {
    background-color: #e8edf5;
    border: 1px solid #c5cfdf;
    border-radius: 6px;
    padding: 7px 14px;
    color: #1d2330;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #dce3ef;
}
QPushButton#primaryBtn {
    background-color: #1d6fa5;
    border-color: #155885;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #2484c4;
}
QPushButton#dangerBtn {
    background-color: #c93b44;
    border-color: #a62b33;
    color: #ffffff;
}
QProgressBar {
    background-color: #e8edf5;
    border: 1px solid #c5cfdf;
    border-radius: 6px;
    text-align: center;
    height: 18px;
}
QProgressBar::chunk {
    background-color: #1d6fa5;
    border-radius: 5px;
}
"""


def resolve_is_dark(app: QApplication, mode: ThemeMode) -> bool:
    """Determine whether dark mode should be active."""
    if mode == "dark":
        return True
    if mode == "light":
        return False
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except AttributeError:
        pass
    bg = app.palette().color(QPalette.ColorRole.Window)
    return bg.lightness() < 128


def apply_theme(app: QApplication, mode: ThemeMode = "auto") -> bool:
    """Apply Dark or Light stylesheet to QApplication. Returns True if dark."""
    dark = resolve_is_dark(app, mode)
    palette = QPalette()
    if dark:
        palette.setColor(QPalette.ColorRole.Window, QColor("#161920"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6ebf5"))
        app.setPalette(palette)
        app.setStyleSheet(DARK_QSS)
    else:
        palette.setColor(QPalette.ColorRole.Window, QColor("#f4f6fa"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#1d2330"))
        app.setPalette(palette)
        app.setStyleSheet(LIGHT_QSS)
    return dark
