"""Wine tools run inside a prefix, and anti-cheat detection in a game folder."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from deckinstall import classify
from deckinstall import runtime_manager as rm


def test_anticheat_found_by_folder_and_file(tmp_path: Path) -> None:
    (tmp_path / "Game" / "EasyAntiCheat").mkdir(parents=True)
    (tmp_path / "Game" / "Binaries").mkdir()
    (tmp_path / "Game" / "Binaries" / "BEService_x64.exe").write_bytes(b"MZ")
    assert set(
        classify.detect_anticheat(tmp_path / "Game")
    ) == {"Easy Anti-Cheat", "BattlEye"}


def test_no_anticheat_and_symlinks_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "BattlEye").mkdir(parents=True)
    game = tmp_path / "Game"
    game.mkdir()
    (game / "data").symlink_to(outside)
    assert classify.detect_anticheat(game) == []


def test_wine_tool_runs_from_the_prefix(tmp_path: Path) -> None:
    prefix = tmp_path / "pfx"
    umu = tmp_path / "umu-run"
    umu.write_text("")
    with (
        mock.patch.object(rm, "find_system_umu_run", return_value=umu),
        mock.patch.object(rm.subprocess, "run") as run,
    ):
        run.return_value = mock.Mock(returncode=0)
        assert rm.run_exe_in_prefix(Path("winecfg"), prefix, proton_path=tmp_path, allow_download=False) == 0
    assert run.call_args.args[0] == [str(umu), "winecfg"]
    assert run.call_args.kwargs["cwd"] == str(prefix)
