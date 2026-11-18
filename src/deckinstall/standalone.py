"""Standalone (non-Steam) game management via umu-run and bidirectional Steam migration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import library as _lib
from .extractor import check_disk_space
from .fsutil import (
    atomic_write_bytes,
    copy_or_move_dir_contents,
    is_within,
    move_dir_safely,
    remove_path_or_tree,
    sanitize_dir_name,
    slugify,
)
from .library import (
    Library,
    ensure_library,
    find_library_root,
    forget_game,
    internal_library_root,
    list_mount_points,
    read_library_file,
    record_game,
)
from .manifest import (
    MANIFEST_V1,
    MANIFEST_V2,
    decode_path,
    encode_path,
    find_manifest,
    is_manifest_name,
    new_uid,
    now_iso,
    read_manifest_fields,
    write_manifest_v2,
)
from .scanner import detect_steam_appid, detect_steam_emulator, gog_product_id
from .steam_paths import (
    SteamPaths,
    dir_size_bytes,
    find_steam_root,
    list_steam_users,
    resolve_proton_dir,
)
from .steam_vdf import (
    _SHELL_OPS,
    ShortcutEntry,
    _atomic_write_text,
    _is_env_assignment,
    _raw_shell_tokens,
    _strip_vdf_quotes,
    _validate_launch_options,
    add_or_update_shortcut,
    apply_launch_presets,
    get_shortcut_proton,
    infer_game_root_from_exe,
    load_shortcuts,
    remove_shortcut,
    set_shortcut_proton,
    sync_prefix_symlinks,
)

_DEFAULT_GAMES_DIR = Path.home() / "Games"
_DEFAULT_PREFIXES_DIR = _DEFAULT_GAMES_DIR / ".prefixes"
_DEFAULT_APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"

GAMES_DIR = _DEFAULT_GAMES_DIR
PREFIXES_DIR = _DEFAULT_PREFIXES_DIR
APPLICATIONS_DIR = _DEFAULT_APPLICATIONS_DIR
UMU_RUN_PATH = Path.home() / ".local" / "share" / "DeckInstall" / "runtime" / "umu" / "umu-run"
STANDALONE_MANIFEST = MANIFEST_V1  # legacy (schema v1) manifest filename
_LIBRARY_INDEX_DIR = ".deckinstall-library"


def get_applications_dir() -> Path:
    """Return the desktop applications directory (respecting monkeypatching and XDG_DATA_HOME)."""
    if APPLICATIONS_DIR != _DEFAULT_APPLICATIONS_DIR:
        return APPLICATIONS_DIR
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser() / "applications"
    return Path.home() / ".local" / "share" / "applications"


def _legacy_layout() -> bool:
    """True when the games folder is overridden (DECKINSTALL_GAMES_DIR, GAMES_DIR or a patched
    get_games_dir): the old ~/Games layout (<dir>, <dir>/.prefixes, <dir>/.caches) is used then."""
    return get_games_dir() != internal_library_root() / _lib.GAMES


def uses_library_layout() -> bool:
    """True when new installs go to a DeckInstall library (not to an overridden games folder)."""
    return not _legacy_layout()


def get_games_dir() -> Path:
    """Return the games directory of the internal library (~/DeckInstall/Games).

    DECKINSTALL_GAMES_DIR or a patched GAMES_DIR selects the old layout (<dir>, <dir>/.prefixes).
    """
    env_dir = os.environ.get("DECKINSTALL_GAMES_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if GAMES_DIR != _DEFAULT_GAMES_DIR:
        return GAMES_DIR
    return internal_library_root() / _lib.GAMES


def get_prefixes_dir() -> Path:
    """Return the Wine prefixes directory of the internal library."""
    env_dir = os.environ.get("DECKINSTALL_GAMES_DIR", "").strip()
    if env_dir:
        return get_games_dir() / ".prefixes"
    if PREFIXES_DIR != _DEFAULT_PREFIXES_DIR:
        return PREFIXES_DIR
    if _legacy_layout():
        return get_games_dir() / ".prefixes"
    return internal_library_root() / _lib.PREFIXES


def get_shader_caches_dir() -> Path:
    """Return the shader caches directory of the internal library."""
    if _legacy_layout():
        return get_games_dir() / ".caches"
    return internal_library_root() / _lib.CACHES


def get_legacy_games_dir() -> Path:
    """Return the pre-library games folder (~/Games), still scanned until games are migrated."""
    return get_games_dir() if _legacy_layout() else _DEFAULT_GAMES_DIR


def get_legacy_prefixes_dir() -> Path:
    """Return the pre-library prefixes folder (~/Games/.prefixes), also used for Steam prefix links."""
    return get_prefixes_dir() if _legacy_layout() else _DEFAULT_PREFIXES_DIR


def _library_roots_on_disks() -> list[Path]:
    """Return library roots on mounted disks (not the internal one)."""
    roots: list[Path] = []
    if _legacy_layout():
        return roots
    for mount in list_mount_points():
        root = mount / _lib.LIBRARY_DIRNAME
        if read_library_file(root) is not None:
            roots.append(root)
    return roots


def _managed_roots(sub: str, game_dir: Path | None = None) -> list[Path]:
    """Folders where DeckInstall may keep prefixes (sub=Prefixes) or caches (sub=Caches)."""
    if sub == _lib.PREFIXES:
        roots = [get_prefixes_dir(), get_legacy_prefixes_dir()]
    else:
        roots = [get_shader_caches_dir(), get_legacy_games_dir() / ".caches"]
    if not _legacy_layout():
        roots.append(internal_library_root() / sub)
        roots.extend(r / sub for r in _library_roots_on_disks())
    if game_dir is not None:
        lib_root = find_library_root(game_dir)
        if lib_root is not None:
            roots.append(lib_root / sub)
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def get_game_cache_dir(game_dir: Path) -> Path:
    """Return the centralized shader cache directory for a standalone game directory."""
    safe_name = sanitize_dir_name(game_dir.name)
    try:
        resolved = str(game_dir.resolve())
    except OSError:
        resolved = str(game_dir)
    dir_hash = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return get_shader_caches_dir() / f"{safe_name}-{dir_hash}"


# "lsfg-vk" is no longer managed by the app, but legacy launch.sh files may still contain it.
_KNOWN_WRAPPERS = frozenset({"gamemoderun", "lsfg-vk", "mangohud"})


@dataclass
class StandaloneGame:
    """Metadata for a standalone game managed outside Steam via umu-run."""

    name: str
    game_dir: Path
    exe_path: Path
    prefix_dir: Path
    proton_name: str
    steam_appid: int | None
    winedlloverrides: str
    launch_options: str
    launch_script: Path
    desktop_file: Path
    desktop_shortcut: Path | None = None
    native_linux: bool = False
    cache_dir: Path | None = None
    uid: str = ""
    schema: int = 1
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrphanCacheEntry:
    """An orphaned shader cache directory under get_shader_caches_dir()."""

    path: Path
    size_bytes: int
    name: str = ""


def _strip_control_chars(value: str) -> str:
    """Remove CR, LF, and ASCII control characters from a string."""
    return "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)


def _get_protected_dirs() -> set[Path]:
    """Return resolved paths of system/user directories that must never be moved or deleted."""
    home = Path.home()
    candidates = (
        home,
        home / "Downloads",
        home / "Desktop",
        get_desktop_dir(),
        home / "Documents",
        home / "Music",
        home / "Pictures",
        home / "Videos",
        home / "Public",
        home / "Templates",
        home / "Games",
        get_games_dir(),
        home / "Syncthing",
        Path("/"),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/mnt"),
        Path("/media"),
    )
    protected: set[Path] = set()
    for candidate in candidates:
        try:
            protected.add(candidate.resolve())
        except OSError:
            protected.add(candidate)
    return protected


def _is_protected_dir(path: Path) -> bool:
    """Return True if path is a protected directory or a Syncthing folder root."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in _get_protected_dirs():
        return True
    try:
        return (resolved / ".stfolder").exists()
    except OSError:
        return False


def _resolve_proton_path(proton_name: str) -> str:
    """Resolve a Proton/GE-Proton tool name to a filesystem path for PROTONPATH when possible."""
    name = proton_name.strip()
    if not name:
        return "GE-Proton"
    steam_root = find_steam_root() or (Path.home() / ".local" / "share" / "Steam")
    resolved = resolve_proton_dir(name, steam_root)
    return str(resolved) if resolved is not None else name


def resolve_standalone_proton_fallback(game: StandaloneGame) -> tuple[Path | None, str | None]:
    """Resolve game.proton_name or fall back to the newest installed GE-Proton11+/10 tool."""
    from .runtime_manager import list_ge_proton10_dirs, list_ge_proton11_dirs

    if game.proton_name.strip():
        resolved_str = _resolve_proton_path(game.proton_name)
        resolved_path = Path(resolved_str)
        if resolved_path.is_dir() and (resolved_path / "proton").is_file():
            return (resolved_path, None)

    candidates = list_ge_proton11_dirs() or list_ge_proton10_dirs()
    if candidates:
        fallback_dir = candidates[0]
        return (fallback_dir, fallback_dir.name)
    return (None, None)


def resolve_umu_game_id(
    steam_appid: int | None,
    store: str = "",
    exe_path: Path | None = None,
    game_title: str = "",
    db_path: Path | None = None,
    codename: str = "",
) -> tuple[str, str]:
    """Return (GAMEID, STORE) for umu-run given an optional Steam AppID, store and store id."""
    cleaned_store = store.strip()
    if isinstance(steam_appid, int) and not isinstance(steam_appid, bool) and steam_appid > 0:
        return f"umu-{steam_appid}", cleaned_store or "steam"

    if codename.strip() and cleaned_store:
        from .runtime_manager import lookup_umu_codename

        by_id = lookup_umu_codename(codename, cleaned_store, db_path=db_path)
        if by_id is not None and by_id.umu_id.strip():
            return by_id.umu_id.strip(), cleaned_store

    if exe_path is not None or game_title.strip():
        from .runtime_manager import lookup_umu_database

        hit = lookup_umu_database(
            exe_path=exe_path,
            game_title=game_title,
            store=cleaned_store,
            db_path=db_path,
        )
        if hit is not None and hit.umu_id.strip():
            return hit.umu_id.strip(), cleaned_store or hit.store.strip() or "none"

    return "umu-default", cleaned_store or "none"


estimate_dir_size = dir_size_bytes


def _parse_launch_tokens(
    launch_options: str,
    winedlloverrides: str = "",
) -> tuple[list[tuple[str, str]], list[str], list[str], str]:
    """Parse Steam-style launch_options into (env_pairs, wrappers, game_args, effective_dll_overrides)."""
    _validate_launch_options(launch_options)
    _validate_launch_options(winedlloverrides)
    try:
        tokens = _raw_shell_tokens(launch_options) if launch_options.strip() else []
    except ValueError:
        tokens = launch_options.split()

    if "%command%" in tokens:
        idx = tokens.index("%command%")
        before = tokens[:idx]
        after = tokens[idx + 1 :]
    else:
        before = [
            t
            for t in tokens
            if _is_env_assignment(t) or t.strip("\"'") in _KNOWN_WRAPPERS
        ]
        after = [
            t
            for t in tokens
            if not (_is_env_assignment(t) or t.strip("\"'") in _KNOWN_WRAPPERS)
        ]

    env_pairs: list[tuple[str, str]] = []
    wrappers: list[str] = []
    parsed_dll = ""

    for tok in before:
        if _is_env_assignment(tok):
            k, v = tok.split("=", 1)
            v_unquoted = _strip_vdf_quotes(v)
            if k == "WINEDLLOVERRIDES":
                parsed_dll = v_unquoted
                continue
            env_pairs.append((k, v_unquoted))
        else:
            wrappers.append(_strip_vdf_quotes(tok))

    effective_dll = winedlloverrides.strip() or parsed_dll
    game_args = [_strip_vdf_quotes(t) for t in after]
    return env_pairs, wrappers, game_args, effective_dll


def _quote_game_arg(arg: str) -> str:
    """Quote a game argument unless it is a shell operator (excluding %command%)."""
    if arg in _SHELL_OPS and arg != "%command%":
        return arg
    return shlex.quote(arg)


def _store_of(game: StandaloneGame) -> str:
    return str((game.meta.get("source") or {}).get("store") or "")


def _codename_of(game: StandaloneGame) -> str:
    return str((game.meta.get("source") or {}).get("codename") or "")


def merge_dll_overrides(user: str, extra: str) -> str:
    """Add the extra WINEDLLOVERRIDES entries the user did not set already (user entries win)."""
    from .steam_vdf import merge_winedlloverride

    def names(spec: str) -> set[str]:
        out: set[str] = set()
        for entry in spec.split(";"):
            part = entry.split("=", 1)[0]
            out.update(n.strip().lower().removesuffix(".dll") for n in part.split(",") if n.strip())
        return out

    taken = names(user)
    merged = user.strip()
    for entry in extra.split(";"):
        if not entry.strip():
            continue
        dll, _, mode = entry.partition("=")
        if dll.strip().lower() in taken:
            continue
        merged = merge_winedlloverride(merged, dll.strip(), mode.strip() or "n,b")
    return merged


def _write_launch_script(
    script_path: Path,
    exe_path: Path,
    prefix_dir: Path,
    proton_name: str,
    winedlloverrides: str,
    launch_options: str,
    native_linux: bool = False,
    steam_appid: int | None = None,
    game_name: str = "",
    cache_dir: Path | None = None,
    store: str = "",
    codename: str = "",
) -> None:
    """Generate an executable launch.sh script invoking umu-run or a native Linux binary."""
    env_pairs, wrappers, game_args, effective_dll = _parse_launch_tokens(
        launch_options, "" if native_linux else winedlloverrides
    )
    user_env_keys = {k for k, _ in env_pairs}

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(exe_path.parent))}",
    ]

    if not native_linux:
        proton_path = _resolve_proton_path(proton_name)
        game_id, store = resolve_umu_game_id(
            steam_appid,
            store=store,
            exe_path=exe_path,
            game_title=game_name,
            codename=codename,
        )
        lines.extend([
            f"export WINEPREFIX={shlex.quote(str(prefix_dir))}",
            f"export PROTONPATH={shlex.quote(proton_path)}",
            f"export GAMEID={shlex.quote(game_id)}",
            f"export STORE={shlex.quote(store)}",
        ])
        if effective_dll:
            lines.append(f"export WINEDLLOVERRIDES={shlex.quote(effective_dll)}")

    seen_keys = {"WINEPREFIX", "PROTONPATH", "GAMEID", "STORE", "WINEDLLOVERRIDES"}
    if cache_dir is not None and not cache_dir.is_symlink():
        dxvk_cache = cache_dir / "dxvk"
        mesa_cache = cache_dir / "mesa"
        dxvk_cache.mkdir(parents=True, exist_ok=True)
        mesa_cache.mkdir(parents=True, exist_ok=True)
        if not native_linux and "DXVK_STATE_CACHE_PATH" not in user_env_keys:
            lines.append(f"export DXVK_STATE_CACHE_PATH={shlex.quote(str(dxvk_cache))}")
            seen_keys.add("DXVK_STATE_CACHE_PATH")
        if "MESA_SHADER_CACHE_DIR" not in user_env_keys:
            lines.append(f"export MESA_SHADER_CACHE_DIR={shlex.quote(str(mesa_cache))}")
            seen_keys.add("MESA_SHADER_CACHE_DIR")

    for key, val in env_pairs:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        lines.append(f"export {key}={shlex.quote(val)}")

    cmd_parts = [shlex.quote(w) for w in wrappers]
    if not native_linux:
        cmd_parts.append(shlex.quote(str(UMU_RUN_PATH)))
    cmd_parts.append(shlex.quote(str(exe_path)))
    cmd_parts.extend(_quote_game_arg(arg) for arg in game_args)
    lines.append("exec " + " ".join(cmd_parts))

    _atomic_write_text(script_path, "\n".join(lines) + "\n")
    script_path.chmod(0o755)


def _desktop_quote(path: Path) -> str:
    """Quote a path for .desktop Exec= value."""
    s = str(path).replace('"', '\\"')
    return f'"{s}"' if any(ch in s for ch in (" ", "\t", "'", '"')) else s


def _desktop_points_to_game_dir(desktop_path: Path, target_game_dir: Path) -> bool:
    """Return True if an existing .desktop file points to target_game_dir."""
    try:
        target_resolved = target_game_dir.resolve()
        text = desktop_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    exec_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Path="):
            path_val = stripped[5:].strip()
            if path_val:
                try:
                    return Path(path_val).resolve() == target_resolved
                except OSError:
                    return False
        elif stripped.startswith("Exec="):
            exec_line = stripped[5:].strip()
    if exec_line:
        try:
            tokens = shlex.split(exec_line)
        except ValueError:
            tokens = [exec_line.strip('"')]
        if tokens:
            try:
                return Path(tokens[0]).parent.resolve() == target_resolved
            except OSError:
                return False
    return False


def _resolve_desktop_file_path(clean_name: str, target_game_dir: Path) -> Path:
    """Return a unique .desktop path, disambiguating if slug collides with another game_dir."""
    apps_dir = get_applications_dir()
    slug = slugify(clean_name)
    candidate = apps_dir / f"deckinstall-{slug}.desktop"
    if not candidate.exists() or _desktop_points_to_game_dir(candidate, target_game_dir):
        return candidate
    try:
        key = str(target_game_dir.resolve())
    except OSError:
        key = str(target_game_dir)
    short_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    hashed = apps_dir / f"deckinstall-{slug}-{short_hash}.desktop"
    if not hashed.exists() or _desktop_points_to_game_dir(hashed, target_game_dir):
        return hashed
    idx = 2
    while True:
        numbered = apps_dir / f"deckinstall-{slug}-{short_hash}-{idx}.desktop"
        if not numbered.exists() or _desktop_points_to_game_dir(numbered, target_game_dir):
            return numbered
        idx += 1


def _resolve_desktop_shortcut_path(safe_dir_name: str, target_game_dir: Path) -> Path:
    """Return a clean Desktop .desktop path, disambiguating with (N) only on collision."""
    desktop_dir = get_desktop_dir()
    candidate = desktop_dir / f"{safe_dir_name}.desktop"
    if not candidate.exists() or _desktop_points_to_game_dir(candidate, target_game_dir):
        return candidate
    idx = 2
    while True:
        numbered = desktop_dir / f"{safe_dir_name} ({idx}).desktop"
        if not numbered.exists() or _desktop_points_to_game_dir(numbered, target_game_dir):
            return numbered
        idx += 1


def get_desktop_dir() -> Path:
    """Return the user's Desktop directory (via xdg-user-dir DESKTOP or ~/Desktop)."""
    xdg = shutil.which("xdg-user-dir")
    if xdg:
        try:
            res = subprocess.run(
                [xdg, "DESKTOP"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            out = res.stdout.strip()
            if res.returncode == 0 and out:
                return Path(out).expanduser()
        except Exception:
            pass
    return Path.home() / "Desktop"


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    """Convert a PE RVA to a raw file offset using section headers."""
    for v_size, v_addr, raw_size, raw_ptr in sections:
        span = max(v_size, raw_size)
        if v_addr <= rva < v_addr + span:
            return raw_ptr + (rva - v_addr)
    return None


def _parse_rsrc_entries(data: bytes, rsrc_off: int, dir_off: int) -> list[tuple[int, int]]:
    """Return (id_or_name, offset_to_data_or_subdir) entries from a PE resource directory."""
    if dir_off < 0 or dir_off + 16 > len(data):
        return []
    num_named, num_id = struct.unpack_from("<HH", data, dir_off + 12)
    total = num_named + num_id
    entries: list[tuple[int, int]] = []
    base = dir_off + 16
    for i in range(total):
        pos = base + i * 8
        if pos + 8 > len(data):
            break
        name_or_id, off_data = struct.unpack_from("<II", data, pos)
        entries.append((name_or_id, off_data))
    return entries


_MAX_PE_SECTIONS = 96
_MAX_PE_OPT_SIZE = 4096
_MAX_PE_RSRC_BYTES = 16 * 1024 * 1024


def extract_pe_icon(exe_path: Path, out_ico: Path) -> Path | None:
    """Extract the primary embedded icon group from a Windows PE .exe into a .ico file."""
    try:
        if exe_path.is_symlink() or out_ico.is_symlink() or not exe_path.is_file():
            return None
        with open(exe_path, "rb") as fh:
            dos = fh.read(0x40)
            if len(dos) < 0x40 or dos[:2] != b"MZ":
                return None
            pe_off = struct.unpack_from("<I", dos, 0x3C)[0]
            fh.seek(pe_off)
            pe_hdr = fh.read(24)
            if len(pe_hdr) < 24 or pe_hdr[:4] != b"PE\x00\x00":
                return None
            num_sections = struct.unpack_from("<H", pe_hdr, 6)[0]
            opt_size = struct.unpack_from("<H", pe_hdr, 20)[0]
            if (
                num_sections == 0
                or num_sections > _MAX_PE_SECTIONS
                or opt_size < 2
                or opt_size > _MAX_PE_OPT_SIZE
            ):
                return None
            opt_hdr = fh.read(opt_size)
            if len(opt_hdr) < opt_size:
                return None
            magic = struct.unpack_from("<H", opt_hdr, 0)[0]
            if magic == 0x10B and opt_size >= 112:
                rsrc_rva, _ = struct.unpack_from("<II", opt_hdr, 96 + 2 * 8)
            elif magic == 0x20B and opt_size >= 128:
                rsrc_rva, _ = struct.unpack_from("<II", opt_hdr, 112 + 2 * 8)
            else:
                return None
            if not rsrc_rva:
                return None

            sec_bytes = fh.read(num_sections * 40)
            sections: list[tuple[int, int, int, int]] = []
            for i in range(len(sec_bytes) // 40):
                v_size, v_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", sec_bytes, i * 40 + 8)
                sections.append((v_size, v_addr, raw_size, raw_ptr))

            rsrc_sec: tuple[int, int, int, int] | None = None
            for v_size, v_addr, raw_size, raw_ptr in sections:
                span = max(v_size, raw_size)
                if v_addr <= rsrc_rva < v_addr + span:
                    rsrc_sec = (v_size, v_addr, raw_size, raw_ptr)
                    break
            if rsrc_sec is None:
                return None
            _v_size, rsrc_vaddr, rsrc_raw_size, rsrc_raw_ptr = rsrc_sec
            if rsrc_raw_size == 0 or rsrc_raw_size > _MAX_PE_RSRC_BYTES:
                return None
            fh.seek(rsrc_raw_ptr)
            rsrc_data = fh.read(rsrc_raw_size)

        rsrc_local_off = rsrc_rva - rsrc_vaddr
        if rsrc_local_off < 0 or rsrc_local_off >= len(rsrc_data):
            return None

        # Collect RT_ICON (3) blobs keyed by icon ID, and first RT_GROUP_ICON (14)
        rt_icons: dict[int, bytes] = {}
        group_blob: bytes | None = None

        for type_id, type_sub in _parse_rsrc_entries(rsrc_data, rsrc_local_off, rsrc_local_off):
            if not (type_sub & 0x80000000):
                continue
            l2_off = rsrc_local_off + (type_sub & 0x7FFFFFFF)
            if type_id == 3:  # RT_ICON
                for icon_id, icon_sub in _parse_rsrc_entries(rsrc_data, rsrc_local_off, l2_off):
                    if not (icon_sub & 0x80000000):
                        continue
                    l3_off = rsrc_local_off + (icon_sub & 0x7FFFFFFF)
                    for _lang, data_entry_rel in _parse_rsrc_entries(rsrc_data, rsrc_local_off, l3_off):
                        if data_entry_rel & 0x80000000:
                            continue
                        de_off = rsrc_local_off + data_entry_rel
                        if de_off + 16 <= len(rsrc_data):
                            blob_rva, blob_size = struct.unpack_from("<II", rsrc_data, de_off)
                            blob_off = _rva_to_offset(blob_rva, sections)
                            if blob_off is not None and blob_off >= rsrc_raw_ptr:
                                local_blob_off = blob_off - rsrc_raw_ptr
                                if local_blob_off + blob_size <= len(rsrc_data):
                                    rt_icons[icon_id] = rsrc_data[local_blob_off : local_blob_off + blob_size]
                                    break
            elif type_id == 14 and group_blob is None:  # RT_GROUP_ICON
                for _grp_id, grp_sub in _parse_rsrc_entries(rsrc_data, rsrc_local_off, l2_off):
                    if not (grp_sub & 0x80000000):
                        continue
                    l3_off = rsrc_local_off + (grp_sub & 0x7FFFFFFF)
                    for _lang, data_entry_rel in _parse_rsrc_entries(rsrc_data, rsrc_local_off, l3_off):
                        if data_entry_rel & 0x80000000:
                            continue
                        de_off = rsrc_local_off + data_entry_rel
                        if de_off + 16 <= len(rsrc_data):
                            blob_rva, blob_size = struct.unpack_from("<II", rsrc_data, de_off)
                            blob_off = _rva_to_offset(blob_rva, sections)
                            if blob_off is not None and blob_off >= rsrc_raw_ptr:
                                local_blob_off = blob_off - rsrc_raw_ptr
                                if local_blob_off + blob_size <= len(rsrc_data):
                                    group_blob = rsrc_data[local_blob_off : local_blob_off + blob_size]
                                    break
                    if group_blob is not None:
                        break

        if not group_blob or len(group_blob) < 6:
            return None
        _res, ico_type, count = struct.unpack_from("<HHH", group_blob, 0)
        if ico_type != 1 or count == 0:
            return None

        valid_entries: list[tuple[bytes, bytes]] = []
        for i in range(count):
            e_off = 6 + i * 14
            if e_off + 14 > len(group_blob):
                break
            entry_12 = group_blob[e_off : e_off + 12]
            n_id = struct.unpack_from("<H", group_blob, e_off + 12)[0]
            blob = rt_icons.get(n_id)
            if blob:
                valid_entries.append((entry_12, blob))

        if not valid_entries:
            return None

        header = struct.pack("<HHH", 0, 1, len(valid_entries))
        dir_table = bytearray()
        img_data = bytearray()
        offset = 6 + 16 * len(valid_entries)
        for entry_12, blob in valid_entries:
            # Update dwBytesInRes to actual blob length for consistency
            w, h, cc, res, planes, bpp = struct.unpack_from("<BBBBHH", entry_12, 0)
            dir_table.extend(struct.pack("<BBBBHHII", w, h, cc, res, planes, bpp, len(blob), offset))
            img_data.extend(blob)
            offset += len(blob)

        atomic_write_bytes(out_ico, header + bytes(dir_table) + bytes(img_data))
        return out_ico
    except Exception as exc:
        logging.debug("PE icon extraction skipped for %s: %s", exe_path, exc)
        return None


_ICON_SCAN_SKIP_DIRS = frozenset({".prefixes", "drive_c", "__pycache__"})


def _safe_file_size(path: Path) -> int:
    """Return file size in bytes, or 0 if unavailable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _scan_nested_icon(target_game_dir: Path, exe_stem_lower: str) -> Path | None:
    """Scan up to depth 2 inside target_game_dir for .ico or icon*.png / <exe>.png."""
    ico_candidates: list[Path] = []
    png_candidates: list[Path] = []
    queue: list[tuple[Path, int]] = [(target_game_dir, 0)]

    while queue:
        current_dir, depth = queue.pop(0)
        try:
            entries = list(current_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < 2 and entry.name not in _ICON_SCAN_SKIP_DIRS:
                    queue.append((entry, depth + 1))
                continue
            if depth == 0 or not entry.is_file():
                continue
            name_lower = entry.name.lower()
            suffix_lower = entry.suffix.lower()
            if suffix_lower == ".ico":
                ico_candidates.append(entry)
            elif suffix_lower == ".png" and (
                name_lower.startswith("icon") or name_lower == f"{exe_stem_lower}.png"
            ):
                png_candidates.append(entry)

    if ico_candidates:
        ico_candidates.sort(key=lambda p: (-_safe_file_size(p), str(p).lower()))
        return ico_candidates[0]
    if png_candidates:
        png_candidates.sort(key=lambda p: str(p).lower())
        return png_candidates[0]
    return None


def resolve_game_icon(
    exe_path: Path,
    target_game_dir: Path,
    steam_appid: int | None = None,
    explicit_icon: Path | None = None,
) -> Path | None:
    """Resolve or extract a local icon file for a standalone game's .desktop launcher."""
    if explicit_icon is not None and not explicit_icon.is_symlink() and explicit_icon.is_file():
        try:
            if is_within(explicit_icon.resolve(), target_game_dir.resolve()):
                return explicit_icon
        except OSError:
            pass
        dest = target_game_dir / f".deckinstall_icon{explicit_icon.suffix.lower()}"
        if not dest.is_symlink():
            try:
                target_game_dir.mkdir(parents=True, exist_ok=True)
                if explicit_icon.resolve() != dest.resolve():
                    atomic_write_bytes(dest, explicit_icon.read_bytes())
                return dest
            except OSError:
                return explicit_icon

    # 1) Existing .deckinstall_icon.{ico,png,jpg,svg}
    for ext in (".ico", ".png", ".jpg", ".svg"):
        cand = target_game_dir / f".deckinstall_icon{ext}"
        if not cand.is_symlink() and cand.is_file():
            return cand

    exe_stem_lower = exe_path.stem.lower()
    try:
        root_files = [p for p in target_game_dir.iterdir() if not p.is_symlink() and p.is_file()]
    except OSError:
        root_files = []

    # 2) Root-level *.ico (preferring icon.ico or {exe_path.stem}.ico, else largest/first)
    root_icos = [p for p in root_files if p.suffix.lower() == ".ico"]
    if root_icos:
        ico_by_name = {p.name.lower(): p for p in root_icos}
        for preferred in ("icon.ico", f"{exe_stem_lower}.ico"):
            if preferred in ico_by_name:
                return ico_by_name[preferred]
        root_icos.sort(key=lambda p: (-_safe_file_size(p), p.name.lower()))
        return root_icos[0]

    # 3) Root-level icon PNGs (icon.png, {exe_path.stem}.png, game.png, app.png)
    png_by_name = {p.name.lower(): p for p in root_files if p.suffix.lower() == ".png"}
    for preferred in ("icon.png", f"{exe_stem_lower}.png", "game.png", "app.png"):
        if preferred in png_by_name:
            return png_by_name[preferred]

    # 4) Embedded PE icon extraction for Windows executables
    extracted = extract_pe_icon(exe_path, target_game_dir / ".deckinstall_icon.ico")
    if extracted is not None and extracted.is_file():
        return extracted

    # 5) Depth <= 2 scan inside target_game_dir
    nested = _scan_nested_icon(target_game_dir, exe_stem_lower)
    if nested is not None:
        return nested

    # 6) Fallback to Steam header preview
    if steam_appid:
        try:
            from .artwork import fetch_header_preview

            raw = fetch_header_preview(int(steam_appid))
            if raw:
                out_img = target_game_dir / ".deckinstall_icon.jpg"
                if out_img.is_symlink():
                    return None
                atomic_write_bytes(out_img, raw)
                return out_img
        except Exception as exc:
            logging.debug("Steam header icon fallback failed for %s: %s", steam_appid, exc)
    return None


def _write_desktop_file(
    desktop_path: Path,
    game_name: str,
    launch_script: Path,
    game_dir: Path,
    icon_path: Path | None = None,
    executable: bool = False,
    startup_wm_class: str = "",
) -> None:
    """Create or update a .desktop launcher file."""
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    clean_name = _strip_control_chars(game_name)
    clean_game_dir = _strip_control_chars(str(game_dir))
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={clean_name}",
        f"Exec={_desktop_quote(launch_script)}",
        f"Path={clean_game_dir}",
        "Terminal=false",
        "Categories=Game;",
        "StartupNotify=true",
    ]
    clean_wm_class = _strip_control_chars(startup_wm_class.strip())
    if clean_wm_class:
        lines.append(f"StartupWMClass={clean_wm_class}")
    if icon_path is not None and icon_path.is_file():
        clean_icon = _strip_control_chars(str(icon_path))
        if clean_icon:
            lines.append(f"Icon={clean_icon}")
    _atomic_write_text(desktop_path, "\n".join(lines) + "\n")
    if executable:
        try:
            desktop_path.chmod(0o755)
        except OSError:
            pass
        if shutil.which("gio"):
            try:
                subprocess.run(
                    ["gio", "set", str(desktop_path), "metadata::trusted", "true"],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            except Exception:
                pass


def _game_fields(game: StandaloneGame) -> dict[str, Any]:
    return {
        "uid": game.uid,
        "name": game.name,
        "game_dir": game.game_dir,
        "exe_path": game.exe_path,
        "prefix_dir": game.prefix_dir,
        "proton_name": game.proton_name,
        "steam_appid": game.steam_appid,
        "winedlloverrides": game.winedlloverrides,
        "launch_options": game.launch_options,
        "launch_script": game.launch_script,
        "desktop_file": game.desktop_file if game.desktop_file.is_file() else None,
        "desktop_shortcut": game.desktop_shortcut,
        "native_linux": game.native_linux,
        "cache_dir": game.cache_dir,
        "meta": game.meta,
    }


def _startup_wm_class(game: StandaloneGame) -> str:
    if game.native_linux:
        return game.exe_path.name
    if game.steam_appid and game.steam_appid > 0:
        return f"steam_app_{game.steam_appid}"
    return "steam_app_0"


def refresh_integration_files(game: StandaloneGame) -> None:
    """Rewrite launch.sh and the existing .desktop launchers after the game's paths changed."""
    _write_launch_script(
        game.launch_script,
        game.exe_path,
        game.prefix_dir,
        game.proton_name,
        game.winedlloverrides,
        game.launch_options,
        native_linux=game.native_linux,
        steam_appid=game.steam_appid,
        game_name=game.name,
        cache_dir=game.cache_dir,
        store=_store_of(game),
        codename=_codename_of(game),
    )
    targets = [(game.desktop_file, False)]
    if game.desktop_shortcut is not None:
        targets.append((game.desktop_shortcut, True))
    existing = [(p, ex) for p, ex in targets if p.is_file() and not p.is_symlink()]
    if not existing:
        return
    icon = resolve_game_icon(game.exe_path, game.game_dir, steam_appid=game.steam_appid)
    for path, executable in existing:
        _write_desktop_file(
            path,
            game.name,
            game.launch_script,
            game.game_dir,
            icon_path=icon,
            executable=executable,
            startup_wm_class=_startup_wm_class(game),
        )


def change_standalone_exe(game_dir: Path, new_exe: Path) -> StandaloneGame:
    """Point a game at another executable inside its folder and rewrite its launchers."""
    from .scanner import is_native_linux_exe

    manifest = find_manifest(game_dir)
    game = _load_manifest(manifest) if manifest is not None else None
    if game is None:
        raise FileNotFoundError(f"Standalone manifest not found in {game_dir}")
    if not new_exe.is_file() or not is_within(new_exe, game.game_dir, strict=True):
        raise ValueError(f"The executable must be a file inside the game folder: {new_exe}")
    native = is_native_linux_exe(new_exe)
    if native != game.native_linux:
        raise ValueError("Switching between a Windows and a Linux executable needs a new install.")
    game.exe_path = new_exe
    refresh_integration_files(game)
    _write_manifest(game)
    return game


def _write_manifest(game: StandaloneGame) -> Path:
    """Persist the game's manifest, keeping its schema (v1 games are only upgraded by migration)."""
    if game.schema >= 2:
        root = find_library_root(game.game_dir) or get_games_dir()
        path, payload = write_manifest_v2(_game_fields(game), root)
        game.uid = str(payload["id"])
        game.meta["created_at"] = payload["created_at"]
        return path
    payload = {
        "name": game.name,
        "game_dir": str(game.game_dir),
        "exe_path": str(game.exe_path),
        "prefix_dir": str(game.prefix_dir),
        "proton_name": game.proton_name,
        "steam_appid": game.steam_appid,
        "winedlloverrides": game.winedlloverrides,
        "launch_options": game.launch_options,
        "launch_script": str(game.launch_script),
        "desktop_file": str(game.desktop_file),
        "desktop_shortcut": str(game.desktop_shortcut) if game.desktop_shortcut else "",
        "native_linux": bool(game.native_linux),
        "cache_dir": str(game.cache_dir) if game.cache_dir else "",
    }
    path = game.game_dir / MANIFEST_V1
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def _library_index_path(game_dir: Path) -> Path:
    key = hashlib.sha256(str(game_dir.resolve()).encode("utf-8")).hexdigest()
    return get_games_dir() / _LIBRARY_INDEX_DIR / f"{key}.json"


def _register_standalone_game(game: StandaloneGame, manifest_path: Path) -> None:
    """Remember an installed game's manifest even when its files stay outside ~/Games."""
    index_path = _library_index_path(game.game_dir)
    if index_path.parent.is_symlink():
        raise OSError(f"Standalone library index cannot be a symlink: {index_path.parent}")
    _atomic_write_text(index_path, json.dumps({
        "manifest": str(manifest_path.resolve()),
    }) + "\n")


def _prefix_used_by_other_game(prefix_dir: Path, game_dir: Path) -> bool:
    """True when another registered standalone game already uses prefix_dir."""
    try:
        own = game_dir.resolve()
        target = prefix_dir.resolve()
    except OSError:
        return True
    for other in list_standalone_games():
        try:
            if other.game_dir.resolve() != own and other.prefix_dir.resolve() == target:
                return True
        except OSError:
            continue
    return False


def _choose_prefix_dir(
    target_game_dir: Path, prefixes_dir: Path, keep_in_place: bool, uid: str = ""
) -> Path:
    """Reuse the game's existing prefix, else pick one not shared with another game.

    In a library (uid given) a new prefix is named after the game's stable id.
    """
    manifest = find_manifest(target_game_dir)
    if manifest is not None:
        existing = _load_manifest(manifest)
        if existing is not None and is_within(existing.prefix_dir, prefixes_dir, strict=True):
            return existing.prefix_dir
    if uid:
        return prefixes_dir / uid
    base_pfx_name = sanitize_dir_name(target_game_dir.name)
    dir_hash = hashlib.sha256(str(target_game_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    if keep_in_place:
        return prefixes_dir / f"{base_pfx_name}-{dir_hash}"
    prefix_dir = prefixes_dir / base_pfx_name
    if _prefix_used_by_other_game(prefix_dir, target_game_dir):
        prefix_dir = prefixes_dir / f"{base_pfx_name}-{dir_hash}"
    return prefix_dir


def install_standalone_game(
    game_name: str,
    exe_path: Path,
    game_dir: Path,
    proton_name: str = "",
    steam_appid: int | None = None,
    winedlloverrides: str = "",
    launch_options: str = "",
    icon_path: Path | None = None,
    create_menu_shortcut: bool = True,
    create_desktop_shortcut: bool = False,
    keep_in_place: bool = False,
    native_linux: bool = False,
    single_exe_only: bool = False,
    source: dict[str, Any] | None = None,
    library: Library | None = None,
) -> StandaloneGame:
    """Standardize a game in ~/Games/<name>/ (or keep in place), create launch.sh + .desktop + manifest."""
    _validate_launch_options(launch_options)
    _validate_launch_options(winedlloverrides)

    if library is None and not _legacy_layout():
        library = ensure_library(internal_library_root(), internal=True)
    if library is not None and not library.available:
        raise FileNotFoundError(f"Library disk is not mounted: {library.label}")
    prefix_location = "library"
    if library is None:
        games_dir = get_games_dir()
        prefixes_dir = get_prefixes_dir()
        caches_dir: Path | None = None
    else:
        games_dir = library.games_dir
        if library.unix_fs:
            prefixes_dir, caches_dir = library.prefixes_dir, library.caches_dir
        else:
            # exFAT/FAT/NTFS cannot hold a Wine prefix: keep it on the internal disk.
            internal = ensure_library(internal_library_root(), internal=True)
            prefixes_dir, caches_dir = internal.prefixes_dir, internal.caches_dir
            prefix_location = "internal"
    clean_name = game_name.strip() or game_dir.name
    safe_dir_name = sanitize_dir_name(clean_name)

    src_dir = game_dir.resolve()
    src_exe = exe_path.resolve()
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Game directory not found: {game_dir}")
    if not src_exe.is_file():
        raise FileNotFoundError(f"Game executable not found: {exe_path}")
    if not is_within(src_exe, src_dir, strict=True):
        raise ValueError(f"Game executable must be inside the game directory: {exe_path}")

    if keep_in_place:
        target_game_dir = game_dir
        target_exe_path = exe_path
    else:
        games_dir.mkdir(parents=True, exist_ok=True)
        if is_within(src_dir, games_dir) and src_dir != games_dir.resolve():
            target_game_dir = game_dir
            target_exe_path = exe_path
        else:
            target_game_dir = games_dir / safe_dir_name
            if target_game_dir.exists() and target_game_dir.resolve() != src_dir:
                idx = 2
                while (games_dir / f"{safe_dir_name} ({idx})").exists():
                    idx += 1
                target_game_dir = games_dir / f"{safe_dir_name} ({idx})"
            if single_exe_only:
                target_game_dir.mkdir(parents=True, exist_ok=True)
                target_exe_path = target_game_dir / exe_path.name
                if src_exe != target_exe_path.resolve():
                    shutil.move(str(exe_path), str(target_exe_path))
            elif target_game_dir.resolve() != src_dir:
                if _is_protected_dir(src_dir):
                    raise ValueError(
                        f"Refusing to move protected or Syncthing directory: {src_dir}"
                    )
                try:
                    rel_exe = src_exe.relative_to(src_dir)
                except ValueError:
                    rel_exe = Path(src_exe.name)
                if src_dir.stat().st_dev != games_dir.stat().st_dev:
                    check_disk_space(games_dir, estimate_dir_size(src_dir))
                move_dir_safely(game_dir, target_game_dir)
                target_exe_path = target_game_dir / rel_exe
            else:
                target_exe_path = exe_path

    uid = ""
    created_at = ""
    previous = find_manifest(target_game_dir)
    if previous is not None:
        prev_game = _load_manifest(previous)
        if prev_game is not None and prev_game.schema >= 2:
            uid = prev_game.uid
            created_at = str(prev_game.meta.get("created_at") or "")
    uid = uid or new_uid()
    prefix_dir = _choose_prefix_dir(
        target_game_dir, prefixes_dir, keep_in_place, uid=uid if library is not None else ""
    )
    if not native_linux:
        prefixes_dir.mkdir(parents=True, exist_ok=True)
        if prefix_dir.is_symlink():
            prefix_dir.unlink()
        prefix_dir.mkdir(parents=True, exist_ok=True)

    if steam_appid is None:
        steam_appid = detect_steam_appid(target_game_dir)

    launch_script = target_game_dir / "launch.sh"
    desktop_file = _resolve_desktop_file_path(clean_name, target_game_dir)
    desktop_shortcut = (
        _resolve_desktop_shortcut_path(safe_dir_name, target_game_dir)
        if create_desktop_shortcut
        else None
    )
    resolved_icon = resolve_game_icon(
        target_exe_path,
        target_game_dir,
        steam_appid=steam_appid,
        explicit_icon=icon_path,
    )

    cache_dir = caches_dir / uid if caches_dir is not None else get_game_cache_dir(target_game_dir)
    source_meta = dict(source or {})
    emulator = ""
    if not native_linux:
        emulator, emu_overrides = detect_steam_emulator(target_exe_path, target_game_dir)
        if emu_overrides:
            winedlloverrides = merge_dll_overrides(winedlloverrides, emu_overrides)
        gog_id = gog_product_id(target_game_dir)
        if gog_id and not source_meta.get("store"):
            source_meta["store"] = "gog"
            source_meta["codename"] = gog_id
    icons: list[Path] = []
    if (
        resolved_icon is not None
        and resolved_icon.name.startswith(".deckinstall_icon")
        and is_within(resolved_icon, target_game_dir, strict=True)
    ):
        icons.append(resolved_icon)
    game = StandaloneGame(
        name=clean_name,
        game_dir=target_game_dir,
        exe_path=target_exe_path,
        prefix_dir=prefix_dir,
        proton_name="" if native_linux else proton_name.strip(),
        steam_appid=steam_appid,
        winedlloverrides="" if native_linux else winedlloverrides.strip(),
        launch_options=launch_options.strip(),
        launch_script=launch_script,
        desktop_file=desktop_file,
        desktop_shortcut=desktop_shortcut,
        native_linux=native_linux,
        cache_dir=cache_dir,
        uid=uid,
        schema=2,
        meta={
            "created_at": created_at or now_iso(),
            "source": source_meta,
            "proton_mode": "pinned" if (proton_name.strip() and not native_linux) else "default",
            "prefix_location": prefix_location,
            "icons": icons,
            **({"emulator": emulator} if emulator else {}),
        },
    )

    _write_launch_script(
        launch_script,
        game.exe_path,
        game.prefix_dir,
        game.proton_name,
        game.winedlloverrides,
        game.launch_options,
        native_linux=game.native_linux,
        steam_appid=game.steam_appid,
        game_name=game.name,
        cache_dir=game.cache_dir,
        store=_store_of(game),
        codename=_codename_of(game),
    )
    startup_wm_class = _startup_wm_class(game)

    if create_menu_shortcut:
        _write_desktop_file(
            desktop_file,
            game.name,
            launch_script,
            game.game_dir,
            icon_path=resolved_icon,
            startup_wm_class=startup_wm_class,
        )
    if desktop_shortcut is not None:
        _write_desktop_file(
            desktop_shortcut,
            game.name,
            launch_script,
            game.game_dir,
            icon_path=resolved_icon,
            executable=True,
            startup_wm_class=startup_wm_class,
        )
    manifest_path = _write_manifest(game)
    legacy = target_game_dir / MANIFEST_V1
    if legacy.is_file() and not legacy.is_symlink():
        legacy.unlink()
    _register_standalone_game(game, manifest_path)
    lib_root = find_library_root(game.game_dir)
    if lib_root is not None:
        record_game(lib_root, game.uid, game.name, game.game_dir)
    return game


def _load_manifest(manifest_path: Path) -> StandaloneGame | None:
    """Load a StandaloneGame from a v1 or v2 manifest, rejecting out-of-bounds paths."""
    fields = read_manifest_fields(manifest_path)
    if fields is None:
        return None
    try:
        game_dir: Path = fields["game_dir"]
        name = str(fields["name"])
        exe_path: Path = fields["exe_path"]
        prefix_dir: Path = fields["prefix_dir"]
        launch_script: Path = fields["launch_script"]
        if not is_within(exe_path, game_dir, strict=True):
            logging.warning("Manifest exe_path %s escapes game_dir %s", exe_path, game_dir)
            return None
        if not is_within(launch_script, game_dir, strict=True):
            logging.warning("Manifest launch_script %s escapes game_dir %s", launch_script, game_dir)
            return None
        prefix_roots = [*_managed_roots(_lib.PREFIXES, game_dir), game_dir.parent / ".prefixes", game_dir]
        if prefix_dir.is_symlink() or not any(is_within(prefix_dir, r) for r in prefix_roots):
            logging.warning("Manifest prefix_dir %s is invalid or out of bounds", prefix_dir)
            return None
        desktop_file = fields["desktop_file"] or (
            get_applications_dir() / f"deckinstall-{slugify(name)}.desktop"
        )
        cache_dir: Path | None = None
        cand_cache = fields["cache_dir"]
        if cand_cache is not None:
            cache_roots = [*_managed_roots(_lib.CACHES, game_dir), game_dir.parent / ".caches"]
            if any(is_within(cand_cache, r, strict=True) for r in cache_roots):
                cache_dir = cand_cache
            else:
                logging.warning("Manifest cache_dir %s is invalid or out of bounds", cand_cache)
        return StandaloneGame(
            name=name,
            game_dir=game_dir,
            exe_path=exe_path,
            prefix_dir=prefix_dir,
            proton_name=str(fields["proton_name"]),
            steam_appid=fields["steam_appid"],
            winedlloverrides=str(fields["winedlloverrides"]),
            launch_options=str(fields["launch_options"]),
            launch_script=launch_script,
            desktop_file=desktop_file,
            desktop_shortcut=fields["desktop_shortcut"],
            native_linux=bool(fields["native_linux"]),
            cache_dir=cache_dir,
            uid=str(fields["uid"]),
            schema=int(fields["schema"]),
            meta=dict(fields["meta"]),
        )
    except Exception as exc:
        logging.warning("Failed to load standalone manifest %s: %s", manifest_path, exc)
        return None


def list_standalone_games(games_dir: Path | None = None) -> list[StandaloneGame]:
    """Load games from games_dir, or by default from every library plus the legacy ~/Games."""
    if games_dir is not None:
        return _list_games_in(games_dir, set())
    roots = [get_games_dir(), get_legacy_games_dir(), *(r / _lib.GAMES for r in _library_roots_on_disks())]
    seen: set[Path] = set()
    games: list[StandaloneGame] = []
    done: list[Path] = []
    for root in roots:
        if root in done:
            continue
        done.append(root)
        games.extend(_list_games_in(root, seen))
    games.sort(key=lambda g: g.name.lower())
    return games


def _list_games_in(root: Path, seen: set[Path]) -> list[StandaloneGame]:
    """Load registered games and recover existing manifests from nested game folders under root."""
    if not root.is_dir():
        return []
    games: list[StandaloneGame] = []

    def add_manifest(manifest: Path) -> None:
        # An index entry may still point at a v1 file after the game got a v2 manifest.
        manifest = find_manifest(manifest.parent) or manifest
        if manifest.is_symlink() or not manifest.is_file():
            return
        resolved = manifest.parent.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        game = _load_manifest(manifest)
        if game is not None:
            games.append(game)

    index_dir = root / _LIBRARY_INDEX_DIR
    if index_dir.is_dir() and not index_dir.is_symlink():
        for entry in index_dir.glob("*.json"):
            try:
                if entry.is_symlink() or entry.stat().st_size > 16384:
                    continue
                data = json.loads(entry.read_text(encoding="utf-8"))
                manifest_name = data.get("manifest") if isinstance(data, dict) else None
                if isinstance(manifest_name, str):
                    manifest = Path(manifest_name)
                    if manifest.is_absolute() and is_manifest_name(manifest.name):
                        add_manifest(manifest)
            except (OSError, ValueError):
                logging.warning("Failed to read standalone library entry %s", entry)

    # Recover pre-index installations such as Games/KIBORG/Kiborg. Avoid
    # descending into managed games, prefixes, engine files or symlinked trees.
    skipped = {"engine", "node_modules", "_redist", "drive_c", "compatdata", "shadercache"}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        folder = Path(current)
        found = next((n for n in (MANIFEST_V2, MANIFEST_V1) if n in filenames), None)
        if found is not None:
            add_manifest(folder / found)
            dirnames[:] = []
            continue
        if len(folder.relative_to(root).parts) >= 8:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name.lower() not in skipped
            and not (folder / name).is_symlink()
        ]
    games.sort(key=lambda g: g.name.lower())
    return games


def update_standalone_launch_options(
    game_dir: Path,
    winedlloverrides: str = "",
    launch_options: str = "",
    proton_name: str = "",
) -> StandaloneGame:
    """Update launch options, WINEDLLOVERRIDES, and optionally Proton for a standalone game."""
    _validate_launch_options(launch_options)
    _validate_launch_options(winedlloverrides)
    manifest = find_manifest(game_dir)
    game = _load_manifest(manifest) if manifest is not None else None
    if game is None:
        raise FileNotFoundError(f"Standalone manifest not found in {game_dir}")

    game.winedlloverrides = "" if game.native_linux else winedlloverrides.strip()
    game.launch_options = launch_options.strip()
    if not game.native_linux and proton_name.strip():
        game.proton_name = proton_name.strip()
    if game.cache_dir is None:
        game.cache_dir = get_game_cache_dir(game.game_dir)

    _write_launch_script(
        game.launch_script,
        game.exe_path,
        game.prefix_dir,
        game.proton_name,
        game.winedlloverrides,
        game.launch_options,
        native_linux=game.native_linux,
        steam_appid=game.steam_appid,
        game_name=game.name,
        cache_dir=game.cache_dir,
        store=_store_of(game),
        codename=_codename_of(game),
    )
    _write_manifest(game)
    return game


def _safe_remove_prefix_dir(prefix_dir: Path) -> None:
    """Remove prefix_dir only if it is a non-symlink strictly inside a managed prefixes folder."""
    if not any(is_within(prefix_dir, r, strict=True) for r in _managed_roots(_lib.PREFIXES)):
        logging.warning(
            "Refusing to remove prefix_dir outside prefixes directory: %s",
            prefix_dir,
        )
        return
    remove_path_or_tree(prefix_dir)


def _safe_remove_cache_dir(cache_dir: Path | None) -> None:
    """Remove cache_dir only if it is a non-symlink strictly inside get_shader_caches_dir()."""
    if cache_dir is None:
        return
    if not any(is_within(cache_dir, r, strict=True) for r in _managed_roots(_lib.CACHES)):
        logging.warning(
            "Refusing to remove cache_dir outside shader caches directory: %s",
            cache_dir,
        )
        return
    remove_path_or_tree(cache_dir)


def _orphan_cache_roots(games_dir: Path | None) -> list[Path]:
    if games_dir is not None:
        return [games_dir / ".caches"]
    return _managed_roots(_lib.CACHES)


def find_orphan_shader_caches(games_dir: Path | None = None) -> list[OrphanCacheEntry]:
    """Return shader cache directories (in every library by default) with no known game."""
    caches_dirs = [d for d in _orphan_cache_roots(games_dir) if d.is_dir() and not d.is_symlink()]
    if not caches_dirs:
        return []

    active_games = list_standalone_games(games_dir=games_dir)
    if games_dir is None:
        from .scanner import default_scan_roots

        for scan_root in default_scan_roots():
            if scan_root != get_games_dir():
                active_games.extend(list_standalone_games(games_dir=scan_root))
    active_caches: set[Path] = set()
    for game in active_games:
        if game.cache_dir is not None:
            try:
                active_caches.add(game.cache_dir.resolve())
            except OSError:
                pass
        else:
            fallback_name = get_game_cache_dir(game.game_dir).name
            for caches_dir in caches_dirs:
                try:
                    active_caches.add((caches_dir / fallback_name).resolve())
                except OSError:
                    pass
    # Games on an unmounted disk may keep their cache on the internal disk (exFAT/NTFS).
    absent_uids = {g["id"] for g in _lib.list_unavailable_games() if g.get("id")} if games_dir is None else set()

    orphans: list[OrphanCacheEntry] = []
    for caches_dir in caches_dirs:
        try:
            entries = list(caches_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in absent_uids:
                continue
            try:
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if resolved_entry not in active_caches:
                orphans.append(
                    OrphanCacheEntry(
                        path=entry,
                        size_bytes=dir_size_bytes(entry),
                        name=entry.name,
                    )
                )
    orphans.sort(key=lambda e: e.name.lower())
    return orphans


def prune_orphan_shader_caches(
    entries: list[OrphanCacheEntry] | None = None,
    games_dir: Path | None = None,
) -> tuple[int, int]:
    """Delete orphaned shader cache directories safely and return (deleted_count, freed_bytes)."""
    caches_roots = _orphan_cache_roots(games_dir)
    targets = entries if entries is not None else find_orphan_shader_caches(games_dir=games_dir)
    # A scan may have become stale while a game was installed or reconfigured.
    current_orphans = {
        entry.path.resolve()
        for entry in (find_orphan_shader_caches(games_dir=games_dir) if entries is not None else targets)
    }
    deleted = 0
    freed = 0
    for entry in targets:
        if not any(is_within(entry.path, d, strict=True) for d in caches_roots):
            logging.warning("Refusing to prune cache directory outside the caches folders: %s", entry.path)
            continue
        if (
            entry.path.is_dir()
            and not entry.path.is_symlink()
            and entry.path.resolve() in current_orphans
        ):
            remove_path_or_tree(entry.path)
            if not entry.path.exists():
                deleted += 1
                freed += entry.size_bytes
    return deleted, freed


def remove_standalone_game(
    game: StandaloneGame,
    delete_files: bool = False,
    delete_prefix: bool = False,
    delete_cache: bool = True,
) -> None:
    """Remove standalone integration files and optionally delete game, prefix, and cache directories."""
    try:
        resolved_game_dir = game.game_dir.resolve()
    except OSError:
        resolved_game_dir = game.game_dir

    validated_targets: list[tuple[Path, Path]] = [
        (game.desktop_file, get_applications_dir()),
        (game.launch_script, resolved_game_dir),
        (game.game_dir / MANIFEST_V1, resolved_game_dir),
        (game.game_dir / MANIFEST_V2, resolved_game_dir),
    ]
    if game.desktop_shortcut is not None:
        validated_targets.append((game.desktop_shortcut, get_desktop_dir()))

    for fpath, allowed_parent in validated_targets:
        if not is_within(fpath, allowed_parent, strict=True):
            logging.warning(
                "Skipping out-of-bounds or symlink integration file %s (expected inside %s)",
                fpath,
                allowed_parent,
            )
            continue
        try:
            if fpath.exists():
                fpath.unlink()
        except OSError as exc:
            logging.warning("Failed to remove %s: %s", fpath, exc)

    lib_root = find_library_root(game.game_dir)
    if lib_root is not None:
        try:
            forget_game(lib_root, game.uid)
        except OSError as exc:
            logging.warning("Failed to update library list for %s: %s", game.name, exc)
    index_path = _library_index_path(game.game_dir)
    if not index_path.parent.is_symlink() and index_path.exists():
        try:
            index_path.unlink()
        except OSError as exc:
            logging.warning("Failed to remove standalone library entry %s: %s", index_path, exc)

    if delete_files:
        if game.game_dir.is_symlink() or _is_protected_dir(resolved_game_dir):
            logging.warning(
                "Refusing to delete protected or symlinked directory as game_dir: %s",
                game.game_dir,
            )
        else:
            remove_path_or_tree(game.game_dir)
    if (delete_files or delete_prefix) and not _prefix_used_by_other_game(game.prefix_dir, game.game_dir):
        _unlink_steam_compatdata(game)
        _safe_remove_prefix_dir(game.prefix_dir)
    elif delete_files or delete_prefix:
        logging.warning("Keeping prefix %s: it is shared with another standalone game", game.prefix_dir)

    if delete_cache:
        _safe_remove_cache_dir(game.cache_dir)


def _unlink_prefix_symlinks_for_compat(
    appid_u32: int,
    compat_dir: Path,
    preferred_name: str,
) -> None:
    """Remove any Shortix-style symlinks in prefixes_dir pointing to compat_dir and update manifest."""
    prefixes_dir = get_legacy_prefixes_dir()  # where sync_prefix_symlinks keeps them
    if not prefixes_dir.is_dir():
        return
    manifest_file = prefixes_dir / ".deckinstall_links.json"
    managed: set[str] = set()
    if manifest_file.is_file():
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(raw, list):
                managed = {str(x) for x in raw}
        except Exception:
            managed = set()

    removed_names: set[str] = set()
    try:
        compat_resolved = compat_dir.resolve()
    except OSError:
        compat_resolved = compat_dir

    candidates = [prefixes_dir / preferred_name]
    try:
        candidates.extend(prefixes_dir.iterdir())
    except OSError:
        pass

    for child in candidates:
        if not child.is_symlink():
            continue
        is_match = child.name == preferred_name
        if not is_match:
            try:
                if child.resolve() == compat_resolved or child.readlink().name == str(appid_u32):
                    is_match = True
            except OSError:
                pass
        if is_match:
            try:
                child.unlink()
                removed_names.add(child.name)
            except OSError:
                pass

    if removed_names and manifest_file.is_file():
        updated = sorted(managed - removed_names)
        try:
            _atomic_write_text(manifest_file, json.dumps(updated, indent=2, ensure_ascii=False))
        except OSError:
            pass


def _is_shortcut_shared_across_users(paths: SteamPaths, appid_u32: int) -> bool:
    """Return True if another Steam user on this machine also has appid_u32 in shortcuts.vdf."""
    for uid in list_steam_users(paths.steam_root):
        if uid == paths.user_id:
            continue
        other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
        if any(e.appid_u32 == appid_u32 for e in load_shortcuts(other_sv)):
            return True
    return False


def export_steam_to_standalone(
    paths: SteamPaths,
    shortcut: ShortcutEntry,
    remove_from_steam: bool = False,
) -> StandaloneGame:
    """Export a Steam Non-Steam shortcut and its compatdata prefix to standalone ~/Games/."""
    exe_str = _strip_vdf_quotes(shortcut.exe)
    exe_path = Path(exe_str)
    start_str = _strip_vdf_quotes(shortcut.start_dir)
    try:
        inferred_dir: Path | None = infer_game_root_from_exe(exe_path)
    except ValueError:
        inferred_dir = None

    if inferred_dir is not None and inferred_dir.is_dir() and not _is_protected_dir(inferred_dir):
        game_dir = inferred_dir
    elif start_str and Path(start_str).is_dir() and not _is_protected_dir(Path(start_str)):
        game_dir = Path(start_str)
    elif inferred_dir is not None:
        game_dir = inferred_dir
    else:
        raise ValueError(
            f"Cannot determine a safe game directory for {exe_path} (StartDir={start_str!r})."
        )

    game_name = shortcut.app_name.strip() or game_dir.name or f"Game {shortcut.appid_u32}"
    safe_name = sanitize_dir_name(game_name)
    src_compat = paths.compatdata_dir / str(shortcut.appid_u32)
    src_pfx = src_compat / "pfx"

    _unlink_prefix_symlinks_for_compat(shortcut.appid_u32, src_compat, safe_name)

    proton_name = get_shortcut_proton(paths.config_vdf, shortcut.appid_u32)
    env_pairs, wrappers, game_args, dll_overrides = _parse_launch_tokens(shortcut.launch_options)

    clean_tokens: list[str] = []
    for k, v in env_pairs:
        clean_tokens.append(f"{k}={shlex.quote(v)}" if any(c.isspace() for c in v) else f"{k}={v}")
    clean_tokens.extend(wrappers)
    if clean_tokens or game_args:
        clean_tokens.append("%command%")
        clean_tokens.extend(_quote_game_arg(arg) for arg in game_args)
    clean_launch_options = " ".join(clean_tokens)

    icon_raw = _strip_vdf_quotes(shortcut.icon)
    icon_path = Path(icon_raw) if icon_raw else None

    shared_multi_user = _is_shortcut_shared_across_users(paths, shortcut.appid_u32)
    should_copy = (not remove_from_steam) or shared_multi_user

    game = install_standalone_game(
        game_name=game_name,
        exe_path=exe_path,
        game_dir=game_dir,
        proton_name=proton_name,
        steam_appid=detect_steam_appid(game_dir),
        winedlloverrides=dll_overrides,
        launch_options=clean_launch_options,
        icon_path=icon_path,
        source={"kind": "adopted", "origin": "steam", "shortcut_appid": shortcut.appid_u32},
    )

    moved_prefix = False
    if src_pfx.is_dir() and not src_pfx.is_symlink():
        copy_or_move_dir_contents(src_pfx, game.prefix_dir, move=not should_copy)
        moved_prefix = not should_copy

    if remove_from_steam:
        try:
            removed = remove_shortcut(paths, shortcut.appid_u32)
            if not removed:
                raise RuntimeError(f"Shortcut {shortcut.appid_u32} could not be removed from Steam.")
        except Exception:
            if moved_prefix and game.prefix_dir.is_dir() and not game.prefix_dir.is_symlink():
                copy_or_move_dir_contents(game.prefix_dir, src_pfx, move=True)
            try:
                remove_standalone_game(game, delete_files=False, delete_prefix=not moved_prefix)
            except Exception:
                pass
            raise
        if moved_prefix:
            shutil.rmtree(src_compat, ignore_errors=True)
    elif moved_prefix:
        shutil.rmtree(src_compat, ignore_errors=True)

    return game


def _has_drive_c(prefix: Path) -> bool:
    """True when prefix holds a Wine prefix, either umu-style (drive_c) or Steam-style (pfx/drive_c)."""
    return (prefix / "drive_c").is_dir() or (
        (prefix / "pfx").is_dir() and not (prefix / "pfx").is_symlink() and (prefix / "pfx" / "drive_c").is_dir()
    )


def _ensure_umu_pfx_link(prefix_dir: Path) -> None:
    """Make prefix_dir usable as a Steam compatdata folder: Proton opens <compatdata>/pfx."""
    pfx = prefix_dir / "pfx"
    if not pfx.exists() and not pfx.is_symlink():
        prefix_dir.mkdir(parents=True, exist_ok=True)
        pfx.symlink_to(".")


def _adopt_steam_prefix(src_compat: Path, prefix_dir: Path) -> None:
    """Move a Steam compatdata folder into an empty game prefix, converting it to the umu layout."""
    inner = src_compat / "pfx"
    if inner.is_dir() and not inner.is_symlink():
        copy_or_move_dir_contents(inner, prefix_dir, move=True)
        inner.rmdir()
    for item in list(src_compat.iterdir()):
        if item.name == "pfx":
            continue
        target = prefix_dir / item.name
        if target.exists() or target.is_symlink():
            continue
        shutil.move(str(item), str(target))
    remaining = list(src_compat.iterdir())
    if remaining:
        raise RuntimeError(f"Could not fully adopt {src_compat}: {[p.name for p in remaining]}")
    src_compat.rmdir()


def _compatdata_backup_path(compat: Path) -> Path:
    candidate = compat.with_name(f"{compat.name}.deckinstall.bak")
    idx = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = compat.with_name(f"{compat.name}.{idx}.deckinstall.bak")
        idx += 1
    return candidate


def link_compatdata_to_prefix(compat: Path, prefix_dir: Path) -> Path | None:
    """Point Steam's compatdata/<appid> at the game's own prefix (one game, one prefix).

    An existing compatdata folder is adopted when the game prefix is still empty; otherwise it
    is kept as <appid>.deckinstall.bak (listed under Backups in Maintenance). Nothing is deleted.
    Returns the backup path, if one was made.
    """
    if prefix_dir.is_symlink():
        raise ValueError(f"Game prefix cannot be a symlink: {prefix_dir}")
    prefix_dir.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if compat.is_symlink():
        try:
            if compat.resolve() == prefix_dir.resolve():
                _ensure_umu_pfx_link(prefix_dir)
                return None
        except OSError:
            pass
        if compat.exists():
            raise RuntimeError(
                f"{compat} already points to another prefix ({os.readlink(compat)}); "
                "restore it from microSD before linking."
            )
        compat.unlink()  # dangling link left by a removed prefix
    elif compat.is_dir():
        if _has_drive_c(compat) and not _has_drive_c(prefix_dir):
            _adopt_steam_prefix(compat, prefix_dir)
        else:
            backup = _compatdata_backup_path(compat)
            os.replace(compat, backup)
    elif compat.exists():
        raise RuntimeError(f"{compat} is not a directory")
    _ensure_umu_pfx_link(prefix_dir)
    compat.parent.mkdir(parents=True, exist_ok=True)
    compat.symlink_to(prefix_dir)
    return backup


def _unlink_steam_compatdata(game: StandaloneGame) -> None:
    """Remove the compatdata link recorded for game, only if it still points at its prefix."""
    raw = str((game.meta.get("steam") or {}).get("compatdata_link") or "")
    if not raw:
        return
    try:
        link = decode_path(raw, None)
    except ValueError:
        return
    if not link.is_symlink():
        return
    try:
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = link.parent / target
        if os.path.abspath(target) != os.path.abspath(game.prefix_dir):
            return
    except OSError:
        return
    try:
        link.unlink()
    except OSError as exc:
        logging.warning("Failed to remove Steam compatdata link %s: %s", link, exc)


def import_standalone_to_steam(
    paths: SteamPaths,
    game: StandaloneGame,
    compat_tool: str = "",
    remove_standalone_files: bool = False,
) -> int:
    """Import a standalone game into Steam shortcuts.vdf and compatdata/<appid>/pfx."""
    env_pairs, wrappers, _, _ = _parse_launch_tokens(game.launch_options, game.winedlloverrides)
    env_map = dict(env_pairs)

    steam_launch = apply_launch_presets(
        game.launch_options,
        gamemode="gamemoderun" in wrappers,
        mangohud=env_map.get("MANGOHUD") == "1",
        wined3d=env_map.get("PROTON_USE_WINED3D") == "1",
        no_esync=env_map.get("PROTON_NO_ESYNC") == "1",
        no_fsync=env_map.get("PROTON_NO_FSYNC") == "1",
        winedlloverrides=game.winedlloverrides,
        dlss_upgrade=env_map.get("PROTON_DLSS_UPGRADE") == "1",
        nvidia_nvapi=(
            env_map.get("PROTON_ENABLE_NVAPI") == "1"
            or env_map.get("DXVK_ENABLE_NVAPI") == "1"
        ),
        prime_offload=(
            env_map.get("__NV_PRIME_RENDER_OFFLOAD") == "1"
            or env_map.get("__GLX_VENDOR_LIBRARY_NAME") == "nvidia"
        ),
        proton_wayland=env_map.get("PROTON_ENABLE_WAYLAND") == "1",
    )

    resolved_icon = resolve_game_icon(
        game.exe_path,
        game.game_dir,
        steam_appid=game.steam_appid,
    )

    appid_u32 = add_or_update_shortcut(
        paths,
        app_name=game.name,
        exe_path=game.exe_path,
        start_dir=game.exe_path.parent,
        icon_path=resolved_icon,
        launch_options=steam_launch or None,
        enable_proton=not game.native_linux,
    )

    target_tool = compat_tool.strip() or game.proton_name.strip()
    if target_tool and not game.native_linux:
        set_shortcut_proton(paths.config_vdf, appid_u32, target_tool)

    target_compat = paths.compatdata_dir / str(appid_u32)
    target_pfx = target_compat / "pfx"
    if game.schema >= 2 and not game.native_linux and not remove_standalone_files:
        # One game, one prefix: Steam runs the same prefix the launcher uses.
        backup = link_compatdata_to_prefix(target_compat, game.prefix_dir)
        steam_meta: dict[str, Any] = {
            "user_id": paths.user_id,
            "shortcut_appid": appid_u32,
            "compatdata_link": encode_path(target_compat, None),
        }
        if target_tool:
            steam_meta["compat_tool"] = target_tool
        if backup is not None:
            steam_meta["compatdata_backup"] = encode_path(backup, None)
        game.meta["steam"] = steam_meta
        _write_manifest(game)
    elif game.prefix_dir.is_dir() and not game.prefix_dir.is_symlink():
        if target_compat.is_symlink():
            # Converting a linked game to Steam-only: give Steam a real folder again.
            target_compat.unlink()
        copy_or_move_dir_contents(game.prefix_dir, target_pfx, move=remove_standalone_files)
        if remove_standalone_files:
            _safe_remove_prefix_dir(game.prefix_dir)

    if remove_standalone_files:
        remove_standalone_game(game, delete_files=False, delete_cache=True)

    sync_prefix_symlinks(paths)
    return appid_u32
