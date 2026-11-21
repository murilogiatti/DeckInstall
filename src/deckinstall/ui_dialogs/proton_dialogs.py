"""Proton version manager dialog (install / remove Proton builds)."""

from __future__ import annotations

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
)
from ..ui_cards import _dialog_parent, _format_worker_error, _SortableTableItem, format_size_bytes
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit

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


def open_proton_manager_dialog(win: MainWindow) -> None:
    """Manage installed custom Proton tools and download/install new releases."""
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("proton_mgr.title"))
    dlg.resize(640, 580)
    lay = QVBoxLayout(dlg)
    dlg_alive = {"open": True}
    busy_state = {"active": False}
    orig_close_event = dlg.closeEvent

    def _on_finished(_r: int) -> None:
        dlg_alive["open"] = False

    def _close_event(event) -> None:
        if busy_state["active"]:
            event.ignore()
            return
        orig_close_event(event)

    dlg.closeEvent = _close_event  # type: ignore[method-assign]
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
        busy_state["active"] = busy
        for w in (btn_refresh_inst, btn_fetch, family_combo, installed_list, releases_list, btn_close):
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
            not busy_state["active"] and isinstance(_selected_table_data(installed_list), tuple)
        )
    )
    releases_list.currentCellChanged.connect(
        lambda *_args: btn_install.setEnabled(
            not busy_state["active"] and isinstance(_selected_table_data(releases_list), ProtonRelease)
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
                failed = Signal(str)

                def run(self) -> None:
                    try:
                        protons = [
                            (tid, disp)
                            for tid, disp in list_installed_protons(paths.steam_root)
                            if tid not in (tool.tool_id, tool.folder_name)
                        ]
                        self.ready.emit(protons)
                    except Exception as exc:
                        self.failed.emit(_format_worker_error(exc))

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
                    queued = win.should_queue_vdf_edits()
                    win._begin_busy()
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
                        if not queued:
                            bulk_migrate_compat_tool(paths.config_vdf, ids, target_tool)
                        remove_compat_tool(tool.path)

                    worker = SteamOpWorker(_do_migrate_and_remove, need_steam_closed=not queued)
                    win._track_worker(worker)

                    def _ok_mig(_res: object) -> None:
                        win._end_busy()
                        if queued:
                            win.enqueue_vdf_edit(
                                QueuedVdfEdit(
                                    f"bulk_migrate_compat:{target_tool}:{','.join(map(str, ids))}",
                                    "bulk_migrate_compat",
                                    0,
                                    tr("proton_bulk.queue_label", tool=target_tool),
                                    {"appids": ids, "target_tool": target_tool},
                                )
                            )
                        if not dlg_alive["open"]:
                            return
                        progress.setRange(0, 100)
                        _set_busy(False, tr("proton_mgr.removed", name=tool.folder_name))
                        _refresh_installed()
                        win._refresh_maintenance_health()

                    def _err_mig(err: str) -> None:
                        win._end_busy()
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

            def _on_protons_failed(err: str) -> None:
                if dlg_alive["open"] and not win._closing_pending:
                    QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

            protons_worker = _ListProtonsWorker()
            win._track_worker(protons_worker)
            protons_worker.ready.connect(_on_protons_loaded)
            protons_worker.failed.connect(_on_protons_failed)
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
    dlg.deleteLater()
