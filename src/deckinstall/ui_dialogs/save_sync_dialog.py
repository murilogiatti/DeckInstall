"""Backup/Sync page (per-game snapshots, Ludusavi import/export) and the folder Save Sync dialog."""

from __future__ import annotations

import logging
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, save_sources
from .. import ludusavi as lds
from ..i18n import tr
from ..save_sources import is_nexus_running, nexus_save_sources, saves_size, steam_save_sources
from ..save_sync import (
    DEFAULT_SYNC_DIR,
    SNAPSHOT_KEEP_DEFAULT,
    ExtraDirs,
    SaveArchiveEntry,
    SteamCloudAppMismatch,
    SyncDryRunPreview,
    SyncGameTarget,
    SyncPlanItem,
    _resolve_wine_root,
    _slugify,
    archive_has_steamcloud,
    auto_snapshot,
    list_save_archives,
    newest_save_mtime,
    plan_sync_all,
    preview_sync_operation,
    pull_game_saves,
    push_game_saves,
    remove_restored_snapshot,
    restore_snapshot_safely,
    restored_snapshot_matches,
    snapshot_after_play,
    snapshot_dir_for,
    snapshot_display_name,
    snapshot_home_files,
)
from ..standalone import StandaloneGame, game_log_dir
from ..steam_paths import SteamPaths, invalidate_process_cache, is_steam_running
from ..steam_vdf import DEFAULT_SAVES_DIR
from ..ui_cards import (
    _dialog_parent,
    _format_worker_error,
    _is_wine_game_running,
    _pick_dir_async,
    _setting_bool,
    _SortableTableItem,
    action_row,
    dialog_footer,
    format_size_bytes,
    page_layout,
    section_card,
    show_text_dialog,
)
from ..ui_workers import SteamOpWorker

if TYPE_CHECKING:
    from ..ui import MainWindow

AUTO_SNAPSHOT_KEY = "safety/auto_snapshot"
KEEP_KEY = "saves/keep_snapshots"
AFTER_PLAY_KEY = "saves/snapshot_after_play"
LUDUSAVI_DIR_KEY = "saves/ludusavi_folder"


class SaveTarget(NamedTuple):
    """A game whose Wine prefix (and maybe extra folders) holds saves."""

    name: str
    prefix_dir: Path
    game_dir: Path | None
    steam_appid: int | None
    run_log: Path | None  # launch.sh run log (library games only)
    source: str = "deckinstall"  # deckinstall, shortcut, steam, heroic, lutris, bottles, hydra, umu, nexus
    extra_dirs: ExtraDirs = ()  # e.g. Steam's local cloud folder


def source_label(source: str) -> str:
    """Name shown in the "Origin" column."""
    labels = {
        "deckinstall": "DeckInstall",
        "shortcut": tr("saves.source_shortcut"),
        "steam": "Steam",
        "heroic": "Heroic",
        "lutris": "Lutris",
        "bottles": "Bottles",
        "hydra": "Hydra",
        "umu": "UMU",
        "nexus": "Nexus",
    }
    return labels.get(source, source)


def scan_external_save_targets(paths: SteamPaths | None, home: Path | None = None) -> list[SaveTarget]:
    """Steam store games, Hydra/UMU prefixes and Nexus games (read only; run off the UI thread)."""
    from ..adopt import HYDRA, UMU, find_adoption_candidates

    home = home or save_sources.default_home()
    out: list[SaveTarget] = []
    if paths is not None:
        try:
            for es in steam_save_sources(paths):
                out.append(
                    SaveTarget(es.name, es.prefix_dir, es.game_dir, es.steam_appid, None, "steam", es.extra_dirs)
                )
        except Exception as exc:  # one broken source must not hide the others
            logging.getLogger(__name__).warning("Steam save scan failed: %s", exc)
    try:
        for c in find_adoption_candidates(home=home, kinds=(HYDRA, UMU)):
            if c.prefix_dir is not None and not c.native_linux and c.prefix_dir.is_dir():
                out.append(SaveTarget(c.name, c.prefix_dir, c.game_dir, c.steam_appid, None, c.kind))
    except Exception as exc:
        logging.getLogger(__name__).warning("Hydra/UMU save scan failed: %s", exc)
    try:
        for es in nexus_save_sources(home):
            out.append(SaveTarget(es.name, es.prefix_dir, es.game_dir, None, None, "nexus"))
    except Exception as exc:
        logging.getLogger(__name__).warning("Nexus save scan failed: %s", exc)
    return out


class SaveRow(NamedTuple):
    """One line of the Backup/Sync table (target None: snapshots of a game no longer installed)."""

    slug: str
    name: str
    target: SaveTarget | None
    snapshots: int
    last_mtime: float
    size_bytes: int


def snapshot_keep(settings: QSettings) -> int:
    """Snapshots kept per game (setting saves/keep_snapshots)."""
    try:
        value = int(str(settings.value(KEEP_KEY, SNAPSHOT_KEEP_DEFAULT)))
    except (TypeError, ValueError):
        return SNAPSHOT_KEEP_DEFAULT
    return max(1, min(50, value))


def _target_size_key(target: SaveTarget) -> str:
    """Unique lookup key per game target so same-name games keep independent sizes."""
    return f"{_slugify(target.name)}|{target.prefix_dir}"


def _resolved_prefix_key(prefix_dir: Path) -> Path | None:
    if not prefix_dir.exists():
        return None
    try:
        return _resolve_wine_root(prefix_dir).resolve()
    except OSError:
        return None


def collect_save_targets(win: MainWindow) -> list[SaveTarget]:
    """Library, Steam shortcut and other-launcher games with a Wine prefix (first matching target wins)."""
    targets: list[SaveTarget] = []
    for sg in win._standalone_games:
        if not sg.native_linux:
            targets.append(
                SaveTarget(sg.name, sg.prefix_dir, sg.game_dir, sg.steam_appid, game_log_dir(sg) / "run.log")
            )
    if win.paths is not None:
        for entry in win._steam_shortcuts:
            compat = win.paths.compatdata_dir / str(entry.appid_u32)
            pfx = compat / "pfx" if (compat / "pfx").is_dir() else compat
            raw_dir = entry.start_dir.strip().strip('"')
            targets.append(
                SaveTarget(entry.app_name, pfx, Path(raw_dir) if raw_dir else None, None, None, "shortcut")
            )
    for ext in win._external_games:
        if ext.prefix_dir is not None and ext.runner != "linux":
            targets.append(SaveTarget(ext.name, ext.prefix_dir, ext.game_dir, None, None, ext.source))
    targets.extend(win._external_save_targets)
    seen_by_slug: dict[str, list[tuple[Path | None, int | None]]] = {}
    unique: list[SaveTarget] = []
    for t in targets:
        slug = _slugify(t.name)
        pfx_key = _resolved_prefix_key(t.prefix_dir)
        prior = seen_by_slug.get(slug, [])
        is_dup = any(
            (pfx_key is None or prev_pfx is None)
            or pfx_key == prev_pfx
            or (t.steam_appid is not None and t.steam_appid == prev_appid)
            for prev_pfx, prev_appid in prior
        )
        if is_dup:
            continue
        seen_by_slug.setdefault(slug, []).append((pfx_key, t.steam_appid))
        unique.append(t)
    return unique


def build_save_rows(targets: list[SaveTarget], archives: list[SaveArchiveEntry]) -> list[SaveRow]:
    """Group snapshot archives per game; games without snapshots and orphan snapshots included."""
    stats: dict[str, list[float]] = {}  # slug -> [count, last mtime, size]
    for a in archives:
        if a.kind not in ("save_snapshot", "state_snapshot") or not a.game_slug:
            continue
        st = stats.setdefault(a.game_slug, [0, 0.0, 0])
        st[2] += a.size_bytes
        if a.kind == "save_snapshot":
            st[0] += 1
            st[1] = max(st[1], a.mtime)
    rows: list[SaveRow] = []
    for t in targets:
        slug = _slugify(t.name)
        c, m, z = stats.pop(slug, [0, 0.0, 0])
        rows.append(SaveRow(slug, t.name, t, int(c), m, int(z)))
    for slug, (c, m, z) in sorted(stats.items()):
        rows.append(SaveRow(slug, snapshot_display_name(slug), None, int(c), m, int(z)))
    return rows


def _snapshots_of(slug: str) -> list[SaveArchiveEntry]:
    return [
        a for a in list_save_archives()
        if a.game_slug == slug and a.kind in ("save_snapshot", "state_snapshot")
    ]


def build_saves_page(win: MainWindow) -> QWidget:
    """The Saves page, by purpose: per-game copies first, then whole backups and sync, then
    Ludusavi, then the automatic-copy settings."""
    page, layout = page_layout(tr("saves.page_title"), tr("saves.page_subtitle"))

    # 1. Copies of each game: the everyday part.
    card, card_layout, top = section_card(tr("saves.section_games"), tr("saves.section_games_hint"))
    win.chk_saves_orphans = QCheckBox(tr("saves.show_orphans", count=0))
    win.chk_saves_orphans.setChecked(_setting_bool(win.settings, SHOW_ORPHANS_KEY, False))

    def _toggle_orphans(on: bool) -> None:
        win.settings.setValue(SHOW_ORPHANS_KEY, bool(on))
        _apply_orphan_filter(win)

    win.chk_saves_orphans.toggled.connect(_toggle_orphans)
    top.addWidget(win.chk_saves_orphans)
    win.btn_save_refresh = QPushButton(tr("save_sync.btn_refresh"))
    win.btn_save_refresh.clicked.connect(lambda: refresh_saves_page(win))
    top.addWidget(win.btn_save_refresh)

    table = QTableWidget(0, 6)
    table.setHorizontalHeaderLabels(
        [
            tr("add.detected_name_column"),
            tr("saves.column_source"),
            tr("saves.column_now"),
            tr("saves.column_last"),
            tr("saves.column_count"),
            tr("maint.column_size"),
        ]
    )
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for col in (1, 2, 3, 4, 5):
        table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    now_header = table.horizontalHeaderItem(2)
    if now_header is not None:
        now_header.setToolTip(tr("saves.column_now_tip"))
    table.setSortingEnabled(True)
    table.setMinimumHeight(330)
    table.currentCellChanged.connect(lambda *_a: update_saves_action_states(win))
    win.saves_list = table
    card_layout.addWidget(table, 1)

    win.lbl_saves_empty = QLabel(tr("saves.empty_list"))
    win.lbl_saves_empty.setObjectName("MutedText")
    win.lbl_saves_empty.setWordWrap(True)
    card_layout.addWidget(win.lbl_saves_empty)

    row_actions = QHBoxLayout()
    win.btn_save_snapshot_now = QPushButton(tr("saves.btn_snapshot_now"))
    win.btn_save_snapshot_now.setObjectName("primaryBtn")
    win.btn_save_snapshot_now.clicked.connect(lambda: _snapshot_now(win))
    win.btn_save_restore_selected = QPushButton(tr("saves.btn_restore_selected"))
    win.btn_save_restore_selected.clicked.connect(lambda: _restore_selected(win))
    win.btn_save_open_folder = QPushButton(tr("saves.btn_open_folder"))
    win.btn_save_open_folder.clicked.connect(lambda: _open_folder(win))
    for b in (win.btn_save_snapshot_now, win.btn_save_restore_selected, win.btn_save_open_folder):
        row_actions.addWidget(b)
    row_actions.addStretch()
    card_layout.addLayout(row_actions)
    layout.addWidget(card, 1)

    # 2. Every game at once: a .zip to keep elsewhere, or a folder kept in step (Syncthing, SD card).
    all_card, all_layout, _top = section_card(tr("saves.section_all"))
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    grid.setColumnStretch(1, 1)
    win.btn_save_backup = QPushButton(tr("library.btn_backup_saves"))
    win.btn_save_backup.clicked.connect(win._backup_all_saves)
    win.btn_save_restore = QPushButton(tr("library.btn_restore_saves"))
    win.btn_save_restore.clicked.connect(win._restore_saves_backup)
    win.btn_save_sync = QPushButton(tr("saves.btn_sync"))
    win.btn_save_sync.clicked.connect(win._open_save_sync_dialog)
    action_row(grid, 0, win.btn_save_backup, tr("saves.backup_hint"))
    action_row(grid, 1, win.btn_save_restore, tr("saves.restore_hint"))
    action_row(grid, 2, win.btn_save_sync, tr("saves.sync_hint"))
    all_layout.addLayout(grid)
    layout.addWidget(all_card)

    # 3. Ludusavi: only for people who already use it.
    lds_card, lds_layout, _top = section_card(tr("saves.section_ludusavi"), tr("saves.section_ludusavi_hint"))
    lds_grid = QGridLayout()
    lds_grid.setHorizontalSpacing(12)
    lds_grid.setColumnStretch(1, 1)
    win.btn_save_import = QPushButton(tr("saves.btn_import"))
    win.btn_save_import.clicked.connect(lambda: _ludusavi_pick(win, export=False))
    win.btn_save_import_one = QPushButton(tr("saves.btn_import_one"))
    win.btn_save_import_one.clicked.connect(lambda: _ludusavi_pick(win, export=False, one=True))
    win.btn_save_export = QPushButton(tr("saves.btn_export"))
    win.btn_save_export.clicked.connect(lambda: _ludusavi_pick(win, export=True))
    win.btn_save_dedup = QPushButton(tr("saves.btn_dedup"))
    win.btn_save_dedup.clicked.connect(lambda: _remove_import_duplicates(win))
    action_row(lds_grid, 0, win.btn_save_import, tr("saves.import_hint"))
    action_row(lds_grid, 1, win.btn_save_import_one, tr("saves.import_one_hint"))
    action_row(lds_grid, 2, win.btn_save_export, tr("saves.export_hint"))
    action_row(lds_grid, 3, win.btn_save_dedup, tr("saves.btn_dedup_tip"))
    lds_layout.addLayout(lds_grid)
    layout.addWidget(lds_card)

    # 4. What happens on its own.
    auto_card, auto_layout, _top = section_card(tr("saves.section_auto"))
    chk_auto = QCheckBox(tr("save_sync.auto_snapshot"))
    chk_auto.setChecked(_setting_bool(win.settings, AUTO_SNAPSHOT_KEY, True))
    chk_auto.toggled.connect(lambda on: win.settings.setValue(AUTO_SNAPSHOT_KEY, bool(on)))
    chk_play = QCheckBox(tr("saves.after_play"))
    chk_play.setChecked(_setting_bool(win.settings, AFTER_PLAY_KEY, True))
    chk_play.toggled.connect(lambda on: win.settings.setValue(AFTER_PLAY_KEY, bool(on)))
    auto_layout.addWidget(chk_auto)
    auto_layout.addWidget(chk_play)
    keep_row = QHBoxLayout()
    keep_row.addWidget(QLabel(tr("saves.keep_label")))
    spin_keep = QSpinBox()
    spin_keep.setRange(1, 50)
    spin_keep.setValue(snapshot_keep(win.settings))
    spin_keep.valueChanged.connect(lambda v: win.settings.setValue(KEEP_KEY, int(v)))
    keep_row.addWidget(spin_keep)
    keep_row.addStretch()
    auto_layout.addLayout(keep_row)
    win.spin_save_keep = spin_keep
    folder_label = QLabel(tr("saves.default_folder", path=DEFAULT_SAVES_DIR))
    folder_label.setObjectName("MutedText")
    folder_label.setWordWrap(True)
    auto_layout.addWidget(folder_label)
    layout.addWidget(auto_card)

    # Steam/Nexus sources and save sizes are looked up when the tab is opened, not at startup.
    refresh_saves_page(win, scan_sources=False)
    return page


SHOW_ORPHANS_KEY = "saves/show_orphans"


def _apply_orphan_filter(win: MainWindow) -> None:
    """Copies of games that are not installed (old installs, Ludusavi imports) stay out of the
    way unless asked for; the check box says how many there are."""
    table = win.saves_list
    show = win.chk_saves_orphans.isChecked()
    orphans = 0
    for r in range(table.rowCount()):
        item = table.item(r, 0)
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        orphan = isinstance(data, SaveRow) and data.target is None
        orphans += orphan
        table.setRowHidden(r, orphan and not show)
    win.chk_saves_orphans.setText(tr("saves.show_orphans", count=orphans))
    win.chk_saves_orphans.setVisible(orphans > 0)
    current = table.currentRow()
    if current >= 0 and table.isRowHidden(current):
        table.setCurrentCell(-1, -1)


def save_row_buttons(win: MainWindow) -> tuple[QPushButton, ...]:
    return (
        win.btn_save_snapshot_now,
        win.btn_save_restore_selected,
        win.btn_save_open_folder,
        win.btn_save_export,
        win.btn_save_import,
        win.btn_save_import_one,
    )


def _selected_row(win: MainWindow) -> SaveRow | None:
    item = win.saves_list.item(win.saves_list.currentRow(), 0)
    data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
    return data if isinstance(data, SaveRow) else None


def update_saves_action_states(win: MainWindow) -> None:
    row = _selected_row(win)
    idle = not win._steam_busy
    has_target = row is not None and row.target is not None
    win.btn_save_snapshot_now.setEnabled(idle and has_target)
    win.btn_save_restore_selected.setEnabled(idle and has_target and row is not None and row.snapshots > 0)
    win.btn_save_open_folder.setEnabled(row is not None)
    win.btn_save_export.setEnabled(idle and has_target)
    win.btn_save_import.setEnabled(idle)  # every game of a backup folder, installed or not
    win.btn_save_import_one.setEnabled(idle and has_target)


def refresh_saves_page(win: MainWindow, after_play: bool = True, scan_sources: bool = True) -> None:
    """Reload the per-game table; with after_play, snapshot games played since the last look.

    With scan_sources, Steam/Hydra/UMU/Nexus games and the current save sizes are looked up in a
    worker, and the table is filled again when they arrive.
    """
    if not hasattr(win, "saves_list"):
        return  # the page is not built yet
    targets = collect_save_targets(win)
    if scan_sources:
        _start_sources_scan(win)
    selected = _selected_row(win)
    rows = build_save_rows(targets, list_save_archives())
    table = win.saves_list
    sizes: dict[str, int] = win._save_sizes
    table.setSortingEnabled(False)
    table.setRowCount(0)
    for r, row in enumerate(rows):
        table.insertRow(r)
        name_item = QTableWidgetItem(row.name if row.target is not None else tr("saves.orphan", name=row.name))
        name_item.setData(Qt.ItemDataRole.UserRole, row)
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.last_mtime)) if row.snapshots else "-"
        table.setItem(r, 0, name_item)
        source = source_label(row.target.source) if row.target is not None else "-"
        table.setItem(r, 1, QTableWidgetItem(source))
        now = (
            sizes.get(_target_size_key(row.target), sizes.get(row.slug))
            if row.target is not None
            else None
        )
        now_text = format_size_bytes(now) if now is not None else ("…" if row.target is not None else "-")
        table.setItem(r, 2, _SortableTableItem(now_text, sort_key=now or 0))
        table.setItem(r, 3, _SortableTableItem(last, sort_key=row.last_mtime))
        table.setItem(r, 4, _SortableTableItem(str(row.snapshots), sort_key=row.snapshots))
        table.setItem(r, 5, _SortableTableItem(format_size_bytes(row.size_bytes), sort_key=row.size_bytes))
        if selected is not None:
            same_row = (
                row.target is not None
                and selected.target is not None
                and row.target.prefix_dir == selected.target.prefix_dir
                and row.slug == selected.slug
            ) or (row.target is None and selected.target is None and row.slug == selected.slug)
            if same_row:
                table.setCurrentCell(r, 0)
    table.setSortingEnabled(True)
    _apply_orphan_filter(win)
    win.lbl_saves_empty.setVisible(not rows)
    update_saves_action_states(win)
    if after_play and _setting_bool(win.settings, AFTER_PLAY_KEY, True):
        _start_after_play(win, [t for t in targets if t.run_log is not None])


def _start_sources_scan(win: MainWindow) -> None:
    """Find Steam/Hydra/UMU/Nexus games and measure every game's saves, off the UI thread."""
    if win._closing_pending:
        return
    if win._save_sources_running:
        win._save_sources_pending = True
        return
    paths = win.paths
    base = collect_save_targets(win)

    def _job() -> object:
        extra = scan_external_save_targets(paths)
        sizes: dict[str, int] = {}
        for t in [*base, *extra]:
            skey = _target_size_key(t)
            if skey not in sizes:
                home_files = snapshot_home_files(snapshot_dir_for(t.name))
                sz = saves_size(
                    t.prefix_dir,
                    t.extra_dirs,
                    game_name=t.name,
                    game_dir=t.game_dir,
                    steam_appid=t.steam_appid,
                    home_files=home_files,
                )[0]
                sizes[skey] = sz
                sizes.setdefault(_slugify(t.name), sz)
        return extra, sizes

    worker = SteamOpWorker(_job)
    win._track_worker(worker)
    win._save_sources_running = True

    def _maybe_rerun_pending() -> None:
        if getattr(win, "_save_sources_pending", False) and not win._closing_pending:
            win._save_sources_pending = False
            _start_sources_scan(win)

    def _done(res: object) -> None:
        win._save_sources_running = False
        if isinstance(res, tuple) and not win._closing_pending:
            win._external_save_targets, win._save_sizes = res
            refresh_saves_page(win, after_play=False, scan_sources=False)
        _maybe_rerun_pending()

    def _err(_e: str) -> None:
        win._save_sources_running = False
        _maybe_rerun_pending()

    worker.finished_ok.connect(_done)
    worker.failed.connect(_err)
    worker.start()


def _start_after_play(win: MainWindow, targets: list[SaveTarget]) -> None:
    if not targets or win._after_play_running or win._steam_busy or win._closing_pending:
        return
    keep = snapshot_keep(win.settings)

    def _job() -> object:
        made = 0
        for t in targets:
            assert t.run_log is not None
            try:
                if snapshot_after_play(
                    t.prefix_dir, t.name, t.run_log, keep=keep, game_dir=t.game_dir, steam_appid=t.steam_appid
                ) is not None:
                    made += 1
            except Exception as exc:  # one bad prefix must not stop the others
                logging.getLogger(__name__).warning("After-play snapshot of %s failed: %s", t.name, exc)
        return made

    worker = SteamOpWorker(_job)
    win._track_worker(worker)
    win._after_play_running = True

    def _done(made: object) -> None:
        win._after_play_running = False
        if not win._closing_pending and isinstance(made, int) and made:
            refresh_saves_page(win, after_play=False)

    def _err(_e: str) -> None:
        win._after_play_running = False

    worker.finished_ok.connect(_done)
    worker.failed.connect(_err)
    worker.start()


def _run(
    win: MainWindow, status: str, job: Callable[[], object], on_ok: Callable[[object], None]
) -> None:
    """Run job() in a worker with the window busy; on_ok(result) on the UI thread."""
    win._begin_busy()
    win.status_label.setText(status)
    worker = SteamOpWorker(job)
    win._track_worker(worker)

    def _done(res: object) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        refresh_saves_page(win, after_play=False)
        if not win._closing_pending:
            on_ok(res)

    def _err(err: str) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        refresh_saves_page(win, after_play=False)
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("saves.error", err=err))

    worker.finished_ok.connect(_done)
    worker.failed.connect(_err)
    worker.start()
def _info(win: MainWindow, msg: str) -> None:
    win.status_label.setText(msg.splitlines()[0])
    QMessageBox.information(_dialog_parent(win), __app_name__, msg)


def _snapshot_now(win: MainWindow) -> None:
    row = _selected_row(win)
    if row is None or row.target is None or win._steam_busy:
        return
    t, keep = row.target, snapshot_keep(win.settings)
    before = {a.path for a in _snapshots_of(row.slug)}

    def _ok(res: object) -> None:
        if not isinstance(res, Path):
            _info(win, tr("saves.no_saves", name=t.name))
        elif res in before:
            _info(win, tr("saves.snapshot_unchanged", name=t.name))
        else:
            _info(win, tr("saves.snapshot_done", name=t.name, path=res))

    _run(
        win,
        tr("saves.snapshot_running", name=t.name),
        lambda: auto_snapshot(
            t.prefix_dir, t.name, "manual", keep=keep, game_dir=t.game_dir, steam_appid=t.steam_appid,
            extra_dirs=t.extra_dirs,
        ),
        _ok,
    )


def _restore_selected(win: MainWindow) -> None:
    row = _selected_row(win)
    if row is None or row.target is None or win._steam_busy:
        return
    t = row.target
    snaps = _snapshots_of(row.slug)
    if not snaps:
        return
    kinds = {"save_snapshot": tr("saves.type_save_snapshot"), "state_snapshot": tr("saves.type_state_snapshot")}
    labels = [
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(a.mtime))}  {a.path.stem}  "
        f"({kinds.get(a.kind, a.kind)}, {format_size_bytes(a.size_bytes)})"
        for a in snaps
    ]
    picked, ok = QInputDialog.getItem(
        _dialog_parent(win), __app_name__, tr("saves.pick_snapshot", name=t.name), labels, 0, False
    )
    if not ok or picked not in labels:
        return
    entry = snaps[labels.index(picked)]
    question = tr("saves.confirm_restore", name=t.name, snap=entry.path.name)
    if entry.kind == "save_snapshot":
        try:
            current = newest_save_mtime(
                t.prefix_dir, t.name, t.game_dir, steam_appid=t.steam_appid, extra_dirs=t.extra_dirs
            )
        except OSError:
            current = 0.0
        if current > entry.mtime + 60:  # the saves on disk are newer than the snapshot
            fmt = "%Y-%m-%d %H:%M"
            question += "\n\n" + tr(
                "saves.restore_older_warning",
                current=time.strftime(fmt, time.localtime(current)),
                snap=time.strftime(fmt, time.localtime(entry.mtime)),
            )
    if t.source == "nexus":
        if is_nexus_running():
            QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.nexus_running"))
            return
        question += "\n\n" + tr("saves.nexus_cloud_warning")
    if _restores_steam_cloud(t, entry):
        invalidate_process_cache()
        if is_steam_running():
            QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.steam_running"))
            return
    if _is_wine_game_running():
        QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.game_running"))
        return
    if QMessageBox.question(_dialog_parent(win), __app_name__, question) != QMessageBox.StandardButton.Yes:
        return

    def _ok(res: object) -> None:
        data = res if isinstance(res, tuple) else ()
        count, safety, skipped = data[:3] if len(data) >= 3 else (0, None, 0)
        msg = tr("saves.snapshot_restored", name=t.name, count=count, safety=safety or "-")
        if skipped:
            msg += "\n" + tr("saves.home_skipped", count=skipped)
        _info(win, msg)
        _offer_snapshot_cleanup(win, t, entry, data)

    _run(
        win,
        tr("saves.restore_running", name=t.name),
        lambda: _restore_and_verify_job(t, entry),
        _ok,
    )


def _restores_steam_cloud(t: SaveTarget, entry: SaveArchiveEntry) -> bool:
    """True when restoring entry would write into the game's Steam cloud folder."""
    if entry.kind == "state_snapshot" or not any(name == "steamcloud" for name, _ in t.extra_dirs):
        return False
    try:
        return archive_has_steamcloud(entry.path)
    except (OSError, zipfile.BadZipFile):
        return False


def _restore_job(t: SaveTarget, entry: SaveArchiveEntry) -> object:
    if t.source == "nexus" and is_nexus_running():  # checked again right before writing
        raise RuntimeError(tr("saves.nexus_running"))
    if _restores_steam_cloud(t, entry):
        invalidate_process_cache()
        if is_steam_running():  # Steam would sync over the restored files
            raise RuntimeError(tr("saves.steam_running"))
    if _is_wine_game_running():
        raise RuntimeError(tr("saves.game_running"))
    skipped: list[str] = []
    try:
        count, safety = restore_snapshot_safely(
            entry.path, t.prefix_dir, t.name, state=entry.kind == "state_snapshot",
            game_dir=t.game_dir, steam_appid=t.steam_appid, extra_dirs=t.extra_dirs, skipped=skipped,
        )
    except SteamCloudAppMismatch as exc:
        raise RuntimeError(
            tr("saves.steamcloud_appid_mismatch", snap=exc.snap_appid or "?", target=exc.target_appid or "?")
        ) from exc
    return count, safety, len(skipped)


def _restore_and_verify_job(t: SaveTarget, entry: SaveArchiveEntry) -> object:
    result = _restore_job(t, entry)
    if not isinstance(result, tuple):
        return result
    restored, _, skipped = result
    verified = entry.kind == "save_snapshot" and restored > 0 and not skipped and restored_snapshot_matches(
        entry.path, t.prefix_dir, t.game_dir, steam_appid=t.steam_appid, extra_dirs=t.extra_dirs,
    )
    return (*result, verified)


def _offer_snapshot_cleanup(win: MainWindow, t: SaveTarget, entry: SaveArchiveEntry, result: tuple) -> None:
    if len(result) != 4 or not result[3] or win._closing_pending:
        return
    win._offer_imported_backup_cleanup(
        entry.path,
        remove=lambda: remove_restored_snapshot(
            entry.path, t.prefix_dir, t.game_dir, steam_appid=t.steam_appid, extra_dirs=t.extra_dirs,
        ),
    )


def _open_folder(win: MainWindow) -> None:
    row = _selected_row(win)
    if row is None:
        return
    folder = snapshot_dir_for(row.name).parent / row.slug
    folder.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _import_to_snapshots(win: MainWindow, root: Path, row: SaveRow | None) -> None:
    """Ludusavi backup of games that are not installed: keep it as snapshots to restore later."""
    if win._steam_busy or win._closing_pending:
        return

    def _scan_job() -> object:
        if row is not None:
            folder = lds.find_game_folder(root, row.name)
            return [(row.name, folder)] if folder is not None else []
        return lds.list_backup_games(root)

    win._begin_busy()
    win.status_label.setText(tr("saves.import_scanning"))
    scan_worker = SteamOpWorker(_scan_job)
    win._track_worker(scan_worker)

    def _on_scanned(res: object) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending or win._steam_busy:
            return
        games: list[tuple[str, Path]] = res if isinstance(res, list) else []
        if not games:
            QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.import_none_found", path=root))
            return
        if len(games) > 1:
            labels = [tr("saves.import_all", count=len(games))] + [name for name, _ in games]
            picked, ok = QInputDialog.getItem(
                _dialog_parent(win), __app_name__, tr("saves.import_pick_game"), labels, 0, False
            )
            if not ok or picked not in labels:
                return
            if picked != labels[0]:
                games = [games[labels.index(picked) - 1]]

        def _job() -> object:
            done: list[tuple[str, int, int, bool]] = []
            for name, folder in games:
                _snap, stored, skipped, already = lds.import_ludusavi_to_snapshot(folder, name)
                done.append((name, stored, skipped, already))
            return done

        def _ok(import_res: object) -> None:
            win.chk_saves_orphans.setChecked(True)  # show what was just imported, installed or not
            show_import_result(win, import_res if isinstance(import_res, list) else [])

        _run(win, tr("saves.import_running", name=root.name), _job, _ok)

    def _on_scan_err(err: str) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("saves.error", err=err))

    scan_worker.finished_ok.connect(_on_scanned)
    scan_worker.failed.connect(_on_scan_err)
    scan_worker.start()


def import_result_text(results: list[tuple[str, int, int, bool]]) -> tuple[str, str]:
    """(summary with the 3 counters, one line per game) of a Ludusavi import; summary only when all was there."""
    new = sum(1 for _n, stored, _s, already in results if stored and not already)
    old = sum(1 for *_x, already in results if already)
    empty = sum(1 for _n, stored, _s, already in results if not stored and not already)
    if results and old == len(results):
        return tr("saves.import_all_already"), ""
    lines = []
    for name, stored, skipped, already in results:
        if already:
            lines.append(tr("saves.import_line_already", name=name))
        elif stored:
            lines.append(tr("saves.import_snapshot_line", name=name, stored=stored, skipped=skipped))
        else:
            lines.append(tr("saves.import_line_empty", name=name, skipped=skipped))
    return tr("saves.import_summary", new=new, already=old, empty=empty), "\n".join(lines)


def show_import_result(win: MainWindow, results: list[tuple[str, int, int, bool]]) -> None:
    """Import result inside the window: counters on top, scrollable per-game list (sized to the screen)."""
    summary, details = import_result_text(results)
    win.status_label.setText(summary.splitlines()[0])
    if not details:
        QMessageBox.information(_dialog_parent(win), __app_name__, summary)
        return
    show_text_dialog(win, tr("saves.import_result_title"), summary, tr("saves.import_snapshot_done") + "\n\n" + details)


def _remove_import_duplicates(win: MainWindow) -> None:
    """Delete Ludusavi imports identical to a newer one of the same game, after a confirmation listing them."""
    if win._steam_busy or win._closing_pending:
        return
    win._begin_busy()
    win.status_label.setText(tr("saves.dedup_scanning"))
    scan_worker = SteamOpWorker(lds.find_import_duplicates)
    win._track_worker(scan_worker)

    def _on_scanned(res: object) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if win._closing_pending or win._steam_busy:
            return
        dupes: list[Path] = res if isinstance(res, list) else []
        parent = _dialog_parent(win)
        if not dupes:
            QMessageBox.information(parent, __app_name__, tr("saves.dedup_none"))
            return
        listing = "\n".join(f"• {snapshot_display_name(p.parent.name)}: {p.name}" for p in dupes)
        box = QMessageBox(
            QMessageBox.Icon.Question,
            __app_name__,
            tr("saves.dedup_confirm", count=len(dupes)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            parent,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setDetailedText(listing)
        box.setInformativeText(listing if len(dupes) <= 8 else tr("saves.dedup_see_details"))
        answer = box.exec()
        box.deleteLater()
        if answer != QMessageBox.StandardButton.Yes:
            return

        def _delete_job() -> object:
            removed = 0
            for p in dupes:
                try:
                    if lds.remove_import_duplicate(p):
                        removed += 1
                except OSError as exc:
                    logging.getLogger(__name__).warning("Could not remove %s: %s", p, exc)
            return removed

        def _on_deleted(removed: object) -> None:
            _info(win, tr("saves.dedup_done", count=int(removed) if isinstance(removed, int) else 0))

        _run(win, tr("saves.dedup_scanning"), _delete_job, _on_deleted)

    def _on_scan_err(err: str) -> None:
        win._end_busy()
        win.status_label.setText(tr("status.ready"))
        if not win._closing_pending:
            QMessageBox.critical(_dialog_parent(win), __app_name__, tr("saves.error", err=err))

    scan_worker.finished_ok.connect(_on_scanned)
    scan_worker.failed.connect(_on_scan_err)
    scan_worker.start()


def existing_snapshots_for(name: str, archives: list[SaveArchiveEntry]) -> list[SaveArchiveEntry]:
    """Save snapshots kept for a game with this name (same folder, or same name ignoring case/symbols)."""
    slug, key = _slugify(name), lds._norm_key(name)
    return sorted(
        (
            a for a in archives
            if a.kind == "save_snapshot"
            and (a.game_slug == slug or (key and lds._norm_key(snapshot_display_name(a.game_slug)) == key))
        ),
        key=lambda a: a.mtime,
        reverse=True,
    )


def _find_ludusavi_backup(saved: str, name: str) -> Path | None:
    """Locate the game's backup folder inside saved or Ludusavi's configured backup root."""
    for root in (Path(saved) if saved else None, lds.ludusavi_backup_path()):
        if root is None or not root.is_dir():
            continue
        folder = lds.find_game_folder(root, name)
        if folder is None:
            continue
        found = lds.read_mapping_name(folder)
        if found is None:
            try:
                found = str(lds.load_mapping(folder).get("name") or folder.name)
            except (lds.LudusaviError, OSError):
                continue
        if lds._norm_key(found) == lds._norm_key(name):  # root may itself be another game's backup
            return folder
    return None


def _import_ludusavi_checked(target: SaveTarget, folder: Path) -> lds.ImportResult:
    if target.source == "nexus" and is_nexus_running():
        raise RuntimeError(tr("saves.nexus_running"))
    if _is_wine_game_running():
        raise RuntimeError(tr("saves.game_running"))
    return lds.import_ludusavi(
        folder,
        target.prefix_dir,
        target.name,
        game_dir=target.game_dir,
        steam_appid=target.steam_appid,
    )


def offer_existing_saves(win: MainWindow, game: StandaloneGame) -> None:
    """Right after an install: offer the saves DeckInstall already has (snapshot or Ludusavi backup)."""
    if game.native_linux or win._steam_busy or win._closing_pending:
        return
    parent = _dialog_parent(win)
    target = SaveTarget(game.name, game.prefix_dir, game.game_dir, game.steam_appid, game_log_dir(game) / "run.log")
    snaps = existing_snapshots_for(game.name, list_save_archives())
    if snaps:
        newest = snaps[0]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest.mtime))
        if QMessageBox.question(
            parent, __app_name__, tr("saves.offer_restore", name=game.name, count=len(snaps), date=when)
        ) != QMessageBox.StandardButton.Yes:
            return

        def _restored(res: object) -> None:
            data = res if isinstance(res, tuple) else ()
            count, safety, skipped = data[:3] if len(data) >= 3 else (0, None, 0)
            msg = tr("saves.snapshot_restored", name=game.name, count=count, safety=safety or "-")
            if skipped:
                msg += "\n" + tr("saves.home_skipped", count=skipped)
            _info(win, msg)
            _offer_snapshot_cleanup(win, target, newest, data)

        _run(
            win, tr("saves.restore_running", name=game.name),
            lambda: _restore_and_verify_job(target, newest), _restored,
        )
        return

    saved = str(win.settings.value(LUDUSAVI_DIR_KEY, "") or "")
    lookup_worker = SteamOpWorker(lambda: _find_ludusavi_backup(saved, game.name))
    if hasattr(win, "_track_worker"):
        win._track_worker(lookup_worker)

    def _on_found(res: object) -> None:
        if not isinstance(res, Path) or win._closing_pending or win._steam_busy:
            return
        folder = res
        if QMessageBox.question(
            _dialog_parent(win), __app_name__, tr("saves.offer_ludusavi", name=game.name, path=folder)
        ) != QMessageBox.StandardButton.Yes:
            return

        def _imported(imp_res: object) -> None:
            if isinstance(imp_res, lds.ImportResult):
                _info(win, tr(
                    "saves.import_done", name=game.name, restored=imp_res.restored, unchanged=imp_res.unchanged,
                    kept=imp_res.kept, skipped=imp_res.skipped, safety=imp_res.snapshot or "-",
                ))

        _run(
            win,
            tr("saves.import_running", name=game.name),
            lambda: _import_ludusavi_checked(target, folder),
            _imported,
        )

    def _on_lookup_err(err: str) -> None:
        logging.getLogger(__name__).debug("Ludusavi backup lookup skipped for %s: %s", game.name, err)

    lookup_worker.finished_ok.connect(_on_found)
    lookup_worker.failed.connect(_on_lookup_err)
    lookup_worker.start()


def _ludusavi_start_dir(win: MainWindow) -> Path:
    """Last folder used, else the backup folder from Ludusavi's own config, else ~/ludusavi-backup."""
    saved = str(win.settings.value(LUDUSAVI_DIR_KEY, "") or "")
    for cand in (Path(saved) if saved else None, lds.ludusavi_backup_path(), Path.home() / "ludusavi-backup"):
        if cand is not None and cand.is_dir():
            return cand
    return Path.home()


def _ludusavi_import_setting_dir(root: Path) -> str:
    """Store the Ludusavi backup root (root.parent when user picked a single game subfolder)."""
    return str(root.parent if (root / lds.MAPPING_FILE).is_file() else root)


def _ludusavi_pick(win: MainWindow, *, export: bool, one: bool = False) -> None:
    """Export/import-one work on the selected game; plain import takes every game of a backup folder."""
    row = _selected_row(win)
    if win._steam_busy or win._closing_pending or ((export or one) and (row is None or row.target is None)):
        return
    if not export and not one:
        row = None
        start_dir = _ludusavi_start_dir(win)

        def _picked_root(chosen: str) -> None:
            if not chosen or win._closing_pending or win._steam_busy:
                return
            root = Path(chosen)
            win.settings.setValue(LUDUSAVI_DIR_KEY, _ludusavi_import_setting_dir(root))
            _import_to_snapshots(win, root, row)

        _pick_dir_async(_dialog_parent(win), tr("saves.pick_import"), start_dir, _picked_root)
        return
    assert row is not None and row.target is not None
    t = row.target
    start = _ludusavi_start_dir(win)
    title = tr("saves.pick_export") if export else tr("saves.pick_import")

    def _picked(chosen: str) -> None:
        if not chosen or win._closing_pending or win._steam_busy:
            return
        root = Path(chosen)
        win.settings.setValue(
            LUDUSAVI_DIR_KEY,
            str(root) if export else _ludusavi_import_setting_dir(root),
        )
        if export:
            target_dir = root / lds.escape_folder_name(t.name)
            if (target_dir / lds.MAPPING_FILE).is_file():
                box = QMessageBox(
                    QMessageBox.Icon.Question,
                    __app_name__,
                    tr("saves.confirm_export_overwrite", name=t.name, path=target_dir),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    _dialog_parent(win),
                )
                box.setDefaultButton(QMessageBox.StandardButton.No)
                answer = box.exec()
                box.deleteLater()
                if answer != QMessageBox.StandardButton.Yes:
                    return

            def _exported(res: object) -> None:
                if isinstance(res, tuple):
                    path, count, left = res
                    _info(win, tr("saves.export_done", name=t.name, count=count, left=left, path=path))

            _run(
                win,
                tr("saves.export_running", name=t.name),
                lambda: lds.export_ludusavi(t.prefix_dir, t.name, root, game_dir=t.game_dir, steam_appid=t.steam_appid),
                _exported,
            )
            return

        win._begin_busy()
        win.status_label.setText(tr("saves.import_scanning"))
        scan_worker = SteamOpWorker(lambda: lds.find_game_folder(root, t.name))
        win._track_worker(scan_worker)

        def _on_found_one(res: object) -> None:
            win._end_busy()
            win.status_label.setText(tr("status.ready"))
            if win._closing_pending or win._steam_busy:
                return
            folder = res if isinstance(res, Path) else None
            if folder is None:
                QMessageBox.warning(
                    _dialog_parent(win), __app_name__, tr("saves.import_not_found", name=t.name, path=root)
                )
                return
            if t.source == "nexus" and is_nexus_running():
                QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.nexus_running"))
                return
            if _is_wine_game_running():
                QMessageBox.warning(_dialog_parent(win), __app_name__, tr("saves.game_running"))
                return
            if QMessageBox.question(
                _dialog_parent(win), __app_name__, tr("saves.confirm_import", name=t.name, path=folder)
            ) != QMessageBox.StandardButton.Yes:
                return

            def _ok(imp_res: object) -> None:
                if isinstance(imp_res, lds.ImportResult):
                    _info(win, tr(
                        "saves.import_done", name=t.name, restored=imp_res.restored, unchanged=imp_res.unchanged,
                        kept=imp_res.kept, skipped=imp_res.skipped, safety=imp_res.snapshot or "-",
                    ))

            _run(
                win,
                tr("saves.import_running", name=t.name),
                lambda: _import_ludusavi_checked(t, folder),
                _ok,
            )

        def _on_find_err(err: str) -> None:
            win._end_busy()
            win.status_label.setText(tr("status.ready"))
            if not win._closing_pending:
                QMessageBox.critical(_dialog_parent(win), __app_name__, tr("saves.error", err=err))

        scan_worker.finished_ok.connect(_on_found_one)
        scan_worker.failed.connect(_on_find_err)
        scan_worker.start()

    _pick_dir_async(_dialog_parent(win), title, start, _picked)


def open_save_sync_dialog(win: MainWindow) -> None:
    """Open the folder-based Save Sync dialog."""
    dlg_alive = {"open": True}
    syncing = {"active": False}

    class _BusySyncDialog(QDialog):
        def reject(self) -> None:
            if syncing["active"]:
                return
            super().reject()

        def closeEvent(self, event: QCloseEvent) -> None:
            if syncing["active"]:
                event.ignore()
                return
            super().closeEvent(event)

    dlg = _BusySyncDialog(_dialog_parent(win))
    dlg.setWindowTitle(tr("save_sync.title"))
    from ..ui_cards import clamp_dialog_size

    clamp_dialog_size(dlg, 640, 520, dlg.parentWidget())
    lay = QVBoxLayout(dlg)

    def _on_finished(_r: int) -> None:
        dlg_alive["open"] = False

    dlg.finished.connect(_on_finished)

    folder_row = QHBoxLayout()
    folder_row.addWidget(QLabel(tr("save_sync.folder_label")))
    legacy_default = Path.home() / "Sync" / "DeckInstallSaves"
    configured_dir = str(
        win.settings.value("save_sync/folder", str(DEFAULT_SYNC_DIR)) or DEFAULT_SYNC_DIR
    )
    if configured_dir == str(legacy_default) and not legacy_default.exists():
        configured_dir = str(DEFAULT_SYNC_DIR)
        win.settings.setValue("save_sync/folder", configured_dir)
    saved_dir = configured_dir
    sync_dir_edit = QLineEdit(saved_dir)
    btn_browse = QPushButton(tr("install.browse"))
    folder_row.addWidget(sync_dir_edit, 1)
    folder_row.addWidget(btn_browse)
    lay.addLayout(folder_row)

    plan_top = QHBoxLayout()
    plan_top.addWidget(QLabel(tr("save_sync.plan_label")))
    plan_top.addStretch()
    btn_refresh = QPushButton(tr("save_sync.btn_refresh"))
    plan_top.addWidget(btn_refresh)
    lay.addLayout(plan_top)

    plan_list = QTableWidget(0, 3)
    plan_list.setHorizontalHeaderLabels(
        [tr("add.detected_name_column"), tr("save_sync.table_action"), tr("save_sync.table_reason")]
    )
    plan_list.verticalHeader().setVisible(False)
    plan_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    plan_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    plan_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    plan_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    plan_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    plan_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    plan_list.horizontalHeader().setStretchLastSection(True)
    plan_list.horizontalHeader().setSectionsClickable(True)
    plan_list.setSortingEnabled(True)
    lay.addWidget(plan_list, 1)

    lbl_status = QLabel(tr("status.ready"))
    lbl_status.setObjectName("MutedText")
    lbl_status.setWordWrap(True)
    lay.addWidget(lbl_status)

    btn_preview = QPushButton(tr("save_sync.btn_preview"))
    btn_preview.setEnabled(False)
    btn_sync = QPushButton(tr("save_sync.btn_sync_now"))
    btn_sync.setObjectName("primaryBtn")
    btn_close = QPushButton(tr("proton_mgr.btn_close"))
    btn_close.clicked.connect(dlg.reject)
    lay.addLayout(dialog_footer(btn_sync, btn_close, left=(btn_preview,)))

    current_plan: list[SyncPlanItem] = []

    def _collect_games_for_sync() -> list[SyncGameTarget]:
        return [
            SyncGameTarget(
                game_name=t.name,
                prefix_dir=t.prefix_dir,
                game_dir=t.game_dir,
                steam_appid=t.steam_appid,
                extra_dirs=t.extra_dirs,
                source=t.source,
            )
            for t in collect_save_targets(win)
        ]

    def _current_sync_root() -> Path:
        raw = sync_dir_edit.text().strip() or str(DEFAULT_SYNC_DIR)
        win.settings.setValue("save_sync/folder", raw)
        return Path(raw).expanduser()

    plan_req = {"id": 0}

    class _PlanSyncWorker(QThread):
        ready = Signal(int, list)
        failed = Signal(int, str)

        def __init__(
            self, req_id: int, games: list[SyncGameTarget], sync_root: Path
        ) -> None:
            super().__init__()
            self._req_id = req_id
            self._games = games
            self._sync_root = sync_root

        def run(self) -> None:
            try:
                if self.isInterruptionRequested():
                    return
                items = plan_sync_all(self._games, self._sync_root)
                if self.isInterruptionRequested():
                    return
                self.ready.emit(self._req_id, items)
            except Exception as exc:
                self.failed.emit(self._req_id, _format_worker_error(exc))

    def _apply_plan_items(items: list[SyncPlanItem]) -> None:
        plan_list.setSortingEnabled(False)
        plan_list.clearSpans()
        plan_list.setRowCount(0)
        current_plan.clear()
        current_plan.extend(items)
        if not items:
            plan_list.setRowCount(1)
            plan_list.setSpan(0, 0, 1, 3)
            empty = QTableWidgetItem(tr("save_sync.no_games"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            plan_list.setItem(0, 0, empty)
            btn_sync.setEnabled(False)
            btn_preview.setEnabled(False)
            return
        actionable = False
        for row, item in enumerate(items):
            if item.action in ("PUSH", "PULL", "CONFLICT"):
                actionable = True
            plan_list.insertRow(row)
            it = QTableWidgetItem(item.game_name)
            it.setData(Qt.ItemDataRole.UserRole, item)
            plan_list.setItem(row, 0, it)
            plan_list.setItem(row, 1, QTableWidgetItem(item.action))
            plan_list.setItem(row, 2, QTableWidgetItem(item.reason))
        plan_list.setSortingEnabled(True)
        btn_sync.setEnabled(actionable)
        btn_preview.setEnabled(bool(items))

    def _refresh_plan() -> None:
        if not dlg_alive["open"] or win._closing_pending or syncing["active"]:
            return
        plan_req["id"] += 1
        req_id = plan_req["id"]
        sync_root = _current_sync_root()
        games = _collect_games_for_sync()
        btn_sync.setEnabled(False)
        btn_refresh.setEnabled(False)
        lbl_status.setText(tr("save_sync.loading_plan"))

        worker = _PlanSyncWorker(req_id, games, sync_root)
        win._track_worker(worker)

        def _on_plan_ready(r_id: int, items: list[SyncPlanItem]) -> None:
            if not dlg_alive["open"] or win._closing_pending or syncing["active"] or r_id != plan_req["id"]:
                return
            btn_refresh.setEnabled(True)
            lbl_status.setText(tr("status.ready"))
            _apply_plan_items(items)

        def _on_plan_err(r_id: int, err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending or syncing["active"] or r_id != plan_req["id"]:
                return
            btn_refresh.setEnabled(True)
            lbl_status.setText(tr("save_sync.error", err=err))

        worker.ready.connect(_on_plan_ready)
        worker.failed.connect(_on_plan_err)
        worker.start()

    def _on_browse() -> None:
        def _picked(picked: str) -> None:
            if picked and dlg_alive["open"] and not win._closing_pending and not syncing["active"]:
                sync_dir_edit.setText(picked)
                _refresh_plan()

        _pick_dir_async(dlg, tr("save_sync.pick_folder"), _current_sync_root(), _picked)

    class _FolderSaveSyncWorker(QThread):
        status = Signal(str)
        finished_ok = Signal(int, int, list)
        failed = Signal(str)

        def __init__(
            self,
            ops: list[tuple[SyncPlanItem, str]],
            sync_root: Path,
            max_keep: int,
        ) -> None:
            super().__init__()
            self._ops = ops
            self._sync_root = sync_root
            self._max_keep = max_keep

        def run(self) -> None:
            try:
                pushed = 0
                pulled = 0
                errors: list[str] = []
                for item, action in self._ops:
                    if self.isInterruptionRequested():
                        break
                    self.status.emit(
                        tr("save_sync.syncing_game", name=item.game_name, action=action)
                    )
                    try:
                        if action == "PUSH":
                            if (
                                push_game_saves(
                                    item.game_name,
                                    item.prefix_dir,
                                    self._sync_root,
                                    game_dir=item.game_dir,
                                    steam_appid=item.steam_appid,
                                    extra_dirs=item.extra_dirs,
                                )
                                is not None
                            ):
                                pushed += 1
                        elif action == "PULL":
                            if item.source == "nexus" and is_nexus_running():
                                raise RuntimeError(tr("saves.nexus_running"))
                            if any(name == "steamcloud" for name, _ in item.extra_dirs):
                                invalidate_process_cache()
                                if is_steam_running():
                                    raise RuntimeError(tr("saves.steam_running"))
                            if _is_wine_game_running():
                                raise RuntimeError(tr("saves.game_running"))
                            restored, _ = pull_game_saves(
                                item.game_name,
                                item.prefix_dir,
                                self._sync_root,
                                game_dir=item.game_dir,
                                steam_appid=item.steam_appid,
                                max_keep=self._max_keep,
                                extra_dirs=item.extra_dirs,
                            )
                            if restored > 0:
                                pulled += 1
                    except Exception as exc:
                        errors.append(f"{item.game_name}: {_format_worker_error(exc)}")
                self.finished_ok.emit(pushed, pulled, errors)
            except Exception as exc:
                self.failed.emit(_format_worker_error(exc))

    def _on_sync_now() -> None:
        if syncing["active"] or win._steam_busy or win._closing_pending:
            return
        sync_root = _current_sync_root()
        plan_snapshot = list(current_plan)
        ops: list[tuple[SyncPlanItem, str]] = []
        for item in plan_snapshot:
            if not dlg_alive["open"] or win._closing_pending:
                return
            if item.action in ("PUSH", "PULL"):
                ops.append((item, item.action))
            elif item.action == "CONFLICT":
                box = QMessageBox(dlg)
                box.setIcon(QMessageBox.Icon.Question)
                box.setWindowTitle(__app_name__)
                box.setText(tr("save_sync.conflict_prompt", name=item.game_name, reason=item.reason))
                btn_push = box.addButton(
                    tr("save_sync.conflict_keep_local"), QMessageBox.ButtonRole.AcceptRole
                )
                btn_pull = box.addButton(
                    tr("save_sync.conflict_use_remote"), QMessageBox.ButtonRole.AcceptRole
                )
                btn_skip = box.addButton(
                    tr("save_sync.conflict_skip"), QMessageBox.ButtonRole.RejectRole
                )
                box.setDefaultButton(btn_skip)
                box.exec()
                clicked = box.clickedButton()
                box.deleteLater()
                if clicked == btn_push:
                    ops.append((item, "PUSH"))
                elif clicked == btn_pull:
                    ops.append((item, "PULL"))

        if not dlg_alive["open"] or win._closing_pending:
            return
        if not ops:
            QMessageBox.information(dlg, __app_name__, tr("save_sync.nothing_to_sync"))
            return

        keep = snapshot_keep(win.settings)
        syncing["active"] = True
        win._begin_busy()
        for w in (btn_sync, btn_preview, btn_refresh, btn_browse, btn_close, sync_dir_edit):
            w.setEnabled(False)
        lbl_status.setText(tr("save_sync.syncing"))
        worker = _FolderSaveSyncWorker(ops, sync_root, keep)
        win._track_worker(worker)

        def _finish_sync_state() -> None:
            syncing["active"] = False
            win._end_busy()
            if not win._closing_pending:
                refresh_saves_page(win, after_play=False)

        def _ok(pushed: int, pulled: int, errors: list[str]) -> None:
            _finish_sync_state()
            if not dlg_alive["open"] or win._closing_pending:
                return
            for w in (btn_refresh, btn_browse, btn_close, sync_dir_edit):
                w.setEnabled(True)
            btn_preview.setEnabled(bool(current_plan))
            msg = tr("save_sync.done", pushed=pushed, pulled=pulled)
            if errors:
                msg += tr("save_sync.partial_errors", errs="\n".join(errors))
            lbl_status.setText(msg.splitlines()[0])
            _refresh_plan()
            if errors:
                QMessageBox.warning(dlg, __app_name__, msg)
            else:
                QMessageBox.information(dlg, __app_name__, msg)

        def _err(err: str) -> None:
            _finish_sync_state()
            if not dlg_alive["open"] or win._closing_pending:
                return
            for w in (btn_sync, btn_refresh, btn_browse, btn_close, sync_dir_edit):
                w.setEnabled(True)
            btn_preview.setEnabled(bool(current_plan))
            lbl_status.setText(tr("save_sync.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("save_sync.error", err=err))

        def _on_sync_status(m: str) -> None:
            if dlg_alive["open"]:
                lbl_status.setText(m)

        worker.status.connect(_on_sync_status)
        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _on_preview() -> None:
        if syncing["active"]:
            return
        sync_root = _current_sync_root()
        cur_item = plan_list.item(plan_list.currentRow(), 0)
        selected_plan = cur_item.data(Qt.ItemDataRole.UserRole) if cur_item is not None else None
        if isinstance(selected_plan, SyncPlanItem):
            targets = [selected_plan]
        else:
            targets = [it for it in current_plan if it.action in ("PUSH", "PULL", "CONFLICT")]
            if not targets:
                targets = list(current_plan)
        if not targets:
            QMessageBox.information(dlg, __app_name__, tr("save_sync.nothing_to_sync"))
            return

        btn_preview.setEnabled(False)
        lbl_status.setText(tr("save_sync.preview_loading"))

        class _PreviewSyncWorker(QThread):
            ready = Signal(list)
            failed = Signal(str)

            def run(self) -> None:
                try:
                    previews: list[SyncDryRunPreview] = []
                    for item in targets:
                        if self.isInterruptionRequested():
                            return
                        action_arg = item.action if item.action in ("PUSH", "PULL") else None
                        previews.append(
                            preview_sync_operation(
                                item.game_name,
                                item.prefix_dir,
                                sync_root,
                                action=action_arg,
                                game_dir=item.game_dir,
                                steam_appid=item.steam_appid,
                                extra_dirs=item.extra_dirs,
                            )
                        )
                    if self.isInterruptionRequested():
                        return
                    self.ready.emit(previews)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        p_worker = _PreviewSyncWorker()
        win._track_worker(p_worker)

        def _on_prev_ok(previews: list[SyncDryRunPreview]) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            btn_preview.setEnabled(bool(current_plan) and not syncing["active"])
            lbl_status.setText(tr("status.ready"))
            lines: list[str] = []
            for pv in previews:
                lines.append(
                    tr(
                        "save_sync.preview_header",
                        action=pv.action,
                        name=pv.game_name,
                        count=len(pv.files),
                        size=f"{pv.total_bytes:,} B",
                        reg=pv.registry_entries,
                    )
                )
                lines.append(f"  {pv.reason}")
                for f in pv.files[:200]:
                    lines.append(f"  [{f.operation}] {f.rel_path} ({f.size_bytes:,} B)")
                if len(pv.files) > 200:
                    lines.append(tr("save_sync.preview_truncated", extra=len(pv.files) - 200))
                lines.append("")

            pv_dlg = QDialog(dlg)
            pv_dlg.setWindowTitle(tr("save_sync.preview_title"))
            pv_dlg.resize(620, 440)
            pv_lay = QVBoxLayout(pv_dlg)
            txt = QPlainTextEdit()
            txt.setReadOnly(True)
            txt.setPlainText("\n".join(lines).strip())
            pv_lay.addWidget(txt, 1)
            btn_ok = QPushButton(tr("proton_mgr.btn_close"))
            btn_ok.clicked.connect(pv_dlg.accept)
            pv_lay.addWidget(btn_ok)
            pv_dlg.exec()
            pv_dlg.deleteLater()

        def _on_prev_err(err: str) -> None:
            if not dlg_alive["open"] or win._closing_pending:
                return
            btn_preview.setEnabled(bool(current_plan) and not syncing["active"])
            lbl_status.setText(tr("save_sync.error", err=err))
            QMessageBox.critical(dlg, __app_name__, tr("save_sync.error", err=err))

        p_worker.ready.connect(_on_prev_ok)
        p_worker.failed.connect(_on_prev_err)
        p_worker.start()

    btn_browse.clicked.connect(_on_browse)
    btn_refresh.clicked.connect(_refresh_plan)
    btn_preview.clicked.connect(_on_preview)
    btn_sync.clicked.connect(_on_sync_now)
    sync_dir_edit.editingFinished.connect(_refresh_plan)
    _refresh_plan()
    dlg.exec()
    dlg.deleteLater()
