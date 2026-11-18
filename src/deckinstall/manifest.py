"""Per-game manifest (schema v2) with library-relative paths, plus read support for schema v1.

Schema v2 lives in ``<game dir>/.deckinstall.json``. Relative paths are relative to the
library root, i.e. the directory obtained by stripping ``game.dir`` from the manifest's
own folder, so a library keeps working when its disk is mounted somewhere else. Paths
outside the library are stored as ``~/...`` when under $HOME, else absolute.

Schema v1 (``.deckinstall_standalone.json``, absolute paths only) is read and normalized
into the same flat field dict; it is only rewritten as v2 by an explicit migration.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .fsutil import atomic_write_text, is_within

MANIFEST_V2 = ".deckinstall.json"
MANIFEST_V1 = ".deckinstall_standalone.json"
MANIFEST_NAMES = (MANIFEST_V2, MANIFEST_V1)
SCHEMA_VERSION = 2
_MAX_MANIFEST_BYTES = 1024 * 1024


def new_uid() -> str:
    """Return a new stable game identifier."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def find_manifest(game_dir: Path) -> Path | None:
    """Return the game's manifest file (v2 preferred over v1), ignoring symlinks."""
    for name in MANIFEST_NAMES:
        cand = game_dir / name
        if cand.is_file() and not cand.is_symlink():
            return cand
    return None


def is_manifest_name(name: str) -> bool:
    """Return True if name is a recognized manifest filename."""
    return name in MANIFEST_NAMES


def encode_path(path: Path, root: Path | None) -> str:
    """Encode path relative to root when inside it, else as ~/... or absolute."""
    if root is not None and is_within(path, root):
        rel = path.resolve().relative_to(root.resolve())
        return rel.as_posix() if rel.parts else "."
    home = Path.home()
    absolute = Path(path).absolute()
    try:
        rel_home = absolute.relative_to(home)
        return "~/" + rel_home.as_posix() if rel_home.parts else "~"
    except ValueError:
        return str(absolute)


def decode_path(raw: str, root: Path | None) -> Path:
    """Decode a path written by encode_path. Relative paths may not contain '..'."""
    text = raw.strip()
    if not text:
        raise ValueError("empty path")
    if text == "~":
        return Path.home()
    if text.startswith("~/"):
        rest = PurePosixPath(text[2:])
        if ".." in rest.parts:
            raise ValueError(f"path escapes home: {raw!r}")
        return Path.home().joinpath(*rest.parts)
    if text.startswith("/"):
        return Path(text)
    rel = PurePosixPath(text)
    if ".." in rel.parts:
        raise ValueError(f"relative path escapes library root: {raw!r}")
    if root is None:
        raise ValueError(f"relative path without library root: {raw!r}")
    return root.joinpath(*rel.parts) if rel.parts and rel.parts != (".",) else root


def library_root_for(game_dir: Path, rel_game_dir: str) -> Path | None:
    """Derive the library root by stripping the relative game dir from game_dir."""
    rel = PurePosixPath(rel_game_dir.strip())
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts == (".",):
        return None
    # Only the depth matters, so a game folder renamed by hand still finds its root.
    root = game_dir
    for _ in rel.parts:
        root = root.parent
    return root


def _read_json(manifest_path: Path) -> dict[str, Any] | None:
    if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
        logging.warning("Refusing to load symlinked manifest %s", manifest_path)
        return None
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        logging.warning("Manifest too large: %s", manifest_path)
        return None
    raw = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    return raw if isinstance(raw, dict) else None


def _str(value: Any) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _paths(values: Any, root: Path | None) -> list[Path]:
    out: list[Path] = []
    if isinstance(values, list):
        for v in values:
            if isinstance(v, str) and v.strip():
                try:
                    out.append(decode_path(v, root))
                except ValueError:
                    logging.warning("Ignoring invalid manifest path %r", v)
    return out


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    val = raw.get(key)
    return val if isinstance(val, dict) else {}


def _v1_fields(raw: dict[str, Any], game_dir: Path) -> dict[str, Any]:
    sa = raw.get("steam_appid")
    ds = _str(raw.get("desktop_shortcut")).strip()
    cd = _str(raw.get("cache_dir")).strip()
    df = _str(raw.get("desktop_file")).strip()
    return {
        "schema": 1,
        "uid": "",
        "name": _str(raw.get("name")) or game_dir.name,
        "game_dir": game_dir,
        "exe_path": Path(str(raw["exe_path"])),
        "prefix_dir": Path(str(raw["prefix_dir"])),
        "proton_name": _str(raw.get("proton_name")),
        "steam_appid": sa if isinstance(sa, int) and not isinstance(sa, bool) else None,
        "winedlloverrides": _str(raw.get("winedlloverrides")),
        "launch_options": _str(raw.get("launch_options")),
        "launch_script": Path(_str(raw.get("launch_script")) or str(game_dir / "launch.sh")),
        "desktop_file": Path(df) if df else None,
        "desktop_shortcut": Path(ds) if ds else None,
        "native_linux": bool(raw.get("native_linux", False)),
        "cache_dir": Path(cd) if cd else None,
        "meta": {},
    }


def _v2_fields(raw: dict[str, Any], game_dir: Path) -> dict[str, Any]:
    game = _section(raw, "game")
    rel_dir = _str(game.get("dir")).strip()
    if rel_dir.startswith(("/", "~")):
        root = None
    else:
        root = library_root_for(game_dir, rel_dir)
        if root is None:
            raise ValueError(f"invalid game.dir {rel_dir!r}")
    exe_raw = _str(game.get("exe")).strip()
    if not exe_raw:
        raise ValueError("missing game.exe")
    exe_rel = PurePosixPath(exe_raw)
    if exe_rel.is_absolute() or ".." in exe_rel.parts:
        raise ValueError(f"game.exe must be relative to the game dir: {exe_raw!r}")

    source = _section(raw, "source")
    runtime = _section(raw, "runtime")
    proton = _section(runtime, "proton")
    prefix = _section(raw, "prefix")
    cache = _section(raw, "cache")
    created = _section(raw, "created")
    sa = source.get("steam_appid")

    prefix_raw = _str(prefix.get("path")).strip()
    if not prefix_raw:
        raise ValueError("missing prefix.path")
    cache_raw = _str(cache.get("path")).strip()
    script_raw = _str(created.get("launch_script")).strip()
    desktop_files = _paths(created.get("desktop_files"), root)
    shortcuts = _paths(created.get("desktop_shortcuts"), root)

    return {
        "schema": SCHEMA_VERSION,
        "uid": _str(raw.get("id")),
        "name": _str(raw.get("name")) or game_dir.name,
        "game_dir": game_dir,
        "exe_path": game_dir.joinpath(*exe_rel.parts),
        "prefix_dir": decode_path(prefix_raw, root),
        "proton_name": _str(proton.get("name")),
        "steam_appid": sa if isinstance(sa, int) and not isinstance(sa, bool) else None,
        "winedlloverrides": _str(runtime.get("winedlloverrides")),
        "launch_options": _str(runtime.get("launch_options")),
        "launch_script": decode_path(script_raw, root) if script_raw else game_dir / "launch.sh",
        "desktop_file": desktop_files[0] if desktop_files else None,
        "desktop_shortcut": shortcuts[0] if shortcuts else None,
        "native_linux": bool(game.get("native_linux", False)),
        "cache_dir": decode_path(cache_raw, root) if cache_raw else None,
        "meta": {
            "created_at": _str(raw.get("created_at")),
            "app_version": _str(raw.get("app_version")),
            "source": {k: v for k, v in source.items() if k != "steam_appid"},
            "proton_mode": _str(proton.get("mode")) or "pinned",
            **({"proton_previous": _str(proton.get("previous"))} if _str(proton.get("previous")) else {}),
            **({"emulator": _str(runtime.get("emulator"))} if _str(runtime.get("emulator")) else {}),
            "prefix_location": _str(prefix.get("location")) or "library",
            "icons": _paths(created.get("icons"), root),
            "steam": _section(created, "steam"),
            "optiscaler": _section(created, "optiscaler"),
            "logs": _section(raw, "logs"),
        },
    }


def read_manifest_fields(manifest_path: Path) -> dict[str, Any] | None:
    """Read a v1 or v2 manifest into a flat dict of absolute paths, or None if unreadable.

    Keys: schema, uid, name, game_dir, exe_path, prefix_dir, proton_name, steam_appid,
    winedlloverrides, launch_options, launch_script, desktop_file, desktop_shortcut,
    native_linux, cache_dir, meta. Bounds validation is left to the caller.
    """
    try:
        raw = _read_json(manifest_path)
        if raw is None:
            return None
        game_dir = manifest_path.parent.resolve()
        schema = raw.get("schema")
        if schema == SCHEMA_VERSION:
            return _v2_fields(raw, game_dir)
        if schema is None and "exe_path" in raw:
            return _v1_fields(raw, game_dir)
        logging.warning("Unsupported manifest schema %r in %s", schema, manifest_path)
        return None
    except Exception as exc:
        logging.warning("Failed to read manifest %s: %s", manifest_path, exc)
        return None


def build_manifest_v2(fields: dict[str, Any], library_root: Path | None) -> dict[str, Any]:
    """Build a v2 manifest payload from the flat field dict used by read_manifest_fields."""
    game_dir: Path = fields["game_dir"]
    root = library_root if library_root is not None and is_within(game_dir, library_root, strict=True) else None
    meta: dict[str, Any] = dict(fields.get("meta") or {})

    def enc(p: Path | None) -> str:
        return encode_path(p, root) if p is not None else ""

    exe_rel = Path(fields["exe_path"]).resolve().relative_to(game_dir.resolve()).as_posix()
    source = dict(meta.get("source") or {})
    if fields.get("steam_appid") is not None:
        source["steam_appid"] = fields["steam_appid"]
    proton_name = str(fields.get("proton_name") or "")
    created_at = str(meta.get("created_at") or "") or now_iso()
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": str(fields.get("uid") or "") or new_uid(),
        "name": fields["name"],
        "created_at": created_at,
        "updated_at": now_iso(),
        "app_version": __version__,
        "game": {
            "dir": enc(game_dir),
            "exe": exe_rel,
            "native_linux": bool(fields.get("native_linux", False)),
        },
        "source": source,
        "runtime": {
            "proton": {
                "mode": str(meta.get("proton_mode") or ("pinned" if proton_name else "default")),
                "name": proton_name,
                **({"previous": str(meta["proton_previous"])} if meta.get("proton_previous") else {}),
            },
            "winedlloverrides": str(fields.get("winedlloverrides") or ""),
            "launch_options": str(fields.get("launch_options") or ""),
            **({"emulator": str(meta["emulator"])} if meta.get("emulator") else {}),
        },
        "prefix": {
            "path": enc(fields["prefix_dir"]),
            "location": str(meta.get("prefix_location") or "library"),
        },
        "cache": {"path": enc(fields.get("cache_dir"))},
        "created": {
            "launch_script": enc(fields.get("launch_script")),
            "desktop_files": [enc(fields["desktop_file"])] if fields.get("desktop_file") else [],
            "desktop_shortcuts": [enc(fields["desktop_shortcut"])] if fields.get("desktop_shortcut") else [],
            "icons": [enc(p) for p in meta.get("icons") or []],
            "steam": dict(meta.get("steam") or {}),
            "optiscaler": dict(meta.get("optiscaler") or {}),
        },
        "logs": dict(meta.get("logs") or {}),
    }
    return payload


def write_manifest_v2(fields: dict[str, Any], library_root: Path | None) -> tuple[Path, dict[str, Any]]:
    """Write <game_dir>/.deckinstall.json atomically and return (path, payload)."""
    payload = build_manifest_v2(fields, library_root)
    path = Path(fields["game_dir"]) / MANIFEST_V2
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path, payload
