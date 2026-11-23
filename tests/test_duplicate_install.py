"""Installing a game that is already in the library warns first (KIBORG / "KIBORG (2)" report)."""

from __future__ import annotations

from pathlib import Path

from deckinstall.standalone import StandaloneGame, find_existing_installs, planned_game_dir


def _game(tmp: Path, name: str, game_dir: Path, appid: int | None = None, origin: str = "") -> StandaloneGame:
    return StandaloneGame(
        name=name,
        game_dir=game_dir,
        exe_path=game_dir / "game.exe",
        prefix_dir=tmp / "pfx",
        proton_name="",
        steam_appid=appid,
        winedlloverrides="",
        launch_options="",
        launch_script=game_dir / "launch.sh",
        desktop_file=tmp / "x.desktop",
        meta={"source": {"origin": origin}} if origin else {},
    )


def test_leftover_of_a_half_migrated_game_is_reported(tmp_path: Path) -> None:
    # v0.21 migrated only ~/Games/KIBORG/Kiborg (the Unreal project folder); the rest stayed behind.
    leftover = tmp_path / "Games" / "KIBORG"
    leftover.mkdir(parents=True)
    installed = _game(
        tmp_path, "KIBORG", tmp_path / "DeckInstall" / "Games" / "KIBORG" / "Kiborg",
        appid=2405060, origin=str(leftover / "Kiborg"),
    )
    found = find_existing_installs("Something else", None, leftover, [installed])
    assert found == [(installed, "folder")]


def test_same_appid_and_same_name_are_reported(tmp_path: Path) -> None:
    a = _game(tmp_path, "Game A", tmp_path / "lib" / "A", appid=42)
    b = _game(tmp_path, "The: Game-B", tmp_path / "lib" / "B")
    src = tmp_path / "new"
    assert find_existing_installs("X", 42, src, [a, b]) == [(a, "appid")]
    assert find_existing_installs("the game b", None, src, [a, b]) == [(b, "name")]
    assert find_existing_installs("Other", 7, src, [a, b]) == []


def test_shared_container_folder_holding_games_is_not_a_match(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    kept = _game(tmp_path, "Kept", downloads / "Kept")
    assert find_existing_installs("New", None, downloads, [kept], container=True) == []
    assert find_existing_installs("New", None, downloads, [kept]) == [(kept, "folder")]


def test_planned_dir_shows_the_real_numbered_folder(tmp_path: Path) -> None:
    games = tmp_path / "Games"
    (games / "KIBORG" / "Kiborg").mkdir(parents=True)
    src = tmp_path / "src" / "KIBORG"
    src.mkdir(parents=True)
    assert planned_game_dir(games, "KIBORG", src) == games / "KIBORG (2)"
    (games / "Empty").mkdir()
    assert planned_game_dir(games, "Empty", src) == games / "Empty"
    assert planned_game_dir(games, "New", src) == games / "New"
