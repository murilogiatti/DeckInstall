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

from . import __app_name__, __version__
from .app_updater import get_running_appimage
from .i18n import tr
from .steam_vdf import CleanupCandidate
from .ui_cards import (
    FlowLayout,
    _cleanup_candidate_auto_check,
    _dialog_parent,
    _format_cleanup_candidate_label,
    _is_wine_game_running,
    _make_card,
    _SortableTableItem,
    format_size_bytes,
)
from .ui_workers_maint import (
    MaintBackupWorker,
    MaintDeleteWorker,
    MaintHealthWorker,
    MaintRevalWorker,
    MaintScanWorker,
)

if TYPE_CHECKING:
    from .ui import MainWindow


_MAINT_FILTER_CATEGORIES: dict[str, frozenset[str] | None] = {
    "all": None,
    "prefixes": frozenset(
        {"Prefixo Externo", "external_prefix", "Compatdata Órfão", "orphan_compatdata"}
    ),
    "backups": frozenset({"Backup", "backup"}),
    "caches": frozenset(
        {"Steam Logs", "Trash", "App Cache", "Orphan Desktop", "Prefix Cache"}
    ),
    "shaders": frozenset({"Orphan Shadercache", "GPU Shader Cache"}),
    "runtimes": frozenset({"Old Proton/Runtime"}),
}


def build_maintenance_page(win: MainWindow) -> QWidget:
    """Build the Maintenance & Cleanup page widget and bind controls onto ``win``."""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)

    win._maint_candidates = []
    win._maint_checked = {}
    win._maint_active_filter = "all"
    win._maint_scanned = False

    title = QLabel(tr("maint.page_title"))
    title.setObjectName("PageTitle")
    sub = QLabel(tr("maint.page_subtitle"))
    sub.setObjectName("MutedText")
    sub.setWordWrap(True)
    layout.addWidget(title)
    layout.addWidget(sub)

    health_card, health_layout = _make_card()
    health_top = QHBoxLayout()
    health_lbl = QLabel(tr("maint.health_title"))
    health_lbl.setObjectName("SectionTitle")
    win.btn_maint_protons = QPushButton(tr("maint.btn_manage_protons"))
    win.btn_maint_protons.clicked.connect(win._open_proton_manager_dialog)
    win.btn_maint_refresh_health = QPushButton(tr("maint.btn_refresh_health"))
    win.btn_maint_refresh_health.clicked.connect(lambda: refresh_maintenance_health(win))
    health_top.addWidget(health_lbl)
    health_top.addStretch()
    health_top.addWidget(win.btn_maint_protons)
    health_top.addWidget(win.btn_maint_refresh_health)
    health_layout.addLayout(health_top)

    win.lbl_maint_app = QLabel()
    win.lbl_maint_umu = QLabel()
    win.lbl_maint_proton = QLabel()
    win.lbl_maint_steamrt4 = QLabel()
    win.lbl_maint_disk = QLabel()
    for lbl in (
        win.lbl_maint_app,
        win.lbl_maint_umu,
        win.lbl_maint_proton,
        win.lbl_maint_steamrt4,
        win.lbl_maint_disk,
    ):
        lbl.setWordWrap(True)
        health_layout.addWidget(lbl)
    health_actions = FlowLayout(spacing=8)
    win.btn_update_runtimes = QPushButton(tr("runtime.btn_update"))
    win.btn_update_runtimes.setToolTip(tr("runtime.tooltip"))
    win.btn_update_runtimes.clicked.connect(win._update_runtimes)
    win.btn_app_update = QPushButton(tr("app_update.btn"))
    win.btn_app_update.clicked.connect(win._check_and_update_app)
    health_actions.addWidget(win.btn_update_runtimes)
    health_actions.addWidget(win.btn_app_update)
    health_layout.addLayout(health_actions)
    layout.addWidget(health_card)

    batch_card, batch_layout = _make_card()
    win.maint_batch_card = batch_card
    batch_layout.addWidget(QLabel(tr("library.menu_batch")))
    for action in (win.act_bulk_migrate, win.act_bulk_proton):
        button = QPushButton(action.text())
        button.setEnabled(action.isEnabled())
        action.changed.connect(lambda a=action, b=button: b.setEnabled(a.isEnabled()))
        button.clicked.connect(lambda _checked=False, a=action: a.trigger())
        batch_layout.addWidget(button)
    layout.addWidget(batch_card)

    clean_card, clean_layout = _make_card()
    clean_top = QHBoxLayout()
    clean_lbl = QLabel(tr("maint.cleanup_title"))
    clean_lbl.setObjectName("SectionTitle")
    win.btn_maint_scan = QPushButton(tr("maint.btn_scan"))
    win.btn_maint_scan.clicked.connect(lambda: scan_maintenance_cleanup(win))
    win.btn_maint_revalidate = QPushButton(tr("cleanup.btn_revalidate"))
    win.btn_maint_revalidate.setEnabled(win.paths is not None)
    win.btn_maint_revalidate.clicked.connect(lambda: revalidate_maintenance_cleanup(win))
    clean_top.addWidget(clean_lbl)
    clean_top.addStretch()
    clean_top.addWidget(win.btn_maint_scan)
    clean_top.addWidget(win.btn_maint_revalidate)
    clean_layout.addLayout(clean_top)

    pills_flow = FlowLayout(spacing=6)
    win._maint_filter_group = QButtonGroup(win)
    win._maint_filter_group.setExclusive(True)
    filter_defs = (
        ("all", tr("maint.filter_all")),
        ("prefixes", tr("maint.filter_prefixes")),
        ("backups", tr("maint.filter_backups")),
        ("caches", tr("maint.filter_caches")),
        ("shaders", tr("maint.filter_shaders")),
        ("runtimes", tr("maint.filter_runtimes")),
    )
    for key, label in filter_defs:
        pill = QPushButton(label)
        pill.setObjectName("FilterPill")
        pill.setCheckable(True)
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
    win.maint_list.horizontalHeader().setSectionsClickable(True)
    win.maint_list.setSortingEnabled(True)
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

    refresh_maintenance_health(win)
    _populate_maint_list(win)
    return page


def open_maintenance_page(win: MainWindow, *, auto_scan: bool = True) -> None:
    """Switch to the Maintenance page and refresh health/cleanup data."""
    win.stack.setCurrentIndex(3)
    if len(win._nav_buttons) > 3:
        win._nav_buttons[3].setChecked(True)
    refresh_maintenance_health(win)
    if auto_scan:
        scan_maintenance_cleanup(win)


def refresh_maintenance_health(win: MainWindow) -> None:
    """Refresh app mode, runtime health, and disk usage labels asynchronously."""
    mode = (
        tr("maint.mode_appimage")
        if get_running_appimage() is not None
        else tr("maint.mode_python")
    )
    win.lbl_maint_app.setText(
        tr("maint.health_app", app=__app_name__, version=__version__, mode=mode)
    )
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
        win.lbl_maint_disk.setText(
            tr(
                "maint.health_disk",
                prefixes=format_size_bytes(data["prefixes_bytes"]),
                runtimes=format_size_bytes(data["runtimes_bytes"]),
                caches=format_size_bytes(data["caches_bytes"]),
            )
        )

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
    """Return cleanup candidates matching the currently active filter pill."""
    allowed_cats = _MAINT_FILTER_CATEGORIES.get(win._maint_active_filter)
    if allowed_cats is None:
        return list(win._maint_candidates)
    return [c for c in win._maint_candidates if c.category in allowed_cats]


def toggle_maint_filter_selection(win: MainWindow) -> None:
    """Toggle selection for all candidates visible in the active filter."""
    visible = _visible_maint_candidates(win)
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
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = win._maint_checked.get(
                cand.path, _cleanup_candidate_auto_check(cand)
            )
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
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
    sel_items = [c for c in visible if win._maint_checked.get(c.path, False)]
    sel_count = len(sel_items)
    sel_size = format_size_bytes(sum(c.size_bytes for c in sel_items))
    safe_items = [
        c for c in win._maint_candidates if _cleanup_candidate_auto_check(c)
    ]
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

    all_checked = bool(visible) and all(
        win._maint_checked.get(c.path, False) for c in visible
    )
    win.btn_maint_toggle_filter.setEnabled(bool(visible))
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


def scan_maintenance_cleanup(win: MainWindow) -> None:
    """Scan for cleanup candidates in the background."""
    win._set_steam_busy(True)
    win.status_label.setText(tr("cleanup.scanning"))

    def _on_found(candidates: list[CleanupCandidate]) -> None:
        win._set_steam_busy(False)
        if win._closing_pending:
            return
        _apply_maint_scan_results(win, candidates)
        win.status_label.setText(
            tr("cleanup.none_found") if not candidates else tr("status.ready")
        )

    def _on_failed(err: str) -> None:
        win._set_steam_busy(False)
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


def revalidate_maintenance_cleanup(win: MainWindow) -> None:
    """Sync external saves and revalidate cleanup candidates."""
    if not win.paths:
        return
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
        return
    win._set_steam_busy(True)
    win.status_label.setText(tr("cleanup.revalidating"))

    def _on_done(
        new_cands: list[CleanupCandidate], checked: int, copied: int, pending: int
    ) -> None:
        win._set_steam_busy(False)
        if win._closing_pending:
            return
        _apply_maint_scan_results(win, new_cands)
        extra = tr("cleanup.reval_pending", pending=pending) if pending else ""
        win.status_label.setText(
            tr("cleanup.reval_done", checked=checked, copied=copied) + extra
        )

    def _on_failed(err: str) -> None:
        win._set_steam_busy(False)
        if win._closing_pending:
            return
        msg = tr("cleanup.reval_error", err=err)
        win.status_label.setText(msg)
        QMessageBox.critical(_dialog_parent(win), __app_name__, msg)

    ui_mod = sys.modules.get("deckinstall.ui")
    load_sc_fn = getattr(ui_mod, "load_shortcuts", None) if ui_mod is not None else None
    worker = MaintRevalWorker(win.paths, load_shortcuts_fn=load_sc_fn)
    win._track_worker(worker)
    worker.done.connect(_on_done)
    worker.failed.connect(_on_failed)
    worker.start()


def maint_clean_safe(win: MainWindow) -> None:
    """Confirm and delete all auto-checked safe cleanup candidates."""
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("cleanup.wine_running"))
        return
    safe_items = [
        c for c in win._maint_candidates if _cleanup_candidate_auto_check(c)
    ]
    safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
    if not safe_items:
        QMessageBox.information(_dialog_parent(win), __app_name__, tr("maint.safe_clean_empty"))
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
        if win._maint_checked.get(c.path, False)
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
    win._set_steam_busy(True)
    win.status_label.setText(tr("cleanup.deleting", count=len(items)))

    def _on_deleted(
        deleted: int,
        freed: int,
        errs: list[str],
        new_cands: list[CleanupCandidate],
        backup_zip_str: str,
    ) -> None:
        win._set_steam_busy(False)
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
            QMessageBox.information(_dialog_parent(win), __app_name__, msg)

    def _on_failed(err: str) -> None:
        win._set_steam_busy(False)
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
        win._set_steam_busy(True)
        win.status_label.setText(tr("maint.backup_running"))

        def _on_backup_done(zip_str: str, files_backed: int) -> None:
            win._set_steam_busy(False)
            win.status_label.setText(tr("status.ready"))
            if win._closing_pending:
                return
            if files_backed > 0:
                if hasattr(win, "_refresh_saves_list"):
                    win._refresh_saves_list()
                QMessageBox.information(
                    _dialog_parent(win),
                    __app_name__,
                    tr("maint.backup_done", path=zip_str, files=files_backed),
                )
            else:
                QMessageBox.information(_dialog_parent(win), __app_name__, tr("maint.backup_empty"))

        def _on_backup_failed(err: str) -> None:
            win._set_steam_busy(False)
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
