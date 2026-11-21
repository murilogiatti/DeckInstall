"""Static detection of a game's graphics API and engine (with versions) for the library table.

Nothing is executed: the game folder is walked a few levels deep for tell-tale files, and the
main binary is searched for DLL names it references. The graphics result is therefore what the
game supports, not what it will pick at runtime.
"""

from __future__ import annotations

import mmap
import os
import re
from pathlib import Path

_MAX_DEPTH = 4
_MAX_ENTRIES = 6000
_MAX_SCAN_BYTES = 768 * 1024 * 1024
_UNKNOWN = "-"

_DX_DLLS = {
    b"d3d8": "8",
    b"d3d9": "9",
    b"d3d10": "10",
    b"d3d11": "11",
    b"d3d12": "12",
}
_UNITY_VERSION_RE = re.compile(rb"\d{4}\.\d+\.\d+[fpab]\d+|\d\.\d+\.\d+[fpab]\d+")
_UE_VERSION_RE = re.compile(rb"\+\+UE([45])\+Release-(\d+\.\d+)")
_UE_VERSION_W_RE = re.compile(
    rb"\+\x00\+\x00U\x00E\x00([45])\x00\+\x00R\x00e\x00l\x00e\x00a\x00s\x00e\x00-\x00"
    rb"(\d)\x00\.\x00(\d)\x00(?:(\d)\x00)?"
)
# Stripped builds only keep the file version resource, "UE5-CL-0": the major version.
_UE_MAJOR_RE = re.compile(rb"U\x00E\x00([45])\x00-\x00C\x00L\x00")
# Crash client tag in Core of modular UE4 builds without a version string ("UE4CC-Windows-...").
_UE_CC_RE = re.compile(rb"U\x00E\x00([45])\x00C\x00C\x00-\x00")
# Modular Unreal builds keep each graphics backend in its own module: <Game>-D3D12RHI-Win64-Shipping.dll.
_UE_RHI_RE = re.compile(r"(?:^|-)(d3d11|d3d12|vulkan)rhi(?:-win64-shipping)?\.dll$")
_GODOT_VERSION_RE = re.compile(rb"Godot Engine v(\d+\.\d+(?:\.\d+)?)")

_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def _variants(name: bytes) -> tuple[bytes, ...]:
    return (name + b".dll", name.upper() + b".dll", name.upper() + b".DLL")


def _scan_binary(path: Path) -> set[str]:
    """Graphics APIs referenced by a PE/ELF binary: {'dx9','dx11','dx12','vulkan','opengl'...}."""
    found: set[str] = set()
    try:
        size = path.stat().st_size
        if size == 0 or size > _MAX_SCAN_BYTES:
            return found
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for stem, ver in _DX_DLLS.items():
                if any(mm.find(v) != -1 for v in _variants(stem)):
                    found.add("dx" + ver)
            if any(mm.find(v) != -1 for v in _variants(b"vulkan-1")) or mm.find(b"libvulkan.so") != -1:
                found.add("vulkan")
            if any(mm.find(v) != -1 for v in _variants(b"opengl32")) or mm.find(b"libGL.so") != -1:
                found.add("opengl")
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


class _Tree:
    """File names collected from a bounded walk of the game folder."""

    def __init__(self, root: Path) -> None:
        self.files: dict[str, Path] = {}  # lowercase name -> first path seen
        self.dirs: dict[str, Path] = {}
        count = 0
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            cur = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (cur / d).is_symlink()]
            for d in dirnames:
                self.dirs.setdefault(d.lower(), cur / d)
            for f in filenames:
                self.files.setdefault(f.lower(), cur / f)
            count += len(filenames) + len(dirnames)
            if count > _MAX_ENTRIES or len(cur.parts) - base >= _MAX_DEPTH:
                dirnames[:] = []

    def first_suffix(self, suffix: str) -> Path | None:
        for name, p in self.files.items():
            if name.endswith(suffix):
                return p
        return None


def _unity(tree: _Tree) -> str | None:
    """Unity version like '2021.3.5f1', '' when it is Unity of unknown version, None when not Unity."""
    marker = tree.files.get("unityplayer.dll") or tree.files.get("unityplayer.so")
    manager = next((p for n, p in tree.files.items() if n in ("globalgamemanagers", "data.unity3d")), None)
    if marker is None and manager is None:
        return None
    for source in (manager, tree.files.get("globalgamemanagers")):
        if source is None:
            continue
        try:
            with open(source, "rb") as fh:
                m = _UNITY_VERSION_RE.search(fh.read(1 << 16))
        except OSError:
            continue
        if m:
            return m.group(0).decode("ascii")
    return ""


def _unreal(tree: _Tree, exe: Path) -> tuple[str, Path | None] | None:
    """('UE4'/'UE5'/'', version) and the shipping binary for Unreal games; None when not Unreal."""
    shipping = next((p for n, p in tree.files.items() if n.endswith("-win64-shipping.exe")), None)
    if shipping is None:
        shipping = next((p for n, p in tree.files.items() if n.endswith("-linux-shipping")), None)
    has_engine_dir = "engine" in tree.dirs and ("content" in tree.dirs or shipping is not None)
    if shipping is None and not has_engine_dir:
        return None
    probes = [p for p in (shipping, exe) if p is not None and p.is_file()]
    crash = tree.files.get("crashreportclient.exe")
    if crash is not None:
        probes.append(crash)
    core = next((p for n, p in tree.files.items() if n.endswith("-core-win64-shipping.dll")), None)
    if core is not None:  # modular build: the engine version lives in Core, not in the .exe
        probes.append(core)
    for probe in probes:
        m = _find_regex(probe, _UE_VERSION_RE)
        if m:
            return f"UE{m[0].decode()} {m[1].decode()}", shipping
        w = _find_regex(probe, _UE_VERSION_W_RE)
        if w:
            minor = w[2].decode() + (w[3].decode() if w[3] else "")
            return f"UE{w[0].decode()} {w[1].decode()}.{minor}", shipping
        major = _find_regex(probe, _UE_MAJOR_RE) or _find_regex(probe, _UE_CC_RE)
        if major:
            return f"UE{major[0].decode()}", shipping
    return "Unreal Engine", shipping


def _engine(tree: _Tree, exe: Path) -> tuple[str, Path | None]:
    """(engine label with version when known, the binary that carries the graphics imports)."""
    unity = _unity(tree)
    if unity is not None:
        return (f"Unity {unity}" if unity else "Unity"), None
    unreal = _unreal(tree, exe)
    if unreal is not None:
        return unreal[0], unreal[1]
    pck = tree.first_suffix(".pck")
    for probe in [exe] + ([pck] if pck is not None else []):
        m = _find_regex(probe, _GODOT_VERSION_RE)
        if m:
            return f"Godot {m[0].decode()}", None
    if pck is not None:
        return "Godot", None
    if "data.win" in tree.files or "game.droid" in tree.files:
        return "GameMaker", None
    if "rpg_rt.exe" in tree.files or "rpg_core.js" in tree.files or "rmmz_core.js" in tree.files:
        return ("RPG Maker MZ" if "rmmz_core.js" in tree.files else "RPG Maker"), None
    if "crysystem.dll" in tree.files:
        return "CryEngine", None
    if "renpy" in tree.dirs:
        return "Ren'Py", None
    if "tier0.dll" in tree.files and "vstdlib.dll" in tree.files:
        return "Source", None
    if "monogame.framework.dll" in tree.files:
        return "MonoGame", None
    if "fna.dll" in tree.files:
        return "FNA", None
    if "libcef.dll" in tree.files and "package.nw" in tree.files:
        return "NW.js", None
    return "Other", None


def _format_graphics(apis: set[str]) -> str:
    parts: list[str] = []
    dx = sorted((a[2:] for a in apis if a.startswith("dx")), key=int)
    if dx:
        parts.append("DirectX " + "/".join(dx))
    if "vulkan" in apis:
        parts.append("Vulkan")
    if not parts and "opengl" in apis:
        parts.append("OpenGL")
    return " + ".join(parts) if parts else _UNKNOWN


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

    root = exe_path.parent
    if game_dir is not None and game_dir.is_dir() and not game_dir.is_symlink():
        try:
            resolved_game = game_dir.resolve()
            if root.resolve().is_relative_to(resolved_game) and resolved_game not in (
                Path("/"),
                Path.home().resolve(),
                Path.home().resolve() / "Games",
            ):
                root = game_dir
        except OSError:
            pass
    tree = _Tree(root)
    engine, engine_binary = _engine(tree, exe_path)

    apis: set[str] = set()
    for name in tree.files:
        if name == "vulkan-1.dll":
            apis.add("vulkan")
        elif name in ("d3d12core.dll", "d3d12.dll"):
            apis.add("dx12")
        elif name in ("d3d11.dll", "d3d10.dll", "d3d9.dll", "d3d8.dll"):
            apis.add("dx" + name[3:].split(".")[0])
    rhis = {m.group(1) for name in tree.files if (m := _UE_RHI_RE.search(name))}
    if rhis - {"vulkan"}:  # every UE build ships Vulkan/OpenGL too: on Windows the game runs on Direct3D
        apis |= {"dx" + r[3:] for r in rhis if r != "vulkan"}
    elif rhis:
        apis.add("vulkan")
    if engine.startswith("Unity"):
        apis = apis or {"dx11"}  # Unity's player carries every backend, so only the default is claimed
    else:
        apis |= _scan_binary(engine_binary or exe_path)

    result = (_format_graphics(apis), engine)
    _cache[key] = result
    return result
