"""Tests for newly added DeckInstall features."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from deckinstall.runtime_manager import (
    get_umu_protonfix_status,
    lookup_umu_database,
    validate_ge_proton_integrity,
)
from deckinstall.save_sync import (
    _extract_registry_saves,
    _restore_registry_saves,
    load_ludusavi_manifest,
    preview_sync_operation,
    push_game_saves,
    restore_prefix_state_snapshot,
    snapshot_prefix_state,
)
from deckinstall.scanner import scan_game_folder
from deckinstall.standalone import (
    find_orphan_shader_caches,
    install_standalone_game,
    prune_orphan_shader_caches,
    resolve_umu_game_id,
)
from deckinstall.steam_paths import SteamPaths, dir_size_bytes
from deckinstall.steam_vdf import (
    apply_launch_presets,
    delete_cleanup_candidates_checked,
    find_cleanup_candidates,
    get_steam_input_mode,
    migrate_compatdata_to_microsd,
    restore_compatdata_from_microsd,
    set_steam_input_mode,
)


def _make_steam_paths(tmp_path: Path) -> SteamPaths:
    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "123456"
    (userdata / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=steam_root,
        user_id="123456",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )


# 1. umu-database.csv lookup, resolve_umu_game_id, get_umu_protonfix_status
def test_umu_database_lookup_resolve_and_protonfix_status(tmp_path: Path) -> None:
    db_csv = tmp_path / "umu-database.csv"
    db_csv.write_text(
        "TITLE,STORE,CODENAME,UMU_ID,COMMON_REDIST,NOTE\n"
        "Cyberpunk 2077,gog,cyberpunk2077,umu-1091500,vcrun2019,Works OOTB\n"
        "Cyberpunk 2077,egs,cyberpunk2077_egs,umu-1091500,,EGS build\n"
        "Hades II,steam,hades2,umu-1145350,,\n",
        encoding="utf-8",
    )

    hit = lookup_umu_database(game_title="Cyberpunk 2077", store="gog", db_path=db_csv)
    assert hit is not None
    assert hit.umu_id == "umu-1091500"
    assert hit.store == "gog"
    assert hit.common_redist == "vcrun2019"
    assert hit.match_source == "title"

    exe_hit = lookup_umu_database(
        exe_path=tmp_path / "Games" / "Hades II" / "hades2.exe",
        db_path=db_csv,
    )
    assert exe_hit is not None
    assert exe_hit.umu_id == "umu-1145350"
    assert exe_hit.match_source == "exe_stem"

    # resolve_umu_game_id prefers positive steam_appid, otherwise falls back to umu-database
    assert resolve_umu_game_id(1091500, store="steam", db_path=db_csv) == ("umu-1091500", "steam")
    assert resolve_umu_game_id(None, store="gog", game_title="Cyberpunk 2077", db_path=db_csv) == (
        "umu-1091500",
        "gog",
    )
    assert resolve_umu_game_id(None, game_title="Unknown Game", db_path=db_csv) == (
        "umu-default",
        "none",
    )

    status = get_umu_protonfix_status("umu-1091500", store="gog", db_path=db_csv)
    assert status["found"] is True
    assert status["has_protonfix"] is True
    assert status["umu_id"] == "umu-1091500"
    assert status["common_redist"] == "vcrun2019"
    assert status["db_cached"] is True

    missing_status = get_umu_protonfix_status("umu-default", db_path=db_csv)
    assert missing_status["found"] is False
    assert missing_status["has_protonfix"] is False


# 2. validate_ge_proton_integrity
def test_validate_ge_proton_integrity(tmp_path: Path) -> None:
    valid_dir = tmp_path / "GE-Proton9-10"
    (valid_dir / "files" / "bin").mkdir(parents=True)
    for rel in ("proton", "compatibilitytool.vdf", "toolmanifest.vdf", "files/bin/wine"):
        (valid_dir / rel).write_bytes(b"ok")

    ok_report = validate_ge_proton_integrity(valid_dir)
    assert ok_report.valid is True
    assert ok_report.missing_files == ()
    assert ok_report.broken_symlinks == ()
    assert ok_report.error is None

    # Empty file + broken symlink
    broken_dir = tmp_path / "GE-Proton-Broken"
    (broken_dir / "files" / "bin").mkdir(parents=True)
    (broken_dir / "proton").write_bytes(b"")  # empty
    (broken_dir / "compatibilitytool.vdf").write_bytes(b"ok")
    (broken_dir / "files" / "bin" / "wine").write_bytes(b"ok")
    (broken_dir / "files" / "bin" / "broken_tool").symlink_to(tmp_path / "does_not_exist")

    bad_report = validate_ge_proton_integrity(broken_dir)
    assert bad_report.valid is False
    assert "proton" in bad_report.missing_files
    assert "toolmanifest.vdf" in bad_report.missing_files
    assert "files/bin/broken_tool" in bad_report.broken_symlinks
    assert bad_report.error is not None

    # Missing directory
    missing_report = validate_ge_proton_integrity(tmp_path / "nonexistent")
    assert missing_report.valid is False
    assert missing_report.error == "Proton directory does not exist."


# 3. snapshot_prefix_state & restore_prefix_state_snapshot
def test_snapshot_and_restore_prefix_state(tmp_path: Path) -> None:
    pfx = tmp_path / "prefix" / "pfx"
    pfx.mkdir(parents=True)
    (pfx / "user.reg").write_text('WINE REGISTRY Version 2\n[Software\\\\Test]\n"v"="1"\n', encoding="utf-8")
    sys32 = pfx / "drive_c" / "windows" / "system32"
    sys32.mkdir(parents=True)
    (sys32 / "dxgi.dll").write_bytes(b"orig-dxgi")

    snap_root = tmp_path / "snapshots"
    snap = snapshot_prefix_state(pfx, "Test Game", reason="optiscaler", dest_root=snap_root)
    assert snap is not None
    assert snap.reg_count == 1
    assert snap.dll_count == 1
    assert snap.snapshot_zip.is_file()

    # Mutate prefix
    (pfx / "user.reg").write_text("CORRUPTED", encoding="utf-8")
    (sys32 / "dxgi.dll").write_bytes(b"modified-dxgi")

    reg_restored, dll_restored = restore_prefix_state_snapshot(snap.snapshot_zip, pfx)
    assert reg_restored == 1
    assert dll_restored == 1
    assert "WINE REGISTRY Version 2" in (pfx / "user.reg").read_text(encoding="utf-8")
    assert (sys32 / "dxgi.dll").read_bytes() == b"orig-dxgi"


# 4. load_ludusavi_manifest + _extract_registry_saves/_restore_registry_saves + preview_sync_operation
def test_ludusavi_manifest_registry_saves_and_preview_sync(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "My Game": {
            "steam": {"id": 424242},
            "files": {
                "<winDocuments>/My Game/*": {"tags": ["save"]},
            },
            "registry": {
                "HKEY_CURRENT_USER/Software/MyStudio/MyGame": {"tags": ["save"]},
            },
        }
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    loaded = load_ludusavi_manifest(manifest_file)
    assert "My Game" in loaded
    assert load_ludusavi_manifest(tmp_path / "missing.json") == {}

    src_pfx = tmp_path / "src_pfx"
    src_pfx.mkdir()
    (src_pfx / "user.reg").write_text(
        "WINE REGISTRY Version 2\n"
        "\n"
        "[Software\\\\MyStudio\\\\MyGame] 1700000000\n"
        '"Progress"="chapter3"\n'
        "\n"
        "[Software\\\\Other\\\\Key] 1700000000\n"
        '"Ignore"="yes"\n',
        encoding="utf-8",
    )
    save_file = src_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "My Game" / "save.bin"
    save_file.parent.mkdir(parents=True)
    save_file.write_bytes(b"save-bytes-123")

    reg_bytes = _extract_registry_saves(src_pfx, ["HKEY_CURRENT_USER/Software/MyStudio/MyGame"])
    assert reg_bytes is not None
    reg_json = json.loads(reg_bytes.decode("utf-8"))
    assert len(reg_json["entries"]) == 1
    assert reg_json["entries"][0]["subkey"] == "Software\\MyStudio\\MyGame"

    dst_pfx = tmp_path / "dst_pfx"
    dst_pfx.mkdir()
    restored_keys = _restore_registry_saves(dst_pfx, reg_bytes)
    assert restored_keys == 1
    assert '"Progress"="chapter3"' in (dst_pfx / "user.reg").read_text(encoding="utf-8")

    # preview_sync_operation for PUSH and PULL
    sync_root = tmp_path / "sync_root"
    with patch("deckinstall.save_sync.DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest_file):
        push_preview = preview_sync_operation(
            "My Game",
            src_pfx,
            sync_root,
            action="PUSH",
            steam_appid=424242,
        )
        assert push_preview.action == "PUSH"
        assert push_preview.registry_entries == 1
        assert any(f.rel_path == "registry/registry.json" for f in push_preview.files)
        assert any(f.rel_path.endswith("save.bin") for f in push_preview.files)

        push_game_saves("My Game", src_pfx, sync_root, steam_appid=424242)

        pull_preview = preview_sync_operation(
            "My Game",
            dst_pfx,
            sync_root,
            action="PULL",
            steam_appid=424242,
        )
        assert pull_preview.action == "PULL"
        assert pull_preview.registry_entries == 1
        assert any(f.operation == "PULL" and f.rel_path.endswith("save.bin") for f in pull_preview.files)


# 5. apply_launch_presets with lsfg=True and preserving lsfg-vk when lsfg=False
def test_apply_launch_presets_lsfg_and_preserve_lsfg_vk() -> None:
    opts = apply_launch_presets(
        "",
        gamemode=True,
        mangohud=False,
        wined3d=False,
        no_esync=False,
        no_fsync=False,
        lsfg=True,
        lsfg_multiplier=3,
        lsfg_flow_scale=0.75,
        lsfg_perf_mode=True,
        lsfg_profile="my-profile",
    )
    assert "ENABLE_LSFG=1" in opts
    assert "LSFG_MULTIPLIER=3" in opts
    assert "LSFG_FLOW_SCALE=0.75" in opts
    assert "LSFG_PERF_MODE=1" in opts
    assert "LSFGVK_PROFILE=my-profile" in opts
    assert "gamemoderun %command%" in opts

    # When lsfg=False, lsfg-vk wrapper in existing options must be preserved
    preserved = apply_launch_presets(
        "ENABLE_LSFG=1 LSFG_MULTIPLIER=2 lsfg-vk %command% -dx11",
        gamemode=False,
        mangohud=True,
        wined3d=False,
        no_esync=False,
        no_fsync=False,
        lsfg=False,
    )
    assert "ENABLE_LSFG=" not in preserved
    assert "LSFG_MULTIPLIER=" not in preserved
    assert "lsfg-vk" in preserved
    assert "MANGOHUD=1" in preserved
    assert preserved.endswith("%command% -dx11")


# 6. get_steam_input_mode & set_steam_input_mode
def test_steam_input_mode_get_and_set(tmp_path: Path) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    assert get_steam_input_mode(localconfig, 123456) == "default"

    set_steam_input_mode(localconfig, 123456, "enabled", create_if_missing=True)
    assert get_steam_input_mode(localconfig, 123456) == "enabled"

    set_steam_input_mode(localconfig, 123456, "disabled")
    assert get_steam_input_mode(localconfig, 123456) == "disabled"

    set_steam_input_mode(localconfig, 123456, "default")
    assert get_steam_input_mode(localconfig, 123456) == "default"

    with pytest.raises(ValueError):
        set_steam_input_mode(localconfig, 123456, "invalid-mode")


# 7. migrate_compatdata_to_microsd & restore_compatdata_from_microsd
def test_migrate_and_restore_compatdata_microsd(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path)
    appid = 777888
    src_compat = paths.compatdata_dir / str(appid)
    save_file = src_compat / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat"
    save_file.parent.mkdir(parents=True)
    save_file.write_bytes(b"compat-save-data")

    microsd_compat = tmp_path / "microsd" / "compatdata"
    microsd_compat.mkdir(parents=True)

    with patch("deckinstall.steam_vdf._is_path_on_mounted_volume", return_value=True):
        migrated = migrate_compatdata_to_microsd(
            paths, appid, microsd_compat, verify_mounted=True
        )

    assert migrated == microsd_compat / str(appid)
    assert src_compat.is_symlink()
    assert src_compat.resolve() == migrated.resolve()
    assert (migrated / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat").read_bytes() == b"compat-save-data"

    restored = restore_compatdata_from_microsd(paths, appid, remove_microsd_copy=True)
    assert restored == src_compat
    assert not src_compat.is_symlink()
    assert src_compat.is_dir()
    assert not migrated.exists()
    assert (src_compat / "pfx" / "drive_c" / "users" / "steamuser" / "save.dat").read_bytes() == b"compat-save-data"


# 8. "Prefix Cache" in find_cleanup_candidates + delete_cleanup_candidates_checked recreating Temp
def test_prefix_cache_cleanup_recreates_temp(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path)
    appid = 555111
    manifest = paths.steam_root / "steamapps" / f"appmanifest_{appid}.acf"
    manifest.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"Cached Game"\n}}\n',
        encoding="utf-8",
    )
    temp_dir = (
        paths.compatdata_dir / str(appid) / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Temp"
    )
    temp_dir.mkdir(parents=True)
    junk = temp_dir / "shader_junk.tmp"
    junk.write_bytes(b"1234567890")

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    candidates = find_cleanup_candidates(paths, include_system_caches=True, home_dir=home_dir)
    prefix_caches = [c for c in candidates if c.category == "Prefix Cache"]
    assert len(prefix_caches) == 1
    assert prefix_caches[0].path == temp_dir
    assert prefix_caches[0].size_bytes == 10

    deleted, freed, errors = delete_cleanup_candidates_checked(
        paths=paths,
        candidates=prefix_caches,
        include_system_caches=True,
        home_dir=home_dir,
    )
    assert deleted == 1
    assert freed == 10
    assert errors == []
    assert temp_dir.is_dir()
    assert not junk.exists()


# 9. standalone cache_dir + find_orphan_shader_caches / prune_orphan_shader_caches
def test_standalone_cache_dir_and_orphan_shader_caches(tmp_path: Path) -> None:
    games_dir = tmp_path / "Games"
    games_dir.mkdir()
    app_dir = tmp_path / "applications"
    app_dir.mkdir()

    game_src = games_dir / "src_game"
    game_src.mkdir()
    exe_path = game_src / "game.exe"
    exe_path.write_bytes(b"MZ-exe")

    with (
        patch("deckinstall.standalone.get_games_dir", return_value=games_dir),
        patch("deckinstall.standalone.get_applications_dir", return_value=app_dir),
    ):
        game = install_standalone_game(
            game_name="My Standalone",
            exe_path=exe_path,
            game_dir=game_src,
            create_menu_shortcut=False,
            create_desktop_shortcut=False,
            keep_in_place=True,
        )

        assert game.cache_dir is not None
        assert game.cache_dir.is_dir()
        assert (game.cache_dir / "dxvk").is_dir()
        assert (game.cache_dir / "mesa").is_dir()

        # Create an orphaned shader cache under games_dir/.caches
        orphan_dir = games_dir / ".caches" / "dead-game-cache"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "dxvk.cache").write_bytes(b"abcdef")

        orphans = find_orphan_shader_caches(games_dir=games_dir)
        assert [o.name for o in orphans] == ["dead-game-cache"]
        assert orphans[0].size_bytes == 6

        deleted, freed = prune_orphan_shader_caches(entries=orphans, games_dir=games_dir)
        assert deleted == 1
        assert freed == 6
        assert not orphan_dir.exists()
        assert game.cache_dir.is_dir()


# 10. dir_size_bytes + single-pass scanner.scan_game_folder
def test_dir_size_bytes_and_scan_game_folder(tmp_path: Path) -> None:
    folder = tmp_path / "My.Cool.Game.v1.2"
    (folder / "bin").mkdir(parents=True)
    (folder / "bin" / "CoolGame.exe").write_bytes(b"x" * 100)
    (folder / "bin" / "unins000.exe").write_bytes(b"y" * 50)
    (folder / "steam_appid.txt").write_text("480\n", encoding="utf-8")
    # Symlink should be skipped by dir_size_bytes
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"z" * 1000)
    os.symlink(outside, folder / "link.bin")

    expected_size = 100 + 50 + len("480\n")
    assert dir_size_bytes(folder) == expected_size

    detected = scan_game_folder(folder)
    assert detected is not None
    assert detected.name == "My Cool Game v1 2"
    assert detected.steam_appid == 480
    assert detected.primary_exe == folder / "bin" / "CoolGame.exe"
    assert detected.executables == [folder / "bin" / "CoolGame.exe"]
