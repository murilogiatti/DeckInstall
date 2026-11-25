"""Proton version manager dialog (install / remove Proton builds)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    _steam_transaction_lock,
    bulk_migrate_compat_tool,
    list_compat_tool_usage,
)
from ..ui_cards import (
    _BusyGuardedDialog,
    _dialog_parent,
    _format_worker_error,
    _is_wine_game_running,
    _SortableTableItem,
    dialog_footer,
    format_size_bytes,
    note_label,
    section_card,
)
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


def open_proton_manager_dialog(win: MainWindow) -> None:
    """Manage installed custom Proton tools and download/install new releases."""
    busy_state = {"active": False}
    dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: busy_state["active"])
    dlg.setWindowTitle(tr("proton_mgr.title"))
    dlg.resize(680, 640)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)
    dlg_alive = {"open": True}

    dlg.finished.connect(lambda _result: dlg_alive.__setitem__("open", False))

    # 1. What is installed, and what uses it (remove only what nothing uses).
    inst_card, inst_lay, inst_top = section_card(tr("proton_mgr.installed_label"), tr("proton_mgr.installed_hint"))
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
    inst_lay.addWidget(installed_list, 1)
    btn_refresh_inst = QPushButton(tr("proton_mgr.btn_refresh_installed"))
    inst_top.addWidget(btn_refresh_inst)
    btn_remove_inst = QPushButton(tr("proton_mgr.btn_remove"))
    btn_remove_inst.setObjectName("dangerBtn")
    btn_remove_inst.setEnabled(False)
    inst_lay.addWidget(btn_remove_inst, 0, Qt.AlignmentFlag.AlignLeft)
    lay.addWidget(inst_card, 1)

    # 2. Another version: pick a family, look up its releases, install one.
    dl_card, dl_lay, _top = section_card(tr("proton_mgr.download_label"))
    fetch_row = QHBoxLayout()
    family_combo = QComboBox()
    for fam in PROTON_FAMILIES.values():
        family_combo.addItem(fam.label, fam.id)
    btn_fetch = QPushButton(tr("proton_mgr.btn_fetch"))
    fetch_row.addWidget(family_combo, 1)
    fetch_row.addWidget(btn_fetch)
    dl_lay.addLayout(fetch_row)

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
    dl_lay.addWidget(releases_list, 1)
    lay.addWidget(dl_card, 1)

    lbl_status = note_label(tr("status.ready"))
    lay.addWidget(lbl_status)

    progress = QProgressBar()
    progress.setRange(0, 100)
    progress.setVisible(False)
    lay.addWidget(progress)

    btn_install = QPushButton(tr("proton_mgr.btn_install"))
    btn_install.setObjectName("primaryBtn")
    btn_install.setEnabled(False)
    btn_close = QPushButton(tr("proton_mgr.btn_close"))
    btn_close.clicked.connect(dlg.reject)
    lay.addLayout(dialog_footer(btn_install, btn_close))

    def _can_remove_selected() -> bool:
        if busy_state["active"]:
            return False
        sel = _selected_table_data(installed_list)
        if not isinstance(sel, tuple):
            return False
        from ..runtime_manager import is_system_compat_tool

        return not is_system_compat_tool(sel[0].path)

    def _set_busy(busy: bool, text: str = "") -> None:
        busy_state["active"] = busy
        for w in (btn_refresh_inst, btn_fetch, family_combo, installed_list, releases_list, btn_close):
            w.setEnabled(not busy)
        btn_remove_inst.setEnabled(_can_remove_selected())
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
            if not busy_state["active"]:
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
                installed_list.setItem(row, 2, _SortableTableItem(usage_tag, len(used_entries)))
            installed_list.setSortingEnabled(True)
            btn_remove_inst.setEnabled(False)

        def _on_inst_err(r_id: int, err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending or r_id != inst_req["id"]:
                return
            if not busy_state["active"]:
                btn_refresh_inst.setEnabled(True)
                lbl_status.setText(tr("proton_mgr.error", err=err))

        worker.ready.connect(_on_inst_ready)
        worker.failed.connect(_on_inst_err)
        worker.start()

    installed_list.currentCellChanged.connect(
        lambda *_args: btn_remove_inst.setEnabled(_can_remove_selected())
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
            if QMessageBox.question(
                dlg, __app_name__, tr("runtime.offer_proton_installed", name=folder_name),
            ) == QMessageBox.StandardButton.Yes:
                from .proton_bulk_dialog import bulk_proton_dialog

                dlg.accept()
                bulk_proton_dialog(win, preferred_tool=folder_name)
            else:
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
        if busy_state["active"] or not (isinstance(data, tuple) and len(data) == 2):
            return
        tool, _ = data
        assert isinstance(tool, InstalledCompatTool)
        paths = win.paths
        names = {tool.tool_id, tool.folder_name}
        auto_snap = win._is_auto_snapshot_enabled()
        _set_busy(True, tr("proton_mgr.loading_installed"))
        win._begin_busy()

        def _usage() -> list[CompatMappingEntry]:
            return [entry for entry in _manager_tool_usage(paths) if entry.tool_name in names]

        def _finish(text: str = "") -> None:
            win._end_busy()
            if dlg_alive["open"]:
                progress.setRange(0, 100)
                _set_busy(False, text or tr("status.ready"))

        def _failed(err: str) -> None:
            _finish(tr("proton_mgr.error", err=err))
            if dlg_alive["open"] and not win._closing_pending:
                QMessageBox.critical(dlg, __app_name__, tr("proton_mgr.error", err=err))

        def _inspect() -> tuple[list[CompatMappingEntry], list[tuple[str, str]]]:
            usage = _usage()
            alternatives = (
                [(tid, label) for tid, label in list_installed_protons(paths.steam_root) if tid not in names]
                if paths is not None and any(u.source != "Standalone" and u.appid_u32 > 0 for u in usage)
                else []
            )
            return usage, alternatives

        def _start_removal(target: str, approved_ids: set[int]) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                _finish()
                return
            queued = win.should_queue_vdf_edits()
            progress.setRange(0, 0)
            lbl_status.setText(tr("proton_mgr.removing", name=tool.folder_name))

            def _remove() -> None:
                from ..runtime_manager import is_system_compat_tool

                if is_system_compat_tool(tool.path):
                    raise ValueError(f"Refusing to remove system-managed compatibility tool: {tool.path}")
                if _is_wine_game_running():
                    raise RuntimeError(tr("cleanup.wine_running"))
                lock = _steam_transaction_lock(paths.config_vdf) if paths is not None else nullcontext()
                with lock:
                    usage = _usage()
                    if any(u.appid_u32 <= 0 or u.source == "Standalone" for u in usage):
                        raise RuntimeError(tr("proton_mgr.remove_requires_review"))
                    ids = {u.appid_u32 for u in usage}
                    if ids and (queued or not target or not ids.issubset(approved_ids)):
                        raise RuntimeError(tr("proton_mgr.remove_requires_review"))
                    if paths is not None and ids:
                        for u in usage:
                            win._auto_snapshot_if_enabled(
                                paths.compatdata_dir / str(u.appid_u32) / "pfx", u.name,
                                "proton-bulk-migrate", "proton", enabled=auto_snap,
                            )
                        bulk_migrate_compat_tool(paths.config_vdf, sorted(ids), target)
                    remove_compat_tool(tool.path)

            worker = SteamOpWorker(_remove, need_steam_closed=bool(approved_ids))
            win._track_worker(worker)

            def _done(_result: object) -> None:
                _finish(tr("proton_mgr.removed", name=tool.folder_name))
                if dlg_alive["open"] and not win._closing_pending:
                    _refresh_installed()
                    win._refresh_maintenance_health()
                    win._refresh_shortcuts()
                    from ..ui_maintenance import offer_maintenance_review

                    if offer_maintenance_review(win, "runtimes", dlg, details=str(tool.path)):
                        dlg.accept()

            worker.finished_ok.connect(_done)
            worker.failed.connect(_failed)
            worker.start()

        def _ready(result: object) -> None:
            if not dlg_alive["open"] or win._closing_pending or not isinstance(result, tuple):
                _finish()
                return
            usage, alternatives = result
            standalone_users = [u.name for u in usage if u.source == "Standalone"]
            if standalone_users:
                _finish()
                QMessageBox.warning(dlg, __app_name__, tr(
                    "proton_mgr.standalone_in_use", name=tool.folder_name, games="\n".join(standalone_users),
                ))
                return
            if any(u.appid_u32 <= 0 for u in usage) or (usage and win.should_queue_vdf_edits()):
                _finish()
                QMessageBox.warning(dlg, __app_name__, tr("proton_mgr.remove_requires_review"))
                return
            if not usage:
                if QMessageBox.question(dlg, __app_name__, tr(
                    "proton_mgr.confirm_remove", name=tool.folder_name,
                )) == QMessageBox.StandardButton.Yes:
                    _start_removal("", set())
                else:
                    _finish()
                return
            if not alternatives:
                _finish()
                QMessageBox.warning(dlg, __app_name__, tr(
                    "proton_mgr.in_use_no_target", name=tool.folder_name, count=len(usage),
                ))
                return
            labels = [label for _, label in alternatives]
            choice, ok = QInputDialog.getItem(
                dlg, tr("proton_mgr.title"),
                tr("proton_mgr.in_use_migrate_prompt", name=tool.folder_name, count=len(usage)),
                labels, 0, False,
            )
            if not ok or not choice:
                _finish()
                return
            target = alternatives[labels.index(choice)][0]
            win._confirm_close_steam_async(
                lambda: _start_removal(target, {u.appid_u32 for u in usage}),
                on_cancelled=_finish, parent=dlg, alive_fn=lambda: dlg_alive["open"],
            )

        preflight = SteamOpWorker(_inspect)
        win._track_worker(preflight)
        preflight.finished_ok.connect(_ready)
        preflight.failed.connect(_failed)
        preflight.start()

    btn_refresh_inst.clicked.connect(_refresh_installed)
    btn_fetch.clicked.connect(_on_fetch)
    btn_install.clicked.connect(_on_install)
    btn_remove_inst.clicked.connect(_on_remove)
    _refresh_installed()
    dlg.exec()
    dlg.deleteLater()
