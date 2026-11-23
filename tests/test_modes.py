"""Steam mode vs Standalone mode: one rule for every screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from deckinstall import modes
from deckinstall.i18n import tr
from deckinstall.modes import STANDALONE, STEAM, ModeEnv, feature_note, mode_of_kind


@pytest.mark.parametrize(
    ("text", "steamos"),
    [
        ('NAME="SteamOS"\nID=steamos\nID_LIKE=arch\n', True),
        ("ID=bazzite\nID_LIKE=fedora\n", False),
        ('ID="ubuntu"\n', False),
        ("", False),
    ],
)
def test_steamos_is_read_from_os_release(tmp_path: Path, text: str, steamos: bool) -> None:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    assert modes.is_steamos(path) is steamos


def test_missing_os_release_is_not_steamos(tmp_path: Path) -> None:
    assert modes.is_steamos(tmp_path / "nope") is False
    assert modes.is_steamos() is False  # conftest points the default at a missing file


def test_modes_offered_per_system() -> None:
    steamos = ModeEnv(steamos=True, steam=True)
    assert steamos.default_mode == STEAM and steamos.mode_note(STEAM) == ""
    assert steamos.mode_note(STANDALONE) == tr("mode.note_standalone_steamos")
    desktop = ModeEnv(steamos=False, steam=True)
    assert desktop.default_mode == STEAM
    assert desktop.mode_note(STANDALONE) == "" and desktop.mode_note(STEAM) == ""
    no_steam = ModeEnv(steamos=False, steam=False)
    assert no_steam.default_mode == STANDALONE and no_steam.mode_note(STANDALONE) == ""
    assert no_steam.mode_note(STEAM) == tr("mode.note_steam_missing")


def test_features_belong_to_one_mode() -> None:
    assert feature_note("mangohud", STEAM) == tr("mode.note_mangohud_steam")
    assert feature_note("mangohud", STANDALONE) == ""
    assert feature_note("app_shortcuts", STEAM) == tr("mode.note_shortcuts_steam")
    assert feature_note("gamemode", STEAM) == "" and feature_note("gamemode", STANDALONE) == ""


def test_steamos_needs_no_standalone_runtime() -> None:
    steamos = ModeEnv(steamos=True, steam=True)
    for component in ("ge_proton", "umu", "mangohud"):
        assert steamos.component_note(component)
    assert ModeEnv(steamos=False, steam=True).component_note("mangohud") == ""


def test_library_row_kind_maps_to_mode() -> None:
    assert mode_of_kind("steam") == STEAM
    assert mode_of_kind("standalone") == STANDALONE
    assert mode_of_kind("external") == STANDALONE


def test_immutable_os_detection(tmp_path: Path) -> None:
    steamos_rel = tmp_path / "steamos"
    steamos_rel.write_text("ID=steamos\n", encoding="utf-8")
    bazzite_rel = tmp_path / "bazzite"
    bazzite_rel.write_text("ID=bazzite\n", encoding="utf-8")
    ubuntu_rel = tmp_path / "ubuntu"
    ubuntu_rel.write_text("ID=ubuntu\n", encoding="utf-8")
    ostree = tmp_path / "ostree-booted"
    ostree.write_text("", encoding="utf-8")

    assert modes.is_immutable_os(steamos_rel, tmp_path / "missing") is True
    assert modes.is_immutable_os(bazzite_rel, tmp_path / "missing") is True
    assert modes.is_immutable_os(ubuntu_rel, ostree) is True
    assert modes.is_immutable_os(ubuntu_rel, tmp_path / "missing") is False


def test_cachyos_proton_and_system_runtime_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import proton_policy, runtime_manager

    sys_compat = tmp_path / "usr_share_compat"
    cachy_dir = sys_compat / "proton-cachyos"
    cachy_dir.mkdir(parents=True)
    (cachy_dir / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    user_compat = tmp_path / "user_compat"
    user_compat.mkdir(parents=True)

    monkeypatch.setattr(runtime_manager, "get_compat_tools_dir", lambda: user_compat)
    monkeypatch.setattr(runtime_manager, "_all_compat_tool_dirs", lambda steam_root=None: [user_compat, sys_compat])
    monkeypatch.setattr(runtime_manager, "list_ge_proton11_dirs", lambda: [])
    monkeypatch.setattr(runtime_manager, "list_ge_proton10_dirs", lambda: [])

    assert runtime_manager.list_cachyos_proton_dirs() == [cachy_dir]
    assert proton_policy.newest_ge_proton() == cachy_dir
    assert runtime_manager.ensure_ge_proton11() == cachy_dir
    assert runtime_manager.is_managed_compat_tool(cachy_dir) is False

    monkeypatch.setattr(runtime_manager, "_SYSTEM_COMPAT_TOOL_DIRS", (sys_compat,))
    assert runtime_manager.is_system_compat_tool(cachy_dir) is True
    with pytest.raises(ValueError, match="system-managed"):
        runtime_manager.remove_compat_tool(cachy_dir)

    from types import SimpleNamespace

    pfx = tmp_path / "standalone_pfx"
    pfx.mkdir()
    resolved_pfx, resolved_proton = runtime_manager.resolve_game_prefix(
        "standalone", SimpleNamespace(prefix_dir=pfx, proton_name=""), None
    )
    assert resolved_pfx == pfx and resolved_proton == cachy_dir

    # System umu-run is preferred when present outside managed runtime dir, and launch.sh uses it
    sys_umu = tmp_path / "usr_bin" / "umu-run"
    sys_umu.parent.mkdir(parents=True)
    sys_umu.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(runtime_manager.shutil, "which", lambda name: str(sys_umu) if name == "umu-run" else None)
    assert runtime_manager.ensure_umu_run() == sys_umu

    from deckinstall import standalone

    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    exe_path = game_dir / "Game.exe"
    exe_path.write_bytes(b"MZ")
    launch_sh = tmp_path / "launch.sh"
    standalone._write_launch_script(
        script_path=launch_sh,
        exe_path=exe_path,
        prefix_dir=tmp_path / "pfx",
        proton_name=str(cachy_dir),
        winedlloverrides="",
        launch_options="",
    )
    assert str(sys_umu) in launch_sh.read_text(encoding="utf-8")

    # Immutable OS blocks get_system_package_install_cmd; mutable OS supports innoextract
    monkeypatch.setattr(modes, "is_immutable_os", lambda: True)
    assert runtime_manager.get_system_package_install_cmd("innoextract") is None
    monkeypatch.setattr(modes, "is_immutable_os", lambda: False)
    monkeypatch.setattr(
        runtime_manager.shutil,
        "which",
        lambda name: "/usr/bin/pacman" if name == "pacman" else None,
    )
    assert runtime_manager.get_system_package_install_cmd("innoextract") == [
        "pacman",
        "-S",
        "--noconfirm",
        "innoextract",
    ]


def test_stale_default_launcher_is_retargeted(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from deckinstall import proton_policy

    newest = tmp_path / "proton-cachyos-new"
    newest.mkdir()
    script = tmp_path / "launch.sh"
    script.write_text(f"export PROTONPATH={tmp_path / 'proton-cachyos-old'}\n", encoding="utf-8")
    game = SimpleNamespace(native_linux=False, proton_name="", launch_script=script, name="G")
    monkeypatch.setattr(proton_policy, "newest_ge_proton", lambda: newest)
    calls: list[object] = []
    monkeypatch.setattr(proton_policy, "refresh_integration_files", calls.append)
    assert proton_policy.retarget_stale_default_games([game]) == 1  # type: ignore[list-item]
    script.write_text(f"export PROTONPATH={newest}\n", encoding="utf-8")
    assert proton_policy.retarget_stale_default_games([game]) == 0  # type: ignore[list-item]
