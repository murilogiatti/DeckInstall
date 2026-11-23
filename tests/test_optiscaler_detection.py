"""OptiScaler is only offered to games that ship an upscaler it can replace."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from deckinstall import optiscaler as opti

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00" + b"\x64\x86" + b"\x00" * 40


def _game(root: Path, *files: str) -> Path:
    game = root / "Game"
    game.mkdir(parents=True)
    (game / "Game.exe").write_bytes(PE)
    for rel in files:
        path = game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return game


def _check(game: Path, api: str = "DirectX 12") -> opti.OptiScalerCompatibility:
    with mock.patch("deckinstall.gameinfo.detect_game_tech", return_value=(api, "Unity")):
        return opti.check_optiscaler_compatibility(game / "Game.exe", game)


def test_game_without_upscaler_is_not_compatible(tmp_path: Path) -> None:
    compat = _check(_game(tmp_path, "UnityPlayer.dll"))
    assert not compat.compatible and compat.tier == "no_upscaler" and compat.upscalers == ()


def test_optiscalers_own_files_do_not_count_as_the_games_upscaler(tmp_path: Path) -> None:
    game = _game(tmp_path, "libxess.dll", "amd_fidelityfx_dx12.dll", "dxgi.dll")
    state = opti.OptiScalerState(
        proxy_dll="dxgi", backend="auto", version="v0.9", exe_dir=game,
        installed_files=[game / "libxess.dll", game / "amd_fidelityfx_dx12.dll", game / "dxgi.dll"],
        backed_up_files=[], added_override="dxgi=n,b",
    )
    with mock.patch.object(opti, "inspect_optiscaler_state", return_value=state):
        compat = _check(game)
    assert compat.tier == "no_upscaler" and compat.installed_state is state


def test_unreal_plugins_deep_in_the_tree_are_found(tmp_path: Path) -> None:
    game = _game(
        tmp_path,
        "Kiborg/Plugins/DLSS/Binaries/ThirdParty/Win64/nvngx_dlss.dll",
        "Kiborg/Plugins/FSR/Source/readme.txt",
        "Kiborg/Plugins/Runtime/Intel/XeSS/XeSS.uplugin",
    )
    compat = _check(game, "DirectX 11/12 + Vulkan")
    assert compat.compatible and compat.upscalers == ("DLSS", "FSR", "XeSS")


def test_plugin_named_folder_outside_plugins_does_not_count(tmp_path: Path) -> None:
    assert _check(_game(tmp_path, "Content/FSR/notes.txt")).tier == "no_upscaler"


def test_old_graphics_api_rules_it_out_but_unknown_does_not(tmp_path: Path) -> None:
    game = _game(tmp_path, "nvngx_dlss.dll")
    assert _check(game, "OpenGL").tier == "unsupported_api"
    assert _check(game, "DirectX 9").tier == "unsupported_api"
    assert _check(game, "DirectX 9/11/12 + Vulkan").compatible
    assert _check(game, "-").compatible
