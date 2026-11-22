"""Official Steam Store search and Steam CDN grid artwork downloader."""

from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_CDN_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
USER_AGENT = f"DeckInstall/{__version__}"
_MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _validate_appid(appid: int) -> int:
    if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
        raise ValueError(f"Invalid Steam AppID: {appid!r}")
    return appid


def _is_valid_image_bytes(data: bytes) -> bool:
    if len(data) <= 512:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _read_bounded_image(resp: requests.Response) -> bytes | None:
    if resp.status_code != 200:
        return None
    ctype = str(resp.headers.get("Content-Type", "")).lower()
    if ctype and not ctype.startswith("image/"):
        return None
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > _MAX_IMAGE_BYTES:
            return None
    data = bytes(buf)
    return data if _is_valid_image_bytes(data) else None


@dataclass(frozen=True)
class SteamMatch:
    """A matching game from Steam Store search."""

    appid: int
    name: str


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
    try:
        payload = resp.json()
    except ValueError:
        return []
    items = payload.get("items", []) if isinstance(payload, dict) else []
    matches: list[SteamMatch] = []
    for item in items[:limit]:
        appid = item.get("id")
        name = item.get("name")
        if isinstance(appid, int) and name:
            matches.append(SteamMatch(appid=appid, name=str(name)))
    return matches


def fetch_header_preview(steam_appid: int) -> bytes | None:
    """Fetch official Steam header.jpg bytes safely for UI preview."""
    appid = _validate_appid(steam_appid)
    url = f"{STEAM_CDN_BASE}/{appid}/header.jpg"
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, stream=True) as resp:
            return _read_bounded_image(resp)
    except requests.RequestException:
        return None


def _download_first(urls: list[str], dest: Path, alt_remove: list[Path] | None = None) -> bool:
    for url in urls:
        try:
            with requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=15, stream=True
            ) as resp:
                data = _read_bounded_image(resp)
            if data is not None:
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent)
                )
                tmp_path = Path(tmp_name)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                    if dest.is_symlink():
                        dest.unlink()
                    os.replace(tmp_path, dest)
                    for alt in alt_remove or ():
                        if alt != dest and alt.exists() and not alt.is_symlink():
                            try:
                                alt.unlink()
                            except OSError:
                                pass
                    return True
                except Exception:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                    raise
        except (requests.RequestException, OSError):
            continue
    return False


def _grid_files_for_appid(shortcut_appid_u32: int, grid_dir: Path) -> list[Path]:
    if not grid_dir.is_dir():
        return []
    prefix = str(shortcut_appid_u32)
    matched: list[Path] = []
    for p in sorted(grid_dir.iterdir()):
        if p.is_symlink() or not p.is_file() or not p.name.startswith(prefix):
            continue
        rest = p.name[len(prefix) :]
        if rest and rest[0] not in ("p", "_", "."):
            continue
        matched.append(p)
    return matched


def backup_steam_artwork(shortcut_appid_u32: int, grid_dir: Path) -> list[str]:
    """Back up existing Steam grid artwork for shortcut_appid_u32 before overwriting."""
    if grid_dir.is_symlink():
        return []
    bak_root = grid_dir / ".deckinstall_art_bak"
    if bak_root.is_symlink():
        return []
    bak_dir = bak_root / str(shortcut_appid_u32)
    if bak_dir.is_symlink():
        return []
    if bak_dir.is_dir():
        existing_bak = [
            p.name for p in sorted(bak_dir.iterdir()) if not p.is_symlink() and p.is_file()
        ]
        if existing_bak:
            return existing_bak
    existing = _grid_files_for_appid(shortcut_appid_u32, grid_dir)
    if not existing:
        return []
    bak_root.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".bak_{shortcut_appid_u32}_", dir=str(bak_root)))
    copied: list[str] = []
    try:
        for src in existing:
            if src.is_symlink():
                continue
            shutil.copy2(src, stage_dir / src.name, follow_symlinks=False)
            copied.append(src.name)
        if bak_dir.exists():
            shutil.rmtree(bak_dir, ignore_errors=True)
        stage_dir.rename(bak_dir)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return copied


def download_steam_artwork(
    steam_appid: int,
    shortcut_appid_u32: int,
    grid_dir: Path,
) -> ArtworkResult:
    """Download official capsule, header, hero, and logo into Steam's grid folder."""
    appid = _validate_appid(steam_appid)
    if grid_dir.is_symlink():
        raise OSError(f"Refusing symlinked Steam grid folder: {grid_dir}")
    grid_dir.mkdir(parents=True, exist_ok=True)
    backup_steam_artwork(shortcut_appid_u32, grid_dir)
    base = f"{STEAM_CDN_BASE}/{appid}"

    targets: dict[str, tuple[Path, list[str], list[Path]]] = {
        "capsule": (
            grid_dir / f"{shortcut_appid_u32}p.jpg",
            [f"{base}/library_600x900_2x.jpg", f"{base}/library_600x900.jpg"],
            [grid_dir / f"{shortcut_appid_u32}p.png"],
        ),
        "header": (
            grid_dir / f"{shortcut_appid_u32}.jpg",
            [f"{base}/header.jpg", f"{base}/library_capsule.jpg"],
            [grid_dir / f"{shortcut_appid_u32}.png"],
        ),
        "hero": (
            grid_dir / f"{shortcut_appid_u32}_hero.jpg",
            [f"{base}/library_hero_2x.jpg", f"{base}/library_hero.jpg"],
            [grid_dir / f"{shortcut_appid_u32}_hero.png"],
        ),
        "logo": (
            grid_dir / f"{shortcut_appid_u32}_logo.png",
            [f"{base}/logo.png", f"{base}/library_logo.png"],
            [grid_dir / f"{shortcut_appid_u32}_logo.jpg"],
        ),
    }

    saved: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {
            kind: pool.submit(_download_first, urls, dest, alts)
            for kind, (dest, urls, alts) in targets.items()
        }
        for kind, fut in futs.items():
            try:
                if fut.result():
                    saved[kind] = targets[kind][0]
            except Exception:
                continue

    return ArtworkResult(steam_appid=appid, saved_files=saved)
