"""Bulk Proton/OptiScaler live in their Runtimes cards; the Library keeps the legacy migration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from deckinstall import ui  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402


def test_batch_actions_are_grouped_in_the_library() -> None:
    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
    ):
        win = ui.MainWindow(app)
    try:
        pages = {idx: win.stack.widget(idx) for idx in (ui.PAGE_LIBRARY, ui.PAGE_MAINT, ui.PAGE_RUNTIMES)}
        labels = {
            idx: [b.text() for b in page.findChildren(QPushButton)] + [a.text() for a in page.findChildren(QAction)]
            for idx, page in pages.items()
        }
        expected = {
            "library.btn_bulk_optiscaler": ui.PAGE_LIBRARY,
            "library.btn_bulk_proton": ui.PAGE_LIBRARY,
            "maint.btn_migrate_legacy": ui.PAGE_LIBRARY,
        }
        for key, home in expected.items():
            for idx, page_labels in labels.items():  # exactly one place, the one of its purpose
                assert page_labels.count(tr(key)) == (1 if idx == home else 0), (key, idx)
        assert not hasattr(win, "btn_maint_protons")
        assert not hasattr(win, "btn_maint_game_protons")
        for gone in ("act_art", "act_revert_art", "act_migrate", "chk_batch_vdf", "act_bulk_migrate", "maint_batch_card"):
            assert not hasattr(win, gone), gone
    finally:
        win.close()
