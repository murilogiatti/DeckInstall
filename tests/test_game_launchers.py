"""Fase 8.4: desktop/app-menu launchers in Configure game, detected from disk and kept in the manifest."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import standalone  # noqa: E402
from deckinstall import uninstall as un  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _install(tmp_path: Path) -> standalone.StandaloneGame:
    src = tmp_path / "src" / "Alpha"
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game("Alpha", src / "Game.exe", src, create_menu_shortcut=False)


def _reload(game: standalone.StandaloneGame) -> standalone.StandaloneGame:
    manifest = standalone.find_manifest(game.game_dir)
    assert manifest is not None
    loaded = standalone._load_manifest(manifest)
    assert loaded is not None
    return loaded


def test_launchers_follow_disk_and_manifest(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    with (
        mock.patch.object(standalone, "get_desktop_dir", return_value=desktop),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        game = _install(tmp_path)
        assert standalone.find_game_launchers(game) == {"menu": [], "desktop": []}

        standalone.set_game_launcher(game, "desktop", True)
        standalone.set_game_launcher(game, "menu", True)
        found = standalone.find_game_launchers(game)
        assert len(found["desktop"]) == 1 and len(found["menu"]) == 1
        assert os.access(found["desktop"][0], os.X_OK)
        loaded = _reload(game)
        assert loaded.desktop_shortcut == found["desktop"][0]
        assert loaded.desktop_file == found["menu"][0]
        removed = {p for i in un.plan_uninstall(loaded).items for p in i.paths}
        assert {found["desktop"][0], found["menu"][0]} <= removed  # uninstall still removes them

        # A file the user deleted by hand shows as unchecked; a hand-made one that opens the game shows checked.
        found["desktop"][0].unlink()
        (desktop / "mine.desktop").write_text(f"[Desktop Entry]\nExec={game.launch_script}\n", encoding="utf-8")
        (desktop / "other.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/true\n", encoding="utf-8")
        assert standalone.find_game_launchers(game)["desktop"] == [desktop / "mine.desktop"]

        standalone.set_game_launcher(game, "desktop", False)
        standalone.set_game_launcher(game, "menu", False)
        assert standalone.find_game_launchers(game) == {"menu": [], "desktop": []}
        assert (desktop / "other.desktop").is_file()  # other launchers are never touched
        loaded = _reload(game)
        assert loaded.desktop_shortcut is None and not loaded.desktop_file.exists()


def test_configure_checkboxes_reflect_and_toggle(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog

    from deckinstall.ui_dialogs import configure_game_dialog as cgd

    app = QApplication.instance() or QApplication([])
    desktop = tmp_path / "Desktop"
    with (
        mock.patch.object(standalone, "get_desktop_dir", return_value=desktop),
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        game = _install(tmp_path)
        standalone.set_game_launcher(game, "menu", True)
        win = SimpleNamespace(status_label=mock.Mock())
        dlg = QDialog()
        try:
            box = cgd._launcher_checkboxes(win, dlg, game)  # type: ignore[arg-type]
            chk_desktop, chk_menu = box.findChildren(QCheckBox)
            assert not chk_desktop.isChecked() and chk_menu.isChecked()
            chk_desktop.setChecked(True)
            chk_menu.setChecked(False)
            found = standalone.find_game_launchers(game)
            assert len(found["desktop"]) == 1 and found["menu"] == []
        finally:
            dlg.deleteLater()
            app.processEvents()
