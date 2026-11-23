"""Switching a game between Steam mode and Standalone mode leaves nothing of the old mode behind."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QPushButton  # noqa: E402

from deckinstall import mode_switch, standalone, ui  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402
from deckinstall.modes import STANDALONE, STEAM  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import load_shortcuts  # noqa: E402
from deckinstall.ui_dialogs import configure_game_dialog as cgd  # noqa: E402
from tests.test_audit_phase2 import _drain, _make_window, _steam_paths  # noqa: E402


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


def _install(tmp_path: Path, options: str = "") -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / "Indie"
    src.mkdir(parents=True)
    (src / "game.exe").write_bytes(b"MZ")
    game = standalone.install_standalone_game(
        "Indie", src / "game.exe", src, launch_options=options, create_desktop_shortcut=True,
    )
    (game.prefix_dir / "drive_c").mkdir(parents=True, exist_ok=True)
    (game.prefix_dir / "drive_c" / "save.dat").write_text("progress", encoding="utf-8")
    return game


def _manifest(game: standalone.StandaloneGame) -> dict:
    return json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))


def test_round_trip_keeps_prefix_and_leaves_no_residue(tmp_path: Path) -> None:
    paths = _steam(tmp_path / "Steam")
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "sync_prefix_symlinks"),
    ):
        game = _install(tmp_path, "MANGOHUD=1 gamemoderun %command%")
        launchers = standalone.find_game_launchers(game)
        assert launchers["menu"] and launchers["desktop"]
        game = mode_switch.switch_to_steam(paths, game)

        assert _manifest(game)["runtime"]["mode"] == STEAM
        assert standalone.find_game_launchers(game) == {"menu": [], "desktop": []}
        entries = load_shortcuts(paths.shortcuts_vdf, strict=True)
        assert len(entries) == 1
        # Steam mode uses Steam's own overlay: MangoHud is not carried into the shortcut.
        assert "MANGOHUD" not in entries[0].launch_options and "mangohud" not in entries[0].launch_options
        assert "gamemoderun" in entries[0].launch_options
        compat = paths.compatdata_dir / str(entries[0].appid_u32)
        assert compat.is_symlink() and (compat / "pfx" / "drive_c" / "save.dat").read_text() == "progress"

        game = mode_switch.switch_to_standalone(paths, game)

        data = _manifest(game)
        assert data["runtime"]["mode"] == STANDALONE
        assert data["created"]["steam"] == {}
        assert load_shortcuts(paths.shortcuts_vdf, strict=True) == []
        assert not compat.exists() and not compat.is_symlink()
        assert standalone.find_game_launchers(game)["menu"]
        assert not standalone.find_game_launchers(game)["desktop"]
        assert (game.prefix_dir / "drive_c" / "save.dat").read_text() == "progress"
        assert "MANGOHUD=1" in game.launch_options  # the Standalone settings are kept


def test_switch_back_restores_the_steam_prefix_the_link_replaced(tmp_path: Path) -> None:
    paths = _steam(tmp_path / "Steam")
    with (
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
        mock.patch.object(standalone, "sync_prefix_symlinks"),
    ):
        game = _install(tmp_path)
        appid, _ = standalone.compute_shortcut_appid(f'"{game.exe_path}"', game.name)
        compat = paths.compatdata_dir / str(appid)
        (compat / "pfx" / "drive_c").mkdir(parents=True)
        (compat / "pfx" / "drive_c" / "old.sav").write_text("steam", encoding="utf-8")
        game.meta["steam"] = {}
        # A Steam prefix already sitting at the AppID is refused by a fresh add; simulate the
        # adopted case where it was kept aside as a backup by linking it directly.
        backup = standalone.link_compatdata_to_prefix(compat, game.prefix_dir)
        assert backup is not None
        game.meta["steam"] = {
            "user_id": "42",
            "shortcut_appid": appid,
            "compatdata_link": str(compat),
            "compatdata_backup": str(backup),
        }
        standalone._write_manifest(game)

        game = mode_switch.switch_to_standalone(paths, game)

        assert compat.is_dir() and not compat.is_symlink()
        assert (compat / "pfx" / "drive_c" / "old.sav").read_text() == "steam"
        assert not backup.exists()


def _configure_buttons(win, game, kind: str, monkeypatch) -> list:
    """Open Configure for `game` and return the mode-switch buttons it shows."""
    found: list = []
    labels = {tr("mode.btn_switch_steam"), tr("mode.btn_switch_standalone")}

    def fake_exec(dlg: QDialog) -> int:
        found.extend(b for b in dlg.findChildren(QPushButton) if b.text() in labels)
        return 0

    monkeypatch.setattr(win, "_selected_library_item", lambda: (kind, game))
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    cgd.open_configure_game_dialog(win)
    return found


def test_configure_offers_the_other_mode_and_greys_it_out_where_unavailable(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = _install(tmp_path)
        win = _make_window(tmp_path, app)
        try:
            # No Steam: a standalone game cannot move to Steam; the button says why.
            (btn,) = _configure_buttons(win, game, "standalone", monkeypatch)
            assert btn.text() == tr("mode.btn_switch_steam")
            assert not btn.isEnabled() and btn.toolTip() == tr("mode.note_steam_missing")

            win.paths = _steam_paths(tmp_path)
            (btn,) = _configure_buttons(win, game, "standalone", monkeypatch)
            assert btn.isEnabled()
            with mock.patch.object(win, "_switch_game_mode") as switch:
                btn.click()
            assert switch.call_args.args[:2] == (game, STEAM)

            # SteamOS only runs games through Steam: switching to Standalone is refused.
            win._steamos = True
            with mock.patch.object(ui.QMessageBox, "warning") as warn:
                win._switch_game_mode(game, STANDALONE)
            assert warn.call_args.args[2] == tr("mode.note_standalone_steamos")
        finally:
            win.paths = None
            _drain(win, app)
            win.close()

