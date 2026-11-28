"""Settings > General > System: what DeckInstall detected on this computer and what it means for games."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFormLayout, QLabel

from .hardware import Gpu, HardwareProfile, hardware, mesa_version
from .i18n import tr
from .ui_cards import note_label

if TYPE_CHECKING:
    from .ui import MainWindow


def generation_label(gpu: Gpu) -> str:
    return {
        "gcn": tr("hw.gen_gcn"),
        "rdna1": "RDNA 1",
        "rdna2": "RDNA 2",
        "rdna3": "RDNA 3",
        "rdna4": "RDNA 4",
        "rtx": "RTX",
        "gtx": tr("hw.gen_gtx"),
        "arc": "Arc",
        "xe": "Xe",
    }.get(gpu.generation, "")


def describe(profile: HardwareProfile, mesa: tuple[int, int] | None) -> tuple[list[str], list[str]]:
    """(facts, notes): one line per fact, and what to do about anything that holds games back."""
    facts = [tr("hw.cpu", model=profile.cpu_model or profile.cpu_vendor.upper())]
    if profile.device:
        facts.append(tr("hw.device", name=profile.device))
    for gpu in profile.gpus:
        parts = [tr("hw.integrated") if gpu.integrated else tr("hw.dedicated")]
        if label := generation_label(gpu):
            parts.append(label)
        driver = gpu.driver or "-"
        if gpu.proprietary_nvidia and profile.nvidia_driver:
            driver += f" {profile.nvidia_driver}"
        parts.append(tr("hw.driver", name=driver))
        role = tr("hw.gpu_primary") if gpu is profile.primary else tr("hw.gpu_other")
        facts.append(f"{role}: {gpu.name or gpu.vendor.upper()} ({', '.join(parts)})")
    if not profile.gpus:
        facts.append(tr("hw.no_gpu"))
    session = {"gamescope": tr("hw.session_gamescope"), "wayland": "Wayland", "x11": "X11"}[profile.session]
    facts.append(tr("hw.session", name=session) + (f" · Mesa {mesa[0]}.{mesa[1]}" if mesa else ""))

    notes: list[str] = []
    dedicated = profile.dedicated
    if dedicated is not None and profile.primary is not None:
        notes.append(tr("hw.note_hybrid", primary=profile.primary.name or "-", dedicated=dedicated.name or "-"))
    if any(g.vendor == "nvidia" and g.driver == "nouveau" for g in profile.gpus):
        notes.append(tr("hw.note_nouveau"))
    if any(g.vendor == "intel" for g in profile.gpus) and mesa is not None and mesa < (26, 1):
        notes.append(tr("hw.note_intel_mesa", version=f"{mesa[0]}.{mesa[1]}"))
    return facts, notes


def summary(profile: HardwareProfile) -> str:
    """One short line: CPU, GPUs (the one games run on first) and the session."""
    cpu = (profile.cpu_model or profile.cpu_vendor.upper()).replace(" with Radeon Graphics", "")
    gpus = sorted(profile.gpus, key=lambda gpu: gpu is not profile.primary)
    names = " + ".join(gpu.name or gpu.vendor.upper() for gpu in gpus) or tr("hw.no_gpu")
    session = {"gamescope": tr("hw.session_gamescope"), "wayland": "Wayland", "x11": "X11"}[profile.session]
    return " · ".join(part for part in (profile.device, cpu, names, session) if part)


def build_hardware_rows(win: MainWindow, form: QFormLayout) -> None:
    """"This computer" in the System form: a short line (details in its tooltip) and any warning."""
    win.lbl_hardware = QLabel(tr("runtimes.loading"))
    win.lbl_hardware.setWordWrap(True)
    form.addRow(tr("hw.title"), win.lbl_hardware)
    win.lbl_hardware_notes = note_label(kind="WarningText")
    form.addRow(win.lbl_hardware_notes)
    form.setRowVisible(win.lbl_hardware_notes, False)
    win._hardware_form = form


def refresh_hardware_card(win: MainWindow) -> None:
    """Fill the card in the background (vulkaninfo, for the Mesa version, takes about half a second)."""
    from .ui_workers import SteamOpWorker

    worker = SteamOpWorker(lambda: (hardware(), mesa_version()))
    worker.read_only = True  # type: ignore[attr-defined]
    win._track_worker(worker)

    def _done(result: object) -> None:
        if win._closing_pending or not isinstance(result, tuple):
            return
        facts, notes = describe(*result)
        win.lbl_hardware.setText(summary(result[0]))
        win.lbl_hardware.setToolTip("\n".join(facts))
        win.lbl_hardware_notes.setText("\n".join(notes))
        win._hardware_form.setRowVisible(win.lbl_hardware_notes, bool(notes))

    worker.finished_ok.connect(_done)
    worker.start()
