"""The default game search walks the home folder down to each game folder, skipping heavy folders."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conftest import pe_exe_bytes  # noqa: E402

from deckinstall import scanner  # noqa: E402


def _exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pe_exe_bytes())


def test_home_walk_finds_games_at_any_depth_and_skips_heavy_folders(tmp_path: Path) -> None:
    home = tmp_path / "deck"
    _exe(home / "games" / "takeover" / "Game.exe")  # the example from the request
    _exe(home / "Downloads" / "setup_tool.exe")  # a loose installer in a container is not a game
    _exe(home / "Downloads" / "Hades" / "Hades.exe")
    _exe(home / "Solo" / "Solo.exe")  # one level down
    _exe(home / "Games" / "Bin Game" / "bin" / "BinGame.exe")  # exe in bin/: the game is the parent
    _exe(home / "a" / "b" / "c" / "d" / "Deep" / "Deep.exe")  # 5 levels down: still found
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
        home / "a" / "b" / "c" / "d" / "Deep",
    }
    with mock.patch.object(scanner.Path, "home", return_value=home), mock.patch.object(
        scanner, "default_scan_roots", return_value=[]
    ):
        names = {g.name for g in scanner.scan_all_games()}
    assert names == {"takeover", "Hades", "Solo", "Bin Game", "Deep"}


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


def test_home_walk_skips_repack_side_folders(tmp_path: Path) -> None:
    home = tmp_path / "deck"
    _exe(home / "Downloads" / "Game [Repack]" / "MD5" / "QuickSFV.EXE")
    _exe(home / "Jogos" / "Tropical" / "_Redist" / "dxwebsetup.exe")
    _exe(home / "Jogos" / "Tropical" / "Windows" / "Tropical.exe")
    _exe(home / "Jogos" / "Unity" / "Windows" / "Unity.exe")
    assert set(scanner.find_home_game_dirs(home)) == {home / "Jogos" / "Tropical", home / "Jogos" / "Unity"}


def test_home_walk_respects_the_folder_cap_and_safety_depth(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for i in range(50):
        (home / f"empty{i:02d}").mkdir(parents=True)
    _exe(home / "zz" / "Late" / "Late.exe")  # sorted last: beyond a small cap
    assert scanner.find_home_game_dirs(home, max_dirs=10) == []
    assert scanner.find_home_game_dirs(home) == [home / "zz" / "Late"]
    deep = home / "1" / "2" / "3" / "4" / "5" / "6" / "7" / "8" / "9"
    _exe(deep / "TooDeep" / "TooDeep.exe")  # deeper than HOME_SCAN_MAX_DEPTH
    assert deep / "TooDeep" not in scanner.find_home_game_dirs(home)


def test_dir_has_game_exe_detects_proton_pe_and_executable_native_files(tmp_path: Path) -> None:
    cases = {
        "dos": ("GAME.EXE", b"MZ" + b"\x00" * 64),
        "arm64": ("Game.exe", pe_exe_bytes(0xAA64)),
        "native": ("start.sh", b"#!/bin/sh\n"),
        "elf": ("game.x86_64", b"\x7fELF" + b"\x00" * 60),
        "setup": ("setup.exe", pe_exe_bytes()),
        "i386": ("Game.exe", pe_exe_bytes(0x14C)),
        "amd64": ("Game.exe", pe_exe_bytes(0x8664)),
    }
    for folder, (name, data) in cases.items():
        path = tmp_path / folder / name
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        if folder in {"native", "elf"}:
            path.chmod(0o755)
    (tmp_path / "gog" / "goggame-1.info").parent.mkdir()
    (tmp_path / "gog" / "goggame-1.info").write_text("{}", encoding="utf-8")
    found = {d.name for d in tmp_path.iterdir() if scanner._dir_has_game_exe(d)}
    assert found == {"i386", "amd64", "native", "elf"}
