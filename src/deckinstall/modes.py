"""Steam mode vs Standalone mode: which one a system offers and what each mode uses.

Steam mode: Steam runs the game (its own Proton, Steam Linux Runtime and performance overlay).
DeckInstall installs/configures the game and adds the shortcut; it downloads no runtime for it.
Standalone mode: DeckInstall runs the game itself with umu-run + GE-Proton (+ MangoHud), from its
Library, the app menu or a desktop shortcut. Steam is not needed.

SteamOS always runs games through Steam, so it only offers Steam mode. Without Steam only
Standalone mode exists. Every screen asks here instead of deciding on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .i18n import tr

STEAM = "steam"
STANDALONE = "standalone"

_OS_RELEASE = Path("/etc/os-release")

# Features that only one mode uses. Value: (mode that uses it, why it is off in the other mode).
_FEATURE_MODE: dict[str, tuple[str, Callable[[], str]]] = {
    "mangohud": (STANDALONE, lambda: tr("mode.note_mangohud_steam")),
    "app_shortcuts": (STANDALONE, lambda: tr("mode.note_shortcuts_steam")),
}


def _os_release_fields(path: Path | None = None) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        text = (path or _OS_RELEASE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fields
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


_IMMUTABLE_DISTRO_IDS = frozenset({"steamos", "bazzite", "chimeraos"})


def is_steamos(path: Path | None = None) -> bool:
    """True on Valve's SteamOS (ID=steamos), not on other gaming distros such as Bazzite."""
    return _os_release_fields(path).get("ID", "").lower() == "steamos"


def is_immutable_os(
    os_release_path: Path | None = None,
    ostree_marker: Path | None = None,
) -> bool:
    """True on immutable gaming OSes (SteamOS, Bazzite/ostree, ChimeraOS) where pacman/dnf cannot mutate /usr."""
    if _os_release_fields(os_release_path).get("ID", "").lower() in _IMMUTABLE_DISTRO_IDS:
        return True
    marker = ostree_marker if ostree_marker is not None else Path("/run/ostree-booted")
    return marker.exists()


@dataclass(frozen=True)
class ModeEnv:
    """What the running system offers: SteamOS, and whether a usable Steam was found."""

    steamos: bool
    steam: bool

    @property
    def default_mode(self) -> str:
        return STEAM if self.steam or self.steamos else STANDALONE

    def mode_note(self, mode: str) -> str:
        """Why `mode` cannot be used here, for the user ("" when it can)."""
        if mode == STANDALONE and self.steamos:
            return tr("mode.note_standalone_steamos")
        if mode == STEAM and not self.steam:
            return tr("mode.note_steam_missing")
        return ""

    def component_note(self, component: str) -> str:
        """Why a runtime component is not needed on this system ("" when it is useful).

        component: "ge_proton", "umu" or "mangohud" (what Standalone mode downloads).
        """
        if not self.steamos:
            return ""
        if component == "ge_proton":
            return tr("runtimes.note_proton_steamos")
        if component == "umu":
            return tr("runtimes.note_umu_steamos")
        if component == "mangohud":
            return tr("runtimes.note_mangohud_steamos")
        return ""


def feature_note(feature: str, mode: str) -> str:
    """Why `feature` is off in `mode`, for the user ("" when the mode uses it)."""
    rule = _FEATURE_MODE.get(feature)
    if rule is None or rule[0] == mode:
        return ""
    return rule[1]()


def mode_of_kind(kind: str) -> str:
    """Mode of a Library row: Steam shortcuts run through Steam; DeckInstall games outside
    Steam run standalone. Games of other launchers (Heroic, Lutris...) keep their launcher."""
    return STEAM if kind == STEAM else STANDALONE
