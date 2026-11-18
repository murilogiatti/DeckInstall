"""Regression and security tests for Fase 4 coverage."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall.extractor import _is_non_game_helper_exe  # noqa: E402
from deckinstall.fsutil import file_sha256  # noqa: E402
from deckinstall.optiscaler import (  # noqa: E402
    OPTISCALER_MANIFEST,
    inspect_optiscaler_state,
    install_optiscaler_for_game,
    uninstall_optiscaler_for_game,
)
from deckinstall.runtime_manager import (  # noqa: E402
    ProtonRelease,
    ensure_ge_proton10,
    is_win32_prefix,
    list_ge_proton10_dirs,
    run_exe_in_prefix,
)
from deckinstall.save_sync import pull_game_saves, push_game_saves  # noqa: E402
from deckinstall.scanner import default_scan_roots, scan_all_games  # noqa: E402
from deckinstall.standalone import (  # noqa: E402
    StandaloneGame,
    install_standalone_game,
    remove_standalone_game,
    resolve_standalone_proton_fallback,
)
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import (  # noqa: E402
    _format_launch_token,
    find_cleanup_candidates,
    infer_game_root_from_exe,
)


def _make_dummy_pe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal MZ/PE header so detect_exe_kind recognizes a Windows PE executable
    data = bytearray(0x100)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(bytes(data))


def _make_optiscaler_payload(payload_dir: Path) -> None:
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "OptiScaler.dll").write_bytes(b"OPTISCALER_DLL")
    (payload_dir / "OptiScaler.ini").write_text("[Upscalers]\nDx12Upscaler=auto\n", encoding="utf-8")


class Fase4RegressionTests(unittest.TestCase):
    """Targeted regression tests for security, standalone, OptiScaler, Proton 10, scanner, and i18n."""

    def test_infer_game_root_desktop_stfolder_and_multigame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            desktop = fake_home / "Desktop"
            desktop.mkdir(parents=True)
            loose_desktop_exe = desktop / "loose.exe"
            _make_dummy_pe(loose_desktop_exe)

            desktop_game_exe = desktop / "KIBORG" / "KIBORG.exe"
            _make_dummy_pe(desktop_game_exe)

            sync_root = fake_home / "SyncGames"
            (sync_root / ".stfolder").mkdir(parents=True)
            loose_sync_exe = sync_root / "loose.exe"
            _make_dummy_pe(loose_sync_exe)
            nested_sync_exe = sync_root / "KIBORG" / "Binaries" / "Win64" / "KIBORG-Win64-Shipping.exe"
            _make_dummy_pe(nested_sync_exe)

            container = fake_home / "CustomContainer"
            _make_dummy_pe(container / "GameA" / "a.exe")
            _make_dummy_pe(container / "GameB" / "b.exe")
            loose_container_exe = container / "loose.exe"
            _make_dummy_pe(loose_container_exe)

            with mock.patch("pathlib.Path.home", return_value=fake_home):
                with self.assertRaises(ValueError):
                    infer_game_root_from_exe(loose_desktop_exe)
                self.assertEqual(
                    infer_game_root_from_exe(desktop_game_exe),
                    (desktop / "KIBORG").resolve(),
                )

                with self.assertRaises(ValueError):
                    infer_game_root_from_exe(loose_sync_exe)
                self.assertEqual(
                    infer_game_root_from_exe(nested_sync_exe),
                    (sync_root / "KIBORG").resolve(),
                )

                with self.assertRaises(ValueError):
                    infer_game_root_from_exe(loose_container_exe)
                self.assertEqual(
                    infer_game_root_from_exe(container / "GameA" / "a.exe"),
                    (container / "GameA").resolve(),
                )

    def test_install_standalone_rejects_protected_dirs_and_stfolder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            games_dir = fake_home / "Games"
            prefixes_dir = games_dir / ".prefixes"
            desktop_dir = fake_home / "Desktop"
            desktop_dir.mkdir(parents=True)
            desktop_exe = desktop_dir / "game.exe"
            _make_dummy_pe(desktop_exe)

            sync_dir = fake_home / "MySync"
            (sync_dir / ".stfolder").mkdir(parents=True)
            sync_exe = sync_dir / "game.exe"
            _make_dummy_pe(sync_exe)

            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch("deckinstall.standalone.get_games_dir", return_value=games_dir),
                mock.patch("deckinstall.standalone.get_prefixes_dir", return_value=prefixes_dir),
            ):
                with self.assertRaises(ValueError):
                    install_standalone_game("BadDesktop", desktop_exe, desktop_dir, keep_in_place=False)
                with self.assertRaises(ValueError):
                    install_standalone_game("BadSync", sync_exe, sync_dir, keep_in_place=False)

    def test_remove_standalone_rejects_out_of_bounds_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            apps_dir.mkdir()
            desktop_dir = root / "Desktop"
            desktop_dir.mkdir()
            game_dir = root / "Games" / "LegitGame"
            game_dir.mkdir(parents=True)

            outside_desktop = root / "outside.desktop"
            outside_launch = root / "outside_launch.sh"
            outside_shortcut = root / "outside_shortcut.desktop"
            outside_desktop.write_text("keep", encoding="utf-8")
            outside_launch.write_text("keep", encoding="utf-8")
            outside_shortcut.write_text("keep", encoding="utf-8")

            game = StandaloneGame(
                name="LegitGame",
                game_dir=game_dir,
                exe_path=game_dir / "game.exe",
                prefix_dir=root / "Games" / ".prefixes" / "LegitGame",
                proton_name="Proton",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=outside_launch,
                desktop_file=outside_desktop,
                desktop_shortcut=outside_shortcut,
            )

            with (
                mock.patch("deckinstall.standalone.APPLICATIONS_DIR", apps_dir),
                mock.patch("deckinstall.standalone.get_desktop_dir", return_value=desktop_dir),
            ):
                remove_standalone_game(game, delete_files=False, delete_prefix=False)

            self.assertTrue(outside_desktop.exists())
            self.assertTrue(outside_launch.exists())
            self.assertTrue(outside_shortcut.exists())

    def test_find_cleanup_candidates_protects_keep_in_place_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "home"
            steam_root = root / "Steam"
            (steam_root / "userdata" / "1" / "config").mkdir(parents=True)
            (steam_root / "steamapps" / "compatdata").mkdir(parents=True)
            paths = SteamPaths(
                steam_root=steam_root,
                user_id="1",
                userdata_dir=steam_root / "userdata" / "1",
                shortcuts_vdf=steam_root / "userdata" / "1" / "config" / "shortcuts.vdf",
                grid_dir=steam_root / "userdata" / "1" / "config" / "grid",
                config_vdf=steam_root / "config" / "config.vdf",
                compatdata_dir=steam_root / "steamapps" / "compatdata",
            )

            games_dir = home_dir / "Games"
            prefixes_dir = games_dir / ".prefixes"
            keep_game_dir = home_dir / "ExternalDrive" / "KeepGame"
            keep_game_dir.mkdir(parents=True)
            keep_prefix = prefixes_dir / "KeepGame"
            (keep_prefix / "drive_c" / "users").mkdir(parents=True)

            (keep_game_dir / ".deckinstall_standalone.json").write_text(
                json.dumps(
                    {
                        "name": "KeepGame",
                        "exe_path": str(keep_game_dir / "game.exe"),
                        "prefix_dir": str(keep_prefix),
                    }
                ),
                encoding="utf-8",
            )
            apps_dir = home_dir / ".local" / "share" / "applications"
            apps_dir.mkdir(parents=True)
            (apps_dir / "deckinstall-keepgame.desktop").write_text(
                f"[Desktop Entry]\nName=KeepGame\nPath={keep_game_dir}\n",
                encoding="utf-8",
            )

            orphan_prefix = prefixes_dir / "OrphanGame"
            (orphan_prefix / "drive_c" / "users").mkdir(parents=True)

            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(home_dir / ".local" / "share")}):
                candidates = find_cleanup_candidates(
                    paths,
                    extra_external_roots=[prefixes_dir],
                    home_dir=home_dir,
                )
            candidate_paths = {c.path.resolve() for c in candidates}
            self.assertNotIn(keep_prefix.resolve(), candidate_paths)
            self.assertIn(orphan_prefix.resolve(), candidate_paths)

    def test_optiscaler_moved_folder_uninstall_and_traversal_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_dir = root / "payload"
            _make_optiscaler_payload(payload_dir)

            old_dir = root / "Downloads" / "MyGame"
            old_exe = old_dir / "MyGame.exe"
            _make_dummy_pe(old_exe)
            (old_dir / "dxgi.dll").write_bytes(b"ORIG_DXGI")

            install_optiscaler_for_game(
                old_exe,
                proxy_dll="dxgi",
                overwrite_existing=True,
                payload_dir=payload_dir,
            )
            self.assertTrue((old_dir / "dxgi.dll.deckinstall.bak").is_file())

            # Simulate moving the game folder to ~/Games/MyGame
            new_dir = root / "Games" / "MyGame"
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_dir), str(new_dir))
            new_exe = new_dir / "MyGame.exe"

            res = uninstall_optiscaler_for_game(new_exe)
            self.assertGreaterEqual(res.removed_count, 2)
            self.assertEqual(res.restored_count, 1)
            self.assertEqual((new_dir / "dxgi.dll").read_bytes(), b"ORIG_DXGI")
            self.assertFalse((new_dir / OPTISCALER_MANIFEST).exists())

            # Manifest path traversal rejection
            outside_file = root / "outside_secret.txt"
            outside_file.write_text("do-not-delete", encoding="utf-8")
            (new_dir / OPTISCALER_MANIFEST).write_text(
                json.dumps(
                    {
                        "proxy_dll": "dxgi",
                        "backend": "auto",
                        "version": "1.0",
                        "installed_files": ["../../outside_secret.txt", str(outside_file)],
                        "backed_up_files": ["../outside_bak.dll"],
                    }
                ),
                encoding="utf-8",
            )
            state = inspect_optiscaler_state(new_exe)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.installed_files, [])
            self.assertEqual(state.backed_up_files, [])
            uninstall_optiscaler_for_game(new_exe)
            self.assertTrue(outside_file.exists())

    def test_save_sync_pull_rejects_sha256_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_pfx = root / "src_pfx"
            dst_pfx = root / "dst_pfx"
            sync_root = root / "sync"
            snaps_root = root / "snaps"

            src_save = src_pfx / "drive_c" / "users" / "deck" / "Documents" / "save.dat"
            src_save.parent.mkdir(parents=True)
            src_save.write_bytes(b"REMOTE")

            dst_save = dst_pfx / "drive_c" / "users" / "deck" / "Documents" / "save.dat"
            dst_save.parent.mkdir(parents=True)
            dst_save.write_bytes(b"LOCAL_SAFE")

            pushed_zip = push_game_saves("TamperGame", src_pfx, sync_root)
            self.assertIsNotNone(pushed_zip)
            assert pushed_zip is not None

            meta_file = pushed_zip.parent / "meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["sha256"] = "0" * 64
            meta_file.write_text(json.dumps(meta), encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                pull_game_saves("TamperGame", dst_pfx, sync_root, snapshots_root=snaps_root)
            self.assertIn("checksum mismatch", str(ctx.exception))
            self.assertEqual(dst_save.read_bytes(), b"LOCAL_SAFE")

    def test_format_launch_token_quotes_shell_metacharacters_with_quotes(self) -> None:
        tricky_tokens = [
            'foo"bar;rm -rf /',
            'a|b"c',
            "x&y'z",
            'MY_VAR="val";id',
        ]
        for tok in tricky_tokens:
            formatted = _format_launch_token(tok)
            if "=" in tok and tok.split("=", 1)[0].isidentifier():
                val_part = formatted.split("=", 1)[1]
                self.assertEqual(shlex.split(val_part), [tok.split("=", 1)[1]])
            else:
                self.assertEqual(shlex.split(formatted), [tok])

    def test_list_ge_proton10_dirs_and_ensure_ge_proton10(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            compat_root = Path(tmp) / "compatibilitytools.d"
            for name in ("GE-Proton9-20", "GE-Proton10-5", "GE-Proton10-20", "GE-Proton11-1"):
                d = compat_root / name
                d.mkdir(parents=True)
                (d / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                mock.patch("deckinstall.runtime_manager.get_compat_tools_dir", return_value=compat_root),
                mock.patch("deckinstall.steam_paths.find_steam_root", return_value=None),
                mock.patch("deckinstall.runtime_manager._iter_extra_compat_tool_dirs", return_value=[]),
            ):
                dirs = list_ge_proton10_dirs()
                self.assertEqual([p.name for p in dirs], ["GE-Proton10-20", "GE-Proton10-5"])
                with mock.patch("deckinstall.runtime_manager.fetch_available_proton_releases") as mock_fetch:
                    chosen = ensure_ge_proton10(force_update=False)
                mock_fetch.assert_not_called()
                self.assertEqual(chosen.name, "GE-Proton10-20")

            # Online mocked fetch when no GE-Proton10 is installed locally
            empty_compat = Path(tmp) / "empty_compat"
            empty_compat.mkdir()
            releases = [
                ProtonRelease(
                    family_id="ge-proton",
                    tag_name="GE-Proton11-1",
                    name="GE-Proton11-1",
                    asset_name="GE-Proton11-1.tar.gz",
                    download_url="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-1/GE-Proton11-1.tar.gz",
                    size_bytes=100,
                    published_at="2026-01-01T00:00:00Z",
                    prerelease=False,
                ),
                ProtonRelease(
                    family_id="ge-proton",
                    tag_name="GE-Proton10-25",
                    name="GE-Proton10-25",
                    asset_name="GE-Proton10-25.tar.gz",
                    download_url="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton10-25/GE-Proton10-25.tar.gz",
                    size_bytes=100,
                    published_at="2026-01-01T00:00:00Z",
                    prerelease=False,
                ),
            ]
            installed_target = empty_compat / "GE-Proton10-25"
            with (
                mock.patch("deckinstall.runtime_manager.get_compat_tools_dir", return_value=empty_compat),
                mock.patch("deckinstall.steam_paths.find_steam_root", return_value=None),
                mock.patch("deckinstall.runtime_manager._iter_extra_compat_tool_dirs", return_value=[]),
                mock.patch("deckinstall.runtime_manager.fetch_available_proton_releases", return_value=releases),
                mock.patch("deckinstall.runtime_manager.install_compat_tool", return_value=installed_target) as mock_install,
            ):
                res = ensure_ge_proton10()
            self.assertEqual(res, installed_target)
            mock_install.assert_called_once()
            self.assertEqual(mock_install.call_args[0][0].tag_name, "GE-Proton10-25")

    def test_scan_all_games_empty_roots_and_run_media_discovery(self) -> None:
        self.assertEqual(scan_all_games(roots=[]), [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            fake_home.mkdir()
            fake_media = root / "run" / "media"
            device_dir = fake_media / "mmcblk0p1"
            game_dir = device_dir / "CoolMediaGame"
            _make_dummy_pe(game_dir / "CoolMediaGame.exe")

            real_path = Path

            class _PatchedPath:
                def __new__(cls, *args, **kwargs):
                    if args == ("/run/media",):
                        return fake_media
                    return real_path(*args, **kwargs)

                @staticmethod
                def home():
                    return fake_home

            with mock.patch("deckinstall.scanner.Path", _PatchedPath):
                roots = default_scan_roots()
                self.assertIn(device_dir, roots)
                found = scan_all_games()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].root_dir.resolve(), game_dir.resolve())

    def test_i18n_keys_and_placeholders_parity(self) -> None:
        locales_dir = Path(__file__).resolve().parent.parent / "src" / "deckinstall" / "locales"
        en = json.loads((locales_dir / "en.json").read_text(encoding="utf-8"))
        pt = json.loads((locales_dir / "pt_BR.json").read_text(encoding="utf-8"))

        self.assertEqual(set(en.keys()), set(pt.keys()))
        placeholder_re = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for key in sorted(en.keys()):
            en_ph = set(placeholder_re.findall(str(en[key])))
            pt_ph = set(placeholder_re.findall(str(pt[key])))
            self.assertEqual(en_ph, pt_ph, f"Placeholder mismatch for i18n key {key!r}")

    def test_is_non_game_helper_exe_filtering(self) -> None:
        self.assertTrue(_is_non_game_helper_exe(Path("unins000.exe")))
        self.assertTrue(_is_non_game_helper_exe(Path("vcredist_x64.exe")))
        self.assertTrue(_is_non_game_helper_exe(Path("dxsetup.exe")))
        self.assertFalse(_is_non_game_helper_exe(Path("Game.exe")))

    def test_is_win32_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win32_pfx = root / "win32_pfx"
            win32_pfx.mkdir()
            (win32_pfx / "system.reg").write_text(
                "WINE REGISTRY Version 2\n#arch=win32\n",
                encoding="utf-8",
            )

            win64_pfx = root / "win64_pfx"
            win64_pfx.mkdir()
            (win64_pfx / "system.reg").write_text(
                "WINE REGISTRY Version 2\n#arch=win64\n",
                encoding="utf-8",
            )

            missing_pfx = root / "missing_pfx"
            missing_pfx.mkdir()

            self.assertTrue(is_win32_prefix(win32_pfx))
            self.assertFalse(is_win32_prefix(win64_pfx))
            self.assertFalse(is_win32_prefix(missing_pfx))

    def test_run_exe_in_prefix_invokes_umu_and_subprocess_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "game"
            game_dir.mkdir()
            exe_path = game_dir / "config.exe"
            _make_dummy_pe(exe_path)
            prefix_dir = root / "pfx"
            proton_dir = root / "GE-Proton10-25"
            proton_dir.mkdir()
            fake_umu = Path("/usr/bin/umu-run")
            fake_proc = mock.Mock(returncode=42)

            with (
                mock.patch("deckinstall.runtime_manager.ensure_umu_run", return_value=fake_umu) as mock_umu,
                mock.patch("deckinstall.runtime_manager.subprocess.run", return_value=fake_proc) as mock_run,
            ):
                rc = run_exe_in_prefix(
                    exe_path=exe_path,
                    prefix_dir=prefix_dir,
                    proton_path=proton_dir,
                    game_id="umu-1234",
                    wait=True,
                )

            self.assertEqual(rc, 42)
            self.assertTrue(prefix_dir.is_dir())
            mock_umu.assert_called_once_with()
            mock_run.assert_called_once()
            call_args, call_kwargs = mock_run.call_args
            self.assertEqual(call_args[0], [str(fake_umu), str(exe_path)])
            self.assertEqual(call_kwargs["cwd"], str(game_dir))
            self.assertFalse(call_kwargs["check"])
            self.assertEqual(call_kwargs["env"]["WINEPREFIX"], str(prefix_dir))
            self.assertEqual(call_kwargs["env"]["PROTONPATH"], str(proton_dir))
            self.assertEqual(call_kwargs["env"]["GAMEID"], "umu-1234")

    def test_resolve_standalone_proton_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ge11_dir = root / "GE-Proton11-1"
            ge10_dir = root / "GE-Proton10-25"
            ge11_dir.mkdir()
            ge10_dir.mkdir()

            game = StandaloneGame(
                name="TestGame",
                game_dir=root / "Games" / "TestGame",
                exe_path=root / "Games" / "TestGame" / "Game.exe",
                prefix_dir=root / "Games" / ".prefixes" / "testgame",
                proton_name="GE-Proton9-99",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=root / "launch.sh",
                desktop_file=root / "testgame.desktop",
            )

            with (
                mock.patch("deckinstall.standalone._resolve_proton_path", return_value="GE-Proton9-99"),
                mock.patch("deckinstall.runtime_manager.list_ge_proton11_dirs", return_value=[ge11_dir]),
                mock.patch("deckinstall.runtime_manager.list_ge_proton10_dirs", return_value=[ge10_dir]),
            ):
                resolved_path, fallback_name = resolve_standalone_proton_fallback(game)
            self.assertEqual(resolved_path, ge11_dir)
            self.assertEqual(fallback_name, "GE-Proton11-1")

            with (
                mock.patch("deckinstall.standalone._resolve_proton_path", return_value="GE-Proton9-99"),
                mock.patch("deckinstall.runtime_manager.list_ge_proton11_dirs", return_value=[]),
                mock.patch("deckinstall.runtime_manager.list_ge_proton10_dirs", return_value=[ge10_dir]),
            ):
                resolved_path, fallback_name = resolve_standalone_proton_fallback(game)
            self.assertEqual(resolved_path, ge10_dir)
            self.assertEqual(fallback_name, "GE-Proton10-25")

            with (
                mock.patch("deckinstall.standalone._resolve_proton_path", return_value="GE-Proton9-99"),
                mock.patch("deckinstall.runtime_manager.list_ge_proton11_dirs", return_value=[]),
                mock.patch("deckinstall.runtime_manager.list_ge_proton10_dirs", return_value=[]),
            ):
                resolved_path, fallback_name = resolve_standalone_proton_fallback(game)
            self.assertIsNone(resolved_path)
            self.assertIsNone(fallback_name)


class Phase1SecurityAndCoreTests(unittest.TestCase):
    def test_extractor_precheck_tar_and_unzip_rejects_links_and_traversal(self) -> None:
        from deckinstall import extractor

        with tempfile.TemporaryDirectory() as tmp:
            arc = Path(tmp) / "fake.tar.zst"
            arc.write_bytes(b"dummy")

            sym_proc = mock.Mock(
                returncode=0,
                stdout="lrwxrwxrwx user/user 0 2026-01-01 00:00 sub -> /etc/passwd\n",
                stderr="",
            )
            with mock.patch("deckinstall.extractor.subprocess.run", return_value=sym_proc):
                with self.assertRaises(RuntimeError):
                    extractor._precheck_cli_archive("/usr/bin/tar", arc)

            trav_proc = mock.Mock(
                returncode=0,
                stdout="-rw-r--r-- user/user 10 2026-01-01 00:00 ../evil.txt\n",
                stderr="",
            )
            with mock.patch("deckinstall.extractor.subprocess.run", return_value=trav_proc):
                with self.assertRaises(RuntimeError):
                    extractor._precheck_cli_archive("/usr/bin/tar", arc)

            unzip_proc = mock.Mock(returncode=0, stdout="../evil.txt\n", stderr="")
            with mock.patch("deckinstall.extractor.subprocess.run", return_value=unzip_proc):
                with self.assertRaises(RuntimeError):
                    extractor._precheck_cli_archive("/usr/bin/unzip", arc)

    def test_extractor_zip_entry_limit_and_staging_cleanup(self) -> None:
        import zipfile

        from deckinstall import extractor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zpath = root / "many.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("a.txt", "1")
                zf.writestr("b.txt", "2")
            dest = root / "out"
            with mock.patch.object(extractor, "_MAX_ARCHIVE_ENTRIES", 1):
                with self.assertRaises(RuntimeError):
                    extractor._extract_zip_safe(zpath, dest)

    def test_redirect_validation_blocks_non_github_host(self) -> None:
        from deckinstall import app_updater, runtime_manager

        redir_resp = mock.Mock()
        redir_resp.status_code = 302
        redir_resp.is_redirect = True
        redir_resp.headers = {"Location": "https://evil.example.com/payload.AppImage"}

        with mock.patch("deckinstall.app_updater.requests.get", return_value=redir_resp):
            with self.assertRaises(ValueError):
                app_updater._request_github_with_validated_redirects(
                    "https://github.com/owner/repo/releases/download/v1/app.AppImage",
                    headers={},
                    timeout=5,
                )

        with mock.patch("deckinstall.runtime_manager.requests.get", return_value=redir_resp):
            with self.assertRaises(ValueError):
                runtime_manager._request_github_with_validated_redirects(
                    "https://github.com/owner/repo/releases/latest",
                    headers={},
                    timeout=5,
                )

    def test_import_standalone_to_steam_passes_correct_resolve_icon_arg_order(self) -> None:
        from deckinstall.standalone import StandaloneGame, import_standalone_to_steam
        from deckinstall.steam_paths import SteamPaths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "MyGame"
            game_dir.mkdir()
            exe_path = game_dir / "game.exe"
            exe_path.write_bytes(b"MZ")
            game = StandaloneGame(
                name="MyGame",
                game_dir=game_dir,
                exe_path=exe_path,
                prefix_dir=root / "pfx",
                native_linux=False,
                proton_name="GE-Proton11-1",
                steam_appid=None,
                winedlloverrides="",
                launch_options="",
                launch_script=game_dir / "launch.sh",
                desktop_file=root / "mygame.desktop",
            )
            paths = SteamPaths(
                steam_root=root,
                user_id="123",
                userdata_dir=root,
                shortcuts_vdf=root / "shortcuts.vdf",
                config_vdf=root / "config.vdf",
                grid_dir=root / "grid",
                compatdata_dir=root / "compatdata",
            )
            with (
                mock.patch("deckinstall.standalone.resolve_game_icon", return_value=None) as m_icon,
                mock.patch("deckinstall.standalone.add_or_update_shortcut", return_value=999),
                mock.patch("deckinstall.standalone.set_shortcut_proton"),
            ):
                import_standalone_to_steam(paths, game)
            m_icon.assert_called_once_with(exe_path, game_dir, steam_appid=None)

    def test_steam_vdf_hardening_shortcuts_config_sd_card_and_quotes(self) -> None:
        from deckinstall import steam_vdf

        self.assertEqual(steam_vdf._format_launch_token('--opt="a b"'), '--opt="a b"')
        self.assertEqual(steam_vdf._format_launch_token('FOO="bar\\"baz"'), 'FOO="bar\\"baz"')

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrupt = root / "shortcuts.vdf"
            corrupt.write_bytes(b"\xff\xfe\x00corrupt")
            self.assertEqual(steam_vdf.load_shortcuts(corrupt), [])

            cfg = root / "config.vdf"
            cfg.write_text(
                '"InstallConfigStore"\n{\n\t"software"\n\t{\n\t\t"valve"\n\t\t{\n\t\t\t"steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n',
                encoding="utf-8",
            )
            steam_vdf.set_compat_tool(cfg, 12345, "GE-Proton11-1")
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("12345", text)
            self.assertEqual(text.lower().count('"software"'), 1)
            steam_vdf.remove_compat_tool(cfg, 12345)
            self.assertNotIn("12345", cfg.read_text(encoding="utf-8"))

        sd_exe = Path("/run/media/deck/SDCard/MyGame/Binaries/Win64/Game.exe")
        self.assertEqual(steam_vdf.infer_game_root_from_exe(sd_exe), Path("/run/media/deck/SDCard/MyGame"))
        with self.assertRaises(ValueError):
            steam_vdf.infer_game_root_from_exe(Path("/run/media/deck/SDCard/Game.exe"))

    def test_pull_game_saves_restores_mtime_and_requires_metadata(self) -> None:
        import json
        import zipfile

        from deckinstall import save_sync

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pfx = root / "pfx"
            sync_root = root / "sync"
            game_sync = sync_root / "mygame"
            game_sync.mkdir(parents=True)
            zpath = game_sync / "latest.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("users/steamuser/AppData/Local/MyGame/save.dat", b"SAVE1")

            with self.assertRaises(RuntimeError):
                save_sync.pull_game_saves("MyGame", pfx, sync_root, snapshots_root=root / "snaps")

            sha = file_sha256(zpath)
            target_ts = 1700000123.0
            (game_sync / "meta.json").write_text(
                json.dumps({"sha256": sha, "updated_at": target_ts}),
                encoding="utf-8",
            )
            count, _ = save_sync.pull_game_saves("MyGame", pfx, sync_root, snapshots_root=root / "snaps")
            self.assertEqual(count, 1)
            restored = pfx / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "MyGame" / "save.dat"
            self.assertAlmostEqual(restored.stat().st_mtime, target_ts, delta=1.0)

    def test_apply_launch_presets_keeps_lsfg_vk_before_command_and_preserves_winedlloverrides_when_none(
        self,
    ) -> None:
        from deckinstall import steam_vdf

        res1 = steam_vdf.apply_launch_presets(
            "lsfg-vk --flag",
            gamemode=False,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            winedlloverrides=None,
        )
        self.assertEqual(res1, "lsfg-vk %command% --flag")

        res2 = steam_vdf.apply_launch_presets(
            "WINEDLLOVERRIDES=dxgi=n,b lsfg-vk %command% --flag",
            gamemode=True,
            mangohud=False,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
            winedlloverrides=None,
        )
        tokens = res2.split()
        cmd_idx = tokens.index("%command%")
        self.assertIn("WINEDLLOVERRIDES=dxgi=n,b", tokens[:cmd_idx])
        self.assertIn("lsfg-vk", tokens[:cmd_idx])
        self.assertIn("gamemoderun", tokens[:cmd_idx])
        self.assertEqual(tokens[cmd_idx + 1 :], ["--flag"])

    def test_file_lock_rejects_symlinked_lockfile(self) -> None:
        from deckinstall import steam_vdf

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_lock = root / "real.lock"
            real_lock.write_text("", encoding="utf-8")
            sym_lock = root / "sym.lock"
            sym_lock.symlink_to(real_lock)

            with self.assertRaisesRegex(RuntimeError, "Refusing symlink lockfile"):
                with steam_vdf._file_lock(sym_lock):
                    pass

    def test_push_game_saves_allows_exact_max_entries_and_rejects_excess(self) -> None:
        from unittest.mock import patch

        from deckinstall import save_sync

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pfx = root / "pfx"
            save_dir = pfx / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "MyGame"
            save_dir.mkdir(parents=True)
            (save_dir / "s1.dat").write_bytes(b"ONE")
            (save_dir / "s2.dat").write_bytes(b"TWO")
            sync_root = root / "sync"

            with patch.object(save_sync, "_RESTORE_MAX_ENTRIES", 2):
                out_zip = save_sync.push_game_saves("MyGame", pfx, sync_root)
                self.assertIsNotNone(out_zip)
                self.assertTrue(out_zip.is_file())

                (save_dir / "s3.dat").write_bytes(b"THREE")
                with self.assertRaisesRegex(RuntimeError, "maximum entry count"):
                    save_sync.push_game_saves("MyGame", pfx, sync_root)


if __name__ == "__main__":
    unittest.main()
