"""Saves of games DeckInstall does not manage: Steam store games and the Xodus Nexus launcher.

Read only: this module finds where the saves are, the snapshot/restore code in save_sync does the rest.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, NamedTuple

import vdf

from .save_sync import ExtraDirs, _collect_prefix_save_entries
from .steam_paths import SteamPaths, read_library_paths

# Proton, Steam Linux Runtime and redistributables have a compatdata folder but no saves.
_STEAM_TOOL_APPIDS = frozenset({228980, 1070560, 1391110, 1628350, 1493710, 2180100, 1826330, 2348590, 2805730})
_STEAM_TOOL_NAME = re.compile(
    r"^(proton (\d|experimental|hotfix|next|easyanticheat|battleye)|steam linux runtime|steamworks common)",
    re.IGNORECASE,
)

NEXUS_PROCESS_NAMES = frozenset({"xodus-nexus"})
_NEXUS_GAME_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NEXUS_FAMILY = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NEXUS_BUILD = re.compile(r"^[a-f0-9]{64}$")


def default_home() -> Path:
    """Home folder searched for other launchers (a seam for tests)."""
    return Path.home()


class ExternalSaves(NamedTuple):
    """Where one game outside the DeckInstall library keeps its saves."""

    name: str
    source: str  # "steam" | "nexus"
    prefix_dir: Path  # may not exist (a native Steam game with only cloud saves)
    extra_dirs: ExtraDirs
    steam_appid: int | None
    game_dir: Path | None


def _steam_libraries(steam_root: Path) -> list[Path]:
    out: list[Path] = []
    for sa in (steam_root / "steamapps", *(lib / "steamapps" for lib in read_library_paths(steam_root))):
        try:
            key = sa.resolve()
        except OSError:
            continue
        if sa.is_dir() and key not in {p.resolve() for p in out}:
            out.append(sa)
    return out


def steam_save_sources(paths: SteamPaths) -> list[ExternalSaves]:
    """Installed Steam store games with a Proton prefix or Steam's local cloud folder."""
    out: list[ExternalSaves] = []
    seen: set[int] = set()
    for sa in _steam_libraries(paths.steam_root):
        for acf in sorted(sa.glob("appmanifest_*.acf")):
            stem = acf.stem.removeprefix("appmanifest_")
            if not (stem.isascii() and stem.isdigit()):
                continue
            appid = int(stem)
            if appid in seen or appid in _STEAM_TOOL_APPIDS:
                continue
            try:
                state = vdf.loads(acf.read_text(encoding="utf-8", errors="replace")).get("AppState") or {}
            except Exception:
                continue
            name = str(state.get("name") or "").strip() if isinstance(state, dict) else ""
            if not name or _STEAM_TOOL_NAME.match(name):
                continue
            prefix = next(
                (c / str(appid) / "pfx" for c in (sa / "compatdata", paths.compatdata_dir)
                 if (c / str(appid) / "pfx").is_dir()),
                sa / "compatdata" / str(appid) / "pfx",
            )
            cloud = paths.userdata_dir / str(appid) / "remote"
            extra: ExtraDirs = (("steamcloud", cloud),) if cloud.is_dir() and not cloud.is_symlink() else ()
            if not prefix.is_dir() and not extra:
                continue
            installdir = str(state.get("installdir") or "")
            game_dir = sa / "common" / installdir if installdir else None
            seen.add(appid)
            out.append(ExternalSaves(name, "steam", prefix, extra, appid, game_dir))
    return out


def _nexus_prefix(data_root: Path, game_id: str, identity: object) -> Path | None:
    """The game's Wine prefix, following Nexus's runtime-prefix-binding layouts (game / family / build)."""

    def usable(p: Path) -> bool:
        return (p / "drive_c").is_dir() and not p.is_symlink()

    def from_root(root: Path) -> Path | None:
        for cand in (root / "compatdata" / "pfx", root / "compatdata", root / "wine"):
            if usable(cand):
                return cand
        return None

    canonical: list[Path] = [
        data_root / "compatdata" / game_id / "pfx",
        data_root / "compatdata" / game_id,
        data_root / "prefixes" / game_id,
    ]
    if isinstance(identity, dict):
        layout, family = identity.get("layout"), str(identity.get("family") or "")
        origin = identity.get("origin") if isinstance(identity.get("origin"), dict) else {}
        build = str(origin.get("buildId") or "") if isinstance(origin, dict) else ""
        if layout == "family" and _NEXUS_FAMILY.match(family):
            found = from_root(data_root / "runtime-prefixes" / family / "prefixes" / game_id)
            if found is not None:
                return found
        elif layout == "build" and _NEXUS_FAMILY.match(family) and _NEXUS_BUILD.match(build):
            found = from_root(data_root / "runtime-prefixes" / family / build / game_id)
            if found is not None:
                return found
    for cand in canonical:
        if usable(cand):
            return cand
    # No binding recorded: accept an experimental prefix only when exactly one exists.
    found_exp = [
        p for p in (from_root(d / game_id) for d in (data_root / "runtime-prefixes").glob("*/*") if d.is_dir())
        if p is not None
    ]
    return found_exp[0] if len(found_exp) == 1 else None


def nexus_save_sources(home: Path | None = None) -> list[ExternalSaves]:
    """Games in Xodus Nexus (Microsoft Store / Game Pass), from ~/.config/xodus-nexus/launcher-state.json."""
    home = home or default_home()
    state_file = home / ".config" / "xodus-nexus" / "launcher-state.json"
    try:
        if state_file.is_symlink() or state_file.stat().st_size > 64 * 1024 * 1024:
            return []
        state: Any = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logging.warning("Could not read the Nexus launcher state %s: %s", state_file, exc)
        return []
    if not isinstance(state, dict):
        return []
    raw_settings = state.get("settings")
    settings: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    runtime_root = Path(str(settings.get("runtimeRoot") or home / ".local" / "share" / "xodus-nexus" / "runtimes"))
    data_root = runtime_root.expanduser().parent
    out: list[ExternalSaves] = []
    for game in state.get("games") or []:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("id") or "")
        title = str(game.get("title") or "").strip()
        if not _NEXUS_GAME_ID.match(game_id) or not title:
            continue
        prefix = _nexus_prefix(data_root, game_id, game.get("runtimePrefixIdentity"))
        if prefix is None:
            continue
        raw_path = str(game.get("path") or "")
        out.append(ExternalSaves(title, "nexus", prefix, (), None, Path(raw_path) if raw_path else None))
    return out


def is_nexus_running() -> bool:
    """True while the Nexus launcher runs (its Xbox cloud sync could fight a restore)."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
                if fh.read().strip() in NEXUS_PROCESS_NAMES:
                    return True
        except OSError:
            continue
    return False


def saves_size(prefix_dir: Path, extra_dirs: ExtraDirs = ()) -> tuple[int, int]:
    """(bytes, files) a snapshot would hold right now (caches and logs left out)."""
    total = files = 0
    for _section, src, _rel in _collect_prefix_save_entries(prefix_dir, extra_dirs=extra_dirs):
        try:
            total += src.stat().st_size
            files += 1
        except OSError:
            continue
    return total, files
