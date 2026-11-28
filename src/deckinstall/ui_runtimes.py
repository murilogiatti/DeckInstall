"""Runtimes tab: one card per component (Proton, umu + Steam Runtime, MangoHud, OptiScaler, tools).

Each card shows the component's status and only its own actions. What the system's mode does not
use is greyed out with the reason (SteamOS ships Proton and a performance overlay; Steam-mode games
use Steam's own Proton and runtime).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from .i18n import tr
from .runtime_uninstall import GE, MANGOHUD, OPTISCALER, STEAMRT4, UMU
from .ui_cards import FlowLayout, _make_card, page_layout

if TYPE_CHECKING:
    from .ui import MainWindow


def _status_label() -> QLabel:
    lbl = QLabel(tr("runtimes.loading"))  # filled by the health check; never an empty gap meanwhile
    lbl.setWordWrap(True)
    return lbl


def _note_label() -> QLabel:
    lbl = QLabel()
    lbl.setObjectName("MutedText")
    lbl.setWordWrap(True)
    lbl.setVisible(False)
    return lbl


def _component_card(
    title: str, about: str, labels: list[QLabel], buttons: list[QPushButton], note: QLabel
) -> QWidget:
    card, layout = _make_card()
    head = QLabel(title)
    head.setObjectName("SectionTitle")
    layout.addWidget(head)
    desc = QLabel(about)
    desc.setObjectName("MutedText")
    desc.setWordWrap(True)
    layout.addWidget(desc)
    status = QHBoxLayout()  # one status line, its parts side by side
    status.setSpacing(6)
    for i, lbl in enumerate(labels):
        if i:
            dot = QLabel("·")
            dot.setObjectName("MutedText")
            status.addWidget(dot)
        status.addWidget(lbl)
    status.addStretch()
    layout.addLayout(status)
    layout.addWidget(note)
    row = FlowLayout(spacing=8)
    for btn in buttons:
        row.addWidget(btn)
    layout.addLayout(row)
    return card


def build_runtimes_page(win: MainWindow) -> QWidget:
    """Build the Runtimes tab and bind its labels and buttons onto ``win``."""
    from .ui_maintenance import open_uninstall_runtimes_dialog

    page, layout = page_layout(tr("nav.runtimes"), tr("runtimes.page_subtitle"))

    from .ui_updates import build_updates_card

    layout.addWidget(build_updates_card(win))

    def _remove(kinds: set[str]) -> QPushButton:
        btn = QPushButton(tr("runtimes.btn_remove"))
        btn.clicked.connect(lambda: open_uninstall_runtimes_dialog(win, kinds=kinds))
        return btn

    def _update(component: str, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.clicked.connect(lambda: win._update_runtimes((component,)))
        return btn

    # Proton
    win.lbl_maint_proton = _status_label()
    win.lbl_rt_steam_proton = _status_label()
    win.btn_rt_ge_update = _update("ge_proton", tr("runtimes.btn_ge_update"))
    win.lbl_rt_proton_note = _note_label()
    layout.addWidget(_component_card(
        tr("runtimes.proton_title"), tr("runtimes.proton_about"),
        [win.lbl_rt_steam_proton, win.lbl_maint_proton],
        [win.btn_rt_ge_update, _remove({GE})], win.lbl_rt_proton_note,
    ))

    # umu-run + Steam Runtime (Standalone mode only)
    win.lbl_maint_umu = _status_label()
    win.lbl_maint_steamrt4 = _status_label()
    win.btn_rt_umu_update = _update("umu", tr("runtimes.btn_umu_update"))
    win.lbl_rt_umu_note = _note_label()
    layout.addWidget(_component_card(
        tr("runtimes.umu_title"), tr("runtimes.umu_about"),
        [win.lbl_maint_umu, win.lbl_maint_steamrt4],
        [win.btn_rt_umu_update, _remove({UMU, STEAMRT4})], win.lbl_rt_umu_note,
    ))

    # MangoHud (Standalone mode only)
    win.lbl_maint_mangohud = _status_label()
    win.btn_rt_mangohud_update = _update("mangohud", tr("runtimes.btn_mangohud_update"))
    win.lbl_rt_mangohud_note = _note_label()
    layout.addWidget(_component_card(
        tr("runtimes.mangohud_title"), tr("runtimes.mangohud_about"),
        [win.lbl_maint_mangohud],
        [win.btn_rt_mangohud_update, _remove({MANGOHUD})], win.lbl_rt_mangohud_note,
    ))

    # OptiScaler (both modes)
    win.lbl_rt_optiscaler = _status_label()
    win.btn_rt_optiscaler_update = _update("optiscaler", tr("runtimes.btn_optiscaler_update"))
    layout.addWidget(_component_card(
        tr("runtimes.optiscaler_title"), tr("runtimes.optiscaler_about"),
        [win.lbl_rt_optiscaler],
        [win.btn_rt_optiscaler_update, _remove({OPTISCALER})], _note_label(),
    ))

    # Tools found on the system (read only)
    win.lbl_rt_tools = _status_label()
    layout.addWidget(_component_card(
        tr("runtimes.tools_title"), tr("runtimes.tools_about"), [win.lbl_rt_tools], [], _note_label(),
    ))
    layout.addStretch(1)
    apply_runtimes_mode(win)
    return page


def apply_runtimes_mode(win: MainWindow) -> None:
    """Grey out what this system's mode does not use, and say why."""
    env = win.mode_env
    for component, button, note in (
        ("ge_proton", win.btn_rt_ge_update, win.lbl_rt_proton_note),
        ("umu", win.btn_rt_umu_update, win.lbl_rt_umu_note),
        ("mangohud", win.btn_rt_mangohud_update, win.lbl_rt_mangohud_note),
    ):
        blocked = env.component_note(component)
        button.setEnabled(not blocked)
        text = blocked or (tr("runtimes.note_standalone_only") if env.steam else "")
        note.setText(text)
        note.setVisible(bool(text))


def show_runtime_health(win: MainWindow, data: dict) -> None:
    """Fill the Runtimes cards from MaintHealthWorker's result."""
    protons = [str(p) for p in data.get("steam_protons") or []]
    win.lbl_rt_steam_proton.setText(
        tr("runtimes.steam_protons", names=", ".join(protons)) if protons else tr("runtimes.steam_protons_none")
    )
    win.lbl_rt_optiscaler.setText(tr("runtimes.optiscaler_status", status=data.get("optiscaler_status", "-")))
    tools = data.get("tools") or {}
    win.lbl_rt_tools.setText(
        "  ".join(f"{'✓' if ok else '✗'} {name}" for name, ok in sorted(tools.items()))
    )
