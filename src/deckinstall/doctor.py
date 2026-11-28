"""`deckinstall --doctor`: what this machine offers DeckInstall, as text or JSON.

Read-only. Every check runs with a short timeout and never needs a display, Steam or the network,
so the report also works over ssh and in a bug report ("Copy diagnostics" in Maintenance).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __app_name__, __version__

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

# Tools DeckInstall can use, and what each is for (shown next to "missing").
_TOOLS: dict[str, str] = {
    "7z": "unpack the OptiScaler download (.7z)",
    "7zz": "unpack the OptiScaler download (.7z)",
    "innoextract": "unpack GOG offline installers",
    "winetricks": "Winetricks verbs on a prefix",
    "gamemoderun": "GameMode preset",
    "bwrap": "umu-run container (Standalone mode)",
    "unshare": "user namespace probe",
    "fusermount3": "mount the AppImage",
    "fusermount": "mount the AppImage",
    "flatpak": "Flatpak Steam",
    "systemd-inhibit": "keep the PC awake while a game runs",
}


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _run(cmd: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _app() -> list[dict[str, str]]:
    from .app_updater import get_running_appimage

    appimage = get_running_appimage()
    return [
        _check("version", INFO, f"{__app_name__} {__version__}"),
        _check("python", INFO, f"{platform.python_version()} ({sys.executable})"),
        _check("appimage", INFO, str(appimage) if appimage else "not running from an AppImage"),
    ]


def _system() -> list[dict[str, str]]:
    from .modes import _os_release_fields, is_immutable_os, is_steamos

    fields = _os_release_fields()
    pretty = fields.get("PRETTY_NAME") or fields.get("NAME") or "unknown"
    session = os.environ.get("XDG_SESSION_TYPE", "") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "")
    return [
        _check("os", INFO, f"{pretty} (ID={fields.get('ID', '?')})"),
        _check("kernel", INFO, platform.release()),
        _check("steamos", INFO, "yes" if is_steamos() else "no"),
        _check(
            "immutable",
            INFO,
            "yes: DeckInstall never installs system packages here" if is_immutable_os() else "no",
        ),
        _check("session", INFO, f"{session or 'unknown'} desktop={os.environ.get('XDG_CURRENT_DESKTOP', '')}"),
    ]


def _fuse() -> list[dict[str, str]]:
    has_dev = Path("/dev/fuse").exists()
    helper = shutil.which("fusermount3") or shutil.which("fusermount")
    if has_dev and helper:
        return [_check("fuse", OK, f"/dev/fuse and {helper}")]
    missing = ", ".join(x for x, ok in (("/dev/fuse", has_dev), ("fusermount3/fusermount", helper)) if not ok)
    return [
        _check(
            "fuse",
            WARN,
            f"missing {missing}: install fuse3 (or run the AppImage with --appimage-extract-and-run)",
        )
    ]


def _gpu() -> list[dict[str, str]]:
    from .runtime_manager import detect_gpu_profile

    gpu = detect_gpu_profile()
    vendors = [n for n, has in (("NVIDIA", gpu.has_nvidia), ("AMD", gpu.has_amd), ("Intel", gpu.has_intel)) if has]
    detail = ", ".join(vendors) or "none found"
    if gpu.is_hybrid:
        detail += " (hybrid)"
    if gpu.has_nvidia:
        detail += "; NVIDIA driver " + ("loaded" if gpu.nvidia_driver_present else "NOT loaded")
    status = WARN if gpu.has_nvidia and not gpu.nvidia_driver_present else INFO
    checks = [_check("gpu", status, detail)]
    from .hardware import detect_hardware

    profile = detect_hardware()
    for card in profile.gpus:
        role = "primary" if card is profile.primary else "other"
        kind = "integrated" if card.integrated else "dedicated"
        driver = card.driver
        if card.proprietary_nvidia and profile.nvidia_driver:
            driver += f" {profile.nvidia_driver}"
        checks.append(_check(
            f"gpu {card.pci}",
            WARN if card.driver == "nouveau" else INFO,
            f"{card.name or card.vendor} ({role}, {kind}, {card.generation or '?'}, driver {driver or '?'})",
        ))
    if profile.device:
        checks.append(_check("device", INFO, profile.device))
    return checks


def _userns() -> list[dict[str, str]]:
    """Real probe: can bwrap (what umu-run uses) create a user namespace for this user?

    bwrap is what matters: on Ubuntu 24.04+ AppArmor may block a plain `unshare --user` while a
    profiled bwrap still works, so unshare is only the fallback when bwrap is missing.
    """
    bwrap = shutil.which("bwrap")
    if bwrap:
        code, text = _run([bwrap, "--unshare-user", "--ro-bind", "/", "/", "--dev", "/dev", "true"])
        if code == 0:
            return [_check("userns", OK, "bwrap can create user namespaces (umu-run works)")]
        return [
            _check(
                "userns",
                FAIL,
                f"bwrap --unshare-user failed: {text or code}. On Ubuntu, DeckInstall offers the "
                "AppArmor fix when it sets up umu-run",
            )
        ]
    unshare = shutil.which("unshare")
    if not unshare:
        return [_check("userns", INFO, "neither bwrap nor unshare found; probe skipped")]
    code, text = _run([unshare, "--user", "--map-root-user", "true"])
    if code == 0:
        return [_check("userns", OK, "unprivileged user namespaces work (bwrap not installed)")]
    return [_check("userns", WARN, f"unshare --user failed: {text or code} (bwrap not installed)")]


def _tools() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for tool, use in _TOOLS.items():
        path = shutil.which(tool)
        out.append(_check(f"tool:{tool}", OK if path else INFO, path or f"missing ({use})"))
    return out


def _steam() -> list[dict[str, str]]:
    from .steam_paths import (
        STEAM_ROOT_ENV,
        find_steam_root,
        find_steam_roots,
        is_gamescope_session,
        is_steam_running,
        list_steam_accounts,
        steam_variant,
        unsupported_snap_steam,
    )

    out: list[dict[str, str]] = []
    snap = unsupported_snap_steam()
    if snap is not None:
        msg = f"Snap Steam found at {snap}: not supported; use the native or Flatpak Steam"
        out.append(_check("steam_snap", WARN, msg))
    roots = find_steam_roots()
    if not roots:
        return [*out, _check("steam", INFO, "no Steam installation found: only Standalone mode")]
    active = find_steam_root()
    for root in roots:
        mark = " [in use]" if root == active else ""
        accounts = list_steam_accounts(root)
        detail = f"{steam_variant(root)}: {root} ({len(accounts)} user(s)){mark}"
        if len(accounts) > 1:
            labels = ", ".join(a.display_label for a in accounts[:5])
            detail += f" — {labels}"
        out.append(_check("steam", OK, detail))
    if len(roots) > 1:
        out.append(_check("steam_choice", INFO, f"several Steams; choose one with {STEAM_ROOT_ENV}=<path>"))
    out.append(_check("steam_running", INFO, "yes" if is_steam_running() else "no"))
    out.append(_check("gaming_mode", INFO, "yes" if is_gamescope_session() else "no"))
    return out


def _runtimes() -> list[dict[str, str]]:
    from .runtime_manager import (
        find_system_umu_run,
        get_umu_run_path,
        list_ge_proton_dirs,
        list_umu_proton_dirs,
        official_mangohud_installed,
        optiscaler_downloaded,
        system_mangohud_installed,
    )

    umu = get_umu_run_path()
    umu_detail = str(umu) if umu.is_file() else (str(find_system_umu_run() or "") or "not installed")
    umu_proton = [p.name for p in list_umu_proton_dirs()]
    legacy = [p.name for p in list_ge_proton_dirs(min_major=1)]
    mango = "official" if official_mangohud_installed() else ("system" if system_mangohud_installed() else "none")
    return [
        _check("umu-run", INFO, umu_detail),
        _check("umu-proton", INFO, ", ".join(umu_proton) or "none"),
        *([_check("ge-proton (legacy)", INFO, ", ".join(legacy))] if legacy else []),
        _check("mangohud", INFO, mango),
        _check("optiscaler", INFO, "downloaded" if optiscaler_downloaded() else "not downloaded"),
    ]


def _libraries() -> list[dict[str, str]]:
    from .library import discover_libraries

    out = []
    for lib in discover_libraries():
        if not lib.available:
            out.append(_check("library", WARN, f"{lib.root} (disk not mounted)"))
            continue
        try:
            free = shutil.disk_usage(lib.root).free / 1024**3
        except OSError:
            free = -1.0
        fs = lib.fs_type or "?"
        status = WARN if not lib.unix_fs else OK
        note = "; prefixes go to the internal disk" if not lib.unix_fs else ""
        out.append(_check("library", status, f"{lib.root} [{fs}] {free:.1f} GiB free{note}"))
    return out or [_check("library", INFO, "no library yet")]


_SECTIONS: tuple[tuple[str, Callable[[], list[dict[str, str]]]], ...] = (
    ("app", _app),
    ("system", _system),
    ("appimage", _fuse),
    ("gpu", _gpu),
    ("sandbox", _userns),
    ("steam", _steam),
    ("runtimes", _runtimes),
    ("libraries", _libraries),
    ("tools", _tools),
)


def collect_report() -> dict[str, Any]:
    """All checks, by section. A section that raises becomes one "fail" line instead of aborting."""
    report: dict[str, Any] = {}
    for name, fn in _SECTIONS:
        try:
            report[name] = fn()
        except Exception as exc:  # a broken probe must not hide the others
            report[name] = [_check(name, FAIL, f"check crashed: {exc!r}")]
    return report


def worst_status(report: dict[str, Any]) -> str:
    statuses = {c["status"] for checks in report.values() for c in checks}
    for status in (FAIL, WARN):
        if status in statuses:
            return status
    return OK


def format_text(report: dict[str, Any]) -> str:
    marks = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]", INFO: "      "}
    lines: list[str] = []
    for section, checks in report.items():
        lines.append(f"== {section}")
        for c in checks:
            lines.append(f"{marks.get(c['status'], '      ')} {c['name']}: {c['detail']}")
    return "\n".join(lines)


def _private_words(accounts: bool = True) -> list[tuple[str, str]]:
    """(text, replacement) pairs that identify this person: home, user and host names, and (with
    accounts) the Steam account names and ids."""
    import getpass
    import os
    import socket
    from pathlib import Path

    pairs: list[tuple[str, str]] = []
    home = str(Path.home())
    if home not in ("", "/"):
        pairs.append((home, "~"))
    try:
        if not accounts:
            raise LookupError
        from .steam_paths import find_steam_roots, list_steam_accounts

        n = 0
        for root in find_steam_roots():
            for acc in list_steam_accounts(root):
                n += 1
                for value in {acc.persona_name, acc.account_name, getattr(acc, "user_id", ""), acc.display_label}:
                    if value and len(str(value)) >= 3:
                        pairs.append((str(value), f"<steam account {n}>"))
    except Exception:  # a broken Steam probe must not leak by aborting the masking
        pass
    for value, label in ((getpass.getuser(), "<user>"), (socket.gethostname(), "<host>"),
                         (os.environ.get("USER", ""), "<user>")):
        if value and len(value) >= 3:
            pairs.append((value, label))
    return sorted(set(pairs), key=lambda kv: -len(kv[0]))  # longest first: the home before the user name


def mask_text(text: str, words: list[tuple[str, str]] | None = None) -> str:
    """The report with home paths, user/host names and Steam accounts replaced by placeholders."""
    import re

    for value, label in (words if words is not None else _private_words()):
        if re.fullmatch(r"[\w.-]+", value):  # a bare name or number: only as a whole word
            text = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", label, text)
        else:
            text = text.replace(value, label)
    return re.sub(r"\b7656119\d{10}\b", "<steamid>", text)  # 64-bit Steam IDs


def shareable_report(report: dict[str, Any]) -> dict[str, Any]:
    """A copy of the report with personal data masked in every value.

    Steam account names are masked only in the Steam section, where they are listed: a persona
    called "Proton" must not turn every "Proton" of the report into a placeholder.
    """
    general = _private_words(accounts=False)
    with_accounts = _private_words(accounts=True)
    out: dict[str, Any] = {}
    for section, checks in report.items():
        words = with_accounts if section == "steam" else general
        out[section] = [
            {key: mask_text(value, words) if isinstance(value, str) else value for key, value in check.items()}
            for check in checks
        ]
    return out


def shareable_text(report: dict[str, Any]) -> str:
    """What "Copy diagnostics" puts on the clipboard: masked, with a note saying so."""
    return "(personal paths, user and Steam account names are masked)\n" + format_text(shareable_report(report))


def main(args: list[str]) -> int:
    """Print the report. Exit code: 0 all fine, 1 warnings, 2 failures.

    --share masks personal data (home folder, user and host names, Steam accounts).
    """
    report = collect_report()
    if "--share" in args:
        print(json.dumps(shareable_report(report), indent=2, ensure_ascii=False) if "--json" in args
              else shareable_text(report))
    elif "--json" in args:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return {OK: 0, WARN: 1, FAIL: 2}[worst_status(report)]
