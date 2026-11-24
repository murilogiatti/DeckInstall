"""Portable umu-launcher and GE-Proton11+ runtime manager."""

from __future__ import annotations

import codecs
import contextlib
import csv
import hashlib
import inspect
import json
import logging
import os
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ParamSpec, TypeVar

import requests

from . import __version__
from .fsutil import (
    FileIdentity,
    atomic_write_bytes,
    atomic_write_text,
    clean_child_env,
    file_identity,
    file_lock,
    file_sha256,
    open_regular_fd,
)
from .steam_paths import dir_size_bytes as _dir_size_bytes

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
MANGOHUD_RELEASES_API = (
    "https://api.github.com/repos/flightlessmango/MangoHud/releases/latest"
)

UPDATE_CACHE_TTL_SECONDS = 24 * 60 * 60
USER_AGENT = f"DeckInstall/{__version__}"
_GE_PROTON_TAG_RE = re.compile(r"^GE-Proton(\d+)([A-Za-z0-9._-]*)$")
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
_ALLOWED_UMU_DB_HOSTS = frozenset({"raw.githubusercontent.com"})
# umu.openwinecomponents.org/umu-database.csv serves an HTML page, not the CSV.
UMU_DATABASE_URL = "https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-database.csv"
UMU_DATABASE_TTL_SECONDS = 7 * 24 * 60 * 60
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _serialize_runtime(name: str) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Share a lock across installs, repairs and removal of the same runtime."""
    def decorate(fn: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(fn)
        def locked(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with file_lock(get_runtime_dir() / ".locks" / f"{name}.lock"):
                return fn(*args, **kwargs)

        return locked

    return decorate

def _runtime_temp_prefix(component: str) -> str:
    return f"deckinstall_{component}_"


WINETRICKS_PRESETS: dict[str, str] = {
    "vcrun2022": "Visual C++ 2015-2022 Redistributable",
    "vcrun2013": "Visual C++ 2013 Redistributable (msvcr120)",
    "d3dcompiler_47": "Direct3D Shader Compiler (d3dcompiler_47)",
    "d3dx9": "DirectX 9 Runtime (d3dx9)",
    "dotnet48": ".NET Framework 4.8 (dotnet48)",
    "xact": "XAudio / XACT Runtime (xact)",
    "physx": "NVIDIA PhysX Runtime (physx)",
    "corefonts": "Microsoft Core Fonts (Arial, Times, etc.)",
}

# Windows version verbs (winetricks), newest first.
WINDOWS_VERSIONS: dict[str, str] = {
    "win11": "Windows 11",
    "win10": "Windows 10",
    "win81": "Windows 8.1",
    "win7": "Windows 7",
    "winxp": "Windows XP",
}
# Wine's own tools, run by name inside a prefix (umu-run hands the name to Proton).
WINE_TOOLS = ("winecfg", "regedit", "taskmgr")

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
    sha256_digest: str | None = None
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


@dataclass(frozen=True)
class ProtonIntegrityReport:
    """Integrity verification result for an installed GE-Proton directory."""

    proton_dir: Path
    valid: bool
    missing_files: tuple[str, ...]
    broken_symlinks: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class UmuDbLookupResult:
    """Match result from the local umu-database.csv cache."""

    umu_id: str
    store: str
    title: str
    common_redist: str
    note: str
    match_source: str


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

    gpu_cards = 0
    drm_root = Path("/sys/class/drm")
    if drm_root.is_dir():
        for vendor_file in drm_root.glob("card*/device/vendor"):
            if "-" in vendor_file.parent.parent.name:  # connector (card1-eDP-1), not a GPU
                continue
            try:
                vendor_id = vendor_file.read_text(encoding="utf-8").strip().lower()
            except OSError:
                continue
            if not vendor_id:
                continue
            found_vendor_file = True
            gpu_cards += 1
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
                    if "[1002:" in low or "amd" in low or "radeon" in low or re.search(r"\bati\b|\[amd/ati\]", low):
                        has_amd = True
                    if "[8086:" in low or "intel" in low:
                        has_intel = True
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Two vendors, or two GPUs of one vendor (Ryzen iGPU + Radeon dGPU, Intel iGPU + Arc).
    is_hybrid = sum((has_nvidia, has_amd, has_intel)) >= 2 or gpu_cards >= 2
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


def get_mangohud_dir() -> Path:
    """Official MangoHud build kept by DeckInstall (lib32/, lib64/ and the layer manifests)."""
    return get_runtime_dir() / "mangohud"


def get_user_vulkan_layer_dir() -> Path:
    """User Vulkan implicit layers: searched before the system ones, also inside Steam's runtime."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "vulkan" / "implicit_layer.d"


_MANGOHUD_LAYERS = (("MangoHud.x86_64.json", "lib64"), ("MangoHud.x86.json", "lib32"))
_GE_MANAGED_MARKER = ".deckinstall-managed"


def official_mangohud_installed() -> bool:
    """True when the user layers point at DeckInstall's official MangoHud and its libraries exist."""
    for json_name, lib in _MANGOHUD_LAYERS:
        expected = get_mangohud_dir() / lib / "libMangoHud.so"
        try:
            layer = json.loads((get_user_vulkan_layer_dir() / json_name).read_text(encoding="utf-8"))
            path = Path(str(layer["layer"]["library_path"]))
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if path != expected or not expected.is_file():
            return False
    return True


_MANGOHUD_LIB_DIRS = ("lib", "lib64", "lib32", "lib/x86_64-linux-gnu", "lib/i386-linux-gnu")


def _resolve_system_layer_lib(raw_path: str) -> Path | None:
    """Resolve an implicit Vulkan layer library_path (including $LIB tokens) to an existing file."""
    cleaned = raw_path.strip()
    if not cleaned:
        return None
    if "$LIB" in cleaned:
        for lib_sub in _MANGOHUD_LIB_DIRS:
            candidate = Path(cleaned.replace("$LIB", lib_sub))
            try:
                if candidate.is_absolute() and candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None
    candidate = Path(cleaned)
    try:
        return candidate if candidate.is_absolute() and candidate.is_file() else None
    except OSError:
        return None


def system_mangohud_installed() -> bool:
    """Recognize a native installation from its Vulkan manifests and libraries."""
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for base in map(Path, data_dirs.split(os.pathsep)):
        try:
            if not base.is_absolute() or base.is_relative_to(Path.home()):
                continue
        except ValueError:
            pass
        layers = base / "vulkan" / "implicit_layer.d"
        try:
            libraries = [
                _resolve_system_layer_lib(
                    str(json.loads((layers / name).read_text(encoding="utf-8"))["layer"]["library_path"])
                )
                for name, _ in _MANGOHUD_LAYERS
            ]
        except (OSError, ValueError, KeyError, TypeError):
            libraries = []
        if libraries and all(lib is not None for lib in libraries):
            return True
        single_layer = layers / "MangoHud.json"
        if single_layer.is_file():
            try:
                raw_lib = str(json.loads(single_layer.read_text(encoding="utf-8"))["layer"]["library_path"])
            except (OSError, ValueError, KeyError, TypeError):
                raw_lib = ""
            if _resolve_system_layer_lib(raw_lib) is not None:
                return True
    return False


def _mangohud_layers(mh_dir: Path, target: Path) -> dict[str, str]:
    layers: dict[str, str] = {}
    for json_name, lib in _MANGOHUD_LAYERS:
        library = mh_dir / lib / "libMangoHud.so"
        if not library.is_file():
            raise FileNotFoundError(f"Incomplete MangoHud library: {library}")
        layer = json.loads((mh_dir / "layers" / json_name).read_text(encoding="utf-8"))
        layer["layer"]["library_path"] = str(target / lib / "libMangoHud.so")
        layers[json_name] = json.dumps(layer, indent=4) + "\n"
    return layers


def _write_mangohud_layers(layers: dict[str, str]) -> None:
    """Install both Vulkan manifests, restoring the originals if either write fails."""
    layer_dir = get_user_vulkan_layer_dir()
    layer_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as rollback:
        for name, content in layers.items():
            path = layer_dir / name
            if path.is_symlink():
                raise ValueError(f"Refusing to replace a symlinked Vulkan layer: {path}")
            previous = path.read_bytes() if path.is_file() else None
            atomic_write_text(path, content)
            if previous is None:
                rollback.callback(path.unlink, missing_ok=True)
            else:
                rollback.callback(atomic_write_bytes, path, previous)
        rollback.pop_all()


def _pick_mangohud_asset(release: dict[str, Any]) -> dict[str, Any]:
    for asset in release.get("assets") or []:
        name = str(asset.get("name", "")) if isinstance(asset, dict) else ""
        if re.fullmatch(r"MangoHud-[\w.+-]+\.tar\.gz", name) and "Source" not in name:
            return asset
    raise RuntimeError("No MangoHud binary package in the latest GitHub release.")


@_serialize_runtime("mangohud")
def ensure_mangohud(force_update: bool = False, progress_cb: ProgressCb | None = None) -> Path:
    """Install (or update) the official MangoHud from GitHub for this user, on any distro, without root.

    Distro packages can be built without NVIDIA support (Debian/Ubuntu "DFSG": NVML removed), so
    DeckInstall always uses the upstream build: libraries under runtime/mangohud and the Vulkan
    layer manifests in ~/.local/share/vulkan/implicit_layer.d, which win over a system MangoHud.
    """
    mh_dir = get_mangohud_dir()
    mh_dir.parent.mkdir(parents=True, exist_ok=True)
    if mh_dir.is_symlink():
        raise ValueError(f"Refusing a symlinked MangoHud directory: {mh_dir}")
    if not force_update:
        try:
            layers = _mangohud_layers(mh_dir, mh_dir)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # An incomplete cache is repaired with a fresh download.
        else:
            _write_mangohud_layers(layers)
            return mh_dir
    _emit_progress(progress_cb, "Checking latest MangoHud release...", 0)
    release = _fetch_json(MANGOHUD_RELEASES_API)
    if not isinstance(release, dict):
        raise RuntimeError("Unexpected GitHub release response for MangoHud.")
    tag = str(release.get("tag_name", "")).strip()
    asset = _pick_mangohud_asset(release)
    asset_name = _sanitize_asset_name(str(asset["name"]))
    with tempfile.TemporaryDirectory(prefix=_runtime_temp_prefix("mangohud"), dir=str(mh_dir.parent)) as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset_name
        _download_file(str(asset.get("browser_download_url", "")), archive, asset_name, progress_cb)
        _verify_release_asset_integrity(release, asset, archive, tmp_path, label="MangoHud")
        _emit_progress(progress_cb, "Extracting MangoHud...", 90)
        outer, inner = tmp_path / "outer", tmp_path / "pkg"
        outer.mkdir()
        inner.mkdir()
        _safe_extract_tar(archive, outer)
        package = next(outer.rglob("MangoHud-package.tar"), None)
        if package is None:
            raise RuntimeError("MangoHud-package.tar missing from the MangoHud release.")
        _safe_extract_tar(package, inner)
        staged = tmp_path / "mangohud"
        (staged / "layers").mkdir(parents=True)
        for json_name, lib in _MANGOHUD_LAYERS:
            src_lib = inner / "usr" / "lib" / "mangohud" / lib
            if not (src_lib / "libMangoHud.so").is_file():
                raise RuntimeError(f"MangoHud release has no {lib}/libMangoHud.so.")
            shutil.copytree(src_lib, staged / lib, symlinks=False)
            shutil.copy2(inner / "usr" / "share" / "vulkan" / "implicit_layer.d" / json_name, staged / "layers")
        layers = _mangohud_layers(staged, mh_dir)
        with _publish_runtime_directory(staged, mh_dir):
            _write_mangohud_layers(layers)

    def _record(versions: dict[str, Any]) -> None:
        versions["mangohud_installed_version"] = tag
        versions["mangohud_latest_version"] = tag
        versions["mangohud_last_check"] = int(time.time())

    _update_versions(_record)
    _emit_progress(progress_cb, f"MangoHud {tag} installed.", 100)
    return mh_dir


@_serialize_runtime("mangohud")
def mangohud_versions() -> tuple[str, str]:
    """(installed tag or "", latest tag on GitHub or "") of the official MangoHud."""
    versions = _load_versions()
    installed = str(versions.get("mangohud_installed_version") or "") if official_mangohud_installed() else ""
    latest = str(versions.get("mangohud_latest_version") or "")
    now = int(time.time())
    last_check = versions.get("mangohud_last_check", 0)
    if isinstance(last_check, int) and latest and 0 <= now - last_check < UPDATE_CACHE_TTL_SECONDS:
        return installed, latest
    release = _fetch_json(MANGOHUD_RELEASES_API)
    latest = str(release.get("tag_name", "")).strip() if isinstance(release, dict) else ""
    if latest:
        _update_versions(lambda data: data.update(mangohud_latest_version=latest, mangohud_last_check=now))
    return installed, latest


@_serialize_runtime("mangohud")
def uninstall_mangohud() -> bool:
    """Remove DeckInstall's official MangoHud and its user layers (a layer pointing elsewhere is kept)."""
    mh_dir = get_mangohud_dir()
    mh_resolved = mh_dir.resolve()
    removed = False
    layer_names = (*[json_name for json_name, _lib in _MANGOHUD_LAYERS], "MangoHud.json")
    layer_dir = get_user_vulkan_layer_dir()
    for json_name in layer_names:
        layer = layer_dir / json_name
        if layer.is_symlink():
            continue
        try:
            data = json.loads(layer.read_text(encoding="utf-8"))
            ours = Path(str(data["layer"]["library_path"])).resolve().is_relative_to(mh_resolved)
        except (OSError, ValueError, KeyError, TypeError):
            ours = False
        if ours:
            layer.unlink(missing_ok=True)
            removed = True
    if mh_dir.is_dir() and not mh_dir.is_symlink():
        shutil.rmtree(mh_dir)
        removed = True

    def _forget(versions: dict[str, Any]) -> None:
        versions.pop("mangohud_installed_version", None)

    _update_versions(_forget)
    return removed


def _remove_runtime_dir(path: Path, version_keys: tuple[str, ...] = ()) -> bool:
    """Remove a runtime folder that DeckInstall downloaded (never a symlink)."""
    if not path.is_dir() or path.is_symlink():
        return False
    shutil.rmtree(path)

    def _forget(versions: dict[str, Any]) -> None:
        for key in version_keys:
            versions.pop(key, None)

    if version_keys:
        _update_versions(_forget)
    return True


@_serialize_runtime("umu")
def uninstall_umu_run() -> bool:
    """Remove the portable umu-run (installed again by "Update runtimes" or the next launch)."""
    return _remove_runtime_dir(get_umu_dir(), ("umu_installed_version",))


def uninstall_steamrt4() -> bool:
    """Remove umu's Steam Runtime 4 (umu downloads it again on the next launch)."""
    return _remove_runtime_dir(get_steamrt4_dir())


@_serialize_runtime("optiscaler")
def uninstall_optiscaler_package() -> bool:
    """Remove the downloaded OptiScaler package (games keep the copy installed in their folder)."""
    return _remove_runtime_dir(get_optiscaler_dir().parent, ("optiscaler_installed_version",))


def get_umu_database_path() -> Path:
    """Return the local cache path for umu-database.csv."""
    return get_runtime_dir() / "umu-database.csv"


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


def _all_compat_tool_dirs(steam_root: Path | None = None) -> list[Path]:
    """Return every compatibilitytools.d directory to search (writable one first, may contain duplicates)."""
    from .steam_paths import _iter_compat_tool_dirs, find_steam_root

    root = steam_root if steam_root is not None else find_steam_root()
    dirs: list[Path] = [get_compat_tools_dir()]
    if root is not None:
        dirs.extend(_iter_compat_tool_dirs(root))
    else:
        for sys_dir in (
            Path("/usr/share/steam/compatibilitytools.d"),
            Path("/usr/local/share/steam/compatibilitytools.d"),
        ):
            if sys_dir.is_dir():
                dirs.append(sys_dir)
    dirs.extend(_iter_extra_compat_tool_dirs())
    return dirs


def get_steamrt4_dir() -> Path:
    """Return the umu steamrt4 runtime directory."""
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser() / "umu" / "steamrt4"
    return Path.home() / ".local" / "share" / "umu" / "steamrt4"


def find_runtime_leftovers() -> dict[Path, str]:
    """Runtime installer workspaces and replaced builds with a working successor."""
    runtime = get_runtime_dir()
    compat = get_compat_tools_dir()
    opti = get_optiscaler_dir()
    mango = get_mangohud_dir()
    locations = (
        (runtime, "mangohud", (_runtime_temp_prefix("mangohud"),)),
        (runtime, "umu", (_runtime_temp_prefix("umu"),)),
        (opti.parent, "optiscaler", (_runtime_temp_prefix("optiscaler"), f".{opti.name}_stage_")),
        (compat, "compat-tools", (_runtime_temp_prefix("ge"), _runtime_temp_prefix("proton"))),
    )
    leftovers: dict[Path, str] = {}

    def children(root: Path) -> list[Path]:
        try:
            if not root.is_dir() or root.absolute() != root.resolve():
                return []
            return [
                path for path in root.iterdir()
                if not path.is_symlink() and path.is_dir() and path.stat().st_uid == os.getuid()
            ]
        except (OSError, RuntimeError):
            return []

    def matches(name: str, prefix: str) -> bool:
        return name.startswith(prefix) and bool(re.fullmatch(r"[a-z0-9_]{8}", name[len(prefix):]))

    for root, lock, prefixes in locations:
        for child in children(root):
            if any(matches(child.name, prefix) for prefix in prefixes):
                leftovers[child] = lock

    replacements = [
        (mango, "mangohud", official_mangohud_installed()),
        (opti, "optiscaler", _has_optiscaler_dll(opti)),
    ]
    replacements.extend(
        (path, "compat-tools", True) for path in children(compat)
        if is_managed_compat_tool(path) and (path / "proton").is_file()
    )
    for current, lock, valid in replacements:
        if not valid or current.is_symlink():
            continue
        prefix = f".{current.name}_old_"
        for child in children(current.parent):
            if matches(child.name, prefix):
                leftovers[child] = lock
    return leftovers


def remove_runtime_leftover(path: Path) -> bool:
    """Serialize deletion with the installer that owns the workspace."""
    lock = find_runtime_leftovers().get(path)
    if lock is None:
        return False
    with file_lock(get_runtime_dir() / ".locks" / f"{lock}.lock"):
        if find_runtime_leftovers().get(path) != lock:
            return False
        shutil.rmtree(path)
        return True


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
    candidate_dirs = _all_compat_tool_dirs()

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


def is_managed_compat_tool(path: Path) -> bool:
    """True for any compatibility tool directory recorded as installed by this app."""
    marker = path / _GE_MANAGED_MARKER
    try:
        return (
            path.is_dir()
            and not path.is_symlink()
            and path.parent.resolve() == get_compat_tools_dir().resolve()
            and not marker.is_symlink()
            and marker.is_file()
        )
    except OSError:
        return False


def is_managed_ge_proton(path: Path) -> bool:
    """True for a GE-Proton directory recorded as installed by this app."""
    return is_managed_compat_tool(path) and _parse_ge_proton_version(path.name, min_major=1) is not None


def superseded_managed_compat_tools() -> list[Path]:
    """Return older owned releases, preserving the newest installed build of each variant."""
    ge = list_ge_proton_dirs(min_major=1)
    superseded = [path for path in ge[1:] if is_managed_ge_proton(path)]
    other_families = {
        name.lower(): family.id
        for family in PROTON_FAMILIES.values() if family.id != "ge-proton"
        for name in (family.id, family.repo.rsplit("/", 1)[-1])
    }
    root = get_compat_tools_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return superseded
    variants: dict[tuple[str, str], list[tuple[tuple[int, ...], Path]]] = {}
    for tool in entries:
        for prefix, family in other_families.items():
            if not tool.name.lower().startswith(f"{prefix}-"):
                continue
            release = re.fullmatch(r"([0-9]+(?:[._-][0-9]+)*)([-_.].*)?", tool.name[len(prefix) + 1:])
            if release is None or tool.is_symlink() or not (tool / "proton").is_file():
                break
            version = tuple(int(part) for part in re.findall(r"[0-9]+", release[1]))
            variant = (release[2] or "").lower()
            variants.setdefault((family, variant), []).append((version, tool))
            break
    for builds in variants.values():
        newest = max(version for version, _ in builds)
        superseded.extend(p for version, p in builds if version < newest and is_managed_compat_tool(p))
    return superseded


def list_ge_proton11_dirs() -> list[Path]:
    """List installed GE-Proton11+ directories sorted newest first."""
    return list_ge_proton_dirs(min_major=11)


def list_cachyos_proton_dirs() -> list[Path]:
    """List installed Proton-CachyOS directories (system or user compatibilitytools.d) sorted newest first."""
    candidate_dirs = _all_compat_tool_dirs()
    seen_dirs: set[Path] = set()
    seen_entries: set[Path] = set()
    found: list[tuple[tuple[int, ...], str, Path]] = []
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
            low = entry.name.lower()
            if not (low.startswith("proton-cachyos") or low.startswith("cachyos-proton")):
                continue
            try:
                if not entry.is_dir() or not (entry / "proton").is_file():
                    continue
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if resolved_entry in seen_entries:
                continue
            seen_entries.add(resolved_entry)
            nums = tuple(int(n) for n in re.findall(r"\d+", entry.name))
            found.append((nums, low, entry))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in found]


def list_ge_proton10_dirs() -> list[Path]:
    """List installed GE-Proton10 directories sorted newest first."""
    return list_ge_proton_dirs(min_major=10, max_major=10)


@contextlib.contextmanager
def _versions_lock():
    """Serialize read/write access to versions.json across threads and processes."""
    with file_lock(get_versions_file().with_suffix(".lock")):
        yield


def _read_versions_unlocked() -> dict[str, Any]:
    path = get_versions_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_versions_unlocked(data: dict[str, Any]) -> None:
    path = get_versions_file()
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _load_versions() -> dict[str, Any]:
    with _versions_lock():
        return _read_versions_unlocked()


def _update_versions(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically read, mutate, and write versions.json under _versions_lock."""
    with _versions_lock():
        data = _read_versions_unlocked()
        mutator(data)
        _write_versions_unlocked(data)
        return data


def _validate_allowed_url(
    url: str,
    allowed_hosts: frozenset[str],
    label: str = "resource",
) -> str:
    """Validate that url uses HTTPS and points to an allowlisted host."""
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in allowed_hosts:
        raise ValueError(f"Disallowed URL for {label} (must be HTTPS on allowlisted host): {url!r}")
    return cleaned


def _validate_github_url(url: str) -> str:
    """Validate that url uses HTTPS and points to an allowlisted GitHub/CDN host."""
    return _validate_allowed_url(url, _ALLOWED_URL_HOSTS, label="GitHub")


def _request_with_validated_redirects(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    headers: dict[str, str],
    timeout: float,
    stream: bool = False,
    max_redirects: int = 5,
    label: str = "resource",
) -> requests.Response:
    """Perform a GET request validating every redirect hop against allowed_hosts."""
    current_url = _validate_allowed_url(url, allowed_hosts, label=label)
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
            current_url = _validate_allowed_url(
                urllib.parse.urljoin(current_url, location),
                allowed_hosts,
                label=label,
            )
            continue
        return resp
    raise RuntimeError(f"Too many redirects while fetching {label}.")


def _request_github_with_validated_redirects(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    stream: bool = False,
    max_redirects: int = 5,
) -> requests.Response:
    """Perform a GET request validating every redirect hop against _ALLOWED_URL_HOSTS."""
    return _request_with_validated_redirects(
        url,
        allowed_hosts=_ALLOWED_URL_HOSTS,
        headers=headers,
        timeout=timeout,
        stream=stream,
        max_redirects=max_redirects,
        label="GitHub resource",
    )


def _sanitize_asset_name(name: str) -> str:
    """Ensure a release asset filename is a single safe basename."""
    raw = name.strip()
    if not raw or "/" in raw or "\\" in raw:
        raise ValueError(f"Unsafe asset filename: {name!r}")
    cleaned = Path(raw).name
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Unsafe asset filename: {name!r}")
    return cleaned


GITHUB_TOKEN_ENV = "DECKINSTALL_GITHUB_TOKEN"
_FETCH_RETRIES = 2  # extra attempts after a network error or a 5xx answer


def github_cache_dir() -> Path:
    """Where GitHub API answers are kept with their ETag (re-asked with If-None-Match)."""
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "DeckInstall" / "github"


def _github_cache_file(url: str) -> Path:
    return github_cache_dir() / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ".json")


def _read_github_cache(url: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_github_cache_file(url).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("url") == url and isinstance(data.get("etag"), str):
        return data
    return None


def _write_github_cache(url: str, etag: str, body: Any) -> None:
    try:
        target = _github_cache_file(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps({"url": url, "etag": etag, "body": body}))
    except (OSError, TypeError, ValueError) as exc:
        logging.debug("GitHub cache not written for %s: %s", url, exc)


def _fetch_json(url: str, timeout: int = 20) -> Any:
    """GET a GitHub API URL as JSON.

    Sends the cached ETag (a 304 answer costs no rate limit and returns the cached body),
    retries network errors and 5xx answers twice, uses DECKINSTALL_GITHUB_TOKEN when set, and
    falls back to the cached body when GitHub is unreachable or rate-limits us.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    cached = _read_github_cache(url)
    if cached is not None:
        headers["If-None-Match"] = cached["etag"]
    last_exc: Exception | None = None
    for attempt in range(_FETCH_RETRIES + 1):
        if attempt:
            time.sleep(min(4.0, 1.0 * 2 ** (attempt - 1)))
        try:
            with _request_github_with_validated_redirects(url, headers=headers, timeout=timeout) as resp:
                status = resp.status_code if isinstance(resp.status_code, int) else 200
                if status == 304 and cached is not None:
                    return cached.get("body")
                if status >= 500:
                    last_exc = requests.HTTPError(f"GitHub answered {status} for {url}")
                    continue
                if status in (403, 429) and cached is not None:
                    logging.warning("GitHub rate limit for %s; using the cached answer", url)
                    return cached.get("body")
                resp.raise_for_status()
                body = resp.json()
                etag = resp.headers.get("ETag") if resp.headers else None
                if isinstance(etag, str) and etag:
                    _write_github_cache(url, etag, body)
                return body
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
    if cached is not None:
        logging.warning("GitHub unreachable for %s (%s); using the cached answer", url, last_exc)
        return cached.get("body")
    assert last_exc is not None
    raise last_exc


def _download_file(
    url: str,
    dest: Path,
    label: str,
    progress_cb: ProgressCallback | None = None,
    timeout: int = 60,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _request_github_with_validated_redirects(
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


_MAX_RUNTIME_TAR_ENTRIES = 50_000
_MAX_RUNTIME_TAR_FILE_BYTES = 1024 * 1024 * 1024
_MAX_RUNTIME_TAR_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _safe_extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """Safely extract a tar/tar.gz archive preventing path traversal, bombs, and special files."""
    dest_resolved = dest_dir.resolve()
    total_bytes = 0
    members: list[tarfile.TarInfo] = []
    with tarfile.open(tar_path, "r:*") as tf:
        for idx, member in enumerate(tf):
            if idx >= _MAX_RUNTIME_TAR_ENTRIES:
                raise RuntimeError("Archive exceeds maximum allowed entry count.")
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
            elif member.isfile():
                if member.size > _MAX_RUNTIME_TAR_FILE_BYTES:
                    raise RuntimeError(f"Archive member exceeds maximum allowed size: {member.name}")
                total_bytes += max(0, int(member.size))
                if total_bytes > _MAX_RUNTIME_TAR_TOTAL_BYTES:
                    raise RuntimeError("Archive exceeds maximum allowed total uncompressed size.")
            member.mode &= 0o777
            members.append(member)
        if callable(getattr(tarfile, "data_filter", None)):
            tf.extractall(dest_dir, members=members, filter="data")
        else:
            for member in members:
                # Earlier archive entries may have introduced links since the
                # initial inspection. Re-resolve every destination and link
                # target immediately before extracting on older Python builds.
                target = (dest_resolved / member.name).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise RuntimeError(f"Archive member escapes destination: {member.name}")
                if member.issym() or member.islnk():
                    base = target.parent if member.issym() else dest_resolved
                    if not (base / member.linkname).resolve().is_relative_to(dest_resolved):
                        raise RuntimeError(f"Unsafe archive link target: {member.linkname}")
                if "filter" in inspect.signature(tf.extract).parameters:
                    tf.extract(member, dest_dir, filter="fully_trusted")
                else:
                    tf.extract(member, dest_dir)


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


def _verify_sha256(file_path: Path, checksum_text_or_hex: str) -> None:
    """Verify file_path against a raw 64-hex SHA-256 digest or a SHA256SUMS-formatted text."""
    text = checksum_text_or_hex.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        expected = text.lower()
    else:
        entries: list[tuple[str, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^([0-9a-fA-F]{64})(?:\s+[* ]?(.+))?$", line)
            if match:
                digest_hex = match.group(1).lower()
                entry_name = Path((match.group(2) or "").strip()).name
                entries.append((digest_hex, entry_name))
        if not entries:
            raise RuntimeError("Invalid SHA-256 checksum format.")
        expected_opt: str | None = None
        for digest_hex, entry_name in entries:
            if entry_name == file_path.name:
                expected_opt = digest_hex
                break
        if expected_opt is None:
            if len(entries) == 1:
                expected_opt = entries[0][0]
            else:
                raise RuntimeError(f"No SHA-256 entry matching {file_path.name!r}.")
        expected = expected_opt

    actual = file_sha256(file_path).lower()
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 checksum mismatch for {file_path.name}: expected {expected}, got {actual}"
        )


def _extract_github_asset_sha256(asset: dict[str, Any]) -> str | None:
    raw = str(asset.get("digest") or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("sha256:"):
        raw = raw.split(":", 1)[1].strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return raw.lower()
    return None


def _find_release_checksum_asset(
    release: dict[str, Any], asset_name: str
) -> tuple[dict[str, Any], str] | None:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return None
    candidates = [
        (f"{asset_name}.sha512sum", "sha512"),
        (f"{asset_name}.sha256sum", "sha256"),
        (f"{asset_name}.sha256", "sha256"),
        ("SHA512SUMS", "sha512"),
        ("SHA256SUMS", "sha256"),
    ]
    for target_name, algo in candidates:
        for asset in assets:
            if isinstance(asset, dict) and str(asset.get("name", "")) == target_name:
                if asset.get("browser_download_url"):
                    return asset, algo
    return None


def _verify_release_asset_integrity(
    release: dict[str, Any],
    asset: dict[str, Any],
    downloaded_path: Path,
    tmp_dir: Path,
    *,
    label: str,
    require_checksum: bool = False,
) -> None:
    """Verify downloaded asset against GitHub digest, checksum asset, and reported size.

    When ``require_checksum`` is False (default), missing checksum metadata logs a
    warning and falls back to size-only verification so upstream releases without
    published digests remain installable. When ``require_checksum`` is True, a
    missing digest/checksum asset raises ``RuntimeError``.
    """
    raw_size = asset.get("size")
    if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0:
        actual_size = downloaded_path.stat().st_size
        if actual_size != raw_size:
            raise RuntimeError(
                f"Downloaded {label} size mismatch: expected {raw_size} bytes, got {actual_size}."
            )

    sha256_digest = _extract_github_asset_sha256(asset)
    if sha256_digest:
        _verify_sha256(downloaded_path, sha256_digest)
        return

    found = _find_release_checksum_asset(release, downloaded_path.name)
    if found is not None:
        cs_asset, algo = found
        cs_url = str(cs_asset.get("browser_download_url", ""))
        cs_name = _sanitize_asset_name(str(cs_asset.get("name", f"{downloaded_path.name}.{algo}")))
        cs_path = tmp_dir / cs_name
        _download_file(cs_url, cs_path, cs_name)
        cs_text = cs_path.read_text(encoding="utf-8", errors="ignore")
        if algo == "sha512":
            _verify_sha512(downloaded_path, cs_text)
        else:
            _verify_sha256(downloaded_path, cs_text)
        return

    if require_checksum:
        raise RuntimeError(
            f"No checksum metadata published for {label} ({downloaded_path.name})."
        )
    logging.warning(
        "No checksum metadata published for %s (%s); verified size only.",
        label,
        downloaded_path.name,
    )


def find_system_umu_run() -> Path | None:
    """Return a system-installed umu-run binary outside DeckInstall's managed runtime dir, or None."""
    found = shutil.which("umu-run")
    if not found:
        return None
    candidate = Path(found)
    try:
        if candidate.resolve().is_relative_to(get_umu_dir().resolve()):
            return None
    except OSError:
        pass
    return candidate if candidate.is_file() else None


@_serialize_runtime("umu")
def ensure_umu_run(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Ensure portable umu-run is installed and executable, preferring system umu-run when present."""
    if not force_update:
        sys_umu = find_system_umu_run()
        if sys_umu is not None:
            return sys_umu
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
    with tempfile.TemporaryDirectory(prefix=_runtime_temp_prefix("umu"), dir=str(get_runtime_dir())) as tmp_dir:
        tmp_path = Path(tmp_dir)
        tar_path = tmp_path / asset_name
        _download_file(download_url, tar_path, asset_name, progress_cb=progress_cb)
        _verify_release_asset_integrity(
            release, zipapp_asset, tar_path, tmp_path, label="umu-run", require_checksum=True,
        )

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

    if tag:
        def _set_umu(versions: dict[str, Any]) -> None:
            versions["umu_installed_version"] = tag
            versions["umu_latest_version"] = tag

        _update_versions(_set_umu)
    _emit_progress(progress_cb, "umu-run ready.", 100)
    return umu_run


def optiscaler_downloaded() -> bool:
    """True when DeckInstall's OptiScaler package is on disk (games use copies of it)."""
    return _has_optiscaler_dll(get_optiscaler_dir())


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


@contextlib.contextmanager
def _publish_runtime_directory(staged: Path, target: Path) -> Iterator[None]:
    """Publish a complete runtime and preserve the previous directory until commit."""
    backup: Path | None = None
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"Refusing to replace a non-directory runtime: {target}")
    if target.is_dir():
        backup = Path(tempfile.mkdtemp(prefix=f".{target.name}_old_", dir=target.parent))
        backup.rmdir()
        os.replace(target, backup)
    published = False
    try:
        os.replace(staged, target)
        published = True
        yield
    except BaseException:
        try:
            if published:
                os.replace(target, staged)
            if backup is not None:
                os.replace(backup, target)
        except OSError:
            logging.exception("Runtime rollback failed for %s. Previous runtime: %s", target, backup)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _swap_directory_atomic(src_dir: Path, target_dir: Path) -> None:
    """Copy src_dir into a staging sibling and atomically swap it into target_dir."""
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target_dir.name}_stage_", dir=parent) as tmp:
        staged = Path(tmp) / "payload"
        shutil.copytree(src_dir, staged, symlinks=False)
        with _publish_runtime_directory(staged, target_dir):
            pass


@_serialize_runtime("optiscaler")
def ensure_optiscaler(
    force_update: bool = False,
    progress_cb: ProgressCb | None = None,
    *,
    check_update: bool = False,
) -> Path:
    """Ensure OptiScaler payload is cached under runtime/optiscaler/current."""
    opti_dir = get_optiscaler_dir()
    opti_dir.parent.mkdir(parents=True, exist_ok=True)
    return _ensure_optiscaler_payload(opti_dir, force_update, progress_cb, check_update=check_update)


def _ensure_optiscaler_payload(
    opti_dir: Path,
    force_update: bool,
    progress_cb: ProgressCb | None,
    *,
    check_update: bool = False,
) -> Path:
    """Check and populate the cache while its install lock is held."""
    has_cached = _has_optiscaler_dll(opti_dir)
    if not force_update and not check_update and has_cached:
        version = str(_load_versions().get("optiscaler_installed_version") or "")
        logging.info("Using cached OptiScaler %s from %s; no download", version, opti_dir)
        _emit_progress(progress_cb, f"Using cached OptiScaler {version} (no download).".strip(), 100)
        return opti_dir

    from .extractor import extract_archive_to_dir

    _emit_progress(progress_cb, "Checking latest OptiScaler release...", 0)
    if not force_update and check_update and has_cached:
        installed_ver = str(_load_versions().get("optiscaler_installed_version") or "").strip()
        try:
            release = _fetch_json(OPTISCALER_RELEASES_API)
        except Exception as exc:
            logging.warning(
                "Failed checking OptiScaler updates (%s); using cached %s.",
                exc,
                installed_ver or "payload",
            )
            _emit_progress(
                progress_cb,
                f"Using cached OptiScaler {installed_ver} (offline fallback).".strip(),
                100,
            )
            return opti_dir
    else:
        release = _fetch_json(OPTISCALER_RELEASES_API)

    if not isinstance(release, dict):
        raise RuntimeError("Unexpected GitHub release response for OptiScaler.")

    tag = str(release.get("tag_name", "")).strip()
    if not force_update and check_update and has_cached:
        installed_ver = str(_load_versions().get("optiscaler_installed_version") or "").strip()
        if tag and installed_ver and tag == installed_ver:
            def _touch_opti(versions: dict[str, Any]) -> None:
                versions["optiscaler_latest_version"] = tag
                versions["optiscaler_last_check"] = int(time.time())

            _update_versions(_touch_opti)
            _emit_progress(progress_cb, f"Using cached OptiScaler {tag} (up to date).", 100)
            return opti_dir
    asset = _pick_optiscaler_asset(release)
    download_url = str(asset.get("browser_download_url", "")).strip()
    asset_name = _sanitize_asset_name(str(asset.get("name", "OptiScaler.zip")))
    logging.info("Downloading OptiScaler %s: %s", tag, asset_name)

    opti_parent = opti_dir.parent
    opti_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=_runtime_temp_prefix("optiscaler"), dir=str(opti_parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / asset_name
        _download_file(download_url, archive_path, asset_name, progress_cb=progress_cb)
        _verify_release_asset_integrity(release, asset, archive_path, tmp_path, label="OptiScaler")

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

    def _set_opti(versions: dict[str, Any]) -> None:
        if tag:
            versions["optiscaler_installed_version"] = tag
            versions["optiscaler_latest_version"] = tag
        versions["optiscaler_last_check"] = int(time.time())

    _update_versions(_set_opti)
    _emit_progress(progress_cb, "OptiScaler ready.", 100)
    return opti_dir


def _is_nonempty_file(path: Path) -> bool:
    """Return True if path exists as a regular (or symlink-resolved) non-empty file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def validate_ge_proton_integrity(proton_dir: Path) -> ProtonIntegrityReport:
    """Validate an installed GE-Proton directory for required files and broken symlinks."""
    if proton_dir.is_symlink():
        return ProtonIntegrityReport(
            proton_dir=proton_dir,
            valid=False,
            missing_files=(),
            broken_symlinks=(),
            error="Proton directory is a symlink.",
        )
    if not proton_dir.is_dir():
        return ProtonIntegrityReport(
            proton_dir=proton_dir,
            valid=False,
            missing_files=(),
            broken_symlinks=(),
            error="Proton directory does not exist.",
        )

    missing: list[str] = []
    for rel in ("proton", "compatibilitytool.vdf", "toolmanifest.vdf"):
        if not _is_nonempty_file(proton_dir / rel):
            missing.append(rel)

    wine_candidates = ("files/bin/wine", "dist/bin/wine")
    if not any(_is_nonempty_file(proton_dir / rel) for rel in wine_candidates):
        if (proton_dir / "dist").is_dir() and not (proton_dir / "files").is_dir():
            missing.append("dist/bin/wine")
        else:
            missing.append("files/bin/wine")

    broken_symlinks: list[str] = []
    for bin_rel in ("files/bin", "dist/bin"):
        bin_dir = proton_dir / bin_rel
        if not bin_dir.is_dir():
            continue
        try:
            entries = sorted(bin_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() and not entry.exists():
                broken_symlinks.append(f"{bin_rel}/{entry.name}")

    valid = not missing and not broken_symlinks
    errors: list[str] = []
    if missing:
        errors.append(f"Missing or empty required files: {', '.join(missing)}")
    if broken_symlinks:
        errors.append(f"Broken symlinks: {', '.join(broken_symlinks)}")
    error = "; ".join(errors) if errors else None
    return ProtonIntegrityReport(
        proton_dir=proton_dir,
        valid=valid,
        missing_files=tuple(missing),
        broken_symlinks=tuple(broken_symlinks),
        error=error,
    )


@_serialize_runtime("compat-tools")
def repair_ge_proton(
    proton_dir: Path,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Replace a managed GE-Proton after its replacement has been downloaded and validated."""
    if not is_managed_compat_tool(proton_dir):
        raise ValueError(f"Refusing to repair a compatibility tool installed outside DeckInstall: {proton_dir}")
    _emit_progress(progress_cb, f"Replacing broken GE-Proton directory {proton_dir.name}...", 0)
    tag = proton_dir.name
    if _parse_ge_proton_version(tag, min_major=1) is not None:
        try:
            releases = fetch_available_proton_releases("ge-proton", limit=50)
            for rel in releases:
                if rel.tag_name == tag:
                    return install_compat_tool(rel, progress_cb=progress_cb)
        except Exception:
            logging.warning("Could not reinstall %s; trying the latest GE-Proton", tag, exc_info=True)
    return ensure_ge_proton11(force_update=True, progress_cb=progress_cb)


@_serialize_runtime("compat-tools")
def ensure_ge_proton11(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
    verify_integrity: bool = True,
) -> Path:
    """Ensure a GE-Proton11+ directory is installed in Steam compatibilitytools.d."""
    existing = list_ge_proton11_dirs()
    if existing and not force_update:
        if not verify_integrity:
            return existing[0]
        first_report: ProtonIntegrityReport | None = None
        for candidate in existing:
            report = validate_ge_proton_integrity(candidate)
            if first_report is None:
                first_report = report
            if report.valid:
                return candidate
        managed = next((candidate for candidate in existing if is_managed_compat_tool(candidate)), None)
        if managed is not None:
            logging.warning("Repairing broken DeckInstall GE-Proton: %s (%s)", managed, first_report)
            return repair_ge_proton(managed, progress_cb=progress_cb)
        logging.warning("GE-Proton integrity check failed; preserving manually installed copies.")

    if not force_update:
        cachy_dirs = list_cachyos_proton_dirs()
        if cachy_dirs:
            return cachy_dirs[0]

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
    if (target_dir.exists() or target_dir.is_symlink()) and not is_managed_compat_tool(target_dir):
        if not target_dir.is_symlink() and validate_ge_proton_integrity(target_dir).valid:
            return target_dir
        raise ValueError(f"Manually installed GE-Proton occupies {target_dir}; it was preserved.")

    with tempfile.TemporaryDirectory(prefix=_runtime_temp_prefix("ge"), dir=str(compat_dir)) as tmp_dir:
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

        report = validate_ge_proton_integrity(extracted_root)
        if not report.valid:
            raise RuntimeError(f"Invalid GE-Proton package {tag}: {report.error}")
        atomic_write_text(extracted_root / _GE_MANAGED_MARKER, f"{tag}\n")
        if (target_dir.exists() or target_dir.is_symlink()) and not is_managed_compat_tool(target_dir):
            raise ValueError(f"Refusing to overwrite a compatibility tool installed outside DeckInstall: {target_dir}")
        with _publish_runtime_directory(extracted_root, target_dir):
            pass


    def _set_ge(versions: dict[str, Any]) -> None:
        versions["ge_proton_latest_version"] = tag

    _update_versions(_set_ge)
    _emit_progress(progress_cb, f"{tag} ready.", 100)
    return target_dir




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
                sha256_digest=_extract_github_asset_sha256(tar_asset),
                size_bytes=size_bytes,
                published_at=str(rel.get("published_at") or "").strip(),
                prerelease=bool(rel.get("prerelease", False)),
            )
        )
    # GitHub lists by publish date, which puts GE-Proton11-9 above GE-Proton11-12: order by version.
    results.sort(
        key=lambda r: (tuple(int(n) for n in re.findall(r"\d+", r.tag_name)), r.published_at),
        reverse=True,
    )
    return results


def list_installed_compat_tools() -> list[InstalledCompatTool]:
    """List all custom compatibility tools with compatibilitytool.vdf installed."""
    from .steam_paths import _read_compat_tool_id

    candidate_dirs = _all_compat_tool_dirs()

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


@_serialize_runtime("compat-tools")
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
        prefix=_runtime_temp_prefix("proton"), dir=str(compat_dir)
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

        if release.size_bytes > 0:
            actual_size = tar_path.stat().st_size
            if actual_size != release.size_bytes:
                raise RuntimeError(
                    f"Downloaded {tar_name} size mismatch: expected {release.size_bytes} bytes, got {actual_size}."
                )

        if sha_path is not None:
            _emit_progress(progress_cb, f"Verifying SHA-512 for {tar_name}...", 92)
            _verify_sha512(
                tar_path, sha_path.read_text(encoding="utf-8", errors="ignore")
            )
        elif release.sha256_digest:
            _emit_progress(progress_cb, f"Verifying SHA-256 for {tar_name}...", 92)
            _verify_sha256(tar_path, release.sha256_digest)
        else:
            logging.warning(
                "No checksum_url or sha256_digest published for compatibility tool %s (%s); verified size only.",
                release.tag_name,
                tar_name,
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
        if not all((extracted_root / name).is_file() for name in ("proton", "compatibilitytool.vdf")):
            raise RuntimeError(f"Compatibility tool package is incomplete: {folder_name}")
        if release.family_id == "ge-proton":
            report = validate_ge_proton_integrity(extracted_root)
            if not report.valid:
                raise RuntimeError(f"Invalid GE-Proton package {folder_name}: {report.error}")
        atomic_write_text(extracted_root / _GE_MANAGED_MARKER, f"{folder_name}\n")
        if (target_dir.exists() or target_dir.is_symlink()) and not is_managed_compat_tool(target_dir):
            raise ValueError(f"Refusing to overwrite a compatibility tool installed outside DeckInstall: {target_dir}")
        with _publish_runtime_directory(extracted_root, target_dir):
            pass


    _emit_progress(progress_cb, f"{folder_name} installed.", 100)
    return target_dir


_SYSTEM_COMPAT_TOOL_DIRS = (
    Path("/usr/share/steam/compatibilitytools.d"),
    Path("/usr/local/share/steam/compatibilitytools.d"),
)


def is_system_compat_tool(tool_path: Path) -> bool:
    """True when tool_path lives inside a system-wide compatibilitytools.d directory."""
    try:
        parent_res = tool_path.parent.resolve()
        return parent_res in {d.resolve() for d in _SYSTEM_COMPAT_TOOL_DIRS if d.exists()}
    except OSError:
        return False


@_serialize_runtime("compat-tools")
def remove_compat_tool(tool_path: Path, *, managed_only: bool = False) -> None:
    """Safely remove a custom compatibility tool directory from compatibilitytools.d."""
    if tool_path.is_symlink():
        raise ValueError(f"Refusing to remove symlink: {tool_path}")
    if not tool_path.exists() or not tool_path.is_dir():
        raise FileNotFoundError(f"Compatibility tool directory not found: {tool_path}")
    if is_system_compat_tool(tool_path):
        raise ValueError(f"Refusing to remove system-managed compatibility tool: {tool_path}")
    parent = tool_path.parent
    if parent.resolve() not in {path.resolve() for path in _all_compat_tool_dirs()}:
        raise ValueError(
            f"Refusing to remove path outside compatibilitytools.d: {tool_path}"
        )
    if tool_path.resolve().parent != parent.resolve():
        raise ValueError(
            f"Refusing to remove non-direct child of compatibilitytools.d: {tool_path}"
        )
    if managed_only and not is_managed_compat_tool(tool_path):
        raise ValueError(f"Runtime is not managed by DeckInstall: {tool_path}")
    shutil.rmtree(tool_path)


def check_runtime_updates(force: bool = False) -> dict[str, Any]:
    """Check GitHub for newer umu-launcher and GE-Proton11+ releases (24h cache)."""
    versions = _load_versions()
    now = time.time()
    last_check = float(versions.get("last_check", 0) or 0)
    umu_path = get_umu_run_path()
    sys_umu = find_system_umu_run()
    # Same precedence as every launcher: the system umu-run wins over the managed copy.
    use_managed_umu = sys_umu is None and umu_path.is_file()
    installed_umu = (
        versions.get("umu_installed_version")
        if use_managed_umu
        else (f"system ({sys_umu.name})" if sys_umu is not None else None)
    )
    ge_dirs = list_ge_proton11_dirs()
    cachy_dirs = [] if ge_dirs else list_cachyos_proton_dirs()
    installed_ge = ge_dirs[0].name if ge_dirs else (cachy_dirs[0].name if cachy_dirs else None)

    if not force and last_check > 0 and (now - last_check) < UPDATE_CACHE_TTL_SECONDS:
        latest_umu = versions.get("umu_latest_version")
        latest_ge = versions.get("ge_proton_latest_version")
        return {
            "checked_at": last_check,
            "from_cache": True,
            "umu": {
                "installed": installed_umu,
                "latest": latest_umu,
                "update_available": bool(use_managed_umu and latest_umu and installed_umu != latest_umu),
                "installed_path": str(sys_umu) if sys_umu else (str(umu_path) if umu_path.is_file() else None),
            },
            "ge_proton": {
                "installed": installed_ge,
                "latest": latest_ge,
                "update_available": bool(ge_dirs and latest_ge and installed_ge != latest_ge),
                "installed_path": str(ge_dirs[0]) if ge_dirs else (str(cachy_dirs[0]) if cachy_dirs else None),
            },
        }

    umu_release = _fetch_json(UMU_RELEASES_LATEST_URL)
    latest_umu = str(umu_release.get("tag_name", "")).strip() or None
    ge_release = _fetch_latest_ge_proton11_release()
    latest_ge = str(ge_release.get("tag_name", "")).strip() or None

    def _set_checked(v: dict[str, Any]) -> None:
        v["last_check"] = now
        if latest_umu:
            v["umu_latest_version"] = latest_umu
        if latest_ge:
            v["ge_proton_latest_version"] = latest_ge

    _update_versions(_set_checked)

    return {
        "checked_at": now,
        "from_cache": False,
        "umu": {
            "installed": installed_umu,
            "latest": latest_umu,
            "update_available": bool(use_managed_umu and latest_umu and installed_umu != latest_umu),
            "installed_path": str(sys_umu) if sys_umu else (str(umu_path) if umu_path.is_file() else None),
        },
        "ge_proton": {
            "installed": installed_ge,
            "latest": latest_ge,
            "update_available": bool(ge_dirs and latest_ge and installed_ge != latest_ge),
            "installed_path": str(ge_dirs[0]) if ge_dirs else (str(cachy_dirs[0]) if cachy_dirs else None),
        },
    }


def is_steamrt4_valid() -> bool:
    """Read-only check whether ~/.local/share/umu/steamrt4 holds the expected runtime files."""
    steamrt4_dir = get_steamrt4_dir()
    if steamrt4_dir.is_symlink():
        return False
    if not steamrt4_dir.is_dir():
        return True
    manifest = steamrt4_dir / "toolmanifest.vdf"
    mtree = steamrt4_dir / "mtree.txt.gz"
    return manifest.is_file() and not manifest.is_symlink() and mtree.is_file() and not mtree.is_symlink()


def validate_steamrt4_integrity() -> bool:
    """Remove broken/partial ~/.local/share/umu/steamrt4 directory if required files are missing."""
    steamrt4_dir = get_steamrt4_dir()
    if steamrt4_dir.is_symlink():
        return False
    if not steamrt4_dir.is_dir():
        return True
    if not is_steamrt4_valid():
        shutil.rmtree(steamrt4_dir, ignore_errors=True)
        return False
    return True


def check_ubuntu_bwrap_userns() -> str | None:
    """Check if AppArmor unprivileged userns restriction blocks bwrap."""
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
    except subprocess.TimeoutExpired:
        logging.warning("Bubblewrap user namespace check timed out")
        return None
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
    "winetricks": {
        "apt-get": ["winetricks"],
        "dnf": ["winetricks"],
        "pacman": ["winetricks"],
        "zypper": ["winetricks"],
    },
    "innoextract": {
        "apt-get": ["innoextract"],
        "dnf": ["innoextract"],
        "pacman": ["innoextract"],
        "zypper": ["innoextract"],
    },
}


def get_system_package_install_cmd(bin_name: str) -> list[str] | None:
    """Return an OS package-manager install command for bin_name, or None on immutable OSes/unsupported."""
    from .modes import is_immutable_os

    if is_immutable_os():
        return None
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
    try:
        proc = subprocess.run(
            [pkexec, *cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return (False, f"Timed out installing {bin_name}.")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return (False, err)
    _record_package_install(cmd)
    return (True, f"{bin_name} installed.")


_PACKAGE_REMOVE = {"apt-get": "apt remove", "dnf": "dnf remove", "pacman": "pacman -Rs", "zypper": "zypper remove"}


def _record_package_install(install_cmd: list[str]) -> None:
    """Note the packages in system-changes.json with the command that removes them."""
    from .system_changes import KIND_PACKAGE, record_change

    manager = install_cmd[0]
    pkgs = [a for a in install_cmd[1:] if not a.startswith("-") and a not in ("install", "-S")]
    remove = _PACKAGE_REMOVE.get(manager)
    if not pkgs or remove is None:
        return
    try:
        record_change(KIND_PACKAGE, " ".join(pkgs), f"sudo {remove} {' '.join(pkgs)}", needs_root=True,
                      detail=f"installed with {manager}")
    except OSError as exc:
        logging.warning("Could not record the package install: %s", exc)


def fix_bwrap_userns(confirmed: bool = False) -> tuple[bool, str]:
    """Install a persistent AppArmor userns profile for bwrap via pkexec."""
    if not confirmed:
        return (False, "User confirmation required to modify AppArmor bwrap profile.")
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return (False, f"sudo apparmor_parser -r {BWRAP_PROFILE}")
    bwrap_path = _bwrap_binary_path()
    if not re.fullmatch(r"/[\w./+-]+", bwrap_path):
        return (False, f"Unexpected bwrap path: {bwrap_path!r}")
    script = (
        "set -e; "
        "if [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null 2>&1; then "
        # Never replace a profile someone else wrote (ours carries the marker or the old exact line).
        f"if [ -e {BWRAP_PROFILE} ] && ! grep -q -e '{_BWRAP_MARKER}' "
        f"-e 'profile bwrap /usr/bin/bwrap flags=(unconfined)' {BWRAP_PROFILE}; then "
        f"echo '{BWRAP_PROFILE} exists and was not written by DeckInstall.' >&2; exit 3; fi; "
        f"cat <<'EOF' >{BWRAP_PROFILE}\n"
        f"{_BWRAP_MARKER}\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        f"profile bwrap {bwrap_path} flags=(unconfined) {{\n"
        "  userns,\n"
        "  include if exists <local/bwrap>\n"
        "}\n"
        "EOF\n"
        f"apparmor_parser -r {BWRAP_PROFILE}; "
        "else "
        "echo 'AppArmor profile directory or apparmor_parser not found.' >&2; exit 1; "
        "fi"
    )
    try:
        proc = subprocess.run(
            [pkexec, "sh", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return (False, "Timed out applying AppArmor bwrap profile.")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return (False, err)
    try:
        from .system_changes import KIND_APPARMOR, record_change

        record_change(
            KIND_APPARMOR,
            BWRAP_PROFILE,
            f"sudo apparmor_parser -R {BWRAP_PROFILE} && sudo rm {BWRAP_PROFILE}",
            needs_root=True,
            detail=f"lets {bwrap_path} create user namespaces (umu-run)",
        )
    except OSError as exc:
        logging.warning("Could not record the AppArmor change: %s", exc)
    return (True, "OK")


BWRAP_PROFILE = "/etc/apparmor.d/bwrap"
_BWRAP_MARKER = "# Written by DeckInstall"


def _bwrap_binary_path() -> str:
    """The real bwrap binary (AppArmor matches the resolved path), /usr/bin/bwrap by default."""
    found = shutil.which("bwrap")
    if not found:
        return "/usr/bin/bwrap"
    try:
        return str(Path(found).resolve())
    except OSError:
        return found


def kill_proc_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the subprocess group when start_new_session=True, falling back to proc."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def _emit_buffered_lines(buf: str, on_line: Callable[[str], None]) -> str:
    """Emit complete newline/CR-terminated lines from buf and return the remainder."""
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
    return buf


def drain_stream_lines(
    proc: subprocess.Popen[bytes],
    cancel_flag: Callable[[], bool] | None,
    on_line: Callable[[str], None],
    cancel_msg: str,
) -> int:
    """Read subprocess stdout non-blockingly so cancel_flag is checked every <=0.2s."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""

    def check_cancelled() -> None:
        if cancel_flag and cancel_flag():
            raise RuntimeError(cancel_msg)

    try:
        os.set_blocking(fd, False)
        while True:
            check_cancelled()
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    chunk = None
                if chunk is not None:
                    if not chunk:
                        buf += decoder.decode(b"", final=True)
                        if buf.strip():
                            on_line(buf)
                        break
                    buf += decoder.decode(chunk, final=False)
                    buf = _emit_buffered_lines(buf, on_line)
            elif proc.poll() is not None:
                while True:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    buf += decoder.decode(chunk, final=False)
                    buf = _emit_buffered_lines(buf, on_line)
                buf += decoder.decode(b"", final=True)
                if buf.strip():
                    on_line(buf)
                break
        # A tool can close stdout long before it exits.
        while True:
            check_cancelled()
            try:
                return proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if proc.poll() is None:
            try:
                kill_proc_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill_proc_group(proc, signal.SIGKILL)
                    proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                logging.warning("Could not stop process group for PID %s", proc.pid, exc_info=True)
        raise
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _resolve_proton_name_in_compat_dirs(
    proton_name: str, steam_root: Path | None = None
) -> Path | None:
    """Resolve a Proton/compat tool name or path across all compatibility tool directories."""
    from .steam_paths import resolve_compat_tool_dir

    cleaned = proton_name.strip()
    if not cleaned:
        return None
    as_path = Path(cleaned)
    if as_path.is_dir() and (as_path / "proton").is_file():
        return as_path
    return resolve_compat_tool_dir(cleaned, _all_compat_tool_dirs(steam_root))


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
        from .steam_paths import find_proton_binary, resolve_builtin_proton
        from .steam_vdf import get_shortcut_proton

        proton_dir: Path | None = None
        proton_name = get_shortcut_proton(paths.config_vdf, appid)
        if proton_name:
            proton_dir = _resolve_proton_name_in_compat_dirs(proton_name, paths.steam_root)
            if proton_dir is None:
                proton_dir = resolve_builtin_proton(proton_name, paths.steam_root)
        if proton_dir is None:
            proton_bin = find_proton_binary(paths.steam_root)
            proton_dir = proton_bin.parent if proton_bin is not None else None
        return (prefix_dir, proton_dir)

    if kind_norm == "standalone":
        raw_prefix = getattr(obj, "prefix_dir", None)
        if raw_prefix is None:
            raise ValueError("Standalone game object has no prefix_dir attribute.")
        prefix_dir = Path(raw_prefix)
        inner = prefix_dir / "pfx"
        if not inner.is_symlink() and inner.is_dir() and not (prefix_dir / "drive_c").is_dir():
            prefix_dir = inner

        proton_dir = None
        proton_name = str(getattr(obj, "proton_name", "") or "").strip()
        if proton_name:
            proton_dir = _resolve_proton_name_in_compat_dirs(proton_name)
        if proton_dir is None:
            ge_dirs = list_ge_proton11_dirs() or list_cachyos_proton_dirs() or list_ge_proton10_dirs()
            if ge_dirs:
                proton_dir = ge_dirs[0]
        return (prefix_dir, proton_dir)

    if kind_norm == "external":
        raw_prefix = getattr(obj, "prefix_dir", None)
        if raw_prefix is None:
            raise ValueError("External game object has no prefix_dir attribute.")
        prefix_dir = Path(raw_prefix)
        inner = prefix_dir / "pfx"
        if not inner.is_symlink() and inner.is_dir() and not (prefix_dir / "drive_c").is_dir():
            prefix_dir = inner
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
    game_name: str = "",
    snapshot_before: bool = True,
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

    if snapshot_before and prefix_dir.is_dir():
        try:
            from .save_sync import snapshot_prefix_state

            label = game_name.strip() or prefix_dir.name
            snapshot_path = snapshot_prefix_state(prefix_dir, label)
            if on_log and snapshot_path is not None:
                on_log(f"Created prefix snapshot before winetricks: {snapshot_path}")
        except (ImportError, AttributeError):
            pass
        except Exception as exc:
            logging.warning("Prefix snapshot before winetricks failed: %s", exc)
            if on_log:
                on_log(f"Warning: prefix snapshot failed ({exc}); continuing winetricks.")

    prefix_dir.mkdir(parents=True, exist_ok=True)
    env = clean_child_env()
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
    env = clean_child_env()
    env["WINEPREFIX"] = str(prefix_dir)
    env["PROTONPATH"] = str(proton_dir)
    env["GAMEID"] = game_id or "umu-default"
    env["STORE"] = store or "none"
    if extra_env:
        env.update(extra_env)
    return env


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
    allow_download: bool = True,
) -> int:
    """Run an arbitrary Windows executable inside an existing Wine prefix via umu-run."""
    resolved_proton = proton_path
    if resolved_proton is None:
        candidates = list_ge_proton11_dirs() or list_cachyos_proton_dirs() or list_ge_proton10_dirs()
        if candidates:
            resolved_proton = candidates[0]
        else:
            from .steam_paths import find_proton_binary, find_steam_root

            steam_root = find_steam_root()
            steam_proton = find_proton_binary(steam_root) if steam_root is not None else None
            if steam_proton is not None:
                resolved_proton = steam_proton.parent
            else:
                raise RuntimeError(
                    "No GE-Proton11+/10 or official Steam Proton installation found. Install Proton first."
                )

    if allow_download:
        umu_run = ensure_umu_run()
    else:
        umu_run = find_system_umu_run() or get_umu_run_path()
        if not umu_run.is_file():
            raise RuntimeError("umu-run is not installed.")
    prefix_dir.mkdir(parents=True, exist_ok=True)
    env = build_umu_env(prefix_dir, resolved_proton, game_id=game_id)
    builtin = str(exe_path) in WINE_TOOLS
    cmd = [str(umu_run), str(exe_path)]
    # A Wine tool runs from the prefix, so a same-named file in the app's folder is never picked.
    cwd = str(prefix_dir) if builtin else (str(exe_path.parent) if exe_path.parent.is_dir() else None)

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


@dataclass(frozen=True)
class _UmuDbIndex:
    """Structure-of-Arrays in-memory index for umu-database.csv."""

    titles: tuple[str, ...]
    norm_titles: tuple[str, ...]
    stores: tuple[str, ...]
    codenames: tuple[str, ...]
    umu_ids: tuple[str, ...]
    common_redists: tuple[str, ...]
    notes: tuple[str, ...]
    by_norm_title: dict[str, tuple[int, ...]]
    by_codename: dict[str, tuple[int, ...]]
    by_umu_id: dict[str, tuple[int, ...]]


_UMU_DB_CACHE_LOCK = threading.Lock()
_UMU_DB_CACHE: tuple[tuple[Path, FileIdentity], _UmuDbIndex] | None = None
_GENERIC_EXE_PARENTS = frozenset(
    {"", "bin", "binaries", "game", "win32", "win64", "x64", "x86"}
)


def _normalize_umu_title(text: str) -> str:
    """Normalize a game title or executable stem for umu-database lookup."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _validate_umu_csv_header(csv_path: Path) -> None:
    """Verify csv_path has a valid umu-database.csv header."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
    if not header:
        raise RuntimeError("Downloaded umu-database.csv is empty.")
    cols = {col.strip().upper() for col in header if col.strip()}
    if "TITLE" not in cols or "UMU_ID" not in cols:
        raise RuntimeError(f"Invalid umu-database.csv header: {header!r}")


def _build_umu_db_index(fh: IO[str]) -> _UmuDbIndex:
    """Parse umu-database.csv into a Structure-of-Arrays index."""
    titles: list[str] = []
    norm_titles: list[str] = []
    stores: list[str] = []
    codenames: list[str] = []
    umu_ids: list[str] = []
    common_redists: list[str] = []
    notes: list[str] = []
    by_norm_title: dict[str, list[int]] = {}
    by_codename: dict[str, list[int]] = {}
    by_umu_id: dict[str, list[int]] = {}

    reader = csv.reader(fh)
    raw_header = next(reader, None)
    if not raw_header:
        raise RuntimeError("Empty umu-database.csv.")
    col_map = {col.strip().upper(): idx for idx, col in enumerate(raw_header)}
    if "TITLE" not in col_map or "UMU_ID" not in col_map:
        raise RuntimeError(f"Invalid umu-database.csv header: {raw_header!r}")

    title_i = col_map["TITLE"]
    store_i = col_map.get("STORE", -1)
    codename_i = col_map.get("CODENAME", -1)
    umu_id_i = col_map["UMU_ID"]
    redist_i = col_map.get("COMMON_REDIST", col_map.get("COMMONREDIST", -1))
    note_i = col_map.get("NOTE", -1)

    def _cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if 0 <= idx < len(row) else ""

    for row in reader:
        if not row:
            continue
        title = _cell(row, title_i)
        umu_id = _cell(row, umu_id_i)
        if not title and not umu_id:
            continue
        store = _cell(row, store_i)
        codename = _cell(row, codename_i)
        common_redist = _cell(row, redist_i)
        note = _cell(row, note_i)
        norm = _normalize_umu_title(title)

        row_idx = len(titles)
        titles.append(title)
        norm_titles.append(norm)
        stores.append(store)
        codenames.append(codename)
        umu_ids.append(umu_id)
        common_redists.append(common_redist)
        notes.append(note)

        if norm:
            by_norm_title.setdefault(norm, []).append(row_idx)
        if codename:
            by_codename.setdefault(codename.lower(), []).append(row_idx)
        if umu_id:
            by_umu_id.setdefault(umu_id.lower(), []).append(row_idx)

    return _UmuDbIndex(
        titles=tuple(titles),
        norm_titles=tuple(norm_titles),
        stores=tuple(stores),
        codenames=tuple(codenames),
        umu_ids=tuple(umu_ids),
        common_redists=tuple(common_redists),
        notes=tuple(notes),
        by_norm_title={k: tuple(v) for k, v in by_norm_title.items()},
        by_codename={k: tuple(v) for k, v in by_codename.items()},
        by_umu_id={k: tuple(v) for k, v in by_umu_id.items()},
    )


def _get_umu_db_index(db_path: Path) -> _UmuDbIndex | None:
    """Return cached SoA index for db_path, rebuilding if file mtime/size changed."""
    global _UMU_DB_CACHE
    with _UMU_DB_CACHE_LOCK:
        try:
            st = db_path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                return None
            cache_key = (db_path.absolute(), file_identity(st))
            if _UMU_DB_CACHE is not None and _UMU_DB_CACHE[0] == cache_key:
                return _UMU_DB_CACHE[1]
            with os.fdopen(open_regular_fd(db_path), "r", encoding="utf-8-sig", newline="") as source:
                if file_identity(os.fstat(source.fileno())) != cache_key[1]:
                    return None
                index = _build_umu_db_index(source)
                if file_identity(os.fstat(source.fileno())) != cache_key[1]:
                    return None
        except (OSError, ValueError, RuntimeError, csv.Error):
            return None
        _UMU_DB_CACHE = (cache_key, index)
        return index


def _ensure_umu_database_impl(
    force_update: bool = False,
    progress_cb: ProgressCallback | None = None,
    timeout: float = 20.0,
) -> Path | None:
    """Download umu-database.csv when missing or older than a week; keep the cached copy on failure."""
    db_path = get_umu_database_path()
    has_cached = db_path.is_file() and db_path.stat().st_size > 0
    if not force_update and has_cached:
        try:
            last_check = int(_load_versions().get("umu_db_last_check", 0) or 0)
        except (TypeError, ValueError):
            last_check = 0
        if (time.time() - last_check) < UMU_DATABASE_TTL_SECONDS:
            return db_path

    tmp_path: Path | None = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _emit_progress(progress_cb, "Downloading umu-database.csv...", 0)
        with _request_with_validated_redirects(
            UMU_DATABASE_URL,
            allowed_hosts=_ALLOWED_UMU_DB_HOSTS,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            stream=True,
            label="umu-database.csv",
        ) as resp:
            resp.raise_for_status()
            fd, tmp_name = tempfile.mkstemp(prefix=".umu-database_", suffix=".csv", dir=str(db_path.parent))
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
        _validate_umu_csv_header(tmp_path)
        os.replace(tmp_path, db_path)
        tmp_path = None

        def _set_umu_db(versions: dict[str, Any]) -> None:
            versions["umu_db_last_check"] = int(time.time())

        _update_versions(_set_umu_db)
        _emit_progress(progress_cb, "umu-database.csv ready.", 100)
        return db_path
    except Exception as exc:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if has_cached:
            logging.warning("Failed to update umu-database.csv (%s); using the cached copy.", exc)
            return db_path
        logging.warning("Failed to fetch umu-database.csv and no local cache exists: %s", exc)
        return None


ensure_umu_database = _ensure_umu_database_impl


def lookup_umu_codename(codename: str, store: str, db_path: Path | None = None) -> UmuDbLookupResult | None:
    """Look up a game by its store id (e.g. the GOG product id) in umu-database.csv."""
    cleaned = codename.strip().lower()
    if not cleaned:
        return None
    index = _get_umu_db_index(db_path if db_path is not None else get_umu_database_path())
    if index is None:
        return None
    rows = index.by_codename.get(cleaned)
    if not rows:
        return None
    store_low = store.strip().lower()
    matching = tuple(i for i in rows if not store_low or index.stores[i].lower() == store_low)
    if not matching:
        return None
    idx = matching[0]
    return UmuDbLookupResult(
        umu_id=index.umu_ids[idx],
        store=index.stores[idx],
        title=index.titles[idx],
        common_redist=index.common_redists[idx],
        note=index.notes[idx],
        match_source="codename",
    )


def _pick_store_row(
    index: _UmuDbIndex,
    candidates: tuple[int, ...],
    store: str,
) -> int:
    """Select the row matching store (case-insensitive), or the first candidate."""
    store_low = store.strip().lower()
    if store_low:
        for idx in candidates:
            if index.stores[idx].lower() == store_low:
                return idx
    return candidates[0]


def lookup_umu_database(
    exe_path: Path | None = None,
    game_title: str = "",
    store: str = "",
    db_path: Path | None = None,
) -> UmuDbLookupResult | None:
    """Look up a game in the local umu-database.csv cache by title or executable path."""
    resolved_db = db_path if db_path is not None else get_umu_database_path()
    index = _get_umu_db_index(resolved_db)
    if index is None:
        return None

    matched_idx: int | None = None
    match_source = ""

    if game_title.strip():
        norm = _normalize_umu_title(game_title)
        rows = index.by_norm_title.get(norm)
        if rows:
            matched_idx = _pick_store_row(index, rows, store)
            match_source = "title"

    if matched_idx is None and exe_path is not None:
        stem = exe_path.stem.strip()
        if stem:
            norm_stem = _normalize_umu_title(stem)
            rows = index.by_norm_title.get(norm_stem) or index.by_codename.get(stem.lower())
            if rows:
                matched_idx = _pick_store_row(index, rows, store)
                match_source = "exe_stem"

        if matched_idx is None:
            parent_name = exe_path.parent.name.strip()
            if parent_name.lower() not in _GENERIC_EXE_PARENTS:
                norm_parent = _normalize_umu_title(parent_name)
                rows = index.by_norm_title.get(norm_parent)
                if rows:
                    matched_idx = _pick_store_row(index, rows, store)
                    match_source = "exe_parent"

    if matched_idx is None:
        return None

    return UmuDbLookupResult(
        umu_id=index.umu_ids[matched_idx],
        store=index.stores[matched_idx],
        title=index.titles[matched_idx],
        common_redist=index.common_redists[matched_idx],
        note=index.notes[matched_idx],
        match_source=match_source,
    )


def get_umu_protonfix_status(
    game_id: str,
    store: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Return protonfix/database status for a UMU game_id from local umu-database.csv."""
    resolved_db = db_path if db_path is not None else get_umu_database_path()
    db_cached = resolved_db.is_file()
    cleaned_id = game_id.strip()
    if not cleaned_id or cleaned_id.lower() in ("umu-default", "none"):
        return {
            "game_id": cleaned_id,
            "store": store,
            "found": False,
            "has_protonfix": False,
            "umu_id": "",
            "title": "",
            "common_redist": "",
            "note": "",
            "db_cached": db_cached,
        }

    index = _get_umu_db_index(resolved_db)
    if index is None:
        return {
            "game_id": cleaned_id,
            "store": store,
            "found": False,
            "has_protonfix": False,
            "umu_id": "",
            "title": "",
            "common_redist": "",
            "note": "",
            "db_cached": db_cached,
        }

    low_id = cleaned_id.lower()
    rows = index.by_umu_id.get(low_id)
    if not rows and not low_id.startswith("umu-"):
        rows = index.by_umu_id.get(f"umu-{low_id}")

    if not rows:
        return {
            "game_id": cleaned_id,
            "store": store,
            "found": False,
            "has_protonfix": False,
            "umu_id": "",
            "title": "",
            "common_redist": "",
            "note": "",
            "db_cached": True,
        }

    idx = _pick_store_row(index, rows, store)
    matched_umu_id = index.umu_ids[idx]
    return {
        "game_id": cleaned_id,
        "store": index.stores[idx] or store,
        "found": True,
        "has_protonfix": bool(matched_umu_id and matched_umu_id.lower() != "umu-default"),
        "umu_id": matched_umu_id,
        "title": index.titles[idx],
        "common_redist": index.common_redists[idx],
        "note": index.notes[idx],
        "db_cached": True,
    }
