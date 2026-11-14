"""Core unit tests for DeckInstall."""

from __future__ import annotations

import os
import sys
import tempfile
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

from deckinstall.extractor import (  # noqa: E402
    _select_extract_cmd,
    extract_archive,
    is_archive,
    sanitize_folder_name,
    suggest_target_name,
)
from deckinstall.scanner import (  # noqa: E402
    clean_game_name,
    detect_steam_appid,
    rank_executables,
    scan_all_games,
    scan_game_folder,
)
from deckinstall.steam_paths import SteamPaths, find_default_proton_name, list_steam_users  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    ShortcutEntry,
    _backup_file,
    compute_shortcut_appid,
    delete_cleanup_candidates,
    find_cleanup_candidates,
    import_external_prefix_saves,
    import_hydra_umu_saves,
    infer_game_root_from_exe,
    migrate_shortcut_to_games,
    plan_bulk_migration,
)


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
            (game_dir / "Launcher.exe").write_bytes(b"0" * 100)
            shipping = bin_dir / "SWGR-Win64-Shipping.exe"
            shipping.write_bytes(b"0" * 200)
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
            (locked / "Secret.exe").write_bytes(b"123")
            valid = root / "ValidGame"
            valid.mkdir()
            (valid / "Game.exe").write_bytes(b"12345")
            broken_link = valid / "Broken.exe"
            broken_link.symlink_to(root / "does_not_exist.exe")

            os.chmod(locked, 0o000)
            try:
                games = scan_all_games([root, root / "missing_root"])
                self.assertEqual(len(games), 1)
                self.assertEqual(games[0].name, "ValidGame")
                exes = rank_executables(valid)
                self.assertEqual(exes, [valid / "Game.exe"])
            finally:
                os.chmod(locked, 0o755)

    def test_extract_archive_unwrap_single_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zpath = root / "Test.Game-RUNE.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Test.Game-RUNE/Game.exe", b"MZ")
                zf.writestr("Test.Game-RUNE/data/config.ini", b"ok=1")
            dest_root = root / "Games"
            out = extract_archive(zpath, dest_root, "Test Game")
            self.assertEqual(out, dest_root / "Test Game")
            self.assertTrue((out / "Game.exe").is_file())
            self.assertTrue((out / "data" / "config.ini").is_file())

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
            shortcuts_vdf = steam_root / "userdata" / "123" / "config" / "shortcuts.vdf"
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
            self.assertEqual(plan[0].new_name, "Stellar Blade")
            self.assertTrue(plan[0].has_compatdata)

            paths = SteamPaths(
                steam_root=steam_root,
                user_id="123",
                userdata_dir=steam_root / "userdata" / "123",
                shortcuts_vdf=shortcuts_vdf,
                grid_dir=grid_dir,
                config_vdf=config_vdf,
                compatdata_dir=compat_dir,
            )
            with mock.patch("deckinstall.steam_vdf.infer_game_root_from_exe", return_value=src_game):
                new_appid, new_exe, steam_id = migrate_shortcut_to_games(
                    paths, entry, games_root, "Stellar Blade"
                )
            self.assertEqual(steam_id, 3489700)
            self.assertEqual(new_exe, games_root / "Stellar Blade" / "SB.exe")
            self.assertTrue(new_exe.is_file())
            self.assertTrue((compat_dir / str(new_appid) / "pfx" / "save.dat").is_file())

    def test_sanitize_folder_name_and_tar_priority(self) -> None:
        self.assertEqual(sanitize_folder_name("My Game"), "My Game")
        with self.assertRaises(ValueError):
            sanitize_folder_name("../escape")
        with self.assertRaises(ValueError):
            sanitize_folder_name("/abs/path")
        cmd = _select_extract_cmd(Path("game.tar.gz"), Path("/tmp/out"))
        self.assertEqual(cmd[0], "tar")

    def test_extract_archive_merges_existing_directory_without_wiping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest_root = root / "Games"
            existing = dest_root / "PatchGame" / "data"
            existing.mkdir(parents=True)
            (existing / "keep.txt").write_text("original", encoding="utf-8")
            (existing / "config.ini").write_text("old", encoding="utf-8")

            zpath = root / "patch.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("PatchGame/data/config.ini", "new")

            out = extract_archive(zpath, dest_root, "PatchGame")
            self.assertEqual((out / "data" / "keep.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual((out / "data" / "config.ini").read_text(encoding="utf-8"), "new")

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

    def test_import_hydra_umu_saves_merges_only_newer_and_skips_junk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hydra_root = root / "hydra" / "wine-prefixes"
            src_users = hydra_root / "4423270" / "drive_c" / "users"
            (src_users / "steamuser" / "AppData" / "LocalLow" / "Game").mkdir(parents=True)
            (src_users / "steamuser" / "AppData" / "Local" / "Temp").mkdir(parents=True)
            (src_users / "Public" / "Documents" / "Steam" / "RUNE").mkdir(parents=True)

            save_src = src_users / "steamuser" / "AppData" / "LocalLow" / "Game" / "save.dat"
            save_src.write_bytes(b"hydra-save")
            rune_src = src_users / "Public" / "Documents" / "Steam" / "RUNE" / "stats.bin"
            rune_src.write_bytes(b"rune")
            junk_src = src_users / "steamuser" / "AppData" / "Local" / "Temp" / "junk.tmp"
            junk_src.write_bytes(b"junk")

            compat_dir = root / "Steam" / "steamapps" / "compatdata"
            dst_existing = (
                compat_dir / "3387715067" / "pfx" / "drive_c" / "users" / "Public" / "Documents" / "Steam" / "RUNE" / "stats.bin"
            )
            dst_existing.parent.mkdir(parents=True)
            dst_existing.write_bytes(b"newer-steam-save")
            os.utime(save_src, (1000, 1000))
            os.utime(rune_src, (1000, 1000))
            os.utime(dst_existing, (2000, 2000))

            paths = SteamPaths(
                steam_root=root / "Steam",
                user_id="1",
                userdata_dir=root / "Steam" / "userdata" / "1",
                shortcuts_vdf=root / "shortcuts.vdf",
                grid_dir=root / "grid",
                config_vdf=root / "config.vdf",
                compatdata_dir=compat_dir,
            )
            copied = import_hydra_umu_saves(paths, 3387715067, 4423270, hydra_prefixes_root=hydra_root)
            self.assertEqual(copied, 1)
            self.assertEqual(
                (compat_dir / "3387715067" / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow" / "Game" / "save.dat").read_bytes(),
                b"hydra-save",
            )
            self.assertEqual(dst_existing.read_bytes(), b"newer-steam-save")
            self.assertFalse(
                (compat_dir / "3387715067" / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Temp" / "junk.tmp").exists()
            )

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
            self.assertEqual(plan[0].new_name, "My Indie Game")

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

            deleted, freed = delete_cleanup_candidates([c for c in cands if c.path in (orphan, bak)])
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

            # 2) rollback if add_or_update_shortcut fails after moving game dir
            dl_game = root / "Downloads" / "RollbackGame"
            dl_game.mkdir(parents=True)
            exe = dl_game / "Game.exe"
            exe.write_bytes(b"MZ")
            games_root = root / "Games"
            paths = SteamPaths(
                steam_root=root / "Steam",
                user_id="1",
                userdata_dir=root / "Steam" / "userdata" / "1",
                shortcuts_vdf=root / "Steam" / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=root / "Steam" / "userdata" / "1" / "config" / "grid",
                config_vdf=root / "Steam" / "config" / "config.vdf",
                compatdata_dir=root / "Steam" / "steamapps" / "compatdata",
            )
            entry = ShortcutEntry("0", 3000000002, "RollbackGame", f'"{exe}"', f'"{dl_game}"', "", "")
            with mock.patch("deckinstall.steam_vdf.add_or_update_shortcut", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    migrate_shortcut_to_games(paths, entry, games_root, "RollbackGame")
            self.assertTrue(dl_game.is_dir())
            self.assertFalse((games_root / "RollbackGame").exists())

            # 3) multi-user compatdata protection + backup has_saves detection
            (root / "Steam" / "userdata" / "1").mkdir(parents=True, exist_ok=True)
            (root / "Steam" / "userdata" / "2" / "config").mkdir(parents=True, exist_ok=True)
            other_compat = paths.compatdata_dir / "3555555555"
            (other_compat / "pfx" / "drive_c" / "users").mkdir(parents=True)
            bak_with_save = paths.compatdata_dir / "3000000002.20261114.deckinstall.bak"
            (bak_with_save / "pfx" / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
            (bak_with_save / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "s.dat").write_bytes(b"save")

            other_entry = ShortcutEntry("0", 3555555555, "OtherUserGame", '"/g/o.exe"', '"/g"', "", "")
            with mock.patch("deckinstall.steam_vdf.load_shortcuts", side_effect=[[], [other_entry]]):
                cands = find_cleanup_candidates(paths, extra_external_roots=[])
            self.assertNotIn(other_compat, {c.path for c in cands})
            bak_cand = next(c for c in cands if c.path == bak_with_save)
            self.assertTrue(bak_cand.has_saves)


if __name__ == "__main__":
    unittest.main()
