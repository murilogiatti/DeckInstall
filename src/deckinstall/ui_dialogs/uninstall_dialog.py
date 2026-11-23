"""Uninstall dialog: one checkbox per thing the game left on disk, with the space it frees."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import uninstall as un
from ..i18n import tr
from ..ui_cards import format_size_bytes


def _item_label(item_id: str) -> str:
    return {
        un.SHORTCUTS: tr("uninstall.item_shortcuts"),
        un.GAME_FILES: tr("uninstall.item_game_files"),
        un.SAVES: tr("uninstall.item_saves"),
        un.PREFIX: tr("uninstall.item_prefix"),
        un.STEAM: tr("uninstall.item_steam"),
        un.ICONS: tr("uninstall.item_icons"),
        un.OPTISCALER: tr("uninstall.item_optiscaler"),
        un.CACHE: tr("uninstall.item_cache"),
        un.LOGS: tr("uninstall.item_logs"),
    }.get(item_id, item_id)


def _reason_text(reason: str) -> str:
    return {
        un.REASON_REQUIRED: tr("uninstall.reason_required"),
        un.REASON_SHARED_PREFIX: tr("uninstall.reason_shared_prefix"),
        un.REASON_OUTSIDE_ROOTS: tr("uninstall.reason_outside_roots"),
        un.REASON_SYMLINK: tr("uninstall.reason_symlink"),
        un.REASON_PROTECTED: tr("uninstall.reason_protected"),
    }.get(reason, reason)


class UninstallDialog(QDialog):
    """Pick what to remove from an UninstallPlan; the total updates as boxes change."""

    def __init__(self, plan: un.UninstallPlan, also_steam: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plan = plan
        if plan.leftovers:
            self.setWindowTitle(tr("uninstall.leftovers_title", name=plan.game.name))
            intro_text = tr("uninstall.leftovers_intro", name=plan.game.name)
            action_text = tr("cleanup.btn_remove_leftovers")
        else:
            self.setWindowTitle(tr("uninstall.title", name=plan.game.name))
            intro_text = tr("uninstall.intro", name=plan.game.name)
            action_text = tr("uninstall.btn_uninstall")
        lay = QVBoxLayout(self)
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        if also_steam:
            note = QLabel(tr("uninstall.also_steam_note"))
            note.setWordWrap(True)
            lay.addWidget(note)

        grid = QGridLayout()
        self.boxes: dict[str, QCheckBox] = {}
        for row, item in enumerate(plan.items):
            box = QCheckBox(_item_label(item.id))
            box.setChecked(item.checked)
            box.setToolTip("\n".join(str(p) for p in item.paths))
            if item.locked:
                box.setEnabled(False)
            box.toggled.connect(self._refresh)
            self.boxes[item.id] = box
            grid.addWidget(box, row, 0)
            if plan.leftovers:
                location = QLabel("\n".join(str(path) for path in item.paths))
                location.setWordWrap(True)
                location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(location, row, 3)
            size = QLabel(format_size_bytes(item.size_bytes) if item.size_bytes else "")
            grid.addWidget(size, row, 1)
            if item.reason:
                why = QLabel(_reason_text(item.reason))
                why.setWordWrap(True)
                why.setEnabled(False)
                grid.addWidget(why, row, 2)
        grid.setColumnStretch(3 if plan.leftovers else 2, 1)
        lay.addLayout(grid)

        self.total_label = QLabel()
        lay.addWidget(self.total_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.btn_uninstall = buttons.addButton(
            action_text,
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.btn_uninstall.setObjectName("dangerBtn")
        self.btn_uninstall.setAutoDefault(False)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        cancel.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._refresh()

    def selected_ids(self) -> set[str]:
        return {i for i, box in self.boxes.items() if box.isChecked() and box.isEnabled()}

    def _refresh(self) -> None:
        for item in self.plan.items:
            if item.locked:
                continue
            if item.id == un.SAVES:
                self.boxes[item.id].setEnabled(
                    any(self.boxes[key].isChecked() for key in (un.PREFIX, un.GAME_FILES) if key in self.boxes)
                )
                continue
            parent = item.covered_by or item.requires
            if not parent or parent not in self.boxes:
                continue
            parent_on = self.boxes[parent].isChecked()
            # Covered items go with their parent; dependent ones only make sense with it.
            self.boxes[item.id].setEnabled(not parent_on if item.covered_by else parent_on)
        if self.plan.leftovers:
            self.btn_uninstall.setEnabled(bool(self.selected_ids()))
        freed = self.plan.freed_bytes(self.selected_ids())
        self.total_label.setText(tr("uninstall.total", size=format_size_bytes(freed)))


def ask_uninstall(parent: QWidget | None, plan: un.UninstallPlan, also_steam: bool = False) -> set[str] | None:
    """Show the dialog; return the chosen item ids, or None when cancelled."""
    dlg = UninstallDialog(plan, also_steam=also_steam, parent=parent)
    try:
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.selected_ids()
    finally:
        dlg.deleteLater()
