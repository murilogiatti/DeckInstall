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
QWidget {
    font-size: 12px;
}
QMainWindow, QDialog, #ContentArea {
    background-color: $bg_window;
    color: $fg_text;
    font-size: 12px;
}
QFrame#Card, QFrame#DropZone {
    background-color: $bg_surface;
    border: 1px solid $border_subtle;
    border-radius: 12px;
}
QFrame#DropZone {
    border: 2px dashed $dropzone_border;
}
QTabWidget::pane {
    background-color: $bg_surface;
    border: 1px solid $border_subtle;
    border-radius: 6px;
}
QTabWidget > QStackedWidget > QWidget {
    background-color: $bg_surface;
}
QTabBar::tab {
    background-color: $bg_input;
    color: $fg_text;
    padding: 7px 12px;
    border: 1px solid $border_subtle;
}
QTabBar::tab:selected {
    background-color: $bg_surface;
    border-bottom: 2px solid $focus_border;
}
QLabel {
    color: $fg_text;
}
QLabel#PageTitle {
    font-size: 18px;
    font-weight: 700;
    color: $title_fg;
}
QLabel#MutedText {
    color: $muted_fg;
    font-size: 12px;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: 600;
    color: $section_fg;
}
QLineEdit, QComboBox, QListWidget, QTableWidget, QTextEdit {
    background-color: $bg_input;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 7px 11px;
    color: $fg_text;
    min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus, QTextEdit:focus {
    border: 2px solid $focus_border;
}
QListWidget::item, QTableWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
}
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #1d6fa5;
    color: #ffffff;
}
QListWidget::item:focus, QTableWidget::item:focus {
    border: 1px solid $focus_border;
}
QHeaderView::section {
    background-color: $bg_surface;
    color: $fg_text;
    border: none;
    border-bottom: 1px solid $border_subtle;
    border-right: 1px solid $border_subtle;
    padding: 6px 10px;
    font-weight: 600;
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
QPushButton:focus {
    border: 2px solid $focus_border;
}
QPushButton:disabled {
    background-color: $btn_disabled_bg;
    border-color: $disabled_border;
    color: $disabled_fg;
}
QPushButton#primaryBtn, QMessageBox QPushButton:default {
    background-color: #1d6fa5;
    border-color: $primary_border;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primaryBtn:hover, QMessageBox QPushButton:default:hover {
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
QTabBar#NavTabs::tab {
    font-size: 14px;
    font-weight: 600;
    padding: 9px 18px;
    min-height: 22px;
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
    background-color: $progress_fill;
    border-radius: 7px;
}
QLabel#SteamReady { color: $good_fg; font-size: 11px; }
QLabel#SteamWarning { color: $warning_fg; font-size: 11px; }
QLabel#SteamUnavailable { color: $info_fg; font-size: 11px; }
QLabel#VdfErrorBanner {
    color: $error_fg; background-color: $error_bg; border: 1px solid $error_fg;
    border-radius: 6px; padding: 8px;
}
QLabel#UnavailableBanner {
    color: $warning_fg; background-color: $warning_bg; border: 1px solid $warning_fg;
    border-radius: 6px; padding: 8px;
}
QLabel#SteamInfoBanner {
    color: $info_fg; background-color: $info_bg; border: 1px solid $info_fg;
    border-radius: 6px; padding: 8px;
}
QFrame#VdfQueueBanner {
    background-color: $info_bg; border: 1px solid $info_fg; border-radius: 6px; padding: 6px;
}
QLabel#VdfQueueText { color: $info_fg; }
""")

_DARK_TOKENS: dict[str, str] = {
    "bg_window": "#13161d",
    "fg_text": "#e6ebf5",
    "bg_surface": "#1a1e28",
    "border_subtle": "#282f3e",
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
    "disabled_fg": "#8d99b2",
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
    "progress_fill": "#1d6fa5",
    "warning_fg": "#ffd180",
    "warning_bg": "#342819",
    "error_fg": "#ff9b9b",
    "error_bg": "#361e25",
    "info_fg": "#90caf9",
    "info_bg": "#1b2b3d",
    "good_fg": "#a5d6a7",
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
    "disabled_fg": "#5f6b80",
    "primary_border": "#155885",
    "primary_disabled_bg": "#9dc0d9",
    "primary_disabled_border": "#88aecb",
    "primary_disabled_fg": "#173a54",
    "danger_bg": "#c93b44",
    "danger_border": "#a62b33",
    "danger_hover_bg": "#df4952",
    "danger_disabled_bg": "#dfb2b5",
    "danger_disabled_border": "#d09fa3",
    "danger_disabled_fg": "#542022",
    "input_disabled_bg": "#f1f4f9",
    "progress_bg": "#e9eef6",
    "progress_border": "#cbd4e3",
    "progress_fg": "#1b2230",
    "progress_fill": "#83b7dc",
    "warning_fg": "#744000",
    "warning_bg": "#fff2d9",
    "error_fg": "#9d2128",
    "error_bg": "#fff1f1",
    "info_fg": "#155885",
    "info_bg": "#eaf5ff",
    "good_fg": "#27602b",
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
    bg = app.style().standardPalette().color(QPalette.ColorRole.Window)
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
