"""Prefix tool dialogs: Run EXE in Prefix, Winetricks, and OptiScaler."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..hardware import hardware
from ..i18n import tr
from ..launch_presets import apply_presets, parse_presets, recommended_upscaler
from ..optiscaler import (
    OPTISCALER_BACKENDS,
    OPTISCALER_FG_MODES,
    OPTISCALER_PROXY_DLLS,
    OPTISCALER_SPOOF_MODES,
    OptiScalerConflictError,
    OptiScalerState,
    check_optiscaler_compatibility,
    inspect_optiscaler_state,
    install_optiscaler_for_game,
    optiscaler_executable,
    recommend_proxy,
    uninstall_optiscaler_for_game,
)
from ..runtime_manager import (
    WINDOWS_VERSIONS,
    WINE_TOOLS,
    WINETRICKS_PRESETS,
    find_system_umu_run,
    find_winetricks,
    get_system_package_install_cmd,
    get_umu_run_path,
    install_system_package,
    resolve_game_prefix,
)
from ..standalone import StandaloneGame, list_standalone_games, update_standalone_launch_options
from ..steam_vdf import (
    ShortcutEntry,
    merge_winedlloverride,
    remove_winedlloverride,
    update_shortcut_launch_options,
)
from ..ui_cards import (
    ShownWhenEnabledButton,
    _BusyGuardedDialog,
    _dialog_guard,
    _dialog_parent,
    _format_worker_error,
    _is_native_linux_game,
    _parse_launch_preset_state,
    _pick_file_async,
    _resolve_game_exe_path,
    dialog_footer,
    note_label,
    section_card,
)
from ..ui_workers import RunExeInPrefixWorker, SteamOpWorker, WinetricksWorker
from ..vdf_queue import QueuedVdfEdit

if TYPE_CHECKING:
    from ..ui import MainWindow


def run_exe_in_prefix_dialog(
    win: MainWindow, kind: str | None = None, obj: object | None = None
) -> None:
    """Prompt for an .exe/.msi and run it inside the selected game's Wine prefix."""
    if kind is None or obj is None:
        sel = win._selected_library_item()
        if not sel:
            return
        kind, obj = sel
    if kind not in ("steam", "standalone") or _is_native_linux_game(kind, obj):
        return
    if kind == "steam" and not win.paths:
        return

    try:
        prefix_dir, proton_dir = resolve_game_prefix(kind, obj, win.paths)
    except Exception as exc:
        QMessageBox.critical(_dialog_parent(win), __app_name__, str(exc))
        return

    if win._warn_if_win32_prefix(prefix_dir):
        return

    start_dir = obj.game_dir if isinstance(obj, StandaloneGame) else Path.home()

    def _on_picked(exe_path_str: str) -> None:
        if not exe_path_str or win._closing_pending:
            return
        if (
            not get_umu_run_path().is_file()
            and find_system_umu_run() is None
            and (win.paths is not None or getattr(win, "_steam_root_found", False))
        ):
            ans = QMessageBox.question(
                _dialog_parent(win),
                __app_name__,
                tr("run_exe.confirm_umu_steamos"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        exe_path = Path(exe_path_str)
        worker = RunExeInPrefixWorker(prefix_dir, exe_path, proton_dir=proton_dir)
        win._track_worker(worker)
        worker.status.connect(win.status_label.setText)

        def _ok() -> None:
            msg = tr("run_exe.done", exe=exe_path.name)
            win.status_label.setText(msg)
            if not win._closing_pending:
                QMessageBox.information(_dialog_parent(win), __app_name__, msg)

        def _err(err: str) -> None:
            if not win._closing_pending:
                QMessageBox.critical(_dialog_parent(win), __app_name__, tr("run_exe.error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    _pick_file_async(
        _dialog_parent(win),
        tr("run_exe.pick_title"),
        start_dir,
        tr("run_exe.filter"),
        _on_picked,
    )


def open_winetricks_dialog(
    win: MainWindow, kind: str | None = None, obj: object | None = None, preset_verbs: list[str] | None = None
) -> None:
    """Open Winetricks verbs/presets installer dialog for the selected game's prefix."""
    if kind is None or obj is None:
        sel = win._selected_library_item()
        if not sel:
            return
        kind, obj = sel
    if kind == "standalone" and bool(getattr(obj, "native_linux", False)):
        return
    if kind == "steam" and not win.paths:
        return

    try:
        prefix_dir, proton_dir = resolve_game_prefix(kind, obj, win.paths)
    except Exception as exc:
        QMessageBox.critical(_dialog_parent(win), __app_name__, str(exc))
        return

    title_name = (
        obj.app_name
        if isinstance(obj, ShortcutEntry)
        else str(getattr(obj, "name", "") or "")
    )
    active_worker: dict[str, WinetricksWorker | None] = {"w": None}
    dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: active_worker["w"] is not None)
    dlg.setWindowTitle(tr("winetricks.title", name=title_name))
    dlg.resize(620, 640)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)
    lay.addWidget(note_label(tr("winetricks.prefix_label", path=str(prefix_dir))))

    dlg_alive = _dialog_guard(dlg)

    warn_row = QWidget()
    warn_lay = QHBoxLayout(warn_row)
    warn_lay.setContentsMargins(0, 0, 0, 0)
    warn_lay.setSpacing(8)
    warn_lay.addWidget(note_label(tr("winetricks.missing_warn"), "WarningText"), 1)
    btn_install_wt = QPushButton(tr("launch.btn_install_bin", bin="winetricks"))
    warn_lay.addWidget(btn_install_wt)
    has_winetricks = find_winetricks() is not None
    warn_row.setVisible(not has_winetricks)
    lay.addWidget(warn_row)

    # 1. Wine's own tools: open at once, nothing to install.
    tools_card, tools_lay, _top = section_card(tr("winetricks.tools_label"), tr("winetricks.tools_hint"))
    tools_row = QHBoxLayout()
    tool_labels = {
        "winecfg": tr("winetricks.tool_winecfg"),
        "regedit": tr("winetricks.tool_regedit"),
        "taskmgr": tr("winetricks.tool_taskmgr"),
    }
    tool_buttons: list[QPushButton] = []

    def _run_tool(tool: str) -> None:
        worker = RunExeInPrefixWorker(prefix_dir, Path(tool), proton_dir=proton_dir, allow_download=False)
        win._track_worker(worker)
        worker.status.connect(win.status_label.setText)

        def _tool_err(err: str) -> None:
            if not win._closing_pending:
                QMessageBox.warning(dlg if dlg_alive() else _dialog_parent(win), __app_name__, err)

        worker.failed.connect(_tool_err)
        worker.start()

    for tool in WINE_TOOLS:
        btn_tool = QPushButton(tool_labels[tool])
        btn_tool.setToolTip(tool)
        btn_tool.clicked.connect(lambda _=False, t=tool: _run_tool(t))
        tool_buttons.append(btn_tool)
        tools_row.addWidget(btn_tool)
    tools_row.addStretch()
    tools_lay.addLayout(tools_row)
    lay.addWidget(tools_card)

    # 2. Dependencies: pick, then install (the log shows up once it runs).
    deps_card, deps_lay, _top = section_card(tr("winetricks.presets_label"), tr("winetricks.presets_hint"))
    deps_form = QFormLayout()
    winver_combo = QComboBox()
    winver_combo.addItem(tr("winetricks.winver_keep"), "")
    for verb, label in WINDOWS_VERSIONS.items():
        winver_combo.addItem(label, verb)
    deps_form.addRow(tr("winetricks.winver_label"), winver_combo)
    deps_lay.addLayout(deps_form)
    preset_checks: dict[str, QCheckBox] = {}
    checks_grid = QGridLayout()
    checks_grid.setHorizontalSpacing(16)
    for i, (verb, desc) in enumerate(WINETRICKS_PRESETS.items()):
        chk = QCheckBox(desc)
        chk.setToolTip(verb)
        preset_checks[verb] = chk
        chk.setChecked(verb in (preset_verbs or ()))
        checks_grid.addWidget(chk, i // 2, i % 2)
    deps_lay.addLayout(checks_grid)
    custom_edit = QLineEdit()
    custom_edit.setPlaceholderText(tr("winetricks.custom_placeholder"))
    for verb in preset_verbs or ():  # from the recommendations: tick the known ones, list the rest
        if verb in WINETRICKS_PRESETS:
            continue
        custom_edit.setText((custom_edit.text() + " " + verb).strip())
    deps_form_extra = QFormLayout()
    deps_form_extra.addRow(tr("winetricks.custom_label"), custom_edit)
    deps_lay.addLayout(deps_form_extra)
    lay.addWidget(deps_card)

    log_edit = QPlainTextEdit()
    log_edit.setReadOnly(True)
    log_edit.setMaximumBlockCount(500)
    log_edit.setVisible(False)
    lay.addWidget(log_edit, 1)
    lay.addStretch()

    btn_install = QPushButton(tr("winetricks.btn_install"))
    btn_install.setObjectName("primaryBtn")
    btn_install.setEnabled(has_winetricks)
    btn_cancel = ShownWhenEnabledButton(tr("winetricks.btn_cancel"))
    btn_cancel.setEnabled(False)
    btn_close = QPushButton(tr("winetricks.btn_close"))
    btn_close.clicked.connect(dlg.accept)
    lay.addLayout(dialog_footer(btn_install, btn_close, left=(btn_cancel,)))

    def _on_install_wt_pkg() -> None:
        cmd = get_system_package_install_cmd("winetricks")
        if not cmd:
            QMessageBox.information(
                dlg,
                __app_name__,
                tr("launch.install_bin_manual", cmd="<install winetricks>"),
            )
            return
        ans = QMessageBox.question(
            dlg,
            __app_name__,
            tr("launch.install_bin_confirm", bin="winetricks", cmd="sudo " + " ".join(cmd)),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        btn_install_wt.setEnabled(False)
        btn_install_wt.setText(tr("launch.installing_bin", bin="winetricks"))

        def _pkg_ok(ok: bool, out: str) -> None:
            if dlg_alive():
                btn_install_wt.setEnabled(True)
                btn_install_wt.setText(tr("launch.btn_install_bin", bin="winetricks"))
            parent_win = dlg if dlg_alive() else win
            if win._closing_pending:
                return
            if ok:
                if dlg_alive():
                    warn_row.setVisible(False)
                    btn_install.setEnabled(True)
                QMessageBox.information(
                    parent_win, __app_name__, tr("launch.install_bin_done", bin="winetricks")
                )
            else:
                QMessageBox.warning(
                    parent_win,
                    __app_name__,
                    tr("launch.install_bin_error", bin="winetricks", err=out),
                )

        def _pkg_err(err: str) -> None:
            if dlg_alive():
                btn_install_wt.setEnabled(True)
                btn_install_wt.setText(tr("launch.btn_install_bin", bin="winetricks"))
            if not win._closing_pending:
                QMessageBox.warning(
                    dlg if dlg_alive() else win,
                    __app_name__,
                    tr("launch.install_bin_error", bin="winetricks", err=err),
                )

        win._run_pkexec(
            lambda: install_system_package("winetricks"),
            on_result=_pkg_ok,
            on_error=_pkg_err,
        )

    btn_install_wt.clicked.connect(_on_install_wt_pkg)

    def _set_running(running: bool) -> None:
        if not dlg_alive():
            return
        for chk in preset_checks.values():
            chk.setEnabled(not running)
        custom_edit.setEnabled(not running)
        winver_combo.setEnabled(not running)
        for btn_tool in tool_buttons:
            btn_tool.setEnabled(not running)
        btn_install.setEnabled((not running) and (find_winetricks() is not None))
        btn_cancel.setEnabled(running)
        btn_close.setEnabled(not running)

    def _on_run() -> None:
        verbs = [v for v, chk in preset_checks.items() if chk.isChecked()]
        winver = str(winver_combo.currentData() or "")
        if winver:
            verbs.insert(0, winver)
        raw_custom = custom_edit.text().replace(",", " ").strip()
        if raw_custom:
            for tok in raw_custom.split():
                if tok not in verbs:
                    verbs.append(tok)
        if not verbs:
            QMessageBox.information(dlg, __app_name__, tr("winetricks.no_verbs"))
            return

        log_edit.clear()
        log_edit.setVisible(True)
        log_edit.appendPlainText(tr("winetricks.running"))
        _set_running(True)

        worker = WinetricksWorker(
            prefix_dir,
            verbs,
            proton_dir=proton_dir,
            game_name=title_name,
            pre_snapshot_fn=win._make_pre_snapshot_fn(),
        )
        active_worker["w"] = worker
        win._track_worker(worker)

        def _on_log(line: str) -> None:
            if dlg_alive():
                log_edit.appendPlainText(line)

        def _on_ok() -> None:
            active_worker["w"] = None
            _set_running(False)
            if dlg_alive():
                log_edit.appendPlainText(tr("winetricks.done"))
                if not win._closing_pending:
                    QMessageBox.information(dlg, __app_name__, tr("winetricks.done"))

        def _on_err(err: str) -> None:
            active_worker["w"] = None
            _set_running(False)
            if dlg_alive():
                msg = tr("winetricks.error", err=err)
                log_edit.appendPlainText(msg)
                if not win._closing_pending:
                    QMessageBox.warning(dlg, __app_name__, msg)

        worker.log.connect(_on_log)
        worker.finished_ok.connect(_on_ok)
        worker.failed.connect(_on_err)
        worker.start()

    def _on_cancel() -> None:
        w = active_worker["w"]
        if w is not None and w.isRunning():
            btn_cancel.setEnabled(False)
            w.cancel()

    btn_install.clicked.connect(_on_run)
    btn_cancel.clicked.connect(_on_cancel)
    dlg.exec()
    dlg.deleteLater()


def open_optiscaler_dialog(
    win: MainWindow, kind: str | None = None, obj: object | None = None, *,
    on_saved: Callable[[str], None] | None = None,
) -> None:
    """Open OptiScaler install/configure/uninstall dialog for the selected game."""
    if kind is None or obj is None:
        sel = win._selected_library_item()
        if not sel:
            return
        kind, obj = sel
    if _is_native_linux_game(kind, obj):
        return
    if kind == "steam" and not win.paths:
        return

    managed = win._managed_library_game(obj) if isinstance(obj, ShortcutEntry) else None
    exe_path = managed.exe_path if managed is not None else _resolve_game_exe_path(kind, obj)
    current_steam_opts = obj.launch_options if isinstance(obj, ShortcutEntry) else ""
    if exe_path is None or not str(exe_path).strip() or not exe_path.parent.is_dir():
        QMessageBox.warning(
            win,
            __app_name__,
            tr("optiscaler.invalid_exe_dir", path=str(exe_path.parent) if exe_path else ""),
        )
        return

    title_name = (
        obj.app_name
        if isinstance(obj, ShortcutEntry)
        else str(getattr(obj, "name", "") or "")
    )
    operation = {"busy": False, "refresh_id": 0}
    dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: bool(operation["busy"]))
    dlg.setWindowTitle(tr("optiscaler.title", name=title_name))
    dlg.resize(560, 460)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)

    dlg_alive = _dialog_guard(dlg)

    # 1. Where it goes and whether it is there.
    state_card, state_lay, _top = section_card(tr("optiscaler.state_title"))
    lbl_state = QLabel()
    lbl_state.setWordWrap(True)
    state_lay.addWidget(lbl_state)
    state_lay.addWidget(
        note_label(tr("optiscaler.exe_dir_label", path=str(optiscaler_executable(exe_path).parent)))
    )
    lay.addWidget(state_card)

    # 2. How it runs (Automatic picks what fits this game and GPU).
    opts_card, opts_lay, _top = section_card(tr("optiscaler.options_title"), tr("optiscaler.options_hint"))
    form = QFormLayout()
    combo_backend = QComboBox()
    backend_labels = {
        "auto": tr("optiscaler.backend_auto"),
        "fsr3": tr("optiscaler.backend_fsr3"),
        "fsr22": tr("optiscaler.backend_fsr22"),
        "fsr21": tr("optiscaler.backend_fsr21"),
        "xess": tr("optiscaler.backend_xess"),
        "dlss": tr("optiscaler.backend_dlss"),
    }
    for b in OPTISCALER_BACKENDS:
        combo_backend.addItem(backend_labels.get(b, b), b)
    form.addRow(tr("optiscaler.backend_label"), combo_backend)

    combo_proxy = QComboBox()
    for dll in OPTISCALER_PROXY_DLLS:
        label = tr("optiscaler.proxy_dxgi") if dll == "dxgi" else f"{dll}.dll"
        combo_proxy.addItem(label, dll)
    form.addRow(tr("optiscaler.proxy_label"), combo_proxy)

    fg_combo = QComboBox()
    fg_labels = {
        "auto": tr("optiscaler.fg_auto"),
        "nofg": tr("optiscaler.fg_nofg"),
        "optifg": tr("optiscaler.fg_optifg"),
        "nukems": tr("optiscaler.fg_nukems"),
    }
    for fg in OPTISCALER_FG_MODES:
        fg_combo.addItem(fg_labels.get(fg, fg), fg)
    form.addRow(tr("optiscaler.fg_label"), fg_combo)

    spoof_combo = QComboBox()
    spoof_labels = {
        "auto": tr("optiscaler.spoof_auto"),
        "true": tr("optiscaler.spoof_true"),
        "false": tr("optiscaler.spoof_false"),
    }
    for sp in OPTISCALER_SPOOF_MODES:
        spoof_combo.addItem(spoof_labels.get(sp, sp), sp)
    form.addRow(tr("optiscaler.spoof_label"), spoof_combo)

    chk_overlay_menu = QCheckBox(tr("optiscaler.overlay_menu_label"))
    chk_overlay_menu.setChecked(True)
    form.addRow("", chk_overlay_menu)
    opts_lay.addLayout(form)
    lay.addWidget(opts_card)

    lbl_progress = note_label()
    lbl_progress.setVisible(False)
    lay.addWidget(lbl_progress)
    lay.addStretch(1)

    btn_install = QPushButton(tr("optiscaler.btn_install"))
    btn_install.setObjectName("primaryBtn")
    btn_remove = QPushButton(tr("optiscaler.btn_remove"))
    btn_remove.setObjectName("dangerBtn")
    btn_close = QPushButton(tr("optiscaler.btn_close"))
    btn_close.clicked.connect(dlg.accept)
    lay.addLayout(dialog_footer(btn_install, btn_close, left=(btn_remove,)))


    def _on_state_refreshed(req_id: int, st_now: object, recommended: str = "") -> None:
        if (
            not dlg_alive() or win._closing_pending or operation["busy"]
            or req_id != operation["refresh_id"]
        ):
            return
        if isinstance(st_now, OptiScalerState) and not st_now.complete:
            # A crash/kill left a half-done install: "Remove" undoes it and restores the originals.
            lbl_state.setText(tr("optiscaler.state_incomplete"))
            btn_remove.setEnabled(True)
        elif isinstance(st_now, OptiScalerState):
            lbl_state.setText(
                tr("optiscaler.state_needs_repair")
                if st_now.needs_repair
                else tr(
                    "optiscaler.state_installed",
                    proxy=f"{st_now.proxy_dll}.dll",
                    backend=st_now.backend,
                    version=st_now.version or "-",
                )
            )
            b_idx = combo_backend.findData(st_now.backend)
            if b_idx >= 0:
                combo_backend.setCurrentIndex(b_idx)
            p_idx = combo_proxy.findData(recommended if st_now.needs_repair else st_now.proxy_dll)
            if p_idx >= 0:
                combo_proxy.setCurrentIndex(p_idx)
            fg_raw = getattr(st_now, "fg_mode", "auto") or "auto"
            fg_norm = "auto" if fg_raw == "default" else fg_raw
            fg_idx = fg_combo.findData(fg_norm)
            if fg_idx >= 0:
                fg_combo.setCurrentIndex(fg_idx)
            sp_idx = spoof_combo.findData(getattr(st_now, "spoof_mode", "auto") or "auto")
            if sp_idx >= 0:
                spoof_combo.setCurrentIndex(sp_idx)
            chk_overlay_menu.setChecked(bool(getattr(st_now, "overlay_menu", True)))
            btn_remove.setEnabled(True)
        else:
            lbl_state.setText(tr("optiscaler.state_not_installed"))
            btn_remove.setEnabled(False)
            r_idx = combo_proxy.findData(recommended) if recommended else -1
            if r_idx >= 0:
                combo_proxy.setCurrentIndex(r_idx)
                combo_proxy.setToolTip(tr("optiscaler.proxy_recommended", proxy=f"{recommended}.dll"))
            # Not installed yet: start from the upscaler that suits the GPU this game runs on.
            game_gpu = hardware().game_gpu(bool(parse_presets(_current_opts()).get("prime")))
            b_idx = combo_backend.findData(recommended_upscaler(game_gpu))
            if b_idx >= 0 and combo_backend.currentData() == "auto":
                combo_backend.setCurrentIndex(b_idx)
                if game_gpu is not None:
                    combo_backend.setToolTip(tr("optiscaler.backend_for_gpu", name=game_gpu.name or game_gpu.vendor))

    def _refresh_state_ui() -> None:
        game_exe = exe_path
        assert game_exe is not None
        operation["refresh_id"] += 1
        req_id = operation["refresh_id"]

        class _OptiRefreshWorker(QThread):
            ready = Signal(int, object, str)

            def run(self) -> None:
                state = inspect_optiscaler_state(game_exe)
                proxy, _notes = recommend_proxy(game_exe, None, state)
                self.ready.emit(req_id, state, proxy)

        refresh_worker = _OptiRefreshWorker()
        win._track_worker(refresh_worker)
        refresh_worker.ready.connect(_on_state_refreshed)
        refresh_worker.start()

    def _set_busy(busy: bool, status_text: str = "") -> None:
        operation["busy"] = busy
        if not dlg_alive():
            return
        combo_backend.setEnabled(not busy)
        combo_proxy.setEnabled(not busy)
        fg_combo.setEnabled(not busy)
        spoof_combo.setEnabled(not busy)
        chk_overlay_menu.setEnabled(not busy)
        btn_install.setEnabled(not busy)
        btn_close.setEnabled(not busy)
        if busy:
            btn_remove.setEnabled(False)
            lbl_progress.setText(status_text)
            lbl_progress.setVisible(True)
        else:
            lbl_progress.setVisible(False)
            _refresh_state_ui()

    def _current_opts() -> str:
        if isinstance(obj, StandaloneGame):
            return str(obj.launch_options or "")
        if kind == "external":
            from ..external_actions import load_external_launch_presets
            from ..launcher_scan import ExternalLauncherGame

            return load_external_launch_presets(obj) if isinstance(obj, ExternalLauncherGame) else ""
        return current_steam_opts

    def _current_winedll() -> str:
        if kind == "standalone":
            return str(getattr(obj, "winedlloverrides", "") or "")
        if kind == "external":
            from ..external_actions import load_external_launch_presets
            from ..launcher_scan import ExternalLauncherGame

            if isinstance(obj, ExternalLauncherGame):
                return str(_parse_launch_preset_state(load_external_launch_presets(obj))["winedll"])
        return str(_parse_launch_preset_state(current_steam_opts)["winedll"])

    def _apply_winedll_to_opts(cur_opts: str, winedll: str, *, allow_dlss_upgrade: bool = False) -> str:
        extra: dict[str, Any] = {} if allow_dlss_upgrade else {"dlss_upgrade": False}
        return apply_presets(cur_opts, winedlloverrides=winedll, **extra)

    def _apply_winedll_update(
        new_winedll: str,
        done_msg: str,
        *,
        previous_proxy: str = "",
        new_proxy: str = "",
    ) -> None:
        nonlocal current_steam_opts
        parent_win = dlg if dlg_alive() else win
        if kind == "standalone" and isinstance(obj, StandaloneGame):
            cur_opts = str(obj.launch_options or "")
            try:
                updated = update_standalone_launch_options(
                    obj.game_dir,
                    winedlloverrides=new_winedll,
                    launch_options=cur_opts,
                )
            except Exception as exc:
                _set_busy(False)
                if not win._closing_pending:
                    QMessageBox.critical(parent_win, __app_name__, tr("optiscaler.error", err=str(exc)))
                return
            obj.winedlloverrides = updated.winedlloverrides
            if on_saved is not None:
                on_saved(updated.launch_options)
            win._refresh_shortcuts()
            _set_busy(False)
            win.status_label.setText(done_msg)
            if not win._closing_pending:
                QMessageBox.information(parent_win, __app_name__, done_msg)
            return

        if kind == "external":
            from ..external_actions import load_external_launch_presets, save_external_launch_presets
            from ..launcher_scan import ExternalLauncherGame

            if not isinstance(obj, ExternalLauncherGame):
                _set_busy(False)
                return
            cur_opts = load_external_launch_presets(obj)
            new_opts = _apply_winedll_to_opts(cur_opts, new_winedll, allow_dlss_upgrade=True)
            try:
                save_external_launch_presets(obj, new_opts)
            except Exception as exc:
                _set_busy(False)
                if not win._closing_pending:
                    QMessageBox.critical(parent_win, __app_name__, tr("optiscaler.error", err=str(exc)))
                return
            if on_saved is not None:
                on_saved(new_opts)
            _set_busy(False)
            win.status_label.setText(done_msg)
            if not win._closing_pending:
                QMessageBox.information(parent_win, __app_name__, done_msg)
            return

        if not isinstance(obj, ShortcutEntry) or win.paths is None:
            _set_busy(False)
            return
        paths = win.paths
        entry = obj

        new_opts = _apply_winedll_to_opts(current_steam_opts, new_winedll, allow_dlss_upgrade=True)

        def _queue_steam_winedll() -> None:
            nonlocal current_steam_opts
            win.enqueue_vdf_edit(
                QueuedVdfEdit(
                    f"set_launch_options:{entry.appid_u32}", "set_launch_options",
                    entry.appid_u32, entry.app_name, {"launch_options": new_opts},
                ),
                trigger_watch=win.should_queue_vdf_edits(),
            )
            current_steam_opts = new_opts
            if on_saved is not None:
                on_saved(new_opts)
            _set_busy(False)
            if not win._closing_pending:
                win.status_label.setText(tr("vdf_queue.enqueued", name=entry.app_name))

        if managed is not None:
            try:
                current = next((g for g in list_standalone_games() if g.game_dir == managed.game_dir), managed)
                dll = current.winedlloverrides
                if previous_proxy and previous_proxy != new_proxy:
                    dll = remove_winedlloverride(dll, previous_proxy)
                if new_proxy:
                    dll = merge_winedlloverride(dll, new_proxy, "n,b")
                update_standalone_launch_options(
                    current.game_dir, winedlloverrides=dll, launch_options=current.launch_options,
                )
            except Exception as exc:
                _set_busy(False)
                if not win._closing_pending:
                    QMessageBox.critical(parent_win, __app_name__, tr("optiscaler.error", err=str(exc)))
                return

        def _proceed_steam_winedll() -> None:
            if win._closing_pending:
                _queue_steam_winedll()
                return

            def _write() -> None:
                if not update_shortcut_launch_options(paths, entry.appid_u32, new_opts):
                    raise RuntimeError(tr("add.steam_missing"))

            win._begin_busy()
            steam_worker = SteamOpWorker(_write, need_steam_closed=True)
            win._track_worker(steam_worker)
            steam_worker.status.connect(win.status_label.setText)

            def _steam_ok(_res: object) -> None:
                nonlocal current_steam_opts
                win._end_busy()
                win._refresh_shortcuts()
                current_steam_opts = new_opts
                if on_saved is not None:
                    on_saved(new_opts)
                _set_busy(False)
                win.status_label.setText(done_msg)
                if not win._closing_pending:
                    QMessageBox.information(dlg if dlg_alive() else win, __app_name__, done_msg)

            def _steam_err(err: str) -> None:
                win._end_busy()
                _queue_steam_winedll()
                if not win._closing_pending:
                    QMessageBox.critical(dlg if dlg_alive() else win, __app_name__, tr("optiscaler.error", err=err))

            steam_worker.finished_ok.connect(_steam_ok)
            steam_worker.failed.connect(_steam_err)
            steam_worker.start()

        if win.should_queue_vdf_edits():
            _queue_steam_winedll()
        else:
            win._confirm_close_steam_async(
                _proceed_steam_winedll, on_cancelled=_queue_steam_winedll, parent=parent_win, alive_fn=dlg_alive,
            )

    class _OptiScalerOpWorker(QThread):
        status = Signal(str)
        finished_ok = Signal(object)
        conflict = Signal(str)
        failed = Signal(str)

        def __init__(self, fn: Callable[[Callable[[str, int | None], None]], object]) -> None:
            super().__init__()
            self._fn = fn

        def run(self) -> None:
            def _emit_status(msg: str, _pct: int | None = None) -> None:
                self.status.emit(msg)

            try:
                res = self._fn(_emit_status)
                self.finished_ok.emit(res)
            except OptiScalerConflictError as exc:
                self.conflict.emit(str(exc.existing_path))
            except Exception as exc:
                self.failed.emit(_format_worker_error(exc))

    def _snapshot_optiscaler_prefix(reason: str, auto_snap_enabled: bool) -> None:
        try:
            prefix_dir, _ = resolve_game_prefix(kind, obj, win.paths)
        except Exception:
            return
        win._auto_snapshot_if_enabled(
            prefix_dir,
            title_name,
            reason,
            "optiscaler",
            enabled=auto_snap_enabled,
        )

    def _start_install(overwrite_existing: bool) -> None:
        if operation["busy"]:
            return
        if not overwrite_existing:
            game_dir = managed.game_dir if managed is not None else getattr(obj, "game_dir", None)
            compat = check_optiscaler_compatibility(exe_path, game_dir if isinstance(game_dir, Path) else None)
            if not compat.compatible and compat.tier in ("no_upscaler", "unsupported_api"):
                answer = QMessageBox.question(
                    dlg,
                    __app_name__,
                    tr("optiscaler.confirm_no_upscaler")
                    if compat.tier == "no_upscaler"
                    else tr("optiscaler.confirm_unsupported_api"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        proxy_dll = str(combo_proxy.currentData() or "dxgi")
        backend = str(combo_backend.currentData() or "auto")
        fg_mode = str(fg_combo.currentData() or "auto")
        spoof_mode = str(spoof_combo.currentData() or "auto")
        overlay_menu = chk_overlay_menu.isChecked()
        auto_snap_enabled = win._is_auto_snapshot_enabled()
        _set_busy(True, tr("optiscaler.installing"))
        win.status_label.setText(tr("optiscaler.installing"))

        def _do_install(cb: Callable[[str, int | None], None]) -> object:
            prev_state = inspect_optiscaler_state(exe_path)
            _snapshot_optiscaler_prefix("optiscaler-install", auto_snap_enabled)
            try:
                pfx_dir, _ = resolve_game_prefix(kind, obj, win.paths)
            except Exception:
                pfx_dir = None
            state = install_optiscaler_for_game(
                exe_path,
                proxy_dll=proxy_dll,
                backend=backend,
                fg_mode=fg_mode,
                spoof_mode=spoof_mode,
                overlay_menu=overlay_menu,
                overwrite_existing=overwrite_existing,
                progress_cb=cb,
                prefix_dir=pfx_dir,
                game_name=title_name,
                auto_snapshot=auto_snap_enabled,
            )
            return state, prev_state

        worker = _OptiScalerOpWorker(_do_install)
        win._track_worker(worker)

        def _on_status(msg: str) -> None:
            win.status_label.setText(msg)
            if dlg_alive():
                lbl_progress.setText(msg)

        def _on_conflict(existing_path: str) -> None:
            win.status_label.setText(tr("status.ready"))
            _set_busy(False)
            if win._closing_pending:
                return
            ans = QMessageBox.question(
                dlg if dlg_alive() else win,
                __app_name__,
                tr("optiscaler.conflict_confirm", path=existing_path),
            )
            if ans == QMessageBox.StandardButton.Yes and dlg_alive():
                _start_install(True)

        def _on_ok(res: object) -> None:
            win.status_label.setText(tr("status.ready"))
            if not isinstance(res, tuple) or len(res) != 2:
                _set_busy(False)
                return
            state, prev_state = res
            if not isinstance(state, OptiScalerState):
                _set_busy(False)
                return
            cur_winedll = _current_winedll()
            if (
                isinstance(prev_state, OptiScalerState)
                and prev_state.proxy_dll
                and prev_state.proxy_dll != state.proxy_dll
            ):
                cur_winedll = remove_winedlloverride(cur_winedll, prev_state.proxy_dll)
            new_winedll = merge_winedlloverride(cur_winedll, state.proxy_dll, "n,b")
            done_msg = tr(
                "optiscaler.install_done",
                proxy=f"{state.proxy_dll}.dll",
                backend=state.backend,
                version=state.version or "-",
            )
            previous = prev_state.proxy_dll if isinstance(prev_state, OptiScalerState) else ""
            _apply_winedll_update(new_winedll, done_msg, previous_proxy=previous, new_proxy=state.proxy_dll)

        def _on_err(err: str) -> None:
            win.status_label.setText(tr("status.ready"))
            _set_busy(False)
            if not win._closing_pending:
                QMessageBox.critical(
                    dlg if dlg_alive() else win,
                    __app_name__,
                    tr("optiscaler.error", err=err),
                )

        worker.status.connect(_on_status)
        worker.conflict.connect(_on_conflict)
        worker.finished_ok.connect(_on_ok)
        worker.failed.connect(_on_err)
        worker.start()

    def _on_remove() -> None:
        if operation["busy"]:
            return
        auto_snap_enabled = win._is_auto_snapshot_enabled()
        _set_busy(True, tr("optiscaler.removing"))
        win.status_label.setText(tr("optiscaler.removing"))

        def _do_remove(_cb: Callable[[str, int | None], None]) -> object:
            _snapshot_optiscaler_prefix("optiscaler-remove", auto_snap_enabled)
            return uninstall_optiscaler_for_game(exe_path)

        worker = _OptiScalerOpWorker(_do_remove)
        win._track_worker(worker)

        def _on_ok(res: object) -> None:
            win.status_label.setText(tr("status.ready"))
            cur_winedll = _current_winedll()
            state = getattr(res, "state", None)
            new_winedll = (
                remove_winedlloverride(cur_winedll, state.proxy_dll)
                if state is not None and state.proxy_dll
                else cur_winedll
            )
            _apply_winedll_update(
                new_winedll, tr("optiscaler.remove_done"), previous_proxy=state.proxy_dll if state is not None else "",
            )

        def _on_err(err: str) -> None:
            win.status_label.setText(tr("status.ready"))
            _set_busy(False)
            if not win._closing_pending:
                QMessageBox.critical(
                    dlg if dlg_alive() else win,
                    __app_name__,
                    tr("optiscaler.error", err=err),
                )

        worker.finished_ok.connect(_on_ok)
        worker.failed.connect(_on_err)
        worker.start()

    btn_install.clicked.connect(lambda: _start_install(False))
    btn_remove.clicked.connect(_on_remove)
    _refresh_state_ui()
    dlg.exec()
    dlg.deleteLater()
