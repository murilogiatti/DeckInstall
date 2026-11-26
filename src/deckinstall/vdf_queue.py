"""Queued, coalesced batch VDF editor for Steam shortcuts and compat mappings."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

from .fsutil import atomic_write_text
from .steam_paths import SteamPaths, list_steam_users
from .steam_vdf import (
    _backup_file,
    _file_lock,
    _steam_transaction_lock,
    _vdf_batch_backup_scope,
    add_or_update_shortcut,
    bulk_migrate_compat_tool,
    load_shortcuts,
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


def _coalescing_key(edit: QueuedVdfEdit) -> tuple[str, int, str, str]:
    root = edit.payload.get("steam_root")
    steam_root = str(Path(root)) if isinstance(root, str) and root else ""
    user = str(edit.payload.get("user_id") or "") if edit.op != "set_proton" else ""
    return edit.op, edit.appid_u32, steam_root, user


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


class VdfQueueSaveError(OSError):
    """The queue file could not be written: the edit is NOT pending (nothing was announced)."""


@dataclass(frozen=True)
class QueueLoadProblem:
    """The saved queue could not be read as is. The original file is kept at ``preserved``."""

    kind: Literal["unreadable", "future_version", "invalid_entries"]
    preserved: Path | None
    kept: int
    dropped: int
    detail: str = ""


@dataclass(frozen=True)
class BatchApplyResult:
    """Summary of applying all queued VDF edits.

    ``applied`` counts edits whose effect is in Steam's files now (``already_done`` of them were
    found already in place, e.g. after an interrupted earlier run, and were not written again).
    ``confirm_error`` is set when Steam's files were changed but the queue file could not be
    updated: those edits are recognised as done on the next run instead of being repeated.
    ``steam_running`` means nothing was written because Steam was running when the write began.
    """

    applied: int
    errors: list[str]
    removed_for_review: tuple[tuple[int, str], ...] = ()
    already_done: int = 0
    confirm_error: str = ""
    steam_running: bool = False


class VdfEditQueue:
    """Queue of Steam VDF edits with coalescing and single-transaction apply.

    With ``store_path`` the queue is loaded from and saved to that JSON file after every change,
    so edits made in Gaming Mode survive closing the app until Steam is closed.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self._queue: list[QueuedVdfEdit] = []
        self._store_path = store_path
        self.load_problem: QueueLoadProblem | None = None
        if store_path is not None:
            with self._store_lock():
                self._queue = self._load()

    def _load(self) -> list[QueuedVdfEdit]:
        """Read the queue file; a bad file is preserved aside and reported (never fatal)."""
        assert self._store_path is not None
        edits, problem = load_queue_file(self._store_path)
        if problem is not None:
            self.load_problem = problem
        return edits

    def _store_lock(self):
        if self._store_path is None:
            return nullcontext()
        return _file_lock(self._store_path.with_suffix(".lock"))

    def _save(self) -> None:
        """Persist the queue. Raises VdfQueueSaveError when the file cannot be written."""
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
            raise VdfQueueSaveError(exc.errno or 0, f"Could not save the Steam edit queue: {exc}") from exc

    def reload(self) -> None:
        """Re-read the queue file (another DeckInstall instance may have changed it)."""
        if self._store_path is not None:
            with self._lock, self._store_lock():
                self._queue = self._load()

    def enqueue(self, edit: QueuedVdfEdit) -> None:
        """Add an edit to the queue, coalescing set_proton/set_launch_options by (op, appid_u32).

        Raises VdfQueueSaveError when the queue file cannot be written; the edit is then not
        pending and the in-memory queue is left as it was on disk.
        """
        with self._lock, self._store_lock():
            if self._store_path is not None:
                self._queue = self._load()
            before = list(self._queue)
            if edit.op in _COALESCIBLE_OPS:
                key = _coalescing_key(edit)
                self._queue = [
                    existing for existing in self._queue if _coalescing_key(existing) != key
                ]
            else:
                self._queue = [
                    existing for existing in self._queue if existing.edit_id != edit.edit_id
                ]
            self._queue.append(edit)
            try:
                self._save()
            except VdfQueueSaveError:
                self._queue = before
                raise

    def remove(self, edit_id: str) -> bool:
        """Remove a queued edit by edit_id. Returns True if an entry was removed.

        Raises VdfQueueSaveError when the removal could not be saved (the edit stays pending).
        """
        with self._lock, self._store_lock():
            if self._store_path is not None:
                self._queue = self._load()
            before = list(self._queue)
            self._queue = [edit for edit in self._queue if edit.edit_id != edit_id]
            removed = len(self._queue) < len(before)
            if removed:
                try:
                    self._save()
                except VdfQueueSaveError:
                    self._queue = before
                    raise
            return removed

    def clear(self) -> None:
        """Discard all pending edits. Raises VdfQueueSaveError when the file cannot be removed."""
        with self._lock, self._store_lock():
            before = list(self._queue)
            self._queue.clear()
            try:
                self._save()
            except VdfQueueSaveError:
                self._queue = before
                raise

    def pending_count(self) -> int:
        """Return the number of queued edits."""
        with self._lock:
            return len(self._queue)

    def list_pending(self) -> list[QueuedVdfEdit]:
        """Return a shallow copy of pending edits in queue order."""
        with self._lock:
            return list(self._queue)

    def apply_all(
        self, paths: SteamPaths, *, steam_running: Callable[[], bool] | None = None
    ) -> BatchApplyResult:
        """Apply all queued edits under a single transaction lock and single backup pass.

        ``steam_running`` is checked again once the locks are held, right before the first
        write: when it returns True nothing is written and ``steam_running`` is set in the result.
        """
        apply_lock = (
            _file_lock(self._store_path.with_suffix(".apply.lock"))
            if self._store_path is not None
            else self._apply_lock
        )
        with apply_lock:
            return self._apply_batch(paths, steam_running)

    def _apply_batch(self, paths: SteamPaths, steam_running: Callable[[], bool] | None = None) -> BatchApplyResult:
        self.reload()
        with self._lock:
            batch = list(self._queue)
        if not batch:
            return BatchApplyResult(applied=0, errors=[])

        applied = 0
        already_done = 0
        errors: list[str] = []
        succeeded: list[QueuedVdfEdit] = []

        anchors = {paths.shortcuts_vdf, paths.config_vdf}
        if any(e.op == "set_app_launch_options" for e in batch):
            anchors.add(paths.localconfig_vdf)
        for edit in batch:
            if "user_id" in edit.payload or "steam_root" in edit.payload:
                try:
                    anchors.add(self._resolve_edit_paths(paths, edit.payload).shortcuts_vdf)
                except (KeyError, OSError, ValueError):
                    pass  # Dispatch reports invalid edits and keeps them in the queue.
        with ExitStack() as locks:
            for anchor in sorted(anchors):
                locks.enter_context(_steam_transaction_lock(anchor))
            if steam_running is not None and steam_running():
                return BatchApplyResult(applied=0, errors=[], steam_running=True)
            locks.enter_context(_vdf_batch_backup_scope())
            _backup_file(paths.shortcuts_vdf)
            _backup_file(paths.config_vdf)
            if any(e.op == "set_app_launch_options" for e in batch) and getattr(paths, "localconfig_vdf", None):
                _backup_file(paths.localconfig_vdf)

            for edit in batch:
                try:
                    if self._dispatch_edit(paths, edit) is ALREADY_DONE:
                        already_done += 1
                    applied += 1
                    succeeded.append(edit)
                except Exception as exc:
                    errors.append(f"{edit.label}: {exc}")

        confirm_error = ""
        with self._lock, self._store_lock():
            if self._store_path is not None:
                self._queue = self._load()
            remaining: list[QueuedVdfEdit] = []
            for current in self._queue:
                if any(
                    done.edit_id == current.edit_id
                    and done.op == current.op
                    and done.appid_u32 == current.appid_u32
                    and done.payload == current.payload
                    for done in succeeded
                ):
                    continue
                remaining.append(current)
            self._queue = remaining
            try:
                self._save()
            except VdfQueueSaveError as exc:
                # Steam's files already have the change; the next run finds it in place and only
                # confirms it (see _dispatch_edit), so nothing is written twice.
                confirm_error = str(exc)
                if self._store_path is not None:
                    self._queue = self._load()
        return BatchApplyResult(
            applied=applied,
            errors=errors,
            already_done=already_done,
            confirm_error=confirm_error,
            removed_for_review=tuple(
                (edit.appid_u32, edit.label) for edit in succeeded
                if edit.op == "remove_shortcut" and edit.payload.get("review_cleanup") is True
            ),
        )

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
    def _resolve_edit_paths(paths: SteamPaths, payload: dict[str, Any]) -> SteamPaths:
        raw_root = payload.get("steam_root")
        if raw_root:
            if Path(str(raw_root)).resolve() != paths.steam_root.resolve():
                raise ValueError("The shortcut belongs to another Steam installation")
        raw_user = payload.get("user_id")
        if not raw_user:
            return paths
        account = str(raw_user)
        if account == paths.user_id:
            return paths
        if account not in list_steam_users(paths.steam_root):
            raise ValueError("The Steam account for this shortcut is unavailable")
        userdata = paths.steam_root / "userdata" / account
        return replace(
            paths,
            user_id=account,
            userdata_dir=userdata,
            shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
            grid_dir=userdata / "config" / "grid",
        )

    @staticmethod
    def _dispatch_edit(paths: SteamPaths, edit: QueuedVdfEdit) -> object:
        """Apply one edit. Returns ALREADY_DONE when its effect was already in Steam's files."""
        payload = edit.payload
        if edit.op == "set_proton":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            tool_name = str(payload.get("tool_name") or payload.get("proton_name") or "")
            set_shortcut_proton(target_paths.config_vdf, edit.appid_u32, tool_name)
        elif edit.op == "set_launch_options":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            launch_options = str(payload.get("launch_options", ""))
            if not update_shortcut_launch_options(target_paths, edit.appid_u32, launch_options):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "set_app_launch_options":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            launch_options = str(payload.get("launch_options", ""))
            if not set_steam_app_launch_options(target_paths, edit.appid_u32, launch_options):
                raise RuntimeError(f"localconfig.vdf ausente para o jogo {edit.appid_u32}")
        elif edit.op == "bulk_migrate_compat":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            raw_appids = payload.get("appids")
            appids = (
                [int(a) for a in raw_appids]
                if raw_appids is not None
                else ([edit.appid_u32] if edit.appid_u32 else [])
            )
            target_tool = str(payload.get("target_tool") or payload.get("tool_name") or "")
            bulk_migrate_compat_tool(target_paths.config_vdf, appids, target_tool)
        elif edit.op == "remove_compat":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            raw_appids = payload.get("appids")
            appids = (
                [int(a) for a in raw_appids]
                if raw_appids is not None
                else ([edit.appid_u32] if edit.appid_u32 else [])
            )
            remove_orphan_shortcut_mappings(target_paths, appids)
        elif edit.op == "remove_shortcut":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            if not remove_shortcut(target_paths, edit.appid_u32):
                if _shortcut_absent(target_paths, edit.appid_u32):
                    return ALREADY_DONE  # removed by an earlier, interrupted run (or by the user)
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "update_shortcut_paths":
            target_paths = VdfEditQueue._account_paths(paths, payload)
            if not update_shortcut_paths(
                target_paths,
                edit.appid_u32,
                Path(payload["exe_path"]),
                Path(payload["start_dir"]),
                expected_exe=payload["expected_exe"],
                allow_existing_old=bool(payload.get("allow_existing_old", False)),
            ):
                raise RuntimeError(f"Atalho {edit.appid_u32} não encontrado em shortcuts.vdf")
        elif edit.op == "add_installed_game":
            from .standalone import add_installed_game_to_steam

            target_paths = VdfEditQueue._account_paths(paths, payload)
            add_installed_game_to_steam(target_paths, Path(payload["game_dir"]), payload["uid"])
        elif edit.op == "add_or_update_shortcut":
            target_paths = VdfEditQueue._resolve_edit_paths(paths, payload)
            app_name = str(payload.get("app_name") or edit.label)
            exe_path = Path(payload["exe_path"])
            raw_start = payload.get("start_dir")
            start_dir = Path(raw_start) if raw_start else None
            raw_icon = payload.get("icon_path")
            icon_path = Path(raw_icon) if raw_icon else None
            raw_launch = payload.get("launch_options")
            opt_launch: str | None = str(raw_launch) if raw_launch is not None else None
            enable_proton = bool(payload.get("enable_proton", True))
            raw_preserve = payload.get("preserve_from_appid")
            preserve_from_appid = int(raw_preserve) if raw_preserve is not None else None
            add_or_update_shortcut(
                target_paths,
                app_name=app_name,
                exe_path=exe_path,
                start_dir=start_dir,
                icon_path=icon_path,
                launch_options=opt_launch,
                enable_proton=enable_proton,
                preserve_from_appid=preserve_from_appid,
            )
        else:
            raise ValueError(f"Operação VDF desconhecida: {edit.op}")
        return None


ALREADY_DONE = object()


def _shortcut_absent(paths: SteamPaths, appid_u32: int) -> bool:
    """True when the account's shortcuts.vdf is readable (or absent) and has no such shortcut."""
    if paths.shortcuts_vdf.is_symlink():
        return False
    if not paths.shortcuts_vdf.exists():
        return paths.userdata_dir.is_dir()
    try:
        entries = load_shortcuts(paths.shortcuts_vdf, strict=True)
    except (OSError, ValueError):
        return False
    return all(e.appid_u32 != appid_u32 for e in entries)


def _preserve_bad_queue(path: Path, tag: str) -> Path | None:
    """Move a queue file that cannot be used aside (never deleted), next to the original."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(100):
        target = path.with_name(f"{path.name}.{tag}-{stamp}{'' if n == 0 else f'-{n}'}")
        if target.exists():
            continue
        try:
            os.replace(path, target)
        except OSError as exc:
            logging.warning("Could not set the bad Steam edit queue %s aside: %s", path, exc)
            return None
        return target
    return None


def _parse_edit(item: object) -> QueuedVdfEdit:
    if not isinstance(item, dict):
        raise TypeError(f"entry is {type(item).__name__}, not an object")
    op = str(item["op"])
    payload = item.get("payload") or {}
    if op not in _KNOWN_OPS:
        raise ValueError(f"unknown operation {op!r}")
    if not isinstance(payload, dict):
        raise TypeError("payload is not an object")
    edit_id = item["edit_id"]
    if not isinstance(edit_id, str) or not edit_id:
        raise ValueError("missing edit_id")
    appid = item["appid_u32"]
    if isinstance(appid, bool) or not isinstance(appid, int) or not (0 <= appid <= 0xFFFFFFFF):
        raise ValueError(f"bad appid_u32 {appid!r}")
    return QueuedVdfEdit(
        edit_id=edit_id, op=cast(VdfEditOp, op), appid_u32=appid,
        label=str(item.get("label") or ""), payload=payload,
    )


def load_queue_file(path: Path) -> tuple[list[QueuedVdfEdit], QueueLoadProblem | None]:
    """Read a saved queue. Never raises for bad content.

    An empty or missing file is an empty queue. An unreadable file, one written by a newer
    DeckInstall, or one with invalid entries is moved aside unchanged (``.invalid-*`` or
    ``.v<N>-*``) and reported; the valid entries of a partly invalid file are kept.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        logging.warning("Cannot read the Steam edit queue %s: %s", path, exc)
        return [], QueueLoadProblem("unreadable", None, 0, 0, str(exc))
    if not text.strip():
        return [], None
    try:
        raw = json.loads(text)
    except ValueError as exc:
        kept = _preserve_bad_queue(path, "invalid")
        logging.warning("Unreadable Steam edit queue %s set aside as %s: %s", path, kept, exc)
        return [], QueueLoadProblem("unreadable", kept, 0, 0, str(exc))
    version = raw.get("version") if isinstance(raw, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > QUEUE_FILE_VERSION:
        kept = _preserve_bad_queue(path, f"v{version}")
        logging.warning("Steam edit queue %s from a newer DeckInstall (v%s) set aside as %s", path, version, kept)
        return [], QueueLoadProblem("future_version", kept, 0, 0, f"version {version}")
    raw_edits = raw.get("edits") if isinstance(raw, dict) else None
    if version != QUEUE_FILE_VERSION or not isinstance(raw_edits, list):
        kept = _preserve_bad_queue(path, "invalid")
        logging.warning("Steam edit queue %s has an unknown layout; set aside as %s", path, kept)
        return [], QueueLoadProblem("unreadable", kept, 0, 0, "unknown layout")
    edits: list[QueuedVdfEdit] = []
    bad: list[str] = []
    for item in raw_edits:
        try:
            edits.append(_parse_edit(item))
        except (KeyError, TypeError, ValueError) as exc:
            bad.append(str(exc))
            logging.warning("Invalid queued Steam edit %r: %s", item, exc)
    if not bad:
        return edits, None
    kept = _preserve_bad_queue(path, "invalid")
    if kept is not None and edits:
        try:  # the valid entries stay pending; the original (with the bad ones) is kept aside
            data = {"version": QUEUE_FILE_VERSION, "edits": [asdict(e) for e in edits]}
            atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
        except OSError as exc:
            logging.warning("Could not rewrite the Steam edit queue %s: %s", path, exc)
    return edits, QueueLoadProblem("invalid_entries", kept, len(edits), len(bad), "; ".join(bad[:3]))


def _load_edits(path: Path) -> list[QueuedVdfEdit]:
    """Read a saved queue (bad files are preserved aside and logged), never fatal."""
    return load_queue_file(path)[0]
