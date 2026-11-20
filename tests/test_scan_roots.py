"""Default scan folders: only really mounted disks, removable/restorable defaults, caches tied to libraries."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, scanner, standalone  # noqa: E402

MOUNTINFO = "\n".join(
    [
        "22 1 259:2 / / rw,relatime - ext4 /dev/nvme0n1p2 rw",
        "23 22 259:3 / /home rw,relatime - ext4 /dev/nvme0n1p3 rw",
        "24 22 259:1 / /boot/efi rw - vfat /dev/nvme0n1p1 rw",
        "25 22 259:3 /.offload/docker /var/lib/docker rw - ext4 /dev/nvme0n1p3 rw",
        "26 22 0:30 / /run/user/1000 rw - tmpfs tmpfs rw",
        "27 22 0:31 / /mnt/ram rw - tmpfs tmpfs rw",
        "28 22 179:1 / /run/media/deck/SD rw,nosuid - ext4 /dev/mmcblk0p1 rw",
        "29 22 8:17 / /media/murilo/HD rw - ntfs3 /dev/sdb1 rw",
        "30 22 8:33 /@jogos /mnt/jogos rw - btrfs /dev/sdc1 rw",
        "31 22 8:33 /@jogos /mnt/jogos-again rw - btrfs /dev/sdc1 rw",
        "32 22 8:49 / /media/murilo/Meu\\040HD rw - exfat /dev/sdd1 rw",
        "33 22 8:65 / /var/mnt/extra rw - xfs /dev/sde1 rw",
        "34 22 259:3 /deck /home/deck/HD rw - ext4 /dev/nvme0n1p3 rw",
    ]
)


def test_mounted_disks_reads_mountinfo_only() -> None:
    with (
        mock.patch.object(scanner, "_mount_usable", return_value=True),
        mock.patch.object(scanner.Path, "home", return_value=Path("/home/deck")),
    ):
        disks = scanner.mounted_disks(MOUNTINFO)
    assert disks == sorted(
        [
            Path("/media/murilo/HD"),
            Path("/media/murilo/Meu HD"),  # \040 decoded
            Path("/mnt/jogos"),  # the same btrfs subvolume mounted twice counts once
            Path("/run/media/deck/SD"),
            Path("/var/mnt/extra"),
        ],
        key=str,
    )
    assert Path("/run/media/root") not in disks  # a folder under /run/media, but nothing mounted there
    assert Path("/mnt/ram") not in disks  # tmpfs


def test_unreadable_mount_point_is_skipped(tmp_path: Path) -> None:
    ok = tmp_path / "ok"
    locked = tmp_path / "locked"
    (ok / "Games" / "Alpha").mkdir(parents=True)
    locked.mkdir()
    info = f"40 22 8:1 / {ok} rw - ext4 /dev/sdx1 rw\n41 22 8:2 / {locked} rw - ext4 /dev/sdy1 rw\n"
    locked.chmod(0o000)
    try:
        with mock.patch.object(scanner, "_read_mountinfo", return_value=info):
            roots = scanner.default_scan_roots()
    finally:
        locked.chmod(0o700)
    assert ok in roots and ok / "Games" in roots
    assert all(locked not in (r, *r.parents) for r in roots)


def _lib_game(tmp_path: Path, lib: library.Library | None = None) -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / "Indie"
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)


def test_library_game_cache_is_not_orphan_without_scan_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_root = tmp_path / "media"
    (media_root / "SD").mkdir(parents=True)
    monkeypatch.setattr(library, "default_mount_search_roots", lambda: [media_root])
    monkeypatch.setattr(library, "_is_mount", lambda p: p.parent == media_root)
    lib = library.ensure_library(media_root / "SD" / "DeckInstall", label="SD")
    game = _lib_game(tmp_path, lib)
    assert game.cache_dir is not None
    game.cache_dir.mkdir(parents=True, exist_ok=True)
    stray = library.internal_library_root() / "Caches" / "stray"
    stray.mkdir(parents=True)
    # The user's scan folders never decide what is an orphan.
    with mock.patch.object(scanner, "default_scan_roots", side_effect=AssertionError("scan folders used")):
        orphans = {entry.path for entry in standalone.find_orphan_shader_caches()}
    assert stray in orphans
    assert game.cache_dir not in orphans


class _FakeSettingsWin:
    """MainWindow's scan-folder methods on a QWidget with its own QSettings file."""

    def __init__(self, ini: Path) -> None:
        from PySide6.QtCore import QSettings

        from deckinstall import ui

        self.settings = QSettings(str(ini), QSettings.Format.IniFormat)
        self.refreshed = 0
        for name in ("_load_scan_folders", "_scan_folder_set", "_save_scan_folders"):
            setattr(self, name, getattr(ui.MainWindow, name).__get__(self))

    def _update_scan_folders_label(self) -> None:
        pass

    def _refresh_games(self) -> None:
        self.refreshed += 1


def test_scan_folder_defaults_can_be_removed_and_restored(tmp_path: Path) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QTableWidget

    from deckinstall import ui
    from deckinstall.i18n import tr
    from deckinstall.ui_dialogs import configure_game_dialog

    app = QApplication.instance() or QApplication([])
    home = tmp_path / "home"
    disk = tmp_path / "SD"
    home.mkdir()
    disk.mkdir()
    win = _FakeSettingsWin(tmp_path / "settings.ini")
    seen: dict[str, list[str]] = {}

    def paths(tbl: QTableWidget) -> list[str]:
        out = []
        for r in range(tbl.rowCount()):
            item = tbl.item(r, 0)
            assert item is not None
            out.append(str(item.data(Qt.ItemDataRole.UserRole)[1]))
        return sorted(out)

    def fake_exec(dlg: QDialog) -> int:
        dlg.deleteLater()  # parentless dialog: delete it while Qt is still up
        tbl = dlg.findChild(QTableWidget)
        assert tbl is not None
        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}
        seen["fresh"] = paths(tbl)
        home_row = next(r for r in range(tbl.rowCount()) if tbl.item(r, 0).data(Qt.ItemDataRole.UserRole)[1] == str(home))  # type: ignore[union-attr]
        tbl.setCurrentCell(home_row, 0)
        assert buttons[tr("scan_folders.btn_remove")].isEnabled()
        buttons[tr("scan_folders.btn_remove")].click()
        seen["removed"] = paths(tbl)
        disk_item = tbl.item(0, 0)
        assert disk_item is not None
        disk_item.setCheckState(Qt.CheckState.Unchecked)
        seen["after_remove"] = [str(p) for p, _ in win._load_scan_folders()[0]]
        buttons[tr("scan_folders.btn_restore")].click()
        seen["restored"] = paths(tbl)
        return 0

    with (
        mock.patch.object(Path, "home", return_value=home),
        mock.patch.object(ui, "default_scan_roots", return_value=[disk]),
        mock.patch.object(configure_game_dialog, "_dialog_parent", return_value=None),
        mock.patch.object(QDialog, "exec", fake_exec),
    ):
        configure_game_dialog.open_scan_folders_dialog(win)  # type: ignore[arg-type]
        defaults_now = win._load_scan_folders()[0]
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

    assert seen["fresh"] == sorted([str(home), str(disk)])  # no keys yet: home + disks
    assert seen["removed"] == [str(disk)]
    assert seen["after_remove"] == [str(disk)]
    assert seen["restored"] == sorted([str(home), str(disk)])
    assert defaults_now == [(home, True), (disk, True)]  # restore clears removed and disabled
    assert win.settings.value("scan_folders/removed_defaults") == "[]"
    assert win.settings.value("scan_folders/disabled_defaults") == "[]"
    assert win.refreshed >= 2
