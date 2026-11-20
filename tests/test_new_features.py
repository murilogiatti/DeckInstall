"""Tests for newly added DeckInstall features."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deckinstall.cleanup import delete_cleanup_candidates_checked, find_cleanup_candidates
from deckinstall.runtime_manager import (
    get_umu_protonfix_status,
    lookup_umu_database,
    validate_ge_proton_integrity,
)
from deckinstall.save_sync import (
    _extract_registry_saves,
    _restore_registry_saves,
    load_ludusavi_manifest,
    preview_sync_operation,
    push_game_saves,
    restore_prefix_state_snapshot,
    snapshot_prefix_state,
)
from deckinstall.scanner import scan_game_folder
from deckinstall.standalone import (
    find_orphan_shader_caches,
    install_standalone_game,
    resolve_umu_game_id,
)
from deckinstall.steam_paths import SteamPaths, dir_size_bytes
from deckinstall.steam_vdf import (
    apply_launch_presets,
    migrate_compatdata_to_microsd,
    restore_compatdata_from_microsd,
)


def _make_steam_paths(tmp_path: Path) -> SteamPaths:
    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "123456"
    (userdata / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=steam_root,
        user_id="123456",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )


class TestNewFeatures(unittest.TestCase):
    """Tests for newly added DeckInstall features."""

    # 1. umu-database.csv lookup, resolve_umu_game_id, get_umu_protonfix_status
    def test_umu_database_lookup_resolve_and_protonfix_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_csv = tmp_path / "umu-database.csv"
            db_csv.write_text(
                "TITLE,STORE,CODENAME,UMU_ID,COMMON_REDIST,NOTE\n"
                "Cyberpunk 2077,gog,cyberpunk2077,umu-1091500,vcrun2019,Works OOTB\n"
                "Cyberpunk 2077,egs,cyberpunk2077_egs,umu-1091500,,EGS build\n"
                "Hades II,steam,hades2,umu-1145350,,\n",
                encoding="utf-8",
            )

            hit = lookup_umu_database(game_title="Cyberpunk 2077", store="gog", db_path=db_csv)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.umu_id, "umu-1091500")
            self.assertEqual(hit.store, "gog")
            self.assertEqual(hit.common_redist, "vcrun2019")
            self.assertEqual(hit.match_source, "title")

            exe_hit = lookup_umu_database(
                exe_path=tmp_path / "Games" / "Hades II" / "hades2.exe",
                db_path=db_csv,
            )
            self.assertIsNotNone(exe_hit)
            assert exe_hit is not None
            self.assertEqual(exe_hit.umu_id, "umu-1145350")
            self.assertEqual(exe_hit.match_source, "exe_stem")

            # resolve_umu_game_id prefers positive steam_appid, otherwise falls back to umu-database
            self.assertEqual(resolve_umu_game_id(1091500, store="steam", db_path=db_csv), ("umu-1091500", "steam"))
            self.assertEqual(
                resolve_umu_game_id(None, store="gog", game_title="Cyberpunk 2077", db_path=db_csv),
                ("umu-1091500", "gog"),
            )
            self.assertEqual(
                resolve_umu_game_id(None, game_title="Unknown Game", db_path=db_csv),
                ("umu-default", "none"),
            )

            status = get_umu_protonfix_status("umu-1091500", store="gog", db_path=db_csv)
            self.assertTrue(status["found"])
            self.assertTrue(status["has_protonfix"])
            self.assertEqual(status["umu_id"], "umu-1091500")
            self.assertEqual(status["common_redist"], "vcrun2019")
            self.assertTrue(status["db_cached"])

            missing_status = get_umu_protonfix_status("umu-default", db_path=db_csv)
            self.assertFalse(missing_status["found"])
            self.assertFalse(missing_status["has_protonfix"])

    # 2. validate_ge_proton_integrity
    def test_validate_ge_proton_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            valid_dir = tmp_path / "GE-Proton9-10"
            (valid_dir / "files" / "bin").mkdir(parents=True)
            for rel in ("proton", "compatibilitytool.vdf", "toolmanifest.vdf", "files/bin/wine"):
                (valid_dir / rel).write_bytes(b"ok")

            ok_report = validate_ge_proton_integrity(valid_dir)
            self.assertTrue(ok_report.valid)
            self.assertEqual(ok_report.missing_files, ())
            self.assertEqual(ok_report.broken_symlinks, ())
            self.assertIsNone(ok_report.error)

            # Empty file + broken symlink
            broken_dir = tmp_path / "GE-Proton-Broken"
            (broken_dir / "files" / "bin").mkdir(parents=True)
            (broken_dir / "proton").write_bytes(b"")  # empty
            (broken_dir / "compatibilitytool.vdf").write_bytes(b"ok")
            (broken_dir / "files" / "bin" / "wine").write_bytes(b"ok")
            (broken_dir / "files" / "bin" / "broken_tool").symlink_to(tmp_path / "does_not_exist")

            bad_report = validate_ge_proton_integrity(broken_dir)
            self.assertFalse(bad_report.valid)
            self.assertIn("proton", bad_report.missing_files)
            self.assertIn("toolmanifest.vdf", bad_report.missing_files)
            self.assertIn("files/bin/broken_tool", bad_report.broken_symlinks)
            self.assertIsNotNone(bad_report.error)

            # Missing directory
            missing_report = validate_ge_proton_integrity(tmp_path / "nonexistent")
            self.assertFalse(missing_report.valid)
            self.assertEqual(missing_report.error, "Proton directory does not exist.")

    # 3. snapshot_prefix_state & restore_prefix_state_snapshot
    def test_snapshot_and_restore_prefix_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pfx = tmp_path / "prefix" / "pfx"
            pfx.mkdir(parents=True)
            (pfx / "user.reg").write_text('WINE REGISTRY Version 2\n[Software\\\\Test]\n"v"="1"\n', encoding="utf-8")
            sys32 = pfx / "drive_c" / "windows" / "system32"
            sys32.mkdir(parents=True)
            (sys32 / "dxgi.dll").write_bytes(b"orig-dxgi")

            snap_root = tmp_path / "snapshots"
            snap = snapshot_prefix_state(pfx, "Test Game", reason="optiscaler", dest_root=snap_root)
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.reg_count, 1)
            self.assertEqual(snap.dll_count, 1)
            self.assertTrue(snap.snapshot_zip.is_file())

            # Mutate prefix
            (pfx / "user.reg").write_text("CORRUPTED", encoding="utf-8")
            (sys32 / "dxgi.dll").write_bytes(b"modified-dxgi")

            reg_restored, dll_restored = restore_prefix_state_snapshot(snap.snapshot_zip, pfx)
            self.assertEqual(reg_restored, 1)
            self.assertEqual(dll_restored, 1)
            self.assertIn("WINE REGISTRY Version 2", (pfx / "user.reg").read_text(encoding="utf-8"))
            self.assertEqual((sys32 / "dxgi.dll").read_bytes(), b"orig-dxgi")

    # 4. load_ludusavi_manifest + _extract_registry_saves/_restore_registry_saves + preview_sync_operation
    def test_ludusavi_manifest_registry_saves_and_preview_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_file = tmp_path / "manifest.json"
            manifest_data = {
                "My Game": {
                    "steam": {"id": 424242},
                    "files": {
                        "<winDocuments>/My Game/*": {"tags": ["save"]},
                    },
                    "registry": {
                        "HKEY_CURRENT_USER/Software/MyStudio/MyGame": {"tags": ["save"]},
                    },
                }
            }
            manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

            loaded = load_ludusavi_manifest(manifest_file)
            self.assertIn("My Game", loaded)
            self.assertEqual(load_ludusavi_manifest(tmp_path / "missing.json"), {})

            src_pfx = tmp_path / "src_pfx"
            src_pfx.mkdir()
            (src_pfx / "user.reg").write_text(
                "WINE REGISTRY Version 2\n"
                "\n"
                "[Software\\\\MyStudio\\\\MyGame] 1700000000\n"
                '"Progress"="chapter3"\n'
                "\n"
                "[Software\\\\Other\\\\Key] 1700000000\n"
                '"Ignore"="yes"\n',
                encoding="utf-8",
            )
            save_file = src_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "My Game" / "save.bin"
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"save-bytes-123")

            reg_bytes = _extract_registry_saves(src_pfx, ["HKEY_CURRENT_USER/Software/MyStudio/MyGame"])
            self.assertIsNotNone(reg_bytes)
            assert reg_bytes is not None
            reg_json = json.loads(reg_bytes.decode("utf-8"))
            self.assertEqual(len(reg_json["entries"]), 1)
            self.assertEqual(reg_json["entries"][0]["subkey"], "Software\\MyStudio\\MyGame")

            dst_pfx = tmp_path / "dst_pfx"
            dst_pfx.mkdir()
            restored_keys = _restore_registry_saves(dst_pfx, reg_bytes)
            self.assertEqual(restored_keys, 1)
            self.assertIn('"Progress"="chapter3"', (dst_pfx / "user.reg").read_text(encoding="utf-8"))

            # preview_sync_operation for PUSH and PULL
            sync_root = tmp_path / "sync_root"
            with patch("deckinstall.save_sync.DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest_file):
                push_preview = preview_sync_operation(
                    "My Game",
                    src_pfx,
                    sync_root,
                    action="PUSH",
                    steam_appid=424242,
                )
                self.assertEqual(push_preview.action, "PUSH")
                self.assertEqual(push_preview.registry_entries, 1)
                self.assertTrue(any(f.rel_path == "registry/registry.json" for f in push_preview.files))
                self.assertTrue(any(f.rel_path.endswith("save.bin") for f in push_preview.files))

                push_game_saves("My Game", src_pfx, sync_root, steam_appid=424242)

                pull_preview = preview_sync_operation(
                    "My Game",
                    dst_pfx,
                    sync_root,
                    action="PULL",
                    steam_appid=424242,
                )
                self.assertEqual(pull_preview.action, "PULL")
                self.assertEqual(pull_preview.registry_entries, 1)
                self.assertTrue(
                    any(f.operation == "PULL" and f.rel_path.endswith("save.bin") for f in pull_preview.files)
                )

    # 5. legacy LSFG tokens in existing launch options are preserved untouched
    def test_apply_launch_presets_preserves_legacy_lsfg_tokens(self) -> None:
        preserved = apply_launch_presets(
            "ENABLE_LSFG=1 LSFG_MULTIPLIER=2 lsfg-vk %command% -dx11",
            gamemode=False,
            mangohud=True,
            wined3d=False,
            no_esync=False,
            no_fsync=False,
        )
        self.assertIn("ENABLE_LSFG=1", preserved)
        self.assertIn("LSFG_MULTIPLIER=2", preserved)
        self.assertIn("lsfg-vk", preserved)
        self.assertIn("MANGOHUD=1", preserved)
        self.assertTrue(preserved.endswith("%command% -dx11"))

    # 7. migrate_compatdata_to_microsd & restore_compatdata_from_microsd
    def test_migrate_and_restore_compatdata_microsd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = _make_steam_paths(tmp_path)
            appid = 777888
            src_compat = paths.compatdata_dir / str(appid)
            save_file = src_compat / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat"
            save_file.parent.mkdir(parents=True)
            save_file.write_bytes(b"compat-save-data")

            microsd_compat = tmp_path / "microsd" / "compatdata"
            microsd_compat.mkdir(parents=True)

            with patch("deckinstall.steam_vdf._is_path_on_mounted_volume", return_value=True):
                migrated = migrate_compatdata_to_microsd(
                    paths, appid, microsd_compat, verify_mounted=True
                )

            self.assertEqual(migrated, microsd_compat / str(appid))
            self.assertTrue(src_compat.is_symlink())
            self.assertEqual(src_compat.resolve(), migrated.resolve())
            self.assertEqual(
                (migrated / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat").read_bytes(),
                b"compat-save-data",
            )

            restored = restore_compatdata_from_microsd(paths, appid, remove_microsd_copy=True)
            self.assertEqual(restored, src_compat)
            self.assertFalse(src_compat.is_symlink())
            self.assertTrue(src_compat.is_dir())
            self.assertFalse(migrated.exists())
            self.assertEqual(
                (src_compat / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat").read_bytes(),
                b"compat-save-data",
            )

    # 8. "Prefix Cache" in find_cleanup_candidates + delete_cleanup_candidates_checked recreating Temp
    def test_prefix_cache_cleanup_recreates_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = _make_steam_paths(tmp_path)
            appid = 555111
            manifest = paths.steam_root / "steamapps" / f"appmanifest_{appid}.acf"
            manifest.write_text(
                f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"Cached Game"\n}}\n',
                encoding="utf-8",
            )
            temp_dir = (
                paths.compatdata_dir / str(appid) / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Temp"
            )
            temp_dir.mkdir(parents=True)
            junk = temp_dir / "shader_junk.tmp"
            junk.write_bytes(b"1234567890")

            home_dir = tmp_path / "home"
            home_dir.mkdir()

            candidates = find_cleanup_candidates(paths, include_system_caches=True, home_dir=home_dir)
            prefix_caches = [c for c in candidates if c.category == "Prefix Cache"]
            self.assertEqual(len(prefix_caches), 1)
            self.assertEqual(prefix_caches[0].path, temp_dir)
            self.assertEqual(prefix_caches[0].size_bytes, 10)

            deleted, freed, errors = delete_cleanup_candidates_checked(
                paths=paths,
                candidates=prefix_caches,
                include_system_caches=True,
                home_dir=home_dir,
            )
            self.assertEqual(deleted, 1)
            self.assertEqual(freed, 10)
            self.assertEqual(errors, [])
            self.assertTrue(temp_dir.is_dir())
            self.assertFalse(junk.exists())

    # 9. standalone cache_dir + orphan shader caches cleaned by the Maintenance rule
    def test_standalone_cache_dir_and_orphan_shader_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            games_dir = tmp_path / "Games"
            games_dir.mkdir()
            app_dir = tmp_path / "applications"
            app_dir.mkdir()

            game_src = games_dir / "src_game"
            game_src.mkdir()
            exe_path = game_src / "game.exe"
            exe_path.write_bytes(b"MZ-exe")

            with (
                patch("deckinstall.standalone.get_games_dir", return_value=games_dir),
                patch("deckinstall.standalone.get_applications_dir", return_value=app_dir),
            ):
                game = install_standalone_game(
                    game_name="My Standalone",
                    exe_path=exe_path,
                    game_dir=game_src,
                    create_menu_shortcut=False,
                    create_desktop_shortcut=False,
                    keep_in_place=True,
                )

                self.assertIsNotNone(game.cache_dir)
                assert game.cache_dir is not None
                self.assertTrue(game.cache_dir.is_dir())
                self.assertTrue((game.cache_dir / "dxvk").is_dir())
                self.assertTrue((game.cache_dir / "mesa").is_dir())

                # Create an orphaned shader cache under games_dir/.caches
                orphan_dir = games_dir / ".caches" / "dead-game-cache"
                orphan_dir.mkdir(parents=True)
                (orphan_dir / "dxvk.cache").write_bytes(b"abcdef")

                orphans = find_orphan_shader_caches(games_dir=games_dir)
                self.assertEqual([o.name for o in orphans], ["dead-game-cache"])
                self.assertEqual(orphans[0].size_bytes, 6)

                from deckinstall import cleanup

                cands = [
                    c for c in cleanup.find_cleanup_candidates(home_dir=tmp_path)
                    if c.category == "Orphan Game Cache"
                ]
                self.assertEqual([c.path for c in cands], [orphan_dir])
                self.assertTrue(cleanup.is_default_checked(cands[0]))  # caches come back by themselves
                deleted, freed, errors = cleanup.delete_cleanup_candidates_checked(
                    candidates=cands, home_dir=tmp_path
                )
                self.assertEqual((deleted, freed, errors), (1, 6, []))
                self.assertFalse(orphan_dir.exists())
                self.assertTrue(game.cache_dir.is_dir())

    # 10. dir_size_bytes + single-pass scanner.scan_game_folder
    def test_dir_size_bytes_and_scan_game_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            folder = tmp_path / "My.Cool.Game.v1.2"
            (folder / "bin").mkdir(parents=True)
            (folder / "bin" / "CoolGame.exe").write_bytes(b"x" * 100)
            (folder / "bin" / "unins000.exe").write_bytes(b"y" * 50)
            (folder / "steam_appid.txt").write_text("480\n", encoding="utf-8")
            # Symlink should be skipped by dir_size_bytes
            outside = tmp_path / "outside.bin"
            outside.write_bytes(b"z" * 1000)
            os.symlink(outside, folder / "link.bin")

            expected_size = 100 + 50 + len("480\n")
            self.assertEqual(dir_size_bytes(folder), expected_size)

            detected = scan_game_folder(folder)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.name, "My Cool Game v1 2")
            self.assertEqual(detected.steam_appid, 480)
            self.assertEqual(detected.primary_exe, folder / "bin" / "CoolGame.exe")
            self.assertEqual(detected.executables, [folder / "bin" / "CoolGame.exe"])


if __name__ == "__main__":
    unittest.main()
