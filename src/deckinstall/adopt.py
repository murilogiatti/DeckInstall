"""Adopt games from other launchers into a DeckInstall library (one game = one prefix).

Three steps, each safe to call from a worker thread:

1. find_adoption_candidates() - read-only discovery that unifies the existing detectors:
   Heroic/Lutris/Bottles (launcher_scan), Hydra (its game database and per-game prefixes),
   old Steam Non-Steam shortcuts (shortcuts.vdf + compatdata), umu prefixes (~/Games/umu)
   and a loose folder chosen by the user (candidate_from_folder, ranked by scanner).
   Games that live inside a prefix's drive_c (Hydra, Lutris and Bottles installers) are
   recognized and their real game folder is used.
2. plan_adoption() - read-only: what moves where, sizes, same-filesystem rename or
   cross-device copy, free space, conflicts (blockers) and warnings.
3. adopt() - snapshots the saves, moves the game folder into <library>/Games, MOVES the
   prefix into <library>/Prefixes/<uid> (Steam's compatdata/<appid>/pfx layout is converted),
   or copies only the saves when the prefix is shared, writes the v2 manifest, launch.sh and
   .desktop (install_standalone_game). Every step is undone, in reverse order, when a later
   one fails: files go back where they were and the source launcher's config is never
   touched (a Steam shortcut is only removed on request, as the very last step).

Symlinks are never followed, nothing outside the managed roots is deleted, and a symlinked
or shared prefix is never moved.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any

from . import library as _lib
from . import save_sync
from .external_actions import import_prefix_saves
from .extractor import _SYSTEM_DIRS, DRIVE_C_GAME_PARENTS, find_games_in_drive_c
from .fsutil import (
    atomic_copy_file,
    atomic_write_bytes,
    is_within,
    move_dir_safely,
    remove_path_or_tree,
    sanitize_dir_name,
    unique_dir,
)
from .launcher_scan import ExternalLauncherGame, scan_external_launcher_games
from .manifest import MANIFEST_V1, MANIFEST_V2, encode_path, find_manifest, new_uid
from .scanner import _BIN_DIR_NAMES, clean_game_name, is_native_linux_exe, scan_game_folder
from .standalone import (
    StandaloneGame,
    _foreign_launch_backup_path,
    _has_drive_c,
    _is_protected_dir,
    _is_shortcut_shared_across_users,
    _library_index_path,
    _managed_roots,
    _resolve_desktop_file_path,
    _resolve_desktop_shortcut_path,
    _resolve_proton_path,
    _write_manifest,
    install_standalone_game,
    link_compatdata_to_prefix,
    list_standalone_games,
    shortcut_game_dir,
    shortcut_launch_options,
)
from .steam_paths import SteamPaths, dir_size_bytes, is_gamescope_session, is_steam_running
from .steam_vdf import (
    _PREFIX_REL_DIRS,
    _strip_vdf_quotes,
    get_shortcut_proton,
    load_shortcuts,
    remove_shortcut,
)

# Source kinds.
HEROIC = "heroic"
LUTRIS = "lutris"
BOTTLES = "bottles"
HYDRA = "hydra"
STEAM = "steam"
UMU = "umu"
FOLDER = "folder"
ALL_KINDS = (HEROIC, LUTRIS, BOTTLES, HYDRA, STEAM, UMU)

# Warning codes (the wizard translates them).
W_SHARED_PREFIX = "shared_prefix"  # used by other games: only the saves are copied
W_SYMLINK_PREFIX = "symlink_prefix"  # never moved nor read through
W_NO_PREFIX = "no_prefix"  # a new prefix is created
W_GAME_IN_DRIVE_C = "game_in_drive_c"  # the game folder is moved out of the prefix
W_NEEDS_GAME_FOLDER = "needs_game_folder"  # umu prefix without a recognizable game: ask for it
W_GAME_ROOT_GUESSED = "game_root_guessed"  # game folder inferred from the exe; check it
W_PREFIX_INTERNAL = "prefix_on_internal_disk"  # library on exFAT/NTFS: prefix goes to ~/DeckInstall
W_PREFIX_IN_GAME_DIR = "prefix_inside_game_dir"  # prefix stays with the game; saves copied
W_LAUNCHER_ENTRY_BREAKS = "launcher_entry_breaks"  # the source launcher's entry will stop working
W_STEAM_SHORTCUT_BREAKS = "steam_shortcut_breaks"  # moved game, shortcut kept
W_STEAM_OTHER_USERS = "steam_other_users"  # other Steam users have the same shortcut
W_RUNNER_NOT_INSTALLED = "runner_not_installed"  # the source's Proton is missing: default is used

# Blocker codes.
B_NEEDS_GAME_FOLDER = "needs_game_folder"
B_EXE_MISSING = "exe_missing"
B_EXE_OUTSIDE_GAME_DIR = "exe_outside_game_dir"
B_GAME_DIR_SYMLINK = "game_dir_symlink"
B_GAME_DIR_PROTECTED = "game_dir_protected"
B_GAME_DIR_IS_PREFIX = "game_dir_is_prefix"
B_OVERLAPS_LIBRARY = "overlaps_library"
B_LIBRARY_UNAVAILABLE = "library_unavailable"
B_PREFIX_LAYOUT_CONFLICT = "prefix_layout_conflict"
B_NO_SPACE = "no_space"
B_ALREADY_MANAGED = "already_managed"
B_NATIVE_NOT_SUPPORTED = "native_not_supported"  # Windows games only

# adopt() progress steps.
STEP_SNAPSHOT = "snapshot"
STEP_MOVE_GAME = "move_game"
STEP_MOVE_PREFIX = "move_prefix"
STEP_LAUNCHER = "launcher"
STEP_SAVES = "saves"
STEP_STEAM = "steam"
STEP_DONE = "done"

SnapshotFn = Callable[[Path, str, str, str], "Path | None"]
ProgressFn = Callable[[str], None]

_HYDRA_DB_MAX_BYTES = 64 * 1024 * 1024
_HYDRA_GAME_KEY = re.compile(rb"!games!([A-Za-z0-9_-]{1,32}):([A-Za-z0-9._-]{1,128})")
_GE_PROTON = re.compile(r"(GE-Proton\d+(?:-\d+)?|Proton[ -]\d+\.\d+|Proton - Experimental|Proton Experimental)")
_LAUNCHER_COMMANDS = frozenset(
    {"flatpak", "lutris", "heroic", "bottles-cli", "umu-run", "env", "sh", "bash", "steam", "xdg-open"}
)
_SHARED_PREFIX_NAMES = frozenset({"default", "umu-default", ".wine"})
_UMU_STORES = {"epic": "egs", "gog": "gog", "amazon": "amazon", "steam": "steam"}


@dataclass
class AdoptionCandidate:
    """A game found in another launcher (or a folder) that can be adopted.

    exe/game_dir are None only for a umu prefix whose game was not recognized
    (W_NEEDS_GAME_FOLDER): use candidate_with_folder() once the user picks it.
    """

    kind: str
    name: str
    exe: Path | None
    game_dir: Path | None
    prefix_dir: Path | None
    runner: str = "wine"  # "wine" or "linux"
    runtime: str = ""  # runner version as the source names it
    proton_hint: str = ""  # Proton name derived from runtime ("" when unknown)
    native_linux: bool = False
    store: str = ""  # "steam", "epic", "gog", "amazon", ...
    game_id: str = ""  # Heroic app name, Lutris id, Hydra objectId, umu GAMEID
    steam_appid: int | None = None
    shortcut_appid: int | None = None  # Steam Non-Steam shortcut appid
    launch_options: str = ""
    winedlloverrides: str = ""
    icon: Path | None = None
    in_drive_c: bool = False  # the game folder lives inside the prefix's drive_c
    prefix_shared: bool = False
    warnings: list[str] = field(default_factory=list)
    extra_save_prefixes: tuple[Path, ...] = ()
    steam_compat_dir: Path | None = None

    @property
    def key(self) -> str:
        """Stable identifier for lists/selection."""
        ref = self.exe or self.prefix_dir or self.game_dir
        return f"{self.kind}:{self.game_id}:{ref}"


@dataclass
class AdoptOptions:
    """User choices for one adoption."""

    move_game: bool = True  # False keeps the game folder where it is (forced True for drive_c games)
    move_prefix: bool = True  # False leaves the source prefix untouched and copies only the saves
    name: str = ""  # "" = candidate name
    exe: Path | None = None  # another exe inside the game folder
    proton_name: str | None = None  # None = the source's Proton when installed, else default; "" = default
    create_menu_shortcut: bool = True
    create_desktop_shortcut: bool = False
    remove_from_steam: bool = False  # Steam shortcuts only
    link_steam_compatdata: bool = True  # Steam shortcut kept: compatdata/<appid> links to the new prefix
    add_to_steam: bool = False  # requested after the adoption succeeds


@dataclass
class PlannedMove:
    what: str  # "game" or "prefix"
    src: Path
    dst: Path
    size_bytes: int
    same_fs: bool  # True: atomic rename; False: copy then delete the source


@dataclass
class AdoptionPlan:
    candidate: AdoptionCandidate
    options: AdoptOptions
    uid: str
    name: str
    library_root: Path
    prefixes_root: Path
    prefix_location: str  # "library" or "internal"
    game_dir: Path  # final game folder
    exe: Path  # final exe path
    prefix_dir: Path | None  # final prefix (None for native Linux games)
    prefix_action: str  # "move", "copy_saves", "new" or "none"
    prefix_layout: str = ""  # "umu" (drive_c at the top) or "steam" (pfx/drive_c)
    proton_name: str = ""
    native_linux: bool = False
    moves: list[PlannedMove] = field(default_factory=list)
    bytes_to_copy: int = 0  # bytes crossing filesystems
    free_bytes: dict[str, int] = field(default_factory=dict)  # target folder -> free bytes
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass
class AdoptionResult:
    game: StandaloneGame
    snapshot: Path | None = None
    saves_copied: int = 0
    steam_link: Path | None = None
    removed_from_steam: bool = False
    warnings: list[str] = field(default_factory=list)
    add_to_steam: bool = False
    shortcut_needs_repoint: bool = False
    shortcut_appid: int | None = None
    remove_shortcut_deferred: bool = False


class AdoptionError(RuntimeError):
    """Adoption failed. rolled_back is False when undoing left problems for the user."""

    def __init__(self, message: str, rolled_back: bool = True, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.rolled_back = rolled_back
        self.problems = problems or []


# --- small helpers ----------------------------------------------------------------------------


def _add(warnings: list[str], code: str) -> None:
    if code not in warnings:
        warnings.append(code)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_real_dir(path: Path | None) -> bool:
    return path is not None and not path.is_symlink() and path.is_dir()


def _steam_layout(prefix: Path) -> bool:
    """True for Proton's compatdata layout (pfx/drive_c) with pfx a real folder."""
    pfx = prefix / "pfx"
    return not pfx.is_symlink() and pfx.is_dir() and (pfx / "drive_c").is_dir()


def _wine_root(prefix: Path) -> Path:
    return prefix / "pfx" if _steam_layout(prefix) else prefix


def _proton_hint(runtime: str) -> str:
    m = _GE_PROTON.search(runtime or "")
    return m.group(1) if m else ""


def _proton_installed(name: str) -> bool:
    return bool(name.strip()) and _resolve_proton_path(name) != name


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe


def _dev(path: Path) -> int | None:
    try:
        return _existing_ancestor(path).stat().st_dev
    except OSError:
        return None


def _drive_c_game_root(exe: Path, drive_c: Path) -> Path | None:
    """Return the game's own folder for an exe inside drive_c (e.g. drive_c/Games/<X>)."""
    try:
        rel = exe.relative_to(drive_c)
    except ValueError:
        try:
            rel = _resolved(exe).relative_to(_resolved(drive_c))
        except ValueError:
            return None
    parts = rel.parts
    parents = {p.lower() for p in DRIVE_C_GAME_PARENTS}
    if len(parts) >= 3 and parts[0].lower() in parents:
        if parts[1].lower() in _SYSTEM_DIRS:
            return None
        return drive_c / parts[0] / parts[1]
    if (
        len(parts) >= 6
        and parts[0].lower() == "users"
        and parts[2].lower() == "appdata"
        and parts[3].lower() == "local"
    ):
        from .extractor import _APPDATA_SKIP_DIRS

        if parts[4].lower() == "programs" and len(parts) >= 7:
            if parts[5].lower() in _SYSTEM_DIRS | _APPDATA_SKIP_DIRS:
                return None
            return drive_c / parts[0] / parts[1] / parts[2] / parts[3] / parts[4] / parts[5]
        if parts[4].lower() not in _SYSTEM_DIRS | _APPDATA_SKIP_DIRS:
            return drive_c / parts[0] / parts[1] / parts[2] / parts[3] / parts[4]
        return None
    if len(parts) >= 2 and parts[0].lower() not in parents | {"windows", "users", "programdata"}:
        from .extractor import _APPDATA_SKIP_DIRS

        if parts[0].lower() in _SYSTEM_DIRS | _APPDATA_SKIP_DIRS:
            return None
        return drive_c / parts[0]
    return None


def _saves_hints(prefix: Path | None) -> list[Path]:
    """Folders of the prefix's users that hold something (Saved Games, AppData, Documents...)."""
    if not _is_real_dir(prefix):
        return []
    assert prefix is not None
    users = _wine_root(prefix) / "drive_c" / "users"
    if users.is_symlink() or not users.is_dir():
        return []
    boring = {"microsoft", "temp", "my music", "my pictures", "my videos", "desktop.ini", "openwith"}
    hints: list[Path] = []
    try:
        user_dirs = sorted(d for d in users.iterdir() if d.is_dir() and not d.is_symlink())
    except OSError:
        return []
    for user in user_dirs:
        if user.name.lower() in {"public", "default", "default user", "all users"}:
            continue
        for rel in _PREFIX_REL_DIRS:
            d = user / rel
            if d.is_symlink() or not d.is_dir():
                continue
            try:
                if any(c.name.lower() not in boring for c in d.iterdir()):
                    hints.append(d)
            except OSError:
                continue
    return hints


def _game_exe_for_folder(folder: Path) -> Path | None:
    detected = scan_game_folder(folder)
    return detected.primary_exe if detected is not None else None


# --- discovery --------------------------------------------------------------------------------


def _from_external(ext: ExternalLauncherGame) -> AdoptionCandidate:
    native = ext.runner == "linux"
    return AdoptionCandidate(
        kind=ext.source,
        name=ext.name,
        exe=ext.exe_path,
        game_dir=ext.game_dir,
        prefix_dir=ext.prefix_dir,
        runner=ext.runner,
        runtime=ext.runtime,
        native_linux=native,
        store=ext.store,
        game_id=ext.game_id,
    )


def _hydra_config_dirs(home: Path) -> list[Path]:
    return [home / ".config" / "hydralauncher"]


def read_hydra_games(db_dir: Path) -> list[dict[str, Any]]:
    """Best-effort read of Hydra's LevelDB game records ("!games!<shop>:<id>" -> JSON).

    Only uncompressed records are found (the .log and most .ldb blocks); newer files win.
    Returns dicts with title, objectId, shop, executablePath, winePrefixPath (when present).
    """
    if db_dir.is_symlink() or not db_dir.is_dir():
        return []
    try:
        files = sorted(
            (p for p in db_dir.iterdir() if p.suffix in (".log", ".ldb") and p.is_file() and not p.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    decoder = json.JSONDecoder()
    records: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            if path.stat().st_size > _HYDRA_DB_MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        keys = iter(_HYDRA_GAME_KEY.finditer(data))
        next_match = next(keys, None)
        while next_match is not None:
            m, next_match = next_match, next(keys, None)
            record_end = next_match.start() if next_match is not None else len(data)
            start = data.find(b"{", m.end(), min(m.end() + 64, record_end))
            if start < 0:
                continue
            chunk = data[start : min(start + 256 * 1024, record_end)].decode("utf-8", errors="replace")
            try:
                obj, _ = decoder.raw_decode(chunk)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            key = f"{m.group(1).decode()}:{m.group(2).decode()}"
            if obj.get("isDeleted") is True:
                records.pop(key, None)
                continue
            records[key] = obj
    return list(records.values())


def _scan_hydra(home: Path) -> list[AdoptionCandidate]:
    out: list[AdoptionCandidate] = []
    for config in _hydra_config_dirs(home):
        if config.is_symlink() or not config.is_dir():
            continue
        prefixes_root = config / "wine-prefixes"
        prefixes: list[Path] = []
        if _is_real_dir(prefixes_root):
            try:
                prefixes = sorted(p for p in prefixes_root.iterdir() if _is_real_dir(p) and _has_drive_c(p))
            except OSError:
                prefixes = []
        used_prefixes: set[Path] = set()

        for rec in read_hydra_games(config / "hydra-db"):
            exe_raw = rec.get("executablePath")
            if not isinstance(exe_raw, str) or not exe_raw.strip():
                continue
            exe = Path(exe_raw)
            if not exe.is_absolute() or exe.is_symlink() or not exe.is_file():
                continue
            object_id = str(rec.get("objectId") or "")
            shop = str(rec.get("shop") or "")
            prefix: Path | None = None
            raw_prefix = rec.get("winePrefixPath")
            if isinstance(raw_prefix, str) and raw_prefix.strip():
                prefix = Path(raw_prefix)
            if prefix is None:
                prefix = next((p for p in prefixes if is_within(exe, p, strict=True)), None)
            if prefix is None and object_id:
                prefix = next(
                    (
                        p
                        for p in prefixes
                        if p.name == object_id
                        or p.name.endswith(f"-{object_id}")
                        or p.name.startswith(f"{object_id}-")
                    ),
                    None,
                )
            if prefix is not None:
                used_prefixes.add(_resolved(prefix))
            appid = int(object_id) if shop == "steam" and object_id.isdigit() else None
            out.append(
                AdoptionCandidate(
                    kind=HYDRA,
                    name=str(rec.get("title") or "").strip() or clean_game_name(exe.parent.name),
                    exe=exe,
                    game_dir=None,  # inferred in _finish
                    prefix_dir=prefix,
                    store=shop,
                    game_id=object_id,
                    steam_appid=appid,
                )
            )

        # Prefixes without a usable record: Hydra installs repacks inside drive_c.
        for prefix in prefixes:
            if _resolved(prefix) in used_prefixes:
                continue
            out.extend(_drive_c_candidates(HYDRA, prefix))
    return out


def _drive_c_candidates(kind: str, prefix: Path) -> list[AdoptionCandidate]:
    picked, _ = find_games_in_drive_c(_wine_root(prefix) / "drive_c")
    out: list[AdoptionCandidate] = []
    for folder in picked:
        exe = _game_exe_for_folder(folder)
        if exe is None:
            continue
        out.append(
            AdoptionCandidate(
                kind=kind,
                name=clean_game_name(folder.name),
                exe=exe,
                game_dir=folder,
                prefix_dir=prefix,
                game_id=prefix.name if kind == UMU else "",
            )
        )
    return out


def _scan_umu(home: Path) -> list[AdoptionCandidate]:
    root = home / "Games" / "umu"
    if not _is_real_dir(root):
        return []
    out: list[AdoptionCandidate] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for prefix in children:
        if prefix.name.startswith(".") or not _is_real_dir(prefix) or not _has_drive_c(prefix):
            continue
        found = _drive_c_candidates(UMU, prefix)
        if not found:
            found = [
                AdoptionCandidate(
                    kind=UMU,
                    name=prefix.name,
                    exe=None,
                    game_dir=None,
                    prefix_dir=prefix,
                    game_id=prefix.name,
                    warnings=[W_NEEDS_GAME_FOLDER],
                )
            ]
        appid_str = prefix.name.removeprefix("umu-")
        for c in found:
            if appid_str.isdigit():
                c.steam_appid = int(appid_str)
                c.store = "steam"
        out.extend(found)
    return out


def _scan_steam(paths: SteamPaths) -> list[AdoptionCandidate]:
    out: list[AdoptionCandidate] = []
    for entry in load_shortcuts(paths.shortcuts_vdf):
        exe = Path(_strip_vdf_quotes(entry.exe))
        if not exe.is_absolute() or exe.name in _LAUNCHER_COMMANDS or exe.name == "launch.sh":
            continue
        if len(exe.parts) > 1 and exe.parts[1] in ("usr", "bin", "sbin", "opt", "app", "snap"):
            continue  # a system program or launcher, not a game folder
        if exe.is_symlink() or not exe.is_file():
            continue
        native = exe.suffix.lower() not in (".exe", ".bat", ".msi")
        if native and not is_native_linux_exe(exe):
            continue
        warnings: list[str] = []
        try:
            game_dir = shortcut_game_dir(entry)
        except ValueError:
            game_dir = exe.parent
            warnings.append(W_GAME_ROOT_GUESSED)
        compat = paths.compatdata_dir / str(entry.appid_u32)
        prefix: Path | None = None
        if compat.is_symlink():
            if any(is_within(compat, r) for r in _managed_roots(_lib.PREFIXES)):
                continue  # already a DeckInstall game linked into Steam
            prefix = compat
        elif compat.is_dir():
            prefix = compat
        launch_options, dll = shortcut_launch_options(entry)
        icon_raw = _strip_vdf_quotes(entry.icon)
        icon = Path(icon_raw) if icon_raw and Path(icon_raw).is_file() else None
        runtime = "" if native else get_shortcut_proton(paths.config_vdf, entry.appid_u32)
        try:
            if _is_shortcut_shared_across_users(paths, entry.appid_u32):
                warnings.append(W_STEAM_OTHER_USERS)
        except OSError:
            pass
        out.append(
            AdoptionCandidate(
                kind=STEAM,
                name=entry.app_name.strip() or game_dir.name,
                exe=exe,
                game_dir=game_dir,
                prefix_dir=prefix,
                runner="linux" if native else "wine",
                runtime=runtime,
                native_linux=native,
                shortcut_appid=entry.appid_u32,
                launch_options=launch_options,
                winedlloverrides=dll,
                icon=icon,
                warnings=warnings,
            )
        )
    return out


def _is_managed(c: AdoptionCandidate) -> bool:
    """True when DeckInstall already manages this game (manifest, library folder or prefix)."""
    if c.game_dir is not None:
        if find_manifest(c.game_dir) is not None or _lib.find_library_root(c.game_dir) is not None:
            return True
    if c.exe is not None and _lib.find_library_root(c.exe) is not None:
        return True
    if c.prefix_dir is not None and not c.in_drive_c:
        return any(is_within(c.prefix_dir, r, strict=True) for r in _managed_roots(_lib.PREFIXES))
    return False


def _finish(c: AdoptionCandidate, prefix_users: dict[Path, int], managed_prefixes: set[Path]) -> None:
    """Fill in the derived fields: drive_c game root, game folder, sharing, saves, warnings."""
    if c.prefix_dir is not None and c.prefix_dir.is_symlink():
        _add(c.warnings, W_SYMLINK_PREFIX)
    if c.exe is not None and _is_real_dir(c.prefix_dir):
        assert c.prefix_dir is not None
        drive_c = _wine_root(c.prefix_dir) / "drive_c"
        if is_within(c.exe, drive_c, strict=True):
            c.in_drive_c = True
            _add(c.warnings, W_GAME_IN_DRIVE_C)
            root = _drive_c_game_root(c.exe, drive_c)
            if root is None or root.is_symlink():
                c.game_dir = None
                _add(c.warnings, W_NEEDS_GAME_FOLDER)
            else:
                c.game_dir = root
            games, _ = find_games_in_drive_c(drive_c)
            if len(games) > 1:
                c.prefix_shared = True
    if c.exe is not None and _needs_inferred_game_dir(c) and not c.in_drive_c:
        from .steam_vdf import infer_game_root_from_exe

        try:
            c.game_dir = infer_game_root_from_exe(c.exe)
        except ValueError:
            c.game_dir = c.exe.parent
            _add(c.warnings, W_GAME_ROOT_GUESSED)
    if c.exe is not None and not c.native_linux and c.runner == "linux":
        c.native_linux = True
    if c.prefix_dir is not None and not c.native_linux:
        if W_STEAM_OTHER_USERS in c.warnings:
            c.prefix_shared = True
        key = _resolved(c.prefix_dir)
        if (
            prefix_users.get(key, 0) > 1
            or key in managed_prefixes
            or c.prefix_dir.name.lower() in _SHARED_PREFIX_NAMES
            or _is_protected_dir(c.prefix_dir)
        ):
            c.prefix_shared = True
        if c.prefix_shared:
            _add(c.warnings, W_SHARED_PREFIX)
        if not _has_drive_c(c.prefix_dir):
            _add(c.warnings, W_NO_PREFIX)
    elif not c.native_linux:
        _add(c.warnings, W_NO_PREFIX)
    if not c.proton_hint:
        c.proton_hint = _proton_hint(c.runtime)
    if c.store and not c.game_id and c.steam_appid:
        c.game_id = str(c.steam_appid)


def _prefix_save_score(prefix: Path | None) -> tuple[int, float, int]:
    """Return (has_hints, newest_mtime, save_file_count) for saves inside a Wine prefix."""
    hints = _saves_hints(prefix)
    if not hints:
        return (0, 0.0, 0)
    count = 0
    newest_mtime = 0.0
    for hint in hints:
        try:
            for root_s, _dirs, files_s in os.walk(hint, followlinks=False):
                for fname in files_s:
                    fp = Path(root_s) / fname
                    if fp.is_symlink():
                        continue
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    count += 1
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
        except OSError:
            continue
    return (1, newest_mtime, count)


def _merge_steam_into_candidate(primary: AdoptionCandidate, steam_cand: AdoptionCandidate) -> None:
    """Attach a matching Steam shortcut onto a Heroic/Lutris/Bottles/Hydra candidate."""
    if primary.shortcut_appid is None and steam_cand.shortcut_appid is not None:
        primary.shortcut_appid = steam_cand.shortcut_appid
        primary.steam_compat_dir = steam_cand.prefix_dir
    if not primary.launch_options and steam_cand.launch_options:
        primary.launch_options = steam_cand.launch_options
    if not primary.winedlloverrides and steam_cand.winedlloverrides:
        primary.winedlloverrides = steam_cand.winedlloverrides
    if primary.icon is None and steam_cand.icon is not None:
        primary.icon = steam_cand.icon
    if not primary.runtime and steam_cand.runtime:
        primary.runtime = steam_cand.runtime
    if W_STEAM_OTHER_USERS in steam_cand.warnings:
        _add(primary.warnings, W_STEAM_OTHER_USERS)

    steam_pfx = steam_cand.prefix_dir
    if steam_pfx is None or primary.native_linux:
        return
    prim_pfx = primary.prefix_dir
    prim_valid = prim_pfx is not None and _is_real_dir(prim_pfx) and _has_drive_c(prim_pfx)
    steam_valid = _is_real_dir(steam_pfx) and _has_drive_c(steam_pfx)
    if prim_pfx is None or not prim_valid:
        if prim_pfx is not None and _is_real_dir(prim_pfx) and _resolved(prim_pfx) != _resolved(steam_pfx):
            primary.extra_save_prefixes = (*primary.extra_save_prefixes, prim_pfx)
        primary.prefix_dir = steam_pfx
    elif steam_valid and _resolved(steam_pfx) != _resolved(prim_pfx):
        if _prefix_save_score(steam_pfx) > _prefix_save_score(prim_pfx):
            primary.extra_save_prefixes = (*primary.extra_save_prefixes, prim_pfx)
            primary.prefix_dir = steam_pfx
        else:
            primary.extra_save_prefixes = (*primary.extra_save_prefixes, steam_pfx)


def _needs_inferred_game_dir(c: AdoptionCandidate) -> bool:
    return c.game_dir is None or (
        c.kind in {LUTRIS, HEROIC, BOTTLES}
        and c.exe is not None
        and c.game_dir == c.exe.parent
        and c.game_dir.name.lower() in _BIN_DIR_NAMES
    )


def _candidate_game_dir(c: AdoptionCandidate) -> Path | None:
    if not _needs_inferred_game_dir(c):
        return c.game_dir
    if c.exe is None:
        return None
    from .steam_vdf import infer_game_root_from_exe

    try:
        return infer_game_root_from_exe(c.exe)
    except ValueError:
        return c.exe.parent


def find_adoption_candidates(
    *,
    home: Path | None = None,
    paths: SteamPaths | None = None,
    kinds: tuple[str, ...] | None = None,
) -> list[AdoptionCandidate]:
    """Read-only scan of every supported source. Steam shortcuts need paths (current user).

    Games DeckInstall already manages are left out; the same exe/folder found by another
    source and Steam merges the Steam shortcut into the primary candidate without duplicating it.
    """
    home_dir = home or Path.home()
    wanted = set(kinds or ALL_KINDS)
    found: list[AdoptionCandidate] = []
    if wanted & {HEROIC, LUTRIS, BOTTLES}:
        try:
            exts = scan_external_launcher_games(home_dir)
        except Exception as exc:  # a broken launcher config must not hide the other sources
            logging.warning("External launcher scan failed: %s", exc)
            exts = []
        order = {HEROIC: 0, LUTRIS: 1, BOTTLES: 2}
        exts = sorted((e for e in exts if e.source in wanted), key=lambda e: order.get(e.source, 9))
        found.extend(_from_external(e) for e in exts)
    for kind, scan in ((HYDRA, _scan_hydra), (UMU, _scan_umu)):
        if kind in wanted:
            try:
                found.extend(scan(home_dir))
            except Exception as exc:
                logging.warning("%s scan failed: %s", kind, exc)
    if STEAM in wanted and paths is not None:
        try:
            steam = _scan_steam(paths)
        except Exception as exc:
            logging.warning("Steam shortcut scan failed: %s", exc)
            steam = []
        umu = [c for c in found if c.kind == UMU]
        found = [c for c in found if c.kind != UMU] + steam + umu

    out: list[AdoptionCandidate] = []
    by_exe: dict[Path, AdoptionCandidate] = {}
    by_game_dir: dict[Path, AdoptionCandidate] = {}
    seen_prefixes: set[Path] = set()
    for c in found:
        gdir = _candidate_game_dir(c)
        existing: AdoptionCandidate | None = None
        if c.exe is not None:
            existing = by_exe.get(_resolved(c.exe))
        if existing is None and gdir is not None and not c.in_drive_c:
            existing = by_game_dir.get(_resolved(gdir))
        if existing is not None:
            if c.kind == STEAM and existing.kind != STEAM:
                _merge_steam_into_candidate(existing, c)
            continue
        if c.exe is not None:
            by_exe[_resolved(c.exe)] = c
        if gdir is not None and not c.in_drive_c:
            by_game_dir[_resolved(gdir)] = c
        if (c.kind != UMU or c.exe is not None) and c.prefix_dir is not None:
            seen_prefixes.add(_resolved(c.prefix_dir))
        out.append(c)
    # A umu prefix without a recognized game is dropped when another source owns it.
    out = [c for c in out if c.exe is not None or c.prefix_dir is None or _resolved(c.prefix_dir) not in seen_prefixes]

    managed_prefixes: set[Path] = set()
    for g in list_standalone_games():
        managed_prefixes.add(_resolved(g.prefix_dir))
    prefix_users: dict[Path, int] = {}
    for c in out:
        if c.prefix_dir is not None:
            k = _resolved(c.prefix_dir)
            prefix_users[k] = prefix_users.get(k, 0) + 1
    result: list[AdoptionCandidate] = []
    for c in out:
        _finish(c, prefix_users, managed_prefixes)
        if not _is_managed(c):
            result.append(c)
    return result


def candidate_from_folder(
    folder: Path,
    exe: Path | None = None,
    prefix_dir: Path | None = None,
    name: str = "",
) -> AdoptionCandidate:
    """A loose game folder chosen by the user ("pasta solta"); the exe is ranked by scanner."""
    chosen = exe or _game_exe_for_folder(folder)
    native = chosen is not None and chosen.suffix.lower() != ".exe" and is_native_linux_exe(chosen)
    c = AdoptionCandidate(
        kind=FOLDER,
        name=name.strip() or clean_game_name(folder.name),
        exe=chosen,
        game_dir=folder,
        prefix_dir=prefix_dir,
        runner="linux" if native else "wine",
        native_linux=native,
    )
    if chosen is None:
        c.warnings.append(W_NEEDS_GAME_FOLDER)
    _finish(c, {}, {_resolved(g.prefix_dir) for g in list_standalone_games()})
    return c


def candidate_with_folder(candidate: AdoptionCandidate, folder: Path, exe: Path | None = None) -> AdoptionCandidate:
    """Copy of candidate (e.g. a umu prefix) with the game folder the user picked."""
    chosen = exe or _game_exe_for_folder(folder)
    c = replace(
        candidate,
        exe=chosen,
        game_dir=folder,
        in_drive_c=False,
        warnings=[w for w in candidate.warnings if w not in (W_NEEDS_GAME_FOLDER, W_GAME_IN_DRIVE_C)],
    )
    if chosen is None:
        c.warnings.append(W_NEEDS_GAME_FOLDER)
    if candidate.prefix_dir is not None and c.name == candidate.prefix_dir.name:
        c.name = clean_game_name(folder.name)  # umu prefixes are named after the GAMEID
    _finish(c, {}, {_resolved(g.prefix_dir) for g in list_standalone_games()})
    return c


# --- planning ---------------------------------------------------------------------------------


def _layout_conflicts(compat: Path) -> bool:
    """True when compatdata/<appid> holds an entry that also exists inside pfx."""
    pfx = compat / "pfx"
    try:
        return any(item.name != "pfx" and (pfx / item.name).exists() for item in compat.iterdir())
    except OSError:
        return True


def _adoption_space(moves: Iterable[PlannedMove]) -> tuple[dict[str, int], bool]:
    """Measure the combined space needed on each destination filesystem."""
    needed: dict[int | str, tuple[str, int]] = {}
    for mv in moves:
        if not mv.same_fs:
            probe = str(_existing_ancestor(mv.dst.parent))
            device = _dev(Path(probe))
            key = device if device is not None else probe
            representative, total = needed.get(key, (probe, 0))
            needed[key] = (representative, total + mv.size_bytes)
    free: dict[str, int] = {}
    enough = True
    for probe, bytes_needed in needed.values():
        try:
            free[probe] = shutil.disk_usage(probe).free
        except OSError:
            free[probe] = 0
        if free[probe] < int(bytes_needed * 1.05):
            enough = False
    return free, enough


def plan_adoption(
    candidate: AdoptionCandidate,
    library_root: Path | None = None,
    options: AdoptOptions | None = None,
) -> AdoptionPlan:
    """Work out an adoption without changing anything (walks folders for sizes: use a worker)."""
    c = candidate
    opts = options or AdoptOptions()
    warnings = list(c.warnings)
    blockers: list[str] = []
    root = library_root or _lib.internal_library_root()
    internal_root = _lib.internal_library_root()
    uid = new_uid()
    name = (opts.name or c.name).strip() or (c.game_dir.name if c.game_dir else "Game")

    if root.is_symlink() or not root.parent.is_dir():
        blockers.append(B_LIBRARY_UNAVAILABLE)
    unix_fs = _lib.fs_type_of(root).lower() not in _lib.NON_UNIX_FS
    if unix_fs:
        prefixes_root, prefix_location = root / _lib.PREFIXES, "library"
    else:
        prefixes_root, prefix_location = internal_root / _lib.PREFIXES, "internal"
        _add(warnings, W_PREFIX_INTERNAL)

    exe = opts.exe or c.exe
    has_game = c.game_dir is not None and exe is not None
    game_src = c.game_dir or root
    exe = exe or game_src
    if not has_game:
        blockers.append(B_NEEDS_GAME_FOLDER)
    elif _is_managed(c):
        blockers.append(B_ALREADY_MANAGED)
    if has_game:
        if game_src.is_symlink():
            blockers.append(B_GAME_DIR_SYMLINK)
        elif _is_protected_dir(game_src):
            blockers.append(B_GAME_DIR_PROTECTED)
        if not exe.is_file() or exe.is_symlink():
            blockers.append(B_EXE_MISSING)
        elif not is_within(exe, game_src, strict=True):
            blockers.append(B_EXE_OUTSIDE_GAME_DIR)
        if is_within(root, game_src) or is_within(game_src, root / _lib.PREFIXES):
            blockers.append(B_OVERLAPS_LIBRARY)

    native = c.native_linux or (exe.is_file() and exe.suffix.lower() != ".exe" and is_native_linux_exe(exe))
    if native:
        blockers.append(B_NATIVE_NOT_SUPPORTED)
    games_root = root / _lib.GAMES
    move_game = (opts.move_game or c.in_drive_c) and not is_within(game_src, games_root, strict=True)
    game_dst = unique_dir(games_root / sanitize_dir_name(name)) if move_game else game_src
    try:
        exe_dst = game_dst / exe.relative_to(game_src) if move_game else exe
    except ValueError:
        try:
            exe_dst = game_dst / _resolved(exe).relative_to(_resolved(game_src)) if move_game else exe
        except ValueError:
            exe_dst = exe
            _add(blockers, B_EXE_OUTSIDE_GAME_DIR)

    if c.steam_compat_dir is not None and c.steam_compat_dir.is_symlink():
        _add(warnings, W_SYMLINK_PREFIX)
    src_prefix = c.prefix_dir
    prefix_layout = ""
    if native:
        prefix_action = "none"
    elif src_prefix is None or src_prefix.is_symlink() or not src_prefix.is_dir() or not _has_drive_c(src_prefix):
        prefix_action = "new"
        _add(warnings, W_SYMLINK_PREFIX if src_prefix is not None and src_prefix.is_symlink() else W_NO_PREFIX)
    elif has_game and not c.in_drive_c and is_within(src_prefix, game_src):
        if _resolved(src_prefix) == _resolved(game_src):
            blockers.append(B_GAME_DIR_IS_PREFIX)
        prefix_action = "copy_saves"
        _add(warnings, W_PREFIX_IN_GAME_DIR)
    elif c.prefix_shared or _is_protected_dir(src_prefix) or is_within(root, src_prefix):
        prefix_action = "copy_saves"
        _add(warnings, W_SHARED_PREFIX)
    elif not opts.move_prefix:
        prefix_action = "copy_saves"
    else:
        prefix_action = "move"
        prefix_layout = "steam" if _steam_layout(src_prefix) else "umu"
        if prefix_layout == "steam" and _layout_conflicts(src_prefix):
            blockers.append(B_PREFIX_LAYOUT_CONFLICT)
    prefix_dst = None if native else prefixes_root / uid

    moves: list[PlannedMove] = []
    game_size = 0
    if move_game and has_game and not blockers:
        game_size = dir_size_bytes(game_src)
        moves.append(PlannedMove("game", game_src, game_dst, game_size, _dev(game_src) == _dev(game_dst)))
    if prefix_action == "move" and src_prefix is not None and prefix_dst is not None:
        size = dir_size_bytes(src_prefix) - (game_size if c.in_drive_c else 0)
        moves.append(PlannedMove("prefix", src_prefix, prefix_dst, max(size, 0), _dev(src_prefix) == _dev(prefix_dst)))

    free, enough = _adoption_space(moves)
    if not enough:
        _add(blockers, B_NO_SPACE)

    if c.shortcut_appid is not None and move_game and not opts.remove_from_steam:
        _add(warnings, W_STEAM_SHORTCUT_BREAKS)
    if c.kind not in (STEAM, FOLDER) and (move_game or prefix_action == "move"):
        _add(warnings, W_LAUNCHER_ENTRY_BREAKS)

    if opts.proton_name is not None:
        proton = "" if native else opts.proton_name.strip()
    elif native or not c.proton_hint:
        proton = ""
    elif _proton_installed(c.proton_hint):
        proton = c.proton_hint
    else:
        proton = ""
        _add(warnings, W_RUNNER_NOT_INSTALLED)

    return AdoptionPlan(
        candidate=c,
        options=opts,
        uid=uid,
        name=name,
        library_root=root,
        prefixes_root=prefixes_root,
        prefix_location=prefix_location,
        game_dir=game_dst,
        exe=exe_dst,
        prefix_dir=prefix_dst,
        prefix_action=prefix_action,
        prefix_layout=prefix_layout,
        proton_name=proton,
        native_linux=native,
        moves=moves,
        bytes_to_copy=sum(m.size_bytes for m in moves if not m.same_fs),
        free_bytes=free,
        warnings=warnings,
        blockers=blockers,
    )


# --- execution --------------------------------------------------------------------------------


def _move_tree(src: Path, dst: Path) -> None:
    """Move a folder to a new location, staging the copy when crossing disks."""
    if src.is_symlink() or not src.is_dir():
        raise FileNotFoundError(f"Not a folder: {src}")
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"Target already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    move_dir_safely(src, dst)


def _move_entry(src: Path, dst: Path) -> None:
    """Move one file, symlink or folder (symlinks are moved as links, never followed)."""
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"Target already exists: {dst}")
    if src.is_dir() and not src.is_symlink():
        move_dir_safely(src, dst)
        return
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if src.is_symlink():
        dst.symlink_to(os.readlink(src))
    else:
        atomic_copy_file(src, dst)
    src.unlink()


class _Undo:
    """Undo steps, run in reverse order on failure."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], None]]] = []

    def push(self, label: str, fn: Callable[[], None]) -> None:
        self.steps.append((label, fn))

    def run(self) -> list[str]:
        problems: list[str] = []
        for label, fn in reversed(self.steps):
            try:
                fn()
            except Exception as exc:
                logging.warning("Adoption undo step %r failed: %s", label, exc)
                problems.append(f"{label}: {exc}")
        self.steps.clear()
        return problems


def _move_back(src: Path, dst: Path) -> Callable[[], None]:
    """Undo for a move src -> dst."""

    def fn() -> None:
        if not dst.exists() and not dst.is_symlink():
            raise FileNotFoundError(f"missing {dst} (expected to move back to {src})")
        if src.exists() or src.is_symlink():
            raise FileExistsError(f"{src} already exists; left {dst} in place")
        src.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_dir() and not dst.is_symlink():
            _move_tree(dst, src)
        else:
            _move_entry(dst, src)

    return fn


def _move_prefix(plan: AdoptionPlan, src: Path, dst: Path, undo: _Undo) -> None:
    """Move the prefix to dst; Steam's compatdata/<appid>/{pfx,version,...} becomes dst/{drive_c,version,...}."""
    if plan.prefix_layout != "steam":
        _move_tree(src, dst)
        undo.push(f"move {dst} back", _move_back(src, dst))
        return
    _move_tree(src / "pfx", dst)
    undo.push(f"move {dst} back", _move_back(src / "pfx", dst))
    for item in sorted(src.iterdir()):
        target = dst / item.name
        _move_entry(item, target)
        undo.push(f"move {target} back", _move_back(item, target))
    src.rmdir()
    undo.push(f"recreate {src}", lambda: src.mkdir(exist_ok=False))


def _track_install_file(undo: _Undo, path: Path) -> None:
    """Restore integration files if registration fails, including a replaced symbolic link."""
    if path.is_symlink():
        target = path.readlink()

        def restore_link() -> None:
            if path.is_symlink() and path.readlink() == target:
                return
            path.unlink(missing_ok=True)
            path.symlink_to(target)

        undo.push(f"restore {path}", restore_link)
    elif path.is_file():
        data, mode = path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
        undo.push(f"restore {path}", lambda: atomic_write_bytes(path, data, mode=mode))
    elif not path.exists():
        undo.push(f"remove {path}", lambda: path.unlink(missing_ok=True))


def _source_meta(plan: AdoptionPlan) -> dict[str, Any]:
    c = plan.candidate
    meta: dict[str, Any] = {"kind": "adopted", "origin": c.kind}
    store = _UMU_STORES.get(c.store, c.store)
    if store and store != "steam":
        meta["store"] = store
        if c.game_id:
            meta["codename"] = c.game_id
    if c.game_id:
        meta["launcher_id"] = c.game_id
    if c.shortcut_appid is not None and not plan.options.remove_from_steam:
        meta["shortcut_appid"] = c.shortcut_appid
    if c.game_dir is not None:
        meta["original_game_dir"] = encode_path(c.game_dir, None)
    if c.prefix_dir is not None:
        meta["original_prefix"] = encode_path(c.prefix_dir, None)
    meta["prefix_action"] = plan.prefix_action
    return meta


def _default_snapshot(prefix: Path, name: str, reason: str, key_prefix: str) -> Path | None:
    return save_sync.auto_snapshot(prefix, name, reason, key_prefix)


def adopt(
    plan: AdoptionPlan,
    progress_cb: ProgressFn | None = None,
    snapshot_fn: SnapshotFn | None = None,
    paths: SteamPaths | None = None,
) -> AdoptionResult:
    """Carry out plan (see the module docstring). Raises AdoptionError after undoing everything.

    paths is needed for Steam shortcuts (compatdata link, removing the shortcut).
    """
    if not plan.ok:
        raise AdoptionError(f"Adoption blocked: {', '.join(plan.blockers)}")
    if not _adoption_space(plan.moves)[1]:
        raise AdoptionError(f"Adoption blocked: {B_NO_SPACE}")
    c = plan.candidate
    opts = plan.options

    def progress(step: str) -> None:
        if progress_cb is not None:
            progress_cb(step)

    # 1. Saves snapshot before anything moves (nothing is touched if it fails).
    snapshot: Path | None = None
    src_prefix = c.prefix_dir
    if src_prefix is not None and _is_real_dir(src_prefix) and _has_drive_c(src_prefix):
        progress(STEP_SNAPSHOT)
        try:
            snapshot = (snapshot_fn or _default_snapshot)(src_prefix, plan.name, "adopt", "adopt")
        except Exception as exc:
            raise AdoptionError(f"Save snapshot failed, nothing was changed: {exc}") from exc

    undo = _Undo()
    result_warnings = list(plan.warnings)
    try:
        internal = plan.library_root == _lib.internal_library_root()
        lib = _lib.ensure_library(plan.library_root, internal=internal)
        if plan.prefix_location == "internal":
            _lib.ensure_library(_lib.internal_library_root(), internal=True)

        # 2. Game folder (first, so a game inside drive_c leaves the prefix before it moves).
        game_move = next((m for m in plan.moves if m.what == "game"), None)
        icon_path = c.icon
        if game_move is not None:
            if icon_path is not None and is_within(icon_path, game_move.src, strict=True):
                icon_path = game_move.dst / _resolved(icon_path).relative_to(_resolved(game_move.src))
            progress(STEP_MOVE_GAME)
            _move_tree(game_move.src, game_move.dst)
            undo.push(f"move {game_move.dst} back", _move_back(game_move.src, game_move.dst))

        # 3. Prefix: moved (never copied) to Prefixes/<uid>.
        if plan.prefix_action == "move" and src_prefix is not None and plan.prefix_dir is not None:
            progress(STEP_MOVE_PREFIX)
            _move_prefix(plan, src_prefix, plan.prefix_dir, undo)

        # 4. launch.sh, .desktop, v2 manifest, library registration.
        progress(STEP_LAUNCHER)
        launch_sh = plan.game_dir / "launch.sh"
        backup_path = _foreign_launch_backup_path(launch_sh)
        if backup_path is None:
            _track_install_file(undo, launch_sh)
        else:
            def restore_launch() -> None:
                if backup_path.is_file() and not backup_path.is_symlink():
                    os.replace(backup_path, launch_sh)

            undo.push(f"restore {launch_sh}", restore_launch)
        for manifest_name in (MANIFEST_V1, MANIFEST_V2):
            _track_install_file(undo, plan.game_dir / manifest_name)
        index_path = _library_index_path(plan.game_dir)
        if not index_path.parent.is_symlink():
            _track_install_file(undo, index_path)
        if opts.create_menu_shortcut:
            _track_install_file(undo, _resolve_desktop_file_path(plan.name, plan.game_dir))
        if opts.create_desktop_shortcut:
            desktop_path = _resolve_desktop_shortcut_path(sanitize_dir_name(plan.name), plan.game_dir)
            _track_install_file(undo, desktop_path)
        icon_extensions = {".ico", ".jpg"}
        if icon_path is not None:
            icon_extensions.add(icon_path.suffix.lower())
        for ext in icon_extensions:
            _track_install_file(undo, plan.game_dir / f".deckinstall_icon{ext}")
        cache_root = lib.caches_dir if lib.unix_fs else _lib.internal_library_root() / _lib.CACHES
        for folder, root in ((plan.prefix_dir, plan.prefixes_root), (cache_root / plan.uid, cache_root)):
            if folder is not None and not folder.exists() and is_within(folder, root, strict=True):
                undo.push(f"remove {folder}", partial(remove_path_or_tree, folder))
        if (game_library_root := _lib.find_library_root(plan.game_dir)) is not None:
            undo.push(f"forget {plan.uid}", lambda: _lib.forget_game(game_library_root, plan.uid))

        game = install_standalone_game(
            game_name=plan.name,
            exe_path=plan.exe,
            game_dir=plan.game_dir,
            proton_name=plan.proton_name,
            steam_appid=c.steam_appid,
            winedlloverrides=c.winedlloverrides,
            launch_options=c.launch_options,
            icon_path=icon_path,
            create_menu_shortcut=opts.create_menu_shortcut,
            create_desktop_shortcut=opts.create_desktop_shortcut,
            keep_in_place=game_move is None,
            native_linux=plan.native_linux,
            source=_source_meta(plan),
            library=lib,
            uid=plan.uid,
        )

        if plan.prefix_dir is not None and game.prefix_dir != plan.prefix_dir:
            raise RuntimeError(f"Unexpected prefix {game.prefix_dir} (planned {plan.prefix_dir})")

        # 5. Saves from prefixes that stay where they are (including secondary merged prefixes).
        saves_copied = 0
        seen_save_srcs: set[Path] = set()
        if plan.prefix_action == "copy_saves" and src_prefix is not None:
            progress(STEP_SAVES)
            seen_save_srcs.add(_resolved(src_prefix))
            saves_copied += import_prefix_saves(src_prefix, game.prefix_dir, game.name)
        if not plan.native_linux and game.prefix_dir is not None:
            for extra_pfx in c.extra_save_prefixes:
                if not _is_real_dir(extra_pfx) or not _has_drive_c(extra_pfx):
                    continue
                res_extra = _resolved(extra_pfx)
                if res_extra in seen_save_srcs or res_extra == _resolved(game.prefix_dir):
                    continue
                seen_save_srcs.add(res_extra)
                progress(STEP_SAVES)
                saves_copied += import_prefix_saves(extra_pfx, game.prefix_dir, game.name)

        # 6. Steam: keep the shortcut on the same prefix, or remove it (last step, never undone).
        steam_link: Path | None = None
        removed = False
        remove_deferred = False
        shortcut_needs_repoint = (
            c.shortcut_appid is not None
            and not opts.remove_from_steam
            and (game_move is not None or plan.exe != c.exe)
        )
        if c.shortcut_appid is not None and paths is not None:
            progress(STEP_STEAM)
            if not opts.remove_from_steam:
                compat = paths.compatdata_dir / str(c.shortcut_appid)
                compat_points_elsewhere = False
                if compat.is_symlink() and compat.exists():
                    try:
                        compat_points_elsewhere = compat.resolve() != game.prefix_dir.resolve()
                    except OSError:
                        compat_points_elsewhere = True
                can_link_compat = (
                    opts.link_steam_compatdata
                    and opts.move_prefix
                    and plan.prefix_action in ("move", "new")
                    and not c.prefix_shared
                    and not plan.native_linux
                    and not compat_points_elsewhere
                    and not (
                        plan.prefix_action == "new"
                        and compat.is_dir()
                        and not compat.is_symlink()
                        and _has_drive_c(compat)
                    )
                )
                if can_link_compat:
                    pfx_link = game.prefix_dir / "pfx"
                    had_pfx = pfx_link.exists() or pfx_link.is_symlink()
                    backup = link_compatdata_to_prefix(compat, game.prefix_dir)

                    def undo_link() -> None:
                        if compat.is_symlink():
                            compat.unlink()
                        if not had_pfx and pfx_link.is_symlink():
                            pfx_link.unlink()
                        if backup is not None and not compat.exists() and backup.is_dir():
                            os.replace(backup, compat)

                    undo.push(f"unlink {compat}", undo_link)
                    steam_meta: dict[str, Any] = {
                        "user_id": paths.user_id,
                        "shortcut_appid": c.shortcut_appid,
                        "compatdata_link": encode_path(compat, None),
                    }
                    if plan.proton_name:
                        steam_meta["compat_tool"] = plan.proton_name
                    if backup is not None:
                        steam_meta["compatdata_backup"] = encode_path(backup, None)
                    game.meta["steam"] = steam_meta
                    _write_manifest(game)
                    steam_link = compat
                elif not game.meta.get("steam"):
                    steam_meta = {
                        "user_id": paths.user_id,
                        "shortcut_appid": c.shortcut_appid,
                    }
                    if plan.proton_name:
                        steam_meta["compat_tool"] = plan.proton_name
                    game.meta["steam"] = steam_meta
                    _write_manifest(game)
            if opts.remove_from_steam:
                if is_steam_running() or is_gamescope_session():
                    remove_deferred = True
                else:
                    if not remove_shortcut(paths, c.shortcut_appid):
                        raise RuntimeError(f"Shortcut {c.shortcut_appid} could not be removed from Steam.")
                    removed = True
    except Exception as exc:
        logging.exception("Adoption of %s failed; undoing it", plan.name)
        problems = undo.run()
        msg = str(exc) if not problems else f"{exc} (undo problems: {'; '.join(problems)})"
        raise AdoptionError(msg, rolled_back=not problems, problems=problems) from exc

    progress(STEP_DONE)
    return AdoptionResult(
        game=game,
        snapshot=snapshot,
        saves_copied=saves_copied,
        steam_link=steam_link,
        removed_from_steam=removed,
        warnings=result_warnings,
        add_to_steam=opts.add_to_steam,
        shortcut_needs_repoint=shortcut_needs_repoint,
        shortcut_appid=c.shortcut_appid,
        remove_shortcut_deferred=remove_deferred,
    )
