"""Drive real Qt dialogs while replacing only external operations."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QToolButton,
)

from deckinstall import ui, ui_workers
from deckinstall.i18n import tr
from deckinstall.library import Library
from deckinstall.optiscaler import BulkOutcome, BulkTarget, OptiScalerCompatibility, OptiScalerState
from deckinstall.relocate import RelocatePlan
from deckinstall.runtime_manager import InstalledCompatTool
from deckinstall.standalone import StandaloneGame
from deckinstall.steam_paths import SteamPaths
from deckinstall.steam_vdf import CompatMappingEntry, ShortcutEntry
from deckinstall.ui_dialogs import (
    configure_game_dialog,
    launch_presets_dialog,
    optiscaler_bulk_dialog,
    prefix_tools_dialogs,
    proton_bulk_dialog,
    proton_dialogs,
    relocate_dialog,
)


@pytest.fixture
def window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ui.MainWindow]:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "en")
    monkeypatch.setattr(ui, "QSettings", lambda *_: settings)
    with monkeypatch.context() as startup:
        startup.setattr(ui.MainWindow, "_refresh_games", lambda *_: None)
        startup.setattr(ui.MainWindow, "_redetect_steam", lambda *_: None)
        win = ui.MainWindow(app)
    try:
        yield win
    finally:
        for worker in list(win._active_workers):
            assert worker.wait(5000)
        app.processEvents()
        win._vdf_queue.clear()
        win.close()
        app.processEvents()


@pytest.fixture
def game(tmp_path: Path) -> StandaloneGame:
    folder = tmp_path / "Game"
    folder.mkdir()
    exe = folder / "game.exe"
    exe.write_bytes(b"MZ")
    return StandaloneGame("Game", folder, exe, tmp_path / "prefix", "GE-Current", None, "", "",
                          folder / "launch.sh", tmp_path / "game.desktop")


def _paths(root: Path) -> SteamPaths:
    return SteamPaths(root, "17", root, root / "shortcuts.vdf", root / "grid",
                      root / "config.vdf", root / "compatdata")


def _button(dlg: QDialog, key: str) -> QPushButton:
    return next(b for b in dlg.findChildren(QPushButton) if b.text() == tr(key))


def _modal(call: Callable[[], object], steps: list[Callable[[QDialog], bool]]) -> object:
    """Run steps against real modal widgets and surface exceptions from timer callbacks."""
    pending = iter(steps)
    current = next(pending, None)
    errors: list[BaseException] = []
    deadline = monotonic() + 6
    timer = QTimer()
    timer.setInterval(10)

    def tick() -> None:
        nonlocal current
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QDialog):
            return
        try:
            if monotonic() > deadline:
                raise AssertionError(f"Modal did not finish: {dialog.windowTitle()}")
            if current is not None and current(dialog):
                current = next(pending, None)
        except BaseException as exc:
            errors.append(exc)
            timer.stop()
            dialog.done(QDialog.DialogCode.Rejected)

    timer.timeout.connect(tick)
    timer.start()
    try:
        result = call()
    finally:
        timer.stop()
    if errors:
        raise errors[0]
    assert current is None, "dialog returned before the expected interaction"
    return result


def _escape(dlg: QDialog) -> bool:
    QTest.keyClick(dlg, Qt.Key.Key_Escape)
    return True


@pytest.mark.parametrize("appid", [0, 2147483649])
def test_proton_manager_rechecks_usage_before_removing_in_gaming_mode(
    window: ui.MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, appid: int,
) -> None:
    window.paths = _paths(tmp_path)
    monkeypatch.setattr(ui, "is_gamescope_session", lambda: True)
    tool = InstalledCompatTool("GE-Used", "GE-Used", tmp_path / "GE-Used", 100)
    monkeypatch.setattr(proton_dialogs, "list_installed_compat_tools", lambda: [tool])
    release = Event()
    calls: list[int] = []

    def usage(_paths: SteamPaths) -> list[CompatMappingEntry]:
        calls.append(appid)
        if len(calls) == 1:
            return []
        assert release.wait(5)
        return [CompatMappingEntry(appid, "Game", "Steam", tool.tool_id)]

    monkeypatch.setattr(proton_dialogs, "_manager_tool_usage", usage)
    remove = Mock()
    monkeypatch.setattr(proton_dialogs, "remove_compat_tool", remove)

    def click_remove(dialog: QDialog) -> bool:
        table = dialog.findChild(QTableWidget)
        if table.rowCount() != 1:
            return False
        table.selectRow(0)
        button = _button(dialog, "proton_mgr.btn_remove")
        if not button.isEnabled():
            return False
        button.click()
        return True

    def busy_escape(dialog: QDialog) -> bool:
        assert window._steam_busy
        assert not dialog.close()
        _escape(dialog)
        assert dialog.isVisible()
        release.set()
        return True

    def dismiss_warning(dialog: QDialog) -> bool:
        if not isinstance(dialog, QMessageBox):
            return False
        assert dialog.text() == tr("proton_mgr.remove_requires_review")
        dialog.button(QMessageBox.StandardButton.Ok).click()
        return True

    _modal(lambda: proton_dialogs.open_proton_manager_dialog(window),
           [click_remove, busy_escape, dismiss_warning, _escape])
    remove.assert_not_called()
    assert len(calls) == 2 and not window._steam_busy


def test_winetricks_busy_escape_and_explicit_cancel(
    window: ui.MainWindow, game: StandaloneGame, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefix_tools_dialogs, "resolve_game_prefix", lambda *_: (game.prefix_dir, None))
    monkeypatch.setattr(prefix_tools_dialogs, "find_winetricks", lambda: "/bin/winetricks")
    monkeypatch.setattr(window, "_make_pre_snapshot_fn", lambda: None)
    release = Event()
    recorded: list[object] = []

    def run_verbs(prefix, verbs, *, proton_dir, on_log, cancel_flag):
        recorded.extend((prefix, verbs))
        assert release.wait(5)
        recorded.append(cancel_flag())

    monkeypatch.setattr(ui_workers, "run_winetricks_verbs", run_verbs)

    def install(dialog: QDialog) -> bool:
        dialog.findChild(QLineEdit).setText("dxvk")
        _button(dialog, "winetricks.btn_install").click()
        return True

    def cancel(dialog: QDialog) -> bool:
        _escape(dialog)
        assert dialog.isVisible() and not dialog.close()
        _button(dialog, "winetricks.btn_cancel").click()
        release.set()
        return True

    def dismiss_warning(dialog: QDialog) -> bool:
        if not isinstance(dialog, QMessageBox):
            return False
        assert tr("winetricks.cancelled") in dialog.text()
        dialog.button(QMessageBox.StandardButton.Ok).click()
        return True

    _modal(lambda: prefix_tools_dialogs.open_winetricks_dialog(window, "standalone", game),
           [install, cancel, dismiss_warning, _escape])
    assert recorded == [game.prefix_dir, ["dxvk"], True]


def test_game_tools_run_exe_uses_active_parent(
    window: ui.MainWindow, game: StandaloneGame, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window._selected_library_item = lambda: ("standalone", game)
    runner = Mock(return_value=0)
    monkeypatch.setattr(ui_workers, "run_exe_in_prefix", runner)
    monkeypatch.setattr(window, "_warn_if_win32_prefix", lambda _: False)
    monkeypatch.setattr(prefix_tools_dialogs, "resolve_game_prefix", lambda *_: (game.prefix_dir, None))
    picker_parent: list[QDialog] = []
    monkeypatch.setattr(prefix_tools_dialogs, "_pick_file_async",
                        lambda parent, _title, _start, _filter, picked:
                        (picker_parent.append(parent), picked(str(game.exe_path))))

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    def run_exe(dialog: QDialog) -> bool:
        _button(dialog, "library.btn_run_exe_in_prefix").click()
        return True

    def wait_for_run(dialog: QDialog) -> bool:
        if not runner.called:
            return False
        assert picker_parent == [dialog]
        return _escape(dialog)

    _modal(lambda: configure_game_dialog.open_configure_game_dialog(window, tools_only=True),
           [run_exe, wait_for_run])
    runner.assert_called_once_with(game.exe_path, game.prefix_dir, proton_path=None)


def test_launch_presets_applies_checkbox_with_real_dialog(
    window: ui.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launch_presets_dialog.shutil, "which", lambda _: "/bin/gamemoderun")

    def edit(dialog: QDialog) -> bool:
        apply = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply)
        assert not apply.isEnabled()
        assert not dialog.findChild(QLineEdit, "gameLaunchOptions").isVisible()
        next(b for b in dialog.findChildren(QToolButton) if b.text() == tr("launch.more_options")).click()
        checkbox = next(c for c in dialog.findChildren(QCheckBox) if "GameMode" in c.text())
        checkbox.click()
        assert apply.isEnabled()
        apply.click()
        return True

    result = _modal(lambda: launch_presets_dialog.open_launch_presets_dialog(window, "Game", "standalone", "", ""),
                    [edit])
    assert isinstance(result, tuple) and "gamemoderun" in result[0]


@pytest.mark.parametrize("queued", [False, True])
def test_configure_applies_proton_and_fps_together_for_steam(
    window: ui.MainWindow, game: StandaloneGame, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, queued: bool,
) -> None:
    from deckinstall import steam_vdf
    from deckinstall.launch_presets import parse_presets

    window.paths = paths = _paths(tmp_path)
    appid = steam_vdf.add_or_update_shortcut(
        paths, game.name, game.exe_path, game.game_dir,
        enable_proton=True, launch_options="CUSTOM=abc %command% --manual",
    )
    entry = steam_vdf.load_shortcuts(paths.shortcuts_vdf)[0]
    window._selected_library_item = lambda: ("steam", entry)
    monkeypatch.setattr(window, "_managed_library_game", lambda _: None)
    monkeypatch.setattr(window, "_vdf_watch_tick", lambda: None)
    monkeypatch.setattr(window, "should_queue_vdf_edits", lambda: queued)
    monkeypatch.setattr(ui_workers, "is_steam_running", lambda: False)
    monkeypatch.setattr(configure_game_dialog, "list_installed_protons", lambda _: [("proton_experimental", "Experimental")])
    confirm = Mock(side_effect=lambda apply, **_: apply())
    monkeypatch.setattr(window, "_confirm_close_steam_async", confirm)

    def edit(dialog: QDialog) -> bool:
        proton = dialog.findChild(QComboBox, "gameProton")
        if not proton.isEnabled():
            return False
        fps = dialog.findChild(QComboBox, "gameFps")
        apply = _button(dialog, "rec.apply")
        assert not apply.isEnabled()
        assert not dialog.findChild(QLineEdit, "gameLaunchOptions").isVisible()
        more = next(b for b in dialog.findChildren(QToolButton) if b.text() == tr("launch.more_options"))
        assert more.visibleRegion().boundingRect() == more.rect()
        proton.setCurrentIndex(proton.findData("proton_experimental"))
        fps.setCurrentIndex(fps.findData(45))
        apply.click()
        return True

    def check(dialog: QDialog) -> bool:
        if not _button(dialog, "configure.btn_close").isEnabled():
            return False
        assert not _button(dialog, "rec.apply").isEnabled()
        return _escape(dialog)

    _modal(lambda: configure_game_dialog.open_configure_game_dialog(window), [edit, check])
    if queued:
        edits = {edit.op: edit.payload for edit in window._vdf_queue.list_pending()}
        assert edits["set_proton"]["tool_name"] == "proton_experimental"
        opts = edits["set_launch_options"]["launch_options"]
        assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == ""
        assert edits["set_launch_options"]["user_id"] == paths.user_id
    else:
        confirm.assert_called_once()
        assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == "proton_experimental"
        opts = steam_vdf.load_shortcuts(paths.shortcuts_vdf)[0].launch_options
    assert parse_presets(opts)["frame_rate"] == 45
    assert "CUSTOM=abc" in opts and opts.endswith("%command% --manual")


def test_configure_standalone_persists_options_from_one_apply(
    window: ui.MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deckinstall import standalone
    from deckinstall.launch_presets import parse_presets
    from tests.conftest import pe_exe_bytes

    source = tmp_path / "Game"
    source.mkdir()
    exe = source / "game.exe"
    exe.write_bytes(pe_exe_bytes())
    monkeypatch.setattr(standalone, "resolve_game_icon", lambda *a, **kw: None)
    game = standalone.install_standalone_game(
        "Game", exe, source, launch_options="CUSTOM=abc %command% --manual", create_menu_shortcut=False,
    )
    window._selected_library_item = lambda: ("standalone", game)
    tool = InstalledCompatTool("GE-Proton11-test", "GE-Proton11-test", tmp_path / "GE-Proton11-test", 0)
    monkeypatch.setattr(configure_game_dialog, "list_installed_compat_tools", lambda: [tool])

    def edit(dialog: QDialog) -> bool:
        proton = dialog.findChild(QComboBox, "gameProton")
        if not proton.isEnabled():
            return False
        proton.setCurrentIndex(proton.findData(tool.tool_id))
        fps = dialog.findChild(QComboBox, "gameFps")
        fps.setCurrentIndex(fps.findData(60))
        _button(dialog, "rec.apply").click()
        return True

    def check(dialog: QDialog) -> bool:
        if not _button(dialog, "configure.btn_close").isEnabled():
            return False
        assert not _button(dialog, "rec.apply").isEnabled()
        return _escape(dialog)

    _modal(lambda: configure_game_dialog.open_configure_game_dialog(window), [edit, check])
    saved = standalone._load_manifest(standalone.find_manifest(game.game_dir))
    assert saved is not None and saved.proton_name == tool.tool_id
    assert parse_presets(saved.launch_options)["frame_rate"] == 60
    assert "CUSTOM=abc" in saved.launch_options and saved.launch_options.endswith("%command% --manual")


def test_bulk_proton_previous_in_gaming_mode_persists_vdf_edit(
    window: ui.MainWindow, game: StandaloneGame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.paths = _paths(tmp_path)
    monkeypatch.setattr(ui, "is_gamescope_session", lambda: True)
    row = proton_bulk_dialog.ProtonRow(game.name, "shortcut", "GE-Current", False, game.game_dir,
                                      ((2147483649, "GE-Current"),), True)
    options = ([("GE-Previous", "GE-Previous")], {}, False)
    monkeypatch.setattr(proton_bulk_dialog, "collect_rows", lambda _: ([row], [], "GE-Current"))
    monkeypatch.setattr(proton_bulk_dialog, "collect_target_options", lambda *_: options)
    previous = Mock(return_value=game)
    game.proton_name = "GE-Previous"
    monkeypatch.setattr(proton_bulk_dialog, "use_previous_proton", previous)
    monkeypatch.setattr(QMessageBox, "information", lambda *_: QMessageBox.StandardButton.Ok)

    def run(dialog: QDialog) -> bool:
        dialog.findChild(QTableWidget).item(0, 0).setCheckState(Qt.CheckState.Checked)
        _button(dialog, "configure.btn_previous_proton").click()
        assert window._steam_busy
        _escape(dialog)
        assert dialog.isVisible()
        return True

    def finish(dialog: QDialog) -> bool:
        if not _button(dialog, "proton_bulk.btn_close").isEnabled():
            return False
        return _escape(dialog)

    _modal(lambda: proton_bulk_dialog._show(window, [row], [], "GE-Current", options), [run, finish])
    previous.assert_called_once_with(game.game_dir)
    edit, = window._vdf_queue.list_pending()
    assert edit.payload["target_tool"] == "GE-Previous"
    assert edit.payload["appids"] == [2147483649]


def test_single_optiscaler_queues_steam_options_and_keeps_other_overrides(
    window: ui.MainWindow, game: StandaloneGame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.paths = _paths(tmp_path)
    monkeypatch.setattr(ui, "is_gamescope_session", lambda: True)
    entry = ShortcutEntry("0", 2147483649, game.name, str(game.exe_path), str(game.game_dir), "",
                          'WINEDLLOVERRIDES="foo=b" %command%')
    state = OptiScalerState("dxgi", "auto", "1", game.game_dir, [], [], "dxgi=n,b")
    monkeypatch.setattr(prefix_tools_dialogs, "install_optiscaler_for_game", Mock(return_value=state))
    monkeypatch.setattr(prefix_tools_dialogs, "resolve_game_prefix", lambda *_: (game.prefix_dir, None))
    on_saved = Mock()

    def install(dialog: QDialog) -> bool:
        _button(dialog, "optiscaler.btn_install").click()
        _escape(dialog)
        assert dialog.isVisible()
        return True

    def finish(dialog: QDialog) -> bool:
        if not on_saved.called:
            return False
        return _escape(dialog)

    _modal(lambda: prefix_tools_dialogs.open_optiscaler_dialog(window, "steam", entry, on_saved=on_saved),
           [install, finish])
    edit, = window._vdf_queue.list_pending()
    options = edit.payload["launch_options"]
    assert "dxgi=n,b" in options and "foo=b" in options
    on_saved.assert_called_once_with(options)


def test_bulk_optiscaler_refreshes_before_returning_from_gaming_mode(
    window: ui.MainWindow, game: StandaloneGame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.paths = _paths(tmp_path)
    monkeypatch.setattr(ui, "is_gamescope_session", lambda: True)
    target = BulkTarget(game.name, game.exe_path, game.game_dir, game.prefix_dir, "", "shortcut")
    rows = [(target, OptiScalerCompatibility(True, "verified", "x64", ("FSR",)))]
    state = OptiScalerState("dxgi", "auto", "1", game.game_dir, [], [], "dxgi=n,b")
    updated = [(target, OptiScalerCompatibility(True, "verified", "x64", ("FSR",), installed_state=state))]
    monkeypatch.setattr(optiscaler_bulk_dialog, "_scan", lambda _: updated)
    monkeypatch.setattr(optiscaler_bulk_dialog, "_install_files", lambda *_a, **_k: (
        [BulkOutcome(game.name, "installed", "")], [("shortcut", 2147483649, game.name, "%command%")],
    ))
    monkeypatch.setattr(QMessageBox, "information", lambda *_: QMessageBox.StandardButton.Ok)

    def install(dialog: QDialog) -> bool:
        _button(dialog, "optiscaler_bulk.btn_select_all").click()
        _button(dialog, "optiscaler_bulk.btn_install").click()
        _escape(dialog)
        assert dialog.isVisible() and window._steam_busy
        return True

    def finish(dialog: QDialog) -> bool:
        if window._steam_busy:
            return False
        assert "dxgi.dll" in dialog.findChild(QTableWidget).item(0, 3).text()
        return _escape(dialog)

    _modal(lambda: optiscaler_bulk_dialog._show(window, rows), [install, finish])
    edit, = window._vdf_queue.list_pending()
    assert edit.op == "set_launch_options"


def test_relocation_runs_after_real_target_and_confirmation_dialogs(
    window: ui.MainWindow, game: StandaloneGame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Library("disk", tmp_path / "Disk", "Disk", False, True)
    plan = RelocatePlan(game, target, target.games_dir / game.name)
    monkeypatch.setattr(relocate_dialog.rl, "target_libraries", lambda _: [target])
    monkeypatch.setattr(relocate_dialog.rl, "plan_relocation", lambda *_: plan)
    move = Mock(return_value=game)
    monkeypatch.setattr(relocate_dialog.rl, "relocate_game", move)
    # Keep the timer-driven modal stack open while the background operation completes.
    outer = QDialog(window)
    QTimer.singleShot(0, outer, lambda: relocate_dialog.move_game_to_disk(window, game))

    def pick(dialog: QDialog) -> bool:
        if not isinstance(dialog, QInputDialog):
            return False
        dialog.accept()
        return True

    def confirm(dialog: QDialog) -> bool:
        if not isinstance(dialog, QMessageBox):
            return False
        assert window._steam_busy
        dialog.button(QMessageBox.StandardButton.Yes).click()
        return True

    def finish(dialog: QDialog) -> bool:
        if not isinstance(dialog, QMessageBox):
            return False
        assert "Disk" in dialog.text()
        dialog.button(QMessageBox.StandardButton.Ok).click()
        return True

    def done(dialog: QDialog) -> bool:
        if dialog is not outer or any(w.isRunning() for w in window._active_workers):
            return False
        assert not window._steam_busy
        dialog.accept()
        return True

    _modal(outer.exec, [pick, confirm, finish, done])
    move.assert_called_once()
    assert move.call_args.args == (plan,)


def _open_more(dialog: QDialog) -> None:
    next(b for b in dialog.findChildren(QToolButton) if b.text() == tr("launch.more_options")).click()


def test_launch_editor_drops_old_mangohud_in_steam_mode(window: ui.MainWindow) -> None:
    """Steam has its own overlay: MANGOHUD=1 kept from an older version leaves the line."""
    def edit(dialog: QDialog) -> bool:
        fps = dialog.findChild(QComboBox, "gameFps")
        fps.setCurrentIndex(fps.findData(45))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()
        return True

    result = _modal(lambda: launch_presets_dialog.open_launch_presets_dialog(
        window, "Game", "steam", "MANGOHUD=1 %command%", "", mode="steam"), [edit])
    assert isinstance(result, tuple)
    assert "MANGOHUD" not in result[0] and "45" in result[0]


def test_launch_editor_moves_standalone_dll_token_to_its_field(window: ui.MainWindow) -> None:
    """Standalone keeps WINEDLLOVERRIDES in its own field, so unticking DXGI really turns it off."""
    def edit(dialog: QDialog) -> bool:
        raw = dialog.findChild(QLineEdit, "gameLaunchOptions")
        assert "WINEDLLOVERRIDES" not in raw.text()
        _open_more(dialog)
        dxgi = next(c for c in dialog.findChildren(QCheckBox) if c.text() == tr("launch.dxgi_preset"))
        assert dxgi.isChecked()
        dxgi.click()
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()
        return True

    result = _modal(lambda: launch_presets_dialog.open_launch_presets_dialog(
        window, "Game", "standalone", "WINEDLLOVERRIDES=dxgi=n,b %command%", ""), [edit])
    assert result == ("", "")  # nothing left but %command%: the line is empty


def test_launch_editor_enforces_optiscaler_dxgi_without_more_options(
    window: ui.MainWindow, game: StandaloneGame, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OptiScaler as dxgi.dll needs the override even when only the FPS is changed."""
    from types import SimpleNamespace

    monkeypatch.setattr(launch_presets_dialog, "inspect_optiscaler_state",
                        lambda _exe: SimpleNamespace(proxy_dll="dxgi"))

    def edit(dialog: QDialog) -> bool:
        dxgi = next(c for c in dialog.findChildren(QCheckBox) if c.text() == tr("launch.dxgi_preset"))
        if dxgi.isEnabled():
            return False  # the OptiScaler check runs in the background
        assert dxgi.isChecked()
        fps = dialog.findChild(QComboBox, "gameFps")
        fps.setCurrentIndex(fps.findData(45))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()
        return True

    result = _modal(lambda: launch_presets_dialog.open_launch_presets_dialog(
        window, "Game", "standalone", "%command%", "", game_obj=game), [edit])
    assert isinstance(result, tuple) and "dxgi=n,b" in result[1]


def test_configure_shows_optiscaler_options_in_the_list(
    window: ui.MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OptiScaler's options sit in Configure's own list (More options), not behind a button."""
    from deckinstall import standalone
    from deckinstall.ui_dialogs import prefix_tools_dialogs
    from tests.conftest import pe_exe_bytes

    source = tmp_path / "Game"
    source.mkdir()
    exe = source / "game.exe"
    exe.write_bytes(pe_exe_bytes())
    monkeypatch.setattr(standalone, "resolve_game_icon", lambda *a, **kw: None)
    game = standalone.install_standalone_game("Game", exe, source, create_menu_shortcut=False)
    window._selected_library_item = lambda: ("standalone", game)
    monkeypatch.setattr(prefix_tools_dialogs, "inspect_optiscaler_state", lambda _exe: None)
    monkeypatch.setattr(prefix_tools_dialogs, "recommend_proxy", lambda *_a: ("dxgi", []))

    def check(dialog: QDialog) -> bool:
        more = next(b for b in dialog.findChildren(QToolButton) if b.text() == tr("launch.more_options"))
        more.click()
        install = _button(dialog, "optiscaler.btn_install")
        assert install.isVisibleTo(dialog)
        proxy = next(c for c in dialog.findChildren(QComboBox) if c.findData("dxgi") >= 0 and c.count() > 1)
        assert proxy.isVisibleTo(dialog)  # the options themselves, not a button to another window
        assert not any(b.text() == "OptiScaler" for b in dialog.findChildren(QPushButton))
        return _escape(dialog)

    _modal(lambda: configure_game_dialog.open_configure_game_dialog(window), [check])
