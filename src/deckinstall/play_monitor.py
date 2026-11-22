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
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
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
_WINE_BACKGROUND_EXES = {
    b"services.exe", b"winedevice.exe", b"plugplay.exe", b"rpcss.exe", b"svchost.exe", b"explorer.exe",
}
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
        normalized = os.path.normpath(text)
        if normalized in homes or len(Path(normalized).parts) < 3 or _is_system_path(normalized, homes):
            continue
        resolved = os.path.realpath(normalized)
        for path in (normalized, resolved):
            if (
                path not in out and path not in homes
                and len(Path(path).parts) >= 3 and not _is_system_path(path, homes)
            ):
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


@dataclass
class _ProcessSnapshot:
    matches: dict[tuple[str, ...], set[int]]
    states: dict[bytes, list[tuple[int, bool, int]]]
    child_exes: set[int]

    def exe_pids(self, name: str) -> set[int]:
        return {pid for pid, zombie, _ in self.states.get(_exe_comm_bytes(name), ()) if not zombie}

    def exe_state(self, name: str, scope: set[int] | None = None, session_id: int = 0) -> tuple[bool, bool]:
        states = {
            zombie for pid, zombie, session in self.states.get(_exe_comm_bytes(name), ())
            if scope is None or pid in scope or (session_id != 0 and session == session_id)
        }
        return False in states, True in states


_PROC_ROOT = "/proc"
_PROC_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deckinstall-proc")
_RELEVANT_ENV_KEYS = frozenset(key.rstrip(b"=") for key in _ENV_KEYS)


def _scan_processes(
    watches: set[tuple[str, ...]], names: set[str], proc_root: str, *, find_children: bool = False,
) -> _ProcessSnapshot:
    """Read each PID's data once, matching it to all watched folders and executable names."""
    snapshot = _ProcessSnapshot({watch: set() for watch in watches}, {}, set())
    encoded = {}
    for watch in watches:
        unix = [folder.encode() for folder in watch if folder and folder != "/"]
        if unix:
            encoded[watch] = (unix, [b"z:" + value.replace(b"/", b"\\").lower() for value in unix])
    exe_names = {_exe_comm_bytes(name) for name in names if name}
    ignored_roots = {b"", b"/", *(home.rstrip("/").encode() for home in _home_roots())}
    own_pid = os.getpid() if proc_root == "/proc" else -1
    try:
        with os.scandir(proc_root) as listing:
            pids = [entry.name for entry in listing if entry.name.isascii() and entry.name.isdigit()]
    except OSError:
        return snapshot
    for name in pids:
        pid = int(name)
        if pid == own_pid:
            continue
        base = f"{proc_root}/{name}"
        comm = b""
        zombie = False
        if exe_names or find_children:
            proc_stat = _read(f"{base}/stat")
            start, end = proc_stat.find(b"("), proc_stat.rfind(b")")
            if start >= 0 and end > start:
                comm = proc_stat[start + 1:end]
                fields = proc_stat[end + 2:].split(None, 4)
                zombie = fields[:1] == [b"Z"]
                if comm in exe_names:
                    session = int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else 0
                    snapshot.states.setdefault(comm, []).append((pid, zombie, session))
        if not encoded:
            continue
        cmd = _read(f"{base}/cmdline")
        if not cmd:
            continue
        low = cmd.lower()
        if find_children and comm and not zombie and comm.lower() not in _WINE_BACKGROUND_EXES:
            if b".exe" in low and b"proton" not in low and b"wineserver" not in low:
                snapshot.child_exes.add(pid)
        cwd: bytes | None = None
        prefixes: list[bytes] | None = None
        for watch, (unix, dos) in encoded.items():
            if any(_cmd_has_path_prefix(cmd, u) for u in unix) or any(_cmd_has_path_prefix(low, d) for d in dos):
                snapshot.matches[watch].add(pid)
                continue
            if cwd is None:
                try:
                    cwd = os.fsencode(os.readlink(f"{base}/cwd"))
                except OSError:
                    cwd = b""
            if cwd and any(cwd == u or cwd.startswith(u + b"/") for u in unix):
                if not _is_interactive_host_cwd_only(cmd, unix):
                    snapshot.matches[watch].add(pid)
                    continue
            if prefixes is None:
                prefixes = []
                for field in _read(f"{base}/environ").split(b"\0"):
                    key, _, raw = field.partition(b"=")
                    prefix = raw.rstrip(b"/")
                    if key in _RELEVANT_ENV_KEYS and prefix not in ignored_roots:
                        prefixes.append(prefix)
            if any(p == u or p.startswith(u + b"/") or u == p + b"/pfx" for p in prefixes for u in unix):
                snapshot.matches[watch].add(pid)
    return snapshot


def find_game_pids(watch: tuple[str, ...], proc_root: str | None = None) -> set[int]:
    """PIDs whose cmdline, cwd or Wine prefix points inside a watched folder."""
    return _scan_processes({watch}, set(), proc_root or _PROC_ROOT).matches[watch] if watch else set()


def _exe_comm_bytes(exe_name: str) -> bytes:
    """Truncate exe_name to Linux TASK_COMM_LEN - 1 (15 bytes) on UTF-8 boundary."""
    raw = exe_name.encode("utf-8", errors="ignore")[:15]
    return raw


def exe_process_state(
    exe_name: str,
    proc_root: str | None = None,
    *,
    scope: set[int] | None = None,
    session_id: int = 0,
) -> tuple[bool, bool]:
    """(alive, zombie): processes named like the game's .exe (Wine sets comm to its first 15 bytes)."""
    comm = _exe_comm_bytes(exe_name)
    alive = zombie = False
    try:
        entries = [e.name for e in os.scandir(proc_root or _PROC_ROOT) if e.name.isdigit()]
    except OSError:
        return False, False
    for name in entries:
        stat = _read(f"{proc_root or _PROC_ROOT}/{name}/stat")
        start, end = stat.find(b"("), stat.rfind(b")")
        if start < 0 or end < 0 or stat[start + 1:end] != comm:
            continue
        if scope is not None and int(name) not in scope:
            # Exited processes lose cmdline and cwd. Their session still identifies our child.
            fields = stat[end + 2:].split(None, 4)
            if not session_id or len(fields) < 4 or fields[3] != str(session_id).encode():
                continue
        if stat[end + 2:end + 3] == b"Z":
            zombie = True
        else:
            alive = True
    return alive, zombie


def exe_pids(exe_name: str, proc_root: str | None = None) -> set[int]:
    """Live (not zombie) processes named like the game's .exe (Wine sets comm to its first 15 bytes)."""
    if not exe_name:
        return set()
    comm = _exe_comm_bytes(exe_name)
    found: set[int] = set()
    try:
        entries = [e.name for e in os.scandir(proc_root or _PROC_ROOT) if e.name.isdigit()]
    except OSError:
        return found
    for name in entries:
        stat = _read(f"{proc_root or _PROC_ROOT}/{name}/stat")
        start, end = stat.find(b"("), stat.rfind(b")")
        if start >= 0 and end >= 0 and stat[start + 1:end] == comm and stat[end + 2:end + 3] != b"Z":
            found.add(int(name))
    return found


@lru_cache(maxsize=1)
def _system_boot_time() -> int:
    return _boot_time("/proc")


def _boot_time(proc_root: str) -> int:
    return next(int(line.split()[1]) for line in _read(f"{proc_root}/stat").splitlines()
                if line.startswith(b"btime "))


def process_start_epoch(pid: int, proc_root: str = "/proc") -> float | None:
    """When a process started (seconds since the epoch), from /proc/<pid>/stat and the boot time."""
    proc_stat = _read(f"{proc_root}/{pid}/stat")
    end = proc_stat.rfind(b")")
    try:
        ticks = int(proc_stat[end + 2:].split()[19])
        btime = _system_boot_time() if proc_root == "/proc" else _boot_time(proc_root)
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError, StopIteration, OSError):
        return None


def stale_instance_pids(exe_name: str, watch: tuple[str, ...], proc_root: str | None = None) -> set[int]:
    """Processes of a game instance already running (its .exe alive inside the watched folders).

    Proton launches with "waitforexitandrun": a new launch waits, without any window, until the
    old instance of the same prefix exits. Empty when the game's .exe is not running.
    """
    if not exe_name or not watch:
        return set()
    snapshot = _scan_processes({watch}, {exe_name}, proc_root or _PROC_ROOT)
    alive = snapshot.exe_pids(exe_name)
    game = snapshot.matches[watch]
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
        self._proc_poll: Future[_ProcessSnapshot] | None = None
        self._polled_runs: dict[str, _Run] = {}
        self._proc_snapshot: _ProcessSnapshot | None = None

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
        old_exe_pids = self.exe_pids(exe_name)
        if watch and old_exe_pids:
            old_exe_pids &= self.scan(watch)
        self._runs[key] = _Run(
            name, LOADING, self.clock(), watch, log_path or log_file, proc, exe_name=exe_name,
            old_exe_pids=old_exe_pids,
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

    def kill_pids(
        self, pids: set[int], grace_ms: int = 3000, *, on_finished: Callable[[], None] | None = None,
    ) -> None:
        """SIGTERM now, SIGKILL to whatever is left after grace_ms (guarding against PID reuse)."""
        epochs = {pid: process_start_epoch(pid) for pid in pids}
        for pid, epoch in epochs.items():
            if epoch is None:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        def _kill() -> None:
            for pid, orig_epoch in epochs.items():
                if orig_epoch is None:
                    continue
                cur_epoch = process_start_epoch(pid)
                if cur_epoch is None or cur_epoch != orig_epoch:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if on_finished is not None:
                on_finished()

        QTimer.singleShot(grace_ms, self, _kill)

    def acknowledge(self, key: str) -> None:
        """The error was shown: back to Play."""
        if self.state(key) == ERROR:
            self._finish(key)

    def release(self) -> None:
        """Stop monitoring (the app is closing): running games keep running."""
        self._timer.stop()
        if self._proc_poll is not None:
            self._proc_poll.cancel()
            self._proc_poll = None
        self._polled_runs.clear()
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

    def _watch_pids(self, watch: tuple[str, ...]) -> set[int]:
        if self._proc_snapshot is not None:
            return self._proc_snapshot.matches.get(watch, set())
        return self.scan(watch)

    def _named_pids(self, name: str) -> set[int]:
        if self._proc_snapshot is not None:
            return self._proc_snapshot.exe_pids(name)
        return self.exe_pids(name)

    def _started(self, run: _Run, now: float) -> bool:
        """Running: without a known .exe, our process alive RUNNING_AFTER_S; with one, a new .exe process."""
        if now - run.started < self.RUNNING_AFTER_S:
            return False
        if not run.exe_name:
            return True
        pids = self._named_pids(run.exe_name) - run.old_exe_pids
        return bool(pids and (not run.watch or pids & self._watch_pids(run.watch)))

    def _has_live_child_exe(self, run: _Run, proc_root: str = "/proc") -> bool:
        """True if another live Windows .exe runs inside run.watch (bootstrapper handed off to real exe)."""
        if not run.watch:
            return False
        pids = self._watch_pids(run.watch) - run.old_exe_pids
        if self._proc_snapshot is not None:
            return bool(pids & self._proc_snapshot.child_exes)
        for pid in pids:
            stat = _read(f"{proc_root}/{pid}/stat")
            start, end = stat.find(b"("), stat.rfind(b")")
            if start < 0 or end < 0 or stat[end + 2 : end + 3] == b"Z":
                continue
            if stat[start + 1:end].lower() in _WINE_BACKGROUND_EXES:
                continue
            cmd_low = _read(f"{proc_root}/{pid}/cmdline").replace(b"\0", b" ").lower()
            if b".exe" in cmd_low and b"proton" not in cmd_low and b"wineserver" not in cmd_low:
                return True
        return False

    def _check_hung(self, run: _Run, now: float) -> None:
        """The game .exe is a zombie and nothing alive has its name: Proton hung after the game died."""
        if self._proc_snapshot is not None:
            scope = self._watch_pids(run.watch) if run.watch else None
            session = int(run.proc.processId()) if run.proc is not None else 0
            alive, zombie = self._proc_snapshot.exe_state(run.exe_name, scope, session)
        elif run.watch:
            session = int(run.proc.processId()) if run.proc is not None else 0
            alive, zombie = self.exe_state(run.exe_name, scope=self._watch_pids(run.watch), session_id=session)
        else:
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
        runs = dict(self._runs)
        if self.scan is not find_game_pids or self.exe_state is not exe_process_state or self.exe_pids is not exe_pids:
            for key, run in runs.items():
                self._tick_run(key, run, now)
            return
        if self._proc_poll is not None and self._proc_poll.done():
            self._proc_snapshot = self._proc_poll.result()
            self._proc_poll = None
        try:
            for key, run in runs.items():
                needs_proc = not run.preparing and run.state != ERROR and (
                    (run.proc is None and bool(run.watch)) or bool(run.exe_name)
                )
                if not needs_proc or (self._proc_snapshot is not None and self._polled_runs.get(key) is run):
                    self._tick_run(key, run, now)
                elif run.state == LOADING:
                    self.changed.emit(key)
        finally:
            self._proc_snapshot = None
        if self._proc_poll is None:
            active = {key: run for key, run in self._runs.items() if not run.preparing and run.state != ERROR}
            watches = {run.watch for run in active.values() if run.watch}
            names = {run.exe_name for run in active.values() if run.exe_name}
            if watches or names:
                self._polled_runs = active
                self._proc_poll = _PROC_POOL.submit(
                    _scan_processes, watches, names, _PROC_ROOT, find_children=bool(names),
                )

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
                old = run.old_exe_pids & self._named_pids(run.exe_name)
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
        pids = self._watch_pids(run.watch)
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
