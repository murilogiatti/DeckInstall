"""Fase 6.3: move a library game to another disk's library (relocate.py)."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, relocate, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2, encode_path  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> library.Library:
    """A mounted second disk holding its own (unix) library."""
    media = tmp_path / "media"
    mount = media / "SD"
    mount.mkdir(parents=True)
    monkeypatch.setattr(library, "default_mount_search_roots", lambda: [media])
    monkeypatch.setattr(library, "_is_mount", lambda p: p.parent == media)
    lib = library.ensure_library(mount / library.LIBRARY_DIRNAME, label="SD")
    return lib


def _install(tmp_path: Path, name: str = "Alpha") -> standalone.StandaloneGame:
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(PE)
    (src / "data.pak").write_bytes(b"d" * 500)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game(name, src / "Game.exe", src)
    saves = game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games"
    saves.mkdir(parents=True)
    (saves / "slot1.sav").write_bytes(b"s" * 100)
    assert game.cache_dir is not None
    game.cache_dir.mkdir(parents=True, exist_ok=True)
    (game.cache_dir / "dxvk.cache").write_bytes(b"c" * 50)
    return game


def _reload(name: str) -> standalone.StandaloneGame:
    found = [g for g in standalone.list_standalone_games() if g.name == name]
    assert len(found) == 1
    return found[0]


def test_plan_lists_sizes_and_destinations(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    plan = relocate.plan_relocation(game, disk)
    assert plan.can_run and not plan.warnings
    assert plan.new_game_dir == disk.games_dir / game.game_dir.name
    assert plan.new_prefix_dir == disk.prefixes_dir / game.uid
    assert plan.new_cache_dir == disk.caches_dir / game.uid
    assert plan.game_bytes >= 500 and plan.prefix_bytes == 100 and plan.cache_bytes == 50
    assert plan.free_bytes > 0
    assert game.game_dir.is_dir()  # read-only


def test_target_libraries_excludes_current(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    libs = library.discover_libraries()
    assert [lib.root for lib in relocate.target_libraries(game, libs)] == [disk.root]


def test_move_updates_everything(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    old_dir, old_prefix, old_cache = game.game_dir, game.prefix_dir, game.cache_dir
    plan = relocate.plan_relocation(game, disk)
    moved = relocate.relocate_game(plan)

    assert moved.game_dir == disk.games_dir / old_dir.name
    assert not old_dir.exists() and not old_prefix.exists()
    assert old_cache is not None and not old_cache.exists()
    assert (moved.game_dir / "data.pak").read_bytes() == b"d" * 500
    assert (moved.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games" / "slot1.sav").is_file()
    assert moved.prefix_dir == disk.prefixes_dir / game.uid

    again = _reload("Alpha")  # manifest v2 at the new place, found through the new library
    assert again.game_dir == moved.game_dir and again.uid == game.uid and again.schema == 2
    assert again.prefix_dir == moved.prefix_dir and again.cache_dir == moved.cache_dir
    assert again.exe_path == moved.game_dir / "Game.exe"
    assert (moved.game_dir / MANIFEST_V2).is_file()
    script = again.launch_script.read_text(encoding="utf-8")
    assert str(moved.game_dir) in script and str(old_dir) not in script
    assert str(moved.game_dir) in again.desktop_file.read_text(encoding="utf-8")
    data = library.read_library_file(disk.root)
    assert data is not None and [g["id"] for g in data["games"]] == [game.uid]
    internal = library.read_library_file(library.internal_library_root())
    assert internal is not None and game.uid not in [g["id"] for g in internal["games"]]


def test_cross_device_copies_then_deletes(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    real_rename = os.rename

    def rename(src: object, dst: object, *a: object, **k: object) -> None:
        if str(dst).startswith(str(disk.root)):
            raise OSError(errno.EXDEV, "cross-device")
        real_rename(src, dst, *a, **k)  # type: ignore[arg-type]

    plan = relocate.plan_relocation(game, disk)
    with mock.patch.object(os, "rename", rename):
        moved = relocate.relocate_game(plan)
    assert (moved.game_dir / "data.pak").is_file()
    assert not game.game_dir.exists() and not game.prefix_dir.exists()


def test_same_filesystem_is_a_rename(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    plan = relocate.plan_relocation(game, disk)
    assert plan.same_fs and plan.cross_device_bytes == 0  # tmp_path: both libraries on one filesystem
    with mock.patch("shutil.copytree") as copy:
        relocate.relocate_game(plan)
    copy.assert_not_called()


def test_failure_rolls_back(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    manifest = game.game_dir / MANIFEST_V2
    before_manifest = manifest.read_text(encoding="utf-8")
    before_script = game.launch_script.read_text(encoding="utf-8")
    plan = relocate.plan_relocation(game, disk)
    real = relocate._move_tree
    calls: list[Path] = []

    def flaky(src: Path, dst: Path) -> None:
        calls.append(src)
        if len(calls) == 2:  # the prefix
            raise OSError("disk full")
        real(src, dst)

    with mock.patch.object(relocate, "_move_tree", flaky), pytest.raises(relocate.RelocateError) as err:
        relocate.relocate_game(plan)
    assert err.value.rolled_back
    assert game.game_dir.is_dir() and not plan.new_game_dir.exists()
    assert (game.prefix_dir / "drive_c").is_dir()
    assert manifest.read_text(encoding="utf-8") == before_manifest
    assert game.launch_script.read_text(encoding="utf-8") == before_script
    assert _reload("Alpha").game_dir == game.game_dir
    data = library.read_library_file(disk.root)
    assert data is not None and data["games"] == []


def test_failure_after_move_restores_integration_files(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    before = game.launch_script.read_text(encoding="utf-8")
    plan = relocate.plan_relocation(game, disk)
    with (
        mock.patch.object(standalone, "_write_manifest", side_effect=OSError("boom")),
        pytest.raises(relocate.RelocateError) as err,
    ):
        relocate.relocate_game(plan)
    assert err.value.rolled_back
    assert game.game_dir.is_dir() and game.launch_script.read_text(encoding="utf-8") == before
    assert game.prefix_dir.is_dir()


def test_shared_prefix_is_refused(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    other = _install(tmp_path, "Beta")
    other.prefix_dir = game.prefix_dir
    standalone._write_manifest(other)
    plan = relocate.plan_relocation(game, disk)
    assert relocate.B_SHARED_PREFIX in plan.blockers and not plan.can_run
    with pytest.raises(relocate.RelocateError):
        relocate.relocate_game(plan)
    assert game.game_dir.is_dir()


def test_exfat_target_keeps_prefix_internal(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    exfat = library.Library(disk.id, disk.root, disk.label, False, True, "exfat")
    plan = relocate.plan_relocation(game, exfat)
    assert plan.can_run and plan.new_prefix_dir is None and plan.new_cache_dir is None
    assert relocate.W_PREFIX_STAYS in plan.warnings
    moved = relocate.relocate_game(plan)
    assert moved.game_dir.parent == exfat.games_dir
    assert moved.prefix_dir == game.prefix_dir and (moved.prefix_dir / "drive_c").is_dir()
    again = _reload("Alpha")
    assert again.game_dir == moved.game_dir and again.prefix_dir == game.prefix_dir


def test_same_library_and_unavailable_are_blocked(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    internal = library.ensure_library(library.internal_library_root(), internal=True)
    assert relocate.B_SAME_LIBRARY in relocate.plan_relocation(game, internal).blockers
    gone = library.Library(disk.id, disk.root, disk.label, False, False, "ext4")
    assert relocate.B_UNAVAILABLE in relocate.plan_relocation(game, gone).blockers


def test_not_enough_space_is_blocked(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    exe_dev = os.stat(tmp_path).st_dev
    real_dev = relocate._dev

    def dev(path: Path) -> int | None:  # pretend the disk is another device that is full
        return exe_dev + 1 if str(path).startswith(str(disk.root)) else real_dev(path)

    with (
        mock.patch.object(relocate, "_dev", dev),
        mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)),
    ):
        plan = relocate.plan_relocation(game, disk)
    assert not plan.same_fs and relocate.B_NO_SPACE in plan.blockers


def test_steam_compat_link_follows_prefix(tmp_path: Path, disk: library.Library) -> None:
    game = _install(tmp_path)
    compat = tmp_path / "steam" / "compatdata" / "4242"
    compat.parent.mkdir(parents=True)
    compat.symlink_to(game.prefix_dir)
    game.meta["steam"] = {"shortcut_appid": 4242, "compatdata_link": encode_path(compat, None)}
    standalone._write_manifest(game)
    plan = relocate.plan_relocation(game, disk)
    assert relocate.W_STEAM_SHORTCUT in plan.warnings
    moved = relocate.relocate_game(plan)
    assert os.readlink(compat) == str(moved.prefix_dir)


def test_plan_summary_mentions_sizes_and_notes(tmp_path: Path, disk: library.Library) -> None:
    from deckinstall.ui_dialogs.relocate_dialog import plan_summary

    game = _install(tmp_path)
    exfat = library.Library(disk.id, disk.root, disk.label, False, True, "exfat")
    text = plan_summary(relocate.plan_relocation(game, exfat))
    assert game.name in text and "exFAT" in text or "exfat" in text.lower()
    blocked = plan_summary(relocate.plan_relocation(game, library.Library("x", disk.root, "X", False, False, "ext4")))
    assert blocked.count("\n- ") >= 1
