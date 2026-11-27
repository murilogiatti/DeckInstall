"""Per-game Proton policy: "Default (newest)" or pinned, previous version, update and pruning.

A game in "default" mode has an empty ``proton_name``: its launcher is written with the newest
installed GE-Proton, and updating GE-Proton rewrites those launchers.  The version a game ran
before a change is kept in ``meta["proton_previous"]`` so "Use previous version" is one click.
Only the current and the previous GE-Proton are kept, unless a game is pinned to another one.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Iterable
from pathlib import Path

from . import runtime_manager as _rm
from .manifest import find_manifest
from .standalone import (
    StandaloneGame,
    _load_manifest,
    _resolve_proton_path,
    _validate_launch_options,
    _write_manifest,
    list_standalone_games,
    refresh_integration_files,
)

DEFAULT = "default"
PINNED = "pinned"
# Combo/item data meaning "Default (newest)" in the UI (an empty string means "nothing" there).
DEFAULT_PROTON_ID = "__default__"


def newest_ge_proton() -> Path | None:
    """Return the newest installed GE-Proton (11+), CachyOS Proton, or GE-Proton10, or None."""
    found = (
        _rm.list_ge_proton11_dirs()
        or _rm.list_cachyos_proton_dirs()
        or _rm.list_ge_proton10_dirs()
    )
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


def set_game_proton(
    game_dir: Path, tool_name: str, *, previous_tool: str = "", launch_settings: tuple[str, str] | None = None,
) -> StandaloneGame:
    """Pin a game to tool_name, or put it back on "Default (newest)" with an empty name."""
    game = _load(game_dir)
    if launch_settings is not None:
        opts, dll = launch_settings
        _validate_launch_options(opts)
        _validate_launch_options(dll)
        game.launch_options = opts
        game.winedlloverrides = dll
    cleaned = "" if tool_name.strip() == DEFAULT_PROTON_ID else tool_name.strip()
    return _apply(game, cleaned, before=previous_tool or effective_proton_name(game))


def use_previous_proton(game_dir: Path, *, previous_tool: str = "") -> StandaloneGame:
    """Pin the game to the Proton it used before the last change."""
    game = _load(game_dir)
    previous = previous_proton_name(game)
    if not previous:
        raise ValueError(f"{game.name} has no previous Proton version recorded.")
    if not Path(_resolve_proton_path(previous)).is_dir():
        raise FileNotFoundError(f"{previous} is no longer installed.")
    return _apply(game, previous, before=previous_tool or effective_proton_name(game))


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
    """After a GE-Proton change, rewrite the launchers of every "Default" game to match."""
    newest = newest_ge_proton()
    moved = 0
    for game in games if games is not None else list_standalone_games():
        if not is_default(game):
            continue
        if previous_name and (newest is None or previous_name != newest.name):
            game.meta["proton_previous"] = previous_name
        game.meta["proton_mode"] = DEFAULT
        try:
            refresh_integration_files(game)
            _write_manifest(game)
            moved += 1
        except OSError as exc:
            logging.warning(
                "Could not move %s to %s: %s",
                game.name,
                newest.name if newest is not None else "default",
                exc,
            )
    return moved


def retarget_stale_default_games(games: Iterable[StandaloneGame] | None = None) -> int:
    """Rewrite "Default" launchers whose baked PROTONPATH no longer exists (e.g. a system package upgrade)."""
    newest = newest_ge_proton()
    if newest is None:
        return 0
    moved = 0
    for game in games if games is not None else list_standalone_games():
        if not is_default(game):
            continue
        try:
            text = game.launch_script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"^export PROTONPATH=(.+)$", text, re.MULTILINE)
        if match is None:
            continue
        try:
            baked = shlex.split(match.group(1))[0]
        except (ValueError, IndexError):
            continue
        if baked == str(newest) or Path(baked).is_dir():
            continue
        try:
            refresh_integration_files(game)
            moved += 1
        except OSError as exc:
            logging.warning("Could not retarget %s: %s", game.name, exc)
    return moved


