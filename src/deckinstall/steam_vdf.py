"""Safe reader/writer for Steam shortcuts.vdf and config.vdf."""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import vdf

from .scanner import clean_game_name, detect_steam_appid
from .steam_paths import SteamPaths, find_default_proton_name, is_steam_running, list_steam_users

_STEAM_LOCK = threading.RLock()


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
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text to a temp file in the same directory and atomically replace target."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _backup_file(path: Path, keep: int = 5) -> Path | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.deckinstall.bak")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.{stamp}.{counter}.deckinstall.bak")
        counter += 1
    shutil.copy2(path, backup)
    old_backups = sorted(
        path.parent.glob(f"{path.name}.*.deckinstall.bak"),
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


def shutdown_steam(timeout_seconds: int = 15) -> bool:
    """Gracefully shut down Steam and wait for exit."""
    if not is_steam_running():
        return True
    subprocess.run(
        ["/usr/bin/steam", "-shutdown"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_steam_running():
            return True
        time.sleep(0.5)
    subprocess.run(
        ["pkill", "-TERM", "-x", "steam"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    time.sleep(2.0)
    return not is_steam_running()


def start_steam() -> None:
    """Start Steam in the background using the current desktop environment."""
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    subprocess.Popen(
        ["/usr/bin/steam", "-silent"],
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
    launch_options: str = "",
    enable_proton: bool = True,
) -> int:
    """Add or update a Non-Steam shortcut and optionally set default Proton.

    Returns the unsigned 32-bit shortcut AppID.
    """
    exe_quoted = _quote_path(str(exe_path))
    dir_quoted = _quote_path(str(start_dir or exe_path.parent))
    appid_u32, appid_s32 = compute_shortcut_appid(exe_quoted, app_name)

    paths.shortcuts_vdf.parent.mkdir(parents=True, exist_ok=True)
    _backup_file(paths.shortcuts_vdf)

    if paths.shortcuts_vdf.is_file() and paths.shortcuts_vdf.stat().st_size > 0:
        data = vdf.binary_loads(paths.shortcuts_vdf.read_bytes())
    else:
        data = {"shortcuts": {}}

    shortcuts = data.setdefault("shortcuts", {})
    filtered = [
        item
        for item in shortcuts.values()
        if isinstance(item, dict)
        and str(item.get("AppName") or item.get("appname") or "") != app_name
    ]

    new_entry = {
        "appid": appid_s32,
        "AppName": app_name,
        "Exe": exe_quoted,
        "StartDir": dir_quoted,
        "icon": str(icon_path) if icon_path else "",
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "sortas": "",
        "tags": {},
    }
    filtered.append(new_entry)
    data["shortcuts"] = {str(i): entry for i, entry in enumerate(filtered)}
    _atomic_write_bytes(paths.shortcuts_vdf, vdf.binary_dumps(data))

    if enable_proton:
        set_compat_tool(
            paths.config_vdf,
            appid_u32,
            find_default_proton_name(paths.steam_root),
        )

    return appid_u32


def remove_shortcut(paths: SteamPaths, appid_u32: int) -> bool:
    """Remove a Non-Steam shortcut by its unsigned 32-bit AppID."""
    if not paths.shortcuts_vdf.is_file():
        return False
    _backup_file(paths.shortcuts_vdf)
    data = vdf.binary_loads(paths.shortcuts_vdf.read_bytes())
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
    remove_compat_tool(paths.config_vdf, appid_u32)
    return True


def set_compat_tool(config_vdf: Path, appid_u32: int, tool_name: str) -> None:
    """Ensure CompatToolMapping for appid_u32 is set in Steam's config.vdf."""
    if not config_vdf.is_file():
        return
    _backup_file(config_vdf)
    data = vdf.loads(config_vdf.read_text(encoding="utf-8", errors="replace"))
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


def _rewrite_launch_options(launch_options: str, old_root: Path, new_root: Path) -> str:
    """Update references to old_root inside LaunchOptions when moving a game folder."""
    if not launch_options or old_root.resolve() == new_root.resolve():
        return launch_options
    old_str = str(old_root)
    old_res = str(old_root.resolve())
    new_str = str(new_root)
    updated = launch_options.replace(old_res, new_str)
    if old_str != old_res:
        updated = updated.replace(old_str, new_str)
    return updated


def _has_proton_mapping(config_vdf: Path, appid_u32: int) -> bool:
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
        return str(appid_u32) in mapping
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
    moved_root = False
    if old_root.resolve() != new_root.resolve():
        if new_root.exists():
            raise FileExistsError(f"A pasta de destino já existe: {new_root}")
        shutil.move(str(old_root), str(new_root))
        moved_root = True

    new_exe = new_root / rel_exe
    new_launch = _rewrite_launch_options(entry.launch_options, old_root, new_root)
    try:
        with _STEAM_LOCK:
            new_appid = add_or_update_shortcut(
                paths=paths,
                app_name=target_name,
                exe_path=new_exe,
                start_dir=new_exe.parent,
                icon_path=Path(entry.icon) if entry.icon else None,
                launch_options=new_launch,
                enable_proton=True,
            )

            if new_appid != entry.appid_u32:
                old_compat = paths.compatdata_dir / str(entry.appid_u32)
                new_compat = paths.compatdata_dir / str(new_appid)
                if old_compat.is_dir():
                    if new_compat.exists():
                        stamp = time.strftime("%Y%m%d-%H%M%S")
                        backup_compat = paths.compatdata_dir / f"{new_appid}.{stamp}.deckinstall.bak"
                        counter = 1
                        while backup_compat.exists():
                            backup_compat = paths.compatdata_dir / f"{new_appid}.{stamp}.{counter}.deckinstall.bak"
                            counter += 1
                        shutil.move(str(new_compat), str(backup_compat))
                    shutil.move(str(old_compat), str(new_compat))

                if paths.grid_dir.is_dir():
                    old_prefix = str(entry.appid_u32)
                    for art_file in paths.grid_dir.iterdir():
                        if art_file.is_file() and art_file.name.startswith(old_prefix):
                            suffix = art_file.name[len(old_prefix) :]
                            dest_art = paths.grid_dir / f"{new_appid}{suffix}"
                            if not dest_art.exists():
                                shutil.move(str(art_file), str(dest_art))

                remove_shortcut(paths, entry.appid_u32)
    except Exception:
        if moved_root and new_root.exists() and not old_root.exists():
            try:
                shutil.move(str(new_root), str(old_root))
            except OSError:
                logging.exception("Rollback failed moving %s back to %s", new_root, old_root)
        raise

    steam_appid = detect_steam_appid(new_root)
    import_external_prefix_saves(
        paths=paths,
        shortcut_appid_u32=new_appid,
        game_name=target_name,
        steam_appid=steam_appid,
    )
    return new_appid, new_exe, steam_appid


_PREFIX_SKIP_DIRS = {"microsoft", "temp", "dxvk", "nvidia", "amd", "d3dscache"}
_PREFIX_REL_DIRS = (
    Path("Documents"),
    Path("Saved Games"),
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
    for rel in (Path("pfx") / "drive_c" / "users", Path("drive_c") / "users"):
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
            if not child.is_dir() or child.name.startswith("."):
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


def _iter_prefix_save_pairs(src_users: Path, dst_users: Path):
    """Yield (src_file, dst_file) for user saves across steamuser/deck/Public."""
    try:
        user_dirs = [d for d in src_users.iterdir() if d.is_dir()]
    except OSError:
        return
    for user_dir in user_dirs:
        if user_dir.name.lower() == "public":
            mappings = [(user_dir / "Documents", dst_users / "Public" / "Documents")]
        else:
            mappings = [
                (user_dir / rel, dst_users / "steamuser" / rel)
                for rel in _PREFIX_REL_DIRS
            ]
        for src_root, dst_root in mappings:
            if not src_root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
                dirnames[:] = [d for d in dirnames if d.lower() not in _PREFIX_SKIP_DIRS]
                cur = Path(dirpath)
                rel_dir = cur.relative_to(src_root)
                for fname in filenames:
                    src_file = cur / fname
                    if src_file.is_symlink() or not src_file.is_file():
                        continue
                    yield src_file, dst_root / rel_dir / fname


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
    dst_users = paths.compatdata_dir / str(shortcut_appid_u32) / "pfx" / "drive_c" / "users"
    dst_users.mkdir(parents=True, exist_ok=True)
    dst_users_resolved = dst_users.resolve()
    copied = 0
    errors: list[str] = []
    for prefix_dir in prefixes:
        src_users = _prefix_users_dir(prefix_dir)
        if src_users is None:
            continue
        for src_file, dst_file in _iter_prefix_save_pairs(src_users, dst_users):
            try:
                if dst_file.is_symlink():
                    continue
                if dst_file.exists() and dst_file.stat().st_mtime >= src_file.stat().st_mtime:
                    continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if not dst_file.parent.resolve().is_relative_to(dst_users_resolved):
                    continue
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
                copied += 1
            except OSError as exc:
                logging.warning("Falha ao importar save %s -> %s: %s", src_file, dst_file, exc)
                errors.append(f"{src_file.name}: {exc}")
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
            needs_proton = bool(config_vdf and not _has_proton_mapping(config_vdf, entry.appid_u32))
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


def _prefix_has_saves(prefix_dir: Path) -> bool:
    src_users = _prefix_users_dir(prefix_dir)
    if src_users is None:
        return False
    dummy_dst = prefix_dir / "__unused__"
    for src_file, _ in _iter_prefix_save_pairs(src_users, dummy_dst):
        try:
            if src_file.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _installed_steam_appids(steam_root: Path) -> set[int]:
    """Return installed Steam AppIDs from appmanifest_*.acf files."""
    appids: set[int] = {0, 228980}
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
            if stem_id.isdigit():
                appids.add(int(stem_id))
    return appids


def find_cleanup_candidates(
    paths: SteamPaths,
    extra_external_roots: list[Path] | None = None,
) -> list[CleanupCandidate]:
    """Find old external prefixes, orphan Steam compatdata folders, and DeckInstall backups."""
    candidates: list[CleanupCandidate] = []
    seen: set[Path] = set()

    for root in extra_external_roots or external_prefix_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if _prefix_users_dir(child) is None:
                continue
            resolved = child.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(
                CleanupCandidate(
                    category="Prefixo Externo",
                    label=f"{root.parent.name}/{root.name}/{child.name}",
                    path=child,
                    size_bytes=_dir_size_bytes(child),
                    has_saves=_prefix_has_saves(child),
                )
            )

    active_shortcuts: set[int] = set()
    all_users = list_steam_users(paths.steam_root)
    if not all_users:
        active_shortcuts = {e.appid_u32 for e in load_shortcuts(paths.shortcuts_vdf)}
    else:
        for uid in all_users:
            sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
            active_shortcuts.update(e.appid_u32 for e in load_shortcuts(sv))
    installed_steam = _installed_steam_appids(paths.steam_root)
    if paths.compatdata_dir.is_dir():
        for child in paths.compatdata_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name.endswith(".deckinstall.bak"):
                candidates.append(
                    CleanupCandidate(
                        category="Backup",
                        label=child.name,
                        path=child,
                        size_bytes=_dir_size_bytes(child),
                        has_saves=_prefix_has_saves(child),
                    )
                )
                continue
            if not child.name.isdigit():
                continue
            appid = int(child.name)
            if appid in active_shortcuts or appid in installed_steam:
                continue
            candidates.append(
                CleanupCandidate(
                    category="Compatdata Órfão",
                    label=f"compatdata/{child.name}",
                    path=child,
                    size_bytes=_dir_size_bytes(child),
                    has_saves=_prefix_has_saves(child),
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
