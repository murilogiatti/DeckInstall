"""Portable umu-launcher and GE-Proton11+ runtime manager."""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from . import __version__

if TYPE_CHECKING:
    from .steam_paths import SteamPaths

UMU_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/Open-Wine-Components/umu-launcher/releases/latest"
)
GE_PROTON_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
)
GE_PROTON_RELEASES_URL = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
)
OPTISCALER_RELEASES_API = (
    "https://api.github.com/repos/optiscaler/OptiScaler/releases/latest"
)

UPDATE_CACHE_TTL_SECONDS = 24 * 60 * 60
USER_AGENT = f"DeckInstall/{__version__}"
_GE_PROTON_TAG_RE = re.compile(r"^GE-Proton(\d+)([A-Za-z0-9._-]*)$")
_SHA512_RE = re.compile(r"\b([0-9a-fA-F]{128})\b")
_WINETRICKS_VERB_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ALLOWED_URL_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_VERSIONS_LOCK = threading.Lock()

WINETRICKS_PRESETS: dict[str, str] = {
    "vcrun2022": "Visual C++ 2015-2022 Redistributable",
    "d3dcompiler_47": "Direct3D Shader Compiler (d3dcompiler_47)",
    "corefonts": "Microsoft Core Fonts (Arial, Times, etc.)",
    "d3dx9": "DirectX 9 Runtime (d3dx9)",
}

ProgressCallback = Callable[[str, int], None]
ProgressCb = ProgressCallback


@dataclass(frozen=True)
class ProtonFamily:
    """Supported custom Proton release family hosted on GitHub."""

    id: str
    label: str
    repo: str


@dataclass(frozen=True)
class ProtonRelease:
    """Metadata for an available custom Proton release asset on GitHub."""

    family_id: str
    tag_name: str
    name: str
    asset_name: str
    download_url: str
    checksum_url: str | None = None
    size_bytes: int = 0
    published_at: str = ""
    prerelease: bool = False


@dataclass(frozen=True)
class InstalledCompatTool:
    """An installed custom compatibility tool in compatibilitytools.d."""

    folder_name: str
    tool_id: str
    path: Path
    size_bytes: int


PROTON_FAMILIES: dict[str, ProtonFamily] = {
    "ge-proton": ProtonFamily(
        id="ge-proton",
        label="GE-Proton (GloriousEggroll)",
        repo="GloriousEggroll/proton-ge-custom",
    ),
    "cachyos-proton": ProtonFamily(
        id="cachyos-proton",
        label="CachyOS-Proton",
        repo="CachyOS/proton-cachyos",
    ),
}


@dataclass(frozen=True)
class GpuProfile:
    """Detected GPU hardware, driver, and display-session capabilities."""

    has_nvidia: bool
    has_amd: bool
    has_intel: bool
    is_hybrid: bool
    is_wayland: bool
    nvidia_driver_present: bool


def detect_gpu_profile() -> GpuProfile:
    """Inspect DRM sysfs vendor IDs (with lspci fallback) and session environment."""
    has_nvidia = False
    has_amd = False
    has_intel = False
    found_vendor_file = False

    drm_root = Path("/sys/class/drm")
    if drm_root.is_dir():
        for vendor_file in drm_root.glob("card*/device/vendor"):
            try:
                vendor_id = vendor_file.read_text(encoding="utf-8").strip().lower()
            except OSError:
                continue
            if not vendor_id:
                continue
            found_vendor_file = True
            if vendor_id == "0x10de":
                has_nvidia = True
            elif vendor_id == "0x1002":
                has_amd = True
            elif vendor_id == "0x8086":
                has_intel = True

    if not found_vendor_file:
        try:
            proc = subprocess.run(
                ["lspci", "-nn"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    low = line.lower()
                    if not any(tok in low for tok in ("vga", "3d", "display")):
                        continue
                    if "[10de:" in low or "nvidia" in low:
                        has_nvidia = True
                    if "[1002:" in low or "amd" in low or "ati" in low or "radeon" in low:
                        has_amd = True
                    if "[8086:" in low or "intel" in low:
                        has_intel = True
        except (OSError, subprocess.TimeoutExpired):
            pass

    is_hybrid = sum((has_nvidia, has_amd, has_intel)) >= 2
    nvidia_driver_present = (
        Path("/proc/driver/nvidia/version").exists()
        or shutil.which("nvidia-smi") is not None
    )
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
    )
    return GpuProfile(
        has_nvidia=has_nvidia,
        has_amd=has_amd,
        has_intel=has_intel,
        is_hybrid=is_hybrid,
        is_wayland=is_wayland,
        nvidia_driver_present=nvidia_driver_present,
    )


def get_runtime_dir() -> Path:
    """Return the portable DeckInstall runtime root directory."""
    return Path.home() / ".local" / "share" / "DeckInstall" / "runtime"


def get_umu_dir() -> Path:
    """Return the directory containing the portable umu-run binary."""
    return get_runtime_dir() / "umu"


def get_umu_run_path() -> Path:
    """Return the expected path to the portable umu-run executable."""
    return get_umu_dir() / "umu-run"


def get_versions_file() -> Path:
    """Return the path to the runtime version/cache metadata JSON file."""
    return get_runtime_dir() / "versions.json"


def get_optiscaler_dir() -> Path:
    """Return the directory containing the cached OptiScaler runtime payload."""
    return get_runtime_dir() / "optiscaler" / "current"


def _iter_extra_compat_tool_dirs() -> list[Path]:
    """Return expanded paths from STEAM_EXTRA_COMPAT_TOOLS_PATHS."""
    raw = os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").strip()
    if not raw:
        return []
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        cleaned = part.strip()
        if cleaned:
            out.append(Path(cleaned).expanduser())
    return out


def _is_writable_dir_candidate(path: Path) -> bool:
    """Return True if path is a writable directory or can be created under a writable parent."""
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    parent = path.parent
    while parent != parent.parent and not parent.exists():
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def get_compat_tools_dir() -> Path:
    """Return the Steam compatibilitytools.d directory for GE-Proton."""
    for extra_dir in _iter_extra_compat_tool_dirs():
        if _is_writable_dir_candidate(extra_dir):
            return extra_dir

    from .steam_paths import find_steam_root

    steam_root = find_steam_root()
    if steam_root is not None:
        return steam_root / "compatibilitytools.d"

    xdg_data_home = Path(
        os.environ.get("XDG_DATA_HOME", "").strip() or (Path.home() / ".local" / "share")
    ).expanduser()
    return xdg_data_home / "Steam" / "compatibilitytools.d"


def get_steamrt4_dir() -> Path:
    """Return the umu steamrt4 runtime directory."""
    return Path.home() / ".local" / "share" / "umu" / "steamrt4"


def _emit_progress(
    progress_cb: ProgressCallback | None, message: str, percent: int
) -> None:
    if progress_cb is not None:
        progress_cb(message, max(0, min(100, percent)))


def _get_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return machine


def _parse_ge_proton_version(
    name: str, min_major: int = 11, max_major: int | None = None
) -> tuple[int, ...] | None:
    """Parse GE-Proton folder/tag names within [min_major, max_major]."""
    match = _GE_PROTON_TAG_RE.match(name)
    if not match:
        return None
    major = int(match.group(1))
    if major < min_major:
        return None
    if max_major is not None and major > max_major:
        return None
    rest = [int(part) for part in re.findall(r"\d+", match.group(2))]
    return (major, *rest)


def list_ge_proton_dirs(
    min_major: int = 11, max_major: int | None = None
) -> list[Path]:
    """List installed GE-Proton directories within [min_major, max_major] sorted newest first."""
    from .steam_paths import _iter_compat_tool_dirs, find_steam_root

    steam_root = find_steam_root()
    candidate_dirs: list[Path] = [get_compat_tools_dir()]
    if steam_root is not None:
        candidate_dirs.extend(_iter_compat_tool_dirs(steam_root))
    candidate_dirs.extend(_iter_extra_compat_tool_dirs())

    seen_dirs: set[Path] = set()
    seen_entries: set[Path] = set()
    found: list[tuple[tuple[int, ...], Path]] = []

    for compat_dir in candidate_dirs:
        if not compat_dir.is_dir():
            continue
        try:
            resolved_dir = compat_dir.resolve()
        except OSError:
            resolved_dir = compat_dir
        if resolved_dir in seen_dirs:
            continue
        seen_dirs.add(resolved_dir)

        try:
            entries = list(compat_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if resolved_entry in seen_entries:
                continue
            version = _parse_ge_proton_version(
                entry.name, min_major=min_major, max_major=max_major
            )
            if version is not None:
                seen_entries.add(resolved_entry)
                found.append((version, entry))

    found.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in found]


def list_ge_proton11_dirs() -> list[Path]:
    """List installed GE-Proton11+ directories sorted newest first."""
    return list_ge_proton_dirs(min_major=11)


def list_ge_proton10_dirs() -> list[Path]:
    """List installed GE-Proton10 directories sorted newest first."""
    return list_ge_proton_dirs(min_major=10, max_major=10)


@contextlib.contextmanager
def _versions_lock():
    """Serialize read/write access to versions.json across threads and processes."""
    lock_path = get_versions_file().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _VERSIONS_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _load_versions() -> dict[str, Any]:
    with _versions_lock():
        path = get_versions_file()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _save_versions(data: dict[str, Any]) -> None:
    with _versions_lock():
        path = get_versions_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".versions_", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _validate_github_url(url: str) -> str:
    """Validate that url uses HTTPS and points to an allowlisted GitHub/CDN host."""
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in _ALLOWED_URL_HOSTS:
        raise ValueError(f"Disallowed URL (must be HTTPS on GitHub): {url!r}")
    return cleaned


def _sanitize_asset_name(name: str) -> str:
    """Ensure a release asset filename is a single safe basename."""
    raw = name.strip()
    if not raw or "/" in raw or "\\" in raw:
        raise ValueError(f"Unsafe asset filename: {name!r}")
    cleaned = Path(raw).name
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Unsafe asset filename: {name!r}")
    return cleaned


def _fetch_json(url: str, timeout: int = 20) -> Any:
    validated_url = _validate_github_url(url)
    resp = requests.get(
        validated_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _download_file(
    url: str,
    dest: Path,
    label: str,
    progress_cb: ProgressCallback | None = None,
    timeout: int = 60,
) -> None:
    validated_url = _validate_github_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        validated_url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        total_raw = resp.headers.get("Content-Length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else 0
        downloaded = 0
        last_pct = -1
        _emit_progress(progress_cb, f"Downloading {label}...", 0)
        with open(dest, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = min(99, int((downloaded * 100) / total))
                    if pct != last_pct:
                        last_pct = pct
                        _emit_progress(progress_cb, f"Downloading {label}...", pct)
    _emit_progress(progress_cb, f"Downloaded {label}.", 100)


def _safe_extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """Safely extract a tar/tar.gz archive preventing path traversal and special files."""
    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe archive member path: {member.name}")
            if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
                raise RuntimeError(f"Unsafe archive special file: {member.name}")
            target_path = (dest_resolved / member_path).resolve()
            if target_path != dest_resolved and dest_resolved not in target_path.parents:
                raise RuntimeError(f"Archive member escapes destination: {member.name}")
            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                if link_target.is_absolute():
                    raise RuntimeError(f"Unsafe archive link target: {member.linkname}")
                base = target_path.parent if member.issym() else dest_resolved
                resolved_link = (base / link_target).resolve()
                if resolved_link != dest_resolved and dest_resolved not in resolved_link.parents:
                    raise RuntimeError(f"Unsafe archive link target: {member.linkname}")
        if sys.version_info >= (3, 12):
            tf.extractall(dest_dir, members=members, filter="data")
        else:
            tf.extractall(dest_dir, members=members)


def _fetch_latest_ge_proton11_release() -> dict[str, Any]:
    latest = _fetch_json(GE_PROTON_RELEASES_LATEST_URL)
    if isinstance(latest, dict):
        tag = str(latest.get("tag_name", ""))
        if _parse_ge_proton_version(tag) is not None:
            return latest

    releases = _fetch_json(GE_PROTON_RELEASES_URL)
    if not isinstance(releases, list):
        raise RuntimeError("Unexpected GitHub releases response for GE-Proton.")
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name", ""))
        if _parse_ge_proton_version(tag) is not None:
            return rel
    raise RuntimeError("No GE-Proton11+ release found on GitHub.")


def _find_release_asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == name:
            return asset
    return None


def _verify_sha512(file_path: Path, checksum_text: str) -> None:
    entries: list[tuple[str, str]] = []
    for raw_line in checksum_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([0-9a-fA-F]{128})(?:\s+[* ]?(.+))?$", line)
        if match:
            digest_hex = match.group(1).lower()
            entry_name = Path((match.group(2) or "").strip()).name
            entries.append((digest_hex, entry_name))

    if not entries:
        raise RuntimeError("Invalid .sha512sum file format.")

    expected: str | None = None
    for digest_hex, entry_name in entries:
        if entry_name == file_path.name:
            expected = digest_hex
            break

    if expected is None:
        if len(entries) == 1:
            expected = entries[0][0]
        else:
            raise RuntimeError(
                f"No SHA-512 entry matching {file_path.name!r} in .sha512sum."
            )

    digest = hashlib.sha512()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise RuntimeError(
            f"SHA-512 checksum mismatch for {file_path.name}: expected {expected}, got {actual}"
        )


def ensure_umu_run(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Ensure portable umu-run is installed and executable, downloading if needed."""
    umu_run = get_umu_run_path()
    if umu_run.is_file() and not force_update:
        if not os.access(umu_run, os.X_OK):
            umu_run.chmod(umu_run.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return umu_run

    _emit_progress(progress_cb, "Checking latest umu-launcher release...", 0)
    release = _fetch_json(UMU_RELEASES_LATEST_URL)
    if not isinstance(release, dict):
        raise RuntimeError("Unexpected GitHub release response for umu-launcher.")
    tag = str(release.get("tag_name", "")).strip()
    assets = release.get("assets", [])
    zipapp_asset: dict[str, Any] | None = None
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name", ""))
            if name.startswith("umu-launcher-") and name.endswith("-zipapp.tar"):
                zipapp_asset = asset
                break
    if zipapp_asset is None:
        raise RuntimeError("Could not find umu-launcher-*-zipapp.tar in latest release.")

    download_url = str(zipapp_asset.get("browser_download_url", ""))
    asset_name = _sanitize_asset_name(str(zipapp_asset.get("name", "umu-launcher-zipapp.tar")))
    if not download_url:
        raise RuntimeError("umu-launcher zipapp asset is missing download URL.")

    umu_dir = get_umu_dir()
    umu_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deckinstall_umu_", dir=str(get_runtime_dir())) as tmp_dir:
        tmp_path = Path(tmp_dir)
        tar_path = tmp_path / asset_name
        _download_file(download_url, tar_path, asset_name, progress_cb=progress_cb)

        _emit_progress(progress_cb, "Extracting umu-run...", 90)
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_tar(tar_path, extract_dir)

        extracted_umu = next(
            (p for p in extract_dir.rglob("umu-run") if p.is_file() and not p.is_symlink()),
            None,
        )
        if extracted_umu is None:
            raise RuntimeError("umu-run binary not found inside downloaded zipapp archive.")

        fd, staged_name = tempfile.mkstemp(prefix=".umu-run_", dir=str(umu_dir))
        os.close(fd)
        staged_path = Path(staged_name)
        try:
            shutil.copy2(extracted_umu, staged_path)
            staged_path.chmod(0o755)
            os.replace(staged_path, umu_run)
        except Exception:
            staged_path.unlink(missing_ok=True)
            raise

    versions = _load_versions()
    if tag:
        versions["umu_installed_version"] = tag
        versions["umu_latest_version"] = tag
    _save_versions(versions)
    _emit_progress(progress_cb, "umu-run ready.", 100)
    return umu_run


def _has_optiscaler_dll(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(
        p.is_file() and not p.is_symlink() and p.name.lower() == "optiscaler.dll"
        for p in directory.iterdir()
    )


def _pick_optiscaler_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise RuntimeError("OptiScaler release response is missing assets list.")
    zip_asset: dict[str, Any] | None = None
    seven_z_asset: dict[str, Any] | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).lower()
        url = str(asset.get("browser_download_url", "")).strip()
        if not url:
            continue
        if name.endswith(".zip") and zip_asset is None:
            zip_asset = asset
        elif name.endswith(".7z") and seven_z_asset is None:
            seven_z_asset = asset
    chosen = zip_asset or seven_z_asset
    if chosen is None:
        raise RuntimeError("Could not find a .zip or .7z asset in latest OptiScaler release.")
    return chosen


def _swap_directory_atomic(src_dir: Path, target_dir: Path) -> None:
    """Copy src_dir into a staging sibling and atomically swap it into target_dir."""
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staged_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}_stage_", dir=str(parent)))
    backup_dir: Path | None = None
    try:
        shutil.copytree(src_dir, staged_dir, dirs_exist_ok=True, symlinks=False)
        if target_dir.exists() or target_dir.is_symlink():
            backup_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}_old_", dir=str(parent)))
            backup_dir.rmdir()
            os.replace(target_dir, backup_dir)
        os.replace(staged_dir, target_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            try:
                os.replace(backup_dir, target_dir)
            except OSError:
                pass
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise
    if backup_dir is not None:
        shutil.rmtree(backup_dir, ignore_errors=True)


def ensure_optiscaler(
    force_update: bool = False,
    progress_cb: ProgressCb | None = None,
) -> Path:
    """Ensure OptiScaler payload is cached under runtime/optiscaler/current."""
    opti_dir = get_optiscaler_dir()
    if not force_update and _has_optiscaler_dll(opti_dir):
        return opti_dir

    from .extractor import extract_archive_to_dir

    _emit_progress(progress_cb, "Checking latest OptiScaler release...", 0)
    release = _fetch_json(OPTISCALER_RELEASES_API)
    if not isinstance(release, dict):
        raise RuntimeError("Unexpected GitHub release response for OptiScaler.")

    tag = str(release.get("tag_name", "")).strip()
    asset = _pick_optiscaler_asset(release)
    download_url = str(asset.get("browser_download_url", "")).strip()
    asset_name = _sanitize_asset_name(str(asset.get("name", "OptiScaler.zip")))

    opti_parent = opti_dir.parent
    opti_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deckinstall_optiscaler_", dir=str(opti_parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / asset_name
        _download_file(download_url, archive_path, asset_name, progress_cb=progress_cb)

        _emit_progress(progress_cb, "Extracting OptiScaler...", 90)
        extract_dir = tmp_path / "extract"
        extract_archive_to_dir(archive_path, extract_dir)

        dll_candidates = [
            p
            for p in extract_dir.rglob("*")
            if p.is_file() and not p.is_symlink() and p.name.lower() == "optiscaler.dll"
        ]
        if not dll_candidates:
            raise RuntimeError("OptiScaler.dll not found inside downloaded OptiScaler archive.")
        payload_src = min(dll_candidates, key=lambda p: len(p.parts)).parent

        _emit_progress(progress_cb, "Installing OptiScaler runtime...", 96)
        _swap_directory_atomic(payload_src, opti_dir)

    versions = _load_versions()
    if tag:
        versions["optiscaler_installed_version"] = tag
        versions["optiscaler_latest_version"] = tag
    versions["optiscaler_last_check"] = int(time.time())
    _save_versions(versions)
    _emit_progress(progress_cb, "OptiScaler ready.", 100)
    return opti_dir


def ensure_ge_proton11(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Ensure a GE-Proton11+ directory is installed in Steam compatibilitytools.d."""
    existing = list_ge_proton11_dirs()
    if existing and not force_update:
        return existing[0]

    _emit_progress(progress_cb, "Checking latest GE-Proton11+ release...", 0)
    release = _fetch_latest_ge_proton11_release()
    tag = str(release.get("tag_name", "")).strip()
    if not tag:
        raise RuntimeError("GE-Proton release is missing tag_name.")
    if _parse_ge_proton_version(tag) is None or ".." in tag:
        raise RuntimeError(f"Unsafe GE-Proton tag name: {tag!r}")

    arch = _get_arch()
    tar_name = _sanitize_asset_name(f"{tag}-{arch}.tar.gz")
    tar_asset = _find_release_asset(release, tar_name)
    if tar_asset is None:
        if arch != "x86_64":
            raise RuntimeError(
                f"Upstream GE-Proton release {tag} did not publish an asset for {arch} "
                f"(expected {tar_name})."
            )
        raise RuntimeError(f"GE-Proton asset {tar_name} not found in release {tag}.")

    sha_asset = (
        _find_release_asset(release, f"{tag}-{arch}.sha512sum")
        or _find_release_asset(release, f"{tar_name}.sha512sum")
    )
    if sha_asset is None:
        raise RuntimeError(f"GE-Proton checksum asset for {tar_name} not found in release {tag}.")

    tar_url = str(tar_asset.get("browser_download_url", ""))
    sha_url = str(sha_asset.get("browser_download_url", ""))
    if not tar_url or not sha_url:
        raise RuntimeError("GE-Proton release asset is missing download URL.")

    compat_dir = get_compat_tools_dir()
    compat_dir.mkdir(parents=True, exist_ok=True)
    target_dir = compat_dir / tag
    if target_dir.resolve().parent != compat_dir.resolve():
        raise RuntimeError(f"Unsafe GE-Proton target directory: {target_dir}")

    with tempfile.TemporaryDirectory(prefix="deckinstall_ge_", dir=str(compat_dir)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        tar_path = tmp_path / tar_name
        sha_name = _sanitize_asset_name(str(sha_asset.get("name", f"{tag}-{arch}.sha512sum")))
        sha_path = tmp_path / sha_name

        _download_file(sha_url, sha_path, sha_path.name)
        _download_file(tar_url, tar_path, tar_name, progress_cb=progress_cb, timeout=120)

        _emit_progress(progress_cb, f"Verifying SHA-512 for {tar_name}...", 92)
        _verify_sha512(tar_path, sha_path.read_text(encoding="utf-8", errors="ignore"))

        _emit_progress(progress_cb, f"Extracting {tag}...", 95)
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_tar(tar_path, extract_dir)

        extracted_root = extract_dir / tag
        if not extracted_root.is_dir():
            subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
            extracted_root = subdirs[0] if len(subdirs) == 1 else extract_dir

        backup_dir: Path | None = None
        try:
            if target_dir.exists() or target_dir.is_symlink():
                backup_dir = Path(
                    tempfile.mkdtemp(prefix=f".{tag}_old_", dir=str(compat_dir))
                )
                backup_dir.rmdir()
                os.replace(target_dir, backup_dir)
            os.replace(extracted_root, target_dir)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
                try:
                    os.replace(backup_dir, target_dir)
                except OSError:
                    pass
            raise
        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)

    versions = _load_versions()
    versions["ge_proton_latest_version"] = tag
    _save_versions(versions)
    _emit_progress(progress_cb, f"{tag} ready.", 100)
    return target_dir


def ensure_ge_proton10(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Ensure a GE-Proton10 directory is installed in Steam compatibilitytools.d."""
    existing = list_ge_proton10_dirs()
    if existing and not force_update:
        return existing[0]

    _emit_progress(progress_cb, "Checking latest GE-Proton10 release...", 0)
    releases = fetch_available_proton_releases("ge-proton", limit=50)
    release: ProtonRelease | None = None
    for rel in releases:
        if rel.prerelease:
            continue
        if _parse_ge_proton_version(rel.tag_name, min_major=10, max_major=10) is not None:
            release = rel
            break
    if release is None:
        raise RuntimeError("No GE-Proton10 release found.")

    return install_compat_tool(release, progress_cb=progress_cb)


def _dir_size_bytes(path: Path) -> int:
    """Return total byte size of regular files under path without following symlinks."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            try:
                if not fpath.is_symlink():
                    total += fpath.stat().st_size
            except OSError:
                continue
    return total


def _strip_tar_suffix(name: str) -> str:
    for suffix in (".tar.gz", ".tar.xz"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _pick_proton_tar_asset(assets: list[Any], arch: str) -> dict[str, Any] | None:
    """Select the best .tar.gz or .tar.xz Proton release asset for the host arch."""
    tar_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name") or "").endswith((".tar.gz", ".tar.xz"))
    ]
    if not tar_assets:
        return None

    def _score(asset: dict[str, Any]) -> tuple[int, int, str]:
        name = str(asset.get("name") or "")
        low = name.lower()
        if arch == "x86_64" and ("aarch64" in low or "arm64" in low):
            return (-10, 0, name)
        if arch == "aarch64" and ("x86_64" in low or "amd64" in low):
            return (-10, 0, name)
        arch_score = 0
        if f"-{arch}." in low or f"_{arch}." in low:
            arch_score = 4
        elif arch in low:
            arch_score = 2 if ("_v3" in low or "_v4" in low) else 3
        else:
            arch_score = 1
        ext_score = 1 if low.endswith(".tar.xz") else 0
        return (arch_score, ext_score, name)

    best = max(tar_assets, key=_score)
    if _score(best)[0] < 0:
        return None
    return best


def _find_proton_checksum_asset(
    assets: list[Any], asset_name: str
) -> dict[str, Any] | None:
    """Find an optional .sha512sum asset corresponding to asset_name."""
    stem = _strip_tar_suffix(asset_name)
    for target_name in (f"{asset_name}.sha512sum", f"{stem}.sha512sum"):
        for asset in assets:
            if isinstance(asset, dict) and asset.get("name") == target_name:
                return asset
    sha_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict) and str(asset.get("name") or "").endswith(".sha512sum")
    ]
    if len(sha_assets) == 1:
        return sha_assets[0]
    return None


def fetch_available_proton_releases(
    family_id: str, limit: int = 15
) -> list[ProtonRelease]:
    """Fetch available custom Proton releases from GitHub for the given family."""
    family = PROTON_FAMILIES.get(family_id)
    if family is None:
        raise ValueError(f"Unknown Proton family: {family_id!r}")

    per_page = max(1, min(100, int(limit)))
    url = f"https://api.github.com/repos/{family.repo}/releases?per_page={per_page}"
    releases_raw = _fetch_json(url)
    if not isinstance(releases_raw, list):
        raise RuntimeError(f"Unexpected GitHub releases response for {family.label}.")

    arch = _get_arch()
    results: list[ProtonRelease] = []
    for rel in releases_raw:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag_name = str(rel.get("tag_name") or "").strip()
        if not tag_name:
            continue
        assets = rel.get("assets")
        if not isinstance(assets, list):
            continue
        tar_asset = _pick_proton_tar_asset(assets, arch)
        if tar_asset is None:
            continue
        download_url = str(tar_asset.get("browser_download_url") or "").strip()
        if not download_url:
            continue
        asset_name = str(tar_asset.get("name") or "").strip()
        sha_asset = _find_proton_checksum_asset(assets, asset_name)
        checksum_url = (
            str(sha_asset.get("browser_download_url") or "").strip() or None
            if sha_asset is not None
            else None
        )
        raw_size = tar_asset.get("size")
        size_bytes = int(raw_size) if isinstance(raw_size, int) and raw_size >= 0 else 0
        results.append(
            ProtonRelease(
                family_id=family.id,
                tag_name=tag_name,
                name=str(rel.get("name") or tag_name).strip() or tag_name,
                asset_name=asset_name,
                download_url=download_url,
                checksum_url=checksum_url,
                size_bytes=size_bytes,
                published_at=str(rel.get("published_at") or "").strip(),
                prerelease=bool(rel.get("prerelease", False)),
            )
        )
    return results


def list_installed_compat_tools() -> list[InstalledCompatTool]:
    """List all custom compatibility tools with compatibilitytool.vdf installed."""
    from .steam_paths import _iter_compat_tool_dirs, _read_compat_tool_id, find_steam_root

    steam_root = find_steam_root()
    candidate_dirs: list[Path] = [get_compat_tools_dir()]
    if steam_root is not None:
        candidate_dirs.extend(_iter_compat_tool_dirs(steam_root))
    candidate_dirs.extend(_iter_extra_compat_tool_dirs())

    seen_dirs: set[Path] = set()
    seen_entries: set[Path] = set()
    tools: list[InstalledCompatTool] = []

    for compat_dir in candidate_dirs:
        if not compat_dir.is_dir():
            continue
        try:
            resolved_dir = compat_dir.resolve()
        except OSError:
            resolved_dir = compat_dir
        if resolved_dir in seen_dirs:
            continue
        seen_dirs.add(resolved_dir)

        try:
            entries = list(compat_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if resolved_entry in seen_entries:
                continue
            vdf_file = entry / "compatibilitytool.vdf"
            if not vdf_file.is_file():
                continue
            seen_entries.add(resolved_entry)
            tool_id = _read_compat_tool_id(vdf_file, entry.name)
            tools.append(
                InstalledCompatTool(
                    folder_name=entry.name,
                    tool_id=tool_id,
                    path=entry,
                    size_bytes=_dir_size_bytes(entry),
                )
            )

    tools.sort(
        key=lambda t: (
            tuple(int(n) for n in re.findall(r"\d+", t.folder_name)),
            t.folder_name.lower(),
        ),
        reverse=True,
    )
    return tools


def install_compat_tool(
    release: ProtonRelease,
    progress_cb: ProgressCb | None = None,
) -> Path:
    """Download, verify (when checksum_url is set), and install a Proton release."""
    if not release.download_url:
        raise ValueError("ProtonRelease is missing download_url.")

    compat_dir = get_compat_tools_dir()
    compat_dir.mkdir(parents=True, exist_ok=True)
    raw_tar_name = release.asset_name or Path(release.download_url.split("?", 1)[0]).name
    tar_name = _sanitize_asset_name(raw_tar_name)

    with tempfile.TemporaryDirectory(
        prefix="deckinstall_proton_", dir=str(compat_dir)
    ) as tmp_dir:
        tmp_path = Path(tmp_dir)
        tar_path = tmp_path / tar_name
        sha_path: Path | None = None
        if release.checksum_url:
            sha_path = tmp_path / f"{tar_name}.sha512sum"
            _download_file(release.checksum_url, sha_path, sha_path.name)

        _download_file(
            release.download_url,
            tar_path,
            tar_name,
            progress_cb=progress_cb,
            timeout=180,
        )

        if sha_path is not None:
            _emit_progress(progress_cb, f"Verifying SHA-512 for {tar_name}...", 92)
            _verify_sha512(
                tar_path, sha_path.read_text(encoding="utf-8", errors="ignore")
            )

        _emit_progress(progress_cb, f"Extracting {tar_name}...", 95)
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_tar(tar_path, extract_dir)

        subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(subdirs) == 1:
            extracted_root = subdirs[0]
            folder_name = extracted_root.name
        elif (extract_dir / release.tag_name).is_dir():
            extracted_root = extract_dir / release.tag_name
            folder_name = extracted_root.name
        else:
            extracted_root = extract_dir
            folder_name = _strip_tar_suffix(tar_name) or release.tag_name

        if not folder_name or folder_name in (".", "..") or "/" in folder_name or "\\" in folder_name:
            raise RuntimeError(f"Unsafe compatibility tool folder name: {folder_name!r}")

        target_dir = compat_dir / folder_name
        if ".." in folder_name or target_dir.resolve().parent != compat_dir.resolve():
            raise RuntimeError(f"Unsafe compatibility tool target directory: {target_dir}")
        backup_dir: Path | None = None
        try:
            if target_dir.exists() or target_dir.is_symlink():
                backup_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".{folder_name}_old_", dir=str(compat_dir)
                    )
                )
                backup_dir.rmdir()
                os.replace(target_dir, backup_dir)
            os.replace(extracted_root, target_dir)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
                try:
                    os.replace(backup_dir, target_dir)
                except OSError:
                    pass
            raise
        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)

    _emit_progress(progress_cb, f"{folder_name} installed.", 100)
    return target_dir


def remove_compat_tool(tool_path: Path) -> None:
    """Safely remove a custom compatibility tool directory from compatibilitytools.d."""
    if tool_path.is_symlink():
        raise ValueError(f"Refusing to remove symlink: {tool_path}")
    if not tool_path.exists() or not tool_path.is_dir():
        raise FileNotFoundError(f"Compatibility tool directory not found: {tool_path}")
    parent = tool_path.parent
    if parent.name != "compatibilitytools.d" or tool_path.name in ("", ".", ".."):
        raise ValueError(
            f"Refusing to remove path outside compatibilitytools.d: {tool_path}"
        )
    if tool_path.resolve().parent != parent.resolve():
        raise ValueError(
            f"Refusing to remove non-direct child of compatibilitytools.d: {tool_path}"
        )
    shutil.rmtree(tool_path)


def check_runtime_updates(force: bool = False) -> dict[str, Any]:
    """Check GitHub for newer umu-launcher and GE-Proton11+ releases (24h cache)."""
    versions = _load_versions()
    now = time.time()
    last_check = float(versions.get("last_check", 0) or 0)
    umu_path = get_umu_run_path()
    installed_umu = versions.get("umu_installed_version") if umu_path.is_file() else None
    ge_dirs = list_ge_proton11_dirs()
    installed_ge = ge_dirs[0].name if ge_dirs else None

    if not force and last_check > 0 and (now - last_check) < UPDATE_CACHE_TTL_SECONDS:
        latest_umu = versions.get("umu_latest_version")
        latest_ge = versions.get("ge_proton_latest_version")
        return {
            "checked_at": last_check,
            "from_cache": True,
            "umu": {
                "installed": installed_umu,
                "latest": latest_umu,
                "update_available": bool(latest_umu and installed_umu != latest_umu),
                "installed_path": str(umu_path) if umu_path.is_file() else None,
            },
            "ge_proton": {
                "installed": installed_ge,
                "latest": latest_ge,
                "update_available": bool(latest_ge and installed_ge != latest_ge),
                "installed_path": str(ge_dirs[0]) if ge_dirs else None,
            },
        }

    umu_release = _fetch_json(UMU_RELEASES_LATEST_URL)
    latest_umu = str(umu_release.get("tag_name", "")).strip() or None
    ge_release = _fetch_latest_ge_proton11_release()
    latest_ge = str(ge_release.get("tag_name", "")).strip() or None

    versions["last_check"] = now
    if latest_umu:
        versions["umu_latest_version"] = latest_umu
    if latest_ge:
        versions["ge_proton_latest_version"] = latest_ge
    _save_versions(versions)

    return {
        "checked_at": now,
        "from_cache": False,
        "umu": {
            "installed": installed_umu,
            "latest": latest_umu,
            "update_available": bool(latest_umu and installed_umu != latest_umu),
            "installed_path": str(umu_path) if umu_path.is_file() else None,
        },
        "ge_proton": {
            "installed": installed_ge,
            "latest": latest_ge,
            "update_available": bool(latest_ge and installed_ge != latest_ge),
            "installed_path": str(ge_dirs[0]) if ge_dirs else None,
        },
    }


def validate_steamrt4_integrity() -> bool:
    """Remove broken/partial ~/.local/share/umu/steamrt4 directory if required files are missing."""
    steamrt4_dir = get_steamrt4_dir()
    if not steamrt4_dir.is_dir():
        return True
    manifest = steamrt4_dir / "toolmanifest.vdf"
    mtree = steamrt4_dir / "mtree.txt.gz"
    if not manifest.is_file() or not mtree.is_file():
        shutil.rmtree(steamrt4_dir, ignore_errors=True)
        return False
    return True


def check_ubuntu_bwrap_userns() -> str | None:
    """Check if AppArmor unprivileged userns restriction (kernel.apparmor_restrict_unprivileged_userns=1) blocks bwrap."""
    sysctl_path = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    if not sysctl_path.is_file():
        return None
    try:
        value = sysctl_path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if value != "1":
        return None

    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None

    try:
        proc = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return (
            "AppArmor restricts unprivileged user namespaces "
            "(kernel.apparmor_restrict_unprivileged_userns=1), blocking bubblewrap (bwrap).\n"
            "Fix with: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
        )

    if proc.returncode != 0:
        return (
            "AppArmor restricts unprivileged user namespaces "
            "(kernel.apparmor_restrict_unprivileged_userns=1), blocking bubblewrap (bwrap) "
            "required by umu-run.\n"
            "Fix with: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
        )
    return None


_SYSTEM_PACKAGE_MAP: dict[str, dict[str, list[str]]] = {
    "gamemoderun": {
        "apt-get": ["gamemode"],
        "dnf": ["gamemode"],
        "pacman": ["gamemode"],
        "zypper": ["gamemode"],
    },
    "mangohud": {
        "apt-get": ["mangohud"],
        "dnf": ["mangohud"],
        "pacman": ["mangohud"],
        "zypper": ["mangohud"],
    },
    "winetricks": {
        "apt-get": ["winetricks"],
        "dnf": ["winetricks"],
        "pacman": ["winetricks"],
        "zypper": ["winetricks"],
    },
}


def get_system_package_install_cmd(bin_name: str) -> list[str] | None:
    """Return an OS package-manager install command for bin_name, or None if unsupported."""
    pkg_map = _SYSTEM_PACKAGE_MAP.get(bin_name)
    if not pkg_map:
        return None
    if shutil.which("apt-get"):
        return ["apt-get", "install", "-y", *pkg_map["apt-get"]]
    if shutil.which("dnf"):
        return ["dnf", "install", "-y", *pkg_map["dnf"]]
    if shutil.which("pacman"):
        return ["pacman", "-S", "--noconfirm", *pkg_map["pacman"]]
    if shutil.which("zypper"):
        return ["zypper", "--non-interactive", "install", *pkg_map["zypper"]]
    return None


def install_system_package(bin_name: str) -> tuple[bool, str]:
    """Attempt to install the package providing bin_name via pkexec. Returns (ok, message)."""
    cmd = get_system_package_install_cmd(bin_name)
    if not cmd:
        return (False, f"No supported package manager found for {bin_name}.")
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return (False, f"sudo {' '.join(cmd)}")
    proc = subprocess.run(
        [pkexec, *cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return (False, err)
    return (True, f"{bin_name} installed.")


def fix_bwrap_userns() -> tuple[bool, str]:
    """Install a persistent AppArmor userns profile for bwrap (or fallback sysctl) via pkexec."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return (False, "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0")
    script = (
        "set -e; "
        "if [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null 2>&1; then "
        "cat <<'EOF' >/etc/apparmor.d/bwrap\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "profile bwrap /usr/bin/bwrap flags=(unconfined) {\n"
        "  userns,\n"
        "  include if exists <local/bwrap>\n"
        "}\n"
        "EOF\n"
        "apparmor_parser -r /etc/apparmor.d/bwrap || true; "
        "fi; "
        "if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then "
        "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true; "
        "fi"
    )
    proc = subprocess.run(
        [pkexec, "sh", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return (False, err)
    return (True, "OK")


def kill_proc_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the subprocess group when start_new_session=True, falling back to proc."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def drain_stream_lines(
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
                kill_proc_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill_proc_group(proc, signal.SIGKILL)
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


def resolve_game_prefix(
    kind: str,
    obj: object,
    paths: SteamPaths | None,
) -> tuple[Path, Path | None]:
    """Resolve (prefix_dir, proton_dir) for a Steam shortcut or standalone game."""
    kind_norm = kind.strip().lower()
    if kind_norm == "steam":
        if paths is None:
            raise ValueError("SteamPaths is required to resolve a Steam game prefix.")
        appid = getattr(obj, "appid_u32", obj)
        if not isinstance(appid, int):
            raise ValueError(f"Expected integer appid or ShortcutEntry, got {type(obj).__name__}.")
        prefix_dir = paths.compatdata_dir / str(appid) / "pfx"
        from .steam_paths import find_proton_binary

        proton_bin = find_proton_binary(paths.steam_root)
        proton_dir = proton_bin.parent if proton_bin is not None else None
        return (prefix_dir, proton_dir)

    if kind_norm == "standalone":
        raw_prefix = getattr(obj, "prefix_dir", None)
        if raw_prefix is None:
            raise ValueError("Standalone game object has no prefix_dir attribute.")
        prefix_dir = Path(raw_prefix)
        if (prefix_dir / "pfx").is_dir() and not (prefix_dir / "drive_c").is_dir():
            prefix_dir = prefix_dir / "pfx"

        proton_dir: Path | None = None
        proton_name = str(getattr(obj, "proton_name", "") or "").strip()
        if proton_name:
            as_path = Path(proton_name)
            if as_path.is_dir() and (as_path / "proton").is_file():
                proton_dir = as_path
            else:
                for cand in list_ge_proton11_dirs():
                    if cand.name == proton_name:
                        proton_dir = cand
                        break
                if proton_dir is None:
                    compat_cand = get_compat_tools_dir() / proton_name
                    if compat_cand.is_dir() and (compat_cand / "proton").is_file():
                        proton_dir = compat_cand
        if proton_dir is None:
            ge_dirs = list_ge_proton11_dirs()
            if ge_dirs:
                proton_dir = ge_dirs[0]
        return (prefix_dir, proton_dir)

    if kind_norm == "external":
        raw_prefix = getattr(obj, "prefix_dir", None)
        if raw_prefix is None:
            raise ValueError("External game object has no prefix_dir attribute.")
        prefix_dir = Path(raw_prefix)
        if (prefix_dir / "pfx").is_dir() and not (prefix_dir / "drive_c").is_dir():
            prefix_dir = prefix_dir / "pfx"
        proton_dir = None
        if paths is not None:
            from .steam_paths import find_proton_binary

            proton_bin = find_proton_binary(paths.steam_root)
            if proton_bin is not None:
                proton_dir = proton_bin.parent
        return (prefix_dir, proton_dir)

    raise ValueError(f"Unsupported game kind: {kind!r}")


def run_winetricks_verbs(
    prefix_dir: Path,
    verbs: list[str],
    proton_dir: Path | None = None,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> None:
    """Install one or more winetricks verbs silently into prefix_dir."""
    if not verbs:
        raise ValueError("At least one winetricks verb is required.")
    for verb in verbs:
        if not _WINETRICKS_VERB_RE.fullmatch(verb):
            raise ValueError(f"Invalid winetricks verb: {verb!r}")

    winetricks = shutil.which("winetricks")
    if winetricks is None:
        raise RuntimeError("winetricks is not installed or not found in PATH.")

    prefix_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["WINEPREFIX"] = str(prefix_dir)
    if proton_dir is not None:
        for rel in ("files/bin/wine", "dist/bin/wine"):
            wine_bin = proton_dir / rel
            if wine_bin.is_file():
                env["WINE"] = str(wine_bin)
                wineserver = wine_bin.parent / "wineserver"
                if wineserver.is_file():
                    env["WINESERVER"] = str(wineserver)
                break

    if cancel_flag and cancel_flag():
        raise RuntimeError("Winetricks cancelled by user.")

    cmd = [winetricks, "-q", *verbs]
    if on_log:
        on_log(f"Running winetricks ({', '.join(verbs)}) in {prefix_dir}...")

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def _handle_line(line: str) -> None:
        if on_log and line.strip():
            on_log(line.strip()[:160])

    rc = drain_stream_lines(proc, cancel_flag, _handle_line, "Winetricks cancelled by user.")
    if rc != 0:
        raise RuntimeError(f"winetricks exited with code {rc} for verbs: {', '.join(verbs)}")


def build_umu_env(
    prefix_dir: Path,
    proton_dir: Path,
    extra_env: dict[str, str] | None = None,
    game_id: str = "umu-default",
    store: str = "none",
) -> dict[str, str]:
    """Build environment variables for executing a Windows binary via umu-run."""
    env = os.environ.copy()
    env["WINEPREFIX"] = str(prefix_dir)
    env["PROTONPATH"] = str(proton_dir)
    env["GAMEID"] = game_id or "umu-default"
    env["STORE"] = store or "none"
    if extra_env:
        env.update(extra_env)
    return env


def run_with_umu(
    exe_path: Path,
    prefix_dir: Path,
    proton_dir: Path | None = None,
    args: list[str] | None = None,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Ensure umu-run, GE-Proton11+, and steamrt4 integrity, then execute exe_path."""
    umu_run = ensure_umu_run()
    resolved_proton = proton_dir if proton_dir is not None else ensure_ge_proton11()
    validate_steamrt4_integrity()
    prefix_dir.mkdir(parents=True, exist_ok=True)
    env = build_umu_env(prefix_dir, resolved_proton, extra_env=extra_env)
    cmd = [str(umu_run), str(exe_path), *(args or [])]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        check=False,
    )


def is_win32_prefix(prefix_dir: Path) -> bool:
    """Return True if prefix_dir/system.reg declares a 32-bit (#arch=win32) Wine prefix."""
    reg_file = prefix_dir / "system.reg"
    try:
        with reg_file.open("r", encoding="utf-8", errors="ignore") as fh:
            has_win32 = False
            has_win64 = False
            for idx, line in enumerate(fh):
                if idx >= 64:
                    break
                if "#arch=win32" in line:
                    has_win32 = True
                if "#arch=win64" in line:
                    has_win64 = True
            return has_win32 and not has_win64
    except OSError:
        return False


def run_exe_in_prefix(
    exe_path: Path,
    prefix_dir: Path,
    proton_path: Path | None = None,
    game_id: str = "umu-default",
    wait: bool = True,
) -> int:
    """Run an arbitrary Windows executable inside an existing Wine prefix via umu-run."""
    resolved_proton = proton_path
    if resolved_proton is None:
        candidates = list_ge_proton11_dirs() or list_ge_proton10_dirs()
        if not candidates:
            raise RuntimeError(
                "No GE-Proton11+ or GE-Proton10 installation found. Install GE-Proton first."
            )
        resolved_proton = candidates[0]

    umu_run = ensure_umu_run()
    prefix_dir.mkdir(parents=True, exist_ok=True)
    env = build_umu_env(prefix_dir, resolved_proton, game_id=game_id)
    cmd = [str(umu_run), str(exe_path)]
    cwd = str(exe_path.parent) if exe_path.parent.is_dir() else None

    if wait:
        proc = subprocess.run(cmd, cwd=cwd, env=env, check=False)
        return proc.returncode

    subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0
