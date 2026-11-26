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
from deckinstall.cleanup import find_cleanup_candidates  # noqa: E402
from deckinstall.fsutil import copy_or_move_dir_contents  # noqa: E402
from deckinstall.scanner import detect_exe_kind, detect_steam_appid  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    apply_launch_presets,
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
        self.assertEqual(runtime_manager._parse_ge_proton_version("GE-Proton10-34"), None)
        self.assertIsNone(runtime_manager._parse_ge_proton_version("GE-Proton9-27"))

    def test_safe_extract_tar_allows_internal_relative_symlinks_and_blocks_escapes(self) -> None:
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok_tar = root / "ok.tar"
            with tarfile.open(ok_tar, "w") as tf:
                target_info = tarfile.TarInfo("GE-Proton11-12/files/lib/wine/i386-windows/start.exe")
                data = b"MZ"
                target_info.size = len(data)
                tf.addfile(target_info, io.BytesIO(data))

                link_info = tarfile.TarInfo(
                    "GE-Proton11-12/files/share/default_pfx/drive_c/windows/system32/start.exe"
                )
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = "../../../../../lib/wine/i386-windows/start.exe"
                tf.addfile(link_info)

            dest_ok = root / "dest_ok"
            dest_ok.mkdir()
            runtime_manager._safe_extract_tar(ok_tar, dest_ok)
            self.assertTrue(
                (dest_ok / "GE-Proton11-12/files/share/default_pfx/drive_c/windows/system32/start.exe").is_symlink()
            )

            bad_tar = root / "bad.tar"
            with tarfile.open(bad_tar, "w") as tf:
                bad_link = tarfile.TarInfo("GE-Proton11-12/escape")
                bad_link.type = tarfile.SYMTYPE
                bad_link.linkname = "../../../../etc/passwd"
                tf.addfile(bad_link)

            dest_bad = root / "dest_bad"
            dest_bad.mkdir()
            with self.assertRaises(RuntimeError):
                runtime_manager._safe_extract_tar(bad_tar, dest_bad)

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

    def test_get_system_package_install_cmd_and_fix_bwrap(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/apt-get" if b == "apt-get" else None):
            self.assertEqual(
                runtime_manager.get_system_package_install_cmd("gamemoderun"),
                ["apt-get", "install", "-y", "gamemode"],
            )
            # MangoHud never comes from the distro (Debian/Ubuntu builds lack NVIDIA support).
            self.assertIsNone(runtime_manager.get_system_package_install_cmd("mangohud"))
        with mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/pacman" if b == "pacman" else None):
            self.assertEqual(
                runtime_manager.get_system_package_install_cmd("winetricks"),
                ["pacman", "-S", "--noconfirm", "winetricks"],
            )
        with mock.patch("shutil.which", return_value=None):
            ok_unconfirmed, _ = runtime_manager.fix_bwrap_userns()
            self.assertFalse(ok_unconfirmed)
            ok, msg = runtime_manager.fix_bwrap_userns(confirmed=True)
            self.assertFalse(ok)
            self.assertIn("apparmor_parser", msg)
        with (
            mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/pkexec" if b == "pkexec" else None),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as mrun,
        ):
            ok, _ = runtime_manager.fix_bwrap_userns(confirmed=True)
            self.assertTrue(ok)
            self.assertEqual(mrun.call_args[0][0][0], "/usr/bin/pkexec")

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
            self.assertEqual(detect_exe_kind(dos_path), "unknown")

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
    """Tests for standalone game lifecycle, launch presets, and cleanup."""

    def test_prefix_merge_does_not_follow_destination_symlinks(self) -> None:
        for move in (False, True):
            with self.subTest(move=move), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                target = root / "target"
                outside = root / "outside"
                for folder in (source, target, outside):
                    folder.mkdir()
                (source / "users").mkdir()
                (source / "users" / "save.dat").write_bytes(b"import-save")
                (source / "user.reg").write_bytes(b"import-registry")
                (outside / "save.dat").write_bytes(b"outside-save")
                (outside / "user.reg").write_bytes(b"outside-registry")
                (target / "users").symlink_to(outside, target_is_directory=True)
                (target / "user.reg").symlink_to(outside / "user.reg")

                copy_or_move_dir_contents(source, target, move=move)

                self.assertEqual((target / "users" / "save.dat").read_bytes(), b"import-save")
                self.assertEqual((target / "user.reg").read_bytes(), b"import-registry")
                self.assertFalse((target / "users").is_symlink())
                self.assertFalse((target / "user.reg").is_symlink())
                self.assertEqual((outside / "save.dat").read_bytes(), b"outside-save")
                self.assertEqual((outside / "user.reg").read_bytes(), b"outside-registry")

    def test_prefix_merge_accepts_directory_over_file_and_same_directory(self) -> None:
        for move in (False, True):
            with self.subTest(move=move), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                target = root / "target"
                (source / "users").mkdir(parents=True)
                (source / "users" / "save.dat").write_bytes(b"save")
                target.mkdir()
                (target / "users").write_bytes(b"old-file")
                copy_or_move_dir_contents(source, target, move=move)
                copy_or_move_dir_contents(target, target, move=move)
                self.assertEqual((target / "users" / "save.dat").read_bytes(), b"save")

    def test_standalone_rejects_invalid_executable_before_moving_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            source = root / "Source"
            source.mkdir()
            (source / "save.dat").write_bytes(b"keep-save")
            outside_exe = root / "outside.exe"
            outside_exe.write_bytes(b"MZ")
            with mock.patch.object(standalone, "get_games_dir", return_value=games_dir):
                with self.assertRaises(ValueError):
                    standalone.install_standalone_game("Invalid", outside_exe, source)
                with self.assertRaises(FileNotFoundError):
                    standalone.install_standalone_game("Missing", source / "missing.exe", source)
            self.assertEqual((source / "save.dat").read_bytes(), b"keep-save")
            self.assertFalse(games_dir.exists())

    def test_shader_cache_pruning_revalidates_stale_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            apps_dir = root / "applications"
            source = games_dir / "My Game"
            source.mkdir(parents=True)
            exe = source / "game.exe"
            exe.write_bytes(b"MZ")
            with (
                mock.patch.object(standalone, "get_games_dir", return_value=games_dir),
                mock.patch.object(standalone, "get_applications_dir", return_value=apps_dir),
                mock.patch.object(standalone, "resolve_game_icon", return_value=None),
            ):
                cache = standalone.get_game_cache_dir(source)
                cache.mkdir(parents=True)
                (cache / "existing.cache").write_bytes(b"keep-cache")
                from deckinstall import cleanup

                stale = [
                    c for c in cleanup.find_cleanup_candidates(home_dir=root)
                    if c.category == "Orphan Game Cache"
                ]
                self.assertEqual([c.path for c in stale], [cache])
                standalone.install_standalone_game(
                    "My Game", exe, source, keep_in_place=True, create_menu_shortcut=False
                )
                # The Maintenance delete rescans first: the cache has an owner now, so it stays.
                deleted, freed, errors = cleanup.delete_cleanup_candidates_checked(candidates=stale, home_dir=root)
                self.assertEqual((deleted, freed), (0, 0))
                self.assertTrue(errors)
                self.assertEqual((cache / "existing.cache").read_bytes(), b"keep-cache")

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

    def test_apply_launch_presets_with_winedlloverrides(self) -> None:
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
                f'{{"name": "ActiveGame", "exe_path": "{active_game_dir / "game.exe"}", "prefix_dir": "{active_prefix}"}}',
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

    def test_standalone_desktop_shortcut_and_pe_icon_extraction(self) -> None:
        import struct

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            prefixes_dir = games_dir / ".prefixes"
            apps_dir = root / "applications"
            desktop_dir = root / "Desktop"
            src_game = root / "Downloads" / "IconGame"
            src_game.mkdir(parents=True)
            src_exe = src_game / "IconGame.exe"

            # Build minimal valid PE32 with .rsrc containing RT_ICON(3) & RT_GROUP_ICON(14)
            icon_png = b"\x89PNG\r\n\x1a\nFAKE_ICON_BYTES"
            grp_blob = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHIH", 32, 32, 0, 0, 1, 32, len(icon_png), 1)

            # Lay out .rsrc starting at file offset 0x200, RVA 0x2000
            rsrc = bytearray(512)
            # Level 1 (offset 0): 2 ID entries: type 3 -> subdir 0x28, type 14 -> subdir 0x60
            struct.pack_into("<12xHH", rsrc, 0, 0, 2)
            struct.pack_into("<II", rsrc, 16, 3, 0x80000028)
            struct.pack_into("<II", rsrc, 24, 14, 0x80000060)

            # Type 3 Level 2 (offset 0x28): 1 ID entry: id 1 -> subdir 0x40
            struct.pack_into("<12xHH", rsrc, 0x28, 0, 1)
            struct.pack_into("<II", rsrc, 0x38, 1, 0x80000040)
            # Type 3 Level 3 (offset 0x40): 1 ID entry: lang 1033 -> data entry 0x58
            struct.pack_into("<12xHH", rsrc, 0x40, 0, 1)
            struct.pack_into("<II", rsrc, 0x50, 1033, 0x58)
            # Data entry 0x58: RVA 0x2100, size len(icon_png)
            struct.pack_into("<II", rsrc, 0x58, 0x2100, len(icon_png))

            # Type 14 Level 2 (offset 0x60): 1 ID entry: id 1 -> subdir 0x78
            struct.pack_into("<12xHH", rsrc, 0x60, 0, 1)
            struct.pack_into("<II", rsrc, 0x70, 1, 0x80000078)
            # Type 14 Level 3 (offset 0x78): 1 ID entry: lang 1033 -> data entry 0x90
            struct.pack_into("<12xHH", rsrc, 0x78, 0, 1)
            struct.pack_into("<II", rsrc, 0x88, 1033, 0x90)
            # Data entry 0x90: RVA 0x2140, size len(grp_blob)
            struct.pack_into("<II", rsrc, 0x90, 0x2140, len(grp_blob))

            rsrc[0x100 : 0x100 + len(icon_png)] = icon_png
            rsrc[0x140 : 0x140 + len(grp_blob)] = grp_blob

            pe = bytearray(0x200)
            pe[0:2] = b"MZ"
            struct.pack_into("<I", pe, 0x3C, 0x80)
            pe[0x80:0x84] = b"PE\x00\x00"
            struct.pack_into("<H", pe, 0x86, 1)  # NumberOfSections
            struct.pack_into("<H", pe, 0x94, 128)  # SizeOfOptionalHeader
            opt = 0x98
            struct.pack_into("<H", pe, opt, 0x10B)  # PE32
            struct.pack_into("<II", pe, opt + 96 + 2 * 8, 0x2000, len(rsrc))
            sec = opt + 128
            pe[sec : sec + 5] = b".rsrc"
            struct.pack_into("<IIII", pe, sec + 8, len(rsrc), 0x2000, len(rsrc), 0x200)
            src_exe.write_bytes(bytes(pe) + bytes(rsrc))

            # Non-PE file returns None safely
            bad_exe = root / "bad.exe"
            bad_exe.write_bytes(b"NOT_PE")
            self.assertIsNone(standalone.extract_pe_icon(bad_exe, root / "bad.ico"))

            with (
                mock.patch.object(standalone, "GAMES_DIR", games_dir),
                mock.patch.object(standalone, "PREFIXES_DIR", prefixes_dir),
                mock.patch.object(standalone, "APPLICATIONS_DIR", apps_dir),
                mock.patch.object(standalone, "get_desktop_dir", return_value=desktop_dir),
            ):
                installed = standalone.install_standalone_game(
                    game_name="IconGame",
                    exe_path=src_exe,
                    game_dir=src_game,
                    proton_name="GE-Proton11-12",
                    create_menu_shortcut=False,
                    create_desktop_shortcut=True,
                )
                self.assertFalse(installed.desktop_file.exists())
                self.assertIsNotNone(installed.desktop_shortcut)
                assert installed.desktop_shortcut is not None
                self.assertTrue(installed.desktop_shortcut.is_file())
                ico_path = installed.game_dir / ".deckinstall_icon.ico"
                self.assertTrue(ico_path.is_file())
                self.assertIn(f"Icon={ico_path}", installed.desktop_shortcut.read_text(encoding="utf-8"))
                self.assertIn(icon_png, ico_path.read_bytes())

                standalone.remove_standalone_game(installed, delete_files=False)
                self.assertFalse(installed.desktop_shortcut.exists())

    def test_standalone_keep_in_place_native_linux_nested_icon_and_prefix_only_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            games_dir = root / "Games"
            prefixes_dir = games_dir / ".prefixes"
            apps_dir = root / "applications"

            # 1) Keep-in-place + nested icon in Resources/ + prefix-only removal
            sd_game = root / "sd_card" / "PixelQuest"
            (sd_game / "Resources").mkdir(parents=True)
            sd_exe = sd_game / "PixelQuest.exe"
            sd_exe.write_bytes(b"MZ")
            nested_ico = sd_game / "Resources" / "app.ico"
            nested_ico.write_bytes(b"\x00\x00\x01\x00NESTED")

            with (
                mock.patch.object(standalone, "GAMES_DIR", games_dir),
                mock.patch.object(standalone, "PREFIXES_DIR", prefixes_dir),
                mock.patch.object(standalone, "APPLICATIONS_DIR", apps_dir),
            ):
                kept = standalone.install_standalone_game(
                    game_name="PixelQuest",
                    exe_path=sd_exe,
                    game_dir=sd_game,
                    proton_name="GE-Proton11-12",
                    keep_in_place=True,
                )
                self.assertEqual(kept.game_dir, sd_game)
                self.assertEqual(kept.exe_path, sd_exe)
                self.assertTrue(kept.prefix_dir.is_dir())
                self.assertIn(f"Icon={nested_ico}", kept.desktop_file.read_text(encoding="utf-8"))

                # Remove shortcut + prefix only: game files stay, prefix is deleted
                standalone.remove_standalone_game(kept, delete_files=False, delete_prefix=True)
                self.assertFalse(kept.desktop_file.exists())
                self.assertFalse(kept.prefix_dir.exists())
                self.assertTrue(sd_exe.is_file())

                # 2) Native Linux + single_exe_only from Downloads
                dl_dir = root / "Downloads"
                dl_dir.mkdir(parents=True)
                elf_bin = dl_dir / "CelesteNative"
                elf_bin.write_bytes(b"\x7fELF\x02\x01\x01\x00")
                native = standalone.install_standalone_game(
                    game_name="CelesteNative",
                    exe_path=elf_bin,
                    game_dir=dl_dir,
                    native_linux=True,
                    single_exe_only=True,
                )
                self.assertEqual(native.game_dir, games_dir / "CelesteNative")
                self.assertTrue(native.exe_path.is_file())
                self.assertFalse(elf_bin.exists())
                self.assertTrue(native.native_linux)
                launch_sh = native.launch_script.read_text(encoding="utf-8")
                self.assertNotIn("umu-run", launch_sh)
                self.assertNotIn("WINEPREFIX", launch_sh)
                self.assertIn(str(native.exe_path), launch_sh)

    def test_v090_gpu_profile_umu_gameid_winetricks_and_disk_space(self) -> None:
        from deckinstall import extractor

        # 1) resolve_umu_game_id
        self.assertEqual(standalone.resolve_umu_game_id(1293170), ("umu-1293170", "steam"))
        self.assertEqual(standalone.resolve_umu_game_id(None), ("umu-default", "none"))

        # 2) detect_gpu_profile (mocked DRM vendor files)
        with tempfile.TemporaryDirectory() as tmp:
            drm = Path(tmp) / "drm"
            (drm / "card0" / "device").mkdir(parents=True)
            (drm / "card1" / "device").mkdir(parents=True)
            (drm / "card0" / "device" / "vendor").write_text("0x8086\n", encoding="utf-8")
            (drm / "card1" / "device" / "vendor").write_text("0x10de\n", encoding="utf-8")

            orig_glob = Path.glob

            def _fake_glob(self_p: Path, pattern: str):
                if str(self_p) == "/sys/class/drm" and pattern == "card*/device/vendor":
                    return drm.glob(pattern)
                return orig_glob(self_p, pattern)

            with (
                mock.patch.object(Path, "glob", _fake_glob),
                mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=False),
            ):
                prof = runtime_manager.detect_gpu_profile()
                self.assertTrue(prof.has_nvidia)
                self.assertTrue(prof.has_intel)
                self.assertTrue(prof.is_hybrid)
                self.assertTrue(prof.is_wayland)

            # 3) check_disk_space & InsufficientSpaceError
            with mock.patch("shutil.disk_usage", return_value=os.statvfs_result((4096, 4096, 1000, 100, 100, 0, 0, 0, 0, 255))):
                pass
            fake_usage = type("Usage", (), {"total": 1000, "used": 900, "free": 100})()
            with mock.patch("shutil.disk_usage", return_value=fake_usage):
                with self.assertRaises(extractor.InsufficientSpaceError):
                    extractor.check_disk_space(Path(tmp), 500)

            # 4) run_winetricks_verbs validation + Popen mock
            with self.assertRaises(ValueError):
                runtime_manager.run_winetricks_verbs(Path(tmp), ["vcrun2022;rm -rf /"])

            r_fd, w_fd = os.pipe()
            os.write(w_fd, b"Executing vcrun2022\n")
            os.close(w_fd)
            with (
                mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/winetricks" if b == "winetricks" else None),
                mock.patch("subprocess.Popen") as mock_popen,
                os.fdopen(r_fd, "r", encoding="utf-8") as stdout_reader,
            ):
                proc = mock.MagicMock()
                proc.stdout = stdout_reader
                proc.poll.return_value = 0
                proc.wait.return_value = 0
                proc.returncode = 0
                mock_popen.return_value = proc
                logs: list[str] = []
                runtime_manager.run_winetricks_verbs(Path(tmp) / "pfx", ["vcrun2022"], on_log=logs.append)
                self.assertTrue(any("vcrun2022" in line for line in logs))


if __name__ == "__main__":
    unittest.main()


def test_bundled_winetricks_and_cabextract_when_the_system_has_none(tmp_path, monkeypatch) -> None:
    """SteamOS has neither and loses what is installed at updates: the AppImage carries them."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("winetricks", "cabextract"):
        (tools / name).write_text("#!/bin/sh\n")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("APPDIR", str(tmp_path))
    monkeypatch.setattr(runtime_manager.shutil, "which", lambda name, path=None: None)
    assert runtime_manager.find_winetricks() == str(tools / "winetricks")
    env = runtime_manager.with_bundled_tools({"PATH": "/usr/bin", "DISPLAY": ":0"})
    assert env["PATH"] == f"{tools}:/usr/bin" and env["DISPLAY"] == ":0"
    monkeypatch.setattr(runtime_manager.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    assert runtime_manager.find_winetricks() == "/usr/bin/winetricks"  # the system's first
    assert runtime_manager.with_bundled_tools({"PATH": "/usr/bin"})["PATH"] == "/usr/bin"
