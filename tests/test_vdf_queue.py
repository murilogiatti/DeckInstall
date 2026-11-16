"""Tests for deckinstall.vdf_queue."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from deckinstall.steam_paths import SteamPaths
from deckinstall.vdf_queue import QueuedVdfEdit, VdfEditQueue


def _make_paths(tmp_path: Path) -> SteamPaths:
    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "123456"
    (userdata / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "config").mkdir(parents=True, exist_ok=True)
    (steam_root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    shortcuts_vdf = userdata / "config" / "shortcuts.vdf"
    config_vdf = steam_root / "config" / "config.vdf"
    shortcuts_vdf.write_bytes(b"shortcuts")
    config_vdf.write_text('"InstallConfigStore" {}', encoding="utf-8")
    return SteamPaths(
        steam_root=steam_root,
        user_id="123456",
        userdata_dir=userdata,
        shortcuts_vdf=shortcuts_vdf,
        grid_dir=userdata / "config" / "grid",
        config_vdf=config_vdf,
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )


def test_enqueue_coalescing_remove_clear_and_pending_count() -> None:
    queue = VdfEditQueue()
    assert queue.pending_count() == 0

    # set_proton coalesces by (op, appid_u32)
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="p1",
            op="set_proton",
            appid_u32=1001,
            label="Game A Proton",
            payload={"tool_name": "GE-Proton9-1"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="p2",
            op="set_proton",
            appid_u32=1001,
            label="Game A Proton",
            payload={"tool_name": "GE-Proton9-2"},
        )
    )
    # Different appid does not coalesce
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="p3",
            op="set_proton",
            appid_u32=1002,
            label="Game B Proton",
            payload={"tool_name": "GE-Proton9-3"},
        )
    )
    # set_launch_options coalesces by (op, appid_u32), independent of set_proton
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="l1",
            op="set_launch_options",
            appid_u32=1001,
            label="Game A Launch",
            payload={"launch_options": "MANGOHUD=1 %command%"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="l2",
            op="set_launch_options",
            appid_u32=1001,
            label="Game A Launch",
            payload={"launch_options": "gamemoderun %command%"},
        )
    )

    assert queue.pending_count() == 3
    pending = queue.list_pending()
    assert [e.edit_id for e in pending] == ["p2", "p3", "l2"]
    assert pending[0].payload["tool_name"] == "GE-Proton9-2"
    assert pending[2].payload["launch_options"] == "gamemoderun %command%"

    # remove by edit_id
    assert queue.remove("p3") is True
    assert queue.remove("nonexistent") is False
    assert queue.pending_count() == 2

    # clear discards all
    queue.clear()
    assert queue.pending_count() == 0
    assert queue.list_pending() == []


def test_apply_all_uses_single_lock_and_mocked_writers(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    queue = VdfEditQueue()
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="e1",
            op="set_proton",
            appid_u32=2001,
            label="Set Proton",
            payload={"tool_name": "GE-Proton9-5"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="e2",
            op="set_launch_options",
            appid_u32=2001,
            label="Set Launch",
            payload={"launch_options": "ENABLE_LSFG=1 %command%"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="e3",
            op="bulk_migrate_compat",
            appid_u32=0,
            label="Bulk Migrate",
            payload={"appids": [2001, 2002], "target_tool": "GE-Proton9-5"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="e4",
            op="remove_compat",
            appid_u32=2003,
            label="Remove Compat",
            payload={},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="e5",
            op="remove_shortcut",
            appid_u32=2004,
            label="Remove Shortcut",
            payload={},
        )
    )

    lock_entries: list[Path] = []
    backups: list[Path] = []

    @contextmanager
    def fake_lock(path: Path):
        lock_entries.append(path)
        yield

    with (
        patch("deckinstall.vdf_queue._steam_transaction_lock", side_effect=fake_lock),
        patch("deckinstall.vdf_queue._backup_file", side_effect=lambda p: backups.append(p)),
        patch("deckinstall.vdf_queue.set_shortcut_proton") as mock_set_proton,
        patch(
            "deckinstall.vdf_queue.update_shortcut_launch_options", return_value=True
        ) as mock_update_launch,
        patch("deckinstall.vdf_queue.bulk_migrate_compat_tool") as mock_bulk_migrate,
        patch("deckinstall.vdf_queue.remove_compat_tools") as mock_remove_compat,
        patch("deckinstall.vdf_queue.remove_shortcut", return_value=True) as mock_remove_shortcut,
    ):
        result = queue.apply_all(paths)

    assert result.applied == 5
    assert result.errors == []
    assert queue.pending_count() == 0
    assert lock_entries == [paths.shortcuts_vdf]
    assert backups == [paths.shortcuts_vdf, paths.config_vdf]

    mock_set_proton.assert_called_once_with(paths.config_vdf, 2001, "GE-Proton9-5")
    mock_update_launch.assert_called_once_with(paths, 2001, "ENABLE_LSFG=1 %command%")
    mock_bulk_migrate.assert_called_once_with(paths.config_vdf, [2001, 2002], "GE-Proton9-5")
    mock_remove_compat.assert_called_once_with(paths.config_vdf, [2003])
    mock_remove_shortcut.assert_called_once_with(paths, 2004)


def test_apply_all_retains_failed_edits(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    queue = VdfEditQueue()
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="ok",
            op="set_proton",
            appid_u32=3001,
            label="Good Edit",
            payload={"tool_name": "GE-Proton9-5"},
        )
    )
    queue.enqueue(
        QueuedVdfEdit(
            edit_id="fail",
            op="remove_shortcut",
            appid_u32=3002,
            label="Missing Shortcut",
            payload={},
        )
    )

    @contextmanager
    def fake_lock(_path: Path):
        yield

    with (
        patch("deckinstall.vdf_queue._steam_transaction_lock", side_effect=fake_lock),
        patch("deckinstall.vdf_queue._backup_file"),
        patch("deckinstall.vdf_queue.set_shortcut_proton"),
        patch("deckinstall.vdf_queue.remove_shortcut", return_value=False),
    ):
        result = queue.apply_all(paths)

    assert result.applied == 1
    assert len(result.errors) == 1
    assert "Missing Shortcut" in result.errors[0]
    assert queue.pending_count() == 1
    assert queue.list_pending()[0].edit_id == "fail"
