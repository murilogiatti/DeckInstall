"""Manifest-guided uninstall: plan what a game left on disk, then remove only the chosen parts.

plan_uninstall() is read-only (sizes included, so run it off the UI thread). execute_uninstall()
re-checks every path with the same safety helpers as remove_standalone_game: nothing outside the
managed roots is deleted, symlinks are never followed, and a prefix shared with another game is kept.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import library as _lib
from . import save_sync
from . import standalone as _sa
from .fsutil import is_within, prune_empty_parents, remove_path_or_tree, symlink_points_to
from .library import find_library_root
from .manifest import decode_path
from .standalone import (
    StandaloneGame,
    _is_protected_dir,
    _managed_roots,
    _prefix_used_by_other_game,
    _safe_remove_cache_dir,
    _safe_remove_prefix_dir,
    _unlink_steam_compatdata,
    game_log_dir,
    get_games_dir,
    get_legacy_games_dir,
    remove_standalone_game,
)
from .steam_paths import dir_size_bytes

# Item ids, in execution-relevant display order.
SHORTCUTS = "shortcuts"
GAME_FILES = "game_files"
SAVES = "saves_snapshot"
PREFIX = "prefix"
STEAM = "steam_link"
ICONS = "icons"
OPTISCALER = "optiscaler"
CACHE = "cache"
LOGS = "logs"

# Reasons (the dialog translates them).
REASON_REQUIRED = "required"
REASON_SHARED_PREFIX = "shared_prefix"
REASON_OUTSIDE_ROOTS = "outside_roots"
REASON_SYMLINK = "symlink"
REASON_PROTECTED = "protected"

SnapshotFn = Callable[[Path, str, str, str], "Path | None"]


@dataclass
class UninstallItem:
    """One thing the game left on disk. covered_by: removed anyway when that item is removed;
    requires: only meaningful when that item is removed."""

    id: str
    paths: list[Path]
    size_bytes: int = 0
    checked: bool = True
    locked: bool = False
    reason: str = ""
    covered_by: str = ""
    requires: str = ""


@dataclass
class UninstallPlan:
    game: StandaloneGame
    items: list[UninstallItem]

    def item(self, item_id: str) -> UninstallItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def freed_bytes(self, selected: Iterable[str]) -> int:
        """Space freed by removing the selected items (covered items are not counted twice)."""
        chosen = set(selected) | {i.id for i in self.items if i.locked and i.checked}
        total = 0
        for it in self.items:
            if it.id not in chosen or (it.locked and not it.checked):
                continue
            if it.covered_by and it.covered_by in chosen:
                continue
            if it.requires and it.requires not in chosen:
                continue
            total += it.size_bytes
        return total


@dataclass
class UninstallResult:
    freed_bytes: int = 0
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    snapshot: Path | None = None


def _file_size(path: Path) -> int:
    try:
        return path.stat(follow_symlinks=False).st_size if path.is_file() and not path.is_symlink() else 0
    except OSError:
        return 0


def _games_roots(game_dir: Path) -> list[Path]:
    roots = [get_games_dir(), get_legacy_games_dir()]
    lib_root = find_library_root(game_dir)
    if lib_root is not None:
        roots.append(lib_root / _lib.GAMES)
    return roots


def _decoded(raw: object) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return decode_path(text, None)
    except ValueError:
        return None


def _steam_link(game: StandaloneGame) -> Path | None:
    """The recorded compatdata link, only while it still points at this game's prefix."""
    link = _decoded((game.meta.get("steam") or {}).get("compatdata_link"))
    if link is None or not symlink_points_to(link, game.prefix_dir):
        return None
    return link


def _steam_backup(game: StandaloneGame, link: Path) -> Path | None:
    backup = _decoded((game.meta.get("steam") or {}).get("compatdata_backup"))
    if backup is None or backup.parent != link.parent or backup.is_symlink() or not backup.is_dir():
        return None
    return backup


def _logs_dir(game: StandaloneGame) -> Path:
    return _decoded((game.meta.get("logs") or {}).get("dir")) or game_log_dir(game)


def _in_game_dir(game: StandaloneGame, raw: Iterable[object]) -> list[Path]:
    out: list[Path] = []
    for entry in raw:
        p = entry if isinstance(entry, Path) else game.game_dir / str(entry)
        if is_within(p, game.game_dir, strict=True) and (p.is_file() or p.is_dir()):
            out.append(p)
    return out


def plan_uninstall(game: StandaloneGame) -> UninstallPlan:
    """List what removing game would touch, with sizes and what must be kept (and why)."""
    items: list[UninstallItem] = []

    shortcut_paths = [p for p in (game.desktop_file, game.desktop_shortcut, game.launch_script) if p is not None]
    items.append(
        UninstallItem(
            SHORTCUTS,
            [p for p in shortcut_paths if p.exists() or p.is_symlink()],
            sum(_file_size(p) for p in shortcut_paths),
            checked=True,
            locked=True,
            reason=REASON_REQUIRED,
        )
    )

    gd = game.game_dir
    if gd.is_dir() or gd.is_symlink():
        game_item = UninstallItem(GAME_FILES, [gd])
        if gd.is_symlink():
            game_item.checked, game_item.locked, game_item.reason = False, True, REASON_SYMLINK
        elif _is_protected_dir(gd):
            game_item.checked, game_item.locked, game_item.reason = False, True, REASON_PROTECTED
        else:
            game_item.size_bytes = dir_size_bytes(gd)
            # Folders the user installed in place (outside the libraries) start unchecked.
            game_item.checked = any(is_within(gd, r, strict=True) for r in _games_roots(gd))
        items.append(game_item)

    pfx = game.prefix_dir
    if not game.native_linux and (pfx.is_dir() or pfx.is_symlink()):
        prefix_item = UninstallItem(PREFIX, [pfx])
        if pfx.is_symlink():
            prefix_item.checked, prefix_item.locked, prefix_item.reason = False, True, REASON_SYMLINK
        elif not any(is_within(pfx, r, strict=True) for r in _managed_roots(_lib.PREFIXES, gd)):
            prefix_item.checked, prefix_item.locked, prefix_item.reason = False, True, REASON_OUTSIDE_ROOTS
        elif _prefix_used_by_other_game(pfx, gd):
            prefix_item.checked, prefix_item.locked, prefix_item.reason = False, True, REASON_SHARED_PREFIX
        else:
            prefix_item.size_bytes = dir_size_bytes(pfx)
        if not prefix_item.locked:
            items.append(UninstallItem(SAVES, [pfx], requires=PREFIX))
        items.append(prefix_item)

    link = _steam_link(game)
    if link is not None:
        paths = [link]
        backup = _steam_backup(game, link)
        if backup is not None:
            paths.append(backup)
        prefix_planned = PREFIX in {i.id for i in items if not i.locked}
        items.append(UninstallItem(STEAM, paths, checked=prefix_planned))

    icons = _in_game_dir(game, game.meta.get("icons") or [])
    if icons:
        items.append(UninstallItem(ICONS, icons, sum(_file_size(p) for p in icons), covered_by=GAME_FILES))

    opti = game.meta.get("optiscaler") or {}
    opti_files = _in_game_dir(game, opti.get("files") or [])
    if opti_files:
        items.append(
            UninstallItem(OPTISCALER, opti_files, sum(_file_size(p) for p in opti_files), covered_by=GAME_FILES)
        )

    cache = game.cache_dir
    if cache is not None and (cache.is_dir() or cache.is_symlink()):
        cache_item = UninstallItem(CACHE, [cache])
        if cache.is_symlink() or not any(is_within(cache, r, strict=True) for r in _managed_roots(_lib.CACHES, gd)):
            cache_item.checked, cache_item.locked, cache_item.reason = False, True, REASON_OUTSIDE_ROOTS
        else:
            cache_item.size_bytes = dir_size_bytes(cache)
        items.append(cache_item)

    logs = _logs_dir(game)
    if logs.is_dir() or logs.is_symlink():
        logs_item = UninstallItem(LOGS, [logs])
        if not is_within(logs, _sa.game_logs_root(), strict=True):
            logs_item.checked, logs_item.locked, logs_item.reason = False, True, REASON_OUTSIDE_ROOTS
        else:
            logs_item.size_bytes = dir_size_bytes(logs)
        items.append(logs_item)

    return UninstallPlan(game, items)


def _gone(paths: Iterable[Path]) -> bool:
    return all(not p.exists() and not p.is_symlink() for p in paths)


def execute_uninstall(
    plan: UninstallPlan,
    selected_ids: Iterable[str],
    snapshot_fn: SnapshotFn | None = None,
) -> UninstallResult:
    """Remove the selected (unlocked) items of plan; the game is always unregistered."""
    game = plan.game
    chosen = {i.id for i in plan.items if not i.locked and i.id in set(selected_ids)}
    result = UninstallResult()

    def done(item: UninstallItem, ok: bool) -> None:
        if ok:
            result.removed.append(item.id)
            result.freed_bytes += item.size_bytes
        else:
            result.skipped.append(item.id)

    for item in plan.items:
        if item.locked and item.id != SHORTCUTS:
            result.skipped.append(item.id)

    prefix_item = plan.item(PREFIX)
    if prefix_item is not None and PREFIX in chosen and SAVES in chosen:
        snap = snapshot_fn or save_sync.auto_snapshot
        try:
            result.snapshot = snap(game.prefix_dir, game.name, "remove-game", "remove")
            result.removed.append(SAVES)
        except Exception as exc:
            # No snapshot, no prefix deletion: the saves live there.
            logging.warning("Save snapshot failed before uninstalling %s: %s", game.name, exc)
            result.errors.append(f"{SAVES}: {exc}")
            chosen.discard(PREFIX)

    opti_item = plan.item(OPTISCALER)
    if opti_item is not None and OPTISCALER in chosen and GAME_FILES not in chosen:
        try:
            from .optiscaler import uninstall_optiscaler_for_game

            uninstall_optiscaler_for_game(game.exe_path)
            done(opti_item, _gone(opti_item.paths))
        except Exception as exc:
            result.errors.append(f"{OPTISCALER}: {exc}")

    icons_item = plan.item(ICONS)
    if icons_item is not None and ICONS in chosen and GAME_FILES not in chosen:
        for p in icons_item.paths:
            if is_within(p, game.game_dir, strict=True):
                p.unlink(missing_ok=True)
        done(icons_item, _gone(icons_item.paths))

    shortcuts_item = plan.item(SHORTCUTS)
    try:
        remove_standalone_game(game, delete_files=False, delete_prefix=False, delete_cache=False)
        if shortcuts_item is not None:
            done(shortcuts_item, True)
    except Exception as exc:
        result.errors.append(f"{SHORTCUTS}: {exc}")

    game_item = plan.item(GAME_FILES)
    if game_item is not None and GAME_FILES in chosen:
        gd = game.game_dir
        if gd.is_symlink() or _is_protected_dir(gd):
            done(game_item, False)
        else:
            remove_path_or_tree(gd)
            prune_empty_parents(gd, _games_roots(gd))
            done(game_item, _gone([gd]))
            for covered in (opti_item, icons_item):
                if covered is not None and covered.id in chosen:
                    result.removed.append(covered.id)

    prefix_removed = False
    if prefix_item is not None and PREFIX in chosen:
        # Re-check: another game may have adopted this prefix since the plan was made.
        if _prefix_used_by_other_game(game.prefix_dir, game.game_dir):
            done(prefix_item, False)
        else:
            _unlink_steam_compatdata(game)
            _safe_remove_prefix_dir(game.prefix_dir, game.game_dir)
            prefix_removed = _gone([game.prefix_dir])
            done(prefix_item, prefix_removed)

    steam_item = plan.item(STEAM)
    if steam_item is not None and (STEAM in chosen or prefix_removed):
        link = steam_item.paths[0]
        _unlink_steam_compatdata(game)
        unlinked = not link.exists() and not link.is_symlink()
        backup = steam_item.paths[1] if len(steam_item.paths) > 1 else None
        if unlinked and backup is not None and STEAM in chosen and backup.is_dir() and not backup.is_symlink():
            try:
                os.replace(backup, link)
            except OSError as exc:
                result.errors.append(f"{STEAM}: {exc}")
        if STEAM in chosen:
            done(steam_item, unlinked)

    cache_item = plan.item(CACHE)
    if cache_item is not None and CACHE in chosen:
        _safe_remove_cache_dir(game.cache_dir, game.game_dir)
        done(cache_item, _gone(cache_item.paths))

    logs_item = plan.item(LOGS)
    if logs_item is not None and LOGS in chosen:
        logs = logs_item.paths[0]
        if is_within(logs, _sa.game_logs_root(), strict=True):
            remove_path_or_tree(logs)
        done(logs_item, _gone([logs]))

    return result
