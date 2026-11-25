"""Core unit tests for DeckInstall."""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import vdf  # type: ignore  # noqa: F401
except ImportError:
    fake_vdf = types.ModuleType("vdf")
    fake_vdf.binary_loads = lambda b: {"shortcuts": {}}
    fake_vdf.binary_dumps = lambda d: b"VDF"
    fake_vdf.loads = lambda s: {"InstallConfigStore": {"Software": {"Valve": {"Steam": {"CompatToolMapping": {}}}}}}
    fake_vdf.dumps = lambda d, pretty=True: "{}"
    sys.modules["vdf"] = fake_vdf

from conftest import pe_exe_bytes  # noqa: E402

from deckinstall.cleanup import find_cleanup_candidates  # noqa: E402
from deckinstall.extractor import (  # noqa: E402
    _select_extract_cmd,
    extract_archive_to_dir,
    is_archive,
    sanitize_folder_name,
    suggest_target_name,
)
from deckinstall.scanner import (  # noqa: E402
    clean_game_name,
    detect_steam_appid,
    scan_all_games,
    scan_game_folder,
)
from deckinstall.steam_paths import SteamPaths, find_default_proton_name, list_steam_users  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    ShortcutEntry,
    _backup_file,
    compute_shortcut_appid,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    plan_bulk_migration,
    sync_all_external_saves,
)

_ORIG_UNRAISABLE_HOOK = None
_UNRAISABLE_ERRORS: list[str] = []


def setUpModule() -> None:
    """Capture unraisable exceptions during tests (#15)."""
    global _ORIG_UNRAISABLE_HOOK
    _UNRAISABLE_ERRORS.clear()
    _ORIG_UNRAISABLE_HOOK = sys.unraisablehook

    def _capture(unraisable: object) -> None:
        _UNRAISABLE_ERRORS.append(repr(unraisable))
        if _ORIG_UNRAISABLE_HOOK is not None:
            _ORIG_UNRAISABLE_HOOK(unraisable)

    sys.unraisablehook = _capture


def tearDownModule() -> None:
    """Restore unraisablehook and fail if any unraisable exception occurred."""
    global _ORIG_UNRAISABLE_HOOK
    gc.collect()
    if _ORIG_UNRAISABLE_HOOK is not None:
        sys.unraisablehook = _ORIG_UNRAISABLE_HOOK
        _ORIG_UNRAISABLE_HOOK = None
    if _UNRAISABLE_ERRORS:
        raise AssertionError(f"Unraisable exceptions during tests: {_UNRAISABLE_ERRORS}")


class CoreTests(unittest.TestCase):
    def test_clean_game_name(self) -> None:
        self.assertEqual(
            clean_game_name("Atomic.Heart.Ultimate.Edition-InsaneRamZes"),
            "Atomic Heart",
        )
        self.assertEqual(
            clean_game_name("Stellar.Blade.Complete.Edition-InsaneRamZes"),
            "Stellar Blade",
        )

    def test_archive_detection_and_name(self) -> None:
        self.assertTrue(is_archive(Path("My.Game.v1.0-RUNE.rar")))
        self.assertTrue(is_archive(Path("Game.tar.zst")))
        self.assertFalse(is_archive(Path("setup.exe")))
        self.assertEqual(suggest_target_name(Path("Takeover.2-RUNE.zip")), "Takeover 2")

    def test_scan_game_folder_ranking_and_appid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "STAR WARS - Galactic Racer"
            bin_dir = game_dir / "Griffin" / "Binaries" / "Win64"
            bin_dir.mkdir(parents=True)
            (game_dir / "unins000.exe").write_bytes(b"0" * 100)
            (game_dir / "UnityCrashHandler64.exe").write_bytes(b"0" * 100)
            (game_dir / "Launcher.exe").write_bytes(pe_exe_bytes(extra=b"0" * 100))
            shipping = bin_dir / "SWGR-Win64-Shipping.exe"
            shipping.write_bytes(pe_exe_bytes(extra=b"0" * 200))
            (game_dir / "steam_appid.txt").write_text("4078430\n", encoding="utf-8")

            detected = scan_game_folder(game_dir)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.primary_exe, shipping)
            self.assertEqual(detected.steam_appid, 4078430)
            self.assertEqual(detect_steam_appid(game_dir), 4078430)

    def test_nested_steam_appid_emu_ini_and_stable_proton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "StellarBlade" / "SB" / "Binaries" / "Win64"
            nested.mkdir(parents=True)
            (nested / "steam_appid.txt").write_text("3489700\n", encoding="utf-8")
            self.assertEqual(detect_steam_appid(root / "StellarBlade"), 3489700)

            emu_dir = root / "EmuGame" / "Plugins"
            emu_dir.mkdir(parents=True)
            (emu_dir / "steam_emu.ini").write_text("[Settings]\nAppId=4423270\n", encoding="utf-8")
            self.assertEqual(detect_steam_appid(root / "EmuGame"), 4423270)

            common = root / "steam" / "steamapps" / "common"
            (common / "Proton - Experimental").mkdir(parents=True)
            (common / "Proton - Experimental" / "proton").write_text("", encoding="utf-8")
            (common / "Proton 11.0").mkdir(parents=True)
            (common / "Proton 11.0" / "proton").write_text("", encoding="utf-8")
            self.assertEqual(find_default_proton_name(root / "steam"), "proton_11")

    def test_scan_all_games_ignores_unreadable_and_broken_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locked = root / "locked_dir"
            locked.mkdir()
            (locked / "Secret.exe").write_bytes(pe_exe_bytes())
            valid = root / "ValidGame"
            valid.mkdir()
            (valid / "Game.exe").write_bytes(pe_exe_bytes())
            broken_link = valid / "Broken.exe"
            broken_link.symlink_to(root / "does_not_exist.exe")

            os.chmod(locked, 0o000)
            try:
                games = scan_all_games([root, root / "missing_root"])
                self.assertEqual(len(games), 1)
                self.assertEqual(games[0].name, "ValidGame")
            finally:
                os.chmod(locked, 0o755)

    def test_plan_bulk_migration_and_compatdata_preserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_game = root / "Downloads" / "Stellar.Blade.Complete.Edition-InsaneRamZes"
            src_game.mkdir(parents=True)
            old_exe = src_game / "SB.exe"
            old_exe.write_bytes(b"MZ")
            (src_game / "steam_appid.txt").write_text("3489700\n", encoding="utf-8")

            games_root = root / "Games"
            steam_root = root / "Steam"
            (steam_root / "steamapps" / "common" / "Proton 11.0").mkdir(parents=True)
            (steam_root / "steamapps" / "common" / "Proton 11.0" / "proton").write_text("", encoding="utf-8")
            compat_dir = steam_root / "steamapps" / "compatdata"
            grid_dir = steam_root / "userdata" / "123" / "config" / "grid"
            grid_dir.mkdir(parents=True)
            config_vdf = steam_root / "config" / "config.vdf"
            config_vdf.parent.mkdir(parents=True)
            config_vdf.write_text("{}", encoding="utf-8")

            old_appid, _ = compute_shortcut_appid(f'"{old_exe}"', "SB.exe")
            (compat_dir / str(old_appid) / "pfx").mkdir(parents=True)
            (compat_dir / str(old_appid) / "pfx" / "save.dat").write_text("my-save", encoding="utf-8")

            entry = ShortcutEntry(
                index="0",
                appid_u32=old_appid,
                app_name="SB.exe",
                exe=f'"{old_exe}"',
                start_dir=f'"{src_game}"',
                icon="",
                launch_options="",
            )
            plan = plan_bulk_migration([entry], games_root, compat_dir)
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].new_root.name, "Stellar Blade")

    def test_sanitize_folder_name_and_tar_priority(self) -> None:
        self.assertEqual(sanitize_folder_name("My Game"), "My Game")
        with self.assertRaises(ValueError):
            sanitize_folder_name("../escape")
        with self.assertRaises(ValueError):
            sanitize_folder_name("/abs/path")
        cmd = _select_extract_cmd(Path("game.tar.gz"), Path("/tmp/out"))
        self.assertEqual(cmd[0], "tar")

    def test_infer_game_root_skips_binaries_win64_and_guards_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Custom" / "MyGame" / "Binaries" / "Win64" / "Game-Win64-Shipping.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            self.assertEqual(infer_game_root_from_exe(exe), (root / "Custom" / "MyGame").resolve())

            dl_dir = root / "Downloads"
            dl_dir.mkdir(parents=True)
            loose = dl_dir / "LooseGame.exe"
            loose.write_bytes(b"MZ")
            with mock.patch("pathlib.Path.home", return_value=root):
                with self.assertRaises(ValueError):
                    infer_game_root_from_exe(loose)

    def test_backup_file_rotation_and_loginusers_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "shortcuts.vdf"
            target.write_bytes(b"v1")
            for _ in range(7):
                _backup_file(target, keep=5)
            backups = list(root.glob("shortcuts.vdf.*.deckinstall.bak"))
            self.assertLessEqual(len(backups), 5)
            self.assertGreaterEqual(len(backups), 1)

            steam_root = root / "Steam"
            (steam_root / "userdata" / "100").mkdir(parents=True)
            (steam_root / "userdata" / "200").mkdir(parents=True)
            (steam_root / "config").mkdir(parents=True)
            (steam_root / "config" / "loginusers.vdf").write_text("users", encoding="utf-8")

            fake_login = {
                "users": {
                    str(76561197960265728 + 100): {"MostRecent": "0", "Timestamp": "100"},
                    str(76561197960265728 + 200): {"MostRecent": "1", "Timestamp": "200"},
                }
            }
            with mock.patch("vdf.loads", return_value=fake_login):
                ordered = list_steam_users(steam_root)
            self.assertEqual(ordered[0], "200")

    def test_heroic_lutris_prefix_import_and_plan_detects_pending_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_root = root / "Games"
            game_dir = games_root / "My Indie Game"
            game_dir.mkdir(parents=True)
            exe = game_dir / "Game.exe"
            exe.write_bytes(b"MZ")

            lutris_root = root / "lutris" / "prefixes"
            save_file = lutris_root / "my-indie-game" / "drive_c" / "users" / "deck" / "Documents" / "save.sav"
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"lutris-save")

            compat_dir = root / "Steam" / "steamapps" / "compatdata"
            appid_u32, _ = compute_shortcut_appid(f'"{exe}"', "My Indie Game")
            entry = ShortcutEntry(
                index="0",
                appid_u32=appid_u32,
                app_name="My Indie Game",
                exe=f'"{exe}"',
                start_dir=f'"{game_dir}"',
                icon="",
                launch_options="",
            )
            plan = plan_bulk_migration([entry], games_root, compat_dir, search_roots=[lutris_root])
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].new_root.name, "My Indie Game")

            paths = SteamPaths(
                steam_root=root / "Steam",
                user_id="1",
                userdata_dir=root / "Steam" / "userdata" / "1",
                shortcuts_vdf=root / "shortcuts.vdf",
                grid_dir=root / "grid",
                config_vdf=root / "config.vdf",
                compatdata_dir=compat_dir,
            )
            copied = import_external_prefix_saves(
                paths, appid_u32, "My Indie Game", search_roots=[lutris_root]
            )
            self.assertEqual(copied, 1)
            self.assertEqual(
                (compat_dir / str(appid_u32) / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "save.sav").read_bytes(),
                b"lutris-save",
            )
            self.assertEqual(
                plan_bulk_migration([entry], games_root, compat_dir, search_roots=[lutris_root]),
                [],
            )

    def test_find_and_delete_cleanup_candidates_protects_active_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            steamapps = steam_root / "steamapps"
            compat_dir = steamapps / "compatdata"
            (steamapps / "appmanifest_12345.acf").parent.mkdir(parents=True, exist_ok=True)
            (steamapps / "appmanifest_12345.acf").write_text("", encoding="utf-8")

            # Protected compatdata: 0 (Steam default), 12345 (Steam game), 3000000001 (active shortcut)
            for pid in ("0", "12345", "3000000001"):
                (compat_dir / pid / "pfx" / "drive_c" / "users").mkdir(parents=True)
                (compat_dir / pid / "data.bin").write_bytes(b"x" * 100)

            # Orphan compatdata + old backup + external Hydra prefix
            orphan = compat_dir / "3999999999"
            (orphan / "pfx" / "drive_c" / "users").mkdir(parents=True)
            (orphan / "big.bin").write_bytes(b"y" * 2048)

            bak = compat_dir / "3000000001.20261114.deckinstall.bak"
            bak.mkdir(parents=True)
            (bak / "old.bin").write_bytes(b"z" * 512)

            ext_root = root / "hydra" / "wine-prefixes"
            ext_pfx = ext_root / "4078430"
            (ext_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            (ext_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat").write_bytes(b"s" * 256)

            shortcuts_vdf = steam_root / "userdata" / "1" / "config" / "shortcuts.vdf"
            shortcuts_vdf.parent.mkdir(parents=True)
            shortcuts_vdf.write_bytes(b"VDF")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=shortcuts_vdf,
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat_dir,
            )
            active_entry = ShortcutEntry("0", 3000000001, "Active", '"/g/a.exe"', '"/g"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[active_entry]):
                cands = find_cleanup_candidates(paths, extra_external_roots=[ext_root])

                cand_paths = {c.path for c in cands}
                self.assertEqual(cand_paths, {orphan, bak, ext_pfx})
                ext_cand = next(c for c in cands if c.path == ext_pfx)
                self.assertTrue(ext_cand.has_saves)

                from deckinstall.cleanup import delete_cleanup_candidates_checked

                deleted, freed, _ = delete_cleanup_candidates_checked(
                    paths, [c for c in cands if c.path in (orphan, bak)], allow_unverified_saves=True
                )
            self.assertEqual(deleted, 2)
            self.assertGreaterEqual(freed, 2560)
            self.assertFalse(orphan.exists())
            self.assertTrue((compat_dir / "3000000001").exists())

    def test_rollback_drive_c_guard_and_multiuser_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1) drive_c guard in infer_game_root_from_exe
            pfx_exe = root / ".config" / "hydralauncher" / "wine-prefixes" / "123" / "drive_c" / "Game" / "g.exe"
            pfx_exe.parent.mkdir(parents=True)
            pfx_exe.write_bytes(b"MZ")
            with self.assertRaises(ValueError):
                infer_game_root_from_exe(pfx_exe)

            # 2) multi-user compatdata protection + backup has_saves detection
            paths = SteamPaths(
                steam_root=root / "Steam",
                user_id="1",
                userdata_dir=root / "Steam" / "userdata" / "1",
                shortcuts_vdf=root / "Steam" / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=root / "Steam" / "userdata" / "1" / "config" / "grid",
                config_vdf=root / "Steam" / "config" / "config.vdf",
                compatdata_dir=root / "Steam" / "steamapps" / "compatdata",
            )
            (root / "Steam" / "userdata" / "1").mkdir(parents=True, exist_ok=True)
            (root / "Steam" / "userdata" / "2" / "config").mkdir(parents=True, exist_ok=True)
            other_compat = paths.compatdata_dir / "3555555555"
            (other_compat / "pfx" / "drive_c" / "users").mkdir(parents=True)
            bak_with_save = paths.compatdata_dir / "3000000002.20261114.deckinstall.bak"
            (bak_with_save / "pfx" / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            (bak_with_save / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "s.dat").write_bytes(b"save")

            other_entry = ShortcutEntry("0", 3555555555, "OtherUserGame", '"/g/o.exe"', '"/g"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", side_effect=[[], [], [other_entry]]):
                cands = find_cleanup_candidates(paths, extra_external_roots=[])
            self.assertNotIn(other_compat, {c.path for c in cands})
            bak_cand = next(c for c in cands if c.path == bak_with_save)
            self.assertTrue(bak_cand.has_saves)

    def test_missing_proton_triggers_bulk_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_root = root / "Games" / "My Game"
            new_root.mkdir(parents=True)
            exe = new_root / "Game.exe"
            exe.write_bytes(b"MZ")
            appid_u32, _ = compute_shortcut_appid(f'"{exe}"', "My Game")
            entry = ShortcutEntry("0", appid_u32, "My Game", f'"{exe}"', f'"{new_root}"', "", "")
            cfg = root / "config.vdf"
            cfg.write_text('{"InstallConfigStore":{}}', encoding="utf-8")
            with mock.patch(
                "vdf.loads",
                return_value={
                    "InstallConfigStore": {
                        "Software": {"Valve": {"Steam": {"CompatToolMapping": {str(appid_u32): {"name": ""}}}}}
                    }
                },
            ):
                plan = plan_bulk_migration([entry], root / "Games", root / "compat", search_roots=[], config_vdf=cfg)
            self.assertEqual(len(plan), 1)

    def test_sync_all_external_saves_and_verified_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            compat_dir = steam_root / "steamapps" / "compatdata"
            game_dir = root / "Games" / "Hollow Knight"
            game_dir.mkdir(parents=True)
            exe = game_dir / "hollow_knight.exe"
            exe.write_bytes(b"MZ")
            appid_u32 = 3123456789
            (compat_dir / str(appid_u32) / "pfx" / "drive_c" / "users").mkdir(parents=True)

            ext_root = root / "Heroic" / "Prefixes"
            ext_pfx = ext_root / "Hollow Knight"
            save_src = ext_pfx / "drive_c" / "users" / "deck" / "AppData" / "LocalLow" / "save.dat"
            save_src.parent.mkdir(parents=True)
            save_src.write_bytes(b"SAVE123")

            shortcuts_vdf = steam_root / "userdata" / "1" / "config" / "shortcuts.vdf"
            shortcuts_vdf.parent.mkdir(parents=True)
            shortcuts_vdf.write_bytes(b"VDF")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=shortcuts_vdf,
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat_dir,
            )
            entry = ShortcutEntry("0", appid_u32, "Hollow Knight", f'"{exe}"', f'"{game_dir}"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[entry]):
                before = find_cleanup_candidates(paths, extra_external_roots=[ext_root])
                self.assertEqual(len(before), 1)
                self.assertTrue(before[0].has_saves)
                self.assertFalse(before[0].saves_verified_migrated)

                checked, copied = sync_all_external_saves(paths, extra_external_roots=[ext_root])
                self.assertEqual(checked, 1)
                self.assertEqual(copied, 1)

                after = find_cleanup_candidates(paths, extra_external_roots=[ext_root])
                self.assertEqual(len(after), 1)
                self.assertTrue(after[0].has_saves)
                self.assertTrue(after[0].saves_verified_migrated)
                self.assertEqual(after[0].matched_game, "Hollow Knight")

    def test_security_fixes_hash_active_prefix_and_shell_assign(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 3) External prefix still referenced by shortcut exe is excluded from cleanup
            steam_root = root / "Steam"
            compat_dir = steam_root / "steamapps" / "compatdata"
            compat_dir.mkdir(parents=True)
            ext_root = root / "Heroic" / "Prefixes"
            active_pfx = ext_root / "StillActive"
            active_exe = active_pfx / "drive_c" / "Game" / "g.exe"
            active_exe.parent.mkdir(parents=True)
            active_exe.write_bytes(b"MZ")
            (active_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            shortcuts_vdf = steam_root / "userdata" / "1" / "config" / "shortcuts.vdf"
            shortcuts_vdf.parent.mkdir(parents=True)
            shortcuts_vdf.write_bytes(b"VDF")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=shortcuts_vdf,
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat_dir,
            )
            entry = ShortcutEntry("0", 3000000099, "StillActive", f'"{active_exe}"', f'"{active_exe.parent}"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[entry]):
                cands = find_cleanup_candidates(paths, extra_external_roots=[ext_root])
            self.assertEqual(cands, [])

    def test_i18n_and_artwork_backup_restore(self) -> None:
        from deckinstall.artwork import backup_steam_artwork
        from deckinstall.i18n import available_languages, set_language, tr

        langs = dict(available_languages())
        self.assertIn("en", langs)
        self.assertIn("pt_BR", langs)
        self.assertEqual(set_language("en"), "en")
        self.assertEqual(tr("header.about"), "About")
        self.assertEqual(set_language("pt_BR"), "pt_BR")
        self.assertEqual(tr("header.about"), "Sobre")
        self.assertEqual(set_language("unknown_xx"), "en")

        with tempfile.TemporaryDirectory() as tmp:
            grid = Path(tmp) / "grid"
            grid.mkdir()
            appid = 3111111111
            (grid / f"{appid}p.jpg").write_bytes(b"ORIG_CAPSULE")
            (grid / f"{appid}.jpg").write_bytes(b"ORIG_HEADER")
            backed = backup_steam_artwork(appid, grid)
            self.assertEqual(set(backed), {f"{appid}p.jpg", f"{appid}.jpg"})

    def test_backup_restore_symlinks_proton_and_launch_presets(self) -> None:
        from deckinstall.steam_paths import list_installed_protons
        from deckinstall.steam_vdf import (
            apply_launch_presets,
            backup_all_saves,
            restore_saves_backup,
            sync_prefix_symlinks,
        )

        # 1) Launch presets
        opts = apply_launch_presets("--fullscreen", gamemode=True, mangohud=True, wined3d=False, no_esync=True, no_fsync=False)
        self.assertIn("MANGOHUD=1", opts)
        self.assertIn("PROTON_NO_ESYNC=1", opts)
        self.assertIn("gamemoderun", opts)
        self.assertIn("%command%", opts)
        self.assertIn("--fullscreen", opts)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            compat_dir = steam_root / "steamapps" / "compatdata"
            compat_tools = steam_root / "compatibilitytools.d" / "GE-Proton9-20"
            compat_tools.mkdir(parents=True)
            (compat_tools / "compatibilitytool.vdf").write_text("{}", encoding="utf-8")
            proton_exp = steam_root / "steamapps" / "common" / "Proton - Experimental"
            proton_exp.mkdir(parents=True)
            (proton_exp / "proton").write_bytes(b"#!/bin/sh\n")

            protons = dict(list_installed_protons(steam_root))
            self.assertIn("GE-Proton9-20", protons)
            self.assertIn("proton_experimental", protons)

            appid = 3222222222
            save_file = compat_dir / str(appid) / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat"
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"MY_SAVE_DATA")

            shortcuts_vdf = steam_root / "userdata" / "1" / "config" / "shortcuts.vdf"
            shortcuts_vdf.parent.mkdir(parents=True)
            shortcuts_vdf.write_bytes(b"VDF")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=shortcuts_vdf,
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat_dir,
            )
            entry = ShortcutEntry("0", appid, "TestGame", '"/g/t.exe"', '"/g"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[entry]):
                zpath, games, files = backup_all_saves(
                    paths, dest_root=root / "Backups", games_dir=root / "Games"
                )
                self.assertEqual((games, files), (1, 1))
                self.assertTrue(zpath.is_file())

                save_file.write_bytes(b"CORRUPTED")
                rgames, rfiles = restore_saves_backup(paths, zpath)
                self.assertEqual((rgames, rfiles), (1, 1))
                self.assertEqual(save_file.read_bytes(), b"MY_SAVE_DATA")

                created, removed = sync_prefix_symlinks(paths, links_root=root / "Links")
                self.assertEqual((created, removed), (1, 0))
                self.assertTrue((root / "Links" / "TestGame").is_symlink())

    def test_v073_audit_regressions(self) -> None:
        from deckinstall.artwork import backup_steam_artwork
        from deckinstall.cleanup import CleanupCandidate, delete_cleanup_candidates_checked
        from deckinstall.fsutil import file_sha256
        from deckinstall.scanner import clean_game_name
        from deckinstall.steam_paths import SteamPaths
        from deckinstall.steam_vdf import (
            ShortcutEntry,
            apply_launch_presets,
            backup_all_saves,
            restore_saves_backup,
            sync_prefix_symlinks,
            update_shortcut_launch_options,
        )

        # 1) clean_game_name preserves legitimate words containing scene tag substrings
        self.assertEqual(clean_game_name("Old School RuneScape"), "Old School RuneScape")
        self.assertEqual(clean_game_name("Hades.II-RUNE"), "Hades II")

        # 2) apply_launch_presets keeps env vars before gamemoderun and game flags after %command%
        opts = apply_launch_presets(
            "FOO=bar --fullscreen",
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertEqual(opts, "FOO=bar MANGOHUD=1 gamemoderun %command% --fullscreen")
        opts_env = apply_launch_presets(
            "LOG=$HOME/out.log %command% -windowed",
            gamemode=True,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertIn("LOG=$HOME/out.log", opts_env)
        self.assertTrue(opts_env.endswith("%command% -windowed"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 3) _file_sha256 detects replacement with identical size & mtime_ns (different inode)
            f1 = root / "save.dat"
            f1.write_bytes(b"AAAA")
            ts_ns = 1_700_000_000_000_000_000
            os.utime(f1, ns=(ts_ns, ts_ns))
            h1 = file_sha256(f1)
            f2 = root / "save.dat.new"
            f2.write_bytes(b"BBBB")
            os.utime(f2, ns=(ts_ns, ts_ns))
            os.replace(f2, f1)
            h2 = file_sha256(f1)
            self.assertNotEqual(h1, h2)

            # 4) backup_steam_artwork preserves original backup on repeated runs
            grid = root / "grid"
            grid.mkdir()
            (grid / "123p.jpg").write_bytes(b"ORIGINAL")
            backup_steam_artwork(123, grid)
            (grid / "123p.jpg").write_bytes(b"BAD_ART")
            backup_steam_artwork(123, grid)
            self.assertEqual(
                (grid / ".deckinstall_art_bak" / "123" / "123p.jpg").read_bytes(),
                b"ORIGINAL",
            )

            # 5) ZIP backup/restore preserves distinct Wine profiles + ProgramData + manifest remapping
            steam_root = root / "Steam"
            compat_dir = steam_root / "steamapps" / "compatdata"
            old_id = 3111111111
            new_id = 3999999999
            u1 = compat_dir / str(old_id) / "pfx" / "drive_c" / "users" / "deck" / "Documents" / "s.dat"
            u2 = compat_dir / str(old_id) / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "s.dat"
            pd = compat_dir / str(old_id) / "pfx" / "drive_c" / "ProgramData" / "MyGame" / "cfg.ini"
            for p, data in ((u1, b"DECK_PROFILE"), (u2, b"STEAMUSER_PROFILE"), (pd, b"PD_CFG")):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)

            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=grid,
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat_dir,
            )
            old_entry = ShortcutEntry("0", old_id, "Cool Game", '"/g/a.exe"', '"/g"', "", "")
            new_entry = ShortcutEntry("0", new_id, "Cool Game", '"/Games/Cool Game/a.exe"', '"/Games/Cool Game"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[old_entry]):
                zpath, games, files = backup_all_saves(
                    paths, dest_root=root / "Backups", games_dir=root / "Games"
                )
                self.assertEqual((games, files), (1, 3))
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[new_entry]):
                rgames, rfiles = restore_saves_backup(paths, zpath)
                self.assertEqual((rgames, rfiles), (1, 3))
            self.assertEqual(
                (compat_dir / str(new_id) / "pfx" / "drive_c" / "users" / "deck" / "Documents" / "s.dat").read_bytes(),
                b"DECK_PROFILE",
            )
            self.assertEqual(
                (compat_dir / str(new_id) / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "s.dat").read_bytes(),
                b"STEAMUSER_PROFILE",
            )
            self.assertEqual(
                (compat_dir / str(new_id) / "pfx" / "drive_c" / "ProgramData" / "MyGame" / "cfg.ini").read_bytes(),
                b"PD_CFG",
            )

            # 6) sync_prefix_symlinks does not delete non-DeckInstall symlinks
            links_root = root / "Links"
            links_root.mkdir()
            manual_target = root / "OtherTarget"
            manual_target.mkdir()
            (links_root / "ManualLink").symlink_to(manual_target)
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[]):
                sync_prefix_symlinks(paths, links_root=links_root)
            self.assertTrue((links_root / "ManualLink").is_symlink())

            # 7) delete_cleanup_candidates_checked refuses prefix that gained unverified saves
            orphan_pfx = compat_dir / "2888888888"
            (orphan_pfx / "pfx" / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            cand = CleanupCandidate("Compatdata Órfão", "compatdata/2888888888", orphan_pfx, 0, False, False)
            (orphan_pfx / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "new.sav").write_bytes(b"DATA")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[]):
                deleted, _, errs = delete_cleanup_candidates_checked(paths, [cand])
            self.assertEqual(deleted, 0)
            self.assertTrue(orphan_pfx.exists())
            self.assertTrue(errs)

            # 8) update_shortcut_launch_options preserves AppID and metadata
            stored = {
                "shortcuts": {
                    "0": {
                        "appid": -123456,
                        "AppName": "Cool Game",
                        "Exe": '"/g/a.exe"',
                        "StartDir": '"/g"',
                        "LaunchOptions": "",
                        "IsHidden": 1,
                        "tags": {"0": "Deck"},
                    }
                }
            }
            paths.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
            paths.shortcuts_vdf.write_bytes(b"DUMMY")
            written: list[dict] = []
            with (
                mock.patch("deckinstall.steam_vdf.vdf.binary_loads", return_value=stored),
                mock.patch(
                    "deckinstall.steam_vdf.vdf.binary_dumps",
                    side_effect=lambda d: (written.append(d), b"OUT")[1],
                ),
            ):
                ok = update_shortcut_launch_options(paths, (-123456) & 0xFFFFFFFF, "MANGOHUD=1 %command%")
            self.assertTrue(ok)
            self.assertEqual(written[0]["shortcuts"]["0"]["IsHidden"], 1)
            self.assertEqual(written[0]["shortcuts"]["0"]["tags"], {"0": "Deck"})
            self.assertEqual(written[0]["shortcuts"]["0"]["LaunchOptions"], "MANGOHUD=1 %command%")

        # 9) i18n parity and zero unused keys
        import json
        import re

        en = json.loads(Path(__file__).resolve().parents[1].joinpath("src/deckinstall/locales/en.json").read_text())
        pt = json.loads(Path(__file__).resolve().parents[1].joinpath("src/deckinstall/locales/pt_BR.json").read_text())
        self.assertEqual(set(en.keys()), set(pt.keys()))
        pkg_dir = Path(__file__).resolve().parents[1].joinpath("src/deckinstall")
        used: set[str] = set()
        for py_file in pkg_dir.rglob("*.py"):
            used.update(re.findall(r'\btr\(\s*["\']([^"\']+)["\']', py_file.read_text()))
        self.assertEqual(sorted(used - set(en.keys())), [])
        self.assertEqual(sorted(set(en.keys()) - used), [])

    def test_v074_audit_regressions(self) -> None:
        from deckinstall.cleanup import delete_cleanup_candidates_checked
        from deckinstall.steam_paths import find_proton_binary
        from deckinstall.steam_vdf import (
            _is_prefix_referenced_by_shortcuts,
            _iter_prefix_save_pairs,
            add_or_update_shortcut,
            apply_launch_presets,
            backup_all_saves,
            restore_saves_backup,
            sync_prefix_symlinks,
        )

        # 1) Desktop included & distinct Wine user profiles preserved when >1 non-public user
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pfx = root / "pfx"
            dst_pfx = root / "dst_pfx"
            (pfx / "drive_c" / "users" / "steamuser" / "Desktop").mkdir(parents=True)
            (pfx / "drive_c" / "users" / "deck" / "Documents").mkdir(parents=True)
            src_desktop = pfx / "drive_c" / "users" / "steamuser" / "Desktop" / "save.dat"
            src_desktop.write_bytes(b"A")
            (pfx / "drive_c" / "users" / "deck" / "Documents" / "save.dat").write_bytes(b"B")
            pairs = list(_iter_prefix_save_pairs(
                pfx / "drive_c" / "users",
                dst_pfx / "drive_c" / "users",
                preserve_user_dirs=True,
            ))
            dsts = {str(dst.relative_to(dst_pfx / "drive_c" / "users")) for _, dst in pairs}
            self.assertIn("steamuser/Desktop/save.dat", dsts)
            self.assertIn("deck/Documents/save.dat", dsts)


        # 3) $HOME expansion in shortcut reference
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            compat = steam_root / "steamapps" / "compatdata"
            compat.mkdir(parents=True)
            grid = steam_root / "userdata" / "1" / "config" / "grid"

            # $HOME expansion in _is_prefix_referenced_by_shortcuts
            fake_home = root / "home"
            ext_pfx = fake_home / "Games" / "MyPrefix"
            (ext_pfx / "drive_c").mkdir(parents=True)
            entry = ShortcutEntry(
                index="0",
                appid_u32=1,
                app_name="Test",
                exe=f'"{ext_pfx}/drive_c/game.exe"',
                start_dir="$HOME/Games/MyPrefix",
                icon="",
                launch_options="",
            )
            with mock.patch("deckinstall.steam_vdf.Path.home", return_value=fake_home):
                self.assertTrue(_is_prefix_referenced_by_shortcuts(ext_pfx, [entry]))

            # 4) sync_prefix_symlinks preserves unmanaged symlinks
            links_root = root / "Links"
            links_root.mkdir()
            user_target = root / "UserCustomTarget"
            user_target.mkdir()
            (links_root / "My Custom Link").symlink_to(user_target)
            compat_entry = compat / "1"
            compat_entry.mkdir()
            paths_links = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=grid,
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat,
            )
            paths_links.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
            paths_links.shortcuts_vdf.write_bytes(b"VDF")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[entry]):
                sync_prefix_symlinks(paths_links, links_root=links_root)
            self.assertTrue((links_root / "My Custom Link").is_symlink())
            self.assertEqual((links_root / "My Custom Link").resolve(), user_target.resolve())
            self.assertTrue((links_root / "Test").is_symlink())

        # 5) apply_launch_presets preserves quoted operators and redirections
        out = apply_launch_presets(
            "FOO=\"a b\" ';' >\"/tmp/game log\" $USER_VAR %command% --windowed",
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertIn("';'", out)
        self.assertIn('>"/tmp/game log"', out)
        self.assertIn("$USER_VAR", out)
        self.assertIn("MANGOHUD=1", out)
        self.assertIn("gamemoderun", out)

        # 6) backup_all_saves creates unique zips & restore rejects escaping/orphan targets
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            compat = steam_root / "steamapps" / "compatdata"
            pfx_saves = compat / "999" / "pfx" / "drive_c" / "users" / "steamuser" / "Documents"
            pfx_saves.mkdir(parents=True)
            (pfx_saves / "s.sav").write_bytes(b"SAVE")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat,
            )
            paths.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
            paths.shortcuts_vdf.write_bytes(b"VDF")
            fake_entry = ShortcutEntry("0", 999, "Game999", '"/g/a.exe"', '"/g"', "", "")
            out_dir = root / "backups"
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[fake_entry]):
                z1, g1, f1 = backup_all_saves(paths, out_dir, games_dir=root / "Games")
                z2, g2, f2 = backup_all_saves(paths, out_dir, games_dir=root / "Games")
            self.assertNotEqual(z1, z2)
            self.assertEqual((g1, f1), (1, 1))
            self.assertEqual((g2, f2), (1, 1))

            # Malicious manifest escaping compatdata must fail
            bad_zip = root / "evil.zip"
            import json as _json
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr(
                    "manifest.json",
                    _json.dumps({"entries": [{"appid": "999", "prefix": str(root / "outside"), "files": ["999/a.sav"]}]}),
                )
                zf.writestr("999/a.sav", b"X")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[fake_entry]):
                restored_games, restored_files = restore_saves_backup(paths, bad_zip)
            self.assertEqual((restored_games, restored_files), (0, 0))
            self.assertFalse((root / "outside").exists())

        # 7) compatibilitytools.d discovery when no official Proton exists
        with tempfile.TemporaryDirectory() as tmp:
            steam_root = Path(tmp) / "Steam"
            ge_dir = steam_root / "compatibilitytools.d" / "GE-Proton9-20"
            ge_dir.mkdir(parents=True)
            (ge_dir / "proton").write_text("#!/bin/sh\n")
            (ge_dir / "compatibilitytool.vdf").write_text(
                '"compatibilitytools" { "compat_tools" { "GE-Proton9-20" { "install_path" "." } } }'
            )
            with mock.patch("deckinstall.steam_paths.Path.home", return_value=Path(tmp)):
                self.assertEqual(find_default_proton_name(steam_root), "GE-Proton9-20")
                self.assertEqual(find_proton_binary(steam_root), ge_dir / "proton")

        # 9) Rollback shortcuts.vdf if set_compat_tool fails + preserve metadata
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            s_vdf = steam_root / "userdata" / "1" / "config" / "shortcuts.vdf"
            c_vdf = steam_root / "config" / "config.vdf"
            s_vdf.parent.mkdir(parents=True, exist_ok=True)
            s_vdf.write_bytes(b"ORIGINAL_SHORTCUTS")
            paths_rb = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=s_vdf,
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=c_vdf,
                compatdata_dir=steam_root / "steamapps" / "compatdata",
            )
            stored = {
                "shortcuts": {
                    "0": {
                        "appid": 111,
                        "AppName": "OldName",
                        "Exe": '"/old/a.exe"',
                        "StartDir": '"/old"',
                        "IsHidden": 1,
                        "tags": {"0": "Fav"},
                    }
                }
            }
            written: list[dict] = []
            with (
                mock.patch("deckinstall.steam_vdf.vdf.binary_loads", return_value=stored),
                mock.patch(
                    "deckinstall.steam_vdf.vdf.binary_dumps",
                    side_effect=lambda d: (written.append(d), b"MODIFIED")[1],
                ),
                mock.patch("deckinstall.steam_vdf.set_compat_tool", side_effect=OSError("disk full")),
            ):
                with self.assertRaises(OSError):
                    add_or_update_shortcut(
                        paths=paths_rb,
                        app_name="NewName",
                        exe_path=root / "b.exe",
                        start_dir=root,
                        enable_proton=True,
                        preserve_from_appid=111,
                    )
            self.assertEqual(written[0]["shortcuts"]["0"]["IsHidden"], 1)
            self.assertEqual(written[0]["shortcuts"]["0"]["tags"], {"0": "Fav"})
            self.assertEqual(s_vdf.read_bytes(), b"ORIGINAL_SHORTCUTS")

        # 10) delete_cleanup_candidates_checked skips candidate if shortcut appears before deletion
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            compat = steam_root / "steamapps" / "compatdata"
            cand_pfx = compat / "555" / "pfx"
            (cand_pfx / "drive_c").mkdir(parents=True)
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=compat,
            )
            paths.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
            paths.shortcuts_vdf.write_bytes(b"VDF")
            from deckinstall.cleanup import CleanupCandidate
            cand = CleanupCandidate("orphan", "Orphan", cand_pfx.parent, 100, False)
            live_entry = ShortcutEntry("0", 555, "Orphan", '"/g/a.exe"', '"/g"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[live_entry]):
                deleted, freed, errs = delete_cleanup_candidates_checked(paths, [cand])
            self.assertEqual(deleted, 0)
            self.assertTrue(cand_pfx.parent.exists())
            self.assertTrue(errs)

    def test_real_vdf_roundtrip(self) -> None:
        """Verify shortcuts.vdf and config.vdf round-trip against the real vdf package."""
        import vdf as _real_vdf
        if getattr(_real_vdf, "__file__", None) is None:
            self.skipTest("real vdf package not installed")
        from deckinstall.steam_vdf import (
            add_or_update_shortcut,
            get_shortcut_proton,
            load_shortcuts,
            remove_shortcut,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            grid = steam_root / "userdata" / "1" / "config" / "grid"
            grid.mkdir(parents=True)
            config_vdf = steam_root / "config" / "config.vdf"
            config_vdf.parent.mkdir(parents=True)
            config_vdf.write_text('"InstallConfigStore"\n{\n}\n', encoding="utf-8")
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=grid,
                config_vdf=config_vdf,
                compatdata_dir=steam_root / "steamapps" / "compatdata",
            )
            exe = root / "Games" / "MyGame" / "game.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            with mock.patch("deckinstall.steam_vdf.find_default_proton_name", return_value="proton_experimental"):
                appid = add_or_update_shortcut(paths, "MyGame", exe, exe.parent, enable_proton=True)
            loaded = load_shortcuts(paths.shortcuts_vdf)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].appid_u32, appid)
            self.assertEqual(loaded[0].app_name, "MyGame")
            self.assertEqual(get_shortcut_proton(paths.config_vdf, appid), "proton_experimental")

            # Remove shortcut cleans shortcuts.vdf and CompatToolMapping
            self.assertTrue(remove_shortcut(paths, appid))
            self.assertEqual(load_shortcuts(paths.shortcuts_vdf), [])
            self.assertEqual(get_shortcut_proton(paths.config_vdf, appid), "")

    def test_v075_audit_regressions(self) -> None:
        """Cover v0.7.5 security, backend, frontend, i18n, and packaging regressions."""
        import tarfile

        from deckinstall.artwork import _is_valid_image_bytes, _validate_appid, fetch_header_preview
        from deckinstall.scanner import default_scan_roots
        from deckinstall.steam_paths import _proton_version_key
        from deckinstall.steam_vdf import (
            _iter_prefix_save_pairs,
            _steam_transaction_lock,
            apply_launch_presets,
        )

        # 1) Split archive detection & tar symlink stripping + flags
        self.assertTrue(is_archive(Path("game.part1.rar")))
        self.assertTrue(is_archive(Path("game.7z.001")))
        self.assertTrue(is_archive(Path("game.zip.001")))
        tar_cmd = _select_extract_cmd(Path("game.tar.gz"), Path("/tmp/out"))
        self.assertIn("--no-same-owner", tar_cmd)
        self.assertIn("--no-same-permissions", tar_cmd)
        sevenz_cmd = _select_extract_cmd(Path("game.7z"), Path("/tmp/out"))
        self.assertIn("-p-", sevenz_cmd)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tgz = root / "sym.tar.gz"
            with tarfile.open(tgz, "w:gz") as tf:
                ti = tarfile.TarInfo("Game/ok.txt")
                payload = b"hello"
                ti.size = len(payload)
                tf.addfile(ti, fileobj=tarfile.io.BytesIO(payload))
                sym = tarfile.TarInfo("Game/evil_link")
                sym.type = tarfile.SYMTYPE
                sym.linkname = "/etc/passwd"
                tf.addfile(sym)
            out = root / "out"
            extract_archive_to_dir(tgz, out)
            self.assertTrue((out / "Game" / "ok.txt").is_file())
            self.assertFalse((out / "Game" / "evil_link").exists() or (out / "Game" / "evil_link").is_symlink())

        # 2) /run/media steamapps skipped
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_mnt = root / "card1"
            (media_mnt / "steamapps").mkdir(parents=True)
            (media_mnt / "Games").mkdir(parents=True)
            mountinfo = f"40 30 179:1 / {media_mnt} rw - exfat /dev/mmcblk0p1 rw\n"
            with mock.patch("deckinstall.scanner._read_mountinfo", return_value=mountinfo):
                roots = default_scan_roots()
            self.assertIn(media_mnt / "Games", roots)
            self.assertNotIn(media_mnt / "steamapps", roots)

        # 3) Numeric Proton sorting (10.0 > 9.0)
        self.assertGreater(_proton_version_key(Path("Proton 10.0")), _proton_version_key(Path("Proton 9.0")))

        # 4) Artwork validation & fetch_header_preview size/type guards
        with self.assertRaises(ValueError):
            _validate_appid(0)
        self.assertTrue(_is_valid_image_bytes(b"\xff\xd8\xff\xe0" + b"0" * 600))
        self.assertTrue(_is_valid_image_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 600))
        self.assertFalse(_is_valid_image_bytes(b"<html>not image</html>"))
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {"Content-Type": "text/html"}
        fake_resp.iter_content.return_value = [b"<html>oops</html>"]
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: False
        with mock.patch("deckinstall.artwork.requests.get", return_value=fake_resp):
            self.assertIsNone(fetch_header_preview(12345))

        # 5) Launch options safety + empty preset + reentrant flock + Default user skip
        _preset_off = dict(gamemode=False, mangohud=False, wined3d=False, no_esync=False, no_fsync=False)
        with self.assertRaises(ValueError):
            apply_launch_presets("FOO=1\nrm -rf /", **_preset_off)
        quoted_cmd = apply_launch_presets("$(whoami)", **_preset_off)
        self.assertIn("'$(whoami)'", quoted_cmd)
        self.assertEqual(apply_launch_presets("", **_preset_off), "")
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(troot)}):
                with _steam_transaction_lock(troot / "shortcuts.vdf"):
                    locks = Path.home() / ".local" / "state" / "DeckInstall" / "locks"
                    before = set(locks.glob(".deckinstall.*.vdf.lock"))
                    self.assertTrue(before)
                    with (
                        mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(troot / "alternate")}),
                        _steam_transaction_lock(troot / "shortcuts.vdf"),
                    ):
                        self.assertEqual(set(locks.glob(".deckinstall.*.vdf.lock")), before)

            pfx = troot / "pfx"
            (pfx / "drive_c" / "users" / "Default" / "Documents").mkdir(parents=True)
            (pfx / "drive_c" / "users" / "Default" / "Documents" / "sys.dat").write_bytes(b"DEF")
            (pfx / "drive_c" / "users" / "All Users" / "Documents").mkdir(parents=True)
            (pfx / "drive_c" / "users" / "All Users" / "Documents" / "all.dat").write_bytes(b"ALL")
            (pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            (pfx / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat").write_bytes(b"SAV")
            pairs = list(_iter_prefix_save_pairs(pfx / "drive_c" / "users", troot / "dst_pfx" / "drive_c" / "users"))
            self.assertTrue(all("Default" not in str(s) and "All Users" not in str(s) for s, _ in pairs))
            self.assertTrue(any("steamuser" in str(s) for s, _ in pairs))


    def test_qt_ui_smoke(self) -> None:
        """Smoke test MainWindow, FlowLayout, and theme switching in offscreen Qt."""
        try:
            from PySide6.QtWidgets import QApplication, QPushButton, QWidget
        except ImportError:
            self.skipTest("PySide6 not installed in this environment")
        from deckinstall.theme import apply_theme
        from deckinstall.ui import FlowLayout, MainWindow

        app = QApplication.instance() or QApplication([])
        apply_theme(app, "dark")
        apply_theme(app, "light")
        host = QWidget()
        fl = FlowLayout(host)
        b1 = QPushButton("Visible")
        b2 = QPushButton("Hidden")
        b2.hide()
        fl.addWidget(b1)
        fl.addWidget(b2)
        self.assertGreater(fl.minimumSize().width(), 0)
        with (
            mock.patch("deckinstall.ui.resolve_steam_paths", return_value=None),
            mock.patch("deckinstall.ui.scan_all_games", return_value=[]),
        ):
            win = MainWindow(app)
            self.assertIn("DeckInstall", win.windowTitle())
            for w in list(win._active_workers):
                w.wait(2000)
            win.close()
            app.processEvents()
        # --smoke-test: every worker still runs (the AppImage check covers them), none walks the disks.
        from deckinstall import ui as ui_mod

        with (
            mock.patch("deckinstall.ui.resolve_steam_paths", return_value=None),
            mock.patch("deckinstall.ui.find_steam_root", return_value=None),
            mock.patch("deckinstall.ui.scan_external_launcher_games", return_value=[]),
            mock.patch("deckinstall.ui.list_standalone_games", return_value=[]) as standalone,
            mock.patch("deckinstall.ui.scan_all_games", return_value=[]) as scan,
            mock.patch("deckinstall.ui.discover_libraries", return_value=[]) as discover,
            mock.patch("deckinstall.ui.disks_without_library", return_value=[]) as disks,
        ):
            win = MainWindow(app, smoke=True)
            self.assertTrue(ui_mod._wait_for_workers(app, win, 10.0))
            scan.assert_called_once_with(roots=[])
            standalone.assert_called_once()
            self.assertIsNotNone(standalone.call_args.args[0])  # one games folder, not every disk
            discover.assert_called_with([])
            disks.assert_called_with([])
            self.assertTrue(win.close())
            app.processEvents()

    def test_main_smoke_flag_reaches_main_window(self) -> None:
        """main() --smoke-test builds the window in smoke mode and waits for its workers."""
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed in this environment")
        from deckinstall import ui as ui_mod

        app = QApplication.instance() or QApplication([])
        fake_win = mock.Mock(_active_workers=set())
        fake_win.close.return_value = True
        with (
            mock.patch.object(sys, "argv", ["deckinstall", "--smoke-test"]),
            mock.patch("deckinstall.ui.QApplication", return_value=app),
            mock.patch("deckinstall.ui.qInstallMessageHandler"),
            mock.patch("deckinstall.ui.install_touch_scroll"),
            mock.patch("deckinstall.ui.MainWindow", return_value=fake_win) as main_window,
        ):
            self.assertEqual(ui_mod.main(), 0)
        main_window.assert_called_once_with(app, smoke=True)
        fake_win.close.assert_called_once()
        fake_win.showMaximized.assert_not_called()

    def test_wait_for_workers_follows_chained_workers(self) -> None:
        """The smoke wait also covers a worker started by another worker's result."""
        try:
            from PySide6.QtCore import QThread
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed in this environment")
        from deckinstall import ui as ui_mod

        app = QApplication.instance() or QApplication([])
        class Sleeper(QThread):
            def __init__(self, seconds: float) -> None:
                super().__init__()
                self.seconds = seconds

            def run(self) -> None:
                time.sleep(self.seconds)

        win = mock.Mock(_active_workers=set())
        first, second = Sleeper(0.1), Sleeper(0.3)

        def _first_done() -> None:  # like a result handler that starts another worker
            win._active_workers.discard(first)
            win._active_workers.add(second)
            second.start()

        first.finished.connect(_first_done)
        second.finished.connect(lambda: win._active_workers.discard(second))
        win._active_workers.add(first)
        first.start()
        self.assertTrue(ui_mod._wait_for_workers(app, win, 5.0))
        self.assertTrue(second.isFinished())

    def test_v076_audit_regressions(self) -> None:
        """Regression tests for v0.7.6 multi-agent audit findings (#1-#16)."""
        from deckinstall import steam_vdf as sv
        from deckinstall.extractor import run_windows_installer

        with self.subTest("1_unconventional_saves_block_verified_cleanup"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                src = root / "src"
                dst = root / "dst"
                (src / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
                (src / "drive_c" / "users" / "steamuser" / "Documents" / "ok.sav").write_bytes(b"1")
                (dst / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
                (dst / "drive_c" / "users" / "steamuser" / "Documents" / "ok.sav").write_bytes(b"1")
                (src / "drive_c" / "MyGame" / "Saves").mkdir(parents=True)
                (src / "drive_c" / "MyGame" / "Saves" / "slot1.dat").write_bytes(b"SECRET")
                self.assertTrue(sv._prefix_has_unconventional_saves(src))  # used by the cleanup check

        with self.subTest("2_import_prefix_rejects_symlink_escape"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                outside = root / "outside"
                outside.mkdir()
                compat = root / "compatdata"
                (compat / "999").mkdir(parents=True)
                os.symlink(outside, compat / "999" / "pfx")
                src_pfx = root / "ext_pfx"
                (src_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
                (src_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.dat").write_bytes(b"X")
                paths = SteamPaths(
                    steam_root=root,
                    user_id="1",
                    userdata_dir=root / "userdata" / "1",
                    shortcuts_vdf=root / "shortcuts.vdf",
                    grid_dir=root / "grid",
                    config_vdf=root / "config.vdf",
                    compatdata_dir=compat,
                )
                with mock.patch("deckinstall.steam_vdf.find_external_prefixes", return_value=[src_pfx]):
                    with self.assertRaises(OSError):
                        import_external_prefix_saves(paths, 999, "TestGame")

        with self.subTest("4_backup_all_saves_enforces_entry_cap"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                compat = root / "compatdata"
                udir = compat / "100" / "pfx" / "drive_c" / "users" / "steamuser" / "Documents"
                udir.mkdir(parents=True)
                for i in range(5):
                    (udir / f"f{i}.sav").write_bytes(b"1")
                paths = SteamPaths(
                    steam_root=root,
                    user_id="1",
                    userdata_dir=root / "userdata" / "1",
                    shortcuts_vdf=root / "shortcuts.vdf",
                    grid_dir=root / "grid",
                    config_vdf=root / "config.vdf",
                    compatdata_dir=compat,
                )
                dummy_entry = ShortcutEntry("0", 100, "G", '"g.exe"', '"."', "", "")
                with (
                    mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[dummy_entry]),
                    mock.patch.object(sv, "_RESTORE_MAX_ENTRIES", 3),
                ):
                    with self.assertRaises(RuntimeError):
                        sv.backup_all_saves(paths, root / "backups")

        with self.subTest("5_6_7_add_or_update_preserves_meta_proton_and_unlinks_new_vdf"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config").mkdir()
                (root / "config" / "config.vdf").write_text("{}", encoding="utf-8")
                (root / "grid").mkdir()
                (root / "compatdata" / "555").mkdir(parents=True)
                (root / "compatdata" / "555" / "marker.txt").write_text("ok", encoding="utf-8")
                exe = root / "Games" / "MyGame" / "game.exe"
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"MZ")
                svdf = root / "shortcuts.vdf"
                paths = SteamPaths(
                    steam_root=root,
                    user_id="1",
                    userdata_dir=root / "userdata" / "1",
                    shortcuts_vdf=svdf,
                    grid_dir=root / "grid",
                    config_vdf=root / "config" / "config.vdf",
                    compatdata_dir=root / "compatdata",
                )
                # Rollback unlinks brand-new shortcuts.vdf on error
                with (
                    mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[]),
                    mock.patch("deckinstall.steam_vdf.vdf.binary_loads", return_value={"shortcuts": {}}),
                    mock.patch("deckinstall.steam_vdf.vdf.binary_dumps", return_value=b"VDF"),
                    mock.patch("deckinstall.steam_vdf.set_compat_tool", side_effect=OSError("fail")),
                ):
                    with self.assertRaises(OSError):
                        sv.add_or_update_shortcut(paths, "MyGame", exe, enable_proton=True)
                self.assertFalse(svdf.exists())

                # Preserves existing LaunchOptions/icon + Proton + migrates stale_appid compatdata
                svdf.write_bytes(b"EXISTING_VDF")
                existing = ShortcutEntry(
                    index="0",
                    appid_u32=555,
                    app_name="MyGame",
                    exe=f'"{exe}"',
                    start_dir=f'"{exe.parent}"',
                    icon="/tmp/custom.png",
                    launch_options="gamemoderun %command%",
                )
                dumped: list[dict] = []

                def _cap_dumps(data: dict) -> bytes:
                    dumped.append(data)
                    return b"VDF"

                with (
                    mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[existing]),
                    mock.patch(
                        "deckinstall.steam_vdf.vdf.binary_loads",
                        return_value={
                            "shortcuts": {
                                "0": {
                                    "appid": 555,
                                    "AppName": "MyGame",
                                    "icon": "/tmp/custom.png",
                                    "LaunchOptions": "gamemoderun %command%",
                                }
                            }
                        },
                    ),
                    mock.patch("deckinstall.steam_vdf.vdf.binary_dumps", side_effect=_cap_dumps),
                    mock.patch("deckinstall.steam_vdf.get_compat_tool_map", return_value={
                        sv.compute_shortcut_appid(f'"{exe}"', "MyGame")[0]: "Proton Experimental",
                    }),
                    mock.patch("deckinstall.steam_vdf.set_compat_tool") as m_set_compat,
                    mock.patch("deckinstall.steam_vdf.remove_compat_tool") as m_rm_compat,
                ):
                    new_id = sv.add_or_update_shortcut(paths, "MyGame", exe, enable_proton=True)
                self.assertTrue(dumped)
                rec = next(iter(dumped[-1]["shortcuts"].values()))
                self.assertEqual(rec["LaunchOptions"], "gamemoderun %command%")
                self.assertEqual(rec["icon"], "/tmp/custom.png")
                m_set_compat.assert_not_called()
                m_rm_compat.assert_called_once_with(paths.config_vdf, 555)
                self.assertTrue((paths.compatdata_dir / str(new_id) / "marker.txt").exists())

        with self.subTest("8_installer_prefix_preserved_and_adopted"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "Games" / "SetupGame"
                target.mkdir(parents=True)
                inst = root / "setup.exe"
                inst.write_bytes(b"MZ")
                proton_bin = root / "proton"
                proton_bin.write_text("#!/bin/sh\n", encoding="utf-8")

                def _fake_popen(*args, env=None, **kwargs):
                    pfx_dir = Path(env["STEAM_COMPAT_DATA_PATH"]) / "pfx" / "drive_c" / "users" / "steamuser"
                    pfx_dir.mkdir(parents=True, exist_ok=True)
                    (pfx_dir / "reg.dat").write_bytes(b"REG")
                    (target / "installed.exe").write_bytes(b"MZ")
                    proc = mock.MagicMock()
                    proc.stdout = iter([])
                    proc.wait.return_value = 0
                    proc.returncode = 0
                    return proc

                with (
                    mock.patch("deckinstall.extractor.find_steam_root", return_value=root),
                    mock.patch("deckinstall.extractor.find_proton_binary", return_value=proton_bin),
                    mock.patch("deckinstall.extractor.subprocess.Popen", side_effect=_fake_popen),
                    mock.patch("deckinstall.extractor._drain_stream_lines", return_value=0),
                ):
                    res = run_windows_installer(inst, target)
                self.assertEqual(res, target)
                self.assertTrue((target / ".deckinstall_installer_pfx" / "drive_c" / "users" / "steamuser" / "reg.dat").exists())

        with self.subTest("9_prefix_symlink_users_and_programdata_blocked"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                outside = root / "outside"
                outside.mkdir()
                pfx = root / "pfx"
                (pfx / "drive_c").mkdir(parents=True)
                os.symlink(outside, pfx / "drive_c" / "users")
                os.symlink(outside, pfx / "drive_c" / "ProgramData")
                self.assertIsNone(sv._prefix_users_dir(pfx))
                self.assertIsNone(sv._prefix_programdata_dir(pfx))
                self.assertEqual(list(sv._iter_prefix_save_pairs(pfx / "drive_c" / "users", root / "dst")), [])
                self.assertEqual(list(sv._iter_programdata_save_pairs(pfx / "drive_c" / "ProgramData", root / "dst2")), [])

        with self.subTest("10_appid_bounds_and_scan_all_games_fault_isolation"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                g_bad_id = root / "BadAppID"
                g_bad_id.mkdir()
                (g_bad_id / "steam_appid.txt").write_text("0\n", encoding="utf-8")
                self.assertIsNone(detect_steam_appid(g_bad_id))
                (g_bad_id / "steam_appid.txt").write_text("99999999999\n", encoding="utf-8")
                self.assertIsNone(detect_steam_appid(g_bad_id))
                g_good = root / "GoodGame"
                g_good.mkdir()
                (g_good / "game.exe").write_bytes(pe_exe_bytes())
                orig_scan = scan_game_folder

                def _flaky_scan(folder: Path):
                    if folder.name == "BadAppID":
                        raise PermissionError("denied")
                    return orig_scan(folder)

                with mock.patch("deckinstall.scanner.scan_game_folder", side_effect=_flaky_scan):
                    games = scan_all_games([root])
                self.assertEqual([g.name for g in games], ["GoodGame"])

        with self.subTest("11_14_16_ui_vdf_corrupt_and_ci_hardening"):
            wf = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-appimage.yml"
            wf_text = wf.read_text(encoding="utf-8")
            self.assertIn("timeout 30s", wf_text)
            self.assertIn("GITHUB_REF_NAME", wf_text)
            self.assertIn("format('v{0}', steps.meta.outputs.version)", wf_text)
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
            except ImportError:
                pass
            else:
                from deckinstall.ui import MainWindow

                app = QApplication.instance() or QApplication([])
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    paths = SteamPaths(
                        steam_root=root,
                        user_id="1",
                        userdata_dir=root / "userdata" / "1",
                        shortcuts_vdf=root / "shortcuts.vdf",
                        grid_dir=root / "grid",
                        config_vdf=root / "config.vdf",
                        compatdata_dir=root / "compatdata",
                    )
                    (root / "shortcuts.vdf").write_bytes(b"corrupt")
                    with (
                        mock.patch("deckinstall.ui.resolve_steam_paths", return_value=paths),
                        mock.patch("deckinstall.ui.scan_all_games", return_value=[]),
                        mock.patch("deckinstall.ui.load_shortcuts", side_effect=ValueError("bad vdf")),
                    ):
                        win = MainWindow(app)
                        for w in list(win._active_workers):
                            w.wait(2000)
                        app.processEvents()
                        self.assertTrue(win._vdf_corrupt)
                        self.assertFalse(win.vdf_error_banner.isHidden())
                        with mock.patch.object(QMessageBox, "critical") as crit:
                            win.btn_mode_steam.setChecked(True)
                            win._install_selected()  # Steam mode refuses while shortcuts.vdf is unreadable
                        crit.assert_called_once()
                        self.assertTrue(win.btn_reload.isEnabled())
                        win.steam_search_input.setText("0")
                        win._search_steam_art()
                        self.assertEqual(win.steam_match_combo.count(), 0)
                        for w in list(win._active_workers):
                            w.wait(2000)
                        win.close()
                        app.processEvents()

    def test_system_and_steam_cleanup_discovery(self) -> None:
        """Verify Steam cleanup covers game data and preserves system files and Steam-owned runtimes."""
        from deckinstall.cleanup import delete_cleanup_candidates_checked, find_cleanup_candidates

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            steam = root / "Steam"
            (steam / "userdata" / "1" / "config").mkdir(parents=True)
            (steam / "userdata" / "1" / "config" / "shortcuts.vdf").write_bytes(b"VDF")
            (steam / "config").mkdir(parents=True)
            (steam / "config" / "config.vdf").write_text("{}", encoding="utf-8")
            (steam / "steamapps" / "shadercache" / "99999").mkdir(parents=True)
            (steam / "steamapps" / "shadercache" / "99999" / "shader.bin").write_bytes(b"S" * 1024)
            (steam / "steamapps" / "shadercache" / "0").mkdir(parents=True)
            (steam / "steamapps" / "shadercache" / "0" / "sys.bin").write_bytes(b"X" * 1024)
            (steam / "logs").mkdir(parents=True)
            (steam / "logs" / "content_log.txt").write_bytes(b"L" * (1024 * 1024 + 10))
            (home / ".local" / "share" / "Trash" / "files").mkdir(parents=True)
            (home / ".local" / "share" / "Trash" / "files" / "old.zip").write_bytes(b"T" * 1024)
            (home / ".cache" / "mesa_shader_cache").mkdir(parents=True)
            (home / ".cache" / "mesa_shader_cache" / "c.bin").write_bytes(b"C" * (5 * 1024 * 1024 + 10))
            (home / ".var" / "app" / "com.google.Chrome" / "cache").mkdir(parents=True)
            (home / ".var" / "app" / "com.google.Chrome" / "cache" / "c.bin").write_bytes(b"F" * (5 * 1024 * 1024 + 10))
            (steam / "compatibilitytools.d" / "UMU-Proton-10.0-4").mkdir(parents=True)
            (steam / "compatibilitytools.d" / "UMU-Proton-10.0-4" / "proton").write_bytes(b"U" * 1024)
            (steam / "steamapps" / "common" / "Proton 11.0").mkdir(parents=True)
            (steam / "steamapps" / "common" / "Proton 10.0").mkdir(parents=True)
            (steam / "steamapps" / "common" / "Proton 10.0" / "p").write_bytes(b"P" * 1024)
            (steam / "steamapps" / "common" / "SteamLinuxRuntime_4").mkdir(parents=True)
            (steam / "steamapps" / "common" / "SteamLinuxRuntime_sniper").mkdir(parents=True)
            (steam / "steamapps" / "common" / "SteamLinuxRuntime_sniper" / "r").write_bytes(b"R" * 1024)

            paths = SteamPaths(
                steam_root=steam,
                user_id="1",
                userdata_dir=steam / "userdata" / "1",
                shortcuts_vdf=steam / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=steam / "userdata" / "1" / "config" / "grid",
                config_vdf=steam / "config" / "config.vdf",
                compatdata_dir=steam / "steamapps" / "compatdata",
            )
            with (
                mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[]),
                mock.patch("pathlib.Path.home", return_value=home),
            ):
                cands = find_cleanup_candidates(
                    paths, extra_external_roots=[], include_system_caches=True, home_dir=home
                )
            labels = {c.label for c in cands}
            self.assertIn("shadercache/99999", labels)
            self.assertNotIn("shadercache/0", labels)
            self.assertIn("Steam/logs", labels)
            self.assertNotIn("Trash/files", labels)
            self.assertNotIn(".cache/mesa_shader_cache", labels)
            self.assertNotIn("Flatpak/com.google.Chrome/cache", labels)
            self.assertNotIn("compatibilitytools.d/UMU-Proton-10.0-4", labels)
            self.assertNotIn("common/Proton 10.0", labels)
            self.assertNotIn("common/SteamLinuxRuntime_sniper", labels)
            with (
                mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[]),
                mock.patch("pathlib.Path.home", return_value=home),
            ):
                deleted, _, errs = delete_cleanup_candidates_checked(
                    paths, cands, extra_external_roots=[], include_system_caches=True, home_dir=home
                )
            self.assertEqual(errs, [])
            self.assertEqual(deleted, len(cands))
            self.assertTrue((steam / "steamapps" / "common" / "Proton 11.0").is_dir())

    def test_bulk_proton_migration_and_orphan_mapping_cleanup(self) -> None:
        """Verify list_compat_tool_usage, bulk_migrate_compat_tool, and remove_compat_tools."""
        from deckinstall.steam_vdf import (
            bulk_migrate_compat_tool,
            list_compat_tool_usage,
            remove_compat_tools,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam = root / "Steam"
            (steam / "userdata" / "1" / "config").mkdir(parents=True)
            (steam / "userdata" / "1" / "config" / "shortcuts.vdf").write_bytes(b"VDF")
            (steam / "steamapps").mkdir(parents=True)
            (steam / "config").mkdir(parents=True)
            cfg = steam / "config" / "config.vdf"
            cfg_data = {
                "InstallConfigStore": {
                    "Software": {
                        "Valve": {
                            "Steam": {
                                "CompatToolMapping": {
                                    "1001": {"name": "GE-Proton10-34", "config": "", "priority": "250"},
                                    "2002": {"name": "GE-Proton10-34", "config": "", "priority": "250"},
                                }
                            }
                        }
                    }
                }
            }
            paths = SteamPaths(
                steam_root=steam,
                user_id="1",
                userdata_dir=steam / "userdata" / "1",
                shortcuts_vdf=steam / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=steam / "userdata" / "1" / "config" / "grid",
                config_vdf=cfg,
                compatdata_dir=steam / "steamapps" / "compatdata",
            )
            cfg.write_text("{}", encoding="utf-8")
            sc = ShortcutEntry("0", 1001, "ActiveGame", '"g.exe"', '"."', "", "")
            with (
                mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[sc]),
                mock.patch("deckinstall.steam_vdf.vdf.loads", return_value=cfg_data),
                mock.patch("deckinstall.steam_vdf.vdf.dumps", return_value="{}"),
            ):
                usage = list_compat_tool_usage(paths)
                self.assertEqual([(u.appid_u32, u.source, u.name, u.tool_name) for u in usage], [
                    (1001, "Non-Steam", "ActiveGame", "GE-Proton10-34"),
                    (2002, "Orphan", "AppID 2002", "GE-Proton10-34"),
                ])
                migrated = bulk_migrate_compat_tool(cfg, [1001], "proton_11")
                self.assertEqual(migrated, 1)
                self.assertEqual(
                    cfg_data["InstallConfigStore"]["Software"]["Valve"]["Steam"]["CompatToolMapping"]["1001"]["name"],
                    "proton_11",
                )
                removed = remove_compat_tools(cfg, [2002])
                self.assertEqual(removed, 1)
                self.assertNotIn(
                    "2002",
                    cfg_data["InstallConfigStore"]["Software"]["Valve"]["Steam"]["CompatToolMapping"],
                )


if __name__ == "__main__":
    unittest.main()
