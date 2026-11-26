"""What this computer is, for choosing launch options: CPU, every GPU (vendor, driver, generation,
integrated or dedicated), the device (Steam Deck, ROG Ally...) and the display session.

Everything is read from sysfs/procfs (no root, no extra tools); names come from lspci when it is
there. `root` lets tests point at a fake tree. x86_64 only for now (ARM is out of scope).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

AMD, NVIDIA, INTEL, OTHER = "amd", "nvidia", "intel", "other"
_VENDORS = {"0x1002": AMD, "0x10de": NVIDIA, "0x8086": INTEL}
_INTEL_IGPU_SLOT = "0000:00:02.0"  # where Intel puts its integrated GPU on every chipset
_APU_MAX_VRAM = 4 << 30  # an AMD APU carve-out is small; a dedicated card has its own memory

# Generations, as the launch-option rules use them.
GCN, RDNA1, RDNA2, RDNA3, RDNA4 = "gcn", "rdna1", "rdna2", "rdna3", "rdna4"
RTX, GTX = "rtx", "gtx"  # RTX: tensor cores (DLSS); GTX: every older or smaller NVIDIA
ARC, XE = "arc", "xe"  # Intel dedicated (Arc A/B) and integrated (Xe, also "Arc Graphics" iGPUs)

# DMI product name or version -> handheld, for what a small screen and battery change.
_HANDHELDS = (
    (re.compile(r"\b(Jupiter|Galileo)\b"), "Steam Deck"),
    (re.compile(r"ROG Ally|RC71L|RC72L"), "ROG Ally"),
    (re.compile(r"Legion Go"), "Legion Go"),
    (re.compile(r"Claw\b"), "MSI Claw"),
)


@dataclass(frozen=True)
class Gpu:
    vendor: str
    driver: str  # amdgpu, nvidia, nouveau, i915, xe...
    pci: str
    name: str
    integrated: bool
    primary: bool  # the GPU the system boots on (boot_vga): games run here unless sent elsewhere
    generation: str
    vram_mb: int = 0

    @property
    def proprietary_nvidia(self) -> bool:
        return self.vendor == NVIDIA and self.driver == "nvidia"


@dataclass(frozen=True)
class HardwareProfile:
    cpu_vendor: str
    cpu_model: str
    device: str  # "Steam Deck", "ROG Ally"... or ""
    gpus: tuple[Gpu, ...]
    session: str  # "gamescope", "wayland" or "x11"
    nvidia_driver: str = ""  # "595.99.02" when the NVIDIA driver is loaded

    @property
    def primary(self) -> Gpu | None:
        return next((g for g in self.gpus if g.primary), self.gpus[0] if self.gpus else None)

    @property
    def dedicated(self) -> Gpu | None:
        """The dedicated GPU a hybrid machine can send a game to (PRIME / DRI_PRIME). None when the
        main GPU is already the dedicated one (a desktop with its iGPU on): never offload to an iGPU."""
        primary = self.primary
        return next((g for g in self.gpus if g is not primary and not g.integrated), None)

    @property
    def is_hybrid(self) -> bool:
        return self.dedicated is not None

    @property
    def nvidia_major(self) -> int:
        match = re.match(r"(\d+)", self.nvidia_driver)
        return int(match.group(1)) if match else 0

    def game_gpu(self, dedicated: bool = False) -> Gpu | None:
        """The GPU a game runs on: the dedicated one when sent there (PRIME), else the primary."""
        return (self.dedicated if dedicated else None) or self.primary


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _amd_generation(device: Path) -> str:
    """From the graphics core version the kernel reports (IP discovery, RDNA1 and newer)."""
    major, minor = _read(device / "ip_discovery/die/0/GC/0/major"), _read(device / "ip_discovery/die/0/GC/0/minor")
    if not major.isdigit():
        return GCN  # no IP discovery: older than RDNA
    gc = int(major)
    if gc >= 12:
        return RDNA4
    if gc == 11:
        return RDNA3
    if gc == 10:
        return RDNA2 if minor.isdigit() and int(minor) >= 3 else RDNA1
    return GCN


def _nvidia_model(root: Path, pci: str) -> str:
    for line in _read(root / "proc/driver/nvidia/gpus" / pci / "information").splitlines():
        if line.startswith("Model:"):
            return line.split(":", 1)[1].strip()
    return ""


def _cpu(root: Path) -> tuple[str, str]:
    vendor, model = "", ""
    for line in _read(root / "proc/cpuinfo").splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "vendor_id" and not vendor:
            vendor = {"AuthenticAMD": AMD, "GenuineIntel": INTEL}.get(value.strip(), OTHER)
        elif key == "model name" and not model:
            model = value.strip()
        if vendor and model:
            break
    return vendor or OTHER, model


def _device(root: Path) -> str:
    dmi = root / "sys/class/dmi/id"
    text = " ".join(_read(dmi / name) for name in ("product_name", "product_version", "product_family"))
    return next((label for pattern, label in _HANDHELDS if pattern.search(text)), "")


def _session(env: Mapping[str, str]) -> str:
    if "gamescope" in env.get("XDG_CURRENT_DESKTOP", "").lower() or env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return "gamescope"
    if env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    return "x11"


def tidy_gpu_name(name: str) -> str:
    """lspci's 'Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]' -> 'Radeon Vega Series'."""
    if match := re.search(r"\[([^\]]+)\]", name):
        name = match.group(1)
    return name.split(" / ")[0].strip()


def _lspci_names() -> dict[str, str]:
    from .hwnames import _gpus

    return {pci: tidy_gpu_name(model) for pci, model in _gpus()}


def detect_hardware(
    root: Path = Path("/"),
    env: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
) -> HardwareProfile:
    """Read the profile (fast: files only, plus one lspci for names when `names` is None)."""
    cpu_vendor, cpu_model = _cpu(root)
    apu_cpu = cpu_vendor == AMD and bool(re.search(r"radeon|graphics|\bapu\b", cpu_model, re.I))
    device = _device(root)
    gpu_names = dict(names) if names is not None else _lspci_names()
    gpus: list[Gpu] = []
    seen: set[str] = set()
    drm = root / "sys/class/drm"
    try:
        cards = sorted(p for p in drm.iterdir() if re.fullmatch(r"card\d+", p.name))
    except OSError:
        cards = []
    for card in cards:
        dev = card / "device"
        vendor = _VENDORS.get(_read(dev / "vendor").lower(), OTHER)
        try:
            pci = os.path.basename(os.path.realpath(dev))
            driver = os.path.basename(os.path.realpath(dev / "driver")) if (dev / "driver").exists() else ""
        except OSError:
            continue
        if pci in seen or vendor == OTHER:
            continue
        seen.add(pci)
        vram = _read(dev / "mem_info_vram_total")
        vram_bytes = int(vram) if vram.isdigit() else 0
        name = gpu_names.get(pci, "")
        if vendor == AMD:
            generation = _amd_generation(dev)
            integrated = (apu_cpu or bool(device)) and 0 < vram_bytes < _APU_MAX_VRAM
        elif vendor == NVIDIA:
            name = _nvidia_model(root, pci) or name
            generation = RTX if re.search(r"\bRTX\b", name) else (GTX if name else "")
            integrated = False
        else:
            integrated = pci == _INTEL_IGPU_SLOT
            generation = XE if integrated else ARC
        gpus.append(Gpu(
            vendor=vendor,
            driver=driver,
            pci=pci,
            name=name,
            integrated=integrated,
            primary=_read(dev / "boot_vga") == "1",
            generation=generation,
            vram_mb=vram_bytes >> 20,
        ))
    nvidia_driver = _read(root / "sys/module/nvidia/version")
    return HardwareProfile(
        cpu_vendor=cpu_vendor,
        cpu_model=cpu_model,
        device=device,
        gpus=tuple(gpus),
        session=_session(env if env is not None else os.environ),
        nvidia_driver=nvidia_driver,
    )


@lru_cache(maxsize=1)
def hardware() -> HardwareProfile:
    """The profile of this computer, read once per run (GPUs do not change while it runs)."""
    return detect_hardware()


@lru_cache(maxsize=1)
def mesa_version() -> tuple[int, int] | None:
    """Mesa (major, minor) from vulkaninfo, or None (no vulkaninfo, no Mesa driver). Slow: ~0.5 s."""
    try:
        out = subprocess.run(
            ["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Mesa (\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None
