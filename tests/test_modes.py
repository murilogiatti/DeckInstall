"""Steam mode vs Standalone mode: one rule for every screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from deckinstall import modes
from deckinstall.i18n import tr
from deckinstall.modes import STANDALONE, STEAM, ModeEnv, feature_note, mode_of_kind


@pytest.mark.parametrize(
    ("text", "steamos"),
    [
        ('NAME="SteamOS"\nID=steamos\nID_LIKE=arch\n', True),
        ("ID=bazzite\nID_LIKE=fedora\n", False),
        ('ID="ubuntu"\n', False),
        ("", False),
    ],
)
def test_steamos_is_read_from_os_release(tmp_path: Path, text: str, steamos: bool) -> None:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    assert modes.is_steamos(path) is steamos


def test_missing_os_release_is_not_steamos(tmp_path: Path) -> None:
    assert modes.is_steamos(tmp_path / "nope") is False
    assert modes.is_steamos() is False  # conftest points the default at a missing file


def test_modes_offered_per_system() -> None:
    steamos = ModeEnv(steamos=True, steam=True)
    assert steamos.default_mode == STEAM and steamos.mode_note(STEAM) == ""
    assert steamos.mode_note(STANDALONE) == tr("mode.note_standalone_steamos")
    desktop = ModeEnv(steamos=False, steam=True)
    assert desktop.default_mode == STEAM
    assert desktop.mode_note(STANDALONE) == "" and desktop.mode_note(STEAM) == ""
    no_steam = ModeEnv(steamos=False, steam=False)
    assert no_steam.default_mode == STANDALONE and no_steam.mode_note(STANDALONE) == ""
    assert no_steam.mode_note(STEAM) == tr("mode.note_steam_missing")


def test_features_belong_to_one_mode() -> None:
    assert feature_note("mangohud", STEAM) == tr("mode.note_mangohud_steam")
    assert feature_note("mangohud", STANDALONE) == ""
    assert feature_note("app_shortcuts", STEAM) == tr("mode.note_shortcuts_steam")
    assert feature_note("gamemode", STEAM) == "" and feature_note("gamemode", STANDALONE) == ""


def test_steamos_needs_no_standalone_runtime() -> None:
    steamos = ModeEnv(steamos=True, steam=True)
    for component in ("ge_proton", "umu", "mangohud"):
        assert steamos.component_note(component)
    assert ModeEnv(steamos=False, steam=True).component_note("mangohud") == ""


def test_library_row_kind_maps_to_mode() -> None:
    assert mode_of_kind("steam") == STEAM
    assert mode_of_kind("standalone") == STANDALONE
    assert mode_of_kind("external") == STANDALONE
