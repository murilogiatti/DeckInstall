"""Launch options presets dialog and library launch options editor."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..external_actions import load_external_launch_presets, save_external_launch_presets
from ..hardware import Gpu, hardware
from ..i18n import tr
from ..launch_presets import (
    FRAME_RATES,
    HIDE,
    OFF,
    OK,
    TOGGLES,
    apply_presets,
    availability,
    parse_presets,
    proton_kind,
)
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
from ..standalone import StandaloneGame, merge_dll_overrides, update_standalone_launch_options
from ..steam_vdf import (
    ShortcutEntry,
    get_shortcut_proton,
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


def screen_refresh_hz(widget: QWidget | None) -> int:
    """The refresh rate of the screen the window is on (120.21 -> 120), 0 when unknown."""
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    rate = screen.refreshRate() if screen is not None else 0.0
    return int(round(rate)) if rate >= 20 else 0


def half_rate(hz: int) -> int:
    """A cap that divides the screen's rate evenly, about half of it (90 -> 45, 60 -> 30, 165 -> 55)."""
    for divisor in (2, 3, 4):
        if hz % divisor == 0 and hz // divisor >= 30:
            return hz // divisor
    return max(30, hz // 2)


def game_proton(win: MainWindow, kind: str, obj: object | None) -> str:
    """The Proton kind a game runs on ("umu", "ge", "cachyos", "valve" or "" when unknown)."""
    if isinstance(obj, StandaloneGame):
        return proton_kind(obj.proton_name or "UMU-Proton")  # default: the newest UMU-Proton
    if isinstance(obj, ExternalLauncherGame):
        return proton_kind(obj.runtime)
    if isinstance(obj, ShortcutEntry) and win.paths is not None:
        try:
            tool = get_shortcut_proton(win.paths.config_vdf, obj.appid_u32)
        except Exception:  # an unreadable config.vdf only means "unknown"
            tool = ""
        if isinstance(tool, str) and tool:
            return proton_kind(tool)
        managed = win._managed_library_game(obj)
        return proton_kind(managed.proton_name) if isinstance(managed, StandaloneGame) else ""
    return "umu" if kind == "standalone" else ""


def _gpu_label(gpu: Gpu) -> str:
    """'NVIDIA GeForce RTX 3050 Laptop GPU (dedicated)' for the dialog."""
    kind = tr("hw.integrated") if gpu.integrated else tr("hw.dedicated")
    return f"{gpu.name or gpu.vendor.upper()} ({kind})"


@dataclass
class LaunchOptions:
    widget: QWidget
    raw: QLineEdit
    dll: QLineEdit
    set_options: Callable[[str, str], None]
    set_proton: Callable[[str], None]

    def values(self) -> tuple[str, str]:
        return self.raw.text().strip(), self.dll.text().strip()


def build_launch_options(
    win: MainWindow,
    dlg: QDialog,
    kind: str,
    initial_opts: str,
    initial_winedll: str,
    game_obj: object | None = None,
    is_native_linux: bool = False,
    mode: str | None = None,
    proton: str = "",
    *,
    proton_control: QWidget | None = None,
    extra_options: QWidget | None = None,
    more: Callable[[QWidget, QVBoxLayout, LaunchOptions], None] | None = None,
) -> LaunchOptions:
    """Shared launch controls for a new or installed game."""
    mangohud_note = feature_note("mangohud", mode or mode_of_kind(kind))
    is_native_linux = bool(
        is_native_linux or (game_obj is not None and _is_native_linux_game(kind, game_obj))
    )
    opts_now = initial_opts or ""
    winedll_now = initial_winedll or ""
    # Steam and external games keep WINEDLLOVERRIDES in the launch line; Standalone in its own field,
    # so a token found in a Standalone line moves to the field (the launcher would merge it anyway).
    dll_in_line = kind in ("steam", "external") and not is_native_linux
    dll_arg = None if dll_in_line else ""
    st = _parse_launch_preset_state(opts_now)
    extracted_dll = str(st.get("winedll") or "").strip()
    if not dll_in_line and extracted_dll:
        winedll_now = merge_dll_overrides(winedll_now, extracted_dll) if winedll_now.strip() else extracted_dll
    if not dll_in_line:
        try:
            opts_now = apply_presets(opts_now, winedlloverrides="", native_linux=is_native_linux)
        except ValueError:
            pass
    if mangohud_note and st["mangohud"]:  # a Steam-mode game keeps MangoHud from an older version
        try:
            opts_now = apply_presets(opts_now, mangohud=False, winedlloverrides=dll_arg, native_linux=is_native_linux)
        except ValueError:
            pass

    pane = QWidget(dlg)
    layout = QVBoxLayout(pane)
    layout.setContentsMargins(0, 0, 0, 0)
    quick = QFormLayout()
    if proton_control is not None:
        quick.addRow(tr("configure.lbl_current_proton"), proton_control)
    layout.addLayout(quick)
    more_button = QToolButton(pane)
    more_button.setText(tr("launch.more_options"))
    more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    more_button.setArrowType(Qt.ArrowType.RightArrow)
    more_button.setCheckable(True)
    layout.addWidget(more_button)
    details = QWidget(pane)
    details_layout = QVBoxLayout(details)
    details_layout.setContentsMargins(0, 0, 0, 0)
    details.hide()
    layout.addWidget(details)
    current: dict[str, QVBoxLayout] = {}

    section_cards: list[tuple[QWidget, QVBoxLayout]] = []

    def _section(text: str) -> None:
        card, card_lay, _top = section_card(text)
        card_lay.setSpacing(4)
        details_layout.addWidget(card)
        current["lay"] = card_lay
        section_cards.append((card, card_lay))

    def _add(widget: QWidget) -> None:
        current["lay"].addWidget(widget)

    def _muted_hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("MutedText")
        lbl.setWordWrap(True)
        lbl.setContentsMargins(20, 0, 0, 4)
        return lbl

    chk_gm = QCheckBox(tr("opt.gamemode"))
    chk_gm.setChecked(bool(st["gamemode"]))
    chk_mh = QCheckBox(tr("opt.mangohud"))
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

    mh_extra_row.addWidget(mh_pos_lbl)
    mh_extra_row.addWidget(mh_pos_combo, 1)

    mh_opts_col = QVBoxLayout()
    mh_opts_col.setContentsMargins(0, 0, 0, 0)
    mh_opts_col.setSpacing(2)
    mh_opts_col.addLayout(mh_level_row)
    mh_opts_col.addLayout(mh_extra_row)

    profile = hardware()
    prime_active = bool(st["prime"])

    chk_prime = QCheckBox()  # no box of its own: the GPU picker above drives it
    chk_prime.setChecked(prime_active)

    chk_debug = QCheckBox(tr("opt.debug_log"))
    chk_debug.setChecked(bool(st.get("debug_log")))

    chk_w3d = QCheckBox(tr("opt.wined3d"))
    chk_w3d.setChecked(bool(st["wined3d"]))
    chk_fsync = QCheckBox(tr("opt.no_fsync"))
    chk_fsync.setChecked(bool(st["no_fsync"]))
    chk_dxgi = QCheckBox(tr("opt.dxgi"))
    chk_dxgi.setChecked(remove_winedlloverride(winedll_now, "dxgi") != winedll_now)

    st_all = parse_presets(opts_now)
    # A plain name on the box; what it does, when to use it and the variable it sets underneath.
    toggle_texts = {  # label, caption
        "local_shader_cache": (tr("opt.local_shader_cache"), tr("opt.local_shader_cache_hint")),
        "disable_nvapi": (tr("opt.disable_nvapi"), tr("opt.disable_nvapi_hint")),
        "hide_nvidia_gpu": (tr("opt.hide_nvidia_gpu"), tr("opt.hide_nvidia_gpu_hint")),
        "vendor_amd": (tr("opt.vendor_amd"), tr("opt.vendor_amd_hint")),
        "descriptor_heap": (tr("opt.descriptor_heap"), tr("opt.descriptor_heap_hint")),
        "dxvk_d3d8": (tr("opt.dxvk_d3d8"), tr("opt.dxvk_d3d8_hint")),
        "no_d3d11": (tr("opt.no_d3d11"), tr("opt.no_d3d11_hint")),
        "no_ntsync": (tr("opt.no_ntsync"), tr("opt.no_ntsync_hint")),
        "heap_delay_free": (tr("opt.heap_delay_free"), tr("opt.heap_delay_free_hint")),
        "old_gl_string": (tr("opt.old_gl_string"), tr("opt.old_gl_string_hint")),
        "integer_scaling": (tr("opt.integer_scaling"), tr("opt.integer_scaling_hint")),
        "no_xalia": (tr("opt.no_xalia"), tr("opt.no_xalia_hint")),
    }
    toggle_checks: dict[str, QCheckBox] = {}
    for toggle in TOGGLES:
        chk = QCheckBox(toggle_texts[toggle.key][0])
        chk.setChecked(bool(st_all.get(toggle.key)))
        toggle_checks[toggle.key] = chk
    # Frame cap: one field for every API (DXVK, VKD3D-Proton and MangoHud), with the values of this
    # screen first (Qt knows its refresh rate on X11, Wayland and gamescope alike).
    screen_hz = screen_refresh_hz(dlg)
    frame_combo = QComboBox()
    frame_combo.addItem(tr("launch.mangohud_fps_unlimited"), 0)
    if screen_hz:
        frame_combo.addItem(tr("launch.fps_half_screen", fps=half_rate(screen_hz)), half_rate(screen_hz))
        frame_combo.addItem(tr("launch.fps_full_screen", fps=screen_hz), screen_hz)
    for fps_val in FRAME_RATES:
        frame_combo.addItem(tr("launch.mangohud_fps_value", fps=fps_val), fps_val)
    frame_combo.addItem(tr("launch.fps_other"), -1)
    current_rate = int(st_all.get("frame_rate") or 0)
    frame_idx = frame_combo.findData(current_rate)
    if frame_idx < 0:  # a value typed by hand: keep it selectable
        frame_combo.insertItem(frame_combo.count() - 1, tr("launch.mangohud_fps_value", fps=current_rate), current_rate)
        frame_idx = frame_combo.findData(current_rate)
    frame_combo.setCurrentIndex(frame_idx)
    last_frame = {"idx": frame_idx}

    def _on_frame_picked(idx: int) -> None:
        if frame_combo.itemData(idx) != -1:
            last_frame["idx"] = idx
            return
        value, ok = QInputDialog.getInt(dlg, tr("launch.frame_rate_label"), tr("launch.fps_other_prompt"), 60, 10, 500)
        if not ok:
            frame_combo.setCurrentIndex(last_frame["idx"])
            return
        found = frame_combo.findData(value)
        if found < 0:
            frame_combo.insertItem(frame_combo.count() - 1, tr("launch.mangohud_fps_value", fps=value), value)
            found = frame_combo.findData(value)
        frame_combo.setCurrentIndex(found)
        last_frame["idx"] = found

    winedll_lbl = QLabel(tr("launch.winedlloverrides_label"))
    winedll_edit = QLineEdit(winedll_now)

    wined3d_warn = note_label(tr("launch.wined3d_vulkan_warn"), "UnavailableBanner")
    wined3d_warn.setVisible(False)

    raw_edit = QLineEdit(opts_now)
    raw_edit.setObjectName("gameLaunchOptions")

    syncing = {"active": False}

    def _update_wined3d_warning() -> None:
        if is_native_linux or not chk_w3d.isChecked():
            wined3d_warn.setVisible(False)
            return
        wined3d_warn.setVisible(chk_dxgi.isChecked())  # DXGI/OptiScaler need DXVK, which wined3d turns off

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

    checks = {
        "gamemode": chk_gm, "mangohud": chk_mh, "wined3d": chk_w3d, "no_fsync": chk_fsync,
        "prime": chk_prime, "debug_log": chk_debug, **toggle_checks,
    }

    def _widget_state() -> dict[str, object]:
        return {
            **{key: chk.isChecked() for key, chk in checks.items()},
            "mh_level": mh_level_combo.currentData(),
            "mh_position": str(mh_pos_combo.currentData() or ""),
            "frame_rate": max(0, int(frame_combo.currentData() or 0)),
        }

    previous = _widget_state()

    def _sync_preview() -> None:
        mh_level_combo.setEnabled(chk_mh.isChecked())
        mh_pos_combo.setEnabled(chk_mh.isChecked())
        if syncing["active"]:
            return
        state = _widget_state()
        changes = {key: value for key, value in state.items() if value != previous[key]}
        if not changes:
            return
        try:
            opts = raw_edit.text()
            if mangohud_note:
                changes["mangohud"] = False
            raw_edit.setText(apply_presets(
                opts, {**parse_presets(opts), **changes}, winedlloverrides=dll_arg, native_linux=is_native_linux,
            ))
        except ValueError:
            return
        previous.update(state)
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
        if syncing["active"]:
            return
        syncing["active"] = True
        try:
            has_dxgi = remove_winedlloverride(text, "dxgi") != text
            chk_dxgi.setChecked(has_dxgi)
            if dll_in_line:
                raw_edit.setText(apply_presets(raw_edit.text(), winedlloverrides=text.strip()))
        except ValueError:
            pass
        finally:
            syncing["active"] = False

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

    def _on_opti_saved(new_opts: str) -> None:
        """OptiScaler changed the game's DLL overrides: show them here so OK keeps them."""
        if not dlg_alive():
            return
        if kind == "standalone":
            new_dll = str(getattr(game_obj, "winedlloverrides", "") or "")
        else:
            new_dll = str(parse_presets(new_opts).get("winedll") or "")
        winedll_edit.setText(new_dll)
        _update_optiscaler_dxgi_guard()

    def _add_optiscaler_card() -> None:
        """OptiScaler's own options, right in the list (an installed game only)."""
        from .prefix_tools_dialogs import build_optiscaler_panel

        _section(tr("launch.optiscaler_title"))
        _add(_muted_hint(tr("configure.optiscaler_hint")))
        usable = game_obj is not None and kind in ("steam", "standalone", "external")
        if usable:
            assert game_obj is not None
            _add(build_optiscaler_panel(win, dlg, kind, game_obj, on_saved=_on_opti_saved))
        else:
            _add(note_label(tr("launch.optiscaler_after_install")))
        if proton == "cachyos":
            _add(note_label(tr("launch.optiscaler_cachyos")))

    gpu_choice = QComboBox()
    gpu_choice.setObjectName("gameGpu")
    primary = profile.primary
    dedicated = profile.dedicated
    automatic = tr("launch.gpu_auto", name=_gpu_label(primary)) if primary else tr("launch.gpu_automatic")
    gpu_choice.addItem(automatic, False)
    if dedicated is not None or prime_active:
        gpu_choice.addItem(_gpu_label(dedicated) if dedicated else tr("launch.prime_offload"), True)
    gpu_choice.setCurrentIndex(gpu_choice.findData(prime_active))
    gpu_choice.setEnabled(gpu_choice.count() > 1)
    gpu_row = QHBoxLayout()
    gpu_row.addWidget(gpu_choice, 1)
    if dedicated is not None:
        gpu_name = dedicated.name.removeprefix("NVIDIA GeForce ").removesuffix(" Laptop GPU")
        use_gpu = QPushButton(tr("launch.use_gpu", name=gpu_name or dedicated.vendor.upper()))
        use_gpu.clicked.connect(lambda: gpu_choice.setCurrentIndex(gpu_choice.findData(True)))
        gpu_choice.currentIndexChanged.connect(lambda: use_gpu.setVisible(not bool(gpu_choice.currentData())))
        use_gpu.setVisible(not prime_active)
        gpu_row.addWidget(use_gpu)
    quick.addRow(tr("launch.gpu_label"), gpu_row)
    frame_combo.setObjectName("gameFps")
    quick.addRow(tr("launch.frame_rate_label"), frame_combo)
    gpu_choice.currentIndexChanged.connect(lambda: chk_prime.setChecked(bool(gpu_choice.currentData())))

    _section(tr("launch.section_performance"))
    _add_tool_preset(chk_gm, tr("opt.gamemode_hint"), "gamemoderun")
    _add_tool_preset(chk_mh, tr("opt.mangohud_hint"), "mangohud", extra_row=mh_opts_col, blocked_note=mangohud_note)
    mh_level_combo.currentIndexChanged.connect(_sync_preview)
    mh_pos_combo.currentIndexChanged.connect(_sync_preview)
    frame_combo.activated.connect(_on_frame_picked)
    frame_combo.currentIndexChanged.connect(_sync_preview)

    # Options that depend on the hardware or the Proton: shown, greyed out with the reason, or
    # hidden (another GPU vendor), always judged for the GPU the game runs on.
    ruled: dict[str, tuple[QCheckBox, QLabel, str]] = {}
    reasons = {
        "need_nvidia_driver": tr("launch.need_nvidia_driver"),
        "need_nvidia_595": tr("launch.need_nvidia_595"),
    }

    def _ruled(key: str, chk: QCheckBox, hint_text: str) -> None:
        chk.toggled.connect(_sync_preview)
        hint = _muted_hint(hint_text)
        _add(chk)
        _add(hint)
        ruled[key] = (chk, hint, hint_text)

    def _add_toggles(section: str) -> None:
        for toggle in TOGGLES:
            if toggle.section == section:
                _ruled(toggle.key, toggle_checks[toggle.key], toggle_texts[toggle.key][1])

    def _refresh_rules() -> None:
        game_gpu = profile.game_gpu(chk_prime.isChecked())
        for key, (chk, hint, normal) in ruled.items():
            state, reason = availability(key, game_gpu, profile, proton)
            on = chk.isChecked()  # an option already on stays usable, so it can be turned off
            chk.setVisible(state != HIDE or on)
            hint.setVisible(state != HIDE or on)
            chk.setEnabled(state == OK or on)
            hint.setText(reasons.get(reason, normal) if state == OFF else normal)
        # A card whose every option is hidden on this GPU goes too (no empty "GPU" card).
        for card, card_lay in section_cards:
            items = (card_lay.itemAt(i) for i in range(card_lay.count()))
            boxes = [item.widget() for item in items if item is not None]
            checks_in = [w for w in boxes if isinstance(w, QCheckBox)]
            others = [w for w in boxes if w is not None and not isinstance(w, (QCheckBox, QLabel))]
            card.setVisible(bool(others) or not checks_in or any(not c.isHidden() for c in checks_in))

    def _gpu_changed() -> None:
        _sync_preview()
        _refresh_rules()

    chk_prime.toggled.connect(_gpu_changed)
    if not is_native_linux:
        _add_toggles("performance")  # still in Performance

        _section(tr("launch.section_gpu"))
        _add_toggles("gpu")

        _add_optiscaler_card()

        _section(tr("launch.section_compat"))
        for chk, hint in (
            (chk_w3d, tr("opt.wined3d_hint")),
            (chk_fsync, tr("opt.no_fsync_hint")),
        ):
            chk.toggled.connect(_sync_preview)
            _add(chk)
            _add(_muted_hint(hint))
        _add_toggles("compat")
        chk_dxgi.toggled.connect(_on_dxgi_toggled)
        _add(chk_dxgi)
        _add(_muted_hint(tr("opt.dxgi_hint")))
        _add(wined3d_warn)
    else:
        for chk in (chk_w3d, chk_fsync, chk_dxgi, *toggle_checks.values()):
            chk.setVisible(False)
            chk.setEnabled(False)

    # Advanced: the debug log and the raw DLL overrides (Wine games only).
    if not is_native_linux:
        _section(tr("launch.section_advanced"))
        chk_debug.toggled.connect(_sync_preview)
        _add(chk_debug)
        _add(_muted_hint(tr("opt.debug_log_hint")))
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
    winedll_edit.textChanged.connect(_on_winedll_text_changed)
    raw_edit.textChanged.connect(_on_raw_text_changed)
    _update_optiscaler_dxgi_guard()
    _update_wined3d_warning()
    _refresh_rules()

    result_card, result_lay, _top = section_card(tr("launch.raw_label"))
    result_lay.setSpacing(4)
    result_lay.addWidget(raw_edit)
    result_lay.addWidget(note_label(tr("launch.raw_hint")))
    if kind == "external":
        result_lay.addWidget(note_label(tr("launch.external_nondestructive_note")))
    details_layout.addWidget(result_card)
    if extra_options is not None:
        details_layout.addWidget(extra_options)
        extra_options.show()

    def set_options(opts: str, dll: str) -> None:
        state = parse_presets(opts)
        syncing["active"] = True
        try:
            for key, chk in checks.items():
                chk.setChecked(bool(state.get(key)))
            gpu_idx = gpu_choice.findData(chk_prime.isChecked())
            if gpu_idx < 0:  # PRIME in the line on a PC without a known dedicated GPU
                gpu_choice.addItem(tr("launch.prime_offload"), True)
                gpu_choice.setEnabled(True)
                gpu_idx = gpu_choice.findData(True)
            gpu_choice.setCurrentIndex(gpu_idx)
            rate = int(state.get("frame_rate") or 0)
            idx = frame_combo.findData(rate)
            if idx < 0:
                idx = frame_combo.count() - 1
                frame_combo.insertItem(idx, tr("launch.mangohud_fps_value", fps=rate), rate)
            frame_combo.setCurrentIndex(idx)
            last_frame["idx"] = idx
            mh_level_combo.setCurrentIndex(max(0, mh_level_combo.findData(state.get("mh_level"))))
            mh_pos_combo.setCurrentIndex(max(0, mh_pos_combo.findData(state.get("mh_position"))))
            chk_dxgi.setChecked(remove_winedlloverride(dll, "dxgi") != dll)
            winedll_edit.setText(dll)
            raw_edit.setText(opts)
        finally:
            syncing["active"] = False
        previous.update(_widget_state())
        _refresh_rules()
        _update_wined3d_warning()

    suggestions: QWidget | None = None

    def set_proton(new_proton: str) -> None:
        nonlocal proton, suggestions
        proton = new_proton
        _refresh_rules()
        if more_button.isChecked():
            _load_recommendations()
        elif suggestions is not None:
            details_layout.removeWidget(suggestions)
            suggestions.hide()
            suggestions.deleteLater()
            suggestions = None

    def read_raw_edit() -> None:
        opts = raw_edit.text()
        typed_dll = str(parse_presets(opts).get("winedll") or "")
        if dll_in_line:
            dll = typed_dll
        else:
            dll = winedll_edit.text().strip()
            if typed_dll:
                dll = merge_dll_overrides(dll, typed_dll) if dll else typed_dll
                try:
                    opts = apply_presets(opts, winedlloverrides="", native_linux=is_native_linux)
                except ValueError:
                    pass
        set_options(opts, dll)

    raw_edit.editingFinished.connect(read_raw_edit)
    editor = LaunchOptions(pane, raw_edit, winedll_edit, set_options, set_proton)

    def _load_recommendations() -> None:
        nonlocal suggestions
        if more is None:
            return
        if suggestions is not None:
            details_layout.removeWidget(suggestions)
            suggestions.hide()
            suggestions.deleteLater()
        suggestions = QWidget(details)
        suggestions_layout = QVBoxLayout(suggestions)
        suggestions_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.insertWidget(0, suggestions)
        more(suggestions, suggestions_layout, editor)

    compact_height = dlg.height()

    def _toggle_more(opened: bool) -> None:
        nonlocal compact_height
        if opened:
            compact_height = dlg.height()
        details.setVisible(opened)
        dlg.resize(dlg.width(), max(compact_height, 560) if opened else compact_height)
        more_button.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        if opened and suggestions is None:
            _load_recommendations()

    more_button.toggled.connect(_toggle_more)
    return editor


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
    proton: str = "",
    *,
    proton_picker: QComboBox | None = None,
    more: Callable[[QWidget, QVBoxLayout, LaunchOptions], None] | None = None,
) -> tuple[str, str] | None:
    """Edit the same launch controls before installing a game."""
    from ..ui_cards import clamp_dialog_size

    dlg = QDialog(parent if parent is not None else _dialog_parent(win))
    dlg.setWindowTitle(tr("launch.title", name=title))
    clamp_dialog_size(dlg, 620, 265, parent)
    dlg_lay = QVBoxLayout(dlg)
    scroll = QScrollArea(dlg)
    scroll.setWidgetResizable(True)
    body = QWidget(scroll)
    layout = QVBoxLayout(body)
    editor = build_launch_options(
        win, dlg, kind, initial_opts, initial_winedll, game_obj, is_native_linux, mode, proton,
        proton_control=proton_picker, more=more,
    )
    layout.addWidget(editor.widget)
    layout.addStretch()
    scroll.setWidget(body)
    dlg_lay.addWidget(scroll, 1)
    if proton_picker is not None:
        proton_picker.currentIndexChanged.connect(
            lambda: editor.set_proton(proton_kind(str(proton_picker.currentData() or "")) or proton)
        )

    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
    )
    apply = btns.button(QDialogButtonBox.StandardButton.Apply)
    apply.clicked.connect(dlg.accept)
    original = editor.values()
    original_proton = proton_picker.currentData() if proton_picker is not None else None

    def update_apply() -> None:
        apply.setEnabled(
            editor.values() != original
            or (proton_picker is not None and proton_picker.currentData() != original_proton)
        )

    editor.raw.textChanged.connect(update_apply)
    editor.dll.textChanged.connect(update_apply)
    if proton_picker is not None:
        proton_picker.currentIndexChanged.connect(update_apply)
    update_apply()
    btns.rejected.connect(dlg.reject)
    dlg_lay.addWidget(btns)

    try:
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        new_opts_out, new_dll_out = editor.values()
        win._save_last_launch_prefs(new_opts_out, new_dll_out)
        return new_opts_out, new_dll_out
    finally:
        dlg.deleteLater()


def launch_options_of(obj: object) -> tuple[str, str]:
    """(launch options, WINEDLLOVERRIDES) a game has now."""
    if isinstance(obj, StandaloneGame):
        return obj.launch_options or "", obj.winedlloverrides or ""
    if isinstance(obj, ExternalLauncherGame):
        opts = load_external_launch_presets(obj)
    elif isinstance(obj, ShortcutEntry):
        opts = obj.launch_options or ""
    else:
        return "", ""
    return opts, str(_parse_launch_preset_state(opts)["winedll"])


def save_launch_options(
    win: MainWindow,
    sel: tuple[str, object],
    new_opts: str,
    new_dll: str,
    parent: QWidget | None = None,
    on_saved: Callable[[str], None] | None = None,
    on_failed: Callable[[], None] | None = None,
) -> None:
    """Store a game's new launch options where its mode keeps them (manifest + launch.sh, the
    external launcher, or Steam's shortcut: right away, or queued in Gaming Mode)."""
    kind, obj = sel
    fail = on_failed or (lambda: None)

    def box_parent() -> QWidget:
        if parent is not None and shiboken6.isValid(parent):
            return parent
        return _dialog_parent(win)

    title_name = obj.app_name if isinstance(obj, ShortcutEntry) else str(getattr(obj, "name", ""))
    opts_now = launch_options_of(obj)[0]

    if isinstance(obj, ExternalLauncherGame):
        try:
            save_external_launch_presets(obj, new_opts)
        except Exception as exc:
            QMessageBox.critical(box_parent(), __app_name__, str(exc))
            fail()
            return
        msg = tr("launch.saved", name=title_name)
        win.status_label.setText(msg)
        if on_saved is not None:
            on_saved(new_opts)
        if on_failed is None and not win._closing_pending:
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
            fail()
            return
        win._refresh_shortcuts()
        msg = tr("launch.saved", name=title_name)
        win.status_label.setText(msg)
        if on_saved is not None:
            on_saved(new_opts)
        if on_failed is None and not win._closing_pending:
            QMessageBox.information(box_parent(), __app_name__, msg)
        return

    if not win.paths:
        fail()
        return
    paths = win.paths
    if not isinstance(obj, ShortcutEntry):
        fail()
        return
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
        saved_opts = launch_options_of(managed_game) if managed_game is not None else ("", "")
        try:
            queued_update = _sync_managed_launch_opts()
        except Exception as exc:
            QMessageBox.critical(box_parent(), __app_name__, str(exc))
            fail()
            return
        if not win.enqueue_vdf_edit(
            QueuedVdfEdit(
                edit_id=f"set_launch_options:{entry.appid_u32}",
                op="set_launch_options",
                appid_u32=entry.appid_u32,
                label=title_name,
                payload={"launch_options": new_opts},
            )
        ):
            if managed_game is not None:
                try:
                    update_standalone_launch_options(
                        managed_game.game_dir, winedlloverrides=saved_opts[1], launch_options=saved_opts[0],
                    )
                except Exception as exc:
                    QMessageBox.critical(box_parent(), __app_name__, str(exc))
            fail()
            return
        _apply_managed_result(queued_update)
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
            if on_failed is None and not win._closing_pending:
                QMessageBox.information(box_parent(), __app_name__, msg)

        def _err(err: str) -> None:
            win._end_busy()
            if not win._closing_pending:
                QMessageBox.critical(box_parent(), __app_name__, err)
            fail()

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    win._confirm_close_steam_async(
        _proceed_update_steam_opts,
        on_cancelled=fail,
        parent=box_parent(),
        alive_fn=lambda: bool(win and not win._closing_pending),
    )
