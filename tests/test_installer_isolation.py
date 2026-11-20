"""Throwaway installer prefix: no Z: (/) and no drive reaching home, only I: and D:."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import extractor  # noqa: E402

INNO = b"MZ" + b"\x00" * 64 + b"Inno Setup Setup Data (6.2.0)" + b"\x00" * 64


def _drives(pfx: Path) -> dict[str, str]:
    dd = pfx / "dosdevices"
    return {e.name: os.path.realpath(e) for e in dd.iterdir()}


def _repack_with_parts(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads" / "Game [FitGirl Repack]"
    (folder / "MD5").mkdir(parents=True)
    (folder / "setup.exe").write_bytes(INNO)
    (folder / "setup-1.bin").write_bytes(b"a" * 100)
    (folder / "fg-optional.7z").write_bytes(b"b")
    (folder / "MD5" / "fitgirl-bins.md5").write_bytes(b"c")
    return folder / "setup.exe"


def test_fresh_prefix_has_no_root_drive_and_own_letters(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "Downloads" / "Setup"
    lib = tmp_path / "Lib" / "Games"
    src.mkdir(parents=True)
    lib.mkdir(parents=True)
    pfx = tmp_path / "pfx"
    extractor.isolate_installer_prefix(pfx, {"i:": src, "d:": lib}, home=home)

    drives = _drives(pfx)
    assert drives["c:"] == str((pfx / "drive_c").resolve())
    assert drives["i:"] == str(src.resolve()) and drives["d:"] == str(lib.resolve())
    # z: exists (so Proton does not recreate it as /) but shows an empty folder of the prefix.
    assert drives["z:"] == str((pfx / ".deckinstall_no_root").resolve())
    assert not any((pfx / ".deckinstall_no_root").iterdir())
    assert "/" not in drives.values()
    assert extractor.unsafe_prefix_drives(pfx, home) == []


def test_drives_reaching_root_or_home_are_removed(tmp_path: Path) -> None:
    home = tmp_path / "users" / "me"
    (home / "Games").mkdir(parents=True)
    other = tmp_path / "Other"
    other.mkdir()
    src = tmp_path / "Downloads"
    src.mkdir()
    pfx = tmp_path / "pfx"
    dd = pfx / "dosdevices"
    dd.mkdir(parents=True)
    (dd / "z:").symlink_to("/")
    (dd / "e:").symlink_to(home)
    (dd / "f:").symlink_to(tmp_path / "users")  # ancestor of home
    (dd / "g:").symlink_to(other)  # unrelated folder: kept
    (dd / "d:").symlink_to(home / "Games")  # replaced by our own d:
    (dd / "com1").symlink_to("/dev/ttyS0")  # not a drive: left alone
    assert set(extractor.unsafe_prefix_drives(pfx, home)) == {"z:", "e:", "f:"}

    extractor.isolate_installer_prefix(pfx, {"i:": src, "d:": other}, home=home)
    drives = _drives(pfx)
    assert "e:" not in drives and "f:" not in drives
    assert drives["z:"] != "/" and drives["g:"] == str(other.resolve())
    assert drives["d:"] == str(other.resolve()) and drives["i:"] == str(src.resolve())
    assert (dd / "com1").is_symlink()
    assert extractor.unsafe_prefix_drives(pfx, home) == []


@pytest.mark.parametrize("unsafe", ["home", "root_parent"])
def test_folder_that_is_home_or_holds_it_is_refused(tmp_path: Path, unsafe: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = home if unsafe == "home" else tmp_path
    with pytest.raises(extractor.InstallerIsolationError):
        extractor.isolate_installer_prefix(tmp_path / "pfx", {"i:": root, "d:": tmp_path / "x"}, home=home)
    assert not (tmp_path / "pfx" / "dosdevices").exists()


def test_symlink_failure_is_a_clear_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with (
        mock.patch.object(Path, "symlink_to", side_effect=OSError("read-only")),
        pytest.raises(extractor.InstallerIsolationError, match="read-only"),
    ):
        extractor.isolate_installer_prefix(tmp_path / "pfx", {"i:": tmp_path / "a", "d:": tmp_path / "b"}, home=home)


def test_to_wine_path_uses_most_specific_drive(tmp_path: Path) -> None:
    lib = tmp_path / "Lib"
    src = lib / "Downloads"
    src.mkdir(parents=True)
    drives = {"d:": lib, "i:": src}
    assert extractor.to_wine_path(src / "setup.exe", drives) == "I:\\setup.exe"
    assert extractor.to_wine_path(lib / "Game" / "Sub", drives) == "D:\\Game\\Sub"
    assert extractor.to_wine_path(lib, drives) == "D:\\"
    with pytest.raises(extractor.InstallerIsolationError):
        extractor.to_wine_path(tmp_path / "elsewhere", drives)


class _Seen:
    def __init__(self) -> None:
        self.cmd: list[str] = []
        self.env: dict[str, str] = {}
        self.cwd = ""
        self.drives: dict[str, str] = {}
        self.calls = 0


def _run(installer: Path, target: Path, seen: _Seen, write: Path | None = None, **kw: Any) -> Path:
    def fake_command(exe: Path, args: list[str], tmp_pfx: Path, *_a: object) -> tuple[list[str], dict[str, str], Path]:
        return ["umu-run", str(exe), *args], {"WINEPREFIX": str(tmp_pfx)}, tmp_pfx

    def popen(cmd: list[str], **pkw: Any) -> mock.Mock:
        seen.calls += 1
        seen.cmd, seen.env, seen.cwd = cmd, dict(pkw["env"]), pkw["cwd"]
        seen.drives = _drives(Path(seen.env["WINEPREFIX"]))  # state when the installer starts
        if write is not None:
            write.parent.mkdir(parents=True, exist_ok=True)
            write.write_bytes(b"MZ")
        return mock.Mock()

    with (
        mock.patch.object(extractor, "_installer_command", side_effect=fake_command),
        mock.patch.object(extractor.subprocess, "Popen", side_effect=popen),
        mock.patch.object(extractor, "_drain_stream_lines", return_value=0),
        mock.patch.object(extractor, "check_disk_space"),
    ):
        return extractor.run_windows_installer(installer, target, **kw)


def test_inno_installer_runs_isolated_with_dir_on_d(tmp_path: Path) -> None:
    setup = tmp_path / "Downloads" / "GOG" / "setup_game.exe"
    setup.parent.mkdir(parents=True)
    setup.write_bytes(INNO)
    target = tmp_path / "Lib" / "Games" / "My Game"
    seen = _Seen()
    out = _run(setup, target, seen, write=target / "Game.exe")

    assert out == target
    assert "/DIR=D:\\My Game" in seen.cmd and not any(a.startswith("/DIR=Z:") for a in seen.cmd)
    assert seen.drives["d:"] == str(target.parent.resolve())
    assert seen.drives["i:"] == str(setup.parent.resolve())
    assert seen.drives["z:"] != "/" and "/" not in seen.drives.values()
    assert seen.env["PROTON_SET_GAME_DRIVE"] == "0" and seen.env["PROTON_SET_STEAM_DRIVE"] == "0"


def test_repack_parts_stay_reachable_next_to_setup(tmp_path: Path) -> None:
    setup = _repack_with_parts(tmp_path)
    target = tmp_path / "Lib" / "Games" / "Game"
    seen = _Seen()
    _run(setup, target, seen, write=target / "Game.exe", repack=True)

    i_root = Path(seen.drives["i:"])
    assert i_root == setup.parent.resolve() and seen.cwd == str(setup.parent)
    for part in ("setup-1.bin", "fg-optional.7z", "MD5/fitgirl-bins.md5"):
        assert (i_root / part).is_file()  # same relative path the installer uses from I:\
    drives = {"i:": i_root}
    assert extractor.to_wine_path(setup.parent / "setup-1.bin", drives) == "I:\\setup-1.bin"
    assert extractor.to_wine_path(setup.parent / "MD5" / "fitgirl-bins.md5", drives) == "I:\\MD5\\fitgirl-bins.md5"
    assert not any(a.startswith("/DIR=") for a in seen.cmd)


def test_installer_in_home_root_is_not_started(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    setup = home / "setup.exe"  # I: would expose the whole home folder
    setup.write_bytes(INNO)
    seen = _Seen()
    with (
        mock.patch.object(Path, "home", return_value=home),
        pytest.raises(extractor.InstallerIsolationError),
    ):
        _run(setup, tmp_path / "Lib" / "Games" / "Game", seen)
    assert seen.calls == 0
