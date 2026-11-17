"""OptiScaler cache reuse, concurrent installation and nested modal regressions."""

from __future__ import annotations

import hashlib
import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget

from deckinstall import runtime_manager as rm
from deckinstall.i18n import tr
from deckinstall.optiscaler import OptiScalerState
from deckinstall.standalone import StandaloneGame
from deckinstall.ui_dialogs import launch_presets_dialog as presets
from deckinstall.ui_dialogs import prefix_tools_dialogs as tools


def test_concurrent_cold_cache_downloads_once_then_reuses_payload(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("OptiScaler.dll", b"cached-dll")
    content = buffer.getvalue()
    asset = {
        "name": "OptiScaler.zip", "size": len(content),
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "browser_download_url": "https://github.com/optiscaler/OptiScaler/releases/download/v1/OptiScaler.zip",
    }
    cache = tmp_path / "optiscaler/current"
    barrier = threading.Barrier(2)
    messages: list[str] = []

    def download(_url, target, *_args, **_kwargs):
        time.sleep(0.05)  # Keep the cold download active while the second caller arrives.
        target.write_bytes(content)

    def install():
        barrier.wait(timeout=3)
        return rm.ensure_optiscaler(progress_cb=lambda message, _pct: messages.append(message))

    with (
        mock.patch.object(rm, "get_optiscaler_dir", return_value=cache),
        mock.patch.object(rm, "get_versions_file", return_value=tmp_path / "versions.json"),
        mock.patch.object(rm, "_fetch_json", return_value={"tag_name": "v1", "assets": [asset]}) as fetch,
        mock.patch.object(rm, "_download_file", side_effect=download) as transfer,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(pool.map(lambda _: install(), range(2)))
        assert results == [cache, cache]
        assert fetch.call_count == transfer.call_count == 1
        assert (cache / "OptiScaler.dll").read_bytes() == b"cached-dll"
        assert any("cached OptiScaler v1 (no download)" in message for message in messages)
        assert rm.ensure_optiscaler() == cache
        assert transfer.call_count == 1


def _window():
    app = QApplication.instance() or QApplication([])
    win = QWidget()
    win.paths = None
    win._closing_pending = False
    win.status_label = QLabel(win)
    win._get_gpu_profile = lambda: SimpleNamespace(has_nvidia=False, is_hybrid=False, is_wayland=False)
    win._save_last_launch_prefs = mock.Mock()
    win._refresh_shortcuts = mock.Mock()
    win._is_auto_snapshot_enabled = lambda: False
    win._auto_snapshot_if_enabled = mock.Mock()
    workers = []

    def track(worker):
        worker.setParent(win)
        workers.append(worker)

    win._track_worker = track
    return app, win, workers


def test_presets_and_optiscaler_are_children_of_active_configuration(tmp_path: Path) -> None:
    app, win, workers = _window()
    configuration = QDialog(win)
    configuration.setModal(True)
    configuration.show()
    app.processEvents()
    observed = []

    def inspect_and_close():
        active = app.activeModalWidget()
        observed.append(active is not configuration and active.parentWidget() is configuration)
        active.reject()

    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    game = SimpleNamespace(name="KIBORG", exe_path=exe, native_linux=False)
    try:
        QTimer.singleShot(0, inspect_and_close)
        presets.open_launch_presets_dialog(win, "KIBORG", "standalone", "", "")
        QTimer.singleShot(0, inspect_and_close)
        tools.open_optiscaler_dialog(win, "standalone", game)
        assert observed == [True, True]
        assert app.activeModalWidget() is configuration
    finally:
        for worker in workers:
            worker.wait(3000)
        app.processEvents()
        configuration.close()
        win.close()
        app.processEvents()


def test_repeated_click_and_stale_inspection_cannot_start_parallel_operations(tmp_path: Path) -> None:
    app, win, workers = _window()
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    game = StandaloneGame(
        "KIBORG", tmp_path, exe, tmp_path / "prefix", "GE-Proton", None,
        "", "", tmp_path / "launch.sh", tmp_path / "game.desktop",
    )
    state = OptiScalerState("dxgi", "auto", "v1", tmp_path, [], [], "dxgi=n,b")
    release_inspection = threading.Event()
    installation_started = threading.Event()
    release_install = threading.Event()
    inspected = threading.Event()
    observations = []
    stages = {"value": 0}
    buttons = {}

    def inspect(_exe):
        if not inspected.is_set():
            inspected.set()
            release_inspection.wait(3)
        return state

    def install(*_args, **_kwargs):
        installation_started.set()
        release_install.wait(3)
        return state

    def step():
        dialog = app.activeModalWidget()
        if stages["value"] == 0:
            if not inspected.is_set():
                return
            for button in dialog.findChildren(QPushButton):
                buttons[button.text()] = button
            buttons[tr("optiscaler.btn_install")].clicked.emit()
            buttons[tr("optiscaler.btn_install")].clicked.emit()
            stages["value"] = 1
        elif stages["value"] == 1 and installation_started.is_set():
            release_inspection.set()
            stages["value"] = 2
            QTimer.singleShot(30, finish)

    def finish():
        observations.append(not buttons[tr("optiscaler.btn_remove")].isEnabled())
        release_install.set()
        stages["value"] = 3

    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(step)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.activeModalWidget().reject())
    try:
        with (
            mock.patch.object(tools, "inspect_optiscaler_state", side_effect=inspect),
            mock.patch.object(tools, "resolve_game_prefix", return_value=(tmp_path, None)),
            mock.patch.object(tools, "install_optiscaler_for_game", side_effect=install) as installer,
            mock.patch.object(tools, "update_standalone_launch_options", return_value=game),
            mock.patch.object(tools.QMessageBox, "critical"),
            mock.patch.object(tools.QMessageBox, "information"),
        ):
            def close_when_done():
                if stages["value"] == 3 and buttons[tr("optiscaler.btn_install")].isEnabled():
                    app.activeModalWidget().reject()

            poll.timeout.connect(close_when_done)
            poll.start()
            watchdog.start(2000)
            tools.open_optiscaler_dialog(win, "standalone", game)
            assert installer.call_count == 1
            assert observations == [True]
            assert "v1" in win.status_label.text()
    finally:
        poll.stop()
        watchdog.stop()
        release_install.set()
        release_inspection.set()
        for worker in workers:
            worker.wait(3000)
        app.processEvents()
        win.close()
