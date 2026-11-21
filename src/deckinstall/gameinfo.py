"""Static detection of a game's graphics API and engine (with versions) for the library table.

Nothing is executed: the game folder is walked a few levels deep for tell-tale files, and the
main binary is searched for DLL names it references (strings and the PE import tables). The
graphics result is therefore what the game supports, not what it will pick at runtime.
"""

from __future__ import annotations

import mmap
import os
import re
import struct
from collections import deque
from pathlib import Path

_MAX_DEPTH = 4
_MAX_ENTRIES = 6000
_MAX_SCAN_BYTES = 768 * 1024 * 1024
_UNKNOWN = "-"

# Graphics DLLs as referenced by a Windows binary -> API key.
_GFX_DLLS = {
    "d3d8.dll": "dx8",
    "d3d9.dll": "dx9",
    "d3d10.dll": "dx10",
    "d3d10_1.dll": "dx10",
    "d3d11.dll": "dx11",
    "d3d12.dll": "dx12",
    "d3d12core.dll": "dx12",
    "vulkan-1.dll": "vulkan",
    "opengl32.dll": "opengl",
    "ddraw.dll": "ddraw",
    "glide.dll": "glide",
    "glide2x.dll": "glide",
    "glide3x.dll": "glide",
}
# Linux libraries (ELF needs are plain strings) -> API key.
_GFX_SO = {b"libvulkan.so": "vulkan", b"libGL.so": "opengl", b"libGLX.so": "opengl", b"libOpenGL.so": "opengl"}

_DLL_STEM_RE = re.compile(rb"[A-Za-z0-9_\-]+$")
_MAX_DLL_STRINGS = 200_000
# Per-engine graphics backend modules: the main binary of these games imports no graphics DLL.
_BACKEND_MODULES: tuple[tuple[re.Pattern[str], dict[str, str]], ...] = (
    # CryEngine / Lumberyard: CryRenderD3D11.dll, CryRenderVulkan.dll
    (re.compile(r"^cryrender(d3d9|d3d10|d3d11|d3d12|vulkan|gl)\.dll$"),
     {"d3d9": "dx9", "d3d10": "dx10", "d3d11": "dx11", "d3d12": "dx12", "vulkan": "vulkan", "gl": "opengl"}),
    # Source 1: shaderapidx9.dll; Source 2: rendersystemdx11.dll / rendersystemvulkan.dll
    (re.compile(r"^shaderapi(dx9|dx10|dx11|vulkan)\.(?:dll|so)$"),
     {"dx9": "dx9", "dx10": "dx10", "dx11": "dx11", "vulkan": "vulkan"}),
    (re.compile(r"^(?:lib)?rendersystem(dx11|dx12|vulkan|gl)\.(?:dll|so)$"),
     {"dx11": "dx11", "dx12": "dx12", "vulkan": "vulkan", "gl": "opengl"}),
    # OGRE: RenderSystem_Direct3D11.dll, RenderSystem_GL3Plus.dll, RenderSystem_Vulkan.dll
    (re.compile(r"^rendersystem_(direct3d9|direct3d11|gl|gl3plus|glsupport|vulkan)(?:_d)?\.(?:dll|so)$"),
     {"direct3d9": "dx9", "direct3d11": "dx11", "gl": "opengl", "gl3plus": "opengl", "glsupport": "opengl",
      "vulkan": "vulkan"}),
)

_UNITY_VERSION_RE = re.compile(rb"\d{4}\.\d+\.\d+[fpab]\d+|\d\.\d+\.\d+[fpab]\d+")
_UE_VERSION_RE = re.compile(rb"\+\+UE([45])\+Release-(\d+\.\d+)")
_UE_VERSION_W_RE = re.compile(
    rb"\+\x00\+\x00U\x00E\x00([45])\x00\+\x00R\x00e\x00l\x00e\x00a\x00s\x00e\x00-\x00"
    rb"(\d)\x00\.\x00(\d)\x00(?:(\d)\x00)?"
)
# ENGINE_VERSION_STRING is "<major>.<minor>.<patch>-<CL>+<branch>"; the branch is "++UE4+Release-4.27"
# for Epic's builds but any name for licensees ("++depot+UE4-Releases+4.27"), so the version in front
# of the "+++" is what tells it in every case.
_UE_FULL_VERSION_RE = re.compile(r"([45])\.(\d{1,2})\.\d{1,3}-\d+$")
# Stripped builds only keep the file version resource, "UE5-CL-0": the major version.
_UE_MAJOR_RE = re.compile(rb"U\x00E\x00([45])\x00-\x00C\x00L\x00")
# Crash client tag in Core of modular UE4 builds without a version string ("UE4CC-Windows-...").
_UE_CC_RE = re.compile(rb"U\x00E\x00([45])\x00C\x00C\x00-\x00")
# Unreal binaries: <Game>-Win64-Shipping.exe, Game Pass <Game>-WinGDK-Shipping.exe, Test builds,
# 32-bit builds and the Linux ones (no extension). Development builds are just <Game>.exe.
_UE_EXE_RE = re.compile(r"-(?:win64|win32|wingdk|winarm64)-(?:shipping|test|debuggame)\.exe$")
_UE_ELF_RE = re.compile(r"-(?:linux|linuxarm64)-(?:shipping|test|debuggame)$")
_UE_MODULE_SUFFIX = r"(?:-(?:win64|win32|wingdk|winarm64|linux|linuxarm64)-(?:shipping|test|debuggame))?"
# Modular Unreal builds keep each graphics backend in its own module: <Game>-D3D12RHI-Win64-Shipping.dll.
_UE_RHI_RE = re.compile(rf"(?:^|-)(d3d11|d3d12|vulkan)rhi{_UE_MODULE_SUFFIX}\.(?:dll|so)$")
_UE_OPENGL_RE = re.compile(rf"(?:^|-)opengldrv{_UE_MODULE_SUFFIX}\.(?:dll|so)$")
_UE_CORE_RE = re.compile(r"-core-(?:win64|win32|wingdk|winarm64)-(?:shipping|test|debuggame)\.dll$")
_GODOT_VERSION_RE = re.compile(rb"Godot Engine v(\d+\.\d+(?:\.\d+)?)")
_RENPY_VERSION_RE = re.compile(rb"""version\s*=\s*u?['"](\d+\.\d+(?:\.\d+)?)""")
_RENPY_TUPLE_RE = re.compile(rb"version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")

# Folders that hold many games: walking them as "the game" would mix the tells of all of them.
_CONTAINER_NAMES = frozenset(
    {"games", "jogos", "common", "steamapps", "heroic", "lutris", "downloads", "desktop", "documents",
     "documentos", "área de trabalho", "transferências", "syncthing", "deckinstall", "media", "mnt", "run"}
)

# Folders added by injected tools, not by the game: OptiScaler brings its own Agility SDK
# (D3D12_Optiscaler/D3D12Core.dll), which says nothing about the game's API.
_TOOL_DIRS = frozenset({"d3d12_optiscaler"})
# Folders where engines keep their binaries and backend modules.
_CODE_DIRS = frozenset(
    {"engine", "binaries", "bin", "bin64", "bin32", "win64", "win32", "wingdk", "winarm64", "linux", "x64",
     "x86", "plugins", "d3d12", "system", "game"}
)

_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def is_unreal_binary_name(name: str) -> bool:
    """True for an Unreal game binary name: <Game>-Win64-Shipping.exe, -WinGDK-, -Test, Linux..."""
    low = name.lower()
    return bool(_UE_EXE_RE.search(low) or _UE_ELF_RE.search(low))


def _pe_imports(mm: mmap.mmap) -> set[str]:
    """Lowercase DLL names from the import and delay-import tables of a PE (empty when not one)."""
    names: set[str] = set()
    try:
        if mm[:2] != b"MZ":
            return names
        pe = struct.unpack_from("<I", mm, 0x3C)[0]
        if mm[pe:pe + 4] != b"PE\x00\x00":
            return names
        n_sections, = struct.unpack_from("<H", mm, pe + 6)
        opt_size, = struct.unpack_from("<H", mm, pe + 20)
        opt = pe + 24
        magic, = struct.unpack_from("<H", mm, opt)
        dirs = opt + (96 if magic == 0x10B else 112)
        n_dirs, = struct.unpack_from("<I", mm, dirs - 4)
        sections = []
        for i in range(n_sections):
            off = opt + opt_size + 40 * i
            vsize, vaddr, rsize, raw = struct.unpack_from("<IIII", mm, off + 8)
            sections.append((vaddr, max(vsize, rsize), raw))

        def to_off(rva: int) -> int | None:
            for vaddr, size, raw in sections:
                if vaddr <= rva < vaddr + size:
                    return rva - vaddr + raw
            return None

        def read_name(rva: int) -> None:
            off = to_off(rva)
            if off is None:
                return
            end = mm.find(b"\x00", off, off + 256)
            if end > off:
                names.add(mm[off:end].decode("ascii", "ignore").lower())

        # Import directory (index 1): 20-byte descriptors, name RVA at +12.
        # Delay-import directory (index 13): 32-byte descriptors, name RVA at +4.
        for index, size, name_at in ((1, 20, 12), (13, 32, 4)):
            if index >= n_dirs:
                continue
            rva, _ = struct.unpack_from("<II", mm, dirs + 8 * index)
            start = to_off(rva) if rva else None
            if start is None:
                continue
            off = start
            for _ in range(512):
                desc = mm[off:off + size]
                if len(desc) < size or desc == b"\x00" * size:
                    break
                name_rva, = struct.unpack_from("<I", desc, name_at)
                if name_rva:
                    read_name(name_rva)
                off += size
    except (struct.error, ValueError, IndexError):
        pass
    return names


def _scan_binary(path: Path) -> set[str]:
    """Graphics APIs referenced by a PE/ELF binary: {'dx9','dx11','dx12','vulkan','opengl'...}."""
    found: set[str] = set()
    try:
        size = path.stat().st_size
        if size == 0 or size > _MAX_SCAN_BYTES:
            return found
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            # Import tables: exact, any letter case.
            found |= {api for dll in _pe_imports(mm) if (api := _GFX_DLLS.get(dll))}
            # Strings: what the binary loads at runtime by name (LoadLibrary), in any letter case.
            # Two passes over the file (".dll", ".DLL") instead of one per DLL name.
            for ext in (b".dll", b".DLL"):
                pos = mm.find(ext)
                for _ in range(_MAX_DLL_STRINGS):
                    if pos == -1:
                        break
                    m = _DLL_STEM_RE.search(mm[max(0, pos - 24):pos])
                    if m and (api := _GFX_DLLS.get(m.group(0).decode().lower() + ".dll")):
                        found.add(api)
                    pos = mm.find(ext, pos + 4)
            if mm[:4] == b"\x7fELF":
                for lib, api in _GFX_SO.items():
                    if api not in found and mm.find(lib) != -1:
                        found.add(api)
    except (OSError, ValueError):
        pass
    return found


def _find_regex(path: Path, pattern: re.Pattern[bytes], limit: int = _MAX_SCAN_BYTES) -> tuple[bytes, ...] | None:
    """Groups of the first match of pattern in the file (group 0 when it has none)."""
    try:
        if path.stat().st_size > limit:
            return None
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            m = pattern.search(mm)
            return (m.groups() or (m.group(0),)) if m else None
    except (OSError, ValueError):
        return None


def _read_head(path: Path | None, size: int = 1 << 16) -> bytes:
    if path is None:
        return b""
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def _ue_full_version(path: Path) -> tuple[str, str] | None:
    """(major, minor) from ENGINE_VERSION_STRING ("4.27.2-18319896+++<branch>"), ASCII or UTF-16."""
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for marker, enc in ((b"+++", "ascii"), ("+++".encode("utf-16-le"), "utf-16-le")):
                width = 1 if enc == "ascii" else 2
                pos = mm.find(marker)
                for _ in range(16):
                    if pos == -1:
                        break
                    window = mm[max(0, pos - 32 * width):pos]
                    if width == 2 and len(window) % 2:
                        window = window[1:]
                    m = _UE_FULL_VERSION_RE.search(window.decode(enc, "ignore"))
                    if m:
                        return m.group(1), m.group(2)
                    pos = mm.find(marker, pos + len(marker))
    except (OSError, ValueError):
        pass
    return None


def _ue_fixed_file_version(path: Path) -> tuple[int, int] | None:
    """(major, minor) of the VS_FIXEDFILEINFO of an Unreal binary: Epic stamps the engine version."""
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            pos = mm.rfind(b"\xbd\x04\xef\xfe")  # the resource section sits at the end
            if pos == -1:
                return None
            struc, ms = struct.unpack_from("<II", mm, pos + 4)
    except (OSError, ValueError, struct.error):
        return None
    if struc != 0x00010000:
        return None
    major, minor = ms >> 16, ms & 0xFFFF
    if (major == 4 and minor <= 27) or (major == 5 and minor <= 20):
        return major, minor
    return None


class _Tree:
    """File names collected from a bounded, breadth-first walk of the game folder."""

    def __init__(self, root: Path, must_list: Path | None = None) -> None:
        self.files: dict[str, Path] = {}  # lowercase name -> first path seen (shallowest)
        self.dirs: dict[str, Path] = {}
        count = 0
        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        # Breadth-first so a huge content folder cannot use up the budget before Binaries/ is seen.
        listed: set[Path] = set()
        while queue:
            cur, depth = queue.popleft()
            listed.add(cur)
            if depth < _MAX_DEPTH:
                count += self._list(cur, queue, depth, budget_left=count <= _MAX_ENTRIES)
            else:
                count += self._list(cur, None, depth, budget_left=False)
        # The exe's own folder is always looked at, whatever the budget did.
        if must_list is not None and must_list not in listed:
            self._list(must_list, None, 0, budget_left=False)

    def _list(self, cur: Path, queue: deque[tuple[Path, int]] | None, depth: int, *, budget_left: bool) -> int:
        n = 0
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return 0
        for entry in entries:
            n += 1
            low = entry.name.lower()
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if low in _TOOL_DIRS:
                        continue
                    path = Path(entry.path)
                    self.dirs.setdefault(low, path)
                    # Code folders are always entered: the budget is for content, not for binaries.
                    if queue is not None and (budget_left or low in _CODE_DIRS):
                        queue.append((path, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    self.files.setdefault(low, Path(entry.path))
            except OSError:
                continue
        return n

    def first_suffix(self, suffix: str) -> Path | None:
        for name, p in self.files.items():
            if name.endswith(suffix):
                return p
        return None

    def any_suffix(self, *suffixes: str) -> bool:
        return any(name.endswith(suffixes) for name in self.files)

    def matching(self, pattern: re.Pattern[str]) -> list[tuple[re.Match[str], Path]]:
        return [(m, p) for n, p in self.files.items() if (m := pattern.search(n))]


def _unity(tree: _Tree) -> str | None:
    """Unity version like '2021.3.5f1', '' when it is Unity of unknown version, None when not Unity."""
    marker = tree.files.get("unityplayer.dll") or tree.files.get("unityplayer.so")
    # globalgamemanagers (5.x+), data.unity3d (compressed build), mainData (4.x and older).
    managers = [tree.files.get(n) for n in ("globalgamemanagers", "data.unity3d", "maindata")]
    if marker is None and not any(managers):
        return None
    # Every serialized file carries the editor version in its header.
    extra = [tree.files.get(n) for n in ("level0", "sharedassets0.assets", "resources.assets")]
    for source in [*managers, *extra]:
        m = _UNITY_VERSION_RE.search(_read_head(source))
        if m:
            return m.group(0).decode("ascii")
    return ""


def _unreal(tree: _Tree, exe: Path) -> tuple[str, Path | None] | None:
    """('UE4 4.27'/'UE5'/'UE3'..., the binary that carries the graphics imports); None when not Unreal."""
    shipping_bins = [p for _, p in tree.matching(_UE_EXE_RE)] or [p for _, p in tree.matching(_UE_ELF_RE)]
    if is_unreal_binary_name(exe.name):
        shipping: Path | None = exe
    elif shipping_bins:
        # The game is the biggest one (servers, -Cmd and tool binaries are smaller).
        shipping = max(shipping_bins, key=lambda p: p.stat().st_size if p.is_file() else -1)
    else:
        shipping = None
    has_engine_dir = "engine" in tree.dirs and ("content" in tree.dirs or shipping is not None
                                               or any(n.endswith(".pak") for n in tree.files))
    if shipping is None and not has_engine_dir:
        return _legacy_unreal(tree)
    probes = [p for p in (shipping, exe) if p is not None and p.is_file()]
    crash = tree.files.get("crashreportclient.exe")
    if crash is not None:
        probes.append(crash)
    core = next((p for _, p in tree.matching(_UE_CORE_RE)), None)
    if core is not None:  # modular build: the engine version lives in Core, not in the .exe
        probes.append(core)
    probes = list(dict.fromkeys(probes))
    for probe in probes:
        m = _find_regex(probe, _UE_VERSION_RE)
        if m:
            return f"UE{m[0].decode()} {m[1].decode()}", shipping
        w = _find_regex(probe, _UE_VERSION_W_RE)
        if w:
            minor = w[2].decode() + (w[3].decode() if w[3] else "")
            return f"UE{w[0].decode()} {w[1].decode()}.{minor}", shipping
        full = _ue_full_version(probe)
        if full:
            return f"UE{full[0]} {full[0]}.{full[1]}", shipping
    # The fixed file version is only trusted on engine-built binaries, not on a launcher exe.
    for probe in [p for p in probes if p is not exe or p is shipping]:
        fixed = _ue_fixed_file_version(probe)
        if fixed:
            return f"UE{fixed[0]} {fixed[0]}.{fixed[1]}", shipping
    for probe in probes:
        major = _find_regex(probe, _UE_MAJOR_RE) or _find_regex(probe, _UE_CC_RE)
        if major:
            return f"UE{major[0].decode()}", shipping
    return "Unreal Engine", shipping


def _legacy_unreal(tree: _Tree) -> tuple[str, Path | None] | None:
    """UE3 (cooked .upk packages) and UE1/UE2 (System/Core.u); None when neither."""
    if tree.any_suffix(".upk") or any(d.startswith("cookedpc") for d in tree.dirs):
        return "UE3", None
    if "core.u" in tree.files and "engine.u" in tree.files:
        return ("UE2" if tree.any_suffix(".ukx", ".usx", ".upx") else "UE1"), None
    return None


def _godot_pck(tree: _Tree) -> Path | None:
    """A real Godot pack (GDPC magic): Wwise sound banks are .pck files too (AKPK)."""
    for name, p in tree.files.items():
        if name.endswith(".pck") and _read_head(p, 4) == b"GDPC":
            return p
    return None


def _renpy_version(tree: _Tree) -> str:
    base = tree.dirs.get("renpy")
    if base is None:
        return ""
    m = _RENPY_VERSION_RE.search(_read_head(base / "vc_version.py", 4096))
    if m:
        return m.group(1).decode()
    t = _RENPY_TUPLE_RE.search(_read_head(base / "__init__.py", 1 << 17))
    return ".".join(g.decode() for g in t.groups()) if t else ""


def _rpg_maker(files: dict[str, Path]) -> str | None:
    names = files.keys()
    if "rmmz_core.js" in files:
        return "RPG Maker MZ"
    if "rpg_core.js" in files:
        return "RPG Maker MV"
    if any(n.startswith("rgss3") for n in names) or "game.rgss3a" in files:
        return "RPG Maker VX Ace"
    if any(n.startswith("rgss2") for n in names) or "game.rgss2a" in files:
        return "RPG Maker VX"
    if any(n.startswith("rgss1") for n in names) or "game.rgssad" in files:
        return "RPG Maker XP"
    if "rpg_rt.exe" in files or "rpg_rt.ldb" in files:
        return "RPG Maker 2000/2003"
    return None


def _engine(tree: _Tree, exe: Path) -> tuple[str, Path | None]:
    """(engine label with version when known, the binary that carries the graphics imports)."""
    unity = _unity(tree)
    if unity is not None:
        return (f"Unity {unity}" if unity else "Unity"), None
    unreal = _unreal(tree, exe)
    if unreal is not None:
        return unreal[0], unreal[1]
    f, d = tree.files, tree.dirs
    pck = _godot_pck(tree)
    for probe in [exe] + ([pck] if pck is not None else []):
        m = _find_regex(probe, _GODOT_VERSION_RE)
        if m:
            return f"Godot {m[0].decode()}", None
    if pck is not None:
        return "Godot", None
    if "data.win" in f or "game.droid" in f or "game.unx" in f or "game.ios" in f:
        return "GameMaker", None
    rpg = _rpg_maker(f)
    if rpg:
        return rpg, None
    if "renpy" in d:
        ver = _renpy_version(tree)
        return (f"Ren'Py {ver}" if ver else "Ren'Py"), None
    if "crysystem.dll" in f:
        return "CryEngine", None
    if "engine2.dll" in f or "libengine2.so" in f:
        return "Source 2", None
    if "tier0.dll" in f and "vstdlib.dll" in f:
        return "Source", None
    if "hw.dll" in f and ("sw.dll" in f or "valve" in d):
        return "GoldSrc", None
    if any(n.startswith("re_chunk_000.pak") for n in f):
        return "RE Engine", None
    if "layout.toc" in f and ("cas.cat" in f or "initfs_win32" in f or "initfs_win64" in f):
        return "Frostbite", None
    if tree.any_suffix(".ba2") or "skyrim.esm" in f or "starfield.esm" in f:
        return "Creation Engine", None
    if tree.any_suffix(".esm") and tree.any_suffix(".bsa"):
        return "Gamebryo", None
    if "meta.resources" in f:
        return "id Tech", None
    if "packed_dx12" in d:
        return "Decima", None
    if "ogremain.dll" in f or "libogremain.so" in f:
        return "OGRE", None
    if "flaxengine.dll" in f:
        return "Flax", None
    if "stride.engine.dll" in f or "xenko.engine.dll" in f:
        return "Stride", None
    if any(n.startswith("unigine") and n.endswith((".dll", ".so")) for n in f):
        return "UNIGINE", None
    if "monogame.framework.dll" in f:
        return "MonoGame", None
    if "fna.dll" in f:
        return "FNA", None
    if "microsoft.xna.framework.dll" in f:
        return "XNA", None
    if "libcocos2d.dll" in f or "cocos2d.dll" in f:
        return "Cocos2d-x", None
    if "love.dll" in f or "liblove.so" in f:
        return "LÖVE", None
    if "libhl.dll" in f or "hlboot.dat" in f:
        return "HashLink", None
    if "data.xp3" in f:
        return "KiriKiri", None
    if tree.any_suffix(".wolf"):
        return "WOLF RPG Editor", None
    if "lwjgl.dll" in f or "lwjgl64.dll" in f or "liblwjgl.so" in f or (tree.any_suffix(".jar") and "jre" in d):
        return "Java", None
    if "nw.dll" in f or "package.nw" in f:
        return "NW.js", None
    if "app.asar" in f or ("v8_context_snapshot.bin" in f and "libcef.dll" not in f):
        return "Electron", None
    return "Other", None


def _format_graphics(apis: set[str]) -> str:
    parts: list[str] = []
    dx = sorted({a[2:] for a in apis if a.startswith("dx")}, key=int)
    if dx:
        parts.append("DirectX " + "/".join(dx))
    if "vulkan" in apis:
        parts.append("Vulkan")
    if not parts:
        # Fallbacks only shown alone: almost every Direct3D game also mentions these.
        for key, label in (("opengl", "OpenGL"), ("ddraw", "DirectDraw"), ("glide", "Glide")):
            if key in apis:
                parts.append(label)
                break
    return " + ".join(parts) if parts else _UNKNOWN


def _is_container(path: Path) -> bool:
    """A folder that holds many games or the user's files, never one game."""
    try:
        resolved = path.resolve()
        home = Path.home().resolve()
    except OSError:
        return True
    # "/", "/home" and the home folder itself.
    if home.is_relative_to(resolved):
        return True
    # A library or user folder (~/Games, steamapps/common, <disk>/DeckInstall/Games): the name only
    # counts where such folders live, so a game called "Run" or "Games" is still walked.
    name = resolved.name.lower()
    if name in _CONTAINER_NAMES and (
        resolved.parent == home or resolved.parent.name.lower() in ("steamapps", "deckinstall", "heroic")
        or os.path.ismount(resolved.parent) or name in ("common", "steamapps")
    ):
        return True
    try:
        return os.path.ismount(resolved)
    except OSError:
        return True


def safe_scan_root(exe_path: Path, game_dir: Path | None) -> Path:
    """game_dir when it is a real game folder above the exe, else the exe's own folder."""
    root = exe_path.parent
    if game_dir is None or not game_dir.is_dir() or game_dir.is_symlink() or _is_container(game_dir):
        return root
    try:
        if root.resolve().is_relative_to(game_dir.resolve()):
            return game_dir
    except OSError:
        pass
    return root


def detect_game_tech(exe_path: Path, game_dir: Path | None = None) -> tuple[str, str]:
    """(graphics API text, engine text) for a game. Cheap to call again: results are cached."""
    try:
        st = exe_path.stat()
    except OSError:
        return _UNKNOWN, _UNKNOWN
    key = (str(exe_path), st.st_size, st.st_mtime_ns)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    root = safe_scan_root(exe_path, game_dir)
    tree = _Tree(root, must_list=exe_path.parent)
    engine, engine_binary = _engine(tree, exe_path)

    apis: set[str] = set()
    # Loose DLLs in the game folder: Agility SDK (D3D12/D3D12Core.dll), bundled Vulkan loader,
    # wrappers (dgVoodoo, DXVK, ReShade) only ever installed for the API the game uses.
    for name in tree.files:
        api = _GFX_DLLS.get(name)
        if api is not None and api != "opengl":  # a loose opengl32.dll is a wrapper, not a tell
            apis.add(api)
    for pattern, mapping in _BACKEND_MODULES:
        for m, _ in tree.matching(pattern):
            apis.add(mapping[m.group(1)])
    rhis = {m.group(1) for m, _ in tree.matching(_UE_RHI_RE)}
    if rhis - {"vulkan"}:  # every UE build ships Vulkan/OpenGL too: on Windows the game runs on Direct3D
        apis |= {"dx" + r[3:] for r in rhis if r != "vulkan"}
    elif rhis:
        apis.add("vulkan")
    elif tree.matching(_UE_OPENGL_RE):
        apis.add("opengl")
    if engine.startswith("Unity"):
        native = tree.files.get("unityplayer.so")
        if native is not None and "unityplayer.dll" not in tree.files:
            apis |= _scan_binary(native)  # Linux player: Vulkan/OpenGL as built
        # Unity's Windows player carries every backend, so only the default (Direct3D 11) is claimed.
        apis = apis or {"dx11"}
    elif engine.startswith("Ren'Py"):
        apis = apis or {"opengl"}  # the launcher exe is Python; the renderer is OpenGL (ANGLE on Windows)
    else:
        apis |= _scan_binary(engine_binary or exe_path)

    result = (_format_graphics(apis), engine)
    _cache[key] = result
    return result
