"""Safe in-place AppImage self-updating for DeckInstall."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__
from .fsutil import clean_child_env, file_lock

GITHUB_RELEASES_API = "https://api.github.com/repos/murilogiatti/DeckInstall/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/murilogiatti/DeckInstall/releases"

_CHUNK_SIZE = 65536
_MAX_CHECKSUM_BYTES = 512 * 1024
_ALLOWED_URL_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _validate_github_url(url: str) -> str:
    """Validate that url uses HTTPS and points to an allowlisted GitHub/CDN host."""
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in _ALLOWED_URL_HOSTS:
        raise ValueError(f"Disallowed update URL (must be HTTPS on GitHub): {url!r}")
    return cleaned


def _request_github_with_validated_redirects(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    stream: bool = False,
    max_redirects: int = 5,
) -> requests.Response:
    """Perform a GET request validating every redirect hop against _ALLOWED_URL_HOSTS."""
    current_url = _validate_github_url(url)
    for _ in range(max_redirects + 1):
        resp = requests.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )
        status = getattr(resp, "status_code", 200)
        is_redir = (getattr(resp, "is_redirect", False) is True) or (
            isinstance(status, int) and status in (301, 302, 303, 307, 308)
        )
        if is_redir:
            location = (getattr(resp, "headers", None) or {}).get("Location") or ""
            resp.close()
            if not location:
                raise RuntimeError("Redirect response missing Location header.")
            current_url = _validate_github_url(urllib.parse.urljoin(current_url, location))
            continue
        return resp
    raise RuntimeError("Too many redirects while fetching GitHub resource.")


@dataclass(frozen=True)
class AppUpdateInfo:
    """Metadata and eligibility details for an AppImage self-update."""

    current_version: str
    latest_version: str
    has_update: bool
    download_url: str
    asset_name: str
    expected_sha256: str
    release_notes: str
    html_url: str
    is_appimage: bool
    appimage_path: Path | None
    can_self_update: bool
    cannot_update_reason: str  # "", "not_appimage", "not_writable", "no_asset", "no_checksum"


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison."""
    cleaned = version_str.strip().lstrip("vV")
    core = cleaned.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in re.findall(r"\d+", core))


def get_running_appimage() -> Path | None:
    """Return the path to the currently running AppImage, or None."""
    val = os.environ.get("APPIMAGE", "").strip()
    appdir_val = os.environ.get("APPDIR", "").strip()
    if not val or not appdir_val:
        return None
    path = Path(val)
    appdir = Path(appdir_val)
    if not path.is_absolute() or not appdir.is_absolute() or path.is_symlink() or not path.is_file():
        return None
    try:
        mounted = appdir.resolve(strict=True)
        if not (mounted / "AppRun").is_file():
            return None
        entrypoints = (Path(sys.executable).resolve(), Path(__file__).resolve())
        if not any(entry.is_relative_to(mounted) for entry in entrypoints):
            return None
        if path.resolve().is_relative_to(mounted):
            return None
    except (OSError, RuntimeError):
        return None
    return path


def can_replace_appimage(appimage_path: Path | None) -> tuple[bool, str]:
    """Check whether the running AppImage file and its parent directory are writable."""
    if appimage_path is None:
        return False, "not_appimage"
    if not os.access(appimage_path, os.W_OK) or not os.access(appimage_path.parent, os.W_OK):
        return False, "not_writable"
    return True, ""


def _normalize_arch(machine: str | None = None) -> str:
    """Normalize a machine architecture string (e.g. amd64 -> x86_64, arm64 -> aarch64)."""
    raw = (machine if machine is not None else platform.machine()).strip().lower()
    if raw in ("x86_64", "amd64"):
        return "x86_64"
    if raw in ("aarch64", "arm64"):
        return "aarch64"
    return raw


def _select_appimage_asset(assets: list[dict], arch: str) -> dict | None:
    """Pick the architecture-matching AppImage asset, or None if unavailable."""
    normalized_arch = _normalize_arch(arch)
    arch_prefix = "DeckInstall-"
    arch_suffix = f"-{normalized_arch}.AppImage"

    for asset in assets:
        name = str(asset.get("name") or "")
        if name.startswith(arch_prefix) and name.endswith(arch_suffix):
            return asset

    for asset in assets:
        name = str(asset.get("name") or "")
        if name.endswith(arch_suffix):
            return asset

    return None


def _extract_sha256(asset: dict) -> str:
    """Extract a normalized SHA-256 hex digest from a GitHub release asset."""
    digest = str(asset.get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        digest = digest.split(":", 1)[1].strip()
    if not digest:
        return ""
    if not _SHA256_HEX_RE.match(digest):
        raise ValueError(f"Invalid SHA-256 digest format: {digest!r}")
    return digest.lower()


def _release_checksum(assets: list[dict], name: str, headers: dict[str, str], timeout: float) -> str:
    url = next((str(a.get("browser_download_url") or "") for a in assets if a.get("name") == "SHA256SUMS"), "")
    if not url:
        return ""
    data = bytearray()
    try:
        with _request_github_with_validated_redirects(url, headers=headers, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if len(data) + len(chunk) > _MAX_CHECKSUM_BYTES:
                    return ""
                data.extend(chunk)
        matches = set()
        for line in data.decode("utf-8").splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) == 2 and fields[1].removeprefix("*") == name and _SHA256_HEX_RE.fullmatch(fields[0]):
                matches.add(fields[0].lower())
        return next(iter(matches)) if len(matches) == 1 else ""
    except (requests.RequestException, OSError, ValueError, RuntimeError):
        return ""


def check_app_update(timeout: float = 10.0) -> AppUpdateInfo:
    """Query GitHub Releases for a newer DeckInstall AppImage release."""
    current_version = __version__
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"DeckInstall/{current_version}",
    }
    api_url = _validate_github_url(GITHUB_RELEASES_API)
    response = _request_github_with_validated_redirects(api_url, headers=headers, timeout=timeout)
    try:
        response.raise_for_status()
        data = response.json()
    finally:
        if hasattr(response, "close"):
            response.close()

    tag_name = str(data.get("tag_name") or "").strip()
    latest_version = tag_name.lstrip("vV")
    has_update = parse_semver(latest_version) > parse_semver(current_version)

    assets = data.get("assets") or []
    asset = _select_appimage_asset(assets, _normalize_arch())
    download_url = str(asset.get("browser_download_url") or "") if asset else ""
    if download_url:
        download_url = _validate_github_url(download_url)
    asset_name = str(asset.get("name") or "") if asset else ""
    try:
        expected_sha256 = _extract_sha256(asset) if asset else ""
    except ValueError:
        expected_sha256 = ""

    release_notes = str(data.get("body") or "")
    raw_html_url = str(data.get("html_url") or GITHUB_RELEASES_URL)
    try:
        html_url = _validate_github_url(raw_html_url)
    except ValueError:
        html_url = GITHUB_RELEASES_URL

    appimage_path = get_running_appimage()
    is_appimage = appimage_path is not None
    can_replace, reason = can_replace_appimage(appimage_path)

    if not can_replace:
        can_self_update = False
        cannot_update_reason = reason
    elif has_update and not download_url:
        can_self_update = False
        cannot_update_reason = "no_asset"
    else:
        if has_update and not expected_sha256:
            expected_sha256 = _release_checksum(assets, asset_name, headers, timeout)
        can_self_update = has_update and bool(expected_sha256)
        cannot_update_reason = "no_checksum" if has_update and not expected_sha256 else ""

    return AppUpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        has_update=has_update,
        download_url=download_url,
        asset_name=asset_name,
        expected_sha256=expected_sha256,
        release_notes=release_notes,
        html_url=html_url,
        is_appimage=is_appimage,
        appimage_path=appimage_path,
        can_self_update=can_self_update,
        cannot_update_reason=cannot_update_reason,
    )


def download_and_replace_appimage(
    info: AppUpdateInfo,
    progress_cb: Callable[[int], None] | None = None,
) -> Path:
    """Download the new AppImage, verify its SHA-256, and atomically replace the current one."""
    if not info.can_self_update or not info.download_url or info.appimage_path is None:
        raise ValueError("Self-update is not available for the provided AppUpdateInfo.")
    if not info.expected_sha256:
        raise RuntimeError("Missing expected SHA-256 digest")
    if not _SHA256_HEX_RE.match(info.expected_sha256):
        raise RuntimeError("Invalid expected SHA-256 digest")
    download_url = _validate_github_url(info.download_url)

    target_path = info.appimage_path
    if target_path != get_running_appimage():
        raise RuntimeError("The update target is not the running DeckInstall AppImage")
    with file_lock(target_path.with_name(f".{target_path.name}.update.lock")):
        return _replace_appimage(info, target_path, download_url, progress_cb)


def _replace_appimage(
    info: AppUpdateInfo, target_path: Path, download_url: str, progress_cb: Callable[[int], None] | None,
) -> Path:
    original = target_path.lstat()
    tmp_path: Path | None = None

    try:
        headers = {"User-Agent": f"DeckInstall/{info.current_version}"}
        with _request_github_with_validated_redirects(
            download_url, headers=headers, stream=True, timeout=60.0
        ) as resp:
            resp.raise_for_status()
            length = (resp.headers.get("Content-Length") or "").strip()
            total_size = int(length) if length.isascii() and length.isdecimal() and len(length) <= 20 else 0
            downloaded = 0
            last_percent = -1
            hasher = hashlib.sha256()

            with tempfile.NamedTemporaryFile(
                dir=target_path.parent,
                prefix=".deckinstall-update-",
                suffix=".AppImage.tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    tmp_file.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None and total_size > 0:
                        percent = min(100, int(downloaded * 100 / total_size))
                        if percent != last_percent:
                            last_percent = percent
                            progress_cb(percent)

                tmp_file.flush()
                os.fsync(tmp_file.fileno())

        if progress_cb is not None and last_percent < 100:
            progress_cb(100)

        actual_sha256 = hasher.hexdigest()
        if actual_sha256.lower() != info.expected_sha256.lower():
            raise RuntimeError("SHA256 mismatch")

        current = target_path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_ctime_ns)
            != (original.st_dev, original.st_ino, original.st_ctime_ns)
        ):
            raise RuntimeError("The AppImage changed during the download")
        new_mode = (original.st_mode & 0o777) | stat.S_IXUSR
        os.chmod(tmp_path, new_mode)

        os.replace(tmp_path, target_path)
        return target_path
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def build_clean_relaunch_env() -> dict[str, str]:
    """Build an environment dict stripped of the current AppImage's mount paths."""
    return clean_child_env()


def relaunch_appimage(appimage_path: Path) -> None:
    """Spawn the updated AppImage in a new session with a clean environment."""
    subprocess.Popen(
        [str(appimage_path), *sys.argv[1:]],
        start_new_session=True,
        env=build_clean_relaunch_env(),
    )
