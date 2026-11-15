"""Archive extraction and Windows setup.exe runner via Proton."""

from __future__ import annotations

import codecs
import errno
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
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


def _select_extract_cmd(archive: Path, out_dir: Path) -> list[str]:
    arc = archive.resolve()
    low = arc.name.lower()
    if any(low.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")) and shutil.which("tar"):
        return ["tar", "--no-same-owner", "--no-same-permissions", "-xf", str(arc), "-C", str(out_dir)]
    if low.endswith((".rar", ".part1.rar", ".rar.001")) and shutil.which("unrar"):
        return ["unrar", "x", "-o+", "-y", "-p-", "--", str(arc), str(out_dir) + "/"]
    if low.endswith(".zip") and shutil.which("unzip"):
        return ["unzip", "-o", "--", str(arc), "-d", str(out_dir)]
    if shutil.which("7z"):
        return ["7z", "x", "-y", "-p-", f"-o{out_dir}", "--", str(arc)]
    raise RuntimeError(f"No extraction tool found for: {archive.name}")


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
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def _drain_stream_lines(
    proc: subprocess.Popen[bytes],
    cancel_flag: Callable[[], bool] | None,
    on_line: Callable[[str], None],
    cancel_msg: str,
) -> int:
    """Read subprocess stdout non-blockingly so cancel_flag is checked every <=0.2s."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    try:
        while True:
            if cancel_flag and cancel_flag():
                _kill_proc_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_proc_group(proc, signal.SIGKILL)
                    proc.wait(timeout=2)
                raise RuntimeError(cancel_msg)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 4096)
                if not chunk:
                    buf += decoder.decode(b"", final=True)
                    if buf.strip():
                        on_line(buf)
                    break
                buf += decoder.decode(chunk, final=False)
                while True:
                    idx_n = buf.find("\n")
                    idx_r = buf.find("\r")
                    if idx_n == -1 and idx_r == -1:
                        break
                    if idx_n == -1:
                        sep = idx_r
                    elif idx_r == -1:
                        sep = idx_n
                    else:
                        sep = min(idx_n, idx_r)
                    line = buf[:sep]
                    buf = buf[sep + 1 :]
                    if line.strip():
                        on_line(line)
            elif proc.poll() is not None:
                buf += decoder.decode(b"", final=True)
                if buf.strip():
                    on_line(buf)
                break
        rc = proc.wait()
        if cancel_flag and cancel_flag():
            raise RuntimeError(cancel_msg)
        return rc
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass


def extract_archive(
    archive: Path,
    dest_root: Path,
    game_folder_name: str | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Extract an archive into dest_root/<game_folder_name> and return the target path."""
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
    steam_root = find_steam_root()
    proton = find_proton_binary(steam_root) if steam_root is not None else None
    use_steam_proton = steam_root is not None and proton is not None

    target_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = {p for p in target_dir.rglob("*") if p.is_file()}
    with tempfile.TemporaryDirectory(prefix="deckinstall_pfx_", dir=str(target_dir.parent)) as tmp_pfx:
        if use_steam_proton:
            env = os.environ.copy()
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
            env["STEAM_COMPAT_DATA_PATH"] = tmp_pfx
            if on_log:
                on_log(
                    f"Starting {installer_exe.name} via Proton.\n"
                    f"Tip: in the installer, set destination to Z:{target_dir}"
                )
            proc = subprocess.Popen(
                [str(proton), "run", str(installer_exe)],
                cwd=str(tmp_pfx),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def _handle_inst_line(line: str) -> None:
                if on_log and line.strip():
                    on_log(line.strip()[:140])

            rc = _drain_stream_lines(proc, cancel_flag, _handle_inst_line, "Installer cancelled by user.")
            if rc != 0:
                raise RuntimeError(f"Installer exited with code {rc}.")
            inst_pfx = Path(tmp_pfx) / "pfx"
        else:
            if cancel_flag and cancel_flag():
                raise RuntimeError("Installer cancelled by user.")
            if on_log:
                on_log(
                    f"Starting {installer_exe.name} via umu-run.\n"
                    f"Tip: in the installer, set destination to Z:{target_dir}"
                )
            res = runtime_manager.run_with_umu(
                installer_exe,
                Path(tmp_pfx),
                cwd=Path(tmp_pfx),
            )
            if res.returncode != 0:
                raise RuntimeError(f"Installer exited with code {res.returncode}.")
            inst_pfx = Path(tmp_pfx)

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
