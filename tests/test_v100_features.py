"""Unit tests for DeckInstall v1.0.0 features."""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    import vdf  # type: ignore  # noqa: F401
except ImportError:
    fake_vdf = types.ModuleType("vdf")
    fake_vdf.binary_loads = lambda b: {"shortcuts": {}}
    fake_vdf.binary_dumps = lambda d: b"VDF"
    fake_vdf.loads = lambda s: {"InstallConfigStore": {"Software": {"Valve": {"Steam": {"CompatToolMapping": {}}}}}}
    fake_vdf.dumps = lambda d, pretty=True: "{}"
    sys.modules["vdf"] = fake_vdf

from deckinstall.launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from deckinstall.runtime_manager import (
    list_installed_compat_tools,
    remove_compat_tool,
    resolve_game_prefix,
)
from deckinstall.save_sync import (
    plan_sync_all,
    pull_game_saves,
    push_game_saves,
    snapshot_prefix_saves,
)
from deckinstall.standalone import install_standalone_game


def _make_prefix_with_save(
    prefix_dir: Path,
    rel_subpath: str = "drive_c/users/steamuser/Documents/MyGame/save.dat",
    content: bytes = b"save-data-v1",
) -> Path:
    save_file = prefix_dir / rel_subpath
    save_file.parent.mkdir(parents=True, exist_ok=True)
    save_file.write_bytes(content)
    return save_file


class SaveSyncTests(unittest.TestCase):
    def test_snapshot_rotation_keeps_max_keep(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            snaps = root / "snapshots"
            _make_prefix_with_save(prefix, content=b"save-content")

            created: list[Path] = []
            for idx in range(5):
                snap = snapshot_prefix_saves(
                    prefix,
                    "Test Game",
                    f"op-{idx}",
                    dest_root=snaps,
                    max_keep=3,
                )
                self.assertIsNotNone(snap)
                assert snap is not None
                created.append(snap)
                # Ensure distinct mtimes for deterministic rotation order.
                os.utime(snap, (1000 + idx, 1000 + idx))

            game_snap_dir = snaps / "test-game"
            remaining = sorted(game_snap_dir.glob("*.zip"))
            self.assertEqual(len(remaining), 3)
            self.assertEqual(set(remaining), set(created[-3:]))

    def test_push_and_pull_with_pre_pull_snapshot_and_traversal_block(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_prefix = root / "src_prefix"
            dst_prefix = root / "dst_prefix"
            sync_root = root / "sync"
            snaps_root = root / "snaps"

            _make_prefix_with_save(src_prefix, content=b"remote-save-payload")
            local_save = _make_prefix_with_save(dst_prefix, content=b"local-old-payload")

            pushed_zip = push_game_saves("Cool Game", src_prefix, sync_root)
            self.assertIsNotNone(pushed_zip)
            assert pushed_zip is not None
            self.assertTrue(pushed_zip.is_file())
            self.assertTrue((pushed_zip.parent / "meta.json").is_file())

            # Inject malicious path-traversal and symlink entries into the sync archive.
            with zipfile.ZipFile(pushed_zip, "a") as zf:
                zf.writestr("save/users/../../escaped.txt", b"pwned")
                zf.writestr("save/other_section/bad.txt", b"ignored")

            restored, snap_path = pull_game_saves(
                "Cool Game",
                dst_prefix,
                sync_root,
                snapshots_root=snaps_root,
            )
            self.assertEqual(restored, 1)
            self.assertIsNotNone(snap_path)
            assert snap_path is not None
            self.assertTrue(snap_path.is_file())
            self.assertEqual(local_save.read_bytes(), b"remote-save-payload")
            self.assertFalse((root / "escaped.txt").exists())
            self.assertFalse((dst_prefix / "drive_c" / "escaped.txt").exists())

    def test_plan_sync_all_states(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_root = root / "sync"

            # 1) PUSH: local saves only
            p_push = root / "pfx_push"
            _make_prefix_with_save(p_push, content=b"push-me")

            # 2) PULL: remote saves only
            p_pull_src = root / "pfx_pull_src"
            _make_prefix_with_save(p_pull_src, content=b"pull-me")
            push_game_saves("Pull Game", p_pull_src, sync_root)
            p_pull_dst = root / "pfx_pull_dst"
            p_pull_dst.mkdir(parents=True, exist_ok=True)

            # 3) IN_SYNC: identical digest
            p_sync = root / "pfx_insync"
            _make_prefix_with_save(p_sync, content=b"synced-data")
            push_game_saves("Sync Game", p_sync, sync_root)

            # 4) CONFLICT: different digest and missing/unreadable meta.json
            p_conflict = root / "pfx_conflict"
            _make_prefix_with_save(p_conflict, content=b"local-conflict")
            push_game_saves("Conflict Game", p_conflict, sync_root)
            (sync_root / "conflict-game" / "meta.json").unlink()

            plan = plan_sync_all(
                [
                    ("Push Game", p_push),
                    ("Pull Game", p_pull_dst),
                    ("Sync Game", p_sync),
                    ("Conflict Game", p_conflict),
                ],
                sync_root,
            )
            by_name = {item.game_name: item.action for item in plan}
            self.assertEqual(by_name["Push Game"], "PUSH")
            self.assertEqual(by_name["Pull Game"], "PULL")
            self.assertEqual(by_name["Sync Game"], "IN_SYNC")
            self.assertEqual(by_name["Conflict Game"], "CONFLICT")

            # Also verify CONFLICT when local and remote differ with identical timestamps.
            p_ts = root / "pfx_ts"
            ts_file = _make_prefix_with_save(p_ts, content=b"v1")
            push_game_saves("TS Game", p_ts, sync_root)
            meta_path = sync_root / "ts-game" / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ts_file.write_bytes(b"v2-modified")
            os.utime(ts_file, (meta["updated_at"], meta["updated_at"]))
            ts_plan = plan_sync_all([("TS Game", p_ts)], sync_root)
            self.assertEqual(ts_plan[0].action, "CONFLICT")


class LauncherScanTests(unittest.TestCase):
    def test_scan_external_launcher_games_lutris_heroic_bottles(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)

            # --- Lutris ---
            lutris_game_dir = home / "Games" / "LutrisGame"
            lutris_game_dir.mkdir(parents=True)
            lutris_exe = lutris_game_dir / "game.exe"
            lutris_exe.write_bytes(b"MZ")
            lutris_pfx = home / "Prefixes" / "lutris_pfx"
            lutris_pfx.mkdir(parents=True)
            lutris_yml_dir = home / ".config" / "lutris" / "games"
            lutris_yml_dir.mkdir(parents=True)
            (lutris_yml_dir / "lutris-game-123.yml").write_text(
                f"name: Lutris Quest\n"
                f"runner: wine\n"
                f"game:\n"
                f"  exe: {lutris_exe}\n"
                f"  prefix: {lutris_pfx}\n",
                encoding="utf-8",
            )

            # --- Heroic (Legendary) ---
            heroic_dir = home / "Games" / "HeroicGame"
            heroic_dir.mkdir(parents=True)
            heroic_exe = heroic_dir / "heroic.exe"
            heroic_exe.write_bytes(b"MZ")
            heroic_pfx = home / "Prefixes" / "heroic_pfx"
            heroic_pfx.mkdir(parents=True)
            heroic_cfg = home / ".config" / "heroic"
            (heroic_cfg / "legendaryConfig" / "legendary").mkdir(parents=True)
            (heroic_cfg / "GamesConfig").mkdir(parents=True)
            (heroic_cfg / "legendaryConfig" / "legendary" / "installed.json").write_text(
                json.dumps(
                    {
                        "app123": {
                            "title": "Heroic Adventure",
                            "install_path": str(heroic_dir),
                            "executable": "heroic.exe",
                            "platform": "Windows",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (heroic_cfg / "GamesConfig" / "app123.json").write_text(
                json.dumps({"app123": {"winePrefix": str(heroic_pfx)}}),
                encoding="utf-8",
            )

            # --- Bottles ---
            bottle_dir = home / ".local" / "share" / "bottles" / "bottles" / "MyBottle"
            bottle_prog_dir = bottle_dir / "drive_c" / "Program Files" / "BottleApp"
            bottle_prog_dir.mkdir(parents=True)
            bottle_exe = bottle_prog_dir / "app.exe"
            bottle_exe.write_bytes(b"MZ")
            (bottle_dir / "bottle.yml").write_text(
                "Name: MyBottle\n"
                "External_Programs:\n"
                "  prog1:\n"
                "    name: Bottle Runner\n"
                "    path: C:\\Program Files\\BottleApp\\app.exe\n",
                encoding="utf-8",
            )

            found = scan_external_launcher_games(home_dir=home)
            by_source = {g.source: g for g in found}
            self.assertIn("lutris", by_source)
            self.assertIn("heroic", by_source)
            self.assertIn("bottles", by_source)

            self.assertEqual(by_source["lutris"].name, "Lutris Quest")
            self.assertEqual(by_source["lutris"].exe_path, lutris_exe)
            self.assertEqual(by_source["lutris"].prefix_dir, lutris_pfx)

            self.assertEqual(by_source["heroic"].name, "Heroic Adventure")
            self.assertEqual(by_source["heroic"].exe_path, heroic_exe)
            self.assertEqual(by_source["heroic"].prefix_dir, heroic_pfx)

            self.assertEqual(by_source["bottles"].name, "Bottle Runner")
            self.assertEqual(by_source["bottles"].exe_path, bottle_exe)
            self.assertEqual(by_source["bottles"].prefix_dir, bottle_dir)


class RuntimeManagerV100Tests(unittest.TestCase):
    def test_list_installed_compat_tools_and_remove_guards(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            compat_dir = root / "compatibilitytools.d"
            tool_dir = compat_dir / "GE-Proton11-2"
            tool_dir.mkdir(parents=True)
            (tool_dir / "compatibilitytool.vdf").write_text(
                '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "GE-Proton11-2"\n    {\n      "display_name" "GE-Proton11-2"\n    }\n  }\n}\n',
                encoding="utf-8",
            )
            (tool_dir / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                patch("deckinstall.runtime_manager.get_compat_tools_dir", return_value=compat_dir),
                patch("deckinstall.runtime_manager._iter_extra_compat_tool_dirs", return_value=[]),
                patch("deckinstall.steam_paths.find_steam_root", return_value=None),
            ):
                tools = list_installed_compat_tools()
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0].folder_name, "GE-Proton11-2")
                self.assertEqual(tools[0].tool_id, "GE-Proton11-2")

            # Symlink guard
            link_path = compat_dir / "GE-Link"
            link_path.symlink_to(tool_dir)
            with self.assertRaises(ValueError):
                remove_compat_tool(link_path)

            # Outside compatibilitytools.d guard
            outside = root / "not_compat"
            outside.mkdir()
            with self.assertRaises(ValueError):
                remove_compat_tool(outside)

            # Valid removal
            remove_compat_tool(tool_dir)
            self.assertFalse(tool_dir.exists())

    def test_resolve_game_prefix_external(self) -> None:
        with TemporaryDirectory() as tmp:
            pfx = Path(tmp) / "ext_pfx"
            (pfx / "pfx").mkdir(parents=True)
            ext = ExternalLauncherGame(
                source="lutris",
                name="Ext",
                exe_path=Path(tmp) / "game.exe",
                game_dir=Path(tmp),
                prefix_dir=pfx,
                runner="wine",
            )
            resolved_pfx, _ = resolve_game_prefix("external", ext, paths=None)
            self.assertEqual(resolved_pfx, pfx / "pfx")


class StandaloneDesktopWMClassTests(unittest.TestCase):
    def _install_and_read_desktop(
        self,
        tmp_path: Path,
        app_name: str,
        exe_name: str,
        steam_appid: int | None,
        native_linux: bool,
    ) -> str:
        games_dir = tmp_path / "Games"
        prefixes_dir = tmp_path / "Prefixes"
        apps_dir = tmp_path / "applications"
        desktop_dir = tmp_path / "Desktop"
        src_dir = tmp_path / f"src_{app_name}"
        src_dir.mkdir(parents=True, exist_ok=True)
        exe_path = src_dir / exe_name
        exe_path.write_bytes(b"\x7fELF" if native_linux else b"MZ")

        with (
            patch("deckinstall.standalone.get_games_dir", return_value=games_dir),
            patch("deckinstall.standalone.get_prefixes_dir", return_value=prefixes_dir),
            patch("deckinstall.standalone.APPLICATIONS_DIR", apps_dir),
            patch("deckinstall.standalone.get_desktop_dir", return_value=desktop_dir),
            patch("deckinstall.scanner.detect_exe_kind", return_value="linux_elf" if native_linux else "windows_pe"),
            patch("deckinstall.standalone.resolve_game_icon", return_value=None),
            patch("deckinstall.standalone.detect_steam_appid", return_value=steam_appid),
            patch("deckinstall.standalone.check_disk_space", return_value=None),
        ):
            game = install_standalone_game(
                game_name=app_name,
                game_dir=src_dir,
                exe_path=exe_path,
                steam_appid=steam_appid,
                create_menu_shortcut=True,
                create_desktop_shortcut=False,
                keep_in_place=True,
                native_linux=native_linux,
            )
            return game.desktop_file.read_text(encoding="utf-8")

    def test_startup_wm_class_variants(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            txt_steam = self._install_and_read_desktop(root, "AppID Game", "game.exe", 1145360, False)
            self.assertIn("StartupWMClass=steam_app_1145360\n", txt_steam)

            txt_zero = self._install_and_read_desktop(root, "NoAppID Game", "win.exe", None, False)
            self.assertIn("StartupWMClass=steam_app_0\n", txt_zero)

            txt_native = self._install_and_read_desktop(root, "Native Game", "native_bin.x86_64", None, True)
            self.assertIn("StartupWMClass=native_bin.x86_64\n", txt_native)


if __name__ == "__main__":
    unittest.main()
