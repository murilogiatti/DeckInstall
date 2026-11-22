"""Launch presets (MangoHud, gamemode, DLSS, NVAPI, PRIME, Wayland, wined3d, esync/fsync).

One place to read the presets out of a launch-options string, re-apply them after a change
and describe them; the single presets dialog and every caller go through here.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from .steam_vdf import apply_launch_presets

# Boolean presets, in the order the dialog and the summary show them.
FLAGS = ("gamemode", "mangohud", "dlss_upgrade", "nvapi", "prime", "wayland", "wined3d", "no_esync", "no_fsync")
MANGOHUD_LEVELS = (1, 2, 3, 4)


def parse_presets(opts: str) -> dict[str, Any]:
    """Parse known preset flags and values from a LaunchOptions string."""
    try:
        tokens_list = shlex.split(opts) if opts.strip() else []
    except ValueError:
        tokens_list = opts.split()
    tokens_now = set(tokens_list)

    mh_level = 0
    mh_position = ""
    mh_fps_limit = 0
    for tok in tokens_list:
        if tok.startswith("MANGOHUD_CONFIG="):
            val = tok.split("=", 1)[1]
            parts = [part.strip() for part in val.split(",") if part.strip()]
            parts_set = set(parts)
            if "horizontal" in parts_set and ("hud_compact" in parts_set or "position=top-center" in parts_set):
                mh_level = 2
            else:
                for part_s in parts:
                    for lv in MANGOHUD_LEVELS:
                        if part_s == f"preset={lv}":
                            mh_level = lv
                            break
            for part_s in parts:
                if part_s.startswith("position="):
                    mh_position = part_s.split("=", 1)[1].strip()
                elif part_s.startswith("fps_limit="):
                    try:
                        parsed_fps = int(part_s.split("=", 1)[1].strip())
                        if parsed_fps > 0:
                            mh_fps_limit = parsed_fps
                    except ValueError:
                        pass

    winedll_from_tokens = ""
    for tok in tokens_list:
        if tok.startswith("WINEDLLOVERRIDES="):
            winedll_from_tokens = tok.split("=", 1)[1]

    return {
        "tokens_list": tokens_list,
        "tokens_now": tokens_now,
        "gamemode": "gamemoderun" in tokens_now,
        "mangohud": "MANGOHUD=1" in tokens_now,
        "mh_level": mh_level,
        "mh_position": mh_position,
        "mh_fps_limit": mh_fps_limit,
        "nvapi": "PROTON_ENABLE_NVAPI=1" in tokens_now or "DXVK_ENABLE_NVAPI=1" in tokens_now,
        "prime": (
            "__NV_PRIME_RENDER_OFFLOAD=1" in tokens_now
            or "__GLX_VENDOR_LIBRARY_NAME=nvidia" in tokens_now
        ),
        "wayland": "PROTON_ENABLE_WAYLAND=1" in tokens_now,
        "dlss_upgrade": "PROTON_DLSS_UPGRADE=1" in tokens_now,
        "wined3d": "PROTON_USE_WINED3D=1" in tokens_now,
        "no_esync": "PROTON_NO_ESYNC=1" in tokens_now,
        "no_fsync": "PROTON_NO_FSYNC=1" in tokens_now,
        "winedll": winedll_from_tokens,
    }


def apply_presets(
    opts: str,
    state: Mapping[str, Any] | None = None,
    *,
    winedlloverrides: str | None = None,
    native_linux: bool = False,
    **changes: Any,
) -> str:
    """Rewrite `opts` with the presets in `state` (default: the ones already in `opts`) plus `changes`.

    Custom options are preserved. Proton-only presets are dropped for native Linux games.
    """
    merged: dict[str, Any] = dict(parse_presets(opts) if state is None else state)
    merged.update(changes)
    windows = not native_linux

    def flag(key: str) -> bool:
        return bool(merged.get(key))

    level = merged.get("mh_level")
    fps = merged.get("mh_fps_limit")
    return apply_launch_presets(
        opts,
        gamemode=flag("gamemode"),
        mangohud=flag("mangohud"),
        wined3d=windows and flag("wined3d"),
        no_esync=windows and flag("no_esync"),
        no_fsync=windows and flag("no_fsync"),
        winedlloverrides=winedlloverrides,
        dlss_upgrade=windows and flag("dlss_upgrade"),
        mangohud_level=level if isinstance(level, int) and level in MANGOHUD_LEVELS else None,
        nvidia_nvapi=windows and flag("nvapi"),
        prime_offload=flag("prime"),
        proton_wayland=windows and flag("wayland"),
        mangohud_position=str(merged.get("mh_position") or ""),
        mangohud_fps_limit=fps if isinstance(fps, int) and fps > 0 else None,
    )


def active_flags(opts: str) -> list[str]:
    """Names (from FLAGS) of the presets turned on in `opts`, in display order."""
    state = parse_presets(opts)
    return [key for key in FLAGS if state.get(key)]
