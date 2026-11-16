"""Portable umu-launcher and GE-Proton11+ runtime manager."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from . import __version__

UMU_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/Open-Wine-Components/umu-launcher/releases/latest"
)
GE_PROTON_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
)
GE_PROTON_RELEASES_URL = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
)

UPDATE_CACHE_TTL_SECONDS = 24 * 60 * 60
USER_AGENT = f"DeckInstall/{__version__}"
_GE_PROTON_TAG_RE = re.compile(r"^GE-Proton(\d+)(.*)$")
_SHA512_RE = re.compile(r"\b([0-9a-fA-F]{128})\b")

ProgressCallback = Callable[[str, int], None]


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


def _parse_ge_proton_version(name: str) -> tuple[int, ...] | None:
    """Parse GE-Proton11+ folder/tag names; return None for Proton 10 and older."""
    match = _GE_PROTON_TAG_RE.match(name)
    if not match:
        return None
    major = int(match.group(1))
    if major < 11:
        return None
    rest = [int(part) for part in re.findall(r"\d+", match.group(2))]
    return (major, *rest)


def list_ge_proton11_dirs() -> list[Path]:
    """List installed GE-Proton11+ directories sorted newest first."""
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
            version = _parse_ge_proton_version(entry.name)
            if version is not None:
                seen_entries.add(resolved_entry)
                found.append((version, entry))

    found.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in found]


def _load_versions() -> dict[str, Any]:
    path = get_versions_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_versions(data: dict[str, Any]) -> None:
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


def _fetch_json(url: str, timeout: int = 20) -> Any:
    resp = requests.get(
        url,
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
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        url,
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
    """Safely extract a tar/tar.gz archive preventing path traversal."""
    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe archive member path: {member.name}")
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
    match = _SHA512_RE.search(checksum_text)
    if not match:
        raise RuntimeError("Invalid .sha512sum file format.")
    expected = match.group(1).lower()
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
    asset_name = str(zipapp_asset.get("name", "umu-launcher-zipapp.tar"))
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

    arch = _get_arch()
    tar_name = f"{tag}-{arch}.tar.gz"
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

    with tempfile.TemporaryDirectory(prefix="deckinstall_ge_", dir=str(compat_dir)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        tar_path = tmp_path / tar_name
        sha_path = tmp_path / str(sha_asset.get("name", f"{tag}-{arch}.sha512sum"))

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

        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.move(str(extracted_root), str(target_dir))

    versions = _load_versions()
    versions["ge_proton_latest_version"] = tag
    _save_versions(versions)
    _emit_progress(progress_cb, f"{tag} ready.", 100)
    return target_dir


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


def build_umu_env(
    prefix_dir: Path,
    proton_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build environment variables for executing a Windows binary via umu-run."""
    env = os.environ.copy()
    env["WINEPREFIX"] = str(prefix_dir)
    env["PROTONPATH"] = str(proton_dir)
    env["GAMEID"] = "umu-default"
    env["STORE"] = "none"
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
