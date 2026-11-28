"""Background QThread worker classes for library, artwork, backup, and VDF batch ops."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .external_actions import (
    import_external_saves_to_target,
)
from .extractor import sanitize_folder_name
from .fsutil import clean_child_env, sleep_inhibit_problem_cached
from .gameinfo import detect_game_tech
from .i18n import tr
from .launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from .optiscaler import inspect_optiscaler_state
from .runtime_manager import (
    build_umu_env,
    ensure_mangohud,
    get_umu_run_path,
    is_win32_prefix,
    list_default_proton_dirs,
    list_installed_compat_tools,
    official_mangohud_installed,
    system_mangohud_installed,
)
from .standalone import (
    StandaloneGame,
    list_standalone_games,
    match_owned_shortcuts,
    refresh_launcher_if_outdated,
    resolve_standalone_proton_fallback,
    shortcut_game_dir,
    update_standalone_launch_options,
)
from .steam_paths import (
    SteamPaths,
    find_proton_binary,
    find_steam_root,
    is_steam_running,
    measured_dir_size_bytes,
    resolve_steam_paths,
)
from .steam_vdf import (
    ShortcutEntry,
    _dir_size_bytes,
    _strip_vdf_quotes,
    backup_all_saves,
    get_compat_tool_map,
    load_shortcuts,
    remove_shortcut,
    restore_saves_backup,
    save_backup_matches_installed_games,
    shutdown_steam,
    start_steam,
)
from .ui_cards import _format_worker_error, _is_wine_game_running


def close_steam_for_edit(
    is_running: Callable[[], bool],
    shutdown: Callable[[], bool],
    status: Callable[[str], None] | None = None,
) -> bool:
    """Close Steam when running and report whether this operation closed it."""
    if not is_running():
        return False
    if status is not None:
        status(tr("add.closing_steam"))
    if not shutdown():
        raise RuntimeError(tr("add.close_steam_timeout"))
    return True


def restart_steam_after_edit(
    start: Callable[[], bool], operation: str, status: Callable[[str], None] | None = None,
) -> bool:
    if status is not None:
        status(tr("add.restarting_steam"))
    try:
        ok = start()
    except Exception:
        logging.exception("Failed restarting Steam after %s", operation)
        ok = False
    if not ok and status is not None:
        status(tr("steam.restart_failed"))
    return ok


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

    from .proton_policy import newest_default_proton

    newest = newest_default_proton()
    # "Default" means two things: Steam's own default tool, or DeckInstall's newest UMU-Proton.
    steam_default_lbl = tr("proton.steam_default_label")
    default_lbl = tr("proton.default_is", name=newest.name) if newest is not None else tr("proton.default_label")
    native_lbl = tr("library.tag_native_linux")

    for entry in entries:
        runner = compat_map.get(entry.appid_u32, "") or steam_default_lbl
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

    _add_tech_labels(labels, entries, standalone_games, external_games)
    return labels


def compute_library_sizes(
    entries: list[ShortcutEntry],
    games: list[StandaloneGame],
    external: list[ExternalLauncherGame],
    paths: SteamPaths | None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    owned = match_owned_shortcuts(entries, games, paths.user_id if paths else None)
    jobs = {f"standalone:{game.game_dir}": game.game_dir for game in games}
    jobs.update({f"external:{game.source}:{game.name}:{game.exe_path}": game.game_dir for game in external})
    for entry in entries:
        game = owned.get(entry.appid_u32)
        try:
            root = game.game_dir if game else shortcut_game_dir(entry)
        except (OSError, ValueError):
            continue
        jobs[f"steam:{entry.appid_u32}"] = root

    by_path: dict[Path, list[str]] = {}
    for key, root in jobs.items():
        try:
            if root.is_symlink() or not root.is_dir():
                continue
            by_path.setdefault(root.resolve(), []).append(key)
        except OSError:
            continue

    def measure(root: Path) -> int | None:
        return measured_dir_size_bytes(root, strict=True, cancelled=cancelled)

    sizes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for root, size in zip(by_path, pool.map(measure, by_path), strict=True):
            if size is None:
                continue
            for key in by_path[root]:
                sizes[key] = size
    return sizes


def _add_tech_labels(
    labels: dict[tuple[str, str], str],
    entries: list[ShortcutEntry],
    standalone_games: list[StandaloneGame],
    external_games: list[ExternalLauncherGame],
) -> None:
    """Add ("api", key) / ("engine", key) labels: graphics API and engine with versions."""
    jobs: list[tuple[str, Path, Path | None]] = []  # ("<kind>:<key>", exe, game folder)
    for entry in entries:
        exe_clean = _strip_vdf_quotes(entry.exe)
        if exe_clean:
            start = _strip_vdf_quotes(entry.start_dir)
            jobs.append((f"steam:{entry.appid_u32}", Path(exe_clean), Path(start) if start else None))
    jobs.extend((f"standalone:{g.game_dir}", g.exe_path, g.game_dir) for g in standalone_games)
    jobs.extend((f"external:{e.source}:{e.name}:{e.exe_path}", e.exe_path, e.game_dir) for e in external_games)

    def _one(job: tuple[str, Path, Path | None]) -> tuple[str, tuple[str, str]]:
        try:
            return job[0], detect_game_tech(job[1], job[2])
        except Exception:
            return job[0], ("-", "-")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, (api, engine) in pool.map(_one, jobs):
            labels[("api", key)] = api
            labels[("engine", key)] = engine


class SteamDetectWorker(QThread):
    """Detect Steam paths and load Steam, standalone, and external games."""

    read_only = True  # only reads: closing the window never waits for it on screen

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
        # Library folders a Flatpak/Snap Steam cannot open (set before `ready` is emitted).
        self.sandbox_blocked: list[Path] = []

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

    read_only = True
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
        self.owned: dict[int, StandaloneGame] | None = None
        self.primary: dict[Path, ShortcutEntry] | None = None
        self.repairs: list[tuple[ShortcutEntry, StandaloneGame]] | None = None
        self.sandbox_blocked: list[Path] = []  # only filled when Steam is there
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
        # Matching shortcuts to games resolves every path on disk: done here, off the UI thread.
        from .standalone import match_owned_shortcuts, primary_owned_shortcuts, shortcut_path_repairs

        user_id = self._paths.user_id if self._paths is not None else None
        self.owned = match_owned_shortcuts(entries, standalone_games, user_id)
        self.primary = primary_owned_shortcuts(entries, self.owned)
        self.repairs = shortcut_path_repairs(entries, standalone_games, user_id) if self._paths is not None else []
        if self._paths is not None and entries:
            try:
                from .steam_sandbox import folders_steam_cannot_see, shortcut_folders

                self.sandbox_blocked = folders_steam_cannot_see(
                    self._paths.steam_root, shortcut_folders(e.exe for e in entries)
                )
            except Exception as exc:  # only a hint; never break the Library
                logging.warning("Steam sandbox check failed: %s", exc)
        self.ready.emit(
            self._req_id, entries, standalone_games, external_games, vdf_err, runner_labels
        )


class LaunchPrepWorker(QThread):
    """Prepare launch command, working directory, and environment for a game."""

    ready = Signal(str, str, list, object, object)
    # The game starts anyway; this says why the PC may sleep or dim while it runs.
    inhibit_unavailable = Signal(str)

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
            from .launch_presets import parse_presets
            from .modes import STANDALONE, mode_of_kind

            # Only Standalone games use MangoHud; Steam runs its own performance overlay.
            wants_mangohud = (
                mode_of_kind(self._kind) == STANDALONE
                and parse_presets(str(getattr(self._obj, "launch_options", "") or ""))["mangohud"]
            )
            if wants_mangohud and not official_mangohud_installed() and not system_mangohud_installed():
                try:  # the overlay always comes from the official build when no system MangoHud exists
                    ensure_mangohud()
                except Exception as exc:
                    logging.warning("Could not get the official MangoHud: %s", exc)
            if self._kind == "standalone":
                obj = self._obj
                assert isinstance(obj, StandaloneGame)
                if obj.native_linux:
                    self.ready.emit("warn", tr("library.native_not_supported", name=obj.name), [], None, None)
                    return
                fallback_name = ""
                try:
                    refresh_launcher_if_outdated(obj)
                except Exception as exc:  # the old launcher still works
                    logging.warning("Could not refresh the launcher of %s: %s", obj.name, exc)
                if is_win32_prefix(obj.prefix_dir):
                    self.ready.emit("win32", "", [], None, None)
                    return
                problem = sleep_inhibit_problem_cached()
                if problem:
                    self.inhibit_unavailable.emit(problem)
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
                    clean_child_env(),
                )
                return

            if self._kind == "external":
                obj = self._obj
                assert isinstance(obj, ExternalLauncherGame)
                if obj.runner == "linux":
                    self.ready.emit("warn", tr("library.native_not_supported", name=obj.name), [], None, None)
                    return
                if obj.launch_cmd:
                    self.ready.emit("ok", "", list(obj.launch_cmd), None, clean_child_env())
                    return
                from .runtime_manager import find_system_umu_run

                umu_run = find_system_umu_run() or get_umu_run_path()
                if not umu_run.is_file():
                    self.ready.emit("warn", tr("play.umu_missing"), [], None, None)
                    return
                proton_dir: Path | None = None
                defaults = list_default_proton_dirs()
                if defaults:
                    proton_dir = defaults[0]
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
        self.verified_for_cleanup = False

    def run(self) -> None:
        try:
            if _is_wine_game_running():
                raise RuntimeError(tr("cleanup.wine_running"))
            games, files = restore_saves_backup(self._paths, self._zip_file)
            if files:
                try:
                    root = Path.home().resolve()
                    self.verified_for_cleanup = (
                        self._zip_file.absolute() == self._zip_file.resolve()
                        and self._zip_file.is_relative_to(root)
                        and save_backup_matches_installed_games(self._paths, self._zip_file)
                    )
                except (OSError, RuntimeError):
                    self.verified_for_cleanup = False
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
        self.restart_failed = False
        self.closed_steam = False
        self.restart_if_was_running = True

    def run(self) -> None:
        try:
            self.closed_steam = close_steam_for_edit(is_steam_running, shutdown_steam)
            try:
                removed = remove_shortcut(self._paths, self._shortcut.appid_u32)
            finally:
                try:
                    if self.closed_steam and not is_steam_running():
                        self.restart_failed = not restart_steam_after_edit(start_steam, type(self).__name__)
                except Exception:
                    logging.exception("Could not check Steam after removing the shortcut")
                    self.restart_failed = True
            self.done.emit(removed)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class DirStatsWorker(QThread):
    """Compute item count and byte size for a game directory."""

    ready = Signal(int, object)  # byte size as object: a Qt int is 32-bit and games pass 2 GiB

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
