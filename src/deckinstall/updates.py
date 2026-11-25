"""One update check for DeckInstall itself and the runtimes it manages.

Every component is checked in parallel and on its own: a failure (offline, rate limit) marks that
component and never hides the others. Nothing here downloads; the UI decides what to apply.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from . import runtime_manager as _rm
from .app_updater import check_app_update

APP = "app"
# Display order; "app" is always checked, the runtimes only when in use (the caller says which).
ORDER = (APP, "ge_proton", "umu", "optiscaler", "mangohud")
LABELS = {
    APP: "DeckInstall",
    "ge_proton": "GE-Proton",
    "umu": "umu-run",
    "optiscaler": "OptiScaler",
    "mangohud": "MangoHud",
}


def _entry(installed: str = "", latest: str = "", update: bool = False) -> dict[str, Any]:
    """installed/latest: version text. update: a newer one exists and can be applied here.
    reason: why an available update cannot be applied here ("" when it can). error: the check
    failed. stale: GitHub was unreachable and the last known answer was used. info: AppUpdateInfo."""
    return {
        "installed": installed,
        "latest": latest,
        "update": update,
        "reason": "",
        "error": "",
        "stale": False,
        "info": None,
    }


def _check_app() -> dict[str, dict[str, Any]]:
    info = check_app_update(fetch_json=_rm._fetch_json)  # ETag + cached answer when offline
    entry = _entry(info.current_version, info.latest_version, info.has_update and info.can_self_update)
    entry["info"] = info
    if info.has_update and not info.can_self_update:
        entry["reason"] = info.cannot_update_reason
    return {APP: entry}


def _check_umu_ge(wanted: set[str]) -> dict[str, dict[str, Any]]:
    data = _rm.check_runtime_updates(force=True)
    out: dict[str, dict[str, Any]] = {}
    for comp in ("umu", "ge_proton"):
        if comp not in wanted:
            continue
        d = data.get(comp) or {}
        installed = str(d.get("installed") or "")
        latest = str(d.get("latest") or "")
        out[comp] = _entry(installed, latest, bool(d.get("update_available")) or (not installed and bool(latest)))
    return out


def _check_pair(comp: str, fn: Callable[..., tuple[str, str]]) -> dict[str, dict[str, Any]]:
    installed, latest = fn(force=True)
    return {comp: _entry(installed, latest, bool(latest) and installed != latest)}


def check_all(runtimes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Check DeckInstall plus the given runtime components ("umu", "ge_proton", "mangohud",
    "optiscaler"); returns {component: entry} in ORDER."""
    wanted = set(runtimes)
    jobs: list[tuple[tuple[str, ...], Callable[[], dict[str, dict[str, Any]]]]] = [((APP,), _check_app)]
    umu_ge = tuple(c for c in ("umu", "ge_proton") if c in wanted)
    if umu_ge:
        jobs.append((umu_ge, lambda: _check_umu_ge(wanted)))
    if "mangohud" in wanted:
        jobs.append((("mangohud",), lambda: _check_pair("mangohud", _rm.mangohud_versions)))
    if "optiscaler" in wanted:
        jobs.append((("optiscaler",), lambda: _check_pair("optiscaler", _rm.optiscaler_versions)))

    def _run(job: tuple[tuple[str, ...], Callable[[], dict[str, dict[str, Any]]]]) -> dict[str, dict[str, Any]]:
        comps, fn = job
        _rm.reset_stale_answers()
        try:
            result = fn()
        except Exception as exc:  # one component failing never hides the others
            logging.warning("Update check failed for %s: %s", ", ".join(comps), exc)
            result = {}
            for comp in comps:
                result[comp] = _entry()
                result[comp]["error"] = error_kind(exc)
        stale = _rm.stale_answers_used()
        for entry in result.values():
            entry["stale"] = entry["stale"] or stale
        return result

    merged: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for part in pool.map(_run, jobs):
            merged.update(part)
    return {comp: merged[comp] for comp in ORDER if comp in merged}


def error_kind(exc: BaseException) -> str:
    """"rate_limit", "offline" or the error text (the UI words the first two)."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", 0)
        if status in (403, 429):
            return "rate_limit"
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "offline"
    return str(exc) or type(exc).__name__


def pending(results: dict[str, dict[str, Any]]) -> list[str]:
    """Components with an update that can be applied here."""
    return [comp for comp, entry in results.items() if entry["update"] and not entry["error"]]


AUTO_CHECK_INTERVAL = 24 * 60 * 60
MANUAL_CHECK_COOLDOWN = 60  # GitHub allows 60 unauthenticated calls an hour; a check costs ~5


def auto_check_due(last_check: float, now: float | None = None) -> bool:
    """True when the daily background check should run (also after a clock jump backwards)."""
    now = time.time() if now is None else now
    return not (0 <= now - last_check < AUTO_CHECK_INTERVAL)
