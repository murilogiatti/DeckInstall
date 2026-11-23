"""Phase 2 regression tests: UI safety guards, managed+Steam unification, Gaming Mode queuing."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import vdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QPushButton  # noqa: E402

from deckinstall import optiscaler as opti  # noqa: E402
from deckinstall import standalone, steam_vdf, ui, ui_workers_library  # noqa: E402
from deckinstall.launcher_scan import ExternalLauncherGame  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.steam_vdf import ShortcutEntry  # noqa: E402
from deckinstall.ui_dialogs import configure_game_dialog, optiscaler_bulk_dialog, save_sync_dialog  # noqa: E402
from deckinstall.vdf_queue import QueuedVdfEdit  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _drain(win: ui.MainWindow, app: QApplication) -> None:
    for _ in range(5):
        for _ in range(200):
            app.processEvents()
            if not any(w.isRunning() for w in win._active_workers):
                break
            for w in list(win._active_workers):
                w.wait(20)
        app.processEvents()


def _make_window(tmp_path: Path, app: QApplication) -> ui.MainWindow:
    with (
        mock.patch.object(
            ui, "QSettings", return_value=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        ),
        mock.patch.object(save_sync_dialog, "list_save_archives", return_value=[]),
        mock.patch.object(ui, "resolve_steam_paths", return_value=None),
        mock.patch.object(ui, "find_steam_root", return_value=None),
        mock.patch.object(ui, "scan_all_games", return_value=[]),
        mock.patch.object(ui, "scan_external_launcher_games", return_value=[]),
        mock.patch.object(ui.MainWindow, "_refresh_games"),
    ):
        win = ui.MainWindow(app)
    _drain(win, app)
    return win


def _steam_paths(tmp_path: Path) -> SteamPaths:
    root = tmp_path / "steam"
    user = root / "userdata" / "1"
    (user / "config").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True)
    paths = SteamPaths(
        steam_root=root,
        user_id="1",
        userdata_dir=user,
        shortcuts_vdf=user / "config" / "shortcuts.vdf",
        grid_dir=user / "config" / "grid",
        config_vdf=root / "config" / "config.vdf",
        compatdata_dir=root / "steamapps" / "compatdata",
    )
    paths.shortcuts_vdf.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    return paths


def test_delete_external_no_crash_and_managed_steam_passes_entry(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        ext = ExternalLauncherGame(
            source="heroic",
            name="Heroic Game",
            exe_path=tmp_path / "h.exe",
            game_dir=tmp_path,
            prefix_dir=None,
            runner="wine",
        )
        win._apply_shortcuts_data(None, [], [], [ext], "", runner_labels={})
        win.shortcuts_list.setCurrentCell(0, 0)
        win._remove_selected_shortcut()  # must return cleanly without AssertionError

        sa_dir = tmp_path / "src_managed"
        sa_dir.mkdir(parents=True)
        exe = sa_dir / "game.exe"
        exe.write_bytes(PE)
        with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
            sa = standalone.install_standalone_game("ManagedGame", exe, sa_dir)
        sc = ShortcutEntry("0", 3000001234, "ManagedGame", f'"{sa.exe_path}"', f'"{sa.game_dir}"', "", "")
        win.paths = _steam_paths(tmp_path)
        win._apply_shortcuts_data(win.paths, [sc], [sa], [], "", runner_labels={})
        win.shortcuts_list.setCurrentCell(0, 0)
        with mock.patch.object(win, "_uninstall_standalone_async") as uninst:
            win._remove_selected_shortcut()
        uninst.assert_called_once_with(sa, steam_entry=sc)
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win.close()


def test_native_elf_validates(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        elf = tmp_path / "game_linux"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
        elf.chmod(0o755)

        win.add_name.setText("Native Game")
        win.add_exe_combo.setEditText(str(elf))
        assert win._validate_add_inputs() == ("Native Game", elf)
    finally:
        _drain(win, app)
        win.close()


def test_configure_dialog_managed_steam_and_reset_default_proton(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        sa_dir = tmp_path / "src_cfg"
        sa_dir.mkdir(parents=True)
        exe1 = sa_dir / "game.exe"
        exe2 = sa_dir / "alt.exe"
        exe1.write_bytes(PE)
        exe2.write_bytes(PE)
        with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
            sa = standalone.install_standalone_game("ManagedSteam", exe1, sa_dir)
        paths = _steam_paths(tmp_path)
        appid = steam_vdf.add_or_update_shortcut(paths, "ManagedSteam", sa.exe_path, sa.game_dir, enable_proton=True)
        steam_vdf.set_shortcut_proton(paths.config_vdf, appid, "proton_experimental")
        sc = steam_vdf.load_shortcuts(paths.shortcuts_vdf)[0]

        win.paths = paths
        win._apply_shortcuts_data(paths, [sc], [sa], [], "", runner_labels={})
        win.shortcuts_list.setCurrentCell(0, 0)

        seen: dict[str, object] = {}

        def _inspect() -> None:
            modal = QApplication.activeModalWidget()
            if modal is None:
                QTimer.singleShot(10, _inspect)
                return
            combos = modal.findChildren(QComboBox)
            buttons = [b.text() for b in modal.findChildren(QPushButton)]
            seen["combo_count"] = len(combos)
            seen["buttons"] = buttons
            modal.accept()

        with (
            mock.patch.object(configure_game_dialog, "list_installed_protons", return_value=[("proton_experimental", "Proton Experimental")]),
            mock.patch.object(configure_game_dialog, "resolve_game_prefix", return_value=(sa.prefix_dir, None)),
            mock.patch.object(configure_game_dialog, "is_win32_prefix", return_value=False),
            mock.patch.object(
                configure_game_dialog,
                "get_umu_protonfix_status",
                return_value={"found": False, "has_protonfix": False},
            ),
        ):
            QTimer.singleShot(0, _inspect)
            configure_game_dialog.open_configure_game_dialog(win)

        assert seen["combo_count"] == 2  # exe_combo + proton_combo both present
        btn_texts = seen["buttons"]
        assert isinstance(btn_texts, list)
        assert not any("microSD" in t or "Compatdata" in t for t in btn_texts)

        # set_shortcut_proton with empty tool removes entry instead of writing "".
        assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == "proton_experimental"
        steam_vdf.set_shortcut_proton(paths.config_vdf, appid, "")
        assert steam_vdf.get_shortcut_proton(paths.config_vdf, appid) == ""
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win.close()


def test_optiscaler_bulk_attaches_owned_shortcut_to_managed_game(tmp_path: Path) -> None:
    paths = _steam_paths(tmp_path)
    sa_dir = tmp_path / "src_opti"
    sa_dir.mkdir(parents=True)
    exe = sa_dir / "game.exe"
    exe.write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        sa = standalone.install_standalone_game("OptiGame", exe, sa_dir)
    appid = steam_vdf.add_or_update_shortcut(
        paths, "OptiGame", sa.exe_path, sa.game_dir, launch_options="gamemoderun %command%"
    )

    with mock.patch.object(optiscaler_bulk_dialog, "list_standalone_games", return_value=[sa]):
        targets = optiscaler_bulk_dialog._collect_targets(paths)
    assert len(targets) == 1
    assert targets[0].source == "deckinstall"
    assert targets[0].steam_shortcut is not None and targets[0].steam_shortcut.appid_u32 == appid

    payload = tmp_path / "opti_payload"
    (payload / "D3D12_Optiscaler").mkdir(parents=True)
    (payload / "OptiScaler.dll").write_bytes(b"OPTI")
    (payload / "OptiScaler.ini").write_text("[Upscalers]\n", encoding="utf-8")
    (payload / "nvngx.dll").write_bytes(b"NV")
    (payload / "D3D12_Optiscaler" / "D3D12Core.dll").write_bytes(b"D3D")

    with mock.patch.object(opti, "ensure_optiscaler", return_value=payload):
        outcomes, writes = optiscaler_bulk_dialog._install_files(targets, remove=False, backend="auto")
    assert len(outcomes) == 1 and outcomes[0].status == "installed"
    assert len(writes) == 1 and writes[0][0] == "shortcut" and writes[0][1] == appid
    assert "dxgi=n,b" in writes[0][3] and "gamemoderun" in writes[0][3]


def test_gaming_mode_queues_edits_and_stops_watch_timer(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        paths = _steam_paths(tmp_path)
        exe = tmp_path / "Game" / "game.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(PE)
        win.paths = paths
        with mock.patch.object(ui, "is_gamescope_session", return_value=True), mock.patch.object(QMessageBox, "information"):
            win.add_name.setText("Queued Game")
            win.add_exe_combo.setEditText(str(exe))
            win.enqueue_vdf_edit(
                QueuedVdfEdit(
                    "add_installed_game:x", "add_installed_game", 0, "Queued Game",
                    {"game_dir": str(exe.parent), "uid": "x", "user_id": "0", "steam_root": str(tmp_path)},
                )
            )
            sc = ShortcutEntry("0", 3000007777, "Queued Game", f'"{exe}"', f'"{exe.parent}"', "", "")
            win._remove_steam_shortcut_async(sc)
            assert not win._vdf_watch_timer.isActive()
            with mock.patch.object(ui, "SteamRunningWorker") as srw:
                win._vdf_watch_tick()
            srw.assert_not_called()
        assert win._vdf_queue.pending_count() == 2

        # _auto_apply_vdf_queue must NOT offer Steam restart when Steam was not previously open.
        win._vdf_steam_open = False
        win._vdf_queue.apply_all = lambda _p: SimpleNamespace(applied=2, errors=[])  # type: ignore[method-assign]
        with (
            mock.patch.object(ui, "is_steam_running", return_value=False),
            mock.patch.object(win, "_offer_steam_restart") as offer,
            mock.patch.object(win, "_refresh_shortcuts"),
        ):
            win._auto_apply_vdf_queue()
            _drain(win, app)
        offer.assert_not_called()
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win.close()


def test_remove_shortcut_worker_restarts_before_done(tmp_path: Path) -> None:
    events: list[str] = []
    paths = _steam_paths(tmp_path)
    appid = steam_vdf.add_or_update_shortcut(paths, "RM", tmp_path / "rm.exe", tmp_path)
    sc = steam_vdf.load_shortcuts(paths.shortcuts_vdf)[0]
    worker = ui_workers_library.RemoveShortcutWorker(paths, sc)
    assert worker.restart_failed is False and worker.restart_if_was_running is True
    worker.done.connect(lambda ok: events.append(f"done:{ok}"))
    with (
        mock.patch.object(ui_workers_library, "is_steam_running", side_effect=[True, False]),
        mock.patch.object(ui_workers_library, "shutdown_steam", return_value=True),
        mock.patch.object(ui_workers_library, "start_steam", side_effect=lambda: (events.append("start"), False)[1]),
    ):
        worker.run()
    assert events == ["start", "done:True"]
    assert worker.restart_failed is True
    assert steam_vdf.load_shortcuts(paths.shortcuts_vdf) == []
    assert appid


def test_filter_clears_hidden_selection_and_updates_actions(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    win = _make_window(tmp_path, app)
    try:
        ext = ExternalLauncherGame(
            source="heroic",
            name="OnlyHeroic",
            exe_path=tmp_path / "h.exe",
            game_dir=tmp_path,
            prefix_dir=None,
            runner="wine",
        )
        win._apply_shortcuts_data(None, [], [], [ext], "", runner_labels={})
        win.shortcuts_list.setCurrentCell(0, 0)
        assert win._selected_library_item() is not None
        assert win.btn_configure_game.isEnabled()

        win._on_library_source_filter_changed("steam")
        assert win._selected_library_item() is None
        assert not win.btn_configure_game.isEnabled()
    finally:
        win._vdf_queue.clear()
        _drain(win, app)
        win.close()
