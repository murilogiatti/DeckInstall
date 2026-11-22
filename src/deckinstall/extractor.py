"""Archive extraction and Windows setup.exe runner via Proton."""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from . import runtime_manager
from .fsutil import copy_dir_no_symlinks, move_dir_safely
from .scanner import clean_game_name
from .steam_paths import find_proton_binary, find_steam_root

ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".tar.bz2",
    ".tar",
    ".part1.rar",
    ".7z.001",
    ".zip.001",
    ".rar.001",
    ".001",
    ".zip",
    ".rar",
    ".7z",
)


class InsufficientSpaceError(RuntimeError):
    """Raised when the target filesystem lacks enough free space."""

    def __init__(self, target_path: Path, required_bytes: int, available_bytes: int) -> None:
        self.target_path = target_path
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        req_mb = required_bytes / (1024 * 1024)
        avail_mb = available_bytes / (1024 * 1024)
        super().__init__(
            f"Insufficient disk space at {target_path}: "
            f"need {req_mb:.1f} MiB, only {avail_mb:.1f} MiB available."
        )


def check_disk_space(
    target_dir: Path,
    required_bytes: int,
    safety_margin: float = 1.1,
) -> tuple[int, int]:
    """Verify free space on target_dir's filesystem and return (required, available) bytes."""
    probe = target_dir
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    usage = shutil.disk_usage(probe)
    needed = int(required_bytes * safety_margin) if required_bytes > 0 else 0
    if usage.free < needed:
        raise InsufficientSpaceError(target_dir, needed, usage.free)
    return (needed, usage.free)


def estimate_archive_uncompressed_size(archive: Path) -> int:
    """Estimate uncompressed byte size from zip/tar metadata, falling back to 2x file size."""
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive, "r") as zf:
                infos = zf.infolist()
                if len(infos) > _MAX_ARCHIVE_ENTRIES:
                    return archive_set_size(archive) * 2
                total = sum(info.file_size for info in infos)
                if total > 0:
                    return total
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive, "r:*") as tf:
                total = 0
                for idx, member in enumerate(tf):
                    if idx >= _MAX_ARCHIVE_ENTRIES:
                        return archive_set_size(archive) * 2
                    if member.isfile():
                        total += max(0, int(member.size))
                if total > 0:
                    return total
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, ValueError):
        pass
    return archive_set_size(archive) * 2


def is_archive(path: Path) -> bool:
    """Return True if path has a supported archive extension."""
    low = path.name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_SUFFIXES)


def suggest_target_name(source: Path) -> str:
    """Derive a clean destination folder name from an archive or installer file."""
    name = source.name
    for ext in ARCHIVE_SUFFIXES:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    else:
        name = source.stem
    return clean_game_name(name)


def sanitize_folder_name(name: str) -> str:
    """Ensure folder_name is a single safe directory name without path traversal."""
    cleaned = name.strip().strip("/\\")
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Invalid folder name: {name!r}")
    return cleaned


def _find_7z_bin() -> str | None:
    """Return the first available 7-Zip CLI binary (7z, 7zz, or 7za)."""
    return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


def _select_extract_cmd(archive: Path, out_dir: Path) -> list[str]:
    arc = archive.resolve()
    low = arc.name.lower()
    tar_exts = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")
    if any(low.endswith(ext) for ext in tar_exts) and shutil.which("tar"):
        return ["tar", "--no-same-owner", "--no-same-permissions", "-xf", str(arc), "-C", str(out_dir)]
    if low.endswith((".rar", ".part1.rar", ".rar.001")) and shutil.which("unrar"):
        return ["unrar", "x", "-o+", "-y", "-p-", "--", str(arc), str(out_dir) + "/"]
    if low.endswith(".zip") and shutil.which("unzip"):
        return ["unzip", "-o", "--", str(arc), "-d", str(out_dir)]
    seven_z = _find_7z_bin()
    if seven_z:
        return [seven_z, "x", "-y", "-p-", f"-o{out_dir}", "--", str(arc)]
    raise RuntimeError(f"No extraction tool found for: {archive.name}")


_MAX_ARCHIVE_ENTRIES = 50_000
# Zip-bomb guard: the declared uncompressed size may be at most this many times the size of
# the archive (all its parts). Game data compresses far less; bombs go well past 1000x.
MAX_COMPRESSION_RATIO = 100
_MIN_SIZE_BUDGET = 256 * 1024 * 1024
# Fixed limits (bytes) instead of the ratio; None = derive from the archive. Tests override these.
_MAX_ARCHIVE_FILE_BYTES: int | None = None
_MAX_ARCHIVE_TOTAL_BYTES: int | None = None
_SPLIT_PART_RE = re.compile(r"^(?P<base>.+?)(?:\.part\d+\.rar|\.(?:7z|zip|rar)\.\d{3}|\.\d{3})$", re.IGNORECASE)


def archive_parts(archive: Path) -> list[Path]:
    """All volumes of a split archive (game.part1.rar, game.7z.001, ...), or just the archive."""
    m = _SPLIT_PART_RE.match(archive.name)
    if not m:
        return [archive]
    base = m.group("base")
    low = archive.name.lower()
    if re.search(r"\.part\d+\.rar$", low):
        pattern = re.compile(re.escape(base) + r"\.part\d+\.rar$", re.IGNORECASE)
    else:
        suffix = re.search(r"((?:\.(?:7z|zip|rar))?)\.\d{3}$", archive.name, re.IGNORECASE)
        ext = suffix.group(1) if suffix else ""
        pattern = re.compile(re.escape(base + ext) + r"\.\d{3}$", re.IGNORECASE)
    parts = sorted(p for p in archive.parent.iterdir() if p.is_file() and not p.is_symlink() and pattern.match(p.name))
    return parts or [archive]


def archive_set_size(archive: Path) -> int:
    """Total size of an archive and all of its split volumes."""
    return sum(p.stat().st_size for p in archive_parts(archive))


def _archive_limits(archive: Path) -> tuple[int, int]:
    """(per-file limit, total limit) for the uncompressed contents of archive."""
    ratio_budget = max(archive_set_size(archive) * MAX_COMPRESSION_RATIO, _MIN_SIZE_BUDGET)
    total = _MAX_ARCHIVE_TOTAL_BYTES if _MAX_ARCHIVE_TOTAL_BYTES is not None else ratio_budget
    per_file = _MAX_ARCHIVE_FILE_BYTES if _MAX_ARCHIVE_FILE_BYTES is not None else total
    return per_file, total


def _too_big_error(total: int) -> RuntimeError:
    return RuntimeError(
        "Archive exceeds maximum allowed total uncompressed size "
        f"({total / (1024 ** 3):.1f} GiB declared; more than {MAX_COMPRESSION_RATIO}x the archive size)."
    )
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")


def _extract_zip_safe(
    archive: Path,
    dest_dir: Path,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> None:
    """Extract a .zip archive using Python zipfile with strict path-traversal and size limits."""
    dest_resolved = dest_dir.resolve()
    total_written = 0
    max_file, max_total = _archive_limits(archive)
    with zipfile.ZipFile(archive, "r") as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("Archive exceeds maximum allowed entry count.")
        declared = sum(max(0, info.file_size) for info in infos)
        if declared > max_total:
            raise _too_big_error(declared)
        total_entries = max(len(infos), 1)
        for idx, info in enumerate(infos):
            if cancel_flag and cancel_flag():
                raise RuntimeError("Extraction cancelled by user.")
            member_name = info.filename.replace("\\", "/")
            if not member_name:
                continue
            member_path = Path(member_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe archive member path: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            raw_target = dest_resolved / member_path
            if raw_target.is_symlink():
                raw_target.unlink()
            target_path = raw_target.resolve()
            if target_path != dest_resolved and dest_resolved not in target_path.parents:
                raise RuntimeError(f"Archive member escapes destination: {info.filename}")
            if info.is_dir():
                if target_path.is_symlink():
                    raise RuntimeError(f"Unsafe symlink at destination directory: {info.filename}")
                target_path.mkdir(parents=True, exist_ok=True)
            else:
                if info.file_size > max_file:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {info.filename}")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if target_path.is_symlink():
                    target_path.unlink()
                file_written = 0
                with zf.open(info, "r") as src, open(target_path, "wb") as dst:
                    while True:
                        if cancel_flag and cancel_flag():
                            raise RuntimeError("Extraction cancelled by user.")
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        file_written += len(chunk)
                        total_written += len(chunk)
                        if file_written > max_file or file_written > info.file_size:
                            raise RuntimeError(
                                f"Archive member exceeds maximum allowed size: {info.filename}"
                            )
                        if total_written > max_total:
                            raise _too_big_error(total_written)
                        dst.write(chunk)
            if on_progress:
                pct = min(95, 10 + int(((idx + 1) * 85) / total_entries))
                on_progress(pct, f"Extracting {archive.name}...")


def _is_tar_archive(name_lower: str) -> bool:
    return any(name_lower.endswith(ext) for ext in _TAR_SUFFIXES)


def _extract_tar_safe(
    archive: Path,
    dest_dir: Path,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> None:
    """Extract a tar archive streamingly with size/count limits, skipping symlinks/hardlinks/specials."""
    dest_resolved = dest_dir.resolve()
    total_bytes = 0
    max_file, max_total = _archive_limits(archive)
    has_data_filter = hasattr(tarfile, "data_filter")
    with tarfile.open(archive, "r:*") as tf:
        for idx, member in enumerate(tf):
            if cancel_flag and cancel_flag():
                raise RuntimeError("Extraction cancelled by user.")
            if idx >= _MAX_ARCHIVE_ENTRIES:
                raise RuntimeError("Archive exceeds maximum allowed entry count.")
            norm_name = member.name.replace("\\", "/")
            member_path = PurePosixPath(norm_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe archive member path: {member.name}")
            target_path = (dest_resolved / member.name).resolve()
            if target_path != dest_resolved and dest_resolved not in target_path.parents:
                raise RuntimeError(f"Archive member escapes destination: {member.name}")
            if (
                member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or member.ischr()
                or member.isblk()
            ):
                continue
            if member.isfile():
                if member.size > max_file:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {member.name}")
                total_bytes += max(0, int(member.size))
                if total_bytes > max_total:
                    raise _too_big_error(total_bytes)
            if has_data_filter:
                tf.extract(member, dest_dir, filter="data")
            else:
                tf.extract(member, dest_dir)
            if on_progress and (idx % 50 == 0):
                on_progress(50, f"Extracting {archive.name}...")

        if on_progress:
            on_progress(95, f"Extracting {archive.name}...")


def _check_unsafe_member_path(raw_path: str) -> None:
    norm = raw_path.replace("\\", "/").strip()
    if not norm:
        return
    p = PurePosixPath(norm)
    if p.is_absolute() or (len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha()) or ".." in p.parts:
        raise RuntimeError(f"Unsafe archive member path: {raw_path}")


def _precheck_7z_archive(seven_z_bin: str, archive: Path) -> int:
    """List .7z/.rar archive via 7z l -slt and abort if symlinks or traversal paths are present."""
    max_file, max_total = _archive_limits(archive)
    try:
        proc = subprocess.run(
            [seven_z_bin, "l", "-slt", "-p-", "--", str(archive)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out inspecting {archive.name}.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"Failed to inspect {archive.name} (code {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    in_entries = False
    entry_count = 0
    total_bytes = 0
    for raw_line in proc.stdout.splitlines():
        if not in_entries:
            if raw_line.strip().startswith("----------"):
                in_entries = True
            continue
        if "=" not in raw_line:
            continue
        key, _, val = raw_line.partition("=")
        key_low = key.strip().lower()
        val_str = val.strip()
        if key_low == "path":
            entry_count += 1
            if entry_count > _MAX_ARCHIVE_ENTRIES:
                raise RuntimeError("Archive exceeds maximum allowed entry count.")
            _check_unsafe_member_path(val_str)
        elif key_low == "size" and val_str.isdigit():
            size = int(val_str)
            if size > max_file:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {size}")
            total_bytes += size
            if total_bytes > max_total:
                raise _too_big_error(total_bytes)
        elif key_low in ("symbolic link", "hard link") and val_str:
            raise RuntimeError(f"Unsafe link in archive {archive.name}: {val_str}")
        elif key_low == "mode" and val_str.startswith("l"):
            raise RuntimeError(f"Unsafe symlink in archive {archive.name}.")
        elif key_low == "attributes":
            tokens = val_str.split()
            if any(
                ("L" in tok and not tok.startswith("l")) or (len(tok) == 10 and tok.startswith("l"))
                for tok in tokens
            ):
                raise RuntimeError(f"Unsafe symlink attribute in archive {archive.name}.")
    return total_bytes


def _precheck_unrar_archive(unrar_bin: str, archive: Path) -> int:
    """List .rar archive via unrar vt and abort if symlinks or traversal paths are present."""
    max_file, max_total = _archive_limits(archive)
    try:
        proc = subprocess.run(
            [unrar_bin, "vt", "-p-", "--", str(archive)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out inspecting {archive.name}.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"Failed to inspect {archive.name} (code {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    entry_count = 0
    total_bytes = 0
    for raw_line in proc.stdout.splitlines():
        if ":" not in raw_line:
            continue
        key, _, val = raw_line.partition(":")
        key_low = key.strip().lower()
        val_str = val.strip()
        if key_low == "name":
            entry_count += 1
            if entry_count > _MAX_ARCHIVE_ENTRIES:
                raise RuntimeError("Archive exceeds maximum allowed entry count.")
            _check_unsafe_member_path(val_str)
        elif key_low == "size" and val_str.isdigit():
            size = int(val_str)
            if size > max_file:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {size}")
            total_bytes += size
            if total_bytes > max_total:
                raise _too_big_error(total_bytes)
        elif key_low == "type" and ("link" in val_str.lower()):
            raise RuntimeError(f"Unsafe link in archive {archive.name}: {val_str}")
        elif key_low == "target" and val_str:
            raise RuntimeError(f"Unsafe link target in archive {archive.name}: {val_str}")
        elif key_low == "attributes" and val_str.startswith("l"):
            raise RuntimeError(f"Unsafe symlink in archive {archive.name}.")
    return total_bytes


def _precheck_tar_cli_archive(tar_bin: str, archive: Path) -> int:
    """List tar archive via `tar -tvf` and abort if symlinks, hardlinks, specials, or traversal exist."""
    max_file, max_total = _archive_limits(archive)
    try:
        proc = subprocess.run(
            [tar_bin, "-tvf", str(archive)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out inspecting {archive.name}.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"Failed to inspect {archive.name} (code {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    total_bytes = 0
    for idx, raw_line in enumerate(proc.stdout.splitlines()):
        if idx >= _MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("Archive exceeds maximum allowed entry count.")
        line = raw_line.strip()
        if not line:
            continue
        type_ch = line[0]
        if type_ch in ("l", "h", "c", "b", "p", "D"):
            raise RuntimeError(f"Unsafe link or special file in archive {archive.name}: {line[:120]}")
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        if type_ch != "d" and parts[2].isdigit():
            size = int(parts[2])
            if size > max_file:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {parts[5]}")
            total_bytes += size
            if total_bytes > max_total:
                raise _too_big_error(total_bytes)
        entry_part = parts[5]
        if " -> " in entry_part or " link to " in entry_part:
            raise RuntimeError(f"Unsafe link in archive {archive.name}: {entry_part[:120]}")
        _check_unsafe_member_path(entry_part)
    return total_bytes


def _precheck_unzip_archive(unzip_bin: str, archive: Path) -> int:
    """List zip archive via `unzip -Z -l` (or `-Z -1`) and reject symlinks, oversized entries, and traversal."""
    max_file, max_total = _archive_limits(archive)
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                infos = zf.infolist()
                if len(infos) > _MAX_ARCHIVE_ENTRIES:
                    raise RuntimeError("Archive exceeds maximum allowed entry count.")
                total_bytes = sum(max(0, info.file_size) for info in infos)
                if total_bytes > max_total:
                    raise _too_big_error(total_bytes)
                for info in infos:
                    if info.file_size > max_file:
                        raise RuntimeError(f"Archive member exceeds maximum allowed size: {info.filename}")
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise RuntimeError(f"Unsafe symlink in archive {archive.name}: {info.filename}")
                    _check_unsafe_member_path(info.filename)
            return total_bytes
        except zipfile.BadZipFile:
            pass
    try:
        proc = subprocess.run(
            [unzip_bin, "-Z", "-l", "--", str(archive)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out inspecting {archive.name}.") from exc
    if proc.returncode == 0:
        entry_count = 0
        total_bytes = 0
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 8)
            if len(parts) == 9 and parts[3].isdigit():
                mode = parts[0]
                if mode.startswith("l"):
                    raise RuntimeError(f"Unsafe symlink in archive {archive.name}: {parts[8]}")
                entry_count += 1
                if entry_count > _MAX_ARCHIVE_ENTRIES:
                    raise RuntimeError("Archive exceeds maximum allowed entry count.")
                size = int(parts[3])
                if size > max_file:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {parts[8]}")
                total_bytes += size
                if total_bytes > max_total:
                    raise _too_big_error(total_bytes)
                _check_unsafe_member_path(parts[8])
            elif len(parts) == 1:
                entry_count += 1
                if entry_count > _MAX_ARCHIVE_ENTRIES:
                    raise RuntimeError("Archive exceeds maximum allowed entry count.")
                _check_unsafe_member_path(parts[0])
        return total_bytes

    try:
        proc = subprocess.run(
            [unzip_bin, "-Z", "-1", "--", str(archive)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out inspecting {archive.name}.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"Failed to inspect {archive.name} (code {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    for idx, raw_line in enumerate(proc.stdout.splitlines()):
        if idx >= _MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("Archive exceeds maximum allowed entry count.")
        _check_unsafe_member_path(raw_line)
    return 0


def _precheck_cli_archive(cmd_bin: str, archive: Path) -> int:
    """Pre-list CLI-extracted archives to reject symlinks, hardlinks, and path traversal.

    Returns the declared uncompressed size (0 when the listing does not show sizes).
    """
    bin_name = Path(cmd_bin).name.lower()
    if bin_name.startswith("7z"):
        return _precheck_7z_archive(cmd_bin, archive)
    if bin_name == "unrar":
        return _precheck_unrar_archive(cmd_bin, archive)
    if bin_name == "tar":
        return _precheck_tar_cli_archive(cmd_bin, archive)
    if bin_name == "unzip":
        return _precheck_unzip_archive(cmd_bin, archive)
    return 0


def _merge_staging_dir(stage_dir: Path, dest_dir: Path) -> None:
    """Move files/directories from stage_dir into dest_dir without following symlinks."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    for root_s, dirs_s, files_s in os.walk(stage_dir, followlinks=False):
        rel = Path(root_s).relative_to(stage_dir)
        target_sub = dest_dir / rel
        if target_sub.is_symlink():
            target_sub.unlink()
        target_sub.mkdir(parents=True, exist_ok=True)
        if not target_sub.resolve().is_relative_to(dest_resolved):
            raise RuntimeError(f"Destination escapes target folder: {target_sub}")
        dirs_s[:] = [d for d in dirs_s if not (Path(root_s) / d).is_symlink()]
        for fname in files_s:
            src_f = Path(root_s) / fname
            if src_f.is_symlink():
                continue
            dst_f = target_sub / fname
            _move_file_replace(src_f, dst_f)


def extract_archive_to_dir(archive: Path, dest_dir: Path) -> None:
    """Extract an archive safely into dest_dir using staging for CLI extractors."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    low = archive.name.lower()
    if low.endswith(".zip"):
        _extract_zip_safe(archive, dest_dir)
        return
    if _is_tar_archive(low):
        try:
            _extract_tar_safe(archive, dest_dir)
            return
        except tarfile.CompressionError:
            if not low.endswith(".tar.zst"):
                raise

    with tempfile.TemporaryDirectory(prefix=".deckinstall_stage_", dir=str(dest_dir.parent)) as tmp_str:
        stage_dir = Path(tmp_str)
        if low.endswith(".7z"):
            seven_z = _find_7z_bin()
            if not seven_z:
                raise RuntimeError(
                    "No 7-Zip extraction tool found (tried 7z, 7zz, 7za). "
                    "Install 7zip/p7zip to extract .7z archives."
                )
            cmd = [seven_z, "x", "-y", "-p-", f"-o{stage_dir}", "--", str(archive)]
        else:
            cmd = _select_extract_cmd(archive, stage_dir)

        _precheck_cli_archive(cmd[0], archive)

        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Extraction timed out for {archive.name}.") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(
                f"Failed to extract {archive.name} (code {proc.returncode})"
                + (f": {detail}" if detail else ".")
            )
        _strip_symlinks_and_specials(stage_dir)
        _merge_staging_dir(stage_dir, dest_dir)


def _strip_symlinks_and_specials(root: Path) -> None:
    """Remove symlinks and special files from extracted staging directory before move."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        dp = Path(dirpath)
        for name in filenames:
            p = dp / name
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                p.unlink(missing_ok=True)
        for name in dirnames:
            p = dp / name
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                p.unlink(missing_ok=True)


def _set_aside(path: Path) -> Path:
    """Rename an existing file/folder to '<name>.deckinstall-old[.N]' instead of deleting it."""
    candidate = path.with_name(f"{path.name}.deckinstall-old")
    idx = 1
    while candidate.exists() or candidate.is_symlink():
        idx += 1
        candidate = path.with_name(f"{path.name}.deckinstall-old.{idx}")
    os.replace(path, candidate)
    logging.warning("Kept existing %s as %s (type clash with archive content)", path, candidate)
    return candidate


def _move_file_replace(src_f: Path, dst_f: Path) -> None:
    """Atomically replace dst_f with src_f on same filesystem, falling back to move."""
    if dst_f.is_symlink():
        dst_f.unlink()
    elif dst_f.is_dir():
        _set_aside(dst_f)
    try:
        os.replace(src_f, dst_f)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            shutil.move(str(src_f), str(dst_f))
        else:
            raise


def _unwrap_single_folder(temp_dir: Path, final_dir: Path) -> None:
    """Move extracted contents to final_dir, merging safely without following symlinks."""
    entries = list(temp_dir.iterdir())
    source_root = (
        entries[0]
        if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink()
        else temp_dir
    )
    if final_dir.is_symlink():
        raise RuntimeError(f"Destination folder cannot be a symlink: {final_dir}")
    final_dir.mkdir(parents=True, exist_ok=True)
    final_resolved = final_dir.resolve()
    for item in source_root.iterdir():
        if item.is_symlink():
            item.unlink(missing_ok=True)
            continue
        dest = final_dir / item.name
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists() and not dest.resolve().is_relative_to(final_resolved):
            raise RuntimeError(f"Destination escapes target folder: {dest}")
        if dest.exists():
            if item.is_dir() and dest.is_dir():
                for root_s, dirs_s, files_s in os.walk(item, followlinks=False):
                    rel = Path(root_s).relative_to(item)
                    target_sub = dest / rel
                    if target_sub.is_symlink():
                        target_sub.unlink()
                    elif target_sub.is_file():
                        _set_aside(target_sub)
                    target_sub.mkdir(parents=True, exist_ok=True)
                    if not target_sub.resolve().is_relative_to(final_resolved):
                        raise RuntimeError(f"Destination escapes target folder: {target_sub}")
                    for d in list(dirs_s):
                        src_d = Path(root_s) / d
                        if src_d.is_symlink():
                            dirs_s.remove(d)
                            src_d.unlink(missing_ok=True)
                    for f in files_s:
                        src_f = Path(root_s) / f
                        if src_f.is_symlink():
                            src_f.unlink(missing_ok=True)
                            continue
                        _move_file_replace(src_f, target_sub / f)
                shutil.rmtree(item)
                continue
            if dest.is_dir() or item.is_dir():
                # File/folder type clash: never delete existing user data, rename it aside.
                _set_aside(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))


def _drain_stream_lines(
    proc: subprocess.Popen[bytes],
    cancel_flag: Callable[[], bool] | None,
    on_line: Callable[[str], None],
    cancel_msg: str,
) -> int:
    """Read subprocess stdout non-blockingly so cancel_flag is checked every <=0.2s."""
    return runtime_manager.drain_stream_lines(proc, cancel_flag, on_line, cancel_msg)


def extract_archive(
    archive: Path,
    dest_root: Path,
    game_folder_name: str | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Extract an archive into dest_root/<game_folder_name> and return the target path.

    Free space is checked against the uncompressed size the archive declares (zip/tar headers,
    or the 7z/unrar/tar/unzip listing), falling back to twice the archive size.
    """
    low_name = archive.name.lower()
    if low_name.endswith(".zip") or _is_tar_archive(low_name):
        check_disk_space(dest_root, estimate_archive_uncompressed_size(archive))
    folder_name = sanitize_folder_name(game_folder_name or suggest_target_name(archive))
    final_dir = dest_root / folder_name
    dest_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="deckinstall_extract_", dir=str(dest_root)) as tmp:
        tmp_path = Path(tmp)
        low = archive.name.lower()
        if on_progress:
            on_progress(5, f"Extracting {archive.name}...")

        used_python_extractor = False
        if low.endswith(".zip"):
            _extract_zip_safe(archive, tmp_path, on_progress=on_progress, cancel_flag=cancel_flag)
            used_python_extractor = True
        elif _is_tar_archive(low):
            try:
                _extract_tar_safe(archive, tmp_path, on_progress=on_progress, cancel_flag=cancel_flag)
                used_python_extractor = True
            except tarfile.CompressionError:
                if not low.endswith(".tar.zst"):
                    raise

        if not used_python_extractor:
            cmd = _select_extract_cmd(archive, tmp_path)
            declared = _precheck_cli_archive(cmd[0], archive)
            check_disk_space(dest_root, declared if declared > 0 else archive_set_size(archive) * 2)

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pct = 10
            tail_lines: deque[str] = deque(maxlen=8)

            def _handle_line(line: str) -> None:
                nonlocal pct
                clean = line.strip()
                if clean:
                    tail_lines.append(clean[:140])
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    pct = min(95, max(pct, int(m.group(1))))
                elif pct < 90:
                    pct = min(90, pct + 1)
                if on_progress:
                    on_progress(pct, clean[:120] or f"Extracting {archive.name}...")

            try:
                rc = _drain_stream_lines(proc, cancel_flag, _handle_line, "Extraction cancelled by user.")
            finally:
                if proc.poll() is None and isinstance(getattr(proc, "pid", None), int):
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            proc.kill()
                            proc.wait(timeout=2)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                if proc.stdout is not None and hasattr(proc.stdout, "close"):
                    proc.stdout.close()
            if rc != 0:
                detail = f": {' | '.join(tail_lines)}" if tail_lines else ""
                raise RuntimeError(f"Failed to extract {archive.name} (code {rc}){detail}.")

        if on_progress:
            on_progress(96, "Organizing files...")
        _strip_symlinks_and_specials(tmp_path)
        _unwrap_single_folder(tmp_path, final_dir)

    if on_progress:
        on_progress(100, f"Completed in {final_dir}")
    return final_dir


def _collect_regular_files(root: Path) -> set[Path]:
    """Return regular non-symlink files under root without following symlinks."""
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for d in list(dirnames):
            if (dp / d).is_symlink():
                dirnames.remove(d)
        for f in filenames:
            p = dp / f
            if not p.is_symlink() and p.is_file():
                found.add(p)
    return found


_NON_GAME_EXE_PREFIXES = (
    "unins",
    "vcredist",
    "dxsetup",
    "ueprereq",
    "prereq",
    "crashreport",
    "crash",
    "redist",
    "setup",
)


def _is_non_game_helper_exe(path: Path) -> bool:
    """Return True if an executable stem matches known installer/redist/crash helpers."""
    stem = path.stem.lower()
    return any(stem.startswith(prefix) for prefix in _NON_GAME_EXE_PREFIXES)


class UnarcError(RuntimeError):
    """A repack's unarc.dll / ISDone.dll failed while unpacking its data."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"unarc.dll failed (code {code or '?'}): {detail}".strip())


_UNARC_RE = re.compile(r"(unarc\.dll|isdone\.dll).*?(error|failed|returned)", re.IGNORECASE)
_UNARC_CODE_RE = re.compile(r"code\s*:?\s*(-?\d+)", re.IGNORECASE)
_SYSTEM_DIRS = {"common files", "internet explorer", "windows media player", "windows nt", "uninstall information"}


# Drive letters of the throwaway installer prefix. Proton manages s: (game drive) and
# t: (Steam drive) itself on every run, so the installer gets letters Proton never touches.
INSTALLER_SOURCE_DRIVE = "i:"
INSTALLER_TARGET_DRIVE = "d:"
_NO_ROOT_DIR = ".deckinstall_no_root"
_DRIVE_RE = re.compile(r"^[a-z]:$")


class InstallerIsolationError(RuntimeError):
    """The throwaway installer prefix could not be cut off from / and the home folder."""


def installer_drives(installer_exe: Path, target_dir: Path) -> dict[str, Path]:
    """Drives the installer sees: its own folder (with setup-N.bin/.7z/MD5 parts) and the library."""
    return {
        INSTALLER_SOURCE_DRIVE: installer_exe.parent.resolve(),
        INSTALLER_TARGET_DRIVE: target_dir.parent.resolve(),
    }


def to_wine_path(path: Path, drives: dict[str, Path]) -> str:
    """Map a Linux path to the most specific installer drive holding it (D:\\Game, I:\\setup.exe)."""
    resolved = path.resolve()
    for letter, root in sorted(drives.items(), key=lambda kv: len(kv[1].parts), reverse=True):
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        return letter.upper() + "\\" + "\\".join(rel.parts)
    raise InstallerIsolationError(f"{path} is outside the installer drives")


def installer_arguments(installer_exe: Path, target_dir: Path) -> list[str]:
    """Command-line switches for the installer: Inno Setup gets /DIR= on the D: drive.

    Not used for repacks: FitGirl/DODI unpackers (ISDone/unarc) fail with "Unable to write
    data to disk" (-11) when the folder comes from /DIR=, but work when picked in the installer.
    """
    from .classify import is_inno_setup

    if not is_inno_setup(installer_exe):
        return []
    return [f"/DIR={to_wine_path(target_dir, installer_drives(installer_exe, target_dir))}"]


def _exposes_home(path: Path, home: Path) -> bool:
    """True if a drive pointing at path would show / or the home folder (or one of its parents)."""
    return path == Path("/") or home == path or home.is_relative_to(path)


def _drive_target(entry: Path) -> Path:
    return Path(os.path.realpath(entry))


def unsafe_prefix_drives(wine_pfx: Path, home: Path | None = None) -> list[str]:
    """Drive letters in wine_pfx/dosdevices that reach / or the home folder."""
    home_r = (home or Path.home()).resolve()
    dosdevices = wine_pfx / "dosdevices"
    try:
        entries = sorted(dosdevices.iterdir())
    except OSError:
        return []
    return [
        e.name for e in entries
        if _DRIVE_RE.match(e.name.lower()) and _exposes_home(_drive_target(e), home_r)
    ]


def isolate_installer_prefix(wine_pfx: Path, drives: dict[str, Path], home: Path | None = None) -> None:
    """Cut the throwaway installer prefix off from / and the home folder.

    Every drive letter (except c:) reaching / or home, plus z:, is removed; z: is then pointed at
    an empty folder inside the prefix, because Proton recreates z: -> / on every run when it is
    missing but leaves an existing one alone. The installer folder and the library get their own
    letters (drives). Done before the installer starts, so it never sees the rest of the disk.
    """
    from .i18n import tr

    home_r = (home or Path.home()).resolve()
    for root in drives.values():
        if _exposes_home(root, home_r):
            raise InstallerIsolationError(tr("install.isolation_unsafe_folder", path=str(root)))
    dosdevices = wine_pfx / "dosdevices"
    empty_root = wine_pfx / _NO_ROOT_DIR
    try:
        dosdevices.mkdir(parents=True, exist_ok=True)
        (wine_pfx / "drive_c").mkdir(parents=True, exist_ok=True)
        empty_root.mkdir(exist_ok=True)
        c_drive = dosdevices / "c:"
        if not c_drive.exists() and not c_drive.is_symlink():
            c_drive.symlink_to("../drive_c")
        for entry in list(dosdevices.iterdir()):
            name = entry.name.lower()
            if not _DRIVE_RE.match(name) or name == "c:":
                continue
            if name == "z:" or name in drives or _exposes_home(_drive_target(entry), home_r):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        (dosdevices / "z:").symlink_to(empty_root)
        for letter, root in drives.items():
            (dosdevices / letter).symlink_to(root)
    except OSError as exc:
        raise InstallerIsolationError(tr("install.isolation_failed", err=str(exc))) from exc
    left = unsafe_prefix_drives(wine_pfx, home_r)
    if left:
        raise InstallerIsolationError(tr("install.isolation_failed", err=", ".join(left)))


def _installer_prefix_parent(target_dir: Path) -> Path:
    """Folder for the throwaway installer prefix; never on exFAT/FAT/NTFS, where Wine cannot run."""
    from .library import NON_UNIX_FS, PREFIXES, fs_type_of, internal_library_root

    parent = target_dir.parent
    if fs_type_of(parent).lower() in NON_UNIX_FS:
        parent = internal_library_root() / PREFIXES
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _repack_data_size(installer_exe: Path) -> int:
    total = 0
    for p in installer_exe.parent.iterdir():
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total


def _installer_command(
    installer_exe: Path,
    args: list[str],
    tmp_pfx: Path,
    on_log: Callable[[str], None] | None,
    cancel_flag: Callable[[], bool] | None,
) -> tuple[list[str], dict[str, str], Path]:
    """Return (command, env, wine prefix): Steam Proton, else umu-run + GE-Proton, else system wine."""
    steam_root = find_steam_root()
    proton = find_proton_binary(steam_root) if steam_root is not None else None
    if steam_root is not None and proton is not None:
        env = os.environ.copy()
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        env["STEAM_COMPAT_DATA_PATH"] = str(tmp_pfx)
        if on_log:
            on_log(f"Starting {installer_exe.name} via Proton.")
        return [str(proton), "run", str(installer_exe), *args], env, tmp_pfx / "pfx"
    if cancel_flag and cancel_flag():
        raise RuntimeError("Installer cancelled by user.")
    try:
        umu_run = runtime_manager.ensure_umu_run()
        resolved_proton = runtime_manager.ensure_ge_proton11()
        runtime_manager.validate_steamrt4_integrity()
        env = runtime_manager.build_umu_env(tmp_pfx, resolved_proton)
        if on_log:
            on_log(f"Starting {installer_exe.name} via umu-run.")
        return [str(umu_run), str(installer_exe), *args], env, tmp_pfx
    except Exception as exc:
        wine = shutil.which("wine")
        if wine is None:
            raise
        logging.warning("umu-run/GE-Proton unavailable (%s); using system wine", exc)
        env = os.environ.copy()
        env["WINEPREFIX"] = str(tmp_pfx)
        if on_log:
            on_log(f"umu-run is unavailable ({exc}); starting {installer_exe.name} with the system wine.")
        return [wine, str(installer_exe), *args], env, tmp_pfx


DRIVE_C_GAME_PARENTS = ("Games", "GOG Games", "Program Files", "Program Files (x86)")


def find_games_in_drive_c(drive_c: Path) -> tuple[list[Path], list[Path]]:
    """Return (game folders, helper-only folders) found under drive_c's Games/Program Files dirs.

    Windows system folders and symlinks are skipped; a folder counts as a game when it holds
    an .exe that is not a known helper (redist, crash handler, ...).
    """
    picked: list[Path] = []
    fallback: list[Path] = []
    if drive_c.is_symlink() or not drive_c.is_dir():
        return picked, fallback
    for sub in DRIVE_C_GAME_PARENTS:
        base = drive_c / sub
        if not base.is_dir() or base.is_symlink():
            continue
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for cand in children:
            if cand.is_symlink() or not cand.is_dir() or cand.name.lower() in _SYSTEM_DIRS:
                continue
            new_exes = [p for p in _collect_regular_files(cand) if p.suffix.lower() == ".exe"]
            if not new_exes:
                continue
            if any(not _is_non_game_helper_exe(p) for p in new_exes):
                picked.append(cand)
            else:
                fallback.append(cand)
    return picked, fallback


def _adopt_from_drive_c(drive_c: Path, target_dir: Path) -> bool:
    """Move a game the installer put inside the prefix (ignoring /DIR=) into target_dir."""
    picked, fallback = find_games_in_drive_c(drive_c)
    for cand in picked or fallback:
        dest = target_dir / cand.name
        if dest.exists() or dest.is_symlink():
            copy_dir_no_symlinks(cand, dest)
        else:
            move_dir_safely(cand, dest)
    return bool(picked or fallback)


def _installed_elsewhere_roots(target_dir: Path) -> list[Path]:
    """Folders where a user-picked install folder usually ends up (outside the prefix)."""
    home = Path.home()
    roots = [target_dir.parent, home / "Games", home]
    out: list[Path] = []
    for r in roots:
        if r.is_dir() and not r.is_symlink() and r not in out:
            out.append(r)
    return out


def _adopt_installed_elsewhere(
    new_dirs: set[Path], target_dir: Path, started: float, on_log: Callable[[str], None] | None
) -> bool:
    """Move the one game folder an installer created outside target_dir into target_dir."""
    games: list[Path] = []
    for cand in sorted(new_dirs):
        try:
            if cand.is_symlink() or cand.stat().st_mtime < started - 5:
                continue
        except OSError:
            continue
        if any(not _is_non_game_helper_exe(p) for p in _collect_regular_files(cand) if p.suffix.lower() == ".exe"):
            games.append(cand)
    if len(games) != 1:
        return False  # none, or ambiguous: never guess between several new folders
    found = games[0]
    if any(target_dir.iterdir()):
        return False
    if on_log:
        on_log(f"Game installed in {found}; moving it to {target_dir}")
    target_dir.rmdir()
    move_dir_safely(found, target_dir)
    return True


def run_windows_installer(
    installer_exe: Path,
    target_dir: Path,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
    *,
    repack: bool = False,
    keep_prefix: bool | None = None,
) -> Path:
    """Run a Windows installer in a throwaway prefix and return the folder holding the game.

    The installer prefix sees no Z: (/) and no drive reaching the home folder: only C:, I:
    (installer folder, with its setup-N.bin/.7z/MD5 parts) and D: (target_dir's parent).
    Inno Setup installers (GOG) get /DIR=D:\\<target_dir name>, so the game lands in the
    library directly. Repacks (FitGirl/DODI) run as they are, with no /DIR=
    (their unarc unpacker fails with /DIR=): the user picks the folder, and a folder created
    elsewhere is moved into target_dir afterwards. If an installer ignores /DIR=, the game is
    moved out of the prefix's drive_c. Repacks get twice their size checked,
    WINE_LARGE_ADDRESS_AWARE=0 while installing, a specific error when unarc.dll fails, and
    their installer prefix is discarded. Other installers keep it as .deckinstall_installer_pfx
    (adopted when added to Steam).
    """
    data_size = _repack_data_size(installer_exe) if repack else installer_exe.stat().st_size
    check_disk_space(target_dir, max(data_size * 2, 512 * 1024 * 1024))
    keep = (not repack) if keep_prefix is None else keep_prefix
    args = [] if repack else installer_arguments(installer_exe, target_dir)
    drives = installer_drives(installer_exe, target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = _collect_regular_files(target_dir)
    watch_roots = _installed_elsewhere_roots(target_dir)
    dirs_before = _child_dirs(watch_roots)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="deckinstall_pfx_", dir=str(_installer_prefix_parent(target_dir))) as tmp:
        tmp_pfx = Path(tmp)
        cmd, env, inst_pfx = _installer_command(installer_exe, args, tmp_pfx, on_log, cancel_flag)
        if repack:
            env["WINE_LARGE_ADDRESS_AWARE"] = "0"
        # Proton must not map s: (game drive) / t: (Steam drive) to a mount above the installer.
        env["PROTON_SET_GAME_DRIVE"] = "0"
        env["PROTON_SET_STEAM_DRIVE"] = "0"
        try:
            isolate_installer_prefix(inst_pfx, drives)
        except InstallerIsolationError as exc:
            logging.error("Installer prefix isolation failed: %s", exc)
            if on_log:
                on_log(str(exc))
            raise
        if on_log:
            on_log(
                f"Isolated installer prefix: {INSTALLER_SOURCE_DRIVE.upper()} = {drives[INSTALLER_SOURCE_DRIVE]}, "
                f"{INSTALLER_TARGET_DRIVE.upper()} = {drives[INSTALLER_TARGET_DRIVE]}"
            )
            on_log(f"Destination: {to_wine_path(target_dir, drives)}")
        unarc_hits: list[str] = []

        def _handle_inst_line(line: str) -> None:
            text = line.strip()
            if not text:
                return
            if _UNARC_RE.search(text):
                code = _UNARC_CODE_RE.search(text)
                unarc_hits.append(code.group(1) if code else "")
            if on_log:
                on_log(text[:140])

        proc = subprocess.Popen(
            cmd,
            cwd=str(installer_exe.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = _drain_stream_lines(proc, cancel_flag, _handle_inst_line, "Installer cancelled by user.")
        finally:
            if proc.poll() is None and isinstance(getattr(proc, "pid", None), int):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if proc.stdout is not None and hasattr(proc.stdout, "close"):
                proc.stdout.close()
        leaked = unsafe_prefix_drives(inst_pfx)
        if leaked:
            logging.warning("Installer prefix drives reached / or home after the run: %s", ", ".join(leaked))
        if unarc_hits:
            code = next((c for c in unarc_hits if c), "")
            raise UnarcError(code, f"installer exit code {rc}")
        if rc != 0:
            if repack:
                raise UnarcError("", f"installer exit code {rc}")
            raise RuntimeError(f"Installer exited with code {rc}.")

        if not (_collect_regular_files(target_dir) - pre_existing):
            new_dirs = {
                d for d in _child_dirs(watch_roots) - dirs_before
                if d != target_dir and d != tmp_pfx and not d.name.startswith("deckinstall_pfx_")
            }
            moved = _adopt_installed_elsewhere(new_dirs, target_dir, started, on_log)
            if not moved and not _adopt_from_drive_c(inst_pfx / "drive_c", target_dir):
                raise RuntimeError(f"No files were installed in {target_dir}.")
        game_root = game_root_from_new_files(target_dir, _collect_regular_files(target_dir) - pre_existing)
        saved_pfx = target_dir / ".deckinstall_installer_pfx"
        if keep and (inst_pfx / "drive_c").is_dir() and not inst_pfx.is_symlink():
            if saved_pfx.is_symlink():
                saved_pfx.unlink()
            elif saved_pfx.exists():
                shutil.rmtree(saved_pfx, ignore_errors=True)
            if not saved_pfx.exists() and not saved_pfx.is_symlink():
                copy_dir_no_symlinks(inst_pfx, saved_pfx)
    return game_root


def game_root_from_new_files(target_dir: Path, new_files: set[Path]) -> Path:
    """Folder holding what an installer just wrote: target_dir, or its only new subfolder."""
    tops: set[str] = set()
    for f in new_files:
        try:
            parts = f.relative_to(target_dir).parts
        except ValueError:
            continue
        if len(parts) == 1:
            return target_dir  # files at the top level: target_dir is the game folder
        tops.add(parts[0])
    if len(tops) == 1:
        only = target_dir / next(iter(tops))
        if only.is_dir() and not only.is_symlink():
            return only
    return target_dir


def _native_install_roots() -> list[Path]:
    """Folders where Linux .sh installers usually put games when run without a destination."""
    home = Path.home()
    return [home / "GOG Games", home / "Games", home]


def _child_dirs(roots: list[Path]) -> set[Path]:
    out: set[Path] = set()
    for root in roots:
        try:
            out.update(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
    return out


class ToolMissingError(RuntimeError):
    """Raised when an optional external tool (e.g. innoextract) is not installed."""


def _installer_data_size(installer: Path) -> int:
    """Size of an installer plus the setup-N.bin parts that ship next to it."""
    total = installer.stat().st_size
    stem = installer.stem.lower()
    for part in installer.parent.glob("*.bin"):
        if part.is_file() and not part.is_symlink() and part.name.lower().startswith(stem):
            total += part.stat().st_size
    return total


def extract_gog_installer(
    installer: Path,
    dest_root: Path,
    game_folder_name: str,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Unpack a GOG Inno Setup installer with innoextract (no Wine needed) into dest_root/<name>."""
    tool = shutil.which("innoextract")
    if tool is None:
        raise ToolMissingError("innoextract is not installed")
    check_disk_space(dest_root, _installer_data_size(installer) * 2)
    final_dir = dest_root / sanitize_folder_name(game_folder_name)
    dest_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deckinstall_gog_", dir=str(dest_root)) as tmp:
        tmp_path = Path(tmp)
        if on_progress:
            on_progress(5, f"Unpacking {installer.name} with innoextract...")
        cmd = [tool, "--gog", "--exclude-temp", "--silent", "--output-dir", str(tmp_path), str(installer)]
        proc = subprocess.Popen(
            cmd,
            cwd=str(installer.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        def _line(line: str) -> None:
            if on_progress and line.strip():
                on_progress(50, line.strip()[:120])

        rc = _drain_stream_lines(proc, cancel_flag, _line, "Extraction cancelled by user.")
        if rc != 0:
            raise RuntimeError(f"innoextract failed for {installer.name} (code {rc}).")
        root = tmp_path / "app" if (tmp_path / "app").is_dir() else tmp_path
        for junk in ("tmp", "__redist", "commonappdata"):
            junk_dir = root / junk
            if junk_dir.is_dir() and not junk_dir.is_symlink():
                shutil.rmtree(junk_dir, ignore_errors=True)
        if not any(root.iterdir()):
            raise RuntimeError(f"innoextract produced no files for {installer.name}.")
        if on_progress:
            on_progress(96, "Organizing files...")
        _strip_symlinks_and_specials(root)
        _unwrap_single_folder(root, final_dir)
    if on_progress:
        on_progress(100, f"Completed in {final_dir}")
    return final_dir


def _is_gog_linux_installer(script: Path) -> bool:
    try:
        return zipfile.is_zipfile(script) and any(
            n.startswith("data/noarch/") for n in zipfile.ZipFile(script).namelist()[:2000]
        )
    except (OSError, zipfile.BadZipFile):
        return False


def install_native_sh(
    script: Path,
    dest_root: Path,
    game_folder_name: str,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Install a Linux .sh installer into dest_root/<name>.

    GOG Linux installers (makeself + MojoSetup) carry a zip with the game under data/noarch:
    that is unpacked directly, with no GUI. Any other .sh is run with dest_root/<name> as its
    working directory.
    """
    final_dir = dest_root / sanitize_folder_name(game_folder_name)
    dest_root.mkdir(parents=True, exist_ok=True)
    if _is_gog_linux_installer(script):
        check_disk_space(dest_root, script.stat().st_size * 2)
        with tempfile.TemporaryDirectory(prefix="deckinstall_sh_", dir=str(dest_root)) as tmp:
            tmp_path = Path(tmp)
            _extract_zip_safe(script, tmp_path, on_progress=on_progress, cancel_flag=cancel_flag)
            root = tmp_path / "data" / "noarch"
            if on_progress:
                on_progress(96, "Organizing files...")
            _strip_symlinks_and_specials(root)
            _unwrap_single_folder(root, final_dir)
        start = final_dir / "start.sh"
        if start.is_file():
            start.chmod(start.stat().st_mode | stat.S_IXUSR)
        if on_progress:
            on_progress(100, f"Completed in {final_dir}")
        return final_dir

    final_dir.mkdir(parents=True, exist_ok=True)
    roots = _native_install_roots()
    dirs_before = _child_dirs(roots)
    if on_progress:
        on_progress(10, f"Running {script.name}...")
    proc = subprocess.Popen(
        ["sh", str(script.resolve())],
        cwd=str(final_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def _line(line: str) -> None:
        if on_progress and line.strip():
            on_progress(50, line.strip()[:140])

    rc = _drain_stream_lines(proc, cancel_flag, _line, "Installer cancelled by user.")
    if rc != 0:
        raise RuntimeError(f"{script.name} exited with code {rc}.")
    result = final_dir
    if not _collect_regular_files(final_dir):
        # The installer chose its own folder (e.g. ~/GOG Games/<game>): find the one it created.
        created = sorted(_child_dirs(roots) - dirs_before - {final_dir})
        if len(created) == 1:
            result = created[0]
            try:
                final_dir.rmdir()
            except OSError:
                pass
    if on_progress:
        on_progress(100, f"Completed in {result}")
    return result
