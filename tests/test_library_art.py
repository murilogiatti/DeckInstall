"""SteamGridDB artwork for games already in the Library (no network: the API is faked)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from deckinstall import library_art, steamgriddb, ui
from deckinstall.i18n import tr


@pytest.fixture(autouse=True)
def _data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def _fake_api(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_get(path: str, key: str, params: dict[str, str] | None = None) -> object:
        calls.append(path)
        if path.startswith("search/autocomplete/"):
            return [{"id": 77}]
        return [{"url": f"https://cdn2.steamgriddb.com/{path.split('/')[0]}/x.png"}]

    def fake_download(urls: list[str], dest: Path, alt_remove: object = None) -> bool:
        dest.write_bytes(b"\x89PNG" + urls[0].encode())
        return True

    monkeypatch.setattr(steamgriddb, "_get", fake_get)
    monkeypatch.setattr(steamgriddb, "_download_first", fake_download)


def test_jobs_per_kind_of_library_row(tmp_path: Path) -> None:
    grid = tmp_path / "grid"
    shortcut = SimpleNamespace(appid_u32=3000000001)
    managed = SimpleNamespace(steam_appid=1145360)
    job = library_art.job_for_row("steam", shortcut, "steam:3000000001", "Hades", grid, managed)
    assert job == library_art.ArtJob("Hades", grid, 3000000001, 1145360, None)  # every kind, Steam's folder
    game = SimpleNamespace(steam_appid=None)
    own = library_art.job_for_row("standalone", game, "standalone:/g/Hades", "Hades", None)
    assert own is not None and own.kinds == ("capsule",) and own.grid_dir == library_art.covers_dir()
    assert own.file_id == library_art.cover_file_id("standalone:/g/Hades")
    assert library_art.job_for_row("steam", shortcut, "k", "Hades", None) is None  # no Steam folder


def test_fetch_fills_only_what_is_missing_and_replace_keeps_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _fake_api(monkeypatch, calls)
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "42p.jpg").write_bytes(b"mine")  # the user's own cover
    job = library_art.ArtJob("Hades", grid, 42, None, None)
    assert library_art.fetch_artwork([job], key="k") == (1, 3, [])
    assert (grid / "42p.jpg").read_bytes() == b"mine"  # filling never overwrites
    assert {p.name for p in grid.iterdir()} == {"42p.jpg", "42.png", "42_hero.png", "42_logo.png"}
    assert library_art.has_all_art(job)
    assert library_art.fetch_artwork([job], key="k") == (0, 0, [])  # nothing missing: no download
    games, files, _ = library_art.fetch_artwork([job], replace=True, key="k")
    assert (games, files) == (1, 4) and not (grid / "42p.jpg").exists()
    assert (grid / ".deckinstall_art_bak" / "42" / "42p.jpg").read_bytes() == b"mine"


def test_capsule_only_for_games_without_a_shortcut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _fake_api(monkeypatch, calls)
    job = library_art.job_for_row("external", object(), "external:heroic:X", "X", None)
    assert job is not None
    assert library_art.fetch_artwork([job], key="k") == (1, 1, [])
    assert [p.name for p in library_art.covers_dir().iterdir()] == [f"{job.file_id}p.png"]
    assert calls == ["search/autocomplete/X", "grids/game/77"]


def test_a_refused_key_stops_the_whole_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(path: str, key: str, params: object = None) -> object:
        raise requests.HTTPError(response=SimpleNamespace(status_code=401))  # type: ignore[arg-type]

    monkeypatch.setattr(steamgriddb, "_get", refused)
    jobs = [library_art.ArtJob(n, tmp_path, i, None, None) for i, n in enumerate(("A", "B"))]
    with pytest.raises(requests.HTTPError):
        library_art.fetch_artwork(jobs, key="bad")


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


def test_library_buttons_fetch_art_and_the_cover_grid_shows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import ui_library_art, ui_library_grid

    calls: list[str] = []
    _fake_api(monkeypatch, calls)
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        win = _window(app, tmp)  # type: ignore[arg-type]
        try:
            assert ui._wait_for_workers(app, win, 10.0)  # type: ignore[arg-type]
            table = win.shortcuts_list
            table.setSortingEnabled(False)
            table.insertRow(0)
            cell = QTableWidgetItem("★ Hades")
            cell.setData(Qt.ItemDataRole.UserRole, ("standalone", SimpleNamespace(steam_appid=None)))
            cell.setData(Qt.ItemDataRole.UserRole + 1, "standalone:/g/Hades")
            table.setItem(0, 0, cell)
            table.setCurrentCell(0, 0)
            assert win.btn_game_art.isEnabled()
            # No key yet: the key dialog opens right here; cancelled, nothing is fetched.
            with mock.patch("deckinstall.ui_system.set_steamgriddb_key") as ask:
                monkeypatch.setattr(ui_library_art, "api_key", lambda: "")
                win.btn_game_art.click()
                ask.assert_called_once()
            assert calls == []
            monkeypatch.setattr(ui_library_art, "api_key", lambda: "k")
            monkeypatch.setattr(steamgriddb, "api_key", lambda: "k")
            win.btn_all_art.click()
            assert ui._wait_for_workers(app, win, 10.0)  # type: ignore[arg-type]
            app.processEvents()
            assert calls == ["search/autocomplete/Hades", "grids/game/77"]  # the star is not part of the name
            assert win.message_bar.text().startswith(tr("art.done", games=1, files=1))
            cover = ui_library_grid.cover_path("standalone", object(), None, "standalone:/g/Hades")
            assert cover is not None and cover.parent == library_art.covers_dir()
            win.btn_all_art.click()  # nothing missing any more
            assert win.message_bar.text() == tr("art.nothing_missing")
            with mock.patch.object(ui_library_art.QMessageBox, "question", return_value=ui.QMessageBox.StandardButton.No) as q:
                win.btn_game_art.click()  # it has art: asks before swapping
                q.assert_called_once()
        finally:
            assert ui._wait_for_workers(app, win, 10.0)  # type: ignore[arg-type]
            win.close()
