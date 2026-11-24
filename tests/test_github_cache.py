"""GitHub API answers: ETag cache, retries, stale fallback and optional token."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
import requests

from deckinstall import runtime_manager as rm

URL = "https://api.github.com/repos/x/y/releases/latest"


class _Resp:
    def __init__(self, status: int, body: Any = None, etag: str | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm.time, "sleep", lambda _s: None)


def test_etag_is_sent_and_304_returns_cached_body() -> None:
    calls: list[dict[str, str]] = []
    answers = iter([_Resp(200, {"tag_name": "v1"}, etag='"abc"'), _Resp(304)])

    def fake(url: str, *, headers: dict[str, str], timeout: float, **_: Any) -> _Resp:
        calls.append(dict(headers))
        return next(answers)

    with mock.patch.object(rm, "_request_github_with_validated_redirects", side_effect=fake):
        assert rm._fetch_json(URL) == {"tag_name": "v1"}
        assert rm._fetch_json(URL) == {"tag_name": "v1"}
    assert "If-None-Match" not in calls[0]
    assert calls[1]["If-None-Match"] == '"abc"'


def test_server_errors_are_retried() -> None:
    answers = iter([_Resp(502), _Resp(503), _Resp(200, [1, 2])])
    with mock.patch.object(rm, "_request_github_with_validated_redirects", side_effect=lambda *a, **k: next(answers)):
        assert rm._fetch_json(URL) == [1, 2]


def test_rate_limit_and_network_errors_fall_back_to_cache() -> None:
    rm._write_github_cache(URL, '"e"', {"cached": True})
    with mock.patch.object(rm, "_request_github_with_validated_redirects", return_value=_Resp(403)):
        assert rm._fetch_json(URL) == {"cached": True}
    with mock.patch.object(
        rm, "_request_github_with_validated_redirects", side_effect=requests.ConnectionError("offline")
    ):
        assert rm._fetch_json(URL) == {"cached": True}


def test_network_error_without_cache_raises() -> None:
    with mock.patch.object(
        rm, "_request_github_with_validated_redirects", side_effect=requests.ConnectionError("offline")
    ) as req:
        with pytest.raises(requests.ConnectionError):
            rm._fetch_json(URL)
    assert req.call_count == rm._FETCH_RETRIES + 1


def test_token_only_goes_to_the_api_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rm.GITHUB_TOKEN_ENV, "secret")
    seen: list[dict[str, str]] = []

    def fake(url: str, *, headers: dict[str, str], timeout: float, **_: Any) -> _Resp:
        seen.append(dict(headers))
        return _Resp(200, {})

    with mock.patch.object(rm, "_request_github_with_validated_redirects", side_effect=fake):
        rm._fetch_json(URL)
        rm._fetch_json("https://github.com/x/y/releases.atom")
    assert seen[0]["Authorization"] == "Bearer secret"
    assert "Authorization" not in seen[1]
