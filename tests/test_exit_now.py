"""The app ends with os._exit after syncing, so PySide's teardown at interpreter exit never runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deckinstall import ui  # noqa: E402


def test_exit_now_flushes_logs_and_skips_interpreter_shutdown() -> None:
    with (
        mock.patch.object(ui.logging, "shutdown") as shutdown,
        mock.patch.object(ui.os, "_exit", side_effect=SystemExit(3)) as hard_exit,
    ):
        try:
            ui.exit_now(3)
        except SystemExit:
            pass
    shutdown.assert_called_once()
    hard_exit.assert_called_once_with(3)


def test_entry_point_exit_codes(tmp_path: Path) -> None:
    """main.py keeps the exit code of main() (version and smoke test) through os._exit."""
    env = {"HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--version"], capture_output=True, text=True, env=env, timeout=60
    )
    assert out.returncode == 0
    assert ui.__version__ in out.stdout
