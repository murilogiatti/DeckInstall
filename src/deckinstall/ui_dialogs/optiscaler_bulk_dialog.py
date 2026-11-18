"""Bulk OptiScaler install/remove for DeckInstall games, with per-game compatibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import __app_name__
from ..i18n import tr
from ..optiscaler import (
    OPTISCALER_BACKENDS,
    BulkOutcome,
    OptiScalerCompatibility,
    bulk_optiscaler,
    check_optiscaler_compatibility,
)
from ..standalone import StandaloneGame, list_standalone_games
from ..ui_cards import _dialog_parent
from ..ui_workers import SteamOpWorker

if TYPE_CHECKING:
    from ..ui import MainWindow


def _scan() -> list[tuple[StandaloneGame, OptiScalerCompatibility]]:
    return [
        (g, check_optiscaler_compatibility(g.exe_path, g.game_dir))
        for g in list_standalone_games()
        if not g.native_linux
    ]


def _tier_text(compat: OptiScalerCompatibility) -> str:
    if not compat.compatible:
        return tr("optiscaler_bulk.tier_incompatible", arch=compat.pe_arch)
    if compat.tier == "verified":
        return tr("optiscaler_bulk.tier_verified", upscalers=", ".join(compat.upscalers))
    return tr("optiscaler_bulk.tier_likely")


def _state_text(compat: OptiScalerCompatibility) -> str:
    st = compat.installed_state
    if st is None:
        return tr("optiscaler_bulk.state_none", proxy=f"{compat.recommended_proxy}.dll")
    if not st.complete:
        return tr("optiscaler_bulk.state_incomplete")
    return tr("optiscaler_bulk.state_installed", proxy=f"{st.proxy_dll}.dll", version=st.version or "-")


def _outcome_line(o: BulkOutcome) -> str:
    labels = {
        "installed": tr("optiscaler_bulk.out_installed"),
        "removed": tr("optiscaler_bulk.out_removed"),
        "skipped": tr("optiscaler_bulk.out_skipped"),
        "conflict": tr("optiscaler_bulk.out_conflict"),
        "failed": tr("optiscaler_bulk.out_failed"),
    }
    detail = f" ({o.detail})" if o.detail else ""
    return f"{o.name}: {labels.get(o.status, o.status)}{detail}"


def bulk_optiscaler_dialog(win: MainWindow) -> None:
    """Scan DeckInstall games in the background, then open the bulk OptiScaler dialog."""
    win.status_label.setText(tr("optiscaler_bulk.scanning"))
    loader = SteamOpWorker(_scan, need_steam_closed=False)
    win._track_worker(loader)

    def _loaded(rows: object) -> None:
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending:
            return
        if not isinstance(rows, list) or not rows:
            QMessageBox.information(win, __app_name__, tr("proton_games.empty"))
            return
        _show(win, rows)

    def _failed(err: str) -> None:
        win.status_label.setText(tr("status.ready"))
        QMessageBox.critical(win, __app_name__, tr("optiscaler.error", err=err))

    loader.finished_ok.connect(_loaded)
    loader.failed.connect(_failed)
    loader.start()


def _show(win: MainWindow, rows: list[tuple[StandaloneGame, OptiScalerCompatibility]]) -> None:
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("optiscaler_bulk.title"))
    dlg.resize(820, 540)
    lay = QVBoxLayout(dlg)
    hint = QLabel(tr("optiscaler_bulk.hint"))
    hint.setWordWrap(True)
    lay.addWidget(hint)

    tbl = QTableWidget(0, 3)
    tbl.setHorizontalHeaderLabels(
        [tr("table.name"), tr("optiscaler_bulk.col_compat"), tr("optiscaler_bulk.col_state")]
    )
    tbl.verticalHeader().setVisible(False)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    lay.addWidget(tbl, 1)

    games: list[StandaloneGame] = []

    def _fill(data: list[tuple[StandaloneGame, OptiScalerCompatibility]]) -> None:
        games.clear()
        tbl.setRowCount(0)
        for game, compat in data:
            row = tbl.rowCount()
            tbl.insertRow(row)
            games.append(game)
            it = QTableWidgetItem(game.name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # Pre-check games where OptiScaler is known to have something to hook into.
            it.setCheckState(
                Qt.CheckState.Checked
                if compat.compatible and compat.tier == "verified" and compat.installed_state is None
                else Qt.CheckState.Unchecked
            )
            if not compat.compatible:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, QTableWidgetItem(_tier_text(compat)))
            tbl.setItem(row, 2, QTableWidgetItem(_state_text(compat)))

    _fill(rows)

    form = QFormLayout()
    combo_backend = QComboBox()
    for b in OPTISCALER_BACKENDS:
        combo_backend.addItem(b.upper() if b != "auto" else tr("optiscaler.backend_auto"), b)
    form.addRow(tr("optiscaler.backend_label"), combo_backend)
    lay.addLayout(form)

    btn_row = QHBoxLayout()
    btn_install = QPushButton(tr("optiscaler_bulk.btn_install"))
    btn_install.setObjectName("primaryBtn")
    btn_remove = QPushButton(tr("optiscaler_bulk.btn_remove"))
    btn_close = QPushButton(tr("proton_bulk.btn_close"))
    btn_row.addWidget(btn_install)
    btn_row.addWidget(btn_remove)
    btn_row.addStretch()
    btn_row.addWidget(btn_close)
    lay.addLayout(btn_row)
    btn_close.clicked.connect(dlg.accept)

    def _checked() -> list[StandaloneGame]:
        out: list[StandaloneGame] = []
        for i, game in enumerate(games):
            it = tbl.item(i, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(game)
        return out

    def _run(remove: bool) -> None:
        picked = _checked()
        if not picked:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        backend = str(combo_backend.currentData() or "auto")
        for b in (btn_install, btn_remove):
            b.setEnabled(False)

        def _job() -> tuple[list[BulkOutcome], list[tuple[StandaloneGame, OptiScalerCompatibility]]]:
            outcomes = bulk_optiscaler(list(picked), remove=remove, backend=backend)
            return outcomes, _scan()

        worker = SteamOpWorker(_job, need_steam_closed=False)
        win._track_worker(worker)

        def _done(result: object) -> None:
            for b in (btn_install, btn_remove):
                b.setEnabled(True)
            if not isinstance(result, tuple):
                return
            outcomes, fresh = result
            win._refresh_shortcuts()
            if dlg.isVisible():
                _fill(fresh)
            QMessageBox.information(dlg, __app_name__, "\n".join(_outcome_line(o) for o in outcomes))

        def _fail(err: str) -> None:
            for b in (btn_install, btn_remove):
                b.setEnabled(True)
            QMessageBox.critical(dlg, __app_name__, tr("optiscaler.error", err=err))

        worker.finished_ok.connect(_done)
        worker.failed.connect(_fail)
        worker.start()

    btn_install.clicked.connect(lambda: _run(False))
    btn_remove.clicked.connect(lambda: _run(True))
    dlg.exec()
