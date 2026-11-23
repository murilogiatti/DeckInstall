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
        # Standalone: menu/desktop yes, Steam shortcut greyed out with the reason.
        assert not win.btn_shortcut_steam.isEnabled() and not win.btn_shortcut_steam.isChecked()
        assert win.lbl_shortcuts_note.text() == tr("mode.note_steam_shortcut_standalone")
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
        assert win.btn_shortcut_steam.isChecked() and not win.btn_shortcut_steam.isEnabled()
        assert not win.btn_shortcut_menu.isChecked() and not win.btn_shortcut_desktop.isChecked()
        assert not win.chk_restart.isHidden()
        win.btn_mode_standalone.click()
        win._update_steam_mode_ui()  # a later Steam re-detection keeps the user's choice
        assert win._selected_install_mode() == STANDALONE
        assert win.btn_shortcut_menu.isEnabled() and win.btn_shortcut_menu.isChecked()  # choice restored
        assert not win.btn_shortcut_steam.isChecked()
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
        with mock.patch.object(ui, "StandaloneInstallWorker") as worker, mock.patch.object(win, "_track_worker"):
            win._install_selected()  # a Mock worker must not be tracked: closing would wait for it forever
        kwargs = worker.call_args.kwargs
        assert kwargs["create_menu_shortcut"] is False and kwargs["create_desktop_shortcut"] is False
        assert kwargs["prepare_standalone_runtime"] is False
        win._end_busy()
    finally:
        win.paths = None
        _drain(win, app)
        win.close()


def test_runtimes_tab_greys_out_what_steamos_does_not_use(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        assert win.btn_rt_umu_update.isEnabled() and win.lbl_rt_umu_note.isHidden()  # no Steam
        _with_steam(win, tmp_path)
        assert win.btn_rt_mangohud_update.isEnabled()
        assert win.lbl_rt_mangohud_note.text() == tr("runtimes.note_standalone_only")
        win._steamos = True
        win._update_steam_mode_ui()
        for btn, note, key in (
            (win.btn_rt_ge_update, win.lbl_rt_proton_note, "runtimes.note_proton_steamos"),
            (win.btn_rt_umu_update, win.lbl_rt_umu_note, "runtimes.note_umu_steamos"),
            (win.btn_rt_mangohud_update, win.lbl_rt_mangohud_note, "runtimes.note_mangohud_steamos"),
        ):
            assert not btn.isEnabled() and note.text() == tr(key)
        assert win.btn_rt_optiscaler_update.isEnabled()  # OptiScaler serves both modes
    finally:
        win.paths = None
        _drain(win, app)
        win.close()


def test_card_remove_lists_only_its_component(tmp_path: Path) -> None:
    from deckinstall import ui_maintenance
    from deckinstall.runtime_uninstall import MANGOHUD, UMU, RuntimeItem

    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        items = [RuntimeItem(UMU, "umu-run", tmp_path / "umu", 1), RuntimeItem(MANGOHUD, "MangoHud", tmp_path / "mh", 1)]
        loader = mock.Mock()
        loader.start.side_effect = lambda: loader.finished_ok.connect.call_args.args[0](items)
        with (
            mock.patch("deckinstall.ui_workers.SteamOpWorker", return_value=loader),
            mock.patch.object(win, "_track_worker"),
            mock.patch.object(ui_maintenance.QMessageBox, "information") as info,
        ):
            ui_maintenance.open_uninstall_runtimes_dialog(win, kinds={"ge"})
        info.assert_called_once()  # nothing of that kind: "nothing to remove", no dialog
        assert info.call_args.args[2] == tr("runtime.uninstall_none")
    finally:
        _drain(win, app)
        win.close()
