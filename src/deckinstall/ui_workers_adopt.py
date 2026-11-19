"""Background QThread workers for the adoption wizard (adopt.py)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .adopt import (
    AdoptionCandidate,
    AdoptionError,
    AdoptionPlan,
    AdoptOptions,
    SnapshotFn,
    adopt,
    find_adoption_candidates,
    plan_adoption,
)
from .steam_paths import SteamPaths
from .ui_cards import _format_worker_error


class AdoptScanWorker(QThread):
    """find_adoption_candidates() off the UI thread. ready(req_id, list[AdoptionCandidate])."""

    ready = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, req_id: int, paths: SteamPaths | None) -> None:
        super().__init__()
        self._req_id = req_id
        self._paths = paths

    def run(self) -> None:
        try:
            self.ready.emit(self._req_id, find_adoption_candidates(paths=self._paths))
        except Exception as exc:
            self.failed.emit(self._req_id, _format_worker_error(exc))


class AdoptPlanWorker(QThread):
    """plan_adoption() (walks folders for sizes). ready(req_id, AdoptionPlan)."""

    ready = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        req_id: int,
        candidate: AdoptionCandidate,
        library_root: Path | None,
        options: AdoptOptions,
    ) -> None:
        super().__init__()
        self._req_id = req_id
        self._candidate = candidate
        self._library_root = library_root
        self._options = options

    def run(self) -> None:
        try:
            self.ready.emit(self._req_id, plan_adoption(self._candidate, self._library_root, self._options))
        except Exception as exc:
            self.failed.emit(self._req_id, _format_worker_error(exc))


class AdoptWorker(QThread):
    """adopt(): progress(step id), finished_ok(AdoptionResult), failed(message, rolled_back)."""

    progress = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str, bool)

    def __init__(
        self,
        plan: AdoptionPlan,
        paths: SteamPaths | None,
        snapshot_fn: SnapshotFn | None = None,
    ) -> None:
        super().__init__()
        self._plan = plan
        self._paths = paths
        self._snapshot_fn = snapshot_fn

    def run(self) -> None:
        try:
            result = adopt(self._plan, self.progress.emit, self._snapshot_fn, paths=self._paths)
            self.finished_ok.emit(result)
        except AdoptionError as exc:
            self.failed.emit(_format_worker_error(exc), exc.rolled_back)
        except Exception as exc:
            self.failed.emit(_format_worker_error(exc), False)
