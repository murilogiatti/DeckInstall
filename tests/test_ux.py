"""UI/UX behaviour: empty states, loading text, check box styling, labels."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from deckinstall import theme, ui
from deckinstall.i18n import LOCALES_DIR


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


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
    assert ui._wait_for_workers(app, win, 10.0)
    win.close()
    app.processEvents()


def test_library_empty_state_only_after_the_first_load() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            win._filter_library_list()
            assert win.library_empty_state.isHidden()  # detection still running: no "no games" flash
            assert ui._wait_for_workers(app, win, 10.0)
            assert win._library_loaded
            assert not win.library_empty_state.isHidden()
            assert win.shortcuts_list.isHidden()
        finally:
            _close(app, win)


def test_runtime_cards_say_checking_before_the_health_check() -> None:
    from deckinstall.i18n import tr

    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            assert win.lbl_rt_tools.text() == tr("runtimes.loading")
        finally:
            _close(app, win)


def test_check_boxes_have_a_visible_border_and_a_check_mark(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    app = _app()
    for mode, tokens in (("dark", theme._DARK_TOKENS), ("light", theme._LIGHT_TOKENS)):
        theme.apply_theme(app, mode)  # type: ignore[arg-type]
        qss = app.styleSheet()
        assert "QCheckBox::indicator" in qss and f"border: 1px solid {tokens['muted_fg']}" in qss
    png = tmp_path / "DeckInstall" / "ui" / "check-v1.png"
    assert png.is_file() and png.read_bytes()[:4] == b"\x89PNG"
    assert f"url({png.as_posix()})" in app.styleSheet()


def test_check_box_border_contrast_is_at_least_3_to_1() -> None:
    def lum(color: str) -> float:
        c = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    for tokens in (theme._DARK_TOKENS, theme._LIGHT_TOKENS):
        a, b = sorted((lum(tokens["muted_fg"]), lum(tokens["bg_surface"])), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 3.0


def test_check_box_without_cache_dir_still_styles(monkeypatch, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))  # mkdir fails: no image, border still drawn
    assert theme._check_mark_png() == ""
    assert "image:" not in theme.indicator_qss(theme._DARK_TOKENS, "")


def test_reload_buttons_do_not_say_update_in_portuguese() -> None:
    pt = json.loads((LOCALES_DIR / "pt_BR.json").read_text(encoding="utf-8"))
    for key in ("maint.btn_refresh_health", "proton_mgr.btn_refresh_installed", "save_sync.btn_refresh"):
        assert pt[key] == "Recarregar"


def test_ui_scale_choice_resolution() -> None:
    from deckinstall.ui_scale import resolve_scale

    assert resolve_scale("auto", gaming_mode=True, env={}) == "1.25"
    assert resolve_scale("auto", gaming_mode=False, env={}) is None
    assert resolve_scale("1", gaming_mode=True, env={}) is None
    assert resolve_scale("1.25", gaming_mode=False, env={}) == "1.25"
    assert resolve_scale("1.5", gaming_mode=True, env={}) == "1.25"  # unknown value: Auto
    assert resolve_scale("1.25", gaming_mode=True, env={"QT_SCALE_FACTOR": "2"}) is None  # the user's own


def test_ui_scale_env_is_set_only_around_the_qapplication() -> None:
    from deckinstall.ui_scale import clear_scale_env, set_scale_env

    env: dict[str, str] = {}
    assert set_scale_env("1.25", env) and env == {"QT_SCALE_FACTOR": "1.25"}
    clear_scale_env(True, env)
    assert env == {}
    env = {"QT_SCALE_FACTOR": "2"}
    assert not set_scale_env(None, env)
    clear_scale_env(False, env)
    assert env == {"QT_SCALE_FACTOR": "2"}  # never removes the user's variable


def test_main_applies_the_scale_and_children_do_not_inherit_it(monkeypatch) -> None:
    import os
    import sys

    seen: dict[str, str | None] = {}
    app = _app()

    def fake_qapp(_argv: object) -> QApplication:
        seen["during"] = os.environ.get("QT_SCALE_FACTOR")
        return app

    fake_win = mock.Mock(_active_workers=set())
    fake_win.close.return_value = True
    monkeypatch.delenv("QT_SCALE_FACTOR", raising=False)
    with (
        mock.patch.object(sys, "argv", ["deckinstall", "--smoke-test"]),
        mock.patch.object(ui, "QApplication", side_effect=fake_qapp),
        mock.patch.object(ui, "qInstallMessageHandler"),
        mock.patch.object(ui, "install_touch_scroll"),
        mock.patch.object(ui, "is_gamescope_session", return_value=True),
        mock.patch.object(ui, "MainWindow", return_value=fake_win),
    ):
        assert ui.main() == 0
    assert seen["during"] == "1.25"  # Auto in Gaming Mode
    assert "QT_SCALE_FACTOR" not in os.environ


def test_scale_combo_saves_and_says_it_needs_a_restart() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            win.scale_combo.setCurrentIndex(win.scale_combo.findData("1.25"))
            assert win.settings.value("ui/scale") == "1.25"
            assert not win.message_bar.isHidden()
        finally:
            _close(app, win)


def test_notify_uses_the_bar_and_a_box_only_over_our_open_dialog() -> None:
    from PySide6.QtWidgets import QDialog

    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            with mock.patch.object(ui.QMessageBox, "information") as box:
                win.notify("Backup done", "success")
                box.assert_not_called()
            assert win.message_bar.text() == "Backup done" and win.message_bar.level() == "success"
            assert win.message_bar._timer.isActive()  # successes fade
            win.notify("Proton fallback", "warning")
            assert not win.message_bar._timer.isActive()  # warnings stay until closed
            dlg = QDialog(win)
            dlg.setModal(True)
            dlg.show()
            app.processEvents()
            with (
                mock.patch.object(ui, "_dialog_parent", return_value=dlg),
                mock.patch.object(ui.QMessageBox, "information") as box,
            ):
                win.notify("Saved", "success")
                assert box.call_args.args[0] is dlg  # never hidden behind our own dialog
            dlg.close()
        finally:
            _close(app, win)


def test_message_bar_actions_and_keys() -> None:
    from deckinstall.ui_cards import MessageBar

    _app()
    bar = MessageBar()
    hits: list[str] = []
    bar.show_message("Update ready", "info", actions=(("Open", lambda: hits.append("open")),), key="updates")
    bar.dismiss("other")
    assert not bar.isHidden()  # only its owner (or anyone without a key) clears it
    btn = [b for b in bar.findChildren(ui.QPushButton) if b.text() == "Open"][0]
    btn.click()
    assert hits == ["open"] and bar.isHidden()


def test_busy_bar_shows_while_an_operation_runs() -> None:
    app = _app()
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            assert win.busy_bar.isHidden()
            win._begin_busy()
            win._begin_busy()
            assert not win.busy_bar.isHidden()
            win._end_busy()
            assert not win.busy_bar.isHidden()  # one operation still running
            win._end_busy()
            assert win.busy_bar.isHidden()
        finally:
            _close(app, win)
