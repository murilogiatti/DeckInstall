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


# GPU drivers MangoHud reads (gpu.cpp); a GPU on another driver (e.g. nouveau) gets no number.
_MANGOHUD_DRIVERS = frozenset({"amdgpu", "nvidia", "i915", "xe", "panfrost", "panthor", "msm_dpu", "msm_drm"})


def _mangohud_gpus(drm_root: str = "/sys/class/drm") -> list[str]:
    """PCI addresses of the GPUs MangoHud shows, in its order (GPU0, GPU1...: renderD* node order)."""
    try:
        nodes = sorted(n for n in os.listdir(drm_root) if n.startswith("renderD") and n[7:].isdigit())
    except OSError:
        return []
    out: list[str] = []
    for node in nodes:
        try:
            driver_link = f"{drm_root}/{node}/device/driver"
            if not os.path.exists(driver_link):
                continue
            driver = os.path.basename(os.path.realpath(driver_link))
            if driver in _MANGOHUD_DRIVERS:
                out.append(os.path.basename(os.path.realpath(f"{drm_root}/{node}/device")))
        except OSError:
            continue
    return out


@lru_cache(maxsize=1)
def mangohud_gpu_text() -> str:
    """gpu_text for MangoHud: one name per GPU it shows, joined with '+' ("RTX-3050+Radeon-Vega").

    MangoHud shows every GPU (like the Steam Deck overlay shows its one GPU) and takes gpu_text as
    a list, one entry per GPU number; without it a hybrid laptop reads "GPU0 / GPU1".
    """
    models = dict(_gpus())
    order = _mangohud_gpus()
    if not order:  # no sysfs (containers, tests): lspci order is the best guess
        order = list(models)
    names = [shorten_gpu(models.get(addr, "")) or "GPU" for addr in order]
    return "+".join(names)
