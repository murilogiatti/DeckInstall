"""Maintenance page UI construction and cleanup/health handlers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__
from .cleanup import GROUP_ORDER, CleanupCandidate
from .i18n import tr
from .ui_cards import (
    FlowLayout,
    _BusyGuardedDialog,
    _cleanup_candidate_auto_check,
    _dialog_guard,
    _dialog_parent,
    _format_cleanup_candidate_label,
    _is_wine_game_running,
    _make_card,
    _SortableTableItem,
    dialog_footer,
    format_size_bytes,
    section_card,
    section_page,
)
from .ui_runtimes import show_runtime_health
from .ui_workers_maint import (
    MaintBackupWorker,
    MaintDeleteWorker,
    MaintHealthWorker,
    MaintScanWorker,
)

if TYPE_CHECKING:
    from .ui import MainWindow


_GROUP_RANK = {group: idx for idx, group in enumerate(GROUP_ORDER)}


def build_maintenance_page(win: MainWindow) -> QWidget:
    """Build the Maintenance & Cleanup page widget and bind controls onto ``win``."""
    page, layout = section_page()

    win._maint_candidates = []
    win._maint_checked = {}
    win._maint_active_filter = "all"
    win._maint_scanned = False

    clean_card, clean_layout = _make_card()
    clean_top = QHBoxLayout()
    clean_lbl = QLabel(tr("maint.cleanup_title"))
    clean_lbl.setObjectName("SectionTitle")
    win.btn_maint_scan = QPushButton(tr("maint.btn_scan"))
    win.btn_maint_scan.clicked.connect(lambda: scan_maintenance_cleanup(win))
    clean_top.addWidget(clean_lbl)
    clean_top.addStretch()
    clean_top.addWidget(win.btn_maint_scan)
    clean_layout.addLayout(clean_top)
    clean_hint = QLabel(tr("maint.cleanup_hint"))
    clean_hint.setObjectName("MutedText")
    clean_hint.setWordWrap(True)
    clean_layout.addWidget(clean_hint)
    win.lbl_maint_disk = QLabel()  # where the space goes, filled by the runtime health check
    win.lbl_maint_disk.setObjectName("MutedText")
    win.lbl_maint_disk.setWordWrap(True)
    win.lbl_maint_disk.setVisible(False)
    clean_layout.addWidget(win.lbl_maint_disk)

    pills_flow = FlowLayout(spacing=6)
    win._maint_filter_group = QButtonGroup(win)
    win._maint_filter_group.setExclusive(True)
    group_labels = {
        "games": tr("cleanup.group_games"),
        "runtimes": tr("cleanup.group_runtimes"),
        "launchers": tr("cleanup.group_launchers"),
        "system": tr("cleanup.group_system"),
    }
    filter_defs = [("all", tr("maint.filter_all"))] + [(g, group_labels[g]) for g in GROUP_ORDER]
    for key, label in filter_defs:
        pill = QPushButton(label)
        pill.setObjectName("FilterPill")
        pill.setCheckable(True)
        pill.setProperty("cleanupGroup", key)
        if key == "all":
            pill.setChecked(True)
        pill.clicked.connect(lambda _=False, k=key: on_maint_filter_changed(win, k))
        win._maint_filter_group.addButton(pill)
        pills_flow.addWidget(pill)
    filter_row = QHBoxLayout()
    filter_row.setSpacing(8)
    filter_row.addLayout(pills_flow, 1)
    win.btn_maint_toggle_filter = QPushButton(tr("maint.btn_select_all_filter"))
    win.btn_maint_toggle_filter.clicked.connect(lambda: toggle_maint_filter_selection(win))
    filter_row.addWidget(win.btn_maint_toggle_filter, 0, Qt.AlignmentFlag.AlignTop)
    clean_layout.addLayout(filter_row)

    win.maint_list = QTableWidget(0, 3)
    win.maint_list.setHorizontalHeaderLabels(
        [tr("maint.column_item"), tr("maint.column_folder"), tr("maint.column_size")]
    )
    win.maint_list.verticalHeader().setVisible(False)
    win.maint_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    win.maint_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    win.maint_list.horizontalHeader().setSectionResizeMode(
        0, QHeaderView.ResizeMode.ResizeToContents
    )
    win.maint_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    win.maint_list.horizontalHeader().setSectionResizeMode(
        2, QHeaderView.ResizeMode.ResizeToContents
    )
    win.maint_list.setSortingEnabled(True)
    win.maint_list.sortItems(2, Qt.SortOrder.DescendingOrder)
    win.maint_list.setMinimumHeight(220)
    win.maint_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    win.maint_list.customContextMenuRequested.connect(
        lambda pos: show_maint_context_menu(win, pos)
    )
    win.maint_list.itemChanged.connect(lambda item: on_maint_item_changed(win, item))
    clean_layout.addWidget(win.maint_list, 1)

    footer = QHBoxLayout()
    win.lbl_maint_summary = QLabel()
    win.lbl_maint_summary.setWordWrap(True)
    win.btn_maint_clean_safe = QPushButton()
    win.btn_maint_clean_safe.setObjectName("primaryBtn")
    win.btn_maint_clean_safe.clicked.connect(lambda: maint_clean_safe(win))
    win.btn_maint_delete_selected = QPushButton()
    win.btn_maint_delete_selected.setObjectName("dangerBtn")
    win.btn_maint_delete_selected.clicked.connect(lambda: maint_delete_selected(win))
    footer.addWidget(win.lbl_maint_summary, 1)
    footer.addWidget(win.btn_maint_clean_safe)
    footer.addWidget(win.btn_maint_delete_selected)
    clean_layout.addLayout(footer)
    layout.addWidget(clean_card, 1)

    # Last and apart: undo everything DeckInstall changed on this PC.
    remove_card, remove_layout, _top = section_card(tr("remove_all.card_title"), tr("remove_all.open_tip"))
    btn_remove_all = QPushButton(tr("remove_all.open_btn"))
    btn_remove_all.setObjectName("dangerBtn")
    btn_remove_all.clicked.connect(win._remove_everything)
    remove_layout.addWidget(btn_remove_all, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(remove_card)

    _populate_maint_list(win)
    return page




def refresh_maintenance_health(win: MainWindow) -> None:
    """Refresh the runtime health (Runtimes tab) and the disk usage line asynchronously."""
    win._maint_health_req_id += 1
    req_id = win._maint_health_req_id
    paths_snapshot = win.paths
    caches_bytes = sum(c.size_bytes for c in win._maint_candidates)

    def _on_health_ready(r_id: int, data: dict) -> None:
        if win._closing_pending or r_id != win._maint_health_req_id:
            return
        win.lbl_maint_umu.setText(tr("maint.health_umu", status=data["umu_status"]))
        win.lbl_maint_proton.setText(
            tr("maint.health_proton", status=data["proton_status"])
        )
        win.lbl_maint_steamrt4.setText(
            tr("maint.health_steamrt4", status=data["rt4_status"])
        )
        win.lbl_maint_mangohud.setText(tr("maint.health_mangohud", status=data.get("mangohud_status", "-")))
        show_runtime_health(win, data)
        win.lbl_maint_disk.setText(
            tr(
                "maint.health_disk",
                prefixes=format_size_bytes(data["prefixes_bytes"]),
                runtimes=format_size_bytes(data["runtimes_bytes"]),
            )
        )
        win.lbl_maint_disk.setVisible(True)

    ui_mod = sys.modules.get("deckinstall.ui")
    find_steam_fn = getattr(ui_mod, "find_steam_root", None) if ui_mod is not None else None
    worker = MaintHealthWorker(
        req_id,
        paths_snapshot,
        caches_bytes,
        find_steam_root_fn=find_steam_fn,
    )
    win._track_worker(worker)
    worker.ready.connect(_on_health_ready)
    worker.failed.connect(lambda _rid, err: win._on_background_failed(err))
    worker.start()


def on_maint_filter_changed(win: MainWindow, key: str) -> None:
    """Update active maintenance filter and repopulate the candidate list."""
    win._maint_active_filter = key
    _populate_maint_list(win)


def on_maint_item_changed(win: MainWindow, item: QTableWidgetItem) -> None:
    """Sync check-state changes for a cleanup candidate item."""
    cand = item.data(Qt.ItemDataRole.UserRole)
    if isinstance(cand, CleanupCandidate):
        win._maint_checked[cand.path] = item.checkState() == Qt.CheckState.Checked
        _update_maint_summary(win)


def _visible_maint_candidates(win: MainWindow) -> list[CleanupCandidate]:
    """Return candidates of the active group pill ("all" = every group), largest first."""
    key = win._maint_active_filter
    cands = [c for c in win._maint_candidates if key == "all" or c.group == key]
    return sorted(cands, key=lambda c: (-c.size_bytes, _GROUP_RANK.get(c.group, len(GROUP_ORDER)), str(c.path)))


def toggle_maint_filter_selection(win: MainWindow) -> None:
    """Toggle selection for all candidates visible in the active filter."""
    visible = [cand for cand in _visible_maint_candidates(win) if not cand.review_only]
    if not visible:
        return
    all_checked = all(win._maint_checked.get(c.path, False) for c in visible)
    new_state = not all_checked
    for cand in visible:
        win._maint_checked[cand.path] = new_state
    _populate_maint_list(win)


def _populate_maint_list(win: MainWindow) -> None:
    visible = _visible_maint_candidates(win)
    win.maint_list.blockSignals(True)
    win.maint_list.setSortingEnabled(False)
    win.maint_list.clearSpans()
    win.maint_list.setRowCount(0)
    if not visible:
        if not win._maint_scanned:
            msg = tr("maint.empty_prompt")
        elif not win._maint_candidates:
            msg = tr("cleanup.none_found")
        else:
            msg = tr("maint.filter_empty")
        win.maint_list.setRowCount(1)
        win.maint_list.setSpan(0, 0, 1, 3)
        placeholder = QTableWidgetItem(msg)
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        win.maint_list.setItem(0, 0, placeholder)
    else:
        for row, cand in enumerate(visible):
            win.maint_list.insertRow(row)
            item = QTableWidgetItem(_format_cleanup_candidate_label(cand))
            item.setToolTip(str(cand.path))
            if not cand.review_only:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = win._maint_checked.get(
                cand.path, _cleanup_candidate_auto_check(cand)
            )
            if not cand.review_only:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, cand)
            win.maint_list.setItem(row, 0, item)
            win.maint_list.setItem(row, 1, QTableWidgetItem(str(cand.path)))
            size_item = _SortableTableItem(
                format_size_bytes(cand.size_bytes), cand.size_bytes
            )
            win.maint_list.setItem(row, 2, size_item)
    win.maint_list.setSortingEnabled(True)
    win.maint_list.blockSignals(False)
    _update_maint_summary(win)


def _update_maint_summary(win: MainWindow) -> None:
    visible = _visible_maint_candidates(win)
    sel_items = [c for c in visible if not c.review_only and win._maint_checked.get(c.path, False)]
    sel_count = len(sel_items)
    sel_size = format_size_bytes(sum(c.size_bytes for c in sel_items))
    safe_items = [c for c in visible if _cleanup_candidate_auto_check(c)]
    safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
    win.lbl_maint_summary.setText(
        tr("maint.summary", sel_size=sel_size, sel_count=sel_count, safe_size=safe_size)
    )
    win.btn_maint_clean_safe.setText(tr("maint.btn_clean_safe", size=safe_size))
    win.btn_maint_delete_selected.setText(
        tr("maint.btn_delete_selected", size=sel_size)
    )
    win.btn_maint_clean_safe.setEnabled(len(safe_items) > 0)
    win.btn_maint_delete_selected.setEnabled(sel_count > 0)

    selectable = [c for c in visible if not c.review_only]
    all_checked = bool(selectable) and all(win._maint_checked.get(c.path, False) for c in selectable)
    win.btn_maint_toggle_filter.setEnabled(bool(selectable))
    win.btn_maint_toggle_filter.setText(
        tr("maint.btn_deselect_all_filter")
        if all_checked
        else tr("maint.btn_select_all_filter")
    )


def _apply_maint_scan_results(
    win: MainWindow, candidates: list[CleanupCandidate]
) -> None:
    win._maint_scanned = True
    win._maint_candidates = candidates
    win._maint_checked = {
        c.path: _cleanup_candidate_auto_check(c) for c in candidates
    }
    _populate_maint_list(win)
    refresh_maintenance_health(win)


def offer_maintenance_review(
    win: MainWindow, group: str, parent: QWidget | None = None, *, details: str = "", appids: set[int] | None = None,
) -> bool:
    if win._closing_pending:
        return False
    box = QMessageBox(parent or _dialog_parent(win))
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(__app_name__)
    box.setText(tr("cleanup.offer_review"))
    box.setDetailedText(details)
    review = box.addButton(tr("cleanup.review"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is review:
        from .ui import PAGE_MAINT

        win._show_page(PAGE_MAINT)
        on_maint_filter_changed(win, group)
        for button in win._maint_filter_group.buttons():
            if button.property("cleanupGroup") == group:
                button.setChecked(True)
        scan_maintenance_cleanup(win, appids=appids)
        return True
    return False


def scan_maintenance_cleanup(win: MainWindow, *, appids: set[int] | None = None) -> None:
    """Scan for cleanup candidates in the background."""
    win._begin_busy()
    win.status_label.setText(tr("cleanup.scanning"))

    def _on_found(candidates: list[CleanupCandidate], copied: int) -> None:
        win._end_busy()
        if win._closing_pending:
            return
        if appids:
            names = {str(appid) for appid in appids}
            candidates = [
                item for item in candidates
                if item.category in ("Compatdata Órfão", "Orphan Shadercache", "Backup")
                and item.path.name.split(".", 1)[0] in names
            ]
        _apply_maint_scan_results(win, candidates)
        win.status_label.setText(
            tr("cleanup.saves_copied", copied=copied) if copied
            else tr("cleanup.none_found") if not candidates else tr("status.ready")
        )

    def _on_failed(err: str) -> None:
        win._end_busy()
        if win._closing_pending:
            return
        msg = tr("cleanup.scan_error", err=err)
        win.status_label.setText(msg)
        QMessageBox.critical(_dialog_parent(win), __app_name__, msg)

    worker = MaintScanWorker(win.paths)
    win._track_worker(worker)
    worker.found.connect(_on_found)
    worker.failed.connect(_on_failed)
    worker.start()


def maint_clean_safe(win: MainWindow) -> None:
    """Confirm and delete all auto-checked safe cleanup candidates."""
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
        return
    safe_items = [c for c in _visible_maint_candidates(win) if _cleanup_candidate_auto_check(c)]
    safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
    if not safe_items:
        win.notify(tr("maint.safe_clean_empty"), "info")
        return
    ans = QMessageBox.question(
        _dialog_parent(win),
        __app_name__,
        tr("maint.confirm_safe_clean", count=len(safe_items), size=safe_size),
    )
    if ans == QMessageBox.StandardButton.Yes:
        _run_maint_delete(win, safe_items)


def maint_delete_selected(win: MainWindow) -> None:
    """Confirm and delete currently checked cleanup candidates."""
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
        return
    selected = [
        c
        for c in _visible_maint_candidates(win)
        if not c.review_only and win._maint_checked.get(c.path, False)
    ]
    if not selected:
        return
    unverified = [c for c in selected if c.has_saves and not c.saves_verified_migrated]
    if unverified:
        preview_lines = "\n".join(f"• {c.label}" for c in unverified[:5])
        if len(unverified) > 5:
            preview_lines += f"\n• ... (+{len(unverified) - 5})"
        box = QMessageBox(win)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(__app_name__)
        box.setText(
            tr(
                "maint.confirm_delete_unverified",
                count=len(unverified),
                items=preview_lines,
            )
        )
        btn_backup_del = box.addButton(
            tr("maint.btn_backup_and_delete"), QMessageBox.ButtonRole.AcceptRole
        )
        btn_del_anyway = box.addButton(
            tr("maint.btn_delete_anyway"), QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_backup_del)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_backup_del:
            _run_maint_delete(
                win, selected, allow_unverified_saves=True, backup_unverified=True
            )
        elif clicked is btn_del_anyway:
            _run_maint_delete(
                win, selected, allow_unverified_saves=True, backup_unverified=False
            )
        return

    sel_size = format_size_bytes(sum(c.size_bytes for c in selected))
    ans = QMessageBox.question(
        _dialog_parent(win),
        __app_name__,
        tr("maint.confirm_delete_selected", count=len(selected), size=sel_size),
    )
    if ans == QMessageBox.StandardButton.Yes:
        _run_maint_delete(win, selected)


def _run_maint_delete(
    win: MainWindow,
    items: list[CleanupCandidate],
    *,
    allow_unverified_saves: bool = False,
    backup_unverified: bool = False,
) -> None:
    win._begin_busy()
    win.status_label.setText(tr("cleanup.deleting", count=len(items)))

    def _on_deleted(
        deleted: int,
        freed: int,
        errs: list[str],
        new_cands: list[CleanupCandidate],
        backup_zip_str: str,
    ) -> None:
        win._end_busy()
        if win._closing_pending:
            return
        _apply_maint_scan_results(win, new_cands)
        msg = tr("cleanup.deleted", deleted=deleted, size=format_size_bytes(freed))
        if backup_zip_str:
            msg += "\n" + tr("maint.backup_created_note", path=backup_zip_str)
        if errs:
            msg += tr("cleanup.delete_warnings", errs="\n".join(errs[:10]))
        win.status_label.setText(msg.splitlines()[0])
        if errs:
            QMessageBox.warning(_dialog_parent(win), __app_name__, msg)
        else:
            win.notify(msg, "success")

    def _on_failed(err: str) -> None:
        win._end_busy()
        if win._closing_pending:
            return
        msg = tr("cleanup.delete_error", err=err)
        win.status_label.setText(msg)
        QMessageBox.critical(_dialog_parent(win), __app_name__, msg)

    worker = MaintDeleteWorker(
        win.paths, items, allow_unverified_saves, backup_unverified
    )
    win._track_worker(worker)
    worker.done.connect(_on_deleted)
    worker.failed.connect(_on_failed)
    worker.start()


def show_maint_context_menu(win: MainWindow, pos: QPoint) -> None:
    """Show the context menu for a maintenance cleanup item."""
    item = win.maint_list.itemAt(pos)
    if item is None:
        return
    item = win.maint_list.item(item.row(), 0)
    if item is None:
        return
    cand = item.data(Qt.ItemDataRole.UserRole)
    if not isinstance(cand, CleanupCandidate):
        return

    menu = QMenu(win)
    act_open = menu.addAction(tr("maint.ctx_open_folder"))
    act_backup: QAction | None = None
    if cand.path.is_dir() and cand.category in (
        "Prefixo Externo",
        "Orphan Game Prefix",
        "external_prefix",
        "Compatdata Órfão",
        "orphan_compatdata",
    ):
        act_backup = menu.addAction(tr("maint.ctx_backup_saves"))

    chosen = menu.exec(win.maint_list.viewport().mapToGlobal(pos))
    if chosen is None:
        return
    if chosen is act_open:
        target = cand.path if cand.path.is_dir() else cand.path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
    elif act_backup is not None and chosen is act_backup:
        win._begin_busy()
        win.status_label.setText(tr("maint.backup_running"))

        def _on_backup_done(zip_str: str, files_backed: int) -> None:
            win._end_busy()
            win.status_label.setText(tr("status.ready"))
            if win._closing_pending:
                return
            if files_backed > 0:
                if hasattr(win, "_refresh_saves_list"):
                    win._refresh_saves_list()
                win.notify(tr("maint.backup_done", path=zip_str, files=files_backed), "success")
            else:
                win.notify(tr("maint.backup_empty"), "info")

        def _on_backup_failed(err: str) -> None:
            win._end_busy()
            win.status_label.setText(tr("status.ready"))
            if win._closing_pending:
                return
            QMessageBox.critical(
                _dialog_parent(win), __app_name__, tr("maint.backup_error", err=err)
            )

        worker = MaintBackupWorker(cand)
        win._track_worker(worker)
        worker.done.connect(_on_backup_done)
        worker.failed.connect(_on_backup_failed)
        worker.start()


def open_uninstall_runtimes_dialog(win: MainWindow, kinds: set[str] | None = None) -> None:
    """Check the runtimes to remove (one, some or all); sizes and Proton users are shown.

    kinds: only list these runtime kinds (a Runtimes card's "Remove"); None lists everything.
    """
    from .runtime_uninstall import OPTISCALER, PROTON, RuntimeItem, affected_steam_appids, list_runtimes, uninstall
    from .steam_paths import _read_compat_tool_id
    from .steam_vdf import bulk_migrate_compat_tool
    from .ui_workers import SteamOpWorker
    from .vdf_queue import QueuedVdfEdit

    if win._steam_busy:
        return
    win._begin_busy()
    win.status_label.setText(tr("runtime.uninstall_loading"))
    loader = SteamOpWorker(lambda: list_runtimes(win.paths))
    win._track_worker(loader)

    def _show(res: object) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        items = [i for i in res if isinstance(i, RuntimeItem)] if isinstance(res, list) else []
        if kinds is not None:
            items = [i for i in items if i.kind in kinds]
        if win._closing_pending:
            return
        if not items:
            win.notify(tr("runtime.uninstall_none"), "info")
            return
        op_state = {"busy": False}
        dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: op_state["busy"])
        dlg.setWindowTitle(tr("runtime.uninstall_title"))
        dlg.resize(760, 420)
        dlg_alive = _dialog_guard(dlg)
        lay = QVBoxLayout(dlg)
        hint = QLabel(tr("runtime.uninstall_hint"))
        hint.setWordWrap(True)
        lay.addWidget(hint)
        tbl = QTableWidget(0, 3)
        tbl.setHorizontalHeaderLabels([tr("table.name"), tr("table.size"), tr("runtime.uninstall_col_users")])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for item in items:
            row = tbl.rowCount()
            tbl.insertRow(row)
            it = QTableWidgetItem(item.name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, item)
            it.setToolTip(str(item.path))
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, _SortableTableItem(format_size_bytes(item.size_bytes), item.size_bytes))
            users = ", ".join(item.users) if item.users else ("-" if item.kind == PROTON else "")
            tbl.setItem(row, 2, QTableWidgetItem(users))
        tbl.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        tbl.setSortingEnabled(True)
        lay.addWidget(tbl, 1)
        btn_all = QPushButton(tr("optiscaler_bulk.btn_select_all"))
        btn_none = QPushButton(tr("optiscaler_bulk.btn_select_none"))
        btn_remove = QPushButton(tr("runtime.uninstall_btn_selected"))
        btn_remove.setObjectName("dangerBtn")
        btn_close = QPushButton(tr("proton_bulk.btn_close"))
        lay.addLayout(dialog_footer(btn_remove, btn_close, left=(btn_all, btn_none)))

        def _set_all(state: Qt.CheckState) -> None:
            for r in range(tbl.rowCount()):
                cell = tbl.item(r, 0)
                if cell is not None:
                    cell.setCheckState(state)

        def _remove() -> None:
            chosen = [
                cell.data(Qt.ItemDataRole.UserRole) for r in range(tbl.rowCount())
                if (cell := tbl.item(r, 0)) is not None and cell.checkState() == Qt.CheckState.Checked
            ]
            if not chosen:
                QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
                return
            if _is_wine_game_running():
                QMessageBox.warning(dlg, __app_name__, tr("runtime.uninstall_game_running"))
                return
            listing = "\n".join(
                f"• {i.name} ({format_size_bytes(i.size_bytes)})"
                + (f" - {tr('runtime.uninstall_used_by', games=', '.join(i.users))}" if i.users else "")
                for i in chosen
            )
            if QMessageBox.question(
                dlg, __app_name__, tr("runtime.uninstall_confirm", count=len(chosen), items=listing)
            ) != QMessageBox.StandardButton.Yes:
                return
            op_state["busy"] = True
            for b in (btn_all, btn_none, btn_remove, btn_close):
                b.setEnabled(False)
            win._begin_busy()

            paths_at_start = win.paths
            queued_now = win.should_queue_vdf_edits()

            def _uninstall() -> tuple[list[str], list[str], list[int]]:
                if _is_wine_game_running():
                    raise RuntimeError(tr("runtime.uninstall_game_running"))
                to_remove = []
                blocked = []
                affected_by_name: dict[str, list[int]] = {}
                for item in chosen:
                    if item.kind == PROTON:
                        tool_id = _read_compat_tool_id(item.path / "compatibilitytool.vdf", item.name)
                        ids = affected_steam_appids(paths_at_start, {item.name, tool_id})
                        if 0 in ids or (queued_now and ids):
                            blocked.append(f"{item.name}: {tr('proton_mgr.remove_requires_review')}")
                            continue
                        affected_by_name[item.name] = ids
                    to_remove.append(item)
                removed_names, errs = uninstall(to_remove)
                affected = sorted({appid for name in removed_names for appid in affected_by_name.get(name, ())})
                return removed_names, blocked + errs, affected

            worker = SteamOpWorker(_uninstall)
            win._track_worker(worker)

            def _done(result: object) -> None:
                op_state["busy"] = False
                win._end_busy()
                removed, errors, affected = (
                    result if isinstance(result, tuple) and len(result) == 3 else ([], [], [])
                )
                msg = tr("runtime.uninstall_done", count=len(removed))
                if errors:
                    msg += "\n\n" + "\n".join(errors)
                win.status_label.setText(tr("runtime.uninstall_done", count=len(removed)))
                refresh_maintenance_health(win)
                win._refresh_shortcuts()

                def _cleanup_steam_mappings() -> None:
                    paths = win.paths
                    if not affected or paths is None or not removed or win._closing_pending:
                        return
                    msg_parent = win
                    if win.should_queue_vdf_edits():
                        edit = QueuedVdfEdit(
                            f"bulk_migrate_compat::{','.join(map(str, affected))}",
                            "bulk_migrate_compat",
                            0,
                            tr("proton_bulk.queue_label", tool=tr("proton.steam_default_label")),
                            {"appids": affected, "target_tool": ""},
                        )
                        if not win.enqueue_vdf_edit(edit):
                            return
                        QMessageBox.information(
                            msg_parent, __app_name__, tr("runtime.uninstall_steam_queued", count=len(affected))
                        )
                        return
                    if (
                        QMessageBox.question(
                            msg_parent,
                            __app_name__,
                            tr("runtime.uninstall_clean_steam", count=len(affected)),
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.Yes,
                        )
                        != QMessageBox.StandardButton.Yes
                    ):
                        return

                    def _go_clean() -> None:
                        if win._closing_pending:
                            return
                        win._begin_busy()
                        cleaner = SteamOpWorker(
                            lambda: bulk_migrate_compat_tool(paths.config_vdf, affected, ""),
                            need_steam_closed=True,
                        )
                        win._track_worker(cleaner)

                        def _cleaned(cnt: object) -> None:
                            win._end_busy()
                            win._refresh_shortcuts()
                            if not win._closing_pending:
                                win.notify(
                                    tr("runtime.uninstall_steam_cleaned", count=cnt if isinstance(cnt, int) else 0),
                                    "success",
                                )

                        def _clean_err(cerr: str) -> None:
                            win._end_busy()
                            if not win._closing_pending:
                                QMessageBox.critical(_dialog_parent(win), __app_name__, tr("runtime.error", err=cerr))

                        cleaner.finished_ok.connect(_cleaned)
                        cleaner.failed.connect(_clean_err)
                        cleaner.start()

                    win._confirm_close_steam_async(
                        _go_clean,
                        parent=msg_parent,
                        alive_fn=lambda: not win._closing_pending,
                    )

                def _review() -> None:
                    if not removed or win._closing_pending:
                        return
                    if any(item.kind == OPTISCALER and item.name in removed for item in chosen):
                        if QMessageBox.question(win, __app_name__, tr("runtime.offer_optiscaler_cleanup")) == (
                            QMessageBox.StandardButton.Yes
                        ):
                            from .ui_dialogs.optiscaler_bulk_dialog import bulk_optiscaler_dialog

                            bulk_optiscaler_dialog(win, cleanup_only=True)
                            return
                    remaining = [
                        str(item.path) for item in chosen
                        if item.path.exists() or item.path.is_symlink()
                    ]
                    offer_maintenance_review(win, "runtimes", details="\n".join(remaining))

                if not dlg_alive():
                    if not win._closing_pending:
                        QMessageBox.information(_dialog_parent(win), __app_name__, msg)
                        _cleanup_steam_mappings()
                        _review()
                    return
                for row in reversed(range(tbl.rowCount())):
                    cell = tbl.item(row, 0)
                    if cell is not None and cell.data(Qt.ItemDataRole.UserRole).name in removed:
                        tbl.removeRow(row)
                for b in (btn_all, btn_none, btn_remove, btn_close):
                    b.setEnabled(True)
                QMessageBox.information(dlg, __app_name__, msg)
                if not errors:
                    dlg.accept()
                _cleanup_steam_mappings()
                _review()

            def _fail(err: str) -> None:
                op_state["busy"] = False
                win._end_busy()
                if not dlg_alive():
                    if not win._closing_pending:
                        QMessageBox.critical(_dialog_parent(win), __app_name__, tr("runtime.error", err=err))
                    return
                for b in (btn_all, btn_none, btn_remove, btn_close):
                    b.setEnabled(True)
                QMessageBox.critical(dlg, __app_name__, tr("runtime.error", err=err))

            worker.finished_ok.connect(_done)
            worker.failed.connect(_fail)
            worker.start()

        btn_all.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
        btn_none.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))
        btn_remove.clicked.connect(_remove)
        btn_close.clicked.connect(dlg.reject)
        dlg.exec()
        dlg.deleteLater()

    def _err(err: str) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("runtime.error", err=err))

    loader.finished_ok.connect(_show)
    loader.failed.connect(_err)
    loader.start()
