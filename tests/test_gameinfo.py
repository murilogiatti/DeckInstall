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


def _pe_with_imports(dlls: list[str], delay: list[str] | None = None) -> bytes:
    """Minimal PE32+ with an import table (and optional delay-import table) naming dlls."""
    import struct

    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", buf, 0x86, 1)  # one section
    struct.pack_into("<H", buf, 0x94, 240)  # optional header size
    opt = 0x98
    struct.pack_into("<H", buf, opt, 0x20B)
    struct.pack_into("<I", buf, opt + 108, 16)
    sec = opt + 240
    struct.pack_into("<IIII", buf, sec + 8, 0x1000, 0x1000, 0x1000, 0x400)  # .rdata at file 0x400
    data = bytearray(0x1000)
    names_at = 0x800
    tables = [(1, 20, 12, dlls), (13, 32, 4, delay or [])]
    desc_at = 0
    for index, size, name_off, items in tables:
        struct.pack_into("<II", buf, opt + 112 + 8 * index, 0x1000 + desc_at, size * (len(items) + 1))
        for dll in items:
            struct.pack_into("<I", data, desc_at + name_off, 0x1000 + names_at)
            data[names_at:names_at + len(dll)] = dll.encode()
            names_at += len(dll) + 1
            desc_at += size
        desc_at += size  # zero terminator
    return bytes(buf) + bytes(data)


def test_pe_import_table_any_case(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    exe.write_bytes(_pe_with_imports(["KERNEL32.dll", "OpenGL32.dll"], delay=["D3d11.dll"]))
    assert detect_game_tech(exe, None) == ("DirectX 11", "Other")


def test_pe_import_opengl_only(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    exe.write_bytes(_pe_with_imports(["OpenGL32.dll"]))
    assert detect_game_tech(exe, None)[0] == "OpenGL"


def test_directdraw_game(tmp_path: Path) -> None:
    (tmp_path / "old.exe").write_bytes(b"MZ DDRAW.dll WINMM.dll")
    assert detect_game_tech(tmp_path / "old.exe", None)[0] == "DirectDraw"


def test_gdk_build_engine_version_from_full_version_string(tmp_path: Path) -> None:
    """Game Pass build with a licensee branch name: the version comes from ENGINE_VERSION_STRING."""
    bins = tmp_path / "Game" / "Binaries" / "WinGDK"
    bins.mkdir(parents=True)
    (tmp_path / "Engine").mkdir()
    exe = bins / "Game-WinGDK-Shipping.exe"
    exe.write_bytes(b"MZ " + "4.27.2-0+++depot+UE4-Releases+4.27".encode("utf-16-le") + b" d3d12.dll")
    assert detect_game_tech(exe, tmp_path) == ("DirectX 12", "UE4 4.27")


def test_unreal_fixed_file_version(tmp_path: Path) -> None:
    import struct

    bins = tmp_path / "Game" / "Binaries" / "Win64"
    bins.mkdir(parents=True)
    exe = bins / "Game-Win64-Test.exe"
    exe.write_bytes(b"MZ d3d11.dll " + b"\xbd\x04\xef\xfe" + struct.pack("<II", 0x10000, (5 << 16) | 3))
    assert detect_game_tech(exe, tmp_path) == ("DirectX 11", "UE5 5.3")


def test_modular_unreal_test_config_and_opengl_only(tmp_path: Path) -> None:
    bins = tmp_path / "G" / "Binaries" / "Linux"
    bins.mkdir(parents=True)
    exe = bins / "G-Linux-Shipping"
    exe.write_bytes(b"\x7fELF")
    (bins / "libG-OpenGLDrv-Linux-Shipping.so").write_bytes(b"\x7fELF")
    assert detect_game_tech(exe, tmp_path) == ("OpenGL", "Unreal Engine")


def test_ue3_from_cooked_packages(tmp_path: Path) -> None:
    (tmp_path / "Binaries" / "Win64").mkdir(parents=True)
    (tmp_path / "GameName" / "CookedPCConsole").mkdir(parents=True)
    exe = tmp_path / "Binaries" / "Win64" / "Game.exe"
    exe.write_bytes(b"MZ d3d9.dll")
    assert detect_game_tech(exe, tmp_path) == ("DirectX 9", "UE3")


def test_wwise_pck_is_not_godot(tmp_path: Path) -> None:
    (tmp_path / "Audio").mkdir()
    (tmp_path / "Audio" / "Init.pck").write_bytes(b"AKPK" + b"\x00" * 16)
    (tmp_path / "game.exe").write_bytes(b"MZ d3d11.dll")
    assert detect_game_tech(tmp_path / "game.exe", tmp_path) == ("DirectX 11", "Other")


def test_godot_pck_with_magic(tmp_path: Path) -> None:
    (tmp_path / "game.pck").write_bytes(b"GDPC" + b"\x00" * 16)
    (tmp_path / "game.exe").write_bytes(b"MZ vulkan-1.dll")
    assert detect_game_tech(tmp_path / "game.exe", tmp_path) == ("Vulkan", "Godot")


def test_backend_modules_cryengine_source2_ogre(tmp_path: Path) -> None:
    cry = tmp_path / "cry"
    (cry / "Bin64").mkdir(parents=True)
    for n in ("CrySystem.dll", "CryRenderD3D11.dll", "CryRenderVulkan.dll"):
        (cry / "Bin64" / n).write_bytes(b"MZ")
    (cry / "Bin64" / "Game.exe").write_bytes(b"MZ")
    assert detect_game_tech(cry / "Bin64" / "Game.exe", cry) == ("DirectX 11 + Vulkan", "CryEngine")

    s2 = tmp_path / "s2" / "game" / "bin" / "win64"
    s2.mkdir(parents=True)
    for n in ("engine2.dll", "tier0.dll", "vstdlib.dll", "rendersystemdx11.dll", "rendersystemvulkan.dll"):
        (s2 / n).write_bytes(b"MZ")
    (s2 / "cs2.exe").write_bytes(b"MZ")
    assert detect_game_tech(s2 / "cs2.exe", tmp_path / "s2") == ("DirectX 11 + Vulkan", "Source 2")

    ogre = tmp_path / "ogre"
    ogre.mkdir()
    for n in ("OgreMain.dll", "RenderSystem_Direct3D9.dll", "RenderSystem_GL.dll"):
        (ogre / n).write_bytes(b"MZ")
    (ogre / "g.exe").write_bytes(b"MZ")
    assert detect_game_tech(ogre / "g.exe", ogre) == ("DirectX 9", "OGRE")


def test_more_engines(tmp_path: Path) -> None:
    cases = {
        "re": (["re_chunk_000.pak"], "RE Engine"),
        "fb": (["Data/layout.toc", "Data/cas.cat"], "Frostbite"),
        "ce": (["Data/Fallout4.esm", "Data/Fallout4 - Textures1.ba2"], "Creation Engine"),
        "gb": (["Data/Oblivion.esm", "Data/Oblivion - Meshes.bsa"], "Gamebryo"),
        "vx": (["RGSS301.dll", "Game.rgss3a"], "RPG Maker VX Ace"),
        "mv": (["www/js/rpg_core.js"], "RPG Maker MV"),
        "nw": (["nw.dll", "package.nw"], "NW.js"),
        "el": (["resources/app.asar"], "Electron"),
        "kk": (["data.xp3"], "KiriKiri"),
        "gm": (["assets/game.unx"], "GameMaker"),
        "xna": (["Microsoft.Xna.Framework.dll"], "XNA"),
        "hl": (["libhl.dll", "hlboot.dat"], "HashLink"),
    }
    for folder, (files, engine) in cases.items():
        root = tmp_path / folder
        for rel in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"x")
        (root / "game.exe").write_bytes(b"MZ")
        assert detect_game_tech(root / "game.exe", root)[1] == engine, folder


def test_renpy_version(tmp_path: Path) -> None:
    (tmp_path / "renpy").mkdir()
    (tmp_path / "renpy" / "vc_version.py").write_text("branch = 'fix'\nversion = '8.1.3.23091805'\n")
    (tmp_path / "game.exe").write_bytes(b"MZ")
    assert detect_game_tech(tmp_path / "game.exe", tmp_path) == ("OpenGL", "Ren'Py 8.1.3")


def test_old_unity_maindata(tmp_path: Path) -> None:
    (tmp_path / "G_Data").mkdir()
    (tmp_path / "G_Data" / "mainData").write_bytes(b"\x00" * 16 + b"4.7.2f1\x00")
    (tmp_path / "G.exe").write_bytes(b"MZ")
    assert detect_game_tech(tmp_path / "G.exe", tmp_path) == ("DirectX 11", "Unity 4.7.2f1")


def test_breadth_first_walk_reaches_binaries(tmp_path: Path, monkeypatch) -> None:
    """A huge content folder must not hide Engine/ and Binaries/ from the walk."""
    import deckinstall.gameinfo as gi

    monkeypatch.setattr(gi, "_MAX_ENTRIES", 50)
    content = tmp_path / "AAContent" / "deep"
    content.mkdir(parents=True)
    for i in range(200):
        (content / f"f{i}.bin").write_bytes(b"")
    (tmp_path / "Engine").mkdir()
    bins = tmp_path / "Game" / "Binaries" / "Win64"
    bins.mkdir(parents=True)
    (bins / "Game-Win64-Shipping.exe").write_bytes(b"MZ ++UE5+Release-5.1 d3d12.dll")
    (tmp_path / "Game.exe").write_bytes(b"MZ")
    assert detect_game_tech(tmp_path / "Game.exe", tmp_path) == ("DirectX 12", "UE5 5.1")


def test_container_folder_is_not_walked(tmp_path: Path) -> None:
    from deckinstall.gameinfo import safe_scan_root

    games = tmp_path / "Games"
    exe = games / "A" / "a.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert safe_scan_root(exe, games) == exe.parent
    assert safe_scan_root(exe, games / "A") == games / "A"


def test_optiscaler_agility_sdk_is_not_the_games_api(tmp_path: Path) -> None:
    (tmp_path / "D3D12_Optiscaler").mkdir()
    (tmp_path / "D3D12_Optiscaler" / "D3D12Core.dll").write_bytes(b"MZ")
    (tmp_path / "dxgi.dll").write_bytes(b"MZ")
    (tmp_path / "game.exe").write_bytes(b"MZ d3d11.dll")
    assert detect_game_tech(tmp_path / "game.exe", tmp_path)[0] == "DirectX 11"
