"""Runtimes: the updates card, then one line per component (Proton, umu + Steam Runtime, MangoHud,
OptiScaler, tools) with its status and only its own actions. What the system's mode does not
use is greyed out with the reason (SteamOS ships Proton and a performance overlay; Steam-mode games
use Steam's own Proton and runtime).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .i18n import tr
from .runtime_uninstall import MANGOHUD, OPTISCALER, PROTON, STEAMRT4, UMU
from .ui_cards import section_card, section_page

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


def _component_row(
    grid: QGridLayout, row: int, title: str, about: str, labels: list[QLabel],
    buttons: list[QPushButton], note: QLabel,
) -> None:
    """One line per component: its name (what it is for in the tooltip), its status, its actions."""
    name = QLabel(f"<b>{title}</b>")
    name.setToolTip(about)
    grid.addWidget(name, row, 0)
    status = QWidget()
    status_lay = QVBoxLayout(status)
    status_lay.setContentsMargins(0, 0, 0, 0)
    status_lay.setSpacing(2)
    line = QHBoxLayout()
    line.setSpacing(6)
    for i, lbl in enumerate(labels):
        if i:
            dot = QLabel("·")
            dot.setObjectName("MutedText")
            line.addWidget(dot)
        line.addWidget(lbl)
    line.addStretch()
    status_lay.addLayout(line)
    status_lay.addWidget(note)
    grid.addWidget(status, row, 1)
    for col, btn in enumerate(buttons, start=4 - len(buttons)):  # "Remove…" always in the last column
        btn.setToolTip(about)
        grid.addWidget(btn, row, col)


def build_runtimes_page(win: MainWindow) -> QWidget:
    """Build the Runtimes section and bind its labels and buttons onto ``win``."""
    from .ui_maintenance import open_uninstall_runtimes_dialog

    page, layout = section_page()

    from .ui_updates import build_updates_card

    layout.addWidget(build_updates_card(win))

    def _remove(kinds: set[str]) -> QPushButton:
        btn = QPushButton(tr("runtimes.btn_remove"))
        btn.clicked.connect(lambda: open_uninstall_runtimes_dialog(win, kinds=kinds))
        return btn

    def _update(component: str) -> QPushButton:
        """Only while the component is missing: updates are in the Updates card above."""
        btn = QPushButton(tr("runtimes.btn_download"))
        btn.clicked.connect(lambda: win._update_runtimes((component,)))
        return btn

    card, card_lay, _top = section_card(tr("runtimes.components_title"), tr("runtimes.components_hint"))
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(1, 1)
    card_lay.addLayout(grid)

    win.lbl_maint_proton = _status_label()
    win.lbl_rt_steam_proton = _status_label()
    win.btn_rt_proton_update = _update("umu_proton")
    win.lbl_rt_proton_note = _note_label()
    _component_row(
        grid, 0, tr("runtimes.proton_title"), tr("runtimes.proton_about"),
        [win.lbl_maint_proton, win.lbl_rt_steam_proton],
        [win.btn_rt_proton_update, _remove({PROTON})], win.lbl_rt_proton_note,
    )
    win.lbl_maint_umu = _status_label()
    win.lbl_maint_steamrt4 = _status_label()
    win.btn_rt_umu_update = _update("umu")
    win.lbl_rt_umu_note = _note_label()
    _component_row(
        grid, 1, tr("runtimes.umu_title"), tr("runtimes.umu_about"),
        [win.lbl_maint_umu, win.lbl_maint_steamrt4],
        [win.btn_rt_umu_update, _remove({UMU, STEAMRT4})], win.lbl_rt_umu_note,
    )
    win.lbl_maint_mangohud = _status_label()
    win.btn_rt_mangohud_update = _update("mangohud")
    win.lbl_rt_mangohud_note = _note_label()
    _component_row(
        grid, 2, tr("runtimes.mangohud_title"), tr("runtimes.mangohud_about"),
        [win.lbl_maint_mangohud], [win.btn_rt_mangohud_update, _remove({MANGOHUD})], win.lbl_rt_mangohud_note,
    )
    win.lbl_rt_optiscaler = _status_label()  # downloaded with its first install in a game
    _component_row(
        grid, 3, tr("runtimes.optiscaler_title"), tr("runtimes.optiscaler_about"),
        [win.lbl_rt_optiscaler], [_remove({OPTISCALER})], _note_label(),
    )
    win.lbl_rt_tools = _status_label()
    _component_row(
        grid, 4, tr("runtimes.tools_title"), tr("runtimes.tools_about"), [win.lbl_rt_tools], [], _note_label(),
    )
    layout.addWidget(card)
    layout.addStretch(1)
    apply_runtimes_mode(win)
    return page


def _installed(component: str) -> bool:
    from .runtime_manager import get_umu_run_path, list_default_proton_dirs, official_mangohud_installed

    if component == "umu":
        return get_umu_run_path().is_file()
    if component == "umu_proton":
        return bool(list_default_proton_dirs())
    return official_mangohud_installed()


def apply_runtimes_mode(win: MainWindow) -> None:
    """Grey out what this system's mode does not use, and say why; "Download" only for what is missing."""
    env = win.mode_env
    for component, button, note in (
        ("umu_proton", win.btn_rt_proton_update, win.lbl_rt_proton_note),
        ("umu", win.btn_rt_umu_update, win.lbl_rt_umu_note),
        ("mangohud", win.btn_rt_mangohud_update, win.lbl_rt_mangohud_note),
    ):
        blocked = env.component_note(component)
        button.setEnabled(not blocked)
        button.setVisible(not blocked and not _installed(component))
        text = blocked or (tr("runtimes.note_standalone_only") if env.steam else "")
        note.setText(text)
        note.setVisible(bool(text))


def show_runtime_health(win: MainWindow, data: dict) -> None:
    """Fill the Runtimes cards from MaintHealthWorker's result."""
    apply_runtimes_mode(win)  # something may have been installed or removed meanwhile
    protons = [str(p) for p in data.get("steam_protons") or []]
    win.lbl_rt_steam_proton.setText(
        tr("runtimes.steam_protons", names=", ".join(protons)) if protons else tr("runtimes.steam_protons_none")
    )
    win.lbl_rt_optiscaler.setText(tr("runtimes.optiscaler_status", status=data.get("optiscaler_status", "-")))
    tools = data.get("tools") or {}
    win.lbl_rt_tools.setText(
        "  ".join(f"{'✓' if ok else '✗'} {name}" for name, ok in sorted(tools.items()))
    )
