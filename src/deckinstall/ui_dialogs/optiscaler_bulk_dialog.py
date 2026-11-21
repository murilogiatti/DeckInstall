"""Bulk OptiScaler install/remove for DeckInstall games, Steam shortcuts and Steam store games."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
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
from ..launch_presets import apply_presets, parse_presets
from ..optiscaler import (
    OPTISCALER_BACKENDS,
    BulkOutcome,
    BulkTarget,
    OptiScalerCompatibility,
    bulk_optiscaler_targets,
    check_optiscaler_compatibility,
    standalone_bulk_target,
)
from ..standalone import (
    list_standalone_games,
    match_owned_shortcuts,
    primary_owned_shortcuts,
    update_standalone_launch_options,
)
from ..steam_paths import SteamPaths
from ..steam_vdf import (
    _strip_vdf_quotes,
    get_steam_app_launch_options,
    list_steam_library_games,
    load_shortcuts,
    set_steam_app_launch_options,
    update_shortcut_launch_options,
)
from ..ui_cards import _dialog_guard, _dialog_parent
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit, VdfEditOp

if TYPE_CHECKING:
    from ..ui import MainWindow


Row = tuple[BulkTarget, OptiScalerCompatibility]
# One pending launch-options write: (source "shortcut"/"steam", appid, name, new LaunchOptions).
VdfWrite = tuple[str, int, str, str]


def _collect_targets(paths: SteamPaths | None) -> list[BulkTarget]:
    games = [g for g in list_standalone_games() if not g.native_linux]
    if paths is None:
        return [standalone_bulk_target(g) for g in games]
    try:
        shortcuts = load_shortcuts(paths.shortcuts_vdf)
    except Exception:
        logging.exception("Could not read shortcuts.vdf for bulk OptiScaler")
        shortcuts = []
    owned = match_owned_shortcuts(shortcuts, games, paths.user_id)
    primary = primary_owned_shortcuts(shortcuts, owned)
    targets = [
        replace(standalone_bulk_target(g), steam_shortcut=primary.get(g.game_dir))
        for g in games
    ]
    seen_exes = {t.exe_path for t in targets}
    for sc in shortcuts:
        if sc.appid_u32 in owned:
            continue
        raw = _strip_vdf_quotes(sc.exe)
        # Only shortcuts that point straight at a Windows .exe (DeckInstall's launch.sh games are listed above).
        if not raw.lower().endswith(".exe"):
            continue
        exe = Path(raw)
        if exe in seen_exes or not exe.is_file():
            continue
        seen_exes.add(exe)
        start = Path(_strip_vdf_quotes(sc.start_dir)) if sc.start_dir else exe.parent
        targets.append(
            BulkTarget(
                name=sc.app_name,
                exe_path=exe,
                game_dir=start if start.is_dir() else exe.parent,
                prefix_dir=paths.compatdata_dir / str(sc.appid_u32) / "pfx",
                winedll=str(parse_presets(sc.launch_options)["winedll"] or ""),
                source="shortcut",
                ref=sc,
            )
        )
    try:
        steam_games = list_steam_library_games(paths)
    except Exception:
        logging.exception("Could not list Steam library games for bulk OptiScaler")
        steam_games = []
    for g in steam_games:
        if g.exe_path in seen_exes:
            continue
        opts = get_steam_app_launch_options(paths, g.appid)
        targets.append(
            BulkTarget(
                name=g.name,
                exe_path=g.exe_path,
                game_dir=g.install_dir,
                prefix_dir=g.prefix_dir,
                winedll=str(parse_presets(opts)["winedll"] or ""),
                source="steam",
                ref=(g.appid, opts),
            )
        )
    return targets


def _scan(paths: SteamPaths | None) -> list[Row]:
    return [(t, check_optiscaler_compatibility(t.exe_path, t.game_dir)) for t in _collect_targets(paths)]


def _source_text(t: BulkTarget) -> str:
    labels = {
        "deckinstall": tr("optiscaler_bulk.source_deckinstall"),
        "shortcut": tr("optiscaler_bulk.source_shortcut"),
        "steam": tr("optiscaler_bulk.source_steam"),
    }
    return labels.get(t.source, t.source)


def _install_files(
    targets: list[BulkTarget], remove: bool, backend: str
) -> tuple[list[BulkOutcome], list[VdfWrite]]:
    """Copy/remove OptiScaler files (Steam may stay open); save DeckInstall games; return the Steam writes."""
    outcomes: list[BulkOutcome] = []
    writes: list[VdfWrite] = []
    for t, (outcome, dll) in zip(
        targets, bulk_optiscaler_targets(targets, remove=remove, backend=backend), strict=True
    ):
        if dll is not None:
            try:
                if t.source == "deckinstall":
                    updated = update_standalone_launch_options(
                        t.game_dir, winedlloverrides=dll, launch_options=t.ref.launch_options
                    )
                    t.ref.winedlloverrides = updated.winedlloverrides
                    t.ref.launch_options = updated.launch_options
                    if t.steam_shortcut is not None:
                        writes.append(
                            (
                                "shortcut",
                                t.steam_shortcut.appid_u32,
                                t.name,
                                apply_presets(t.steam_shortcut.launch_options, winedlloverrides=dll),
                            )
                        )
                elif t.source == "shortcut":
                    writes.append(
                        ("shortcut", t.ref.appid_u32, t.name, apply_presets(t.ref.launch_options, winedlloverrides=dll))
                    )
                elif t.source == "steam":
                    appid, opts = t.ref
                    writes.append(("steam", appid, t.name, apply_presets(opts, winedlloverrides=dll)))
            except Exception as exc:
                logging.exception("Could not save DLL overrides for %s", t.name)
                outcome = BulkOutcome(t.name, "failed", str(exc))
        outcomes.append(outcome)
    return outcomes, writes


def _write_vdf(paths: SteamPaths, writes: list[VdfWrite]) -> list[str]:
    """Write every launch-options change in one go (Steam closed by the caller). Returns error lines."""
    errors: list[str] = []
    for source, appid, name, opts in writes:
        try:
            ok = (
                update_shortcut_launch_options(paths, appid, opts)
                if source == "shortcut"
                else set_steam_app_launch_options(paths, appid, opts)
            )
            if not ok:
                errors.append(tr("optiscaler_bulk.vdf_missing", name=name))
        except Exception as exc:
            logging.exception("Could not write launch options for %s", name)
            errors.append(f"{name}: {exc}")
    return errors


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
    if hasattr(win, "btn_bulk_optiscaler"):
        win.btn_bulk_optiscaler.setEnabled(False)
    win.status_label.setText(tr("optiscaler_bulk.scanning"))
    paths = win.paths
    loader = SteamOpWorker(lambda: _scan(paths), need_steam_closed=False)
    win._track_worker(loader)

    def _loaded(rows: object) -> None:
        if hasattr(win, "btn_bulk_optiscaler"):
            win.btn_bulk_optiscaler.setEnabled(not win._steam_busy)
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending:
            return
        if not isinstance(rows, list) or not rows:
            QMessageBox.information(_dialog_parent(win), __app_name__, tr("proton_games.empty"))
            return
        _show(win, rows)

    def _failed(err: str) -> None:
        if hasattr(win, "btn_bulk_optiscaler"):
            win.btn_bulk_optiscaler.setEnabled(not win._steam_busy)
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("optiscaler.error", err=err))

    loader.finished_ok.connect(_loaded)
    loader.failed.connect(_failed)
    loader.start()


def _show(win: MainWindow, rows: list[Row]) -> None:
    dlg = QDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("optiscaler_bulk.title"))
    dlg.resize(1100, 560)
    dlg_alive = _dialog_guard(dlg)
    op_state = {"busy": False}
    orig_close_event = dlg.closeEvent

    def _close_event(event) -> None:
        if op_state["busy"]:
            event.ignore()
            return
        orig_close_event(event)

    dlg.closeEvent = _close_event  # type: ignore[method-assign]
    lay = QVBoxLayout(dlg)
    hint = QLabel(tr("optiscaler_bulk.hint"))
    hint.setWordWrap(True)
    lay.addWidget(hint)

    tbl = QTableWidget(0, 4)
    tbl.setHorizontalHeaderLabels(
        [
            tr("table.name"),
            tr("optiscaler_bulk.col_source"),
            tr("optiscaler_bulk.col_compat"),
            tr("optiscaler_bulk.col_state"),
        ]
    )
    tbl.verticalHeader().setVisible(False)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    header = tbl.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(False)
    lay.addWidget(tbl, 1)

    games: list[BulkTarget] = []
    current: list[Row] = []

    def _fill(data: list[Row]) -> None:
        games.clear()
        current[:] = data
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
            tbl.setItem(row, 1, QTableWidgetItem(_source_text(game)))
            tbl.setItem(row, 2, QTableWidgetItem(_tier_text(compat)))
            tbl.setItem(row, 3, QTableWidgetItem(_state_text(compat)))

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
    btn_select_all = QPushButton(tr("optiscaler_bulk.btn_select_all"))
    btn_select_none = QPushButton(tr("optiscaler_bulk.btn_select_none"))
    btn_all = QPushButton(tr("optiscaler_bulk.btn_install_all"))
    btn_all.setToolTip(tr("optiscaler_bulk.btn_install_all_tooltip"))
    btn_remove = QPushButton(tr("optiscaler_bulk.btn_remove"))
    btn_close = QPushButton(tr("proton_bulk.btn_close"))
    btn_row.addWidget(btn_select_all)
    btn_row.addWidget(btn_select_none)
    btn_row.addWidget(btn_all)
    btn_row.addWidget(btn_install)
    btn_row.addWidget(btn_remove)
    btn_row.addStretch()
    btn_row.addWidget(btn_close)
    lay.addLayout(btn_row)
    btn_close.clicked.connect(dlg.accept)

    def _checked() -> list[BulkTarget]:
        out: list[BulkTarget] = []
        for i, game in enumerate(games):
            it = tbl.item(i, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(game)
        return out

    def _set_all_checked(state: Qt.CheckState) -> None:
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None and it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(state)

    def _install_all() -> None:
        """Check every compatible game that has no OptiScaler yet, then install on them."""
        todo = [i for i, (_g, compat) in enumerate(current) if compat.compatible and compat.installed_state is None]
        if not todo:
            QMessageBox.information(dlg, __app_name__, tr("optiscaler_bulk.none_compatible"))
            return
        answer = QMessageBox.question(
            dlg, __app_name__, tr("optiscaler_bulk.confirm_all", count=len(todo))
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Checked if i in todo else Qt.CheckState.Unchecked)
        _run(False)

    def _run(remove: bool) -> None:
        picked = _checked()
        if not picked:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        backend = str(combo_backend.currentData() or "auto")
        buttons = (btn_all, btn_install, btn_remove, btn_select_all, btn_select_none, btn_close, combo_backend)
        op_state["busy"] = True
        for b in buttons:
            b.setEnabled(False)
        paths = win.paths

        def _finish(outcomes: list[BulkOutcome], fresh: object, extra: list[str]) -> None:
            op_state["busy"] = False
            win._refresh_shortcuts()
            lines = [_outcome_line(o) for o in outcomes] + extra
            if not dlg_alive():
                if not win._closing_pending:
                    QMessageBox.information(_dialog_parent(win), __app_name__, "\n".join(lines))
                return
            for b in buttons:
                b.setEnabled(True)
            if isinstance(fresh, list):
                _fill(fresh)
            QMessageBox.information(dlg, __app_name__, "\n".join(lines))

        def _fail(err: str) -> None:
            op_state["busy"] = False
            if not dlg_alive():
                if not win._closing_pending:
                    QMessageBox.critical(_dialog_parent(win), __app_name__, tr("optiscaler.error", err=err))
                return
            for b in buttons:
                b.setEnabled(True)
            QMessageBox.critical(dlg, __app_name__, tr("optiscaler.error", err=err))

        def _write_steam(outcomes: list[BulkOutcome], writes: list[VdfWrite]) -> None:
            if paths is None:
                _finish(outcomes, None, [tr("add.steam_missing")])
                return
            if win.should_queue_vdf_edits():
                for source, appid, name, opts in writes:
                    op: VdfEditOp = "set_launch_options" if source == "shortcut" else "set_app_launch_options"
                    win.enqueue_vdf_edit(
                        QueuedVdfEdit(
                            edit_id=f"{op}:{appid}",
                            op=op,
                            appid_u32=appid,
                            label=name,
                            payload={"launch_options": opts},
                        )
                    )
                _finish(outcomes, None, [tr("optiscaler_bulk.queued", count=len(writes))])
                return

            def _go_vdf() -> None:
                if win._closing_pending:
                    _finish(outcomes, None, [tr("proton_bulk.steam_skipped")])
                    return
                win._begin_busy()
                # One Steam shutdown and one restart for every game in the batch.
                vdf_worker = SteamOpWorker(
                    lambda: (_write_vdf(paths, writes), _scan(paths)), need_steam_closed=True
                )
                win._track_worker(vdf_worker)
                vdf_worker.status.connect(win.status_label.setText)

                def _vdf_done(res: object) -> None:
                    win._end_busy()
                    errs, fresh = res if isinstance(res, tuple) else ([], None)
                    _finish(outcomes, fresh, list(errs))

                def _vdf_fail(err: str) -> None:
                    win._end_busy()
                    _fail(err)

                vdf_worker.finished_ok.connect(_vdf_done)
                vdf_worker.failed.connect(_vdf_fail)
                vdf_worker.start()

            win._confirm_close_steam_async(
                _go_vdf,
                on_cancelled=lambda: _finish(outcomes, None, [tr("proton_bulk.steam_skipped")]),
                parent=dlg if dlg_alive() else _dialog_parent(win),
            )

        def _job() -> tuple[list[BulkOutcome], list[VdfWrite], list[Row] | None]:
            # Files are copied with Steam open; the launch options then go to Steam in one write
            # (or to the saved queue in Gaming Mode, where Steam cannot be closed).
            outcomes, writes = _install_files(list(picked), remove, backend)
            return outcomes, writes, (None if writes else _scan(paths))

        worker = SteamOpWorker(_job, need_steam_closed=False)
        win._track_worker(worker)

        def _done(result: object) -> None:
            if not isinstance(result, tuple):
                _fail("?")
                return
            outcomes, writes, fresh = result
            if writes:  # files are already in place: always write the launch options, even if the dialog closed
                _write_steam(outcomes, writes)
            else:
                _finish(outcomes, fresh, [])

        worker.finished_ok.connect(_done)
        worker.failed.connect(_fail)
        worker.start()

    btn_select_all.clicked.connect(lambda: _set_all_checked(Qt.CheckState.Checked))
    btn_select_none.clicked.connect(lambda: _set_all_checked(Qt.CheckState.Unchecked))
    btn_all.clicked.connect(_install_all)
    btn_install.clicked.connect(lambda: _run(False))
    btn_remove.clicked.connect(lambda: _run(True))
    dlg.exec()
    dlg.deleteLater()
