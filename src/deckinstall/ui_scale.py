"""Interface size: Auto (125% in Gaming Mode), 100% or 125%.

Applied through QT_SCALE_FACTOR, which Qt reads once when the QApplication is created: set just
before it and removed right after, so games and tools started by DeckInstall never inherit it.
A QT_SCALE_FACTOR the user exported is left alone. 150% is not offered: on the Deck's 1280x800
screen it leaves 853x533 logical pixels, less than the window's 840x560 minimum.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

SCALE_KEY = "ui/scale"
CHOICES = ("auto", "1", "1.25")
GAMING_MODE_SCALE = "1.25"  # buttons ~47 px and text >= 11 px tall on the Deck (measured)
_ENV = "QT_SCALE_FACTOR"


def resolve_scale(choice: str, *, gaming_mode: bool, env: MutableMapping[str, str]) -> str | None:
    """Factor to set for this start, or None (leave Qt's default / the user's own variable)."""
    if env.get(_ENV, "").strip():
        return None
    if choice not in CHOICES:
        choice = "auto"
    if choice == "auto":
        return GAMING_MODE_SCALE if gaming_mode else None
    return None if choice == "1" else choice


def set_scale_env(factor: str | None, env: MutableMapping[str, str] | None = None) -> bool:
    """Export the factor for the QApplication about to be created; True when set (to undo later)."""
    if factor is None:
        return False
    (os.environ if env is None else env)[_ENV] = factor
    return True


def clear_scale_env(was_set: bool, env: MutableMapping[str, str] | None = None) -> None:
    """Drop the factor again once the QApplication exists (children must not inherit it)."""
    if was_set:
        (os.environ if env is None else env).pop(_ENV, None)
