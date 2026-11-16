"""Standalone (non-Steam) game management via umu-run and bidirectional Steam migration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .scanner import detect_steam_appid
from .steam_paths import SteamPaths, _iter_compat_tool_dirs, _list_steam_library_roots, _read_compat_tool_id, find_steam_root, list_steam_users
from .steam_vdf import (
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

GAMES_DIR = Path.home() / "Games"
PREFIXES_DIR = GAMES_DIR / ".prefixes"
APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"
UMU_RUN_PATH = Path.home() / ".local" / "share" / "DeckInstall" / "runtime" / "umu" / "umu-run"
STANDALONE_MANIFEST = ".deckinstall_standalone.json"

_KNOWN_WRAPPERS = frozenset({"gamemoderun", "lsfg-vk", "mangohud", "gamescope"})


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


def _sanitize_dir_name(name: str) -> str:
    """Return a filesystem-safe folder name."""
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip() or "Game"


def _slugify(name: str) -> str:
    """Return a lowercase hyphenated slug for .desktop filenames."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "game"


def _is_under_dir(path: Path, parent: Path) -> bool:
    """Return True if path resolves inside parent (or equals parent)."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_proton_path(proton_name: str) -> str:
    """Resolve a Proton/GE-Proton tool name to a filesystem path for PROTONPATH when possible."""
    name = proton_name.strip()
    if not name:
        return "GE-Proton"
    as_path = Path(name)
    if as_path.is_dir() and (as_path / "proton").is_file():
        return str(as_path)

    steam_root = find_steam_root() or (Path.home() / ".local" / "share" / "Steam")
    for compat_dir in _iter_compat_tool_dirs(steam_root):
        direct = compat_dir / name
        if direct.is_dir() and (direct / "proton").is_file():
            return str(direct)
        try:
            for entry in compat_dir.iterdir():
                vdf_file = entry / "compatibilitytool.vdf"
                if entry.is_dir() and vdf_file.is_file() and (entry / "proton").is_file():
                    if _read_compat_tool_id(vdf_file, entry.name) == name:
                        return str(entry)
        except OSError:
            continue

    for lib in _list_steam_library_roots(steam_root):
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        direct = common / name
        if direct.is_dir() and (direct / "proton").is_file():
            return str(direct)
        if name == "proton_experimental":
            cand = common / "Proton - Experimental"
            if (cand / "proton").is_file():
                return str(cand)
        elif name == "proton_hotfix":
            cand = common / "Proton - Hotfix"
            if (cand / "proton").is_file():
                return str(cand)
        else:
            m = re.fullmatch(r"proton_(\d+)", name)
            if m:
                ver = m.group(1)
                for folder in (f"Proton {ver}.0", f"Proton {ver}"):
                    cand = common / folder
                    if (cand / "proton").is_file():
                        return str(cand)

    return name


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
        before = [t for t in tokens if _is_env_assignment(t) or t.strip("\"'") in _KNOWN_WRAPPERS]
        after = [t for t in tokens if not (_is_env_assignment(t) or t.strip("\"'") in _KNOWN_WRAPPERS)]

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


def _write_launch_script(
    script_path: Path,
    exe_path: Path,
    prefix_dir: Path,
    proton_name: str,
    winedlloverrides: str,
    launch_options: str,
) -> None:
    """Generate an executable launch.sh script invoking umu-run."""
    env_pairs, wrappers, game_args, effective_dll = _parse_launch_tokens(
        launch_options, winedlloverrides
    )
    proton_path = _resolve_proton_path(proton_name)

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(exe_path.parent))}",
        f"export WINEPREFIX={shlex.quote(str(prefix_dir))}",
        f"export PROTONPATH={shlex.quote(proton_path)}",
        "export GAMEID=umu-default",
        "export STORE=none",
    ]
    if effective_dll:
        lines.append(f"export WINEDLLOVERRIDES={shlex.quote(effective_dll)}")

    seen_keys = {"WINEPREFIX", "PROTONPATH", "GAMEID", "STORE", "WINEDLLOVERRIDES"}
    for key, val in env_pairs:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        lines.append(f"export {key}={shlex.quote(val)}")

    cmd_parts = [shlex.quote(w) for w in wrappers]
    cmd_parts.append(shlex.quote(str(UMU_RUN_PATH)))
    cmd_parts.append(shlex.quote(str(exe_path)))
    cmd_parts.extend(shlex.quote(arg) for arg in game_args)
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
    slug = _slugify(clean_name)
    candidate = APPLICATIONS_DIR / f"deckinstall-{slug}.desktop"
    if not candidate.exists() or _desktop_points_to_game_dir(candidate, target_game_dir):
        return candidate
    try:
        key = str(target_game_dir.resolve())
    except OSError:
        key = str(target_game_dir)
    short_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    hashed = APPLICATIONS_DIR / f"deckinstall-{slug}-{short_hash}.desktop"
    if not hashed.exists() or _desktop_points_to_game_dir(hashed, target_game_dir):
        return hashed
    idx = 2
    while True:
        numbered = APPLICATIONS_DIR / f"deckinstall-{slug}-{short_hash}-{idx}.desktop"
        if not numbered.exists() or _desktop_points_to_game_dir(numbered, target_game_dir):
            return numbered
        idx += 1


def _write_desktop_file(
    desktop_path: Path,
    game_name: str,
    launch_script: Path,
    game_dir: Path,
    icon_path: Path | None = None,
) -> None:
    """Create or update the ~/.local/share/applications/deckinstall-<slug>.desktop file."""
    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={game_name}",
        f"Exec={_desktop_quote(launch_script)}",
        f"Path={game_dir}",
        "Terminal=false",
        "Categories=Game;",
        "StartupNotify=true",
    ]
    if icon_path is not None and icon_path.is_file():
        lines.append(f"Icon={icon_path}")
    _atomic_write_text(desktop_path, "\n".join(lines) + "\n")


def _write_manifest(game: StandaloneGame) -> None:
    """Persist .deckinstall_standalone.json inside game.game_dir."""
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
    }
    _atomic_write_text(
        game.game_dir / STANDALONE_MANIFEST,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def _copy_or_move_dir_contents(src_dir: Path, dst_dir: Path, move: bool) -> None:
    """Copy or move all entries from src_dir into dst_dir."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if move:
            if target.exists() or target.is_symlink():
                if item.is_dir() and not item.is_symlink() and target.is_dir() and not target.is_symlink():
                    _copy_or_move_dir_contents(item, target, move=True)
                    try:
                        item.rmdir()
                    except OSError:
                        pass
                continue
            shutil.move(str(item), str(target))
        else:
            if item.is_symlink():
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(item.readlink())
            elif item.is_dir():
                shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)


def install_standalone_game(
    game_name: str,
    exe_path: Path,
    game_dir: Path,
    proton_name: str = "",
    steam_appid: int | None = None,
    winedlloverrides: str = "",
    launch_options: str = "",
    icon_path: Path | None = None,
) -> StandaloneGame:
    """Standardize a game in ~/Games/<name>/, create prefix + launch.sh + .desktop + manifest."""
    _validate_launch_options(launch_options)
    _validate_launch_options(winedlloverrides)

    clean_name = game_name.strip() or game_dir.name
    safe_dir_name = _sanitize_dir_name(clean_name)
    GAMES_DIR.mkdir(parents=True, exist_ok=True)
    PREFIXES_DIR.mkdir(parents=True, exist_ok=True)

    src_dir = game_dir.resolve()
    src_exe = exe_path.resolve()

    if _is_under_dir(src_dir, GAMES_DIR) and src_dir != GAMES_DIR.resolve():
        target_game_dir = game_dir
        target_exe_path = exe_path
    else:
        target_game_dir = GAMES_DIR / safe_dir_name
        if target_game_dir.exists() and target_game_dir.resolve() != src_dir:
            idx = 2
            while (GAMES_DIR / f"{safe_dir_name} ({idx})").exists():
                idx += 1
            target_game_dir = GAMES_DIR / f"{safe_dir_name} ({idx})"
        if target_game_dir.resolve() != src_dir:
            try:
                rel_exe = src_exe.relative_to(src_dir)
            except ValueError:
                rel_exe = Path(src_exe.name)
            shutil.move(str(game_dir), str(target_game_dir))
            target_exe_path = target_game_dir / rel_exe
        else:
            target_exe_path = exe_path

    prefix_dir = PREFIXES_DIR / _sanitize_dir_name(target_game_dir.name)
    if prefix_dir.is_symlink():
        prefix_dir.unlink()
    prefix_dir.mkdir(parents=True, exist_ok=True)

    if steam_appid is None:
        steam_appid = detect_steam_appid(target_game_dir)

    launch_script = target_game_dir / "launch.sh"
    desktop_file = _resolve_desktop_file_path(clean_name, target_game_dir)

    game = StandaloneGame(
        name=clean_name,
        game_dir=target_game_dir,
        exe_path=target_exe_path,
        prefix_dir=prefix_dir,
        proton_name=proton_name.strip(),
        steam_appid=steam_appid,
        winedlloverrides=winedlloverrides.strip(),
        launch_options=launch_options.strip(),
        launch_script=launch_script,
        desktop_file=desktop_file,
    )

    _write_launch_script(
        launch_script,
        game.exe_path,
        game.prefix_dir,
        game.proton_name,
        game.winedlloverrides,
        game.launch_options,
    )
    _write_desktop_file(
        desktop_file,
        game.name,
        launch_script,
        game.game_dir,
        icon_path=icon_path,
    )
    _write_manifest(game)
    return game


def _load_manifest(manifest_path: Path) -> StandaloneGame | None:
    """Load a StandaloneGame from a .deckinstall_standalone.json file."""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(raw, dict):
            return None
        game_dir = Path(str(raw.get("game_dir") or manifest_path.parent))
        name = str(raw.get("name") or game_dir.name)
        exe_path = Path(str(raw["exe_path"]))
        prefix_dir = Path(str(raw["prefix_dir"]))
        proton_name = str(raw.get("proton_name") or "")
        sa_raw = raw.get("steam_appid")
        steam_appid = int(sa_raw) if isinstance(sa_raw, int) else None
        winedlloverrides = str(raw.get("winedlloverrides") or "")
        launch_options = str(raw.get("launch_options") or "")
        launch_script = Path(str(raw.get("launch_script") or (game_dir / "launch.sh")))
        desktop_file = Path(
            str(raw.get("desktop_file") or (APPLICATIONS_DIR / f"deckinstall-{_slugify(name)}.desktop"))
        )
        return StandaloneGame(
            name=name,
            game_dir=game_dir,
            exe_path=exe_path,
            prefix_dir=prefix_dir,
            proton_name=proton_name,
            steam_appid=steam_appid,
            winedlloverrides=winedlloverrides,
            launch_options=launch_options,
            launch_script=launch_script,
            desktop_file=desktop_file,
        )
    except Exception as exc:
        logging.warning("Failed to load standalone manifest %s: %s", manifest_path, exc)
        return None


def list_standalone_games(games_dir: Path | None = None) -> list[StandaloneGame]:
    """Return all standalone games discovered under ~/Games/*/.deckinstall_standalone.json."""
    root = games_dir if games_dir is not None else GAMES_DIR
    if not root.is_dir():
        return []
    games: list[StandaloneGame] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for child in entries:
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest = child / STANDALONE_MANIFEST
        if manifest.is_file():
            game = _load_manifest(manifest)
            if game is not None:
                games.append(game)
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
    manifest = game_dir / STANDALONE_MANIFEST
    game = _load_manifest(manifest)
    if game is None:
        raise FileNotFoundError(f"Standalone manifest not found in {game_dir}")

    game.winedlloverrides = winedlloverrides.strip()
    game.launch_options = launch_options.strip()
    if proton_name.strip():
        game.proton_name = proton_name.strip()

    _write_launch_script(
        game.launch_script,
        game.exe_path,
        game.prefix_dir,
        game.proton_name,
        game.winedlloverrides,
        game.launch_options,
    )
    _write_manifest(game)
    return game


def remove_standalone_game(game: StandaloneGame, delete_files: bool = False) -> None:
    """Remove standalone integration files and optionally delete game & prefix directories."""
    for fpath in (game.desktop_file, game.launch_script, game.game_dir / STANDALONE_MANIFEST):
        try:
            if fpath.exists() or fpath.is_symlink():
                fpath.unlink()
        except OSError as exc:
            logging.warning("Failed to remove %s: %s", fpath, exc)

    if delete_files:
        if game.game_dir.exists():
            shutil.rmtree(game.game_dir, ignore_errors=True)
        if game.prefix_dir.is_symlink():
            game.prefix_dir.unlink(missing_ok=True)
        elif game.prefix_dir.exists():
            shutil.rmtree(game.prefix_dir, ignore_errors=True)


def _unlink_prefix_symlinks_for_compat(
    appid_u32: int,
    compat_dir: Path,
    preferred_name: str,
) -> None:
    """Remove any Shortix-style symlinks in PREFIXES_DIR pointing to compat_dir and update manifest."""
    if not PREFIXES_DIR.is_dir():
        return
    manifest_file = PREFIXES_DIR / ".deckinstall_links.json"
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

    candidates = [PREFIXES_DIR / preferred_name]
    try:
        candidates.extend(PREFIXES_DIR.iterdir())
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
    if start_str and Path(start_str).is_dir():
        game_dir = Path(start_str)
    else:
        game_dir = infer_game_root_from_exe(exe_path)

    game_name = shortcut.app_name.strip() or game_dir.name or f"Game {shortcut.appid_u32}"
    safe_name = _sanitize_dir_name(game_name)
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
        clean_tokens.extend(game_args)
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
    )

    if src_pfx.is_dir() and not src_pfx.is_symlink():
        _copy_or_move_dir_contents(src_pfx, game.prefix_dir, move=not should_copy)
        if not should_copy:
            shutil.rmtree(src_compat, ignore_errors=True)

    if remove_from_steam:
        remove_shortcut(paths, shortcut.appid_u32)

    return game


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
    )

    appid_u32 = add_or_update_shortcut(
        paths,
        app_name=game.name,
        exe_path=game.exe_path,
        start_dir=game.exe_path.parent,
        launch_options=steam_launch or None,
        enable_proton=True,
    )

    target_tool = compat_tool.strip() or game.proton_name.strip()
    if target_tool:
        set_shortcut_proton(paths.config_vdf, appid_u32, target_tool)

    target_compat = paths.compatdata_dir / str(appid_u32)
    target_pfx = target_compat / "pfx"
    if game.prefix_dir.is_dir() and not game.prefix_dir.is_symlink():
        _copy_or_move_dir_contents(game.prefix_dir, target_pfx, move=remove_standalone_files)
        if remove_standalone_files:
            shutil.rmtree(game.prefix_dir, ignore_errors=True)

    if remove_standalone_files:
        remove_standalone_game(game, delete_files=False)

    sync_prefix_symlinks(paths)
    return appid_u32
