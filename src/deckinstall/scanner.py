"""Game folder scanner, executable ranker, and Steam AppID detector."""

from __future__ import annotations

import os
import re
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
    "launcher",
)

SCENE_TAG_RE = re.compile(
    r"[\.\-_ ]?(InsaneRamZes|RUNE|CODEX|EMPRESS|FLT|TENOKE|ElAmigos|FitGirl|DODI|GOG|Portable|Complete[.\-_ ]Edition|Ultimate[.\-_ ]Edition|Deluxe[.\-_ ]Edition).*$",
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


def detect_steam_appid(game_dir: Path) -> int | None:
    """Check common Steam emulator/appid files for an official numeric Steam AppID."""
    for appid_file in [game_dir / "steam_appid.txt", *game_dir.rglob("steam_appid.txt")]:
        if not appid_file.is_file():
            continue
        try:
            text = appid_file.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text.isdigit():
            return int(text)

    for ini_name in ("steam_emu.ini", "rune.ini", "tenoke.ini", "flt.ini"):
        for match in game_dir.rglob(ini_name):
            try:
                content = match.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r"^\s*AppId\s*=\s*(\d+)", content, re.MULTILINE | re.IGNORECASE)
            if m:
                return int(m.group(1))
    return None


def _is_ignored_exe(exe: Path) -> bool:
    low = exe.name.lower()
    return any(pat in low for pat in IGNORED_EXE_PATTERNS)


def rank_executables(game_dir: Path) -> list[Path]:
    """Find and rank candidate Windows executables inside a game folder."""
    candidates: list[Path] = []
    root_depth_limit = 4
    base_parts = len(game_dir.parts)
    for dirpath, dirnames, filenames in os.walk(game_dir, followlinks=False):
        cur = Path(dirpath)
        depth = len(cur.parts) - base_parts
        if depth >= root_depth_limit:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if depth + 1 > root_depth_limit:
            continue
        for fname in filenames:
            if not fname.lower().endswith(".exe"):
                continue
            path = cur / fname
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if _is_ignored_exe(path):
                continue
            candidates.append(path)

    def score(exe: Path) -> tuple[int, int, int, int]:
        name_low = exe.name.lower()
        rel_depth = len(exe.relative_to(game_dir).parts)
        is_shipping = 0 if "win64-shipping.exe" in name_low else 1
        is_root = 0 if rel_depth == 1 else 1
        try:
            size_neg = -exe.stat().st_size
        except OSError:
            size_neg = 0
        return (is_shipping, is_root, rel_depth, size_neg)

    candidates.sort(key=score)
    return candidates


def scan_game_folder(game_dir: Path) -> DetectedGame | None:
    """Inspect a single game directory and return a DetectedGame if executables exist."""
    if not game_dir.is_dir():
        return None
    exes = rank_executables(game_dir)
    if not exes:
        return None
    return DetectedGame(
        name=clean_game_name(game_dir.name),
        root_dir=game_dir,
        executables=exes,
        primary_exe=exes[0],
        steam_appid=detect_steam_appid(game_dir),
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
            media_children = list(media_root.iterdir())
        except OSError:
            media_children = []
        for child in media_children:
            if not _safe_is_dir(child):
                continue
            games_sub = child / "Games"
            if _safe_is_dir(games_sub):
                roots.append(games_sub)
            try:
                sub_children = list(child.iterdir())
            except OSError:
                sub_children = []
            for sub in sub_children:
                if not _safe_is_dir(sub):
                    continue
                sub_games = sub / "Games"
                roots.append(sub_games if _safe_is_dir(sub_games) else sub)
    return [r for r in roots if _safe_is_dir(r)]


def scan_all_games(roots: list[Path] | None = None) -> list[DetectedGame]:
    """Scan top-level folders in all roots for installed/extracted games."""
    seen: set[Path] = set()
    results: list[DetectedGame] = []
    for root in roots or default_scan_roots():
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if (
                not _safe_is_dir(child)
                or child.name.startswith(".")
                or child.name in {"steamapps", "lost+found", "System Volume Information"}
            ):
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            detected = scan_game_folder(child)
            if detected is not None:
                seen.add(resolved)
                results.append(detected)
    return results
