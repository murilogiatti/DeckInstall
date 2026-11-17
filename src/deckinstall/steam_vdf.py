"""Safe reader/writer for Steam shortcuts.vdf and config.vdf."""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import warnings
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vdf

from .extractor import check_disk_space
from .scanner import clean_game_name, detect_steam_appid
from .steam_paths import (
    SteamPaths,
    _read_compat_tool_id,
    dir_size_bytes,
    find_default_proton_name,
    is_flatpak_steam,
    is_steam_running,
    list_steam_users,
)

DEFAULT_SAVES_DIR = Path.home() / "Games" / "saves"
_STEAM_LOCK = threading.RLock()
_THREAD_LOCAL = threading.local()


@contextmanager
def _file_lock(lock_path: Path):
    """Acquire both intra-process RLock and cross-process flock for a lockfile path."""
    if lock_path.is_symlink():
        raise RuntimeError(f"Refusing symlink lockfile: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = Path(os.path.abspath(lock_path))
    depths: dict[Path, int] | None = getattr(_THREAD_LOCAL, "flock_depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCAL.flock_depths = depths
    depth = depths.get(lock_key, 0)
    with _STEAM_LOCK:
        if depth > 0:
            depths[lock_key] = depth + 1
            try:
                yield
            finally:
                depths[lock_key] = depth
            return
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            depths[lock_key] = 1
            try:
                yield
            finally:
                depths.pop(lock_key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _steam_transaction_lock(anchor: Path):
    """Acquire both intra-process RLock and cross-process flock per lockfile for Steam VDF mutations."""
    lock_dir: Path | None = None
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg_runtime:
        runtime_path = Path(xdg_runtime)
        try:
            if (
                runtime_path.is_dir()
                and not runtime_path.is_symlink()
                and runtime_path.stat().st_uid == os.getuid()
            ):
                lock_dir = runtime_path / "deckinstall"
        except OSError:
            lock_dir = None
    if lock_dir is None:
        lock_dir = Path.home() / ".local" / "state" / "DeckInstall" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(lock_dir, 0o700)
    except OSError:
        pass
    anchor_hash = hashlib.sha256(os.path.abspath(anchor).encode("utf-8")).hexdigest()[:16]
    lock_path = lock_dir / f".deckinstall.{anchor_hash}.vdf.lock"
    with _file_lock(lock_path):
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


def _parse_appid_u32(raw_appid: object) -> int | None:
    """Safely parse a VDF appid value into an unsigned 32-bit AppID, or None if malformed."""
    if isinstance(raw_appid, bool) or not isinstance(raw_appid, (int, str, bytes, bytearray)):
        return None
    try:
        return s32_to_u32(int(raw_appid))
    except (TypeError, ValueError):
        return None


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
        dest_dir.glob(f"{glob.escape(path.name)}.*.deckinstall.bak"),
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
    try:
        raw = shortcuts_vdf.read_bytes()
        if not raw:
            return []
        data = vdf.binary_loads(raw)
    except Exception as exc:
        logging.warning("Failed to parse %s: %s", shortcuts_vdf, exc)
        return []
    if not isinstance(data, dict):
        return []
    shortcuts = data.get("shortcuts", {})
    if not isinstance(shortcuts, dict):
        return []
    entries: list[ShortcutEntry] = []
    for idx, item in shortcuts.items():
        if not isinstance(item, dict):
            continue
        app_name = str(item.get("AppName") or item.get("appname") or "")
        exe = str(item.get("Exe") or item.get("exe") or "")
        appid_raw = item.get("appid", 0)
        if isinstance(appid_raw, int) and not isinstance(appid_raw, bool) and appid_raw != 0:
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
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_steam_running():
            return True
        time.sleep(0.5)
    if use_flatpak:
        try:
            subprocess.run(
                ["flatpak", "kill", "com.valvesoftware.Steam"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            subprocess.run(
                ["pkill", "-u", str(os.getuid()), "-TERM", "-x", "steam"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
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
            item_u32 = _parse_appid_u32(item.get("appid", 0))
            if item_u32 is None:
                filtered.append(item)
                continue
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
        shared_stale_ids: set[int] = set()
        if stale_appids:
            other_user_appids: set[int] = set()
            for uid in list_steam_users(paths.steam_root):
                if uid == paths.user_id:
                    continue
                other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
                other_user_appids.update(e.appid_u32 for e in load_shortcuts(other_sv))
            shared_stale_ids = {old_id for old_id in stale_appids if old_id in other_user_appids}

        if preserve_from_appid is None and stale_appids:
            new_compat = paths.compatdata_dir / str(appid_u32)
            for old_id in stale_appids:
                old_compat = paths.compatdata_dir / str(old_id)
                used_by_other = old_id in shared_stale_ids
                if new_compat.is_symlink() and not new_compat.exists():
                    try:
                        new_compat.unlink(missing_ok=True)
                    except OSError:
                        pass
                if (
                    old_compat.is_dir()
                    and not old_compat.is_symlink()
                    and not (new_compat.exists() or new_compat.is_symlink())
                ):
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
            if stale_id not in shared_stale_ids:
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
            item_u32 = _parse_appid_u32(item.get("appid", 0))
            if item_u32 is not None and item_u32 == appid_u32:
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
            item_u32 = _parse_appid_u32(item.get("appid", 0))
            if item_u32 is not None and item_u32 == appid_u32:
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


def _load_vdf_mapping(config_vdf: Path) -> Any:
    """Load a text VDF file preserving duplicate keys when VDFDict is available and raw non-UTF8 bytes."""
    text = config_vdf.read_bytes().decode("utf-8", errors="surrogateescape")
    mapper = getattr(vdf, "VDFDict", None)
    if mapper is not None:
        try:
            return vdf.loads(text, mapper=mapper)
        except TypeError:
            pass
    return vdf.loads(text)


def _dump_vdf_mapping(config_vdf: Path, data: Any) -> None:
    dumped = vdf.dumps(data, pretty=True)
    _atomic_write_bytes(config_vdf, dumped.encode("utf-8", errors="surrogateescape"))


def _get_vdf_section(mapping: Any, key: str) -> Any | None:
    if not hasattr(mapping, "items"):
        return None
    if key in mapping:
        return mapping[key]
    low = key.lower()
    for k, v in mapping.items():
        if str(k).lower() == low:
            return v
    return None


def _ensure_vdf_section(mapping: Any, key: str) -> Any:
    existing = _get_vdf_section(mapping, key)
    if existing is not None and hasattr(existing, "items"):
        return existing
    new_sec: Any = getattr(vdf, "VDFDict", dict)()
    mapping[key] = new_sec
    return mapping[key]


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
            data = _load_vdf_mapping(config_vdf)
        elif create_if_missing:
            data = getattr(vdf, "VDFDict", dict)()
        else:
            return
        store = _ensure_vdf_section(data, "InstallConfigStore")
        software = _ensure_vdf_section(store, "Software")
        valve = _ensure_vdf_section(software, "Valve")
        steam = _ensure_vdf_section(valve, "Steam")
        mapping = _ensure_vdf_section(steam, "CompatToolMapping")
        mapping[str(appid_u32)] = {
            "name": tool_name,
            "config": "",
            "priority": "250",
        }
        _dump_vdf_mapping(config_vdf, data)


def remove_compat_tool(config_vdf: Path, appid_u32: int) -> None:
    """Remove CompatToolMapping entry for appid_u32 if present."""
    with _steam_transaction_lock(config_vdf):
        if not config_vdf.is_file():
            return
        data = _load_vdf_mapping(config_vdf)
        mapping = data
        for sec in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
            mapping = _get_vdf_section(mapping, sec)
            if mapping is None:
                return
        if hasattr(mapping, "__contains__") and str(appid_u32) in mapping:
            _backup_file(config_vdf)
            del mapping[str(appid_u32)]
            _dump_vdf_mapping(config_vdf, data)


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
        data = _load_vdf_mapping(config_vdf)
        store = _ensure_vdf_section(data, "InstallConfigStore")
        software = _ensure_vdf_section(store, "Software")
        valve = _ensure_vdf_section(software, "Valve")
        steam = _ensure_vdf_section(valve, "Steam")
        mapping = _ensure_vdf_section(steam, "CompatToolMapping")
        updated = 0
        for appid in appids:
            mapping[str(appid)] = {
                "name": target_tool.strip(),
                "config": "",
                "priority": "250",
            }
            updated += 1
        _dump_vdf_mapping(config_vdf, data)
        return updated


def remove_compat_tools(config_vdf: Path, appids: list[int]) -> int:
    """Remove multiple AppIDs from CompatToolMapping in one atomic write."""
    if not appids:
        return 0
    with _steam_transaction_lock(config_vdf):
        if not config_vdf.is_file():
            return 0
        data = _load_vdf_mapping(config_vdf)
        mapping = data
        for sec in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
            mapping = _get_vdf_section(mapping, sec)
            if mapping is None:
                return 0
        if not hasattr(mapping, "items"):
            return 0
        removed = 0
        for appid in appids:
            if str(appid) in mapping:
                if removed == 0:
                    _backup_file(config_vdf)
                del mapping[str(appid)]
                removed += 1
        if removed:
            _dump_vdf_mapping(config_vdf, data)
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


def _dir_has_game_exe(directory: Path, max_depth: int = 3) -> bool:
    """Return True if directory contains a .exe or .bat file within max_depth levels."""
    stack: list[tuple[Path, int]] = [(directory, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    name_low = entry.name.lower()
                    if entry.is_file(follow_symlinks=False):
                        if name_low.endswith((".exe", ".bat")):
                            return True
                    elif (
                        depth + 1 < max_depth
                        and entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and name_low != "drive_c"
                    ):
                        stack.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    return False


def _looks_like_multi_game_container(directory: Path) -> bool:
    """Return True if directory is a Syncthing root or contains multiple game subfolders."""
    if directory == directory.parent:
        return False
    try:
        if (directory / ".stfolder").exists() or (directory / ".stfolder").is_symlink():
            return True
    except OSError:
        pass
    game_subdirs = 0
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                    continue
                low = entry.name.lower()
                if low in _SUBDIR_NAMES or low in _FORBIDDEN_ROOT_NAMES:
                    continue
                if _dir_has_game_exe(Path(entry.path)):
                    game_subdirs += 1
                    if game_subdirs >= 2:
                        return True
    except OSError:
        return False
    return False


def infer_game_root_from_exe(exe_path: Path) -> Path:
    """Infer the top-level game folder from an executable path."""
    home = Path.home()
    known_containers = {
        p.resolve()
        for p in (
            home,
            home / "Games",
            home / "Downloads",
            home / "Syncthing",
            home / "Desktop",
            home / "Documents",
            home / "Music",
            home / "Pictures",
            home / "Videos",
            home / "Public",
            home / "Templates",
            Path("/tmp"),
            Path("/var/tmp"),
            Path("/mnt"),
            Path("/media"),
        )
    }
    resolved = exe_path.resolve()
    if any(part.lower() == "drive_c" for part in resolved.parts):
        raise ValueError(
            f"O executável está dentro de um prefixo Wine (drive_c): {resolved}. "
            "Mova o jogo para uma pasta própria antes de migrar."
        )
    if resolved.parent in known_containers or _looks_like_multi_game_container(resolved.parent):
        raise ValueError(
            f"O executável está solto diretamente em {resolved.parent}; "
            "coloque-o dentro de uma subpasta própria antes de migrar."
        )
    if resolved.parent.parts[:3] == ("/", "run", "media") and len(resolved.parent.parts) <= 5:
        raise ValueError(
            f"O executável está solto diretamente na raiz da mídia removível ({resolved.parent}); "
            "coloque-o dentro de uma subpasta própria antes de migrar."
        )
    tmp_containers = {Path("/tmp").resolve(), Path("/var/tmp").resolve()}
    candidate = resolved.parent
    for parent in resolved.parents:
        if parent == parent.parent:
            continue
        if parent in known_containers:
            break
        if parent.parent in tmp_containers:
            rel_parts = resolved.relative_to(parent).parts
            if len(rel_parts) == 1 or rel_parts[0].lower() in _SUBDIR_NAMES:
                candidate = parent
                break
            while (
                candidate.name.lower() in _SUBDIR_NAMES
                and candidate.parent != candidate
                and candidate.parent != parent
            ):
                candidate = candidate.parent
            break
        if parent.parent in known_containers or _looks_like_multi_game_container(parent.parent):
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
            and not _looks_like_multi_game_container(candidate.parent)
            and not (
                len(candidate.parent.parts) <= 5
                and candidate.parent.parts[:3] == ("/", "run", "media")
            )
        ):
            candidate = candidate.parent
    if (
        candidate.name.lower() in _FORBIDDEN_ROOT_NAMES
        or candidate in known_containers
        or _looks_like_multi_game_container(candidate)
        or (candidate.parts[:3] == ("/", "run", "media") and len(candidate.parts) <= 5)
    ):
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
    other_sv_snaps: list[tuple[Path, bytes]] = []
    moved_root = False
    attempted_move_root = False
    moved_compat_pair: tuple[Path, Path] | None = None
    copied_compat_dst: Path | None = None
    backed_compat_pair: tuple[Path, Path] | None = None
    moved_art_pairs: list[tuple[Path, Path]] = []

    other_sv_pairs: list[tuple[str, Path]] = []
    for uid in list_steam_users(paths.steam_root):
        if uid == paths.user_id:
            continue
        other_sv_pairs.append((uid, paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"))

    lock_targets_by_resolved: dict[str, Path] = {}
    for lock_path in (paths.shortcuts_vdf, paths.config_vdf, *(sv for _, sv in other_sv_pairs)):
        try:
            key = str(lock_path.resolve())
        except OSError:
            key = str(lock_path)
        lock_targets_by_resolved.setdefault(key, lock_path)
    ordered_lock_targets = [lock_targets_by_resolved[k] for k in sorted(lock_targets_by_resolved)]

    with ExitStack() as lock_stack:
        for lock_target in ordered_lock_targets:
            lock_stack.enter_context(_steam_transaction_lock(lock_target))
        try:
                shortcuts_snap = paths.shortcuts_vdf.read_bytes() if paths.shortcuts_vdf.is_file() else None
                config_snap = paths.config_vdf.read_bytes() if paths.config_vdf.is_file() else None
                if old_root.resolve() != new_root.resolve():
                    if new_root.exists():
                        raise FileExistsError(f"Destination folder already exists: {new_root}")
                    try:
                        same_dev = old_root.stat().st_dev == games_root.stat().st_dev
                    except OSError:
                        same_dev = False
                    if not same_dev:
                        check_disk_space(games_root, _dir_size_bytes(old_root))
                    attempted_move_root = True
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
                other_appid_migrations: list[tuple[int, int]] = []
                for uid, other_sv in other_sv_pairs:
                    if not other_sv.is_file() or other_sv.stat().st_size == 0:
                        continue
                    try:
                        orig_other_bytes = other_sv.read_bytes()
                        other_data = vdf.binary_loads(orig_other_bytes)
                        other_sc = other_data.get("shortcuts", {})
                        other_changed = False
                        for item in other_sc.values():
                            if not isinstance(item, dict):
                                continue
                            other_app_name = str(item.get("AppName") or item.get("appname") or "")
                            old_exe_raw = str(item.get("Exe") or item.get("exe") or "")
                            appid_raw = item.get("appid", 0)
                            if isinstance(appid_raw, int) and not isinstance(appid_raw, bool) and appid_raw != 0:
                                old_other_u32 = s32_to_u32(appid_raw)
                            else:
                                old_other_u32, _ = compute_shortcut_appid(old_exe_raw, other_app_name)
                            exe_changed = False
                            for field in ("Exe", "StartDir", "icon"):
                                val = str(item.get(field) or "")
                                cleaned = _strip_vdf_quotes(val)
                                if not cleaned:
                                    continue
                                upd = _rewrite_path_prefix(cleaned, old_roots, str(new_root))
                                if upd != cleaned:
                                    item[field] = _quote_path(upd) if field != "icon" else upd
                                    other_changed = True
                                    if field == "Exe":
                                        exe_changed = True
                            if exe_changed:
                                new_other_exe = str(item.get("Exe") or "")
                                new_other_u32, new_other_s32 = compute_shortcut_appid(new_other_exe, other_app_name)
                                item["appid"] = new_other_s32
                                if new_other_u32 != old_other_u32:
                                    other_appid_migrations.append((old_other_u32, new_other_u32))
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

                if other_appid_migrations and paths.config_vdf.is_file():
                    remaining_appids: set[int] = {e.appid_u32 for e in load_shortcuts(paths.shortcuts_vdf)}
                    for _, other_sv in other_sv_pairs:
                        remaining_appids.update(e.appid_u32 for e in load_shortcuts(other_sv))
                    config_data = _load_vdf_mapping(paths.config_vdf)
                    mapping: Any = config_data
                    for sec in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
                        mapping = _get_vdf_section(mapping, sec)
                        if mapping is None:
                            break
                    if mapping is not None and hasattr(mapping, "__contains__"):
                        config_changed = False
                        for old_u32, new_u32 in other_appid_migrations:
                            old_key = str(old_u32)
                            new_key = str(new_u32)
                            if old_key in mapping and new_key not in mapping:
                                old_val = mapping[old_key]
                                mapping[new_key] = dict(old_val) if hasattr(old_val, "items") else old_val
                                config_changed = True
                        for old_u32, _ in other_appid_migrations:
                            old_key = str(old_u32)
                            if old_u32 not in remaining_appids and old_key in mapping:
                                del mapping[old_key]
                                config_changed = True
                        if config_changed:
                            _backup_file(paths.config_vdf)
                            _dump_vdf_mapping(paths.config_vdf, config_data)

                if new_appid != entry.appid_u32:
                    new_compat = paths.compatdata_dir / str(new_appid)
                    used_by_other = False
                    for _, other_sv in other_sv_pairs:
                        if any(e.appid_u32 == entry.appid_u32 for e in load_shortcuts(other_sv)):
                            used_by_other = True
                            break
                    if old_compat.is_dir():
                        if new_compat.is_symlink() and not new_compat.exists():
                            new_compat.unlink(missing_ok=True)
                        elif new_compat.exists() or new_compat.is_symlink():
                            stamp = time.strftime("%Y%m%d-%H%M%S")
                            backup_compat = paths.compatdata_dir / f"{new_appid}.{stamp}.deckinstall.bak"
                            counter = 1
                            while backup_compat.exists() or backup_compat.is_symlink():
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
                            if (
                                not art_file.is_symlink()
                                and art_file.is_file()
                                and art_file.name.startswith(old_prefix)
                            ):
                                suffix = art_file.name[len(old_prefix) :]
                                if suffix and suffix[0] not in ("p", "_", "."):
                                    continue
                                dest_art = paths.grid_dir / f"{new_appid}{suffix}"
                                if not dest_art.exists():
                                    shutil.move(str(art_file), str(dest_art))
                                    moved_art_pairs.append((dest_art, art_file))

                    if not used_by_other:
                        remove_compat_tool(paths.config_vdf, entry.appid_u32)
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
            if (
                backed_compat_pair
                and (backed_compat_pair[0].exists() or backed_compat_pair[0].is_symlink())
                and not (backed_compat_pair[1].exists() or backed_compat_pair[1].is_symlink())
            ):
                try:
                    shutil.move(str(backed_compat_pair[0]), str(backed_compat_pair[1]))
                except OSError:
                    pass
            for other_sv, orig_other in reversed(locals().get("other_sv_snaps", [])):
                try:
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
            elif (
                not moved_root
                and attempted_move_root
                and new_root.resolve() != old_root.resolve()
                and new_root.exists()
            ):
                try:
                    if new_root.is_symlink() or new_root.is_file():
                        new_root.unlink(missing_ok=True)
                    else:
                        shutil.rmtree(new_root)
                except OSError:
                    logging.exception("Rollback failed cleaning up partial %s", new_root)
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
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        file_digest = getattr(hashlib, "file_digest", None)
        if file_digest is not None:
            return file_digest(fh, "sha256").hexdigest()
        h = hashlib.sha256()
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


_dir_size_bytes = dir_size_bytes


_PREFIX_SAFE_CACHE_RELPATHS = (
    "AppData/Local/Temp",
    "AppData/Local/NVIDIA/DXCache",
    "AppData/Local/AMD/DxCache",
    "AppData/Local/D3DSCache",
)


def _iter_prefix_internal_cache_dirs(prefix_root: Path) -> list[Path]:
    """Return existing non-symlink internal cache directories inside a Wine/Proton prefix."""
    users_dir = _prefix_users_dir(prefix_root)
    if users_dir is None:
        return []
    try:
        users_resolved = users_dir.resolve()
    except OSError:
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    try:
        user_entries = list(users_dir.iterdir())
    except OSError:
        return []
    for user_entry in user_entries:
        if user_entry.is_symlink() or not user_entry.is_dir():
            continue
        for rel in _PREFIX_SAFE_CACHE_RELPATHS:
            rel_parts = Path(rel).parts
            if not _safe_parents_within(user_entry, users_resolved, rel_parts):
                continue
            cache_dir = user_entry / rel
            if cache_dir.is_dir() and not cache_dir.is_symlink():
                try:
                    resolved = cache_dir.resolve()
                except OSError:
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(cache_dir)
    return found


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
                            c
                            for c in sd.iterdir()
                            if c.is_dir()
                            and not c.name.startswith(".")
                            and c.name.lower() not in _GENERIC_GAME_HINT_DIRS
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
    apps_dir = data_home / "applications"
    if apps_dir.is_dir():
        try:
            for desktop_file in apps_dir.glob("deckinstall-*.desktop"):
                if desktop_file.is_symlink() or not desktop_file.is_file():
                    continue
                try:
                    for line in desktop_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("Path="):
                            gdir_path = Path(line.split("=", 1)[1].strip().strip('"')).expanduser()
                            manifest = gdir_path / ".deckinstall_standalone.json"
                            if manifest.is_file():
                                mdata = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                                p_raw = mdata.get("prefix_dir")
                                if p_raw:
                                    standalone_prefix_paths.add(Path(str(p_raw)).resolve())
                            break
                except Exception:
                    pass
        except OSError:
            pass

    cache_lock = threading.Lock()
    sha256_cache: dict[Path, tuple[int, int, str]] = {}
    active_save_index: dict[Path, dict[tuple[str, str], tuple[Path, int]] | None] = {}
    pending_sized: list[tuple[CleanupCandidate, int]] = []

    def _cached_sha256(file_path: Path) -> str:
        st = file_path.stat(follow_symlinks=False)
        key = (st.st_size, st.st_mtime_ns)
        with cache_lock:
            cached = sha256_cache.get(file_path)
            if cached is not None and cached[0] == key[0] and cached[1] == key[1]:
                return cached[2]
        digest = _file_sha256(file_path)
        with cache_lock:
            sha256_cache[file_path] = (key[0], key[1], digest)
        return digest

    def _get_target_save_index(
        dst_compatdata: Path,
    ) -> dict[tuple[str, str], tuple[Path, int]] | None:
        with cache_lock:
            if dst_compatdata in active_save_index:
                return active_save_index[dst_compatdata]
        if not dst_compatdata.is_dir():
            with cache_lock:
                active_save_index[dst_compatdata] = None
            return None
        dst_users = _prefix_users_dir(dst_compatdata)
        if dst_users is None:
            with cache_lock:
                active_save_index[dst_compatdata] = None
            return None
        index: dict[tuple[str, str], tuple[Path, int]] = {}
        try:
            dst_progdata = _prefix_programdata_dir(dst_compatdata)
            if dst_progdata is not None:
                dummy_pd = dst_compatdata / "__idx_pd__"
                for fp, rel_dst in _iter_programdata_save_pairs(
                    dst_progdata, dummy_pd, raise_on_error=True
                ):
                    sz = fp.stat().st_size
                    if sz > 0:
                        index[("ProgramData", rel_dst.relative_to(dummy_pd).as_posix())] = (fp, sz)
            dummy_users = dst_compatdata / "__idx_users__"
            for fp, rel_dst in _iter_prefix_save_pairs(
                dst_users, dummy_users, preserve_user_dirs=True, raise_on_error=True
            ):
                sz = fp.stat().st_size
                if sz > 0:
                    index[("users", rel_dst.relative_to(dummy_users).as_posix())] = (fp, sz)
        except OSError:
            with cache_lock:
                active_save_index[dst_compatdata] = None
            return None
        with cache_lock:
            active_save_index[dst_compatdata] = index
        return index

    def _collect_orphan_saves(
        src_prefix: Path,
    ) -> tuple[bool, list[tuple[str, str, Path, int]] | None]:
        src_users = _prefix_users_dir(src_prefix)
        if src_users is None:
            return True, None
        entries: list[tuple[str, str, Path, int]] = []
        try:
            src_progdata = _prefix_programdata_dir(src_prefix)
            if src_progdata is not None:
                dummy_pd = src_prefix / "__unused_pd__"
                for src_file, dst_rel in _iter_programdata_save_pairs(
                    src_progdata, dummy_pd, raise_on_error=True
                ):
                    sz = src_file.stat().st_size
                    if sz > 0:
                        entries.append(("ProgramData", dst_rel.relative_to(dummy_pd).as_posix(), src_file, sz))
            dummy_users = src_prefix / "__unused_users__"
            for src_file, dst_rel in _iter_prefix_save_pairs(
                src_users, dummy_users, preserve_user_dirs=False, raise_on_error=True
            ):
                sz = src_file.stat().st_size
                if sz > 0:
                    entries.append(("users", dst_rel.relative_to(dummy_users).as_posix(), src_file, sz))
            unconventional = _prefix_has_unconventional_saves(src_prefix, src_users)
        except OSError:
            return True, None
        if unconventional:
            return True, None
        if not entries:
            return False, []
        return True, entries

    def _orphan_saves_verified_in_target(
        src_prefix_resolved: Path,
        src_entries: list[tuple[str, str, Path, int]] | None,
        dst_compatdata: Path,
    ) -> bool:
        if not src_entries:
            return False
        dst_index = _get_target_save_index(dst_compatdata)
        if dst_index is None:
            return False
        matched_pairs: list[tuple[Path, Path]] = []
        try:
            for section, rel_posix, src_file, src_size in src_entries:
                hit = dst_index.get((section, rel_posix))
                if hit is None:
                    return False
                dst_file, dst_size = hit
                if dst_size == 0 or dst_size != src_size:
                    return False
                if dst_file.is_symlink() or not dst_file.is_file():
                    return False
                dst_resolved = dst_file.resolve()
                if os.path.samefile(src_file, dst_file) or src_file.resolve() == dst_resolved:
                    return False
                if dst_resolved.is_relative_to(src_prefix_resolved):
                    return False
                matched_pairs.append((src_file, dst_file))
            for src_file, dst_file in matched_pairs:
                if _cached_sha256(src_file) != _cached_sha256(dst_file):
                    return False
        except OSError:
            return False
        return True

    if paths is not None:
        for entry in current_entries:
            _get_target_save_index(paths.compatdata_dir / str(entry.appid_u32))

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
            min_sz = 1 if (is_standalone_prefixes_root and _prefix_users_dir(child) is None) else 0
            matched_entry = by_steam_appid.get(child.name) or by_norm_key.get(_norm_key(child.name))
            has_saves, src_entries = _collect_orphan_saves(child)
            saves_verified = False
            if has_saves and src_entries and matched_entry is not None and paths is not None:
                dst_compat = paths.compatdata_dir / str(matched_entry.appid_u32)
                saves_verified = _orphan_saves_verified_in_target(resolved, src_entries, dst_compat)
            matched_name = matched_entry.app_name if matched_entry else ""
            pending_sized.append((
                CleanupCandidate(
                    category="Prefixo Externo",
                    label=f"{root.parent.name}/{root.name}/{child.name}",
                    path=child,
                    size_bytes=0,
                    has_saves=has_saves,
                    saves_verified_migrated=saves_verified,
                    matched_game=matched_name,
                    game_hint=matched_name or _infer_prefix_game_name(child),
                ),
                min_sz,
            ))

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
                    has_saves, src_entries = _collect_orphan_saves(child)
                    saves_verified = False
                    if has_saves and src_entries and matched_entry is not None:
                        try:
                            child_resolved = child.resolve()
                            saves_verified = _orphan_saves_verified_in_target(
                                child_resolved, src_entries, paths.compatdata_dir / prefix_id
                            )
                        except OSError:
                            saves_verified = False
                    matched_name = matched_entry.app_name if matched_entry else ""
                    pending_sized.append((
                        CleanupCandidate(
                            category="Backup",
                            label=child.name,
                            path=child,
                            size_bytes=0,
                            has_saves=has_saves,
                            saves_verified_migrated=saves_verified,
                            matched_game=matched_name,
                            game_hint=matched_name or _infer_prefix_game_name(child),
                        ),
                        0,
                    ))
                    continue
                if not (child.name.isascii() and child.name.isdigit()):
                    continue
                appid = int(child.name)
                if appid in active_shortcuts or appid in installed_steam:
                    if include_system_caches:
                        matched_entry = by_appid_u32.get(appid)
                        matched_name = matched_entry.app_name if matched_entry else ""
                        for cache_dir in _iter_prefix_internal_cache_dirs(child):
                            pending_sized.append((
                                CleanupCandidate(
                                    category="Prefix Cache",
                                    label=f"compatdata/{child.name}/{cache_dir.name}",
                                    path=cache_dir,
                                    size_bytes=0,
                                    has_saves=False,
                                    saves_verified_migrated=False,
                                    matched_game=matched_name,
                                    game_hint=matched_name,
                                ),
                                1,
                            ))
                    continue
                try:
                    child_resolved = child.resolve()
                    if _is_prefix_referenced_by_shortcuts(child_resolved, all_user_entries):
                        continue
                except OSError:
                    continue
                has_saves, src_entries = _collect_orphan_saves(child)
                saves_verified = False
                matched_game = ""
                if has_saves and src_entries:
                    for entry in current_entries:
                        dst_compat = paths.compatdata_dir / str(entry.appid_u32)
                        if dst_compat.is_dir() and _orphan_saves_verified_in_target(
                            child_resolved, src_entries, dst_compat
                        ):
                            saves_verified = True
                            matched_game = entry.app_name
                            break
                pending_sized.append((
                    CleanupCandidate(
                        category="Compatdata Órfão",
                        label=f"compatdata/{child.name}",
                        path=child,
                        size_bytes=0,
                        has_saves=has_saves,
                        saves_verified_migrated=saves_verified,
                        matched_game=matched_game,
                        game_hint=matched_game or _infer_prefix_game_name(child),
                    ),
                    0,
                ))

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
                pending_sized.append((
                    CleanupCandidate(
                        category="Orphan Shadercache",
                        label=f"shadercache/{child.name}",
                        path=child,
                        size_bytes=0,
                        has_saves=False,
                        saves_verified_migrated=False,
                        matched_game="",
                        game_hint="",
                    ),
                    1,
                ))

        steam_logs_dir = paths.steam_root / "logs"
        if steam_logs_dir.is_dir() and not steam_logs_dir.is_symlink():
            pending_sized.append((
                CleanupCandidate(
                    category="Steam Logs",
                    label="Steam/logs",
                    path=steam_logs_dir,
                    size_bytes=0,
                    has_saves=False,
                    saves_verified_migrated=False,
                    matched_game="",
                    game_hint="",
                ),
                1024 * 1024,
            ))

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

    for backups_dir in (home / "Games" / "saves" / "backups", home / "Backups" / "DeckInstall"):
        if not (backups_dir.is_dir() and not backups_dir.is_symlink()):
            continue
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
            pending_sized.append((
                CleanupCandidate(
                    category="Trash",
                    label="Trash/files",
                    path=trash_files,
                    size_bytes=0,
                    has_saves=False,
                    saves_verified_migrated=False,
                    matched_game="",
                    game_hint="",
                ),
                1,
            ))

        user_cache = cache_home
        if user_cache.is_dir() and not user_cache.is_symlink():
            for child in user_cache.iterdir():
                if child.is_symlink() or child.name in ("fontconfig", "dconf"):
                    continue
                is_dir = child.is_dir()
                if is_dir:
                    if child.name in _GPU_SHADER_CACHE_NAMES:
                        pending_sized.append((
                            CleanupCandidate(
                                category="GPU Shader Cache",
                                label=f".cache/{child.name}",
                                path=child,
                                size_bytes=0,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            ),
                            1,
                        ))
                    else:
                        pending_sized.append((
                            CleanupCandidate(
                                category="App Cache",
                                label=f".cache/{child.name}",
                                path=child,
                                size_bytes=0,
                                has_saves=False,
                                saves_verified_migrated=False,
                                matched_game="",
                                game_hint="",
                            ),
                            5 * 1024 * 1024,
                        ))
                else:
                    try:
                        sz = child.stat().st_size
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
            pending_sized.append((
                CleanupCandidate(
                    category="GPU Shader Cache",
                    label=".nv/ComputeCache",
                    path=nv_compute,
                    size_bytes=0,
                    has_saves=False,
                    saves_verified_migrated=False,
                    matched_game="",
                    game_hint="",
                ),
                1,
            ))

        var_app = home / ".var" / "app"
        if var_app.is_dir() and not var_app.is_symlink():
            for app_dir in var_app.iterdir():
                if app_dir.is_symlink() or not app_dir.is_dir():
                    continue
                cdir = app_dir / "cache"
                if cdir.is_dir() and not cdir.is_symlink():
                    pending_sized.append((
                        CleanupCandidate(
                            category="App Cache",
                            label=f"Flatpak/{app_dir.name}/cache",
                            path=cdir,
                            size_bytes=0,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        ),
                        5 * 1024 * 1024,
                    ))

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
                tool_id = _read_compat_tool_id(child / "compatibilitytool.vdf", child.name)
                if child.name in active_tools or tool_id in active_tools:
                    continue
                is_old_umu = child.name.startswith("UMU-Proton-")
                is_old_ge = False
                if child.name.startswith("GE-Proton"):
                    try:
                        is_old_ge = child.resolve() not in preserved_ge11
                    except OSError:
                        is_old_ge = False
                if is_old_umu or is_old_ge:
                    pending_sized.append((
                        CleanupCandidate(
                            category="Old Proton/Runtime",
                            label=f"{compat_dir.name}/{child.name}",
                            path=child,
                            size_bytes=0,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        ),
                        1,
                    ))

        umu_dir = data_home / "umu"
        steamrt3 = umu_dir / "steamrt3"
        if (
            not sniper_in_use
            and (umu_dir / "steamrt4" / "toolmanifest.vdf").is_file()
            and steamrt3.is_dir()
            and not steamrt3.is_symlink()
        ):
            pending_sized.append((
                CleanupCandidate(
                    category="Old Proton/Runtime",
                    label="umu/steamrt3",
                    path=steamrt3,
                    size_bytes=0,
                    has_saves=False,
                    saves_verified_migrated=False,
                    matched_game="",
                    game_hint="",
                ),
                1,
            ))

        if paths is not None:
            common_dir = paths.steam_root / "steamapps" / "common"
            if (
                (common_dir / "Proton 11.0").is_dir()
                and (common_dir / "Proton 10.0").is_dir()
                and not any(t.startswith("proton_10") for t in active_tools)
            ):
                p10 = common_dir / "Proton 10.0"
                if not p10.is_symlink():
                    pending_sized.append((
                        CleanupCandidate(
                            category="Old Proton/Runtime",
                            label="common/Proton 10.0",
                            path=p10,
                            size_bytes=0,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        ),
                        1,
                    ))
            if (
                not sniper_in_use
                and (common_dir / "SteamLinuxRuntime_4").is_dir()
                and (common_dir / "SteamLinuxRuntime_sniper").is_dir()
            ):
                sniper = common_dir / "SteamLinuxRuntime_sniper"
                if not sniper.is_symlink():
                    pending_sized.append((
                        CleanupCandidate(
                            category="Old Proton/Runtime",
                            label="common/SteamLinuxRuntime_sniper",
                            path=sniper,
                            size_bytes=0,
                            has_saves=False,
                            saves_verified_migrated=False,
                            matched_game="",
                            game_hint="",
                        ),
                        1,
                    ))

    if pending_sized:
        unique_dirs = list({cand.path for cand, _ in pending_sized})
        dir_sizes: dict[Path, int] = {}
        if len(unique_dirs) == 1:
            dir_sizes[unique_dirs[0]] = _dir_size_bytes(unique_dirs[0])
        else:
            workers = min(8, len(unique_dirs))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for p, sz in zip(unique_dirs, pool.map(_dir_size_bytes, unique_dirs), strict=True):
                    dir_sizes[p] = sz
        for cand, min_size in pending_sized:
            sz = dir_sizes.get(cand.path, 0)
            if sz < min_size:
                continue
            candidates.append(
                CleanupCandidate(
                    category=cand.category,
                    label=cand.label,
                    path=cand.path,
                    size_bytes=sz,
                    has_saves=cand.has_saves,
                    saves_verified_migrated=cand.saves_verified_migrated,
                    matched_game=cand.matched_game,
                    game_hint=cand.game_hint,
                )
            )

    candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return candidates


def delete_cleanup_candidates(candidates: list[CleanupCandidate]) -> tuple[int, int]:
    """Deprecated: use delete_cleanup_candidates_checked instead."""
    warnings.warn(
        "delete_cleanup_candidates is deprecated; use delete_cleanup_candidates_checked instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    deleted, freed, _ = delete_cleanup_candidates_checked(candidates=candidates)
    return deleted, freed


def delete_cleanup_candidates_checked(
    paths: SteamPaths | None = None,
    candidates: list[CleanupCandidate] | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
    allow_unverified_saves: bool = False,
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
            if fresh.has_saves and not fresh.saves_verified_migrated and not allow_unverified_saves:
                errors.append(f"{item.label}: skipped for safety (unvalidated saves).")
                continue
            try:
                if item.path.is_dir() and not item.path.is_symlink():
                    shutil.rmtree(item.path)
                    if fresh.category == "Prefix Cache" and item.path.name.lower() == "temp":
                        item.path.mkdir(parents=True, exist_ok=True)
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
            copied = 0
            with os.fdopen(sfd, "rb") as sfh, zf.open(arcname, "w") as zout:
                while chunk := sfh.read(65536):
                    copied += len(chunk)
                    total_uncompressed += len(chunk)
                    if copied > _RESTORE_MAX_FILE_BYTES or total_uncompressed > _RESTORE_MAX_TOTAL_BYTES:
                        raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
                    zout.write(chunk)
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

    out_dir = dest_root or (DEFAULT_SAVES_DIR / "backups")
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


def backup_prefix_candidates_to_zip(
    candidates: list[CleanupCandidate],
    dest_root: Path | None = None,
) -> tuple[Path, int, int]:
    """Create a ZIP backup of saves found inside the given cleanup prefix candidates."""
    root = dest_root or (DEFAULT_SAVES_DIR / "backups")
    root.mkdir(parents=True, exist_ok=True)
    zip_path = _reserve_unique_backup_zip(root, "prefix-cleanup-backup")
    manifest: dict[str, str] = {}
    games_count = 0
    files_count = 0
    total_uncompressed = 0

    fd, tmp_name = tempfile.mkstemp(prefix=".saves-", suffix=".zip.tmp", dir=str(root))
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for cand in candidates:
                if not cand.path.is_dir():
                    continue
                slug_base = re.sub(r"[^a-zA-Z0-9._-]+", "-", cand.label).strip("-") or "prefix"
                key = slug_base
                idx = 2
                while key in manifest:
                    key = f"{slug_base}-{idx}"
                    idx += 1
                game_files, files_count, total_uncompressed = _write_prefix_saves_to_zip(
                    zf, cand.path, key, files_count, total_uncompressed
                )
                if game_files > 0:
                    manifest[key] = cand.label
                    games_count += 1
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
    return zip_path, games_count, files_count


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
    from .standalone import list_standalone_games

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

        standalone_games = list_standalone_games()
        standalone_by_key: dict[str, Path] = {}
        standalone_by_exact: dict[str, Path] = {}
        standalone_norm_counts: dict[str, list[Path]] = {}
        for sgame in standalone_games:
            slug_base = re.sub(r"[^a-zA-Z0-9._-]+", "-", sgame.game_dir.name).strip("-") or "game"
            skey = f"standalone-{slug_base}"
            idx = 2
            while skey in standalone_by_key:
                skey = f"standalone-{slug_base}-{idx}"
                idx += 1
            standalone_by_key[skey] = sgame.prefix_dir
            standalone_by_exact[sgame.name] = sgame.prefix_dir
            snk = _norm_key(sgame.name)
            if snk:
                standalone_norm_counts.setdefault(snk, []).append(sgame.prefix_dir)
        standalone_by_norm = {
            nk: pdirs[0] for nk, pdirs in standalone_norm_counts.items() if len(pdirs) == 1
        }

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
                if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                    continue
                parts = Path(info.filename).parts
                if len(parts) < 3 or parts[1] not in ("users", "ProgramData"):
                    continue
                prefix_key = parts[0]
                section = parts[1]
                rel_parts = parts[2:]
                if any(p in ("..", "", ".") for p in rel_parts):
                    continue
                if info.file_size > _RESTORE_MAX_FILE_BYTES:
                    raise RuntimeError(f"Backup file too large: {info.filename}")

                if prefix_key.isascii() and prefix_key.isdigit():
                    appid_str = prefix_key
                    if appid_str not in active_ids:
                        if manifest:
                            old_name = manifest.get(appid_str, "")
                            mapped = by_exact.get(old_name) or by_norm.get(_norm_key(old_name))
                            if not mapped:
                                continue
                            appid_str = mapped
                        else:
                            continue
                    target_root = paths.compatdata_dir
                    target_root_resolved = compat_root_resolved
                    target_prefix = paths.compatdata_dir / appid_str
                    pfx_rel: tuple[str, ...] = ("pfx", "drive_c")
                    prefix_rel = (appid_str, *pfx_rel, section, *rel_parts[:-1])
                    game_id_key = appid_str
                elif prefix_key.startswith("standalone-"):
                    sprefix = standalone_by_key.get(prefix_key)
                    if sprefix is None and manifest:
                        old_name = manifest.get(prefix_key, "")
                        sprefix = standalone_by_exact.get(old_name) or standalone_by_norm.get(
                            _norm_key(old_name)
                        )
                    if sprefix is None or not sprefix.is_dir() or sprefix.is_symlink():
                        continue
                    target_root = sprefix
                    try:
                        target_root_resolved = sprefix.resolve()
                    except OSError:
                        continue
                    target_prefix = sprefix
                    pfx_rel = (
                        ("pfx", "drive_c")
                        if (sprefix / "pfx" / "drive_c").is_dir()
                        else ("drive_c",)
                    )
                    prefix_rel = (*pfx_rel, section, *rel_parts[:-1])
                    game_id_key = prefix_key
                else:
                    continue

                if not _safe_parents_within(target_root, target_root_resolved, prefix_rel):
                    continue
                dst_base = target_prefix.joinpath(*pfx_rel, section)
                dst_base.mkdir(parents=True, exist_ok=True)
                dst_base_resolved = dst_base.resolve()
                if not dst_base_resolved.is_relative_to(target_root_resolved):
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
                        backup_dir=target_prefix / ".deckinstall_bak",
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
                restored_games.add(game_id_key)
                restored_files += 1
        return len(restored_games), restored_files


def sync_prefix_symlinks(
    paths: SteamPaths,
    links_root: Path | None = None,
) -> tuple[int, int]:
    """Create Shortix-style named symlinks in ~/Games/.prefixes/<Game> -> compatdata/<appid>."""
    from .standalone import get_prefixes_dir

    root = links_root or get_prefixes_dir()
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
    "MANGOHUD_CONFIG",
    "PROTON_USE_WINED3D",
    "PROTON_NO_ESYNC",
    "PROTON_NO_FSYNC",
    "PROTON_DLSS_UPGRADE",
    "PROTON_ENABLE_NVAPI",
    "DXVK_ENABLE_NVAPI",
    "__NV_PRIME_RENDER_OFFLOAD",
    "__GLX_VENDOR_LIBRARY_NAME",
    "PROTON_ENABLE_WAYLAND",
    "WINEDLLOVERRIDES",
    "ENABLE_LSFG",
    "LSFG_MULTIPLIER",
    "LSFG_FLOW_SCALE",
    "LSFG_PERF_MODE",
    "LSFGVK_PROFILE",
)
_OPTISCALER_PROXY_DLLS = frozenset(
    {"dxgi", "amd_fidelityfx_dx12", "amd_fidelityfx_vk", "winmm", "version", "dbghelp"}
)
_SHELL_OPS = {"&&", "||", ";", "|", ">", ">>", "<", "2>&1", "1>&2", "%command%"}
_SAFE_UNQUOTED_DOLLAR_RE = re.compile(
    r"^[\w./:=+-]*\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})[\w./:=+-]*$"
)


def _validate_launch_options(launch_options: str) -> None:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in launch_options):
        raise ValueError("Launch options cannot contain control characters or newlines.")


def _is_env_assignment(tok: str) -> bool:
    if "=" not in tok or tok.startswith("-"):
        return False
    k, _ = tok.split("=", 1)
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))


def _is_clean_single_quoted(s: str) -> bool:
    return len(s) >= 2 and s[0] == "'" and s[-1] == "'" and "'" not in s[1:-1]


def _is_clean_double_quoted(s: str) -> bool:
    return (
        len(s) >= 2
        and s[0] == '"'
        and s[-1] == '"'
        and not any(ch in s[1:-1] for ch in ('"', "$", "`", "\\"))
    )


def _is_whole_quoted(s: str) -> bool:
    """Return True if s is a single complete '...' or "..." quoted string (allowing escapes in double quotes)."""
    if len(s) < 2:
        return False
    if s[0] == "'" and s[-1] == "'":
        return "'" not in s[1:-1]
    if s[0] == '"' and s[-1] == '"':
        i = 1
        while i < len(s) - 1:
            ch = s[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return False
            i += 1
        return i == len(s) - 1
    return False


def _is_balanced_shell_quoted(s: str) -> bool:
    if _is_whole_quoted(s):
        return True
    if "=" in s:
        head, _, val = s.partition("=")
        if head.startswith("-") and _is_whole_quoted(val):
            return True
    return False


def _format_launch_token(tok: str) -> str:
    if tok in _SHELL_OPS:
        return tok
    if _is_env_assignment(tok):
        k, v = tok.split("=", 1)
        if "$" in v and _SAFE_UNQUOTED_DOLLAR_RE.fullmatch(v) and "`" not in v and "$(" not in v:
            return f"{k}={v}"
        if _is_clean_single_quoted(v) or _is_clean_double_quoted(v) or _is_balanced_shell_quoted(v):
            return f"{k}={v}"
        return f"{k}={shlex.quote(v)}"
    for redir in (">>", ">", "<"):
        if tok.startswith(redir) and len(tok) > len(redir):
            rest = tok[len(redir):]
            if _is_clean_single_quoted(rest) or _is_clean_double_quoted(rest) or _is_balanced_shell_quoted(rest):
                return tok
    if "$" in tok and _SAFE_UNQUOTED_DOLLAR_RE.fullmatch(tok) and "`" not in tok and "$(" not in tok:
        return tok
    if _is_clean_single_quoted(tok) or _is_clean_double_quoted(tok) or _is_balanced_shell_quoted(tok):
        return tok
    return shlex.quote(tok)


def _format_winedlloverrides_token(winedlloverrides: str) -> str:
    val = winedlloverrides.strip()
    if not any(ch in val for ch in ('"', "$", "`", "\\")):
        return f'WINEDLLOVERRIDES="{val}"'
    return f"WINEDLLOVERRIDES={shlex.quote(val)}"


def _normalize_dll_name(dll_name: str) -> str:
    norm = dll_name.strip().lstrip("*").lower()
    if norm.endswith(".dll"):
        norm = norm[:-4]
    return norm


def _strip_dll_from_overrides(existing: str, target_norm: str) -> list[str]:
    kept_entries: list[str] = []
    for raw_entry in existing.split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" in entry:
            names_part, mode_part = entry.split("=", 1)
            mode_str: str | None = mode_part.strip()
        else:
            names_part = entry
            mode_str = None
        remaining_names = [
            name.strip()
            for name in names_part.split(",")
            if name.strip() and _normalize_dll_name(name) != target_norm
        ]
        if not remaining_names:
            continue
        names_joined = ",".join(remaining_names)
        kept_entries.append(f"{names_joined}={mode_str}" if mode_str is not None else names_joined)
    return kept_entries


def merge_winedlloverride(existing: str, dll_name: str, mode: str = "n,b") -> str:
    """Merge a DLL override into a WINEDLLOVERRIDES string without clobbering other DLLs."""
    norm = _normalize_dll_name(dll_name)
    if not norm:
        return existing.strip()
    entries = _strip_dll_from_overrides(existing, norm)
    clean_mode = mode.strip()
    entries.append(f"{norm}={clean_mode}" if clean_mode else norm)
    return ";".join(entries)


def remove_winedlloverride(existing: str, dll_name: str) -> str:
    """Remove a DLL override from a WINEDLLOVERRIDES string without clobbering sibling DLLs."""
    norm = _normalize_dll_name(dll_name)
    if not norm:
        return existing.strip()
    return ";".join(_strip_dll_from_overrides(existing, norm))


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
        "position=top-center\n"
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


def detect_optiscaler_lsfg_conflict(launch_options: str, winedlloverrides: str = "") -> bool:
    """Return True if lsfg-vk / ENABLE_LSFG is enabled alongside OptiScaler DLL overrides."""
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
                if norm in _OPTISCALER_PROXY_DLLS:
                    return True
    return False


def _is_managed_mangohud_config(value: str) -> bool:
    cleaned = value.strip("\"'").strip()
    if "preset=" in cleaned or (
        "legacy_layout=0" in cleaned and "horizontal" in cleaned and "hud_compact" in cleaned
    ):
        return True
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return bool(parts) and all(p.startswith(("position=", "fps_limit=")) for p in parts)


def _build_mangohud_config_parts(
    mangohud_level: int | None,
    mangohud_position: str,
    mangohud_fps_limit: int | None,
) -> list[str]:
    """Build managed MANGOHUD_CONFIG key-value parts from level, position, and FPS limit."""
    pos = mangohud_position.strip()
    if mangohud_level == 2:
        parts = [
            "legacy_layout=0",
            "horizontal",
            "hud_compact",
            f"position={pos or 'top-center'}",
            "fps",
            "frametime",
            "gpu_stats",
            "gpu_temp",
            "cpu_stats",
            "cpu_temp",
            "vram",
            "ram",
        ]
    elif mangohud_level in (1, 3, 4):
        parts = [f"preset={mangohud_level}"]
        if pos:
            parts.append(f"position={pos}")
    else:
        parts = [f"position={pos}"] if pos else []

    if mangohud_fps_limit and mangohud_fps_limit > 0:
        parts.append(f"fps_limit={mangohud_fps_limit}")
    return parts


_KNOWN_WRAPPERS = frozenset({"gamemoderun", "lsfg-vk", "mangohud", "gamescope"})


def apply_launch_presets(
    launch_options: str,
    gamemode: bool,
    mangohud: bool,
    wined3d: bool,
    no_esync: bool,
    no_fsync: bool,
    winedlloverrides: str | None = None,
    dlss_upgrade: bool = False,
    mangohud_level: int | None = None,
    nvidia_nvapi: bool = False,
    prime_offload: bool = False,
    proton_wayland: bool = False,
    mangohud_position: str = "",
    mangohud_fps_limit: int | None = None,
    lsfg: bool = False,
    lsfg_multiplier: int = 2,
    lsfg_flow_scale: float | None = None,
    lsfg_perf_mode: bool = False,
    lsfg_profile: str = "",
) -> str:
    """Toggle common SteamOS/Proton launch presets while preserving existing custom options."""
    _validate_launch_options(launch_options)
    if winedlloverrides is not None:
        _validate_launch_options(winedlloverrides)
    if lsfg_profile:
        _validate_launch_options(lsfg_profile)
    try:
        tokens = _raw_shell_tokens(launch_options) if launch_options.strip() else []
    except ValueError:
        tokens = launch_options.split()

    mh_parts = (
        _build_mangohud_config_parts(mangohud_level, mangohud_position, mangohud_fps_limit)
        if mangohud
        else []
    )

    # Strip existing managed presets
    filtered: list[str] = []
    for tok in tokens:
        if tok == "gamemoderun":
            continue
        if mangohud and tok == "mangohud":
            continue
        if _is_env_assignment(tok):
            env_key, env_val = tok.split("=", 1)
            if env_key == "MANGOHUD_CONFIG":
                if (
                    not mangohud
                    or bool(mh_parts)
                    or _is_managed_mangohud_config(env_val)
                ):
                    continue
            elif env_key == "WINEDLLOVERRIDES":
                if winedlloverrides is not None:
                    continue
            elif env_key in _PRESET_ENV_KEYS:
                continue
        filtered.append(tok)

    if "%command%" in filtered:
        cmd_idx = filtered.index("%command%")
        before_raw = filtered[:cmd_idx]
        after_cmd = filtered[cmd_idx + 1 :]
    else:
        before_raw = [t for t in filtered if _is_env_assignment(t) or t in _KNOWN_WRAPPERS]
        after_cmd = [t for t in filtered if not _is_env_assignment(t) and t not in _KNOWN_WRAPPERS]

    env_before = [t for t in before_raw if _is_env_assignment(t)]
    wrappers_before = [t for t in before_raw if not _is_env_assignment(t)]

    preset_envs: list[str] = []
    if mangohud:
        preset_envs.append("MANGOHUD=1")
        if mh_parts:
            preset_envs.append(f"MANGOHUD_CONFIG={','.join(mh_parts)}")
    if wined3d:
        preset_envs.append("PROTON_USE_WINED3D=1")
    if no_esync:
        preset_envs.append("PROTON_NO_ESYNC=1")
    if no_fsync:
        preset_envs.append("PROTON_NO_FSYNC=1")
    if dlss_upgrade:
        preset_envs.append("PROTON_DLSS_UPGRADE=1")
    if nvidia_nvapi:
        preset_envs.append("PROTON_ENABLE_NVAPI=1")
        preset_envs.append("DXVK_ENABLE_NVAPI=1")
    if prime_offload:
        preset_envs.append("__NV_PRIME_RENDER_OFFLOAD=1")
        preset_envs.append("__GLX_VENDOR_LIBRARY_NAME=nvidia")
    if proton_wayland:
        preset_envs.append("PROTON_ENABLE_WAYLAND=1")
    if lsfg:
        preset_envs.append("ENABLE_LSFG=1")
        preset_envs.append(f"LSFG_MULTIPLIER={int(lsfg_multiplier)}")
        if lsfg_flow_scale is not None:
            preset_envs.append(f"LSFG_FLOW_SCALE={float(lsfg_flow_scale):g}")
        if lsfg_perf_mode:
            preset_envs.append("LSFG_PERF_MODE=1")
        if lsfg_profile.strip():
            preset_envs.append(f"LSFGVK_PROFILE={lsfg_profile.strip()}")
    if winedlloverrides is not None and winedlloverrides.strip():
        preset_envs.append(_format_winedlloverrides_token(winedlloverrides))

    wrappers_out: list[str] = []
    if gamemode:
        wrappers_out.append("gamemoderun")
    wrappers_out.extend(wrappers_before)

    if not env_before and not preset_envs and not wrappers_out and not after_cmd:
        return ""
    final_tokens = env_before + preset_envs + wrappers_out + ["%command%"] + after_cmd
    return " ".join(_format_launch_token(tok) for tok in final_tokens)


STEAM_INPUT_MODES = ("default", "enabled", "disabled")
_STEAM_INPUT_VALUE_BY_MODE = {
    "disabled": "0",
    "default": "1",
    "enabled": "2",
}
_STEAM_INPUT_MODE_BY_VALUE = {v: k for k, v in _STEAM_INPUT_VALUE_BY_MODE.items()}


def get_steam_input_mode(localconfig_vdf: Path, appid_u32: int) -> str:
    """Return per-game Steam Input override ('default', 'enabled', or 'disabled') from localconfig.vdf."""
    if not localconfig_vdf.is_file():
        return "default"
    try:
        data = _load_vdf_mapping(localconfig_vdf)
        cur: Any = data
        for sec in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", str(appid_u32)):
            cur = _get_vdf_section(cur, sec)
            if cur is None:
                return "default"
        if not hasattr(cur, "items"):
            return "default"
        for k, v in cur.items():
            if str(k).lower() == "usesteamcontrollerconfig":
                return _STEAM_INPUT_MODE_BY_VALUE.get(str(v).strip(), "default")
    except Exception:
        return "default"
    return "default"


def set_steam_input_mode(
    localconfig_vdf: Path,
    appid_u32: int,
    mode: str,
    create_if_missing: bool = False,
) -> None:
    """Set per-game Steam Input override in localconfig.vdf atomically under _steam_transaction_lock."""
    mode_norm = mode.strip().lower()
    if mode_norm not in STEAM_INPUT_MODES:
        raise ValueError(f"Invalid Steam Input mode: {mode!r} (expected one of {STEAM_INPUT_MODES})")
    with _steam_transaction_lock(localconfig_vdf):
        if localconfig_vdf.is_file():
            _backup_file(localconfig_vdf)
            data = _load_vdf_mapping(localconfig_vdf)
        elif create_if_missing:
            data = getattr(vdf, "VDFDict", dict)()
        else:
            return
        root = _ensure_vdf_section(data, "UserLocalConfigStore")
        software = _ensure_vdf_section(root, "Software")
        valve = _ensure_vdf_section(software, "Valve")
        steam = _ensure_vdf_section(valve, "Steam")
        apps = _ensure_vdf_section(steam, "apps")
        app_sec = _ensure_vdf_section(apps, str(appid_u32))
        target_key = "UseSteamControllerConfig"
        existing_keys = [k for k in list(app_sec.keys()) if str(k).lower() == "usesteamcontrollerconfig"]
        if existing_keys:
            target_key = str(existing_keys[0])
            for k in existing_keys:
                while k in app_sec:
                    del app_sec[k]
        app_sec[target_key] = _STEAM_INPUT_VALUE_BY_MODE[mode_norm]
        _dump_vdf_mapping(localconfig_vdf, data)


_MOUNT_CONTAINER_PREFIXES = frozenset({"/", "/run", "/run/media", "/media", "/mnt"})


def _is_path_on_mounted_volume(path: Path, mount_root: Path | None = None) -> bool:
    """Return True if path resides on a mounted volume distinct from mount_root/system roots."""
    try:
        if path.is_symlink() or not path.exists():
            return False
        if os.path.ismount(path):
            return True
        if mount_root is not None and mount_root.exists():
            return path.stat().st_dev != mount_root.stat().st_dev
        cur = path
        while cur != cur.parent and str(cur) not in _MOUNT_CONTAINER_PREFIXES:
            if os.path.ismount(cur):
                return True
            cur = cur.parent
    except OSError:
        return False
    return False


def list_microsd_compatdata_targets(
    media_root: Path | None = None,
    steam_root: Path | None = None,
) -> list[Path]:
    """Return candidate compatdata directories on mounted microSD/removable volumes."""
    root = media_root or Path("/run/media")
    skip_names = {"lost+found", "System Volume Information"}
    volume_roots: list[Path] = []
    if root.is_dir() and not root.is_symlink():
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir() or entry.name.startswith(".") or entry.name in skip_names:
                continue
            if _is_path_on_mounted_volume(entry, mount_root=root):
                volume_roots.append(entry)
                continue
            try:
                sub_entries = sorted(entry.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for sub in sub_entries:
                if sub.is_symlink() or not sub.is_dir() or sub.name.startswith(".") or sub.name in skip_names:
                    continue
                if _is_path_on_mounted_volume(sub, mount_root=root):
                    volume_roots.append(sub)

    if steam_root is not None:
        lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        if lib_vdf.is_file():
            try:
                data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
                folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
                for val in folders.values():
                    if isinstance(val, dict) and val.get("path"):
                        lib_path = Path(str(val["path"]))
                        try:
                            lib_dev = lib_path.stat().st_dev
                            steam_dev = steam_root.stat().st_dev
                        except OSError:
                            continue
                        if (
                            lib_path.is_dir()
                            and not lib_path.is_symlink()
                            and lib_dev != steam_dev
                            and _is_path_on_mounted_volume(lib_path)
                        ):
                            volume_roots.append(lib_path)
            except Exception:
                pass

    targets: list[Path] = []
    seen: set[Path] = set()
    for vol in volume_roots:
        target = (
            vol / "steamapps" / "compatdata"
            if (vol / "steamapps").is_dir()
            else vol / "DeckInstall" / "compatdata"
        )
        try:
            key = Path(os.path.abspath(target))
        except OSError:
            key = target
        if key not in seen:
            seen.add(key)
            targets.append(target)
    targets.sort(key=lambda p: str(p))
    return targets


def get_compatdata_migration_state(paths: SteamPaths, appid_u32: int) -> tuple[str, Path | None]:
    """Return (state, target_path) for compatdata/<appid_u32>: local, microsd, symlink, broken_symlink, or missing."""
    compat_path = paths.compatdata_dir / str(appid_u32)
    if compat_path.is_symlink():
        target = Path(os.path.realpath(compat_path))
        if target.is_dir():
            state = "microsd" if _is_path_on_mounted_volume(target) else "symlink"
            return state, target
        return "broken_symlink", target
    if compat_path.is_dir():
        return "local", compat_path
    return "missing", None


def _verify_compatdata_copy(source: Path, target: Path) -> None:
    """Verify every directory, regular file and symlink before removing a prefix's original copy."""
    def fingerprint(root: Path) -> dict[str, tuple[str, str]]:
        entries: dict[str, tuple[str, str]] = {}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise_oserror):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    entries[rel] = ("symlink", os.readlink(path))
                elif stat.S_ISDIR(mode):
                    entries[rel] = ("dir", "")
                elif stat.S_ISREG(mode):
                    entries[rel] = ("file", _file_sha256(path))
                else:
                    raise RuntimeError(f"Unsupported prefix file: {path}")
        return entries

    if fingerprint(source) != fingerprint(target):
        raise RuntimeError("Prefix copy verification failed; the original prefix was preserved")


def _validate_migration_appid(appid_u32: int) -> None:
    if isinstance(appid_u32, bool) or not isinstance(appid_u32, int) or not 1 <= appid_u32 <= 0xFFFFFFFF:
        raise ValueError(f"Invalid compatdata AppID: {appid_u32!r}")


def migrate_compatdata_to_microsd(
    paths: SteamPaths,
    appid_u32: int,
    target_compat_root: Path,
    verify_mounted: bool = True,
) -> Path:
    """Move compatdata/<appid_u32> to microSD and symlink it, keeping shadercache on NVMe."""
    _validate_migration_appid(appid_u32)
    target_compat_root = target_compat_root.resolve()
    with _steam_transaction_lock(paths.shortcuts_vdf):
        src_compat = paths.compatdata_dir / str(appid_u32)
        if src_compat.is_symlink():
            raise ValueError(f"compatdata/{appid_u32} is already a symlink: {src_compat}")
        if not src_compat.is_dir():
            raise FileNotFoundError(f"compatdata/{appid_u32} does not exist: {src_compat}")
        if target_compat_root.resolve().is_relative_to(src_compat.resolve()):
            raise ValueError("Migration destination cannot be inside the source prefix")
        if verify_mounted:
            probe = target_compat_root
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if not _is_path_on_mounted_volume(probe):
                raise ValueError(f"Target is not on a mounted volume: {target_compat_root}")
        target_compat_root.mkdir(parents=True, exist_ok=True)
        dst_compat = target_compat_root / str(appid_u32)
        if dst_compat.exists() or dst_compat.is_symlink():
            raise FileExistsError(f"Destination already exists: {dst_compat}")

        needed_bytes = _dir_size_bytes(src_compat)
        check_disk_space(target_compat_root, needed_bytes)

        stage_dir = Path(
            tempfile.mkdtemp(prefix=f".{appid_u32}.", suffix=".stage", dir=str(target_compat_root))
        )
        staged_compat = stage_dir / str(appid_u32)
        backup_local = paths.compatdata_dir / f".{appid_u32}.{int(time.time())}.{os.getpid()}.migrate.bak"
        try:
            shutil.copytree(src_compat, staged_compat, symlinks=True)
            _verify_compatdata_copy(src_compat, staged_compat)
            staged_compat.rename(dst_compat)
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            shutil.rmtree(dst_compat, ignore_errors=True)
            raise
        try:
            stage_dir.rmdir()
        except OSError:
            pass

        try:
            src_compat.rename(backup_local)
        except Exception:
            shutil.rmtree(dst_compat, ignore_errors=True)
            raise

        try:
            src_compat.symlink_to(dst_compat)
        except Exception:
            try:
                if src_compat.is_symlink():
                    src_compat.unlink()
            except OSError:
                pass
            try:
                backup_local.rename(src_compat)
            except OSError:
                pass
            shutil.rmtree(dst_compat, ignore_errors=True)
            raise

        shutil.rmtree(backup_local, ignore_errors=True)
        return dst_compat


def restore_compatdata_from_microsd(
    paths: SteamPaths,
    appid_u32: int,
    remove_microsd_copy: bool = True,
) -> Path:
    """Restore a symlinked compatdata/<appid_u32> back to local NVMe storage atomically."""
    _validate_migration_appid(appid_u32)
    with _steam_transaction_lock(paths.shortcuts_vdf):
        compat_link = paths.compatdata_dir / str(appid_u32)
        if not compat_link.is_symlink():
            raise ValueError(f"compatdata/{appid_u32} is not a migrated symlink: {compat_link}")
        src_microsd = Path(os.path.realpath(compat_link))
        if not src_microsd.is_dir():
            raise FileNotFoundError(f"Migrated compatdata target not found: {src_microsd}")
        if (
            src_microsd.name != str(appid_u32)
            or paths.compatdata_dir.resolve().is_relative_to(src_microsd)
            or not ((src_microsd / "pfx" / "drive_c").is_dir() or (src_microsd / "drive_c").is_dir())
        ):
            raise ValueError(f"Symlink target is not a migrated game prefix: {src_microsd}")

        needed_bytes = _dir_size_bytes(src_microsd)
        check_disk_space(paths.compatdata_dir, needed_bytes)

        stage_dir = Path(
            tempfile.mkdtemp(prefix=f".{appid_u32}.", suffix=".restore", dir=str(paths.compatdata_dir))
        )
        staged_local = stage_dir / str(appid_u32)
        try:
            shutil.copytree(src_microsd, staged_local, symlinks=True)
            _verify_compatdata_copy(src_microsd, staged_local)
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

        compat_link.unlink()
        try:
            staged_local.rename(compat_link)
        except Exception:
            try:
                compat_link.symlink_to(src_microsd)
            except OSError:
                pass
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

        try:
            stage_dir.rmdir()
        except OSError:
            pass

        if remove_microsd_copy:
            shutil.rmtree(src_microsd, ignore_errors=True)
        return compat_link
