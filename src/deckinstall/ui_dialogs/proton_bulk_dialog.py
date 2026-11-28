"""Proton in batch: unified list (DeckInstall games + Steam shortcuts) with target Proton selector.

Never downloads UMU-Proton implicitly: Steam users default to Steam's official Proton (or pick an
already-installed Proton / explicit GitHub download with confirmation). Only checked rows are modified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
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
from ..proton_policy import (
    DEFAULT_PROTON_ID,
    effective_proton_name,
    is_default,
    newest_default_proton,
    retarget_default_games,
    set_games_proton,
    use_previous_proton,
)
from ..runtime_manager import (
    check_runtime_updates,
    ensure_umu_proton,
    list_installed_compat_tools,
)
from ..standalone import list_standalone_games, match_owned_shortcuts, primary_owned_shortcuts
from ..steam_paths import SteamPaths, find_steam_root, list_installed_protons, resolve_builtin_proton
from ..steam_vdf import (
    _strip_vdf_quotes,
    bulk_migrate_compat_tool,
    get_compat_tool_map,
    load_shortcuts,
    orphan_shortcut_mappings,
    remove_orphan_shortcut_mappings,
)
from ..ui_cards import _BusyGuardedDialog, _dialog_guard, _dialog_parent, _format_worker_error, dialog_footer
from ..ui_workers import SteamOpWorker
from ..vdf_queue import QueuedVdfEdit

if TYPE_CHECKING:
    from ..ui import MainWindow

STEAM_DEFAULT_PROTON_ID = "__steam_default__"
DOWNLOAD_NEWEST_PROTON_ID = "__download_umu__"


@dataclass(frozen=True)
class ProtonRow:
    name: str
    source: str  # "deckinstall" | "shortcut"
    current: str  # Proton in use now ("" = Steam's default tool)
    newest: bool  # already on the newest local UMU-Proton
    game_dir: Path | None = None  # DeckInstall game (set even on Steam shortcuts owned by a game)
    steam_tools: tuple[tuple[int, str], ...] = ()
    has_previous: bool = False


def collect_rows(paths: SteamPaths | None) -> tuple[list[ProtonRow], list[int], str]:
    """(rows, orphan Steam mappings, newest local default Proton name) for the dialog."""
    newest = newest_default_proton()
    newest_name = newest.name if newest is not None else ""
    games = list_standalone_games()
    orphans: list[int] = []
    shortcuts = []
    compat: dict[int, str] = {}
    if paths is not None:
        try:
            shortcuts = load_shortcuts(paths.shortcuts_vdf)
            compat = get_compat_tool_map(paths.config_vdf)
        except Exception:
            logging.exception("Could not read Steam shortcuts for bulk Proton")
        try:
            orphans = orphan_shortcut_mappings(paths)
        except (OSError, ValueError):
            logging.exception("Could not verify all Steam shortcut files for orphan cleanup")
    owned = match_owned_shortcuts(shortcuts, games, paths.user_id if paths else None)
    primary = primary_owned_shortcuts(shortcuts, owned)
    chosen_appids = {entry.appid_u32 for entry in primary.values()}
    rows = [
        ProtonRow(
            name=g.name,
            source="deckinstall",
            current=effective_proton_name(g),
            newest=bool(newest_name) and (is_default(g) or g.proton_name.strip() == newest_name),
            game_dir=g.game_dir,
            has_previous=bool(str(g.meta.get("proton_previous") or "").strip()),
        )
        for g in games if not g.native_linux and g.game_dir not in primary
    ]
    for sc in shortcuts:
        game = owned.get(sc.appid_u32)
        if game is not None and game.native_linux:
            continue
        if game is None and not _strip_vdf_quotes(sc.exe).lower().endswith((".exe", ".bat")):
            continue
        if game is not None and sc.appid_u32 not in chosen_appids:
            continue
        tool = compat.get(sc.appid_u32, "")
        rows.append(
            ProtonRow(
                name=game.name if game is not None else sc.app_name,
                source="shortcut",
                current=tool,
                newest=bool(newest_name) and tool == newest_name,
                game_dir=game.game_dir if game is not None else None,
                steam_tools=((sc.appid_u32, tool),),
                has_previous=bool(game is not None and str(game.meta.get("proton_previous") or "").strip()),
            )
        )
    rows.sort(key=lambda r: r.name.lower())
    return rows, orphans, newest_name


def collect_target_options(
    paths: SteamPaths | None, newest_name: str
) -> tuple[list[tuple[str, str]], dict[str, str], bool]:
    """Return (combo_items, custom_folder_by_id, has_official_steam) with zero network access."""
    steam_root = paths.steam_root if paths is not None else find_steam_root()
    installed = list_installed_protons(steam_root) if steam_root is not None else []
    official = [(tid, label) for tid, label in installed if steam_root and resolve_builtin_proton(tid, steam_root)]
    custom = list_installed_compat_tools()
    has_official = bool(official)

    options: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    custom_map: dict[str, str] = {}

    if paths is not None or has_official:
        options.append((STEAM_DEFAULT_PROTON_ID, tr("proton.steam_default_label")))
        seen_ids.add(STEAM_DEFAULT_PROTON_ID)
        for tid, label in official:
            if tid and tid not in seen_ids:
                options.append((tid, label))
                seen_ids.add(tid)
    if newest_name and DEFAULT_PROTON_ID not in seen_ids:
        options.append((DEFAULT_PROTON_ID, f"{tr('proton.default_label')} ({newest_name})"))
        seen_ids.add(DEFAULT_PROTON_ID)

    for tool in custom:
        custom_map[tool.tool_id] = tool.folder_name
        custom_map[tool.folder_name] = tool.folder_name
        if tool.tool_id not in seen_ids:
            options.append((tool.tool_id, tool.folder_name))
            seen_ids.add(tool.tool_id)

    options.append((DOWNLOAD_NEWEST_PROTON_ID, tr("proton_bulk.target_download_umu")))
    return options, custom_map, has_official


def ensure_newest_default_proton(
    progress: Callable[[str], None] | None = None,
    *,
    retarget_defaults: bool = True,
) -> str:
    """Download a newer UMU-Proton when GitHub has one; optionally retarget all Default games."""
    before = newest_default_proton()
    info = check_runtime_updates(force=True).get("umu_proton", {})
    if info.get("update_available") or not info.get("installed"):
        ensure_umu_proton(
            force_update=bool(info.get("update_available")),
            progress_cb=(lambda msg, _pct: progress(msg)) if progress is not None else None,
        )
    after = newest_default_proton()
    if after is None:
        raise RuntimeError(tr("proton.none_found"))
    if retarget_defaults and before is not None and before.name != after.name:
        retarget_default_games(before.name)
    return after.name


class _LoadWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        try:
            rows, orphans, newest = collect_rows(self._paths)
            target_info = collect_target_options(self._paths, newest)
            self.ready.emit((rows, orphans, newest, target_info))
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class _ApplyWorker(QThread):
    """Apply the selected Proton target to checked games (only downloads if explicitly requested)."""

    status = Signal(str)
    done = Signal(str, int, list)
    failed = Signal(str)

    def __init__(
        self,
        game_dirs: list[Path],
        target_id: str,
        standalone_tool: str,
        download_proton: bool,
    ) -> None:
        super().__init__()
        self._dirs = game_dirs
        self._target_id = target_id
        self._standalone_tool = standalone_tool
        self._download_proton = download_proton

    def run(self) -> None:
        try:
            if self._download_proton:
                resolved = ensure_newest_default_proton(self.status.emit, retarget_defaults=False)
                standalone_arg = ""
            else:
                resolved = self._target_id
                standalone_arg = self._standalone_tool
            changed, errors = set_games_proton(self._dirs, standalone_arg) if self._dirs else (0, [])
            self.done.emit(resolved, changed, errors)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


def bulk_proton_dialog(
    win: MainWindow, *, preferred_tool: str = "", on_close: Callable[[], None] | None = None,
) -> None:
    """Open the batch Proton dialog (load off the UI thread)."""
    win.status_label.setText(tr("proton_bulk.loading"))
    worker = _LoadWorker(win.paths)
    win._track_worker(worker)

    def _loaded(res: object) -> None:
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending or not isinstance(res, tuple) or len(res) != 4:
            return
        rows, orphans, newest, target_info = res
        if not rows and not orphans:
            QMessageBox.information(_dialog_parent(win), __app_name__, tr("proton_bulk.empty"))
        else:
            _show(win, rows, orphans, newest, target_info, preferred_tool=preferred_tool)
        if on_close is not None:
            on_close()

    def _failed(err: str) -> None:
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("proton.error", err=err))
            if on_close is not None:
                on_close()

    worker.ready.connect(_loaded)
    worker.failed.connect(_failed)
    worker.start()


def _source_text(row: ProtonRow) -> str:
    return tr("proton_bulk.source_deckinstall") if row.source == "deckinstall" else tr("proton_bulk.source_shortcut")


def _row_matches_target(
    row: ProtonRow, target_id: str, newest: str, custom_map: dict[str, str]
) -> bool:
    if target_id == STEAM_DEFAULT_PROTON_ID:
        if not row.steam_tools:
            return True
        return row.current == ""
    if target_id == DOWNLOAD_NEWEST_PROTON_ID:
        return row.newest
    if target_id == DEFAULT_PROTON_ID:
        return row.newest or (row.game_dir is not None and not row.steam_tools and row.current == newest)
    folder = custom_map.get(target_id, target_id)
    return bool(row.current and row.current in (target_id, folder))


def _show(
    win: MainWindow,
    rows: list[ProtonRow],
    orphans: list[int],
    newest: str,
    target_info: tuple[list[tuple[str, str]], dict[str, str], bool],
    *,
    preferred_tool: str = "",
) -> None:
    busy_state = {"active": False}
    dlg = _BusyGuardedDialog(_dialog_parent(win), lambda: busy_state["active"])
    dlg.setWindowTitle(tr("proton_bulk.title"))
    from ..ui_cards import clamp_dialog_size

    clamp_dialog_size(dlg, 900, 580, dlg.parentWidget())
    dlg_alive = _dialog_guard(dlg)
    lay = QVBoxLayout(dlg)
    hint = QLabel(tr("proton_bulk.hint"))
    hint.setWordWrap(True)
    lay.addWidget(hint)

    target_row = QHBoxLayout()
    lbl_target = QLabel(tr("proton_bulk.target_label"))
    cmb_target = QComboBox()
    target_row.addWidget(lbl_target)
    target_row.addWidget(cmb_target, 1)
    lay.addLayout(target_row)

    tbl = QTableWidget(0, 4)
    tbl.setHorizontalHeaderLabels(
        [tr("table.name"), tr("proton_bulk.col_source"), tr("proton_bulk.col_current"), tr("proton_bulk.col_state")]
    )
    tbl.verticalHeader().setVisible(False)
    tbl.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    header = tbl.horizontalHeader()
    header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for col in (1, 2, 3):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    lay.addWidget(tbl, 1)

    lbl_status = QLabel()
    lbl_status.setObjectName("MutedText")
    lbl_status.setWordWrap(True)
    lay.addWidget(lbl_status)

    current: list[ProtonRow] = []
    state_ctx: dict[str, object] = {
        "newest": newest,
        "custom_map": target_info[1],
        "has_official_steam": target_info[2],
    }

    def _populate_targets(
        options: list[tuple[str, str]],
        custom_map: dict[str, str],
        has_official: bool,
        *,
        preserve_id: str | None = None,
    ) -> None:
        state_ctx["custom_map"] = custom_map
        state_ctx["has_official_steam"] = has_official
        cmb_target.blockSignals(True)
        cmb_target.clear()
        for tid, label in options:
            cmb_target.addItem(label, tid)
        selected_idx = 0
        if preserve_id:
            idx = cmb_target.findData(preserve_id)
            if idx >= 0:
                selected_idx = idx
        cmb_target.setCurrentIndex(selected_idx)
        cmb_target.blockSignals(False)

    def _sync_target_states() -> None:
        target_id = str(cmb_target.currentData() or STEAM_DEFAULT_PROTON_ID)
        newest_local = str(state_ctx["newest"] or "")
        custom_map = state_ctx["custom_map"]
        assert isinstance(custom_map, dict)
        tbl.blockSignals(True)
        was_sorting = tbl.isSortingEnabled()
        tbl.setSortingEnabled(False)
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is None:
                continue
            r = it.data(Qt.ItemDataRole.UserRole)
            matches = _row_matches_target(r, target_id, newest_local, custom_map)
            it.setCheckState(Qt.CheckState.Unchecked if matches else Qt.CheckState.Checked)
            tbl.setItem(
                i,
                3,
                QTableWidgetItem(
                    tr("proton_bulk.state_on_target") if matches else tr("proton_bulk.state_will_change")
                ),
            )
        tbl.setSortingEnabled(was_sorting)
        tbl.blockSignals(False)

    def _fill(data: list[ProtonRow]) -> None:
        current[:] = data
        tbl.setSortingEnabled(False)
        tbl.setRowCount(0)
        for r in data:
            row = tbl.rowCount()
            tbl.insertRow(row)
            it = QTableWidgetItem(r.name)
            it.setData(Qt.ItemDataRole.UserRole, r)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, QTableWidgetItem(_source_text(r)))
            if r.game_dir is None or r.steam_tools:
                proton = r.current or tr("proton.steam_default_label")
            else:
                proton = r.current or tr("proton.default_label")
            tbl.setItem(row, 2, QTableWidgetItem(proton))
            tbl.setItem(row, 3, QTableWidgetItem(""))
        _sync_target_states()
        tbl.setSortingEnabled(True)

    preferred_id = next(
        (tool_id for tool_id, _ in target_info[0] if target_info[1].get(tool_id) == preferred_tool),
        None,
    ) if preferred_tool else None
    _populate_targets(*target_info, preserve_id=preferred_id)
    _fill(rows)
    cmb_target.currentIndexChanged.connect(lambda _idx: _sync_target_states())

    btn_select_all = QPushButton(tr("optiscaler_bulk.btn_select_all"))
    btn_select_none = QPushButton(tr("optiscaler_bulk.btn_select_none"))
    btn_update = QPushButton(tr("proton_bulk.btn_update"))
    btn_update.setObjectName("primaryBtn")
    btn_update_all = QPushButton(tr("proton_bulk.btn_update_all"))
    btn_previous = QPushButton(tr("configure.btn_previous_proton"))
    btn_previous.setToolTip(tr("proton_bulk.btn_previous_tip"))
    btn_orphans = QPushButton(tr("proton_bulk.btn_clean_orphans", count=len(orphans)))
    btn_orphans.setVisible(bool(orphans))
    btn_versions = QPushButton(tr("library.btn_manage_protons"))
    btn_close = QPushButton(tr("proton_bulk.btn_close"))
    lay.addLayout(dialog_footer(btn_update_all, btn_update, left=(btn_select_all, btn_select_none)))
    lay.addLayout(dialog_footer(btn_close, left=(btn_previous, btn_orphans, btn_versions)))
    actions = (
        cmb_target,
        btn_update,
        btn_update_all,
        btn_previous,
        btn_orphans,
        btn_versions,
        btn_select_all,
        btn_select_none,
        btn_close,
    )
    btn_close.clicked.connect(dlg.accept)

    # Expose key widgets for tests and accessibility inspection.
    dlg.cmb_target = cmb_target  # type: ignore[attr-defined]
    dlg.tbl = tbl  # type: ignore[attr-defined]
    dlg.btn_update = btn_update  # type: ignore[attr-defined]
    dlg.btn_update_all = btn_update_all  # type: ignore[attr-defined]
    dlg.lbl_status = lbl_status  # type: ignore[attr-defined]

    def _checked() -> list[ProtonRow]:
        return [
            it.data(Qt.ItemDataRole.UserRole) for i in range(tbl.rowCount())
            if (it := tbl.item(i, 0)) is not None and it.checkState() == Qt.CheckState.Checked
        ]

    def _set_all(state: Qt.CheckState) -> None:
        for i in range(tbl.rowCount()):
            it = tbl.item(i, 0)
            if it is not None:
                it.setCheckState(state)

    def _busy(on: bool) -> None:
        busy_state["active"] = on
        if not dlg_alive():
            return
        for b in actions:
            b.setEnabled(not on)

    def _set_status(text: str) -> None:
        win.status_label.setText(text)
        if dlg_alive():
            lbl_status.setText(text)

    def _finish_reload(extra: list[str]) -> None:
        _busy(False)
        _set_status(tr("status.ready"))
        if extra:
            parent = dlg if dlg_alive() else _dialog_parent(win)
            if dlg_alive() or not win._closing_pending:
                QMessageBox.information(parent, __app_name__, "\n".join(extra))

    def _reload(extra: list[str]) -> None:
        win._refresh_shortcuts()
        if not dlg_alive() or win._closing_pending:
            _finish_reload(extra)
            return
        _busy(True)
        _set_status(tr("proton_bulk.loading"))
        prev_target = str(cmb_target.currentData() or "")
        loader = _LoadWorker(win.paths)
        win._track_worker(loader)

        def _ready(res: object) -> None:
            if dlg_alive() and isinstance(res, tuple) and len(res) == 4:
                fresh_rows, fresh_orphans, fresh_newest, fresh_targets = res
                state_ctx["newest"] = fresh_newest
                _populate_targets(
                    fresh_targets[0], fresh_targets[1], fresh_targets[2], preserve_id=prev_target
                )
                _fill(fresh_rows)
                btn_orphans.setText(tr("proton_bulk.btn_clean_orphans", count=len(fresh_orphans)))
                btn_orphans.setVisible(bool(fresh_orphans))
            _finish_reload(extra)

        def _fail(err: str) -> None:
            _finish_reload([*extra, tr("proton.error", err=err)])

        loader.ready.connect(_ready)
        loader.failed.connect(_fail)
        loader.start()

    def _steam_write(
        job_name: str,
        appids: list[int],
        tool: str,
        lines: list[str],
        *,
        skip_confirm: bool = False,
    ) -> None:
        """Steam shortcuts: one write with Steam closed, or the queue in Gaming Mode."""
        paths = win.paths
        if not appids or paths is None:
            _reload(lines)
            return
        display_tool = tool or tr("proton.steam_default_label")
        if win.should_queue_vdf_edits():
            if job_name == "remove":
                edit = QueuedVdfEdit(f"remove_compat:{','.join(map(str, appids))}", "remove_compat", 0,
                                     tr("proton_bulk.queue_label_orphans"), {"appids": appids})
            else:
                edit = QueuedVdfEdit(f"bulk_migrate_compat:{tool}:{','.join(map(str, appids))}",
                                     "bulk_migrate_compat", 0, tr("proton_bulk.queue_label", tool=display_tool),
                                     {"appids": appids, "target_tool": tool})
            if win.enqueue_vdf_edit(edit):
                _reload([*lines, tr("proton_bulk.queued", count=len(appids))])
            else:
                _reload(lines)
            return

        def _write() -> int:
            if job_name == "remove":
                return remove_orphan_shortcut_mappings(paths, appids)
            for appid in appids:
                win._auto_snapshot_if_enabled(
                    paths.compatdata_dir / str(appid) / "pfx", str(appid), "proton-bulk-migrate", "proton",
                    enabled=auto_snap,
                )
            return bulk_migrate_compat_tool(paths.config_vdf, appids, tool)

        auto_snap = win._is_auto_snapshot_enabled()

        def _go() -> None:
            if not dlg_alive() or win._closing_pending:
                _busy(False)
                return
            win._begin_busy()
            worker = SteamOpWorker(_write, need_steam_closed=True)
            win._track_worker(worker)
            worker.status.connect(_set_status)

            def _ok(count: object) -> None:
                win._end_busy()
                if job_name == "remove":
                    _reload([*lines, tr("proton_bulk.orphans_removed", count=count)])
                else:
                    _reload([*lines, tr("proton_bulk.migrated", count=count, tool=display_tool)])

            def _err(err: str) -> None:
                win._end_busy()
                _reload([*lines, tr("proton.error", err=err)])

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if skip_confirm:
            _go()
        else:
            win._confirm_close_steam_async(
                _go,
                on_cancelled=lambda: _reload([*lines, tr("proton_bulk.steam_skipped")]),
                parent=dlg,
                alive_fn=dlg_alive,
            )

    def _update(selection: list[ProtonRow]) -> None:
        if not selection:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        target_id = str(cmb_target.currentData() or STEAM_DEFAULT_PROTON_ID)
        custom_map = state_ctx["custom_map"]
        assert isinstance(custom_map, dict)
        dirs = [
            r.game_dir
            for r in selection
            if r.game_dir is not None and not (target_id == STEAM_DEFAULT_PROTON_ID and not r.steam_tools)
        ]
        appids = list(dict.fromkeys(appid for r in selection for appid, _ in r.steam_tools))
        if not dirs and not appids:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
            return
        download_proton = target_id == DOWNLOAD_NEWEST_PROTON_ID
        has_official = bool(
            state_ctx["has_official_steam"] or win.paths is not None or win._steam_root_found
        )
        if download_proton and has_official:
            ans = QMessageBox.question(
                dlg,
                __app_name__,
                tr("proton_bulk.confirm_download_steamos"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        standalone_tool = (
            ""
            if target_id in (STEAM_DEFAULT_PROTON_ID, DEFAULT_PROTON_ID, DOWNLOAD_NEWEST_PROTON_ID)
            else custom_map.get(target_id, target_id)
        )

        def _launch_apply(confirmed_steam: bool) -> None:
            if not dlg_alive() or win._closing_pending:
                return
            _busy(True)
            worker = _ApplyWorker(dirs, target_id, standalone_tool, download_proton)
            win._track_worker(worker)
            worker.status.connect(_set_status)

            def _done(resolved: str, changed: int, errors: list[str]) -> None:
                _set_status(tr("status.ready"))
                if not dlg_alive():
                    _busy(False)
                    return
                label_name = resolved if resolved != STEAM_DEFAULT_PROTON_ID else tr("proton.steam_default_label")
                steam_tool = "" if target_id == STEAM_DEFAULT_PROTON_ID else resolved
                lines = [tr("proton_bulk.games_updated", count=changed, name=label_name)] if dirs else []
                _steam_write("migrate", appids, steam_tool, lines + errors, skip_confirm=confirmed_steam)

            def _fail(err: str) -> None:
                _set_status(tr("status.ready"))
                _reload([tr("proton.error", err=err)])

            worker.done.connect(_done)
            worker.failed.connect(_fail)
            worker.start()

        if appids and win.paths is not None and not win.should_queue_vdf_edits():
            win._confirm_close_steam_async(
                lambda: _launch_apply(True),
                parent=dlg,
                alive_fn=dlg_alive,
            )
        else:
            _launch_apply(False)

    def _previous() -> None:
        selection = {row.game_dir: row for row in _checked() if row.game_dir is not None and row.has_previous}
        if not selection:
            QMessageBox.information(dlg, __app_name__, tr("proton_bulk.no_previous"))
            return
        paths = win.paths
        needs_steam_write = paths is not None and any(row.steam_tools for row in selection.values())
        queued = win.should_queue_vdf_edits()
        _busy(True)

        def _go() -> None:
            win._begin_busy()

            def _job() -> tuple[int, list[str], dict[str, list[int]]]:
                errors: list[str] = []
                writes: dict[str, list[int]] = {}
                changed = 0
                for directory, row in selection.items():
                    try:
                        game = use_previous_proton(directory)
                        changed += 1
                        for appid, _ in row.steam_tools:
                            writes.setdefault(game.proton_name, []).append(appid)
                    except (OSError, ValueError) as exc:
                        errors.append(f"{row.name}: {exc}")
                pending = {}
                for tool, ids in writes.items():
                    if queued:
                        pending[tool] = ids
                    elif paths is not None:
                        try:
                            bulk_migrate_compat_tool(paths.config_vdf, ids, tool)
                        except Exception as exc:
                            errors.append(str(exc))
                            pending[tool] = ids
                return changed, errors, pending

            worker = SteamOpWorker(_job, need_steam_closed=needs_steam_write and not queued)
            win._track_worker(worker)

            def _done(result: object) -> None:
                win._end_busy()
                assert isinstance(result, tuple)
                changed, errors, pending = result
                for tool, ids in pending.items():
                    win.enqueue_vdf_edit(QueuedVdfEdit(
                        f"bulk_migrate_compat:{tool}:{','.join(map(str, ids))}", "bulk_migrate_compat", 0,
                        tr("proton_bulk.queue_label", tool=tool), {"appids": ids, "target_tool": tool},
                    ), trigger_watch=queued)
                _reload([tr("proton_games.changed", count=changed), *errors])

            def _failed(err: str) -> None:
                win._end_busy()
                _reload([tr("proton.error", err=err)])

            worker.finished_ok.connect(_done)
            worker.failed.connect(_failed)
            worker.start()

        if needs_steam_write and not queued:
            win._confirm_close_steam_async(_go, on_cancelled=lambda: _busy(False), parent=dlg, alive_fn=dlg_alive)
        else:
            _go()

    def _clean_orphans() -> None:
        paths = win.paths
        if paths is None:
            return
        _busy(True)
        worker = SteamOpWorker(lambda: orphan_shortcut_mappings(paths))
        win._track_worker(worker)

        def _ready(fresh: object) -> None:
            if not dlg_alive() or win._closing_pending:
                return
            if isinstance(fresh, list) and fresh and (
                QMessageBox.question(dlg, __app_name__, tr("proton_bulk.confirm_orphans", count=len(fresh)))
                == QMessageBox.StandardButton.Yes
            ):
                _steam_write("remove", fresh, "", [])
            else:
                _busy(False)

        worker.finished_ok.connect(_ready)
        worker.failed.connect(lambda err: _finish_reload([tr("proton.error", err=err)]))
        worker.start()

    def _versions() -> None:
        from .proton_dialogs import open_proton_manager_dialog

        open_proton_manager_dialog(win)
        _reload([])

    btn_select_all.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
    btn_select_none.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))
    btn_update.clicked.connect(lambda: _update(_checked()))
    btn_update_all.clicked.connect(lambda: _update(list(current)))
    btn_previous.clicked.connect(_previous)
    btn_orphans.clicked.connect(_clean_orphans)
    btn_versions.clicked.connect(_versions)
    dlg.exec()
    dlg.deleteLater()
