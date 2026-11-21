"""Uninstall the runtimes DeckInstall downloaded: umu-run, Steam Runtime 4, GE-Proton, MangoHud, OptiScaler.

Each item can be removed on its own or all together. Nothing here is needed to keep games and
saves: "Update runtimes" (or the next launch) downloads what is missing again. Removing the
GE-Proton that "Default" games use moves them to the newest one left.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import runtime_manager as rm
from .steam_paths import SteamPaths, dir_size_bytes

UMU, STEAMRT4, MANGOHUD, OPTISCALER, GE = "umu", "steamrt4", "mangohud", "optiscaler", "ge"


@dataclass(frozen=True)
class RuntimeItem:
    kind: str
    name: str
    path: Path
    size_bytes: int
    users: tuple[str, ...] = ()  # games that use it (GE-Proton only)


def _proton_users(paths: SteamPaths | None) -> dict[str, list[str]]:
    """{Proton folder name: games using it} for DeckInstall games and Steam shortcuts."""
    from .proton_policy import effective_proton_name
    from .standalone import list_standalone_games

    users: dict[str, list[str]] = {}
    for game in list_standalone_games():
        name = effective_proton_name(game)
        if name:
            users.setdefault(name, []).append(game.name)
    if paths is not None:
        try:
            from .steam_vdf import list_compat_tool_usage

            for u in list_compat_tool_usage(paths):
                if u.tool_name and u.source != "Orphan":
                    users.setdefault(u.tool_name, []).append(u.name or str(u.appid_u32))
        except Exception:
            logging.exception("Could not read Steam compat-tool mappings")
    return users


def list_runtimes(paths: SteamPaths | None = None) -> list[RuntimeItem]:
    """What can be uninstalled now, with its size and (for GE-Proton) the games that use it."""
    items: list[RuntimeItem] = []
    fixed = (
        (UMU, "umu-run", rm.get_umu_dir()),
        (STEAMRT4, "Steam Runtime 4 (umu)", rm.get_steamrt4_dir()),
        (MANGOHUD, "MangoHud", rm.get_mangohud_dir()),
        (OPTISCALER, "OptiScaler", rm.get_optiscaler_dir().parent),
    )
    for kind, name, path in fixed:
        if path.is_dir() and not path.is_symlink():
            items.append(RuntimeItem(kind, name, path, dir_size_bytes(path)))
    users = _proton_users(paths)
    for path in rm.list_ge_proton_dirs(min_major=1):
        if rm.is_managed_ge_proton(path):
            items.append(RuntimeItem(GE, path.name, path, dir_size_bytes(path), tuple(users.get(path.name, ()))))
    return items


def uninstall(items: list[RuntimeItem]) -> tuple[list[str], list[str]]:
    """Remove the items. Returns (removed names, errors)."""
    from .proton_policy import newest_ge_proton, retarget_default_games

    removed: list[str] = []
    errors: list[str] = []
    newest_before = newest_ge_proton()
    for item in items:
        try:
            if item.kind == UMU:
                changed = rm.uninstall_umu_run()
            elif item.kind == STEAMRT4:
                changed = rm.uninstall_steamrt4()
            elif item.kind == MANGOHUD:
                changed = rm.uninstall_mangohud()
            elif item.kind == OPTISCALER:
                changed = rm.uninstall_optiscaler_package()
            elif item.kind == GE and rm.is_managed_ge_proton(item.path):
                changed = rm._remove_runtime_dir(item.path)
            else:
                raise ValueError(f"Runtime is not managed by DeckInstall: {item.path}")
            if changed:
                removed.append(item.name)
        except (OSError, ValueError) as exc:
            errors.append(f"{item.name}: {exc}")
    newest_after = newest_ge_proton()
    if newest_before is not None and (newest_after is None or newest_after.name != newest_before.name):
        # "Default" games pointed at the removed newest version: rewrite them to the newest left.
        retarget_default_games("")
    return removed, errors
