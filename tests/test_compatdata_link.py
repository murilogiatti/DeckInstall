"""One game, one prefix: Steam's compatdata/<appid> is a link to the game's own prefix."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import standalone  # noqa: E402
from deckinstall.cleanup import find_cleanup_candidates  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402

APPID = 3000000001


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


def _steam_prefix(compat: Path, save: str = "steam-save") -> None:
    """A compatdata folder as Steam's Proton creates it."""
    saves = compat / "pfx" / "drive_c" / "users" / "steamuser" / "Saved Games"
    saves.mkdir(parents=True)
    (saves / "slot1.sav").write_text(save, encoding="utf-8")
    (compat / "version").write_text("10.0-1\n", encoding="utf-8")


def test_missing_compatdata_becomes_link_with_umu_pfx(tmp_path: Path) -> None:
    prefix = tmp_path / "Prefixes" / "uid"
    compat = tmp_path / "compatdata" / str(APPID)
    assert standalone.link_compatdata_to_prefix(compat, prefix) is None
    assert compat.is_symlink() and compat.resolve() == prefix.resolve()
    assert os.readlink(prefix / "pfx") == "."
    # Proton opens <compatdata>/pfx: it must land on the game prefix itself.
    assert (compat / "pfx").resolve() == prefix.resolve()
    assert standalone.link_compatdata_to_prefix(compat, prefix) is None  # idempotent


def test_steam_prefix_is_adopted_when_game_prefix_is_empty(tmp_path: Path) -> None:
    prefix = tmp_path / "Prefixes" / "uid"
    prefix.mkdir(parents=True)
    compat = tmp_path / "compatdata" / str(APPID)
    _steam_prefix(compat)

    assert standalone.link_compatdata_to_prefix(compat, prefix) is None

    assert (prefix / "drive_c" / "users" / "steamuser" / "Saved Games" / "slot1.sav").read_text() == "steam-save"
    assert (prefix / "version").is_file()
    assert compat.is_symlink()
    assert (compat / "pfx" / "drive_c").is_dir()


def test_both_prefixes_with_data_keep_steam_one_as_backup(tmp_path: Path) -> None:
    prefix = tmp_path / "Prefixes" / "uid"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "game.sav").write_text("game-save", encoding="utf-8")
    compat = tmp_path / "compatdata" / str(APPID)
    _steam_prefix(compat)

    backup = standalone.link_compatdata_to_prefix(compat, prefix)

    assert backup == compat.with_name(f"{APPID}.deckinstall.bak")
    assert (backup / "pfx" / "drive_c" / "users" / "steamuser" / "Saved Games" / "slot1.sav").read_text() == "steam-save"
    assert (prefix / "drive_c" / "game.sav").read_text() == "game-save"
    assert compat.is_symlink()


def test_link_to_another_prefix_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "microsd" / "compatdata" / str(APPID)
    _steam_prefix(other)
    compat = tmp_path / "compatdata" / str(APPID)
    compat.parent.mkdir(parents=True)
    compat.symlink_to(other)
    with pytest.raises(RuntimeError):
        standalone.link_compatdata_to_prefix(compat, tmp_path / "Prefixes" / "uid")
    assert compat.resolve() == other.resolve()


def test_import_links_records_and_uninstall_unlinks(tmp_path: Path) -> None:
    paths = _steam(tmp_path / "Steam")
    src = tmp_path / "Downloads" / "Indie"
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    with (
        mock.patch.object(standalone, "APPLICATIONS_DIR", tmp_path / "applications"),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        game = standalone.install_standalone_game("Indie", src / "game.exe", src, proton_name="GE-Proton11-1")
        with (
            mock.patch.object(standalone, "add_or_update_shortcut", return_value=APPID),
            mock.patch.object(standalone, "set_shortcut_proton"),
            mock.patch.object(standalone, "sync_prefix_symlinks"),
        ):
            assert standalone.import_standalone_to_steam(paths, game) == APPID

        compat = paths.compatdata_dir / str(APPID)
        assert compat.is_symlink() and compat.resolve() == game.prefix_dir.resolve()
        data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
        steam = data["created"]["steam"]
        assert steam["shortcut_appid"] == APPID
        assert steam["user_id"] == "42"
        assert steam["compat_tool"] == "GE-Proton11-1"
        loaded = standalone._load_manifest(game.game_dir / MANIFEST_V2)
        assert loaded is not None

        # The linked compatdata is never offered as an orphan, even without a shortcut.
        found = {c.path for c in find_cleanup_candidates(paths, extra_external_roots=[], home_dir=tmp_path / "home")}
        assert compat not in found

        standalone.remove_standalone_game(loaded, delete_files=True)
    assert not compat.is_symlink() and not compat.exists()
    assert not game.prefix_dir.exists()


def test_v1_game_keeps_copy_behavior(tmp_path: Path) -> None:
    paths = _steam(tmp_path / "Steam")
    game_dir = tmp_path / "Games" / "Old"
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_bytes(b"MZ")
    prefix = tmp_path / "Games" / ".prefixes" / "Old"
    (prefix / "drive_c").mkdir(parents=True)
    game = standalone.StandaloneGame(
        name="Old",
        game_dir=game_dir,
        exe_path=game_dir / "game.exe",
        prefix_dir=prefix,
        proton_name="",
        steam_appid=None,
        winedlloverrides="",
        launch_options="",
        launch_script=game_dir / "launch.sh",
        desktop_file=tmp_path / "x.desktop",
    )
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "add_or_update_shortcut", return_value=APPID),
        mock.patch.object(standalone, "set_shortcut_proton"),
        mock.patch.object(standalone, "sync_prefix_symlinks"),
    ):
        standalone.import_standalone_to_steam(paths, game)
    compat = paths.compatdata_dir / str(APPID)
    assert not compat.is_symlink()
    assert (compat / "pfx" / "drive_c").is_dir()
    assert (prefix / "drive_c").is_dir()
