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

from deckinstall import cleanup, runtime_manager, standalone, steam_vdf  # noqa: E402
from deckinstall.cleanup import delete_cleanup_candidates_checked, find_cleanup_candidates  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    backup_all_saves,
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
                mock.patch.object(cleanup, "external_prefix_roots", return_value=[]),
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
                self.assertNotIn(mesa_cache.resolve(), found_paths)
                self.assertNotIn(nv_cache.resolve(), found_paths)
                self.assertTrue(all(zip_path.resolve() not in found_paths for zip_path in zip_paths))

                deleted, freed, errs = delete_cleanup_candidates_checked(
                    None,
                    candidates,
                    include_system_caches=True,
                    home_dir=home,
                )

            self.assertEqual(errs, [])
            self.assertEqual(deleted, 2)
            self.assertGreater(freed, 0)
            self.assertFalse(orphan_desktop.exists())
            self.assertFalse(orphan_prefix.exists())
            self.assertTrue(mesa_cache.exists())
            self.assertTrue(nv_cache.exists())
            self.assertTrue(active_prefix.is_dir())
            self.assertTrue(all(zip_path.is_file() for zip_path in zip_paths))

    def test_cleanup_protects_a_pinned_game_version(self) -> None:
        from deckinstall import steam_paths

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            compat_tools = runtime_manager.get_compat_tools_dir()
            older, current, newest = [compat_tools / f"GE-Proton11-{n}" for n in (1, 2, 3)]
            for tool in (older, current, newest):
                tool.mkdir(parents=True)
                (tool / "proton").write_bytes(b"proton-bin")
                (tool / ".deckinstall-managed").write_text(tool.name)

            with (
                mock.patch.object(cleanup, "external_prefix_roots", return_value=[]),
                mock.patch.object(steam_paths, "find_steam_root", return_value=None),
            ):
                candidates = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                self.assertEqual({c.path for c in candidates}, {older, current})
                _write_standalone_manifest(
                    home / "Games",
                    "PinnedGame",
                    home / "Games" / ".prefixes" / "PinnedGame",
                    proton_name=older.name,
                )
                candidates = find_cleanup_candidates(None, include_system_caches=True, home_dir=home)
                self.assertEqual({c.path for c in candidates}, {current})

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

    def test_format_size_bytes_mb_and_gb(self) -> None:
        try:
            from deckinstall.ui import format_size_bytes
        except Exception:
            self.skipTest("PySide6 not installed in this environment")
        self.assertEqual(format_size_bytes(500 * 1024 * 1024), "500.0 MB")
        self.assertEqual(format_size_bytes(1024 * 1024 * 1024), "1.00 GB")
        self.assertEqual(format_size_bytes(int(1.5 * 1024 * 1024 * 1024)), "1.50 GB")

    def test_mangohud_levels_and_system_package_cmds(self) -> None:
        opts = steam_vdf.apply_launch_presets(
            "",
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=2,
        )
        self.assertIn("MANGOHUD=1", opts)
        self.assertIn("position=top-center", opts)
        self.assertIn("horizontal", opts)
        self.assertIn("gamemoderun", opts)

        # Turning off MangoHud & GameMode strips MANGOHUD_CONFIG and gamemoderun
        stripped = steam_vdf.apply_launch_presets(
            opts,
            gamemode=False,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertEqual(stripped, "")

        # MangoHud position and FPS limit options
        opts_l2 = steam_vdf.apply_launch_presets(
            "",
            gamemode=False,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=2,
            mangohud_position="bottom-right",
            mangohud_fps_limit=60,
        )
        self.assertIn("MANGOHUD=1", opts_l2)
        self.assertIn("position=bottom-right", opts_l2)
        self.assertIn("fps_limit=60", opts_l2)
        self.assertIn("horizontal", opts_l2)
        self.assertNotIn("position=top-center", opts_l2)

        opts_l1 = steam_vdf.apply_launch_presets(
            opts_l2,
            gamemode=False,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=1,
            mangohud_position="top-left",
            mangohud_fps_limit=120,
        )
        self.assertIn("MANGOHUD_CONFIG=preset=1,position=top-left,fps_limit=120", opts_l1)

        opts_default = steam_vdf.apply_launch_presets(
            opts_l1,
            gamemode=False,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=None,
            mangohud_position="top-right",
            mangohud_fps_limit=40,
        )
        self.assertIn("MANGOHUD_CONFIG=position=top-right,fps_limit=40", opts_default)

        opts_reset = steam_vdf.apply_launch_presets(
            opts_default,
            gamemode=False,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=None,
            mangohud_position="",
            mangohud_fps_limit=0,
        )
        self.assertEqual(opts_reset, "MANGOHUD=1 %command%")

        with mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/apt-get" if b == "apt-get" else None):
            self.assertEqual(
                runtime_manager.get_system_package_install_cmd("gamemoderun"),
                ["apt-get", "install", "-y", "gamemode"],
            )
            # MangoHud comes from the official build (runtime_manager.ensure_mangohud), never the distro.
            self.assertIsNone(runtime_manager.get_system_package_install_cmd("mangohud"))
            self.assertIsNone(runtime_manager.get_system_package_install_cmd("gamescope"))

    def test_gpu_presets_and_unverified_prefix_cleanup(self) -> None:
        opts = steam_vdf.apply_launch_presets(
            "",
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            mangohud_level=2,
            nvidia_nvapi=True,
            prime_offload=True,
            proton_wayland=True,
        )
        self.assertIn("MANGOHUD=1", opts)
        self.assertIn("position=top-center", opts)
        self.assertIn("horizontal", opts)
        self.assertIn("PROTON_ENABLE_NVAPI=1", opts)
        self.assertIn("DXVK_ENABLE_NVAPI=1", opts)
        self.assertIn("__NV_PRIME_RENDER_OFFLOAD=1", opts)
        self.assertIn("__GLX_VENDOR_LIBRARY_NAME=nvidia", opts)
        self.assertIn("PROTON_ENABLE_WAYLAND=1", opts)

        stripped = steam_vdf.apply_launch_presets(
            opts,
            gamemode=False,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertEqual(stripped, "")

        # Unverified prefix cleanup: skipped when allow_unverified_saves=False, deleted when True, + backup ZIP
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hydra_pfx = root / "hydra" / "OldHydraGame"
            save_file = hydra_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat"
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"HYDRA-SAVE")

            with mock.patch.object(cleanup, "external_prefix_roots", return_value=[root / "hydra"]):
                cands = cleanup.find_cleanup_candidates(
                    None,
                    home_dir=root / "home",
                )
                self.assertEqual(len(cands), 1)
                self.assertTrue(cands[0].has_saves)
                self.assertFalse(cands[0].saves_verified_migrated)

                deleted, _freed, errs = cleanup.delete_cleanup_candidates_checked(
                    None, cands, allow_unverified_saves=False
                )
                self.assertEqual(deleted, 0)
                self.assertTrue(errs)
                self.assertTrue(hydra_pfx.is_dir())

                zpath, gcount, fcount = steam_vdf.backup_prefix_candidates_to_zip(cands, dest_root=root / "backups")
                self.assertTrue(zpath.is_file())
                self.assertEqual((gcount, fcount), (1, 1))

                deleted2, _freed2, errs2 = cleanup.delete_cleanup_candidates_checked(
                    None, cands, allow_unverified_saves=True
                )
                self.assertEqual(deleted2, 1)
                self.assertEqual(errs2, [])
                self.assertFalse(hydra_pfx.exists())


if __name__ == "__main__":
    unittest.main()
