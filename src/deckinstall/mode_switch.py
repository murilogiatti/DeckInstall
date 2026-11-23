"""Move a managed game between Steam mode and Standalone mode without leaving anything behind.

To Steam: add the Steam shortcut (its compatdata links to the game's own prefix, so saves stay),
then remove the app-menu and Desktop launchers. To Standalone: remove the game's Steam shortcuts
and the compatdata link (restoring any Steam prefix that link replaced), then create the app-menu
launcher. Game files, prefix and saves are never touched. Steam must be closed (VDF writes).
"""

from __future__ import annotations

import logging
import os

from .manifest import decode_path, find_manifest
from .modes import STANDALONE, STEAM
from .standalone import (
    StandaloneGame,
    _load_manifest,
    _unlink_steam_compatdata,
    _write_manifest,
    add_installed_game_to_steam,
    match_owned_shortcuts,
    set_game_launcher,
)
from .steam_paths import SteamPaths
from .steam_vdf import load_shortcuts, remove_shortcut


def _reload(game: StandaloneGame) -> StandaloneGame:
    manifest = find_manifest(game.game_dir)
    loaded = _load_manifest(manifest) if manifest is not None else None
    if loaded is None or loaded.uid != game.uid:
        raise ValueError(f"Game manifest is unavailable or changed: {game.game_dir}")
    return loaded


def _require_managed(game: StandaloneGame) -> None:
    if game.schema < 2 or not game.uid:
        raise ValueError("This game uses an old manifest; open it once in the Library to update it first.")


def switch_to_steam(paths: SteamPaths, game: StandaloneGame) -> StandaloneGame:
    """Steam runs the game from now on: add its shortcut, then drop the menu/Desktop launchers."""
    _require_managed(game)
    add_installed_game_to_steam(paths, game.game_dir, game.uid)
    game = _reload(game)
    for where in ("menu", "desktop"):
        game = set_game_launcher(game, where, False)
    game.meta["mode"] = STEAM
    _write_manifest(game)
    return game


def _restore_compatdata_backup(game: StandaloneGame) -> None:
    """Put back the Steam prefix that the compatdata link replaced, once the link is gone."""
    steam = game.meta.get("steam") or {}
    raw_link, raw_backup = str(steam.get("compatdata_link") or ""), str(steam.get("compatdata_backup") or "")
    if not raw_link or not raw_backup:
        return
    try:
        link, backup = decode_path(raw_link, None), decode_path(raw_backup, None)
    except ValueError:
        return
    if link.exists() or link.is_symlink():
        return
    if backup.is_dir() and not backup.is_symlink():
        try:
            os.replace(backup, link)
        except OSError as exc:
            logging.warning("Could not restore Steam prefix %s from %s: %s", link, backup, exc)


def switch_to_standalone(
    paths: SteamPaths | None, game: StandaloneGame, *, menu: bool = True, desktop: bool = False,
) -> StandaloneGame:
    """DeckInstall runs the game from now on: remove its Steam shortcuts and compatdata link,
    then create the chosen launchers. ``paths`` may be None when Steam is gone."""
    _require_managed(game)
    if paths is not None and paths.shortcuts_vdf.is_file():
        entries = load_shortcuts(paths.shortcuts_vdf, strict=True)
        for appid in match_owned_shortcuts(entries, [game], paths.user_id):
            remove_shortcut(paths, appid)
    _unlink_steam_compatdata(game)
    _restore_compatdata_backup(game)
    game.meta.pop("steam", None)
    game.meta["mode"] = STANDALONE
    _write_manifest(game)
    for where, wanted in (("menu", menu), ("desktop", desktop)):
        if wanted:
            game = set_game_launcher(game, where, True)
    return game

