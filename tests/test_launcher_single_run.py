"""launch.sh starts the game exactly once and passes its exit status through, whatever systemd-inhibit does."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import fsutil, standalone  # noqa: E402

# A fake systemd-inhibit for each case. "works" behaves like the real one: options, then runs the command.
FAKE_INHIBIT = {
    # like the real one, a child killed by a signal comes back as exit code 1
    "works": '#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done\n"$@"\nrc=$?\n[ $rc -gt 128 ] && exit 1\nexit $rc\n',
    "refused": (
        "#!/bin/sh\necho 'Failed to inhibit: Access denied as the requested operation requires"
        " interactive authentication.' >&2\nexit 1\n"
    ),
    "no_bus": "#!/bin/sh\necho 'Failed to connect to bus: No such file or directory' >&2\nexit 1\n",
}
CODES = [0, 1, 7, 42]


def _game(tmp_path: Path, hooks: tuple[str, str] = ("", "")) -> standalone.StandaloneGame:
    src = tmp_path / "Games" / "Counter"
    src.mkdir(parents=True)
    exe = src / "start.sh"
    # Counts its runs, then exits with $GAME_RC (or kills itself with $GAME_SIG).
    exe.write_text(
        "#!/bin/sh\n"
        'echo run >> "$COUNTER"\n'
        'echo "game stderr line" >&2\n'
        'if [ -n "$GAME_SIG" ]; then kill -s "$GAME_SIG" $$; fi\n'
        'exit "${GAME_RC:-0}"\n',
        encoding="utf-8",
    )
    exe.chmod(0o755)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("Counter", exe, src, native_linux=True)
    if any(hooks):
        game = standalone.set_launch_hooks(game, *hooks)
    return game


def _run(game: standalone.StandaloneGame, tmp_path: Path, case: str, **env: str) -> tuple[int, int, str]:
    bin_dir = tmp_path / f"bin-{case}"
    bin_dir.mkdir(exist_ok=True)
    if case in FAKE_INHIBIT:
        fake = bin_dir / "systemd-inhibit"
        fake.write_text(FAKE_INHIBIT[case], encoding="utf-8")
        fake.chmod(0o755)
    counter = tmp_path / f"count-{case}-{env.get('GAME_RC', '')}{env.get('GAME_SIG', '')}"
    counter.unlink(missing_ok=True)
    # "absent": PATH without any systemd-inhibit (only the basic tools the launcher needs)
    tools = tmp_path / "tools"
    if not tools.exists():
        tools.mkdir()
        for name in ("bash", "sh", "mktemp", "rm", "tr", "date", "mkdir", "touch", "find", "cat", "mv",
                     "ls", "printf", "kill", "dirname", "basename", "env", "tee", "head", "sed", "stat"):
            for base in ("/usr/bin", "/bin"):
                if (Path(base) / name).exists():
                    (tools / name).symlink_to(Path(base) / name)
                    break
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["bash", str(game.launch_script)],
        env={
            "PATH": f"{bin_dir}:{tools}", "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime),
            "COUNTER": str(counter), **env,
        },
        capture_output=True, text=True, timeout=60,
    )
    runs = len(counter.read_text().splitlines()) if counter.exists() else 0
    assert not list(runtime.glob("deckinstall-run.*")), "marker files are cleaned up"
    log = standalone.game_run_logs(game)[0].read_text(encoding="utf-8")
    return proc.returncode, runs, log


@pytest.mark.parametrize("case", ["absent", "works", "refused", "no_bus"])
@pytest.mark.parametrize("code", CODES)
def test_game_starts_once_and_keeps_its_exit_code(tmp_path: Path, case: str, code: int) -> None:
    game = _game(tmp_path)
    rc, runs, log = _run(game, tmp_path, case, GAME_RC=str(code))
    assert runs == 1, log
    assert rc == code
    assert f"exit code {code}" in log
    assert "game stderr line" in log  # the game's own stderr still reaches the run log
    if case in ("refused", "no_bus"):
        assert "Failed to" in log  # systemd-inhibit says why, right in the log
    if case in ("refused", "no_bus"):
        assert "could not keep the PC awake" in log
    else:
        assert "could not keep the PC awake" not in log


@pytest.mark.parametrize("case", ["absent", "works", "refused"])
def test_game_killed_by_a_signal_is_not_restarted(tmp_path: Path, case: str) -> None:
    game = _game(tmp_path)
    rc, runs, _ = _run(game, tmp_path, case, GAME_SIG="TERM")
    assert runs == 1
    assert rc == 128 + signal.SIGTERM


@pytest.mark.parametrize("case", ["works", "refused"])
def test_hooks_run_once_around_the_single_run(tmp_path: Path, case: str) -> None:
    game = _game(tmp_path, hooks=('echo pre >> "$COUNTER.hooks"', 'echo post >> "$COUNTER.hooks"'))
    rc, runs, _ = _run(game, tmp_path, case, GAME_RC="42")
    assert (rc, runs) == (42, 1)
    hooks = Path(str(next(tmp_path.glob(f"count-{case}-42"))) + ".hooks").read_text().split()
    assert hooks == ["pre", "post"]


def test_old_managed_launcher_is_upgraded_with_a_copy_and_own_script_untouched(tmp_path: Path) -> None:
    game = _game(tmp_path)
    script = game.launch_script
    old = script.read_text(encoding="utf-8").replace(
        f"# DeckInstall launcher format {standalone.LAUNCHER_FORMAT}", "# DeckInstall launcher format 3"
    )
    script.write_text(old, encoding="utf-8")
    assert standalone.refresh_launcher_if_outdated(game)
    assert f"# DeckInstall launcher format {standalone.LAUNCHER_FORMAT}" in script.read_text(encoding="utf-8")
    assert script.with_name("launch.sh.prev").read_text(encoding="utf-8") == old

    own = "#!/bin/sh\n# my own launcher\nexec ./start.sh\n"
    script.write_text(own, encoding="utf-8")
    assert not standalone.refresh_launcher_if_outdated(game)
    assert script.read_text(encoding="utf-8") == own


def test_sleep_inhibit_problem_reports_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case, expected in (("works", ""), ("refused", "Access denied"), ("no_bus", "Failed to connect to bus")):
        bin_dir = tmp_path / case
        bin_dir.mkdir()
        fake = bin_dir / "systemd-inhibit"
        fake.write_text(FAKE_INHIBIT[case], encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.defpath}")
        problem = fsutil.sleep_inhibit_problem()
        assert (problem == "") if not expected else (expected in problem)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert "not installed" in fsutil.sleep_inhibit_problem()


def test_run_log_is_capped_without_blocking_the_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(standalone, "LOG_MAX_BYTES", 64 * 1024)
    src = tmp_path / "Games" / "Chatty"
    src.mkdir(parents=True)
    exe = src / "start.sh"
    exe.write_text(
        '#!/bin/sh\necho run >> "$COUNTER"\nhead -c 3000000 /dev/zero | tr "\\\\0" x\necho done-writing >&2\nexit 5\n',
        encoding="utf-8",
    )
    exe.chmod(0o755)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("Chatty", exe, src, native_linux=True)
    rc, runs, log = _run(game, tmp_path, "works")
    assert (rc, runs) == (5, 1)  # the game was never blocked or killed by a full pipe
    size = standalone.game_run_logs(game)[0].stat().st_size
    assert size < 64 * 1024 + 4096
    assert "log truncated" in log
    assert log.rstrip().splitlines()[-1].startswith("== DeckInstall: exit code 5")


def test_uninstall_also_removes_the_kept_previous_launcher(tmp_path: Path) -> None:
    from deckinstall import uninstall

    game = _game(tmp_path)
    prev = game.launch_script.with_name("launch.sh.prev")
    prev.write_text("#!/bin/sh\n", encoding="utf-8")
    plan = uninstall.plan_uninstall(game)
    shortcuts = next(i for i in plan.items if i.id == uninstall.SHORTCUTS)
    assert prev in shortcuts.paths and game.launch_script in shortcuts.paths
