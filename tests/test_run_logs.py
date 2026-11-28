"""Fase 3.4: every run writes a log from launch.sh; the log folder is recorded in the manifest."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2, decode_path  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _install(tmp_path: Path, name: str, exe_name: str, body: bytes, native: bool = False) -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True)
    exe = src / exe_name
    exe.write_bytes(body)
    exe.chmod(0o755)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, exe, src, native_linux=native)


def test_native_runs_are_logged_rotated_and_recorded(tmp_path: Path) -> None:
    game = _install(
        tmp_path, "Shell Game", "start.sh", b'#!/bin/sh\necho "hello from $1"\necho oops >&2\nexit 3\n', native=True
    )
    assert game.native_linux
    script = game.launch_script.read_text(encoding="utf-8")
    assert "LOG_DIR=" in script and "exec " not in script.split("exec >", 1)[0]

    for _ in range(7):
        done = subprocess.run(["bash", str(game.launch_script)], capture_output=True, timeout=30)
        assert done.returncode == 3  # the game's exit code is passed through
    logs = standalone.game_run_logs(game)
    assert [p.name for p in logs] == ["run.log", "run.1.log", "run.2.log", "run.3.log", "run.4.log"]
    text = logs[0].read_text(encoding="utf-8")
    assert "== DeckInstall: starting Shell Game" in text
    assert "oops" in text and "exit code 3" in text

    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["logs"]["keep"] == standalone.LOG_KEEP
    assert decode_path(data["logs"]["dir"], None) == standalone.game_log_dir(game)
    assert standalone.game_log_dir(game).name == game.uid


def test_windows_launcher_logs_and_keeps_args(tmp_path: Path) -> None:
    game = _install(tmp_path, "Win Game", "Game.exe", PE)
    standalone.update_standalone_launch_options(game.game_dir, launch_options="%command% -dx11")
    script = game.launch_script.read_text(encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(game.launch_script)]).returncode == 0
    run_lines = [line for line in script.splitlines() if "umu-run" in line and "|| rc=" in line]
    assert run_lines and all(not line.lstrip().startswith("exec ") for line in run_lines)
    assert all("-dx11" in line for line in run_lines)
    # The run log starts with the exact command and the environment umu-run gets.
    assert "== DeckInstall: command: " in script and "-dx11" in script.split("command: ", 1)[1].splitlines()[0]
    assert "export STEAM_COMPAT_LIBRARY_PATHS=" in script
    assert f"export PROTON_LOG_DIR={standalone.game_log_dir(game)}" in script
    assert "export PROTON_LOCAL_SHADER_CACHE=1" in script  # on by default
    off = _install(tmp_path, "Off Game", "Game.exe", PE)
    standalone.update_standalone_launch_options(off.game_dir, launch_options="PROTON_LOCAL_SHADER_CACHE=0 %command%")
    off_script = off.launch_script.read_text(encoding="utf-8")
    assert "export PROTON_LOCAL_SHADER_CACHE=0" in off_script and "PROTON_LOCAL_SHADER_CACHE=1" not in off_script
    assert "export UMU_RUNTIME_UPDATE=0" in script  # only once the weekly check stamp is fresh
    assert "systemd-inhibit --what=idle --mode=block --who=DeckInstall" in script
    assert script.rstrip().endswith('exit "$rc"')


def test_old_launcher_is_rewritten_once_and_foreign_one_kept(tmp_path: Path) -> None:
    game = _install(tmp_path, "Old Game", "Game.exe", PE)
    script = game.launch_script
    old = script.read_text(encoding="utf-8").replace(standalone._LAUNCHER_FORMAT_LINE + "\n", "")
    script.write_text(old, encoding="utf-8")
    assert standalone.refresh_launcher_if_outdated(game) is True
    assert standalone._LAUNCHER_FORMAT_LINE in script.read_text(encoding="utf-8")
    assert standalone.refresh_launcher_if_outdated(game) is False

    script.write_text("#!/bin/sh\nexec wine Game.exe\n", encoding="utf-8")  # the user's own launcher
    assert standalone.refresh_launcher_if_outdated(game) is False
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\nexec wine Game.exe\n"


def test_before_and_after_commands_run_around_the_game(tmp_path: Path) -> None:
    game = _install(tmp_path, "Hook Game", "start.sh", b"#!/bin/sh\necho game-ran\nexit 4\n", native=True)
    marker = tmp_path / "order.txt"
    standalone.set_launch_hooks(game, f"echo before >> {marker}; false", f"echo after >> {marker}")
    done = subprocess.run(["bash", str(game.launch_script)], capture_output=True, timeout=30)
    assert done.returncode == 4  # the game's exit code, not the hook's
    assert marker.read_text(encoding="utf-8").split() == ["before", "after"]
    log = standalone.game_run_logs(game)[0].read_text(encoding="utf-8")
    assert "the command before the game failed" in log and "game-ran" in log
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["runtime"]["hooks"] == {"pre": f"echo before >> {marker}; false", "post": f"echo after >> {marker}"}
    standalone.set_launch_hooks(game, "", "")
    assert "the command before the game failed" not in game.launch_script.read_text(encoding="utf-8")
    try:
        standalone.set_launch_hooks(game, "a\nb", "")
    except ValueError:
        pass
    else:
        raise AssertionError("multi-line command accepted")
