"""Import and export game saves in the Ludusavi backup layout.

A Ludusavi backup is one folder per game:

    <root>/<escaped game name>/mapping.yaml
    <root>/<escaped game name>/drive-C/Users/...           full backup named "." (simple mode)
    <root>/<escaped game name>/backup-<stamp>/drive-C/...  named full or differential backup
    <root>/<escaped game name>/backup-<stamp>.zip          the same, with compression on

mapping.yaml:

    name: <game>
    drives: {drive-C: "C:", drive-0: ""}            # folder name -> original drive ("" = Unix root)
    backups:                                         # full backups, oldest first
      - name: "."
        when: "2026-01-01T00:00:00Z"
        os: windows
        files: {"C:/Users/me/save.dat": {hash: <sha1>, size: 12}}
        registry: {hash: ~}
        children:                                    # differential backups against that full one
          - name: backup-20260102T000000Z
            files: {"C:/Users/me/save.dat": {hash: ..., size: ...}, "C:/Users/me/old.dat": ~}

Export writes one simple full backup ("." with C:/users/steamuser/... paths). Import restores the
newest full backup plus its newest differential child; Windows paths go into the game's prefix
(any user -> steamuser), Unix paths only when they stay inside $HOME. Every file is checked against
its sha1 and size before anything is written, and an existing save is only replaced when the
safety snapshot taken first holds it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from . import save_sync
from .fsutil import is_within
from .launcher_scan import _split_yaml_key_value, _strip_yaml_comment, _unquote_yaml_scalar
from .steam_vdf import (
    _RESTORE_MAX_ENTRIES,
    _RESTORE_MAX_FILE_BYTES,
    _RESTORE_MAX_TOTAL_BYTES,
    _norm_key,
    _safe_parents_within,
)

MAPPING_FILE = "mapping.yaml"
_MAPPING_MAX_BYTES = 16 * 1024 * 1024
_DRIVE_FOLDER = re.compile(r"^drive-[A-Za-z0-9_]{1,32}$")
_BACKUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WIN_PATH = re.compile(r"^([A-Za-z]:)(?:/(.*))?$")
_INT = re.compile(r"^-?[0-9]+$")
_FOLDER_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class LudusaviError(ValueError):
    """The backup is malformed, unsafe or does not match its hashes; nothing was written."""


class ImportResult(NamedTuple):
    restored: int  # files written
    unchanged: int  # already identical
    kept: int  # existing different files left alone (not covered by the safety snapshot)
    skipped: int  # paths that do not map into this prefix or $HOME
    backup: str  # name of the backup that was restored
    snapshot: Path | None  # safety snapshot taken before overwriting


# --- minimal YAML (the subset serde_yaml writes for mapping.yaml) -----------------------------


def _yaml_scalar(raw: str) -> Any:
    val = raw.strip()
    if val in ("~", "null", "Null", "NULL", ""):
        return None
    if val == "{}":
        return {}
    if val == "[]":
        return []
    if val[0] in "'\"":
        return _unquote_yaml_scalar(val)
    if val in ("true", "false"):
        return val == "true"
    if _INT.match(val):
        return int(val)
    return val


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _yaml_block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    if _is_item(lines[i][1]):
        return _yaml_seq(lines, i, indent)
    return _yaml_map(lines, i, indent)


def _yaml_value_after(lines: list[tuple[int, str]], i: int, indent: int, rest: str) -> tuple[Any, int]:
    if rest:
        return _yaml_scalar(rest), i
    if i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and _is_item(lines[i][1]))):
        return _yaml_block(lines, i, lines[i][0])
    return None, i


def _yaml_map(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while i < len(lines):
        ind, text = lines[i]
        if ind > indent:
            raise LudusaviError(f"Unexpected indentation in {MAPPING_FILE} line: {text[:60]}")
        if ind < indent or _is_item(text):
            break
        if text == "?" or text.startswith("? "):
            # Explicit key, which serde_yaml writes for long keys: "? <key>" then ": <value>".
            raw_key = text[1:].strip()
            i += 1
            # A long plain key wraps onto more-indented lines, folded back with single spaces.
            while i < len(lines) and lines[i][0] > indent:
                raw_key += " " + lines[i][1]
                i += 1
            key = str(_yaml_scalar(raw_key) or "")
            if i >= len(lines) or lines[i][0] != indent or not (lines[i][1] == ":" or lines[i][1].startswith(": ")):
                out[key] = None
                continue
            out[key], i = _yaml_inline_block(lines, i, indent)
            continue
        split = _split_yaml_key_value(text)
        if split is None:
            raise LudusaviError(f"Unsupported {MAPPING_FILE} line: {text[:60]}")
        key = _unquote_yaml_scalar(split[0])
        out[key], i = _yaml_value_after(lines, i + 1, indent, split[1].strip())
    return out, i


def _yaml_inline_block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    """Value after a one-char indicator ("- " item or ": " explicit value) on line i."""
    text = lines[i][1]
    body = text[1:].lstrip(" ")
    offset = indent + len(text) - len(body)
    if not body:
        return _yaml_value_after(lines, i + 1, indent, "")
    if _split_yaml_key_value(body) is not None:
        lines[i] = (offset, body)
        return _yaml_map(lines, i, offset)
    return _yaml_scalar(body), i + 1


def _yaml_seq(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while i < len(lines):
        ind, text = lines[i]
        if ind > indent:
            raise LudusaviError(f"Unexpected indentation in {MAPPING_FILE} line: {text[:60]}")
        if ind < indent or not _is_item(text):
            break
        value, i = _yaml_inline_block(lines, i, indent)
        out.append(value)
    return out, i


def parse_yaml(text: str) -> Any:
    """Parse block mappings/sequences with plain or quoted scalars (no anchors, no multi-line)."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        cleaned = _strip_yaml_comment(raw).rstrip()
        stripped = cleaned.lstrip(" ")
        if not stripped or stripped in ("---", "..."):
            continue
        if "\t" in cleaned[: len(cleaned) - len(stripped)]:
            raise LudusaviError(f"Tabs are not valid YAML indentation in {MAPPING_FILE}")
        lines.append((len(cleaned) - len(stripped), stripped))
    if not lines:
        return {}
    value, end = _yaml_block(lines, 0, lines[0][0])
    if end != len(lines):
        raise LudusaviError(f"Could not read {MAPPING_FILE} past: {lines[end][1][:60]}")
    return value


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _key(text: str) -> str:
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", text) else _q(text)


def _yaml_out(value: Any) -> str:
    if value is None:
        return "~"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _q(str(value))


def _emit(value: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)) and child:
                out.append(f"{pad}{_key(str(key))}:")
                _emit(child, indent + 2, out)
            else:
                out.append(f"{pad}{_key(str(key))}: {_yaml_out(child)}")
    else:
        for child in value:
            if isinstance(child, (dict, list)) and child:
                sub: list[str] = []
                _emit(child, indent + 2, sub)
                sub[0] = f"{pad}- {sub[0][indent + 2:]}"
                out.extend(sub)
            else:
                out.append(f"{pad}- {_yaml_out(child)}")


def dump_yaml(value: dict[str, Any]) -> str:
    out = ["---"]
    _emit(value, 0, out)
    return "\n".join(out) + "\n"


# --- layout helpers ----------------------------------------------------------------------------


def escape_folder_name(name: str) -> str:
    """Ludusavi's per-game folder name: unsafe characters and a trailing dot/space become '_'."""
    escaped = _FOLDER_UNSAFE.sub("_", name.strip())
    if escaped.endswith((".", " ")):
        escaped = escaped[:-1] + "_"
    if escaped.startswith("."):
        escaped = "_" + escaped[1:]
    return escaped or "_"


def _safe_parts(rel: str) -> tuple[str, ...]:
    """Split a '/' path into components, rejecting traversal, empty parts and control characters."""
    if "\\" in rel or "\x00" in rel:
        raise LudusaviError(f"Unsafe path in backup: {rel!r}")
    parts = tuple(rel.split("/")) if rel else ()
    for part in parts:
        if part in ("", ".", "..") or any(ord(c) < 32 for c in part):
            raise LudusaviError(f"Unsafe path in backup: {rel!r}")
    return parts


def _split_drive(path: str) -> tuple[str, tuple[str, ...]]:
    """'C:/Users/me/x' -> ('C:', parts); '/home/me/x' -> ('', parts)."""
    m = _WIN_PATH.match(path)
    if m:
        return m.group(1).upper(), _safe_parts(m.group(2) or "")
    if path.startswith("/"):
        return "", _safe_parts(path[1:])
    raise LudusaviError(f"Unsupported path in backup: {path!r}")


def _sha1_stream(chunks: Iterator[bytes]) -> tuple[str, int]:
    h = hashlib.sha1()  # noqa: S324 - Ludusavi's own content hash, not used for security
    size = 0
    for chunk in chunks:
        size += len(chunk)
        h.update(chunk)
    return h.hexdigest(), size


def _file_chunks(path: Path) -> Iterator[bytes]:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(65536):
            yield chunk


# --- export ------------------------------------------------------------------------------------


def _export_entries(
    prefix_dir: Path, game_name: str, game_dir: Path | None, steam_appid: int | None
) -> tuple[list[tuple[str, Path]], int]:
    """(original Windows path, source file) for prefix saves, and the count left out."""
    entries: list[tuple[str, Path]] = []
    left_out = 0
    for section, src, rel in save_sync._collect_prefix_save_entries(
        prefix_dir, game_name=game_name, game_dir=game_dir, steam_appid=steam_appid
    ):
        if section not in ("users", "ProgramData"):
            left_out += 1  # files in the game folder have no stable drive path
            continue
        try:
            parts = _safe_parts(rel.as_posix())
        except LudusaviError:
            left_out += 1
            continue
        entries.append(("C:/" + "/".join((section, *parts)), src))
    return entries, left_out


def export_ludusavi(
    prefix_dir: Path,
    game_name: str,
    dest_root: Path,
    *,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
) -> tuple[Path, int, int]:
    """Write the game's prefix saves as a Ludusavi simple full backup under dest_root.

    Returns (game folder, files exported, files left out). An earlier Ludusavi backup of the same
    game is replaced; any other existing folder of that name is refused.
    """
    entries, left_out = _export_entries(prefix_dir, game_name, game_dir, steam_appid)
    if not entries:
        raise LudusaviError(f"No saves found for {game_name}")
    if len(entries) > _RESTORE_MAX_ENTRIES:
        raise LudusaviError(f"Too many save files ({len(entries)})")
    target = dest_root / escape_folder_name(game_name)
    if target.is_symlink() or (target.exists() and not (target / MAPPING_FILE).is_file()):
        raise LudusaviError(f"{target} exists and is not a Ludusavi backup")
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(dest_root)))
    try:
        files: dict[str, Any] = {}
        total = 0
        for original, src in entries:
            dst = tmp / "drive-C" / original[3:]
            dst.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha1()  # noqa: S324 - format-defined hash
            size = 0
            with dst.open("wb") as out:
                for chunk in _file_chunks(src):
                    size += len(chunk)
                    total += len(chunk)
                    if size > _RESTORE_MAX_FILE_BYTES or total > _RESTORE_MAX_TOTAL_BYTES:
                        raise LudusaviError(f"Save files too large to export ({src.name})")
                    h.update(chunk)
                    out.write(chunk)
            files[original] = {"hash": h.hexdigest(), "size": size}
        mapping = {
            "name": game_name,
            "drives": {"drive-C": "C:"},
            "backups": [
                {
                    "name": ".",
                    "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "os": "windows",
                    "files": files,
                    "registry": {"hash": None},
                    "children": [],
                }
            ],
        }
        (tmp / MAPPING_FILE).write_text(dump_yaml(mapping), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp, target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target, len(files), left_out


# --- import ------------------------------------------------------------------------------------


def load_mapping(game_folder: Path) -> dict[str, Any]:
    path = game_folder / MAPPING_FILE
    if path.is_symlink() or not path.is_file():
        raise LudusaviError(f"{MAPPING_FILE} not found in {game_folder}")
    if path.stat().st_size > _MAPPING_MAX_BYTES:
        raise LudusaviError(f"{MAPPING_FILE} is too large")
    data = parse_yaml(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict) or not isinstance(data.get("backups"), list):
        raise LudusaviError(f"{MAPPING_FILE} has no backups list")
    return data


def find_game_folder(root: Path, game_name: str) -> Path | None:
    """root itself when it holds mapping.yaml, else the child backup whose name matches the game."""
    if (root / MAPPING_FILE).is_file():
        return root
    direct = root / escape_folder_name(game_name)
    if (direct / MAPPING_FILE).is_file():
        return direct
    want = _norm_key(game_name)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_symlink() or not (child / MAPPING_FILE).is_file():
            continue
        try:
            name = load_mapping(child).get("name")
        except (LudusaviError, OSError):
            continue
        if isinstance(name, str) and want and _norm_key(name) == want:
            return child
    return None


def _newest_files(mapping: dict[str, Any], older: int = 0) -> tuple[str, dict[str, tuple[str, str, int]]]:
    """(label, {original path: (backup name, sha1, size)}) for the newest full backup + child.

    older=1 takes the full backup before the newest, and so on.
    """
    backups = [b for b in mapping["backups"] if isinstance(b, dict)]
    if len(backups) <= older:
        raise LudusaviError("The backup holds no full backup")
    full = backups[-1 - older]
    chain = [full]
    children = full.get("children")
    if isinstance(children, list) and children and isinstance(children[-1], dict):
        chain.append(children[-1])
    files: dict[str, tuple[str, str, int]] = {}
    label = ""
    for backup in chain:
        name = str(backup.get("name") or "")
        if name != "." and (not _BACKUP_NAME.match(name) or ".." in name):
            raise LudusaviError(f"Unsafe backup name: {name!r}")
        label = name
        raw = backup.get("files") or {}
        if not isinstance(raw, dict):
            raise LudusaviError(f"Backup {name!r} has no file list")
        for original, info in raw.items():
            if info is None:  # removed in this differential backup
                files.pop(original, None)
                continue
            if not isinstance(info, dict):
                raise LudusaviError(f"Bad entry for {original!r}")
            digest, size = str(info.get("hash") or "").lower(), info.get("size")
            if not re.fullmatch(r"[0-9a-f]{40}", digest) or not isinstance(size, int) or size < 0:
                raise LudusaviError(f"Missing hash or size for {original!r}")
            files[original] = (name, digest, size)
    if len(files) > _RESTORE_MAX_ENTRIES:
        raise LudusaviError(f"Too many files in backup ({len(files)})")
    return label, files


def _drive_folders(mapping: dict[str, Any]) -> dict[str, str]:
    """{original drive ('C:' / '') : folder name}."""
    raw = mapping.get("drives") or {}
    if not isinstance(raw, dict):
        raise LudusaviError("Bad drives table")
    out: dict[str, str] = {}
    for folder, drive in raw.items():
        if not _DRIVE_FOLDER.match(str(folder)):
            raise LudusaviError(f"Unsafe drive folder: {folder!r}")
        drv = str(drive or "").rstrip("/").upper()
        out[drv] = str(folder)
    return out


def _target_for(drive: str, parts: tuple[str, ...], home: Path) -> tuple[str, tuple[str, ...]] | None:
    """('prefix', parts under drive_c) or ('home', parts under home); None when it does not map."""

    def windows(rest: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        if not rest:
            return None
        if rest[0].lower() == "users" and len(rest) >= 3:
            user = "Public" if rest[1].lower() == "public" else "steamuser"
            return "prefix", ("users", user, *rest[2:])
        if rest[0].lower() == "users":
            return None
        return "prefix", rest

    if drive == "C:":
        return windows(parts)
    if drive:
        return None  # other Windows drives (game install folders) have no place in a prefix
    lowered = [p.lower() for p in parts]
    if "drive_c" in lowered:
        return windows(parts[lowered.index("drive_c") + 1:])
    home_parts = home.parts[1:]
    if len(parts) > len(home_parts) and parts[: len(home_parts)] == home_parts:
        rest = parts[len(home_parts):]
        return ("home", rest) if save_sync.home_entry_allowed(rest) else None
    return None


def _ci_path(base: Path, parts: tuple[str, ...]) -> tuple[str, ...]:
    """Reuse existing directory names that differ only in case (Wine paths are case-insensitive)."""
    cur, out = base, []
    for part in parts[:-1]:
        found = save_sync._find_child_ci(cur, part.lower()) if not (cur / part).exists() else None
        name = found.name if found is not None else part
        out.append(name)
        cur = cur / name
    return (*out, parts[-1])


class _Source(NamedTuple):
    original: str
    zip_path: Path | None  # None: a plain file at file_path
    member: str
    file_path: Path
    digest: str
    size: int


def _source_chunks(src: _Source, zips: dict[Path, zipfile.ZipFile]) -> Iterator[bytes]:
    if src.zip_path is None:
        yield from _file_chunks(src.file_path)
        return
    zf = zips[src.zip_path]
    try:
        info = zf.getinfo(src.member)
    except KeyError as exc:
        raise LudusaviError(f"Missing in backup: {src.original}") from exc
    if (info.external_attr >> 16) & 0o170000 == 0o120000 or info.file_size > _RESTORE_MAX_FILE_BYTES:
        raise LudusaviError(f"Unsafe entry in backup: {src.original}")
    with zf.open(info) as fh:
        while chunk := fh.read(65536):
            yield chunk


def _bounded(chunks: Iterator[bytes], limit: int, original: str) -> Iterator[bytes]:
    seen = 0
    for chunk in chunks:
        seen += len(chunk)
        if seen > limit:
            raise LudusaviError(f"File larger than recorded: {original}")
        yield chunk


def _plan_sources(
    game_folder: Path,
    home: Path,
    mapper: Callable[[str, tuple[str, ...]], tuple[str, tuple[str, ...]] | None] | None = None,
    older: int = 0,
) -> tuple[str, list[tuple[_Source, str, tuple[str, ...]]], int]:
    """(backup label, [(source, kind, target parts)], files that map nowhere) for the newest backup.

    mapper(drive, parts) -> (kind, parts) | None; the default maps into a prefix or under home.
    """
    mapping = load_mapping(game_folder)
    label, files = _newest_files(mapping, older)
    drives = _drive_folders(mapping)
    folder = game_folder.resolve()
    plan: list[tuple[_Source, str, tuple[str, ...]]] = []
    skipped = 0
    for original, (backup, digest, size) in sorted(files.items()):
        drive, parts = _split_drive(original)
        if not parts:
            raise LudusaviError(f"Unsafe path in backup: {original!r}")
        dfolder = drives.get(drive) or ("drive-0" if drive == "" else f"drive-{drive[:1]}")
        base = folder if backup == "." else folder / backup
        zip_path = folder / f"{backup}.zip" if backup != "." and not base.is_dir() else None
        member = "/".join((dfolder, *parts))
        file_path = base.joinpath(dfolder, *parts)
        if zip_path is None and (not is_within(file_path, folder, strict=True) or not file_path.is_file()):
            raise LudusaviError(f"Missing in backup: {original}")
        if zip_path is not None and (zip_path.is_symlink() or not zip_path.is_file()):
            raise LudusaviError(f"Missing in backup: {original}")
        target = mapper(drive, parts) if mapper is not None else _target_for(drive, parts, home)
        src = _Source(original, zip_path, member, file_path, digest, size)
        if target is None or (target[0] == "home" and not save_sync.home_entry_allowed(target[1], _source_mode(src))):
            skipped += 1
            continue
        plan.append((src, *target))

    return label, plan, skipped


def _source_mode(src: _Source) -> int:
    """Unix mode recorded for a zipped backup file (0 for plain files: exFAT/NTFS mark everything executable)."""
    if src.zip_path is None:
        return 0
    try:
        with zipfile.ZipFile(src.zip_path) as zf:
            return zf.getinfo(src.member).external_attr >> 16
    except (OSError, KeyError, zipfile.BadZipFile):
        return 0


def _verified_chunks(src: _Source, zips: dict[Path, zipfile.ZipFile]) -> Iterator[bytes]:
    """The file's bytes, failing if its sha1/size do not match the mapping (checked at the end)."""
    h = hashlib.sha1()  # noqa: S324 - format-defined hash
    seen = 0
    for chunk in _bounded(_source_chunks(src, zips), src.size, src.original):
        h.update(chunk)
        seen += len(chunk)
        yield chunk
    if h.hexdigest() != src.digest or seen != src.size:
        raise LudusaviError(f"Hash mismatch for {src.original}")


def list_backup_games(root: Path) -> list[tuple[str, Path]]:
    """(game name, folder) for every game in a Ludusavi backup root (or root itself)."""
    if (root / MAPPING_FILE).is_file():
        candidates = [root]
    else:
        try:
            candidates = sorted(c for c in root.iterdir() if not c.is_symlink() and (c / MAPPING_FILE).is_file())
        except OSError:
            return []
    out: list[tuple[str, Path]] = []
    for folder in candidates:
        try:
            name = load_mapping(folder).get("name")
        except (LudusaviError, OSError):
            continue
        out.append((str(name) if isinstance(name, str) and name.strip() else folder.name, folder))
    return out


_SNAPSHOT_SKIP_HOME_DIRS = frozenset({"shadercache", "cache", "logs", "compatdata", "proton", ".cache"})


def _snapshot_target(drive: str, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """Where a backed-up file goes inside a snapshot: (section, parts under it), or None.

    Sections: "prefix" (parts start with users/ProgramData, any Windows user becomes steamuser),
    "steamcloud" (Steam userdata/<uid>/<appid>/remote, the user id is dropped) and "home"
    (a Unix path under /home/<user>/, the user name is dropped).
    """
    lowered = [p.lower() for p in parts]
    if drive == "C:" or (drive == "" and "drive_c" in lowered):
        rest = parts if drive == "C:" else parts[lowered.index("drive_c") + 1:]
        if len(rest) >= 3 and rest[0].lower() == "users":
            user = "Public" if rest[1].lower() == "public" else "steamuser"
            return "prefix", ("users", user, *rest[2:])
        if len(rest) >= 2 and rest[0].lower() == "programdata":
            return "prefix", ("ProgramData", *rest[1:])
        return None
    if drive or len(parts) < 3 or parts[0] != "home":
        return None
    for i in range(len(parts) - 4):
        if parts[i] == "userdata" and parts[i + 1].isdigit() and parts[i + 2].isdigit() and parts[i + 3] == "remote":
            return "steamcloud", parts[i + 4:]
    rest = parts[2:]
    if any(p.lower() in _SNAPSHOT_SKIP_HOME_DIRS for p in rest[:-1]) or "drive_c" in [p.lower() for p in rest]:
        return None
    return ("home", rest) if save_sync.home_entry_allowed(rest) else None


def ludusavi_backup_path(home: Path | None = None) -> Path | None:
    """The backup folder set in Ludusavi's own config.yaml (native or Flatpak), when it exists."""
    home = home or Path.home()
    for cfg in (
        home / ".config" / "ludusavi" / "config.yaml",
        home / ".var" / "app" / "com.github.mtkennerly.ludusavi" / "config" / "ludusavi" / "config.yaml",
    ):
        try:
            if cfg.is_symlink() or cfg.stat().st_size > 1024 * 1024:
                continue
            lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_backup = False
        for line in lines:
            if line.startswith("backup:"):
                in_backup = True
            elif in_backup and line and not line.startswith(" "):
                break
            elif in_backup and line.startswith("  path:"):
                value = _unquote_yaml_scalar(_strip_yaml_comment(line.split(":", 1)[1]).strip())
                if value and Path(value).expanduser().is_dir():
                    return Path(value).expanduser()
                break
    return None


_IMPORT_ZIP_RE = re.compile(r"^(\d{8}-\d{6})-ludusavi-import(?:-(\d+))?\.zip$")


def _content_fingerprint(items: list[tuple[str, str, int]]) -> str:
    """Stable hash of a snapshot's content: sorted (path in the zip, sha1, size) of every file."""
    h = hashlib.sha1()  # noqa: S324 - content id, not security
    for dest, digest, size in sorted(items):
        h.update(f"{dest}\0{digest}\0{size}\n".encode())
    return h.hexdigest()


def _zip_fingerprint(zip_path: Path) -> str | None:
    """The content fingerprint of an existing snapshot zip (None when unreadable or too large)."""
    items: list[tuple[str, str, int]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > _RESTORE_MAX_ENTRIES or sum(i.file_size for i in infos) > _RESTORE_MAX_TOTAL_BYTES:
                return None
            for info in infos:
                with zf.open(info) as fh:
                    digest, size = _sha1_stream(iter(partial(fh.read, 65536), b""))
                items.append((info.filename, digest, size))
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    return _content_fingerprint(items)


def _import_zip_order(path: Path) -> tuple[str, int, str]:
    """Creation order of import zips: name timestamp, then the -N counter."""
    m = _IMPORT_ZIP_RE.match(path.name)
    return (m.group(1), int(m.group(2) or 0), path.name) if m else ("", 0, path.name)


def import_snapshot_fingerprints(snap_dir: Path) -> dict[Path, str]:
    """{import zip: fingerprint} of a game's Ludusavi imports, oldest first.

    Zips from older versions have no recorded fingerprint: it is computed once and saved.
    """
    known = save_sync.import_fingerprints(snap_dir)
    try:
        zips = sorted(
            (p for p in snap_dir.glob("*-ludusavi-import*.zip") if not p.is_symlink() and p.is_file()),
            key=_import_zip_order,
        )
    except OSError:
        return {}
    out: dict[Path, str] = {}
    for zp in zips:
        fp = known.get(zp.name)
        if fp is None:
            fp = _zip_fingerprint(zp)
            if fp is None:
                continue
            save_sync.remember_import_fingerprint(snap_dir, zp.name, fp)
        out[zp] = fp
    return out


def _newest_snapshot(snap_dir: Path) -> Path | None:
    """The game's newest snapshot zip (what Restore and the install offer pick)."""
    try:
        zips = [p for p in snap_dir.glob("*.zip") if not p.is_symlink() and p.is_file()]
        return max(zips, key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None


def find_import_duplicates(dest_root: Path | None = None) -> list[Path]:
    """Ludusavi import zips whose content equals a newer import of the same game (the newest is kept).

    Keeping the newest copy keeps the newest snapshot the same: after importing A, B and A again,
    the save restored is still A.
    """
    root = dest_root or save_sync.DEFAULT_SNAPSHOTS_DIR
    try:
        game_dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.is_symlink())
    except OSError:
        return []
    dupes: list[Path] = []
    for snap_dir in game_dirs:
        seen: set[str] = set()
        for zp, fp in reversed(list(import_snapshot_fingerprints(snap_dir).items())):  # newest first
            if fp in seen:
                dupes.append(zp)
            seen.add(fp)
    return sorted(dupes)


def import_ludusavi_to_snapshot(
    game_folder: Path,
    game_name: str = "",
    *,
    home: Path | None = None,
    dest_root: Path | None = None,
) -> tuple[Path | None, int, int, bool]:
    """Store the newest backup as a save snapshot of game_name, without touching any prefix.

    For games that are not installed yet: the snapshot is restored later with the normal
    "Restore" (which takes a safety snapshot first). Only Windows user/ProgramData files fit a
    snapshot; anything else is counted as skipped. Every file is checked against its sha1/size.
    An import with the same content as the newest snapshot creates nothing.
    Returns (snapshot zip or None when nothing fit, files stored, files skipped, already imported).
    """
    home = (home or Path.home()).resolve()
    name = game_name or str(load_mapping(game_folder).get("name") or game_folder.name)
    # The newest full backup can hold only registry/config files; fall back to an older full one.
    n_full = len([b for b in load_mapping(game_folder)["backups"] if isinstance(b, dict)])
    for older in range(max(1, n_full)):
        _, plan, skipped = _plan_sources(game_folder, home, _snapshot_target, older)
        if plan:
            break
    usable: list[tuple[_Source, tuple[str, ...]]] = []
    appid = ""
    for src, kind, parts in plan:
        if kind == "steamcloud":
            m = re.search(r"/userdata/\d+/(\d+)/remote/", src.original)
            if m is not None and appid in ("", m.group(1)):  # one Steam app per snapshot
                appid = m.group(1)
                usable.append((src, (kind, *parts)))
            else:
                skipped += 1
        else:
            usable.append((src, (parts if kind == "prefix" else (kind, *parts))))
    if not usable:
        return None, 0, skipped, False
    snap_dir = save_sync.snapshot_dir_for(name, dest_root)
    fingerprint = _content_fingerprint([("snap/" + "/".join(p), src.digest, src.size) for src, p in usable])
    if snap_dir.is_dir():
        # Already there only when it is the newest snapshot: importing an older state again (A, B, A)
        # must make it the newest, or Restore would bring back B.
        newest = _newest_snapshot(snap_dir)
        if newest is not None and import_snapshot_fingerprints(snap_dir).get(newest) == fingerprint:
            return newest, len(usable), skipped, True
    snap_dir.mkdir(parents=True, exist_ok=True)
    target = snap_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-ludusavi-import.zip"
    counter = 1
    while target.exists():
        target = snap_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-ludusavi-import-{counter}.zip"
        counter += 1
    fd, tmp_name = tempfile.mkstemp(prefix=".ludusavi-", suffix=".zip.tmp", dir=str(snap_dir))
    os.close(fd)
    tmp = Path(tmp_name)
    zips: dict[Path, zipfile.ZipFile] = {}
    total = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for src, parts in usable:
                total += src.size
                if src.size > _RESTORE_MAX_FILE_BYTES or total > _RESTORE_MAX_TOTAL_BYTES:
                    raise LudusaviError(f"Backup too large: {src.original}")
                if src.zip_path is not None and src.zip_path not in zips:
                    zips[src.zip_path] = zipfile.ZipFile(src.zip_path)
                with out.open("snap/" + "/".join(parts), "w") as dst:
                    for chunk in _verified_chunks(src, zips):
                        dst.write(chunk)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        for zf in zips.values():
            zf.close()
    save_sync.remember_snapshot_name(snap_dir, name)
    save_sync.remember_import_fingerprint(snap_dir, target.name, fingerprint)
    if appid:
        save_sync.remember_steamcloud_appid(snap_dir, target.name, appid)
    save_sync.remember_home_files(snap_dir, ["/".join(p[1:]) for _, p in usable if p[0] == "home"])
    return target, len(usable), skipped, False


def import_ludusavi(
    game_folder: Path,
    prefix_dir: Path,
    game_name: str,
    *,
    home: Path | None = None,
    game_dir: Path | None = None,
    steam_appid: int | None = None,
    dest_root: Path | None = None,
) -> ImportResult:
    """Restore the newest backup in game_folder into prefix_dir (see the module docstring)."""
    home = (home or Path.home()).resolve()
    drive_c = save_sync._resolve_drive_c_target(prefix_dir)
    # 1. Map and validate every path before touching anything.
    label, plan, skipped = _plan_sources(game_folder, home)
    zips: dict[Path, zipfile.ZipFile] = {}
    try:
        for src, _, _ in plan:
            if src.zip_path is not None and src.zip_path not in zips:
                zips[src.zip_path] = zipfile.ZipFile(src.zip_path)

        # 2. Every file must match its recorded sha1 and size.
        total = 0
        for src, _, _ in plan:
            total += src.size
            if src.size > _RESTORE_MAX_FILE_BYTES or total > _RESTORE_MAX_TOTAL_BYTES:
                raise LudusaviError(f"Backup too large: {src.original}")
            digest, size = _sha1_stream(_bounded(_source_chunks(src, zips), src.size, src.original))
            if digest != src.digest or size != src.size:
                raise LudusaviError(f"Hash mismatch for {src.original}")

        # 3. Decide per target: new, identical, or an overwrite.
        writes: list[tuple[_Source, Path, Path, tuple[str, ...]]] = []
        overwrites: list[tuple[_Source, Path, Path, tuple[str, ...], str]] = []
        unchanged = kept = 0
        for src, kind, parts in plan:
            base = drive_c if kind == "prefix" else home
            parts = _ci_path(base, parts)
            dst = base.joinpath(*parts)
            if dst.is_symlink() or (dst.exists() and not dst.is_file()):
                kept += 1
                continue
            if not dst.exists():
                writes.append((src, base, dst, parts))
                continue
            if _sha1_stream(_file_chunks(dst))[0] == src.digest:
                unchanged += 1
            elif kind == "prefix":
                overwrites.append((src, base, dst, parts, "/".join(parts)))
            else:
                kept += 1

        # 4. Overwrite only what the safety snapshot holds.
        snapshot: Path | None = None
        if overwrites:
            snapshot = save_sync.auto_snapshot(
                prefix_dir, game_name, "pre-import", keep=0,
                game_dir=game_dir, steam_appid=steam_appid, dest_root=dest_root,
            )
            covered: set[str] = set()
            if snapshot is not None:
                with zipfile.ZipFile(snapshot) as zf:
                    covered = {n.split("/", 1)[1] for n in zf.namelist() if "/" in n}
            for src, base, dst, parts, rel in overwrites:
                if rel in covered:
                    writes.append((src, base, dst, parts))
                else:
                    kept += 1

        # 5. Write atomically, checking the hash again while copying.
        restored = 0
        for src, base, dst, parts in writes:
            if not _safe_parents_within(base, base.resolve(), parts[:-1]):
                kept += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
            tmp = Path(tmp_name)
            try:
                h = hashlib.sha1()  # noqa: S324 - format-defined hash
                with os.fdopen(fd, "wb") as out:
                    for chunk in _bounded(_source_chunks(src, zips), src.size, src.original):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != src.digest:
                    raise LudusaviError(f"Hash mismatch for {src.original}")
                os.replace(tmp, dst)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            restored += 1
    finally:
        for zf in zips.values():
            zf.close()
    return ImportResult(restored, unchanged, kept, skipped, label, snapshot)
