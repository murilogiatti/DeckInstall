"""Regression tests for data-loss guards (shortcuts, moves, prefixes, OptiScaler, extraction)."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest import mock

import pytest

from deckinstall import extractor, fsutil, optiscaler, standalone
from deckinstall import steam_vdf as sv


# --- Same-named shortcuts --------------------------------------------------------------
def test_same_name_different_game_is_not_treated_as_same(tmp_path: Path) -> None:
    other = tmp_path / "Games" / "A" / "launcher.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"MZ")
    new = tmp_path / "Games" / "B" / "launcher.exe"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"MZ")
    item = {"AppName": "Launcher", "Exe": f'"{other}"', "StartDir": f'"{other.parent}"'}
    assert not sv._is_same_game_entry(item, new, new.parent)


def test_same_name_same_folder_or_missing_exe_is_same_game(tmp_path: Path) -> None:
    exe = tmp_path / "Games" / "A" / "bin" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    same_dir = {"Exe": f'"{exe.parent.parent / "old.exe"}"', "StartDir": f'"{exe.parent.parent}"'}
    assert sv._is_same_game_entry(same_dir, exe, exe.parent)
    moved = {"Exe": f'"{tmp_path / "Gone" / "game.exe"}"', "StartDir": f'"{tmp_path / "Gone"}"'}
    assert sv._is_same_game_entry(moved, exe, exe.parent)
    assert sv._is_same_game_entry({"AppName": "X"}, exe, exe.parent)


def test_same_name_on_unmounted_removable_media_is_kept(tmp_path: Path) -> None:
    exe = tmp_path / "Games" / "A" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    sd = {"Exe": '"/run/media/deck/sd/Games/X/game.exe"', "StartDir": '"/run/media/deck/sd/Games/X"'}
    assert not sv._is_same_game_entry(sd, exe, exe.parent)


# --- Cross-device move -----------------------------------------------------------------
def _exdev(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.EXDEV, "cross-device")


def test_move_dir_safely_keeps_destination_when_source_cleanup_fails(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "save.dat").write_bytes(b"data")
    dst = tmp_path / "dst"
    with (
        mock.patch.object(fsutil.os, "rename", side_effect=_exdev),
        mock.patch.object(fsutil.shutil, "rmtree", side_effect=OSError("busy")),
    ):
        fsutil.move_dir_safely(src, dst)
    assert (dst / "sub" / "save.dat").read_bytes() == b"data"
    assert (src / "sub" / "save.dat").exists()


def test_move_dir_safely_copy_failure_leaves_source_intact(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "save.dat").write_bytes(b"data")
    with (
        mock.patch.object(fsutil.os, "rename", side_effect=_exdev),
        mock.patch.object(fsutil.shutil, "copytree", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        fsutil.move_dir_safely(src, tmp_path / "dst")
    assert (src / "save.dat").read_bytes() == b"data"


# --- Standalone prefix collision -------------------------------------------------------
def _install(games: Path, game_dir: Path) -> standalone.StandaloneGame:
    exe = game_dir / "Foo.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    with (
        mock.patch.dict(os.environ, {"DECKINSTALL_GAMES_DIR": str(games)}),
        mock.patch.object(standalone, "APPLICATIONS_DIR", games / "apps"),
        mock.patch.object(standalone, "get_desktop_dir", return_value=games / "desktop"),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "detect_steam_appid", return_value=None),
        mock.patch.object(standalone, "check_disk_space", return_value=None),
    ):
        return standalone.install_standalone_game("Foo", exe, game_dir, create_menu_shortcut=False)


def test_standalone_prefixes_do_not_collide_and_removal_keeps_shared(tmp_path: Path) -> None:
    games = tmp_path / "Games"
    a = _install(games, games / "A" / "Foo")
    b = _install(games, games / "B" / "Foo")
    assert a.prefix_dir != b.prefix_dir
    # Reinstalling the same folder reuses its existing prefix.
    assert _install(games, games / "A" / "Foo").prefix_dir == a.prefix_dir

    with mock.patch.dict(os.environ, {"DECKINSTALL_GAMES_DIR": str(games)}):
        assert standalone._prefix_used_by_other_game(a.prefix_dir, games / "C") is True
        assert standalone._prefix_used_by_other_game(a.prefix_dir, a.game_dir) is False


# --- OptiScaler backup -----------------------------------------------------------------
def test_optiscaler_backup_preserves_differing_file(tmp_path: Path) -> None:
    target = tmp_path / "dxgi.dll"
    target.write_bytes(b"game-updated")
    backup = tmp_path / "dxgi.dll.deckinstall.bak"
    backup.write_bytes(b"original")
    backed: list[Path] = []
    optiscaler._backup_if_needed(target, set(), backed)
    assert not target.exists()
    assert backup.read_bytes() == b"original"
    assert (tmp_path / "dxgi.dll.deckinstall.bak.1").read_bytes() == b"game-updated"
    assert backed == [target]


def test_optiscaler_backup_drops_identical_duplicate(tmp_path: Path) -> None:
    target = tmp_path / "dxgi.dll"
    target.write_bytes(b"same")
    (tmp_path / "dxgi.dll.deckinstall.bak").write_bytes(b"same")
    optiscaler._backup_if_needed(target, set(), [])
    assert not target.exists()
    assert not (tmp_path / "dxgi.dll.deckinstall.bak.1").exists()


# --- Extraction over existing folder ---------------------------------------------------
def test_unwrap_type_clash_sets_existing_data_aside(tmp_path: Path) -> None:
    final = tmp_path / "Game"
    (final / "saves").mkdir(parents=True)
    (final / "saves" / "slot1.sav").write_bytes(b"progress")
    (final / "config").write_bytes(b"user-config")

    staged = tmp_path / "stage" / "Game"
    staged.mkdir(parents=True)
    (staged / "saves").write_bytes(b"archive-file")
    (staged / "config").mkdir()
    (staged / "config" / "default.ini").write_bytes(b"ini")

    extractor._unwrap_single_folder(tmp_path / "stage", final)

    assert (final / "saves").read_bytes() == b"archive-file"
    assert (final / "saves.deckinstall-old" / "slot1.sav").read_bytes() == b"progress"
    assert (final / "config" / "default.ini").read_bytes() == b"ini"
    assert (final / "config.deckinstall-old").read_bytes() == b"user-config"
