""""Remove everything DeckInstall added": one checkbox per part, games and saves never included."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import remove_all as ra
from ..i18n import tr


def _part_label(part: ra.RemovalPart) -> str:
    if part.key == ra.RUNTIMES:
        return tr("remove_all.part_runtimes", count=len(part.paths))
    if part.key == ra.APP_DATA:
        return tr("remove_all.part_app_data")
    if part.key == ra.MIGRATION_BACKUPS:
        return tr("remove_all.part_migrations")
    if part.key == ra.SYSTEM_CHANGES:
        return tr("remove_all.part_system_changes")
    return part.key


class RemoveAllDialog(QDialog):
    def __init__(self, parts: list[ra.RemovalPart], games: int, kept: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("remove_all.title"))
        lay = QVBoxLayout(self)
        intro = QLabel(tr("remove_all.intro", games=games))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self._boxes: list[tuple[QCheckBox, ra.RemovalPart]] = []
        for part in parts:
            box = QCheckBox(_part_label(part))
            box.setChecked(part.default_on)
            if part.paths:
                box.setToolTip("\n".join(str(p) for p in part.paths))
            lay.addWidget(box)
            self._boxes.append((box, part))
        if kept:
            note = QLabel(tr("remove_all.kept", files="\n".join(kept)))
            note.setObjectName("MutedText")
            note.setWordWrap(True)
            lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.addButton(tr("remove_all.btn"), QDialogButtonBox.ButtonRole.DestructiveRole)
        self._ok.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        for box, _ in self._boxes:
            box.toggled.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self._ok.setEnabled(any(box.isChecked() for box, _ in self._boxes))

    def chosen(self) -> list[ra.RemovalPart]:
        return [part for box, part in self._boxes if box.isChecked()]
