"""Regressions for the Qt, Proton, adoption and theme fixes in phase F."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidgetItem, QWidget

from deckinstall import adopt, theme, ui
from deckinstall.standalone import StandaloneGame
from deckinstall.steam_paths import SteamPaths
from deckinstall.steam_vdf import ShortcutEntry
from deckinstall.ui_cards import _BusyGuardedDialog, _dialog_guard
from deckinstall.ui_dialogs import proton_bulk_dialog


def test_busy_dialog_ignores_escape_and_window_close() -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    busy = True
    dialog = _BusyGuardedDialog(parent, lambda: busy)
    alive = _dialog_guard(dialog)
    dialog.show()
    try:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert alive() and dialog.isVisible()
        assert not dialog.close()
        assert alive() and dialog.isVisible()

        busy = False
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not alive()
        assert not dialog.isVisible()
    finally:
        busy = False
        dialog.close()
        parent.close()
        app.processEvents()


def test_library_search_stays_applied_after_sorting() -> None:
    app = QApplication.instance() or QApplication([])
    with patch.object(ui.MainWindow, "_refresh_games"), patch.object(ui.MainWindow, "_redetect_steam"):
        window = ui.MainWindow(app)
    try:
        table = window.shortcuts_list
        table.setSortingEnabled(False)
        table.setRowCount(2)
        table.setItem(0, 0, QTableWidgetItem("Alpha"))
        table.setItem(1, 0, QTableWidgetItem("Beta"))
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)
        window.library_search_input.setText("Alpha")
        app.processEvents()
        assert not table.isRowHidden(0) and table.isRowHidden(1)

        table.sortItems(0, Qt.SortOrder.DescendingOrder)
        app.processEvents()
        assert table.item(1, 0).text() == "Alpha"
        assert table.isRowHidden(0) and not table.isRowHidden(1)
    finally:
        window.close()


def test_runtime_update_unlocks_and_refreshes_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    win = Mock(_steam_busy=False, _closing_pending=False)
    updater = Mock()
    worker_cls = Mock(return_value=updater)
    monkeypatch.setattr(ui, "RuntimeUpdateWorker", worker_cls)
    monkeypatch.setattr(ui, "_dialog_parent", lambda parent: parent)
    monkeypatch.setattr(ui, "QMessageBox", Mock())

    ui.MainWindow._update_runtimes(win, ("mangohud",))  # one Runtimes card updates only its component
    assert worker_cls.call_args.args[1] == ("mangohud",)
    win._begin_busy.assert_called_once()
    win._end_busy.assert_not_called()
    updater.finished_ok.connect.call_args.args[0]("Updated", "")
    win._end_busy.assert_called_once()
    win._refresh_maintenance_health.assert_called_once()
    win._refresh_shortcuts.assert_called_once()


def test_update_all_skips_what_steamos_does_not_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall.modes import ModeEnv

    monkeypatch.setattr(ui, "get_umu_run_path", lambda: Path("/nonexistent/umu-run"))
    monkeypatch.setattr(ui, "list_default_proton_dirs", lambda: [Path("/x/UMU-Proton-11.0-1")])
    monkeypatch.setattr(ui, "official_mangohud_installed", lambda: True)
    monkeypatch.setattr(ui, "optiscaler_downloaded", lambda: True)
    win = Mock(mode_env=ModeEnv(steamos=True, steam=True))
    assert ui.MainWindow._runtime_components_for_update_all(win) == ("optiscaler",)
    win = Mock(mode_env=ModeEnv(steamos=False, steam=True))
    assert ui.MainWindow._runtime_components_for_update_all(win) == ("umu_proton", "mangohud", "optiscaler")
    win = Mock(mode_env=ModeEnv(steamos=False, steam=False))
    assert ui.MainWindow._runtime_components_for_update_all(win)[:2] == ("umu", "umu_proton")


def test_managed_windows_shortcut_pointing_to_launch_script_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_dir = tmp_path / "Game"
    game = StandaloneGame(
        "Game", game_dir, game_dir / "game.exe", tmp_path / "prefix", "GE-Proton11",
        None, "", "", game_dir / "launch.sh", tmp_path / "game.desktop",
    )
    appid = 0x80000001
    shortcut = ShortcutEntry("0", appid, game.name, f'"{game.launch_script}"', str(game_dir), "", "")
    other = ShortcutEntry("1", appid + 1, "Linux", str(tmp_path / "linux.sh"), str(tmp_path), "", "")
    paths = SteamPaths(tmp_path, "123", tmp_path, tmp_path / "shortcuts.vdf", tmp_path / "grid",
                       tmp_path / "config.vdf", tmp_path / "compatdata")
    monkeypatch.setattr(proton_bulk_dialog, "list_standalone_games", lambda: [game])
    monkeypatch.setattr(proton_bulk_dialog, "load_shortcuts", lambda _: [shortcut, other])
    monkeypatch.setattr(proton_bulk_dialog, "get_compat_tool_map", lambda _: {appid: "GE-Proton11"})
    monkeypatch.setattr(proton_bulk_dialog, "orphan_shortcut_mappings", lambda _: [])
    monkeypatch.setattr(proton_bulk_dialog, "newest_default_proton", lambda: None)

    rows, _, _ = proton_bulk_dialog.collect_rows(paths)

    assert [(row.source, row.game_dir, row.steam_tools) for row in rows] == [
        ("shortcut", game_dir, ((appid, "GE-Proton11"),))
    ]


def test_bulk_proton_ge_alone_does_not_count_as_official(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ge = tmp_path / "compatibilitytools.d" / "GE-Proton11"
    ge.mkdir(parents=True)
    (ge / "proton").touch()
    monkeypatch.setattr(proton_bulk_dialog, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(proton_bulk_dialog, "list_installed_compat_tools", lambda: [])
    installed = [("GE-Proton11", "GE-Proton11")]
    monkeypatch.setattr(proton_bulk_dialog, "list_installed_protons", lambda _: installed)

    _, _, official = proton_bulk_dialog.collect_target_options(None, ge.name)
    assert not official

    builtin = tmp_path / "steamapps" / "common" / "Proton 9.0"
    builtin.mkdir(parents=True)
    (builtin / "proton").touch()
    installed.append(("proton_9", "Proton 9.0"))
    options, _, official = proton_bulk_dialog.collect_target_options(None, ge.name)
    assert official
    assert ("proton_9", "Proton 9.0") in options


def test_batch_adoption_checks_combined_space_on_one_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destinations = [tmp_path / "lib1", tmp_path / "lib2"]
    for dest in destinations:
        dest.mkdir()
    disk_usage = Mock(return_value=SimpleNamespace(free=50))
    monkeypatch.setattr(adopt.shutil, "disk_usage", disk_usage)
    moves = [
        adopt.PlannedMove("game", tmp_path / f"source-{idx}", dest / "Game", 40, False)
        for idx, dest in enumerate(destinations)
    ]

    assert adopt._adoption_space(moves[:1])[1]
    disk_usage.reset_mock()
    assert not adopt._adoption_space(moves)[1]
    disk_usage.assert_called_once()


def test_auto_theme_uses_style_palette_when_scheme_is_unknown() -> None:
    light = QPalette()
    light.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    dark = QPalette()
    dark.setColor(QPalette.ColorRole.Window, QColor("#101010"))
    app = Mock()
    app.styleHints.return_value.colorScheme.return_value = Qt.ColorScheme.Unknown
    app.style.return_value.standardPalette.return_value = light
    app.palette.return_value = dark

    assert not theme.resolve_is_dark(app, "auto")
    app.style.return_value.standardPalette.return_value = dark
    app.palette.return_value = light
    assert theme.resolve_is_dark(app, "auto")


@pytest.mark.parametrize(("fg", "bg"), [
    ("warning_fg", "bg_window"), ("warning_fg", "warning_bg"),
    ("info_fg", "bg_window"), ("info_fg", "info_bg"), ("error_fg", "error_bg"),
    ("progress_fg", "progress_bg"), ("progress_fg", "progress_fill"),
    ("disabled_fg", "btn_disabled_bg"), ("primary_disabled_fg", "primary_disabled_bg"),
    ("danger_disabled_fg", "danger_disabled_bg"),
])
def test_light_theme_status_and_controls_contrast(fg: str, bg: str) -> None:
    def luminance(key: str) -> float:
        rgb = QColor(theme._LIGHT_TOKENS[key]).getRgbF()[:3]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                  for channel in rgb]
        return sum(component * weight for component, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    low, high = sorted([luminance(fg), luminance(bg)])
    assert (high + 0.05) / (low + 0.05) >= 4.5
