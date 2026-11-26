"""Can Flatpak Steam see the game folders its shortcuts point at?

Flatpak Steam only sees its own data folder unless the user grants more with
`flatpak override --user --filesystem=<dir> com.valvesoftware.Steam` (no root needed; DeckInstall
asks first and records it).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from .fsutil import is_within
from .steam_paths import STEAM_FLATPAK, steam_variant

FLATPAK_APP_ID = "com.valvesoftware.Steam"


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.debug("%s failed: %s", cmd[0], exc)
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _filesystems_from_keyfile(text: str) -> list[str]:
    out: list[str] = []
    in_context = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_context = line == "[Context]"
            continue
        if in_context and line.startswith("filesystems="):
            out.extend(part for part in line.split("=", 1)[1].split(";") if part)
    return out


def flatpak_filesystems(home: Path | None = None) -> list[str]:
    """Filesystem permissions Flatpak Steam has now (app metadata + system and user overrides)."""
    base = home if home is not None else Path.home()
    entries: list[str] = []
    if shutil.which("flatpak"):
        entries += _filesystems_from_keyfile(_run(["flatpak", "info", "--show-permissions", FLATPAK_APP_ID]))
    for override in (
        Path("/var/lib/flatpak/overrides") / FLATPAK_APP_ID,
        base / ".local/share/flatpak/overrides" / FLATPAK_APP_ID,
    ):
        try:
            entries += _filesystems_from_keyfile(override.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return entries


def flatpak_can_see(path: Path, filesystems: Iterable[str], home: Path | None = None) -> bool:
    """True when one of the Flatpak filesystem entries covers path (read access is enough)."""
    base = (home if home is not None else Path.home()).resolve()
    granted: set[Path] = set()
    denied: set[Path] = set()
    for raw in filesystems:
        entry = raw.split(":", 1)[0].strip()
        negate = entry.startswith("!")
        entry = entry.lstrip("!")
        if entry in ("host", "host-os", "host-etc"):
            target = Path("/") if entry == "host" else None
        elif entry == "home":
            target = base
        elif entry.startswith("~/"):
            target = base / entry[2:]
        elif entry.startswith("/"):
            target = Path(entry)
        else:
            target = None  # xdg-* names: never a game library
        if target is None:
            continue
        (denied if negate else granted).add(target)
    granted -= denied
    return any(is_within(path, root) for root in granted)


def folders_steam_cannot_see(steam_root: Path, folders: Iterable[Path]) -> list[Path]:
    """The folders (game library roots) Flatpak Steam cannot open. Empty for native Steam."""
    if steam_variant(steam_root) != STEAM_FLATPAK:
        return []
    unique: list[Path] = []
    for folder in folders:
        try:
            resolved = folder.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    if not unique:
        return []
    fs = flatpak_filesystems()
    return [p for p in unique if not flatpak_can_see(p, fs)]


def shortcut_folders(exe_values: Iterable[str]) -> list[Path]:
    """Folder to grant for each shortcut target: its DeckInstall library, else the game folder."""
    from .library import find_library_root

    out: list[Path] = []
    for raw in exe_values:
        text = raw.strip().strip('"')
        if not text.startswith("/"):
            continue
        exe = Path(text)
        folder = find_library_root(exe) or exe.parent
        if folder not in out:
            out.append(folder)
    return out


def grant_flatpak_access(folders: Iterable[Path]) -> tuple[bool, str]:
    """Let Flatpak Steam read and write folders (user override; undone by revoke_flatpak_access)."""
    args = [f"--filesystem={p}" for p in folders]
    if not args:
        return True, ""
    cmd = ["flatpak", "override", "--user", *args, FLATPAK_APP_ID]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def revoke_flatpak_access(folders: Iterable[Path]) -> tuple[bool, str]:
    """Undo grant_flatpak_access for folders."""
    args = [f"--nofilesystem={p}" for p in folders]
    if not args:
        return True, ""
    cmd = ["flatpak", "override", "--user", *args, FLATPAK_APP_ID]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()

