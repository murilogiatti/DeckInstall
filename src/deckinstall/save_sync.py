"""Pre-operation save snapshots and folder-based save synchronization."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import save_sync_repo as repo
from .fsutil import FileIdentity, atomic_write_bytes, file_identity, file_lock, is_within, needs_zip64, open_regular_fd
from .steam_vdf import (
    _RESTORE_MAX_ENTRIES,
    _RESTORE_MAX_FILE_BYTES,
    _RESTORE_MAX_TOTAL_BYTES,
    DEFAULT_SAVES_DIR,
    SNAPSHOT_KEEP_DEFAULT,
    _iter_prefix_save_pairs,
    _iter_programdata_save_pairs,
    _norm_key,
    _prefix_programdata_dir,
    _prefix_users_dir,
    _safe_parents_within,
    _zip_member_matches_file,
    restore_entry_blocked,
)

DEFAULT_SNAPSHOTS_DIR = DEFAULT_SAVES_DIR / "snapshots"
DEFAULT_SYNC_DIR = DEFAULT_SAVES_DIR / "sync"
DEFAULT_LUDUSAVI_MANIFEST_PATH = (
    Path.home() / ".local" / "share" / "deckinstall" / "ludusavi_manifest.json"
)

# SNAPSHOT_KEEP_DEFAULT (steam_vdf): snapshots kept per game unless the Backup/Sync screen changes it.
# Per-game sidecar in snapshots/<slug>/: {"digest": last content hash, "zip": its file, "played": run.log mtime}.
_SNAPSHOT_INDEX = ".index.json"
_RUN_EXIT_MARKER = b"== DeckInstall: exit code"

_META_MAX_BYTES = 1024 * 1024
_LUDUSAVI_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
_PREFIX_STATE_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class SaveFingerprint:
    """Deterministic fingerprint of all save files inside a Wine/Proton prefix."""

    latest_mtime: float
    file_count: int
    digest: str


@dataclass(frozen=True)
class SyncGameTarget:
    """Game metadata passed to folder save synchronization."""

    game_name: str
    prefix_dir: Path
    game_dir: Path | None = None
    steam_appid: int | None = None
    extra_dirs: ExtraDirs = ()
    source: str = "deckinstall"


@dataclass(frozen=True)
class SyncPlanItem:
    """Planned synchronization action for a single game prefix."""

    game_name: str
    slug: str
    prefix_dir: Path
    action: str  # "PUSH", "PULL", "IN_SYNC", "CONFLICT", "NO_SAVES", "UNAVAILABLE"
    reason: str
    game_dir: Path | None = None
    steam_appid: int | None = None
    extra_dirs: ExtraDirs = ()
    source: str = "deckinstall"
    heads: tuple[repo.Revision, ...] = ()  # the shared versions (several when they conflict)
    waiting: int = 0  # revision files still arriving or damaged (never used)


@dataclass(frozen=True)
class PrefixStateSnapshot:
    """Snapshot of Wine prefix registry files and system DLLs."""

    snapshot_zip: Path
    prefix_dir: Path
    reason: str
    reg_count: int
    dll_count: int


@dataclass(frozen=True)
class SyncDryRunFile:
    """Single file entry in a save-sync dry-run preview."""

    rel_path: str
    size_bytes: int
    operation: str


@dataclass(frozen=True)
class SyncDryRunPreview:
    """Dry-run summary of a planned save-sync operation."""

    game_name: str
    slug: str
    action: str
    reason: str
    files: list[SyncDryRunFile]
    total_bytes: int
    registry_entries: int = 0


def _slugify(value: str, fallback: str = "game") -> str:
    """Return a filesystem-safe lowercase slug (legacy ASCII behavior for reason tags/compat)."""
    stripped = value.strip()
    if not stripped:
        return fallback
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", stripped).strip("-.")
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:8]
    if cleaned in ("", ".", ".."):
        norm = _norm_key(stripped)
        base = norm or fallback
        return f"{base}-{digest}"
    if not stripped.isascii():
        return f"{cleaned.lower()}-{digest}"
    return cleaned.lower()


def _game_slug_hashed(game_name: str, fallback: str = "game") -> str:
    """Collision-free game slug: appends an 8-char SHA-256 suffix whenever punctuation could collide."""
    stripped = game_name.strip()
    if not stripped:
        return fallback
    base = _slugify(stripped, fallback=fallback)
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:8]
    if base.endswith(f"-{digest}"):
        return base
    # Plain alphanumeric words separated by single spaces map bijectively (case-insensitively)
    # to hyphen-joined lowercase, so they never collide with punctuated names (which get -<hash8>).
    if re.fullmatch(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*", stripped):
        return base
    return f"{base}-{digest}"


def _resolve_game_slug_dir(root: Path, game_name: str, *, meta_file: str, name_key: str) -> tuple[str, Path]:
    """Resolve a game's folder under root without ASCII slug collisions, preserving legacy folders."""
    hashed = _game_slug_hashed(game_name)
    hashed_dir = root / hashed
    legacy = _slugify(game_name)
    if legacy == hashed or hashed_dir.exists():
        return hashed, hashed_dir
    legacy_dir = root / legacy
    if legacy_dir.is_dir() and not legacy_dir.is_symlink():
        meta = _read_sync_meta(legacy_dir / meta_file)
        recorded = str(meta.get(name_key) or "").strip() if meta else ""
        if not recorded or recorded == game_name.strip():
            return legacy, legacy_dir
    return hashed, hashed_dir


_PREFIX_SYSTEM_PREFIXES = ("/usr", "/bin", "/sbin", "/opt", "/etc", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev")


def _unwrap_prefix_root(prefix_dir: Path) -> Path:
    """Follow prefix_dir only when it is a symlink to a real Wine/Proton prefix directory."""
    if not prefix_dir.is_symlink():
        return prefix_dir
    try:
        real = prefix_dir.resolve()
    except OSError:
        return prefix_dir
    s = str(real)
    if s == "/" or any(s == r or s.startswith(r + "/") for r in _PREFIX_SYSTEM_PREFIXES):
        return prefix_dir
    if s == "/var" or (s.startswith("/var/") and not s.startswith(("/var/home/", "/var/mnt/", "/var/tmp/"))):
        return prefix_dir
    if real.is_dir() and ((real / "drive_c").is_dir() or (real / "pfx" / "drive_c").is_dir()):
        return real
    return prefix_dir


def _resolve_wine_root(prefix_dir: Path) -> Path:
    """Return the Wine prefix root containing .reg files (pfx/ or prefix_dir)."""
    root = _unwrap_prefix_root(prefix_dir)
    pfx_dir = root / "pfx"
    if pfx_dir.is_dir() and not pfx_dir.is_symlink():
        return pfx_dir
    return root


def _require_safe_prefix_root(prefix_dir: Path) -> Path:
    """Unwrap prefix_dir and reject any symlink that failed prefix validation."""
    root = _unwrap_prefix_root(prefix_dir)
    if root.is_symlink():
        raise OSError(f"Unsafe target prefix root: {prefix_dir}")
    return root


def _require_safe_wine_root(prefix_dir: Path) -> tuple[Path, Path]:
    """Return validated (root, wine_root) for writing .reg files, rejecting unsafe symlinks."""
    root = _require_safe_prefix_root(prefix_dir)
    pfx_dir = root / "pfx"
    if pfx_dir.is_symlink():
        try:
            if pfx_dir.resolve() == root.resolve():
                return root, root
        except OSError as exc:
            raise OSError(f"Unsafe target wine root: {pfx_dir}") from exc
        raise OSError(f"Unsafe target wine root: {pfx_dir}")
    if pfx_dir.exists():
        if not pfx_dir.is_dir():
            raise OSError(f"Unsafe target wine root: {pfx_dir}")
        try:
            root_resolved = root.resolve()
        except OSError as exc:
            raise OSError(f"Unsafe target wine root: {pfx_dir}") from exc
        if not _safe_parents_within(root, root_resolved, ("pfx",)):
            raise OSError(f"Unsafe target wine root: {pfx_dir}")
        return root, pfx_dir
    return root, root


def _require_safe_drive_c(prefix_dir: Path) -> tuple[Path, Path]:
    """Return validated (root, drive_c) for writing prefix files, rejecting unsafe symlinks."""
    root = _require_safe_prefix_root(prefix_dir)
    drive_c = _resolve_drive_c_target(root)
    try:
        root_resolved = root.resolve()
        rel_parts = drive_c.relative_to(root).parts
    except (OSError, ValueError) as exc:
        raise OSError(f"Unsafe target drive_c path: {drive_c}") from exc
    if not _safe_parents_within(root, root_resolved, rel_parts):
        raise OSError(f"Unsafe target drive_c path: {drive_c}")
    return root, drive_c


_SYSTEM_WINE_USERS = frozenset({"public", "default", "default user", "all users"})


def _detect_prefix_user(drive_c: Path) -> str:
    """Return the existing non-Public Wine user directory name under drive_c/users, or 'steamuser'."""
    users_dir = _find_child_ci(drive_c, "users")
    if users_dir is None:
        return "steamuser"
    candidates: list[str] = []
    try:
        for child in users_dir.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name.lower() in _SYSTEM_WINE_USERS:
                continue
            candidates.append(child.name)
    except OSError:
        return "steamuser"
    for name in candidates:
        if name.lower() == "steamuser":
            return name
    return min(candidates) if candidates else "steamuser"


def _find_child_ci(parent: Path, name_lower: str) -> Path | None:
    """Find a direct non-symlink child directory matching name_lower."""
    if not parent.is_dir() or parent.is_symlink():
        return None
    try:
        for child in parent.iterdir():
            if not child.is_symlink() and child.is_dir() and child.name.lower() == name_lower:
                return child
    except OSError:
        return None
    return None


def _collect_prefix_state_entries(
    prefix_dir: Path, include_system_dlls: bool = True,
) -> list[tuple[str, Path, Path]]:
    """Collect sorted (kind, src_file, rel_path) tuples for prefix .reg and system DLL files."""
    root = _unwrap_prefix_root(prefix_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    entries: list[tuple[str, Path, Path]] = []
    wine_root = _resolve_wine_root(root)
    if wine_root.is_dir() and not wine_root.is_symlink():
        try:
            for item in sorted(wine_root.iterdir()):
                if not item.is_symlink() and item.is_file() and item.suffix.lower() == ".reg":
                    entries.append(("reg", item, Path(item.name)))
        except OSError:
            pass

    drive_c = _resolve_drive_c_target(root)
    windows_dir = _find_child_ci(drive_c, "windows") if include_system_dlls else None
    if windows_dir is not None:
        for sys_name in ("system32", "syswow64"):
            sys_dir = _find_child_ci(windows_dir, sys_name)
            if sys_dir is None:
                continue
            for dirpath, dirnames, filenames in os.walk(sys_dir, followlinks=False):
                dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
                cur = Path(dirpath)
                for fname in filenames:
                    if not fname.lower().endswith(".dll"):
                        continue
                    src_file = cur / fname
                    if src_file.is_symlink() or not src_file.is_file():
                        continue
                    entries.append(("dll", src_file, src_file.relative_to(drive_c)))

    entries.sort(key=lambda item: (item[0], item[2].as_posix()))
    return entries


@dataclass(frozen=True)
class _LudusaviManifestIndex:
    """Data-oriented flat lookup tables for the cached Ludusavi manifest."""

    key: tuple[Path, FileIdentity]
    entries: dict[str, dict[str, object]]
    by_appid: dict[str, dict[str, object]]
    by_norm_name: dict[str, dict[str, object]]


_LUDUSAVI_CACHE_LOCK = threading.Lock()
_LUDUSAVI_CACHE: _LudusaviManifestIndex | None = None


def _build_ludusavi_index(
    cache_key: tuple[Path, FileIdentity], raw: dict[object, object]
) -> _LudusaviManifestIndex:
    entries: dict[str, dict[str, object]] = {}
    by_appid: dict[str, dict[str, object]] = {}
    by_norm_name: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        name = str(k)
        entries[name] = v
        steam_info = v.get("steam")
        if isinstance(steam_info, dict):
            sid = str(steam_info.get("id", "")).strip()
            if sid and sid not in by_appid:
                by_appid[sid] = v
        nk = _norm_key(name)
        if nk and nk not in by_norm_name:
            by_norm_name[nk] = v
    return _LudusaviManifestIndex(
        key=cache_key,
        entries=entries,
        by_appid=by_appid,
        by_norm_name=by_norm_name,
    )


def _get_ludusavi_index(manifest_path: Path | None = None) -> _LudusaviManifestIndex | None:
    global _LUDUSAVI_CACHE
    target = manifest_path if manifest_path is not None else DEFAULT_LUDUSAVI_MANIFEST_PATH
    with _LUDUSAVI_CACHE_LOCK:
        try:
            st = target.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > _LUDUSAVI_MANIFEST_MAX_BYTES:
                return None
            cache_key = (target.absolute(), file_identity(st))
            if _LUDUSAVI_CACHE is not None and _LUDUSAVI_CACHE.key == cache_key:
                return _LUDUSAVI_CACHE
            with os.fdopen(open_regular_fd(target), "rb") as source:
                if file_identity(os.fstat(source.fileno())) != cache_key[1]:
                    return None
                data = source.read(_LUDUSAVI_MANIFEST_MAX_BYTES + 1)
                if len(data) > _LUDUSAVI_MANIFEST_MAX_BYTES or file_identity(os.fstat(source.fileno())) != cache_key[1]:
                    return None
            raw = json.loads(data.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        _LUDUSAVI_CACHE = _build_ludusavi_index(cache_key, raw)
        return _LUDUSAVI_CACHE


def find_manifest_game_entry(
    game_name: str,
    steam_appid: int | None = None,
    manifest: dict[str, dict[str, object]] | None = None,
) -> dict[str, object] | None:
    """Find a Ludusavi manifest entry by Steam AppID, exact name, or normalized name."""
    if manifest is None:
        idx = _get_ludusavi_index()
        if idx is None or not idx.entries:
            return None
        if steam_appid is not None:
            hit = idx.by_appid.get(str(steam_appid))
            if hit is not None:
                return deepcopy(hit)
        if game_name in idx.entries:
            return deepcopy(idx.entries[game_name])
        target_key = _norm_key(game_name)
        return deepcopy(idx.by_norm_name.get(target_key)) if target_key else None

    if not manifest:
        return None

    if steam_appid is not None:
        target_id = str(steam_appid)
        for entry in manifest.values():
            steam_info = entry.get("steam")
            if isinstance(steam_info, dict) and str(steam_info.get("id", "")).strip() == target_id:
                return entry

    if game_name in manifest:
        return manifest[game_name]

    target_key = _norm_key(game_name)
    if target_key:
        for key, entry in manifest.items():
            if _norm_key(key) == target_key:
                return entry
    return None


def _manifest_entry_applies(spec: object) -> bool:
    """Return True if a Ludusavi file/registry spec applies to Windows saves."""
    if not isinstance(spec, dict):
        return True
    tags = spec.get("tags")
    if isinstance(tags, list) and tags:
        norm_tags = {str(t).strip().lower() for t in tags}
        if "save" not in norm_tags:
            return False
    when = spec.get("when")
    if isinstance(when, list) and when:
        os_values = {
            str(w.get("os", "")).strip().lower()
            for w in when
            if isinstance(w, dict) and w.get("os")
        }
        if os_values and "windows" not in os_values:
            return False
    return True


def _expand_manifest_template(
    template: str,
    user_dirs: list[Path],
    drive_c: Path,
    game_dir: Path | None,
) -> list[tuple[str, Path, Path]]:
    """Expand a Ludusavi path template into (section, root_dir, resolved_pattern) tuples."""
    norm = template.replace("\\", "/").strip()
    if not norm:
        return []

    results: list[tuple[str, Path, Path]] = []
    users_dir = drive_c / "users"
    progdata_dir = drive_c / "ProgramData"

    def _sub_wildcards(rel: str, user_name: str) -> str:
        rel = rel.replace("<storeUserId>", "*")
        rel = rel.replace("<osUserName>", user_name)
        return rel.lstrip("/")

    user_placeholders = {
        "<winAppData>": "AppData/Roaming",
        "<winLocalAppData>": "AppData/Local",
        "<winLocalAppDataLow>": "AppData/LocalLow",
        "<winDocuments>": "Documents",
        "<home>": "",
    }
    for placeholder, sub_prefix in user_placeholders.items():
        if norm.startswith(placeholder):
            rest = norm[len(placeholder) :].lstrip("/")
            for udir in user_dirs:
                if udir.name.lower() == "public":
                    continue
                rel_sub = _sub_wildcards(rest, udir.name)
                base = udir / sub_prefix if sub_prefix else udir
                target = base / rel_sub if rel_sub else base
                results.append(("users", users_dir, target))
            return results

    if norm.startswith("<winPublic>"):
        rest = _sub_wildcards(norm[len("<winPublic>") :], "Public")
        pub_dir = users_dir / "Public"
        results.append(("users", users_dir, pub_dir / rest if rest else pub_dir))
        return results

    if norm.startswith("<winProgramData>"):
        rest = _sub_wildcards(norm[len("<winProgramData>") :], "Public")
        results.append(("ProgramData", progdata_dir, progdata_dir / rest if rest else progdata_dir))
        return results

    for base_tag in ("<base>", "<game>"):
        if norm.startswith(base_tag) and game_dir is not None:
            rest = _sub_wildcards(norm[len(base_tag) :], "steamuser")
            results.append(("game", game_dir, game_dir / rest if rest else game_dir))
            return results

    return results


_LINUX_TEMPLATE_ROOTS: dict[str, tuple[str, ...]] = {
    "<xdgData>": (".local", "share"),
    "<xdgConfig>": (".config",),
    "<home>": (),
}
_HOME_WALK_LIMIT = 2000  # files per template: a wrong template must not sweep the home folder


def _linux_only_spec(spec: object) -> bool:
    """A Ludusavi save path that applies on Linux (its `when` names os: linux)."""
    if not isinstance(spec, dict):
        return False
    tags = spec.get("tags")
    if isinstance(tags, list) and tags and "save" not in {str(t).strip().lower() for t in tags}:
        return False
    when = spec.get("when")
    if not isinstance(when, list):
        return False
    return any(isinstance(w, dict) and str(w.get("os", "")).strip().lower() == "linux" for w in when)


def _resolve_manifest_home_paths(entry: dict[str, object]) -> list[tuple[str, Path, Path]]:
    """("home", file, path under $HOME) for the Linux save paths of a Ludusavi entry (native games).

    Only <xdgData>, <xdgConfig> and <home> templates marked for Linux, and only files that
    home_entry_allowed accepts (never dotfiles of the session, keys or executables).
    """
    files_spec = entry.get("files")
    if not isinstance(files_spec, dict):
        return []
    root = _home_root()
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    out: list[tuple[str, Path, Path]] = []
    seen: set[str] = set()
    for raw_tpl, spec in files_spec.items():
        if not _linux_only_spec(spec):
            continue
        norm = str(raw_tpl).replace("\\", "/").strip()
        base_parts: tuple[str, ...] | None = None
        rest = ""
        for tag, parts in _LINUX_TEMPLATE_ROOTS.items():
            if norm.startswith(tag):
                base_parts, rest = parts, norm[len(tag):].lstrip("/")
                break
        if base_parts is None or not rest or "<" in rest:
            continue
        base = root.joinpath(*base_parts)
        try:
            matches = list(base.glob(rest)) if any(ch in rest for ch in "*?[") else [base / rest]
        except (OSError, ValueError):
            continue
        for cand in matches:
            if cand.is_symlink() or not is_within(cand, root_resolved):
                continue
            files = [cand] if cand.is_file() else []
            if cand.is_dir():
                for dirpath, dirnames, filenames in os.walk(cand, followlinks=False):
                    dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
                    files.extend(Path(dirpath) / f for f in filenames)
                    if len(files) > _HOME_WALK_LIMIT:
                        break
            for src in files[:_HOME_WALK_LIMIT]:
                try:
                    st = src.lstat()
                    rel = src.relative_to(root)
                except (OSError, ValueError):
                    continue
                if not stat.S_ISREG(st.st_mode) or not home_entry_allowed(rel.parts, st.st_mode):
                    continue
                if rel.as_posix() not in seen:
                    seen.add(rel.as_posix())
                    out.append(("home", src, rel))
    return out


def _manifest_registry_paths(entry: dict[str, object]) -> list[str]:
    """Return applicable Windows registry paths declared in a Ludusavi manifest entry."""
    reg_paths: list[str] = []
    reg_spec = entry.get("registry")
    if isinstance(reg_spec, dict):
        for raw_reg, spec in reg_spec.items():
            if _manifest_entry_applies(spec) and str(raw_reg).strip():
                reg_paths.append(str(raw_reg).strip())
    return reg_paths


def _resolve_manifest_save_paths(
    prefix_dir: Path,
    entry: dict[str, object],
    game_dir: Path | None = None,
) -> tuple[list[tuple[str, Path, Path]], list[str]]:
    """Resolve Ludusavi manifest files and registry paths inside prefix_dir/game_dir."""
    file_entries: list[tuple[str, Path, Path]] = []
    seen_files: set[tuple[str, str]] = set()

    root = _unwrap_prefix_root(prefix_dir)
    if root.is_symlink():
        drive_c = root / "drive_c"
        user_dirs: list[Path] = []
    else:
        drive_c = _resolve_drive_c_target(root)
        users_dir = _prefix_users_dir(root)
        user_dirs = []
        if users_dir is not None:
            try:
                user_dirs = [
                    d
                    for d in users_dir.iterdir()
                    if not d.is_symlink()
                    and d.is_dir()
                    and d.name.lower() not in {"default", "default user", "all users"}
                ]
            except OSError:
                user_dirs = []

    files_spec = entry.get("files")
    if isinstance(files_spec, dict):
        for raw_tpl, spec in files_spec.items():
            if not _manifest_entry_applies(spec):
                continue
            for section, section_root, target_pat in _expand_manifest_template(
                str(raw_tpl), user_dirs, drive_c, game_dir
            ):
                if section_root.is_symlink() or not section_root.is_dir():
                    continue
                try:
                    section_root_resolved = section_root.resolve()
                except OSError:
                    continue

                pat_str = str(target_pat)
                if any(ch in pat_str for ch in ("*", "?", "[")):
                    try:
                        rel_pat = target_pat.relative_to(section_root).as_posix()
                        matched_paths = list(section_root.glob(rel_pat))
                    except (OSError, ValueError):
                        matched_paths = []
                else:
                    matched_paths = [target_pat]

                for cand in matched_paths:
                    if cand.is_symlink() or not is_within(cand, section_root_resolved):
                        continue
                    if cand.is_file():
                        rel = cand.relative_to(section_root)
                        key = (section, rel.as_posix())
                        if key not in seen_files:
                            seen_files.add(key)
                            file_entries.append((section, cand, rel))
                    elif cand.is_dir():
                        for dirpath, dirnames, filenames in os.walk(cand, followlinks=False):
                            dirnames[:] = [
                                d for d in dirnames if not (Path(dirpath) / d).is_symlink()
                            ]
                            cur = Path(dirpath)
                            for fname in filenames:
                                src_file = cur / fname
                                if (
                                    src_file.is_symlink()
                                    or not src_file.is_file()
                                    or not is_within(src_file, section_root_resolved)
                                ):
                                    continue
                                rel = src_file.relative_to(section_root)
                                key = (section, rel.as_posix())
                                if key not in seen_files:
                                    seen_files.add(key)
                                    file_entries.append((section, src_file, rel))

    reg_paths = _manifest_registry_paths(entry)

    file_entries.sort(key=lambda item: (item[0], item[2].as_posix()))
    return file_entries, reg_paths


_HIVE_TO_REG_FILE: dict[str, tuple[str, str]] = {
    "HKEY_CURRENT_USER": ("HKEY_CURRENT_USER", "user.reg"),
    "HKCU": ("HKEY_CURRENT_USER", "user.reg"),
    "HKEY_LOCAL_MACHINE": ("HKEY_LOCAL_MACHINE", "system.reg"),
    "HKLM": ("HKEY_LOCAL_MACHINE", "system.reg"),
    "HKEY_USERS\\.DEFAULT": ("HKEY_USERS\\.DEFAULT", "userdef.reg"),
    "HKU\\.DEFAULT": ("HKEY_USERS\\.DEFAULT", "userdef.reg"),
}


def _split_reg_hive(reg_path: str) -> tuple[str, str, str] | None:
    """Return (canonical_hive, reg_filename, normalized_subkey) for a Windows registry path."""
    norm = reg_path.strip().replace("/", "\\")
    while "\\\\" in norm:
        norm = norm.replace("\\\\", "\\")
    norm = norm.strip("\\")
    if not norm:
        return None
    upper = norm.upper()
    for prefix, (hive, reg_file) in _HIVE_TO_REG_FILE.items():
        if upper == prefix or upper.startswith(prefix + "\\"):
            subkey = norm[len(prefix) :].lstrip("\\")
            if not subkey:
                return None
            return hive, reg_file, subkey
    return None


def _parse_wine_reg_sections(reg_text: str) -> list[tuple[str, str, list[str]]]:
    """Parse a Wine .reg file into [(unescaped_subkey, header_line, value_lines), ...]."""
    sections: list[tuple[str, str, list[str]]] = []
    cur_subkey: str | None = None
    cur_header = ""
    cur_lines: list[str] = []
    continuing = False

    for line in reg_text.splitlines():
        if continuing and cur_subkey is not None:
            cur_lines.append(line)
            continuing = line.endswith("\\")
            continue
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            if cur_subkey is not None:
                sections.append((cur_subkey, cur_header, cur_lines))
            rbracket = stripped.rindex("]")
            raw_key = stripped[1:rbracket]
            cur_subkey = raw_key.replace("\\\\", "\\")
            cur_header = line
            cur_lines = []
            continuing = False
            continue
        if cur_subkey is not None:
            if not stripped:
                sections.append((cur_subkey, cur_header, cur_lines))
                cur_subkey = None
                cur_header = ""
                cur_lines = []
                continue
            if stripped.startswith(";"):
                continue
            cur_lines.append(line)
            continuing = line.endswith("\\")

    if cur_subkey is not None:
        sections.append((cur_subkey, cur_header, cur_lines))
    return sections


def _extract_registry_saves(prefix_dir: Path, reg_paths: list[str]) -> bytes | None:
    """Extract matching Wine registry keys from prefix_dir as UTF-8 JSON bytes."""
    root = _unwrap_prefix_root(prefix_dir)
    if not reg_paths or root.is_symlink() or not root.is_dir():
        return None
    wine_root = _resolve_wine_root(root)
    targets_by_file: dict[str, list[tuple[str, str]]] = {}
    for raw_path in reg_paths:
        parsed = _split_reg_hive(raw_path)
        if parsed is None:
            continue
        hive, reg_filename, subkey = parsed
        targets_by_file.setdefault(reg_filename, []).append((hive, subkey))

    if not targets_by_file:
        return None

    extracted: list[dict[str, object]] = []
    for reg_filename, targets in sorted(targets_by_file.items()):
        reg_file = wine_root / reg_filename
        if not reg_file.is_file() or reg_file.is_symlink():
            continue
        try:
            text = reg_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sections = _parse_wine_reg_sections(text)
        for sec_subkey, _, val_lines in sections:
            value_only_lines = [ln for ln in val_lines if not ln.lstrip().startswith("#")]
            if not value_only_lines:
                continue
            sec_low = sec_subkey.lower()
            matched_hive: str | None = None
            for hive, target_subkey in targets:
                t_low = target_subkey.lower()
                if sec_low == t_low or sec_low.startswith(t_low + "\\"):
                    matched_hive = hive
                    break
            if matched_hive is not None:
                extracted.append(
                    {
                        "hive": matched_hive,
                        "subkey": sec_subkey,
                        "lines": value_only_lines,
                    }
                )

    if not extracted:
        return None
    payload = {"version": 1, "entries": extracted}
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def _restore_registry_saves(prefix_dir: Path, reg_json_bytes: bytes) -> int:
    """Restore registry key entries from JSON bytes into Wine .reg files in prefix_dir."""
    if len(reg_json_bytes) > _RESTORE_MAX_FILE_BYTES:
        raise RuntimeError("Registry save payload exceeds maximum allowed size")
    try:
        payload = json.loads(reg_json_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid registry save JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return 0

    _, wine_root = _require_safe_wine_root(prefix_dir)
    wine_root.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list[tuple[str, list[str]]]] = {}

    for item in payload["entries"]:
        if not isinstance(item, dict):
            continue
        hive_raw = str(item.get("hive", "")).strip().upper()
        subkey = str(item.get("subkey", "")).strip().replace("/", "\\").strip("\\")
        raw_lines = item.get("lines")
        if not subkey or "\n" in subkey or "\r" in subkey or "\0" in subkey or "[" in subkey or "]" in subkey:
            continue
        if not isinstance(raw_lines, list):
            continue
        hive_info = _HIVE_TO_REG_FILE.get(hive_raw)
        if hive_info is None:
            continue
        _, reg_filename = hive_info
        lines: list[str] = []
        prev_continued = False
        for ln in raw_lines:
            if not isinstance(ln, str) or "\n" in ln or "\r" in ln or "\0" in ln:
                continue
            ls = ln.lstrip()
            if not ls or ls.startswith("["):
                continue
            if not prev_continued and not (ls.startswith('"') or ls.startswith("@=")):
                continue
            lines.append(ln)
            prev_continued = ln.endswith("\\")
        if lines:
            by_file.setdefault(reg_filename, []).append((subkey, lines))

    restored_count = 0
    stamp = int(time.time())
    for reg_filename, items in by_file.items():
        reg_file = wine_root / reg_filename
        if reg_file.is_symlink():
            continue
        try:
            existing_text = reg_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            existing_text = ""

        if not existing_text.strip():
            existing_text = "WINE REGISTRY Version 2\n"

        existing_sections = _parse_wine_reg_sections(existing_text)
        item_map = {subkey.lower(): (subkey, lines) for subkey, lines in items}
        replaced_keys: set[str] = set()

        # Rebuild file preserving header and non-overwritten sections
        header_lines: list[str] = []
        for ln in existing_text.splitlines():
            if ln.strip().startswith("["):
                break
            header_lines.append(ln)
        if not header_lines:
            header_lines = ["WINE REGISTRY Version 2", ""]

        out_lines = list(header_lines)
        for sec_subkey, sec_header, sec_lines in existing_sections:
            sec_low = sec_subkey.lower()
            if sec_low in item_map:
                _, new_lines = item_map[sec_low]
                out_lines.append(sec_header)
                out_lines.extend(new_lines)
                out_lines.append("")
                replaced_keys.add(sec_low)
                restored_count += 1
            else:
                out_lines.append(sec_header)
                out_lines.extend(sec_lines)
                out_lines.append("")

        for subkey, new_lines in items:
            sec_low = subkey.lower()
            if sec_low in replaced_keys:
                continue
            replaced_keys.add(sec_low)
            escaped_subkey = subkey.replace("\\", "\\\\")
            out_lines.append(f"[{escaped_subkey}] {stamp}")
            out_lines.extend(new_lines)
            out_lines.append("")
            restored_count += 1

        atomic_write_bytes(reg_file, ("\n".join(out_lines).rstrip() + "\n").encode("utf-8"))

    return restored_count


# Save folders outside the Wine prefix, stored in snapshots under their own section.
# "steamcloud" = Steam's local cloud copy (userdata/<user>/<appid>/remote).
EXTRA_SECTIONS = ("steamcloud",)
ExtraDirs = tuple[tuple[str, Path], ...]
_EXTRA_SKIP_DIRS = frozenset({"shadercache", "cache", "logs", "dxvk", "temp", "gpucache"})
_EXTRA_SKIP_SUFFIXES = (".log", ".dxvk-cache", ".tmp")

# "home" section (native games' saves under $HOME, from Ludusavi): only these folders, each with a
# game folder below it; the blocked ones and the names/suffixes below are refused even inside them.
_HOME_ALLOWED_ROOTS = (
    (".local", "share"), (".local", "state"), (".config",), (".renpy",), ("documents", "my games"),
    ("documents", "paradox interactive"), (".paradoxinteractive",), (".unity3d",), (".godot",),
)
_HOME_BLOCKED_ROOTS = (
    (".ssh",), (".gnupg",), (".password-store",), (".mozilla",), (".steam",),
    (".config", "autostart"), (".config", "systemd"), (".config", "environment.d"),
    (".config", "google-chrome"), (".config", "chromium"), (".config", "fish"),
    (".config", "plasma-workspace"), (".local", "bin"), (".local", "share", "applications"),
    (".local", "share", "keyrings"), (".local", "share", "systemd"), (".local", "share", "dbus-1"),
    (".local", "share", "steam"),
    # Config read by tools that run code or change permissions (hooks, plugins, sandbox overrides).
    (".config", "git"), (".config", "nvim"), (".config", "vim"), (".vim",), (".config", "pip"),
    (".config", "code"), (".config", "vscodium"), (".config", "kitty"), (".config", "alacritty"),
    (".config", "mpv"), (".config", "containers"), (".config", "pipewire"), (".config", "wireplumber"),
    (".config", "xdg-desktop-portal"), (".config", "kservicemenus"), (".config", "menus"),
    (".config", "gtk-3.0"), (".config", "gtk-4.0"), (".config", "dconf"),
    (".local", "share", "flatpak"), (".local", "share", "kservices5"), (".local", "share", "kservices6"),
    (".local", "share", "kio"), (".local", "share", "plasma"), (".local", "share", "dolphin"),
    (".local", "share", "kwin"), (".local", "share", "trash"),
    (".local", "share", "vulkan"), (".local", "share", "deckinstall"), (".config", "deckinstall"),
    (".local", "share", "bash-completion"), (".local", "share", "konsole"),
    (".local", "share", "nautilus"), (".local", "share", "nemo"),
    (".config", "autostart-scripts"), (".config", "gdb"), (".config", "tmux"),
    (".config", "direnv"), (".config", "wezterm"), (".config", "hypr"),
    (".config", "sway"), (".config", "i3"), (".config", "cargo"),
    (".config", "nix"), (".config", "npm"), (".config", "zsh"), (".config", "bash"),
    # Session/WM startup and shell-extension code that runs at the next login.
    (".local", "share", "gnome-shell"), (".config", "openbox"), (".config", "lxsession"),
    (".config", "lxqt"), (".config", "awesome"), (".config", "qtile"), (".config", "niri"),
    (".config", "river"), (".config", "bspwm"), (".config", "sxhkd"), (".config", "xfce4"),
    (".config", "labwc"), (".config", "wayfire"), (".config", "foot"), (".config", "ranger"),
    (".local", "share", "autostart"),
)
_HOME_BLOCKED_NAMES = frozenset({
    ".bashrc", ".profile", ".bash_profile", ".bash_login", ".bash_logout", ".zshrc", ".zprofile",
    ".zshenv", ".pam_environment", ".xprofile", ".xinitrc", ".xsession",
})
_HOME_BLOCKED_SUFFIXES = (
    ".desktop", ".service", ".sh", ".jar", ".exe", ".dll", ".so", ".bat", ".cmd", ".appimage",
)
# Under ~/.config these are scripts/configs of the session (WM rc files, plugins); elsewhere they may be saves.
_HOME_CONFIG_BLOCKED_SUFFIXES = (".js", ".mjs", ".py", ".lua", ".rb", ".pl", ".kdl", ".rc")
_HOME_BLOCKED_PREFIXES = ("autostart", "rc.", "xinitrc", "xprofile")


def home_entry_allowed(parts: tuple[str, ...], mode: int = 0) -> bool:
    """True when a "home" save path (relative to $HOME) may be imported, snapshotted or restored.

    mode is the file's Unix mode (st_mode, or a zip entry's external_attr >> 16): executables
    and symlinks are refused.
    """
    if not parts or any(p in ("", ".", "..") or "/" in p or "\\" in p or "\0" in p for p in parts):
        return False
    if mode & 0o111 or stat.S_ISLNK(mode):
        return False
    low = tuple(p.lower() for p in parts)
    if (
        low[-1] in _HOME_BLOCKED_NAMES
        or low[-1].endswith(_HOME_BLOCKED_SUFFIXES)
        or low[-1].startswith(_HOME_BLOCKED_PREFIXES)
        or (low[0] == ".config" and low[-1].endswith(_HOME_CONFIG_BLOCKED_SUFFIXES))
    ):
        return False
    if any(low[: len(root)] == root for root in _HOME_BLOCKED_ROOTS):
        return False
    return any(len(low) >= len(root) + 2 and low[: len(root)] == root for root in _HOME_ALLOWED_ROOTS)


def _home_root() -> Path:
    """Home folder for "home" snapshot entries (a seam for tests)."""
    return Path.home()


def _home_save_entries(home_files: tuple[str, ...]) -> list[tuple[str, Path, Path]]:
    """("home", file, path under $HOME) for the game's recorded home files that exist and are allowed."""
    if not home_files:
        return []
    root = _home_root()
    root_resolved = root.resolve()
    entries: list[tuple[str, Path, Path]] = []
    for rel in sorted(set(home_files)):
        parts = tuple(rel.split("/"))
        if not home_entry_allowed(parts) or not _safe_parents_within(root, root_resolved, parts[:-1]):
            continue
        src = root.joinpath(*parts)
        try:
            st = src.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and home_entry_allowed(parts, st.st_mode):
            entries.append(("home", src, Path(*parts)))
    return entries


def _extra_save_entries(extra_dirs: ExtraDirs | None) -> list[tuple[str, Path, Path]]:
    """(section, file, path inside that folder) for extra save folders; caches and logs left out."""
    entries: list[tuple[str, Path, Path]] = []
    for section, root in extra_dirs or ():
        if section not in EXTRA_SECTIONS or root.is_symlink() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d.lower() not in _EXTRA_SKIP_DIRS]
            cur = Path(dirpath)
            for fname in filenames:
                if fname.lower().endswith(_EXTRA_SKIP_SUFFIXES):
                    continue
                src = cur / fname
                if src.is_symlink() or not src.is_file():
                    continue
                entries.append((section, src, src.relative_to(root)))
    return entries


def _collect_prefix_save_entries(
    prefix_dir: Path,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
    home_files: tuple[str, ...] = (),
    *,
    strict: bool = False,
) -> list[tuple[str, Path, Path]]:
    """Collect sorted (section, src_file, rel_path) tuples for save files in prefix_dir (and extra_dirs).

    home_files: paths under $HOME (recorded in the snapshot index) added as the "home" section.
    """
    entries: list[tuple[str, Path, Path]] = _extra_save_entries(extra_dirs) + _home_save_entries(home_files)
    seen: set[tuple[str, str]] = {(sec, rel.as_posix()) for sec, _src, rel in entries}
    if prefix_dir.is_dir():
        src_users = _prefix_users_dir(prefix_dir)
        if src_users is not None:
            dummy_users = prefix_dir / "__fp_users__"
            for src_file, dst_rel in _iter_prefix_save_pairs(
                src_users, dummy_users, preserve_user_dirs=True, raise_on_error=strict
            ):
                rel = dst_rel.relative_to(dummy_users)
                seen.add(("users", rel.as_posix()))
                entries.append(("users", src_file, rel))
        src_progdata = _prefix_programdata_dir(prefix_dir)
        if src_progdata is not None:
            dummy_pd = prefix_dir / "__fp_pd__"
            for src_file, dst_rel in _iter_programdata_save_pairs(
                src_progdata, dummy_pd, raise_on_error=strict
            ):
                rel = dst_rel.relative_to(dummy_pd)
                seen.add(("ProgramData", rel.as_posix()))
                entries.append(("ProgramData", src_file, rel))

    if game_name or steam_appid is not None:
        manifest_entry = find_manifest_game_entry(game_name, steam_appid=steam_appid)
        if manifest_entry is not None:
            m_files, _ = _resolve_manifest_save_paths(prefix_dir, manifest_entry, game_dir=game_dir)
            m_files += _resolve_manifest_home_paths(manifest_entry)
            for section, src_file, rel in m_files:
                key = (section, rel.as_posix())
                if key not in seen:
                    seen.add(key)
                    entries.append((section, src_file, rel))

    entries.sort(key=lambda item: (item[0], item[2].as_posix()))
    return entries


_PROTECTED_SNAPSHOT_RE = re.compile(
    r"-(?:pre-restore|pre-import|pre-sync-pull|ludusavi-import)(?:-\d+)?\.zip$"
)


def _rotate_snapshots(snap_dir: Path, max_keep: int) -> None:
    """Keep at most max_keep newest normal .zip snapshots in snap_dir (safety/imports preserved)."""
    if max_keep <= 0 or not snap_dir.is_dir():
        return
    zips: list[tuple[float, str, Path]] = []
    for p in snap_dir.glob("*.zip"):
        if p.is_symlink() or not p.is_file() or _PROTECTED_SNAPSHOT_RE.search(p.name):
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
    target_zip: Path | None,
    key_prefix: str,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
    home_files: tuple[str, ...] = (),
) -> tuple[int, str]:
    """Write and fingerprint the same save bytes in one pass.

    With target_zip None nothing is written: only (count, digest) are computed, the same way.
    """
    entries = _collect_prefix_save_entries(
        prefix_dir, game_name, game_dir, steam_appid, extra_dirs, home_files, strict=True,
    )
    reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
    written = 0
    total_bytes = 0
    has_content = False
    digest = hashlib.sha256()
    if target_zip is not None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target_zip.stem}-", suffix=".zip.tmp", dir=target_zip.parent)
        os.close(fd)
        tmp_zip: Path | None = Path(tmp_name)
    else:
        tmp_zip = None
    try:
        with (zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) if tmp_zip is not None
              else nullcontext(None)) as zf:
            seen: set[str] = set()
            for section, src_file, rel_path in entries:
                name = f"{section}/{rel_path.as_posix()}"
                if name in seen:
                    continue
                with os.fdopen(open_regular_fd(src_file), "rb") as source:
                    before = os.fstat(source.fileno())
                    size = before.st_size
                    if written >= _RESTORE_MAX_ENTRIES:
                        raise RuntimeError(f"Snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
                    if size > _RESTORE_MAX_FILE_BYTES or total_bytes + size > _RESTORE_MAX_TOTAL_BYTES:
                        raise RuntimeError(f"Snapshot size limit exceeded on {src_file.name}")
                    if size:
                        digest.update(f"{name}\0{size}\0".encode())
                        has_content = True
                    copied = 0
                    with (zf.open(f"{key_prefix}/{name}", "w", force_zip64=needs_zip64(size))
                          if zf is not None else nullcontext(None)) as dest:
                        while chunk := source.read(65536):
                            copied += len(chunk)
                            if copied > size:
                                raise RuntimeError(f"Save changed during snapshot: {src_file}")
                            if dest is not None:
                                dest.write(chunk)
                            digest.update(chunk)
                    if copied != size or file_identity(os.fstat(source.fileno())) != file_identity(before):
                        raise RuntimeError(f"Save changed during snapshot: {src_file}")
                    total_bytes += size
                    written += 1
                    seen.add(name)
            if reg_bytes:
                if written >= _RESTORE_MAX_ENTRIES:
                    raise RuntimeError(f"Snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
                if len(reg_bytes) > _RESTORE_MAX_FILE_BYTES or total_bytes + len(reg_bytes) > _RESTORE_MAX_TOTAL_BYTES:
                    raise RuntimeError("Snapshot size limit exceeded on registry.json")
                digest.update(f"registry/registry.json\0{len(reg_bytes)}\0".encode())
                digest.update(reg_bytes)
                if zf is not None:
                    zf.writestr(f"{key_prefix}/registry/registry.json", reg_bytes)
                written += 1
                has_content = True
        if written and tmp_zip is not None and target_zip is not None:
            os.replace(tmp_zip, target_zip)
        return written, digest.hexdigest() if has_content else ""
    finally:
        if tmp_zip is not None:
            tmp_zip.unlink(missing_ok=True)


@dataclass
class _DirLockEntry:
    lock: threading.RLock
    depth: int = 0
    fh: IO[str] | None = None


_SNAP_LOCKS_GUARD = threading.Lock()
_SNAP_THREAD_LOCKS: dict[str, _DirLockEntry] = {}


@contextmanager
def _snapshot_dir_lock(snap_dir: Path) -> Iterator[None]:
    """Serialize snapshot creation, rotation, and .index.json updates per snapshot folder."""
    snap_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = str(snap_dir.resolve())
    except OSError:
        key = str(snap_dir)
    with _SNAP_LOCKS_GUARD:
        entry = _SNAP_THREAD_LOCKS.get(key)
        if entry is None:
            entry = _DirLockEntry(lock=threading.RLock())
            _SNAP_THREAD_LOCKS[key] = entry
    lock_path = snap_dir.parent / f".{snap_dir.name}.lock"
    with entry.lock:
        if entry.depth == 0:
            lfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                st = os.fstat(lfd)
                if not stat.S_ISREG(st.st_mode):
                    raise OSError(f"Refusing non-regular lock file: {lock_path}")
                fh = os.fdopen(lfd, "r+", encoding="utf-8")
            except Exception:
                os.close(lfd)
                raise
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                fh.close()
                raise
            entry.fh = fh
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0 and entry.fh is not None:
                lock_fh = entry.fh
                entry.fh = None
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_fh.close()


def _reserve_snapshot_zip(snap_dir: Path, stem_prefix: str) -> Path:
    """Atomically reserve a unique .zip filename in snap_dir via O_CREAT | O_EXCL."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        cand = snap_dir / f"{stem_prefix}{suffix}.zip"
        try:
            fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            counter += 1
            continue
        os.close(fd)
        return cand


def snapshot_dir_for(game_name: str, dest_root: Path | None = None) -> Path:
    """The folder holding a game's save snapshots."""
    root = dest_root or DEFAULT_SNAPSHOTS_DIR
    _, path = _resolve_game_slug_dir(root, game_name, meta_file=_SNAPSHOT_INDEX, name_key="name")
    return path


def _read_snapshot_index(snap_dir: Path) -> dict[str, object]:
    raw = _read_sync_meta(snap_dir / _SNAPSHOT_INDEX)
    return raw if raw is not None else {}


def _write_snapshot_index(snap_dir: Path, index: dict[str, object]) -> None:
    try:
        atomic_write_bytes(snap_dir / _SNAPSHOT_INDEX, json.dumps(index, sort_keys=True).encode("utf-8"))
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not write snapshot index in %s: %s", snap_dir, exc)


def remember_snapshot_name(snap_dir: Path, game_name: str) -> None:
    """Keep the game's real name next to its snapshots (shown once the game is uninstalled)."""
    if not game_name or not snap_dir.is_dir():
        return
    with _snapshot_dir_lock(snap_dir):
        index = _read_snapshot_index(snap_dir)
        if index.get("name") != game_name:
            index["name"] = game_name
            _write_snapshot_index(snap_dir, index)


def import_fingerprints(snap_dir: Path) -> dict[str, str]:
    """{zip name: content fingerprint} recorded for the Ludusavi imports still on disk."""
    raw = _read_snapshot_index(snap_dir).get("import_fingerprints")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and (snap_dir / k).is_file()}


def remember_import_fingerprint(snap_dir: Path, zip_name: str, fingerprint: str) -> None:
    """Record the content fingerprint of one Ludusavi import zip (to skip importing it again)."""
    with _snapshot_dir_lock(snap_dir):
        index = _read_snapshot_index(snap_dir)
        known = import_fingerprints(snap_dir)
        known[zip_name] = fingerprint
        index["import_fingerprints"] = known
        _write_snapshot_index(snap_dir, index)


def snapshot_home_files(snap_dir: Path) -> tuple[str, ...]:
    """The game's files under $HOME recorded in the index (allowed relative paths only)."""
    raw = _read_snapshot_index(snap_dir).get("home")
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({r for r in raw if isinstance(r, str) and home_entry_allowed(tuple(r.split("/")))}))


def remember_home_files(snap_dir: Path, rels: list[str] | tuple[str, ...]) -> None:
    """Add home save paths to the index, so every later snapshot (and its fingerprint) holds them."""
    incoming = {r for r in rels if home_entry_allowed(tuple(r.split("/")))}
    if not incoming:
        return
    with _snapshot_dir_lock(snap_dir):
        merged = set(snapshot_home_files(snap_dir)) | incoming
        if not merged:
            return
        snap_dir.mkdir(parents=True, exist_ok=True)
        index = _read_snapshot_index(snap_dir)
        if index.get("home") != sorted(merged):
            index["home"] = sorted(merged)
            _write_snapshot_index(snap_dir, index)


def remember_steamcloud_appid(snap_dir: Path, zip_name: str, appid: str) -> None:
    """Record which Steam app the "steamcloud" files of one snapshot zip belong to."""
    with _snapshot_dir_lock(snap_dir):
        index = _read_snapshot_index(snap_dir)
        raw = index.get("steamcloud_appids")
        known = {k: v for k, v in raw.items() if (snap_dir / k).is_file()} if isinstance(raw, dict) else {}
        known[zip_name] = appid
        index["steamcloud_appids"] = known
        _write_snapshot_index(snap_dir, index)


def snapshot_steamcloud_appid(snapshot_zip: Path) -> str | None:
    """The Steam app id recorded for a snapshot's "steamcloud" files, or None when unknown."""
    raw = _read_snapshot_index(snapshot_zip.parent).get("steamcloud_appids")
    value = raw.get(snapshot_zip.name) if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value.isdigit() else None


def _archive_home_and_cloud(archive_path: Path) -> tuple[list[str], bool]:
    """(allowed "home" paths, has "steamcloud" files) of a snapshot zip."""
    home: list[str] = []
    cloud = False
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist()[:_RESTORE_MAX_ENTRIES]:
            parsed = None if info.is_dir() else _parse_sync_zip_entry(info.filename)
            if parsed is None:
                continue
            section, rel_parts = parsed
            if section == "home" and home_entry_allowed(rel_parts, info.external_attr >> 16):
                home.append("/".join(rel_parts))
            cloud = cloud or section == "steamcloud"
    return home, cloud


def archive_has_steamcloud(archive_path: Path) -> bool:
    """True when a snapshot zip holds Steam cloud ("steamcloud") files."""
    return _archive_home_and_cloud(archive_path)[1]


class SteamCloudAppMismatch(RuntimeError):
    """A snapshot's Steam cloud files belong to another (or an unknown) Steam app."""

    def __init__(self, snap_appid: str | None, target_appid: int | None) -> None:
        super().__init__(f"Steam cloud saves of app {snap_appid or '?'} cannot go into app {target_appid or '?'}")
        self.snap_appid = snap_appid
        self.target_appid = target_appid


def snapshot_display_name(slug: str, dest_root: Path | None = None) -> str:
    """The name saved with a game's snapshots, or the folder name."""
    name = _read_snapshot_index((dest_root or DEFAULT_SNAPSHOTS_DIR) / slug).get("name")
    return name if isinstance(name, str) and name.strip() else slug


def newest_save_mtime(
    prefix_dir: Path,
    game_name: str,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> float:
    """Modification time of the newest save file the game has now (0 when it has none)."""
    home_files = snapshot_home_files(snapshot_dir_for(game_name))
    newest = 0.0
    for _section, src, _rel in _collect_prefix_save_entries(
        prefix_dir, game_name, game_dir, steam_appid, extra_dirs, home_files
    ):
        try:
            newest = max(newest, src.stat().st_mtime)
        except OSError:
            continue
    return newest


def snapshot_prefix_saves(
    prefix_dir: Path,
    game_name: str,
    reason: str,
    key_prefix: str = "snap",
    dest_root: Path | None = None,
    max_keep: int = SNAPSHOT_KEEP_DEFAULT,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    dedup: bool = False,
    extra_dirs: ExtraDirs | None = None,
) -> Path | None:
    """Create a rotated ZIP snapshot of prefix saves (max_keep <= 0: no rotation).

    With dedup, a matching content hash reuses the newest snapshot.
    """
    snap_dir = snapshot_dir_for(game_name, dest_root)
    with _snapshot_dir_lock(snap_dir):
        home_files = snapshot_home_files(snap_dir)
        if not prefix_dir.is_dir() and not extra_dirs and not home_files and not (game_dir and game_dir.is_dir()):
            return None
        if dedup:
            # Compare first, write only when something changed (no rewrite of an unchanged
            # snapshot on a microSD card or a slow disk).
            index = _read_snapshot_index(snap_dir)
            last_zip = snap_dir / Path(str(index.get("zip") or "")).name
            if last_zip.suffix == ".zip" and last_zip.is_file() and not last_zip.is_symlink() and index.get("digest"):
                count, pre_digest = _write_saves_zip_atomic(
                    prefix_dir, None, key_prefix, game_name=game_name,
                    game_dir=game_dir, steam_appid=steam_appid, extra_dirs=extra_dirs, home_files=home_files,
                )
                if count and pre_digest == index.get("digest"):
                    return last_zip
        safe_reason = _slugify(reason, fallback="op")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target_zip = _reserve_snapshot_zip(snap_dir, f"{stamp}-{safe_reason}")
        try:
            written, digest = _write_saves_zip_atomic(
                prefix_dir, target_zip, key_prefix, game_name=game_name,
                game_dir=game_dir, steam_appid=steam_appid, extra_dirs=extra_dirs, home_files=home_files,
            )
            if written == 0 or (dedup and not digest):
                target_zip.unlink(missing_ok=True)
                return None
        except BaseException:
            target_zip.unlink(missing_ok=True)
            raise
        if dedup:
            index = _read_snapshot_index(snap_dir)
            last_zip = snap_dir / Path(str(index.get("zip") or "")).name
            if (
                index.get("digest") == digest
                and last_zip.suffix == ".zip"
                and last_zip.is_file()
                and not last_zip.is_symlink()
            ):
                target_zip.unlink()
                return last_zip
        if steam_appid is not None and any(name == "steamcloud" for name, _ in extra_dirs or ()):
            remember_steamcloud_appid(snap_dir, target_zip.name, str(steam_appid))
        index = _read_snapshot_index(snap_dir)
        if dedup:
            index.update(digest=digest, zip=target_zip.name)
        index["name"] = game_name
        _write_snapshot_index(snap_dir, index)
        _rotate_snapshots(snap_dir, max_keep)
        return target_zip


def auto_snapshot(
    prefix_dir: Path,
    game_name: str,
    reason: str,
    key_prefix: str = "snap",
    *,
    keep: int = SNAPSHOT_KEEP_DEFAULT,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    dest_root: Path | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> Path | None:
    """The one entry point for app snapshots: deduplicated by content, newest `keep` kept per game."""
    return snapshot_prefix_saves(
        prefix_dir,
        game_name,
        reason,
        key_prefix=key_prefix,
        dest_root=dest_root,
        max_keep=keep,
        game_dir=game_dir,
        steam_appid=steam_appid,
        dedup=True,
        extra_dirs=extra_dirs,
    )


def _run_finished(run_log: Path) -> bool:
    """True when launch.sh wrote its exit line (the game is not running anymore)."""
    try:
        with run_log.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - 4096))
            return _RUN_EXIT_MARKER in fh.read()
    except OSError:
        return False


def snapshot_after_play(
    prefix_dir: Path,
    game_name: str,
    run_log: Path,
    *,
    keep: int = SNAPSHOT_KEEP_DEFAULT,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    dest_root: Path | None = None,
) -> Path | None:
    """Snapshot once per finished session recorded in run.log; None when there is nothing new."""
    if run_log.is_symlink() or not run_log.is_file():
        return None
    try:
        played = run_log.stat().st_mtime
    except OSError:
        return None
    snap_dir = snapshot_dir_for(game_name, dest_root)
    with _snapshot_dir_lock(snap_dir):
        seen = _read_snapshot_index(snap_dir).get("played")
        if isinstance(seen, (int, float)) and played <= float(seen):
            return None
        if not _run_finished(run_log):
            return None
        snap = auto_snapshot(
            prefix_dir, game_name, "after-play", keep=keep,
            game_dir=game_dir, steam_appid=steam_appid, dest_root=dest_root,
        )
        snap_dir.mkdir(parents=True, exist_ok=True)
        index = _read_snapshot_index(snap_dir)
        index["played"] = played
        _write_snapshot_index(snap_dir, index)
        return snap


def _wrap_restore_error(exc: Exception, safety: Path | None) -> Exception:
    """Attach the created safety snapshot path to a restore failure while preserving OSError."""
    if safety is None or str(safety) in str(exc):
        return exc
    msg = f"{exc} (safety snapshot: {safety})"
    if isinstance(exc, OSError):
        try:
            return type(exc)(msg)
        except TypeError:
            return OSError(msg)
    return RuntimeError(msg)


def restore_snapshot_safely(
    snapshot_zip: Path,
    prefix_dir: Path,
    game_name: str,
    *,
    state: bool = False,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    dest_root: Path | None = None,
    extra_dirs: ExtraDirs | None = None,
    skipped: list[str] | None = None,
) -> tuple[int, Path | None]:
    """Snapshot the current saves (never rotated away), then restore snapshot_zip.

    Returns (items restored, safety snapshot). A failed safety snapshot aborts the restore.
    Files under $HOME are overwritten only when the safety snapshot holds them; the others are
    left alone and their paths appended to skipped. Steam cloud files go only into the cloud
    folder of the Steam app recorded for the snapshot (SteamCloudAppMismatch otherwise).
    """
    if snapshot_zip.is_symlink() or not snapshot_zip.is_file():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_zip}")
    home_rels: list[str] = []
    if not state:
        home_rels, has_cloud = _archive_home_and_cloud(snapshot_zip)
        if has_cloud and any(name == "steamcloud" for name, _ in extra_dirs or ()):
            want = snapshot_steamcloud_appid(snapshot_zip)
            # Snapshots taken before the app id was recorded: only the app's own ones (never an import).
            legacy_ok = want is None and "ludusavi-import" not in snapshot_zip.name and steam_appid is not None
            if not legacy_ok and (want is None or want != str(steam_appid)):
                raise SteamCloudAppMismatch(want, steam_appid)
        if home_rels:  # the safety snapshot (and every later one) must hold the home files replaced now
            remember_home_files(snapshot_dir_for(game_name, dest_root), home_rels)
    if state:
        state_snap = snapshot_prefix_state(
            prefix_dir, game_name, "pre-restore", dest_root=dest_root,
        )
        safety = state_snap.snapshot_zip if state_snap is not None else None
        try:
            files, regs = restore_prefix_state_snapshot(snapshot_zip, prefix_dir)
        except Exception as exc:
            raise _wrap_restore_error(exc, safety) from exc
        return files + regs, safety
    safety = auto_snapshot(
        prefix_dir, game_name, "pre-restore", keep=0,
        game_dir=game_dir, steam_appid=steam_appid, dest_root=dest_root, extra_dirs=extra_dirs,
    )
    covered: set[str] = set()
    if safety is not None and home_rels:
        with zipfile.ZipFile(safety) as zf:
            covered = {n.split("/", 2)[2] for n in zf.namelist() if n.count("/") >= 2 and n.split("/", 2)[1] == "home"}
    try:
        count = _restore_saves_archive(
            snapshot_zip,
            prefix_dir,
            game_dir,
            extra_dirs=extra_dirs,
            home_overwrite=covered,
            skipped=skipped,
            allow_home=True,
        )
    except Exception as exc:
        raise _wrap_restore_error(exc, safety) from exc
    return count, safety


def snapshot_prefix_state(
    prefix_dir: Path,
    game_name: str,
    reason: str = "state",
    dest_root: Path | None = None,
    max_keep: int = 5,
    max_total_bytes: int = _PREFIX_STATE_MAX_BYTES,
    include_system_dlls: bool = True,
) -> PrefixStateSnapshot | None:
    """Create a rotated ZIP snapshot of prefix .reg files and system DLLs."""
    root = _unwrap_prefix_root(prefix_dir)
    if not root.is_dir() or root.is_symlink():
        return None
    entries = _collect_prefix_state_entries(root, include_system_dlls=include_system_dlls)
    if not entries:
        return None

    dest = dest_root or DEFAULT_SNAPSHOTS_DIR
    safe_slug = _slugify(game_name)
    safe_reason = _slugify(reason, fallback="state")
    snap_dir = dest / safe_slug / "state"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    with _snapshot_dir_lock(snap_dir):
        target_zip = _reserve_snapshot_zip(snap_dir, f"{stamp}-{safe_reason}")
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target_zip.stem}-", suffix=".zip.tmp", dir=str(snap_dir)
        )
        os.close(fd)
        tmp_zip = Path(tmp_name)

        reg_count = 0
        dll_count = 0
        total_uncompressed = 0
        try:
            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for kind, src_file, rel_path in entries:
                    try:
                        st = src_file.stat()
                    except OSError:
                        continue
                    fsize = st.st_size
                    if reg_count + dll_count + 1 > _RESTORE_MAX_ENTRIES:
                        raise RuntimeError(
                            f"Prefix state snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
                        )
                    if fsize > _RESTORE_MAX_FILE_BYTES or total_uncompressed + fsize > max_total_bytes:
                        raise RuntimeError(
                            f"Prefix state snapshot size limit exceeded on {src_file.name}"
                        )
                    try:
                        sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
                    except OSError:
                        continue
                    arcname = f"{kind}/{rel_path.as_posix()}"
                    file_bytes = 0
                    with os.fdopen(sfd, "rb") as fh, zf.open(arcname, "w", force_zip64=needs_zip64(fsize)) as zout:
                        while chunk := fh.read(65536):
                            file_bytes += len(chunk)
                            if (
                                file_bytes > _RESTORE_MAX_FILE_BYTES
                                or total_uncompressed + file_bytes > max_total_bytes
                            ):
                                raise RuntimeError(
                                    f"Prefix state snapshot size limit exceeded on {src_file.name}"
                                )
                            zout.write(chunk)
                    total_uncompressed += file_bytes
                    if kind == "reg":
                        reg_count += 1
                    else:
                        dll_count += 1
            if reg_count + dll_count == 0:
                tmp_zip.unlink(missing_ok=True)
                target_zip.unlink(missing_ok=True)
                return None
            os.replace(tmp_zip, target_zip)
        except BaseException:
            tmp_zip.unlink(missing_ok=True)
            target_zip.unlink(missing_ok=True)
            raise

        _rotate_snapshots(snap_dir, max_keep)
        return PrefixStateSnapshot(
            snapshot_zip=target_zip,
            prefix_dir=prefix_dir,
            reason=reason,
            reg_count=reg_count,
            dll_count=dll_count,
        )


def _commit_staged_files(
    staged: list[tuple[Path, Path]],
    *,
    mtime: float | None = None,
    post_commit: Callable[[], None] | None = None,
) -> None:
    """Validate every target and atomically replace them all, rolling back on any failure."""
    if not staged and post_commit is None:
        return
    for _tmp_path, target in staged:
        if target.is_symlink():
            raise OSError(f"Refusing to overwrite symlink target: {target}")
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Target path is a directory: {target}")

    committed: list[tuple[Path, Path | None]] = []
    remaining_idx = 0
    try:
        for idx, (tmp_path, target) in enumerate(staged):
            remaining_idx = idx
            bak_path: Path | None = None
            if target.exists():
                fd, bak_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".bak.tmp", dir=str(target.parent)
                )
                os.close(fd)
                bak_path = Path(bak_name)
                os.replace(target, bak_path)
            committed.append((target, bak_path))
            os.replace(tmp_path, target)
            remaining_idx = idx + 1
        if post_commit is not None:
            post_commit()
    except BaseException:
        for target, bak_path in reversed(committed):
            try:
                if bak_path is not None and bak_path.exists():
                    os.replace(bak_path, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        for tmp_path, _target in staged[remaining_idx:]:
            tmp_path.unlink(missing_ok=True)
        raise

    for _target, bak_path in committed:
        if bak_path is not None:
            bak_path.unlink(missing_ok=True)
    if mtime is not None:
        for _tmp_path, target in staged:
            try:
                os.utime(target, (mtime, mtime))
            except OSError:
                pass


def restore_prefix_state_snapshot(snapshot_zip: Path, prefix_dir: Path) -> tuple[int, int]:
    """Safely restore .reg and system DLL files from a prefix state snapshot ZIP."""
    if not snapshot_zip.is_file() or snapshot_zip.is_symlink():
        raise FileNotFoundError(f"Prefix state snapshot not found: {snapshot_zip}")

    root, wine_root = _require_safe_wine_root(prefix_dir)
    _, drive_c = _require_safe_drive_c(root)
    wine_root.mkdir(parents=True, exist_ok=True)
    drive_c.mkdir(parents=True, exist_ok=True)
    wine_root_resolved = wine_root.resolve()
    drive_c_resolved = drive_c.resolve()

    staged_by_target: dict[Path, tuple[Path, str]] = {}
    total_bytes = 0
    try:
        with zipfile.ZipFile(snapshot_zip, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > _RESTORE_MAX_ENTRIES:
                raise RuntimeError(
                    f"Prefix state snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
                )
            for info in infolist:
                if info.is_dir():
                    continue
                if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                    continue
                filename = info.filename
                if not filename or filename.startswith("/") or "\\" in filename:
                    continue
                parts = Path(filename).parts
                if any(p in ("..", "", ".") for p in parts):
                    continue
                rel_parts: tuple[str, ...]
                if parts[0] == "reg" and len(parts) == 2 and parts[1].lower().endswith(".reg"):
                    base_dir = wine_root
                    base_resolved = wine_root_resolved
                    rel_parts = (parts[1],)
                    kind = "reg"
                elif parts[0] == "dll" and len(parts) >= 2:
                    base_dir = drive_c
                    base_resolved = drive_c_resolved
                    rel_parts = parts[1:]
                    kind = "dll"
                else:
                    continue

                if info.file_size > _RESTORE_MAX_FILE_BYTES:
                    raise RuntimeError(f"Snapshot file too large: {info.filename}")
                if not _safe_parents_within(base_dir, base_resolved, rel_parts[:-1]):
                    continue

                target = base_dir.joinpath(*rel_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not is_within(target.parent, base_resolved) or target.is_symlink():
                    continue
                if target.exists() and target.is_dir():
                    raise IsADirectoryError(f"Target path is a directory: {target}")

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
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise

                prev = staged_by_target.get(target)
                if prev is not None:
                    prev[0].unlink(missing_ok=True)
                staged_by_target[target] = (tmp_path, kind)

        staged = [(tmp_path, target) for target, (tmp_path, _kind) in staged_by_target.items()]
        _commit_staged_files(staged)
    except BaseException:
        for tmp_path, _kind in staged_by_target.values():
            tmp_path.unlink(missing_ok=True)
        raise

    reg_count = sum(1 for _tmp_path, kind in staged_by_target.values() if kind == "reg")
    dll_count = sum(1 for _tmp_path, kind in staged_by_target.values() if kind == "dll")
    return reg_count, dll_count


def _extract_manifest_reg_bytes(
    prefix_dir: Path,
    game_name: str,
    steam_appid: int | None,
) -> bytes | None:
    """Extract registry saves if a Ludusavi manifest entry defines registry paths."""
    if not game_name and steam_appid is None:
        return None
    entry = find_manifest_game_entry(game_name, steam_appid=steam_appid)
    if entry is None:
        return None
    reg_paths = _manifest_registry_paths(entry)
    if not reg_paths:
        return None
    return _extract_registry_saves(prefix_dir, reg_paths)


def compute_prefix_saves_fingerprint(
    prefix_dir: Path,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
    home_files: tuple[str, ...] = (),
) -> SaveFingerprint:
    """Compute a deterministic fingerprint over all non-empty save files in prefix_dir (and extra_dirs, home_files)."""
    entries = _collect_prefix_save_entries(
        prefix_dir,
        game_name=game_name,
        game_dir=game_dir,
        steam_appid=steam_appid,
        extra_dirs=extra_dirs,
        home_files=home_files,
    )
    reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
    if not entries and not reg_bytes:
        return SaveFingerprint(latest_mtime=0.0, file_count=0, digest="")

    h = hashlib.sha256()
    latest_mtime = 0.0
    file_count = 0
    for section, src_file, rel_path in entries:
        try:
            sfd = open_regular_fd(src_file)
        except OSError:
            continue
        with os.fdopen(sfd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_size == 0:
                continue
            h.update(f"{section}/{rel_path.as_posix()}\0{st.st_size}\0".encode())
            while chunk := fh.read(65536):
                h.update(chunk)
            if file_identity(os.fstat(fh.fileno())) != file_identity(st):
                raise RuntimeError(f"Save changed while calculating its fingerprint: {src_file}")
        latest_mtime = max(latest_mtime, float(st.st_mtime))
        file_count += 1

    if reg_bytes:
        h.update(f"registry/registry.json\0{len(reg_bytes)}\0".encode())
        h.update(reg_bytes)
        wine_root = _resolve_wine_root(prefix_dir)
        for reg_name in ("user.reg", "system.reg", "userdef.reg"):
            rp = wine_root / reg_name
            if rp.is_file() and not rp.is_symlink():
                try:
                    latest_mtime = max(latest_mtime, float(rp.stat().st_mtime))
                except OSError:
                    pass
        file_count += 1

    if file_count == 0:
        return SaveFingerprint(latest_mtime=0.0, file_count=0, digest="")
    return SaveFingerprint(
        latest_mtime=latest_mtime,
        file_count=file_count,
        digest=h.hexdigest(),
    )


_SYNC_BASE_FILE = ".deckinstall_sync_base.json"


def _sync_base_key(sync_root: Path, slug: str) -> str:
    try:
        root = str(sync_root.resolve())
    except OSError:
        root = str(sync_root)
    return f"{root}|{slug}"


def _read_sync_base_pair(prefix_dir: Path, sync_root: Path, slug: str) -> tuple[str, str]:
    """Return (remote_digest, local_digest) last agreed upon by this device."""
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 1_048_576:
            return "", ""
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    value = data.get(_sync_base_key(sync_root, slug)) if isinstance(data, dict) else None
    if isinstance(value, str):
        return value, value
    if isinstance(value, dict):
        rem = value.get("remote")
        loc = value.get("local")
        return (rem if isinstance(rem, str) else ""), (loc if isinstance(loc, str) else "")
    return "", ""


def _link_key(sync_root: Path, game_dir: Path | None, game_name: str) -> str:
    """Which installation a link belongs to: its game folder when known, else its name."""
    try:
        root = str(sync_root.resolve())
    except OSError:
        root = str(sync_root)
    return f"{root}|link|{game_dir if game_dir is not None else game_name}"


def _read_sync_link(prefix_dir: Path, sync_root: Path, game_dir: Path | None, game_name: str) -> tuple[str, str]:
    """(folder slug, save history id) this installation is linked to in sync_root ("" when none)."""
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 1_048_576:
            return "", ""
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    value = data.get(_link_key(sync_root, game_dir, game_name)) if isinstance(data, dict) else None
    if not isinstance(value, dict):
        return "", ""
    slug, save_id = value.get("slug"), value.get("save_id")
    return (slug if isinstance(slug, str) else ""), (save_id if isinstance(save_id, str) else "")


def _record_sync_link(
    prefix_dir: Path, sync_root: Path, game_dir: Path | None, game_name: str, slug: str, save_id: str,
) -> None:
    """Remember which shared folder and history this installation uses (kept when it is renamed)."""
    if not save_id or not prefix_dir.is_dir():
        return
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink():
            return
        data: dict[str, object] = {}
        if state_path.is_file():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except ValueError:
                data = {}
        data[_link_key(sync_root, game_dir, game_name)] = {"slug": slug, "save_id": save_id}
        atomic_write_bytes(state_path, json.dumps(data, indent=2).encode("utf-8"))
    except OSError:
        logging.warning("Could not record the sync link of %s in %s", game_name, prefix_dir, exc_info=True)


def _game_sync_folder(
    sync_root: Path, game_name: str, prefix_dir: Path, game_dir: Path | None,
) -> tuple[str, Path, str]:
    """(slug, folder, linked save id): the linked folder when this installation has one (so a
    renamed game keeps its history), otherwise the folder of its name."""
    linked_slug, save_id = _read_sync_link(prefix_dir, sync_root, game_dir, game_name)
    if linked_slug and "/" not in linked_slug and linked_slug not in (".", "..") and (sync_root / linked_slug).is_dir():
        return linked_slug, sync_root / linked_slug, save_id
    slug, folder = _resolve_game_slug_dir(sync_root, game_name, meta_file="meta.json", name_key="game_name")
    return slug, folder, save_id if slug == linked_slug else ""


def _identity_problem(
    heads: Sequence[repo.Revision], linked_save_id: str, steam_appid: int | None,
) -> str:
    """Why the shared saves may belong to another game ("" when nothing says so)."""
    ids = {h.save_id for h in heads if h.save_id}
    if linked_save_id and ids and linked_save_id not in ids:
        return "The shared saves belong to a different save history (another game with the same name?)"
    appids = {h.steam_appid for h in heads if h.steam_appid}
    if steam_appid and appids and steam_appid not in appids:
        return f"The shared saves were made for Steam AppID {', '.join(map(str, sorted(appids)))}, not {steam_appid}"
    return ""


def _read_sync_base_rev(prefix_dir: Path, sync_root: Path, slug: str) -> str:
    """The shared revision this device last pushed or restored ("" when none)."""
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 1_048_576:
            return ""
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(_sync_base_key(sync_root, slug)) if isinstance(data, dict) else None
    rev = value.get("rev") if isinstance(value, dict) else None
    return rev if isinstance(rev, str) else ""


def _record_sync_base(
    prefix_dir: Path,
    sync_root: Path,
    slug: str,
    digest: str,
    local_digest: str | None = None,
    rev_id: str = "",
) -> None:
    """Remember the digest both sides agreed on; stored locally in the prefix (never synced)."""
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink():
            return
        data: dict[str, object] = {}
        if state_path.is_file():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except ValueError:
                data = {}
        key = _sync_base_key(sync_root, slug)
        data[key] = {"remote": digest, "local": local_digest or digest, "rev": rev_id}
        atomic_write_bytes(state_path, json.dumps(data, indent=2).encode("utf-8"))
    except OSError:
        logging.warning("Could not record sync base for %s in %s", slug, prefix_dir, exc_info=True)


def push_game_saves(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
    *,
    resolve_heads: bool = False,
) -> Path | None:
    """Publish the prefix saves as a new immutable revision in <sync_root>/<slug>/revs/.

    Nothing in the shared folder is ever replaced. With resolve_heads the new revision replaces
    every current version (the user resolved a conflict); otherwise the push refuses, with
    SyncRemoteChanged, when another device published something this device has not seen.
    """
    if not prefix_dir.is_dir() and not extra_dirs:
        return None
    entries = _collect_prefix_save_entries(
        prefix_dir,
        game_name=game_name,
        game_dir=game_dir,
        steam_appid=steam_appid,
        extra_dirs=extra_dirs,
    )
    reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
    if not entries and not reg_bytes:
        return None

    repo.open_sync_root(sync_root)  # never creates a missing (unmounted) folder
    slug, game_sync_dir, linked_save_id = _game_sync_folder(sync_root, game_name, prefix_dir, game_dir)
    game_sync_dir.mkdir(exist_ok=True)
    with repo.game_lock(game_sync_dir), file_lock(game_sync_dir / ".sync.lock"):
        tmp_zip = repo.staging_file(game_sync_dir)
        try:
            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                file_count, total_uncompressed = _write_save_entries(zf, entries, reg_bytes)
                if file_count == 0:
                    zf.close()
                    tmp_zip.unlink(missing_ok=True)
                    return None
                digest, fp_count, _ = repo.save_entries_digest(zf)
                # Decide the parents only now, right before publishing (the folder may have changed).
                state = repo.scan_game_folder(game_sync_dir)
                if not resolve_heads:
                    problem = _identity_problem(state.heads(), linked_save_id, steam_appid)
                    if problem:
                        raise repo.SyncRemoteChanged(problem)
                parents, same = _push_parents(state, prefix_dir, sync_root, slug, digest, resolve_heads)
                if same is not None:  # the shared version already has exactly these saves
                    zf.close()
                    tmp_zip.unlink(missing_ok=True)
                    if prefix_dir.is_dir():
                        _record_sync_base(prefix_dir, sync_root, slug, digest, rev_id=same.rev_id)
                        _record_sync_link(prefix_dir, sync_root, game_dir, game_name, slug,
                                          same.save_id or linked_save_id)
                    return same.path
                rev_id = repo.new_rev_id()
                save_id = (
                    next((state.revisions[p].save_id for p in parents
                          if p in state.revisions and state.revisions[p].save_id), "")
                    or linked_save_id or repo.new_rev_id()
                )
                zf.writestr(repo.REV_MANIFEST, repo.revision_manifest(
                    rev_id, parents, save_id, digest, fp_count, total_uncompressed, game_name, steam_appid,
                ))
            final = repo.publish_revision(game_sync_dir, tmp_zip, rev_id)
        except BaseException:
            tmp_zip.unlink(missing_ok=True)
            raise
        if prefix_dir.is_dir():
            _record_sync_base(prefix_dir, sync_root, slug, digest, rev_id=rev_id)
            _record_sync_link(prefix_dir, sync_root, game_dir, game_name, slug, save_id)
        return final


def _push_parents(
    state: repo.RepoState, prefix_dir: Path, sync_root: Path, slug: str, digest: str, resolve_heads: bool,
) -> tuple[list[str], repo.Revision | None]:
    """Parents of a new revision, or the existing head that already holds these saves.

    Raises SyncRemoteChanged when another device published something the plan did not see:
    pushing would hide it, so the user plans again (a conflict resolution passes resolve_heads).
    """
    heads = state.heads()
    if not heads:
        return [], None
    distinct = state.distinct_heads()
    if len(distinct) == 1 and distinct[0].content_digest == digest:
        return [h.rev_id for h in heads] if len(heads) > 1 else [], distinct[0] if len(heads) == 1 else None
    if resolve_heads:
        return [h.rev_id for h in heads], None
    base_rev = _read_sync_base_rev(prefix_dir, sync_root, slug) if prefix_dir.is_dir() else ""
    base_remote, _ = _read_sync_base_pair(prefix_dir, sync_root, slug) if prefix_dir.is_dir() else ("", "")
    head_ids = {h.rev_id for h in heads}
    if len(distinct) == 1 and (base_rev in head_ids or (not base_rev and base_remote == distinct[0].content_digest)):
        return sorted(head_ids), None
    raise repo.SyncRemoteChanged(
        "The shared saves changed since the last sync on this device; check again before sending"
    )


def _write_save_entries(
    zf: zipfile.ZipFile, entries: list[tuple[str, Path, Path]], reg_bytes: bytes | None,
) -> tuple[int, int]:
    """Write save/<section>/<path> entries (bounded, checked against changes while reading)."""
    file_count = 0
    total_uncompressed = 0
    for section, src_file, rel_path in entries:
        try:
            sfd = open_regular_fd(src_file)
        except OSError:
            continue
        try:
            st = os.fstat(sfd)
            before_id = file_identity(st)
        except OSError:
            os.close(sfd)
            continue
        fsize = st.st_size
        if file_count + 1 > _RESTORE_MAX_ENTRIES:
            os.close(sfd)
            raise RuntimeError(f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
        if fsize > _RESTORE_MAX_FILE_BYTES or total_uncompressed + fsize > _RESTORE_MAX_TOTAL_BYTES:
            os.close(sfd)
            raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
        arcname = f"save/{section}/{rel_path.as_posix()}"
        file_bytes = 0
        with os.fdopen(sfd, "rb") as fh, zf.open(arcname, "w", force_zip64=needs_zip64(fsize)) as zout:
            while chunk := fh.read(65536):
                file_bytes += len(chunk)
                if (
                    file_bytes > _RESTORE_MAX_FILE_BYTES
                    or total_uncompressed + file_bytes > _RESTORE_MAX_TOTAL_BYTES
                ):
                    raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
                zout.write(chunk)
            after_id = file_identity(os.fstat(fh.fileno()))
            if after_id != before_id or file_bytes != fsize:
                raise RuntimeError(f"Save file changed while syncing (is the game running?): {src_file.name}")
        total_uncompressed += file_bytes
        file_count += 1
    if reg_bytes:
        if file_count + 1 > _RESTORE_MAX_ENTRIES:
            raise RuntimeError(f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
        if len(reg_bytes) > _RESTORE_MAX_FILE_BYTES or total_uncompressed + len(reg_bytes) > _RESTORE_MAX_TOTAL_BYTES:
            raise RuntimeError("Backup size limit exceeded on registry.json")
        zf.writestr("save/registry/registry.json", reg_bytes)
        total_uncompressed += len(reg_bytes)
        file_count += 1
    return file_count, total_uncompressed


def _resolve_drive_c_target(prefix_dir: Path) -> Path:
    """Return the drive_c directory for extraction, supporting Proton pfx/drive_c or drive_c."""
    root = _unwrap_prefix_root(prefix_dir)
    pfx_dir = root / "pfx"
    if pfx_dir.is_symlink():
        try:
            # Our self-link pfx -> . stores files directly in root/drive_c.
            if pfx_dir.resolve() == root.resolve():
                return root / "drive_c"
        except OSError:
            pass
        return pfx_dir / "drive_c"  # external symlink: _safe_parents_within rejects it safely
    pfx_drive_c = pfx_dir / "drive_c"
    if pfx_drive_c.is_dir() or pfx_dir.is_dir():
        return pfx_drive_c
    return root / "drive_c"


def _parse_sync_zip_entry(
    filename: str,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Extract (section, rel_parts) for valid users/, ProgramData/, game/, or registry/ entries."""
    del game_dir, steam_appid
    if not filename or filename.startswith("/") or "\\" in filename:
        return None
    parts = Path(filename).parts
    if any(p in ("..", "", ".") for p in parts):
        return None
    valid_sections = ("users", "ProgramData", "game", "registry", "home", *EXTRA_SECTIONS)
    if len(parts) >= 2 and parts[0] in valid_sections:
        found = parts[0], parts[1:]
    elif len(parts) >= 3 and parts[1] in valid_sections:
        found = parts[1], parts[2:]
    else:
        return None
    if found[0] == "home" and not home_entry_allowed(found[1]):
        return None
    return found


def pull_game_saves(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    snapshots_root: Path | None = None,
    allow_unverified: bool = False,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    *,
    max_keep: int = SNAPSHOT_KEEP_DEFAULT,
    allow_home: bool = False,
    extra_dirs: ExtraDirs | None = None,
    rev_id: str = "",
) -> tuple[int, Path | None]:
    """Verify a private copy of the shared version, snapshot local saves, then restore that copy.

    rev_id picks one version when several conflict; without it there must be exactly one.
    """
    repo.open_sync_root(sync_root)
    slug, game_sync_dir, _linked = _game_sync_folder(sync_root, game_name, prefix_dir, game_dir)
    if not game_sync_dir.is_dir() or game_sync_dir.is_symlink():
        return 0, None
    with repo.game_lock(game_sync_dir):
        state = repo.scan_game_folder(game_sync_dir)
        distinct = state.distinct_heads()
        if not distinct:
            legacy = game_sync_dir / repo.LEGACY_ZIP
            if state.waiting and not state.legacy_unverified:
                raise RuntimeError(
                    f"Sync archive checksum mismatch for {game_name}: the shared version is incomplete "
                    "or damaged (still being copied?); nothing was restored"
                )
            if state.legacy_unverified and not allow_unverified:
                raise RuntimeError(f"Missing sync metadata or checksum for {game_name}")
            if not (state.legacy_unverified and legacy.is_file() and not legacy.is_symlink()):
                return 0, None
            source = repo.Revision(
                rev_id="", path=legacy, parents=(), save_id="", content_digest="", file_count=0,
                uncompressed_bytes=0, created_at=0.0, legacy=True,
            )
            staging = repo.local_staging_dir()
            staging.mkdir(parents=True, exist_ok=True)
            fd, copy_name = tempfile.mkstemp(prefix="unverified-", suffix=".zip", dir=str(staging))
            os.close(fd)
            copy = Path(copy_name)
            try:
                shutil_copy(legacy, copy)
            except BaseException:
                copy.unlink(missing_ok=True)
                raise
        else:
            if rev_id:
                source_opt = state.revisions.get(rev_id)
                if source_opt is None or source_opt.rev_id not in {h.rev_id for h in state.heads()}:
                    raise repo.SyncRemoteChanged("The chosen shared version is no longer current; check again")
                source = source_opt
            elif len(distinct) == 1:
                source = distinct[0]
            else:
                raise repo.SyncRemoteChanged("There are several shared versions of these saves; choose one")
            try:
                copy = repo.verified_local_copy(source, repo.local_staging_dir())
            except repo.RevisionInvalid as exc:
                raise RuntimeError(
                    f"Sync archive checksum mismatch for {game_name}: the shared version is incomplete "
                    f"or damaged ({exc}); it was not restored"
                ) from exc
        try:
            snap_path = snapshot_prefix_saves(
                prefix_dir,
                game_name,
                "pre-sync-pull",
                dest_root=snapshots_root,
                max_keep=max_keep,
                game_dir=game_dir,
                steam_appid=steam_appid,
                extra_dirs=extra_dirs,
            )
            try:
                restored_files = _restore_saves_archive(
                    copy,
                    prefix_dir,
                    game_dir,
                    source.created_at or None,
                    extra_dirs=extra_dirs,
                    allow_home=allow_home,
                )
            except Exception as exc:
                raise _wrap_restore_error(exc, snap_path) from exc
        finally:
            copy.unlink(missing_ok=True)

        if source.content_digest and prefix_dir.is_dir():
            post_fp = compute_prefix_saves_fingerprint(
                prefix_dir,
                game_name=game_name,
                game_dir=game_dir,
                steam_appid=steam_appid,
                extra_dirs=extra_dirs,
            )
            _record_sync_base(
                prefix_dir,
                sync_root,
                slug,
                source.content_digest,
                local_digest=post_fp.digest or source.content_digest,
                rev_id=source.rev_id,
            )
            _record_sync_link(prefix_dir, sync_root, game_dir, game_name, slug, source.save_id)
    return restored_files, snap_path


def shutil_copy(src: Path, dst: Path) -> None:
    import shutil

    shutil.copyfile(src, dst)


def _resolve_existing_rel_parts(
    drive_c: Path,
    section: str,
    rel_parts: tuple[str, ...],
    target_user: str,
) -> tuple[str, ...]:
    """Remap non-Public users onto target_user if absent and reuse existing Wine path case."""
    adj_rel = rel_parts
    if section == "users" and len(rel_parts) >= 2 and rel_parts[0].lower() != "public":
        users_dir = _find_child_ci(drive_c, "users")
        if users_dir is None or _find_child_ci(users_dir, rel_parts[0].lower()) is None:
            adj_rel = (target_user, *rel_parts[1:])
    raw_parts = (section, *adj_rel)
    cur = drive_c
    out: list[str] = []
    for part in raw_parts[:-1]:
        exact = cur / part
        if exact.is_dir() and not exact.is_symlink():
            chosen = part
        else:
            found = _find_child_ci(cur, part.lower())
            chosen = found.name if found is not None else part
        out.append(chosen)
        cur = cur / chosen
    leaf = raw_parts[-1]
    exact_leaf = cur / leaf
    if not (exact_leaf.exists() or exact_leaf.is_symlink()) and cur.is_dir() and not cur.is_symlink():
        leaf_low = leaf.lower()
        try:
            for child in sorted(cur.iterdir()):
                if not child.is_symlink() and child.is_file() and child.name.lower() == leaf_low:
                    leaf = child.name
                    break
        except OSError:
            pass
    out.append(leaf)
    return tuple(out)


def restored_snapshot_matches(
    archive_path: Path,
    prefix_dir: Path,
    game_dir: Path | None,
    *,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> bool:
    """Verify that every file in this save snapshot exists at its restore destination."""
    try:
        root = _home_root().resolve()
        if (
            archive_path.absolute() != archive_path.resolve()
            or not archive_path.is_relative_to(root)
            or not archive_path.is_file()
        ):
            return False
        if archive_has_steamcloud(archive_path):
            if steam_appid is None or snapshot_steamcloud_appid(archive_path) != str(steam_appid):
                return False
        return _restore_saves_archive(
            archive_path, prefix_dir, game_dir, extra_dirs=extra_dirs,
            allow_home=True, verify_only=True,
        ) > 0
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        return False


def remove_restored_snapshot(
    archive_path: Path,
    prefix_dir: Path,
    game_dir: Path | None,
    *,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> bool:
    """Remove the source snapshot only if its contents still match the installed saves."""
    try:
        before = file_identity(archive_path.lstat())
        with _snapshot_dir_lock(archive_path.parent):
            if not restored_snapshot_matches(
                archive_path, prefix_dir, game_dir, steam_appid=steam_appid, extra_dirs=extra_dirs,
            ) or file_identity(archive_path.lstat()) != before:
                return False
            archive_path.unlink()
            return True
    except OSError:
        return False


def _restore_saves_archive(
    archive_path: Path,
    prefix_dir: Path,
    game_dir: Path | None,
    remote_updated_at: float | None = None,
    extra_dirs: ExtraDirs | None = None,
    home_overwrite: set[str] | None = None,
    skipped: list[str] | None = None,
    *,
    allow_home: bool = False,
    verify_only: bool = False,
) -> int:
    """Extract bounded save entries into explicitly selected prefix, game and extra roots.

    Existing files under $HOME are replaced only when allow_home is True and the file is listed
    in home_overwrite (held by a safety snapshot); the others are kept and appended to skipped.
    """
    root_prefix = _unwrap_prefix_root(prefix_dir)
    extras = {name: root for name, root in (extra_dirs or ()) if name in EXTRA_SECTIONS}
    has_prefix = root_prefix.is_dir() or not extras  # an extras-only game (no prefix) never gets one created
    if has_prefix and not root_prefix.is_dir():  # nor a native game whose archive holds only home files
        with zipfile.ZipFile(archive_path, "r") as zf_scan:
            sections = {p[0] for n in zf_scan.namelist()[:_RESTORE_MAX_ENTRIES] if (p := _parse_sync_zip_entry(n))}
        has_prefix = not sections or bool(sections - {"home"})

    if has_prefix:
        if root_prefix.is_symlink():
            raise OSError(f"Unsafe target drive_c path: {prefix_dir}")
        drive_c = _resolve_drive_c_target(root_prefix)
        try:
            root_resolved = root_prefix.resolve()
            drive_c_rel = drive_c.relative_to(root_prefix).parts
        except (OSError, ValueError) as exc:
            raise OSError(f"Unsafe target drive_c path: {drive_c}") from exc
        if not _safe_parents_within(root_prefix, root_resolved, drive_c_rel):
            raise OSError(f"Unsafe target drive_c path: {drive_c}")
        if not verify_only:
            drive_c.mkdir(parents=True, exist_ok=True)
        drive_c_resolved = drive_c.resolve()
        target_user = _detect_prefix_user(drive_c)
    else:
        drive_c = _resolve_drive_c_target(root_prefix)
        drive_c_resolved = drive_c.resolve() if drive_c.exists() else drive_c
        target_user = "steamuser"

    if game_dir is not None and not game_dir.is_symlink() and game_dir.is_dir():
        game_dir_resolved = game_dir.resolve()
    else:
        game_dir_resolved = None

    staged_by_target: dict[Path, Path] = {}
    reg_payloads: list[bytes] = []
    reg_restored = 0
    total_bytes = 0
    expected_files = 0
    verified_targets: set[Path] = set()

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > _RESTORE_MAX_ENTRIES:
                raise RuntimeError(
                    f"Sync archive exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
                )
            if sum(max(0, info.file_size) for info in infolist) > _RESTORE_MAX_TOTAL_BYTES:
                raise RuntimeError(
                    f"Sync archive exceeds maximum uncompressed size ({_RESTORE_MAX_TOTAL_BYTES})"
                )
            for info in infolist:
                if info.is_dir():
                    continue
                expected_files += 1
                # Block symlink entries stored in POSIX zip attributes.
                if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                    continue
                parsed = _parse_sync_zip_entry(info.filename, game_dir=game_dir)
                if parsed is None:
                    continue
                section, rel_parts = parsed
                if section != "registry" and restore_entry_blocked(rel_parts, info.external_attr):
                    continue
                if info.file_size > _RESTORE_MAX_FILE_BYTES:
                    raise RuntimeError(f"Sync file too large: {info.filename}")

                if section == "registry":
                    if verify_only:
                        return 0
                    if rel_parts != ("registry.json",):
                        continue
                    if root_prefix.is_symlink():
                        raise OSError(f"Unsafe target wine root: {prefix_dir}")
                    chunks: list[bytes] = []
                    copied = 0
                    with zf.open(info, "r") as src_fh:
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
                            chunks.append(chunk)
                    raw_reg = b"".join(chunks)
                    try:
                        json.loads(raw_reg.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"Invalid registry save JSON: {exc}") from exc
                    reg_payloads.append(raw_reg)
                    continue

                if section == "game":
                    if game_dir is None or game_dir_resolved is None:
                        continue
                    base_dir = game_dir
                    base_resolved = game_dir_resolved
                    check_parts = rel_parts[:-1]
                    target = game_dir.joinpath(*rel_parts)
                elif section == "home":
                    if not allow_home:
                        continue
                    # Native games' files under $HOME (from Ludusavi): allowed save folders only, and an
                    # existing file is replaced only when the safety snapshot holds it.
                    if not home_entry_allowed(rel_parts, info.external_attr >> 16):
                        continue
                    home_root = _home_root()
                    base_dir = home_root
                    base_resolved = home_root.resolve()
                    check_parts = rel_parts[:-1]
                    target = home_root.joinpath(*rel_parts)
                    rel = "/".join(rel_parts)
                    covered = target.is_file() and rel in (home_overwrite or ())
                    if target.is_symlink() or (not verify_only and target.exists() and not covered):
                        if skipped is not None:
                            skipped.append(rel)
                        continue
                elif section in EXTRA_SECTIONS:
                    extra_root = extras.get(section)
                    if extra_root is None or extra_root.is_symlink():
                        continue
                    if not verify_only:
                        extra_root.mkdir(parents=True, exist_ok=True)
                    base_dir = extra_root
                    base_resolved = extra_root.resolve()
                    check_parts = rel_parts[:-1]
                    target = extra_root.joinpath(*rel_parts)
                elif not has_prefix:
                    continue
                else:
                    base_dir = drive_c
                    base_resolved = drive_c_resolved
                    resolved_parts = _resolve_existing_rel_parts(drive_c, section, rel_parts, target_user)
                    check_parts = resolved_parts[:-1]
                    target = drive_c.joinpath(*resolved_parts)

                if not _safe_parents_within(base_dir, base_resolved, check_parts):
                    continue

                if verify_only:
                    if target in verified_targets or target == archive_path:
                        return 0
                    if not _zip_member_matches_file(zf, info, target):
                        return 0
                    verified_targets.add(target)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                if not is_within(target.parent, base_resolved) or target.is_symlink():
                    continue
                if target.exists() and target.is_dir():
                    raise IsADirectoryError(f"Target path is a directory: {target}")

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
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise

                prev_tmp = staged_by_target.get(target)
                if prev_tmp is not None:
                    prev_tmp.unlink(missing_ok=True)
                staged_by_target[target] = tmp_path

        if verify_only:
            return len(verified_targets) if len(verified_targets) == expected_files else 0

        def _apply_regs() -> None:
            nonlocal reg_restored
            if not reg_payloads:
                return
            _, wine_root = _require_safe_wine_root(root_prefix)
            reg_backups: dict[Path, bytes | None] = {}
            for reg_name in ("user.reg", "system.reg", "userdef.reg"):
                rp = wine_root / reg_name
                if not rp.is_symlink():
                    reg_backups[rp] = rp.read_bytes() if rp.is_file() else None
            try:
                for raw_reg in reg_payloads:
                    if _restore_registry_saves(root_prefix, raw_reg) > 0:
                        reg_restored += 1
            except BaseException:
                for rp, orig_bytes in reg_backups.items():
                    try:
                        if orig_bytes is None:
                            rp.unlink(missing_ok=True)
                        else:
                            atomic_write_bytes(rp, orig_bytes)
                    except OSError:
                        pass
                raise

        staged = [(tmp_path, target) for target, tmp_path in staged_by_target.items()]
        _commit_staged_files(
            staged,
            mtime=remote_updated_at,
            post_commit=_apply_regs if reg_payloads else None,
        )
    except BaseException:
        for tmp_path in staged_by_target.values():
            tmp_path.unlink(missing_ok=True)
        raise

    return len(staged_by_target) + reg_restored


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


def _save_file_nonempty(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_size > 0
    except OSError:
        return False


def _sync_root_problem(sync_root: Path) -> str:
    """Why the sync folder cannot be used now ("" when it can; a new folder is set up on push)."""
    try:
        repo.open_sync_root(sync_root, initialize=False)
    except repo.SyncFolderUnavailable as exc:
        if repo.is_new_sync_root(sync_root):
            return ""  # set up by the first push
        return str(exc)
    return ""


def plan_sync_all(
    games: Sequence[tuple[str, Path] | SyncGameTarget],
    sync_root: Path,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> list[SyncPlanItem]:
    """Compare this device's saves with the shared versions (read only: nothing is written)."""
    plan: list[SyncPlanItem] = []
    unavailable = _sync_root_problem(sync_root)
    for item in games:
        if isinstance(item, SyncGameTarget):
            game_name = item.game_name
            prefix_dir = item.prefix_dir
            item_game_dir = item.game_dir if item.game_dir is not None else game_dir
            item_appid = item.steam_appid if item.steam_appid is not None else steam_appid
            item_extra = item.extra_dirs or extra_dirs
            item_source = item.source
        else:
            game_name, prefix_dir = item
            item_game_dir = game_dir
            item_appid = steam_appid
            item_extra = extra_dirs
            item_source = "deckinstall"

        slug, game_sync_dir, linked_save_id = _game_sync_folder(sync_root, game_name, prefix_dir, item_game_dir)
        common = {
            "game_name": game_name,
            "slug": slug,
            "prefix_dir": prefix_dir,
            "game_dir": item_game_dir,
            "steam_appid": item_appid,
            "extra_dirs": tuple(item_extra or ()),
            "source": item_source,
        }
        if unavailable:
            plan.append(SyncPlanItem(action="UNAVAILABLE", reason=unavailable, **common))  # type: ignore[arg-type]
            continue
        state = repo.scan_game_folder(game_sync_dir) if game_sync_dir.is_dir() else repo.RepoState()
        distinct = state.distinct_heads()
        common["heads"] = tuple(distinct)
        common["waiting"] = len(state.waiting)
        has_remote = bool(distinct)
        local_fp = None
        if has_remote:
            local_fp = compute_prefix_saves_fingerprint(
                prefix_dir, game_name=game_name, game_dir=item_game_dir, steam_appid=item_appid,
                extra_dirs=item_extra,
            )
            has_local = local_fp.file_count > 0
        else:
            entries = _collect_prefix_save_entries(
                prefix_dir, game_name, item_game_dir, item_appid, item_extra,
            )
            has_local = any(_save_file_nonempty(path) for _, path, _ in entries) or bool(
                _extract_manifest_reg_bytes(prefix_dir, game_name, item_appid)
            )
        action, reason = _plan_action(state, distinct, has_local, local_fp, prefix_dir, sync_root, slug)
        problem = _identity_problem(state.heads(), linked_save_id, item_appid) if distinct else ""
        if problem and action != "IN_SYNC":  # never pull or push across different games silently
            action, reason = "CONFLICT", problem
        if state.waiting and action in ("IN_SYNC", "PUSH", "NO_SAVES"):
            reason += f" ({len(state.waiting)} shared file(s) still arriving or damaged; not used)"
        plan.append(SyncPlanItem(action=action, reason=reason, **common))  # type: ignore[arg-type]
    return plan


def _ancestry_still_arriving(state: repo.RepoState, ids: Iterable[str]) -> bool:
    """True when a revision names a parent that is not here yet (and can still arrive).

    A legacy parent (an old latest.zip that an older DeckInstall has since rewritten) never
    comes back, so it is not "arriving".
    """
    return any(i not in state.revisions and not i.startswith(repo.LEGACY_PREFIX) for i in ids)


def _plan_action(
    state: repo.RepoState,
    distinct: list[repo.Revision],
    has_local: bool,
    local_fp: SaveFingerprint | None,
    prefix_dir: Path,
    sync_root: Path,
    slug: str,
) -> tuple[str, str]:
    """Three-way decision between this device's saves, the shared versions and the last sync."""
    if not distinct:
        if state.legacy_unverified:
            return "CONFLICT", "Remote metadata missing or unreadable (the old archive could not be verified)"
        if has_local:
            return "PUSH", "Local saves present; no remote sync archive yet"
        return "NO_SAVES", "No local or remote saves found"
    if len(distinct) > 1:
        if _ancestry_still_arriving(state, [p for h in distinct for p in h.parents]):
            return "CONFLICT", "Older shared versions are still arriving; wait for the transfer to finish (or choose)"
        return "CONFLICT", f"{len(distinct)} different shared versions (made offline on different devices)"
    head = distinct[0]
    if not has_local:
        return "PULL", "Remote sync archive available; no local saves"
    assert local_fp is not None
    if local_fp.digest == head.content_digest:
        return "IN_SYNC", "Local and remote save digests match"
    base_rev = _read_sync_base_rev(prefix_dir, sync_root, slug)
    base_remote, base_local = _read_sync_base_pair(prefix_dir, sync_root, slug)
    head_ids = {h.rev_id for h in state.heads()}
    local_unchanged = bool(base_local) and local_fp.digest == base_local
    if base_rev in head_ids or (not base_rev and base_remote and base_remote == head.content_digest):
        if local_unchanged:
            return "IN_SYNC", "Local and remote save digests match"
        return "PUSH", "Only local saves changed since last sync"
    ancestors = state.ancestors(head.rev_id)
    known_ancestor = (base_rev and base_rev in ancestors) or (
        not base_rev and base_remote
        and any(state.revisions[a].content_digest == base_remote for a in ancestors if a in state.revisions)
    )
    if known_ancestor:
        if local_unchanged:
            return "PULL", "Only remote saves changed since last sync"
        return "CONFLICT", "Both local and remote saves changed since last sync"
    if not base_rev and not base_remote:
        return "CONFLICT", "No previous sync recorded on this device; choose which saves to keep"
    if _ancestry_still_arriving(state, ancestors):
        return "CONFLICT", "Older shared versions are still arriving; wait for the transfer to finish (or choose)"
    return "CONFLICT", "The shared saves do not continue the version this device synced last"


def preview_sync_operation(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    action: str | None = None,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    extra_dirs: ExtraDirs | None = None,
) -> SyncDryRunPreview:
    """Build a dry-run preview of files and registry entries for a sync operation."""
    slug, _ = _resolve_game_slug_dir(sync_root, game_name, meta_file="meta.json", name_key="game_name")
    if action:
        effective_action = action.strip().upper()
        reason = f"Requested {effective_action}"
    else:
        target = SyncGameTarget(
            game_name,
            prefix_dir,
            game_dir=game_dir,
            steam_appid=steam_appid,
            extra_dirs=tuple(extra_dirs or ()),
        )
        planned = plan_sync_all([target], sync_root)[0]
        effective_action = planned.action
        reason = planned.reason

    files: list[SyncDryRunFile] = []
    total_bytes = 0
    registry_entries = 0

    if effective_action == "PUSH":
        entries = _collect_prefix_save_entries(
            prefix_dir,
            game_name=game_name,
            game_dir=game_dir,
            steam_appid=steam_appid,
            extra_dirs=extra_dirs,
        )
        for section, src_file, rel_path in entries:
            try:
                fsize = src_file.stat().st_size
            except OSError:
                continue
            if fsize == 0:
                continue
            files.append(
                SyncDryRunFile(
                    rel_path=f"{section}/{rel_path.as_posix()}",
                    size_bytes=fsize,
                    operation="PUSH",
                )
            )
            total_bytes += fsize
        reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
        if reg_bytes:
            try:
                reg_payload = json.loads(reg_bytes.decode("utf-8"))
                registry_entries = len(reg_payload.get("entries", []))
            except (ValueError, TypeError):
                registry_entries = 1
            files.append(
                SyncDryRunFile(
                    rel_path="registry/registry.json",
                    size_bytes=len(reg_bytes),
                    operation="PUSH",
                )
            )
            total_bytes += len(reg_bytes)
    elif effective_action == "PULL":
        _, preview_dir = _resolve_game_slug_dir(sync_root, game_name, meta_file="meta.json", name_key="game_name")
        preview_heads = repo.scan_game_folder(preview_dir).distinct_heads() if preview_dir.is_dir() else []
        latest_zip = preview_heads[-1].path if preview_heads else preview_dir / "latest.zip"
        if latest_zip.is_file() and not latest_zip.is_symlink():
            with zipfile.ZipFile(latest_zip, "r") as zf:
                infos = zf.infolist()
                if len(infos) > _RESTORE_MAX_ENTRIES:
                    raise ValueError(
                        f"Sync archive exceeds maximum entry count ({len(infos)} > {_RESTORE_MAX_ENTRIES})"
                    )
                for info in infos:
                    if info.is_dir():
                        continue
                    if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                        continue
                    parsed = _parse_sync_zip_entry(
                        info.filename, game_dir=game_dir, steam_appid=steam_appid
                    )
                    if parsed is None:
                        continue
                    section, rel_parts = parsed
                    if section == "game" and game_dir is None:
                        continue
                    if section == "registry" and rel_parts == ("registry.json",):
                        if info.file_size <= _RESTORE_MAX_FILE_BYTES:
                            try:
                                reg_data = json.loads(zf.read(info).decode("utf-8", errors="replace"))
                                registry_entries = (
                                    len(reg_data.get("entries", []))
                                    if isinstance(reg_data, dict)
                                    else 1
                                )
                            except (ValueError, TypeError):
                                registry_entries = 1
                        else:
                            registry_entries = 1
                    rel_str = f"{section}/{'/'.join(rel_parts)}"
                    files.append(
                        SyncDryRunFile(
                            rel_path=rel_str,
                            size_bytes=info.file_size,
                            operation="PULL",
                        )
                    )
                    total_bytes += info.file_size

    return SyncDryRunPreview(
        game_name=game_name,
        slug=slug,
        action=effective_action,
        reason=reason,
        files=files,
        total_bytes=total_bytes,
        registry_entries=registry_entries,
    )


@dataclass(frozen=True)
class SaveArchiveEntry:
    """Discovered backup or snapshot ZIP archive."""

    path: Path
    kind: str  # "backup" | "save_snapshot" | "state_snapshot"
    label: str
    game_slug: str
    mtime: float
    size_bytes: int


def list_save_archives(
    saves_dir: Path | None = None,
    legacy_backups_dir: Path | None = None,
) -> list[SaveArchiveEntry]:
    """List backup and snapshot ZIP archives across current and legacy save folders."""
    base_saves = saves_dir or DEFAULT_SAVES_DIR
    legacy_base = legacy_backups_dir or (Path.home() / "Backups" / "DeckInstall")
    seen: set[Path] = set()
    entries: list[SaveArchiveEntry] = []

    def _add_entry(path: Path, kind: str, label: str, game_slug: str) -> None:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".zip":
            return
        try:
            resolved = path.resolve()
            if resolved in seen:
                return
            st = path.stat()
        except OSError:
            return
        seen.add(resolved)
        entries.append(
            SaveArchiveEntry(
                path=path,
                kind=kind,
                label=label,
                game_slug=game_slug,
                mtime=st.st_mtime,
                size_bytes=st.st_size,
            )
        )

    for bdir in (base_saves / "backups", legacy_base):
        if not bdir.is_dir() or bdir.is_symlink():
            continue
        try:
            for child in bdir.iterdir():
                _add_entry(child, "backup", child.name, "")
        except OSError:
            continue

    snapshots_root = base_saves / "snapshots" if saves_dir is not None else DEFAULT_SNAPSHOTS_DIR
    for sroot in (snapshots_root, legacy_base / "snapshots"):
        if not sroot.is_dir() or sroot.is_symlink():
            continue
        try:
            game_dirs = list(sroot.iterdir())
        except OSError:
            continue
        for gdir in game_dirs:
            if gdir.is_symlink() or not gdir.is_dir():
                continue
            slug = gdir.name
            try:
                for child in gdir.iterdir():
                    if child.is_symlink():
                        continue
                    if child.is_file():
                        _add_entry(child, "save_snapshot", f"{slug} — {child.stem}", slug)
                    elif child.is_dir() and child.name == "state":
                        for st_zip in child.iterdir():
                            _add_entry(st_zip, "state_snapshot", f"{slug} — {st_zip.stem}", slug)
            except OSError:
                continue

    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries
