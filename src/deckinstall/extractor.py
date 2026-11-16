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
from pathlib import Path

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
                total = sum(info.file_size for info in zf.infolist())
                if total > 0:
                    return total
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive, "r:*") as tf:
                total = sum(member.size for member in tf.getmembers() if member.isfile())
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
    if any(low.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")) and shutil.which("tar"):
        return ["tar", "--no-same-owner", "--no-same-permissions", "-xf", str(arc), "-C", str(out_dir)]
    if low.endswith((".rar", ".part1.rar", ".rar.001")) and shutil.which("unrar"):
        return ["unrar", "x", "-o+", "-y", "-p-", "--", str(arc), str(out_dir) + "/"]
    if low.endswith(".zip") and shutil.which("unzip"):
        return ["unzip", "-o", "--", str(arc), "-d", str(out_dir)]
    seven_z = _find_7z_bin()
    if seven_z:
        return [seven_z, "x", "-y", "-p-", f"-o{out_dir}", "--", str(arc)]
    raise RuntimeError(f"No extraction tool found for: {archive.name}")


def _extract_zip_safe(archive: Path, dest_dir: Path) -> None:
    """Extract a .zip archive using Python zipfile with strict path-traversal checks."""
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            member_name = info.filename.replace("\\", "/")
            if not member_name:
                continue
            member_path = Path(member_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe archive member path: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            target_path = (dest_resolved / member_path).resolve()
            if target_path != dest_resolved and dest_resolved not in target_path.parents:
                raise RuntimeError(f"Archive member escapes destination: {info.filename}")
            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def extract_archive_to_dir(archive: Path, dest_dir: Path) -> None:
    """Extract a .zip or .7z (or other supported) archive directly into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    low = archive.name.lower()
    if low.endswith(".zip"):
        _extract_zip_safe(archive, dest_dir)
        return
    if low.endswith(".7z"):
        seven_z = _find_7z_bin()
        if not seven_z:
            raise RuntimeError(
                "No 7-Zip extraction tool found (tried 7z, 7zz, 7za). "
                "Install 7zip/p7zip to extract .7z archives."
            )
        cmd = [seven_z, "x", "-y", "-p-", f"-o{dest_dir}", "--", str(archive)]
    else:
        cmd = _select_extract_cmd(archive, dest_dir)

    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(
            f"Failed to extract {archive.name} (code {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    _strip_symlinks_and_specials(dest_dir)


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
        cmd = _select_extract_cmd(archive, tmp_path)
        if on_progress:
            on_progress(5, f"Extracting {archive.name}...")

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
    pre_existing = {p for p in target_dir.rglob("*") if p.is_file()}
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

        has_installed = bool({p for p in target_dir.rglob("*") if p.is_file()} - pre_existing)
        if not has_installed:
            drive_c = inst_pfx / "drive_c"
            copied_any = False
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
                    if any(p.is_file() and not p.is_symlink() and p.suffix.lower() == ".exe" for p in cand.rglob("*")):
                        _copy_dir_no_symlinks(cand, target_dir / cand.name)
                        copied_any = True
            if not copied_any:
                raise RuntimeError(
                    f"No files were installed in {target_dir}. "
                    "Point the installer to Z:" + str(target_dir)
                )
        if (inst_pfx / "drive_c").is_dir() and not inst_pfx.is_symlink():
            saved_pfx = target_dir / ".deckinstall_installer_pfx"
            if saved_pfx.exists() and not saved_pfx.is_symlink():
                shutil.rmtree(saved_pfx, ignore_errors=True)
            if not saved_pfx.exists():
                _copy_dir_no_symlinks(inst_pfx, saved_pfx)
    return target_dir
