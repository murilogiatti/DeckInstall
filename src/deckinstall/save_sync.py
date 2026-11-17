"""Pre-operation save snapshots and folder-based save synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .steam_vdf import (
    _RESTORE_MAX_ENTRIES,
    _RESTORE_MAX_FILE_BYTES,
    _RESTORE_MAX_TOTAL_BYTES,
    DEFAULT_SAVES_DIR,
    _iter_prefix_save_pairs,
    _iter_programdata_save_pairs,
    _norm_key,
    _prefix_programdata_dir,
    _prefix_users_dir,
    _safe_parents_within,
    _write_prefix_saves_to_zip,
)

DEFAULT_SNAPSHOTS_DIR = DEFAULT_SAVES_DIR / "snapshots"
DEFAULT_SYNC_DIR = DEFAULT_SAVES_DIR / "sync"
DEFAULT_LUDUSAVI_MANIFEST_PATH = (
    Path.home() / ".local" / "share" / "deckinstall" / "ludusavi_manifest.json"
)

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
class SyncPlanItem:
    """Planned synchronization action for a single game prefix."""

    game_name: str
    slug: str
    prefix_dir: Path
    action: str  # "PUSH", "PULL", "IN_SYNC", "CONFLICT", "NO_SAVES"
    reason: str


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
    """Return a filesystem-safe lowercase slug."""
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


def _is_within_root(path: Path, root_resolved: Path) -> bool:
    """Return True if path resolves inside root_resolved."""
    try:
        return path.resolve().is_relative_to(root_resolved)
    except OSError:
        return False


def _resolve_wine_root(prefix_dir: Path) -> Path:
    """Return the Wine prefix root containing .reg files (pfx/ or prefix_dir)."""
    pfx_dir = prefix_dir / "pfx"
    if pfx_dir.is_dir() and not pfx_dir.is_symlink():
        return pfx_dir
    return prefix_dir


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
    if not prefix_dir.is_dir() or prefix_dir.is_symlink():
        return []
    entries: list[tuple[str, Path, Path]] = []
    wine_root = _resolve_wine_root(prefix_dir)
    if wine_root.is_dir() and not wine_root.is_symlink():
        try:
            for item in sorted(wine_root.iterdir()):
                if not item.is_symlink() and item.is_file() and item.suffix.lower() == ".reg":
                    entries.append(("reg", item, Path(item.name)))
        except OSError:
            pass

    drive_c = _resolve_drive_c_target(prefix_dir)
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


def load_ludusavi_manifest(
    manifest_path: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Load a Ludusavi JSON manifest mapping game names to save/registry definitions."""
    target = manifest_path if manifest_path is not None else DEFAULT_LUDUSAVI_MANIFEST_PATH
    if not target.is_file() or target.is_symlink():
        return {}
    try:
        if target.stat().st_size > _LUDUSAVI_MANIFEST_MAX_BYTES:
            return {}
        raw = json.loads(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def find_manifest_game_entry(
    game_name: str,
    steam_appid: int | None = None,
    manifest: dict[str, dict[str, object]] | None = None,
) -> dict[str, object] | None:
    """Find a Ludusavi manifest entry by Steam AppID, exact name, or normalized name."""
    loaded = manifest if manifest is not None else load_ludusavi_manifest()
    if not loaded:
        return None

    if steam_appid is not None:
        target_id = str(steam_appid)
        for entry in loaded.values():
            steam_info = entry.get("steam")
            if isinstance(steam_info, dict) and str(steam_info.get("id", "")).strip() == target_id:
                return entry

    if game_name in loaded:
        return loaded[game_name]

    target_key = _norm_key(game_name)
    if target_key:
        for key, entry in loaded.items():
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


def _resolve_manifest_save_paths(
    prefix_dir: Path,
    entry: dict[str, object],
    game_dir: Path | None = None,
) -> tuple[list[tuple[str, Path, Path]], list[str]]:
    """Resolve Ludusavi manifest files and registry paths inside prefix_dir/game_dir."""
    file_entries: list[tuple[str, Path, Path]] = []
    seen_files: set[tuple[str, str]] = set()

    drive_c = _resolve_drive_c_target(prefix_dir)
    users_dir = _prefix_users_dir(prefix_dir)
    user_dirs: list[Path] = []
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
                if not section_root.is_dir() or section_root.is_symlink():
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
                    if cand.is_symlink() or not _is_within_root(cand, section_root_resolved):
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
                                    or not _is_within_root(src_file, section_root_resolved)
                                ):
                                    continue
                                rel = src_file.relative_to(section_root)
                                key = (section, rel.as_posix())
                                if key not in seen_files:
                                    seen_files.add(key)
                                    file_entries.append((section, src_file, rel))

    reg_paths: list[str] = []
    reg_spec = entry.get("registry")
    if isinstance(reg_spec, dict):
        for raw_reg, spec in reg_spec.items():
            if _manifest_entry_applies(spec) and str(raw_reg).strip():
                reg_paths.append(str(raw_reg).strip())

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
            rbracket = stripped.index("]")
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
            if stripped.startswith("#") or stripped.startswith(";"):
                continue
            cur_lines.append(line)
            continuing = line.endswith("\\")

    if cur_subkey is not None:
        sections.append((cur_subkey, cur_header, cur_lines))
    return sections


def _extract_registry_saves(prefix_dir: Path, reg_paths: list[str]) -> bytes | None:
    """Extract matching Wine registry keys from prefix_dir as UTF-8 JSON bytes."""
    if not reg_paths or not prefix_dir.is_dir():
        return None
    wine_root = _resolve_wine_root(prefix_dir)
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
            if not val_lines:
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
                        "lines": val_lines,
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

    wine_root = _resolve_wine_root(prefix_dir)
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
        lines = [
            ln
            for ln in raw_lines
            if isinstance(ln, str) and "\n" not in ln and "\r" not in ln and "\0" not in ln
        ]
        if lines:
            by_file.setdefault(reg_filename, []).append((subkey, lines))

    restored_count = 0
    stamp = int(time.time())
    for reg_filename, items in by_file.items():
        reg_file = wine_root / reg_filename
        if reg_file.is_symlink():
            continue
        existing_text = ""
        if reg_file.is_file():
            try:
                existing_text = reg_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
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
            if subkey.lower() in replaced_keys:
                continue
            escaped_subkey = subkey.replace("\\", "\\\\")
            out_lines.append(f"[{escaped_subkey}] {stamp}")
            out_lines.extend(new_lines)
            out_lines.append("")
            restored_count += 1

        _atomic_write_bytes(reg_file, ("\n".join(out_lines).rstrip() + "\n").encode("utf-8"))

    return restored_count


def _collect_prefix_save_entries(
    prefix_dir: Path,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> list[tuple[str, Path, Path]]:
    """Collect sorted (section, src_file, rel_path) tuples for save files in prefix_dir."""
    if not prefix_dir.is_dir():
        return []
    entries: list[tuple[str, Path, Path]] = []
    seen: set[tuple[str, str]] = set()
    src_users = _prefix_users_dir(prefix_dir)
    if src_users is not None:
        dummy_users = prefix_dir / "__fp_users__"
        for src_file, dst_rel in _iter_prefix_save_pairs(
            src_users, dummy_users, preserve_user_dirs=True, raise_on_error=False
        ):
            rel = dst_rel.relative_to(dummy_users)
            seen.add(("users", rel.as_posix()))
            entries.append(("users", src_file, rel))
    src_progdata = _prefix_programdata_dir(prefix_dir)
    if src_progdata is not None:
        dummy_pd = prefix_dir / "__fp_pd__"
        for src_file, dst_rel in _iter_programdata_save_pairs(
            src_progdata, dummy_pd, raise_on_error=False
        ):
            rel = dst_rel.relative_to(dummy_pd)
            seen.add(("ProgramData", rel.as_posix()))
            entries.append(("ProgramData", src_file, rel))

    if game_name or steam_appid is not None:
        manifest_entry = find_manifest_game_entry(game_name, steam_appid=steam_appid)
        if manifest_entry is not None:
            m_files, _ = _resolve_manifest_save_paths(prefix_dir, manifest_entry, game_dir=game_dir)
            for section, src_file, rel in m_files:
                key = (section, rel.as_posix())
                if key not in seen:
                    seen.add(key)
                    entries.append((section, src_file, rel))

    entries.sort(key=lambda item: (item[0], item[2].as_posix()))
    return entries


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to target using a temp file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    with path.open("rb") as fh:
        file_digest = getattr(hashlib, "file_digest", None)
        if file_digest is not None:
            return str(file_digest(fh, "sha256").hexdigest())
        h = hashlib.sha256()
        while chunk := fh.read(65536):
            h.update(chunk)
        return h.hexdigest()


def _rotate_snapshots(snap_dir: Path, max_keep: int) -> None:
    """Keep at most max_keep newest .zip snapshots in snap_dir."""
    if max_keep <= 0 or not snap_dir.is_dir():
        return
    zips: list[tuple[float, str, Path]] = []
    for p in snap_dir.glob("*.zip"):
        if p.is_symlink() or not p.is_file():
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
    target_zip: Path,
    key_prefix: str,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> int:
    """Write prefix saves to target_zip atomically; return count of backed-up files."""
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_zip.stem}-", suffix=".zip.tmp", dir=str(target_zip.parent)
    )
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            game_files, _, total_bytes = _write_prefix_saves_to_zip(
                zf, prefix_dir, key_prefix, 0, 0
            )
            written = set(zf.namelist())
            for section, src_file, rel_path in _collect_prefix_save_entries(
                prefix_dir, game_name=game_name, game_dir=game_dir, steam_appid=steam_appid
            ):
                arcname = f"{key_prefix}/{section}/{rel_path.as_posix()}"
                if arcname in written:
                    continue
                if game_files + 1 > _RESTORE_MAX_ENTRIES:
                    raise RuntimeError(f"Snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
                sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
                copied = 0
                with os.fdopen(sfd, "rb") as src_fh, zf.open(arcname, "w") as dst_fh:
                    while chunk := src_fh.read(65536):
                        copied += len(chunk)
                        total_bytes += len(chunk)
                        if copied > _RESTORE_MAX_FILE_BYTES or total_bytes > _RESTORE_MAX_TOTAL_BYTES:
                            raise RuntimeError(f"Snapshot size limit exceeded on {src_file.name}")
                        dst_fh.write(chunk)
                game_files += 1
                written.add(arcname)

            reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
            if reg_bytes:
                if game_files + 1 > _RESTORE_MAX_ENTRIES:
                    raise RuntimeError(f"Snapshot exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})")
                if len(reg_bytes) > _RESTORE_MAX_FILE_BYTES or total_bytes + len(reg_bytes) > _RESTORE_MAX_TOTAL_BYTES:
                    raise RuntimeError("Snapshot size limit exceeded on registry.json")
                zf.writestr(f"{key_prefix}/registry/registry.json", reg_bytes)
                game_files += 1
        if game_files == 0:
            try:
                tmp_zip.unlink()
            except OSError:
                pass
            return 0
        os.replace(tmp_zip, target_zip)
        return game_files
    except Exception:
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        raise


def snapshot_prefix_saves(
    prefix_dir: Path,
    game_name: str,
    reason: str,
    key_prefix: str = "snap",
    dest_root: Path | None = None,
    max_keep: int = 5,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> Path | None:
    """Create a rotated pre-operation ZIP snapshot of prefix saves."""
    if not prefix_dir.is_dir():
        return None
    root = dest_root or DEFAULT_SNAPSHOTS_DIR
    safe_slug = _slugify(game_name)
    safe_reason = _slugify(reason, fallback="op")
    snap_dir = root / safe_slug
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_zip = snap_dir / f"{stamp}-{safe_reason}.zip"
    if target_zip.exists():
        counter = 1
        while (snap_dir / f"{stamp}-{safe_reason}-{counter}.zip").exists():
            counter += 1
        target_zip = snap_dir / f"{stamp}-{safe_reason}-{counter}.zip"

    written = _write_saves_zip_atomic(
        prefix_dir, target_zip, key_prefix, game_name=game_name,
        game_dir=game_dir, steam_appid=steam_appid,
    )
    if written == 0:
        return None
    _rotate_snapshots(snap_dir, max_keep)
    return target_zip


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
    if not prefix_dir.is_dir() or prefix_dir.is_symlink():
        return None
    entries = _collect_prefix_state_entries(prefix_dir, include_system_dlls=include_system_dlls)
    if not entries:
        return None

    root = dest_root or DEFAULT_SNAPSHOTS_DIR
    safe_slug = _slugify(game_name)
    safe_reason = _slugify(reason, fallback="state")
    snap_dir = root / safe_slug / "state"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_zip = snap_dir / f"{stamp}-{safe_reason}.zip"
    if target_zip.exists():
        counter = 1
        while (snap_dir / f"{stamp}-{safe_reason}-{counter}.zip").exists():
            counter += 1
        target_zip = snap_dir / f"{stamp}-{safe_reason}-{counter}.zip"

    snap_dir.mkdir(parents=True, exist_ok=True)
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
                with os.fdopen(sfd, "rb") as fh, zf.open(arcname, "w") as zout:
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
            return None
        os.replace(tmp_zip, target_zip)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise

    _rotate_snapshots(snap_dir, max_keep)
    return PrefixStateSnapshot(
        snapshot_zip=target_zip,
        prefix_dir=prefix_dir,
        reason=reason,
        reg_count=reg_count,
        dll_count=dll_count,
    )


def restore_prefix_state_snapshot(snapshot_zip: Path, prefix_dir: Path) -> tuple[int, int]:
    """Safely restore .reg and system DLL files from a prefix state snapshot ZIP."""
    if not snapshot_zip.is_file() or snapshot_zip.is_symlink():
        raise FileNotFoundError(f"Prefix state snapshot not found: {snapshot_zip}")

    wine_root = _resolve_wine_root(prefix_dir)
    drive_c = _resolve_drive_c_target(prefix_dir)
    wine_root.mkdir(parents=True, exist_ok=True)
    drive_c.mkdir(parents=True, exist_ok=True)
    wine_root_resolved = wine_root.resolve()
    drive_c_resolved = drive_c.resolve()

    reg_count = 0
    dll_count = 0
    total_bytes = 0
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
            if not _is_within_root(target.parent, base_resolved) or target.is_symlink():
                continue

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
                os.replace(tmp_path, target)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

            if kind == "reg":
                reg_count += 1
            else:
                dll_count += 1

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
    _, reg_paths = _resolve_manifest_save_paths(prefix_dir, entry)
    if not reg_paths:
        return None
    return _extract_registry_saves(prefix_dir, reg_paths)


def compute_prefix_saves_fingerprint(
    prefix_dir: Path,
    game_name: str = "",
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> SaveFingerprint:
    """Compute a deterministic fingerprint over all non-empty save files in prefix_dir."""
    entries = _collect_prefix_save_entries(
        prefix_dir,
        game_name=game_name,
        game_dir=game_dir,
        steam_appid=steam_appid,
    )
    reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
    if not entries and not reg_bytes:
        return SaveFingerprint(latest_mtime=0.0, file_count=0, digest="")

    h = hashlib.sha256()
    latest_mtime = 0.0
    file_count = 0
    for section, src_file, rel_path in entries:
        try:
            st = src_file.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        try:
            sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        h.update(f"{section}/{rel_path.as_posix()}\0{st.st_size}\0".encode())
        with os.fdopen(sfd, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
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


def _read_sync_base(prefix_dir: Path, sync_root: Path, slug: str) -> str:
    """Return the save digest last pushed/pulled by this device for (sync_root, slug)."""
    state_path = prefix_dir / _SYNC_BASE_FILE
    try:
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 1_048_576:
            return ""
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(_sync_base_key(sync_root, slug)) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _record_sync_base(prefix_dir: Path, sync_root: Path, slug: str, digest: str) -> None:
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
        data[_sync_base_key(sync_root, slug)] = digest
        _atomic_write_bytes(state_path, json.dumps(data, indent=2).encode("utf-8"))
    except OSError:
        logging.warning("Could not record sync base for %s in %s", slug, prefix_dir, exc_info=True)


def push_game_saves(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> Path | None:
    """Package prefix saves into <sync_root>/<slug>/latest.zip with atomic meta.json."""
    if not prefix_dir.is_dir():
        return None
    entries = _collect_prefix_save_entries(
        prefix_dir,
        game_name=game_name,
        game_dir=game_dir,
        steam_appid=steam_appid,
    )
    reg_bytes = _extract_manifest_reg_bytes(prefix_dir, game_name, steam_appid)
    if not entries and not reg_bytes:
        return None

    slug = _slugify(game_name)
    game_sync_dir = sync_root / slug
    latest_zip = game_sync_dir / "latest.zip"
    game_sync_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{latest_zip.stem}-", suffix=".zip.tmp", dir=str(game_sync_dir)
    )
    os.close(fd)
    tmp_zip = Path(tmp_name)

    h = hashlib.sha256()
    file_count = 0
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for section, src_file, rel_path in entries:
                try:
                    st = src_file.stat()
                except OSError:
                    continue
                fsize = st.st_size
                if fsize == 0:
                    continue
                if file_count + 1 > _RESTORE_MAX_ENTRIES:
                    raise RuntimeError(
                        f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
                    )
                if (
                    fsize > _RESTORE_MAX_FILE_BYTES
                    or total_uncompressed + fsize > _RESTORE_MAX_TOTAL_BYTES
                ):
                    raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
                try:
                    sfd = os.open(str(src_file), os.O_RDONLY | os.O_NOFOLLOW)
                except OSError:
                    continue
                arcname = f"save/{section}/{rel_path.as_posix()}"
                h.update(f"{section}/{rel_path.as_posix()}\0{fsize}\0".encode())
                file_bytes = 0
                with os.fdopen(sfd, "rb") as fh, zf.open(arcname, "w") as zout:
                    while chunk := fh.read(65536):
                        file_bytes += len(chunk)
                        if (
                            file_bytes > _RESTORE_MAX_FILE_BYTES
                            or total_uncompressed + file_bytes > _RESTORE_MAX_TOTAL_BYTES
                        ):
                            raise RuntimeError(f"Backup size limit exceeded on {src_file.name}")
                        h.update(chunk)
                        zout.write(chunk)
                total_uncompressed += file_bytes
                file_count += 1

            if reg_bytes:
                if file_count + 1 > _RESTORE_MAX_ENTRIES:
                    raise RuntimeError(
                        f"Backup exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
                    )
                if (
                    len(reg_bytes) > _RESTORE_MAX_FILE_BYTES
                    or total_uncompressed + len(reg_bytes) > _RESTORE_MAX_TOTAL_BYTES
                ):
                    raise RuntimeError("Backup size limit exceeded on registry.json")
                h.update(f"registry/registry.json\0{len(reg_bytes)}\0".encode())
                h.update(reg_bytes)
                zf.writestr("save/registry/registry.json", reg_bytes)
                total_uncompressed += len(reg_bytes)
                file_count += 1

        if file_count == 0:
            try:
                tmp_zip.unlink()
            except OSError:
                pass
            return None
        os.replace(tmp_zip, latest_zip)
    except Exception:
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        raise

    meta = {
        "game_name": game_name,
        "slug": slug,
        "updated_at": time.time(),
        "sha256": _sha256_file(latest_zip),
        "file_count": file_count,
        "fingerprint_digest": h.hexdigest(),
    }
    meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(game_sync_dir / "meta.json", meta_bytes)
    _record_sync_base(prefix_dir, sync_root, slug, str(meta["fingerprint_digest"]))
    return latest_zip


def _resolve_drive_c_target(prefix_dir: Path) -> Path:
    """Return the drive_c directory for extraction, supporting Proton pfx/drive_c or drive_c."""
    pfx_dir = prefix_dir / "pfx"
    pfx_drive_c = pfx_dir / "drive_c"
    if pfx_drive_c.is_dir() or pfx_dir.is_dir():
        return pfx_drive_c
    return prefix_dir / "drive_c"


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
    valid_sections = ("users", "ProgramData", "game", "registry")
    if len(parts) >= 2 and parts[0] in valid_sections:
        return parts[0], parts[1:]
    if len(parts) >= 3 and parts[1] in valid_sections:
        return parts[1], parts[2:]
    return None


def pull_game_saves(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    snapshots_root: Path | None = None,
    allow_unverified: bool = False,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> tuple[int, Path | None]:
    """Snapshot local saves, then safely extract synced saves into prefix drive_c."""
    slug = _slugify(game_name)
    latest_zip = sync_root / slug / "latest.zip"
    if not latest_zip.is_file():
        return 0, None

    meta = _read_sync_meta(sync_root / slug / "meta.json")
    expected_sha = meta.get("sha256") if meta is not None else None
    if isinstance(expected_sha, str) and expected_sha.strip():
        actual_sha = _sha256_file(latest_zip)
        if actual_sha.lower() != expected_sha.strip().lower():
            raise RuntimeError(
                f"Sync archive checksum mismatch for {game_name}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
    elif not allow_unverified:
        raise RuntimeError(f"Missing sync metadata or checksum for {game_name}")

    raw_ts = meta.get("updated_at") if meta is not None else None
    remote_updated_at = (
        float(raw_ts)
        if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) and float(raw_ts) > 0
        else None
    )

    snap_path = snapshot_prefix_saves(
        prefix_dir,
        game_name,
        "pre-sync-pull",
        dest_root=snapshots_root,
        game_dir=game_dir,
        steam_appid=steam_appid,
    )

    restored_files = _restore_saves_archive(latest_zip, prefix_dir, game_dir, remote_updated_at)
    remote_digest = meta.get("fingerprint_digest") if meta is not None else None
    if isinstance(remote_digest, str) and remote_digest:
        _record_sync_base(prefix_dir, sync_root, slug, remote_digest)
    return restored_files, snap_path


def restore_prefix_saves_snapshot(
    snapshot_zip: Path,
    prefix_dir: Path,
    game_dir: Path | None = None,
) -> int:
    """Restore a selected pre-operation snapshot, including manifest game files and registry saves."""
    if snapshot_zip.is_symlink() or not snapshot_zip.is_file():
        raise FileNotFoundError(f"Save snapshot not found: {snapshot_zip}")
    return _restore_saves_archive(snapshot_zip, prefix_dir, game_dir)


def _restore_saves_archive(
    archive_path: Path,
    prefix_dir: Path,
    game_dir: Path | None,
    remote_updated_at: float | None = None,
) -> int:
    """Extract bounded save entries into explicitly selected prefix and game roots."""
    drive_c = _resolve_drive_c_target(prefix_dir)
    if not _safe_parents_within(
        prefix_dir, prefix_dir.resolve(), drive_c.relative_to(prefix_dir).parts
    ):
        raise OSError(f"Unsafe target drive_c path: {drive_c}")
    drive_c.mkdir(parents=True, exist_ok=True)
    drive_c_resolved = drive_c.resolve()
    game_dir_resolved = game_dir.resolve() if game_dir is not None and game_dir.is_dir() else None

    restored_files = 0
    total_bytes = 0
    with zipfile.ZipFile(archive_path, "r") as zf:
        infolist = zf.infolist()
        if len(infolist) > _RESTORE_MAX_ENTRIES:
            raise RuntimeError(
                f"Sync archive exceeds maximum entry count ({_RESTORE_MAX_ENTRIES})"
            )
        for info in infolist:
            if info.is_dir():
                continue
            # Block symlink entries stored in POSIX zip attributes.
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                continue
            parsed = _parse_sync_zip_entry(info.filename, game_dir=game_dir)
            if parsed is None:
                continue
            section, rel_parts = parsed
            if info.file_size > _RESTORE_MAX_FILE_BYTES:
                raise RuntimeError(f"Sync file too large: {info.filename}")

            if section == "registry":
                if rel_parts != ("registry.json",):
                    continue
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
                if _restore_registry_saves(prefix_dir, b"".join(chunks)) > 0:
                    restored_files += 1
                continue

            if section == "game":
                if game_dir is None or game_dir_resolved is None:
                    continue
                base_dir = game_dir
                base_resolved = game_dir_resolved
                check_parts = rel_parts[:-1]
                target = game_dir.joinpath(*rel_parts)
            else:
                base_dir = drive_c
                base_resolved = drive_c_resolved
                check_parts = (section, *rel_parts[:-1])
                target = drive_c.joinpath(section, *rel_parts)

            if not _safe_parents_within(base_dir, base_resolved, check_parts):
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            if not _is_within_root(target.parent, base_resolved):
                continue
            if target.is_symlink():
                continue

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
                os.replace(tmp_path, target)
                if remote_updated_at is not None:
                    try:
                        os.utime(target, (remote_updated_at, remote_updated_at))
                    except OSError:
                        pass
            except Exception:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
            restored_files += 1

    return restored_files


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


def plan_sync_all(
    games: list[tuple[str, Path]],
    sync_root: Path,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> list[SyncPlanItem]:
    """Compare local prefix save fingerprints against sync_root metadata."""
    plan: list[SyncPlanItem] = []
    for game_name, prefix_dir in games:
        slug = _slugify(game_name)
        local_fp = compute_prefix_saves_fingerprint(
            prefix_dir,
            game_name=game_name,
            game_dir=game_dir,
            steam_appid=steam_appid,
        )
        has_local = local_fp.file_count > 0

        game_sync_dir = sync_root / slug
        has_remote = (game_sync_dir / "latest.zip").is_file()

        if not has_local and not has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="NO_SAVES",
                    reason="No local or remote saves found",
                )
            )
            continue
        if has_local and not has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="PUSH",
                    reason="Local saves present; no remote sync archive yet",
                )
            )
            continue
        if not has_local and has_remote:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="PULL",
                    reason="Remote sync archive available; no local saves",
                )
            )
            continue

        meta = _read_sync_meta(game_sync_dir / "meta.json")
        if meta is None:
            plan.append(
                SyncPlanItem(
                    game_name=game_name,
                    slug=slug,
                    prefix_dir=prefix_dir,
                    action="CONFLICT",
                    reason="Remote metadata missing or unreadable",
                )
            )
            continue

        remote_digest = str(meta.get("fingerprint_digest") or "")
        raw_updated = meta.get("updated_at")
        if isinstance(raw_updated, (int, float, str)) and not isinstance(raw_updated, bool):
            try:
                remote_updated_at = float(raw_updated)
            except (TypeError, ValueError):
                remote_updated_at = 0.0
        else:
            remote_updated_at = 0.0

        # Three-way decision against the last digest this machine synced (the "base").
        # Timestamps alone cannot tell "only one side changed" from "both sides changed".
        base_digest = _read_sync_base(prefix_dir, sync_root, slug)
        if remote_digest and local_fp.digest == remote_digest:
            action = "IN_SYNC"
            reason = "Local and remote save digests match"
        elif not remote_digest or remote_updated_at <= 0.0:
            action = "CONFLICT"
            reason = "Incomplete remote metadata; manual resolution required"
        elif not base_digest:
            action = "CONFLICT"
            reason = "No previous sync recorded on this device; choose which saves to keep"
        elif remote_digest == base_digest:
            action = "PUSH"
            reason = "Only local saves changed since last sync"
        elif local_fp.digest == base_digest:
            action = "PULL"
            reason = "Only remote saves changed since last sync"
        else:
            action = "CONFLICT"
            reason = "Both local and remote saves changed since last sync"

        plan.append(
            SyncPlanItem(
                game_name=game_name,
                slug=slug,
                prefix_dir=prefix_dir,
                action=action,
                reason=reason,
            )
        )
    return plan


def preview_sync_operation(
    game_name: str,
    prefix_dir: Path,
    sync_root: Path,
    action: str | None = None,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> SyncDryRunPreview:
    """Build a dry-run preview of files and registry entries for a sync operation."""
    slug = _slugify(game_name)
    planned = plan_sync_all(
        [(game_name, prefix_dir)],
        sync_root,
        game_dir=game_dir,
        steam_appid=steam_appid,
    )[0]
    effective_action = action.strip().upper() if action else planned.action
    reason = planned.reason if action is None else f"Requested {effective_action}"

    files: list[SyncDryRunFile] = []
    total_bytes = 0
    registry_entries = 0

    if effective_action == "PUSH":
        entries = _collect_prefix_save_entries(
            prefix_dir,
            game_name=game_name,
            game_dir=game_dir,
            steam_appid=steam_appid,
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
        latest_zip = sync_root / slug / "latest.zip"
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

    for sroot in (base_saves / "snapshots", legacy_base / "snapshots"):
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
