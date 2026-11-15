"""Official Steam Store search and Steam CDN grid artwork downloader."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import requests

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_CDN_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
USER_AGENT = "DeckInstall/0.7.0"


@dataclass(frozen=True)
class SteamMatch:
    """A matching game from Steam Store search."""

    appid: int
    name: str
    tiny_image: str


@dataclass(frozen=True)
class ArtworkResult:
    """Summary of downloaded Steam grid artwork files."""

    steam_appid: int
    saved_files: dict[str, Path]


def search_steam_games(query: str, limit: int = 8) -> list[SteamMatch]:
    """Search official Steam Store titles by name."""
    if not query.strip():
        return []
    resp = requests.get(
        STEAM_SEARCH_URL,
        params={"term": query.strip(), "l": "english", "cc": "us"},
        headers={"User-Agent": USER_AGENT},
        timeout=12,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    matches: list[SteamMatch] = []
    for item in items[:limit]:
        appid = item.get("id")
        name = item.get("name")
        if isinstance(appid, int) and name:
            matches.append(
                SteamMatch(
                    appid=appid,
                    name=str(name),
                    tiny_image=str(item.get("tiny_image", "")),
                )
            )
    return matches


def _download_first(urls: list[str], dest: Path) -> bool:
    for url in urls:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 512:
                dest.write_bytes(resp.content)
                return True
        except requests.RequestException:
            continue
    return False


def _grid_files_for_appid(shortcut_appid_u32: int, grid_dir: Path) -> list[Path]:
    if not grid_dir.is_dir():
        return []
    prefix = str(shortcut_appid_u32)
    matched: list[Path] = []
    for p in sorted(grid_dir.iterdir()):
        if not p.is_file() or not p.name.startswith(prefix):
            continue
        rest = p.name[len(prefix) :]
        if rest and not (rest[0] in ("p", "_", ".")):
            continue
        matched.append(p)
    return matched


def backup_steam_artwork(shortcut_appid_u32: int, grid_dir: Path) -> list[str]:
    """Back up existing Steam grid artwork for shortcut_appid_u32 before overwriting."""
    existing = _grid_files_for_appid(shortcut_appid_u32, grid_dir)
    if not existing:
        return []
    bak_dir = grid_dir / ".deckinstall_art_bak" / str(shortcut_appid_u32)
    if bak_dir.exists():
        shutil.rmtree(bak_dir, ignore_errors=True)
    bak_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in existing:
        shutil.copy2(src, bak_dir / src.name)
        copied.append(src.name)
    return copied


def restore_steam_artwork_backup(shortcut_appid_u32: int, grid_dir: Path) -> list[str]:
    """Restore previously backed-up Steam grid artwork for shortcut_appid_u32."""
    bak_dir = grid_dir / ".deckinstall_art_bak" / str(shortcut_appid_u32)
    if not bak_dir.is_dir():
        return []
    bak_files = [p for p in sorted(bak_dir.iterdir()) if p.is_file()]
    if not bak_files:
        return []
    for cur in _grid_files_for_appid(shortcut_appid_u32, grid_dir):
        try:
            cur.unlink()
        except OSError:
            pass
    restored: list[str] = []
    for src in bak_files:
        shutil.copy2(src, grid_dir / src.name)
        restored.append(src.name)
    return restored


def download_steam_artwork(
    steam_appid: int,
    shortcut_appid_u32: int,
    grid_dir: Path,
) -> ArtworkResult:
    """Download official capsule, header, hero, and logo into Steam's grid folder."""
    grid_dir.mkdir(parents=True, exist_ok=True)
    backup_steam_artwork(shortcut_appid_u32, grid_dir)
    base = f"{STEAM_CDN_BASE}/{steam_appid}"

    targets: dict[str, tuple[Path, list[str]]] = {
        "capsule": (
            grid_dir / f"{shortcut_appid_u32}p.jpg",
            [f"{base}/library_600x900_2x.jpg", f"{base}/library_600x900.jpg"],
        ),
        "header": (
            grid_dir / f"{shortcut_appid_u32}.jpg",
            [f"{base}/header.jpg", f"{base}/library_capsule.jpg"],
        ),
        "hero": (
            grid_dir / f"{shortcut_appid_u32}_hero.jpg",
            [f"{base}/library_hero_2x.jpg", f"{base}/library_hero.jpg"],
        ),
        "logo": (
            grid_dir / f"{shortcut_appid_u32}_logo.png",
            [f"{base}/logo.png", f"{base}/library_logo.png"],
        ),
    }

    saved: dict[str, Path] = {}
    for kind, (dest, urls) in targets.items():
        if _download_first(urls, dest):
            saved[kind] = dest

    return ArtworkResult(steam_appid=steam_appid, saved_files=saved)
