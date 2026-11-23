"""Steam path discovery and Proton/session detection for SteamOS/Linux."""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SteamPaths:
    """Resolved Steam filesystem paths for a specific Steam user."""

    steam_root: Path
    user_id: str
    userdata_dir: Path
    shortcuts_vdf: Path
    grid_dir: Path
    config_vdf: Path
    compatdata_dir: Path

    @property
    def localconfig_vdf(self) -> Path:
        """Return the path to the user's localconfig.vdf."""
        return self.userdata_dir / "config" / "localconfig.vdf"


def dir_size_bytes(path: Path) -> int:
    """Return total byte size of regular files under path, skipping symlinks."""
    return measured_dir_size_bytes(path) or 0


def measured_dir_size_bytes(
    path: Path, *, strict: bool = False, cancelled: Callable[[], bool] | None = None,
) -> int | None:
    """Measure regular files without following symlinks. Strict scans flag unreadable trees."""
    if path.is_symlink() or not path.is_dir():
        return None
    total = 0
    stack: list[Path | str] = [path]
    while stack:
        if cancelled is not None and cancelled():
            return None
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        if strict:
                            return None
        except OSError:
            if strict:
                return None
    return total


def dir_size_bytes_many(paths: list[Path]) -> int:
    """Compute summed regular-file sizes across multiple directories using bounded parallelism."""
    unique: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_symlink() or not p.is_dir():
            continue
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
    if not unique:
        return 0
    if len(unique) == 1:
        return dir_size_bytes(unique[0])
    workers = min(8, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(dir_size_bytes, unique))


def _has_active_steam_profile(candidate: Path) -> bool:
    userdata = candidate / "userdata"
    if not userdata.is_dir():
        return False
    for entry in _safe_iterdir(userdata):
        if entry.name.isascii() and entry.name.isdigit() and entry.is_dir():
            return True
    cfg = candidate / "config"
    return (cfg / "loginusers.vdf").is_file() or (cfg / "config.vdf").is_file()


def find_steam_root() -> Path | None:
    """Return the active Steam installation directory, if present."""
    candidates = [
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".steam" / "steam",
    ]
    fallback: Path | None = None
    for candidate in candidates:
        if not (candidate / "userdata").is_dir():
            continue
        if _has_active_steam_profile(candidate):
            return candidate.resolve()
        if fallback is None:
            fallback = candidate.resolve()
    return fallback


def is_flatpak_steam(steam_root: Path | None = None) -> bool:
    """Return True if the active Steam installation is Flatpak Steam."""
    root = steam_root or find_steam_root()
    if root is None:
        return False
    return "com.valvesoftware.Steam" in root.parts


_VDF_READ_ERRORS = (
    OSError,
    UnicodeDecodeError,
    ValueError,
    KeyError,
    TypeError,
    SyntaxError,
    ImportError,
)


def _safe_iterdir(directory: Path) -> list[Path]:
    """Return directory entries, or an empty list if the directory cannot be read."""
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _parse_loginusers_priority(steam_root: Path) -> dict[str, tuple[int, int]]:
    """Return {steam3_id: (most_recent_flag, timestamp)} from config/loginusers.vdf."""
    loginusers = steam_root / "config" / "loginusers.vdf"
    if not loginusers.is_file():
        return {}
    try:
        import vdf

        data = vdf.loads(loginusers.read_text(encoding="utf-8", errors="replace"))
    except _VDF_READ_ERRORS:
        return {}
    users_map = data.get("users")
    if not isinstance(users_map, dict):
        return {}
    priority: dict[str, tuple[int, int]] = {}
    for steam64_str, info in users_map.items():
        s64 = str(steam64_str)
        if not isinstance(info, dict) or not (s64.isascii() and s64.isdigit()):
            continue
        account_id = int(s64) - 76561197960265728
        if account_id <= 0:
            continue
        most_recent = 1 if str(info.get("MostRecent", "0")) == "1" else 0
        ts_str = str(info.get("Timestamp", "0"))
        ts = int(ts_str) if (ts_str.isascii() and ts_str.isdigit()) else 0
        priority[str(account_id)] = (most_recent, ts)
    return priority


def list_steam_users(steam_root: Path) -> list[str]:
    """Return numeric Steam3 userdata IDs, prioritizing loginusers.vdf MostRecent/Timestamp."""
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return []
    users = [
        p
        for p in _safe_iterdir(userdata)
        if p.is_dir() and p.name.isascii() and p.name.isdigit() and p.name != "0"
    ]
    login_prio = _parse_loginusers_priority(steam_root)

    def _sort_key(p: Path) -> tuple[int, int, float]:
        mr, ts = login_prio.get(p.name, (0, 0))
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (mr, ts, mtime)

    users.sort(key=_sort_key, reverse=True)
    return [u.name for u in users]


def resolve_steam_paths(user_id: str | None = None) -> SteamPaths | None:
    """Resolve SteamPaths for the given user_id or the most recent active user."""
    steam_root = find_steam_root()
    if steam_root is None:
        return None
    users = list_steam_users(steam_root)
    if not users:
        return None
    chosen = user_id if (user_id and user_id in users) else users[0]
    userdata_dir = steam_root / "userdata" / chosen
    return SteamPaths(
        steam_root=steam_root,
        user_id=chosen,
        userdata_dir=userdata_dir,
        shortcuts_vdf=userdata_dir / "config" / "shortcuts.vdf",
        grid_dir=userdata_dir / "config" / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )


def read_library_paths(steam_root: Path) -> list[Path]:
    """Return raw library paths declared in steamapps/libraryfolders.vdf (not filtered or deduplicated)."""
    lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not lib_vdf.is_file():
        return []
    try:
        import vdf

        data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
    except _VDF_READ_ERRORS:
        return []
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    if not isinstance(folders, dict):
        return []
    out: list[Path] = []
    for val in folders.values():
        raw_path = val.get("path") if isinstance(val, dict) else (val if isinstance(val, str) else None)
        if raw_path:
            out.append(Path(str(raw_path)))
    return out


def _list_steam_library_roots(steam_root: Path) -> list[Path]:
    """Return all Steam library root directories (main steam_root + libraryfolders.vdf)."""
    roots: list[Path] = [steam_root]
    seen: set[Path] = set()
    try:
        seen.add(steam_root.resolve())
    except OSError:
        seen.add(steam_root)
    for cand in read_library_paths(steam_root):
        try:
            res = cand.resolve()
        except OSError:
            res = cand
        if cand.is_dir() and res not in seen:
            seen.add(res)
            roots.append(cand)
    return roots


def _read_compat_tool_id(vdf_path: Path, fallback: str) -> str:
    """Read internal tool identifier from compatibilitytool.vdf when available."""
    try:
        import vdf

        data = vdf.loads(vdf_path.read_text(encoding="utf-8", errors="replace"))
        tools = (
            data.get("compatibilitytools", {}).get("compat_tools")
            or data.get("compat_tools")
        )
        if isinstance(tools, dict) and tools:
            first_key = next(iter(tools.keys()))
            if isinstance(first_key, str) and first_key.strip():
                return first_key.strip()
    except _VDF_READ_ERRORS:
        pass
    return fallback


def _iter_compat_tool_dirs(steam_root: Path) -> list[Path]:
    """Return deduplicated existing compatibilitytools.d directories."""
    compat_dirs = [
        steam_root / "compatibilitytools.d",
        Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d",
        Path.home() / ".steam" / "root" / "compatibilitytools.d",
        (
            Path.home()
            / ".var"
            / "app"
            / "com.valvesoftware.Steam"
            / ".local"
            / "share"
            / "Steam"
            / "compatibilitytools.d"
        ),
    ]
    out: list[Path] = []
    visited: set[Path] = set()
    for compat_tools in compat_dirs:
        if not compat_tools.is_dir():
            continue
        try:
            res_dir = compat_tools.resolve()
        except OSError:
            res_dir = compat_tools
        if res_dir in visited:
            continue
        visited.add(res_dir)
        out.append(compat_tools)
    return out


def _proton_version_key(entry: Path) -> tuple[tuple[int, ...], str]:
    nums = tuple(int(n) for n in re.findall(r"\d+", entry.name))
    return (nums, entry.name.lower())


def find_default_proton_name(steam_root: Path) -> str:
    """Return the Steam CompatToolMapping name for the newest stable Proton."""
    libs = _list_steam_library_roots(steam_root)
    for candidate_dir, tool_name in [
        ("Proton 11.0", "proton_11"),
        ("Proton 10.0", "proton_10"),
        ("Proton 9.0", "proton_9"),
        ("Proton 9.0 (Beta)", "proton_9"),
    ]:
        for lib in libs:
            if (lib / "steamapps" / "common" / candidate_dir / "proton").is_file():
                return tool_name
    for lib in libs:
        common = lib / "steamapps" / "common"
        if common.is_dir():
            for entry in sorted(common.glob("Proton [0-9]*"), key=_proton_version_key, reverse=True):
                if (entry / "proton").is_file():
                    m = re.match(r"Proton\s+(\d+)", entry.name)
                    if m:
                        return f"proton_{m.group(1)}"
    for lib in libs:
        if (lib / "steamapps" / "common" / "Proton - Experimental" / "proton").is_file():
            return "proton_experimental"
    for compat_tools in _iter_compat_tool_dirs(steam_root):
        for entry in sorted(_safe_iterdir(compat_tools), key=_proton_version_key, reverse=True):
            vdf_file = entry / "compatibilitytool.vdf"
            if entry.is_dir() and vdf_file.is_file() and (entry / "proton").is_file():
                return _read_compat_tool_id(vdf_file, entry.name)
    installed = list_installed_protons(steam_root)
    if installed:
        return installed[0][0]
    return "proton_experimental"


def find_proton_binary(steam_root: Path) -> Path | None:
    """Locate an installed stable Proton executable for running Windows setup.exe installers."""
    libs = _list_steam_library_roots(steam_root)
    for name in ("Proton 11.0", "Proton 10.0", "Proton 9.0", "Proton - Experimental"):
        for lib in libs:
            proton = lib / "steamapps" / "common" / name / "proton"
            if proton.is_file():
                return proton
    for lib in libs:
        common = lib / "steamapps" / "common"
        if common.is_dir():
            for entry in sorted(common.glob("Proton*"), key=_proton_version_key, reverse=True):
                proton = entry / "proton"
                if proton.is_file():
                    return proton
    for compat_tools in _iter_compat_tool_dirs(steam_root):
        for entry in sorted(_safe_iterdir(compat_tools), key=_proton_version_key, reverse=True):
            proton = entry / "proton"
            if entry.is_dir() and proton.is_file():
                return proton
    return None


def resolve_compat_tool_dir(name: str, compat_dirs: Iterable[Path]) -> Path | None:
    """Find a custom Proton by folder name or by its compatibilitytool.vdf internal id."""
    seen: set[Path] = set()
    for compat_dir in compat_dirs:
        if not compat_dir.is_dir():
            continue
        try:
            res_dir = compat_dir.resolve()
        except OSError:
            res_dir = compat_dir
        if res_dir in seen:
            continue
        seen.add(res_dir)
        direct = compat_dir / name
        if direct.is_dir() and (direct / "proton").is_file():
            return direct
        for entry in _safe_iterdir(compat_dir):
            if not entry.is_dir() or not (entry / "proton").is_file():
                continue
            vdf_file = entry / "compatibilitytool.vdf"
            if vdf_file.is_file() and _read_compat_tool_id(vdf_file, entry.name) == name:
                return entry
    return None


def resolve_builtin_proton(name: str, steam_root: Path) -> Path | None:
    """Resolve a built-in Steam Proton tool name (proton_9, proton_experimental...) to its directory."""
    cleaned = name.strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    candidates: list[str] = [cleaned]
    if low == "proton_experimental":
        candidates.append("Proton - Experimental")
    elif low == "proton_hotfix":
        candidates.append("Proton - Hotfix")
    else:
        m = re.fullmatch(r"proton_(\d+)", low)
        if m:
            candidates.extend([f"Proton {m.group(1)}.0", f"Proton {m.group(1)}"])
    for lib in _list_steam_library_roots(steam_root):
        common_dir = lib / "steamapps" / "common"
        if not common_dir.is_dir():
            continue
        for cand_name in candidates:
            cand = common_dir / cand_name
            if cand.is_dir() and (cand / "proton").is_file():
                return cand
    return None


def resolve_proton_dir(
    name: str,
    steam_root: Path | None = None,
    extra_compat_dirs: Iterable[Path] = (),
) -> Path | None:
    """Resolve a Proton name or path to its directory: explicit path, custom tools, then built-ins."""
    cleaned = name.strip()
    if not cleaned:
        return None
    as_path = Path(cleaned)
    if as_path.is_dir() and (as_path / "proton").is_file():
        return as_path
    root = steam_root if steam_root is not None else find_steam_root()
    compat_dirs = list(extra_compat_dirs)
    if root is not None:
        compat_dirs.extend(_iter_compat_tool_dirs(root))
    found = resolve_compat_tool_dir(cleaned, compat_dirs)
    if found is not None:
        return found
    return resolve_builtin_proton(cleaned, root) if root is not None else None


def list_installed_protons(steam_root: Path) -> list[tuple[str, str]]:
    """Return list of (internal_tool_name, display_label) for installed Proton/GE-Proton versions."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for compat_tools in _iter_compat_tool_dirs(steam_root):
        for entry in sorted(_safe_iterdir(compat_tools), key=_proton_version_key, reverse=True):
            vdf_file = entry / "compatibilitytool.vdf"
            if entry.is_dir() and vdf_file.is_file():
                tool_id = _read_compat_tool_id(vdf_file, entry.name)
                if tool_id not in seen:
                    seen.add(tool_id)
                    label = (
                        f"{entry.name} (Custom / GE-Proton)"
                        if tool_id == entry.name
                        else f"{entry.name} ({tool_id})"
                    )
                    found.append((tool_id, label))

    for lib in _list_steam_library_roots(steam_root):
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        for entry in sorted(common.glob("Proton*"), key=_proton_version_key, reverse=True):
            if not (entry / "proton").is_file():
                continue
            folder = entry.name
            if "Experimental" in folder:
                tool_id = "proton_experimental"
            elif "Hotfix" in folder:
                tool_id = "proton_hotfix"
            else:
                m = re.match(r"Proton\s+(\d+)", folder)
                tool_id = f"proton_{m.group(1)}" if m else folder.lower().replace(" ", "_")
            if tool_id not in seen:
                seen.add(tool_id)
                found.append((tool_id, f"{folder} ({tool_id})"))

    return found


def _env_is_gamescope(env: dict[str, str] | os._Environ[str]) -> bool:
    return (
        "gamescope" in env.get("XDG_CURRENT_DESKTOP", "").lower()
        or bool(env.get("GAMESCOPE_WAYLAND_DISPLAY"))
        or env.get("SteamGamepadUI") == "1"
    )


def configure_qt_environment(env: dict[str, str] | os._Environ[str]) -> None:
    """Pick safe Qt platform settings before QApplication exists (called from main.py).

    The AppImage's Qt crashed (SIGSEGV in libqwayland / the portal theme) on the Deck. So:
    - Gaming Mode (gamescope): never load the xdg-desktop-portal theme (no portal there) and prefer
      XWayland (xcb) over gamescope's Wayland socket, falling back to wayland if xcb is missing.
    - Desktop: use the portal theme only when a D-Bus session exists (needed for the file chooser).
    Explicit user settings always win.
    """
    if _env_is_gamescope(env):
        if env.get("QT_QPA_PLATFORMTHEME") == "xdgdesktopportal" and env.get("DECKINSTALL_KEEP_PORTAL") != "1":
            env.pop("QT_QPA_PLATFORMTHEME", None)
        if not env.get("QT_QPA_PLATFORM") and env.get("DISPLAY"):
            env["QT_QPA_PLATFORM"] = "xcb;wayland"
        return
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        env.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")


_PROC_ROOT = Path("/proc")
_PROCESS_CACHE_SECONDS = 2.0
_PROCESS_CACHE_LOCK = threading.Lock()
_process_cache: dict[str, tuple[float, bool]] = {}
# /proc/<pid>/comm keeps only 15 characters: "gamescope-session" and "gamescope-session-plus"
# (SteamOS, Bazzite) both show up as "gamescope-sessi".
_SESSION_COMM_PREFIX = "gamescope-sessi"


def _read_processes(proc_root: Path | None = None) -> dict[int, tuple[str, int]]:
    """Map pid -> (comm, ppid) for the current user's processes, read straight from /proc."""
    root = proc_root if proc_root is not None else _PROC_ROOT
    uid = str(os.geteuid())
    procs: dict[int, tuple[str, int]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return procs
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = Path(entry.path, "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, ppid, owner = "", 0, ""
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key == "Name":
                name = value.strip()
            elif key == "PPid":
                ppid = int(value.strip() or 0)
            elif key == "Uid":
                fields = value.split()
                owner = fields[1] if len(fields) > 1 else (fields[0] if fields else "")
        if owner == uid and name:
            procs[int(entry.name)] = (name, ppid)
    return procs


def _gamescope_from_processes(procs: dict[int, tuple[str, int]]) -> bool:
    """Gaming Mode: a gamescope session process exists, or Steam runs under gamescope."""
    if any(comm.startswith(_SESSION_COMM_PREFIX) for comm, _ in procs.values()):
        return True
    for pid, (comm, _) in procs.items():
        if comm != "steam":
            continue
        seen: set[int] = set()
        cur = procs.get(pid, ("", 0))[1]
        while cur > 1 and cur not in seen and cur in procs:
            seen.add(cur)
            parent_comm, parent_ppid = procs[cur]
            if parent_comm == "gamescope":
                return True
            cur = parent_ppid
    return False


def _cached(key: str, compute: Callable[[], bool]) -> bool:
    now = time.monotonic()
    with _PROCESS_CACHE_LOCK:
        hit = _process_cache.get(key)
        if hit is not None and now - hit[0] < _PROCESS_CACHE_SECONDS:
            return hit[1]
    value = compute()
    with _PROCESS_CACHE_LOCK:
        _process_cache[key] = (time.monotonic(), value)
    return value


def invalidate_process_cache() -> None:
    """Forget the cached Steam/Gaming Mode checks (call around anything that closes or opens Steam)."""
    with _PROCESS_CACHE_LOCK:
        _process_cache.clear()


def is_gamescope_session() -> bool:
    """Return True if currently running inside SteamOS Gaming Mode (gamescope).

    A stray gamescope process (a game wrapped in gamescope on the Desktop) does not count.
    """
    if _env_is_gamescope(os.environ):
        return True
    return _cached("gamescope", lambda: _gamescope_from_processes(_read_processes()))


def is_steam_running() -> bool:
    """Return True if a Steam client process of this user is currently running."""
    return _cached("steam", lambda: any(comm == "steam" for comm, _ in _read_processes().values()))
