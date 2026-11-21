"""Migration of v1 (~/Games) games into the internal library (and its rollback on failure)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import library, migrate, save_sync, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V1, MANIFEST_V2  # noqa: E402


def _legacy() -> Path:
    return standalone.get_legacy_games_dir()


def _make_v1_game(name: str, game_dir: Path | None = None, prefix_name: str | None = None) -> dict[str, Path]:
    """Create a game exactly as v0.13 left it: v1 manifest, launch.sh, .desktop, prefix, cache."""
    legacy = _legacy()
    game_dir = game_dir or legacy / name
    game_dir.mkdir(parents=True)
    exe = game_dir / "bin" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    prefix = legacy / ".prefixes" / (prefix_name or name)
    save = prefix / "drive_c" / "users" / "steamuser" / "Saved Games" / name / "slot1.sav"
    save.parent.mkdir(parents=True)
    save.write_text("my progress", encoding="utf-8")
    (prefix / "pfx").symlink_to(".")
    cache = legacy / ".caches" / f"{name}-abcd1234"
    (cache / "dxvk").mkdir(parents=True)
    launch = game_dir / "launch.sh"
    launch.write_text(f"#!/bin/sh\nexport WINEPREFIX='{prefix}'\n", encoding="utf-8")
    apps = standalone.get_applications_dir()
    apps.mkdir(parents=True, exist_ok=True)
    desktop = apps / f"deckinstall-{name.lower().replace(' ', '-')}.desktop"
    desktop.write_text(f"[Desktop Entry]\nName={name}\nPath={game_dir}\n", encoding="utf-8")
    manifest = {
        "name": name,
        "game_dir": str(game_dir),
        "exe_path": str(exe),
        "prefix_dir": str(prefix),
        "proton_name": "GE-Proton10-25",
        "steam_appid": None,
        "winedlloverrides": "",
        "launch_options": "",
        "launch_script": str(launch),
        "desktop_file": str(desktop),
        "desktop_shortcut": "",
        "native_linux": False,
        "cache_dir": str(cache),
    }
    (game_dir / MANIFEST_V1).write_text(json.dumps(manifest), encoding="utf-8")
    return {"game_dir": game_dir, "prefix": prefix, "cache": cache, "launch": launch, "desktop": desktop, "save": save}


def _snapshot(paths: dict[str, Path]) -> dict[str, str]:
    return {k: paths[k].read_text(encoding="utf-8") for k in ("launch", "desktop", "save")} | {
        "manifest": (paths["game_dir"] / MANIFEST_V1).read_text(encoding="utf-8")
    }


def test_migrate_moves_everything_and_keeps_saves(tmp_path: Path) -> None:
    old = _make_v1_game("Hollow")
    plan = migrate.plan_migration()
    assert [(i.game.name, i.action) for i in plan] == [("Hollow", "migrate")]

    result = migrate.migrate_legacy_games(plan)

    assert result.migrated == ["Hollow"] and not result.failed
    lib = library.internal_library_root()
    games = standalone.list_standalone_games()
    assert len(games) == 1
    game = games[0]
    assert game.schema == 2
    assert game.game_dir == (lib / "Games" / "Hollow").resolve()
    assert game.exe_path == game.game_dir / "bin" / "game.exe"
    assert game.prefix_dir.resolve() == (lib / "Prefixes" / game.uid).resolve()
    assert game.cache_dir is not None and (game.cache_dir / "dxvk").is_dir()
    save = game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games" / "Hollow" / "slot1.sav"
    assert save.read_text(encoding="utf-8") == "my progress"
    assert (game.game_dir / MANIFEST_V2).is_file()
    assert not (game.game_dir / MANIFEST_V1).exists()
    assert str(game.prefix_dir) in game.launch_script.read_text(encoding="utf-8")
    assert f"Path={game.game_dir}" in old["desktop"].read_text(encoding="utf-8")
    assert not old["game_dir"].exists() and not old["prefix"].exists() and not old["cache"].exists()
    lib_file = library.read_library_file(lib)
    assert lib_file is not None and [g["name"] for g in lib_file["games"]] == ["Hollow"]
    snaps = list((save_sync.DEFAULT_SNAPSHOTS_DIR).rglob("*.zip"))
    assert len(snaps) == 1
    assert migrate.plan_migration() == []  # nothing left to migrate


def test_failure_rolls_the_game_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _make_v1_game("Hollow")
    before = _snapshot(old)

    def boom(_game: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(standalone, "_write_manifest", boom)
    result = migrate.migrate_legacy_games()

    assert result.migrated == []
    assert result.failed and result.failed[0][0] == "Hollow" and "disk full" in result.failed[0][1]
    assert _snapshot(old) == before
    assert old["prefix"].is_dir() and old["cache"].is_dir()
    assert not any((library.internal_library_root() / "Games").iterdir())


def test_kept_in_place_and_nested_games(tmp_path: Path) -> None:
    outside = _make_v1_game("Outside", game_dir=tmp_path / "Downloads" / "Outside")
    standalone._register_standalone_game(
        standalone._load_manifest(outside["game_dir"] / MANIFEST_V1),  # type: ignore[arg-type]
        outside["game_dir"] / MANIFEST_V1,
    )
    _make_v1_game("Kiborg", game_dir=_legacy() / "KIBORG" / "Kiborg")

    result = migrate.migrate_legacy_games()

    assert sorted(result.migrated) == ["Kiborg", "Outside"]
    by_name = {g.name: g for g in standalone.list_standalone_games()}
    assert by_name["Outside"].game_dir == outside["game_dir"].resolve()  # not moved
    assert by_name["Outside"].prefix_dir.resolve() == (
        library.internal_library_root() / "Prefixes" / by_name["Outside"].uid
    ).resolve()
    assert by_name["Kiborg"].game_dir == (library.internal_library_root() / "Games" / "KIBORG" / "Kiborg").resolve()
    assert not (_legacy() / "KIBORG").exists()



def test_games_used_by_steam_shortcuts_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _make_v1_game("Hollow")
    monkeypatch.setattr(migrate, "_shortcut_paths", lambda _paths: [str(old["game_dir"] / "bin" / "game.exe")])
    plan = migrate.plan_migration()
    assert [(i.action, i.reason) for i in plan] == [("skip", "steam_shortcut")]
    result = migrate.migrate_legacy_games(plan)
    assert result.migrated == [] and result.skipped == [("Hollow", "steam_shortcut")]
    assert result.journal is None
    assert (old["game_dir"] / MANIFEST_V1).is_file()


def test_shared_prefix_stays_where_it_is(tmp_path: Path) -> None:
    a = _make_v1_game("Alpha", prefix_name="Shared")
    legacy = _legacy()
    b_dir = legacy / "Beta"
    b_dir.mkdir()
    (b_dir / "game.exe").write_bytes(b"MZ")
    (b_dir / MANIFEST_V1).write_text(json.dumps({
        "name": "Beta",
        "exe_path": str(b_dir / "game.exe"),
        "prefix_dir": str(a["prefix"]),
        "launch_script": str(b_dir / "launch.sh"),
    }), encoding="utf-8")

    result = migrate.migrate_legacy_games()

    assert sorted(result.migrated) == ["Alpha", "Beta"]
    assert a["prefix"].is_dir()  # both games still share the original prefix
    for game in standalone.list_standalone_games():
        assert game.prefix_dir.resolve() == a["prefix"].resolve()


def test_ui_migrate_and_undo_buttons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest import mock

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    from deckinstall import ui

    old = _make_v1_game("Hollow")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "_is_wine_game_running", lambda: False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[0])  # "Migrate now"
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    with (
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(ui.MainWindow, "_maybe_prompt_migration", lambda self, games: None),
    ):
        win = ui.MainWindow(app)
        win.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

        def _drain() -> None:
            for _ in range(50):
                app.processEvents()
                for w in list(win._active_workers):
                    w.wait(2000)
                app.processEvents()
                if not win._active_workers:
                    return

        _drain()
        win._start_legacy_migration()
        _drain()
        assert not old["game_dir"].exists()
        assert [g.schema for g in standalone.list_standalone_games()] == [2]

        win._active_workers.clear()
        win.close()
