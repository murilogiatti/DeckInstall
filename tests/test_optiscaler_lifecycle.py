"""Fase 3.3: OptiScaler journal/rollback, half-done installs, proxy choice, game manifest, bulk."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import optiscaler as opti  # noqa: E402
from deckinstall import standalone  # noqa: E402
from deckinstall.fsutil import atomic_copy_file  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _payload(root: Path) -> Path:
    payload = root / "payload"
    (payload / "D3D12_Optiscaler").mkdir(parents=True)
    (payload / "OptiScaler.dll").write_bytes(b"OPTI")
    (payload / "OptiScaler.ini").write_text("[Upscalers]\n", encoding="utf-8")
    (payload / "nvngx.dll").write_bytes(b"NV")
    (payload / "D3D12_Optiscaler" / "D3D12Core.dll").write_bytes(b"D3D")
    return payload


def _game_dir(root: Path, *files: str) -> Path:
    game = root / "Game"
    game.mkdir(parents=True)
    (game / "Game.exe").write_bytes(PE)
    for name in files:
        (game / name).write_bytes(b"ORIGINAL-" + name.encode())
    return game


def test_failed_install_rolls_back(tmp_path: Path) -> None:
    game = _game_dir(tmp_path, "dxgi.dll", "nvngx.dll")
    calls = {"n": 0}

    def flaky_copy(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        atomic_copy_file(src, dst)

    with mock.patch.object(opti, "atomic_copy_file", side_effect=flaky_copy), pytest.raises(OSError):
        opti.install_optiscaler_for_game(
            game / "Game.exe", proxy_dll="dxgi", overwrite_existing=True, payload_dir=_payload(tmp_path)
        )
    assert (game / "dxgi.dll").read_bytes() == b"ORIGINAL-dxgi.dll"
    assert (game / "nvngx.dll").read_bytes() == b"ORIGINAL-nvngx.dll"
    assert not list(game.rglob("*.deckinstall.bak"))
    assert not (game / opti.OPTISCALER_MANIFEST).exists()
    assert not (game / "OptiScaler.ini").exists()


def test_interrupted_install_is_flagged_and_cleaned(tmp_path: Path) -> None:
    game = _game_dir(tmp_path, "dxgi.dll", "nvngx.dll")

    def crash_copy(src: Path, dst: Path) -> None:
        if dst.name == "D3D12Core.dll":
            raise KeyboardInterrupt  # like the app being killed: skip our rollback
        atomic_copy_file(src, dst)

    with (
        mock.patch.object(opti, "atomic_copy_file", side_effect=crash_copy),
        mock.patch.object(opti, "uninstall_optiscaler_for_game"),
        pytest.raises(KeyboardInterrupt),
    ):
        opti.install_optiscaler_for_game(
            game / "Game.exe", proxy_dll="dxgi", overwrite_existing=True, payload_dir=_payload(tmp_path)
        )
    state = opti.inspect_optiscaler_state(game / "Game.exe")
    assert state is not None and state.complete is False

    # A backup moved aside right before the crash, but not yet journaled, is restored too.
    (game / "winmm.dll.deckinstall.bak").write_bytes(b"ORIGINAL-winmm")
    result = opti.uninstall_optiscaler_for_game(game / "Game.exe")
    assert result.state is not None
    assert (game / "dxgi.dll").read_bytes() == b"ORIGINAL-dxgi.dll"
    assert (game / "nvngx.dll").read_bytes() == b"ORIGINAL-nvngx.dll"
    assert (game / "winmm.dll").read_bytes() == b"ORIGINAL-winmm"
    assert not (game / "OptiScaler.ini").exists()
    assert opti.inspect_optiscaler_state(game / "Game.exe") is None


def test_recommend_proxy(tmp_path: Path) -> None:
    clean = _game_dir(tmp_path / "a")
    assert opti.recommend_proxy(clean / "Game.exe") == ("dxgi", ())

    reshade = _game_dir(tmp_path / "b", "dxgi.dll")
    assert opti.recommend_proxy(reshade / "Game.exe") == ("winmm", ("dxgi_taken",))

    onlinefix = _game_dir(tmp_path / "c", "dxgi.dll", "OnlineFix64.dll")
    (onlinefix / "OnlineFix.ini").write_text("[Main]\n", encoding="utf-8")
    proxy, notes = opti.recommend_proxy(onlinefix / "Game.exe")
    assert proxy == "version" and "winmm_onlinefix" in notes

    compat = opti.check_optiscaler_compatibility(reshade / "Game.exe", reshade)
    assert compat.recommended_proxy == "winmm"


def _installed_game(tmp_path: Path, name: str = "Alpha") -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, src / "Game.exe", src)


def test_install_and_remove_are_recorded_in_game_manifest(tmp_path: Path) -> None:
    game = _installed_game(tmp_path)
    opti.install_optiscaler_for_game(game.exe_path, proxy_dll="dxgi", payload_dir=_payload(tmp_path))
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    rec = data["created"]["optiscaler"]
    assert rec["proxy_dll"] == "dxgi" and rec["manifest"] == opti.OPTISCALER_MANIFEST
    assert "dxgi.dll" in rec["files"] and "OptiScaler.ini" in rec["files"]

    opti.uninstall_optiscaler_for_game(game.exe_path)
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["created"]["optiscaler"] == {}


def test_bulk_install_and_remove(tmp_path: Path) -> None:
    alpha = _installed_game(tmp_path, "Alpha")
    beta = _installed_game(tmp_path, "Beta")
    (beta.game_dir / "dxgi.dll").write_bytes(b"RESHADE")
    payload = _payload(tmp_path)

    from deckinstall.ui_dialogs.optiscaler_bulk_dialog import _install_files

    def _run(games: list[standalone.StandaloneGame], remove: bool) -> list[opti.BulkOutcome]:
        # The same path the bulk dialog uses: copy files, then save each game's DLL overrides.
        outcomes, writes = _install_files([opti.standalone_bulk_target(g) for g in games], remove, "auto")
        assert writes == []  # DeckInstall games never need a Steam write
        return outcomes

    with mock.patch.object(opti, "ensure_optiscaler", return_value=payload):
        out = _run([alpha, beta], False)
    assert [(o.name, o.status, o.detail) for o in out] == [
        ("Alpha", "installed", "dxgi.dll"),
        ("Beta", "installed", "winmm.dll"),
    ]
    assert (beta.game_dir / "dxgi.dll").read_bytes() == b"RESHADE"  # never overwritten
    games = {g.name: g for g in standalone.list_standalone_games()}
    assert "dxgi=n,b" in games["Alpha"].winedlloverrides
    assert "winmm=n,b" in games["Beta"].winedlloverrides
    assert "winmm=n,b" in games["Beta"].launch_script.read_text(encoding="utf-8")

    out = _run(list(games.values()), True)
    assert [o.status for o in out] == ["removed", "removed"]
    games = {g.name: g for g in standalone.list_standalone_games()}
    assert "dxgi" not in games["Alpha"].winedlloverrides and "winmm" not in games["Beta"].winedlloverrides
    assert not (alpha.game_dir / "OptiScaler.ini").exists()


def test_bulk_dialog_lists_compatibility(tmp_path: Path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog, QTableWidget

    from deckinstall.ui_dialogs import optiscaler_bulk_dialog as bd

    app = QApplication.instance() or QApplication([])
    game = _installed_game(tmp_path, "Alpha")
    (game.game_dir / "nvngx_dlss.dll").write_bytes(b"x")
    rows = bd._scan(None)
    seen: list[tuple[str, bool, str]] = []

    def fake_exec(dlg: QDialog) -> int:
        dlg.deleteLater()  # parentless dialog: delete it while Qt is still up
        tbl = dlg.findChild(QTableWidget)
        assert tbl is not None
        item = tbl.item(0, 0)
        seen.append((item.text(), item.checkState() == Qt.CheckState.Checked, tbl.item(0, 2).text()))
        return 0

    with mock.patch.object(bd, "_dialog_parent", return_value=None), mock.patch.object(QDialog, "exec", fake_exec):
        bd._show(mock.Mock(), rows)
    from PySide6.QtCore import QEvent

    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert seen and seen[0][0] == "Alpha" and seen[0][1] is True and "DLSS" in seen[0][2]


def test_readme_is_not_installed_and_remove_leaves_no_empty_folders(tmp_path: Path) -> None:
    game = _game_dir(tmp_path)
    (game / "Licenses").mkdir()
    (game / "Licenses" / "game_eula.txt").write_text("game", encoding="utf-8")  # the game's own folder
    payload = _payload(tmp_path)
    (payload / "!! README_EXTRACT ALL FILES TO GAME FOLDER !!.txt").write_text("extract", encoding="utf-8")
    (payload / "Licenses").mkdir()
    (payload / "Licenses" / "XeSS_LICENSE.txt").write_text("xess", encoding="utf-8")
    (payload / "Extras").mkdir()
    (payload / "Extras" / "fakenvapi.ini").write_text("[x]\n", encoding="utf-8")
    state = opti.install_optiscaler_for_game(game / "Game.exe", proxy_dll="dxgi", payload_dir=payload)
    names = {p.name for p in state.installed_files}
    assert not any("README" in n for n in names) and not list(game.glob("*README*"))
    assert {"XeSS_LICENSE.txt", "D3D12Core.dll", "fakenvapi.ini"} <= names

    opti.uninstall_optiscaler_for_game(game / "Game.exe")
    assert not (game / "D3D12_Optiscaler").exists() and not (game / "Extras").exists()
    assert (game / "Licenses" / "game_eula.txt").exists()  # a folder that still has game files stays
    assert (game / "Game.exe").exists()
