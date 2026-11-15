"""Unit tests for standalone game management, runtime manager, and scanner enhancements."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
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

from deckinstall import runtime_manager, standalone  # noqa: E402
from deckinstall.scanner import detect_exe_kind, detect_steam_appid  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    apply_launch_presets,
    detect_helixsr_lsfg_conflict,
    find_cleanup_candidates,
)


def _make_steam_paths(root: Path, user_id: str = "123456") -> SteamPaths:
    userdata_dir = root / "userdata" / user_id
    config_dir = userdata_dir / "config"
    grid_dir = config_dir / "grid"
    steam_config_dir = root / "config"
    compatdata_dir = root / "steamapps" / "compatdata"
    for d in (grid_dir, steam_config_dir, compatdata_dir):
        d.mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=root,
        user_id=user_id,
        userdata_dir=userdata_dir,
        shortcuts_vdf=config_dir / "shortcuts.vdf",
        grid_dir=grid_dir,
        config_vdf=steam_config_dir / "config.vdf",
        compatdata_dir=compatdata_dir,
    )


class RuntimeManagerTests(unittest.TestCase):
    """Tests for GE-Proton11+ parsing, steamrt4 integrity, umu env, and bwrap checks."""

    def test_parse_ge_proton_version(self) -> None:
        self.assertEqual(runtime_manager._parse_ge_proton_version("GE-Proton11-12"), (11, 12))
        self.assertEqual(runtime_manager._parse_ge_proton_version("GE-Proton12-1"), (12, 1))
        self.assertIsNone(runtime_manager._parse_ge_proton_version("GE-Proton10-34"))
        self.assertIsNone(runtime_manager._parse_ge_proton_version("GE-Proton9-27"))

    def test_validate_steamrt4_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steamrt4_dir = Path(tmp) / "steamrt4"
            with mock.patch.object(runtime_manager, "get_steamrt4_dir", return_value=steamrt4_dir):
                # Missing directory is treated as clean/valid
                self.assertTrue(runtime_manager.validate_steamrt4_integrity())

                # Broken directory missing toolmanifest.vdf is removed
                steamrt4_dir.mkdir(parents=True)
                (steamrt4_dir / "mtree.txt.gz").write_bytes(b"partial")
                self.assertFalse(runtime_manager.validate_steamrt4_integrity())
                self.assertFalse(steamrt4_dir.exists())

                # Valid directory with both required files is kept
                steamrt4_dir.mkdir(parents=True)
                (steamrt4_dir / "toolmanifest.vdf").write_text('"manifest" {}', encoding="utf-8")
                (steamrt4_dir / "mtree.txt.gz").write_bytes(b"ok")
                self.assertTrue(runtime_manager.validate_steamrt4_integrity())
                self.assertTrue(steamrt4_dir.is_dir())

    def test_build_umu_env(self) -> None:
        prefix = Path("/tmp/test-prefix")
        proton = Path("/tmp/GE-Proton11-12")
        env = runtime_manager.build_umu_env(
            prefix,
            proton,
            extra_env={"WINEDLLOVERRIDES": "dxgi=n,b", "MANGOHUD": "1"},
        )
        self.assertEqual(env["WINEPREFIX"], str(prefix))
        self.assertEqual(env["PROTONPATH"], str(proton))
        self.assertEqual(env["GAMEID"], "umu-default")
        self.assertEqual(env["STORE"], "none")
        self.assertEqual(env["WINEDLLOVERRIDES"], "dxgi=n,b")
        self.assertEqual(env["MANGOHUD"], "1")

    def test_check_ubuntu_bwrap_userns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_sysctl = Path(tmp) / "apparmor_restrict_unprivileged_userns"
            orig_path_cls = runtime_manager.Path

            def _fake_path(arg: str | os.PathLike[str]) -> Path:
                if str(arg) == "/proc/sys/kernel/apparmor_restrict_unprivileged_userns":
                    return fake_sysctl
                return orig_path_cls(arg)

            with mock.patch.object(runtime_manager, "Path", side_effect=_fake_path):
                # Sysctl file absent -> None
                self.assertIsNone(runtime_manager.check_ubuntu_bwrap_userns())

                # Sysctl = 0 -> None
                fake_sysctl.write_text("0\n", encoding="utf-8")
                self.assertIsNone(runtime_manager.check_ubuntu_bwrap_userns())

                # Sysctl = 1 and bwrap fails -> actionable warning
                fake_sysctl.write_text("1\n", encoding="utf-8")
                with (
                    mock.patch.object(runtime_manager.shutil, "which", return_value="/usr/bin/bwrap"),
                    mock.patch.object(
                        runtime_manager.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(["bwrap"], returncode=1, stdout="", stderr="perm"),
                    ),
                ):
                    msg = runtime_manager.check_ubuntu_bwrap_userns()
                    self.assertIsNotNone(msg)
                    self.assertIn("apparmor_restrict_unprivileged_userns=0", msg or "")

                # Sysctl = 1 and bwrap succeeds -> None
                with (
                    mock.patch.object(runtime_manager.shutil, "which", return_value="/usr/bin/bwrap"),
                    mock.patch.object(
                        runtime_manager.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(["bwrap"], returncode=0, stdout="", stderr=""),
                    ),
                ):
                    self.assertIsNone(runtime_manager.check_ubuntu_bwrap_userns())


class ScannerTests(unittest.TestCase):
    """Tests for executable binary classification and deep Steam AppID detection."""

    def test_detect_exe_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            pe_path = root / "game.exe"
            pe_header = bytearray(b"MZ" + b"\x00" * 58 + (128).to_bytes(4, "little"))
            pe_header.extend(b"\x00" * (128 - len(pe_header)))
            pe_header.extend(b"PE\x00\x00")
            pe_path.write_bytes(bytes(pe_header))
            self.assertEqual(detect_exe_kind(pe_path), "windows_pe")

            dos_path = root / "dos.exe"
            dos_path.write_bytes(b"MZ" + b"\x00" * 62)
            self.assertEqual(detect_exe_kind(dos_path), "dos_mz")

            elf_path = root / "game.x86_64"
            elf_path.write_bytes(b"\x7fELF\x02\x01\x01\x00")
            self.assertEqual(detect_exe_kind(elf_path), "linux_elf")

            unknown_path = root / "readme.bin"
            unknown_path.write_bytes(b"NOT_AN_EXE")
            self.assertEqual(detect_exe_kind(unknown_path), "unknown")
            self.assertEqual(detect_exe_kind(root / "missing.exe"), "unknown")

    def test_detect_steam_appid_unreal_depth7_and_tenoke_ini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unreal_dir = Path(tmp) / "UnrealGame"
            deep_settings = (
                unreal_dir
                / "Engine"
                / "Binaries"
                / "ThirdParty"
                / "Steamworks"
                / "Steamv159"
                / "Win64"
                / "steam_settings"
            )
            deep_settings.mkdir(parents=True)
            (deep_settings / "steam_appid.txt").write_text("2358720\n", encoding="utf-8")
            self.assertEqual(detect_steam_appid(unreal_dir), 2358720)

            tenoke_dir = Path(tmp) / "TenokeGame"
            tenoke_dir.mkdir(parents=True)
            (tenoke_dir / "tenoke.ini").write_text("[TENOKE]\nid = 1293170 # Bzzzt\n", encoding="utf-8")
            self.assertEqual(detect_steam_appid(tenoke_dir), 1293170)


class StandaloneAndVdfTests(unittest.TestCase):
    """Tests for standalone game lifecycle, launch presets, HelixSR/LSFG conflict, and cleanup."""

    def test_standalone_lifecycle_in_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            prefixes_dir = games_dir / ".prefixes"
            apps_dir = root / "applications"
            src_game = root / "Downloads" / "My Indie Game"
            src_game.mkdir(parents=True)
            src_exe = src_game / "IndieGame.exe"
            src_exe.write_bytes(b"MZ")
            (src_game / "steam_appid.txt").write_text("1293170\n", encoding="utf-8")

            with (
                mock.patch.object(standalone, "GAMES_DIR", games_dir),
                mock.patch.object(standalone, "PREFIXES_DIR", prefixes_dir),
                mock.patch.object(standalone, "APPLICATIONS_DIR", apps_dir),
            ):
                installed = standalone.install_standalone_game(
                    game_name="My Indie Game",
                    exe_path=src_exe,
                    game_dir=src_game,
                    proton_name="GE-Proton11-12",
                    winedlloverrides="dxgi=n,b",
                    launch_options="MANGOHUD=1 %command%",
                )
                self.assertEqual(installed.game_dir, games_dir / "My Indie Game")
                self.assertTrue(installed.exe_path.is_file())
                self.assertTrue(installed.prefix_dir.is_dir())
                self.assertTrue(installed.launch_script.is_file())
                self.assertTrue(installed.desktop_file.is_file())
                self.assertEqual(installed.steam_appid, 1293170)

                listed = standalone.list_standalone_games()
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0].name, "My Indie Game")
                self.assertEqual(listed[0].winedlloverrides, "dxgi=n,b")

                updated = standalone.update_standalone_launch_options(
                    installed.game_dir,
                    winedlloverrides="winmm=n,b",
                    launch_options="gamemoderun %command%",
                    proton_name="GE-Proton12-1",
                )
                self.assertEqual(updated.winedlloverrides, "winmm=n,b")
                self.assertEqual(updated.launch_options, "gamemoderun %command%")
                self.assertEqual(updated.proton_name, "GE-Proton12-1")
                launch_sh = installed.launch_script.read_text(encoding="utf-8")
                self.assertIn("winmm=n,b", launch_sh)
                self.assertIn("GE-Proton12-1", launch_sh)

                # Remove integration files only (keep game & prefix dirs)
                standalone.remove_standalone_game(updated, delete_files=False)
                self.assertFalse(installed.desktop_file.exists())
                self.assertFalse(installed.launch_script.exists())
                self.assertEqual(standalone.list_standalone_games(), [])
                self.assertTrue(installed.game_dir.is_dir())
                self.assertTrue(installed.prefix_dir.is_dir())

                # Remove with delete_files=True removes game and prefix dirs
                standalone.remove_standalone_game(updated, delete_files=True)
                self.assertFalse(installed.game_dir.exists())
                self.assertFalse(installed.prefix_dir.exists())

    def test_apply_launch_presets_and_helixsr_lsfg_conflict(self) -> None:
        opts = apply_launch_presets(
            launch_options="",
            gamemode=True,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            winedlloverrides="dxgi=n,b",
        )
        self.assertIn('WINEDLLOVERRIDES="dxgi=n,b"', opts)
        self.assertIn("gamemoderun", opts)
        self.assertIn("%command%", opts)

        # Only dxgi=n,b without lsfg-vk -> False
        self.assertFalse(detect_helixsr_lsfg_conflict(opts, winedlloverrides="dxgi=n,b"))
        # lsfg-vk + dxgi=n,b -> True
        self.assertTrue(detect_helixsr_lsfg_conflict("lsfg-vk %command%", winedlloverrides="dxgi=n,b"))
        self.assertTrue(detect_helixsr_lsfg_conflict('WINEDLLOVERRIDES="dxgi=n,b" lsfg-vk %command%'))

    def test_find_cleanup_candidates_protects_active_standalone_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "home"
            steam_root = root / "Steam"
            paths = _make_steam_paths(steam_root)

            games_dir = home_dir / "Games"
            prefixes_dir = games_dir / ".prefixes"

            active_game_dir = games_dir / "ActiveGame"
            active_game_dir.mkdir(parents=True)
            active_prefix = prefixes_dir / "ActiveGame"
            (active_prefix / "drive_c" / "users").mkdir(parents=True)
            (active_game_dir / ".deckinstall_standalone.json").write_text(
                f'{{"name": "ActiveGame", "prefix_dir": "{active_prefix}"}}',
                encoding="utf-8",
            )

            orphan_prefix = prefixes_dir / "OrphanGame"
            (orphan_prefix / "drive_c" / "users").mkdir(parents=True)

            candidates = find_cleanup_candidates(
                paths,
                extra_external_roots=[prefixes_dir],
                home_dir=home_dir,
            )
            candidate_paths = {c.path.resolve() for c in candidates}
            self.assertNotIn(active_prefix.resolve(), candidate_paths)
            self.assertIn(orphan_prefix.resolve(), candidate_paths)


if __name__ == "__main__":
    unittest.main()
