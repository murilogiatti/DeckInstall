"""Actions for integrating external launcher games into Steam or standalone mode."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .launcher_scan import ExternalLauncherGame
from .standalone import StandaloneGame, install_standalone_game, resolve_game_icon
from .steam_paths import SteamPaths
from .steam_vdf import (
    _copy_save_pair_if_newer,
    _iter_prefix_save_pairs,
    _iter_programdata_save_pairs,
    _prefix_programdata_dir,
    _prefix_users_dir,
    _safe_parents_within,
    add_or_update_shortcut,
    sync_prefix_symlinks,
)

_PRESETS_FALLBACK_FILE = Path.home() / ".config" / "DeckInstall" / "external_launch_presets.json"


def _external_game_preset_key(ext: ExternalLauncherGame) -> str:
    digest = hashlib.sha256(f"{ext.source}:{ext.exe_path}".encode()).hexdigest()[:16]
    return f"external_launch_presets/{digest}"


def _combine_launch_options(base_options: str, extra_args: str) -> str:
    base = base_options.strip()
    extra = extra_args.strip()
    if not extra:
        return base
    if not base:
        return extra
    if "%command%" in base:
        return base.replace("%command%", f"%command% {extra}", 1)
    return f"{base} {extra}"


def build_steam_shortcut_payload(
    ext: ExternalLauncherGame,
    launch_options: str = "",
    use_launcher_cmd: bool = False,
) -> dict[str, Any]:
    """Build keyword payload for add_or_update_shortcut from an ExternalLauncherGame."""
    if use_launcher_cmd and ext.launch_cmd:
        target_exe = Path(ext.launch_cmd[0])
        extra_args = shlex.join(ext.launch_cmd[1:]) if len(ext.launch_cmd) > 1 else ""
        resolved_launch = _combine_launch_options(launch_options, extra_args)
        start_dir = ext.game_dir if ext.game_dir.is_dir() else target_exe.parent
        enable_proton = False
    else:
        target_exe = ext.exe_path
        resolved_launch = launch_options.strip()
        start_dir = ext.exe_path.parent if ext.exe_path.parent.is_dir() else ext.game_dir
        enable_proton = ext.runner != "linux"

    icon_path = resolve_game_icon(ext.exe_path, ext.game_dir)
    return {
        "app_name": ext.name,
        "exe_path": target_exe,
        "start_dir": start_dir,
        "icon_path": icon_path,
        "launch_options": resolved_launch or None,
        "enable_proton": enable_proton,
    }


def import_external_saves_to_target(
    ext: ExternalLauncherGame,
    target_prefix: Path,
    target_name: str,
    auto_snapshot_fn: Callable[[Path, str], Any] | None = None,
) -> int:
    """Copy newer save files from an external game's Wine prefix into target_prefix."""
    if ext.prefix_dir is None:
        return 0
    return import_prefix_saves(ext.prefix_dir, target_prefix, target_name, auto_snapshot_fn)


def import_prefix_saves(
    src_prefix: Path,
    target_prefix: Path,
    target_name: str,
    auto_snapshot_fn: Callable[[Path, str], Any] | None = None,
) -> int:
    """Copy newer save files (users/*, ProgramData) from src_prefix into target_prefix."""
    if src_prefix.is_symlink() or not src_prefix.is_dir():
        return 0

    src_users = _prefix_users_dir(src_prefix)
    src_progdata = _prefix_programdata_dir(src_prefix)
    if src_users is None and src_progdata is None:
        return 0

    if auto_snapshot_fn is not None and target_prefix.is_dir():
        auto_snapshot_fn(target_prefix, target_name)

    if target_prefix.name == "pfx" or (target_prefix / "drive_c").is_dir():
        dst_drive_c = target_prefix / "drive_c"
    elif (target_prefix / "pfx").is_dir():
        dst_drive_c = target_prefix / "pfx" / "drive_c"
    else:
        dst_drive_c = target_prefix / "drive_c"

    if not _safe_parents_within(
        target_prefix, target_prefix.resolve(), dst_drive_c.relative_to(target_prefix).parts
    ):
        raise OSError(f"Unsafe target drive_c path: {dst_drive_c}")
    dst_drive_c.mkdir(parents=True, exist_ok=True)
    dst_drive_c_resolved = dst_drive_c.resolve()

    copied = 0
    errors: list[str] = []

    if src_users is not None:
        if not _safe_parents_within(dst_drive_c, dst_drive_c_resolved, ("users",)):
            raise OSError(f"Unsafe target users path: {dst_drive_c}")
        dst_users = dst_drive_c / "users"
        dst_users.mkdir(parents=True, exist_ok=True)
        dst_users_resolved = dst_users.resolve()
        if not dst_users_resolved.is_relative_to(dst_drive_c_resolved):
            raise OSError(f"Resolved users path escapes drive_c: {dst_users_resolved}")
        try:
            for src_file, dst_file in _iter_prefix_save_pairs(
                src_users, dst_users, raise_on_error=True
            ):
                copied += _copy_save_pair_if_newer(
                    src_file, dst_file, dst_users, dst_users_resolved, errors
                )
        except OSError as exc:
            errors.append(f"{src_users}: {exc}")

    if src_progdata is not None:
        if not _safe_parents_within(dst_drive_c, dst_drive_c_resolved, ("ProgramData",)):
            raise OSError(f"Unsafe target ProgramData path: {dst_drive_c}")
        dst_progdata = dst_drive_c / "ProgramData"
        dst_progdata.mkdir(parents=True, exist_ok=True)
        dst_progdata_resolved = dst_progdata.resolve()
        if not dst_progdata_resolved.is_relative_to(dst_drive_c_resolved):
            raise OSError(f"Resolved ProgramData path escapes drive_c: {dst_progdata_resolved}")
        try:
            for src_file, dst_file in _iter_programdata_save_pairs(
                src_progdata, dst_progdata, raise_on_error=True
            ):
                copied += _copy_save_pair_if_newer(
                    src_file, dst_file, dst_progdata, dst_progdata_resolved, errors
                )
        except OSError as exc:
            errors.append(f"{src_progdata}: {exc}")

    if errors:
        raise RuntimeError(f"Falha ao importar {len(errors)} save(s): {errors[0]}")
    return copied


def create_steam_shortcut_from_external(
    paths: SteamPaths,
    ext: ExternalLauncherGame,
    launch_options: str = "",
    use_launcher_cmd: bool = False,
    import_saves: bool = True,
) -> int:
    """Create a Non-Steam shortcut for an external launcher game and optionally import saves."""
    payload = build_steam_shortcut_payload(
        ext,
        launch_options=launch_options,
        use_launcher_cmd=use_launcher_cmd,
    )
    appid_u32 = add_or_update_shortcut(paths, **payload)

    if import_saves and ext.prefix_dir is not None and ext.prefix_dir.is_dir():
        target_pfx = paths.compatdata_dir / str(appid_u32) / "pfx"
        import_external_saves_to_target(ext, target_pfx, ext.name)

    sync_prefix_symlinks(paths)
    return appid_u32


def create_standalone_from_external(
    ext: ExternalLauncherGame,
    create_menu_shortcut: bool = True,
    create_desktop_shortcut: bool = False,
    launch_options: str = "",
    winedlloverrides: str = "",
) -> StandaloneGame:
    """Register an external launcher game as a DeckInstall standalone game (in-place)."""
    game = install_standalone_game(
        game_name=ext.name,
        exe_path=ext.exe_path,
        game_dir=ext.game_dir,
        winedlloverrides=winedlloverrides,
        launch_options=launch_options,
        create_menu_shortcut=create_menu_shortcut,
        create_desktop_shortcut=create_desktop_shortcut,
        keep_in_place=True,
        native_linux=(ext.runner == "linux"),
        source={"kind": "adopted", "origin": str(ext.source)},
    )
    if ext.prefix_dir is not None and ext.prefix_dir.is_dir():
        import_external_saves_to_target(ext, game.prefix_dir, game.name)
    return game


def load_external_launch_presets(ext: ExternalLauncherGame) -> str:
    """Load saved launch options for an external launcher game."""
    key = _external_game_preset_key(ext)
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings("DeckInstall", "DeckInstall")
        val = settings.value(key, "")
        return str(val or "")
    except ImportError:
        if not _PRESETS_FALLBACK_FILE.is_file():
            return ""
        try:
            data = json.loads(_PRESETS_FALLBACK_FILE.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if isinstance(data, dict):
            return str(data.get(key, "") or "")
        return ""


def save_external_launch_presets(ext: ExternalLauncherGame, launch_options: str) -> None:
    """Persist launch options for an external launcher game."""
    key = _external_game_preset_key(ext)
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings("DeckInstall", "DeckInstall")
        settings.setValue(key, launch_options)
        settings.sync()
    except ImportError:
        data: dict[str, str] = {}
        if _PRESETS_FALLBACK_FILE.is_file():
            try:
                loaded = json.loads(_PRESETS_FALLBACK_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {str(k): str(v) for k, v in loaded.items()}
            except Exception:
                data = {}
        data[key] = launch_options
        _PRESETS_FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRESETS_FALLBACK_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
