"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

from PySide6.QtCore import (
    QPoint,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
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
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .app_updater import (
    GITHUB_RELEASES_URL,
    AppUpdateInfo,
    get_running_appimage,
    relaunch_appimage,
)
from .artwork import (
    SteamMatch,
    download_steam_artwork,
    restore_steam_artwork_backup,
    search_steam_games,
)
from .external_actions import (
    create_standalone_from_external,
    create_steam_shortcut_from_external,
    import_external_saves_to_target,
    load_external_launch_presets,
)
from .extractor import (
    sanitize_folder_name,
    suggest_target_name,
)
from .i18n import available_languages, set_language, tr
from .launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from .runtime_manager import (
    build_umu_env,
    detect_gpu_profile,
    ensure_ge_proton11,
    ensure_umu_run,
    fix_bwrap_userns,
    get_compat_tools_dir,
    get_steamrt4_dir,
    get_umu_run_path,
    is_win32_prefix,
    list_ge_proton11_dirs,
    list_installed_compat_tools,
    validate_steamrt4_integrity,
)
from .save_sync import (
    snapshot_prefix_saves,
)
from .scanner import (
    DetectedGame,
    clean_game_name,
    detect_exe_kind,
    scan_all_games,
    scan_game_folder,
)
from .standalone import (
    StandaloneGame,
    export_steam_to_standalone,
    get_games_dir,
    import_standalone_to_steam,
    list_standalone_games,
    remove_standalone_game,
    resolve_standalone_proton_fallback,
    update_standalone_launch_options,
)
from .steam_paths import (
    SteamPaths,
    find_proton_binary,
    find_steam_root,
    is_gamescope_session,
    is_steam_running,
    list_installed_protons,
    resolve_steam_paths,
)
from .steam_vdf import (
    DEFAULT_SAVES_DIR,
    CleanupCandidate,
    MigrationPlanItem,
    ShortcutEntry,
    _dir_size_bytes,
    apply_launch_presets,
    backup_all_saves,
    backup_prefix_candidates_to_zip,
    delete_cleanup_candidates_checked,
    find_cleanup_candidates,
    get_shortcut_proton,
    infer_game_root_from_exe,
    load_shortcuts,
    plan_bulk_migration,
    remove_shortcut,
    restore_saves_backup,
    set_shortcut_proton,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
    sync_prefix_symlinks,
)
from .theme import ThemeMode, apply_theme
from .ui_cards import (  # noqa: F401
    DropZoneFrame,
    FlowLayout,
    _cleanup_candidate_auto_check,
    _dialog_guard,
    _dialog_parent,
    _format_cleanup_candidate_label,
    _format_worker_error,
    _is_native_linux_game,
    _is_wine_game_running,
    _make_card,
    _pick_dir_async,
    _pick_file_async,
    _resolve_asset_path,
    _resolve_game_exe_path,
    _setting_bool,
    _SortableTableItem,
    _start_detached_process,
    format_size_bytes,
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
from .ui_workers import (  # noqa: F401
    AddToSteamWorker,
    AppUpdateCheckWorker,
    AppUpdateDownloadWorker,
    BulkMigrateWorker,
    ExtractWorker,
    FallbackProton10Worker,
    PkexecWorker,
    PreviewWorker,
    RunExeInPrefixWorker,
    RuntimeUpdateWorker,
    ScanWorker,
    StandaloneInstallWorker,
    SteamOpWorker,
    SteamSearchWorker,
    WinetricksWorker,
)
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
    lsfg: bool
    lsfg_multiplier: int
    lsfg_flow_scale: float | None
    lsfg_perf_mode: bool
    lsfg_profile: str
    winedll: str


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


_MAINT_FILTER_CATEGORIES: dict[str, frozenset[str] | None] = {
    "all": None,
    "prefixes": frozenset(
        {"Prefixo Externo", "external_prefix", "Compatdata Órfão", "orphan_compatdata"}
    ),
    "backups": frozenset({"Backup", "backup"}),
    "caches": frozenset({"Steam Logs", "Trash", "App Cache", "Orphan Desktop"}),
    "shaders": frozenset({"Orphan Shadercache", "GPU Shader Cache"}),
    "runtimes": frozenset({"Old Proton/Runtime"}),
}


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings(__app_name__, __app_name__)
        set_language(str(self.settings.value("language", "auto")))
        self.paths: SteamPaths | None = None
        self._steam_root_found = False
        self._gpu_profile_cache: object | None = None
        self._detected_games: list[DetectedGame] = []
        self._standalone_games: list[StandaloneGame] = []
        self._external_games: list[ExternalLauncherGame] = []
        self._steam_shortcuts: list[ShortcutEntry] = []
        self._custom_icon_path: Path | None = None
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._force_closing = False
        self._close_force_timer: QTimer | None = None
        self._vdf_corrupt = False
        self._vdf_queue = VdfEditQueue()
        self._search_req_id = 0
        self._preview_req_id = 0
        self._maint_health_req_id = 0
        self._shortcuts_req_id = 0
        self._steam_detect_req_id = 0
        self._extract_worker: ExtractWorker | None = None
        self._search_worker: SteamSearchWorker | None = None
        self._preview_worker: PreviewWorker | None = None
        self._add_worker: AddToSteamWorker | None = None
        self._bulk_worker: BulkMigrateWorker | None = None
        self._scan_worker: ScanWorker | None = None

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1080, 700)
        self.setMinimumSize(840, 560)
        self._build_ui()
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
                "_search_worker",
                "_preview_worker",
                "_add_worker",
                "_bulk_worker",
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
            super().closeEvent(event)
            return
        if self._vdf_queue.pending_count() > 0:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(__app_name__)
            box.setText(tr("vdf_queue.close_prompt", count=self._vdf_queue.pending_count()))
            btn_apply = box.addButton(tr("vdf_queue.btn_apply_exit"), QMessageBox.ButtonRole.AcceptRole)
            btn_discard = box.addButton(tr("vdf_queue.btn_discard_exit"), QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(btn_apply)
            box.exec()
            clicked = box.clickedButton()
            if clicked == btn_cancel or clicked not in (btn_apply, btn_discard):
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
                    if was_running:
                        shutdown_steam()
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
                    if was_running:
                        start_steam()
                self._update_vdf_queue_banner()
                if apply_failed or self._vdf_queue.pending_count() > 0:
                    event.ignore()
                    return
            elif clicked == btn_discard:
                self._vdf_queue.clear()
                self._update_vdf_queue_banner()
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

        self.btn_update_runtimes = QPushButton(tr("runtime.btn_update"))
        self.btn_update_runtimes.setToolTip(tr("runtime.tooltip"))
        self.btn_update_runtimes.clicked.connect(self._update_runtimes)
        layout.addWidget(self.btn_update_runtimes)

        self.btn_app_update = QPushButton(tr("app_update.btn"))
        self.btn_app_update.clicked.connect(self._check_and_update_app)
        layout.addWidget(self.btn_app_update)

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
        """Refresh Maintenance health metrics when switching to the Maintenance tab."""
        if idx == 3:
            self._refresh_maintenance_health()

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

        class _SteamDetectWorker(QThread):
            ready = Signal(int, int, object, bool, list, list, list, str)

            def __init__(self, r_id: int, sc_req_id: int) -> None:
                super().__init__()
                self._req_id = r_id
                self._sc_req_id = sc_req_id

            def run(self) -> None:
                paths = resolve_steam_paths()
                steam_root_found = True if paths is not None else (find_steam_root() is not None)
                entries: list[ShortcutEntry] = []
                vdf_err = ""
                if paths is not None:
                    try:
                        entries = load_shortcuts(paths.shortcuts_vdf)
                    except Exception as exc:
                        vdf_err = str(exc)
                standalone_games = list_standalone_games()
                external_games = scan_external_launcher_games()
                self.ready.emit(
                    self._req_id,
                    self._sc_req_id,
                    paths,
                    steam_root_found,
                    entries,
                    standalone_games,
                    external_games,
                    vdf_err,
                )

        def _on_detected(
            r_id: int,
            sc_req_id: int,
            paths: SteamPaths | None,
            steam_root_found: bool,
            entries: list[ShortcutEntry],
            standalone_games: list[StandaloneGame],
            external_games: list[ExternalLauncherGame],
            vdf_err: str,
        ) -> None:
            if self._closing_pending or r_id != self._steam_detect_req_id:
                return
            self.paths = paths
            self._steam_root_found = steam_root_found
            status_text = self._format_steam_status_text()
            self.steam_status_lbl.setText(status_text)
            self._update_steam_status_style()
            self._update_steam_mode_ui()
            self.status_label.setText(status_text)
            if sc_req_id == self._shortcuts_req_id:
                self._apply_shortcuts_data(
                    paths, entries, standalone_games, external_games, vdf_err
                )

        worker = _SteamDetectWorker(req_id, shortcuts_req_id)
        self._track_worker(worker)
        worker.ready.connect(_on_detected)
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
        self.act_bulk_migrate.setVisible(has_steam)
        if hasattr(self, "maint_batch_card"):
            self.maint_batch_card.setVisible(has_steam)
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
        sub = QLabel(tr("app.subtitle"))
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

        form = QFormLayout()
        form.setSpacing(10)
        self.extract_name = QLineEdit()
        self.extract_name.setPlaceholderText(tr("install.dest_placeholder"))
        form.addRow(tr("install.folder_label"), self.extract_name)

        dst_row = QHBoxLayout()
        self.extract_dst_root = QLineEdit(str(Path.home() / "Games"))
        btn_dst = QPushButton(tr("install.choose_folder"))
        btn_dst.clicked.connect(self._pick_dest_root)
        dst_row.addWidget(self.extract_dst_root, 1)
        dst_row.addWidget(btn_dst)
        form.addRow(tr("install.dest_label"), dst_row)
        drop_layout.addLayout(form)
        layout.addWidget(drop_card)

        prog_card, prog_layout = _make_card()
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
        prog_layout.addLayout(btn_row)

        self.extract_progress = QProgressBar()
        prog_layout.addWidget(self.extract_progress)

        self.extract_log = QTextEdit()
        self.extract_log.setReadOnly(True)
        self.extract_log.setPlaceholderText(tr("install.log_placeholder"))
        prog_layout.addWidget(self.extract_log)
        layout.addWidget(prog_card, 1)
        return page

    def _build_add_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel(tr("add.page_title"))
        title.setObjectName("PageTitle")
        sub = QLabel(tr("add.page_subtitle"))
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        self.add_no_steam_banner = QLabel(tr("add.no_steam_banner"))
        self.add_no_steam_banner.setWordWrap(True)
        self.add_no_steam_banner.setStyleSheet(
            "background-color: rgba(61, 174, 233, 0.14); "
            "border: 1px solid rgba(61, 174, 233, 0.55); border-radius: 6px; padding: 8px;"
        )
        self.add_no_steam_banner.setVisible(False)
        layout.addWidget(self.add_no_steam_banner)

        add_vsplit = QSplitter(Qt.Orientation.Vertical)
        add_vsplit.setChildrenCollapsible(False)

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
        self.games_list.setMinimumHeight(150)
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
        add_vsplit.addWidget(list_card)

        cfg_card, cfg_layout = _make_card()
        prev_lbl = QLabel(tr("install.preview_title"))
        prev_lbl.setObjectName("SectionTitle")
        cfg_layout.addWidget(prev_lbl)
        cfg_split = QSplitter(Qt.Orientation.Horizontal)
        cfg_split.setChildrenCollapsible(False)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(10)
        self.add_name = QLineEdit()
        self.add_name.setPlaceholderText(tr("install.game_name_placeholder"))
        self.add_name.textChanged.connect(self._update_remove_existing_state)
        form.addRow(tr("install.folder_label"), self.add_name)

        self.add_exe_combo = QComboBox()
        self.add_exe_combo.setEditable(True)
        self.add_exe_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_exe_combo.setMinimumContentsLength(24)
        self.add_exe_combo.currentTextChanged.connect(self._on_exe_changed)
        form.addRow(tr("install.exe_label"), self.add_exe_combo)

        art_row = QHBoxLayout()
        self.steam_search_input = QLineEdit()
        self.steam_search_input.setPlaceholderText(tr("install.game_name_placeholder"))
        self.steam_search_input.returnPressed.connect(self._search_steam_art)
        btn_search = QPushButton(tr("install.search_steam"))
        btn_search.clicked.connect(self._search_steam_art)
        art_row.addWidget(self.steam_search_input, 1)
        art_row.addWidget(btn_search)
        form.addRow(tr("add.steam_store_label"), art_row)

        self.steam_match_combo = QComboBox()
        self.steam_match_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.steam_match_combo.setMinimumContentsLength(24)
        self.steam_match_combo.currentIndexChanged.connect(self._on_steam_match_changed)
        form.addRow(tr("add.match_label"), self.steam_match_combo)

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

        lbl_standalone_opts_group = QLabel(tr("add.group_standalone_opts"))
        lbl_standalone_opts_group.setObjectName("MutedText")
        lbl_standalone_opts_group.setStyleSheet("font-weight: 700; margin-top: 6px;")
        form.addRow(lbl_standalone_opts_group)

        targets_row = QHBoxLayout()
        self.chk_shortcut_menu = QCheckBox(tr("add.shortcut_menu"))
        self.chk_shortcut_menu.setChecked(True)
        self.chk_shortcut_desktop = QCheckBox(tr("add.shortcut_desktop"))
        self.chk_shortcut_desktop.setChecked(False)
        targets_row.addWidget(self.chk_shortcut_menu)
        targets_row.addWidget(self.chk_shortcut_desktop)
        targets_row.addStretch()
        form.addRow(tr("add.standalone_targets"), targets_row)

        self.chk_keep_in_place = QCheckBox(tr("add.keep_in_place"))
        self.chk_keep_in_place.setChecked(False)
        form.addRow("", self.chk_keep_in_place)

        self._add_launch_options = ""
        self._add_winedlloverrides = ""
        lbl_launch_opts_group = QLabel(tr("add.group_launch_opts"))
        lbl_launch_opts_group.setObjectName("MutedText")
        lbl_launch_opts_group.setStyleSheet("font-weight: 700; margin-top: 6px;")
        form.addRow(lbl_launch_opts_group)

        launch_opts_box = QVBoxLayout()
        launch_opts_box.setSpacing(4)
        launch_row1 = QHBoxLayout()
        launch_row1.setSpacing(10)
        self.chk_add_gamemode = QCheckBox(tr("launch.gamemode"))
        self.chk_add_mangohud = QCheckBox(tr("launch.mangohud"))
        launch_row1.addWidget(self.chk_add_gamemode)
        launch_row1.addWidget(self.chk_add_mangohud)
        launch_row1.addStretch()

        launch_row2 = QHBoxLayout()
        launch_row2.setSpacing(10)
        self.chk_add_dlss = QCheckBox(tr("launch.dlss_upgrade"))
        self.chk_add_dlss.setVisible(self._get_gpu_profile().has_nvidia)
        self.btn_add_launch_adv = QPushButton(tr("add.btn_launch_advanced"))
        self.btn_add_launch_adv.clicked.connect(self._open_add_launch_advanced)
        launch_row2.addWidget(self.chk_add_dlss)
        launch_row2.addWidget(self.btn_add_launch_adv)
        launch_row2.addStretch()

        launch_opts_box.addLayout(launch_row1)
        launch_opts_box.addLayout(launch_row2)
        form.addRow("", launch_opts_box)
        self._reset_add_launch_options()

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

        lbl_standalone_icon = QLabel(tr("add.group_standalone_icon"))
        lbl_standalone_icon.setObjectName("MutedText")
        lbl_standalone_icon.setStyleSheet("font-weight: 700; margin-top: 4px;")
        preview_lay.addWidget(lbl_standalone_icon)

        icon_btn_row = QHBoxLayout()
        icon_btn_row.setSpacing(6)
        self.btn_pick_icon = QPushButton(tr("add.btn_pick_icon"))
        self.btn_pick_icon.clicked.connect(self._pick_custom_icon)
        icon_btn_row.addWidget(self.btn_pick_icon, 1)
        self.btn_clear_icon = QPushButton(tr("add.btn_clear_icon"))
        self.btn_clear_icon.clicked.connect(self._clear_custom_icon)
        self.btn_clear_icon.setVisible(False)
        icon_btn_row.addWidget(self.btn_clear_icon)
        preview_lay.addLayout(icon_btn_row)
        preview_lay.addStretch()
        cfg_split.addWidget(preview_wrap)
        cfg_split.setStretchFactor(0, 1)
        cfg_split.setStretchFactor(1, 0)
        cfg_split.setSizes([460, 240])

        cfg_layout.addWidget(cfg_split, 1)

        actions_row = FlowLayout(spacing=8)
        self.btn_add_steam = QPushButton(tr("install.btn_add_steam"))
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        actions_row.addWidget(self.btn_add_steam)

        self.btn_add_standalone = QPushButton(tr("install.btn_add_standalone"))
        self.btn_add_standalone.clicked.connect(self._add_selected_standalone)
        self.chk_keep_in_place.toggled.connect(self._update_standalone_install_label)
        self._update_standalone_install_label()
        actions_row.addWidget(self.btn_add_standalone)

        self.btn_remove_existing = QPushButton(tr("add.btn_remove_existing"))
        self.btn_remove_existing.setObjectName("dangerBtn")
        self.btn_remove_existing.clicked.connect(self._remove_existing_from_add)
        self.btn_remove_existing.setVisible(False)
        actions_row.addWidget(self.btn_remove_existing)
        cfg_layout.addLayout(actions_row)

        add_vsplit.addWidget(cfg_card)
        add_vsplit.setStretchFactor(0, 1)
        add_vsplit.setStretchFactor(1, 2)
        add_vsplit.setSizes([200, 420])
        layout.addWidget(add_vsplit, 1)
        return page

    def _update_standalone_install_label(self) -> None:
        text = (
            tr("install.btn_add_standalone_in_place")
            if self.chk_keep_in_place.isChecked()
            else tr("install.btn_add_standalone")
        )
        self.btn_add_standalone.setText(text)

    def _switch_library_subview(self, idx: int) -> None:
        """Switch between 'My Library' (0) and 'Scan & Add Game' (1) sub-views."""
        if idx == 1:
            self.scan_add_dialog.show()
            self.scan_add_dialog.raise_()
            self.scan_add_dialog.activateWindow()


    def _build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.scan_add_dialog = QDialog(self)
        self.scan_add_dialog.setWindowTitle(tr("library.subtab_add"))
        self.scan_add_dialog.resize(1000, 780)

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
        top.addWidget(lbl)
        top.addStretch()
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
        self.btn_vdf_apply.clicked.connect(self._apply_vdf_queue)
        self.btn_vdf_clear = QPushButton(tr("vdf_queue.btn_clear"))
        self.btn_vdf_clear.clicked.connect(self._clear_vdf_queue)
        vq_lay.addWidget(self.lbl_vdf_queue, 1)
        vq_lay.addWidget(self.btn_vdf_apply)
        vq_lay.addWidget(self.btn_vdf_clear)
        self.vdf_queue_banner.setVisible(False)
        lib_layout.addWidget(self.vdf_queue_banner)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.library_search_input = QLineEdit()
        self.library_search_input.setPlaceholderText(tr("library.search_placeholder"))
        self.library_search_input.setClearButtonEnabled(True)
        self.library_search_input.textChanged.connect(self._filter_library_list)
        self.chk_batch_vdf = QCheckBox(tr("vdf_queue.chk_batch"))
        self.chk_batch_vdf.setToolTip(tr("vdf_queue.chk_batch_tip"))
        self.chk_batch_vdf.setChecked(_setting_bool(self.settings, "vdf_queue/batch_mode", False))
        self.chk_batch_vdf.toggled.connect(
            lambda checked: self.settings.setValue("vdf_queue/batch_mode", bool(checked))
        )
        self.lbl_library_count = QLabel()
        self.lbl_library_count.setObjectName("MutedText")
        filter_row.addWidget(self.library_search_input, 1)
        filter_row.addWidget(self.chk_batch_vdf)
        filter_row.addWidget(self.lbl_library_count)
        lib_layout.addLayout(filter_row)

        self.shortcuts_list = QTableWidget(0, 3)
        self.shortcuts_list.setHorizontalHeaderLabels(
            [tr("table.name"), tr("table.source"), tr("table.executable")]
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
        self.shortcuts_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
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

        self.act_bulk_migrate = QAction(tr("library.btn_bulk_migrate"), self)
        self.act_bulk_migrate.triggered.connect(self._bulk_migrate_shortcuts)

        self._game_actions_menu = QMenu(self)
        self.act_play = QAction(tr("library.btn_play"), self)
        self.act_play.triggered.connect(self._play_selected_game)
        self.act_configure_game = QAction(tr("library.btn_configure_game"), self)
        self.act_configure_game.triggered.connect(self._open_configure_game_dialog)
        self.act_migrate = QAction(tr("library.btn_migrate_one"), self)
        self.act_migrate.triggered.connect(self._migrate_selected_shortcut)
        self.act_art = QAction(tr("library.btn_update_art"), self)
        self.act_art.triggered.connect(self._update_selected_shortcut_art)
        self.act_revert_art = QAction(tr("library.btn_revert_art"), self)
        self.act_revert_art.triggered.connect(self._revert_selected_shortcut_art)
        self.act_proton = QAction(tr("library.btn_proton"), self)
        self.act_proton.triggered.connect(self._change_selected_proton)
        self.act_fallback_proton10 = QAction(tr("library.btn_fallback_proton10"), self)
        self.act_fallback_proton10.triggered.connect(self._fallback_selected_proton10)
        self.act_launch_opts = QAction(tr("library.btn_launch_opts"), self)
        self.act_launch_opts.triggered.connect(self._edit_launch_options_presets)
        self.act_winetricks = QAction(tr("library.btn_winetricks"), self)
        self.act_winetricks.triggered.connect(lambda: self._open_winetricks_dialog())
        self.act_optiscaler = QAction(tr("library.btn_optiscaler"), self)
        self.act_optiscaler.triggered.connect(lambda: self._open_optiscaler_dialog())
        self.act_run_exe_in_prefix = QAction(tr("library.btn_run_exe_in_prefix"), self)
        self.act_run_exe_in_prefix.triggered.connect(lambda: self._run_exe_in_prefix_dialog())
        self.act_export_standalone = QAction(tr("library.btn_export_standalone"), self)
        self.act_export_standalone.triggered.connect(self._export_selected_to_standalone)
        self.act_import_steam = QAction(tr("library.btn_import_steam"), self)
        self.act_import_steam.triggered.connect(self._import_selected_to_steam)
        self.act_ext_to_steam = QAction(tr("library.btn_ext_to_steam"), self)
        self.act_ext_to_steam.triggered.connect(self._external_add_to_steam)
        self.act_ext_to_standalone = QAction(tr("library.btn_ext_to_standalone"), self)
        self.act_ext_to_standalone.triggered.connect(self._external_create_standalone)
        self.act_ext_import_saves = QAction(tr("library.btn_ext_import_saves"), self)
        self.act_ext_import_saves.triggered.connect(self._external_import_saves)
        self.act_remove = QAction(tr("library.btn_remove"), self)
        self.act_remove.triggered.connect(self._remove_selected_shortcut)
        self.btn_configure_game = QPushButton(tr("library.btn_configure_game"))
        self.btn_configure_game.clicked.connect(self._open_configure_game_dialog)

        self.btn_remove_game = QPushButton(tr("library.btn_remove_game"))
        self.btn_remove_game.setObjectName("dangerBtn")
        self.btn_remove_game.clicked.connect(self._remove_selected_shortcut)

        self._tools_menu = QMenu(self)
        self.act_backup_saves = QAction(tr("library.btn_backup_saves"), self)
        self.act_backup_saves.triggered.connect(self._backup_all_saves)
        self.act_restore_saves = QAction(tr("library.btn_restore_saves"), self)
        self.act_restore_saves.triggered.connect(self._restore_saves_backup)
        self.act_save_sync = QAction(tr("library.btn_save_sync"), self)
        self.act_save_sync.triggered.connect(self._open_save_sync_dialog)
        self.act_link_prefixes = QAction(tr("library.btn_link_prefixes"), self)
        self.act_link_prefixes.triggered.connect(self._sync_prefix_links)
        self.act_bulk_proton = QAction(tr("library.btn_bulk_proton"), self)
        self.act_bulk_proton.triggered.connect(self._bulk_migrate_proton_dialog)
        self.act_manage_protons = QAction(tr("library.btn_manage_protons"), self)
        self.act_manage_protons.triggered.connect(self._open_proton_manager_dialog)
        self.act_cleanup = QAction(tr("library.btn_cleanup"), self)
        self.act_cleanup.triggered.connect(lambda: self._open_maintenance_page(auto_scan=True))
        footer = QHBoxLayout()
        footer.addWidget(self.btn_play_game)
        footer.addWidget(self.btn_configure_game)
        footer.addStretch()
        footer.addWidget(self.btn_remove_game)
        lib_layout.addLayout(footer)
        my_lib_layout.addWidget(lib_card, 1)

        scan_layout = QVBoxLayout(self.scan_add_dialog)
        scan_scroll = QScrollArea()
        scan_scroll.setWidgetResizable(True)
        scan_scroll.setFrameShape(QFrame.Shape.NoFrame)
        scan_scroll.setWidget(self._build_add_page())
        scan_layout.addWidget(scan_scroll)
        layout.addWidget(my_lib_page, 1)
        return page

    def _build_saves_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel(tr("saves.page_title"))
        title.setObjectName("PageTitle")
        subtitle = QLabel(tr("saves.page_subtitle"))
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card, card_layout = _make_card()
        folder_label = QLabel(tr("saves.default_folder", path=DEFAULT_SAVES_DIR))
        folder_label.setWordWrap(True)
        card_layout.addWidget(folder_label)

        buttons = QHBoxLayout()
        self.btn_save_backup = QPushButton(tr("library.btn_backup_saves"))
        self.btn_save_backup.clicked.connect(self.act_backup_saves.trigger)
        self.btn_save_restore = QPushButton(tr("library.btn_restore_saves"))
        self.btn_save_restore.setEnabled(self.act_restore_saves.isEnabled())
        self.act_restore_saves.changed.connect(
            lambda: self.btn_save_restore.setEnabled(self.act_restore_saves.isEnabled())
        )
        self.btn_save_restore.clicked.connect(self.act_restore_saves.trigger)
        self.btn_save_sync = QPushButton(tr("saves.btn_sync"))
        self.btn_save_sync.clicked.connect(self._open_save_sync_dialog)
        buttons.addWidget(self.btn_save_backup)
        buttons.addWidget(self.btn_save_restore)
        buttons.addWidget(self.btn_save_sync)
        buttons.addStretch()
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_maintenance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self._maint_candidates: list[CleanupCandidate] = []
        self._maint_checked: dict[Path, bool] = {}
        self._maint_active_filter = "all"
        self._maint_scanned = False

        title = QLabel(tr("maint.page_title"))
        title.setObjectName("PageTitle")
        sub = QLabel(tr("maint.page_subtitle"))
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        health_card, health_layout = _make_card()
        health_top = QHBoxLayout()
        health_lbl = QLabel(tr("maint.health_title"))
        health_lbl.setObjectName("SectionTitle")
        self.btn_maint_protons = QPushButton(tr("maint.btn_manage_protons"))
        self.btn_maint_protons.clicked.connect(self._open_proton_manager_dialog)
        self.btn_maint_refresh_health = QPushButton(tr("maint.btn_refresh_health"))
        self.btn_maint_refresh_health.clicked.connect(self._refresh_maintenance_health)
        health_top.addWidget(health_lbl)
        health_top.addStretch()
        health_top.addWidget(self.btn_maint_protons)
        health_top.addWidget(self.btn_maint_refresh_health)
        health_layout.addLayout(health_top)

        self.lbl_maint_app = QLabel()
        self.lbl_maint_umu = QLabel()
        self.lbl_maint_proton = QLabel()
        self.lbl_maint_steamrt4 = QLabel()
        self.lbl_maint_disk = QLabel()
        for lbl in (
            self.lbl_maint_app,
            self.lbl_maint_umu,
            self.lbl_maint_proton,
            self.lbl_maint_steamrt4,
            self.lbl_maint_disk,
        ):
            lbl.setWordWrap(True)
            health_layout.addWidget(lbl)
        layout.addWidget(health_card)

        batch_card, batch_layout = _make_card()
        self.maint_batch_card = batch_card
        batch_layout.addWidget(QLabel(tr("library.menu_batch")))
        for action in (self.act_bulk_migrate, self.act_bulk_proton):
            button = QPushButton(action.text())
            button.setEnabled(action.isEnabled())
            action.changed.connect(lambda a=action, b=button: b.setEnabled(a.isEnabled()))
            button.clicked.connect(lambda _checked=False, a=action: a.trigger())
            batch_layout.addWidget(button)
        layout.addWidget(batch_card)

        clean_card, clean_layout = _make_card()
        clean_top = QHBoxLayout()
        clean_lbl = QLabel(tr("maint.cleanup_title"))
        clean_lbl.setObjectName("SectionTitle")
        self.btn_maint_scan = QPushButton(tr("maint.btn_scan"))
        self.btn_maint_scan.clicked.connect(self._scan_maintenance_cleanup)
        self.btn_maint_revalidate = QPushButton(tr("cleanup.btn_revalidate"))
        self.btn_maint_revalidate.setEnabled(self.paths is not None)
        self.btn_maint_revalidate.clicked.connect(self._revalidate_maintenance_cleanup)
        clean_top.addWidget(clean_lbl)
        clean_top.addStretch()
        clean_top.addWidget(self.btn_maint_scan)
        clean_top.addWidget(self.btn_maint_revalidate)
        clean_layout.addLayout(clean_top)

        pills_flow = FlowLayout(spacing=6)
        self._maint_filter_group = QButtonGroup(self)
        self._maint_filter_group.setExclusive(True)
        filter_defs = (
            ("all", tr("maint.filter_all")),
            ("prefixes", tr("maint.filter_prefixes")),
            ("backups", tr("maint.filter_backups")),
            ("caches", tr("maint.filter_caches")),
            ("shaders", tr("maint.filter_shaders")),
            ("runtimes", tr("maint.filter_runtimes")),
        )
        for key, label in filter_defs:
            pill = QPushButton(label)
            pill.setObjectName("FilterPill")
            pill.setCheckable(True)
            if key == "all":
                pill.setChecked(True)
            pill.clicked.connect(lambda _=False, k=key: self._on_maint_filter_changed(k))
            self._maint_filter_group.addButton(pill)
            pills_flow.addWidget(pill)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addLayout(pills_flow, 1)
        self.btn_maint_toggle_filter = QPushButton(tr("maint.btn_select_all_filter"))
        self.btn_maint_toggle_filter.clicked.connect(self._toggle_maint_filter_selection)
        filter_row.addWidget(self.btn_maint_toggle_filter, 0, Qt.AlignmentFlag.AlignTop)
        clean_layout.addLayout(filter_row)

        self.maint_list = QTableWidget(0, 3)
        self.maint_list.setHorizontalHeaderLabels(
            [tr("maint.column_item"), tr("maint.column_folder"), tr("maint.column_size")]
        )
        self.maint_list.verticalHeader().setVisible(False)
        self.maint_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.maint_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.maint_list.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.maint_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.maint_list.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.maint_list.horizontalHeader().setSectionsClickable(True)
        self.maint_list.setSortingEnabled(True)
        self.maint_list.setMinimumHeight(220)
        self.maint_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.maint_list.customContextMenuRequested.connect(self._show_maint_context_menu)
        self.maint_list.itemChanged.connect(self._on_maint_item_changed)
        clean_layout.addWidget(self.maint_list, 1)

        footer = QHBoxLayout()
        self.lbl_maint_summary = QLabel()
        self.lbl_maint_summary.setWordWrap(True)
        self.btn_maint_clean_safe = QPushButton()
        self.btn_maint_clean_safe.setObjectName("primaryBtn")
        self.btn_maint_clean_safe.clicked.connect(self._maint_clean_safe)
        self.btn_maint_delete_selected = QPushButton()
        self.btn_maint_delete_selected.setObjectName("dangerBtn")
        self.btn_maint_delete_selected.clicked.connect(self._maint_delete_selected)
        footer.addWidget(self.lbl_maint_summary, 1)
        footer.addWidget(self.btn_maint_clean_safe)
        footer.addWidget(self.btn_maint_delete_selected)
        clean_layout.addLayout(footer)
        layout.addWidget(clean_card, 1)

        self._refresh_maintenance_health()
        self._populate_maint_list()
        return page

    def _open_maintenance_page(self, *, auto_scan: bool = True) -> None:
        self.stack.setCurrentIndex(3)
        if len(self._nav_buttons) > 3:
            self._nav_buttons[3].setChecked(True)
        self._refresh_maintenance_health()
        if auto_scan:
            self._scan_maintenance_cleanup()

    def _refresh_maintenance_health(self) -> None:
        mode = (
            tr("maint.mode_appimage")
            if get_running_appimage() is not None
            else tr("maint.mode_python")
        )
        self.lbl_maint_app.setText(
            tr("maint.health_app", app=__app_name__, version=__version__, mode=mode)
        )
        self._maint_health_req_id += 1
        req_id = self._maint_health_req_id
        paths_snapshot = self.paths
        caches_bytes = sum(c.size_bytes for c in self._maint_candidates)

        class _MaintHealthWorker(QThread):
            ready = Signal(int, dict)

            def __init__(self, r_id: int, paths: SteamPaths | None, cached_bytes: int) -> None:
                super().__init__()
                self._req_id = r_id
                self._paths = paths
                self._cached_bytes = cached_bytes

            def run(self) -> None:
                steam_root = find_steam_root()
                has_steam_proton = (
                    steam_root is not None and find_proton_binary(steam_root) is not None
                )
                missing_status = (
                    tr("maint.status_optional_steam")
                    if has_steam_proton
                    else tr("maint.status_missing")
                )

                umu_path = get_umu_run_path()
                umu_status = (
                    f"{tr('maint.status_ok')} ({umu_path})"
                    if umu_path.is_file()
                    else missing_status
                )

                ge_dirs = list_ge_proton11_dirs()
                proton_status = (
                    f"{len(ge_dirs)} ({ge_dirs[0].name})" if ge_dirs else missing_status
                )

                steamrt4_dir = get_steamrt4_dir()
                if not steamrt4_dir.is_dir():
                    rt4_status = missing_status
                elif validate_steamrt4_integrity():
                    rt4_status = tr("maint.status_ok")
                else:
                    rt4_status = tr("maint.status_corrupted")

                prefix_dirs: list[Path] = []
                if self._paths is not None and self._paths.compatdata_dir.is_dir():
                    prefix_dirs.append(self._paths.compatdata_dir)
                games_dir = Path.home() / "Games"
                if games_dir.is_dir():
                    try:
                        for child in games_dir.iterdir():
                            if child.is_dir():
                                for sub_name in ("prefix", "pfx", "wineprefix"):
                                    sub = child / sub_name
                                    if sub.is_dir():
                                        prefix_dirs.append(sub)
                    except OSError:
                        pass
                for extra_pfx_root in (
                    Path.home() / "Games" / "Prefixes",
                    Path.home() / "Prefixes",
                ):
                    if extra_pfx_root.is_dir():
                        prefix_dirs.append(extra_pfx_root)
                prefixes_bytes = sum(_dir_size_bytes(p) for p in prefix_dirs)

                runtime_dirs = [
                    p for p in (umu_path.parent, get_compat_tools_dir()) if p.is_dir()
                ]
                runtimes_bytes = sum(_dir_size_bytes(p) for p in runtime_dirs)

                self.ready.emit(
                    self._req_id,
                    {
                        "umu_status": umu_status,
                        "proton_status": proton_status,
                        "rt4_status": rt4_status,
                        "prefixes_bytes": prefixes_bytes,
                        "runtimes_bytes": runtimes_bytes,
                        "caches_bytes": self._cached_bytes,
                    },
                )

        def _on_health_ready(r_id: int, data: dict) -> None:
            if self._closing_pending or r_id != self._maint_health_req_id:
                return
            self.lbl_maint_umu.setText(tr("maint.health_umu", status=data["umu_status"]))
            self.lbl_maint_proton.setText(tr("maint.health_proton", status=data["proton_status"]))
            self.lbl_maint_steamrt4.setText(
                tr("maint.health_steamrt4", status=data["rt4_status"])
            )
            self.lbl_maint_disk.setText(
                tr(
                    "maint.health_disk",
                    prefixes=format_size_bytes(data["prefixes_bytes"]),
                    runtimes=format_size_bytes(data["runtimes_bytes"]),
                    caches=format_size_bytes(data["caches_bytes"]),
                )
            )

        worker = _MaintHealthWorker(req_id, paths_snapshot, caches_bytes)
        self._track_worker(worker)
        worker.ready.connect(_on_health_ready)
        worker.start()

    def _on_maint_filter_changed(self, key: str) -> None:
        self._maint_active_filter = key
        self._populate_maint_list()

    def _on_maint_item_changed(self, item: QTableWidgetItem) -> None:
        cand = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(cand, CleanupCandidate):
            self._maint_checked[cand.path] = item.checkState() == Qt.CheckState.Checked
            self._update_maint_summary()

    def _visible_maint_candidates(self) -> list[CleanupCandidate]:
        """Return cleanup candidates matching the currently active filter pill."""
        allowed_cats = _MAINT_FILTER_CATEGORIES.get(self._maint_active_filter)
        if allowed_cats is None:
            return list(self._maint_candidates)
        return [c for c in self._maint_candidates if c.category in allowed_cats]

    def _toggle_maint_filter_selection(self) -> None:
        """Toggle selection for all candidates visible in the active filter."""
        visible = self._visible_maint_candidates()
        if not visible:
            return
        all_checked = all(self._maint_checked.get(c.path, False) for c in visible)
        new_state = not all_checked
        for cand in visible:
            self._maint_checked[cand.path] = new_state
        self._populate_maint_list()

    def _populate_maint_list(self) -> None:
        visible = self._visible_maint_candidates()
        self.maint_list.blockSignals(True)
        self.maint_list.setSortingEnabled(False)
        self.maint_list.clearSpans()
        self.maint_list.setRowCount(0)
        if not visible:
            if not self._maint_scanned:
                msg = tr("maint.empty_prompt")
            elif not self._maint_candidates:
                msg = tr("cleanup.none_found")
            else:
                msg = tr("maint.filter_empty")
            self.maint_list.setRowCount(1)
            self.maint_list.setSpan(0, 0, 1, 3)
            placeholder = QTableWidgetItem(msg)
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.maint_list.setItem(0, 0, placeholder)
        else:
            for row, cand in enumerate(visible):
                self.maint_list.insertRow(row)
                item = QTableWidgetItem(_format_cleanup_candidate_label(cand))
                item.setToolTip(str(cand.path))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = self._maint_checked.get(cand.path, _cleanup_candidate_auto_check(cand))
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, cand)
                self.maint_list.setItem(row, 0, item)
                self.maint_list.setItem(row, 1, QTableWidgetItem(str(cand.path)))
                size_item = _SortableTableItem(
                    format_size_bytes(cand.size_bytes), cand.size_bytes
                )
                self.maint_list.setItem(row, 2, size_item)
        self.maint_list.setSortingEnabled(True)
        self.maint_list.blockSignals(False)
        self._update_maint_summary()

    def _update_maint_summary(self) -> None:
        visible = self._visible_maint_candidates()
        sel_items = [c for c in visible if self._maint_checked.get(c.path, False)]
        sel_count = len(sel_items)
        sel_size = format_size_bytes(sum(c.size_bytes for c in sel_items))
        safe_items = [c for c in self._maint_candidates if _cleanup_candidate_auto_check(c)]
        safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
        self.lbl_maint_summary.setText(
            tr("maint.summary", sel_size=sel_size, sel_count=sel_count, safe_size=safe_size)
        )
        self.btn_maint_clean_safe.setText(tr("maint.btn_clean_safe", size=safe_size))
        self.btn_maint_delete_selected.setText(tr("maint.btn_delete_selected", size=sel_size))
        self.btn_maint_clean_safe.setEnabled(len(safe_items) > 0)
        self.btn_maint_delete_selected.setEnabled(sel_count > 0)

        all_checked = bool(visible) and all(
            self._maint_checked.get(c.path, False) for c in visible
        )
        self.btn_maint_toggle_filter.setEnabled(bool(visible))
        self.btn_maint_toggle_filter.setText(
            tr("maint.btn_deselect_all_filter")
            if all_checked
            else tr("maint.btn_select_all_filter")
        )

    def _apply_maint_scan_results(self, candidates: list[CleanupCandidate]) -> None:
        self._maint_scanned = True
        self._maint_candidates = candidates
        self._maint_checked = {c.path: _cleanup_candidate_auto_check(c) for c in candidates}
        self._populate_maint_list()
        self._refresh_maintenance_health()

    def _scan_maintenance_cleanup(self) -> None:
        self._set_steam_busy(True)
        self.status_label.setText(tr("cleanup.scanning"))

        class _MaintScanWorker(QThread):
            found = Signal(list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths | None) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    self.found.emit(find_cleanup_candidates(self._paths, include_system_caches=True))
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _on_found(candidates: list[CleanupCandidate]) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            self._apply_maint_scan_results(candidates)
            self.status_label.setText(
                tr("cleanup.none_found") if not candidates else tr("status.ready")
            )

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            msg = tr("cleanup.scan_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker = _MaintScanWorker(self.paths)
        self._track_worker(worker)
        worker.found.connect(_on_found)
        worker.failed.connect(_on_failed)
        worker.start()

    def _revalidate_maintenance_cleanup(self) -> None:
        if not self.paths:
            return
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("cleanup.wine_running"))
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("cleanup.revalidating"))

        class _MaintRevalWorker(QThread):
            done = Signal(list, int, int, int)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    if _is_wine_game_running():
                        raise RuntimeError(tr("cleanup.wine_running"))
                    entries = load_shortcuts(self._paths.shortcuts_vdf)
                    pending_plan = plan_bulk_migration(
                        entries=entries,
                        games_root=Path.home() / "Games",
                        compatdata_dir=self._paths.compatdata_dir,
                        config_vdf=self._paths.config_vdf,
                    )
                    checked, copied = sync_all_external_saves(self._paths)
                    new_cands = find_cleanup_candidates(self._paths, include_system_caches=True)
                    self.done.emit(new_cands, checked, copied, len(pending_plan))
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _on_done(
            new_cands: list[CleanupCandidate], checked: int, copied: int, pending: int
        ) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            self._apply_maint_scan_results(new_cands)
            extra = tr("cleanup.reval_pending", pending=pending) if pending else ""
            self.status_label.setText(tr("cleanup.reval_done", checked=checked, copied=copied) + extra)

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            msg = tr("cleanup.reval_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker = _MaintRevalWorker(self.paths)
        self._track_worker(worker)
        worker.done.connect(_on_done)
        worker.failed.connect(_on_failed)
        worker.start()

    def _maint_clean_safe(self) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("cleanup.wine_running"))
            return
        safe_items = [c for c in self._maint_candidates if _cleanup_candidate_auto_check(c)]
        safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
        if not safe_items:
            QMessageBox.information(_dialog_parent(self), __app_name__, tr("maint.safe_clean_empty"))
            return
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("maint.confirm_safe_clean", count=len(safe_items), size=safe_size),
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._run_maint_delete(safe_items)

    def _maint_delete_selected(self) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("cleanup.wine_running"))
            return
        selected = [
            c for c in self._visible_maint_candidates() if self._maint_checked.get(c.path, False)
        ]
        if not selected:
            return
        unverified = [c for c in selected if c.has_saves and not c.saves_verified_migrated]
        if unverified:
            preview_lines = "\n".join(f"• {c.label}" for c in unverified[:5])
            if len(unverified) > 5:
                preview_lines += f"\n• ... (+{len(unverified) - 5})"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(__app_name__)
            box.setText(
                tr(
                    "maint.confirm_delete_unverified",
                    count=len(unverified),
                    items=preview_lines,
                )
            )
            btn_backup_del = box.addButton(
                tr("maint.btn_backup_and_delete"), QMessageBox.ButtonRole.AcceptRole
            )
            btn_del_anyway = box.addButton(
                tr("maint.btn_delete_anyway"), QMessageBox.ButtonRole.DestructiveRole
            )
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(btn_backup_del)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_backup_del:
                self._run_maint_delete(
                    selected, allow_unverified_saves=True, backup_unverified=True
                )
            elif clicked is btn_del_anyway:
                self._run_maint_delete(
                    selected, allow_unverified_saves=True, backup_unverified=False
                )
            return

        sel_size = format_size_bytes(sum(c.size_bytes for c in selected))
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("maint.confirm_delete_selected", count=len(selected), size=sel_size),
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._run_maint_delete(selected)

    def _run_maint_delete(
        self,
        items: list[CleanupCandidate],
        *,
        allow_unverified_saves: bool = False,
        backup_unverified: bool = False,
    ) -> None:
        self._set_steam_busy(True)
        self.status_label.setText(tr("cleanup.deleting", count=len(items)))

        class _MaintDeleteWorker(QThread):
            done = Signal(int, int, list, list, str)
            failed = Signal(str)

            def __init__(
                self,
                paths: SteamPaths | None,
                target_items: list[CleanupCandidate],
                allow_unverified: bool,
                do_backup_unverified: bool,
            ) -> None:
                super().__init__()
                self._paths = paths
                self._items = target_items
                self._allow_unverified = allow_unverified
                self._backup_unverified = do_backup_unverified

            def run(self) -> None:
                try:
                    if _is_wine_game_running():
                        raise RuntimeError(tr("cleanup.wine_running"))
                    backup_zip_str = ""
                    if self._backup_unverified:
                        unverified_items = [
                            c
                            for c in self._items
                            if c.has_saves and not c.saves_verified_migrated
                        ]
                        if unverified_items:
                            zip_path, _games, files_backed = backup_prefix_candidates_to_zip(
                                unverified_items
                            )
                            if files_backed > 0:
                                backup_zip_str = str(zip_path)
                    deleted, freed, errs = delete_cleanup_candidates_checked(
                        self._paths,
                        self._items,
                        include_system_caches=True,
                        allow_unverified_saves=self._allow_unverified,
                    )
                    new_cands = find_cleanup_candidates(self._paths, include_system_caches=True)
                    self.done.emit(deleted, freed, errs, new_cands, backup_zip_str)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _on_deleted(
            deleted: int,
            freed: int,
            errs: list[str],
            new_cands: list[CleanupCandidate],
            backup_zip_str: str,
        ) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            self._apply_maint_scan_results(new_cands)
            msg = tr("cleanup.deleted", deleted=deleted, size=format_size_bytes(freed))
            if backup_zip_str:
                msg += "\n" + tr("maint.backup_created_note", path=backup_zip_str)
            if errs:
                msg += tr("cleanup.delete_warnings", errs="\n".join(errs[:10]))
            self.status_label.setText(msg.splitlines()[0])
            if errs:
                QMessageBox.warning(_dialog_parent(self), __app_name__, msg)
            else:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            msg = tr("cleanup.delete_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

        worker = _MaintDeleteWorker(
            self.paths, items, allow_unverified_saves, backup_unverified
        )
        self._track_worker(worker)
        worker.done.connect(_on_deleted)
        worker.failed.connect(_on_failed)
        worker.start()

    def _show_maint_context_menu(self, pos: QPoint) -> None:
        item = self.maint_list.itemAt(pos)
        if item is None:
            return
        item = self.maint_list.item(item.row(), 0)
        if item is None:
            return
        cand = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(cand, CleanupCandidate):
            return

        menu = QMenu(self)
        act_open = menu.addAction(tr("maint.ctx_open_folder"))
        act_backup: QAction | None = None
        if cand.path.is_dir() and cand.category in (
            "Prefixo Externo",
            "external_prefix",
            "Compatdata Órfão",
            "orphan_compatdata",
        ):
            act_backup = menu.addAction(tr("maint.ctx_backup_saves"))

        chosen = menu.exec(self.maint_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_open:
            target = cand.path if cand.path.is_dir() else cand.path.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        elif act_backup is not None and chosen is act_backup:
            self._set_steam_busy(True)
            self.status_label.setText(tr("maint.backup_running"))

            class _MaintBackupWorker(QThread):
                done = Signal(str, int)
                failed = Signal(str)

                def __init__(self, target_cand: CleanupCandidate) -> None:
                    super().__init__()
                    self._cand = target_cand

                def run(self) -> None:
                    try:
                        zip_path, _games, files_backed = backup_prefix_candidates_to_zip(
                            [self._cand]
                        )
                        if files_backed <= 0:
                            try:
                                zip_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                        self.done.emit(str(zip_path), files_backed)
                    except Exception as exc:
                        self.failed.emit(_format_worker_error(exc))

            def _on_backup_done(zip_str: str, files_backed: int) -> None:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                if self._closing_pending:
                    return
                if files_backed > 0:
                    QMessageBox.information(
                        _dialog_parent(self),
                        __app_name__,
                        tr("maint.backup_done", path=zip_str, files=files_backed),
                    )
                else:
                    QMessageBox.information(_dialog_parent(self), __app_name__, tr("maint.backup_empty"))

            def _on_backup_failed(err: str) -> None:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                if self._closing_pending:
                    return
                QMessageBox.critical(
                    _dialog_parent(self), __app_name__, tr("maint.backup_error", err=err)
                )

            worker = _MaintBackupWorker(cand)
            self._track_worker(worker)
            worker.done.connect(_on_backup_done)
            worker.failed.connect(_on_backup_failed)
            worker.start()

    def _apply_saved_theme(self) -> None:
        saved = str(self.settings.value("theme", "auto"))
        idx = self.theme_combo.findData(saved)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        mode: ThemeMode = cast(ThemeMode, saved) if saved in ("auto", "dark", "light") else "auto"
        apply_theme(self.app, mode)

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

        class _RuntimePreflightWorker(QThread):
            ready = Signal(bool)

            def run(self) -> None:
                steam_root = find_steam_root()
                has_steam_proton = (
                    steam_root is not None and find_proton_binary(steam_root) is not None
                )
                needs_confirm = has_steam_proton and (
                    not get_umu_run_path().is_file() or not list_ge_proton11_dirs()
                )
                self.ready.emit(needs_confirm)

        def _on_preflight(needs_confirm: bool) -> None:
            if self._closing_pending:
                self.btn_update_runtimes.setEnabled(True)
                return
            if needs_confirm:
                ans = QMessageBox.question(
                    _dialog_parent(self),
                    __app_name__,
                    tr("runtime.confirm_optional_steamos"),
                )
                if ans != QMessageBox.StandardButton.Yes:
                    self.btn_update_runtimes.setEnabled(True)
                    self.status_label.setText(tr("status.ready"))
                    return

            worker = RuntimeUpdateWorker()
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

        preflight = _RuntimePreflightWorker()
        self._track_worker(preflight)
        preflight.ready.connect(_on_preflight)
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
                if is_gamescope_session():
                    msg = tr("app_update.done_gamescope", latest=info.latest_version)
                    self.status_label.setText(msg)
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)
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
        if p.is_file():
            self.extract_src.setText(str(p))
            if not self.extract_name.text().strip():
                self.extract_name.setText(suggest_target_name(p))

    def _pick_archive_or_installer(self) -> None:
        _pick_file_async(
            self,
            tr("install.pick_archive_title"),
            Path.home() / "Downloads",
            tr("install.pick_archive_qt"),
            lambda path: self._set_extract_source(path) if path else None,
        )

    def _pick_dest_root(self) -> None:
        start = Path(self.extract_dst_root.text().strip() or str(Path.home() / "Games"))
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
        name = self.extract_name.text().strip() or suggest_target_name(src)
        dst = Path(self.extract_dst_root.text().strip() or str(Path.home() / "Games"))

        self.extract_log.clear()
        self.extract_progress.setValue(0)
        self.btn_extract.setEnabled(False)
        self.btn_cancel_extract.setEnabled(True)
        self.btn_use_extracted.setEnabled(False)

        self._extract_worker = ExtractWorker(src, dst, name)
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
        self._nav_buttons[1].setChecked(True)
        self.stack.setCurrentIndex(1)
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

        return [], custom_entries

    def _save_scan_folders(
        self, disabled_defaults: list[str], custom_entries: list[dict[str, object]]
    ) -> None:
        """Persist scan folder configuration to QSettings and refresh label."""
        self.settings.setValue("scan_folders/disabled_defaults", json.dumps(disabled_defaults))
        self.settings.setValue("scan_folders/custom", json.dumps(custom_entries))
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
        self.settings.setValue("last_launch_opts/lsfg", bool(st.get("lsfg", False)))
        self.settings.setValue("last_launch_opts/lsfg_multiplier", int(st.get("lsfg_multiplier", 2) or 2))
        flow_val = st.get("lsfg_flow_scale")
        self.settings.setValue(
            "last_launch_opts/lsfg_flow_scale",
            "" if flow_val is None else f"{float(flow_val):g}",
        )
        self.settings.setValue("last_launch_opts/lsfg_perf_mode", bool(st.get("lsfg_perf_mode", False)))
        self.settings.setValue("last_launch_opts/lsfg_profile", str(st.get("lsfg_profile") or ""))
        self.settings.setValue("last_launch_opts/winedll", winedll.strip())

    def _load_last_launch_prefs(self) -> tuple[str, str]:
        """Load last-used launch option preferences from QSettings."""
        if not _setting_bool(self.settings, "last_launch_opts/has_saved", False):
            return "", ""
        mh_level = _setting_int(self.settings, "last_launch_opts/mh_level", 0)
        mh_fps = _setting_int(self.settings, "last_launch_opts/mh_fps_limit", 0)
        lsfg_mult = _setting_int(self.settings, "last_launch_opts/lsfg_multiplier", 2)
        raw_flow = str(self.settings.value("last_launch_opts/lsfg_flow_scale", "") or "").strip()
        lsfg_flow: float | None = None
        if raw_flow:
            try:
                lsfg_flow = float(raw_flow)
            except ValueError:
                lsfg_flow = None
        try:
            opts = apply_launch_presets(
                "",
                gamemode=_setting_bool(self.settings, "last_launch_opts/gamemode", False),
                mangohud=_setting_bool(self.settings, "last_launch_opts/mangohud", False),
                wined3d=_setting_bool(self.settings, "last_launch_opts/wined3d", False),
                no_esync=_setting_bool(self.settings, "last_launch_opts/no_esync", False),
                no_fsync=_setting_bool(self.settings, "last_launch_opts/no_fsync", False),
                winedlloverrides="",
                dlss_upgrade=_setting_bool(self.settings, "last_launch_opts/dlss_upgrade", False),
                mangohud_level=mh_level if mh_level in (1, 2, 3, 4) else None,
                nvidia_nvapi=_setting_bool(self.settings, "last_launch_opts/nvapi", False),
                prime_offload=_setting_bool(self.settings, "last_launch_opts/prime", False),
                proton_wayland=_setting_bool(self.settings, "last_launch_opts/wayland", False),
                mangohud_position=str(self.settings.value("last_launch_opts/mh_position", "") or ""),
                mangohud_fps_limit=mh_fps if mh_fps > 0 else None,
                lsfg=_setting_bool(self.settings, "last_launch_opts/lsfg", False),
                lsfg_multiplier=lsfg_mult if lsfg_mult in (2, 3, 4) else 2,
                lsfg_flow_scale=lsfg_flow,
                lsfg_perf_mode=_setting_bool(self.settings, "last_launch_opts/lsfg_perf_mode", False),
                lsfg_profile=str(self.settings.value("last_launch_opts/lsfg_profile", "") or ""),
            )
        except ValueError:
            opts = ""
        winedll = str(self.settings.value("last_launch_opts/winedll", "") or "").strip()
        return opts, winedll

    def _reset_add_launch_options(self) -> None:
        opts, winedll = self._load_last_launch_prefs()
        self._add_launch_options = opts
        self._add_winedlloverrides = winedll
        st = _parse_launch_preset_state(opts)
        self.chk_add_gamemode.setChecked(bool(st["gamemode"]))
        self.chk_add_mangohud.setChecked(bool(st["mangohud"]))
        self.chk_add_dlss.setChecked(bool(st["dlss_upgrade"]))

    def _is_add_exe_native(self) -> bool:
        exe_text = self.add_exe_combo.currentText().strip()
        if not exe_text:
            return False
        exe_path = Path(exe_text)
        return exe_path.is_file() and detect_exe_kind(exe_path) == "linux_elf"

    def _resolve_add_launch_options(self) -> tuple[str, str]:
        is_native = self._is_add_exe_native()
        st = _parse_launch_preset_state(self._add_launch_options)
        try:
            merged_opts = apply_launch_presets(
                self._add_launch_options,
                gamemode=self.chk_add_gamemode.isChecked(),
                mangohud=self.chk_add_mangohud.isChecked(),
                wined3d=bool(st["wined3d"]),
                no_esync=bool(st["no_esync"]),
                no_fsync=bool(st["no_fsync"]),
                winedlloverrides="",
                dlss_upgrade=False if is_native else self.chk_add_dlss.isChecked(),
                mangohud_level=st["mh_level"] if st["mh_level"] in (1, 2, 3, 4) else None,
                nvidia_nvapi=bool(st["nvapi"]),
                prime_offload=bool(st["prime"]),
                proton_wayland=bool(st["wayland"]),
                mangohud_position=str(st.get("mh_position") or ""),
                mangohud_fps_limit=int(st.get("mh_fps_limit") or 0) or None,
                lsfg=bool(st.get("lsfg", False)),
                lsfg_multiplier=int(st.get("lsfg_multiplier", 2) or 2),
                lsfg_flow_scale=(
                    st.get("lsfg_flow_scale")
                    if isinstance(st.get("lsfg_flow_scale"), (int, float))
                    else None
                ),
                lsfg_perf_mode=bool(st.get("lsfg_perf_mode", False)),
                lsfg_profile=str(st.get("lsfg_profile") or ""),
            )
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
        st = _parse_launch_preset_state(new_opts)
        self.chk_add_gamemode.setChecked(bool(st["gamemode"]))
        self.chk_add_mangohud.setChecked(bool(st["mangohud"]))
        self.chk_add_dlss.setChecked(bool(st["dlss_upgrade"]))

    def _populate_game_form(self, game: DetectedGame, from_extract: bool = False) -> None:
        self._search_req_id += 1
        self._custom_icon_path = None
        self.btn_clear_icon.setVisible(False)
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
        self._update_remove_existing_state()

    def _on_manual_exe_picked(self, path: str) -> None:
        if not path:
            return
        self._search_req_id += 1
        self._custom_icon_path = None
        self.btn_clear_icon.setVisible(False)
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
        else:
            self.preview_label.setText(tr("add.art_placeholder"))
        self._update_remove_existing_state()

    def _on_exe_changed(self, text: str) -> None:
        """Update Proton default and Remove button visibility when the executable changes."""
        cleaned = text.strip()
        is_elf = False
        if cleaned:
            exe_path = Path(cleaned)
            is_elf = exe_path.is_file() and detect_exe_kind(exe_path) == "linux_elf"
        if is_elf:
            self.chk_proton.setChecked(False)
            self.chk_proton.setEnabled(False)
            self.chk_add_dlss.setChecked(False)
            self.chk_add_dlss.setEnabled(False)
        else:
            self.chk_proton.setEnabled(True)
            self.chk_add_dlss.setEnabled(True)
        self._update_remove_existing_state()

    def _pick_custom_icon(self) -> None:
        _pick_file_async(
            self,
            tr("add.pick_icon_title"),
            Path.home(),
            tr("add.pick_icon_filter"),
            self._on_custom_icon_picked,
        )

    def _on_custom_icon_picked(self, path: str) -> None:
        if not path:
            return
        icon_path = Path(path)
        pix = QPixmap(str(icon_path))
        self._custom_icon_path = icon_path
        self.btn_clear_icon.setVisible(True)
        if not pix.isNull():
            self._display_preview_pixmap(pix)
        else:
            self.preview_label.setText(icon_path.name)

    def _clear_custom_icon(self) -> None:
        self._custom_icon_path = None
        self.btn_clear_icon.setVisible(False)
        self.preview_label.clear()
        if self.steam_match_combo.count() > 0:
            self._on_steam_match_changed()
        else:
            self.preview_label.setText(tr("add.art_placeholder"))

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
        self._search_worker = worker
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
        if self._custom_icon_path is not None:
            return
        appid = self.steam_match_combo.currentData()
        self._preview_req_id += 1
        if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
            self.preview_label.setText(tr("add.art_placeholder"))
            return
        req_id = self._preview_req_id
        self.preview_label.setText(tr("add.art_loading"))
        worker = PreviewWorker(req_id, appid)
        self._preview_worker = worker
        self._track_worker(worker)
        worker.loaded.connect(self._set_preview_bytes)
        worker.failed.connect(self._on_preview_failed)
        worker.start()

    def _on_preview_failed(self, req_id: int) -> None:
        if req_id != self._preview_req_id or self._custom_icon_path is not None:
            return
        self.preview_label.setText(tr("add.art_placeholder"))

    def _set_preview_bytes(self, req_id: int, raw: bytes) -> None:
        if req_id != self._preview_req_id or self._custom_icon_path is not None:
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
        if detect_exe_kind(exe_path) == "dos_mz":
            QMessageBox.warning(_dialog_parent(self), __app_name__, tr("add.dos_unsupported", name=exe_path.name))
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

            self._set_steam_busy(True)
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

        games_dir = get_games_dir()
        try:
            game_dir.resolve().relative_to(games_dir.resolve())
            inside_games_dir = True
        except ValueError:
            inside_games_dir = False

        def _start_install(final_keep_in_place: bool, final_single_exe_only: bool) -> None:
            self._set_steam_busy(True)
            worker = StandaloneInstallWorker(
                app_name=app_name,
                exe_path=exe_path,
                steam_appid=steam_appid,
                create_menu_shortcut=create_menu,
                create_desktop_shortcut=create_desktop,
                icon_path=self._custom_icon_path,
                keep_in_place=final_keep_in_place,
                launch_options=launch_opts,
                winedlloverrides=winedll,
                game_dir=game_dir,
                single_exe_only=final_single_exe_only,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(game: StandaloneGame, bwrap_warn: str) -> None:
                self._set_steam_busy(False)
                self._refresh_shortcuts()
                msg = tr("add.done_standalone", name=game.name, path=str(game.game_dir))
                self.status_label.setText(msg)
                if self._closing_pending:
                    return
                if bwrap_warn:
                    self._show_bwrap_warning_dialog(bwrap_warn)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._set_steam_busy(False)
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        if keep_in_place or inside_games_dir:
            _start_install(keep_in_place, single_exe_only)
            return

        class _DirStatsWorker(QThread):
            ready = Signal(int, int)

            def run(self) -> None:
                try:
                    count = sum(1 for _ in game_dir.iterdir())
                except OSError:
                    count = 0
                try:
                    size_b = (
                        exe_path.stat().st_size
                        if inferred_container
                        else _dir_size_bytes(game_dir)
                    )
                except OSError:
                    size_b = 0
                self.ready.emit(count, size_b)

        self._set_steam_busy(True)
        stats_worker = _DirStatsWorker()
        self._track_worker(stats_worker)

        def _on_stats_ready(item_count: int, total_bytes: int) -> None:
            self._set_steam_busy(False)
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
        self._set_steam_busy(False)
        self.status_label.setText(msg)
        self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.information(_dialog_parent(self), __app_name__, msg)

    def _on_add_failed(self, err: str, partial_written: bool = False) -> None:
        self._set_steam_busy(False)
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
        rows: list[tuple[str, str, str, tuple[str, object]]] = [
            (entry.app_name, "Steam", entry.exe, ("steam", entry)) for entry in entries
        ]
        rows.extend(
            (game.name, tr("library.tag_standalone"), str(game.exe_path), ("standalone", game))
            for game in standalone_games
        )
        rows.extend((ext.name, ext.source.title(), str(ext.exe_path), ("external", ext)) for ext in external_games)
        for row, (name, source, executable, data) in enumerate(rows):
            self.shortcuts_list.insertRow(row)
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, data)
            self.shortcuts_list.setItem(row, 0, item)
            self.shortcuts_list.setItem(row, 1, QTableWidgetItem(source))
            self.shortcuts_list.setItem(row, 2, QTableWidgetItem(executable))
        self.shortcuts_list.setSortingEnabled(True)

        self._filter_library_list()
        self._update_library_action_states()

    def _refresh_shortcuts(self) -> None:
        self._shortcuts_req_id += 1
        req_id = self._shortcuts_req_id
        paths_snapshot = self.paths

        class _ShortcutsRefreshWorker(QThread):
            ready = Signal(int, list, list, list, str)

            def __init__(self, r_id: int, paths: SteamPaths | None) -> None:
                super().__init__()
                self._req_id = r_id
                self._paths = paths

            def run(self) -> None:
                entries: list[ShortcutEntry] = []
                vdf_err = ""
                if self._paths is not None:
                    try:
                        entries = load_shortcuts(self._paths.shortcuts_vdf)
                    except Exception as exc:
                        vdf_err = str(exc)
                standalone_games = list_standalone_games()
                external_games = scan_external_launcher_games()
                self.ready.emit(
                    self._req_id, entries, standalone_games, external_games, vdf_err
                )

        def _on_loaded(
            r_id: int,
            entries: list[ShortcutEntry],
            standalone_games: list[StandaloneGame],
            external_games: list[ExternalLauncherGame],
            vdf_err: str,
        ) -> None:
            if self._closing_pending or r_id != self._shortcuts_req_id:
                return
            self._apply_shortcuts_data(
                paths_snapshot, entries, standalone_games, external_games, vdf_err
            )

        worker = _ShortcutsRefreshWorker(req_id, paths_snapshot)
        self._track_worker(worker)
        worker.ready.connect(_on_loaded)
        worker.start()

    def _filter_library_list(self) -> None:
        """Filter Steam, Standalone, and External library items live and update the count label."""
        query = self.library_search_input.text().strip().lower()
        total = 0
        shown = 0
        for i in range(self.shortcuts_list.rowCount()):
            item = self.shortcuts_list.item(i, 0)
            if item is None:
                continue
            total += 1
            visible = not query or any(
                query in cell.text().lower()
                for column in range(self.shortcuts_list.columnCount())
                if (cell := self.shortcuts_list.item(i, column)) is not None
            )
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

    def _selected_shortcut(self) -> ShortcutEntry | None:
        sel = self._selected_library_item()
        if sel is None or sel[0] != "steam":
            return None
        return sel[1] if isinstance(sel[1], ShortcutEntry) else None

    def _update_library_action_states(self) -> None:
        sel = self._selected_library_item()
        is_steam = sel is not None and sel[0] == "steam"
        is_standalone = sel is not None and sel[0] == "standalone"
        is_external = sel is not None and sel[0] == "external"
        can_write_vdf = bool(self.paths) and not self._vdf_corrupt

        self.btn_configure_game.setEnabled(sel is not None)
        self.btn_play_game.setEnabled(sel is not None)
        self.btn_remove_game.setEnabled(sel is not None)
        self.act_play.setVisible(True)
        self.act_play.setEnabled(sel is not None)
        self.act_configure_game.setVisible(sel is not None)
        self.act_configure_game.setEnabled(sel is not None)
        self.act_remove.setText(
            tr("library.btn_remove_standalone") if is_standalone else tr("library.btn_remove")
        )

        # Hide mutually exclusive per-game actions instead of merely disabling them
        for act in (
            self.act_migrate,
            self.act_art,
            self.act_revert_art,
            self.act_export_standalone,
        ):
            act.setVisible(is_steam)
        self.act_import_steam.setVisible(is_standalone)
        self.act_ext_to_steam.setVisible(is_external)
        self.act_ext_to_standalone.setVisible(is_external)
        self.act_ext_import_saves.setVisible(is_external)
        self.act_launch_opts.setVisible(sel is not None)
        is_native = sel is not None and _is_native_linux_game(sel[0], sel[1])
        self.act_proton.setVisible((is_steam or is_standalone) and not is_native)
        self.act_fallback_proton10.setVisible((is_steam or is_standalone) and not is_native)
        ext_prefix = getattr(sel[1], "prefix_dir", None) if is_external and sel is not None else None
        has_ext_prefix = (
            is_external
            and ext_prefix is not None
            and Path(ext_prefix).is_dir()
        )
        self.act_winetricks.setVisible((not is_native) and (not is_external or has_ext_prefix))
        self.act_optiscaler.setVisible(not is_native)
        self.act_run_exe_in_prefix.setVisible((is_steam or is_standalone) and not is_native)
        self.act_remove.setVisible(not is_external)

        self.act_migrate.setEnabled(is_steam and can_write_vdf)
        self.act_art.setEnabled(is_steam and bool(self.paths))
        self.act_revert_art.setEnabled(is_steam and bool(self.paths))
        self.act_proton.setEnabled(((is_steam and can_write_vdf) or is_standalone) and not is_native)
        self.act_fallback_proton10.setEnabled(
            ((is_steam and can_write_vdf) or is_standalone) and not is_native
        )
        self.act_export_standalone.setEnabled(is_steam and bool(self.paths))
        self.act_import_steam.setEnabled(is_standalone and can_write_vdf)
        self.act_ext_to_steam.setEnabled(is_external and can_write_vdf)
        self.act_ext_to_standalone.setEnabled(is_external)
        self.act_ext_import_saves.setEnabled(is_external and has_ext_prefix)
        self.act_launch_opts.setEnabled((is_steam and can_write_vdf) or is_standalone or is_external)
        self.act_winetricks.setEnabled(
            (is_steam and bool(self.paths))
            or (is_standalone and not is_native)
            or (has_ext_prefix and not is_native)
        )
        self.act_optiscaler.setEnabled(
            sel is not None
            and not is_native
            and ((is_steam and bool(self.paths)) or is_standalone or is_external)
        )
        self.act_run_exe_in_prefix.setEnabled(
            ((is_steam and bool(self.paths)) or is_standalone) and not is_native
        )
        self.act_remove.setEnabled((is_steam and can_write_vdf) or is_standalone)
        self.btn_remove_game.setEnabled(self.act_remove.isEnabled())
        self.btn_remove_game.setVisible(not is_external)

    def is_batch_vdf_enabled(self) -> bool:
        """Return True if Steam VDF edits should be queued for batch application."""
        return bool(self.chk_batch_vdf.isChecked())

    def enqueue_vdf_edit(self, edit: QueuedVdfEdit) -> None:
        """Enqueue a VDF edit and update the library queue banner."""
        self._vdf_queue.enqueue(edit)
        self._update_vdf_queue_banner()

    def _update_vdf_queue_banner(self) -> None:
        count = self._vdf_queue.pending_count()
        if count <= 0:
            self.vdf_queue_banner.setVisible(False)
            return
        self.lbl_vdf_queue.setText(tr("vdf_queue.banner_text", count=count))
        self.vdf_queue_banner.setVisible(True)

    def _clear_vdf_queue(self) -> None:
        self._vdf_queue.clear()
        self._update_vdf_queue_banner()
        self.status_label.setText(tr("vdf_queue.cleared"))

    def _apply_vdf_queue(self) -> None:
        if not self.paths or self._vdf_queue.pending_count() <= 0:
            return
        paths = self.paths

        def _proceed() -> None:
            if self._closing_pending:
                return
            self._set_steam_busy(True)
            worker = SteamOpWorker(
                lambda: self._vdf_queue.apply_all(paths),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(res: object) -> None:
                self._set_steam_busy(False)
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
                    if not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._set_steam_busy(False)
                self._update_vdf_queue_banner()
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

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
            self._set_steam_busy(True)
            worker = SteamOpWorker(
                lambda: create_steam_shortcut_from_external(
                    paths, ext, launch_options=launch_opts
                ),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(_res: object) -> None:
                self._set_steam_busy(False)
                self._refresh_shortcuts()
                msg = tr("library.ext_added_steam", name=ext.name)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._set_steam_busy(False)
                if not self._closing_pending:
                    QMessageBox.critical(_dialog_parent(self), __app_name__, err)

            worker.finished_ok.connect(_ok)
            worker.failed.connect(_err)
            worker.start()

        self._confirm_close_steam_async(_proceed)

    def _external_create_standalone(self) -> None:
        sel = self._selected_library_item()
        if not sel or sel[0] != "external" or not isinstance(sel[1], ExternalLauncherGame):
            return
        ext = sel[1]
        launch_opts = load_external_launch_presets(ext)
        self._set_steam_busy(True)

        class _ExtStandaloneWorker(QThread):
            finished_ok = Signal(object)
            failed = Signal(str)

            def run(self) -> None:
                try:
                    game = create_standalone_from_external(ext, launch_options=launch_opts)
                    self.finished_ok.emit(game)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        worker = _ExtStandaloneWorker()
        self._track_worker(worker)

        def _ok(_game: object) -> None:
            self._set_steam_busy(False)
            self._refresh_shortcuts()
            msg = tr("library.ext_created_standalone", name=ext.name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
        auto_snap = self._is_auto_snapshot_enabled()

        class _ExtImportSavesWorker(QThread):
            finished_ok = Signal(int)
            failed = Signal(str)

            def run(self) -> None:
                try:
                    snap_fn = (
                        (lambda pfx, gname: snapshot_prefix_saves(pfx, gname, reason="ext-import"))
                        if auto_snap
                        else None
                    )
                    copied = import_external_saves_to_target(
                        ext, target_pfx, target_label, auto_snapshot_fn=snap_fn
                    )
                    self.finished_ok.emit(copied)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        worker = _ExtImportSavesWorker()
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
        return _setting_bool(self.settings, "safety/auto_snapshot", True)

    def _make_pre_snapshot_fn(self) -> Callable[[Path, str, str, str], Path | None]:
        """Capture safety/auto_snapshot on the main thread for worker threads."""
        enabled = self._is_auto_snapshot_enabled()

        def _snap(
            prefix_dir: Path,
            game_name: str,
            reason: str,
            key_prefix: str = "snap",
        ) -> Path | None:
            return self._auto_snapshot_if_enabled(
                prefix_dir,
                game_name,
                reason,
                key_prefix=key_prefix,
                enabled=enabled,
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
        """Create a pre-operation save snapshot when safety/auto_snapshot is enabled."""
        if enabled is None:
            local_settings = QSettings(__app_name__, __app_name__)
            enabled = _setting_bool(local_settings, "safety/auto_snapshot", True)
        if not enabled:
            return None
        if not prefix_dir.is_dir():
            return None
        try:
            return snapshot_prefix_saves(
                prefix_dir,
                game_name,
                reason,
                key_prefix=key_prefix,
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

    def _play_selected_game(self) -> None:
        """Launch the selected Steam, Standalone, or External game."""
        sel = self._selected_library_item()
        if not sel:
            return
        kind, obj = sel
        if isinstance(obj, ShortcutEntry):
            rungameid = (obj.appid_u32 << 32) | 0x02000000
            QDesktopServices.openUrl(QUrl(f"steam://rungameid/{rungameid}"))
            self.status_label.setText(tr("play.launched", name=obj.app_name))
            return
        title_name = obj.name

        paths_snap = self.paths

        class _LaunchPrepWorker(QThread):
            ready = Signal(str, str, list, object, object)

            def run(self) -> None:
                try:
                    if kind == "standalone":
                        assert isinstance(obj, StandaloneGame)
                        fallback_name = ""
                        if not obj.native_linux:
                            if is_win32_prefix(obj.prefix_dir):
                                self.ready.emit("win32", "", [], None, None)
                                return
                            _, fallback_name_opt = resolve_standalone_proton_fallback(obj)
                            fallback_name = fallback_name_opt or ""
                            if fallback_name:
                                update_standalone_launch_options(
                                    obj.game_dir,
                                    winedlloverrides=obj.winedlloverrides,
                                    launch_options=obj.launch_options,
                                    proton_name=fallback_name,
                                )
                        self.ready.emit(
                            "ok",
                            fallback_name,
                            ["bash", str(obj.launch_script)],
                            obj.game_dir,
                            None,
                        )
                        return

                    if kind == "external":
                        assert isinstance(obj, ExternalLauncherGame)
                        if obj.launch_cmd:
                            self.ready.emit("ok", "", list(obj.launch_cmd), None, None)
                            return
                        if obj.runner == "linux":
                            self.ready.emit("ok", "", [str(obj.exe_path)], obj.game_dir, None)
                            return
                        umu_run = get_umu_run_path()
                        if not umu_run.is_file():
                            self.ready.emit("warn", tr("play.umu_missing"), [], None, None)
                            return
                        proton_dir: Path | None = None
                        ge_dirs = list_ge_proton11_dirs()
                        if ge_dirs:
                            proton_dir = ge_dirs[0]
                        else:
                            installed_tools = list_installed_compat_tools()
                            if installed_tools:
                                proton_dir = installed_tools[0].path
                            elif paths_snap is not None:
                                proton_bin = find_proton_binary(paths_snap.steam_root)
                                if proton_bin is not None:
                                    proton_dir = proton_bin.parent
                        if proton_dir is None:
                            self.ready.emit("warn", tr("proton.none_found"), [], None, None)
                            return
                        prefix_dir = (
                            obj.prefix_dir
                            if obj.prefix_dir is not None
                            else (
                                Path.home()
                                / "Games"
                                / ".prefixes"
                                / sanitize_folder_name(obj.name)
                            )
                        )
                        prefix_dir.mkdir(parents=True, exist_ok=True)
                        env = build_umu_env(prefix_dir, proton_dir)
                        self.ready.emit(
                            "ok",
                            "",
                            [str(umu_run), str(obj.exe_path)],
                            obj.game_dir,
                            env,
                        )
                except Exception as exc:
                    self.ready.emit("error", str(exc), [], None, None)

        def _on_prep_ready(
            status: str,
            msg_or_fallback: str,
            cmd: list[str],
            cwd: Path | None,
            env: dict[str, str] | None,
        ) -> None:
            if self._closing_pending:
                return
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
            try:
                _start_detached_process(cmd, cwd=cwd, env=env)
                self.status_label.setText(tr("play.launched", name=title_name))
            except Exception as exc:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("play.error", err=str(exc)))

        worker = _LaunchPrepWorker()
        self._track_worker(worker)
        worker.ready.connect(_on_prep_ready)
        worker.start()

    def _set_vdf_write_enabled(self, enabled: bool) -> None:
        for w in (
            self.btn_add_steam,
            self.act_bulk_migrate,
            self.act_migrate,
            self.act_proton,
            self.act_bulk_proton,
            self.act_launch_opts,
            self.act_restore_saves,
            self.act_link_prefixes,
            self.act_import_steam,
            self.act_remove,
        ):
            w.setEnabled(enabled)

    def _set_steam_busy(self, busy: bool) -> None:
        enabled = not busy
        for w in (
            self.btn_redetect_steam,
            self.btn_reload,
            self.btn_add_standalone,
            self.shortcuts_list,
            self.btn_play_game,
            self.btn_configure_game,
            self.btn_remove_game,
            self.btn_save_backup,
            self.btn_save_restore,
            self.btn_save_sync,
            self.act_play,
            self.act_art,
            self.act_revert_art,
            self.act_backup_saves,
            self.act_save_sync,
            self.act_manage_protons,
            self.act_cleanup,
            self.act_export_standalone,
            self.btn_maint_protons,
            self.btn_maint_scan,
            self.btn_maint_clean_safe,
            self.btn_maint_delete_selected,
            self.btn_maint_toggle_filter,
        ):
            w.setEnabled(enabled)
        self.btn_maint_revalidate.setEnabled(enabled and (self.paths is not None))
        self._set_vdf_write_enabled(enabled and bool(self.paths) and not self._vdf_corrupt)
        if enabled:
            self._update_library_action_states()
            self._update_maint_summary()
        self._update_remove_existing_state()

    def _update_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        exe_clean = entry.exe.strip().strip('"')
        is_flatpak_or_native = ("flatpak" in exe_clean.lower()) or (
            bool(exe_clean) and not exe_clean.lower().endswith(".exe")
        )
        if is_flatpak_or_native:
            ans = QMessageBox.question(
                _dialog_parent(self),
                __app_name__,
                tr("art.flatpak_confirm", name=entry.app_name, exe=exe_clean),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        self._set_steam_busy(True)
        self.status_label.setText(tr("art.searching", name=entry.app_name))

        class _ArtSearchWorker(QThread):
            found = Signal(list)
            failed = Signal(str)

            def __init__(self, app_name: str) -> None:
                super().__init__()
                self._app_name = app_name

            def run(self) -> None:
                try:
                    self.found.emit(search_steam_games(self._app_name, limit=5))
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _on_search_found(matches: list[SteamMatch]) -> None:
            if self._closing_pending:
                self._set_steam_busy(False)
                return
            if not matches:
                self._set_steam_busy(False)
                msg = tr("art.not_found", name=entry.app_name)
                self.status_label.setText(msg)
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)
                return
            labels = [tr("art.match_item", name=m.name, appid=m.appid) for m in matches]
            choice, ok = QInputDialog.getItem(
                _dialog_parent(self),
                tr("art.pick_title"),
                tr("art.pick_label", name=entry.app_name),
                labels,
                0,
                False,
            )
            if not ok or not choice:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                return
            chosen = matches[labels.index(choice)]

            class _ArtDownloadWorker(QThread):
                done = Signal(str)
                failed = Signal(str)

                def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry, match: SteamMatch) -> None:
                    super().__init__()
                    self._paths = paths
                    self._shortcut = shortcut
                    self._match = match

                def run(self) -> None:
                    try:
                        res = download_steam_artwork(
                            self._match.appid, self._shortcut.appid_u32, self._paths.grid_dir
                        )
                        files_str = ", ".join(res.saved_files.keys()) or tr("art.none")
                        self.done.emit(
                            tr("art.updated", name=self._match.name, files=files_str)
                        )
                    except Exception as exc:
                        self.failed.emit(_format_worker_error(exc))

            if self.paths is None:
                return
            dl_worker = _ArtDownloadWorker(self.paths, entry, chosen)
            self._track_worker(dl_worker)
            dl_worker.done.connect(_ok)
            dl_worker.failed.connect(_err)
            dl_worker.start()

        worker = _ArtSearchWorker(entry.app_name)
        self._track_worker(worker)

        def _ok(msg: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("art.error", err=err))

        worker.found.connect(_on_search_found)
        worker.failed.connect(_err)
        worker.start()

    def _revert_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        bak_dir = self.paths.grid_dir / ".deckinstall_art_bak" / str(entry.appid_u32)
        bak_files = [p.name for p in sorted(bak_dir.iterdir()) if p.is_file()] if bak_dir.is_dir() else []
        if not bak_files:
            QMessageBox.information(
                _dialog_parent(self), __app_name__, tr("art.revert_none", name=entry.app_name)
            )
            return
        if (
            QMessageBox.question(
                _dialog_parent(self),
                __app_name__,
                tr("art.revert_confirm", name=entry.app_name, files=", ".join(bak_files)),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        paths = self.paths
        self._set_steam_busy(True)
        self.status_label.setText(tr("art.revert_confirm", name=entry.app_name, files=", ".join(bak_files)))

        worker = SteamOpWorker(lambda: restore_steam_artwork_backup(entry.appid_u32, paths.grid_dir))
        self._track_worker(worker)

        def _ok(restored: object) -> None:
            self._set_steam_busy(False)
            files_list = list(restored) if isinstance(restored, list) else []
            msg = tr("art.revert_done", name=entry.app_name, files=", ".join(files_list))
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("art.revert_error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _check_steam_running_async(
        self,
        on_result: Callable[[bool], None],
    ) -> None:
        """Check whether Steam is running on a tracked background thread."""

        class _SteamRunningWorker(QThread):
            ready = Signal(bool)

            def run(self) -> None:
                self.ready.emit(is_steam_running())

        worker = _SteamRunningWorker()
        self._track_worker(worker)

        def _done(running: bool) -> None:
            if not self._closing_pending:
                on_result(running)

        worker.ready.connect(_done)
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
            if running:
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

            class _StandaloneProtonPrepWorker(QThread):
                ready = Signal(bool, list)

                def run(self) -> None:
                    if is_win32_prefix(sa.prefix_dir):
                        self.ready.emit(True, [])
                        return
                    tools: list[tuple[str, str]] = []
                    seen_ids: set[str] = set()
                    for ct in list_installed_compat_tools():
                        if ct.tool_id not in seen_ids:
                            seen_ids.add(ct.tool_id)
                            tools.append((ct.tool_id, ct.folder_name or ct.tool_id))
                    if paths_snap:
                        for tid, disp in list_installed_protons(paths_snap.steam_root):
                            if tid not in seen_ids:
                                seen_ids.add(tid)
                                tools.append((tid, disp))
                    self.ready.emit(False, tools)

            def _on_standalone_prep(is_win32: bool, tools: list[tuple[str, str]]) -> None:
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

            prep_worker = _StandaloneProtonPrepWorker()
            self._track_worker(prep_worker)
            prep_worker.ready.connect(_on_standalone_prep)
            prep_worker.start()
            return

        if kind != "steam" or not isinstance(obj, ShortcutEntry) or not self.paths:
            return
        entry = obj
        paths = self.paths
        pfx_dir = paths.compatdata_dir / str(entry.appid_u32) / "pfx"
        auto_snap_enabled = self._is_auto_snapshot_enabled()

        class _SteamProtonPrepWorker(QThread):
            ready = Signal(bool, list, str)

            def run(self) -> None:
                if is_win32_prefix(pfx_dir):
                    self.ready.emit(True, [], "")
                    return
                protons = list_installed_protons(paths.steam_root)
                current_id = get_shortcut_proton(paths.config_vdf, entry.appid_u32) if protons else ""
                self.ready.emit(False, protons, current_id)

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
                self._set_steam_busy(True)

                def _do_set_proton() -> None:
                    self._auto_snapshot_if_enabled(
                        pfx_dir,
                        entry.app_name,
                        "proton-change",
                        "proton",
                        enabled=auto_snap_enabled,
                    )
                    set_shortcut_proton(paths.config_vdf, entry.appid_u32, tool_id)

                worker = SteamOpWorker(_do_set_proton, need_steam_closed=True)
                self._track_worker(worker)
                worker.status.connect(self.status_label.setText)

                def _ok(_res: object) -> None:
                    self._set_steam_busy(False)
                    msg = tr("proton.updated", name=entry.app_name, tool=tool_id)
                    self.status_label.setText(msg)
                    if not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

                def _err(err: str) -> None:
                    self._set_steam_busy(False)
                    if not self._closing_pending:
                        QMessageBox.critical(_dialog_parent(self), __app_name__, tr("proton.error", err=err))

                worker.finished_ok.connect(_ok)
                worker.failed.connect(_err)
                worker.start()

            self._confirm_close_steam_async(_proceed_set_proton)

        steam_prep = _SteamProtonPrepWorker()
        self._track_worker(steam_prep)
        steam_prep.ready.connect(_on_steam_prep)
        steam_prep.start()

    def _fallback_selected_proton10(self) -> None:
        sel = self._selected_library_item()
        if sel is None:
            return
        kind, obj = sel
        if _is_native_linux_game(kind, obj):
            return

        if kind == "steam":
            if not isinstance(obj, ShortcutEntry) or not self.paths or self._vdf_corrupt:
                return
            if self._warn_if_win32_prefix(self.paths.compatdata_dir / str(obj.appid_u32) / "pfx"):
                return
            paths = self.paths
            entry = obj
            auto_snap_enabled = self._is_auto_snapshot_enabled()

            def _proceed_fallback_steam() -> None:
                if self._closing_pending:
                    return
                self._set_steam_busy(True)

                def _apply_steam(tool_id: str) -> None:
                    self._auto_snapshot_if_enabled(
                        paths.compatdata_dir / str(entry.appid_u32) / "pfx",
                        entry.app_name,
                        "proton10-fallback",
                        "proton",
                        enabled=auto_snap_enabled,
                    )
                    set_shortcut_proton(paths.config_vdf, entry.appid_u32, tool_id)

                worker = FallbackProton10Worker(_apply_steam, need_steam_closed=True)
                self._track_worker(worker)
                worker.status.connect(self.status_label.setText)

                def _ok_steam(tool_id: str) -> None:
                    self._set_steam_busy(False)
                    msg = tr("proton10.updated", name=entry.app_name, tool=tool_id)
                    self.status_label.setText(msg)
                    if not self._closing_pending:
                        QMessageBox.information(_dialog_parent(self), __app_name__, msg)

                def _err_steam(err: str, is_offline: bool) -> None:
                    self._set_steam_busy(False)
                    if not self._closing_pending:
                        msg = tr("proton10.offline_error", err=err) if is_offline else tr("proton10.error", err=err)
                        QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

                worker.finished_ok.connect(_ok_steam)
                worker.failed.connect(_err_steam)
                worker.start()

            self._confirm_close_steam_async(_proceed_fallback_steam)
            return

        if kind == "standalone" and isinstance(obj, StandaloneGame):
            if self._warn_if_win32_prefix(obj.prefix_dir):
                return
            auto_snap_enabled = self._is_auto_snapshot_enabled()

            def _apply_standalone(tool_id: str) -> None:
                self._auto_snapshot_if_enabled(
                    obj.prefix_dir,
                    obj.name,
                    "proton10-fallback",
                    "proton",
                    enabled=auto_snap_enabled,
                )
                update_standalone_launch_options(
                    obj.game_dir,
                    winedlloverrides=obj.winedlloverrides,
                    launch_options=obj.launch_options,
                    proton_name=tool_id,
                )

            worker = FallbackProton10Worker(_apply_standalone, need_steam_closed=False)
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok_standalone(tool_id: str) -> None:
                self._refresh_shortcuts()
                msg = tr("proton10.updated_standalone", name=obj.name, tool=tool_id)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err_standalone(err: str, is_offline: bool) -> None:
                if not self._closing_pending:
                    msg = tr("proton10.offline_error", err=err) if is_offline else tr("proton10.error", err=err)
                    QMessageBox.critical(_dialog_parent(self), __app_name__, msg)

            worker.finished_ok.connect(_ok_standalone)
            worker.failed.connect(_err_standalone)
            worker.start()

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
    ) -> tuple[str, str] | None:
        return open_launch_presets_dialog(
            self,
            title=title,
            kind=kind,
            initial_opts=initial_opts,
            initial_winedll=initial_winedll,
            game_obj=game_obj,
            is_native_linux=is_native_linux,
        )

    def _edit_launch_options_presets(self) -> None:
        edit_launch_options_presets(self)

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
        self._set_steam_busy(True)
        self.status_label.setText(tr("backup.running"))

        class _BackupWorker(QThread):
            done = Signal(str, int, int)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths | None) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    zpath, games, files = backup_all_saves(self._paths)
                    self.done.emit(str(zpath), games, files)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _ok(zpath: str, games: int, files: int) -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            if files == 0:
                Path(zpath).unlink(missing_ok=True)
                msg = tr("backup.empty")
            else:
                msg = tr("backup.done", files=files, games=games, path=zpath)
            self.status_label.setText(msg.splitlines()[0])
            QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("backup.error", err=err))

        w = _BackupWorker(self.paths)
        self._track_worker(w)
        w.done.connect(_ok)
        w.failed.connect(_err)
        w.start()

    def _restore_saves_backup(self) -> None:
        if not self.paths:
            return
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
        if not chosen or not self.paths:
            return
        zpath = Path(chosen)
        if (
            QMessageBox.question(
                _dialog_parent(self), __app_name__, tr("restore.confirm", name=zpath.name)
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("library.restore_running"))

        class _RestoreWorker(QThread):
            done = Signal(int, int)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, zip_file: Path) -> None:
                super().__init__()
                self._paths = paths
                self._zip_file = zip_file

            def run(self) -> None:
                try:
                    if _is_wine_game_running():
                        raise RuntimeError(tr("cleanup.wine_running"))
                    games, files = restore_saves_backup(self._paths, self._zip_file)
                    self.done.emit(games, files)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        def _on_restore_done(games: int, files: int) -> None:
            self._set_steam_busy(False)
            msg = tr("restore.done", files=files, games=games)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _on_restore_err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("restore.error", err=err))

        worker = _RestoreWorker(self.paths, zpath)
        self._track_worker(worker)
        worker.done.connect(_on_restore_done)
        worker.failed.connect(_on_restore_err)
        worker.start()

    def _sync_prefix_links(self) -> None:
        if not self.paths:
            return
        paths = self.paths
        self._set_steam_busy(True)
        worker = SteamOpWorker(lambda: sync_prefix_symlinks(paths))
        self._track_worker(worker)

        def _ok(res: object) -> None:
            self._set_steam_busy(False)
            created, removed = res if isinstance(res, tuple) and len(res) == 2 else (0, 0)
            msg = tr("links.done", created=created, removed=removed)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, tr("links.error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _export_selected_to_standalone(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        ans = QMessageBox.question(
            _dialog_parent(self),
            __app_name__,
            tr("library.export_confirm", name=entry.app_name),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        paths = self.paths
        self._set_steam_busy(True)
        self.status_label.setText(tr("add.preparing_runtime"))
        worker = SteamOpWorker(
            lambda: (
                ensure_umu_run(),
                ensure_ge_proton11(),
                export_steam_to_standalone(paths, entry, remove_from_steam=False),
            )[-1],
            need_steam_closed=False,
        )
        self._track_worker(worker)
        worker.status.connect(self.status_label.setText)

        def _ok(res: object) -> None:
            self._set_steam_busy(False)
            self._refresh_shortcuts()
            game_path = str(res.game_dir) if isinstance(res, StandaloneGame) else ""
            msg = tr("library.export_done", name=entry.app_name, path=game_path)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(_dialog_parent(self), __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
            self._set_steam_busy(True)
            worker = SteamOpWorker(
                lambda: import_standalone_to_steam(paths, game, remove_standalone_files=False),
                need_steam_closed=True,
            )
            self._track_worker(worker)
            worker.status.connect(self.status_label.setText)

            def _ok(res: object) -> None:
                self._set_steam_busy(False)
                self._refresh_shortcuts()
                msg = tr("library.import_done", name=game.name, appid=res)
                self.status_label.setText(msg)
                if not self._closing_pending:
                    QMessageBox.information(_dialog_parent(self), __app_name__, msg)

            def _err(err: str) -> None:
                self._set_steam_busy(False)
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
        self.btn_remove_existing.setEnabled(has_match)

    def _ask_standalone_remove_mode(
        self, game_name: str, also_steam: bool = False
    ) -> tuple[bool, bool] | None:
        """Prompt user for 3-tier standalone removal: (delete_files, delete_prefix) or None."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(__app_name__)
        msg = tr("remove.choose_mode", name=game_name)
        if also_steam:
            msg += "\n\n" + tr("remove.also_steam_note")
        box.setText(msg)
        btn_shortcut = box.addButton(
            tr("remove.mode_shortcut_only"), QMessageBox.ButtonRole.AcceptRole
        )
        btn_prefix = box.addButton(
            tr("remove.mode_shortcut_prefix"), QMessageBox.ButtonRole.AcceptRole
        )
        btn_all = box.addButton(
            tr("remove.mode_all"), QMessageBox.ButtonRole.DestructiveRole
        )
        btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_shortcut:
            return (False, False)
        if clicked == btn_prefix:
            return (False, True)
        if clicked == btn_all:
            if also_steam:
                confirm = QMessageBox.question(
                    _dialog_parent(self),
                    __app_name__,
                    tr("remove.confirm_all_with_steam", name=game_name),
                )
                if confirm != QMessageBox.StandardButton.Yes:
                    return None
            return (True, True)
        return None

    def _remove_steam_shortcut_async(
        self, entry: ShortcutEntry, refresh_games: bool = False
    ) -> None:
        """Remove a Steam Non-Steam shortcut in a background thread."""
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("library.removing", name=entry.app_name))

        class _RemoveWorker(QThread):
            done = Signal(bool)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry) -> None:
                super().__init__()
                self._paths = paths
                self._shortcut = shortcut

            def run(self) -> None:
                was_running = is_steam_running()
                try:
                    if was_running and not shutdown_steam():
                        raise RuntimeError(tr("add.close_steam_timeout"))
                    removed = remove_shortcut(self._paths, self._shortcut.appid_u32)
                    self.done.emit(removed)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))
                finally:
                    if was_running and not is_steam_running():
                        start_steam()

        worker = _RemoveWorker(self.paths, entry)
        self._track_worker(worker)

        def _on_removed(removed: bool) -> None:
            self._set_steam_busy(False)
            if refresh_games:
                self._refresh_games()
            if removed or refresh_games:
                self._refresh_shortcuts()
                self.status_label.setText(tr("library.removed", name=entry.app_name))

        def _on_remove_err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.done.connect(_on_removed)
        worker.failed.connect(_on_remove_err)
        worker.start()

    def _remove_standalone_async(
        self,
        sa: StandaloneGame,
        *,
        delete_files: bool,
        delete_prefix: bool,
        steam_entry: ShortcutEntry | None = None,
        refresh_games: bool = False,
    ) -> None:
        self._set_steam_busy(True)

        class _StandaloneRemoveWorker(QThread):
            done = Signal()
            failed = Signal(str)

            def __init__(
                self,
                game: StandaloneGame,
                del_files: bool,
                del_prefix: bool,
                snap_fn: Callable[[Path, str, str, str], Path | None],
            ) -> None:
                super().__init__()
                self._game = game
                self._del_files = del_files
                self._del_prefix = del_prefix
                self._snap_fn = snap_fn

            def run(self) -> None:
                try:
                    if self._del_files or self._del_prefix:
                        self._snap_fn(
                            self._game.prefix_dir, self._game.name, "remove-game", "remove"
                        )
                    remove_standalone_game(
                        self._game,
                        delete_files=self._del_files,
                        delete_prefix=self._del_prefix,
                    )
                    self.done.emit()
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        worker = _StandaloneRemoveWorker(
            sa, delete_files, delete_prefix, self._make_pre_snapshot_fn()
        )
        self._track_worker(worker)

        def _on_done() -> None:
            self._set_steam_busy(False)
            if self._closing_pending:
                return
            if steam_entry is not None and self.paths is not None:
                self._remove_steam_shortcut_async(steam_entry, refresh_games=True)
                return
            if refresh_games or delete_files:
                self._refresh_games()
            self._refresh_shortcuts()
            self.status_label.setText(tr("library.removed", name=sa.name))

        def _on_err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(_dialog_parent(self), __app_name__, err)

        worker.done.connect(_on_done)
        worker.failed.connect(_on_err)
        worker.start()

    def _remove_existing_from_add(self) -> None:
        """Remove an existing Standalone game and/or Steam shortcut from the Add tab."""
        sa, steam_entry = self._find_matching_installed()
        if sa is None and steam_entry is None:
            return
        if sa is not None:
            choice = self._ask_standalone_remove_mode(
                sa.name, also_steam=steam_entry is not None
            )
            if choice is None:
                return
            delete_files, delete_prefix = choice
            self._remove_standalone_async(
                sa,
                delete_files=delete_files,
                delete_prefix=delete_prefix,
                steam_entry=steam_entry,
                refresh_games=True,
            )
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
            choice = self._ask_standalone_remove_mode(obj.name, also_steam=False)
            if choice is None:
                return
            delete_files, delete_prefix = choice
            self._remove_standalone_async(
                obj,
                delete_files=delete_files,
                delete_prefix=delete_prefix,
                refresh_games=delete_files,
            )
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

    def _migrate_selected_shortcut(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        old_exe = Path(entry.exe.strip().strip('"'))
        try:
            old_root = infer_game_root_from_exe(old_exe)
        except Exception as exc:
            QMessageBox.critical(_dialog_parent(self), __app_name__, str(exc))
            return
        suggested = clean_game_name(old_root.name)
        new_name, ok = QInputDialog.getText(
            _dialog_parent(self),
            __app_name__,
            tr("migrate.prompt_name"),
            text=suggested,
        )
        if not ok or not new_name.strip():
            return
        paths = self.paths
        item = MigrationPlanItem(
            entry=entry,
            old_root=old_root,
            new_name=new_name.strip(),
            new_root=Path.home() / "Games" / new_name.strip(),
            has_compatdata=(paths.compatdata_dir / str(entry.appid_u32)).is_dir(),
        )

        def _proceed_migrate() -> None:
            if self._closing_pending:
                return
            self._set_steam_busy(True)
            self._bulk_worker = BulkMigrateWorker(
                paths,
                [item],
                pre_snapshot_fn=self._make_pre_snapshot_fn(),
            )
            self._track_worker(self._bulk_worker)
            self._bulk_worker.status.connect(self.status_label.setText)
            self._bulk_worker.finished_ok.connect(self._on_bulk_done)
            self._bulk_worker.failed.connect(self._on_bulk_failed)
            self._bulk_worker.start()

        if is_gamescope_session():
            def _on_steam_checked(running: bool) -> None:
                if running:
                    ans = QMessageBox.question(
                        _dialog_parent(self),
                        __app_name__,
                        tr("migrate.gamescope_one"),
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                _proceed_migrate()

            self._check_steam_running_async(_on_steam_checked)
            return
        _proceed_migrate()

    def _bulk_migrate_shortcuts(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("migrate.analyzing"))

        class _BulkPlanWorker(QThread):
            ready = Signal(list, bool)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    entries = load_shortcuts(self._paths.shortcuts_vdf)
                    plan = plan_bulk_migration(
                        entries=entries,
                        games_root=Path.home() / "Games",
                        compatdata_dir=self._paths.compatdata_dir,
                        config_vdf=self._paths.config_vdf,
                    )
                    steam_running = is_gamescope_session() and is_steam_running()
                    self.ready.emit(plan, steam_running)
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc))

        worker = _BulkPlanWorker(self.paths)
        self._track_worker(worker)
        worker.ready.connect(self._confirm_and_run_bulk_plan)
        worker.failed.connect(self._on_bulk_failed)
        worker.start()

    def _confirm_and_run_bulk_plan(
        self, plan: list[MigrationPlanItem], steam_running_in_gamescope: bool = False
    ) -> None:
        if self._closing_pending or not self.paths:
            self._set_steam_busy(False)
            return
        if not plan:
            self.status_label.setText(tr("migrate.sync_only"))

            class _SaveSyncWorker(QThread):
                done = Signal(int, int)
                failed = Signal(str)

                def __init__(self, paths: SteamPaths) -> None:
                    super().__init__()
                    self._paths = paths

                def run(self) -> None:
                    try:
                        if _is_wine_game_running():
                            raise RuntimeError(tr("migrate.wine_running"))
                        checked, copied = sync_all_external_saves(self._paths)
                        self.done.emit(checked, copied)
                    except Exception as exc:
                        self.failed.emit(_format_worker_error(exc))

            def _on_sync_done(checked: int, copied: int) -> None:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                if not self._closing_pending:
                    QMessageBox.information(
                        _dialog_parent(self),
                        __app_name__,
                        tr("migrate.all_standardized", checked=checked, copied=copied),
                    )

            sync_worker = _SaveSyncWorker(self.paths)
            self._track_worker(sync_worker)
            sync_worker.done.connect(_on_sync_done)
            sync_worker.failed.connect(self._on_bulk_failed)
            sync_worker.start()
            return

        lines = [
            f"• {p.entry.app_name} → {p.new_name}"
            for p in plan
        ]
        msg = tr("migrate.confirm_bulk", lines="\n".join(lines))
        if steam_running_in_gamescope:
            msg += tr("migrate.gamescope_warn")
        if QMessageBox.question(_dialog_parent(self), __app_name__, msg) != QMessageBox.StandardButton.Yes:
            self._set_steam_busy(False)
            return

        self._bulk_worker = BulkMigrateWorker(
            self.paths,
            plan,
            pre_snapshot_fn=self._make_pre_snapshot_fn(),
        )
        self._track_worker(self._bulk_worker)
        self._bulk_worker.status.connect(self.status_label.setText)
        self._bulk_worker.finished_ok.connect(self._on_bulk_done)
        self._bulk_worker.failed.connect(self._on_bulk_failed)
        self._bulk_worker.start()

    def _on_bulk_done(self, summary: str) -> None:
        self._set_steam_busy(False)
        self.status_label.setText(tr("migrate.done"))
        self._refresh_games()
        self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.information(_dialog_parent(self), __app_name__, summary)

    def _on_bulk_failed(self, err: str) -> None:
        self._set_steam_busy(False)
        if not self._closing_pending:
            QMessageBox.critical(_dialog_parent(self), __app_name__, tr("migrate.error", err=err))


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
    return app.exec()
