"""Launch options presets dialog and library launch options editor."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..external_actions import load_external_launch_presets, save_external_launch_presets
from ..i18n import tr
from ..launch_presets import apply_presets
from ..launcher_scan import ExternalLauncherGame
from ..modes import feature_note, mode_of_kind
from ..optiscaler import inspect_optiscaler_state
from ..runtime_manager import (
    ensure_mangohud,
    get_system_package_install_cmd,
    install_system_package,
    official_mangohud_installed,
    system_mangohud_installed,
)
from ..standalone import StandaloneGame, update_standalone_launch_options
from ..steam_vdf import (
    ShortcutEntry,
    merge_winedlloverride,
    remove_winedlloverride,
    update_shortcut_launch_options,
)
from ..ui_cards import (
    _dialog_guard,
    _dialog_parent,
    _is_native_linux_game,
    _parse_launch_preset_state,
    _resolve_game_exe_path,
    note_label,
    section_card,
)
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit

if TYPE_CHECKING:
    from ..ui import MainWindow


def open_launch_presets_dialog(
    win: MainWindow,
    title: str,
    kind: str,
    initial_opts: str,
    initial_winedll: str,
    game_obj: object | None = None,
    is_native_linux: bool = False,
    parent: QWidget | None = None,
    mode: str | None = None,
) -> tuple[str, str] | None:
    """Open launch options presets dialog and return (launch_opts, winedll) or None.

    parent: the dialog that asked (e.g. Configure game), so the child stays above it under gamescope/Wayland.
    mode: Steam or Standalone (default: from kind). Options a mode does not use are shown greyed out
    with the reason (MangoHud in Steam mode: Steam has its own performance overlay).
    """
    mangohud_note = feature_note("mangohud", mode or mode_of_kind(kind))
    is_native_linux = bool(
        is_native_linux or (game_obj is not None and _is_native_linux_game(kind, game_obj))
    )
    opts_now = initial_opts or ""
    winedll_now = initial_winedll or ""
    st = _parse_launch_preset_state(opts_now)
    if kind == "standalone" and not winedll_now.strip():
        extracted_dll = str(st.get("winedll") or "").strip()
        if extracted_dll:
            winedll_now = extracted_dll

    dlg = QDialog(parent if parent is not None else _dialog_parent(win))
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setWindowTitle(tr("launch.title", name=title))
    dlg.resize(640, 640)
    dlg_lay = QVBoxLayout(dlg)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    body = QWidget()
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(0, 0, 4, 0)
    body_lay.setSpacing(12)
    current = {"lay": body_lay}  # the card the next options go into

    def _section(text: str) -> None:
        card, card_lay, _top = section_card(text)
        card_lay.setSpacing(4)
        body_lay.addWidget(card)
        current["lay"] = card_lay

    def _add(widget: QWidget) -> None:
        current["lay"].addWidget(widget)

    def _muted_hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("MutedText")
        lbl.setWordWrap(True)
        lbl.setContentsMargins(20, 0, 0, 4)
        return lbl

    chk_gm = QCheckBox(tr("launch.gamemode"))
    chk_gm.setChecked(bool(st["gamemode"]))
    chk_mh = QCheckBox(tr("launch.mangohud"))
    chk_mh.setChecked(bool(st["mangohud"]) and not mangohud_note)
    chk_mh.setEnabled(not mangohud_note)

    mh_level_row = QHBoxLayout()
    mh_level_row.setContentsMargins(20, 0, 0, 4)
    mh_level_lbl = QLabel(tr("launch.mangohud_level_label"))
    mh_level_combo = QComboBox()
    mh_level_combo.addItem(tr("launch.mangohud_level_default"), 0)
    mh_level_combo.addItem(tr("launch.mangohud_level_1"), 1)
    mh_level_combo.addItem(tr("launch.mangohud_level_2"), 2)
    mh_level_combo.addItem(tr("launch.mangohud_level_3"), 3)
    mh_level_combo.addItem(tr("launch.mangohud_level_4"), 4)
    mh_idx = mh_level_combo.findData(st["mh_level"])
    if mh_idx >= 0:
        mh_level_combo.setCurrentIndex(mh_idx)
    mh_level_combo.setEnabled(chk_mh.isChecked())
    mh_level_row.addWidget(mh_level_lbl)
    mh_level_row.addWidget(mh_level_combo, 1)

    mh_extra_row = QHBoxLayout()
    mh_extra_row.setContentsMargins(20, 0, 0, 4)
    mh_pos_lbl = QLabel(tr("launch.mangohud_position_label"))
    mh_pos_combo = QComboBox()
    mh_pos_combo.addItem(tr("launch.mangohud_pos_auto"), "")
    mh_pos_combo.addItem(tr("launch.mangohud_pos_top_center"), "top-center")
    mh_pos_combo.addItem(tr("launch.mangohud_pos_top_left"), "top-left")
    mh_pos_combo.addItem(tr("launch.mangohud_pos_top_right"), "top-right")
    mh_pos_combo.addItem(tr("launch.mangohud_pos_bottom_left"), "bottom-left")
    mh_pos_combo.addItem(tr("launch.mangohud_pos_bottom_right"), "bottom-right")
    pos_idx = mh_pos_combo.findData(st.get("mh_position", ""))
    if pos_idx >= 0:
        mh_pos_combo.setCurrentIndex(pos_idx)
    mh_pos_combo.setEnabled(chk_mh.isChecked())

    mh_fps_lbl = QLabel(tr("launch.mangohud_fps_limit_label"))
    mh_fps_combo = QComboBox()
    mh_fps_combo.addItem(tr("launch.mangohud_fps_unlimited"), 0)
    for fps_val in (30, 40, 60, 90, 120, 144):
        mh_fps_combo.addItem(tr("launch.mangohud_fps_value", fps=fps_val), fps_val)
    fps_idx = mh_fps_combo.findData(st.get("mh_fps_limit", 0))
    if fps_idx >= 0:
        mh_fps_combo.setCurrentIndex(fps_idx)
    mh_fps_combo.setEnabled(chk_mh.isChecked())

    mh_extra_row.addWidget(mh_pos_lbl)
    mh_extra_row.addWidget(mh_pos_combo, 1)
    mh_extra_row.addSpacing(8)
    mh_extra_row.addWidget(mh_fps_lbl)
    mh_extra_row.addWidget(mh_fps_combo)

    mh_opts_col = QVBoxLayout()
    mh_opts_col.setContentsMargins(0, 0, 0, 0)
    mh_opts_col.setSpacing(2)
    mh_opts_col.addLayout(mh_level_row)
    mh_opts_col.addLayout(mh_extra_row)

    gpu = win._get_gpu_profile()
    nvapi_active = bool(st["nvapi"])
    prime_active = bool(st["prime"])
    wayland_active = bool(st["wayland"])

    chk_nvapi = QCheckBox(tr("launch.nvidia_nvapi"))
    chk_nvapi.setChecked(nvapi_active)
    chk_prime = QCheckBox(tr("launch.prime_offload"))
    chk_prime.setChecked(prime_active)
    chk_wayland = QCheckBox(tr("launch.proton_wayland"))
    chk_wayland.setChecked(wayland_active)

    chk_debug = QCheckBox(tr("launch.debug_log"))
    chk_debug.setChecked(bool(st.get("debug_log")))

    chk_dlss = QCheckBox(tr("launch.dlss_upgrade"))
    chk_dlss.setChecked(bool(st["dlss_upgrade"]))

    chk_w3d = QCheckBox(tr("launch.wined3d"))
    chk_w3d.setChecked(bool(st["wined3d"]))
    chk_esync = QCheckBox(tr("launch.no_esync"))
    chk_esync.setChecked(bool(st["no_esync"]))
    chk_fsync = QCheckBox(tr("launch.no_fsync"))
    chk_fsync.setChecked(bool(st["no_fsync"]))
    chk_dxgi = QCheckBox(tr("launch.dxgi_preset"))
    chk_dxgi.setChecked(remove_winedlloverride(winedll_now, "dxgi") != winedll_now)

    winedll_lbl = QLabel(tr("launch.winedlloverrides_label"))
    winedll_edit = QLineEdit(winedll_now)

    wined3d_warn = note_label(tr("launch.wined3d_vulkan_warn"), "UnavailableBanner")
    wined3d_warn.setVisible(False)

    raw_edit = QLineEdit(opts_now)

    syncing = {"active": False}

    def _update_wined3d_warning() -> None:
        if is_native_linux or not chk_w3d.isChecked():
            wined3d_warn.setVisible(False)
            return
        has_vulkan_feat = chk_dlss.isChecked() or chk_nvapi.isChecked() or chk_dxgi.isChecked()
        wined3d_warn.setVisible(has_vulkan_feat)

    def _apply_optiscaler_dxgi_guard(opt_st: object | None) -> None:
        if not dlg_alive():
            return
        if opt_st is not None and getattr(opt_st, "proxy_dll", None) == "dxgi":
            chk_dxgi.setChecked(True)
            chk_dxgi.setEnabled(False)
            chk_dxgi.setToolTip(tr("launch.dxgi_optiscaler_locked"))
        else:
            chk_dxgi.setEnabled(True)
            chk_dxgi.setToolTip("")

    def _update_optiscaler_dxgi_guard() -> None:
        if is_native_linux or game_obj is None:
            return
        exe_p = _resolve_game_exe_path(kind, game_obj)

        class _OptiStateWorker(QThread):
            ready = Signal(object)

            def run(self) -> None:
                opt_st = (
                    inspect_optiscaler_state(exe_p)
                    if exe_p is not None and exe_p.parent.is_dir()
                    else None
                )
                self.ready.emit(opt_st)

        worker = _OptiStateWorker()
        win._track_worker(worker)
        worker.ready.connect(_apply_optiscaler_dxgi_guard)
        worker.start()

    def _sync_preview() -> None:
        mh_enabled = chk_mh.isChecked()
        mh_level_combo.setEnabled(mh_enabled)
        mh_pos_combo.setEnabled(mh_enabled)
        mh_fps_combo.setEnabled(mh_enabled)
        if syncing["active"]:
            return
        syncing["active"] = True
        try:
            dll_val = (
                winedll_edit.text().strip()
                if (kind in ("steam", "external") and not is_native_linux)
                else ""
            )
            updated = apply_presets(
                raw_edit.text(),
                {
                    "gamemode": chk_gm.isChecked(),
                    "mangohud": mh_enabled,
                    "wined3d": chk_w3d.isChecked(),
                    "no_esync": chk_esync.isChecked(),
                    "no_fsync": chk_fsync.isChecked(),
                    "dlss_upgrade": chk_dlss.isChecked(),
                    "nvapi": chk_nvapi.isChecked(),
                    "prime": chk_prime.isChecked(),
                    "wayland": chk_wayland.isChecked(),
                    "debug_log": chk_debug.isChecked(),
                    "mh_level": mh_level_combo.currentData(),
                    "mh_position": str(mh_pos_combo.currentData() or ""),
                    "mh_fps_limit": mh_fps_combo.currentData(),
                },
                winedlloverrides=dll_val,
                native_linux=is_native_linux,
            )
            raw_edit.setText(updated)
        except ValueError:
            pass
        finally:
            syncing["active"] = False
        _update_wined3d_warning()

    def _on_dxgi_toggled(checked: bool) -> None:
        if syncing["active"]:
            return
        cur_dll = winedll_edit.text()
        new_dll = (
            merge_winedlloverride(cur_dll, "dxgi", "n,b")
            if checked
            else remove_winedlloverride(cur_dll, "dxgi")
        )
        winedll_edit.setText(new_dll)

    def _on_winedll_text_changed(text: str) -> None:
        if not syncing["active"]:
            has_dxgi = remove_winedlloverride(text, "dxgi") != text
            if chk_dxgi.isChecked() != has_dxgi:
                syncing["active"] = True
                try:
                    chk_dxgi.setChecked(has_dxgi)
                finally:
                    syncing["active"] = False
        _sync_preview()

    def _on_raw_text_changed() -> None:
        _update_wined3d_warning()

    dlg_alive = _dialog_guard(dlg)

    def _add_tool_preset(
        chk: QCheckBox, hint_text: str, bin_name: str, extra_row: QLayout | None = None, blocked_note: str = "",
    ) -> None:
        chk.toggled.connect(_sync_preview)
        _add(chk)
        _add(_muted_hint(blocked_note or hint_text))
        if extra_row is not None:
            current["lay"].addLayout(extra_row)
        if blocked_note:
            return  # the mode does not use this tool: nothing to install
        official_mh = bin_name == "mangohud"
        available = (
            official_mangohud_installed() or system_mangohud_installed()
            if official_mh else shutil.which(bin_name) is not None
        )
        if not available:
            warn_row = QWidget()
            warn_lay = QHBoxLayout(warn_row)
            warn_lay.setContentsMargins(20, 0, 0, 4)
            warn_lay.setSpacing(8)
            warn_lbl = note_label(
                tr("launch.mangohud_official_missing") if official_mh else tr("launch.missing_bin_warn", bin=bin_name),
                "WarningText",
            )
            warn_lay.addWidget(warn_lbl, 1)

            install_btn = QPushButton(tr("launch.btn_install_bin", bin=bin_name))

            def _on_install_bin() -> None:
                if official_mh:
                    if QMessageBox.question(
                        dlg, __app_name__, tr("launch.mangohud_official_confirm")
                    ) == QMessageBox.StandardButton.Yes:
                        _start_install(_do_install_official_mangohud)
                    return
                cmd = get_system_package_install_cmd(bin_name)
                if not cmd:
                    QMessageBox.information(
                        dlg,
                        __app_name__,
                        tr("launch.install_bin_manual", cmd=f"<install {bin_name}>"),
                    )
                    return
                ans = QMessageBox.question(
                    dlg,
                    __app_name__,
                    tr("launch.install_bin_confirm", bin=bin_name, cmd="sudo " + " ".join(cmd)),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
                _start_install(lambda: install_system_package(bin_name))

            def _do_install_official_mangohud() -> tuple[bool, str]:
                try:
                    ensure_mangohud(force_update=True)
                except Exception as exc:
                    return False, str(exc)
                return True, ""

            def _start_install(job: Callable[[], tuple[bool, str]]) -> None:
                install_btn.setEnabled(False)
                install_btn.setText(tr("launch.installing_bin", bin=bin_name))
                win.status_label.setText(tr("launch.installing_bin", bin=bin_name))

                def _on_pkg_done(ok: bool, out: str) -> None:
                    win.status_label.setText(tr("status.ready"))
                    if dlg_alive():
                        install_btn.setEnabled(True)
                        install_btn.setText(tr("launch.btn_install_bin", bin=bin_name))
                    parent_win = dlg if dlg_alive() else win
                    if win._closing_pending:
                        return
                    if ok:
                        if dlg_alive():
                            warn_row.setVisible(False)
                        QMessageBox.information(
                            parent_win, __app_name__, tr("launch.install_bin_done", bin=bin_name)
                        )
                    else:
                        QMessageBox.warning(
                            parent_win, __app_name__, tr("launch.install_bin_error", bin=bin_name, err=out)
                        )

                def _on_pkg_err(err: str) -> None:
                    win.status_label.setText(tr("status.ready"))
                    if dlg_alive():
                        install_btn.setEnabled(True)
                        install_btn.setText(tr("launch.btn_install_bin", bin=bin_name))
                    if not win._closing_pending:
                        QMessageBox.warning(
                            dlg if dlg_alive() else win,
                            __app_name__,
                            tr("launch.install_bin_error", bin=bin_name, err=err),
                        )

                win._run_pkexec(
                    job,
                    on_result=_on_pkg_done,
                    on_error=_on_pkg_err,
                )

            install_btn.clicked.connect(_on_install_bin)
            warn_lay.addWidget(install_btn)
            _add(warn_row)

    _section(tr("launch.section_performance"))
    _add_tool_preset(chk_gm, tr("launch.gamemode_hint"), "gamemoderun")
    _add_tool_preset(chk_mh, tr("launch.mangohud_hint"), "mangohud", extra_row=mh_opts_col, blocked_note=mangohud_note)
    mh_level_combo.currentIndexChanged.connect(_sync_preview)
    mh_pos_combo.currentIndexChanged.connect(_sync_preview)
    mh_fps_combo.currentIndexChanged.connect(_sync_preview)

    gpu_rows = (
        ((not is_native_linux) and (gpu.has_nvidia or nvapi_active))
        or (gpu.is_hybrid or prime_active)
        or ((not is_native_linux) and (gpu.has_nvidia or bool(st["dlss_upgrade"])))
    )
    if gpu_rows:
        _section(tr("launch.section_gpu"))
    if (not is_native_linux) and (gpu.has_nvidia or nvapi_active):
        chk_nvapi.toggled.connect(_sync_preview)
        _add(chk_nvapi)
        _add(_muted_hint(tr("launch.nvidia_nvapi_hint")))
    else:
        chk_nvapi.setVisible(False)
        chk_nvapi.setEnabled(False)
    if gpu.is_hybrid or prime_active:
        if not gpu.has_nvidia:
            chk_prime.setText(tr("launch.prime_offload_mesa"))
        chk_prime.toggled.connect(_sync_preview)
        _add(chk_prime)
        prime_hint = tr("launch.prime_offload_hint") if gpu.has_nvidia else tr("launch.prime_offload_mesa_hint")
        _add(_muted_hint(prime_hint))
    if (not is_native_linux) and (gpu.has_nvidia or bool(st["dlss_upgrade"])):
        chk_dlss.toggled.connect(_sync_preview)
        _add(chk_dlss)
        _add(_muted_hint(tr("launch.dlss_hint")))
    else:
        chk_dlss.setVisible(False)
        chk_dlss.setEnabled(False)

    if not is_native_linux:
        _section(tr("launch.section_compat"))
    if (not is_native_linux) and (gpu.is_wayland or wayland_active):
        chk_wayland.toggled.connect(_sync_preview)
        _add(chk_wayland)
        _add(_muted_hint(tr("launch.proton_wayland_hint")))
    else:
        chk_wayland.setVisible(False)
        chk_wayland.setEnabled(False)

    if not is_native_linux:
        for chk, hint in (
            (chk_w3d, tr("launch.wined3d_hint")),
            (chk_esync, tr("launch.no_esync_hint")),
            (chk_fsync, tr("launch.no_fsync_hint")),
        ):
            chk.toggled.connect(_sync_preview)
            _add(chk)
            _add(_muted_hint(hint))
        chk_dxgi.toggled.connect(_on_dxgi_toggled)
        _add(chk_dxgi)
        _add(_muted_hint(tr("launch.dxgi_hint")))
        _add(wined3d_warn)
    else:
        for chk in (chk_w3d, chk_esync, chk_fsync, chk_dxgi):
            chk.setVisible(False)
            chk.setEnabled(False)

    # Advanced: the debug log and the raw DLL overrides (Wine games only).
    if not is_native_linux:
        _section(tr("launch.section_advanced"))
        chk_debug.toggled.connect(_sync_preview)
        _add(chk_debug)
        _add(_muted_hint(tr("launch.debug_log_hint")))
        _add(winedll_lbl)
        _add(winedll_edit)
        _add(_muted_hint(tr("launch.dll_hint")))
        if kind == "standalone":
            _add(note_label(tr("launch.winedll_standalone_note")))
    else:
        chk_debug.setVisible(False)
        chk_debug.setEnabled(False)
        winedll_lbl.setVisible(False)
        winedll_edit.setVisible(False)
        winedll_edit.setEnabled(False)
    body_lay.addStretch(1)

    winedll_edit.textChanged.connect(_on_winedll_text_changed)
    raw_edit.textChanged.connect(_on_raw_text_changed)
    _update_optiscaler_dxgi_guard()
    _update_wined3d_warning()
    if mangohud_note and st["mangohud"]:
        _sync_preview()  # drop MangoHud from options a Steam-mode game keeps from before

    scroll.setWidget(body)
    dlg_lay.addWidget(scroll, 1)

    # The result, always in sight: the line the game starts with (editable by hand).
    result_card, result_lay, _top = section_card(tr("launch.raw_label"))
    result_lay.setSpacing(4)
    result_lay.addWidget(raw_edit)
    result_lay.addWidget(note_label(tr("launch.raw_hint")))
    if kind == "external":
        result_lay.addWidget(note_label(tr("launch.external_nondestructive_note")))
    dlg_lay.addWidget(result_card)

    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    dlg_lay.addWidget(btns)

    try:
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        new_opts_out = raw_edit.text().strip()
        new_dll_out = winedll_edit.text().strip()
        win._save_last_launch_prefs(new_opts_out, new_dll_out)
        return new_opts_out, new_dll_out
    finally:
        dlg.deleteLater()


def edit_launch_options_presets(
    win: MainWindow,
    sel: tuple[str, object] | None = None,
    parent: QWidget | None = None,
    on_saved: Callable[[str], None] | None = None,
) -> None:
    """Open launch options presets for a game (default: the selected library game) and save.

    sel/parent come from the Configure dialog so the right game is edited and the child opens above it;
    on_saved(new_opts) runs once the new options are stored (or queued).
    """
    if sel is None:
        sel = win._selected_library_item()

    def box_parent() -> QWidget:
        # The Configure dialog may be closed by the time an async save reports back.
        if parent is not None and shiboken6.isValid(parent):
            return parent
        return _dialog_parent(win)

    if not sel:
        QMessageBox.information(box_parent(), __app_name__, tr("launch.no_game"))
        return
    kind, obj = sel
    if kind not in ("steam", "standalone", "external"):
        return
    if isinstance(obj, ShortcutEntry):
        if not win.paths:
            QMessageBox.warning(box_parent(), __app_name__, tr("add.steam_missing"))
            return
        title_name = obj.app_name
        opts_now = obj.launch_options or ""
        winedll_now = str(_parse_launch_preset_state(opts_now)["winedll"])
    elif isinstance(obj, StandaloneGame):
        title_name = obj.name
        opts_now = obj.launch_options or ""
        winedll_now = obj.winedlloverrides or ""
    elif isinstance(obj, ExternalLauncherGame):
        title_name = obj.name
        opts_now = load_external_launch_presets(obj)
        winedll_now = str(_parse_launch_preset_state(opts_now)["winedll"])
    else:
        return

    res = win._open_launch_presets_dialog(
        title=title_name,
        kind=kind,
        initial_opts=opts_now,
        initial_winedll=winedll_now,
        game_obj=obj,
        is_native_linux=_is_native_linux_game(kind, obj),
        parent=parent,
    )
    if res is None:
        return
    new_opts, new_dll = res

    if isinstance(obj, ExternalLauncherGame):
        try:
            save_external_launch_presets(obj, new_opts)
        except Exception as exc:
            QMessageBox.critical(box_parent(), __app_name__, str(exc))
            return
        msg = tr("launch.saved", name=title_name)
        win.status_label.setText(msg)
        if on_saved is not None:
            on_saved(new_opts)
        if not win._closing_pending:
            QMessageBox.information(box_parent(), __app_name__, msg)
        return

    if isinstance(obj, StandaloneGame):
        try:
            updated = update_standalone_launch_options(
                obj.game_dir,
                winedlloverrides=new_dll,
                launch_options=new_opts,
            )
            obj.launch_options = updated.launch_options
            obj.winedlloverrides = updated.winedlloverrides
        except Exception as exc:
            QMessageBox.critical(box_parent(), __app_name__, str(exc))
            return
        win._refresh_shortcuts()
        msg = tr("launch.saved", name=title_name)
        win.status_label.setText(msg)
        if on_saved is not None:
            on_saved(new_opts)
        if not win._closing_pending:
            QMessageBox.information(box_parent(), __app_name__, msg)
        return

    if not win.paths:
        return
    paths = win.paths
    entry = obj
    managed_game = win._managed_library_game(entry)

    def _sync_managed_launch_opts() -> StandaloneGame | None:
        if managed_game is None:
            return None
        return update_standalone_launch_options(
            managed_game.game_dir,
            winedlloverrides=new_dll,
            launch_options=new_opts,
        )

    def _apply_managed_result(result: object) -> None:
        if managed_game is not None and isinstance(result, StandaloneGame):
            managed_game.launch_options = result.launch_options
            managed_game.winedlloverrides = result.winedlloverrides

    if win.should_queue_vdf_edits():
        try:
            _apply_managed_result(_sync_managed_launch_opts())
        except Exception as exc:
            QMessageBox.critical(box_parent(), __app_name__, str(exc))
            return
        win.enqueue_vdf_edit(
            QueuedVdfEdit(
                edit_id=f"set_launch_options:{entry.appid_u32}",
                op="set_launch_options",
                appid_u32=entry.appid_u32,
                label=title_name,
                payload={"launch_options": new_opts},
            )
        )
        win._refresh_shortcuts()
        msg = tr("vdf_queue.enqueued", name=title_name)
        win.status_label.setText(msg)
        if on_saved is not None:
            on_saved(new_opts)
        return

    def _proceed_update_steam_opts() -> None:
        if win._closing_pending:
            return
        win._begin_busy()

        def _do_update() -> StandaloneGame | None:
            if not update_shortcut_launch_options(paths, entry.appid_u32, new_opts):
                raise RuntimeError(tr("add.steam_missing"))
            try:
                return _sync_managed_launch_opts()
            except Exception:
                update_shortcut_launch_options(paths, entry.appid_u32, opts_now)
                raise

        worker = SteamOpWorker(_do_update, need_steam_closed=True)
        win._track_worker(worker)
        worker.status.connect(win.status_label.setText)

        def _ok(result: object) -> None:
            win._end_busy()
            _apply_managed_result(result)
            win._refresh_shortcuts()
            msg = tr("launch.saved", name=title_name)
            win.status_label.setText(msg)
            if on_saved is not None:
                on_saved(new_opts)
            if not win._closing_pending:
                QMessageBox.information(box_parent(), __app_name__, msg)

        def _err(err: str) -> None:
            win._end_busy()
            if not win._closing_pending:
                QMessageBox.critical(box_parent(), __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    win._confirm_close_steam_async(
        _proceed_update_steam_opts,
        parent=box_parent(),
        alive_fn=lambda: bool(win and not win._closing_pending),
    )
