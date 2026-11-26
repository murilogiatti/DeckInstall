"""Steam edit queue: what it announces is saved, bad files never break startup, replays are idempotent."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import vdf

from deckinstall import vdf_queue
from deckinstall.steam_paths import SteamPaths
from deckinstall.steam_vdf import load_shortcuts
from deckinstall.vdf_queue import QueuedVdfEdit, VdfEditQueue, VdfQueueSaveError

APPID = 3_000_000_123  # an unsigned shortcut AppID (stored signed in shortcuts.vdf)


def _signed(appid: int) -> int:
    return appid - (1 << 32) if appid >= 0x80000000 else appid


def _paths(tmp_path: Path, user: str = "111", appids: tuple[int, ...] = (APPID,)) -> SteamPaths:
    root = tmp_path / "Steam"
    userdata = root / "userdata" / user
    (userdata / "config").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    shortcuts = {
        "shortcuts": {
            str(i): {"appid": _signed(a), "AppName": f"Game {i}", "Exe": f'"/games/{i}/game.exe"', "StartDir": '"/games"'}
            for i, a in enumerate(appids)
        }
    }
    (userdata / "config" / "shortcuts.vdf").write_bytes(vdf.binary_dumps(shortcuts))
    (root / "config" / "config.vdf").write_text('"InstallConfigStore"\n{\n}\n', encoding="utf-8")
    return SteamPaths(
        steam_root=root, user_id=user, userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf", grid_dir=userdata / "config" / "grid",
        config_vdf=root / "config" / "config.vdf", compatdata_dir=root / "steamapps" / "compatdata",
    )


def _remove(paths: SteamPaths, appid: int = APPID) -> QueuedVdfEdit:
    return QueuedVdfEdit(
        f"remove_shortcut:{appid}", "remove_shortcut", appid, "Game",
        {"user_id": paths.user_id, "steam_root": str(paths.steam_root)},
    )


def _launch(edit_id: str, appid: int, opts: str = "%command%") -> QueuedVdfEdit:
    return QueuedVdfEdit(edit_id, "set_launch_options", appid, f"G{appid}", {"launch_options": opts})


# --- loading: never fatal, the original is kept -------------------------------------------------


def test_edits_of_wrong_type_do_not_crash_and_the_file_is_kept(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    original = json.dumps({"version": 1, "edits": {"oops": 1}})
    store.write_text(original, encoding="utf-8")
    queue = VdfEditQueue(store)  # used to raise TypeError (MainWindow builds it at startup)
    assert queue.pending_count() == 0
    problem = queue.load_problem
    assert problem is not None and problem.kind == "unreadable" and problem.preserved is not None
    assert problem.preserved.read_text(encoding="utf-8") == original
    assert not store.exists()


@pytest.mark.parametrize("bad_item", ["junk", 7, None, ["list"]])
def test_non_object_entries_are_dropped_but_valid_ones_stay(tmp_path: Path, bad_item: object) -> None:
    store = tmp_path / "vdf_queue.json"
    good = {"edit_id": "ok", "op": "set_proton", "appid_u32": 5, "label": "G", "payload": {"tool_name": "X"}}
    original = json.dumps({"version": 1, "edits": [good, bad_item]})
    store.write_text(original, encoding="utf-8")
    queue = VdfEditQueue(store)
    assert [e.edit_id for e in queue.list_pending()] == ["ok"]
    problem = queue.load_problem
    assert problem is not None and problem.kind == "invalid_entries" and (problem.kept, problem.dropped) == (1, 1)
    assert problem.preserved is not None and problem.preserved.read_text(encoding="utf-8") == original
    # the rewritten queue holds only the valid edit and loads cleanly
    again = VdfEditQueue(store)
    assert again.load_problem is None and [e.edit_id for e in again.list_pending()] == ["ok"]


def test_bad_appid_and_unknown_op_are_rejected(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    items = [
        {"edit_id": "a", "op": "set_proton", "appid_u32": "12", "payload": {}},
        {"edit_id": "b", "op": "set_proton", "appid_u32": -1, "payload": {}},
        {"edit_id": "c", "op": "set_proton", "appid_u32": True, "payload": {}},
        {"edit_id": "d", "op": "rm_rf", "appid_u32": 1, "payload": {}},
        {"edit_id": "", "op": "set_proton", "appid_u32": 1, "payload": {}},
    ]
    store.write_text(json.dumps({"version": 1, "edits": items}), encoding="utf-8")
    queue = VdfEditQueue(store)
    assert queue.pending_count() == 0
    assert queue.load_problem is not None and queue.load_problem.dropped == len(items)


def test_queue_from_a_newer_version_is_kept_aside_not_destroyed(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    original = json.dumps({"version": 7, "edits": [{"whatever": True}]})
    store.write_text(original, encoding="utf-8")
    queue = VdfEditQueue(store)
    assert queue.pending_count() == 0
    problem = queue.load_problem
    assert problem is not None and problem.kind == "future_version"
    assert problem.preserved is not None and problem.preserved.name.startswith("vdf_queue.json.v7-")
    assert problem.preserved.read_text(encoding="utf-8") == original
    queue.enqueue(_launch("x", 1))  # a new queue starts; the newer one is still there
    assert problem.preserved.read_text(encoding="utf-8") == original


def test_empty_file_is_an_empty_queue(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    store.write_text("  \n", encoding="utf-8")
    queue = VdfEditQueue(store)
    assert queue.pending_count() == 0 and queue.load_problem is None


# --- saving: never announce what was not written --------------------------------------------------


def test_enqueue_save_failure_raises_and_counter_matches_disk(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(_launch("first", 1))
    with patch.object(vdf_queue, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(VdfQueueSaveError):
            queue.enqueue(_launch("second", 2))
    assert [e.edit_id for e in queue.list_pending()] == ["first"]  # used to report 2
    assert [e.edit_id for e in VdfEditQueue(store).list_pending()] == ["first"]


def test_remove_and_clear_failures_keep_the_edits(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(_launch("a", 1))
    queue.enqueue(_launch("b", 2))
    with patch.object(vdf_queue, "atomic_write_text", side_effect=OSError(13, "Permission denied")):
        with pytest.raises(VdfQueueSaveError):
            queue.remove("a")
    assert queue.pending_count() == 2
    with patch.object(Path, "unlink", side_effect=OSError(13, "Permission denied")):
        with pytest.raises(VdfQueueSaveError):
            queue.clear()
    assert queue.pending_count() == 2 and VdfEditQueue(store).pending_count() == 2


# --- applying: idempotent replay, Steam check under the locks -------------------------------------


def test_replay_after_interrupted_confirmation_writes_nothing_twice(tmp_path: Path) -> None:
    """The VDF change lands, the queue update fails: the next run confirms it without an error."""
    paths = _paths(tmp_path, appids=(APPID, APPID + 1))
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(_remove(paths))

    real_write = vdf_queue.atomic_write_text
    with patch.object(Path, "unlink", side_effect=OSError(30, "Read-only file system")):
        first = queue.apply_all(paths)
    assert first.applied == 1 and first.errors == [] and first.confirm_error
    assert [e.appid_u32 for e in load_shortcuts(paths.shortcuts_vdf)] == [APPID + 1]
    assert VdfEditQueue(store).pending_count() == 1  # still on disk: not confirmed

    before = paths.shortcuts_vdf.read_bytes()
    with patch.object(vdf_queue, "remove_shortcut", wraps=vdf_queue.remove_shortcut) as spy:
        second = VdfEditQueue(store).apply_all(paths)
    assert (second.applied, second.already_done, second.errors) == (1, 1, [])  # was 0 applied / 1 error
    assert spy.call_count == 1 and paths.shortcuts_vdf.read_bytes() == before  # no second mutation
    assert VdfEditQueue(store).pending_count() == 0
    assert real_write is vdf_queue.atomic_write_text


def test_remove_for_another_account_is_kept_with_a_reason(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(QueuedVdfEdit(
        "remove_shortcut:9", "remove_shortcut", APPID, "Game",
        {"user_id": "999", "steam_root": str(paths.steam_root)},
    ))
    res = queue.apply_all(paths)
    assert res.applied == 0 and len(res.errors) == 1 and "unavailable" in res.errors[0]
    assert VdfEditQueue(store).pending_count() == 1
    assert [e.appid_u32 for e in load_shortcuts(paths.shortcuts_vdf)] == [APPID]


def test_remove_on_another_steam_installation_is_never_applied(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(QueuedVdfEdit(
        "remove_shortcut:x", "remove_shortcut", APPID, "Game",
        {"user_id": paths.user_id, "steam_root": str(tmp_path / "OtherSteam")},
    ))
    res = queue.apply_all(paths)
    assert res.applied == 0 and "another Steam installation" in res.errors[0]
    assert [e.appid_u32 for e in load_shortcuts(paths.shortcuts_vdf)] == [APPID]


def test_missing_shortcut_in_unreadable_vdf_is_an_error_not_done(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.shortcuts_vdf.write_bytes(b"\x00garbage")
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(_remove(paths))
    res = queue.apply_all(paths)
    assert res.applied == 0 and res.errors
    assert VdfEditQueue(store).pending_count() == 1


def test_steam_started_right_before_the_write_defers_everything(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = tmp_path / "vdf_queue.json"
    queue = VdfEditQueue(store)
    queue.enqueue(_remove(paths))
    before = paths.shortcuts_vdf.read_bytes()
    res = queue.apply_all(paths, steam_running=lambda: True)
    assert res.steam_running and res.applied == 0 and res.errors == []
    assert paths.shortcuts_vdf.read_bytes() == before
    assert VdfEditQueue(store).pending_count() == 1


def _enqueue_in_child(store: str, n: int) -> None:
    q = VdfEditQueue(Path(store))
    for i in range(n):
        q.enqueue(_launch(f"{os.getpid()}-{i}", 10_000 + os.getpid() * 100 + i))


def test_two_processes_enqueuing_lose_nothing(tmp_path: Path) -> None:
    store = tmp_path / "vdf_queue.json"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_enqueue_in_child, args=(str(store), 15)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    queue = VdfEditQueue(store)
    assert queue.pending_count() == 30 and queue.load_problem is None
