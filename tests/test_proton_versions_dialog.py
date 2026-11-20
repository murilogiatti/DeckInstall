"""Fase 3.1: the "Proton per game" dialog lists games and pins them in batch."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QTableWidget  # noqa: E402

from deckinstall import runtime_manager, standalone  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.ui_dialogs import proton_dialogs  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def test_dialog_lists_games_with_mode_and_versions(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    tool = runtime_manager.get_compat_tools_dir() / "GE-Proton11-2"
    tool.mkdir(parents=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "Alpha"
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game("Alpha", src / "Game.exe", src)

    dlg = QDialog()
    page = proton_dialogs._build_game_proton_widget(mock.Mock(), dlg, True)
    tbl = page.findChild(QTableWidget)
    assert tbl is not None
    rows = [tuple(tbl.item(r, c).text() for c in range(4)) for r in range(tbl.rowCount())]  # type: ignore[union-attr]
    assert rows == [("Alpha", tr("proton_games.mode_default"), "GE-Proton11-2", "-")]
    assert app is not None
    page.deleteLater()
    dlg.deleteLater()


def test_pin_action_calls_set_games_proton(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QPushButton

    app = QApplication.instance() or QApplication([])
    tool = runtime_manager.get_compat_tools_dir() / "GE-Proton11-2"
    tool.mkdir(parents=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "Beta"
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game("Beta", src / "Game.exe", src)
    dlg = QDialog()
    win = mock.Mock()
    worker_cls = mock.Mock()
    with (
        mock.patch("deckinstall.proton_policy.set_games_proton", return_value=(1, [])) as sgp,
        mock.patch.object(proton_dialogs, "SteamOpWorker", worker_cls),
    ):
        page = proton_dialogs._build_game_proton_widget(win, dlg, True)
        tbl = page.findChild(QTableWidget)
        assert tbl is not None
        tbl.item(0, 0).setCheckState(Qt.CheckState.Checked)  # type: ignore[union-attr]
        btn = next(b for b in page.findChildren(QPushButton) if b.text() == tr("proton_games.btn_apply"))
        btn.click()
        job = worker_cls.call_args.args[0]
        job()
    assert sgp.called
    assert app is not None
    page.deleteLater()
    dlg.deleteLater()


def test_bulk_dialog_games_tab_visibility(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QTabWidget

    app = QApplication.instance() or QApplication([])
    assert app is not None
    src = tmp_path / "Gamma"
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    assert proton_dialogs._deckinstall_proton_games() == []
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game("Gamma", src / "Game.exe", src)
    assert [g.name for g in proton_dialogs._deckinstall_proton_games()] == ["Gamma"]
    assert tr("proton_bulk.tab_games")
    assert QTabWidget is not None
