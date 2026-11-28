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
from typing import TYPE_CHECKING, Any, NoReturn, cast

if TYPE_CHECKING:
    from .ui_library_grid import LibraryGridView

from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
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
    QGridLayout,
    QHBoxLayout,
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
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__, classify, library_extras, vdf_queue
from .app_updater import (
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
from .i18n import LOCALES_DIR, set_language, tr
from .launch_presets import active_flags, apply_presets, parse_presets
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
from .mode_switch import switch_to_standalone, switch_to_steam
from .modes import STANDALONE, STEAM, ModeEnv, feature_note, is_steamos
from .play_monitor import ERROR, LOADING, RUNNING, PlayMonitor, process_start_epoch, stale_instance_pids, watch_paths
from .runtime_manager import (
    detect_gpu_profile,
    fix_bwrap_userns,
    get_umu_run_path,
    is_win32_prefix,
    list_default_proton_dirs,
    list_umu_proton_dirs,
    official_mangohud_installed,
    optiscaler_downloaded,
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
    find_existing_installs,
    game_log_dir,
    get_games_dir,
    get_legacy_games_dir,
    list_standalone_games,
    match_owned_shortcuts,
    planned_game_dir,
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
    shortcuts_for_exe,
    start_steam,
)
from .theme import ThemeMode, apply_theme
from .touch_scroll import install_touch_scroll
from .ui_cards import (  # noqa: F401
    DropZoneFrame,
    FlowLayout,
    MessageBar,
    _dialog_parent,
    _is_native_linux_game,
    _is_wine_game_running,
    _make_card,
    _pick_dir_async,
    _pick_file_async,
    _resolve_asset_path,
    _setting_bool,
    _SortableTableItem,
    format_size_bytes,
    get_game_log_path,
    shortcut_target_button,
    show_text_dialog,
)
from .ui_dialogs import (
    bulk_proton_dialog,
    open_configure_game_dialog,
    open_launch_presets_dialog,
    open_optiscaler_dialog,
    open_save_sync_dialog,
    open_scan_folders_dialog,
    open_winetricks_dialog,
    run_exe_in_prefix_dialog,
)
from .ui_dialogs.adopt_wizard import open_adopt_wizard
from .ui_dialogs.launch_presets_dialog import LaunchOptions
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
from .ui_runtimes import apply_runtimes_mode, build_runtimes_page
from .ui_workers import (  # noqa: F401
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
from .ui_workers_library import compute_library_sizes
from .uninstall import GAME_FILES, SHORTCUTS, UninstallPlan, UninstallResult, execute_uninstall, plan_uninstall
from .vdf_queue import BatchApplyResult, QueuedVdfEdit, VdfEditQueue, VdfQueueSaveError

DEFAULT_FPS_KEY = "install/default_fps"  # Settings: the frame cap new games start with
INSTALL_MODE_KEY = "install/mode"  # the mode chosen last, offered first on the next install
_VDF_WATCH_MS = 5000  # how often queued Steam edits check whether Steam has closed

# How versions before 0.32 stored the last presets (one setting each); still read once.
_OLD_LAST_PREFS_FLAGS = (
    "gamemode", "mangohud", "prime", "wined3d", "no_fsync",
)


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


PAGE_INSTALL, PAGE_LIBRARY, PAGE_SAVES, PAGE_MAINT, PAGE_RUNTIMES, PAGE_SETTINGS = range(6)
# Order of the tabs on top of the window (Library first); each maps to a page of the stack.
NAV_PAGES = (PAGE_LIBRARY, PAGE_INSTALL, PAGE_SAVES, PAGE_SETTINGS)
# Message bar keys about one tab's action; switching tabs clears them (updates and welcome stay).
_PAGE_MESSAGE_KEYS = ("notify", "scale")
# The Settings tab holds three pages, picked with the section pills under the tabs.
SETTINGS_PAGES = (PAGE_SETTINGS, PAGE_RUNTIMES, PAGE_MAINT)
SETTINGS_SECTION_KEY = "ui/settings_section"


def nav_tab_of(page: int) -> int:
    """The top tab that shows this page."""
    return NAV_PAGES.index(PAGE_SETTINGS if page in SETTINGS_PAGES else page)


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    # Built by ui_pages / ui_system (bound onto the window there).
    act_favorite: QAction
    game_action_buttons: dict[QAction, QPushButton]
    _install_mode_chosen: bool
    _standalone_launchers: tuple[bool, ...]
    _add_launch_options: str
    _add_winedlloverrides: str
    _install_mode_group: QButtonGroup
    _lib_filter_group: QButtonGroup
    _match_install_timer: QTimer
    _sandbox_prompt_open: bool
    act_ext_import_saves: QAction
    act_ext_to_steam: QAction
    add_exe_combo: QComboBox
    add_library_combo: QComboBox
    add_name: QLineEdit
    btn_adopt_games: QPushButton
    act_bulk_optiscaler: QAction
    act_bulk_proton: QAction
    btn_cancel_extract: QPushButton
    btn_configure_game: QPushButton
    btn_copy_diag: QPushButton
    btn_extract: QPushButton
    btn_install: QPushButton
    act_game_art: QAction
    lbl_sgdb_state: QLabel
    btn_all_art: QPushButton
    btn_mode_standalone: QPushButton
    btn_mode_steam: QPushButton
    act_move_game: QAction
    btn_play_game: QPushButton
    btn_redetect_steam: QPushButton
    btn_reload: QPushButton
    btn_remove_existing: QPushButton
    act_remove_game: QAction
    btn_scan: QPushButton
    btn_scan_folders: QPushButton
    default_fps_combo: QComboBox
    lbl_hardware: QLabel
    lbl_hardware_notes: QLabel
    _hardware_form: QFormLayout
    btn_shortcut_desktop: QCheckBox
    btn_shortcut_menu: QCheckBox
    btn_shortcut_steam: QCheckBox
    btn_use_extracted: QPushButton
    btn_vdf_apply: QPushButton
    btn_vdf_clear: QPushButton
    chk_keep_in_place: QCheckBox
    chk_restart: QCheckBox
    extract_details_box: QWidget
    extract_log: QTextEdit
    extract_progress: QProgressBar
    extract_src: QLineEdit
    games_list: QTableWidget
    lang_combo: QComboBox
    lbl_add_presets: QLabel
    lbl_library_count: QLabel
    lbl_mode_note: QLabel
    lbl_scan_folders_count: QLabel
    lbl_shortcuts_note: QLabel
    lbl_vdf_queue: QLabel
    library_search_input: QLineEdit
    preview_label: QLabel
    shortcuts_list: QTableWidget
    steam_match_combo: QComboBox
    steam_search_input: QLineEdit
    steam_status_lbl: QLabel
    _settings_system_form: QFormLayout
    steam_account_combo: QComboBox
    theme_combo: QComboBox
    unavailable_banner: QLabel
    vdf_error_banner: QLabel
    vdf_queue_banner: QFrame
    _maint_candidates: list[CleanupCandidate]
    _maint_checked: dict[Path, bool]
    _maint_active_filter: str
    _maint_scanned: bool
    _maint_filter_group: QButtonGroup
    maint_list: QTableWidget
    btn_maint_scan: QPushButton
    btn_maint_toggle_filter: QPushButton
    btn_maint_clean_safe: QPushButton
    btn_maint_delete_selected: QPushButton
    lbl_maint_umu: QLabel
    lbl_maint_proton: QLabel
    lbl_maint_steamrt4: QLabel
    lbl_maint_mangohud: QLabel
    lbl_maint_disk: QLabel
    lbl_maint_summary: QLabel
    btn_update_runtimes: QPushButton
    library_empty_state: QWidget
    library_no_results_state: QWidget
    message_bar: MessageBar
    scale_combo: QComboBox
    busy_bar: QProgressBar
    library_grid: LibraryGridView
    _library_search_timer: QTimer
    btn_view_list: QPushButton
    btn_view_grid: QPushButton
    # Updates card (ui_updates.build_updates_card)
    lbl_updates_status: QLabel
    btn_updates_check: QPushButton
    chk_updates_auto: QCheckBox
    updates_rows: QWidget
    updates_grid: QGridLayout
    _update_results: dict
    _update_checks: dict
    _updates_checking: bool
    _updates_cooldown: QTimer
    # Runtimes tab (ui_runtimes.py)
    btn_rt_proton_update: QPushButton
    btn_rt_umu_update: QPushButton
    btn_rt_mangohud_update: QPushButton
    lbl_rt_steam_proton: QLabel
    lbl_rt_optiscaler: QLabel
    lbl_rt_tools: QLabel
    lbl_rt_proton_note: QLabel
    lbl_rt_umu_note: QLabel
    lbl_rt_mangohud_note: QLabel
    # Backup/Sync page (ui_dialogs/save_sync_dialog.py)
    saves_list: QTableWidget
    lbl_saves_empty: QLabel
    chk_saves_orphans: QCheckBox
    spin_save_keep: QSpinBox
    btn_save_backup: QPushButton
    btn_save_restore: QPushButton
    btn_save_sync: QPushButton
    btn_save_refresh: QPushButton
    btn_save_snapshot_now: QPushButton
    btn_save_restore_selected: QPushButton
    btn_save_open_folder: QPushButton
    btn_save_export: QPushButton
    btn_save_import: QPushButton
    _after_play_running: bool = False
    _save_sources_running: bool = False
    _save_sources_pending: bool = False
    _external_save_targets: list = []  # Steam/Hydra/UMU/Nexus games for Backup/Sync (filled by a worker)
    _save_sizes: dict = {}  # slug -> bytes of saves right now

    def __init__(self, app: QApplication, *, smoke: bool = False) -> None:
        super().__init__()
        self.app = app
        # --smoke-test: start every worker, but none of them walks the mounted disks (minutes on a
        # big disk or on the CI container's bind mounts); the app only has to start and close.
        self._smoke = smoke
        self._mount_search_roots: list[Path] | None = [] if smoke else None
        self.settings = QSettings(__app_name__, __app_name__)
        self._first_run = not self.settings.allKeys()  # before anything is saved
        _install_qt_translator(app, set_language(str(self.settings.value("language", "auto"))))
        self.paths: SteamPaths | None = None
        self._steam_root_found = False
        self._steamos = is_steamos()
        self._gpu_profile_cache: object | None = None
        self._detected_games: list[DetectedGame] = []
        self._standalone_games: list[StandaloneGame] = []
        self._library_steam_games: dict[int, StandaloneGame] = {}
        self._external_games: list[ExternalLauncherGame] = []
        self._steam_shortcuts: list[ShortcutEntry] = []
        self._library_loaded = False  # the library table was filled at least once
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._closed = False
        self._relaunch_after_close: Path | None = None
        self._force_closing = False
        self._hidden_for_close = False  # closed while only read-only work ran: quit when it ends
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
        self._scan_worker: ScanWorker | None = None
        self._library_size_worker: SteamOpWorker | None = None
        self._play = PlayMonitor(self)
        self._play.changed.connect(self._on_play_changed)
        self._play.failed.connect(self._on_play_failed)
        self._play.timed_out.connect(self._on_play_timed_out)
        self._play.session_ended.connect(self._on_play_session_ended)

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1080, 700)
        self.setMinimumSize(840, 560)
        self._build_ui()
        self._setup_mount_watcher()
        self._refresh_libraries()
        self._apply_saved_theme()
        self._refresh_games()
        self._redetect_steam()
        if not smoke and self._first_run:
            from .ui_system import show_welcome

            QTimer.singleShot(800, self, lambda: show_welcome(self))
        elif not smoke:
            from .ui_updates import AUTO_CHECK_DELAY_MS, auto_check_updates

            QTimer.singleShot(AUTO_CHECK_DELAY_MS, self, lambda: auto_check_updates(self))

    _NOTIFY_SUCCESS_MS = 15000  # successes fade; info, warnings and errors stay until closed

    def notify(self, text: str, level: str = "success") -> None:
        """News that needs no answer: the message bar, not a modal box (KDE HIG inline message).

        When one of our dialogs is open on top, the bar would be hidden behind it, so the
        message goes in a box over that dialog instead.
        """
        if self._closing_pending or not text:
            return
        parent = _dialog_parent(self)
        if parent is not self:
            box = QMessageBox.warning if level in ("warning", "error") else QMessageBox.information
            box(parent, __app_name__, text)
            return
        timeout = self._NOTIFY_SUCCESS_MS if level == "success" else 0
        self.message_bar.show_message(text, level, key="notify", timeout_ms=timeout)

    def _track_worker(self, worker: QThread) -> None:
        """Keep a strong reference to running QThreads until finished to prevent Qt SIGABRT."""
        worker.setParent(self)
        self._active_workers.add(worker)

        def _cleanup() -> None:
            self._active_workers.discard(worker)
            for attr in (
                "_extract_worker",
                "_scan_worker",
                "_library_size_worker",
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
        hidden_read_only = not self.isVisible() and self._only_read_only_work()
        if hidden_read_only:  # a scan stuck on a slow disk: it only reads, so stop it without asking
            logging.warning("Read-only workers still running after close: %s", [type(w).__name__ for w in running])
        ans = (
            QMessageBox.StandardButton.Yes
            if hidden_read_only
            else QMessageBox.question(_dialog_parent(self), __app_name__, tr("status.closing_force_prompt"))
        )
        if ans == QMessageBox.StandardButton.Yes:
            for w in list(self._active_workers):
                if w.isRunning():
                    if hasattr(w, "cancel") and callable(w.cancel):
                        w.cancel()
                    else:
                        w.requestInterruption()
                    w.wait(250)
            self._force_closing = True
            self._closing_pending = False
            self.close()
        elif self._closing_pending and any(w.isRunning() for w in self._active_workers):
            if self._close_force_timer is not None:
                self._close_force_timer.start(5000)

    _READ_ONLY_CLOSE_GRACE_MS = 10000

    def _quit_if_hidden_for_close(self) -> None:
        # Closing a window that is already hidden does not end the event loop by itself.
        if self._hidden_for_close:
            QCoreApplication.quit()

    def _only_read_only_work(self) -> bool:
        """True when everything still running only reads (scans, sizes, checks): nothing to lose."""
        running = [w for w in self._active_workers if w.isRunning()]
        return bool(running) and not self._vdf_applying and all(getattr(w, "read_only", False) for w in running)

    def _defer_close_for_workers(self, event: QCloseEvent) -> None:
        """Wait for background workers before closing and arm the force-close timer.

        When they only read (the scans started with the window), the window goes away at once
        and the process ends as soon as they stop; there is nothing to wait for on screen.
        """
        self._closing_pending = True
        running = sorted(type(w).__name__ for w in self._active_workers if w.isRunning())
        read_only = self._only_read_only_work()
        logging.info(
            "Close waits for %s%s", ", ".join(running) or "a Steam write", " (window hidden)" if read_only else ""
        )
        if read_only:
            self._hidden_for_close = True
            self.hide()
        else:
            self.status_label.setText(tr("status.closing_wait"))
        if self._close_force_timer is None:
            self._close_force_timer = QTimer(self)
            self._close_force_timer.setSingleShot(True)
            self._close_force_timer.timeout.connect(self._on_close_force_timeout)
        if not self._close_force_timer.isActive():
            self._close_force_timer.start(self._READ_ONLY_CLOSE_GRACE_MS if read_only else 5000)
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            super().closeEvent(event)
            return
        if self._force_closing:
            self._closed = True
            self._play.release()
            super().closeEvent(event)
            self._quit_if_hidden_for_close()
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
        mount_timer = getattr(self, "_mount_refresh_timer", None)
        if mount_timer is not None:
            mount_timer.stop()
        updates_cd = getattr(self, "_updates_cooldown", None)
        if updates_cd is not None:
            updates_cd.stop()
        search_timer = getattr(self, "_library_search_timer", None)
        if search_timer is not None:
            search_timer.stop()
        self._play.release()  # stop following running games; they keep running
        self._closed = True
        super().closeEvent(event)
        self._quit_if_hidden_for_close()
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

        # Tabs on top: Library, Install, Backup/Sync and Maintenance (no "back" buttons).
        self.nav_tabs = QTabBar()
        self.nav_tabs.setObjectName("NavTabs")
        self.nav_tabs.setExpanding(False)
        self.nav_tabs.setDrawBase(False)
        for label in (tr("nav.library"), tr("nav.install"), tr("nav.saves"), tr("nav.settings")):
            self.nav_tabs.addTab(label)
        right_layout.addWidget(self.nav_tabs)
        self.message_bar = MessageBar()  # inline news that needs no answer (instead of a modal box)
        right_layout.addWidget(self.message_bar)
        # Sections of the Settings tab: general, updates & runtimes, cleanup.
        self.settings_sections = QWidget()
        sections_row = QHBoxLayout(self.settings_sections)
        sections_row.setContentsMargins(0, 0, 0, 0)
        sections_row.setSpacing(6)
        self._section_group = QButtonGroup(self)
        self._section_buttons: dict[int, QPushButton] = {}
        for page, label in (
            (PAGE_SETTINGS, tr("settings.section_general")),
            (PAGE_RUNTIMES, tr("nav.runtimes")),
            (PAGE_MAINT, tr("nav.maintenance")),
        ):
            pill = QPushButton(label)
            pill.setObjectName("FilterPill")
            pill.setCheckable(True)
            pill.clicked.connect(lambda _=False, p=page: self._show_page(p))
            self._section_group.addButton(pill)
            self._section_buttons[page] = pill
            sections_row.addWidget(pill)
        sections_row.addStretch()
        self.settings_sections.setVisible(False)
        right_layout.addWidget(self.settings_sections)

        self.stack = QStackedWidget()
        self.stack.addWidget(_wrap_scroll(self._build_extract_page()))  # PAGE_INSTALL
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))  # PAGE_LIBRARY
        self.stack.addWidget(_wrap_scroll(self._build_saves_page()))  # PAGE_SAVES
        runtimes_page = _wrap_scroll(build_runtimes_page(self))  # its labels are filled by the health check
        self.stack.addWidget(_wrap_scroll(self._build_maintenance_page()))  # PAGE_MAINT
        self.stack.addWidget(runtimes_page)  # PAGE_RUNTIMES
        self.stack.addWidget(_wrap_scroll(self._build_settings_page()))  # PAGE_SETTINGS
        self.stack.setCurrentIndex(PAGE_LIBRARY)  # always open on the Library (no jump after the scan)
        self.nav_tabs.setCurrentIndex(NAV_PAGES.index(PAGE_LIBRARY))
        self.nav_tabs.currentChanged.connect(self._on_nav_tab_changed)
        # Ctrl+Tab / Ctrl+Shift+Tab: next / previous tab, in a loop (after the last comes the first).
        for keys, step in ((QKeySequence.StandardKey.NextChild, 1), (QKeySequence.StandardKey.PreviousChild, -1)):
            shortcut = QShortcut(keys, self)  # every binding of the platform (Ctrl+Tab, Ctrl+F6…)
            shortcut.activated.connect(lambda s=step: self._cycle_nav_tab(s))
        self.stack.currentChanged.connect(self._sync_settings_sections)
        self.stack.currentChanged.connect(self._on_stack_tab_changed)
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        self.busy_bar = QProgressBar()  # moving bar while any background operation runs
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedSize(96, 8)
        self.busy_bar.setAccessibleName(tr("status.busy"))
        self.busy_bar.setVisible(False)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.busy_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status_label, 1)
        right_layout.addLayout(status_row)

        shell.addWidget(right)
        self.setCentralWidget(central)
        self._update_steam_mode_ui()

    def _build_settings_page(self) -> QWidget:
        from .ui_pages import build_settings_page

        return build_settings_page(self)

    def _remove_everything(self) -> None:
        from .ui_system import remove_everything

        return remove_everything(self)

    def _set_steamgriddb_key(self) -> None:
        from .ui_system import set_steamgriddb_key

        return set_steamgriddb_key(self)

    def _copy_diagnostics(self) -> None:
        from .ui_system import copy_diagnostics

        return copy_diagnostics(self)

    def _update_steam_status_style(self) -> None:
        """Apply subtle semantic color to the sidebar Steam status label."""
        name = "SteamReady" if self.paths else ("SteamWarning" if self._steam_root_found else "SteamUnavailable")
        self.steam_status_lbl.setObjectName(name)
        style = self.steam_status_lbl.style()
        style.unpolish(self.steam_status_lbl)
        style.polish(self.steam_status_lbl)

    def _show_page(self, idx: int) -> None:
        self.nav_tabs.setCurrentIndex(nav_tab_of(idx))
        self.stack.setCurrentIndex(idx)

    def _cycle_nav_tab(self, step: int) -> None:
        if self.nav_tabs.isEnabled():
            self.nav_tabs.setCurrentIndex((self.nav_tabs.currentIndex() + step) % self.nav_tabs.count())

    def _on_nav_tab_changed(self, tab: int) -> None:
        # News about the tab being left (an action's result) must not follow the user to other tabs.
        for key in _PAGE_MESSAGE_KEYS:
            self.message_bar.dismiss(key)
        page = NAV_PAGES[tab]
        if page == PAGE_SETTINGS:  # back to the section opened last
            try:
                saved = int(str(self.settings.value(SETTINGS_SECTION_KEY, PAGE_SETTINGS)))
            except ValueError:
                saved = PAGE_SETTINGS
            page = saved if saved in SETTINGS_PAGES else PAGE_SETTINGS
        self.stack.setCurrentIndex(page)

    def _sync_settings_sections(self, page: int) -> None:
        in_settings = page in SETTINGS_PAGES
        self.settings_sections.setVisible(in_settings)
        if in_settings:
            self._section_buttons[page].setChecked(True)
            self.settings.setValue(SETTINGS_SECTION_KEY, page)

    def _on_stack_tab_changed(self, idx: int) -> None:
        """Refresh Saves list or Maintenance metrics when their page opens."""
        if idx == PAGE_SAVES:
            self._refresh_saves_list()
        elif idx in (PAGE_MAINT, PAGE_RUNTIMES):
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
            self._refresh_steam_account_selector()
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

        preferred_uid = str(self.settings.value("steam/user_id", "") or "").strip() or None

        def _resolve_paths() -> SteamPaths | None:
            return resolve_steam_paths(user_id=preferred_uid)

        worker = SteamDetectWorker(
            req_id,
            shortcuts_req_id,
            resolve_steam_paths_fn=_resolve_paths,
            find_steam_root_fn=find_steam_root,
            load_shortcuts_fn=load_shortcuts,
            list_standalone_games_fn=(
                (lambda: list_standalone_games(get_games_dir())) if self._smoke else list_standalone_games
            ),
            scan_external_launcher_games_fn=scan_external_launcher_games,
        )
        self._track_worker(worker)
        worker.ready.connect(_on_detected)
        worker.failed.connect(self._on_background_failed)
        worker.start()

    def _refresh_steam_account_selector(self) -> None:
        """Populate and show the Steam account selector when multiple accounts exist in userdata/."""
        if not hasattr(self, "steam_account_combo"):
            return
        from .steam_paths import list_steam_accounts

        root = self.paths.steam_root if self.paths is not None else find_steam_root()
        accounts = list_steam_accounts(root) if root is not None else []
        show = len(accounts) > 1
        self._settings_system_form.setRowVisible(self.steam_account_combo, show)
        if not show:
            return
        saved_uid = str(self.settings.value("steam/user_id", "") or "").strip()
        combo = self.steam_account_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(tr("settings.steam_account_auto"), "")
            selected_idx = 0
            for idx, acct in enumerate(accounts, start=1):
                combo.addItem(acct.display_label, acct.user_id)
                if acct.user_id == saved_uid:
                    selected_idx = idx
            combo.setCurrentIndex(selected_idx)
        finally:
            combo.blockSignals(False)

    def _on_steam_account_changed(self, index: int) -> None:
        if not hasattr(self, "steam_account_combo") or index < 0:
            return
        uid = str(self.steam_account_combo.itemData(index) or "").strip()
        if str(self.settings.value("steam/user_id", "") or "").strip() == uid:
            return
        self.settings.setValue("steam/user_id", uid)
        self._redetect_steam()

    def _update_steam_mode_ui(self) -> None:
        """Adapt Add/Extract/Library/Maintenance controls for Steam vs Standalone-only mode."""
        self._apply_install_mode_ui(follow_default=True)
        apply_runtimes_mode(self)
        self._update_steam_status_style()

    def _build_extract_page(self) -> QWidget:
        from .ui_pages import build_extract_page

        return build_extract_page(self)

    def _build_add_page(self) -> QWidget:
        from .ui_pages import build_add_page

        return build_add_page(self)

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
            libs = discover_libraries(self._mount_search_roots)
            disks = disks_without_library(self._mount_search_roots)
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

    def _remember_shortcut_choice(self, key: str, on: bool) -> None:
        """Remember a launcher box only when the user sets it (Standalone mode), not when Steam
        mode turns the boxes off."""
        if self._selected_install_mode() == STANDALONE:
            self.settings.setValue(key, on)

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

    def _update_install_label(self) -> None:
        self.btn_install.setText(
            tr("install.btn_install_in_place") if self.chk_keep_in_place.isChecked() else tr("install.btn_install")
        )

    @property
    def mode_env(self) -> ModeEnv:
        """Which modes this system offers (SteamOS: Steam only; no Steam: Standalone only)."""
        return ModeEnv(steamos=self._steamos, steam=self.paths is not None)

    def _on_install_mode_clicked(self) -> None:
        self._install_mode_chosen = True
        self.settings.setValue(INSTALL_MODE_KEY, self._selected_install_mode())  # the default next time

    def _selected_install_mode(self) -> str:
        return STEAM if self.btn_mode_steam.isChecked() else STANDALONE

    def _apply_install_mode_ui(self, follow_default: bool = False) -> None:
        """Enable only what the chosen mode uses and say why the rest is off.

        follow_default: Steam detection changed; pick the system's default mode unless the user chose one.
        """
        env = self.mode_env
        for button, mode in ((self.btn_mode_steam, STEAM), (self.btn_mode_standalone, STANDALONE)):
            note = env.mode_note(mode)
            button.setEnabled(not note)
            button.setToolTip(note)
        checked = self._install_mode_group.checkedButton()
        last = str(self.settings.value(INSTALL_MODE_KEY, "") or "")
        preferred = last if last in (STEAM, STANDALONE) and not env.mode_note(last) else env.default_mode
        default_btn = self.btn_mode_steam if preferred == STEAM else self.btn_mode_standalone
        if (
            checked is None
            or not checked.isEnabled()
            or (follow_default and not self._install_mode_chosen and checked is not default_btn)
        ):
            default_btn.setChecked(True)
            return  # toggled() calls back with the new mode
        mode = self._selected_install_mode()
        lines = [tr("mode.desc_steam") if mode == STEAM else tr("mode.desc_standalone")]
        lines += [env.mode_note(m) for m in (STEAM, STANDALONE) if env.mode_note(m)]
        self.lbl_mode_note.setText("\n".join(lines))
        shortcuts_note = feature_note("app_shortcuts", mode) or tr("mode.note_steam_shortcut_standalone")
        launchers = (self.btn_shortcut_menu, self.btn_shortcut_desktop)
        if mode == STEAM and self.btn_shortcut_menu.isEnabled():
            # Leaving Standalone: remember its launcher choice and show them off (Steam mode has none).
            self._standalone_launchers = tuple(b.isChecked() for b in launchers)
            for box in launchers:
                box.setChecked(False)
        elif mode == STANDALONE and not self.btn_shortcut_menu.isEnabled():
            for box, was_on in zip(launchers, self._standalone_launchers, strict=True):
                box.setChecked(was_on)
        for box in launchers:
            box.setEnabled(mode == STANDALONE)
        self.btn_shortcut_steam.setChecked(mode == STEAM)  # always disabled: the mode decides it
        self.lbl_shortcuts_note.setText(shortcuts_note)
        self.lbl_shortcuts_note.setVisible(bool(shortcuts_note))
        self.chk_restart.setEnabled(mode == STEAM)
        self.chk_restart.setVisible(mode == STEAM)
        if feature_note("mangohud", mode) and active_flags(self._add_launch_options).count("mangohud"):
            try:  # Steam mode: Steam's own performance overlay replaces MangoHud
                self._add_launch_options = apply_presets(self._add_launch_options, mangohud=False)
            except ValueError:
                pass
        self._update_add_presets_summary()

    def _switch_library_subview(self, idx: int) -> None:
        """Switch to unified Install / Add Game view (Tab 0) and focus game name."""
        if idx == 1:
            self._show_page(PAGE_INSTALL)
            self.add_name.setFocus()

    def _build_library_page(self) -> QWidget:
        from .ui_pages import build_library_page

        return build_library_page(self)

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
        return build_maintenance_page(self)


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
        if self._closed:
            return  # a closed window must not keep restyling the whole application
        if str(self.theme_combo.currentData() or "auto") == "auto":
            apply_theme(self.app, "auto")

    def _on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue("theme", mode)
        apply_theme(self.app, mode)

    def _on_language_changed(self) -> None:
        code = str(self.lang_combo.currentData() or "auto")
        self.settings.setValue("language", code)
        self.notify(tr("lang.changed", name=self.lang_combo.currentText()), "info")

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
                    self.notify(tr("runtime.bwrap_fix_done"), "success")
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

    def _runtime_components_for_update_all(self) -> tuple[str, ...]:
        """What "Update all" touches: what is already installed, plus umu-run and UMU-Proton when only
        Standalone mode exists (a legacy GE-Proton counts as installed: updating brings UMU-Proton).
        Never what the system does not use (SteamOS)."""
        env = self.mode_env
        wanted: list[str] = []
        if get_umu_run_path().is_file() or not env.steam:
            wanted.append("umu")
        if list_default_proton_dirs() or not env.steam:
            wanted.append("umu_proton")
        if official_mangohud_installed():
            wanted.append("mangohud")
        if optiscaler_downloaded():
            wanted.append("optiscaler")
        return tuple(c for c in wanted if not env.component_note(c))

    def _update_runtimes(
        self,
        components: tuple[str, ...] | None = None,
        *,
        on_finished: Callable[[str, bool], None] | None = None,
    ) -> None:
        """Install/update one runtime component (a Runtimes card) or everything in use.

        With on_finished(message, ok) (the Updates card): no result box; it runs after the
        follow-up offers (Proton/OptiScaler for the games), so the caller can go on.
        """
        if self._steam_busy:
            return
        comps = components if components is not None else self._runtime_components_for_update_all()
        if not comps:
            QMessageBox.information(_dialog_parent(self), __app_name__, tr("runtimes.nothing_to_update"))
            return
        self._begin_busy()
        self.status_label.setText(tr("runtime.checking"))
        worker = RuntimeUpdateWorker(self.paths, comps)
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
            if on_finished is None:
                self.notify(msg, "success")
                from .ui_updates import recheck_if_shown

                recheck_if_shown(self)
            updated_proton = getattr(worker, "updated_proton", "")
            updated_optiscaler = getattr(worker, "updated_optiscaler", "")
            done = (lambda: on_finished(msg, True)) if on_finished is not None else None
            if any(isinstance(value, str) and value for value in (updated_proton, updated_optiscaler)):
                self._offer_runtime_game_update(
                    updated_proton if isinstance(updated_proton, str) else "",
                    optiscaler=updated_optiscaler if isinstance(updated_optiscaler, str) else "",
                    on_done=done,
                )
            elif done is not None:
                done()

        def _err(err: str) -> None:
            self._end_busy()
            msg = tr("runtime.error", err=err)
            self.status_label.setText(msg)
            if self._closing_pending:
                return
            if on_finished is not None:
                on_finished(msg, False)
            else:
                QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _offer_runtime_game_update(
        self, proton: str = "", *, optiscaler: str = "", on_done: Callable[[], None] | None = None,
    ) -> None:
        """Offer to move games to the new Proton/OptiScaler, then to review old versions; on_done
        runs after the last of these (whatever the answers)."""
        def _review_old_versions() -> None:
            if proton and not self._closing_pending:
                from .ui_maintenance import offer_maintenance_review

                offer_maintenance_review(self, "runtimes")
            if on_done is not None and not self._closing_pending:
                on_done()

        def _offer_optiscaler() -> None:
            if self._closing_pending:
                return
            if optiscaler and QMessageBox.question(
                _dialog_parent(self), __app_name__, tr("runtime.offer_optiscaler"),
            ) == QMessageBox.StandardButton.Yes:
                from .ui_dialogs.optiscaler_bulk_dialog import bulk_optiscaler_dialog

                bulk_optiscaler_dialog(self, target_version=optiscaler, on_close=_review_old_versions)
            else:
                _review_old_versions()

        if self._closing_pending:
            return
        if proton and QMessageBox.question(
            _dialog_parent(self), __app_name__, tr("runtime.offer_proton", name=proton),
        ) == QMessageBox.StandardButton.Yes:
            bulk_proton_dialog(self, preferred_tool=proton, on_close=_offer_optiscaler)
        else:
            _offer_optiscaler()

    def _download_app_update(self, info: AppUpdateInfo, on_done: Callable[[Path | None, str], None]) -> None:
        """Download, verify and swap in the new AppImage with a progress box; then
        on_done(new_path, "") or on_done(None, error). The running copy keeps working until restart."""
        self._begin_busy()
        prog_dlg = QProgressDialog(tr("app_update.downloading", latest=info.latest_version), "", 0, 100, self)
        prog_dlg.setWindowTitle(__app_name__)
        prog_dlg.setCancelButton(None)
        prog_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        prog_dlg.setAutoClose(False)
        prog_dlg.setAutoReset(False)
        prog_dlg.setValue(0)
        prog_dlg.show()
        worker = AppUpdateDownloadWorker(info)
        self._track_worker(worker)
        worker.progress.connect(prog_dlg.setValue)

        def _finish(path: Path | None, err: str) -> None:
            self._end_busy()
            prog_dlg.close()
            prog_dlg.deleteLater()
            if not self._closing_pending:
                on_done(path, err)

        worker.finished_ok.connect(lambda path: _finish(path, ""))
        worker.failed.connect(lambda err: _finish(None, err))
        worker.start()

    def _after_app_update(self, info: AppUpdateInfo, updated_path: Path, summary: str) -> None:
        """The new AppImage is in place: offer the restart (Gaming Mode: reopen from Steam)."""
        pending = self._vdf_queue.pending_count() if self.paths is not None else 0
        if is_gamescope_session():
            msg = tr("app_update.done_gamescope", latest=info.latest_version)
            if pending:
                msg += "\n\n" + tr("app_update.pending_kept", count=pending)
            self.status_label.setText(msg.splitlines()[0])
            QMessageBox.information(_dialog_parent(self), __app_name__, f"{summary}\n\n{msg}".strip())
        elif pending:
            self._ask_update_with_pending_edits(pending, updated_path, summary=summary)
        else:
            text = f"{summary}\n\n{tr('app_update.done_relaunch', latest=info.latest_version)}".strip()
            if QMessageBox.question(_dialog_parent(self), __app_name__, text) == QMessageBox.StandardButton.Yes:
                self._restart_updated_appimage(updated_path)

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
            if info.kind == classify.NATIVE_LINUX:
                self.extract_src.clear()
                self.extract_details_box.setVisible(False)
                self.btn_use_extracted.setEnabled(False)
                msg = tr("install.native_not_supported")
                self.status_label.setText(msg)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                return
            if info.kind == classify.ARCHIVE:
                self.extract_src.clear()
                self.extract_details_box.setVisible(False)
                self.btn_use_extracted.setEnabled(False)
                msg = tr("install.archive_extract_first")
                self.status_label.setText(msg)
                self.notify(msg, "info")
                return
            if info.needs_install and info.installer is not None:
                self.extract_src.setText(str(info.installer))
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
            classify.GOG_SETUP: tr("install.detected_gog"),
            classify.REPACK: tr("install.detected_repack", name=info.installer.name if info.installer else ""),
            classify.INSTALLER: tr("install.detected_installer"),
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

    def _start_extract(self) -> None:
        if self._steam_busy or self._extract_worker is not None:
            return
        src = Path(self.extract_src.text().strip())
        if not src.is_file():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("install.invalid_file"))
            return
        name = self.add_name.text().strip()
        # Installers and archives go to the library chosen in step 3 (the game name comes from step 2).
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

    def _on_extract_done(self, out_dir: str, _source_kind: str) -> None:
        self._end_busy()
        self.btn_cancel_extract.setEnabled(False)
        self.btn_use_extracted.setEnabled(True)
        self._last_extracted_dir = Path(out_dir)
        self.status_label.setText(tr("install.done_prefix", path=out_dir))
        self._refresh_games()
        if self._closing_pending or self._closed:
            return
        if self.isVisible():
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
        roots = [] if self._smoke else self._enabled_scan_roots()
        worker = ScanWorker(roots=roots, scan_fn=scan_all_games)
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
        """Remember the presets chosen last (every preset, current and future: the managed part of
        the line, without the game's own arguments) for the next install."""
        try:
            presets_only = apply_presets("", parse_presets(launch_opts), winedlloverrides="")
        except ValueError:
            return
        self.settings.setValue("last_launch_opts/has_saved", True)
        self.settings.setValue("last_launch_opts/presets", presets_only)
        self.settings.setValue("last_launch_opts/winedll", winedll.strip())

    def _load_last_launch_prefs(self) -> tuple[str, str]:
        """The presets chosen last (empty on a first install)."""
        if not _setting_bool(self.settings, "last_launch_opts/has_saved", False):
            return self._with_default_fps(""), ""
        saved = self.settings.value("last_launch_opts/presets", None)
        if saved is not None:
            try:
                opts = apply_presets(str(saved), winedlloverrides="")  # drops anything not a preset
            except ValueError:
                opts = ""
        else:  # saved by an older version, one setting per preset
            state: dict[str, object] = {
                key: _setting_bool(self.settings, f"last_launch_opts/{key}", False) for key in _OLD_LAST_PREFS_FLAGS
            }
            state["mh_level"] = _setting_int(self.settings, "last_launch_opts/mh_level", 0)
            state["mh_fps_limit"] = _setting_int(self.settings, "last_launch_opts/mh_fps_limit", 0)
            state["mh_position"] = str(self.settings.value("last_launch_opts/mh_position", "") or "")
            try:
                opts = apply_presets("", state, winedlloverrides="")
            except ValueError:
                opts = ""
        winedll = str(self.settings.value("last_launch_opts/winedll", "") or "").strip()
        return self._with_default_fps(opts), winedll

    def _with_default_fps(self, opts: str) -> str:
        """The frame cap chosen in Settings for new games ("last": whatever the last install used)."""
        choice = str(self.settings.value(DEFAULT_FPS_KEY, "last") or "last")
        if choice == "last":
            return opts
        from .ui_dialogs.launch_presets_dialog import half_rate, screen_refresh_hz

        hz = screen_refresh_hz(self)
        value = {"half": half_rate(hz) if hz else 30, "full": hz}.get(choice, int(choice) if choice.isdigit() else 0)
        try:
            return apply_presets(opts, winedlloverrides="", frame_rate=value)
        except ValueError:
            return opts

    def _reset_add_launch_options(self) -> None:
        opts, winedll = self._load_last_launch_prefs()
        self._add_launch_options = opts
        self._add_winedlloverrides = winedll
        self._update_add_presets_summary()

    def _update_add_presets_summary(self) -> None:
        names = {  # the same plain names the options have in Configure
            "gamemode": tr("opt.gamemode"),
            "mangohud": tr("opt.mangohud"),
            "prime": tr("presets.short_prime"),
            "wined3d": tr("opt.wined3d"),
            "no_fsync": tr("opt.no_fsync"),
            "debug_log": tr("opt.debug_log"),
            "local_shader_cache": tr("opt.local_shader_cache"),
            "disable_nvapi": tr("opt.disable_nvapi"),
            "hide_nvidia_gpu": tr("opt.hide_nvidia_gpu"),
            "vendor_amd": tr("opt.vendor_amd"),
            "descriptor_heap": tr("opt.descriptor_heap"),
            "dxvk_d3d8": tr("opt.dxvk_d3d8"),
            "no_d3d11": tr("opt.no_d3d11"),
            "no_ntsync": tr("opt.no_ntsync"),
            "heap_delay_free": tr("opt.heap_delay_free"),
            "old_gl_string": tr("opt.old_gl_string"),
            "integer_scaling": tr("opt.integer_scaling"),
            "no_xalia": tr("opt.no_xalia"),
        }
        active = list(dict.fromkeys(names[k] for k in active_flags(self._add_launch_options) if k in names))
        frame = int(parse_presets(self._add_launch_options).get("frame_rate") or 0)
        if frame:
            active.append(tr("presets.short_frame_rate", fps=frame))
        if self._add_winedlloverrides.strip():
            active.append(tr("presets.short_dll"))
        self.lbl_add_presets.setText(tr("add.presets_after", opts=", ".join(active) if active else tr("presets.none")))

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

    def _on_exe_changed(self, _text: str) -> None:
        """Re-check the "Remove existing" button after the executable changes."""
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
        if is_native_linux_exe(exe_path):
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("install.native_not_supported"))
            return None
        if not is_proton_exe(exe_path):
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

    def _confirm_not_duplicate(
        self, app_name: str, steam_appid: int | None, game_dir: Path, container: bool
    ) -> bool:
        """Ask before installing a game that already looks installed. True = go on."""
        matches = find_existing_installs(
            app_name, steam_appid, game_dir, self._standalone_games, container=container
        )
        if not matches:
            return True
        reason_text = {
            "folder": lambda g: tr("add.duplicate_reason_folder", name=g.name, path=str(g.game_dir)),
            "appid": lambda g: tr("add.duplicate_reason_appid", name=g.name, path=str(g.game_dir)),
            "name": lambda g: tr("add.duplicate_reason_name", name=g.name, path=str(g.game_dir)),
        }
        lines = "\n".join(reason_text[reason](game) for game, reason in matches)
        box = QMessageBox(_dialog_parent(self))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(__app_name__)
        box.setText(tr("add.duplicate_found", name=app_name, matches=lines))
        btn_copy = box.addButton(tr("add.duplicate_btn_copy"), QMessageBox.ButtonRole.AcceptRole)
        btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_cancel)
        box.exec()
        return box.clickedButton() is btn_copy

    def _install_selected(self) -> None:
        """Install the selected game into the library in the chosen mode.

        Steam mode: the game gets a Steam shortcut (Steam's Proton, no umu/UMU-Proton download, no menu or
        desktop launcher). Standalone mode: launch.sh + umu-run, plus the menu/desktop launchers picked.
        """
        if self._steam_busy:
            return
        mode = self._selected_install_mode()
        if mode == STEAM:
            if self.paths is None:
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
        create_menu = mode == STANDALONE and self.btn_shortcut_menu.isChecked()
        create_desktop = mode == STANDALONE and self.btn_shortcut_desktop.isChecked()
        steam_target = self.paths if mode == STEAM else None
        restart_steam = self.chk_restart.isChecked()
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

        if not self._confirm_not_duplicate(app_name, steam_appid, game_dir, inferred_container):
            return
        anticheat = [] if inferred_container else classify.detect_anticheat(game_dir)
        if anticheat:
            ans = QMessageBox.question(
                _dialog_parent(self), __app_name__,
                tr("add.anticheat_warning", name=app_name, systems=", ".join(anticheat)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

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
            from .runtime_manager import find_system_umu_run

            steam_only = mode == STEAM  # Steam runs it: no umu-run / UMU-Proton download
            has_umu = find_system_umu_run() is not None or get_umu_run_path().is_file()
            has_proton = bool(list_umu_proton_dirs())
            if (
                not steam_only
                and not is_native_linux_exe(exe_path)
                and (self.paths is not None or self._steam_root_found)
                and (not has_umu or not has_proton)
            ):
                # Standalone chosen while Steam exists: say what it downloads before doing it.
                ans = QMessageBox.question(
                    _dialog_parent(self),
                    __app_name__,
                    tr("add.confirm_standalone_runtime"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
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
                self.notify(msg, "success")
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
                    dst=str(planned_game_dir(games_dir, app_name, game_dir)),
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

    def _apply_shortcuts_data(
        self,
        paths_snapshot: SteamPaths | None,
        entries: list[ShortcutEntry],
        standalone_games: list[StandaloneGame],
        external_games: list[ExternalLauncherGame],
        vdf_err: str,
        runner_labels: dict[tuple[str, str], str] | None = None,
        matched: tuple[Any, Any, Any] | None = None,
    ) -> None:
        if not self._library_loaded:
            QTimer.singleShot(0, self, self._report_interrupted_moves)
        self._library_loaded = True
        if self._library_size_worker is not None:
            self._library_size_worker.requestInterruption()
        self._library_size_worker = None
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
        if self.stack.currentIndex() == PAGE_SAVES:  # otherwise it is rebuilt when the page opens
            refresh_saves_page(self, after_play=False, scan_sources=True)
        labels = (
            runner_labels
            if runner_labels is not None
            else compute_library_runner_labels(
                paths_snapshot, entries, standalone_games, external_games
            )
        )
        default_runner = tr("proton.default_label")
        # Managed games with a shortcut use the Steam entry for play and configuration.
        if matched is not None:  # computed by the refresh worker, off the UI thread
            owned, primary, repairs = matched
        else:
            owned = match_owned_shortcuts(entries, standalone_games, paths_snapshot.user_id if paths_snapshot else None)
            primary = primary_owned_shortcuts(entries, owned)
            repairs = None
        self._library_steam_games = {entry.appid_u32: owned[entry.appid_u32] for entry in primary.values()}
        standalone_dirs = [os.path.normpath(str(g.game_dir)) for g in standalone_games]
        self._queue_shortcut_repairs(
            entries, standalone_games, repairs=repairs if self.paths is paths_snapshot else None,
        )
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
        favs = library_extras.favorites(self.settings)
        for row, (name, source, runner, executable, data, key) in enumerate(rows):
            self.shortcuts_list.insertRow(row)
            item = QTableWidgetItem(("★ " + name) if key in favs else name)
            item.setData(Qt.ItemDataRole.UserRole, data)
            item.setToolTip(self._playtime_tooltip(*data))
            self.shortcuts_list.setItem(row, 0, item)
            self.shortcuts_list.setItem(row, 1, QTableWidgetItem(source))
            self.shortcuts_list.setItem(row, 2, QTableWidgetItem(runner))
            self.shortcuts_list.setItem(row, 3, QTableWidgetItem(labels.get(("api", key), "-")))
            self.shortcuts_list.setItem(row, 4, QTableWidgetItem(labels.get(("engine", key), "-")))
            item.setData(Qt.ItemDataRole.UserRole + 1, key)
            loading = _SortableTableItem(tr("library.size_loading"), -1)
            loading.setToolTip(tr("library.size_hint"))
            self.shortcuts_list.setItem(row, 5, loading)
            self.shortcuts_list.setItem(row, 6, QTableWidgetItem(executable))
        self.shortcuts_list.setSortingEnabled(True)

        self._filter_library_list()
        self._update_library_action_states()
        self._maybe_prompt_migration(standalone_games)
        if rows:
            self._measure_library_sizes(entries, standalone_games, external_games, paths_snapshot)

    def _measure_library_sizes(
        self, entries: list[ShortcutEntry], games: list[StandaloneGame],
        external: list[ExternalLauncherGame], paths: SteamPaths | None,
    ) -> None:
        worker = SteamOpWorker(lambda: compute_library_sizes(
            entries, games, external, paths, cancelled=worker.isInterruptionRequested,
        ))
        worker.read_only = True  # type: ignore[attr-defined]  # closing never waits for it on screen
        self._library_size_worker = worker
        self._track_worker(worker)

        def _finish(result: object) -> None:
            if self._closing_pending or self._library_size_worker is not worker:
                return
            sizes = result if isinstance(result, dict) else {}
            table = self.shortcuts_list
            table.setSortingEnabled(False)
            for row in range(table.rowCount()):
                name = table.item(row, 0)
                if name is None:
                    continue
                size = sizes.get(name.data(Qt.ItemDataRole.UserRole + 1))
                text = format_size_bytes(size) if size is not None else tr("library.size_unavailable")
                cell = _SortableTableItem(text, size if size is not None else -1)
                cell.setToolTip(tr("library.size_hint"))
                table.setItem(row, 5, cell)
            table.setSortingEnabled(True)
            self._filter_library_list(skip_grid=True)

        worker.finished_ok.connect(_finish)
        worker.failed.connect(lambda _err: _finish({}))
        worker.start()


    def _maybe_prompt_migration(self, standalone_games: list[StandaloneGame]) -> None:
        """Offer once per session to bring games from old DeckInstall versions into a library."""
        try:
            from .proton_policy import retarget_stale_default_games

            retarget_stale_default_games(standalone_games)
        except Exception:
            logging.exception("Could not refresh launchers with a missing Proton")
        if self._migration_prompted or self._closing_pending:
            return
        if not any(g.schema < 2 for g in standalone_games):
            return
        self._migration_prompted = True
        if _setting_bool(self.settings, "migration/dont_ask", False):
            return
        QTimer.singleShot(0, lambda: self._start_legacy_migration(from_startup=True))

    def _start_legacy_migration(self, from_startup: bool = False) -> None:
        """Plan, confirm and run the migration of v1 games into the library chosen on the Install tab."""
        if self._closing_pending or self._steam_busy or not uses_library_layout():
            return
        paths = self.paths
        lib_root = self._target_library_root()
        self._begin_busy()

        def _inspect() -> list[MigrationItem] | None:
            return None if _is_wine_game_running() else plan_migration(paths, lib_root)

        def _planned(result: object) -> None:
            self._end_busy()
            if self._closed or self._closing_pending or self.paths != paths:
                return
            if isinstance(result, list):
                self._confirm_legacy_migration(result, from_startup, paths, lib_root)
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

    def _target_library_root(self) -> Path:
        """The library chosen last on the Install tab (nothing is created here)."""
        data = str(self.add_library_combo.currentData() or "") if hasattr(self, "add_library_combo") else ""
        kind, _, raw = data.partition(":")
        if kind == "lib" and raw:
            return Path(raw)
        if kind == "disk" and raw:
            return Path(raw) / "DeckInstall"
        return internal_library_root()

    def _confirm_legacy_migration(
        self, plan: list[MigrationItem], from_startup: bool, paths: SteamPaths | None, lib_root: Path,
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
                self.notify(text, "info")
            return
        names = "\n".join(f"• {i.game.name}" for i in todo)
        text = tr(
            "migrate.confirm", count=len(todo), dest=str(lib_root), old=str(get_legacy_games_dir()), names=names
        )
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
            return migrate_legacy_games(todo, paths, lib_root)

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
                matched=(worker.owned, worker.primary, worker.repairs) if worker.owned is not None else None,
            )
            if worker.sandbox_blocked and paths_snapshot is not None:
                self._offer_steam_sandbox_access(paths_snapshot.steam_root, list(worker.sandbox_blocked))

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

    def _offer_steam_sandbox_access(self, steam_root: Path, folders: list[Path]) -> None:
        from .ui_system import offer_steam_sandbox_access

        return offer_steam_sandbox_access(self, steam_root, folders)

    def _queue_shortcut_repairs(
        self, entries: list[ShortcutEntry], games: list[StandaloneGame], *, repairs: list[Any] | None = None,
    ) -> None:
        """Steam shortcuts of adopted/moved games still pointing at the old folder (Steam was open):
        queue the fix, applied as soon as Steam is closed (AppID, artwork and playtime are kept)."""
        if self.paths is None or self._vdf_corrupt or self._vdf_applying:
            return
        pending = {e.edit_id: e for e in self._vdf_queue.list_pending()}
        found = repairs if repairs is not None else shortcut_path_repairs(entries, games, self.paths.user_id)
        for entry, game in found:
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

    def _filter_library_list(self, *, skip_grid: bool = False) -> None:
        """Filter Steam, Standalone, and External library items live and update the count label."""
        source_filter = getattr(self, "_library_source_filter", "all")
        favs = library_extras.favorites(self.settings) if source_filter == "favorites" else set()
        query = self.library_search_input.text().strip().lower()
        total = 0
        shown = 0
        table = self.shortcuts_list
        table.setUpdatesEnabled(False)
        try:
            col_count = table.columnCount()
            for i in range(table.rowCount()):
                item = table.item(i, 0)
                if item is None:
                    continue
                total += 1
                data = item.data(Qt.ItemDataRole.UserRole)
                kind = data[0] if isinstance(data, tuple) and len(data) == 2 else ""
                if source_filter == "favorites":
                    source_ok = item.data(Qt.ItemDataRole.UserRole + 1) in favs
                else:
                    source_ok = source_filter == "all" or kind == source_filter
                query_ok = not query or any(
                    query in cell.text().lower()
                    for column in range(col_count)
                    if (cell := table.item(i, column)) is not None
                )
                visible = source_ok and query_ok
                table.setRowHidden(i, not visible)
                if visible:
                    shown += 1
        finally:
            table.setUpdatesEnabled(True)
        cur_row = table.currentRow()
        if cur_row >= 0 and table.isRowHidden(cur_row):
            table.clearSelection()
            table.setCurrentCell(-1, -1)
        self._update_library_action_states()
        self.lbl_library_count.setText(tr("library.count_summary", shown=shown, total=total))
        empty = total == 0 and self._library_loaded  # not while the first detection still runs
        no_results = not empty and total > 0 and shown == 0
        self.library_empty_state.setVisible(empty)
        self.library_no_results_state.setVisible(no_results)
        from .ui_library_grid import sync_views

        sync_views(self, empty or no_results, skip_grid=skip_grid)

    def _clear_library_filters(self) -> None:
        """Reset search text and source filter back to All."""
        self.library_search_input.blockSignals(True)
        self.library_search_input.clear()
        self.library_search_input.blockSignals(False)
        self._library_search_timer.stop()
        self._library_source_filter = "all"
        buttons = self._lib_filter_group.buttons()
        if buttons:
            buttons[0].setChecked(True)
        self._filter_library_list()

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
        row_key = self._selected_row_key()
        self.act_favorite.setEnabled(bool(row_key))
        self.act_game_art.setEnabled(bool(row_key) and not self._steam_busy)
        self.act_favorite.setChecked(bool(row_key) and row_key in library_extras.favorites(self.settings))
        managed = self._managed_library_game(sel[1]) if sel is not None else None
        self.act_move_game.setEnabled(managed is not None and not self._steam_busy)

        # Hide mutually exclusive per-game actions instead of merely disabling them
        self.act_ext_to_steam.setVisible(is_external)
        self.act_ext_import_saves.setVisible(is_external)
        ext_prefix = getattr(sel[1], "prefix_dir", None) if is_external and sel is not None else None
        has_ext_prefix = (
            is_external
            and ext_prefix is not None
            and Path(ext_prefix).is_dir()
        )

        self.act_ext_to_steam.setEnabled(is_external and can_write_vdf)
        self.act_ext_import_saves.setEnabled(idle and has_ext_prefix)
        self.act_remove_game.setEnabled((is_steam and can_write_vdf) or (idle and is_standalone))
        self.act_remove_game.setVisible(not is_external)

    def should_queue_vdf_edits(self) -> bool:
        """Gaming Mode cannot close Steam: Steam edits wait in the saved queue instead of applying now."""
        return self.paths is not None and is_gamescope_session()

    def enqueue_vdf_edit(self, edit: QueuedVdfEdit, *, trigger_watch: bool = True) -> bool:
        """Enqueue a VDF edit; it is applied once Steam is closed.

        Returns False (after telling the user) when the queue could not be saved: the edit is
        then not pending, so callers must not announce it as waiting.
        """
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
        try:
            self._vdf_queue.enqueue(edit)
        except VdfQueueSaveError as exc:
            msg = tr("vdf_queue.save_failed", name=edit.label or str(edit.appid_u32), err=str(exc))
            self.status_label.setText(msg.splitlines()[0])
            if not self._closing_pending:
                QMessageBox.warning(_dialog_parent(self), __app_name__, msg)
            return False
        self._vdf_auto_paused = False
        self._update_vdf_queue_banner()
        if trigger_watch:
            self._vdf_watch_tick()
        return True

    def _report_interrupted_moves(self) -> None:
        """A game move that did not finish (app closed, power lost): say where its files are."""
        from .relocate import interrupted_moves

        try:
            moves = interrupted_moves()
        except Exception as exc:  # only a hint
            logging.warning("Could not check interrupted moves: %s", exc)
            return
        for move in moves:
            msg = tr(
                "relocate.interrupted", name=move.name or "?", step=move.step,
                old=str(move.old_dir), new=str(move.new_dir), journal=str(move.journal),
            )
            logging.warning("%s", msg)
            if not self._closing_pending:
                self.notify(msg, "warning")

    def _report_vdf_queue_load_problem(self) -> None:
        """Tell the user once when the saved Steam edit queue could not be read as it was."""
        problem = self._vdf_queue.load_problem
        if problem is None:
            return
        self._vdf_queue.load_problem = None
        kept = str(problem.preserved) if problem.preserved is not None else tr("vdf_queue.load_not_kept")
        if problem.kind == "future_version":
            msg = tr("vdf_queue.load_future", kept=kept)
        elif problem.kind == "invalid_entries":
            msg = tr("vdf_queue.load_partial", kept_count=problem.kept, dropped=problem.dropped, kept=kept)
        else:
            msg = tr("vdf_queue.load_unreadable", kept=kept)
        logging.warning("Steam edit queue: %s", msg)
        self.status_label.setText(msg.splitlines()[0])
        self.notify(msg, "warning")

    def _on_vdf_queue_paths_ready(self) -> None:
        """Steam was (re)detected: pick up edits saved earlier (e.g. in Gaming Mode) and apply if Steam is closed."""
        if self._busy_count == 0:  # never swap the list under a running apply
            self._vdf_queue.reload()
        self._report_vdf_queue_load_problem()
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
            res = queue.apply_all(paths, steam_running=is_steam_running)  # checked again under the locks
            return None if getattr(res, "steam_running", False) else res

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
            self._review_queued_game_removals(res)
            applied = getattr(res, "applied", 0)
            errors = list(getattr(res, "errors", []))
            confirm_error = str(getattr(res, "confirm_error", "") or "")
            if confirm_error:
                errors.append(tr("vdf_queue.confirm_failed", err=confirm_error))
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

    def _review_queued_game_removals(self, result: object) -> None:
        if self._closing_pending or not isinstance(result, BatchApplyResult) or not result.removed_for_review:
            return

        def _review() -> None:
            from .ui_maintenance import offer_maintenance_review

            offer_maintenance_review(
                self, "games",
                details="\n".join(name for _, name in result.removed_for_review),
                appids={appid for appid, _ in result.removed_for_review},
            )

        QTimer.singleShot(0, self, _review)

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
        try:
            self._vdf_queue.clear()
        except VdfQueueSaveError as exc:
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("vdf_queue.clear_failed", err=str(exc)))
            return
        self._update_vdf_queue_banner()
        self.status_label.setText(tr("vdf_queue.cleared"))

    def _restart_updated_appimage(self, updated_path: Path) -> None:
        if self._closed:
            return
        self._relaunch_after_close = updated_path
        self._skip_vdf_close_prompt = True
        self.close()

    def _ask_update_with_pending_edits(self, pending: int, updated_path: Path, *, summary: str = "") -> None:
        """Before restarting into the new version, offer to send the queued edits to Steam."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(__app_name__)
        box.setText(f"{summary}\n\n{tr('app_update.pending_prompt', count=pending)}".strip())
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
                if on_done is None:
                    self._review_queued_game_removals(res)
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
                        self.notify(msg, "success")

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
        existing = shortcuts_for_exe(list(getattr(self, "_steam_shortcuts", []) or []), ext.exe_path)
        if existing:
            ans = QMessageBox.question(
                _dialog_parent(self), __app_name__,
                tr("library.ext_steam_duplicate", name=ext.name, existing=", ".join(e.app_name for e in existing)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

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
                self.notify(msg, "info")
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
                    self.notify(msg, "success")

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
            self.notify(tr("library.ext_no_prefix"), "info")
            return

        targets: list[tuple[str, Path]] = []
        if self.paths is not None:
            for sc in self._steam_shortcuts:
                targets.append((f"[Steam] {sc.app_name}", self.paths.compatdata_dir / str(sc.appid_u32) / "pfx"))
        for sg in self._standalone_games:
            if not sg.native_linux:
                targets.append((f"{tr('library.tag_standalone')} {sg.name}", sg.prefix_dir))

        if not targets:
            self.notify(tr("library.ext_no_import_targets"), "info")
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
                self.notify(msg, "success")

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

    def _selected_row_key(self) -> str:
        row = self.shortcuts_list.currentRow()
        item = self.shortcuts_list.item(row, 0) if row >= 0 else None
        key = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        return str(key) if key else ""

    def _toggle_favorite(self) -> None:
        key = self._selected_row_key()
        if not key:
            return
        now = library_extras.toggle_favorite(self.settings, key)
        row = self.shortcuts_list.currentRow()
        item = self.shortcuts_list.item(row, 0)
        if item is not None:
            text = item.text().removeprefix("★ ")
            item.setText(("★ " + text) if now else text)
        self.act_favorite.setChecked(now)
        self._filter_library_list()

    def _focus_library_search(self) -> None:
        """Ctrl+F: go to the Library and put the cursor in its search box."""
        self.nav_tabs.setCurrentIndex(NAV_PAGES.index(PAGE_LIBRARY))
        self.library_search_input.setFocus()
        self.library_search_input.selectAll()

    def _playtime_tooltip(self, kind: str, obj: object) -> str:
        total, last = library_extras.playtime(self.settings, self._play_key(kind, obj))
        if total <= 0:
            return ""
        when = QDateTime.fromSecsSinceEpoch(int(last)).toString("dd/MM/yyyy HH:mm") if last else "-"
        return tr("library.playtime_tip", time=library_extras.format_duration(total), last=when)

    def _on_play_session_ended(self, key: str, seconds: float) -> None:
        library_extras.add_session(self.settings, key, seconds)
        for row in range(self.shortcuts_list.rowCount()):
            item = self.shortcuts_list.item(row, 0)
            data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if item is not None and isinstance(data, tuple) and self._play_key(*data) == key:
                item.setToolTip(self._playtime_tooltip(*data))

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
                self.notify(fb_msg, "warning")
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

        def _on_inhibit_unavailable(reason: str) -> None:
            if self._closing_pending or getattr(self, "_inhibit_warned", False):
                return  # said once per session: the launcher handles it every time anyway
            self._inhibit_warned = True
            msg = tr("play.inhibit_unavailable", name=title_name, reason=reason)
            logging.warning("%s", msg)
            self.notify(msg, "warning")

        worker.inhibit_unavailable.connect(_on_inhibit_unavailable)
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
        self.busy_bar.setVisible(not enabled)
        for w in (
            self.btn_redetect_steam,
            self.btn_reload,
            self.btn_install,
            self.shortcuts_list,
            self.btn_play_game,
            self.btn_configure_game,
            self.act_favorite,
            self.act_move_game,
            self.act_remove_game,
            self.act_ext_import_saves,
            self.btn_save_backup,
            self.btn_save_restore,
            self.btn_save_sync,
            self.btn_save_refresh,
            self.btn_adopt_games,
            self.btn_all_art,
            self.act_bulk_optiscaler,
            self.act_bulk_proton,
            self.btn_maint_scan,
            self.btn_maint_clean_safe,
            self.btn_maint_delete_selected,
            self.btn_maint_toggle_filter,
        ):
            w.setEnabled(enabled)
        for name in (
            "btn_extract",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(enabled)
        if hasattr(self, "_update_checks"):  # its buttons also depend on the selection and the check
            from .ui_updates import _sync_apply_button, _sync_check_button

            _sync_apply_button(self)
            _sync_check_button(self)
        can_vdf = enabled and bool(self.paths) and not self._vdf_corrupt
        for name in ("btn_vdf_apply", "btn_vdf_clear"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(can_vdf)
        for b in save_row_buttons(self):
            b.setEnabled(enabled)
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
        mode: str | None = None,
        proton: str = "",
        *,
        proton_picker: QComboBox | None = None,
        more: Callable[[QWidget, QVBoxLayout, LaunchOptions], None] | None = None,
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
            mode=mode,
            proton=proton,
            proton_picker=proton_picker,
            more=more,
        )

    def _open_winetricks_dialog(
        self, kind: str | None = None, obj: object | None = None, preset_verbs: list[str] | None = None
    ) -> None:
        open_winetricks_dialog(self, kind=kind, obj=obj, preset_verbs=preset_verbs)

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
            self.notify(msg, "success")

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
                if worker.verified_for_cleanup:
                    self._offer_imported_backup_cleanup(zpath)
                else:
                    self.notify(msg, "success")

        def _on_restore_err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("restore.error", err=err))

        worker = RestoreWorker(self.paths, zpath)
        self._track_worker(worker)
        worker.done.connect(_on_restore_done)
        worker.failed.connect(_on_restore_err)
        worker.start()

    def _offer_imported_backup_cleanup(
        self, backup: Path, *, remove: Callable[[], bool] | None = None,
    ) -> None:
        from .steam_vdf import remove_imported_save_backup

        if QMessageBox.question(
            _dialog_parent(self), __app_name__, tr("restore.offer_remove_imported", path=str(backup)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        paths = self.paths

        def _remove() -> bool:
            if _is_wine_game_running():
                raise RuntimeError(tr("cleanup.wine_running"))
            return remove() if remove is not None else remove_imported_save_backup(paths, backup)

        def _done(removed: object) -> None:
            self._end_busy()
            if self._closing_pending:
                return
            self._refresh_saves_list()
            if removed is True:
                self.status_label.setText(tr("restore.imported_backup_removed", name=backup.name))
            else:
                QMessageBox.warning(_dialog_parent(self), __app_name__, tr("restore.imported_backup_kept"))

        def _failed(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.warning(_dialog_parent(self), __app_name__, err)

        self._begin_busy()
        worker = SteamOpWorker(_remove)
        self._track_worker(worker)
        worker.finished_ok.connect(_done)
        worker.failed.connect(_failed)
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
                steamos=self._steamos,
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
            queued = sum(1 for edit in edits if self.enqueue_vdf_edit(edit))
            if queued:
                self.status_label.setText(tr("add.steam_shortcuts_queued", count=queued))
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

    def _switch_game_mode(
        self, game: StandaloneGame, target: str, on_done: Callable[[], None] | None = None,
    ) -> None:
        """Move a managed game to Steam or Standalone mode, leaving nothing of the old mode behind."""
        parent = _dialog_parent(self)
        if self._steam_busy:
            return
        blocked = self.mode_env.mode_note(target)
        if blocked:
            QMessageBox.warning(parent, __app_name__, blocked)
            return
        if self.paths is not None and self._vdf_corrupt:
            QMessageBox.critical(
                parent, __app_name__, self.vdf_error_banner.text() or tr("library.vdf_error", err="shortcuts.vdf"),
            )
            return
        if self.should_queue_vdf_edits():
            QMessageBox.warning(parent, __app_name__, tr("mode.switch_gaming_mode"))
            return
        if _is_wine_game_running() or self._play.state(self._play_key("standalone", game)) in (LOADING, RUNNING):
            QMessageBox.warning(parent, __app_name__, tr("cleanup.wine_running"))
            return
        if target == STEAM and not game.native_linux and self._warn_if_win32_prefix(game.prefix_dir):
            return
        question = (
            tr("mode.switch_confirm_steam", name=game.name)
            if target == STEAM
            else tr("mode.switch_confirm_standalone", name=game.name)
        )
        if QMessageBox.question(parent, __app_name__, question) != QMessageBox.StandardButton.Yes:
            return
        paths = self.paths

        def _proceed() -> None:
            if self._closing_pending:
                return
            if target == STEAM:
                assert paths is not None
                worker = SteamOpWorker(lambda: switch_to_steam(paths, game), need_steam_closed=True)
            else:
                from .runtime_manager import find_system_umu_run

                needs_runtime = not game.native_linux and (
                    (find_system_umu_run() is None and not get_umu_run_path().is_file())
                    or not list_umu_proton_dirs()
                )

                def _to_standalone() -> object:
                    if needs_runtime:  # first, so a failed download changes nothing
                        from . import runtime_manager as rm

                        say = worker.status.emit
                        say(tr("add.preparing_runtime"))
                        rm.ensure_umu_run(progress_cb=lambda msg, _pct: say(msg))
                        rm.ensure_umu_database(timeout=15)
                        rm.ensure_umu_proton(progress_cb=lambda msg, _pct: say(msg))
                    return switch_to_standalone(paths, game)

                worker = SteamOpWorker(_to_standalone, need_steam_closed=paths is not None)
            self._begin_busy()
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(_res: object) -> None:
                self._end_busy()
                self._refresh_shortcuts()
                mode_name = tr("mode.steam") if target == STEAM else tr("mode.standalone")
                self.status_label.setText(tr("mode.switched", name=game.name, mode=mode_name))
                if on_done is not None and not self._closing_pending:
                    on_done()

            def _err(err: str) -> None:
                self._end_busy()
                if not self._closing_pending:
                    QMessageBox.critical(parent, __app_name__, tr("mode.switch_failed", err=err))

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if paths is None:
            _proceed()
        else:
            self._confirm_close_steam_async(_proceed)

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
        *, defer_on_cancel: bool = False, review: bool = True,
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
                    {"review_cleanup": review},
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
                if removed and review and not self._closing_pending:
                    from .ui_maintenance import offer_maintenance_review

                    offer_maintenance_review(
                        self, "games", details=entry.app_name, appids={entry.appid_u32},
                    )

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
            def _finish_shortcut() -> None:
                if steam_entry is not None and self.paths is not None:
                    self._remove_steam_shortcut_async(
                        steam_entry, done_msg=msg, defer_on_cancel=True, review=result.remaining is None,
                    )

            remaining = result.remaining
            if remaining is None:
                _finish_shortcut()
                return
            selected = ask_uninstall(_dialog_parent(self), remaining)
            if not selected:
                _finish_shortcut()
                return

            snapshot = self._make_pre_snapshot_fn(force=True, game=sa)

            def _remove_leftovers() -> UninstallResult:
                if _is_wine_game_running():
                    raise RuntimeError(tr("cleanup.wine_running"))
                return execute_uninstall(remaining, selected, snapshot)

            def _leftovers_done(value: object) -> None:
                self._end_busy()
                if self._closing_pending or not isinstance(value, UninstallResult):
                    return
                self._refresh_shortcuts()
                if GAME_FILES in value.removed:
                    self._refresh_games()
                if value.errors:
                    QMessageBox.warning(
                        _dialog_parent(self), __app_name__, tr("uninstall.errors", errors="\n".join(value.errors)),
                    )
                else:
                    self.status_label.setText(
                        tr(
                            "uninstall.done", name=sa.name,
                            size=format_size_bytes(result.freed_bytes + value.freed_bytes),
                        )
                    )
                _finish_shortcut()

            self._begin_busy()
            self.status_label.setText(tr("cleanup.scanning"))
            cleanup_worker = SteamOpWorker(_remove_leftovers)
            self._track_worker(cleanup_worker)
            cleanup_worker.finished_ok.connect(_leftovers_done)
            cleanup_worker.failed.connect(_on_err)
            cleanup_worker.start()

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


_SMOKE_WAIT_S = 20.0


def _wait_for_workers(app: QApplication, win: MainWindow, timeout_s: float) -> bool:
    """Wait until every worker is done, including ones started by another worker's result.

    A worker leaves _active_workers when its queued `finished` is handled, after its results;
    an empty set means every result was delivered and no handler started anything new.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        app.processEvents()
        if not win._active_workers:
            return True
        if time.monotonic() >= deadline:
            return False
        running = [w for w in win._active_workers if w.isRunning()]
        if running:
            running[0].wait(100)
        else:
            time.sleep(0.01)  # finished, its queued signals not handled yet


def main() -> int:
    """Application entry point."""
    args = sys.argv[1:]
    if "--version" in args:
        print(f"{__app_name__} {__version__}")
        return 0
    if "--doctor" in args:
        from .doctor import main as doctor_main

        return doctor_main(args)
    qInstallMessageHandler(_qt_message_handler)
    from .ui_scale import SCALE_KEY, clear_scale_env, resolve_scale, set_scale_env

    scale_choice = str(QSettings(__app_name__, __app_name__).value(SCALE_KEY, "auto") or "auto")
    scale_set = set_scale_env(resolve_scale(scale_choice, gaming_mode=is_gamescope_session(), env=os.environ))
    app = QApplication(sys.argv)
    clear_scale_env(scale_set)
    app.setDesktopFileName("deckinstall")
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    icon_file = _resolve_asset_path("icon.png")
    if icon_file is not None:
        app.setWindowIcon(QIcon(str(icon_file)))
    install_touch_scroll(app)
    smoke = "--smoke-test" in args
    win = MainWindow(app, smoke=smoke)
    if smoke:
        if not _wait_for_workers(app, win, _SMOKE_WAIT_S):
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
