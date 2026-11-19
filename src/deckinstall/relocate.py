"""Move a library game to another disk's library (for example internal storage to a microSD).

plan_relocation() is read-only (it measures sizes, so run it off the UI thread).
relocate_game() moves the game folder, and the prefix and shader cache when the target
filesystem can hold them, with the same safe-move helper the adoption wizard uses (rename on the
same filesystem, copy-then-delete across disks), then rewrites launch.sh/.desktop launchers and
the v2 manifest, re-registers the game and repoints Steam's compatdata link. Any failure undoes
every step already done.

exFAT/FAT/NTFS targets cannot hold Wine prefixes: the game folder moves, the prefix and cache
stay where they are (W_PREFIX_STAYS).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import library as _lib
from . import standalone as _sa
from .adopt import SnapshotFn, _default_snapshot, _move_back, _move_tree
from .fsutil import atomic_write_text, is_within
from .library import Library, find_library_root
from .manifest import decode_path, find_manifest
from .migrate import _unique_dir
from .standalone import StandaloneGame
from .steam_paths import dir_size_bytes

# Blockers: the move cannot run.
B_LEGACY = "legacy_schema"
B_UNAVAILABLE = "unavailable"
B_SAME_LIBRARY = "same_library"
B_SYMLINK = "symlink"
B_PROTECTED = "protected"
B_SHARED_PREFIX = "shared_prefix"
B_NO_SPACE = "no_space"
# Warnings: the move runs, the user is told.
W_PREFIX_STAYS = "prefix_stays_internal"
W_STEAM_SHORTCUT = "steam_shortcut"

_SPACE_MARGIN = 1.1

ProgressFn = Callable[[str], None]


class RelocateError(RuntimeError):
    """The move failed. problems lists undo steps that failed too (empty: fully rolled back)."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []

    @property
    def rolled_back(self) -> bool:
        return not self.problems


@dataclass
class RelocatePlan:
    game: StandaloneGame
    target: Library
    new_game_dir: Path
    new_prefix_dir: Path | None = None  # None: the prefix stays where it is
    new_cache_dir: Path | None = None
    game_bytes: int = 0
    prefix_bytes: int = 0
    cache_bytes: int = 0
    cross_device_bytes: int = 0  # bytes that must be copied (a same-filesystem move is a rename)
    free_bytes: int = 0
    same_fs: bool = True
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return self.game_bytes + self.prefix_bytes + self.cache_bytes

    @property
    def can_run(self) -> bool:
        return not self.blockers


def target_libraries(game: StandaloneGame, libraries: list[Library] | None = None) -> list[Library]:
    """Available libraries the game could move to (every one except the library it already lives in)."""
    libs = libraries if libraries is not None else _lib.discover_libraries()
    current = find_library_root(game.game_dir)
    out: list[Library] = []
    for lib in libs:
        if not lib.available:
            continue
        if current is not None and _same_path(lib.root, current):
            continue
        out.append(lib)
    return out


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _dev(path: Path) -> int | None:
    try:
        return os.stat(_existing(path)).st_dev
    except OSError:
        return None


def _managed(path: Path, sub: str, game_dir: Path) -> bool:
    return any(is_within(path, r, strict=True) for r in _sa._managed_roots(sub, game_dir))


def plan_relocation(game: StandaloneGame, target: Library) -> RelocatePlan:
    """Work out what moving game to target involves, without changing anything."""
    blockers: list[str] = []
    warnings: list[str] = []
    gd = game.game_dir
    if game.schema < 2 or not game.uid:
        blockers.append(B_LEGACY)
    if not target.available:
        blockers.append(B_UNAVAILABLE)
    src_root = find_library_root(gd)
    if src_root is not None and _same_path(src_root, target.root):
        blockers.append(B_SAME_LIBRARY)
    if gd.is_symlink() or game.prefix_dir.is_symlink():
        blockers.append(B_SYMLINK)
    if _sa._is_protected_dir(gd):
        blockers.append(B_PROTECTED)

    plan = RelocatePlan(game, target, _unique_dir(target.games_dir / gd.name))

    # The prefix and the cache move only when they live in a library folder and the target can hold them.
    prefix = game.prefix_dir
    if not game.native_linux and prefix.is_dir() and not is_within(prefix, gd):
        if _managed(prefix, _lib.PREFIXES, gd) and not is_within(prefix, target.prefixes_dir):
            if not target.unix_fs:
                warnings.append(W_PREFIX_STAYS)
            elif _sa._prefix_used_by_other_game(prefix, gd):
                blockers.append(B_SHARED_PREFIX)
            else:
                plan.new_prefix_dir = target.prefixes_dir / game.uid
    cache = game.cache_dir
    if (
        cache is not None
        and target.unix_fs
        and cache.is_dir()
        and not cache.is_symlink()
        and _managed(cache, _lib.CACHES, gd)
        and not is_within(cache, target.caches_dir)
    ):
        plan.new_cache_dir = target.caches_dir / game.uid
    if (game.meta.get("steam") or {}).get("shortcut_appid"):
        warnings.append(W_STEAM_SHORTCUT)

    plan.game_bytes = dir_size_bytes(gd)
    if plan.new_prefix_dir is not None:
        plan.prefix_bytes = dir_size_bytes(prefix)
    if plan.new_cache_dir is not None and cache is not None:
        plan.cache_bytes = dir_size_bytes(cache)

    target_dev = _dev(target.root)
    moves = [(gd, plan.game_bytes, plan.new_game_dir)]
    if plan.new_prefix_dir is not None:
        moves.append((prefix, plan.prefix_bytes, plan.new_prefix_dir))
    if plan.new_cache_dir is not None and cache is not None:
        moves.append((cache, plan.cache_bytes, plan.new_cache_dir))
    plan.same_fs = _dev(gd) == target_dev and target_dev is not None
    plan.cross_device_bytes = sum(size for src, size, _dst in moves if _dev(src) != target_dev or target_dev is None)
    try:
        plan.free_bytes = shutil.disk_usage(_existing(target.root)).free
    except OSError:
        plan.free_bytes = 0
    if plan.cross_device_bytes and plan.free_bytes < int(plan.cross_device_bytes * _SPACE_MARGIN):
        blockers.append(B_NO_SPACE)
    plan.warnings, plan.blockers = warnings, blockers
    return plan


def _read_text(path: Path) -> str | None:
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def _restore_files(saved: dict[Path, str | None]) -> Callable[[], None]:
    def fn() -> None:
        for path, content in saved.items():
            if content is None:
                continue
            atomic_write_text(path, content)

    return fn


def _repoint_compat_link(link: Path, old_prefix: Path, new_prefix: Path) -> bool:
    """Retarget Steam's compatdata symlink from old_prefix to new_prefix. False if it is not ours."""
    if not link.is_symlink():
        return False
    try:
        current = Path(os.readlink(link))
        if not current.is_absolute():
            current = link.parent / current
    except OSError:
        return False
    if os.path.abspath(current) != os.path.abspath(old_prefix):
        return False
    tmp = link.with_name(f".{link.name}.deckinstall.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(new_prefix)
    os.replace(tmp, link)
    return True


def _follow_compat_link(
    link_raw: str, old_prefix: Path, new_prefix: Path, undo: list[tuple[str, Callable[[], None]]]
) -> None:
    try:
        link = decode_path(link_raw, None)
    except ValueError:
        return
    if _repoint_compat_link(link, old_prefix, new_prefix):

        def back() -> None:
            _repoint_compat_link(link, new_prefix, old_prefix)

        undo.append(("repoint compatdata link", back))


def _run_undo(steps: list[tuple[str, Callable[[], None]]]) -> list[str]:
    problems: list[str] = []
    for label, fn in reversed(steps):
        try:
            fn()
        except Exception as exc:
            logging.warning("Relocation undo step %r failed: %s", label, exc)
            problems.append(f"{label}: {exc}")
    return problems


def relocate_game(
    plan: RelocatePlan,
    progress: ProgressFn | None = None,
    snapshot_fn: SnapshotFn | None = None,
) -> StandaloneGame:
    """Run plan. Returns the game at its new location; raises RelocateError (everything undone) on failure."""
    if plan.blockers:
        raise RelocateError(f"Cannot move this game: {', '.join(plan.blockers)}")
    game, target = plan.game, plan.target
    old_dir, new_dir = game.game_dir, plan.new_game_dir
    if new_dir.exists() or new_dir.is_symlink():
        raise RelocateError(f"Target already exists: {new_dir}")
    say = progress or (lambda _step: None)
    if plan.new_prefix_dir is not None and not game.native_linux and game.prefix_dir.is_dir():
        say("snapshot")
        try:
            (snapshot_fn or _default_snapshot)(game.prefix_dir, game.name, "relocate", "relocate")
        except Exception as exc:
            raise RelocateError(f"Save snapshot failed, nothing was changed: {exc}") from exc
    undo: list[tuple[str, Callable[[], None]]] = []
    src_root = find_library_root(old_dir)

    try:
        # Files rewritten below keep their old content so a failure restores them.
        index_old = _sa._library_index_path(old_dir)
        manifest_old = find_manifest(old_dir)
        saved: dict[Path, str | None] = {
            p: _read_text(p)
            for p in (manifest_old, game.launch_script, game.desktop_file, game.desktop_shortcut, index_old)
            if p is not None
        }
        undo.append(("restore files", _restore_files(saved)))

        say("game")
        target.games_dir.mkdir(parents=True, exist_ok=True)
        _move_tree(old_dir, new_dir, target.root)
        undo.append((f"move {new_dir} back", _move_back(old_dir, new_dir)))

        new_prefix = game.prefix_dir
        if plan.new_prefix_dir is not None:
            say("prefix")
            plan.new_prefix_dir.parent.mkdir(parents=True, exist_ok=True)
            _move_tree(game.prefix_dir, plan.new_prefix_dir, target.root)
            undo.append((f"move {plan.new_prefix_dir} back", _move_back(game.prefix_dir, plan.new_prefix_dir)))
            new_prefix = plan.new_prefix_dir
        new_cache = game.cache_dir
        if plan.new_cache_dir is not None and game.cache_dir is not None:
            say("cache")
            plan.new_cache_dir.parent.mkdir(parents=True, exist_ok=True)
            _move_tree(game.cache_dir, plan.new_cache_dir, target.root)
            undo.append((f"move {plan.new_cache_dir} back", _move_back(game.cache_dir, plan.new_cache_dir)))
            new_cache = plan.new_cache_dir

        def rebase(p: Path) -> Path:
            return new_dir / p.relative_to(old_dir) if is_within(p, old_dir) else p

        meta: dict[str, Any] = dict(game.meta)
        if meta.get("icons"):
            meta["icons"] = [rebase(Path(p)) for p in meta["icons"]]
        if meta.get("launch_script_backup"):
            meta["launch_script_backup"] = rebase(Path(meta["launch_script_backup"]))
        moved = dataclasses.replace(
            game,
            game_dir=new_dir,
            exe_path=rebase(game.exe_path),
            launch_script=rebase(game.launch_script),
            prefix_dir=new_prefix,
            cache_dir=new_cache,
            meta=meta,
        )

        say("integration")
        # Steam's compatdata link follows the prefix (the shortcut itself is only warned about).
        link_raw = str((game.meta.get("steam") or {}).get("compatdata_link") or "")
        if plan.new_prefix_dir is not None and link_raw:
            _follow_compat_link(link_raw, game.prefix_dir, plan.new_prefix_dir, undo)
        undo.append(("remove new index", lambda: _sa._library_index_path(new_dir).unlink(missing_ok=True)))
        _sa.refresh_integration_files(moved)
        manifest_path = _sa._write_manifest(moved)
        index_old.unlink(missing_ok=True)
        _sa._register_standalone_game(moved, manifest_path)

        if is_within(new_dir, target.games_dir, strict=True):
            _lib.record_game(target.root, moved.uid, moved.name, new_dir)
            undo.append(("forget in target", lambda: _lib.forget_game(target.root, moved.uid)))
        if src_root is not None:
            _lib.forget_game(src_root, game.uid)
            undo.append(("record in source", lambda: _lib.record_game(src_root, game.uid, game.name, old_dir)))
        return moved
    except Exception as exc:
        logging.exception("Relocating %s failed; undoing it", game.name)
        raise RelocateError(str(exc), _run_undo(undo)) from exc

