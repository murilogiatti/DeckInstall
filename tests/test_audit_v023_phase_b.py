"""Regression tests for Phase B save/Ludusavi/sync hardening and safety fixes."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import ludusavi as lds  # noqa: E402
from deckinstall import save_sources, save_sync, steam_paths  # noqa: E402
from deckinstall.ui_dialogs import save_sync_dialog as ssd  # noqa: E402


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324


def test_c05_unsafe_symlink_prefix_rejected_everywhere(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    bad_pfx = tmp_path / "bad_pfx"
    bad_pfx.symlink_to(outside)

    snap = tmp_path / "snap.zip"
    with zipfile.ZipFile(snap, "w") as zf:
        zf.writestr("snap/users/steamuser/Documents/save.dat", b"data")

    state_snap = tmp_path / "state.zip"
    with zipfile.ZipFile(state_snap, "w") as zf:
        zf.writestr("reg/user.reg", b"WINE REGISTRY Version 2\n")
        zf.writestr("dll/windows/system32/x.dll", b"dll")

    with pytest.raises(OSError):
        save_sync._restore_saves_archive(snap, bad_pfx, None)
    with pytest.raises(OSError):
        save_sync.restore_prefix_state_snapshot(state_snap, bad_pfx)
    with pytest.raises(OSError):
        save_sync._restore_registry_saves(
            bad_pfx,
            json.dumps({"version": 1, "entries": [{"hive": "HKCU", "subkey": "Software\\G", "lines": ['"k"="1"']}]}).encode(),
        )

    backup = tmp_path / "bk" / "Game"
    (backup / "drive-C" / "Users" / "me" / "Documents").mkdir(parents=True)
    save_file = backup / "drive-C" / "Users" / "me" / "Documents" / "slot.sav"
    save_file.write_bytes(b"s1")
    (backup / "mapping.yaml").write_text(
        f'name: Game\ndrives:\n  drive-C: "C:"\nbackups:\n  - name: "."\n    files:\n      "C:/Users/me/Documents/slot.sav":\n        hash: {_sha1(b"s1")}\n        size: 2\n    children: []\n',
        encoding="utf-8",
    )
    with pytest.raises(OSError):
        lds.import_ludusavi(backup, bad_pfx, "Game", home=tmp_path / "home")
    assert list(outside.iterdir()) == []

    # Symlinked mapping.yaml / child folder is ignored by find_game_folder & list_backup_games.
    root_bk = tmp_path / "bk_sym"
    root_bk.mkdir()
    sym_game = root_bk / "Game"
    sym_game.symlink_to(backup)
    assert lds.find_game_folder(root_bk, "Game") is None
    assert lds.list_backup_games(root_bk) == []


def test_c07_a39_registry_oserror_propagates_section_injection_and_rbracket(tmp_path: Path) -> None:
    pfx = tmp_path / "pfx"
    pfx.mkdir()
    reg_file = pfx / "user.reg"
    orig_text = 'WINE REGISTRY Version 2\n\n[Software\\\\Keep\\\\Me] 1\n"Keep"="yes"\n'
    reg_file.write_text(orig_text, encoding="utf-8")

    real_read_text = Path.read_text

    def fail_on_user_reg(self: Path, *args: object, **kwargs: object) -> str:
        if self == reg_file:
            raise PermissionError("EACCES")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    payload = json.dumps({
        "version": 1,
        "entries": [{"hive": "HKCU", "subkey": "Software\\G", "lines": ['"Slot"="2"']}],
    }).encode()
    with mock.patch.object(Path, "read_text", fail_on_user_reg), pytest.raises(PermissionError):
        save_sync._restore_registry_saves(pfx, payload)
    assert reg_file.read_text(encoding="utf-8") == orig_text

    # Section injection lines must be filtered out; values preserved.
    inject_payload = json.dumps({
        "version": 1,
        "entries": [
            {
                "hive": "HKCU",
                "subkey": "Software\\LegitGame",
                "lines": [
                    '"Progress"="5"',
                    "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Run] 1",
                    '"Evil"="evil.exe"',
                    "@=\"default\"",
                ],
            }
        ],
    }).encode()
    count = save_sync._restore_registry_saves(pfx, inject_payload)
    assert count == 1
    out = reg_file.read_text(encoding="utf-8")
    assert '"Progress"="5"' in out
    assert '@="default"' in out
    assert "CurrentVersion\\\\Run" not in out

    # Subkey containing ']' uses rindex(']').
    sections = save_sync._parse_wine_reg_sections(
        'WINE REGISTRY Version 2\n\n[Software\\\\My [Special] Game\\\\Sub] 123\n"K"="V"\n'
    )
    assert sections[0][0] == r"Software\My [Special] Game\Sub"


def test_c08_newly_blocked_home_execution_roots() -> None:
    newly_blocked = (
        ".local/share/bash-completion/completions/git",
        ".local/share/konsole/Evil.profile",
        ".config/autostart-scripts/run.txt",
        ".config/gdb/gdbinit",
        ".config/tmux/tmux.conf",
        ".config/direnv/direnvrc",
        ".config/hypr/hyprland.conf",
        ".config/cargo/config.toml",
        ".config/npm/npmrc",
    )
    for rel in newly_blocked:
        assert not save_sync.home_entry_allowed(tuple(rel.split("/"))), rel


def test_a34_stage_all_and_rollback_leaves_prefix_untouched_on_failure(tmp_path: Path) -> None:
    pfx = tmp_path / "pfx"
    target_a = pfx / "drive_c" / "users" / "steamuser" / "Documents" / "a.sav"
    target_b_dir = pfx / "drive_c" / "users" / "steamuser" / "Documents" / "b.sav"
    target_a.parent.mkdir(parents=True)
    target_a.write_bytes(b"OLD_A")
    target_b_dir.mkdir()  # IsADirectoryError on second target must fail staging before touching a.sav

    snaps = tmp_path / "snaps"
    snap = tmp_path / "bad.zip"
    with zipfile.ZipFile(snap, "w") as zf:
        zf.writestr("snap/users/steamuser/Documents/a.sav", b"NEW_A")
        zf.writestr("snap/users/steamuser/Documents/b.sav", b"NEW_B")

    with pytest.raises(IsADirectoryError) as exc_info:
        save_sync.restore_snapshot_safely(snap, pfx, "RollGame", dest_root=snaps)
    assert target_a.read_bytes() == b"OLD_A"
    assert "safety snapshot:" in str(exc_info.value)

    # Also test Ludusavi commit rollback when os.replace fails on 2nd target.
    bk = tmp_path / "lud" / "RollGame"
    docs = bk / "drive-C" / "Users" / "me" / "Documents"
    docs.mkdir(parents=True)
    (docs / "a.sav").write_bytes(b"LUD_A")
    (docs / "c.sav").write_bytes(b"LUD_C")
    (pfx / "drive_c" / "users" / "steamuser" / "Documents" / "c.sav").write_bytes(b"OLD_C")
    (bk / "mapping.yaml").write_text(
        "name: RollGame\ndrives:\n  drive-C: \"C:\"\nbackups:\n  - name: \".\"\n    files:\n"
        f"      \"C:/Users/me/Documents/a.sav\":\n        hash: {_sha1(b'LUD_A')}\n        size: 5\n"
        f"      \"C:/Users/me/Documents/c.sav\":\n        hash: {_sha1(b'LUD_C')}\n        size: 5\n"
        "    children: []\n",
        encoding="utf-8",
    )
    real_replace = save_sync.os.replace

    def fail_on_second_target(src: object, dst: object) -> None:
        dst_path = Path(os.fspath(dst))  # type: ignore[arg-type]
        if dst_path.name == "c.sav" and ".bak.tmp" not in str(src):
            raise OSError("simulated ENOSPC")
        real_replace(src, dst)  # type: ignore[arg-type]

    with mock.patch.object(save_sync.os, "replace", side_effect=fail_on_second_target):
        with pytest.raises(OSError) as lud_err:
            lds.import_ludusavi(bk, pfx, "RollGame", home=tmp_path / "home", dest_root=snaps)
    assert target_a.read_bytes() == b"OLD_A"
    assert (pfx / "drive_c" / "users" / "steamuser" / "Documents" / "c.sav").read_bytes() == b"OLD_C"
    assert "safety snapshot:" in str(lud_err.value)


def test_a35_rotation_preserves_safety_and_import_zips_and_pull_uses_keep(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps" / "game"
    snap_dir.mkdir(parents=True)
    pre_res = snap_dir / "20260101-000001-pre-restore.zip"
    pre_imp = snap_dir / "20260101-000002-pre-import.zip"
    lud_imp = snap_dir / "20260101-000003-ludusavi-import.zip"
    n1 = snap_dir / "20260101-000004-manual.zip"
    n2 = snap_dir / "20260101-000005-manual.zip"
    for idx, p in enumerate((pre_res, pre_imp, lud_imp, n1, n2)):
        p.write_bytes(b"z")
        os.utime(p, (1000 + idx, 1000 + idx))

    save_sync._rotate_snapshots(snap_dir, max_keep=1)
    assert pre_res.is_file()
    assert pre_imp.is_file()
    assert lud_imp.is_file()
    assert not n1.exists()
    assert n2.is_file()

    # Verify pull_game_saves passes max_keep through to snapshot_prefix_saves.
    sync_root = tmp_path / "sync"
    src_pfx = tmp_path / "src_pfx"
    (src_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
    (src_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.sav").write_bytes(b"remote")
    save_sync.push_game_saves("KeepGame", src_pfx, sync_root)
    dst_pfx = tmp_path / "dst_pfx"
    (dst_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
    (dst_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.sav").write_bytes(b"local")
    with mock.patch.object(save_sync, "snapshot_prefix_saves", wraps=save_sync.snapshot_prefix_saves) as spy:
        save_sync.pull_game_saves("KeepGame", dst_pfx, sync_root, snapshots_root=tmp_path / "s", max_keep=7)
    assert spy.call_args.kwargs["max_keep"] == 7


def test_a36_concurrent_snapshot_and_index_updates_create_distinct_zips(tmp_path: Path) -> None:
    pfx = tmp_path / "pfx"
    save = pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.sav"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"base")
    snaps = tmp_path / "snaps"

    def worker(idx: int) -> Path | None:
        return save_sync.snapshot_prefix_saves(
            pfx,
            "RaceGame",
            "manual",
            dest_root=snaps,
            max_keep=10,
            steam_appid=42,
            extra_dirs=(("steamcloud", tmp_path / "empty_cloud"),),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    valid = [r for r in results if r is not None]
    assert len(valid) == 4
    assert len({r.name for r in valid}) == 4
    snap_dir = save_sync.snapshot_dir_for("RaceGame", snaps)
    idx_data = json.loads((snap_dir / ".index.json").read_text(encoding="utf-8"))
    assert len(idx_data.get("steamcloud_appids", {})) == 4


def test_a37_restore_job_invalidates_process_cache_and_checks_running_wine_game(tmp_path: Path) -> None:
    snap = tmp_path / "snap.zip"
    with zipfile.ZipFile(snap, "w") as zf:
        zf.writestr("snap/steamcloud/slot.sav", b"cloud")

    steam_paths._process_cache["steam"] = (steam_paths.time.monotonic(), False)
    cloud_dir = tmp_path / "cloud"
    cloud_dir.mkdir()
    target = ssd.SaveTarget("G", tmp_path / "pfx", None, 42, None, "steam", (("steamcloud", cloud_dir),))
    entry = save_sync.SaveArchiveEntry(snap, "save_snapshot", "", "g", 0.0, 0)

    seen_cache_cleared = False

    def fake_steam_running() -> bool:
        nonlocal seen_cache_cleared
        seen_cache_cleared = "steam" not in steam_paths._process_cache
        return False

    with (
        mock.patch.object(ssd, "is_steam_running", side_effect=fake_steam_running),
        mock.patch.object(ssd, "_is_wine_game_running", return_value=True),
        pytest.raises(RuntimeError, match="running"),
    ):
        ssd._restore_job(target, entry)
    assert seen_cache_cleared


def test_a38_non_proton_user_ci_file_var_home_and_windows_steamcloud(tmp_path: Path) -> None:
    # 1. Non-Proton prefix (users/murilo) + case-preserving overwrite on Save.dat
    pfx = tmp_path / "lutris_pfx"
    target_save = pfx / "drive_c" / "users" / "murilo" / "Documents" / "MyGame" / "Save.dat"
    target_save.parent.mkdir(parents=True)
    target_save.write_bytes(b"old")

    snap = tmp_path / "s.zip"
    with zipfile.ZipFile(snap, "w") as zf:
        zf.writestr("snap/users/steamuser/Documents/MyGame/save.dat", b"from-snap")

    save_sync._restore_saves_archive(snap, pfx, None)
    assert target_save.read_bytes() == b"from-snap"
    assert not (pfx / "drive_c" / "users" / "steamuser").exists()

    # 2. _ci_path case-insensitivity only matches files when match_file=True
    home = tmp_path / "home"
    (home / ".config" / "G").mkdir(parents=True)
    (home / ".config" / "G" / "Config.ini").write_bytes(b"upper")
    assert lds._ci_path(home, (".config", "G", "config.ini"), match_file=False) == (".config", "G", "config.ini")
    assert lds._ci_path(
        pfx / "drive_c", ("users", "murilo", "Documents", "MyGame", "save.dat"), match_file=True
    )[-1] == "Save.dat"

    # 3. _target_for supports cross-user /home/<u1> and /var/home/<u2>
    local_home = Path("/var/home/murilo")
    assert lds._target_for("", ("home", "deck", ".local", "share", "G", "s.sav"), local_home) == (
        "home",
        (".local", "share", "G", "s.sav"),
    )
    assert lds._target_for("", ("var", "home", "bazzite", ".config", "G", "s.sav"), local_home) == (
        "home",
        (".config", "G", "s.sav"),
    )

    # 4. _snapshot_target captures Windows Steam userdata cloud files
    assert lds._snapshot_target(
        "C:", ("Program Files (x86)", "Steam", "userdata", "123", "311340", "remote", "slot1.dat")
    ) == ("steamcloud", ("slot1.dat",))


def test_a40_m32_fast_header_reader_yaml_backslash_and_dedup(tmp_path: Path) -> None:
    bk_root = tmp_path / "lud_root"
    game_bk = bk_root / "My_Game"
    (game_bk / "drive-C" / "Users" / "Alice" / "Documents").mkdir(parents=True)
    (game_bk / "drive-C" / "Users" / "Bob" / "Documents").mkdir(parents=True)
    d1 = b"alice-data"
    d2 = b"bob-data"
    (game_bk / "drive-C" / "Users" / "Alice" / "Documents" / "slot.sav").write_bytes(d1)
    (game_bk / "drive-C" / "Users" / "Bob" / "Documents" / "slot.sav").write_bytes(d2)
    (game_bk / "mapping.yaml").write_text(
        "---\n"
        'name: "My: Game"\n'
        "drives:\n"
        '  drive-C: "C:"\n'
        "backups:\n"
        '  - name: "."\n'
        "    files:\n"
        "      C:/Users/Alice/Documents/slot.sav:\n"
        f"        hash: {_sha1(d1)}\n"
        f"        size: {len(d1)}\n"
        "      C:/Users/Bob/Documents/slot.sav:\n"
        f"        hash: {_sha1(d2)}\n"
        f"        size: {len(d2)}\n"
        "    children: []\n",
        encoding="utf-8",
    )

    with mock.patch.object(lds, "parse_yaml", side_effect=AssertionError("must not full-parse YAML")):
        assert lds.read_mapping_name(game_bk) == "My: Game"
        assert lds.list_backup_games(bk_root) == [("My: Game", game_bk)]
        assert lds.find_game_folder(bk_root, "my game") == game_bk

    # Colliding normalized targets must not emit duplicate ZIP arcnames.
    snaps = tmp_path / "snaps"
    snap, stored, skipped, _ = lds.import_ludusavi_to_snapshot(game_bk, home=tmp_path / "h", dest_root=snaps)
    assert snap is not None and stored == 1 and skipped == 1
    with zipfile.ZipFile(snap) as zf:
        assert len(zf.namelist()) == len(set(zf.namelist())) == 1

    # Double-quoted wrapped explicit key ending in '\' must continue without extra space.
    yaml_text = (
        "name: G\n"
        "backups:\n"
        "  - name: .\n"
        "    files:\n"
        '      ? "C:/Users/deck/AppData/Roaming/My Game/Long\\\n'
        '        Name/slot.sav"\n'
        "      : size: 1\n"
    )
    parsed = lds.parse_yaml(yaml_text)
    assert "C:/Users/deck/AppData/Roaming/My Game/LongName/slot.sav" in parsed["backups"][0]["files"]


def test_m31_b07_sync_reconciliation_symlink_saves_size_and_slug_collision(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    src_pfx = tmp_path / "src_pfx"
    (src_pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
    (src_pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.sav").write_bytes(b"remote-save")
    save_sync.push_game_saves("CrossUser", src_pfx, sync_root)

    # Pull into a non-Proton Wine prefix (users/murilo) must immediately report IN_SYNC!
    dst_pfx = tmp_path / "dst_pfx"
    (dst_pfx / "drive_c" / "users" / "murilo" / "Documents").mkdir(parents=True)
    pulled, _ = save_sync.pull_game_saves("CrossUser", dst_pfx, sync_root, snapshots_root=tmp_path / "s")
    assert pulled == 1
    assert (dst_pfx / "drive_c" / "users" / "murilo" / "Documents" / "s.sav").read_bytes() == b"remote-save"
    plan = save_sync.plan_sync_all([("CrossUser", dst_pfx)], sync_root)
    assert plan[0].action == "IN_SYNC"

    # Symlinked latest.zip must be ignored by pull_game_saves & plan_sync_all.
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("save/users/murilo/Documents/s.sav", b"evil")
    sym_sync = tmp_path / "sym_sync"
    (sym_sync / "crossuser").mkdir(parents=True)
    (sym_sync / "crossuser" / "latest.zip").symlink_to(evil_zip)
    assert save_sync.pull_game_saves("CrossUser", dst_pfx, sym_sync) == (0, None)
    assert (dst_pfx / "drive_c" / "users" / "murilo" / "Documents" / "s.sav").read_bytes() == b"remote-save"

    # saves_size includes game_dir + home_files when passed metadata.
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "profile.dat").write_bytes(b"12345")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"SGame": {"steam": {"id": 99}, "files": {"<base>/profile.dat": {}}}}))
    home = tmp_path / "home"
    (home / ".config" / "SGame").mkdir(parents=True)
    (home / ".config" / "SGame" / "cfg.sav").write_bytes(b"123")
    with (
        mock.patch.object(save_sync, "DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest),
        mock.patch.object(save_sync, "_home_root", return_value=home),
    ):
        total, cnt = save_sources.saves_size(
            tmp_path / "empty_pfx",
            game_name="SGame",
            game_dir=game_dir,
            steam_appid=99,
            home_files=(".config/SGame/cfg.sav",),
        )
    assert (total, cnt) == (8, 2)

    # collect_save_targets keeps distinct games colliding on slug ("DOOM" 1993 vs 2016) with distinct prefixes.
    pfx1 = tmp_path / "pfx1"
    pfx2 = tmp_path / "pfx2"
    (pfx1 / "drive_c").mkdir(parents=True)
    (pfx2 / "drive_c").mkdir(parents=True)
    fake_win = SimpleNamespace(
        _standalone_games=[
            SimpleNamespace(
                name="DOOM",
                native_linux=False,
                prefix_dir=pfx1,
                game_dir=tmp_path / "g1",
                steam_appid=None,
                uid="g1",
            )
        ],
        paths=None,
        _steam_shortcuts=[],
        _external_games=[
            SimpleNamespace(name="DOOM", prefix_dir=pfx2, game_dir=tmp_path / "g2", runner="wine", source="heroic")
        ],
        _external_save_targets=[],
    )
    targets = ssd.collect_save_targets(fake_win)  # type: ignore[arg-type]
    assert len(targets) == 2
    assert {t.prefix_dir for t in targets} == {pfx1, pfx2}


def test_dialog_ludusavi_setting_dir_and_export_overwrite_confirmation(tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings

    root_bk = tmp_path / "Ludusavi"
    game_bk = root_bk / "MyGame"
    game_bk.mkdir(parents=True)
    (game_bk / lds.MAPPING_FILE).write_text("name: MyGame\nbackups: []\n", encoding="utf-8")

    # Picking either root or the game subfolder normalizes LUDUSAVI_DIR_KEY to backup root.
    assert ssd._ludusavi_import_setting_dir(game_bk) == str(root_bk)
    assert ssd._ludusavi_import_setting_dir(root_bk) == str(root_bk)

    from PySide6.QtWidgets import QApplication

    from deckinstall import ui

    app = QApplication.instance() or QApplication([])
    pfx = tmp_path / "pfx"
    (pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True)
    (pfx / "drive_c" / "users" / "steamuser" / "Documents" / "s.sav").write_bytes(b"s1")
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(tmp_path / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
        mock.patch.object(ssd, "list_save_archives", return_value=[]),
    ):
        win = ui.MainWindow(app)
        try:
            row = ssd.SaveRow(
                "mygame",
                "MyGame",
                ssd.SaveTarget("MyGame", pfx, None, None, None),
                0,
                0.0,
                0,
            )
            with (
                mock.patch.object(ssd, "_selected_row", return_value=row),
                mock.patch.object(ssd, "_pick_dir_async", side_effect=lambda _p, _t, _d, cb: cb(str(root_bk))),
                mock.patch.object(ssd.QMessageBox, "exec", return_value=ssd.QMessageBox.StandardButton.No) as box_exec,
                mock.patch.object(ssd, "_run") as run_mock,
            ):
                ssd._ludusavi_pick(win, export=True)
                assert box_exec.called
                run_mock.assert_not_called()
        finally:
            for w in list(win._active_workers):
                w.wait(5000)
            win.close()
            win.deleteLater()
            app.processEvents()

