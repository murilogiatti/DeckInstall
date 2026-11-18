"""Tests for per-disk libraries: <disk>/DeckInstall/{Games,Prefixes,Caches} + library.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake /run/media/$USER with one mounted disk called SD."""
    media_root = tmp_path / "media"
    (media_root / "SD").mkdir(parents=True)
    monkeypatch.setattr(library, "default_mount_search_roots", lambda: [media_root])
    monkeypatch.setattr(library, "_is_mount", lambda p: p.parent == media_root)
    monkeypatch.setattr(standalone, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(standalone, "resolve_game_icon", lambda *a, **k: None)
    return media_root


def _source_game(tmp_path: Path, name: str = "Indie") -> Path:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    return src


def test_mount_table_parsing_and_fs_type(tmp_path: Path) -> None:
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/nvme0n1p8 / ext4 rw 0 0\n"
        "/dev/mmcblk0p1 /run/media/deck/My\\040SD exfat rw 0 0\n",
        encoding="utf-8",
    )
    table = library.read_mount_table(mounts)
    assert (Path("/run/media/deck/My SD"), "exfat") in table
    assert library.fs_type_of(Path("/run/media/deck/My SD/DeckInstall"), table) == "exfat"
    assert library.fs_type_of(Path("/home/deck"), table) == "ext4"


def test_install_into_disk_library_uses_relative_paths(tmp_path: Path, media: Path) -> None:
    lib = library.ensure_library(media / "SD" / "DeckInstall", label="SD")
    assert lib.games_dir.is_dir() and lib.prefixes_dir.is_dir() and lib.caches_dir.is_dir()
    src = _source_game(tmp_path)

    game = standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)

    assert game.game_dir == lib.games_dir / "Indie"
    assert game.prefix_dir == lib.prefixes_dir / game.uid
    assert game.cache_dir == lib.caches_dir / game.uid
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["game"]["dir"] == "Games/Indie"
    assert data["prefix"] == {"path": f"Prefixes/{game.uid}", "location": "library"}
    assert data["cache"]["path"] == f"Caches/{game.uid}"
    lib_file = library.read_library_file(lib.root)
    assert lib_file is not None
    assert lib_file["games"] == [{"id": game.uid, "name": "Indie", "dir": "Games/Indie"}]

    listed = standalone.list_standalone_games()
    assert [g.uid for g in listed] == [game.uid]

    standalone.remove_standalone_game(listed[0], delete_files=True)
    assert not game.prefix_dir.exists()
    lib_file = library.read_library_file(lib.root)
    assert lib_file is not None and lib_file["games"] == []


def test_default_install_goes_to_internal_library(tmp_path: Path, media: Path) -> None:
    src = _source_game(tmp_path)
    game = standalone.install_standalone_game("Indie", src / "game.exe", src)
    internal = library.internal_library_root()
    assert game.game_dir == internal / "Games" / "Indie"
    assert game.prefix_dir == internal / "Prefixes" / game.uid
    assert library.read_library_file(internal) is not None


def test_non_unix_disk_keeps_prefix_on_internal_disk(tmp_path: Path, media: Path) -> None:
    sd_root = media / "SD" / "DeckInstall"
    with mock.patch.object(library, "fs_type_of", side_effect=lambda p, t=None: "exfat" if "SD" in str(p) else "ext4"):
        lib = library.ensure_library(sd_root, label="SD")
        assert not lib.unix_fs
        assert not lib.prefixes_dir.exists()
        src = _source_game(tmp_path)
        game = standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)

    internal = library.internal_library_root()
    assert game.game_dir == sd_root / "Games" / "Indie"
    assert game.prefix_dir == internal / "Prefixes" / game.uid
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["prefix"]["location"] == "internal"
    loaded = standalone._load_manifest(game.game_dir / MANIFEST_V2)
    assert loaded is not None and loaded.prefix_dir == game.prefix_dir


def test_unmounted_disk_lists_games_as_unavailable(tmp_path: Path, media: Path) -> None:
    lib = library.ensure_library(media / "SD" / "DeckInstall", label="SD")
    src = _source_game(tmp_path)
    game = standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)
    assert library.list_unavailable_games() == []

    (media / "SD").rename(tmp_path / "SD-ejected")  # the disk goes away

    libs = library.discover_libraries()
    sd = next(entry for entry in libs if entry.id == lib.id)
    assert not sd.available
    assert library.list_unavailable_games(libs) == [{"id": game.uid, "name": "Indie", "library": "SD"}]
    assert standalone.list_standalone_games() == []
    with pytest.raises(FileNotFoundError):
        standalone.install_standalone_game("Other", src / "game.exe", src, library=sd)


def test_disk_library_found_again_at_new_mount_point(tmp_path: Path, media: Path) -> None:
    lib = library.ensure_library(media / "SD" / "DeckInstall", label="SD")
    src = _source_game(tmp_path)
    game = standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)

    (media / "SD").rename(media / "SD-renamed")  # remounted under another name

    libs = library.discover_libraries()
    assert [entry.root for entry in libs if entry.id == lib.id] == [media / "SD-renamed" / "DeckInstall"]
    listed = standalone.list_standalone_games()
    assert len(listed) == 1
    assert listed[0].uid == game.uid
    assert listed[0].prefix_dir == (media / "SD-renamed" / "DeckInstall" / "Prefixes" / game.uid).resolve()


def test_absent_game_cache_on_internal_disk_is_not_an_orphan(tmp_path: Path, media: Path) -> None:
    sd_root = media / "SD" / "DeckInstall"
    with mock.patch.object(library, "fs_type_of", side_effect=lambda p, t=None: "exfat" if "SD" in str(p) else "ext4"):
        lib = library.ensure_library(sd_root, label="SD")
        src = _source_game(tmp_path)
        game = standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)
    assert game.cache_dir is not None
    game.cache_dir.mkdir(parents=True, exist_ok=True)
    stray = library.internal_library_root() / "Caches" / "stray"
    stray.mkdir(parents=True)

    (media / "SD").rename(tmp_path / "SD-ejected")

    orphans = {entry.path for entry in standalone.find_orphan_shader_caches()}
    assert stray in orphans
    assert game.cache_dir not in orphans


def test_install_tab_lists_libraries_disks_and_absent_games(tmp_path: Path, media: Path) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from deckinstall import ui

    lib = library.ensure_library(media / "SD" / "DeckInstall", label="SD")
    src = _source_game(tmp_path)
    standalone.install_standalone_game("Indie", src / "game.exe", src, library=lib)
    (media / "USB").mkdir()  # a mounted disk without a library
    old = library.ensure_library(media / "OLD" / "DeckInstall", label="OLD")
    src2 = _source_game(tmp_path, "Gone")
    standalone.install_standalone_game("Gone", src2 / "game.exe", src2, library=old)
    (media / "OLD").rename(tmp_path / "OLD-ejected")

    app = QApplication.instance() or QApplication([])
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
    ):
        win = ui.MainWindow(app)
        win.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        win._refresh_libraries()
        combo = win.add_library_combo
        items = [str(combo.itemData(i)) for i in range(combo.count())]
        assert items == [
            f"lib:{library.internal_library_root()}",
            f"lib:{lib.root}",
            f"disk:{media / 'USB'}",
        ]
        assert not win.unavailable_banner.isHidden()
        assert "Gone (OLD)" in win.unavailable_banner.text()

        combo.setCurrentIndex(2)
        ok, chosen = win._selected_library()
        assert ok and chosen is not None
        assert chosen.root == media / "USB" / "DeckInstall"
        assert library.read_library_file(chosen.root) is not None

        for w in list(win._active_workers):
            w.wait(2000)
        app.processEvents()
        win._active_workers.clear()
        win.close()
