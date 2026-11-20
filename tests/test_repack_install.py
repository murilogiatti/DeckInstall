"""Repack / Windows installer runner (Fase 2.2): /DIR=, LAA, wine fallback, unarc."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import extractor, library  # noqa: E402

INNO = b"MZ" + b"\x00" * 64 + b"Inno Setup Setup Data (6.2.0)" + b"\x00" * 64


def _repack(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads" / "Game [FitGirl Repack]"
    folder.mkdir(parents=True)
    (folder / "setup.exe").write_bytes(INNO)
    (folder / "fg-01.bin").write_bytes(b"a" * 1000)
    (folder / "unarc.dll").write_bytes(b"x")
    return folder / "setup.exe"


class _Runner:
    """Stands in for Popen + the output drain: records the call and simulates the installer."""

    def __init__(self, action: Callable[[list[str], dict[str, str]], None] | None = None,
                 lines: tuple[str, ...] = (), rc: int = 0) -> None:
        self.action = action
        self.lines = lines
        self.rc = rc
        self.cmd: list[str] = []
        self.env: dict[str, str] = {}

    def popen(self, cmd: list[str], **kw: Any) -> mock.Mock:
        self.cmd = cmd
        self.env = dict(kw["env"])
        if self.action is not None:
            self.action(cmd, self.env)
        return mock.Mock()

    def drain(self, _proc: object, _cancel: object, on_line: Callable[[str], None], _msg: str) -> int:
        for line in self.lines:
            on_line(line)
        return self.rc


def _run(runner: _Runner, installer: Path, target: Path, **kw: Any) -> Path:
    def fake_command(exe: Path, args: list[str], tmp_pfx: Path, *_a: object) -> tuple[list[str], dict[str, str], Path]:
        return ["umu-run", str(exe), *args], {"WINEPREFIX": str(tmp_pfx)}, tmp_pfx

    with (
        mock.patch.object(extractor, "_installer_command", side_effect=fake_command),
        mock.patch.object(extractor.subprocess, "Popen", side_effect=runner.popen),
        mock.patch.object(extractor, "_drain_stream_lines", side_effect=runner.drain),
    ):
        return extractor.run_windows_installer(installer, target, **kw)


def test_inno_arguments(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    target = tmp_path / "Lib" / "Games" / "My Game"
    assert extractor.installer_arguments(setup, target) == ["/DIR=D:\\My Game"]
    plain = tmp_path / "nsis.exe"
    plain.write_bytes(b"MZ")
    assert extractor.installer_arguments(plain, target) == []


def test_repack_runs_without_dir_and_installs_into_dir(tmp_path: Path) -> None:
    # unarc fails (-11 "Unable to write data to disk") with /DIR=, so repacks run as they are.
    setup = _repack(tmp_path)
    target = tmp_path / "Lib" / "Games" / "Game"
    logs: list[str] = []

    def installer(_cmd: list[str], env: dict[str, str]) -> None:
        (target / "Game.exe").write_bytes(b"MZ")  # the user picked the suggested folder
        (Path(env["WINEPREFIX"]) / "drive_c").mkdir(parents=True, exist_ok=True)

    runner = _Runner(installer)
    with mock.patch.object(extractor, "check_disk_space") as space:
        out = _run(runner, setup, target, repack=True, on_log=logs.append)

    assert out == target and (target / "Game.exe").is_file()
    assert not any(a.startswith("/DIR=") for a in runner.cmd)
    assert runner.env["WINE_LARGE_ADDRESS_AWARE"] == "0"
    assert space.call_args.args[1] >= 2 * (1000 + len(INNO) + 1)
    assert not (target / ".deckinstall_installer_pfx").exists()
    assert not list(target.parent.glob("deckinstall_pfx_*"))  # throwaway prefix is gone


def test_repack_that_ignores_dir_is_moved_out_of_drive_c(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    target = tmp_path / "Lib" / "Games" / "Game"

    def installer(_cmd: list[str], env: dict[str, str]) -> None:
        game = Path(env["WINEPREFIX"]) / "drive_c" / "Games" / "Game"
        game.mkdir(parents=True)
        (game / "Game.exe").write_bytes(b"MZ")

    out = _run(_Runner(installer), setup, target, repack=True)
    assert out == target / "Game"  # the folder the game really lives in
    assert (out / "Game.exe").is_file()


def test_repack_installed_in_another_folder_is_moved_into_target(tmp_path: Path) -> None:
    # The user picked X:\Games\<Game> in the installer instead of the library folder.
    setup = _repack(tmp_path)
    home_games = tmp_path / "home" / "Games"
    home_games.mkdir(parents=True)
    (home_games / "Old Game").mkdir()  # existed before: never touched
    (home_games / "Old Game" / "Old.exe").write_bytes(b"MZ")
    target = tmp_path / "Lib" / "Games" / "Game"
    logs: list[str] = []

    def installer(_cmd: list[str], _env: dict[str, str]) -> None:
        picked = home_games / "The Game"
        (picked / "Data").mkdir(parents=True)
        (picked / "Game.exe").write_bytes(b"MZ")
        (picked / "Data" / "a.pak").write_bytes(b"x")

    with mock.patch.object(extractor, "_installed_elsewhere_roots", return_value=[target.parent, home_games]):
        out = _run(_Runner(installer), setup, target, repack=True, on_log=logs.append)

    assert out == target and (target / "Game.exe").is_file() and (target / "Data" / "a.pak").is_file()
    assert not (home_games / "The Game").exists()
    assert (home_games / "Old Game" / "Old.exe").is_file()
    assert any("moving it to" in line for line in logs)


def test_repack_with_two_new_folders_is_not_guessed(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    home_games = tmp_path / "home" / "Games"
    home_games.mkdir(parents=True)
    target = tmp_path / "Lib" / "Games" / "Game"

    def installer(_cmd: list[str], _env: dict[str, str]) -> None:
        for name in ("A", "B"):
            (home_games / name).mkdir()
            (home_games / name / f"{name}.exe").write_bytes(b"MZ")

    with (
        mock.patch.object(extractor, "_installed_elsewhere_roots", return_value=[target.parent, home_games]),
        pytest.raises(RuntimeError, match="No files were installed"),
    ):
        _run(_Runner(installer), setup, target, repack=True)
    assert (home_games / "A").is_dir() and (home_games / "B").is_dir()


def test_unarc_failure_has_its_own_error(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    runner = _Runner(lines=("ISDone.dll: An error occurred when unpacking!", "Unarc.dll returned an error code: -11"), rc=1)
    with pytest.raises(extractor.UnarcError) as err:
        _run(runner, setup, tmp_path / "Lib" / "Games" / "Game", repack=True)
    assert err.value.code == "-11"
    with pytest.raises(extractor.UnarcError):
        _run(_Runner(rc=3), setup, tmp_path / "Lib" / "Games" / "Game2", repack=True)


def test_plain_installer_keeps_prefix_and_plain_error(tmp_path: Path) -> None:
    setup = tmp_path / "Downloads" / "install.exe"
    setup.parent.mkdir(parents=True)
    setup.write_bytes(b"MZ")
    target = tmp_path / "Lib" / "Games" / "Game"

    def installer(_cmd: list[str], env: dict[str, str]) -> None:
        (Path(env["WINEPREFIX"]) / "drive_c" / "windows").mkdir(parents=True)
        (target / "Game.exe").write_bytes(b"MZ")

    _run(_Runner(installer), setup, target)
    assert (target / ".deckinstall_installer_pfx" / "drive_c").is_dir()
    with pytest.raises(RuntimeError) as err:
        _run(_Runner(rc=2), setup, tmp_path / "Lib" / "Games" / "Other")
    assert not isinstance(err.value, extractor.UnarcError)


def test_system_wine_fallback(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    with (
        mock.patch.object(extractor, "find_steam_root", return_value=None),
        mock.patch.object(extractor.runtime_manager, "ensure_umu_run", side_effect=RuntimeError("offline")),
        mock.patch.object(extractor.shutil, "which", return_value="/usr/bin/wine"),
    ):
        cmd, env, pfx = extractor._installer_command(setup, ["/DIR=Z:\\x"], tmp_path / "pfx", None, None)
    assert cmd == ["/usr/bin/wine", str(setup), "/DIR=Z:\\x"]
    assert env["WINEPREFIX"] == str(tmp_path / "pfx") and pfx == tmp_path / "pfx"
    with (
        mock.patch.object(extractor, "find_steam_root", return_value=None),
        mock.patch.object(extractor.runtime_manager, "ensure_umu_run", side_effect=RuntimeError("offline")),
        mock.patch.object(extractor.shutil, "which", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="offline"):
            extractor._installer_command(setup, [], tmp_path / "pfx", None, None)


def test_installer_prefix_never_on_exfat(tmp_path: Path) -> None:
    target = tmp_path / "SD" / "DeckInstall" / "Games" / "Game"
    with mock.patch.object(library, "fs_type_of", return_value="exfat"):
        parent = extractor._installer_prefix_parent(target)
    assert parent == library.internal_library_root() / "Prefixes"
    with mock.patch.object(library, "fs_type_of", return_value="ext4"):
        assert extractor._installer_prefix_parent(target) == target.parent


def test_worker_reports_unarc_without_md5(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    setup = _repack(tmp_path)
    failures: list[str] = []
    worker = ui_workers.ExtractWorker(setup, tmp_path / "Lib" / "Games", "Game")
    worker.failed.connect(failures.append)
    with mock.patch.object(ui_workers, "run_windows_installer", side_effect=extractor.UnarcError("-7")) as run:
        worker.run()
    assert run.call_args.kwargs["repack"] is True
    assert failures and "-7" in failures[0] and "unarc.dll" in failures[0]
    assert "MD5" not in failures[0]
