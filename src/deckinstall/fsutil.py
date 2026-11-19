"""Shared filesystem helpers: atomic writes, hashing, slugs, containment checks, copy/move."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to a unique temp file next to path, fsync, then atomically replace path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically (see atomic_write_bytes)."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy_file(src: Path, dst: Path) -> None:
    """Atomically copy src to dst via a temporary file in dst.parent (metadata preserved)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file."""
    with path.open("rb") as fh:
        file_digest = getattr(hashlib, "file_digest", None)
        if file_digest is not None:
            return str(file_digest(fh, "sha256").hexdigest())
        h = hashlib.sha256()
        while chunk := fh.read(65536):
            h.update(chunk)
        return h.hexdigest()


def short_hash(value: str, length: int = 8) -> str:
    """Return the first `length` hex chars of the SHA-256 of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def sanitize_dir_name(name: str) -> str:
    """Return a filesystem-safe folder name (illegal characters replaced by spaces)."""
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip() or "Game"


def slugify(name: str) -> str:
    """Return a lowercase hyphenated slug (used for .desktop filenames)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "game"


def is_within(path: Path, parent: Path, *, strict: bool = False) -> bool:
    """Return True if path resolves inside parent (or equals it unless strict).

    With strict=True, a path equal to parent and a symlinked path both yield False.
    """
    if strict and path.is_symlink():
        return False
    try:
        resolved_path = path.resolve()
        resolved_parent = parent.resolve()
        resolved_path.relative_to(resolved_parent)
        return not strict or resolved_path != resolved_parent
    except (OSError, ValueError):
        return False


def remove_path_or_tree(path: Path) -> None:
    """Remove a file/symlink or a whole directory tree (a symlink is unlinked, never followed)."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def prune_empty_parents(path: Path, roots: list[Path]) -> None:
    """Remove the empty folders left above a deleted path, stopping at (and keeping) each root."""
    try:
        resolved_roots = [r.resolve() for r in roots]
    except OSError:
        return
    parent = path.parent
    while True:
        try:
            if (
                parent.is_symlink()
                or not parent.is_dir()
                or parent.resolve() in resolved_roots
                or not any(parent.resolve().is_relative_to(r) for r in resolved_roots)
                or any(parent.iterdir())
            ):
                return
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def move_dir_safely(src: Path, dst: Path) -> None:
    """Move a directory without ever leaving the only full copy at risk.

    Same filesystem: atomic rename. Across filesystems: copy everything first, and only
    then delete the source. A failure while copying raises (source untouched, partial
    destination may remain). A failure while deleting the source is only logged: the
    destination is already complete, so leftovers are preferable to data loss.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copytree(src, dst, symlinks=True)
    try:
        shutil.rmtree(src)
    except OSError:
        logging.warning("Moved %s to %s but could not fully remove the source", src, dst, exc_info=True)


def copy_dir_no_symlinks(src_dir: Path, dst_dir: Path) -> None:
    """Copy a directory tree skipping symlinks."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for root_s, dirs_s, files_s in os.walk(src_dir, followlinks=False):
        rel = Path(root_s).relative_to(src_dir)
        target_sub = dst_dir / rel
        target_sub.mkdir(parents=True, exist_ok=True)
        for d in list(dirs_s):
            if (Path(root_s) / d).is_symlink():
                dirs_s.remove(d)
        for f in files_s:
            sf = Path(root_s) / f
            if not sf.is_symlink() and sf.is_file():
                shutil.copy2(sf, target_sub / f)


def copy_or_move_dir_contents(src_dir: Path, dst_dir: Path, move: bool) -> None:
    """Copy or move all entries from src_dir into dst_dir, merging existing directories."""
    if src_dir.resolve() == dst_dir.resolve():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if move:
            if target.exists() or target.is_symlink():
                if item.is_dir() and not item.is_symlink() and target.is_dir() and not target.is_symlink():
                    copy_or_move_dir_contents(item, target, move=True)
                    try:
                        item.rmdir()
                    except OSError:
                        pass
                    continue
                remove_path_or_tree(target)
            shutil.move(str(item), str(target))
        else:
            if item.is_symlink():
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(item.readlink())
            elif item.is_dir():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    remove_path_or_tree(target)
                copy_or_move_dir_contents(item, target, move=False)
                shutil.copystat(item, target, follow_symlinks=False)
            else:
                if target.is_symlink() or target.is_dir():
                    remove_path_or_tree(target)
                shutil.copy2(item, target)
