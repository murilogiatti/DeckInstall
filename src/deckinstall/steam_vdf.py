"""Safe reader/writer for Steam shortcuts.vdf and config.vdf."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import vdf

from .scanner import clean_game_name, detect_steam_appid
from .steam_paths import (
    SteamPaths,
    find_default_proton_name,
    is_flatpak_steam,
    is_steam_running,
    list_steam_users,
)

_STEAM_LOCK = threading.RLock()
_THREAD_LOCAL = threading.local()


@contextmanager
def _file_lock(lock_path: Path):
    """Acquire both intra-process RLock and cross-process flock for a lockfile path."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = lock_path.resolve()
    depths: dict[Path, int] = getattr(_THREAD_LOCAL, "flock_depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCAL.flock_depths = depths
    depth = depths.get(resolved, 0)
    with _STEAM_LOCK:
        if depth > 0:
            depths[resolved] = depth + 1
            try:
                yield
            finally:
                depths[resolved] = depth
            return
        fd = os.open(str(resolved), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            depths[resolved] = 1
            try:
                yield
            finally:
                depths.pop(resolved, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _steam_transaction_lock(anchor: Path):
    """Acquire both intra-process RLock and cross-process flock per lockfile for Steam VDF mutations."""
    lock_dir = anchor.parent if anchor.parent.exists() else Path(tempfile.gettempdir())
    with _file_lock(lock_dir / ".deckinstall.vdf.lock"):
        yield


@dataclass(frozen=True)
class ShortcutEntry:
    """Normalized representation of a Non-Steam shortcut."""

    index: str
    appid_u32: int
    app_name: str
    exe: str
    start_dir: str
    icon: str
    launch_options: str


def compute_shortcut_appid(exe_quoted: str, app_name: str) -> tuple[int, int]:
    """Return (unsigned_u32, signed_i32) Steam shortcut AppID."""
    crc = zlib.crc32((exe_quoted + app_name).encode("utf-8")) & 0xFFFFFFFF
    u32 = crc | 0x80000000
    s32 = struct.unpack("<i", struct.pack("<I", u32))[0]
    return u32, s32


def s32_to_u32(val: int) -> int:
    """Convert signed 32-bit VDF int to unsigned 32-bit AppID."""
    return val & 0xFFFFFFFF


def _quote_path(path: str) -> str:
    stripped = path.strip().strip('"')
    return f'"{stripped}"'


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to a unique temp file in the same directory and atomically replace target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _STEAM_LOCK:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text to a temp file in the same directory and atomically replace target."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _backup_file(path: Path, keep: int = 5, backup_dir: Path | None = None) -> Path | None:
    if path.is_symlink() or not path.is_file():
        return None
    dest_dir = backup_dir if backup_dir is not None else path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = dest_dir / f"{path.name}.{stamp}.deckinstall.bak"
    counter = 1
    while backup.exists():
        backup = dest_dir / f"{path.name}.{stamp}.{counter}.deckinstall.bak"
        counter += 1
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".bak.tmp", dir=str(dest_dir))
    tmp = Path(tmp_name)
    try:
        src_fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(src_fd, "rb") as src_fh, os.fdopen(fd, "wb") as dst_fh:
                shutil.copyfileobj(src_fh, dst_fh, 65536)
                dst_fh.flush()
                os.fsync(dst_fh.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(tmp, backup)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    old_backups = sorted(
        dest_dir.glob(f"{path.name}.*.deckinstall.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in old_backups[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return backup


def load_shortcuts(shortcuts_vdf: Path) -> list[ShortcutEntry]:
    """Read all Non-Steam shortcuts from shortcuts.vdf."""
    if not shortcuts_vdf.is_file():
        return []
    raw = shortcuts_vdf.read_bytes()
    if not raw:
        return []
    data = vdf.binary_loads(raw)
    shortcuts = data.get("shortcuts", {})
    entries: list[ShortcutEntry] = []
    for idx, item in shortcuts.items():
        if not isinstance(item, dict):
            continue
        app_name = str(item.get("AppName") or item.get("appname") or "")
        exe = str(item.get("Exe") or item.get("exe") or "")
        appid_raw = item.get("appid", 0)
        if isinstance(appid_raw, int) and appid_raw != 0:
            appid_u32 = s32_to_u32(appid_raw)
        else:
            appid_u32, _ = compute_shortcut_appid(exe, app_name)
        entries.append(
            ShortcutEntry(
                index=str(idx),
                appid_u32=appid_u32,
                app_name=app_name,
                exe=exe,
                start_dir=str(item.get("StartDir", "")),
                icon=str(item.get("icon", "")),
                launch_options=str(item.get("LaunchOptions", "")),
            )
        )
    return entries


def shutdown_steam(timeout_seconds: int = 15, steam_root: Path | None = None) -> bool:
    """Gracefully shut down Steam and wait for exit."""
    if not is_steam_running():
        return True
    use_flatpak = is_flatpak_steam(steam_root)
    cmd = (
        ["flatpak", "run", "com.valvesoftware.Steam", "-shutdown"]
        if use_flatpak
        else [shutil.which("steam") or "/usr/bin/steam", "-shutdown"]
    )
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_steam_running():
            return True
        time.sleep(0.5)
    if use_flatpak:
        subprocess.run(
            ["flatpak", "kill", "com.valvesoftware.Steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        subprocess.run(
            ["pkill", "-u", str(os.getuid()), "-TERM", "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(2.0)
    return not is_steam_running()


def start_steam(steam_root: Path | None = None) -> None:
    """Start Steam in the background using the current desktop environment."""
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    cmd = (
        ["flatpak", "run", "com.valvesoftware.Steam", "-silent"]
        if is_flatpak_steam(steam_root)
        else [shutil.which("steam") or "/usr/bin/steam", "-silent"]
    )
    subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def add_or_update_shortcut(
    paths: SteamPaths,
    app_name: str,
    exe_path: Path,
    start_dir: Path | None = None,
    icon_path: Path | None = None,
    launch_options: str | None = None,
    enable_proton: bool = True,
    preserve_from_appid: int | None = None,
) -> int:
    """Add or update a Non-Steam shortcut and optionally set default Proton.

    Returns the unsigned 32-bit shortcut AppID.
    """
    exe_quoted = _quote_path(str(exe_path))
    dir_quoted = _quote_path(str(start_dir or exe_path.parent))
    appid_u32, appid_s32 = compute_shortcut_appid(exe_quoted, app_name)

    with _steam_transaction_lock(paths.shortcuts_vdf):
        paths.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
        existed_before = paths.shortcuts_vdf.is_file()
        raw_before = paths.shortcuts_vdf.read_bytes() if existed_before else None
        old_shortcuts_bytes = raw_before if (raw_before and len(raw_before) > 0) else None
        _backup_file(paths.shortcuts_vdf)

        if old_shortcuts_bytes is not None:
            data = vdf.binary_loads(old_shortcuts_bytes)
        else:
            data = {"shortcuts": {}}

        shortcuts = data.setdefault("shortcuts", {})
        existing_meta: dict = {}
        stale_appids: list[int] = []
        filtered = []
        for item in shortcuts.values():
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("AppName") or item.get("appname") or "")
            item_u32 = s32_to_u32(int(item.get("appid", 0)))
            if preserve_from_appid is not None and item_u32 == preserve_from_appid:
                existing_meta = dict(item)
                continue
            if item_u32 == appid_u32 or item_name == app_name:
                if not existing_meta:
                    existing_meta = dict(item)
                if item_u32 != appid_u32 and item_u32 != preserve_from_appid:
                    stale_appids.append(item_u32)
                continue
            filtered.append(item)

        resolved_icon = str(icon_path) if icon_path is not None else str(existing_meta.get("icon", ""))
        resolved_launch = (
            launch_options if launch_options is not None else str(existing_meta.get("LaunchOptions", ""))
        )
        new_entry = {
            "appid": appid_s32,
            "AppName": app_name,
            "Exe": exe_quoted,
            "StartDir": dir_quoted,
            "icon": resolved_icon,
            "ShortcutPath": existing_meta.get("ShortcutPath", ""),
            "LaunchOptions": resolved_launch,
            "IsHidden": existing_meta.get("IsHidden", 0),
            "AllowDesktopConfig": existing_meta.get("AllowDesktopConfig", 1),
            "AllowOverlay": existing_meta.get("AllowOverlay", 1),
            "OpenVR": existing_meta.get("OpenVR", 0),
            "Devkit": existing_meta.get("Devkit", 0),
            "DevkitGameID": existing_meta.get("DevkitGameID", ""),
            "DevkitOverrideAppID": existing_meta.get("DevkitOverrideAppID", 0),
            "LastPlayTime": existing_meta.get("LastPlayTime", 0),
            "FlatpakAppID": existing_meta.get("FlatpakAppID", ""),
            "sortas": existing_meta.get("sortas", ""),
            "tags": existing_meta.get("tags", {}),
        }
        filtered.append(new_entry)
        data["shortcuts"] = {str(i): entry for i, entry in enumerate(filtered)}
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))

        if enable_proton:
            existing_proton = get_shortcut_proton(paths.config_vdf, appid_u32)
            if not existing_proton:
                inherited_proton = ""
                if preserve_from_appid is not None:
                    inherited_proton = get_shortcut_proton(paths.config_vdf, preserve_from_appid)
                elif stale_appids:
                    for sid in stale_appids:
                        inherited_proton = get_shortcut_proton(paths.config_vdf, sid)
                        if inherited_proton:
                            break
                target_proton = inherited_proton or find_default_proton_name(paths.steam_root)
                try:
                    set_compat_tool(
                        paths.config_vdf,
                        appid_u32,
                        target_proton,
                    )
                except Exception:
                    if existed_before and raw_before is not None:
                        _atomic_write_bytes(paths.shortcuts_vdf, raw_before)
                    else:
                        paths.shortcuts_vdf.unlink(missing_ok=True)
                    raise
        if preserve_from_appid is None and stale_appids:
            new_compat = paths.compatdata_dir / str(appid_u32)
            for old_id in stale_appids:
                old_compat = paths.compatdata_dir / str(old_id)
                if old_compat.is_dir() and not old_compat.is_symlink() and not new_compat.exists():
                    used_by_other = False
                    for uid in list_steam_users(paths.steam_root):
                        if uid == paths.user_id:
                            continue
                        other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
                        if any(e.appid_u32 == old_id for e in load_shortcuts(other_sv)):
                            used_by_other = True
                            break
                    try:
                        new_compat.parent.mkdir(parents=True, exist_ok=True)
                        if used_by_other:
                            shutil.copytree(old_compat, new_compat, symlinks=True)
                        else:
                            shutil.move(str(old_compat), str(new_compat))
                    except OSError:
                        logging.warning("Failed migrating compatdata %s -> %s", old_id, appid_u32)
                if paths.grid_dir.is_dir():
                    old_prefix = str(old_id)
                    for art_file in paths.grid_dir.iterdir():
                        if not art_file.is_symlink() and art_file.is_file() and art_file.name.startswith(old_prefix):
                            suffix = art_file.name[len(old_prefix) :]
                            if suffix and suffix[0] not in ("p", "_", "."):
                                continue
                            dest_art = paths.grid_dir / f"{appid_u32}{suffix}"
                            if not dest_art.exists():
                                try:
                                    shutil.move(str(art_file), str(dest_art))
                                except OSError:
                                    pass
        for stale_id in stale_appids:
            try:
                remove_compat_tool(paths.config_vdf, stale_id)
            except Exception:
                pass

    return appid_u32


def update_shortcut_launch_options(
    paths: SteamPaths,
    appid_u32: int,
    launch_options: str,
) -> bool:
    """Update only LaunchOptions on an existing shortcut without altering AppID or metadata."""
    with _steam_transaction_lock(paths.shortcuts_vdf):
        if not paths.shortcuts_vdf.is_file() or paths.shortcuts_vdf.stat().st_size == 0:
            return False
        data = vdf.binary_loads(paths.shortcuts_vdf.read_bytes())
        shortcuts = data.get("shortcuts", {})
        updated = False
        for item in shortcuts.values():
            if not isinstance(item, dict):
                continue
            if s32_to_u32(int(item.get("appid", 0))) == appid_u32:
                item["LaunchOptions"] = launch_options
                updated = True
                break
        if not updated:
            return False
        _backup_file(paths.shortcuts_vdf)
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))
        return True


def remove_shortcut(paths: SteamPaths, appid_u32: int) -> bool:
    """Remove a Non-Steam shortcut by its unsigned 32-bit AppID."""
    with _steam_transaction_lock(paths.shortcuts_vdf):
        if not paths.shortcuts_vdf.is_file():
            return False
        old_shortcuts_bytes = paths.shortcuts_vdf.read_bytes()
        _backup_file(paths.shortcuts_vdf)
        data = vdf.binary_loads(old_shortcuts_bytes)
        shortcuts = data.get("shortcuts", {})
        kept = []
        removed = False
        for item in shortcuts.values():
            if not isinstance(item, dict):
                continue
            item_u32 = s32_to_u32(int(item.get("appid", 0)))
            if item_u32 == appid_u32:
                removed = True
                continue
            kept.append(item)
        if not removed:
            return False
        data["shortcuts"] = {str(i): entry for i, entry in enumerate(kept)}
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))
        try:
            remove_compat_tool(paths.config_vdf, appid_u32)
        except Exception:
            _atomic_write_bytes(paths.shortcuts_vdf, old_shortcuts_bytes)
            raise
        return True


def set_compat_tool(
    config_vdf: Path,
    appid_u32: int,
    tool_name: str,
    create_if_missing: bool = False,
) -> None:
    """Ensure CompatToolMapping for appid_u32 is set in Steam's config.vdf."""
    with _steam_transaction_lock(config_vdf):
        if config_vdf.is_file():
            _backup_file(config_vdf)
            data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        elif create_if_missing:
            data = {}
        else:
            return
        store = data.setdefault("InstallConfigStore", {})
        steam = (
            store.setdefault("Software", {})
            .setdefault("Valve", {})
            .setdefault("Steam", {})
        )
        mapping = steam.setdefault("CompatToolMapping", {})
        mapping[str(appid_u32)] = {
            "name": tool_name,
            "config": "",
            "priority": "250",
        }
        _atomic_write_text(config_vdf, vdf.dumps(data, pretty=True))


def remove_compat_tool(config_vdf: Path, appid_u32: int) -> None:
    """Remove CompatToolMapping entry for appid_u32 if present."""
    with _steam_transaction_lock(config_vdf):
        if not config_vdf.is_file():
            return
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        mapping = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
        )
        if str(appid_u32) in mapping:
            _backup_file(config_vdf)
            del mapping[str(appid_u32)]
            _atomic_write_text(config_vdf, vdf.dumps(data, pretty=True))


@dataclass(frozen=True)
class CompatMappingEntry:
    """One entry in Steam's CompatToolMapping resolved against active games."""

    appid_u32: int
    name: str
    source: str
    tool_name: str


def _installed_steam_app_names(steam_root: Path) -> dict[int, str]:
    """Return {appid: name} for installed Steam games across all library folders."""
    names: dict[int, str] = {}
    steamapps_dirs = [steam_root / "steamapps"]
    lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if lib_vdf.is_file():
        try:
            data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for val in folders.values():
                if isinstance(val, dict) and val.get("path"):
                    steamapps_dirs.append(Path(str(val["path"])) / "steamapps")
        except Exception:
            pass
    for sa in steamapps_dirs:
        if not sa.is_dir():
            continue
        for acf in sa.glob("appmanifest_*.acf"):
            stem_id = acf.stem.removeprefix("appmanifest_")
            if not (stem_id.isascii() and stem_id.isdigit()):
                continue
            appid = int(stem_id)
            app_name = f"AppID {appid}"
            try:
                parsed = vdf.loads(acf.read_text(encoding="utf-8", errors="replace"))
                state = parsed.get("AppState") or parsed.get("appstate") or {}
                if isinstance(state, dict) and state.get("name"):
                    app_name = str(state["name"])
            except Exception:
                pass
            names[appid] = app_name
    return names


def list_compat_tool_usage(paths: SteamPaths) -> list[CompatMappingEntry]:
    """List all CompatToolMapping entries resolved against Non-Steam shortcuts and Steam games."""
    shortcut_names: dict[int, str] = {}
    for e in load_shortcuts(paths.shortcuts_vdf):
        shortcut_names[e.appid_u32] = e.app_name
    for uid in list_steam_users(paths.steam_root):
        sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
        for e in load_shortcuts(sv):
            shortcut_names.setdefault(e.appid_u32, e.app_name)

    steam_names = _installed_steam_app_names(paths.steam_root)
    results: list[CompatMappingEntry] = []
    if not paths.config_vdf.is_file():
        return results
    try:
        data = vdf.loads(paths.config_vdf.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return results
    mapping = (
        data.get("InstallConfigStore", {})
        .get("Software", {})
        .get("Valve", {})
        .get("Steam", {})
        .get("CompatToolMapping", {})
    )
    if not isinstance(mapping, dict):
        return results
    for k, val in mapping.items():
        if not (str(k).isascii() and str(k).isdigit()) or not isinstance(val, dict):
            continue
        appid = int(k)
        tool = str(val.get("name") or "")
        if appid in shortcut_names:
            source, name = "Non-Steam", shortcut_names[appid]
        elif appid in steam_names:
            source, name = "Steam", steam_names[appid]
        else:
            source, name = "Orphan", f"AppID {appid}"
        results.append(CompatMappingEntry(appid_u32=appid, name=name, source=source, tool_name=tool))
    rank = {"Non-Steam": 0, "Steam": 1, "Orphan": 2}
    results.sort(key=lambda r: (rank.get(r.source, 9), r.name.lower(), r.appid_u32))
    return results


def bulk_migrate_compat_tool(config_vdf: Path, appids: list[int], target_tool: str) -> int:
    """Migrate multiple AppIDs in CompatToolMapping to target_tool in one atomic write."""
    if not appids or not target_tool.strip():
        return 0
    with _steam_transaction_lock(config_vdf):
        if not config_vdf.is_file():
            return 0
        _backup_file(config_vdf)
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        store = data.setdefault("InstallConfigStore", {})
        steam = (
            store.setdefault("Software", {})
            .setdefault("Valve", {})
            .setdefault("Steam", {})
        )
        mapping = steam.setdefault("CompatToolMapping", {})
        updated = 0
        for appid in appids:
            mapping[str(appid)] = {
                "name": target_tool.strip(),
                "config": "",
                "priority": "250",
            }
            updated += 1
        _atomic_write_text(config_vdf, vdf.dumps(data, pretty=True))
        return updated


def remove_compat_tools(config_vdf: Path, appids: list[int]) -> int:
    """Remove multiple AppIDs from CompatToolMapping in one atomic write."""
    if not appids:
        return 0
    with _steam_transaction_lock(config_vdf):
        if not config_vdf.is_file():
            return 0
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        mapping = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
        )
        if not isinstance(mapping, dict):
            return 0
        removed = 0
        for appid in appids:
            if str(appid) in mapping:
                if removed == 0:
                    _backup_file(config_vdf)
                del mapping[str(appid)]
                removed += 1
        if removed:
            _atomic_write_text(config_vdf, vdf.dumps(data, pretty=True))
        return removed


_SUBDIR_NAMES = {
    "win64",
    "win32",
    "x64",
    "x86",
    "bin",
    "binaries",
    "shipping",
    "retail",
    "build",
    "release",
    "engine",
}


_FORBIDDEN_ROOT_NAMES = {
    ".config",
    ".var",
    ".local",
    "heroic",
    "lutris",
    "prefixes",
    "wine-prefixes",
    "bottles",
    "umu",
    "steam",
    "hydralauncher",
}


def _rewrite_path_prefix(val: str, old_prefixes: tuple[str, ...], new_prefix: str) -> str:
    for old_p in old_prefixes:
        if not old_p:
            continue
        if val == old_p:
            return new_prefix
        if val.startswith(old_p + "/"):
            return new_prefix + val[len(old_p) :]
    return val


def _raw_shell_tokens(s: str) -> list[str]:
    """Split shell string on unquoted whitespace while keeping quotes and escapes intact."""
    tokens: list[str] = []
    cur: list[str] = []
    in_sq = False
    in_dq = False
    escape = False
    for ch in s:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if ch == "\\" and not in_sq:
            cur.append(ch)
            escape = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            cur.append(ch)
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            cur.append(ch)
            continue
        if not in_sq and not in_dq and ch.isspace():
            if cur:
                tokens.append("".join(cur))
                cur.clear()
            continue
        cur.append(ch)
    if in_sq or in_dq or escape:
        raise ValueError("No closing quotation")
    if cur:
        tokens.append("".join(cur))
    return tokens


def _rewrite_launch_options(
    launch_options: str,
    old_root: Path,
    new_root: Path,
    old_compat: Path | None = None,
    new_compat: Path | None = None,
) -> str:
    """Safely rewrite path tokens in LaunchOptions without altering unrelated quoting or expansions."""
    if not launch_options:
        return launch_options
    old_roots = tuple(dict.fromkeys([str(old_root.resolve()), str(old_root)]))
    new_root_str = str(new_root)
    old_compats = (
        tuple(dict.fromkeys([str(old_compat.resolve()), str(old_compat)]))
        if old_compat
        else ()
    )
    new_compat_str = str(new_compat) if new_compat else ""
    try:
        raw_tokens = _raw_shell_tokens(launch_options)
    except ValueError:
        return launch_options

    changed = False
    out_tokens: list[str] = []
    for raw_tok in raw_tokens:
        try:
            unquoted = shlex.split(raw_tok)
            tok = unquoted[0] if len(unquoted) == 1 else raw_tok
        except ValueError:
            tok = raw_tok
        new_raw = raw_tok
        if _is_env_assignment(tok):
            key, val = tok.split("=", 1)
            upd = _rewrite_path_prefix(val, old_roots, new_root_str)
            if old_compats and new_compat_str:
                upd = _rewrite_path_prefix(upd, old_compats, new_compat_str)
            if upd != val:
                new_raw = f"{key}={shlex.quote(upd)}"
                changed = True
        else:
            upd = _rewrite_path_prefix(tok, old_roots, new_root_str)
            if old_compats and new_compat_str:
                upd = _rewrite_path_prefix(upd, old_compats, new_compat_str)
            if upd != tok:
                new_raw = shlex.quote(upd)
                changed = True
        out_tokens.append(new_raw)
    return " ".join(out_tokens) if changed else launch_options


def _has_proton_mapping(
    config_vdf: Path, appid_u32: int, expected_tool: str | None = None
) -> bool:
    if not config_vdf.is_file():
        return False
    try:
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        mapping = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
        )
        entry = mapping.get(str(appid_u32))
        if not isinstance(entry, dict):
            return False
        name = str(entry.get("name") or "").strip()
        if not name:
            return False
        if expected_tool:
            return name == expected_tool
        return True
    except Exception:
        return False


def infer_game_root_from_exe(exe_path: Path) -> Path:
    """Infer the top-level game folder from an executable path."""
    home = Path.home()
    known_containers = {
        home.resolve(),
        (home / "Games").resolve(),
        (home / "Downloads").resolve(),
        (home / "Syncthing").resolve(),
    }
    resolved = exe_path.resolve()
    if any(part.lower() == "drive_c" for part in resolved.parts):
        raise ValueError(
            f"O executável está dentro de um prefixo Wine (drive_c): {resolved}. "
            "Mova o jogo para uma pasta própria antes de migrar."
        )
    if resolved.parent in known_containers:
        raise ValueError(
            f"O executável está solto diretamente em {resolved.parent}; "
            "coloque-o dentro de uma subpasta própria antes de migrar."
        )
    candidate = resolved.parent
    for parent in resolved.parents:
        if parent.parent in known_containers:
            candidate = parent
            break
        if (
            len(parent.parts) >= 5
            and parent.parts[:3] == ("/", "run", "media")
            and parent.parent.name == "Games"
        ):
            candidate = parent
            break
    else:
        while (
            candidate.name.lower() in _SUBDIR_NAMES
            and candidate.parent != candidate
            and candidate.parent not in known_containers
            and not (
                len(candidate.parent.parts) <= 4
                and candidate.parent.parts[:3] == ("/", "run", "media")
            )
        ):
            candidate = candidate.parent
    if candidate.name.lower() in _FORBIDDEN_ROOT_NAMES or candidate in known_containers:
        raise ValueError(f"Raiz inferida inválida ou protegida para migração: {candidate}")
    return candidate


def migrate_shortcut_to_games(
    paths: SteamPaths,
    entry: ShortcutEntry,
    games_root: Path,
    new_name: str | None = None,
) -> tuple[int, Path, int | None]:
    """Move a game folder to games_root/<clean_name>, preserve compatdata/saves, and update Steam.

    Returns (new_appid_u32, new_exe_path, detected_steam_appid).
    """
    old_exe = Path(entry.exe.strip().strip('"'))
    if not old_exe.is_file():
        raise FileNotFoundError(f"Executável não encontrado: {old_exe}")

    old_root = infer_game_root_from_exe(old_exe)
    target_name = (new_name or "").strip() or clean_game_name(old_root.name)
    if not target_name or target_name in (".", "..") or "/" in target_name or "\\" in target_name:
        raise ValueError(f"Nome de jogo inválido: {target_name!r}")
    new_root = games_root / target_name
    games_root.mkdir(parents=True, exist_ok=True)

    rel_exe = old_exe.resolve().relative_to(old_root.resolve())
    new_exe = new_root / rel_exe
    old_roots = tuple(dict.fromkeys([str(old_root.resolve()), str(old_root)]))
    raw_start = _strip_vdf_quotes(entry.start_dir)
    if raw_start:
        rewritten_start = _rewrite_path_prefix(raw_start, old_roots, str(new_root))
        new_start_dir = Path(rewritten_start)
    else:
        new_start_dir = new_exe.parent
    raw_icon = _strip_vdf_quotes(entry.icon)
    new_icon: Path | None = None
    if raw_icon:
        new_icon = Path(_rewrite_path_prefix(raw_icon, old_roots, str(new_root)))

    predicted_appid, _ = compute_shortcut_appid(_quote_path(str(new_exe)), target_name)
    old_compat = paths.compatdata_dir / str(entry.appid_u32)
    new_compat = paths.compatdata_dir / str(predicted_appid)
    new_launch = _rewrite_launch_options(
        entry.launch_options,
        old_root,
        new_root,
        old_compat=old_compat if predicted_appid != entry.appid_u32 else None,
        new_compat=new_compat if predicted_appid != entry.appid_u32 else None,
    )

    shortcuts_snap: bytes | None = None
    config_snap: bytes | None = None
    moved_root = False
    moved_compat_pair: tuple[Path, Path] | None = None
    copied_compat_dst: Path | None = None
    backed_compat_pair: tuple[Path, Path] | None = None
    moved_art_pairs: list[tuple[Path, Path]] = []

    try:
        with _steam_transaction_lock(paths.shortcuts_vdf), _steam_transaction_lock(paths.config_vdf):
            shortcuts_snap = paths.shortcuts_vdf.read_bytes() if paths.shortcuts_vdf.is_file() else None
            config_snap = paths.config_vdf.read_bytes() if paths.config_vdf.is_file() else None
            if old_root.resolve() != new_root.resolve():
                if new_root.exists():
                    raise FileExistsError(f"Destination folder already exists: {new_root}")
                shutil.move(str(old_root), str(new_root))
                moved_root = True
            new_appid = add_or_update_shortcut(
                paths=paths,
                app_name=target_name,
                exe_path=new_exe,
                start_dir=new_start_dir,
                icon_path=new_icon,
                launch_options=new_launch,
                enable_proton=True,
                preserve_from_appid=entry.appid_u32,
            )

            # Update other Steam users' shortcuts that pointed into old_root
            other_sv_snaps: list[tuple[Path, bytes]] = []
            for uid in list_steam_users(paths.steam_root):
                if uid == paths.user_id:
                    continue
                other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
                if not other_sv.is_file() or other_sv.stat().st_size == 0:
                    continue
                try:
                    with _steam_transaction_lock(other_sv):
                        orig_other_bytes = other_sv.read_bytes()
                        other_data = vdf.binary_loads(orig_other_bytes)
                        other_sc = other_data.get("shortcuts", {})
                        other_changed = False
                        for item in other_sc.values():
                            if not isinstance(item, dict):
                                continue
                            for field in ("Exe", "StartDir", "icon"):
                                val = str(item.get(field) or "")
                                cleaned = _strip_vdf_quotes(val)
                                if not cleaned:
                                    continue
                                upd = _rewrite_path_prefix(cleaned, old_roots, str(new_root))
                                if upd != cleaned:
                                    item[field] = _quote_path(upd) if field != "icon" else upd
                                    other_changed = True
                            lo = str(item.get("LaunchOptions") or "")
                            if lo:
                                upd_lo = _rewrite_launch_options(lo, old_root, new_root)
                                if upd_lo != lo:
                                    item["LaunchOptions"] = upd_lo
                                    other_changed = True
                        if other_changed:
                            other_sv_snaps.append((other_sv, orig_other_bytes))
                            _backup_file(other_sv)
                            _atomic_write_bytes(other_sv, vdf.binary_dumps(other_data))
                except Exception as exc:
                    logging.warning("Could not update other user %s shortcuts: %s", uid, exc)

            if new_appid != entry.appid_u32:
                new_compat = paths.compatdata_dir / str(new_appid)
                if old_compat.is_dir():
                    used_by_other = False
                    for uid in list_steam_users(paths.steam_root):
                        if uid == paths.user_id:
                            continue
                        other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
                        if any(e.appid_u32 == entry.appid_u32 for e in load_shortcuts(other_sv)):
                            used_by_other = True
                            break
                    if new_compat.exists():
                        stamp = time.strftime("%Y%m%d-%H%M%S")
                        backup_compat = paths.compatdata_dir / f"{new_appid}.{stamp}.deckinstall.bak"
                        counter = 1
                        while backup_compat.exists():
                            backup_compat = paths.compatdata_dir / f"{new_appid}.{stamp}.{counter}.deckinstall.bak"
                            counter += 1
                        shutil.move(str(new_compat), str(backup_compat))
                        backed_compat_pair = (backup_compat, new_compat)
                    if used_by_other:
                        copied_compat_dst = new_compat
                        shutil.copytree(old_compat, new_compat, symlinks=True)
                    else:
                        shutil.move(str(old_compat), str(new_compat))
                        moved_compat_pair = (new_compat, old_compat)

                if paths.grid_dir.is_dir():
                    old_prefix = str(entry.appid_u32)
                    for art_file in paths.grid_dir.iterdir():
                        if not art_file.is_symlink() and art_file.is_file() and art_file.name.startswith(old_prefix):
                            suffix = art_file.name[len(old_prefix) :]
                            if suffix and suffix[0] not in ("p", "_", "."):
                                continue
                            dest_art = paths.grid_dir / f"{new_appid}{suffix}"
                            if not dest_art.exists():
                                shutil.move(str(art_file), str(dest_art))
                                moved_art_pairs.append((dest_art, art_file))

                remove_shortcut(paths, entry.appid_u32)
    except Exception:
        for dst_a, src_a in reversed(moved_art_pairs):
            try:
                if dst_a.exists() and not src_a.exists():
                    shutil.move(str(dst_a), str(src_a))
            except OSError:
                pass
        if copied_compat_dst and copied_compat_dst.exists():
            try:
                shutil.rmtree(copied_compat_dst)
            except OSError:
                pass
        if moved_compat_pair and moved_compat_pair[0].exists() and not moved_compat_pair[1].exists():
            try:
                shutil.move(str(moved_compat_pair[0]), str(moved_compat_pair[1]))
            except OSError:
                pass
        if backed_compat_pair and backed_compat_pair[0].exists() and not backed_compat_pair[1].exists():
            try:
                shutil.move(str(backed_compat_pair[0]), str(backed_compat_pair[1]))
            except OSError:
                pass
        for other_sv, orig_other in reversed(locals().get("other_sv_snaps", [])):
            try:
                with _steam_transaction_lock(other_sv):
                    _atomic_write_bytes(other_sv, orig_other)
            except OSError:
                logging.exception("Rollback failed restoring %s", other_sv)
        try:
            if shortcuts_snap is not None:
                _atomic_write_bytes(paths.shortcuts_vdf, shortcuts_snap)
            if config_snap is not None:
                _atomic_write_bytes(paths.config_vdf, config_snap)
        except OSError:
            logging.exception("Rollback failed restoring VDF files")
        if moved_root and new_root.exists() and not old_root.exists():
            try:
                shutil.move(str(new_root), str(old_root))
            except OSError:
                logging.exception("Rollback failed moving %s back to %s", new_root, old_root)
        raise

    steam_appid = detect_steam_appid(new_root)
    try:
        import_external_prefix_saves(
            paths=paths,
            shortcut_appid_u32=new_appid,
            game_name=target_name,
            steam_appid=steam_appid,
        )
    except Exception as exc:
        logging.warning(
            "Migration succeeded for %s, but post-migration save import failed: %s",
            target_name,
            exc,
        )
    return new_appid, new_exe, steam_appid


_PREFIX_SKIP_DIRS = {
    "microsoft",
    "temp",
    "dxvk",
    "nvidia",
    "amd",
    "d3dscache",
    ".sentry-native",
    "crashes",
    "crashreportclient",
    "cache",
    "shadercache",
    "gpucache",
    "code cache",
    "webcache",
    "telemetry",
    "logs",
    "cef",
    "crashdumps",
}
_PREFIX_SKIP_FILES = {"steam_autocloud.vdf", "player.log", "player-prev.log"}
_PREFIX_REL_DIRS = (
    Path("Documents"),
    Path("Saved Games"),
    Path("Desktop"),
    Path("AppData") / "Local",
    Path("AppData") / "LocalLow",
    Path("AppData") / "Roaming",
)


def _norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_game_name(name).lower())


def external_prefix_roots() -> list[Path]:
    """Return candidate directories where Hydra, Heroic, Lutris, Bottles, and UMU store prefixes."""
    home = Path.home()
    return [
        home / ".config" / "hydralauncher" / "wine-prefixes",
        home / ".config" / "hydralauncher" / "umu",
        home / "Games" / "Heroic" / "Prefixes",
        home / ".config" / "heroic" / "Prefixes",
        home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic" / "Prefixes",
        home / "Games" / "lutris",
        home / ".local" / "share" / "lutris" / "prefixes",
        home / ".var" / "app" / "net.lutris.Lutris" / "data" / "lutris" / "prefixes",
        home / ".local" / "share" / "bottles" / "bottles",
        home / ".var" / "app" / "com.usebottles.bottles" / "data" / "bottles" / "bottles",
        home / ".local" / "share" / "umu",
        home / "Games" / "Prefixes",
        home / "Prefixes",
    ]


def _prefix_users_dir(prefix_dir: Path) -> Path | None:
    try:
        base_resolved = prefix_dir.resolve()
    except OSError:
        return None
    for rel in (Path("pfx") / "drive_c" / "users", Path("drive_c") / "users"):
        if not _safe_parents_within(prefix_dir, base_resolved, rel.parts):
            continue
        candidate = prefix_dir / rel
        if candidate.is_dir():
            return candidate
    return None


def find_external_prefixes(
    game_name: str,
    steam_appid: int | None = None,
    search_roots: list[Path] | None = None,
) -> list[Path]:
    """Find Wine/UMU/Heroic/Lutris/Bottles prefix directories matching game_name or steam_appid."""
    key = _norm_key(game_name)
    appid_names = {str(steam_appid), f"umu-{steam_appid}"} if steam_appid else set()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots or external_prefix_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            matched = (child.name in appid_names) or (bool(key) and _norm_key(child.name) == key)
            if not matched or _prefix_users_dir(child) is None:
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                found.append(child)
    return found


def _raise_oserror(exc: OSError) -> None:
    raise exc


_NON_SAVE_USER_DIRS = {"public", "default", "default user", "all users"}


def _iter_prefix_save_pairs(
    src_users: Path,
    dst_users: Path,
    *,
    preserve_user_dirs: bool = False,
    raise_on_error: bool = False,
):
    """Yield (src_file, dst_file) for user saves across steamuser/deck/Public."""
    try:
        user_dirs = [
            d
            for d in src_users.iterdir()
            if not d.is_symlink() and d.is_dir() and d.name.lower() not in {"default", "default user", "all users"}
        ]
    except OSError:
        if raise_on_error:
            raise
        return
    onerror = _raise_oserror if raise_on_error else None
    non_public_count = sum(1 for d in user_dirs if d.name.lower() not in _NON_SAVE_USER_DIRS)
    for user_dir in user_dirs:
        if user_dir.name.lower() == "public":
            target_user = user_dir.name if preserve_user_dirs else "Public"
            mappings = [(user_dir / "Documents", dst_users / target_user / "Documents")]
        else:
            if preserve_user_dirs or non_public_count > 1:
                target_user = user_dir.name
            else:
                target_user = "steamuser"
            mappings = [
                (user_dir / rel, dst_users / target_user / rel)
                for rel in _PREFIX_REL_DIRS
            ]
        for src_root, dst_root in mappings:
            if src_root.is_symlink() or not src_root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(
                src_root, followlinks=False, onerror=onerror
            ):
                dirnames[:] = [d for d in dirnames if d.lower() not in _PREFIX_SKIP_DIRS]
                cur = Path(dirpath)
                rel_dir = cur.relative_to(src_root)
                for fname in filenames:
                    if fname.lower() in _PREFIX_SKIP_FILES or fname.lower().endswith(".deckinstall.bak"):
                        continue
                    src_file = cur / fname
                    if src_file.is_symlink() or not src_file.is_file():
                        continue
                    yield src_file, dst_root / rel_dir / fname


def _iter_programdata_save_pairs(
    src_progdata: Path,
    dst_progdata: Path,
    *,
    raise_on_error: bool = False,
):
    """Yield (src_file, dst_file) for non-system ProgramData save files."""
    if src_progdata.is_symlink() or not src_progdata.is_dir():
        return
    onerror = _raise_oserror if raise_on_error else None
    for dirpath, dirnames, filenames in os.walk(
        src_progdata, followlinks=False, onerror=onerror
    ):
        dirnames[:] = [d for d in dirnames if d.lower() not in _PROGRAMDATA_SKIP_DIRS]
        cur = Path(dirpath)
        rel_dir = cur.relative_to(src_progdata)
        for fname in filenames:
            src_file = cur / fname
            if src_file.is_symlink() or not src_file.is_file():
                continue
            yield src_file, dst_progdata / rel_dir / fname


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _strip_vdf_quotes(val: str) -> str:
    return val.strip().strip('"').strip()


def _copy_save_pair_if_newer(
    src_file: Path,
    dst_file: Path,
    dst_root: Path,
    dst_root_resolved: Path,
    errors: list[str],
) -> int:
    try:
        if dst_file.is_symlink():
            return 0
        try:
            rel_parent = dst_file.parent.relative_to(dst_root)
        except ValueError:
            return 0
        if rel_parent.parts and not _safe_parents_within(
            dst_root, dst_root_resolved, rel_parent.parts
        ):
            return 0
        if dst_file.exists() and dst_file.stat().st_mtime >= src_file.stat().st_mtime:
            return 0
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if not dst_file.parent.resolve().is_relative_to(dst_root_resolved):
            return 0
        if dst_file.exists() and (
            dst_file.stat().st_size != src_file.stat().st_size
            or _file_sha256(dst_file) != _file_sha256(src_file)
        ):
            _backup_file(dst_file, keep=3, backup_dir=dst_root_resolved / ".deckinstall_bak")
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dst_file.name}.", suffix=".tmp", dir=str(dst_file.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            shutil.copy2(src_file, tmp_path, follow_symlinks=False)
            os.replace(tmp_path, dst_file)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        return 1
    except OSError as exc:
        logging.warning("Falha ao importar save %s -> %s: %s", src_file, dst_file, exc)
        errors.append(f"{src_file.name}: {exc}")
        return 0


def adopt_installer_prefix(
    paths: SteamPaths,
    game_root: Path,
    shortcut_appid_u32: int,
) -> bool:
    """Adopt `.deckinstall_installer_pfx` saved by setup.exe into compatdata/<appid>/pfx."""
    saved_pfx = game_root / ".deckinstall_installer_pfx"
    if not saved_pfx.is_dir() or saved_pfx.is_symlink():
        return False
    paths.compatdata_dir.mkdir(parents=True, exist_ok=True)
    compat_root_resolved = paths.compatdata_dir.resolve()
    if not _safe_parents_within(
        paths.compatdata_dir, compat_root_resolved, (str(shortcut_appid_u32),)
    ):
        raise OSError(f"Unsafe compatdata path for {shortcut_appid_u32}")
    target_compat = paths.compatdata_dir / str(shortcut_appid_u32)
    target_pfx = target_compat / "pfx"
    if target_pfx.exists() or target_pfx.is_symlink():
        return False
    target_compat.mkdir(parents=True, exist_ok=True)
    shutil.move(str(saved_pfx), str(target_pfx))
    return True


def import_external_prefix_saves(
    paths: SteamPaths,
    shortcut_appid_u32: int,
    game_name: str,
    steam_appid: int | None = None,
    search_roots: list[Path] | None = None,
) -> int:
    """Import newer/missing save files from Hydra, Heroic, Lutris, Bottles, or UMU prefixes."""
    prefixes = find_external_prefixes(
        game_name=game_name,
        steam_appid=steam_appid,
        search_roots=search_roots,
    )
    if not prefixes:
        return 0
    paths.compatdata_dir.mkdir(parents=True, exist_ok=True)
    compat_root_resolved = paths.compatdata_dir.resolve()
    if not _safe_parents_within(
        paths.compatdata_dir,
        compat_root_resolved,
        (str(shortcut_appid_u32), "pfx", "drive_c", "users"),
    ):
        raise OSError(f"Unsafe compatdata users path for {shortcut_appid_u32}")
    dst_drive_c = paths.compatdata_dir / str(shortcut_appid_u32) / "pfx" / "drive_c"
    dst_users = dst_drive_c / "users"
    dst_users.mkdir(parents=True, exist_ok=True)
    dst_users_resolved = dst_users.resolve()
    if not dst_users_resolved.is_relative_to(compat_root_resolved):
        raise OSError(f"Resolved users path escapes compatdata: {dst_users_resolved}")
    dst_progdata = dst_drive_c / "ProgramData"
    copied = 0
    errors: list[str] = []
    for prefix_dir in prefixes:
        src_users = _prefix_users_dir(prefix_dir)
        if src_users is not None:
            try:
                for src_file, dst_file in _iter_prefix_save_pairs(
                    src_users, dst_users, raise_on_error=True
                ):
                    copied += _copy_save_pair_if_newer(
                        src_file, dst_file, dst_users, dst_users_resolved, errors
                    )
            except OSError as exc:
                errors.append(f"{src_users}: {exc}")
        src_progdata = _prefix_programdata_dir(prefix_dir)
        if src_progdata is not None:
            if not _safe_parents_within(
                paths.compatdata_dir,
                compat_root_resolved,
                (str(shortcut_appid_u32), "pfx", "drive_c", "ProgramData"),
            ):
                raise OSError(f"Unsafe compatdata ProgramData path for {shortcut_appid_u32}")
            dst_progdata.mkdir(parents=True, exist_ok=True)
            dst_progdata_resolved = dst_progdata.resolve()
            if not dst_progdata_resolved.is_relative_to(compat_root_resolved):
                raise OSError(f"Resolved ProgramData path escapes compatdata: {dst_progdata_resolved}")
            try:
                for src_file, dst_file in _iter_programdata_save_pairs(
                    src_progdata, dst_progdata, raise_on_error=True
                ):
                    copied += _copy_save_pair_if_newer(
                        src_file, dst_file, dst_progdata, dst_progdata_resolved, errors
                    )
            except OSError as exc:
                errors.append(f"{src_progdata}: {exc}")
    if errors:
        raise RuntimeError(f"Falha ao importar {len(errors)} save(s): {errors[0]}")
    return copied


def import_hydra_umu_saves(
    paths: SteamPaths,
    shortcut_appid_u32: int,
    steam_appid: int | None,
    hydra_prefixes_root: Path | None = None,
) -> int:
    """Import newer/missing save files from Hydra UMU wine-prefixes into Steam compatdata."""
    if not steam_appid:
        return 0
    roots = [hydra_prefixes_root] if hydra_prefixes_root else None
    return import_external_prefix_saves(
        paths=paths,
        shortcut_appid_u32=shortcut_appid_u32,
        game_name="",
        steam_appid=steam_appid,
        search_roots=roots,
    )


def _has_pending_external_saves(
    compatdata_dir: Path,
    shortcut_appid_u32: int,
    game_name: str,
    steam_appid: int | None,
    search_roots: list[Path] | None = None,
) -> bool:
    dst_users = compatdata_dir / str(shortcut_appid_u32) / "pfx" / "drive_c" / "users"
    for prefix_dir in find_external_prefixes(game_name, steam_appid, search_roots):
        src_users = _prefix_users_dir(prefix_dir)
        if src_users is None:
            continue
        for src_file, dst_file in _iter_prefix_save_pairs(src_users, dst_users):
            try:
                if not dst_file.exists() or dst_file.stat().st_mtime < src_file.stat().st_mtime:
                    return True
            except OSError:
                continue
    return False


@dataclass(frozen=True)
class MigrationPlanItem:
    """Preview item for bulk shortcut migration."""

    entry: ShortcutEntry
    old_root: Path
    new_name: str
    new_root: Path
    has_compatdata: bool


def plan_bulk_migration(
    entries: list[ShortcutEntry],
    games_root: Path,
    compatdata_dir: Path,
    search_roots: list[Path] | None = None,
    config_vdf: Path | None = None,
) -> list[MigrationPlanItem]:
    """Identify Windows .exe shortcuts needing standardization, Proton Stable, or external save import."""
    plan: list[MigrationPlanItem] = []
    games_resolved = games_root.resolve()
    for entry in entries:
        exe_path = Path(entry.exe.strip().strip('"'))
        if exe_path.suffix.lower() != ".exe" or not exe_path.is_file():
            continue
        try:
            old_root = infer_game_root_from_exe(exe_path)
        except ValueError:
            continue
        clean_name = clean_game_name(old_root.name)
        new_root = games_root / clean_name
        already_in_games = old_root.resolve().parent == games_resolved
        already_named = entry.app_name == clean_name and old_root.resolve() == new_root.resolve()
        if already_in_games and already_named:
            steam_appid = detect_steam_appid(old_root)
            expected_proton = find_default_proton_name(config_vdf.parent.parent) if config_vdf else None
            needs_proton = bool(
                config_vdf
                and not _has_proton_mapping(config_vdf, entry.appid_u32, expected_tool=expected_proton)
            )
            has_pending = _has_pending_external_saves(
                compatdata_dir, entry.appid_u32, clean_name, steam_appid, search_roots
            )
            if not needs_proton and not has_pending:
                continue
        plan.append(
            MigrationPlanItem(
                entry=entry,
                old_root=old_root,
                new_name=clean_name,
                new_root=new_root,
                has_compatdata=(compatdata_dir / str(entry.appid_u32)).is_dir(),
            )
        )
    return plan


@dataclass(frozen=True)
class CleanupCandidate:
    """An old external prefix, orphan Steam compatdata folder, or backup safe to review for removal."""

    category: str
    label: str
    path: Path
    size_bytes: int
    has_saves: bool
    saves_verified_migrated: bool = False
    matched_game: str = ""
    game_hint: str = ""


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        cur = Path(dirpath)
        for fname in filenames:
            fp = cur / fname
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                continue
    return total


_PROGRAMDATA_SKIP_DIRS = {
    "microsoft",
    "package cache",
    "packages",
    "usoshared",
    "ssh",
    "temp",
    "nvidia",
    "amd",
}


def _prefix_programdata_dir(prefix_dir: Path) -> Path | None:
    try:
        base_resolved = prefix_dir.resolve()
    except OSError:
        return None
    for rel in (Path("pfx") / "drive_c" / "ProgramData", Path("drive_c") / "ProgramData"):
        if not _safe_parents_within(prefix_dir, base_resolved, rel.parts):
            continue
        cand = prefix_dir / rel
        if cand.is_dir():
            return cand
    return None


_DRIVE_C_SYSTEM_DIRS = {
    "windows",
    "program files",
    "program files (x86)",
    "users",
    "programdata",
    "vr",
}


def _prefix_has_unconventional_saves(prefix_dir: Path, src_users: Path | None = None) -> bool:
    """Return True if drive_c contains non-symlink files outside standard Windows user/system dirs."""
    users_dir = src_users if src_users is not None else _prefix_users_dir(prefix_dir)
    if users_dir is None:
        return True
    drive_c = users_dir.parent
    if drive_c.is_symlink() or not drive_c.is_dir():
        return True
    try:
        for sub in drive_c.iterdir():
            if sub.is_symlink() or not sub.is_dir() or sub.name.lower() in _DRIVE_C_SYSTEM_DIRS:
                continue
            for dirpath, _, filenames in os.walk(sub, followlinks=False, onerror=_raise_oserror):
                cur = Path(dirpath)
                for fname in filenames:
                    fp = cur / fname
                    if not fp.is_symlink() and fp.is_file():
                        return True
    except OSError:
        return True
    return False


def _prefix_has_saves(prefix_dir: Path) -> bool:
    src_users = _prefix_users_dir(prefix_dir)
    if src_users is None:
        return True
    dummy_dst = prefix_dir / "__unused__"
    try:
        for src_file, _ in _iter_prefix_save_pairs(
            src_users, dummy_dst, raise_on_error=True
        ):
            if src_file.stat().st_size > 0:
                return True
        prog_data = _prefix_programdata_dir(prefix_dir)
        if prog_data is not None:
            for src_file, _ in _iter_programdata_save_pairs(
                prog_data, dummy_dst, raise_on_error=True
            ):
                if src_file.stat().st_size > 0:
                    return True
        if _prefix_has_unconventional_saves(prefix_dir, src_users):
            return True
    except OSError:
        return True
    return False


_GENERIC_GAME_HINT_DIRS = _PREFIX_SKIP_DIRS | {
    "unrealengine",
    "unity",
    "gse saves",
    "steam",
    "valve",
    "vulkan",
    "goldberg steamemu saves",
    "empress",
    "rune",
    "codex",
    "saved",
    "config",
    "default",
}


def _infer_prefix_game_name(prefix_dir: Path) -> str:
    """Best-effort inference of the game name from save/AppData folders inside a Wine prefix."""
    src_users = _prefix_users_dir(prefix_dir)
    if src_users is None:
        return ""
    try:
        user_dirs = [d for d in src_users.iterdir() if d.is_dir() and d.name.lower() != "public"]
    except OSError:
        return ""
    for user_dir in user_dirs:
        candidates_roots = [
            (user_dir / "Documents" / "My Games", False),
            (user_dir / "Saved Games", False),
            (user_dir / "AppData" / "LocalLow", True),
            (user_dir / "AppData" / "Local", False),
            (user_dir / "AppData" / "Roaming", False),
        ]
        for root_dir, check_sub in candidates_roots:
            if not root_dir.is_dir():
                continue
            try:
                subdirs = sorted(
                    d for d in root_dir.iterdir()
                    if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in _GENERIC_GAME_HINT_DIRS
                )
            except OSError:
                continue
            for sd in subdirs:
                if check_sub:
                    try:
                        inner = sorted(
                            c for c in sd.iterdir()
                            if c.is_dir() and not c.name.startswith(".") and c.name.lower() not in _GENERIC_GAME_HINT_DIRS
                        )
                    except OSError:
                        inner = []
                    if inner:
                        for inn in inner:
                            if any(p.is_file() for p in inn.rglob("*")):
                                return inn.name
                if any(p.is_file() for p in sd.rglob("*")):
                    return sd.name
    return ""


def _verify_save_pair_exact(
    src_file: Path,
    dst_file: Path,
    src_prefix_resolved: Path | None = None,
) -> bool:
    """Return True if dst_file is an independent, non-symlink file with identical content."""
    if dst_file.is_symlink() or not dst_file.is_file():
        return False
    dst_resolved = dst_file.resolve()
    if os.path.samefile(src_file, dst_file) or src_file.resolve() == dst_resolved:
        return False
    if src_prefix_resolved is not None and dst_resolved.is_relative_to(src_prefix_resolved):
        return False
    src_stat = src_file.stat()
    dst_stat = dst_file.stat()
    if dst_stat.st_size == 0 or dst_stat.st_size != src_stat.st_size:
        return False
    return _file_sha256(src_file) == _file_sha256(dst_file)


def _prefix_saves_verified_in_target(src_prefix: Path, dst_compatdata: Path) -> bool:
    """Return True if src_prefix has >=1 non-empty save and all are present, identical, and independent in dst."""
    src_users = _prefix_users_dir(src_prefix)
    dst_users = _prefix_users_dir(dst_compatdata)
    if src_users is None or dst_users is None:
        return False
    if _prefix_has_unconventional_saves(src_prefix, src_users):
        return False
    any_nonempty = False
    try:
        src_prefix_resolved = src_prefix.resolve()
        src_progdata = _prefix_programdata_dir(src_prefix)
        if src_progdata is not None:
            dst_progdata = _prefix_programdata_dir(dst_compatdata)
            if dst_progdata is None:
                for fp, _ in _iter_programdata_save_pairs(
                    src_progdata, src_progdata / "__unused__", raise_on_error=True
                ):
                    if fp.stat().st_size > 0:
                        return False
            else:
                for src_file, dst_file in _iter_programdata_save_pairs(
                    src_progdata, dst_progdata, raise_on_error=True
                ):
                    if src_file.stat().st_size == 0:
                        continue
                    any_nonempty = True
                    if not _verify_save_pair_exact(src_file, dst_file, src_prefix_resolved):
                        return False
        for src_file, dst_file in _iter_prefix_save_pairs(
            src_users, dst_users, raise_on_error=True
        ):
            if src_file.stat().st_size == 0:
                continue
            any_nonempty = True
            if not _verify_save_pair_exact(src_file, dst_file, src_prefix_resolved):
                return False
    except OSError:
        return False
    return any_nonempty


def _entry_steam_appid(entry: ShortcutEntry) -> int | None:
    start_str = _strip_vdf_quotes(entry.start_dir)
    if start_str:
        sa = detect_steam_appid(Path(start_str))
        if sa:
            return sa
    exe_str = _strip_vdf_quotes(entry.exe)
    if exe_str:
        exe_path = Path(exe_str)
        sa = detect_steam_appid(exe_path.parent)
        if sa:
            return sa
        try:
            return detect_steam_appid(infer_game_root_from_exe(exe_path))
        except Exception:
            pass
    return None


def sync_all_external_saves(
    paths: SteamPaths,
    extra_external_roots: list[Path] | None = None,
) -> tuple[int, int]:
    """Import any pending/newer saves from external prefixes into all matching active shortcuts.

    Returns (shortcuts_checked, save_files_copied).
    """
    entries = load_shortcuts(paths.shortcuts_vdf)
    checked = 0
    copied = 0
    for entry in entries:
        exe_clean = _strip_vdf_quotes(entry.exe)
        if not exe_clean.lower().endswith(".exe") and not (
            paths.compatdata_dir / str(entry.appid_u32)
        ).is_dir():
            continue
        checked += 1
        steam_appid = _entry_steam_appid(entry)
        roots = extra_external_roots
        if roots is None:
            roots = external_prefix_roots()
            if steam_appid and paths.compatdata_dir.is_dir():
                roots = [*roots, paths.compatdata_dir]
        try:
            copied += import_external_prefix_saves(
                paths=paths,
                shortcut_appid_u32=entry.appid_u32,
                game_name=entry.app_name,
                steam_appid=steam_appid,
                search_roots=roots,
            )
        except Exception as exc:
            logging.warning("Save sync skipped for %s: %s", entry.app_name, exc)
    return checked, copied


def _installed_steam_appids(steam_root: Path) -> set[int]:
    """Return installed Steam AppIDs and core runtime IDs from appmanifest_*.acf files."""
    appids: set[int] = {0, 228980, 1070560, 1391110, 1493710, 1628350, 1887720}
    steamapps_dirs = [steam_root / "steamapps"]
    lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if lib_vdf.is_file():
        try:
            data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for val in folders.values():
                if isinstance(val, dict) and val.get("path"):
                    steamapps_dirs.append(Path(str(val["path"])) / "steamapps")
        except Exception:
            pass
    for sa in steamapps_dirs:
        if not sa.is_dir():
            continue
        for acf in sa.glob("appmanifest_*.acf"):
            stem_id = acf.stem.removeprefix("appmanifest_")
            if stem_id.isascii() and stem_id.isdigit():
                appids.add(int(stem_id))
    return appids


def _is_prefix_referenced_by_shortcuts(
    prefix_resolved: Path, entries: list[ShortcutEntry]
) -> bool:
    """Return True if any shortcut exe, start_dir, icon, or launch_options points inside prefix_resolved."""
    prefix_str = str(prefix_resolved)
    home_str = str(Path.home())
    for entry in entries:
        lo_expanded = entry.launch_options.replace("$HOME", home_str).replace("${HOME}", home_str)
        if prefix_str and (prefix_str in entry.launch_options or prefix_str in lo_expanded):
            return True
        try:
            for tok in shlex.split(lo_expanded):
                val = tok.split("=", 1)[1] if ("=" in tok and not tok.startswith("-")) else tok
                if val.startswith("~"):
                    val = os.path.expanduser(val)
                if val.startswith("/"):
                    try:
                        rp = Path(val).resolve()
                        if rp == prefix_resolved or rp.is_relative_to(prefix_resolved):
                            return True
                    except OSError:
                        pass
        except ValueError:
            pass
        for raw in (entry.exe, entry.start_dir, entry.icon):
            cleaned = _strip_vdf_quotes(raw)
            if not cleaned:
                continue
            try:
                p = Path(cleaned).resolve()
                if p == prefix_resolved or p.is_relative_to(prefix_resolved):
                    return True
            except OSError:
                continue
    return False


def _active_compat_tools(config_vdf: Path, active_appids: set[int]) -> set[str]:
    """Return compatibility tool names currently mapped to active games/shortcuts."""
    used: set[str] = set()
    if not config_vdf.is_file():
        return used
    try:
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        mapping = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
        )
        if isinstance(mapping, dict):
            for k, val in mapping.items():
                if not (str(k).isascii() and str(k).isdigit()):
                    continue
                if int(k) in active_appids and isinstance(val, dict):
                    tname = str(val.get("name") or "").strip()
                    if tname:
                        used.add(tname)
    except Exception:
        pass
    return used


_GPU_SHADER_CACHE_NAMES = frozenset(
    {"mesa_shader_cache", "mesa_shader_cache_db", "radv_builtin_shaders", "nvidia", "amd"}
)
_OLD_PROTON_PREFIXES = (
    "proton_7",
    "proton_8",
    "proton_9",
    "proton_10",
    "proton_experimental",
    "proton_hotfix",
    "UMU-Proton",
)


def _all_user_shortcuts(paths: SteamPaths) -> tuple[list[ShortcutEntry], list[ShortcutEntry]]:
    """Return (current_user_entries, deduplicated_all_user_entries) for Steam shortcuts."""
    current_entries = load_shortcuts(paths.shortcuts_vdf)
    all_users = list_steam_users(paths.steam_root)
    all_user_entries: list[ShortcutEntry] = list(current_entries)
    if all_users:
        seen_entry_keys = {
            (e.appid_u32, e.exe, e.start_dir, e.icon, e.launch_options)
            for e in current_entries
        }
        for uid in all_users:
            sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
            for e in load_shortcuts(sv):
                key = (e.appid_u32, e.exe, e.start_dir, e.icon, e.launch_options)
                if key not in seen_entry_keys:
                    all_user_entries.append(e)
                    seen_entry_keys.add(key)
    return current_entries, all_user_entries


def collect_active_proton_tools(
    paths: SteamPaths | None,
    games_dir: Path | None = None,
) -> set[str]:
    """Return compatibility tool names in use by Steam games/shortcuts and standalone games."""
    from .standalone import list_standalone_games

    tools: set[str] = set()
    if paths is not None:
        _, all_user_entries = _all_user_shortcuts(paths)
        active_shortcuts = {e.appid_u32 for e in all_user_entries}
        installed_steam = _installed_steam_appids(paths.steam_root)
        tools.update(_active_compat_tools(paths.config_vdf, active_shortcuts | installed_steam))
    for game in list_standalone_games(games_dir):
        if game.proton_name:
            tools.add(game.proton_name)
    return tools


def _tool_uses_sniper_or_old_proton(tool_name: str) -> bool:
    """Return True if a compatibility tool requires steamrt3/sniper or Proton <= 10."""
    if tool_name.startswith(_OLD_PROTON_PREFIXES):
        return True
    if tool_name.startswith("GE-Proton"):
        match = re.match(r"^GE-Proton(\d+)", tool_name)
        if match is None:
            return True
        return int(match.group(1)) < 11
    return False


def _newest_ge_proton11_paths(compat_tools: Path) -> set[Path]:
    """Return resolved paths for the newest installed GE-Proton11+ directory."""
    from .runtime_manager import _parse_ge_proton_version, list_ge_proton11_dirs

    preserved: set[Path] = set()
    global_ge11 = list_ge_proton11_dirs()
    if global_ge11:
        try:
            preserved.add(global_ge11[0].resolve())
        except OSError:
            pass
    if compat_tools.is_dir():
        found: list[tuple[tuple[int, ...], Path]] = []
        try:
            entries = list(compat_tools.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
            except OSError:
                continue
            ver = _parse_ge_proton_version(entry.name)
            if ver is not None:
                found.append((ver, entry))
        if found:
            found.sort(key=lambda item: item[0], reverse=True)
            try:
                preserved.add(found[0][1].resolve())
            except OSError:
                pass
    return preserved


def _is_orphan_desktop_file(desktop_path: Path) -> bool:
    """Return True if a deckinstall-*.desktop file points to a missing Exec= or Path=."""
    try:
        text = desktop_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    exec_val = ""
    path_val = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Exec=") and not exec_val:
            exec_val = stripped[5:].strip()
        elif stripped.startswith("Path=") and not path_val:
            path_val = stripped[5:].strip()
    if not exec_val:
        return True
    try:
        tokens = shlex.split(exec_val)
    except ValueError:
        tokens = [exec_val.strip('"')]
    if not tokens:
        return True
    if not Path(tokens[0]).exists():
        return True
    if path_val and not Path(path_val).exists():
        return True
    return False


def find_cleanup_candidates(
    paths: SteamPaths | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
) -> list[CleanupCandidate]:
    """Find old external prefixes, orphan Steam compatdata/shadercache, backups, and optional system caches."""
    from .standalone import get_games_dir, list_standalone_games

    candidates: list[CleanupCandidate] = []
    seen: set[Path] = set()

    current_entries: list[ShortcutEntry] = []
    all_user_entries: list[ShortcutEntry] = []
    if paths is not None:
        current_entries, all_user_entries = _all_user_shortcuts(paths)

    by_norm_key: dict[str, ShortcutEntry] = {}
    by_steam_appid: dict[str, ShortcutEntry] = {}
    by_appid_u32: dict[int, ShortcutEntry] = {}
    for entry in current_entries:
        by_appid_u32[entry.appid_u32] = entry
        nk = _norm_key(entry.app_name)
        if nk and nk not in by_norm_key:
            by_norm_key[nk] = entry
        sa = _entry_steam_appid(entry)
        if sa:
            by_steam_appid.setdefault(str(sa), entry)
            by_steam_appid.setdefault(f"umu-{sa}", entry)

    home = home_dir or Path.home()
    data_home = (
        (home / ".local" / "share")
        if home_dir is not None
        else Path(os.environ.get("XDG_DATA_HOME", "").strip() or (home / ".local" / "share")).expanduser()
    )
    cache_home = (
        (home / ".cache")
        if home_dir is not None
        else Path(os.environ.get("XDG_CACHE_HOME", "").strip() or (home / ".cache")).expanduser()
    )
    games_dir = (home_dir / "Games") if home_dir is not None else get_games_dir()
    standalone_prefixes_dir = games_dir / ".prefixes"
    standalone_prefix_paths: set[Path] = set()
    for game in list_standalone_games(games_dir):
        try:
            standalone_prefix_paths.add(game.prefix_dir.resolve())
        except OSError:
            pass
    if games_dir.is_dir():
        try:
            for gdir in games_dir.iterdir():
                if not gdir.is_dir() or gdir.name.startswith("."):
                    continue
                manifest = gdir / ".deckinstall_standalone.json"
                if manifest.is_file():
                    try:
                        mdata = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                        p_raw = mdata.get("prefix_dir")
                        if p_raw:
                            standalone_prefix_paths.add(Path(str(p_raw)).resolve())
                    except Exception:
                        pass
                    if standalone_prefixes_dir.is_dir():
                        try:
                            standalone_prefix_paths.add((standalone_prefixes_dir / gdir.name).resolve())
                        except OSError:
                            pass
        except OSError:
            pass

    if extra_external_roots is not None:
        roots = list(extra_external_roots)
    else:
        roots = list(external_prefix_roots()) + [standalone_prefixes_dir]

    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        is_standalone_prefixes_root = False
        if standalone_prefixes_dir.is_dir():
            try:
                is_standalone_prefixes_root = root.resolve() == standalone_prefixes_dir.resolve()
            except OSError:
                pass
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            if _prefix_users_dir(child) is None and not is_standalone_prefixes_root:
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved in standalone_prefix_paths:
                continue
            if is_standalone_prefixes_root and (
                (games_dir / child.name / ".deckinstall_standalone.json").is_file()
                or (games_dir / child.name).is_dir()
            ):
                continue
            if _is_prefix_referenced_by_shortcuts(resolved, all_user_entries):
                continue
            sz = _dir_size_bytes(child)
            if is_standalone_prefixes_root and _prefix_users_dir(child) is None and sz == 0:
                continue
            matched_entry = by_steam_appid.get(child.name) or by_norm_key.get(_norm_key(child.name))
            has_saves = _prefix_has_saves(child)
            saves_verified = False
            if has_saves and matched_entry is not None and paths is not None:
                dst_compat = paths.compatdata_dir / str(matched_entry.appid_u32)
                saves_verified = _prefix_saves_verified_in_target(child, dst_compat)
            matched_name = matched_entry.app_name if matched_entry else ""
            candidates.append(
                CleanupCandidate(
                    category="Prefixo Externo",
                    label=f"{root.parent.name}/{root.name}/{child.name}",
                    path=child,
                    size_bytes=sz,
                    has_saves=has_saves,
                    saves_verified_migrated=saves_verified,
                    matched_game=matched_name,
                    game_hint=matched_name or _infer_prefix_game_name(child),
                )
            )

    if paths is not None:
        active_shortcuts = {e.appid_u32 for e in all_user_entries}
        installed_steam = _installed_steam_appids(paths.steam_root)
        if paths.compatdata_dir.is_dir():
            for child in paths.compatdata_dir.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                if child.name.endswith(".deckinstall.bak"):
                    try:
                        if _is_prefix_referenced_by_shortcuts(child.resolve(), all_user_entries):
                            continue
                    except OSError:
                        continue
                    prefix_id = child.name.split(".", 1)[0]
                    matched_entry = (
                        by_appid_u32.get(int(prefix_id))
                        if (prefix_id.isascii() and prefix_id.isdigit())
                        else None
                    )
                    has_saves = _prefix_has_saves(child)
                    saves_verified = False
                    if has_saves and matched_entry is not None:
                        saves_verified = _prefix_saves_verified_in_target(
                            child, paths.compatdata_dir / prefix_id
                        )
                    matched_name = matched_entry.app_name if matched_entry else ""
                    candidates.append(
                        CleanupCandidate(
                            category="Backup",
                            label=child.name,
                            path=child,
                            size_bytes=_dir_size_bytes(child),
                            has_saves=has_saves,
                            saves_verified_migrated=saves_verified,
                            matched_game=matched_name,
                            game_hint=matched_name or _infer_prefix_game_name(child),
                        )
                    )
                    continue
                if not (child.name.isascii() and child.name.isdigit()):
                    continue
                appid = int(child.name)
                if appid in active_shortcuts or appid in installed_steam:
                    continue
                try:
                    if _is_prefix_referenced_by_shortcuts(child.resolve(), all_user_entries):
                        continue
                except OSError:
                    continue
                has_saves = _prefix_has_saves(child)
                saves_verified = False
                matched_game = ""
                if has_saves:
                    for entry in current_entries:
                        dst_compat = paths.compatdata_dir / str(entry.appid_u32)
                        if dst_compat.is_dir() and _prefix_saves_verified_in_target(child, dst_compat):
                            saves_verified = True
                            matched_game = entry.app_name
                            break
                candidates.append(
                    CleanupCandidate(
                        category="Compatdata Órfão",
                        label=f"compatdata/{child.name}",
                        path=child,
                        size_bytes=_dir_size_bytes(child),
                        has_saves=has_saves,
                        saves_verified_migrated=saves_verified,
                        matched_game=matched_game,
                        game_hint=matched_game or _infer_prefix_game_name(child),
                    )
                )

        shadercache_dir = paths.steam_root / "steamapps" / "shadercache"
        if shadercache_dir.is_dir():
            for child in shadercache_dir.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                if not (child.name.isascii() and child.name.isdigit()):
                    continue
                appid = int(child.name)
                if appid == 0 or appid in active_shortcuts or appid in installed_steam:
                    continue
                sz = _dir_size_bytes(child)
                if sz > 0:
                    candidates.append(
                        CleanupCandidate(
                            category="Orphan Shadercache",
                            label=f"shadercache/{child.name}",
                            path=child,
                            size_bytes=sz,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        )
                    )

        steam_logs_dir = paths.steam_root / "logs"
        if steam_logs_dir.is_dir() and not steam_logs_dir.is_symlink():
            sz = _dir_size_bytes(steam_logs_dir)
            if sz >= 1024 * 1024:
                candidates.append(
                    CleanupCandidate(
                        category="Steam Logs",
                        label="Steam/logs",
                        path=steam_logs_dir,
                        size_bytes=sz,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    )
                )

    apps_dir = data_home / "applications"
    if apps_dir.is_dir() and not apps_dir.is_symlink():
        for child in apps_dir.iterdir():
            if child.is_symlink() or not child.is_file():
                continue
            if not (child.name.startswith("deckinstall-") and child.name.endswith(".desktop")):
                continue
            if _is_orphan_desktop_file(child):
                try:
                    sz = child.stat().st_size
                except OSError:
                    continue
                candidates.append(
                    CleanupCandidate(
                        category="Orphan Desktop",
                        label=child.name,
                        path=child,
                        size_bytes=sz,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    )
                )

    backups_dir = home / "Backups" / "DeckInstall"
    if backups_dir.is_dir() and not backups_dir.is_symlink():
        save_zips: list[tuple[float, str, Path, int]] = []
        for child in backups_dir.iterdir():
            if child.is_symlink() or not child.is_file():
                continue
            if not (child.name.startswith("saves-") and child.name.endswith(".zip")):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            save_zips.append((st.st_mtime, child.name, child, st.st_size))
        save_zips.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _, _, old_zip, sz in save_zips[3:]:
            candidates.append(
                CleanupCandidate(
                    category="Backup",
                    label=old_zip.name,
                    path=old_zip,
                    size_bytes=sz,
                    has_saves=False,
                    saves_verified_migrated=False,
                    matched_game="",
                    game_hint="",
                )
            )

    if include_system_caches:
        trash_files = data_home / "Trash" / "files"
        if trash_files.is_dir() and not trash_files.is_symlink():
            sz = _dir_size_bytes(trash_files)
            if sz > 0:
                candidates.append(
                    CleanupCandidate(
                        category="Trash",
                        label="Trash/files",
                        path=trash_files,
                        size_bytes=sz,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    )
                )

        user_cache = cache_home
        if user_cache.is_dir() and not user_cache.is_symlink():
            for child in user_cache.iterdir():
                if child.is_symlink() or child.name in ("fontconfig", "dconf"):
                    continue
                try:
                    sz = _dir_size_bytes(child) if child.is_dir() else child.stat().st_size
                except OSError:
                    continue
                if child.name in _GPU_SHADER_CACHE_NAMES:
                    if sz > 0:
                        candidates.append(
                            CleanupCandidate(
                                category="GPU Shader Cache",
                                label=f".cache/{child.name}",
                                path=child,
                                size_bytes=sz,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            )
                        )
                elif sz >= 5 * 1024 * 1024:
                    candidates.append(
                        CleanupCandidate(
                            category="App Cache",
                            label=f".cache/{child.name}",
                            path=child,
                            size_bytes=sz,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        )
                    )

        nv_compute = home / ".nv" / "ComputeCache"
        if nv_compute.is_dir() and not nv_compute.is_symlink():
            sz = _dir_size_bytes(nv_compute)
            if sz > 0:
                candidates.append(
                    CleanupCandidate(
                        category="GPU Shader Cache",
                        label=".nv/ComputeCache",
                        path=nv_compute,
                        size_bytes=sz,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    )
                )

        var_app = home / ".var" / "app"
        if var_app.is_dir() and not var_app.is_symlink():
            for app_dir in var_app.iterdir():
                if app_dir.is_symlink() or not app_dir.is_dir():
                    continue
                cdir = app_dir / "cache"
                if cdir.is_dir() and not cdir.is_symlink():
                    sz = _dir_size_bytes(cdir)
                    if sz >= 5 * 1024 * 1024:
                        candidates.append(
                            CleanupCandidate(
                                category="App Cache",
                                label=f"Flatpak/{app_dir.name}/cache",
                                path=cdir,
                                size_bytes=sz,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            )
                        )

        active_tools = collect_active_proton_tools(paths, games_dir=games_dir)
        sniper_in_use = any(_tool_uses_sniper_or_old_proton(t) for t in active_tools)
        compat_tools = (
            paths.steam_root / "compatibilitytools.d"
            if paths is not None
            else data_home / "Steam" / "compatibilitytools.d"
        )
        compat_dirs: list[Path] = [compat_tools]
        raw_extra_compat = os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").strip()
        if raw_extra_compat:
            for part in raw_extra_compat.split(os.pathsep):
                cleaned = part.strip()
                if cleaned:
                    compat_dirs.append(Path(cleaned).expanduser())

        seen_compat_dirs: set[Path] = set()
        for compat_dir in compat_dirs:
            if not compat_dir.is_dir():
                continue
            try:
                resolved_compat = compat_dir.resolve()
            except OSError:
                resolved_compat = compat_dir
            if resolved_compat in seen_compat_dirs:
                continue
            seen_compat_dirs.add(resolved_compat)

            preserved_ge11 = _newest_ge_proton11_paths(compat_dir)
            for child in compat_dir.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                if child.name in active_tools:
                    continue
                is_old_umu = child.name.startswith("UMU-Proton-")
                is_old_ge = False
                if child.name.startswith("GE-Proton"):
                    try:
                        is_old_ge = child.resolve() not in preserved_ge11
                    except OSError:
                        is_old_ge = False
                if is_old_umu or is_old_ge:
                    sz = _dir_size_bytes(child)
                    if sz > 0:
                        candidates.append(
                            CleanupCandidate(
                                category="Old Proton/Runtime",
                                label=f"{compat_dir.name}/{child.name}",
                                path=child,
                                size_bytes=sz,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            )
                        )

        umu_dir = data_home / "umu"
        steamrt3 = umu_dir / "steamrt3"
        if (
            not sniper_in_use
            and (umu_dir / "steamrt4" / "toolmanifest.vdf").is_file()
            and steamrt3.is_dir()
            and not steamrt3.is_symlink()
        ):
            sz = _dir_size_bytes(steamrt3)
            if sz > 0:
                candidates.append(
                    CleanupCandidate(
                        category="Old Proton/Runtime",
                        label="umu/steamrt3",
                        path=steamrt3,
                        size_bytes=sz,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    )
                )

        if paths is not None:
            common_dir = paths.steam_root / "steamapps" / "common"
            if (
                (common_dir / "Proton 11.0").is_dir()
                and (common_dir / "Proton 10.0").is_dir()
                and not any(t.startswith("proton_10") for t in active_tools)
            ):
                p10 = common_dir / "Proton 10.0"
                if not p10.is_symlink():
                    sz = _dir_size_bytes(p10)
                    if sz > 0:
                        candidates.append(
                            CleanupCandidate(
                                category="Old Proton/Runtime",
                                label="common/Proton 10.0",
                                path=p10,
                                size_bytes=sz,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            )
                        )
            if (
                not sniper_in_use
                and (common_dir / "SteamLinuxRuntime_4").is_dir()
                and (common_dir / "SteamLinuxRuntime_sniper").is_dir()
            ):
                sniper = common_dir / "SteamLinuxRuntime_sniper"
                if not sniper.is_symlink():
                    sz = _dir_size_bytes(sniper)
                    if sz > 0:
                        candidates.append(
                            CleanupCandidate(
                                category="Old Proton/Runtime",
                                label="common/SteamLinuxRuntime_sniper",
                                path=sniper,
                                size_bytes=sz,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            )
                        )

    candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return candidates


def delete_cleanup_candidates(candidates: list[CleanupCandidate]) -> tuple[int, int]:
    """Delete selected cleanup directories/files and return (deleted_count, freed_bytes)."""
    deleted = 0
    freed = 0
    for item in candidates:
        if not item.path.exists():
            continue
        try:
            if item.path.is_dir() and not item.path.is_symlink():
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
            deleted += 1
            freed += item.size_bytes
        except OSError:
            continue
    return deleted, freed


def delete_cleanup_candidates_checked(
    paths: SteamPaths | None = None,
    candidates: list[CleanupCandidate] | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
) -> tuple[int, int, list[str]]:
    """Revalidate each candidate immediately before deleting it and return (deleted, freed, errors)."""
    deleted = 0
    freed = 0
    errors: list[str] = []
    home = home_dir or Path.home()
    lock_ctx = (
        _steam_transaction_lock(paths.shortcuts_vdf)
        if paths is not None
        else _file_lock(home / ".local" / "share" / "DeckInstall" / "cleanup.lock")
    )
    with lock_ctx:
        fresh_by_path = {
            c.path.resolve(): c
            for c in find_cleanup_candidates(
                paths,
                extra_external_roots=extra_external_roots,
                include_system_caches=include_system_caches,
                home_dir=home_dir,
            )
        }
        for item in candidates or []:
            if not item.path.exists():
                continue
            try:
                resolved = item.path.resolve()
            except OSError as exc:
                errors.append(f"{item.label}: {exc}")
                continue
            fresh = fresh_by_path.get(resolved)
            if fresh is None:
                errors.append(f"{item.label}: skipped for safety (no longer orphan/safe).")
                continue
            if fresh.has_saves and not fresh.saves_verified_migrated:
                errors.append(f"{item.label}: skipped for safety (unvalidated saves).")
                continue
            try:
                if item.path.is_dir() and not item.path.is_symlink():
                    shutil.rmtree(item.path)
                else:
                    item.path.unlink()
                if item.label == "common/SteamLinuxRuntime_sniper" and paths is not None:
                    acf = paths.steam_root / "steamapps" / "appmanifest_1628350.acf"
                    if acf.is_file() and not acf.is_symlink():
                        acf.unlink()
                deleted += 1
                freed += item.size_bytes
            except OSError as exc:
                errors.append(f"{item.label}: {exc}")
    return deleted, freed, errors


_RESTORE_MAX_FILE_BYTES = 512 * 1024 * 1024
_RESTORE_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_RESTORE_MAX_ENTRIES = 20_000


def _reserve_unique_backup_zip(out_dir: Path, stamp: str) -> Path:
    """Atomically reserve a unique destination path for the backup ZIP."""
    candidates = [out_dir / f"saves-{stamp}.zip"] + [
        out_dir / f"saves-{stamp}-{idx}.zip" for idx in range(1, 1000)
    ]
    for cand in candidates:
        try:
            fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return cand
        except FileExistsError:
            continue
    cand = out_dir / f"saves-{stamp}-{int(time.time() * 1000)}-{os.getpid()}.zip"
    fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    return cand


def _write_prefix_saves_to_zip(
    zf: zipfile.ZipFile,
    prefix_dir: Path,
    key_prefix: str,
    files_backed: int,
    total_uncompressed: int,
) -> tuple[int, int, int]:
    """Write saves from a single Wine/Proton prefix into zf; return (game_files, files_backed, total_uncompressed)."""
    game_files = 0

    def _add_save_pairs(pairs: list[tuple[Path, Path]], dummy_base: Path, subfolder: str) -> None:
        nonlocal game_files, files_backed, total_uncompressed
        for src_file, dst_rel in pairs:
            fsize = src_file.stat().st_size
            if fsize == 0:
                continue
            if files_backed + 1 >= _RESTORE_MAX_ENTRIES:
                raise RuntimeError(f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
            if fsize > _RESTORE_MAX_FILE_BYTES or total_uncompressed + fsize > _RESTORE_MAX_TOTAL_BYTES:
                raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
            rel = dst_rel.relative_to(dummy_base)
            arcname = f"{key_prefix}/{subfolder}/{rel.as_posix()}"
            sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(sfd, "rb") as sfh:
                zf.writestr(arcname, sfh.read())
            total_uncompressed += fsize
            game_files += 1
            files_backed += 1

    src_users = _prefix_users_dir(prefix_dir)
    if src_users is not None:
        dummy_dst = prefix_dir / "__bak_rel__"
        _add_save_pairs(
            _iter_prefix_save_pairs(src_users, dummy_dst, preserve_user_dirs=True, raise_on_error=True),
            dummy_dst,
            "users",
        )
    src_progdata = _prefix_programdata_dir(prefix_dir)
    if src_progdata is not None:
        dummy_pd = prefix_dir / "__bak_pd_rel__"
        _add_save_pairs(
            _iter_programdata_save_pairs(src_progdata, dummy_pd, raise_on_error=True),
            dummy_pd,
            "ProgramData",
        )
    return game_files, files_backed, total_uncompressed


def backup_all_saves(
    paths: SteamPaths | None = None,
    dest_root: Path | None = None,
    games_dir: Path | None = None,
) -> tuple[Path, int, int]:
    """Create a timestamped ZIP backup of saves for all active Non-Steam shortcuts and standalone games."""
    from .standalone import list_standalone_games

    out_dir = dest_root or (Path.home() / "Backups" / "DeckInstall")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_path = _reserve_unique_backup_zip(out_dir, stamp)
    entries = load_shortcuts(paths.shortcuts_vdf) if paths is not None else []
    standalone_games = list_standalone_games(games_dir)
    manifest: dict[str, str] = {}
    games_backed = 0
    files_backed = 0
    total_uncompressed = 0

    fd, tmp_name = tempfile.mkstemp(prefix=".saves-", suffix=".zip.tmp", dir=str(out_dir))
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if paths is not None:
                for entry in entries:
                    compat_dir = paths.compatdata_dir / str(entry.appid_u32)
                    game_files, files_backed, total_uncompressed = _write_prefix_saves_to_zip(
                        zf, compat_dir, str(entry.appid_u32), files_backed, total_uncompressed
                    )
                    if game_files > 0:
                        manifest[str(entry.appid_u32)] = entry.app_name
                        games_backed += 1
            for game in standalone_games:
                slug_base = re.sub(r"[^a-zA-Z0-9._-]+", "-", game.game_dir.name).strip("-") or "game"
                key = f"standalone-{slug_base}"
                idx = 2
                while key in manifest:
                    key = f"standalone-{slug_base}-{idx}"
                    idx += 1
                game_files, files_backed, total_uncompressed = _write_prefix_saves_to_zip(
                    zf, game.prefix_dir, key, files_backed, total_uncompressed
                )
                if game_files > 0:
                    manifest[key] = game.name
                    games_backed += 1
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        os.replace(tmp_zip, zip_path)
    except Exception:
        for p in (tmp_zip, zip_path):
            try:
                if p.exists() and p.stat().st_size == 0:
                    p.unlink()
            except OSError:
                pass
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        raise
    return zip_path, games_backed, files_backed


def _safe_parents_within(base_dir: Path, base_resolved: Path, rel_parts: tuple[str, ...]) -> bool:
    cur = base_dir
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            return False
        if cur.exists():
            try:
                if not cur.resolve().is_relative_to(base_resolved):
                    return False
            except OSError:
                return False
    return True


def restore_saves_backup(paths: SteamPaths, zip_path: Path) -> tuple[int, int]:
    """Restore saves from a DeckInstall ZIP backup into matching Steam compatdata folders."""
    with _steam_transaction_lock(paths.shortcuts_vdf):
        entries = load_shortcuts(paths.shortcuts_vdf)
        active_ids = {str(e.appid_u32) for e in entries}
        by_exact = {e.app_name: str(e.appid_u32) for e in entries}
        norm_counts: dict[str, list[str]] = {}
        for e in entries:
            nk = _norm_key(e.app_name)
            if nk:
                norm_counts.setdefault(nk, []).append(str(e.appid_u32))
        by_norm = {nk: ids[0] for nk, ids in norm_counts.items() if len(ids) == 1}

        paths.compatdata_dir.mkdir(parents=True, exist_ok=True)
        compat_root_resolved = paths.compatdata_dir.resolve()
        restored_games: set[str] = set()
        restored_files = 0
        total_bytes = 0
        backed_up_targets: set[Path] = set()
        with zipfile.ZipFile(zip_path, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > _RESTORE_MAX_ENTRIES:
                raise RuntimeError(f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
            manifest: dict[str, str] = {}
            try:
                minfo = zf.getinfo("manifest.json")
            except KeyError:
                minfo = None
            if minfo is not None:
                if minfo.file_size > _MANIFEST_MAX_BYTES:
                    raise ValueError("manifest.json exceeds size limit")
                chunks: list[bytes] = []
                read_len = 0
                with zf.open(minfo, "r") as mfh:
                    while chunk := mfh.read(65536):
                        read_len += len(chunk)
                        if read_len > _MANIFEST_MAX_BYTES:
                            raise ValueError("manifest.json exceeds size limit")
                        chunks.append(chunk)
                raw_manifest = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
                if not isinstance(raw_manifest, dict):
                    raise ValueError("Invalid manifest.json format")
                manifest = {str(k): str(v) for k, v in raw_manifest.items()}

            for info in infolist:
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                parts = Path(info.filename).parts
                if (
                    len(parts) < 3
                    or not (parts[0].isascii() and parts[0].isdigit())
                    or parts[1] not in ("users", "ProgramData")
                ):
                    continue
                appid_str = parts[0]
                if appid_str not in active_ids:
                    if manifest:
                        old_name = manifest.get(appid_str, "")
                        mapped = by_exact.get(old_name) or by_norm.get(_norm_key(old_name))
                        if not mapped:
                            continue
                        appid_str = mapped
                    else:
                        continue
                section = parts[1]
                rel_parts = parts[2:]
                if any(p in ("..", "", ".") for p in rel_parts):
                    continue
                if info.file_size > _RESTORE_MAX_FILE_BYTES:
                    raise RuntimeError(f"Backup file too large: {info.filename}")
                prefix_rel = (appid_str, "pfx", "drive_c", section, *rel_parts[:-1])
                if not _safe_parents_within(paths.compatdata_dir, compat_root_resolved, prefix_rel):
                    continue
                dst_base = paths.compatdata_dir / appid_str / "pfx" / "drive_c" / section
                dst_base.mkdir(parents=True, exist_ok=True)
                dst_base_resolved = dst_base.resolve()
                if not dst_base_resolved.is_relative_to(compat_root_resolved):
                    continue
                target = dst_base.joinpath(*rel_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.resolve().is_relative_to(dst_base_resolved):
                    continue
                if target.is_symlink():
                    continue
                if target.is_file() and target not in backed_up_targets:
                    _backup_file(
                        target,
                        keep=3,
                        backup_dir=paths.compatdata_dir / appid_str / ".deckinstall_bak",
                    )
                    backed_up_targets.add(target)
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
                )
                os.close(fd)
                tmp_path = Path(tmp_name)
                try:
                    copied_bytes = 0
                    with zf.open(info, "r") as src_fh, tmp_path.open("wb") as dst_fh:
                        while chunk := src_fh.read(65536):
                            copied_bytes += len(chunk)
                            total_bytes += len(chunk)
                            if copied_bytes > _RESTORE_MAX_FILE_BYTES or total_bytes > _RESTORE_MAX_TOTAL_BYTES:
                                raise RuntimeError(f"Decompression limit exceeded on {info.filename}")
                            dst_fh.write(chunk)
                    os.replace(tmp_path, target)
                except Exception:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                    raise
                restored_games.add(appid_str)
                restored_files += 1
        return len(restored_games), restored_files


def sync_prefix_symlinks(
    paths: SteamPaths,
    links_root: Path | None = None,
) -> tuple[int, int]:
    """Create Shortix-style named symlinks in ~/Games/.prefixes/<Game> -> compatdata/<appid>."""
    root = links_root or (Path.home() / "Games" / ".prefixes")
    root.mkdir(parents=True, exist_ok=True)
    manifest_file = root / ".deckinstall_links.json"
    managed_prev: set[str] = set()
    if manifest_file.is_file():
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(raw, list):
                managed_prev = {str(x) for x in raw}
        except Exception:
            managed_prev = set()

    entries = load_shortcuts(paths.shortcuts_vdf)
    expected: dict[str, Path] = {}
    for entry in entries:
        compat = paths.compatdata_dir / str(entry.appid_u32)
        if not compat.is_dir():
            continue
        safe = re.sub(r'[\\/:*?"<>|]+', " ", entry.app_name).strip() or str(entry.appid_u32)
        if safe in expected:
            safe = f"{safe} ({entry.appid_u32})"
        expected[safe] = compat

    created = 0
    removed = 0
    for child in root.iterdir():
        if not child.is_symlink() or child.name in expected:
            continue
        if child.name in managed_prev:
            child.unlink()
            removed += 1
    managed_now: set[str] = set(managed_prev - {c for c in managed_prev if c not in expected})
    for name, target in expected.items():
        link = root / name
        if link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    managed_now.add(name)
                    continue
            except OSError:
                pass
            if name not in managed_prev:
                continue
            link.unlink()
        elif link.exists():
            continue
        link.symlink_to(target)
        managed_now.add(name)
        created += 1

    try:
        _atomic_write_text(
            manifest_file,
            json.dumps(sorted(managed_now), indent=2, ensure_ascii=False),
        )
    except OSError:
        pass
    return created, removed


def get_shortcut_proton(config_vdf: Path, appid_u32: int) -> str:
    """Return configured compatibility tool name for appid_u32, or ''."""
    if not config_vdf.is_file():
        return ""
    try:
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        entry = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
            .get(str(appid_u32), {})
        )
        return str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""
    except Exception:
        return ""


def set_shortcut_proton(config_vdf: Path, appid_u32: int, tool_name: str) -> None:
    """Set compatibility tool for a Steam shortcut in config.vdf atomically."""
    set_compat_tool(config_vdf, appid_u32, tool_name, create_if_missing=True)


_PRESET_ENV_KEYS = (
    "MANGOHUD",
    "PROTON_USE_WINED3D",
    "PROTON_NO_ESYNC",
    "PROTON_NO_FSYNC",
    "PROTON_DLSS_UPGRADE",
    "WINEDLLOVERRIDES",
)
_HELIXSR_OPTISCALER_DLLS = frozenset(
    {"dxgi", "amd_fidelityfx_dx12", "amd_fidelityfx_vk", "winmm", "version"}
)
_SHELL_OPS = {"&&", "||", ";", "|", ">", ">>", "<", "2>&1", "1>&2", "%command%"}
_SAFE_UNQUOTED_DOLLAR_RE = re.compile(r"^[\w./:=+-]*\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})[\w./:=+-]*$")


def _validate_launch_options(launch_options: str) -> None:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in launch_options):
        raise ValueError("Launch options cannot contain control characters or newlines.")


def _is_env_assignment(tok: str) -> bool:
    if "=" not in tok or tok.startswith("-"):
        return False
    k, _ = tok.split("=", 1)
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))


def _format_launch_token(tok: str) -> str:
    if tok in _SHELL_OPS:
        return tok
    if ('"' in tok or "'" in tok) and "`" not in tok and "$(" not in tok:
        return tok
    if _is_env_assignment(tok):
        k, v = tok.split("=", 1)
        if "$" in v and _SAFE_UNQUOTED_DOLLAR_RE.fullmatch(v) and "`" not in v and "$(" not in v:
            return f"{k}={v}"
        return f"{k}={shlex.quote(v)}"
    if "$" in tok and _SAFE_UNQUOTED_DOLLAR_RE.fullmatch(tok) and "`" not in tok and "$(" not in tok:
        return tok
    return shlex.quote(tok)


def _format_winedlloverrides_token(winedlloverrides: str) -> str:
    val = winedlloverrides.strip()
    if not any(ch in val for ch in ('"', "$", "`", "\\")):
        return f'WINEDLLOVERRIDES="{val}"'
    return f"WINEDLLOVERRIDES={shlex.quote(val)}"


def install_mangohud_topbar_config(config_path: Path | None = None) -> Path:
    """Write a Steam-Deck-style horizontal top-bar MangoHud.conf (backing up any existing file)."""
    xdg_config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", "").strip() or (Path.home() / ".config")
    ).expanduser()
    target = config_path or (xdg_config_home / "MangoHud" / "MangoHud.conf")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# DeckInstall — Steam Deck style horizontal top-bar\n"
        "legacy_layout=0\n"
        "horizontal\n"
        "position=top-left\n"
        "hud_compact\n"
        "background_alpha=0.35\n"
        "font_size=19\n"
        "fps\n"
        "frametime\n"
        "gpu_stats\n"
        "gpu_temp\n"
        "cpu_stats\n"
        "cpu_temp\n"
        "vram\n"
        "ram\n"
    )
    if target.exists() and not target.is_symlink():
        try:
            if target.read_text(encoding="utf-8", errors="replace") != content:
                backup = target.with_name(f"{target.name}.deckinstall.bak")
                if not backup.exists():
                    shutil.copy2(target, backup)
        except OSError:
            pass
    _atomic_write_bytes(target, content.encode("utf-8"))
    return target


def detect_helixsr_lsfg_conflict(launch_options: str, winedlloverrides: str = "") -> bool:
    """Return True if lsfg-vk / ENABLE_LSFG is enabled alongside HelixSR/OptiScaler DLL overrides."""
    try:
        tokens = _raw_shell_tokens(launch_options) if launch_options.strip() else []
    except ValueError:
        tokens = launch_options.split()

    lsfg_enabled = False
    dll_strings: list[str] = []
    if winedlloverrides.strip():
        dll_strings.append(winedlloverrides.strip())

    for tok in tokens:
        stripped = tok.strip("\"'")
        if stripped.lower() == "lsfg-vk":
            lsfg_enabled = True
        if _is_env_assignment(tok):
            k, v = tok.split("=", 1)
            v_clean = v.strip("\"'").strip()
            if k == "ENABLE_LSFG" and v_clean.lower() not in ("", "0", "false", "no", "off"):
                lsfg_enabled = True
            elif k == "WINEDLLOVERRIDES" and v_clean:
                dll_strings.append(v_clean)

    if not lsfg_enabled or not dll_strings:
        return False

    for dll_str in dll_strings:
        for entry in dll_str.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            if "=" in entry:
                names_part, mode_part = entry.split("=", 1)
                if mode_part.strip().lower() == "d":
                    continue
            else:
                names_part = entry
            for dll_name in names_part.split(","):
                norm = dll_name.strip().lstrip("*").lower()
                if norm.endswith(".dll"):
                    norm = norm[:-4]
                if norm in _HELIXSR_OPTISCALER_DLLS:
                    return True
    return False


def apply_launch_presets(
    launch_options: str,
    gamemode: bool,
    mangohud: bool,
    wined3d: bool,
    no_esync: bool,
    no_fsync: bool,
    winedlloverrides: str = "",
    dlss_upgrade: bool = False,
) -> str:
    """Toggle common SteamOS/Proton launch presets while preserving existing custom options."""
    _validate_launch_options(launch_options)
    _validate_launch_options(winedlloverrides)
    try:
        tokens = _raw_shell_tokens(launch_options) if launch_options.strip() else []
    except ValueError:
        tokens = launch_options.split()

    # Strip existing managed presets
    filtered: list[str] = []
    for tok in tokens:
        if tok == "gamemoderun":
            continue
        if _is_env_assignment(tok) and tok.split("=", 1)[0] in _PRESET_ENV_KEYS:
            continue
        filtered.append(tok)

    if "%command%" in filtered:
        cmd_idx = filtered.index("%command%")
        before_raw = filtered[:cmd_idx]
        after_cmd = filtered[cmd_idx + 1 :]
    else:
        before_raw = [t for t in filtered if _is_env_assignment(t)]
        after_cmd = [t for t in filtered if not _is_env_assignment(t)]

    env_before = [t for t in before_raw if _is_env_assignment(t)]
    wrappers_before = [t for t in before_raw if not _is_env_assignment(t)]

    preset_envs: list[str] = []
    if mangohud:
        preset_envs.append("MANGOHUD=1")
    if wined3d:
        preset_envs.append("PROTON_USE_WINED3D=1")
    if no_esync:
        preset_envs.append("PROTON_NO_ESYNC=1")
    if no_fsync:
        preset_envs.append("PROTON_NO_FSYNC=1")
    if dlss_upgrade:
        preset_envs.append("PROTON_DLSS_UPGRADE=1")
    if winedlloverrides.strip():
        preset_envs.append(_format_winedlloverrides_token(winedlloverrides))

    wrappers_out: list[str] = []
    if gamemode:
        wrappers_out.append("gamemoderun")
    wrappers_out.extend(wrappers_before)

    if not env_before and not preset_envs and not wrappers_out and not after_cmd:
        return ""
    final_tokens = env_before + preset_envs + wrappers_out + ["%command%"] + after_cmd
    return " ".join(_format_launch_token(tok) for tok in final_tokens)
