"""Auto theme must follow the system light/dark switch while the app is running."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from deckinstall import theme, ui


def _window(app: QApplication, tmp: str) -> ui.MainWindow:
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(Path(tmp) / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "list_standalone_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
    ):
        return ui.MainWindow(app)


def _close(app: QApplication, win: ui.MainWindow) -> None:
    for worker in list(win._active_workers):
        worker.wait(5000)
    app.processEvents()
    win.close()
    app.processEvents()


def test_auto_mode_reapplies_stylesheet_and_palette_on_system_change() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            idx = win.theme_combo.findData("auto")
            win.theme_combo.setCurrentIndex(idx)
            with mock.patch.object(theme, "resolve_is_dark", return_value=True):
                # Same path Qt takes when the desktop switches scheme (signal is wired).
                app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
            assert app.styleSheet().startswith(theme.DARK_QSS)
            dark_window = app.palette().window().color().name()
            with mock.patch.object(theme, "resolve_is_dark", return_value=False):
                win._on_system_scheme_changed()
            assert app.styleSheet().startswith(theme.LIGHT_QSS)
            assert app.palette().window().color().name() != dark_window
        finally:
            _close(app, win)


def test_fixed_mode_ignores_system_change() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            win.theme_combo.setCurrentIndex(win.theme_combo.findData("dark"))
            before = app.styleSheet()
            with mock.patch.object(theme, "resolve_is_dark", return_value=False):
                win._on_system_scheme_changed()
            assert app.styleSheet() == before and before.startswith(theme.DARK_QSS)
        finally:
            _close(app, win)
