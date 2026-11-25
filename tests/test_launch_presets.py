"""Fase 3.2: one presets module behind the single presets dialog (no LSFG)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import launch_presets as lp  # noqa: E402

FULL = (
    "PROTON_ENABLE_NVAPI=1 PROTON_DLSS_UPGRADE=1 PROTON_NO_ESYNC=1 "
    "MANGOHUD=1 MANGOHUD_CONFIG=preset=3,position=top-left,fps_limit=60 "
    "gamemoderun %command% -dx11"
)


def test_reapply_keeps_presets_and_custom_args() -> None:
    once = lp.apply_presets(FULL)
    assert lp.apply_presets(once) == once  # idempotent
    st = lp.parse_presets(once)
    assert st["gamemode"] and st["mangohud"] and st["nvapi"] and st["dlss_upgrade"] and st["no_esync"]
    assert (st["mh_level"], st["mh_position"], st["mh_fps_limit"]) == (3, "top-left", 60)
    assert once.endswith("%command% -dx11")
    assert lp.active_flags(once) == ["gamemode", "mangohud", "dlss_upgrade", "nvapi", "no_esync"]


def test_changes_override_state() -> None:
    out = lp.apply_presets(FULL, mangohud=False, wined3d=True)
    st = lp.parse_presets(out)
    assert not st["mangohud"] and "MANGOHUD_CONFIG" not in out
    assert st["wined3d"] and st["gamemode"]


def test_native_linux_drops_proton_only_presets() -> None:
    out = lp.apply_presets(FULL, native_linux=True)
    assert lp.active_flags(out) == ["gamemode", "mangohud"]


def test_winedlloverrides_handling() -> None:
    opts = "WINEDLLOVERRIDES=dxgi=n,b %command%"
    assert "WINEDLLOVERRIDES" in lp.apply_presets(opts)  # None keeps it
    assert "WINEDLLOVERRIDES" not in lp.apply_presets(opts, winedlloverrides="")


def test_legacy_lsfg_tokens_are_left_alone() -> None:
    out = lp.apply_presets("ENABLE_LSFG=1 lsfg-vk %command%", gamemode=True)
    assert "ENABLE_LSFG=1" in out and "lsfg-vk" in out and "gamemoderun" in out


def test_install_form_has_one_presets_button_with_summary(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from deckinstall import ui
    from deckinstall.i18n import tr

    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
    ):
        win = ui.MainWindow(app)
    try:
        assert not hasattr(win, "chk_add_gamemode") and not hasattr(win, "chk_add_dlss")
        win._add_launch_options = ""
        win._add_winedlloverrides = ""
        win._update_add_presets_summary()
        assert win.lbl_add_presets.text() == tr("presets.none")

        with mock.patch.object(win, "_open_launch_presets_dialog", return_value=("gamemoderun MANGOHUD=1 %command%", "")):
            win._open_add_launch_advanced()
        assert win.lbl_add_presets.text() == f"{tr('presets.short_gamemode')}, {tr('presets.short_mangohud')}"
        opts, dll = win._resolve_add_launch_options()
        assert "gamemoderun" in opts and "MANGOHUD=1" in opts and dll == ""
    finally:
        # Let the window's background workers finish: a QThread alive at interpreter exit segfaults.
        for w in list(win._active_workers):
            w.wait(2000)
        app.processEvents()
        win._active_workers.clear()
        win.close()


def test_edit_presets_uses_given_game_and_parent_and_warns_without_steam(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QWidget

    from deckinstall.steam_vdf import ShortcutEntry
    from deckinstall.ui_dialogs import launch_presets_dialog as lpd

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    try:
        win = mock.Mock()
        win.paths = None
        win._selected_library_item.return_value = None
        sc = ShortcutEntry("0", 2147483649, "Game", '"/g/Game.exe"', '"/g"', "", "")
        with mock.patch.object(lpd.QMessageBox, "warning") as warn:
            lpd.edit_launch_options_presets(win, sel=("steam", sc), parent=parent)
        warn.assert_called_once()
        assert warn.call_args.args[0] is parent
        win._open_launch_presets_dialog.assert_not_called()

        win.paths = mock.Mock()
        win._open_launch_presets_dialog.return_value = None
        lpd.edit_launch_options_presets(win, sel=("steam", sc), parent=parent)
        assert win._open_launch_presets_dialog.call_args.kwargs["parent"] is parent
        assert win._open_launch_presets_dialog.call_args.kwargs["game_obj"] is sc
        win._selected_library_item.assert_not_called()
    finally:
        parent.deleteLater()
        app.processEvents()


def test_toggle_presets_round_trip_and_keep_custom_values() -> None:
    from deckinstall.launch_presets import TOGGLES, apply_presets, parse_presets

    every = {toggle.key: True for toggle in TOGGLES}
    opts = apply_presets("CUSTOM=1 %command% -dx11", every, winedlloverrides="", frame_rate=60)
    state = parse_presets(opts)
    assert all(state[toggle.key] for toggle in TOGGLES)
    assert state["frame_rate"] == 60 and "VKD3D_FRAME_RATE=60" in opts
    assert opts.startswith("CUSTOM=1 ") and opts.endswith("%command% -dx11")
    off = apply_presets(opts, {key: False for key in every}, winedlloverrides="", frame_rate=0)
    assert off == "CUSTOM=1 %command% -dx11"  # turning them off leaves nothing behind
    hand = "PROTON_FSR4_UPGRADE=4.0.1 %command%"  # a version typed by hand is not ours to drop
    assert apply_presets(hand) == hand and not parse_presets(hand)["fsr4_upgrade"]
    native = apply_presets("", every, winedlloverrides="", native_linux=True, frame_rate=60)
    assert native == ""  # Proton-only presets never reach a native Linux game


def test_toggles_are_offered_by_gpu_and_always_while_on() -> None:
    from types import SimpleNamespace

    from deckinstall.launch_presets import TOGGLES, toggle_offered

    by_key = {toggle.key: toggle for toggle in TOGGLES}
    intel = SimpleNamespace(has_amd=False, has_intel=True, has_nvidia=False, is_wayland=False)
    assert toggle_offered(by_key["vendor_amd"], intel, False)
    assert not toggle_offered(by_key["fsr4_upgrade"], intel, False)
    assert toggle_offered(by_key["fsr4_upgrade"], intel, True)  # on: shown so it can be turned off
    assert toggle_offered(by_key["xess_upgrade"], intel, False)
    assert not toggle_offered(by_key["hdr"], intel, False)
