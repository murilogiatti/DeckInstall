"""'Move to another disk' flow: pick a library, review the plan, then move (relocate.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QInputDialog, QMessageBox

from .. import __app_name__
from .. import relocate as rl
from ..i18n import tr
from ..library import Library
from ..standalone import StandaloneGame
from ..ui_cards import _dialog_parent, _is_wine_game_running, format_size_bytes
from ..ui_workers import SteamOpWorker

if TYPE_CHECKING:
    from ..ui import MainWindow

_NOTES: dict[str, Callable[[], str]] = {
    rl.W_PREFIX_STAYS: lambda: tr("relocate.warn_prefix_stays"),
    rl.W_STEAM_SHORTCUT: lambda: tr("relocate.warn_steam_shortcut"),
    rl.B_LEGACY: lambda: tr("relocate.block_legacy"),
    rl.B_UNAVAILABLE: lambda: tr("relocate.block_unavailable"),
    rl.B_SAME_LIBRARY: lambda: tr("relocate.block_same_library"),
    rl.B_SYMLINK: lambda: tr("relocate.block_symlink"),
    rl.B_PROTECTED: lambda: tr("relocate.block_protected"),
    rl.B_SHARED_PREFIX: lambda: tr("relocate.block_shared_prefix"),
    rl.B_NO_SPACE: lambda: tr("relocate.block_no_space"),
}


def _library_label(lib: Library) -> str:
    kind = tr("relocate.internal") if lib.internal else (lib.fs_type or "?")
    return f"{lib.label} ({kind}) - {lib.root.parent}"


def plan_summary(plan: rl.RelocatePlan) -> str:
    """Human-readable text for the confirmation (sizes, space, warnings and blockers)."""
    lines = [tr("relocate.confirm", name=plan.game.name, target=plan.target.label)]
    lines.append(tr("relocate.line_game", size=format_size_bytes(plan.game_bytes)))
    if plan.new_prefix_dir is not None:
        lines.append(tr("relocate.line_prefix", size=format_size_bytes(plan.prefix_bytes)))
    if plan.new_cache_dir is not None:
        lines.append(tr("relocate.line_cache", size=format_size_bytes(plan.cache_bytes)))
    if plan.cross_device_bytes:
        lines.append(
            tr(
                "relocate.line_copy",
                size=format_size_bytes(plan.cross_device_bytes),
                free=format_size_bytes(plan.free_bytes),
            )
        )
    else:
        lines.append(tr("relocate.line_rename"))
    for code in (*plan.blockers, *plan.warnings):
        note = _NOTES.get(code)
        lines.append("- " + (note() if note is not None else tr("relocate.unknown", code=code)))
    return "\n".join(lines)


def move_game_to_disk(win: MainWindow, sa: StandaloneGame) -> None:
    """Choose a target library, plan in the background, confirm, then move in the background."""
    if win._steam_busy:
        return
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
        return

    def _fail(err: str) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, err)

    win._begin_busy()
    win.status_label.setText(tr("relocate.planning", name=sa.name))
    lister = SteamOpWorker(lambda: rl.target_libraries(sa))
    win._track_worker(lister)

    def _on_libs(res: object) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        libs = [lib for lib in res if isinstance(lib, Library)] if isinstance(res, list) else []
        if win._closing_pending:
            return
        if not libs:
            QMessageBox.information(_dialog_parent(win), __app_name__, tr("relocate.no_target"))
            return
        labels = [_library_label(lib) for lib in libs]
        choice, ok = QInputDialog.getItem(
            _dialog_parent(win), __app_name__, tr("relocate.pick_target", name=sa.name), labels, 0, False
        )
        if not ok:
            return
        _plan(libs[labels.index(choice)])

    def _plan(target: Library) -> None:
        win._begin_busy()
        win.status_label.setText(tr("relocate.planning", name=sa.name))
        planner = SteamOpWorker(lambda: rl.plan_relocation(sa, target))
        win._track_worker(planner)

        def _on_planned(res: object) -> None:
            win._end_busy()
            win.status_label.setText(tr("status.ready"))
            if win._closing_pending or not isinstance(res, rl.RelocatePlan):
                return
            if not res.can_run:
                QMessageBox.warning(_dialog_parent(win), __app_name__, plan_summary(res))
                return
            ans = QMessageBox.question(_dialog_parent(win), __app_name__, plan_summary(res))
            if ans == QMessageBox.StandardButton.Yes:
                _run(res)

        planner.finished_ok.connect(_on_planned)
        planner.failed.connect(_fail)
        planner.start()

    def _run(plan: rl.RelocatePlan) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
            return
        win._begin_busy()
        win.status_label.setText(tr("relocate.running", name=sa.name))
        snap = win._make_pre_snapshot_fn(force=True)

        def _job() -> object:
            try:
                return rl.relocate_game(plan, snapshot_fn=snap)
            except rl.RelocateError as exc:
                return exc

        worker = SteamOpWorker(_job)
        win._track_worker(worker)

        def _done(res: object) -> None:
            win._end_busy()
            win._refresh_games()
            win._refresh_shortcuts()
            if isinstance(res, rl.RelocateError):
                problems = "\n".join(res.problems)
                if res.rolled_back:
                    msg = tr("relocate.failed_rolled_back", name=sa.name, err=str(res))
                else:
                    msg = tr("relocate.failed_partial", name=sa.name, err=str(res), problems=problems)
                win.status_label.setText(msg.splitlines()[0])
                if not win._closing_pending:
                    QMessageBox.critical(_dialog_parent(win), __app_name__, msg)
                return
            msg = tr("relocate.done", name=sa.name, target=plan.target.label)
            win.status_label.setText(msg)
            if not win._closing_pending:
                QMessageBox.information(_dialog_parent(win), __app_name__, msg)

        worker.finished_ok.connect(_done)
        worker.failed.connect(_fail)
        worker.start()

    lister.finished_ok.connect(_on_libs)
    lister.failed.connect(_fail)
    lister.start()
