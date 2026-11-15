"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import logging
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QThread, Signal, qInstallMessageHandler
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .artwork import (
    SteamMatch,
    download_steam_artwork,
    fetch_header_preview,
    restore_steam_artwork_backup,
    search_steam_games,
)
from .i18n import available_languages, get_language, set_language, tr
from .extractor import (
    extract_archive,
    is_archive,
    run_windows_installer,
    sanitize_folder_name,
    suggest_target_name,
)
from .scanner import DetectedGame, clean_game_name, scan_all_games, scan_game_folder
from .steam_paths import (
    SteamPaths,
    is_gamescope_session,
    is_steam_running,
    list_installed_protons,
    resolve_steam_paths,
)
from .steam_vdf import (
    CleanupCandidate,
    CompatMappingEntry,
    MigrationPlanItem,
    ShortcutEntry,
    add_or_update_shortcut,
    adopt_installer_prefix,
    apply_launch_presets,
    backup_all_saves,
    bulk_migrate_compat_tool,
    delete_cleanup_candidates_checked,
    find_cleanup_candidates,
    get_shortcut_proton,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    list_compat_tool_usage,
    load_shortcuts,
    migrate_shortcut_to_games,
    plan_bulk_migration,
    remove_compat_tools,
    remove_shortcut,
    restore_saves_backup,
    set_shortcut_proton,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
    sync_prefix_symlinks,
    update_shortcut_launch_options,
)
from .theme import ThemeMode, apply_theme


def _resolve_asset_path(filename: str) -> Path | None:
    """Locate an asset file across package resources, checkout layout, and AppImage bundles."""
    candidates = [
        Path(__file__).resolve().parent / "assets" / filename,
        Path(__file__).resolve().parent.parent / "assets" / filename,
        Path(__file__).resolve().parent.parent.parent / "assets" / filename,
        Path(sys.executable).resolve().parent / "assets" / filename,
        Path(sys.prefix) / "share" / "deckinstall" / "assets" / filename,
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / filename)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _pick_file_async(
    parent: QWidget,
    title: str,
    start_dir: Path,
    qt_filter: str,
    on_selected: Callable[[str], None],
) -> None:
    """Open a non-blocking QFileDialog file picker (in-window under Gamescope)."""
    dlg = QFileDialog(parent, title, str(start_dir), qt_filter)
    dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, is_gamescope_session())
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.fileSelected.connect(on_selected)
    dlg.finished.connect(dlg.deleteLater)
    dlg.open()


def _pick_dir_async(
    parent: QWidget,
    title: str,
    start_dir: Path,
    on_selected: Callable[[str], None],
) -> None:
    """Open a non-blocking QFileDialog folder picker (in-window under Gamescope)."""
    dlg = QFileDialog(parent, title, str(start_dir))
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, is_gamescope_session())
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.fileSelected.connect(on_selected)
    dlg.finished.connect(dlg.deleteLater)
    dlg.open()


class DropZoneFrame(QFrame):
    """Drag-and-drop card for archive or setup.exe selection."""

    file_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.file_dropped.emit(local)


class ExtractWorker(QThread):
    """Background worker for archive extraction or Proton setup.exe execution."""

    progress = Signal(int, str)
    finished_ok = Signal(str)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, source: Path, dest_root: Path, folder_name: str) -> None:
        super().__init__()
        self.source = source
        self.dest_root = dest_root
        self.folder_name = folder_name
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            if is_archive(self.source):
                out = extract_archive(
                    self.source,
                    self.dest_root,
                    self.folder_name,
                    on_progress=lambda p, msg: self.progress.emit(p, msg),
                    cancel_flag=lambda: self._cancelled or self.isInterruptionRequested(),
                )
            elif self.source.suffix.lower() == ".exe":
                self.progress.emit(20, tr("install.running_installer", name=self.source.name))
                out = run_windows_installer(
                    self.source,
                    self.dest_root / sanitize_folder_name(self.folder_name),
                    on_log=lambda msg: self.progress.emit(50, msg),
                    cancel_flag=lambda: self._cancelled or self.isInterruptionRequested(),
                )
                self.progress.emit(100, tr("install.installer_done", out=out))
            else:
                raise RuntimeError(tr("install.unsupported_format", name=self.source.name))
            if self._cancelled or self.isInterruptionRequested():
                self.cancelled.emit()
                return
            self.finished_ok.emit(str(out))
        except Exception as exc:
            if self._cancelled or self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))


class SteamSearchWorker(QThread):
    """Background worker for Steam Store search."""

    results = Signal(int, list)
    failed = Signal(int, str)

    def __init__(self, req_id: int, query: str) -> None:
        super().__init__()
        self.req_id = req_id
        self.query = query

    def run(self) -> None:
        try:
            self.results.emit(self.req_id, search_steam_games(self.query))
        except Exception as exc:
            self.failed.emit(self.req_id, str(exc))


class PreviewWorker(QThread):
    """Background worker to fetch a Steam header preview image."""

    loaded = Signal(int, bytes)
    failed = Signal(int)

    def __init__(self, req_id: int, steam_appid: int) -> None:
        super().__init__()
        self.req_id = req_id
        self.steam_appid = steam_appid

    def run(self) -> None:
        if isinstance(self.steam_appid, bool) or not isinstance(self.steam_appid, int) or self.steam_appid <= 0:
            self.failed.emit(self.req_id)
            return
        try:
            data = fetch_header_preview(self.steam_appid)
        except Exception:
            data = None
        if data:
            self.loaded.emit(self.req_id, data)
        else:
            self.failed.emit(self.req_id)


class SteamOpWorker(QThread):
    """Background worker for Steam shutdown + VDF, prefix, or artwork operations."""

    status = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fn: Callable[[], object],
        need_steam_closed: bool = False,
        restart_if_was_running: bool = False,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.need_steam_closed = need_steam_closed
        self.restart_if_was_running = restart_if_was_running

    def run(self) -> None:
        was_running = False
        try:
            if self.need_steam_closed:
                was_running = is_steam_running()
                if was_running:
                    self.status.emit(tr("add.closing_steam"))
                    if not shutdown_steam():
                        raise RuntimeError(tr("add.close_steam_timeout"))
            result = self.fn()
            if was_running and self.restart_if_was_running:
                self.status.emit(tr("add.restarting_steam"))
                start_steam()
            self.finished_ok.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ScanWorker(QThread):
    """Background worker to scan game folders without blocking the UI thread."""

    finished_scan = Signal(list)

    def run(self) -> None:
        try:
            self.finished_scan.emit(scan_all_games())
        except Exception:
            logging.exception("ScanWorker failed")
            self.finished_scan.emit([])


class AddToSteamWorker(QThread):
    """Background worker to safely shut down Steam, write VDFs, fetch art, and relaunch."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str, bool)

    def __init__(
        self,
        paths: SteamPaths,
        app_name: str,
        exe_path: Path,
        steam_appid: int | None,
        enable_proton: bool,
        restart_steam: bool,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.app_name = app_name
        self.exe_path = exe_path
        self.steam_appid = steam_appid
        self.enable_proton = enable_proton
        self.restart_steam = restart_steam

    def run(self) -> None:
        was_running = False
        shortcut_written = False
        try:
            was_running = is_steam_running()
            if was_running:
                self.status.emit(tr("add.closing_steam"))
                if not shutdown_steam():
                    raise RuntimeError(tr("add.close_steam_timeout"))

            self.status.emit(tr("add.writing_shortcut"))
            shortcut_id = add_or_update_shortcut(
                self.paths,
                app_name=self.app_name,
                exe_path=self.exe_path,
                start_dir=self.exe_path.parent,
                enable_proton=self.enable_proton,
            )
            shortcut_written = True

            game_root = infer_game_root_from_exe(self.exe_path) or self.exe_path.parent
            adopt_installer_prefix(self.paths, game_root, shortcut_id)

            imported = import_external_prefix_saves(
                paths=self.paths,
                shortcut_appid_u32=shortcut_id,
                game_name=self.app_name,
                steam_appid=self.steam_appid,
            )
            if imported:
                self.status.emit(tr("add.saves_imported", count=imported))
            art_msg = tr("add.no_art")
            if self.steam_appid:
                self.status.emit(tr("add.downloading_art", appid=self.steam_appid))
                art = download_steam_artwork(
                    steam_appid=self.steam_appid,
                    shortcut_appid_u32=shortcut_id,
                    grid_dir=self.paths.grid_dir,
                )
                art_msg = tr(
                    "add.art_saved",
                    files=", ".join(art.saved_files.keys()) or tr("art.none"),
                )

            self.finished_ok.emit(
                tr("add.done", name=self.app_name, appid=shortcut_id, art=art_msg)
            )
        except Exception as exc:
            self.failed.emit(str(exc), shortcut_written)
        finally:
            if was_running and self.restart_steam:
                try:
                    self.status.emit(tr("add.restarting_steam"))
                    start_steam()
                except Exception:
                    logging.exception("Failed restarting Steam after AddToSteamWorker")


class BulkMigrateWorker(QThread):
    """Background worker to migrate multiple Non-Steam games to ~/Games safely."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths, items: list[MigrationPlanItem]) -> None:
        super().__init__()
        self.paths = paths
        self.items = items

    def run(self) -> None:
        was_running = is_steam_running()
        migrated: list[str] = []
        errors: list[str] = []
        try:
            if was_running:
                self.status.emit(tr("add.closing_steam"))
                if not shutdown_steam():
                    raise RuntimeError(tr("add.close_steam_timeout"))

            games_root = Path.home() / "Games"
            for idx, item in enumerate(self.items, start=1):
                self.status.emit(
                    tr(
                        "migrate.progress",
                        idx=idx,
                        total=len(self.items),
                        old=item.entry.app_name,
                        new=item.new_name,
                    )
                )
                try:
                    new_appid, _, steam_appid = migrate_shortcut_to_games(
                        paths=self.paths,
                        entry=item.entry,
                        games_root=games_root,
                        new_name=item.new_name,
                    )
                    if steam_appid is None:
                        matches = search_steam_games(item.new_name, limit=1)
                        if matches:
                            steam_appid = matches[0].appid
                    if steam_appid:
                        download_steam_artwork(steam_appid, new_appid, self.paths.grid_dir)
                    migrated.append(item.new_name)
                except Exception as exc:
                    errors.append(f"{item.entry.app_name}: {exc}")

            self.status.emit(tr("migrate.sync_all"))
            checked, copied = 0, 0
            try:
                checked, copied = sync_all_external_saves(self.paths)
            except Exception as exc:
                errors.append(f"sync_all_external_saves: {exc}")

            summary = tr(
                "migrate.summary",
                count=len(migrated),
                names=", ".join(migrated) or tr("art.none"),
                checked=checked,
                copied=copied,
            )
            if errors:
                summary += tr("migrate.errors_header", errs="\n".join(errors))
            self.finished_ok.emit(summary)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if was_running and not is_steam_running():
                self.status.emit(tr("add.restarting_steam"))
                start_steam()


def _is_wine_game_running() -> bool:
    """Return True if a Windows .exe process owned by the current user appears to be running."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    uid = os.getuid()
    for p in proc.iterdir():
        if not (p.name.isascii() and p.name.isdigit()):
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            raw_cmd = (p / "cmdline").read_bytes()
            if not raw_cmd:
                continue
            args = [a.decode("utf-8", errors="ignore").lower() for a in raw_cmd.split(b"\x00") if a]
            if not args or any("wineserver" in a for a in args):
                continue
            if any(a.endswith(".exe") for a in args):
                return True
        except OSError:
            continue
    return False


class FlowLayout(QLayout):
    """Responsive wrapping layout so action buttons never clip horizontally."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list = []

    def __del__(self) -> None:
        try:
            while self._items:
                self.takeAt(0)
        except RuntimeError:
            self._items.clear()

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisibleTo(self.parentWidget()):
                continue
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisibleTo(self.parentWidget()):
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing - 1 > eff.right() and line_height > 0:
                x = eff.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


def _make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return card, layout


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings(__app_name__, __app_name__)
        set_language(str(self.settings.value("language", "auto")))
        self.paths = resolve_steam_paths()
        self._detected_games: list[DetectedGame] = []
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._vdf_corrupt = False
        self._search_req_id = 0
        self._preview_req_id = 0
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
        self._refresh_shortcuts()

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
                self._closing_pending = False
                self.close()

        worker.finished.connect(_cleanup)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._extract_worker and self._extract_worker.isRunning():
            self._extract_worker.cancel()
        if any(w.isRunning() for w in self._active_workers):
            self._closing_pending = True
            self.status_label.setText(tr("status.closing_wait"))
            event.ignore()
            return
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("ContentArea")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        shell.addWidget(self._build_sidebar())

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
        self.stack.addWidget(_wrap_scroll(self._build_add_page()))
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
        self.status_label.setObjectName("MutedText")
        right_layout.addWidget(self.status_label)

        shell.addWidget(right, 1)
        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(225)
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
        title.setStyleSheet("font-size: 16px;")
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
            tr("tabs.add"),
            tr("tabs.library"),
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

        steam_lbl = QLabel(
            tr("status.steam_id", id=self.paths.user_id)
            if self.paths
            else tr("status.steam_not_detected")
        )
        steam_lbl.setObjectName("MutedText")
        layout.addWidget(steam_lbl)

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
        return sidebar

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
        self.btn_use_extracted = QPushButton(tr("install.btn_add_steam"))
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

        list_card, list_layout = _make_card()
        scan_row = QHBoxLayout()
        lbl_det = QLabel(tr("install.detected_title"))
        lbl_det.setObjectName("SectionTitle")
        self.btn_scan = QPushButton(tr("install.btn_rescan"))
        self.btn_scan.clicked.connect(self._refresh_games)
        btn_browse_exe = QPushButton(tr("install.browse"))
        btn_browse_exe.clicked.connect(self._pick_manual_exe)
        scan_row.addWidget(lbl_det)
        scan_row.addStretch()
        scan_row.addWidget(self.btn_scan)
        scan_row.addWidget(btn_browse_exe)
        list_layout.addLayout(scan_row)

        self.games_list = QListWidget()
        self.games_list.setMaximumHeight(145)
        self.games_list.currentRowChanged.connect(self._on_game_selected)
        list_layout.addWidget(self.games_list)
        layout.addWidget(list_card)

        cfg_card, cfg_layout = _make_card()
        prev_lbl = QLabel(tr("install.preview_title"))
        prev_lbl.setObjectName("SectionTitle")
        cfg_layout.addWidget(prev_lbl)
        cfg_split = QHBoxLayout()
        cfg_split.setSpacing(16)

        form = QFormLayout()
        form.setSpacing(10)
        self.add_name = QLineEdit()
        self.add_name.setPlaceholderText(tr("install.game_name_placeholder"))
        form.addRow(tr("install.folder_label"), self.add_name)

        self.add_exe_combo = QComboBox()
        self.add_exe_combo.setEditable(True)
        self.add_exe_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.add_exe_combo.setMinimumContentsLength(24)
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

        self.chk_proton = QCheckBox(tr("install.enable_proton"))
        self.chk_proton.setChecked(True)
        form.addRow("", self.chk_proton)

        self.chk_restart = QCheckBox(tr("add.reopen_steam"))
        self.chk_restart.setChecked(True)
        form.addRow("", self.chk_restart)

        cfg_split.addLayout(form, 1)

        self.preview_label = QLabel(tr("install.art_preview_empty"))
        self.preview_label.setObjectName("PreviewBox")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setFixedSize(230, 108)
        cfg_split.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignTop)

        cfg_layout.addLayout(cfg_split)

        self.btn_add_steam = QPushButton(tr("install.btn_add_steam"))
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        cfg_layout.addWidget(self.btn_add_steam)
        layout.addWidget(cfg_card, 1)
        return page

    def _build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel(tr("library.page_title"))
        title.setObjectName("PageTitle")
        sub = QLabel(tr("library.page_subtitle"))
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        lib_card, lib_layout = _make_card()
        top = QHBoxLayout()
        lbl = QLabel(tr("library.shortcuts_title"))
        lbl.setObjectName("SectionTitle")
        self.btn_reload = QPushButton(tr("install.btn_rescan"))
        self.btn_reload.clicked.connect(self._refresh_shortcuts)
        top.addWidget(lbl)
        top.addStretch()
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

        self.shortcuts_list = QListWidget()
        self.shortcuts_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.shortcuts_list.customContextMenuRequested.connect(self._show_shortcut_context_menu)
        lib_layout.addWidget(self.shortcuts_list, 1)

        actions = FlowLayout(spacing=8)
        self.btn_bulk_migrate = QPushButton(tr("library.btn_bulk_migrate"))
        self.btn_bulk_migrate.setObjectName("primaryBtn")
        self.btn_bulk_migrate.clicked.connect(self._bulk_migrate_shortcuts)

        self._game_actions_menu = QMenu(self)
        self.act_migrate = QAction(tr("library.btn_migrate_one"), self)
        self.act_migrate.triggered.connect(self._migrate_selected_shortcut)
        self.act_art = QAction(tr("library.btn_update_art"), self)
        self.act_art.triggered.connect(self._update_selected_shortcut_art)
        self.act_revert_art = QAction(tr("library.btn_revert_art"), self)
        self.act_revert_art.triggered.connect(self._revert_selected_shortcut_art)
        self.act_proton = QAction(tr("library.btn_proton"), self)
        self.act_proton.triggered.connect(self._change_selected_proton)
        self.act_bulk_proton = QAction(tr("library.btn_bulk_proton"), self)
        self.act_bulk_proton.triggered.connect(self._bulk_migrate_proton_dialog)
        self.act_launch_opts = QAction(tr("library.btn_launch_opts"), self)
        self.act_launch_opts.triggered.connect(self._edit_launch_options_presets)
        self.act_remove = QAction(tr("library.btn_remove"), self)
        self.act_remove.triggered.connect(self._remove_selected_shortcut)
        for act in (
            self.act_migrate,
            self.act_art,
            self.act_revert_art,
            self.act_proton,
            self.act_bulk_proton,
            self.act_launch_opts,
        ):
            self._game_actions_menu.addAction(act)
        self._game_actions_menu.addSeparator()
        self._game_actions_menu.addAction(self.act_remove)

        self.btn_game_actions = QPushButton(tr("library.btn_game_actions"))
        self.btn_game_actions.setMenu(self._game_actions_menu)

        self._tools_menu = QMenu(self)
        self.act_backup_saves = QAction(tr("library.btn_backup_saves"), self)
        self.act_backup_saves.triggered.connect(self._backup_all_saves)
        self.act_restore_saves = QAction(tr("library.btn_restore_saves"), self)
        self.act_restore_saves.triggered.connect(self._restore_saves_backup)
        self.act_link_prefixes = QAction(tr("library.btn_link_prefixes"), self)
        self.act_link_prefixes.triggered.connect(self._sync_prefix_links)
        self.act_cleanup = QAction(tr("library.btn_cleanup"), self)
        self.act_cleanup.triggered.connect(self._cleanup_old_prefixes)
        for act in (self.act_backup_saves, self.act_restore_saves, self.act_link_prefixes):
            self._tools_menu.addAction(act)
        self._tools_menu.addSeparator()
        self._tools_menu.addAction(self.act_cleanup)

        self.btn_tools_menu = QPushButton(tr("library.btn_tools_menu"))
        self.btn_tools_menu.setMenu(self._tools_menu)

        for b in (self.btn_bulk_migrate, self.btn_game_actions, self.btn_tools_menu):
            actions.addWidget(b)
        lib_layout.addLayout(actions)
        layout.addWidget(lib_card, 1)
        return page

    def _show_shortcut_context_menu(self, pos: QPoint) -> None:
        item = self.shortcuts_list.itemAt(pos)
        if item is not None:
            self.shortcuts_list.setCurrentItem(item)
        if self._selected_shortcut() is None:
            return
        self._game_actions_menu.exec(self.shortcuts_list.viewport().mapToGlobal(pos))

    def _apply_saved_theme(self) -> None:
        saved = str(self.settings.value("theme", "auto"))
        idx = self.theme_combo.findData(saved)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        mode: ThemeMode = saved if saved in ("auto", "dark", "light") else "auto"
        apply_theme(self.app, mode)

    def _on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue("theme", mode)
        apply_theme(self.app, mode)

    def _on_language_changed(self) -> None:
        code = str(self.lang_combo.currentData() or "auto")
        self.settings.setValue("language", code)
        QMessageBox.information(
            self,
            __app_name__,
            tr("lang.changed", name=self.lang_combo.currentText()),
        )

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            tr("about.title", app=__app_name__),
            tr("about.body", app=__app_name__, version=__version__),
        )

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
            QMessageBox.warning(self, __app_name__, tr("install.invalid_file"))
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
            QMessageBox.critical(self, __app_name__, err)

    def _send_extracted_to_add_tab(self) -> None:
        target = getattr(self, "_last_extracted_dir", None)
        if isinstance(target, Path) and target.is_dir():
            game = scan_game_folder(target)
            if game:
                self._populate_game_form(game)
        self._nav_buttons[1].setChecked(True)
        self.stack.setCurrentIndex(1)

    def _refresh_games(self) -> None:
        self.btn_scan.setEnabled(False)
        worker = ScanWorker()
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
        self.games_list.clear()
        if not self._detected_games:
            empty_item = QListWidgetItem(tr("add.no_games_detected"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.games_list.addItem(empty_item)
            return
        for g in self._detected_games:
            tag = f" [AppID {g.steam_appid}]" if g.steam_appid else ""
            self.games_list.addItem(f"{g.name}{tag}  —  {g.primary_exe.name}")

    def _on_game_selected(self, row: int) -> None:
        if 0 <= row < len(self._detected_games):
            self._populate_game_form(self._detected_games[row])

    def _populate_game_form(self, game: DetectedGame) -> None:
        self._search_req_id += 1
        self.add_name.setText(game.name)
        self.add_exe_combo.clear()
        for exe in game.executables:
            self.add_exe_combo.addItem(str(exe))
        self.steam_search_input.setText(
            str(game.steam_appid) if game.steam_appid else game.name
        )
        self.steam_match_combo.clear()
        if game.steam_appid:
            self.steam_match_combo.addItem(
                tr("add.detected_appid", appid=game.steam_appid), game.steam_appid
            )

    def _on_manual_exe_picked(self, path: str) -> None:
        if not path:
            return
        self._search_req_id += 1
        exe = Path(path)
        game = scan_game_folder(exe.parent)
        name = game.name if game else exe.stem
        self.add_name.setText(name)
        self.add_exe_combo.clear()
        self.add_exe_combo.addItem(str(exe))
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
        QMessageBox.warning(self, __app_name__, tr("add.search_error", err=err))

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
        self._preview_worker = worker
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
            dpr = max(1.0, self.preview_label.devicePixelRatioF())
            scaled = pix.scaled(
                self.preview_label.size() * dpr,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            self.preview_label.setPixmap(scaled)
        else:
            self.preview_label.setText(tr("add.art_placeholder"))

    def _add_selected_to_steam(self) -> None:
        if not self.paths:
            QMessageBox.critical(self, __app_name__, tr("add.steam_missing"))
            return
        if self._vdf_corrupt:
            QMessageBox.critical(
                self,
                __app_name__,
                self.vdf_error_banner.text() or tr("library.vdf_error", err="shortcuts.vdf"),
            )
            return
        app_name = self.add_name.text().strip()
        exe_str = self.add_exe_combo.currentText().strip()
        if not app_name or not exe_str or not Path(exe_str).is_file():
            QMessageBox.warning(self, __app_name__, tr("add.invalid_input"))
            return

        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(
                self,
                __app_name__,
                tr("add.gamescope_confirm"),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        steam_appid = self.steam_match_combo.currentData()
        if isinstance(steam_appid, bool) or not isinstance(steam_appid, int) or not (1 <= steam_appid <= 0xFFFFFFFF):
            steam_appid = None
        if steam_appid is None:
            raw_q = self.steam_search_input.text().strip()
            if raw_q.isascii() and raw_q.isdigit() and len(raw_q) <= 10:
                candidate = int(raw_q)
                if 1 <= candidate <= 0xFFFFFFFF:
                    steam_appid = candidate

        self._set_steam_busy(True)
        self._add_worker = AddToSteamWorker(
            paths=self.paths,
            app_name=app_name,
            exe_path=Path(exe_str),
            steam_appid=steam_appid,
            enable_proton=self.chk_proton.isChecked(),
            restart_steam=self.chk_restart.isChecked(),
        )
        self._track_worker(self._add_worker)
        self._add_worker.status.connect(self.status_label.setText)
        self._add_worker.finished_ok.connect(self._on_add_done)
        self._add_worker.failed.connect(self._on_add_failed)
        self._add_worker.start()

    def _on_add_done(self, msg: str) -> None:
        self._set_steam_busy(False)
        self.status_label.setText(msg)
        self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.information(self, __app_name__, msg)

    def _on_add_failed(self, err: str, partial_written: bool = False) -> None:
        self._set_steam_busy(False)
        if partial_written:
            self._refresh_shortcuts()
        if not self._closing_pending:
            QMessageBox.critical(self, __app_name__, err)

    def _refresh_shortcuts(self) -> None:
        self.shortcuts_list.clear()
        if not self.paths:
            self._vdf_corrupt = False
            self.vdf_error_banner.hide()
            self._set_vdf_write_enabled(True)
            empty_item = QListWidgetItem(tr("library.no_shortcuts"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.shortcuts_list.addItem(empty_item)
            return
        try:
            entries = load_shortcuts(self.paths.shortcuts_vdf)
        except Exception as exc:
            self._vdf_corrupt = True
            self.vdf_error_banner.setText(tr("library.vdf_error", err=exc))
            self.vdf_error_banner.show()
            self._set_vdf_write_enabled(False)
            err_item = QListWidgetItem(tr("library.no_shortcuts"))
            err_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.shortcuts_list.addItem(err_item)
            return
        self._vdf_corrupt = False
        self.vdf_error_banner.hide()
        self._set_vdf_write_enabled(True)
        if not entries:
            empty_item = QListWidgetItem(tr("library.no_shortcuts"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.shortcuts_list.addItem(empty_item)
            return
        for entry in entries:
            item = QListWidgetItem(f"{entry.app_name}  ({entry.appid_u32})  —  {entry.exe}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.shortcuts_list.addItem(item)

    def _selected_shortcut(self) -> ShortcutEntry | None:
        item = self.shortcuts_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, ShortcutEntry) else None

    def _set_vdf_write_enabled(self, enabled: bool) -> None:
        for w in (
            self.btn_add_steam,
            self.btn_bulk_migrate,
            self.act_migrate,
            self.act_proton,
            self.act_bulk_proton,
            self.act_launch_opts,
            self.act_backup_saves,
            self.act_restore_saves,
            self.act_remove,
        ):
            w.setEnabled(enabled)

    def _set_steam_busy(self, busy: bool) -> None:
        enabled = not busy
        for w in (
            self.btn_reload,
            self.shortcuts_list,
            self.btn_game_actions,
            self.btn_tools_menu,
            self.act_art,
            self.act_revert_art,
            self.act_link_prefixes,
            self.act_cleanup,
        ):
            w.setEnabled(enabled)
        self._set_vdf_write_enabled(enabled and not self._vdf_corrupt)

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
                self,
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
                    self.failed.emit(str(exc))

        def _on_search_found(matches: list[SteamMatch]) -> None:
            if self._closing_pending:
                self._set_steam_busy(False)
                return
            if not matches:
                self._set_steam_busy(False)
                msg = tr("art.not_found", name=entry.app_name)
                self.status_label.setText(msg)
                QMessageBox.information(self, __app_name__, msg)
                return
            labels = [tr("art.match_item", name=m.name, appid=m.appid) for m in matches]
            choice, ok = QInputDialog.getItem(
                self,
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
                        self.failed.emit(str(exc))

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
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, tr("art.error", err=err))

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
                self, __app_name__, tr("art.revert_none", name=entry.app_name)
            )
            return
        if (
            QMessageBox.question(
                self,
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
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, tr("art.revert_error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _confirm_close_steam_if_running(self) -> bool:
        """Ask the user to close Steam if it is currently running."""
        if not is_steam_running():
            return True
        return (
            QMessageBox.question(self, __app_name__, tr("proton.steam_running_confirm"))
            == QMessageBox.StandardButton.Yes
        )

    def _change_selected_proton(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        protons = list_installed_protons(self.paths.steam_root)
        if not protons:
            QMessageBox.warning(self, __app_name__, tr("proton.none_found"))
            return
        current = get_shortcut_proton(self.paths.config_vdf, entry.appid_u32) or "default"
        labels = [disp for _, disp in protons]
        cur_idx = next((i for i, (tid, _) in enumerate(protons) if tid == current), 0)
        choice, ok = QInputDialog.getItem(
            self,
            tr("proton.title"),
            tr("proton.label", name=entry.app_name, current=current),
            labels,
            cur_idx,
            False,
        )
        if not ok or not choice:
            return
        if not self._confirm_close_steam_if_running():
            return
        tool_id = protons[labels.index(choice)][0]
        paths = self.paths
        self._set_steam_busy(True)
        worker = SteamOpWorker(
            lambda: set_shortcut_proton(paths.config_vdf, entry.appid_u32, tool_id),
            need_steam_closed=True,
        )
        self._track_worker(worker)
        worker.status.connect(self.status_label.setText)

        def _ok(_res: object) -> None:
            self._set_steam_busy(False)
            msg = tr("proton.updated", name=entry.app_name, tool=tool_id)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, tr("proton.error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _bulk_migrate_proton_dialog(self) -> None:
        if not self.paths:
            return
        usage = list_compat_tool_usage(self.paths)
        if not usage:
            QMessageBox.information(self, __app_name__, tr("proton_bulk.empty"))
            return
        protons = list_installed_protons(self.paths.steam_root)
        if not protons:
            QMessageBox.warning(self, __app_name__, tr("proton.none_found"))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("proton_bulk.title"))
        dlg.resize(780, 560)
        lay = QVBoxLayout(dlg)

        by_tool: dict[str, list[CompatMappingEntry]] = {}
        for u in usage:
            by_tool.setdefault(u.tool_name or "(empty)", []).append(u)
        summary_lines = [
            f"• {tool}: {sum(1 for x in items if x.source != 'Orphan')} ativos, "
            f"{sum(1 for x in items if x.source == 'Orphan')} órfãos"
            for tool, items in sorted(by_tool.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]
        summary_box = QTextEdit()
        summary_box.setReadOnly(True)
        summary_box.setMaximumHeight(110)
        summary_box.setPlainText("\n".join(summary_lines))
        lay.addWidget(QLabel(tr("proton_bulk.summary_label")))
        lay.addWidget(summary_box)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(tr("proton_bulk.filter_label")))
        filter_combo = QComboBox()
        filter_combo.addItem(tr("proton_bulk.filter_all"), "__ALL__")
        filter_combo.addItem(tr("proton_bulk.filter_orphans"), "__ORPHAN__")
        for tool in sorted(by_tool.keys()):
            filter_combo.addItem(f"{tool} ({len(by_tool[tool])})", tool)
        filter_row.addWidget(filter_combo, 1)

        filter_row.addWidget(QLabel(tr("proton_bulk.target_label")))
        target_combo = QComboBox()
        for tid, disp in protons:
            target_combo.addItem(disp, tid)
        filter_row.addWidget(target_combo, 1)
        lay.addLayout(filter_row)

        lst = QListWidget()
        lay.addWidget(lst, 1)

        def _populate() -> None:
            flt = filter_combo.currentData()
            lst.clear()
            for u in usage:
                tkey = u.tool_name or "(empty)"
                if flt == "__ORPHAN__" and u.source != "Orphan":
                    continue
                if flt not in ("__ALL__", "__ORPHAN__") and tkey != flt:
                    continue
                it = QListWidgetItem(
                    f"[{u.source}] {u.name} ({u.appid_u32}) — {tkey}"
                )
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                auto_check = flt != "__ALL__"
                it.setCheckState(Qt.CheckState.Checked if auto_check else Qt.CheckState.Unchecked)
                it.setData(Qt.ItemDataRole.UserRole, u)
                lst.addItem(it)

        filter_combo.currentIndexChanged.connect(_populate)
        _populate()

        btn_row = QHBoxLayout()
        btn_migrate = QPushButton(tr("proton_bulk.btn_migrate"))
        btn_migrate.setObjectName("primaryBtn")
        btn_clean_orphans = QPushButton(tr("proton_bulk.btn_clean_orphans"))
        btn_close = QPushButton(tr("proton_bulk.btn_close"))
        btn_row.addWidget(btn_migrate)
        btn_row.addWidget(btn_clean_orphans)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
        btn_close.clicked.connect(dlg.reject)

        action_mode = {"kind": ""}

        def _selected_entries() -> list[CompatMappingEntry]:
            out: list[CompatMappingEntry] = []
            for i in range(lst.count()):
                it = lst.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    out.append(it.data(Qt.ItemDataRole.UserRole))
            return out

        def _on_migrate() -> None:
            if not _selected_entries():
                QMessageBox.information(dlg, __app_name__, tr("proton_bulk.none_selected"))
                return
            action_mode["kind"] = "migrate"
            dlg.accept()

        def _on_clean_orphans() -> None:
            orphans = [u for u in _selected_entries() if u.source == "Orphan"]
            if not orphans:
                QMessageBox.information(dlg, __app_name__, tr("proton_bulk.no_orphans_selected"))
                return
            action_mode["kind"] = "clean_orphans"
            dlg.accept()

        btn_migrate.clicked.connect(_on_migrate)
        btn_clean_orphans.clicked.connect(_on_clean_orphans)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = _selected_entries()
        if not selected or not self._confirm_close_steam_if_running():
            return
        paths = self.paths
        self._set_steam_busy(True)
        if action_mode["kind"] == "clean_orphans":
            orphan_ids = [u.appid_u32 for u in selected if u.source == "Orphan"]
            worker = SteamOpWorker(
                lambda: remove_compat_tools(paths.config_vdf, orphan_ids),
                need_steam_closed=True,
            )
            self._track_worker(worker)

            def _ok_clean(count: object) -> None:
                self._set_steam_busy(False)
                msg = tr("proton_bulk.orphans_removed", count=count)
                self.status_label.setText(msg)
                QMessageBox.information(self, __app_name__, msg)

            worker.finished_ok.connect(_ok_clean)
        else:
            target_tool = str(target_combo.currentData())
            ids = [u.appid_u32 for u in selected]
            worker = SteamOpWorker(
                lambda: bulk_migrate_compat_tool(paths.config_vdf, ids, target_tool),
                need_steam_closed=True,
            )
            self._track_worker(worker)

            def _ok_mig(count: object) -> None:
                self._set_steam_busy(False)
                msg = tr("proton_bulk.migrated", count=count, tool=target_tool)
                self.status_label.setText(msg)
                QMessageBox.information(self, __app_name__, msg)

            worker.finished_ok.connect(_ok_mig)

        worker.status.connect(self.status_label.setText)
        worker.failed.connect(
            lambda err: (
                self._set_steam_busy(False),
                QMessageBox.critical(self, __app_name__, tr("proton.error", err=err)),
            )
        )
        worker.start()

    def _edit_launch_options_presets(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("launch.title", name=entry.app_name))
        dlg.resize(520, 300)
        lay = QVBoxLayout(dlg)

        opts_now = entry.launch_options or ""
        try:
            tokens_now = set(shlex.split(opts_now))
        except ValueError:
            tokens_now = set(opts_now.split())
        chk_gm = QCheckBox(tr("launch.gamemode"))
        chk_gm.setChecked("gamemoderun" in tokens_now)
        chk_mh = QCheckBox(tr("launch.mangohud"))
        chk_mh.setChecked("MANGOHUD=1" in tokens_now)
        chk_w3d = QCheckBox(tr("launch.wined3d"))
        chk_w3d.setChecked("PROTON_USE_WINED3D=1" in tokens_now)
        chk_esync = QCheckBox(tr("launch.no_esync"))
        chk_esync.setChecked("PROTON_NO_ESYNC=1" in tokens_now)
        chk_fsync = QCheckBox(tr("launch.no_fsync"))
        chk_fsync.setChecked("PROTON_NO_FSYNC=1" in tokens_now)

        raw_lbl = QLabel(tr("launch.raw_label"))
        raw_edit = QLineEdit(opts_now)

        def _sync_preview() -> None:
            try:
                updated = apply_launch_presets(
                    raw_edit.text(),
                    gamemode=chk_gm.isChecked(),
                    mangohud=chk_mh.isChecked(),
                    wined3d=chk_w3d.isChecked(),
                    no_esync=chk_esync.isChecked(),
                    no_fsync=chk_fsync.isChecked(),
                )
                raw_edit.setText(updated)
            except ValueError:
                pass

        for chk in (chk_gm, chk_mh, chk_w3d, chk_esync, chk_fsync):
            chk.toggled.connect(_sync_preview)
            lay.addWidget(chk)
        lay.addWidget(raw_lbl)
        lay.addWidget(raw_edit)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not self._confirm_close_steam_if_running():
            return
        new_opts = raw_edit.text().strip()
        paths = self.paths
        self._set_steam_busy(True)
        worker = SteamOpWorker(
            lambda: update_shortcut_launch_options(paths, entry.appid_u32, new_opts),
            need_steam_closed=True,
        )
        self._track_worker(worker)
        worker.status.connect(self.status_label.setText)

        def _ok(_res: object) -> None:
            self._set_steam_busy(False)
            self._refresh_shortcuts()
            msg = tr("launch.saved", name=entry.app_name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _backup_all_saves(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("backup.running"))

        class _BackupWorker(QThread):
            done = Signal(str, int, int)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    zpath, games, files = backup_all_saves(self._paths)
                    self.done.emit(str(zpath), games, files)
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _ok(zpath: str, games: int, files: int) -> None:
            self._set_steam_busy(False)
            if files == 0:
                Path(zpath).unlink(missing_ok=True)
                msg = tr("backup.empty")
            else:
                msg = tr("backup.done", files=files, games=games, path=zpath)
            self.status_label.setText(msg.splitlines()[0])
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, tr("backup.error", err=err))

        w = _BackupWorker(self.paths)
        self._track_worker(w)
        w.done.connect(_ok)
        w.failed.connect(_err)
        w.start()

    def _restore_saves_backup(self) -> None:
        if not self.paths:
            return
        bak_dir = Path.home() / "Backups" / "DeckInstall"
        start_dir = bak_dir if bak_dir.is_dir() else Path.home()
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
                self, __app_name__, tr("restore.confirm", name=zpath.name)
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
                    self.failed.emit(str(exc))

        def _on_restore_done(games: int, files: int) -> None:
            self._set_steam_busy(False)
            msg = tr("restore.done", files=files, games=games)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(self, __app_name__, msg)

        def _on_restore_err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, tr("restore.error", err=err))

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
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, tr("links.error", err=err))

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _remove_selected_shortcut(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        ans = QMessageBox.question(
            self,
            __app_name__,
            tr("library.remove_confirm", name=entry.app_name),
        )
        if ans != QMessageBox.StandardButton.Yes:
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
                    self.failed.emit(str(exc))
                finally:
                    if was_running and not is_steam_running():
                        start_steam()

        worker = _RemoveWorker(self.paths, entry)
        self._track_worker(worker)

        def _on_removed(removed: bool) -> None:
            self._set_steam_busy(False)
            if removed:
                self._refresh_shortcuts()
                self.status_label.setText(tr("library.removed", name=entry.app_name))

        def _on_remove_err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, err)

        worker.done.connect(_on_removed)
        worker.failed.connect(_on_remove_err)
        worker.start()

    def _migrate_selected_shortcut(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        old_exe = Path(entry.exe.strip().strip('"'))
        try:
            old_root = infer_game_root_from_exe(old_exe)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, str(exc))
            return
        suggested = clean_game_name(old_root.name)
        new_name, ok = QInputDialog.getText(
            self,
            __app_name__,
            tr("migrate.prompt_name"),
            text=suggested,
        )
        if not ok or not new_name.strip():
            return
        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(
                self,
                __app_name__,
                tr("migrate.gamescope_one"),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        item = MigrationPlanItem(
            entry=entry,
            old_root=old_root,
            new_name=new_name.strip(),
            new_root=Path.home() / "Games" / new_name.strip(),
            has_compatdata=(self.paths.compatdata_dir / str(entry.appid_u32)).is_dir(),
        )
        self._set_steam_busy(True)
        self._bulk_worker = BulkMigrateWorker(self.paths, [item])
        self._track_worker(self._bulk_worker)
        self._bulk_worker.status.connect(self.status_label.setText)
        self._bulk_worker.finished_ok.connect(self._on_bulk_done)
        self._bulk_worker.failed.connect(self._on_bulk_failed)
        self._bulk_worker.start()

    def _bulk_migrate_shortcuts(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("migrate.analyzing"))

        class _BulkPlanWorker(QThread):
            ready = Signal(list)
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
                    self.ready.emit(plan)
                except Exception as exc:
                    self.failed.emit(str(exc))

        worker = _BulkPlanWorker(self.paths)
        self._track_worker(worker)
        worker.ready.connect(self._confirm_and_run_bulk_plan)
        worker.failed.connect(self._on_bulk_failed)
        worker.start()

    def _confirm_and_run_bulk_plan(self, plan: list[MigrationPlanItem]) -> None:
        if not self.paths:
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
                        self.failed.emit(str(exc))

            def _on_sync_done(checked: int, copied: int) -> None:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                QMessageBox.information(
                    self,
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
        if is_gamescope_session() and is_steam_running():
            msg += tr("migrate.gamescope_warn")
        if QMessageBox.question(self, __app_name__, msg) != QMessageBox.StandardButton.Yes:
            self._set_steam_busy(False)
            return

        self._bulk_worker = BulkMigrateWorker(self.paths, plan)
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
        QMessageBox.information(self, __app_name__, summary)

    def _on_bulk_failed(self, err: str) -> None:
        self._set_steam_busy(False)
        QMessageBox.critical(self, __app_name__, tr("migrate.error", err=err))

    def _cleanup_old_prefixes(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("cleanup.scanning"))

        class _CleanupScanWorker(QThread):
            found = Signal(list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    self.found.emit(find_cleanup_candidates(self._paths, include_system_caches=True))
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _on_scan_failed(err: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText(tr("cleanup.scan_error", err=err))
            QMessageBox.critical(self, __app_name__, tr("cleanup.scan_error", err=err))

        worker = _CleanupScanWorker(self.paths)
        self._track_worker(worker)
        worker.found.connect(self._show_cleanup_dialog)
        worker.failed.connect(_on_scan_failed)
        worker.start()

    def _show_cleanup_dialog(self, candidates: list[CleanupCandidate]) -> None:
        if not candidates:
            self._set_steam_busy(False)
            self.status_label.setText(tr("cleanup.none_found"))
            QMessageBox.information(self, __app_name__, tr("cleanup.none_found"))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("cleanup.title"))
        dlg.resize(720, 460)
        lay = QVBoxLayout(dlg)
        info = QLabel(tr("cleanup.info"))
        info.setWordWrap(True)
        lay.addWidget(info)

        lst = QListWidget()
        lay.addWidget(lst, 1)

        total_lbl = QLabel()

        def _update_total() -> None:
            sel_bytes = 0
            for i in range(lst.count()):
                it = lst.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    c: CleanupCandidate = it.data(Qt.ItemDataRole.UserRole)
                    sel_bytes += c.size_bytes
            total_lbl.setText(tr("cleanup.total", mb=sel_bytes / (1024 * 1024)))

        def _populate_list(items: list[CleanupCandidate]) -> None:
            lst.blockSignals(True)
            lst.clear()
            for cand in items:
                mb = cand.size_bytes / (1024 * 1024)
                if cand.saves_verified_migrated:
                    save_tag = tr("cleanup.tag_verified", game=cand.matched_game)
                elif cand.has_saves:
                    save_tag = tr("cleanup.tag_unverified")
                elif cand.category in ("Prefixo Externo", "external_prefix") and not cand.matched_game:
                    save_tag = tr("cleanup.tag_no_steam")
                else:
                    save_tag = tr("cleanup.tag_no_saves")
                cat_map = {
                    "Prefixo Externo": tr("cleanup.cat_external"),
                    "external_prefix": tr("cleanup.cat_external"),
                    "Backup": tr("cleanup.cat_backup"),
                    "backup": tr("cleanup.cat_backup"),
                    "Compatdata Órfão": tr("cleanup.cat_orphan"),
                    "orphan_compatdata": tr("cleanup.cat_orphan"),
                    "Orphan Shadercache": tr("cleanup.cat_shadercache"),
                    "Steam Logs": tr("cleanup.cat_logs"),
                    "Trash": tr("cleanup.cat_trash"),
                    "App Cache": tr("cleanup.cat_cache"),
                    "Old Proton/Runtime": tr("cleanup.cat_runtime"),
                }
                cat_disp = cat_map.get(cand.category, cand.category)
                hint = f" ({cand.game_hint})" if cand.game_hint else ""
                item = QListWidgetItem(f"[{cat_disp}] {cand.label}{hint} — {mb:.1f} MB{save_tag}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if cand.category in ("Prefixo Externo", "external_prefix"):
                    auto_check = bool(cand.matched_game) and (
                        (not cand.has_saves) or cand.saves_verified_migrated
                    )
                elif cand.category == "Old Proton/Runtime":
                    auto_check = False
                else:
                    auto_check = (not cand.has_saves) or cand.saves_verified_migrated
                item.setCheckState(
                    Qt.CheckState.Checked if auto_check else Qt.CheckState.Unchecked
                )
                item.setData(Qt.ItemDataRole.UserRole, cand)
                lst.addItem(item)
            lst.blockSignals(False)
            _update_total()

        _populate_list(candidates)
        lst.itemChanged.connect(lambda _: _update_total())
        lay.addWidget(total_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText(tr("cleanup.btn_delete"))
        btn_revalidate = btns.addButton(
            tr("cleanup.btn_revalidate"), QDialogButtonBox.ButtonRole.ActionRole
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        dialog_open = True
        reval_running = False
        dlg.finished.connect(lambda _: nonlocal_close())

        def nonlocal_close() -> None:
            nonlocal dialog_open
            dialog_open = False

        def _revalidate_now() -> None:
            nonlocal reval_running
            if not self.paths or reval_running:
                return
            reval_running = True
            btn_revalidate.setEnabled(False)
            ok_btn.setEnabled(False)
            lst.setEnabled(False)
            info.setText(tr("cleanup.revalidating"))

            class _RevalidateWorker(QThread):
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
                        self.failed.emit(str(exc))

            def _on_reval_done(
                new_cands: list[CleanupCandidate], checked: int, copied: int, pending: int
            ) -> None:
                nonlocal reval_running
                reval_running = False
                if not dialog_open:
                    self._set_steam_busy(False)
                    return
                btn_revalidate.setEnabled(True)
                ok_btn.setEnabled(True)
                lst.setEnabled(True)
                _populate_list(new_cands)
                extra = tr("cleanup.reval_pending", pending=pending) if pending else ""
                info.setText(tr("cleanup.reval_done", checked=checked, copied=copied) + extra)

            def _on_reval_err(err: str) -> None:
                nonlocal reval_running
                reval_running = False
                if not dialog_open:
                    self._set_steam_busy(False)
                    return
                btn_revalidate.setEnabled(True)
                ok_btn.setEnabled(True)
                lst.setEnabled(True)
                info.setText(tr("cleanup.reval_error", err=err))

            w = _RevalidateWorker(self.paths)
            self._track_worker(w)
            w.done.connect(_on_reval_done)
            w.failed.connect(_on_reval_err)
            w.start()

        btn_revalidate.clicked.connect(_revalidate_now)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            if not reval_running:
                self._set_steam_busy(False)
            return
        selected = [
            lst.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(lst.count())
            if lst.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not selected or not self.paths:
            if not reval_running:
                self._set_steam_busy(False)
            return
        self.status_label.setText(tr("cleanup.deleting", count=len(selected)))

        class _CleanupDeleteWorker(QThread):
            done = Signal(int, int, list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, items: list[CleanupCandidate]) -> None:
                super().__init__()
                self._paths = paths
                self._items = items

            def run(self) -> None:
                try:
                    if _is_wine_game_running():
                        raise RuntimeError(tr("cleanup.wine_running"))
                    deleted, freed, errs = delete_cleanup_candidates_checked(
                        self._paths, self._items, include_system_caches=True
                    )
                    self.done.emit(deleted, freed, errs)
                except Exception as exc:
                    self.failed.emit(str(exc))

        worker = _CleanupDeleteWorker(self.paths, selected)
        self._track_worker(worker)

        def _on_deleted(deleted: int, freed: int, errs: list[str]) -> None:
            self._set_steam_busy(False)
            mb = freed / (1024 * 1024)
            msg = tr("cleanup.deleted", deleted=deleted, mb=mb)
            if errs:
                msg += tr("cleanup.delete_warnings", errs="\n".join(errs[:10]))
            self.status_label.setText(msg.splitlines()[0])
            if errs:
                QMessageBox.warning(self, __app_name__, msg)
            else:
                QMessageBox.information(self, __app_name__, msg)

        def _on_delete_failed(err: str) -> None:
            self._set_steam_busy(False)
            msg = tr("cleanup.delete_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(self, __app_name__, msg)

        worker.done.connect(_on_deleted)
        worker.failed.connect(_on_delete_failed)
        worker.start()


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
    win.show()
    return app.exec()
