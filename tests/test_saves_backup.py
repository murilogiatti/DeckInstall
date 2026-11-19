"""Phase 6.1: automatic snapshots (dedup, retention, after play), safe restore, Ludusavi import/export."""

from __future__ import annotations

import hashlib
import os
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import ludusavi as lds  # noqa: E402
from deckinstall import save_sync, standalone  # noqa: E402

SAVE_REL = "drive_c/users/steamuser/Documents/My Game/slot1.sav"


def _prefix(root: Path, content: bytes = b"v1", rel: str = SAVE_REL) -> tuple[Path, Path]:
    prefix = root / "pfx"
    save = prefix / rel
    save.parent.mkdir(parents=True, exist_ok=True)
    save.write_bytes(content)
    return prefix, save


def _zips(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.zip"))


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324


# --- automatic snapshots -------------------------------------------------------------------------


def test_auto_snapshot_dedups_unchanged_saves_and_keeps_newest(tmp_path: Path) -> None:
    prefix, save = _prefix(tmp_path)
    snaps = tmp_path / "snaps"
    first = save_sync.auto_snapshot(prefix, "My Game", "proton-change", keep=2, dest_root=snaps)
    again = save_sync.auto_snapshot(prefix, "My Game", "optiscaler", keep=2, dest_root=snaps)
    assert first is not None and again == first
    game_dir = save_sync.snapshot_dir_for("My Game", snaps)
    assert _zips(game_dir) == [first]

    os.utime(first, (1000, 1000))  # distinct mtimes: rotation keeps the newest by mtime
    made = [first]
    for idx, content in enumerate((b"v2", b"v3")):
        save.write_bytes(content)
        snap = save_sync.auto_snapshot(prefix, "My Game", f"op{idx}", keep=2, dest_root=snaps)
        assert snap is not None and snap not in made
        os.utime(snap, (2000 + idx, 2000 + idx))
        made.append(snap)
    assert set(_zips(game_dir)) == set(made[1:])  # retention: the 2 newest per game


def test_no_saves_means_no_snapshot(tmp_path: Path) -> None:
    (tmp_path / "pfx" / "drive_c").mkdir(parents=True)
    assert save_sync.auto_snapshot(tmp_path / "pfx", "Empty", "manual", dest_root=tmp_path / "s") is None


def test_snapshot_after_play_once_per_finished_session(tmp_path: Path) -> None:
    prefix, save = _prefix(tmp_path)
    snaps = tmp_path / "snaps"
    log = tmp_path / "logs" / "run.log"
    log.parent.mkdir()
    log.write_text("== DeckInstall: starting My Game\n")  # still running: no exit line yet
    kw = {"dest_root": snaps}
    assert save_sync.snapshot_after_play(prefix, "My Game", log, **kw) is None

    log.write_text("== DeckInstall: starting My Game\n== DeckInstall: exit code 0 at now\n")
    first = save_sync.snapshot_after_play(prefix, "My Game", log, **kw)
    assert first is not None and "after-play" in first.name
    assert save_sync.snapshot_after_play(prefix, "My Game", log, **kw) is None  # same session

    st = log.stat()
    os.utime(log, (st.st_atime, st.st_mtime + 10))  # a new session, saves unchanged
    assert save_sync.snapshot_after_play(prefix, "My Game", log, **kw) == first
    assert len(_zips(first.parent)) == 1

    save.write_bytes(b"after level 2")
    os.utime(log, (st.st_atime, st.st_mtime + 20))
    second = save_sync.snapshot_after_play(prefix, "My Game", log, **kw)
    assert second is not None and second != first


def test_restore_takes_a_safety_snapshot_first(tmp_path: Path) -> None:
    prefix, save = _prefix(tmp_path, b"old")
    snaps = tmp_path / "snaps"
    old = save_sync.auto_snapshot(prefix, "My Game", "manual", keep=1, dest_root=snaps)
    assert old is not None
    save.write_bytes(b"new")
    count, safety = save_sync.restore_snapshot_safely(old, prefix, "My Game", dest_root=snaps)
    assert count == 1 and save.read_bytes() == b"old"
    assert safety is not None and safety != old and old.is_file()  # keep=1 did not rotate it away
    with zipfile.ZipFile(safety) as zf:
        assert [zf.read(n) for n in zf.namelist() if n.endswith("slot1.sav")] == [b"new"]


def test_restore_aborts_when_safety_snapshot_fails(tmp_path: Path) -> None:
    prefix, save = _prefix(tmp_path, b"old")
    old = save_sync.auto_snapshot(prefix, "My Game", "manual", dest_root=tmp_path / "s")
    assert old is not None
    save.write_bytes(b"new")
    with mock.patch.object(save_sync, "auto_snapshot", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            save_sync.restore_snapshot_safely(old, prefix, "My Game")
    assert save.read_bytes() == b"new"


def test_relocate_and_uninstall_paths_use_the_central_snapshot() -> None:
    from deckinstall import adopt, migrate, relocate

    assert "auto_snapshot" in adopt._default_snapshot.__code__.co_names
    assert "snapshot_fn" in relocate.relocate_game.__code__.co_varnames
    assert "auto_snapshot" in Path(migrate.__file__).read_text()


# --- Ludusavi ------------------------------------------------------------------------------------


def test_ludusavi_export_import_roundtrip(tmp_path: Path) -> None:
    prefix, save = _prefix(tmp_path, b"progress-42")
    folder, count, left_out = lds.export_ludusavi(prefix, "My: Game", tmp_path / "ludusavi")
    assert folder.name == "My_ Game" and count == 1 and left_out == 0
    mapping = lds.load_mapping(folder)
    assert mapping["name"] == "My: Game" and mapping["drives"] == {"drive-C": "C:"}
    entry = mapping["backups"][0]
    original = "C:/users/steamuser/Documents/My Game/slot1.sav"
    assert entry["name"] == "." and entry["files"][original] == {"hash": _sha1(b"progress-42"), "size": 11}
    assert (folder / "drive-C" / "users" / "steamuser" / "Documents" / "My Game" / "slot1.sav").is_file()

    other = tmp_path / "other" / "pfx"
    (other / "drive_c").mkdir(parents=True)
    res = lds.import_ludusavi(folder, other, "My: Game", home=tmp_path / "home", dest_root=tmp_path / "s")
    assert (res.restored, res.unchanged, res.kept, res.skipped, res.snapshot) == (1, 0, 0, 0, None)
    assert (other / SAVE_REL).read_bytes() == b"progress-42"

    # Importing over different local saves snapshots them first.
    save.write_bytes(b"local-newer")
    res = lds.import_ludusavi(folder, prefix, "My: Game", home=tmp_path / "home", dest_root=tmp_path / "s")
    assert res.restored == 1 and res.snapshot is not None
    assert save.read_bytes() == b"progress-42"
    with zipfile.ZipFile(res.snapshot) as zf:
        assert b"local-newer" in [zf.read(n) for n in zf.namelist()]

    assert lds.import_ludusavi(folder, prefix, "My: Game", home=tmp_path / "home").unchanged == 1
    # Exporting again replaces the earlier Ludusavi backup of the same game.
    assert lds.export_ludusavi(prefix, "My: Game", tmp_path / "ludusavi")[0] == folder
    assert lds.find_game_folder(tmp_path / "ludusavi", "my game") == folder


def _fixture(root: Path, files_yaml: str, extra: Callable[[Path], None] | None = None) -> Path:
    game = root / "backup" / "Fixture Game"
    game.mkdir(parents=True)
    (game / "mapping.yaml").write_text(
        "---\nname: Fixture Game\ndrives:\n  drive-C: \"C:\"\n  drive-0: \"\"\nbackups:\n" + files_yaml
    )
    if extra is not None:
        extra(game)
    return game


def test_ludusavi_fixture_with_differential_zip_child(tmp_path: Path) -> None:
    home = tmp_path / "home" / "deck"
    base_a, base_b, new_a = b"slot-a v1", b"slot-b v1", b"slot-a v2"
    linux_path = f"{home.as_posix()}/.local/share/Fixture/config.ini"
    yaml = f"""\
- name: "."
  when: "2026-01-01T00:00:00Z"
  os: windows
  files:
    C:/Users/Murilo/AppData/Roaming/Fixture/a.sav:
      hash: {_sha1(base_a)}
      size: {len(base_a)}
    "C:/Users/Murilo/AppData/Roaming/Fixture/b.sav":
      hash: {_sha1(base_b)}
      size: {len(base_b)}
    "D:/Games/Fixture/local.sav":
      hash: {_sha1(b"x")}
      size: 1
    "{linux_path}":
      hash: {_sha1(b"ini")}
      size: 3
  registry:
    hash: ~
  children:
  - name: backup-20260102T000000Z
    when: "2026-01-02T00:00:00Z"
    files:
      C:/Users/Murilo/AppData/Roaming/Fixture/a.sav:
        hash: {_sha1(new_a)}
        size: {len(new_a)}
      C:/Users/Murilo/AppData/Roaming/Fixture/b.sav: ~
"""

    def extra(game: Path) -> None:
        roaming = game / "drive-C" / "Users" / "Murilo" / "AppData" / "Roaming" / "Fixture"
        roaming.mkdir(parents=True)
        (roaming / "a.sav").write_bytes(base_a)
        (roaming / "b.sav").write_bytes(base_b)
        (game / "drive-D" / "Games" / "Fixture").mkdir(parents=True)
        (game / "drive-D" / "Games" / "Fixture" / "local.sav").write_bytes(b"x")
        cfg = game / "drive-0" / Path(linux_path.lstrip("/"))
        cfg.parent.mkdir(parents=True)
        cfg.write_bytes(b"ini")
        with zipfile.ZipFile(game / "backup-20260102T000000Z.zip", "w") as zf:
            zf.writestr("drive-C/Users/Murilo/AppData/Roaming/Fixture/a.sav", new_a)

    game = _fixture(tmp_path, yaml, extra)
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    res = lds.import_ludusavi(game, prefix, "Fixture Game", home=home, dest_root=tmp_path / "s")
    roaming = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "Fixture"
    assert (roaming / "a.sav").read_bytes() == new_a  # the differential child wins
    assert not (roaming / "b.sav").exists()  # removed in the child
    assert (home / ".local" / "share" / "Fixture" / "config.ini").read_bytes() == b"ini"
    assert (res.restored, res.skipped, res.backup) == (2, 1, "backup-20260102T000000Z")  # D: skipped


@pytest.mark.parametrize(
    "bad_path",
    [
        "C:/Users/Murilo/../../../../etc/evil.sav",
        "C:/Users/Murilo/./x.sav",
        "relative/evil.sav",
    ],
)
def test_ludusavi_import_rejects_path_traversal(tmp_path: Path, bad_path: str) -> None:
    yaml = f"""\
- name: "."
  files:
    "{bad_path}":
      hash: {_sha1(b"x")}
      size: 1
"""
    game = _fixture(tmp_path, yaml)
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    with pytest.raises(lds.LudusaviError):
        lds.import_ludusavi(game, prefix, "Fixture Game", home=tmp_path / "home")
    assert not (tmp_path / "etc").exists()
    assert [p for p in (prefix / "drive_c").rglob("*") if p.is_file()] == []


def test_ludusavi_import_rejects_unsafe_drive_and_backup_names(tmp_path: Path) -> None:
    game = tmp_path / "g"
    game.mkdir()
    (game / "mapping.yaml").write_text(
        'name: G\ndrives:\n  "../../x": "C:"\nbackups:\n  - name: "."\n    files: {}\n'
    )
    with pytest.raises(lds.LudusaviError):
        lds.import_ludusavi(game, tmp_path / "pfx", "G")
    (game / "mapping.yaml").write_text("name: G\nbackups:\n  - name: ../up\n    files: {}\n")
    with pytest.raises(lds.LudusaviError):
        lds.import_ludusavi(game, tmp_path / "pfx", "G")


def test_ludusavi_import_rejects_hash_mismatch_and_writes_nothing(tmp_path: Path) -> None:
    good, bad = b"good", b"tampered"
    yaml = f"""\
- name: "."
  files:
    "C:/Users/me/Documents/G/ok.sav":
      hash: {_sha1(good)}
      size: {len(good)}
    "C:/Users/me/Documents/G/bad.sav":
      hash: {_sha1(b"original")}
      size: {len(bad)}
"""

    def extra(game: Path) -> None:
        docs = game / "drive-C" / "Users" / "me" / "Documents" / "G"
        docs.mkdir(parents=True)
        (docs / "ok.sav").write_bytes(good)
        (docs / "bad.sav").write_bytes(bad)

    game = _fixture(tmp_path, yaml, extra)
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    with pytest.raises(lds.LudusaviError, match="Hash mismatch"):
        lds.import_ludusavi(game, prefix, "Fixture Game", home=tmp_path / "home")
    assert [p for p in (prefix / "drive_c").rglob("*") if p.is_file()] == []


def test_yaml_writer_reader_roundtrip() -> None:
    data = {
        "name": 'He said "hi" \\ there',
        "drives": {"drive-C": "C:"},
        "backups": [{"name": ".", "files": {"C:/a b/c: d.sav": {"hash": "0" * 40, "size": 0}}, "children": []}],
        "flag": True,
        "none": None,
    }
    assert lds.parse_yaml(lds.dump_yaml(data)) == data


def test_yaml_reads_explicit_and_folded_long_keys() -> None:
    # serde_yaml writes keys longer than ~128 chars as "? key" / ": value", folding at spaces.
    long_dir = "/home/deck/.local/share/Steam/steamapps/compatdata/3212989388/pfx/drive_c/users/steamuser"
    text = (
        "name: Game\n"
        "backups:\n"
        "  - name: .\n"
        "    files:\n"
        f"      ? {long_dir}/AppData/Roaming/GSE\n"
        "        Saves/3489700/playtime.txt\n"
        "      : hash: b6589fc6ab0dc82cf12099d1c2d40ab994e8410c\n"
        "        size: 1\n"
        f"      ? {long_dir}/AppData/Local/SB/Saved/Engine.ini\n"
        "      : hash: 3018403059560e21808cf6e5f3e9575c1ff1bd95\n"
        "        size: 2530\n"
        "    children: []\n"
    )
    files = lds.parse_yaml(text)["backups"][0]["files"]
    assert files == {
        f"{long_dir}/AppData/Roaming/GSE Saves/3489700/playtime.txt": {
            "hash": "b6589fc6ab0dc82cf12099d1c2d40ab994e8410c",
            "size": 1,
        },
        f"{long_dir}/AppData/Local/SB/Saved/Engine.ini": {
            "hash": "3018403059560e21808cf6e5f3e9575c1ff1bd95",
            "size": 2530,
        },
    }


# --- Qt: the Backup/Sync page ---------------------------------------------------------------------


def _wait(app: object, cond: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        app.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.01)


def test_backup_sync_page_lists_games_and_snapshots_now(tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from deckinstall import ui
    from deckinstall.i18n import tr
    from deckinstall.ui_dialogs import save_sync_dialog as ssd

    app = QApplication.instance() or QApplication([])
    game_dir = tmp_path / "sa"
    prefix, _ = _prefix(game_dir)
    sa = standalone.StandaloneGame(
        name="Page Game",
        game_dir=game_dir,
        exe_path=game_dir / "game.exe",
        prefix_dir=prefix,
        proton_name="GE-Proton11",
        steam_appid=None,
        winedlloverrides="",
        launch_options="",
        launch_script=game_dir / "launch.sh",
        desktop_file=tmp_path / "sa.desktop",
    )
    with (
        mock.patch.object(ui, "QSettings", return_value=QSettings(str(tmp_path / "p.ini"), QSettings.Format.IniFormat)),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
        mock.patch.object(ui.MainWindow, "_redetect_steam"),
        mock.patch.object(
            ssd, "list_save_archives", lambda: save_sync.list_save_archives(tmp_path / "x", tmp_path / "legacy")
        ),
        mock.patch.object(save_sync, "DEFAULT_SNAPSHOTS_DIR", tmp_path / "x" / "snapshots"),
    ):
        win = ui.MainWindow(app)
        try:
            assert win.saves_list.columnCount() == 4
            assert win.spin_save_keep.value() == save_sync.SNAPSHOT_KEEP_DEFAULT
            win._apply_shortcuts_data(None, [], [sa], [], "")
            row = next(r for r in range(win.saves_list.rowCount()) if win.saves_list.item(r, 0).text() == "Page Game")
            win.saves_list.setCurrentCell(row, 0)
            assert win.btn_save_snapshot_now.isEnabled()
            assert not win.btn_save_restore_selected.isEnabled()  # nothing to restore yet

            with mock.patch.object(ssd.QMessageBox, "information") as info:
                ssd._snapshot_now(win)
                _wait(app, lambda: win._busy_count == 0 and info.called)
                assert tr("saves.snapshot_done", name="Page Game", path="").split("\n")[0] in info.call_args[0][2]
                row = next(r for r in range(win.saves_list.rowCount()) if win.saves_list.item(r, 0).text() == "Page Game")
                assert win.saves_list.item(row, 2).text() == "1"
                win.saves_list.setCurrentCell(row, 0)
                assert win.btn_save_restore_selected.isEnabled()
        finally:
            for worker in list(win._active_workers):
                worker.wait(5000)
            win.close()
            win.deleteLater()
            app.processEvents()
