"""Flatpak Steam folder access, the system-changes ledger and duplicate Steam shortcuts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from deckinstall import runtime_manager, steam_sandbox, system_changes
from deckinstall.steam_vdf import ShortcutEntry, shortcuts_for_exe

HOME = Path("/home/u")


@pytest.mark.parametrize(
    ("entries", "path", "visible"),
    [
        ([], HOME / "DeckInstall", False),
        (["xdg-music:ro", "xdg-pictures:ro"], HOME / "DeckInstall", False),
        (["home"], HOME / "DeckInstall/Games/X", True),
        (["~/DeckInstall"], HOME / "DeckInstall/Games/X", True),
        (["~/DeckInstall:ro"], HOME / "Other", False),
        (["/run/media/u/SD"], Path("/run/media/u/SD/DeckInstall"), True),
        (["host"], Path("/mnt/games"), True),
        (["home", "!home"], HOME / "DeckInstall", False),
    ],
)
def test_flatpak_coverage(entries: list[str], path: Path, visible: bool) -> None:
    assert steam_sandbox.flatpak_can_see(path, entries, home=HOME) is visible


def test_flatpak_permissions_read_from_override_files(tmp_path: Path) -> None:
    override = tmp_path / ".local/share/flatpak/overrides" / steam_sandbox.FLATPAK_APP_ID
    override.parent.mkdir(parents=True)
    override.write_text("[Context]\nfilesystems=~/DeckInstall;/mnt/games:ro;\n", encoding="utf-8")
    with mock.patch.object(steam_sandbox.shutil, "which", return_value=None):
        assert steam_sandbox.flatpak_filesystems(home=tmp_path) == ["~/DeckInstall", "/mnt/games:ro"]


def test_native_steam_needs_nothing(tmp_path: Path) -> None:
    assert steam_sandbox.folders_steam_cannot_see(tmp_path / ".local/share/Steam", [tmp_path]) == []


def test_flatpak_folders_listed_and_granted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    games = tmp_path / "games"
    games.mkdir()
    monkeypatch.setattr(steam_sandbox, "flatpak_filesystems", lambda: ["xdg-music:ro"])
    assert steam_sandbox.folders_steam_cannot_see(root, [games, games]) == [games.resolve()]
    with mock.patch.object(steam_sandbox.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        assert steam_sandbox.grant_flatpak_access([games])[0]
    assert run.call_args.args[0] == [
        "flatpak", "override", "--user", f"--filesystem={games}", steam_sandbox.FLATPAK_APP_ID,
    ]


def test_ledger_records_once_and_forgets() -> None:
    system_changes.record_change("k", "t", "undo 1", needs_root=False)
    system_changes.record_change("k", "t", "undo 2", needs_root=False)
    system_changes.record_change("k", "u", "undo 3", needs_root=True)
    changes = system_changes.list_changes()
    assert [(c["target"], c["undo"]) for c in changes] == [("t", "undo 2"), ("u", "undo 3")]
    system_changes.forget_change("k", "t")
    assert [c["target"] for c in system_changes.list_changes()] == ["u"]
    data = json.loads(system_changes.ledger_path().read_text(encoding="utf-8"))
    assert data["version"] == 1


def test_package_install_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_manager, "get_system_package_install_cmd", lambda _b: ["apt-get", "install", "-y", "gamemode"]
    )
    monkeypatch.setattr(runtime_manager.shutil, "which", lambda _n: "/usr/bin/pkexec")
    with mock.patch.object(runtime_manager.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        assert runtime_manager.install_system_package("gamemoderun")[0]
    (change,) = system_changes.list_changes()
    assert change["kind"] == system_changes.KIND_PACKAGE
    assert change["undo"] == "sudo apt remove gamemode" and change["needs_root"]


def test_bwrap_profile_never_overwrites_a_foreign_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_manager.shutil, "which", lambda n: f"/usr/bin/{n}")
    with mock.patch.object(runtime_manager.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        assert runtime_manager.fix_bwrap_userns(confirmed=True)[0]
    script = run.call_args.args[0][-1]
    assert "was not written by DeckInstall" in script and "# Written by DeckInstall" in script
    (change,) = system_changes.list_changes()
    assert change["kind"] == system_changes.KIND_APPARMOR and "apparmor_parser -R" in change["undo"]


def test_shortcuts_for_exe_matches_any_name(tmp_path: Path) -> None:
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    link = tmp_path / "link.exe"
    link.symlink_to(exe)
    entries = [
        ShortcutEntry("0", 1, "My Game", f'"{exe}"', "", "", ""),
        ShortcutEntry("1", 2, "Renamed", f'"{link}"', "", "", ""),
        ShortcutEntry("2", 3, "Other", '"/elsewhere/Other.exe"', "", "", ""),
    ]
    assert [e.appid_u32 for e in shortcuts_for_exe(entries, exe)] == [1, 2]


def test_remove_everything_keeps_games_and_foreign_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import remove_all

    data = tmp_path / "data" / "DeckInstall"
    (data / "logs").mkdir(parents=True)
    (data / "libraries.json").write_text("{}", encoding="utf-8")
    (data / "migrations").mkdir()
    (data / "my-backup").mkdir()  # not DeckInstall's: must stay
    config = tmp_path / "config" / "DeckInstall"
    config.mkdir(parents=True)
    games = tmp_path / "DeckInstall" / "Games" / "G"
    games.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(system_changes, "ledger_path", lambda: data / "system-changes.json")
    monkeypatch.setattr("deckinstall.runtime_uninstall.list_runtimes", lambda paths=None: [])
    system_changes.record_change(system_changes.KIND_FLATPAK_ACCESS, str(games.parent), "x", needs_root=False)
    system_changes.record_change(system_changes.KIND_APPARMOR, "/etc/apparmor.d/bwrap", "sudo rm it", needs_root=True)
    monkeypatch.setattr(steam_sandbox, "revoke_flatpak_access", lambda folders: (True, ""))

    parts = remove_all.plan()
    assert [p.key for p in parts] == [remove_all.APP_DATA, remove_all.MIGRATION_BACKUPS, remove_all.SYSTEM_CHANGES]
    assert remove_all.unknown_data_entries() == [data / "my-backup"]
    chosen = [p for p in parts if p.default_on]
    res = remove_all.execute(chosen)
    assert res.root_commands == ["sudo rm it"] and not res.errors
    assert not (data / "logs").exists() and not (data / "libraries.json").exists() and not config.exists()
    assert (data / "migrations").is_dir() and (data / "my-backup").is_dir() and games.is_dir()
    # The root change is still in the ledger so it can be undone later.
    assert [c["kind"] for c in system_changes.list_changes()] == [system_changes.KIND_APPARMOR]


def test_remove_everything_also_covers_the_fixed_local_share_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deckinstall import remove_all

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-data"))
    fixed = home / ".local" / "share" / "DeckInstall"
    (fixed / "logs" / "g").mkdir(parents=True)
    (fixed / "vdf_queue.json").write_text("{}", encoding="utf-8")
    (fixed / "my-notes.txt").write_text("mine", encoding="utf-8")  # not written by DeckInstall
    (tmp_path / "custom-data" / "DeckInstall").mkdir(parents=True)
    (tmp_path / "custom-data" / "DeckInstall" / "deckinstall.log").write_text("x", encoding="utf-8")
    monkeypatch.setattr("deckinstall.runtime_uninstall.list_runtimes", lambda: [])
    monkeypatch.setattr("deckinstall.system_changes.list_changes", lambda: [])
    parts = remove_all.plan()
    app = next(p for p in parts if p.key == remove_all.APP_DATA)
    assert fixed / "logs" in app.paths and fixed / "vdf_queue.json" in app.paths
    assert tmp_path / "custom-data" / "DeckInstall" / "deckinstall.log" in app.paths
    assert fixed / "my-notes.txt" in remove_all.unknown_data_entries()
    remove_all.execute([app])
    assert not (fixed / "logs").exists() and not (fixed / "vdf_queue.json").exists()
    assert (fixed / "my-notes.txt").read_text(encoding="utf-8") == "mine"
