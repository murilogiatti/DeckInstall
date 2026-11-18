"""Keep every test away from the real ~/DeckInstall and ~/Games, the library registry and mounted disks."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, save_sync, standalone  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_libraries(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    base = tmp_path_factory.mktemp("libs")
    monkeypatch.setattr(library, "INTERNAL_ROOT", base / "DeckInstall")
    monkeypatch.setattr(library, "registry_path", lambda: base / "data" / "libraries.json")
    monkeypatch.setattr(library, "default_mount_search_roots", lambda: [])
    legacy_games = base / "home" / "Games"
    for attr, value in (
        ("_DEFAULT_GAMES_DIR", legacy_games),
        ("GAMES_DIR", legacy_games),
        ("_DEFAULT_PREFIXES_DIR", legacy_games / ".prefixes"),
        ("PREFIXES_DIR", legacy_games / ".prefixes"),
    ):
        monkeypatch.setattr(standalone, attr, value)
    monkeypatch.setattr(standalone, "APPLICATIONS_DIR", base / "home" / ".local" / "share" / "applications")
    monkeypatch.setattr(save_sync, "DEFAULT_SNAPSHOTS_DIR", base / "home" / "Games" / "saves" / "snapshots")
    yield
