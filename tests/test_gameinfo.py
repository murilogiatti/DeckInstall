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


def test_modular_unreal_build_takes_api_from_rhi_modules(tmp_path: Path) -> None:
    """Returnal-like build: the .exe imports no graphics DLL, each backend is its own module."""
    bins = tmp_path / "Returnal" / "Binaries" / "Win64"
    bins.mkdir(parents=True)
    (tmp_path / "Engine").mkdir()
    exe = bins / "Returnal-Win64-Shipping.exe"
    exe.write_bytes(b"MZ UE4Game")
    (bins / "Returnal-Core-Win64-Shipping.dll").write_bytes(b"MZ " + "UE4CC-Windows".encode("utf-16-le"))
    for rhi in ("D3D11RHI", "D3D12RHI", "VulkanRHI", "OpenGLDrv"):
        (bins / f"Returnal-{rhi}-Win64-Shipping.dll").write_bytes(b"MZ")
    assert detect_game_tech(exe, tmp_path) == ("DirectX 11/12", "UE4")


def test_vulkan_only_unreal_build(tmp_path: Path) -> None:
    bins = tmp_path / "Game" / "Binaries" / "Win64"
    bins.mkdir(parents=True)
    exe = bins / "Game-Win64-Shipping.exe"
    exe.write_bytes(b"MZ")
    (bins / "Game-VulkanRHI-Win64-Shipping.dll").write_bytes(b"MZ")
    assert detect_game_tech(exe, tmp_path)[0] == "Vulkan"
