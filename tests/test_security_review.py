"""Regression checks for runtime links and save/mod destination containment."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deckinstall import runtime_manager
from deckinstall.external_actions import import_external_saves_to_target
from deckinstall.launcher_scan import ExternalLauncherGame
from deckinstall.optiscaler import OPTISCALER_MANIFEST, install_optiscaler_for_game


class RuntimeArchiveSecurityTests(unittest.TestCase):
    def test_legacy_extractor_preserves_internal_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "runtime.tar"
            with tarfile.open(archive, "w") as tf:
                file = tarfile.TarInfo("bin/real")
                file.size = 7
                tf.addfile(file, io.BytesIO(b"runtime"))
                for name, kind, target in (
                    ("bin/alias", tarfile.SYMTYPE, "real"),
                    ("hardlink", tarfile.LNKTYPE, "bin/real"),
                ):
                    link = tarfile.TarInfo(name)
                    link.type = kind
                    link.linkname = target
                    tf.addfile(link)
            dest = root / "dest"
            dest.mkdir()
            with mock.patch.object(tarfile, "data_filter", None, create=True):
                runtime_manager._safe_extract_tar(archive, dest)
            self.assertTrue((dest / "bin/alias").is_symlink())
            self.assertEqual((dest / "bin/alias").read_bytes(), b"runtime")
            self.assertEqual((dest / "hardlink").stat().st_ino, (dest / "bin/real").stat().st_ino)

    def test_legacy_extractor_rejects_link_pivot_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "pivot.tar"
            outside = root / "outside"
            outside.mkdir()
            with tarfile.open(archive, "w") as tf:
                for name, target in (
                    ("dir/alias", ".."),
                    ("dir/alias/sub/escape", "../../outside"),
                ):
                    link = tarfile.TarInfo(name)
                    link.type = tarfile.SYMTYPE
                    link.linkname = target
                    tf.addfile(link)
                file = tarfile.TarInfo("sub/escape/nested/payload")
                file.size = 4
                tf.addfile(file, io.BytesIO(b"evil"))
            dest = root / "dest"
            dest.mkdir()
            with mock.patch.object(tarfile, "data_filter", None, create=True):
                with self.assertRaisesRegex(RuntimeError, "Unsafe archive link target"):
                    runtime_manager._safe_extract_tar(archive, dest)
            self.assertEqual(list(outside.iterdir()), [])


class ExternalSaveDestinationSecurityTests(unittest.TestCase):
    def test_import_rejects_symlinked_drive_c_and_nested_pfx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source"
            save = src / "drive_c/users/deck/Documents/game/save.dat"
            save.parent.mkdir(parents=True)
            save.write_bytes(b"save")
            ext = ExternalLauncherGame(
                source="lutris", name="Game", exe_path=root / "game.exe",
                game_dir=root, prefix_dir=src, runner="wine", launch_cmd=[],
            )
            outside = root / "outside"
            outside.mkdir()
            for component in ("drive_c", "pfx"):
                with self.subTest(component=component):
                    target = root / f"target_{component}"
                    target.mkdir()
                    (target / component).symlink_to(outside, target_is_directory=True)
                    with self.assertRaisesRegex(OSError, "Unsafe target drive_c"):
                        import_external_saves_to_target(ext, target, "Game")
                    self.assertEqual(list(outside.iterdir()), [])


class OptiScalerManifestSecurityTests(unittest.TestCase):
    def test_reinstall_does_not_restore_backup_outside_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "game"
            game_dir.mkdir()
            exe = game_dir / "game.exe"
            exe.write_bytes(b"MZ")
            outside = root / "outside"
            outside.mkdir()
            backup = outside / "victim.dll.deckinstall.bak"
            backup.write_bytes(b"unrelated-backup")
            payload = root / "payload"
            payload.mkdir()
            (payload / "OptiScaler.dll").write_bytes(b"mod-dll")
            (game_dir / OPTISCALER_MANIFEST).write_text(json.dumps({
                "proxy_dll": "../outside/victim", "installed_files": [],
                "backed_up_files": [],
            }), encoding="utf-8")
            install_optiscaler_for_game(exe, payload_dir=payload)
            self.assertEqual((game_dir / "dxgi.dll").read_bytes(), b"mod-dll")
            self.assertEqual(backup.read_bytes(), b"unrelated-backup")
            self.assertFalse((outside / "victim.dll").exists())


if __name__ == "__main__":
    unittest.main()
