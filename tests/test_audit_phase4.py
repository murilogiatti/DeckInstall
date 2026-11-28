"""Phase 4 regression tests: SteamOS Proton policy, bulk Proton dialog redesign, runtime uninstall & health."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import vdf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtCore import QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from deckinstall import library, proton_policy, standalone, steam_vdf, ui, ui_workers, ui_workers_maint  # noqa: E402
from deckinstall import runtime_manager as rm  # noqa: E402
from deckinstall import runtime_uninstall as ru  # noqa: E402
from deckinstall.steam_paths import SteamPaths  # noqa: E402
from deckinstall.ui_dialogs import proton_bulk_dialog as pbd  # noqa: E402
from deckinstall.ui_dialogs import save_sync_dialog  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    return h


def _steam_paths(root: Path) -> SteamPaths:
    user = root / "userdata" / "1"
    (user / "config" / "grid").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "steamapps" / "compatdata").mkdir(parents=True, exist_ok=True)
    sv = user / "config" / "shortcuts.vdf"
    sv.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    cfg = root / "config" / "config.vdf"
    cfg.write_text('"InstallConfigStore"\n{\n}\n', encoding="utf-8")
    return SteamPaths(
        steam_root=root,
        user_id="1",
        userdata_dir=user,
        shortcuts_vdf=sv,
        grid_dir=user / "config" / "grid",
        config_vdf=cfg,
        compatdata_dir=root / "steamapps" / "compatdata",
    )


def _install_game(tmp_path: Path, name: str, proton_name: str = "") -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True, exist_ok=True)
    exe = src / "Game.exe"
    exe.write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, exe, src, proton_name=proton_name)


def _make_tool(name: str, *, managed: bool = True) -> Path:
    tool = rm.get_compat_tools_dir() / name
    tool.mkdir(parents=True, exist_ok=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (tool / "compatibilitytool.vdf").write_text(
        f'"compatibilitytools"\n{{\n  "compat_tools"\n  {{\n    "{name}"\n    {{\n      "install_path" "."\n    }}\n  }}\n}}\n',
        encoding="utf-8",
    )
    if managed:
        (tool / ".deckinstall-managed").write_text(f"{name}\n", encoding="utf-8")
    return tool


def test_batch_proton_visible_without_steam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    app = QApplication.instance() or QApplication([])
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
        try:
            win._update_steam_mode_ui()
            win._apply_shortcuts_data(None, [], [], [], "", None)
            assert win.act_bulk_proton.isVisible()
            assert win.act_bulk_proton.isEnabled()
        finally:
            for w in list(win._active_workers):
                w.wait(100)
            win.close()


def test_bulk_proton_steamos_defaults_no_download_confirm_and_single_row_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    app = QApplication.instance() or QApplication([])
    paths = _steam_paths(tmp_path / "steam")
    official = paths.steam_root / "steamapps" / "common" / "Proton - Experimental"
    official.mkdir(parents=True)
    (official / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    game_a = _install_game(tmp_path, "Alpha", proton_name="")
    game_b = _install_game(tmp_path, "Beta", proton_name="")
    appid_a = 3100000001
    paths.shortcuts_vdf.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": appid_a - (1 << 32),
                        "AppName": game_a.name,
                        "Exe": f'"{game_a.exe_path}"',
                        "StartDir": f'"{game_a.exe_path.parent}"',
                        "LaunchOptions": "",
                    }
                }
            }
        )
    )
    steam_vdf.bulk_migrate_compat_tool(paths.config_vdf, [appid_a], "proton_9")

    class _FakeWin:
        def __init__(self) -> None:
            self.paths = paths
            self._steam_root_found = True
            self._closing_pending = False
            self.status_label = SimpleNamespace(setText=lambda _t: None)
            self._workers: list[object] = []

        def _track_worker(self, w: object) -> None:
            self._workers.append(w)

        def _refresh_shortcuts(self) -> None:
            pass

        def should_queue_vdf_edits(self) -> bool:
            return False

        def _is_auto_snapshot_enabled(self) -> bool:
            return False

        def _auto_snapshot_if_enabled(self, *a: object, **k: object) -> None:
            pass

        def _begin_busy(self) -> None:
            pass

        def _end_busy(self) -> None:
            pass

        def _confirm_close_steam_async(self, cb, on_cancelled=None, parent=None, alive_fn=None) -> None:  # noqa: ANN001
            cb()

    win = _FakeWin()
    rows, orphans, newest = pbd.collect_rows(paths)
    by_name = {r.name: r for r in rows}
    # Unified row keeps BOTH game_dir and steam_tools for managed Steam shortcuts.
    assert by_name["Alpha"].game_dir == game_a.game_dir
    assert by_name["Alpha"].steam_tools == ((appid_a, "proton_9"),)
    target_info = pbd.collect_target_options(paths, newest)
    assert target_info[0][0][0] == pbd.STEAM_DEFAULT_PROTON_ID

    with (
        mock.patch.object(pbd, "_dialog_parent", return_value=None),
        mock.patch.object(pbd, "ensure_newest_default_proton") as m_dl,
        mock.patch.object(steam_vdf, "is_steam_running", return_value=False),
        mock.patch("deckinstall.ui_workers.is_steam_running", return_value=False),
        mock.patch.object(QMessageBox, "information"),
    ):
        def _drive() -> None:
            dlg = next(w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible())
            assert dlg.cmb_target.currentData() == pbd.STEAM_DEFAULT_PROTON_ID  # type: ignore[attr-defined]
            # Unchecked Beta is not selected for STEAM_DEFAULT_PROTON_ID; Alpha (shortcut on proton_9) is checked.
            assert dlg.tbl.item(0, 0).checkState() == Qt.CheckState.Checked  # type: ignore[attr-defined]
            assert dlg.tbl.item(1, 0).checkState() == Qt.CheckState.Unchecked  # type: ignore[attr-defined]

            # Switching to Download GE on SteamOS asks confirmation with default No -> No skips download.
            dl_idx = dlg.cmb_target.findData(pbd.DOWNLOAD_NEWEST_PROTON_ID)  # type: ignore[attr-defined]
            assert dl_idx >= 0
            dlg.cmb_target.setCurrentIndex(dl_idx)  # type: ignore[attr-defined]
            app.processEvents()
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as m_q:
                dlg.btn_update.click()  # type: ignore[attr-defined]
                app.processEvents()
                assert m_q.call_args.args[4] == QMessageBox.StandardButton.No
                m_dl.assert_not_called()

            # Sorting before switching the target must keep the checked shortcut attached to Alpha.
            dlg.tbl.sortItems(0, Qt.SortOrder.DescendingOrder)  # type: ignore[attr-defined]
            assert dlg.tbl.item(0, 0).text() == "Beta"  # type: ignore[attr-defined]

            # Switch back to Steam Default -> clears config.vdf mapping with zero downloads.
            dlg.cmb_target.setCurrentIndex(0)  # type: ignore[attr-defined]
            app.processEvents()
            dlg.btn_update.click()  # type: ignore[attr-defined]
            tries = [0]

            def _close_when_written() -> None:  # a slow CI runner may take longer than any fixed delay
                if steam_vdf.get_compat_tool_map(paths.config_vdf) and tries[0] < 200:
                    tries[0] += 1
                    QTimer.singleShot(25, _close_when_written)
                    return
                dlg.accept()

            QTimer.singleShot(25, _close_when_written)

        QTimer.singleShot(50, _drive)
        pbd._show(win, rows, orphans, newest, target_info)  # type: ignore[arg-type]
        for w in list(win._workers):
            if hasattr(w, "wait"):
                w.wait(2000)
        app.processEvents()
        m_dl.assert_not_called()

    assert steam_vdf.get_compat_tool_map(paths.config_vdf) == {}
    # Beta (unrelated standalone game) stays untouched in Default mode.
    reloaded = {g.name: g for g in standalone.list_standalone_games()}
    assert reloaded[game_b.name].proton_name == ""

    # ensure_newest_default_proton(retarget_defaults=False) does not invoke global retargeting.
    _make_tool("GE-Proton11-1")
    with (
        mock.patch.object(pbd, "check_runtime_updates", return_value={"umu_proton": {"update_available": False, "installed": True}}),
        mock.patch.object(pbd, "retarget_default_games") as m_retarget,
    ):
        assert pbd.ensure_newest_default_proton(retarget_defaults=False) == "GE-Proton11-1"
        m_retarget.assert_not_called()


def test_standalone_install_worker_steam_only_skips_downloads_and_keeps_default_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    src = tmp_path / "Downloads" / "SteamOnlyGame"
    src.mkdir(parents=True)
    exe = src / "Game.exe"
    exe.write_bytes(PE)

    done_games: list[standalone.StandaloneGame] = []
    worker = ui_workers.StandaloneInstallWorker(
        app_name="SteamOnlyGame",
        exe_path=exe,
        steam_appid=None,
        create_menu_shortcut=False,
        create_desktop_shortcut=False,
        game_dir=src,
        prepare_standalone_runtime=False,
    )
    worker.finished_ok.connect(lambda g, _b: done_games.append(g))
    with (
        mock.patch.object(ui_workers, "ensure_umu_run") as m_umu,
        mock.patch.object(ui_workers, "ensure_umu_proton") as m_ge,
        mock.patch.object(standalone, "resolve_game_icon", return_value=None),
    ):
        worker.run()
        m_umu.assert_not_called()
        m_ge.assert_not_called()
    assert len(done_games) == 1
    assert done_games[0].proton_name == ""
    assert proton_policy.is_default(done_games[0])


def test_resolve_standalone_proton_fallback_does_not_pin_default_games(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    ge = _make_tool("GE-Proton11-5")
    default_game = _install_game(tmp_path, "DefGame", proton_name="")
    resolved_dir, fallback_pin = standalone.resolve_standalone_proton_fallback(default_game)
    assert resolved_dir == ge
    assert fallback_pin is None

    pinned_missing = _install_game(tmp_path, "PinGame", proton_name="GE-Proton9-99")
    resolved_dir2, fallback_pin2 = standalone.resolve_standalone_proton_fallback(pinned_missing)
    assert resolved_dir2 == ge
    assert fallback_pin2 == "GE-Proton11-5"


def test_system_mangohud_and_symlinked_uninstall_mangohud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sys = tmp_path / "sysdata"
    layer_dir = real_sys / "vulkan" / "implicit_layer.d"
    layer_dir.mkdir(parents=True)
    lib64 = tmp_path / "usr" / "lib64" / "mangohud"
    lib64.mkdir(parents=True)
    (lib64 / "libMangoHud.so").write_bytes(b"ELF")
    (layer_dir / "MangoHud.json").write_text(
        json.dumps({"layer": {"library_path": str(tmp_path / "usr" / "$LIB" / "mangohud" / "libMangoHud.so")}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_DIRS", str(real_sys))
    assert rm.system_mangohud_installed() is True

    # Symlinked XDG_DATA_HOME still recognizes and removes DeckInstall's MangoHud layers cleanly.
    real_data = tmp_path / "real_data"
    link_data = tmp_path / "link_data"
    real_data.mkdir()
    link_data.symlink_to(real_data)
    monkeypatch.setenv("XDG_DATA_HOME", str(link_data))

    mh_dir = rm.get_mangohud_dir()
    (mh_dir / "lib64").mkdir(parents=True)
    (mh_dir / "lib64" / "libMangoHud.so").write_bytes(b"ELF")
    user_layers = rm.get_user_vulkan_layer_dir()
    user_layers.mkdir(parents=True)
    (user_layers / "MangoHud.x86_64.json").write_text(
        json.dumps({"layer": {"library_path": str(mh_dir / "lib64" / "libMangoHud.so")}}),
        encoding="utf-8",
    )
    (user_layers / "MangoHud.x86.json").write_text(
        json.dumps({"layer": {"library_path": "/usr/lib32/libMangoHud.so"}}),
        encoding="utf-8",
    )
    assert rm.uninstall_mangohud() is True
    assert not (user_layers / "MangoHud.x86_64.json").exists()
    assert (user_layers / "MangoHud.x86.json").exists()


def test_non_ge_compat_tool_managed_and_orphan_mangohud_in_runtime_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (
            ("Proton-CachyOS-10/proton", b"#!/bin/sh\n"),
            ("Proton-CachyOS-10/compatibilitytool.vdf", b'"compatibilitytools"\n{}\n'),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    tar_bytes = buf.getvalue()
    digest = hashlib.sha256(tar_bytes).hexdigest()

    rel = rm.ProtonRelease(
        family_id="cachyos",
        tag_name="Proton-CachyOS-10",
        name="Proton-CachyOS-10",
        asset_name="tool.tar.gz",
        download_url="https://github.com/CachyOS/proton-cachyos/releases/download/10/tool.tar.gz",
        sha256_digest=digest,
    )
    with mock.patch.object(rm, "_download_file", side_effect=lambda _u, dst, _l, **k: dst.write_bytes(tar_bytes)):
        installed_dir = rm.install_compat_tool(rel)
    assert (installed_dir / ".deckinstall-managed").is_file()
    assert rm.is_managed_compat_tool(installed_dir)

    # Also add an orphan MangoHud layer pointing at DeckInstall's MangoHud dir even after dir deletion.
    mh_dir = rm.get_mangohud_dir()
    user_layers = rm.get_user_vulkan_layer_dir()
    user_layers.mkdir(parents=True, exist_ok=True)
    (user_layers / "MangoHud.x86_64.json").write_text(
        json.dumps({"layer": {"library_path": str(mh_dir / "lib64" / "libMangoHud.so")}}),
        encoding="utf-8",
    )

    items = {i.name: i for i in ru.list_runtimes(None)}
    assert "Proton-CachyOS-10" in items
    assert "MangoHud" in items
    removed, errors = ru.uninstall([items["Proton-CachyOS-10"], items["MangoHud"]])
    assert sorted(removed) == ["MangoHud", "Proton-CachyOS-10"] and errors == []
    assert not installed_dir.exists()
    assert not (user_layers / "MangoHud.x86_64.json").exists()


def test_retarget_without_ge_and_repair_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    for n in ("GE-Proton11-2", "GE-Proton11-3"):
        _make_tool(n, managed=True)

    # Retarget with newest_default_proton() == None still clears dead PROTONPATH from launch.sh.
    game = _install_game(tmp_path, "LaunchRefresh", proton_name="")
    game.launch_script.write_text("export PROTONPATH='/gone/GE-Proton11-3'\n", encoding="utf-8")
    for d in list(rm.get_compat_tools_dir().iterdir()):
        if d.is_dir():
            import shutil

            shutil.rmtree(d)
    assert proton_policy.newest_default_proton() is None
    assert proton_policy.retarget_default_games("") == 1
    assert "/gone/GE-Proton11-3" not in game.launch_script.read_text(encoding="utf-8")

    # repair_managed_proton preserves existing folder if replacement fails.
    broken = _make_tool("GE-Proton11-9", managed=True)
    marker = broken / "keep-on-fail.txt"
    marker.write_text("precious", encoding="utf-8")
    with (
        mock.patch.object(rm, "fetch_available_proton_releases", side_effect=RuntimeError("offline")),
        mock.patch.object(rm, "ensure_umu_proton", side_effect=RuntimeError("offline")),
        pytest.raises(RuntimeError, match="offline"),
    ):
        rm.repair_managed_proton(broken)
    assert marker.is_file()


def test_runtime_update_isolates_optiscaler_and_maint_health_counts_external_libs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    umu_dir = rm.get_umu_dir()
    umu_dir.mkdir(parents=True)
    (umu_dir / "umu-run").write_bytes(b"U" * 100)
    ge = _make_tool("GE-Proton11-2")
    opti = rm.get_optiscaler_dir()
    opti.mkdir(parents=True)
    (opti / "OptiScaler.dll").write_bytes(b"D" * 50)
    mh = rm.get_mangohud_dir()
    (mh / "lib64").mkdir(parents=True)
    (mh / "lib64" / "libMangoHud.so").write_bytes(b"M" * 40)

    ext_lib = library.ensure_library(tmp_path / "sd" / "DeckInstall", internal=False)
    pfx = ext_lib.prefixes_dir / "uid-ext"
    pfx.mkdir(parents=True)
    (pfx / "save.bin").write_bytes(b"S" * 1234)

    # RuntimeUpdateWorker finishes cleanly even if ensure_optiscaler raises.
    worker = ui_workers.RuntimeUpdateWorker(paths=None)
    done_msgs: list[str] = []
    worker.finished_ok.connect(lambda m, _w: done_msgs.append(m))
    with (
        mock.patch.object(
            ui_workers,
            "check_runtime_updates",
            return_value={
                "umu": {"installed": "0.1", "latest": "0.1", "update_available": False, "installed_path": str(umu_dir / "umu-run")},
                "umu_proton": {"installed": "GE-Proton11-2", "latest": "GE-Proton11-2", "update_available": False, "installed_path": str(ge)},
            },
        ),
        mock.patch.object(ui_workers, "ensure_optiscaler", side_effect=RuntimeError("opti github 503")),
        mock.patch.object(rm, "ensure_umu_database", return_value=None),
        mock.patch.object(ui_workers.RuntimeUpdateWorker, "_update_mangohud", return_value="MangoHud v0.8.4"),
        mock.patch.object(ui_workers, "check_ubuntu_bwrap_userns", return_value=""),
    ):
        worker.run()
    assert len(done_msgs) == 1
    assert "opti github 503" in done_msgs[0]

    # MaintHealthWorker counts external library Prefixes and non-duplicated runtime folders.
    results: list[dict] = []
    hw = ui_workers_maint.MaintHealthWorker(1, None, 0, find_steam_root_fn=lambda: None)
    hw.ready.connect(lambda _req, data: results.append(data))
    hw.run()
    assert results and results[0]["prefixes_bytes"] >= 1234
    assert results[0]["runtimes_bytes"] >= 190


def test_run_exe_in_prefix_worker_fails_on_nonzero_and_steam_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    pfx = tmp_path / "pfx"
    exe = tmp_path / "setup.exe"
    exe.write_bytes(PE)

    steam_root = tmp_path / "steam"
    proton_dir = steam_root / "steamapps" / "common" / "Proton - Experimental"
    proton_dir.mkdir(parents=True)
    (proton_dir / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    # Missing umu-run with allow_download=False raises cleanly without downloading.
    with (
        mock.patch("deckinstall.steam_paths.find_steam_root", return_value=steam_root),
        pytest.raises(RuntimeError, match="umu-run"),
    ):
        rm.run_exe_in_prefix(exe, pfx, allow_download=False)

    # With umu-run present and no GE-Proton, falls back to official Steam Proton dir.
    umu_dir = rm.get_umu_dir()
    umu_dir.mkdir(parents=True)
    (umu_dir / "umu-run").write_text("#!/bin/sh\n", encoding="utf-8")
    with (
        mock.patch("deckinstall.steam_paths.find_steam_root", return_value=steam_root),
        mock.patch.object(rm.subprocess, "run", return_value=SimpleNamespace(returncode=2)) as m_sub,
    ):
        rc = rm.run_exe_in_prefix(exe, pfx, allow_download=False)
    assert rc == 2
    assert m_sub.call_args.kwargs["env"]["PROTONPATH"] == str(proton_dir)

    # Worker emits failed on nonzero rc.
    failures: list[str] = []
    w = ui_workers.RunExeInPrefixWorker(pfx, exe, proton_dir=proton_dir)
    w.failed.connect(failures.append)
    with mock.patch.object(ui_workers, "run_exe_in_prefix", return_value=3):
        w.run()
    assert failures and "3" in failures[0]
