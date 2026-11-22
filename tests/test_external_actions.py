"""Tests for deckinstall.external_actions."""

from __future__ import annotations

import builtins
import hashlib
from pathlib import Path
from unittest.mock import patch

import deckinstall.external_actions as external_actions
from deckinstall.external_actions import (
    build_steam_shortcut_payload,
    create_steam_shortcut_from_external,
    import_external_saves_to_target,
    load_external_launch_presets,
    save_external_launch_presets,
)
from deckinstall.launcher_scan import ExternalLauncherGame
from deckinstall.steam_paths import SteamPaths


def _make_external_game(tmp_path: Path, runner: str = "wine") -> tuple[ExternalLauncherGame, Path]:
    ext_root = tmp_path / "external_launcher"
    launcher_cfg = ext_root / "lutris_game.yml"
    launcher_cfg.parent.mkdir(parents=True, exist_ok=True)
    launcher_cfg.write_text("name: External RPG\nrunner: wine\n", encoding="utf-8")

    game_dir = ext_root / "games" / "ExternalRPG"
    game_dir.mkdir(parents=True, exist_ok=True)
    exe_path = game_dir / "game.exe"
    exe_path.write_bytes(b"MZ-fake-exe")

    ext_prefix = ext_root / "prefixes" / "external_rpg"
    save_file = ext_prefix / "drive_c" / "users" / "deck" / "Documents" / "RPG" / "slot1.sav"
    save_file.parent.mkdir(parents=True, exist_ok=True)
    save_file.write_bytes(b"external-save-v1")

    progdata_save = ext_prefix / "drive_c" / "ProgramData" / "RPGStudio" / "profile.dat"
    progdata_save.parent.mkdir(parents=True, exist_ok=True)
    progdata_save.write_bytes(b"progdata-v1")

    ext = ExternalLauncherGame(
        source="lutris",
        name="External RPG",
        exe_path=exe_path,
        game_dir=game_dir,
        prefix_dir=ext_prefix,
        runner=runner,
        launch_cmd=["lutris", "lutris:rungameid/42"],
    )
    return ext, launcher_cfg


def _make_steam_paths(tmp_path: Path) -> SteamPaths:
    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "123456"
    (userdata / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "config").mkdir(parents=True, exist_ok=True)
    compatdata_dir = steam_root / "steamapps" / "compatdata"
    compatdata_dir.mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=steam_root,
        user_id="123456",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=compatdata_dir,
    )


def _snapshot_tree(root: Path) -> dict[Path, tuple[bytes, int]]:
    snap: dict[Path, tuple[bytes, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[p.relative_to(root)] = (p.read_bytes(), p.stat().st_mtime_ns)
    return snap


def test_build_steam_shortcut_payload_direct_and_launcher_cmd(tmp_path: Path) -> None:
    ext, _ = _make_external_game(tmp_path, runner="wine")

    direct = build_steam_shortcut_payload(ext, launch_options="MANGOHUD=1 %command%")
    assert direct["app_name"] == "External RPG"
    assert direct["exe_path"] == ext.exe_path
    assert direct["start_dir"] == ext.exe_path.parent
    assert direct["launch_options"] == "MANGOHUD=1 %command%"
    assert direct["enable_proton"] is True

    with patch("deckinstall.external_actions.shutil.which", return_value="/usr/bin/lutris"):
        via_launcher = build_steam_shortcut_payload(
            ext, launch_options="gamemoderun %command%", use_launcher_cmd=True
        )
        without_presets = build_steam_shortcut_payload(ext, use_launcher_cmd=True)
    assert via_launcher["exe_path"] == Path("/usr/bin/lutris")
    assert via_launcher["launch_options"] == "gamemoderun %command% lutris:rungameid/42"
    assert without_presets["launch_options"] == "%command% lutris:rungameid/42"
    assert via_launcher["enable_proton"] is False

    linux_ext, _ = _make_external_game(tmp_path / "linux_case", runner="linux")
    linux_direct = build_steam_shortcut_payload(linux_ext)
    assert linux_direct["enable_proton"] is False
    assert linux_direct["launch_options"] is None


def test_import_external_saves_to_target_preserves_external_files(tmp_path: Path) -> None:
    ext, _ = _make_external_game(tmp_path)
    before = _snapshot_tree(tmp_path / "external_launcher")

    target_pfx = tmp_path / "target_compat" / "999999" / "pfx"
    target_pfx.mkdir(parents=True, exist_ok=True)
    snap_calls: list[tuple[Path, str]] = []

    copied = import_external_saves_to_target(
        ext,
        target_pfx,
        "External RPG",
        auto_snapshot_fn=lambda p, n: snap_calls.append((p, n)),
    )

    assert copied == 2
    assert snap_calls == [(target_pfx, "External RPG")]
    assert (
        target_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "RPG" / "slot1.sav"
    ).read_bytes() == b"external-save-v1"
    assert (
        target_pfx / "drive_c" / "ProgramData" / "RPGStudio" / "profile.dat"
    ).read_bytes() == b"progdata-v1"

    # External launcher files and prefix must remain completely untouched
    assert _snapshot_tree(tmp_path / "external_launcher") == before


def test_create_steam_shortcut_from_external(tmp_path: Path) -> None:
    ext, launcher_cfg = _make_external_game(tmp_path)
    before = _snapshot_tree(tmp_path / "external_launcher")
    paths = _make_steam_paths(tmp_path)
    fake_appid = 3141592653

    with (
        patch("deckinstall.external_actions.add_or_update_shortcut", return_value=fake_appid) as mock_add,
        patch("deckinstall.external_actions.sync_prefix_symlinks") as mock_sync,
    ):
        appid = create_steam_shortcut_from_external(
            paths,
            ext,
            launch_options="MANGOHUD=1 %command%",
            use_launcher_cmd=False,
            import_saves=True,
        )

    assert appid == fake_appid
    mock_add.assert_called_once()
    assert mock_add.call_args.kwargs["app_name"] == "External RPG"
    assert mock_add.call_args.kwargs["exe_path"] == ext.exe_path
    mock_sync.assert_called_once_with(paths)

    imported_save = (
        paths.compatdata_dir
        / str(fake_appid)
        / "pfx"
        / "drive_c"
        / "users"
        / "steamuser"
        / "Documents"
        / "RPG"
        / "slot1.sav"
    )
    assert imported_save.read_bytes() == b"external-save-v1"
    assert launcher_cfg.read_text(encoding="utf-8") == "name: External RPG\nrunner: wine\n"
    assert _snapshot_tree(tmp_path / "external_launcher") == before




def test_load_and_save_external_launch_presets_fallback_json(tmp_path: Path, monkeypatch) -> None:
    ext, _ = _make_external_game(tmp_path)
    fallback_file = tmp_path / "config" / "external_launch_presets.json"
    monkeypatch.setattr(external_actions, "_PRESETS_FALLBACK_FILE", fallback_file)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "PySide6.QtCore" or name.startswith("PySide6"):
            raise ImportError("PySide6 disabled in test")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=fake_import):
        assert load_external_launch_presets(ext) == ""
        save_external_launch_presets(ext, "ENABLE_LSFG=1 MANGOHUD=1 %command%")
        assert load_external_launch_presets(ext) == "ENABLE_LSFG=1 MANGOHUD=1 %command%"

    assert fallback_file.is_file()
    expected_digest = hashlib.sha256(f"{ext.source}:{ext.exe_path}".encode()).hexdigest()[:16]
    assert f"external_launch_presets/{expected_digest}" in fallback_file.read_text(encoding="utf-8")
