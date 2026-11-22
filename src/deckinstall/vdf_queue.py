"""Queued, coalesced batch VDF editor for Steam shortcuts and compat mappings."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

from .fsutil import atomic_write_text
from .steam_paths import SteamPaths, list_steam_users
from .steam_vdf import (
    _backup_file,
    _steam_transaction_lock,
    add_or_update_shortcut,
    bulk_migrate_compat_tool,
    remove_orphan_shortcut_mappings,
    remove_shortcut,
    set_shortcut_proton,
    set_steam_app_launch_options,
    update_shortcut_launch_options,
    update_shortcut_paths,
)

VdfEditOp = Literal[
    "set_proton",
    "set_launch_options",
    "set_app_launch_options",
    "bulk_migrate_compat",
    "remove_compat",
    "remove_shortcut",
    "add_or_update_shortcut",
    "update_shortcut_paths",
    "add_installed_game",
]

_COALESCIBLE_OPS = frozenset({"set_proton", "set_launch_options", "set_app_launch_options"})
_KNOWN_OPS = frozenset(get_args(VdfEditOp))
QUEUE_FILE_VERSION = 1


def default_queue_file() -> Path:
    """Where Gaming Mode edits wait for Steam to close (survives restarts of the app)."""
    return Path.home() / ".local" / "share" / "DeckInstall" / "vdf_queue.json"


@dataclass(frozen=True)
class QueuedVdfEdit:
    """A single pending VDF mutation queued for batch application."""

    edit_id: str
    op: VdfEditOp
    appid_u32: int
    label: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class BatchApplyResult:
    """Summary of applying all queued VDF edits."""

    applied: int
    errors: list[str]


class VdfEditQueue:
    """Queue of Steam VDF edits with coalescing and single-transaction apply.

    With ``store_path`` the queue is loaded from and saved to that JSON file after every change,
    so edits made in Gaming Mode survive closing the app until Steam is closed.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._queue: list[QueuedVdfEdit] = []
        self._store_path = store_path
        if store_path is not None:
            self._queue = _load_edits(store_path)

    def _save(self) -> None:
        if self._store_path is None:
            return
        try:
            if not self._queue:
                self._store_path.unlink(missing_ok=True)
                return
            data = {"version": QUEUE_FILE_VERSION, "edits": [asdict(e) for e in self._queue]}
            atomic_write_text(self._store_path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
        except OSError as exc:
            logging.warning("Could not save the Steam edit queue to %s: %s", self._store_path, exc)

    def reload(self) -> None:
        """Re-read the queue file (another DeckInstall instance may have changed it)."""
        if self._store_path is not None:
            self._queue = _load_edits(self._store_path)

    def enqueue(self, edit: QueuedVdfEdit) -> None:
        """Add an edit to the queue, coalescing set_proton/set_launch_options by (op, appid_u32)."""
        if edit.op in _COALESCIBLE_OPS:
            self._queue = [
                existing
                for existing in self._queue
                if not (existing.op == edit.op and existing.appid_u32 == edit.appid_u32)
            ]
        else:
            self._queue = [
                existing for existing in self._queue if existing.edit_id != edit.edit_id
            ]
        self._queue.append(edit)
        self._save()

    def remove(self, edit_id: str) -> bool:
        """Remove a queued edit by edit_id. Returns True if an entry was removed."""
        before = len(self._queue)
        self._queue = [edit for edit in self._queue if edit.edit_id != edit_id]
        removed = len(self._queue) < before
        if removed:
            self._save()
        return removed

    def clear(self) -> None:
        """Discard all pending edits."""
        self._queue.clear()
        self._save()

    def pending_count(self) -> int:
        """Return the number of queued edits."""
        return len(self._queue)

    def list_pending(self) -> list[QueuedVdfEdit]:
        """Return a shallow copy of pending edits in queue order."""
        return list(self._queue)

    def apply_all(self, paths: SteamPaths) -> BatchApplyResult:
        """Apply all queued edits under a single transaction lock and single backup pass."""
        if not self._queue:
            return BatchApplyResult(applied=0, errors=[])

        applied = 0
        errors: list[str] = []
        remaining: list[QueuedVdfEdit] = []

        anchors = {paths.shortcuts_vdf, paths.config_vdf}
        if any(e.op == "set_app_launch_options" for e in self._queue):
            anchors.add(paths.localconfig_vdf)
        for edit in self._queue:
            if edit.op in ("update_shortcut_paths", "add_installed_game"):
                try:
                    anchors.add(self._account_paths(paths, edit.payload).shortcuts_vdf)
                except (KeyError, OSError, ValueError):
                    pass  # Dispatch reports invalid edits and keeps them in the queue.
        with ExitStack() as locks:
            for anchor in sorted(anchors):
                locks.enter_context(_steam_transaction_lock(anchor))
            _backup_file(paths.shortcuts_vdf)
            _backup_file(paths.config_vdf)

            for edit in self._queue:
                try:
                    self._dispatch_edit(paths, edit)
                    applied += 1
                except Exception as exc:
                    errors.append(f"{edit.label}: {exc}")
                    remaining.append(edit)

        self._queue = remaining
        self._save()
        return BatchApplyResult(applied=applied, errors=errors)

    @staticmethod
    def _account_paths(paths: SteamPaths, payload: dict[str, Any]) -> SteamPaths:
        account = str(payload["user_id"])
        if Path(payload["steam_root"]).resolve() != paths.steam_root.resolve():
            raise ValueError("The shortcut belongs to another Steam installation")
        if account == paths.user_id:
            return paths
        if account not in list_steam_users(paths.steam_root):
            raise ValueError("The Steam account for this shortcut is unavailable")
        userdata = paths.steam_root / "userdata" / account
        return replace(paths, user_id=account, userdata_dir=userdata,
                       shortcuts_vdf=userdata / "config" / "shortcuts.vdf", grid_dir=userdata / "config" / "grid")

    @staticmethod
    def _dispatch_edit(paths: SteamPaths, edit: QueuedVdfEdit) -> None:
        payload = edit.payload
        if edit.op == "set_proton":
            tool_name = str(payload.get("tool_name") or payload.get("proton_name") or "")
            set_shortcut_proton(paths.config_vdf, edit.appid_u32, tool_name)
        elif edit.op == "set_launch_options":
            launch_options = str(payload.get("launch_options", ""))
            if not update_shortcut_launch_options(paths, edit.appid_u32, launch_options):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "set_app_launch_options":
            launch_options = str(payload.get("launch_options", ""))
            if not set_steam_app_launch_options(paths, edit.appid_u32, launch_options):
                raise RuntimeError(f"localconfig.vdf ausente para o jogo {edit.appid_u32}")
        elif edit.op == "bulk_migrate_compat":
            raw_appids = payload.get("appids")
            appids = (
                [int(a) for a in raw_appids]
                if raw_appids is not None
                else ([edit.appid_u32] if edit.appid_u32 else [])
            )
            target_tool = str(payload.get("target_tool") or payload.get("tool_name") or "")
            bulk_migrate_compat_tool(paths.config_vdf, appids, target_tool)
        elif edit.op == "remove_compat":
            raw_appids = payload.get("appids")
            appids = (
                [int(a) for a in raw_appids]
                if raw_appids is not None
                else ([edit.appid_u32] if edit.appid_u32 else [])
            )
            remove_orphan_shortcut_mappings(paths, appids)
        elif edit.op == "remove_shortcut":
            if not remove_shortcut(paths, edit.appid_u32):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "update_shortcut_paths":
            paths = VdfEditQueue._account_paths(paths, payload)
            if not update_shortcut_paths(
                paths,
                edit.appid_u32,
                Path(payload["exe_path"]),
                Path(payload["start_dir"]),
                expected_exe=payload["expected_exe"],
                allow_existing_old=bool(payload.get("allow_existing_old", False)),
            ):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "add_installed_game":
            from .standalone import add_installed_game_to_steam

            paths = VdfEditQueue._account_paths(paths, payload)
            add_installed_game_to_steam(paths, Path(payload["game_dir"]), payload["uid"])
        elif edit.op == "add_or_update_shortcut":
            app_name = str(payload.get("app_name") or edit.label)
            exe_path = Path(payload["exe_path"])
            raw_start = payload.get("start_dir")
            start_dir = Path(raw_start) if raw_start else None
            raw_icon = payload.get("icon_path")
            icon_path = Path(raw_icon) if raw_icon else None
            raw_launch = payload.get("launch_options")
            launch_options = str(raw_launch) if raw_launch is not None else ""
            enable_proton = bool(payload.get("enable_proton", True))
            raw_preserve = payload.get("preserve_from_appid")
            preserve_from_appid = int(raw_preserve) if raw_preserve is not None else None
            add_or_update_shortcut(
                paths,
                app_name=app_name,
                exe_path=exe_path,
                start_dir=start_dir,
                icon_path=icon_path,
                launch_options=launch_options,
                enable_proton=enable_proton,
                preserve_from_appid=preserve_from_appid,
            )
        else:
            raise ValueError(f"Operação VDF desconhecida: {edit.op}")


def _load_edits(path: Path) -> list[QueuedVdfEdit]:
    """Read a saved queue; unreadable files or entries are skipped (and logged), never fatal."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logging.warning("Ignoring unreadable Steam edit queue %s: %s", path, exc)
        return []
    if not isinstance(raw, dict) or raw.get("version") != QUEUE_FILE_VERSION:
        logging.warning("Ignoring Steam edit queue %s with unknown version", path)
        return []
    edits: list[QueuedVdfEdit] = []
    for item in raw.get("edits") or []:
        try:
            op = str(item["op"])
            payload = item.get("payload") or {}
            if op not in _KNOWN_OPS or not isinstance(payload, dict):
                raise ValueError(op)
            edits.append(
                QueuedVdfEdit(
                    edit_id=str(item["edit_id"]),
                    op=cast(VdfEditOp, op),
                    appid_u32=int(item["appid_u32"]),
                    label=str(item.get("label") or ""),
                    payload=payload,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logging.warning("Skipping invalid queued Steam edit %r: %s", item, exc)
    return edits
