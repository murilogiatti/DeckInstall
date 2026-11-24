"""Ledger of what DeckInstall changed outside its own folders (system-changes.json).

Every change to the system or to another app's settings is recorded here with how to undo it:
packages installed with pkexec, the AppArmor profile for bwrap, Flatpak Steam folder access.
"Remove everything" reads it to undo what can be undone without root and to list the rest.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text, file_lock

KIND_PACKAGE = "system_package"
KIND_APPARMOR = "apparmor_profile"
KIND_FLATPAK_ACCESS = "flatpak_filesystem"


def ledger_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "DeckInstall" / "system-changes.json"


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logging.warning("system-changes.json unreadable (%s); starting a new one", exc)
        return []
    items = data.get("changes") if isinstance(data, dict) else None
    return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []


def list_changes() -> list[dict[str, Any]]:
    """Recorded changes, oldest first."""
    return _read(ledger_path())


def record_change(kind: str, target: str, undo: str, *, needs_root: bool, detail: str = "") -> None:
    """Remember a change (one entry per kind+target; recording it again updates the time).

    undo: the command that reverts it, as the user would type it.
    """
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(path.name + ".lock")):
        changes = [c for c in _read(path) if not (c.get("kind") == kind and c.get("target") == target)]
        changes.append({
            "kind": kind,
            "target": target,
            "undo": undo,
            "needs_root": needs_root,
            "detail": detail,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        atomic_write_text(path, json.dumps({"version": 1, "changes": changes}, indent=2, ensure_ascii=False))


def forget_change(kind: str, target: str) -> None:
    """Drop an entry once its change was undone."""
    path = ledger_path()
    if not path.is_file():
        return
    with file_lock(path.with_name(path.name + ".lock")):
        changes = [c for c in _read(path) if not (c.get("kind") == kind and c.get("target") == target)]
        atomic_write_text(path, json.dumps({"version": 1, "changes": changes}, indent=2, ensure_ascii=False))
