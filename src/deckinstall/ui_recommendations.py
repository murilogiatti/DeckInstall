"""Recommendations for this PC and this game: one window, every item with its reason and source.

Sources: this PC (hardware rules), Valve (Proton's own per-game list, given back to shortcuts with
STEAM_COMPAT_CONFIG), umu (protonfixes, translated for any Proton) and ProtonDB (on demand). Nothing
is applied until the user clicks Apply; fixes Valve or umu already tested start ticked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QDialog, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .game_fixes import Fix, apply_fixes, fix_file, local_protonfixes, refresh_protonfixes, umu_fixes, valve_fixes
from .hardware import hardware
from .i18n import tr
from .launch_presets import OK, apply_presets, availability, parse_presets, recommend
from .ui_cards import _dialog_guard, _dialog_parent, dialog_footer, note_label, section_card

if TYPE_CHECKING:
    from .ui import MainWindow


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


def open_recommendations_dialog(
    win: MainWindow,
    target: Target,
    on_apply: Callable[[str, str, list[str]], None],
    parent: QWidget | None = None,
) -> None:
    """Show the recommendations; on Apply, on_apply(new_opts, new_winedll, winetricks_verbs)."""
    from .ui_workers import SteamOpWorker

    dlg = QDialog(parent if parent is not None else _dialog_parent(win))
    dlg.setWindowTitle(tr("rec.title", name=target.title))
    dlg.resize(620, 560)
    alive = _dialog_guard(dlg)
    lay = QVBoxLayout(dlg)
    lay.addWidget(note_label(tr("rec.intro")))
    body = QWidget()
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(0, 0, 4, 0)
    body_lay.setSpacing(12)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(body)
    lay.addWidget(scroll, 1)
    status = note_label(tr("rec.loading"))
    body_lay.addWidget(status)
    choices: list[tuple[QCheckBox, str, object]] = []  # (box, "preset"|"fix", payload)

    def _card(title: str, hint: str) -> QVBoxLayout:
        card, card_lay, _top = section_card(title, hint)
        card_lay.setSpacing(4)
        body_lay.addWidget(card)
        return card_lay

    def _choice(card_lay: QVBoxLayout, text: str, kind: str, payload: object, checked: bool,
                enabled: bool = True, note: str = "") -> None:
        box = QCheckBox(text)
        box.setChecked(checked and enabled)
        box.setEnabled(enabled)
        card_lay.addWidget(box)
        if note:
            card_lay.addWidget(note_label(note))
        choices.append((box, kind, payload))

    def _show(found: Found) -> None:
        if not alive():
            return
        status.setVisible(False)
        pc = _card(tr("rec.pc_title"), tr("rec.pc_hint"))
        if found.presets:
            for key, value in found.presets.items():
                _choice(pc, _preset_text(key), "preset", (key, value), True)
        else:
            pc.addWidget(note_label(tr("rec.pc_none")))
        if found.suggest_optiscaler:
            pc.addWidget(note_label(tr("rec.optiscaler"), "WarningText"))

        game = _card(tr("rec.fixes_title"), tr("rec.fixes_hint"))
        for fix in found.valve:
            _choice(game, _fix_text(fix), "fix", fix, True, note=tr("rec.from_valve"))
        if target.mode == "standalone" and found.umu_file:
            game.addWidget(note_label(tr("rec.umu_automatic")))
        elif found.umu_file and target.proton in ("ge", "cachyos") and target.steam_appid:
            umu = Fix("env", f"UMU_ID=umu-{target.steam_appid}", "umu")
            _choice(game, _fix_text(umu), "fix", umu, True, note=tr("rec.umu_by_id"))
        else:
            for fix in found.umu:
                if fix.kind == "advanced":
                    continue
                verb = fix.kind == "winetricks"
                _choice(game, _fix_text(fix), "fix", fix, True, enabled=not verb or target.prefix_ready,
                        note=tr("rec.from_umu") if not verb or target.prefix_ready else tr("rec.after_first_play"))
            if any(f.kind == "advanced" for f in found.umu):
                game.addWidget(note_label(tr("rec.umu_advanced"), "WarningText"))
        if not found.valve and not found.umu_file:
            game.addWidget(note_label(
                tr("rec.no_fixes") if target.steam_appid or target.codename else tr("rec.no_id")
            ))

        if target.steam_appid:
            pdb = _card("ProtonDB", tr("rec.protondb_hint"))
            lbl = QLabel()
            lbl.setWordWrap(True)
            btn = QPushButton(tr("rec.protondb_btn"))
            link = QPushButton(tr("rec.protondb_open"))
            from .protondb import PAGE_URL

            link.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(PAGE_URL.format(appid=target.steam_appid)))
            )
            pdb.addWidget(lbl)
            pdb.addLayout(dialog_footer(link, left=(btn,)))

            def _ask() -> None:
                from .protondb import summary

                btn.setEnabled(False)
                lbl.setText(tr("rec.loading"))
                job = SteamOpWorker(lambda: summary(int(target.steam_appid or 0)))
                job.read_only = True  # type: ignore[attr-defined]
                win._track_worker(job)

                def _got(result: object) -> None:
                    if not alive():
                        return
                    from .protondb import Summary

                    if not isinstance(result, Summary) or not result.found:
                        lbl.setText(tr("rec.protondb_none"))
                        return
                    text = tr("rec.protondb_tier", tier=result.tier.title(), total=result.total)
                    if result.poor and target.proton == "valve":
                        text += "\n" + tr("rec.protondb_try_ge")
                    lbl.setText(text)

                def _fail(err: str) -> None:
                    if alive():
                        btn.setEnabled(True)
                        lbl.setText(tr("rec.protondb_error", err=err))

                job.finished_ok.connect(_got)
                job.failed.connect(_fail)
                job.start()

            btn.clicked.connect(_ask)
        body_lay.addStretch(1)
        btn_apply.setEnabled(bool(choices))

    def _failed(err: str) -> None:
        if alive():
            status.setText(tr("rec.error", err=err))

    btn_apply = QPushButton(tr("rec.apply"))
    btn_apply.setObjectName("primaryBtn")
    btn_apply.setEnabled(False)
    btn_close = QPushButton(tr("configure.btn_close"))
    btn_close.clicked.connect(dlg.reject)
    lay.addLayout(dialog_footer(btn_apply, btn_close))

    def _apply() -> None:
        changes = {payload[0]: payload[1] for box, kind, payload in choices
                   if box.isChecked() and kind == "preset" and isinstance(payload, tuple)}
        fixes = [payload for box, kind, payload in choices
                 if box.isChecked() and kind == "fix" and isinstance(payload, Fix)]
        opts = apply_presets(target.opts, **changes) if changes else target.opts
        opts, winedll = apply_fixes(opts, target.winedll, fixes)
        if target.dll_in_opts:
            opts = apply_presets(opts, winedlloverrides=winedll)
        verbs = [f.value for f in fixes if f.kind == "winetricks"]
        dlg.accept()
        on_apply(opts, winedll, verbs)

    btn_apply.clicked.connect(_apply)
    worker = SteamOpWorker(lambda: gather(target))
    worker.read_only = True  # type: ignore[attr-defined]
    win._track_worker(worker)
    def _loaded(found: object) -> None:
        if isinstance(found, Found):
            _show(found)

    worker.finished_ok.connect(_loaded)
    worker.failed.connect(_failed)
    worker.start()
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    dlg.exec()
    dlg.deleteLater()
