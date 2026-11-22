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
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vdf

from .extractor import check_disk_space
from .fsutil import atomic_write_bytes, file_sha256
from .manifest import read_manifest_fields
from .scanner import clean_game_name, detect_steam_appid
from .steam_paths import (
    SteamPaths,
    dir_size_bytes,
    find_default_proton_name,
    invalidate_process_cache,
    is_flatpak_steam,
    is_gamescope_session,
    is_steam_running,
    list_steam_users,
    read_library_paths,
)

DEFAULT_SAVES_DIR = Path.home() / "Games" / "saves"
# Newest save snapshots kept per game and full saves-*.zip backups kept by cleanup.
SNAPSHOT_KEEP_DEFAULT = 3
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


def _entry_appid_u32(item: dict) -> int | None:
    """Resolve a shortcut dict's unsigned AppID, recomputing missing or 0 IDs like load_shortcuts."""
    if "appid" in item:
        parsed = _parse_appid_u32(item["appid"])
        if parsed is None:
            return None
        if parsed != 0:
            return parsed
    exe = str(item.get("Exe") or item.get("exe") or "")
    app_name = str(item.get("AppName") or item.get("appname") or "")
    if not exe and not app_name:
        return None
    u32, _ = compute_shortcut_appid(exe, app_name)
    return u32


def _quote_path(path: str) -> str:
    stripped = path.strip().strip('"')
    return f'"{stripped}"'


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to path while holding the global Steam lock."""
    with _STEAM_LOCK:
        atomic_write_bytes(path, data)


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
        try:
            src_fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
            src_fh = os.fdopen(src_fd, "rb")
        except Exception:
            os.close(fd)
            raise
        with src_fh, os.fdopen(fd, "wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh, 65536)
            dst_fh.flush()
            os.fsync(dst_fh.fileno())
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


def load_shortcuts(shortcuts_vdf: Path, *, strict: bool = False) -> list[ShortcutEntry]:
    """Read all Non-Steam shortcuts from shortcuts.vdf."""
    try:
        raw = shortcuts_vdf.read_bytes()
        if not raw and not strict:
            return []
        data = vdf.binary_loads(raw)
        if not isinstance(data.get("shortcuts"), dict):
            raise ValueError("Invalid shortcuts.vdf structure")
    except FileNotFoundError:
        return []
    except Exception as exc:
        if strict:
            raise ValueError(f"Could not read {shortcuts_vdf}: {exc}") from exc
        logging.warning("Failed to parse %s: %s", shortcuts_vdf, exc)
        return []
    entries: list[ShortcutEntry] = []
    for idx, item in data["shortcuts"].items():
        if not isinstance(item, dict):
            if strict:
                raise ValueError("Invalid shortcut record")
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
    """Gracefully shut down Steam and wait for exit.

    Raises SteamGamingModeError in SteamOS Gaming Mode: closing Steam there ends the session.
    """
    invalidate_process_cache()
    try:
        return _shutdown_steam(timeout_seconds, steam_root)
    finally:
        invalidate_process_cache()


def _shutdown_steam(timeout_seconds: int, steam_root: Path | None) -> bool:
    if not is_steam_running():
        return True
    if is_gamescope_session():
        raise SteamGamingModeError("Steam cannot be closed in Gaming Mode")
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
        invalidate_process_cache()
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
    invalidate_process_cache()
    return not is_steam_running()


_STEAM_ENV_DROP = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONNOUSERSITE",
    "QT_QPA_PLATFORMTHEME",
    "QT_QPA_PLATFORM",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)


_STEAM_STABLE_SECONDS = 3.0


class SteamGamingModeError(RuntimeError):
    """Raised instead of closing Steam inside SteamOS Gaming Mode (it would end the session)."""


def _steam_launch_env() -> dict[str, str]:
    """Environment for Steam without the AppImage's bundled libraries, Python and Qt settings."""
    from .app_updater import build_clean_relaunch_env

    env = build_clean_relaunch_env()
    for key in _STEAM_ENV_DROP:
        env.pop(key, None)
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    return env


def start_steam(steam_root: Path | None = None, wait_seconds: float = 20.0) -> bool:
    """Start Steam in its own session with a clean environment; return True once it is running.

    Inside Gaming Mode the session restarts Steam by itself, so nothing is launched there.
    """
    invalidate_process_cache()
    try:
        return _start_steam(steam_root, wait_seconds)
    finally:
        invalidate_process_cache()


def _start_steam(steam_root: Path | None, wait_seconds: float) -> bool:
    if is_steam_running():
        return True
    if is_gamescope_session():
        return False
    cmd = (
        ["flatpak", "run", "com.valvesoftware.Steam", "-silent"]
        if is_flatpak_steam(steam_root)
        else [shutil.which("steam") or "/usr/bin/steam", "-silent"]
    )
    # Own systemd scope: Steam must not share DeckInstall's cgroup, or it dies when that is cleaned up
    # (app closed, ssh/tailscale session ended).
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        cmd = [systemd_run, "--user", "--scope", "--collect", "--quiet", *cmd]
    try:
        subprocess.Popen(
            cmd,
            env=_steam_launch_env(),
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        logging.exception("Could not launch Steam with %s", cmd)
        return False
    deadline = time.monotonic() + wait_seconds
    seen_since: float | None = None
    while time.monotonic() < deadline:
        now = time.monotonic()
        invalidate_process_cache()
        if is_steam_running():
            # The bootstrap script also matches for a moment and may exit (e.g. no display):
            # only count Steam as up once it is still running a few seconds later.
            if seen_since is None:
                seen_since = now
            elif now - seen_since >= _STEAM_STABLE_SECONDS:
                return True
        else:
            seen_since = None
        time.sleep(0.5)
    logging.warning("Steam did not appear within %.0f s after %s", wait_seconds, cmd)
    return False


def _is_same_game_entry(item: dict, exe_path: Path, start_dir: Path) -> bool:
    """Decide whether a same-named shortcut refers to the game being added.

    Two different games may share an AppName (e.g. "Launcher"); only treat a name match as
    the same game when its folder overlaps the new one or its executable is gone from a
    non-removable location (the game folder was moved/renamed).
    """
    old_exe_raw = str(item.get("Exe") or item.get("exe") or "").strip().strip('"')
    if not old_exe_raw:
        return True
    old_exe = Path(old_exe_raw)
    old_dir_raw = str(item.get("StartDir") or item.get("startdir") or "").strip().strip('"')
    old_dir = Path(old_dir_raw) if old_dir_raw else old_exe.parent
    if old_exe == exe_path:
        return True
    for a, b in ((old_dir, start_dir), (old_exe.parent, exe_path.parent)):
        if a == b or a in b.parents or b in a.parents:
            return True
    removable = ("/run/media/", "/media/", "/mnt/")
    if not old_exe_raw.startswith(removable):
        try:
            if not old_exe.exists():
                return True
        except OSError:
            return False
    return False


def add_or_update_shortcut(
    paths: SteamPaths,
    app_name: str,
    exe_path: Path,
    start_dir: Path | None = None,
    icon_path: Path | None = None,
    launch_options: str | None = None,
    enable_proton: bool = True,
    preserve_from_appid: int | None = None,
    *,
    match_existing_name: bool = True,
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
            item_u32 = _entry_appid_u32(item)
            if item_u32 is None:
                filtered.append(item)
                continue
            if preserve_from_appid is not None and item_u32 == preserve_from_appid:
                existing_meta = dict(item)
                continue
            if item_u32 == appid_u32 or (
                match_existing_name and item_name == app_name
                and _is_same_game_entry(item, exe_path, start_dir or exe_path.parent)
            ):
                if not existing_meta:
                    existing_meta = dict(item)
                if item_u32 and item_u32 != appid_u32 and item_u32 != preserve_from_appid:
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
            item_u32 = _entry_appid_u32(item)
            if item_u32 is not None and item_u32 == appid_u32:
                item["LaunchOptions"] = launch_options
                updated = True
                break
        if not updated:
            return False
        _backup_file(paths.shortcuts_vdf)
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))
        return True


def update_shortcut_paths(
    paths: SteamPaths,
    appid_u32: int,
    exe_path: Path,
    start_dir: Path,
    *,
    expected_exe: str,
    allow_existing_old: bool = False,
) -> bool:
    """Point an existing shortcut at a new exe/StartDir keeping its AppID (artwork, compatdata, playtime)."""
    with _steam_transaction_lock(paths.shortcuts_vdf):
        if paths.shortcuts_vdf.is_symlink() or not paths.shortcuts_vdf.is_file():
            return False
        if not exe_path.is_absolute() or not exe_path.is_file() or not start_dir.is_dir():
            raise ValueError(f"Shortcut destination unavailable: {exe_path}")
        data = vdf.binary_loads(paths.shortcuts_vdf.read_bytes())
        matches = [
            item for item in data.get("shortcuts", {}).values()
            if isinstance(item, dict) and _entry_appid_u32(item) == appid_u32
        ]
        if len(matches) != 1:
            return False
        item = matches[0]
        exe_key = "Exe" if "Exe" in item else "exe"
        current = _strip_vdf_quotes(str(item.get(exe_key) or ""))
        if current == str(exe_path):
            return True
        old = Path(current)
        if current != _strip_vdf_quotes(expected_exe) or not old.is_absolute():
            raise ValueError(f"Shortcut target changed or is available again: {current}")
        if not allow_existing_old and (old.exists() or old.is_symlink()):
            raise ValueError(f"Shortcut target changed or is available again: {current}")
        start_key = "startdir" if "startdir" in item and "StartDir" not in item else "StartDir"
        raw_id = _parse_appid_u32(item.get("appid", 0))
        if not raw_id:
            item["appid"] = appid_u32 - (1 << 32) if appid_u32 >= 0x80000000 else appid_u32
        item[exe_key] = _quote_path(str(exe_path))
        item[start_key] = _quote_path(str(start_dir))
        _backup_file(paths.shortcuts_vdf)
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))
        return True


def _other_steam_user_has_shortcut(paths: SteamPaths, appid_u32: int) -> bool | None:
    """Return True if another Steam user has appid_u32, False if confirmed absent, None if unreadable."""
    for uid in list_steam_users(paths.steam_root):
        if uid == paths.user_id:
            continue
        other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
        if not other_sv.is_file():
            continue
        try:
            entries = load_shortcuts(other_sv, strict=True)
        except ValueError:
            return None
        if any(e.appid_u32 == appid_u32 for e in entries):
            return True
    return False


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
            item_u32 = _entry_appid_u32(item)
            if item_u32 is not None and item_u32 == appid_u32:
                removed = True
                continue
            kept.append(item)
        if not removed:
            return False
        data["shortcuts"] = {str(i): entry for i, entry in enumerate(kept)}
        _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))
        try:
            if _other_steam_user_has_shortcut(paths, appid_u32) is False:
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


def _vdf_matching_keys(mapping: Any, key: str) -> list[str]:
    low = key.lower()
    seen: list[str] = []
    for k in list(mapping.keys()):
        ks = str(k)
        if ks.lower() == low and ks not in seen:
            seen.append(ks)
    return seen


def _vdf_delete_all(mapping: Any, key: str) -> bool:
    """Remove every occurrence (case-insensitive) of key from a dict or VDFDict."""
    removed = False
    for mk in _vdf_matching_keys(mapping, key):
        while mk in mapping:
            del mapping[mk]
            removed = True
    return removed


def _vdf_set_unique(mapping: Any, key: str, value: Any) -> None:
    """Set key uniquely in a dict or VDFDict without creating duplicate VDFDict entries."""
    matched = _vdf_matching_keys(mapping, key)
    target_key = matched[0] if matched else key
    _vdf_delete_all(mapping, key)
    mapping[target_key] = value


def _ensure_vdf_section(mapping: Any, key: str) -> Any:
    existing = _get_vdf_section(mapping, key)
    if existing is not None and hasattr(existing, "items"):
        return existing
    new_sec: Any = getattr(vdf, "VDFDict", dict)()
    _vdf_set_unique(mapping, key, new_sec)
    return _get_vdf_section(mapping, key)


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
        _vdf_set_unique(
            mapping,
            str(appid_u32),
            {
                "name": tool_name,
                "config": "",
                "priority": "250",
            },
        )
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
        if hasattr(mapping, "items") and _vdf_matching_keys(mapping, str(appid_u32)):
            _backup_file(config_vdf)
            _vdf_delete_all(mapping, str(appid_u32))
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
    steamapps_dirs.extend(lib / "steamapps" for lib in read_library_paths(steam_root))
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


@dataclass(frozen=True)
class SteamLibraryGame:
    """An installed Steam (store) game that runs through Proton and has a locatable Windows exe."""

    appid: int
    name: str
    install_dir: Path
    exe_path: Path
    prefix_dir: Path


def _localconfig_apps(data: Any, create: bool = False) -> Any | None:
    node = data
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        child = _get_vdf_section(node, key)
        if child is None:
            if not create or not hasattr(node, "items"):
                return None
            child = type(node)()
            node[key] = child
        node = child
    return node


def get_steam_app_launch_options(paths: SteamPaths, appid: int) -> str:
    """Return the LaunchOptions of a Steam store game from localconfig.vdf ("" if unset)."""
    cfg = paths.localconfig_vdf
    if not cfg.is_file():
        return ""
    try:
        apps = _localconfig_apps(_load_vdf_mapping(cfg))
    except Exception:
        return ""
    app = _get_vdf_section(apps, str(appid)) if apps is not None else None
    value = _get_vdf_section(app, "LaunchOptions") if app is not None else None
    return str(value or "")


def set_steam_app_launch_options(paths: SteamPaths, appid: int, launch_options: str) -> bool:
    """Write LaunchOptions of a Steam store game into localconfig.vdf (backup first). Steam must be closed."""
    cfg = paths.localconfig_vdf
    with _steam_transaction_lock(cfg):
        if not cfg.is_file():
            return False
        data = _load_vdf_mapping(cfg)
        apps = _localconfig_apps(data, create=True)
        if apps is None:
            return False
        app = _ensure_vdf_section(apps, str(appid))
        key = next((str(k) for k in app if str(k).lower() == "launchoptions"), "LaunchOptions")
        _vdf_set_unique(app, key, launch_options)
        _backup_file(cfg)
        _dump_vdf_mapping(cfg, data)
        return True


def list_steam_library_games(paths: SteamPaths) -> list[SteamLibraryGame]:
    """List installed Steam store games with a Proton prefix and a Windows .exe found in their folder."""
    from .scanner import scan_game_folder

    out: list[SteamLibraryGame] = []
    seen: set[int] = set()
    steamapps_dirs = [paths.steam_root / "steamapps"]
    steamapps_dirs.extend(lib / "steamapps" for lib in read_library_paths(paths.steam_root))
    for sa in steamapps_dirs:
        if not sa.is_dir():
            continue
        for acf in sorted(sa.glob("appmanifest_*.acf")):
            stem_id = acf.stem.removeprefix("appmanifest_")
            if not (stem_id.isascii() and stem_id.isdigit()):
                continue
            appid = int(stem_id)
            if appid in seen:
                continue
            try:
                parsed = vdf.loads(acf.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            state = parsed.get("AppState") or parsed.get("appstate") or {}
            if not isinstance(state, dict) or not state.get("installdir"):
                continue
            install_dir = sa / "common" / str(state["installdir"])
            prefix_dir = next(
                (
                    c / str(appid) / "pfx"
                    for c in (sa / "compatdata", paths.compatdata_dir)
                    if (c / str(appid) / "pfx").is_dir()
                ),
                None,
            )
            if prefix_dir is None or not install_dir.is_dir():
                continue  # native Linux game or never launched with Proton
            try:
                detected = scan_game_folder(install_dir)
            except OSError:
                continue
            if detected is None or detected.primary_exe.suffix.lower() != ".exe":
                continue
            seen.add(appid)
            out.append(
                SteamLibraryGame(
                    appid=appid,
                    name=str(state.get("name") or f"AppID {appid}"),
                    install_dir=install_dir,
                    exe_path=detected.primary_exe,
                    prefix_dir=prefix_dir,
                )
            )
    return out


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
    """Migrate multiple AppIDs in CompatToolMapping to target_tool ("" removes them) atomically."""
    cleaned = target_tool.strip()
    if not appids:
        return 0
    if not cleaned:
        return remove_compat_tools(config_vdf, appids)
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
            _vdf_set_unique(
                mapping,
                str(appid),
                {
                    "name": target_tool.strip(),
                    "config": "",
                    "priority": "250",
                },
            )
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
            if _vdf_matching_keys(mapping, str(appid)):
                if removed == 0:
                    _backup_file(config_vdf)
                if _vdf_delete_all(mapping, str(appid)):
                    removed += 1
        if removed:
            _dump_vdf_mapping(config_vdf, data)
        return removed


def orphan_shortcut_mappings(paths: SteamPaths) -> list[int]:
    """Find Non-Steam mappings absent from every readable Steam shortcut file."""
    known: set[int] = set()
    userdata = paths.steam_root / "userdata"
    for account in userdata.iterdir():
        if account.name.isascii() and account.name.isdigit():
            sv = account / "config" / "shortcuts.vdf"
            known.update(entry.appid_u32 for entry in load_shortcuts(sv, strict=True))
    mapping = get_compat_tool_map(paths.config_vdf)
    return sorted(appid for appid in mapping if appid >= 0x80000000 and appid not in known)


def remove_orphan_shortcut_mappings(paths: SteamPaths, appids: list[int]) -> int:
    """Recheck the list before a confirmed cleanup, including edits queued with Steam open."""
    with _steam_transaction_lock(paths.config_vdf):
        still_orphaned = set(orphan_shortcut_mappings(paths))
        return remove_compat_tools(paths.config_vdf, [appid for appid in appids if appid in still_orphaned])


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
    "deckinstall",
    "heroic",
    "lutris",
    "prefixes",
    "wine-prefixes",
    "bottles",
    "umu",
    "steam",
    "hydralauncher",
}


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


def _is_unreal_game_root(directory: Path) -> bool:
    """An Unreal Engine game root: Engine/ next to <Project>/Binaries (one game, not a container)."""
    try:
        if not (directory / "Engine").is_dir():
            return False
        with os.scandir(directory) as it:
            return any(
                entry.is_dir(follow_symlinks=False)
                and entry.name != "Engine"
                and (Path(entry.path) / "Binaries").is_dir()
                for entry in it
            )
    except OSError:
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
    if _is_unreal_game_root(directory):
        return False
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
    home_resolved = home.resolve()
    known_containers = {
        p.resolve()
        for p in (
            home,
            home / "Games",
            home / "DeckInstall",
            home / "DeckInstall" / "Games",
            home / "DeckInstall" / "Prefixes",
            home / "DeckInstall" / "Caches",
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
            Path("/var/mnt"),
            Path("/media"),
            Path("/run/media"),
        )
    }
    resolved = exe_path.resolve()
    if any(part.lower() == "drive_c" for part in resolved.parts):
        raise ValueError(
            f"O executável está dentro de um prefixo Wine (drive_c): {resolved}. "
            "Mova o jogo para uma pasta própria antes de migrar."
        )
    tmp_containers = {Path("/tmp").resolve(), Path("/var/tmp").resolve()}

    # Detect real mount roots or DeckInstall library roots anywhere above the executable.
    real_mount: Path | None = None
    media_prefixes = (
        ("/", "run", "media"),
        ("/", "media"),
        ("/", "mnt"),
        ("/", "var", "mnt"),
    )
    for parent in resolved.parents:
        if (
            parent == Path("/")
            or parent in tmp_containers
            or parent == home_resolved
            or parent in home_resolved.parents
        ):
            continue
        try:
            is_mount = os.path.ismount(parent)
            has_lib = (parent / "library.json").is_file()
        except OSError:
            is_mount = False
            has_lib = False
        if is_mount or has_lib:
            if is_mount and real_mount is None and any(parent.parts[: len(pfx)] == pfx for pfx in media_prefixes):
                real_mount = parent
            for extra in (
                parent,
                parent / "Games",
                parent / "DeckInstall",
                parent / "DeckInstall" / "Games",
                parent / "DeckInstall" / "Prefixes",
                parent / "DeckInstall" / "Caches",
            ):
                try:
                    known_containers.add(extra.resolve() if extra.exists() or extra.is_symlink() else extra)
                except OSError:
                    known_containers.add(extra)
            if real_mount is not None and has_lib:
                break

    def _is_media_mount_container(p: Path) -> bool:
        if p == Path("/") or p in tmp_containers or p == home_resolved or p in home_resolved.parents:
            return False
        try:
            if os.path.ismount(p) or (p / "library.json").is_file():
                return True
        except OSError:
            pass
        in_media_tree = (
            p.parts[:3] == ("/", "run", "media")
            or p.parts[:2] == ("/", "media")
            or p.parts[:2] == ("/", "mnt")
            or p.parts[:3] == ("/", "var", "mnt")
        )
        if real_mount is not None:
            return in_media_tree and (p == real_mount or p in real_mount.parents)
        return (
            (p.parts[:3] == ("/", "run", "media") and len(p.parts) <= 5)
            or (p.parts[:2] == ("/", "media") and len(p.parts) <= 4)
            or (p.parts[:2] == ("/", "mnt") and len(p.parts) <= 3)
            or (p.parts[:3] == ("/", "var", "mnt") and len(p.parts) <= 4)
        )

    def _is_container(p: Path) -> bool:
        if p in known_containers or _is_media_mount_container(p):
            return True
        if p.name.lower() in ("games", "deckinstall", "prefixes", "caches") and _is_media_mount_container(p.parent):
            return True
        return (
            p.name.lower() == "games"
            and p.parent.name.lower() == "deckinstall"
            and _is_media_mount_container(p.parent.parent)
        )

    if _is_container(resolved.parent) or _looks_like_multi_game_container(resolved.parent):
        raise ValueError(
            f"O executável está solto diretamente em {resolved.parent}; "
            "coloque-o dentro de uma subpasta própria antes de migrar."
        )
    candidate = resolved.parent
    for parent in resolved.parents:
        if parent == parent.parent:
            continue
        if _is_container(parent):
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
        if _is_container(parent.parent) or _looks_like_multi_game_container(parent.parent):
            candidate = parent
            break
    else:
        while (
            candidate.name.lower() in _SUBDIR_NAMES
            and candidate.parent != candidate
            and not _is_container(candidate.parent)
            and not _looks_like_multi_game_container(candidate.parent)
        ):
            candidate = candidate.parent
    if (
        candidate.name.lower() in _FORBIDDEN_ROOT_NAMES
        or _is_container(candidate)
        or _looks_like_multi_game_container(candidate)
    ):
        raise ValueError(f"Raiz inferida inválida ou protegida para migração: {candidate}")
    return candidate


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


def external_prefix_roots(include_configured: bool = True) -> list[Path]:
    """Return candidate directories where Hydra, Heroic, Lutris, Bottles, and UMU store prefixes.

    The usual locations plus, with include_configured, the ones each launcher's settings point to
    (custom prefix folders). Those can be broad (the parent of a per-game prefix), so they are
    only for finding a game's prefix by name, never for listing prefixes to delete.
    """
    from .launcher_scan import configured_prefix_roots

    home = Path.home()
    roots = [
        home / ".config" / "hydralauncher" / "wine-prefixes",
        home / ".config" / "hydralauncher" / "umu",
        home / "Games" / "Heroic" / "Prefixes",
        # Heroic before defaultWinePrefixDir: per-game prefixes went inside Prefixes/default/<title>.
        home / "Games" / "Heroic" / "Prefixes" / "default",
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
    if not include_configured:
        return roots
    try:
        extra = configured_prefix_roots(home)
    except Exception:  # a broken launcher config must never break the prefix search
        extra = []
    return list(dict.fromkeys([*roots, *extra]))


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
            or file_sha256(dst_file) != file_sha256(src_file)
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


def _resolve_prefix_drive_c_layout(
    prefix_dir: Path,
    trusted_targets: set[Path] | None = None,
) -> tuple[Path, Path, tuple[str, ...]] | None:
    """Resolve (real_prefix_root, root_resolved, pfx_rel_parts) handling compatdata symlinks and pfx -> . links."""
    target_prefix = prefix_dir
    if target_prefix.is_symlink():
        try:
            real = target_prefix.resolve()
        except OSError:
            return None
        if not real.is_dir():
            return None
        # Only follow an outer prefix symlink when it points to a known prefix or holds Wine prefix dirs.
        is_trusted = (
            (trusted_targets is not None and real in trusted_targets)
            or (real / "drive_c").is_dir()
            or ((real / "pfx").is_dir() and not (real / "pfx").is_symlink() and (real / "pfx" / "drive_c").is_dir())
        )
        if not is_trusted:
            return None
        target_prefix = real
    elif not target_prefix.is_dir():
        return None
    try:
        root_resolved = target_prefix.resolve()
    except OSError:
        return None
    pfx = target_prefix / "pfx"
    if pfx.is_symlink():
        try:
            # DeckInstall links pfx -> . so Steam Proton sees drive_c directly inside Prefixes/<uid>.
            if pfx.resolve() == root_resolved:
                return target_prefix, root_resolved, ("drive_c",)
        except OSError:
            pass
        return None
    if (pfx / "drive_c").is_dir() or pfx.is_dir():
        return target_prefix, root_resolved, ("pfx", "drive_c")
    return target_prefix, root_resolved, ("drive_c",)


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
    compat_dir = paths.compatdata_dir / str(shortcut_appid_u32)
    layout = _resolve_prefix_drive_c_layout(compat_dir)
    if layout is not None:
        target_root, target_root_resolved, pfx_rel = layout
    elif compat_dir.exists() or compat_dir.is_symlink():
        raise OSError(f"Unsafe compatdata users path for {shortcut_appid_u32}")
    else:
        target_root = paths.compatdata_dir
        target_root_resolved = paths.compatdata_dir.resolve()
        pfx_rel = (str(shortcut_appid_u32), "pfx", "drive_c")
    if not _safe_parents_within(target_root, target_root_resolved, (*pfx_rel, "users")):
        raise OSError(f"Unsafe compatdata users path for {shortcut_appid_u32}")
    dst_drive_c = target_root.joinpath(*pfx_rel)
    dst_users = dst_drive_c / "users"
    dst_users.mkdir(parents=True, exist_ok=True)
    dst_users_resolved = dst_users.resolve()
    if not dst_users_resolved.is_relative_to(target_root_resolved):
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
            if not _safe_parents_within(target_root, target_root_resolved, (*pfx_rel, "ProgramData")):
                raise OSError(f"Unsafe compatdata ProgramData path for {shortcut_appid_u32}")
            dst_progdata.mkdir(parents=True, exist_ok=True)
            dst_progdata_resolved = dst_progdata.resolve()
            if not dst_progdata_resolved.is_relative_to(target_root_resolved):
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


_dir_size_bytes = dir_size_bytes


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


def _add_manifest_prefix(manifest: Path, out: set[Path]) -> None:
    """Add the prefix recorded in a v1/v2 game manifest to out (best effort)."""
    fields = read_manifest_fields(manifest)
    if fields is None:
        return
    try:
        out.add(Path(fields["prefix_dir"]).resolve())
    except OSError:
        pass


def _installed_steam_appids(steam_root: Path) -> set[int]:
    """Return installed Steam AppIDs from appmanifest_*.acf and libraryfolders.vdf apps entries."""
    appids: set[int] = {0, 228980, 1070560, 1391110, 1493710, 1628350, 1887720}
    for lib_vdf in (
        steam_root / "steamapps" / "libraryfolders.vdf",
        steam_root / "config" / "libraryfolders.vdf",
    ):
        if not lib_vdf.is_file():
            continue
        try:
            data = vdf.loads(lib_vdf.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        if not isinstance(folders, dict):
            continue
        for entry in folders.values():
            if not isinstance(entry, dict):
                continue
            apps = entry.get("apps")
            if not isinstance(apps, dict):
                continue
            for app_k in apps:
                key_str = str(app_k).strip()
                if key_str.isascii() and key_str.isdigit():
                    appids.add(int(key_str))
    steamapps_dirs = [steam_root / "steamapps"]
    steamapps_dirs.extend(lib / "steamapps" for lib in read_library_paths(steam_root))
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


def _all_user_shortcuts(
    paths: SteamPaths,
    *,
    strict: bool = False,
) -> tuple[list[ShortcutEntry], list[ShortcutEntry]]:
    """Return (current_user_entries, deduplicated_all_user_entries) for Steam shortcuts."""
    current_entries = load_shortcuts(paths.shortcuts_vdf, strict=strict)
    all_users = list_steam_users(paths.steam_root)
    all_user_entries: list[ShortcutEntry] = list(current_entries)
    if all_users:
        seen_entry_keys = {
            (e.appid_u32, e.exe, e.start_dir, e.icon, e.launch_options)
            for e in current_entries
        }
        for uid in all_users:
            sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
            for e in load_shortcuts(sv, strict=strict):
                key = (e.appid_u32, e.exe, e.start_dir, e.icon, e.launch_options)
                if key not in seen_entry_keys:
                    all_user_entries.append(e)
                    seen_entry_keys.add(key)
    return current_entries, all_user_entries


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
            seen_prefixes: set[Path] = set()
            if paths is not None:
                for entry in entries:
                    compat_dir = paths.compatdata_dir / str(entry.appid_u32)
                    try:
                        if compat_dir.exists():
                            seen_prefixes.add(compat_dir.resolve())
                    except OSError:
                        pass
                    game_files, files_backed, total_uncompressed = _write_prefix_saves_to_zip(
                        zf, compat_dir, str(entry.appid_u32), files_backed, total_uncompressed
                    )
                    if game_files > 0:
                        manifest[str(entry.appid_u32)] = entry.app_name
                        games_backed += 1
            for game in standalone_games:
                try:
                    resolved_pfx = game.prefix_dir.resolve()
                except OSError:
                    resolved_pfx = game.prefix_dir
                if resolved_pfx in seen_prefixes:
                    continue
                seen_prefixes.add(resolved_pfx)
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


def restore_saves_backup(paths: SteamPaths | None, zip_path: Path) -> tuple[int, int]:
    """Restore saves from a DeckInstall ZIP backup into matching Steam or Standalone prefixes."""
    from .standalone import list_standalone_games

    lock_ctx = (
        _steam_transaction_lock(paths.shortcuts_vdf)
        if paths is not None
        else _file_lock(Path.home() / ".local" / "share" / "DeckInstall" / "restore.lock")
    )
    with lock_ctx:
        entries = load_shortcuts(paths.shortcuts_vdf) if paths is not None else []
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

        compat_root_resolved: Path | None = None
        if paths is not None:
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
                    if paths is None or compat_root_resolved is None:
                        continue
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
                    compat_candidate = paths.compatdata_dir / appid_str
                    trusted_pfx = {p.resolve() for p in standalone_by_key.values() if p.exists()}
                    layout = _resolve_prefix_drive_c_layout(compat_candidate, trusted_pfx)
                    if layout is not None:
                        target_prefix, target_root_resolved, pfx_rel = layout
                        target_root = target_prefix
                        prefix_rel = (*pfx_rel, section, *rel_parts[:-1])
                    elif compat_candidate.exists() or compat_candidate.is_symlink():
                        continue
                    else:
                        target_root = paths.compatdata_dir
                        target_root_resolved = compat_root_resolved
                        target_prefix = compat_candidate
                        pfx_rel = ("pfx", "drive_c")
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
                    layout = _resolve_prefix_drive_c_layout(sprefix)
                    if layout is None:
                        continue
                    target_prefix, target_root_resolved, pfx_rel = layout
                    target_root = target_prefix
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
    from .standalone import get_legacy_prefixes_dir

    root = links_root or get_legacy_prefixes_dir()
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


def get_compat_tool_map(config_vdf: Path) -> dict[int, str]:
    """Return a mapping of appid_u32 -> configured compatibility tool name from config.vdf."""
    if not config_vdf.is_file():
        return {}
    try:
        data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
        mapping = (
            data.get("InstallConfigStore", {})
            .get("Software", {})
            .get("Valve", {})
            .get("Steam", {})
            .get("CompatToolMapping", {})
        )
    except Exception:
        return {}
    if not isinstance(mapping, dict):
        return {}
    out: dict[int, str] = {}
    for k, val in mapping.items():
        if str(k).isascii() and str(k).isdigit() and isinstance(val, dict):
            name = str(val.get("name") or "").strip()
            if name:
                out[int(k)] = name
    return out


def get_shortcut_proton(config_vdf: Path, appid_u32: int) -> str:
    """Return configured compatibility tool name for appid_u32, or ''."""
    return get_compat_tool_map(config_vdf).get(appid_u32, "")


def set_shortcut_proton(config_vdf: Path, appid_u32: int, tool_name: str) -> None:
    """Set compatibility tool for a Steam shortcut in config.vdf atomically."""
    cleaned = tool_name.strip()
    if not cleaned:
        remove_compat_tool(config_vdf, appid_u32)
        return
    set_compat_tool(config_vdf, appid_u32, cleaned, create_if_missing=True)


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


def _is_managed_mangohud_config(value: str) -> bool:
    cleaned = value.strip("\"'").strip()
    if "preset=" in cleaned or (
        "legacy_layout=0" in cleaned and "horizontal" in cleaned and "hud_compact" in cleaned
    ):
        return True
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    managed = ("position=", "fps_limit=", "cpu_text=", "gpu_text=", "gpu_list=")
    return bool(parts) and all(p.startswith(managed) for p in parts)


def _build_mangohud_config_parts(
    mangohud_level: int | None,
    mangohud_position: str,
    mangohud_fps_limit: int | None,
) -> list[str]:
    """Build managed MANGOHUD_CONFIG key-value parts from level, position, and FPS limit."""
    pos = mangohud_position.strip()
    if mangohud_level == 2:
        # One line at the top, like MangoHud's preset 2 (the Steam Deck "horizontal" level) plus temperatures:
        # FPS + frame time graph, GPU and CPU load/temp/watts, VRAM/RAM and battery (hidden without one).
        parts = [
            "legacy_layout=0",
            "horizontal",
            "hud_compact",
            "table_columns=20",
            f"position={pos or 'top-center'}",
            "fps",
            "frametime",
            "frame_timing",
            "gpu_stats",
            "gpu_temp",
            "gpu_power",
            "cpu_stats",
            "cpu_temp",
            "cpu_power",
            "vram",
            "ram",
            "battery",
            "battery_watt",
            "battery_time",
        ]
    elif mangohud_level in (1, 3, 4):
        parts = [f"preset={mangohud_level}"]
        if pos:
            parts.append(f"position={pos}")
    else:
        parts = [f"position={pos}"] if pos else []

    if mangohud_level in (2, 3, 4):
        # Real hardware names instead of the generic "CPU" / "GPU" labels.
        from .hwnames import cpu_short_name, mangohud_gpu_text

        cpu, gpu = cpu_short_name(), mangohud_gpu_text()
        if cpu:
            parts.append(f"cpu_text={cpu}")
        if gpu:
            parts.append(f"gpu_text={gpu}")  # one name per GPU: "RTX-3050+Radeon-Vega" on hybrid laptops

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
) -> str:
    """Toggle common SteamOS/Proton launch presets while preserving existing custom options."""
    _validate_launch_options(launch_options)
    if winedlloverrides is not None:
        _validate_launch_options(winedlloverrides)
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
        steam_dev: int | None
        try:
            steam_dev = steam_root.stat().st_dev
        except OSError:
            steam_dev = None
        for lib_path in read_library_paths(steam_root):
            try:
                lib_dev = lib_path.stat().st_dev
            except OSError:
                continue
            if steam_dev is None:
                continue
            if (
                lib_path.is_dir()
                and not lib_path.is_symlink()
                and lib_dev != steam_dev
                and _is_path_on_mounted_volume(lib_path)
            ):
                volume_roots.append(lib_path)

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
                    entries[rel] = ("file", file_sha256(path))
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


# Compatibility re-export: cleanup code now lives in cleanup.py.
from .cleanup import CleanupCandidate as CleanupCandidate  # noqa: E402
