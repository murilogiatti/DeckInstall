"""Shared save-sync folder, format 2: one immutable, self-contained ZIP per revision.

Layout (inside the folder the user picked, replicated by Syncthing, rclone, rsync, SFTP...)::

    <root>/.deckinstall-sync.json         {"format": 2}: tells a real sync folder from a missing disk
    <root>/<game>/revs/<rev_id>.zip       one revision, never rewritten
    <root>/<game>/.staging/               half-written revisions of this device (ignored by readers)
    <root>/<game>/latest.zip + meta.json  legacy layout (read only, never changed)

Every revision ZIP carries ``deckinstall-revision.json`` with its id, the ids of the revisions
it replaces (``parents``), the save history id, the device that made it, the digest of its save
files and their count. A revision is used only when the ZIP opens, every entry passes its CRC and
the save files hash to the recorded digest, so a file still being transferred, truncated or
damaged is "waiting" and never restored. The current state is computed from the parents: the
heads are the revisions no other one replaces. Two heads with different saves are a conflict;
nothing is ever overwritten, so a device that was offline can only add a branch.

The transport only copies files. This module owns format, integrity, identity and conflicts.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
import zlib
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import atomic_write_bytes, file_lock, file_sha256

FORMAT = 2
ROOT_MARKER = ".deckinstall-sync.json"
REVS_DIR = "revs"
STAGING_DIR = ".staging"
REV_MANIFEST = "deckinstall-revision.json"
_MANIFEST_MAX = 64 * 1024
LEGACY_ZIP = "latest.zip"
LEGACY_META = "meta.json"
LEGACY_PREFIX = "legacy-"


class SyncFolderUnavailable(RuntimeError):
    """The sync folder is missing, not mounted, or was replaced: nothing is read or written."""


class SyncRemoteChanged(RuntimeError):
    """The shared folder changed since the plan was made: plan again (nothing was written)."""


class RevisionInvalid(RuntimeError):
    """A revision is incomplete, damaged or does not match its manifest."""


@dataclass(frozen=True)
class Revision:
    rev_id: str
    path: Path
    parents: tuple[str, ...]
    save_id: str
    content_digest: str
    file_count: int
    uncompressed_bytes: int
    created_at: float
    device_id: str = ""
    device_label: str = ""
    game_name: str = ""
    app_version: str = ""
    legacy: bool = False
    stored_bytes: int = 0
    pruned: bool = False  # only its manifest is left (the saves were cleaned up to free space)


@dataclass
class RepoState:
    """What a game's folder holds right now."""

    revisions: dict[str, Revision] = field(default_factory=dict)
    waiting: list[Path] = field(default_factory=list)  # incomplete or invalid revision files
    legacy_unverified: bool = False

    def heads(self) -> list[Revision]:
        replaced = {p for rev in self.revisions.values() for p in rev.parents}
        return sorted(
            (r for r in self.revisions.values() if r.rev_id not in replaced),
            key=lambda r: (r.created_at, r.rev_id),
        )

    def distinct_heads(self) -> list[Revision]:
        """Heads with different saves (the newest of each identical group)."""
        by_digest: dict[str, Revision] = {}
        for rev in self.heads():
            by_digest[rev.content_digest] = rev  # heads() is oldest first: keep the newest
        return sorted(by_digest.values(), key=lambda r: (r.created_at, r.rev_id))

    def ancestors(self, rev_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [rev_id]
        while stack:
            cur = self.revisions.get(stack.pop())
            if cur is None:
                continue
            for parent in cur.parents:
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return seen


# --- device identity -------------------------------------------------------------------------


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".local" / "state") / "DeckInstall"


def device_id() -> str:
    """A random id for this device (no personal data), created once."""
    path = _state_dir() / "device_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, value.encode())
    except OSError as exc:
        logging.warning("Could not save the device id: %s", exc)
    return value


def device_label() -> str:
    """The machine model (e.g. "Jupiter", "Dell G15 5515"): shown in conflicts, never a user name."""
    try:
        vendor = Path("/sys/class/dmi/id/sys_vendor").read_text(encoding="utf-8").strip()
        product = Path("/sys/class/dmi/id/product_name").read_text(encoding="utf-8").strip()
    except OSError:
        return "Linux PC"
    label = f"{vendor} {product}".strip() if product and vendor.split()[0:1] != product.split()[0:1] else product
    return "".join(c for c in label if c.isprintable())[:60] or "Linux PC"


# --- the shared folder -----------------------------------------------------------------------


def _known_roots_file() -> Path:
    return _state_dir() / "sync_roots.json"


def _known_roots() -> set[str]:
    try:
        raw = json.loads(_known_roots_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(p) for p in raw.get("roots", [])} if isinstance(raw, dict) else set()


def _remember_root(root: Path) -> None:
    roots = _known_roots() | {str(root)}
    try:
        _known_roots_file().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(_known_roots_file(), json.dumps({"roots": sorted(roots)}, indent=2).encode())
    except OSError as exc:
        logging.warning("Could not remember the sync folder %s: %s", root, exc)


def _root_key(sync_root: Path) -> Path:
    try:
        return sync_root.absolute()
    except OSError:
        return sync_root


def is_new_sync_root(sync_root: Path) -> bool:
    """True for a folder this device never used and that has no marker yet (first setup)."""
    if str(_root_key(sync_root)) in _known_roots() or (sync_root / ROOT_MARKER).exists():
        return False
    return sync_root.is_dir() or (sync_root.parent.is_dir() and not sync_root.is_symlink())


def open_sync_root(sync_root: Path, *, initialize: bool = True) -> Path:
    """Check that sync_root is the sync folder this device uses.

    Only a folder this device never used is created (first setup). A folder used before that
    is missing or lost its marker (a removable disk not mounted, an
    empty mount point, another folder in its place) is unavailable, never "empty". The first
    use writes the marker (which the transport then copies to the other devices).
    """
    key = _root_key(sync_root)
    if not sync_root.is_dir():
        known = str(key) in _known_roots()
        if known or not initialize or not sync_root.parent.is_dir() or sync_root.is_symlink():
            raise SyncFolderUnavailable(
                f"The sync folder is not available (not found or not mounted): {sync_root}"
                + (". It was used before, so it is not created again; mount the disk or pick the folder."
                   if known else "")
            )
        sync_root.mkdir()  # first setup of a folder this device never used
    marker = sync_root / ROOT_MARKER
    if marker.is_file() and not marker.is_symlink():
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SyncFolderUnavailable(f"The sync folder marker cannot be read: {marker} ({exc})") from exc
        fmt = raw.get("format") if isinstance(raw, dict) else None
        if not isinstance(fmt, int) or fmt < FORMAT:
            raise SyncFolderUnavailable(f"Unknown sync folder marker: {marker}")
        if fmt > FORMAT:
            raise SyncFolderUnavailable(
                f"This sync folder was set up by a newer DeckInstall (format {fmt}); update DeckInstall"
            )
        if str(key) not in _known_roots():
            _remember_root(key)
        return sync_root
    if str(key) in _known_roots():
        raise SyncFolderUnavailable(
            f"The sync folder is not the one used before (its marker {ROOT_MARKER} is missing): "
            f"{sync_root}. Mount the disk or check the folder; nothing was changed."
        )
    if not initialize:
        raise SyncFolderUnavailable(f"The sync folder is not set up yet: {sync_root}")
    atomic_write_bytes(marker, json.dumps({"format": FORMAT}, indent=2).encode())
    _remember_root(key)
    return sync_root


# --- reading revisions -----------------------------------------------------------------------


def _read_manifest(zf: zipfile.ZipFile) -> dict[str, object]:
    try:
        info = zf.getinfo(REV_MANIFEST)
    except KeyError as exc:
        raise RevisionInvalid("no revision manifest") from exc
    if info.file_size > _MANIFEST_MAX:
        raise RevisionInvalid("revision manifest too large")
    try:
        raw = json.loads(zf.read(info).decode("utf-8"))
    except (ValueError, zipfile.BadZipFile, OSError) as exc:
        raise RevisionInvalid(f"unreadable revision manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise RevisionInvalid("revision manifest is not an object")
    return raw


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _revision_from_manifest(raw: dict[str, object], path: Path) -> Revision:
    rev_id = raw.get("rev_id")
    parents = raw.get("parents")
    if raw.get("format") != FORMAT:
        raise RevisionInvalid(f"unsupported revision format {raw.get('format')!r}")
    if not isinstance(rev_id, str) or path.stem != rev_id or not _valid_rev_id(rev_id):
        raise RevisionInvalid("revision id does not match its file name")
    if not isinstance(parents, list) or not all(isinstance(p, str) and _valid_rev_id(p) for p in parents):
        raise RevisionInvalid("bad parents")
    if not _is_hex(raw.get("content_digest"), 64):
        raise RevisionInvalid("bad content digest")
    count, size, created = raw.get("file_count"), raw.get("uncompressed_bytes"), raw.get("created_at")
    if not isinstance(count, int) or not isinstance(size, int) or not isinstance(created, (int, float)):
        raise RevisionInvalid("bad sizes")
    try:
        stored = path.stat().st_size
    except OSError:
        stored = 0
    return Revision(
        rev_id=rev_id, path=path, parents=tuple(parents), save_id=str(raw.get("save_id") or ""),
        content_digest=str(raw["content_digest"]), file_count=count, uncompressed_bytes=size,
        created_at=float(created), device_id=str(raw.get("device_id") or ""),
        device_label=str(raw.get("device_label") or ""), game_name=str(raw.get("game_name") or ""),
        app_version=str(raw.get("app_version") or ""), stored_bytes=stored,
    )


def _valid_rev_id(rev_id: str) -> bool:
    if rev_id.startswith(LEGACY_PREFIX):
        return _is_hex(rev_id[len(LEGACY_PREFIX):], 32)
    return _is_hex(rev_id, 32)


def read_revision(path: Path) -> Revision:
    """Read a revision's manifest (cheap: no CRC pass). Raises RevisionInvalid."""
    if path.is_symlink() or not path.is_file():
        raise RevisionInvalid("not a regular file")
    try:
        with zipfile.ZipFile(path) as zf:
            return _revision_from_manifest(_read_manifest(zf), path)
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError) as exc:
        raise RevisionInvalid(f"not a complete ZIP yet: {exc}") from exc


def _legacy_revision(game_dir: Path) -> tuple[Revision | None, bool]:
    """The legacy latest.zip as a read-only revision when meta.json vouches for it."""
    zpath, mpath = game_dir / LEGACY_ZIP, game_dir / LEGACY_META
    if zpath.is_symlink() or not zpath.is_file():
        return None, False
    try:
        meta = json.loads(mpath.read_text(encoding="utf-8")) if mpath.is_file() and not mpath.is_symlink() else None
    except (OSError, ValueError):
        meta = None
    sha = str(meta.get("sha256") or "").strip().lower() if isinstance(meta, dict) else ""
    digest = str(meta.get("fingerprint_digest") or "") if isinstance(meta, dict) else ""
    if not _is_hex(sha, 64):
        return None, True
    try:
        if file_sha256(zpath).lower() != sha:
            return None, True  # e.g. an old client was writing it, or it was damaged
        stored = zpath.stat().st_size
        if not _is_hex(digest, 64):  # very old meta.json: no save fingerprint recorded
            with zipfile.ZipFile(zpath) as zf:
                digest = save_entries_digest(zf)[0] or sha
    except (OSError, zipfile.BadZipFile, zlib.error):
        return None, True
    assert isinstance(meta, dict)
    raw_ts = meta.get("updated_at")
    created = float(raw_ts) if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) else 0.0
    raw_count = meta.get("file_count")
    return Revision(
        rev_id=LEGACY_PREFIX + sha[:32], path=zpath, parents=(), save_id="", content_digest=digest,
        file_count=raw_count if isinstance(raw_count, int) else 0, uncompressed_bytes=0, created_at=created,
        device_label="DeckInstall ≤ 0.35", game_name=str(meta.get("game_name") or ""), legacy=True,
        stored_bytes=stored,
    ), False


def _ignored_name(name: str) -> bool:
    low = name.lower()
    return (
        name.startswith(".")
        or low.endswith((".tmp", ".part", ".partial"))
        or ".sync-conflict-" in low
        or low.startswith("~syncthing~")
    )


def scan_game_folder(game_dir: Path) -> RepoState:
    """Revisions of one game (valid ones by id; files that are not usable yet are listed apart)."""
    state = RepoState()
    legacy, unverified = _legacy_revision(game_dir)
    state.legacy_unverified = unverified
    if legacy is not None:
        state.revisions[legacy.rev_id] = legacy
    revs = game_dir / REVS_DIR
    try:
        entries = sorted(os.scandir(revs), key=lambda e: e.name)
    except OSError:
        return state
    for entry in entries:
        if entry.name.endswith(PRUNED_SUFFIX) and not entry.name.startswith("."):
            pruned = _read_pruned(Path(entry.path))
            if pruned is not None:
                state.revisions.setdefault(pruned.rev_id, pruned)
            continue
        if _ignored_name(entry.name) or not entry.name.endswith(".zip"):
            continue
        path = Path(entry.path)
        try:
            rev = read_revision(path)
        except RevisionInvalid as exc:
            logging.info("Sync revision %s not usable yet: %s", path.name, exc)
            state.waiting.append(path)
            continue
        state.revisions[rev.rev_id] = rev  # a full copy wins over its pruned marker
    return state


# --- cleaning up old revisions (only when the user asks) -------------------------------------

PRUNED_SUFFIX = ".pruned.json"
KEEP_ANCESTORS = 2


def _read_pruned(path: Path) -> Revision | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MANIFEST_MAX:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        rev = _revision_from_manifest(raw, path.with_name(path.name[: -len(PRUNED_SUFFIX)] + ".zip"))
    except (OSError, ValueError, RevisionInvalid):
        return None
    return Revision(**{**rev.__dict__, "pruned": True, "stored_bytes": 0})


@dataclass(frozen=True)
class CleanupPlan:
    game_dir: Path
    remove: tuple[Revision, ...]
    kept: int
    freed_bytes: int


def plan_cleanup(game_dir: Path, keep_ancestors: int = KEEP_ANCESTORS) -> CleanupPlan:
    """Old revisions whose saves can go: never a head, never one of the last keep_ancestors
    versions before each head, never a legacy archive and never a file not verified as complete."""
    state = scan_game_folder(game_dir)
    keep: set[str] = set()
    frontier = [h.rev_id for h in state.heads()]
    keep.update(frontier)
    for _ in range(keep_ancestors):
        nxt: list[str] = []
        for rid in frontier:
            rev = state.revisions.get(rid)
            for parent in rev.parents if rev is not None else ():
                if parent not in keep:
                    keep.add(parent)
                    nxt.append(parent)
        frontier = nxt
    remove = tuple(
        r for r in state.revisions.values()
        if r.rev_id not in keep and not r.legacy and not r.pruned and r.path.is_file()
    )
    full = [r for r in state.revisions.values() if not r.pruned and not r.legacy]
    return CleanupPlan(game_dir, remove, len(full) - len(remove), sum(r.stored_bytes for r in remove))


def apply_cleanup(plan: CleanupPlan) -> int:
    """Replace each planned revision by its manifest only. Returns the bytes freed.

    The marker is written before the ZIP is deleted, so the history (which revision replaced
    which) survives; a device that synced one of these versions still recognises its base.
    """
    freed = 0
    with game_lock(plan.game_dir):
        current = plan_cleanup(plan.game_dir)
        allowed = {r.rev_id for r in current.remove}
        for rev in plan.remove:
            if rev.rev_id not in allowed:
                continue  # it became current again (e.g. a branch arrived meanwhile)
            try:
                with zipfile.ZipFile(rev.path) as zf:
                    manifest = zf.read(REV_MANIFEST)
                marker = rev.path.with_name(rev.rev_id + PRUNED_SUFFIX)
                atomic_write_bytes(marker, manifest)
                if _read_pruned(marker) is None:
                    marker.unlink(missing_ok=True)
                    continue
                size = rev.path.stat().st_size
                rev.path.unlink()
                freed += size
            except (OSError, KeyError, zipfile.BadZipFile, zlib.error) as exc:
                logging.warning("Could not clean up sync revision %s: %s", rev.path, exc)
    return freed


def folder_usage(sync_root: Path) -> tuple[int, int]:
    """(bytes used by revisions, bytes a cleanup would free) for the whole sync folder."""
    used = freed = 0
    try:
        games = [Path(e.path) for e in os.scandir(sync_root) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return 0, 0
    for game_dir in games:
        state = scan_game_folder(game_dir)
        used += sum(r.stored_bytes for r in state.revisions.values() if not r.legacy)
        freed += plan_cleanup(game_dir).freed_bytes
    return used, freed


# --- verifying a copy ------------------------------------------------------------------------


def save_entries_digest(zf: zipfile.ZipFile) -> tuple[str, int, int]:
    """(digest, file_count, bytes) of the save/ entries, computed like the save fingerprint."""
    h = hashlib.sha256()
    count = 0
    total = 0
    any_file = False
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith("save/"):
            continue
        rel = info.filename[len("save/"):]
        any_file = True
        total += info.file_size
        if info.file_size == 0:
            continue  # the fingerprint skips empty files
        h.update(f"{rel}\0{info.file_size}\0".encode())
        with zf.open(info) as src:
            while chunk := src.read(65536):
                h.update(chunk)
        count += 1
    return (h.hexdigest() if any_file else ""), count, total


def verified_local_copy(rev: Revision, staging: Path) -> Path:
    """Copy a revision out of the shared folder and verify the copy; return it.

    Restoring from this private copy means the bytes that were checked are the bytes restored,
    even while a transport keeps writing in the shared folder.
    """
    staging.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{rev.rev_id[:16]}-", suffix=".zip", dir=str(staging))
    os.close(fd)
    copy = Path(name)
    try:
        shutil.copyfile(rev.path, copy)
        if rev.legacy:
            # the legacy archive is verified by the meta.json checksum (already matched once)
            if LEGACY_PREFIX + file_sha256(copy).lower()[:32] != rev.rev_id:
                raise RevisionInvalid("legacy archive changed while it was copied")
            with zipfile.ZipFile(copy) as zf:
                if zf.testzip() is not None:
                    raise RevisionInvalid("damaged legacy archive")
            return copy
        with zipfile.ZipFile(copy) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RevisionInvalid(f"damaged entry {bad}")
            copied = _revision_from_manifest(_read_manifest(zf), rev.path)
            names = [i.filename for i in zf.infolist()]
            if len(names) != len(set(names)) or len({n.lower() for n in names}) != len(names):
                raise RevisionInvalid("repeated entry names")
            digest, count, total = save_entries_digest(zf)
        if (copied.content_digest, copied.parents) != (rev.content_digest, rev.parents):
            raise RevisionInvalid("revision changed while it was copied")
        if digest != rev.content_digest or count != rev.file_count or total != rev.uncompressed_bytes:
            raise RevisionInvalid("save files do not match the revision manifest")
        return copy
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError, RevisionInvalid) as exc:
        copy.unlink(missing_ok=True)
        if isinstance(exc, RevisionInvalid):
            raise
        raise RevisionInvalid(f"{rev.path.name}: {exc}") from exc


def local_staging_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".cache") / "DeckInstall" / "sync-staging"


# --- writing a revision ----------------------------------------------------------------------


def new_rev_id() -> str:
    return uuid.uuid4().hex


def revision_manifest(
    rev_id: str, parents: Iterable[str], save_id: str, content_digest: str, file_count: int,
    uncompressed_bytes: int, game_name: str,
) -> bytes:
    from . import __version__

    return json.dumps({
        "format": FORMAT,
        "rev_id": rev_id,
        "parents": sorted(set(parents)),
        "save_id": save_id,
        "content_digest": content_digest,
        "file_count": file_count,
        "uncompressed_bytes": uncompressed_bytes,
        "created_at": time.time(),  # shown to people; never used to order revisions
        "device_id": device_id(),
        "device_label": device_label(),
        "game_name": game_name,
        "app_version": __version__,
    }, indent=2, ensure_ascii=False).encode("utf-8")


def staging_file(game_dir: Path) -> Path:
    staging = game_dir / STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="rev-", suffix=".zip.tmp", dir=str(staging))
    os.close(fd)
    return Path(name)


def _fsync_path(path: Path, directory: bool = False) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # best effort (some removable and network filesystems)
    finally:
        os.close(fd)


def publish_revision(game_dir: Path, tmp: Path, rev_id: str) -> Path:
    """Move a finished, checked staging file to revs/<rev_id>.zip without replacing anything."""
    _fsync_path(tmp)
    rev = read_revision_at(tmp, rev_id)
    revs = game_dir / REVS_DIR
    revs.mkdir(parents=True, exist_ok=True)
    final = revs / f"{rev_id}.zip"
    try:
        os.link(tmp, final)  # fails if the name exists: never overwrites
        tmp.unlink(missing_ok=True)
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS):
            raise
        if final.exists() or final.is_symlink():  # filesystems without hard links (exFAT, some FUSE)
            tmp.unlink(missing_ok=True)
            raise FileExistsError(final) from exc
        os.replace(tmp, final)
    _fsync_path(revs, directory=True)
    del rev
    return final


def read_revision_at(path: Path, rev_id: str) -> Revision:
    """Fully check a just-written staging file before it is published."""
    with zipfile.ZipFile(path) as zf:
        if zf.testzip() is not None:
            raise RevisionInvalid("the new revision is damaged")
        raw = _read_manifest(zf)
        digest, count, total = save_entries_digest(zf)
    rev = _revision_from_manifest(raw, path.with_name(f"{rev_id}.zip"))
    if (digest, count, total) != (rev.content_digest, rev.file_count, rev.uncompressed_bytes):
        raise RevisionInvalid("the new revision does not match its manifest")
    return rev


def game_lock(game_dir: Path) -> AbstractContextManager[None]:
    """Local lock for one game's folder (other devices are never locked: they add branches)."""
    lock_dir = _state_dir() / "sync-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(_root_key(game_dir)).encode()).hexdigest()[:24]
    return file_lock(lock_dir / f"{key}.lock")
