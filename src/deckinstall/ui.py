"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
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
    MigrationResult,
    latest_undoable_journal,
    migrate_legacy_games,
    plan_migration,
    undo_migration,
)
from .play_monitor import ERROR, LOADING, RUNNING, PlayMonitor, watch_paths
from .runtime_manager import (
    detect_gpu_profile,
    fix_bwrap_userns,
    is_win32_prefix,
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
    game_log_dir,
    get_games_dir,
    import_standalone_to_steam,
    list_standalone_games,
    shortcut_game_dir,
    update_standalone_launch_options,
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
    infer_game_root_from_exe,
    load_shortcuts,
    set_shortcut_proton,
    shutdown_steam,
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
    show_text_dialog,
)
from .ui_cards import (
    _parse_launch_preset_state as _raw_parse_launch_preset_state,
)
from .ui_dialogs import (
    bulk_migrate_proton_dialog,
    edit_launch_options_presets,
    open_configure_game_dialog,
    open_launch_presets_dialog,
    open_optiscaler_dialog,
    open_proton_manager_dialog,
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
    ProtonPrepWorker,
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
from .uninstall import GAME_FILES, UninstallPlan, UninstallResult, execute_uninstall, plan_uninstall
from .vdf_queue import QueuedVdfEdit, VdfEditQueue


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


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    _maint_candidates: list[CleanupCandidate]
    _maint_checked: dict[Path, bool]
    _maint_active_filter: str
    _maint_scanned: bool
    _maint_filter_group: QButtonGroup
    maint_list: QTableWidget
    btn_manage_protons: QPushButton
    btn_migrate_legacy: QPushButton
    btn_undo_migration: QPushButton
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
    lbl_maint_disk: QLabel
    lbl_maint_summary: QLabel
    btn_update_runtimes: QPushButton
    btn_app_update: QPushButton
    # Backup/Sync page (ui_dialogs/save_sync_dialog.py)
    saves_list: QTableWidget
    lbl_saves_empty: QLabel
    chk_save_auto: QCheckBox
    chk_save_after_play: QCheckBox
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
        self._external_games: list[ExternalLauncherGame] = []
        self._steam_shortcuts: list[ShortcutEntry] = []
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._force_closing = False
        self._close_force_timer: QTimer | None = None
        self._busy_count = 0
        self._vdf_corrupt = False
        self._initial_tab_decided = False
        self._migration_prompted = False
        self._vdf_queue = VdfEditQueue(vdf_queue.default_queue_file())
        self._vdf_steam_open = False  # last poll saw Steam running while edits were queued
        self._vdf_checking = False
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

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_closing:
            self._play.release()
            super().closeEvent(event)
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
            if clicked == btn_cancel or clicked not in (btn_apply, btn_later):
                event.ignore()
                return
            if clicked == btn_apply:
                if self.paths is None:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.steam_missing"))
                    event.ignore()
                    return
                was_running = False
                apply_failed = False
                try:
                    was_running = is_steam_running()
                    if was_running and not shutdown_steam():
                        # Steam rewrites its VDFs on exit; writing now would be silently lost.
                        was_running = False
                        raise RuntimeError(tr("add.close_steam_timeout"))
                    batch_res = self._vdf_queue.apply_all(self.paths)
                    if batch_res.errors:
                        apply_failed = True
                        QMessageBox.warning(
                            _dialog_parent(self),
                            __app_name__,
                            tr(
                                "vdf_queue.applied_errors",
                                applied=batch_res.applied,
                                errs="\n".join(batch_res.errors),
                            ),
                        )
                except Exception as exc:
                    apply_failed = True
                    QMessageBox.warning(_dialog_parent(self), __app_name__, str(exc))
                finally:
                    if was_running and not start_steam():
                        logging.warning("Steam did not start again after applying the VDF queue on close")
                self._update_vdf_queue_banner()
                if apply_failed or self._vdf_queue.pending_count() > 0:
                    event.ignore()
                    return
        for w in list(self._active_workers):
            if hasattr(w, "cancel") and callable(w.cancel):
                w.cancel()
            else:
                w.requestInterruption()
        if any(w.isRunning() for w in self._active_workers):
            self._closing_pending = True
            self.status_label.setText(tr("status.closing_wait"))
            if self._close_force_timer is None:
                self._close_force_timer = QTimer(self)
                self._close_force_timer.setSingleShot(True)
                self._close_force_timer.timeout.connect(self._on_close_force_timeout)
            if not self._close_force_timer.isActive():
                self._close_force_timer.start(5000)
            event.ignore()
            return
        if self._close_force_timer is not None:
            self._close_force_timer.stop()
        self._auto_art_timer.stop()
        self._play.release()  # stop following running games; they keep running
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("ContentArea")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.addWidget(self._build_sidebar())

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

        self.stack = QStackedWidget()
        self.stack.addWidget(_wrap_scroll(self._build_extract_page()))
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))
        self.stack.addWidget(_wrap_scroll(self._build_saves_page()))
        self.stack.addWidget(_wrap_scroll(self._build_maintenance_page()))
        self.stack.currentChanged.connect(self._on_stack_tab_changed)
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        right_layout.addWidget(self.status_label)

        main_splitter.addWidget(right)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([225, 755])
        shell.addWidget(main_splitter)
        self.setCentralWidget(central)
        self._update_steam_mode_ui()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(180)
        sidebar.setMaximumWidth(360)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        icon_lbl = QLabel()
        icon_path = _resolve_asset_path("icon.png")
        if icon_path is not None:
            pix = QPixmap(str(icon_path)).scaled(
                36, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            icon_lbl.setPixmap(pix)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel(f"<b>{__app_name__}</b>")
        title.setStyleSheet("font-size: 15px;")
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("MutedText")
        brand_text.addWidget(title)
        brand_text.addWidget(ver)
        brand_row.addWidget(icon_lbl)
        brand_row.addSpacing(8)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(18)

        self._nav_buttons: list[QPushButton] = []
        nav_items = [
            tr("tabs.install"),
            tr("tabs.library"),
            tr("tabs.saves"),
            tr("tabs.maintenance"),
        ]
        for idx, label in enumerate(nav_items):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.clicked.connect(lambda _=False, i=idx: self.stack.setCurrentIndex(i))
            layout.addWidget(btn)
            self._nav_buttons.append(btn)
        self._nav_buttons[0].setChecked(True)

        layout.addStretch()

        self.steam_status_lbl = QLabel(self._format_steam_status_text())
        self.steam_status_lbl.setObjectName("MutedText")
        self.steam_status_lbl.setWordWrap(True)
        layout.addWidget(self.steam_status_lbl)

        self.btn_redetect_steam = QPushButton(tr("sidebar.redetect_steam"))
        self.btn_redetect_steam.clicked.connect(self._redetect_steam)
        layout.addWidget(self.btn_redetect_steam)

        theme_lbl = QLabel(tr("header.theme"))
        theme_lbl.setObjectName("MutedText")
        layout.addWidget(theme_lbl)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem(tr("header.theme.auto"), "auto")
        self.theme_combo.addItem(tr("header.theme.dark"), "dark")
        self.theme_combo.addItem(tr("header.theme.light"), "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        layout.addWidget(self.theme_combo)

        lang_lbl = QLabel(tr("header.language"))
        lang_lbl.setObjectName("MutedText")
        layout.addWidget(lang_lbl)
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(tr("header.language.auto"), "auto")
        for code, disp in available_languages():
            self.lang_combo.addItem(disp, code)
        saved_lang = str(self.settings.value("language", "auto"))
        lidx = self.lang_combo.findData(saved_lang)
        if lidx >= 0:
            self.lang_combo.setCurrentIndex(lidx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        layout.addWidget(self.lang_combo)

        btn_about = QPushButton(tr("header.about"))
        btn_about.clicked.connect(self._show_about_dialog)
        layout.addWidget(btn_about)
        self._update_steam_status_style()

        sidebar_scroll = QScrollArea()
        sidebar_scroll.setObjectName("Sidebar")
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.setMinimumWidth(180)
        sidebar_scroll.setMaximumWidth(360)
        sidebar_scroll.setWidget(sidebar)
        return sidebar_scroll

    def _update_steam_status_style(self) -> None:
        """Apply subtle semantic color to the sidebar Steam status label."""
        if self.paths:
            color = "#4caf50"
        elif self._steam_root_found:
            color = "#ffb74d"
        else:
            color = "#64b5f6"
        self.steam_status_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _on_stack_tab_changed(self, idx: int) -> None:
        """Refresh Saves list or Maintenance metrics when switching tabs."""
        if idx == 2:
            self._refresh_saves_list()
        elif idx == 3:
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
        self.lbl_steam_opts_group.setVisible(has_steam)
        self.chk_proton.setVisible(has_steam)
        self.chk_restart.setVisible(has_steam)
        self.btn_add_steam.setObjectName("primaryBtn" if has_steam else "")
        self.btn_add_standalone.setObjectName("" if has_steam else "primaryBtn")
        for btn in (self.btn_add_steam, self.btn_add_standalone):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self.btn_bulk_proton.setVisible(has_steam)
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
        self.add_no_steam_banner.setStyleSheet(
            "background-color: rgba(61, 174, 233, 0.14); "
            "border: 1px solid rgba(61, 174, 233, 0.55); border-radius: 6px; padding: 8px;"
        )
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
        self.add_name.textChanged.connect(self._update_remove_existing_state)
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

        targets_row = QHBoxLayout()
        self.chk_shortcut_menu = QCheckBox(tr("add.shortcut_menu"))
        self.chk_shortcut_menu.setChecked(True)
        self.chk_shortcut_desktop = QCheckBox(tr("add.shortcut_desktop"))
        self.chk_shortcut_desktop.setChecked(False)
        targets_row.addWidget(self.chk_shortcut_menu)
        targets_row.addWidget(self.chk_shortcut_desktop)
        targets_row.addStretch()
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
            self._nav_buttons[0].setChecked(True)
            self.stack.setCurrentIndex(0)
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
        self.vdf_error_banner.setStyleSheet(
            "color: #ff8a8a; background-color: rgba(180, 40, 40, 0.18); "
            "border: 1px solid #b83b44; border-radius: 6px; padding: 8px;"
        )
        self.vdf_error_banner.setVisible(False)
        lib_layout.addWidget(self.vdf_error_banner)

        self.unavailable_banner = QLabel()
        self.unavailable_banner.setWordWrap(True)
        self.unavailable_banner.setStyleSheet(
            "color: #ffd180; background-color: rgba(180, 110, 20, 0.18); "
            "border: 1px solid #c77c1a; border-radius: 6px; padding: 8px;"
        )
        self.unavailable_banner.setVisible(False)
        lib_layout.addWidget(self.unavailable_banner)

        self.vdf_queue_banner = QFrame()
        self.vdf_queue_banner.setStyleSheet(
            "background-color: rgba(40, 110, 180, 0.18); "
            "border: 1px solid #3b7db8; border-radius: 6px; padding: 6px;"
        )
        vq_lay = QHBoxLayout(self.vdf_queue_banner)
        vq_lay.setContentsMargins(8, 4, 8, 4)
        self.lbl_vdf_queue = QLabel()
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
            ("standalone", "Standalone"),
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
        self.btn_bulk_proton.clicked.connect(self._bulk_migrate_proton_dialog)
        self.btn_manage_protons = QPushButton(tr("library.btn_manage_protons"))
        self.btn_manage_protons.clicked.connect(self._open_proton_manager_dialog)
        self.btn_migrate_legacy = QPushButton(tr("maint.btn_migrate_legacy"))
        self.btn_migrate_legacy.setToolTip(tr("maint.migrate_legacy_tooltip"))
        self.btn_migrate_legacy.clicked.connect(lambda: self._start_legacy_migration())
        self.btn_undo_migration = QPushButton(tr("maint.btn_undo_migration"))
        self.btn_undo_migration.clicked.connect(lambda: self._undo_last_migration())
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
            self.btn_manage_protons,
            self.btn_migrate_legacy,
            self.btn_undo_migration,
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
        self.btn_update_runtimes.setEnabled(False)
        self.status_label.setText(tr("runtime.checking"))

        def _on_preflight(needs_confirm: bool) -> None:
            if self._closing_pending:
                self.btn_update_runtimes.setEnabled(True)
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
                    self.btn_update_runtimes.setEnabled(True)
                    self.status_label.setText(tr("status.ready"))
                    return

            worker = RuntimeUpdateWorker(self.paths)
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(msg: str, bwrap_warn: str) -> None:
                self.btn_update_runtimes.setEnabled(True)
                self.status_label.setText(msg)
                if self._closing_pending:
                    return
                if bwrap_warn:
                    self._show_bwrap_warning_dialog(bwrap_warn)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self.btn_update_runtimes.setEnabled(True)
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
            _on_preflight(False)

        preflight.failed.connect(_preflight_failed)
        preflight.start()

    def _check_and_update_app(self) -> None:
        self.btn_app_update.setEnabled(False)
        self.status_label.setText(tr("app_update.checking"))
        worker = AppUpdateCheckWorker()
        self._track_worker(worker)

        def _on_checked(info: AppUpdateInfo) -> None:
            self.btn_app_update.setEnabled(True)
            if self._closing_pending:
                return
            if not info.has_update:
                msg = tr("app_update.up_to_date", version=info.current_version)
                self.status_label.setText(msg)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                return

            if not info.can_self_update:
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
                else:
                    msg = tr(
                        "app_update.manual_no_asset",
                        latest=info.latest_version,
                        url=url,
                    )
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
                        relaunch_appimage(updated_path)
                        QApplication.quit()

            def _dl_err(err: str) -> None:
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
            self.btn_app_update.setEnabled(True)
            msg = tr("app_update.error", err=err)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker.checked.connect(_on_checked)
        worker.failed.connect(_on_err)
        worker.start()

    def _set_extract_source(self, path_str: str) -> None:
        p = Path(path_str)
        info = classify.classify_source(p)
        if info.needs_install and info.installer is not None:
            self.extract_src.setText(str(info.installer if info.kind != classify.ARCHIVE else p))
            self.add_name.setText(classify.suggest_install_name(info))
            self.extract_details_box.setVisible(True)
            self.status_label.setText(self._detected_kind_text(info))
            return
        if p.is_dir():
            game = scan_game_folder(p)
            if game is not None:
                self._populate_game_form(game)
                self._nav_buttons[0].setChecked(True)
                self.stack.setCurrentIndex(0)
                self.add_name.setFocus()
            return
        if p.is_file():
            self._on_manual_exe_picked(str(p))
            self._nav_buttons[0].setChecked(True)
            self.stack.setCurrentIndex(0)

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
        src = Path(self.extract_src.text().strip())
        if not src.is_file():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("install.invalid_file"))
            return
        # suggest_install_name: a repack's setup.exe is named after its folder, not "setup".
        name = self.add_name.text().strip() or classify.suggest_install_name(classify.classify_source(src))
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
        self.btn_extract.setEnabled(False)
        self.btn_cancel_extract.setEnabled(True)
        self.btn_use_extracted.setEnabled(False)

        self._extract_source_kind = classify.classify_source(src).kind
        self._extract_worker = ExtractWorker(
            src,
            dst,
            name,
        )
        self._track_worker(self._extract_worker)
        self._extract_worker.progress.connect(self._on_extract_progress)
        self._extract_worker.finished_ok.connect(self._on_extract_done)
        self._extract_worker.cancelled.connect(self._on_extract_cancelled)
        self._extract_worker.failed.connect(self._on_extract_failed)
        self._extract_worker.start()

    def _cancel_extract(self) -> None:
        if self._extract_worker:
            self.btn_cancel_extract.setEnabled(False)
            self.status_label.setText(tr("install.cancelling"))
            self._extract_worker.cancel()

    def _on_extract_cancelled(self) -> None:
        self.btn_extract.setEnabled(True)
        self.btn_cancel_extract.setEnabled(False)
        msg = tr("install.cancelled")
        self.extract_log.append(msg)
        self.status_label.setText(msg)

    def _on_extract_progress(self, pct: int, msg: str) -> None:
        self.extract_progress.setValue(pct)
        self.extract_log.append(msg)
        self.status_label.setText(msg)

    def _on_extract_done(self, out_dir: str) -> None:
        self.btn_extract.setEnabled(True)
        self.btn_cancel_extract.setEnabled(False)
        self.btn_use_extracted.setEnabled(True)
        self._last_extracted_dir = Path(out_dir)
        self.status_label.setText(tr("install.done_prefix", path=out_dir))
        self._refresh_games()
        if getattr(self, "_extract_source_kind", "") == classify.ARCHIVE:
            inner = classify.classify_source(Path(out_dir))
            if inner.kind in (classify.REPACK, classify.GOG_SETUP, classify.INSTALLER) and inner.installer:
                # The archive held an installer: offer to run it instead of picking an exe.
                self.extract_src.setText(str(inner.installer))
                self.extract_log.append(tr("install.archive_has_installer", name=inner.installer.name))
                self.status_label.setText(tr("install.archive_has_installer", name=inner.installer.name))
                return
        if not self._closing_pending and self.isVisible():
            self._send_extracted_to_add_tab()

    def _on_extract_failed(self, err: str) -> None:
        self.btn_extract.setEnabled(True)
        self.btn_cancel_extract.setEnabled(False)
        self.extract_log.append(tr("install.error_prefix", err=err))
        if not self._closing_pending:
            QMessageBox.critical(_dialog_parent(self), __app_name__, err)

    def _send_extracted_to_add_tab(self) -> None:
        target = getattr(self, "_last_extracted_dir", None)
        if isinstance(target, Path) and target.is_dir():
            game = scan_game_folder(target)
            if game:
                self._populate_game_form(game, from_extract=True)
        self._switch_library_subview(1)

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

    def _refresh_games(self) -> None:
        self.btn_scan.setEnabled(False)
        self._update_scan_folders_label()
        worker = ScanWorker(roots=self._enabled_scan_roots(), scan_fn=scan_all_games)
        self._scan_worker = worker
        self._track_worker(worker)
        worker.finished_scan.connect(
            lambda games, w=worker: self._on_games_scanned(w, games)
        )
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
        self._auto_art_timer.stop()
        self._search_req_id += 1
        self._reset_add_launch_options()
        exe = Path(path)
        game = scan_game_folder(exe.parent)
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
        if is_elf:
            self.chk_proton.setChecked(False)
            self.chk_proton.setEnabled(False)
        else:
            self.chk_proton.setEnabled(True)
        self._update_remove_existing_state()

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

    def _add_selected_to_steam(self) -> None:
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

        def _proceed_add() -> None:
            if self._closing_pending or not self.paths:
                return
            steam_appid = self._resolve_selected_steam_appid()
            launch_opts, winedll = self._resolve_add_launch_options()
            self._save_last_launch_prefs(launch_opts, winedll)

            self._begin_busy()
            self._add_worker = AddToSteamWorker(
                paths=self.paths,
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

        if is_gamescope_session():
            def _on_steam_checked(running: bool) -> None:
                if running:
                    ans = QMessageBox.question(
                        _dialog_parent(self),
                        __app_name__,
                        tr("add.gamescope_confirm"),
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                _proceed_add()

            self._check_steam_running_async(_on_steam_checked)
            return
        _proceed_add()

    def _add_selected_standalone(self) -> None:
        validated = self._validate_add_inputs()
        if validated is None:
            return
        create_menu = self.chk_shortcut_menu.isChecked()
        create_desktop = self.chk_shortcut_desktop.isChecked()
        if not create_menu and not create_desktop:
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
            box.addButton(
                tr("add.btn_move_folder"), QMessageBox.ButtonRole.AcceptRole
            )
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
        refresh_saves_page(self)
        labels = (
            runner_labels
            if runner_labels is not None
            else compute_library_runner_labels(
                paths_snapshot, entries, standalone_games, external_games
            )
        )
        default_runner = tr("proton.default_label")
        rows: list[tuple[str, str, str, str, tuple[str, object], str]] = [
            (
                entry.app_name,
                "Steam",
                labels.get(("steam", str(entry.appid_u32)), default_runner),
                entry.exe,
                ("steam", entry),
                f"steam:{entry.appid_u32}",
            )
            for entry in entries
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
        if not self._initial_tab_decided:
            self._initial_tab_decided = True
            if (
                (entries or standalone_games or external_games)
                and self.stack.currentIndex() == 0
                and self._extract_worker is None
            ):
                self._nav_buttons[1].setChecked(True)
                self.stack.setCurrentIndex(1)
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
        if self._closing_pending:
            return
        if not uses_library_layout():
            return
        if _is_wine_game_running():
            if not from_startup:
                QMessageBox.warning(_dialog_parent(self), __app_name__, tr("migrate.game_running"))
            return
        try:
            plan = plan_migration(self.paths)
        except Exception as exc:
            QMessageBox.critical(_dialog_parent(self), __app_name__, tr("migrate.failed", err=str(exc)))
            return
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
        if btn_never is not None and clicked is btn_never:
            self.settings.setValue("migration/dont_ask", True)
            return
        if clicked is not btn_go:
            return

        paths = self.paths
        self._begin_busy()
        self.status_label.setText(tr("migrate.running"))
        worker = SteamOpWorker(lambda: migrate_legacy_games(todo, paths))
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
                lines.append(tr("migrate.undo_hint"))
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

    def _undo_last_migration(self) -> None:
        """Put the games of the latest migration back in ~/Games."""
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("migrate.game_running"))
            return
        journal = latest_undoable_journal()
        if journal is None:
            QMessageBox.information(_dialog_parent(self), __app_name__, tr("migrate.nothing_to_undo"))
            return
        ans = QMessageBox.question(
            _dialog_parent(self), __app_name__, tr("migrate.undo_confirm", name=journal.stem)
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._begin_busy()
        worker = SteamOpWorker(lambda: undo_migration(journal))
        self._track_worker(worker)

        def _ok(res: object) -> None:
            self._end_busy()
            self._refresh_shortcuts()
            if self._closing_pending or not isinstance(res, tuple):
                return
            restored, problems = res
            text = tr("migrate.undo_done", count=len(restored))
            if problems:
                text += "\n\n" + tr("migrate.undo_problems", items="\n".join(f"• {p}" for p in problems))
                QMessageBox.warning(_dialog_parent(self), __app_name__, text)
            else:
                QMessageBox.information(_dialog_parent(self), __app_name__, text)

        def _err(err: str) -> None:
            self._end_busy()
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("migrate.failed", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _refresh_shortcuts(self) -> None:
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
        worker.start()

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


    def _update_library_action_states(self) -> None:
        sel = self._selected_library_item()
        is_steam = sel is not None and sel[0] == "steam"
        is_standalone = sel is not None and sel[0] == "standalone"
        is_external = sel is not None and sel[0] == "external"
        can_write_vdf = bool(self.paths) and not self._vdf_corrupt

        self.btn_configure_game.setEnabled(sel is not None)
        self._update_play_button(sel)
        self.btn_move_game.setEnabled(is_standalone and not self._steam_busy)

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
        self.act_ext_import_saves.setEnabled(is_external and has_ext_prefix)
        self.btn_remove_game.setEnabled((is_steam and can_write_vdf) or is_standalone)
        self.btn_remove_game.setVisible(not is_external)

    def should_queue_vdf_edits(self) -> bool:
        """Gaming Mode cannot close Steam: Steam edits wait in the saved queue instead of applying now."""
        return self.paths is not None and is_gamescope_session()

    def enqueue_vdf_edit(self, edit: QueuedVdfEdit) -> None:
        """Enqueue a VDF edit; it is applied once Steam is closed."""
        self._vdf_queue.enqueue(edit)
        self._vdf_auto_paused = False
        self._update_vdf_queue_banner()
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
        if not self._vdf_watch_timer.isActive():
            self._vdf_watch_timer.start()

    def _vdf_watch_tick(self) -> None:
        """Poll (off the UI thread) whether Steam is closed; queued edits apply as soon as it is."""
        if (
            self._vdf_checking
            or self._vdf_auto_paused
            or self._closing_pending
            or self._busy_count > 0
            or not self.paths
            or self._vdf_corrupt
            or self._vdf_queue.pending_count() <= 0
        ):
            return
        self._vdf_checking = True
        worker = SteamRunningWorker()
        self._track_worker(worker)

        def _done(running: bool) -> None:
            self._vdf_checking = False
            self._vdf_steam_open = running
            self._update_vdf_queue_banner()
            if not running and not self._closing_pending:
                self._auto_apply_vdf_queue()

        def _failed(err: str) -> None:
            self._vdf_checking = False
            logging.warning("Steam running check failed; keeping VDF edits queued: %s", err)

        worker.ready.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def _auto_apply_vdf_queue(self) -> None:
        """Apply the queue now that Steam is closed (never while it runs)."""
        paths = self.paths
        queue = self._vdf_queue
        if paths is None or queue.pending_count() <= 0:
            return

        def _apply() -> object:
            if is_steam_running():  # Steam was started again since the poll: keep waiting
                return None
            return queue.apply_all(paths)

        self._begin_busy()
        worker = SteamOpWorker(_apply)
        self._track_worker(worker)

        def _ok(res: object) -> None:
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
            if applied:
                self._offer_steam_restart()

        def _err(err: str) -> None:
            self._end_busy()
            self._vdf_auto_paused = True
            self._update_vdf_queue_banner()
            self.status_label.setText(tr("worker.failed", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _maybe_offer_steam_restart(self, worker: QThread) -> None:
        if getattr(worker, "restart_failed", False):
            self._warn_steam_restart_failed()
        elif getattr(worker, "closed_steam", False) and not getattr(worker, "restart_if_was_running", False):
            self._offer_steam_restart()

    def _warn_steam_restart_failed(self) -> None:
        if self._closing_pending or self._force_closing:
            return
        self.status_label.setText(tr("steam.restart_failed"))
        box = QMessageBox(
            QMessageBox.Icon.Warning, __app_name__, tr("steam.restart_failed"), QMessageBox.StandardButton.Ok, self
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def _offer_steam_restart(self) -> None:
        """Ask (without blocking) whether to start Steam again after it was closed for an edit."""
        if self._closing_pending or self._force_closing or self.paths is None:
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
            if result != QMessageBox.StandardButton.Yes.value or self._closing_pending:
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
            self._skip_vdf_close_prompt = True  # the queue is saved on disk; the new version picks it up
            relaunch_appimage(updated_path)
            QApplication.quit()

        if clicked == btn_apply:
            self._apply_vdf_queue(on_done=lambda ok: _restart() if ok else None)
        elif clicked == btn_update:
            _restart()

    def _apply_vdf_queue(self, on_done: Callable[[bool], None] | None = None) -> None:
        """Send the queued edits to Steam now (closes and reopens it).

        With ``on_done`` the user already agreed to close Steam: no extra question, and
        ``on_done(True)`` runs only when everything was applied (errors are still shown).
        """
        if not self.paths or self._vdf_queue.pending_count() <= 0:
            if on_done is not None:
                on_done(True)
            return
        paths = self.paths

        def _proceed() -> None:
            if self._closing_pending:
                return
            self._begin_busy()
            worker = SteamOpWorker(
                lambda: self._vdf_queue.apply_all(paths),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(res: object) -> None:
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
                else:
                    msg = tr("vdf_queue.applied_ok", applied=applied)
                    self.status_label.setText(msg)
                    if on_done is not None:
                        on_done(True)
                    elif not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._end_busy()
                self._update_vdf_queue_banner()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

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
        if not self.paths:
            return
        ext = sel[1]
        paths = self.paths
        launch_opts = load_external_launch_presets(ext)

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
                targets.append((f"[Standalone] {sg.name}", sg.prefix_dir))

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
        worker = ExtImportSavesWorker(
            ext, target_pfx, target_label, snapshot_fn=self._make_pre_snapshot_fn()
        )
        self._track_worker(worker)

        def _ok(copied: int) -> None:
            msg = tr("library.ext_saves_imported", count=copied, target=target_label)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _is_auto_snapshot_enabled(self) -> bool:
        """Read safety/auto_snapshot on the main thread."""
        return _setting_bool(self.settings, AUTO_SNAPSHOT_KEY, True)

    def _make_pre_snapshot_fn(self, force: bool = False) -> Callable[[Path, str, str, str], Path | None]:
        """Capture the snapshot settings on the main thread for worker threads.

        force=True snapshots even when automatic snapshots are off (uninstall, adopt, move).
        """
        enabled = force or self._is_auto_snapshot_enabled()
        keep = snapshot_keep(self.settings)

        def _snap(
            prefix_dir: Path,
            game_name: str,
            reason: str,
            key_prefix: str = "snap",
        ) -> Path | None:
            if not enabled or not prefix_dir.is_dir():
                return None
            return auto_snapshot(prefix_dir, game_name, reason, key_prefix, keep=keep)

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
        self._play.prepare(key, title_name)
        paths_snap = self.paths

        def _on_prep_ready(
            status: str,
            msg_or_fallback: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
        ) -> None:
            if self._closing_pending:
                return
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
                watch=watch_paths(obj.game_dir, obj.prefix_dir),
            )
            self.status_label.setText(tr("play.launched", name=title_name))

        worker = LaunchPrepWorker(kind, obj, paths_snap)
        self._track_worker(worker)
        worker.ready.connect(_on_prep_ready)
        worker.start()

    def _move_selected_game(self) -> None:
        sel = self._selected_library_item()
        if sel is not None and sel[0] == "standalone" and isinstance(sel[1], StandaloneGame):
            move_game_to_disk(self, sel[1])

    def _on_background_failed(self, err: str) -> None:
        """A background worker died with an error: say so instead of failing silently."""
        logging.warning("Background worker failed: %s", err)
        if not self._closing_pending:
            self.status_label.setText(tr("worker.failed", err=err))

    def _set_vdf_write_enabled(self, enabled: bool) -> None:
        for w in (
            self.btn_add_steam,
            self.btn_bulk_proton,
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
            self.btn_save_backup,
            self.btn_save_restore,
            self.btn_save_sync,
            self.btn_save_dedup,
            self.btn_save_refresh,
            self.btn_adopt_games,
            self.btn_manage_protons,
            self.btn_bulk_optiscaler,
            self.btn_maint_scan,
            self.btn_maint_clean_safe,
            self.btn_maint_delete_selected,
            self.btn_maint_toggle_filter,
        ):
            w.setEnabled(enabled)
        for b in save_row_buttons(self):
            b.setEnabled(enabled)
        self.btn_maint_revalidate.setEnabled(enabled and (self.paths is not None))
        self._set_vdf_write_enabled(enabled and bool(self.paths) and not self._vdf_corrupt)
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
    ) -> None:
        """Check if Steam is running asynchronously and prompt before proceeding."""

        def _on_checked(running: bool) -> None:
            if alive_fn is not None and not alive_fn():
                return
            if running and not self.should_queue_vdf_edits():  # Gaming Mode queues: Steam stays open
                ans = QMessageBox.question(
                    parent or self,
                    __app_name__,
                    tr("proton.steam_running_confirm"),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    if on_cancelled is not None:
                        on_cancelled()
                    return
            on_confirmed()

        self._check_steam_running_async(_on_checked)

    def _change_selected_proton(self) -> None:
        sel = self._selected_library_item()
        if sel is None:
            return
        kind, obj = sel
        if _is_native_linux_game(kind, obj):
            return

        if kind == "standalone" and isinstance(obj, StandaloneGame):
            sa = obj
            paths_snap = self.paths
            auto_snap_enabled = self._is_auto_snapshot_enabled()

            def _on_standalone_prep(
                is_win32: bool, tools: list[tuple[str, str]], _current_id: str = ""
            ) -> None:
                if self._closing_pending:
                    return
                if is_win32:
                    QMessageBox.warning(
                        _dialog_parent(self),
                        __app_name__,
                        tr("prefix.win32_unsupported", path=str(sa.prefix_dir)),
                    )
                    return
                if not tools:
                    QMessageBox.warning(_dialog_parent(self), __app_name__, tr("proton.none_found"))
                    return
                current_id = sa.proton_name or ""
                current_disp = current_id or tr("proton.default_label")
                labels = [disp for _, disp in tools]
                cur_idx = next((i for i, (tid, _) in enumerate(tools) if tid == current_id), 0)
                choice, ok = QInputDialog.getItem(
                    _dialog_parent(self),
                    tr("proton.title"),
                    tr("proton.label", name=sa.name, current=current_disp),
                    labels,
                    cur_idx,
                    False,
                )
                if not ok or not choice:
                    return
                tool_id = tools[labels.index(choice)][0]

                def _do_set_standalone_proton() -> None:
                    self._auto_snapshot_if_enabled(
                        sa.prefix_dir,
                        sa.name,
                        "proton-change",
                        "proton",
                        enabled=auto_snap_enabled,
                    )
                    update_standalone_launch_options(
                        sa.game_dir,
                        winedlloverrides=sa.winedlloverrides,
                        launch_options=sa.launch_options,
                        proton_name=tool_id,
                    )

                worker = SteamOpWorker(_do_set_standalone_proton, need_steam_closed=False)
                self._track_worker(worker)
                worker.status.connect(self.status_label.setText)

                def _ok_standalone(_res: object) -> None:
                    self._refresh_shortcuts()
                    msg = tr("proton.updated_standalone", name=sa.name, tool=tool_id)
                    self.status_label.setText(msg)
                    if not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

                def _err_standalone(err: str) -> None:
                    if not self._closing_pending:
                        QMessageBox.critical(_dialog_parent(self), __app_name__, tr("proton.error", err=err))

                worker.finished_ok.connect(_ok_standalone)
                worker.failed.connect(_err_standalone)
                worker.start()

            prep_worker = ProtonPrepWorker("standalone", sa.prefix_dir, paths_snap)
            self._track_worker(prep_worker)
            prep_worker.ready.connect(_on_standalone_prep)
            prep_worker.failed.connect(self._on_background_failed)
            prep_worker.start()
            return

        if kind != "steam" or not isinstance(obj, ShortcutEntry) or not self.paths:
            return
        entry = obj
        paths = self.paths
        pfx_dir = paths.compatdata_dir / str(entry.appid_u32) / "pfx"
        auto_snap_enabled = self._is_auto_snapshot_enabled()

        def _on_steam_prep(is_win32: bool, protons: list[tuple[str, str]], current_id: str) -> None:
            if self._closing_pending:
                return
            if is_win32:
                QMessageBox.warning(
                    _dialog_parent(self),
                    __app_name__,
                    tr("prefix.win32_unsupported", path=str(pfx_dir)),
                )
                return
            if not protons:
                QMessageBox.warning(_dialog_parent(self), __app_name__, tr("proton.none_found"))
                return
            current_disp = current_id or tr("proton.default_label")
            labels = [disp for _, disp in protons]
            cur_idx = next((i for i, (tid, _) in enumerate(protons) if tid == current_id), 0)
            choice, ok = QInputDialog.getItem(
                _dialog_parent(self),
                tr("proton.title"),
                tr("proton.label", name=entry.app_name, current=current_disp),
                labels,
                cur_idx,
                False,
            )
            if not ok or not choice:
                return
            tool_id = protons[labels.index(choice)][0]

            def _proceed_set_proton() -> None:
                if self._closing_pending:
                    return
                self._begin_busy()
                queued = self.should_queue_vdf_edits()  # Gaming Mode: save it for when Steam closes

                def _do_set_proton() -> None:
                    self._auto_snapshot_if_enabled(
                        pfx_dir,
                        entry.app_name,
                        "proton-change",
                        "proton",
                        enabled=auto_snap_enabled,
                    )
                    if not queued:
                        set_shortcut_proton(paths.config_vdf, entry.appid_u32, tool_id)

                worker = SteamOpWorker(_do_set_proton, need_steam_closed=not queued)
                self._track_worker(worker)
                worker.status.connect(self.status_label.setText)

                def _ok(_res: object) -> None:
                    self._end_busy()
                    msg = tr("proton.updated", name=entry.app_name, tool=tool_id)
                    if queued:
                        self.enqueue_vdf_edit(_proton_edit(entry.appid_u32, entry.app_name, tool_id))
                        msg = tr("vdf_queue.enqueued", name=entry.app_name)
                    self.status_label.setText(msg)
                    if not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

                def _err(err: str) -> None:
                    self._end_busy()
                    if not self._closing_pending:
                        QMessageBox.critical(_dialog_parent(self), __app_name__, tr("proton.error", err=err))

                worker.finished_ok.connect(_ok)
                worker.failed.connect(_err)
                worker.start()

            self._confirm_close_steam_async(_proceed_set_proton)

        steam_prep = ProtonPrepWorker("steam", pfx_dir, paths, entry.appid_u32)
        self._track_worker(steam_prep)
        steam_prep.ready.connect(_on_steam_prep)
        steam_prep.failed.connect(self._on_background_failed)
        steam_prep.start()

    def _bulk_migrate_proton_dialog(self) -> None:
        bulk_migrate_proton_dialog(self)

    def _open_proton_manager_dialog(self) -> None:
        open_proton_manager_dialog(self)

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
        self, kind: str | None = None, obj: object | None = None
    ) -> None:
        open_optiscaler_dialog(self, kind=kind, obj=obj)

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
            )
            if results:
                self.status_label.setText(tr("adopt.status_done", count=len(results)))
                self._refresh_libraries()
                self._refresh_shortcuts()

        self._check_steam_running_async(_open)

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
            worker = SteamOpWorker(
                lambda: import_standalone_to_steam(paths, game, remove_standalone_files=False),
                need_steam_closed=True,
            )
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

        if is_gamescope_session():
            def _on_steam_checked(running: bool) -> None:
                if running:
                    ans = QMessageBox.question(_dialog_parent(self), __app_name__, tr("add.gamescope_confirm"))
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                _proceed_import()

            self._check_steam_running_async(_on_steam_checked)
            return
        _proceed_import()

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
        self, entry: ShortcutEntry, refresh_games: bool = False, done_msg: str = ""
    ) -> None:
        """Remove a Steam Non-Steam shortcut in a background thread."""
        if not self.paths:
            return
        self._begin_busy()
        self.status_label.setText(tr("library.removing", name=entry.app_name))

        worker = RemoveShortcutWorker(self.paths, entry)
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
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.done.connect(_on_removed)
        worker.failed.connect(_on_remove_err)
        worker.start()

    def _uninstall_standalone_async(
        self,
        sa: StandaloneGame,
        *,
        steam_entry: ShortcutEntry | None = None,
        refresh_games: bool = False,
    ) -> None:
        """Plan the uninstall in the background, let the user pick the parts, then remove them."""
        self._begin_busy()
        self.status_label.setText(tr("uninstall.planning", name=sa.name))
        planner = SteamOpWorker(lambda: plan_uninstall(sa))
        self._track_worker(planner)

        def _on_err(err: str) -> None:
            self._end_busy()
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

            snap = self._make_pre_snapshot_fn(force=True)
            worker = SteamOpWorker(lambda: execute_uninstall(plan, selected, snap))
            self._track_worker(worker)
            worker.finished_ok.connect(_on_done)
            worker.failed.connect(_on_err)
            worker.start()

        def _on_done(result: object) -> None:
            self._end_busy()
            if self._closing_pending or not isinstance(result, UninstallResult):
                return
            msg = tr("uninstall.done", name=sa.name, size=format_size_bytes(result.freed_bytes))
            if result.errors:
                QMessageBox.warning(
                    _dialog_parent(self), __app_name__, tr("uninstall.errors", errors="\n".join(result.errors))
                )
            if steam_entry is not None and self.paths is not None:
                self._remove_steam_shortcut_async(steam_entry, refresh_games=True, done_msg=msg)
                return
            if refresh_games or GAME_FILES in result.removed:
                self._refresh_games()
            self._refresh_shortcuts()
            self.status_label.setText(msg)

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
        if kind == "standalone":
            assert isinstance(obj, StandaloneGame)
            self._uninstall_standalone_async(obj)
            return

        entry = obj
        assert isinstance(entry, ShortcutEntry)
        if not self.paths:
            return
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("library.remove_confirm", name=entry.app_name),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._remove_steam_shortcut_async(entry)


def _proton_edit(appid_u32: int, name: str, tool_id: str) -> QueuedVdfEdit:
    """Queued "use this Proton" for a Steam shortcut (Gaming Mode)."""
    return QueuedVdfEdit(f"set_proton:{appid_u32}", "set_proton", appid_u32, name, {"tool_name": tool_id})


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


def _qt_message_handler(mode, context, message: str) -> None:
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
