"""ProtonDB's summary of a game, asked for only when the user clicks (it sends the Steam AppID and
nothing else), and kept for a few days."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_text

SUMMARY_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
PAGE_URL = "https://www.protondb.com/app/{appid}"
CACHE_MAX_AGE_S = 3 * 24 * 3600
TIERS = ("platinum", "gold", "silver", "bronze", "borked", "pending")


@dataclass(frozen=True)
class Summary:
    tier: str  # one of TIERS, or "" when ProtonDB has no page for the game
    total: int = 0
    trending: str = ""

    @property
    def found(self) -> bool:
        return bool(self.tier)

    @property
    def poor(self) -> bool:
        return self.tier in ("bronze", "borked")


def cache_dir() -> Path:
    return Path.home() / ".cache" / "DeckInstall" / "protondb"


def summary(
    appid: int | str,
    cache: Path | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    now: float | None = None,
) -> Summary:
    """The game's ProtonDB summary (cached). Raises OSError when offline and nothing is cached."""
    appid = str(int(appid))
    folder = cache or cache_dir()
    path = folder / f"{appid}.json"
    stamp = now or time.time()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if stamp - float(cached.get("fetched", 0)) < CACHE_MAX_AGE_S:
            return Summary(cached.get("tier", ""), int(cached.get("total", 0)), cached.get("trending", ""))
    except (OSError, ValueError, TypeError):
        cached = None
    request = urllib.request.Request(SUMMARY_URL.format(appid=appid), headers={"User-Agent": "DeckInstall"})
    try:
        with opener(request, timeout=10) as response:  # type: ignore[attr-defined]
            data = json.loads(response.read(1 << 16))
        tier = str(data.get("tier", "")).lower()
        result = Summary(tier if tier in TIERS else "", int(data.get("total", 0) or 0),
                         str(data.get("trendingTier", "")).lower())
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code != 404:
            raise OSError(str(exc)) from exc
        result = Summary("")  # ProtonDB has no reports for this game
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        if cached:  # offline: an older answer is better than none
            return Summary(cached.get("tier", ""), int(cached.get("total", 0)), cached.get("trending", ""))
        raise OSError(str(exc)) from exc
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"tier": result.tier, "total": result.total,
                                        "trending": result.trending, "fetched": stamp}))
    return result
