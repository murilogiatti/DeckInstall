"""Launch presets (MangoHud, gamemode, frame cap, upscaler upgrades, PRIME, Wayland, wined3d, ...).

Checked against what UMU-Proton 11 (the default Proton) and Valve's Proton 11 really read (their
`proton` scripts and the DXVK/VKD3D-Proton DLLs), not against older guides.

One place to read the presets out of a launch-options string, re-apply them after a change
and describe them; the single presets dialog and every caller go through here.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .steam_vdf import apply_launch_presets

# Boolean presets, in the order the dialog and the summary show them.
FLAGS: tuple[str, ...] = (
    "gamemode",
    "mangohud",
    "dlss_upgrade",
    "nvapi",
    "prime",
    "wayland",
    "wined3d",
    "no_esync",
    "no_fsync",
    "debug_log",
)
MANGOHUD_LEVELS = (1, 2, 3, 4)


@dataclass(frozen=True)
class Toggle:
    """An on/off preset that is just environment variables (exact tokens, so a custom value the
    user typed, e.g. PROTON_FSR4_UPGRADE=4.0.1, is left alone and the box shows off), or one entry
    of DXVK_CONFIG (`dxvk`), which shares one variable with the frame cap and the user's own entries."""

    key: str
    tokens: tuple[str, ...]
    section: str  # "gpu", "upscaler" or "compat"
    dxvk: str = ""


# Data, not code: one line per preset.
TOGGLES: tuple[Toggle, ...] = (
    Toggle("fsr3_upgrade", ("PROTON_FSR3_UPGRADE=1",), "upscaler"),
    Toggle("fsr4_upgrade", ("PROTON_FSR4_UPGRADE=1",), "upscaler"),
    Toggle("xess_upgrade", ("PROTON_XESS_UPGRADE=1",), "upscaler"),
    Toggle("vendor_amd", (), "gpu", dxvk="dxgi.customVendorId=1002"),
    Toggle("descriptor_heap", ("VKD3D_CONFIG=descriptor_heap",), "gpu"),
    Toggle("disable_nvapi", ("PROTON_DISABLE_NVAPI=1",), "gpu"),
    Toggle("large_address", ("PROTON_FORCE_LARGE_ADDRESS_AWARE=1",), "compat"),
    Toggle("prefer_sdl", ("PROTON_PREFER_SDL=1",), "compat"),
    Toggle("no_ntsync", ("PROTON_NO_NTSYNC=1",), "compat"),
    # No Proton reads these any more (kept so a game that has them can turn them off).
    Toggle("fsr4_rdna3", ("PROTON_FSR4_RDNA3_UPGRADE=1",), "upscaler"),
    Toggle("hdr", ("PROTON_ENABLE_HDR=1",), "gpu"),
)
TOGGLE_TOKENS = frozenset(tok for toggle in TOGGLES for tok in toggle.tokens)
# Old presets: shown only while on (so they can be turned off), never offered or recommended. NVAPI is
# on by default; esync is gone; RDNA3 FSR 4 and HDR flags are gone; the upscaler upgrades, Wayland,
# SDL and NTSYNC variables were GE-Proton's, which DeckInstall no longer uses (UMU-Proton ignores them).
LEGACY = frozenset({
    "nvapi", "no_esync", "fsr4_rdna3", "hdr",
    "dlss_upgrade", "fsr3_upgrade", "fsr4_upgrade", "xess_upgrade", "wayland", "prefer_sdl", "no_ntsync",
})
# Frame cap (works in Steam mode too): DXVK reads it from DXVK_CONFIG (DXVK_FRAME_RATE is gone,
# checked in Proton 11 and UMU-Proton 11), VKD3D-Proton from VKD3D_FRAME_RATE.
FRAME_RATES = (30, 40, 45, 60, 90, 120, 144)
_DXVK_FRAME_KEYS = ("dxgi.maxFrameRate", "d3d9.maxFrameRate")
_DXVK_MANAGED = frozenset((*_DXVK_FRAME_KEYS, *(t.dxvk.split("=", 1)[0] for t in TOGGLES if t.dxvk)))
_MANAGED_KEYS = ("DXVK_CONFIG", "VKD3D_FRAME_RATE", "DXVK_FRAME_RATE")
FLAGS = FLAGS + tuple(toggle.key for toggle in TOGGLES)


def _dxvk_entries(tokens: list[str]) -> list[str]:
    """The `key=value` entries of every DXVK_CONFIG in the options, in order."""
    entries: list[str] = []
    for tok in tokens:
        if tok.startswith("DXVK_CONFIG="):
            entries += [e.strip().replace(" ", "") for e in tok.split("=", 1)[1].split(";") if e.strip()]
    return entries


@dataclass(frozen=True)
class Need:
    """What a preset needs, checked against the GPU the game runs on and its Proton.

    vendor: another vendor hides the preset (nothing to say); everything else greys it out with
    the reason, so the user knows why and what would make it work.
    """

    vendor: str = ""
    generations: tuple[str, ...] = ()
    proprietary_nvidia: bool = False
    min_nvidia: int = 0
    hybrid: bool = False
    wayland: bool = False  # a Wayland or gamescope session
    legacy: bool = False  # no Proton reads it any more: hidden unless already on


NEEDS: dict[str, Need] = {
    "descriptor_heap": Need(vendor="nvidia", proprietary_nvidia=True, min_nvidia=595),
    "disable_nvapi": Need(vendor="nvidia"),
    "vendor_amd": Need(vendor="intel"),
    "prime": Need(hybrid=True),
    **{key: Need(legacy=True) for key in LEGACY},
}

OK, HIDE, OFF = "ok", "hide", "off"


def proton_kind(name: str) -> str:
    """"umu", "ge", "cachyos", "valve" or "" (unknown) from a compatibility tool name or id."""
    low = name.lower()
    if "umu-proton" in low:
        return "umu"
    if "cachyos" in low:
        return "cachyos"
    if "ge-proton" in low or low.startswith("ge_") or "proton-ge" in low:
        return "ge"
    if "proton" in low:
        return "valve"
    return ""


def availability(key: str, gpu: Any, profile: Any, proton: str = "") -> tuple[str, str]:
    """(OK, "") or (HIDE | OFF, reason) for a preset on the game's GPU (a hardware.Gpu) and Proton.

    Reasons: "need_nvidia_driver", "need_rtx", "need_rdna4", "need_nvidia_595",
    "need_wayland". Unknown hardware or Proton never blocks.
    """
    need = NEEDS.get(key)
    if need is None:
        return OK, ""
    if need.legacy:
        return HIDE, ""
    if need.hybrid and profile is not None and not profile.is_hybrid:
        return HIDE, ""
    if gpu is not None:
        if need.vendor and gpu.vendor != need.vendor:
            return HIDE, ""
        if need.proprietary_nvidia and gpu.driver != "nvidia":
            return OFF, "need_nvidia_driver"
        if need.generations and gpu.generation and gpu.generation not in need.generations:
            return OFF, f"need_{need.generations[0]}"
        if need.min_nvidia and profile is not None and 0 < profile.nvidia_major < need.min_nvidia:
            return OFF, "need_nvidia_595"
    if need.wayland and profile is not None and profile.session == "x11":
        return OFF, "need_wayland"
    return OK, ""


def recommend(profile: Any, proton: str = "", frame_rate: int = 0) -> dict[str, Any]:
    """The presets this computer benefits from, as changes to apply (never turns anything off).

    Hybrid laptop: the game goes to the dedicated GPU. Upscalers are OptiScaler's (per game), not a
    preset. The frame cap is the user's choice (it depends on the screen and the battery), never here.
    """
    changes: dict[str, Any] = {}
    dedicated = profile.dedicated if profile is not None else None
    if dedicated is not None and not dedicated.integrated:
        changes["prime"] = True
    return changes


def recommended_upscaler(gpu: Any) -> str:
    """OptiScaler backend for a GPU: DLSS on RTX, XeSS on Intel, FSR 3.1 elsewhere (cheap on
    weak GPUs such as the Steam Deck's; XeSS without Intel hardware costs more); unknown: auto."""
    if gpu is None:
        return "auto"
    if gpu.vendor == "nvidia" and gpu.generation == "rtx" and gpu.driver == "nvidia":
        return "dlss"
    if gpu.vendor == "intel":
        return "xess"
    return "fsr3"


def parse_presets(opts: str) -> dict[str, Any]:
    """Parse known preset flags and values from a LaunchOptions string."""
    try:
        tokens_list = shlex.split(opts) if opts.strip() else []
    except ValueError:
        tokens_list = opts.split()
    tokens_now = set(tokens_list)
    command_idx = tokens_list.index("%command%") if "%command%" in tokens_now else len(tokens_list)

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
    frame_rates: dict[str, int] = {}
    dxvk = _dxvk_entries(tokens_list)
    for tok in tokens_list:
        key, _, value = tok.partition("=")
        if key == "WINEDLLOVERRIDES":
            winedll_from_tokens = value
        elif key in ("VKD3D_FRAME_RATE", "DXVK_FRAME_RATE") and value.isdigit():
            frame_rates[key] = int(value)
    for entry in dxvk:
        key, _, value = entry.partition("=")
        if key == "dxgi.maxFrameRate" and value.isdigit():
            frame_rates["dxvk"] = int(value)
    # The cap, wherever it is written (DXVK_FRAME_RATE and MangoHud's fps_limit: older versions).
    frame_rate = next(
        (frame_rates[k] for k in ("dxvk", "VKD3D_FRAME_RATE", "DXVK_FRAME_RATE") if frame_rates.get(k)),
        mh_fps_limit,
    )
    toggles = {
        toggle.key: (toggle.dxvk in dxvk) if toggle.dxvk else all(tok in tokens_now for tok in toggle.tokens)
        for toggle in TOGGLES
    }

    return {
        "tokens_list": tokens_list,
        "tokens_now": tokens_now,
        "gamemode": "gamemoderun" in tokens_now,
        "mangohud": "MANGOHUD=1" in tokens_now or "mangohud" in tokens_list[:command_idx],
        "mh_level": mh_level,
        "mh_position": mh_position,
        "mh_fps_limit": mh_fps_limit,
        "nvapi": "PROTON_ENABLE_NVAPI=1" in tokens_now or "DXVK_ENABLE_NVAPI=1" in tokens_now,
        "prime": (
            "__NV_PRIME_RENDER_OFFLOAD=1" in tokens_now
            or "__GLX_VENDOR_LIBRARY_NAME=nvidia" in tokens_now
            or "DRI_PRIME=1" in tokens_now
        ),
        "wayland": "PROTON_ENABLE_WAYLAND=1" in tokens_now,
        "dlss_upgrade": "PROTON_DLSS_UPGRADE=1" in tokens_now,
        "wined3d": "PROTON_USE_WINED3D=1" in tokens_now,
        "no_esync": "PROTON_NO_ESYNC=1" in tokens_now,
        "no_fsync": "PROTON_NO_FSYNC=1" in tokens_now,
        "debug_log": "UMU_LOG=1" in tokens_now or "PROTON_LOG=1" in tokens_now,
        "winedll": winedll_from_tokens,
        "frame_rate": frame_rate,
        "dxvk_custom": [e for e in dxvk if e.partition("=")[0] not in _DXVK_MANAGED],
        **toggles,
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
    frame = merged.get("frame_rate")
    frame = frame if isinstance(frame, int) and frame > 0 else 0
    extra = [tok for toggle in TOGGLES if windows and flag(toggle.key) for tok in toggle.tokens]
    # One DXVK_CONFIG: the user's own entries (always from `opts`), then the cap and DXVK toggles.
    dxvk = list(parse_presets(opts)["dxvk_custom"])
    if windows:
        if frame:
            dxvk += [f"{key}={frame}" for key in _DXVK_FRAME_KEYS]
        dxvk += [toggle.dxvk for toggle in TOGGLES if toggle.dxvk and flag(toggle.key)]
    if dxvk:
        extra.append("DXVK_CONFIG=" + ";".join(dxvk))
    if windows and frame:
        extra.append(f"VKD3D_FRAME_RATE={frame}")
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
        mangohud_fps_limit=frame or None,  # one cap: MangoHud also covers Vulkan/OpenGL games
        prime_vendor=prime_vendor(),
        debug_log=flag("debug_log"),
        extra_envs=extra,
        managed_tokens=TOGGLE_TOKENS,
        managed_keys=_MANAGED_KEYS,
    )


_OBSOLETE = ("PROTON_ENABLE_NVAPI=1", "DXVK_ENABLE_NVAPI=1", "DXVK_FRAME_RATE=")


def migrate_launch_options(opts: str) -> str:
    """Rewrite options saved by older versions: drop the NVAPI flags (on by default now) and move
    the frame cap from DXVK_FRAME_RATE (no longer read) to DXVK_CONFIG. Anything else is kept."""
    if not any(old in opts for old in _OBSOLETE):
        return opts
    try:
        return apply_presets(opts, nvapi=False)
    except ValueError:
        return opts


def prime_vendor() -> str:
    """How to send a game to the other GPU here: "nvidia" (PRIME offload), or "mesa" (DRI_PRIME)
    on a hybrid machine without NVIDIA (AMD + AMD, Intel + AMD, Intel + Intel Arc)."""
    from .runtime_manager import detect_gpu_profile

    gpu = detect_gpu_profile()
    return "mesa" if gpu.is_hybrid and not gpu.has_nvidia else "nvidia"


def active_flags(opts: str) -> list[str]:
    """Names (from FLAGS) of the presets turned on in `opts`, in display order."""
    state = parse_presets(opts)
    return [key for key in FLAGS if state.get(key)]
