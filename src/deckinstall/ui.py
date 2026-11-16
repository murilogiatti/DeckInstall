"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QThread, Signal, qInstallMessageHandler
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .app_updater import (
    GITHUB_RELEASES_URL,
    AppUpdateInfo,
    check_app_update,
    download_and_replace_appimage,
    get_running_appimage,
    relaunch_appimage,
)
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
from .runtime_manager import (
    check_runtime_updates,
    check_ubuntu_bwrap_userns,
    ensure_ge_proton11,
    ensure_umu_run,
    fix_bwrap_userns,
    get_compat_tools_dir,
    get_steamrt4_dir,
    get_system_package_install_cmd,
    get_umu_run_path,
    install_system_package,
    list_ge_proton11_dirs,
    validate_steamrt4_integrity,
)
from .scanner import DetectedGame, clean_game_name, detect_exe_kind, scan_all_games, scan_game_folder
from .standalone import (
    StandaloneGame,
    export_steam_to_standalone,
    import_standalone_to_steam,
    install_standalone_game,
    list_standalone_games,
    remove_standalone_game,
    update_standalone_launch_options,
)
from .steam_paths import (
    SteamPaths,
    find_steam_root,
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
    _dir_size_bytes,
    add_or_update_shortcut,
    adopt_installer_prefix,
    apply_launch_presets,
    backup_all_saves,
    bulk_migrate_compat_tool,
    delete_cleanup_candidates_checked,
    detect_helixsr_lsfg_conflict,
    find_cleanup_candidates,
    get_shortcut_proton,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    install_mangohud_topbar_config,
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


class RuntimeUpdateWorker(QThread):
    """Background worker to check and update umu-run and GE-Proton11+ runtimes."""

    status = Signal(str)
    finished_ok = Signal(str, str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.status.emit(tr("runtime.checking"))
            info = check_runtime_updates(force=True)
            umu_info = info.get("umu", {})
            ge_info = info.get("ge_proton", {})
            umu_needs_update = bool(umu_info.get("update_available")) or not umu_info.get("installed")
            ge_needs_update = bool(ge_info.get("update_available")) or not ge_info.get("installed")
            any_updated = umu_needs_update or ge_needs_update

            if umu_needs_update:
                umu_path = ensure_umu_run(
                    force_update=bool(umu_info.get("update_available")),
                    progress_cb=lambda msg, _pct: self.status.emit(msg),
                )
            else:
                umu_path = Path(str(umu_info["installed_path"])) if umu_info.get("installed_path") else ensure_umu_run(
                    force_update=False,
                    progress_cb=lambda msg, _pct: self.status.emit(msg),
                )

            if ge_needs_update:
                ge_path = ensure_ge_proton11(
                    force_update=bool(ge_info.get("update_available")),
                    progress_cb=lambda msg, _pct: self.status.emit(msg),
                )
            else:
                ge_path = Path(str(ge_info["installed_path"])) if ge_info.get("installed_path") else ensure_ge_proton11(
                    force_update=False,
                    progress_cb=lambda msg, _pct: self.status.emit(msg),
                )

            bwrap_warn = check_ubuntu_bwrap_userns() or ""
            msg = (
                tr("runtime.updated", umu=umu_path.name, proton=ge_path.name)
                if any_updated
                else tr("runtime.up_to_date", umu=umu_path.name, proton=ge_path.name)
            )
            self.finished_ok.emit(msg, bwrap_warn)
        except Exception as exc:
            self.failed.emit(str(exc))


class AppUpdateCheckWorker(QThread):
    """Background worker to query GitHub Releases for a newer DeckInstall AppImage."""

    checked = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.checked.emit(check_app_update())
        except Exception as exc:
            self.failed.emit(str(exc))


class AppUpdateDownloadWorker(QThread):
    """Background worker to download, verify, and replace the running AppImage."""

    progress = Signal(int)
    finished_ok = Signal(Path)
    failed = Signal(str)

    def __init__(self, info: AppUpdateInfo) -> None:
        super().__init__()
        self.info = info

    def run(self) -> None:
        try:
            updated_path = download_and_replace_appimage(
                self.info,
                progress_cb=self.progress.emit,
            )
            self.finished_ok.emit(updated_path)
        except Exception as exc:
            self.failed.emit(str(exc))


class StandaloneInstallWorker(QThread):
    """Background worker to prepare runtimes and install a game in standalone mode."""

    status = Signal(str)
    finished_ok = Signal(object, str)
    failed = Signal(str)

    def __init__(
        self,
        app_name: str,
        exe_path: Path,
        steam_appid: int | None,
        create_menu_shortcut: bool = True,
        create_desktop_shortcut: bool = False,
        icon_path: Path | None = None,
        keep_in_place: bool = False,
    ) -> None:
        super().__init__()
        self.app_name = app_name
        self.exe_path = exe_path
        self.steam_appid = steam_appid
        self.create_menu_shortcut = create_menu_shortcut
        self.create_desktop_shortcut = create_desktop_shortcut
        self.icon_path = icon_path
        self.keep_in_place = keep_in_place

    def run(self) -> None:
        try:
            is_native = detect_exe_kind(self.exe_path) == "linux_elf"
            if is_native:
                proton_name = ""
            else:
                self.status.emit(tr("add.preparing_runtime"))
                ensure_umu_run(progress_cb=lambda msg, _pct: self.status.emit(msg))
                ge_dir = ensure_ge_proton11(progress_cb=lambda msg, _pct: self.status.emit(msg))
                proton_name = ge_dir.name

            single_exe_only = False
            try:
                game_dir = infer_game_root_from_exe(self.exe_path)
            except ValueError:
                game_dir = self.exe_path.parent
                if not self.keep_in_place:
                    single_exe_only = True

            game = install_standalone_game(
                game_name=self.app_name,
                exe_path=self.exe_path,
                game_dir=game_dir,
                proton_name=proton_name,
                steam_appid=self.steam_appid,
                icon_path=self.icon_path,
                create_menu_shortcut=self.create_menu_shortcut,
                create_desktop_shortcut=self.create_desktop_shortcut,
                keep_in_place=self.keep_in_place,
                native_linux=is_native,
                single_exe_only=single_exe_only,
            )
            bwrap_warn = "" if is_native else (check_ubuntu_bwrap_userns() or "")
            self.finished_ok.emit(game, bwrap_warn)
        except Exception as exc:
            self.failed.emit(str(exc))


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


_WINE_PROC_NAMES = frozenset(
    {
        "umu-run",
        "pressure-vessel-wrap",
        "pv-bwrap",
        "srt-bwrap",
        "wineserver",
    }
)


def _is_wine_game_running() -> bool:
    """Return True if a Windows .exe, umu/pressure-vessel wrapper, or wineserver is running."""
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
            if not args:
                continue
            if any(a.endswith(".exe") or Path(a).name in _WINE_PROC_NAMES for a in args):
                return True
        except OSError:
            continue
    return False


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


def _cleanup_candidate_auto_check(cand: CleanupCandidate) -> bool:
    """Return True if a cleanup candidate is safe to auto-select for 1-click clean."""
    if cand.category in ("GPU Shader Cache", "Old Proton/Runtime"):
        return False
    if cand.category in ("Prefixo Externo", "external_prefix"):
        return bool(cand.matched_game) and ((not cand.has_saves) or cand.saves_verified_migrated)
    return (not cand.has_saves) or cand.saves_verified_migrated


def format_size_bytes(num_bytes: int | float) -> str:
    """Format byte count as MB (< 1024 MB) or GB (>= 1024 MB)."""
    size = max(0.0, float(num_bytes))
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 ** 3):.2f} GB"
    return f"{size / (1024 ** 2):.1f} MB"


def _format_cleanup_candidate_label(cand: CleanupCandidate) -> str:
    """Format a human-readable list label for a CleanupCandidate."""
    size_str = format_size_bytes(cand.size_bytes)
    if cand.saves_verified_migrated:
        save_tag = tr("cleanup.tag_verified", game=cand.matched_game)
    elif cand.has_saves:
        save_tag = tr("cleanup.tag_unverified")
    elif cand.category in ("Prefixo Externo", "external_prefix") and not cand.matched_game:
        save_tag = tr("cleanup.tag_no_steam")
    elif cand.category in (
        "Prefixo Externo",
        "external_prefix",
        "Compatdata Órfão",
        "orphan_compatdata",
    ):
        save_tag = tr("cleanup.tag_no_saves")
    else:
        save_tag = ""
    cat_map = {
        "Prefixo Externo": tr("cleanup.cat_external"),
        "external_prefix": tr("cleanup.cat_external"),
        "Backup": tr("cleanup.cat_backup"),
        "backup": tr("cleanup.cat_backup"),
        "Compatdata Órfão": tr("cleanup.cat_orphan"),
        "orphan_compatdata": tr("cleanup.cat_orphan"),
        "Orphan Shadercache": tr("cleanup.cat_shadercache"),
        "GPU Shader Cache": tr("maint.cat_gpu_shader"),
        "Steam Logs": tr("cleanup.cat_logs"),
        "Trash": tr("cleanup.cat_trash"),
        "App Cache": tr("cleanup.cat_cache"),
        "Old Proton/Runtime": tr("cleanup.cat_runtime"),
        "Orphan Desktop": tr("maint.cat_orphan_desktop"),
    }
    cat_disp = cat_map.get(cand.category, cand.category)
    hint = f" ({cand.game_hint})" if cand.game_hint else ""
    return f"[{cat_disp}] {cand.label}{hint} — {size_str}{save_tag}"


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
        self._standalone_games: list[StandaloneGame] = []
        self._steam_shortcuts: list[ShortcutEntry] = []
        self._custom_icon_path: Path | None = None
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
        self.stack.addWidget(_wrap_scroll(self._build_add_page()))
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))
        self.stack.addWidget(_wrap_scroll(self._build_maintenance_page()))
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
        self.status_label.setObjectName("MutedText")
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
        self.btn_update_runtimes.setToolTip("umu-run + GE-Proton11+")
        self.btn_update_runtimes.clicked.connect(self._update_runtimes)
        layout.addWidget(self.btn_update_runtimes)

        self.btn_app_update = QPushButton(tr("app_update.btn"))
        self.btn_app_update.clicked.connect(self._check_and_update_app)
        layout.addWidget(self.btn_app_update)

        btn_about = QPushButton(tr("header.about"))
        btn_about.clicked.connect(self._show_about_dialog)
        layout.addWidget(btn_about)
        return sidebar

    def _format_steam_status_text(self) -> str:
        """Return localized sidebar status text for the current Steam detection state."""
        if self.paths:
            return tr("status.steam_id", id=self.paths.user_id)
        if find_steam_root() is not None:
            return tr("status.steam_needs_login")
        return tr("status.steam_not_detected")

    def _redetect_steam(self) -> None:
        """Re-scan Steam paths and update all adaptive UI states."""
        self.paths = resolve_steam_paths()
        status_text = self._format_steam_status_text()
        self.steam_status_lbl.setText(status_text)
        self._update_steam_mode_ui()
        self._refresh_shortcuts()
        self.status_label.setText(status_text)

    def _update_steam_mode_ui(self) -> None:
        """Adapt Add/Extract/Maintenance controls for Steam vs Standalone-only mode."""
        has_steam = self.paths is not None
        self.add_no_steam_banner.setVisible(not has_steam)
        self.btn_add_steam.setVisible(has_steam)
        self.chk_proton.setVisible(has_steam)
        self.chk_restart.setVisible(has_steam)
        self.btn_add_steam.setObjectName("primaryBtn" if has_steam else "")
        self.btn_add_standalone.setObjectName("" if has_steam else "primaryBtn")
        for btn in (self.btn_add_steam, self.btn_add_standalone):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self.btn_use_extracted.setText(
            tr("install.btn_add_steam") if has_steam else tr("install.btn_continue_add")
        )
        if hasattr(self, "btn_maint_revalidate"):
            self.btn_maint_revalidate.setEnabled(has_steam)

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
        btn_browse_exe = QPushButton(tr("install.browse"))
        btn_browse_exe.clicked.connect(self._pick_manual_exe)
        scan_row.addWidget(lbl_det)
        scan_row.addStretch()
        scan_row.addWidget(self.btn_scan)
        scan_row.addWidget(btn_browse_exe)
        list_layout.addLayout(scan_row)

        self.games_list = QListWidget()
        self.games_list.setMinimumHeight(110)
        self.games_list.currentRowChanged.connect(self._on_game_selected)
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

        self.chk_proton = QCheckBox(tr("install.enable_proton"))
        self.chk_proton.setChecked(True)
        form.addRow("", self.chk_proton)

        self.chk_restart = QCheckBox(tr("add.reopen_steam"))
        self.chk_restart.setChecked(True)
        form.addRow("", self.chk_restart)

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

        actions_row = QHBoxLayout()
        actions_row.setSpacing(10)
        self.btn_add_steam = QPushButton(tr("install.btn_add_steam"))
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        actions_row.addWidget(self.btn_add_steam, 1)

        self.btn_add_standalone = QPushButton(tr("install.btn_add_standalone"))
        self.btn_add_standalone.clicked.connect(self._add_selected_standalone)
        actions_row.addWidget(self.btn_add_standalone, 1)

        self.btn_remove_existing = QPushButton(tr("add.btn_remove_existing"))
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
        self.shortcuts_list.currentItemChanged.connect(lambda _c, _p: self._update_library_action_states())
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
        self.act_export_standalone = QAction(tr("library.btn_export_standalone"), self)
        self.act_export_standalone.triggered.connect(self._export_selected_to_standalone)
        self.act_import_steam = QAction(tr("library.btn_import_steam"), self)
        self.act_import_steam.triggered.connect(self._import_selected_to_steam)
        self.act_remove = QAction(tr("library.btn_remove"), self)
        self.act_remove.triggered.connect(self._remove_selected_shortcut)
        for act in (
            self.act_migrate,
            self.act_art,
            self.act_revert_art,
            self.act_proton,
            self.act_bulk_proton,
            self.act_launch_opts,
            self.act_export_standalone,
            self.act_import_steam,
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
        self.act_cleanup.triggered.connect(lambda: self._open_maintenance_page(auto_scan=True))
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

    def _build_maintenance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self._maint_candidates: list[CleanupCandidate] = []
        self._maint_checked: dict[Path, bool] = {}
        self._maint_active_filter = "all"

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
        self.btn_maint_refresh_health = QPushButton(tr("maint.btn_refresh_health"))
        self.btn_maint_refresh_health.clicked.connect(self._refresh_maintenance_health)
        health_top.addWidget(health_lbl)
        health_top.addStretch()
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
        clean_layout.addLayout(pills_flow)

        self.maint_list = QListWidget()
        self.maint_list.setMinimumHeight(220)
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
        self._update_maint_summary()
        return page

    def _open_maintenance_page(self, *, auto_scan: bool = True) -> None:
        self.stack.setCurrentIndex(3)
        if len(self._nav_buttons) > 3:
            self._nav_buttons[3].setChecked(True)
        self._refresh_maintenance_health()
        if auto_scan:
            self._scan_maintenance_cleanup()

    def _refresh_maintenance_health(self) -> None:
        mode = "AppImage" if get_running_appimage() is not None else "Python"
        self.lbl_maint_app.setText(
            tr("maint.health_app", app=__app_name__, version=__version__, mode=mode)
        )

        umu_path = get_umu_run_path()
        umu_status = f"OK ({umu_path})" if umu_path.is_file() else "Missing"
        self.lbl_maint_umu.setText(tr("maint.health_umu", status=umu_status))

        ge_dirs = list_ge_proton11_dirs()
        proton_status = f"{len(ge_dirs)} ({ge_dirs[0].name})" if ge_dirs else "Missing"
        self.lbl_maint_proton.setText(tr("maint.health_proton", status=proton_status))

        steamrt4_dir = get_steamrt4_dir()
        if not steamrt4_dir.is_dir():
            rt4_status = "Missing"
        elif validate_steamrt4_integrity():
            rt4_status = "OK"
        else:
            rt4_status = "Corrupted (cleaned)"
        self.lbl_maint_steamrt4.setText(tr("maint.health_steamrt4", status=rt4_status))

        prefix_dirs: list[Path] = []
        if self.paths is not None and self.paths.compatdata_dir.is_dir():
            prefix_dirs.append(self.paths.compatdata_dir)
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
        for extra_pfx_root in (Path.home() / "Games" / "Prefixes", Path.home() / "Prefixes"):
            if extra_pfx_root.is_dir():
                prefix_dirs.append(extra_pfx_root)
        prefixes_bytes = sum(_dir_size_bytes(p) for p in prefix_dirs)

        runtime_dirs = [p for p in (umu_path.parent, get_compat_tools_dir()) if p.is_dir()]
        runtimes_bytes = sum(_dir_size_bytes(p) for p in runtime_dirs)

        caches_bytes = sum(c.size_bytes for c in self._maint_candidates)
        self.lbl_maint_disk.setText(
            tr(
                "maint.health_disk",
                prefixes=format_size_bytes(prefixes_bytes),
                runtimes=format_size_bytes(runtimes_bytes),
                caches=format_size_bytes(caches_bytes),
            )
        )

    def _on_maint_filter_changed(self, key: str) -> None:
        self._maint_active_filter = key
        self._populate_maint_list()

    def _on_maint_item_changed(self, item: QListWidgetItem) -> None:
        cand = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(cand, CleanupCandidate):
            self._maint_checked[cand.path] = item.checkState() == Qt.CheckState.Checked
            self._update_maint_summary()

    def _populate_maint_list(self) -> None:
        allowed_cats = _MAINT_FILTER_CATEGORIES.get(self._maint_active_filter)
        self.maint_list.blockSignals(True)
        self.maint_list.clear()
        for cand in self._maint_candidates:
            if allowed_cats is not None and cand.category not in allowed_cats:
                continue
            item = QListWidgetItem(_format_cleanup_candidate_label(cand))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = self._maint_checked.get(cand.path, _cleanup_candidate_auto_check(cand))
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, cand)
            self.maint_list.addItem(item)
        self.maint_list.blockSignals(False)
        self._update_maint_summary()

    def _update_maint_summary(self) -> None:
        sel_items = [c for c in self._maint_candidates if self._maint_checked.get(c.path, False)]
        sel_count = len(sel_items)
        sel_size = format_size_bytes(sum(c.size_bytes for c in sel_items))
        safe_items = [c for c in self._maint_candidates if _cleanup_candidate_auto_check(c)]
        safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
        self.lbl_maint_summary.setText(
            tr("maint.summary", sel_size=sel_size, sel_count=sel_count, safe_size=safe_size)
        )
        self.btn_maint_clean_safe.setText(tr("maint.btn_clean_safe", size=safe_size))
        self.btn_maint_delete_selected.setText(tr("maint.btn_delete_selected", size=sel_size))

    def _apply_maint_scan_results(self, candidates: list[CleanupCandidate]) -> None:
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
                    self.failed.emit(str(exc))

        def _on_found(candidates: list[CleanupCandidate]) -> None:
            self._set_steam_busy(False)
            self._apply_maint_scan_results(candidates)
            self.status_label.setText(
                tr("cleanup.none_found") if not candidates else tr("status.ready")
            )

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            msg = tr("cleanup.scan_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(self, __app_name__, msg)

        worker = _MaintScanWorker(self.paths)
        self._track_worker(worker)
        worker.found.connect(_on_found)
        worker.failed.connect(_on_failed)
        worker.start()

    def _revalidate_maintenance_cleanup(self) -> None:
        if not self.paths:
            return
        if _is_wine_game_running():
            QMessageBox.warning(self, __app_name__, tr("cleanup.wine_running"))
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
                    self.failed.emit(str(exc))

        def _on_done(
            new_cands: list[CleanupCandidate], checked: int, copied: int, pending: int
        ) -> None:
            self._set_steam_busy(False)
            self._apply_maint_scan_results(new_cands)
            extra = tr("cleanup.reval_pending", pending=pending) if pending else ""
            self.status_label.setText(tr("cleanup.reval_done", checked=checked, copied=copied) + extra)

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            msg = tr("cleanup.reval_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(self, __app_name__, msg)

        worker = _MaintRevalWorker(self.paths)
        self._track_worker(worker)
        worker.done.connect(_on_done)
        worker.failed.connect(_on_failed)
        worker.start()

    def _maint_clean_safe(self) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(self, __app_name__, tr("cleanup.wine_running"))
            return
        safe_items = [c for c in self._maint_candidates if _cleanup_candidate_auto_check(c)]
        safe_size = format_size_bytes(sum(c.size_bytes for c in safe_items))
        if not safe_items:
            QMessageBox.information(self, __app_name__, tr("maint.safe_clean_empty"))
            return
        ans = QMessageBox.question(
            self,
            __app_name__,
            tr("maint.confirm_safe_clean", count=len(safe_items), size=safe_size),
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._run_maint_delete(safe_items)

    def _maint_delete_selected(self) -> None:
        if _is_wine_game_running():
            QMessageBox.warning(self, __app_name__, tr("cleanup.wine_running"))
            return
        selected = [c for c in self._maint_candidates if self._maint_checked.get(c.path, False)]
        if not selected:
            return
        sel_size = format_size_bytes(sum(c.size_bytes for c in selected))
        ans = QMessageBox.question(
            self,
            __app_name__,
            tr("maint.confirm_delete_selected", count=len(selected), size=sel_size),
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._run_maint_delete(selected)

    def _run_maint_delete(self, items: list[CleanupCandidate]) -> None:
        self._set_steam_busy(True)
        self.status_label.setText(tr("cleanup.deleting", count=len(items)))

        class _MaintDeleteWorker(QThread):
            done = Signal(int, int, list, list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths | None, target_items: list[CleanupCandidate]) -> None:
                super().__init__()
                self._paths = paths
                self._items = target_items

            def run(self) -> None:
                try:
                    if _is_wine_game_running():
                        raise RuntimeError(tr("cleanup.wine_running"))
                    deleted, freed, errs = delete_cleanup_candidates_checked(
                        self._paths, self._items, include_system_caches=True
                    )
                    new_cands = find_cleanup_candidates(self._paths, include_system_caches=True)
                    self.done.emit(deleted, freed, errs, new_cands)
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _on_deleted(
            deleted: int, freed: int, errs: list[str], new_cands: list[CleanupCandidate]
        ) -> None:
            self._set_steam_busy(False)
            self._apply_maint_scan_results(new_cands)
            msg = tr("cleanup.deleted", deleted=deleted, size=format_size_bytes(freed))
            if errs:
                msg += tr("cleanup.delete_warnings", errs="\n".join(errs[:10]))
            self.status_label.setText(msg.splitlines()[0])
            if errs:
                QMessageBox.warning(self, __app_name__, msg)
            else:
                QMessageBox.information(self, __app_name__, msg)

        def _on_failed(err: str) -> None:
            self._set_steam_busy(False)
            msg = tr("cleanup.delete_error", err=err)
            self.status_label.setText(msg)
            QMessageBox.critical(self, __app_name__, msg)

        worker = _MaintDeleteWorker(self.paths, items)
        self._track_worker(worker)
        worker.done.connect(_on_deleted)
        worker.failed.connect(_on_failed)
        worker.start()

    def _show_shortcut_context_menu(self, pos: QPoint) -> None:
        item = self.shortcuts_list.itemAt(pos)
        if item is not None:
            self.shortcuts_list.setCurrentItem(item)
        if self._selected_library_item() is None:
            return
        self._update_library_action_states()
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

    def _show_bwrap_warning_dialog(self, bwrap_warn: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(__app_name__)
        box.setText(tr("runtime.bwrap_warning", msg=bwrap_warn))
        fix_btn = box.addButton(tr("runtime.bwrap_fix_btn"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is fix_btn:
            ok, out = fix_bwrap_userns()
            if ok:
                QMessageBox.information(self, __app_name__, tr("runtime.bwrap_fix_done"))
            else:
                QMessageBox.warning(self, __app_name__, tr("runtime.bwrap_fix_error", err=out))

    def _update_runtimes(self) -> None:
        self.btn_update_runtimes.setEnabled(False)
        self.status_label.setText(tr("runtime.checking"))
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
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self.btn_update_runtimes.setEnabled(True)
            msg = tr("runtime.error", err=err)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, msg)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
                QMessageBox.information(self, __app_name__, msg)
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
                    QMessageBox.information(self, __app_name__, msg)
                elif info.cannot_update_reason == "not_writable":
                    msg = tr(
                        "app_update.manual_not_writable",
                        latest=info.latest_version,
                        current=info.current_version,
                        path=str(info.appimage_path or ""),
                        url=url,
                    )
                    QMessageBox.warning(self, __app_name__, msg)
                else:
                    msg = tr(
                        "app_update.manual_no_asset",
                        latest=info.latest_version,
                        url=url,
                    )
                    QMessageBox.information(self, __app_name__, msg)
                self.status_label.setText(msg.splitlines()[0])
                return

            ans = QMessageBox.question(
                self,
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
                    QMessageBox.information(self, __app_name__, msg)
                else:
                    relaunch = QMessageBox.question(
                        self,
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
                    QMessageBox.critical(self, __app_name__, msg)

            dl_worker.finished_ok.connect(_dl_ok)
            dl_worker.failed.connect(_dl_err)
            dl_worker.start()

        def _on_err(err: str) -> None:
            self.btn_app_update.setEnabled(True)
            msg = tr("app_update.error", err=err)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, msg)

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
        self._custom_icon_path = None
        self.btn_clear_icon.setVisible(False)
        self.add_name.setText(game.name)
        self.add_exe_combo.clear()
        for exe in game.executables:
            self.add_exe_combo.addItem(str(exe))
        self.chk_keep_in_place.setChecked(
            str(game.primary_exe).startswith(("/run/media/", "/mnt/"))
        )
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
        exe = Path(path)
        game = scan_game_folder(exe.parent)
        name = game.name if game else exe.stem
        self.add_name.setText(name)
        self.add_exe_combo.clear()
        self.add_exe_combo.addItem(str(exe))
        self.chk_keep_in_place.setChecked(
            str(exe).startswith(("/run/media/", "/mnt/"))
        )
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
        if cleaned:
            exe_path = Path(cleaned)
            if exe_path.is_file() and detect_exe_kind(exe_path) == "linux_elf":
                self.chk_proton.setChecked(False)
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
            QMessageBox.warning(self, __app_name__, tr("add.invalid_input"))
            return None
        if detect_exe_kind(exe_path) == "dos_mz":
            QMessageBox.warning(self, __app_name__, tr("add.dos_unsupported", name=exe_path.name))
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
            QMessageBox.critical(self, __app_name__, tr("add.steam_missing"))
            return
        if self._vdf_corrupt:
            QMessageBox.critical(
                self,
                __app_name__,
                self.vdf_error_banner.text() or tr("library.vdf_error", err="shortcuts.vdf"),
            )
            return
        validated = self._validate_add_inputs()
        if validated is None:
            return
        app_name, exe_path = validated

        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(
                self,
                __app_name__,
                tr("add.gamescope_confirm"),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        steam_appid = self._resolve_selected_steam_appid()

        self._set_steam_busy(True)
        self._add_worker = AddToSteamWorker(
            paths=self.paths,
            app_name=app_name,
            exe_path=exe_path,
            steam_appid=steam_appid,
            enable_proton=self.chk_proton.isChecked(),
            restart_steam=self.chk_restart.isChecked(),
        )
        self._track_worker(self._add_worker)
        self._add_worker.status.connect(self.status_label.setText)
        self._add_worker.finished_ok.connect(self._on_add_done)
        self._add_worker.failed.connect(self._on_add_failed)
        self._add_worker.start()

    def _add_selected_standalone(self) -> None:
        validated = self._validate_add_inputs()
        if validated is None:
            return
        create_menu = self.chk_shortcut_menu.isChecked()
        create_desktop = self.chk_shortcut_desktop.isChecked()
        if not create_menu and not create_desktop:
            QMessageBox.warning(self, __app_name__, tr("add.no_shortcut_selected"))
            return
        app_name, exe_path = validated
        steam_appid = self._resolve_selected_steam_appid()

        self._set_steam_busy(True)
        worker = StandaloneInstallWorker(
            app_name=app_name,
            exe_path=exe_path,
            steam_appid=steam_appid,
            create_menu_shortcut=create_menu,
            create_desktop_shortcut=create_desktop,
            icon_path=self._custom_icon_path,
            keep_in_place=self.chk_keep_in_place.isChecked(),
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
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
        entries: list[ShortcutEntry] = []
        if not self.paths:
            self._vdf_corrupt = False
            self.vdf_error_banner.hide()
            self._set_vdf_write_enabled(False)
        else:
            try:
                entries = load_shortcuts(self.paths.shortcuts_vdf)
                self._vdf_corrupt = False
                self.vdf_error_banner.hide()
                self._set_vdf_write_enabled(True)
            except Exception as exc:
                self._vdf_corrupt = True
                self.vdf_error_banner.setText(tr("library.vdf_error", err=exc))
                self.vdf_error_banner.show()
                self._set_vdf_write_enabled(False)

        standalone_games = list_standalone_games()
        self._steam_shortcuts = entries
        self._standalone_games = standalone_games
        self._update_remove_existing_state()
        if not entries and not standalone_games:
            empty_item = QListWidgetItem(tr("library.no_shortcuts"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.shortcuts_list.addItem(empty_item)
            self._update_library_action_states()
            return

        for entry in entries:
            item = QListWidgetItem(f"{entry.app_name}  ({entry.appid_u32})  —  {entry.exe}")
            item.setData(Qt.ItemDataRole.UserRole, ("steam", entry))
            self.shortcuts_list.addItem(item)

        standalone_tag = tr("library.tag_standalone")
        for game in standalone_games:
            item = QListWidgetItem(f"{standalone_tag} {game.name}  —  {game.exe_path}")
            item.setData(Qt.ItemDataRole.UserRole, ("standalone", game))
            self.shortcuts_list.addItem(item)

        self._update_library_action_states()

    def _selected_library_item(self) -> tuple[str, ShortcutEntry | StandaloneGame] | None:
        item = self.shortcuts_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2 and data[0] in ("steam", "standalone"):
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
        can_write_vdf = bool(self.paths) and not self._vdf_corrupt

        self.act_migrate.setEnabled(is_steam and can_write_vdf)
        self.act_art.setEnabled(is_steam and bool(self.paths))
        self.act_revert_art.setEnabled(is_steam and bool(self.paths))
        self.act_proton.setEnabled(is_steam and can_write_vdf)
        self.act_export_standalone.setEnabled(is_steam and bool(self.paths))
        self.act_import_steam.setEnabled(is_standalone and can_write_vdf)
        self.act_launch_opts.setEnabled((is_steam and can_write_vdf) or is_standalone)
        self.act_remove.setEnabled((is_steam and can_write_vdf) or is_standalone)

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
            self.act_link_prefixes,
            self.act_import_steam,
            self.act_remove,
        ):
            w.setEnabled(enabled)

    def _set_steam_busy(self, busy: bool) -> None:
        enabled = not busy
        for w in (
            self.btn_reload,
            self.btn_add_standalone,
            self.shortcuts_list,
            self.btn_game_actions,
            self.btn_tools_menu,
            self.act_art,
            self.act_revert_art,
            self.act_cleanup,
            self.act_export_standalone,
            self.btn_maint_scan,
            self.btn_maint_clean_safe,
            self.btn_maint_delete_selected,
        ):
            w.setEnabled(enabled)
        self.btn_maint_revalidate.setEnabled(enabled and (self.paths is not None))
        self._set_vdf_write_enabled(enabled and bool(self.paths) and not self._vdf_corrupt)
        if enabled:
            self._update_library_action_states()
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
        sel = self._selected_library_item()
        if not sel:
            return
        kind, obj = sel
        if kind == "steam" and not self.paths:
            return

        title_name = obj.app_name if kind == "steam" else obj.name
        opts_now = (obj.launch_options or "") if kind == "steam" else (obj.launch_options or "")
        try:
            tokens_list = shlex.split(opts_now)
        except ValueError:
            tokens_list = opts_now.split()
        tokens_now = set(tokens_list)

        if kind == "standalone":
            winedll_now = obj.winedlloverrides or ""
        else:
            winedll_now = ""
            for tok in tokens_list:
                if tok.startswith("WINEDLLOVERRIDES="):
                    winedll_now = tok.split("=", 1)[1]

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("launch.title", name=title_name))
        dlg.resize(580, 500)
        lay = QVBoxLayout(dlg)

        def _muted_hint(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("MutedText")
            lbl.setWordWrap(True)
            lbl.setContentsMargins(20, 0, 0, 4)
            return lbl

        chk_gm = QCheckBox(tr("launch.gamemode"))
        chk_gm.setChecked("gamemoderun" in tokens_now)
        chk_mh = QCheckBox(tr("launch.mangohud"))
        chk_mh.setChecked("MANGOHUD=1" in tokens_now)
        btn_mh_topbar = QPushButton(tr("launch.btn_mangohud_topbar"))
        btn_mh_topbar.clicked.connect(
            lambda: QMessageBox.information(
                dlg,
                __app_name__,
                tr("launch.mangohud_topbar_done", path=str(install_mangohud_topbar_config())),
            )
        )
        chk_dlss = QCheckBox(tr("launch.dlss_upgrade"))
        chk_dlss.setChecked("PROTON_DLSS_UPGRADE=1" in tokens_now)
        chk_w3d = QCheckBox(tr("launch.wined3d"))
        chk_w3d.setChecked("PROTON_USE_WINED3D=1" in tokens_now)
        chk_esync = QCheckBox(tr("launch.no_esync"))
        chk_esync.setChecked("PROTON_NO_ESYNC=1" in tokens_now)
        chk_fsync = QCheckBox(tr("launch.no_fsync"))
        chk_fsync.setChecked("PROTON_NO_FSYNC=1" in tokens_now)
        chk_helixsr = QCheckBox(tr("launch.helixsr_preset"))
        chk_helixsr.setChecked(
            any(p.strip().lower() == "dxgi=n,b" for p in winedll_now.split(";") if p.strip())
        )

        winedll_lbl = QLabel(tr("launch.winedlloverrides_label"))
        winedll_edit = QLineEdit(winedll_now)

        conflict_warn = QLabel(tr("launch.helixsr_lsfg_warn"))
        conflict_warn.setWordWrap(True)
        conflict_warn.setStyleSheet(
            "color: #ffd180; background-color: rgba(180, 110, 20, 0.22); "
            "border: 1px solid #c77c1a; border-radius: 6px; padding: 6px;"
        )
        conflict_warn.setVisible(False)

        raw_lbl = QLabel(tr("launch.raw_label"))
        raw_edit = QLineEdit(opts_now)

        syncing = {"active": False}

        def _update_conflict_warning() -> None:
            has_conflict = detect_helixsr_lsfg_conflict(raw_edit.text(), winedll_edit.text())
            conflict_warn.setVisible(has_conflict)

        def _sync_preview() -> None:
            if syncing["active"]:
                return
            syncing["active"] = True
            try:
                dll_val = winedll_edit.text().strip() if kind == "steam" else ""
                updated = apply_launch_presets(
                    raw_edit.text(),
                    gamemode=chk_gm.isChecked(),
                    mangohud=chk_mh.isChecked(),
                    wined3d=chk_w3d.isChecked(),
                    no_esync=chk_esync.isChecked(),
                    no_fsync=chk_fsync.isChecked(),
                    winedlloverrides=dll_val,
                    dlss_upgrade=chk_dlss.isChecked(),
                )
                raw_edit.setText(updated)
            except ValueError:
                pass
            finally:
                syncing["active"] = False
            _update_conflict_warning()

        def _on_helixsr_toggled(checked: bool) -> None:
            parts = [p.strip() for p in winedll_edit.text().split(";") if p.strip()]
            if checked:
                if not any(p.lower().startswith("dxgi=") for p in parts):
                    parts.append("dxgi=n,b")
            else:
                parts = [p for p in parts if p.lower() != "dxgi=n,b"]
            winedll_edit.setText(";".join(parts))

        def _add_tool_preset(chk: QCheckBox, hint_text: str, bin_name: str) -> None:
            chk.toggled.connect(_sync_preview)
            lay.addWidget(chk)
            lay.addWidget(_muted_hint(hint_text))
            if shutil.which(bin_name) is None:
                warn_row = QWidget()
                warn_lay = QHBoxLayout(warn_row)
                warn_lay.setContentsMargins(20, 0, 0, 4)
                warn_lay.setSpacing(8)
                warn_lbl = QLabel(tr("launch.missing_bin_warn", bin=bin_name))
                warn_lbl.setWordWrap(True)
                warn_lbl.setStyleSheet("color: #ffd180;")
                warn_lay.addWidget(warn_lbl, 1)

                install_btn = QPushButton(tr("launch.btn_install_bin", bin=bin_name))

                def _on_install_bin() -> None:
                    cmd = get_system_package_install_cmd(bin_name)
                    if not cmd:
                        QMessageBox.information(
                            dlg,
                            __app_name__,
                            tr("launch.install_bin_manual", cmd=f"<install {bin_name}>"),
                        )
                        return
                    ans = QMessageBox.question(
                        dlg,
                        __app_name__,
                        tr("launch.install_bin_confirm", bin=bin_name, cmd="sudo " + " ".join(cmd)),
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return
                    install_btn.setEnabled(False)
                    ok, out = install_system_package(bin_name)
                    install_btn.setEnabled(True)
                    if ok and shutil.which(bin_name):
                        warn_row.setVisible(False)
                        QMessageBox.information(dlg, __app_name__, tr("launch.install_bin_done", bin=bin_name))
                    elif ok:
                        warn_row.setVisible(False)
                        QMessageBox.information(dlg, __app_name__, tr("launch.install_bin_done", bin=bin_name))
                    else:
                        QMessageBox.warning(
                            dlg, __app_name__, tr("launch.install_bin_error", bin=bin_name, err=out)
                        )

                install_btn.clicked.connect(_on_install_bin)
                warn_lay.addWidget(install_btn)
                lay.addWidget(warn_row)

        _add_tool_preset(chk_gm, tr("launch.gamemode_hint"), "gamemoderun")
        _add_tool_preset(chk_mh, tr("launch.mangohud_hint"), "mangohud")
        lay.addWidget(btn_mh_topbar)

        chk_dlss.toggled.connect(_sync_preview)
        lay.addWidget(chk_dlss)
        lay.addWidget(_muted_hint(tr("launch.dlss_hint")))

        for chk in (chk_w3d, chk_esync, chk_fsync):
            chk.toggled.connect(_sync_preview)
            lay.addWidget(chk)
        chk_helixsr.toggled.connect(_on_helixsr_toggled)
        lay.addWidget(chk_helixsr)
        lay.addWidget(_muted_hint(tr("launch.helixsr_hint")))
        lay.addWidget(winedll_lbl)
        lay.addWidget(winedll_edit)
        lay.addWidget(conflict_warn)
        lay.addWidget(raw_lbl)
        lay.addWidget(raw_edit)

        winedll_edit.textChanged.connect(_sync_preview)
        raw_edit.textChanged.connect(_update_conflict_warning)
        _update_conflict_warning()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_opts = raw_edit.text().strip()
        new_dll = winedll_edit.text().strip()

        if kind == "standalone":
            try:
                update_standalone_launch_options(
                    obj.game_dir,
                    winedlloverrides=new_dll,
                    launch_options=new_opts,
                )
            except Exception as exc:
                QMessageBox.critical(self, __app_name__, str(exc))
                return
            self._refresh_shortcuts()
            msg = tr("launch.saved", name=title_name)
            self.status_label.setText(msg)
            if not self._closing_pending:
                QMessageBox.information(self, __app_name__, msg)
            return

        if not self._confirm_close_steam_if_running():
            return
        paths = self.paths
        assert paths is not None
        self._set_steam_busy(True)
        worker = SteamOpWorker(
            lambda: update_shortcut_launch_options(paths, obj.appid_u32, new_opts),
            need_steam_closed=True,
        )
        self._track_worker(worker)
        worker.status.connect(self.status_label.setText)

        def _ok(_res: object) -> None:
            self._set_steam_busy(False)
            self._refresh_shortcuts()
            msg = tr("launch.saved", name=title_name)
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
        if _is_wine_game_running():
            QMessageBox.warning(self, __app_name__, tr("cleanup.wine_running"))
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

    def _export_selected_to_standalone(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        ans = QMessageBox.question(
            self,
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
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

    def _import_selected_to_steam(self) -> None:
        sel = self._selected_library_item()
        if not sel or sel[0] != "standalone" or not self.paths:
            return
        game = sel[1]
        assert isinstance(game, StandaloneGame)
        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(self, __app_name__, tr("add.gamescope_confirm"))
            if ans != QMessageBox.StandardButton.Yes:
                return
        paths = self.paths
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
                QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            if not self._closing_pending:
                QMessageBox.critical(self, __app_name__, err)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
                    self,
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
                    self.failed.emit(str(exc))
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
                QMessageBox.critical(self, __app_name__, err)

        worker.done.connect(_on_removed)
        worker.failed.connect(_on_remove_err)
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
            try:
                remove_standalone_game(
                    sa, delete_files=delete_files, delete_prefix=delete_prefix
                )
            except Exception as exc:
                QMessageBox.critical(self, __app_name__, str(exc))
                return
            if steam_entry is not None and self.paths is not None:
                self._remove_steam_shortcut_async(steam_entry, refresh_games=True)
            else:
                self._refresh_games()
                self._refresh_shortcuts()
                self.status_label.setText(tr("library.removed", name=sa.name))
            return

        if steam_entry is not None and self.paths is not None:
            ans = QMessageBox.question(
                self,
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
            try:
                remove_standalone_game(
                    obj, delete_files=delete_files, delete_prefix=delete_prefix
                )
            except Exception as exc:
                QMessageBox.critical(self, __app_name__, str(exc))
                return
            if delete_files:
                self._refresh_games()
            self._refresh_shortcuts()
            self.status_label.setText(tr("library.removed", name=obj.name))
            return

        entry = obj
        assert isinstance(entry, ShortcutEntry)
        if not self.paths:
            return
        ans = QMessageBox.question(
            self,
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
            total_lbl.setText(tr("cleanup.total", size=format_size_bytes(sel_bytes)))

        def _populate_list(items: list[CleanupCandidate]) -> None:
            lst.blockSignals(True)
            lst.clear()
            for cand in items:
                item = QListWidgetItem(_format_cleanup_candidate_label(cand))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                auto_check = _cleanup_candidate_auto_check(cand)
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
            msg = tr("cleanup.deleted", deleted=deleted, size=format_size_bytes(freed))
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
