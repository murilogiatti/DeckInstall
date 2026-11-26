"""Shared filesystem helpers: atomic writes, hashing, slugs, containment checks, copy/move."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_NON_UNIX_PERM_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL})


_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCAL = threading.local()

FileIdentity = tuple[int, int, int, int, int]


def file_identity(st: os.stat_result) -> FileIdentity:
    return st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size


def open_regular_fd(path: Path, flags: int = os.O_RDONLY) -> int:
    """Open a regular file without following its final component or blocking on a FIFO."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Acquire both per-path intra-process RLock and cross-process flock for a lockfile path."""
    if lock_path.is_symlink():
        raise RuntimeError(f"Refusing symlink lockfile: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = Path(os.path.realpath(lock_path))
    with _FILE_LOCKS_GUARD:
        path_lock = _FILE_LOCKS.get(lock_key)
        if path_lock is None:
            path_lock = threading.RLock()
            _FILE_LOCKS[lock_key] = path_lock
    depths: dict[Path, int] | None = getattr(_THREAD_LOCAL, "flock_depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCAL.flock_depths = depths
    depth = depths.get(lock_key, 0)
    with path_lock:
        if depth > 0:
            depths[lock_key] = depth + 1
            try:
                yield
            finally:
                depths[lock_key] = depth
            return
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"Lock path is not a regular file: {lock_path}")
            fcntl.flock(fd, fcntl.LOCK_EX)
            depths[lock_key] = 1
            try:
                yield
            finally:
                depths.pop(lock_key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _safe_chmod(path: Path, mode: int) -> None:
    """Apply mode, tolerating permission-unsupported errors on exFAT/vFAT/NTFS filesystems."""
    try:
        os.chmod(path, mode)
    except OSError as exc:
        if exc.errno not in _NON_UNIX_PERM_ERRNOS:
            raise


def _safe_copystat(src: Path, dst: Path, *, follow_symlinks: bool = False) -> None:
    """Copy metadata timestamps/mode best-effort, tolerating exFAT/vFAT/NTFS limitations."""
    try:
        shutil.copystat(src, dst, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if exc.errno not in _NON_UNIX_PERM_ERRNOS:
            raise


def _rmtree_force(path: Path, *, ignore_errors: bool = False) -> None:
    """Retry removal after restoring owner permissions to read-only directories in the tree."""
    if not path.exists() and not path.is_symlink():
        return

    try:
        try:
            shutil.rmtree(path)
        except PermissionError:
            pending = [path]
            while pending:
                directory = pending.pop()
                try:
                    mode = directory.lstat().st_mode
                    if not stat.S_ISDIR(mode):
                        continue
                    if mode & 0o700 != 0o700:
                        os.chmod(directory, stat.S_IMODE(mode) | 0o700, follow_symlinks=False)
                    with os.scandir(directory) as entries:
                        pending.extend(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))
                except FileNotFoundError:
                    continue
            shutil.rmtree(path)
    except FileNotFoundError:
        if path.exists() and not ignore_errors:
            raise
    except OSError:
        if not ignore_errors:
            raise


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write bytes to a unique temp file next to path, fsync, then atomically replace path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    preserved_mode = mode
    if preserved_mode is None and not path.is_symlink() and path.is_file():
        try:
            preserved_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            preserved_mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if preserved_mode is not None:
            _safe_chmod(tmp, preserved_mode)
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


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write UTF-8 text atomically (see atomic_write_bytes)."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_copy_file(src: Path, dst: Path) -> None:
    """Atomically copy src to dst via a temporary file in dst.parent (metadata best-effort)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        try:
            shutil.copy2(src, tmp)
        except OSError as exc:
            if exc.errno not in _NON_UNIX_PERM_ERRNOS:
                raise
            shutil.copyfile(src, tmp)
            _safe_copystat(src, tmp, follow_symlinks=True)
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


def unique_dir(target: Path, taken: set[Path] | None = None) -> Path:
    """Find an unused sibling without following a dangling symlink."""
    cand = target
    idx = 2
    while cand.exists() or cand.is_symlink() or (taken is not None and cand in taken):
        cand = target.with_name(f"{target.name} ({idx})")
        idx += 1
    return cand


def sanitize_dir_name(name: str) -> str:
    """Return a filesystem-safe folder name (illegal characters replaced by spaces)."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", name).strip()
    if cleaned in ("", ".", ".."):
        return "Game"
    return cleaned


def symlink_points_to(link: Path, target: Path) -> bool:
    """Return True if link is a symlink whose resolved target matches target.resolve()."""
    if not link.is_symlink():
        return False
    try:
        raw = Path(os.readlink(link))
        if not raw.is_absolute():
            raw = link.parent / raw
        return raw.resolve() == target.resolve()
    except OSError:
        return False


_DESKTOP_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_DESKTOP_STRING_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


def desktop_exec_quote(path: Path) -> str:
    """Quote a path as one Exec= argument (Desktop Entry spec: %% for %, backslash escapes inside quotes)."""
    s = str(path).replace("%", "%%")
    needs_quotes = any(ch in _DESKTOP_RESERVED for ch in s)
    if not needs_quotes:
        return s
    for ch in ("\\", '"', "`", "$"):
        s = s.replace(ch, "\\" + ch)
    return '"' + s.replace("\\", "\\\\") + '"'


def desktop_exec_tokens(value: str) -> list[str]:
    """Split an Exec= value into arguments, undoing desktop_exec_quote (and unquoted legacy values)."""
    text = re.sub(
        r"\\(.)", lambda m: _DESKTOP_STRING_ESCAPES.get(m.group(1), m.group(0)), value
    )
    tokens: list[str] = []
    cur: list[str] = []
    in_token = in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quotes:
            if ch == "\\" and i + 1 < len(text) and text[i + 1] in '"`$\\':
                cur.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_quotes = False
            else:
                cur.append(ch)
        elif ch == '"':
            in_quotes = in_token = True
        elif ch in " \t":
            if in_token:
                tokens.append("".join(cur))
                cur, in_token = [], False
        else:
            cur.append(ch)
            in_token = True
        i += 1
    if in_token:
        tokens.append("".join(cur))
    return [tok.replace("%%", "%") for tok in tokens]


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
        _rmtree_force(path, ignore_errors=True)


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


def _copytree_portable(src: Path, dst: Path) -> None:
    """Copy a directory tree cross-device, tolerating non-Unix chmod/copystat/symlink limits."""
    src_resolved = src.resolve()
    visited: set[Path] = {src_resolved}

    def _copy_dir(cur_src: Path, cur_dst: Path) -> None:
        cur_dst.mkdir(parents=True, exist_ok=True)
        for item in cur_src.iterdir():
            target = cur_dst / item.name
            if item.is_symlink():
                try:
                    os.symlink(os.readlink(item), target)
                except OSError as exc:
                    if exc.errno not in _NON_UNIX_PERM_ERRNOS:
                        raise
                    try:
                        resolved = item.resolve()
                    except OSError:
                        logging.warning("Skipping broken symlink %s on non-Unix filesystem", item)
                        continue
                    if not resolved.is_relative_to(src_resolved) or resolved in visited:
                        logging.warning("Skipping external/cyclic symlink %s on non-Unix filesystem", item)
                        continue
                    if resolved.is_file():
                        shutil.copyfile(resolved, target)
                        _safe_copystat(resolved, target, follow_symlinks=True)
                    elif resolved.is_dir():
                        visited.add(resolved)
                        try:
                            _copy_dir(resolved, target)
                        finally:
                            visited.discard(resolved)
            elif item.is_dir():
                try:
                    r_dir = item.resolve()
                except OSError:
                    r_dir = item
                if r_dir in visited:
                    continue
                visited.add(r_dir)
                try:
                    _copy_dir(item, target)
                finally:
                    visited.discard(r_dir)
                _safe_copystat(item, target, follow_symlinks=False)
            elif item.is_file():
                shutil.copyfile(item, target)
                _safe_copystat(item, target, follow_symlinks=False)
        _safe_copystat(cur_src, cur_dst, follow_symlinks=False)

    _copy_dir(src, dst)


def move_dir_safely(src: Path, dst: Path) -> None:
    """Move a directory without ever leaving the only full copy at risk.

    Same filesystem: atomic rename. Across filesystems: copy everything first, and only
    then publish the destination and delete the source. A copy failure leaves the source
    and destination untouched. A failure while deleting the source is only logged: the
    destination is already complete, so leftovers are preferable to data loss.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    stage_dir = Path(tempfile.mkdtemp(prefix=".deckinstall-move-", dir=dst.parent))
    try:
        staged = stage_dir / "tree"
        try:
            shutil.copytree(src, staged, symlinks=True)
        except OSError as exc:
            if exc.errno not in _NON_UNIX_PERM_ERRNOS and not isinstance(exc, shutil.Error):
                raise
            _rmtree_force(staged)
            _copytree_portable(src, staged)
        os.replace(staged, dst)
    finally:
        _rmtree_force(stage_dir, ignore_errors=True)
    try:
        _rmtree_force(src)
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
                shutil.copyfile(sf, target_sub / f)
                _safe_copystat(sf, target_sub / f, follow_symlinks=True)


def copy_or_move_dir_contents(src_dir: Path, dst_dir: Path, move: bool) -> None:
    """Copy or move all entries from src_dir into dst_dir, merging existing directories."""
    if src_dir.resolve() == dst_dir.resolve():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if move:
            if item.is_dir() and not item.is_symlink() and target.is_dir() and not target.is_symlink():
                copy_or_move_dir_contents(item, target, move=True)
                try:
                    item.rmdir()
                except OSError:
                    pass
                continue
            backup_path: Path | None = None
            if target.exists() or target.is_symlink():
                fd, bak_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".movebak", dir=str(dst_dir)
                )
                os.close(fd)
                backup_path = Path(bak_name)
                backup_path.unlink(missing_ok=True)
                os.rename(target, backup_path)
            try:
                if item.is_dir() and not item.is_symlink():
                    move_dir_safely(item, target)
                else:
                    try:
                        os.rename(item, target)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        if item.is_symlink():
                            os.symlink(os.readlink(item), target)
                        else:
                            atomic_copy_file(item, target)
                        item.unlink()
            except BaseException:
                if backup_path is not None and (backup_path.exists() or backup_path.is_symlink()):
                    try:
                        if not target.exists() and not target.is_symlink():
                            os.rename(backup_path, target)
                    except OSError:
                        pass
                raise
            if backup_path is not None:
                remove_path_or_tree(backup_path)
        else:
            if item.is_symlink():
                if not target.exists() and not target.is_symlink():
                    try:
                        target.symlink_to(item.readlink())
                    except OSError as exc:
                        if exc.errno not in _NON_UNIX_PERM_ERRNOS:
                            raise
                        logging.warning("Skipping symlink %s on non-Unix filesystem %s", item, target)
            elif item.is_dir():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    remove_path_or_tree(target)
                copy_or_move_dir_contents(item, target, move=False)
                _safe_copystat(item, target, follow_symlinks=False)
            else:
                if target.is_symlink() or target.is_dir():
                    remove_path_or_tree(target)
                atomic_copy_file(item, target)


_APPIMAGE_ENV_KEYS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")
_PATH_ENV_KEYS = ("PATH", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH")
# Set for DeckInstall's own interpreter/Qt or API tokens; a game, installer or Steam must never inherit them.
_APP_ONLY_ENV_KEYS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONNOUSERSITE",
    "DECKINSTALL_STEAMGRIDDB_KEY",
    "DECKINSTALL_GITHUB_TOKEN",
)


def sleep_inhibit_problem(timeout: float = 5.0) -> str:
    """Why the PC cannot be kept awake while a game runs ("" when systemd-inhibit works).

    Runs the same request the game launcher makes, around a no-op command, so the answer
    matches what will happen when the game starts.
    """
    import shutil
    import subprocess

    exe = shutil.which("systemd-inhibit")
    if exe is None:
        return "systemd-inhibit is not installed"
    try:
        proc = subprocess.run(
            [exe, "--what=idle", "--mode=block", "--who=DeckInstall", "--why=check", "true"],
            capture_output=True, text=True, timeout=timeout, check=False, env=clean_child_env(),
        )
    except subprocess.TimeoutExpired:
        return f"systemd-inhibit did not answer within {timeout:g} s"
    except OSError as exc:
        return str(exc)
    if proc.returncode == 0:
        return ""
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[0][:300] if text else f"systemd-inhibit exited with code {proc.returncode}"


def clean_child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for programs DeckInstall starts (games, installers, Steam, the new AppImage).

    Drops the AppImage variables and DeckInstall's own interpreter settings, and removes the
    AppImage mount from PATH/LD_LIBRARY_PATH/QT_PLUGIN_PATH, so children use the system's libraries.
    """
    env = dict(os.environ if base is None else base)
    appdir = env.get("APPDIR", "")
    for key in (*_APPIMAGE_ENV_KEYS, *_APP_ONLY_ENV_KEYS):
        env.pop(key, None)
    if appdir:
        appdir_clean = appdir.rstrip("/")
        appdir_prefix = appdir_clean + "/"
        for key in _PATH_ENV_KEYS:
            raw_val = env.get(key)
            if not raw_val:
                continue
            kept = [e for e in raw_val.split(":") if e != appdir_clean and not e.startswith(appdir_prefix)]
            if kept:
                env[key] = ":".join(kept)
            else:
                env.pop(key, None)
    return env
