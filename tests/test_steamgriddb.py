"""SteamGridDB artwork: only with a key, only what is missing, only from its CDN."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from deckinstall import steamgriddb as sg


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._body


def test_no_key_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sg.KEY_ENV, raising=False)
    with mock.patch.object(sg.requests, "get") as get:
        assert sg.fill_missing_artwork(1, tmp_path, name="X") == {}
    get.assert_not_called()


def test_key_saved_private_and_removed() -> None:
    sg.save_api_key("  abc  ")
    assert sg.api_key() == "abc"
    assert sg.key_file().stat().st_mode & 0o777 == 0o600
    sg.save_api_key("")
    assert sg.api_key() == "" and not sg.key_file().exists()


def test_fills_only_missing_kinds_from_the_cdn(tmp_path: Path) -> None:
    (tmp_path / "7p.jpg").write_bytes(b"x")  # capsule already there
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        calls.append(url)
        if "search/autocomplete" in url:
            return _Resp({"success": True, "data": [{"id": 42, "name": "Indie"}]})
        if "/logos/" in url:
            return _Resp({"success": True, "data": [{"url": "https://evil.example/logo.png"}]})
        return _Resp({"success": True, "data": [{"url": "https://cdn2.steamgriddb.com/x/img.png"}]})

    with (
        mock.patch.object(sg.requests, "get", side_effect=fake_get),
        mock.patch.object(sg, "_download_first", return_value=True) as dl,
    ):
        saved = sg.fill_missing_artwork(7, tmp_path, name="Indie", key="k")
    assert set(saved) == {"header", "hero"}  # logo came from a foreign host: refused
    assert saved["header"] == tmp_path / "7.png" and saved["hero"] == tmp_path / "7_hero.png"
    assert not any("/grids/game/42" in c and "600x900" in c for c in calls)
    assert all(args[0][0][0].startswith("https://cdn2.steamgriddb.com/") for args in dl.call_args_list)


def test_steam_appid_uses_the_steam_platform(tmp_path: Path) -> None:
    urls: list[str] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        urls.append(url)
        return _Resp({"success": True, "data": []})

    with mock.patch.object(sg.requests, "get", side_effect=fake_get):
        assert sg.fill_missing_artwork(7, tmp_path, name="Indie", steam_appid=570, key="k") == {}
    assert urls and all("/steam/570" in u for u in urls)
