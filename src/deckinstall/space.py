"""Space meter: usage per disk (with DeckInstall's share) and per game. Read-only; run off the UI thread.

Sizes are apparent file sizes (same unit as the uninstall dialog). Symlinks are never followed,
a file with several hard links is counted once, a prefix shared by games is counted once
(on the first game, every sharer is flagged) and disks are deduplicated by st_dev.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import standalone as _sa
from . import steam_vdf as _vdf
from .library import Library, discover_libraries, find_library_root, internal_library_root, list_unavailable_games
from .standalone import StandaloneGame, list_standalone_games
from .steam_paths import SteamPaths
from .uninstall import _logs_dir

GAMES = "games"
PREFIXES = "prefixes"
CACHES = "caches"
CATEGORIES = (GAMES, PREFIXES, CACHES)
SOURCE_DECKINSTALL = "deckinstall"
SOURCE_STEAM = "steam"
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/opt", "/etc", "/var")
_SYSTEM_EXES = frozenset({"flatpak", "sh", "bash", "env"})


@dataclass
class DiskSpace:
    """One physical disk. labels: libraries on it; deckinstall: bytes per category."""

    mount: Path
    internal: bool
    total: int
    used: int
    free: int
    labels: list[str] = field(default_factory=list)
    deckinstall: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))

    @property
    def deckinstall_bytes(self) -> int:
        return sum(self.deckinstall.values())


@dataclass
class GameSpace:
    name: str
    uid: str = ""
    library: str = ""
    available: bool = True
    game_bytes: int = 0
    prefix_bytes: int = 0
    cache_bytes: int = 0
    logs_bytes: int = 0
    shared_prefix: bool = False
    source: str = SOURCE_DECKINSTALL

    @property
    def total_bytes(self) -> int:
        return self.game_bytes + self.prefix_bytes + self.cache_bytes + self.logs_bytes


@dataclass
class SpaceReport:
    disks: list[DiskSpace]
    games: list[GameSpace]
    top_folders: list[tuple[str, int]] = field(default_factory=list)


def _real(path: Path | None) -> str | None:
    """Real path of a directory that is not itself a symlink, else None (never measured)."""
    if path is None:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return os.path.realpath(path)


def _count(st: os.stat_result, seen: set[tuple[int, int]]) -> int:
    if st.st_nlink > 1:
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return 0
        seen.add(key)
    return st.st_size


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def tree_size(
    root: str,
    seen: set[tuple[int, int]],
    *,
    skip: Iterable[str] = (),
    known: dict[str, int] | None = None,
) -> int:
    """Bytes of regular files under root (a real path). Folders in skip are left out; folders in
    known add their recorded size without being walked again."""
    skip_set = {s for s in skip if s != root}
    total = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.path in skip_set:
                                continue
                            if known is not None and entry.path in known:
                                total += known[entry.path]
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += _count(entry.stat(follow_symlinks=False), seen)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _measure(
    path: str | None, seen: set[tuple[int, int]], skip: list[str], measured: dict[str, int]
) -> int:
    if path is None:
        return 0
    if path in measured:
        return measured[path]
    size = tree_size(path, seen, skip=skip)
    # Remember it for the disk pass only when nothing inside was skipped.
    if not any(s != path and _inside(s, path) for s in skip):
        measured[path] = size
    return size


def game_space(
    games: list[StandaloneGame],
    libraries: list[Library] | None = None,
    measured: dict[str, int] | None = None,
) -> list[GameSpace]:
    """Per-game usage: game files, prefix, cache and run logs. measured collects folder sizes."""
    labels = {os.path.realpath(lib.root): lib.label for lib in libraries or [] if lib.available}
    memo = measured if measured is not None else {}
    seen: set[tuple[int, int]] = set()
    owners: dict[str, list[GameSpace]] = {}
    out: list[GameSpace] = []
    for game in games:
        gd = _real(game.game_dir)
        pfx = None if game.native_linux else _real(game.prefix_dir)
        cache = _real(game.cache_dir)
        logs = _real(_logs_dir(game))
        lib_root = find_library_root(game.game_dir)
        entry = GameSpace(
            game.name,
            uid=game.uid,
            library=labels.get(os.path.realpath(lib_root), "") if lib_root is not None else "",
        )
        # A legacy prefix/cache inside the game folder is counted in its own column.
        inner = [p for p in (pfx, cache, logs) if p is not None]
        entry.game_bytes = _measure(gd, seen, inner, memo)
        if pfx is not None:
            sharers = owners.setdefault(pfx, [])
            if not sharers:
                entry.prefix_bytes = _measure(pfx, seen, [], memo)
            sharers.append(entry)
        entry.cache_bytes = _measure(cache, seen, [], memo)
        entry.logs_bytes = _measure(logs, seen, [], memo)
        out.append(entry)
    for sharers in owners.values():
        if len(sharers) > 1:
            for entry in sharers:
                entry.shared_prefix = True
    return out


def _is_system_exe(exe: str) -> bool:
    """True for launchers (flatpak, sh...) and anything living in system folders."""
    path = exe.strip().strip('"')
    return os.path.basename(path) in _SYSTEM_EXES or any(_inside(path, root) for root in _SYSTEM_ROOTS)


def steam_game_space(
    paths: SteamPaths,
    libraries: list[Library],
    known_dirs: set[str],
    measured: dict[str, int],
) -> list[GameSpace]:
    """Non-Steam shortcuts of the current Steam user that DeckInstall does not manage.

    known_dirs: real paths of folders already listed as DeckInstall games (skipped)."""
    labels = {os.path.realpath(lib.root): lib.label for lib in libraries if lib.available}
    steamapps = paths.steam_root / "steamapps"
    home = os.path.realpath(Path.home())
    seen: set[tuple[int, int]] = set()
    dirs = set(known_dirs)
    prefixes: set[str] = set()
    out: list[GameSpace] = []
    for entry in _vdf.load_shortcuts(paths.shortcuts_vdf):
        if _is_system_exe(entry.exe):
            continue
        try:
            folder = _sa.shortcut_game_dir(entry)
        except ValueError:
            continue
        gd = _real(folder)
        if gd is None or gd in dirs or gd in ("/", home) or find_library_root(folder) == folder:
            continue
        dirs.add(gd)
        pfx = _real(paths.compatdata_dir / str(entry.appid_u32))
        cache = _real(steamapps / "shadercache" / str(entry.appid_u32))
        inner = [p for p in (pfx, cache) if p is not None and _inside(p, gd)]
        lib_root = find_library_root(folder)
        item = GameSpace(
            entry.app_name,
            uid=str(entry.appid_u32),
            library=labels.get(os.path.realpath(lib_root), "") if lib_root is not None else "",
            source=SOURCE_STEAM,
        )
        item.game_bytes = _measure(gd, seen, inner, measured)
        if pfx is not None and pfx not in prefixes:
            prefixes.add(pfx)
            item.prefix_bytes = _measure(pfx, seen, [], measured)
        item.cache_bytes = _measure(cache, seen, [], measured)
        out.append(item)
    return out


def _dev(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _existing(path: Path) -> Path:
    """path, or its nearest existing ancestor."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _mount_point(path: Path) -> Path:
    cur = Path(os.path.realpath(path))
    while not os.path.ismount(cur) and cur.parent != cur:
        cur = cur.parent
    return cur


def _category_roots(libraries: list[Library]) -> dict[str, list[Path]]:
    avail = [lib for lib in libraries if lib.available]
    return {
        GAMES: [_sa.get_games_dir(), _sa.get_legacy_games_dir(), *(lib.games_dir for lib in avail)],
        PREFIXES: [_sa.get_prefixes_dir(), _sa.get_legacy_prefixes_dir(), *(lib.prefixes_dir for lib in avail)],
        CACHES: [
            _sa.get_shader_caches_dir(),
            _sa.get_legacy_games_dir() / ".caches",
            *(lib.caches_dir for lib in avail),
        ],
    }


def disk_space(libraries: list[Library], measured: dict[str, int] | None = None) -> list[DiskSpace]:
    """Usage of the internal disk and of every disk holding an available library."""
    disks: dict[int, DiskSpace] = {}

    def disk_for(path: Path, internal: bool = False) -> DiskSpace | None:
        dev = _dev(path)
        if dev is None:
            return None
        disk = disks.get(dev)
        if disk is None:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                return None
            disk = DiskSpace(_mount_point(path), internal, usage.total, usage.used, usage.free)
            disks[dev] = disk
        disk.internal = disk.internal or internal
        return disk

    disk_for(_existing(internal_library_root()), internal=True)
    for lib in libraries:
        if lib.available:
            disk = disk_for(lib.root, internal=lib.internal)
            # The internal library is shown as "Internal disk"; list the other labels.
            if disk is not None and not lib.internal and lib.label and lib.label not in disk.labels:
                disk.labels.append(lib.label)

    roots = {
        cat: [r for r in (_real(p) for p in paths) if r is not None]
        for cat, paths in _category_roots(libraries).items()
    }
    all_roots = sorted({r for rs in roots.values() for r in rs})
    seen: set[tuple[int, int]] = set()
    done: set[str] = set()
    for cat in CATEGORIES:
        for root in roots[cat]:
            if root in done:
                continue
            done.add(root)
            disk = disk_for(Path(root))
            if disk is not None:
                disk.deckinstall[cat] += tree_size(root, seen, skip=all_roots, known=measured)
    return sorted(disks.values(), key=lambda d: (not d.internal, str(d.mount)))


TOP_FOLDERS = 5


def top_folders(measured: dict[str, int], limit: int = TOP_FOLDERS) -> list[tuple[str, int]]:
    """The largest measured folders (path, bytes), biggest first; empty folders are left out."""
    ranked = sorted(((p, n) for p, n in measured.items() if n > 0), key=lambda it: (-it[1], it[0]))
    return ranked[:limit]


def scan_space(
    libraries: list[Library] | None = None,
    games: list[StandaloneGame] | None = None,
    steam_paths: SteamPaths | None = None,
) -> SpaceReport:
    """Disk and per-game usage; games on unmounted disks are listed without sizes.
    steam_paths adds the user's unmanaged Non-Steam shortcuts (source "steam")."""
    libs = libraries if libraries is not None else discover_libraries()
    game_list = games if games is not None else list_standalone_games()
    measured: dict[str, int] = {}
    per_game = game_space(game_list, libs, measured)
    if steam_paths is not None:
        known = {r for r in (_real(g.game_dir) for g in game_list) if r is not None}
        per_game.extend(steam_game_space(steam_paths, libs, known, measured))
    for g in list_unavailable_games(libs):
        per_game.append(GameSpace(g["name"], uid=g["id"], library=g["library"], available=False))
    per_game.sort(key=lambda g: g.name.lower())
    return SpaceReport(disk_space(libs, measured), per_game, top_folders(measured))

