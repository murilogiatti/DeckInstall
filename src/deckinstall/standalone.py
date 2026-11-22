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
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import library as _lib
from .extractor import check_disk_space
from .fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    copy_or_move_dir_contents,
    is_within,
    move_dir_safely,
    prune_empty_parents,
    remove_path_or_tree,
    sanitize_dir_name,
    slugify,
    symlink_points_to,
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
    find_proton_binary,
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
    _steam_transaction_lock,
    _strip_vdf_quotes,
    _validate_launch_options,
    add_or_update_shortcut,
    apply_launch_presets,
    compute_shortcut_appid,
    infer_game_root_from_exe,
    load_shortcuts,
    set_shortcut_proton,
    sync_prefix_symlinks,
    update_shortcut_paths,
)

_DEFAULT_GAMES_DIR = Path.home() / "Games"
_DEFAULT_PREFIXES_DIR = _DEFAULT_GAMES_DIR / ".prefixes"
_DEFAULT_APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"

GAMES_DIR = _DEFAULT_GAMES_DIR
PREFIXES_DIR = _DEFAULT_PREFIXES_DIR
APPLICATIONS_DIR = _DEFAULT_APPLICATIONS_DIR
UMU_RUN_PATH = Path.home() / ".local" / "share" / "DeckInstall" / "runtime" / "umu" / "umu-run"
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


def _games_roots_for(game_dir: Path) -> list[Path]:
    """Games folders DeckInstall manages: empty wrappers are pruned up to (not including) them."""
    roots = [get_games_dir(), get_legacy_games_dir()]
    lib_root = find_library_root(game_dir)
    if lib_root is not None:
        roots.append(lib_root / _lib.GAMES)
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
        home / "DeckInstall",
        home / "DeckInstall" / "Games",
        home / "DeckInstall" / "Prefixes",
        home / "DeckInstall" / "Caches",
        get_games_dir(),
        home / "Syncthing",
        Path("/"),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/mnt"),
        Path("/var/mnt"),
        Path("/media"),
        Path("/run/media"),
    )
    protected: set[Path] = set()
    for candidate in candidates:
        try:
            protected.add(candidate.resolve())
        except OSError:
            protected.add(candidate)
    return protected


def _is_protected_dir(path: Path) -> bool:
    """Return True if path is a protected directory, library root, mountpoint, or Syncthing root."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in _get_protected_dirs():
        return True
    if resolved.parent in (Path("/mnt"), Path("/var/mnt"), Path("/media")):
        return True
    try:
        if (resolved / ".stfolder").exists() or (resolved / "library.json").is_file():
            return True
    except OSError:
        return False
    try:
        home_resolved = Path.home().resolve()
        tmp_resolved = {Path("/tmp").resolve(), Path("/var/tmp").resolve()}
        if (
            resolved != Path("/")
            and resolved != home_resolved
            and resolved not in home_resolved.parents
            and resolved not in tmp_resolved
            and os.path.ismount(resolved)
        ):
            return True
    except OSError:
        pass
    return False


def _resolve_proton_path(proton_name: str) -> str:
    """Resolve a Proton/GE-Proton tool name to a filesystem path for PROTONPATH when possible."""
    name = proton_name.strip()
    if not name:
        # "Default (newest)": bake the newest installed GE-Proton into the launcher; updating
        # GE-Proton rewrites these launchers (proton_policy.retarget_default_games).
        from .runtime_manager import list_ge_proton10_dirs, list_ge_proton11_dirs

        newest = list_ge_proton11_dirs() or list_ge_proton10_dirs()
        if newest:
            return str(newest[0])
        steam_root = find_steam_root()
        proton = find_proton_binary(steam_root) if steam_root is not None else None
        return str(proton.parent) if proton is not None else "GE-Proton"
    from .runtime_manager import get_compat_tools_dir

    steam_root = find_steam_root() or (Path.home() / ".local" / "share" / "Steam")
    resolved = resolve_proton_dir(name, steam_root, extra_compat_dirs=[get_compat_tools_dir()])
    return str(resolved) if resolved is not None else name


def resolve_standalone_proton_fallback(game: StandaloneGame) -> tuple[Path | None, str | None]:
    """Resolve game.proton_name or fall back to the newest installed GE-Proton11+/10 tool."""
    from .runtime_manager import list_ge_proton10_dirs, list_ge_proton11_dirs

    pinned_name = game.proton_name.strip()
    if pinned_name:
        resolved_str = _resolve_proton_path(pinned_name)
        resolved_path = Path(resolved_str)
        if resolved_path.is_dir() and (resolved_path / "proton").is_file():
            return (resolved_path, None)

    candidates = list_ge_proton11_dirs() or list_ge_proton10_dirs()
    if candidates:
        fallback_dir = candidates[0]
        return (fallback_dir, fallback_dir.name if pinned_name else None)
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
    """Return environment values, wrappers, shell-formatted arguments and effective DLL overrides."""
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
            v_unquoted = _unquote_launch_token(v)
            if k == "WINEDLLOVERRIDES":
                parsed_dll = v_unquoted
                continue
            env_pairs.append((k, v_unquoted))
        else:
            wrappers.append(_unquote_launch_token(tok))

    effective_dll = merge_dll_overrides(winedlloverrides, parsed_dll) if winedlloverrides.strip() else parsed_dll
    game_args = [_quote_game_arg(t) for t in after]
    return env_pairs, wrappers, game_args, effective_dll


def _unquote_launch_token(token: str) -> str:
    try:
        parts = shlex.split(token)
    except ValueError:
        return token
    return parts[0] if len(parts) == 1 else token


def _quote_game_arg(arg: str) -> str:
    """Quote a game argument unless it is a shell operator (excluding %command%)."""
    if arg in _SHELL_OPS and arg != "%command%":
        return arg
    return shlex.quote(_unquote_launch_token(arg))


def _store_of(game: StandaloneGame) -> str:
    return str((game.meta.get("source") or {}).get("store") or "")


def _codename_of(game: StandaloneGame) -> str:
    return str((game.meta.get("source") or {}).get("codename") or "")


def merge_dll_overrides(user: str, extra: str) -> str:
    """Add the extra WINEDLLOVERRIDES entries the user did not set already (user entries win)."""
    from .steam_vdf import _normalize_dll_name, merge_winedlloverride

    taken = {
        _normalize_dll_name(dll)
        for entry in user.split(";")
        for dll in entry.partition("=")[0].split(",")
    }
    merged = user.strip()
    for entry in extra.split(";"):
        dlls, separator, mode = entry.partition("=")
        for dll in dlls.split(","):
            norm = _normalize_dll_name(dll)
            if norm and norm not in taken:
                merged = merge_winedlloverride(merged, norm, mode.strip() if separator else "n,b")
                taken.add(norm)
    return merged


LAUNCHER_MARKER = "# Generated by DeckInstall"
_LAUNCH_BACKUP_SUFFIX = ".orig"


def _is_deckinstall_launcher(script: Path) -> bool:
    """True when script was written by DeckInstall (marker, run-log lines or the old fixed header)."""
    try:
        head = script.read_bytes()[:4096].decode("utf-8", errors="replace")
    except OSError:
        return False
    if LAUNCHER_MARKER in head or "== DeckInstall:" in head:
        return True
    # Launchers written before the marker existed always started with these lines.
    return head.startswith("#!/usr/bin/env bash\nset -euo pipefail\ncd ") and (
        "umu-run" in head or "exec " in head
    )


def _foreign_launch_backup_path(script: Path) -> Path | None:
    if script.is_symlink() or not script.is_file() or _is_deckinstall_launcher(script):
        return None
    backup = script.with_name(script.name + _LAUNCH_BACKUP_SUFFIX)
    idx = 2
    while backup.exists() or backup.is_symlink():
        backup = script.with_name(f"{script.name}{_LAUNCH_BACKUP_SUFFIX}.{idx}")
        idx += 1
    return backup


def preserve_foreign_launch_script(script: Path) -> Path | None:
    """Keep a game-provided launch.sh as a sibling backup."""
    backup = _foreign_launch_backup_path(script)
    if backup is None:
        return None
    os.rename(script, backup)
    logging.info("Kept the game's own %s as %s", script.name, backup.name)
    return backup


def restore_foreign_launch_script(game: StandaloneGame) -> bool:
    """Put the game's own launch.sh back (after ours was removed). True when restored."""
    backup = game.meta.get("launch_script_backup")
    if not isinstance(backup, Path):
        return False
    script = game.launch_script
    if (
        script.exists()
        or script.is_symlink()
        or backup.is_symlink()
        or not backup.is_file()
        or backup.parent != script.parent
        or not is_within(backup, game.game_dir, strict=True)
    ):
        return False
    try:
        os.rename(backup, script)
    except OSError as exc:
        logging.warning("Could not restore %s from %s: %s", script, backup, exc)
        return False
    return True


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
    log_dir: Path | None = None,
) -> None:
    """Generate an executable launch.sh script invoking umu-run or a native Linux binary.

    With log_dir, every run (from the app, Steam, the menu or the desktop) writes its output to
    log_dir/run.log, keeping the previous runs as run.1.log ... and the exit code.
    """
    env_pairs, wrappers, game_args, effective_dll = _parse_launch_tokens(
        launch_options, "" if native_linux else winedlloverrides
    )
    user_env_keys = {k for k, _ in env_pairs}

    lines = [
        "#!/usr/bin/env bash",
        LAUNCHER_MARKER,
        "set -euo pipefail",
    ]
    if log_dir is not None:
        lines.extend(_log_lines(log_dir, game_name or exe_path.stem))
    lines.append(f"cd {shlex.quote(str(exe_path.parent))}")

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
    cmd_parts.extend(game_args)
    command = " ".join(cmd_parts)
    if log_dir is None:
        lines.append("exec " + command)
    else:
        lines.extend([
            "rc=0",
            f"{command} || rc=$?",
            'echo "== DeckInstall: exit code $rc at $(date -Is)"',
            'exit "$rc"',
        ])

    atomic_write_text(script_path, "\n".join(lines) + "\n", mode=0o755)


LOG_KEEP = 5


def game_logs_root() -> Path:
    """Where per-game run logs live (one folder per game, on the internal disk)."""
    return Path.home() / ".local" / "share" / "DeckInstall" / "logs"


def game_log_dir(game: StandaloneGame) -> Path:
    """The game's run-log folder: by stable id for library games, by name for older ones."""
    return game_logs_root() / (game.uid or sanitize_dir_name(game.name) or "game")


def game_run_logs(game: StandaloneGame) -> list[Path]:
    """Existing run logs, newest first."""
    folder = game_log_dir(game)
    logs = [folder / "run.log", *(folder / f"run.{i}.log" for i in range(1, LOG_KEEP))]
    return [p for p in logs if p.is_file()]


def _log_lines(log_dir: Path, title: str) -> list[str]:
    """Bash lines that rotate run.log and send this run's output to it."""
    rotate = " ".join(str(i) for i in range(LOG_KEEP - 2, 0, -1))
    safe_title = "".join(c for c in title if c.isprintable() and c not in '"$`\\')
    return [
        f"LOG_DIR={shlex.quote(str(log_dir))}",
        'if mkdir -p "$LOG_DIR" 2>/dev/null; then',
        f'  for i in {rotate}; do',
        '    if [ -f "$LOG_DIR/run.$i.log" ]; then mv -f "$LOG_DIR/run.$i.log" "$LOG_DIR/run.$((i + 1)).log"; fi',
        "  done",
        '  if [ -f "$LOG_DIR/run.log" ]; then mv -f "$LOG_DIR/run.log" "$LOG_DIR/run.1.log"; fi',
        '  exec >"$LOG_DIR/run.log" 2>&1',
        f'  echo "== DeckInstall: starting {safe_title} at $(date -Is)"',
        "fi",
    ]


def _write_game_launcher(game: StandaloneGame) -> None:
    """Write the game's launch.sh (with run logs) and note the log folder in its manifest data."""
    log_dir = game_log_dir(game)
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
        log_dir=log_dir,
    )
    game.meta["logs"] = {"dir": encode_path(log_dir, None), "keep": LOG_KEEP}


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


def _parse_rsrc_entries(data: bytes, dir_off: int) -> list[tuple[int, int]]:
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
            if magic == 0x10B and opt_size >= 120:
                rsrc_rva, _ = struct.unpack_from("<II", opt_hdr, 96 + 2 * 8)
            elif magic == 0x20B and opt_size >= 136:
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

        for type_id, type_sub in _parse_rsrc_entries(rsrc_data, rsrc_local_off):
            if not (type_sub & 0x80000000):
                continue
            l2_off = rsrc_local_off + (type_sub & 0x7FFFFFFF)
            if type_id == 3:  # RT_ICON
                for icon_id, icon_sub in _parse_rsrc_entries(rsrc_data, l2_off):
                    if not (icon_sub & 0x80000000):
                        continue
                    l3_off = rsrc_local_off + (icon_sub & 0x7FFFFFFF)
                    for _lang, data_entry_rel in _parse_rsrc_entries(rsrc_data, l3_off):
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
                for _grp_id, grp_sub in _parse_rsrc_entries(rsrc_data, l2_off):
                    if not (grp_sub & 0x80000000):
                        continue
                    l3_off = rsrc_local_off + (grp_sub & 0x7FFFFFFF)
                    for _lang, data_entry_rel in _parse_rsrc_entries(rsrc_data, l3_off):
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
    _write_game_launcher(game)
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


def _desktop_opens_game(path: Path, game: StandaloneGame) -> bool:
    """True if this .desktop starts the game (its launch.sh, or a working dir in the game folder)."""
    if path.is_symlink() or not path.is_file():
        return False
    if _desktop_points_to_game_dir(path, game.game_dir):
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    script = str(game.launch_script)
    return any(
        line.strip().startswith("Exec=") and script in line for line in text.splitlines()
    )


def find_game_launchers(game: StandaloneGame) -> dict[str, list[Path]]:
    """The .desktop files that really exist for this game: {"menu": [...], "desktop": [...]}.

    Looks at the app menu folder and the user's Desktop, not only at what the manifest says,
    so launchers the user deleted (or made by hand) are reported as they are.
    """
    found: dict[str, list[Path]] = {"menu": [], "desktop": []}
    for where, folder in (("menu", get_applications_dir()), ("desktop", get_desktop_dir())):
        try:
            entries = sorted(folder.glob("*.desktop"))
        except OSError:
            continue
        found[where] = [p for p in entries if _desktop_opens_game(p, game)]
    return found


def set_game_launcher(game: StandaloneGame, where: str, enabled: bool) -> StandaloneGame:
    """Create (enabled) or remove the menu/Desktop launcher and record it in the manifest.

    ``where`` is "menu" or "desktop". Removing deletes every .desktop there that opens this game.
    """
    if where not in ("menu", "desktop"):
        raise ValueError(where)
    existing = find_game_launchers(game)[where]
    if enabled:
        if existing:
            path = existing[0]
        elif where == "menu":
            path = _resolve_desktop_file_path(game.name, game.game_dir)
        else:
            path = _resolve_desktop_shortcut_path(sanitize_dir_name(game.name), game.game_dir)
        if not existing:
            _write_desktop_file(
                path,
                game.name,
                game.launch_script,
                game.game_dir,
                icon_path=resolve_game_icon(game.exe_path, game.game_dir, steam_appid=game.steam_appid),
                executable=where == "desktop",
                startup_wm_class=_startup_wm_class(game),
            )
        if where == "menu":
            game.desktop_file = path
        else:
            game.desktop_shortcut = path
    else:
        for path in existing:
            path.unlink(missing_ok=True)
        if where == "desktop":
            game.desktop_shortcut = None
        # The menu path stays as the default name; the manifest only records it while the file exists.
    _write_manifest(game)
    return game


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
    uid: str = "",
) -> StandaloneGame:
    """Standardize a game in ~/Games/<name>/ (or keep in place), create launch.sh + .desktop + manifest.

    uid: id for a new game (adoption moves the prefix to Prefixes/<uid> before calling this);
    ignored when the folder already has a v2 manifest.
    """
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
            if target_game_dir.is_dir() and not target_game_dir.is_symlink() and not any(target_game_dir.iterdir()):
                target_game_dir.rmdir()  # an empty leftover folder is not a reason for "(2)"
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

    created_at = ""
    launch_backup: Path | None = None
    previous = find_manifest(target_game_dir)
    if previous is not None:
        prev_game = _load_manifest(previous)
        if prev_game is not None and prev_game.schema >= 2:
            uid = prev_game.uid
            created_at = str(prev_game.meta.get("created_at") or "")
        if prev_game is not None and isinstance(prev_game.meta.get("launch_script_backup"), Path):
            launch_backup = prev_game.meta["launch_script_backup"]
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
    if previous is None:
        # A launch.sh the game shipped itself is kept (restored on uninstall), never overwritten.
        launch_backup = preserve_foreign_launch_script(launch_script) or launch_backup
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
            **({"launch_script_backup": launch_backup} if launch_backup is not None else {}),
        },
    )

    _write_game_launcher(game)
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


def owned_shortcut_appids(game: StandaloneGame) -> set[int]:
    """Steam shortcut AppIDs that belong to this game: added to Steam by DeckInstall, or the
    shortcut it was adopted from (kept in Steam, now running the same prefix)."""
    out: set[int] = set()
    source = game.meta.get("source") or {}
    candidates = [(game.meta.get("steam") or {}).get("shortcut_appid")]
    if source.get("origin") == "steam" or source.get("kind") == "adopted":
        candidates.append(source.get("shortcut_appid"))
    for raw in candidates:
        try:
            if raw is not None and int(raw) > 0:
                out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _prior_game_dirs(game: StandaloneGame) -> list[Path]:
    """Return decoded original or previously relocated game directories for a game."""
    source = game.meta.get("source") or {}
    raw_dirs: list[object] = [source.get("original_game_dir")]
    previous = source.get("previous_game_dirs")
    if isinstance(previous, list):
        raw_dirs.extend(previous)
    out: list[Path] = []
    for raw in raw_dirs:
        if not isinstance(raw, str):
            continue
        try:
            decoded = decode_path(raw, None)
        except ValueError:
            continue
        if decoded not in out:
            out.append(decoded)
    return out


def match_owned_shortcuts(
    entries: list[ShortcutEntry], games: list[StandaloneGame], user_id: str | None = None,
) -> dict[int, StandaloneGame]:
    """Associate shortcuts with a unique game using current paths, AppIDs or prior paths."""
    by_path: dict[Path, list[StandaloneGame]] = {}
    by_appid: dict[int, list[StandaloneGame]] = {}
    for g in games:
        paths = {g.exe_path, g.launch_script}
        try:
            rel_exe = g.exe_path.relative_to(g.game_dir)
        except ValueError:
            rel_exe = None
        if rel_exe is not None:
            for prior in _prior_game_dirs(g):
                paths.add(prior / rel_exe)
        for path in paths:
            if path:
                by_path.setdefault(Path(os.path.normpath(path)), []).append(g)
                by_path.setdefault(Path(os.path.realpath(path)), []).append(g)
        account = (g.meta.get("steam") or {}).get("user_id")
        if not account or user_id is None or str(account) == user_id:
            for appid in owned_shortcut_appids(g):
                by_appid.setdefault(appid, []).append(g)
    owned: dict[int, StandaloneGame] = {}
    for entry in entries:
        exe = Path(os.path.normpath(_strip_vdf_quotes(entry.exe)))
        if not exe.is_absolute():
            continue
        candidates = {
            g.game_dir: g
            for g in [
                *by_path.get(exe, ()),
                *by_path.get(Path(os.path.realpath(exe)), ()),
                *by_appid.get(entry.appid_u32, ()),
            ]
        }
        if len(candidates) == 1:
            owned[entry.appid_u32] = next(iter(candidates.values()))
    return owned


def primary_owned_shortcuts(
    entries: list[ShortcutEntry], owned: dict[int, StandaloneGame],
) -> dict[Path, ShortcutEntry]:
    """Choose one Steam entry per managed game, preferring its current executable."""
    primary: dict[Path, ShortcutEntry] = {}
    priorities: dict[Path, bool] = {}
    for entry in entries:
        game = owned.get(entry.appid_u32)
        if game is None:
            continue
        current = os.path.realpath(_strip_vdf_quotes(entry.exe)) == os.path.realpath(game.exe_path)
        if game.game_dir not in primary or (current and not priorities[game.game_dir]):
            primary[game.game_dir] = entry
            priorities[game.game_dir] = current
    return primary


def shortcut_path_repairs(
    entries: list[ShortcutEntry], games: list[StandaloneGame], user_id: str | None = None,
) -> list[tuple[ShortcutEntry, StandaloneGame]]:
    """Owned shortcuts whose exe no longer exists (the game was adopted/moved with Steam open):
    they should run the game's exe from its new folder, keeping their AppID."""
    owned = match_owned_shortcuts(entries, games, user_id)
    out: list[tuple[ShortcutEntry, StandaloneGame]] = []
    for entry in entries:
        game = owned.get(entry.appid_u32)
        if game is None or not game.exe_path.is_file():
            continue
        exe = Path(_strip_vdf_quotes(entry.exe))
        if not exe.exists() and not exe.is_symlink():
            out.append((entry, game))
    return out


def repaired_shortcut_start_dir(entry: ShortcutEntry, game: StandaloneGame) -> Path:
    """Keep the original working directory's relative location when the game moved."""
    start = Path(_strip_vdf_quotes(entry.start_dir))
    norm_start = Path(os.path.normpath(start))
    for root in _prior_game_dirs(game):
        try:
            relative = norm_start.relative_to(root)
            mapped = game.game_dir / relative
            if mapped.is_dir() and is_within(mapped, game.game_dir):
                return mapped
        except ValueError:
            continue
    return game.exe_path.parent


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

    _write_game_launcher(game)
    _write_manifest(game)
    return game


def _safe_remove_prefix_dir(prefix_dir: Path, game_dir: Path | None = None) -> None:
    """Remove prefix_dir only if it is a non-symlink strictly inside a managed prefixes folder."""
    if not any(is_within(prefix_dir, r, strict=True) for r in _managed_roots(_lib.PREFIXES, game_dir)):
        logging.warning(
            "Refusing to remove prefix_dir outside prefixes directory: %s",
            prefix_dir,
        )
        return
    remove_path_or_tree(prefix_dir)


def _safe_remove_cache_dir(cache_dir: Path | None, game_dir: Path | None = None) -> None:
    """Remove cache_dir only if it is a non-symlink strictly inside get_shader_caches_dir()."""
    if cache_dir is None:
        return
    if not any(is_within(cache_dir, r, strict=True) for r in _managed_roots(_lib.CACHES, game_dir)):
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

    # Only DeckInstall's own libraries and the legacy ~/Games decide which games exist, never the
    # user's scan folders (a disk that is not a scan folder must not make its caches look orphaned).
    active_games = list_standalone_games(games_dir=games_dir)
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
    if not delete_files:
        restore_foreign_launch_script(game)

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
            prune_empty_parents(game.game_dir, _games_roots_for(game.game_dir))
    if (delete_files or delete_prefix) and not _prefix_used_by_other_game(game.prefix_dir, game.game_dir):
        _unlink_steam_compatdata(game)
        _safe_remove_prefix_dir(game.prefix_dir, game.game_dir)
    elif delete_files or delete_prefix:
        logging.warning("Keeping prefix %s: it is shared with another standalone game", game.prefix_dir)

    if delete_cache:
        _safe_remove_cache_dir(game.cache_dir, game.game_dir)


def _is_shortcut_shared_across_users(paths: SteamPaths, appid_u32: int) -> bool:
    """Return True if another Steam user on this machine also has appid_u32 in shortcuts.vdf."""
    for uid in list_steam_users(paths.steam_root):
        if uid == paths.user_id:
            continue
        other_sv = paths.steam_root / "userdata" / uid / "config" / "shortcuts.vdf"
        if any(e.appid_u32 == appid_u32 for e in load_shortcuts(other_sv)):
            return True
    return False


def shortcut_game_dir(shortcut: ShortcutEntry) -> Path:
    """Return the game folder of a Steam Non-Steam shortcut (inferred from its exe, else StartDir)."""
    exe_path = Path(_strip_vdf_quotes(shortcut.exe))
    start_str = _strip_vdf_quotes(shortcut.start_dir)
    try:
        inferred_dir: Path | None = infer_game_root_from_exe(exe_path)
    except ValueError:
        inferred_dir = None
    if inferred_dir is not None and inferred_dir.is_dir() and not _is_protected_dir(inferred_dir):
        return inferred_dir
    if start_str and Path(start_str).is_dir() and not _is_protected_dir(Path(start_str)):
        return Path(start_str)
    if inferred_dir is not None:
        return inferred_dir
    raise ValueError(f"Cannot determine a safe game directory for {exe_path} (StartDir={start_str!r}).")


def shortcut_launch_options(shortcut: ShortcutEntry) -> tuple[str, str]:
    """Return (launch options for launch.sh, WINEDLLOVERRIDES) taken from a shortcut's launch options."""
    env_pairs, wrappers, game_args, dll_overrides = _parse_launch_tokens(shortcut.launch_options)
    clean_tokens: list[str] = []
    for k, v in env_pairs:
        clean_tokens.append(f"{k}={shlex.quote(v)}")
    clean_tokens.extend(shlex.quote(wrapper) for wrapper in wrappers)
    if clean_tokens or game_args:
        clean_tokens.append("%command%")
        clean_tokens.extend(game_args)
    return " ".join(clean_tokens), dll_overrides


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
    real_inner = inner.is_dir() and not inner.is_symlink()
    top_items = [item for item in src_compat.iterdir() if item.name != "pfx"]
    incoming = [*top_items, *(inner.iterdir() if real_inner else ())]
    seen: set[str] = set()
    for item in incoming:
        target = prefix_dir / item.name
        if item.name in seen or target.exists() or target.is_symlink():
            raise FileExistsError(f"Cannot adopt Steam prefix because {target} already exists")
        seen.add(item.name)
    if real_inner:
        copy_or_move_dir_contents(inner, prefix_dir, move=True)
        inner.rmdir()
    for item in top_items:
        shutil.move(str(item), str(prefix_dir / item.name))
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
    if not symlink_points_to(link, game.prefix_dir):
        return
    try:
        link.unlink()
    except OSError as exc:
        logging.warning("Failed to remove Steam compatdata link %s: %s", link, exc)


def _copy_import_prefix(source: Path, compat: Path, convert_link: bool) -> None:
    """Commit the merged prefix after copying, while keeping the original recoverable."""
    if source.is_symlink():
        raise ValueError(f"Refusing to copy a symlinked source prefix: {source}")
    if not compat.is_symlink() and source.resolve() == (compat / "pfx").resolve():
        return
    if compat.is_symlink():
        if not convert_link or not symlink_points_to(compat, source):
            raise ValueError(f"Refusing to replace a foreign prefix link: {compat}")
    elif compat.exists() and not compat.is_dir():
        raise ValueError(f"Steam compatdata is not a directory: {compat}")
    compat.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    with tempfile.TemporaryDirectory(prefix=".deckinstall-import-", dir=compat.parent) as work:
        staged = Path(work) / "compatdata"
        if compat.is_dir() and not compat.is_symlink():
            shutil.copytree(compat, staged, symlinks=True)
        else:
            staged.mkdir()
        target = staged / "pfx"
        if target.is_symlink():
            raise ValueError(f"Refusing to merge into a symlinked Steam pfx: {compat / 'pfx'}")
        copy_or_move_dir_contents(source, target, move=False)
        if compat.exists() or compat.is_symlink():
            backup = _compatdata_backup_path(compat)
            os.replace(compat, backup)
        try:
            os.replace(staged, compat)
        except BaseException:
            if backup is not None:
                try:
                    os.replace(backup, compat)
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not restore Steam compatdata. Previous prefix retained at {backup}"
                    ) from exc
            raise
    if backup is not None:
        try:
            remove_path_or_tree(backup)
        except OSError:
            logging.warning("Could not remove previous Steam prefix backup %s", backup, exc_info=True)


def import_standalone_to_steam(
    paths: SteamPaths,
    game: StandaloneGame,
    compat_tool: str = "",
    remove_standalone_files: bool = False,
    *,
    match_existing_name: bool = True,
) -> int:
    """Import a standalone game into Steam shortcuts.vdf and compatdata/<appid>/pfx."""
    env_pairs, wrappers, _, dll_overrides = _parse_launch_tokens(game.launch_options, game.winedlloverrides)
    env_map = dict(env_pairs)

    steam_launch = apply_launch_presets(
        game.launch_options,
        gamemode="gamemoderun" in wrappers,
        mangohud=env_map.get("MANGOHUD") == "1" or "mangohud" in wrappers,
        preserve_mangohud_config=True,
        wined3d=env_map.get("PROTON_USE_WINED3D") == "1",
        no_esync=env_map.get("PROTON_NO_ESYNC") == "1",
        no_fsync=env_map.get("PROTON_NO_FSYNC") == "1",
        winedlloverrides=dll_overrides,
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

    manifest = game.game_dir / MANIFEST_V2
    tracked = [paths.shortcuts_vdf, paths.config_vdf]
    link_prefix = game.schema >= 2 and not game.native_linux and not remove_standalone_files
    if link_prefix:
        tracked.append(manifest)
    with ExitStack() as locks, ExitStack() as rollback:
        for anchor in sorted({paths.shortcuts_vdf, paths.config_vdf}):
            locks.enter_context(_steam_transaction_lock(anchor))
        for path in tracked:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(f"Unsafe Steam import file: {path}")
            if path.is_file():
                rollback.callback(atomic_write_bytes, path, path.read_bytes(), mode=path.stat().st_mode & 0o777)
            else:
                rollback.callback(path.unlink, missing_ok=True)

        appid_u32 = add_or_update_shortcut(
            paths,
            app_name=game.name,
            exe_path=game.exe_path,
            start_dir=game.exe_path.parent,
            icon_path=resolved_icon,
            launch_options=steam_launch or None,
            enable_proton=not game.native_linux,
            match_existing_name=match_existing_name,
            keep_matching_appid=True,
        )
        target_tool = compat_tool.strip() or game.proton_name.strip()
        if target_tool and not game.native_linux:
            set_shortcut_proton(paths.config_vdf, appid_u32, target_tool)

        target_compat = paths.compatdata_dir / str(appid_u32)
        if not game.native_linux and game.prefix_dir.is_symlink():
            raise ValueError(f"Refusing to import a symlinked source prefix: {game.prefix_dir}")
        if link_prefix:
            backup = None
            linked_before = symlink_points_to(target_compat, game.prefix_dir)
            if target_compat.is_symlink() and not linked_before:
                raise ValueError(f"Steam compatdata points at another prefix: {target_compat}")
            if target_compat.is_dir() and not target_compat.is_symlink():
                backup = _compatdata_backup_path(target_compat)
                os.replace(target_compat, backup)
                rollback.callback(os.replace, backup, target_compat)
            if not target_compat.is_symlink():
                rollback.callback(target_compat.unlink, missing_ok=True)
            pfx_link = game.prefix_dir / "pfx"
            if not pfx_link.exists() and not pfx_link.is_symlink():
                rollback.callback(pfx_link.unlink, missing_ok=True)
            link_compatdata_to_prefix(target_compat, game.prefix_dir)
            steam_meta: dict[str, Any] = {
                "user_id": paths.user_id,
                "shortcut_appid": appid_u32,
                "compatdata_link": encode_path(target_compat, None),
            }
            if target_tool:
                steam_meta["compat_tool"] = target_tool
            if backup is not None:
                steam_meta["compatdata_backup"] = encode_path(backup, None)
            elif linked_before:
                prior_steam = game.meta.get("steam") or {}
                if prior_steam.get("shortcut_appid") == appid_u32 and prior_steam.get("compatdata_backup"):
                    steam_meta["compatdata_backup"] = prior_steam["compatdata_backup"]
            previous_meta = dict(game.meta)
            rollback.callback(setattr, game, "meta", previous_meta)
            game.meta["steam"] = steam_meta
            _write_manifest(game)
        elif not game.native_linux and game.prefix_dir.is_dir():
            _copy_import_prefix(game.prefix_dir, target_compat, remove_standalone_files)

        rollback.pop_all()

    if remove_standalone_files:
        if (
            not game.native_linux and not is_within(game.prefix_dir, target_compat)
            and not _prefix_used_by_other_game(game.prefix_dir, game.game_dir)
        ):
            _safe_remove_prefix_dir(game.prefix_dir, game.game_dir)
        remove_standalone_game(game, delete_files=False, delete_cache=True)

    try:
        sync_prefix_symlinks(paths)
    except OSError:
        logging.warning("Could not refresh named prefix links after Steam import", exc_info=True)
    if game.steam_appid and paths.grid_dir:
        try:
            from .artwork import download_steam_artwork

            download_steam_artwork(
                steam_appid=game.steam_appid,
                shortcut_appid_u32=appid_u32,
                grid_dir=paths.grid_dir,
            )
        except Exception as exc:
            logging.debug("Skipping Steam artwork download for %s: %s", game.name, exc)
    return appid_u32


def add_installed_game_to_steam(paths: SteamPaths, game_dir: Path, uid: str) -> int:
    """Add a managed game, keeping existing shortcuts and any foreign compatdata."""
    from .runtime_manager import is_win32_prefix

    if not paths.userdata_dir.is_dir():
        raise ValueError("The Steam account for this shortcut is unavailable")
    manifest_path = find_manifest(game_dir)
    if manifest_path is None:
        raise ValueError(f"Game manifest is unavailable: {game_dir}")
    game = _load_manifest(manifest_path)
    if game is None or not uid or uid != game.uid or not game.exe_path.is_file():
        raise ValueError(f"Installed game is unavailable or changed: {game_dir}")

    tracked = (paths.shortcuts_vdf, paths.config_vdf, manifest_path)
    anchors = sorted({paths.shortcuts_vdf, paths.config_vdf})
    with ExitStack() as locks:
        for anchor in anchors:
            locks.enter_context(_steam_transaction_lock(anchor))
        if any(path.is_symlink() for path in tracked):
            raise ValueError("Refusing to replace a symlinked Steam file or game manifest")
        entries = load_shortcuts(paths.shortcuts_vdf, strict=True)
        existing = match_owned_shortcuts(entries, [game], paths.user_id)
        if existing:
            for entry, _ in shortcut_path_repairs(entries, [game], paths.user_id):
                if not update_shortcut_paths(
                    paths, entry.appid_u32, game.exe_path, repaired_shortcut_start_dir(entry, game),
                    expected_exe=entry.exe,
                ):
                    raise RuntimeError(f"Could not update existing Steam shortcut {entry.appid_u32}")
            appid = next(iter(existing))
            if (
                not game.native_linux
                and not game.prefix_dir.is_symlink()
                and not is_win32_prefix(game.prefix_dir)
            ):
                compat = paths.compatdata_dir / str(appid)
                compat_missing = not compat.exists() and not compat.is_symlink()
                compat_empty = compat.is_dir() and not compat.is_symlink() and not any(compat.iterdir())
                if compat_missing or compat_empty or symlink_points_to(compat, game.prefix_dir):
                    if compat_empty:
                        compat.rmdir()
                    backup = link_compatdata_to_prefix(compat, game.prefix_dir)
                    if game.schema >= 2:
                        steam_meta: dict[str, Any] = dict(game.meta.get("steam") or {})
                        steam_meta.update({
                            "user_id": paths.user_id,
                            "shortcut_appid": appid,
                            "compatdata_link": encode_path(compat, None),
                        })
                        if game.proton_name.strip():
                            steam_meta.setdefault("compat_tool", game.proton_name.strip())
                        if backup is not None:
                            steam_meta["compatdata_backup"] = encode_path(backup, None)
                        if game.meta.get("steam") != steam_meta:
                            game.meta["steam"] = steam_meta
                            _write_manifest(game)
            return appid
        appid, _ = compute_shortcut_appid(f'"{game.exe_path}"', game.name)
        if any(entry.appid_u32 == appid for entry in entries):
            raise ValueError(f"Steam AppID {appid} already belongs to another shortcut")

        compat = paths.compatdata_dir / str(appid)
        had_link = compat.is_symlink()
        pfx_link = game.prefix_dir / "pfx"
        had_pfx = pfx_link.exists() or pfx_link.is_symlink()
        if not game.native_linux:
            if game.prefix_dir.is_symlink() or is_win32_prefix(game.prefix_dir):
                raise ValueError(f"Unsupported game prefix: {game.prefix_dir}")
            if (compat.exists() or had_link) and not (
                had_link and symlink_points_to(compat, game.prefix_dir)
            ):
                raise ValueError(f"Steam compatdata {appid} already contains a different prefix")

        before = {path: path.read_bytes() if path.is_file() else None for path in tracked}
        try:
            return import_standalone_to_steam(paths, game, match_existing_name=False)
        except Exception:
            for path, original in before.items():
                if original is None:
                    path.unlink(missing_ok=True)
                elif not path.is_file() or path.read_bytes() != original:
                    atomic_write_bytes(path, original)
            if not had_link and symlink_points_to(compat, game.prefix_dir):
                compat.unlink()
            if not had_pfx and symlink_points_to(pfx_link, game.prefix_dir):
                pfx_link.unlink()
            raise
