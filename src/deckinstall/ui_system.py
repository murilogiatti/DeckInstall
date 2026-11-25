"""Main-window actions about the system itself: diagnostics, sandboxed Steam access, removing DeckInstall."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMessageBox,
)

from . import __app_name__
from .i18n import tr
from .ui_cards import (
    _dialog_parent,
)

if TYPE_CHECKING:
    from .ui import MainWindow


def copy_diagnostics(win: MainWindow) -> None:
    """Run the --doctor checks in the background and put the text report on the clipboard."""
    from .doctor import collect_report, format_text

    win.btn_copy_diag.setEnabled(False)

    def _done(_ok: bool, text: str) -> None:
        win.btn_copy_diag.setEnabled(True)
        if win._closing_pending:
            return
        QApplication.clipboard().setText(text)
        win.status_label.setText(tr("maint.copy_diagnostics_done"))

    def _err(err: str) -> None:
        win.btn_copy_diag.setEnabled(True)
        if not win._closing_pending:
            QMessageBox.warning(_dialog_parent(win), __app_name__, err)

    win._run_pkexec(lambda: (True, format_text(collect_report())), on_result=_done, on_error=_err)


def offer_steam_sandbox_access(win: MainWindow, steam_root: Path, folders: list[Path]) -> None:
    """Flatpak/Snap Steam cannot open these game folders: offer the fix once per folder."""
    from .steam_paths import STEAM_FLATPAK, steam_variant
    from .steam_sandbox import SNAP_FIX_COMMAND, grant_flatpak_access

    raw = win.settings.value("steam_sandbox/asked", [])
    # QSettings hands back a one-item list as a plain string.
    asked = {str(p) for p in raw if p} if isinstance(raw, (list, tuple)) else ({str(raw)} if raw else set())
    new = [p for p in folders if str(p) not in asked]
    if not new or getattr(win, "_sandbox_prompt_open", False):
        return
    win.settings.setValue("steam_sandbox/asked", sorted(asked | {str(p) for p in new}))
    listing = "\n".join(f"• {p}" for p in new)
    if steam_variant(steam_root) != STEAM_FLATPAK:
        QMessageBox.information(
            _dialog_parent(win), __app_name__,
            tr("steam_sandbox.snap_body", folders=listing, cmd=SNAP_FIX_COMMAND),
        )
        return
    win._sandbox_prompt_open = True
    try:
        ans = QMessageBox.question(
            _dialog_parent(win), __app_name__, tr("steam_sandbox.flatpak_body", folders=listing),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
    finally:
        win._sandbox_prompt_open = False
    if ans != QMessageBox.StandardButton.Yes:
        return

    def _grant() -> tuple[bool, str]:
        from .system_changes import KIND_FLATPAK_ACCESS, record_change

        ok, out = grant_flatpak_access(new)
        if ok:
            for folder in new:
                record_change(
                    KIND_FLATPAK_ACCESS, str(folder),
                    f"flatpak override --user --nofilesystem={folder} com.valvesoftware.Steam",
                    needs_root=False, detail="Flatpak Steam can open this game folder",
                )
        return ok, out

    def _done(ok: bool, out: str) -> None:
        if win._closing_pending:
            return
        if ok:
            win.notify(tr("steam_sandbox.granted"), "success")
        else:
            QMessageBox.warning(_dialog_parent(win), __app_name__, tr("steam_sandbox.failed", err=out))

    win._run_pkexec(_grant, on_result=_done, on_error=lambda err: _done(False, err))


def remove_everything(win: MainWindow) -> None:
    """Remove the runtimes, settings, caches and recorded system changes, then quit."""
    from . import remove_all as ra
    from .ui_dialogs.remove_all_dialog import RemoveAllDialog

    if win._busy_count or win._play._runs:  # another operation, or a game is running
        QMessageBox.information(_dialog_parent(win), __app_name__, tr("remove_all.busy"))
        return
    parts = ra.plan()
    if not parts:
        QMessageBox.information(_dialog_parent(win), __app_name__, tr("remove_all.nothing"))
        return
    games = len(getattr(win, "_standalone_games", []) or [])
    kept = [str(p) for p in ra.unknown_data_entries()]
    dlg = RemoveAllDialog(parts, games, kept, _dialog_parent(win))
    if dlg.exec() != RemoveAllDialog.DialogCode.Accepted:
        return
    chosen = dlg.chosen()
    quits = any(p.key == ra.APP_DATA for p in chosen)
    win._begin_busy()

    def _job() -> tuple[bool, str]:
        res = ra.execute(chosen)
        lines = [tr("remove_all.done", count=len(res.removed))]
        if res.errors:
            lines.append(tr("remove_all.errors", errors="\n".join(res.errors[:10])))
        if res.root_commands:
            lines.append(tr("remove_all.root_commands", cmds="\n".join(res.root_commands)))
        appimage = ra.running_appimage_hint()
        if appimage and quits:
            lines.append(tr("remove_all.delete_appimage", path=appimage))
        return not res.errors, "\n\n".join(lines)

    def _done(ok: bool, text: str) -> None:
        win._end_busy()
        show = QMessageBox.information if ok else QMessageBox.warning
        show(_dialog_parent(win), __app_name__, text)
        if quits:
            # Settings were removed: leave without QSettings writing them back on the way out.
            from .ui import exit_now

            exit_now(0)
        win._refresh_shortcuts()

    def _err(err: str) -> None:
        win._end_busy()
        QMessageBox.critical(_dialog_parent(win), __app_name__, err)

    win._run_pkexec(_job, on_result=_done, on_error=_err)


def set_steamgriddb_key(win: MainWindow) -> None:
    """Ask for the user's SteamGridDB API key (empty removes it)."""
    from .steamgriddb import api_key, save_api_key

    text, ok = QInputDialog.getText(
        _dialog_parent(win), __app_name__, tr("sgdb.prompt"), QLineEdit.EchoMode.Password, api_key()
    )
    if not ok:
        return
    try:
        save_api_key(text)
    except OSError as exc:
        QMessageBox.warning(_dialog_parent(win), __app_name__, str(exc))
        return
    win.status_label.setText(tr("sgdb.saved") if text.strip() else tr("sgdb.removed"))
