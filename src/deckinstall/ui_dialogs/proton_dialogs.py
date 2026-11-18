"""Proton bulk migration and Proton version manager dialogs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from .. import __app_name__
from ..i18n import tr
from ..runtime_manager import (
    PROTON_FAMILIES,
    InstalledCompatTool,
    ProtonRelease,
    fetch_available_proton_releases,
    install_compat_tool,
    list_installed_compat_tools,
    remove_compat_tool,
)
from ..standalone import list_standalone_games, resolve_standalone_proton_fallback
from ..steam_paths import SteamPaths, list_installed_protons
from ..steam_vdf import (
    CompatMappingEntry,
    bulk_migrate_compat_tool,
    list_compat_tool_usage,
    remove_compat_tools,
)
from ..ui_cards import _dialog_parent, _format_worker_error, _SortableTableItem, format_size_bytes
from ..ui_workers import SteamOpWorker

if TYPE_CHECKING:
    from ..ui import MainWindow


def _selected_table_data(tbl: QTableWidget) -> object:
    """Return UserRole data from column 0 of the selected row, or None."""
    row = tbl.currentRow()
    if row < 0:
        return None
    item = tbl.item(row, 0)
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


def _manager_tool_usage(paths: SteamPaths | None) -> list[CompatMappingEntry]:
    usage = list_compat_tool_usage(paths) if paths is not None else []
    for game in list_standalone_games():
        if game.native_linux:
            continue
        resolved, _ = resolve_standalone_proton_fallback(game)
        tool_name = resolved.name if resolved is not None else game.proton_name
        if tool_name:
            usage.append(CompatMappingEntry(0, game.name, "Standalone", tool_name))
    return usage


def bulk_migrate_proton_dialog(win: MainWindow) -> None:
    """Open bulk Proton migration and orphan compat-tool cleanup dialog."""
    if not win.paths:
        return
    paths = win.paths
    win._set_steam_busy(True)

    class _BulkMigrateLoadWorker(QThread):
        ready = Signal(list, list)
        failed = Signal(str)

        def run(self) -> None:
            try:
                usage = list_compat_tool_usage(paths)
                protons = list_installed_protons(paths.steam_root) if usage else []
                self.ready.emit(usage, protons)
            except Exception as exc:
                self.failed.emit(_format_worker_error(exc))

    def _on_load_err(err: str) -> None:
        win._set_steam_busy(False)
        if not win._closing_pending:
            QMessageBox.critical(win, __app_name__, tr("proton.error", err=err))

    def _on_loaded(
        usage: list[CompatMappingEntry],
        protons: list[tuple[str, str]],
    ) -> None:
        win._set_steam_busy(False)
        if win._closing_pending:
            return
        if not usage:
            QMessageBox.information(win, __app_name__, tr("proton_bulk.empty"))
            return
        if not protons:
            QMessageBox.warning(win, __app_name__, tr("proton.none_found"))
            return

        dlg = QDialog(_dialog_parent(win))
        dlg.setWindowTitle(tr("proton_bulk.title"))
        dlg.resize(780, 560)
        lay = QVBoxLayout(dlg)

        empty_tool_label = tr("proton_bulk.empty_tool")
        by_tool: dict[str, list[CompatMappingEntry]] = {}
        for u in usage:
            by_tool.setdefault(u.tool_name or empty_tool_label, []).append(u)
        summary_lines = [
            tr(
                "proton_bulk.summary_line",
                tool=tool,
                active=sum(1 for x in items if x.source != "Orphan"),
                orphans=sum(1 for x in items if x.source == "Orphan"),
            )
            for tool, items in sorted(by_tool.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]
        summary_box = QTextEdit()
        summary_box.setReadOnly(True)
        summary_box.setMaximumHeight(110)
        summary_box.setPlainText("\n".join(summary_lines))
        lay.addWidget(QLabel(tr("proton_bulk.summary_label")))
        lay.addWidget(summary_box)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(tr("proton_bulk.filter_label")))
        filter_combo = QComboBox()
        filter_combo.addItem(tr("proton_bulk.filter_all"), "__ALL__")
        filter_combo.addItem(tr("proton_bulk.filter_orphans"), "__ORPHAN__")
        for tool in sorted(by_tool.keys()):
            filter_combo.addItem(f"{tool} ({len(by_tool[tool])})", tool)
        filter_row.addWidget(filter_combo, 1)

        filter_row.addWidget(QLabel(tr("proton_bulk.target_label")))
        target_combo = QComboBox()
        for tid, disp in protons:
            target_combo.addItem(disp, tid)
        filter_row.addWidget(target_combo, 1)
        lay.addLayout(filter_row)

        lst = QTableWidget(0, 3)
        lst.setHorizontalHeaderLabels(
            [tr("table.name"), tr("table.source"), tr("proton_mgr.column_version")]
        )
        lst.verticalHeader().setVisible(False)
        lst.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        lst.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        lst.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lst.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        lst.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        lst.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lst.horizontalHeader().setStretchLastSection(True)
        lst.horizontalHeader().setSectionsClickable(True)
        lst.setSortingEnabled(True)
        lay.addWidget(lst, 1)

        def _populate() -> None:
            flt = filter_combo.currentData()
            lst.setSortingEnabled(False)
            lst.setRowCount(0)
            for u in usage:
                tkey = u.tool_name or empty_tool_label
                if flt == "__ORPHAN__" and u.source != "Orphan":
                    continue
                if flt not in ("__ALL__", "__ORPHAN__") and tkey != flt:
                    continue
                row = lst.rowCount()
                lst.insertRow(row)
                it = QTableWidgetItem(f"{u.name} ({u.appid_u32})")
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                auto_check = flt != "__ALL__"
                it.setCheckState(Qt.CheckState.Checked if auto_check else Qt.CheckState.Unchecked)
                it.setData(Qt.ItemDataRole.UserRole, u)
                lst.setItem(row, 0, it)
                lst.setItem(row, 1, QTableWidgetItem(u.source))
                lst.setItem(row, 2, QTableWidgetItem(tkey))
            lst.setSortingEnabled(True)

        filter_combo.currentIndexChanged.connect(_populate)
        _populate()

        btn_row = QHBoxLayout()
        btn_migrate = QPushButton(tr("proton_bulk.btn_migrate"))
        btn_migrate.setObjectName("primaryBtn")
        btn_clean_orphans = QPushButton(tr("proton_bulk.btn_clean_orphans"))
        btn_close = QPushButton(tr("proton_bulk.btn_close"))
        btn_row.addWidget(btn_migrate)
        btn_row.addWidget(btn_clean_orphans)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
        btn_close.clicked.connect(dlg.reject)

        action_mode = {"kind": ""}

        def _selected_entries() -> list[CompatMappingEntry]:
            out: list[CompatMappingEntry] = []
            for i in range(lst.rowCount()):
                it = lst.item(i, 0)
                if it is not None and it.checkState() == Qt.CheckState.Checked:
                    data = it.data(Qt.ItemDataRole.UserRole)
                    if isinstance(data, CompatMappingEntry):
                        out.append(data)
            return out

        def _on_migrate() -> None:
            if not _selected_entries():
                QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
                return
            action_mode["kind"] = "migrate"
            dlg.accept()

        def _on_clean_orphans() -> None:
            orphans = [u for u in _selected_entries() if u.source == "Orphan"]
            if not orphans:
                QMessageBox.information(dlg, __app_name__, tr("proton_bulk.no_orphans_selected"))
                return
            action_mode["kind"] = "clean_orphans"
            dlg.accept()

        btn_migrate.clicked.connect(_on_migrate)
        btn_clean_orphans.clicked.connect(_on_clean_orphans)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = _selected_entries()
        if not selected:
            return

        auto_snap_enabled = win._is_auto_snapshot_enabled()
        mode_kind = action_mode["kind"]
        target_tool = str(target_combo.currentData())

        def _proceed_bulk_action() -> None:
            if win._closing_pending:
                return
            win._set_steam_busy(True)
            if mode_kind == "clean_orphans":
                orphan_ids = [u.appid_u32 for u in selected if u.source == "Orphan"]
                worker = SteamOpWorker(
                    lambda: remove_compat_tools(paths.config_vdf, orphan_ids),
                    need_steam_closed=True,
                )
                win._track_worker(worker)

                def _ok_clean(count: object) -> None:
                    win._set_steam_busy(False)
                    msg = tr("proton_bulk.orphans_removed", count=count)
                    win.status_label.setText(msg)
                    if not win._closing_pending:
                        QMessageBox.information(win, __app_name__, msg)

                worker.finished_ok.connect(_ok_clean)
            else:
                ids = [u.appid_u32 for u in selected]

                def _do_bulk_proton() -> int:
                    for u in selected:
                        win._auto_snapshot_if_enabled(
                            paths.compatdata_dir / str(u.appid_u32) / "pfx",
                            u.name or str(u.appid_u32),
                            "proton-bulk-migrate",
                            "proton",
                            enabled=auto_snap_enabled,
                        )
                    return bulk_migrate_compat_tool(paths.config_vdf, ids, target_tool)

                worker = SteamOpWorker(_do_bulk_proton, need_steam_closed=True)
                win._track_worker(worker)

                def _ok_mig(count: object) -> None:
                    win._set_steam_busy(False)
                    msg = tr("proton_bulk.migrated", count=count, tool=target_tool)
                    win.status_label.setText(msg)
                    if not win._closing_pending:
                        QMessageBox.information(win, __app_name__, msg)

                worker.finished_ok.connect(_ok_mig)

            def _on_bulk_proton_err(err: str) -> None:
                win._set_steam_busy(False)
                if not win._closing_pending:
                    QMessageBox.critical(win, __app_name__, tr("proton.error", err=err))

            worker.status.connect(win.status_label.setText)
            worker.failed.connect(_on_bulk_proton_err)
            worker.start()

        win._confirm_close_steam_async(_proceed_bulk_action)

    load_worker = _BulkMigrateLoadWorker()
    win._track_worker(load_worker)
    load_worker.ready.connect(_on_loaded)
    load_worker.failed.connect(_on_load_err)
    load_worker.start()


def open_proton_manager_dialog(win: MainWindow) -> None:
    """Manage installed custom Proton tools and download/install new releases."""
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("proton_mgr.title"))
    dlg.resize(640, 580)
    lay = QVBoxLayout(dlg)
    dlg_alive = {"open": True}

    def _on_finished(_r: int) -> None:
        dlg_alive["open"] = False

    dlg.finished.connect(_on_finished)

    lay.addWidget(QLabel(tr("proton_mgr.installed_label")))
    installed_list = QTableWidget(0, 3)
    installed_list.setHorizontalHeaderLabels(
        [tr("proton_mgr.column_version"), tr("proton_mgr.column_size"), tr("proton_mgr.column_usage")]
    )
    installed_list.verticalHeader().setVisible(False)
    installed_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    installed_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    installed_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    installed_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    installed_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    installed_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    installed_list.horizontalHeader().setStretchLastSection(True)
    installed_list.horizontalHeader().setSectionsClickable(True)
    installed_list.setSortingEnabled(True)
    lay.addWidget(installed_list, 1)

    inst_btns = QHBoxLayout()
    btn_refresh_inst = QPushButton(tr("proton_mgr.btn_refresh_installed"))
    btn_remove_inst = QPushButton(tr("proton_mgr.btn_remove"))
    btn_remove_inst.setObjectName("dangerBtn")
    btn_remove_inst.setEnabled(False)
    inst_btns.addWidget(btn_refresh_inst)
    inst_btns.addStretch()
    inst_btns.addWidget(btn_remove_inst)
    lay.addLayout(inst_btns)

    lay.addWidget(QLabel(tr("proton_mgr.download_label")))
    fetch_row = QHBoxLayout()
    family_combo = QComboBox()
    for fam in PROTON_FAMILIES.values():
        family_combo.addItem(fam.label, fam.id)
    btn_fetch = QPushButton(tr("proton_mgr.btn_fetch"))
    fetch_row.addWidget(family_combo, 1)
    fetch_row.addWidget(btn_fetch)
    lay.addLayout(fetch_row)

    releases_list = QTableWidget(0, 3)
    releases_list.setHorizontalHeaderLabels(
        [tr("proton_mgr.column_version"), tr("proton_mgr.column_size"), tr("proton_mgr.column_file")]
    )
    releases_list.verticalHeader().setVisible(False)
    releases_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    releases_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    releases_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    releases_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    releases_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    releases_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    releases_list.horizontalHeader().setStretchLastSection(True)
    releases_list.horizontalHeader().setSectionsClickable(True)
    releases_list.setSortingEnabled(True)
    lay.addWidget(releases_list, 1)

    lbl_status = QLabel(tr("status.ready"))
    lbl_status.setObjectName("MutedText")
    lbl_status.setWordWrap(True)
    lay.addWidget(lbl_status)

    progress = QProgressBar()
    progress.setRange(0, 100)
    progress.setVisible(False)
    lay.addWidget(progress)

    footer = QHBoxLayout()
    btn_install = QPushButton(tr("proton_mgr.btn_install"))
    btn_install.setObjectName("primaryBtn")
    btn_install.setEnabled(False)
    btn_close = QPushButton(tr("proton_mgr.btn_close"))
    btn_close.clicked.connect(dlg.reject)
    footer.addWidget(btn_install)
    footer.addStretch()
    footer.addWidget(btn_close)
    lay.addLayout(footer)

    def _set_busy(busy: bool, text: str = "") -> None:
        for w in (btn_refresh_inst, btn_fetch, family_combo):
            w.setEnabled(not busy)
        btn_remove_inst.setEnabled(
            (not busy) and isinstance(_selected_table_data(installed_list), tuple)
        )
        btn_install.setEnabled(
            (not busy) and isinstance(_selected_table_data(releases_list), ProtonRelease)
        )
        progress.setVisible(busy)
        if text:
            lbl_status.setText(text)

    inst_req = {"id": 0}

    class _InstalledToolsWorker(QThread):
        ready = Signal(int, list, list)
        failed = Signal(int, str)

        def __init__(self, req_id: int, paths: SteamPaths | None) -> None:
            super().__init__()
            self._req_id = req_id
            self._paths = paths

        def run(self) -> None:
            try:
                tools = list_installed_compat_tools()
                usage = _manager_tool_usage(self._paths)
                self.ready.emit(self._req_id, tools, usage)
            except Exception as exc:
                self.failed.emit(self._req_id, _format_worker_error(exc))

    def _refresh_installed() -> None:
        inst_req["id"] += 1
        req_id = inst_req["id"]
        installed_list.setSortingEnabled(False)
        installed_list.clearSpans()
        installed_list.setRowCount(0)
        btn_refresh_inst.setEnabled(False)
        btn_remove_inst.setEnabled(False)
        lbl_status.setText(tr("proton_mgr.loading_installed"))

        worker = _InstalledToolsWorker(req_id, win.paths)
        win._track_worker(worker)

        def _on_inst_ready(
            r_id: int, tools: list[InstalledCompatTool], usage: list[CompatMappingEntry]
        ) -> None:
            if not dlg_alive["open"] or win._closing_pending or r_id != inst_req["id"]:
                return
            btn_refresh_inst.setEnabled(True)
            lbl_status.setText(tr("status.ready"))
            installed_list.setSortingEnabled(False)
            installed_list.clearSpans()
            installed_list.setRowCount(0)
            by_tool: dict[str, list[CompatMappingEntry]] = {}
            for u in usage:
                by_tool.setdefault(u.tool_name, []).append(u)
            if not tools:
                installed_list.setRowCount(1)
                installed_list.setSpan(0, 0, 1, 3)
                empty = QTableWidgetItem(tr("proton_mgr.none_installed"))
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                installed_list.setItem(0, 0, empty)
                btn_remove_inst.setEnabled(False)
                return
            for row, tool in enumerate(tools):
                used_entries = list(by_tool.get(tool.tool_id, []))
                if tool.folder_name != tool.tool_id:
                    used_entries.extend(by_tool.get(tool.folder_name, []))
                size_str = format_size_bytes(tool.size_bytes)
                usage_tag = (
                    tr("proton_mgr.in_use", count=len(used_entries))
                    if used_entries
                    else tr("proton_mgr.unused")
                )
                installed_list.insertRow(row)
                it = QTableWidgetItem(tool.folder_name)
                it.setData(Qt.ItemDataRole.UserRole, (tool, used_entries))
                installed_list.setItem(row, 0, it)
                installed_list.setItem(
                    row, 1, _SortableTableItem(size_str, tool.size_bytes)
                )
                installed_list.setItem(row, 2, QTableWidgetItem(usage_tag))
            installed_list.setSortingEnabled(True)
            btn_remove_inst.setEnabled(False)

        def _on_inst_err(r_id: int, err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending or r_id != inst_req["id"]:
                return
            btn_refresh_inst.setEnabled(True)
            lbl_status.setText(tr("proton_mgr.error", err=err))

        worker.ready.connect(_on_inst_ready)
        worker.failed.connect(_on_inst_err)
        worker.start()

    installed_list.currentCellChanged.connect(
        lambda *_args: btn_remove_inst.setEnabled(
            isinstance(_selected_table_data(installed_list), tuple)
        )
    )
    releases_list.currentCellChanged.connect(
        lambda *_args: btn_install.setEnabled(
            isinstance(_selected_table_data(releases_list), ProtonRelease)
        )
    )

    class _FetchProtonWorker(QThread):
        ready = Signal(list)
        failed = Signal(str)

        def __init__(self, family_id: str) -> None:
            super().__init__()
            self._family_id = family_id

        def run(self) -> None:
            try:
                self.ready.emit(fetch_available_proton_releases(self._family_id))
            except Exception as exc:
                self.failed.emit(_format_worker_error(exc))

    class _InstallProtonWorker(QThread):
        progress = Signal(str, int)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self, release: ProtonRelease) -> None:
            super().__init__()
            self._release = release

        def run(self) -> None:
            try:
                target = install_compat_tool(
                    self._release,
                    progress_cb=lambda msg, pct: self.progress.emit(msg, pct),
                )
                self.finished_ok.emit(target.name)
            except Exception as exc:
                self.failed.emit(_format_worker_error(exc))

    def _on_fetch() -> None:
        fam_id = str(family_combo.currentData() or "")
        if not fam_id:
            return
        releases_list.setSortingEnabled(False)
        releases_list.clearSpans()
        releases_list.setRowCount(0)
        progress.setRange(0, 0)
        _set_busy(True, tr("proton_mgr.fetching"))
        worker = _FetchProtonWorker(fam_id)
        win._track_worker(worker)

        def _ok(rels: list[ProtonRelease]) -> None:
            if not dlg_alive["open"]:
                return
            progress.setRange(0, 100)
            _set_busy(False, tr("proton_mgr.fetched", count=len(rels)))
            releases_list.setSortingEnabled(False)
            releases_list.clearSpans()
            releases_list.setRowCount(0)
            if not rels:
                releases_list.setRowCount(1)
                releases_list.setSpan(0, 0, 1, 3)
                empty = QTableWidgetItem(tr("proton_mgr.no_releases"))
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                releases_list.setItem(0, 0, empty)
                return
            for row, rel in enumerate(rels):
                size_str = format_size_bytes(rel.size_bytes) if rel.size_bytes else "?"
                pre_tag = tr("proton_mgr.prerelease_tag") if rel.prerelease else ""
                releases_list.insertRow(row)
                it = QTableWidgetItem(f"{rel.tag_name}{pre_tag}")
                it.setData(Qt.ItemDataRole.UserRole, rel)
                releases_list.setItem(row, 0, it)
                releases_list.setItem(
                    row, 1, _SortableTableItem(size_str, rel.size_bytes or 0)
                )
                releases_list.setItem(row, 2, QTableWidgetItem(rel.asset_name))
            releases_list.setSortingEnabled(True)

        def _err(err: str) -> None:
            if not dlg_alive["open"]:
                return
            progress.setRange(0, 100)
            _set_busy(False, tr("proton_mgr.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

        worker.ready.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _on_install() -> None:
        rel = _selected_table_data(releases_list)
        if not isinstance(rel, ProtonRelease):
            return
        progress.setRange(0, 100)
        progress.setValue(0)
        _set_busy(True, tr("proton_mgr.installing", name=rel.tag_name))
        worker = _InstallProtonWorker(rel)
        win._track_worker(worker)

        def _prog(msg: str, pct: int) -> None:
            if dlg_alive["open"]:
                lbl_status.setText(msg)
                progress.setValue(max(0, min(100, pct)))

        def _ok(folder_name: str) -> None:
            if not dlg_alive["open"]:
                return
            _set_busy(False, tr("proton_mgr.installed", name=folder_name))
            _refresh_installed()
            win._refresh_maintenance_health()
            QMessageBox.information(dlg, __app_name__, tr("proton_mgr.installed", name=folder_name))

        def _err(err: str) -> None:
            if not dlg_alive["open"]:
                return
            _set_busy(False, tr("proton_mgr.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

        worker.progress.connect(_prog)
        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _on_remove() -> None:
        data = _selected_table_data(installed_list)
        if not (isinstance(data, tuple) and len(data) == 2):
            return
        tool, used_entries = data
        assert isinstance(tool, InstalledCompatTool)
        # Refresh usage before removal: the list may predate a new game install.
        active_standalone = [
            entry for entry in _manager_tool_usage(win.paths)
            if entry.source == "Standalone" and entry.tool_name in (tool.tool_id, tool.folder_name)
        ]
        if active_standalone:
            QMessageBox.warning(
                dlg, __app_name__,
                tr("proton_mgr.standalone_in_use", name=tool.folder_name,
                   games="\n".join(entry.name for entry in active_standalone)),
            )
            return
        if used_entries and win.paths is not None:
            paths = win.paths
            auto_snap_enabled = win._is_auto_snapshot_enabled()

            class _ListProtonsWorker(QThread):
                ready = Signal(list)

                def run(self) -> None:
                    protons = [
                        (tid, disp)
                        for tid, disp in list_installed_protons(paths.steam_root)
                        if tid not in (tool.tool_id, tool.folder_name)
                    ]
                    self.ready.emit(protons)

            def _on_protons_loaded(protons: list[tuple[str, str]]) -> None:
                if not dlg_alive["open"] or win._closing_pending:
                    return
                if not protons:
                    QMessageBox.warning(
                        dlg,
                        __app_name__,
                        tr("proton_mgr.in_use_no_target", name=tool.folder_name, count=len(used_entries)),
                    )
                    return
                labels = [disp for _, disp in protons]
                choice, ok = QInputDialog.getItem(
                    dlg,
                    tr("proton_mgr.title"),
                    tr("proton_mgr.in_use_migrate_prompt", name=tool.folder_name, count=len(used_entries)),
                    labels,
                    0,
                    False,
                )
                if not ok or not choice:
                    return
                target_tool = protons[labels.index(choice)][0]
                ids = [u.appid_u32 for u in used_entries]

                def _proceed_migrate_and_remove() -> None:
                    if not dlg_alive["open"] or win._closing_pending:
                        return
                    progress.setRange(0, 0)
                    _set_busy(True, tr("proton_mgr.migrating_removing", name=tool.folder_name))

                    def _do_migrate_and_remove() -> None:
                        for u in used_entries:
                            win._auto_snapshot_if_enabled(
                                paths.compatdata_dir / str(u.appid_u32) / "pfx",
                                u.name or str(u.appid_u32),
                                "proton-bulk-migrate",
                                "proton",
                                enabled=auto_snap_enabled,
                            )
                        bulk_migrate_compat_tool(paths.config_vdf, ids, target_tool)
                        remove_compat_tool(tool.path)

                    worker = SteamOpWorker(_do_migrate_and_remove, need_steam_closed=True)
                    win._track_worker(worker)

                    def _ok_mig(_res: object) -> None:
                        if not dlg_alive["open"]:
                            return
                        progress.setRange(0, 100)
                        _set_busy(False, tr("proton_mgr.removed", name=tool.folder_name))
                        _refresh_installed()
                        win._refresh_maintenance_health()

                    def _err_mig(err: str) -> None:
                        if not dlg_alive["open"]:
                            return
                        progress.setRange(0, 100)
                        _set_busy(False, tr("proton_mgr.error", err=err))
                        QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

                    worker.finished_ok.connect(_ok_mig)
                    worker.failed.connect(_err_mig)
                    worker.start()

                win._confirm_close_steam_async(
                    _proceed_migrate_and_remove,
                    parent=dlg,
                    alive_fn=lambda: dlg_alive["open"],
                )

            protons_worker = _ListProtonsWorker()
            win._track_worker(protons_worker)
            protons_worker.ready.connect(_on_protons_loaded)
            protons_worker.start()
            return

        if (
            QMessageBox.question(
                dlg,
                __app_name__,
                tr("proton_mgr.confirm_remove", name=tool.folder_name),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        class _RemoveCompatWorker(QThread):
            done = Signal()
            failed = Signal(str)

            def __init__(self, target_path: Path) -> None:
                super().__init__()
                self._path = target_path

            def run(self) -> None:
                try:
                    remove_compat_tool(self._path)
                    self.done.emit()
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        progress.setRange(0, 0)
        _set_busy(True, tr("proton_mgr.removing", name=tool.folder_name))
        rm_worker = _RemoveCompatWorker(tool.path)
        win._track_worker(rm_worker)

        def _on_rm_done() -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            progress.setRange(0, 100)
            _set_busy(False, tr("proton_mgr.removed", name=tool.folder_name))
            _refresh_installed()
            win._refresh_maintenance_health()

        def _on_rm_err(err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            progress.setRange(0, 100)
            _set_busy(False, tr("proton_mgr.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

        rm_worker.done.connect(_on_rm_done)
        rm_worker.failed.connect(_on_rm_err)
        rm_worker.start()

    btn_refresh_inst.clicked.connect(_refresh_installed)
    btn_fetch.clicked.connect(_on_fetch)
    btn_install.clicked.connect(_on_install)
    btn_remove_inst.clicked.connect(_on_remove)
    _refresh_installed()
    dlg.exec()


def game_proton_versions_dialog(win: MainWindow) -> None:
    """Show each DeckInstall game's Proton (Default or pinned) and pin/unpin them in batch."""
    from ..proton_policy import (
        DEFAULT_PROTON_ID,
        effective_proton_name,
        is_default,
        newest_ge_proton,
        previous_proton_name,
        set_games_proton,
        use_previous_proton,
    )

    games = [g for g in list_standalone_games() if not g.native_linux]
    if not games:
        QMessageBox.information(win, __app_name__, tr("proton_games.empty"))
        return

    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("proton_games.title"))
    dlg.resize(780, 520)
    lay = QVBoxLayout(dlg)
    hint = QLabel(tr("proton_games.hint"))
    hint.setWordWrap(True)
    lay.addWidget(hint)

    tbl = QTableWidget(0, 4)
    tbl.setHorizontalHeaderLabels([
        tr("table.name"), tr("proton_games.col_mode"), tr("proton_games.col_current"), tr("proton_games.col_previous"),
    ])
    tbl.verticalHeader().setVisible(False)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    lay.addWidget(tbl, 1)

    def _populate() -> None:
        tbl.setRowCount(0)
        for game in sorted(list_standalone_games(), key=lambda g: g.name.lower()):
            if game.native_linux:
                continue
            row = tbl.rowCount()
            tbl.insertRow(row)
            it = QTableWidgetItem(game.name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, str(game.game_dir))
            tbl.setItem(row, 0, it)
            mode = tr("proton_games.mode_default") if is_default(game) else tr("proton_games.mode_pinned")
            tbl.setItem(row, 1, QTableWidgetItem(mode))
            tbl.setItem(row, 2, QTableWidgetItem(effective_proton_name(game) or "-"))
            tbl.setItem(row, 3, QTableWidgetItem(previous_proton_name(game) or "-"))

    _populate()

    target_row = QHBoxLayout()
    target_row.addWidget(QLabel(tr("proton_bulk.target_label")))
    target_combo = QComboBox()
    newest = newest_ge_proton()
    target_combo.addItem(tr("proton.default_newest", name=newest.name if newest else "GE-Proton"), DEFAULT_PROTON_ID)
    for ct in list_installed_compat_tools():
        target_combo.addItem(ct.folder_name or ct.tool_id, ct.folder_name or ct.tool_id)
    target_row.addWidget(target_combo, 1)
    lay.addLayout(target_row)

    btn_row = QHBoxLayout()
    btn_all = QPushButton(tr("proton_games.btn_check_all"))
    btn_apply = QPushButton(tr("proton_games.btn_apply"))
    btn_apply.setObjectName("primaryBtn")
    btn_previous = QPushButton(tr("configure.btn_previous_proton"))
    btn_close = QPushButton(tr("proton_bulk.btn_close"))
    for b in (btn_all, btn_apply, btn_previous):
        btn_row.addWidget(b)
    btn_row.addStretch()
    btn_row.addWidget(btn_close)
    lay.addLayout(btn_row)
    btn_close.clicked.connect(dlg.accept)

    def _checked() -> list[Path]:
        out: list[Path] = []
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(Path(str(it.data(Qt.ItemDataRole.UserRole))))
        return out

    def _check_all() -> None:
        state = Qt.CheckState.Checked if not _checked() else Qt.CheckState.Unchecked
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None:
                it.setCheckState(state)

    def _run(job: Callable[[list[Path]], tuple[int, list[str]]]) -> None:
        dirs = _checked()
        if not dirs:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        for b in (btn_apply, btn_previous):
            b.setEnabled(False)
        worker = SteamOpWorker(lambda: job(dirs), need_steam_closed=False)
        win._track_worker(worker)

        def _done(result: object) -> None:
            changed, errors = result if isinstance(result, tuple) else (0, [])
            win._refresh_shortcuts()
            if dlg.isVisible():
                for b in (btn_apply, btn_previous):
                    b.setEnabled(True)
                _populate()
            msg = tr("proton_games.changed", count=changed)
            if errors:
                msg = f"{msg}\n\n" + "\n".join(errors)
            win.status_label.setText(tr("proton_games.changed", count=changed))
            QMessageBox.information(dlg, __app_name__, msg)

        def _fail(err: str) -> None:
            for b in (btn_apply, btn_previous):
                b.setEnabled(True)
            QMessageBox.critical(dlg, __app_name__, tr("proton.error", err=err))

        worker.finished_ok.connect(_done)
        worker.failed.connect(_fail)
        worker.start()

    def _apply() -> None:
        target = str(target_combo.currentData() or "")
        tool = "" if target == DEFAULT_PROTON_ID else target
        _run(lambda dirs: set_games_proton(dirs, tool))

    def _previous_all(dirs: list[Path]) -> tuple[int, list[str]]:
        changed = 0
        errors: list[str] = []
        for d in dirs:
            try:
                use_previous_proton(d)
                changed += 1
            except (OSError, ValueError) as exc:
                errors.append(f"{d.name}: {exc}")
        return changed, errors

    btn_all.clicked.connect(_check_all)
    btn_apply.clicked.connect(_apply)
    btn_previous.clicked.connect(lambda: _run(_previous_all))
    dlg.exec()
