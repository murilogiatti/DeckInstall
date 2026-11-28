"""Background QThread worker classes for the Maintenance page."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from zipfile import ZipFile

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
    list_default_proton_dirs,
)
from .steam_paths import SteamPaths, dir_size_bytes_many, find_proton_binary, find_steam_root
from .steam_vdf import (
    _prefix_has_unconventional_saves,
    backup_prefix_candidates_to_zip,
    sync_all_external_saves,
)
from .ui_cards import _format_worker_error, _is_wine_game_running


class MaintHealthWorker(QThread):
    """Collect runtime health and disk usage metrics for the Maintenance page."""

    read_only = True  # only reads: closing the window never waits for it on screen

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

        from .runtime_manager import find_system_umu_run

        umu_path = find_system_umu_run() or get_umu_run_path()
        umu_status = (
            f"{tr('maint.status_ok')} ({umu_path})"
            if umu_path.is_file()
            else missing_status
        )

        proton_dirs = list_default_proton_dirs()
        proton_status = (
            f"{len(proton_dirs)} ({proton_dirs[0].name})" if proton_dirs else missing_status
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

        from .runtime_manager import get_umu_dir

        raw_runtime_dirs = (
            get_umu_dir(),
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

        from .extractor import _find_7z_bin
        from .runtime_manager import _has_optiscaler_dll
        from .steam_paths import list_installed_protons

        if _has_optiscaler_dll(get_optiscaler_dir()):
            opti_ver = str(_load_versions().get("optiscaler_installed_version") or "")
            optiscaler_status = f"{tr('maint.status_ok')} ({opti_ver})" if opti_ver else tr("maint.status_ok")
        else:
            optiscaler_status = tr("runtimes.status_not_downloaded")
        steam_protons = [
            label for tool_id, label in (list_installed_protons(steam_root) if steam_root is not None else [])
            if not tool_id.lower().startswith(("ge-proton", "umu-proton"))
        ]
        tools = {
            name: shutil.which(name) is not None
            for name in ("winetricks", "gamemoderun", "innoextract", "unrar", "unzip")
        }
        tools["7z"] = _find_7z_bin() is not None

        self.ready.emit(
            self._req_id,
            {
                "optiscaler_status": optiscaler_status,
                "steam_protons": steam_protons,
                "tools": tools,
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
    """Scan for cleanup candidates including system caches. With Steam, the saves of games kept
    outside their prefix are copied in first, so prefixes whose saves are kept show as safe."""

    found = Signal(list, int)  # candidates, saves copied
    failed = Signal(str)

    def __init__(self, paths: SteamPaths | None) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        try:
            copied = 0
            if self._paths is not None and not _is_wine_game_running():
                try:
                    copied = sync_all_external_saves(self._paths)[1]
                except Exception:
                    logging.exception("Could not copy external saves before the cleanup scan")
            self.found.emit(find_cleanup_candidates(self._paths, include_system_caches=True), copied)
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
            backed_up_paths: set[Path] | None = None
            if self._backup_unverified:
                backed_up_paths = set()
                unverified_items = [
                    c for c in self._items if c.has_saves and not c.saves_verified_migrated
                ]
                if unverified_items:
                    if any(
                        item.path.is_symlink() or _prefix_has_unconventional_saves(item.path)
                        for item in unverified_items
                    ):
                        raise RuntimeError(tr("cleanup.backup_incomplete"))
                    zip_path, games_backed, files_backed = backup_prefix_candidates_to_zip(unverified_items)
                    with ZipFile(zip_path) as archive:
                        if not files_backed or games_backed != len(unverified_items) or archive.testzip() is not None:
                            raise RuntimeError(tr("cleanup.backup_incomplete"))
                    backup_zip_str = str(zip_path)
                    backed_up_paths.update(item.path.resolve() for item in unverified_items)
            if _is_wine_game_running():
                raise RuntimeError(tr("cleanup.wine_running"))
            deleted, freed, errs = delete_cleanup_candidates_checked(
                self._paths,
                self._items,
                include_system_caches=True,
                allow_unverified_saves=self._allow_unverified,
                backed_up_paths=backed_up_paths,
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
