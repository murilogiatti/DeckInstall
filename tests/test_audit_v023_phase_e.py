"""Regression coverage for Steam import rollback and runtime maintenance."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deckinstall import app_updater, launch_presets, optiscaler, runtime_manager, standalone, steam_vdf
from deckinstall.steam_paths import SteamPaths


def _legacy_game_and_steam(base: Path) -> tuple[standalone.StandaloneGame, SteamPaths]:
    game_dir = base / "game"
    game_dir.mkdir()
    exe = game_dir / "game.exe"
    exe.write_bytes(b"MZ")
    prefix = base / "legacy-prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "save").write_bytes(b"standalone")
    game = standalone.StandaloneGame(
        name="Legacy Game", game_dir=game_dir, exe_path=exe, prefix_dir=prefix,
        proton_name="GE-Proton11-1", steam_appid=None, winedlloverrides="",
        launch_options="", launch_script=game_dir / "launch.sh", desktop_file=game_dir / "game.desktop",
        schema=1,
    )
    root = base / "Steam"
    userdata = root / "userdata" / "42"
    cfg = userdata / "config"
    cfg.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "config.vdf").write_text('"InstallConfigStore" {}\n', encoding="utf-8")
    paths = SteamPaths(
        steam_root=root, user_id="42", userdata_dir=userdata, shortcuts_vdf=cfg / "shortcuts.vdf",
        config_vdf=root / "config" / "config.vdf", grid_dir=cfg / "grid",
        compatdata_dir=root / "steamapps" / "compatdata",
    )
    return game, paths


@pytest.mark.parametrize("stage", ["copy", "publish"])
def test_legacy_import_rolls_back_steam_files_and_keeps_both_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    game, paths = _legacy_game_and_steam(tmp_path)
    old_exe = tmp_path / "former-location" / game.exe_path.name
    appid = steam_vdf.add_or_update_shortcut(paths, game.name, old_exe)
    compat = paths.compatdata_dir / str(appid)
    old_file = compat / "pfx" / "drive_c" / "save"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"steam")
    saved = {p: p.read_bytes() for p in (paths.shortcuts_vdf, paths.config_vdf)}
    replace = os.replace

    def fail_publish(src: Path, dst: Path) -> None:
        if dst == compat and Path(src).parent.name.startswith(".deckinstall-import-"):
            raise OSError(errno.ENOSPC, "publish failed")
        replace(src, dst)

    def fail_copy(_source: Path, dest: Path, *, move: bool) -> None:
        assert not move
        (dest / "drive_c" / "save").write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "copy failed")

    if stage == "copy":
        monkeypatch.setattr(standalone, "copy_or_move_dir_contents", fail_copy)
    else:
        monkeypatch.setattr(standalone.os, "replace", fail_publish)
    with pytest.raises(OSError, match=f"{stage} failed"):
        standalone.import_standalone_to_steam(paths, game)
    assert all(path.read_bytes() == data for path, data in saved.items())
    assert old_file.read_bytes() == b"steam"
    assert (game.prefix_dir / "drive_c" / "save").read_bytes() == b"standalone"
    assert list(paths.compatdata_dir.iterdir()) == [compat]


def test_legacy_import_reuses_original_appid(tmp_path: Path) -> None:
    game, paths = _legacy_game_and_steam(tmp_path)
    appid = steam_vdf.add_or_update_shortcut(paths, game.name, tmp_path / "moved" / "game.exe")
    assert standalone.import_standalone_to_steam(paths, game) == appid
    assert steam_vdf.load_shortcuts(paths.shortcuts_vdf, strict=True)[0].exe == f'"{game.exe_path}"'
    assert (paths.compatdata_dir / str(appid) / "pfx" / "drive_c" / "save").read_bytes() == b"standalone"
    # The shader cache is on by default (launch.sh adds it): Steam's Proton gets it written out.
    assert "PROTON_LOCAL_SHADER_CACHE=1" in steam_vdf.load_shortcuts(paths.shortcuts_vdf, strict=True)[0].launch_options


def test_manually_installed_umu_proton_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_manager, "get_compat_tools_dir", lambda: tmp_path)
    tool = tmp_path / "UMU-Proton-11.0-42-x86_64"
    tool.mkdir()
    user_file = tool / "my-settings"
    user_file.write_bytes(b"keep")
    monkeypatch.setattr(runtime_manager, "list_umu_proton_dirs", lambda: [])
    monkeypatch.setattr(runtime_manager, "_get_arch", lambda: "x86_64")
    monkeypatch.setattr(runtime_manager, "_fetch_latest_umu_proton_release", lambda: {
        "tag_name": "UMU-Proton-11.0-42",
        "assets": [
            {"name": f"{tool.name}{suffix}", "browser_download_url": "https://github.com/test/test"}
            for suffix in (".tar.gz", ".sha512sum")
        ],
    })
    download = Mock()
    monkeypatch.setattr(runtime_manager, "_download_file", download)
    with pytest.raises(ValueError, match="outside DeckInstall"):
        runtime_manager.repair_managed_proton(tool)
    with pytest.raises(ValueError, match="was preserved"):
        runtime_manager.ensure_umu_proton()  # a broken copy someone else put there is never replaced
    assert user_file.read_bytes() == b"keep"
    download.assert_not_called()


@pytest.mark.parametrize("kind", ["standalone", "external"])
def test_empty_prefix_self_link_is_not_returned(kind: str, tmp_path: Path) -> None:
    (tmp_path / "pfx").symlink_to(".")
    path, _ = runtime_manager.resolve_game_prefix(kind, SimpleNamespace(prefix_dir=tmp_path), None)
    assert path == tmp_path


def test_runtime_lock_serializes_updates_for_the_same_package() -> None:
    started, another_entered, finish = Event(), Event(), Event()

    @runtime_manager._serialize_runtime("umu")
    def first() -> None:
        started.set()
        assert finish.wait(5)

    @runtime_manager._serialize_runtime("umu")
    def second() -> None:
        another_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_job = pool.submit(first)
        assert started.wait(5)
        second_job = pool.submit(second)
        try:
            assert not another_entered.wait(0.1)
        finally:
            finish.set()
        first_job.result(timeout=5)
        second_job.result(timeout=5)
    assert another_entered.is_set()


def test_stream_cancellation_stops_child_that_closed_stdout() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os, time; os.close(1); time.sleep(15)"],
        stdout=subprocess.PIPE, start_new_session=True,
    )
    polls = 0

    def cancel() -> bool:
        nonlocal polls
        polls += 1
        return polls >= 3

    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            runtime_manager.drain_stream_lines(proc, cancel, lambda _: None, "cancelled")
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_optiscaler_keeps_original_install_on_reinstall_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    game, payload, prefix = (tmp_path / part for part in ("game", "payload", "prefix"))
    for path in (game, payload, prefix):
        path.mkdir()
    exe = game / "game.exe"
    exe.write_bytes(b"MZ")
    proxy = game / "DXGI.dll"
    proxy.write_bytes(b"original")
    payload_dll = payload / "OptiScaler.dll"
    payload_dll.write_bytes(b"OptiScaler old")
    snapshot = Mock()
    monkeypatch.setattr("deckinstall.save_sync.snapshot_prefix_state", snapshot)
    with pytest.raises(optiscaler.OptiScalerConflictError):
        optiscaler.install_optiscaler_for_game(exe, payload_dir=payload)
    optiscaler.install_optiscaler_for_game(
        exe, overwrite_existing=True, payload_dir=payload, prefix_dir=prefix, auto_snapshot=False,
    )
    snapshot.assert_not_called()
    assert proxy.read_bytes() == b"OptiScaler old" and not (game / "dxgi.dll").exists()
    state = game / optiscaler.OPTISCALER_MANIFEST
    original_state = state.read_bytes()
    payload_dll.write_bytes(b"OptiScaler new")
    write = optiscaler._write_state_manifest

    def fail_after_copy(new_state: optiscaler.OptiScalerState) -> None:
        if new_state.complete:
            raise OSError("manifest write failed")
        write(new_state)

    monkeypatch.setattr(optiscaler, "_write_state_manifest", fail_after_copy)
    with pytest.raises(OSError, match="manifest write failed"):
        optiscaler.install_optiscaler_for_game(exe, payload_dir=payload)
    assert state.read_bytes() == original_state and proxy.read_bytes() == b"OptiScaler old"
    optiscaler.uninstall_optiscaler_for_game(exe)
    assert proxy.read_bytes() == b"original"


def test_mangohud_wrapper_survives_preset_changes() -> None:
    options = "MANGOHUD_CONFIG=preset=3 mangohud %command%"
    assert launch_presets.parse_presets(options)["mangohud"]
    assert "mangohud %command%" in launch_presets.apply_presets(options, no_fsync=True)
    assert launch_presets.apply_presets(options, mangohud=False) == "PROTON_LOCAL_SHADER_CACHE=1 %command%"


@pytest.mark.parametrize("valid", [True, False])
def test_appimage_resolves_checksum_only_for_selected_release_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool,
) -> None:
    target = tmp_path / "DeckInstall.AppImage"
    target.touch()
    monkeypatch.setattr(app_updater, "get_running_appimage", lambda: target)
    monkeypatch.setattr(app_updater, "_normalize_arch", lambda *_: "x86_64")
    name = "DeckInstall-99.0.0-x86_64.AppImage"
    base_url = "https://github.com/example/deckinstall/releases/download/99.0.0/"
    release = Mock()
    release.json.return_value = {
        "tag_name": "v99.0.0",
        "assets": [{"name": n, "browser_download_url": base_url + n} for n in (name, "SHA256SUMS")],
    }
    checksum = Mock()
    session = Mock()
    session.__enter__ = Mock(return_value=checksum)
    session.__exit__ = Mock(return_value=False)
    checksum.iter_content.return_value = [
        f"{'a' * 64}  unrelated.AppImage\n{'b' * 64}  {name if valid else name + '.old'}\n".encode()
    ]
    monkeypatch.setattr(app_updater, "_request_github_with_validated_redirects", Mock(side_effect=[release, session]))
    info = app_updater.check_app_update()
    assert info.can_self_update == valid
    assert info.expected_sha256 == ("b" * 64 if valid else "")
    assert info.cannot_update_reason == ("" if valid else "no_checksum")
