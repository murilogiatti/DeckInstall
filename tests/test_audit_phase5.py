"""Phase 5 regression tests: DOD Ludusavi cache, GameInfo cache/UTF-16LE, scanners, presets, updater & hwnames."""

from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import (  # noqa: E402
    app_updater,
    classify,
    extractor,
    gameinfo,
    hwnames,
    launch_presets,
    save_sources,
    save_sync,
    theme,
)
from deckinstall.steam_paths import SteamPaths  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def test_ludusavi_manifest_dod_cache_and_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "ludusavi.json"
    manifest.write_text(
        json.dumps({"Alpha": {"steam": {"id": "100"}, "files": {"C:/a": {}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(save_sync, "DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest)
    save_sync._LUDUSAVI_CACHE = None

    calls = 0
    orig_loads = json.loads

    def _counting_loads(s: str, *a: object, **kw: object) -> object:
        nonlocal calls
        calls += 1
        return orig_loads(s, *a, **kw)

    with mock.patch.object(save_sync.json, "loads", side_effect=_counting_loads):
        assert save_sync.find_manifest_game_entry("Missing", steam_appid=100) is not None
        assert save_sync.find_manifest_game_entry("Alpha") is not None
        assert save_sync.find_manifest_game_entry("alpha") is not None
    assert calls == 1

    # Updating manifest invalidates cache automatically on mtime/size change.
    future = time.time() + 10
    manifest.write_text(
        json.dumps({"Beta": {"steam": {"id": "200"}, "files": {"C:/b": {}}}}),
        encoding="utf-8",
    )
    os.utime(manifest, (future, future))
    assert save_sync.find_manifest_game_entry("Beta", steam_appid=200) is not None
    assert save_sync.find_manifest_game_entry("Alpha") is None


def test_home_blocked_roots_and_zip_total_size_guard(tmp_path: Path) -> None:
    assert not save_sync.home_entry_allowed((".local", "share", "vulkan", "implicit_layer.d", "x.json"))
    assert not save_sync.home_entry_allowed((".local", "share", "deckinstall", "library.json"))
    assert not save_sync.home_entry_allowed((".config", "deckinstall", "state.json"))
    assert save_sync.home_entry_allowed((".local", "share", "CoolGame", "save1.sav"))

    # Craft zip metadata exceeding _RESTORE_MAX_TOTAL_BYTES without writing huge payload.
    zpath = tmp_path / "big.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("users/steamuser/Saved Games/a.sav", b"x")
    fake_info = mock.Mock(
        filename="users/steamuser/Saved Games/a.sav",
        file_size=save_sync._RESTORE_MAX_TOTAL_BYTES + 1,
        external_attr=0,
        is_dir=lambda: False,
    )
    fake_zf = mock.MagicMock()
    fake_zf.__enter__.return_value = fake_zf
    fake_zf.infolist.return_value = [fake_info]
    with (
        mock.patch.object(save_sync.zipfile, "ZipFile", return_value=fake_zf),
        pytest.raises(RuntimeError, match="maximum uncompressed size"),
    ):
        save_sync._restore_saves_archive(zpath, tmp_path / "pfx", None)


def test_gameinfo_cache_key_includes_root_and_detects_utf16_dlls(tmp_path: Path) -> None:
    gameinfo._cache.clear()
    root_a = tmp_path / "GameRootA"
    sub = root_a / "bin"
    sub.mkdir(parents=True)
    exe = sub / "game.exe"
    exe.write_bytes(PE + "d3d12.dll\x00".encode("utf-16-le"))

    # Detection under sub alone finds DirectX 12 from UTF-16LE DLL reference.
    gfx1, eng1 = gameinfo.detect_game_tech(exe, sub)
    assert gfx1 == "DirectX 12" and eng1 == "Other"

    # Adding Unity marker in root_a (above sub) produces a distinct cache entry when scanned with root_a.
    (root_a / "UnityPlayer.dll").write_bytes(b"2022.3.1f1")
    gfx2, eng2 = gameinfo.detect_game_tech(exe, root_a)
    assert eng2.startswith("Unity")
    assert (gfx1, eng1) != (gfx2, eng2)


def test_launch_presets_exact_mangohud_preset_and_app_updater_closes_response(tmp_path: Path) -> None:
    st10 = launch_presets.parse_presets("MANGOHUD=1 MANGOHUD_CONFIG=preset=10,fps_limit=60 %command%")
    assert st10["mh_level"] == 0
    assert st10["mh_fps_limit"] == 60

    st3 = launch_presets.parse_presets("MANGOHUD=1 MANGOHUD_CONFIG=preset=1 MANGOHUD_CONFIG=preset=3,position=top-left %command%")
    assert st3["mh_level"] == 3
    assert st3["mh_position"] == "top-left"

    fake_resp = mock.MagicMock()
    fake_resp.json.return_value = {"tag_name": "v0.0.1", "assets": [], "body": "", "html_url": "https://github.com/murilogiatti/DeckInstall/releases"}
    with (
        mock.patch.object(app_updater, "_request_github_with_validated_redirects", return_value=fake_resp),
        mock.patch.object(app_updater, "get_running_appimage", return_value=None),
    ):
        info = app_updater.check_app_update()
    fake_resp.close.assert_called_once()
    assert info.latest_version == "0.0.1"


def test_extractor_cli_error_tail_classify_no_symlink_and_steam_sources_skip_symlink(
    tmp_path: Path,
) -> None:
    arch = tmp_path / "broken.rar"
    arch.write_bytes(b"Rar!")
    fake_proc = mock.Mock()
    with (
        mock.patch.object(extractor, "_select_extract_cmd", return_value=["7z", "x", str(arch)]),
        mock.patch.object(extractor, "_precheck_cli_archive", return_value=10),
        mock.patch.object(extractor, "check_disk_space"),
        mock.patch.object(extractor.subprocess, "Popen", return_value=fake_proc),
        mock.patch.object(
            extractor,
            "_drain_stream_lines",
            side_effect=lambda _p, _c, on_line, _m: (on_line("CRC Failed in data.pak"), 2)[1],
        ),
        pytest.raises(RuntimeError, match="CRC Failed in data.pak"),
    ):
        extractor.extract_archive(arch, tmp_path / "out")

    # Symlink loop inside folder doesn't trap classify_source.
    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / "loop").symlink_to(folder)
    assert classify.classify_source(folder).kind == classify.UNKNOWN

    # Symlinked appmanifest_*.acf is skipped by steam_save_sources.
    steam_root = tmp_path / "steam"
    sa = steam_root / "steamapps"
    sa.mkdir(parents=True)
    real_acf = tmp_path / "outside.acf"
    real_acf.write_text('"AppState"\n{\n "name" "SymlinkGame"\n}\n', encoding="utf-8")
    (sa / "appmanifest_999.acf").symlink_to(real_acf)
    user = steam_root / "userdata" / "1"
    (user / "config").mkdir(parents=True)
    paths = SteamPaths(
        steam_root=steam_root,
        user_id="1",
        userdata_dir=user,
        shortcuts_vdf=user / "config" / "shortcuts.vdf",
        grid_dir=user / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=sa / "compatdata",
    )
    assert save_sources.steam_save_sources(paths) == []


def test_hwnames_hybrid_dgpu_and_theme_contrast(tmp_path: Path) -> None:
    drm = tmp_path / "drm"
    pci_amd = tmp_path / "pci" / "0000:05:00.0"
    pci_nv = tmp_path / "pci" / "0000:01:00.0"
    drv_amd = tmp_path / "drivers" / "amdgpu"
    drv_nv = tmp_path / "drivers" / "nvidia"
    for p in (pci_amd, pci_nv, drv_amd, drv_nv):
        p.mkdir(parents=True)
    (pci_amd / "driver").symlink_to(drv_amd)
    (pci_nv / "driver").symlink_to(drv_nv)
    (drm / "renderD128").mkdir(parents=True)
    (drm / "renderD129").mkdir(parents=True)
    (drm / "renderD130" / "device").mkdir(parents=True)  # missing driver symlink is skipped safely
    (drm / "renderD128" / "device").symlink_to(pci_amd)
    (drm / "renderD129" / "device").symlink_to(pci_nv)

    order = hwnames._mangohud_gpus(str(drm))
    assert order == ["0000:05:00.0", "0000:01:00.0"]

    hwnames.mangohud_gpu_text.cache_clear()
    try:
        with (
            mock.patch.object(
                hwnames,
                "_gpus",
                return_value=[
                    ("0000:01:00.0", "GA107M [GeForce RTX 3050 Mobile]"),
                    ("0000:05:00.0", "Cezanne [Radeon Vega Series]"),
                ],
            ),
            mock.patch.object(hwnames, "_mangohud_gpus", return_value=order),
        ):
            text = hwnames.mangohud_gpu_text()
    finally:
        hwnames.mangohud_gpu_text.cache_clear()
    assert text == "Cezanne-Radeon+RTX-3050" or "RTX-3050" in text
    assert theme._DARK_TOKENS["disabled_fg"] == "#8d99b2"
    assert theme._LIGHT_TOKENS["disabled_fg"] == "#5f6b80"
