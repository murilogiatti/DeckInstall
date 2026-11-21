"""Short CPU/GPU names for the MangoHud overlay (cpu_text / gpu_text).

Names are one token (no spaces or commas) because they end up inside MANGOHUD_CONFIG in the
launch options, e.g. "R5-5600H" or "RTX-3050".
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

_MAX_LEN = 16


def _tidy(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9.+-]+", "-", text).strip("-")
    return text[:_MAX_LEN].rstrip("-")


def shorten_cpu(model: str) -> str:
    """'AMD Ryzen 5 5600H with Radeon Graphics' -> 'R5-5600H'; 'AMD Custom APU 0932' -> 'APU-0932'."""
    name = re.sub(r"\((?:R|TM|tm)\)", " ", model)
    name = re.sub(r"@.*$|\bwith\b.*$|\b\d+-Core Processor\b|\bProcessor\b|\bCPU\b", " ", name, flags=re.I)
    if m := re.search(r"Ryzen\s+(?:AI\s+)?(\d)\s+(?:PRO\s+)?([\w-]+)", name, re.I):
        return _tidy(f"R{m.group(1)}-{m.group(2)}")
    if m := re.search(r"Ryzen\s+Threadripper\s+([\w-]+)", name, re.I):
        return _tidy(f"TR-{m.group(1)}")
    if m := re.search(r"Custom\s+APU\s+(\w+)", name, re.I):
        return _tidy(f"APU-{m.group(1)}")
    if m := re.search(r"Core\s+Ultra\s+(\d)\s+(\w+)", name, re.I):
        return _tidy(f"U{m.group(1)}-{m.group(2)}")
    if m := re.search(r"\b(i[3579])-(\w+)", name, re.I):
        return _tidy(f"{m.group(1).lower()}-{m.group(2)}")
    for word in ("AMD", "Intel", "Genuine", "Core", "Series"):
        name = re.sub(rf"\b{word}\b", " ", name, flags=re.I)
    return _tidy(" ".join(name.split()))


def shorten_gpu(name: str) -> str:
    """'GeForce RTX 3050 Mobile' -> 'RTX-3050'; 'AMD Custom GPU 0932' -> 'GPU-0932'; 'Radeon RX 6600M' -> 'RX-6600M'."""
    name = name.split("/")[-1]
    if m := re.search(r"\b(RTX|GTX|GT|RX)\s*(\d{3,4}\w*)(?:\s+(Ti|Super|XT)\b)?", name, re.I):
        extra = f"-{m.group(3)}" if m.group(3) else ""
        return _tidy(f"{m.group(1).upper()}-{m.group(2)}{extra}")
    if m := re.search(r"Custom\s+GPU\s+(\w+)", name, re.I):
        return _tidy(f"GPU-{m.group(1)}")
    if m := re.search(r"Arc\s+(\w+)", name, re.I):
        return _tidy(f"Arc-{m.group(1)}")
    words = ("AMD", "ATI", "NVIDIA", "Intel", "GeForce", "Mobile", "Laptop", "GPU", "Series", "Graphics", "Corporation")
    for word in words:
        name = re.sub(rf"\b{word}\b", " ", name, flags=re.I)
    return _tidy(" ".join(name.split()))


@lru_cache(maxsize=1)
def cpu_short_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return shorten_cpu(line.split(":", 1)[1].strip())
    except OSError:
        pass
    return ""


def _gpus() -> list[tuple[str, str]]:
    """(PCI address, model) of each display controller from lspci, in bus order."""
    try:
        out = subprocess.run(
            ["lspci", "-D", "-mm"], capture_output=True, text=True, timeout=3, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) >= 3 and re.search(r"VGA|3D|Display", parts[0], re.I):
            gpus.append((line.split(maxsplit=1)[0], parts[2]))
    return gpus


def _is_discrete(model: str) -> bool:
    return bool(re.search(r"RTX|GTX|\bRX\b|Arc|GeForce|Quadro", model, re.I))


def _mangohud_index(pci_addr: str, drm_root: str = "/sys/class/drm") -> str:
    """MangoHud's number for a GPU: its position among the renderD* nodes ("" when not found)."""
    try:
        nodes = sorted(
            (n for n in os.listdir(drm_root) if n.startswith("renderD") and n[7:].isdigit()), key=lambda n: int(n[7:])
        )
    except OSError:
        return ""
    for idx, node in enumerate(nodes):
        if os.path.basename(os.path.realpath(f"{drm_root}/{node}/device")) == pci_addr:
            return str(idx)
    return ""


@lru_cache(maxsize=2)
def game_gpu(prime_offload: bool = False) -> tuple[str, str]:
    """(short name, MangoHud GPU number) of the GPU the game runs on; the number is "" with a single GPU.

    Hybrid laptops render on the integrated GPU unless PRIME offload sends the game to the
    discrete one. MangoHud shows every GPU (GPU0, GPU1...) and puts gpu_text on the first, so the
    number goes to gpu_list to make the overlay follow, and name, the right GPU (pci_dev cannot be
    used: ':' separates options in MANGOHUD_CONFIG).
    """
    gpus = _gpus()
    if not gpus:
        return "", ""
    if len(gpus) == 1:
        return shorten_gpu(gpus[0][1]), ""
    discrete = [g for g in gpus if _is_discrete(g[1])]
    integrated = [g for g in gpus if not _is_discrete(g[1])]
    pool = (discrete or gpus) if prime_offload or not integrated else integrated
    addr, model = pool[0]
    return shorten_gpu(model), _mangohud_index(addr)
