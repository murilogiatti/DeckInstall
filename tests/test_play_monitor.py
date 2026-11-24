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
    first_pid = os.getpid() + 1
    for pid, cmd, env in (
        (first_pid, f"bash\0{game}/launch.sh\0", b""),
        (first_pid + 1, "Z:\\home\\deck\\Games\\Hollow\\game.exe\0", b""),
        (first_pid + 2, "wineserver\0", b"WINEPREFIX=/home/deck/Games/Hollow/pfx\0"),
        (first_pid + 3, "bash\0", b"PWD=/home/deck/Games/Hollow\0"),
    ):
        proc = root / str(pid)
        proc.mkdir(parents=True)
        (proc / "cmdline").write_text(cmd)
        (proc / "environ").write_bytes(env)
    assert pm.find_game_pids((game,), str(root)) == {first_pid, first_pid + 1, first_pid + 2}


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


def test_exe_process_state_reads_comm_and_zombie(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    for pid, stat in (("20", "20 (Returnal-Win64-) Z 1 1"), ("21", "21 (bash) S 1 1"), ("22", "22 (Other (x)) S 1")):
        (root / pid).mkdir(parents=True)
        (root / pid / "stat").write_text(stat)
    assert pm.exe_process_state("Returnal-Win64-Shipping.exe", str(root)) == (False, True)
    (root / "23").mkdir()
    (root / "23" / "stat").write_text("23 (Returnal-Win64-) S 1 1")
    assert pm.exe_process_state("Returnal-Win64-Shipping.exe", str(root)) == (True, True)
    assert pm.exe_process_state("Nope.exe", str(root)) == (False, False)


def test_game_dead_but_proton_hung_is_an_error_and_cleaned_up(mon: pm.PlayMonitor) -> None:
    mon.HUNG_AFTER_S = 0.3
    dead = {"on": False}
    mon.exe_state = lambda name: (False, True) if dead["on"] else (True, False)
    mon.scan = lambda watch: set()
    mon.launch("g", "Game", ["sleep", "30"], exe_name="Game.exe")
    _wait(mon, lambda: mon.state("g") == pm.RUNNING)
    pid = int(mon._runs["g"].proc.processId())
    dead["on"] = True  # the game crashed; its launcher (sleep here) stays up like a hung Proton
    _wait(mon, lambda: mon.state("g") == pm.ERROR)
    (_k, _n, diag), = mon.events["failed"]  # type: ignore[attr-defined]
    assert diag.startswith(pm.tr("play.hung"))
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split(") ")[1][0] == "Z"


def test_watch_paths_keep_var_home(monkeypatch) -> None:
    """Fedora Atomic / Bazzite keep home in /var/home: those games are followed too."""
    monkeypatch.setenv("HOME", "/var/home/deck")
    assert pm.watch_paths("/var/home/deck/Games/X", "/var/lib/foo", "/optical/G", "/usr/share/x") == (
        "/var/home/deck/Games/X",
        "/optical/G",
    )


def test_late_prepare_answer_is_not_a_launch(mon: pm.PlayMonitor) -> None:
    first = mon.prepare("g", "Game")
    assert mon.is_preparing("g", first)
    mon.cancel("g")
    second = mon.prepare("g", "Game")
    assert not mon.is_preparing("g", first) and mon.is_preparing("g", second)


def test_compat_data_path_after_another_wineprefix(tmp_path: Path) -> None:
    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"wine\0game.exe\0")
    (proc / "environ").write_bytes(b"WINEPREFIX=/elsewhere/pfx\0STEAM_COMPAT_DATA_PATH=/data/compat/1\0")
    assert pm.find_game_pids(("/data/compat/1",), str(tmp_path / "proc")) == {4242}


def test_stale_instance_needs_the_exe_alive_in_the_game_folders(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    game = "/home/deck/Games/Returnal"
    procs = {
        "30": ("30 (Returnal-Win64-) S 1 1", "X:\\Games\\Returnal-Win64-Shipping.exe", f"WINEPREFIX={game}/pfx\0".encode()),
        "31": ("31 (wineserver) S 1 1", "wineserver", f"WINEPREFIX={game}/pfx\0".encode()),
        "32": ("32 (Returnal-Win64-) Z 1 1", "", b""),
    }
    for pid, (stat, cmd, env) in procs.items():
        (root / pid).mkdir(parents=True)
        (root / pid / "stat").write_text(stat)
        (root / pid / "cmdline").write_text(cmd)
        (root / pid / "environ").write_bytes(env)
    assert pm.exe_pids("Returnal-Win64-Shipping.exe", str(root)) == {30}  # the zombie does not count
    assert pm.stale_instance_pids("Returnal-Win64-Shipping.exe", (game,), str(root)) == {30, 31}
    (root / "30" / "stat").write_text("30 (Returnal-Win64-) Z 1 1")
    # Only a leftover wineserver (it lingers a few seconds after a normal exit): no stale instance.
    assert pm.stale_instance_pids("Returnal-Win64-Shipping.exe", (game,), str(root)) == set()


def test_process_start_epoch_of_this_process() -> None:
    started = pm.process_start_epoch(os.getpid())
    assert started is not None and started <= time.time() + 1


def test_running_only_when_a_new_exe_process_appears(mon: pm.PlayMonitor) -> None:
    pids = {"set": {50}}
    mon.exe_pids = lambda name: set(pids["set"])
    mon.scan = lambda watch: set()
    mon.LOADING_TIMEOUT_S = 30.0
    mon.launch("g", "Game", ["sleep", "30"], exe_name="Game.exe")
    time.sleep(0.6)
    mon.app.processEvents()  # type: ignore[attr-defined]
    assert mon.state("g") == pm.LOADING  # launcher alive, but only the old .exe exists
    pids["set"] = {50, 51}
    _wait(mon, lambda: mon.state("g") == pm.RUNNING)
    mon.stop("g")


def test_launch_blocked_by_old_instance_is_an_error(mon: pm.PlayMonitor) -> None:
    mon.exe_pids = lambda name: {50}  # the old instance never goes away; no new .exe appears
    mon.scan = lambda watch: set()
    mon.LOADING_TIMEOUT_S = 0.5
    mon.launch("g", "Game", ["sleep", "30"], exe_name="Game.exe")
    _wait(mon, lambda: mon.state("g") == pm.ERROR)
    (_k, _n, diag), = mon.events["failed"]  # type: ignore[attr-defined]
    assert diag.startswith(pm.tr("play.blocked_by_old", pids="50"))


def test_session_length_reported_when_a_running_game_ends(mon: pm.PlayMonitor) -> None:
    sessions: list[tuple[str, float]] = []
    mon.session_ended.connect(lambda k, s: sessions.append((k, s)))
    mon.launch("g", "Game", ["sleep", "30"])
    _wait(mon, lambda: mon.state("g") == pm.RUNNING)
    mon.stop("g")
    _wait(mon, lambda: mon.state("g") == pm.IDLE)
    assert len(sessions) == 1 and sessions[0][0] == "g" and sessions[0][1] > 0
