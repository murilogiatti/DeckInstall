"""Hardware profile from fake sysfs/procfs trees: one per common machine (x86_64 only)."""

from __future__ import annotations

from pathlib import Path

from deckinstall.hardware import detect_hardware


def _gpu(root: Path, card: int, pci: str, vendor: str, driver: str, *, boot: bool, vram: int = 0,
         gc: tuple[int, int] | None = None, nvidia_model: str = "") -> None:
    dev = root / "sys/devices/pci0000:00" / pci
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "boot_vga").write_text("1\n" if boot else "0\n")
    if vram:
        (dev / "mem_info_vram_total").write_text(f"{vram << 20}\n")
    if gc is not None:
        ip = dev / "ip_discovery/die/0/GC/0"
        ip.mkdir(parents=True)
        (ip / "major").write_text(f"{gc[0]}\n")
        (ip / "minor").write_text(f"{gc[1]}\n")
    drv = root / "sys/bus/pci/drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    (dev / "driver").symlink_to(drv)
    card_dir = root / "sys/class/drm" / f"card{card}"
    card_dir.mkdir(parents=True)
    (card_dir / "device").symlink_to(dev)
    (root / "sys/class/drm" / f"card{card}-eDP-1").mkdir()  # a connector, never a GPU
    if nvidia_model:
        info = root / "proc/driver/nvidia/gpus" / pci
        info.mkdir(parents=True)
        (info / "information").write_text(f"Model: \t\t {nvidia_model}\nIRQ: 1\n")


def _machine(root: Path, vendor_id: str, model: str, product: str = "") -> None:
    (root / "proc").mkdir(parents=True, exist_ok=True)
    (root / "proc/cpuinfo").write_text(f"vendor_id\t: {vendor_id}\nmodel name\t: {model}\n")
    dmi = root / "sys/class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "product_name").write_text(product + "\n")


def test_steam_deck(tmp_path: Path) -> None:
    _machine(tmp_path, "AuthenticAMD", "AMD Custom APU 0932", "Galileo")
    _gpu(tmp_path, 0, "0000:04:00.0", "0x1002", "amdgpu", boot=True, vram=1024, gc=(10, 3))
    hw = detect_hardware(tmp_path, env={"XDG_CURRENT_DESKTOP": "gamescope"}, names={})
    assert hw.device == "Steam Deck" and hw.session == "gamescope" and not hw.is_hybrid
    gpu = hw.game_gpu()
    assert gpu is not None and gpu.integrated and gpu.generation == "rdna2"


def test_hybrid_laptop_amd_apu_and_rtx(tmp_path: Path) -> None:
    _machine(tmp_path, "AuthenticAMD", "AMD Ryzen 5 5600H with Radeon Graphics", "Dell G15 5515")
    _gpu(tmp_path, 1, "0000:01:00.0", "0x10de", "nvidia", boot=False, nvidia_model="NVIDIA GeForce RTX 3050 Laptop GPU")
    _gpu(tmp_path, 2, "0000:06:00.0", "0x1002", "amdgpu", boot=True, vram=512, gc=(9, 3))
    (tmp_path / "sys/module/nvidia").mkdir(parents=True)
    (tmp_path / "sys/module/nvidia/version").write_text("595.99.02\n")
    hw = detect_hardware(tmp_path, env={"WAYLAND_DISPLAY": "wayland-0"}, names={"0000:06:00.0": "Radeon Vega"})
    assert hw.is_hybrid and hw.nvidia_major == 595 and hw.device == ""
    primary, dedicated = hw.game_gpu(False), hw.game_gpu(True)
    assert primary is not None and primary.name == "Radeon Vega" and primary.integrated and primary.generation == "gcn"
    assert dedicated is not None and dedicated.generation == "rtx" and dedicated.proprietary_nvidia


def test_intel_desktop_with_rdna4_card(tmp_path: Path) -> None:
    _machine(tmp_path, "GenuineIntel", "Intel(R) Core(TM) i5-14600K")
    _gpu(tmp_path, 0, "0000:03:00.0", "0x1002", "amdgpu", boot=True, vram=16384, gc=(12, 0))
    hw = detect_hardware(tmp_path, env={}, names={})
    gpu = hw.game_gpu()
    assert gpu is not None and not gpu.integrated and gpu.generation == "rdna4" and hw.session == "x11"


def test_rdna3_handheld_and_old_gcn_card(tmp_path: Path) -> None:
    _machine(tmp_path, "AuthenticAMD", "AMD Ryzen Z1 Extreme w/ Radeon 780M Graphics", "ROG Ally RC71L_RC71L")
    _gpu(tmp_path, 0, "0000:c4:00.0", "0x1002", "amdgpu", boot=True, vram=4096 - 1, gc=(11, 0))
    hw = detect_hardware(tmp_path, env={}, names={})
    gpu = hw.game_gpu()
    assert hw.device == "ROG Ally" and gpu is not None and gpu.integrated and gpu.generation == "rdna3"


def test_intel_igpu_plus_arc(tmp_path: Path) -> None:
    _machine(tmp_path, "GenuineIntel", "Intel(R) Core(TM) Ultra 7 155H")
    _gpu(tmp_path, 0, "0000:00:02.0", "0x8086", "i915", boot=True)
    _gpu(tmp_path, 1, "0000:03:00.0", "0x8086", "xe", boot=False)
    hw = detect_hardware(tmp_path, env={}, names={})
    igpu, arc = hw.game_gpu(False), hw.game_gpu(True)
    assert igpu is not None and igpu.integrated and igpu.generation == "xe"
    assert arc is not None and not arc.integrated and arc.generation == "arc" and arc.driver == "xe"


def test_gtx_on_nouveau(tmp_path: Path) -> None:
    _machine(tmp_path, "GenuineIntel", "Intel(R) Core(TM) i7-7700")
    _gpu(tmp_path, 0, "0000:01:00.0", "0x10de", "nouveau", boot=True)
    hw = detect_hardware(tmp_path, env={}, names={"0000:01:00.0": "GP106 [GeForce GTX 1060 6GB]"})
    gpu = hw.game_gpu()
    assert gpu is not None and gpu.driver == "nouveau" and gpu.generation == "gtx" and not gpu.proprietary_nvidia
    assert hw.nvidia_major == 0


def test_nothing_readable_is_not_an_error(tmp_path: Path) -> None:
    hw = detect_hardware(tmp_path, env={}, names={})
    assert hw.gpus == () and hw.game_gpu() is None and not hw.is_hybrid and hw.cpu_vendor == "other"


def test_this_computer_card_says_what_holds_games_back() -> None:
    from PySide6.QtWidgets import QApplication

    from deckinstall.hardware import Gpu, HardwareProfile
    from deckinstall.i18n import tr
    from deckinstall.ui_hardware import describe

    QApplication.instance() or QApplication([])
    igpu = Gpu("intel", "i915", "0000:00:02.0", "UHD 770", True, True, "xe")
    gtx = Gpu("nvidia", "nouveau", "0000:01:00.0", "GTX 1060", False, False, "gtx")
    facts, notes = describe(HardwareProfile("intel", "i7-12700", "", (igpu, gtx), "x11"), (25, 2))
    assert facts[0] == tr("hw.cpu", model="i7-12700") and any("GTX 1060" in line for line in facts)
    assert notes == [
        tr("hw.note_hybrid", primary="UHD 770", dedicated="GTX 1060"),
        tr("hw.note_nouveau"),
        tr("hw.note_intel_mesa", version="25.2"),
    ]
    deck = Gpu("amd", "amdgpu", "a", "Custom GPU 0405", True, True, "rdna2", 1024)
    facts, notes = describe(HardwareProfile("amd", "AMD Custom APU 0932", "Steam Deck", (deck,), "gamescope"), (26, 2))
    assert notes == [] and tr("hw.device", name="Steam Deck") in facts


def test_lspci_names_are_tidied() -> None:
    from deckinstall.hardware import tidy_gpu_name

    assert tidy_gpu_name("Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]") == "Radeon Vega Series"
    assert tidy_gpu_name("Sephiroth [AMD Custom GPU 0405]") == "AMD Custom GPU 0405"
    assert tidy_gpu_name("GA107M [GeForce RTX 3050 Mobile]") == "GeForce RTX 3050 Mobile"
    assert tidy_gpu_name("Arc B580") == "Arc B580"


def test_desktop_card_with_the_igpu_on_is_not_hybrid(tmp_path: Path) -> None:
    _machine(tmp_path, "GenuineIntel", "Intel(R) Core(TM) i5-12400")
    _gpu(tmp_path, 0, "0000:00:02.0", "0x8086", "i915", boot=False)
    _gpu(tmp_path, 1, "0000:01:00.0", "0x10de", "nvidia", boot=True, nvidia_model="NVIDIA GeForce RTX 4070")
    hw = detect_hardware(tmp_path, env={}, names={})
    gpu = hw.game_gpu(True)
    assert not hw.is_hybrid and gpu is not None and gpu.generation == "rtx"  # games stay on the RTX
