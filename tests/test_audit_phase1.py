"""Phase 1 regression tests: core data integrity, VDFDict, prefix symlinks, YAML/Ludusavi, cleanup and play monitor."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import vdf  # noqa: E402

from deckinstall import (  # noqa: E402
    cleanup,
    fsutil,
    launcher_scan,
    library,
    ludusavi,
    play_monitor,
    runtime_manager,
    save_sync,
    standalone,
    steam_vdf,
)
from deckinstall.steam_paths import SteamPaths  # noqa: E402


def _make_steam(root: Path) -> SteamPaths:
    steam = root / "Steam"
    user = steam / "userdata" / "10"
    (user / "config").mkdir(parents=True)
    (steam / "config").mkdir(parents=True)
    (steam / "steamapps" / "compatdata").mkdir(parents=True)
    return SteamPaths(
        steam_root=steam,
        user_id="10",
        userdata_dir=user,
        shortcuts_vdf=user / "config" / "shortcuts.vdf",
        grid_dir=user / "config" / "grid",
        config_vdf=steam / "config" / "config.vdf",
        compatdata_dir=steam / "steamapps" / "compatdata",
    )


def test_vdfdict_updates_and_deletes_without_duplicate_keys(tmp_path: Path) -> None:
    paths = _make_steam(tmp_path)
    paths.config_vdf.write_text('"InstallConfigStore"\n{\n}\n', encoding="utf-8")
    steam_vdf.set_compat_tool(paths.config_vdf, 3000000001, "proton_10")
    steam_vdf.set_compat_tool(paths.config_vdf, 3000000001, "proton_11")
    steam_vdf.bulk_migrate_compat_tool(paths.config_vdf, [3000000001], "GE-Proton11-12")
    raw = paths.config_vdf.read_text(encoding="utf-8")
    assert raw.count('"3000000001"') == 1
    assert "GE-Proton11-12" in raw
    steam_vdf.remove_compat_tool(paths.config_vdf, 3000000001)
    assert '"3000000001"' not in paths.config_vdf.read_text(encoding="utf-8")

    local_cfg = paths.userdata_dir / "config" / "localconfig.vdf"
    local_cfg.write_text('"UserLocalConfigStore"\n{\n}\n', encoding="utf-8")
    assert steam_vdf.set_steam_app_launch_options(paths, 555, "FIRST=1 %command%")
    assert steam_vdf.set_steam_app_launch_options(paths, 555, "SECOND=1 %command%")
    assert steam_vdf.get_steam_app_launch_options(paths, 555) == "SECOND=1 %command%"
    assert local_cfg.read_text(encoding="utf-8").lower().count('"launchoptions"') == 1


def test_legacy_shortcut_without_appid_does_not_touch_compatdata_zero(tmp_path: Path) -> None:
    paths = _make_steam(tmp_path)
    paths.config_vdf.write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
        '\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"0"\n'
        '\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t"proton_11"\n\t\t\t\t\t}\n\t\t\t\t}\n'
        '\t\t\t}\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    (paths.compatdata_dir / "0").mkdir(parents=True)
    (paths.compatdata_dir / "0" / "global.txt").write_text("keep", encoding="utf-8")

    old_exe = tmp_path / "Games" / "Indie" / "game.exe"
    old_exe.parent.mkdir(parents=True)
    old_exe.write_bytes(b"MZ")
    paths.shortcuts_vdf.write_bytes(
        vdf.binary_dumps({"shortcuts": {"0": {"AppName": "Indie", "Exe": f'"{old_exe}"', "StartDir": f'"{old_exe.parent}"'}}})
    )
    loaded = steam_vdf.load_shortcuts(paths.shortcuts_vdf)
    assert len(loaded) == 1
    expected_appid = loaded[0].appid_u32
    assert expected_appid >= 0x80000000

    # Replace via add_or_update_shortcut pointing to another location: compatdata/0 must stay intact.
    new_exe = tmp_path / "DeckInstall" / "Games" / "Indie" / "game.exe"
    new_exe.parent.mkdir(parents=True)
    new_exe.write_bytes(b"MZ")
    with mock.patch("deckinstall.steam_paths.find_default_proton_name", return_value="proton_11"):
        new_appid = steam_vdf.add_or_update_shortcut(paths, "Indie", new_exe, enable_proton=False)
    assert new_appid >= 0x80000000
    assert (paths.compatdata_dir / "0" / "global.txt").read_text(encoding="utf-8") == "keep"
    assert '"0"' in paths.config_vdf.read_text(encoding="utf-8")


def test_restore_and_sync_work_with_pfx_dot_and_compatdata_symlink(tmp_path: Path) -> None:
    paths = _make_steam(tmp_path)
    prefix = tmp_path / "DeckInstall" / "Prefixes" / "uid123"
    save_file = prefix / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat"
    save_file.parent.mkdir(parents=True)
    save_file.write_bytes(b"BEFORE")
    compat = paths.compatdata_dir / "3100000001"
    standalone.link_compatdata_to_prefix(compat, prefix)
    assert (prefix / "pfx").is_symlink() and compat.is_symlink()

    # DeckInstall zip backup + restore through symlinked compatdata and pfx -> .
    entry = steam_vdf.ShortcutEntry("0", 3100000001, "MyGame", '"/g/a.exe"', '"/g"', "", "")
    with mock.patch("deckinstall.steam_vdf.load_shortcuts", return_value=[entry]):
        zpath, games, files = steam_vdf.backup_all_saves(paths, tmp_path / "backups", games_dir=tmp_path / "none")
        assert (games, files) == (1, 1)
        save_file.write_bytes(b"CORRUPT")
        rgames, rfiles = steam_vdf.restore_saves_backup(paths, zpath)
        assert (rgames, rfiles) == (1, 1)
        assert save_file.read_bytes() == b"BEFORE"

    # save_sync push/pull and snapshot restore on compatdata symlink with pfx -> .
    sync_root = tmp_path / "sync"
    save_file.write_bytes(b"SYNCED")
    save_sync.push_game_saves("MyGame", compat, sync_root)
    save_file.write_bytes(b"LOCAL_OLD")
    pulled, snap = save_sync.pull_game_saves(
        "MyGame", compat, sync_root, snapshots_root=tmp_path / "snaps"
    )
    assert pulled == 1 and snap is not None
    assert save_file.read_bytes() == b"SYNCED"
    restored, safety = save_sync.restore_snapshot_safely(snap, compat, "MyGame", dest_root=tmp_path / "snaps")
    assert restored == 1 and safety is not None
    assert save_file.read_bytes() == b"LOCAL_OLD"


def test_registry_restore_preserves_wine_directives_and_state_snapshot(tmp_path: Path) -> None:
    pfx = tmp_path / "pfx"
    (pfx / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (pfx / "drive_c" / "windows" / "system32" / "dxgi.dll").write_bytes(b"DLL1")
    sys_reg = pfx / "system.reg"
    sys_reg.write_text(
        "WINE REGISTRY Version 2\n\n"
        "[System\\\\CurrentControlSet] 1700000000\n"
        "#link\n"
        '"Link"="ControlSet001"\n\n'
        "[Software\\\\MyGame] 1700000000\n"
        "#time=1da123456\n"
        '"Slot"="A"\n',
        encoding="utf-8",
    )
    reg_bytes = json.dumps({"version": 1, "entries": [{"hive": "HKLM", "subkey": "Software\\MyGame", "lines": ['"Slot"="B"']}]}).encode()
    assert save_sync._restore_registry_saves(pfx, reg_bytes) == 1
    out_reg = sys_reg.read_text(encoding="utf-8")
    assert "#link" in out_reg
    assert '"Slot"="B"' in out_reg

    # State snapshot restore must create a state safety snapshot (not a save snapshot).
    snap = save_sync.snapshot_prefix_state(pfx, "RegGame", dest_root=tmp_path / "snaps")
    assert snap is not None
    (pfx / "drive_c" / "windows" / "system32" / "dxgi.dll").write_bytes(b"DLL2")
    _cnt, safety = save_sync.restore_snapshot_safely(snap.snapshot_zip, pfx, "RegGame", state=True, dest_root=tmp_path / "snaps")
    assert safety is not None
    with zipfile.ZipFile(safety) as zf:
        assert "dll/windows/system32/dxgi.dll" in zf.namelist()
        assert zf.read("dll/windows/system32/dxgi.dll") == b"DLL2"


def test_yaml_apostrophe_unicode_and_ludusavi_zip_backup(tmp_path: Path) -> None:
    game_dir = tmp_path / "ludusavi" / "Baldur's Gate 3"
    game_dir.mkdir(parents=True)
    payload = b"bg3-save"
    import hashlib
    sha1 = hashlib.sha1(payload).hexdigest()
    bname = "backup-20260101T000000Z.zip"
    with zipfile.ZipFile(game_dir / bname, "w") as zf:
        zf.writestr("drive-C/users/deck/AppData/Local/Larian Studios/Baldur's Gate 3/save.lsv", payload)
    (game_dir / "mapping.yaml").write_text(
        "---\n"
        'name: "Baldur\'s Gate 3 \\u00e9"\n'
        "drives:\n"
        '  drive-C: "C:"\n'
        "backups:\n"
        f'  - name: {bname}\n'
        '    when: "2026-01-01T00:00:00Z"\n'
        "    os: windows\n"
        "    files:\n"
        "      C:/users/deck/AppData/Local/Larian Studios/Baldur's Gate 3/save.lsv:\n"
        f"        hash: {sha1}\n"
        f"        size: {len(payload)}\n",
        encoding="utf-8",
    )
    games = ludusavi.list_backup_games(tmp_path / "ludusavi")
    assert games[0][0] == "Baldur's Gate 3 é"
    dst_pfx = tmp_path / "pfx"
    (dst_pfx / "drive_c").mkdir(parents=True)
    res = ludusavi.import_ludusavi(game_dir, dst_pfx, "Baldur's Gate 3", home=tmp_path / "home")
    assert res.restored == 1
    assert (dst_pfx / "drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3/save.lsv").read_bytes() == payload


def test_steamrt4_readonly_check_and_nvidia_corporation_gpu_detection(tmp_path: Path) -> None:
    steamrt4_dir = tmp_path / "steamrt4"
    steamrt4_dir.mkdir()
    (steamrt4_dir / "mtree.txt.gz").write_bytes(b"partial")
    with mock.patch.object(runtime_manager, "get_steamrt4_dir", return_value=steamrt4_dir):
        assert not runtime_manager.is_steamrt4_valid()
        assert steamrt4_dir.is_dir()  # read-only health check must not delete in-progress runtime

    fake_lspci = subprocess.CompletedProcess(
        ["lspci", "-nn"], 0,
        stdout="01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA107M [GeForce RTX 3050 Mobile] [10de:25a2]\n",
        stderr="",
    )
    with (
        mock.patch.object(Path, "glob", return_value=()),
        mock.patch("deckinstall.runtime_manager.subprocess.run", return_value=fake_lspci),
    ):
        prof = runtime_manager.detect_gpu_profile()
    assert prof.has_nvidia and not prof.has_amd and not prof.is_hybrid


def test_cleanup_protects_active_external_prefixes_and_unmounted_steam_apps(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = _make_steam(tmp_path)
    # Libraryfolders records app 777777 on an unmounted SD card.
    (paths.steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t"/run/media/deck/RemovedSD"\n'
        '\t\t"apps"\n\t\t{\n\t\t\t"777777"\t"1000"\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    (paths.compatdata_dir / "777777" / "pfx" / "drive_c").mkdir(parents=True)
    # Heroic game with active prefix inside prefix_roots.
    pfx_root = tmp_path / "HeroicPfx"
    active_pfx = pfx_root / "ActiveGame"
    (active_pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    ext = launcher_scan.ExternalLauncherGame("heroic", "ActiveGame", tmp_path / "g.exe", tmp_path, active_pfx, "wine")
    with (
        mock.patch("deckinstall.launcher_scan.scan_external_launcher_games", return_value=[ext]),
        mock.patch("deckinstall.adopt._scan_hydra", return_value=[]),
        mock.patch.object(cleanup, "external_prefix_roots", return_value=[pfx_root]),
    ):
        cands = cleanup.find_cleanup_candidates(paths, extra_external_roots=[pfx_root], home_dir=home)
    cand_paths = {c.path.resolve() for c in cands}
    assert (paths.compatdata_dir / "777777").resolve() not in cand_paths
    assert active_pfx.resolve() not in cand_paths


def test_play_monitor_prefix_boundaries_cwd_and_bootstrapper(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    watch = ("/home/deck/Games/Portal",)
    for pid, cmd, env, cwd in (
        ("101", "wine\0/home/deck/Games/Portal 2/portal2.exe\0", b"", "/"),
        ("102", "bash\0", b"WINEPREFIX=\0", "/"),
        ("103", "dolphin\0", b"", "/home/deck/Games/Portal"),
        ("104", "wine\0/home/deck/Games/Portal/hl2.exe\0", b"", "/home/deck/Games/Portal"),
    ):
        p = root / pid
        p.mkdir(parents=True)
        (p / "cmdline").write_text(cmd)
        (p / "environ").write_bytes(env)
        (p / "cwd").symlink_to(cwd)
    assert play_monitor.find_game_pids(watch, str(root)) == {104}


def test_atomic_write_preserves_exec_mode_and_autofs_fstype(tmp_path: Path) -> None:
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(script, 0o755)
    fsutil.atomic_write_text(script, "#!/bin/sh\necho ok\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert fsutil.sanitize_dir_name("..") == "Game"

    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/nvme / ext4 rw 0 0\n"
        "systemd-1 /run/media/mmcblk0p1 autofs rw 0 0\n"
        "/dev/mmcblk0p1 /run/media/mmcblk0p1 exfat rw 0 0\n",
        encoding="utf-8",
    )
    table = library.read_mount_table(mounts)
    assert library.fs_type_of(Path("/run/media/mmcblk0p1/DeckInstall"), table) == "exfat"
