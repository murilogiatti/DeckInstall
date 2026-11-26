"""Background QThread worker classes for DeckInstall."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from . import classify
from . import runtime_manager as _rm
from .app_updater import (
    AppUpdateInfo,
    download_and_replace_appimage,
)
from .artwork import (
    fetch_header_preview,
    search_steam_games,
)
from .extractor import (
    ToolMissingError,
    UnarcError,
    extract_gog_installer,
    run_windows_installer,
    sanitize_folder_name,
)
from .i18n import tr
from .library import Library
from .runtime_manager import (
    _load_versions,
    check_runtime_updates,
    check_ubuntu_bwrap_userns,
    ensure_ge_proton11,
    ensure_mangohud,
    ensure_optiscaler,
    ensure_umu_run,
    mangohud_versions,
    run_exe_in_prefix,
    run_winetricks_verbs,
)
from .scanner import (
    DetectedGame,
    is_native_linux_exe,
    scan_all_games,
)
from .standalone import install_standalone_game
from .steam_paths import (
    SteamPaths,
    is_steam_running,
)
from .steam_vdf import (
    infer_game_root_from_exe,
    shutdown_steam,
    start_steam,
)
from .ui_cards import _format_worker_error
from .ui_workers_library import close_steam_for_edit, restart_steam_after_edit


class ExtractWorker(QThread):
    """Background worker that runs installers (setup.exe, .msi, GOG, repacks, Linux .sh) in an isolated prefix."""

    progress = Signal(int, str)
    finished_ok = Signal(str)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        source: Path,
        dest_root: Path,
        folder_name: str,
    ) -> None:
        super().__init__()
        self.source = source
        self.dest_root = dest_root
        self.folder_name = folder_name
        self.kind = ""
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or self.isInterruptionRequested()

    def _run_installer(self, installer: Path, repack: bool = False) -> Path:
        self.progress.emit(20, tr("install.running_installer", name=installer.name))
        out = run_windows_installer(
            installer,
            self.dest_root / sanitize_folder_name(self.folder_name),
            on_log=lambda msg: self.progress.emit(50, msg),
            cancel_flag=self._is_cancelled,
            repack=repack,
        )
        self.progress.emit(100, tr("install.installer_done", out=out))
        return out

    def run(self) -> None:
        try:
            info = classify.classify_source(self.source)
            self.kind = info.kind
            if not self.folder_name:
                self.folder_name = classify.suggest_install_name(info)
            if info.installer is not None and info.kind in (classify.GOG_SETUP, classify.REPACK, classify.INSTALLER):
                target = self.dest_root / sanitize_folder_name(self.folder_name)
                if info.installer.resolve().is_relative_to(target.resolve()):
                    index = 2
                    while True:
                        candidate = target.with_name(f"{target.name} ({index})")
                        if not candidate.exists() and not candidate.is_symlink():
                            self.folder_name = candidate.name
                            break
                        index += 1
            on_progress = lambda p, msg: self.progress.emit(p, msg)  # noqa: E731
            if info.kind == classify.ARCHIVE:
                raise RuntimeError(tr("install.archive_extract_first"))
            elif info.kind == classify.GOG_SETUP and info.installer is not None:
                try:
                    out = extract_gog_installer(
                        info.installer,
                        self.dest_root,
                        self.folder_name,
                        on_progress=on_progress,
                        cancel_flag=self._is_cancelled,
                    )
                except ToolMissingError:
                    self.progress.emit(10, tr("install.gog_no_innoextract"))
                    out = self._run_installer(info.installer)
                except RuntimeError as exc:
                    if self._is_cancelled():
                        raise
                    self.progress.emit(10, tr("install.gog_innoextract_failed", err=str(exc)))
                    out = self._run_installer(info.installer)
            elif info.kind == classify.REPACK and info.installer is not None:
                out = self._run_installer(info.installer, repack=True)
            elif info.kind == classify.INSTALLER and info.installer is not None:
                out = self._run_installer(info.installer)
            elif info.kind == classify.NATIVE_LINUX:
                raise RuntimeError(tr("install.native_not_supported"))
            else:
                raise RuntimeError(tr("install.unsupported_format", name=self.source.name))
            if self._is_cancelled():
                self.cancelled.emit()
                return
            self.finished_ok.emit(str(out))
        except UnarcError as exc:
            if self._is_cancelled():
                self.cancelled.emit()
            else:
                self.failed.emit(tr("install.unarc_error", code=exc.code or "?"))
        except Exception as exc:
            if self._is_cancelled():
                self.cancelled.emit()
            else:
                self.failed.emit(_format_worker_error(exc))


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
            self.failed.emit(self.req_id, _format_worker_error(exc))


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


class PkexecWorker(QThread):
    """Background worker for pkexec/system package operations so the GUI never freezes."""

    finished_result = Signal(bool, str)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], tuple[bool, str]]) -> None:
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            ok, out = self.fn()
            self.finished_result.emit(ok, out)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class SteamOpWorker(QThread):
    """Background worker for Steam shutdown + VDF, prefix, or artwork operations."""

    status = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fn: Callable[[], object],
        need_steam_closed: bool = False,
        restart_if_was_running: bool = True,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.need_steam_closed = need_steam_closed
        self.restart_if_was_running = restart_if_was_running
        self.closed_steam = False  # True once this worker shut Steam down (the window may offer a restart)
        self.restart_failed = False

    def run(self) -> None:
        try:
            if self.need_steam_closed:
                self.closed_steam = close_steam_for_edit(is_steam_running, shutdown_steam, self.status.emit)
            try:
                result = self.fn()
            finally:
                # Reopen Steam before reporting, whether the operation worked or not.
                if self.closed_steam and self.restart_if_was_running:
                    self.restart_failed = not restart_steam_after_edit(
                        start_steam, type(self).__name__, self.status.emit,
                    )
            self.finished_ok.emit(result)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class ScanWorker(QThread):
    """Background worker to scan game folders without blocking the UI thread."""

    read_only = True  # only reads: closing the window never waits for it on screen

    finished_scan = Signal(list)

    def __init__(
        self,
        roots: list[Path] | None = None,
        scan_fn: Callable[..., list[DetectedGame]] | None = None,
    ) -> None:
        super().__init__()
        self.roots = roots
        self._scan_fn = scan_fn

    def run(self) -> None:
        try:
            fn = self._scan_fn or scan_all_games
            # closeEvent calls requestInterruption(): stop the disk walk instead of making it wait
            self.finished_scan.emit(fn(roots=self.roots, should_stop=self.isInterruptionRequested))
        except Exception:
            logging.exception("ScanWorker failed")
            self.finished_scan.emit([])




RUNTIME_COMPONENTS = ("umu", "ge_proton", "mangohud", "optiscaler")


class RuntimeUpdateWorker(QThread):
    """Install or update the Standalone-mode runtimes, one component or several.

    components: any of RUNTIME_COMPONENTS (default: all). "umu" also refreshes the umu-database.
    """

    status = Signal(str)
    finished_ok = Signal(str, str)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None = None, components: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self.paths = paths
        self.components = tuple(components) if components else RUNTIME_COMPONENTS
        self.updated_proton = ""
        self.updated_optiscaler = ""

    def _after_ge_update(self, previous: str) -> str:
        """Update launchers on the Default policy and leave older versions for review."""
        from .proton_policy import newest_ge_proton, retarget_default_games

        newest = newest_ge_proton()
        if newest is None or newest.name == previous:
            return ""
        self.updated_proton = newest.name
        moved = retarget_default_games(previous)
        return tr("runtime.default_moved", count=moved, proton=newest.name)

    def _update_mangohud(self) -> str:
        """Official MangoHud: installed when missing (unless the system has one), updated when GitHub
        has a newer one. "+" in front when something changed; a failure never stops the rest."""
        try:
            if not _rm.official_mangohud_installed() and _rm.system_mangohud_installed():
                return tr("maint.mangohud_system")
            installed, latest = mangohud_versions()
            if installed and (not latest or installed == latest):
                return f"MangoHud {installed}"
            ensure_mangohud(force_update=bool(installed), progress_cb=lambda msg, _pct: self.status.emit(msg))
            return "+MangoHud " + (str(_load_versions().get("mangohud_installed_version") or latest or ""))
        except Exception as exc:
            logging.warning("MangoHud update failed: %s", exc)
            return tr("runtime.mangohud_failed", err=str(exc))

    def _update_umu(self, info: dict[str, Any]) -> str:
        """umu-run (portable, upstream) and the umu-database; "+" when it changed."""
        umu_info = info.get("umu", {})
        changed = bool(umu_info.get("update_available")) or not umu_info.get("installed")
        if changed or not umu_info.get("installed_path"):
            umu_path = ensure_umu_run(
                force_update=bool(umu_info.get("update_available")),
                progress_cb=lambda msg, _pct: self.status.emit(msg),
            )
        else:
            umu_path = Path(str(umu_info["installed_path"]))
        self.status.emit(tr("runtime.umu_db_updating"))
        if _rm.ensure_umu_database(force_update=True) is None:
            logging.warning("umu-database.csv could not be downloaded")
        return ("+" if changed else "") + f"umu-run ({umu_path.name})"

    def _update_ge(self, info: dict[str, Any]) -> str:
        """Newest GE-Proton, then move "Default" games to it."""
        from .proton_policy import newest_ge_proton

        before = newest_ge_proton()
        ge_info = info.get("ge_proton", {})
        changed = bool(ge_info.get("update_available")) or not ge_info.get("installed")
        if changed or not ge_info.get("installed_path"):
            ge_path = ensure_ge_proton11(
                force_update=bool(ge_info.get("update_available")),
                progress_cb=lambda msg, _pct: self.status.emit(msg),
            )
        else:
            ge_path = Path(str(ge_info["installed_path"]))
        text = ("+" if changed else "") + ge_path.name
        policy_msg = self._after_ge_update(before.name if before is not None else "")
        return f"{text} | {policy_msg}" if policy_msg else text

    def _update_optiscaler(self) -> str:
        old = str(_load_versions().get("optiscaler_installed_version") or "").strip()
        try:
            ensure_optiscaler(check_update=True, progress_cb=lambda msg, _pct: self.status.emit(msg))
        except Exception as exc:
            logging.warning("OptiScaler update failed: %s", exc)
            return tr("runtime.optiscaler_failed", err=str(exc))
        new = str(_load_versions().get("optiscaler_installed_version") or "").strip()
        self.updated_optiscaler = new if new != old else ""
        return ("+" if self.updated_optiscaler else "") + f"OptiScaler {new or '-'}"

    def run(self) -> None:
        try:
            self.status.emit(tr("runtime.checking"))
            comps = self.components
            info = check_runtime_updates(force=True) if ("umu" in comps or "ge_proton" in comps) else {}
            parts: list[str] = []
            if "umu" in comps:
                parts.append(self._update_umu(info))
            if "ge_proton" in comps:
                parts.append(self._update_ge(info))
            if "optiscaler" in comps:
                parts.append(self._update_optiscaler())
            if "mangohud" in comps:
                parts.append(self._update_mangohud())
            any_updated = any(p.startswith("+") for p in parts)
            listed = " | ".join(p.lstrip("+") for p in parts if p)
            bwrap_warn = (check_ubuntu_bwrap_userns() or "") if "umu" in comps else ""
            msg = tr("runtime.updated", items=listed) if any_updated else tr("runtime.up_to_date", items=listed)
            self.finished_ok.emit(msg, bwrap_warn)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class UpdatesCheckWorker(QThread):
    """Check DeckInstall and the given runtimes for updates (updates.check_all, never raises)."""

    read_only = True  # only reads: closing the window never waits for it on screen

    checked = Signal(object)

    def __init__(self, runtimes: tuple[str, ...]) -> None:
        super().__init__()
        self.runtimes = runtimes

    def run(self) -> None:
        from .updates import check_all

        try:
            self.checked.emit(check_all(self.runtimes))
        except Exception:
            logging.exception("Update check failed")
            self.checked.emit({})


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
            self.failed.emit(_format_worker_error(exc))


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
        launch_options: str = "",
        winedlloverrides: str = "",
        game_dir: Path | None = None,
        single_exe_only: bool = False,
        library: Library | None = None,
        prepare_standalone_runtime: bool = True,
    ) -> None:
        super().__init__()
        self.library = library
        self.app_name = app_name
        self.exe_path = exe_path
        self.steam_appid = steam_appid
        self.create_menu_shortcut = create_menu_shortcut
        self.create_desktop_shortcut = create_desktop_shortcut
        self.icon_path = icon_path
        self.keep_in_place = keep_in_place
        self.launch_options = launch_options
        self.winedlloverrides = winedlloverrides
        self.game_dir = game_dir
        self.single_exe_only = single_exe_only
        self.prepare_standalone_runtime = prepare_standalone_runtime

    def run(self) -> None:
        try:
            if is_native_linux_exe(self.exe_path):
                raise RuntimeError(tr("install.native_not_supported"))
            if self.prepare_standalone_runtime:
                self.status.emit(tr("add.preparing_runtime"))
                ensure_umu_run(progress_cb=lambda msg, _pct: self.status.emit(msg))
                # Best effort: GAMEID/STORE lookups (protonfixes) need the umu-database.
                _rm.ensure_umu_database(timeout=15)
                ensure_ge_proton11(progress_cb=lambda msg, _pct: self.status.emit(msg))
            proton_name = ""

            if self.game_dir is not None:
                game_dir = self.game_dir
                single_exe_only = self.single_exe_only
            else:
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
                winedlloverrides=self.winedlloverrides,
                launch_options=self.launch_options,
                icon_path=self.icon_path,
                create_menu_shortcut=self.create_menu_shortcut,
                create_desktop_shortcut=self.create_desktop_shortcut,
                keep_in_place=self.keep_in_place,
                single_exe_only=single_exe_only,
                source={"kind": "folder", "origin": str(game_dir)},
                library=self.library,
            )
            bwrap_warn = check_ubuntu_bwrap_userns() or ""
            self.finished_ok.emit(game, bwrap_warn)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class WinetricksWorker(QThread):
    """Background worker to install winetricks verbs into a game's Wine prefix."""

    log = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        prefix_dir: Path,
        verbs: list[str],
        proton_dir: Path | None = None,
        game_name: str = "",
        pre_snapshot_fn: Callable[[Path, str, str, str], Path | None] | None = None,
    ) -> None:
        super().__init__()
        self.prefix_dir = prefix_dir
        self.verbs = verbs
        self.proton_dir = proton_dir
        self.game_name = game_name
        self.pre_snapshot_fn = pre_snapshot_fn
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        try:
            if self.pre_snapshot_fn is not None:
                self.pre_snapshot_fn(
                    self.prefix_dir,
                    self.game_name or self.prefix_dir.name,
                    "winetricks",
                    "winetricks",
                )
            run_winetricks_verbs(
                self.prefix_dir,
                self.verbs,
                proton_dir=self.proton_dir,
                on_log=lambda line: self.log.emit(line),
                cancel_flag=self._is_cancelled,
            )
            if self._is_cancelled():
                self.failed.emit(tr("winetricks.cancelled"))
                return
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class RunExeInPrefixWorker(QThread):
    """Background worker to execute an .exe/.msi inside a Wine/Proton prefix."""

    status = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        prefix_dir: Path,
        exe_path: Path,
        proton_dir: Path | None = None,
        allow_download: bool = True,
    ) -> None:
        super().__init__()
        self.prefix_dir = prefix_dir
        self.exe_path = exe_path
        self.proton_dir = proton_dir
        self.allow_download = allow_download

    def run(self) -> None:
        try:
            self.status.emit(tr("run_exe.running", exe=self.exe_path.name))
            kwargs: dict[str, Any] = {"proton_path": self.proton_dir}
            if not self.allow_download:
                kwargs["allow_download"] = False
            rc = run_exe_in_prefix(
                self.exe_path,
                self.prefix_dir,
                **kwargs,
            )
            if isinstance(rc, int) and rc != 0:
                self.failed.emit(tr("run_exe.exit_code", name=self.exe_path.name, code=rc))
                return
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


from .ui_workers_library import (  # noqa: E402, F401
    BackupWorker,
    DirStatsWorker,
    ExtImportSavesWorker,
    LaunchPrepWorker,
    RemoveShortcutWorker,
    RestoreWorker,
    ShortcutsRefreshWorker,
    SteamDetectWorker,
    SteamRunningWorker,
    compute_library_runner_labels,
)
