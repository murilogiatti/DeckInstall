"""Game libraries per disk: <disk>/DeckInstall/{Games,Prefixes,Caches} + library.json.

The internal library lives in ~/DeckInstall. Each removable disk (microSD, USB) mounted under
/run/media/$USER, /media or /mnt can hold its own library. A registry in the app data dir
remembers every library seen, so games on a disk that is not mounted are listed as unavailable.

Wine prefixes need a filesystem with Unix permissions and symlinks; on exFAT/FAT/NTFS disks
the game stays on the disk but its prefix and cache go to the internal library.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text, file_lock, is_within
from .manifest import new_uid, now_iso

LIBRARY_DIRNAME = "DeckInstall"
LIBRARY_FILE = "library.json"
GAMES = "Games"
PREFIXES = "Prefixes"
CACHES = "Caches"
NON_UNIX_FS = frozenset({"exfat", "vfat", "fat", "msdos", "ntfs", "ntfs3", "fuseblk"})
_MAX_JSON_BYTES = 4 * 1024 * 1024

INTERNAL_ROOT: Path | None = None  # tests may override


@dataclass(frozen=True)
class Library:
    """A DeckInstall library root (the DeckInstall folder itself)."""

    id: str
    root: Path
    label: str
    internal: bool
    available: bool
    fs_type: str = ""

    @property
    def games_dir(self) -> Path:
        return self.root / GAMES

    @property
    def prefixes_dir(self) -> Path:
        return self.root / PREFIXES

    @property
    def caches_dir(self) -> Path:
        return self.root / CACHES

    @property
    def unix_fs(self) -> bool:
        """False for exFAT/FAT/NTFS, where Wine prefixes cannot live."""
        return self.fs_type.lower() not in NON_UNIX_FS


def internal_library_root() -> Path:
    """Return ~/DeckInstall (or DECKINSTALL_LIBRARY_DIR / INTERNAL_ROOT when set)."""
    if INTERNAL_ROOT is not None:
        return INTERNAL_ROOT
    env_dir = os.environ.get("DECKINSTALL_LIBRARY_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / LIBRARY_DIRNAME


def registry_path() -> Path:
    """Return the path of the registry of known libraries."""
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "DeckInstall" / "libraries.json"


def _unescape_mount(field: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def read_mount_table(mounts_file: Path = Path("/proc/self/mounts")) -> list[tuple[Path, str]]:
    """Return (mount point, fs type) pairs from the kernel mount table."""
    try:
        text = mounts_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    table: list[tuple[Path, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            table.append((Path(_unescape_mount(parts[1])), parts[2]))
    return table


def fs_type_of(path: Path, table: list[tuple[Path, str]] | None = None) -> str:
    """Return the filesystem type holding path (longest matching mount point), or ''."""
    entries = table if table is not None else read_mount_table()
    try:
        target = path.resolve()
    except OSError:
        target = path
    best: tuple[int, str] = (-1, "")
    for mount, fstype in entries:
        if target == mount or mount in target.parents:
            depth = len(mount.parts)
            if depth >= best[0]:
                best = (depth, fstype)
    return best[1]


def default_mount_search_roots() -> list[Path]:
    """Return the folders whose children are removable-disk mount points."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "")
    roots = [
        Path("/run/media") / user,
        Path("/run/media"),
        Path("/media") / user,
        Path("/media"),
        Path("/mnt"),
        Path("/var/mnt"),
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r.name and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _is_mount(path: Path) -> bool:
    return os.path.ismount(path)


def list_mount_points(search_roots: list[Path] | None = None) -> list[Path]:
    """Return mounted removable disks (direct children of the search roots that are mount points)."""
    found: list[Path] = []
    seen_roots: set[Path] = set()
    seen_children: set[Path] = set()
    for root in search_roots if search_roots is not None else default_mount_search_roots():
        if not root.is_dir():
            continue
        try:
            rroot = root.resolve()
        except OSError:
            rroot = root
        if rroot in seen_roots:
            continue
        seen_roots.add(rroot)
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                rchild = child.resolve()
            except OSError:
                rchild = child
            if rchild in seen_children:
                continue
            if not _is_mount(child):
                continue
            seen_children.add(rchild)
            found.append(child)
    return found


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_library_file(root: Path) -> dict[str, Any] | None:
    """Read <root>/library.json, or None when root is not a library."""
    data = _read_json(root / LIBRARY_FILE)
    if data is None or not isinstance(data.get("id"), str) or not data["id"]:
        return None
    return data


def _library_lock(root: Path):
    return file_lock(root / ".library.lock")


def _registry_lock():
    return file_lock(registry_path().with_suffix(".lock"))


def ensure_library(root: Path, label: str = "", internal: bool = False) -> Library:
    """Create (or open) the library at root with its Games/Prefixes/Caches folders."""
    if root.is_symlink():
        raise OSError(f"Library folder cannot be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    with _library_lock(root):
        lib_file = root / LIBRARY_FILE
        data = read_library_file(root)
        if data is None:
            if lib_file.exists() or lib_file.is_symlink():
                raise OSError(f"Library file is unreadable or corrupt: {lib_file}")
            data = {
                "schema": 1,
                "id": new_uid(),
                "label": label or ("Internal" if internal else root.parent.name),
                "created_at": now_iso(),
                "games": [],
            }
            _write_json(lib_file, data)
    fstype = fs_type_of(root)
    lib = Library(
        id=str(data["id"]),
        root=root,
        label=str(data.get("label") or root.parent.name),
        internal=internal,
        available=True,
        fs_type=fstype,
    )
    for sub in (GAMES, PREFIXES, CACHES) if lib.unix_fs else (GAMES,):
        (root / sub).mkdir(exist_ok=True)
    _remember(lib, data)
    return lib


def _load_registry() -> dict[str, Any]:
    data = _read_json(registry_path()) or {}
    libs = data.get("libraries")
    return {"schema": 1, "libraries": libs if isinstance(libs, dict) else {}}


def _remember(lib: Library, data: dict[str, Any]) -> None:
    """Store/refresh a library (and its game list) in the registry."""
    try:
        with _registry_lock():
            reg = _load_registry()
            games = data.get("games") if isinstance(data.get("games"), list) else []
            entry = {
                "root": str(lib.root),
                "label": lib.label,
                "internal": lib.internal,
                "games": games,
            }
            if reg["libraries"].get(lib.id) == entry:
                return
            reg["libraries"][lib.id] = entry
            _write_json(registry_path(), reg)
    except OSError as exc:
        logging.warning("Could not update library registry: %s", exc)


def discover_libraries(search_roots: list[Path] | None = None) -> list[Library]:
    """Return the internal library, libraries on mounted disks, and known unmounted ones."""
    table = read_mount_table()
    libs: list[Library] = []
    seen_ids: set[str] = set()

    internal_root = internal_library_root()
    internal_data = read_library_file(internal_root)
    if internal_data is not None:
        lib = Library(
            id=str(internal_data["id"]),
            root=internal_root,
            label=str(internal_data.get("label") or "Internal"),
            internal=True,
            available=True,
            fs_type=fs_type_of(internal_root, table),
        )
        _remember(lib, internal_data)
        libs.append(lib)
        seen_ids.add(lib.id)

    for mount in list_mount_points(search_roots):
        root = mount / LIBRARY_DIRNAME
        data = read_library_file(root)
        if data is None or str(data["id"]) in seen_ids:
            continue
        lib = Library(
            id=str(data["id"]),
            root=root,
            label=str(data.get("label") or mount.name),
            internal=False,
            available=True,
            fs_type=fs_type_of(root, table),
        )
        _remember(lib, data)
        libs.append(lib)
        seen_ids.add(lib.id)

    for lib_id, entry in _load_registry()["libraries"].items():
        if lib_id in seen_ids or not isinstance(entry, dict):
            continue
        raw_root = str(entry.get("root") or "").strip()
        if not raw_root:
            continue
        root = Path(raw_root)
        data = read_library_file(root)
        available = data is not None and str(data["id"]) == lib_id
        libs.append(
            Library(
                id=lib_id,
                root=root,
                label=str(entry.get("label") or root.parent.name),
                internal=bool(entry.get("internal", False)),
                available=available,
                fs_type=fs_type_of(root, table) if available else "",
            )
        )
    return libs


def disks_without_library(search_roots: list[Path] | None = None) -> list[Path]:
    """Return mounted disks that do not hold a DeckInstall library yet."""
    return [m for m in list_mount_points(search_roots) if read_library_file(m / LIBRARY_DIRNAME) is None]


def find_library_root(path: Path) -> Path | None:
    """Return the library root whose Games folder contains path, if any."""
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for ancestor in resolved.parents:
        if (ancestor / LIBRARY_FILE).is_file() and is_within(resolved, ancestor / GAMES, strict=True):
            return ancestor if read_library_file(ancestor) is not None else None
    return None


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def library_for_path(path: Path) -> Library | None:
    """Return the available Library holding path (its game folder), if any."""
    root = find_library_root(path)
    if root is None:
        return None
    data = read_library_file(root)
    if data is None:
        return None
    return Library(
        id=str(data["id"]),
        root=root,
        label=str(data.get("label") or root.parent.name),
        internal=_same_dir(root, internal_library_root()),
        available=True,
        fs_type=fs_type_of(root),
    )


def record_game(lib_root: Path, uid: str, name: str, game_dir: Path) -> None:
    """Add/refresh a game in the library's game list (used to show it while the disk is absent)."""
    if not uid or not lib_root.is_dir():
        return
    with _library_lock(lib_root):
        data = read_library_file(lib_root)
        if data is None:
            return
        rel = game_dir.resolve().relative_to(lib_root.resolve()).as_posix()
        games = [g for g in data.get("games") or [] if isinstance(g, dict) and g.get("id") != uid]
        games.append({"id": uid, "name": name, "dir": rel})
        games.sort(key=lambda g: str(g.get("name", "")).lower())
        data["games"] = games
        _write_json(lib_root / LIBRARY_FILE, data)
    lib = library_for_path(game_dir)
    if lib is not None:
        _remember(lib, data)


def forget_game(lib_root: Path, uid: str) -> None:
    """Remove a game from the library's game list."""
    if not uid or not lib_root.is_dir():
        return
    with _library_lock(lib_root):
        data = read_library_file(lib_root)
        if data is None:
            return
        games = [g for g in data.get("games") or [] if isinstance(g, dict) and g.get("id") != uid]
        if len(games) == len(data.get("games") or []):
            return
        data["games"] = games
        _write_json(lib_root / LIBRARY_FILE, data)
    try:
        with _registry_lock():
            reg = _load_registry()
            entry = reg["libraries"].get(str(data["id"]))
            if isinstance(entry, dict):
                entry["games"] = games
                _write_json(registry_path(), reg)
    except OSError as exc:
        logging.warning("Could not update library registry: %s", exc)


def list_unavailable_games(libraries: list[Library] | None = None) -> list[dict[str, str]]:
    """Return {id, name, library} for games whose library disk is not mounted."""
    libs = libraries if libraries is not None else discover_libraries()
    absent = {lib.id: lib for lib in libs if not lib.available}
    out: list[dict[str, str]] = []
    for lib_id, entry in _load_registry()["libraries"].items():
        lib = absent.get(lib_id)
        if lib is None or not isinstance(entry, dict):
            continue
        for g in entry.get("games") or []:
            if isinstance(g, dict) and g.get("name"):
                out.append({"id": str(g.get("id") or ""), "name": str(g["name"]), "library": lib.label})
    out.sort(key=lambda g: g["name"].lower())
    return out
