"""Updates card (Runtimes tab): DeckInstall and its runtimes checked and updated together.

Order when applying: runtimes first (with their follow-up offers for the games), the AppImage
last, then one summary; only the AppImage needs a restart. A daily background check only tells
(tab counter + message bar); it never downloads.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QDateTime, QLocale, QTimer
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QWidget

from .app_updater import GITHUB_RELEASES_URL
from .i18n import tr
from .ui_cards import _make_card, _setting_bool
from .ui_workers import UpdatesCheckWorker
from .updates import APP, LABELS, MANUAL_CHECK_COOLDOWN, auto_check_due, pending

if TYPE_CHECKING:
    from .ui import MainWindow

AUTO_CHECK_DELAY_MS = 5000  # let the window and the first scans settle first
NO_CHECK_ENV = "DECKINSTALL_NO_UPDATE_CHECK"  # "1": never check by itself (tests, offline setups)
_LAST_CHECK_KEY = "updates/last_check"
_AUTO_KEY = "updates/auto_check"
_BAR_KEY = "updates"


def build_updates_card(win: MainWindow) -> QWidget:
    """Card on top of the Runtimes tab; binds its widgets onto ``win``."""
    card, layout = _make_card()
    head = QHBoxLayout()
    title = QLabel(tr("updates.title"))
    title.setObjectName("SectionTitle")
    win.lbl_updates_status = QLabel(tr("updates.never"))
    win.lbl_updates_status.setObjectName("MutedText")
    win.btn_updates_check = QPushButton(tr("updates.btn_check"))
    win.btn_updates_check.clicked.connect(lambda: check_updates(win))
    head.addWidget(title)
    head.addStretch(1)
    head.addWidget(win.lbl_updates_status)
    head.addWidget(win.btn_updates_check)
    layout.addLayout(head)

    win.updates_rows = QWidget()
    win.updates_grid = QGridLayout(win.updates_rows)
    win.updates_grid.setContentsMargins(0, 0, 0, 0)
    win.updates_grid.setHorizontalSpacing(16)
    win.updates_grid.setVerticalSpacing(6)
    layout.addWidget(win.updates_rows)

    foot = QHBoxLayout()
    win.chk_updates_auto = QCheckBox(tr("updates.auto"))
    win.chk_updates_auto.setChecked(_setting_bool(win.settings, _AUTO_KEY, True))
    win.chk_updates_auto.toggled.connect(lambda on: win.settings.setValue(_AUTO_KEY, on))
    # Keeps the old name: the busy state (and tests) find the Runtimes "update" action by it.
    win.btn_update_runtimes = QPushButton(tr("updates.btn_apply", count=0))
    win.btn_update_runtimes.setObjectName("primaryBtn")
    win.btn_update_runtimes.setEnabled(False)
    win.btn_update_runtimes.clicked.connect(lambda: apply_selected_updates(win))
    foot.addWidget(win.chk_updates_auto)
    foot.addStretch(1)
    foot.addWidget(win.btn_update_runtimes)
    layout.addLayout(foot)

    win._update_results = {}
    win._update_checks = {}
    win._updates_checking = False
    win._updates_cooldown = QTimer(win)
    win._updates_cooldown.setSingleShot(True)
    win._updates_cooldown.timeout.connect(lambda: _sync_check_button(win))
    return card


def _sync_check_button(win: MainWindow) -> None:
    cooling = win._updates_cooldown.isActive()
    win.btn_updates_check.setEnabled(not win._updates_checking and not cooling and not win._steam_busy)
    win.btn_updates_check.setToolTip(tr("updates.wait") if cooling else "")


def _format_when(ts: float) -> str:
    when = QDateTime.fromSecsSinceEpoch(int(ts))
    return QLocale().toString(when, QLocale.FormatType.ShortFormat)


def _reason_text(entry: dict[str, Any]) -> str:
    return {
        "not_appimage": tr("updates.reason_not_appimage"),
        "not_writable": tr("updates.reason_not_writable"),
        "no_asset": tr("updates.reason_no_asset"),
        "no_checksum": tr("updates.reason_no_checksum"),
    }.get(str(entry["reason"]), "")


def _row_texts(comp: str, entry: dict[str, Any]) -> tuple[str, str]:
    """(versions, status) for one row."""
    installed, latest = str(entry["installed"]), str(entry["latest"])
    if entry["error"]:
        err = str(entry["error"])
        text = {"rate_limit": tr("updates.err_rate_limit"), "offline": tr("updates.err_offline")}.get(err)
        return installed or "—", text or tr("updates.failed", err=err[:80])
    if entry["reason"]:
        return f"{installed}  →  {latest}", _reason_text(entry)
    if entry["update"]:
        if not installed:
            return f"—  →  {latest}", tr("updates.will_install")
        return f"{installed}  →  {latest}", tr("updates.available")
    return installed or tr("updates.not_installed"), tr("updates.up_to_date")


def render_updates(win: MainWindow) -> None:
    """Rebuild the rows from win._update_results and sync the counter, badge and button."""
    grid = win.updates_grid
    while grid.count():
        item = grid.takeAt(0)
        if item is not None and (w := item.widget()) is not None:
            w.deleteLater()
    win._update_checks = {}
    for row, (comp, entry) in enumerate(win._update_results.items()):
        check = QCheckBox(LABELS.get(comp, comp))
        can = bool(entry["update"]) and not entry["error"]
        check.setChecked(can)
        check.setEnabled(can)
        check.toggled.connect(lambda _on: _sync_apply_button(win))
        versions, status = _row_texts(comp, entry)
        status_lbl = QLabel(status)
        status_lbl.setObjectName("UpdateBadge" if can else "MutedText")
        status_lbl.setWordWrap(True)
        grid.addWidget(check, row, 0)
        grid.addWidget(QLabel(versions), row, 1)
        grid.addWidget(status_lbl, row, 2)
        if comp == APP and (entry["update"] or entry["reason"]) and entry["info"] is not None:
            url = entry["info"].html_url or GITHUB_RELEASES_URL
            link = QLabel(f'<a href="{url}">{tr("updates.release_notes")}</a>')
            link.setOpenExternalLinks(True)
            grid.addWidget(link, row, 3)
        win._update_checks[comp] = check
    grid.setColumnStretch(2, 1)
    _sync_apply_button(win)
    _sync_badge(win)


def _sync_apply_button(win: MainWindow) -> None:
    count = sum(1 for c in win._update_checks.values() if c.isChecked())
    win.btn_update_runtimes.setText(tr("updates.btn_apply", count=count))
    win.btn_update_runtimes.setEnabled(count > 0 and not win._steam_busy and not win._updates_checking)


def _sync_badge(win: MainWindow) -> None:
    from .ui import PAGE_RUNTIMES, nav_tab_of

    count = len(pending(win._update_results))
    for label, set_text in (
        (tr("nav.settings"), lambda text: win.nav_tabs.setTabText(nav_tab_of(PAGE_RUNTIMES), text)),
        (tr("nav.runtimes"), win._section_buttons[PAGE_RUNTIMES].setText),
    ):
        set_text(tr("updates.tab_badge", label=label, count=count) if count else label)


def _runtimes_in_use(win: MainWindow) -> tuple[str, ...]:
    return win._runtime_components_for_update_all()


def check_updates(win: MainWindow, *, silent: bool = False) -> None:
    """Check everything in the background. silent (daily check): only the counter and a message bar."""
    if win._updates_checking or win._closing_pending:
        return
    win._updates_checking = True
    win.lbl_updates_status.setText(tr("updates.checking"))
    _sync_check_button(win)
    _sync_apply_button(win)
    worker = UpdatesCheckWorker(_runtimes_in_use(win))
    win._track_worker(worker)

    def _done(results: object) -> None:
        win._updates_checking = False
        win._updates_cooldown.start(MANUAL_CHECK_COOLDOWN * 1000)
        _sync_check_button(win)
        if win._closing_pending:
            return
        data = results if isinstance(results, dict) else {}
        now = time.time()
        if data and not all(e["error"] for e in data.values()):
            win.settings.setValue(_LAST_CHECK_KEY, now)
        win._update_results = data
        stale = any(e["stale"] for e in data.values())
        offline = not data or all(e["error"] for e in data.values())
        if offline:
            win.lbl_updates_status.setText(tr("updates.offline_none"))
        elif stale:
            win.lbl_updates_status.setText(tr("updates.offline"))
        else:
            win.lbl_updates_status.setText(tr("updates.checked_at", when=_format_when(now)))
        render_updates(win)
        found = pending(data)
        if silent and found:
            names = ", ".join(LABELS.get(c, c) for c in found)
            win.message_bar.show_message(
                tr("updates.banner", count=len(found), names=names),
                "info",
                actions=((tr("updates.banner_action"), lambda: _open_runtimes(win)),),
                key=_BAR_KEY,
            )
        elif not found:
            win.message_bar.dismiss(_BAR_KEY)

    worker.checked.connect(_done)
    worker.start()


def _open_runtimes(win: MainWindow) -> None:
    from .ui import PAGE_RUNTIMES

    win._show_page(PAGE_RUNTIMES)


def auto_check_updates(win: MainWindow) -> None:
    """Daily background check, a few seconds after start; never when switched off."""
    if os.environ.get(NO_CHECK_ENV, "").strip() == "1" or win._closing_pending:
        return
    if not _setting_bool(win.settings, _AUTO_KEY, True):
        return
    try:
        last = float(str(win.settings.value(_LAST_CHECK_KEY, 0) or 0))
    except (TypeError, ValueError):
        last = 0.0
    if auto_check_due(last):
        check_updates(win, silent=True)


def recheck_if_shown(win: MainWindow) -> None:
    """After a single runtime card updated something, refresh the Updates rows (if already shown)."""
    if getattr(win, "_update_results", None):
        check_updates(win)


def apply_selected_updates(win: MainWindow) -> None:
    """Runtimes first (with their follow-ups for the games), then the AppImage, then one summary."""
    if win._steam_busy or win._updates_checking:
        return
    selected = [comp for comp, check in win._update_checks.items() if check.isChecked()]
    if not selected:
        return
    runtimes = tuple(c for c in selected if c != APP)
    app_entry = win._update_results.get(APP) if APP in selected else None
    lines: list[str] = []
    failed = False

    def _finish(app_info: Any = None, new_path: Any = None, app_err: str = "") -> None:
        nonlocal failed
        if app_err:
            failed = True
            lines.append(tr("app_update.error", err=app_err))
        summary = "\n".join(line for line in lines if line)
        if new_path is not None and app_info is not None:
            win._after_app_update(app_info, new_path, summary)
        elif summary:
            win.message_bar.show_message(summary, "warning" if failed else "success", key=_BAR_KEY)
        if new_path is None:
            check_updates(win)  # rows show what is left (the new AppImage needs a restart first)

    def _app_step() -> None:
        if win._closing_pending:
            return
        if app_entry is None or app_entry["info"] is None:
            _finish()
            return
        info = app_entry["info"]

        def _app_done(path: Any, err: str) -> None:
            if path is not None:
                lines.append(tr("updates.app_done", latest=info.latest_version))
            _finish(info, path, err)

        win._download_app_update(info, _app_done)

    def _runtimes_done(msg: str, ok: bool) -> None:
        nonlocal failed
        failed = failed or not ok
        lines.append(msg)
        _app_step()

    win.message_bar.dismiss(_BAR_KEY)
    if runtimes:
        win._update_runtimes(runtimes, on_finished=_runtimes_done)
    else:
        _app_step()
