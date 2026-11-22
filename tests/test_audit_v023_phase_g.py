"""Operation counts and cache invalidation regressions for audit phase G."""

import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from deckinstall import gameinfo, ludusavi, play_monitor, runtime_manager, save_sync


def test_technology_scan_maps_one_binary_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ v4.2.1.stable.official D3D11.DLL\x00\x00" + "vulkan-1.dll".encode("utf-16-le"))
    mapped = Mock(wraps=gameinfo.mmap.mmap)
    monkeypatch.setattr(gameinfo.mmap, "mmap", mapped)

    gfx, _ = gameinfo.detect_game_tech(exe)
    assert gfx == "DirectX 11 + Vulkan"
    mapped.assert_called_once()


def test_ludusavi_cold_cache_builds_once_and_returns_independent_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"Game":{"steam":{"id":"1"}}}')
    monkeypatch.setattr(save_sync, "DEFAULT_LUDUSAVI_MANIFEST_PATH", manifest)
    build = Mock(wraps=save_sync._build_ludusavi_index)
    monkeypatch.setattr(save_sync, "_build_ludusavi_index", build)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save_sync.find_manifest_game_entry, ["Game"] * 12))

    build.assert_called_once()
    first = results[0]
    assert first is not None
    assert isinstance(first["steam"], dict)
    first["steam"]["id"] = "changed"
    assert save_sync.load_ludusavi_manifest() == {"Game": {"steam": {"id": "1"}}}


def test_umu_cache_detects_replaced_inode_and_rejects_symlink(tmp_path: Path) -> None:
    db = tmp_path / "umu.csv"
    db.write_text("TITLE,UMU_ID\nGame,umu-1\n")
    index = runtime_manager._get_umu_db_index(db)
    assert index is not None and index.umu_ids == ("umu-1",)
    previous = db.stat()
    replacement = tmp_path / "new.csv"
    replacement.write_text("TITLE,UMU_ID\nGame,umu-2\n")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(db)

    updated = runtime_manager._get_umu_db_index(db)
    assert updated is not None and updated.umu_ids == ("umu-2",)
    db.rename(replacement)
    db.symlink_to(replacement)
    assert runtime_manager._get_umu_db_index(db) is None


def test_ludusavi_plans_multiple_home_files_with_one_zip_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    folder = tmp_path / "backup"
    folder.mkdir()
    files = {}
    with zipfile.ZipFile(folder / "full.zip", "w") as archive:
        for name in ("a.sav", "b.sav", "c.sav"):
            original = str(home / ".local" / "share" / "game" / name)
            files[original] = {"size": 1, "hash": "0" * 40}
            archive.writestr(f"drive-0/{original.lstrip('/')}", b"x")
    mapping = {"name": "Game", "backups": [{"name": "full.zip", "files": files}]}
    opener = Mock(wraps=zipfile.ZipFile)
    monkeypatch.setattr(ludusavi.zipfile, "ZipFile", opener)

    _, plan, skipped = ludusavi._plan_sources(folder, home, mapping=mapping)
    assert skipped == 0 and len(plan) == 3
    opener.assert_called_once()


def test_snapshot_collects_saves_once_and_matches_existing_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "pfx"
    save = prefix / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot.sav"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"save slot")
    expected = save_sync.compute_prefix_saves_fingerprint(prefix, "Game").digest
    collect = Mock(wraps=save_sync._collect_prefix_save_entries)
    monkeypatch.setattr(save_sync, "_collect_prefix_save_entries", collect)
    dest = tmp_path / "snapshots"

    archive = save_sync.auto_snapshot(prefix, "Game", "test", dest_root=dest)

    assert archive is not None and expected
    collect.assert_called_once()
    assert save_sync._read_snapshot_index(archive.parent)["digest"] == expected
    assert save_sync.auto_snapshot(prefix, "Game", "test", dest_root=dest) == archive
    assert list(archive.parent.glob("*.zip")) == [archive]


def test_sync_without_remote_does_not_hash_and_explicit_preview_does_not_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = tmp_path / "Game"
    game.mkdir()
    (game / "slot.sav").write_bytes(b"save")
    prefix = tmp_path / "prefix"
    target = save_sync.SyncGameTarget("Game", prefix, extra_dirs=(("steamcloud", game),))
    sync = tmp_path / "sync"
    no_hash = Mock(side_effect=AssertionError("read save contents"))
    monkeypatch.setattr(save_sync, "compute_prefix_saves_fingerprint", no_hash)
    assert save_sync.plan_sync_all([target], sync)[0].action == "PUSH"
    monkeypatch.setattr(save_sync, "plan_sync_all", Mock(side_effect=AssertionError("replanned")))
    preview = save_sync.preview_sync_operation("Game", prefix, sync, "PUSH", extra_dirs=target.extra_dirs)
    assert [item.rel_path for item in preview.files] == ["steamcloud/slot.sav"]


def test_proc_batch_reads_each_pid_once_and_skips_kernel_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = ("/games/game-a",)
    two = ("/games/game-b",)
    base = tmp_path / "123"
    base.mkdir()
    (base / "cmdline").write_bytes(b"wine\0")
    (base / "stat").write_bytes(b"123 (game.exe) S 1 1 123")
    (base / "environ").write_bytes(b"WINEPREFIX=/games/game-a/pfx\0STEAM_COMPAT_DATA_PATH=/games/game-b\0")
    kernel = tmp_path / "124"
    kernel.mkdir()
    (kernel / "cmdline").touch()
    read = Mock(wraps=play_monitor._read)
    monkeypatch.setattr(play_monitor, "_read", read)

    snapshot = play_monitor._scan_processes({one, two}, {"game.exe"}, str(tmp_path))

    assert snapshot.matches == {one: {123}, two: {123}}
    assert snapshot.exe_pids("game.exe") == {123}
    reads = [call.args[0] for call in read.call_args_list]
    assert reads.count(str(base / "environ")) == 1
    assert str(kernel / "environ") not in reads
