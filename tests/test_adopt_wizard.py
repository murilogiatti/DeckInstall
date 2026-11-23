"""Phase 5 UI: the single adoption wizard (sources -> plan -> run in batch)."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import adopt as ad  # noqa: E402
from deckinstall import library, standalone, ui_workers_adopt  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.ui_dialogs import adopt_wizard as aw  # noqa: E402

EXE = b"MZ" + b"\0" * 128


@pytest.fixture
def app() -> QApplication:
    inst = QApplication.instance() or QApplication([])
    assert isinstance(inst, QApplication)
    return inst


def _wait(app: QApplication, cond: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for the wizard")
        app.processEvents()
        time.sleep(0.01)


def _dispose(app: QApplication, dlg: aw.AdoptWizard) -> None:
    _wait(app, lambda: not dlg.busy())
    dlg.deleteLater()  # parentless dialog: delete it while Qt is still up
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _cand(tmp: Path, kind: str, name: str, **kw: object) -> ad.AdoptionCandidate:
    game_dir = tmp / kind / name
    game_dir.mkdir(parents=True, exist_ok=True)
    exe = game_dir / "game.exe"
    exe.write_bytes(EXE)
    return ad.AdoptionCandidate(kind=kind, name=name, exe=exe, game_dir=game_dir, prefix_dir=None, **kw)  # type: ignore[arg-type]


def _fake_plan(blocked: set[str]) -> Callable[..., ad.AdoptionPlan]:
    def plan(c: ad.AdoptionCandidate, library_root: Path | None = None, options: ad.AdoptOptions | None = None):
        root = library_root or library.internal_library_root()
        assert c.game_dir is not None and c.exe is not None
        dst = root / "Games" / c.name
        return ad.AdoptionPlan(
            candidate=c,
            options=options or ad.AdoptOptions(),
            uid=f"uid-{c.name}",
            name=c.name,
            library_root=root,
            prefixes_root=root / "Prefixes",
            prefix_location="library",
            game_dir=dst,
            exe=dst / "game.exe",
            prefix_dir=root / "Prefixes" / f"uid-{c.name}",
            prefix_action="new",
            moves=[ad.PlannedMove("game", c.game_dir, dst, 5 * 1024 * 1024, c.name != "Cross")],
            bytes_to_copy=5 * 1024 * 1024 if c.name == "Cross" else 0,
            warnings=list(c.warnings) + [ad.W_LAUNCHER_ENTRY_BREAKS],
            blockers=[ad.B_NO_SPACE] if c.name in blocked else [],
        )

    return plan


def _result(plan: ad.AdoptionPlan) -> ad.AdoptionResult:
    g = standalone.StandaloneGame(
        name=plan.name,
        game_dir=plan.game_dir,
        exe_path=plan.exe,
        prefix_dir=plan.prefix_dir or plan.game_dir,
        proton_name="",
        steam_appid=None,
        winedlloverrides="",
        launch_options="",
        launch_script=plan.game_dir / "launch.sh",
        desktop_file=plan.game_dir / "x.desktop",
    )
    return ad.AdoptionResult(game=g, add_to_steam=plan.options.add_to_steam)


def test_wizard_lists_plans_blocks_and_adopts_in_batch(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cands = [
        _cand(tmp_path, ad.HEROIC, "Alpha", proton_hint="GE-Proton9-20"),
        _cand(tmp_path, ad.LUTRIS, "Broken"),
        _cand(tmp_path, ad.HYDRA, "Cross"),
        _cand(tmp_path, ad.STEAM, "Blocked", shortcut_appid=123),
    ]
    monkeypatch.setattr(ui_workers_adopt, "find_adoption_candidates", lambda paths=None: list(cands))
    monkeypatch.setattr(ui_workers_adopt, "plan_adoption", _fake_plan({"Blocked"}))
    calls: list[str] = []

    def fake_adopt(plan: ad.AdoptionPlan, progress_cb=None, snapshot_fn=None, paths=None) -> ad.AdoptionResult:
        calls.append(plan.name)
        assert plan.ok
        for step in (ad.STEP_SNAPSHOT, ad.STEP_MOVE_GAME, ad.STEP_LAUNCHER):
            if progress_cb is not None:
                progress_cb(step)
        if plan.name == "Broken":
            raise ad.AdoptionError("disk error", rolled_back=True)
        return _result(plan)

    monkeypatch.setattr(ui_workers_adopt, "adopt", fake_adopt)
    lib_a, lib_b = tmp_path / "LibA" / "DeckInstall", tmp_path / "LibB" / "DeckInstall"
    dlg = aw.AdoptWizard(mock.Mock(), [("Internal", lib_a), ("SD", lib_b)], steam_writable=False)
    try:
        _wait(app, lambda: bool(dlg.all_candidates()))
        # One group per source kind, games underneath.
        groups = [dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())]
        assert groups[0].startswith("Heroic") and any(g.startswith(tr("adopt.kind_steam")) for g in groups)
        assert len(groups) == 4
        assert not dlg.btn_next.isEnabled()

        dlg.set_checked({c.key for c in cands})
        assert dlg.btn_next.isEnabled()
        dlg.btn_next.click()
        assert dlg.stack.currentIndex() == dlg.PAGE_PLAN
        _wait(app, lambda: all(it.plan is not None for it in dlg.items))
        assert [it.cand.name for it in dlg.items] == ["Alpha", "Broken", "Cross", "Blocked"]

        # Plan with sizes and disks; warnings translated.
        dlg.game_list.setCurrentRow(2)
        assert tr("adopt.other_disk") in dlg.lbl_plan.text()
        assert "5.0 MB" in dlg.lbl_plan.text() or "5 MB" in dlg.lbl_plan.text()
        assert tr("adopt.warn_launcher_entry_breaks") in dlg.lbl_warnings.text()

        # The blocked game shows its blocker and is not counted.
        dlg.game_list.setCurrentRow(3)
        assert tr("adopt.block_no_space") in dlg.lbl_blockers.text()
        assert dlg.steam_box.isVisibleTo(dlg) and dlg.rb_steam_link.isChecked()
        assert not dlg.btn_shortcut_steam.isEnabled() and dlg.btn_shortcut_steam.text() == tr("add.shortcut_steam_linked")
        assert dlg.btn_next.text() == tr("adopt.btn_adopt", count=3)

        # Changing an option replans that game with the new library.
        dlg.game_list.setCurrentRow(0)
        assert dlg.cmb_proton.currentData() == "source"
        assert dlg.btn_shortcut_steam.isEnabled() and dlg.btn_shortcut_steam.isCheckable()
        dlg.btn_shortcut_desktop.click()
        assert dlg.items[0].opts.create_desktop_shortcut and not dlg.items[0].opts.add_to_steam
        # One mode per game: adding it to Steam drops the desktop launcher and says why.
        dlg.btn_shortcut_steam.click()
        assert dlg.items[0].opts.add_to_steam and not dlg.items[0].opts.create_desktop_shortcut
        assert not dlg.btn_shortcut_desktop.isEnabled()
        assert dlg.lbl_targets_note.text() == tr("mode.note_shortcuts_steam")
        dlg.game_list.setCurrentRow(2)  # already linked in Steam: Steam mode too
        assert not dlg.btn_shortcut_desktop.isEnabled() and not dlg.btn_shortcut_desktop.isChecked()
        dlg.game_list.setCurrentRow(0)
        assert dlg.btn_shortcut_steam.isChecked() and not dlg.btn_shortcut_desktop.isChecked()
        dlg.cmb_library.setCurrentIndex(1)
        _wait(app, lambda: dlg.items[0].plan is not None)
        assert dlg.items[0].plan is not None and dlg.items[0].plan.library_root == lib_b

        dlg.btn_next.click()
        assert dlg.stack.currentIndex() == dlg.PAGE_RUN
        _wait(app, lambda: not dlg._running)
        assert calls == ["Alpha", "Broken", "Cross"]  # one after the other, blocked one skipped
        assert [it.status for it in dlg.items] == ["ok", "failed", "ok", "skipped"]
        assert [r.game.name for r in dlg.adopted] == ["Alpha", "Cross"]
        assert [r.add_to_steam for r in dlg.adopted] == [True, False]
        assert dlg.lbl_summary.text().startswith(tr("adopt.summary", ok=2, failed=1, skipped=1))
        assert "disk error" in dlg.lbl_summary.text()
        assert dlg.btn_cancel.isEnabled() and not dlg.btn_next.isVisible()
    finally:
        _dispose(app, dlg)


def test_guessed_folder_needs_confirmation_and_steam_removal_can_be_deferred(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _cand(tmp_path, ad.STEAM, "Guess", shortcut_appid=7, warnings=[ad.W_GAME_ROOT_GUESSED])
    monkeypatch.setattr(ui_workers_adopt, "find_adoption_candidates", lambda paths=None: [c])
    monkeypatch.setattr(ui_workers_adopt, "plan_adoption", _fake_plan(set()))
    note = tr("adopt.steam_running_note")
    dlg = aw.AdoptWizard(None, [("Internal", tmp_path / "Lib")], steam_writable=False, steam_note=note)
    try:
        _wait(app, lambda: bool(dlg.all_candidates()))
        dlg.set_checked({c.key})
        dlg.btn_next.click()
        _wait(app, lambda: dlg.items[0].plan is not None)
        assert dlg.rb_steam_remove.isEnabled() and dlg.lbl_steam_note.text() == note
        assert dlg.chk_confirm_folder.isVisibleTo(dlg)
        assert tr("adopt.confirm_needed") in dlg.lbl_blockers.text()
        assert not dlg.btn_next.isEnabled()
        dlg.chk_confirm_folder.setChecked(True)
        assert dlg.btn_next.isEnabled()
        assert dlg.items[0].opts.remove_from_steam is False
        dlg.rb_steam_remove.click()
        assert dlg.items[0].opts.remove_from_steam is True
    finally:
        _dispose(app, dlg)


def test_real_batch_adoption_of_loose_folders(app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setattr(ui_workers_adopt, "find_adoption_candidates", lambda paths=None: [])
    folders = []
    for name in ("Game One", "Game Two"):
        d = home / "Loose" / name
        d.mkdir(parents=True)
        (d / "game.exe").write_bytes(EXE)
        folders.append(d)
    root = library.internal_library_root()
    dlg = aw.AdoptWizard(None, [("Internal", root)], steam_writable=False)
    try:
        _wait(app, lambda: not dlg.busy())
        for d in folders:
            dlg.add_candidate(ad.candidate_from_folder(d, exe=d / "game.exe"))
        assert [c.kind for c in dlg.selected_candidates()] == [ad.FOLDER, ad.FOLDER]
        dlg.btn_next.click()
        _wait(app, lambda: all(it.plan is not None for it in dlg.items))
        assert all(it.ready for it in dlg.items), [it.plan.blockers for it in dlg.items if it.plan]
        dlg.btn_next.click()
        _wait(app, lambda: not dlg._running)
        assert [it.status for it in dlg.items] == ["ok", "ok"]
        assert {g.name for g in standalone.list_standalone_games()} == {"Game One", "Game Two"}
        assert all(not d.exists() for d in folders)  # moved into the library
        assert (root / "Games" / "Game One" / "launch.sh").is_file()
    finally:
        _dispose(app, dlg)


def test_main_window_has_one_adopt_entry(app: QApplication, tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings

    from deckinstall import ui

    with (
        mock.patch.object(
            ui, "QSettings", return_value=QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
        ),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
    ):
        win = ui.MainWindow(app)
        try:
            assert win.btn_adopt_games.text() == tr("adopt.btn_open")
            for gone in ("act_export_standalone", "act_ext_to_standalone"):
                assert not hasattr(win, gone)
            seen: dict[str, object] = {}

            def fake_open(
                parent, paths, libraries, steam_writable, steam_note="", track_worker=None,
                snapshot_fn=None, steam_add_allowed=True, steamos=False,
            ):
                seen.update(
                    libraries=libraries, writable=steam_writable, note=steam_note, snap=snapshot_fn,
                    add=steam_add_allowed,
                )
                return []

            with (
                mock.patch.object(ui, "open_adopt_wizard", fake_open),
                mock.patch.object(win, "_check_steam_running_async", lambda cb: cb(True)),
            ):
                win.paths = mock.Mock()
                win._update_steam_mode_ui()
                assert win.btn_mode_steam.isEnabled() and win.btn_mode_steam.isChecked()  # Steam is the default
                win._vdf_corrupt = False
                win._open_adopt_wizard()
            assert seen["writable"] is False and seen["note"] == tr("adopt.steam_running_note")
            assert seen["add"] is True
            assert callable(seen["snap"])
            libs = seen["libraries"]
            assert isinstance(libs, list) and libs and all(isinstance(p, Path) for _, p in libs)
        finally:
            win.paths = None
            win.close()
