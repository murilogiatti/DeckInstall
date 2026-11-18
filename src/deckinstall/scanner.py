"""Game folder scanner, executable ranker, and Steam AppID detector."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

IGNORED_EXE_PATTERNS = (
    "unins",
    "crashhandler",
    "unitycrashhandler",
    "crs-uploader",
    "crs-handler",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "ueprereq",
    "ue4prereq",
    "dotnet",
    "physx",
    "directx",
    "setup",
    "redist",
    "easyanticheat",
    "battleye",
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
    """Inspect executable magic bytes and return windows_pe, dos_mz, linux_elf, or unknown."""
    try:
        with open(exe_path, "rb") as handle:
            header = handle.read(64)
            if header.startswith(b"\x7fELF"):
                return "linux_elf"
            if header.startswith(b"MZ"):
                if len(header) < 64:
                    return "dos_mz"
                e_lfanew = int.from_bytes(header[60:64], "little")
                if e_lfanew < 4:
                    return "dos_mz"
                handle.seek(e_lfanew)
                if handle.read(4) == b"PE\x00\x00":
                    return "windows_pe"
                return "dos_mz"
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


def gog_primary_exe(game_dir: Path) -> Path | None:
    """Return the primary executable declared in a GOG goggame-*.info file (playTasks), if any."""
    infos: list[Path] = []
    for pattern in ("goggame-*.info", "*/goggame-*.info"):
        infos.extend(p for p in sorted(game_dir.glob(pattern)) if p.is_file() and not p.is_symlink())
    for info in infos:
        try:
            if info.stat().st_size > 1024 * 1024:
                continue
            data = json.loads(info.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        tasks = data.get("playTasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            continue
        ordered = sorted(
            (t for t in tasks if isinstance(t, dict) and t.get("type", "FileTask") == "FileTask"),
            key=lambda t: (not t.get("isPrimary", False), t.get("category", "game") != "game"),
        )
        for task in ordered:
            raw = str(task.get("path") or "").replace("\\", "/").strip()
            if not raw or raw.startswith("/") or ".." in Path(raw).parts:
                continue
            cand = info.parent / raw
            try:
                if cand.is_file() and not cand.is_symlink() and cand.resolve().is_relative_to(game_dir.resolve()):
                    return cand
            except OSError:
                continue
    return None


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


_NATIVE_SUFFIXES = (".x86_64", ".x86", ".amd64")


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


def _is_ignored_exe(exe: Path) -> bool:
    low = exe.name.lower()
    return any(pat in low for pat in IGNORED_EXE_PATTERNS)


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
) -> tuple[list[Path], int | None]:
    """Traverse game_dir once via os.scandir(), returning (ranked_exes, steam_appid)."""
    exe_scored: list[tuple[tuple[int, int, int, int, int], Path]] = []
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
                    if collect_exes and dir_depth < 2 and (
                        low == "start.sh" or low.endswith(_NATIVE_SUFFIXES)
                    ):
                        try:
                            size_neg = -entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            size_neg = 0
                        # Native launchers rank after Windows executables of the same folder.
                        native_rank = 0 if low == "start.sh" else 1
                        exe_scored.append(((2, native_rank, 0, rel_depth, size_neg), Path(entry.path)))
                    elif collect_exes and low.endswith(".exe"):
                        if not any(pat in low for pat in IGNORED_EXE_PATTERNS):
                            try:
                                size_neg = -entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                size_neg = 0
                            is_utility = 1 if any(pat in low for pat in UTILITY_EXE_PATTERNS) else 0
                            is_shipping = 0 if "win64-shipping.exe" in low else 1
                            is_root = 0 if rel_depth == 1 else 1
                            score = (is_utility, is_shipping, is_root, rel_depth, size_neg)
                            exe_scored.append((score, Path(entry.path)))
                    elif collect_appid:
                        if low == "steam_appid.txt":
                            appid_files.append(Path(entry.path))
                        elif low in ini_files:
                            ini_files[low].append(Path(entry.path))
        except OSError:
            continue

        if subdirs:
            subdirs.sort(key=lambda p: p.name)
            for sub in subdirs:
                queue.append((sub, dir_depth + 1))

    exe_scored.sort(key=lambda item: item[0])
    ranked_exes = [item[1] for item in exe_scored]
    steam_appid = _extract_steam_appid(appid_files, ini_files)
    return ranked_exes, steam_appid


def scan_game_folder(game_dir: Path) -> DetectedGame | None:
    """Inspect a single game directory and return a DetectedGame if executables exist."""
    if not game_dir.is_dir():
        return None
    exes, steam_appid = _scan_game_tree_scandir(game_dir)
    gog_exe = gog_primary_exe(game_dir)
    if gog_exe is not None:
        exes = [gog_exe, *(e for e in exes if e != gog_exe)]
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


def default_scan_roots() -> list[Path]:
    """Return standard game/download directories on SteamOS."""
    roots = [
        Path.home() / "Games",
        Path.home() / "Downloads",
        Path.home() / "Syncthing",
    ]
    media_root = Path("/run/media")
    if _safe_is_dir(media_root):
        try:
            with os.scandir(media_root) as it:
                media_entries = sorted(
                    (entry for entry in it if not entry.name.startswith(".")),
                    key=lambda e: e.name,
                )
        except OSError:
            media_entries = []
        for entry in media_entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
                is_symlink = entry.is_symlink()
            except OSError:
                continue
            if not is_dir or is_symlink:
                continue
            child = Path(entry.path)
            if entry.name not in {"steamapps", "lost+found", "System Volume Information"}:
                roots.append(child)
            games_sub = child / "Games"
            if _safe_is_dir(games_sub):
                roots.append(games_sub)
            try:
                with os.scandir(child) as sub_it:
                    sub_entries = sorted(
                        (sub for sub in sub_it if not sub.name.startswith(".")),
                        key=lambda e: e.name,
                    )
            except OSError:
                sub_entries = []
            for sub_entry in sub_entries:
                try:
                    sub_is_dir = sub_entry.is_dir(follow_symlinks=True)
                    sub_is_symlink = sub_entry.is_symlink()
                except OSError:
                    continue
                if (
                    not sub_is_dir
                    or sub_is_symlink
                    or sub_entry.name in {"steamapps", "lost+found", "System Volume Information"}
                ):
                    continue
                sub = Path(sub_entry.path)
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


def scan_all_games(roots: list[Path] | None = None) -> list[DetectedGame]:
    """Scan top-level folders in all roots for installed/extracted games."""
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in (roots if roots is not None else default_scan_roots()):
        try:
            with os.scandir(root) as it:
                entries = sorted(
                    (
                        entry
                        for entry in it
                        if not entry.name.startswith(".")
                        and entry.name not in {"steamapps", "lost+found", "System Volume Information"}
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
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(child)

    if not candidates:
        return []
    if len(candidates) == 1:
        try:
            detected = scan_game_folder(candidates[0])
        except Exception:
            return []
        return [detected] if detected is not None else []

    results: list[DetectedGame] = []
    max_workers = min(8, os.cpu_count() or 4)
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(scan_game_folder, child) for child in candidates]
            for fut in futures:
                try:
                    detected = fut.result()
                except Exception:
                    continue
                if detected is not None:
                    results.append(detected)
    except RuntimeError:
        for child in candidates:
            try:
                detected = scan_game_folder(child)
            except Exception:
                continue
            if detected is not None:
                results.append(detected)
    return results
