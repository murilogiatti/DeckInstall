"""Fase 10.3-10.5: saves of Steam store games, Hydra/UMU prefixes and Xodus Nexus games."""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import save_sources, save_sync  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402

USERS = Path("drive_c") / "users" / "steamuser"


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _steam(tmp_path: Path) -> SteamPaths:
    root = tmp_path / "Steam"
    userdata = root / "userdata" / "42"
    (userdata / "config").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    paths = SteamPaths(
        steam_root=root,
        user_id="42",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )
    sa = root / "steamapps"

    def acf(appid: int, name: str) -> None:
        _write(sa / f"appmanifest_{appid}.acf",
               f'"AppState"\n{{\n"appid" "{appid}"\n"name" "{name}"\n"installdir" "{name}"\n}}\n'.encode())

    acf(100, "Proton Game")
    pfx = sa / "compatdata" / "100" / "pfx"
    _write(pfx / USERS / "AppData" / "Roaming" / "Game" / "save.dat", b"proton-save")
    _write(pfx / USERS / "AppData" / "Local" / "Game" / "shadercache" / "big.bin", b"c" * 4096)
    _write(pfx / USERS / "AppData" / "Local" / "Temp" / "junk.tmp", b"t" * 4096)
    acf(200, "Cloud Only")
    _write(userdata / "200" / "remote" / "slot.sav", b"cloud-save")
    _write(userdata / "200" / "remote" / "logs" / "a.log", b"l" * 4096)
    acf(1493710, "Proton Experimental")
    (sa / "compatdata" / "1493710" / "pfx" / "drive_c").mkdir(parents=True)
    acf(300, "No Saves Native")
    return paths


def test_steam_sources_prefix_and_cloud_without_caches(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    found = {s.name: s for s in save_sources.steam_save_sources(paths)}
    assert set(found) == {"Proton Game", "Cloud Only"}  # tools and games without saves are left out
    proton, cloud = found["Proton Game"], found["Cloud Only"]
    assert proton.steam_appid == 100 and proton.extra_dirs == ()
    assert cloud.extra_dirs == (("steamcloud", paths.userdata_dir / "200" / "remote"),)
    assert not cloud.prefix_dir.exists()
    assert save_sources.saves_size(proton.prefix_dir, proton.extra_dirs) == (len(b"proton-save"), 1)
    assert save_sources.saves_size(cloud.prefix_dir, cloud.extra_dirs) == (len(b"cloud-save"), 1)


def test_cloud_only_snapshot_and_restore_never_create_a_prefix(tmp_path: Path) -> None:
    paths = _steam(tmp_path)
    cloud = next(s for s in save_sources.steam_save_sources(paths) if s.name == "Cloud Only")
    snaps = tmp_path / "snaps"
    snap = save_sync.auto_snapshot(cloud.prefix_dir, cloud.name, "manual", dest_root=snaps, extra_dirs=cloud.extra_dirs)
    assert snap is not None
    with zipfile.ZipFile(snap) as zf:
        assert zf.namelist() == ["snap/steamcloud/slot.sav"]
    # same content again: deduplicated
    again = save_sync.auto_snapshot(cloud.prefix_dir, cloud.name, "manual", dest_root=snaps, extra_dirs=cloud.extra_dirs)
    assert again == snap

    remote = cloud.extra_dirs[0][1]
    (remote / "slot.sav").write_bytes(b"newer-local")
    count, safety = save_sync.restore_snapshot_safely(
        snap, cloud.prefix_dir, cloud.name, dest_root=snaps, extra_dirs=cloud.extra_dirs
    )
    assert count == 1 and safety is not None
    assert (remote / "slot.sav").read_bytes() == b"cloud-save"
    assert not cloud.prefix_dir.exists()  # no Wine prefix appears for a native/cloud-only game
    with zipfile.ZipFile(safety) as zf:
        assert zf.read("snap/steamcloud/slot.sav") == b"newer-local"


def _nexus_home(tmp_path: Path, games: list[dict[str, object]], runtime_root: Path | None = None) -> Path:
    home = tmp_path / "home"
    settings: dict[str, object] = {"installRoot": str(home / "Games" / "Xodus Nexus")}
    if runtime_root is not None:
        settings["runtimeRoot"] = str(runtime_root)
    _write(home / ".config" / "xodus-nexus" / "launcher-state.json",
           json.dumps({"games": games, "downloads": [], "settings": settings}).encode())
    return home


def test_nexus_prefix_layouts_and_gdk_saves(tmp_path: Path) -> None:
    data = tmp_path / "home" / ".local" / "share" / "xodus-nexus"
    wgs = USERS / "AppData" / "Local" / "Packages" / "Pub.Game_8wekyb3d8bbwe" / "SystemAppData" / "wgs"
    _write(data / "compatdata" / "aaaa000000000001" / "pfx" / wgs / "container.1", b"gdk")
    build = "b" * 64
    _write(data / "runtime-prefixes" / "proton-xodus" / build / "aaaa000000000002" / "compatdata" / "pfx"
           / USERS / "Saved Games" / "g.sav", b"exp")
    _write(data / "prefixes" / "aaaa000000000003" / USERS / "Documents" / "w.sav", b"wine")
    games = [
        {"id": "aaaa000000000001", "title": "Game Layout", "path": "/x/1"},
        {"id": "aaaa000000000002", "title": "Build Layout", "path": "/x/2",
         "runtimePrefixIdentity": {"layout": "build", "family": "proton-xodus", "origin": {"buildId": build}}},
        {"id": "aaaa000000000003", "title": "Legacy Wine", "path": "/x/3"},
        {"id": "../../etc", "title": "Evil"},
        {"id": "aaaa000000000009", "title": "Never Launched"},
    ]
    home = _nexus_home(tmp_path, games)
    found = {s.name: s for s in save_sources.nexus_save_sources(home)}
    assert set(found) == {"Game Layout", "Build Layout", "Legacy Wine"}
    assert found["Game Layout"].prefix_dir == data / "compatdata" / "aaaa000000000001" / "pfx"
    assert found["Build Layout"].prefix_dir.parts[-4:] == (build, "aaaa000000000002", "compatdata", "pfx")
    assert save_sources.saves_size(found["Game Layout"].prefix_dir)[1] == 1  # wgs container included
    assert save_sources.saves_size(found["Build Layout"].prefix_dir)[1] == 1
    assert save_sources.saves_size(found["Legacy Wine"].prefix_dir)[1] == 1


def test_nexus_custom_runtime_root_and_missing_state(tmp_path: Path) -> None:
    assert save_sources.nexus_save_sources(tmp_path / "nobody") == []
    custom = tmp_path / "elsewhere" / "runtimes"
    _write(custom.parent / "compatdata" / "cccc000000000001" / "pfx" / USERS / "Documents" / "s.sav")
    home = _nexus_home(tmp_path, [{"id": "cccc000000000001", "title": "Custom"}], runtime_root=custom)
    assert [s.name for s in save_sources.nexus_save_sources(home)] == ["Custom"]


def test_nexus_running_detection_reads_proc_comm(tmp_path: Path) -> None:
    real_open = open

    def fake_open(path, *a, **k):  # type: ignore[no-untyped-def]
        if str(path) == "/proc/4242/comm":
            from io import StringIO
            return StringIO("xodus-nexus\n")
        return real_open(path, *a, **k)

    with mock.patch.object(save_sources.os, "listdir", return_value=["4242", "self"]), \
            mock.patch("builtins.open", fake_open):
        assert save_sources.is_nexus_running()
    with mock.patch.object(save_sources.os, "listdir", return_value=[]):
        assert not save_sources.is_nexus_running()


def test_restore_refused_while_nexus_runs(tmp_path: Path) -> None:
    from deckinstall.ui_dialogs import save_sync_dialog as ssd

    t = ssd.SaveTarget("N", tmp_path / "pfx", None, None, None, "nexus")
    entry = save_sync.SaveArchiveEntry(tmp_path / "s.zip", "save_snapshot", "", "n", 0.0, 0)
    with mock.patch.object(ssd, "is_nexus_running", return_value=True), \
            mock.patch.object(ssd, "restore_snapshot_safely") as restore:
        try:
            ssd._restore_job(t, entry)
        except RuntimeError:
            pass
        else:
            raise AssertionError("restore must be refused")
        restore.assert_not_called()


def test_scan_external_targets_marks_origins(tmp_path: Path) -> None:
    from deckinstall.ui_dialogs import save_sync_dialog as ssd

    paths = _steam(tmp_path)
    data = tmp_path / "home" / ".local" / "share" / "xodus-nexus"
    _write(data / "compatdata" / "dddd000000000001" / "pfx" / USERS / "Documents" / "s.sav")
    home = _nexus_home(tmp_path, [{"id": "dddd000000000001", "title": "Halo"}])
    targets = ssd.scan_external_save_targets(paths, home)
    assert {(t.name, t.source) for t in targets} == {("Proton Game", "steam"), ("Cloud Only", "steam"), ("Halo", "nexus")}
    assert ssd.source_label("steam") == "Steam" and ssd.source_label("nexus") == "Nexus"
