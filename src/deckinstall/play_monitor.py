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
# System folders (whole path components): /var/home (Fedora Atomic, Bazzite) is a user folder.
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/opt", "/etc", "/var", "/proc", "/sys", "/dev", "/tmp")


_NON_GAME_CWD_BINARIES = frozenset(
    {
        "bash",
        "sh",
        "dash",
        "zsh",
        "fish",
        "dolphin",
        "nautilus",
        "thunar",
        "nemo",
        "pcmanfm",
        "konsole",
        "kitty",
        "alacritty",
        "wezterm",
        "gnome-terminal",
        "xfce4-terminal",
        "kate",
        "kwrite",
        "gedit",
        "code",
        "codium",
        "vim",
        "nvim",
        "nano",
        "emacs",
        "python",
        "python3",
    }
)


def _home_roots() -> tuple[str, ...]:
    home = str(Path.home())
    try:
        resolved = str(Path.home().resolve())
    except OSError:
        resolved = home
    return (home,) if resolved == home else (home, resolved)


def _is_system_path(path: str, home: str | tuple[str, ...]) -> bool:
    homes = (home,) if isinstance(home, str) else home
    if any(path == h or path.startswith(h + "/") for h in homes):
        return False
    return any(path == root or path.startswith(root + "/") for root in _SYSTEM_ROOTS)
_ENV_KEYS = (b"WINEPREFIX=", b"STEAM_COMPAT_DATA_PATH=")
_READ_MAX = 256 * 1024
_LOG_TAIL_LINES = 20
# Released on close: kept referenced so nothing deletes (and so kills) them before os._exit.
_RELEASED: list[QProcess] = []


def watch_paths(*candidates: object) -> tuple[str, ...]:
    """The folders that identify a game's processes (system folders and $HOME itself are left out)."""
    homes = _home_roots()
    out: list[str] = []
    for cand in candidates:
        if not cand:
            continue
        text = str(cand).strip().strip('"')
        if not text.startswith("/"):
            continue
        path = os.path.normpath(text)
        if path in homes or len(Path(path).parts) < 3 or _is_system_path(path, homes):
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


def _cmd_has_path_prefix(raw_cmd: bytes, prefix: bytes) -> bool:
    """Match prefix inside cmdline args on a path boundary (not '/Games/Portal' inside '/Games/Portal 2')."""
    if not prefix:
        return False
    plen = len(prefix)
    for arg in raw_cmd.split(b"\0"):
        if not arg:
            continue
        alen = len(arg)
        start = 0
        while True:
            idx = arg.find(prefix, start)
            if idx < 0:
                break
            end = idx + plen
            before_ok = idx == 0 or arg[idx - 1 : idx] in (b"=", b'"', b"'", b":", b" ")
            after_ok = end == alen or arg[end : end + 1] in (b"/", b"\\", b'"', b"'")
            if before_ok and after_ok:
                return True
            start = idx + 1
    return False


def _is_interactive_host_cwd_only(raw_cmd: bytes, unix: list[bytes]) -> bool:
    first = raw_cmd.split(b"\0", 1)[0].strip(b'"').strip(b"'")
    if not first:
        return False
    if any(first == u or first.startswith(u + b"/") for u in unix):
        return False
    low = raw_cmd.lower()
    if any(tok in low for tok in (b"wine", b"proton", b"umu", b".exe", b"pressure-vessel", b"reaper")):
        return False
    base = os.path.basename(first.decode("utf-8", errors="ignore")).lower()
    return base in _NON_GAME_CWD_BINARIES


def find_game_pids(watch: tuple[str, ...], proc_root: str = "/proc") -> set[int]:
    """PIDs (other than this app) whose cmdline, cwd or Wine prefix points inside a watched folder."""
    if not watch:
        return set()
    unix = [w.encode() for w in watch if w and w != "/"]
    if not unix:
        return set()
    dos = [("z:" + w.replace("/", "\\")).lower().encode() for w in watch if w and w != "/"]
    ignored_env_roots = {b"", b"/"}
    for h in _home_roots():
        ignored_env_roots.add(h.rstrip("/").encode())
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
        raw_cmd = _read(f"{base}/cmdline")
        raw_cmd_low = raw_cmd.lower()
        if any(_cmd_has_path_prefix(raw_cmd, u) for u in unix) or any(
            _cmd_has_path_prefix(raw_cmd_low, d) for d in dos
        ):
            found.add(pid)
            continue
        try:
            cwd = os.readlink(f"{base}/cwd").encode()
        except OSError:
            cwd = b""
        if (
            cwd
            and any(cwd == u or cwd.startswith(u + b"/") for u in unix)
            and not _is_interactive_host_cwd_only(raw_cmd, unix)
        ):
            found.add(pid)
            continue
        for var in _read(f"{base}/environ").split(b"\0"):
            if var.startswith(_ENV_KEYS):  # WINEPREFIX and STEAM_COMPAT_DATA_PATH: both are checked
                value = var.split(b"=", 1)[1].rstrip(b"/")
                if not value or value in ignored_env_roots:
                    continue
                if any(value == u or value.startswith(u + b"/") or u.startswith(value + b"/") for u in unix):
                    found.add(pid)
                    break
    return found


def _exe_comm_bytes(exe_name: str) -> bytes:
    """Truncate exe_name to Linux TASK_COMM_LEN - 1 (15 bytes) on UTF-8 boundary."""
    raw = exe_name.encode("utf-8", errors="ignore")[:15]
    return raw


def exe_process_state(exe_name: str, proc_root: str = "/proc") -> tuple[bool, bool]:
    """(alive, zombie): processes named like the game's .exe (Wine sets comm to its first 15 bytes)."""
    comm = _exe_comm_bytes(exe_name)
    alive = zombie = False
    try:
        entries = [e.name for e in os.scandir(proc_root) if e.name.isdigit()]
    except OSError:
        return False, False
    for name in entries:
        stat = _read(f"{proc_root}/{name}/stat")
        start, end = stat.find(b"("), stat.rfind(b")")
        if start < 0 or end < 0 or stat[start + 1:end] != comm:
            continue
        if stat[end + 2:end + 3] == b"Z":
            zombie = True
        else:
            alive = True
    return alive, zombie


def exe_pids(exe_name: str, proc_root: str = "/proc") -> set[int]:
    """Live (not zombie) processes named like the game's .exe (Wine sets comm to its first 15 bytes)."""
    if not exe_name:
        return set()
    comm = _exe_comm_bytes(exe_name)
    found: set[int] = set()
    try:
        entries = [e.name for e in os.scandir(proc_root) if e.name.isdigit()]
    except OSError:
        return found
    for name in entries:
        stat = _read(f"{proc_root}/{name}/stat")
        start, end = stat.find(b"("), stat.rfind(b")")
        if start >= 0 and end >= 0 and stat[start + 1:end] == comm and stat[end + 2:end + 3] != b"Z":
            found.add(int(name))
    return found


def process_start_epoch(pid: int, proc_root: str = "/proc") -> float | None:
    """When a process started (seconds since the epoch), from /proc/<pid>/stat and the boot time."""
    stat = _read(f"{proc_root}/{pid}/stat")
    end = stat.rfind(b")")
    try:
        ticks = int(stat[end + 2:].split()[19])
        btime = next(int(line.split()[1]) for line in _read(f"{proc_root}/stat").splitlines()
                     if line.startswith(b"btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError, StopIteration, OSError):
        return None


def stale_instance_pids(exe_name: str, watch: tuple[str, ...], proc_root: str = "/proc") -> set[int]:
    """Processes of a game instance already running (its .exe alive inside the watched folders).

    Proton launches with "waitforexitandrun": a new launch waits, without any window, until the
    old instance of the same prefix exits. Empty when the game's .exe is not running.
    """
    alive = exe_pids(exe_name, proc_root)
    if not alive:
        return set()
    game = find_game_pids(watch, proc_root)
    return game if alive & game else set()


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
    exe_name: str = ""
    zombie_since: float | None = None
    hung: bool = False
    attempt: int = 0
    old_exe_pids: set[int] = field(default_factory=set)  # the .exe already running before this launch


class PlayMonitor(QObject):
    """Launches games and keeps a Play/Loading/Running/Error state per game key."""

    changed = Signal(str)  # key: state (or the Loading animation frame) changed
    failed = Signal(str, str, str)  # key, game name, diagnosis (message + log tail)
    timed_out = Signal(str, str, bool)  # key, game name, steam shortcut

    FAST_FAIL_S = 20.0  # exiting with an error before this is a failed launch
    LOADING_TIMEOUT_S = 60.0  # Loading without any sign of life
    PREPARE_TIMEOUT_S = 600.0  # launch command still being prepared (a late answer is then ignored)
    RUNNING_AFTER_S = 3.0  # our process alive this long: the game is running
    CHILD_GRACE_S = 15.0  # after a quick exit 0, how long to look for the real game process
    GONE_TICKS = 4  # consecutive empty /proc scans before a followed game counts as closed
    HUNG_AFTER_S = 10.0  # game .exe dead (zombie) but Proton/umu still up this long: it hung after a crash
    TICK_MS = 500

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._runs: dict[str, _Run] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.frame = 0
        self._attempt = 0
        self.clock = time.monotonic
        self.scan = find_game_pids
        self.exe_state = exe_process_state
        self.exe_pids = exe_pids

    def state(self, key: str) -> str:
        run = self._runs.get(key)
        return run.state if run is not None else IDLE

    def prepare(self, key: str, name: str) -> int:
        """Show Loading while the launch command is being prepared; returns this attempt's id."""
        self._attempt += 1
        self._runs[key] = _Run(name, LOADING, self.clock(), preparing=True, attempt=self._attempt)
        self._changed(key)
        return self._attempt

    def is_preparing(self, key: str, attempt: int) -> bool:
        """True while this prepare() attempt is still the one shown (a late result must not launch)."""
        run = self._runs.get(key)
        return run is not None and run.preparing and run.attempt == attempt

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
        exe_name: str = "",
    ) -> None:
        """Start cmd and follow it (log_file: where its output goes; log_path: the log shown on errors).

        exe_name: the game's .exe file name, to notice a game that died while Proton stays hung.
        """
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
        self._runs[key] = _Run(
            name, LOADING, self.clock(), watch, log_path or log_file, proc, exe_name=exe_name,
            old_exe_pids=self.exe_pids(exe_name),
        )
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
        self._terminate(run)

    def _terminate(self, run: _Run) -> None:
        """SIGTERM to our session and to every process found for the game."""
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

    def kill_pids(self, pids: set[int], grace_ms: int = 3000) -> None:
        """SIGTERM now, SIGKILL to whatever is left after grace_ms (guarding against PID reuse)."""
        epochs = {pid: process_start_epoch(pid) for pid in pids}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        def _kill() -> None:
            for pid, orig_epoch in epochs.items():
                if orig_epoch is not None:
                    cur_epoch = process_start_epoch(pid)
                    if cur_epoch is None or cur_epoch != orig_epoch:
                        continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        QTimer.singleShot(grace_ms, self, _kill)

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
        if run.state == ERROR:  # already diagnosed (blocked by an old instance): nothing more to say
            return
        if run.hung:
            self._fail(key, tr("play.hung"))
            return
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

    def _started(self, run: _Run, now: float) -> bool:
        """Running: without a known .exe, our process alive RUNNING_AFTER_S; with one, a new .exe process."""
        if now - run.started < self.RUNNING_AFTER_S:
            return False
        if not run.exe_name:
            return True
        return bool(self.exe_pids(run.exe_name) - run.old_exe_pids)

    def _has_live_child_exe(self, run: _Run, proc_root: str = "/proc") -> bool:
        """True if another live Windows .exe runs inside run.watch (bootstrapper handed off to real exe)."""
        if not run.watch:
            return False
        for pid in self.scan(run.watch):
            if pid in run.old_exe_pids:
                continue
            stat = _read(f"{proc_root}/{pid}/stat")
            end = stat.rfind(b")")
            if end < 0 or stat[end + 2 : end + 3] == b"Z":
                continue
            cmd_low = _read(f"{proc_root}/{pid}/cmdline").replace(b"\0", b" ").lower()
            if b".exe" in cmd_low and b"proton" not in cmd_low and b"wineserver" not in cmd_low:
                return True
        return False

    def _check_hung(self, run: _Run, now: float) -> None:
        """The game .exe is a zombie and nothing alive has its name: Proton hung after the game died."""
        alive, zombie = self.exe_state(run.exe_name)
        if not zombie or alive or self._has_live_child_exe(run):
            run.zombie_since = None
            return
        if run.zombie_since is None:
            run.zombie_since = now
        elif now - run.zombie_since >= self.HUNG_AFTER_S:
            run.hung = True
            self._terminate(run)  # nothing of the game is left to lose; finished() then shows the error

    def _tick(self) -> None:
        self.frame += 1
        now = self.clock()
        for key, run in list(self._runs.items()):
            self._tick_run(key, run, now)

    def _tick_run(self, key: str, run: _Run, now: float) -> None:
        if run.state == ERROR:
            return
        if run.preparing:
            # Preparing can download (the official MangoHud): only a worker that never answered times out.
            if now - run.started >= self.PREPARE_TIMEOUT_S:
                self._finish(key)
            else:
                self.changed.emit(key)
            return
        if run.proc is not None:  # our process is alive
            if run.state == LOADING and self._started(run, now):
                run.state = RUNNING
                self._changed(key)
            elif run.state == LOADING and run.exe_name and now - run.started >= self.LOADING_TIMEOUT_S:
                old = run.old_exe_pids & self.exe_pids(run.exe_name)
                if old:  # Proton is waiting for the old instance: this launch never shows anything
                    self._fail(key, tr("play.blocked_by_old", pids=", ".join(map(str, sorted(old)))))
                    if run.proc is not None and run.proc.state() != QProcess.ProcessState.NotRunning:
                        try:
                            os.killpg(int(run.proc.processId()), signal.SIGTERM)
                        except OSError:
                            run.proc.terminate()
                else:  # the .exe never showed under its name (a launcher exe): trust our process
                    run.state = RUNNING
                    self._changed(key)
            elif run.state == LOADING:
                self.changed.emit(key)
            elif run.exe_name and not run.hung and not run.stopping:
                self._check_hung(run, now)
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
