"""Background QThread worker classes for the Maintenance page."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .cleanup import CleanupCandidate, delete_cleanup_candidates_checked, find_cleanup_candidates
from .i18n import tr
from .library import discover_libraries
from .runtime_manager import (
    get_compat_tools_dir,
    get_mangohud_dir,
    get_optiscaler_dir,
    get_steamrt4_dir,
    get_umu_run_path,
    is_steamrt4_valid,
    list_ge_proton11_dirs,
)
from .steam_paths import SteamPaths, dir_size_bytes_many, find_proton_binary, find_steam_root
from .steam_vdf import (
    ShortcutEntry,
    backup_prefix_candidates_to_zip,
    load_shortcuts,
    plan_bulk_migration,
    sync_all_external_saves,
)
from .ui_cards import _format_worker_error, _is_wine_game_running


class MaintHealthWorker(QThread):
    """Collect runtime health and disk usage metrics for the Maintenance page."""

    ready = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(
        self,
        req_id: int,
        paths: SteamPaths | None,
        cached_bytes: int,
        *,
        find_steam_root_fn: Callable[[], Path | None] | None = None,
    ) -> None:
        super().__init__()
        self._req_id = req_id
        self._paths = paths
        self._cached_bytes = cached_bytes
        self._find_steam_root = find_steam_root_fn or find_steam_root

    def run(self) -> None:
        try:
            self._collect()
        except Exception as exc:
            self.failed.emit(self._req_id, _format_worker_error(exc))

    def _collect(self) -> None:
        steam_root = self._find_steam_root()
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
        if not steamrt4_dir.is_dir() and not steamrt4_dir.is_symlink():
            rt4_status = missing_status
        elif is_steamrt4_valid():
            rt4_status = tr("maint.status_ok")
        else:
            rt4_status = tr("maint.status_corrupted")

        from .runtime_manager import _load_versions, official_mangohud_installed, system_mangohud_installed

        if official_mangohud_installed():
            mh_ver = str(_load_versions().get("mangohud_installed_version") or "")
            mangohud_status = f"{tr('maint.status_ok')} ({mh_ver})" if mh_ver else tr("maint.status_ok")
        elif system_mangohud_installed():
            mangohud_status = tr("maint.mangohud_system")
        else:
            mangohud_status = tr("maint.status_missing")

        prefix_dirs: list[Path] = []
        seen_prefix_roots: set[Path] = set()

        def _add_prefix_root(path: Path) -> None:
            if not path.is_dir():
                return
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen_prefix_roots:
                return
            seen_prefix_roots.add(resolved)
            prefix_dirs.append(path)

        if self._paths is not None:
            _add_prefix_root(self._paths.compatdata_dir)
        games_dir = Path.home() / "Games"
        if games_dir.is_dir():
            try:
                for child in games_dir.iterdir():
                    if child.is_dir():
                        for sub_name in ("prefix", "pfx", "wineprefix"):
                            _add_prefix_root(child / sub_name)
            except OSError:
                pass
        for extra_pfx_root in (
            games_dir / "Prefixes",
            games_dir / ".prefixes",
            Path.home() / "Prefixes",
        ):
            _add_prefix_root(extra_pfx_root)
        try:
            for lib in discover_libraries():
                _add_prefix_root(lib.prefixes_dir)
        except OSError:
            pass
        prefixes_bytes = dir_size_bytes_many(prefix_dirs)

        raw_runtime_dirs = (
            umu_path.parent,
            get_compat_tools_dir(),
            get_steamrt4_dir(),
            get_mangohud_dir(),
            get_optiscaler_dir().parent,
        )
        runtime_dirs: list[Path] = []
        seen_rt_dirs: list[Path] = []
        for candidate in raw_runtime_dirs:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                res = candidate.resolve()
            except OSError:
                res = candidate
            if any(res == existing or res.is_relative_to(existing) for existing in seen_rt_dirs):
                continue
            seen_rt_dirs = [p for p in seen_rt_dirs if not p.is_relative_to(res)]
            runtime_dirs = [p for p in runtime_dirs if not p.is_relative_to(res)]
            seen_rt_dirs.append(res)
            runtime_dirs.append(res)
        runtimes_bytes = dir_size_bytes_many(runtime_dirs)

        self.ready.emit(
            self._req_id,
            {
                "umu_status": umu_status,
                "proton_status": proton_status,
                "rt4_status": rt4_status,
                "mangohud_status": mangohud_status,
                "prefixes_bytes": prefixes_bytes,
                "runtimes_bytes": runtimes_bytes,
                "caches_bytes": self._cached_bytes,
            },
        )


class MaintScanWorker(QThread):
    """Scan for cleanup candidates including system caches."""

    found = Signal(list)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        try:
            self.found.emit(
                find_cleanup_candidates(self._paths, include_system_caches=True)
            )
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc))


class MaintRevalWorker(QThread):
    """Revalidate pending migrations, sync external saves, and rescan cleanup items."""

    done = Signal(list, int, int, int)
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
            if _is_wine_game_running():
                raise RuntimeError(tr("cleanup.wine_running"))
            entries = self._load_shortcuts(self._paths.shortcuts_vdf)
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


class MaintDeleteWorker(QThread):
    """Delete selected cleanup candidates with optional unverified save backup."""

    done = Signal(int, object, list, list, str)  # freed bytes as object (can pass 2 GiB)
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


class MaintBackupWorker(QThread):
    """Back up saves from a single prefix cleanup candidate into a zip archive."""

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
