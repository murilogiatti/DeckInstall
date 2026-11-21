"""Fase 8.3/8.5: bulk actions live in the Library ("In bulk"); art and single-migrate actions are gone."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from deckinstall import ui  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402


def test_bulk_buttons_in_library_not_in_maintenance() -> None:
    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
    ):
        win = ui.MainWindow(app)
    try:
        library_page = win.stack.widget(1)
        maintenance_page = win.stack.widget(3)
        lib_labels = [b.text() for b in library_page.findChildren(QPushButton)]
        maint_labels = [b.text() for b in maintenance_page.findChildren(QPushButton)]
        for key in (
            "library.btn_bulk_optiscaler",
            "library.btn_bulk_proton",
            "maint.btn_migrate_legacy",
        ):
            assert lib_labels.count(tr(key)) == 1, key
            assert tr(key) not in maint_labels, key
        assert not hasattr(win, "btn_maint_protons")
        assert not hasattr(win, "btn_maint_game_protons")
        for gone in ("act_art", "act_revert_art", "act_migrate", "chk_batch_vdf", "act_bulk_migrate", "maint_batch_card"):
            assert not hasattr(win, gone), gone
    finally:
        win.close()
