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
    find_game_launchers,
    match_owned_shortcuts,
    set_game_launcher,
)
from .steam_paths import SteamPaths
from .steam_vdf import _other_steam_user_has_shortcut, load_shortcuts, remove_shortcut


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
    had = {where: bool(found) for where, found in find_game_launchers(game).items()}
    prev_meta = dict(game.meta)
    add_installed_game_to_steam(paths, game.game_dir, game.uid)
    try:
        game = _reload(game)
        # Remembered so switching back recreates the same launchers.
        game.meta["standalone_launchers"] = {"menu": had.get("menu", False), "desktop": had.get("desktop", False)}
        for where in ("menu", "desktop"):
            game = set_game_launcher(game, where, False)
        game.meta["mode"] = STEAM
        _write_manifest(game)
        return game
    except BaseException:
        # Roll back the Steam shortcut, compatdata link and launchers on mid-switch failure.
        try:
            current = _reload(game)
        except Exception:
            current = game
        try:
            if paths.shortcuts_vdf.is_file():
                entries = load_shortcuts(paths.shortcuts_vdf, strict=False)
                for appid in match_owned_shortcuts(entries, [current], paths.user_id):
                    remove_shortcut(paths, appid)
        except Exception as rb_exc:
            logging.warning("Rollback failed removing Steam shortcut for %s: %s", game.name, rb_exc)
        try:
            _unlink_steam_compatdata(current)
            _restore_compatdata_backup(current)
        except Exception as rb_exc:
            logging.warning("Rollback failed restoring compatdata for %s: %s", game.name, rb_exc)
        try:
            current.meta = prev_meta
            _write_manifest(current)
            for where, was_present in had.items():
                if was_present:
                    current = set_game_launcher(current, where, True)
        except Exception as rb_exc:
            logging.warning("Rollback failed restoring launchers for %s: %s", game.name, rb_exc)
        raise


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
    paths: SteamPaths | None, game: StandaloneGame, *, menu: bool = True, desktop: bool | None = None,
) -> StandaloneGame:
    """DeckInstall runs the game from now on: remove its Steam shortcuts and compatdata link,
    then create the chosen launchers. ``paths`` may be None when Steam is gone.

    desktop=None: recreate the Desktop launcher only if the game had one before it went to Steam.
    """
    _require_managed(game)
    if desktop is None:
        desktop = bool((game.meta.get("standalone_launchers") or {}).get("desktop"))
    removed_appids: list[int] = []
    if paths is not None and paths.shortcuts_vdf.is_file():
        entries = load_shortcuts(paths.shortcuts_vdf, strict=True)
        for appid in match_owned_shortcuts(entries, [game], paths.user_id):
            remove_shortcut(paths, appid)
            removed_appids.append(appid)
    shared_with_other_account = paths is not None and any(
        _other_steam_user_has_shortcut(paths, aid) is not False for aid in removed_appids
    )
    if not shared_with_other_account:
        _unlink_steam_compatdata(game)
        _restore_compatdata_backup(game)
    game.meta.pop("steam", None)
    game.meta.pop("standalone_launchers", None)
    game.meta["mode"] = STANDALONE
    _write_manifest(game)
    for where, wanted in (("menu", menu), ("desktop", desktop)):
        if wanted:
            game = set_game_launcher(game, where, True)
    return game

