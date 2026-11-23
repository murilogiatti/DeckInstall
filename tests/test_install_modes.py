"""Install page: one mode per game (Steam or Standalone) and only that mode's options."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import ui  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.modes import STANDALONE, STEAM  # noqa: E402
from tests.test_audit_phase2 import _drain, _make_window, _steam_paths  # noqa: E402


def _with_steam(win: ui.MainWindow, tmp_path: Path, steamos: bool = False) -> None:
    win._steamos = steamos
    win.paths = _steam_paths(tmp_path)
    win._update_steam_mode_ui()


def test_no_steam_only_standalone(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        assert win._selected_install_mode() == STANDALONE
        assert not win.btn_mode_steam.isEnabled()
        assert win.btn_mode_steam.toolTip() == tr("mode.note_steam_missing")
        assert win.btn_shortcut_menu.isEnabled() and win.chk_restart.isHidden()
    finally:
        _drain(win, app)
        win.close()


def test_steam_found_later_becomes_the_default_until_the_user_picks(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        _with_steam(win, tmp_path)
        assert win._selected_install_mode() == STEAM
        # Steam mode: no menu/desktop launchers, says why, Steam restart option shown.
        assert not win.btn_shortcut_menu.isEnabled() and not win.btn_shortcut_desktop.isEnabled()
        assert win.lbl_shortcuts_note.text() == tr("mode.note_shortcuts_steam")
        assert not win.chk_restart.isHidden()
        win.btn_mode_standalone.click()
        win._update_steam_mode_ui()  # a later Steam re-detection keeps the user's choice
        assert win._selected_install_mode() == STANDALONE
        assert win.btn_shortcut_menu.isEnabled()
    finally:
        win.paths = None
        _drain(win, app)
        win.close()


def test_steamos_offers_steam_mode_only(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        _with_steam(win, tmp_path, steamos=True)
        assert win._selected_install_mode() == STEAM
        assert not win.btn_mode_standalone.isEnabled()
        assert win.btn_mode_standalone.toolTip() == tr("mode.note_standalone_steamos")
        assert tr("mode.note_standalone_steamos") in win.lbl_mode_note.text()
    finally:
        win.paths = None
        _drain(win, app)
        win.close()


def test_steam_mode_drops_mangohud_from_the_install_presets(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        win._add_launch_options = "MANGOHUD=1 gamemoderun %command%"
        _with_steam(win, tmp_path)
        assert "MANGOHUD" not in win._add_launch_options and "gamemoderun" in win._add_launch_options
        with mock.patch.object(ui, "open_launch_presets_dialog", return_value=None) as dlg:
            win._open_add_launch_advanced()
        assert dlg.call_args.kwargs["mode"] == STEAM
    finally:
        win.paths = None
        _drain(win, app)
        win.close()


def test_steam_mode_install_creates_no_app_launchers(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        _with_steam(win, tmp_path)
        exe = tmp_path / "Game" / "game.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40)
        win.add_name.setText("Game")
        win.add_exe_combo.setEditText(str(exe))
        win.btn_shortcut_menu.setChecked(True)
        win.chk_keep_in_place.setChecked(True)
        with mock.patch.object(ui, "StandaloneInstallWorker") as worker:
            win._install_selected()
        kwargs = worker.call_args.kwargs
        assert kwargs["create_menu_shortcut"] is False and kwargs["create_desktop_shortcut"] is False
        assert kwargs["prepare_standalone_runtime"] is False
        win._end_busy()
    finally:
        win.paths = None
        _drain(win, app)
        win.close()
