"""Install-source classification (Fase 2.1) and the GOG / native .sh install paths."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import classify, extractor  # noqa: E402

INNO = b"MZ" + b"\x00" * 64 + b"Inno Setup Setup Data (6.2.0)" + b"\x00" * 64


def _write(path: Path, data: bytes = b"MZ") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _gog_sh(path: Path) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scripts/mojosetup_init.lua", "--")
        zf.writestr("data/noarch/start.sh", "#!/bin/sh\n")
        zf.writestr("data/noarch/game/game.x86_64", "ELF")
    path.write_bytes(b"#!/bin/sh\n# makeself header\nexit 0\n" + buf.getvalue())
    return path


def test_archives_and_single_files(tmp_path: Path) -> None:
    assert classify.classify_source(_write(tmp_path / "Game.v1.2.zip", b"PK")).kind == classify.ARCHIVE
    assert classify.classify_source(_write(tmp_path / "Game.part1.rar", b"Rar!")).kind == classify.ARCHIVE
    assert classify.classify_source(_write(tmp_path / "Game.exe")).kind == classify.EXE
    assert classify.classify_source(_write(tmp_path / "setup.exe")).kind == classify.INSTALLER
    assert classify.classify_source(_write(tmp_path / "Tool.exe", INNO)).kind == classify.INSTALLER
    assert classify.classify_source(_write(tmp_path / "game.x86_64", b"\x7fELF")).kind == classify.EXE
    assert classify.classify_source(_write(tmp_path / "notes.txt", b"hi")).kind == classify.UNKNOWN


def test_gog_installer(tmp_path: Path) -> None:
    setup = _write(tmp_path / "setup_hollow_knight_1.5.78_(64bit)_(46543).exe", INNO)
    _write(tmp_path / "setup_hollow_knight_1.5.78_(64bit)_(46543)-1.bin", b"x")
    info = classify.classify_source(setup)
    assert info.kind == classify.GOG_SETUP and info.installer == setup
    assert classify.suggest_install_name(info) == "Hollow Knight"
    # A folder holding the GOG installer is classified the same way.
    assert classify.classify_source(tmp_path).kind == classify.GOG_SETUP
    # setup_*.exe that is not Inno Setup is just an installer.
    assert classify.classify_source(_write(tmp_path / "x" / "setup_other.exe")).kind == classify.INSTALLER


def test_repack_folder_picks_main_setup(tmp_path: Path) -> None:
    repack = tmp_path / "Stellar Blade [FitGirl Repack]"
    setup = _write(repack / "setup.exe")
    _write(repack / "fg-01.bin", b"x")
    _write(repack / "fg-02.bin", b"x")
    _write(repack / "unarc.dll", b"x")
    _write(repack / "_Redist" / "vcredist_x64.exe")
    _write(repack / "MD5" / "QuickSFV.exe")
    _write(repack / "setup-update.exe")

    info = classify.classify_source(repack)
    assert info.kind == classify.REPACK
    assert info.installer == setup
    assert classify.suggest_install_name(info) == "Stellar Blade"
    # Picking setup.exe itself is recognized as the repack too.
    assert classify.classify_source(setup).kind == classify.REPACK


def test_find_setup_exe_skips_redist_dlc_and_patch(tmp_path: Path) -> None:
    _write(tmp_path / "DLC" / "setup.exe")
    _write(tmp_path / "Redist" / "setup.exe")
    _write(tmp_path / "patch_setup.exe")
    main = _write(tmp_path / "Installer" / "setup_game.exe")
    assert classify.find_setup_exe(tmp_path) == main


def test_ready_folder_and_installer_only_folder(tmp_path: Path) -> None:
    ready = tmp_path / "Ready"
    _write(ready / "Game.exe")
    _write(ready / "Engine" / "Binaries" / "Win64" / "Game-Win64-Shipping.exe")
    assert classify.classify_source(ready).kind == classify.READY_FOLDER

    only = tmp_path / "Only Setup"
    setup = _write(only / "setup.exe")
    info = classify.classify_source(only)
    assert info.kind == classify.INSTALLER and info.installer == setup
    assert classify.suggest_install_name(info) == "Only Setup"


def test_native_sh_and_gog_linux_unpack(tmp_path: Path) -> None:
    script = _gog_sh(tmp_path / "hollow_knight_1_5_78.sh")
    info = classify.classify_source(script)
    assert info.kind == classify.NATIVE_SH

    out = extractor.install_native_sh(script, tmp_path / "Games", "Hollow Knight")
    assert out == tmp_path / "Games" / "Hollow Knight"
    assert (out / "game" / "game.x86_64").is_file()
    assert (out / "start.sh").stat().st_mode & 0o100
    assert not (out / "scripts").exists()


def test_gog_extract_needs_innoextract(tmp_path: Path) -> None:
    setup = _write(tmp_path / "setup_game_1.0_(1).exe", INNO)
    with mock.patch.object(extractor.shutil, "which", return_value=None):
        with pytest.raises(extractor.ToolMissingError):
            extractor.extract_gog_installer(setup, tmp_path / "Games", "Game")


def test_gog_extract_uses_app_folder(tmp_path: Path) -> None:
    setup = _write(tmp_path / "setup_game_1.0_(1).exe", INNO)

    def fake_popen(cmd: list[str], **_kw: object) -> mock.Mock:
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        _write(out_dir / "app" / "Game.exe")
        _write(out_dir / "app" / "__redist" / "vcredist.exe")
        _write(out_dir / "tmp" / "junk.dll", b"x")
        return mock.Mock()

    with (
        mock.patch.object(extractor.shutil, "which", return_value="/usr/bin/innoextract"),
        mock.patch.object(extractor.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(extractor, "_drain_stream_lines", return_value=0),
    ):
        out = extractor.extract_gog_installer(setup, tmp_path / "Games", "Game")
    assert (out / "Game.exe").is_file()
    assert not (out / "__redist").exists() and not (out / "tmp").exists()


def test_extract_worker_falls_back_to_setup_without_innoextract(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    setup = _write(tmp_path / "setup_game_1.0_(1).exe", INNO)
    worker = ui_workers.ExtractWorker(setup, tmp_path / "Games", "Game")
    with (
        mock.patch.object(ui_workers, "extract_gog_installer", side_effect=extractor.ToolMissingError("x")),
        mock.patch.object(ui_workers, "run_windows_installer", return_value=tmp_path / "Games" / "Game") as run,
    ):
        done: list[str] = []
        worker.finished_ok.connect(done.append)
        worker.run()
    run.assert_called_once()
    assert run.call_args.args[0] == setup
    assert worker.kind == classify.GOG_SETUP
    assert done == [str(tmp_path / "Games" / "Game")]
