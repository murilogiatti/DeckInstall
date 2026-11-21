"""Unit tests for external launcher scanning in deckinstall.launcher_scan."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import launcher_scan  # noqa: E402


class TestYamlHelpers:
    """Tests for _unquote_yaml_scalar and _parse_simple_yaml."""

    def test_unquote_yaml_scalar_handles_escapes_and_nulls(self) -> None:
        assert launcher_scan._unquote_yaml_scalar("'it''s a ''test'''") == "it's a 'test'"
        assert launcher_scan._unquote_yaml_scalar('"say \\"hello\\""') == 'say "hello"'
        assert launcher_scan._unquote_yaml_scalar('"C:\\\\Games\\\\My\\\\Game"') == "C:\\Games\\My\\Game"
        assert launcher_scan._unquote_yaml_scalar("null") == ""
        assert launcher_scan._unquote_yaml_scalar("~") == ""

    def test_parse_simple_yaml_handles_colons_quotes_and_escapes(self) -> None:
        raw = (
            "Name: 'Game: Special Edition'\n"
            "url: https://example.com:8080/path\n"
            "External_Programs:\n"
            "  prog1:\n"
            "    name: 'Bob''s \"Tool\": v1'\n"
            '    path: "C:\\\\Games\\\\Foo: Bar\\\\game.exe"\n'
            '    quoted: "say \\"hi\\""\n'
        )
        parsed = launcher_scan._parse_simple_yaml(raw)
        assert parsed["Name"] == "Game: Special Edition"
        assert parsed["url"] == "https://example.com:8080/path"
        prog1 = parsed["External_Programs"]["prog1"]
        assert prog1["name"] == 'Bob\'s "Tool": v1'
        assert prog1["path"] == "C:\\Games\\Foo: Bar\\game.exe"
        assert prog1["quoted"] == 'say "hi"'


class TestMalformedConfigs:
    """Malformed YAML/JSON/SQLite configs must not crash scanning."""

    def test_malformed_configs_do_not_crash(self, tmp_path: Path) -> None:
        home = tmp_path / "home"

        # Lutris malformed YAML + corrupt pga.db
        lutris_games = home / ".config" / "lutris" / "games"
        lutris_games.mkdir(parents=True)
        (lutris_games / "broken.yml").write_text(":::\n  - [unclosed\n\x00\xff", encoding="utf-8")
        lutris_db_dir = home / ".local" / "share" / "lutris"
        lutris_db_dir.mkdir(parents=True)
        (lutris_db_dir / "pga.db").write_bytes(b"not a sqlite database")

        # Heroic malformed JSONs
        heroic = home / ".config" / "heroic"
        (heroic / "legendaryConfig" / "legendary").mkdir(parents=True)
        (heroic / "legendaryConfig" / "legendary" / "installed.json").write_text("{bad json", encoding="utf-8")
        (heroic / "gog_store").mkdir(parents=True)
        (heroic / "gog_store" / "installed.json").write_text("[{broken", encoding="utf-8")
        (heroic / "nile_config" / "nile").mkdir(parents=True)
        (heroic / "nile_config" / "nile" / "installed.json").write_text("not json", encoding="utf-8")
        (heroic / "sideload_apps").mkdir(parents=True)
        (heroic / "sideload_apps" / "library.json").write_text("{", encoding="utf-8")
        (heroic / "GamesConfig").mkdir(parents=True)
        (heroic / "GamesConfig" / "app1.json").write_text("{bad", encoding="utf-8")

        # Bottles malformed bottle.yml
        bottle = home / ".local" / "share" / "bottles" / "bottles" / "BrokenBottle"
        bottle.mkdir(parents=True)
        (bottle / "bottle.yml").write_text("Name: [broken\nExternal_Programs: not_a_dict\n", encoding="utf-8")

        assert launcher_scan._scan_lutris(home) == []
        assert launcher_scan._scan_heroic(home) == []
        assert launcher_scan._scan_bottles(home) == []
        assert launcher_scan.scan_external_launcher_games(home) == []


class TestBottlesScanner:
    """Tests for Bottles path resolution and _scan_bottles."""

    def test_resolve_bottles_exe_z_and_c_drives(self, tmp_path: Path) -> None:
        bottle_dir = tmp_path / "MyBottle"
        c_exe = bottle_dir / "drive_c" / "Games" / "game.exe"
        c_exe.parent.mkdir(parents=True)
        c_exe.write_bytes(b"MZ")

        assert launcher_scan._resolve_bottles_exe(r"C:\Games\game.exe", bottle_dir) == c_exe

        # Direct unit check for Z:\home\user\Game\game.exe -> /home/user/Game/game.exe (not drive_c)
        expected_z = Path("/home/user/Game/game.exe")
        with mock.patch.object(Path, "is_file", autospec=True, side_effect=lambda p: p == expected_z):
            resolved_z = launcher_scan._resolve_bottles_exe(r"Z:\home\user\Game\game.exe", bottle_dir)
        assert resolved_z == expected_z
        assert "drive_c" not in str(resolved_z)

    def test_scan_bottles_discovers_z_and_c_entries(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        bottle_dir = home / ".local" / "share" / "bottles" / "bottles" / "Gaming"
        c_exe = bottle_dir / "drive_c" / "Games" / "game_c.exe"
        c_exe.parent.mkdir(parents=True)
        c_exe.write_bytes(b"MZ")

        z_real_exe = tmp_path / "external_game" / "game_z.exe"
        z_real_exe.parent.mkdir(parents=True)
        z_real_exe.write_bytes(b"MZ")
        z_win_path = "Z:" + str(z_real_exe).replace("/", "\\")

        bottle_yml = (
            "Name: Gaming\n"
            "External_Programs:\n"
            "  id_c:\n"
            "    name: Game C\n"
            '    path: "C:\\\\Games\\\\game_c.exe"\n'
            "  id_z:\n"
            "    name: Game Z\n"
            f'    path: "{z_win_path.replace(chr(92), chr(92) * 2)}"\n'
        )
        (bottle_dir / "bottle.yml").write_text(bottle_yml, encoding="utf-8")

        games = launcher_scan._scan_bottles(home)
        by_name = {g.name: g for g in games}
        assert set(by_name) == {"Game C", "Game Z"}
        assert by_name["Game C"].exe_path == c_exe
        assert by_name["Game C"].prefix_dir == bottle_dir
        assert by_name["Game Z"].exe_path == z_real_exe
        assert by_name["Game Z"].launch_cmd == ["bottles-cli", "run", "-b", "Gaming", "-p", "Game Z"]


class TestHeroicScanner:
    """Tests for Heroic GOG, Amazon/Nile, and sideload entries."""

    def test_scan_heroic_gog_amazon_sideload_and_backslash_exe(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        heroic = home / ".config" / "heroic"

        # GOG game with Windows backslash relative executable
        gog_dir = tmp_path / "games" / "GogGame"
        gog_exe = gog_dir / "Binaries" / "Win64" / "Game.exe"
        gog_exe.parent.mkdir(parents=True)
        gog_exe.write_bytes(b"MZ")
        (heroic / "gog_store").mkdir(parents=True)
        (heroic / "gog_store" / "installed.json").write_text(
            json.dumps(
                {
                    "installed": [
                        {
                            "appName": "gog_123",
                            "title": "GOG Game",
                            "install_path": str(gog_dir),
                            "executable": r"Binaries\Win64\Game.exe",
                            "platform": "windows",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        # Amazon / Nile game
        nile_dir = tmp_path / "games" / "AmazonGame"
        nile_exe = nile_dir / "Sub" / "Amazon.exe"
        nile_exe.parent.mkdir(parents=True)
        nile_exe.write_bytes(b"MZ")
        (heroic / "nile_config" / "nile").mkdir(parents=True)
        (heroic / "nile_config" / "nile" / "installed.json").write_text(
            json.dumps(
                [
                    {
                        "id": "nile_456",
                        "title": "Amazon Game",
                        "path": str(nile_dir),
                        "executable": r"Sub\Amazon.exe",
                        "platform": "windows",
                    }
                ]
            ),
            encoding="utf-8",
        )

        # Sideloaded app
        side_dir = tmp_path / "games" / "SideGame"
        side_exe = side_dir / "run.sh"
        side_exe.parent.mkdir(parents=True)
        side_exe.write_text("#!/bin/sh\n", encoding="utf-8")
        (heroic / "sideload_apps").mkdir(parents=True)
        (heroic / "sideload_apps" / "library.json").write_text(
            json.dumps(
                {
                    "games": [
                        {
                            "app_name": "side_789",
                            "title": "Sideload Game",
                            "folder_name": str(side_dir),
                            "install": {
                                "executable": "run.sh",
                                "platform": "linux",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        games = launcher_scan._scan_heroic(home)
        by_name = {g.name: g for g in games}
        assert set(by_name) == {"GOG Game", "Amazon Game", "Sideload Game"}
        assert by_name["GOG Game"].exe_path == gog_exe
        assert by_name["GOG Game"].runner == "wine"
        assert by_name["Amazon Game"].exe_path == nile_exe
        assert by_name["Amazon Game"].runner == "wine"
        assert by_name["Sideload Game"].exe_path == side_exe
        assert by_name["Sideload Game"].runner == "linux"


class TestLutrisAndCombinedScan:
    """Tests for Lutris SQLite URI escaping and scan_external_launcher_games."""

    def test_lutris_sqlite_path_with_hash_and_question_mark(self, tmp_path: Path) -> None:
        home = tmp_path / "home#with?specials"
        db_path = home / ".local" / "share" / "lutris" / "pga.db"
        db_path.parent.mkdir(parents=True)

        game_dir = tmp_path / "lutris_game"
        game_exe = game_dir / "game.exe"
        game_exe.parent.mkdir(parents=True)
        game_exe.write_bytes(b"MZ")

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE games (name TEXT, slug TEXT, runner TEXT, directory TEXT, installed INTEGER, exe TEXT)"
            )
            conn.execute(
                "INSERT INTO games (name, slug, runner, directory, installed, exe) VALUES (?, ?, ?, ?, ?, ?)",
                ("Special DB Game", "special-db-game", "wine", str(game_dir), 1, "game.exe"),
            )
            conn.commit()
        finally:
            conn.close()

        games = launcher_scan._scan_lutris(home)
        assert len(games) == 1
        assert games[0].source == "lutris"
        assert games[0].name == "Special DB Game"
        assert games[0].exe_path == game_exe
        assert games[0].launch_cmd == ["lutris", "lutris:rungame/special-db-game"]

    def test_scan_external_launcher_games_combines_all_sources(self, tmp_path: Path) -> None:
        home = tmp_path / "home"

        # 1. Lutris YAML
        lutris_dir = home / ".config" / "lutris" / "games"
        lutris_dir.mkdir(parents=True)
        l_exe = tmp_path / "l_game" / "l.exe"
        l_exe.parent.mkdir(parents=True)
        l_exe.write_bytes(b"MZ")
        (lutris_dir / "lgame.yml").write_text(
            f"name: Lutris Game\nslug: lgame\nrunner: wine\ngame:\n  exe: {l_exe}\n",
            encoding="utf-8",
        )

        # 2. Heroic GOG
        h_dir = tmp_path / "h_game"
        h_exe = h_dir / "h.exe"
        h_exe.parent.mkdir(parents=True)
        h_exe.write_bytes(b"MZ")
        (home / ".config" / "heroic" / "gog_store").mkdir(parents=True)
        (home / ".config" / "heroic" / "gog_store" / "installed.json").write_text(
            json.dumps(
                {
                    "installed": [
                        {
                            "appName": "h1",
                            "title": "Heroic Game",
                            "install_path": str(h_dir),
                            "executable": "h.exe",
                            "platform": "windows",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        # 3. Bottles
        b_dir = home / ".local" / "share" / "bottles" / "bottles" / "B1"
        b_exe = b_dir / "drive_c" / "b.exe"
        b_exe.parent.mkdir(parents=True)
        b_exe.write_bytes(b"MZ")
        (b_dir / "bottle.yml").write_text(
            "Name: B1\nExternal_Programs:\n  p1:\n    name: Bottles Game\n    path: 'C:\\b.exe'\n",
            encoding="utf-8",
        )

        all_games = launcher_scan.scan_external_launcher_games(home)
        sources = {g.source: g.name for g in all_games}
        assert sources == {
            "lutris": "Lutris Game",
            "heroic": "Heroic Game",
            "bottles": "Bottles Game",
        }


def test_configured_prefix_roots_reads_launcher_settings(tmp_path):
    import json

    from deckinstall.launcher_scan import bottles_roots, configured_prefix_roots

    home = tmp_path
    custom = tmp_path / "ssd" / "HeroicPrefixes"
    custom.mkdir(parents=True)
    game_pfx = tmp_path / "ssd" / "Other" / "My Game"
    game_pfx.mkdir(parents=True)
    heroic = home / ".config" / "heroic"
    (heroic / "GamesConfig").mkdir(parents=True)
    (heroic / "config.json").write_text(json.dumps({"defaultSettings": {"defaultWinePrefixDir": str(custom)}}))
    (heroic / "GamesConfig" / "abc.json").write_text(json.dumps({"abc": {"winePrefix": str(game_pfx)}}))
    lutris_pfx = tmp_path / "hdd" / "lutris" / "g1"
    lutris_pfx.mkdir(parents=True)
    games = home / ".local" / "share" / "lutris" / "games"  # no ~/.config/lutris: data dir
    games.mkdir(parents=True)
    (games / "g1-123.yml").write_text(f"game:\n  exe: /x/g.exe\n  prefix: {lutris_pfx}\n")
    bottles_custom = tmp_path / "hdd" / "bottles"
    bottles_custom.mkdir(parents=True)
    base = home / ".local" / "share" / "bottles"
    base.mkdir(parents=True)
    (base / "data.yml").write_text(f"custom_bottles_path: {bottles_custom}\n")

    roots = configured_prefix_roots(home)
    assert custom in roots
    assert game_pfx.parent in roots
    assert lutris_pfx.parent in roots
    assert bottles_custom in roots
    assert (bottles_custom, False) in bottles_roots(home)
