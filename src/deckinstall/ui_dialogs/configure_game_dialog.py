"""Game configuration and scan-folders management dialogs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
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
from ..modes import STANDALONE, STEAM, feature_note
from ..proton_policy import (
    DEFAULT_PROTON_ID,
    newest_ge_proton,
    previous_proton_name,
    set_game_proton,
    use_previous_proton,
)
from ..runtime_manager import (
    get_umu_protonfix_status,
    is_win32_prefix,
    list_installed_compat_tools,
    resolve_game_prefix,
)
from ..scanner import scan_game_folder
from ..standalone import (
    StandaloneGame,
    change_standalone_exe,
    find_game_launchers,
    game_run_logs,
    repaired_shortcut_start_dir,
    set_game_launcher,
)
from ..steam_paths import list_installed_protons
from ..steam_vdf import (
    ShortcutEntry,
    get_compatdata_migration_state,
    get_shortcut_proton,
    list_microsd_compatdata_targets,
    migrate_compatdata_to_microsd,
    remove_compat_tool,
    restore_compatdata_from_microsd,
    set_shortcut_proton,
)
from ..ui_cards import (
    _BusyGuardedDialog,
    _dialog_guard,
    _dialog_parent,
    _format_worker_error,
    _is_native_linux_game,
    _is_wine_game_running,
    _pick_dir_async,
    action_grid,
    action_row,
    dialog_footer,
    get_game_log_path,
    note_label,
    section_card,
)
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit

STEAM_DEFAULT_PROTON_ID = "__steam_default__"

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
    removed_defaults = set(win._scan_folder_set("scan_folders/removed_defaults"))

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

    def _populate() -> None:
        folder_list.blockSignals(True)
        folder_list.setRowCount(0)
        default_entries, custom_entries = win._load_scan_folders()
        for path, enabled in default_entries:
            item = _add_entry("default", str(path), enabled)
            if path == Path.home():
                item.setText(tr("scan_folders.home_entry", path=str(path)))
        for entry in custom_entries:
            _add_entry("custom", str(entry["path"]), bool(entry.get("enabled", True)))
        folder_list.blockSignals(False)

    _populate()

    lay.addWidget(folder_list, 1)

    ctrl_row = QHBoxLayout()
    btn_add = QPushButton(tr("scan_folders.btn_add"))
    btn_remove = QPushButton(tr("scan_folders.btn_remove"))
    btn_remove.setEnabled(False)
    btn_restore = QPushButton(tr("scan_folders.btn_restore"))
    ctrl_row.addWidget(btn_add)
    ctrl_row.addWidget(btn_remove)
    ctrl_row.addWidget(btn_restore)
    ctrl_row.addStretch()
    lay.addLayout(ctrl_row)

    def _update_remove_state() -> None:
        row = folder_list.currentRow()
        cur = folder_list.item(row, 0) if row >= 0 else None
        data = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        btn_remove.setEnabled(isinstance(data, tuple) and len(data) == 2)

    folder_list.currentCellChanged.connect(lambda *_args: _update_remove_state())

    def _on_add_folder() -> None:
        def _picked(chosen: str) -> None:
            if not chosen or not dlg_alive_sf():
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

        _pick_dir_async(dlg, tr("scan_folders.pick_title"), Path.home(), _picked)

    dlg_alive_sf = _dialog_guard(dlg)

    def _on_remove_folder() -> None:
        row = folder_list.currentRow()
        if row < 0:
            return
        cur = folder_list.item(row, 0)
        data = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        if isinstance(data, tuple) and len(data) == 2:
            if data[0] == "default":
                removed_defaults.add(str(data[1]))
            folder_list.removeRow(row)
            _apply()
            _update_remove_state()

    def _on_restore_defaults() -> None:
        removed_defaults.clear()
        win._save_scan_folders([], win._load_scan_folders()[1], [])
        _populate()
        _update_remove_state()
        win._refresh_games()

    btn_add.clicked.connect(_on_add_folder)
    btn_remove.clicked.connect(_on_remove_folder)
    btn_restore.clicked.connect(_on_restore_defaults)

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
        win._save_scan_folders(new_disabled, new_custom, sorted(removed_defaults))
        win._refresh_games()

    folder_list.itemChanged.connect(lambda _item: _apply())
    buttons.rejected.connect(dlg.accept)
    lay.addWidget(buttons)

    dlg.exec()
    dlg.deleteLater()


def _launcher_checkboxes(win: MainWindow, dlg: QDialog, game: StandaloneGame, steam_mode: bool = False) -> QWidget:
    """Desktop / app-menu launcher boxes, checked by what really exists; toggling creates or removes.

    steam_mode: the game opens from Steam, so a launcher can only be removed (left over from before).
    """
    box = QWidget()
    outer = QVBoxLayout(box)
    outer.setContentsMargins(0, 0, 0, 0)
    lay = QHBoxLayout()
    outer.addLayout(lay)
    if steam_mode:
        note = QLabel(feature_note("app_shortcuts", STEAM))
        note.setObjectName("MutedText")
        note.setWordWrap(True)
        outer.addWidget(note)
    try:
        found = find_game_launchers(game)
    except OSError:
        found = {"menu": [], "desktop": []}
    for where, label in (
        ("desktop", tr("configure.chk_desktop_launcher")),
        ("menu", tr("configure.chk_menu_launcher")),
    ):
        chk = QCheckBox(label)
        chk.setChecked(bool(found.get(where)))
        if steam_mode and not chk.isChecked():
            chk.setEnabled(False)

        def _toggled(checked: bool, w: str = where, c: QCheckBox = chk) -> None:
            try:
                set_game_launcher(game, w, checked)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dlg, __app_name__, tr("configure.launcher_failed", err=str(exc)))
                c.blockSignals(True)
                c.setChecked(not checked)
                c.blockSignals(False)
                return
            win.status_label.setText(
                tr("configure.launcher_created", name=game.name)
                if checked
                else tr("configure.launcher_removed", name=game.name)
            )

        chk.toggled.connect(_toggled)
        lay.addWidget(chk)
    lay.addStretch()
    return box


def _mode_switch_row(win: MainWindow, dlg: QDialog, game: StandaloneGame, label: QLabel, is_steam: bool) -> QWidget:
    """The game's mode and one button that moves it to the other mode (greyed out, with the
    reason, when this system does not offer that mode)."""
    target = STANDALONE if is_steam else STEAM
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(label)
    btn = QPushButton(tr("mode.btn_switch_standalone") if is_steam else tr("mode.btn_switch_steam"))
    blocked = win.mode_env.mode_note(target)
    btn.setEnabled(not blocked and not win._steam_busy)
    btn.setToolTip(blocked or (tr("mode.desc_standalone") if is_steam else tr("mode.desc_steam")))
    btn.clicked.connect(lambda: win._switch_game_mode(game, target, on_done=dlg.accept))
    lay.addWidget(btn)
    lay.addStretch()
    return box


def _edit_launch_hooks(win: MainWindow, parent: QDialog, game: StandaloneGame) -> None:
    """Commands to run before and after a Standalone game (stored in its manifest, run by launch.sh)."""
    from ..standalone import launch_hooks, set_launch_hooks

    pre, post = launch_hooks(game)
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("hooks.title", name=game.name))
    lay = QVBoxLayout(dlg)
    intro = QLabel(tr("hooks.intro"))
    intro.setWordWrap(True)
    lay.addWidget(intro)
    form = QFormLayout()
    pre_edit, post_edit = QLineEdit(pre), QLineEdit(post)
    pre_edit.setPlaceholderText("pactl set-default-sink headphones")
    post_edit.setPlaceholderText("notify-send 'Game closed'")
    form.addRow(tr("hooks.before"), pre_edit)
    form.addRow(tr("hooks.after"), post_edit)
    lay.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    try:
        set_launch_hooks(game, pre_edit.text(), post_edit.text())
    except (OSError, ValueError) as exc:
        QMessageBox.warning(parent, __app_name__, str(exc))
        return
    win.status_label.setText(tr("hooks.saved", name=game.name))


def open_configure_game_dialog(win: MainWindow) -> None:
    """Configure the selected game: three tabs by purpose (the game, running it, tools)."""
    sel = win._selected_library_item()
    if not sel:
        return
    kind, obj = sel
    is_steam = kind == "steam" and isinstance(obj, ShortcutEntry)
    is_standalone = kind == "standalone" and isinstance(obj, StandaloneGame)
    is_external = kind == "external" and isinstance(obj, ExternalLauncherGame)
    managed_game = (
        win._managed_library_game(obj) if is_steam and isinstance(obj, ShortcutEntry) else None
    )
    sa_obj: StandaloneGame | None = obj if isinstance(obj, StandaloneGame) else managed_game
    is_native = _is_native_linux_game(kind, obj)
    if isinstance(obj, ShortcutEntry):
        title_name = sa_obj.name if sa_obj is not None else obj.app_name
        exe_str = str(sa_obj.exe_path) if sa_obj is not None else str(obj.exe)
        dir_str = str(sa_obj.game_dir) if sa_obj is not None else str(obj.start_dir)
    else:
        title_name = obj.name
        exe_str = str(obj.exe_path)
        dir_str = str(obj.game_dir)

    operation = {"migrating": False}
    dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: operation["migrating"])
    dlg.setWindowTitle(tr("configure.title", name=title_name))
    dlg.resize(740, 560)
    dlg_alive = _dialog_guard(dlg)
    root_lay = QVBoxLayout(dlg)

    tabs = QTabWidget(dlg)
    root_lay.addWidget(tabs, 1)

    def _tab(title: str) -> QVBoxLayout:
        """One tab: cards stacked in a scroll area (the Deck screen is short)."""
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        tabs.addTab(scroll, title)
        return lay

    def _with_button(label: QLabel, button: QPushButton) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(label, 1)
        row.addWidget(button)
        return box

    # --- Tab 1: the game (its files, how it runs, its shortcuts) ---
    game_tab_lay = _tab(tr("configure.tab_game"))
    files_card, files_lay, _top = section_card(tr("configure.card_files"))
    game_form = QFormLayout()
    files_lay.addLayout(game_form)
    lbl_exe = QLabel(exe_str)
    lbl_exe.setWordWrap(True)
    lbl_dir = QLabel(dir_str)
    lbl_dir.setWordWrap(True)
    if sa_obj is not None:
        # One click switches the game to another executable found in its folder.
        exe_combo = QComboBox()
        exe_combo.setToolTip(tr("configure.exe_switch_tooltip"))
        detected = scan_game_folder(sa_obj.game_dir)
        candidates = [sa_obj.exe_path, *(e for e in (detected.executables if detected else []) if e != sa_obj.exe_path)]
        for cand in candidates[:40]:
            try:
                label = str(cand.relative_to(sa_obj.game_dir))
            except ValueError:
                label = str(cand)
            exe_combo.addItem(label, str(cand))
        game_obj = sa_obj

        def _on_exe_activated(idx: int) -> None:
            new_exe = Path(str(exe_combo.itemData(idx)))
            if new_exe == game_obj.exe_path:
                return
            if win._steam_busy:
                exe_combo.setCurrentIndex(exe_combo.findData(str(game_obj.exe_path)))
                return
            try:
                updated = change_standalone_exe(game_obj.game_dir, new_exe)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dlg, __app_name__, tr("configure.exe_switch_failed", err=str(exc)))
                exe_combo.setCurrentIndex(exe_combo.findData(str(game_obj.exe_path)))
                return
            game_obj.exe_path = updated.exe_path
            if is_steam and isinstance(obj, ShortcutEntry) and win.paths is not None:
                win.enqueue_vdf_edit(
                    QueuedVdfEdit(
                        f"update_shortcut_paths:{win.paths.user_id}:{obj.appid_u32}",
                        "update_shortcut_paths",
                        obj.appid_u32,
                        obj.app_name,
                        {
                            "exe_path": str(updated.exe_path),
                            "start_dir": str(repaired_shortcut_start_dir(obj, updated)),
                            "expected_exe": obj.exe,
                            "user_id": win.paths.user_id,
                            "steam_root": str(win.paths.steam_root),
                            "allow_existing_old": True,
                        },
                    )
                )
            msg = tr("configure.exe_switched", name=new_exe.name)
            if updated.meta.get("steam"):
                msg += " " + tr("configure.exe_switched_steam_note")
            win.status_label.setText(msg)
            win._refresh_shortcuts()

        exe_combo.activated.connect(_on_exe_activated)
        game_form.addRow(tr("configure.lbl_exe"), exe_combo)
    else:
        game_form.addRow(tr("configure.lbl_exe"), lbl_exe)
    btn_open_dir = QPushButton(tr("configure.btn_open_folder"))
    btn_open_dir.setEnabled(Path(dir_str).is_dir())
    btn_open_dir.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(dir_str)))
    game_form.addRow(tr("configure.lbl_dir"), _with_button(lbl_dir, btn_open_dir))
    game_tab_lay.addWidget(files_card)

    mode_card, mode_lay, _top = section_card(tr("configure.card_mode"))
    mode_form = QFormLayout()
    mode_lay.addLayout(mode_form)
    if is_steam or is_standalone:
        lbl_mode = QLabel(tr("mode.steam") if is_steam else tr("mode.standalone"))
        lbl_mode.setToolTip(tr("mode.desc_steam") if is_steam else tr("mode.desc_standalone"))
        if sa_obj is None:
            mode_form.addRow(tr("mode.label"), lbl_mode)
        else:
            mode_form.addRow(tr("mode.label"), _mode_switch_row(win, dlg, sa_obj, lbl_mode, is_steam))
    elif is_external and isinstance(obj, ExternalLauncherGame):
        mode_form.addRow(tr("mode.label"), QLabel(tr("configure.via_launcher", launcher=obj.source.title())))
    if sa_obj is not None:
        mode_form.addRow(tr("configure.lbl_launchers"), _launcher_checkboxes(win, dlg, sa_obj, steam_mode=is_steam))
    if mode_form.rowCount():
        game_tab_lay.addWidget(mode_card)
    game_tab_lay.addStretch()

    # --- Tab 2: running it (Proton, launch options, commands, the log) ---
    run_tab_lay = _tab(tr("configure.tab_run"))
    runner_card, runner_lay, _top = section_card(tr("configure.tab_runner"))
    lbl_cur_proton: QLabel | None = None
    lbl_umu_badge: QLabel | None = None
    proton_combo: QComboBox | None = None
    btn_previous_proton: QPushButton | None = None
    active_proton_ref: list[str] = [""]

    if is_native:
        runner_lay.addWidget(note_label(tr("configure.native_no_proton")))
    else:
        runner_form = QFormLayout()
        if is_steam or is_standalone:
            proton_combo = QComboBox()
            proton_combo.addItem(tr("configure.loading"), "")
            proton_combo.setEnabled(False)
            if sa_obj is not None:
                btn_previous_proton = QPushButton(tr("configure.btn_previous_proton"))
                btn_previous_proton.setEnabled(False)
                proton_row = QWidget()
                proton_row_lay = QHBoxLayout(proton_row)
                proton_row_lay.setContentsMargins(0, 0, 0, 0)
                proton_row_lay.addWidget(proton_combo, 1)
                proton_row_lay.addWidget(btn_previous_proton)
                runner_form.addRow(tr("configure.lbl_current_proton"), proton_row)
            else:
                runner_form.addRow(tr("configure.lbl_current_proton"), proton_combo)
        else:
            lbl_cur_proton = QLabel(tr("configure.loading"))
            runner_form.addRow(tr("configure.lbl_current_proton"), lbl_cur_proton)
        if sa_obj is not None:
            lbl_umu_badge = QLabel(tr("configure.loading"))
            lbl_umu_badge.setWordWrap(True)
            runner_form.addRow(tr("configure.lbl_umu_status"), lbl_umu_badge)
        runner_lay.addLayout(runner_form)
    run_tab_lay.addWidget(runner_card)

    launch_card, launch_lay, _top = section_card(tr("configure.card_launch"), tr("configure.card_launch_hint"))
    initial_opts_text = (
        load_external_launch_presets(obj)
        if isinstance(obj, ExternalLauncherGame)
        else str(getattr(obj, "launch_options", "") or "")
    )
    opts_edit = QLineEdit(initial_opts_text)
    opts_edit.setReadOnly(True)
    opts_edit.setPlaceholderText(tr("configure.no_launch_opts"))
    launch_lay.addWidget(opts_edit)

    btn_edit_opts = QPushButton(tr("configure.btn_edit_launch_opts"))
    btn_edit_opts.setEnabled(is_steam or is_standalone or is_external)

    def _save_displayed_opts(new_opts: str) -> None:
        nonlocal obj
        if dlg_alive():
            if isinstance(obj, ShortcutEntry):
                obj = replace(obj, launch_options=new_opts)
            opts_edit.setText(new_opts)

    def _on_edit_opts() -> None:
        if win._steam_busy:
            return
        # Pass this game and this dialog explicitly: the library selection may differ (or be empty),
        # and without the parent the child can open behind this dialog under gamescope/Wayland.
        win._edit_launch_options_presets((kind, obj), parent=dlg, on_saved=_save_displayed_opts)

    btn_edit_opts.clicked.connect(_on_edit_opts)
    launch_grid = action_grid()
    action_row(launch_grid, 0, btn_edit_opts, tr("configure.edit_opts_hint"))
    if sa_obj is not None and not is_steam:
        btn_hooks = QPushButton(tr("hooks.btn"))
        btn_hooks.clicked.connect(lambda _=False: _edit_launch_hooks(win, dlg, sa_obj))
        action_row(launch_grid, 1, btn_hooks, tr("hooks.hint"))

    legacy_log = get_game_log_path(title_name)
    run_logs = (
        game_run_logs(sa_obj)
        if sa_obj is not None
        else ([legacy_log] if legacy_log.is_file() else [])
    )
    if sa_obj is not None and legacy_log.is_file():
        run_logs.append(legacy_log)  # last run started before logs moved into launch.sh
    btn_view_log = QPushButton(tr("configure.btn_view_log"))
    btn_view_log.setEnabled(bool(run_logs))

    def _read_tail(path: Path) -> str:
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                return fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            return str(exc)

    def _on_view_log() -> None:
        log_dlg = QDialog(dlg)
        log_dlg.setWindowTitle(tr("configure.log_title", name=title_name))
        log_dlg.resize(700, 480)
        log_lay = QVBoxLayout(log_dlg)
        run_combo = QComboBox()
        for i, path in enumerate(run_logs):
            label = tr("configure.log_run_last") if i == 0 else tr("configure.log_run_older", n=i)
            run_combo.addItem(label, str(path))
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)

        def _show_run(_idx: int = 0) -> None:
            viewer.setPlainText(_read_tail(Path(str(run_combo.currentData()))))

        run_combo.currentIndexChanged.connect(_show_run)
        _show_run()
        log_lay.addWidget(run_combo)
        log_lay.addWidget(viewer, 1)
        btn_folder = QPushButton(tr("configure.btn_open_log_folder"))
        btn_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(str(run_combo.currentData())).parent)))
        )
        close_btn = QPushButton(tr("configure.btn_close"))
        close_btn.clicked.connect(log_dlg.accept)
        log_lay.addLayout(dialog_footer(close_btn, left=(btn_folder,)))
        log_dlg.exec()

    btn_view_log.clicked.connect(_on_view_log)
    action_row(launch_grid, 2, btn_view_log, tr("configure.view_log_hint"))
    launch_lay.addLayout(launch_grid)
    run_tab_lay.addWidget(launch_card)
    run_tab_lay.addStretch()

    # --- Tab 3: tools for the Wine prefix, and moving the game elsewhere ---
    prefix_lay = _tab(tr("configure.tab_prefix"))
    pfx_label: QLabel | None = None
    win32_warn: QLabel | None = None
    btn_wt: QPushButton | None = None
    btn_open_pfx: QPushButton | None = None
    lbl_compat_storage: QLabel | None = None
    btn_compat_migrate: QPushButton | None = None
    compat_state_ref: dict[str, object] = {"state": "missing", "targets": []}
    resolved_pfx_ref: list[Path | None] = [None]
    pfx_card, pfx_card_lay, _top = section_card(tr("configure.card_prefix"), tr("configure.card_prefix_hint"))
    if is_native:
        pfx_card_lay.addWidget(note_label(tr("configure.native_no_prefix")))
        prefix_lay.addWidget(pfx_card)
    else:
        pfx_label = QLabel(tr("configure.loading"))
        pfx_label.setWordWrap(True)
        btn_open_pfx = QPushButton(tr("configure.btn_open_folder"))
        btn_open_pfx.setEnabled(False)
        btn_open_pfx.clicked.connect(
            lambda: resolved_pfx_ref[0] is not None
            and QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved_pfx_ref[0])))
        )
        pfx_form = QFormLayout()
        pfx_form.addRow(tr("configure.lbl_prefix_path"), _with_button(pfx_label, btn_open_pfx))
        if is_steam and win.paths and isinstance(obj, ShortcutEntry) and managed_game is None:
            lbl_compat_storage = QLabel(tr("configure.loading"))
            lbl_compat_storage.setWordWrap(True)
            btn_compat_migrate = QPushButton(tr("configure.btn_compat_migrate"))
            btn_compat_migrate.setEnabled(False)
            pfx_form.addRow(tr("configure.lbl_compat_storage"), _with_button(lbl_compat_storage, btn_compat_migrate))
        pfx_card_lay.addLayout(pfx_form)
        win32_warn = note_label(kind="WarningText")
        win32_warn.setVisible(False)
        pfx_card_lay.addWidget(win32_warn)
        prefix_lay.addWidget(pfx_card)

        tools_card, tools_lay, _top = section_card(tr("configure.card_tools"))
        btn_wt = QPushButton(tr("library.btn_winetricks"))
        btn_wt.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        btn_wt.clicked.connect(lambda: win._open_winetricks_dialog(kind, obj))

        btn_opti = QPushButton(tr("library.btn_optiscaler"))
        btn_opti.setEnabled((is_steam and bool(win.paths)) or is_standalone or kind == "external")

        def _open_opti() -> None:
            current = obj
            if isinstance(current, ShortcutEntry) and sa_obj is not None:
                current = replace(current, exe=str(sa_obj.exe_path))
            win._open_optiscaler_dialog(kind, current, on_saved=_save_displayed_opts)

        btn_opti.clicked.connect(_open_opti)
        btn_run_exe = QPushButton(tr("library.btn_run_exe_in_prefix"))
        btn_run_exe.setEnabled((is_steam and bool(win.paths)) or is_standalone)
        btn_run_exe.clicked.connect(lambda: win._run_exe_in_prefix_dialog(kind, obj))
        tools_grid = action_grid()
        action_row(tools_grid, 0, btn_wt, tr("configure.winetricks_hint"))
        action_row(tools_grid, 1, btn_opti, tr("configure.optiscaler_hint"))
        action_row(tools_grid, 2, btn_run_exe, tr("configure.run_exe_hint"))
        tools_lay.addLayout(tools_grid)
        prefix_lay.addWidget(tools_card)

    org_hints = {
        win.act_ext_to_steam: tr("configure.ext_to_steam_hint"),
        win.act_ext_import_saves: tr("configure.ext_import_saves_hint"),
    }
    visible_org_actions = [action for action in org_hints if action.isVisible() and action.isEnabled()]
    if visible_org_actions:
        org_card, org_lay, _top = section_card(tr("configure.tab_organization"))
        org_grid = action_grid()
        for row, action in enumerate(visible_org_actions):
            button = QPushButton(action.text())

            def run_action(_checked=False, selected_action=action):
                dlg.accept()
                selected_action.trigger()

            button.clicked.connect(run_action)
            action_row(org_grid, row, button, org_hints[action])
        org_lay.addLayout(org_grid)
        prefix_lay.addWidget(org_card)
    prefix_lay.addStretch()

    if not is_native:
        paths_snap = win.paths

        class _ConfigureLoadWorker(QThread):
            ready = Signal(str, object, bool, bool, str, str, object, list, str)

            def run(self) -> None:
                raw_proton = ""
                tools: list[tuple[str, str]] = []
                seen_ids: set[str] = set()
                proton_text = tr("proton.default_label")
                resolved_pfx: Path | None = None
                win32_flag = False
                pfx_exists = False
                umu_text = ""
                compat_state = "missing"
                compat_targets: list[Path] = []
                try:
                    if isinstance(obj, ShortcutEntry) and is_steam and paths_snap:
                        raw_proton = get_shortcut_proton(paths_snap.config_vdf, obj.appid_u32)
                        proton_text = raw_proton or tr("proton.default_label")
                        tools.append((STEAM_DEFAULT_PROTON_ID, tr("proton.default_label")))
                        seen_ids.add(STEAM_DEFAULT_PROTON_ID)
                        for tid, disp in list_installed_protons(paths_snap.steam_root):
                            if tid not in seen_ids:
                                seen_ids.add(tid)
                                tools.append((tid, disp))
                        if raw_proton and raw_proton not in seen_ids:
                            tools.insert(1, (raw_proton, raw_proton))
                        raw_proton = raw_proton or STEAM_DEFAULT_PROTON_ID
                    elif isinstance(obj, StandaloneGame) and is_standalone:
                        newest = newest_ge_proton()
                        raw_proton = obj.proton_name or DEFAULT_PROTON_ID
                        proton_text = obj.proton_name or tr("proton.default_label")
                        tools.append((
                            DEFAULT_PROTON_ID,
                            tr("proton.default_newest", name=newest.name if newest else "GE-Proton"),
                        ))
                        seen_ids.add(DEFAULT_PROTON_ID)
                        for ct in list_installed_compat_tools():
                            if ct.tool_id not in seen_ids:
                                seen_ids.add(ct.tool_id)
                                tools.append((ct.tool_id, ct.folder_name or ct.tool_id))
                        if paths_snap:
                            for tid, disp in list_installed_protons(paths_snap.steam_root):
                                if tid not in seen_ids:
                                    seen_ids.add(tid)
                                    tools.append((tid, disp))
                        if raw_proton and raw_proton not in seen_ids:
                            tools.insert(0, (raw_proton, raw_proton))
                    else:
                        proton_text = str(getattr(obj, "runner", "") or tr("proton.default_label"))

                    try:
                        resolved_pfx, _ = resolve_game_prefix(kind, obj, paths_snap)
                    except Exception:
                        resolved_pfx = getattr(obj, "prefix_dir", None)
                    win32_flag = resolved_pfx is not None and is_win32_prefix(resolved_pfx)
                    pfx_exists = resolved_pfx is not None and resolved_pfx.is_dir()

                    if sa_obj is not None:
                        game_id = f"umu-{sa_obj.steam_appid}" if sa_obj.steam_appid else "umu-default"
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

                    if isinstance(obj, ShortcutEntry) and is_steam and paths_snap and managed_game is None:
                        compat_state, _ = get_compatdata_migration_state(paths_snap, obj.appid_u32)
                        compat_targets = list_microsd_compatdata_targets(steam_root=paths_snap.steam_root)
                except Exception:
                    pass

                self.ready.emit(
                    proton_text,
                    resolved_pfx,
                    win32_flag,
                    pfx_exists,
                    umu_text,
                    compat_state,
                    compat_targets,
                    tools,
                    raw_proton,
                )

        def _apply_loaded(
            proton_text: str,
            resolved_pfx: Path | None,
            win32_flag: bool,
            pfx_exists: bool,
            umu_text: str,
            compat_state: str,
            compat_targets: list[Path],
            tools: list[tuple[str, str]],
            raw_proton: str,
        ) -> None:
            if not dlg_alive() or win._closing_pending:
                return
            active_proton_ref[0] = raw_proton
            if lbl_cur_proton is not None:
                lbl_cur_proton.setText(proton_text)
            if proton_combo is not None:
                proton_combo.blockSignals(True)
                proton_combo.clear()
                for tid, disp in tools:
                    proton_combo.addItem(disp, tid)
                cur_idx = proton_combo.findData(raw_proton)
                if cur_idx >= 0:
                    proton_combo.setCurrentIndex(cur_idx)
                proton_combo.blockSignals(False)
                has_real_tools = len(tools) > 1 or any(
                    tid not in ("", STEAM_DEFAULT_PROTON_ID, DEFAULT_PROTON_ID) for tid, _ in tools
                )
                proton_combo.setEnabled(
                    has_real_tools
                    and not win32_flag
                    and ((is_steam and bool(win.paths)) or is_standalone)
                )
            if btn_previous_proton is not None and sa_obj is not None:
                previous = previous_proton_name(sa_obj)
                btn_previous_proton.setEnabled(bool(previous) and not win32_flag)
                btn_previous_proton.setToolTip(
                    tr("configure.previous_proton_tip", name=previous) if previous else ""
                )
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
                lbl_compat_storage.setText(
                    state_labels.get(compat_state, tr("configure.compat_state_missing"))
                )
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

        if proton_combo is not None:

            def _set_proton_controls_enabled(enabled: bool) -> None:
                if not dlg_alive():
                    return
                proton_combo.setEnabled(enabled)
                if btn_previous_proton is not None:
                    btn_previous_proton.setEnabled(enabled)

            def _on_proton_combo_activated(idx: int, use_previous: bool = False) -> None:
                tool_id = "" if use_previous else str(proton_combo.itemData(idx) or "")
                if use_previous and sa_obj is not None:
                    tool_id = previous_proton_name(sa_obj)
                if not tool_id or tool_id == active_proton_ref[0]:
                    return
                auto_snap_enabled = win._is_auto_snapshot_enabled()

                if is_standalone and isinstance(obj, StandaloneGame):
                    sa = obj
                    _set_proton_controls_enabled(False)

                    def _do_sa() -> StandaloneGame:
                        win._auto_snapshot_if_enabled(
                            sa.prefix_dir,
                            sa.name,
                            "proton-change",
                            "proton",
                            enabled=auto_snap_enabled,
                        )
                        return (
                            use_previous_proton(sa.game_dir)
                            if use_previous
                            else set_game_proton(sa.game_dir, "" if tool_id == DEFAULT_PROTON_ID else tool_id)
                        )

                    worker = SteamOpWorker(_do_sa, need_steam_closed=False)
                    win._track_worker(worker)
                    worker.status.connect(win.status_label.setText)

                    def _sa_ok(res: object) -> None:
                        if isinstance(res, StandaloneGame):
                            sa.proton_name = res.proton_name
                            sa.meta = res.meta
                        active_proton_ref[0] = sa.proton_name or DEFAULT_PROTON_ID
                        win._refresh_shortcuts()
                        msg = tr(
                            "proton.updated_standalone",
                            name=sa.name,
                            tool=sa.proton_name or tr("proton.default_label"),
                        )
                        win.status_label.setText(msg)
                        if dlg_alive():
                            _set_proton_controls_enabled(True)
                            _start_config_loader()
                            QMessageBox.information(dlg, __app_name__, msg)

                    def _sa_err(err: str) -> None:
                        if dlg_alive():
                            _set_proton_controls_enabled(True)
                            _start_config_loader()
                            QMessageBox.critical(dlg, __app_name__, tr("proton.error", err=err))

                    worker.finished_ok.connect(_sa_ok)
                    worker.failed.connect(_sa_err)
                    worker.start()
                    return

                if is_steam and isinstance(obj, ShortcutEntry) and paths_snap is not None:
                    entry = obj
                    paths_local_p = paths_snap
                    pfx_dir = paths_local_p.compatdata_dir / str(entry.appid_u32) / "pfx"
                    target_steam_tool = "" if tool_id == STEAM_DEFAULT_PROTON_ID else tool_id
                    display_tool = target_steam_tool or tr("proton.default_label")
                    _set_proton_controls_enabled(False)

                    def _proceed_steam_proton() -> None:
                        if not dlg_alive() or win._closing_pending:
                            return

                        queued = win.should_queue_vdf_edits()  # Gaming Mode: save it for when Steam closes
                        win._begin_busy()

                        def _do_steam() -> StandaloneGame | None:
                            win._auto_snapshot_if_enabled(
                                pfx_dir,
                                entry.app_name,
                                "proton-change",
                                "proton",
                                enabled=auto_snap_enabled,
                            )
                            before = get_shortcut_proton(paths_local_p.config_vdf, entry.appid_u32)
                            if not queued:
                                if target_steam_tool:
                                    set_shortcut_proton(paths_local_p.config_vdf, entry.appid_u32, target_steam_tool)
                                else:
                                    remove_compat_tool(paths_local_p.config_vdf, entry.appid_u32)
                            if managed_game is None:
                                return None
                            try:
                                if use_previous:
                                    return use_previous_proton(managed_game.game_dir, previous_tool=before)
                                return set_game_proton(managed_game.game_dir, target_steam_tool, previous_tool=before)
                            except Exception:
                                if not queued:
                                    if before:
                                        set_shortcut_proton(paths_local_p.config_vdf, entry.appid_u32, before)
                                    else:
                                        remove_compat_tool(paths_local_p.config_vdf, entry.appid_u32)
                                raise

                        worker = SteamOpWorker(_do_steam, need_steam_closed=not queued)
                        win._track_worker(worker)
                        worker.status.connect(win.status_label.setText)

                        def _st_ok(res: object) -> None:
                            win._end_busy()
                            if managed_game is not None and isinstance(res, StandaloneGame):
                                managed_game.proton_name = res.proton_name
                                managed_game.meta = res.meta
                            active_proton_ref[0] = tool_id
                            msg = tr("proton.updated", name=entry.app_name, tool=display_tool)
                            if queued:
                                win.enqueue_vdf_edit(
                                    QueuedVdfEdit(
                                        f"set_proton:{entry.appid_u32}",
                                        "set_proton",
                                        entry.appid_u32,
                                        entry.app_name,
                                        {"tool_name": target_steam_tool},
                                    )
                                )
                                msg = tr("vdf_queue.enqueued", name=entry.app_name)
                            win._refresh_shortcuts()
                            win.status_label.setText(msg)
                            if dlg_alive():
                                _set_proton_controls_enabled(True)
                                _start_config_loader()
                                QMessageBox.information(dlg, __app_name__, msg)

                        def _st_err(err: str) -> None:
                            win._end_busy()
                            if dlg_alive():
                                _set_proton_controls_enabled(True)
                                _start_config_loader()
                            if not win._closing_pending:
                                QMessageBox.critical(
                                    dlg if dlg_alive() else win, __app_name__, tr("proton.error", err=err),
                                )

                        worker.finished_ok.connect(_st_ok)
                        worker.failed.connect(_st_err)
                        worker.start()

                    def _cancel_steam_proton() -> None:
                        if dlg_alive():
                            _set_proton_controls_enabled(True)
                            _start_config_loader()

                    win._confirm_close_steam_async(
                        _proceed_steam_proton,
                        on_cancelled=_cancel_steam_proton,
                        parent=dlg,
                        alive_fn=dlg_alive,
                    )

            proton_combo.activated.connect(_on_proton_combo_activated)
            if btn_previous_proton is not None:
                btn_previous_proton.clicked.connect(
                    lambda: _on_proton_combo_activated(proton_combo.currentIndex(), use_previous=True)
                )

        if btn_compat_migrate is not None and isinstance(obj, ShortcutEntry) and paths_snap is not None:
            paths_local = paths_snap
            appid = obj.appid_u32

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

                if _is_wine_game_running():
                    QMessageBox.warning(dlg, __app_name__, tr("cleanup.wine_running"))
                    return
                operation["migrating"] = True
                btn_compat_migrate.setEnabled(False)
                btn_close.setEnabled(False)
                win._begin_busy()

                class _CompatMigrateWorker(QThread):
                    finished_ok = Signal(str)
                    failed = Signal(str)

                    def run(self) -> None:
                        try:
                            if _is_wine_game_running():
                                raise RuntimeError(tr("cleanup.wine_running"))
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
                    operation["migrating"] = False
                    win._end_busy()
                    if dlg_alive():
                        btn_close.setEnabled(True)
                    win.status_label.setText(msg)
                    if dlg_alive():
                        _start_config_loader()
                        QMessageBox.information(dlg, __app_name__, msg)

                def _cm_err(err: str) -> None:
                    operation["migrating"] = False
                    win._end_busy()
                    if dlg_alive():
                        btn_close.setEnabled(True)
                    if dlg_alive():
                        _start_config_loader()
                    if not win._closing_pending:
                        QMessageBox.critical(dlg if dlg_alive() else win, __app_name__, err)

                cm_worker.finished_ok.connect(_cm_ok)
                cm_worker.failed.connect(_cm_err)
                cm_worker.start()

            btn_compat_migrate.clicked.connect(_on_compat_migrate_clicked)

        _start_config_loader()

    btn_close = QPushButton(tr("configure.btn_close"))
    btn_close.clicked.connect(dlg.accept)
    root_lay.addLayout(dialog_footer(btn_close))

    dlg.exec()
    dlg.deleteLater()
