"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn, TypedDict, cast

from PySide6.QtCore import (
    QFileSystemWatcher,
    QLibraryInfo,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QTranslator,
    QUrl,
    qInstallMessageHandler,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__, classify, vdf_queue
from .app_updater import (
    GITHUB_RELEASES_URL,
    AppUpdateInfo,
    relaunch_appimage,
)
from .artwork import (
    SteamMatch,
)
from .external_actions import (
    build_steam_shortcut_payload,
    create_steam_shortcut_from_external,
    load_external_launch_presets,
)
from .i18n import LOCALES_DIR, available_languages, set_language, tr
from .launch_presets import active_flags, apply_presets
from .launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from .library import (
    Library,
    default_mount_search_roots,
    discover_libraries,
    disks_without_library,
    ensure_library,
    internal_library_root,
    list_unavailable_games,
)
from .migrate import (
    MigrationItem,
    MigrationResult,
    migrate_legacy_games,
    plan_migration,
)
from .play_monitor import ERROR, LOADING, RUNNING, PlayMonitor, process_start_epoch, stale_instance_pids, watch_paths
from .runtime_manager import (
    detect_gpu_profile,
    fix_bwrap_userns,
    get_umu_run_path,
    is_win32_prefix,
    list_ge_proton11_dirs,
)
from .save_sync import auto_snapshot
from .scanner import (
    DetectedGame,
    default_scan_roots,
    is_native_linux_exe,
    is_proton_exe,
    scan_all_games,
    scan_game_folder,
)
from .standalone import (
    StandaloneGame,
    add_installed_game_to_steam,
    game_log_dir,
    get_games_dir,
    import_standalone_to_steam,
    list_standalone_games,
    match_owned_shortcuts,
    primary_owned_shortcuts,
    repaired_shortcut_start_dir,
    shortcut_game_dir,
    shortcut_path_repairs,
    uses_library_layout,
)
from .steam_paths import (
    SteamPaths,
    find_steam_root,
    is_gamescope_session,
    is_steam_running,
    resolve_steam_paths,
)
from .steam_vdf import (
    DEFAULT_SAVES_DIR,
    CleanupCandidate,
    ShortcutEntry,
    _strip_vdf_quotes,
    infer_game_root_from_exe,
    load_shortcuts,
    start_steam,
)
from .theme import ThemeMode, apply_theme
from .touch_scroll import install_touch_scroll
from .ui_cards import (  # noqa: F401
    DropZoneFrame,
    FlowLayout,
    _dialog_parent,
    _is_native_linux_game,
    _is_wine_game_running,
    _make_card,
    _pick_dir_async,
    _pick_file_async,
    _resolve_asset_path,
    _setting_bool,
    format_size_bytes,
    get_game_log_path,
    shortcut_target_button,
    show_text_dialog,
)
from .ui_cards import (
    _parse_launch_preset_state as _raw_parse_launch_preset_state,
)
from .ui_dialogs import (
    bulk_proton_dialog,
    edit_launch_options_presets,
    open_configure_game_dialog,
    open_launch_presets_dialog,
    open_optiscaler_dialog,
    open_save_sync_dialog,
    open_scan_folders_dialog,
    open_winetricks_dialog,
    run_exe_in_prefix_dialog,
)
from .ui_dialogs.adopt_wizard import open_adopt_wizard
from .ui_dialogs.relocate_dialog import move_game_to_disk
from .ui_dialogs.save_sync_dialog import (
    AUTO_SNAPSHOT_KEY,
    build_saves_page,
    offer_existing_saves,
    refresh_saves_page,
    save_row_buttons,
    snapshot_keep,
    update_saves_action_states,
)
from .ui_dialogs.uninstall_dialog import ask_uninstall
from .ui_maintenance import (
    _update_maint_summary,
    build_maintenance_page,
    refresh_maintenance_health,
)
from .ui_workers import (  # noqa: F401
    AddToSteamWorker,
    AppUpdateCheckWorker,
    AppUpdateDownloadWorker,
    BackupWorker,
    DirStatsWorker,
    ExtImportSavesWorker,
    ExtractWorker,
    LaunchPrepWorker,
    PkexecWorker,
    PreviewWorker,
    RemoveShortcutWorker,
    RestoreWorker,
    RuntimePreflightWorker,
    RuntimeUpdateWorker,
    ScanWorker,
    ShortcutsRefreshWorker,
    StandaloneInstallWorker,
    SteamDetectWorker,
    SteamOpWorker,
    SteamRunningWorker,
    SteamSearchWorker,
    compute_library_runner_labels,
)
from .uninstall import GAME_FILES, SHORTCUTS, UninstallPlan, UninstallResult, execute_uninstall, plan_uninstall
from .vdf_queue import BatchApplyResult, QueuedVdfEdit, VdfEditQueue


class LaunchPresetState(TypedDict):
    tokens_list: list[str]
    tokens_now: set[str]
    gamemode: bool
    mangohud: bool
    mh_level: int
    mh_position: str
    mh_fps_limit: int
    nvapi: bool
    prime: bool
    wayland: bool
    dlss_upgrade: bool
    wined3d: bool
    no_esync: bool
    no_fsync: bool
    winedll: str


# Boolean presets remembered for the next install: preset key -> QSettings name.
_VDF_WATCH_MS = 5000  # how often queued Steam edits check whether Steam has closed

_LAST_PREFS_FLAGS = {
    "gamemode": "gamemode",
    "mangohud": "mangohud",
    "nvapi": "nvapi",
    "prime": "prime",
    "wayland": "wayland",
    "dlss_upgrade": "dlss_upgrade",
    "wined3d": "wined3d",
    "no_esync": "no_esync",
    "no_fsync": "no_fsync",
}


def _parse_launch_preset_state(opts: str) -> LaunchPresetState:
    return cast(LaunchPresetState, _raw_parse_launch_preset_state(opts))


def _inside_any(path: Path, dirs: list[str]) -> bool:
    p = os.path.normpath(str(path))
    return any(p == d or p.startswith(d + os.sep) for d in dirs)


def _setting_int(settings: QSettings, key: str, default: int = 0) -> int:
    val = settings.value(key, default)
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return default
    return default


PAGE_INSTALL, PAGE_LIBRARY, PAGE_SAVES, PAGE_MAINT = range(4)


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    _maint_candidates: list[CleanupCandidate]
    _maint_checked: dict[Path, bool]
    _maint_active_filter: str
    _maint_scanned: bool
    _maint_filter_group: QButtonGroup
    maint_list: QTableWidget
    btn_migrate_legacy: QPushButton
    btn_maint_refresh_health: QPushButton
    btn_maint_scan: QPushButton
    btn_maint_revalidate: QPushButton
    btn_maint_toggle_filter: QPushButton
    btn_maint_clean_safe: QPushButton
    btn_maint_delete_selected: QPushButton
    lbl_maint_app: QLabel
    lbl_maint_umu: QLabel
    lbl_maint_proton: QLabel
    lbl_maint_steamrt4: QLabel
    lbl_maint_mangohud: QLabel
    lbl_maint_disk: QLabel
    lbl_maint_summary: QLabel
    btn_update_runtimes: QPushButton
    btn_uninstall_runtimes: QPushButton
    btn_app_update: QPushButton
    # Backup/Sync page (ui_dialogs/save_sync_dialog.py)
    saves_list: QTableWidget
    lbl_saves_empty: QLabel
    spin_save_keep: QSpinBox
    btn_save_backup: QPushButton
    btn_save_restore: QPushButton
    btn_save_sync: QPushButton
    btn_save_dedup: QPushButton
    btn_save_refresh: QPushButton
    btn_save_snapshot_now: QPushButton
    btn_save_restore_selected: QPushButton
    btn_save_open_folder: QPushButton
    btn_save_export: QPushButton
    btn_save_import: QPushButton
    btn_save_import_one: QPushButton
    _after_play_running: bool = False
    _save_sources_running: bool = False
    _save_sources_pending: bool = False
    _external_save_targets: list = []  # Steam/Hydra/UMU/Nexus games for Backup/Sync (filled by a worker)
    _save_sizes: dict = {}  # slug -> bytes of saves right now

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings(__app_name__, __app_name__)
        _install_qt_translator(app, set_language(str(self.settings.value("language", "auto"))))
        self.paths: SteamPaths | None = None
        self._steam_root_found = False
        self._gpu_profile_cache: object | None = None
        self._detected_games: list[DetectedGame] = []
        self._standalone_games: list[StandaloneGame] = []
        self._library_steam_games: dict[int, StandaloneGame] = {}
        self._external_games: list[ExternalLauncherGame] = []
        self._steam_shortcuts: list[ShortcutEntry] = []
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._closed = False
        self._relaunch_after_close: Path | None = None
        self._force_closing = False
        self._close_force_timer: QTimer | None = None
        self._busy_count = 0
        self._vdf_corrupt = False
        self._migration_prompted = False
        self._install_probe = 0
        self._vdf_queue = VdfEditQueue(vdf_queue.default_queue_file())
        self._vdf_steam_open = False  # last poll saw Steam running while edits were queued
        self._vdf_checking = False
        self._vdf_applying = False
        self._vdf_auto_paused = False  # an automatic apply failed: wait for the user
        self._skip_vdf_close_prompt = False  # set when restarting into an update: the queue stays saved
        self._restart_prompt: QMessageBox | None = None
        self._vdf_watch_timer = QTimer(self)
        self._vdf_watch_timer.setInterval(_VDF_WATCH_MS)
        self._vdf_watch_timer.timeout.connect(self._vdf_watch_tick)
        self._auto_art_timer = QTimer(self)
        self._auto_art_timer.setSingleShot(True)
        self._auto_art_timer.setInterval(350)
        self._auto_art_timer.timeout.connect(self._auto_search_steam_art)
        self._search_req_id = 0
        self._preview_req_id = 0
        self._maint_health_req_id = 0
        self._shortcuts_req_id = 0
        self._steam_detect_req_id = 0
        self._extract_worker: ExtractWorker | None = None
        self._add_worker: AddToSteamWorker | None = None
        self._scan_worker: ScanWorker | None = None
        self._play = PlayMonitor(self)
        self._play.changed.connect(self._on_play_changed)
        self._play.failed.connect(self._on_play_failed)
        self._play.timed_out.connect(self._on_play_timed_out)

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1080, 700)
        self.setMinimumSize(840, 560)
        self._build_ui()
        self._setup_mount_watcher()
        self._refresh_libraries()
        self._apply_saved_theme()
        self._refresh_games()
        self._redetect_steam()

    def _track_worker(self, worker: QThread) -> None:
        """Keep a strong reference to running QThreads until finished to prevent Qt SIGABRT."""
        worker.setParent(self)
        self._active_workers.add(worker)

        def _cleanup() -> None:
            self._active_workers.discard(worker)
            for attr in (
                "_extract_worker",
                "_add_worker",
                "_scan_worker",
            ):
                if getattr(self, attr, None) is worker:
                    setattr(self, attr, None)
            worker.deleteLater()
            if self._closing_pending and not any(w.isRunning() for w in self._active_workers):
                if self._close_force_timer is not None:
                    self._close_force_timer.stop()
                self._closing_pending = False
                self.close()

        worker.finished.connect(_cleanup)
        if hasattr(worker, "closed_steam") or hasattr(worker, "restart_failed"):
            worker.finished.connect(lambda: self._maybe_offer_steam_restart(worker))

    def _get_gpu_profile(self):
        """Return cached GPU profile to avoid repeated subprocess/sysfs detection."""
        if self._gpu_profile_cache is None:
            self._gpu_profile_cache = detect_gpu_profile()
        return self._gpu_profile_cache

    def _on_close_force_timeout(self) -> None:
        if not self._closing_pending:
            return
        running = [w for w in list(self._active_workers) if w.isRunning()]
        if not running:
            self._closing_pending = False
            self.close()
            return
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("status.closing_force_prompt"),
        )
        if ans == QMessageBox.StandardButton.Yes:
            for w in list(self._active_workers):
                if w.isRunning():
                    w.terminate()
                    w.wait(500)
            self._force_closing = True
            self._closing_pending = False
            self.close()
        elif self._closing_pending and any(w.isRunning() for w in self._active_workers):
            if self._close_force_timer is not None:
                self._close_force_timer.start(5000)

    def _defer_close_for_workers(self, event: QCloseEvent) -> None:
        """Keep the window open while background workers finish and arm the force-close timer."""
        self._closing_pending = True
        self.status_label.setText(tr("status.closing_wait"))
        if self._close_force_timer is None:
            self._close_force_timer = QTimer(self)
            self._close_force_timer.setSingleShot(True)
            self._close_force_timer.timeout.connect(self._on_close_force_timeout)
        if not self._close_force_timer.isActive():
            self._close_force_timer.start(5000)
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            super().closeEvent(event)
            return
        if self._force_closing:
            self._closed = True
            self._play.release()
            super().closeEvent(event)
            return
        if self._vdf_applying:
            # Never cancel or double-prompt while an async VDF queue write is in flight.
            self._defer_close_for_workers(event)
            return
        for w in list(self._active_workers):
            if hasattr(w, "cancel") and callable(w.cancel):
                w.cancel()
            else:
                w.requestInterruption()
        if any(w.isRunning() for w in self._active_workers):
            self._defer_close_for_workers(event)
            return
        if (
            self._vdf_queue.pending_count() > 0
            and self.paths is not None
            and not self._skip_vdf_close_prompt
            and not is_gamescope_session()
        ):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(__app_name__)
            box.setText(tr("vdf_queue.close_prompt", count=self._vdf_queue.pending_count()))
            btn_apply = box.addButton(tr("vdf_queue.btn_apply_exit"), QMessageBox.ButtonRole.AcceptRole)
            btn_later = box.addButton(tr("vdf_queue.btn_later_exit"), QMessageBox.ButtonRole.RejectRole)
            btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(btn_apply)
            box.exec()
            clicked = box.clickedButton()
            box.deleteLater()
            if clicked == btn_cancel or clicked not in (btn_apply, btn_later):
                event.ignore()
                return
            if clicked == btn_apply:
                if self.paths is None:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.steam_missing"))
                    event.ignore()
                    return
                event.ignore()

                def _after_apply(ok: bool) -> None:
                    if ok and self._vdf_queue.pending_count() == 0:
                        self._skip_vdf_close_prompt = True
                        self.close()

                self._apply_vdf_queue(on_done=_after_apply)
                return
            self._skip_vdf_close_prompt = True
        if self._close_force_timer is not None:
            self._close_force_timer.stop()
        self._auto_art_timer.stop()
        self._vdf_watch_timer.stop()
        self._play.release()  # stop following running games; they keep running
        self._closed = True
        super().closeEvent(event)
        updated_path, self._relaunch_after_close = self._relaunch_after_close, None
        if updated_path is not None:
            try:
                relaunch_appimage(updated_path)
            except OSError:
                logging.exception("Could not relaunch updated AppImage")

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("ContentArea")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(22, 20, 22, 14)
        right_layout.setSpacing(12)

        def _wrap_scroll(w: QWidget) -> QScrollArea:
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setWidget(w)
            return sa

        def _subpage(w: QWidget) -> QWidget:
            """A page opened from the Library: "← Library" on top, the page scrolls below it."""
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            back = QPushButton(tr("nav.back"), page)
            back.setObjectName("BackBtn")
            back.clicked.connect(lambda: self._show_page(PAGE_LIBRARY))
            row = QHBoxLayout()
            row.addWidget(back)
            row.addStretch()
            lay.addLayout(row)
            lay.addWidget(_wrap_scroll(w), 1)
            return page

        # Library is the hub; Install, Backup/Sync and Maintenance open from its buttons.
        self.stack = QStackedWidget()
        self.stack.addWidget(_subpage(self._build_extract_page()))  # PAGE_INSTALL
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))  # PAGE_LIBRARY
        self.stack.addWidget(_subpage(self._build_saves_page()))  # PAGE_SAVES
        self.stack.addWidget(_subpage(self._build_maintenance_page()))  # PAGE_MAINT
        self.stack.setCurrentIndex(PAGE_LIBRARY)  # always open on the Library (no jump after the scan)
        self.stack.currentChanged.connect(self._on_stack_tab_changed)
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        right_layout.addWidget(self.status_label)

        shell.addWidget(right)
        self.setCentralWidget(central)
        self._update_steam_mode_ui()

    def _build_app_settings_card(self) -> QWidget:
        """Maintenance card with what used to be the sidebar: Steam status, theme, language and About."""
        card, layout = _make_card()
        title = QLabel(tr("maint.app_settings_title"))
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        steam_row = QHBoxLayout()
        self.steam_status_lbl = QLabel(self._format_steam_status_text())
        self.steam_status_lbl.setWordWrap(True)
        self.btn_redetect_steam = QPushButton(tr("sidebar.redetect_steam"))
        self.btn_redetect_steam.clicked.connect(self._redetect_steam)
        steam_row.addWidget(self.steam_status_lbl, 1)
        steam_row.addWidget(self.btn_redetect_steam)
        layout.addLayout(steam_row)

        prefs = FlowLayout(spacing=8)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem(tr("header.theme.auto"), "auto")
        self.theme_combo.addItem(tr("header.theme.dark"), "dark")
        self.theme_combo.addItem(tr("header.theme.light"), "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(tr("header.language.auto"), "auto")
        for code, disp in available_languages():
            self.lang_combo.addItem(disp, code)
        saved_lang = str(self.settings.value("language", "auto"))
        lidx = self.lang_combo.findData(saved_lang)
        if lidx >= 0:
            self.lang_combo.setCurrentIndex(lidx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        btn_about = QPushButton(tr("header.about"))
        btn_about.clicked.connect(self._show_about_dialog)
        for label, combo in ((tr("header.theme"), self.theme_combo), (tr("header.language"), self.lang_combo)):
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label))
            row.addWidget(combo)
            prefs.addWidget(box)
        prefs.addWidget(btn_about)
        layout.addLayout(prefs)
        self._update_steam_status_style()
        return card

    def _update_steam_status_style(self) -> None:
        """Apply subtle semantic color to the sidebar Steam status label."""
        name = "SteamReady" if self.paths else ("SteamWarning" if self._steam_root_found else "SteamUnavailable")
        self.steam_status_lbl.setObjectName(name)
        style = self.steam_status_lbl.style()
        style.unpolish(self.steam_status_lbl)
        style.polish(self.steam_status_lbl)

    def _show_page(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)

    def _on_stack_tab_changed(self, idx: int) -> None:
        """Refresh Saves list or Maintenance metrics when their page opens."""
        if idx == PAGE_SAVES:
            self._refresh_saves_list()
        elif idx == PAGE_MAINT:
            refresh_maintenance_health(self)

    def _format_steam_status_text(self) -> str:
        """Return localized sidebar status text for the current Steam detection state."""
        if self.paths:
            return tr("status.steam_id", id=self.paths.user_id)
        if self._steam_root_found:
            return tr("status.steam_needs_login")
        return tr("status.steam_not_detected")

    def _redetect_steam(self) -> None:
        """Re-scan Steam paths in a background worker and update all adaptive UI states."""
        if self._closing_pending:
            return
        self._steam_detect_req_id += 1
        req_id = self._steam_detect_req_id
        self._shortcuts_req_id += 1
        shortcuts_req_id = self._shortcuts_req_id

        def _on_detected(
            r_id: int,
            sc_req_id: int,
            paths: SteamPaths | None,
            steam_root_found: bool,
            entries: list[ShortcutEntry],
            standalone_games: list[StandaloneGame],
            external_games: list[ExternalLauncherGame],
            vdf_err: str,
            runner_labels: dict[tuple[str, str], str],
        ) -> None:
            if self._closing_pending or r_id != self._steam_detect_req_id:
                return
            self.paths = paths
            self._steam_root_found = steam_root_found
            self._on_vdf_queue_paths_ready()
            status_text = self._format_steam_status_text()
            self.steam_status_lbl.setText(status_text)
            self._update_steam_status_style()
            self._update_steam_mode_ui()
            self.status_label.setText(status_text)
            if sc_req_id == self._shortcuts_req_id:
                self._apply_shortcuts_data(
                    paths,
                    entries,
                    standalone_games,
                    external_games,
                    vdf_err,
                    runner_labels=runner_labels,
                )

        worker = SteamDetectWorker(
            req_id,
            shortcuts_req_id,
            resolve_steam_paths_fn=resolve_steam_paths,
            find_steam_root_fn=find_steam_root,
            load_shortcuts_fn=load_shortcuts,
            list_standalone_games_fn=list_standalone_games,
            scan_external_launcher_games_fn=scan_external_launcher_games,
        )
        self._track_worker(worker)
        worker.ready.connect(_on_detected)
        worker.failed.connect(self._on_background_failed)
        worker.start()

    def _update_steam_mode_ui(self) -> None:
        """Adapt Add/Extract/Library/Maintenance controls for Steam vs Standalone-only mode."""
        has_steam = self.paths is not None
        self.add_no_steam_banner.setVisible(not has_steam)
        self.btn_add_steam.setVisible(has_steam)
        self.btn_shortcut_steam.setVisible(has_steam)
        if not has_steam:
            self.btn_shortcut_steam.setChecked(False)
        self.lbl_steam_opts_group.setVisible(has_steam)
        self.chk_proton.setVisible(has_steam)
        self.chk_restart.setVisible(has_steam)
        self.btn_add_steam.setObjectName("primaryBtn" if has_steam else "")
        self.btn_add_standalone.setObjectName("" if has_steam else "primaryBtn")
        for btn in (self.btn_add_steam, self.btn_add_standalone):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self.btn_bulk_proton.setVisible(True)
        if hasattr(self, "btn_maint_revalidate"):
            self.btn_maint_revalidate.setEnabled(has_steam)
        self._update_steam_status_style()

    def _build_extract_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel(tr("install.page_title"))
        title.setObjectName("PageTitle")
        sub = QLabel(tr("install.page_subtitle"))
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        drop_card = DropZoneFrame()
        drop_card.file_dropped.connect(self._set_extract_source)
        drop_layout = QVBoxLayout(drop_card)
        drop_layout.setContentsMargins(18, 16, 18, 16)
        drop_layout.setSpacing(10)

        src_lbl = QLabel(tr("install.source_title"))
        src_lbl.setObjectName("SectionTitle")
        drop_layout.addWidget(src_lbl)

        src_row = QHBoxLayout()
        self.extract_src = QLineEdit()
        self.extract_src.setPlaceholderText(tr("install.source_placeholder"))
        btn_src = QPushButton(tr("install.browse"))
        btn_src.clicked.connect(self._pick_archive_or_installer)
        src_row.addWidget(self.extract_src, 1)
        src_row.addWidget(btn_src)
        drop_layout.addLayout(src_row)

        # Only used for archives and installers; the game name comes from step 2.
        dst_row = QHBoxLayout()
        dst_row.addWidget(QLabel(tr("install.dest_label")))
        self.extract_dst_root = QLineEdit()
        self.extract_dst_root.setPlaceholderText(
            tr("install.dest_library_placeholder") if uses_library_layout() else str(Path.home() / "Games")
        )
        btn_dst = QPushButton(tr("install.choose_folder"))
        btn_dst.clicked.connect(self._pick_dest_root)
        dst_row.addWidget(self.extract_dst_root, 1)
        dst_row.addWidget(btn_dst)
        drop_layout.addLayout(dst_row)

        btn_row = FlowLayout(spacing=8)
        self.btn_extract = QPushButton(tr("install.btn_extract"))
        self.btn_extract.setObjectName("primaryBtn")
        self.btn_extract.clicked.connect(self._start_extract)
        self.btn_cancel_extract = QPushButton(tr("install.btn_cancel"))
        self.btn_cancel_extract.setEnabled(False)
        self.btn_cancel_extract.clicked.connect(self._cancel_extract)
        self.btn_use_extracted = QPushButton(tr("install.btn_continue_add"))
        self.btn_use_extracted.setEnabled(False)
        self.btn_use_extracted.clicked.connect(self._send_extracted_to_add_tab)
        btn_row.addWidget(self.btn_extract)
        btn_row.addWidget(self.btn_cancel_extract)
        btn_row.addWidget(self.btn_use_extracted)
        drop_layout.addLayout(btn_row)


        # Progress and log appear in the Source step once an extraction/setup starts.
        self.extract_details_box = QWidget()
        self.extract_details_box.setVisible(False)
        details_lay = QVBoxLayout(self.extract_details_box)
        details_lay.setContentsMargins(0, 4, 0, 0)
        details_lay.setSpacing(8)

        self.extract_progress = QProgressBar()
        details_lay.addWidget(self.extract_progress)

        self.extract_log = QTextEdit()
        self.extract_log.setReadOnly(True)
        self.extract_log.setMaximumHeight(140)
        self.extract_log.setPlaceholderText(tr("install.log_placeholder"))
        details_lay.addWidget(self.extract_log)
        drop_layout.addWidget(self.extract_details_box)

        layout.addWidget(drop_card)
        layout.addWidget(self._build_add_page(), 1)
        return page

    def _build_add_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.add_no_steam_banner = QLabel(tr("add.no_steam_banner"))
        self.add_no_steam_banner.setWordWrap(True)
        self.add_no_steam_banner.setObjectName("SteamInfoBanner")
        self.add_no_steam_banner.setVisible(False)
        layout.addWidget(self.add_no_steam_banner)

        list_card, list_layout = _make_card()
        scan_row = QHBoxLayout()
        lbl_det = QLabel(tr("install.detected_title"))
        lbl_det.setObjectName("SectionTitle")
        self.btn_scan = QPushButton(tr("install.btn_rescan"))
        self.btn_scan.clicked.connect(self._refresh_games)
        self.btn_scan_folders = QPushButton(tr("scan_folders.btn"))
        self.btn_scan_folders.clicked.connect(self._open_scan_folders_dialog)
        self.lbl_scan_folders_count = QLabel()
        self.lbl_scan_folders_count.setObjectName("MutedText")
        self._update_scan_folders_label()
        btn_browse_exe = QPushButton(tr("install.browse"))
        btn_browse_exe.clicked.connect(self._pick_manual_exe)
        scan_row.addWidget(lbl_det)
        scan_row.addStretch()
        scan_row.addWidget(self.lbl_scan_folders_count)
        scan_row.addWidget(self.btn_scan_folders)
        scan_row.addWidget(self.btn_scan)
        scan_row.addWidget(btn_browse_exe)
        list_layout.addLayout(scan_row)

        self.games_list = QTableWidget(0, 2)
        self.games_list.setHorizontalHeaderLabels(
            [tr("add.detected_name_column"), tr("add.detected_folder_column")]
        )
        self.games_list.verticalHeader().setVisible(False)
        self.games_list.setMinimumHeight(120)
        self.games_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.games_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.games_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.games_list.setSortingEnabled(True)
        self.games_list.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.games_list.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.games_list.horizontalHeader().setStretchLastSection(True)
        self.games_list.horizontalHeader().setSectionsClickable(True)
        self.games_list.currentCellChanged.connect(
            lambda row, _column, _previous_row, _previous_column: self._on_game_selected(row)
        )
        list_layout.addWidget(self.games_list, 1)

        # Step 2 (Game): exe, name, Steam match, where/how to install and the shortcuts, all in one place.
        cfg_split = QSplitter(Qt.Orientation.Horizontal)
        cfg_split.setChildrenCollapsible(False)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(10)

        self._match_install_timer = QTimer(self)
        self._match_install_timer.setSingleShot(True)
        self._match_install_timer.setInterval(180)
        self._match_install_timer.timeout.connect(self._update_remove_existing_state)
        self.add_exe_combo = QComboBox()
        self.add_exe_combo.setEditable(True)
        self.add_exe_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_exe_combo.setMinimumContentsLength(24)
        self.add_exe_combo.currentTextChanged.connect(self._on_exe_changed)
        form.addRow(tr("install.exe_label"), self.add_exe_combo)

        self.add_name = QLineEdit()
        self.add_name.setPlaceholderText(tr("install.dest_placeholder"))
        self.add_name.textChanged.connect(lambda: self._match_install_timer.start())
        self.add_name.textEdited.connect(self._on_add_name_edited)
        form.addRow(tr("install.folder_label"), self.add_name)

        self.steam_match_combo = QComboBox()
        self.steam_match_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.steam_match_combo.setMinimumContentsLength(24)
        self.steam_match_combo.currentIndexChanged.connect(self._on_steam_match_changed)
        form.addRow(tr("add.match_label"), self.steam_match_combo)

        art_row = QHBoxLayout()
        self.steam_search_input = QLineEdit()
        self.steam_search_input.setPlaceholderText(tr("install.game_name_placeholder"))
        self.steam_search_input.returnPressed.connect(self._search_steam_art)
        btn_search = QPushButton(tr("install.search_steam"))
        btn_search.clicked.connect(self._search_steam_art)
        art_row.addWidget(self.steam_search_input, 1)
        art_row.addWidget(btn_search)
        form.addRow(tr("add.steam_store_label"), art_row)

        self.add_library_combo = QComboBox()
        self.add_library_combo.setToolTip(tr("add.library_tooltip"))
        self.add_library_combo.currentIndexChanged.connect(self._remember_library_choice)
        form.addRow(tr("add.library"), self.add_library_combo)
        if not uses_library_layout():
            self.add_library_combo.setVisible(False)
            label_item = form.labelForField(self.add_library_combo)
            if label_item is not None:
                label_item.setVisible(False)

        self.chk_keep_in_place = QCheckBox(tr("add.keep_in_place"))
        self.chk_keep_in_place.setChecked(False)
        form.addRow("", self.chk_keep_in_place)

        self._add_launch_options = ""
        self._add_winedlloverrides = ""
        # One presets dialog for everything; the form only shows what is on.
        launch_row = QHBoxLayout()
        launch_row.setSpacing(10)
        self.btn_add_launch_adv = QPushButton(tr("add.btn_launch_presets"))
        self.btn_add_launch_adv.clicked.connect(self._open_add_launch_advanced)
        self.lbl_add_presets = QLabel()
        self.lbl_add_presets.setObjectName("MutedText")
        self.lbl_add_presets.setWordWrap(True)
        launch_row.addWidget(self.btn_add_launch_adv)
        launch_row.addWidget(self.lbl_add_presets, 1)
        form.addRow(tr("add.presets_label"), launch_row)

        targets_row = FlowLayout(spacing=8)
        self.btn_shortcut_menu = shortcut_target_button(tr("add.shortcut_menu"), selected=True)
        self.btn_shortcut_desktop = shortcut_target_button(tr("add.shortcut_desktop"))
        self.btn_shortcut_steam = shortcut_target_button(tr("add.shortcut_steam"))
        self.btn_shortcut_steam.setVisible(self.paths is not None)
        for button in (self.btn_shortcut_menu, self.btn_shortcut_desktop, self.btn_shortcut_steam):
            targets_row.addWidget(button)
        form.addRow(tr("add.shortcuts_label"), targets_row)

        self.lbl_steam_opts_group = QLabel(tr("add.group_steam_opts"))
        self.lbl_steam_opts_group.setObjectName("MutedText")
        self.lbl_steam_opts_group.setStyleSheet("font-weight: 700; margin-top: 4px;")
        form.addRow(self.lbl_steam_opts_group)
        self.chk_proton = QCheckBox(tr("install.enable_proton"))
        self.chk_proton.setChecked(True)
        form.addRow("", self.chk_proton)
        self.chk_restart = QCheckBox(tr("add.reopen_steam"))
        self.chk_restart.setChecked(True)
        self.chk_restart.setToolTip(tr("add.reopen_steam_tooltip"))
        form.addRow("", self.chk_restart)

        cfg_split.addWidget(form_widget)

        preview_wrap = QWidget()
        preview_lay = QVBoxLayout(preview_wrap)
        preview_lay.setContentsMargins(8, 0, 0, 0)
        self.preview_label = QLabel(tr("install.art_preview_empty"))
        self.preview_label.setObjectName("PreviewBox")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumSize(180, 108)
        preview_lay.addWidget(self.preview_label)
        preview_lay.addStretch()
        cfg_split.addWidget(preview_wrap)
        cfg_split.setStretchFactor(0, 1)
        cfg_split.setStretchFactor(1, 0)
        cfg_split.setSizes([700, 240])
        list_layout.addWidget(cfg_split)

        actions_row = FlowLayout(spacing=8)
        self.btn_add_steam = QPushButton(tr("install.btn_add_steam"))
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        actions_row.addWidget(self.btn_add_steam)

        self.btn_add_standalone = QPushButton(tr("install.btn_add_standalone"))
        self.btn_add_standalone.clicked.connect(self._add_selected_standalone)
        actions_row.addWidget(self.btn_add_standalone)
        self.chk_keep_in_place.toggled.connect(self._update_standalone_install_label)
        self._update_standalone_install_label()

        self.btn_remove_existing = QPushButton(tr("add.btn_remove_existing"))
        self.btn_remove_existing.setObjectName("dangerBtn")
        self.btn_remove_existing.clicked.connect(self._remove_existing_from_add)
        self.btn_remove_existing.setVisible(False)
        actions_row.addWidget(self.btn_remove_existing)
        list_layout.addLayout(actions_row)
        layout.addWidget(list_card)
        self._reset_add_launch_options()

        layout.addStretch(1)
        return page

    def _setup_mount_watcher(self) -> None:
        """Refresh libraries when a disk is mounted or removed."""
        self._mount_refresh_timer = QTimer(self)
        self._mount_refresh_timer.setSingleShot(True)
        self._mount_refresh_timer.setInterval(1500)
        self._mount_refresh_timer.timeout.connect(self._refresh_shortcuts)
        self._mount_watcher = QFileSystemWatcher(self)
        dirs = [str(d) for d in default_mount_search_roots() if d.is_dir()]
        if dirs:
            self._mount_watcher.addPaths(dirs)
        self._mount_watcher.directoryChanged.connect(lambda _p: self._mount_refresh_timer.start())

    def _refresh_libraries(self) -> None:
        """Rebuild the install-target library list and the absent-disk banner."""
        if not hasattr(self, "add_library_combo"):
            return
        try:
            libs = discover_libraries()
            disks = disks_without_library()
            unavailable = list_unavailable_games(libs)
        except Exception as exc:
            logging.warning("Library discovery failed: %s", exc)
            libs, disks, unavailable = [], [], []

        combo = self.add_library_combo
        wanted = str(self.settings.value("install/library", "") or "")
        combo.blockSignals(True)
        combo.clear()
        internal_root = internal_library_root()
        if not any(lib.internal and lib.available for lib in libs):
            combo.addItem(tr("add.library_internal", path=str(internal_root)), f"lib:{internal_root}")
        for lib in libs:
            if not lib.available:
                continue
            if lib.internal:
                text = tr("add.library_internal", path=str(lib.root))
            elif lib.unix_fs:
                text = tr("add.library_disk", label=lib.label, path=str(lib.root))
            else:
                text = tr("add.library_disk_nounix", label=lib.label, path=str(lib.root))
            combo.addItem(text, f"lib:{lib.root}")
        for mount in disks:
            combo.addItem(tr("add.library_create", label=mount.name, path=str(mount)), f"disk:{mount}")
        idx = combo.findData(wanted) if wanted else -1
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

        if unavailable:
            names = ", ".join(f"{g['name']} ({g['library']})" for g in unavailable[:8])
            if len(unavailable) > 8:
                names += ", ..."
            self.unavailable_banner.setText(tr("library.unavailable_banner", count=len(unavailable), names=names))
            self.unavailable_banner.show()
        else:
            self.unavailable_banner.hide()

    def _remember_library_choice(self, _idx: int) -> None:
        data = self.add_library_combo.currentData()
        if isinstance(data, str) and data:
            self.settings.setValue("install/library", data)

    def _selected_library(self) -> tuple[bool, Library | None]:
        """Return (ok, library) for the install target; creates the library folder when needed."""
        if not uses_library_layout():
            return True, None
        data = str(self.add_library_combo.currentData() or "")
        kind, _, raw = data.partition(":")
        try:
            if kind == "disk" and raw:
                lib = ensure_library(Path(raw) / "DeckInstall", label=Path(raw).name)
            elif kind == "lib" and raw:
                root = Path(raw)
                lib = ensure_library(root, internal=root == internal_library_root())
            else:
                lib = ensure_library(internal_library_root(), internal=True)
        except OSError as exc:
            QMessageBox.critical(_dialog_parent(self), __app_name__, tr("add.library_error", err=str(exc)))
            return False, None
        if kind == "disk":
            self.settings.setValue("install/library", f"lib:{lib.root}")
        if not lib.unix_fs:
            QMessageBox.warning(
                _dialog_parent(self),
                __app_name__,
                tr("add.library_nounix_warn", label=lib.label, fs=lib.fs_type or "?"),
            )
        return True, lib

    def _update_standalone_install_label(self) -> None:
        text = (
            tr("install.btn_add_standalone_in_place")
            if self.chk_keep_in_place.isChecked()
            else tr("install.btn_add_standalone")
        )
        self.btn_add_standalone.setText(text)

    def _switch_library_subview(self, idx: int) -> None:
        """Switch to unified Install / Add Game view (Tab 0) and focus game name."""
        if idx == 1:
            self._show_page(PAGE_INSTALL)
            self.add_name.setFocus()

    def _build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        my_lib_page = QWidget()
        my_lib_layout = QVBoxLayout(my_lib_page)
        my_lib_layout.setContentsMargins(0, 0, 0, 0)
        my_lib_layout.setSpacing(14)

        title = QLabel(tr("library.page_title"))
        title.setObjectName("PageTitle")
        sub = QLabel(tr("library.page_subtitle"))
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        my_lib_layout.addWidget(title)
        my_lib_layout.addWidget(sub)

        hub = QHBoxLayout()
        hub.setSpacing(10)
        self._hub_buttons: list[QPushButton] = []
        for label, idx in (
            (tr("nav.install"), PAGE_INSTALL),
            (tr("nav.saves"), PAGE_SAVES),
            (tr("nav.maintenance"), PAGE_MAINT),
        ):
            btn = QPushButton(label)
            btn.setObjectName("HubBtn")
            btn.setMinimumHeight(56)
            btn.clicked.connect(lambda _=False, i=idx: self._show_page(i))
            hub.addWidget(btn, 1)
            self._hub_buttons.append(btn)
        my_lib_layout.addLayout(hub)

        lib_card, lib_layout = _make_card()
        top = QHBoxLayout()
        lbl = QLabel(tr("library.shortcuts_title"))
        lbl.setObjectName("SectionTitle")
        self.btn_reload = QPushButton(tr("install.btn_rescan"))
        self.btn_reload.clicked.connect(self._refresh_shortcuts)
        self.btn_lib_sub_add = QPushButton(tr("library.subtab_add"))
        self.btn_lib_sub_add.clicked.connect(lambda: self._switch_library_subview(1))
        self.btn_adopt_games = QPushButton(tr("adopt.btn_open"))
        self.btn_adopt_games.setToolTip(tr("adopt.btn_open_tip"))
        self.btn_adopt_games.clicked.connect(self._open_adopt_wizard)
        top.addWidget(lbl)
        top.addStretch()
        top.addWidget(self.btn_adopt_games)
        top.addWidget(self.btn_lib_sub_add)
        top.addWidget(self.btn_reload)
        lib_layout.addLayout(top)

        self.vdf_error_banner = QLabel()
        self.vdf_error_banner.setWordWrap(True)
        self.vdf_error_banner.setObjectName("VdfErrorBanner")
        self.vdf_error_banner.setVisible(False)
        lib_layout.addWidget(self.vdf_error_banner)

        self.unavailable_banner = QLabel()
        self.unavailable_banner.setWordWrap(True)
        self.unavailable_banner.setObjectName("UnavailableBanner")
        self.unavailable_banner.setVisible(False)
        lib_layout.addWidget(self.unavailable_banner)

        self.vdf_queue_banner = QFrame()
        self.vdf_queue_banner.setObjectName("VdfQueueBanner")
        vq_lay = QHBoxLayout(self.vdf_queue_banner)
        vq_lay.setContentsMargins(8, 4, 8, 4)
        self.lbl_vdf_queue = QLabel()
        self.lbl_vdf_queue.setObjectName("VdfQueueText")
        self.lbl_vdf_queue.setWordWrap(True)
        self.btn_vdf_apply = QPushButton(tr("vdf_queue.btn_apply"))
        self.btn_vdf_apply.setObjectName("primaryBtn")
        self.btn_vdf_apply.clicked.connect(lambda: self._apply_vdf_queue())
        self.btn_vdf_clear = QPushButton(tr("vdf_queue.btn_clear"))
        self.btn_vdf_clear.clicked.connect(self._clear_vdf_queue)
        vq_lay.addWidget(self.lbl_vdf_queue, 1)
        vq_lay.addWidget(self.btn_vdf_apply)
        vq_lay.addWidget(self.btn_vdf_clear)
        self.vdf_queue_banner.setVisible(False)
        lib_layout.addWidget(self.vdf_queue_banner)

        self._library_source_filter = "all"
        self._lib_filter_group = QButtonGroup(self)
        self._lib_filter_group.setExclusive(True)
        source_row = QHBoxLayout()
        source_row.setSpacing(6)
        for key, label in (
            ("all", tr("maint.filter_all")),
            ("steam", "Steam"),
            ("standalone", tr("library.filter_standalone")),
            ("external", tr("library.filter_external")),
        ):
            pill = QPushButton(label)
            pill.setObjectName("FilterPill")
            pill.setCheckable(True)
            if key == "all":
                pill.setChecked(True)
            pill.clicked.connect(
                lambda _checked=False, k=key: self._on_library_source_filter_changed(k)
            )
            self._lib_filter_group.addButton(pill)
            source_row.addWidget(pill)
        source_row.addStretch()
        lib_layout.addLayout(source_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.library_search_input = QLineEdit()
        self.library_search_input.setPlaceholderText(tr("library.search_placeholder"))
        self.library_search_input.setClearButtonEnabled(True)
        self.library_search_input.textChanged.connect(self._filter_library_list)
        self.lbl_library_count = QLabel()
        self.lbl_library_count.setObjectName("MutedText")
        filter_row.addWidget(self.library_search_input, 1)
        filter_row.addWidget(self.lbl_library_count)
        lib_layout.addLayout(filter_row)

        self.shortcuts_list = QTableWidget(0, 6)
        self.shortcuts_list.setHorizontalHeaderLabels(
            [
                tr("table.name"),
                tr("table.source"),
                tr("table.runner"),
                tr("table.graphics"),
                tr("table.engine"),
                tr("table.executable"),
            ]
        )
        self.shortcuts_list.verticalHeader().setVisible(False)
        self.shortcuts_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.shortcuts_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.shortcuts_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.shortcuts_list.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.shortcuts_list.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.shortcuts_list.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        for column in (3, 4):
            self.shortcuts_list.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.shortcuts_list.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.shortcuts_list.horizontalHeader().setStretchLastSection(True)
        self.shortcuts_list.horizontalHeader().setSectionsClickable(True)
        self.shortcuts_list.setSortingEnabled(True)
        self.shortcuts_list.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_args: QTimer.singleShot(0, self, self._filter_library_list)
        )
        self.shortcuts_list.currentCellChanged.connect(lambda *_args: self._update_library_action_states())
        self.shortcuts_list.cellDoubleClicked.connect(lambda *_args: self._open_configure_game_dialog())
        del_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.shortcuts_list)
        del_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        del_shortcut.activated.connect(self._remove_selected_shortcut)
        lib_layout.addWidget(self.shortcuts_list, 1)

        self.btn_play_game = QPushButton(tr("library.btn_play"))
        self.btn_play_game.setObjectName("primaryBtn")
        self.btn_play_game.clicked.connect(self._play_selected_game)


        self.act_import_steam = QAction(tr("library.btn_import_steam"), self)
        self.act_import_steam.triggered.connect(self._import_selected_to_steam)
        self.act_ext_to_steam = QAction(tr("library.btn_ext_to_steam"), self)
        self.act_ext_to_steam.triggered.connect(self._external_add_to_steam)
        self.act_ext_import_saves = QAction(tr("library.btn_ext_import_saves"), self)
        self.act_ext_import_saves.triggered.connect(self._external_import_saves)
        self.btn_configure_game = QPushButton(tr("library.btn_configure_game"))
        self.btn_configure_game.clicked.connect(self._open_configure_game_dialog)

        self.btn_move_game = QPushButton(tr("library.btn_move_disk"))
        self.btn_move_game.clicked.connect(self._move_selected_game)
        self.btn_bulk_optiscaler = QPushButton(tr("library.btn_bulk_optiscaler"))
        self.btn_bulk_optiscaler.clicked.connect(self._open_bulk_optiscaler)
        self.btn_remove_game = QPushButton(tr("library.btn_uninstall"))
        self.btn_remove_game.setObjectName("dangerBtn")
        self.btn_remove_game.clicked.connect(self._remove_selected_shortcut)

        self.btn_bulk_proton = QPushButton(tr("library.btn_bulk_proton"))
        self.btn_bulk_proton.setToolTip(tr("library.btn_bulk_proton_tip"))
        self.btn_bulk_proton.clicked.connect(lambda: bulk_proton_dialog(self))
        self.btn_migrate_legacy = QPushButton(tr("maint.btn_migrate_legacy"))
        self.btn_migrate_legacy.setToolTip(tr("maint.migrate_legacy_tooltip"))
        self.btn_migrate_legacy.clicked.connect(lambda: self._start_legacy_migration())
        footer = QHBoxLayout()
        footer.setSpacing(24)
        for group_text, group_btns in (
            (tr("library.group_play"), (self.btn_play_game,)),
            (tr("library.group_configure"), (self.btn_configure_game, self.btn_move_game)),
            (tr("library.group_uninstall"), (self.btn_remove_game,)),
        ):
            col = QVBoxLayout()
            col.setSpacing(4)
            group_lbl = QLabel(group_text)
            group_lbl.setObjectName("MutedText")
            col.addWidget(group_lbl)
            btn_row = QHBoxLayout()
            for group_btn in group_btns:
                btn_row.addWidget(group_btn)
            col.addLayout(btn_row)
            footer.addLayout(col)
            if self.btn_configure_game in group_btns:
                footer.addStretch()
        lib_layout.addLayout(footer)
        batch_lbl = QLabel(tr("library.group_batch"))
        batch_lbl.setObjectName("MutedText")
        lib_layout.addWidget(batch_lbl)
        batch_flow = FlowLayout(spacing=8)
        for batch_btn in (
            self.btn_bulk_optiscaler,
            self.btn_bulk_proton,
            self.btn_migrate_legacy,
        ):
            batch_flow.addWidget(batch_btn)
        lib_layout.addLayout(batch_flow)
        my_lib_layout.addWidget(lib_card, 1)
        layout.addWidget(my_lib_page, 1)
        return page

    def _open_bulk_optiscaler(self) -> None:
        from .ui_dialogs.optiscaler_bulk_dialog import bulk_optiscaler_dialog

        bulk_optiscaler_dialog(self)

    def _on_library_source_filter_changed(self, key: str) -> None:
        """Update active library source filter pill and re-filter rows."""
        self._library_source_filter = key
        self._filter_library_list()

    def _build_saves_page(self) -> QWidget:
        return build_saves_page(self)

    def _update_saves_action_states(self) -> None:
        update_saves_action_states(self)

    def _refresh_saves_list(self) -> None:
        refresh_saves_page(self)

    def _build_maintenance_page(self) -> QWidget:
        page = build_maintenance_page(self)
        layout = page.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.insertWidget(3, self._build_app_settings_card())  # after the title, subtitle and health card
        return page


    def _refresh_maintenance_health(self) -> None:
        refresh_maintenance_health(self)

    def _apply_saved_theme(self) -> None:
        saved = str(self.settings.value("theme", "auto"))
        idx = self.theme_combo.findData(saved)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        mode: ThemeMode = cast(ThemeMode, saved) if saved in ("auto", "dark", "light") else "auto"
        apply_theme(self.app, mode)
        self.app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)

    def _on_system_scheme_changed(self, *_args: object) -> None:
        """Follow the system light/dark switch live, but only while the theme is set to Auto."""
        if str(self.theme_combo.currentData() or "auto") == "auto":
            apply_theme(self.app, "auto")

    def _on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue("theme", mode)
        apply_theme(self.app, mode)

    def _on_language_changed(self) -> None:
        code = str(self.lang_combo.currentData() or "auto")
        self.settings.setValue("language", code)
        QMessageBox.information(
            _dialog_parent(self),
            __app_name__,
            tr("lang.changed", name=self.lang_combo.currentText()),
        )

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            tr("about.title", app=__app_name__),
            tr("about.body", app=__app_name__, version=__version__),
        )

    def _run_pkexec(
        self,
        fn: Callable[[], tuple[bool, str]],
        *,
        on_result: Callable[[bool, str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> PkexecWorker:
        """Run a privileged pkexec operation in a tracked PkexecWorker."""
        worker = PkexecWorker(fn)
        self._track_worker(worker)
        if on_result is not None:
            worker.finished_result.connect(on_result)
        if on_error is not None:
            worker.failed.connect(on_error)
        worker.start()
        return worker

    def _show_bwrap_warning_dialog(self, bwrap_warn: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(__app_name__)
        box.setText(tr("runtime.bwrap_warning", msg=bwrap_warn))
        fix_btn = box.addButton(tr("runtime.bwrap_fix_btn"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is fix_btn:
            ans = QMessageBox.question(
                _dialog_parent(self),
                __app_name__,
                tr("runtime.bwrap_fix_confirm"),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            self.status_label.setText(tr("runtime.bwrap_fix_btn"))

            def _on_fix_done(ok: bool, out: str) -> None:
                self.status_label.setText(tr("status.ready"))
                if self._closing_pending:
                    return
                if ok:
                    QMessageBox.information(_dialog_parent(self), __app_name__, tr("runtime.bwrap_fix_done"))
                else:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, tr("runtime.bwrap_fix_error", err=out))

            def _on_fix_err(err: str) -> None:
                self.status_label.setText(tr("status.ready"))
                if not self._closing_pending:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, tr("runtime.bwrap_fix_error", err=err))

            self._run_pkexec(
                lambda: fix_bwrap_userns(confirmed=True),
                on_result=_on_fix_done,
                on_error=_on_fix_err,
            )

    def _update_runtimes(self) -> None:
        if self._steam_busy:
            return
        self._begin_busy()
        self.status_label.setText(tr("runtime.checking"))

        def _on_preflight(needs_confirm: bool) -> None:
            if self._closing_pending:
                self._end_busy()
                return
            if needs_confirm:
                # Steam already installed -> installing the extras is optional: "No" is the default.
                steam_ok = self.paths is not None or self._steam_root_found
                ans = QMessageBox.question(
                    _dialog_parent(self),
                    __app_name__,
                    tr("runtime.confirm_optional_steamos"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    _optional_install_default(steam_ok),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    self._end_busy()
                    self.status_label.setText(tr("status.ready"))
                    return

            worker = RuntimeUpdateWorker(self.paths)
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(msg: str, bwrap_warn: str) -> None:
                self._end_busy()
                self.status_label.setText(msg)
                if self._closing_pending:
                    return
                self._refresh_maintenance_health()
                self._refresh_shortcuts()
                if bwrap_warn:
                    self._show_bwrap_warning_dialog(bwrap_warn)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._end_busy()
                msg = tr("runtime.error", err=err)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        preflight = RuntimePreflightWorker(find_steam_root_fn=find_steam_root)
        self._track_worker(preflight)
        preflight.ready.connect(_on_preflight)

        def _preflight_failed(err: str) -> None:
            logging.warning("Runtime preflight failed: %s", err)
            _on_preflight(True)

        preflight.failed.connect(_preflight_failed)
        preflight.start()

    def _check_and_update_app(self) -> None:
        if self._steam_busy:
            return
        self._begin_busy()
        self.status_label.setText(tr("app_update.checking"))
        worker = AppUpdateCheckWorker()
        self._track_worker(worker)

        def _on_checked(info: AppUpdateInfo) -> None:
            if self._closing_pending:
                self._end_busy()
                return
            if not info.has_update:
                self._end_busy()
                msg = tr("app_update.up_to_date", version=info.current_version)
                self.status_label.setText(msg)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                return

            if not info.can_self_update:
                self._end_busy()
                url = info.html_url or GITHUB_RELEASES_URL
                if info.cannot_update_reason == "not_appimage":
                    msg = tr(
                        "app_update.manual_not_appimage",
                        latest=info.latest_version,
                        current=info.current_version,
                        url=url,
                    )
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                elif info.cannot_update_reason == "not_writable":
                    msg = tr(
                        "app_update.manual_not_writable",
                        latest=info.latest_version,
                        current=info.current_version,
                        path=str(info.appimage_path or ""),
                        url=url,
                    )
                    QMessageBox.warning(_dialog_parent(self), __app_name__, msg)
                elif info.cannot_update_reason == "no_checksum":
                    msg = tr("app_update.manual_no_checksum", latest=info.latest_version, url=url)
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                else:
                    msg = tr("app_update.manual_no_asset", latest=info.latest_version, url=url)
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                self.status_label.setText(msg.splitlines()[0])
                return

            ans = QMessageBox.question(
                _dialog_parent(self),
                __app_name__,
                tr(
                    "app_update.available",
                    latest=info.latest_version,
                    current=info.current_version,
                ),
            )
            if ans != QMessageBox.StandardButton.Yes:
                self._end_busy()
                return

            prog_dlg = QProgressDialog(
                tr("app_update.downloading", latest=info.latest_version),
                "",
                0,
                100,
                self,
            )
            prog_dlg.setWindowTitle(__app_name__)
            prog_dlg.setCancelButton(None)
            prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
            prog_dlg.setAutoClose(False)
            prog_dlg.setAutoReset(False)
            prog_dlg.setValue(0)
            prog_dlg.show()

            dl_worker = AppUpdateDownloadWorker(info)
            self._track_worker(dl_worker)
            dl_worker.progress.connect(prog_dlg.setValue)

            def _dl_ok(updated_path: Path) -> None:
                self._end_busy()
                prog_dlg.close()
                prog_dlg.deleteLater()
                if self._closing_pending:
                    return
                pending = self._vdf_queue.pending_count() if self.paths is not None else 0
                if is_gamescope_session():
                    msg = tr("app_update.done_gamescope", latest=info.latest_version)
                    if pending:
                        msg += "\n\n" + tr("app_update.pending_kept", count=pending)
                    self.status_label.setText(msg.splitlines()[0])
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                elif pending:
                    self._ask_update_with_pending_edits(pending, updated_path)
                else:
                    relaunch = QMessageBox.question(
                        _dialog_parent(self),
                        __app_name__,
                        tr("app_update.done_relaunch", latest=info.latest_version),
                    )
                    if relaunch == QMessageBox.StandardButton.Yes:
                        self._restart_updated_appimage(updated_path)

            def _dl_err(err: str) -> None:
                self._end_busy()
                prog_dlg.close()
                prog_dlg.deleteLater()
                msg = tr("app_update.error", err=err)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

            dl_worker.finished_ok.connect(_dl_ok)
            dl_worker.failed.connect(_dl_err)
            dl_worker.start()

        def _on_err(err: str) -> None:
            self._end_busy()
            msg = tr("app_update.error", err=err)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker.checked.connect(_on_checked)
        worker.failed.connect(_on_err)
        worker.start()

    def _probe_install_source(self, inspect: Callable[[], object], ready: Callable[[object], None]) -> None:
        self._install_probe += 1
        request = self._install_probe
        worker = SteamOpWorker(inspect)
        self._track_worker(worker)

        def _on_ready(result: object) -> None:
            if request == self._install_probe and not self._closing_pending and not self._closed:
                ready(result)

        worker.finished_ok.connect(_on_ready)
        worker.failed.connect(self._on_background_failed)
        worker.start()

    def _set_extract_source(self, path_str: str) -> None:
        path = Path(path_str)

        def _inspect() -> tuple[classify.SourceInfo, bool, DetectedGame | None]:
            info = classify.classify_source(path)
            is_dir = path.is_dir()
            folder = path if is_dir else path.parent
            game = None if info.needs_install else scan_game_folder(folder)
            return info, is_dir, game

        def _ready(result: object) -> None:
            if not isinstance(result, tuple):
                return
            info, is_dir, game = result
            if info.needs_install and info.installer is not None:
                self.extract_src.setText(str(path if info.kind == classify.ARCHIVE else info.installer))
                self.add_name.setText(classify.suggest_install_name(info))
                self.extract_details_box.setVisible(True)
                self.status_label.setText(self._detected_kind_text(info))
            elif is_dir and game is not None:
                self._populate_game_form(game)
                self._show_page(PAGE_INSTALL)
                self.add_name.setFocus()
            elif path.is_file():
                self._populate_manual_exe(path, game)
                self._show_page(PAGE_INSTALL)

        self._probe_install_source(_inspect, _ready)

    def _detected_kind_text(self, info: classify.SourceInfo) -> str:
        texts = {
            classify.ARCHIVE: tr("install.detected_archive"),
            classify.GOG_SETUP: tr("install.detected_gog"),
            classify.REPACK: tr("install.detected_repack", name=info.installer.name if info.installer else ""),
            classify.INSTALLER: tr("install.detected_installer"),
            classify.NATIVE_SH: tr("install.detected_native_sh"),
        }
        return texts.get(info.kind, "")

    def _pick_archive_or_installer(self) -> None:
        _pick_file_async(
            self,
            tr("install.pick_archive_title"),
            Path.home() / "Downloads",
            tr("install.pick_archive_qt"),
            lambda path: self._set_extract_source(path) if path else None,
        )

    def _default_extract_root(self) -> Path:
        """Games folder of the library picked in the install form (or ~/Games in the old layout)."""
        if not uses_library_layout():
            return Path.home() / "Games"
        data = str(self.add_library_combo.currentData() or "") if hasattr(self, "add_library_combo") else ""
        kind, _, raw = data.partition(":")
        if kind == "lib" and raw:
            return Path(raw) / "Games"
        if kind == "disk" and raw:
            return Path(raw) / "DeckInstall" / "Games"
        return internal_library_root() / "Games"

    def _pick_dest_root(self) -> None:
        start = Path(self.extract_dst_root.text().strip() or str(self._default_extract_root()))
        _pick_dir_async(
            self,
            tr("install.pick_dest_title"),
            start,
            lambda path: self.extract_dst_root.setText(path) if path else None,
        )

    def _start_extract(self) -> None:
        if self._steam_busy or self._extract_worker is not None:
            return
        src = Path(self.extract_src.text().strip())
        if not src.is_file():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("install.invalid_file"))
            return
        name = self.add_name.text().strip()
        typed_dst = self.extract_dst_root.text().strip()
        if typed_dst:
            dst = Path(typed_dst)
        else:
            lib_ok, lib = self._selected_library()
            if not lib_ok:
                return
            dst = lib.games_dir if lib is not None else Path.home() / "Games"

        self.extract_details_box.setVisible(True)
        self.extract_log.clear()
        self.extract_progress.setValue(0)
        self._begin_busy()
        self.btn_cancel_extract.setEnabled(True)
        self.btn_use_extracted.setEnabled(False)

        self._extract_worker = ExtractWorker(
            src,
            dst,
            name,
        )
        self._track_worker(self._extract_worker)
        self._extract_worker.progress.connect(self._on_extract_progress)
        worker = self._extract_worker
        worker.finished_ok.connect(lambda path: self._on_extract_done(path, worker.kind))
        self._extract_worker.cancelled.connect(self._on_extract_cancelled)
        self._extract_worker.failed.connect(self._on_extract_failed)
        self._extract_worker.start()

    def _cancel_extract(self) -> None:
        if self._extract_worker:
            self.btn_cancel_extract.setEnabled(False)
            self.status_label.setText(tr("install.cancelling"))
            self._extract_worker.cancel()

    def _on_extract_cancelled(self) -> None:
        self._end_busy()
        self.btn_cancel_extract.setEnabled(False)
        msg = tr("install.cancelled")
        self.extract_log.append(msg)
        self.status_label.setText(msg)

    def _on_extract_progress(self, pct: int, msg: str) -> None:
        self.extract_progress.setValue(pct)
        self.extract_log.append(msg)
        self.status_label.setText(msg)

    def _on_extract_done(self, out_dir: str, source_kind: str) -> None:
        self._end_busy()
        self.btn_cancel_extract.setEnabled(False)
        self.btn_use_extracted.setEnabled(True)
        self._last_extracted_dir = Path(out_dir)
        self.status_label.setText(tr("install.done_prefix", path=out_dir))
        self._refresh_games()
        if self._closing_pending or self._closed:
            return
        if source_kind == classify.ARCHIVE:
            def _ready(result: object) -> None:
                if (
                    isinstance(result, classify.SourceInfo)
                    and result.kind in (classify.REPACK, classify.GOG_SETUP, classify.INSTALLER)
                    and result.installer is not None
                ):
                    self.extract_src.setText(str(result.installer))
                    msg = tr("install.archive_has_installer", name=result.installer.name)
                    self.extract_log.append(msg)
                    self.status_label.setText(msg)
                elif self.isVisible():
                    self._send_extracted_to_add_tab()

            self._probe_install_source(lambda: classify.classify_source(Path(out_dir)), _ready)
        elif self.isVisible():
            self._send_extracted_to_add_tab()

    def _on_extract_failed(self, err: str) -> None:
        self._end_busy()
        self.btn_cancel_extract.setEnabled(False)
        self.extract_log.append(tr("install.error_prefix", err=err))
        if not self._closing_pending:
            QMessageBox.critical(_dialog_parent(self), __app_name__, err)

    def _send_extracted_to_add_tab(self) -> None:
        target = getattr(self, "_last_extracted_dir", None)
        if not isinstance(target, Path) or not target.is_dir():
            return

        def _ready(result: object) -> None:
            if isinstance(result, DetectedGame):
                self._populate_game_form(result, from_extract=True)
            self._switch_library_subview(1)

        self._probe_install_source(lambda: scan_game_folder(target), _ready)

    def _load_scan_folders(self) -> tuple[list[tuple[Path, bool]], list[dict[str, object]]]:
        """Return (default_entries, custom_entries) from QSettings."""
        raw_custom = self.settings.value("scan_folders/custom", "[]")
        if isinstance(raw_custom, str):
            try:
                custom_parsed = json.loads(raw_custom)
            except Exception:
                custom_parsed = []
        elif isinstance(raw_custom, (list, tuple)):
            custom_parsed = list(raw_custom)
        else:
            custom_parsed = []

        custom_entries: list[dict[str, object]] = []
        seen_custom: set[str] = set()
        for item in custom_parsed:
            if isinstance(item, dict) and item.get("path"):
                p_str = str(item["path"])
                if p_str not in seen_custom:
                    seen_custom.add(p_str)
                    custom_entries.append(
                        {"path": p_str, "enabled": bool(item.get("enabled", True))}
                    )

        disabled = self._scan_folder_set("scan_folders/disabled_defaults")
        removed = self._scan_folder_set("scan_folders/removed_defaults")
        # Defaults (recomputed on every load): the home folder and every mounted card/disk.
        defaults = [
            (p, str(p) not in disabled) for p in (Path.home(), *default_scan_roots()) if str(p) not in removed
        ]
        return defaults, custom_entries

    def _scan_folder_set(self, key: str) -> set[str]:
        raw = self.settings.value(key, "[]")
        try:
            data = json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            data = []
        return {str(x) for x in data} if isinstance(data, list) else set()

    def _save_scan_folders(
        self,
        disabled_defaults: list[str],
        custom_entries: list[dict[str, object]],
        removed_defaults: list[str] | None = None,
    ) -> None:
        """Persist scan folder configuration to QSettings and refresh label (None keeps removed_defaults)."""
        self.settings.setValue("scan_folders/disabled_defaults", json.dumps(disabled_defaults))
        self.settings.setValue("scan_folders/custom", json.dumps(custom_entries))
        if removed_defaults is not None:
            self.settings.setValue("scan_folders/removed_defaults", json.dumps(sorted(set(removed_defaults))))
        self.settings.sync()
        self._update_scan_folders_label()

    def _enabled_scan_roots(self) -> list[Path]:
        """Return deduplicated list of enabled scan root Paths."""
        defaults, custom_entries = self._load_scan_folders()
        roots: list[Path] = []
        seen: set[Path] = set()
        candidates: list[Path] = [p for p, enabled in defaults if enabled]
        for entry in custom_entries:
            if entry.get("enabled", True):
                p = Path(str(entry["path"])).expanduser()
                if p.is_dir():
                    candidates.append(p)
        for p in candidates:
            try:
                res = p.resolve()
            except OSError:
                res = p
            if res not in seen:
                seen.add(res)
                roots.append(p)
        return roots

    def _update_scan_folders_label(self) -> None:
        """Update the muted active scan folder count label."""
        if hasattr(self, "lbl_scan_folders_count"):
            count = len(self._enabled_scan_roots())
            self.lbl_scan_folders_count.setText(
                tr("scan_folders.active_count", count=count)
            )

    def _open_scan_folders_dialog(self) -> None:
        open_scan_folders_dialog(self)

    def _refresh_games(self, *, on_done: Callable[[], None] | None = None) -> None:
        self.btn_scan.setEnabled(False)
        self._update_scan_folders_label()
        worker = ScanWorker(roots=self._enabled_scan_roots(), scan_fn=scan_all_games)
        self._scan_worker = worker
        self._track_worker(worker)
        worker.finished_scan.connect(
            lambda games, w=worker: self._on_games_scanned(w, games)
        )
        if on_done is not None:
            worker.finished.connect(on_done)
        worker.start()

    def _on_games_scanned(self, worker: ScanWorker, games: list[DetectedGame]) -> None:
        if worker is not self._scan_worker:
            return
        self.btn_scan.setEnabled(True)
        self._detected_games = games
        self.games_list.setSortingEnabled(False)
        self.games_list.clearSpans()
        self.games_list.setRowCount(0)
        if not self._detected_games:
            self.games_list.setRowCount(1)
            self.games_list.setSpan(0, 0, 1, 2)
            empty_item = QTableWidgetItem(tr("add.no_games_detected"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.games_list.setItem(0, 0, empty_item)
            return
        for row, g in enumerate(self._detected_games):
            tag = f" [AppID {g.steam_appid}]" if g.steam_appid else ""
            self.games_list.insertRow(row)
            name_item = QTableWidgetItem(f"{g.name}{tag}")
            name_item.setData(Qt.ItemDataRole.UserRole, row)
            folder_item = QTableWidgetItem(str(g.primary_exe.parent))
            self.games_list.setItem(row, 0, name_item)
            self.games_list.setItem(row, 1, folder_item)
        self.games_list.setSortingEnabled(True)

    def _on_game_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self.games_list.item(row, 0)
        game_index = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(game_index, int) and 0 <= game_index < len(self._detected_games):
            self._populate_game_form(self._detected_games[game_index])

    def _save_last_launch_prefs(self, launch_opts: str, winedll: str) -> None:
        """Persist last-used launch option preferences to QSettings."""
        st = _parse_launch_preset_state(launch_opts)
        self.settings.setValue("last_launch_opts/has_saved", True)
        self.settings.setValue("last_launch_opts/gamemode", bool(st["gamemode"]))
        self.settings.setValue("last_launch_opts/mangohud", bool(st["mangohud"]))
        self.settings.setValue("last_launch_opts/mh_level", int(st["mh_level"] or 0))
        self.settings.setValue("last_launch_opts/mh_position", str(st.get("mh_position") or ""))
        self.settings.setValue("last_launch_opts/mh_fps_limit", int(st.get("mh_fps_limit") or 0))
        self.settings.setValue("last_launch_opts/nvapi", bool(st["nvapi"]))
        self.settings.setValue("last_launch_opts/prime", bool(st["prime"]))
        self.settings.setValue("last_launch_opts/wayland", bool(st["wayland"]))
        self.settings.setValue("last_launch_opts/dlss_upgrade", bool(st["dlss_upgrade"]))
        self.settings.setValue("last_launch_opts/wined3d", bool(st["wined3d"]))
        self.settings.setValue("last_launch_opts/no_esync", bool(st["no_esync"]))
        self.settings.setValue("last_launch_opts/no_fsync", bool(st["no_fsync"]))
        self.settings.setValue("last_launch_opts/winedll", winedll.strip())

    def _load_last_launch_prefs(self) -> tuple[str, str]:
        """Load last-used launch option preferences from QSettings."""
        if not _setting_bool(self.settings, "last_launch_opts/has_saved", False):
            return "", ""
        state: dict[str, object] = {
            key: _setting_bool(self.settings, f"last_launch_opts/{setting}", False)
            for key, setting in _LAST_PREFS_FLAGS.items()
        }
        state["mh_level"] = _setting_int(self.settings, "last_launch_opts/mh_level", 0)
        state["mh_fps_limit"] = _setting_int(self.settings, "last_launch_opts/mh_fps_limit", 0)
        state["mh_position"] = str(self.settings.value("last_launch_opts/mh_position", "") or "")
        try:
            opts = apply_presets("", state, winedlloverrides="")
        except ValueError:
            opts = ""
        winedll = str(self.settings.value("last_launch_opts/winedll", "") or "").strip()
        return opts, winedll

    def _reset_add_launch_options(self) -> None:
        opts, winedll = self._load_last_launch_prefs()
        self._add_launch_options = opts
        self._add_winedlloverrides = winedll
        self._update_add_presets_summary()

    def _update_add_presets_summary(self) -> None:
        names = {
            "gamemode": tr("presets.short_gamemode"),
            "mangohud": tr("presets.short_mangohud"),
            "dlss_upgrade": tr("presets.short_dlss"),
            "nvapi": tr("presets.short_nvapi"),
            "prime": tr("presets.short_prime"),
            "wayland": tr("presets.short_wayland"),
            "wined3d": tr("presets.short_wined3d"),
            "no_esync": tr("presets.short_no_esync"),
            "no_fsync": tr("presets.short_no_fsync"),
        }
        active = [names[k] for k in active_flags(self._add_launch_options) if k in names]
        if self._add_winedlloverrides.strip():
            active.append(tr("presets.short_dll"))
        self.lbl_add_presets.setText(", ".join(active) if active else tr("presets.none"))

    def _is_add_exe_native(self) -> bool:
        exe_text = self.add_exe_combo.currentText().strip()
        if not exe_text:
            return False
        exe_path = Path(exe_text)
        return exe_path.is_file() and is_native_linux_exe(exe_path)

    def _resolve_add_launch_options(self) -> tuple[str, str]:
        is_native = self._is_add_exe_native()
        try:
            merged_opts = apply_presets(self._add_launch_options, winedlloverrides="", native_linux=is_native)
        except ValueError:
            merged_opts = self._add_launch_options.strip()
        winedll = "" if is_native else self._add_winedlloverrides.strip()
        return merged_opts, winedll

    def _open_add_launch_advanced(self) -> None:
        cur_opts, cur_winedll = self._resolve_add_launch_options()
        title = self.add_name.text().strip() or tr("add.page_title")
        res = self._open_launch_presets_dialog(
            title=title,
            kind="standalone",
            initial_opts=cur_opts,
            initial_winedll=cur_winedll,
            game_obj=None,
            is_native_linux=self._is_add_exe_native(),
        )
        if res is None:
            return
        new_opts, new_winedll = res
        self._add_launch_options = new_opts
        self._add_winedlloverrides = new_winedll
        self._update_add_presets_summary()

    def _auto_search_steam_art(self) -> None:
        """Auto-fetch Steam artwork in the background when the Add form is active."""
        if self._closing_pending or not self.isVisible():
            return
        self._search_steam_art()

    def _on_add_name_edited(self, text: str) -> None:
        """Keep Steam search synced and auto-preview artwork after typing stops."""
        if self._closing_pending or not self.isVisible():
            return
        self.steam_search_input.setText(text)
        if len(text.strip()) >= 2:
            self._auto_art_timer.start()
        else:
            self._auto_art_timer.stop()

    def _populate_game_form(self, game: DetectedGame, from_extract: bool = False) -> None:
        self._auto_art_timer.stop()
        self._search_req_id += 1
        self._reset_add_launch_options()
        self.add_name.setText(game.name)
        self.add_exe_combo.clear()
        for exe in game.executables:
            self.add_exe_combo.addItem(str(exe))
        self.chk_keep_in_place.setChecked(not from_extract)
        self.steam_search_input.setText(
            str(game.steam_appid) if game.steam_appid else game.name
        )
        self.steam_match_combo.clear()
        if game.steam_appid:
            self.steam_match_combo.addItem(
                tr("add.detected_appid", appid=game.steam_appid), game.steam_appid
            )
            if self.isVisible() and not self._closing_pending:
                self._on_steam_match_changed()
        elif self.isVisible() and not self._closing_pending:
            self._auto_art_timer.start()
        self._update_remove_existing_state()

    def _on_manual_exe_picked(self, path: str) -> None:
        if not path:
            return
        exe = Path(path)

        def _ready(result: object) -> None:
            self._populate_manual_exe(exe, result if isinstance(result, DetectedGame) else None)

        self._probe_install_source(lambda: scan_game_folder(exe.parent), _ready)

    def _populate_manual_exe(self, exe: Path, game: DetectedGame | None) -> None:
        self._auto_art_timer.stop()
        self._search_req_id += 1
        self._reset_add_launch_options()
        name = game.name if game else exe.stem
        self.add_name.setText(name)
        self.add_exe_combo.clear()
        self.add_exe_combo.addItem(str(exe))
        self.chk_keep_in_place.setChecked(True)
        self.steam_search_input.setText(
            str(game.steam_appid) if (game and game.steam_appid) else name
        )
        self.steam_match_combo.clear()
        if game and game.steam_appid:
            self.steam_match_combo.addItem(
                tr("add.appid_label", appid=game.steam_appid), game.steam_appid
            )
            if self.isVisible() and not self._closing_pending:
                self._on_steam_match_changed()
        else:
            self.preview_label.setText(tr("add.art_placeholder"))
            if self.isVisible() and not self._closing_pending:
                self._auto_art_timer.start()
        self._update_remove_existing_state()

    def _on_exe_changed(self, text: str) -> None:
        """Update Proton default and Remove button visibility when the executable changes."""
        cleaned = text.strip()
        is_elf = False
        if cleaned:
            exe_path = Path(cleaned)
            is_elf = exe_path.is_file() and is_native_linux_exe(exe_path)
        was_disabled = not self.chk_proton.isEnabled()
        if is_elf:
            self.chk_proton.setChecked(False)
            self.chk_proton.setEnabled(False)
        else:
            self.chk_proton.setEnabled(True)
            if was_disabled:
                self.chk_proton.setChecked(True)
        self._match_install_timer.start()

    def _display_preview_pixmap(self, pix: QPixmap) -> None:
        dpr = max(1.0, self.preview_label.devicePixelRatioF())
        scaled = pix.scaled(
            self.preview_label.size() * dpr,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self.preview_label.setPixmap(scaled)

    def _pick_manual_exe(self) -> None:
        _pick_file_async(
            self,
            tr("add.pick_exe_title"),
            Path.home() / "Games",
            tr("add.pick_exe_qt"),
            self._on_manual_exe_picked,
        )

    def _search_steam_art(self) -> None:
        self._auto_art_timer.stop()
        query = self.steam_search_input.text().strip()
        if not query:
            return
        if query.isascii() and query.isdigit():
            self._search_req_id += 1
            appid = int(query) if len(query) <= 10 else 0
            if not (1 <= appid <= 0xFFFFFFFF):
                self._preview_req_id += 1
                self.steam_match_combo.clear()
                self.preview_label.setText(tr("add.art_placeholder"))
                self.status_label.setText(tr("add.invalid_appid"))
                return
            self.steam_match_combo.clear()
            self.steam_match_combo.addItem(tr("add.appid_label", appid=appid), appid)
            self.status_label.setText(tr("add.appid_status", appid=appid))
            return

        self.status_label.setText(tr("add.searching_steam", query=query))
        self._search_req_id += 1
        req_id = self._search_req_id
        worker = SteamSearchWorker(req_id, query)
        self._track_worker(worker)
        worker.results.connect(self._on_steam_results)
        worker.failed.connect(self._on_steam_search_failed)
        worker.start()

    def _on_steam_search_failed(self, req_id: int, err: str) -> None:
        if req_id != self._search_req_id or self._closing_pending:
            return
        QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.search_error", err=err))

    def _on_steam_results(self, req_id: int, matches: list[SteamMatch]) -> None:
        if req_id != self._search_req_id:
            return
        self.steam_match_combo.clear()
        for m in matches:
            self.steam_match_combo.addItem(f"{m.name} ({m.appid})", m.appid)
        self.status_label.setText(
            tr("add.search_count", count=len(matches))
            if matches
            else tr("add.search_empty")
        )

    def _on_steam_match_changed(self) -> None:
        appid = self.steam_match_combo.currentData()
        self._preview_req_id += 1
        if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
            self.preview_label.setText(tr("add.art_placeholder"))
            return
        req_id = self._preview_req_id
        self.preview_label.setText(tr("add.art_loading"))
        worker = PreviewWorker(req_id, appid)
        self._track_worker(worker)
        worker.loaded.connect(self._set_preview_bytes)
        worker.failed.connect(self._on_preview_failed)
        worker.start()

    def _on_preview_failed(self, req_id: int) -> None:
        if req_id != self._preview_req_id:
            return
        self.preview_label.setText(tr("add.art_placeholder"))

    def _set_preview_bytes(self, req_id: int, raw: bytes) -> None:
        if req_id != self._preview_req_id:
            return
        pix = QPixmap()
        if pix.loadFromData(raw):
            self._display_preview_pixmap(pix)
        else:
            self.preview_label.setText(tr("add.art_placeholder"))

    def _validate_add_inputs(self) -> tuple[str, Path] | None:
        app_name = self.add_name.text().strip()
        exe_str = self.add_exe_combo.currentText().strip()
        exe_path = Path(exe_str) if exe_str else Path()
        if not app_name or not exe_str or not exe_path.is_file():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.invalid_input"))
            return None
        if not (is_proton_exe(exe_path) or is_native_linux_exe(exe_path)):
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.not_proton_exe"))
            return None
        return app_name, exe_path

    def _resolve_selected_steam_appid(self) -> int | None:
        steam_appid = self.steam_match_combo.currentData()
        if isinstance(steam_appid, bool) or not isinstance(steam_appid, int) or not (1 <= steam_appid <= 0xFFFFFFFF):
            steam_appid = None
        if steam_appid is None:
            raw_q = self.steam_search_input.text().strip()
            if raw_q.isascii() and raw_q.isdigit() and len(raw_q) <= 10:
                candidate = int(raw_q)
                if 1 <= candidate <= 0xFFFFFFFF:
                    steam_appid = candidate
        return steam_appid

    def _add_selected_to_steam(self) -> None:
        if self._steam_busy:
            return
        if not self.paths:
            QMessageBox.critical(_dialog_parent(self), __app_name__, tr("add.steam_missing"))
            return
        if self._vdf_corrupt:
            QMessageBox.critical(
                _dialog_parent(self),
                __app_name__,
                self.vdf_error_banner.text() or tr("library.vdf_error", err="shortcuts.vdf"),
            )
            return
        validated = self._validate_add_inputs()
        if validated is None:
            return
        app_name, exe_path = validated
        paths = self.paths

        def _proceed_add() -> None:
            if self._closing_pending or self.paths != paths:
                self._end_busy()
                return
            steam_appid = self._resolve_selected_steam_appid()
            launch_opts, winedll = self._resolve_add_launch_options()
            self._save_last_launch_prefs(launch_opts, winedll)

            self._add_worker = AddToSteamWorker(
                paths=paths,
                app_name=app_name,
                exe_path=exe_path,
                steam_appid=steam_appid,
                enable_proton=self.chk_proton.isChecked(),
                restart_steam=self.chk_restart.isChecked(),
                launch_options=launch_opts,
                winedlloverrides=winedll,
            )
            self._track_worker(self._add_worker)
            self._add_worker.status.connect(self.status_label.setText)
            self._add_worker.finished_ok.connect(self._on_add_done)
            self._add_worker.failed.connect(self._on_add_failed)
            self._add_worker.start()

        if self.should_queue_vdf_edits():
            launch_opts, winedll = self._resolve_add_launch_options()
            self._save_last_launch_prefs(launch_opts, winedll)
            is_native = self._is_add_exe_native()
            try:
                effective_launch = (
                    apply_presets(launch_opts, winedlloverrides=winedll, native_linux=is_native)
                    if winedll.strip() and not is_native
                    else launch_opts
                )
            except ValueError:
                effective_launch = launch_opts
            self.enqueue_vdf_edit(
                QueuedVdfEdit(
                    f"add_or_update_shortcut:{app_name}:{exe_path}",
                    "add_or_update_shortcut",
                    0,
                    app_name,
                    {
                        "app_name": app_name,
                        "exe_path": str(exe_path),
                        "start_dir": str(exe_path.parent),
                        "launch_options": effective_launch,
                        "enable_proton": self.chk_proton.isChecked() and not is_native,
                    },
                )
            )
            msg = tr("vdf_queue.enqueued", name=app_name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
            return
        self._begin_busy()
        self._confirm_close_steam_async(_proceed_add, on_cancelled=self._end_busy)

    def _add_selected_standalone(self) -> None:
        validated = self._validate_add_inputs()
        if validated is None:
            return
        create_menu = self.btn_shortcut_menu.isChecked()
        create_desktop = self.btn_shortcut_desktop.isChecked()
        steam_selected = self.btn_shortcut_steam.isEnabled() and self.btn_shortcut_steam.isChecked()
        steam_target = self.paths if steam_selected else None
        restart_steam = self.chk_restart.isChecked()
        if not (create_menu or create_desktop or steam_target is not None):
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.no_shortcut_selected"))
            return
        app_name, exe_path = validated
        steam_appid = self._resolve_selected_steam_appid()
        launch_opts, winedll = self._resolve_add_launch_options()
        self._save_last_launch_prefs(launch_opts, winedll)

        keep_in_place = self.chk_keep_in_place.isChecked()
        single_exe_only = False
        inferred_container = False
        try:
            game_dir = infer_game_root_from_exe(exe_path)
        except ValueError:
            game_dir = exe_path.parent
            inferred_container = True
            if not keep_in_place:
                single_exe_only = True

        lib_ok, target_library = self._selected_library()
        if not lib_ok:
            return
        games_dir = target_library.games_dir if target_library is not None else get_games_dir()
        try:
            game_dir.resolve().relative_to(games_dir.resolve())
            inside_games_dir = True
        except ValueError:
            inside_games_dir = False

        def _start_install(final_keep_in_place: bool, final_single_exe_only: bool) -> None:
            steam_only = (steam_target is not None) and not create_menu and not create_desktop
            if (
                not steam_only
                and not is_native_linux_exe(exe_path)
                and (self.paths is not None or self._steam_root_found)
                and (
                    not get_umu_run_path().is_file()
                    or not list_ge_proton11_dirs()
                )
            ):
                ans = QMessageBox.question(
                    _dialog_parent(self),
                    __app_name__,
                    tr("add.confirm_runtime_steamos"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    _optional_install_default(True),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
            self._begin_busy()
            worker = StandaloneInstallWorker(
                app_name=app_name,
                exe_path=exe_path,
                steam_appid=steam_appid,
                create_menu_shortcut=create_menu,
                create_desktop_shortcut=create_desktop,
                keep_in_place=final_keep_in_place,
                launch_options=launch_opts,
                winedlloverrides=winedll,
                game_dir=game_dir,
                single_exe_only=final_single_exe_only,
                library=target_library,
                prepare_standalone_runtime=not steam_only,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(game: StandaloneGame, bwrap_warn: str) -> None:
                self._end_busy()
                self._refresh_shortcuts()
                msg = tr("add.done_standalone", name=game.name, path=str(game.game_dir))
                self.status_label.setText(msg)
                if self._closing_pending:
                    return
                if bwrap_warn:
                    self._show_bwrap_warning_dialog(bwrap_warn)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                if steam_target is not None:
                    self._add_installed_shortcuts_to_steam(
                        steam_target, [game], restart_steam=restart_steam,
                        on_done=lambda: offer_existing_saves(self, game),
                    )
                else:
                    offer_existing_saves(self, game)

            def _err(err: str) -> None:
                self._end_busy()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if keep_in_place or inside_games_dir:
            _start_install(keep_in_place, single_exe_only)
            return

        self._begin_busy()
        stats_worker = DirStatsWorker(game_dir, exe_path, inferred_container)
        self._track_worker(stats_worker)

        def _on_stats_ready(item_count: int, total_bytes: int) -> None:
            self._end_busy()
            if self._closing_pending:
                return
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(__app_name__)
            box.setText(
                tr(
                    "add.confirm_move_folder",
                    games_dir=str(games_dir),
                    src=str(game_dir),
                    dst=str(games_dir / app_name),
                    count=item_count,
                    size=format_size_bytes(total_bytes),
                )
            )
            if not inferred_container:
                box.addButton(tr("add.btn_move_folder"), QMessageBox.ButtonRole.AcceptRole)
            btn_keep = box.addButton(
                tr("add.btn_keep_in_place"), QMessageBox.ButtonRole.ActionRole
            )
            btn_move_exe = None
            if single_exe_only or inferred_container:
                btn_move_exe = box.addButton(
                    tr("add.btn_move_exe_only"), QMessageBox.ButtonRole.ActionRole
                )
            btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(btn_cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked == btn_cancel or clicked is None:
                return
            if clicked == btn_keep:
                _start_install(True, False)
            elif btn_move_exe is not None and clicked == btn_move_exe:
                _start_install(False, True)
            else:
                _start_install(False, False)

        stats_worker.ready.connect(_on_stats_ready)
        stats_worker.start()

    def _on_add_done(self, msg: str) -> None:
        self._end_busy()
        self.status_label.setText(msg)
        self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.information(_dialog_parent(self), __app_name__, msg)

    def _on_add_failed(self, err: str, partial_written: bool = False) -> None:
        self._end_busy()
        if partial_written:
            self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.critical(_dialog_parent(self), __app_name__, err)

    def _apply_shortcuts_data(
        self,
        paths_snapshot: SteamPaths | None,
        entries: list[ShortcutEntry],
        standalone_games: list[StandaloneGame],
        external_games: list[ExternalLauncherGame],
        vdf_err: str,
        runner_labels: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.shortcuts_list.setSortingEnabled(False)
        self.shortcuts_list.setRowCount(0)
        if not paths_snapshot:
            self._vdf_corrupt = False
            self.vdf_error_banner.hide()
            self._set_vdf_write_enabled(False)
        elif vdf_err:
            self._vdf_corrupt = True
            self.vdf_error_banner.setText(tr("library.vdf_error", err=vdf_err))
            self.vdf_error_banner.show()
            self._set_vdf_write_enabled(False)
        else:
            self._vdf_corrupt = False
            self.vdf_error_banner.hide()
            self._set_vdf_write_enabled(True)

        self._steam_shortcuts = entries
        self._standalone_games = standalone_games
        self._external_games = external_games
        self._update_remove_existing_state()
        refresh_saves_page(
            self,
            after_play=False,
            scan_sources=(self.stack.currentIndex() == PAGE_SAVES),
        )
        labels = (
            runner_labels
            if runner_labels is not None
            else compute_library_runner_labels(
                paths_snapshot, entries, standalone_games, external_games
            )
        )
        default_runner = tr("proton.default_label")
        # Managed games with a shortcut use the Steam entry for play and configuration.
        owned = match_owned_shortcuts(entries, standalone_games, paths_snapshot.user_id if paths_snapshot else None)
        primary = primary_owned_shortcuts(entries, owned)
        self._library_steam_games = {entry.appid_u32: owned[entry.appid_u32] for entry in primary.values()}
        standalone_dirs = [os.path.normpath(str(g.game_dir)) for g in standalone_games]
        self._queue_shortcut_repairs(entries, standalone_games)
        rows: list[tuple[str, str, str, str, tuple[str, object], str]] = [
            (
                owned[entry.appid_u32].name if entry.appid_u32 in owned else entry.app_name,
                "Steam",
                labels.get(("steam", str(entry.appid_u32)), default_runner),
                entry.exe,
                ("steam", entry),
                f"steam:{entry.appid_u32}",
            )
            for entry in entries
            if entry.appid_u32 not in owned or entry.appid_u32 in self._library_steam_games
        ]
        rows.extend(
            (
                game.name,
                tr("library.tag_standalone"),
                labels.get(("standalone", str(game.game_dir)), default_runner),
                str(game.exe_path),
                ("standalone", game),
                f"standalone:{game.game_dir}",
            )
            for game in standalone_games
            if game.game_dir not in primary
        )
        rows.extend(
            (
                ext.name,
                ext.source.title(),
                labels.get(("external", f"{ext.source}:{ext.name}:{ext.exe_path}"), default_runner),
                str(ext.exe_path),
                ("external", ext),
                f"external:{ext.source}:{ext.name}:{ext.exe_path}",
            )
            for ext in external_games
            if not _inside_any(ext.exe_path, standalone_dirs)  # adopted: the DeckInstall row is the game
        )
        for row, (name, source, runner, executable, data, key) in enumerate(rows):
            self.shortcuts_list.insertRow(row)
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, data)
            self.shortcuts_list.setItem(row, 0, item)
            self.shortcuts_list.setItem(row, 1, QTableWidgetItem(source))
            self.shortcuts_list.setItem(row, 2, QTableWidgetItem(runner))
            self.shortcuts_list.setItem(row, 3, QTableWidgetItem(labels.get(("api", key), "-")))
            self.shortcuts_list.setItem(row, 4, QTableWidgetItem(labels.get(("engine", key), "-")))
            self.shortcuts_list.setItem(row, 5, QTableWidgetItem(executable))
        self.shortcuts_list.setSortingEnabled(True)

        self._filter_library_list()
        self._update_library_action_states()
        self._maybe_prompt_migration(standalone_games)

    def _maybe_prompt_migration(self, standalone_games: list[StandaloneGame]) -> None:
        """Offer once per session to move v1 games (~/Games) into the internal library."""
        if self._migration_prompted or self._closing_pending:
            return
        if not any(g.schema < 2 for g in standalone_games):
            return
        self._migration_prompted = True
        if _setting_bool(self.settings, "migration/dont_ask", False):
            return
        QTimer.singleShot(0, lambda: self._start_legacy_migration(from_startup=True))

    def _start_legacy_migration(self, from_startup: bool = False) -> None:
        """Plan, confirm and run the migration of v1 games into ~/DeckInstall."""
        if self._closing_pending or self._steam_busy or not uses_library_layout():
            return
        paths = self.paths
        self._begin_busy()

        def _inspect() -> list[MigrationItem] | None:
            return None if _is_wine_game_running() else plan_migration(paths)

        def _planned(result: object) -> None:
            self._end_busy()
            if self._closed or self._closing_pending or self.paths != paths:
                return
            if isinstance(result, list):
                self._confirm_legacy_migration(result, from_startup, paths)
            elif not from_startup:
                QMessageBox.warning(_dialog_parent(self), __app_name__, tr("migrate.game_running"))

        def _failed(err: str) -> None:
            self._end_busy()
            if not self._closed and not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("migrate.failed", err=err))

        planner = SteamOpWorker(_inspect)
        self._track_worker(planner)
        planner.finished_ok.connect(_planned)
        planner.failed.connect(_failed)
        planner.start()

    def _confirm_legacy_migration(
        self, plan: list[MigrationItem], from_startup: bool, paths: SteamPaths | None,
    ) -> None:
        todo = [i for i in plan if i.action == "migrate"]
        skipped = [i for i in plan if i.action != "migrate"]
        reasons = {
            "steam_shortcut": tr("migrate.reason_steam_shortcut"),
            "symlink": tr("migrate.reason_symlink"),
        }
        skipped_text = "\n".join(f"• {i.game.name}: {reasons.get(i.reason, i.reason)}" for i in skipped)
        if not todo:
            if not from_startup:
                text = tr("migrate.nothing")
                if skipped_text:
                    text += "\n\n" + tr("migrate.skipped", items=skipped_text)
                QMessageBox.information(_dialog_parent(self), __app_name__, text)
            return
        names = "\n".join(f"• {i.game.name}" for i in todo)
        text = tr("migrate.confirm", count=len(todo), dest=str(internal_library_root()), names=names)
        if skipped_text:
            text += "\n\n" + tr("migrate.skipped", items=skipped_text)
        box = QMessageBox(_dialog_parent(self))
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(__app_name__)
        box.setText(text)
        btn_go = box.addButton(tr("migrate.btn_migrate"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("migrate.btn_later"), QMessageBox.ButtonRole.RejectRole)
        btn_never = (
            box.addButton(tr("migrate.btn_dont_ask"), QMessageBox.ButtonRole.DestructiveRole)
            if from_startup
            else None
        )
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        if btn_never is not None and clicked is btn_never:
            self.settings.setValue("migration/dont_ask", True)
            return
        if clicked is not btn_go:
            return

        self._begin_busy()
        self.status_label.setText(tr("migrate.running"))
        def _migrate() -> MigrationResult:
            if _is_wine_game_running():
                raise RuntimeError(tr("migrate.game_running"))
            return migrate_legacy_games(todo, paths)

        worker = SteamOpWorker(_migrate)
        self._track_worker(worker)

        def _ok(res: object) -> None:
            self._end_busy()
            self._refresh_shortcuts()
            if not isinstance(res, MigrationResult) or self._closing_pending:
                return
            lines = [tr("migrate.done", count=len(res.migrated))]
            if res.failed:
                lines.append(tr("migrate.failed_items", items="\n".join(f"• {n}: {e}" for n, e in res.failed)))
            if res.journal is not None:
                lines.append(tr("migrate.undo_hint", path=str(res.journal)))
            self.status_label.setText(lines[0])
            show = QMessageBox.warning if res.failed else QMessageBox.information
            show(_dialog_parent(self), __app_name__, "\n\n".join(lines))

        def _err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("migrate.failed", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _refresh_shortcuts(self, *, on_done: Callable[[], None] | None = None) -> None:
        self._refresh_libraries()
        self._shortcuts_req_id += 1
        req_id = self._shortcuts_req_id
        paths_snapshot = self.paths

        def _on_loaded(
            r_id: int,
            entries: list[ShortcutEntry],
            standalone_games: list[StandaloneGame],
            external_games: list[ExternalLauncherGame],
            vdf_err: str,
            runner_labels: dict[tuple[str, str], str],
        ) -> None:
            if self._closing_pending or r_id != self._shortcuts_req_id:
                return
            self._apply_shortcuts_data(
                paths_snapshot,
                entries,
                standalone_games,
                external_games,
                vdf_err,
                runner_labels=runner_labels,
            )

        worker = ShortcutsRefreshWorker(
            req_id,
            paths_snapshot,
            load_shortcuts_fn=load_shortcuts,
            list_standalone_games_fn=list_standalone_games,
            scan_external_launcher_games_fn=scan_external_launcher_games,
        )
        self._track_worker(worker)
        worker.ready.connect(_on_loaded)
        worker.failed.connect(self._on_background_failed)
        if on_done is not None:
            worker.finished.connect(on_done)
        worker.start()

    def _queue_shortcut_repairs(self, entries: list[ShortcutEntry], games: list[StandaloneGame]) -> None:
        """Steam shortcuts of adopted/moved games still pointing at the old folder (Steam was open):
        queue the fix, applied as soon as Steam is closed (AppID, artwork and playtime are kept)."""
        if self.paths is None or self._vdf_corrupt or self._vdf_applying:
            return
        pending = {e.edit_id: e for e in self._vdf_queue.list_pending()}
        for entry, game in shortcut_path_repairs(entries, games, self.paths.user_id):
            edit_id = f"update_shortcut_paths:{self.paths.user_id}:{entry.appid_u32}"
            exe_str = str(game.exe_path)
            start_str = str(repaired_shortcut_start_dir(entry, game))
            existing_edit = pending.get(edit_id)
            if (
                existing_edit is not None
                and str(existing_edit.payload.get("exe_path") or "") == exe_str
                and str(existing_edit.payload.get("start_dir") or "") == start_str
                and str(existing_edit.payload.get("expected_exe") or "") == entry.exe
            ):
                continue
            logging.info("Steam shortcut %s points at a missing exe; queueing %s", entry.appid_u32, game.exe_path)
            self.enqueue_vdf_edit(
                QueuedVdfEdit(
                    edit_id, "update_shortcut_paths", entry.appid_u32, entry.app_name,
                    {"exe_path": exe_str, "start_dir": start_str,
                     "expected_exe": entry.exe, "user_id": self.paths.user_id,
                     "steam_root": str(self.paths.steam_root)},
                )
            )

    def _filter_library_list(self) -> None:
        """Filter Steam, Standalone, and External library items live and update the count label."""
        source_filter = getattr(self, "_library_source_filter", "all")
        query = self.library_search_input.text().strip().lower()
        total = 0
        shown = 0
        for i in range(self.shortcuts_list.rowCount()):
            item = self.shortcuts_list.item(i, 0)
            if item is None:
                continue
            total += 1
            data = item.data(Qt.ItemDataRole.UserRole)
            kind = data[0] if isinstance(data, tuple) and len(data) == 2 else ""
            source_ok = source_filter == "all" or kind == source_filter
            query_ok = not query or any(
                query in cell.text().lower()
                for column in range(self.shortcuts_list.columnCount())
                if (cell := self.shortcuts_list.item(i, column)) is not None
            )
            visible = source_ok and query_ok
            self.shortcuts_list.setRowHidden(i, not visible)
            if visible:
                shown += 1
        cur_row = self.shortcuts_list.currentRow()
        if cur_row >= 0 and self.shortcuts_list.isRowHidden(cur_row):
            self.shortcuts_list.clearSelection()
            self.shortcuts_list.setCurrentCell(-1, -1)
        self._update_library_action_states()
        self.lbl_library_count.setText(tr("library.count_summary", shown=shown, total=total))

    def _selected_library_item(
        self,
    ) -> tuple[str, ShortcutEntry | StandaloneGame | ExternalLauncherGame] | None:
        item = self.shortcuts_list.item(self.shortcuts_list.currentRow(), 0)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        if (
            isinstance(data, tuple)
            and len(data) == 2
            and data[0] in ("steam", "standalone", "external")
        ):
            return data
        return None


    def _managed_library_game(self, obj: object) -> StandaloneGame | None:
        if isinstance(obj, StandaloneGame):
            return obj
        if isinstance(obj, ShortcutEntry):
            return self._library_steam_games.get(obj.appid_u32)
        return None

    def _update_library_action_states(self) -> None:
        sel = self._selected_library_item()
        is_steam = sel is not None and sel[0] == "steam"
        is_standalone = sel is not None and sel[0] == "standalone"
        is_external = sel is not None and sel[0] == "external"
        idle = not self._steam_busy
        can_write_vdf = idle and bool(self.paths) and not self._vdf_corrupt

        self.btn_configure_game.setEnabled(idle and sel is not None)
        self._update_play_button(sel)
        managed = self._managed_library_game(sel[1]) if sel is not None else None
        self.btn_move_game.setEnabled(managed is not None and not self._steam_busy)

        # Hide mutually exclusive per-game actions instead of merely disabling them
        self.act_import_steam.setVisible(is_standalone)
        self.act_ext_to_steam.setVisible(is_external)
        self.act_ext_import_saves.setVisible(is_external)
        ext_prefix = getattr(sel[1], "prefix_dir", None) if is_external and sel is not None else None
        has_ext_prefix = (
            is_external
            and ext_prefix is not None
            and Path(ext_prefix).is_dir()
        )

        self.act_import_steam.setEnabled(is_standalone and can_write_vdf)
        self.act_ext_to_steam.setEnabled(is_external and can_write_vdf)
        self.act_ext_import_saves.setEnabled(idle and has_ext_prefix)
        self.btn_remove_game.setEnabled((is_steam and can_write_vdf) or (idle and is_standalone))
        self.btn_remove_game.setVisible(not is_external)

    def should_queue_vdf_edits(self) -> bool:
        """Gaming Mode cannot close Steam: Steam edits wait in the saved queue instead of applying now."""
        return self.paths is not None and is_gamescope_session()

    def enqueue_vdf_edit(self, edit: QueuedVdfEdit, *, trigger_watch: bool = True) -> None:
        """Enqueue a VDF edit; it is applied once Steam is closed."""
        if self.paths is not None:
            payload = dict(edit.payload)
            path_user_id = getattr(self.paths, "user_id", None)
            path_steam_root = getattr(self.paths, "steam_root", None)
            if not payload.get("user_id") and path_user_id:
                payload["user_id"] = str(path_user_id)
            if not payload.get("steam_root") and path_steam_root is not None:
                payload["steam_root"] = str(path_steam_root)
            if payload != edit.payload:
                edit = replace(edit, payload=payload)
        self._vdf_queue.enqueue(edit)
        self._vdf_auto_paused = False
        self._update_vdf_queue_banner()
        if trigger_watch:
            self._vdf_watch_tick()

    def _on_vdf_queue_paths_ready(self) -> None:
        """Steam was (re)detected: pick up edits saved earlier (e.g. in Gaming Mode) and apply if Steam is closed."""
        if self._busy_count == 0:  # never swap the list under a running apply
            self._vdf_queue.reload()
        self._vdf_auto_paused = False
        self._update_vdf_queue_banner()
        self._vdf_watch_tick()

    def _update_vdf_queue_banner(self) -> None:
        count = self._vdf_queue.pending_count()
        if count <= 0 or self.paths is None:
            self.vdf_queue_banner.setVisible(False)
            self._vdf_watch_timer.stop()
            self._vdf_steam_open = False
            return
        gaming = is_gamescope_session()
        if gaming:
            text = tr("vdf_queue.banner_gaming", count=count)
        elif self._vdf_steam_open:
            text = tr("vdf_queue.banner_waiting", count=count)
        else:
            text = tr("vdf_queue.banner_text", count=count)
        self.lbl_vdf_queue.setText(text)
        self.btn_vdf_apply.setVisible(not gaming)
        self.vdf_queue_banner.setVisible(True)
        if gaming:
            self._vdf_watch_timer.stop()
        elif not self._vdf_watch_timer.isActive():
            self._vdf_watch_timer.start()

    def _vdf_watch_tick(self) -> None:
        """Poll (off the UI thread) whether Steam is closed; queued edits apply as soon as it is."""
        if (
            self._vdf_checking
            or self._vdf_applying
            or self._vdf_auto_paused
            or self._closed
            or self._closing_pending
            or self._busy_count > 0
            or not self.paths
            or self._vdf_corrupt
            or is_gamescope_session()
            or self._vdf_queue.pending_count() <= 0
        ):
            return
        self._vdf_checking = True
        worker = SteamRunningWorker()
        self._track_worker(worker)

        def _done(running: bool) -> None:
            self._vdf_checking = False
            prev_steam_open = self._vdf_steam_open
            self._vdf_steam_open = running
            self._update_vdf_queue_banner()
            if (
                not running
                and not self._closed
                and not self._closing_pending
                and not self._vdf_auto_paused
                and not self._vdf_applying
                and not self._vdf_corrupt
                and self._busy_count == 0
                and self.paths is not None
                and self._vdf_queue.pending_count() > 0
                and not is_gamescope_session()
            ):
                self._auto_apply_vdf_queue(offer_restart=prev_steam_open)

        def _failed(err: str) -> None:
            self._vdf_checking = False
            logging.warning("Steam running check failed; keeping VDF edits queued: %s", err)

        worker.ready.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def _auto_apply_vdf_queue(self, offer_restart: bool | None = None) -> None:
        """Apply the queue now that Steam is closed (never while it runs)."""
        paths = self.paths
        queue = self._vdf_queue
        if (
            paths is None
            or self._vdf_applying
            or self._vdf_corrupt
            or self._busy_count > 0
            or queue.pending_count() <= 0
        ):
            return
        should_offer_restart = self._vdf_steam_open if offer_restart is None else offer_restart

        def _apply() -> object:
            if is_steam_running():  # Steam was started again since the poll: keep waiting
                return None
            return queue.apply_all(paths)

        self._vdf_applying = True
        self._begin_busy()
        worker = SteamOpWorker(_apply)
        self._track_worker(worker)

        def _ok(res: object) -> None:
            self._vdf_applying = False
            self._end_busy()
            self._update_vdf_queue_banner()
            if res is None:
                self._vdf_steam_open = True
                self._update_vdf_queue_banner()
                return
            self._refresh_shortcuts()
            applied = getattr(res, "applied", 0)
            errors = list(getattr(res, "errors", []))
            if errors:
                self._vdf_auto_paused = True  # do not retry failing edits in a loop
                msg = tr("vdf_queue.applied_errors", applied=applied, errs="\n".join(errors))
                self.status_label.setText(msg.splitlines()[0])
                if not self._closing_pending:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, msg)
                return
            self.status_label.setText(tr("vdf_queue.applied_auto", applied=applied))
            if applied and should_offer_restart:
                self._offer_steam_restart()

        def _err(err: str) -> None:
            self._vdf_applying = False
            self._end_busy()
            self._vdf_auto_paused = True
            self._update_vdf_queue_banner()
            self.status_label.setText(tr("worker.failed", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _maybe_offer_steam_restart(self, worker: QThread) -> None:
        if self._closed or self._closing_pending or self._relaunch_after_close is not None:
            return
        if getattr(worker, "restart_failed", False):
            self._warn_steam_restart_failed()
        elif getattr(worker, "closed_steam", False) and not getattr(worker, "restart_if_was_running", False):
            self._offer_steam_restart()

    def _warn_steam_restart_failed(self) -> None:
        if self._closed or self._closing_pending or self._force_closing:
            return
        self.status_label.setText(tr("steam.restart_failed"))
        box = QMessageBox(
            QMessageBox.Icon.Warning, __app_name__, tr("steam.restart_failed"), QMessageBox.StandardButton.Ok, self
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def _offer_steam_restart(self) -> None:
        """Ask (without blocking) whether to start Steam again after it was closed for an edit."""
        if self._closed or self._closing_pending or self._force_closing or self.paths is None:
            return
        if self._restart_prompt is not None:
            return
        box = QMessageBox(
            QMessageBox.Icon.Question,
            __app_name__,
            tr("steam.restart_offer"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._restart_prompt = box
        steam_root = self.paths.steam_root

        def _answered(result: int) -> None:
            self._restart_prompt = None
            if result != QMessageBox.StandardButton.Yes.value or self._closed or self._closing_pending:
                return
            worker = SteamOpWorker(lambda: start_steam(steam_root))
            self._track_worker(worker)
            worker.failed.connect(self._on_background_failed)
            worker.finished_ok.connect(lambda ok: None if ok else self._warn_steam_restart_failed())
            worker.start()

        box.finished.connect(_answered)
        box.open()

    def _clear_vdf_queue(self) -> None:
        self._vdf_queue.clear()
        self._update_vdf_queue_banner()
        self.status_label.setText(tr("vdf_queue.cleared"))

    def _restart_updated_appimage(self, updated_path: Path) -> None:
        if self._closed:
            return
        self._relaunch_after_close = updated_path
        self._skip_vdf_close_prompt = True
        self.close()

    def _ask_update_with_pending_edits(self, pending: int, updated_path: Path) -> None:
        """Before restarting into the new version, offer to send the queued edits to Steam."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(__app_name__)
        box.setText(tr("app_update.pending_prompt", count=pending))
        btn_apply = box.addButton(tr("app_update.btn_apply_update"), QMessageBox.ButtonRole.AcceptRole)
        btn_update = box.addButton(tr("app_update.btn_update_only"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_apply)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()

        def _restart() -> None:
            if self._closing_pending:
                return
            self._restart_updated_appimage(updated_path)

        if clicked == btn_apply:
            self._apply_vdf_queue(on_done=lambda ok: _restart() if ok else None)
        elif clicked == btn_update:
            _restart()

    def _apply_vdf_queue(self, on_done: Callable[[bool], None] | None = None) -> None:
        """Send the queued edits to Steam now (closes and reopens it).

        With ``on_done`` the user already agreed to close Steam: no extra question, and
        ``on_done(True)`` runs only when everything was applied (errors are still shown).
        """
        if self._vdf_applying:
            if on_done is not None:
                on_done(False)
            return
        if not self.paths or self._vdf_queue.pending_count() <= 0:
            if on_done is not None:
                on_done(True)
            return
        paths = self.paths

        def _proceed() -> None:
            if self._closing_pending or self._vdf_applying or not self.paths:
                if on_done is not None:
                    on_done(False)
                return
            if self._vdf_queue.pending_count() <= 0:
                if on_done is not None:
                    on_done(True)
                return
            self._vdf_applying = True
            self._begin_busy()
            worker = SteamOpWorker(
                lambda: self._vdf_queue.apply_all(paths),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(res: object) -> None:
                self._vdf_applying = False
                self._end_busy()
                self._update_vdf_queue_banner()
                self._refresh_shortcuts()
                applied = getattr(res, "applied", 0)
                errors = list(getattr(res, "errors", []))
                if errors:
                    msg = tr("vdf_queue.applied_errors", applied=applied, errs="\n".join(errors))
                    self.status_label.setText(msg.splitlines()[0])
                    if not self._closing_pending:
                        QMessageBox.warning(_dialog_parent(self), __app_name__, msg)
                    if on_done is not None:
                        on_done(False)
                else:
                    msg = tr("vdf_queue.applied_ok", applied=applied)
                    self.status_label.setText(msg)
                    if on_done is not None:
                        on_done(True)
                    elif not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._vdf_applying = False
                self._end_busy()
                self._update_vdf_queue_banner()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)
                if on_done is not None:
                    on_done(False)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if on_done is not None:
            _proceed()
        else:
            self._confirm_close_steam_async(_proceed)

    def _external_add_to_steam(self) -> None:
        sel = self._selected_library_item()
        if not sel or sel[0] != "external" or not isinstance(sel[1], ExternalLauncherGame):
            return
        if not self.paths or self._vdf_corrupt:
            return
        ext = sel[1]
        paths = self.paths
        launch_opts = load_external_launch_presets(ext)

        if self.should_queue_vdf_edits():
            payload = build_steam_shortcut_payload(ext, launch_options=launch_opts)
            self.enqueue_vdf_edit(
                QueuedVdfEdit(
                    f"add_or_update_shortcut:ext:{ext.name}:{payload['exe_path']}",
                    "add_or_update_shortcut",
                    0,
                    ext.name,
                    {
                        "app_name": str(payload.get("app_name") or ext.name),
                        "exe_path": str(payload["exe_path"]),
                        "start_dir": str(payload["start_dir"]) if payload.get("start_dir") else None,
                        "icon_path": str(payload["icon_path"]) if payload.get("icon_path") else None,
                        "launch_options": payload.get("launch_options"),
                        "enable_proton": bool(payload.get("enable_proton", True)),
                    },
                )
            )
            msg = tr("vdf_queue.enqueued", name=ext.name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
            return

        def _proceed() -> None:
            if self._closing_pending:
                return
            self._begin_busy()
            worker = SteamOpWorker(
                lambda: create_steam_shortcut_from_external(
                    paths, ext, launch_options=launch_opts
                ),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(_res: object) -> None:
                self._end_busy()
                self._refresh_shortcuts()
                msg = tr("library.ext_added_steam", name=ext.name)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._end_busy()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        self._confirm_close_steam_async(_proceed)

    def _external_import_saves(self) -> None:
        if self._steam_busy:
            return
        sel = self._selected_library_item()
        if not sel or sel[0] != "external" or not isinstance(sel[1], ExternalLauncherGame):
            return
        ext = sel[1]
        if ext.prefix_dir is None or not ext.prefix_dir.is_dir():
            QMessageBox.information(_dialog_parent(self), __app_name__, tr("library.ext_no_prefix"))
            return

        targets: list[tuple[str, Path]] = []
        if self.paths is not None:
            for sc in self._steam_shortcuts:
                targets.append((f"[Steam] {sc.app_name}", self.paths.compatdata_dir / str(sc.appid_u32) / "pfx"))
        for sg in self._standalone_games:
            if not sg.native_linux:
                targets.append((f"{tr('library.tag_standalone')} {sg.name}", sg.prefix_dir))

        if not targets:
            QMessageBox.information(_dialog_parent(self), __app_name__, tr("library.ext_no_import_targets"))
            return

        labels = [t[0] for t in targets]
        picked, ok = QInputDialog.getItem(
            _dialog_parent(self),
            __app_name__,
            tr("library.ext_pick_save_target", name=ext.name),
            labels,
            0,
            False,
        )
        if not ok or not picked:
            return
        target_label, target_pfx = targets[labels.index(picked)]
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("saves.game_running"))
            return
        self._begin_busy()
        worker = ExtImportSavesWorker(
            ext, target_pfx, target_label, snapshot_fn=self._make_pre_snapshot_fn()
        )
        self._track_worker(worker)

        def _ok(copied: int) -> None:
            self._end_busy()
            msg = tr("library.ext_saves_imported", count=copied, target=target_label)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _is_auto_snapshot_enabled(self) -> bool:
        """Read safety/auto_snapshot on the main thread."""
        return _setting_bool(self.settings, AUTO_SNAPSHOT_KEY, True)

    def _make_pre_snapshot_fn(
        self, force: bool = False, *, game: StandaloneGame | None = None,
    ) -> Callable[[Path, str, str, str], Path | None]:
        """Capture the snapshot settings on the main thread for worker threads.

        force=True snapshots even when automatic snapshots are off (uninstall, adopt, move).
        """
        enabled = force or self._is_auto_snapshot_enabled()
        keep = snapshot_keep(self.settings)
        game_dir = game.game_dir if game is not None else None
        steam_appid = game.steam_appid if game is not None else None

        def _snap(
            prefix_dir: Path,
            game_name: str,
            reason: str,
            key_prefix: str = "snap",
        ) -> Path | None:
            if not enabled or (game_dir is None and not prefix_dir.is_dir()):
                return None
            return auto_snapshot(
                prefix_dir, game_name, reason, key_prefix, keep=keep, game_dir=game_dir, steam_appid=steam_appid,
            )

        return _snap

    def _auto_snapshot_if_enabled(
        self,
        prefix_dir: Path,
        game_name: str,
        reason: str,
        key_prefix: str = "snap",
        enabled: bool | None = None,
    ) -> Path | None:
        """Best-effort pre-operation save snapshot when safety/auto_snapshot is enabled."""
        local_settings = QSettings(__app_name__, __app_name__)
        if enabled is None:
            enabled = _setting_bool(local_settings, AUTO_SNAPSHOT_KEY, True)
        if not enabled or not prefix_dir.is_dir():
            return None
        try:
            return auto_snapshot(
                prefix_dir, game_name, reason, key_prefix, keep=snapshot_keep(local_settings)
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Pre-operation save snapshot failed for %s (%s): %s",
                game_name,
                reason,
                exc,
            )
            return None

    def _warn_if_win32_prefix(self, prefix_dir: Path | None) -> bool:
        """Warn and return True if prefix_dir is an unsupported 32-bit Wine prefix."""
        if prefix_dir is not None and is_win32_prefix(prefix_dir):
            QMessageBox.warning(
                _dialog_parent(self),
                __app_name__,
                tr("prefix.win32_unsupported", path=str(prefix_dir)),
            )
            return True
        return False

    def _run_exe_in_prefix_dialog(
        self, kind: str | None = None, obj: object | None = None
    ) -> None:
        run_exe_in_prefix_dialog(self, kind=kind, obj=obj)

    def _open_configure_game_dialog(self) -> None:
        if not self._steam_busy:
            open_configure_game_dialog(self)

    @staticmethod
    def _play_key(kind: str, obj: object) -> str:
        """One Play state per game: Steam shortcuts by app id, the others by their folder."""
        if isinstance(obj, ShortcutEntry):
            return f"steam:{obj.appid_u32}"
        return f"{kind}:{getattr(obj, 'game_dir', '') or getattr(obj, 'name', '')}"

    def _update_play_button(
        self, sel: tuple[str, ShortcutEntry | StandaloneGame | ExternalLauncherGame] | None = None
    ) -> None:
        """Play -> Loading... (disabled) -> Stop while running -> Play; Error while the diagnosis is open."""
        btn = self.btn_play_game
        state = self._play.state(self._play_key(*sel)) if sel is not None else ""
        if state == LOADING:
            btn.setText(tr("play.btn_loading") + "." * (self._play.frame % 3 + 1))
            btn.setEnabled(False)
        elif state == RUNNING:
            btn.setText(tr("play.btn_stop"))
            btn.setToolTip(tr("play.running_tip"))
            btn.setEnabled(not self._steam_busy)
        elif state == ERROR:
            btn.setText(tr("play.btn_error"))
            btn.setEnabled(False)
        else:
            btn.setText(tr("library.btn_play"))
            btn.setToolTip("")
            btn.setEnabled(sel is not None and not self._steam_busy)
        name = "dangerBtn" if state == RUNNING else "primaryBtn"
        if btn.objectName() != name:
            btn.setObjectName(name)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_play_changed(self, key: str) -> None:
        sel = self._selected_library_item()
        if sel is not None and self._play_key(*sel) == key and not self._closing_pending:
            self._update_play_button(sel)

    def _on_play_failed(self, key: str, name: str, diagnosis: str) -> None:
        if self._closing_pending:
            self._play.acknowledge(key)
            return
        self.status_label.setText(tr("play.failed", name=name))
        dlg = show_text_dialog(self, tr("play.failed_title"), tr("play.failed", name=name), diagnosis)
        dlg.finished.connect(lambda _r, k=key: self._play.acknowledge(k))

    def _on_play_timed_out(self, _key: str, name: str, steam: bool) -> None:
        if self._closing_pending:
            return
        msg = tr("play.steam_timeout", name=name) if steam else tr("play.timeout", name=name)
        self.status_label.setText(msg)
        if not steam:
            QMessageBox.warning(_dialog_parent(self), __app_name__, msg)

    def _close_stale_instance(
        self, key: str, obj: ShortcutEntry | StandaloneGame | ExternalLauncherGame, launch: Callable[[], None],
    ) -> None:
        """Launch immediately or after the confirmed cleanup of an old instance."""
        if isinstance(obj, ShortcutEntry):
            exe = Path(_strip_vdf_quotes(obj.exe))
            try:
                game_dir: Path | None = shortcut_game_dir(obj)
            except ValueError:
                game_dir = exe.parent
            compat = self.paths.compatdata_dir / str(obj.appid_u32) if self.paths is not None else None
            name, watch = obj.app_name, watch_paths(game_dir, compat)
        else:
            exe = Path(obj.exe_path)
            name, watch = obj.name, watch_paths(obj.game_dir, obj.prefix_dir)
        attempt = self._play.prepare(key, name)

        def _inspect() -> tuple[set[int], dict[int, float]]:
            pids = stale_instance_pids(exe.name, watch)
            epochs = {pid: epoch for pid in pids if (epoch := process_start_epoch(pid)) is not None}
            return pids, epochs

        def _ready(result: object) -> None:
            if self._closed or self._closing_pending or not self._play.is_preparing(key, attempt):
                return
            if not isinstance(result, tuple):
                self._play.cancel(key)
                return
            pids, epochs = result
            if not pids:
                launch_after_cleanup()
                return
            since = time.strftime("%H:%M", time.localtime(min(epochs.values()))) if epochs else "?"
            box = QMessageBox(
                QMessageBox.Icon.Warning, __app_name__, tr("play.stale_instance", name=name, since=since),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, _dialog_parent(self),
            )
            box.button(QMessageBox.StandardButton.Yes).setText(tr("play.stale_close_and_play"))
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
            answer = box.exec()
            box.deleteLater()
            if answer != QMessageBox.StandardButton.Yes:
                self._play.cancel(key)
                return
            surviving = {pid for pid, epoch in epochs.items() if process_start_epoch(pid) == epoch}
            if not surviving:
                launch_after_cleanup()
                return
            logging.info("Closing old instance of %s: %s", name, sorted(surviving))
            self._play.kill_pids(surviving, on_finished=launch_after_cleanup)

        def launch_after_cleanup() -> None:
            if self._closed or self._closing_pending or not self._play.is_preparing(key, attempt):
                return
            self._play.cancel(key)
            launch()

        def _failed(err: str) -> None:
            if self._play.is_preparing(key, attempt):
                self._play.cancel(key)
                self._on_background_failed(err)

        worker = SteamOpWorker(_inspect)
        self._track_worker(worker)
        worker.finished_ok.connect(_ready)
        worker.failed.connect(_failed)
        worker.start()

    def _play_selected_game(self) -> None:
        """Play the selected game, or stop it when it is already running."""
        sel = self._selected_library_item()
        if not sel:
            return
        kind, obj = sel
        key = self._play_key(kind, obj)
        state = self._play.state(key)
        if state == RUNNING:
            name = obj.app_name if isinstance(obj, ShortcutEntry) else obj.name
            if QMessageBox.question(
                _dialog_parent(self), __app_name__, tr("play.confirm_stop", name=name)
            ) == QMessageBox.StandardButton.Yes:
                self._play.stop(key)
            return
        if state != "idle":
            return
        self._close_stale_instance(key, obj, lambda: self._launch_library_game(key, kind, obj))

    def _launch_library_game(
        self, key: str, kind: str, obj: ShortcutEntry | StandaloneGame | ExternalLauncherGame,
    ) -> None:
        if isinstance(obj, ShortcutEntry):
            rungameid = (obj.appid_u32 << 32) | 0x02000000
            try:
                game_dir: Path | None = shortcut_game_dir(obj)
            except ValueError:
                game_dir = None
            compat = self.paths.compatdata_dir / str(obj.appid_u32) if self.paths is not None else None
            self._play.watch_only(key, obj.app_name, watch_paths(game_dir, compat))
            QDesktopServices.openUrl(QUrl(f"steam://rungameid/{rungameid}"))
            self.status_label.setText(tr("play.launched", name=obj.app_name))
            return
        title_name = obj.name
        attempt = self._play.prepare(key, title_name)
        paths_snap = self.paths

        def _on_prep_ready(
            status: str,
            msg_or_fallback: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
        ) -> None:
            if self._closing_pending or not self._play.is_preparing(key, attempt):
                return  # closing, or this attempt timed out / was replaced: never launch twice
            if status != "ok":
                self._play.cancel(key)
            if status == "win32":
                assert isinstance(obj, StandaloneGame)
                self._warn_if_win32_prefix(obj.prefix_dir)
                return
            if status == "warn":
                QMessageBox.warning(_dialog_parent(self), __app_name__, msg_or_fallback)
                return
            if status == "error":
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("play.error", err=msg_or_fallback))
                return
            if msg_or_fallback and isinstance(obj, StandaloneGame):
                obj.proton_name = msg_or_fallback
                fb_msg = tr("play.proton_fallback", name=obj.name, proton=msg_or_fallback)
                self.status_label.setText(fb_msg)
                QMessageBox.information(_dialog_parent(self), __app_name__, fb_msg)
            # DeckInstall games log every run from their own launch.sh (any launcher).
            standalone = isinstance(obj, StandaloneGame)
            log_file = None if standalone else get_game_log_path(title_name)
            log_path = game_log_dir(obj) / "run.log" if isinstance(obj, StandaloneGame) else log_file
            self._play.launch(
                key, title_name, cmd, cwd=cwd, env=env, log_file=log_file, log_path=log_path,
                watch=watch_paths(obj.game_dir, obj.prefix_dir), exe_name=Path(obj.exe_path).name,
            )
            self.status_label.setText(tr("play.launched", name=title_name))

        worker = LaunchPrepWorker(kind, obj, paths_snap)
        self._track_worker(worker)
        worker.ready.connect(_on_prep_ready)
        worker.start()

    def _move_selected_game(self) -> None:
        sel = self._selected_library_item()
        game = self._managed_library_game(sel[1]) if sel is not None else None
        if game is not None:
            move_game_to_disk(self, game)

    def _on_background_failed(self, err: str) -> None:
        """A background worker died with an error: say so instead of failing silently."""
        logging.warning("Background worker failed: %s", err)
        if not self._closing_pending:
            self.status_label.setText(tr("worker.failed", err=err))

    def _set_vdf_write_enabled(self, enabled: bool) -> None:
        for w in (
            self.btn_add_steam,
            self.btn_shortcut_steam,
            self.act_import_steam,
            self.act_ext_to_steam,
        ):
            w.setEnabled(enabled)

    @property
    def _steam_busy(self) -> bool:
        return self._busy_count > 0

    def _begin_busy(self) -> None:
        """Start one busy operation; the UI stays locked until every begin has its end."""
        self._busy_count += 1
        self._apply_busy_state()

    def _end_busy(self) -> None:
        """Finish one busy operation (never goes below zero)."""
        self._busy_count = max(0, self._busy_count - 1)
        self._apply_busy_state()

    def _apply_busy_state(self) -> None:
        enabled = self._busy_count == 0
        for w in (
            self.btn_redetect_steam,
            self.btn_reload,
            self.btn_add_standalone,
            self.shortcuts_list,
            self.btn_play_game,
            self.btn_configure_game,
            self.btn_move_game,
            self.btn_remove_game,
            self.act_ext_import_saves,
            self.btn_save_backup,
            self.btn_save_restore,
            self.btn_save_sync,
            self.btn_save_dedup,
            self.btn_save_refresh,
            self.btn_adopt_games,
            self.btn_bulk_optiscaler,
            self.btn_bulk_proton,
            self.btn_maint_scan,
            self.btn_maint_clean_safe,
            self.btn_maint_delete_selected,
            self.btn_maint_toggle_filter,
        ):
            w.setEnabled(enabled)
        for name in (
            "btn_extract",
            "btn_migrate_legacy",
            "btn_update_runtimes",
            "btn_uninstall_runtimes",
            "btn_app_update",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(enabled)
        can_vdf = enabled and bool(self.paths) and not self._vdf_corrupt
        for name in ("btn_vdf_apply", "btn_vdf_clear"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(can_vdf)
        for b in save_row_buttons(self):
            b.setEnabled(enabled)
        self.btn_maint_revalidate.setEnabled(enabled and (self.paths is not None))
        self._set_vdf_write_enabled(can_vdf)
        if enabled:
            self._update_library_action_states()
            _update_maint_summary(self)
            self._update_saves_action_states()
        self._update_remove_existing_state()

    def _check_steam_running_async(
        self,
        on_result: Callable[[bool], None],
    ) -> None:
        """Check whether Steam is running on a tracked background thread."""
        worker = SteamRunningWorker()
        self._track_worker(worker)

        def _done(running: bool) -> None:
            if not self._closing_pending:
                on_result(running)

        worker.ready.connect(_done)
        # Unknown state: assume Steam is running, so nothing writes VDF by mistake.

        def _check_failed(err: str) -> None:
            logging.warning("Steam running check failed: %s", err)
            _done(True)

        worker.failed.connect(_check_failed)
        worker.start()

    def _confirm_close_steam_async(
        self,
        on_confirmed: Callable[[], None],
        on_cancelled: Callable[[], None] | None = None,
        parent: QWidget | None = None,
        alive_fn: Callable[[], bool] | None = None,
        prompt: str | None = None,
    ) -> None:
        """Check if Steam is running asynchronously and prompt before proceeding."""

        def _on_checked(running: bool) -> None:
            if alive_fn is not None and not alive_fn():
                return
            if self._closed or self._closing_pending:
                if on_cancelled is not None:
                    on_cancelled()
                return
            if running and not self.should_queue_vdf_edits():  # Gaming Mode queues: Steam stays open
                ans = QMessageBox.question(
                    parent or self,
                    __app_name__,
                    prompt or tr("proton.steam_running_confirm"),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    if on_cancelled is not None:
                        on_cancelled()
                    return
            on_confirmed()

        self._check_steam_running_async(_on_checked)

    def _open_save_sync_dialog(self) -> None:
        open_save_sync_dialog(self)

    def _open_launch_presets_dialog(
        self,
        title: str,
        kind: str,
        initial_opts: str,
        initial_winedll: str,
        game_obj: object | None = None,
        is_native_linux: bool = False,
        parent: QWidget | None = None,
    ) -> tuple[str, str] | None:
        return open_launch_presets_dialog(
            self,
            title=title,
            kind=kind,
            initial_opts=initial_opts,
            initial_winedll=initial_winedll,
            game_obj=game_obj,
            is_native_linux=is_native_linux,
            parent=parent,
        )

    def _edit_launch_options_presets(
        self,
        sel: tuple[str, object] | None = None,
        parent: QWidget | None = None,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        edit_launch_options_presets(self, sel=sel, parent=parent, on_saved=on_saved)

    def _open_winetricks_dialog(
        self, kind: str | None = None, obj: object | None = None
    ) -> None:
        open_winetricks_dialog(self, kind=kind, obj=obj)

    def _open_optiscaler_dialog(
        self, kind: str | None = None, obj: object | None = None, *,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        open_optiscaler_dialog(self, kind=kind, obj=obj, on_saved=on_saved)

    def _backup_all_saves(self) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("cleanup.wine_running"))
            return
        self._begin_busy()
        self.status_label.setText(tr("backup.running"))

        def _ok(zpath: str, games: int, files: int) -> None:
            self._end_busy()
            if self._closing_pending:
                return
            if files == 0:
                Path(zpath).unlink(missing_ok=True)
                msg = tr("backup.empty")
            else:
                msg = tr("backup.done", files=files, games=games, path=zpath)
            self._refresh_saves_list()
            self.status_label.setText(msg.splitlines()[0])
            QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("backup.error", err=err))

        w = BackupWorker(self.paths)
        self._track_worker(w)
        w.done.connect(_ok)
        w.failed.connect(_err)
        w.start()

    def _restore_saves_backup(self) -> None:
        bak_dir = DEFAULT_SAVES_DIR / "backups"
        legacy_bak_dir = Path.home() / "Backups" / "DeckInstall"
        start_dir = (
            bak_dir
            if bak_dir.is_dir()
            else legacy_bak_dir
            if legacy_bak_dir.is_dir()
            else Path.home()
        )
        _pick_file_async(
            self,
            tr("restore.pick"),
            start_dir,
            tr("restore.pick_qt"),
            self._on_restore_zip_picked,
        )

    def _on_restore_zip_picked(self, chosen: str) -> None:
        if not chosen:
            return
        zpath = Path(chosen)
        if (
            QMessageBox.question(
                _dialog_parent(self), __app_name__, tr("restore.confirm", name=zpath.name)
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._begin_busy()
        self.status_label.setText(tr("library.restore_running"))

        def _on_restore_done(games: int, files: int) -> None:
            self._end_busy()
            self._refresh_saves_list()
            msg = tr("restore.done", files=files, games=games)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _on_restore_err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("restore.error", err=err))

        worker = RestoreWorker(self.paths, zpath)
        self._track_worker(worker)
        worker.done.connect(_on_restore_done)
        worker.failed.connect(_on_restore_err)
        worker.start()

    def _adopt_library_choices(self) -> list[tuple[str, Path]]:
        """Libraries offered by the install tab, as (label, library root) for the adoption wizard."""
        self._refresh_libraries()
        choices: list[tuple[str, Path]] = []
        combo = self.add_library_combo
        for i in range(combo.count()):
            kind, _, raw = str(combo.itemData(i) or "").partition(":")
            if kind == "lib" and raw:
                choices.append((combo.itemText(i), Path(raw)))
            elif kind == "disk" and raw:
                choices.append((combo.itemText(i), Path(raw) / "DeckInstall"))
        if not choices:
            root = internal_library_root()
            choices.append((tr("add.library_internal", path=str(root)), root))
        return choices

    def _open_adopt_wizard(self) -> None:
        """One wizard for Hydra, Heroic, Lutris, Bottles, umu, old Steam shortcuts and loose folders."""
        if self._steam_busy:
            return
        paths = self.paths

        def _open(steam_running: bool) -> None:
            if self._closing_pending:
                return
            # Removing a Steam shortcut rewrites shortcuts.vdf, which needs Steam closed.
            if not paths:
                writable, note = False, ""
            elif self._vdf_corrupt:
                writable, note = False, tr("adopt.steam_vdf_unreadable")
            elif steam_running:
                writable, note = False, tr("adopt.steam_running_note")
            else:
                writable, note = True, ""
            results = open_adopt_wizard(
                _dialog_parent(self),
                paths,
                self._adopt_library_choices(),
                steam_writable=writable,
                steam_note=note,
                track_worker=self._track_worker,
                snapshot_fn=self._make_pre_snapshot_fn(force=True),
                steam_add_allowed=not self._vdf_corrupt,
            )
            if results:
                self.status_label.setText(tr("adopt.status_done", count=len(results)))
                self._refresh_libraries()
                self._refresh_shortcuts()
                if paths is not None and not self._vdf_corrupt:
                    for r in results:
                        if r.remove_shortcut_deferred and r.shortcut_appid is not None:
                            self.enqueue_vdf_edit(
                                QueuedVdfEdit(
                                    f"remove_shortcut:{r.shortcut_appid}",
                                    "remove_shortcut",
                                    r.shortcut_appid,
                                    r.game.name,
                                    {},
                                ),
                                trigger_watch=False,
                            )
                        elif (
                            r.shortcut_needs_repoint
                            and r.shortcut_appid is not None
                            and not r.removed_from_steam
                        ):
                            matched_sc = next(
                                (e for e in self._steam_shortcuts if e.appid_u32 == r.shortcut_appid),
                                None,
                            )
                            if matched_sc is not None:
                                self.enqueue_vdf_edit(
                                    QueuedVdfEdit(
                                        f"update_shortcut_paths:{paths.user_id}:{matched_sc.appid_u32}",
                                        "update_shortcut_paths",
                                        matched_sc.appid_u32,
                                        matched_sc.app_name,
                                        {
                                            "exe_path": str(r.game.exe_path),
                                            "start_dir": str(repaired_shortcut_start_dir(matched_sc, r.game)),
                                            "expected_exe": matched_sc.exe,
                                            "user_id": paths.user_id,
                                            "steam_root": str(paths.steam_root),
                                        },
                                    ),
                                    trigger_watch=False,
                                )
                    selected = [result.game for result in results if result.add_to_steam]
                    if selected and self._vdf_queue.pending_count() == 0:
                        self._add_installed_shortcuts_to_steam(paths, selected)
                    else:
                        if selected:
                            for game in selected:
                                self.enqueue_vdf_edit(
                                    QueuedVdfEdit(
                                        f"add_installed_game:{paths.user_id}:{game.uid}",
                                        "add_installed_game",
                                        0,
                                        game.name,
                                        {
                                            "game_dir": str(game.game_dir),
                                            "uid": game.uid,
                                            "user_id": paths.user_id,
                                            "steam_root": str(paths.steam_root),
                                        },
                                    ),
                                    trigger_watch=False,
                                )
                        if self._vdf_queue.pending_count() > 0 and not self.should_queue_vdf_edits():
                            self._apply_vdf_queue()

        self._check_steam_running_async(_open)

    def _add_installed_shortcuts_to_steam(
        self,
        paths: SteamPaths,
        games: list[StandaloneGame],
        *,
        restart_steam: bool = True,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Finish the Steam part of an install or adoption after the game is available."""
        if self._closing_pending:
            return
        if self.paths is None or self.paths.steam_root != paths.steam_root or self._vdf_corrupt:
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.steam_unavailable"))
            if on_done is not None:
                on_done()
            return

        edits = [
            QueuedVdfEdit(
                f"add_installed_game:{paths.user_id}:{game.uid}", "add_installed_game", 0, game.name,
                {"game_dir": str(game.game_dir), "uid": game.uid, "user_id": paths.user_id,
                 "steam_root": str(paths.steam_root)},
            )
            for game in games
        ]

        def _queue() -> None:
            for edit in edits:
                self.enqueue_vdf_edit(edit)
            self.status_label.setText(tr("add.steam_shortcuts_queued", count=len(edits)))
            if on_done is not None:
                on_done()

        if self.should_queue_vdf_edits():
            _queue()
            return

        def _apply() -> None:
            self._begin_busy()
            batch = VdfEditQueue()
            for edit in edits:
                batch.enqueue(edit)
            worker = SteamOpWorker(
                lambda: batch.apply_all(paths), need_steam_closed=True, restart_if_was_running=restart_steam,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _done(result: object) -> None:
                self._end_busy()
                self._refresh_shortcuts()
                if self._closing_pending or not isinstance(result, BatchApplyResult):
                    return
                if result.errors:
                    for pending_edit in batch.list_pending():
                        self.enqueue_vdf_edit(pending_edit)
                msg = tr("add.steam_shortcuts_done", count=result.applied)
                self.status_label.setText(msg)
                if result.errors:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, msg + "\n" + "\n".join(result.errors))
                if on_done is not None:
                    on_done()

            def _failed(err: str) -> None:
                self._end_busy()
                if self._closing_pending:
                    return
                QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.steam_shortcuts_failed", err=err))
                _queue()

            worker.finished_ok.connect(_done)
            worker.failed.connect(_failed)
            worker.start()

        confirm = (
            tr("add.steam_shortcuts_confirm_restart") if restart_steam else tr("add.steam_shortcuts_confirm_close")
        )
        self._confirm_close_steam_async(_apply, on_cancelled=_queue, prompt=confirm)

    def _import_selected_to_steam(self) -> None:
        sel = self._selected_library_item()
        if not sel or sel[0] != "standalone" or not self.paths:
            return
        game = sel[1]
        assert isinstance(game, StandaloneGame)
        if not game.native_linux and self._warn_if_win32_prefix(game.prefix_dir):
            return
        paths = self.paths

        def _proceed_import() -> None:
            if self._closing_pending:
                return
            self._begin_busy()
            def import_game() -> int:
                if game.schema >= 2 and game.uid:
                    return add_installed_game_to_steam(paths, game.game_dir, game.uid)
                return import_standalone_to_steam(paths, game)

            worker = SteamOpWorker(import_game, need_steam_closed=True)
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(res: object) -> None:
                self._end_busy()
                self._refresh_shortcuts()
                msg = tr("library.import_done", name=game.name, appid=res)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._end_busy()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if self.should_queue_vdf_edits():
            self.enqueue_vdf_edit(
                QueuedVdfEdit(
                    f"add_installed_game:{paths.user_id}:{game.uid}",
                    "add_installed_game",
                    0,
                    game.name,
                    {
                        "game_dir": str(game.game_dir),
                        "uid": game.uid,
                        "user_id": paths.user_id,
                        "steam_root": str(paths.steam_root),
                    },
                )
            )
            msg = tr("vdf_queue.enqueued", name=game.name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
            return
        self._confirm_close_steam_async(_proceed_import)

    def _find_matching_installed(self) -> tuple[StandaloneGame | None, ShortcutEntry | None]:
        """Match the current Add-tab game/exe against existing Standalone games and Steam shortcuts."""
        exe_str = self.add_exe_combo.currentText().strip()
        name_str = self.add_name.text().strip().lower()
        exe_resolved: Path | None = None
        if exe_str:
            try:
                exe_resolved = Path(exe_str).expanduser().resolve()
            except OSError:
                exe_resolved = Path(exe_str)

        matched_sa: StandaloneGame | None = None
        for game in self._standalone_games:
            try:
                g_exe = game.exe_path.resolve()
                g_dir = game.game_dir.resolve()
            except OSError:
                g_exe = game.exe_path
                g_dir = game.game_dir
            if exe_resolved is not None and (
                exe_resolved == g_exe or exe_resolved.parent == g_dir or g_dir in exe_resolved.parents
            ):
                matched_sa = game
                break
            if name_str and game.name.strip().lower() == name_str:
                matched_sa = game
                break

        matched_steam: ShortcutEntry | None = None
        for entry in self._steam_shortcuts:
            entry_exe = entry.exe.strip().strip('"')
            if exe_resolved is not None and entry_exe:
                try:
                    if Path(entry_exe).resolve() == exe_resolved:
                        matched_steam = entry
                        break
                except OSError:
                    if entry_exe == exe_str:
                        matched_steam = entry
                        break
            if name_str and entry.app_name.strip().lower() == name_str:
                matched_steam = entry
                break

        return matched_sa, matched_steam

    def _update_remove_existing_state(self) -> None:
        """Show/enable the Add-tab Remove button when the current game is already installed."""
        sa, steam_entry = self._find_matching_installed()
        has_match = (sa is not None) or (
            steam_entry is not None and bool(self.paths) and not self._vdf_corrupt
        )
        self.btn_remove_existing.setVisible(has_match)
        self.btn_remove_existing.setEnabled(has_match and not self._steam_busy)

    def _remove_steam_shortcut_async(
        self, entry: ShortcutEntry, refresh_games: bool = False, done_msg: str = "",
        *, defer_on_cancel: bool = False,
    ) -> None:
        """Remove a Steam Non-Steam shortcut in a background thread (or queue it in Gaming Mode)."""
        if not self.paths or self._vdf_corrupt:
            return

        def _queue_removal() -> None:
            closing = self._closed or self._closing_pending
            self.enqueue_vdf_edit(
                QueuedVdfEdit(
                    f"remove_shortcut:{entry.appid_u32}",
                    "remove_shortcut",
                    entry.appid_u32,
                    entry.app_name,
                    {},
                ),
                trigger_watch=not closing,
            )
            if closing:
                return
            if refresh_games:
                self._refresh_games()
            self._refresh_shortcuts()
            self.status_label.setText(done_msg or tr("vdf_queue.enqueued", name=entry.app_name))
        if self.should_queue_vdf_edits():
            _queue_removal()
            return

        paths = self.paths

        def _start_remove() -> None:
            if self._closing_pending:
                return
            self._begin_busy()
            self.status_label.setText(tr("library.removing", name=entry.app_name))

            worker = RemoveShortcutWorker(paths, entry)
            self._track_worker(worker)

            def _on_removed(removed: bool) -> None:
                self._end_busy()
                if refresh_games:
                    self._refresh_games()
                if removed or refresh_games:
                    self._refresh_shortcuts()
                    self.status_label.setText(done_msg or tr("library.removed", name=entry.app_name))

            def _on_remove_err(err: str) -> None:
                self._end_busy()
                self.status_label.setText(done_msg or tr("status.ready"))
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.done.connect(_on_removed)
            worker.failed.connect(_on_remove_err)
            worker.start()

        self._confirm_close_steam_async(_start_remove, on_cancelled=_queue_removal if defer_on_cancel else None)

    def _uninstall_standalone_async(
        self,
        sa: StandaloneGame,
        *,
        steam_entry: ShortcutEntry | None = None,
        refresh_games: bool = False,
    ) -> None:
        """Plan the uninstall in the background, let the user pick the parts, then remove them."""
        if _is_wine_game_running() or self._play.state(self._play_key("standalone", sa)) in (LOADING, RUNNING):
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("cleanup.wine_running"))
            return
        self._begin_busy()
        self.status_label.setText(tr("uninstall.planning", name=sa.name))
        planner = SteamOpWorker(lambda: plan_uninstall(sa))
        self._track_worker(planner)

        def _on_err(err: str) -> None:
            self._end_busy()
            self.status_label.setText(tr("status.ready"))
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        def _on_planned(plan: object) -> None:
            self._end_busy()
            self.status_label.setText(tr("status.ready"))
            if self._closing_pending or not isinstance(plan, UninstallPlan):
                return
            selected = ask_uninstall(_dialog_parent(self), plan, also_steam=steam_entry is not None)
            if selected is None:
                return
            self._begin_busy()
            self.status_label.setText(tr("uninstall.running", name=sa.name))

            snap = self._make_pre_snapshot_fn(force=True, game=sa)

            def _uninstall() -> UninstallResult:
                if _is_wine_game_running():
                    raise RuntimeError(tr("cleanup.wine_running"))
                return execute_uninstall(plan, selected, snap)

            worker = SteamOpWorker(_uninstall)
            self._track_worker(worker)
            worker.finished_ok.connect(_on_done)
            worker.failed.connect(_on_err)
            worker.start()

        def _on_done(result: object) -> None:
            self._end_busy()
            if self._closing_pending or not isinstance(result, UninstallResult):
                return
            msg = tr("uninstall.done", name=sa.name, size=format_size_bytes(result.freed_bytes))
            if refresh_games or GAME_FILES in result.removed:
                self._refresh_games()
            self._refresh_shortcuts()
            self.status_label.setText(msg if not result.errors or result.removed else tr("status.ready"))
            if result.errors:
                QMessageBox.warning(
                    _dialog_parent(self), __app_name__, tr("uninstall.errors", errors="\n".join(result.errors))
                )
                if SHORTCUTS not in result.removed:
                    return
            if steam_entry is not None and self.paths is not None:
                self._remove_steam_shortcut_async(steam_entry, done_msg=msg, defer_on_cancel=True)

        planner.finished_ok.connect(_on_planned)
        planner.failed.connect(_on_err)
        planner.start()

    def _remove_existing_from_add(self) -> None:
        """Remove an existing Standalone game and/or Steam shortcut from the Add tab."""
        if self._steam_busy:
            return
        sa, steam_entry = self._find_matching_installed()
        if sa is None and steam_entry is None:
            return
        if sa is not None:
            self._uninstall_standalone_async(sa, steam_entry=steam_entry, refresh_games=True)
            return

        if steam_entry is not None and self.paths is not None:
            ans = QMessageBox.question(
                _dialog_parent(self),
                __app_name__,
                tr("library.remove_confirm", name=steam_entry.app_name),
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._remove_steam_shortcut_async(steam_entry, refresh_games=True)

    def _remove_selected_shortcut(self) -> None:
        sel = self._selected_library_item()
        if not sel:
            return
        kind, obj = sel
        if kind == "external":
            return
        if kind == "standalone":
            if isinstance(obj, StandaloneGame):
                self._uninstall_standalone_async(obj)
            return

        if kind != "steam" or not isinstance(obj, ShortcutEntry):
            return
        entry = obj
        if not self.paths or self._vdf_corrupt:
            return
        game = self._managed_library_game(entry)
        if game is not None:
            self._uninstall_standalone_async(game, steam_entry=entry)
            return
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("library.remove_confirm", name=entry.app_name),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._remove_steam_shortcut_async(entry)


def _optional_install_default(steam_installed: bool) -> QMessageBox.StandardButton:
    """Default answer for "install the extra runtimes?": optional when Steam exists, so No."""
    return QMessageBox.StandardButton.No if steam_installed else QMessageBox.StandardButton.Yes


_qt_translators: list[QTranslator] = []


def _install_qt_translator(app: QApplication, lang: str) -> bool:
    """Load Qt's own strings (Yes/No/Cancel...) for lang; looks in Qt's dir, then in the bundled one."""
    if lang == "en":
        return False
    name = f"qtbase_{lang}"
    for directory in (QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath), str(LOCALES_DIR / "qt")):
        translator = QTranslator(app)
        if translator.load(name, directory):
            app.installTranslator(translator)
            _qt_translators.append(translator)
            return True
    return False


def _qt_message_handler(mode, _context, message: str) -> None:
    logging.warning("Qt[%s]: %s", mode, message)


def main() -> int:
    """Application entry point."""
    args = sys.argv[1:]
    if "--version" in args:
        print(f"{__app_name__} {__version__}")
        return 0
    qInstallMessageHandler(_qt_message_handler)
    app = QApplication(sys.argv)
    app.setDesktopFileName("deckinstall")
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    icon_file = _resolve_asset_path("icon.png")
    if icon_file is not None:
        app.setWindowIcon(QIcon(str(icon_file)))
    install_touch_scroll(app)
    win = MainWindow(app)
    if "--smoke-test" in args:
        for w in list(win._active_workers):
            if not w.wait(5000):
                print("Smoke test failed: background worker timed out", file=sys.stderr)
                return 1
        if not win.close():
            print("Smoke test failed: MainWindow did not close cleanly", file=sys.stderr)
            return 1
        app.processEvents()
        return 0
    win.showMaximized()
    code = app.exec()
    win.settings.sync()
    return code


def exit_now(code: int) -> NoReturn:
    """End the process without Python's normal shutdown.

    At interpreter exit PySide deletes the Qt objects it still tracks; one already freed by
    Qt (seen with a QPushButton) crashed the app on close (SIGBUS/SIGSEGV in ~QObject).
    Settings are synced in main(), so only the logs and std streams need flushing here.
    """
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


def run() -> NoReturn:
    """Entry point (main.py and the "deckinstall" command): main(), then exit_now with its code.

    Exiting through os._exit also keeps running games alive: PySide's teardown would destroy
    their QProcess objects, which kills the process.
    """
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        logging.exception("Uncaught exception")
        code = 1
    exit_now(code)
