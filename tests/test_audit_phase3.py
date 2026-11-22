"""Phase 3 regression tests: multi-source adoption merge, in-place Steam AppID preservation, relocation, and uninstall."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
import vdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import adopt, library, relocate, standalone, steam_vdf, ui_workers_adopt, uninstall  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.ui_dialogs import adopt_wizard  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _make_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    return h


def _steam_paths(root: Path, user_id: str = "42", other_users: tuple[str, ...] = ()) -> SteamPaths:
    for uid in (user_id, *other_users):
        (root / "userdata" / uid / "config" / "grid").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    userdata = root / "userdata" / user_id
    return SteamPaths(
        steam_root=root,
        user_id=user_id,
        userdata_dir=userdata,
        shortcuts_vdf=userdata / "config" / "shortcuts.vdf",
        grid_dir=userdata / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )


def _write_shortcuts(sv: Path, entries: list[tuple[int, str, Path, Path]]) -> None:
    payload: dict[str, dict[str, object]] = {}
    for idx, (appid_u32, name, exe, start_dir) in enumerate(entries):
        payload[str(idx)] = {
            "appid": appid_u32 - (1 << 32),
            "AppName": name,
            "Exe": f'"{exe}"',
            "StartDir": f'"{start_dir}"',
            "LaunchOptions": "",
        }
    sv.write_bytes(vdf.binary_dumps({"shortcuts": payload}))


def _make_game(folder: Path, exe_name: str = "game.exe") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / exe_name
    exe.write_bytes(PE)
    (folder / "data.pak").write_bytes(b"data" * 100)
    return exe


def _make_prefix(prefix: Path, *, steam_layout: bool = False, save_text: str | None = None) -> Path | None:
    root = prefix / "pfx" if steam_layout else prefix
    (root / "drive_c" / "users" / "steamuser" / "Saved Games").mkdir(parents=True, exist_ok=True)
    (root / "system.reg").write_text("WINE REGISTRY", encoding="utf-8")
    (root / "dosdevices").mkdir(parents=True, exist_ok=True)
    c_link = root / "dosdevices" / "c:"
    if not c_link.exists() and not c_link.is_symlink():
        c_link.symlink_to("../drive_c")
    if steam_layout:
        (prefix / "version").write_text("10.0-1\n", encoding="utf-8")
    if save_text is None:
        return None
    save_file = root / "drive_c" / "users" / "steamuser" / "Saved Games" / "slot1.sav"
    save_file.write_text(save_text, encoding="utf-8")
    return save_file


def _write_hydra_db(home: Path, rows: list[tuple[str, str, str, Path, Path | None]]) -> None:
    db_dir = home / ".config" / "hydralauncher" / "hydra-db"
    db_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[bytes] = []
    for oid, title, shop, exe, pfx in rows:
        rec = {
            "objectId": oid,
            "title": title,
            "shop": shop,
            "executablePath": str(exe),
            "winePrefixPath": str(pfx) if pfx is not None else "",
        }
        chunks.append(f"!games!{shop}:{oid}{json.dumps(rec)}".encode())
    (db_dir / "000001.log").write_bytes(b"\n".join(chunks))


def test_hydra_and_steam_merge_selects_newer_saves_and_imports_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path, monkeypatch)
    src_dir = home / "Games" / "CyberGame"
    exe = _make_game(src_dir, "CyberGame.exe")

    hydra_pfx = home / ".config" / "hydralauncher" / "wine-prefixes" / "1091500"
    hydra_save = _make_prefix(hydra_pfx, save_text="older-hydra")
    assert hydra_save is not None
    secondary_only = hydra_pfx / "drive_c" / "users" / "steamuser" / "Saved Games" / "extra.sav"
    secondary_only.write_text("extra-from-hydra", encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(hydra_save, (old_ts, old_ts))
    os.utime(secondary_only, (old_ts, old_ts))

    _write_hydra_db(home, [("1091500", "CyberGame", "steam", exe, hydra_pfx)])

    paths = _steam_paths(tmp_path / "steam")
    appid = 3111111111
    _write_shortcuts(paths.shortcuts_vdf, [(appid, "CyberGame", exe, src_dir)])
    steam_compat = paths.compatdata_dir / str(appid)
    steam_save = _make_prefix(steam_compat, steam_layout=True, save_text="newer-steam")
    assert steam_save is not None

    with mock.patch.object(adopt, "is_steam_running", return_value=False):
        cands = adopt.find_adoption_candidates(home=home, paths=paths)

    assert len(cands) == 1
    cand = cands[0]
    assert cand.kind == adopt.HYDRA
    assert cand.shortcut_appid == appid
    assert cand.steam_compat_dir == steam_compat
    assert cand.prefix_dir == steam_compat
    assert cand.extra_save_prefixes == (hydra_pfx,)

    opts = adopt.AdoptOptions(move_game=True, move_prefix=True, remove_from_steam=False)
    plan = adopt.plan_adoption(cand, library.internal_library_root(), opts)
    assert plan.ok
    assert adopt.W_STEAM_SHORTCUT_BREAKS in plan.warnings

    with (
        mock.patch.object(adopt, "is_steam_running", return_value=False),
        mock.patch.object(adopt, "is_gamescope_session", return_value=False),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        res = adopt.adopt(plan, snapshot_fn=lambda *a: None, paths=paths)

    assert res.shortcut_needs_repoint is True
    assert res.shortcut_appid == appid
    # Primary newer Steam save stays intact; secondary-only Hydra save was also imported.
    user_saves = res.game.prefix_dir / "drive_c" / "users" / "steamuser" / "Saved Games"
    assert (user_saves / "slot1.sav").read_text(encoding="utf-8") == "newer-steam"
    assert (user_saves / "extra.sav").read_text(encoding="utf-8") == "extra-from-hydra"
    assert (paths.compatdata_dir / str(appid)).is_symlink()
    assert (paths.compatdata_dir / str(appid)).resolve() == res.game.prefix_dir.resolve()
    assert res.game.meta["steam"]["shortcut_appid"] == appid
    # Rebuilt manifest still links back to the original Steam AppID.
    assert standalone.owned_shortcut_appids(res.game) == {appid}


def test_hydra_exact_object_id_does_not_match_prefix_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path, monkeypatch)
    exe = _make_game(home / "Games" / "ExactId")
    wrong_pfx = home / ".config" / "hydralauncher" / "wine-prefixes" / "prefix-120"
    _make_prefix(wrong_pfx, save_text="from-120")

    _write_hydra_db(home, [("12", "ExactId", "steam", exe, None)])

    cands = adopt.find_adoption_candidates(home=home, kinds=(adopt.HYDRA,))
    exact = [c for c in cands if c.game_id == "12"]
    assert len(exact) == 1
    assert exact[0].prefix_dir is None


def test_shared_steam_shortcut_other_users_sets_prefix_shared_and_skips_relink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path, monkeypatch)
    src_dir = home / "Games" / "SharedGame"
    exe = _make_game(src_dir)
    paths = _steam_paths(tmp_path / "steam", user_id="42", other_users=("99",))
    appid = 3222222222
    _write_shortcuts(paths.shortcuts_vdf, [(appid, "SharedGame", exe, src_dir)])
    _write_shortcuts(
        paths.steam_root / "userdata" / "99" / "config" / "shortcuts.vdf",
        [(appid, "SharedGame", exe, src_dir)],
    )
    steam_compat = paths.compatdata_dir / str(appid)
    _make_prefix(steam_compat, steam_layout=True, save_text="shared-save")

    cands = adopt.find_adoption_candidates(home=home, paths=paths, kinds=(adopt.STEAM,))
    assert len(cands) == 1
    cand = cands[0]
    assert cand.prefix_shared is True
    assert adopt.W_STEAM_OTHER_USERS in cand.warnings

    plan = adopt.plan_adoption(
        cand,
        library.internal_library_root(),
        adopt.AdoptOptions(move_game=True, move_prefix=True, remove_from_steam=False),
    )
    assert plan.prefix_action == "copy_saves"
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        res = adopt.adopt(plan, snapshot_fn=lambda *a: None, paths=paths)
    # Shared Steam compatdata remains a real directory untouched, not unlinked/replaced with a symlink.
    assert steam_compat.is_dir() and not steam_compat.is_symlink()
    assert res.steam_link is None


def test_adopt_remove_from_steam_defers_when_steam_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path, monkeypatch)
    src_dir = home / "Games" / "DeferredGame"
    exe = _make_game(src_dir)
    paths = _steam_paths(tmp_path / "steam")
    appid = 3333333333
    _write_shortcuts(paths.shortcuts_vdf, [(appid, "DeferredGame", exe, src_dir)])
    before_bytes = paths.shortcuts_vdf.read_bytes()

    cands = adopt.find_adoption_candidates(home=home, paths=paths, kinds=(adopt.STEAM,))
    assert len(cands) == 1
    plan = adopt.plan_adoption(
        cands[0],
        library.internal_library_root(),
        adopt.AdoptOptions(move_game=False, move_prefix=False, remove_from_steam=True),
    )
    with (
        mock.patch.object(adopt, "is_steam_running", return_value=True),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        res = adopt.adopt(plan, snapshot_fn=lambda *a: None, paths=paths)

    assert res.remove_shortcut_deferred is True
    assert res.removed_from_steam is False
    assert paths.shortcuts_vdf.read_bytes() == before_bytes


def test_adopt_wizard_merged_hydra_shows_steam_controls_and_pill_avoids_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_home(tmp_path, monkeypatch)
    app = QApplication.instance() or QApplication([])
    paths = _steam_paths(tmp_path / "steam")
    src_dir = tmp_path / "External" / "WizardGame"
    exe = _make_game(src_dir)
    cand = adopt.AdoptionCandidate(
        kind=adopt.HYDRA,
        name="WizardGame",
        exe=exe,
        game_dir=src_dir,
        prefix_dir=None,
        shortcut_appid=3444444444,
    )
    lib_root = library.internal_library_root()

    with mock.patch.object(ui_workers_adopt, "find_adoption_candidates", return_value=[cand]):
        dlg = adopt_wizard.AdoptWizard(paths, [("Internal", lib_root)])
        try:
            for _ in range(200):
                app.processEvents()
                if not dlg.busy() and dlg.tree.topLevelItemCount() > 0:
                    break
                time.sleep(0.01)
            assert dlg.tree.topLevelItemCount() == 1
            item = adopt_wizard._Item(cand=cand, opts=adopt.AdoptOptions(), library_root=lib_root)
            item.plan = adopt.plan_adoption(cand, lib_root, item.opts)
            dlg.items = [item]
            dlg._current = 0
            dlg._show_item(0)

            assert not dlg.steam_box.isHidden()
            assert not dlg.btn_shortcut_steam.isEnabled()

            dlg.rb_steam_remove.setChecked(True)
            app.processEvents()
            assert dlg.btn_shortcut_steam.isEnabled()

            with mock.patch.object(dlg, "_replan") as m_replan:
                dlg.btn_shortcut_menu.toggle()
                app.processEvents()
                m_replan.assert_not_called()
        finally:
            dlg.reject()
            for _ in range(50):
                app.processEvents()


def test_relocate_game_records_previous_dirs_and_matches_steam_shortcut_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path, monkeypatch)
    int_root = library.internal_library_root()
    ext_root = tmp_path / "sd" / "DeckInstall"
    library.ensure_library(int_root, internal=True)
    library.ensure_library(ext_root, internal=False)

    src_dir = tmp_path / "Downloads" / "RelocGame"
    exe = _make_game(src_dir, "Reloc.exe")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("RelocGame", exe, src_dir)
    old_dir = game.game_dir

    paths = _steam_paths(home / ".local" / "share" / "Steam")
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "find_steam_root", return_value=paths.steam_root),
    ):
        appid = standalone.add_installed_game_to_steam(paths, game.game_dir, game.uid)
    game = standalone.list_standalone_games()[0]

    target_lib = library.ensure_library(ext_root, internal=False)
    plan = relocate.plan_relocation(game, target_lib)
    assert plan.can_run
    moved = relocate.relocate_game(plan, snapshot_fn=lambda *a: None)

    assert moved.meta["prefix_location"] == "library"
    prev_dirs = moved.meta["source"]["previous_game_dirs"]
    assert any(Path(d) == old_dir or Path(os.path.expanduser(d)) == old_dir for d in prev_dirs)

    entries = steam_vdf.load_shortcuts(paths.shortcuts_vdf)
    owned = standalone.match_owned_shortcuts(entries, [moved], paths.user_id)
    assert appid in owned
    repairs = standalone.shortcut_path_repairs(entries, [moved], paths.user_id)
    assert len(repairs) == 1
    assert steam_vdf.update_shortcut_paths(
        paths, appid, moved.exe_path, standalone.repaired_shortcut_start_dir(repairs[0][0], moved),
        expected_exe=repairs[0][0].exe,
    )
    updated_entries = steam_vdf.load_shortcuts(paths.shortcuts_vdf)
    assert len(updated_entries) == 1
    assert updated_entries[0].appid_u32 == appid
    assert Path(steam_vdf._strip_vdf_quotes(updated_entries[0].exe)) == moved.exe_path
    assert (moved.game_dir / MANIFEST_V2).is_file()


def test_add_installed_game_to_steam_links_existing_and_cleans_pfx_on_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_home(tmp_path, monkeypatch)
    src_dir = tmp_path / "Downloads" / "LinkRollbackGame"
    exe = _make_game(src_dir, "LinkGame.exe")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("LinkRollbackGame", exe, src_dir)

    paths = _steam_paths(tmp_path / "steam")
    appid, _ = steam_vdf.compute_shortcut_appid(f'"{game.exe_path}"', game.name)
    _write_shortcuts(paths.shortcuts_vdf, [(appid, game.name, game.exe_path, game.exe_path.parent)])
    # Case 1: existing shortcut with missing compatdata -> links compatdata and updates manifest.
    assert standalone.add_installed_game_to_steam(paths, game.game_dir, game.uid) == appid
    compat = paths.compatdata_dir / str(appid)
    assert compat.is_symlink() and compat.resolve() == game.prefix_dir.resolve()
    reloaded = standalone.list_standalone_games()[0]
    assert reloaded.meta["steam"]["shortcut_appid"] == appid

    # Case 2: fresh game where import fails after creating compatdata & pfx -> rollback cleans both.
    src2 = tmp_path / "Downloads" / "RollbackFresh"
    exe2 = _make_game(src2, "Fresh.exe")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game2 = standalone.install_standalone_game("RollbackFresh", exe2, src2)
    paths2 = _steam_paths(tmp_path / "steam2")
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "sync_prefix_symlinks", side_effect=OSError("sync boom")),
        pytest.raises(OSError, match="sync boom"),
    ):
        standalone.add_installed_game_to_steam(paths2, game2.game_dir, game2.uid)

    appid2, _ = steam_vdf.compute_shortcut_appid(f'"{game2.exe_path}"', game2.name)
    assert not (paths2.compatdata_dir / str(appid2)).exists()
    assert not (paths2.compatdata_dir / str(appid2)).is_symlink()
    assert not (game2.prefix_dir / "pfx").exists()
    assert not (game2.prefix_dir / "pfx").is_symlink()


def test_execute_uninstall_uses_default_snapshot_and_removes_external_library_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_home(tmp_path, monkeypatch)
    ext_lib = library.ensure_library(tmp_path / "sd" / "DeckInstall", internal=False)
    src_dir = tmp_path / "Downloads" / "ExtUninstallGame"
    exe = _make_game(src_dir)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("ExtUninstallGame", exe, src_dir, library=ext_lib)
    game.cache_dir.mkdir(parents=True, exist_ok=True)
    (game.cache_dir / "shader.bin").write_bytes(b"cache")
    _make_prefix(game.prefix_dir, save_text="important-save")

    plan = uninstall.plan_uninstall(game)
    snap_out = tmp_path / "snap.zip"
    snap_out.write_bytes(b"PK")
    with mock.patch.object(uninstall.save_sync, "auto_snapshot", return_value=snap_out) as m_snap:
        res = uninstall.execute_uninstall(
            plan,
            [uninstall.SAVES, uninstall.PREFIX, uninstall.CACHE, uninstall.GAME_FILES, uninstall.SHORTCUTS],
            snapshot_fn=None,
        )

    m_snap.assert_called_once()
    assert res.snapshot == snap_out
    assert not game.prefix_dir.exists()
    assert not game.cache_dir.exists()
    assert not game.game_dir.exists()
