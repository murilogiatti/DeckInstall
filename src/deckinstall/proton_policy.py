"""Per-game Proton policy: "Default (newest)" or pinned, previous version, update and pruning.

A game in "default" mode has an empty ``proton_name``: its launcher is written with the newest
installed GE-Proton, and updating GE-Proton rewrites those launchers.  The version a game ran
before a change is kept in ``meta["proton_previous"]`` so "Use previous version" is one click.
Only the current and the previous GE-Proton are kept, unless a game is pinned to another one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from . import runtime_manager as _rm
from .manifest import find_manifest
from .standalone import (
    StandaloneGame,
    _load_manifest,
    _resolve_proton_path,
    _write_manifest,
    list_standalone_games,
    refresh_integration_files,
)

DEFAULT = "default"
PINNED = "pinned"
# Combo/item data meaning "Default (newest)" in the UI (an empty string means "nothing" there).
DEFAULT_PROTON_ID = "__default__"


def newest_ge_proton() -> Path | None:
    """Return the newest installed GE-Proton (11+, then 10), or None."""
    found = _rm.list_ge_proton11_dirs() or _rm.list_ge_proton10_dirs()
    return found[0] if found else None


def is_default(game: StandaloneGame) -> bool:
    return not game.native_linux and not game.proton_name.strip()


def effective_proton_name(game: StandaloneGame) -> str:
    """The Proton the game's launcher uses now ("" for native games or when nothing is installed)."""
    if game.native_linux:
        return ""
    if game.proton_name.strip():
        return game.proton_name.strip()
    newest = newest_ge_proton()
    return newest.name if newest is not None else ""


def previous_proton_name(game: StandaloneGame) -> str:
    return str(game.meta.get("proton_previous") or "").strip()


def _load(game_dir: Path) -> StandaloneGame:
    manifest = find_manifest(game_dir)
    game = _load_manifest(manifest) if manifest is not None else None
    if game is None:
        raise FileNotFoundError(f"Standalone manifest not found in {game_dir}")
    if game.native_linux:
        raise ValueError(f"{game.name} is a native Linux game and does not use Proton.")
    return game


def _apply(game: StandaloneGame, tool_name: str, *, before: str) -> StandaloneGame:
    """Switch the game to tool_name ("" = default) and remember `before` as its previous version."""
    game.proton_name = tool_name.strip()
    game.meta["proton_mode"] = PINNED if game.proton_name else DEFAULT
    after = effective_proton_name(game)
    if before and before != after:
        game.meta["proton_previous"] = before
    refresh_integration_files(game)
    _write_manifest(game)
    return game


def set_game_proton(game_dir: Path, tool_name: str) -> StandaloneGame:
    """Pin a game to tool_name, or put it back on "Default (newest)" with an empty name."""
    game = _load(game_dir)
    return _apply(game, tool_name, before=effective_proton_name(game))


def use_previous_proton(game_dir: Path) -> StandaloneGame:
    """Pin the game to the Proton it used before the last change."""
    game = _load(game_dir)
    previous = previous_proton_name(game)
    if not previous:
        raise ValueError(f"{game.name} has no previous Proton version recorded.")
    if not Path(_resolve_proton_path(previous)).is_dir():
        raise FileNotFoundError(f"{previous} is no longer installed.")
    return _apply(game, previous, before=effective_proton_name(game))


def set_games_proton(game_dirs: Iterable[Path], tool_name: str) -> tuple[int, list[str]]:
    """Batch pin/unpin. Returns (changed, errors)."""
    changed = 0
    errors: list[str] = []
    for game_dir in game_dirs:
        try:
            set_game_proton(game_dir, tool_name)
            changed += 1
        except (OSError, ValueError) as exc:
            errors.append(f"{game_dir.name}: {exc}")
    return changed, errors


def retarget_default_games(previous_name: str, games: Iterable[StandaloneGame] | None = None) -> int:
    """After a GE-Proton update, rewrite the launchers of every "Default" game to the newest version."""
    newest = newest_ge_proton()
    if newest is None:
        return 0
    moved = 0
    for game in games if games is not None else list_standalone_games():
        if not is_default(game):
            continue
        if previous_name and previous_name != newest.name:
            game.meta["proton_previous"] = previous_name
        game.meta["proton_mode"] = DEFAULT
        try:
            refresh_integration_files(game)
            _write_manifest(game)
            moved += 1
        except OSError as exc:
            logging.warning("Could not move %s to %s: %s", game.name, newest.name, exc)
    return moved


def protected_tool_names(games: Iterable[StandaloneGame] | None = None, extra: Iterable[str] = ()) -> set[str]:
    """Proton names a game is pinned to or would fall back to with "Use previous version"."""
    names = {n.strip() for n in extra if n.strip()}
    for game in games if games is not None else list_standalone_games():
        if game.native_linux:
            continue
        for name in (game.proton_name, previous_proton_name(game)):
            if name.strip():
                names.add(name.strip())
    return names


def prune_ge_proton(keep: int = 2, protected: Iterable[str] = ()) -> list[Path]:
    """Remove old GE-Proton versions from DeckInstall's compat-tools dir, keeping the newest `keep`.

    Versions in `protected` (pinned games, Steam mappings) and anything outside the writable
    compatibilitytools.d (Flatpak/system copies) are never touched.
    """
    keep_names = set(protected)
    managed = _rm.get_compat_tools_dir()
    removed: list[Path] = []
    for index, tool_dir in enumerate(_rm.list_ge_proton_dirs(min_major=10)):
        if index < keep or tool_dir.name in keep_names or tool_dir.parent != managed:
            continue
        try:
            _rm.remove_compat_tool(tool_dir)
            removed.append(tool_dir)
        except (OSError, ValueError) as exc:
            logging.warning("Could not remove old %s: %s", tool_dir, exc)
    return removed
