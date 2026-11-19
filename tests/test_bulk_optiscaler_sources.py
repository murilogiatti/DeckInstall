"""Bulk OptiScaler for Steam shortcuts and Steam store games, plus Steam restart and Gaming Mode guards."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import vdf  # noqa: E402

from deckinstall import optiscaler as opti  # noqa: E402
from deckinstall import steam_vdf  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.ui_dialogs import optiscaler_bulk_dialog as bulk  # noqa: E402
from deckinstall.vdf_queue import QueuedVdfEdit, VdfEditQueue  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _payload(root: Path) -> Path:
    payload = root / "payload"
    (payload / "D3D12_Optiscaler").mkdir(parents=True)
    (payload / "OptiScaler.dll").write_bytes(b"OPTI")
    (payload / "OptiScaler.ini").write_text("[Upscalers]\n", encoding="utf-8")
    (payload / "nvngx.dll").write_bytes(b"NV")
    (payload / "D3D12_Optiscaler" / "D3D12Core.dll").write_bytes(b"D3D")
    return payload


def _steam(tmp_path: Path) -> SteamPaths:
    root = tmp_path / "steam"
    user = root / "userdata" / "1"
    (user / "config").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    paths = SteamPaths(
        steam_root=root,
        user_id="1",
        userdata_dir=user,
        shortcuts_vdf=user / "config" / "shortcuts.vdf",
        grid_dir=user / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )
    paths.localconfig_vdf.write_text(
        vdf.dumps({"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {"10": {"x": "1"}}}}}}}),
        encoding="utf-8",
    )
    return paths


def _store_game(paths: SteamPaths, appid: int = 500, name: str = "Store Game") -> Path:
    sa = paths.steam_root / "steamapps"
    game = sa / "common" / "StoreGame"
    game.mkdir(parents=True)
    (game / "StoreGame.exe").write_bytes(PE)
    (sa / f"appmanifest_{appid}.acf").write_text(
        vdf.dumps({"AppState": {"appid": str(appid), "name": name, "installdir": "StoreGame"}}), encoding="utf-8"
    )
    (sa / "compatdata" / str(appid) / "pfx").mkdir(parents=True)
    return game


def _shortcut_game(tmp_path: Path, paths: SteamPaths) -> Path:
    game = tmp_path / "Shortcut Game"
    game.mkdir()
    (game / "Game.exe").write_bytes(PE)
    data = {
        "shortcuts": {
            "0": {
                "appid": steam_vdf.compute_shortcut_appid(f'"{game / "Game.exe"}"', "Shortcut Game")[1],
                "AppName": "Shortcut Game",
                "Exe": f'"{game / "Game.exe"}"',
                "StartDir": f'"{game}"',
                "icon": "",
                "LaunchOptions": "gamemoderun %command%",
            },
            "1": {"appid": 1, "AppName": "Linux thing", "Exe": "/usr/bin/foo", "StartDir": "/", "LaunchOptions": ""},
        }
    }
    paths.shortcuts_vdf.write_bytes(vdf.binary_dumps(data))
    return game


def test_localconfig_launch_options_roundtrip(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    assert steam_vdf.get_steam_app_launch_options(paths, 500) == ""
    assert steam_vdf.set_steam_app_launch_options(paths, 500, 'WINEDLLOVERRIDES="dxgi=n,b" %command%')
    assert steam_vdf.get_steam_app_launch_options(paths, 500) == 'WINEDLLOVERRIDES="dxgi=n,b" %command%'
    assert list(paths.localconfig_vdf.parent.glob("localconfig.vdf.*.deckinstall.bak"))  # backup first
    text = paths.localconfig_vdf.read_text(encoding="utf-8")
    assert '"x"' in text  # other apps untouched


def test_list_steam_library_games_needs_prefix_and_exe(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    _store_game(paths)
    sa = paths.steam_root / "steamapps"
    (sa / "common" / "NativeGame").mkdir(parents=True)
    (sa / "appmanifest_600.acf").write_text(
        vdf.dumps({"AppState": {"appid": "600", "name": "Native", "installdir": "NativeGame"}}), encoding="utf-8"
    )
    games = steam_vdf.list_steam_library_games(paths)
    assert [(g.appid, g.name, g.exe_path.name) for g in games] == [(500, "Store Game", "StoreGame.exe")]


def test_bulk_lists_shortcuts_and_store_games_and_writes_all_at_once(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    _store_game(paths)
    sc_game = _shortcut_game(tmp_path, paths)
    targets = bulk._collect_targets(paths)
    assert sorted((t.source, t.name) for t in targets) == [("shortcut", "Shortcut Game"), ("steam", "Store Game")]

    with mock.patch.object(opti, "ensure_optiscaler", return_value=_payload(tmp_path)):
        outcomes, writes = bulk._install_files(targets, remove=False, backend="auto")
    assert {o.status for o in outcomes} == {"installed"}
    assert (sc_game / "dxgi.dll").is_file()
    assert all('WINEDLLOVERRIDES="dxgi=n,b"' in w[3] for w in writes)
    assert any("gamemoderun" in w[3] for w in writes if w[0] == "shortcut")  # existing options kept

    assert bulk._write_vdf(paths, writes) == []
    shortcut = steam_vdf.load_shortcuts(paths.shortcuts_vdf)[0]
    assert "dxgi=n,b" in shortcut.launch_options
    assert "dxgi=n,b" in steam_vdf.get_steam_app_launch_options(paths, 500)


def test_queue_applies_store_game_launch_options(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    paths.shortcuts_vdf.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    q = VdfEditQueue()
    q.enqueue(QueuedVdfEdit("set_app_launch_options:500", "set_app_launch_options", 500, "G", {"launch_options": "a"}))
    q.enqueue(QueuedVdfEdit("set_app_launch_options:500", "set_app_launch_options", 500, "G", {"launch_options": "b"}))
    assert q.pending_count() == 1
    assert q.apply_all(paths).errors == []
    assert steam_vdf.get_steam_app_launch_options(paths, 500) == "b"


def test_shutdown_refuses_in_gaming_mode() -> None:
    with (
        mock.patch.object(steam_vdf, "is_steam_running", return_value=True),
        mock.patch.object(steam_vdf, "is_gamescope_session", return_value=True),
        mock.patch.object(steam_vdf.subprocess, "run") as run,
        pytest.raises(steam_vdf.SteamGamingModeError),
    ):
        steam_vdf.shutdown_steam()
    run.assert_not_called()


def test_start_steam_uses_clean_env_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDIR", "/tmp/.mount_X")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_X/usr/lib:/usr/lib")
    monkeypatch.setenv("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
    monkeypatch.setenv("PYTHONHOME", "/tmp/.mount_X/usr")
    states = iter([False, False, True])
    with (
        mock.patch.object(steam_vdf, "is_steam_running", side_effect=lambda: next(states)),
        mock.patch.object(steam_vdf, "is_gamescope_session", return_value=False),
        mock.patch.object(steam_vdf, "is_flatpak_steam", return_value=False),
        mock.patch.object(steam_vdf.subprocess, "Popen") as popen,
        mock.patch.object(steam_vdf.time, "sleep"),
    ):
        assert steam_vdf.start_steam() is True
    kwargs = popen.call_args.kwargs
    env = kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert "APPDIR" not in env and "PYTHONHOME" not in env and "QT_QPA_PLATFORMTHEME" not in env
    assert env["LD_LIBRARY_PATH"] == "/usr/lib"


def test_start_steam_reports_failure_and_skips_gaming_mode() -> None:
    with (
        mock.patch.object(steam_vdf, "is_steam_running", return_value=False),
        mock.patch.object(steam_vdf, "is_gamescope_session", return_value=True),
        mock.patch.object(steam_vdf.subprocess, "Popen") as popen,
    ):
        assert steam_vdf.start_steam() is False
    popen.assert_not_called()
    with (
        mock.patch.object(steam_vdf, "is_steam_running", return_value=False),
        mock.patch.object(steam_vdf, "is_gamescope_session", return_value=False),
        mock.patch.object(steam_vdf.subprocess, "Popen"),
        mock.patch.object(steam_vdf.time, "sleep"),
    ):
        assert steam_vdf.start_steam(wait_seconds=0.0) is False


def test_qt_environment_no_portal_in_gaming_mode() -> None:
    from deckinstall.steam_paths import configure_qt_environment

    env = {"XDG_CURRENT_DESKTOP": "gamescope", "DISPLAY": ":0", "QT_QPA_PLATFORMTHEME": "xdgdesktopportal"}
    configure_qt_environment(env)
    assert "QT_QPA_PLATFORMTHEME" not in env and env["QT_QPA_PLATFORM"] == "xcb;wayland"

    env = {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", "QT_QPA_PLATFORM": "wayland"}
    configure_qt_environment(env)
    assert env == {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", "QT_QPA_PLATFORM": "wayland"}  # user choice wins

    env = {"XDG_CURRENT_DESKTOP": "KDE", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    configure_qt_environment(env)
    assert env["QT_QPA_PLATFORMTHEME"] == "xdgdesktopportal" and "QT_QPA_PLATFORM" not in env

    env = {"XDG_CURRENT_DESKTOP": "KDE"}
    configure_qt_environment(env)
    assert "QT_QPA_PLATFORMTHEME" not in env  # no D-Bus: no portal
