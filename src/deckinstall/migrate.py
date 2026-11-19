"""Migrate schema-v1 games (~/Games layout) into the internal library, with a journal to undo it.

For each v1 game the migration:
  1. snapshots the prefix saves (ZIP, see save_sync.auto_snapshot);
  2. moves the game folder (only when it lives under ~/Games), its prefix and its shader cache
     into ~/DeckInstall/{Games,Prefixes/<id>,Caches/<id>};
  3. rewrites launch.sh and the .desktop launchers, writes the v2 manifest and registers the game.

Every step is written to a journal before it runs. A failure undoes that game at once, and
undo_migration() puts every migrated game back as it was (files moved back, original
launch.sh/.desktop/manifest restored). Games referenced by a Steam shortcut are skipped,
because moving them would break the shortcut.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import library as _lib
from . import save_sync, standalone
from .extractor import check_disk_space
from .fsutil import atomic_write_text, is_within, move_dir_safely
from .manifest import MANIFEST_V1, MANIFEST_V2, new_uid, now_iso
from .standalone import StandaloneGame
from .steam_paths import SteamPaths, dir_size_bytes

JOURNAL_SCHEMA = 1


@dataclass
class MigrationItem:
    """One v1 game and what the migration will do with it."""

    game: StandaloneGame
    action: str  # "migrate" or "skip"
    reason: str = ""
    new_game_dir: Path | None = None
    moves: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass
class MigrationResult:
    journal: Path | None
    migrated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def journal_dir() -> Path:
    """Folder holding migration journals (next to the library registry)."""
    return _lib.registry_path().parent / "migrations"


def _shortcut_paths(paths: SteamPaths | None) -> list[str]:
    if paths is None:
        return []
    from .steam_vdf import _all_user_shortcuts, _strip_vdf_quotes

    try:
        _, entries = _all_user_shortcuts(paths)
    except Exception as exc:
        logging.warning("Could not read Steam shortcuts for migration: %s", exc)
        return []
    out: list[str] = []
    for e in entries:
        for raw in (e.exe, e.start_dir, e.icon):
            val = _strip_vdf_quotes(raw)
            if val:
                out.append(os.path.abspath(os.path.expanduser(val)))
        if e.launch_options:
            out.append(e.launch_options)
    return out


def _referenced(path: Path, refs: list[str]) -> bool:
    key = os.path.abspath(path)
    return any(r == key or r.startswith(key + os.sep) or key in r for r in refs)


def _unique_dir(target: Path) -> Path:
    if not target.exists() and not target.is_symlink():
        return target
    idx = 2
    while True:
        cand = target.with_name(f"{target.name} ({idx})")
        if not cand.exists() and not cand.is_symlink():
            return cand
        idx += 1


def plan_migration(paths: SteamPaths | None = None) -> list[MigrationItem]:
    """List every v1 game and whether it can be migrated (no changes are made)."""
    if not standalone.uses_library_layout():
        return []
    lib_root = _lib.internal_library_root()
    games_dir = lib_root / _lib.GAMES
    legacy_games = standalone.get_legacy_games_dir()
    legacy_prefixes = standalone.get_legacy_prefixes_dir()
    legacy_caches = legacy_games / ".caches"
    refs = _shortcut_paths(paths)
    v1 = [g for g in standalone.list_standalone_games() if g.schema < 2]
    prefix_users: dict[Path, int] = {}
    for g in v1:
        try:
            key = g.prefix_dir.resolve()
        except OSError:
            continue
        prefix_users[key] = prefix_users.get(key, 0) + 1

    items: list[MigrationItem] = []
    planned_dirs: set[Path] = set()
    for game in v1:
        if _referenced(game.game_dir, refs) or _referenced(game.prefix_dir, refs):
            items.append(MigrationItem(game, "skip", "steam_shortcut"))
            continue
        if game.game_dir.is_symlink() or game.prefix_dir.is_symlink():
            items.append(MigrationItem(game, "skip", "symlink"))
            continue
        item = MigrationItem(game, "migrate")
        if is_within(game.game_dir, legacy_games, strict=True):
            rel = game.game_dir.resolve().relative_to(legacy_games.resolve())
            target = _unique_dir(games_dir / rel)
            while target in planned_dirs:
                target = _unique_dir(target.with_name(target.name + " (migrated)"))
            planned_dirs.add(target)
            item.new_game_dir = target
            item.moves.append((game.game_dir, target))
        else:
            item.new_game_dir = game.game_dir  # kept in place by the user
        try:
            shared = prefix_users.get(game.prefix_dir.resolve(), 0) > 1
        except OSError:
            shared = True
        if game.prefix_dir.is_dir() and is_within(game.prefix_dir, legacy_prefixes, strict=True) and not shared:
            item.moves.append((game.prefix_dir, lib_root / _lib.PREFIXES / "<uid>"))
        if (
            game.cache_dir is not None
            and game.cache_dir.is_dir()
            and is_within(game.cache_dir, legacy_caches, strict=True)
        ):
            item.moves.append((game.cache_dir, lib_root / _lib.CACHES / "<uid>"))
        items.append(item)
    return items


class _Journal:
    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")


def _new_journal(lib_root: Path) -> _Journal:
    folder = journal_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}.json"
    idx = 2
    while path.exists():
        path = folder / f"{stamp}-{idx}.json"
        idx += 1
    return _Journal(path, {
        "schema": JOURNAL_SCHEMA,
        "created_at": now_iso(),
        "library_root": str(lib_root),
        "undone": False,
        "games": [],
    })


def _read_text(path: Path) -> str | None:
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"Migration target already exists: {dst}")
    try:
        same_fs = src.stat().st_dev == dst.parent.stat().st_dev
    except OSError:
        same_fs = False
    if not same_fs:
        check_disk_space(dst.parent, dir_size_bytes(src))
    move_dir_safely(src, dst)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    cur = path
    while cur != stop and is_within(cur, stop, strict=True):
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def _undo_entry(entry: dict[str, Any]) -> list[str]:
    """Undo one game's journal entry. Returns problems that need the user's attention."""
    problems: list[str] = []
    lib_root = Path(str(entry.get("library_root") or ""))
    uid = str(entry.get("uid") or "")
    if uid and str(lib_root):
        try:
            _lib.forget_game(lib_root, uid)
        except OSError as exc:
            problems.append(f"library.json: {exc}")
    for raw in reversed(entry.get("created") or []):
        p = Path(str(raw))
        try:
            if p.is_file() and not p.is_symlink():
                p.unlink()
        except OSError as exc:
            problems.append(f"{p}: {exc}")
    for move in reversed(entry.get("moves") or []):
        src, dst = Path(str(move["src"])), Path(str(move["dst"]))
        if not move.get("done"):
            continue
        if not dst.exists():
            problems.append(f"missing {dst} (expected to move back to {src})")
            continue
        if src.exists() or src.is_symlink():
            problems.append(f"{src} already exists; left {dst} in place")
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            move_dir_safely(dst, src)
            _remove_empty_parents(dst.parent, Path(str(move.get("dst_stop") or dst.parent)))
        except OSError as exc:
            problems.append(f"{dst} -> {src}: {exc}")
    for raw_path, content in (entry.get("files") or {}).items():
        p = Path(raw_path)
        try:
            if content is None:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
            else:
                atomic_write_text(p, content)
        except OSError as exc:
            problems.append(f"{p}: {exc}")
    return problems


def _migrate_one(item: MigrationItem, journal: _Journal, lib: _lib.Library) -> StandaloneGame:
    game = item.game
    uid = new_uid()
    entry: dict[str, Any] = {
        "name": game.name,
        "uid": uid,
        "library_root": str(lib.root),
        "status": "running",
        "snapshot": "",
        "files": {},
        "moves": [],
        "created": [],
    }
    journal.data["games"].append(entry)

    # 1. Save snapshot before touching anything.
    if game.prefix_dir.is_dir() and not game.native_linux:
        snap = save_sync.auto_snapshot(
            game.prefix_dir,
            game.name,
            "migration",
            game_dir=game.game_dir,
            steam_appid=game.steam_appid,
        )
        entry["snapshot"] = str(snap) if snap is not None else ""

    # 2. Remember the files that will be rewritten (restored on undo).
    index_entry = standalone._library_index_path(game.game_dir)
    originals = [game.game_dir / MANIFEST_V1, game.launch_script, game.desktop_file, index_entry]
    if game.desktop_shortcut is not None:
        originals.append(game.desktop_shortcut)
    for p in originals:
        content = _read_text(p)
        if content is not None:  # absent files are never deleted on undo
            entry["files"][str(p)] = content
    journal.save()

    # 3. Move folders, journaling each move before it runs.
    new_game_dir = item.new_game_dir or game.game_dir
    new_prefix = lib.prefixes_dir / uid
    new_cache = lib.caches_dir / uid
    for src, planned in item.moves:
        if planned.name == "<uid>":
            dst = new_prefix if planned.parent.name == _lib.PREFIXES else new_cache
            dst_stop = dst.parent
        else:
            dst = planned
            dst_stop = lib.games_dir
        move = {"src": str(src), "dst": str(dst), "dst_stop": str(dst_stop), "done": False}
        entry["moves"].append(move)
        journal.save()
        _move(src, dst)
        move["done"] = True
        journal.save()
        _remove_empty_parents(src.parent, standalone.get_legacy_games_dir())

    moved_prefix = any(Path(m["dst"]) == new_prefix for m in entry["moves"])
    moved_cache = any(Path(m["dst"]) == new_cache for m in entry["moves"])

    keep_new_cache = moved_cache or game.cache_dir is None or not game.cache_dir.is_dir()

    def rebase(p: Path) -> Path:
        return new_game_dir / p.relative_to(game.game_dir) if is_within(p, game.game_dir) else p

    migrated = StandaloneGame(
        name=game.name,
        game_dir=new_game_dir,
        exe_path=rebase(game.exe_path),
        prefix_dir=new_prefix if (moved_prefix or game.native_linux) else game.prefix_dir,
        proton_name=game.proton_name,
        steam_appid=game.steam_appid,
        winedlloverrides=game.winedlloverrides,
        launch_options=game.launch_options,
        launch_script=rebase(game.launch_script),
        desktop_file=game.desktop_file,
        desktop_shortcut=game.desktop_shortcut,
        native_linux=game.native_linux,
        cache_dir=new_cache if keep_new_cache else game.cache_dir,
        uid=uid,
        schema=2,
        meta={
            "created_at": now_iso(),
            "source": {"kind": "migrated", "origin": str(game.game_dir)},
            "proton_mode": "pinned" if game.proton_name.strip() else "default",
            "prefix_location": "library",
            "icons": [],
        },
    )

    # 4. New integration files, manifest and registration.
    new_v1 = new_game_dir / MANIFEST_V1
    new_v2 = new_game_dir / MANIFEST_V2
    if new_game_dir != game.game_dir:
        entry["files"][str(new_v1)] = None  # moved copy of the v1 manifest; restored at the old path
    entry["created"].extend([str(new_v2), str(standalone._library_index_path(new_game_dir))])
    if migrated.launch_script != game.launch_script:
        entry["created"].append(str(migrated.launch_script))
    journal.save()
    standalone.refresh_integration_files(migrated)
    manifest_path = standalone._write_manifest(migrated)
    if new_v1.is_file() and not new_v1.is_symlink():
        new_v1.unlink()
    if index_entry.is_file() and not index_entry.is_symlink():
        index_entry.unlink()
    standalone._register_standalone_game(migrated, manifest_path)
    if is_within(new_game_dir, lib.games_dir, strict=True):
        _lib.record_game(lib.root, uid, migrated.name, new_game_dir)
    entry["status"] = "done"
    journal.save()
    return migrated


def migrate_legacy_games(
    items: list[MigrationItem] | None = None,
    paths: SteamPaths | None = None,
) -> MigrationResult:
    """Migrate the planned v1 games into the internal library. Safe to call when nothing is pending."""
    plan = items if items is not None else plan_migration(paths)
    result = MigrationResult(journal=None)
    todo = [i for i in plan if i.action == "migrate"]
    result.skipped = [(i.game.name, i.reason) for i in plan if i.action != "migrate"]
    if not todo:
        return result
    lib = _lib.ensure_library(_lib.internal_library_root(), internal=True)
    if not lib.unix_fs:
        raise RuntimeError(f"The internal library is on {lib.fs_type}, which cannot hold Wine prefixes.")
    journal = _new_journal(lib.root)
    result.journal = journal.path
    for item in todo:
        try:
            _migrate_one(item, journal, lib)
            result.migrated.append(item.game.name)
        except Exception as exc:
            logging.exception("Migration of %s failed; undoing it", item.game.name)
            entry = journal.data["games"][-1] if journal.data["games"] else None
            problems = _undo_entry(entry) if entry is not None else []
            if entry is not None:
                entry["status"] = "rolled_back" if not problems else "rollback_incomplete"
                entry["error"] = str(exc)
                entry["problems"] = problems
            journal.save()
            msg = str(exc) if not problems else f"{exc} (undo problems: {'; '.join(problems)})"
            result.failed.append((item.game.name, msg))
    journal.save()
    return result


def list_journals() -> list[Path]:
    """Return migration journals, newest first."""
    folder = journal_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"), reverse=True)


def latest_undoable_journal() -> Path | None:
    """Return the newest journal that still has migrated games to undo."""
    for path in list_journals():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("undone") and any(g.get("status") == "done" for g in data.get("games") or []):
            return path
    return None


def undo_migration(journal_path: Path) -> tuple[list[str], list[str]]:
    """Put back every game migrated by journal_path. Returns (restored names, problems)."""
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != JOURNAL_SCHEMA:
        raise ValueError(f"Unsupported migration journal: {journal_path}")
    journal = _Journal(journal_path, data)
    restored: list[str] = []
    problems: list[str] = []
    for entry in reversed(data.get("games") or []):
        if entry.get("status") != "done":
            continue
        issues = _undo_entry(entry)
        entry["status"] = "undone" if not issues else "undo_incomplete"
        entry["problems"] = issues
        if issues:
            problems.extend(f"{entry.get('name')}: {p}" for p in issues)
        else:
            restored.append(str(entry.get("name")))
        journal.save()
    data["undone"] = True
    data["undone_at"] = now_iso()
    journal.save()
    return restored, problems
