"""Unified updates: one check for DeckInstall and its runtimes, applied in a safe order."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QCheckBox

from deckinstall import runtime_manager as rm
from deckinstall import ui, ui_updates, updates


def _app_info(**kw: object) -> SimpleNamespace:
    base = dict(
        current_version="0.27.1", latest_version="0.28.0", has_update=True, can_self_update=True,
        cannot_update_reason="", html_url="https://github.com/murilogiatti/DeckInstall/releases/tag/v0.28.0",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _runtime_data() -> dict:
    return {
        "umu": {"installed": "1.4.3", "latest": "1.4.4", "update_available": True},
        "umu_proton": {"installed": "GE-Proton11-14", "latest": "GE-Proton11-14", "update_available": False},
    }


def test_check_all_merges_every_component_in_display_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "check_app_update", lambda **_k: _app_info())
    monkeypatch.setattr(rm, "check_runtime_updates", lambda force=False: _runtime_data())
    monkeypatch.setattr(rm, "optiscaler_versions", lambda force=False: ("v0.9.4", "v0.9.5"))
    monkeypatch.setattr(rm, "mangohud_versions", lambda force=False: ("v0.8.4", "v0.8.4"))
    res = updates.check_all(("mangohud", "umu", "umu_proton", "optiscaler"))
    assert list(res) == ["app", "umu_proton", "umu", "optiscaler", "mangohud"]
    assert updates.pending(res) == ["app", "umu", "optiscaler"]
    assert res["umu_proton"]["update"] is False and res["mangohud"]["update"] is False
    assert res["app"]["info"].latest_version == "0.28.0"


def test_one_failing_check_never_hides_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(updates, "check_app_update", lambda **_k: boom())
    monkeypatch.setattr(rm, "check_runtime_updates", lambda force=False: _runtime_data())
    res = updates.check_all(("umu",))
    assert res["app"]["error"] == "offline" and not res["app"]["update"]
    assert res["umu"]["update"] and not res["umu"]["error"]
    assert updates.pending(res) == ["umu"]


def test_app_update_that_cannot_apply_here_is_shown_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        updates, "check_app_update", lambda **_k: _app_info(can_self_update=False, cannot_update_reason="not_appimage")
    )
    res = updates.check_all(())
    assert res["app"]["reason"] == "not_appimage" and not res["app"]["update"]
    assert updates.pending(res) == []


def test_missing_runtime_needed_by_standalone_mode_counts_as_install(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"umu": {"installed": None, "latest": "1.4.4", "update_available": False}, "umu_proton": {}}
    monkeypatch.setattr(updates, "check_app_update", lambda **_k: _app_info(has_update=False))
    monkeypatch.setattr(rm, "check_runtime_updates", lambda force=False: data)
    res = updates.check_all(("umu",))
    assert res["umu"]["update"] and res["umu"]["installed"] == ""


def test_cached_github_answer_marks_the_component_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm, "_read_github_cache", lambda url: {"etag": "x", "body": {"tag_name": "v0.9.5"}})
    monkeypatch.setattr(rm.time, "sleep", lambda _s: None)

    def offline(*_a: object, **_k: object) -> None:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(rm, "_request_github_with_validated_redirects", offline)
    monkeypatch.setattr(rm, "optiscaler_downloaded", lambda: False)
    monkeypatch.setattr(updates, "check_app_update", lambda **_k: _app_info(has_update=False))
    res = updates.check_all(("optiscaler",))
    assert res["optiscaler"]["latest"] == "v0.9.5" and res["optiscaler"]["stale"]
    assert not res["app"]["stale"]  # tracking is per component (per thread)


def test_optiscaler_versions_reads_the_cache_and_force_asks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(rm, "optiscaler_downloaded", lambda: True)
    monkeypatch.setattr(rm, "_fetch_json", lambda url: calls.append(url) or {"tag_name": "v0.9.5"})
    rm._update_versions(lambda d: d.update(optiscaler_installed_version="v0.9.4"))
    assert rm.optiscaler_versions() == ("v0.9.4", "v0.9.5")
    assert rm.optiscaler_versions() == ("v0.9.4", "v0.9.5") and len(calls) == 1  # 24 h cache
    rm.optiscaler_versions(force=True)
    assert len(calls) == 2


def test_auto_check_runs_once_a_day() -> None:
    now = 1_000_000.0
    assert updates.auto_check_due(0, now)
    assert not updates.auto_check_due(now - 3600, now)
    assert updates.auto_check_due(now - updates.AUTO_CHECK_INTERVAL, now)
    assert updates.auto_check_due(now + 3600, now)  # clock went back: check again


def _flow_win(selected: dict[str, bool]) -> mock.Mock:
    win = mock.Mock(_steam_busy=False, _updates_checking=False, _closing_pending=False)
    win._update_results = {
        "app": {**updates._entry("0.27.1", "0.28.0", True), "info": _app_info()},
        "umu": updates._entry("1.4.3", "1.4.4", True),
    }
    win._update_checks = {k: mock.Mock(isChecked=mock.Mock(return_value=v)) for k, v in selected.items()}
    return win


def test_apply_runs_runtimes_first_then_the_appimage_then_one_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_updates, "check_updates", mock.Mock())
    win = _flow_win({"app": True, "umu": True})
    ui_updates.apply_selected_updates(win)
    assert win._update_runtimes.call_args.args[0] == ("umu",)
    win._download_app_update.assert_not_called()  # the AppImage waits for the runtimes
    win._update_runtimes.call_args.kwargs["on_finished"]("Runtimes updated: umu-run", True)
    info, on_done = win._download_app_update.call_args.args
    assert info.latest_version == "0.28.0"
    on_done(Path("/apps/DeckInstall.AppImage"), "")
    _info, path, summary = win._after_app_update.call_args.args
    assert path == Path("/apps/DeckInstall.AppImage")
    assert "umu-run" in summary and "0.28.0" in summary
    ui_updates.check_updates.assert_not_called()  # restart pending: nothing to re-check yet


def test_apply_runtimes_only_ends_with_a_message_not_a_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_updates, "check_updates", mock.Mock())
    win = _flow_win({"app": False, "umu": True})
    ui_updates.apply_selected_updates(win)
    win._update_runtimes.call_args.kwargs["on_finished"]("Runtimes updated: umu-run", True)
    win._download_app_update.assert_not_called()
    text, level = win.message_bar.show_message.call_args.args
    assert "umu-run" in text and level == "success"
    ui_updates.check_updates.assert_called_once()


def test_failed_appimage_download_is_reported_with_the_runtime_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_updates, "check_updates", mock.Mock())
    win = _flow_win({"app": True, "umu": False})
    ui_updates.apply_selected_updates(win)
    win._update_runtimes.assert_not_called()
    win._download_app_update.call_args.args[1](None, "SHA256 mismatch")
    text, level = win.message_bar.show_message.call_args.args
    assert "SHA256 mismatch" in text and level == "warning"
    win._after_app_update.assert_not_called()


def test_auto_check_respects_the_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    check = mock.Mock()
    monkeypatch.setattr(ui_updates, "check_updates", check)
    with tempfile.TemporaryDirectory() as tmp:
        settings = QSettings(str(Path(tmp) / "s.ini"), QSettings.Format.IniFormat)
        win = mock.Mock(settings=settings, _closing_pending=False)
        monkeypatch.setenv(ui_updates.NO_CHECK_ENV, "1")
        ui_updates.auto_check_updates(win)
        check.assert_not_called()
        monkeypatch.delenv(ui_updates.NO_CHECK_ENV)
        settings.setValue("updates/auto_check", False)
        ui_updates.auto_check_updates(win)
        check.assert_not_called()
        settings.setValue("updates/auto_check", True)
        settings.setValue("updates/last_check", time.time())
        ui_updates.auto_check_updates(win)
        check.assert_not_called()  # checked today
        settings.setValue("updates/last_check", 0)
        ui_updates.auto_check_updates(win)
        check.assert_called_once_with(win, silent=True)


def _window(app: QApplication, tmp: str) -> ui.MainWindow:
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(Path(tmp) / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "list_standalone_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
    ):
        return ui.MainWindow(app)


def test_card_rows_badge_and_banner_follow_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = {
        "app": {**updates._entry("0.27.1", "0.28.0", True), "info": _app_info()},
        "umu_proton": updates._entry("GE-Proton11-14", "GE-Proton11-14", False),
        "umu": updates._entry("1.4.3", "1.4.4", True),
    }
    monkeypatch.setattr(ui_updates, "_runtimes_in_use", lambda win: ("umu", "umu_proton"))
    monkeypatch.setattr(ui_updates, "UpdatesCheckWorker", lambda runtimes: _FakeWorker(results))
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)
        try:
            ui_updates.check_updates(win, silent=True)
            app.processEvents()
            assert win.nav_tabs.tabText(ui.nav_tab_of(ui.PAGE_RUNTIMES)).endswith("(2)")
            assert win._section_buttons[ui.PAGE_RUNTIMES].text().endswith("(2)")
            assert win.btn_update_runtimes.isEnabled() and win.btn_update_runtimes.text().endswith("(2)")
            boxes = {c: w for c, w in win._update_checks.items() if isinstance(w, QCheckBox)}
            assert boxes["app"].isChecked() and not boxes["umu_proton"].isEnabled()
            assert not win.message_bar.isHidden() and "DeckInstall" in win.message_bar.text()
            assert not win.btn_updates_check.isEnabled()  # a minute before asking GitHub again
            boxes["app"].setChecked(False)
            boxes["umu"].setChecked(False)
            assert not win.btn_update_runtimes.isEnabled()
        finally:
            assert ui._wait_for_workers(app, win, 10.0)
            win.close()
            app.processEvents()


class _FakeWorker:
    """Stands in for UpdatesCheckWorker: answers at start(), on the UI thread."""

    def __init__(self, results: dict) -> None:
        self._results = results
        self.checked = mock.Mock()
        self.finished = mock.Mock()

    def setParent(self, _parent: object) -> None:
        pass

    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:
        pass

    def start(self) -> None:
        self.checked.connect.call_args.args[0](self._results)
        for call in self.finished.connect.call_args_list:  # MainWindow's cleanup
            call.args[0]()


def test_app_check_uses_the_cached_release_when_github_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"tag_name": "v99.0.0", "assets": [], "html_url": "https://github.com/murilogiatti/DeckInstall/releases"}
    monkeypatch.setattr(rm, "_read_github_cache", lambda url: {"etag": "x", "body": body})
    monkeypatch.setattr(rm.time, "sleep", lambda _s: None)

    def offline(*_a: object, **_k: object) -> None:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(rm, "_request_github_with_validated_redirects", offline)
    res = updates.check_all(())
    assert res["app"]["latest"] == "99.0.0" and res["app"]["stale"] and not res["app"]["error"]
    assert res["app"]["reason"] == "not_appimage"  # tests run from source, not an AppImage


def test_errors_are_classified_for_friendly_text() -> None:
    resp = requests.Response()
    resp.status_code = 403
    assert updates.error_kind(requests.HTTPError("403 rate limit", response=resp)) == "rate_limit"
    assert updates.error_kind(requests.ConnectionError("down")) == "offline"
    assert updates.error_kind(RuntimeError("odd")) == "odd"
