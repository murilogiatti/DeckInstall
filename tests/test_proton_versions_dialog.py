"""Proton in batch: one list (DeckInstall games + Steam shortcuts), always the newest GE-Proton."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import proton_policy, runtime_manager, standalone  # noqa: E402
from deckinstall.steam_vdf import ShortcutEntry  # noqa: E402
from deckinstall.ui_dialogs import proton_bulk_dialog as pbd  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _tool(name: str) -> None:
    tool = runtime_manager.get_compat_tools_dir() / name
    tool.mkdir(parents=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")


def _install(tmp_path: Path, name: str) -> standalone.StandaloneGame:
    src = tmp_path / name
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, src / "Game.exe", src)


def _shortcut(appid: int, name: str, exe: str) -> ShortcutEntry:
    return ShortcutEntry("0", appid, name, f'"{exe}"', '"/x"', "", "")


def test_rows_mark_default_games_newest_and_pinned_ones_outdated(tmp_path: Path) -> None:
    _tool("GE-Proton11-1")
    _tool("GE-Proton11-2")
    alpha = _install(tmp_path, "Alpha")
    beta = _install(tmp_path, "Beta")
    proton_policy.set_game_proton(beta.game_dir, "GE-Proton11-1")
    rows, orphans, newest = pbd.collect_rows(None)
    assert newest == "GE-Proton11-2" and orphans == []
    by_name = {r.name: r for r in rows}
    assert by_name["Alpha"].newest and by_name["Alpha"].current == "GE-Proton11-2"
    assert not by_name["Beta"].newest and by_name["Beta"].current == "GE-Proton11-1"
    assert by_name["Alpha"].game_dir == alpha.game_dir


def test_steam_shortcuts_owned_by_a_game_are_not_listed_twice(tmp_path: Path) -> None:
    _tool("GE-Proton11-2")
    game = _install(tmp_path, "Alpha")
    paths = mock.Mock()
    shortcuts = [
        _shortcut(0x80000001, "Alpha", str(game.exe_path)),  # the DeckInstall game itself
        _shortcut(0x80000002, "Other", "/games/other/Other.exe"),
        _shortcut(0x80000003, "Native", "/games/native/run.sh"),  # no Proton
    ]
    compat = {0x80000001: "proton_11", 0x80000002: "proton_9", 0x80000099: "GE-Proton9-1"}
    with (
        mock.patch.object(pbd, "load_shortcuts", return_value=shortcuts),
        mock.patch.object(pbd, "get_compat_tool_map", return_value=compat),
        mock.patch.object(pbd, "orphan_shortcut_mappings", return_value=[0x80000099]),
    ):
        rows, orphans, _ = pbd.collect_rows(paths)
    assert [(r.name, r.source, r.current) for r in rows] == [
        ("Alpha", "shortcut", "proton_11"),
        ("Other", "shortcut", "proton_9"),
    ]
    assert rows[0].steam_tools == ((0x80000001, "proton_11"),) and not rows[0].newest
    assert rows[0].game_dir == game.game_dir
    assert orphans == [0x80000099]


def test_update_downloads_a_newer_ge_proton_and_moves_default_games(tmp_path: Path) -> None:
    _tool("GE-Proton11-1")
    _install(tmp_path, "Alpha")

    def _download(force_update: bool = False, progress_cb=None) -> Path:  # noqa: ANN001
        _tool("GE-Proton11-2")
        return runtime_manager.get_compat_tools_dir() / "GE-Proton11-2"

    info = {"ge_proton": {"installed": "GE-Proton11-1", "latest": "GE-Proton11-2", "update_available": True}}
    with (
        mock.patch.object(pbd, "check_runtime_updates", return_value=info),
        mock.patch.object(pbd, "ensure_ge_proton11", side_effect=_download) as dl,
        mock.patch.object(pbd, "retarget_default_games", return_value=1) as retarget,
    ):
        assert pbd.ensure_newest_ge_proton() == "GE-Proton11-2"
    assert dl.call_args.kwargs["force_update"] is True
    retarget.assert_called_once_with("GE-Proton11-1")


def test_up_to_date_ge_proton_is_not_downloaded(tmp_path: Path) -> None:
    _tool("GE-Proton11-2")
    info = {"ge_proton": {"installed": "GE-Proton11-2", "latest": "GE-Proton11-2", "update_available": False}}
    with (
        mock.patch.object(pbd, "check_runtime_updates", return_value=info),
        mock.patch.object(pbd, "ensure_ge_proton11") as dl,
    ):
        assert pbd.ensure_newest_ge_proton() == "GE-Proton11-2"
    dl.assert_not_called()
