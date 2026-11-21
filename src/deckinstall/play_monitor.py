"""Play button state per game: Loading -> Running -> back to Play, or Error with the log tail.

Each launch is a QProcess in its own session (closing DeckInstall never kills the game; the app
exits with os._exit, so no QProcess destructor runs). A launcher that exits 0 right away (umu,
Heroic, a launch.sh that forks) is followed through /proc: any process whose command line, cwd
or WINEPREFIX points at the game's folders counts as the game. Steam shortcuts (steam://) have
no process of ours and are only followed through /proc.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .i18n import tr

IDLE, LOADING, RUNNING, ERROR = "idle", "loading", "running", "error"
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/opt", "/etc", "/var", "/proc", "/sys", "/dev", "/tmp")
_ENV_KEYS = (b"WINEPREFIX=", b"STEAM_COMPAT_DATA_PATH=")
_READ_MAX = 256 * 1024
_LOG_TAIL_LINES = 20
# Released on close: kept referenced so nothing deletes (and so kills) them before os._exit.
_RELEASED: list[QProcess] = []


def watch_paths(*candidates: object) -> tuple[str, ...]:
    """The folders that identify a game's processes (system folders and $HOME itself are left out)."""
    home = str(Path.home())
    out: list[str] = []
    for cand in candidates:
        if not cand:
            continue
        text = str(cand).strip().strip('"')
        if not text.startswith("/"):
            continue
        path = os.path.normpath(text)
        if path == home or len(Path(path).parts) < 3 or path.startswith(_SYSTEM_ROOTS):
            continue
        if path not in out:
            out.append(path)
    return tuple(out)


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(_READ_MAX)
    except OSError:
        return b""


def find_game_pids(watch: tuple[str, ...], proc_root: str = "/proc") -> set[int]:
    """PIDs (other than this app) whose cmdline, cwd or Wine prefix points inside a watched folder."""
    if not watch:
        return set()
    unix = [w.encode() for w in watch]
    dos = [("z:" + w.replace("/", "\\")).lower().encode() for w in watch]
    me = os.getpid()
    found: set[int] = set()
    try:
        entries = [e.name for e in os.scandir(proc_root) if e.name.isdigit()]
    except OSError:
        return found
    for name in entries:
        pid = int(name)
        if pid == me:
            continue
        base = f"{proc_root}/{name}"
        cmd = _read(f"{base}/cmdline").replace(b"\0", b" ")
        if any(u in cmd for u in unix) or any(d in cmd.lower() for d in dos):
            found.add(pid)
            continue
        try:
            cwd = os.readlink(f"{base}/cwd").encode()
        except OSError:
            cwd = b""
        if cwd and any(cwd == u or cwd.startswith(u + b"/") for u in unix):
            found.add(pid)
            continue
        for var in _read(f"{base}/environ").split(b"\0"):
            if var.startswith(_ENV_KEYS):
                value = var.split(b"=", 1)[1].rstrip(b"/")
                if any(value == u or value.startswith(u + b"/") or u.startswith(value + b"/") for u in unix):
                    found.add(pid)
                break
    return found


def log_tail(path: Path | None, lines: int = _LOG_TAIL_LINES) -> str:
    """The last lines of a run log ("" when there is none)."""
    if path is None:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 16 * 1024))
            data = fh.read()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])


@dataclass
class _Run:
    name: str
    state: str
    started: float
    watch: tuple[str, ...] = ()
    log_path: Path | None = None
    proc: QProcess | None = None
    preparing: bool = False
    steam: bool = False
    exited_at: float | None = None
    stopping: bool = False
    misses: int = 0
    pids: set[int] = field(default_factory=set)


class PlayMonitor(QObject):
    """Launches games and keeps a Play/Loading/Running/Error state per game key."""

    changed = Signal(str)  # key: state (or the Loading animation frame) changed
    failed = Signal(str, str, str)  # key, game name, diagnosis (message + log tail)
    timed_out = Signal(str, str, bool)  # key, game name, steam shortcut

    FAST_FAIL_S = 20.0  # exiting with an error before this is a failed launch
    LOADING_TIMEOUT_S = 60.0  # Loading without any sign of life
    RUNNING_AFTER_S = 3.0  # our process alive this long: the game is running
    CHILD_GRACE_S = 15.0  # after a quick exit 0, how long to look for the real game process
    GONE_TICKS = 4  # consecutive empty /proc scans before a followed game counts as closed
    TICK_MS = 500

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._runs: dict[str, _Run] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.frame = 0
        self.clock = time.monotonic
        self.scan = find_game_pids

    def state(self, key: str) -> str:
        run = self._runs.get(key)
        return run.state if run is not None else IDLE

    def prepare(self, key: str, name: str) -> None:
        """Show Loading while the launch command is being prepared."""
        self._runs[key] = _Run(name, LOADING, self.clock(), preparing=True)
        self._changed(key)

    def cancel(self, key: str) -> None:
        """The launch did not happen (prepare failed or was refused): back to Play."""
        run = self._runs.get(key)
        if run is not None and run.preparing:
            self._finish(key)

    def launch(
        self,
        key: str,
        name: str,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        log_file: Path | None = None,
        watch: tuple[str, ...] = (),
        log_path: Path | None = None,
    ) -> None:
        """Start cmd and follow it (log_file: where its output goes; log_path: the log shown on errors)."""
        proc = QProcess(self)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        if cwd is not None:
            proc.setWorkingDirectory(str(cwd))
        if env is not None:
            qenv = QProcessEnvironment()
            for k, v in env.items():
                qenv.insert(k, v)
            proc.setProcessEnvironment(qenv)
        out = os.devnull  # never leave a pipe to us: a full pipe would block the game
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_bytes(b"")  # this run only; both streams then append to it
                out = str(log_file)
            except OSError:
                pass
        proc.setStandardInputFile(QProcess.nullDevice())
        proc.setStandardOutputFile(out, QProcess.OpenModeFlag.Append)
        proc.setStandardErrorFile(out, QProcess.OpenModeFlag.Append)
        proc.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        self._runs[key] = _Run(name, LOADING, self.clock(), watch, log_path or log_file, proc)
        proc.errorOccurred.connect(lambda err, k=key, p=proc: self._on_error(k, p, err))
        proc.finished.connect(lambda code, status, k=key, p=proc: self._on_finished(k, p, code, status))
        proc.started.connect(lambda k=key: self._changed(k))
        proc.start()
        self._changed(key)

    def watch_only(self, key: str, name: str, watch: tuple[str, ...]) -> None:
        """A game started by someone else (Steam): Loading until its process shows up in /proc."""
        self._runs[key] = _Run(name, LOADING, self.clock(), watch, steam=True, exited_at=self.clock())
        self._changed(key)

    def stop(self, key: str) -> None:
        """Ask the running game to close (SIGTERM to its session and the processes found for it)."""
        run = self._runs.get(key)
        if run is None or run.state != RUNNING:
            return
        run.stopping = True
        pids = set(run.pids) | self.scan(run.watch)
        if run.proc is not None and run.proc.state() != QProcess.ProcessState.NotRunning:
            pid = int(run.proc.processId())
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                run.proc.terminate()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def acknowledge(self, key: str) -> None:
        """The error was shown: back to Play."""
        if self.state(key) == ERROR:
            self._finish(key)

    def release(self) -> None:
        """Stop monitoring (the app is closing): running games keep running."""
        self._timer.stop()
        for run in self._runs.values():
            if run.proc is not None:
                run.proc.blockSignals(True)
                if run.proc.state() != QProcess.ProcessState.NotRunning:
                    run.proc.setParent(None)
                    _RELEASED.append(run.proc)
        self._runs.clear()

    # -- internals -------------------------------------------------------------

    def _changed(self, key: str) -> None:
        busy = any(r.state in (LOADING, RUNNING) for r in self._runs.values())
        if busy and not self._timer.isActive():
            self._timer.start()
        elif not busy and self._timer.isActive():
            self._timer.stop()
        self.changed.emit(key)

    def _finish(self, key: str) -> None:
        self._runs.pop(key, None)
        self._changed(key)

    def _fail(self, key: str, message: str) -> None:
        run = self._runs.get(key)
        if run is None:
            return
        run.state = ERROR
        tail = log_tail(run.log_path)
        diag = message + (("\n\n" + tr("play.log_tail", path=run.log_path) + "\n" + tail) if tail else "")
        self._changed(key)
        self.failed.emit(key, run.name, diag)

    def _on_error(self, key: str, proc: QProcess, err: QProcess.ProcessError) -> None:
        run = self._runs.get(key)
        if run is None or run.proc is not proc or err != QProcess.ProcessError.FailedToStart:
            return
        run.proc = None
        proc.deleteLater()
        self._fail(key, tr("play.failed_to_start", err=proc.errorString()))

    def _on_finished(self, key: str, proc: QProcess, code: int, status: QProcess.ExitStatus) -> None:
        run = self._runs.get(key)
        if run is None or run.proc is not proc:
            return
        run.proc = None
        proc.deleteLater()
        now = self.clock()
        if run.stopping:
            self._finish(key)
            return
        failed = code != 0 or status == QProcess.ExitStatus.CrashExit
        quick = now - run.started < self.FAST_FAIL_S
        if failed and quick:
            self._fail(key, tr("play.exit_error", code=code if status != QProcess.ExitStatus.CrashExit else "crash"))
        elif failed or not quick:
            self._finish(key)  # played for a while, then closed (or crashed later): back to Play
        else:
            run.exited_at = now  # quick exit 0: look for the real game process
            self._tick_run(key, run, now)

    def _tick(self) -> None:
        self.frame += 1
        now = self.clock()
        for key, run in list(self._runs.items()):
            self._tick_run(key, run, now)

    def _tick_run(self, key: str, run: _Run, now: float) -> None:
        if run.state == ERROR:
            return
        if run.preparing:
            if now - run.started >= self.LOADING_TIMEOUT_S:
                self._finish(key)
            else:
                self.changed.emit(key)
            return
        if run.proc is not None:  # our process is alive
            if run.state == LOADING and now - run.started >= self.RUNNING_AFTER_S:
                run.state = RUNNING
                self._changed(key)
            elif run.state == LOADING:
                self.changed.emit(key)
            return
        pids = self.scan(run.watch)
        if pids:
            run.pids, run.misses = pids, 0
            if run.state == LOADING:
                run.state = RUNNING
                self._changed(key)
            return
        if run.state == RUNNING:
            run.misses += 1
            if run.misses >= self.GONE_TICKS or run.stopping:
                self._finish(key)
            return
        exited = run.exited_at if run.exited_at is not None else run.started
        if now - run.started >= self.LOADING_TIMEOUT_S or (not run.steam and now - exited >= self.CHILD_GRACE_S):
            if not run.steam and exited - run.started < self.FAST_FAIL_S:
                self._fail(key, tr("play.closed_at_once"))
                return
            self._finish(key)
            self.timed_out.emit(key, run.name, run.steam)
            return
        self.changed.emit(key)
