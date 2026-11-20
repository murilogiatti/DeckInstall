"""Fase 10.1/10.2: Ludusavi import without the game installed, and uninstalled games stay in the table."""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from deckinstall import ludusavi as lds  # noqa: E402
from deckinstall import save_sync  # noqa: E402
from deckinstall.ui_dialogs import save_sync_dialog as ssd  # noqa: E402

SAVE_REL = "drive_c/users/steamuser/Documents/My Game/slot1.sav"


def _backup(tmp_path: Path, name: str = "My Game", data: bytes = b"progress-42") -> Path:
    prefix = tmp_path / "src-pfx"
    (prefix / SAVE_REL).parent.mkdir(parents=True, exist_ok=True)
    (prefix / SAVE_REL).write_bytes(data)
    folder, _, _ = lds.export_ludusavi(prefix, name, tmp_path / "ludusavi")
    return folder


def test_import_without_game_makes_a_restorable_snapshot(tmp_path: Path) -> None:
    folder = _backup(tmp_path)
    snaps = tmp_path / "snaps"
    snap, stored, skipped = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert snap is not None and (stored, skipped) == (1, 0)
    assert snap.parent == save_sync.snapshot_dir_for("My Game", snaps)
    assert save_sync.snapshot_display_name(snap.parent.name, snaps) == "My Game"
    with zipfile.ZipFile(snap) as zf:
        assert zf.read("snap/users/steamuser/Documents/My Game/slot1.sav") == b"progress-42"

    # Later the game is installed: the normal restore (with its safety snapshot) puts the save in place.
    prefix = tmp_path / "new-pfx"
    (prefix / SAVE_REL).parent.mkdir(parents=True)
    (prefix / SAVE_REL).write_bytes(b"fresh-install")
    count, safety = save_sync.restore_snapshot_safely(snap, prefix, "My Game", dest_root=snaps)
    assert count == 1 and safety is not None
    assert (prefix / SAVE_REL).read_bytes() == b"progress-42"
    with zipfile.ZipFile(safety) as zf:
        assert b"fresh-install" in [zf.read(n) for n in zf.namelist()]


def test_import_without_game_checks_hashes(tmp_path: Path) -> None:
    folder = _backup(tmp_path)
    (folder / "drive-C" / "users" / "steamuser" / "Documents" / "My Game" / "slot1.sav").write_bytes(b"tampered!!!")
    with pytest.raises(lds.LudusaviError):
        lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=tmp_path / "snaps")
    assert not list((tmp_path / "snaps").rglob("*.zip"))


def test_list_backup_games(tmp_path: Path) -> None:
    a = _backup(tmp_path, "Alpha")
    b = _backup(tmp_path, "Beta: Two")
    assert lds.list_backup_games(tmp_path / "ludusavi") == [("Alpha", a), ("Beta: Two", b)]
    assert lds.list_backup_games(a) == [("Alpha", a)]
    assert lds.list_backup_games(tmp_path / "missing") == []


def test_uninstalled_game_keeps_its_row_with_real_name(tmp_path: Path) -> None:
    prefix = tmp_path / "pfx"
    (prefix / SAVE_REL).parent.mkdir(parents=True)
    (prefix / SAVE_REL).write_bytes(b"x")
    snaps = tmp_path / "x" / "snapshots"
    save_sync.auto_snapshot(prefix, "Héllo: World", "uninstall", dest_root=snaps)
    archives = save_sync.list_save_archives(tmp_path / "x", tmp_path / "legacy")
    orig = save_sync.DEFAULT_SNAPSHOTS_DIR
    save_sync.DEFAULT_SNAPSHOTS_DIR = snaps
    try:
        rows = ssd.build_save_rows([], archives)  # the game is gone from every list
    finally:
        save_sync.DEFAULT_SNAPSHOTS_DIR = orig
    assert len(rows) == 1 and rows[0].target is None and rows[0].snapshots == 1
    assert rows[0].name == "Héllo: World"


def test_page_imports_backup_of_games_not_installed(tmp_path: Path) -> None:
    import time
    from unittest import mock

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from deckinstall import ui
    from deckinstall.i18n import tr

    app = QApplication.instance() or QApplication([])
    _backup(tmp_path, "Alpha")
    _backup(tmp_path, "Beta")
    snaps = tmp_path / "x" / "snapshots"
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(tmp_path / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
        mock.patch.object(
            ssd, "list_save_archives", lambda: save_sync.list_save_archives(tmp_path / "x", tmp_path / "legacy")
        ),
        mock.patch.object(save_sync, "DEFAULT_SNAPSHOTS_DIR", snaps),
    ):
        win = ui.MainWindow(app)
        try:
            assert win.btn_save_import.isEnabled()  # no game selected: import goes to snapshots
            with (
                mock.patch.object(ssd.QInputDialog, "getItem", return_value=(tr("saves.import_all", count=2), True)),
                mock.patch.object(ssd.QMessageBox, "information") as info,
            ):
                ssd._import_to_snapshots(win, tmp_path / "ludusavi", None)
                deadline = time.monotonic() + 10
                while not info.called and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
            assert info.called
            names = {win.saves_list.item(r, 0).text() for r in range(win.saves_list.rowCount())}
            assert names == {tr("saves.orphan", name="Alpha"), tr("saves.orphan", name="Beta")}
            win.saves_list.setCurrentCell(0, 0)
            assert not win.btn_save_restore_selected.isEnabled()  # restore waits for the game to be installed
        finally:
            for worker in list(win._active_workers):
                worker.wait(5000)
            win.close()
            win.deleteLater()
            app.processEvents()
