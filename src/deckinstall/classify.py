"""Classify what the user picked so the install flow takes the right path.

Kinds:
  archive      .zip/.rar/.7z/.tar.* (also split parts)  -> extract
  gog_setup    GOG offline installer (Inno Setup)       -> innoextract, fallback: run setup
  repack       folder with fg-*.bin / unarc.dll / ISDone.dll / setup-*.bin -> run its setup.exe
  installer    other Windows setup/installer .exe       -> run it
  native_sh    Linux .sh installer (MojoSetup / makeself) -> run it
  ready_folder folder with the game already installed   -> pick the executable
  exe          a single game .exe or Linux binary       -> pick it
  unknown      anything else
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .extractor import is_archive

ARCHIVE = "archive"
GOG_SETUP = "gog_setup"
REPACK = "repack"
INSTALLER = "installer"
NATIVE_SH = "native_sh"
READY_FOLDER = "ready_folder"
EXE = "exe"
UNKNOWN = "unknown"

INSTALL_KINDS = frozenset({ARCHIVE, GOG_SETUP, REPACK, INSTALLER, NATIVE_SH})

_REPACK_MARKERS = ("unarc.dll", "isdone.dll", "isskin.dll", "cls-srep.dll")
_REPACK_BIN_RE = re.compile(r"^(fg-\d+|setup-\d+|data-?\d+|.*-\d+)\.bin$", re.IGNORECASE)
_GOG_NAME_RE = re.compile(r"^setup_.+\.exe$", re.IGNORECASE)
_INSTALLER_WORDS = ("setup", "install")
# Folders and names that never hold the main installer (Hydra-style filtering).
_SKIP_PARTS = frozenset({
    "_redist", "redist", "redistributables", "_commonredist", "commonredist", "directx", "dotnet",
    "vcredist", "physx", "dlc", "dlcs", "patch", "patches", "update", "updates", "md5", "verify bin files",
    "support", "_support", "__redist", "extras", "bonus",
})
_SKIP_NAME_WORDS = (
    "redist", "vcredist", "directx", "dxsetup", "dotnet", "physx", "unins", "uninst", "dlc", "patch",
    "update", "quicksfv", "md5", "verify",
)
_SCAN_LIMIT = 4000
_HEAD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class SourceInfo:
    """What a picked path is and, for installers, which file to run."""

    kind: str
    path: Path
    installer: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_install(self) -> bool:
        return self.kind in INSTALL_KINDS


def _read_head(path: Path, limit: int = _HEAD_BYTES) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def is_inno_setup(path: Path) -> bool:
    """True when an .exe is an Inno Setup installer (the format GOG uses)."""
    head = _read_head(path)
    return head[:2] == b"MZ" and (b"Inno Setup" in head or b"InnoSetupLdrWindow" in head)


def is_gog_installer(path: Path) -> bool:
    """True for GOG offline installers: setup_*.exe built with Inno Setup."""
    if path.suffix.lower() != ".exe" or not is_inno_setup(path):
        return False
    if _GOG_NAME_RE.match(path.name):
        return True
    head = _read_head(path)
    return b"GOG.com" in head or b"gog.com" in head


def _is_elf(path: Path) -> bool:
    return _read_head(path, 4) == b"\x7fELF"


def _skip_part(name: str) -> bool:
    low = name.lower()
    return low in _SKIP_PARTS or low.startswith(("_redist", "redist"))


def _skip_name(name: str) -> bool:
    low = name.lower()
    return any(w in low for w in _SKIP_NAME_WORDS)


def is_repack_folder(folder: Path) -> bool:
    """True for FitGirl/DODI-style repacks: setup.exe next to unarc.dll/ISDone.dll or *.bin parts."""
    try:
        names = [p.name for p in folder.iterdir() if p.is_file() and not p.is_symlink()]
    except OSError:
        return False
    low = {n.lower() for n in names}
    has_setup = any(n.endswith(".exe") and any(w in n for w in _INSTALLER_WORDS) for n in low)
    if not has_setup:
        return False
    if any(m in low for m in _REPACK_MARKERS):
        return True
    bins = [n for n in names if _REPACK_BIN_RE.match(n)]
    return any(n.lower().startswith(("fg-", "setup-", "data")) for n in bins)


def find_setup_exe(folder: Path) -> Path | None:
    """Pick the main installer in folder like Hydra does: setup.exe at the top, skipping redist/DLC/patch."""
    candidates: list[tuple[int, int, str, Path]] = []
    scanned = 0
    stack: list[tuple[Path, int]] = [(folder, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > _SCAN_LIMIT:
                stack.clear()
                break
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < 2 and not _skip_part(entry.name):
                    stack.append((entry, depth + 1))
                continue
            name = entry.name.lower()
            if not name.endswith(".exe") or _skip_name(name):
                continue
            if name == "setup.exe":
                rank = 0
            elif name.startswith("setup"):
                rank = 1
            elif any(w in name for w in _INSTALLER_WORDS):
                rank = 2
            else:
                continue
            candidates.append((depth, rank, name, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return candidates[0][3]


def _is_installer_name(path: Path) -> bool:
    stem = path.stem.lower()
    return any(w in stem for w in _INSTALLER_WORDS) and not _skip_name(stem)


def _folder_has_executable(folder: Path) -> bool:
    queue: deque[tuple[Path, int]] = deque([(folder, 0)])
    scanned = 0
    while queue and scanned < _SCAN_LIMIT:
        cur, depth = queue.popleft()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > _SCAN_LIMIT:
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < 6 and not entry.name.startswith(".") and not _skip_part(entry.name):
                        queue.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                low = entry.name.lower()
                if low.endswith((".exe", ".x86_64")):
                    return True
                if depth <= 2 and (entry.stat(follow_symlinks=False).st_mode & 0o111) and _is_elf(Path(entry.path)):
                    return True
            except OSError:
                continue
    return False


def _has_other_game_exe(folder: Path, setup: Path) -> bool:
    queue: deque[Path] = deque([folder])
    scanned = 0
    while queue and scanned < _SCAN_LIMIT:
        cur = queue.popleft()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > _SCAN_LIMIT:
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith(".") and not _skip_part(entry.name):
                        queue.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".exe"):
                    p = Path(entry.path)
                    if p != setup and not _skip_name(entry.name):
                        return True
            except OSError:
                continue
    return False


def classify_source(path: Path) -> SourceInfo:
    """Classify a file or folder picked by the user. Reads only file headers; changes nothing."""
    if path.is_symlink() and not path.exists():
        return SourceInfo(UNKNOWN, path)
    if path.is_dir():
        if is_repack_folder(path):
            setup = find_setup_exe(path)
            if setup is not None:
                return SourceInfo(REPACK, path, installer=setup)
        top_setups = [
            p for p in sorted(path.glob("*.exe"))
            if p.is_file() and not p.is_symlink() and _GOG_NAME_RE.match(p.name) and is_gog_installer(p)
        ]
        if top_setups:
            return SourceInfo(GOG_SETUP, path, installer=top_setups[0])
        if _folder_has_executable(path):
            setup = find_setup_exe(path)
            only_setup = setup is not None and setup.parent == path and not _has_other_game_exe(path, setup)
            if only_setup and setup is not None:
                return SourceInfo(INSTALLER, path, installer=setup)
            return SourceInfo(READY_FOLDER, path)
        return SourceInfo(UNKNOWN, path)

    if not path.is_file():
        return SourceInfo(UNKNOWN, path)
    if is_archive(path):
        return SourceInfo(ARCHIVE, path, installer=path)
    suffix = path.suffix.lower()
    if suffix == ".sh":
        if path.stem.lower() in {"start", "launch", "run"}:
            return SourceInfo(EXE, path)
        return SourceInfo(NATIVE_SH, path, installer=path)
    if suffix == ".exe":
        if _skip_name(path.name):
            return SourceInfo(UNKNOWN, path)
        if _is_installer_name(path) and is_repack_folder(path.parent):
            return SourceInfo(REPACK, path.parent, installer=path)
        if is_gog_installer(path):
            return SourceInfo(GOG_SETUP, path, installer=path)
        if _is_installer_name(path) or is_inno_setup(path):
            return SourceInfo(INSTALLER, path, installer=path)
        return SourceInfo(EXE, path)
    if _is_elf(path):
        return SourceInfo(EXE, path)
    return SourceInfo(UNKNOWN, path)


_RELEASE_TAG_RE = re.compile(
    r"\s*(\[[^\]]*\]|\([^)]*(repack|fitgirl|dodi|elamigos|kaos|xatab|gog|rune|codex)[^)]*\))",
    re.IGNORECASE,
)


def _strip_release_tags(name: str) -> str:
    """Drop repack/release tags such as "[FitGirl Repack]" or "(DODI Repack)" from a folder name."""
    return _RELEASE_TAG_RE.sub("", name).strip() or name


def suggest_install_name(info: SourceInfo) -> str:
    """Suggest a game folder name for an install source."""
    from .extractor import suggest_target_name
    from .scanner import clean_game_name

    installer = info.installer or info.path
    if info.kind == GOG_SETUP and _GOG_NAME_RE.match(installer.name):
        stem = installer.stem[len("setup_"):]
        base = re.split(r"_(?=v?\d+\.\d|\()", stem)[0]
        words = [w for w in base.replace("-", " ").split("_") if w]
        if words:
            return " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)
    if info.kind in (REPACK, INSTALLER) and info.path.is_dir():
        return clean_game_name(_strip_release_tags(info.path.name))
    if info.kind in (REPACK, INSTALLER) and installer.stem.lower() in ("setup", "install", "installer"):
        return clean_game_name(_strip_release_tags(installer.parent.name))
    return suggest_target_name(installer)
