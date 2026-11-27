"""Detect and show applicable launch suggestions in the game editor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from .game_fixes import Fix, apply_fixes, fix_file, local_protonfixes, refresh_protonfixes, umu_fixes, valve_fixes
from .hardware import hardware
from .i18n import tr
from .launch_presets import OK, apply_presets, availability, parse_presets, recommend
from .ui_cards import note_label, section_card

if TYPE_CHECKING:
    from .ui import MainWindow
    from .ui_dialogs.launch_presets_dialog import LaunchOptions


@dataclass
class Target:
    """The game the recommendations are for (installed or about to be)."""

    title: str
    steam_appid: int | None
    game_dir: Path | None
    mode: str  # "steam", "standalone" or "external"
    proton: str  # "ge", "cachyos", "valve" or ""
    proton_dir: Path | None
    opts: str
    winedll: str
    prefix_ready: bool  # winetricks needs a prefix the game has already created
    store: str = ""
    codename: str = ""
    exe: Path | None = None  # to read which upscalers the game ships
    dll_in_opts: bool = False  # Steam shortcuts and launchers keep WINEDLLOVERRIDES in the line


@dataclass
class Found:
    presets: dict[str, Any] = field(default_factory=dict)
    valve: list[Fix] = field(default_factory=list)
    umu: list[Fix] = field(default_factory=list)
    umu_file: bool = False
    suggest_optiscaler: bool = False


def gather(target: Target) -> Found:
    """Everything to recommend that the game does not have yet (runs in a worker: reads files,
    may download protonfixes)."""
    from .runtime_manager import list_cachyos_proton_dirs, list_ge_proton11_dirs

    profile = hardware()
    current = parse_presets(target.opts)
    presets = recommend(profile, target.proton)
    upscalers = _game_upscalers(target)
    if upscalers is not None:  # an upgrade only for an upscaler the game ships
        for key, name in _UPSCALER_OF.items():
            if name not in upscalers:
                presets.pop(key, None)
        gpu = profile.game_gpu(bool(presets.get("prime") or current.get("prime")))
        if "FSR" in upscalers and "fsr4_upgrade" not in presets and availability("fsr3_upgrade", gpu, profile,
                                                                                  target.proton)[0] == OK:
            presets["fsr3_upgrade"] = True
    found = Found(presets={k: v for k, v in presets.items() if not current.get(k)})
    found.suggest_optiscaler = _optiscaler_helps(target, upscalers, profile.game_gpu(bool(current.get("prime"))))
    found.valve = valve_fixes(target.proton_dir, target.steam_appid)
    if target.steam_appid or (target.store and target.codename):
        root = local_protonfixes([*list_ge_proton11_dirs(), *list_cachyos_proton_dirs()]) or refresh_protonfixes()
        path = fix_file(root, target.steam_appid, target.store, target.codename) if root is not None else None
        found.umu_file = path is not None
        found.umu = umu_fixes(path, target.game_dir)
    return found


_UPSCALER_OF = {"dlss_upgrade": "DLSS", "fsr4_upgrade": "FSR", "xess_upgrade": "XeSS"}


def _game_upscalers(target: Target) -> tuple[str, ...] | None:
    """Upscalers the game ships ("DLSS", "FSR", "XeSS"), None when the game is not on disk yet."""
    if target.exe is None or not target.exe.is_file():
        return None
    from .optiscaler import check_optiscaler_compatibility

    return check_optiscaler_compatibility(target.exe, target.game_dir).upscalers


def _optiscaler_helps(target: Target, upscalers: tuple[str, ...] | None, gpu: object) -> bool:
    """Only DLSS inside and a GPU that cannot run it: OptiScaler gives it FSR/XeSS instead.
    Never with an anti-cheat (it would see OptiScaler's DLL as cheating)."""
    if not upscalers or upscalers != ("DLSS",) or getattr(gpu, "generation", "") == "rtx":
        return False
    from .classify import detect_anticheat

    return not (target.game_dir is not None and detect_anticheat(target.game_dir))


def _fix_text(fix: Fix) -> str:
    texts = {
        "env": tr("rec.fix_env", value=fix.value),
        "arg": tr("rec.fix_arg", value=fix.value),
        "dll": tr("rec.fix_dll", value=fix.value),
        "winetricks": tr("rec.fix_winetricks", value=fix.value),
        "compat": tr("rec.fix_compat", value=fix.value),
    }
    return texts.get(fix.kind, fix.value)


def _preset_text(key: str) -> str:
    dedicated = hardware().dedicated
    texts = {
        "prime": tr("launch.prime_on_gpu", name=dedicated.name if dedicated is not None else "-"),
        "dlss_upgrade": tr("presets.short_dlss"),
        "fsr4_upgrade": tr("presets.short_fsr4"),
        "xess_upgrade": tr("presets.short_xess"),
        "fsr3_upgrade": tr("presets.short_fsr3"),
    }
    return texts.get(key, key)


def add_game_recommendations(
    win: MainWindow,
    parent: QWidget,
    layout: QVBoxLayout,
    editor: LaunchOptions,
    target: Target,
    install_verb: Callable[[str], None] | None = None,
) -> None:
    """Load suggestions when More options is opened. Each change is explicit."""
    from .ui_workers import SteamOpWorker

    card, actions, _ = section_card(tr("rec.fixes_title"))
    layout.insertWidget(0, card)
    status = note_label(tr("rec.loading"))
    actions.addWidget(status)
    empty_count = actions.count()

    def add_fix(fix: Fix) -> None:
        btn = QPushButton(_fix_text(fix))
        btn.setToolTip(tr("rec.from_valve") if fix.source == "valve" else tr("rec.from_umu"))
        if fix.kind == "winetricks":
            if install_verb is None or not target.prefix_ready:
                btn.setEnabled(False)
                btn.setToolTip(tr("rec.after_first_play"))
            else:
                btn.clicked.connect(lambda: install_verb(fix.value))
        else:
            def apply() -> None:
                opts, dll = apply_fixes(*editor.values(), [fix])
                if target.dll_in_opts:
                    opts = apply_presets(opts, winedlloverrides=dll)
                editor.set_options(opts, dll)
            btn.clicked.connect(apply)
        actions.addWidget(btn)

    def loaded(found: object) -> None:
        if not isValid(parent) or not isinstance(found, Found):
            return
        status.hide()
        for key, value in found.presets.items():
            if key == "prime":  # the shortcut for this suggestion is beside the GPU picker
                continue
            btn = QPushButton(_preset_text(key))

            def refresh(_text: str = "", name: str = key, button: QPushButton = btn) -> None:
                if not isValid(button):
                    return
                current = parse_presets(editor.raw.text())
                profile = hardware()
                gpu = profile.game_gpu(bool(current.get("prime")))
                button.setVisible(availability(name, gpu, profile, target.proton)[0] == OK and not current.get(name))

            editor.raw.textChanged.connect(refresh)
            refresh()
            btn.clicked.connect(
                lambda _checked=False, k=key, v=value: editor.set_options(
                    apply_presets(editor.raw.text(), **{k: v}), editor.dll.text(),
                )
            )
            actions.addWidget(btn)
        if found.suggest_optiscaler:
            actions.addWidget(note_label(tr("rec.optiscaler")))
        for fix in found.valve:
            add_fix(fix)
        if target.mode == "standalone" and found.umu_file:
            actions.addWidget(note_label(tr("rec.umu_automatic")))
        elif found.umu_file and target.proton in ("ge", "cachyos") and target.steam_appid:
            add_fix(Fix("env", f"UMU_ID=umu-{target.steam_appid}", "umu"))
        else:
            for fix in found.umu:
                if fix.kind != "advanced":
                    add_fix(fix)
            if any(f.kind == "advanced" for f in found.umu):
                actions.addWidget(note_label(tr("rec.umu_advanced")))
        if actions.count() == empty_count:
            card.hide()

    def failed(err: str) -> None:
        if isValid(parent):
            status.setText(tr("rec.error", err=err))

    worker = SteamOpWorker(lambda: gather(target))
    worker.read_only = True  # type: ignore[attr-defined]
    win._track_worker(worker)
    worker.finished_ok.connect(loaded)
    worker.failed.connect(failed)
    worker.start()
