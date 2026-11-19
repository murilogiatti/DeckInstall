"""Removing a game must not leave its empty wrapper folder behind."""

from __future__ import annotations

from pathlib import Path

from deckinstall.fsutil import prune_empty_parents


def test_prunes_empty_wrapper_but_keeps_root_and_siblings(tmp_path: Path) -> None:
    games = tmp_path / "Games"
    wrapper = games / "Game"
    (wrapper / "Game").mkdir(parents=True)
    (games / "Other").mkdir()
    (wrapper / "Game").rmdir()  # the game folder was just deleted
    prune_empty_parents(wrapper / "Game", [games])
    assert not wrapper.exists()
    assert games.is_dir() and (games / "Other").is_dir()


def test_keeps_wrapper_that_still_has_files(tmp_path: Path) -> None:
    games = tmp_path / "Games"
    wrapper = games / "Game"
    wrapper.mkdir(parents=True)
    (wrapper / "keep.txt").write_text("x")
    prune_empty_parents(wrapper / "Game", [games])
    assert (wrapper / "keep.txt").is_file()


def test_never_prunes_outside_roots(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "Game"
    outside.mkdir(parents=True)
    prune_empty_parents(outside / "Game", [tmp_path / "Games"])
    assert outside.is_dir()
