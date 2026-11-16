"""Modern KDE-inspired Dark and Light themes with Qt6 system auto-detection."""

from __future__ import annotations

from collections.abc import Mapping
from string import Template
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ThemeMode = Literal["auto", "dark", "light"]

_THEME_QSS_TEMPLATE = Template("""
QMainWindow, #ContentArea {
    background-color: $bg_window;
    color: $fg_text;
    font-size: 13px;
}
#Sidebar {
    background-color: $bg_surface;
    border-right: 1px solid $border_subtle;
}
#Sidebar QPushButton {
    background-color: transparent;
    color: $sidebar_fg;
    text-align: left;
    padding: 11px 14px;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
}
#Sidebar QPushButton:hover {
    background-color: $sidebar_hover_bg;
    color: $sidebar_hover_fg;
}
#Sidebar QPushButton:checked {
    background-color: #1d6fa5;
    color: #ffffff;
}
QFrame#Card, QFrame#DropZone {
    background-color: $bg_surface;
    border: 1px solid $border_subtle;
    border-radius: 12px;
}
QFrame#DropZone {
    border: 2px dashed $dropzone_border;
}
QLabel {
    color: $fg_text;
}
QLabel#PageTitle {
    font-size: 20px;
    font-weight: 700;
    color: $title_fg;
}
QLabel#MutedText {
    color: $muted_fg;
    font-size: 12px;
}
QLabel#SectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: $section_fg;
}
QLineEdit, QComboBox, QListWidget, QTextEdit {
    background-color: $bg_input;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 7px 11px;
    color: $fg_text;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTextEdit:focus {
    border: 2px solid $focus_border;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: #1d6fa5;
    color: #ffffff;
}
QListWidget::item:focus {
    border: 1px solid $focus_border;
}
QCheckBox:focus {
    outline: 1px solid $focus_border;
    color: $checkbox_focus_fg;
}
QLabel#PreviewBox {
    border: 1px solid $preview_border;
    border-radius: 8px;
    background-color: $bg_input;
}
QPushButton {
    background-color: $btn_bg;
    border: 1px solid $btn_border;
    border-radius: 8px;
    padding: 8px 15px;
    color: $fg_text;
    font-weight: 500;
    min-height: 20px;
}
QPushButton:hover {
    background-color: $btn_hover_bg;
}
QPushButton:focus, #Sidebar QPushButton:focus {
    border: 2px solid $focus_border;
}
QPushButton:disabled {
    background-color: $btn_disabled_bg;
    border-color: $disabled_border;
    color: $disabled_fg;
}
QPushButton#primaryBtn {
    background-color: #1d6fa5;
    border-color: $primary_border;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: #2584c4;
}
QPushButton#primaryBtn:disabled {
    background-color: $primary_disabled_bg;
    border-color: $primary_disabled_border;
    color: $primary_disabled_fg;
}
QPushButton#dangerBtn {
    background-color: $danger_bg;
    border-color: $danger_border;
    color: #ffffff;
}
QPushButton#dangerBtn:hover {
    background-color: $danger_hover_bg;
}
QPushButton#dangerBtn:disabled {
    background-color: $danger_disabled_bg;
    border-color: $danger_disabled_border;
    color: $danger_disabled_fg;
}
QPushButton#FilterPill {
    padding: 5px 12px;
    border-radius: 14px;
    min-height: 16px;
}
QPushButton#FilterPill:checked {
    background-color: #1d6fa5;
    border-color: $primary_border;
    color: #ffffff;
    font-weight: 600;
}
QLineEdit:disabled, QComboBox:disabled {
    background-color: $input_disabled_bg;
    border-color: $disabled_border;
    color: $disabled_fg;
}
QProgressBar {
    background-color: $progress_bg;
    border: 1px solid $progress_border;
    border-radius: 8px;
    text-align: center;
    height: 20px;
    color: $progress_fg;
}
QProgressBar::chunk {
    background-color: $focus_border;
    border-radius: 7px;
}
""")

_DARK_TOKENS: dict[str, str] = {
    "bg_window": "#13161d",
    "fg_text": "#e6ebf5",
    "bg_surface": "#1a1e28",
    "border_subtle": "#282f3e",
    "sidebar_fg": "#9aa6be",
    "sidebar_hover_bg": "#242a38",
    "sidebar_hover_fg": "#ffffff",
    "dropzone_border": "#344157",
    "title_fg": "#ffffff",
    "muted_fg": "#8f9bb3",
    "section_fg": "#8ab4f8",
    "bg_input": "#11141a",
    "input_border": "#2c3445",
    "focus_border": "#3daee9",
    "checkbox_focus_fg": "#ffffff",
    "preview_border": "#344157",
    "btn_bg": "#242a38",
    "btn_border": "#343d52",
    "btn_hover_bg": "#2e3648",
    "btn_disabled_bg": "#181c26",
    "disabled_border": "#232938",
    "disabled_fg": "#6b768d",
    "primary_border": "#3daee9",
    "primary_disabled_bg": "#173a54",
    "primary_disabled_border": "#234c6b",
    "primary_disabled_fg": "#7da3be",
    "danger_bg": "#7c242b",
    "danger_border": "#b83b44",
    "danger_hover_bg": "#962c34",
    "danger_disabled_bg": "#421b20",
    "danger_disabled_border": "#5b252b",
    "danger_disabled_fg": "#9b7377",
    "input_disabled_bg": "#151922",
    "progress_bg": "#11141a",
    "progress_border": "#2c3445",
    "progress_fg": "#ffffff",
    "bg_alt": "#181c26",
    "highlight_bg": "#1d6fa5",
    "highlight_fg": "#ffffff",
    "placeholder_fg": "#7c88a0",
    "tooltip_bg": "#1c212c",
}

_LIGHT_TOKENS: dict[str, str] = {
    "bg_window": "#f3f5f9",
    "fg_text": "#1b2230",
    "bg_surface": "#ffffff",
    "border_subtle": "#dde3ee",
    "sidebar_fg": "#556178",
    "sidebar_hover_bg": "#edf1f7",
    "sidebar_hover_fg": "#111622",
    "dropzone_border": "#b9c5d8",
    "title_fg": "#0f141e",
    "muted_fg": "#5e6a82",
    "section_fg": "#1d6fa5",
    "bg_input": "#ffffff",
    "input_border": "#c9d3e3",
    "focus_border": "#1d6fa5",
    "checkbox_focus_fg": "#0e3a58",
    "preview_border": "#c9d3e3",
    "btn_bg": "#e9eef6",
    "btn_border": "#cbd4e3",
    "btn_hover_bg": "#dde5f0",
    "btn_disabled_bg": "#eef1f6",
    "disabled_border": "#dce2ec",
    "disabled_fg": "#8b96aa",
    "primary_border": "#155885",
    "primary_disabled_bg": "#9dc0d9",
    "primary_disabled_border": "#88aecb",
    "primary_disabled_fg": "#eef4f9",
    "danger_bg": "#c93b44",
    "danger_border": "#a62b33",
    "danger_hover_bg": "#df4952",
    "danger_disabled_bg": "#dfb2b5",
    "danger_disabled_border": "#d09fa3",
    "danger_disabled_fg": "#f9eff0",
    "input_disabled_bg": "#f1f4f9",
    "progress_bg": "#e9eef6",
    "progress_border": "#cbd4e3",
    "progress_fg": "#1b2230",
    "bg_alt": "#e9eef6",
    "highlight_bg": "#1d6fa5",
    "highlight_fg": "#ffffff",
    "placeholder_fg": "#6b768d",
    "tooltip_bg": "#ffffff",
}


def build_qss(tokens: Mapping[str, str]) -> str:
    """Render the shared QSS template using the given color token mapping."""
    return _THEME_QSS_TEMPLATE.substitute(tokens)


DARK_QSS = build_qss(_DARK_TOKENS)

LIGHT_QSS = build_qss(_LIGHT_TOKENS)


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
    tokens = _DARK_TOKENS if dark else _LIGHT_TOKENS
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(tokens["bg_window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["fg_text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(tokens["bg_input"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["bg_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(tokens["fg_text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(tokens["btn_bg"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["fg_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens["highlight_bg"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens["highlight_fg"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens["placeholder_fg"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens["tooltip_bg"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens["fg_text"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(tokens["disabled_fg"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(tokens["disabled_fg"]))
    app.setPalette(palette)
    app.setStyleSheet(DARK_QSS if dark else LIGHT_QSS)
    return dark
