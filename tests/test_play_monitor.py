"""Fase 12: Play button states (Loading -> Running -> Play, or Error) with fake processes."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import play_monitor as pm  # noqa: E402


@pytest.fixture()
def mon():
    app = QApplication.instance() or QApplication([])
    owner = QObject()
    m = pm.PlayMonitor(owner)
    m.RUNNING_AFTER_S = 0.3
    m.CHILD_GRACE_S = 0.5
    m.LOADING_TIMEOUT_S = 1.0
    m.GONE_TICKS = 2
    m._timer.setInterval(50)
    events: dict[str, list] = {"failed": [], "timeout": []}
    m.failed.connect(lambda k, n, d: events["failed"].append((k, n, d)))
    m.timed_out.connect(lambda k, n, s: events["timeout"].append((k, n, s)))
    m.events = events  # type: ignore[attr-defined]
    m.app = app  # type: ignore[attr-defined]
    yield m
    m.release()
    owner.deleteLater()
    app.processEvents()


def _wait(m: pm.PlayMonitor, cond, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        m.app.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.01)
    assert cond()


def test_running_then_stop_returns_to_play(mon: pm.PlayMonitor) -> None:
    mon.launch("g", "Game", ["sleep", "30"])
    assert mon.state("g") == pm.LOADING
    _wait(mon, lambda: mon.state("g") == pm.RUNNING)
    mon.stop("g")
    _wait(mon, lambda: mon.state("g") == pm.IDLE)
    assert mon.events["failed"] == []  # type: ignore[attr-defined]


def test_quick_error_exit_shows_log_tail(mon: pm.PlayMonitor, tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    mon.launch("g", "Game", ["sh", "-c", "echo bad exe here >&2; exit 3"], log_file=log)
    _wait(mon, lambda: mon.state("g") == pm.ERROR)
    (_k, name, diag), = mon.events["failed"]  # type: ignore[attr-defined]
    assert name == "Game" and "3" in diag and "bad exe here" in diag
    mon.acknowledge("g")
    assert mon.state("g") == pm.IDLE


def test_command_that_cannot_start_is_an_error(mon: pm.PlayMonitor, tmp_path: Path) -> None:
    mon.launch("g", "Game", [str(tmp_path / "missing.exe")])
    _wait(mon, lambda: mon.state("g") == pm.ERROR)


def test_quick_exit_zero_follows_child_processes(mon: pm.PlayMonitor) -> None:
    alive = {"on": True}
    mon.scan = lambda watch: {4242} if alive["on"] else set()
    mon.launch("g", "Game", ["true"], watch=("/games/x",))
    _wait(mon, lambda: mon.state("g") == pm.RUNNING)
    alive["on"] = False
    _wait(mon, lambda: mon.state("g") == pm.IDLE)
    assert mon.events["failed"] == []  # type: ignore[attr-defined]


def test_quick_exit_zero_without_children_is_an_error(mon: pm.PlayMonitor) -> None:
    mon.scan = lambda watch: set()
    mon.launch("g", "Game", ["true"], watch=("/games/x",))
    _wait(mon, lambda: mon.state("g") == pm.ERROR)


def test_steam_shortcut_waits_for_process_or_times_out(mon: pm.PlayMonitor) -> None:
    found = {"on": False}
    mon.scan = lambda watch: {7} if found["on"] else set()
    mon.watch_only("s", "Steam Game", ("/games/s",))
    assert mon.state("s") == pm.LOADING
    found["on"] = True
    _wait(mon, lambda: mon.state("s") == pm.RUNNING)
    found["on"] = False
    _wait(mon, lambda: mon.state("s") == pm.IDLE)
    mon.watch_only("t", "Other", ("/games/t",))
    _wait(mon, lambda: mon.state("t") == pm.IDLE)
    assert mon.events["timeout"] == [("t", "Other", True)]  # type: ignore[attr-defined]


def test_two_games_run_together_and_release_keeps_them(mon: pm.PlayMonitor) -> None:
    mon.launch("a", "A", ["sleep", "30"])
    mon.launch("b", "B", ["sleep", "30"])
    _wait(mon, lambda: mon.state("a") == pm.RUNNING and mon.state("b") == pm.RUNNING)
    procs = [r.proc for r in mon._runs.values()]
    pids = [int(p.processId()) for p in procs if p is not None]
    mon.release()
    assert mon.state("a") == pm.IDLE
    for pid in pids:
        os.kill(pid, 0)  # still alive: closing the app does not kill the games
        os.kill(pid, 15)
    for p in procs:
        if p is not None:
            p.waitForFinished(3000)
    pm._RELEASED.clear()


def test_prepare_and_cancel(mon: pm.PlayMonitor) -> None:
    mon.prepare("g", "Game")
    assert mon.state("g") == pm.LOADING
    mon.cancel("g")
    assert mon.state("g") == pm.IDLE


def test_find_game_pids_matches_cmdline_cwd_and_prefix(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    game = "/home/deck/Games/Hollow"
    for pid, cmd, env in (
        ("10", f"bash\0{game}/launch.sh\0", b""),
        ("11", "Z:\\home\\deck\\Games\\Hollow\\game.exe\0", b""),
        ("12", "wineserver\0", b"WINEPREFIX=/home/deck/Games/Hollow/pfx\0"),
        ("13", "bash\0", b"PWD=/home/deck/Games/Hollow\0"),
    ):
        (root / pid).mkdir(parents=True)
        (root / pid / "cmdline").write_text(cmd)
        (root / pid / "environ").write_bytes(env)
    assert pm.find_game_pids((game,), str(root)) == {10, 11, 12}


def test_watch_paths_skip_system_and_home() -> None:
    assert pm.watch_paths("/usr/bin", str(Path.home()), None, '"/home/deck/Games/X"') == ("/home/deck/Games/X",)


def test_play_button_follows_selected_game_state(tmp_path: Path) -> None:
    from unittest import mock

    from PySide6.QtCore import QSettings

    from deckinstall import ui
    from deckinstall.i18n import tr
    from deckinstall.standalone import StandaloneGame

    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(tmp_path / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
    ):
        win = ui.MainWindow(app)
        try:
            game = mock.Mock(spec=StandaloneGame)
            game.game_dir = tmp_path / "G"
            sel = ("standalone", game)
            key = win._play_key(*sel)
            win._update_play_button(sel)
            assert win.btn_play_game.text() == tr("library.btn_play") and win.btn_play_game.isEnabled()
            win._play.prepare(key, "G")
            win._update_play_button(sel)
            assert win.btn_play_game.text().startswith(tr("play.btn_loading"))
            assert not win.btn_play_game.isEnabled()
            win._play._runs[key].state = pm.RUNNING
            win._update_play_button(sel)
            assert win.btn_play_game.text() == tr("play.btn_stop") and win.btn_play_game.isEnabled()
            other = mock.Mock(spec=StandaloneGame)
            other.game_dir = tmp_path / "H"
            win._update_play_button(("standalone", other))  # another game selected: its own state
            assert win.btn_play_game.text() == tr("library.btn_play")
            win._play._runs[key].state = pm.ERROR
            win._update_play_button(sel)
            assert win.btn_play_game.text() == tr("play.btn_error")
        finally:
            win._play.release()
            for worker in list(win._active_workers):
                worker.wait(5000)
            win.close()
            win.deleteLater()
            app.processEvents()
