"""Adoption wizard: one place to bring games from Hydra, Heroic, Lutris, Bottles, umu, old Steam
shortcuts and loose folders into a DeckInstall library (adopt.py does the work).

Page 1 lists what was found (scan in a worker), page 2 shows each selected game's options and plan
(planned in a worker: sizes, same/other disk, free space, warnings, blockers), page 3 adopts the
ready games one after the other and ends with a summary. Blocked games are skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import adopt as ad
from ..fsutil import unique_dir
from ..i18n import tr
from ..modes import STEAM, feature_note
from ..steam_paths import SteamPaths, is_gamescope_session
from ..ui_cards import FlowLayout, _pick_dir_async, _pick_file_async, format_size_bytes, shortcut_target_button
from ..ui_workers_adopt import AdoptPlanWorker, AdoptScanWorker, AdoptWorker

TrackFn = Callable[[QThread], None]

_KIND_ORDER = (ad.HEROIC, ad.LUTRIS, ad.BOTTLES, ad.HYDRA, ad.STEAM, ad.UMU, ad.FOLDER)
_LIVE_WORKERS: set[QThread] = set()  # workers outlive the dialog until they finish


def kind_label(kind: str) -> str:
    return {
        ad.HEROIC: "Heroic",
        ad.LUTRIS: "Lutris",
        ad.BOTTLES: "Bottles",
        ad.HYDRA: "Hydra",
        ad.STEAM: tr("adopt.kind_steam"),
        ad.UMU: tr("adopt.kind_umu"),
        ad.FOLDER: tr("adopt.kind_folder"),
    }.get(kind, kind)


def warning_text(code: str) -> str:
    return {
        ad.W_SHARED_PREFIX: tr("adopt.warn_shared_prefix"),
        ad.W_SYMLINK_PREFIX: tr("adopt.warn_symlink_prefix"),
        ad.W_NO_PREFIX: tr("adopt.warn_no_prefix"),
        ad.W_GAME_IN_DRIVE_C: tr("adopt.warn_game_in_drive_c"),
        ad.W_NEEDS_GAME_FOLDER: tr("adopt.warn_needs_game_folder"),
        ad.W_GAME_ROOT_GUESSED: tr("adopt.warn_game_root_guessed"),
        ad.W_PREFIX_INTERNAL: tr("adopt.warn_prefix_on_internal_disk"),
        ad.W_PREFIX_IN_GAME_DIR: tr("adopt.warn_prefix_inside_game_dir"),
        ad.W_LAUNCHER_ENTRY_BREAKS: tr("adopt.warn_launcher_entry_breaks"),
        ad.W_STEAM_SHORTCUT_BREAKS: tr("adopt.warn_steam_shortcut_breaks"),
        ad.W_STEAM_OTHER_USERS: tr("adopt.warn_steam_other_users"),
        ad.W_RUNNER_NOT_INSTALLED: tr("adopt.warn_runner_not_installed"),
    }.get(code, code)


def blocker_text(code: str) -> str:
    return {
        ad.B_NEEDS_GAME_FOLDER: tr("adopt.block_needs_game_folder"),
        ad.B_EXE_MISSING: tr("adopt.block_exe_missing"),
        ad.B_EXE_OUTSIDE_GAME_DIR: tr("adopt.block_exe_outside_game_dir"),
        ad.B_GAME_DIR_SYMLINK: tr("adopt.block_game_dir_symlink"),
        ad.B_GAME_DIR_PROTECTED: tr("adopt.block_game_dir_protected"),
        ad.B_GAME_DIR_IS_PREFIX: tr("adopt.block_game_dir_is_prefix"),
        ad.B_OVERLAPS_LIBRARY: tr("adopt.block_overlaps_library"),
        ad.B_LIBRARY_UNAVAILABLE: tr("adopt.block_library_unavailable"),
        ad.B_PREFIX_LAYOUT_CONFLICT: tr("adopt.block_prefix_layout_conflict"),
        ad.B_NO_SPACE: tr("adopt.block_no_space"),
        ad.B_ALREADY_MANAGED: tr("adopt.block_already_managed"),
    }.get(code, code)


def step_text(step: str) -> str:
    return {
        ad.STEP_SNAPSHOT: tr("adopt.step_snapshot"),
        ad.STEP_MOVE_GAME: tr("adopt.step_move_game"),
        ad.STEP_MOVE_PREFIX: tr("adopt.step_move_prefix"),
        ad.STEP_LAUNCHER: tr("adopt.step_launcher"),
        ad.STEP_SAVES: tr("adopt.step_saves"),
        ad.STEP_STEAM: tr("adopt.step_steam"),
        ad.STEP_DONE: tr("adopt.step_done"),
    }.get(step, step)


def _prefix_action_text(action: str) -> str:
    return {
        "move": tr("adopt.prefix_move"),
        "copy_saves": tr("adopt.prefix_copy_saves"),
        "new": tr("adopt.prefix_new"),
        "none": tr("adopt.prefix_none"),
    }.get(action, action)


@dataclass
class _Item:
    """One selected game on page 2/3."""

    cand: ad.AdoptionCandidate
    opts: ad.AdoptOptions
    library_root: Path | None
    plan: ad.AdoptionPlan | None = None
    plan_error: str = ""
    req: int = 0
    folder_confirmed: bool = False
    status: str = ""  # page 3: "ok", "failed", "not_undone", "skipped"
    message: str = ""

    @property
    def needs_confirm(self) -> bool:
        return ad.W_GAME_ROOT_GUESSED in self.cand.warnings and not self.folder_confirmed

    @property
    def ready(self) -> bool:
        return self.plan is not None and self.plan.ok and not self.needs_confirm


class AdoptWizard(QDialog):
    """Pick games from other launchers, review each plan, adopt them in sequence."""

    PAGE_SOURCES, PAGE_PLAN, PAGE_RUN = range(3)

    def __init__(
        self,
        paths: SteamPaths | None,
        libraries: list[tuple[str, Path]],
        steam_writable: bool = True,
        steam_note: str = "",
        track_worker: TrackFn | None = None,
        parent: QWidget | None = None,
        snapshot_fn: ad.SnapshotFn | None = None,
        steam_add_allowed: bool = True,
        steamos: bool = False,
    ) -> None:
        super().__init__(parent)
        self._steamos = steamos  # SteamOS runs every game through Steam (Steam mode only)
        self._steam_linked = False
        self._snapshot_fn = snapshot_fn
        self.setWindowTitle(tr("adopt.title"))
        self.resize(900, 620)
        self._paths = paths
        self._libraries = libraries or []
        self._steam_writable = steam_writable
        self._steam_note = steam_note
        self._steam_add_allowed = steam_add_allowed
        self._track = track_worker
        self._req = 0
        self._scan_req = 0
        self._closed = False
        self._running = False
        self._candidates: list[ad.AdoptionCandidate] = []
        self._extra: list[ad.AdoptionCandidate] = []  # loose folders added by the user
        self._checked_keys: set[str] = set()
        self.items: list[_Item] = []
        self._current = -1
        self._run_index = -1
        self.adopted: list[ad.AdoptionResult] = []

        lay = QVBoxLayout(self)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_sources_page())
        self.stack.addWidget(self._build_plan_page())
        self.stack.addWidget(self._build_run_page())

        nav = QHBoxLayout()
        self.btn_back = QPushButton(tr("adopt.btn_back"))
        self.btn_back.clicked.connect(self._go_back)
        self.btn_next = QPushButton(tr("adopt.btn_next"))
        self.btn_next.setObjectName("primaryBtn")
        self.btn_next.clicked.connect(self._go_next)
        self.btn_cancel = QPushButton(tr("adopt.btn_cancel"))
        self.btn_cancel.clicked.connect(self.reject)
        nav.addWidget(self.btn_back)
        nav.addStretch()
        nav.addWidget(self.btn_cancel)
        nav.addWidget(self.btn_next)
        lay.addLayout(nav)
        self._update_nav()
        self.start_scan()

    # --- worker plumbing ------------------------------------------------------------------

    def _start(self, worker: QThread) -> None:
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda w=worker: _LIVE_WORKERS.discard(w))
        if self._track is not None:
            self._track(worker)
        worker.start()

    def busy(self) -> bool:
        return any(w.isRunning() for w in _LIVE_WORKERS)

    # --- page 1: sources -------------------------------------------------------------------

    def _build_sources_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        intro = QLabel(tr("adopt.intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.lbl_scan = QLabel()
        self.lbl_scan.setObjectName("MutedText")
        self.lbl_scan.setWordWrap(True)
        lay.addWidget(self.lbl_scan)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr("adopt.col_game"), tr("adopt.col_location")])
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.tree.setColumnWidth(0, 320)
        self.tree.itemChanged.connect(self._on_tree_changed)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.btn_add_folder = QPushButton(tr("adopt.btn_add_folder"))
        self.btn_add_folder.clicked.connect(self._add_folder)
        self.btn_rescan = QPushButton(tr("adopt.btn_rescan"))
        self.btn_rescan.clicked.connect(self.start_scan)
        row.addWidget(self.btn_add_folder)
        row.addStretch()
        row.addWidget(self.btn_rescan)
        lay.addLayout(row)
        return page

    def start_scan(self) -> None:
        self._scan_req += 1
        self.lbl_scan.setText(tr("adopt.scanning"))
        self.btn_rescan.setEnabled(False)
        worker = AdoptScanWorker(self._scan_req, self._paths)
        worker.ready.connect(self._on_scan_ready)
        worker.failed.connect(self._on_scan_failed)
        self._start(worker)

    def _on_scan_ready(self, req_id: int, cands: object) -> None:
        if self._closed or req_id != self._scan_req:
            return
        found = cands if isinstance(cands, list) else []
        self._candidates = [c for c in found if isinstance(c, ad.AdoptionCandidate)]
        self.btn_rescan.setEnabled(True)
        self.lbl_scan.setText(
            tr("adopt.scan_done", count=len(self._candidates)) if self._candidates else tr("adopt.scan_empty")
        )
        self._fill_tree()

    def _on_scan_failed(self, req_id: int, err: str) -> None:
        if self._closed or req_id != self._scan_req:
            return
        self.btn_rescan.setEnabled(True)
        self.lbl_scan.setText(tr("adopt.scan_failed", err=err))
        self._fill_tree()

    def all_candidates(self) -> list[ad.AdoptionCandidate]:
        return self._candidates + self._extra

    def _fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        by_kind: dict[str, list[ad.AdoptionCandidate]] = {}
        for c in self.all_candidates():
            by_kind.setdefault(c.kind, []).append(c)
        for kind in sorted(by_kind, key=lambda k: _KIND_ORDER.index(k) if k in _KIND_ORDER else 99):
            group = QTreeWidgetItem([f"{kind_label(kind)} ({len(by_kind[kind])})", ""])
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            self.tree.addTopLevelItem(group)
            for c in sorted(by_kind[kind], key=lambda x: x.name.lower()):
                where = c.game_dir or c.exe or c.prefix_dir
                child = QTreeWidgetItem([c.name, str(where) if where else ""])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.UserRole, c.key)
                child.setCheckState(
                    0, Qt.CheckState.Checked if c.key in self._checked_keys else Qt.CheckState.Unchecked
                )
                child.setToolTip(1, str(where) if where else "")
                if c.warnings:
                    child.setToolTip(0, "\n".join(warning_text(w) for w in c.warnings))
                group.addChild(child)
            group.setExpanded(True)
        self.tree.blockSignals(False)
        self._update_nav()

    def _on_tree_changed(self, item: QTreeWidgetItem, _col: int) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str):
            if item.checkState(0) == Qt.CheckState.Checked:
                self._checked_keys.add(key)
            else:
                self._checked_keys.discard(key)
        else:  # a group: Qt updates the children; sync the keys from them
            for i in range(item.childCount()):
                child = item.child(i)
                if child is None:
                    continue
                ck = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(ck, str):
                    if child.checkState(0) == Qt.CheckState.Checked:
                        self._checked_keys.add(ck)
                    else:
                        self._checked_keys.discard(ck)
        self._update_nav()

    def set_checked(self, keys: set[str]) -> None:
        """Select candidates by key (the tests drive the wizard with it)."""
        self._checked_keys = set(keys)
        self._fill_tree()

    def selected_candidates(self) -> list[ad.AdoptionCandidate]:
        return [c for c in self.all_candidates() if c.key in self._checked_keys]

    def _add_folder(self) -> None:
        def _on_dir(folder: str) -> None:
            if not folder:
                return
            cand = ad.candidate_from_folder(Path(folder))
            if cand.exe is not None:
                self.add_candidate(cand)
                return

            def _on_exe(exe: str) -> None:
                if exe:
                    self.add_candidate(ad.candidate_from_folder(Path(folder), exe=Path(exe)))

            _pick_file_async(self, tr("adopt.pick_exe"), Path(folder), "", _on_exe)

        _pick_dir_async(self, tr("adopt.pick_folder"), Path.home(), _on_dir)

    def add_candidate(self, cand: ad.AdoptionCandidate) -> None:
        for existing in self._candidates:
            if (
                cand.game_dir is not None and existing.game_dir is not None
                and ad._resolved(cand.game_dir) == ad._resolved(existing.game_dir)
            ):
                self._checked_keys.add(existing.key)
                self._fill_tree()
                return
        self._extra = [c for c in self._extra if c.key != cand.key] + [cand]
        self._checked_keys.add(cand.key)
        self._fill_tree()

    # --- page 2: options and plan ----------------------------------------------------------

    def _build_plan_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        split = QSplitter()
        self.game_list = QListWidget()
        self.game_list.currentRowChanged.connect(self._show_item)
        split.addWidget(self.game_list)

        right = QWidget()
        rlay = QVBoxLayout(right)
        self.lbl_game_title = QLabel()
        self.lbl_game_title.setObjectName("SectionTitle")
        rlay.addWidget(self.lbl_game_title)
        self.lbl_source = QLabel()
        self.lbl_source.setWordWrap(True)
        self.lbl_source.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_source.setObjectName("MutedText")
        rlay.addWidget(self.lbl_source)

        folder_row = QHBoxLayout()
        self.btn_pick_game_folder = QPushButton(tr("adopt.btn_pick_game_folder"))
        self.btn_pick_game_folder.clicked.connect(self._pick_game_folder)
        self.chk_confirm_folder = QCheckBox(tr("adopt.chk_confirm_folder"))
        self.chk_confirm_folder.toggled.connect(self._on_confirm_folder)
        folder_row.addWidget(self.btn_pick_game_folder)
        folder_row.addWidget(self.chk_confirm_folder)
        folder_row.addStretch()
        rlay.addLayout(folder_row)

        form = QFormLayout()
        self._plan_form = form
        self.cmb_library = QComboBox()
        for label, root in self._libraries:
            self.cmb_library.addItem(label, str(root))
        self.cmb_library.currentIndexChanged.connect(self._on_options_changed)
        form.addRow(tr("adopt.library"), self.cmb_library)
        self.chk_move_game = QCheckBox(tr("adopt.chk_move_game"))
        self.chk_move_game.toggled.connect(self._on_options_changed)
        form.addRow("", self.chk_move_game)
        self.chk_move_prefix = QCheckBox(tr("adopt.chk_move_prefix"))
        self.chk_move_prefix.setToolTip(tr("adopt.chk_move_prefix_tip"))
        self.chk_move_prefix.toggled.connect(self._on_options_changed)
        form.addRow("", self.chk_move_prefix)
        self.cmb_proton = QComboBox()
        self.cmb_proton.currentIndexChanged.connect(self._on_options_changed)
        form.addRow(tr("adopt.proton"), self.cmb_proton)
        targets = FlowLayout(spacing=8)
        self.btn_shortcut_menu = shortcut_target_button(tr("add.shortcut_menu"))
        self.btn_shortcut_desktop = shortcut_target_button(tr("add.shortcut_desktop"))
        self.btn_shortcut_steam = shortcut_target_button(tr("add.shortcut_steam"))
        for button in (self.btn_shortcut_menu, self.btn_shortcut_desktop, self.btn_shortcut_steam):
            button.toggled.connect(self._on_shortcut_targets_changed)
            targets.addWidget(button)
        self.btn_shortcut_steam.setVisible(self._paths is not None)
        form.addRow(tr("add.shortcuts_label"), targets)
        self.lbl_targets_note = QLabel()
        self.lbl_targets_note.setObjectName("MutedText")
        self.lbl_targets_note.setWordWrap(True)
        form.addRow("", self.lbl_targets_note)

        self.steam_box = QWidget()
        slay = QVBoxLayout(self.steam_box)
        slay.setContentsMargins(0, 0, 0, 0)
        self.rb_steam_link = QRadioButton(tr("adopt.steam_keep_link"))
        self.rb_steam_remove = QRadioButton(tr("adopt.steam_remove"))
        self._steam_group = QButtonGroup(self)
        self._steam_group.addButton(self.rb_steam_link)
        self._steam_group.addButton(self.rb_steam_remove)
        self.rb_steam_link.toggled.connect(self._on_options_changed)
        self.lbl_steam_note = QLabel(self._steam_note)
        self.lbl_steam_note.setWordWrap(True)
        self.lbl_steam_note.setObjectName("MutedText")
        self.lbl_steam_note.setVisible(not self._steam_writable and bool(self._steam_note))
        if not self._steam_add_allowed:
            self.rb_steam_remove.setEnabled(False)
            self.rb_steam_remove.setToolTip(self._steam_note)
        slay.addWidget(self.rb_steam_link)
        slay.addWidget(self.rb_steam_remove)
        slay.addWidget(self.lbl_steam_note)
        form.addRow(tr("adopt.steam_shortcut"), self.steam_box)
        rlay.addLayout(form)

        self.lbl_plan = QLabel()
        self.lbl_plan.setWordWrap(True)
        self.lbl_plan.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_plan.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rlay.addWidget(self.lbl_plan)
        self.lbl_warnings = QLabel()
        self.lbl_warnings.setWordWrap(True)
        self.lbl_warnings.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_warnings.setStyleSheet("color: #ffd180;")
        rlay.addWidget(self.lbl_warnings)
        self.lbl_blockers = QLabel()
        self.lbl_blockers.setWordWrap(True)
        self.lbl_blockers.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_blockers.setStyleSheet("color: #ff8a8a;")
        rlay.addWidget(self.lbl_blockers)
        rlay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right)
        split.addWidget(scroll)
        split.setStretchFactor(1, 3)
        lay.addWidget(split, 1)
        self.lbl_plan_summary = QLabel()
        self.lbl_plan_summary.setWordWrap(True)
        lay.addWidget(self.lbl_plan_summary)
        return page

    def _default_library(self) -> Path | None:
        return Path(self._libraries[0][1]) if self._libraries else None

    def _enter_plan_page(self) -> None:
        old = {it.cand.key: it for it in self.items}
        self.items = []
        for c in self.selected_candidates():
            prev = old.get(c.key)
            if prev is not None:
                self.items.append(prev)
                continue
            opts = ad.AdoptOptions(
                remove_from_steam=False,
                link_steam_compatdata=True,
                proton_name=None if c.proton_hint else "",
            )
            self.items.append(_Item(c, opts, self._default_library()))
        self.game_list.blockSignals(True)
        self.game_list.clear()
        for it in self.items:
            self.game_list.addItem(QListWidgetItem(it.cand.name))
        self.game_list.blockSignals(False)
        for idx, it in enumerate(self.items):
            if it.plan is None:
                self._replan(idx)
        self._refresh_list_marks()
        self.game_list.setCurrentRow(0)
        self._show_item(0)

    def _replan(self, idx: int) -> None:
        it = self.items[idx]
        self._req += 1
        it.req = self._req
        it.plan = None
        it.plan_error = ""
        worker = AdoptPlanWorker(it.req, it.cand, it.library_root, it.opts)
        worker.ready.connect(self._on_plan_ready)
        worker.failed.connect(self._on_plan_failed)
        self._start(worker)
        self._refresh_list_marks()

    def _item_by_req(self, req_id: int) -> int:
        return next((i for i, it in enumerate(self.items) if it.req == req_id), -1)

    def _on_plan_ready(self, req_id: int, plan: object) -> None:
        idx = self._item_by_req(req_id)
        if self._closed or idx < 0 or not isinstance(plan, ad.AdoptionPlan):
            return
        self.items[idx].plan = replace(plan, options=self.items[idx].opts)
        self._refresh_list_marks()
        if idx == self._current:
            self._render_plan(self.items[idx])

    def _on_plan_failed(self, req_id: int, err: str) -> None:
        idx = self._item_by_req(req_id)
        if self._closed or idx < 0:
            return
        self.items[idx].plan_error = err
        self._refresh_list_marks()
        if idx == self._current:
            self._render_plan(self.items[idx])

    def _mark(self, it: _Item) -> str:
        if it.plan_error:
            return tr("adopt.mark_error")
        if it.plan is None:
            return tr("adopt.mark_planning")
        if not it.plan.ok:
            return tr("adopt.mark_blocked")
        if it.needs_confirm:
            return tr("adopt.mark_confirm")
        return tr("adopt.mark_ready")

    def _refresh_list_marks(self) -> None:
        for i, it in enumerate(self.items):
            row = self.game_list.item(i)
            if row is not None:
                row.setText(f"{it.cand.name}  [{self._mark(it)}]")
        ready = sum(1 for it in self.items if it.ready)
        pending = sum(1 for it in self.items if it.plan is None and not it.plan_error)
        if pending:
            self.lbl_plan_summary.setText(tr("adopt.planning_count", count=pending))
        else:
            self.lbl_plan_summary.setText(
                tr("adopt.ready_summary", ready=ready, skipped=len(self.items) - ready)
            )
        self._update_nav()

    def _show_item(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.items):
            return
        self._current = idx
        it = self.items[idx]
        c = it.cand
        widgets = (
            self.cmb_library,
            self.chk_move_game,
            self.chk_move_prefix,
            self.cmb_proton,
            self.btn_shortcut_menu,
            self.btn_shortcut_desktop,
            self.btn_shortcut_steam,
            self.rb_steam_link,
            self.rb_steam_remove,
            self.chk_confirm_folder,
        )
        for w in widgets:
            w.blockSignals(True)
        self.lbl_game_title.setText(c.name)
        where = [
            tr("adopt.source_kind", kind=kind_label(c.kind)),
            tr("adopt.source_game", path=str(c.game_dir) if c.game_dir else "-"),
        ]
        if c.prefix_dir is not None:
            where.append(tr("adopt.source_prefix", path=str(c.prefix_dir)))
        self.lbl_source.setText("\n".join(where))
        lib_idx = self.cmb_library.findData(str(it.library_root)) if it.library_root else -1
        self.cmb_library.setCurrentIndex(max(lib_idx, 0))
        self.chk_move_game.setChecked(it.opts.move_game or c.in_drive_c)
        self.chk_move_game.setEnabled(not c.in_drive_c)
        native = c.native_linux
        self.chk_move_prefix.setChecked(it.opts.move_prefix)
        self.chk_move_prefix.setVisible(not native)
        self.cmb_proton.clear()
        if c.proton_hint:
            self.cmb_proton.addItem(tr("adopt.proton_source", name=c.proton_hint), "source")
        self.cmb_proton.addItem(tr("adopt.proton_default"), "default")
        self.cmb_proton.setCurrentIndex(
            0 if (it.opts.proton_name is None or it.opts.proton_name) and c.proton_hint else self.cmb_proton.count() - 1
        )
        self.cmb_proton.setEnabled(not native)
        self.btn_shortcut_menu.setChecked(it.opts.create_menu_shortcut)
        self.btn_shortcut_desktop.setChecked(it.opts.create_desktop_shortcut)
        has_steam = c.shortcut_appid is not None
        remove_steam = has_steam and it.opts.remove_from_steam and self._steam_add_allowed
        self.steam_box.setVisible(has_steam)
        steam_label = self._plan_form.labelForField(self.steam_box)
        if steam_label is not None:
            steam_label.setVisible(has_steam)
        if has_steam:
            self.rb_steam_remove.setChecked(remove_steam)
            self.rb_steam_link.setChecked(not remove_steam)
        self._sync_steam_shortcut_pill(has_steam, remove_steam)
        guessed = ad.W_GAME_ROOT_GUESSED in c.warnings
        needs = ad.W_NEEDS_GAME_FOLDER in c.warnings or c.game_dir is None
        self.btn_pick_game_folder.setVisible(guessed or needs)
        self.chk_confirm_folder.setVisible(guessed)
        self.chk_confirm_folder.setChecked(it.folder_confirmed)
        for w in widgets:
            w.blockSignals(False)
        self._render_plan(it)

    def _render_plan(self, it: _Item) -> None:
        if it.plan_error:
            self.lbl_plan.setText(tr("adopt.plan_failed", err=it.plan_error))
            self.lbl_warnings.setText("")
            self.lbl_blockers.setText("")
            return
        plan = it.plan
        if plan is None:
            self.lbl_plan.setText(tr("adopt.planning"))
            self.lbl_warnings.setText("")
            self.lbl_blockers.setText("")
            return
        lines = [tr("adopt.plan_target", path=str(plan.game_dir))]
        for mv in plan.moves:
            what = tr("adopt.move_game") if mv.what == "game" else tr("adopt.move_prefix")
            how = tr("adopt.same_disk") if mv.same_fs else tr("adopt.other_disk")
            lines.append(tr("adopt.plan_move", what=what, size=format_size_bytes(mv.size_bytes), how=how))
        if not any(mv.what == "game" for mv in plan.moves):
            lines.append(tr("adopt.plan_game_stays"))
        lines.append(_prefix_action_text(plan.prefix_action))
        if plan.prefix_dir is not None:
            lines.append(tr("adopt.plan_prefix_at", path=str(plan.prefix_dir)))
        lines.append(tr("adopt.plan_proton", name=plan.proton_name or tr("adopt.proton_default")))
        if (
            self._steam_add_allowed and plan.candidate.shortcut_appid is not None
            and not it.opts.remove_from_steam and any(move.what == "game" for move in plan.moves)
        ):
            lines.append(tr("adopt.plan_steam_update"))
        elif it.opts.add_to_steam and self._paths is not None:
            lines.append(tr("adopt.plan_steam_add"))
        if plan.bytes_to_copy:
            lines.append(tr("adopt.plan_copy", size=format_size_bytes(plan.bytes_to_copy)))
        for where, free in plan.free_bytes.items():
            lines.append(tr("adopt.plan_free", path=where, size=format_size_bytes(free)))
        self.lbl_plan.setText("\n".join(lines))
        warns = [w for w in plan.warnings if w not in plan.blockers]
        self.lbl_warnings.setText("\n".join(f"• {warning_text(w)}" for w in warns))
        blocks = [blocker_text(b) for b in plan.blockers]
        if it.needs_confirm:
            blocks.append(tr("adopt.confirm_needed"))
        self.lbl_blockers.setText("\n".join(f"✖ {b}" for b in blocks))

    def _sync_mode_pills(self) -> None:
        """One mode per game: in Steam (linked or being added) means no menu/desktop launchers."""
        steam_on = self.btn_shortcut_steam.isEnabled() and self.btn_shortcut_steam.isChecked()
        if self._steamos and self._paths is not None and self._steam_add_allowed and not self._steam_linked:
            if not steam_on:  # SteamOS: an adopted game always goes to Steam
                self.btn_shortcut_steam.blockSignals(True)
                self.btn_shortcut_steam.setChecked(True)
                self.btn_shortcut_steam.blockSignals(False)
                steam_on = True
        steam_mode = self._steam_linked or steam_on
        note = feature_note("app_shortcuts", STEAM) if steam_mode else ""
        for button in (self.btn_shortcut_menu, self.btn_shortcut_desktop):
            button.blockSignals(True)
            if steam_mode:
                button.setChecked(False)
            button.setEnabled(not steam_mode)
            button.blockSignals(False)
        self.lbl_targets_note.setText(note)
        self.lbl_targets_note.setVisible(bool(note))

    def _sync_steam_shortcut_pill(self, has_steam: bool, removing_steam: bool) -> None:
        linked = has_steam and not removing_steam
        self._steam_linked = linked
        self.btn_shortcut_steam.blockSignals(True)
        self.btn_shortcut_steam.setEnabled(self._steam_add_allowed and not linked)
        self.btn_shortcut_steam.setText(
            tr("add.shortcut_steam_linked") if linked else tr("add.shortcut_steam")
        )
        idx = self._current
        add_checked = (
            bool(self.items[idx].opts.add_to_steam) if 0 <= idx < len(self.items) else False
        )
        self.btn_shortcut_steam.setChecked(not linked and self._steam_add_allowed and add_checked)
        self.btn_shortcut_steam.blockSignals(False)

    def _on_shortcut_targets_changed(self, *_args: object) -> None:
        idx = self._current
        if idx < 0 or idx >= len(self.items):
            return
        it = self.items[idx]
        self._sync_mode_pills()
        add_steam = (
            self._paths is not None
            and self.btn_shortcut_steam.isEnabled()
            and self.btn_shortcut_steam.isChecked()
        )
        it.opts = replace(
            it.opts,
            create_menu_shortcut=self.btn_shortcut_menu.isChecked(),
            create_desktop_shortcut=self.btn_shortcut_desktop.isChecked(),
            add_to_steam=add_steam,
        )
        if it.plan is not None:
            it.plan = replace(it.plan, options=it.opts)
        self._render_plan(it)
        self._update_nav()

    def _on_options_changed(self, *_args: object) -> None:
        idx = self._current
        if idx < 0 or idx >= len(self.items):
            return
        it = self.items[idx]
        it.library_root = Path(str(self.cmb_library.currentData())) if self.cmb_library.count() else None
        proton_data = self.cmb_proton.currentData()
        has_steam = it.cand.shortcut_appid is not None
        remove_steam = has_steam and self.rb_steam_remove.isChecked() and self._steam_add_allowed
        self._sync_steam_shortcut_pill(has_steam, remove_steam)
        self._sync_mode_pills()
        it.opts = ad.AdoptOptions(
            move_game=self.chk_move_game.isChecked(),
            move_prefix=self.chk_move_prefix.isChecked(),
            name=it.opts.name,
            exe=it.opts.exe,
            proton_name=None if proton_data == "source" else "",
            create_menu_shortcut=self.btn_shortcut_menu.isChecked(),
            create_desktop_shortcut=self.btn_shortcut_desktop.isChecked(),
            remove_from_steam=remove_steam,
            link_steam_compatdata=not remove_steam,
            add_to_steam=self._paths is not None and self.btn_shortcut_steam.isEnabled()
            and self.btn_shortcut_steam.isChecked(),
        )
        self._replan(idx)
        self._render_plan(it)

    def _on_confirm_folder(self, checked: bool) -> None:
        if 0 <= self._current < len(self.items):
            it = self.items[self._current]
            it.folder_confirmed = checked
            self._refresh_list_marks()
            self._render_plan(it)

    def _pick_game_folder(self) -> None:
        if not 0 <= self._current < len(self.items):
            return
        idx = self._current
        it = self.items[idx]
        start = it.cand.game_dir or it.cand.prefix_dir or Path.home()

        def _on_dir(folder: str) -> None:
            if not folder or not 0 <= idx < len(self.items):
                return
            folder_path = Path(folder)
            cand = ad.candidate_with_folder(self.items[idx].cand, folder_path, None)
            if cand.exe is not None:
                self.set_game_folder(idx, folder_path, cand.exe)
                return

            def _on_exe(picked: str) -> None:
                if picked and 0 <= idx < len(self.items):
                    self.set_game_folder(idx, folder_path, Path(picked))

            _pick_file_async(self, tr("adopt.pick_exe"), folder_path, "", _on_exe)

        _pick_dir_async(self, tr("adopt.pick_game_folder"), start, _on_dir)

    def set_game_folder(self, idx: int, folder: Path, exe: Path | None = None) -> None:
        it = self.items[idx]
        cand = ad.candidate_with_folder(it.cand, folder, exe)
        if cand.exe is None:
            opts = QFileDialog.Option.DontUseNativeDialog if is_gamescope_session() else QFileDialog.Option(0)
            picked, _ = QFileDialog.getOpenFileName(
                self, tr("adopt.pick_exe"), str(folder), options=opts
            )
            if not picked:
                return
            cand = ad.candidate_with_folder(it.cand, folder, Path(picked))
        it.cand = cand
        it.folder_confirmed = True  # the user chose it
        row = self.game_list.item(idx)
        if row is not None:
            row.setText(cand.name)
        self._replan(idx)
        self._show_item(idx)

    # --- page 3: run -----------------------------------------------------------------------

    def _build_run_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.lbl_run = QLabel()
        self.lbl_run.setWordWrap(True)
        lay.addWidget(self.lbl_run)
        self.progress = QProgressBar()
        lay.addWidget(self.progress)
        self.run_list = QListWidget()
        lay.addWidget(self.run_list, 1)
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_summary)
        return page

    def start_run(self) -> None:
        plans = [it.plan for it in self.items if it.ready and it.plan is not None]
        moves = [move for plan in plans for move in plan.moves]
        if not ad._adoption_space(moves)[1]:
            QMessageBox.warning(self, self.windowTitle(), blocker_text(ad.B_NO_SPACE))
            return
        taken: set[Path] = set()
        for plan in plans:
            for move in plan.moves:
                if move.what == "game":
                    destination = unique_dir(move.dst, taken)
                    taken.add(destination)
                    plan.exe = destination / plan.exe.relative_to(plan.game_dir)
                    plan.game_dir = destination
                    move.dst = destination
        self.stack.setCurrentIndex(self.PAGE_RUN)
        self._running = True
        self.run_list.clear()
        for it in self.items:
            if it.ready:
                it.status, it.message = "", tr("adopt.run_waiting")
            else:
                it.status, it.message = "skipped", tr("adopt.run_skipped")
            self.run_list.addItem(QListWidgetItem(f"{it.cand.name}: {it.message}"))
        todo = sum(1 for it in self.items if it.ready)
        self.progress.setRange(0, max(todo, 1))
        self.progress.setValue(0)
        self._run_index = -1
        self._update_nav()
        self._run_next()

    def _set_run_text(self, idx: int, text: str) -> None:
        self.items[idx].message = text
        row = self.run_list.item(idx)
        if row is not None:
            row.setText(f"{self.items[idx].cand.name}: {text}")

    def _run_next(self) -> None:
        nxt = next(
            (i for i in range(self._run_index + 1, len(self.items)) if self.items[i].status == ""),
            -1,
        )
        if nxt < 0 or self._closed:
            self._finish_run()
            return
        self._run_index = nxt
        it = self.items[nxt]
        assert it.plan is not None
        self.lbl_run.setText(tr("adopt.run_current", name=it.cand.name))
        worker = AdoptWorker(it.plan, self._paths, snapshot_fn=self._snapshot_fn)
        worker.progress.connect(self._on_run_progress)
        worker.finished_ok.connect(self._on_run_ok)
        worker.failed.connect(self._on_run_failed)
        self._start(worker)

    def _on_run_progress(self, step: str) -> None:
        if not self._closed and self._run_index >= 0:
            self._set_run_text(self._run_index, step_text(step))

    def _on_run_ok(self, result: object) -> None:
        if self._closed:
            return
        it = self.items[self._run_index]
        it.status = "ok"
        if isinstance(result, ad.AdoptionResult):
            self.adopted.append(result)
            self._set_run_text(self._run_index, tr("adopt.run_ok", path=str(result.game.game_dir)))
        else:
            self._set_run_text(self._run_index, tr("adopt.step_done"))
        self.progress.setValue(self.progress.value() + 1)
        self._run_next()

    def _on_run_failed(self, err: str, rolled_back: bool) -> None:
        if self._closed:
            return
        it = self.items[self._run_index]
        if rolled_back:
            it.status, text = "failed", tr("adopt.run_failed_rolled_back", err=err)
        else:
            it.status, text = "not_undone", tr("adopt.run_failed_not_undone", err=err)
        self._set_run_text(self._run_index, text)
        self.progress.setValue(self.progress.value() + 1)
        self._run_next()

    def _finish_run(self) -> None:
        self._running = False
        ok = [it for it in self.items if it.status == "ok"]
        failed = [it for it in self.items if it.status == "failed"]
        broken = [it for it in self.items if it.status == "not_undone"]
        skipped = [it for it in self.items if it.status == "skipped"]
        lines = [tr("adopt.summary", ok=len(ok), failed=len(failed) + len(broken), skipped=len(skipped))]
        for it in failed + broken:
            lines.append(f"• {it.cand.name}: {it.message}")
        if broken:
            lines.append(tr("adopt.summary_check_manually"))
        self.lbl_run.setText(tr("adopt.run_finished"))
        self.lbl_summary.setText("\n".join(lines))
        self._update_nav()

    # --- navigation ------------------------------------------------------------------------

    def _update_nav(self) -> None:
        page = self.stack.currentIndex()
        self.btn_back.setVisible(page == self.PAGE_PLAN)
        if page == self.PAGE_SOURCES:
            n = len(self.selected_candidates())
            self.btn_next.setText(tr("adopt.btn_next_count", count=n))
            self.btn_next.setEnabled(n > 0)
            self.btn_next.setVisible(True)
            self.btn_cancel.setText(tr("adopt.btn_cancel"))
        elif page == self.PAGE_PLAN:
            pending = any(it.plan is None and not it.plan_error for it in self.items)
            ready = sum(1 for it in self.items if it.ready)
            self.btn_next.setText(tr("adopt.btn_adopt", count=ready))
            self.btn_next.setEnabled(ready > 0 and not pending)
            self.btn_next.setVisible(True)
        else:
            self.btn_next.setVisible(False)
            self.btn_cancel.setText(tr("adopt.btn_close"))
            self.btn_cancel.setEnabled(not self._running)

    def _go_next(self) -> None:
        page = self.stack.currentIndex()
        if page == self.PAGE_SOURCES:
            self.stack.setCurrentIndex(self.PAGE_PLAN)
            self._enter_plan_page()
        elif page == self.PAGE_PLAN:
            self.start_run()
        self._update_nav()

    def _go_back(self) -> None:
        if self.stack.currentIndex() == self.PAGE_PLAN:
            self.stack.setCurrentIndex(self.PAGE_SOURCES)
        self._update_nav()

    def _shutdown(self) -> None:
        self._closed = True

    def reject(self) -> None:
        if self._running:
            return  # an adoption is moving files: wait for it
        self._shutdown()
        if self.adopted:
            super().accept()
        else:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        if self._running:
            event.ignore()
            return
        self._shutdown()
        super().closeEvent(event)


def open_adopt_wizard(
    parent: QWidget | None,
    paths: SteamPaths | None,
    libraries: list[tuple[str, Path]],
    steam_writable: bool,
    steam_note: str = "",
    track_worker: TrackFn | None = None,
    snapshot_fn: ad.SnapshotFn | None = None,
    steam_add_allowed: bool = True,
    steamos: bool = False,
) -> list[ad.AdoptionResult]:
    """Show the wizard modally; return the successful adoptions."""
    dlg = AdoptWizard(
        paths, libraries, steam_writable, steam_note, track_worker, parent, snapshot_fn, steam_add_allowed,
        steamos,
    )
    try:
        dlg.exec()
        return list(dlg.adopted)
    finally:
        dlg.deleteLater()
