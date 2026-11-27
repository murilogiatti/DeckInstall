"""Favorites, play time of the Library."""

from __future__ import annotations

from typing import Any

from deckinstall import library_extras as le


class _Settings:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def value(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def setValue(self, key: str, value: Any) -> None:  # noqa: N802
        self.data[key] = value


def test_favorites_toggle_and_single_string_value() -> None:
    s = _Settings()
    assert le.toggle_favorite(s, "standalone:/g/A") is True
    assert le.favorites(s) == {"standalone:/g/A"}
    s.data[le.FAVORITES_KEY] = "steam:1"  # how QSettings returns a one-item list
    assert le.favorites(s) == {"steam:1"}
    assert le.toggle_favorite(s, "steam:1") is False
    assert le.favorites(s) == set()


def test_playtime_adds_sessions_and_skips_failed_starts() -> None:
    s = _Settings()
    le.add_session(s, "k", 3, ended_at=100)  # a failed start
    assert le.playtime(s, "k") == (0.0, 0.0)
    le.add_session(s, "k", 3600, ended_at=200)
    le.add_session(s, "k", 1500, ended_at=300)
    assert le.playtime(s, "k") == (5100.0, 300.0)
    assert le.format_duration(5100) == "1 h 25 min"
    assert le.format_duration(59) == "0 min"
    s.data[le.PLAYTIME_KEY] = "not json"
    assert le.playtime(s, "k") == (0.0, 0.0)
