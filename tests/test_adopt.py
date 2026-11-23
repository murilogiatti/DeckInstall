"""Phase 5: adopting games from Hydra, Heroic, Lutris, Bottles, umu, Steam shortcuts and loose folders."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conftest import pe_exe_bytes  # noqa: E402

from deckinstall import adopt, launcher_scan, library, standalone, steam_vdf  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402

EXE = pe_exe_bytes(extra=b"\0" * 64)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    return h


def _game(folder: Path, exe_name: str = "game.exe") -> Path:
    exe = folder / exe_name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(EXE)
    (folder / "data.pak").write_bytes(b"x" * 1000)
    return exe


def _prefix(prefix: Path, save: str = "progress", steam_layout: bool = False) -> Path:
    root = prefix / "pfx" if steam_layout else prefix
    saves = root / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game"
    saves.mkdir(parents=True)
    (saves / "slot1.sav").write_text(save, encoding="utf-8")
    (root / "system.reg").write_text("WINE REGISTRY", encoding="utf-8")
    (root / "dosdevices").mkdir()
    (root / "dosdevices" / "c:").symlink_to("../drive_c")
    if steam_layout:
        (prefix / "version").write_text("10.0-1\n", encoding="utf-8")
    return saves / "slot1.sav"


def _heroic(home: Path, entries: list[tuple[str, str, Path, Path | None]]) -> None:
    cfg = home / ".config" / "heroic"
    (cfg / "gog_store").mkdir(parents=True)
    (cfg / "GamesConfig").mkdir()
    installed = []
    for app, title, game_dir, prefix in entries:
        installed.append(
            {"appName": app, "title": title, "install_path": str(game_dir), "executable": "game.exe", "platform": "windows"}
        )
        conf = {"winePrefix": str(prefix) if prefix else "", "wineVersion": {"name": "GE-Proton9-20", "type": "proton"}}
        (cfg / "GamesConfig" / f"{app}.json").write_text(json.dumps({app: conf}), encoding="utf-8")
    (cfg / "gog_store" / "installed.json").write_text(json.dumps({"installed": installed}), encoding="utf-8")


def _steam(root: Path) -> SteamPaths:
    userdata = root / "userdata" / "42"
    for d in (userdata / "config" / "grid", root / "config", root / "steamapps" / "compatdata"):
        d.mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=root,
        user_id="42",
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )


def _only(cands: list[adopt.AdoptionCandidate], kind: str) -> adopt.AdoptionCandidate:
    found = [c for c in cands if c.kind == kind]
    assert len(found) == 1, found
    return found[0]


def _manifest(game_dir: Path) -> dict:
    return json.loads((game_dir / MANIFEST_V2).read_text(encoding="utf-8"))


# --- Lutris slug ------------------------------------------------------------------------------


def test_lutris_yaml_named_by_configpath_gets_real_slug(home: Path) -> None:
    games = home / ".config" / "lutris" / "games"
    games.mkdir(parents=True)
    exe = _game(home / "Games" / "celeste")
    (games / "celeste-1612345678.yml").write_text(f"game:\n  exe: {exe}\nwine:\n  version: lutris-GE-Proton8-26\n")

    [g] = launcher_scan._scan_lutris(home)

    assert g.launch_cmd == ["lutris", "lutris:rungame/celeste"]
    assert g.name == "Celeste"
    assert g.runtime == "lutris-GE-Proton8-26"


def test_lutris_database_row_names_the_yaml_and_runs_by_id(home: Path) -> None:
    games = home / ".config" / "lutris" / "games"
    games.mkdir(parents=True)
    exe = _game(home / "Games" / "hk")
    (games / "hollow-knight-1700000000.yml").write_text(f"game:\n  exe: {exe}\n")
    db = home / ".local" / "share" / "lutris" / "pga.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE games (id INTEGER, name TEXT, slug TEXT, runner TEXT, directory TEXT, "
        "configpath TEXT, installed INTEGER)"
    )
    conn.execute(
        "INSERT INTO games VALUES (7, 'Hollow Knight', 'hollow-knight', 'wine', '', 'hollow-knight-1700000000', 1)"
    )
    conn.commit()
    conn.close()

    [g] = launcher_scan._scan_lutris(home)

    assert g.name == "Hollow Knight"
    assert g.launch_cmd == ["lutris", "lutris:rungameid/7"]
    assert g.game_id == "7"


# --- discovery --------------------------------------------------------------------------------


def test_each_source_kind_is_detected(home: Path) -> None:
    # Heroic (GOG)
    h_exe = _game(home / "Games" / "Heroic" / "Hades")
    h_pfx = home / "Games" / "Heroic" / "Prefixes" / "Hades"
    _prefix(h_pfx)
    _heroic(home, [("1234", "Hades", h_exe.parent, h_pfx)])
    # Lutris: game installed inside its prefix
    l_pfx = home / "Games" / "lutris-game"
    l_exe = _game(l_pfx / "drive_c" / "Program Files" / "Lutris Game")
    _prefix(l_pfx)
    lg = home / ".config" / "lutris" / "games"
    lg.mkdir(parents=True)
    (lg / "lutris-game-1700000001.yml").write_text(f"game:\n  exe: {l_exe}\n  prefix: {l_pfx}\n")
    # Bottles
    b_dir = home / ".local" / "share" / "bottles" / "bottles" / "B1"
    b_exe = _game(b_dir / "drive_c" / "Games" / "Bottled")
    _prefix(b_dir)
    (b_dir / "bottle.yml").write_text(
        "Name: B1\nRunner: soda-7.0\nExternal_Programs:\n  p1:\n    name: Bottled\n"
        "    path: 'C:\\Games\\Bottled\\game.exe'\n"
    )
    # Hydra: a game left inside drive_c of its prefix
    hy_pfx = home / ".config" / "hydralauncher" / "wine-prefixes" / "2405060"
    _game(hy_pfx / "drive_c" / "Games" / "Repacked")
    _prefix(hy_pfx)
    # umu: prefix without a recognizable game
    umu_pfx = home / "Games" / "umu" / "umu-1245620"
    _prefix(umu_pfx)

    cands = adopt.find_adoption_candidates(home=home)

    heroic = _only(cands, adopt.HEROIC)
    assert heroic.prefix_dir == h_pfx and heroic.proton_hint == "GE-Proton9-20" and heroic.store == "gog"
    hints = adopt._saves_hints(heroic.prefix_dir)
    assert hints and hints[0].name == "Saved Games"
    lutris = _only(cands, adopt.LUTRIS)
    assert lutris.in_drive_c and lutris.game_dir == l_exe.parent
    bottles = _only(cands, adopt.BOTTLES)
    assert bottles.in_drive_c and bottles.game_dir == b_exe.parent and bottles.runtime == "soda-7.0"
    hydra = _only(cands, adopt.HYDRA)
    assert hydra.in_drive_c and hydra.game_dir == hy_pfx / "drive_c" / "Games" / "Repacked"
    umu = _only(cands, adopt.UMU)
    assert umu.exe is None and adopt.W_NEEDS_GAME_FOLDER in umu.warnings and umu.steam_appid == 1245620
    assert adopt.plan_adoption(umu).blockers == [adopt.B_NEEDS_GAME_FOLDER]

    picked = adopt.candidate_with_folder(umu, _game(home / "Loose" / "Sekiro").parent)
    assert picked.exe is not None and picked.exe.name == "game.exe" and picked.name == "Sekiro"
    assert adopt.W_NEEDS_GAME_FOLDER not in picked.warnings


def test_hydra_database_record_is_read(home: Path) -> None:
    exe = _game(home / "abzim" / "KIBORG", "Kiborg.exe")
    _game(home / "abzim" / "Other")  # several games: abzim is a container, not the game folder
    db = home / ".config" / "hydralauncher" / "hydra-db"
    db.mkdir(parents=True)
    rec = {"objectId": "2405060", "title": "KIBORG", "shop": "steam", "executablePath": str(exe), "isDeleted": False}
    gone = {"objectId": "1", "title": "Gone", "shop": "steam", "executablePath": str(exe), "isDeleted": True}
    (db / "000003.log").write_bytes(
        b"\x01\x02!games!steam:2405060\x00\x10" + json.dumps(rec).encode()
        + b"\x00!games!steam:1\x05" + json.dumps(gone).encode()
        + b"\x00!games!steam:1\x05" + json.dumps(gone).encode()
    )

    c = _only(adopt.find_adoption_candidates(home=home), adopt.HYDRA)

    assert c.name == "KIBORG" and c.exe == exe and c.game_dir == exe.parent
    assert c.steam_appid == 2405060 and c.prefix_dir is None and adopt.W_NO_PREFIX in c.warnings


def test_steam_shortcut_detected_and_managed_games_skipped(home: Path) -> None:
    paths = _steam(home / ".steam" / "steam")
    exe = _game(home / "Games" / "OldShortcut")
    _game(home / "Games" / "Other")
    appid = steam_vdf.add_or_update_shortcut(paths, app_name="Old Shortcut", exe_path=exe)
    _prefix(paths.compatdata_dir / str(appid), steam_layout=True)
    # A game DeckInstall already manages is not offered again.
    managed_exe = _game(home / "Elsewhere" / "Managed")
    standalone.install_standalone_game("Managed", managed_exe, managed_exe.parent, keep_in_place=True)
    steam_vdf.add_or_update_shortcut(paths, app_name="Managed", exe_path=managed_exe.parent / "launch.sh")

    cands = adopt.find_adoption_candidates(home=home, paths=paths)

    c = _only(cands, adopt.STEAM)
    assert c.shortcut_appid == appid and c.prefix_dir == paths.compatdata_dir / str(appid)
    assert [x.name for x in cands] == ["Old Shortcut"]


# --- adopt: move, not copy --------------------------------------------------------------------


def _heroic_candidate(home: Path, name: str = "Hades") -> tuple[adopt.AdoptionCandidate, Path, Path]:
    exe = _game(home / "Games" / "Heroic" / name)
    pfx = home / "Games" / "Heroic" / "Prefixes" / name
    _prefix(pfx)
    _heroic(home, [("1234", name, exe.parent, pfx)])
    return _only(adopt.find_adoption_candidates(home=home), adopt.HEROIC), exe, pfx


def test_adopt_moves_game_and_prefix_and_writes_manifest(home: Path) -> None:
    c, exe, pfx = _heroic_candidate(home)
    save = pfx / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav"
    game_inode, save_inode = exe.stat().st_ino, save.stat().st_ino
    snaps: list[tuple[Path, str]] = []

    plan = adopt.plan_adoption(c)
    assert plan.ok and plan.prefix_action == "move" and all(m.same_fs for m in plan.moves)
    assert [m.what for m in plan.moves] == ["game", "prefix"]
    steps: list[str] = []
    result = adopt.adopt(plan, steps.append, lambda p, n, r, k: snaps.append((p, r)) or None)

    lib = library.internal_library_root()
    game = result.game
    assert game.game_dir == lib / "Games" / "Hades" and game.prefix_dir == lib / "Prefixes" / game.uid
    assert not exe.parent.exists() and not pfx.exists()  # moved, not copied
    assert game.exe_path.stat().st_ino == game_inode
    new_save = game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav"
    assert new_save.stat().st_ino == save_inode and new_save.read_text() == "progress"
    assert os.readlink(game.prefix_dir / "drive_c" / ".." / "dosdevices" / "c:") == "../drive_c"
    assert snaps == [(pfx, "adopt")]
    assert steps[0] == adopt.STEP_SNAPSHOT and steps[-1] == adopt.STEP_DONE

    m = _manifest(game.game_dir)
    assert m["game"]["dir"] == "Games/Hades" and m["game"]["exe"] == "game.exe"
    assert m["prefix"]["path"] == f"Prefixes/{game.uid}"
    assert m["source"]["kind"] == "adopted" and m["source"]["origin"] == "heroic"
    assert m["source"]["store"] == "gog" and m["source"]["codename"] == "1234"
    assert m["source"]["original_prefix"] == "~/Games/Heroic/Prefixes/Hades"
    assert (game.game_dir / "launch.sh").is_file() and game.desktop_file.is_file()
    assert [g.name for g in standalone.list_standalone_games()] == ["Hades"]
    # The launcher's own config is left untouched.
    assert (home / ".config" / "heroic" / "GamesConfig" / "1234.json").is_file()


def test_adopt_keep_in_place_only_moves_prefix(home: Path) -> None:
    c, exe, pfx = _heroic_candidate(home)
    plan = adopt.plan_adoption(c, options=adopt.AdoptOptions(move_game=False))
    assert [m.what for m in plan.moves] == ["prefix"]

    game = adopt.adopt(plan).game

    assert game.game_dir == exe.parent and exe.is_file() and not pfx.exists()
    assert _manifest(exe.parent)["prefix"]["path"].endswith(f"Prefixes/{game.uid}")


def test_cross_device_move_copies_then_removes_source(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, exe, pfx = _heroic_candidate(home)
    devs = iter(range(1000))
    monkeypatch.setattr(adopt, "_dev", lambda p: next(devs))
    plan = adopt.plan_adoption(c)
    assert not any(m.same_fs for m in plan.moves) and plan.bytes_to_copy > 1000 and plan.free_bytes

    def exdev(src: object, dst: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", exdev)
    game = adopt.adopt(plan).game

    assert not exe.parent.exists() and not pfx.exists()
    assert game.exe_path.read_bytes() == EXE
    assert os.readlink(game.prefix_dir / "dosdevices" / "c:") == "../drive_c"  # symlinks kept as links


def test_cross_device_without_space_is_blocked(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = _heroic_candidate(home)
    devs = iter(range(1000))
    monkeypatch.setattr(adopt, "_dev", lambda p: next(devs))
    monkeypatch.setattr(adopt.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    plan = adopt.plan_adoption(c)
    assert adopt.B_NO_SPACE in plan.blockers
    with pytest.raises(adopt.AdoptionError):
        adopt.adopt(plan)


def test_rollback_on_failure_leaves_source_intact(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, exe, pfx = _heroic_candidate(home)
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    plan = adopt.plan_adoption(c)

    def boom(**kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(adopt, "install_standalone_game", boom)
    with pytest.raises(adopt.AdoptionError) as err:
        adopt.adopt(plan)

    assert err.value.rolled_back and not err.value.problems
    after = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert after == before
    assert (pfx / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav").read_text() == "progress"
    lib = library.internal_library_root()
    assert not any((lib / "Games").iterdir()) and not any((lib / "Prefixes").iterdir())


def test_snapshot_failure_changes_nothing(home: Path) -> None:
    c, exe, pfx = _heroic_candidate(home)

    def fail(*a: object) -> None:
        raise OSError("no space for the snapshot")

    with pytest.raises(adopt.AdoptionError):
        adopt.adopt(adopt.plan_adoption(c), snapshot_fn=fail)
    assert exe.is_file() and pfx.is_dir()


# --- shared / symlinked prefixes --------------------------------------------------------------


def test_shared_prefix_is_not_moved_saves_are_copied(home: Path) -> None:
    shared = home / "Games" / "Heroic" / "Prefixes" / "default"
    _prefix(shared, save="shared-save")
    a = _game(home / "Games" / "Heroic" / "A")
    b = _game(home / "Games" / "Heroic" / "B")
    _heroic(home, [("a", "A", a.parent, shared), ("b", "B", b.parent, shared)])
    c = next(x for x in adopt.find_adoption_candidates(home=home) if x.name == "A")
    assert c.prefix_shared and adopt.W_SHARED_PREFIX in c.warnings

    plan = adopt.plan_adoption(c)
    assert plan.prefix_action == "copy_saves" and [m.what for m in plan.moves] == ["game"]
    result = adopt.adopt(plan)

    assert shared.is_dir() and (shared / "system.reg").is_file()
    copied = result.game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav"
    assert result.saves_copied == 1 and copied.read_text() == "shared-save"
    assert (shared / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav").is_file()


def test_symlinked_prefix_is_refused(home: Path) -> None:
    real = home / "elsewhere" / "pfx"
    _prefix(real)
    link = home / "Games" / "Heroic" / "Prefixes" / "Linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    exe = _game(home / "Games" / "Heroic" / "Linked")
    _heroic(home, [("l", "Linked", exe.parent, link)])
    c = _only(adopt.find_adoption_candidates(home=home), adopt.HEROIC)
    assert adopt.W_SYMLINK_PREFIX in c.warnings

    plan = adopt.plan_adoption(c)
    assert plan.prefix_action == "new" and [m.what for m in plan.moves] == ["game"]
    game = adopt.adopt(plan).game

    assert link.is_symlink() and (real / "system.reg").is_file()
    assert not (game.prefix_dir / "drive_c" / "users").exists()  # nothing read through the link


# --- Hydra: recover a game left in drive_c ----------------------------------------------------


def _hydra_drive_c(home: Path) -> tuple[adopt.AdoptionCandidate, Path, Path]:
    pfx = home / ".config" / "hydralauncher" / "wine-prefixes" / "2405060"
    exe = _game(pfx / "drive_c" / "Games" / "Repacked" / "bin", "Repacked.exe")
    _prefix(pfx)
    (pfx / "drive_c" / "Program Files" / "Common Files").mkdir(parents=True)
    return _only(adopt.find_adoption_candidates(home=home), adopt.HYDRA), exe, pfx


def test_hydra_game_in_drive_c_is_recovered(home: Path) -> None:
    c, exe, pfx = _hydra_drive_c(home)
    assert c.game_dir == pfx / "drive_c" / "Games" / "Repacked"

    plan = adopt.plan_adoption(c, options=adopt.AdoptOptions(move_game=False))  # forced out of drive_c
    assert [m.what for m in plan.moves] == ["game", "prefix"]
    game = adopt.adopt(plan).game

    lib = library.internal_library_root()
    assert game.game_dir == lib / "Games" / "Repacked"
    assert game.exe_path == game.game_dir / "bin" / "Repacked.exe" and game.exe_path.is_file()
    assert not pfx.exists()
    assert not (game.prefix_dir / "drive_c" / "Games" / "Repacked").exists()
    assert (game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games" / "Game" / "slot1.sav").is_file()
    m = _manifest(game.game_dir)
    assert m["game"]["exe"] == "bin/Repacked.exe" and m["source"]["origin"] == "hydra"


def test_hydra_rollback_puts_game_back_into_drive_c(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, exe, pfx = _hydra_drive_c(home)
    plan = adopt.plan_adoption(c)
    monkeypatch.setattr(adopt, "install_standalone_game", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))

    with pytest.raises(adopt.AdoptionError) as err:
        adopt.adopt(plan)

    assert err.value.rolled_back
    assert exe.read_bytes() == EXE and (pfx / "system.reg").is_file()


# --- Steam shortcuts --------------------------------------------------------------------------


def _steam_candidate(home: Path) -> tuple[adopt.AdoptionCandidate, SteamPaths, int, Path]:
    paths = _steam(home / ".steam" / "steam")
    exe = _game(home / "Games" / "OldShortcut")
    _game(home / "Games" / "Other")
    appid = steam_vdf.add_or_update_shortcut(paths, app_name="Old Shortcut", exe_path=exe)
    _prefix(paths.compatdata_dir / str(appid), steam_layout=True)
    return _only(adopt.find_adoption_candidates(home=home, paths=paths), adopt.STEAM), paths, appid, exe


def test_steam_prefix_layout_is_converted_and_linked(home: Path) -> None:
    c, paths, appid, exe = _steam_candidate(home)
    compat = paths.compatdata_dir / str(appid)
    plan = adopt.plan_adoption(c)
    assert plan.prefix_layout == "steam" and adopt.W_STEAM_SHORTCUT_BREAKS in plan.warnings

    result = adopt.adopt(plan, paths=paths)

    pfx = result.game.prefix_dir
    assert (pfx / "drive_c" / "users").is_dir() and (pfx / "version").is_file()
    assert compat.is_symlink() and compat.resolve() == pfx.resolve() and os.readlink(pfx / "pfx") == "."
    assert result.steam_link == compat and not result.removed_from_steam
    assert _manifest(result.game.game_dir)["created"]["steam"]["shortcut_appid"] == appid
    assert [e.appid_u32 for e in steam_vdf.load_shortcuts(paths.shortcuts_vdf)] == [appid]  # config untouched


def test_steam_shortcut_removed_on_request(home: Path) -> None:
    c, paths, appid, _ = _steam_candidate(home)
    result = adopt.adopt(adopt.plan_adoption(c, options=adopt.AdoptOptions(remove_from_steam=True)), paths=paths)

    assert result.removed_from_steam and result.steam_link is None
    assert steam_vdf.load_shortcuts(paths.shortcuts_vdf) == []
    assert not (paths.compatdata_dir / str(appid)).exists()


def test_steam_rollback_restores_compatdata(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, paths, appid, exe = _steam_candidate(home)
    compat = paths.compatdata_dir / str(appid)
    monkeypatch.setattr(adopt, "remove_shortcut", lambda p, a: False)

    with pytest.raises(adopt.AdoptionError) as err:
        adopt.adopt(adopt.plan_adoption(c, options=adopt.AdoptOptions(remove_from_steam=True)), paths=paths)

    assert err.value.rolled_back, err.value.problems
    assert exe.is_file() and not compat.is_symlink()
    assert (compat / "pfx" / "drive_c" / "users" / "steamuser").is_dir() and (compat / "version").is_file()
    assert not (compat / "pfx" / "version").exists()
    assert standalone.list_standalone_games() == []


# --- loose folder, exFAT --------------------------------------------------------------------


def test_loose_folder_and_exfat_library(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _game(home / "Downloads" / "Loose Game")
    c = adopt.candidate_from_folder(exe.parent)
    assert c.kind == adopt.FOLDER and c.exe == exe and adopt.W_NO_PREFIX in c.warnings

    disk = home / "media" / "sd" / "DeckInstall"
    disk.parent.mkdir(parents=True)
    monkeypatch.setattr(library, "fs_type_of", lambda p, table=None: "exfat" if "media" in str(p) else "ext4")
    plan = adopt.plan_adoption(c, library_root=disk)
    assert plan.prefix_location == "internal" and adopt.W_PREFIX_INTERNAL in plan.warnings
    game = adopt.adopt(plan).game

    assert game.game_dir == disk / "Games" / "Loose Game"
    assert game.prefix_dir == library.internal_library_root() / "Prefixes" / game.uid
    m = _manifest(game.game_dir)
    assert m["game"]["dir"] == "Games/Loose Game" and m["prefix"]["location"] == "internal"


def test_game_shipped_launch_sh_is_kept_and_restored_on_uninstall(home: Path) -> None:
    from deckinstall import uninstall

    c, exe, _pfx = _heroic_candidate(home)
    own = b"#!/bin/sh\n# the game's own launcher\n./game.exe \"$@\"\n"
    (exe.parent / "launch.sh").write_bytes(own)

    game = adopt.adopt(adopt.plan_adoption(c, options=adopt.AdoptOptions(move_game=False))).game

    script = game.game_dir / "launch.sh"
    backup = game.game_dir / "launch.sh.orig"
    assert backup.read_bytes() == own
    assert standalone.LAUNCHER_MARKER in script.read_text()
    assert _manifest(game.game_dir)["created"]["launch_script_backup"] == "~/Games/Heroic/Hades/launch.sh.orig"
    loaded = standalone.list_standalone_games()[0]
    assert loaded.meta["launch_script_backup"] == backup

    # Rewriting our launcher (presets, Proton) never touches the game's copy.
    standalone.refresh_integration_files(loaded)
    assert backup.read_bytes() == own

    plan = uninstall.plan_uninstall(loaded)
    uninstall.execute_uninstall(plan, [uninstall.SHORTCUTS])
    assert script.read_bytes() == own and not backup.exists()


def test_deckinstall_launch_sh_is_not_backed_up(home: Path) -> None:
    exe = _game(home / "Games" / "Native")
    game = standalone.install_standalone_game("Native", exe, exe.parent, keep_in_place=True)
    standalone.remove_standalone_game(game, delete_files=False, delete_prefix=False)
    # A leftover DeckInstall launcher (no manifest any more) is ours: overwritten, not kept.
    standalone._write_game_launcher(game)
    again = standalone.install_standalone_game("Native", exe, exe.parent, keep_in_place=True)
    assert "launch_script_backup" not in again.meta
    assert not (exe.parent / "launch.sh.orig").exists()


def test_adopt_rollback_restores_game_launch_sh(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, exe, pfx = _heroic_candidate(home)
    own = b"#!/bin/sh\necho mine\n"
    (exe.parent / "launch.sh").write_bytes(own)
    monkeypatch.setattr(adopt, "import_prefix_saves", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    plan = adopt.plan_adoption(c, options=adopt.AdoptOptions(move_game=False, move_prefix=False))
    with pytest.raises(adopt.AdoptionError):
        adopt.adopt(plan)
    assert (exe.parent / "launch.sh").read_bytes() == own
    assert not (exe.parent / "launch.sh.orig").exists()
