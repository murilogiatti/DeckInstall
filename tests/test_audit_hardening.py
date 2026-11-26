"""Tests for audit hardening: security, concurrency, 1,200-game performance, 1024x640 UX, and multi-account Steam."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

import pytest
import vdf

from deckinstall import (
    fsutil,
    library,
    manifest,
    play_monitor,
    remove_all,
    runtime_manager,
    save_sync,
    standalone,
    steam_paths,
)


def test_manifest_sanitizes_traversal_uid_and_desktop_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps = tmp_path / "apps"
    desk = tmp_path / "desktop"
    apps.mkdir()
    desk.mkdir()
    monkeypatch.setattr(standalone, "get_applications_dir", lambda: apps)
    monkeypatch.setattr(standalone, "get_desktop_dir", lambda: desk)

    evil_target = tmp_path / ".bashrc"
    evil_target.write_text("# keep me safe\n", encoding="utf-8")

    game_dir = tmp_path / "Games" / "EvilGame"
    game_dir.mkdir(parents=True)
    exe = game_dir / "game.exe"
    exe.write_bytes(b"MZ")
    prefix_dir = game_dir / "prefix"
    prefix_dir.mkdir()
    launch_sh = game_dir / "launch.sh"
    launch_sh.write_text("#!/bin/sh\n", encoding="utf-8")

    raw = {
        "schema": 2,
        "id": "../../../escape",
        "name": "EvilGame",
        "game": {"dir": "Games/EvilGame", "exe": "game.exe", "native_linux": False},
        "prefix": {"path": str(prefix_dir)},
        "cache": {"path": ""},
        "proton": {"name": "GE-Proton"},
        "runtime": {"winedlloverrides": "", "launch_options": ""},
        "created": {
            "launch_script": str(launch_sh),
            "desktop_files": [str(evil_target)],
            "desktop_shortcuts": [str(evil_target)],
        },
        "source": {},
    }
    mf = game_dir / manifest.MANIFEST_V2
    mf.write_text(json.dumps(raw), encoding="utf-8")

    loaded = standalone._load_manifest(mf)
    assert loaded is not None
    assert loaded.uid == ""
    assert loaded.desktop_file.parent == apps
    assert loaded.desktop_shortcut is None

    # Even if someone mutates desktop_file directly, refresh_integration_files refuses out-of-bounds targets.
    loaded.desktop_file = evil_target
    standalone.refresh_integration_files(loaded)
    assert evil_target.read_text(encoding="utf-8") == "# keep me safe\n"


def test_clean_child_env_strips_api_tokens() -> None:
    env = fsutil.clean_child_env(
        {
            "PATH": "/usr/bin",
            "DECKINSTALL_STEAMGRIDDB_KEY": "secret-sgdb",
            "DECKINSTALL_GITHUB_TOKEN": "ghp_secret",
            "KEEP_ME": "1",
        }
    )
    assert "DECKINSTALL_STEAMGRIDDB_KEY" not in env
    assert "DECKINSTALL_GITHUB_TOKEN" not in env
    assert env["KEEP_ME"] == "1"


def test_winetricks_verb_regex_rejects_leading_dash() -> None:
    assert runtime_manager._WINETRICKS_VERB_RE.fullmatch("vcrun2022")
    assert runtime_manager._WINETRICKS_VERB_RE.fullmatch("d3dcompiler_47")
    assert not runtime_manager._WINETRICKS_VERB_RE.fullmatch("--self-update")
    assert not runtime_manager._WINETRICKS_VERB_RE.fullmatch("-k")


def test_bwrap_binary_path_ignores_user_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    resolved = runtime_manager._bwrap_binary_path()
    assert resolved in ("/usr/bin/bwrap", "/bin/bwrap")
    assert str(tmp_path) not in resolved


def test_redirect_drops_authorization_on_cross_host(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_headers: list[dict[str, str]] = []

    class _Resp:
        def __init__(self, status: int, headers: dict[str, str]) -> None:
            self.status_code = status
            self.headers = headers
            self.is_redirect = status in (301, 302, 303, 307, 308)

        def close(self) -> None:
            pass

    calls = 0

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, stream: bool, allow_redirects: bool):
        nonlocal calls
        calls += 1
        seen_headers.append(dict(headers))
        if calls == 1:
            return _Resp(302, {"Location": "https://objects.githubusercontent.com/file.bin"})
        return _Resp(200, {})

    monkeypatch.setattr(runtime_manager.requests, "get", fake_get)
    resp = runtime_manager._request_github_with_validated_redirects(
        "https://api.github.com/repos/a/b/releases/latest",
        headers={"Authorization": "Bearer topsecret", "User-Agent": "DeckInstall"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert "Authorization" in seen_headers[0]
    assert "Authorization" not in seen_headers[1]


def test_library_concurrent_record_and_corrupt_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    lib_root = tmp_path / "DeckInstall"
    library.ensure_library(lib_root, label="Test", internal=True)

    errors: list[Exception] = []

    def _worker(idx: int) -> None:
        try:
            gdir = lib_root / "Games" / f"Game{idx}"
            gdir.mkdir(parents=True, exist_ok=True)
            library.record_game(lib_root, f"uid-{idx}", f"Game {idx}", gdir)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    data = library.read_library_file(lib_root)
    assert data is not None
    assert len(data["games"]) == 12

    # Corrupting library.json must NOT silently wipe it with games: [].
    (lib_root / library.LIBRARY_FILE).write_text("{corrupt", encoding="utf-8")
    with pytest.raises(OSError, match="corrupt"):
        library.ensure_library(lib_root, label="Test", internal=True)


def test_play_monitor_recovers_after_scan_exception() -> None:
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    mon = play_monitor.PlayMonitor(None)
    bad: Future = Future()
    bad.set_exception(RuntimeError("proc glitch"))
    mon._proc_poll = bad
    mon._tick()
    assert mon._proc_poll is None


def test_save_sync_slug_collision_and_legacy_compat(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    # Fresh games with colliding ASCII punctuation get distinct folders.
    d1 = save_sync.snapshot_dir_for("Halo: Reach", snaps)
    d2 = save_sync.snapshot_dir_for("Halo - Reach", snaps)
    assert d1 != d2

    # Legacy folder belonging to "Halo: Reach" is preserved on lookup, while "Halo - Reach" stays isolated.
    legacy = snaps / "halo-reach"
    legacy.mkdir(parents=True)
    (legacy / ".deckinstall_snapshot.json").write_text(json.dumps({"name": "Halo: Reach"}), encoding="utf-8")
    assert save_sync.snapshot_dir_for("Halo: Reach", snaps) == legacy
    assert save_sync.snapshot_dir_for("Halo - Reach", snaps) != legacy

    # pre-sync-pull snapshots are protected from rotation.
    assert save_sync._PROTECTED_SNAPSHOT_RE.search("20260101-120000-pre-sync-pull.zip")


def test_push_game_saves_preserves_zero_byte_and_skips_fifo(tmp_path: Path) -> None:
    pfx = tmp_path / "pfx"
    save_dir = pfx / "drive_c" / "users" / "steamuser" / "Documents" / "MyGame"
    save_dir.mkdir(parents=True)
    (save_dir / "slot1.sav").write_bytes(b"progress")
    (save_dir / "marker.empty").write_bytes(b"")
    fifo = save_dir / "pipe.fifo"
    os.mkfifo(fifo)

    sync_root = tmp_path / "sync"
    archive = save_sync.push_game_saves("MyGame", pfx, sync_root)
    assert archive is not None and archive.is_file()
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert any(n.endswith("slot1.sav") for n in names)
    assert any(n.endswith("marker.empty") for n in names)
    assert not any(n.endswith("pipe.fifo") for n in names)


def test_remove_all_lists_covers_and_locks() -> None:
    assert "covers" in remove_all._OWN_DATA_ENTRIES
    assert "libraries.lock" in remove_all._OWN_DATA_ENTRIES
    assert "vdf_queue.apply.lock" in remove_all._OWN_DATA_ENTRIES


def test_cli_version_does_not_import_pyside6() -> None:
    code = (
        "import sys\n"
        "from deckinstall import cli\n"
        "try:\n"
        "    cli.run(['--version'])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 0\n"
        "assert 'PySide6' not in sys.modules, 'PySide6 should not be imported for --version'\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_multi_account_steam_detection_and_ordering(tmp_path: Path) -> None:
    root = tmp_path / "Steam"
    (root / "userdata" / "111111").mkdir(parents=True)
    (root / "userdata" / "222222").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    loginusers = {
        "users": {
            str(76561197960265728 + 111111): {
                "AccountName": "alice_acc",
                "PersonaName": "Alice",
                "MostRecent": "0",
                "AllowAutoLogin": "0",
                "Timestamp": "1700000000",
            },
            str(76561197960265728 + 222222): {
                "AccountName": "bob_acc",
                "PersonaName": "Bob",
                "MostRecent": "0",
                "AllowAutoLogin": "1",
                "Timestamp": "1700000500",
            },
        }
    }
    (root / "config" / "loginusers.vdf").write_text(vdf.dumps(loginusers), encoding="utf-8")

    accounts = steam_paths.list_steam_accounts(root)
    assert [a.user_id for a in accounts] == ["222222", "111111"]
    assert accounts[0].display_label == "Bob (222222)"
    assert accounts[1].display_label == "Alice (111111)"


def test_library_1200_games_performance_and_no_results_state(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTableWidgetItem

    from tests.test_audit_phase2 import _make_window

    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        win._library_loaded = True
        table = win.shortcuts_list
        table.setSortingEnabled(False)
        table.setRowCount(1200)
        for i in range(1200):
            cell = QTableWidgetItem(f"Game Title {i:04d}")
            cell.setData(Qt.ItemDataRole.UserRole, ("standalone", None))
            cell.setData(Qt.ItemDataRole.UserRole + 1, f"standalone:uid-{i}")
            table.setItem(i, 0, cell)
        table.setSortingEnabled(True)

        t0 = time.perf_counter()
        win.btn_view_grid.click()
        elapsed_grid = time.perf_counter() - t0
        assert win.library_grid.count() == 1200
        assert elapsed_grid < 1.5, f"Grid switch with 1,200 games took {elapsed_grid:.3f}s"

        t1 = time.perf_counter()
        win.library_search_input.setText("Title 0999")
        win._filter_library_list()
        elapsed_filter = time.perf_counter() - t1
        assert win.library_grid.count() == 1
        assert elapsed_filter < 0.5, f"Filter with 1,200 games took {elapsed_filter:.3f}s"

        # Searching for something that matches 0 games shows the no-results state.
        win.library_search_input.setText("nonexistent-xyz-99999")
        win._filter_library_list()
        assert not win.library_no_results_state.isHidden()
        assert win.library_empty_state.isHidden()
        assert win.library_grid.isHidden()

        # Clearing filters restores all 1,200 games.
        win._clear_library_filters()
        assert win.library_no_results_state.isHidden()
        assert win.library_grid.count() == 1200
    finally:
        win._force_closing = True
        win.close()
        app.processEvents()


def test_clamp_dialog_size_respects_deck_1024x640() -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication, QDialog

    from deckinstall.ui_cards import clamp_dialog_size

    _app = QApplication.instance() or QApplication([])
    dlg = QDialog()
    fake_screen = mock.Mock()
    fake_screen.availableGeometry.return_value = QRect(0, 0, 1024, 640)
    with mock.patch("deckinstall.ui_cards.QGuiApplication.primaryScreen", return_value=fake_screen):
        clamp_dialog_size(dlg, 1100, 640)
    assert dlg.width() <= 1024
    assert dlg.height() <= 640
    dlg.deleteLater()
