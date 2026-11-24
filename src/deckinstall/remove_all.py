""""Remove everything DeckInstall added" (before deleting the AppImage).

Games, prefixes and saves are never touched here: each game has its own guided uninstall.
What goes: the runtimes DeckInstall downloaded, its settings, caches and logs, and the changes
it recorded in system-changes.json that can be undone without root (Flatpak Steam access).
Changes that need root are listed with the command that undoes them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import remove_path_or_tree

RUNTIMES = "runtimes"
APP_DATA = "app_data"
MIGRATION_BACKUPS = "migration_backups"
SYSTEM_CHANGES = "system_changes"


@dataclass
class RemovalPart:
    key: str
    paths: list[Path] = field(default_factory=list)
    default_on: bool = True


@dataclass
class RemovalResult:
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    root_commands: list[str] = field(default_factory=list)


def _xdg(var: str, fallback: Path) -> Path:
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser() if raw else fallback


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / "DeckInstall"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "DeckInstall"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / "DeckInstall"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / "DeckInstall"


# What DeckInstall itself writes in its data folder. Anything else there (a backup someone put
# there by hand, files of a newer version) is left alone and listed by unknown_data_entries().
_OWN_DATA_ENTRIES = (
    "logs",
    "runtime",
    "libraries.json",
    "vdf_queue.json",
    "vdf_queue.lock",
    "cleanup.lock",
    "restore.lock",
    "deckinstall.log",
    "deckinstall.log.1",
    "deckinstall.log.2",
    "deckinstall-faults.log",
)
_LEDGER_ENTRIES = ("system-changes.json", "system-changes.json.lock")


def unknown_data_entries() -> list[Path]:
    """Files in DeckInstall's data folder that "Remove everything" keeps because it did not write them."""
    data = data_dir()
    known = {*_OWN_DATA_ENTRIES, *_LEDGER_ENTRIES, "migrations"}
    try:
        return sorted(p for p in data.iterdir() if p.name not in known)
    except OSError:
        return []


def plan() -> list[RemovalPart]:
    """What "Remove everything" would remove, by part (only parts that have something)."""
    from .runtime_uninstall import list_runtimes
    from .system_changes import list_changes

    parts: list[RemovalPart] = []
    runtimes = [item.path for item in list_runtimes()]
    if runtimes:
        parts.append(RemovalPart(RUNTIMES, runtimes))
    data = data_dir()
    app_paths = [data / name for name in _OWN_DATA_ENTRIES if (data / name).exists() or (data / name).is_symlink()]
    app_paths += [p for p in (config_dir(), cache_dir(), state_dir()) if p.exists()]
    if app_paths:
        parts.append(RemovalPart(APP_DATA, app_paths))
    migrations = data / "migrations"
    if migrations.is_dir():
        parts.append(RemovalPart(MIGRATION_BACKUPS, [migrations], default_on=False))
    if list_changes():
        parts.append(RemovalPart(SYSTEM_CHANGES, []))
    return parts


def execute(parts: list[RemovalPart]) -> RemovalResult:
    """Remove the chosen parts. Runtimes first (they read app data), the ledger last."""
    from .system_changes import KIND_FLATPAK_ACCESS, forget_change, ledger_path, list_changes

    result = RemovalResult()
    keys = {p.key for p in parts}
    if RUNTIMES in keys:
        from .runtime_uninstall import list_runtimes, uninstall

        removed, errors = uninstall(list_runtimes())
        result.removed += removed
        result.errors += errors
    if SYSTEM_CHANGES in keys:
        for change in list_changes():
            kind, target = str(change.get("kind")), str(change.get("target"))
            if kind == KIND_FLATPAK_ACCESS:
                from .steam_sandbox import revoke_flatpak_access

                ok, out = revoke_flatpak_access([Path(target)])
                if ok:
                    forget_change(kind, target)
                    result.removed.append(f"Flatpak Steam: {target}")
                else:
                    result.errors.append(f"Flatpak Steam {target}: {out}")
            elif change.get("needs_root"):
                result.root_commands.append(str(change.get("undo")))
    for part in parts:
        if part.key not in (APP_DATA, MIGRATION_BACKUPS):
            continue
        for path in part.paths:
            try:
                if path.is_symlink():
                    path.unlink()
                else:
                    remove_path_or_tree(path)
                result.removed.append(str(path))
            except OSError as exc:
                result.errors.append(f"{path}: {exc}")
    data = data_dir()
    if APP_DATA in keys and not result.root_commands and not result.errors:
        ledger = ledger_path()
        for leftover in (ledger, ledger.with_name(_LEDGER_ENTRIES[1])):
            leftover.unlink(missing_ok=True)
    try:
        if data.is_dir() and not any(data.iterdir()):
            data.rmdir()
    except OSError:
        pass
    logging.info("Remove everything: %d removed, %d errors", len(result.removed), len(result.errors))
    return result


def running_appimage_hint() -> str:
    """The AppImage file to delete last, when running from one."""
    from .app_updater import get_running_appimage

    appimage = get_running_appimage()
    return str(appimage) if appimage is not None else ""

