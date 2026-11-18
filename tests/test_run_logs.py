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
    run_line = next(line for line in script.splitlines() if "umu-run" in line)
    assert run_line.endswith("-dx11 || rc=$?") and not run_line.startswith("exec ")
    assert script.rstrip().endswith('exit "$rc"')
