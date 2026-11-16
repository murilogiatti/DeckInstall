"""Pre-operation save snapshots and folder-based save synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
import stat
from dataclasses import dataclass
from pathlib import Path

from .steam_vdf import (
    _RESTORE_MAX_ENTRIES,
    _RESTORE_MAX_FILE_BYTES,
    _RESTORE_MAX_TOTAL_BYTES,
    _iter_prefix_save_pairs,
    _iter_programdata_save_pairs,
    _norm_key,
    _prefix_programdata_dir,
    _prefix_users_dir,
    _safe_parents_within,
    _write_prefix_saves_to_zip,
)

DEFAULT_SNAPSHOTS_DIR = Path.home() / "Backups" / "DeckInstall" / "snapshots"
DEFAULT_SYNC_DIR = Path.home() / "Sync" / "DeckInstallSaves"

_META_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SaveFingerprint:
    """Deterministic fingerprint of all save files inside a Wine/Proton prefix."""

    latest_mtime: float
    file_count: int
    digest: str


@dataclass(frozen=True)
class SyncPlanItem:
    """Planned synchronization action for a single game prefix."""

    game_name: str
    slug: str
    prefix_dir: Path
    action: str  # "PUSH", "PULL", "IN_SYNC", "CONFLICT", "NO_SAVES"
    reason: str


def _slugify(value: str, fallback: str = "game") -> str:
    """Return a filesystem-safe lowercase slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.")
    if cleaned in ("", ".", ".."):
        norm = _norm_key(value)
        return norm or fallback
    return cleaned.lower()


def _is_within_root(path: Path, root_resolved: Path) -> bool:
    """Return True if path resolves inside root_resolved."""
    try:
        return path.resolve().is_relative_to(root_resolved)
    except OSError:
        return False


def _collect_prefix_save_entries(prefix_dir: Path) -> list[tuple[str, Path, Path]]:
    """Collect sorted (section, src_file, rel_path) tuples for save files in prefix_dir."""
    if not prefix_dir.is_dir():
        return []
    entries: list[tuple[str, Path, Path]] = []
    src_users = _prefix_users_dir(prefix_dir)
    if src_users is not None:
        dummy_users = prefix_dir / "__fp_users__"
        for src_file, dst_rel in _iter_prefix_save_pairs(
            src_users, dummy_users, preserve_user_dirs=True, raise_on_error=False
        ):
            entries.append(("users", src_file, dst_rel.relative_to(dummy_users)))
    src_progdata = _prefix_programdata_dir(prefix_dir)
    if src_progdata is not None:
        dummy_pd = prefix_dir / "__fp_pd__"
        for src_file, dst_rel in _iter_programdata_save_pairs(
            src_progdata, dummy_pd, raise_on_error=False
        ):
            entries.append(("ProgramData", src_file, dst_rel.relative_to(dummy_pd)))
    entries.sort(key=lambda item: (item[0], item[2].as_posix()))
    return entries


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to target using a temp file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("wb") as fh:
            fh.write(data)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _rotate_snapshots(snap_dir: Path, max_keep: int) -> None:
    """Keep at most max_keep newest .zip snapshots in snap_dir."""
    if max_keep <= 0 or not snap_dir.is_dir():
        return
    zips: list[tuple[float, str, Path]] = []
    for p in snap_dir.glob("*.zip"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        zips.append((st.st_mtime, p.name, p))
    zips.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, old_zip in zips[max_keep:]:
        try:
            old_zip.unlink()
        except OSError:
            pass


def _write_saves_zip_atomic(
    prefix_dir: Path,
    target_zip: Path,
    key_prefix: str,
) -> int:
    """Write prefix saves to target_zip atomically; return count of backed-up files."""
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_zip.stem}-", suffix=".zip.tmp", dir=str(target_zip.parent)
    )
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            game_files, _, _ = _write_prefix_saves_to_zip(
                zf, prefix_dir, key_prefix, 0, 0
            )
        if game_files == 0:
            try:
                tmp_zip.unlink()
            except OSError:
                pass
            return 0
        os.replace(tmp_zip, target_zip)
        return game_files
    except Exception:
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        raise


def snapshot_prefix_saves(
    prefix_dir: Path,
    game_name: str,
    reason: str,
    key_prefix: str = "snap",
    dest_root: Path | None = None,
    max_keep: int = 5,
) -> Path | None:
    """Create a rotated pre-operation ZIP snapshot of prefix saves."""
    if not prefix_dir.is_dir():
        return None
    root = dest_root or DEFAULT_SNAPSHOTS_DIR
    safe_slug = _slugify(game_name)
    safe_reason = _slugify(reason, fallback="op")
    snap_dir = root / safe_slug
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_zip = snap_dir / f"{stamp}-{safe_reason}.zip"
    if target_zip.exists():
        counter = 1
        while (snap_dir / f"{stamp}-{safe_reason}-{counter}.zip").exists():
            counter += 1
        target_zip = snap_dir / f"{stamp}-{safe_reason}-{counter}.zip"

    written = _write_saves_zip_atomic(prefix_dir, target_zip, key_prefix)
    if written == 0:
        return None
    _rotate_snapshots(snap_dir, max_keep)
    return target_zip


def compute_prefix_saves_fingerprint(prefix_dir: Path) -> SaveFingerprint:
    """Compute a deterministic fingerprint over all non-empty save files in prefix_dir."""
    entries = _collect_prefix_save_entries(prefix_dir)
    if not entries:
        return SaveFingerprint(latest_mtime=0.0, file_count=0, digest="")

    h = hashlib.sha256()
    latest_mtime = 0.0
    file_count = 0
    for section, src_file, rel_path in entries:
        try:
            st = src_file.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        try:
            sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        h.update(f"{section}/{rel_path.as_posix()}\0{st.st_size}\0".encode("utf-8"))
        with os.fdopen(sfd, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
        latest_mtime = max(latest_mtime, float(st.st_mtime))
        file_count += 1

    if file_count == 0:
        return SaveFingerprint(latest_mtime=0.0, file_count=0, digest="")
    return SaveFingerprint(
        latest_mtime=latest_mtime,
        file_count=file_count,
        digest=h.hexdigest(),
    )


def push_game_saves(game_name: str, prefix_dir: Path, sync_root: Path) -> Path | None:
    """Package prefix saves into <sync_root>/<slug>/latest.zip with atomic meta.json."""
    if not prefix_dir.is_dir():
        return None
    slug = _slugify(game_name)
    fp = compute_prefix_saves_fingerprint(prefix_dir)
    if fp.file_count == 0:
        return None

    game_sync_dir = sync_root / slug
    latest_zip = game_sync_dir / "latest.zip"
    written = _write_saves_zip_atomic(prefix_dir, latest_zip, key_prefix="save")
    if written == 0:
        return None

    meta = {
        "game_name": game_name,
        "slug": slug,
        "updated_at": time.time(),
        "sha256": _sha256_file(latest_zip),
        "file_count": written,
        "fingerprint_digest": fp.digest,
    }
    meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(game_sync_dir / "meta.json", meta_bytes)
    return latest_zip


def _resolve_drive_c_target(prefix_dir: Path) -> Path:
    """Return the drive_c directory for extraction, supporting Proton pfx/drive_c or drive_c."""
    pfx_drive_c = prefix_dir / "pfx" / "drive_c"
    plain_drive_c = prefix_dir / "drive_c"
    if pfx_drive_c.is_dir() and not plain_drive_c.exists():
        return pfx_drive_c
    return plain_drive_c


def _parse_sync_zip_entry(filename: str) -> tuple[str, tuple[str, ...]] | None:
    """Extract (section, rel_parts) for valid users/ or ProgramData/ archive entries."""
    if not filename or filename.startswith("/") or "\\" in filename:
        return None
    parts = Path(filename).parts
    if any(p in ("..", "", ".") for p in parts):
        return None
    if len(parts) >= 2 and parts[0] in ("users", "ProgramData"):
        return parts[0], parts[1:]
    if len(parts) >= 3 and parts[1] in ("users", "ProgramData"):
        return parts[1], parts[2:]
    return None


def pull_game_saves(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    snapshots_root: Path | None = None,
) -> tuple[int, Path | None]:
    """Snapshot local saves, then safely extract synced saves into prefix drive_c."""
    slug = _slugify(game_name)
    latest_zip = sync_root / slug / "latest.zip"
    if not latest_zip.is_file():
        return 0, None

    snap_path = snapshot_prefix_saves(
        prefix_dir,
        game_name,
        "pre-sync-pull",
        dest_root=snapshots_root,
    )

    drive_c = _resolve_drive_c_target(prefix_dir)
    drive_c.mkdir(parents=True, exist_ok=True)
    drive_c_resolved = drive_c.resolve()

    restored_files = 0
    total_bytes = 0
    with zipfile.ZipFile(latest_zip, "r") as zf:
        infolist = zf.infolist()
        if len(infolist) > _RESTORE_MAX_ENTRIES:
            raise RuntimeError(
                f"Sync archive exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
            )
        for info in infolist:
            if info.is_dir():
                continue
            # Block symlink entries stored in POSIX zip attributes.
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                continue
            parsed = _parse_sync_zip_entry(info.filename)
            if parsed is None:
                continue
            section, rel_parts = parsed
            if info.file_size > _RESTORE_MAX_FILE_BYTES:
                raise RuntimeError(f"Sync file too large: {info.filename}")
            if not _safe_parents_within(
                drive_c, drive_c_resolved, (section, *rel_parts[:-1])
            ):
                continue

            target = drive_c.joinpath(section, *rel_parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _is_within_root(target.parent, drive_c_resolved):
                continue
            if target.is_symlink():
                continue

            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                copied = 0
                with zf.open(info, "r") as src_fh, tmp_path.open("wb") as dst_fh:
                    while chunk := src_fh.read(65536):
                        copied += len(chunk)
                        total_bytes += len(chunk)
                        if (
                            copied > _RESTORE_MAX_FILE_BYTES
                            or total_bytes > _RESTORE_MAX_TOTAL_BYTES
                        ):
                            raise RuntimeError(
                                f"Decompression limit exceeded on {info.filename}"
                            )
                        dst_fh.write(chunk)
                os.replace(tmp_path, target)
            except Exception:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
            restored_files += 1

    return restored_files, snap_path


def _read_sync_meta(meta_path: Path) -> dict[str, object] | None:
    """Safely read sync meta.json if present and valid."""
    if not meta_path.is_file() or meta_path.is_symlink():
        return None
    try:
        if meta_path.stat().st_size > _META_MAX_BYTES:
            return None
        raw = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def plan_sync_all(games: list[tuple[str, Path]], sync_root: Path) -> list[SyncPlanItem]:
    """Compare local prefix save fingerprints against sync_root metadata."""
    plan: list[SyncPlanItem] = []
    for game_name, prefix_dir in games:
        slug = _slugify(game_name)
        local_fp = compute_prefix_saves_fingerprint(prefix_dir)
        has_local = local_fp.file_count > 0

        game_sync_dir = sync_root / slug
        has_remote = (game_sync_dir / "latest.zip").is_file()

        if not has_local and not has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="NO_SAVES",
                    reason="No local or remote saves found",
                )
            )
            continue
        if has_local and not has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="PUSH",
                    reason="Local saves present; no remote sync archive yet",
                )
            )
            continue
        if not has_local and has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="PULL",
                    reason="Remote sync archive available; no local saves",
                )
            )
            continue

        meta = _read_sync_meta(game_sync_dir / "meta.json")
        if meta is None:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="CONFLICT",
                    reason="Remote metadata missing or unreadable",
                )
            )
            continue

        remote_digest = str(meta.get("fingerprint_digest") or "")
        try:
            remote_updated_at = float(meta.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            remote_updated_at = 0.0

        if remote_digest and local_fp.digest == remote_digest:
            action = "IN_SYNC"
            reason = "Local and remote save digests match"
        elif not remote_digest or remote_updated_at <= 0.0:
            action = "CONFLICT"
            reason = "Incomplete remote metadata; manual resolution required"
        elif local_fp.latest_mtime > remote_updated_at:
            action = "PUSH"
            reason = "Local saves are newer than remote sync"
        elif remote_updated_at > local_fp.latest_mtime:
            action = "PULL"
            reason = "Remote sync is newer than local saves"
        else:
            action = "CONFLICT"
            reason = "Local and remote saves differ with identical timestamps"

        plan.append(
            SyncPlanItem(
                game_name=game_name,
                slug=slug,
                prefix_dir=prefix_dir,
                action=action,
                reason=reason,
            )
        )
    return plan
