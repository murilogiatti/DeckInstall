"""Adopted / imported games that are also Steam shortcuts: one library row, shortcut repointed."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import vdf

from deckinstall import runtime_manager as rm
from deckinstall import runtime_uninstall as ru
from deckinstall import standalone
from deckinstall.steam_paths import SteamPaths
from deckinstall.steam_vdf import (
    ShortcutEntry,
    bulk_migrate_compat_tool,
    load_shortcuts,
    orphan_shortcut_mappings,
    update_shortcut_paths,
)
from deckinstall.vdf_queue import QueuedVdfEdit, VdfEditQueue

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _game(tmp_path: Path, name: str, meta: dict | None = None) -> standalone.StandaloneGame:
    src = tmp_path / name
    src.mkdir()
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game(name, src / "Game.exe", src)
    if meta:
        game.meta.update(meta)
    return game


def _sc(appid: int, name: str, exe: str) -> ShortcutEntry:
    return ShortcutEntry("0", appid, name, f'"{exe}"', '"/old"', "", "")


def _paths(tmp_path: Path, shortcuts: dict) -> SteamPaths:
    root = tmp_path / "Steam"
    user = root / "userdata" / "1" / "config"
    user.mkdir(parents=True)
    (root / "config").mkdir()
    sv = user / "shortcuts.vdf"
    sv.write_bytes(vdf.binary_dumps({"shortcuts": shortcuts}))
    return SteamPaths(root, "1", root / "userdata" / "1", sv, user / "grid", root / "config" / "config.vdf",
                      root / "steamapps" / "compatdata")


def test_owned_shortcuts_by_manifest_appid_or_exe(tmp_path: Path) -> None:
    adopted = _game(tmp_path, "Adopted", {"source": {"origin": "steam", "shortcut_appid": 3000000001}})
    imported = _game(tmp_path, "Imported", {"steam": {"shortcut_appid": 3000000002}})
    by_exe = _game(tmp_path, "ByExe")
    other_origin = _game(tmp_path, "Heroic", {"source": {"origin": "heroic", "shortcut_appid": 3000000004}})
    entries = [
        _sc(3000000001, "Adopted", "/old/place/Game.exe"),
        _sc(3000000002, "Imported", str(imported.exe_path)),
        _sc(3000000003, "ByExe", str(by_exe.exe_path)),
        _sc(3000000004, "Heroic", "/x/y.exe"),
        _sc(3000000005, "Unrelated", "/z/z.exe"),
    ]
    owned = standalone.match_owned_shortcuts(entries, [adopted, imported, by_exe, other_origin])
    assert {k: g.name for k, g in owned.items()} == {
        3000000001: "Adopted", 3000000002: "Imported", 3000000003: "ByExe"
    }
    repairs = standalone.shortcut_path_repairs(entries, [adopted, imported, by_exe, other_origin])
    assert [(e.appid_u32, g.name) for e, g in repairs] == [(3000000001, "Adopted")]  # old folder is gone


def test_update_shortcut_paths_keeps_the_appid(tmp_path: Path) -> None:
    signed = 3000000001 - (1 << 32)
    paths = _paths(tmp_path, {"0": {"appid": signed, "AppName": "G", "Exe": '"/old/G.exe"', "StartDir": '"/old"',
                                    "LaunchOptions": "MANGOHUD=1 %command%"}})
    new_exe = tmp_path / "lib" / "G" / "G.exe"
    new_exe.parent.mkdir(parents=True)
    new_exe.touch()
    assert update_shortcut_paths(paths, 3000000001, new_exe, new_exe.parent, expected_exe="/old/G.exe")
    (entry,) = load_shortcuts(paths.shortcuts_vdf)
    assert entry.appid_u32 == 3000000001 and entry.exe == f'"{new_exe}"' and entry.start_dir == f'"{new_exe.parent}"'
    assert entry.launch_options == "MANGOHUD=1 %command%"
    assert not update_shortcut_paths(paths, 123, new_exe, new_exe.parent, expected_exe="/old/G.exe")
    assert update_shortcut_paths(paths, 3000000001, new_exe, new_exe.parent, expected_exe="/old/G.exe")


def test_hydra_match_uses_original_path_without_appid(tmp_path: Path) -> None:
    original = tmp_path / "old"
    game = _game(tmp_path, "Adopted", {"source": {
        "kind": "adopted", "origin": "hydra", "original_game_dir": str(original),
    }})
    game.exe_path = game.game_dir / "Binaries" / "Play.exe"
    game.exe_path.parent.mkdir(parents=True)
    game.exe_path.touch()
    linked = ShortcutEntry("0", 3000000001, "Old title", f'"{original}/Binaries/Play.exe"', f'"{original}"', "", "")
    same_name = _sc(3000000002, "Adopted", str(original / "Different.exe"))
    assert standalone.shortcut_path_repairs([linked, same_name], [game], "1") == [(linked, game)]
    assert standalone.repaired_shortcut_start_dir(linked, game) == game.game_dir
    assert standalone.match_owned_shortcuts([linked], [game], "2") == {linked.appid_u32: game}


def test_shortcut_edited_after_queueing_is_preserved(tmp_path: Path) -> None:
    paths = _paths(tmp_path, {"0": {"appid": 3000000001 - (1 << 32), "exe": '"/different/G.exe"'}})
    before = paths.shortcuts_vdf.read_bytes()
    new_exe = tmp_path / "G.exe"
    new_exe.touch()
    with pytest.raises(ValueError, match="target changed"):
        update_shortcut_paths(paths, 3000000001, new_exe, tmp_path, expected_exe="/old/G.exe")
    assert paths.shortcuts_vdf.read_bytes() == before


def test_queue_applies_the_repoint(tmp_path: Path) -> None:
    paths = _paths(tmp_path, {"0": {"appid": 3000000001 - (1 << 32), "AppName": "G", "Exe": '"/old/G.exe"',
                                    "StartDir": '"/old"'}})
    other = paths.steam_root / "userdata" / "2" / "config" / "shortcuts.vdf"
    other.parent.mkdir(parents=True)
    other.write_bytes(paths.shortcuts_vdf.read_bytes())
    new_exe = tmp_path / "G.exe"
    new_exe.touch()
    queue = VdfEditQueue()
    queue.enqueue(QueuedVdfEdit("update_shortcut_paths:2:3000000001", "update_shortcut_paths", 3000000001, "G",
                                {"exe_path": str(new_exe), "start_dir": str(tmp_path), "user_id": "2",
                                 "steam_root": str(paths.steam_root), "expected_exe": '"/old/G.exe"'}))
    assert queue.apply_all(paths).applied == 1
    assert load_shortcuts(paths.shortcuts_vdf)[0].exe == '"/old/G.exe"'
    assert load_shortcuts(other)[0].exe == f'"{new_exe}"'


def test_runtime_uninstall_lists_and_removes(tmp_path: Path) -> None:
    (rm.get_umu_dir()).mkdir(parents=True)
    (rm.get_umu_run_path()).write_bytes(b"x" * 10)
    ge = rm.get_compat_tools_dir() / "GE-Proton11-3"
    ge.mkdir(parents=True)
    (ge / "proton").write_text("#!/bin/sh\n")
    (ge / ".deckinstall-managed").write_text("GE-Proton11-3\n")
    unmanaged = rm.get_compat_tools_dir() / "GE-Proton10-99"
    unmanaged.mkdir()
    game = _game(tmp_path, "Alpha")
    items = {i.name: i for i in ru.list_runtimes(None)}
    assert items["umu-run"].size_bytes == 10
    assert items["GE-Proton11-3"].users == (game.name,)  # Default mode runs the newest GE-Proton
    removed, errors = ru.uninstall([items["umu-run"], items["GE-Proton11-3"]])
    assert sorted(removed) == ["GE-Proton11-3", "umu-run"] and errors == []
    assert not rm.get_umu_dir().exists() and not ge.exists()
    assert unmanaged.is_dir()
    assert [i.name for i in ru.list_runtimes(None)] == []


def test_orphan_cleanup_checks_other_accounts_and_rejects_corruption(tmp_path: Path) -> None:
    active, other, orphan = 0x80000001, 0x80000002, 0x80000003
    paths = _paths(tmp_path, {"0": {"appid": active - (1 << 32)}})
    paths.config_vdf.write_text('"InstallConfigStore" {}')
    assert bulk_migrate_compat_tool(paths.config_vdf, [active, other, orphan], "proton_11") == 3
    other_file = paths.steam_root / "userdata" / "2" / "config" / "shortcuts.vdf"
    other_file.parent.mkdir(parents=True)
    other_file.write_bytes(vdf.binary_dumps({"shortcuts": {"0": {"appid": other - (1 << 32)}}}))
    assert orphan_shortcut_mappings(paths) == [orphan]
    other_file.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Could not read"):
        orphan_shortcut_mappings(paths)
