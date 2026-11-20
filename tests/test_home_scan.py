"""Fase 9.4: the default game search walks the home folder up to 3 levels, skipping heavy folders."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import scanner  # noqa: E402


def _exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x00" * 64)


def test_home_walk_finds_games_three_levels_down_and_skips_heavy_folders(tmp_path: Path) -> None:
    home = tmp_path / "deck"
    _exe(home / "games" / "takeover" / "Game.exe")  # the example from the request
    _exe(home / "Downloads" / "setup_tool.exe")  # a loose installer in a container is not a game
    _exe(home / "Downloads" / "Hades" / "Hades.exe")
    _exe(home / "Solo" / "Solo.exe")  # one level down
    _exe(home / "Games" / "Bin Game" / "bin" / "BinGame.exe")  # exe in bin/: the game is the parent
    _exe(home / "a" / "b" / "c" / "d" / "TooDeep.exe")  # 4 levels: not searched
    for skipped in (
        ".local/share/Steam/steamapps/common/X/X.exe",
        ".cache/x/x.exe",
        ".var/app/y/y.exe",
        "Games/Heroic/Prefixes/default/drive_c/windows/notepad.exe",
        "stuff/compatdata/123/pfx/drive_c/z.exe",
        "proj/node_modules/pkg/tool.exe",
    ):
        _exe(home / skipped)
    (home / "link").symlink_to(home / "games")  # symlinks are not followed (no duplicates)

    found = scanner.find_home_game_dirs(home)
    assert set(found) == {
        home / "games" / "takeover",
        home / "Downloads" / "Hades",
        home / "Solo",
        home / "Games" / "Bin Game",
    }
    with mock.patch.object(scanner.Path, "home", return_value=home), mock.patch.object(
        scanner, "default_scan_roots", return_value=[]
    ):
        names = {g.name for g in scanner.scan_all_games()}
    assert names == {"takeover", "Hades", "Solo", "Bin Game"}


def test_custom_roots_keep_container_behaviour(tmp_path: Path) -> None:
    root = tmp_path / "MyGames"
    _exe(root / "A" / "deep" / "deeper" / "A.exe")
    assert [g.name for g in scanner.scan_all_games([root])] == ["A"]


def test_home_walk_is_fast_on_a_big_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # ~20k folders in ignored places plus a few thousand ordinary ones.
    for i in range(200):
        for j in range(100):
            (home / ".local" / "share" / f"app{i}" / f"d{j}").mkdir(parents=True)
    for i in range(60):
        for j in range(60):
            (home / "Documents" / f"proj{i}" / f"sub{j}").mkdir(parents=True)
    for i in range(30):
        _exe(home / "Games" / f"Game{i}" / f"Game{i}.exe")
    start = time.perf_counter()
    found = scanner.find_home_game_dirs(home)
    elapsed = time.perf_counter() - start
    assert len(found) == 30
    print(f"home walk: {elapsed * 1000:.0f} ms")
    assert elapsed < 2.0


def test_home_walk_skips_repack_side_folders_and_finds_gog_roots(tmp_path: Path) -> None:
    home = tmp_path / "deck"
    _exe(home / "Downloads" / "Game [Repack]" / "MD5" / "QuickSFV.EXE")
    _exe(home / "Jogos" / "Tropical" / "_Redist" / "dxwebsetup.exe")
    _exe(home / "Jogos" / "Tropical" / "Windows" / "Tropical.exe")
    (home / "Jogos" / "Tropical" / "goggame-1.info").write_text("{}", encoding="utf-8")
    _exe(home / "Jogos" / "Unity" / "Windows" / "Unity.exe")
    assert set(scanner.find_home_game_dirs(home)) == {home / "Jogos" / "Tropical", home / "Jogos" / "Unity"}
