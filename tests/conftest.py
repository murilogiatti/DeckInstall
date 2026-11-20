"""Keep every test away from the real ~/DeckInstall and ~/Games, the library registry and mounted disks."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, runtime_manager, save_sources, save_sync, standalone, vdf_queue  # noqa: E402


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
    monkeypatch.setattr(standalone, "game_logs_root", lambda: base / "home" / ".local" / "share" / "DeckInstall" / "logs")
    monkeypatch.setattr(standalone, "APPLICATIONS_DIR", base / "home" / ".local" / "share" / "applications")
    # No network in tests, and never read the real umu-database cache.
    monkeypatch.setattr(runtime_manager, "ensure_umu_database", lambda *a, **k: None)
    monkeypatch.setattr(runtime_manager, "get_umu_database_path", lambda: base / "runtime" / "umu-database.csv")
    # Never see (or prune) the user's real Proton installs.
    compat = base / "compatibilitytools.d"
    monkeypatch.setattr(runtime_manager, "get_compat_tools_dir", lambda: compat)
    monkeypatch.setattr(
        runtime_manager,
        "_all_compat_tool_dirs",
        lambda steam_root=None: [
            runtime_manager.get_compat_tools_dir(),
            *runtime_manager._iter_extra_compat_tool_dirs(),
        ],
    )
    # Other launchers (Hydra, UMU, Nexus) are looked up in an empty home, never the real one.
    monkeypatch.setattr(save_sources, "default_home", lambda: base / "home")
    # Never read or write the real saved Steam edit queue.
    monkeypatch.setattr(vdf_queue, "default_queue_file", lambda: base / "data" / "vdf_queue.json")
    monkeypatch.setattr(save_sync, "DEFAULT_SNAPSHOTS_DIR", base / "home" / "Games" / "saves" / "snapshots")
    yield
