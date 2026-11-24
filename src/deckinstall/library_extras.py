"""Library comforts kept in the app settings: favorite games and DeckInstall's own play time.

Keys are the Library row keys ("standalone:<dir>", "steam:<appid>", "external:..."). Steam keeps
its own play time for Steam shortcuts, so only games DeckInstall starts itself are counted here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

FAVORITES_KEY = "library/favorites"
PLAYTIME_KEY = "library/playtime"
MIN_SESSION_SECONDS = 10  # shorter runs are failed starts, not play
PROTONDB_URL = "https://www.protondb.com/app/{appid}"


class _Settings(Protocol):
    def value(self, key: str, default: Any = ...) -> Any: ...
    def setValue(self, key: str, value: Any) -> None: ...  # noqa: N802 (Qt name)


def _string_list(raw: object) -> list[str]:
    # QSettings hands back a one-item list as a plain string.
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    return [str(raw)] if raw else []


def favorites(settings: _Settings) -> set[str]:
    return set(_string_list(settings.value(FAVORITES_KEY, [])))


def toggle_favorite(settings: _Settings, key: str) -> bool:
    """Flip key in the favorites; True when it is a favorite now."""
    favs = favorites(settings)
    now_favorite = key not in favs
    if now_favorite:
        favs.add(key)
    else:
        favs.discard(key)
    settings.setValue(FAVORITES_KEY, sorted(favs))
    return now_favorite


def _playtime_table(settings: _Settings) -> dict[str, list[float]]:
    try:
        data = json.loads(str(settings.value(PLAYTIME_KEY, "") or "{}"))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[float]] = {}
    for key, row in data.items():
        if isinstance(row, list) and len(row) == 2 and all(isinstance(x, (int, float)) for x in row):
            out[str(key)] = [float(row[0]), float(row[1])]
    return out


def add_session(settings: _Settings, key: str, seconds: float, ended_at: float | None = None) -> None:
    """Add one play session to key's total (sessions under MIN_SESSION_SECONDS are ignored)."""
    if seconds < MIN_SESSION_SECONDS:
        return
    table = _playtime_table(settings)
    total, _last = table.get(key, [0.0, 0.0])
    table[key] = [total + seconds, ended_at if ended_at is not None else time.time()]
    settings.setValue(PLAYTIME_KEY, json.dumps(table, separators=(",", ":")))


def playtime(settings: _Settings, key: str) -> tuple[float, float]:
    """(total seconds, last played as a Unix time); (0, 0) when never played from DeckInstall."""
    total, last = _playtime_table(settings).get(key, [0.0, 0.0])
    return total, last


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def protondb_url(appid: int | None) -> str:
    return PROTONDB_URL.format(appid=appid) if appid and appid > 0 else ""
