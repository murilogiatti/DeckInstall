"""Lifecycle, theme, and i18n unit tests for DeckInstall UI components."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deckinstall import i18n, theme


class ThemeAndI18nTests(unittest.TestCase):
    def test_theme_qss_contains_danger_hover_in_dark_and_light(self) -> None:
        self.assertIn("QPushButton#dangerBtn:hover", theme.DARK_QSS)
        self.assertIn("QPushButton#dangerBtn:hover", theme.LIGHT_QSS)

    def test_i18n_language_env_priority(self) -> None:
        orig_lang = i18n.get_language()
        try:
            i18n.set_language("pt_BR")
            with mock.patch.dict(
                os.environ,
                {"LANGUAGE": "pt_BR:en", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
                clear=False,
            ):
                self.assertEqual(i18n.detect_system_language(), "pt_BR")

            with mock.patch.dict(
                os.environ,
                {"LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "pt_BR.UTF-8", "LANG": "en_US.UTF-8"},
                clear=False,
            ):
                self.assertEqual(i18n.detect_system_language(), "pt_BR")
        finally:
            i18n.set_language(orig_lang)


class UiLifecycleTests(unittest.TestCase):
    def test_close_keeps_pending_vdf_edits_when_apply_fails(self) -> None:
        from types import SimpleNamespace

        from PySide6.QtGui import QCloseEvent

        from deckinstall import ui

        for failure in (RuntimeError("write failed"), SimpleNamespace(errors=["write failed"])):
            with self.subTest(failure=failure):
                win = mock.Mock()
                win._force_closing = False
                win._skip_vdf_close_prompt = False
                win._vdf_queue.pending_count.return_value = 1
                if isinstance(failure, Exception):
                    win._vdf_queue.apply_all.side_effect = failure
                else:
                    win._vdf_queue.apply_all.return_value = failure
                with (
                    mock.patch.object(ui, "QMessageBox") as box_type,
                    mock.patch.object(ui, "is_steam_running", return_value=False),
                ):
                    box = box_type.return_value
                    apply_button = object()
                    box.addButton.side_effect = [apply_button, object(), object()]
                    box.clickedButton.return_value = apply_button
                    ev = QCloseEvent()
                    ui.MainWindow.closeEvent(win, ev)
                    self.assertFalse(ev.isAccepted())
                    win._vdf_queue.apply_all.assert_called_once_with(win.paths)
                    win._update_vdf_queue_banner.assert_called_once()

    def test_close_keeps_pending_vdf_edits_when_steam_is_missing(self) -> None:
        from PySide6.QtGui import QCloseEvent

        from deckinstall import ui

        win = mock.Mock()
        win._force_closing = False
        win.paths = None
        win._vdf_queue.pending_count.return_value = 1
        busy = mock.Mock()
        busy.isRunning.return_value = True  # stop closeEvent before QMainWindow.closeEvent
        win._active_workers = {busy}
        with mock.patch.object(ui, "QMessageBox") as box_type:
            ev = QCloseEvent()
            ui.MainWindow.closeEvent(win, ev)
            box_type.assert_not_called()  # no Steam: nothing about the queue is shown
            win._vdf_queue.apply_all.assert_not_called()
            win._vdf_queue.clear.assert_not_called()  # the saved queue waits for Steam

    def test_worker_cancel_uses_threading_event(self) -> None:
        from PySide6.QtWidgets import QApplication

        from deckinstall.ui_workers import ExtractWorker, WinetricksWorker

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            ew = ExtractWorker(Path(tmp) / "a.zip", Path(tmp), "Game")
            self.assertFalse(ew._cancel_event.is_set())
            ew.cancel()
            self.assertTrue(ew._cancel_event.is_set())

            ww = WinetricksWorker(Path(tmp), ["vcrun2022"])
            self.assertFalse(ww._cancel_event.is_set())
            ww.cancel()
            self.assertTrue(ww._cancel_event.is_set())

    def test_dialog_guard_flips_false_on_finish(self) -> None:
        from PySide6.QtWidgets import QApplication, QDialog

        from deckinstall.ui_cards import _dialog_guard

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        dlg = QDialog()
        alive = _dialog_guard(dlg)
        self.assertTrue(alive())
        dlg.done(0)
        self.assertFalse(alive())

    def test_close_event_cancels_all_active_workers(self) -> None:
        from PySide6.QtGui import QCloseEvent
        from PySide6.QtWidgets import QApplication

        from deckinstall import ui

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with (
            mock.patch.object(ui, "resolve_steam_paths", return_value=None),
            mock.patch.object(ui, "find_steam_root", return_value=None),
            mock.patch.object(ui, "scan_all_games", return_value=[]),
            mock.patch.object(ui, "list_standalone_games", return_value=[]),
            mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        ):
            win = ui.MainWindow(app)
            app.processEvents()
            for w in list(win._active_workers):
                w.wait(2000)
            app.processEvents()

            fake_w1 = mock.Mock()
            fake_w1.isRunning.return_value = True
            fake_w2 = mock.Mock(spec=["isRunning", "requestInterruption"])
            fake_w2.isRunning.return_value = True
            win._active_workers.add(fake_w1)
            win._active_workers.add(fake_w2)

            ev = QCloseEvent()
            win.closeEvent(ev)
            self.assertFalse(ev.isAccepted())
            fake_w1.cancel.assert_called_once()
            fake_w2.requestInterruption.assert_called_once()

            if win._close_force_timer is not None:
                win._close_force_timer.stop()
            win._active_workers.clear()
            win.close()

    def test_run_exe_in_prefix_worker_calls_run_exe_with_correct_args(self) -> None:
        from PySide6.QtWidgets import QApplication

        from deckinstall import ui_workers

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "pfx"
            exe = Path(tmp) / "setup.exe"
            proton = Path(tmp) / "GE-Proton"
            worker = ui_workers.RunExeInPrefixWorker(prefix, exe, proton_dir=proton)

            with mock.patch.object(ui_workers, "run_exe_in_prefix") as mock_run:
                worker.run()

            mock_run.assert_called_once_with(exe, prefix, proton_path=proton)


if __name__ == "__main__":
    unittest.main()
