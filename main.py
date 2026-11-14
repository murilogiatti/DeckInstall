"""Entry point for DeckInstall."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Bridge isolated AppImage Qt6 to the host KDE/XDG file chooser portal.
os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from deckinstall.ui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
