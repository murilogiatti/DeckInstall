"""Safe reader/writer for Steam shortcuts.vdf and config.vdf."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import vdf

from .scanner import clean_game_name, detect_steam_appid
from .steam_paths import SteamPaths, find_default_proton_name, is_steam_running


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
    """Write bytes to a temp file in the same directory and atomically replace target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


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
    if resolved.parent in known_containers:
        raise ValueError(
            f"O executável está solto diretamente em {resolved.parent}; "
            "coloque-o dentro de uma subpasta própria antes de migrar."
        )
    for parent in resolved.parents:
        if parent.parent in known_containers:
            return parent
        if (
            len(parent.parts) >= 5
            and parent.parts[:3] == ("/", "run", "media")
            and parent.parent.name == "Games"
        ):
            return parent
    candidate = resolved.parent
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
    if old_root.resolve() != new_root.resolve():
        if new_root.exists():
            raise FileExistsError(f"A pasta de destino já existe: {new_root}")
        shutil.move(str(old_root), str(new_root))

    new_exe = new_root / rel_exe
    new_appid = add_or_update_shortcut(
        paths=paths,
        app_name=target_name,
        exe_path=new_exe,
        start_dir=new_exe.parent,
        icon_path=Path(entry.icon) if entry.icon else None,
        launch_options=entry.launch_options,
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

    steam_appid = detect_steam_appid(new_root)
    if steam_appid:
        import_hydra_umu_saves(paths, new_appid, steam_appid)
    return new_appid, new_exe, steam_appid


_HYDRA_SKIP_DIRS = {"microsoft", "temp", "dxvk", "nvidia", "amd", "d3dscache"}
_HYDRA_USER_SUBPATHS = (
    Path("steamuser") / "Documents",
    Path("steamuser") / "Saved Games",
    Path("steamuser") / "AppData" / "Local",
    Path("steamuser") / "AppData" / "LocalLow",
    Path("steamuser") / "AppData" / "Roaming",
    Path("Public") / "Documents",
)


def import_hydra_umu_saves(
    paths: SteamPaths,
    shortcut_appid_u32: int,
    steam_appid: int | None,
    hydra_prefixes_root: Path | None = None,
) -> int:
    """Import newer/missing save files from Hydra UMU wine-prefixes into Steam compatdata."""
    if not steam_appid:
        return 0
    base = hydra_prefixes_root or (Path.home() / ".config" / "hydralauncher" / "wine-prefixes")
    src_users = base / str(steam_appid) / "drive_c" / "users"
    if not src_users.is_dir():
        return 0

    dst_users = paths.compatdata_dir / str(shortcut_appid_u32) / "pfx" / "drive_c" / "users"
    copied = 0
    for rel_sub in _HYDRA_USER_SUBPATHS:
        src_root = src_users / rel_sub
        if not src_root.is_dir():
            continue
        dst_root = dst_users / rel_sub
        for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d.lower() not in _HYDRA_SKIP_DIRS]
            cur = Path(dirpath)
            rel_dir = cur.relative_to(src_root)
            for fname in filenames:
                src_file = cur / fname
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                dst_file = dst_root / rel_dir / fname
                try:
                    if dst_file.exists() and dst_file.stat().st_mtime >= src_file.stat().st_mtime:
                        continue
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    copied += 1
                except OSError:
                    continue
    return copied


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
) -> list[MigrationPlanItem]:
    """Identify Windows .exe shortcuts that are not yet standardized in games_root."""
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
