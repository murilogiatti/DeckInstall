"""Queued, coalesced batch VDF editor for Steam shortcuts and compat mappings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .steam_paths import SteamPaths
from .steam_vdf import (
    _backup_file,
    _steam_transaction_lock,
    add_or_update_shortcut,
    bulk_migrate_compat_tool,
    remove_compat_tools,
    remove_shortcut,
    set_shortcut_proton,
    update_shortcut_launch_options,
)

VdfEditOp = Literal[
    "set_proton",
    "set_launch_options",
    "bulk_migrate_compat",
    "remove_compat",
    "remove_shortcut",
    "add_or_update_shortcut",
]

_COALESCIBLE_OPS = frozenset({"set_proton", "set_launch_options"})


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
    """In-memory queue of Steam VDF edits with coalescing and single-transaction apply."""

    def __init__(self) -> None:
        self._queue: list[QueuedVdfEdit] = []

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

    def remove(self, edit_id: str) -> bool:
        """Remove a queued edit by edit_id. Returns True if an entry was removed."""
        before = len(self._queue)
        self._queue = [edit for edit in self._queue if edit.edit_id != edit_id]
        return len(self._queue) < before

    def clear(self) -> None:
        """Discard all pending edits."""
        self._queue.clear()

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

        with _steam_transaction_lock(paths.shortcuts_vdf):
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
        return BatchApplyResult(applied=applied, errors=errors)

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
            remove_compat_tools(paths.config_vdf, appids)
        elif edit.op == "remove_shortcut":
            if not remove_shortcut(paths, edit.appid_u32):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
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
