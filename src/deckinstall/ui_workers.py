"""Background QThread worker classes for DeckInstall."""

from __future__ import annotations

import logging
import shlex
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .app_updater import (
    AppUpdateInfo,
    check_app_update,
    download_and_replace_appimage,
)
from .artwork import (
    download_steam_artwork,
    fetch_header_preview,
    search_steam_games,
)
from .extractor import (
    extract_archive,
    is_archive,
    run_windows_installer,
    sanitize_folder_name,
)
from .i18n import tr
from .library import Library
from .runtime_manager import (
    _has_optiscaler_dll,
    _load_versions,
    check_runtime_updates,
    check_ubuntu_bwrap_userns,
    ensure_ge_proton11,
    ensure_optiscaler,
    ensure_umu_run,
    get_optiscaler_dir,
    run_exe_in_prefix,
    run_winetricks_verbs,
)
from .scanner import (
    DetectedGame,
    detect_exe_kind,
    scan_all_games,
)
from .standalone import install_standalone_game
from .steam_paths import (
    SteamPaths,
    _read_compat_tool_id,
    is_steam_running,
)
from .steam_vdf import (
    MigrationPlanItem,
    _format_winedlloverrides_token,
    add_or_update_shortcut,
    adopt_installer_prefix,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    migrate_shortcut_to_games,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
)
from .ui_cards import _format_worker_error


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
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        try:
            if is_archive(self.source):
                out = extract_archive(
                    self.source,
                    self.dest_root,
                    self.folder_name,
                    on_progress=lambda p, msg: self.progress.emit(p, msg),
                    cancel_flag=self._is_cancelled,
                )
            elif self.source.suffix.lower() == ".exe":
                self.progress.emit(20, tr("install.running_installer", name=self.source.name))
                out = run_windows_installer(
                    self.source,
                    self.dest_root / sanitize_folder_name(self.folder_name),
                    on_log=lambda msg: self.progress.emit(50, msg),
                    cancel_flag=self._is_cancelled,
                )
                self.progress.emit(100, tr("install.installer_done", out=out))
            else:
                raise RuntimeError(tr("install.unsupported_format", name=self.source.name))
            if self._is_cancelled():
                self.cancelled.emit()
                return
            self.finished_ok.emit(str(out))
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
            self.failed.emit(_format_worker_error(exc))


class ScanWorker(QThread):
    """Background worker to scan game folders without blocking the UI thread."""

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
            self.finished_scan.emit(fn(roots=self.roots))
        except Exception:
            logging.exception("ScanWorker failed")
            self.finished_scan.emit([])


class FallbackProton10Worker(QThread):
    """Background worker to resolve/download GE-Proton 10 and apply it to a game."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str, bool)

    def __init__(
        self,
        fn_apply: Callable[[str], None],
        need_steam_closed: bool = False,
    ) -> None:
        super().__init__()
        self.fn_apply = fn_apply
        self.need_steam_closed = need_steam_closed

    def run(self) -> None:
        from .runtime_manager import ensure_ge_proton10, list_ge_proton10_dirs

        try:
            ge10_dirs = list_ge_proton10_dirs()
            if ge10_dirs:
                ge10_dir = ge10_dirs[0]
            else:
                self.status.emit(tr("proton10.downloading"))
                try:
                    ge10_dir = ensure_ge_proton10(
                        progress_cb=lambda msg, _pct: self.status.emit(msg)
                    )
                except Exception as exc:
                    self.failed.emit(_format_worker_error(exc), True)
                    return

            tool_id = _read_compat_tool_id(
                ge10_dir / "compatibilitytool.vdf", ge10_dir.name
            )
            if self.need_steam_closed and is_steam_running():
                self.status.emit(tr("add.closing_steam"))
                if not shutdown_steam():
                    raise RuntimeError(tr("add.close_steam_timeout"))

            self.fn_apply(tool_id)
            self.finished_ok.emit(tool_id)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc), False)


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
        launch_options: str = "",
        winedlloverrides: str = "",
    ) -> None:
        super().__init__()
        self.paths = paths
        self.app_name = app_name
        self.exe_path = exe_path
        self.steam_appid = steam_appid
        self.enable_proton = enable_proton
        self.restart_steam = restart_steam
        self.launch_options = launch_options
        self.winedlloverrides = winedlloverrides

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
            effective_launch = self.launch_options.strip()
            if self.winedlloverrides.strip():
                dll_tok = _format_winedlloverrides_token(self.winedlloverrides)
                if effective_launch:
                    try:
                        toks = shlex.split(effective_launch)
                    except ValueError:
                        toks = effective_launch.split()
                    if not any(t.startswith("WINEDLLOVERRIDES=") for t in toks):
                        effective_launch = f"{dll_tok} {effective_launch}"
                else:
                    effective_launch = f"{dll_tok} %command%"

            shortcut_id = add_or_update_shortcut(
                self.paths,
                app_name=self.app_name,
                exe_path=self.exe_path,
                start_dir=self.exe_path.parent,
                launch_options=effective_launch or None,
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
            self.failed.emit(_format_worker_error(exc), shortcut_written)
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

            opti_ver = ""
            if _has_optiscaler_dll(get_optiscaler_dir()):
                old_opti_ver = str(_load_versions().get("optiscaler_installed_version") or "").strip()
                ensure_optiscaler(
                    check_update=True,
                    progress_cb=lambda msg, _pct: self.status.emit(msg),
                )
                opti_ver = str(_load_versions().get("optiscaler_installed_version") or "").strip()
                if opti_ver and opti_ver != old_opti_ver:
                    any_updated = True

            bwrap_warn = check_ubuntu_bwrap_userns() or ""
            msg = (
                tr("runtime.updated", umu=umu_path.name, proton=ge_path.name)
                if any_updated
                else tr("runtime.up_to_date", umu=umu_path.name, proton=ge_path.name)
            )
            if opti_ver:
                msg = f"{msg} | OptiScaler {opti_ver}"
            self.finished_ok.emit(msg, bwrap_warn)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class AppUpdateCheckWorker(QThread):
    """Background worker to query GitHub Releases for a newer DeckInstall AppImage."""

    checked = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.checked.emit(check_app_update())
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


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
                winedlloverrides="" if is_native else self.winedlloverrides,
                launch_options=self.launch_options,
                icon_path=self.icon_path,
                create_menu_shortcut=self.create_menu_shortcut,
                create_desktop_shortcut=self.create_desktop_shortcut,
                keep_in_place=self.keep_in_place,
                native_linux=is_native,
                single_exe_only=single_exe_only,
                source={"kind": "folder", "origin": str(game_dir)},
                library=self.library,
            )
            bwrap_warn = "" if is_native else (check_ubuntu_bwrap_userns() or "")
            self.finished_ok.emit(game, bwrap_warn)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class BulkMigrateWorker(QThread):
    """Background worker to migrate multiple Non-Steam games to ~/Games safely."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        paths: SteamPaths,
        items: list[MigrationPlanItem],
        pre_snapshot_fn: Callable[[Path, str, str, str], Path | None] | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.items = items
        self.pre_snapshot_fn = pre_snapshot_fn

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
                    if self.pre_snapshot_fn is not None:
                        self.pre_snapshot_fn(
                            self.paths.compatdata_dir / str(item.entry.appid_u32) / "pfx",
                            item.entry.app_name,
                            "migrate-games",
                            "migrate",
                        )
                    new_appid, _, steam_appid = migrate_shortcut_to_games(
                        paths=self.paths,
                        entry=item.entry,
                        games_root=games_root,
                        new_name=item.new_name,
                    )
                    if steam_appid is None:
                        try:
                            matches = search_steam_games(item.new_name, limit=1)
                            if matches:
                                steam_appid = matches[0].appid
                        except Exception:
                            pass
                    if steam_appid:
                        try:
                            download_steam_artwork(steam_appid, new_appid, self.paths.grid_dir)
                        except Exception:
                            pass
                    migrated.append(item.new_name)
                except Exception as exc:
                    errors.append(f"{item.entry.app_name}: {_format_worker_error(exc)}")

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
            self.failed.emit(_format_worker_error(exc))
        finally:
            if was_running and not is_steam_running():
                self.status.emit(tr("add.restarting_steam"))
                start_steam()


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
    ) -> None:
        super().__init__()
        self.prefix_dir = prefix_dir
        self.exe_path = exe_path
        self.proton_dir = proton_dir

    def run(self) -> None:
        try:
            self.status.emit(tr("run_exe.running", exe=self.exe_path.name))
            run_exe_in_prefix(
                self.exe_path,
                self.prefix_dir,
                proton_path=self.proton_dir,
            )
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


from .ui_workers_library import (  # noqa: E402, F401
    ArtDownloadWorker,
    ArtSearchWorker,
    BackupWorker,
    BulkPlanWorker,
    DirStatsWorker,
    ExtImportSavesWorker,
    ExtStandaloneWorker,
    LaunchPrepWorker,
    ProtonPrepWorker,
    RemoveShortcutWorker,
    RestoreWorker,
    RuntimePreflightWorker,
    SaveSyncWorker,
    ShortcutsRefreshWorker,
    StandaloneRemoveWorker,
    SteamDetectWorker,
    SteamRunningWorker,
    compute_library_runner_labels,
)
from .ui_workers_maint import (  # noqa: E402, F401
    MaintBackupWorker,
    MaintDeleteWorker,
    MaintHealthWorker,
    MaintRevalWorker,
    MaintScanWorker,
)
