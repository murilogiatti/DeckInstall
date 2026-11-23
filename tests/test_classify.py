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


def test_archive_not_installable_and_worker_rejects(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    zpath = _write(tmp_path / "Game.zip", b"PK")
    info = classify.classify_source(zpath)
    assert info.kind == classify.ARCHIVE
    assert not info.needs_install

    worker = ui_workers.ExtractWorker(zpath, tmp_path / "Games", "Game")
    errs: list[str] = []
    worker.failed.connect(errs.append)
    worker.run()
    assert len(errs) == 1


def test_nsis_msi_and_installshield_classification(tmp_path: Path) -> None:
    nsis_exe = _write(tmp_path / "IndieGame_v1.0.exe", b"MZ" + b"\x00" * 64 + b"NullsoftInst")
    nsis_info = classify.classify_source(nsis_exe)
    assert nsis_info.kind == classify.INSTALLER
    assert nsis_info.installer == nsis_exe
    args = extractor.installer_arguments(nsis_exe, tmp_path / "Games" / "IndieGame")
    assert len(args) == 1 and args[0].startswith("/D=D:\\")
    assert extractor.installer_arguments(nsis_exe, tmp_path / "Games" / "Indie Game") == []

    msi_dir = tmp_path / "MsiOnlyFolder"
    msi_file = _write(msi_dir / "GamePackage.msi", b"\xd0\xcf\x11\xe0")
    msi_info = classify.classify_source(msi_file)
    assert msi_info.kind == classify.INSTALLER
    assert msi_info.installer == msi_file
    assert classify.classify_source(msi_dir).kind == classify.INSTALLER
    msi_args = extractor.installer_arguments(msi_file, tmp_path / "Games" / "GamePackage")
    assert len(msi_args) == 1 and msi_args[0].startswith("TARGETDIR=D:\\")

    is_dir = tmp_path / "ClassicGame"
    is_setup = _write(is_dir / "setup.exe")
    _write(is_dir / "data1.cab", b"ISc(")
    is_info = classify.classify_source(is_dir)
    assert is_info.kind == classify.INSTALLER
    assert is_info.installer == is_setup

    is_installed = tmp_path / "ClassicInstalled"
    _write(is_installed / "setup.exe")
    _write(is_installed / "data1.cab", b"ISc(")
    _write(is_installed / "Game.exe")
    assert classify.classify_source(is_installed).kind == classify.READY_FOLDER

    nsis_dir = tmp_path / "NsisFolder"
    nsis_only = _write(nsis_dir / "Indie_v1.exe", b"MZ" + b"\x00" * 64 + b"NullsoftInst")
    assert classify.classify_source(nsis_dir).kind == classify.INSTALLER
    assert classify.classify_source(nsis_dir).installer == nsis_only

    linux_msi_dir = tmp_path / "LinuxPlusMsi"
    _write(linux_msi_dir / "game.x86_64", b"\x7fELF")
    _write(linux_msi_dir / "extra.msi", b"\xd0\xcf\x11\xe0")
    assert classify.classify_source(linux_msi_dir).kind == classify.READY_FOLDER

    start_msi_dir = tmp_path / "StartShPlusMsi"
    start_sh = _write(start_msi_dir / "start.sh", b"#!/bin/sh\n")
    start_sh.chmod(0o755)
    _write(start_msi_dir / "Extras.msi", b"\xd0\xcf\x11\xe0")
    assert classify.classify_source(start_msi_dir).kind == classify.READY_FOLDER

    nsis_msi_dir = tmp_path / "NsisPlusMsi"
    nsis_installer = _write(nsis_msi_dir / "Indie_v1.exe", b"MZ" + b"\x00" * 64 + b"NullsoftInst")
    _write(nsis_msi_dir / "Extras.msi", b"\xd0\xcf\x11\xe0")
    nsis_msi_info = classify.classify_source(nsis_msi_dir)
    assert nsis_msi_info.kind == classify.INSTALLER
    assert nsis_msi_info.installer == nsis_installer


def test_msi_command_uses_msiexec_and_appdata_adoption(tmp_path: Path) -> None:
    msi_file = _write(tmp_path / "src" / "Game.msi", b"\xd0\xcf\x11\xe0")
    with (
        mock.patch.object(extractor, "find_steam_root", return_value=tmp_path / "steam"),
        mock.patch.object(extractor, "find_proton_binary", return_value=tmp_path / "proton"),
    ):
        cmd, _env, _pfx = extractor._installer_command(msi_file, ["TARGETDIR=D:\\Game"], tmp_path / "pfx", None, None)
    assert cmd[:4] == [str(tmp_path / "proton"), "run", "msiexec", "/i"]
    assert cmd[4] == "I:\\Game.msi"

    drive_c = tmp_path / "drive_c"
    exe = _write(drive_c / "users" / "steamuser" / "AppData" / "Local" / "Programs" / "MyIndie" / "MyIndie.exe")
    picked, _fallback = extractor.find_games_in_drive_c(drive_c)
    assert [p.name for p in picked] == ["MyIndie"]

    from deckinstall import adopt

    assert adopt._drive_c_game_root(exe, drive_c) == exe.parent

    # Symlinked AppData pointing outside drive_c must be ignored
    outside = tmp_path / "outside"
    _write(outside / "Local" / "Programs" / "Secret" / "Secret.exe")
    dc2 = tmp_path / "drive_c_sym"
    (dc2 / "users" / "steamuser").mkdir(parents=True)
    (dc2 / "users" / "steamuser" / "AppData").symlink_to(outside)
    assert extractor.find_games_in_drive_c(dc2) == ([], [])

    # Program Files game takes precedence over AppData/Local updater
    dc3 = tmp_path / "drive_c_prio"
    _write(dc3 / "Program Files" / "RealGame" / "RealGame.exe")
    _write(dc3 / "users" / "steamuser" / "AppData" / "Local" / "Vendor" / "updater.exe")
    picked3, _ = extractor.find_games_in_drive_c(dc3)
    assert [p.name for p in picked3] == ["RealGame"]

    # Folder with only updater.exe + UnityCrashHandler64.exe in AppData is not picked or adopted
    dc4 = tmp_path / "drive_c_updater_only"
    _write(dc4 / "users" / "steamuser" / "AppData" / "Local" / "Vendor" / "updater.exe")
    _write(dc4 / "users" / "steamuser" / "AppData" / "Local" / "Vendor" / "UnityCrashHandler64.exe")
    picked4, fallback4 = extractor.find_games_in_drive_c(dc4)
    assert picked4 == [] and fallback4 == []
    assert extractor._adopt_from_drive_c(dc4, tmp_path / "out4") is False

    # Collision between Program Files/Meu Jogo and drive_c/Meu Jogo does not overwrite
    dc6 = tmp_path / "drive_c_collision"
    _write(dc6 / "Program Files" / "Meu Jogo" / "a.txt", b"1")
    _write(dc6 / "Program Files" / "Meu Jogo" / "Game.exe")
    _write(dc6 / "Meu Jogo" / "a.txt", b"2")
    _write(dc6 / "Meu Jogo" / "Other.exe")
    out6 = tmp_path / "out6"
    out6.mkdir()
    assert extractor._adopt_from_drive_c(dc6, out6) is True
    assert (out6 / "Meu Jogo" / "a.txt").read_bytes() == b"1"
    assert (out6 / "Meu Jogo (2)" / "a.txt").read_bytes() == b"2"

    dc5 = tmp_path / "drive_c_root_game"
    _write(dc5 / "Program Files" / "Vendor" / "Launcher.exe")
    _write(dc5 / "Meu Jogo" / "MeuJogo.exe")
    picked5, _ = extractor.find_games_in_drive_c(dc5)
    assert sorted(p.name for p in picked5) == ["Meu Jogo", "Vendor"]


def test_winetricks_presets_cover_common_runtimes() -> None:
    from deckinstall import runtime_manager

    for verb in ("vcrun2022", "vcrun2013", "d3dx9", "dotnet48", "xact", "physx"):
        assert verb in runtime_manager.WINETRICKS_PRESETS


def test_autorun_and_crash_helpers_do_not_make_an_installer_folder_a_game(tmp_path: Path) -> None:
    for name in ("setup.exe", "autorun.exe", "crashpad_handler.exe", "updater.exe"):
        _write(tmp_path / name, b"MZ")
    _write(tmp_path / "data1.cab", b"x")
    info = classify.classify_source(tmp_path)
    assert info.kind == classify.INSTALLER and info.installer is not None


def test_game_exe_mentioning_installshield_is_not_an_installer(tmp_path: Path) -> None:
    exe = _write(tmp_path / "Game.exe", b"MZ" + b"\0" * 600_000 + b"InstallShield ISSetup")
    assert classify.classify_source(exe).kind == classify.EXE
    stub = _write(tmp_path / "Launch.exe", b"MZ" + b"\0" * 100 + b"InstallShield Setup Launcher")
    assert classify.classify_source(stub).kind == classify.INSTALLER


def test_adopt_from_drive_c_merges_into_existing_folder(tmp_path: Path) -> None:
    from deckinstall.extractor import _adopt_from_drive_c

    drive_c = tmp_path / "drive_c"
    _write(drive_c / "My Game" / "game.exe", b"MZ")
    target = tmp_path / "target"
    _write(target / "My Game" / "old.exe", b"MZ")
    assert _adopt_from_drive_c(drive_c, target)
    assert sorted(p.name for p in target.iterdir()) == ["My Game"]
    assert (target / "My Game" / "game.exe").is_file() and (target / "My Game" / "old.exe").is_file()
