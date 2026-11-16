"""Steam path discovery and Proton/session detection for SteamOS/Linux."""

from __future__ import annotations

import os
import re
import subprocess
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
    if path.is_symlink() or not path.is_dir():
        return 0
    total = 0
    stack: list[Path | str] = [path]
    while stack:
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
                        continue
        except OSError:
            continue
    return total


def find_steam_root() -> Path | None:
    """Return the active Steam installation directory, if present."""
    candidates = [
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".steam" / "steam",
    ]
    for candidate in candidates:
        if (candidate / "userdata").is_dir():
            return candidate.resolve()
    return None


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


def _list_steam_library_roots(steam_root: Path) -> list[Path]:
    """Return all Steam library root directories (main steam_root + libraryfolders.vdf)."""
    roots: list[Path] = [steam_root]
    seen: set[Path] = set()
    try:
        seen.add(steam_root.resolve())
    except OSError:
        seen.add(steam_root)

    lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not lib_vdf.is_file():
        return roots
    try:
        import vdf

        data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
    except _VDF_READ_ERRORS:
        return roots
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    if isinstance(folders, dict):
        for val in folders.values():
            raw_path = val.get("path") if isinstance(val, dict) else (val if isinstance(val, str) else None)
            if not raw_path:
                continue
            cand = Path(str(raw_path))
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


def is_gamescope_session() -> bool:
    """Return True if currently running inside SteamOS Gaming Mode (gamescope)."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gamescope" in desktop:
        return True
    try:
        res = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "gamescope"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_steam_running() -> bool:
    """Return True if a Steam client process is currently running."""
    try:
        res = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
