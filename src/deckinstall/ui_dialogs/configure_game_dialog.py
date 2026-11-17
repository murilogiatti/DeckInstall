"""Game configuration and scan-folders management dialogs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..external_actions import load_external_launch_presets
from ..i18n import tr
from ..launcher_scan import ExternalLauncherGame
from ..runtime_manager import get_umu_protonfix_status, is_win32_prefix, resolve_game_prefix
from ..standalone import StandaloneGame
from ..steam_vdf import (
    ShortcutEntry,
    get_compatdata_migration_state,
    get_shortcut_proton,
    get_steam_input_mode,
    list_microsd_compatdata_targets,
    migrate_compatdata_to_microsd,
    restore_compatdata_from_microsd,
    set_steam_input_mode,
)
from ..ui_cards import _dialog_guard, _dialog_parent, _format_worker_error, _is_native_linux_game

if TYPE_CHECKING:
    from ..ui import MainWindow


def open_scan_folders_dialog(win: MainWindow) -> None:
    """Open a dialog to enable/disable default scan roots and manage custom folders."""
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("scan_folders.title"))
    dlg.resize(560, 420)
    lay = QVBoxLayout(dlg)

    sub = QLabel(tr("scan_folders.subtitle"))
    sub.setObjectName("MutedText")
    sub.setWordWrap(True)
    lay.addWidget(sub)

    folder_list = QTableWidget(0, 1)
    folder_list.setHorizontalHeaderLabels([tr("scan_folders.column_folder")])
    folder_list.verticalHeader().setVisible(False)
    folder_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    folder_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    folder_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    folder_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    folder_list.horizontalHeader().setSectionsClickable(True)
    folder_list.setSortingEnabled(True)
    _, custom_entries = win._load_scan_folders()

    def _add_entry(kind: str, path_str: str, enabled: bool) -> QTableWidgetItem:
        was_sorting = folder_list.isSortingEnabled()
        folder_list.setSortingEnabled(False)
        row = folder_list.rowCount()
        folder_list.insertRow(row)
        item = QTableWidgetItem(path_str)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(
            Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        )
        item.setData(Qt.ItemDataRole.UserRole, (kind, path_str))
        folder_list.setItem(row, 0, item)
        folder_list.setSortingEnabled(was_sorting)
        return item

    for entry in custom_entries:
        _add_entry("custom", str(entry["path"]), bool(entry.get("enabled", True)))

    lay.addWidget(folder_list, 1)

    ctrl_row = QHBoxLayout()
    btn_add = QPushButton(tr("scan_folders.btn_add"))
    btn_remove = QPushButton(tr("scan_folders.btn_remove"))
    btn_remove.setEnabled(False)
    ctrl_row.addWidget(btn_add)
    ctrl_row.addWidget(btn_remove)
    ctrl_row.addStretch()
    lay.addLayout(ctrl_row)

    def _update_remove_state() -> None:
        row = folder_list.currentRow()
        cur = folder_list.item(row, 0) if row >= 0 else None
        data = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        btn_remove.setEnabled(
            isinstance(data, tuple) and len(data) == 2 and data[0] == "custom"
        )

    folder_list.currentCellChanged.connect(lambda *_args: _update_remove_state())

    def _on_add_folder() -> None:
        chosen = QFileDialog.getExistingDirectory(
            dlg, tr("scan_folders.pick_title"), str(Path.home())
        )
        if not chosen:
            return
        chosen_str = str(Path(chosen))
        for i in range(folder_list.rowCount()):
            existing = folder_list.item(i, 0)
            if existing is None:
                continue
            data = existing.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and data[1] == chosen_str:
                folder_list.selectRow(i)
                return
        item = _add_entry("custom", chosen_str, True)
        found_row = folder_list.row(item)
        if found_row >= 0:
            folder_list.selectRow(found_row)
        _apply()

    def _on_remove_folder() -> None:
        row = folder_list.currentRow()
        if row < 0:
            return
        cur = folder_list.item(row, 0)
        data = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        if isinstance(data, tuple) and data[0] == "custom":
            folder_list.removeRow(row)
            _apply()

    btn_add.clicked.connect(_on_add_folder)
    btn_remove.clicked.connect(_on_remove_folder)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)

    def _apply() -> None:
        new_disabled: list[str] = []
        new_custom: list[dict[str, object]] = []
        for i in range(folder_list.rowCount()):
            it = folder_list.item(i, 0)
            if it is None:
                continue
            data = it.data(Qt.ItemDataRole.UserRole)
            if not (isinstance(data, tuple) and len(data) == 2):
                continue
            kind, p_str = data
            checked = it.checkState() == Qt.CheckState.Checked
            if kind == "default":
                if not checked:
                    new_disabled.append(str(p_str))
            elif kind == "custom":
                new_custom.append({"path": str(p_str), "enabled": checked})
        win._save_scan_folders(new_disabled, new_custom)
        win._refresh_games()

    folder_list.itemChanged.connect(lambda _item: _apply())
    buttons.rejected.connect(dlg.accept)
    lay.addWidget(buttons)

    dlg.exec()


def open_configure_game_dialog(win: MainWindow) -> None:
    """Open a unified 3-tab configuration dialog for the selected game."""
    sel = win._selected_library_item()
    if not sel:
        return
    kind, obj = sel
    is_steam = kind == "steam" and isinstance(obj, ShortcutEntry)
    is_standalone = kind == "standalone" and isinstance(obj, StandaloneGame)
    is_external = kind == "external" and isinstance(obj, ExternalLauncherGame)
    is_native = _is_native_linux_game(kind, obj)
    if isinstance(obj, ShortcutEntry):
        title_name = obj.app_name
        exe_str = str(obj.exe)
        dir_str = str(obj.start_dir)
    else:
        title_name = obj.name
        exe_str = str(obj.exe_path)
        dir_str = str(obj.game_dir)

    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("configure.title", name=title_name))
    dlg.resize(760, 560)
    dlg_alive = _dialog_guard(dlg)
    root_lay = QVBoxLayout(dlg)

    tabs = QTabWidget(dlg)
    root_lay.addWidget(tabs, 1)

    # --- Tab 1: Game & Executable ---
    tab_game = QWidget()
    game_form = QFormLayout(tab_game)
    game_form.addRow(tr("configure.lbl_name"), QLabel(title_name))
    game_form.addRow(tr("configure.lbl_type"), QLabel(kind))
    lbl_exe = QLabel(exe_str)
    lbl_exe.setWordWrap(True)
    lbl_dir = QLabel(dir_str)
    lbl_dir.setWordWrap(True)
    game_form.addRow(tr("configure.lbl_exe"), lbl_exe)
    game_form.addRow(tr("configure.lbl_dir"), lbl_dir)

    initial_opts_text = (
        load_external_launch_presets(obj)
        if isinstance(obj, ExternalLauncherGame)
        else str(getattr(obj, "launch_options", "") or "")
    )
    opts_edit = QLineEdit(initial_opts_text)
    opts_edit.setReadOnly(True)
    game_form.addRow(tr("configure.lbl_launch_opts"), opts_edit)

    btn_edit_opts = QPushButton(tr("configure.btn_edit_launch_opts"))
    btn_edit_opts.setEnabled(is_steam or is_standalone or is_external)

    def _on_edit_opts() -> None:
        win._edit_launch_options_presets()
        if not dlg_alive():
            return
        if isinstance(obj, ExternalLauncherGame):
            opts_edit.setText(load_external_launch_presets(obj))
            return
        cur = win._selected_library_item()
        if cur is not None:
            opts_edit.setText(str(getattr(cur[1], "launch_options", "") or ""))

    btn_edit_opts.clicked.connect(_on_edit_opts)
    game_form.addRow("", btn_edit_opts)

    if is_steam and win.paths and isinstance(obj, ShortcutEntry):
        steam_input_combo = QComboBox()
        steam_input_combo.addItem(tr("configure.steam_input_default"), "default")
        steam_input_combo.addItem(tr("configure.steam_input_enabled"), "enabled")
        steam_input_combo.addItem(tr("configure.steam_input_disabled"), "disabled")
        cur_si_mode = get_steam_input_mode(win.paths.localconfig_vdf, obj.appid_u32)
        si_idx = steam_input_combo.findData(cur_si_mode)
        if si_idx >= 0:
            steam_input_combo.setCurrentIndex(si_idx)

        def _on_steam_input_changed() -> None:
            if not win.paths:
                return
            mode = str(steam_input_combo.currentData() or "default")
            localconfig_path = win.paths.localconfig_vdf
            appid = obj.appid_u32
            steam_input_combo.setEnabled(False)

            class _SteamInputWorker(QThread):
                finished_ok = Signal(str)
                failed = Signal(str)

                def run(self) -> None:
                    try:
                        set_steam_input_mode(localconfig_path, appid, mode, create_if_missing=True)
                        self.finished_ok.emit(mode)
                    except Exception as exc:
                        self.failed.emit(_format_worker_error(exc))

            si_worker = _SteamInputWorker()
            win._track_worker(si_worker)

            def _si_ok(saved_mode: str) -> None:
                if dlg_alive():
                    steam_input_combo.setEnabled(True)
                win.status_label.setText(tr("configure.steam_input_saved", mode=saved_mode))

            def _si_err(err: str) -> None:
                if dlg_alive():
                    steam_input_combo.setEnabled(True)
                    QMessageBox.warning(dlg, __app_name__, err)

            si_worker.finished_ok.connect(_si_ok)
            si_worker.failed.connect(_si_err)
            si_worker.start()

        steam_input_combo.currentIndexChanged.connect(_on_steam_input_changed)
        game_form.addRow(tr("configure.lbl_steam_input"), steam_input_combo)

    tabs.addTab(tab_game, tr("configure.tab_game"))

    # --- Tab 2: Runner & Proton ---
    tab_runner = QWidget()
    runner_lay = QVBoxLayout(tab_runner)
    lbl_cur_proton: QLabel | None = None
    lbl_umu_badge: QLabel | None = None
    if is_native:
        lbl_native = QLabel(tr("configure.native_no_proton"))
        lbl_native.setWordWrap(True)
        runner_lay.addWidget(lbl_native)
        runner_lay.addStretch()
    else:
        lbl_cur_proton = QLabel(tr("configure.loading"))
        runner_form = QFormLayout()
        runner_form.addRow(tr("configure.lbl_current_proton"), lbl_cur_proton)
        if is_standalone:
            lbl_umu_badge = QLabel(tr("configure.loading"))
            lbl_umu_badge.setWordWrap(True)
            runner_form.addRow(tr("configure.lbl_umu_status"), lbl_umu_badge)
        runner_lay.addLayout(runner_form)

        btn_change_proton = QPushButton(tr("library.btn_proton"))
        btn_change_proton.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        runner_lay.addWidget(btn_change_proton)

        btn_fallback10 = QPushButton(tr("library.btn_fallback_proton10"))
        btn_fallback10.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        btn_fallback10.clicked.connect(win._fallback_selected_proton10)
        runner_lay.addWidget(btn_fallback10)
        runner_lay.addStretch()
    tabs.addTab(tab_runner, tr("configure.tab_runner"))

    # --- Tab 3: Prefix & Tools ---
    tab_prefix = QWidget()
    prefix_lay = QVBoxLayout(tab_prefix)
    pfx_label: QLabel | None = None
    win32_warn: QLabel | None = None
    btn_wt: QPushButton | None = None
    btn_open_pfx: QPushButton | None = None
    lbl_compat_storage: QLabel | None = None
    btn_compat_migrate: QPushButton | None = None
    compat_state_ref: dict[str, object] = {"state": "missing", "targets": []}
    resolved_pfx_ref: list[Path | None] = [None]
    if is_native:
        lbl_no_pfx = QLabel(tr("configure.native_no_prefix"))
        lbl_no_pfx.setWordWrap(True)
        prefix_lay.addWidget(lbl_no_pfx)
        prefix_lay.addStretch()
    else:
        pfx_label = QLabel(tr("configure.loading"))
        pfx_label.setWordWrap(True)
        pfx_form = QFormLayout()
        pfx_form.addRow(tr("configure.lbl_prefix_path"), pfx_label)
        if is_steam and win.paths:
            lbl_compat_storage = QLabel(tr("configure.loading"))
            lbl_compat_storage.setWordWrap(True)
            pfx_form.addRow(tr("configure.lbl_compat_storage"), lbl_compat_storage)
        prefix_lay.addLayout(pfx_form)

        win32_warn = QLabel("")
        win32_warn.setWordWrap(True)
        win32_warn.setStyleSheet("color: #ffd180;")
        win32_warn.setVisible(False)
        prefix_lay.addWidget(win32_warn)

        btn_wt = QPushButton(tr("library.btn_winetricks"))
        btn_wt.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        btn_wt.clicked.connect(lambda: win._open_winetricks_dialog(kind, obj))
        prefix_lay.addWidget(btn_wt)

        btn_opti = QPushButton(tr("library.btn_optiscaler"))
        btn_opti.setEnabled((is_steam and bool(win.paths)) or is_standalone or kind == "external")
        btn_opti.clicked.connect(lambda: win._open_optiscaler_dialog(kind, obj))
        prefix_lay.addWidget(btn_opti)

        btn_run_exe = QPushButton(tr("library.btn_run_exe_in_prefix"))
        btn_run_exe.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        btn_run_exe.clicked.connect(lambda: win._run_exe_in_prefix_dialog(kind, obj))
        prefix_lay.addWidget(btn_run_exe)

        btn_open_pfx = QPushButton(tr("configure.btn_open_prefix"))
        btn_open_pfx.setEnabled(False)
        btn_open_pfx.clicked.connect(
            lambda: resolved_pfx_ref[0] is not None
            and QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved_pfx_ref[0])))
        )
        prefix_lay.addWidget(btn_open_pfx)

        if is_steam and win.paths and isinstance(obj, ShortcutEntry):
            btn_compat_migrate = QPushButton(tr("configure.btn_compat_migrate"))
            btn_compat_migrate.setEnabled(False)
            prefix_lay.addWidget(btn_compat_migrate)

        prefix_lay.addStretch()
    tabs.addTab(tab_prefix, tr("configure.tab_prefix"))

    tab_organization = QWidget()
    organization_layout = QVBoxLayout(tab_organization)
    for action in (
        win.act_migrate, win.act_export_standalone, win.act_import_steam,
        win.act_ext_to_steam, win.act_ext_to_standalone, win.act_ext_import_saves,
        win.act_art, win.act_revert_art,
    ):
        if action.isVisible() and action.isEnabled():
            button = QPushButton(action.text())
            button.setEnabled(action.isEnabled())
            def run_action(_checked=False, selected_action=action):
                dlg.accept()
                selected_action.trigger()
            button.clicked.connect(run_action)
            organization_layout.addWidget(button)
    if organization_layout.count():
        organization_layout.addStretch()
        tabs.addTab(tab_organization, tr("configure.tab_organization"))

    if not is_native:
        paths_snap = win.paths

        class _ConfigureLoadWorker(QThread):
            ready = Signal(str, object, bool, bool, str, str, object)

            def run(self) -> None:
                if isinstance(obj, ShortcutEntry) and is_steam and paths_snap:
                    proton_text = (
                        get_shortcut_proton(paths_snap.config_vdf, obj.appid_u32)
                        or tr("proton.default_label")
                    )
                elif isinstance(obj, StandaloneGame) and is_standalone:
                    proton_text = obj.proton_name or tr("proton.default_label")
                else:
                    proton_text = str(getattr(obj, "runner", "") or tr("proton.default_label"))

                resolved_pfx: Path | None = None
                try:
                    resolved_pfx, _ = resolve_game_prefix(kind, obj, paths_snap)
                except Exception:
                    resolved_pfx = getattr(obj, "prefix_dir", None)
                win32_flag = resolved_pfx is not None and is_win32_prefix(resolved_pfx)
                pfx_exists = resolved_pfx is not None and resolved_pfx.is_dir()

                umu_text = ""
                if isinstance(obj, StandaloneGame) and is_standalone:
                    game_id = f"umu-{obj.steam_appid}" if obj.steam_appid else "umu-default"
                    st_umu = get_umu_protonfix_status(game_id)
                    if st_umu.get("has_protonfix"):
                        umu_text = tr(
                            "configure.umu_protonfix_active",
                            game_id=str(st_umu.get("umu_id") or game_id),
                        )
                    elif st_umu.get("found"):
                        umu_text = tr(
                            "configure.umu_db_found",
                            game_id=str(st_umu.get("umu_id") or game_id),
                        )
                    else:
                        umu_text = tr("configure.umu_default", game_id=game_id)

                compat_state = "missing"
                compat_targets: list[Path] = []
                if isinstance(obj, ShortcutEntry) and is_steam and paths_snap:
                    compat_state, _ = get_compatdata_migration_state(paths_snap, obj.appid_u32)
                    compat_targets = list_microsd_compatdata_targets(steam_root=paths_snap.steam_root)

                self.ready.emit(
                    proton_text,
                    resolved_pfx,
                    win32_flag,
                    pfx_exists,
                    umu_text,
                    compat_state,
                    compat_targets,
                )

        def _apply_loaded(
            proton_text: str,
            resolved_pfx: Path | None,
            win32_flag: bool,
            pfx_exists: bool,
            umu_text: str,
            compat_state: str,
            compat_targets: list[Path],
        ) -> None:
            if not dlg_alive() or win._closing_pending:
                return
            if lbl_cur_proton is not None:
                lbl_cur_proton.setText(proton_text)
            if lbl_umu_badge is not None:
                lbl_umu_badge.setText(umu_text or "-")
            if pfx_label is not None:
                pfx_label.setText(str(resolved_pfx) if resolved_pfx else "-")
            if win32_warn is not None:
                if win32_flag and resolved_pfx is not None:
                    win32_warn.setText(tr("prefix.win32_unsupported", path=str(resolved_pfx)))
                    win32_warn.setVisible(True)
                else:
                    win32_warn.setVisible(False)
            if btn_wt is not None:
                btn_wt.setEnabled((is_steam and bool(win.paths)) or is_standalone or pfx_exists)
            resolved_pfx_ref[0] = resolved_pfx
            if btn_open_pfx is not None:
                btn_open_pfx.setEnabled(pfx_exists)

            compat_state_ref["state"] = compat_state
            compat_state_ref["targets"] = compat_targets
            if lbl_compat_storage is not None:
                state_labels = {
                    "local": tr("configure.compat_state_local"),
                    "microsd": tr("configure.compat_state_microsd"),
                    "broken_symlink": tr("configure.compat_state_broken_symlink"),
                    "symlink": tr("configure.compat_state_symlink"),
                    "missing": tr("configure.compat_state_missing"),
                }
                lbl_compat_storage.setText(state_labels.get(compat_state, tr("configure.compat_state_missing")))
            if btn_compat_migrate is not None:
                if compat_state in ("microsd", "symlink"):
                    btn_compat_migrate.setText(tr("configure.btn_compat_restore"))
                    btn_compat_migrate.setEnabled(True)
                elif compat_state == "local":
                    btn_compat_migrate.setText(tr("configure.btn_compat_migrate"))
                    btn_compat_migrate.setEnabled(bool(compat_targets))
                else:
                    btn_compat_migrate.setText(tr("configure.btn_compat_migrate"))
                    btn_compat_migrate.setEnabled(False)

        def _start_config_loader() -> None:
            loader = _ConfigureLoadWorker()
            win._track_worker(loader)
            loader.ready.connect(_apply_loaded)
            loader.start()

        if btn_compat_migrate is not None and isinstance(obj, ShortcutEntry) and paths_snap is not None:
            paths_local = paths_snap

            def _on_compat_migrate_clicked() -> None:
                st_now = str(compat_state_ref.get("state") or "missing")
                raw_targets = compat_state_ref.get("targets")
                targets_now: list[Path] = (
                    [p for p in raw_targets if isinstance(p, Path)]
                    if isinstance(raw_targets, list)
                    else []
                )
                if st_now in ("microsd", "symlink"):
                    ans = QMessageBox.question(
                        dlg,
                        __app_name__,
                        tr("configure.compat_restore_confirm", name=title_name),
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                    op_mode = "restore"
                    chosen_target: Path | None = None
                elif st_now == "local" and targets_now:
                    if len(targets_now) == 1:
                        chosen_target = targets_now[0]
                    else:
                        items = [str(p) for p in targets_now]
                        picked, ok = QInputDialog.getItem(
                            dlg,
                            __app_name__,
                            tr("configure.compat_pick_target"),
                            items,
                            0,
                            False,
                        )
                        if not ok or not picked:
                            return
                        chosen_target = Path(picked)
                    ans = QMessageBox.question(
                        dlg,
                        __app_name__,
                        tr("configure.compat_migrate_confirm", name=title_name, target=str(chosen_target)),
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                    op_mode = "migrate"
                else:
                    return

                btn_compat_migrate.setEnabled(False)
                appid = obj.appid_u32

                class _CompatMigrateWorker(QThread):
                    finished_ok = Signal(str)
                    failed = Signal(str)

                    def run(self) -> None:
                        try:
                            if op_mode == "restore":
                                restore_compatdata_from_microsd(paths_local, appid)
                                self.finished_ok.emit(tr("configure.compat_restored", name=title_name))
                            elif chosen_target is not None:
                                migrate_compatdata_to_microsd(paths_local, appid, chosen_target)
                                self.finished_ok.emit(tr("configure.compat_migrated", name=title_name))
                        except Exception as exc:
                            self.failed.emit(_format_worker_error(exc))

                cm_worker = _CompatMigrateWorker()
                win._track_worker(cm_worker)

                def _cm_ok(msg: str) -> None:
                    win.status_label.setText(msg)
                    if dlg_alive():
                        _start_config_loader()
                        QMessageBox.information(dlg, __app_name__, msg)

                def _cm_err(err: str) -> None:
                    if dlg_alive():
                        _start_config_loader()
                        QMessageBox.critical(dlg, __app_name__, err)

                cm_worker.finished_ok.connect(_cm_ok)
                cm_worker.failed.connect(_cm_err)
                cm_worker.start()

            btn_compat_migrate.clicked.connect(_on_compat_migrate_clicked)

        def _on_change_proton() -> None:
            win._change_selected_proton()
            if dlg_alive():
                _start_config_loader()

        btn_change_proton.clicked.connect(_on_change_proton)
        _start_config_loader()

    btn_close = QPushButton(tr("configure.btn_close"))
    btn_close.clicked.connect(dlg.accept)
    close_row = QHBoxLayout()
    close_row.addStretch()
    close_row.addWidget(btn_close)
    root_lay.addLayout(close_row)

    dlg.exec()
