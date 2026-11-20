"""Regression tests for complete pre-sync backups and verified prefix migration."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from deckinstall import save_sync, steam_vdf
from deckinstall.steam_paths import SteamPaths


def _steam_paths(root: Path) -> SteamPaths:
    steam = root / "Steam"
    user = steam / "userdata" / "123"
    compat = steam / "steamapps" / "compatdata"
    compat.mkdir(parents=True)
    return SteamPaths(
        steam, "123", user, user / "config" / "shortcuts.vdf", user / "config" / "grid",
        steam / "config" / "config.vdf", compat,
    )


class SaveIntegrityTests(unittest.TestCase):
    def test_snapshot_restore_rejects_symlinked_prefix_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "snapshot.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("snap/users/steamuser/Documents/save.dat", b"save")
            outside = root / "outside"
            outside.mkdir()
            for component in ("drive_c", "pfx"):
                with self.subTest(component=component):
                    prefix = root / component
                    prefix.mkdir()
                    (prefix / component).symlink_to(outside, target_is_directory=True)
                    with self.assertRaisesRegex(OSError, "Unsafe target drive_c"):
                        save_sync._restore_saves_archive(archive, prefix, None)
                    self.assertEqual(list(outside.iterdir()), [])

    def test_pull_preserves_manifest_game_files_and_registry_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "Manifest Game": {
                    "steam": {"id": 42},
                    "files": {"<base>/profile.dat": {}},
                    "registry": {"HKCU/Software/MyGame": {}},
                },
            }), encoding="utf-8")
            src_prefix, dst_prefix = root / "src", root / "dst"
            src_game, dst_game = root / "src_game", root / "dst_game"
            for prefix, game, value in ((src_prefix, src_game, "remote"), (dst_prefix, dst_game, "local")):
                (prefix / "drive_c").mkdir(parents=True)
                game.mkdir()
                (game / "profile.dat").write_text(value, encoding="utf-8")
                (prefix / "user.reg").write_text(
                    'WINE REGISTRY Version 2\n\n[Software\\\\MyGame] 1\n"Progress"="' + value + '"\n',
                    encoding="utf-8",
                )
            with mock.patch.object(save_sync, "DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest):
                save_sync.push_game_saves("Manifest Game", src_prefix, root / "sync", src_game, 42)
                restored, snapshot = save_sync.pull_game_saves(
                    "Manifest Game", dst_prefix, root / "sync", snapshots_root=root / "snapshots",
                    game_dir=dst_game, steam_appid=42,
                )
            self.assertEqual(restored, 2)
            self.assertEqual((dst_game / "profile.dat").read_text(encoding="utf-8"), "remote")
            self.assertIsNotNone(snapshot)
            with zipfile.ZipFile(snapshot) as zf:
                self.assertEqual(zf.read("snap/game/profile.dat"), b"local")
                payload = json.loads(zf.read("snap/registry/registry.json"))
                self.assertIn('"Progress"="local"', payload["entries"][0]["lines"])
            with mock.patch.object(save_sync, "DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest):
                count, safety = save_sync.restore_snapshot_safely(
                    snapshot, dst_prefix, "Manifest Game", game_dir=dst_game, steam_appid=42,
                    dest_root=root / "snapshots",
                )
            self.assertEqual(count, 2)
            self.assertIsNotNone(safety)  # the restore always keeps a copy of what it replaces
            self.assertEqual((dst_game / "profile.dat").read_text(encoding="utf-8"), "local")
            self.assertIn('"Progress"="local"', (dst_prefix / "user.reg").read_text(encoding="utf-8"))

    def test_migration_rejects_nested_destination_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _steam_paths(Path(tmp))
            source = paths.compatdata_dir / "42"
            (source / "pfx" / "drive_c").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "inside the source"):
                steam_vdf.migrate_compatdata_to_microsd(paths, 42, source / "nested", verify_mounted=False)
            self.assertFalse((source / "nested").exists())
            self.assertFalse(source.is_symlink())

    def test_restore_rejects_unrelated_symlink_target_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _steam_paths(root)
            unrelated = root / "Documents"
            (unrelated / "drive_c").mkdir(parents=True)
            marker = unrelated / "important.txt"
            marker.write_bytes(b"keep-me")
            link = paths.compatdata_dir / "42"
            link.symlink_to(unrelated)
            with self.assertRaisesRegex(ValueError, "not a migrated game prefix"):
                steam_vdf.restore_compatdata_from_microsd(paths, 42)
            self.assertTrue(link.is_symlink())
            self.assertEqual(marker.read_bytes(), b"keep-me")

    def test_migration_and_restore_verify_bytes_before_removing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _steam_paths(root)
            source = paths.compatdata_dir / "42"
            save_rel = Path("pfx/drive_c/users/steamuser/save.dat")
            save = source / save_rel
            save.parent.mkdir(parents=True)
            save.write_bytes(b"good")
            (source / "pfx" / "drive_c" / "link").symlink_to("users/steamuser/save.dat")
            copytree = shutil.copytree

            def corrupt_copy(src, dst, *args, **kwargs):
                result = copytree(src, dst, *args, **kwargs)
                if Path(src) in (source, target_root / "42"):
                    (Path(dst) / save_rel).write_bytes(b"evil")
                return result

            target_root = root / "microsd" / "compatdata"
            with mock.patch.object(steam_vdf.shutil, "copytree", side_effect=corrupt_copy):
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    steam_vdf.migrate_compatdata_to_microsd(paths, 42, target_root, verify_mounted=False)
            self.assertEqual(save.read_bytes(), b"good")
            self.assertFalse(source.is_symlink())
            self.assertFalse((target_root / "42").exists())

            target = steam_vdf.migrate_compatdata_to_microsd(paths, 42, target_root, verify_mounted=False)
            self.assertTrue(source.is_symlink())
            with mock.patch.object(steam_vdf.shutil, "copytree", side_effect=corrupt_copy):
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    steam_vdf.restore_compatdata_from_microsd(paths, 42)
            self.assertTrue(source.is_symlink())
            self.assertEqual((target / save_rel).read_bytes(), b"good")
            steam_vdf.restore_compatdata_from_microsd(paths, 42)
            self.assertFalse(source.is_symlink())
            self.assertEqual(save.read_bytes(), b"good")
            self.assertTrue((source / "pfx" / "drive_c" / "link").is_symlink())
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
