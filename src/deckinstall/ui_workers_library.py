"""Background QThread worker classes for library, artwork, backup, and VDF batch ops."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QThread, Signal

from .artwork import SteamMatch, download_steam_artwork, search_steam_games
from .external_actions import (
    import_external_saves_to_target,
)
from .extractor import sanitize_folder_name
from .i18n import tr
from .launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from .optiscaler import inspect_optiscaler_state
from .runtime_manager import (
    build_umu_env,
    get_umu_run_path,
    is_win32_prefix,
    list_ge_proton11_dirs,
    list_installed_compat_tools,
)
from .standalone import (
    StandaloneGame,
    list_standalone_games,
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
    ShortcutEntry,
    _dir_size_bytes,
    _strip_vdf_quotes,
    backup_all_saves,
    get_compat_tool_map,
    get_shortcut_proton,
    load_shortcuts,
    plan_bulk_migration,
    remove_shortcut,
    restore_saves_backup,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
)
from .ui_cards import _format_worker_error, _is_wine_game_running


def compute_library_runner_labels(
    paths: SteamPaths | None,
    entries: list[ShortcutEntry],
    standalone_games: list[StandaloneGame],
    external_games: list[ExternalLauncherGame],
) -> dict[tuple[str, str], str]:
    """Resolve display labels for Proton/Runner + OptiScaler status off the UI thread."""
    labels: dict[tuple[str, str], str] = {}
    compat_map = get_compat_tool_map(paths.config_vdf) if paths is not None else {}

    def _opti_tag(exe_path: Path) -> str:
        try:
            if exe_path.parent.is_dir() and inspect_optiscaler_state(exe_path) is not None:
                return " [OptiScaler]"
        except OSError:
            pass
        return ""

    default_lbl = tr("proton.default_label")
    native_lbl = tr("library.tag_native_linux")

    for entry in entries:
        runner = compat_map.get(entry.appid_u32, "") or default_lbl
        exe_clean = _strip_vdf_quotes(entry.exe)
        if exe_clean:
            runner += _opti_tag(Path(exe_clean))
        labels[("steam", str(entry.appid_u32))] = runner

    for game in standalone_games:
        if game.native_linux:
            runner = native_lbl
        else:
            runner = (game.proton_name or default_lbl) + _opti_tag(game.exe_path)
        labels[("standalone", str(game.game_dir))] = runner

    for ext in external_games:
        runner = native_lbl if ext.runner == "linux" else (ext.runner or default_lbl)
        if ext.runner != "linux":
            runner += _opti_tag(ext.exe_path)
        labels[("external", f"{ext.source}:{ext.name}:{ext.exe_path}")] = runner

    return labels


class SteamDetectWorker(QThread):
    """Detect Steam paths and load Steam, standalone, and external games."""

    ready = Signal(int, int, object, bool, list, list, list, str, object)
    failed = Signal(str)

    def __init__(
        self,
        req_id: int,
        sc_req_id: int,
        *,
        resolve_steam_paths_fn: Callable[[], SteamPaths | None] | None = None,
        find_steam_root_fn: Callable[[], Path | None] | None = None,
        load_shortcuts_fn: Callable[[Path], list[ShortcutEntry]] | None = None,
        list_standalone_games_fn: Callable[[], list[StandaloneGame]] | None = None,
        scan_external_launcher_games_fn: Callable[[], list[ExternalLauncherGame]] | None = None,
    ) -> None:
        super().__init__()
        self._req_id = req_id
        self._sc_req_id = sc_req_id
        self._resolve_steam_paths = resolve_steam_paths_fn or resolve_steam_paths
        self._find_steam_root = find_steam_root_fn or find_steam_root
        self._load_shortcuts = load_shortcuts_fn or load_shortcuts
        self._list_standalone_games = list_standalone_games_fn or list_standalone_games
        self._scan_external_launcher_games = (
            scan_external_launcher_games_fn or scan_external_launcher_games
        )

    def run(self) -> None:
        try:
            self._detect()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))

    def _detect(self) -> None:
        paths = self._resolve_steam_paths()
        steam_root_found = (
            True if paths is not None else (self._find_steam_root() is not None)
        )
        entries: list[ShortcutEntry] = []
        vdf_err = ""
        if paths is not None:
            try:
                entries = self._load_shortcuts(paths.shortcuts_vdf)
            except Exception as exc:
                vdf_err = str(exc)
        standalone_games = self._list_standalone_games()
        external_games = self._scan_external_launcher_games()
        runner_labels = compute_library_runner_labels(
            paths, entries, standalone_games, external_games
        )
        self.ready.emit(
            self._req_id,
            self._sc_req_id,
            paths,
            steam_root_found,
            entries,
            standalone_games,
            external_games,
            vdf_err,
            runner_labels,
        )


class ShortcutsRefreshWorker(QThread):
    """Reload Steam shortcuts, standalone games, and external launcher games."""

    ready = Signal(int, list, list, list, str, object)
    failed = Signal(str)

    def __init__(
        self,
        req_id: int,
        paths: SteamPaths | None,
        *,
        load_shortcuts_fn: Callable[[Path], list[ShortcutEntry]] | None = None,
        list_standalone_games_fn: Callable[[], list[StandaloneGame]] | None = None,
        scan_external_launcher_games_fn: Callable[[], list[ExternalLauncherGame]] | None = None,
    ) -> None:
        super().__init__()
        self._req_id = req_id
        self._paths = paths
        self._load_shortcuts = load_shortcuts_fn or load_shortcuts
        self._list_standalone_games = list_standalone_games_fn or list_standalone_games
        self._scan_external_launcher_games = (
            scan_external_launcher_games_fn or scan_external_launcher_games
        )

    def run(self) -> None:
        try:
            self._refresh()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))

    def _refresh(self) -> None:
        entries: list[ShortcutEntry] = []
        vdf_err = ""
        if self._paths is not None:
            try:
                entries = self._load_shortcuts(self._paths.shortcuts_vdf)
            except Exception as exc:
                vdf_err = str(exc)
        standalone_games = self._list_standalone_games()
        external_games = self._scan_external_launcher_games()
        runner_labels = compute_library_runner_labels(
            self._paths, entries, standalone_games, external_games
        )
        self.ready.emit(
            self._req_id, entries, standalone_games, external_games, vdf_err, runner_labels
        )


class LaunchPrepWorker(QThread):
    """Prepare launch command, working directory, and environment for a game."""

    ready = Signal(str, str, list, object, object)

    def __init__(
        self,
        kind: str,
        obj: StandaloneGame | ExternalLauncherGame,
        paths: SteamPaths | None = None,
    ) -> None:
        super().__init__()
        self._kind = kind
        self._obj = obj
        self._paths = paths

    def run(self) -> None:
        try:
            if self._kind == "standalone":
                obj = self._obj
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

            if self._kind == "external":
                obj = self._obj
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
                    elif self._paths is not None:
                        proton_bin = find_proton_binary(self._paths.steam_root)
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


class ArtSearchWorker(QThread):
    """Search Steam games for artwork selection."""

    found = Signal(list)
    failed = Signal(str)

    def __init__(self, app_name: str, limit: int = 5) -> None:
        super().__init__()
        self._app_name = app_name
        self._limit = limit

    def run(self) -> None:
        try:
            self.found.emit(search_steam_games(self._app_name, limit=self._limit))
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class ArtDownloadWorker(QThread):
    """Download Steam grid artwork for a shortcut."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        paths: SteamPaths,
        shortcut: ShortcutEntry,
        match: SteamMatch,
    ) -> None:
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
            self.done.emit(tr("art.updated", name=self._match.name, files=files_str))
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class SteamRunningWorker(QThread):
    """Check whether Steam is currently running."""

    ready = Signal(bool)
    failed = Signal(str)

    def run(self) -> None:
        try:
            running = is_steam_running()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))
            return
        self.ready.emit(running)


class ProtonPrepWorker(QThread):
    """Inspect prefix compatibility and list available Proton tools."""

    ready = Signal(bool, list, str)
    failed = Signal(str)

    def __init__(
        self,
        mode: Literal["standalone", "steam"],
        prefix_dir: Path,
        paths: SteamPaths | None = None,
        appid_u32: int = 0,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._prefix_dir = prefix_dir
        self._paths = paths
        self._appid_u32 = appid_u32

    def run(self) -> None:
        try:
            self._prepare()
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))

    def _prepare(self) -> None:
        if is_win32_prefix(self._prefix_dir):
            self.ready.emit(True, [], "")
            return
        if self._mode == "standalone":
            tools: list[tuple[str, str]] = []
            seen_ids: set[str] = set()
            for ct in list_installed_compat_tools():
                if ct.tool_id not in seen_ids:
                    seen_ids.add(ct.tool_id)
                    tools.append((ct.tool_id, ct.folder_name or ct.tool_id))
            if self._paths is not None:
                for tid, disp in list_installed_protons(self._paths.steam_root):
                    if tid not in seen_ids:
                        seen_ids.add(tid)
                        tools.append((tid, disp))
            self.ready.emit(False, tools, "")
            return

        protons = (
            list_installed_protons(self._paths.steam_root)
            if self._paths is not None
            else []
        )
        current_id = (
            get_shortcut_proton(self._paths.config_vdf, self._appid_u32)
            if protons and self._paths is not None
            else ""
        )
        self.ready.emit(False, protons, current_id)


class BackupWorker(QThread):
    """Back up all game saves into a zip archive."""

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


class RestoreWorker(QThread):
    """Restore game saves from a backup zip archive."""

    done = Signal(int, int)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None, zip_file: Path) -> None:
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


class RemoveShortcutWorker(QThread):
    """Remove a Steam shortcut safely with Steam shutdown/relaunch."""

    done = Signal(bool)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry) -> None:
        super().__init__()
        self._paths = paths
        self._shortcut = shortcut

    def run(self) -> None:
        was_running = False
        try:
            was_running = is_steam_running()
            if was_running and not shutdown_steam():
                raise RuntimeError(tr("add.close_steam_timeout"))
            removed = remove_shortcut(self._paths, self._shortcut.appid_u32)
            self.done.emit(removed)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))
        finally:
            try:
                if was_running and not is_steam_running():
                    start_steam()
            except Exception:
                logging.exception("Failed restarting Steam after RemoveShortcutWorker")


class BulkPlanWorker(QThread):
    """Build a bulk migration plan for non-standardized Steam shortcuts."""

    ready = Signal(list, bool)
    failed = Signal(str)

    def __init__(
        self,
        paths: SteamPaths,
        *,
        load_shortcuts_fn: Callable[[Path], list[ShortcutEntry]] | None = None,
    ) -> None:
        super().__init__()
        self._paths = paths
        self._load_shortcuts = load_shortcuts_fn or load_shortcuts

    def run(self) -> None:
        try:
            entries = self._load_shortcuts(self._paths.shortcuts_vdf)
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


class SaveSyncWorker(QThread):
    """Synchronize external prefix saves back to standardized game prefixes."""

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


class DirStatsWorker(QThread):
    """Compute item count and byte size for a game directory."""

    ready = Signal(int, int)

    def __init__(
        self,
        game_dir: Path,
        exe_path: Path,
        inferred_container: bool,
    ) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._exe_path = exe_path
        self._inferred_container = inferred_container

    def run(self) -> None:
        # A failure only means "unknown": ready(0, 0) still releases the caller's busy state.
        try:
            count = sum(1 for _ in self._game_dir.iterdir())
        except Exception:
            count = 0
        try:
            size_b = (
                self._exe_path.stat().st_size
                if self._inferred_container
                else _dir_size_bytes(self._game_dir)
            )
        except Exception:
            size_b = 0
        self.ready.emit(count, size_b)


class RuntimePreflightWorker(QThread):
    """Check whether SteamOS optional runtime confirmation is needed."""

    ready = Signal(bool)
    failed = Signal(str)

    def __init__(
        self,
        *,
        find_steam_root_fn: Callable[[], Path | None] | None = None,
    ) -> None:
        super().__init__()
        self._find_steam_root = find_steam_root_fn or find_steam_root

    def run(self) -> None:
        try:
            steam_root = self._find_steam_root()
            has_steam_proton = (
                steam_root is not None and find_proton_binary(steam_root) is not None
            )
            needs_confirm = has_steam_proton and (
                not get_umu_run_path().is_file() or not list_ge_proton11_dirs()
            )
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))
            return
        self.ready.emit(needs_confirm)


class ExtImportSavesWorker(QThread):
    """Import saves from an external launcher prefix into a Steam or Standalone prefix."""

    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        ext: ExternalLauncherGame,
        target_pfx: Path,
        target_label: str,
        *,
        snapshot_fn: Callable[[Path, str, str, str], Path | None] | None = None,
    ) -> None:
        super().__init__()
        self._ext = ext
        self._target_pfx = target_pfx
        self._target_label = target_label
        self._snapshot_fn = snapshot_fn

    def run(self) -> None:
        try:
            fn = self._snapshot_fn
            snap_fn = (
                (lambda pfx, gname: fn(pfx, gname, "ext-import", "snap")) if fn is not None else None
            )
            copied = import_external_saves_to_target(
                self._ext, self._target_pfx, self._target_label, auto_snapshot_fn=snap_fn
            )
            self.finished_ok.emit(copied)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))
