"""Executable detection after install (Fase 2.3): GOG playTasks, new folders, ranking, 1-click switch."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import extractor, scanner, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _write(path: Path, data: bytes = PE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_gog_play_task_wins_over_ranking(tmp_path: Path) -> None:
    game = tmp_path / "Witcher"
    _write(game / "Launcher.exe", PE + b"x" * 5000)  # bigger, at the top: ranking alone would pick it
    main = _write(game / "bin" / "x64" / "witcher3.exe")
    (game / "goggame-1207664663.info").write_text(json.dumps({
        "gameId": "1207664663",
        "playTasks": [
            {"category": "launcher", "path": "Launcher.exe", "type": "FileTask"},
            {"category": "game", "isPrimary": True, "path": "bin\\x64\\witcher3.exe", "type": "FileTask"},
            {"category": "document", "type": "URLTask", "link": "https://gog.com"},
        ],
    }), encoding="utf-8")
    assert scanner.gog_primary_exe(game) == main
    detected = scanner.scan_game_folder(game)
    assert detected is not None and detected.primary_exe == main
    assert detected.executables.count(main) == 1


def test_gog_play_task_cannot_escape_the_folder(tmp_path: Path) -> None:
    game = tmp_path / "Game"
    _write(tmp_path / "evil.exe")
    _write(game / "Game.exe")
    (game / "goggame-1.info").write_text(json.dumps({
        "playTasks": [{"isPrimary": True, "path": "..\\evil.exe", "type": "FileTask"}],
    }), encoding="utf-8")
    assert scanner.gog_primary_exe(game) is None


def test_native_linux_candidates(tmp_path: Path) -> None:
    gog_linux = tmp_path / "Hollow Knight"
    start = _write(gog_linux / "start.sh", b"#!/bin/sh\nexec game/hollow_knight.x86_64\n")
    _write(gog_linux / "game" / "hollow_knight.x86_64", b"\x7fELF" + b"\x00" * 60)
    detected = scanner.scan_game_folder(gog_linux)
    assert detected is not None and detected.primary_exe == start
    assert scanner.is_native_linux_exe(start)
    assert not scanner.is_native_linux_exe(_write(tmp_path / "w.exe"))

    both = tmp_path / "Both"
    exe = _write(both / "Game.exe")
    _write(both / "Game.x86_64", b"\x7fELF" + b"\x00" * 60)
    detected = scanner.scan_game_folder(both)
    assert detected is not None and detected.primary_exe == exe


def test_game_root_from_new_files(tmp_path: Path) -> None:
    target = tmp_path / "Games" / "Game"
    nested = _write(target / "Game" / "Game.exe")
    assert extractor.game_root_from_new_files(target, {nested}) == target / "Game"
    top = _write(target / "readme.txt", b"x")
    assert extractor.game_root_from_new_files(target, {nested, top}) == target
    other = _write(target / "Other" / "x.dll", b"x")
    assert extractor.game_root_from_new_files(target, {nested, other}) == target


def test_sh_installer_that_picks_its_own_folder(tmp_path: Path) -> None:
    gog_games = tmp_path / "home" / "GOG Games"
    gog_games.mkdir(parents=True)
    _write(gog_games / "Old Game" / "start.sh", b"#!/bin/sh\n")
    script = _write(tmp_path / "Downloads" / "installer.sh", b"#!/bin/sh\necho hi\n")

    def fake_popen(_cmd: list[str], **_kw: Any) -> mock.Mock:
        _write(gog_games / "New Game" / "start.sh", b"#!/bin/sh\n")
        return mock.Mock()

    with (
        mock.patch.object(extractor, "_native_install_roots", return_value=[gog_games]),
        mock.patch.object(extractor.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(extractor, "_drain_stream_lines", return_value=0),
    ):
        out = extractor.install_native_sh(script, tmp_path / "Lib" / "Games", "New Game")
    assert out == gog_games / "New Game"
    assert not (tmp_path / "Lib" / "Games" / "New Game").exists()


def _installed_game(tmp_path: Path) -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / "Game"
    _write(src / "Game.exe")
    _write(src / "Binaries" / "Win64" / "Game-Win64-Shipping.exe")
    _write(src / "linux" / "game.x86_64", b"\x7fELF" + b"\x00" * 60)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game("Game", src / "Game.exe", src, proton_name="GE-Proton11-1")


def test_switch_exe_in_one_step(tmp_path: Path) -> None:
    game = _installed_game(tmp_path)
    shipping = game.game_dir / "Binaries" / "Win64" / "Game-Win64-Shipping.exe"
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        updated = standalone.change_standalone_exe(game.game_dir, shipping)
    assert updated.exe_path == shipping
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["game"]["exe"] == "Binaries/Win64/Game-Win64-Shipping.exe"
    assert data["id"] == game.uid
    assert "Game-Win64-Shipping.exe" in game.launch_script.read_text(encoding="utf-8")


def test_switch_exe_rejects_outside_and_cross_platform(tmp_path: Path) -> None:
    game = _installed_game(tmp_path)
    outside = _write(tmp_path / "elsewhere.exe")
    with pytest.raises(ValueError):
        standalone.change_standalone_exe(game.game_dir, outside)
    with pytest.raises(ValueError):
        standalone.change_standalone_exe(game.game_dir, game.game_dir / "linux" / "game.x86_64")


def test_configure_dialog_switches_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication, QComboBox, QDialog

    from deckinstall import ui
    from deckinstall.ui_dialogs import configure_game_dialog as cgd

    game = _installed_game(tmp_path)
    app = QApplication.instance() or QApplication([])
    seen: dict[str, Any] = {}

    def fake_exec(dlg: QDialog) -> int:
        combos = [c for c in dlg.findChildren(QComboBox) if c.toolTip() and c.count() > 1]
        combo = combos[0]
        seen["items"] = [combo.itemText(i) for i in range(combo.count())]
        idx = combo.findText("Binaries/Win64/Game-Win64-Shipping.exe")
        combo.setCurrentIndex(idx)
        combo.activated.emit(idx)
        return 0

    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        win = ui.MainWindow(app)
        monkeypatch.setattr(win, "_selected_library_item", lambda: ("standalone", game))
        monkeypatch.setattr(QDialog, "exec", fake_exec)
        cgd.open_configure_game_dialog(win)
        for w in list(win._active_workers):
            w.wait(2000)
        app.processEvents()
        win._active_workers.clear()
        win.close()

    assert seen["items"][0] == "Game.exe"
    assert "Binaries/Win64/Game-Win64-Shipping.exe" in seen["items"]
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["game"]["exe"] == "Binaries/Win64/Game-Win64-Shipping.exe"
