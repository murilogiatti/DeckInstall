"""Fase 6.3c: queued Steam edits apply by themselves once Steam is closed, never while it runs."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import ui, ui_workers_library  # noqa: E402
from deckinstall.vdf_queue import QueuedVdfEdit  # noqa: E402


def _window(app: QApplication) -> ui.MainWindow:
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
    ):
        win = ui.MainWindow(app)
    _drain(win, app)  # let the startup detection finish so it cannot overwrite win.paths later
    return win


def _drain(win: ui.MainWindow, app: QApplication) -> None:
    """Wait for workers, including ones that a finished worker's signal starts next."""
    for _ in range(5):
        for _ in range(200):
            app.processEvents()
            if not any(w.isRunning() for w in win._active_workers):
                break
            for w in list(win._active_workers):
                w.wait(20)
        app.processEvents()


def _edit() -> QueuedVdfEdit:
    return QueuedVdfEdit("set_launch_options:1", "set_launch_options", 1, "Game", {"launch_options": "X=1 %command%"})


def test_edits_wait_while_steam_runs_then_apply_when_closed() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    running = {"value": True}
    applied: list[object] = []
    try:
        win.paths = SimpleNamespace(shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        win._vdf_queue.apply_all = lambda paths: applied.append(paths) or SimpleNamespace(applied=1, errors=[])  # type: ignore[method-assign]
        with (
            mock.patch.object(ui_workers_library, "is_steam_running", side_effect=lambda: running["value"]),
            mock.patch.object(ui, "is_steam_running", side_effect=lambda: running["value"]),
            mock.patch.object(win, "_offer_steam_restart") as offer,
            mock.patch.object(win, "_refresh_shortcuts"),
        ):
            win._vdf_queue.enqueue(_edit())
            win._update_vdf_queue_banner()
            win._vdf_watch_tick()
            _drain(win, app)
            assert applied == []  # Steam runs: nothing is written
            assert win._vdf_queue.pending_count() == 1
            assert win._vdf_steam_open and win._vdf_watch_timer.isActive()
            assert "Steam" in win.lbl_vdf_queue.text()

            running["value"] = False  # the user closes Steam
            win._vdf_watch_tick()
            _drain(win, app)
            assert applied == [win.paths]
            assert win._busy_count == 0
            offer.assert_called_once()
            win._vdf_queue.clear()
            win._update_vdf_queue_banner()
            assert not win._vdf_watch_timer.isActive()
    finally:
        win._vdf_queue.clear()  # a pending queue would make close() ask a modal question
        _drain(win, app)
        win._active_workers.clear()
        win.close()


def test_steam_restarted_between_poll_and_write_keeps_edits() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        win.paths = SimpleNamespace(shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        win._vdf_queue.enqueue(_edit())
        win._vdf_queue.apply_all = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(ui, "is_steam_running", return_value=True):
            win._auto_apply_vdf_queue()
            _drain(win, app)
        win._vdf_queue.apply_all.assert_not_called()
        assert win._vdf_queue.pending_count() == 1 and win._busy_count == 0
        win._vdf_queue.clear()
    finally:
        win._vdf_queue.clear()  # a pending queue would make close() ask a modal question
        _drain(win, app)
        win._active_workers.clear()
        win.close()


def test_failed_apply_pauses_instead_of_looping() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        win.paths = SimpleNamespace(shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        win._vdf_queue.enqueue(_edit())
        win._vdf_queue.apply_all = lambda paths: SimpleNamespace(applied=0, errors=["Game: boom"])  # type: ignore[method-assign]
        with (
            mock.patch.object(ui, "is_steam_running", return_value=False),
            mock.patch.object(ui.QMessageBox, "warning"),
            mock.patch.object(win, "_refresh_shortcuts"),
        ):
            win._auto_apply_vdf_queue()
            _drain(win, app)
        assert win._vdf_auto_paused
        with mock.patch.object(ui_workers_library, "is_steam_running", return_value=False) as running:
            win._vdf_watch_tick()
            running.assert_not_called()
        win._vdf_queue.clear()
    finally:
        win._vdf_queue.clear()  # a pending queue would make close() ask a modal question
        _drain(win, app)
        win._active_workers.clear()
        win.close()


def test_busy_counter_needs_every_end() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        win._begin_busy()
        win._begin_busy()
        win._end_busy()
        assert win._steam_busy and not win.btn_reload.isEnabled()
        win._end_busy()
        assert not win._steam_busy and win.btn_reload.isEnabled()
        win._end_busy()  # never negative
        assert win._busy_count == 0
    finally:
        win._vdf_queue.clear()  # a pending queue would make close() ask a modal question
        _drain(win, app)
        win._active_workers.clear()
        win.close()
