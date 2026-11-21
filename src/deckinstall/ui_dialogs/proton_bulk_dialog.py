"""Proton in batch, like OptiScaler in batch: one list of games, always the newest GE-Proton.

DeckInstall games are put in "Default" mode (their launcher follows the newest GE-Proton) and
Steam shortcuts are mapped to the newest GE-Proton folder. "Update" first downloads a newer
GE-Proton when GitHub has one, so the target is the newest available, not only the newest local.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import __app_name__
from ..i18n import tr
from ..proton_policy import (
    effective_proton_name,
    is_default,
    newest_ge_proton,
    retarget_default_games,
    set_games_proton,
    use_previous_proton,
)
from ..runtime_manager import check_runtime_updates, ensure_ge_proton11
from ..standalone import list_standalone_games, match_owned_shortcuts, primary_owned_shortcuts
from ..steam_paths import SteamPaths
from ..steam_vdf import (
    _strip_vdf_quotes,
    bulk_migrate_compat_tool,
    get_compat_tool_map,
    load_shortcuts,
    orphan_shortcut_mappings,
    remove_orphan_shortcut_mappings,
)
from ..ui_cards import _dialog_guard, _dialog_parent, _format_worker_error
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit

if TYPE_CHECKING:
    from ..ui import MainWindow


@dataclass(frozen=True)
class ProtonRow:
    name: str
    source: str  # "deckinstall" | "shortcut"
    current: str  # Proton in use now ("" = Steam's default tool)
    newest: bool  # already on the newest GE-Proton
    game_dir: Path | None = None  # DeckInstall game
    steam_tools: tuple[tuple[int, str], ...] = ()
    has_previous: bool = False


def collect_rows(paths: SteamPaths | None) -> tuple[list[ProtonRow], list[int], str]:
    """(rows, orphan Steam mappings, newest local GE-Proton name) for the dialog."""
    newest = newest_ge_proton()
    newest_name = newest.name if newest is not None else ""
    games = list_standalone_games()
    orphans: list[int] = []
    shortcuts = []
    compat: dict[int, str] = {}
    if paths is not None:
        try:
            shortcuts = load_shortcuts(paths.shortcuts_vdf)
            compat = get_compat_tool_map(paths.config_vdf)
        except Exception:
            logging.exception("Could not read Steam shortcuts for bulk Proton")
        try:
            orphans = orphan_shortcut_mappings(paths)
        except (OSError, ValueError):
            logging.exception("Could not verify all Steam shortcut files for orphan cleanup")
    owned = match_owned_shortcuts(shortcuts, games, paths.user_id if paths else None)
    primary = primary_owned_shortcuts(shortcuts, owned)
    chosen_appids = {entry.appid_u32 for entry in primary.values()}
    rows = [
        ProtonRow(
            name=g.name,
            source="deckinstall",
            current=effective_proton_name(g),
            newest=bool(newest_name) and (is_default(g) or g.proton_name.strip() == newest_name),
            game_dir=g.game_dir,
            has_previous=bool(str(g.meta.get("proton_previous") or "").strip()),
        )
        for g in games if not g.native_linux and g.game_dir not in primary
    ]
    for sc in shortcuts:
        if not _strip_vdf_quotes(sc.exe).lower().endswith((".exe", ".bat")):
            continue  # native Linux shortcuts do not run Proton
        game = owned.get(sc.appid_u32)
        if game is not None and sc.appid_u32 not in chosen_appids:
            continue
        tool = compat.get(sc.appid_u32, "")
        rows.append(
            ProtonRow(game.name if game is not None else sc.app_name, "shortcut", tool,
                      bool(newest_name) and tool == newest_name, steam_tools=((sc.appid_u32, tool),))
        )
    rows.sort(key=lambda r: r.name.lower())
    return rows, orphans, newest_name


def ensure_newest_ge_proton(progress: Callable[[str], None] | None = None) -> str:
    """Download a newer GE-Proton when GitHub has one; move "Default" games onto it. Returns its name."""
    before = newest_ge_proton()
    info = check_runtime_updates(force=True).get("ge_proton", {})
    if info.get("update_available") or not info.get("installed"):
        ensure_ge_proton11(
            force_update=bool(info.get("update_available")),
            progress_cb=(lambda msg, _pct: progress(msg)) if progress is not None else None,
        )
    after = newest_ge_proton()
    if after is None:
        raise RuntimeError(tr("proton.none_found"))
    if before is not None and before.name != after.name:
        retarget_default_games(before.name)
    return after.name


class _LoadWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        try:
            self.ready.emit(collect_rows(self._paths))
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class _UpdateWorker(QThread):
    """Newest GE-Proton (download when needed), then the DeckInstall games; Steam goes after it."""

    status = Signal(str)
    done = Signal(str, int, list)
    failed = Signal(str)

    def __init__(self, game_dirs: list[Path]) -> None:
        super().__init__()
        self._dirs = game_dirs

    def run(self) -> None:
        try:
            name = ensure_newest_ge_proton(self.status.emit)
            changed, errors = set_games_proton(self._dirs, "") if self._dirs else (0, [])
            self.done.emit(name, changed, errors)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


def bulk_proton_dialog(win: MainWindow) -> None:
    """Open the batch Proton dialog (load off the UI thread)."""
    win.status_label.setText(tr("proton_bulk.loading"))
    worker = _LoadWorker(win.paths)
    win._track_worker(worker)

    def _loaded(res: object) -> None:
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending or not isinstance(res, tuple):
            return
        rows, orphans, newest = res
        if not rows and not orphans:
            QMessageBox.information(_dialog_parent(win), __app_name__, tr("proton_bulk.empty"))
            return
        _show(win, rows, orphans, newest)

    def _failed(err: str) -> None:
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("proton.error", err=err))

    worker.ready.connect(_loaded)
    worker.failed.connect(_failed)
    worker.start()


def _source_text(row: ProtonRow) -> str:
    return tr("proton_bulk.source_deckinstall") if row.source == "deckinstall" else tr("proton_bulk.source_shortcut")


def _show(win: MainWindow, rows: list[ProtonRow], orphans: list[int], newest: str) -> None:
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("proton_bulk.title"))
    dlg.resize(900, 560)
    dlg_alive = _dialog_guard(dlg)
    busy_state = {"active": False}
    orig_close_event = dlg.closeEvent

    def _close_event(event) -> None:
        if busy_state["active"]:
            event.ignore()
            return
        orig_close_event(event)

    dlg.closeEvent = _close_event  # type: ignore[method-assign]
    lay = QVBoxLayout(dlg)
    hint = QLabel(tr("proton_bulk.hint", name=newest or "GE-Proton"))
    hint.setWordWrap(True)
    lay.addWidget(hint)

    tbl = QTableWidget(0, 4)
    tbl.setHorizontalHeaderLabels(
        [tr("table.name"), tr("proton_bulk.col_source"), tr("proton_bulk.col_current"), tr("proton_bulk.col_state")]
    )
    tbl.verticalHeader().setVisible(False)
    tbl.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    header = tbl.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for col in (1, 2, 3):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    lay.addWidget(tbl, 1)

    current: list[ProtonRow] = []

    def _fill(data: list[ProtonRow]) -> None:
        current[:] = data
        tbl.setRowCount(0)
        for r in data:
            row = tbl.rowCount()
            tbl.insertRow(row)
            it = QTableWidgetItem(r.name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if r.newest else Qt.CheckState.Checked)
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, QTableWidgetItem(_source_text(r)))
            if r.game_dir is None:
                proton = r.current or tr("proton.steam_default_label")
            else:
                proton = r.current or tr("proton.default_label")
            tbl.setItem(row, 2, QTableWidgetItem(proton))
            tbl.setItem(row, 3, QTableWidgetItem(
                tr("proton_bulk.state_newest") if r.newest else tr("proton_bulk.state_outdated")
            ))

    _fill(rows)

    btn_select_all = QPushButton(tr("optiscaler_bulk.btn_select_all"))
    btn_select_none = QPushButton(tr("optiscaler_bulk.btn_select_none"))
    btn_update = QPushButton(tr("proton_bulk.btn_update"))
    btn_update.setObjectName("primaryBtn")
    btn_update_all = QPushButton(tr("proton_bulk.btn_update_all"))
    btn_previous = QPushButton(tr("configure.btn_previous_proton"))
    btn_previous.setToolTip(tr("proton_bulk.btn_previous_tip"))
    btn_orphans = QPushButton(tr("proton_bulk.btn_clean_orphans", count=len(orphans)))
    btn_orphans.setVisible(bool(orphans))
    btn_versions = QPushButton(tr("library.btn_manage_protons"))
    btn_close = QPushButton(tr("proton_bulk.btn_close"))
    row1 = QHBoxLayout()
    for b in (btn_select_all, btn_select_none, btn_update_all, btn_update):
        row1.addWidget(b)
    row1.addStretch()
    row2 = QHBoxLayout()
    for b in (btn_previous, btn_orphans, btn_versions):
        row2.addWidget(b)
    row2.addStretch()
    row2.addWidget(btn_close)
    lay.addLayout(row1)
    lay.addLayout(row2)
    actions = (
        btn_update,
        btn_update_all,
        btn_previous,
        btn_orphans,
        btn_versions,
        btn_select_all,
        btn_select_none,
        btn_close,
    )
    btn_close.clicked.connect(dlg.accept)

    def _checked() -> list[ProtonRow]:
        return [
            r for i, r in enumerate(current)
            if (it := tbl.item(i, 0)) is not None and it.checkState() == Qt.CheckState.Checked
        ]

    def _set_all(state: Qt.CheckState) -> None:
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None:
                it.setCheckState(state)

    def _busy(on: bool) -> None:
        busy_state["active"] = on
        if not dlg_alive():
            return
        for b in actions:
            b.setEnabled(not on)

    def _reload(extra: list[str]) -> None:
        win._refresh_shortcuts()
        fresh = collect_rows(win.paths)
        if dlg_alive():
            _fill(fresh[0])
            btn_orphans.setText(tr("proton_bulk.btn_clean_orphans", count=len(fresh[1])))
            btn_orphans.setVisible(bool(fresh[1]))
            hint.setText(tr("proton_bulk.hint", name=fresh[2] or "GE-Proton"))
        _busy(False)
        if extra:
            parent = dlg if dlg_alive() else _dialog_parent(win)
            if dlg_alive() or not win._closing_pending:
                QMessageBox.information(parent, __app_name__, "\n".join(extra))

    def _steam_write(job_name: str, appids: list[int], tool: str, lines: list[str]) -> None:
        """Steam shortcuts: one write with Steam closed, or the queue in Gaming Mode."""
        paths = win.paths
        if not appids or paths is None:
            _reload(lines)
            return
        if win.should_queue_vdf_edits():
            if job_name == "remove":
                edit = QueuedVdfEdit(f"remove_compat:{','.join(map(str, appids))}", "remove_compat", 0,
                                     tr("proton_bulk.queue_label_orphans"), {"appids": appids})
            else:
                edit = QueuedVdfEdit(f"bulk_migrate_compat:{tool}:{','.join(map(str, appids))}",
                                     "bulk_migrate_compat", 0, tr("proton_bulk.queue_label", tool=tool),
                                     {"appids": appids, "target_tool": tool})
            win.enqueue_vdf_edit(edit)
            _reload([*lines, tr("proton_bulk.queued", count=len(appids))])
            return

        def _write() -> int:
            if job_name == "remove":
                return remove_orphan_shortcut_mappings(paths, appids)
            for appid in appids:
                win._auto_snapshot_if_enabled(
                    paths.compatdata_dir / str(appid) / "pfx", str(appid), "proton-bulk-migrate", "proton",
                    enabled=auto_snap,
                )
            return bulk_migrate_compat_tool(paths.config_vdf, appids, tool)

        auto_snap = win._is_auto_snapshot_enabled()

        def _go() -> None:
            if not dlg_alive() or win._closing_pending:
                _busy(False)
                return
            win._begin_busy()
            worker = SteamOpWorker(_write, need_steam_closed=True)
            win._track_worker(worker)
            worker.status.connect(win.status_label.setText)

            def _ok(count: object) -> None:
                win._end_busy()
                if job_name == "remove":
                    _reload([*lines, tr("proton_bulk.orphans_removed", count=count)])
                else:
                    _reload([*lines, tr("proton_bulk.migrated", count=count, tool=tool)])

            def _err(err: str) -> None:
                win._end_busy()
                _reload([*lines, tr("proton.error", err=err)])

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        win._confirm_close_steam_async(
            _go,
            on_cancelled=lambda: _reload([*lines, tr("proton_bulk.steam_skipped")]),
            parent=dlg,
            alive_fn=dlg_alive,
        )

    def _update(selection: list[ProtonRow]) -> None:
        if not selection:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        _busy(True)
        dirs = [r.game_dir for r in selection if r.game_dir is not None]
        appids = list(dict.fromkeys(appid for r in selection for appid, _ in r.steam_tools))
        worker = _UpdateWorker(dirs)
        win._track_worker(worker)
        worker.status.connect(win.status_label.setText)

        def _done(name: str, changed: int, errors: list[str]) -> None:
            win.status_label.setText(tr("status.ready"))
            if not dlg_alive():
                _busy(False)
                return
            lines = [tr("proton_bulk.games_updated", count=changed, name=name)] if dirs else []
            _steam_write("migrate", appids, name, lines + errors)

        def _fail(err: str) -> None:
            win.status_label.setText(tr("status.ready"))
            _reload([tr("proton.error", err=err)])

        worker.done.connect(_done)
        worker.failed.connect(_fail)
        worker.start()

    def _previous() -> None:
        dirs = [r.game_dir for r in _checked() if r.game_dir is not None and r.has_previous]
        if not dirs:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.no_previous"))
            return
        errors: list[str] = []
        for d in dirs:
            try:
                use_previous_proton(d)
            except (OSError, ValueError) as exc:
                errors.append(f"{d.name}: {exc}")
        _reload([tr("proton_games.changed", count=len(dirs) - len(errors)), *errors])

    def _clean_orphans() -> None:
        fresh = collect_rows(win.paths)[1]
        if not fresh:
            return
        if QMessageBox.question(dlg, __app_name__, tr("proton_bulk.confirm_orphans", count=len(fresh))) \
                != QMessageBox.StandardButton.Yes:
            return
        _busy(True)
        _steam_write("remove", fresh, "", [])

    def _versions() -> None:
        from .proton_dialogs import open_proton_manager_dialog

        open_proton_manager_dialog(win)
        _reload([])

    btn_select_all.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
    btn_select_none.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))
    btn_update.clicked.connect(lambda: _update(_checked()))
    btn_update_all.clicked.connect(lambda: _update(list(current)))
    btn_previous.clicked.connect(_previous)
    btn_orphans.clicked.connect(_clean_orphans)
    btn_versions.clicked.connect(_versions)
    dlg.exec()
    dlg.deleteLater()
