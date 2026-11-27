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


def test_presets_are_judged_for_the_game_gpu_and_its_proton() -> None:
    from deckinstall.hardware import Gpu, HardwareProfile
    from deckinstall.launch_presets import HIDE, OFF, OK, availability, proton_kind

    vega = Gpu("amd", "amdgpu", "0000:06:00.0", "Vega", True, True, "gcn", 512)
    rtx = Gpu("nvidia", "nvidia", "0000:01:00.0", "RTX 3050", False, False, "rtx")
    laptop = HardwareProfile("amd", "Ryzen 5 5600H with Radeon Graphics", "", (rtx, vega), "wayland", "595.99")
    assert availability("fsr4_upgrade", vega, laptop, "ge") == (OFF, "need_rdna4")  # Vega is not RDNA 4
    assert availability("dlss_upgrade", vega, laptop, "ge")[0] == HIDE  # an NVIDIA option on the AMD GPU
    assert availability("dlss_upgrade", rtx, laptop, "ge") == (OK, "")
    assert availability("dlss_upgrade", rtx, laptop, "valve") == (OFF, "need_ge")
    assert availability("descriptor_heap", rtx, laptop, "") == (OK, "")
    assert availability("prime", vega, laptop, "") == (OK, "")
    old_driver = HardwareProfile("amd", "", "", (rtx, vega), "x11", "580.1")
    assert availability("descriptor_heap", rtx, old_driver, "") == (OFF, "need_nvidia_595")
    assert availability("wayland", rtx, old_driver, "ge") == (OFF, "need_wayland")
    assert availability("wayland", rtx, laptop, "valve") == (OFF, "need_ge")  # Valve's Proton has no switch
    nouveau = Gpu("nvidia", "nouveau", "0000:01:00.0", "GTX 1060", False, True, "gtx")
    assert availability("dlss_upgrade", nouveau, None, "ge") == (OFF, "need_nvidia_driver")
    for gone in ("nvapi", "no_esync", "hdr", "fsr4_rdna3"):  # no Proton reads them any more
        assert availability(gone, rtx, laptop, "ge")[0] == HIDE
    deck = HardwareProfile("amd", "AMD Custom APU 0932", "Steam Deck", (vega,), "gamescope")
    assert availability("prime", vega, deck, "")[0] == HIDE  # one GPU: nothing to send elsewhere
    assert availability("vendor_amd", vega, deck, "")[0] == HIDE
    assert availability("xess_upgrade", None, None, "") == (OK, "")  # unknown hardware never blocks
    assert [proton_kind(n) for n in ("GE-Proton11-14", "proton-cachyos-11", "proton_experimental", "")] == [
        "ge", "cachyos", "valve", ""
    ]


def test_recommended_for_this_pc() -> None:
    from deckinstall.hardware import Gpu, HardwareProfile
    from deckinstall.launch_presets import recommend, recommended_upscaler

    vega = Gpu("amd", "amdgpu", "a", "Vega", True, True, "gcn", 512)
    rtx = Gpu("nvidia", "nvidia", "b", "RTX 3050", False, False, "rtx")
    laptop = HardwareProfile("amd", "", "", (rtx, vega), "wayland", "595.99")
    assert recommend(laptop, "ge") == {"prime": True, "dlss_upgrade": True}  # NVAPI is on by default
    assert recommend(laptop, "valve") == {"prime": True}  # DLSS upgrade needs GE-Proton
    deck_gpu = Gpu("amd", "amdgpu", "c", "Custom GPU 0405", True, True, "rdna2", 1024)
    deck = HardwareProfile("amd", "AMD Custom APU 0932", "Steam Deck", (deck_gpu,), "gamescope")
    assert recommend(deck, "ge") == {}  # the frame cap is the user's choice, never recommended
    rdna4 = HardwareProfile("amd", "", "", (Gpu("amd", "amdgpu", "d", "RX 9070", False, True, "rdna4", 16384),), "x11")
    assert recommend(rdna4, "ge") == {"fsr4_upgrade": True}
    arc = Gpu("intel", "xe", "0000:03:00.0", "Arc B580", False, True, "arc")
    assert recommend(HardwareProfile("intel", "", "", (arc,), "wayland"), "cachyos") == {"xess_upgrade": True}
    assert [recommended_upscaler(g) for g in (rtx, arc, deck_gpu, None)] == ["dlss", "xess", "fsr3", "auto"]


def test_presets_window_follows_the_gpu_the_game_runs_on() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QToolButton, QWidget

    from deckinstall.hardware import Gpu, HardwareProfile
    from deckinstall.i18n import tr
    from deckinstall.ui_dialogs import launch_presets_dialog as lpd

    app = QApplication.instance() or QApplication([])
    vega = Gpu("amd", "amdgpu", "a", "Radeon Vega", True, True, "gcn", 512)
    rtx = Gpu("nvidia", "nvidia", "b", "RTX 3050", False, False, "rtx")
    laptop = HardwareProfile("amd", "", "", (rtx, vega), "wayland", "595.99")
    parent = QWidget()
    seen: dict[str, object] = {}

    def inspect() -> None:
        dlg = QApplication.activeModalWidget()
        if dlg is None:
            QTimer.singleShot(10, inspect)
            return
        next(b for b in dlg.findChildren(QToolButton) if b.text() == tr("launch.more_options")).click()
        boxes = {c.text(): c for c in dlg.findChildren(QCheckBox)}
        fsr4, dlss = boxes[tr("launch.fsr4_upgrade")], boxes[tr("launch.dlss_upgrade")]
        gpu = dlg.findChild(QComboBox, "gameGpu")
        seen["on_vega"] = (not fsr4.isHidden(), fsr4.isEnabled(), not dlss.isHidden())
        gpu.setCurrentIndex(gpu.findData(True))
        seen["on_rtx"] = (not fsr4.isHidden(), not dlss.isHidden(), dlss.isEnabled())
        dlg.reject()

    try:
        with mock.patch.object(lpd, "hardware", return_value=laptop):
            QTimer.singleShot(0, inspect)
            res = lpd.open_launch_presets_dialog(
                mock.Mock(), "Game", "standalone", "", "", parent=parent, proton="ge"
            )
        assert res is None
        assert seen["on_vega"] == (True, False, False)  # FSR 4: greyed with the reason; DLSS: not for AMD
        assert seen["on_rtx"] == (False, True, True)  # sent to the RTX: DLSS appears, AMD options go
    finally:
        parent.deleteLater()
        app.processEvents()


def test_frame_cap_uses_what_proton_reads_and_one_dxvk_config() -> None:
    from deckinstall.launch_presets import apply_presets, migrate_launch_options, parse_presets

    mine = "DXVK_CONFIG='dxgi.syncInterval=0' %command% -dx11"
    opts = apply_presets(mine, {}, winedlloverrides="", frame_rate=45, vendor_amd=True)
    assert opts == (
        "DXVK_CONFIG='dxgi.syncInterval=0;dxgi.maxFrameRate=45;d3d9.maxFrameRate=45;dxgi.customVendorId=1002' "
        "VKD3D_FRAME_RATE=45 %command% -dx11"
    )  # DXVK_FRAME_RATE is gone from DXVK: Proton 11 and GE-Proton 11 read only DXVK_CONFIG
    state = parse_presets(opts)
    assert state["frame_rate"] == 45 and state["vendor_amd"] and state["dxvk_custom"] == ["dxgi.syncInterval=0"]
    assert apply_presets(opts, frame_rate=0, vendor_amd=False) == "DXVK_CONFIG=dxgi.syncInterval=0 %command% -dx11"
    assert "fps_limit=60" in apply_presets("MANGOHUD=1 %command%", frame_rate=60)  # one cap, MangoHud too
    old = "PROTON_ENABLE_NVAPI=1 DXVK_ENABLE_NVAPI=1 DXVK_FRAME_RATE=40 gamemoderun %command% -x"
    assert migrate_launch_options(old) == (
        "DXVK_CONFIG='dxgi.maxFrameRate=40;d3d9.maxFrameRate=40' VKD3D_FRAME_RATE=40 gamemoderun %command% -x"
    )
    assert migrate_launch_options("gamemoderun %command%") == "gamemoderun %command%"


def test_half_the_screen_divides_its_rate() -> None:
    from deckinstall.ui_dialogs.launch_presets_dialog import half_rate

    assert [half_rate(hz) for hz in (60, 90, 120, 144, 165)] == [30, 45, 60, 72, 55]
