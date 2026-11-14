"""Modern KDE-inspired Dark and Light themes with Qt6 system auto-detection."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ThemeMode = Literal["auto", "dark", "light"]

DARK_QSS = """
QMainWindow, #ContentArea {
    background-color: #13161d;
    color: #e6ebf5;
    font-size: 13px;
}
#Sidebar {
    background-color: #1a1e28;
    border-right: 1px solid #282f3e;
}
#Sidebar QPushButton {
    background-color: transparent;
    color: #9aa6be;
    text-align: left;
    padding: 11px 14px;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
}
#Sidebar QPushButton:hover {
    background-color: #242a38;
    color: #ffffff;
}
#Sidebar QPushButton:checked {
    background-color: #1d6fa5;
    color: #ffffff;
}
QFrame#Card, QFrame#DropZone {
    background-color: #1a1e28;
    border: 1px solid #282f3e;
    border-radius: 12px;
}
QFrame#DropZone {
    border: 2px dashed #344157;
}
QLabel {
    color: #e6ebf5;
}
QLabel#PageTitle {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}
QLabel#MutedText {
    color: #8f9bb3;
    font-size: 12px;
}
QLabel#SectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: #8ab4f8;
}
QLineEdit, QComboBox, QListWidget, QTextEdit {
    background-color: #11141a;
    border: 1px solid #2c3445;
    border-radius: 8px;
    padding: 7px 11px;
    color: #e6ebf5;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QListWidget:focus {
    border-color: #3daee9;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: #1d6fa5;
    color: #ffffff;
}
QPushButton {
    background-color: #242a38;
    border: 1px solid #343d52;
    border-radius: 8px;
    padding: 8px 15px;
    color: #e6ebf5;
    font-weight: 500;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #2e3648;
}
QPushButton:disabled {
    opacity: 0.45;
}
QPushButton#primaryBtn {
    background-color: #1d6fa5;
    border-color: #3daee9;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #2584c4;
}
QPushButton#dangerBtn {
    background-color: #7c242b;
    border-color: #b83b44;
    color: #ffffff;
}
QPushButton#dangerBtn:hover {
    background-color: #962c34;
}
QProgressBar {
    background-color: #11141a;
    border: 1px solid #2c3445;
    border-radius: 8px;
    text-align: center;
    height: 20px;
    color: #ffffff;
}
QProgressBar::chunk {
    background-color: #3daee9;
    border-radius: 7px;
}
"""

LIGHT_QSS = """
QMainWindow, #ContentArea {
    background-color: #f3f5f9;
    color: #1b2230;
    font-size: 13px;
}
#Sidebar {
    background-color: #ffffff;
    border-right: 1px solid #dde3ee;
}
#Sidebar QPushButton {
    background-color: transparent;
    color: #556178;
    text-align: left;
    padding: 11px 14px;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
}
#Sidebar QPushButton:hover {
    background-color: #edf1f7;
    color: #111622;
}
#Sidebar QPushButton:checked {
    background-color: #1d6fa5;
    color: #ffffff;
}
QFrame#Card, QFrame#DropZone {
    background-color: #ffffff;
    border: 1px solid #dde3ee;
    border-radius: 12px;
}
QFrame#DropZone {
    border: 2px dashed #b9c5d8;
}
QLabel {
    color: #1b2230;
}
QLabel#PageTitle {
    font-size: 20px;
    font-weight: 700;
    color: #0f141e;
}
QLabel#MutedText {
    color: #5e6a82;
    font-size: 12px;
}
QLabel#SectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: #1d6fa5;
}
QLineEdit, QComboBox, QListWidget, QTextEdit {
    background-color: #ffffff;
    border: 1px solid #c9d3e3;
    border-radius: 8px;
    padding: 7px 11px;
    color: #1b2230;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QListWidget:focus {
    border-color: #1d6fa5;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: #1d6fa5;
    color: #ffffff;
}
QPushButton {
    background-color: #e9eef6;
    border: 1px solid #cbd4e3;
    border-radius: 8px;
    padding: 8px 15px;
    color: #1b2230;
    font-weight: 500;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #dde5f0;
}
QPushButton#primaryBtn {
    background-color: #1d6fa5;
    border-color: #155885;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #2584c4;
}
QPushButton#dangerBtn {
    background-color: #c93b44;
    border-color: #a62b33;
    color: #ffffff;
}
QProgressBar {
    background-color: #e9eef6;
    border: 1px solid #cbd4e3;
    border-radius: 8px;
    text-align: center;
    height: 20px;
    color: #1b2230;
}
QProgressBar::chunk {
    background-color: #1d6fa5;
    border-radius: 7px;
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
        palette.setColor(QPalette.ColorRole.Window, QColor("#13161d"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6ebf5"))
        app.setPalette(palette)
        app.setStyleSheet(DARK_QSS)
    else:
        palette.setColor(QPalette.ColorRole.Window, QColor("#f3f5f9"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#1b2230"))
        app.setPalette(palette)
        app.setStyleSheet(LIGHT_QSS)
    return dark
