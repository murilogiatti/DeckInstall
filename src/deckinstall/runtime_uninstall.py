"""Uninstall the runtimes DeckInstall downloaded: umu-run, Steam Runtime 4, GE-Proton, MangoHud, OptiScaler.

Each item can be removed on its own or all together. Nothing here is needed to keep games and
saves: "Update runtimes" (or the next launch) downloads what is missing again. Removing the
GE-Proton that "Default" games use moves them to the newest one left.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import runtime_manager as rm
from .steam_paths import SteamPaths, dir_size_bytes

if TYPE_CHECKING:
    from .standalone import StandaloneGame

UMU, STEAMRT4, MANGOHUD, OPTISCALER, GE = "umu", "steamrt4", "mangohud", "optiscaler", "ge"


@dataclass(frozen=True)
class RuntimeItem:
    kind: str
    name: str
    path: Path
    size_bytes: int
    users: tuple[str, ...] = ()  # games that use it (custom Proton tools)


def _managed_mangohud_layers() -> list[tuple[Path, int]]:
    """List the user Vulkan manifests pointing into DeckInstall's MangoHud dir."""
    mh_resolved = rm.get_mangohud_dir().resolve()
    layer_dir = rm.get_user_vulkan_layer_dir()
    layers = []
    for json_name in ("MangoHud.x86_64.json", "MangoHud.x86.json", "MangoHud.json"):
        layer = layer_dir / json_name
        if not layer.is_file() or layer.is_symlink():
            continue
        try:
            data = json.loads(layer.read_text(encoding="utf-8"))
            if Path(str(data["layer"]["library_path"])).resolve().is_relative_to(mh_resolved):
                layers.append((layer, layer.stat().st_size))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return layers


def _proton_users(paths: SteamPaths | None, games: list[StandaloneGame]) -> dict[str, list[str]]:
    """{Proton folder/tool name: games using it} for DeckInstall games and Steam shortcuts."""
    from .proton_policy import effective_proton_name

    users: dict[str, list[str]] = {}
    for game in games:
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


def affected_steam_appids(paths: SteamPaths | None, tool_names: Iterable[str]) -> list[int]:
    """Return Steam AppIDs in config.vdf currently mapped to any tool name in tool_names."""
    wanted = {name.strip() for name in tool_names if name and name.strip()}
    if paths is None or not wanted:
        return []
    try:
        from .steam_vdf import list_compat_tool_usage

        seen: list[int] = []
        for u in list_compat_tool_usage(paths):
            if u.tool_name in wanted and u.appid_u32 not in seen:
                seen.append(u.appid_u32)
        return seen
    except Exception:
        logging.exception("Could not read Steam compat-tool mappings for runtime cleanup")
        return []


def list_runtimes(paths: SteamPaths | None = None) -> list[RuntimeItem]:
    """What can be uninstalled now, with its size and (for custom Protons) the games using it."""
    from .standalone import list_standalone_games

    items: list[RuntimeItem] = []
    games = list_standalone_games()
    standalone_users = tuple(game.name for game in games if not game.native_linux)
    mh_dir = rm.get_mangohud_dir()
    fixed = (
        (UMU, "umu-run", rm.get_umu_dir()),
        (STEAMRT4, "Steam Runtime 4 (umu)", rm.get_steamrt4_dir()),
        (OPTISCALER, "OptiScaler", rm.get_optiscaler_dir().parent),
    )
    for kind, name, path in fixed:
        if path.is_dir() and not path.is_symlink():
            users_of_runtime = standalone_users if kind in (UMU, STEAMRT4) else ()
            items.append(RuntimeItem(kind, name, path, dir_size_bytes(path), users_of_runtime))
    mh_layers = _managed_mangohud_layers()
    mh_layer_bytes = sum(size for _, size in mh_layers)
    if mh_dir.is_dir() and not mh_dir.is_symlink():
        items.append(RuntimeItem(MANGOHUD, "MangoHud", mh_dir, dir_size_bytes(mh_dir) + mh_layer_bytes))
    elif mh_layers:
        items.append(RuntimeItem(MANGOHUD, "MangoHud", mh_layers[0][0], mh_layer_bytes))

    users = _proton_users(paths, games)
    compat_dir = rm.get_compat_tools_dir()
    managed_tools: list[Path] = []
    if compat_dir.is_dir():
        try:
            for entry in compat_dir.iterdir():
                if rm.is_managed_compat_tool(entry):
                    managed_tools.append(entry)
        except OSError:
            managed_tools = []
    ge_order = {p: idx for idx, p in enumerate(rm.list_ge_proton_dirs(min_major=1))}
    managed_tools.sort(key=lambda p: (0 if p in ge_order else 1, ge_order.get(p, 0), p.name.lower()))
    for path in managed_tools:
        tool_users = list(users.get(path.name, ()))
        vdf_file = path / "compatibilitytool.vdf"
        if vdf_file.is_file():
            try:
                from .steam_paths import _read_compat_tool_id

                tid = _read_compat_tool_id(vdf_file, path.name)
                if tid != path.name:
                    for u_name in users.get(tid, ()):
                        if u_name not in tool_users:
                            tool_users.append(u_name)
            except OSError:
                pass
        items.append(RuntimeItem(GE, path.name, path, dir_size_bytes(path), tuple(tool_users)))
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
            elif item.kind == GE and rm.is_managed_compat_tool(item.path):
                rm.remove_compat_tool(item.path, managed_only=True)
                changed = True
            else:
                raise ValueError(f"Runtime is not managed by DeckInstall: {item.path}")
            if changed:
                removed.append(item.name)
        except (OSError, ValueError) as exc:
            errors.append(f"{item.name}: {exc}")
    newest_after = newest_ge_proton()
    if newest_before is not None and (newest_after is None or newest_after.name != newest_before.name):
        # "Default" games pointed at the removed newest version: rewrite them to match what remains.
        retarget_default_games("")
    return removed, errors
