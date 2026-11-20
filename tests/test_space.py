"""Fase 4.3: space meter per disk and per game."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from deckinstall import library, space  # noqa: E402
from deckinstall.standalone import StandaloneGame  # noqa: E402


def _game(name: str, game_dir: Path, prefix: Path, uid: str = "") -> StandaloneGame:
    return StandaloneGame(
        name=name,
        game_dir=game_dir,
        exe_path=game_dir / "Game.exe",
        prefix_dir=prefix,
        proton_name="",
        steam_appid=None,
        winedlloverrides="",
        launch_options="",
        launch_script=game_dir / "launch.sh",
        desktop_file=game_dir / "x.desktop",
        uid=uid or name,
    )


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_shared_prefix_counted_once_and_flagged(tmp_path: Path) -> None:
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    pfx = lib.prefixes_dir / "shared"
    _write(pfx / "drive_c" / "save.dat", 1000)
    a = lib.games_dir / "A"
    b = lib.games_dir / "B"
    _write(a / "Game.exe", 100)
    _write(b / "Game.exe", 200)
    report = space.scan_space([lib], [_game("A", a, pfx), _game("B", b, pfx)])
    by_name = {g.name: g for g in report.games}
    assert by_name["A"].shared_prefix and by_name["B"].shared_prefix
    assert by_name["A"].prefix_bytes + by_name["B"].prefix_bytes == 1000
    assert by_name["A"].library == lib.label
    assert len(report.disks) == 1  # everything lives on the same tmp disk
    disk = report.disks[0]
    assert disk.internal and disk.labels == []
    assert disk.deckinstall == {space.GAMES: 300, space.PREFIXES: 1000, space.CACHES: 0}
    assert disk.total >= disk.used and disk.free > 0


def test_symlinks_not_followed_and_hardlinks_counted_once(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "big.bin", 5000)
    gd = tmp_path / "game"
    _write(gd / "Game.exe", 100)
    os.symlink(outside / "big.bin", gd / "link.bin")
    os.symlink(outside, gd / "linkdir")
    os.link(gd / "Game.exe", gd / "hard.exe")
    linked_game = tmp_path / "linked_game"
    os.symlink(gd, linked_game)
    games = space.game_space([_game("G", gd, tmp_path / "nopfx"), _game("L", linked_game, tmp_path / "nopfx")])
    assert games[0].game_bytes == 100
    assert games[1].game_bytes == 0  # the game folder itself is a symlink


def test_legacy_prefix_inside_game_dir_is_its_own_column(tmp_path: Path) -> None:
    gd = tmp_path / "Old"
    _write(gd / "Game.exe", 100)
    _write(gd / "prefix" / "system.reg", 400)
    (entry,) = space.game_space([_game("Old", gd, gd / "prefix")])
    assert (entry.game_bytes, entry.prefix_bytes, entry.total_bytes) == (100, 400, 500)


def test_disks_deduplicated_by_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    internal = library.ensure_library(library.internal_library_root(), internal=True)
    sd1 = library.ensure_library(tmp_path / "sd1" / "DeckInstall", label="SD1")
    sd2 = library.ensure_library(tmp_path / "sd2" / "DeckInstall", label="SD2")
    _write(sd1.games_dir / "X" / "x.bin", 70)
    real_dev = space._dev

    def fake_dev(path: Path) -> int | None:
        text = os.path.realpath(path)
        if text.startswith(os.path.realpath(tmp_path / "sd")):
            return -1  # both "SD" libraries on one fake device
        return real_dev(path)

    monkeypatch.setattr(space, "_dev", fake_dev)
    disks = space.disk_space([internal, sd1, sd2])
    assert len(disks) == 2
    assert disks[0].internal
    sd = disks[1]
    assert sd.labels == ["SD1", "SD2"] and not sd.internal
    assert sd.deckinstall[space.GAMES] == 70


def test_unavailable_game_listed_without_sizes(tmp_path: Path) -> None:
    sd = library.ensure_library(tmp_path / "sd" / "DeckInstall", label="SD")
    gd = sd.games_dir / "Far"
    _write(gd / "Game.exe", 10)
    library.record_game(sd.root, "far-uid", "Far", gd)
    gone = library.Library(sd.id, sd.root, sd.label, internal=False, available=False)
    report = space.scan_space([gone], [])
    (entry,) = report.games
    assert (entry.name, entry.uid, entry.library, entry.available) == ("Far", "far-uid", "SD", False)
    assert entry.total_bytes == 0
    assert all("SD" not in d.labels for d in report.disks)


def test_space_card_shows_disks_and_games(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from deckinstall import ui_maintenance

    app = QApplication.instance() or QApplication([])
    holder = QWidget()

    class _Win:
        _closing_pending = False
        _space_req_id = 0

    win = _Win()
    card = ui_maintenance._build_space_card(win)  # type: ignore[arg-type]
    QVBoxLayout(holder).addWidget(card)
    report = space.SpaceReport(
        [space.DiskSpace(tmp_path, True, 1000, 400, 600, ["Internal"])],
        [
            space.GameSpace("A", game_bytes=10, prefix_bytes=5, shared_prefix=True),
            space.GameSpace("B", shared_prefix=True),
            space.GameSpace("C", library="SD", available=False),
        ],
    )
    ui_maintenance.apply_space_report(win, report)  # type: ignore[arg-type]
    table = win.space_games_list  # type: ignore[attr-defined]
    assert table.rowCount() == 3
    assert win.space_disks_layout.count() == 3  # type: ignore[attr-defined]
    texts = {table.item(r, 0).text(): r for r in range(3)}
    assert "(" in table.item(texts["A"], 3).text()
    assert table.item(texts["C"], 6).text()
    holder.deleteLater()  # parentless widget: delete it while Qt is still up
    from PySide6.QtCore import QEvent

    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_top_folders_biggest_first_limited_to_five() -> None:
    measured = {f"/d/{i}": i * 100 for i in range(8)}
    top = space.top_folders(measured)
    assert [n for _, n in top] == [700, 600, 500, 400, 300]  # zero-size folder never listed
    assert space.top_folders({"/a": 0}) == []


def test_scan_space_reports_top_folders(tmp_path: Path) -> None:
    lib = library.ensure_library(library.internal_library_root(), internal=True)
    big = lib.games_dir / "Big"
    small = lib.games_dir / "Small"
    _write(big / "Game.exe", 5000)
    _write(small / "Game.exe", 100)
    report = space.scan_space([lib], [_game("Big", big, tmp_path / "p1"), _game("Small", small, tmp_path / "p2")])
    assert [n for _, n in report.top_folders] == [5000, 100]
    from deckinstall import ui_maintenance

    text = ui_maintenance._format_top_folders(report.top_folders)
    assert text.splitlines()[0] and len(text.splitlines()) == 3
    assert "Big" in text.splitlines()[1] and "Small" in text.splitlines()[2]
