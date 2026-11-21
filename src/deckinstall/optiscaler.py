"""OptiScaler (upscale-only) per-game installer, INI patcher, and uninstaller."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fsutil import atomic_copy_file, atomic_write_text, prune_empty_parents
from .gameinfo import safe_scan_root
from .runtime_manager import ProgressCb, _load_versions, ensure_optiscaler
from .scanner import detect_exe_kind, detect_pe_arch

OPTISCALER_PROXY_DLLS: tuple[str, ...] = ("dxgi", "winmm", "version", "dbghelp")
OPTISCALER_BACKENDS: tuple[str, ...] = ("auto", "fsr3", "fsr22", "fsr21", "xess", "dlss")
OPTISCALER_FG_MODES: tuple[str, ...] = ("auto", "nofg", "optifg", "nukems")
OPTISCALER_SPOOF_MODES: tuple[str, ...] = ("auto", "true", "false")
OPTISCALER_MANIFEST = ".deckinstall_optiscaler.json"

_DLSS_DLL_NAMES: frozenset[str] = frozenset(
    {
        "nvngx_dlss.dll",
        "nvngx_dlssd.dll",
        "nvngx_dlssg.dll",
        "sl.interposer.dll",
        "sl.dlss.dll",
        "sl.dlss_g.dll",
    }
)
_FSR_DLL_NAMES: frozenset[str] = frozenset(
    {
        "ffx_fsr2_api_x64.dll",
        "ffx_fsr2_api_dx12_x64.dll",
        "ffx_fsr2_api_vk_x64.dll",
        "ffx_fsr3_x64.dll",
        "ffx_fsr3upscaler_x64.dll",
        "ffx_framegeneration_x64.dll",
        "amd_fidelityfx_dx12.dll",
        "amd_fidelityfx_vk.dll",
        "amd_fidelityfx_upscaler_dx12.dll",
        "amd_fidelityfx_framegeneration_dx12.dll",
    }
)
_XESS_DLL_NAMES: frozenset[str] = frozenset({"libxess.dll", "libxess_fg.dll"})

_BACKEND_UPSCALER_MAP: dict[str, str] = {
    "auto": "auto",
    "fsr3": "fsr31",
    "fsr22": "fsr22",
    "fsr21": "fsr21",
    "xess": "xess",
    "dlss": "dlss",
}
_SKIPPED_SCRIPT_SUFFIXES = frozenset({".bat", ".sh", ".cmd"})


def _normalize_fg_mode(fg_mode: str) -> str:
    """Normalize legacy 'default' FG mode to 'auto' and validate against OPTISCALER_FG_MODES."""
    normalized = "auto" if fg_mode == "default" else fg_mode
    if normalized not in OPTISCALER_FG_MODES:
        raise ValueError(
            f"Unsupported OptiScaler FG mode {fg_mode!r}; expected one of {OPTISCALER_FG_MODES}."
        )
    return normalized


class OptiScalerConflictError(RuntimeError):
    """Raised when an unmanaged proxy DLL already exists and overwrite_existing is False."""

    def __init__(self, existing_path: Path, message: str | None = None) -> None:
        self.existing_path = existing_path
        super().__init__(
            message
            or (
                f"Existing DLL already present at {existing_path}; "
                "pass overwrite_existing=True to back it up and replace it."
            )
        )


@dataclass(frozen=True)
class OptiScalerState:
    """Persisted state of an OptiScaler installation for a game executable."""

    proxy_dll: str
    backend: str
    version: str
    exe_dir: Path
    installed_files: list[Path]
    backed_up_files: list[Path]
    added_override: str
    fg_mode: str = "auto"
    spoof_mode: str = "auto"
    overlay_menu: bool = True
    prefix_snapshot: Path | None = None
    # False while files are being copied: a crash leaves a journal that "remove" can undo.
    complete: bool = True


@dataclass(frozen=True)
class OptiScalerResult:
    """Result summary for an OptiScaler uninstall operation."""

    state: OptiScalerState | None
    removed_count: int = 0
    restored_count: int = 0


@dataclass(frozen=True)
class OptiScalerCompatibility:
    """Compatibility assessment of a game executable and folder for OptiScaler."""

    compatible: bool
    tier: str  # "verified" | "likely" | "incompatible"
    pe_arch: str
    upscalers: tuple[str, ...]
    installed_state: OptiScalerState | None = None
    recommended_proxy: str = "dxgi"
    notes: tuple[str, ...] = ()


def _resolve_upscaler_scan_root(exe_path: Path, game_dir: Path | None) -> Path:
    """Choose a safe directory root to scan for DLSS/FSR/XeSS upscaler DLLs."""
    return safe_scan_root(exe_path, game_dir)


def check_optiscaler_compatibility(
    exe_path: Path,
    game_dir: Path | None = None,
) -> OptiScalerCompatibility:
    """Inspect executable architecture and game DLLs to determine OptiScaler compatibility."""
    if not exe_path.is_file() or not exe_path.parent.is_dir():
        return OptiScalerCompatibility(
            compatible=False,
            tier="incompatible",
            pe_arch="unknown",
            upscalers=(),
            installed_state=None,
        )

    pe_arch = detect_pe_arch(exe_path)
    exe_kind = detect_exe_kind(exe_path)
    if pe_arch in ("linux_elf", "x86", "arm64") or exe_kind != "windows_pe":
        return OptiScalerCompatibility(
            compatible=False,
            tier="incompatible",
            pe_arch=pe_arch,
            upscalers=(),
            installed_state=None,
        )

    scan_root = _resolve_upscaler_scan_root(exe_path, game_dir)
    has_dlss = False
    has_fsr = False
    has_xess = False
    base_parts = len(scan_root.parts)

    try:
        for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
            cur = Path(dirpath)
            depth = len(cur.parts) - base_parts
            if depth >= 5:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not d.startswith(".") and not (cur / d).is_symlink()
                ]
            for fname in filenames:
                low = fname.lower()
                if not low.endswith(".dll"):
                    continue
                if low in _DLSS_DLL_NAMES:
                    has_dlss = True
                elif low in _FSR_DLL_NAMES:
                    has_fsr = True
                elif low in _XESS_DLL_NAMES:
                    has_xess = True
            if has_dlss and has_fsr and has_xess:
                break
    except OSError:
        pass

    found: list[str] = []
    if has_dlss:
        found.append("DLSS")
    if has_fsr:
        found.append("FSR")
    if has_xess:
        found.append("XeSS")
    upscalers = tuple(found)
    tier = "verified" if upscalers else "likely"
    installed_state = inspect_optiscaler_state(exe_path)
    proxy, notes = recommend_proxy(exe_path, game_dir, installed_state)
    return OptiScalerCompatibility(
        compatible=True,
        tier=tier,
        pe_arch=pe_arch,
        upscalers=upscalers,
        installed_state=installed_state,
        recommended_proxy=proxy,
        notes=notes,
    )


def recommend_proxy(
    exe_path: Path,
    game_dir: Path | None = None,
    installed_state: OptiScalerState | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Pick the proxy DLL least likely to clash with what the game already ships.

    Returns (proxy, notes). Notes are short machine keys: "dxgi_taken", "winmm_onlinefix",
    "winmm_taken", "version_taken", "dbghelp_taken", "all_taken".
    """
    if installed_state is not None:
        return installed_state.proxy_dll, ()
    from .scanner import detect_steam_emulator

    exe_dir = exe_path.parent
    try:
        present = {p.name.lower() for p in exe_dir.iterdir() if p.is_file() or p.is_symlink()}
    except OSError:
        present = set()
    emulator, _ = detect_steam_emulator(exe_path, game_dir or exe_dir)
    notes: list[str] = []
    for proxy in OPTISCALER_PROXY_DLLS:
        if proxy == "winmm" and emulator == "onlinefix":
            notes.append("winmm_onlinefix")  # OnlineFix loads itself through winmm.dll
            continue
        if f"{proxy}.dll" in present:
            notes.append(f"{proxy}_taken")  # e.g. ReShade/DXVK/another mod already uses it
            continue
        return proxy, tuple(notes)
    notes.append("all_taken")
    return "dxgi", tuple(notes)




def _resolve_manifest_path(exe_dir: Path, raw: str) -> Path | None:
    """Resolve a manifest-relative path strictly inside exe_dir, rejecting symlinks and escapes."""
    raw_str = raw.strip()
    if not raw_str:
        return None
    p = Path(raw_str)
    if p.is_absolute():
        return None
    try:
        base = exe_dir.resolve()
        cur = exe_dir
        for part in p.parts:
            if part in ("", "."):
                continue
            cur = cur / part
            if cur.is_symlink():
                return None
        resolved = (exe_dir / p).resolve()
        resolved.relative_to(base)
        if resolved == base:
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _serialize_manifest_path(exe_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(exe_dir))
    except ValueError:
        return str(path)


def inspect_optiscaler_state(exe_path: Path) -> OptiScalerState | None:
    """Inspect the game directory for an OptiScaler installation manifest."""
    exe_dir = exe_path.parent.resolve()
    manifest_path = exe_dir / OPTISCALER_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    proxy_dll = str(data.get("proxy_dll", "")).strip()
    if proxy_dll not in OPTISCALER_PROXY_DLLS:
        return None
    backend = str(data.get("backend", "auto")).strip()
    version = str(data.get("version", "")).strip()
    installed_raw = data.get("installed_files", [])
    backed_raw = data.get("backed_up_files", [])
    installed_files = (
        [
            resolved
            for p in installed_raw
            if str(p) and (resolved := _resolve_manifest_path(exe_dir, str(p))) is not None
        ]
        if isinstance(installed_raw, list)
        else []
    )
    backed_up_files = (
        [
            resolved
            for p in backed_raw
            if str(p) and (resolved := _resolve_manifest_path(exe_dir, str(p))) is not None
        ]
        if isinstance(backed_raw, list)
        else []
    )
    added_override = str(data.get("added_override", "")).strip()
    raw_fg = str(data.get("fg_mode", "auto")).strip() or "auto"
    fg_mode = "auto" if raw_fg == "default" else raw_fg
    spoof_mode = str(data.get("spoof_mode", "auto")).strip() or "auto"
    overlay_raw = data.get("overlay_menu", True)
    overlay_menu = (
        overlay_raw
        if isinstance(overlay_raw, bool)
        else str(overlay_raw).strip().lower() not in ("false", "0", "no")
    )
    raw_snapshot = data.get("prefix_snapshot")
    prefix_snapshot = (
        Path(raw_snapshot.strip())
        if isinstance(raw_snapshot, str) and raw_snapshot.strip()
        else None
    )
    return OptiScalerState(
        proxy_dll=proxy_dll,
        backend=backend,
        version=version,
        exe_dir=exe_dir,
        installed_files=installed_files,
        backed_up_files=backed_up_files,
        added_override=added_override,
        fg_mode=fg_mode,
        spoof_mode=spoof_mode,
        overlay_menu=overlay_menu,
        prefix_snapshot=prefix_snapshot,
        complete=data.get("complete", True) is not False,
    )


def _write_state_manifest(state: OptiScalerState) -> None:
    exe_dir = state.exe_dir
    manifest_data = {
        "proxy_dll": state.proxy_dll,
        "backend": state.backend,
        "version": state.version,
        "exe_dir": str(exe_dir),
        "installed_files": [_serialize_manifest_path(exe_dir, p) for p in state.installed_files],
        "backed_up_files": [_serialize_manifest_path(exe_dir, p) for p in state.backed_up_files],
        "added_override": state.added_override,
        "fg_mode": state.fg_mode,
        "spoof_mode": state.spoof_mode,
        "overlay_menu": state.overlay_menu,
        "prefix_snapshot": str(state.prefix_snapshot) if state.prefix_snapshot is not None else None,
        "complete": state.complete,
    }
    atomic_write_text(
        exe_dir / OPTISCALER_MANIFEST,
        json.dumps(manifest_data, indent=2, sort_keys=True) + "\n",
    )


def patch_optiscaler_ini(
    ini_text: str,
    backend: str = "auto",
    fg_mode: str = "auto",
    spoof_mode: str = "auto",
    overlay_menu: bool = True,
) -> str:
    """Patch OptiScaler.ini with upscaler backend, FG mode, DXGI spoofing, and overlay menu."""
    if backend not in OPTISCALER_BACKENDS:
        raise ValueError(
            f"Unsupported OptiScaler backend {backend!r}; expected one of {OPTISCALER_BACKENDS}."
        )
    fg_mode = _normalize_fg_mode(fg_mode)
    if spoof_mode not in OPTISCALER_SPOOF_MODES:
        raise ValueError(
            f"Unsupported OptiScaler spoof mode {spoof_mode!r}; expected one of {OPTISCALER_SPOOF_MODES}."
        )
    upscaler_value = _BACKEND_UPSCALER_MAP[backend]

    section_specs: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Upscalers",
            [
                ("Dx12Upscaler", upscaler_value),
                ("Dx11Upscaler", upscaler_value),
                ("VulkanUpscaler", upscaler_value),
            ],
        ),
        ("FrameGen", [("FGType", fg_mode)]),
        ("Spoofing", [("Dxgi", spoof_mode)]),
        ("Menu", [("OverlayMenu", "true" if overlay_menu else "false")]),
    ]
    key_targets: dict[tuple[str, str], tuple[str, str]] = {
        (sec_name.lower(), key.lower()): (key, value)
        for sec_name, items in section_specs
        for key, value in items
    }

    lines = ini_text.splitlines()
    found_keys: set[tuple[str, str]] = set()
    section_bounds: dict[str, int] = {}
    current_section_lower: str | None = None
    last_nonblank_in_section: dict[str, int] = {}

    updated_lines: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            sec_name = stripped[1 : stripped.index("]")].strip()
            current_section_lower = sec_name.lower()
            section_bounds.setdefault(current_section_lower, idx)
            last_nonblank_in_section[current_section_lower] = idx
            updated_lines.append(line)
            continue

        if current_section_lower is not None and stripped:
            last_nonblank_in_section[current_section_lower] = idx

        if (
            current_section_lower is not None
            and stripped
            and not stripped.startswith((";", "#"))
            and "=" in line
        ):
            lhs, _ = line.split("=", 1)
            key_clean = lhs.strip()
            target_key = (current_section_lower, key_clean.lower())
            target = key_targets.get(target_key)
            if target is not None:
                canonical_key, target_val = target
                found_keys.add(target_key)
                updated_lines.append(f"{canonical_key}={target_val}")
                continue
        updated_lines.append(line)

    # Insert any missing keys into existing sections (back-to-front to keep indices valid)
    insertions: list[tuple[int, list[str]]] = []
    append_sections: list[tuple[str, list[str]]] = []

    for section_name, items in section_specs:
        sec_low = section_name.lower()
        missing_lines = [
            f"{key}={val}" for key, val in items if (sec_low, key.lower()) not in found_keys
        ]
        if not missing_lines:
            continue
        if sec_low in last_nonblank_in_section:
            insertions.append((last_nonblank_in_section[sec_low] + 1, missing_lines))
        else:
            append_sections.append((section_name, missing_lines))

    for insert_idx, new_lines in sorted(insertions, key=lambda item: item[0], reverse=True):
        updated_lines[insert_idx:insert_idx] = new_lines

    for section_name, new_lines in append_sections:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(f"[{section_name}]")
        updated_lines.extend(new_lines)

    return ("\n".join(updated_lines) + "\n") if updated_lines else ""


def _same_file_content(a: Path, b: Path) -> bool:
    """Byte-compare two regular files; False on any doubt."""
    try:
        if a.is_symlink() or b.is_symlink() or not (a.is_file() and b.is_file()):
            return False
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _backup_if_needed(
    target_path: Path,
    managed_paths: set[Path],
    backed_up_files: list[Path],
) -> None:
    """Move an existing unmanaged file to .deckinstall.bak if no backup exists yet."""
    backup_path = target_path.with_name(f"{target_path.name}.deckinstall.bak")
    if not (target_path.exists() or target_path.is_symlink()):
        # An interrupted older installation may already have moved this file.
        # Keep its original backup tracked so uninstall can still restore it.
        if backup_path.is_file() and not backup_path.is_symlink() and target_path not in backed_up_files:
            backed_up_files.append(target_path)
        return
    try:
        resolved = target_path.resolve()
    except OSError:
        resolved = target_path
    if resolved in managed_paths:
        return
    if not (backup_path.exists() or backup_path.is_symlink()):
        os.replace(target_path, backup_path)
    elif _same_file_content(target_path, backup_path):
        target_path.unlink(missing_ok=True)
    else:
        # A stale backup already exists and the current file differs (e.g. the game was
        # updated). Never delete it: keep it next to the original backup.
        idx = 1
        extra = target_path.with_name(f"{backup_path.name}.{idx}")
        while extra.exists() or extra.is_symlink():
            idx += 1
            extra = target_path.with_name(f"{backup_path.name}.{idx}")
        os.replace(target_path, extra)
        logging.warning("Preserved unmanaged %s as %s (backup already existed)", target_path, extra)
    if target_path not in backed_up_files:
        backed_up_files.append(target_path)


def _backup_optiscaler_ini_variants(
    exe_dir: Path,
    managed_paths: set[Path],
    backed_up_files: list[Path],
) -> None:
    """Back up any case-variant optiscaler.ini files as well as OptiScaler.ini."""
    try:
        entries = list(exe_dir.iterdir())
    except OSError:
        entries = []
    for p in entries:
        if p.name.lower() == "optiscaler.ini" and p.name != "OptiScaler.ini":
            _backup_if_needed(p, managed_paths, backed_up_files)
    _backup_if_needed(exe_dir / "OptiScaler.ini", managed_paths, backed_up_files)


def _write_patched_ini(
    exe_dir: Path,
    ini_raw: str,
    *,
    backend: str,
    fg_mode: str,
    spoof_mode: str,
    overlay_menu: bool,
    managed_paths: set[Path],
    backed_up_files: list[Path],
) -> Path:
    """Back up any existing OptiScaler.ini (including case variants) and write patched INI."""
    _backup_optiscaler_ini_variants(exe_dir, managed_paths, backed_up_files)
    dst_ini = exe_dir / "OptiScaler.ini"
    if dst_ini.is_symlink():
        dst_ini.unlink()
    patched_ini = patch_optiscaler_ini(
        ini_raw,
        backend=backend,
        fg_mode=fg_mode,
        spoof_mode=spoof_mode,
        overlay_menu=overlay_menu,
    )
    atomic_write_text(dst_ini, patched_ini)
    return dst_ini


def install_optiscaler_for_game(
    exe_path: Path,
    proxy_dll: str = "dxgi",
    backend: str = "auto",
    overwrite_existing: bool = False,
    payload_dir: Path | None = None,
    progress_cb: ProgressCb | None = None,
    fg_mode: str = "auto",
    spoof_mode: str = "auto",
    overlay_menu: bool = True,
    prefix_dir: Path | None = None,
    game_name: str = "",
) -> OptiScalerState:
    """Install OptiScaler alongside a Windows game executable."""
    if proxy_dll not in OPTISCALER_PROXY_DLLS:
        raise ValueError(
            f"Unsupported OptiScaler proxy DLL {proxy_dll!r}; expected one of {OPTISCALER_PROXY_DLLS}."
        )
    if backend not in OPTISCALER_BACKENDS:
        raise ValueError(
            f"Unsupported OptiScaler backend {backend!r}; expected one of {OPTISCALER_BACKENDS}."
        )
    fg_mode = _normalize_fg_mode(fg_mode)
    if spoof_mode not in OPTISCALER_SPOOF_MODES:
        raise ValueError(
            f"Unsupported OptiScaler spoof mode {spoof_mode!r}; expected one of {OPTISCALER_SPOOF_MODES}."
        )
    if detect_exe_kind(exe_path) == "linux_elf":
        raise ValueError(f"OptiScaler cannot be installed for native Linux executable: {exe_path}")

    exe_dir = exe_path.parent.resolve()
    if not exe_dir.is_dir():
        raise FileNotFoundError(f"Game directory does not exist: {exe_dir}")

    source_dir = payload_dir if payload_dir is not None else ensure_optiscaler(progress_cb=progress_cb)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"OptiScaler payload directory not found: {source_dir}")

    has_dll = any(
        p.is_file() and not p.is_symlink() and p.name.lower() == "optiscaler.dll"
        for p in source_dir.iterdir()
    )
    if not has_dll:
        raise FileNotFoundError(f"OptiScaler.dll not found in payload directory: {source_dir}")

    existing_state = inspect_optiscaler_state(exe_path)
    managed_paths: set[Path] = set()
    backed_up_files: list[Path] = []
    if existing_state is not None:
        for p in existing_state.installed_files:
            try:
                managed_paths.add(p.resolve())
            except OSError:
                managed_paths.add(p)
        backed_up_files.extend(existing_state.backed_up_files)

    target_proxy_path = exe_dir / f"{proxy_dll}.dll"
    if target_proxy_path.exists() or target_proxy_path.is_symlink():
        try:
            resolved_proxy = target_proxy_path.resolve()
        except OSError:
            resolved_proxy = target_proxy_path
        if resolved_proxy not in managed_paths:
            if not overwrite_existing:
                raise OptiScalerConflictError(target_proxy_path)

    prefix_snapshot: Path | None = (
        existing_state.prefix_snapshot if existing_state is not None else None
    )
    if prefix_dir is not None and prefix_dir.is_dir():
        from .save_sync import snapshot_prefix_state

        snap = snapshot_prefix_state(
            prefix_dir,
            game_name or exe_path.stem,
            "pre-optiscaler",
            include_system_dlls=False,
        )
        if snap is not None:
            prefix_snapshot = snap.snapshot_zip

    # Complete the snapshot before moving any existing game DLL. OptiScaler
    # modifies game-folder files, so system DLLs do not belong in its snapshot.
    version = ""
    if payload_dir is None:
        version = str(_load_versions().get("optiscaler_installed_version", "")).strip()
    added_override = f"{proxy_dll}=n,b"
    installed_files: list[Path] = []
    # Files of the previous install are still on disk until they are replaced: track them too,
    # so an interrupted reinstall can still be undone.
    if existing_state is not None:
        installed_files.extend(p for p in existing_state.installed_files if p not in installed_files)

    def _state(complete: bool) -> OptiScalerState:
        return OptiScalerState(
            proxy_dll=proxy_dll,
            backend=backend,
            version=version,
            exe_dir=exe_dir,
            installed_files=list(installed_files),
            backed_up_files=list(backed_up_files),
            added_override=added_override,
            fg_mode=fg_mode,
            spoof_mode=spoof_mode,
            overlay_menu=overlay_menu,
            prefix_snapshot=prefix_snapshot,
            complete=complete,
        )

    def _journal() -> None:
        _write_state_manifest(_state(False))

    try:
        _journal()
        _backup_if_needed(target_proxy_path, managed_paths, backed_up_files)
        _journal()

        # Remove stale files from a previous DeckInstall OptiScaler install (e.g. proxy DLL change)
        if existing_state is not None:
            for old_file in existing_state.installed_files:
                if old_file.is_file() or old_file.is_symlink():
                    old_file.unlink(missing_ok=True)
            installed_files.clear()
            _journal()
            if existing_state.proxy_dll != proxy_dll:
                prior_proxy_path = exe_dir / f"{existing_state.proxy_dll}.dll"
                prior_backup = prior_proxy_path.with_name(f"{prior_proxy_path.name}.deckinstall.bak")
                if (
                    prior_backup.is_file()
                    and not prior_backup.is_symlink()
                    and not prior_proxy_path.exists()
                    and not prior_proxy_path.is_symlink()
                ):
                    os.replace(prior_backup, prior_proxy_path)
                    backed_up_files = [p for p in backed_up_files if p != prior_proxy_path]

        saw_ini = False

        for root_s, dirs_s, files_s in os.walk(source_dir, followlinks=False):
            root_p = Path(root_s)
            rel_dir = root_p.relative_to(source_dir)
            is_payload_root = rel_dir == Path(".")
            for d in list(dirs_s):
                if (root_p / d).is_symlink():
                    dirs_s.remove(d)
            for fname in files_s:
                src_file = root_p / fname
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                low_name = fname.lower()
                if src_file.suffix.lower() in _SKIPPED_SCRIPT_SUFFIXES:
                    continue
                if is_payload_root and "readme" in low_name and src_file.suffix.lower() in (".txt", ".md"):
                    continue  # the release's "extract all files" note is not part of OptiScaler

                if is_payload_root and low_name == "optiscaler.dll":
                    dst_file = target_proxy_path
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    if dst_file.is_symlink():
                        dst_file.unlink()
                    atomic_copy_file(src_file, dst_file)
                    installed_files.append(dst_file)
                    _journal()
                    continue

                if is_payload_root and low_name == "optiscaler.ini":
                    saw_ini = True
                    ini_raw = src_file.read_text(encoding="utf-8", errors="replace")
                    dst_ini = _write_patched_ini(
                        exe_dir,
                        ini_raw,
                        backend=backend,
                        fg_mode=fg_mode,
                        spoof_mode=spoof_mode,
                        overlay_menu=overlay_menu,
                        managed_paths=managed_paths,
                        backed_up_files=backed_up_files,
                    )
                    installed_files.append(dst_ini)
                    _journal()
                    continue

                cur = exe_dir
                for part in rel_dir.parts:
                    cur = cur / part
                    if cur.is_symlink():
                        raise RuntimeError(f"Unsafe symlink in destination path: {cur}")
                target_dir = (exe_dir / rel_dir).resolve()
                if not target_dir.is_relative_to(exe_dir):
                    raise RuntimeError(f"Destination directory escapes game folder: {target_dir}")
                dst_file = exe_dir / rel_dir / fname
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                _backup_if_needed(dst_file, managed_paths, backed_up_files)
                _journal()
                if dst_file.is_symlink():
                    dst_file.unlink()
                atomic_copy_file(src_file, dst_file)
                installed_files.append(dst_file)
                _journal()

        if not saw_ini:
            dst_ini = _write_patched_ini(
                exe_dir,
                "",
                backend=backend,
                fg_mode=fg_mode,
                spoof_mode=spoof_mode,
                overlay_menu=overlay_menu,
                managed_paths=managed_paths,
                backed_up_files=backed_up_files,
            )
            installed_files.append(dst_ini)
            _journal()

    except BaseException:
        # Leave the game as it was: drop what was copied and put the originals back.
        try:
            uninstall_optiscaler_for_game(exe_path)
        except Exception:
            logging.exception("OptiScaler rollback failed for %s; its journal is kept", exe_dir)
        raise

    state = _state(True)
    _write_state_manifest(state)
    _record_in_game_manifest(exe_dir, state)
    return state


def uninstall_optiscaler_for_game(exe_path: Path) -> OptiScalerResult:
    """Remove OptiScaler files installed for exe_path and restore any backed-up DLLs."""
    state = inspect_optiscaler_state(exe_path)
    if state is None:
        return OptiScalerResult(state=None, removed_count=0, restored_count=0)

    removed_count = 0
    for installed in state.installed_files:
        if installed.is_file() or installed.is_symlink():
            installed.unlink(missing_ok=True)
            removed_count += 1
        prune_empty_parents(installed, [state.exe_dir])  # e.g. D3D12_Optiscaler/, Licenses/

    restored_count = 0
    for orig in state.backed_up_files:
        backup = orig.with_name(f"{orig.name}.deckinstall.bak")
        if backup.is_file() and not backup.is_symlink() and not orig.is_symlink():
            orig.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, orig)
            restored_count += 1

    if not state.complete:
        restored_count += _restore_untracked_backups(state.exe_dir)

    manifest_path = state.exe_dir / OPTISCALER_MANIFEST
    manifest_path.unlink(missing_ok=True)
    _record_in_game_manifest(state.exe_dir, None)

    return OptiScalerResult(
        state=state,
        removed_count=removed_count,
        restored_count=restored_count,
    )


def _restore_untracked_backups(exe_dir: Path, max_depth: int = 4) -> int:
    """Put back `<name>.deckinstall.bak` files whose original is missing (crash between move and journal)."""
    restored = 0
    base = len(exe_dir.parts)
    for dirpath, dirnames, filenames in os.walk(exe_dir, followlinks=False):
        cur = Path(dirpath)
        if len(cur.parts) - base >= max_depth:
            dirnames[:] = []
        for fname in filenames:
            if not fname.endswith(".deckinstall.bak"):
                continue
            backup = cur / fname
            orig = cur / fname[: -len(".deckinstall.bak")]
            if backup.is_symlink() or orig.exists() or orig.is_symlink():
                continue
            os.replace(backup, orig)
            restored += 1
    return restored


def _record_in_game_manifest(exe_dir: Path, state: OptiScalerState | None) -> None:
    """Mirror the OptiScaler install (or its removal) into the DeckInstall game manifest, if any."""
    try:
        from .manifest import find_manifest
        from .standalone import _load_manifest, _write_manifest

        game = None
        for folder in (exe_dir, *exe_dir.parents):
            if folder == Path.home() or folder == Path(folder.anchor):
                break
            found = find_manifest(folder)
            if found is not None:
                game = _load_manifest(found)
                break
        if game is None or game.schema < 2:
            return

        def rel(path: Path) -> str:
            try:
                return str(path.relative_to(game.game_dir))
            except ValueError:
                return str(path)

        if state is None:
            if not game.meta.get("optiscaler"):
                return
            game.meta["optiscaler"] = {}
        else:
            game.meta["optiscaler"] = {
                "manifest": rel(state.exe_dir / OPTISCALER_MANIFEST),
                "proxy_dll": state.proxy_dll,
                "backend": state.backend,
                "version": state.version,
                "override": state.added_override,
                "files": [rel(p) for p in state.installed_files],
                "backups": [rel(p) for p in state.backed_up_files],
            }
        _write_manifest(game)
    except Exception:
        logging.exception("Could not record OptiScaler in the game manifest for %s", exe_dir)


@dataclass(frozen=True)
class BulkOutcome:
    """Result for one game in a bulk OptiScaler run."""

    name: str
    status: str  # "installed" | "removed" | "skipped" | "conflict" | "failed"
    detail: str = ""


@dataclass(frozen=True)
class BulkTarget:
    """One game for a bulk OptiScaler run, whatever its origin.

    source: "deckinstall" (StandaloneGame), "shortcut" (Steam non-Steam shortcut) or "steam" (store game).
    ref is the original object; winedll is its current WINEDLLOVERRIDES value.
    """

    name: str
    exe_path: Path
    game_dir: Path
    prefix_dir: Path | None
    winedll: str
    source: str
    ref: Any = None
    native_linux: bool = False
    steam_shortcut: Any = None


def bulk_optiscaler_targets(
    targets: list[BulkTarget],
    *,
    remove: bool = False,
    backend: str = "auto",
    fg_mode: str = "auto",
    spoof_mode: str = "auto",
    overlay_menu: bool = True,
    progress_cb: ProgressCb | None = None,
) -> list[tuple[BulkOutcome, str | None]]:
    """Install (or remove) OptiScaler files on several games; return each outcome and its new DLL overrides.

    Only game files are touched here: the caller writes the returned WINEDLLOVERRIDES (None = unchanged)
    to wherever the game keeps its launch options. Proxy slots taken by an unmanaged DLL are reported
    as "conflict" and left untouched (nothing is overwritten in bulk).
    """
    from .steam_vdf import merge_winedlloverride, remove_winedlloverride

    out: list[tuple[BulkOutcome, str | None]] = []
    for index, t in enumerate(targets):
        if progress_cb is not None:
            progress_cb(t.name, int(index * 100 / max(1, len(targets))))
        try:
            if remove:
                result = uninstall_optiscaler_for_game(t.exe_path)
                if result.state is None:
                    out.append((BulkOutcome(t.name, "skipped", "not_installed"), None))
                    continue
                dll = remove_winedlloverride(t.winedll, result.state.proxy_dll)
                out.append((BulkOutcome(t.name, "removed"), dll))
                continue

            compat = check_optiscaler_compatibility(t.exe_path, t.game_dir)
            if not compat.compatible or t.native_linux:
                out.append((BulkOutcome(t.name, "skipped", compat.pe_arch), None))
                continue
            if "all_taken" in compat.notes:
                out.append((BulkOutcome(t.name, "conflict", "all_taken"), None))
                continue
            prev = compat.installed_state
            state = install_optiscaler_for_game(
                t.exe_path,
                proxy_dll=compat.recommended_proxy,
                backend=backend,
                fg_mode=fg_mode,
                spoof_mode=spoof_mode,
                overlay_menu=overlay_menu,
                prefix_dir=t.prefix_dir,
                game_name=t.name,
            )
            dll = t.winedll
            if prev is not None and prev.proxy_dll != state.proxy_dll:
                dll = remove_winedlloverride(dll, prev.proxy_dll)
            dll = merge_winedlloverride(dll, state.proxy_dll, "n,b")
            out.append((BulkOutcome(t.name, "installed", f"{state.proxy_dll}.dll"), dll))
        except OptiScalerConflictError as exc:
            out.append((BulkOutcome(t.name, "conflict", exc.existing_path.name), None))
        except Exception as exc:
            logging.exception("Bulk OptiScaler failed for %s", t.name)
            out.append((BulkOutcome(t.name, "failed", str(exc)), None))
    return out


def standalone_bulk_target(game: Any) -> BulkTarget:
    """Wrap a DeckInstall StandaloneGame as a BulkTarget."""
    return BulkTarget(
        name=game.name,
        exe_path=game.exe_path,
        game_dir=game.game_dir,
        prefix_dir=game.prefix_dir,
        winedll=game.winedlloverrides,
        source="deckinstall",
        ref=game,
        native_linux=bool(game.native_linux),
    )

