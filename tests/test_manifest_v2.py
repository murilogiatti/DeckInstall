"""Tests for the per-game manifest schema v2 (library-relative paths) and v1 compatibility."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import standalone  # noqa: E402
from deckinstall.manifest import (  # noqa: E402
    MANIFEST_V1,
    MANIFEST_V2,
    decode_path,
    encode_path,
    read_manifest_fields,
)
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import find_cleanup_candidates  # noqa: E402


@contextmanager
def _library(games_dir: Path, apps_dir: Path) -> Iterator[None]:
    with (
        mock.patch.object(standalone, "GAMES_DIR", games_dir),
        mock.patch.object(standalone, "PREFIXES_DIR", games_dir / ".prefixes"),
        mock.patch.object(standalone, "APPLICATIONS_DIR", apps_dir),
        mock.patch.dict("os.environ", {"DECKINSTALL_GAMES_DIR": ""}),
    ):
        yield


def _make_source_game(root: Path, name: str = "My Game") -> Path:
    src = root / "Downloads" / name
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    return src


def test_install_writes_v2_with_relative_paths(tmp_path: Path) -> None:
    games_dir = tmp_path / "Games"
    src = _make_source_game(tmp_path)
    with _library(games_dir, tmp_path / "applications"):
        game = standalone.install_standalone_game(
            "My Game",
            src / "game.exe",
            src,
            proton_name="GE-Proton11-1",
            source={"kind": "folder", "origin": str(src)},
        )
        manifest = game.game_dir / MANIFEST_V2
        assert manifest.is_file()
        assert not (game.game_dir / MANIFEST_V1).exists()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["schema"] == 2
        assert data["game"] == {"dir": "My Game", "exe": "game.exe", "native_linux": False}
        assert data["prefix"]["path"] == ".prefixes/My Game"
        assert data["created"]["launch_script"] == "My Game/launch.sh"
        assert data["runtime"]["proton"] == {"mode": "pinned", "name": "GE-Proton11-1"}
        assert data["source"]["kind"] == "folder"
        assert len(data["created"]["desktop_files"]) == 1
        assert game.uid == data["id"]

        listed = standalone.list_standalone_games()
        assert [g.uid for g in listed] == [game.uid]
        assert listed[0].prefix_dir.resolve() == game.prefix_dir.resolve()

        updated = standalone.update_standalone_launch_options(
            game.game_dir, launch_options="gamemoderun %command%"
        )
        again = json.loads(manifest.read_text(encoding="utf-8"))
        assert updated.uid == game.uid
        assert again["id"] == data["id"]
        assert again["created_at"] == data["created_at"]
        assert again["runtime"]["launch_options"] == "gamemoderun %command%"


def test_v2_library_survives_new_mount_point(tmp_path: Path) -> None:
    old_games = tmp_path / "old" / "Games"
    src = _make_source_game(tmp_path)
    with _library(old_games, tmp_path / "applications"):
        game = standalone.install_standalone_game("My Game", src / "game.exe", src)
        assert game.prefix_dir.is_dir()

    new_games = tmp_path / "new" / "Games"
    new_games.parent.mkdir()
    shutil.move(str(old_games), str(new_games))
    with _library(new_games, tmp_path / "applications"):
        loaded = standalone._load_manifest(new_games / "My Game" / MANIFEST_V2)
    assert loaded is not None
    assert loaded.exe_path == (new_games / "My Game" / "game.exe").resolve()
    assert loaded.prefix_dir == (new_games / ".prefixes" / "My Game").resolve()


def test_v1_manifest_still_loads_and_stays_v1(tmp_path: Path) -> None:
    games_dir = tmp_path / "Games"
    game_dir = games_dir / "Old Game"
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_bytes(b"MZ")
    prefix = games_dir / ".prefixes" / "Old Game"
    prefix.mkdir(parents=True)
    (game_dir / MANIFEST_V1).write_text(
        json.dumps({
            "name": "Old Game",
            "exe_path": str(game_dir / "game.exe"),
            "prefix_dir": str(prefix),
            "launch_script": str(game_dir / "launch.sh"),
        }),
        encoding="utf-8",
    )
    with _library(games_dir, tmp_path / "applications"):
        listed = standalone.list_standalone_games()
        assert len(listed) == 1 and listed[0].schema == 1
        standalone.update_standalone_launch_options(game_dir, launch_options="%command% -x")
    assert (game_dir / MANIFEST_V1).is_file()
    assert not (game_dir / MANIFEST_V2).exists()


def test_cleanup_protects_prefix_recorded_only_in_v2(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    games_dir = home_dir / "Games"
    prefixes_dir = games_dir / ".prefixes"
    game_dir = games_dir / "Active"
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_bytes(b"MZ")
    active_prefix = prefixes_dir / "uid-1234"  # name unrelated to the game folder
    (active_prefix / "drive_c" / "users").mkdir(parents=True)
    (game_dir / MANIFEST_V2).write_text(
        json.dumps({
            "schema": 2,
            "id": "uid-1234",
            "name": "Active",
            "game": {"dir": "Active", "exe": "game.exe"},
            "prefix": {"path": ".prefixes/uid-1234"},
        }),
        encoding="utf-8",
    )
    orphan = prefixes_dir / "Orphan"
    (orphan / "drive_c" / "users").mkdir(parents=True)

    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "1"
    for d in (userdata / "config" / "grid", steam_root / "config", steam_root / "steamapps" / "compatdata"):
        d.mkdir(parents=True)
    paths = SteamPaths(
        steam_root=steam_root,
        user_id="1",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )
    found = {c.path.resolve() for c in find_cleanup_candidates(paths, extra_external_roots=[prefixes_dir], home_dir=home_dir)}
    assert active_prefix.resolve() not in found
    assert orphan.resolve() in found


def test_path_encoding_rules(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    assert encode_path(root / "Games" / "X", root) == "Games/X"
    assert decode_path("Games/X", root) == root / "Games" / "X"
    assert encode_path(Path.home() / ".local" / "a.desktop", None) == "~/.local/a.desktop"
    assert decode_path("~/.local/a.desktop", None) == Path.home() / ".local" / "a.desktop"
    for bad in ("../escape", "Games/../../x", "~/../etc"):
        with pytest.raises(ValueError):
            decode_path(bad, root)
    with pytest.raises(ValueError):
        decode_path("Games/X", None)


def test_v2_rejects_escaping_exe(tmp_path: Path) -> None:
    game_dir = tmp_path / "Games" / "Evil"
    game_dir.mkdir(parents=True)
    (game_dir / MANIFEST_V2).write_text(
        json.dumps({
            "schema": 2,
            "game": {"dir": "Evil", "exe": "../../outside.exe"},
            "prefix": {"path": ".prefixes/Evil"},
        }),
        encoding="utf-8",
    )
    assert read_manifest_fields(game_dir / MANIFEST_V2) is None
