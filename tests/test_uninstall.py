"""Fase 4.1: manifest-guided uninstall (plan, execute, dialog)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import standalone  # noqa: E402
from deckinstall import uninstall as un  # noqa: E402
from deckinstall.manifest import encode_path  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _install(tmp_path: Path, name: str = "Alpha") -> standalone.StandaloneGame:
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game(name, src / "Game.exe", src)
    saves = game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games"
    saves.mkdir(parents=True)
    (saves / "slot1.sav").write_bytes(b"s" * 1000)
    assert game.cache_dir is not None
    game.cache_dir.mkdir(parents=True, exist_ok=True)
    (game.cache_dir / "dxvk.cache").write_bytes(b"c" * 300)
    logs = standalone.game_log_dir(game)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "run.log").write_bytes(b"l" * 50)
    return game


def _ids(plan: un.UninstallPlan) -> dict[str, un.UninstallItem]:
    return {i.id: i for i in plan.items}


def test_plan_lists_items_with_sizes_and_defaults(tmp_path: Path) -> None:
    game = _install(tmp_path)
    items = _ids(un.plan_uninstall(game))
    assert items[un.SHORTCUTS].locked and items[un.SHORTCUTS].checked
    assert items[un.GAME_FILES].size_bytes >= len(PE)
    assert items[un.PREFIX].size_bytes == 1000 and items[un.PREFIX].checked
    assert items[un.SAVES].requires == un.PREFIX
    assert items[un.CACHE].size_bytes == 300
    assert items[un.LOGS].size_bytes == 50
    assert un.STEAM not in items  # never imported into Steam


def test_execute_removes_selected_and_snapshots_before_prefix(tmp_path: Path) -> None:
    game = _install(tmp_path)
    plan = un.plan_uninstall(game)
    seen: list[bool] = []

    def snap(prefix: Path, name: str, reason: str, key: str) -> Path | None:
        seen.append((prefix / "drive_c").is_dir())  # prefix still there
        return None

    selected = {i.id for i in plan.items}
    result = un.execute_uninstall(plan, selected, snap)
    assert seen == [True]
    assert not game.game_dir.exists() and not game.prefix_dir.exists()
    assert game.cache_dir is not None and not game.cache_dir.exists()
    assert not standalone.game_log_dir(game).exists()
    assert not game.desktop_file.exists()
    assert result.freed_bytes == plan.freed_bytes(selected) >= 1350
    assert standalone.list_standalone_games() == []


def test_failed_snapshot_keeps_prefix(tmp_path: Path) -> None:
    game = _install(tmp_path)
    plan = un.plan_uninstall(game)

    def snap(*_a: object) -> Path | None:
        raise OSError("disk full")

    result = un.execute_uninstall(plan, {un.PREFIX, un.SAVES}, snap)
    assert game.prefix_dir.is_dir()
    assert result.errors and un.PREFIX not in result.removed


def test_unchecked_items_stay(tmp_path: Path) -> None:
    game = _install(tmp_path)
    plan = un.plan_uninstall(game)
    result = un.execute_uninstall(plan, {un.CACHE})
    assert game.game_dir.is_dir() and game.prefix_dir.is_dir()
    assert standalone.game_log_dir(game).is_dir()
    assert result.removed == [un.SHORTCUTS, un.CACHE]
    assert result.freed_bytes == plan.freed_bytes({un.CACHE})


def test_shared_prefix_is_locked_and_kept(tmp_path: Path) -> None:
    game = _install(tmp_path)
    other = _install(tmp_path, "Beta")
    other.prefix_dir = game.prefix_dir
    standalone._write_manifest(other)
    items = _ids(un.plan_uninstall(game))
    assert items[un.PREFIX].locked and not items[un.PREFIX].checked
    assert items[un.PREFIX].reason == un.REASON_SHARED_PREFIX
    assert un.SAVES not in items
    un.execute_uninstall(un.plan_uninstall(game), {un.PREFIX, un.GAME_FILES})
    assert game.prefix_dir.is_dir()


def test_out_of_root_and_symlinked_paths_are_refused(tmp_path: Path) -> None:
    game = _install(tmp_path)
    outside = tmp_path / "elsewhere" / "pfx"
    (outside / "drive_c").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x", encoding="utf-8")
    cache_link = game.cache_dir.parent / "linked"  # type: ignore[union-attr]
    cache_link.symlink_to(victim)
    game.prefix_dir = outside
    game.cache_dir = cache_link
    plan = un.plan_uninstall(game)
    items = _ids(plan)
    assert items[un.PREFIX].locked and items[un.PREFIX].reason == un.REASON_OUTSIDE_ROOTS
    assert items[un.CACHE].locked
    un.execute_uninstall(plan, {i.id for i in plan.items})
    assert (outside / "drive_c").is_dir()
    assert (victim / "keep.txt").is_file() and cache_link.is_symlink()


def test_symlinked_game_dir_is_not_followed(tmp_path: Path) -> None:
    game = _install(tmp_path)
    real = tmp_path / "real_game"
    real.mkdir()
    (real / "data.bin").write_bytes(b"d")
    link = tmp_path / "linked_game"
    link.symlink_to(real)
    game.game_dir = link
    items = _ids(un.plan_uninstall(game))
    assert items[un.GAME_FILES].locked and items[un.GAME_FILES].reason == un.REASON_SYMLINK
    un.execute_uninstall(un.plan_uninstall(game), {un.GAME_FILES})
    assert (real / "data.bin").is_file()


def test_compatdata_link_removed_only_when_it_points_at_prefix_and_backup_restored(tmp_path: Path) -> None:
    game = _install(tmp_path)
    compat_dir = tmp_path / "Steam" / "compatdata"
    compat_dir.mkdir(parents=True)
    link = compat_dir / "3000000001"
    link.symlink_to(game.prefix_dir)
    backup = compat_dir / "3000000001.deckinstall.bak"
    (backup / "pfx").mkdir(parents=True)
    game.meta["steam"] = {
        "compatdata_link": encode_path(link, None),
        "compatdata_backup": encode_path(backup, None),
    }
    items = _ids(un.plan_uninstall(game))
    assert items[un.STEAM].paths == [link, backup] and items[un.STEAM].checked
    un.execute_uninstall(un.plan_uninstall(game), {un.PREFIX, un.STEAM})
    assert not link.is_symlink() and (link / "pfx").is_dir()  # Steam's old prefix is back
    assert not backup.exists()

    # A link pointing elsewhere is not ours to remove.
    game2 = _install(tmp_path, "Gamma")
    foreign = tmp_path / "microsd" / "pfx"
    foreign.mkdir(parents=True)
    link2 = compat_dir / "3000000002"
    link2.symlink_to(foreign)
    game2.meta["steam"] = {"compatdata_link": encode_path(link2, None)}
    plan2 = un.plan_uninstall(game2)
    assert un.STEAM not in _ids(plan2)
    un.execute_uninstall(plan2, {i.id for i in plan2.items})
    assert link2.is_symlink() and foreign.is_dir()


def test_covered_items_not_counted_twice(tmp_path: Path) -> None:
    game = _install(tmp_path)
    icon = game.game_dir / ".deckinstall_icon.png"
    icon.write_bytes(b"i" * 10)
    game.meta["icons"] = [icon]
    plan = un.plan_uninstall(game)
    items = _ids(plan)
    assert items[un.ICONS].covered_by == un.GAME_FILES
    both = plan.freed_bytes({un.GAME_FILES, un.ICONS})
    assert both == plan.freed_bytes({un.GAME_FILES})
    assert plan.freed_bytes({un.ICONS}) == items[un.SHORTCUTS].size_bytes + 10
    un.execute_uninstall(plan, {un.ICONS})
    assert not icon.exists() and game.game_dir.is_dir()


def test_dialog_toggles_update_total_and_lock_items(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    from deckinstall.ui_dialogs.uninstall_dialog import UninstallDialog

    game = _install(tmp_path)
    other = _install(tmp_path, "Beta")
    other.prefix_dir = game.prefix_dir
    standalone._write_manifest(other)
    with (game.cache_dir / "big.cache").open("wb") as fh:  # type: ignore[operator]
        fh.truncate(5 * 1024 * 1024)
    plan = un.plan_uninstall(game)
    dlg = UninstallDialog(plan)
    try:
        assert not dlg.boxes[un.PREFIX].isEnabled()
        assert not dlg.boxes[un.SHORTCUTS].isEnabled()
        before = dlg.total_label.text()
        dlg.boxes[un.CACHE].setChecked(False)
        assert dlg.total_label.text() != before
        assert un.CACHE not in dlg.selected_ids() and un.PREFIX not in dlg.selected_ids()
    finally:
        dlg.deleteLater()  # parentless dialog: delete it while Qt is still up
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_remove_existing_button_disabled_while_busy(tmp_path: Path) -> None:
    from deckinstall import ui

    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
    ):
        win = ui.MainWindow(app)
    try:
        game = _install(tmp_path)
        with (
            mock.patch.object(win, "_find_matching_installed", return_value=(game, None)),
            mock.patch.object(win, "_uninstall_standalone_async") as uninstall,
        ):
            win._begin_busy()
            assert not win.btn_remove_existing.isEnabled()
            win._remove_existing_from_add()
            uninstall.assert_not_called()
            win._end_busy()
            assert win.btn_remove_existing.isEnabled()
            win._remove_existing_from_add()
            uninstall.assert_called_once_with(game, steam_entry=None, refresh_games=True)
    finally:
        # Let the window's background workers finish: a QThread alive at interpreter exit segfaults.
        for w in list(win._active_workers):
            w.wait(2000)
        app.processEvents()
        win._active_workers.clear()
        win.close()


def test_leftovers_offer_snapshots_and_rejects_replaced_paths(tmp_path: Path) -> None:
    game = _install(tmp_path)
    result = un.execute_uninstall(un.plan_uninstall(game), {un.LOGS})
    assert result.remaining is not None and result.remaining.leftovers
    assert {item.id for item in result.remaining.items} == {un.GAME_FILES, un.PREFIX, un.CACHE}
    assert all(not item.checked for item in result.remaining.items)

    assert game.cache_dir is not None
    original_cache = game.cache_dir.with_name("original-cache")
    game.cache_dir.rename(original_cache)
    game.cache_dir.mkdir()
    replacement = game.cache_dir / "must-stay"
    replacement.write_bytes(b"new owner")
    snapshot = mock.Mock(return_value=tmp_path / "backup.zip")
    clean = un.execute_uninstall(result.remaining, {un.PREFIX, un.CACHE}, snapshot)
    snapshot.assert_called_once()
    assert un.SHORTCUTS not in clean.removed
    assert un.PREFIX in clean.removed and not game.prefix_dir.exists()
    assert replacement.is_file() and clean.errors
    assert game.game_dir.is_dir()
