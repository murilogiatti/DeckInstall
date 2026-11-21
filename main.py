"""Entry point for DeckInstall."""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
from pathlib import Path

_LOG_DIR = Path.home() / ".local" / "share" / "DeckInstall"
_LOG_FILE = _LOG_DIR / "deckinstall.log"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _fault_fh = _LOG_FILE.open("a", encoding="utf-8")
    faulthandler.enable(file=_fault_fh, all_threads=True)
    _handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=1_048_576, backupCount=2, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
except OSError:
    logging.basicConfig(level=logging.INFO)

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from deckinstall.steam_paths import configure_qt_environment  # noqa: E402

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

from deckinstall.ui import run  # noqa: E402


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    logging.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook

if __name__ == "__main__":
    run()
