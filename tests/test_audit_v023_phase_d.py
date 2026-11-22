"""Regressions in launcher discovery and native game installation."""

from __future__ import annotations

import os
import shlex
import stat
import zipfile
from pathlib import Path

import pytest

from deckinstall import adopt, extractor, play_monitor, standalone, ui_workers
from deckinstall.launcher_scan import scan_external_launcher_games
from deckinstall.manifest import find_manifest


@pytest.mark.parametrize("windows_path", [r"C:\Games\RPG\game.exe", "drive_c/Games/RPG/game.exe"])
def test_lutris_resolves_installation_inside_prefix(windows_path: str) -> None:
    home = Path.home()
    prefix = home / "Prefixes" / "RPG"
    exe = prefix / "drive_c" / "Games" / "RPG" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    config = home / ".config" / "lutris" / "games" / "rpg.yml"
    config.parent.mkdir(parents=True)
    config.write_text(f"game:\n  prefix: {prefix}\n  exe: '{windows_path}'\n", encoding="utf-8")
    games = scan_external_launcher_games(home)
    assert [game.exe_path for game in games] == [exe]


def test_adopt_infers_game_root_above_launcher_binary_subdirectories() -> None:
    home = Path.home()
    root = home / "Games" / "RPG"
    exe = root / "RPGProject" / "Binaries" / "Win64" / "game.exe"
    exe.parent.mkdir(parents=True)
    (root / "Engine").mkdir()
    exe.write_bytes(b"MZ")
    config = home / ".config" / "lutris" / "games" / "rpg.yml"
    config.parent.mkdir(parents=True)
    config.write_text(f"game:\n  exe: {exe}\n", encoding="utf-8")
    games = adopt.find_adoption_candidates(home=home, kinds=[adopt.LUTRIS])
    assert [game.game_dir for game in games] == [root]


def test_gog_linux_merges_architecture_files_and_restores_safe_execute_bits(tmp_path: Path) -> None:
    script = tmp_path / "setup.sh"
    with zipfile.ZipFile(script, "w") as archive:
        archive.writestr("data/noarch/start.sh", "#!/bin/sh\n")
        archive.writestr("data/noarch/game/readme.txt", "help")
        binary = zipfile.ZipInfo("data/x86_64/game/game.x86_64")
        binary.create_system = 3
        binary.external_attr = (stat.S_IFREG | 0o6755) << 16
        archive.writestr(binary, b"\x7fELF")

    out = extractor.install_native_sh(script, tmp_path / "library", "RPG")
    assert (out / "game" / "readme.txt").read_text(encoding="utf-8") == "help"
    mode = (out / "game" / "game.x86_64").stat().st_mode
    assert mode & stat.S_IXUSR
    assert not mode & (stat.S_ISUID | stat.S_ISGID)


def test_launch_script_round_trips_quoted_arguments_and_merges_dll_overrides(tmp_path: Path) -> None:
    launch = tmp_path / "launch.sh"
    options = "DXVK_CONFIG='custom config' WINEDLLOVERRIDES='dxgi,d3d11=n,b' %command% 'some arg' ';'"
    standalone._write_launch_script(
        launch, tmp_path / "Game.exe", tmp_path / "prefix", "", "dxgi=b", options,
    )
    script = launch.read_text(encoding="utf-8").splitlines()
    assert "export DXVK_CONFIG='custom config'" in script
    assert "export WINEDLLOVERRIDES='dxgi=b;d3d11=n,b'" in script
    assert shlex.split(script[-1])[-2:] == ["some arg", ";"]
    assert script[-1].endswith(" ';'")
    assert os.access(launch, os.X_OK)


def test_adoption_restores_files_when_registration_fails_after_writing_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path.home() / "Downloads" / "RPG"
    root.mkdir(parents=True)
    exe = root / "Game.exe"
    exe.write_bytes(b"MZ")
    launch = root / "launch.sh"
    launch.write_bytes(b"#!/bin/sh\necho original\n")
    launch.chmod(0o751)
    candidate = adopt.AdoptionCandidate(kind=adopt.FOLDER, name="RPG", exe=exe, game_dir=root, prefix_dir=None)
    plan = adopt.plan_adoption(candidate)

    def fail_registration(game: standalone.StandaloneGame, manifest_path: Path) -> None:
        assert manifest_path.exists()
        assert game.launch_script.is_file()
        raise OSError("disk full")

    monkeypatch.setattr(standalone, "_register_standalone_game", fail_registration)
    with pytest.raises(adopt.AdoptionError) as exc:
        adopt.adopt(plan)
    assert exc.value.rolled_back and exe.exists()
    assert launch.read_bytes() == b"#!/bin/sh\necho original\n"
    assert stat.S_IMODE(launch.stat().st_mode) == 0o751
    assert find_manifest(root) is None and not (root / "launch.sh.orig").exists()
    assert not standalone._resolve_desktop_file_path("RPG", plan.game_dir).exists()
    assert plan.prefix_dir is not None and not plan.prefix_dir.exists()


def test_archive_installer_uses_a_folder_separate_from_the_extracted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "RPG" / "setup.exe"
    source.parent.mkdir()
    source.write_bytes(b"MZ")
    (tmp_path / "RPG (2)").mkdir()
    destinations = []

    def install(_installer, destination, **_kwargs):
        destinations.append(destination)
        return destination

    monkeypatch.setattr(ui_workers, "run_windows_installer", install)
    ui_workers.ExtractWorker(source, tmp_path, "RPG").run()
    assert destinations == [tmp_path / "RPG (3)"]
    assert source.read_bytes() == b"MZ"


def test_play_monitor_scopes_game_processes_and_recognizes_owned_zombies(tmp_path: Path) -> None:
    for pid, status, session in ((123, "Z", 10), (456, "S", 20)):
        proc = tmp_path / str(pid)
        proc.mkdir()
        (proc / "stat").write_text(f"{pid} (Game.exe) {status} 1 {session} {session} 0\n", encoding="utf-8")
    assert play_monitor.exe_process_state("Game.exe", str(tmp_path), scope=set(), session_id=10) == (False, True)
    assert play_monitor.exe_process_state("Game.exe", str(tmp_path), scope={456}, session_id=10) == (True, True)
