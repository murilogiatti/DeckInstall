"""Fase 3.1: Proton "Default (newest)" or pinned per game, previous version, update and pruning."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import proton_policy, runtime_manager, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _proton(name: str) -> Path:
    tool = runtime_manager.get_compat_tools_dir() / name
    tool.mkdir(parents=True, exist_ok=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (tool / ".deckinstall-managed").write_text(f"{name}\n", encoding="utf-8")
    return tool


def _install(tmp_path: Path, name: str, proton: str = "") -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True)
    exe = src / "Game.exe"
    exe.write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, exe, src, proton_name=proton)


def _protonpath(game: standalone.StandaloneGame) -> str:
    for line in game.launch_script.read_text(encoding="utf-8").splitlines():
        if line.startswith("export PROTONPATH="):
            return line.split("=", 1)[1].strip("'")
    return ""


def test_default_game_uses_newest_and_moves_on_update(tmp_path: Path) -> None:
    old = _proton("GE-Proton10-25")
    game = _install(tmp_path, "Alpha")
    assert proton_policy.is_default(game)
    assert _protonpath(game) == str(old)

    new = _proton("GE-Proton11-2")
    pinned = _install(tmp_path, "Beta", proton="GE-Proton10-25")
    assert proton_policy.retarget_default_games(previous_name=old.name) == 1

    reloaded = standalone.list_standalone_games()
    by_name = {g.name: g for g in reloaded}
    assert _protonpath(by_name["Alpha"]) == str(new)
    assert proton_policy.previous_proton_name(by_name["Alpha"]) == old.name
    assert _protonpath(by_name["Beta"]) == str(old)  # pinned games stay put
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["runtime"]["proton"] == {"mode": "default", "name": "", "previous": old.name}
    assert pinned.proton_name == "GE-Proton10-25"


def test_use_previous_pins_and_back_to_default(tmp_path: Path) -> None:
    old = _proton("GE-Proton10-25")
    new = _proton("GE-Proton11-2")
    game = _install(tmp_path, "Alpha")
    assert proton_policy.effective_proton_name(game) == new.name

    with pytest.raises(ValueError):
        proton_policy.use_previous_proton(game.game_dir)

    game = proton_policy.set_game_proton(game.game_dir, old.name)
    assert game.meta["proton_mode"] == "pinned" and _protonpath(game) == str(old)
    assert proton_policy.previous_proton_name(game) == new.name

    game = proton_policy.set_game_proton(game.game_dir, "")
    assert proton_policy.is_default(game) and _protonpath(game) == str(new)
    assert proton_policy.previous_proton_name(game) == old.name

    game = proton_policy.use_previous_proton(game.game_dir)
    assert game.proton_name == old.name and _protonpath(game) == str(old)


def test_batch_pin_unpin(tmp_path: Path) -> None:
    _proton("GE-Proton11-2")
    a = _install(tmp_path, "Alpha")
    b = _install(tmp_path, "Beta")
    changed, errors = proton_policy.set_games_proton([a.game_dir, b.game_dir, tmp_path / "missing"], "GE-Proton11-2")
    assert changed == 2 and len(errors) == 1
    assert all(g.proton_name == "GE-Proton11-2" for g in standalone.list_standalone_games())
    proton_policy.set_games_proton([a.game_dir, b.game_dir], "")
    assert all(proton_policy.is_default(g) for g in standalone.list_standalone_games())


def test_runtime_update_moves_default_games_and_prunes(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    for name in ("GE-Proton10-4", "GE-Proton10-25"):
        _proton(name)
    game = _install(tmp_path, "Alpha")
    _proton("GE-Proton11-2")  # what ensure_ge_proton11 just installed
    worker = ui_workers.RuntimeUpdateWorker(paths=None)
    msg = worker._after_ge_update("GE-Proton10-25")
    assert "GE-Proton11-2" in msg
    assert _protonpath(standalone.list_standalone_games()[0]).endswith("GE-Proton11-2")
    # Without Steam paths the Steam mappings are unknown, so nothing is deleted.
    assert (runtime_manager.get_compat_tools_dir() / "GE-Proton10-4").is_dir()
    assert game.name == "Alpha"
    assert worker._after_ge_update("GE-Proton11-2") == ""


def test_runtime_update_retains_old_tools_for_review(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from deckinstall import steam_vdf, ui_workers

    for name in ("GE-Proton9-27", "GE-Proton10-4", "GE-Proton10-25", "GE-Proton11-2"):
        _proton(name)
    _install(tmp_path, "Alpha")
    usage = [SimpleNamespace(tool_name="GE-Proton9-27")]
    with mock.patch.object(steam_vdf, "list_compat_tool_usage", return_value=usage):
        msg = ui_workers.RuntimeUpdateWorker(paths=mock.Mock())._after_ge_update("GE-Proton10-25")
    assert "GE-Proton11-2" in msg
    left = sorted(p.name for p in runtime_manager.get_compat_tools_dir().iterdir())
    assert left == ["GE-Proton10-25", "GE-Proton10-4", "GE-Proton11-2", "GE-Proton9-27"]
