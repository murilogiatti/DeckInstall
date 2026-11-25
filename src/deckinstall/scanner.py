"""Game folder scanner, executable ranker, and Steam AppID detector."""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .gameinfo import is_unreal_binary_name
from .library import LIBRARY_DIRNAME, _unescape_mount

IGNORED_EXE_PATTERNS = (
    "unins",
    "crashhandler",
    "crashreport",
    "unitycrashhandler",
    "crs-uploader",
    "crs-handler",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "dxwebsetup",
    "ueprereq",
    "ue4prereq",
    "dotnet",
    "physx",
    "directx",
    "setup",
    "redist",
    "easyanticheat",
    "battleye",
    "quicksfv",
    "notificationhelper",
    "bugreport",
)

UTILITY_EXE_PATTERNS = (
    "launcher",
    "updater",
    "config",
    "settings",
    "benchmark",
    "server",
    "dedicated",
    "editor",
    "crashpad",
    "cefprocess",
    "webview",
    "helper",
    "bootstrapper",
)

SCENE_TAG_RE = re.compile(
    r"(?:^|[\.\-_ ])(InsaneRamZes|RUNE|CODEX|EMPRESS|FLT|TENOKE|ElAmigos|FitGirl|DODI|"
    r"GOG|Portable|Complete[.\-_ ]Edition|Ultimate[.\-_ ]Edition|Deluxe[.\-_ ]Edition)"
    r"(?=$|[\.\-_ ]).*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DetectedGame:
    """A discovered game folder with ranked candidate executables."""

    name: str
    root_dir: Path
    executables: list[Path]
    primary_exe: Path
    steam_appid: int | None


def clean_game_name(raw_name: str) -> str:
    """Normalize a folder or archive name into a clean game title."""
    cleaned = SCENE_TAG_RE.sub("", raw_name)
    cleaned = cleaned.replace(".", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return cleaned or raw_name


def _parse_valid_appid(raw: str) -> int | None:
    s = raw.strip()
    if not s or len(s) > 10 or not (s.isascii() and s.isdigit()):
        return None
    try:
        val = int(s)
    except ValueError:
        return None
    return val if 1 <= val <= 0xFFFFFFFF else None


def detect_steam_appid(game_dir: Path) -> int | None:
    """Check common Steam emulator/appid files for an official numeric Steam AppID."""
    def _read_appid_txt(path: Path) -> int | None:
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore").strip()
        except OSError:
            return None
        return _parse_valid_appid(text)

    root_parsed = _read_appid_txt(game_dir / "steam_appid.txt")
    if root_parsed is not None:
        return root_parsed

    appid_files: list[Path] = []
    ini_files: dict[str, list[Path]] = {
        "steam_emu.ini": [],
        "rune.ini": [],
        "tenoke.ini": [],
        "flt.ini": [],
    }
    base_parts = len(game_dir.parts)
    for dirpath, dirnames, filenames in os.walk(game_dir, followlinks=False):
        cur = Path(dirpath)
        depth = len(cur.parts) - base_parts
        if depth >= 8:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fname in filenames:
            low = fname.lower()
            if low == "steam_appid.txt":
                appid_files.append(cur / fname)
            elif low in ini_files:
                ini_files[low].append(cur / fname)

    for appid_file in appid_files:
        parsed = _read_appid_txt(appid_file)
        if parsed is not None:
            return parsed

    for ini_name in ("steam_emu.ini", "rune.ini", "tenoke.ini", "flt.ini"):
        for match in ini_files[ini_name]:
            try:
                content = match.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            pattern = (
                r"^\s*(?:AppId|id)\s*=\s*(\d+)"
                if ini_name == "tenoke.ini"
                else r"^\s*AppId\s*=\s*(\d+)"
            )
            m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
            if m:
                parsed = _parse_valid_appid(m.group(1))
                if parsed is not None:
                    return parsed
    return None


def detect_exe_kind(exe_path: Path) -> str:
    """Inspect executable magic bytes and return windows_pe, linux_elf, or unknown."""
    try:
        with open(exe_path, "rb") as handle:
            header = handle.read(64)
            if header.startswith(b"\x7fELF"):
                return "linux_elf"
            if header.startswith(b"MZ") and len(header) == 64:
                e_lfanew = int.from_bytes(header[60:64], "little")
                if e_lfanew >= 4:
                    handle.seek(e_lfanew)
                    if handle.read(4) == b"PE\x00\x00":
                        return "windows_pe"
    except OSError:
        return "unknown"
    return "unknown"


def is_native_linux_exe(path: Path) -> bool:
    """True for Linux ELF binaries and shebang scripts (e.g. GOG's start.sh)."""
    if detect_exe_kind(path) == "linux_elf":
        return True
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def gog_product_id(game_dir: Path) -> str:
    """Return the GOG product id from goggame-<id>.info (used as STORE=gog GAMEID lookup key)."""
    for pattern in ("goggame-*.info", "*/goggame-*.info"):
        for info in sorted(game_dir.glob(pattern)):
            if not info.is_file() or info.is_symlink():
                continue
            m = re.fullmatch(r"goggame-(\d+)\.info", info.name)
            if m:
                return m.group(1)
    return ""


# DRM-free emulators/cracks that need DLL overrides under Wine/Proton.
_ONLINEFIX_OVERRIDES = "OnlineFix64=n;SteamOverlay64=n;winmm=n,b;dnet=n;steam_api64=n;winhttp=n,b"
_STEAM_API_OVERRIDES = "steam_api64=n;steam_api=n"
_STEAMCLIENT_OVERRIDES = "steamclient64=n;steamclient=n"


def _lower_names(folder: Path) -> set[str]:
    try:
        return {p.name.lower() for p in folder.iterdir()}
    except OSError:
        return set()


def detect_steam_emulator(exe_path: Path, game_dir: Path) -> tuple[str, str]:
    """Return (kind, WINEDLLOVERRIDES) for OnlineFix, Goldberg or CODEX found next to the game exe.

    kind is "onlinefix", "goldberg", "codex" or "". Goldberg's experimental steamclient(64).dll
    must be loaded as native, otherwise Proton's built-in lsteamclient replaces it.
    """
    folders = [exe_path.parent]
    if game_dir != exe_path.parent:
        folders.append(game_dir)
    names: set[str] = set()
    for folder in folders:
        names |= _lower_names(folder)
    if "onlinefix.ini" in names or "onlinefix64.dll" in names or "onlinefix.dll" in names:
        return "onlinefix", _ONLINEFIX_OVERRIDES
    has_api = "steam_api64.dll" in names or "steam_api.dll" in names
    goldberg_markers = {"steam_settings", "coldclientloader.ini", "steam_interfaces.txt", "local_save.txt"}
    if names & goldberg_markers and (has_api or "steamclient64.dll" in names):
        overrides = _STEAM_API_OVERRIDES
        if "steamclient64.dll" in names or "steamclient.dll" in names:
            overrides = f"{overrides};{_STEAMCLIENT_OVERRIDES}"
        return "goldberg", overrides
    if has_api and names & {"steam_emu.ini", "codex64.dll", "codex.ini"}:
        return "codex", _STEAM_API_OVERRIDES
    return "", ""


def detect_pe_arch(exe_path: Path) -> str:
    """Inspect executable header and return 'x64', 'x86', 'linux_elf', or 'unknown'."""
    try:
        with open(exe_path, "rb") as handle:
            header = handle.read(64)
            if header.startswith(b"\x7fELF"):
                return "linux_elf"
            if not header.startswith(b"MZ") or len(header) < 64:
                return "unknown"
            e_lfanew = int.from_bytes(header[60:64], "little")
            if e_lfanew < 4:
                return "unknown"
            handle.seek(e_lfanew)
            pe_hdr = handle.read(6)
            if len(pe_hdr) < 6 or pe_hdr[:4] != b"PE\x00\x00":
                return "unknown"
            machine = int.from_bytes(pe_hdr[4:6], "little")
            if machine == 0x8664:
                return "x64"
            if machine == 0xAA64:
                return "arm64"
            if machine == 0x014C:
                return "x86"
    except OSError:
        return "unknown"
    return "unknown"


def is_proton_exe(path: Path) -> bool:
    """True for a Windows PE x86/x64 executable (what Proton runs); DOS, ARM64 or unreadable -> False."""
    return detect_pe_arch(path) in ("x86", "x64")


def _extract_steam_appid(
    appid_files: list[Path],
    ini_files: dict[str, list[Path]],
) -> int | None:
    """Parse Steam AppID from collected steam_appid.txt or emulator .ini files."""
    for appid_file in appid_files:
        try:
            text = appid_file.read_text(encoding="utf-8-sig", errors="ignore").strip()
        except OSError:
            continue
        parsed = _parse_valid_appid(text)
        if parsed is not None:
            return parsed

    for ini_name in ("steam_emu.ini", "rune.ini", "tenoke.ini", "flt.ini"):
        pattern = (
            r"^\s*(?:AppId|id)\s*=\s*(\d+)"
            if ini_name == "tenoke.ini"
            else r"^\s*AppId\s*=\s*(\d+)"
        )
        for match in ini_files[ini_name]:
            try:
                content = match.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
            if m:
                parsed = _parse_valid_appid(m.group(1))
                if parsed is not None:
                    return parsed
    return None


def _scan_game_tree_scandir(
    game_dir: Path,
    exe_max_depth: int = 4,
    appid_max_depth: int = 8,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[Path], int | None]:
    """Traverse game_dir once via os.scandir(), returning (ranked_exes, steam_appid).

    should_stop: polled between folders; when it says True the walk gives up and finds nothing.
    """
    exe_scored: list[tuple[tuple[int, int, int, int, int], Path]] = []
    native_scored: list[tuple[tuple[int, int, int], Path]] = []
    appid_files: list[Path] = []
    ini_files: dict[str, list[Path]] = {
        "steam_emu.ini": [],
        "rune.ini": [],
        "tenoke.ini": [],
        "flt.ini": [],
    }
    max_dir_depth = max(exe_max_depth - 1, appid_max_depth)
    queue: deque[tuple[Path, int]] = deque([(game_dir, 0)])

    while queue:
        if should_stop is not None and should_stop():
            return [], None
        cur_dir, dir_depth = queue.popleft()
        subdirs: list[Path] = []
        collect_exes = dir_depth < exe_max_depth
        collect_appid = dir_depth <= appid_max_depth
        descend = dir_depth < max_dir_depth
        rel_depth = dir_depth + 1

        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if descend and not name.startswith("."):
                                subdirs.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue

                    low = name.lower()
                    if collect_exes and low.endswith(".exe"):
                        if not any(pat in low for pat in IGNORED_EXE_PATTERNS) and is_proton_exe(Path(entry.path)):
                            try:
                                size_neg = -entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                size_neg = 0
                            is_utility = 1 if any(pat in low for pat in UTILITY_EXE_PATTERNS) else 0
                            is_shipping = 0 if is_unreal_binary_name(low) else 1
                            is_root = 0 if rel_depth == 1 else 1
                            score = (is_utility, is_shipping, is_root, rel_depth, size_neg)
                            exe_scored.append((score, Path(entry.path)))
                    elif collect_appid:
                        if low == "steam_appid.txt":
                            appid_files.append(Path(entry.path))
                        elif low in ini_files:
                            ini_files[low].append(Path(entry.path))
                    if collect_exes and not exe_scored and _is_native_game_entry(entry):
                        native_score = (0 if low == "start.sh" else 1, rel_depth, -entry.stat().st_size)
                        native_scored.append((native_score, Path(entry.path)))
        except OSError:
            continue

        if subdirs:
            subdirs.sort(key=lambda p: p.name)
            for sub in subdirs:
                queue.append((sub, dir_depth + 1))

    ranked_exes = (
        [path for _, path in sorted(exe_scored)] if exe_scored else [path for _, path in sorted(native_scored)]
    )
    steam_appid = _extract_steam_appid(appid_files, ini_files)
    return ranked_exes, steam_appid


def scan_game_folder(game_dir: Path, should_stop: Callable[[], bool] | None = None) -> DetectedGame | None:
    """Inspect a single game directory and return a DetectedGame if executables exist."""
    if not game_dir.is_dir():
        return None
    exes, steam_appid = _scan_game_tree_scandir(game_dir, should_stop=should_stop)
    if not exes:
        return None
    return DetectedGame(
        name=clean_game_name(game_dir.name),
        root_dir=game_dir,
        executables=exes,
        primary_exe=exes[0],
        steam_appid=steam_appid,
    )


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


# The home walk goes down until it meets each game folder, within these safety limits.
HOME_SCAN_MAX_DEPTH = 8
HOME_SCAN_MAX_DIRS = 20000
# Never walked when searching the home folder: heavy, irrelevant or full of Windows exes that are not games.
HOME_SCAN_SKIP = frozenset(
    {
        "compatdata", "prefixes", "pfx", "drive_c", "node_modules", "steamapps", "shadercache",
        "deckinstall", "lost+found", "system volume information", "__pycache__", "snap",
        # Repack / installer side folders (QuickSFV.exe, redistributables) are never the game.
        "md5", "_redist", "redist", "_commonredist", "commonredist", "directx", "vcredist", "__installer",
    }
)
# Folders that hold the exe but are not the game itself (Game/bin/Game.exe -> Game).
_BIN_DIR_NAMES = frozenset(
    {"bin", "bin32", "bin64", "x64", "x86", "win64", "win32", "binaries", "system", "windows", "game"}
)
# Folders right under home that hold many things (a loose setup.exe there is not a game): always walked into.
_HOME_CONTAINERS = frozenset(
    {"games", "jogos", "downloads", "desktop", "documents", "syncthing", "music", "pictures", "videos",
     "public", "templates", "área de trabalho", "documentos", "transferências"}
)


def _is_native_game_entry(entry: os.DirEntry[str]) -> bool:
    name = entry.name.lower()
    if (
        name.startswith(".") or name.endswith((".so", ".exe")) or ".so." in name
        or any(pattern in name for pattern in IGNORED_EXE_PATTERNS)
    ):
        return False
    path = Path(entry.path)
    try:
        if not entry.stat(follow_symlinks=False).st_mode & 0o111:
            return False
        if name == "start.sh":
            return is_native_linux_exe(path)
        return detect_exe_kind(path) == "linux_elf"
    except OSError:
        return False


def _dir_has_game_exe(path: Path) -> bool:
    """True if the folder directly holds a game executable."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                low = entry.name.lower()
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if low.endswith(".exe") and not any(pat in low for pat in IGNORED_EXE_PATTERNS):
                        if is_proton_exe(Path(entry.path)):
                            return True
                    elif _is_native_game_entry(entry):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def find_home_game_dirs(
    home: Path,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
    max_dirs: int = HOME_SCAN_MAX_DIRS,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    """Game folders under home: walks down until the first folder that holds an exe.

    Hidden folders and HOME_SCAN_SKIP are never entered, symlinks are not followed, and a folder
    that holds an exe is not walked further (the per-game scan looks inside it). The walk stops
    after max_dirs folders and never goes deeper than max_depth levels.
    """
    found: list[Path] = []
    queue: deque[tuple[Path, int]] = deque([(home, 0)])
    visited = 0
    while queue and visited < max_dirs:
        if should_stop is not None and should_stop():
            return []
        cur, depth = queue.popleft()
        visited += 1
        try:
            with os.scandir(cur) as it:
                subdirs = sorted(
                    (
                        Path(e.path)
                        for e in it
                        if not e.name.startswith(".")
                        and e.name.lower() not in HOME_SCAN_SKIP
                        and not e.is_symlink()
                        and e.is_dir(follow_symlinks=False)
                    ),
                    key=lambda p: p.name,
                )
        except OSError:
            continue
        for sub in subdirs:
            container = depth == 0 and sub.name.lower() in _HOME_CONTAINERS
            if not container and _dir_has_game_exe(sub):
                game_dir = sub
                while (
                    game_dir.name.lower() in _BIN_DIR_NAMES
                    and game_dir.parent != home
                    and not (game_dir.parent.parent == home and game_dir.parent.name.lower() in _HOME_CONTAINERS)
                ):
                    game_dir = game_dir.parent
                if game_dir not in found:
                    found.append(game_dir)
            elif depth + 1 < max_depth:
                queue.append((sub, depth + 1))
    return found


# Filesystems that live on a real disk / card (pseudo, network and overlay mounts are ignored).
_DISK_FSTYPES = frozenset({"ext4", "btrfs", "xfs", "f2fs", "exfat", "vfat", "ntfs", "ntfs3", "fuseblk"})
# System mount points (the system disk itself, not a games disk).
_SYSTEM_MOUNTS = frozenset(
    {"/", "/home", "/boot", "/efi", "/var", "/tmp", "/usr", "/opt", "/srv", "/nix", "/snap", "/root",
     "/etc", "/sysroot", "/esp"}
)
# Nothing under these is a games disk (except /run/media and /var/mnt).
_SYSTEM_TREES = ("/proc", "/sys", "/dev", "/run", "/boot", "/efi", "/usr", "/nix", "/snap", "/var", "/sysroot", "/esp")
_DISK_TREE_EXCEPTIONS = ("/run/media", "/var/mnt")
_SKIP_DISK_CHILDREN = frozenset({"steamapps", "prefixes", "caches", "lost+found", "system volume information"})


def _read_mountinfo() -> str:
    """Raw /proc/self/mountinfo (a seam: tests replace it)."""
    try:
        return Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mount_usable(mount: str) -> bool:
    """The mount point is a folder this user can list and enter (not e.g. root's 0750 folder)."""
    return os.path.isdir(mount) and os.access(mount, os.R_OK | os.X_OK)


def _under(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def mounted_disks(mountinfo: str | None = None) -> list[Path]:
    """Mount points of real disks and cards (SD, USB, extra disks), read from mountinfo."""
    text = _read_mountinfo() if mountinfo is None else mountinfo
    home = str(Path.home())
    disks: list[Path] = []
    seen_src: set[tuple[str, str]] = set()
    seen_real: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        try:
            sep = fields.index("-", 6)
        except ValueError:
            continue
        if len(fields) < sep + 2:
            continue
        fstype = fields[sep + 1].lower()
        if fstype not in _DISK_FSTYPES:
            continue
        mount = os.path.normpath(_unescape_mount(fields[4]))
        if mount in _SYSTEM_MOUNTS or _under(mount, home) or _under(home, mount):
            continue
        if any(_under(mount, t) for t in _SYSTEM_TREES) and not any(
            _under(mount, e) for e in _DISK_TREE_EXCEPTIONS
        ):
            continue
        # The same btrfs subvolume (or bind mount) mounted twice is one disk.
        src = (fields[2], _unescape_mount(fields[3]))
        try:
            real = os.path.realpath(mount)
        except OSError:
            real = mount
        if src in seen_src or real in seen_real:
            continue
        if not _mount_usable(mount):
            continue
        seen_src.add(src)
        seen_real.add(real)
        disks.append(Path(mount))
    disks.sort(key=str)
    return disks


def _visible_subdirs(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            entries = sorted((e for e in it if not e.name.startswith(".")), key=lambda e: e.name)
    except OSError:
        return []
    out: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=True):
                continue
        except OSError:
            continue
        if entry.name.lower() not in _SKIP_DISK_CHILDREN:
            out.append(Path(entry.path))
    return out


def default_scan_roots() -> list[Path]:
    """Mounted cards and disks (each child folder is a game), plus their Games and first-level folders.

    Only disks really mounted (mountinfo) and readable are used. Home is walked separately.
    """
    roots: list[Path] = []
    for disk in mounted_disks():
        roots.append(disk)
        games_sub = disk / "Games"
        if _safe_is_dir(games_sub):
            roots.append(games_sub)
        for sub in _visible_subdirs(disk):
            if _dir_has_game_exe(sub):
                continue
            sub_games = sub / "Games"
            roots.append(sub_games if _safe_is_dir(sub_games) else sub)
    deduped: list[Path] = []
    seen_roots: set[Path] = set()
    for r in roots:
        if not _safe_is_dir(r):
            continue
        try:
            res = r.resolve()
        except OSError:
            res = r
        if res not in seen_roots:
            seen_roots.add(res)
            deduped.append(r)
    return deduped


def scan_all_games(
    roots: list[Path] | None = None, should_stop: Callable[[], bool] | None = None
) -> list[DetectedGame]:
    """Scan roots for installed/extracted games.

    should_stop: polled while walking; when it says True the scan ends early with no games
    (closing the window must not wait for a full disk walk).

    The home folder is walked down to each game folder (find_home_game_dirs); any other root is a
    container whose top-level folders are games. None = home + cards/disks (default_scan_roots).
    """
    seen: set[Path] = set()
    candidates: list[Path] = []
    home = Path.home()
    try:
        home_res = home.resolve()
    except OSError:
        home_res = home
    scan_roots = roots if roots is not None else [home, *default_scan_roots()]
    containers = {Path(os.path.realpath(root)) for root in scan_roots}
    for root in scan_roots:
        try:
            is_home = root.resolve() == home_res
        except OSError:
            is_home = False
        if is_home:
            for child in find_home_game_dirs(root, should_stop=should_stop):
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(child)
            continue
        try:
            with os.scandir(root) as it:
                entries = sorted(
                    (
                        entry
                        for entry in it
                        if not entry.name.startswith(".")
                        and entry.name.lower() not in _SKIP_DISK_CHILDREN
                        and entry.name.lower() != LIBRARY_DIRNAME.lower()
                    ),
                    key=lambda e: e.name,
                )
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
            except OSError:
                continue
            child = Path(entry.path)
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in seen or (resolved in containers and child.name.lower() in _HOME_CONTAINERS):
                continue
            seen.add(resolved)
            candidates.append(child)

    if not candidates or (should_stop is not None and should_stop()):
        return []
    if len(candidates) == 1:
        try:
            detected = scan_game_folder(candidates[0], should_stop)
        except Exception:
            return []
        return [detected] if detected is not None else []

    results: list[DetectedGame] = []
    max_workers = min(8, os.cpu_count() or 4)
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(scan_game_folder, child, should_stop) for child in candidates]
            for fut in futures:
                if should_stop is not None and should_stop():
                    for pending in futures:
                        pending.cancel()
                    return []
                try:
                    detected = fut.result()
                except Exception:
                    continue
                if detected is not None:
                    results.append(detected)
    except RuntimeError:
        for child in candidates:
            if should_stop is not None and should_stop():
                return []
            try:
                detected = scan_game_folder(child, should_stop)
            except Exception:
                continue
            if detected is not None:
                results.append(detected)
    by_root = {Path(os.path.realpath(game.root_dir)): set(game.executables) for game in results}
    return [
        game for game in results
        if not any(
            game.primary_exe in by_root.get(parent, ())
            for parent in Path(os.path.realpath(game.root_dir)).parents
        )
    ]
