"""Folder-based Save Sync and Auto-Snapshot settings dialog."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import __app_name__
from ..i18n import tr
from ..save_sync import (
    DEFAULT_SYNC_DIR,
    SyncDryRunPreview,
    SyncPlanItem,
    plan_sync_all,
    preview_sync_operation,
    pull_game_saves,
    push_game_saves,
)
from ..ui_cards import _format_worker_error, _setting_bool

if TYPE_CHECKING:
    from ..ui import MainWindow


def open_save_sync_dialog(win: MainWindow) -> None:
    """Open folder-based Save Sync and Auto-Snapshot settings dialog."""
    dlg = QDialog(win)
    dlg.setWindowTitle(tr("save_sync.title"))
    dlg.resize(640, 520)
    lay = QVBoxLayout(dlg)
    dlg_alive = {"open": True}

    def _on_finished(_r: int) -> None:
        dlg_alive["open"] = False

    dlg.finished.connect(_on_finished)

    folder_row = QHBoxLayout()
    folder_row.addWidget(QLabel(tr("save_sync.folder_label")))
    saved_dir = str(win.settings.value("save_sync/folder", str(DEFAULT_SYNC_DIR)) or DEFAULT_SYNC_DIR)
    sync_dir_edit = QLineEdit(saved_dir)
    btn_browse = QPushButton(tr("install.browse"))
    folder_row.addWidget(sync_dir_edit, 1)
    folder_row.addWidget(btn_browse)
    lay.addLayout(folder_row)

    chk_auto_snap = QCheckBox(tr("save_sync.auto_snapshot"))
    chk_auto_snap.setChecked(_setting_bool(win.settings, "safety/auto_snapshot", True))
    chk_auto_snap.toggled.connect(
        lambda checked: win.settings.setValue("safety/auto_snapshot", bool(checked))
    )
    lay.addWidget(chk_auto_snap)

    plan_top = QHBoxLayout()
    plan_top.addWidget(QLabel(tr("save_sync.plan_label")))
    plan_top.addStretch()
    btn_refresh = QPushButton(tr("save_sync.btn_refresh"))
    plan_top.addWidget(btn_refresh)
    lay.addLayout(plan_top)

    plan_list = QListWidget()
    lay.addWidget(plan_list, 1)

    lbl_status = QLabel(tr("status.ready"))
    lbl_status.setObjectName("MutedText")
    lbl_status.setWordWrap(True)
    lay.addWidget(lbl_status)

    footer = QHBoxLayout()
    btn_preview = QPushButton(tr("save_sync.btn_preview"))
    btn_preview.setEnabled(False)
    btn_sync = QPushButton(tr("save_sync.btn_sync_now"))
    btn_sync.setObjectName("primaryBtn")
    btn_close = QPushButton(tr("proton_mgr.btn_close"))
    btn_close.clicked.connect(dlg.reject)
    footer.addWidget(btn_preview)
    footer.addWidget(btn_sync)
    footer.addStretch()
    footer.addWidget(btn_close)
    lay.addLayout(footer)

    current_plan: list[SyncPlanItem] = []

    def _collect_games_for_sync() -> list[tuple[str, Path]]:
        games: list[tuple[str, Path]] = []
        if win.paths is not None:
            for entry in win._steam_shortcuts:
                games.append((entry.app_name, win.paths.compatdata_dir / str(entry.appid_u32) / "pfx"))
        for sg in win._standalone_games:
            if not sg.native_linux:
                games.append((sg.name, sg.prefix_dir))
        for ext in win._external_games:
            if ext.prefix_dir is not None and ext.runner != "linux":
                games.append((ext.name, ext.prefix_dir))
        return games

    def _current_sync_root() -> Path:
        raw = sync_dir_edit.text().strip() or str(DEFAULT_SYNC_DIR)
        win.settings.setValue("save_sync/folder", raw)
        return Path(raw).expanduser()

    plan_req = {"id": 0}

    class _PlanSyncWorker(QThread):
        ready = Signal(int, list)
        failed = Signal(int, str)

        def __init__(
            self, req_id: int, games: list[tuple[str, Path]], sync_root: Path
        ) -> None:
            super().__init__()
            self._req_id = req_id
            self._games = games
            self._sync_root = sync_root

        def run(self) -> None:
            try:
                items = plan_sync_all(self._games, self._sync_root)
                self.ready.emit(self._req_id, items)
            except Exception as exc:
                self.failed.emit(self._req_id, _format_worker_error(exc))

    def _apply_plan_items(items: list[SyncPlanItem]) -> None:
        plan_list.clear()
        current_plan.clear()
        current_plan.extend(items)
        if not items:
            empty = QListWidgetItem(tr("save_sync.no_games"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            plan_list.addItem(empty)
            btn_sync.setEnabled(False)
            btn_preview.setEnabled(False)
            return
        actionable = False
        for item in items:
            if item.action in ("PUSH", "PULL", "CONFLICT"):
                actionable = True
            it = QListWidgetItem(f"[{item.action}] {item.game_name} — {item.reason}")
            it.setData(Qt.ItemDataRole.UserRole, item)
            plan_list.addItem(it)
        btn_sync.setEnabled(actionable)
        btn_preview.setEnabled(bool(items))

    def _refresh_plan() -> None:
        plan_req["id"] += 1
        req_id = plan_req["id"]
        sync_root = _current_sync_root()
        games = _collect_games_for_sync()
        btn_sync.setEnabled(False)
        btn_refresh.setEnabled(False)
        lbl_status.setText(tr("save_sync.loading_plan"))

        worker = _PlanSyncWorker(req_id, games, sync_root)
        win._track_worker(worker)

        def _on_plan_ready(r_id: int, items: list[SyncPlanItem]) -> None:
            if not dlg_alive["open"] or win._closing_pending or r_id != plan_req["id"]:
                return
            btn_refresh.setEnabled(True)
            lbl_status.setText(tr("status.ready"))
            _apply_plan_items(items)

        def _on_plan_err(r_id: int, err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending or r_id != plan_req["id"]:
                return
            btn_refresh.setEnabled(True)
            lbl_status.setText(tr("save_sync.error", err=err))

        worker.ready.connect(_on_plan_ready)
        worker.failed.connect(_on_plan_err)
        worker.start()

    def _on_browse() -> None:
        picked = QFileDialog.getExistingDirectory(
            dlg, tr("save_sync.pick_folder"), str(_current_sync_root())
        )
        if picked:
            sync_dir_edit.setText(picked)
            _refresh_plan()

    class _FolderSaveSyncWorker(QThread):
        status = Signal(str)
        finished_ok = Signal(int, int, list)
        failed = Signal(str)

        def __init__(
            self,
            ops: list[tuple[SyncPlanItem, str]],
            sync_root: Path,
        ) -> None:
            super().__init__()
            self._ops = ops
            self._sync_root = sync_root

        def run(self) -> None:
            pushed = 0
            pulled = 0
            errors: list[str] = []
            for item, action in self._ops:
                self.status.emit(
                    tr("save_sync.syncing_game", name=item.game_name, action=action)
                )
                try:
                    if action == "PUSH":
                        if (
                            push_game_saves(
                                item.game_name, item.prefix_dir, self._sync_root
                            )
                            is not None
                        ):
                            pushed += 1
                    elif action == "PULL":
                        restored, _ = pull_game_saves(
                            item.game_name, item.prefix_dir, self._sync_root
                        )
                        if restored > 0:
                            pulled += 1
                except Exception as exc:
                    errors.append(f"{item.game_name}: {_format_worker_error(exc)}")
            self.finished_ok.emit(pushed, pulled, errors)

    def _on_sync_now() -> None:
        sync_root = _current_sync_root()
        ops: list[tuple[SyncPlanItem, str]] = []
        for item in current_plan:
            if item.action in ("PUSH", "PULL"):
                ops.append((item, item.action))
            elif item.action == "CONFLICT":
                box = QMessageBox(dlg)
                box.setIcon(QMessageBox.Icon.Question)
                box.setWindowTitle(__app_name__)
                box.setText(tr("save_sync.conflict_prompt", name=item.game_name, reason=item.reason))
                btn_push = box.addButton(
                    tr("save_sync.conflict_keep_local"), QMessageBox.ButtonRole.AcceptRole
                )
                btn_pull = box.addButton(
                    tr("save_sync.conflict_use_remote"), QMessageBox.ButtonRole.AcceptRole
                )
                btn_skip = box.addButton(
                    tr("save_sync.conflict_skip"), QMessageBox.ButtonRole.RejectRole
                )
                box.setDefaultButton(btn_skip)
                box.exec()
                clicked = box.clickedButton()
                if clicked == btn_push:
                    ops.append((item, "PUSH"))
                elif clicked == btn_pull:
                    ops.append((item, "PULL"))

        if not ops:
            QMessageBox.information(dlg, __app_name__, tr("save_sync.nothing_to_sync"))
            return

        for w in (btn_sync, btn_refresh, btn_browse, sync_dir_edit):
            w.setEnabled(False)
        lbl_status.setText(tr("save_sync.syncing"))
        worker = _FolderSaveSyncWorker(ops, sync_root)
        win._track_worker(worker)

        def _ok(pushed: int, pulled: int, errors: list[str]) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            for w in (btn_refresh, btn_browse, sync_dir_edit):
                w.setEnabled(True)
            msg = tr("save_sync.done", pushed=pushed, pulled=pulled)
            if errors:
                msg += tr("save_sync.partial_errors", errs="\n".join(errors))
            lbl_status.setText(msg.splitlines()[0])
            _refresh_plan()
            if errors:
                QMessageBox.warning(dlg, __app_name__, msg)
            else:
                QMessageBox.information(dlg, __app_name__, msg)

        def _err(err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            for w in (btn_sync, btn_refresh, btn_browse, sync_dir_edit):
                w.setEnabled(True)
            lbl_status.setText(tr("save_sync.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("save_sync.error", err=err))

        def _on_sync_status(m: str) -> None:
            if dlg_alive["open"]:
                lbl_status.setText(m)

        worker.status.connect(_on_sync_status)
        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _on_preview() -> None:
        sync_root = _current_sync_root()
        cur_item = plan_list.currentItem()
        selected_plan = cur_item.data(Qt.ItemDataRole.UserRole) if cur_item is not None else None
        if isinstance(selected_plan, SyncPlanItem):
            targets = [selected_plan]
        else:
            targets = [it for it in current_plan if it.action in ("PUSH", "PULL", "CONFLICT")]
            if not targets:
                targets = list(current_plan)
        if not targets:
            QMessageBox.information(dlg, __app_name__, tr("save_sync.nothing_to_sync"))
            return

        btn_preview.setEnabled(False)
        lbl_status.setText(tr("save_sync.preview_loading"))

        class _PreviewSyncWorker(QThread):
            ready = Signal(list)
            failed = Signal(str)

            def run(self) -> None:
                try:
                    previews: list[SyncDryRunPreview] = []
                    for item in targets:
                        action_arg = item.action if item.action in ("PUSH", "PULL") else None
                        previews.append(
                            preview_sync_operation(
                                item.game_name,
                                item.prefix_dir,
                                sync_root,
                                action=action_arg,
                            )
                        )
                    self.ready.emit(previews)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        p_worker = _PreviewSyncWorker()
        win._track_worker(p_worker)

        def _on_prev_ok(previews: list[SyncDryRunPreview]) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            btn_preview.setEnabled(bool(current_plan))
            lbl_status.setText(tr("status.ready"))
            lines: list[str] = []
            for pv in previews:
                lines.append(
                    tr(
                        "save_sync.preview_header",
                        action=pv.action,
                        name=pv.game_name,
                        count=len(pv.files),
                        size=f"{pv.total_bytes:,} B",
                        reg=pv.registry_entries,
                    )
                )
                lines.append(f"  {pv.reason}")
                for f in pv.files[:200]:
                    lines.append(f"  [{f.operation}] {f.rel_path} ({f.size_bytes:,} B)")
                if len(pv.files) > 200:
                    lines.append(tr("save_sync.preview_truncated", extra=len(pv.files) - 200))
                lines.append("")

            pv_dlg = QDialog(dlg)
            pv_dlg.setWindowTitle(tr("save_sync.preview_title"))
            pv_dlg.resize(620, 440)
            pv_lay = QVBoxLayout(pv_dlg)
            txt = QPlainTextEdit()
            txt.setReadOnly(True)
            txt.setPlainText("\n".join(lines).strip())
            pv_lay.addWidget(txt, 1)
            btn_ok = QPushButton(tr("proton_mgr.btn_close"))
            btn_ok.clicked.connect(pv_dlg.accept)
            pv_lay.addWidget(btn_ok)
            pv_dlg.exec()

        def _on_prev_err(err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            btn_preview.setEnabled(bool(current_plan))
            lbl_status.setText(tr("save_sync.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("save_sync.error", err=err))

        p_worker.ready.connect(_on_prev_ok)
        p_worker.failed.connect(_on_prev_err)
        p_worker.start()

    btn_browse.clicked.connect(_on_browse)
    btn_refresh.clicked.connect(_refresh_plan)
    btn_preview.clicked.connect(_on_preview)
    btn_sync.clicked.connect(_on_sync_now)
    sync_dir_edit.editingFinished.connect(_refresh_plan)
    _refresh_plan()
    dlg.exec()
