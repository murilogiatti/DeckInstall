"""Residue harness: install, switch mode and uninstall in both modes, then diff the disk.

Everything the test home, the libraries and Steam hold is listed before and after. What is left
must be in INTENTIONAL_RESIDUE (with the reason) or the test fails and prints the leftovers.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from unittest import mock

import pytest

from deckinstall import library, mode_switch, standalone
from deckinstall import uninstall as un
from deckinstall.steam_paths import SteamPaths
from deckinstall.steam_vdf import load_shortcuts, remove_shortcut

# Relative to the test base. Each entry: what stays on purpose after a full uninstall.
INTENTIONAL_RESIDUE = (
    # The library itself (its folders, library.json and game index) stays for the next game.
    "DeckInstall",
    "DeckInstall/Games",
    "DeckInstall/Games/.deckinstall-library*",
    "DeckInstall/Prefixes",
    "DeckInstall/Caches",
    "DeckInstall/library.json",
    "DeckInstall/.library.lock",
    "data",
    "data/libraries.json",
    "data/libraries.lock",
    # Steam's own files: the shortcut entry is removed, the files (and their backups) stay.
    "Steam/**",
    # The XDG menu folder and the logs root are shared with other games.
    "home",
    "home/.local",
    "home/.local/share",
    "home/.local/share/applications",
    "home/.local/share/DeckInstall",
    "home/.local/share/DeckInstall/logs",
    "home/Desktop",
    # SAVES item: the saves snapshot taken before the prefix is deleted is kept on purpose.
    "home/Games",
    "home/Games/saves",
    "home/Games/saves/**",
    # Lock files of Steam file edits (empty, reused by the next edit).
    "home/.local/state",
    "home/.local/state/DeckInstall",
    "home/.local/state/DeckInstall/**",
    # GNOME stores the desktop shortcut's "trusted" flag here, not DeckInstall.
    "home/.local/share/gvfs-metadata",
    "home/.local/share/gvfs-metadata/**",
)
# A switch to Steam mode leaves these, and they are fine to keep in Standalone mode:
# pfx -> . is the layout umu-run itself creates in every prefix.
ROUND_TRIP_RESIDUE = (
    "DeckInstall/Prefixes/*/pfx",
    "home/.local/state",
    "home/.local/state/DeckInstall",
    "home/.local/state/DeckInstall/**",
)
_SKIP_TOP = {"proc", "runtime-session"}


def _snapshot(base: Path) -> set[str]:
    out: set[str] = set()
    for root, dirs, files in os.walk(base):
        rel_root = Path(root).relative_to(base)
        if rel_root == Path("."):
            dirs[:] = [d for d in dirs if d not in _SKIP_TOP]
        for name in [*dirs, *files]:
            out.add(str(rel_root / name) if rel_root != Path(".") else name)
    return out


def _unexpected(before: set[str], after: set[str]) -> list[str]:
    return sorted(
        p for p in after - before
        if not any(
            fnmatch.fnmatch(p, pat) or (pat.endswith("/**") and p.startswith(pat[:-2]))
            for pat in INTENTIONAL_RESIDUE
        )
    )


def _steam(root: Path) -> SteamPaths:
    userdata = root / "userdata" / "42"
    for d in (userdata / "config" / "grid", root / "config", root / "steamapps" / "compatdata"):
        d.mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=root,
        user_id="42",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )


@pytest.fixture
def base() -> Path:
    assert library.INTERNAL_ROOT is not None
    return library.INTERNAL_ROOT.parent


def _install(downloads: Path) -> standalone.StandaloneGame:
    src = downloads / "Indie"
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    (src / "data.pak").write_bytes(b"x" * 64)
    game = standalone.install_standalone_game(
        "Indie", src / "game.exe", src, launch_options="gamemoderun %command%", create_desktop_shortcut=True,
    )
    (game.prefix_dir / "drive_c" / "users").mkdir(parents=True, exist_ok=True)
    (game.prefix_dir / "drive_c" / "users" / "save.dat").write_text("progress", encoding="utf-8")
    return game


def _uninstall_everything(game: standalone.StandaloneGame) -> un.UninstallResult:
    plan = un.plan_uninstall(game)
    result = un.execute_uninstall(plan, {i.id for i in plan.items})
    assert not result.errors, result.errors
    return result


@pytest.fixture(autouse=True)
def _quiet() -> object:
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "sync_prefix_symlinks"),
    ):
        yield


def test_standalone_mode_leaves_no_residue(base: Path, tmp_path: Path) -> None:
    before = _snapshot(base)
    game = _install(tmp_path / "Downloads")
    assert _snapshot(base) - before, "the install wrote nothing?"
    _uninstall_everything(game)
    leftovers = _unexpected(before, _snapshot(base))
    assert leftovers == [], "\n".join(leftovers)


def test_steam_mode_leaves_no_residue(base: Path, tmp_path: Path) -> None:
    paths = _steam(base / "Steam")
    before = _snapshot(base)
    game = mode_switch.switch_to_steam(paths, _install(tmp_path / "Downloads"))
    assert len(load_shortcuts(paths.shortcuts_vdf, strict=True)) == 1
    (entry,) = load_shortcuts(paths.shortcuts_vdf, strict=True)
    _uninstall_everything(game)
    remove_shortcut(paths, entry.appid_u32)  # what the Library does right after the uninstall
    assert load_shortcuts(paths.shortcuts_vdf, strict=True) == []
    assert list(paths.compatdata_dir.iterdir()) == []
    leftovers = _unexpected(before, _snapshot(base))
    assert leftovers == [], "\n".join(leftovers)


def test_mode_round_trip_adds_nothing(base: Path, tmp_path: Path) -> None:
    paths = _steam(base / "Steam")
    game = _install(tmp_path / "Downloads")
    installed = _snapshot(base)
    game = mode_switch.switch_to_steam(paths, game)
    game = mode_switch._reload(game)  # as the app does: the next switch starts from the manifest
    game = mode_switch.switch_to_standalone(paths, game)
    after = _snapshot(base)
    assert standalone.find_game_launchers(game)["desktop"], "the Desktop launcher must come back"
    assert load_shortcuts(paths.shortcuts_vdf, strict=True) == []
    steam_files = {p for p in after - installed if p.startswith("Steam/")}
    extra = sorted(
        p for p in (after - installed) - steam_files
        if not any(fnmatch.fnmatch(p, pat) for pat in ROUND_TRIP_RESIDUE)
    )
    assert extra == [], "\n".join(extra)
    assert sorted(p for p in installed - after if not p.startswith("Steam/")) == []
