"""Regression tests for FASE C fixes: VDF concurrency, exFAT/NTFS portability, /var/home paths, and storage safety."""

from __future__ import annotations

import errno
import json
import os
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import vdf

from deckinstall import (
    artwork,
    cleanup,
    fsutil,
    library,
    manifest,
    migrate,
    relocate,
    standalone,
    steam_paths,
    steam_vdf,
    uninstall,
    vdf_queue,
)
from deckinstall.steam_paths import SteamPaths


def _make_steam_paths(root: Path, user_id: str = "12345") -> SteamPaths:
    userdata = root / "userdata" / user_id
    cfg = root / "config"
    (userdata / "config" / "grid").mkdir(parents=True, exist_ok=True)
    cfg.mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    (cfg / "loginusers.vdf").write_text('"users" {}\n', encoding="utf-8")
    shortcuts = userdata / "config" / "shortcuts.vdf"
    if not shortcuts.exists():
        shortcuts.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    config_vdf = cfg / "config.vdf"
    if not config_vdf.exists():
        config_vdf.write_text('"InstallConfigStore"\n{\n}\n', encoding="utf-8")
    localconfig = userdata / "config" / "localconfig.vdf"
    if not localconfig.exists():
        localconfig.write_text('"UserLocalConfigStore"\n{\n}\n', encoding="utf-8")
    return SteamPaths(
        steam_root=root,
        user_id=user_id,
        userdata_dir=userdata,
        shortcuts_vdf=shortcuts,
        grid_dir=userdata / "config" / "grid",
        config_vdf=config_vdf,
        compatdata_dir=root / "steamapps" / "compatdata",
    )


def _install_v2_game(
    lib: library.Library,
    name: str,
    *,
    uid: str | None = None,
    with_prefix: bool = True,
    with_cache: bool = False,
) -> standalone.StandaloneGame:
    slug = fsutil.sanitize_dir_name(name)
    gdir = lib.games_dir / slug
    gdir.mkdir(parents=True, exist_ok=True)
    exe = gdir / f"{slug}.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 64)
    uid_val = uid or manifest.new_uid()
    pfx = (lib.prefixes_dir if lib.unix_fs else library.internal_library_root() / library.PREFIXES) / uid_val
    if with_prefix:
        (pfx / "drive_c" / "users" / "steamuser" / "Documents").mkdir(parents=True, exist_ok=True)
        (pfx / "pfx").symlink_to(".")
    cache: Path | None = None
    if with_cache:
        cache = (lib.caches_dir if lib.unix_fs else library.internal_library_root() / library.CACHES) / uid_val
        (cache / "shader.bin").parent.mkdir(parents=True, exist_ok=True)
        (cache / "shader.bin").write_bytes(b"C" * 32)
    game = standalone.StandaloneGame(
        name=name,
        game_dir=gdir,
        exe_path=exe,
        prefix_dir=pfx,
        proton_name="",
        steam_appid=None,
        winedlloverrides="",
        launch_options="",
        launch_script=gdir / "launch.sh",
        desktop_file=Path.home() / ".local" / "share" / "applications" / f"deckinstall-{slug}.desktop",
        desktop_shortcut=None,
        native_linux=False,
        cache_dir=cache,
        uid=uid_val,
        schema=2,
        meta={
            "created_at": manifest.now_iso(),
            "source": {"kind": "installed"},
            "proton_mode": "default",
            "prefix_location": "library" if lib.unix_fs else "internal",
        },
    )
    standalone.refresh_integration_files(game)
    mpath = standalone._write_manifest(game)
    standalone._register_standalone_game(game, mpath)
    library.record_game(lib.root, uid_val, name, gdir)
    return game


# --- [C-09] & [M-01]: VdfEditQueue concurrency, backups, launch_options, user validation ---


def test_vdf_queue_preserves_concurrent_enqueue_and_single_batch_backup(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path / "steam")
    exe = tmp_path / "game" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    appid = steam_vdf.add_or_update_shortcut(paths, "Alpha", exe, launch_options="KEEP=1")

    q = vdf_queue.VdfEditQueue(tmp_path / "queue.json")
    for idx in range(6):
        q.enqueue(
            vdf_queue.QueuedVdfEdit(
                f"set_proton:{idx}",
                "set_proton",
                appid + idx,
                f"Alpha {idx}",
                {"proton_name": f"GE-Proton9-{idx}"},
            )
        )
    q.enqueue(
        vdf_queue.QueuedVdfEdit(
            "keep_launch",
            "add_or_update_shortcut",
            appid,
            "Alpha",
            {"app_name": "Alpha", "exe_path": str(exe), "launch_options": None},
        )
    )

    concurrent_edit = vdf_queue.QueuedVdfEdit(
        "late_edit", "set_proton", appid, "Late", {"proton_name": "GE-Proton9-99"}
    )
    second_queue = vdf_queue.VdfEditQueue(tmp_path / "queue.json")
    injected = {"done": False}
    real_dispatch = q._dispatch_edit

    def _dispatch_with_concurrent_enqueue(p: SteamPaths, edit: vdf_queue.QueuedVdfEdit) -> None:
        if not injected["done"]:
            injected["done"] = True
            vdf_queue.VdfEditQueue(tmp_path / "queue.json").enqueue(concurrent_edit)
        real_dispatch(p, edit)

    backups: list[Path] = []
    real_backup = steam_vdf._backup_file

    def _spy_backup(path: Path, *args: object, **kwargs: object) -> Path | None:
        res = real_backup(path, *args, **kwargs)
        if res is not None:
            backups.append(path)
        return res

    with (
        mock.patch.object(q, "_dispatch_edit", side_effect=_dispatch_with_concurrent_enqueue),
        mock.patch.object(vdf_queue, "_backup_file", side_effect=_spy_backup),
    ):
        res = q.apply_all(paths)

    assert res.applied == 7 and not res.errors
    assert backups == [paths.shortcuts_vdf, paths.config_vdf]
    assert [e.edit_id for e in q.list_pending()] == ["late_edit"]
    entries = steam_vdf.load_shortcuts(paths.shortcuts_vdf, strict=True)
    assert entries[0].launch_options == "KEEP=1"
    replay = second_queue.apply_all(paths)
    assert replay.applied == 1 and not replay.errors
    assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == "GE-Proton9-99"


def test_vdf_queue_rejects_mismatched_steam_root_or_unknown_user(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path / "steam_a")
    other_root = tmp_path / "steam_b"
    other_root.mkdir()
    q = vdf_queue.VdfEditQueue(tmp_path / "queue.json")
    q.enqueue(
        vdf_queue.QueuedVdfEdit(
            "wrong_root",
            "set_proton",
            123,
            "Game",
            {"proton_name": "GE-Proton", "steam_root": str(other_root), "user_id": paths.user_id},
        )
    )
    q.enqueue(
        vdf_queue.QueuedVdfEdit(
            "missing_user",
            "set_launch_options",
            123,
            "Game",
            {"launch_options": "A=1", "steam_root": str(paths.steam_root), "user_id": "99999"},
        )
    )
    res = q.apply_all(paths)
    assert res.applied == 0 and len(res.errors) == 2
    assert q.pending_count() == 2


def test_ui_vdf_watch_tick_and_close_event_respect_active_workers_and_state() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication

    from deckinstall import ui, ui_workers_library

    app = QApplication.instance() or QApplication([])
    assert app is not None
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "list_standalone_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
    ):
        win = ui.MainWindow(app)
    try:
        for _ in range(200):
            app.processEvents()
            if not any(w.isRunning() for w in win._active_workers):
                break
            for w in list(win._active_workers):
                w.wait(20)
        app.processEvents()

        steam_root = Path("/tmp/fake-steam")
        win.paths = SimpleNamespace(user_id="42", steam_root=steam_root, shortcuts_vdf=Path("/nonexistent"))  # type: ignore[assignment]
        win.enqueue_vdf_edit(
            vdf_queue.QueuedVdfEdit("e1", "set_proton", 1, "Game", {"proton_name": "GE"}),
            trigger_watch=False,
        )
        saved_edit = win._vdf_queue.list_pending()[0]
        assert saved_edit.payload["user_id"] == "42"
        assert saved_edit.payload["steam_root"] == str(steam_root)

        # Busy worker active when _vdf_watch_tick finishes must NOT auto-apply.
        with (
            mock.patch.object(ui_workers_library, "is_steam_running", return_value=False),
            mock.patch.object(ui, "is_gamescope_session", return_value=False),
            mock.patch.object(win, "_auto_apply_vdf_queue") as auto_apply,
        ):
            win._vdf_watch_tick()
            win._begin_busy()  # busy increases while SteamRunningWorker is in flight
            for _ in range(200):
                app.processEvents()
                if not any(w.isRunning() for w in win._active_workers):
                    break
                for w in list(win._active_workers):
                    w.wait(20)
            app.processEvents()
            auto_apply.assert_not_called()
            win._end_busy()

        # Active worker blocks closeEvent BEFORE VDF prompt opens.
        fake_w = mock.Mock()
        fake_w.isRunning.return_value = True
        win._active_workers.add(fake_w)
        with mock.patch.object(ui, "QMessageBox") as box_cls:
            ev = QCloseEvent()
            win.closeEvent(ev)
            assert not ev.isAccepted()
            fake_w.cancel.assert_called_once()
            box_cls.assert_not_called()
        if win._close_force_timer is not None:
            win._close_force_timer.stop()
        win._closing_pending = False
        win._active_workers.discard(fake_w)
    finally:
        win._vdf_queue.clear()
        win._active_workers.clear()
        win.close()


# --- [C-10] & [M-04]: exFAT/NTFS portability, 0o555 rmtree, copy_or_move rollback ---


def test_fsutil_chmod_readonly_rmtree_portable_copytree_and_move_rollback(tmp_path: Path) -> None:
    # 1. atomic_write_bytes / atomic_copy_file tolerate EPERM on os.chmod
    dest = tmp_path / "data.txt"
    copied = tmp_path / "copy.txt"
    with mock.patch.object(fsutil.os, "chmod", side_effect=PermissionError(errno.EPERM, "eperm")):
        fsutil.atomic_write_bytes(dest, b"hello", mode=0o755)
        fsutil.atomic_copy_file(dest, copied)
    assert dest.read_bytes() == b"hello" and copied.read_bytes() == b"hello"

    # 2. remove_path_or_tree removes 0o555 read-only directories
    ro_root = tmp_path / "ro_tree"
    ro_sub = ro_root / "sub"
    ro_sub.mkdir(parents=True)
    ro_file = ro_sub / "lock.txt"
    ro_file.write_text("x", encoding="utf-8")
    os.chmod(ro_file, 0o444)
    os.chmod(ro_sub, 0o555)
    os.chmod(ro_root, 0o555)
    fsutil.remove_path_or_tree(ro_root)
    assert not ro_root.exists()

    # 3. move_dir_safely falls back to _copytree_portable across EXDEV when symlinks raise EPERM
    src_tree = tmp_path / "src_tree"
    src_tree.mkdir()
    (src_tree / "real.txt").write_text("content", encoding="utf-8")
    (src_tree / "in_link.txt").symlink_to("real.txt")
    (src_tree / "broken.txt").symlink_to("missing.txt")
    (src_tree / "escape.txt").symlink_to(tmp_path)
    dst_tree = tmp_path / "dst_tree"

    real_symlink = fsutil.os.symlink

    def _no_symlink(target: object, link_path: object, *a: object, **kw: object) -> None:
        raise PermissionError(errno.EPERM, "exfat no symlinks")

    with (
        mock.patch.object(fsutil.os, "rename", side_effect=OSError(errno.EXDEV, "exdev")),
        mock.patch.object(fsutil.shutil, "copytree", side_effect=PermissionError(errno.EPERM, "eperm")),
        mock.patch.object(fsutil.os, "symlink", side_effect=_no_symlink),
    ):
        fsutil.move_dir_safely(src_tree, dst_tree)
    assert callable(real_symlink)
    assert (dst_tree / "in_link.txt").read_text(encoding="utf-8") == "content"
    assert not (dst_tree / "broken.txt").exists() and not (dst_tree / "escape.txt").exists()
    assert not src_tree.exists()

    # 4. copy_or_move_dir_contents(move=True) restores target if move_dir_safely fails mid-flight
    src_merge = tmp_path / "merge_src"
    dst_merge = tmp_path / "merge_dst"
    (src_merge / "dir_a").mkdir(parents=True)
    (src_merge / "dir_a" / "f.bin").write_bytes(b"123")
    dst_merge.mkdir(parents=True)
    (dst_merge / "dir_a").write_bytes(b"existing")

    with mock.patch.object(fsutil, "move_dir_safely", side_effect=OSError(errno.ENOSPC, "no space")):
        with pytest.raises(OSError):
            fsutil.copy_or_move_dir_contents(src_merge, dst_merge, move=True)
    assert (dst_merge / "dir_a").read_bytes() == b"existing"
    assert not list(dst_merge.glob("*.movebak"))


def test_relocate_ext4_to_exfat_moves_prefix_and_cache_to_internal_and_checks_both_devs(
    tmp_path: Path,
) -> None:
    internal = library.ensure_library(library.internal_library_root(), internal=True)
    src_root = tmp_path / "ext4_sd" / library.LIBRARY_DIRNAME
    src_lib = library.ensure_library(src_root, label="Ext4 SD")
    dst_root = tmp_path / "exfat_sd" / library.LIBRARY_DIRNAME
    (dst_root / library.GAMES).mkdir(parents=True)
    exfat_lib = library.Library("exfat-id", dst_root, "exFAT SD", False, True, "exfat")

    game = _install_v2_game(src_lib, "Removable Game", with_prefix=True, with_cache=True)
    plan = relocate.plan_relocation(game, exfat_lib)
    assert plan.can_run
    assert relocate.W_PREFIX_STAYS in plan.warnings
    assert plan.new_prefix_dir == internal.prefixes_dir / game.uid
    assert plan.new_cache_dir == internal.caches_dir / game.uid

    moved = relocate.relocate_game(plan)
    assert moved.game_dir.parent == exfat_lib.games_dir
    assert moved.prefix_dir == internal.prefixes_dir / game.uid
    assert moved.cache_dir == internal.caches_dir / game.uid
    assert moved.meta.get("prefix_location") == "internal"
    assert (moved.prefix_dir / "drive_c").is_dir()

    # Cross-device space shortage on internal root for prefix blocks relocation even if exfat has space.
    game2 = _install_v2_game(src_lib, "Big Prefix Game", with_prefix=True, with_cache=False)
    (game2.prefix_dir / "drive_c" / "big.bin").write_bytes(b"P" * 4096)

    def _fake_dev(p: Path) -> int | None:
        s = str(p)
        if str(src_lib.root) in s:
            return 10
        if str(exfat_lib.root) in s:
            return 20
        return 30

    def _fake_usage(p: Path) -> object:
        return SimpleNamespace(free=100 if str(internal.root) in str(p) else 10**12)

    with (
        mock.patch.object(relocate, "_dev", side_effect=_fake_dev),
        mock.patch.object(relocate.shutil, "disk_usage", side_effect=_fake_usage),
    ):
        plan_low = relocate.plan_relocation(game2, exfat_lib)
    assert relocate.B_NO_SPACE in plan_low.blockers


# --- [A-16], [A-19], [A-20], [A-22], [A-23]: steam_vdf locks, CI keys, symlinks, microSD, restore ---


def test_canonical_lock_anchor_and_non_blocking_distinct_locks(tmp_path: Path) -> None:
    paths_a = _make_steam_paths(tmp_path / "steam_a")
    assert steam_vdf._canonical_steam_lock_anchor(paths_a.shortcuts_vdf) == paths_a.steam_root.resolve()
    assert steam_vdf._canonical_steam_lock_anchor(paths_a.config_vdf) == paths_a.steam_root.resolve()
    assert steam_vdf._canonical_steam_lock_anchor(paths_a.localconfig_vdf) == paths_a.steam_root.resolve()

    lock_1 = tmp_path / "locks" / "one.lock"
    lock_2 = tmp_path / "locks" / "two.lock"
    entered_1 = threading.Event()
    entered_2 = threading.Event()
    release_1 = threading.Event()

    def _hold_first() -> None:
        with steam_vdf._file_lock(lock_1):
            entered_1.set()
            release_1.wait(timeout=3.0)

    t1 = threading.Thread(target=_hold_first)
    t1.start()
    try:
        assert entered_1.wait(timeout=2.0)
        with steam_vdf._file_lock(lock_2):
            entered_2.set()
        assert entered_2.is_set()
    finally:
        release_1.set()
        t1.join(timeout=2.0)


def test_case_insensitive_compat_and_shortcuts_vdf_handling(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path / "steam")
    exe = tmp_path / "game" / "game.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    appid, s32 = steam_vdf.compute_shortcut_appid(f'"{exe}"', "LowerCase")
    paths.shortcuts_vdf.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": s32,
                        "appname": "LowerCase",
                        "exe": f'"{exe}"',
                        "startdir": f'"{exe.parent}"',
                        "launchoptions": "FOO=1 %command%",
                        "lastplaytime": 777,
                    }
                }
            }
        )
    )
    paths.config_vdf.write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
        f'\t\t\t{{\n\t\t\t\t"compattoolmapping"\n\t\t\t\t{{\n\t\t\t\t\t"{appid}"\n'
        '\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"GE-Proton9-1"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )

    loaded = steam_vdf.load_shortcuts(paths.shortcuts_vdf, strict=True)
    assert len(loaded) == 1
    assert loaded[0].launch_options == "FOO=1 %command%"
    assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == "GE-Proton9-1"

    assert steam_vdf.update_shortcut_launch_options(paths, appid, "BAR=2 %command%")
    raw_after = vdf.binary_loads(paths.shortcuts_vdf.read_bytes())["shortcuts"]["0"]
    assert raw_after["LaunchOptions"] == "BAR=2 %command%"
    assert "launchoptions" not in raw_after
    assert raw_after["lastplaytime"] == 777


def test_add_or_update_shortcut_migrates_symlinked_compatdata(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path / "steam", user_id="1001")
    old_exe = tmp_path / "old" / "game.exe"
    new_exe = tmp_path / "new" / "game.exe"
    old_exe.parent.mkdir(parents=True)
    new_exe.parent.mkdir(parents=True)
    old_exe.write_bytes(b"MZ")
    new_exe.write_bytes(b"MZ")

    old_appid = steam_vdf.add_or_update_shortcut(paths, "Relinked", old_exe)
    target_pfx = tmp_path / "real_prefix"
    (target_pfx / "drive_c").mkdir(parents=True)
    old_compat = paths.compatdata_dir / str(old_appid)
    old_compat.symlink_to(target_pfx)

    old_exe.unlink()  # A moved game, distinct from a second installed game with the same title.
    new_appid = steam_vdf.add_or_update_shortcut(paths, "Relinked", new_exe)
    new_compat = paths.compatdata_dir / str(new_appid)
    assert new_appid != old_appid
    assert new_compat.is_symlink() and new_compat.resolve() == target_pfx.resolve()
    assert not old_compat.exists() and not old_compat.is_symlink()


def test_microsd_compatdata_rejects_non_unix_fs_and_keeps_existing_dst(tmp_path: Path) -> None:
    paths = _make_steam_paths(tmp_path / "steam")
    ext_mnt = tmp_path / "mnt" / "sd_ext4"
    exfat_mnt = tmp_path / "mnt" / "sd_exfat"
    ext_mnt.mkdir(parents=True)
    exfat_mnt.mkdir(parents=True)

    with (
        mock.patch.object(steam_vdf, "read_library_paths", return_value=[]),
        mock.patch.object(steam_vdf, "fs_type_of", side_effect=lambda p: "exfat" if "exfat" in str(p) else "ext4"),
        mock.patch.object(steam_vdf, "_is_path_on_mounted_volume", return_value=True),
    ):
        targets = steam_vdf.list_microsd_compatdata_targets(media_root=ext_mnt.parent, steam_root=paths.steam_root)
    assert len(targets) == 1 and "sd_ext4" in str(targets[0])

    appid = 998877
    src_compat = paths.compatdata_dir / str(appid)
    (src_compat / "drive_c").mkdir(parents=True)

    with mock.patch.object(steam_vdf, "fs_type_of", return_value="exfat"):
        with pytest.raises(ValueError, match="exfat"):
            steam_vdf.migrate_compatdata_to_microsd(
                paths, appid, exfat_mnt / "DeckInstall" / "compatdata", verify_mounted=False
            )

    existing_dst = ext_mnt / "DeckInstall" / "compatdata" / str(appid)
    (existing_dst / "keep").mkdir(parents=True)
    (existing_dst / "keep" / "important.sav").write_text("keep", encoding="utf-8")
    with mock.patch.object(steam_vdf, "fs_type_of", return_value="ext4"):
        with pytest.raises(FileExistsError):
            steam_vdf.migrate_compatdata_to_microsd(
                paths, appid, ext_mnt / "DeckInstall" / "compatdata", verify_mounted=False
            )
    assert (existing_dst / "keep" / "important.sav").read_text(encoding="utf-8") == "keep"


def test_restore_saves_backup_cross_mode_steam_standalone_fallback(tmp_path: Path) -> None:
    # 1. Backup from Steam key -> restore onto Standalone on a PC without Steam
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    game = _install_v2_game(lib, "Hollow Knight", with_prefix=True)
    zip_steam = tmp_path / "from_steam.zip"
    with zipfile.ZipFile(zip_steam, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"314159": "Hollow Knight"}))
        zf.writestr("314159/users/steamuser/Documents/save.dat", b"steam-save")

    games_cnt, files_cnt = steam_vdf.restore_saves_backup(None, zip_steam)
    assert (games_cnt, files_cnt) == (1, 1)
    assert (game.prefix_dir / "drive_c" / "users" / "steamuser" / "Documents" / "save.dat").read_bytes() == b"steam-save"

    # 2. Backup from Standalone key -> restore onto Steam shortcut on SteamOS
    paths = _make_steam_paths(tmp_path / "steam")
    exe = tmp_path / "g" / "celeste.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    appid = steam_vdf.add_or_update_shortcut(paths, "Celeste", exe)
    zip_sa = tmp_path / "from_standalone.zip"
    with zipfile.ZipFile(zip_sa, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"standalone-celeste": "Celeste"}))
        zf.writestr("standalone-celeste/users/steamuser/Documents/slot1.bin", b"sa-save")

    g2, f2 = steam_vdf.restore_saves_backup(paths, zip_sa)
    assert (g2, f2) == (1, 1)
    restored = paths.compatdata_dir / str(appid) / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "slot1.bin"
    assert restored.read_bytes() == b"sa-save"


# --- [A-21], [A-24], [M-02], [M-03], [M-05], [M-06], [B-08] ---


def test_var_home_and_var_mnt_symlink_compatibility(tmp_path: Path) -> None:
    # library.list_mount_points deduplicates /mnt -> /var/mnt symlink roots
    var_mnt = tmp_path / "var" / "mnt"
    sd = var_mnt / "sdcard"
    sd.mkdir(parents=True)
    mnt_link = tmp_path / "mnt"
    mnt_link.symlink_to(var_mnt)
    with mock.patch.object(library, "_is_mount", side_effect=lambda p: p.name == "sdcard"):
        mounts = library.list_mount_points([mnt_link, var_mnt])
    assert len(mounts) == 1 and mounts[0].resolve() == sd.resolve()

    # library.library_for_path sets internal=True when HOME is a symlink (/home -> /var/home)
    var_home = tmp_path / "var" / "home" / "deck"
    var_home.mkdir(parents=True)
    home_link = tmp_path / "home_link"
    home_link.symlink_to(var_home)
    with (
        mock.patch.dict(os.environ, {"HOME": str(home_link)}),
        mock.patch.object(library, "INTERNAL_ROOT", home_link / library.LIBRARY_DIRNAME),
    ):
        internal = library.ensure_library(var_home / library.LIBRARY_DIRNAME, internal=True)
        gdir = internal.games_dir / "Sample"
        gdir.mkdir(parents=True)
        detected = library.library_for_path(gdir)
        assert detected is not None and detected.internal is True

    # migrate._referenced & rebase & launch.sh 0o755 rollback & v2 shared prefix guard
    old_gdir = home_link / "Games" / "OldGame"
    old_gdir.mkdir(parents=True)
    real_gdir = var_home / "Games" / "OldGame"
    assert migrate._referenced(real_gdir, [os.path.abspath(old_gdir / "game.exe")])

    lsh = tmp_path / "launch.sh"
    lsh.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(lsh, 0o755)
    probs = migrate._undo_entry(
        {"files": {str(lsh): "#!/bin/sh\necho orig\n"}, "moves": [], "created": [], "library_root": ""},
    )
    assert probs == []
    assert (lsh.stat().st_mode & 0o777) == 0o755

    # v2 game sharing prefix with a v1 game stops prefix move in plan_migration
    shared_pfx = tmp_path / "shared_pfx"
    (shared_pfx / "drive_c").mkdir(parents=True)
    v1_game = SimpleNamespace(
        name="V1", game_dir=tmp_path / "g1", exe_path=tmp_path / "g1" / "g1.exe",
        prefix_dir=shared_pfx, launch_script=tmp_path / "g1" / "launch.sh",
        desktop_file=tmp_path / "d1.desktop", desktop_shortcut=None, native_linux=False,
        cache_dir=None, schema=1,
    )
    v2_game = SimpleNamespace(prefix_dir=shared_pfx, schema=2)
    with (
        mock.patch.object(migrate.standalone, "uses_library_layout", return_value=True),
        mock.patch.object(migrate.standalone, "list_standalone_games", return_value=[v1_game, v2_game]),
        mock.patch.object(migrate, "_shortcut_paths", return_value=[]),
        mock.patch.object(migrate.standalone, "get_legacy_games_dir", return_value=tmp_path / "g"),
        mock.patch.object(migrate.standalone, "get_legacy_prefixes_dir", return_value=tmp_path),
    ):
        plan = migrate.plan_migration(None)
    assert len(plan) == 1 and not any(src == shared_pfx for src, _dst in plan[0].moves)


def test_uninstall_snapshot_icon_errors_covered_and_steam_restore_status(tmp_path: Path) -> None:
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    game = _install_v2_game(lib, "Shot Game", with_prefix=True)
    game.meta["steam"] = {"shortcut_appid": 424242}
    icon_dir = game.game_dir / "bad_icon_dir"
    icon_dir.mkdir()
    game.meta["icons"] = [icon_dir]
    standalone._write_manifest(game)

    with mock.patch.object(uninstall.save_sync, "auto_snapshot", return_value=tmp_path / "snap.zip") as snap_mock:
        plan = uninstall.plan_uninstall(game)
        res = uninstall.execute_uninstall(plan, {uninstall.ICONS, uninstall.SAVES, uninstall.PREFIX})
    snap_mock.assert_called_once()
    assert snap_mock.call_args.kwargs["game_dir"] == game.game_dir
    assert snap_mock.call_args.kwargs["steam_appid"] == 424242
    assert uninstall.ICONS in res.skipped and any(uninstall.ICONS in e for e in res.errors)

    # STEAM restore failure marks STEAM failed, not removed
    game2 = _install_v2_game(lib, "SteamRestoreFail", with_prefix=True)
    steam_dir = tmp_path / "steam" / "steamapps" / "compatdata"
    steam_dir.mkdir(parents=True)
    link = steam_dir / "55555"
    backup = steam_dir / "55555.bak"
    backup.mkdir()
    link.symlink_to(game2.prefix_dir)
    game2.meta["steam"] = {"compatdata_link": str(link), "compatdata_backup": str(backup), "shortcut_appid": 55555}
    standalone._write_manifest(game2)

    plan2 = uninstall.plan_uninstall(game2)
    replace = os.replace

    def fail_restore(src, dst):
        if src == backup and dst == link:
            raise OSError("busy")
        return replace(src, dst)

    with mock.patch.object(uninstall.os, "replace", side_effect=fail_restore):
        res2 = uninstall.execute_uninstall(plan2, {uninstall.SHORTCUTS, uninstall.STEAM})
    assert uninstall.STEAM in res2.skipped and uninstall.STEAM not in res2.removed
    assert backup.is_dir()


def test_cleanup_protects_empty_bottles_unmounted_heroic_lutris_and_trash(tmp_path: Path) -> None:
    home = Path.home()
    # 1. Empty Bottles prefix (bottle.yml present, no External_Programs) is protected
    empty_bottle = home / ".local" / "share" / "bottles" / "bottles" / "EmptyBottle"
    (empty_bottle / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (empty_bottle / "bottle.yml").write_text("Name: EmptyBottle\nRunner: soda\n", encoding="utf-8")

    # 2. Heroic Prefixes/default containing active Prefixes/default/MyHeroicGame on unmounted disk
    heroic_cfg = home / ".config" / "heroic"
    (heroic_cfg / "GamesConfig").mkdir(parents=True)
    h_default = home / "Games" / "Heroic" / "Prefixes" / "default"
    h_game_pfx = h_default / "MyHeroicGame"
    (h_game_pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (heroic_cfg / "GamesConfig" / "app1.json").write_text(
        json.dumps({"app1": {"winePrefix": str(h_game_pfx)}}), encoding="utf-8"
    )

    # 3. Lutris YAML configured prefix whose game exe is on an unmounted disk
    lutris_games = home / ".config" / "lutris" / "games"
    lutris_games.mkdir(parents=True)
    lutris_pfx = home / "Games" / "Lutris" / "unmounted-rpg"
    (lutris_pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (lutris_games / "unmounted-rpg-1700000000.yml").write_text(
        f"game:\n  exe: /media/unmounted/RPG/game.exe\n  prefix: {lutris_pfx}\n",
        encoding="utf-8",
    )

    # 4. The orphan prefix is found. Trash and its metadata are out of scope.
    orphan_pfx = home / "Games" / "Heroic" / "Prefixes" / "OrphanGame"
    (orphan_pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)

    trash_files = home / ".local" / "share" / "Trash" / "files"
    trash_info = home / ".local" / "share" / "Trash" / "info"
    trash_files.mkdir(parents=True)
    trash_info.mkdir(parents=True)
    (trash_files / "junk.dat").write_bytes(b"Z" * 16)
    (trash_info / "junk.dat.trashinfo").write_text("[Trash Info]\nPath=/tmp/junk.dat\n", encoding="utf-8")

    cands = cleanup.find_cleanup_candidates(
        extra_external_roots=[home / "Games" / "Heroic" / "Prefixes", home / "Games" / "Lutris"],
        include_system_caches=True,
    )
    cand_paths = {c.path.resolve() for c in cands}
    assert empty_bottle.resolve() not in cand_paths
    assert h_game_pfx.resolve() not in cand_paths
    assert h_default.resolve() not in cand_paths
    assert lutris_pfx.resolve() not in cand_paths
    assert orphan_pfx.resolve() in cand_paths

    assert trash_files not in cand_paths
    assert (trash_files / "junk.dat").is_file()
    assert (trash_info / "junk.dat.trashinfo").is_file()


def test_medium_safety_guards_manifest_artwork_steam_root_and_empty_library_root(tmp_path: Path) -> None:
    # [M-03] manifest.library_root_for refuses traversing to "/"
    assert manifest.library_root_for(Path("/a/b"), "Games/One/Two/Three") is None

    # [M-05] artwork.download_steam_artwork rejects symlinked grid_dir
    real_grid = tmp_path / "real_grid"
    real_grid.mkdir()
    link_grid = tmp_path / "link_grid"
    link_grid.symlink_to(real_grid)
    with pytest.raises(OSError, match="symlink"):
        artwork.download_steam_artwork(620, 123456, link_grid)

    # [M-06] steam_paths.find_steam_root skips empty native userdata and finds active Flatpak Steam
    empty_native = Path.home() / ".local" / "share" / "Steam" / "userdata"
    empty_native.mkdir(parents=True)
    flatpak_steam = (
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    )
    (flatpak_steam / "userdata" / "55555" / "config").mkdir(parents=True)
    assert steam_paths.find_steam_root() == flatpak_steam

    # [B-08] library.discover_libraries skips registry entries with root=""
    library.ensure_library(library.internal_library_root(), internal=True)
    reg = json.loads(library.registry_path().read_text(encoding="utf-8"))
    reg["libraries"]["corrupt-empty"] = {"root": "", "label": "Broken", "internal": False, "games": []}
    library.registry_path().write_text(json.dumps(reg), encoding="utf-8")
    libs = library.discover_libraries()
    assert all(lib.id != "corrupt-empty" and str(lib.root) for lib in libs)
