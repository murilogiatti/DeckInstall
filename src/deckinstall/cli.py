"""Lightweight CLI and process bootstrap (runs before PySide6 is imported)."""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import NoReturn

from . import __app_name__, __version__

_MAX_FAULTS_LOG_BYTES = 2 * 1024 * 1024
_LOG_CONFIGURED = False


def _data_log_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "DeckInstall"


def configure_logging() -> None:
    """Configure rotating file logging, bounded faulthandler log and excepthook once."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    _LOG_CONFIGURED = True
    log_dir = _data_log_dir()
    log_file = log_dir / "deckinstall.log"
    faults_file = log_dir / "deckinstall-faults.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            if (
                faults_file.is_file()
                and not faults_file.is_symlink()
                and faults_file.stat().st_size > _MAX_FAULTS_LOG_BYTES
            ):
                prev = log_dir / "deckinstall-faults.log.1"
                os.replace(faults_file, prev)
        except OSError:
            pass
        fault_fh = faults_file.open("a", encoding="utf-8")
        faulthandler.enable(file=fault_fh, all_threads=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_048_576, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    except OSError:
        logging.basicConfig(level=logging.INFO)

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        logging.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


def run(argv: list[str] | None = None) -> NoReturn:
    """Entry point for both main.py and the 'deckinstall' console script.

    Handles --version and --doctor without importing PySide6 or touching Qt/GL.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if "--version" in args:
        print(f"{__app_name__} {__version__}")
        raise SystemExit(0)
    if "--doctor" in args:
        from .doctor import main as doctor_main

        raise SystemExit(doctor_main(args))

    configure_logging()
    from .steam_paths import configure_qt_environment

    # Before any Qt import: portal theme only on the desktop, XWayland in Gaming Mode (SIGSEGV on the Deck).
    configure_qt_environment(os.environ)
    logging.info(
        "Qt env: QT_QPA_PLATFORM=%s QT_QPA_PLATFORMTHEME=%s XDG_CURRENT_DESKTOP=%s WAYLAND_DISPLAY=%s DISPLAY=%s",
        os.environ.get("QT_QPA_PLATFORM", ""),
        os.environ.get("QT_QPA_PLATFORMTHEME", ""),
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("WAYLAND_DISPLAY", ""),
        os.environ.get("DISPLAY", ""),
    )

    from .ui import run as ui_run

    ui_run()
