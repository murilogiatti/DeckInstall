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
        win._vdf_queue.apply_all = lambda paths, **_kw: applied.append(paths) or SimpleNamespace(applied=1, errors=[])  # type: ignore[method-assign]
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


def test_saved_queue_reloads_on_start_and_applies_when_steam_is_closed() -> None:
    from deckinstall import vdf_queue

    vdf_queue.VdfEditQueue(vdf_queue.default_queue_file()).enqueue(_edit())  # saved in Gaming Mode last time
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    applied: list[object] = []
    try:
        assert win._vdf_queue.pending_count() == 1
        assert win.vdf_queue_banner.isHidden()  # no Steam detected: nothing of the queue shows
        win.paths = SimpleNamespace(shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        win._vdf_queue.apply_all = lambda paths, **_kw: applied.append(paths) or SimpleNamespace(applied=1, errors=[])  # type: ignore[method-assign]
        with (
            mock.patch.object(ui_workers_library, "is_steam_running", return_value=False),
            mock.patch.object(ui, "is_steam_running", return_value=False),
            mock.patch.object(win, "_offer_steam_restart"),
            mock.patch.object(win, "_refresh_shortcuts"),
        ):
            win._on_vdf_queue_paths_ready()
            _drain(win, app)
        assert applied == [win.paths]
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win._active_workers.clear()
        win.close()


def test_gaming_mode_queues_and_hides_apply_now() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        assert not win.should_queue_vdf_edits()  # no Steam
        win.paths = SimpleNamespace(shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        with (
            mock.patch.object(ui, "is_gamescope_session", return_value=True),
            mock.patch.object(ui_workers_library, "is_steam_running", return_value=True),
        ):
            assert win.should_queue_vdf_edits()
            win.enqueue_vdf_edit(_edit())
            _drain(win, app)
            assert not win.vdf_queue_banner.isHidden()
            assert win.btn_vdf_apply.isHidden()
            assert "Modo Jogo" in win.lbl_vdf_queue.text() or "Gaming Mode" in win.lbl_vdf_queue.text()
        from deckinstall import vdf_queue

        assert vdf_queue.VdfEditQueue(vdf_queue.default_queue_file()).pending_count() == 1  # persisted
        with mock.patch.object(ui, "is_gamescope_session", return_value=False):
            assert not win.should_queue_vdf_edits()  # Desktop: apply right away
            win._update_vdf_queue_banner()
            assert not win.btn_vdf_apply.isHidden()
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win._active_workers.clear()
        win.close()


_UPDATE_CHOICES = ("app_update.btn_apply_update", "app_update.btn_update_only", None)


def _pick(choice: int):  # type: ignore[no-untyped-def]
    """Fake QMessageBox.clickedButton: the button with that label (None = the Cancel button)."""
    key = _UPDATE_CHOICES[choice]

    def _clicked(box: ui.QMessageBox) -> object:
        for b in box.buttons():
            if key is None and box.buttonRole(b) == ui.QMessageBox.ButtonRole.RejectRole:
                return b
            if key is not None and b.text() == ui.tr(key):
                return b
        raise AssertionError(key)

    return _clicked


def _run_update_prompt(win: ui.MainWindow, choice: int) -> tuple[mock.Mock, mock.Mock]:
    """Answer the 'apply before updating' box (0 apply, 1 update only, 2 cancel)."""
    with (
        mock.patch.object(ui.QMessageBox, "exec", lambda box: 0),
        mock.patch.object(ui.QMessageBox, "clickedButton", _pick(choice)),
        mock.patch.object(ui, "relaunch_appimage") as relaunch,
        mock.patch.object(ui.QApplication, "quit") as quit_app,
        mock.patch.object(win, "_apply_vdf_queue", side_effect=lambda on_done: on_done(True)),
    ):
        win._ask_update_with_pending_edits(2, Path("/tmp/new.AppImage"))
    return relaunch, quit_app


def test_update_with_pending_edits_offers_apply_update_or_cancel() -> None:
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        relaunch, quit_app = _run_update_prompt(win, 2)  # Cancel
        relaunch.assert_not_called()
        quit_app.assert_not_called()
        with mock.patch.object(win, "_apply_vdf_queue") as apply_q:
            with (
                mock.patch.object(ui.QMessageBox, "exec", lambda box: 0),
                mock.patch.object(ui.QMessageBox, "clickedButton", _pick(0)),
                mock.patch.object(ui, "relaunch_appimage") as relaunch,
            ):
                win._ask_update_with_pending_edits(2, Path("/tmp/new.AppImage"))
                apply_q.assert_called_once()
                relaunch.assert_not_called()
                apply_q.call_args.kwargs["on_done"](False)
                relaunch.assert_not_called()
                assert not win._closed
        _drain(win, app)
        relaunch, quit_app = _run_update_prompt(win, 1)
        relaunch.assert_called_once_with(Path("/tmp/new.AppImage"))
        quit_app.assert_not_called()
        assert win._closed and win._skip_vdf_close_prompt
    finally:
        win._skip_vdf_close_prompt = False
        win._vdf_queue.clear()
        _drain(win, app)
        win._active_workers.clear()
        win.close()


def test_enqueue_that_cannot_be_saved_warns_and_is_not_pending(tmp_path: Path) -> None:
    from deckinstall.vdf_queue import VdfEditQueue, VdfQueueSaveError

    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        win._vdf_queue = VdfEditQueue(tmp_path / "vdf_queue.json")
        win.paths = None
        with (
            mock.patch.object(win._vdf_queue, "enqueue", side_effect=VdfQueueSaveError(28, "disk full")),
            mock.patch.object(ui.QMessageBox, "warning") as warn,
        ):
            assert win.enqueue_vdf_edit(_edit()) is False
        warn.assert_called_once()
        assert "NÃO" in warn.call_args.args[2] or "NOT" in warn.call_args.args[2]
        assert win._vdf_queue.pending_count() == 0
    finally:
        _drain(win, app)
        win.close()


def test_bad_queue_file_is_reported_once(tmp_path: Path) -> None:
    from deckinstall.vdf_queue import VdfEditQueue

    store = tmp_path / "vdf_queue.json"
    store.write_text('{"version": 1, "edits": {"bad": 1}}', encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    win = _window(app)
    try:
        win._vdf_queue = VdfEditQueue(store)
        with mock.patch.object(win, "notify") as notify:
            win._report_vdf_queue_load_problem()
            win._report_vdf_queue_load_problem()
        notify.assert_called_once()
        assert str(tmp_path) in notify.call_args.args[0]
    finally:
        _drain(win, app)
        win.close()
