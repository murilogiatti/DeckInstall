"""Unit tests for maintenance, cleanup, save backup, and desktop disambiguation."""

from __future__ import annotations

import json
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
    fake_vdf.loads = lambda s: {
        "InstallConfigStore": {"Software": {"Valve": {"Steam": {"CompatToolMapping": {}}}}}
    }
    fake_vdf.dumps = lambda d, pretty=True: "{}"
    sys.modules["vdf"] = fake_vdf

from deckinstall import runtime_manager, standalone, steam_vdf  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    backup_all_saves,
    delete_cleanup_candidates_checked,
    find_cleanup_candidates,
)


def _write_standalone_manifest(
    games_dir: Path,
    folder_name: str,
    prefix_dir: Path,
    proton_name: str = "",
) -> Path:
    """Create a minimal standalone game directory and manifest for testing."""
    game_dir = games_dir / folder_name
    game_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": folder_name,
        "game_dir": str(game_dir),
        "exe_path": str(game_dir / "game.exe"),
        "prefix_dir": str(prefix_dir),
        "proton_name": proton_name,
        "steam_appid": None,
        "winedlloverrides": "",
        "launch_options": "",
        "launch_script": str(game_dir / "launch.sh"),
        "desktop_file": str(game_dir / f"{folder_name}.desktop"),
    }
    manifest_path = game_dir / ".deckinstall_standalone.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return game_dir


class MaintenanceTests(unittest.TestCase):
    """Offline tests for maintenance cleanup, Proton/runtime detection, backups, and .desktop slugs."""

    def test_find_and_delete_cleanup_without_steam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            apps_dir = home / ".local" / "share" / "applications"
            apps_dir.mkdir(parents=True)

            orphan_desktop = apps_dir / "deckinstall-orphan-game.desktop"
            orphan_desktop.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Orphan Game\n"
                f"Exec={home / 'Games' / 'Missing' / 'launch.sh'}\n"
                f"Path={home / 'Games' / 'Missing'}\n",
                encoding="utf-8",
            )

            games_dir = home / "Games"
            prefixes_dir = games_dir / ".prefixes"
            active_prefix = prefixes_dir / "ActiveGame"
            (active_prefix / "drive_c" / "users").mkdir(parents=True)
            (active_prefix / "drive_c" / "users" / "marker.txt").write_text("active", encoding="utf-8")
            _write_standalone_manifest(games_dir, "ActiveGame", active_prefix)

            orphan_prefix = prefixes_dir / "OrphanGame"
            (orphan_prefix / "drive_c" / "users").mkdir(parents=True)
            (orphan_prefix / "drive_c" / "users" / "junk.tmp").write_bytes(b"x" * 64)

            mesa_cache = home / ".cache" / "mesa_shader_cache"
            mesa_cache.mkdir(parents=True)
            (mesa_cache / "shader.bin").write_bytes(b"s" * 128)

            nv_cache = home / ".nv" / "ComputeCache"
            nv_cache.mkdir(parents=True)
            (nv_cache / "compute.bin").write_bytes(b"n" * 128)

            backups_dir = home / "Backups" / "DeckInstall"
            backups_dir.mkdir(parents=True)
            zip_paths: list[Path] = []
            for idx in range(5):
                zpath = backups_dir / f"saves-2026010{idx + 1}-120000.zip"
                zpath.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
                mtime = 1_700_000_000 + idx * 100
                os.utime(zpath, (mtime, mtime))
                zip_paths.append(zpath)

            with (
                mock.patch.object(steam_vdf, "external_prefix_roots", return_value=[]),
                mock.patch.object(runtime_manager, "list_ge_proton11_dirs", return_value=[]),
            ):
                candidates = find_cleanup_candidates(
                    None,
                    include_system_caches=True,
                    home_dir=home,
                )
                found_paths = {c.path.resolve() for c in candidates}

                self.assertIn(orphan_desktop.resolve(), found_paths)
                self.assertIn(orphan_prefix.resolve(), found_paths)
                self.assertNotIn(active_prefix.resolve(), found_paths)
                self.assertIn(mesa_cache.resolve(), found_paths)
                self.assertIn(nv_cache.resolve(), found_paths)

                # 2 oldest zips flagged, 3 newest kept
                self.assertIn(zip_paths[0].resolve(), found_paths)
                self.assertIn(zip_paths[1].resolve(), found_paths)
                for newest in zip_paths[2:]:
                    self.assertNotIn(newest.resolve(), found_paths)

                deleted, freed, errs = delete_cleanup_candidates_checked(
                    None,
                    candidates,
                    include_system_caches=True,
                    home_dir=home,
                )

            self.assertEqual(errs, [])
            self.assertEqual(deleted, 6)
            self.assertGreater(freed, 0)
            self.assertFalse(orphan_desktop.exists())
            self.assertFalse(orphan_prefix.exists())
            self.assertFalse(mesa_cache.exists())
            self.assertFalse(nv_cache.exists())
            self.assertFalse(zip_paths[0].exists())
            self.assertFalse(zip_paths[1].exists())
            self.assertTrue(active_prefix.is_dir())
            for newest in zip_paths[2:]:
                self.assertTrue(newest.is_file())

    def test_ge_proton11_cleanup_preserves_newest_and_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            compat_tools = home / ".local" / "share" / "Steam" / "compatibilitytools.d"
            ge11_10 = compat_tools / "GE-Proton11-10"
            ge11_12 = compat_tools / "GE-Proton11-12"
            ge10_34 = compat_tools / "GE-Proton10-34"
            for d in (ge11_10, ge11_12, ge10_34):
                d.mkdir(parents=True)
                (d / "proton").write_bytes(b"proton-bin")

            games_dir = home / "Games"
            prefixes_dir = games_dir / ".prefixes"

            with (
                mock.patch.object(steam_vdf, "external_prefix_roots", return_value=[]),
                mock.patch.object(runtime_manager, "list_ge_proton11_dirs", return_value=[]),
            ):
                # Without active standalone games: GE-Proton11-12 preserved, 11-10 and 10-34 flagged
                cands = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                cand_paths = {c.path.resolve() for c in cands}
                self.assertNotIn(ge11_12.resolve(), cand_paths)
                self.assertIn(ge11_10.resolve(), cand_paths)
                self.assertIn(ge10_34.resolve(), cand_paths)

                # Standalone game using GE-Proton10-34 -> 10-34 preserved, 11-12 still preserved
                _write_standalone_manifest(
                    games_dir,
                    "LegacyGame",
                    prefixes_dir / "LegacyGame",
                    proton_name="GE-Proton10-34",
                )
                cands_with_10 = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                cand_paths_with_10 = {c.path.resolve() for c in cands_with_10}
                self.assertNotIn(ge11_12.resolve(), cand_paths_with_10)
                self.assertNotIn(ge10_34.resolve(), cand_paths_with_10)
                self.assertIn(ge11_10.resolve(), cand_paths_with_10)

                # Standalone game using GE-Proton11-10 -> 11-10 preserved too
                _write_standalone_manifest(
                    games_dir,
                    "MidGame",
                    prefixes_dir / "MidGame",
                    proton_name="GE-Proton11-10",
                )
                cands_with_11_10 = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                cand_paths_with_11_10 = {c.path.resolve() for c in cands_with_11_10}
                self.assertNotIn(ge11_12.resolve(), cand_paths_with_11_10)
                self.assertNotIn(ge11_10.resolve(), cand_paths_with_11_10)

    def test_obsolete_steamrt3_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            umu_dir = home / ".local" / "share" / "umu"
            steamrt3 = umu_dir / "steamrt3"
            steamrt3.mkdir(parents=True)
            (steamrt3 / "toolmanifest.vdf").write_text('"manifest" {}', encoding="utf-8")

            with (
                mock.patch.object(steam_vdf, "external_prefix_roots", return_value=[]),
                mock.patch.object(runtime_manager, "list_ge_proton11_dirs", return_value=[]),
            ):
                # Missing steamrt4/toolmanifest.vdf -> steamrt3 NOT flagged
                cands_no_rt4 = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                self.assertNotIn(steamrt3.resolve(), {c.path.resolve() for c in cands_no_rt4})

                # With steamrt4/toolmanifest.vdf and no Proton <= 10 tool -> steamrt3 IS flagged
                steamrt4 = umu_dir / "steamrt4"
                steamrt4.mkdir(parents=True)
                (steamrt4 / "toolmanifest.vdf").write_text('"manifest" {}', encoding="utf-8")
                cands_rt4 = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                self.assertIn(steamrt3.resolve(), {c.path.resolve() for c in cands_rt4})

                # Standalone game using GE-Proton10-34 -> steamrt3 NOT flagged
                games_dir = home / "Games"
                _write_standalone_manifest(
                    games_dir,
                    "OldProtonGame",
                    games_dir / ".prefixes" / "OldProtonGame",
                    proton_name="GE-Proton10-34",
                )
                cands_old_proton = find_cleanup_candidates(
                    None, include_system_caches=True, home_dir=home
                )
                self.assertNotIn(steamrt3.resolve(), {c.path.resolve() for c in cands_old_proton})

    def test_backup_all_saves_standalone_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            prefix_dir = games_dir / ".prefixes" / "HollowSave"
            save_file = (
                prefix_dir / "drive_c" / "users" / "steamuser" / "Documents" / "My Game" / "save1.sav"
            )
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"SAVE-DATA-12345")
            _write_standalone_manifest(games_dir, "HollowSave", prefix_dir, proton_name="GE-Proton11-12")

            dest_root = root / "Backups"
            zip_path, games_backed, files_backed = backup_all_saves(
                None,
                dest_root=dest_root,
                games_dir=games_dir,
            )

            self.assertTrue(zip_path.is_file())
            self.assertEqual(games_backed, 1)
            self.assertEqual(files_backed, 1)
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                self.assertIn("manifest.json", names)
                self.assertTrue(any(n.endswith("/save1.sav") for n in names))
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertIn("standalone-HollowSave", manifest)

    def test_desktop_slug_collision_disambiguation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            prefixes_dir = games_dir / ".prefixes"
            apps_dir = root / "applications"

            src_one = root / "src" / "GameOne"
            src_one.mkdir(parents=True)
            exe_one = src_one / "game1.exe"
            exe_one.write_bytes(b"MZ")

            src_two = root / "src" / "GameTwo"
            src_two.mkdir(parents=True)
            exe_two = src_two / "game2.exe"
            exe_two.write_bytes(b"MZ")

            with (
                mock.patch.object(standalone, "GAMES_DIR", games_dir),
                mock.patch.object(standalone, "PREFIXES_DIR", prefixes_dir),
                mock.patch.object(standalone, "APPLICATIONS_DIR", apps_dir),
            ):
                installed_one = standalone.install_standalone_game(
                    game_name="My Game!",
                    exe_path=exe_one,
                    game_dir=src_one,
                )
                installed_two = standalone.install_standalone_game(
                    game_name="My Game?",
                    exe_path=exe_two,
                    game_dir=src_two,
                )

            self.assertNotEqual(installed_one.game_dir, installed_two.game_dir)
            self.assertNotEqual(installed_one.desktop_file, installed_two.desktop_file)
            self.assertTrue(installed_one.desktop_file.is_file())
            self.assertTrue(installed_two.desktop_file.is_file())
            desktop_files = sorted(apps_dir.glob("*.desktop"))
            self.assertEqual(len(desktop_files), 2)

    def test_dlss_upgrade_preset_and_mangohud_topbar(self) -> None:
        opts = steam_vdf.apply_launch_presets(
            "",
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            dlss_upgrade=True,
        )
        self.assertIn("PROTON_DLSS_UPGRADE=1", opts)
        self.assertIn("MANGOHUD=1", opts)
        self.assertIn("gamemoderun", opts)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "MangoHud" / "MangoHud.conf"
            cfg.parent.mkdir(parents=True)
            cfg.write_text("fps_limit=60\n", encoding="utf-8")
            written = steam_vdf.install_mangohud_topbar_config(cfg)
            self.assertEqual(written, cfg)
            self.assertTrue(cfg.with_name("MangoHud.conf.deckinstall.bak").is_file())
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("horizontal", text)
            self.assertIn("hud_compact", text)
            self.assertIn("position=top-left", text)

    def test_format_size_bytes_mb_and_gb(self) -> None:
        try:
            from deckinstall.ui import format_size_bytes
        except Exception:
            self.skipTest("PySide6 not installed in this environment")
        self.assertEqual(format_size_bytes(500 * 1024 * 1024), "500.0 MB")
        self.assertEqual(format_size_bytes(1024 * 1024 * 1024), "1.00 GB")
        self.assertEqual(format_size_bytes(int(1.5 * 1024 * 1024 * 1024)), "1.50 GB")


if __name__ == "__main__":
    unittest.main()
