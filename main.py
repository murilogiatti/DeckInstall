"""Entry point for DeckInstall."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
from pathlib import Path

# Bridge isolated AppImage Qt6 to the host KDE/XDG file chooser portal.
os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")

_LOG_DIR = Path.home() / ".local" / "share" / "DeckInstall"
_LOG_FILE = _LOG_DIR / "deckinstall.log"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _LOG_FILE.is_file() and _LOG_FILE.stat().st_size > 1_048_576:
        _LOG_FILE.write_text("", encoding="utf-8")
    _fault_fh = _LOG_FILE.open("a", encoding="utf-8")
    faulthandler.enable(file=_fault_fh, all_threads=True)
    logging.basicConfig(
        filename=str(_LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
except OSError:
    pass

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from deckinstall.ui import main  # noqa: E402


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    logging.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook

if __name__ == "__main__":
    raise SystemExit(main())
