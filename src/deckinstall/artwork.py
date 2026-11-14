"""Official Steam Store search and Steam CDN grid artwork downloader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_CDN_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
USER_AGENT = "DeckInstall/0.4.1"


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


def download_steam_artwork(
    steam_appid: int,
    shortcut_appid_u32: int,
    grid_dir: Path,
) -> ArtworkResult:
    """Download official capsule, header, hero, and logo into Steam's grid folder."""
    grid_dir.mkdir(parents=True, exist_ok=True)
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
