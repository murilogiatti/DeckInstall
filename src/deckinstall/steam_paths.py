"""Steam path discovery and Proton/session detection for SteamOS/Linux."""

from __future__ import annotations

import os
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


def find_steam_root() -> Path | None:
    """Return the active Steam installation directory, if present."""
    candidates = [
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
    ]
    for candidate in candidates:
        if (candidate / "userdata").is_dir():
            return candidate.resolve()
    return None


def _parse_loginusers_priority(steam_root: Path) -> dict[str, tuple[int, int]]:
    """Return {steam3_id: (most_recent_flag, timestamp)} from config/loginusers.vdf."""
    loginusers = steam_root / "config" / "loginusers.vdf"
    if not loginusers.is_file():
        return {}
    try:
        import vdf

        data = vdf.loads(loginusers.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    users_map = data.get("users")
    if not isinstance(users_map, dict):
        return {}
    priority: dict[str, tuple[int, int]] = {}
    for steam64_str, info in users_map.items():
        if not isinstance(info, dict) or not str(steam64_str).isdigit():
            continue
        account_id = int(steam64_str) - 76561197960265728
        if account_id <= 0:
            continue
        most_recent = 1 if str(info.get("MostRecent", "0")) == "1" else 0
        ts_str = str(info.get("Timestamp", "0"))
        ts = int(ts_str) if ts_str.isdigit() else 0
        priority[str(account_id)] = (most_recent, ts)
    return priority


def list_steam_users(steam_root: Path) -> list[str]:
    """Return numeric Steam3 userdata IDs, prioritizing loginusers.vdf MostRecent/Timestamp."""
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return []
    users = [
        p
        for p in userdata.iterdir()
        if p.is_dir() and p.name.isdigit() and p.name != "0"
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


def find_default_proton_name(steam_root: Path) -> str:
    """Return the Steam CompatToolMapping name for the newest stable Proton."""
    common = steam_root / "steamapps" / "common"
    for candidate_dir, tool_name in [
        ("Proton 11.0", "proton_11"),
        ("Proton 10.0", "proton_10"),
        ("Proton 9.0", "proton_9"),
        ("Proton 9.0 (Beta)", "proton_9"),
    ]:
        if (common / candidate_dir / "proton").is_file():
            return tool_name
    if common.is_dir():
        for entry in sorted(common.glob("Proton [0-9]*"), reverse=True):
            if (entry / "proton").is_file():
                major = entry.name.split()[1].split(".")[0]
                if major.isdigit():
                    return f"proton_{major}"
    if (common / "Proton - Experimental" / "proton").is_file():
        return "proton_experimental"
    return "proton_11"


def find_proton_binary(steam_root: Path) -> Path | None:
    """Locate an installed stable Proton executable for running Windows setup.exe installers."""
    common = steam_root / "steamapps" / "common"
    for name in ("Proton 11.0", "Proton 10.0", "Proton 9.0", "Proton - Experimental"):
        proton = common / name / "proton"
        if proton.is_file():
            return proton
    if common.is_dir():
        for entry in sorted(common.glob("Proton*"), reverse=True):
            proton = entry / "proton"
            if proton.is_file():
                return proton
    return None


def is_gamescope_session() -> bool:
    """Return True if currently running inside SteamOS Gaming Mode (gamescope)."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gamescope" in desktop:
        return True
    try:
        res = subprocess.run(
            ["pgrep", "-x", "gamescope"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False


def is_steam_running() -> bool:
    """Return True if a Steam client process is currently running."""
    try:
        res = subprocess.run(
            ["pgrep", "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False
