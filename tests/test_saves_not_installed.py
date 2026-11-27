"""Fase 10.1/10.2: Ludusavi import without the game installed, and uninstalled games stay in the table."""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from unittest import mock

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
    snap, stored, skipped, _ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
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
            for already in (False, True):  # the second import creates nothing
                with (
                    mock.patch.object(
                        ssd.QInputDialog, "getItem", return_value=(tr("saves.import_all", count=2), True)
                    ),
                    mock.patch.object(ssd, "show_import_result") as shown,
                ):
                    ssd._import_to_snapshots(win, tmp_path / "ludusavi", None)
                    deadline = time.monotonic() + 10
                    while not shown.called and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(0.01)
                assert shown.called
                assert [r[3] for r in shown.call_args.args[1]] == [already, already]
            assert len(list(snaps.rglob("*.zip"))) == 2
            names = {win.saves_list.item(r, 0).text() for r in range(win.saves_list.rowCount())}
            assert names == {tr("saves.orphan", name="Alpha"), tr("saves.orphan", name="Beta")}
            table = win.saves_list
            assert win.chk_saves_orphans.isChecked()  # what was just imported is on screen
            assert not any(table.isRowHidden(r) for r in range(table.rowCount()))
            assert win.chk_saves_orphans.text() == tr("saves.show_orphans", count=2)
            win.chk_saves_orphans.setChecked(False)  # not installed: out of the way unless asked for
            assert all(table.isRowHidden(r) for r in range(table.rowCount()))
            assert win.settings.value("saves/show_orphans") in (False, "false")
            win.chk_saves_orphans.setChecked(True)
            win.saves_list.setCurrentCell(0, 0)
            assert not win.btn_save_restore_selected.isEnabled()  # restore waits for the game to be installed
        finally:
            for worker in list(win._active_workers):
                worker.wait(5000)
            win.close()
            win.deleteLater()
            app.processEvents()


def _unix_backup(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    """A Linux-made Ludusavi backup: absolute /home/deck/... paths under drive-0 (as on the Steam Deck)."""
    import hashlib

    folder = tmp_path / "linux-backup" / name
    lines = [f"name: {name}", "drives:", '  drive-0: ""', "backups:", '  - name: "."', '    when: "2025-12-04T15:02:54Z"',
             "    os: linux", "    files:"]
    for original, data in files.items():
        dst = folder / "drive-0" / original.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        lines += [f"      {original}:", f"        hash: {hashlib.sha1(data).hexdigest()}", f"        size: {len(data)}"]  # noqa: S324
    lines += ["    registry:", "      hash: ~", "    children: []"]
    (folder / "mapping.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_import_keeps_unix_paths_steam_cloud_heroic_and_home(tmp_path: Path) -> None:
    deck = "/home/deck"
    folder = _unix_backup(tmp_path, "Mixed", {
        f"{deck}/.local/share/Steam/userdata/6890423/311340/remote/TPP_GAME_DATA": b"steam-cloud",
        f"{deck}/Games/Heroic/Prefixes/default/Mixed/drive_c/users/deck/AppData/Local/Mixed/s.sav": b"heroic",
        f"{deck}/Games/Heroic/Prefixes/default/Mixed/drive_c/Program Files/Mixed/game.dll": b"x",
        f"{deck}/.local/share/Mixed/saves/auto-1.save": b"native",
        f"{deck}/Syncthing/Mixed/game/saves/auto-1.save": b"outside the allowed folders",
        f"{deck}/.local/share/Steam/steamapps/shadercache/1/x.bin": b"cache",
    })
    snaps = tmp_path / "snaps"
    snap, stored, skipped, _ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "other-home", dest_root=snaps)
    assert snap is not None and (stored, skipped) == (3, 3)
    with zipfile.ZipFile(snap) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == {
            "snap/steamcloud/TPP_GAME_DATA": b"steam-cloud",
            "snap/users/steamuser/AppData/Local/Mixed/s.sav": b"heroic",
            "snap/home/.local/share/Mixed/saves/auto-1.save": b"native",
        }

    # Restore into an installed game: Steam cloud and prefix files land, the home file is replaced
    # after the safety snapshot took a copy of it.
    prefix, cloud, home = tmp_path / "pfx", tmp_path / "userdata" / "311340" / "remote", tmp_path / "home"
    (prefix / "drive_c").mkdir(parents=True)
    (home / ".local/share/Mixed/saves").mkdir(parents=True)
    (home / ".local/share/Mixed/saves/auto-1.save").write_bytes(b"mine")
    orig = save_sync._home_root
    save_sync._home_root = lambda: home
    try:
        count, safety = save_sync.restore_snapshot_safely(
            snap, prefix, "Mixed", dest_root=snaps, extra_dirs=(("steamcloud", cloud),), steam_appid=311340
        )
    finally:
        save_sync._home_root = orig
    assert count == 3 and safety is not None
    assert (cloud / "TPP_GAME_DATA").read_bytes() == b"steam-cloud"
    assert (prefix / "drive_c/users/steamuser/AppData/Local/Mixed/s.sav").read_bytes() == b"heroic"
    assert (home / ".local/share/Mixed/saves/auto-1.save").read_bytes() == b"native"
    with zipfile.ZipFile(safety) as zf:
        assert zf.read("snap/home/.local/share/Mixed/saves/auto-1.save") == b"mine"


def test_ludusavi_backup_path_from_config(tmp_path: Path) -> None:
    store = tmp_path / "bk"
    store.mkdir()
    cfg = tmp_path / ".var/app/com.github.mtkennerly.ludusavi/config/ludusavi/config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"roots:\n- store: steam\n  path: /nope\nbackup:\n  path: {store}\n  format:\n    chosen: simple\n")
    assert lds.ludusavi_backup_path(tmp_path) == store
    assert lds.ludusavi_backup_path(tmp_path / "empty") is None


def test_import_falls_back_to_older_full_backup_when_newest_has_no_saves(tmp_path: Path) -> None:
    folder = _unix_backup(tmp_path, "Old", {"/home/deck/.local/share/Steam/userdata/1/2/remote/slot": b"save"})
    text = (folder / "mapping.yaml").read_text(encoding="utf-8").replace("    children: []\n", "    children: []\n", 1)
    text += (
        '  - name: backup-20261102T160642Z\n    when: "2026-11-02T16:06:42Z"\n    os: linux\n    files:\n'
        "      /home/deck/.local/share/Steam/steamapps/compatdata/9/pfx/system.reg:\n"
        "        hash: da39a3ee5e6b4b0d3255bfef95601890afd80709\n        size: 0\n    children: []\n"
    )
    bk = folder / "backup-20261102T160642Z" / "drive-0" / "home/deck/.local/share/Steam/steamapps/compatdata/9/pfx"
    bk.mkdir(parents=True)
    (bk / "system.reg").write_bytes(b"")
    (folder / "mapping.yaml").write_text(text, encoding="utf-8")
    snap, stored, skipped, _ = lds.import_ludusavi_to_snapshot(folder, home=Path("/home/deck"), dest_root=tmp_path / "s")
    assert snap is not None and (stored, skipped) == (1, 0)


def test_import_twice_makes_one_snapshot(tmp_path: Path) -> None:
    folder = _backup(tmp_path)
    snaps = tmp_path / "snaps"
    first, stored, _, already = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert first is not None and stored == 1 and not already
    again, stored2, _, already2 = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert again == first and stored2 == 1 and already2
    assert len(list(first.parent.glob("*.zip"))) == 1


def test_changed_content_makes_a_new_snapshot(tmp_path: Path) -> None:
    snaps = tmp_path / "snaps"
    first, *_ = lds.import_ludusavi_to_snapshot(_backup(tmp_path / "a"), home=tmp_path / "home", dest_root=snaps)
    second, _, _, already = lds.import_ludusavi_to_snapshot(
        _backup(tmp_path / "b", data=b"progress-43"), home=tmp_path / "home", dest_root=snaps
    )
    assert second is not None and second != first and not already
    assert len(list(second.parent.glob("*.zip"))) == 2


def test_old_snapshot_without_fingerprint_is_recognized(tmp_path: Path) -> None:
    folder = _backup(tmp_path)
    snaps = tmp_path / "snaps"
    first, *_ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert first is not None
    index = first.parent / ".index.json"  # as written by versions before the fingerprint
    data = json.loads(index.read_text())
    data.pop("import_fingerprints")
    index.write_text(json.dumps(data))
    again, _, _, already = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert already and again == first
    assert first.name in save_sync.import_fingerprints(first.parent)  # computed once and saved


def test_find_import_duplicates_keeps_the_newest(tmp_path: Path) -> None:
    folder = _backup(tmp_path)
    snaps = tmp_path / "snaps"
    first, *_ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "home", dest_root=snaps)
    assert first is not None
    older = first.parent / "20000101-000000-ludusavi-import.zip"  # an older version imported it before
    older.write_bytes(first.read_bytes())
    assert lds.find_import_duplicates(snaps) == [older]


def test_reimport_of_an_older_state_becomes_the_newest(tmp_path: Path) -> None:
    """A, B, then A again: Restore must bring back A, so the third import is a new snapshot."""
    import os

    snaps = tmp_path / "snaps"
    a = _backup(tmp_path / "a")
    first, *_ = lds.import_ludusavi_to_snapshot(a, home=tmp_path / "home", dest_root=snaps)
    second, *_ = lds.import_ludusavi_to_snapshot(_backup(tmp_path / "b", data=b"progress-43"),
                                                home=tmp_path / "home", dest_root=snaps)
    assert first is not None and second is not None
    os.utime(first, (1, 1))  # first is clearly older
    third, _, _, already = lds.import_ludusavi_to_snapshot(a, home=tmp_path / "home", dest_root=snaps)
    assert third is not None and not already and third not in (first, second)
    assert lds.find_import_duplicates(snaps) == [first]  # the old copy of A goes, the new one stays


def test_import_result_text_counters() -> None:
    summary, details = ssd.import_result_text([("A", 2, 0, False), ("B", 1, 0, True), ("C", 0, 3, False)])
    assert "1" in summary and details.count("\n") == 2
    summary, details = ssd.import_result_text([("A", 2, 0, True), ("B", 1, 0, True)])
    assert details == "" and summary == ssd.tr("saves.import_all_already")


def test_existing_snapshots_match_the_installed_game_by_name(tmp_path: Path) -> None:
    snaps = tmp_path / "snaps"
    first, *_ = lds.import_ludusavi_to_snapshot(_backup(tmp_path, "Returnal™"), home=tmp_path / "h", dest_root=snaps)
    assert first is not None
    archives = save_sync.list_save_archives(tmp_path / "x", tmp_path / "legacy")  # empty roots: nothing
    assert ssd.existing_snapshots_for("Returnal", archives) == []
    entry = save_sync.SaveArchiveEntry(first, "save_snapshot", "", first.parent.name, 1.0, 1)
    with mock.patch.object(save_sync, "DEFAULT_SNAPSHOTS_DIR", snaps):
        assert ssd.existing_snapshots_for("Returnal", [entry]) == [entry]  # "Returnal™" == "Returnal"
        assert ssd.existing_snapshots_for("Other Game", [entry]) == []
        state = save_sync.SaveArchiveEntry(first, "state_snapshot", "", first.parent.name, 2.0, 1)
        assert ssd.existing_snapshots_for("Returnal", [state]) == []  # only save snapshots are offered


def test_offer_existing_saves_restores_the_newest_when_accepted(tmp_path: Path) -> None:
    game = mock.Mock(native_linux=False, prefix_dir=tmp_path / "pfx", game_dir=tmp_path / "g", steam_appid=None)
    game.name = "My Game"
    win = mock.Mock(_steam_busy=False, _closing_pending=False)
    old = save_sync.SaveArchiveEntry(tmp_path / "a.zip", "save_snapshot", "", "my-game", 1.0, 1)
    new = save_sync.SaveArchiveEntry(tmp_path / "b.zip", "save_snapshot", "", "my-game", 9.0, 1)
    with (
        mock.patch.object(ssd, "list_save_archives", return_value=[old, new]),
        mock.patch.object(ssd, "game_log_dir", return_value=tmp_path / "logs"),
        mock.patch.object(ssd, "_dialog_parent", return_value=None),
        mock.patch.object(ssd.QMessageBox, "question", return_value=ssd.QMessageBox.StandardButton.No) as ask,
        mock.patch.object(ssd, "_run") as run,
    ):
        ssd.offer_existing_saves(win, game)
        assert ask.called and not run.called  # declined: nothing restored
        ask.return_value = ssd.QMessageBox.StandardButton.Yes
        with mock.patch.object(ssd, "_restore_job", return_value=(1, None, 0)) as job:
            ssd.offer_existing_saves(win, game)
            run.call_args.args[2]()  # the job given to the worker
            assert job.call_args.args[1] is new


def test_restore_offer_shows_origin_size_file_and_defaults_to_no(tmp_path: Path) -> None:
    from deckinstall.i18n import tr

    game = mock.Mock(native_linux=False, prefix_dir=tmp_path / "pfx", game_dir=tmp_path / "g", steam_appid=1245620)
    game.name = "Elden Ring"
    win = mock.Mock(_steam_busy=False, _closing_pending=False)
    snap = tmp_path / "elden-ring" / "20261126-221500-remove-game.zip"
    entry = save_sync.SaveArchiveEntry(snap, "save_snapshot", "", "elden-ring", 9.0, 3 * 1024 * 1024)
    with (
        mock.patch.object(ssd, "list_save_archives", return_value=[entry]),
        mock.patch.object(ssd, "game_log_dir", return_value=tmp_path / "logs"),
        mock.patch.object(ssd, "_dialog_parent", return_value=None),
        mock.patch.object(ssd.QMessageBox, "question", return_value=ssd.QMessageBox.StandardButton.No) as ask,
        mock.patch.object(ssd, "_run"),
    ):
        ssd.offer_existing_saves(win, game)
    text = ask.call_args.args[2]
    assert tr("saves.origin.remove-game") in text and snap.name in text and "1245620" in text
    assert ask.call_args.args[4] == ssd.QMessageBox.StandardButton.No  # a stray press restores nothing
    assert ssd.snapshot_origin_label("20260101-120000-after-play-2.zip") == tr("saves.origin.after-play")
    assert ssd.snapshot_origin_label("20260101-120000-something-new.zip") == "something-new"
