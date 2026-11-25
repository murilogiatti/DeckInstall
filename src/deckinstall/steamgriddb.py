"""SteamGridDB artwork for Steam shortcuts, used when the official Steam art is missing.

Needs the user's own free API key (steamgriddb.com > Preferences > API). It is kept in
~/.config/DeckInstall/steamgriddb.key (mode 600), or read from DECKINSTALL_STEAMGRIDDB_KEY.
Without a key nothing here touches the network.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from .artwork import USER_AGENT, _download_first, _validate_appid
from .fsutil import atomic_write_text

API_BASE = "https://www.steamgriddb.com/api/v2"
KEY_ENV = "DECKINSTALL_STEAMGRIDDB_KEY"
_IMAGE_HOSTS = frozenset({"cdn2.steamgriddb.com", "cdn.steamgriddb.com"})

# kind -> (API collection, query, grid file stem suffix). Steam names the files <appid><suffix>.<ext>.
_KINDS: dict[str, tuple[str, dict[str, str], str]] = {
    "capsule": ("grids", {"dimensions": "600x900,342x482,660x930"}, "p"),
    "header": ("grids", {"dimensions": "920x430,460x215"}, ""),
    "hero": ("heroes", {}, "_hero"),
    "logo": ("logos", {}, "_logo"),
}


def key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "DeckInstall" / "steamgriddb.key"


def api_key() -> str:
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    try:
        return key_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_api_key(key: str) -> None:
    """Store the key (empty removes it)."""
    path = key_file()
    key = key.strip()
    if not key:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, key + "\n", mode=0o600)


def _get(path: str, key: str, params: dict[str, str] | None = None) -> Any:
    resp = requests.get(
        f"{API_BASE}/{path}",
        params=params,
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {key}"},
        timeout=15,
        allow_redirects=False,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    return payload.get("data")


def find_game_id(name: str, key: str) -> int | None:
    """SteamGridDB game id of the best name match."""
    term = name.strip()
    if not term:
        return None
    data = _get(f"search/autocomplete/{urllib.parse.quote(term, safe='')}", key)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        gid = data[0].get("id")
        return gid if isinstance(gid, int) and gid > 0 else None
    return None


def _first_image_url(collection: str, owner: str, key: str, params: dict[str, str]) -> str:
    query = {"mimes": "image/png,image/jpeg", "types": "static", "nsfw": "false", **params}
    data = _get(f"{collection}/{owner}", key, query)
    for item in data if isinstance(data, list) else []:
        url = str(item.get("url") or "") if isinstance(item, dict) else ""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https" and (parsed.hostname or "") in _IMAGE_HOSTS:
            return url
    return ""


def _existing(grid_dir: Path, shortcut_appid_u32: int, suffix: str) -> bool:
    return any((grid_dir / f"{shortcut_appid_u32}{suffix}.{ext}").is_file() for ext in ("png", "jpg", "jpeg"))


def fill_missing_artwork(
    shortcut_appid_u32: int,
    grid_dir: Path,
    *,
    name: str,
    steam_appid: int | None = None,
    key: str | None = None,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, Path]:
    """Download from SteamGridDB the grid images the shortcut does not have yet.

    kinds: which of capsule/header/hero/logo to look for (default: all).
    """
    key = api_key() if key is None else key
    if not key or grid_dir.is_symlink():
        return {}
    wanted = {k: v for k, v in _KINDS.items() if kinds is None or k in kinds}
    missing = {k: v for k, v in wanted.items() if not _existing(grid_dir, shortcut_appid_u32, v[2])}
    if not missing:
        return {}
    try:
        if steam_appid:
            owner = f"steam/{_validate_appid(steam_appid)}"
        else:
            gid = find_game_id(name, key)
            if gid is None:
                return {}
            owner = f"game/{gid}"
    except (requests.RequestException, ValueError) as exc:
        logging.info("SteamGridDB lookup failed for %s: %s", name, exc)
        if isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", 0) in (401, 403):
            raise  # a wrong key fails every game the same way: let the caller say so once
        return {}
    grid_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    for kind, (collection, params, suffix) in missing.items():
        try:
            url = _first_image_url(collection, owner, key, params)
        except (requests.RequestException, ValueError) as exc:
            logging.info("SteamGridDB %s failed for %s: %s", kind, name, exc)
            if isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", 0) in (401, 403):
                raise
            continue
        if not url:
            continue
        ext = "png" if url.lower().endswith(".png") else "jpg"
        dest = grid_dir / f"{shortcut_appid_u32}{suffix}.{ext}"
        other = grid_dir / f"{shortcut_appid_u32}{suffix}.{'jpg' if ext == 'png' else 'png'}"
        if _download_first([url], dest, [other]):
            saved[kind] = dest
    return saved
