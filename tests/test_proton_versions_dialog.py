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

from PySide6.QtWidgets import QApplication, QDialog, QTableWidget  # noqa: E402

from deckinstall import runtime_manager, standalone  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.ui_dialogs import proton_dialogs  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def test_dialog_lists_games_with_mode_and_versions(tmp_path: Path) -> None:
    QApplication.instance() or QApplication([])
    tool = runtime_manager.get_compat_tools_dir() / "GE-Proton11-2"
    tool.mkdir(parents=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "Alpha"
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game("Alpha", src / "Game.exe", src)

    rows: list[tuple[str, str, str, str]] = []

    def fake_exec(dlg: QDialog) -> int:
        tbl = dlg.findChild(QTableWidget)
        assert tbl is not None
        for r in range(tbl.rowCount()):
            rows.append(tuple(tbl.item(r, c).text() for c in range(4)))  # type: ignore[arg-type]
        return 0

    with (
        mock.patch.object(proton_dialogs, "_dialog_parent", return_value=None),
        mock.patch.object(QDialog, "exec", fake_exec),
    ):
        proton_dialogs.game_proton_versions_dialog(mock.Mock())
    assert rows == [("Alpha", tr("proton_games.mode_default"), "GE-Proton11-2", "-")]
