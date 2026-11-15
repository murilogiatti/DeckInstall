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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import __version__

GITHUB_RELEASES_API = "https://api.github.com/repos/murilogiatti/DeckInstall/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/murilogiatti/DeckInstall/releases"

_CHUNK_SIZE = 65536
_APPIMAGE_ENV_KEYS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")
_PATH_ENV_KEYS = ("PATH", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH")


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
    cannot_update_reason: str  # "", "not_appimage", "not_writable", "no_asset"


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison."""
    cleaned = version_str.strip().lstrip("vV")
    core = cleaned.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in re.findall(r"\d+", core))


def get_running_appimage() -> Path | None:
    """Return the path to the currently running AppImage, or None."""
    val = os.environ.get("APPIMAGE", "").strip()
    if not val:
        return None
    path = Path(val)
    return path if path.is_file() else None


def can_replace_appimage(appimage_path: Path | None) -> tuple[bool, str]:
    """Check whether the running AppImage file and its parent directory are writable."""
    if appimage_path is None:
        return False, "not_appimage"
    if not os.access(appimage_path, os.W_OK) or not os.access(appimage_path.parent, os.W_OK):
        return False, "not_writable"
    return True, ""


def _select_appimage_asset(assets: list[dict], arch: str) -> dict | None:
    """Pick the architecture-matching AppImage asset, falling back to any AppImage."""
    arch_prefix = "DeckInstall-"
    arch_suffix = f"-{arch}.AppImage"
    fallback: dict | None = None

    for asset in assets:
        name = str(asset.get("name") or "")
        if name.startswith(arch_prefix) and name.endswith(arch_suffix):
            return asset
        if fallback is None and name.endswith(".AppImage"):
            fallback = asset

    return fallback


def _extract_sha256(asset: dict) -> str:
    """Extract a normalized SHA-256 hex digest from a GitHub release asset."""
    digest = str(asset.get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip()
    return digest


def check_app_update(timeout: float = 10.0) -> AppUpdateInfo:
    """Query GitHub Releases for a newer DeckInstall AppImage release."""
    current_version = __version__
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"DeckInstall/{current_version}",
    }
    response = requests.get(GITHUB_RELEASES_API, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    tag_name = str(data.get("tag_name") or "").strip()
    latest_version = tag_name.lstrip("vV")
    has_update = parse_semver(latest_version) > parse_semver(current_version)

    assets = data.get("assets") or []
    asset = _select_appimage_asset(assets, platform.machine())
    download_url = str(asset.get("browser_download_url") or "") if asset else ""
    asset_name = str(asset.get("name") or "") if asset else ""
    expected_sha256 = _extract_sha256(asset) if asset else ""

    release_notes = str(data.get("body") or "")
    html_url = str(data.get("html_url") or GITHUB_RELEASES_URL)

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
        can_self_update = has_update
        cannot_update_reason = ""

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

    target_path = info.appimage_path
    tmp_path: Path | None = None

    try:
        headers = {"User-Agent": f"DeckInstall/{info.current_version}"}
        with requests.get(info.download_url, headers=headers, stream=True, timeout=60.0) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get("Content-Length") or 0)
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
        if info.expected_sha256 and actual_sha256.lower() != info.expected_sha256.lower():
            raise RuntimeError("SHA256 mismatch")

        try:
            old_mode = stat.S_IMODE(target_path.stat().st_mode)
            new_mode = old_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        except OSError:
            new_mode = 0o755
        os.chmod(tmp_path, new_mode)

        os.replace(tmp_path, target_path)
        return target_path
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def build_clean_relaunch_env() -> dict[str, str]:
    """Build an environment dict stripped of the current AppImage's mount paths."""
    env = os.environ.copy()
    appdir = env.get("APPDIR", "")

    for key in _APPIMAGE_ENV_KEYS:
        env.pop(key, None)

    if appdir:
        appdir_prefix = appdir.rstrip("/") + "/"
        appdir_clean = appdir.rstrip("/")
        for key in _PATH_ENV_KEYS:
            raw_val = env.get(key)
            if not raw_val:
                continue
            filtered = [
                entry
                for entry in raw_val.split(":")
                if entry != appdir_clean and not entry.startswith(appdir_prefix)
            ]
            if filtered:
                env[key] = ":".join(filtered)
            else:
                env.pop(key, None)

    return env


def relaunch_appimage(appimage_path: Path) -> None:
    """Spawn the updated AppImage in a new session with a clean environment."""
    subprocess.Popen(
        [str(appimage_path), *sys.argv[1:]],
        start_new_session=True,
        env=build_clean_relaunch_env(),
    )
