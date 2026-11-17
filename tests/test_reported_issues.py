"""Regression tests for reported library, install-label and OptiScaler failures."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from deckinstall import optiscaler, save_sync, standalone


class OptiScalerSnapshotTests(unittest.TestCase):
    def test_install_recovers_original_backup_from_prior_interrupted_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "game.exe"
            exe.write_bytes(b"MZ")
            backup = root / "dxgi.dll.deckinstall.bak"
            backup.write_bytes(b"original-before-failed-install")
            payload = root / "payload"
            payload.mkdir()
            (payload / "OptiScaler.dll").write_bytes(b"replacement")
            state = optiscaler.install_optiscaler_for_game(exe, payload_dir=payload)
            self.assertIn(root / "dxgi.dll", state.backed_up_files)
            optiscaler.uninstall_optiscaler_for_game(exe)
            self.assertEqual((root / "dxgi.dll").read_bytes(), b"original-before-failed-install")

    def test_large_system_dll_does_not_block_install_or_remove_registry_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "game"
            game.mkdir()
            exe = game / "game.exe"
            exe.write_bytes(b"MZ")
            prefix = root / "prefix"
            system32 = prefix / "drive_c/windows/system32"
            system32.mkdir(parents=True)
            registry = prefix / "user.reg"
            registry.write_bytes(b"WINE REGISTRY Version 2\n")
            with (system32 / "xactengine2_9.dll").open("wb") as fh:
                fh.truncate(257 * 1024 * 1024)
            payload = root / "payload"
            payload.mkdir()
            (payload / "OptiScaler.dll").write_bytes(b"proxy")

            with mock.patch.object(save_sync, "DEFAULT_SNAPSHOTS_DIR", root / "snapshots"):
                state = optiscaler.install_optiscaler_for_game(
                    exe, payload_dir=payload, prefix_dir=prefix, game_name="Large prefix",
                )
            self.assertEqual((game / "dxgi.dll").read_bytes(), b"proxy")
            self.assertIsNotNone(state.prefix_snapshot)
            with zipfile.ZipFile(state.prefix_snapshot) as zf:
                self.assertEqual(zf.namelist(), ["reg/user.reg"])
                self.assertEqual(zf.read("reg/user.reg"), registry.read_bytes())
            registry.write_bytes(b"modified")
            self.assertEqual(save_sync.restore_prefix_state_snapshot(state.prefix_snapshot, prefix), (1, 0))
            self.assertEqual(registry.read_bytes(), b"WINE REGISTRY Version 2\n")

    def test_snapshot_failure_preserves_original_game_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "game.exe"
            exe.write_bytes(b"MZ")
            proxy = root / "dxgi.dll"
            proxy.write_bytes(b"original")
            payload = root / "payload"
            payload.mkdir()
            (payload / "OptiScaler.dll").write_bytes(b"replacement")
            prefix = root / "prefix"
            prefix.mkdir()
            with mock.patch.object(save_sync, "snapshot_prefix_state", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    optiscaler.install_optiscaler_for_game(
                        exe, payload_dir=payload, prefix_dir=prefix, overwrite_existing=True,
                    )
            self.assertEqual(proxy.read_bytes(), b"original")
            self.assertFalse((root / "dxgi.dll.deckinstall.bak").exists())


class StandaloneLibraryTests(unittest.TestCase):
    def _install(self, folder: Path) -> standalone.StandaloneGame:
        folder.mkdir(parents=True)
        exe = folder / "game.exe"
        exe.write_bytes(b"MZ")
        return standalone.install_standalone_game(
            "KIBORG", exe, folder, keep_in_place=True, create_menu_shortcut=False,
        )

    def test_nested_legacy_install_is_discovered_without_index_and_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games = root / "Games"
            with (
                mock.patch.object(standalone, "get_games_dir", return_value=games),
                mock.patch.object(standalone, "get_prefixes_dir", return_value=games / ".prefixes"),
                mock.patch.object(standalone, "get_applications_dir", return_value=root / "apps"),
                mock.patch.object(standalone, "resolve_game_icon", return_value=None),
            ):
                installed = self._install(games / "KIBORG/Kiborg")
                self.assertEqual([g.game_dir for g in standalone.list_standalone_games()], [installed.game_dir])
                standalone._library_index_path(installed.game_dir).unlink()
                self.assertEqual([g.game_dir for g in standalone.list_standalone_games()], [installed.game_dir])
                self.assertFalse(standalone.find_orphan_shader_caches(games))

    def test_in_place_game_outside_games_remains_in_library_and_can_be_unregistered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games = root / "Games"
            with (
                mock.patch.object(standalone, "get_games_dir", return_value=games),
                mock.patch.object(standalone, "get_prefixes_dir", return_value=games / ".prefixes"),
                mock.patch.object(standalone, "get_applications_dir", return_value=root / "apps"),
                mock.patch.object(standalone, "resolve_game_icon", return_value=None),
            ):
                installed = self._install(root / "Downloads/KIBORG")
                self.assertEqual([g.game_dir for g in standalone.list_standalone_games()], [installed.game_dir])
                self.assertTrue(installed.exe_path.is_file())
                self.assertFalse(standalone.find_orphan_shader_caches(games))
                standalone.remove_standalone_game(installed)
                self.assertEqual(standalone.list_standalone_games(), [])
                self.assertTrue(installed.exe_path.is_file())


class InstallButtonTests(unittest.TestCase):
    def test_actual_install_button_tracks_keep_in_place_option(self) -> None:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from deckinstall import ui
        from deckinstall.i18n import tr

        app = QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(ui, "QSettings", return_value=QSettings(str(Path(tmp) / "prefs.ini"), QSettings.Format.IniFormat)),
            mock.patch.object(ui, "resolve_steam_paths", return_value=None),
            mock.patch.object(ui, "find_steam_root", return_value=None),
            mock.patch.object(ui, "list_standalone_games", return_value=[]),
            mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
            mock.patch.object(ui, "scan_all_games", return_value=[]),
        ):
            win = ui.MainWindow(app)
            try:
                win.chk_keep_in_place.setChecked(True)
                self.assertEqual(win.btn_add_standalone.text(), tr("install.btn_add_standalone_in_place"))
                win.chk_keep_in_place.setChecked(False)
                self.assertEqual(win.btn_add_standalone.text(), tr("install.btn_add_standalone"))
            finally:
                for worker in list(win._active_workers):
                    worker.wait(5000)
                app.processEvents()
                win.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()


class ProtonUsageTests(unittest.TestCase):
    def test_manager_counts_standalone_games_without_steam_and_resolves_fallback(self) -> None:
        from types import SimpleNamespace

        from deckinstall.ui_dialogs import proton_dialogs

        games = [SimpleNamespace(name=f"Game {i}", proton_name="old-proton", native_linux=False) for i in range(5)]
        games.append(SimpleNamespace(name="Native", proton_name="", native_linux=True))
        with (
            mock.patch.object(proton_dialogs, "list_standalone_games", return_value=games),
            mock.patch.object(proton_dialogs, "resolve_standalone_proton_fallback", return_value=(Path('/tools/GE-Proton11-12'), 'GE-Proton11-12')),
            mock.patch.object(proton_dialogs, "list_compat_tool_usage") as steam_usage,
        ):
            usage = proton_dialogs._manager_tool_usage(None)
        self.assertEqual(len(usage), 5)
        self.assertEqual({entry.tool_name for entry in usage}, {"GE-Proton11-12"})
        self.assertEqual({entry.source for entry in usage}, {"Standalone"})
        steam_usage.assert_not_called()


class UniqueLibraryActionTests(unittest.TestCase):
    def test_library_has_one_visible_entry_for_each_action(self) -> None:
        from PySide6.QtCore import QSettings, Qt
        from PySide6.QtWidgets import QApplication, QPushButton

        from deckinstall import ui
        from deckinstall.i18n import tr

        app = QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(ui, 'QSettings', return_value=QSettings(str(Path(tmp) / 'prefs.ini'), QSettings.Format.IniFormat)),
            mock.patch.object(ui.MainWindow, '_refresh_games'),
            mock.patch.object(ui.MainWindow, '_redetect_steam'),
        ):
            win = ui.MainWindow(app)
            try:
                def leaves(menu):
                    result = []
                    for action in menu.actions():
                        if action.menu():
                            result.extend(leaves(action.menu()))
                        elif not action.isSeparator():
                            result.append(action)
                    return result

                game = leaves(win._game_actions_menu)
                library = leaves(win._tools_menu)
                self.assertEqual(game, [])
                self.assertEqual(library, [])
                self.assertEqual(win.btn_remove_game.objectName(), "dangerBtn")
                self.assertEqual(win.btn_remove_game.text(), tr("library.btn_remove_game"))
                self.assertIsNone(win.btn_remove_game.menu())
                self.assertIsNone(win.btn_save_sync.menu())
                self.assertEqual(win.stack.count(), 4)
                self.assertEqual(win._nav_buttons[2].text(), tr("tabs.saves"))
                self.assertFalse(hasattr(win, "btn_game_actions"))
                self.assertFalse(hasattr(win, "btn_tools_menu"))
                self.assertNotEqual(win.shortcuts_list.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
                labels = [button.text() for button in win.findChildren(QPushButton)]
                self.assertEqual(labels.count(tr('library.btn_play')), 1)
                self.assertEqual(labels.count(tr('library.btn_configure_game')), 1)
            finally:
                win.close()


class SortableTablesTests(unittest.TestCase):
    def test_tables_are_sortable_and_numeric_size_items_sort_by_bytes(self) -> None:
        from PySide6.QtCore import QSettings, Qt
        from PySide6.QtWidgets import QApplication, QTableWidget

        from deckinstall import ui
        from deckinstall.ui_cards import _SortableTableItem

        app = QApplication.instance() or QApplication([])
        small = _SortableTableItem("500.0 MB", 500 * 1024 * 1024)
        large = _SortableTableItem("1.50 GB", int(1.5 * 1024 * 1024 * 1024))
        self.assertLess(small, large)
        self.assertGreater(small.text(), large.text())

        tbl = QTableWidget(2, 1)
        tbl.setSortingEnabled(False)
        tbl.setItem(0, 0, large)
        tbl.setItem(1, 0, small)
        tbl.setSortingEnabled(True)
        tbl.sortItems(0, Qt.SortOrder.AscendingOrder)
        first = tbl.item(0, 0)
        second = tbl.item(1, 0)
        assert first is not None and second is not None
        self.assertEqual(first.text(), "500.0 MB")
        self.assertEqual(second.text(), "1.50 GB")

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                ui,
                "QSettings",
                return_value=QSettings(str(Path(tmp) / "prefs.ini"), QSettings.Format.IniFormat),
            ),
            mock.patch.object(ui.MainWindow, "_refresh_games"),
            mock.patch.object(ui.MainWindow, "_redetect_steam"),
        ):
            win = ui.MainWindow(app)
            try:
                for widget in (win.games_list, win.shortcuts_list, win.maint_list):
                    self.assertIsInstance(widget, QTableWidget)
                    self.assertTrue(widget.isSortingEnabled())
            finally:
                for worker in list(win._active_workers):
                    worker.wait(5000)
                app.processEvents()
                win.close()
                app.processEvents()
