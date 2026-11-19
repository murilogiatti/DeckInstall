"""Engine and graphics API detection for the library table."""

from __future__ import annotations

from pathlib import Path

from deckinstall.gameinfo import detect_game_tech


def test_unity_version_and_default_api(tmp_path: Path) -> None:
    game = tmp_path / "G"
    (game / "G_Data").mkdir(parents=True)
    (game / "G.exe").write_bytes(b"MZ")
    (game / "UnityPlayer.dll").write_bytes(b"MZ")
    (game / "G_Data" / "globalgamemanagers").write_bytes(b"\x00" * 20 + b"2021.3.9f1\x00")
    assert detect_game_tech(game / "G.exe", game) == ("DirectX 11", "Unity 2021.3.9f1")


def test_unreal_version_and_directx_from_binary(tmp_path: Path) -> None:
    game = tmp_path / "U"
    binary = game / "Game" / "Binaries" / "Win64"
    binary.mkdir(parents=True)
    (game / "Engine").mkdir()
    (game / "U.exe").write_bytes(b"MZ")
    (binary / "Game-Win64-Shipping.exe").write_bytes(b"MZ ++UE4+Release-4.27-CL d3d11.dll D3D12.dll vulkan-1.dll")
    assert detect_game_tech(game / "U.exe", game) == ("DirectX 11/12 + Vulkan", "UE4 4.27")


def test_unknown_game(tmp_path: Path) -> None:
    (tmp_path / "x.exe").write_bytes(b"MZ")
    assert detect_game_tech(tmp_path / "x.exe", None) == ("-", "Other")
