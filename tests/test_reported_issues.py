"""Regression tests for reported library, install-label and OptiScaler failures."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from deckinstall import optiscaler, save_sync, standalone
from deckinstall.ui_dialogs import save_sync_dialog


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
                self.assertEqual(win.btn_install.text(), tr("install.btn_install_in_place"))
                win.chk_keep_in_place.setChecked(False)
                self.assertEqual(win.btn_install.text(), tr("install.btn_install"))
            finally:
                for worker in list(win._active_workers):
                    worker.wait(5000)
                app.processEvents()
                win.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()


class UniqueLibraryActionTests(unittest.TestCase):
    def test_library_has_one_visible_entry_for_each_action(self) -> None:
        from PySide6.QtCore import QSettings, Qt
        from PySide6.QtWidgets import QApplication, QPushButton, QWidget

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
                self.assertFalse(hasattr(win, "_tools_menu"))
                remove = win.game_action_buttons[win.act_remove_game]  # a button in the row, not a menu
                self.assertIsNone(remove.menu())
                self.assertEqual(remove.text(), tr("library.btn_uninstall"))
                self.assertEqual(win.act_remove_game.text(), tr("library.btn_uninstall"))
                self.assertIsNone(win.btn_save_sync.menu())
                self.assertEqual(win.stack.count(), 4)  # Settings is one page
                self.assertFalse(hasattr(win, "_nav_buttons"))  # no sidebar
                self.assertEqual(win.findChildren(QWidget, "Sidebar"), [])
                self.assertEqual(win.stack.currentIndex(), ui.PAGE_LIBRARY)
                self.assertTrue(win.stack.widget(ui.PAGE_SETTINGS).isAncestorOf(win.theme_combo))
                self.assertTrue(win.stack.widget(ui.PAGE_SETTINGS).isAncestorOf(win.btn_redetect_steam))
                # Tabs on top replace the hub buttons and the "back" buttons.
                self.assertEqual(
                    [win.nav_tabs.tabText(i) for i in range(win.nav_tabs.count())],
                    [tr("nav.library"), tr("nav.install"), tr("nav.saves"), tr("nav.settings")],
                )
                self.assertEqual(win.nav_tabs.currentIndex(), 0)
                for tab, page in enumerate(ui.NAV_PAGES):
                    win.nav_tabs.setCurrentIndex(tab)
                    self.assertEqual(win.stack.currentIndex(), page)
                # Updates/components and Cleanup are sections of the one Settings page.
                self.assertFalse(hasattr(win, "settings_sections"))
                win._show_page(ui.PAGE_MAINT)
                self.assertEqual(win.stack.currentIndex(), ui.PAGE_SETTINGS)
                self.assertEqual(win.nav_tabs.currentIndex(), ui.NAV_PAGES.index(ui.PAGE_SETTINGS))
                self.assertTrue(win.stack.widget(ui.PAGE_SETTINGS).isAncestorOf(win.btn_maint_scan))
                win._show_page(ui.PAGE_INSTALL)  # code paths that jump to a page keep the tab in sync
                self.assertEqual(win.nav_tabs.currentIndex(), ui.NAV_PAGES.index(ui.PAGE_INSTALL))
                win._show_page(ui.PAGE_LIBRARY)
                self.assertEqual(win.nav_tabs.currentIndex(), 0)
                self.assertFalse(hasattr(win, "_hub_buttons"))
                self.assertFalse(hasattr(win, "btn_game_actions"))
                self.assertNotIn("⋯", [b.text() for b in win.stack.widget(ui.PAGE_LIBRARY).findChildren(QPushButton)])
                self.assertFalse(hasattr(win, "btn_tools_menu"))
                self.assertNotEqual(win.shortcuts_list.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
                labels = [button.text() for button in win.stack.widget(ui.PAGE_LIBRARY).findChildren(QPushButton)]
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
                for widget in (win.games_list, win.shortcuts_list):
                    self.assertIsInstance(widget, QTableWidget)
                    self.assertTrue(widget.isSortingEnabled())
                # Cleanup opens largest first and supports alternative sort orders.
                from deckinstall import ui_maintenance
                from deckinstall.cleanup import CleanupCandidate

                self.assertTrue(win.maint_list.isSortingEnabled())
                self.assertTrue(win.maint_list.horizontalHeader().sectionsClickable())

                def _cand(name: str, size: int, group: str) -> CleanupCandidate:
                    return CleanupCandidate("orphan", name, Path(tmp) / name, size, False, group=group)

                cands = [_cand("a", 10, "games"), _cand("b", 3000, "system"), _cand("c", 500, "games")]

                def _sizes() -> list[str]:
                    return [win.maint_list.item(r, 1).text().rsplit("/", 1)[-1] for r in range(win.maint_list.rowCount())]

                ui_maintenance._apply_maint_scan_results(win, cands)
                self.assertEqual(_sizes(), ["b", "c", "a"])
                ui_maintenance.on_maint_filter_changed(win, "games")
                self.assertEqual(_sizes(), ["c", "a"])
                ui_maintenance.on_maint_filter_changed(win, "all")
                ui_maintenance._apply_maint_scan_results(win, cands[:1] + [_cand("d", 99999, "games")])
                self.assertEqual(_sizes(), ["d", "a"])
                win.maint_list.sortItems(2, Qt.SortOrder.AscendingOrder)
                self.assertEqual(_sizes(), ["a", "d"])
                win.maint_list.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
                self.assertFalse(win._maint_checked[Path(tmp) / "d"])
                self.assertTrue(win._maint_checked[Path(tmp) / "a"])
            finally:
                for worker in list(win._active_workers):
                    worker.wait(5000)
                app.processEvents()
                win.close()
                app.processEvents()


class StandaloneRestoreAndSavesTabTests(unittest.TestCase):
    def test_restore_saves_backup_standalone_only_without_steam(self) -> None:
        from deckinstall import steam_vdf

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "Games" / "MyGame"
            pfx_dir = game_dir / "prefix"
            (pfx_dir / "drive_c").mkdir(parents=True)
            exe_path = game_dir / "MyGame.exe"
            exe_path.write_bytes(b"MZ")
            sa = standalone.StandaloneGame(
                name="MyGame",
                game_dir=game_dir,
                exe_path=exe_path,
                prefix_dir=pfx_dir,
                proton_name="GE-Proton11",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=game_dir / "launch.sh",
                desktop_file=root / "mygame.desktop",
            )
            zip_path = root / "backup.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("standalone-MyGame/users/steamuser/Documents/slot1.sav", b"SAVE123")
                zf.writestr("3000000001/users/steamuser/Documents/steam_only.sav", b"SKIPME")

            with (
                mock.patch.dict("os.environ", {"HOME": str(root)}),
                mock.patch.object(standalone, "list_standalone_games", return_value=[sa]),
            ):
                games, files = steam_vdf.restore_saves_backup(None, zip_path)

            self.assertEqual((games, files), (1, 1))
            restored = pfx_dir / "drive_c" / "users" / "steamuser" / "Documents" / "slot1.sav"
            self.assertTrue(restored.is_file())
            self.assertEqual(restored.read_bytes(), b"SAVE123")

    def test_list_save_archives_discovers_backups_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saves_dir = root / "saves"
            backups_dir = saves_dir / "backups"
            snap_dir = saves_dir / "snapshots" / "mygame"
            state_dir = snap_dir / "state"
            legacy_dir = root / "legacy_backups"
            for d in (backups_dir, state_dir, legacy_dir):
                d.mkdir(parents=True)

            (backups_dir / "saves-backup-20260101.zip").write_bytes(b"zip1")
            (legacy_dir / "legacy-backup.zip").write_bytes(b"zip2")
            (snap_dir / "20260102-manual.zip").write_bytes(b"zip3")
            (state_dir / "20260103-pre-winetricks.zip").write_bytes(b"zip4")

            entries = save_sync.list_save_archives(saves_dir, legacy_dir)
            kinds_by_name = {entry.path.name: (entry.kind, entry.game_slug) for entry in entries}
            self.assertEqual(
                kinds_by_name["saves-backup-20260101.zip"], ("backup", "")
            )
            self.assertEqual(kinds_by_name["legacy-backup.zip"], ("backup", ""))
            self.assertEqual(kinds_by_name["20260102-manual.zip"], ("save_snapshot", "mygame"))
            self.assertEqual(
                kinds_by_name["20260103-pre-winetricks.zip"], ("state_snapshot", "mygame")
            )


class LibraryColumnsFilterAndLogsTests(unittest.TestCase):
    def test_mainwindow_library_columns_filter_pills_and_saves_table(self) -> None:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from deckinstall import ui
        from deckinstall.launcher_scan import ExternalLauncherGame
        from deckinstall.steam_vdf import ShortcutEntry

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_archive = save_sync.SaveArchiveEntry(
                path=root / "bak.zip",
                kind="save_snapshot",
                label="[MyGame] snap.zip",
                game_slug="mygame",
                mtime=1700000000.0,
                size_bytes=2048,
            )
            with (
                mock.patch.object(
                    ui,
                    "QSettings",
                    return_value=QSettings(str(root / "prefs.ini"), QSettings.Format.IniFormat),
                ),
                mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[fake_archive]),
                mock.patch.object(ui.MainWindow, "_refresh_games"),
                mock.patch.object(ui.MainWindow, "_redetect_steam"),
            ):
                win = ui.MainWindow(app)
                try:
                    self.assertEqual(win.shortcuts_list.columnCount(), 7)
                    self.assertEqual(win.saves_list.columnCount(), 6)
                    self.assertEqual(win.saves_list.rowCount(), 1)
                    self.assertTrue(win.btn_save_restore.isEnabled())

                    sc = ShortcutEntry(
                        index="0",
                        appid_u32=3000000001,
                        app_name="Steam Game",
                        exe='"/games/steam/game.exe"',
                        start_dir='"/games/steam"',
                        icon="",
                        launch_options="",
                    )
                    sa = standalone.StandaloneGame(
                        name="Standalone Game",
                        game_dir=root / "sa",
                        exe_path=root / "sa" / "sa.exe",
                        prefix_dir=root / "sa" / "prefix",
                        proton_name="GE-Proton11",
                        steam_appid=None,
                        winedlloverrides="",
                        launch_options="",
                        launch_script=root / "sa" / "launch.sh",
                        desktop_file=root / "sa.desktop",
                    )
                    ext = ExternalLauncherGame(
                        source="lutris",
                        name="Lutris Game",
                        exe_path=root / "ext" / "ext.exe",
                        game_dir=root / "ext",
                        prefix_dir=None,
                        runner="wine",
                    )
                    runner_labels = {
                        ("steam", str(sc.appid_u32)): "GE-Proton9 [OptiScaler]",
                        ("standalone", str(sa.game_dir)): "GE-Proton11",
                        ("external", f"{ext.source}:{ext.name}:{ext.exe_path}"): "wine",
                    }
                    win._apply_shortcuts_data(
                        None, [sc], [sa], [ext], "", runner_labels=runner_labels
                    )
                    win.shortcuts_list.setSortingEnabled(False)
                    self.assertEqual(win.shortcuts_list.rowCount(), 3)
                    col2_texts = {
                        win.shortcuts_list.item(r, 0).text(): win.shortcuts_list.item(r, 2).text()
                        for r in range(3)
                        if win.shortcuts_list.item(r, 0) and win.shortcuts_list.item(r, 2)
                    }
                    self.assertEqual(col2_texts["Steam Game"], "GE-Proton9 [OptiScaler]")
                    self.assertEqual(col2_texts["Standalone Game"], "GE-Proton11")

                    win._on_library_source_filter_changed("steam")
                    visible_names = [
                        win.shortcuts_list.item(r, 0).text()
                        for r in range(win.shortcuts_list.rowCount())
                        if not win.shortcuts_list.isRowHidden(r)
                    ]
                    self.assertEqual(visible_names, ["Steam Game"])

                    win._on_library_source_filter_changed("external")
                    visible_names = [
                        win.shortcuts_list.item(r, 0).text()
                        for r in range(win.shortcuts_list.rowCount())
                        if not win.shortcuts_list.isRowHidden(r)
                    ]
                    self.assertEqual(visible_names, ["Lutris Game"])

                    # The adopted game belongs only to the Steam filter and uses its Steam shortcut.
                    win._on_library_source_filter_changed("all")
                    owned_sc = ShortcutEntry(
                        index="1", appid_u32=3000000002, app_name="Standalone Game",
                        exe=f'"{sa.exe_path}"', start_dir=f'"{sa.game_dir}"', icon="", launch_options="",
                    )
                    adopted_ext = ExternalLauncherGame(
                        source="lutris", name="Standalone Game", exe_path=sa.game_dir / "bin" / "sa.exe",
                        game_dir=sa.game_dir, prefix_dir=None, runner="wine",
                    )
                    win._apply_shortcuts_data(
                        None, [sc, owned_sc], [sa], [ext, adopted_ext], "", runner_labels=runner_labels
                    )
                    win.shortcuts_list.setSortingEnabled(False)
                    rows = {
                        win.shortcuts_list.item(r, 0).text(): win.shortcuts_list.item(r, 1).text()
                        for r in range(win.shortcuts_list.rowCount())
                    }
                    self.assertEqual(win.shortcuts_list.rowCount(), 3)
                    self.assertEqual(rows["Standalone Game"], "Steam")
                    win._on_library_source_filter_changed("steam")
                    visible_names = {
                        win.shortcuts_list.item(r, 0).text()
                        for r in range(win.shortcuts_list.rowCount())
                        if not win.shortcuts_list.isRowHidden(r)
                    }
                    self.assertEqual(visible_names, {"Steam Game", "Standalone Game"})
                    adopted_row = next(
                        r for r in range(win.shortcuts_list.rowCount())
                        if win.shortcuts_list.item(r, 0).text() == "Standalone Game"
                    )
                    win.shortcuts_list.setCurrentCell(adopted_row, 0)
                    self.assertEqual(win._selected_library_item(), ("steam", owned_sc))
                    self.assertEqual(win._managed_library_game(owned_sc), sa)
                    win._on_library_source_filter_changed("standalone")
                    self.assertTrue(all(win.shortcuts_list.isRowHidden(r) for r in range(3)))

                    win._on_library_source_filter_changed("all")
                    self.assertEqual(
                        sum(
                            1
                            for r in range(win.shortcuts_list.rowCount())
                            if not win.shortcuts_list.isRowHidden(r)
                        ),
                        3,
                    )
                finally:
                    for worker in list(win._active_workers):
                        worker.wait(5000)
                    app.processEvents()
                    win.close()
                    app.processEvents()

    def test_get_game_log_path_and_dir_size_bytes_many(self) -> None:
        from deckinstall import steam_paths, ui_cards

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d1 = root / "d1"
            d2 = root / "d2"
            d1.mkdir()
            d2.mkdir()
            (d1 / "a.bin").write_bytes(b"x" * 100)
            (d2 / "b.bin").write_bytes(b"y" * 250)
            self.assertEqual(steam_paths.dir_size_bytes_many([d1, d2, d1]), 350)

            with mock.patch.dict("os.environ", {"HOME": str(root)}):
                log_p = ui_cards.get_game_log_path("My: Game!")
            self.assertEqual(log_p, root / ".local" / "share" / "DeckInstall" / "logs" / "My__Game.log")


class UiUxSimplificationTests(unittest.TestCase):
    def test_unified_tab0_progressive_disclosure_smart_drop_and_maint_buttons(self) -> None:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from deckinstall import ui

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_file = root / "CoolGame-v1.zip"
            zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            game_dir = root / "CoolGame"
            game_dir.mkdir()
            game_exe = game_dir / "CoolGame.exe"
            game_exe.write_bytes(b"MZ")
            with (
                mock.patch.object(
                    ui,
                    "QSettings",
                    return_value=QSettings(str(root / "prefs.ini"), QSettings.Format.IniFormat),
                ),
                mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[]),
                mock.patch.object(ui.MainWindow, "_refresh_games"),
                mock.patch.object(ui.MainWindow, "_redetect_steam"),
            ):
                win = ui.MainWindow(app)
                try:
                    self.assertFalse(hasattr(win, "scan_add_dialog"))
                    self.assertFalse(hasattr(win, "add_advanced_box"))
                    self.assertFalse(hasattr(win, "btn_toggle_add_advanced"))
                    self.assertFalse(hasattr(win, "extract_name"))  # one name field: add_name
                    self.assertFalse(hasattr(win, "chk_verify_md5"))
                    self.assertTrue(win.extract_details_box.isHidden())
                    self.assertTrue(win.stack.widget(0).isAncestorOf(win.add_name))
                    self.assertTrue(win.stack.widget(ui.PAGE_SETTINGS).isAncestorOf(win.btn_update_runtimes))
                    self.assertTrue(win.stack.widget(ui.PAGE_SETTINGS).isAncestorOf(win.btn_updates_check))  # app + runtimes together

                    for always_visible in (win.chk_keep_in_place, win.add_library_combo, win.lbl_add_presets,
                                           win.btn_shortcut_menu):
                        self.assertTrue(win.stack.widget(0).isAncestorOf(always_visible))
                        self.assertFalse(always_visible.isHidden() and always_visible is not win.add_library_combo)
                    self.assertTrue(win.btn_shortcut_menu.isCheckable())
                    self.assertTrue(win.btn_shortcut_menu.isChecked())
                    self.assertTrue(win.btn_shortcut_desktop.isCheckable())
                    # No Steam: only Standalone mode, and the Steam mode button says why.
                    self.assertFalse(win.btn_mode_steam.isEnabled())
                    self.assertTrue(win.btn_mode_standalone.isChecked())
                    self.assertTrue(win.btn_mode_steam.toolTip())

                    setup_file = root / "CoolGameSetup" / "setup.exe"
                    setup_file.parent.mkdir(parents=True, exist_ok=True)
                    setup_file.write_bytes(b"MZ")

                    win._set_extract_source(str(setup_file))
                    for worker in list(win._active_workers):
                        self.assertTrue(worker.wait(5000))
                    app.processEvents()
                    self.assertEqual(win.extract_src.text(), str(setup_file))
                    self.assertEqual(win.add_name.text(), "CoolGameSetup")
                    self.assertFalse(win.extract_details_box.isHidden())

                    with mock.patch("PySide6.QtWidgets.QMessageBox.information") as info_box:
                        win._set_extract_source(str(zip_file))
                        for worker in list(win._active_workers):
                            self.assertTrue(worker.wait(5000))
                        app.processEvents()
                    info_box.assert_not_called()  # "extract it first" goes to the message bar
                    self.assertEqual(win.message_bar.text(), ui.tr("install.archive_extract_first"))
                    self.assertEqual(win.extract_src.text(), "")
                    self.assertTrue(win.extract_details_box.isHidden())

                    win._set_extract_source(str(game_exe))
                    for worker in list(win._active_workers):
                        self.assertTrue(worker.wait(5000))
                    app.processEvents()
                    self.assertEqual(win.add_name.text(), "CoolGame")
                    self.assertEqual(win.add_exe_combo.currentText(), str(game_exe))
                finally:
                    for worker in list(win._active_workers):
                        worker.wait(5000)
                    app.processEvents()
                    win.close()
                    app.processEvents()

    def test_smart_initial_tab_and_three_tab_configure_dialog(self) -> None:
        from PySide6.QtCore import QSettings, QTimer
        from PySide6.QtWidgets import QApplication, QComboBox, QTabWidget

        from deckinstall import ui
        from deckinstall.ui_dialogs import configure_game_dialog

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sa = standalone.StandaloneGame(
                name="Standalone Game",
                game_dir=root / "sa",
                exe_path=root / "sa" / "sa.exe",
                prefix_dir=root / "sa" / "prefix",
                proton_name="GE-Proton11",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=root / "sa" / "launch.sh",
                desktop_file=root / "sa.desktop",
            )
            sa.game_dir.mkdir(parents=True)
            sa.exe_path.write_bytes(b"MZ")
            with (
                mock.patch.object(
                    ui,
                    "QSettings",
                    return_value=QSettings(str(root / "empty.ini"), QSettings.Format.IniFormat),
                ),
                mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[]),
                mock.patch.object(ui.MainWindow, "_refresh_games"),
                mock.patch.object(ui.MainWindow, "_redetect_steam"),
            ):
                win_empty = ui.MainWindow(app)
                try:
                    win_empty._apply_shortcuts_data(None, [], [], [], "", runner_labels={})
                    self.assertEqual(win_empty.stack.currentIndex(), ui.PAGE_LIBRARY)  # always opens on the Library
                finally:
                    for worker in list(win_empty._active_workers):
                        worker.wait(5000)
                    win_empty.close()
                    app.processEvents()

            with (
                mock.patch.object(
                    ui,
                    "QSettings",
                    return_value=QSettings(str(root / "filled.ini"), QSettings.Format.IniFormat),
                ),
                mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[]),
                mock.patch.object(ui.MainWindow, "_refresh_games"),
                mock.patch.object(ui.MainWindow, "_redetect_steam"),
            ):
                win = ui.MainWindow(app)
                try:
                    win._apply_shortcuts_data(
                        None,
                        [],
                        [sa],
                        [],
                        "",
                        runner_labels={("standalone", str(sa.game_dir)): "GE-Proton11"},
                    )
                    self.assertEqual(win.stack.currentIndex(), 1)
                    win.shortcuts_list.selectRow(0)

                    observed: dict[str, int] = {}

                    def _inspect_and_close() -> None:
                        modal = QApplication.activeModalWidget()
                        if modal is None:
                            QTimer.singleShot(10, _inspect_and_close)
                            return
                        tabs = modal.findChild(QTabWidget)
                        combos = modal.findChildren(QComboBox)
                        observed["tabs"] = tabs.count() if tabs is not None else 0
                        observed["combos"] = len(combos)
                        modal.accept()

                    with (
                        mock.patch.object(
                            configure_game_dialog, "list_installed_compat_tools", return_value=[]
                        ),
                        mock.patch.object(
                            configure_game_dialog, "list_installed_protons", return_value=[]
                        ),
                        mock.patch.object(
                            configure_game_dialog,
                            "resolve_game_prefix",
                            return_value=(sa.prefix_dir, None),
                        ),
                        mock.patch.object(
                            configure_game_dialog, "is_win32_prefix", return_value=False
                        ),
                        mock.patch.object(
                            configure_game_dialog,
                            "get_umu_protonfix_status",
                            return_value={"found": False, "has_protonfix": False},
                        ),
                    ):
                        QTimer.singleShot(0, _inspect_and_close)
                        configure_game_dialog.open_configure_game_dialog(win)

                    self.assertEqual(observed.get("tabs"), 0)
                    self.assertGreaterEqual(observed.get("combos", 0), 1)
                finally:
                    for worker in list(win._active_workers):
                        worker.wait(5000)
                    app.processEvents()
                    win.close()
                    app.processEvents()
