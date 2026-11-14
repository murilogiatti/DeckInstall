"""Core unit tests for DeckInstall."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall.extractor import is_archive, suggest_target_name  # noqa: E402
from deckinstall.scanner import clean_game_name, detect_steam_appid, scan_game_folder  # noqa: E402
from deckinstall.steam_paths import find_default_proton_name  # noqa: E402


class CoreTests(unittest.TestCase):
    def test_clean_game_name(self) -> None:
        self.assertEqual(
            clean_game_name("Atomic.Heart.Ultimate.Edition-InsaneRamZes"),
            "Atomic Heart",
        )
        self.assertEqual(
            clean_game_name("Stellar.Blade.Complete.Edition-InsaneRamZes"),
            "Stellar Blade",
        )

    def test_archive_detection_and_name(self) -> None:
        self.assertTrue(is_archive(Path("My.Game.v1.0-RUNE.rar")))
        self.assertTrue(is_archive(Path("Game.tar.zst")))
        self.assertFalse(is_archive(Path("setup.exe")))
        self.assertEqual(suggest_target_name(Path("Takeover.2-RUNE.zip")), "Takeover 2")

    def test_scan_game_folder_ranking_and_appid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "STAR WARS - Galactic Racer"
            bin_dir = game_dir / "Griffin" / "Binaries" / "Win64"
            bin_dir.mkdir(parents=True)
            (game_dir / "unins000.exe").write_bytes(b"0" * 100)
            (game_dir / "UnityCrashHandler64.exe").write_bytes(b"0" * 100)
            (game_dir / "Launcher.exe").write_bytes(b"0" * 100)
            shipping = bin_dir / "SWGR-Win64-Shipping.exe"
            shipping.write_bytes(b"0" * 200)
            (game_dir / "steam_appid.txt").write_text("4078430\n", encoding="utf-8")

            detected = scan_game_folder(game_dir)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.primary_exe, shipping)
            self.assertEqual(detected.steam_appid, 4078430)
            self.assertEqual(detect_steam_appid(game_dir), 4078430)

    def test_nested_steam_appid_and_stable_proton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "StellarBlade" / "SB" / "Binaries" / "Win64"
            nested.mkdir(parents=True)
            (nested / "steam_appid.txt").write_text("3489700\n", encoding="utf-8")
            self.assertEqual(detect_steam_appid(root / "StellarBlade"), 3489700)

            common = root / "steam" / "steamapps" / "common"
            (common / "Proton - Experimental").mkdir(parents=True)
            (common / "Proton - Experimental" / "proton").write_text("", encoding="utf-8")
            (common / "Proton 11.0").mkdir(parents=True)
            (common / "Proton 11.0" / "proton").write_text("", encoding="utf-8")
            self.assertEqual(find_default_proton_name(root / "steam"), "proton_11")


if __name__ == "__main__":
    unittest.main()
