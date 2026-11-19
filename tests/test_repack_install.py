"""Repack / Windows installer runner (Fase 2.2): /DIR=, LAA, silent, wine fallback, unarc, MD5."""

from __future__ import annotations

import hashlib
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
    assert extractor.installer_arguments(setup, target) == [f"/DIR=Z:{str(target.resolve()).replace('/', chr(92))}"]
    silent = extractor.installer_arguments(setup, target, silent=True)
    assert silent[1:] == ["/SP-", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    plain = tmp_path / "nsis.exe"
    plain.write_bytes(b"MZ")
    assert extractor.installer_arguments(plain, target, silent=True) == []
    # Outside $HOME and /run/media there is no X:/V:, so Z: stays.
    with mock.patch.object(extractor, "_repack_drives", return_value=(("x:", tmp_path / "home"),)):
        assert extractor.installer_arguments(setup, target, repack_drive=True)[0].startswith("/DIR=Z:")


def test_repack_dir_goes_through_x_or_v(tmp_path: Path) -> None:
    # unarc fails with -11 "Unable to write data to disk" through Z:; X: ($HOME) and V: work.
    home, media = tmp_path / "home", tmp_path / "media"
    with mock.patch.object(extractor, "_repack_drives", return_value=(("x:", home), ("v:", media))):
        assert extractor.repack_drive_path(home / "DeckInstall" / "Games" / "My Game") == (
            "x:", home, "X:\\DeckInstall\\Games\\My Game"
        )
        assert extractor.repack_drive_path(media / "sd" / "DeckInstall" / "Games" / "G")[2] == (
            "V:\\sd\\DeckInstall\\Games\\G"
        )
        assert extractor.repack_drive_path(tmp_path / "elsewhere" / "G") is None


def test_repack_installs_into_dir_and_discards_prefix(tmp_path: Path) -> None:
    setup = _repack(tmp_path)
    home = tmp_path / "home"
    target = home / "DeckInstall" / "Games" / "Game"
    seen: dict[str, Path] = {}

    def installer(_cmd: list[str], env: dict[str, str]) -> None:
        drive = Path(env["WINEPREFIX"]) / "dosdevices" / "x:"
        seen["x"] = drive.resolve()
        game = drive / "DeckInstall" / "Games" / "Game"  # the installer writes through X:\...
        game.mkdir(parents=True, exist_ok=True)
        (game / "Game.exe").write_bytes(b"MZ")
        (Path(env["WINEPREFIX"]) / "drive_c").mkdir(parents=True)

    runner = _Runner(installer)
    with (
        mock.patch.object(extractor, "_repack_drives", return_value=(("x:", home),)),
        mock.patch.object(extractor, "check_disk_space") as space,
    ):
        out = _run(runner, setup, target, repack=True, silent=True)

    assert seen["x"] == home.resolve()
    assert out == target and (target / "Game.exe").is_file()
    assert "/DIR=X:\\DeckInstall\\Games\\Game" in runner.cmd and "/SILENT" in runner.cmd
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


def test_verify_repack_md5(tmp_path: Path) -> None:
    folder = tmp_path / "Repack"
    (folder / "MD5").mkdir(parents=True)
    (folder / "fg-01.bin").write_bytes(b"one")
    (folder / "fg-02.bin").write_bytes(b"two")
    good = hashlib.md5(b"one").hexdigest()
    (folder / "MD5" / "fitgirl-bins.md5").write_text(
        "; generated by QuickSFV\n"
        f"{good} *..\\fg-01.bin\n"
        f"{'0' * 32} *..\\fg-02.bin\n"
        f"{good} *..\\fg-03.bin\n",
        encoding="utf-8",
    )
    checked, problems = extractor.verify_repack_md5(folder)
    assert checked == 3
    assert problems == ["../fg-02.bin: checksum mismatch", "../fg-03.bin: missing"]
    empty = tmp_path / "Empty"
    empty.mkdir()
    assert extractor.verify_repack_md5(empty) == (0, [])


def test_worker_stops_on_bad_md5_and_reports_unarc(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    setup = _repack(tmp_path)
    failures: list[str] = []
    worker = ui_workers.ExtractWorker(setup, tmp_path / "Lib" / "Games", "Game", verify_md5=True)
    worker.failed.connect(failures.append)
    with (
        mock.patch.object(ui_workers, "verify_repack_md5", return_value=(2, ["fg-01.bin: checksum mismatch"])),
        mock.patch.object(ui_workers, "run_windows_installer") as run,
    ):
        worker.run()
    run.assert_not_called()
    assert failures and "fg-01.bin" in failures[0]

    failures.clear()
    worker = ui_workers.ExtractWorker(setup, tmp_path / "Lib" / "Games", "Game")
    worker.failed.connect(failures.append)
    with mock.patch.object(ui_workers, "run_windows_installer", side_effect=extractor.UnarcError("-7")) as run:
        worker.run()
    assert run.call_args.kwargs["repack"] is True
    assert failures and "-7" in failures[0] and "unarc.dll" in failures[0]
