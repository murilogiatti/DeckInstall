"""Unit tests for OptiScaler integration, WINEDLLOVERRIDES helpers, and archive extraction."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

try:
    import vdf  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    _fake_vdf = types.ModuleType("vdf")
    _fake_vdf.binary_loads = lambda _b: {"shortcuts": {}}
    _fake_vdf.binary_dumps = lambda _d: b"VDF"
    _fake_vdf.loads = lambda _s: {}
    _fake_vdf.dumps = lambda _d, pretty=True: "{}"
    sys.modules["vdf"] = _fake_vdf

from unittest.mock import patch

from deckinstall.extractor import extract_archive_to_dir
from deckinstall.optiscaler import (
    OPTISCALER_MANIFEST,
    OptiScalerConflictError,
    inspect_optiscaler_state,
    install_optiscaler_for_game,
    patch_optiscaler_ini,
    uninstall_optiscaler_for_game,
)
from deckinstall.standalone import StandaloneGame, remove_standalone_game
from deckinstall.steam_vdf import (
    apply_launch_presets,
    merge_winedlloverride,
    remove_winedlloverride,
)


def _make_fake_pe(path: Path) -> None:
    """Write a minimal valid PE header so detect_exe_kind returns 'windows_pe'."""
    buf = bytearray(128)
    buf[0:2] = b"MZ"
    buf[60:64] = (64).to_bytes(4, "little")
    buf[64:68] = b"PE\x00\x00"
    path.write_bytes(bytes(buf))


def _make_fake_elf(path: Path) -> None:
    """Write a minimal ELF header so detect_exe_kind returns 'linux_elf'."""
    path.write_bytes(b"\x7fELF" + b"\x00" * 60)


def _make_payload_dir(payload_dir: Path, ini_text: str = "") -> None:
    """Create a synthetic OptiScaler payload directory with OptiScaler.dll and auxiliary files."""
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "OptiScaler.dll").write_bytes(b"FAKE_OPTISCALER_DLL")
    if ini_text:
        (payload_dir / "OptiScaler.ini").write_text(ini_text, encoding="utf-8")
    (payload_dir / "setup_windows.bat").write_text("@echo off\n", encoding="utf-8")
    sub = payload_dir / "D3D12_Optiscaler"
    sub.mkdir(exist_ok=True)
    (sub / "readme.txt").write_text("aux", encoding="utf-8")


class WineDllOverrideTests(unittest.TestCase):
    def test_merge_and_remove_single_and_suffix(self) -> None:
        self.assertEqual(merge_winedlloverride("", "dxgi", "n,b"), "dxgi=n,b")
        self.assertEqual(merge_winedlloverride("d3d11=n,b", "dxgi.dll", "n,b"), "d3d11=n,b;dxgi=n,b")
        self.assertEqual(merge_winedlloverride("dxgi=n;d3d11=n,b", "DXGI.dll", "n,b"), "d3d11=n,b;dxgi=n,b")

        self.assertEqual(remove_winedlloverride("d3d11=n,b;dxgi=n,b", "dxgi"), "d3d11=n,b")
        self.assertEqual(remove_winedlloverride("dxgi=n,b", "dxgi.dll"), "")

    def test_merge_and_remove_multi_dll_entry(self) -> None:
        merged = merge_winedlloverride("winmm,dxgi=n,b;version=n", "dxgi", "n,b")
        self.assertEqual(merged, "winmm=n,b;version=n;dxgi=n,b")

        removed = remove_winedlloverride("winmm,dxgi=n,b;version=n", "dxgi.dll")
        self.assertEqual(removed, "winmm=n,b;version=n")


class PatchOptiScalerIniTests(unittest.TestCase):
    def test_patch_existing_sections_and_keys(self) -> None:
        raw = (
            "[Upscalers]\n"
            "Dx12Upscaler=auto\n"
            "Dx11Upscaler=auto\n"
            "VulkanUpscaler=auto\n"
            "\n"
            "[FrameGen]\n"
            "FGType=optifg\n"
            "\n"
            "[Spoofing]\n"
            "Dxgi=true\n"
            "\n"
            "[Menu]\n"
            "OverlayMenu=false\n"
        )
        patched_auto = patch_optiscaler_ini(raw, backend="fsr3", fg_mode="auto")
        self.assertIn("Dx12Upscaler=fsr31", patched_auto)
        self.assertIn("Dx11Upscaler=fsr31", patched_auto)
        self.assertIn("VulkanUpscaler=fsr31", patched_auto)
        self.assertIn("FGType=auto", patched_auto)
        self.assertNotIn("FGType=optifg", patched_auto)
        self.assertIn("Dxgi=auto", patched_auto)
        self.assertIn("OverlayMenu=true", patched_auto)
        self.assertNotIn("OverlayMenu=false", patched_auto)

        # Legacy "default" fg_mode alias maps to "auto"
        patched_alias = patch_optiscaler_ini(raw, backend="fsr22", fg_mode="default")
        self.assertIn("Dx12Upscaler=fsr22", patched_alias)
        self.assertIn("FGType=auto", patched_alias)

        patched_nofg = patch_optiscaler_ini(
            raw,
            backend="fsr21",
            fg_mode="nofg",
            spoof_mode="false",
            overlay_menu=False,
        )
        self.assertIn("Dx12Upscaler=fsr21", patched_nofg)
        self.assertIn("FGType=nofg", patched_nofg)
        self.assertIn("Dxgi=false", patched_nofg)
        self.assertIn("OverlayMenu=false", patched_nofg)
        self.assertNotIn("FGType=optifg", patched_nofg)

        patched_optifg = patch_optiscaler_ini(raw, backend="auto", fg_mode="optifg", spoof_mode="true")
        self.assertIn("FGType=optifg", patched_optifg)
        self.assertIn("Dxgi=true", patched_optifg)

        patched_nukems = patch_optiscaler_ini(raw, backend="dlss", fg_mode="nukems")
        self.assertIn("Dx12Upscaler=dlss", patched_nukems)
        self.assertIn("FGType=nukems", patched_nukems)

    def test_patch_empty_ini_adds_sections(self) -> None:
        patched_auto = patch_optiscaler_ini("", backend="xess", fg_mode="auto")
        self.assertIn("[Upscalers]", patched_auto)
        self.assertIn("Dx12Upscaler=xess", patched_auto)
        self.assertIn("Dx11Upscaler=xess", patched_auto)
        self.assertIn("VulkanUpscaler=xess", patched_auto)
        self.assertIn("[FrameGen]", patched_auto)
        self.assertIn("FGType=auto", patched_auto)
        self.assertIn("[Spoofing]", patched_auto)
        self.assertIn("Dxgi=auto", patched_auto)
        self.assertIn("[Menu]", patched_auto)
        self.assertIn("OverlayMenu=true", patched_auto)

        patched_nofg = patch_optiscaler_ini(
            "", backend="xess", fg_mode="nofg", spoof_mode="false", overlay_menu=False
        )
        self.assertIn("[FrameGen]", patched_nofg)
        self.assertIn("FGType=nofg", patched_nofg)
        self.assertIn("Dxgi=false", patched_nofg)
        self.assertIn("OverlayMenu=false", patched_nofg)


class OptiScalerInstallUninstallTests(unittest.TestCase):
    def test_install_conflict_backup_restore_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "game"
            game_dir.mkdir()
            exe_path = game_dir / "Game.exe"
            _make_pe_helper = _make_fake_pe
            _make_pe_helper(exe_path)

            # Pre-existing unmanaged dxgi.dll
            existing_dll = game_dir / "dxgi.dll"
            existing_dll.write_bytes(b"ORIGINAL_DXGI")

            payload_dir = root / "payload"
            _make_payload_dir(payload_dir, ini_text="[FrameGen]\nFGType=optifg\n")

            with self.assertRaises(OptiScalerConflictError) as ctx:
                install_optiscaler_for_game(
                    exe_path,
                    proxy_dll="dxgi",
                    backend="fsr3",
                    overwrite_existing=False,
                    payload_dir=payload_dir,
                )
            self.assertEqual(ctx.exception.existing_path, existing_dll)
            self.assertEqual(existing_dll.read_bytes(), b"ORIGINAL_DXGI")

            state = install_optiscaler_for_game(
                exe_path,
                proxy_dll="dxgi",
                backend="fsr3",
                fg_mode="nukems",
                spoof_mode="false",
                overlay_menu=False,
                overwrite_existing=True,
                payload_dir=payload_dir,
            )
            self.assertEqual(state.proxy_dll, "dxgi")
            self.assertEqual(state.backend, "fsr3")
            self.assertEqual(state.fg_mode, "nukems")
            self.assertEqual(state.spoof_mode, "false")
            self.assertFalse(state.overlay_menu)
            self.assertEqual(existing_dll.read_bytes(), b"FAKE_OPTISCALER_DLL")
            self.assertEqual((game_dir / "dxgi.dll.deckinstall.bak").read_bytes(), b"ORIGINAL_DXGI")
            self.assertFalse((game_dir / "setup_windows.bat").exists())
            self.assertTrue((game_dir / "D3D12_Optiscaler" / "readme.txt").is_file())
            self.assertTrue((game_dir / OPTISCALER_MANIFEST).is_file())

            ini_content = (game_dir / "OptiScaler.ini").read_text(encoding="utf-8")
            self.assertIn("FGType=nukems", ini_content)
            self.assertIn("Dxgi=false", ini_content)
            self.assertIn("OverlayMenu=false", ini_content)
            self.assertIn("Dx12Upscaler=fsr31", ini_content)

            inspected = inspect_optiscaler_state(exe_path)
            self.assertIsNotNone(inspected)
            assert inspected is not None
            self.assertEqual(inspected.proxy_dll, "dxgi")
            self.assertEqual(inspected.fg_mode, "nukems")
            self.assertEqual(inspected.spoof_mode, "false")
            self.assertFalse(inspected.overlay_menu)

            res = uninstall_optiscaler_for_game(exe_path)
            self.assertIsNotNone(res.state)
            self.assertEqual(res.restored_count, 1)
            self.assertGreaterEqual(res.removed_count, 2)
            self.assertEqual(existing_dll.read_bytes(), b"ORIGINAL_DXGI")
            self.assertFalse((game_dir / "dxgi.dll.deckinstall.bak").exists())
            self.assertFalse((game_dir / "OptiScaler.ini").exists())
            self.assertFalse((game_dir / OPTISCALER_MANIFEST).exists())
            self.assertIsNone(inspect_optiscaler_state(exe_path))

    def test_linux_elf_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            elf_path = root / "game.x86_64"
            _make_fake_elf(elf_path)
            payload_dir = root / "payload"
            _make_payload_dir(payload_dir)

            with self.assertRaises(ValueError):
                install_optiscaler_for_game(
                    elf_path,
                    proxy_dll="dxgi",
                    backend="auto",
                    payload_dir=payload_dir,
                )


class ExtractorTests(unittest.TestCase):
    def test_extract_zip_and_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good_zip = root / "good.zip"
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.writestr("OptiScaler.dll", b"DLLDATA")
                zf.writestr("sub/file.txt", b"hello")

            dest = root / "out_good"
            extract_archive_to_dir(good_zip, dest)
            self.assertEqual((dest / "OptiScaler.dll").read_bytes(), b"DLLDATA")
            self.assertEqual((dest / "sub" / "file.txt").read_bytes(), b"hello")

            bad_zip = root / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.txt", b"nope")

            bad_dest = root / "out_bad"
            with self.assertRaises(RuntimeError):
                extract_archive_to_dir(bad_zip, bad_dest)
            self.assertFalse((root / "escape.txt").exists())


class LaunchPresetsAndStandaloneGuardTests(unittest.TestCase):
    def test_apply_launch_presets_preserves_custom_mangohud_config(self) -> None:
        initial = "MANGOHUD=1 MANGOHUD_CONFIG=fps_limit=60,font_size=24 %command%"
        updated = apply_launch_presets(
            initial,
            gamemode=True,
            mangohud=True,
            wined3d=False,
            no_fsync=False,
            mangohud_level=None,
        )
        self.assertIn("MANGOHUD_CONFIG=fps_limit=60,font_size=24", updated)
        self.assertIn("gamemoderun", updated)

    def test_remove_standalone_game_protected_dir_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected_games_dir = root / "Games"
            protected_games_dir.mkdir()
            marker = protected_games_dir / "keep.txt"
            marker.write_text("do not delete", encoding="utf-8")

            apps_dir = root / "applications"
            apps_dir.mkdir()
            desktop_file = apps_dir / "game.desktop"
            launch_script = protected_games_dir / "launch.sh"
            desktop_file.write_text("[Desktop Entry]\n", encoding="utf-8")
            launch_script.write_text("#!/bin/sh\n", encoding="utf-8")

            game = StandaloneGame(
                name="TestGame",
                game_dir=protected_games_dir,
                exe_path=protected_games_dir / "game.exe",
                prefix_dir=root / "prefix",
                proton_name="Proton",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=launch_script,
                desktop_file=desktop_file,
            )
            with (
                patch("deckinstall.standalone.get_games_dir", return_value=protected_games_dir),
                patch("deckinstall.standalone.APPLICATIONS_DIR", apps_dir),
            ):
                remove_standalone_game(game, delete_files=True, delete_prefix=False)

            self.assertTrue(protected_games_dir.exists())
            self.assertTrue(marker.exists())
            self.assertFalse(desktop_file.exists())
            self.assertFalse(launch_script.exists())

    def test_install_optiscaler_rejects_symlink_subdir_escaping_exe_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            game_dir = root / "game"
            game_dir.mkdir()
            exe_path = game_dir / "Game.exe"
            exe_path.write_bytes(b"MZ\x90\x00")
            (game_dir / "sub").symlink_to(outside)

            payload_dir = root / "payload"
            payload_dir.mkdir()
            (payload_dir / "OptiScaler.dll").write_bytes(b"OPTIDLL")
            (payload_dir / "sub").mkdir()
            (payload_dir / "sub" / "extra.dll").write_bytes(b"EXTRADLL")

            with self.assertRaisesRegex(RuntimeError, "Unsafe symlink in destination path"):
                install_optiscaler_for_game(exe_path, payload_dir=payload_dir)

            self.assertFalse((outside / "extra.dll").exists())


if __name__ == "__main__":
    unittest.main()
