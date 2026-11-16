"""Archive extraction and Windows setup.exe runner via Proton."""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from . import runtime_manager
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
                    return _MAX_ARCHIVE_TOTAL_BYTES + 1
                total = sum(info.file_size for info in infos)
                if total > 0:
                    return total
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive, "r:*") as tf:
                total = 0
                for idx, member in enumerate(tf):
                    if idx >= _MAX_ARCHIVE_ENTRIES:
                        return _MAX_ARCHIVE_TOTAL_BYTES + 1
                    if member.isfile():
                        total += max(0, int(member.size))
                if total > 0:
                    return total
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, ValueError):
        pass
    return archive.stat().st_size * 2


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


_MAX_ZIP_FILE_BYTES = 1024 * 1024 * 1024  # 1 GiB per-file limit
_MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB total uncompressed limit
_MAX_ARCHIVE_ENTRIES = 50_000
_MAX_ARCHIVE_FILE_BYTES = _MAX_ZIP_FILE_BYTES
_MAX_ARCHIVE_TOTAL_BYTES = _MAX_ZIP_TOTAL_BYTES
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
    with zipfile.ZipFile(archive, "r") as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("Archive exceeds maximum allowed entry count.")
        if sum(max(0, info.file_size) for info in infos) > _MAX_ZIP_TOTAL_BYTES:
            raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
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
                if info.file_size > _MAX_ZIP_FILE_BYTES:
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
                        if file_written > _MAX_ZIP_FILE_BYTES:
                            raise RuntimeError(
                                f"Archive member exceeds maximum allowed size: {info.filename}"
                            )
                        if total_written > _MAX_ZIP_TOTAL_BYTES:
                            raise RuntimeError(
                                "Archive exceeds maximum allowed total uncompressed size."
                            )
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
                if member.size > _MAX_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {member.name}")
                total_bytes += max(0, int(member.size))
                if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
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


def _precheck_7z_archive(seven_z_bin: str, archive: Path) -> None:
    """List .7z/.rar archive via 7z l -slt and abort if symlinks or traversal paths are present."""
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
            if size > _MAX_ARCHIVE_FILE_BYTES:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {size}")
            total_bytes += size
            if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
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


def _precheck_unrar_archive(unrar_bin: str, archive: Path) -> None:
    """List .rar archive via unrar vt and abort if symlinks or traversal paths are present."""
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
            if size > _MAX_ARCHIVE_FILE_BYTES:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {size}")
            total_bytes += size
            if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
        elif key_low == "type" and ("link" in val_str.lower()):
            raise RuntimeError(f"Unsafe link in archive {archive.name}: {val_str}")
        elif key_low == "target" and val_str:
            raise RuntimeError(f"Unsafe link target in archive {archive.name}: {val_str}")
        elif key_low == "attributes" and val_str.startswith("l"):
            raise RuntimeError(f"Unsafe symlink in archive {archive.name}.")


def _precheck_tar_cli_archive(tar_bin: str, archive: Path) -> None:
    """List tar archive via `tar -tvf` and abort if symlinks, hardlinks, specials, or traversal exist."""
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
            if size > _MAX_ARCHIVE_FILE_BYTES:
                raise RuntimeError(f"Archive member exceeds maximum allowed size: {parts[5]}")
            total_bytes += size
            if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
        entry_part = parts[5]
        if " -> " in entry_part or " link to " in entry_part:
            raise RuntimeError(f"Unsafe link in archive {archive.name}: {entry_part[:120]}")
        _check_unsafe_member_path(entry_part)


def _precheck_unzip_archive(unzip_bin: str, archive: Path) -> None:
    """List zip archive via `unzip -Z -l` (or `-Z -1`) and reject symlinks, oversized entries, and traversal."""
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                infos = zf.infolist()
                if len(infos) > _MAX_ARCHIVE_ENTRIES:
                    raise RuntimeError("Archive exceeds maximum allowed entry count.")
                total_bytes = sum(max(0, info.file_size) for info in infos)
                if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
                for info in infos:
                    if info.file_size > _MAX_ARCHIVE_FILE_BYTES:
                        raise RuntimeError(f"Archive member exceeds maximum allowed size: {info.filename}")
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise RuntimeError(f"Unsafe symlink in archive {archive.name}: {info.filename}")
                    _check_unsafe_member_path(info.filename)
            return
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
                if size > _MAX_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {parts[8]}")
                total_bytes += size
                if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
                _check_unsafe_member_path(parts[8])
            elif len(parts) == 1:
                entry_count += 1
                if entry_count > _MAX_ARCHIVE_ENTRIES:
                    raise RuntimeError("Archive exceeds maximum allowed entry count.")
                _check_unsafe_member_path(parts[0])
        return

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


def _precheck_cli_archive(cmd_bin: str, archive: Path) -> None:
    """Pre-list CLI-extracted archives to reject symlinks, hardlinks, and path traversal."""
    bin_name = Path(cmd_bin).name.lower()
    if bin_name.startswith("7z"):
        _precheck_7z_archive(cmd_bin, archive)
    elif bin_name == "unrar":
        _precheck_unrar_archive(cmd_bin, archive)
    elif bin_name == "tar":
        _precheck_tar_cli_archive(cmd_bin, archive)
    elif bin_name == "unzip":
        _precheck_unzip_archive(cmd_bin, archive)


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


def _move_file_replace(src_f: Path, dst_f: Path) -> None:
    """Atomically replace dst_f with src_f on same filesystem, falling back to move."""
    if dst_f.is_symlink():
        dst_f.unlink()
    elif dst_f.is_dir():
        shutil.rmtree(dst_f)
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
                    if target_sub.is_symlink() or target_sub.is_file():
                        target_sub.unlink()
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
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))


def _kill_proc_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the subprocess group when start_new_session=True, falling back to proc."""
    runtime_manager.kill_proc_group(proc, sig)


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
    """Extract an archive into dest_root/<game_folder_name> and return the target path."""
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
            _precheck_cli_archive(cmd[0], archive)

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pct = 10

            def _handle_line(line: str) -> None:
                nonlocal pct
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    pct = min(95, max(pct, int(m.group(1))))
                elif pct < 90:
                    pct = min(90, pct + 1)
                if on_progress:
                    on_progress(pct, line.strip()[:120] or f"Extracting {archive.name}...")

            rc = _drain_stream_lines(proc, cancel_flag, _handle_line, "Extraction cancelled by user.")
            if rc != 0:
                raise RuntimeError(f"Failed to extract {archive.name} (code {rc}).")

        if on_progress:
            on_progress(96, "Organizing files...")
        _strip_symlinks_and_specials(tmp_path)
        _unwrap_single_folder(tmp_path, final_dir)

    if on_progress:
        on_progress(100, f"Completed in {final_dir}")
    return final_dir


def _copy_dir_no_symlinks(src_dir: Path, dst_dir: Path) -> None:
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


def run_windows_installer(
    installer_exe: Path,
    target_dir: Path,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Run a Windows setup.exe using Steam's Proton or umu-run in a disposable prefix."""
    check_disk_space(target_dir, max(installer_exe.stat().st_size * 2, 512 * 1024 * 1024))
    steam_root = find_steam_root()
    proton = find_proton_binary(steam_root) if steam_root is not None else None
    use_steam_proton = steam_root is not None and proton is not None

    target_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = _collect_regular_files(target_dir)
    with tempfile.TemporaryDirectory(prefix="deckinstall_pfx_", dir=str(target_dir.parent)) as tmp_pfx:
        def _handle_inst_line(line: str) -> None:
            if on_log and line.strip():
                on_log(line.strip()[:140])

        if use_steam_proton:
            env = os.environ.copy()
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
            env["STEAM_COMPAT_DATA_PATH"] = tmp_pfx
            if on_log:
                on_log(
                    f"Starting {installer_exe.name} via Proton.\n"
                    f"Tip: in the installer, set destination to Z:{target_dir}"
                )
            cmd = [str(proton), "run", str(installer_exe)]
            inst_pfx = Path(tmp_pfx) / "pfx"
        else:
            if cancel_flag and cancel_flag():
                raise RuntimeError("Installer cancelled by user.")
            umu_run = runtime_manager.ensure_umu_run()
            resolved_proton = runtime_manager.ensure_ge_proton11()
            runtime_manager.validate_steamrt4_integrity()
            env = runtime_manager.build_umu_env(Path(tmp_pfx), resolved_proton)
            if on_log:
                on_log(
                    f"Starting {installer_exe.name} via umu-run.\n"
                    f"Tip: in the installer, set destination to Z:{target_dir}"
                )
            cmd = [str(umu_run), str(installer_exe)]
            inst_pfx = Path(tmp_pfx)

        proc = subprocess.Popen(
            cmd,
            cwd=str(tmp_pfx),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        rc = _drain_stream_lines(proc, cancel_flag, _handle_inst_line, "Installer cancelled by user.")
        if rc != 0:
            raise RuntimeError(f"Installer exited with code {rc}.")

        has_installed = bool(_collect_regular_files(target_dir) - pre_existing)
        if not has_installed:
            drive_c = inst_pfx / "drive_c"
            copied_any = False
            fallback_candidates: list[Path] = []
            for sub in ("Games", "GOG Games", "Program Files", "Program Files (x86)"):
                base = drive_c / sub
                if not base.is_dir() or base.is_symlink():
                    continue
                for cand in base.iterdir():
                    if cand.is_symlink() or not cand.is_dir() or cand.name.lower() in {
                        "common files",
                        "internet explorer",
                        "windows media player",
                        "windows nt",
                        "uninstall information",
                    }:
                        continue
                    new_exes = [
                        p for p in _collect_regular_files(cand) if p.suffix.lower() == ".exe"
                    ]
                    if not new_exes:
                        continue
                    game_exes = [p for p in new_exes if not _is_non_game_helper_exe(p)]
                    if game_exes:
                        _copy_dir_no_symlinks(cand, target_dir / cand.name)
                        copied_any = True
                    else:
                        fallback_candidates.append(cand)
            if not copied_any and fallback_candidates:
                for cand in fallback_candidates:
                    _copy_dir_no_symlinks(cand, target_dir / cand.name)
                copied_any = True
            if not copied_any:
                raise RuntimeError(
                    f"No files were installed in {target_dir}. "
                    "Point the installer to Z:" + str(target_dir)
                )
        if (inst_pfx / "drive_c").is_dir() and not inst_pfx.is_symlink():
            saved_pfx = target_dir / ".deckinstall_installer_pfx"
            if saved_pfx.is_symlink():
                saved_pfx.unlink()
            elif saved_pfx.exists():
                shutil.rmtree(saved_pfx, ignore_errors=True)
            if not saved_pfx.exists() and not saved_pfx.is_symlink():
                _copy_dir_no_symlinks(inst_pfx, saved_pfx)
    return target_dir
