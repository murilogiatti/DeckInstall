"""Library actions that fetch SteamGridDB artwork for games already in the Library."""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from . import __app_name__
from .i18n import tr
from .library_art import ArtJob, fetch_artwork, has_all_art, job_for_row
from .steamgriddb import api_key
from .ui_cards import _dialog_parent
from .ui_workers import SteamOpWorker

if TYPE_CHECKING:
    from .ui import MainWindow

_KEY_ROLE = Qt.ItemDataRole.UserRole + 1


def _job_at(win: MainWindow, row: int) -> ArtJob | None:
    cell = win.shortcuts_list.item(row, 0)
    if cell is None:
        return None
    data = cell.data(Qt.ItemDataRole.UserRole)
    if not (isinstance(data, tuple) and len(data) == 2):
        return None
    kind, obj = data
    managed = win._library_steam_games.get(getattr(obj, "appid_u32", -1)) if kind == "steam" else None
    name = cell.text().replace("★", "").strip()
    grid_dir = win.paths.grid_dir if win.paths is not None else None
    return job_for_row(kind, obj, str(cell.data(_KEY_ROLE) or ""), name, grid_dir, managed)


def _ensure_key(win: MainWindow) -> bool:
    """No key yet: ask for it right here instead of sending the user elsewhere."""
    if api_key():
        return True
    from .ui_system import set_steamgriddb_key

    set_steamgriddb_key(win)
    return bool(api_key())


def fetch_selected_art(win: MainWindow) -> None:
    row = win.shortcuts_list.currentRow()
    job = _job_at(win, row) if row >= 0 else None
    if job is None or not _ensure_key(win):
        return
    replace = False
    if has_all_art(job):
        ans = QMessageBox.question(_dialog_parent(win), __app_name__, tr("art.replace_prompt", name=job.name))
        if ans != QMessageBox.StandardButton.Yes:
            return
        replace = True
    _run(win, [job], replace=replace)


def fetch_missing_art(win: MainWindow) -> None:
    if not _ensure_key(win):
        return
    jobs = [job for row in range(win.shortcuts_list.rowCount()) if (job := _job_at(win, row)) is not None]
    jobs = [job for job in jobs if not has_all_art(job)]
    if not jobs:
        win.notify(tr("art.nothing_missing"), "info")
        return
    _run(win, jobs, replace=False)


def _run(win: MainWindow, jobs: list[ArtJob], *, replace: bool) -> None:
    win._begin_busy()
    win.status_label.setText(tr("art.running", count=len(jobs)))
    steam_art = any(job.kinds is None for job in jobs)

    def _job() -> object:
        try:
            return fetch_artwork(jobs, replace=replace, cancelled=worker.isInterruptionRequested)
        except requests.HTTPError:
            return None  # the key was refused

    worker = SteamOpWorker(_job)
    win._track_worker(worker)

    def _done(result: object) -> None:
        win._end_busy()
        if win._closing_pending:
            return
        win.status_label.setText(tr("status.ready"))
        if result is None:
            win.notify(tr("art.key_refused"), "error")
            return
        games, files, nothing = result if isinstance(result, tuple) else (0, 0, [])
        lines = [tr("art.done", games=games, files=files)] if games else []
        if nothing:
            lines.append(tr("art.not_found", names=", ".join(nothing[:8]) + ("…" if len(nothing) > 8 else "")))
        if games and steam_art:
            lines.append(tr("art.steam_restart"))
        win.notify("  ".join(lines) or tr("art.nothing_missing"), "success" if games else "warning")
        from .ui_library_grid import sync_views

        sync_views(win, win.shortcuts_list.rowCount() == 0)

    def _err(err: str) -> None:
        win._end_busy()
        if not win._closing_pending:
            win.status_label.setText(tr("status.ready"))
            win.notify(tr("art.error", err=err), "error")

    worker.finished_ok.connect(_done)
    worker.failed.connect(_err)
    worker.start()
