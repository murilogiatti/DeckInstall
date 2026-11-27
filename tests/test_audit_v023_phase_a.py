"""Phase A regression tests for post-v0.22.0 audit fixes."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import vdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from deckinstall import (  # noqa: E402
    adopt,
    cleanup,
    library,
    play_monitor,
    proton_policy,
    save_sync,
    standalone,
    steam_vdf,
    ui,
)
from deckinstall.manifest import MANIFEST_V2  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.ui_dialogs import proton_bulk_dialog as pbd  # noqa: E402
from deckinstall.ui_dialogs import save_sync_dialog  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(h / ".cache"))
    monkeypatch.setenv("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "")
    return h


def _steam_paths(root: Path, uid: str = "42", extra_uids: tuple[str, ...] = ()) -> SteamPaths:
    for u in (uid, *extra_uids):
        (root / "userdata" / u / "config" / "grid").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    ud = root / "userdata" / uid
    return SteamPaths(
        steam_root=root,
        user_id=uid,
        userdata_dir=ud,
        shortcuts_vdf=ud / "config" / "shortcuts.vdf",
        grid_dir=ud / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )


def _write_pe(exe: Path) -> Path:
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(PE)
    return exe


def test_infer_game_root_and_protected_dirs_cover_mnt_media_and_deckinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)

    # Synthetic /mnt and /media paths never collapse to the mountpoint or disk root.
    assert steam_vdf.infer_game_root_from_exe(Path("/mnt/ssd/Games/MyGame/game.exe")) == Path(
        "/mnt/ssd/Games/MyGame"
    ).resolve()
    with pytest.raises(ValueError):
        steam_vdf.infer_game_root_from_exe(Path("/mnt/ssd/game.exe"))

    assert steam_vdf.infer_game_root_from_exe(Path("/media/user/Disk/Games/MyGame/game.exe")) == Path(
        "/media/user/Disk/Games/MyGame"
    ).resolve()
    with pytest.raises(ValueError):
        steam_vdf.infer_game_root_from_exe(Path("/media/user/Disk/game.exe"))

    # ~/DeckInstall internal library root is protected.
    di_game_exe = _write_pe(home / "DeckInstall" / "Games" / "MyGame" / "Binaries" / "Win64" / "Game.exe")
    assert steam_vdf.infer_game_root_from_exe(di_game_exe) == (home / "DeckInstall" / "Games" / "MyGame").resolve()
    with pytest.raises(ValueError):
        steam_vdf.infer_game_root_from_exe(_write_pe(home / "DeckInstall" / "Loose.exe"))
    with pytest.raises(ValueError):
        steam_vdf.infer_game_root_from_exe(_write_pe(home / "DeckInstall" / "Games" / "Loose.exe"))

    # A directory containing library.json (on any disk) is also a protected library root.
    ext_lib = tmp_path / "ext" / "DeckInstall"
    ext_lib.mkdir(parents=True)
    (ext_lib / "library.json").write_text('{"version": 1}\n', encoding="utf-8")
    ext_exe = _write_pe(ext_lib / "Games" / "Alpha" / "alpha.exe")
    assert steam_vdf.infer_game_root_from_exe(ext_exe) == (ext_lib / "Games" / "Alpha").resolve()

    assert standalone._is_protected_dir(home / "DeckInstall")
    assert standalone._is_protected_dir(home / "DeckInstall" / "Games")
    assert standalone._is_protected_dir(ext_lib)
    assert standalone._is_protected_dir(Path("/mnt/ssd"))
    assert not standalone._is_protected_dir(ext_lib / "Games" / "Alpha")


def test_cleanup_protects_v2_library_proton_and_steamrt3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    game_dir = lib.games_dir / "Alpha"
    exe = _write_pe(game_dir / "alpha.exe")
    (lib.prefixes_dir / "uid-alpha" / "drive_c").mkdir(parents=True)

    compat_dir = home / ".local" / "share" / "Steam" / "compatibilitytools.d"
    ge10 = compat_dir / "GE-Proton10-2"
    ge11 = compat_dir / "GE-Proton11-5"
    for tool in (ge10, ge11):
        tool.mkdir(parents=True)
        (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
        (tool / "big.bin").write_bytes(b"x" * 16)

    umu_dir = home / ".local" / "share" / "umu"
    (umu_dir / "steamrt4").mkdir(parents=True)
    (umu_dir / "steamrt4" / "toolmanifest.vdf").write_text('"manifest"\n{}\n', encoding="utf-8")
    (umu_dir / "steamrt3").mkdir(parents=True)
    (umu_dir / "steamrt3" / "big.bin").write_bytes(b"x" * 16)

    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game(
            "Alpha",
            exe,
            game_dir=game_dir,
            proton_name="GE-Proton10-2",
            create_desktop_shortcut=False,
            create_menu_shortcut=False,
            library=lib,
            uid="uid-alpha",
        )
    assert (game_dir / MANIFEST_V2).is_file()

    candidates = cleanup.find_cleanup_candidates(None, include_system_caches=False)
    labels = {c.label for c in candidates}
    assert "compatibilitytools.d/GE-Proton10-2" not in labels
    assert "umu/steamrt3" not in labels


def test_cleanup_corrupt_shortcuts_skips_orphan_compatdata_and_shadercache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    paths = _steam_paths(tmp_path / "steam", uid="42", extra_uids=("99",))
    paths.shortcuts_vdf.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    (paths.steam_root / "userdata" / "99" / "config" / "shortcuts.vdf").write_bytes(b"\xff\xfe\x00bad")

    orphan_compat = paths.compatdata_dir / "999999"
    (orphan_compat / "pfx" / "drive_c").mkdir(parents=True)
    orphan_shader = paths.steam_root / "steamapps" / "shadercache" / "999999"
    orphan_shader.mkdir(parents=True)
    (orphan_shader / "cache.bin").write_bytes(b"1234")

    candidates = cleanup.find_cleanup_candidates(paths, include_system_caches=False)
    labels = {c.label for c in candidates}
    assert "compatdata/999999" not in labels
    assert "shadercache/999999" not in labels


def test_cleanup_preserves_desktop_files_for_unmounted_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    library.ensure_library(library.internal_library_root(), internal=True)
    unmounted_root = tmp_path / "missing_disk" / "DeckInstall"
    reg = library.registry_path()
    reg_data = json.loads(reg.read_text(encoding="utf-8"))
    reg_data["libraries"]["sd-unmounted"] = {
        "root": str(unmounted_root),
        "label": "SD",
        "internal": False,
        "games": [],
    }
    reg.write_text(json.dumps(reg_data), encoding="utf-8")

    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    sd_desktop = apps_dir / "deckinstall-sdgame.desktop"
    sd_desktop.write_text(
        f"[Desktop Entry]\nName=SDGame\nExec={unmounted_root}/Games/SDGame/launch.sh\nPath={unmounted_root}/Games/SDGame\n",
        encoding="utf-8",
    )
    dead_desktop = apps_dir / "deckinstall-dead.desktop"
    dead_desktop.write_text(
        "[Desktop Entry]\nName=Dead\nExec=/nonexistent/dead_dir/launch.sh\nPath=/nonexistent/dead_dir\n",
        encoding="utf-8",
    )

    candidates = cleanup.find_cleanup_candidates(None, include_system_caches=False)
    paths_found = {c.path for c in candidates}
    assert sd_desktop not in paths_found
    assert dead_desktop in paths_found


def test_adopt_move_prefix_false_and_symlink_compatdata_do_not_clobber_or_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    paths = _steam_paths(tmp_path / "steam")

    # (a) move_prefix=False preserves original compatdata/<appid> directory and avoids linking over it.
    src_a = home / "Games" / "KeepPrefixGame"
    exe_a = _write_pe(src_a / "game.exe")
    appid_a = 3111000001
    compat_a = paths.compatdata_dir / str(appid_a)
    (compat_a / "pfx" / "drive_c" / "users" / "steamuser" / "Saved Games").mkdir(parents=True)
    (compat_a / "pfx" / "drive_c" / "users" / "steamuser" / "Saved Games" / "save.dat").write_text(
        "keep-me", encoding="utf-8"
    )
    (compat_a / "pfx" / "drive_c" / "marker.txt").write_text("original-prefix", encoding="utf-8")
    (compat_a / "pfx" / "system.reg").write_text("WINE REGISTRY", encoding="utf-8")

    # (b) Existing compatdata symlink pointing elsewhere avoids step-6 link crash and emits warning.
    src_b = home / "Games" / "SymlinkCompatGame"
    exe_b = _write_pe(src_b / "game.exe")
    appid_b = 3111000002
    ext_compat = tmp_path / "sd_compat" / str(appid_b)
    (ext_compat / "pfx" / "drive_c").mkdir(parents=True)
    compat_b = paths.compatdata_dir / str(appid_b)
    compat_b.symlink_to(ext_compat)

    paths.shortcuts_vdf.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": appid_a - (1 << 32),
                        "AppName": "KeepPrefixGame",
                        "Exe": f'"{exe_a}"',
                        "StartDir": f'"{src_a}"',
                        "LaunchOptions": "",
                    },
                    "1": {
                        "appid": appid_b - (1 << 32),
                        "AppName": "SymlinkCompatGame",
                        "Exe": f'"{exe_b}"',
                        "StartDir": f'"{src_b}"',
                        "LaunchOptions": "",
                    },
                }
            }
        )
    )

    with mock.patch.object(adopt, "is_steam_running", return_value=False):
        cands = adopt.find_adoption_candidates(home=home, paths=paths, kinds=(adopt.STEAM,))
    by_name = {c.name: c for c in cands}

    plan_a = adopt.plan_adoption(
        by_name["KeepPrefixGame"],
        library.internal_library_root(),
        adopt.AdoptOptions(move_game=True, move_prefix=False, remove_from_steam=False, link_steam_compatdata=True),
    )
    assert plan_a.ok and plan_a.prefix_action == "copy_saves"

    plan_b = adopt.plan_adoption(
        by_name["SymlinkCompatGame"],
        library.internal_library_root(),
        adopt.AdoptOptions(move_game=True, move_prefix=True, remove_from_steam=False, link_steam_compatdata=True),
    )
    assert plan_b.ok and adopt.W_SYMLINK_PREFIX in plan_b.warnings

    with (
        mock.patch.object(adopt, "is_steam_running", return_value=False),
        mock.patch.object(adopt, "is_gamescope_session", return_value=False),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        res_a = adopt.adopt(plan_a, snapshot_fn=lambda *a: None, paths=paths)
        res_b = adopt.adopt(plan_b, snapshot_fn=lambda *a: None, paths=paths)

    assert compat_a.is_dir() and not compat_a.is_symlink()
    assert (compat_a / "pfx" / "drive_c" / "marker.txt").read_text(encoding="utf-8") == "original-prefix"
    assert res_a.steam_link is None
    assert compat_b.is_symlink() and compat_b.resolve() == ext_compat.resolve()
    assert res_b.steam_link is None


def test_kill_pids_never_sigkills_when_initial_epoch_unknown() -> None:
    app = QApplication.instance() or QApplication([])
    mon = play_monitor.PlayMonitor(app)
    kills: list[tuple[int, int]] = []
    try:
        with (
            mock.patch.object(play_monitor, "process_start_epoch", return_value=None),
            mock.patch.object(os, "kill", side_effect=lambda pid, sig: kills.append((pid, sig))),
        ):
            mon.kill_pids({987654}, grace_ms=10)
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()
    finally:
        mon.deleteLater()

    assert (987654, signal.SIGKILL) not in kills


def test_resolve_manifest_save_paths_with_symlinked_prefix_and_pfx_selflink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    real_pfx = tmp_path / "Prefixes" / "uid-game"
    save_dir = real_pfx / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "TestGame"
    save_dir.mkdir(parents=True)
    exact_file = save_dir / "save.dat"
    glob_file = save_dir / "slot1.sav"
    exact_file.write_text("exact", encoding="utf-8")
    glob_file.write_text("glob", encoding="utf-8")
    (real_pfx / "pfx").symlink_to(".")
    (real_pfx / "user.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    compat_link = tmp_path / "compatdata" / "12345"
    compat_link.parent.mkdir(parents=True)
    compat_link.symlink_to(real_pfx)

    entry: dict[str, object] = {
        "files": {
            "<winLocalAppData>/TestGame/save.dat": {"tags": ["save"], "when": [{"os": "windows"}]},
            "<winLocalAppData>/TestGame/*.sav": {"tags": ["save"], "when": [{"os": "windows"}]},
        },
        "registry": {"HKEY_CURRENT_USER/Software/TestGame": {"tags": ["save"]}},
    }

    for p in (compat_link, real_pfx / "pfx", real_pfx):
        files, reg_paths = save_sync._resolve_manifest_save_paths(p, entry)
        rels = {rel.as_posix() for _, _, rel in files}
        assert "steamuser/AppData/Local/TestGame/save.dat" in rels
        assert "steamuser/AppData/Local/TestGame/slot1.sav" in rels
        assert reg_paths == ["HKEY_CURRENT_USER/Software/TestGame"]

    with mock.patch.object(save_sync, "find_manifest_game_entry", return_value=entry):
        save_sync._extract_manifest_reg_bytes(compat_link, "TestGame", None)


def test_proton_bulk_options_and_cancel_steam_preserves_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    app = QApplication.instance() or QApplication([])
    paths = _steam_paths(tmp_path / "steam")

    official = paths.steam_root / "steamapps" / "common" / "Proton - Experimental"
    official.mkdir(parents=True)
    (official / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    ge = paths.steam_root / "compatibilitytools.d" / "GE-Proton11-5"
    ge.mkdir(parents=True)
    (ge / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (ge / "compatibilitytool.vdf").write_text(
        '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "GE-Proton11-5" { "install_path" "." }\n  }\n}\n',
        encoding="utf-8",
    )
    (ge / ".deckinstall-managed").write_text("GE-Proton11-5\n", encoding="utf-8")

    options, _, _ = pbd.collect_target_options(paths, "GE-Proton11-5")
    assert any(tid == proton_policy.DEFAULT_PROTON_ID for tid, _ in options)

    lib = library.ensure_library(library.internal_library_root(), internal=True)
    game_dir = lib.games_dir / "Alpha"
    exe = _write_pe(game_dir / "alpha.exe")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        standalone.install_standalone_game(
            "Alpha",
            exe,
            game_dir=game_dir,
            proton_name="",
            create_desktop_shortcut=False,
            create_menu_shortcut=False,
            library=lib,
            uid="uid-alpha",
        )
    appid = 3100000001
    paths.shortcuts_vdf.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": appid - (1 << 32),
                        "AppName": "Alpha",
                        "Exe": f'"{exe}"',
                        "StartDir": f'"{game_dir}"',
                        "LaunchOptions": "",
                    }
                }
            }
        )
    )
    steam_vdf.bulk_migrate_compat_tool(paths.config_vdf, [appid], "proton_9")
    manifest_before = (game_dir / MANIFEST_V2).read_text(encoding="utf-8")

    class _CancelSteamWin:
        def __init__(self) -> None:
            self.paths = paths
            self._steam_root_found = True
            self._closing_pending = False
            self.status_label = SimpleNamespace(setText=lambda _t: None)
            self._workers: list[object] = []

        def _track_worker(self, w: object) -> None:
            self._workers.append(w)

        def _refresh_shortcuts(self) -> None:
            pass

        def should_queue_vdf_edits(self) -> bool:
            return False

        def _is_auto_snapshot_enabled(self) -> bool:
            return False

        def _auto_snapshot_if_enabled(self, *a: object, **k: object) -> None:
            pass

        def _begin_busy(self) -> None:
            pass

        def _end_busy(self) -> None:
            pass

        def _confirm_close_steam_async(self, cb, on_cancelled=None, parent=None, alive_fn=None) -> None:
            if on_cancelled is not None:
                on_cancelled()

    win = _CancelSteamWin()
    rows, orphans, newest = pbd.collect_rows(paths)
    target_info = pbd.collect_target_options(paths, newest)

    with (
        mock.patch.object(pbd, "_dialog_parent", return_value=None),
        mock.patch.object(QMessageBox, "information"),
    ):
        def _drive() -> None:
            dlg = next((w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()), None)
            if dlg is None:
                QTimer.singleShot(20, _drive)
                return
            dlg.btn_update.click()  # type: ignore[attr-defined]
            QTimer.singleShot(50, dlg.accept)

        QTimer.singleShot(30, _drive)
        pbd._show(win, rows, orphans, newest, target_info)  # type: ignore[arg-type]
        for w in list(win._workers):
            if hasattr(w, "wait"):
                w.wait(2000)
        app.processEvents()

    assert (game_dir / MANIFEST_V2).read_text(encoding="utf-8") == manifest_before


def test_open_adopt_wizard_single_confirm_and_unified_mixed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    app = QApplication.instance() or QApplication([])
    paths = _steam_paths(tmp_path / "steam")
    appid_a = 3100000010
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    exe_a = _write_pe(lib.games_dir / "Repointed" / "a.exe")
    exe_b = _write_pe(lib.games_dir / "Added" / "b.exe")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game_a = standalone.install_standalone_game(
            "Repointed",
            exe_a,
            game_dir=exe_a.parent,
            create_desktop_shortcut=False,
            create_menu_shortcut=False,
            library=lib,
            uid="ua",
        )
        game_b = standalone.install_standalone_game(
            "Added",
            exe_b,
            game_dir=exe_b.parent,
            create_desktop_shortcut=False,
            create_menu_shortcut=False,
            library=lib,
            uid="ub",
        )
    shortcut_a = steam_vdf.ShortcutEntry(
        index=0, appid_u32=appid_a, app_name="Repointed",
        exe='"/old/path/a.exe"', start_dir='"/old/path"', icon="", launch_options="",
    )

    with (
        mock.patch.object(
            ui, "QSettings", return_value=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        ),
        mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[]),
        mock.patch.object(ui, "resolve_steam_paths", return_value=paths),
        mock.patch.object(ui, "find_steam_root", return_value=paths.steam_root),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(ui, "is_gamescope_session", return_value=False),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_refresh_shortcuts"),
    ):
        win = ui.MainWindow(app)
        try:
            win.paths = paths
            win._steam_shortcuts = [shortcut_a]
            confirm_calls: list[object] = []

            def _fake_check(cb, *args, **kwargs):
                cb(False)

            def _fake_confirm(cb, *args, **kwargs):
                confirm_calls.append(cb)

            res_repoint = adopt.AdoptionResult(
                game=game_a,
                shortcut_appid=appid_a,
                shortcut_needs_repoint=True,
                removed_from_steam=False,
                remove_shortcut_deferred=False,
                add_to_steam=False,
            )
            res_add = adopt.AdoptionResult(
                game=game_b,
                add_to_steam=True,
            )

            with (
                mock.patch.object(win, "_check_steam_running_async", side_effect=_fake_check),
                mock.patch.object(win, "_confirm_close_steam_async", side_effect=_fake_confirm),
                mock.patch.object(ui, "open_adopt_wizard", return_value=[res_repoint]),
            ):
                win._open_adopt_wizard()
            # Repoint-only invokes _confirm_close_steam_async ONCE (no double confirmation).
            assert len(confirm_calls) == 1
            assert win._vdf_queue.pending_count() == 1

            win._vdf_queue.clear()
            confirm_calls.clear()
            with (
                mock.patch.object(win, "_check_steam_running_async", side_effect=_fake_check),
                mock.patch.object(win, "_confirm_close_steam_async", side_effect=_fake_confirm),
                mock.patch.object(ui, "open_adopt_wizard", return_value=[res_repoint, res_add]),
            ):
                win._open_adopt_wizard()
            # Mixed repoint + add_to_steam goes through ONE unified queue & ONE confirm.
            assert len(confirm_calls) == 1
            ops = {e.op for e in win._vdf_queue.list_pending()}
            assert ops == {"update_shortcut_paths", "add_installed_game"}
        finally:
            win._vdf_queue.clear()
            win._skip_vdf_close_prompt = True
            for w in list(win._active_workers):
                w.wait(100)
            win.close()
