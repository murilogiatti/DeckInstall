"""Moving a game keeps a journal on disk: an interrupted move is found and reported next time."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from deckinstall import library, relocate
from tests.test_audit_v023_phase_c import _install_v2_game


def _libs(tmp_path: Path) -> tuple[library.Library, library.Library]:
    src = library.ensure_library(tmp_path / "a" / library.LIBRARY_DIRNAME, label="A")
    dst = library.ensure_library(tmp_path / "b" / library.LIBRARY_DIRNAME, label="B")
    return src, dst


def test_finished_and_rolled_back_moves_leave_no_journal(tmp_path: Path) -> None:
    src, dst = _libs(tmp_path)
    game = _install_v2_game(src, "Journal Game", with_prefix=True)
    moved = relocate.relocate_game(relocate.plan_relocation(game, dst))
    assert moved.game_dir.parent == dst.games_dir
    assert relocate.interrupted_moves() == [] and not list(relocate._journal_dir().glob("*.json"))

    back = relocate.plan_relocation(moved, src)
    with mock.patch.object(relocate._sa, "_write_manifest", side_effect=OSError("disk full")):
        with pytest.raises(relocate.RelocateError) as err:
            relocate.relocate_game(back)
    assert err.value.problems == []  # fully undone...
    assert not list(relocate._journal_dir().glob("*.json"))  # ...so nothing to report


def test_an_interrupted_move_is_reported_with_both_places(tmp_path: Path) -> None:
    src, dst = _libs(tmp_path)
    game = _install_v2_game(src, "Cut Game", with_prefix=True)
    plan = relocate.plan_relocation(game, dst)

    class PowerLoss(BaseException):
        """Not an Exception: nothing is undone, like the app being killed."""

    real_move = relocate._move_tree

    def move_then_die(a: Path, b: Path) -> None:
        real_move(a, b)
        raise PowerLoss

    with mock.patch.object(relocate, "_move_tree", side_effect=move_then_die):
        with pytest.raises(PowerLoss):
            relocate.relocate_game(plan)
    moves = relocate.interrupted_moves()
    assert len(moves) == 1
    move = moves[0]
    assert move.name == "Cut Game" and move.step == "game"
    assert move.old_dir == game.game_dir and move.new_dir == plan.new_game_dir
    assert move.new_dir.is_dir()  # the game folder arrived; the manifest still names the old place

    # once the user sorts it out (here: puts the folder back), the journal settles by itself
    move.new_dir.rename(move.old_dir)
    assert relocate.interrupted_moves() == []
    assert not move.journal.exists()
