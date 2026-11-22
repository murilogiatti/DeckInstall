"""Maintenance cleanup: declarative rules that find leftovers safe to review for removal.

Each rule belongs to one group shown in the Maintenance page:
games (leftovers of DeckInstall/Steam games), runtimes (old Proton/runtimes),
launchers (prefixes left by other launchers) and system (caches/logs, unchecked by default).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path


@dataclass(frozen=True)
class CleanupCandidate:
    """An old external prefix, orphan Steam compatdata folder, or backup safe to review for removal."""

    category: str
    label: str
    path: Path
    size_bytes: int
    has_saves: bool
    saves_verified_migrated: bool = False
    matched_game: str = ""
    game_hint: str = ""
    group: str = ""


# Imported after CleanupCandidate: steam_vdf re-exports it and may import this module mid-load.
from .fsutil import file_sha256  # noqa: E402
from .manifest import find_manifest  # noqa: E402
from .steam_paths import SteamPaths, _read_compat_tool_id, dir_size_bytes  # noqa: E402
from .steam_vdf import (  # noqa: E402
    SNAPSHOT_KEEP_DEFAULT,
    ShortcutEntry,
    _active_compat_tools,
    _add_manifest_prefix,
    _all_user_shortcuts,
    _entry_steam_appid,
    _file_lock,
    _infer_prefix_game_name,
    _installed_steam_appids,
    _is_prefix_referenced_by_shortcuts,
    _iter_prefix_save_pairs,
    _iter_programdata_save_pairs,
    _norm_key,
    _prefix_has_unconventional_saves,
    _prefix_programdata_dir,
    _prefix_users_dir,
    _safe_parents_within,
    _steam_transaction_lock,
    external_prefix_roots,
)

GROUP_ORDER = ("games", "runtimes", "launchers", "system")
GROUP_DEFAULT_CHECKED = {"games": True, "runtimes": False, "launchers": True, "system": False}

_GPU_SHADER_CACHE_NAMES = frozenset(
    {"mesa_shader_cache", "mesa_shader_cache_db", "radv_builtin_shaders", "nvidia", "amd"}
)
_OLD_PROTON_PREFIXES = (
    "proton_7",
    "proton_8",
    "proton_9",
    "proton_10",
    "proton_experimental",
    "proton_hotfix",
    "UMU-Proton",
)
_PREFIX_SAFE_CACHE_RELPATHS = (
    "AppData/Local/Temp",
    "AppData/Local/NVIDIA/DXCache",
    "AppData/Local/AMD/DxCache",
    "AppData/Local/D3DSCache",
)
_APP_CACHE_MIN_BYTES = 5 * 1024 * 1024
_CORE_RUNTIME_APPIDS = frozenset({0, 228980, 1070560, 1391110, 1493710, 1628350, 1887720})

# A finder yields (candidate, min_size): min_size None = size already known,
# otherwise the directory is sized later and dropped if smaller than min_size.
_Found = tuple[CleanupCandidate, int | None]


@dataclass(frozen=True)
class CleanupRule:
    """One declarative cleanup rule: what it finds, its UI group, and when it runs."""

    id: str
    group: str
    finder: Callable[[_ScanContext], Iterator[_Found]]
    deep: bool = False  # only scanned with include_system_caches=True

    @property
    def default_checked(self) -> bool:
        return GROUP_DEFAULT_CHECKED[self.group]


def _plain(category: str, label: str, path: Path, size_bytes: int = 0) -> CleanupCandidate:
    return CleanupCandidate(category=category, label=label, path=path, size_bytes=size_bytes, has_saves=False)


# --- shared helpers ---------------------------------------------------------------------------


def collect_active_proton_tools(
    paths: SteamPaths | None,
    games_dir: Path | None = None,
) -> set[str]:
    """Return compatibility tool names in use by Steam games/shortcuts and standalone games."""
    from .standalone import get_legacy_games_dir, list_standalone_games

    tools: set[str] = set()
    if paths is not None:
        _, all_user_entries = _all_user_shortcuts(paths)
        active_shortcuts = {e.appid_u32 for e in all_user_entries}
        installed_steam = _installed_steam_appids(paths.steam_root)
        tools.update(_active_compat_tools(paths.config_vdf, active_shortcuts | installed_steam))
    if games_dir is None:
        games = list_standalone_games(None)
    else:
        try:
            is_legacy = games_dir.resolve() == get_legacy_games_dir().resolve()
        except OSError:
            is_legacy = games_dir == get_legacy_games_dir()
        if is_legacy:
            seen_dirs: set[Path] = set()
            games = []
            for game in [*list_standalone_games(games_dir), *list_standalone_games(None)]:
                if game.game_dir not in seen_dirs:
                    seen_dirs.add(game.game_dir)
                    games.append(game)
        else:
            games = list_standalone_games(games_dir)
    for game in games:
        if game.proton_name:
            tools.add(game.proton_name)
        prev = str(game.meta.get("proton_previous") or "").strip()
        if prev:
            tools.add(prev)
    return tools


def _tool_uses_sniper_or_old_proton(tool_name: str) -> bool:
    """Return True if a compatibility tool requires steamrt3/sniper or Proton <= 10."""
    if tool_name.startswith(_OLD_PROTON_PREFIXES):
        return True
    if tool_name.startswith("GE-Proton"):
        match = re.match(r"^GE-Proton(\d+)", tool_name)
        if match is None:
            return True
        return int(match.group(1)) < 11
    return False


def _newest_ge_proton11_paths(compat_tools: Path) -> set[Path]:
    """Return resolved paths for the newest installed GE-Proton11+ directory."""
    from .runtime_manager import _parse_ge_proton_version, list_ge_proton11_dirs

    preserved: set[Path] = set()
    global_ge11 = list_ge_proton11_dirs()
    if global_ge11:
        try:
            preserved.add(global_ge11[0].resolve())
        except OSError:
            pass
    if compat_tools.is_dir():
        found: list[tuple[tuple[int, ...], Path]] = []
        try:
            entries = list(compat_tools.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
            except OSError:
                continue
            ver = _parse_ge_proton_version(entry.name)
            if ver is not None:
                found.append((ver, entry))
        if found:
            found.sort(key=lambda item: item[0], reverse=True)
            try:
                preserved.add(found[0][1].resolve())
            except OSError:
                pass
    return preserved


def _path_under_norm_roots(raw_path: str, norm_roots: list[str]) -> bool:
    """Return True if raw_path lexically lies inside any unmounted library root."""
    if not raw_path or not norm_roots:
        return False
    norm = os.path.normpath(os.path.expanduser(raw_path))
    for root in norm_roots:
        if norm == root or norm.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _is_orphan_desktop_file(
    desktop_path: Path,
    absent_library_roots: Iterable[Path] = (),
) -> bool:
    """Return True if a deckinstall-*.desktop file points to a missing Exec= or Path= (unless on an absent library)."""
    try:
        text = desktop_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    exec_val = ""
    path_val = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Exec=") and not exec_val:
            exec_val = stripped[5:].strip()
        elif stripped.startswith("Path=") and not path_val:
            path_val = stripped[5:].strip()
    if not exec_val:
        return True
    try:
        tokens = shlex.split(exec_val)
    except ValueError:
        tokens = [exec_val.strip('"')]
    if not tokens:
        return True
    norm_roots = [os.path.normpath(str(r)) for r in absent_library_roots]
    if _path_under_norm_roots(tokens[0], norm_roots) or (
        path_val and _path_under_norm_roots(path_val, norm_roots)
    ):
        return False
    if not Path(tokens[0]).exists():
        return True
    if path_val and not Path(path_val).exists():
        return True
    return False


def _iter_prefix_internal_cache_dirs(prefix_root: Path) -> list[Path]:
    """Return existing non-symlink internal cache directories inside a Wine/Proton prefix."""
    users_dir = _prefix_users_dir(prefix_root)
    if users_dir is None:
        return []
    try:
        users_resolved = users_dir.resolve()
    except OSError:
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    try:
        user_entries = list(users_dir.iterdir())
    except OSError:
        return []
    for user_entry in user_entries:
        if user_entry.is_symlink() or not user_entry.is_dir():
            continue
        for rel in _PREFIX_SAFE_CACHE_RELPATHS:
            rel_parts = Path(rel).parts
            if not _safe_parents_within(user_entry, users_resolved, rel_parts):
                continue
            cache_dir = user_entry / rel
            if cache_dir.is_dir() and not cache_dir.is_symlink():
                try:
                    resolved = cache_dir.resolve()
                except OSError:
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(cache_dir)
    return found


# --- scan context: state shared by all rules during one scan -----------------------------------


class _ScanContext:
    """Shortcuts, protected prefixes and save-verification caches for one cleanup scan."""

    def __init__(
        self,
        paths: SteamPaths | None,
        extra_external_roots: list[Path] | None,
        home_dir: Path | None,
    ) -> None:
        from .standalone import get_legacy_games_dir, list_standalone_games

        self.paths = paths
        self.current_entries: list[ShortcutEntry] = []
        self.all_user_entries: list[ShortcutEntry] = []
        self.shortcuts_readable = True
        if paths is not None:
            try:
                self.current_entries, self.all_user_entries = _all_user_shortcuts(paths, strict=True)
            except (OSError, ValueError):
                self.shortcuts_readable = False
                self.current_entries, self.all_user_entries = _all_user_shortcuts(paths, strict=False)

        self.by_norm_key: dict[str, ShortcutEntry] = {}
        self.by_steam_appid: dict[str, ShortcutEntry] = {}
        self.by_appid_u32: dict[int, ShortcutEntry] = {}
        for entry in self.current_entries:
            self.by_appid_u32[entry.appid_u32] = entry
            nk = _norm_key(entry.app_name)
            if nk and nk not in self.by_norm_key:
                self.by_norm_key[nk] = entry
            sa = _entry_steam_appid(entry)
            if sa:
                self.by_steam_appid.setdefault(str(sa), entry)
                self.by_steam_appid.setdefault(f"umu-{sa}", entry)
        self.active_shortcuts = {e.appid_u32 for e in self.all_user_entries}
        self.installed_steam = _installed_steam_appids(paths.steam_root) if paths is not None else set()

        home = home_dir or Path.home()
        self._custom_home = home_dir is not None
        self.home = home
        self.data_home = (
            (home / ".local" / "share")
            if home_dir is not None
            else Path(os.environ.get("XDG_DATA_HOME", "").strip() or (home / ".local" / "share")).expanduser()
        )
        self.cache_home = (
            (home / ".cache")
            if home_dir is not None
            else Path(os.environ.get("XDG_CACHE_HOME", "").strip() or (home / ".cache")).expanduser()
        )
        self.games_dir = (home_dir / "Games") if home_dir is not None else get_legacy_games_dir()
        self.standalone_prefixes_dir = self.games_dir / ".prefixes"
        self.standalone_prefix_paths = self._protected_standalone_prefixes(list_standalone_games)
        self.active_external_prefixes = self._active_external_prefixes()
        if extra_external_roots is not None:
            self.prefix_roots = list(extra_external_roots)
        else:
            self.prefix_roots = list(external_prefix_roots(include_configured=False)) + [self.standalone_prefixes_dir]
        self.seen_prefixes: set[Path] = set()

        self._lock = threading.Lock()
        self._sha256_cache: dict[Path, tuple[int, int, str]] = {}
        self._save_index: dict[Path, dict[tuple[str, str], tuple[Path, int]] | None] = {}
        if paths is not None:
            for entry in self.current_entries:
                self._target_save_index(paths.compatdata_dir / str(entry.appid_u32))

    def _active_external_prefixes(self) -> set[Path]:
        """Collect resolved Wine prefixes currently used by Heroic, Lutris, Bottles or Hydra."""
        active: set[Path] = set()
        try:
            from .launcher_scan import scan_external_launcher_games

            for ext in scan_external_launcher_games(self.home):
                if ext.prefix_dir is not None:
                    try:
                        active.add(ext.prefix_dir.resolve())
                    except OSError:
                        pass
        except Exception:
            pass
        try:
            from .adopt import _scan_hydra

            for cand in _scan_hydra(self.home):
                if cand.prefix_dir is not None:
                    try:
                        active.add(cand.prefix_dir.resolve())
                    except OSError:
                        pass
        except Exception:
            pass
        return active

    def _protected_standalone_prefixes(self, list_standalone_games: Callable) -> set[Path]:
        protected: set[Path] = set()
        games_dir = self.games_dir
        # Protect the prefixes of every library game, not only the ones under games_dir.
        for game in [*list_standalone_games(games_dir), *list_standalone_games()]:
            try:
                protected.add(game.prefix_dir.resolve())
            except OSError:
                pass
        if games_dir.is_dir():
            try:
                for gdir in games_dir.iterdir():
                    if not gdir.is_dir() or gdir.name.startswith("."):
                        continue
                    manifest = find_manifest(gdir)
                    if manifest is not None:
                        _add_manifest_prefix(manifest, protected)
                        if self.standalone_prefixes_dir.is_dir():
                            try:
                                protected.add((self.standalone_prefixes_dir / gdir.name).resolve())
                            except OSError:
                                pass
            except OSError:
                pass
        apps_dir = self.data_home / "applications"
        if apps_dir.is_dir():
            try:
                for desktop_file in apps_dir.glob("deckinstall-*.desktop"):
                    if desktop_file.is_symlink() or not desktop_file.is_file():
                        continue
                    try:
                        for line in desktop_file.read_text(encoding="utf-8", errors="replace").splitlines():
                            if line.startswith("Path="):
                                gdir_path = Path(line.split("=", 1)[1].strip().strip('"')).expanduser()
                                manifest = find_manifest(gdir_path)
                                if manifest is not None:
                                    _add_manifest_prefix(manifest, protected)
                                break
                    except Exception:
                        pass
            except OSError:
                pass
        return protected

    @cached_property
    def active_tools(self) -> set[str]:
        tools = collect_active_proton_tools(
            self.paths,
            games_dir=self.games_dir if self._custom_home else None,
        )
        if self._custom_home:
            v2_games = self.home / "DeckInstall" / "Games"
            if v2_games.is_dir():
                tools.update(collect_active_proton_tools(None, games_dir=v2_games))
        return tools

    @cached_property
    def sniper_in_use(self) -> bool:
        if any(_tool_uses_sniper_or_old_proton(t) for t in self.active_tools):
            return True
        if self.active_shortcuts:
            return True
        return bool(self.installed_steam - _CORE_RUNTIME_APPIDS)

    def _cached_sha256(self, file_path: Path) -> str:
        st = file_path.stat(follow_symlinks=False)
        key = (st.st_size, st.st_mtime_ns)
        with self._lock:
            cached = self._sha256_cache.get(file_path)
            if cached is not None and cached[0] == key[0] and cached[1] == key[1]:
                return cached[2]
        digest = file_sha256(file_path)
        with self._lock:
            self._sha256_cache[file_path] = (key[0], key[1], digest)
        return digest

    def _target_save_index(self, dst_compatdata: Path) -> dict[tuple[str, str], tuple[Path, int]] | None:
        with self._lock:
            if dst_compatdata in self._save_index:
                return self._save_index[dst_compatdata]
        if not dst_compatdata.is_dir():
            with self._lock:
                self._save_index[dst_compatdata] = None
            return None
        dst_users = _prefix_users_dir(dst_compatdata)
        if dst_users is None:
            with self._lock:
                self._save_index[dst_compatdata] = None
            return None
        index: dict[tuple[str, str], tuple[Path, int]] = {}
        try:
            dst_progdata = _prefix_programdata_dir(dst_compatdata)
            if dst_progdata is not None:
                dummy_pd = dst_compatdata / "__idx_pd__"
                for fp, rel_dst in _iter_programdata_save_pairs(dst_progdata, dummy_pd, raise_on_error=True):
                    sz = fp.stat().st_size
                    if sz > 0:
                        index[("ProgramData", rel_dst.relative_to(dummy_pd).as_posix())] = (fp, sz)
            dummy_users = dst_compatdata / "__idx_users__"
            for fp, rel_dst in _iter_prefix_save_pairs(
                dst_users, dummy_users, preserve_user_dirs=True, raise_on_error=True
            ):
                sz = fp.stat().st_size
                if sz > 0:
                    index[("users", rel_dst.relative_to(dummy_users).as_posix())] = (fp, sz)
        except OSError:
            with self._lock:
                self._save_index[dst_compatdata] = None
            return None
        with self._lock:
            self._save_index[dst_compatdata] = index
        return index

    def collect_orphan_saves(self, src_prefix: Path) -> tuple[bool, list[tuple[str, str, Path, int]] | None]:
        """Return (has_saves, entries); entries None means saves exist but cannot be listed safely."""
        src_users = _prefix_users_dir(src_prefix)
        if src_users is None:
            return True, None
        entries: list[tuple[str, str, Path, int]] = []
        try:
            src_progdata = _prefix_programdata_dir(src_prefix)
            if src_progdata is not None:
                dummy_pd = src_prefix / "__unused_pd__"
                for src_file, dst_rel in _iter_programdata_save_pairs(src_progdata, dummy_pd, raise_on_error=True):
                    sz = src_file.stat().st_size
                    if sz > 0:
                        entries.append(("ProgramData", dst_rel.relative_to(dummy_pd).as_posix(), src_file, sz))
            dummy_users = src_prefix / "__unused_users__"
            for src_file, dst_rel in _iter_prefix_save_pairs(
                src_users, dummy_users, preserve_user_dirs=False, raise_on_error=True
            ):
                sz = src_file.stat().st_size
                if sz > 0:
                    entries.append(("users", dst_rel.relative_to(dummy_users).as_posix(), src_file, sz))
            unconventional = _prefix_has_unconventional_saves(src_prefix, src_users)
        except OSError:
            return True, None
        if unconventional:
            return True, None
        if not entries:
            return False, []
        return True, entries

    def saves_verified_in_target(
        self,
        src_prefix_resolved: Path,
        src_entries: list[tuple[str, str, Path, int]] | None,
        dst_compatdata: Path,
    ) -> bool:
        """Return True only if every save file has a byte-identical copy in the target compatdata."""
        if not src_entries:
            return False
        dst_index = self._target_save_index(dst_compatdata)
        if dst_index is None:
            return False
        matched_pairs: list[tuple[Path, Path]] = []
        try:
            for section, rel_posix, src_file, src_size in src_entries:
                hit = dst_index.get((section, rel_posix))
                if hit is None:
                    return False
                dst_file, dst_size = hit
                if dst_size == 0 or dst_size != src_size:
                    return False
                if dst_file.is_symlink() or not dst_file.is_file():
                    return False
                dst_resolved = dst_file.resolve()
                if os.path.samefile(src_file, dst_file) or src_file.resolve() == dst_resolved:
                    return False
                if dst_resolved.is_relative_to(src_prefix_resolved):
                    return False
                matched_pairs.append((src_file, dst_file))
            for src_file, dst_file in matched_pairs:
                if self._cached_sha256(src_file) != self._cached_sha256(dst_file):
                    return False
        except OSError:
            return False
        return True

    def is_standalone_root(self, root: Path) -> bool:
        if not self.standalone_prefixes_dir.is_dir():
            return False
        try:
            return root.resolve() == self.standalone_prefixes_dir.resolve()
        except OSError:
            return False


# --- rule finders ------------------------------------------------------------------------------


def _scan_prefix_roots(ctx: _ScanContext, standalone: bool) -> Iterator[_Found]:
    """Unreferenced Wine prefixes under launcher roots (standalone=False) or Games/.prefixes (True)."""
    for root in ctx.prefix_roots:
        if not root.is_dir():
            continue
        is_standalone_prefixes_root = ctx.is_standalone_root(root)
        if is_standalone_prefixes_root != standalone:
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            if _prefix_users_dir(child) is None and not is_standalone_prefixes_root:
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in ctx.seen_prefixes:
                continue
            ctx.seen_prefixes.add(resolved)
            if resolved in ctx.standalone_prefix_paths or resolved in ctx.active_external_prefixes:
                continue
            if is_standalone_prefixes_root and (
                find_manifest(ctx.games_dir / child.name) is not None or (ctx.games_dir / child.name).is_dir()
            ):
                continue
            if _is_prefix_referenced_by_shortcuts(resolved, ctx.all_user_entries):
                continue
            min_sz = 1 if (is_standalone_prefixes_root and _prefix_users_dir(child) is None) else 0
            matched_entry = ctx.by_steam_appid.get(child.name) or ctx.by_norm_key.get(_norm_key(child.name))
            has_saves, src_entries = ctx.collect_orphan_saves(child)
            saves_verified = False
            if has_saves and src_entries and matched_entry is not None and ctx.paths is not None:
                dst_compat = ctx.paths.compatdata_dir / str(matched_entry.appid_u32)
                saves_verified = ctx.saves_verified_in_target(resolved, src_entries, dst_compat)
            matched_name = matched_entry.app_name if matched_entry else ""
            yield (
                CleanupCandidate(
                    category="Prefixo Externo",
                    label=f"{root.parent.name}/{root.name}/{child.name}",
                    path=child,
                    size_bytes=0,
                    has_saves=has_saves,
                    saves_verified_migrated=saves_verified,
                    matched_game=matched_name,
                    game_hint=matched_name or _infer_prefix_game_name(child),
                ),
                min_sz,
            )


def _find_launcher_prefixes(ctx: _ScanContext) -> Iterator[_Found]:
    return _scan_prefix_roots(ctx, standalone=False)


def _find_library_prefixes(ctx: _ScanContext) -> Iterator[_Found]:
    return _scan_prefix_roots(ctx, standalone=True)


def _compatdata_children(ctx: _ScanContext) -> Iterator[Path]:
    if ctx.paths is None or not ctx.paths.compatdata_dir.is_dir():
        return
    for child in ctx.paths.compatdata_dir.iterdir():
        if not child.is_symlink() and child.is_dir():
            yield child


def _find_compatdata_backups(ctx: _ScanContext) -> Iterator[_Found]:
    """compatdata/<id>.deckinstall.bak folders left by migrations, unless a shortcut still uses them."""
    paths = ctx.paths
    if paths is None:
        return
    for child in _compatdata_children(ctx):
        if not child.name.endswith(".deckinstall.bak"):
            continue
        try:
            if _is_prefix_referenced_by_shortcuts(child.resolve(), ctx.all_user_entries):
                continue
        except OSError:
            continue
        prefix_id = child.name.split(".", 1)[0]
        matched_entry = (
            ctx.by_appid_u32.get(int(prefix_id)) if (prefix_id.isascii() and prefix_id.isdigit()) else None
        )
        has_saves, src_entries = ctx.collect_orphan_saves(child)
        saves_verified = False
        if has_saves and src_entries and matched_entry is not None:
            try:
                saves_verified = ctx.saves_verified_in_target(
                    child.resolve(), src_entries, paths.compatdata_dir / prefix_id
                )
            except OSError:
                saves_verified = False
        matched_name = matched_entry.app_name if matched_entry else ""
        yield (
            CleanupCandidate(
                category="Backup",
                label=child.name,
                path=child,
                size_bytes=0,
                has_saves=has_saves,
                saves_verified_migrated=saves_verified,
                matched_game=matched_name,
                game_hint=matched_name or _infer_prefix_game_name(child),
            ),
            0,
        )


def _is_active_appid_dir(ctx: _ScanContext, child: Path) -> bool | None:
    """True/False for numeric compatdata/shadercache dirs (active or not), None for other names."""
    if child.name.endswith(".deckinstall.bak") or not (child.name.isascii() and child.name.isdigit()):
        return None
    appid = int(child.name)
    return appid in ctx.active_shortcuts or appid in ctx.installed_steam


def _find_prefix_caches(ctx: _ScanContext) -> Iterator[_Found]:
    """Temp/DXCache folders inside prefixes of games still in use."""
    for child in _compatdata_children(ctx):
        if not _is_active_appid_dir(ctx, child):
            continue
        matched_entry = ctx.by_appid_u32.get(int(child.name))
        matched_name = matched_entry.app_name if matched_entry else ""
        for cache_dir in _iter_prefix_internal_cache_dirs(child):
            yield (
                CleanupCandidate(
                    category="Prefix Cache",
                    label=f"compatdata/{child.name}/{cache_dir.name}",
                    path=cache_dir,
                    size_bytes=0,
                    has_saves=False,
                    matched_game=matched_name,
                    game_hint=matched_name,
                ),
                1,
            )


def _find_orphan_compatdata(ctx: _ScanContext) -> Iterator[_Found]:
    """compatdata/<appid> of games no longer installed nor referenced by any shortcut."""
    paths = ctx.paths
    if paths is None or not ctx.shortcuts_readable:
        return
    for child in _compatdata_children(ctx):
        if _is_active_appid_dir(ctx, child) is not False:
            continue
        try:
            child_resolved = child.resolve()
            if _is_prefix_referenced_by_shortcuts(child_resolved, ctx.all_user_entries):
                continue
        except OSError:
            continue
        has_saves, src_entries = ctx.collect_orphan_saves(child)
        saves_verified = False
        matched_game = ""
        if has_saves and src_entries:
            for entry in ctx.current_entries:
                dst_compat = paths.compatdata_dir / str(entry.appid_u32)
                if dst_compat.is_dir() and ctx.saves_verified_in_target(child_resolved, src_entries, dst_compat):
                    saves_verified = True
                    matched_game = entry.app_name
                    break
        yield (
            CleanupCandidate(
                category="Compatdata Órfão",
                label=f"compatdata/{child.name}",
                path=child,
                size_bytes=0,
                has_saves=has_saves,
                saves_verified_migrated=saves_verified,
                matched_game=matched_game,
                game_hint=matched_game or _infer_prefix_game_name(child),
            ),
            0,
        )


def _find_orphan_shadercache(ctx: _ScanContext) -> Iterator[_Found]:
    if ctx.paths is None or not ctx.shortcuts_readable:
        return
    shadercache_dir = ctx.paths.steam_root / "steamapps" / "shadercache"
    if not shadercache_dir.is_dir():
        return
    for child in shadercache_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if _is_active_appid_dir(ctx, child) is not False or int(child.name) == 0:
            continue
        yield _plain("Orphan Shadercache", f"shadercache/{child.name}", child), 1


def _find_orphan_game_caches(_ctx: _ScanContext) -> Iterator[_Found]:
    """DeckInstall shader caches (<library>/Caches/<id>) whose game is gone; absent disks keep theirs."""
    from .standalone import find_orphan_shader_caches

    for entry in find_orphan_shader_caches():
        yield _plain("Orphan Game Cache", f"Caches/{entry.name}", entry.path, entry.size_bytes), None


def _find_steam_logs(ctx: _ScanContext) -> Iterator[_Found]:
    if ctx.paths is None:
        return
    steam_logs_dir = ctx.paths.steam_root / "logs"
    if steam_logs_dir.is_dir() and not steam_logs_dir.is_symlink():
        yield _plain("Steam Logs", "Steam/logs", steam_logs_dir), 1024 * 1024


def _find_orphan_desktop_files(ctx: _ScanContext) -> Iterator[_Found]:
    apps_dir = ctx.data_home / "applications"
    if not apps_dir.is_dir() or apps_dir.is_symlink():
        return
    try:
        from .library import discover_libraries

        absent_roots = [lib.root for lib in discover_libraries() if not lib.available]
    except Exception:
        absent_roots = []
    for child in apps_dir.iterdir():
        if child.is_symlink() or not child.is_file():
            continue
        if not (child.name.startswith("deckinstall-") and child.name.endswith(".desktop")):
            continue
        if _is_orphan_desktop_file(child, absent_library_roots=absent_roots):
            try:
                sz = child.stat().st_size
            except OSError:
                continue
            yield _plain("Orphan Desktop", child.name, child, sz), None


def _find_old_save_backups(ctx: _ScanContext) -> Iterator[_Found]:
    """saves-*.zip backups beyond the newest SNAPSHOT_KEEP_DEFAULT in each backups folder."""
    for backups_dir in (ctx.home / "Games" / "saves" / "backups", ctx.home / "Backups" / "DeckInstall"):
        if not (backups_dir.is_dir() and not backups_dir.is_symlink()):
            continue
        save_zips: list[tuple[float, str, Path, int]] = []
        for child in backups_dir.iterdir():
            if child.is_symlink() or not child.is_file():
                continue
            if not (child.name.startswith("saves-") and child.name.endswith(".zip")):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            save_zips.append((st.st_mtime, child.name, child, st.st_size))
        save_zips.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _, _, old_zip, sz in save_zips[SNAPSHOT_KEEP_DEFAULT:]:
            yield _plain("Backup", old_zip.name, old_zip, sz), None


def _find_trash(ctx: _ScanContext) -> Iterator[_Found]:
    trash_files = ctx.data_home / "Trash" / "files"
    if trash_files.is_dir() and not trash_files.is_symlink():
        yield _plain("Trash", "Trash/files", trash_files), 1


def _find_user_cache(ctx: _ScanContext) -> Iterator[_Found]:
    """Entries of ~/.cache: GPU shader caches (any size) and other app caches >= 5 MiB."""
    user_cache = ctx.cache_home
    if not user_cache.is_dir() or user_cache.is_symlink():
        return
    for child in user_cache.iterdir():
        if child.is_symlink() or child.name in ("fontconfig", "dconf"):
            continue
        label = f".cache/{child.name}"
        is_gpu = child.name in _GPU_SHADER_CACHE_NAMES
        kind = "GPU Shader Cache" if is_gpu else "App Cache"
        if child.is_dir():
            yield _plain(kind, label, child), (1 if is_gpu else _APP_CACHE_MIN_BYTES)
            continue
        try:
            sz = child.stat().st_size
        except OSError:
            continue
        if (is_gpu and sz > 0) or (not is_gpu and sz >= _APP_CACHE_MIN_BYTES):
            yield _plain(kind, label, child, sz), None


def _find_nv_compute_cache(ctx: _ScanContext) -> Iterator[_Found]:
    nv_compute = ctx.home / ".nv" / "ComputeCache"
    if nv_compute.is_dir() and not nv_compute.is_symlink():
        yield _plain("GPU Shader Cache", ".nv/ComputeCache", nv_compute), 1


def _find_flatpak_caches(ctx: _ScanContext) -> Iterator[_Found]:
    var_app = ctx.home / ".var" / "app"
    if not var_app.is_dir() or var_app.is_symlink():
        return
    for app_dir in var_app.iterdir():
        if app_dir.is_symlink() or not app_dir.is_dir():
            continue
        cdir = app_dir / "cache"
        if cdir.is_dir() and not cdir.is_symlink():
            yield _plain("App Cache", f"Flatpak/{app_dir.name}/cache", cdir), _APP_CACHE_MIN_BYTES


def _find_old_compat_tools(ctx: _ScanContext) -> Iterator[_Found]:
    """Unused UMU-Proton-* and GE-Proton* (except the newest GE-Proton11+) in compatibilitytools.d."""
    active_tools = ctx.active_tools
    compat_tools = (
        ctx.paths.steam_root / "compatibilitytools.d"
        if ctx.paths is not None
        else ctx.data_home / "Steam" / "compatibilitytools.d"
    )
    compat_dirs: list[Path] = [compat_tools]
    raw_extra_compat = os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").strip()
    if raw_extra_compat:
        for part in raw_extra_compat.split(os.pathsep):
            cleaned = part.strip()
            if cleaned:
                compat_dirs.append(Path(cleaned).expanduser())

    seen_compat_dirs: set[Path] = set()
    for compat_dir in compat_dirs:
        if not compat_dir.is_dir() or not os.access(compat_dir, os.W_OK):
            continue
        try:
            resolved_compat = compat_dir.resolve()
        except OSError:
            resolved_compat = compat_dir
        if resolved_compat in seen_compat_dirs:
            continue
        seen_compat_dirs.add(resolved_compat)

        preserved_ge11 = _newest_ge_proton11_paths(compat_dir)
        for child in compat_dir.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            tool_id = _read_compat_tool_id(child / "compatibilitytool.vdf", child.name)
            if child.name in active_tools or tool_id in active_tools:
                continue
            is_old_umu = child.name.startswith("UMU-Proton-")
            is_old_ge = False
            if child.name.startswith("GE-Proton"):
                try:
                    is_old_ge = child.resolve() not in preserved_ge11
                except OSError:
                    is_old_ge = False
            if is_old_umu or is_old_ge:
                yield _plain("Old Proton/Runtime", f"{compat_dir.name}/{child.name}", child), 1


def _find_umu_steamrt3(ctx: _ScanContext) -> Iterator[_Found]:
    umu_dir = ctx.data_home / "umu"
    steamrt3 = umu_dir / "steamrt3"
    if (
        not ctx.sniper_in_use
        and (umu_dir / "steamrt4" / "toolmanifest.vdf").is_file()
        and steamrt3.is_dir()
        and not steamrt3.is_symlink()
    ):
        yield _plain("Old Proton/Runtime", "umu/steamrt3", steamrt3), 1


def _find_steam_proton10(ctx: _ScanContext) -> Iterator[_Found]:
    if ctx.paths is None:
        return
    common_dir = ctx.paths.steam_root / "steamapps" / "common"
    p10 = common_dir / "Proton 10.0"
    if (
        (common_dir / "Proton 11.0").is_dir()
        and p10.is_dir()
        and not any(t.startswith("proton_10") for t in ctx.active_tools)
        and not p10.is_symlink()
    ):
        yield _plain("Old Proton/Runtime", "common/Proton 10.0", p10), 1


def _find_steam_sniper(ctx: _ScanContext) -> Iterator[_Found]:
    if ctx.paths is None:
        return
    common_dir = ctx.paths.steam_root / "steamapps" / "common"
    sniper = common_dir / "SteamLinuxRuntime_sniper"
    if (
        not ctx.sniper_in_use
        and (common_dir / "SteamLinuxRuntime_4").is_dir()
        and sniper.is_dir()
        and not sniper.is_symlink()
    ):
        yield _plain("Old Proton/Runtime", "common/SteamLinuxRuntime_sniper", sniper), 1


RULES: tuple[CleanupRule, ...] = (
    CleanupRule("launcher_prefixes", "launchers", _find_launcher_prefixes),
    CleanupRule("library_prefixes", "games", _find_library_prefixes),
    CleanupRule("compatdata_backups", "games", _find_compatdata_backups),
    CleanupRule("prefix_caches", "system", _find_prefix_caches, deep=True),
    CleanupRule("orphan_compatdata", "games", _find_orphan_compatdata),
    CleanupRule("orphan_shadercache", "games", _find_orphan_shadercache),
    CleanupRule("orphan_game_caches", "games", _find_orphan_game_caches),
    CleanupRule("steam_logs", "system", _find_steam_logs),
    CleanupRule("orphan_desktop", "games", _find_orphan_desktop_files),
    CleanupRule("old_save_backups", "games", _find_old_save_backups),
    CleanupRule("trash", "system", _find_trash, deep=True),
    CleanupRule("user_cache", "system", _find_user_cache, deep=True),
    CleanupRule("nv_compute_cache", "system", _find_nv_compute_cache, deep=True),
    CleanupRule("flatpak_caches", "system", _find_flatpak_caches, deep=True),
    CleanupRule("old_compat_tools", "runtimes", _find_old_compat_tools, deep=True),
    CleanupRule("umu_steamrt3", "runtimes", _find_umu_steamrt3, deep=True),
    CleanupRule("steam_proton10", "runtimes", _find_steam_proton10, deep=True),
    CleanupRule("steam_sniper", "runtimes", _find_steam_sniper, deep=True),
)


# --- public API --------------------------------------------------------------------------------


def find_cleanup_candidates(
    paths: SteamPaths | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
) -> list[CleanupCandidate]:
    """Run every cleanup rule and return candidates sorted by size (largest first)."""
    ctx = _ScanContext(paths, extra_external_roots, home_dir)
    candidates: list[CleanupCandidate] = []
    pending_sized: list[tuple[CleanupCandidate, int]] = []
    for rule in RULES:
        if rule.deep and not include_system_caches:
            continue
        for cand, min_size in rule.finder(ctx):
            cand = replace(cand, group=rule.group)
            if min_size is None:
                candidates.append(cand)
            else:
                pending_sized.append((cand, min_size))

    if pending_sized:
        unique_dirs = list({cand.path for cand, _ in pending_sized})
        dir_sizes: dict[Path, int] = {}
        if len(unique_dirs) == 1:
            dir_sizes[unique_dirs[0]] = dir_size_bytes(unique_dirs[0])
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(unique_dirs))) as pool:
                for p, sz in zip(unique_dirs, pool.map(dir_size_bytes, unique_dirs), strict=True):
                    dir_sizes[p] = sz
        for cand, min_size in pending_sized:
            sz = dir_sizes.get(cand.path, 0)
            if sz >= min_size:
                candidates.append(replace(cand, size_bytes=sz))

    candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return candidates


def is_default_checked(cand: CleanupCandidate) -> bool:
    """Return True if a candidate is safe to pre-select for the 1-click clean."""
    if not GROUP_DEFAULT_CHECKED.get(cand.group, True):
        return False
    if cand.category in ("GPU Shader Cache", "Old Proton/Runtime"):
        return False
    if cand.category in ("Prefixo Externo", "external_prefix"):
        return bool(cand.matched_game) and ((not cand.has_saves) or cand.saves_verified_migrated)
    return (not cand.has_saves) or cand.saves_verified_migrated


def delete_cleanup_candidates_checked(
    paths: SteamPaths | None = None,
    candidates: list[CleanupCandidate] | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
    allow_unverified_saves: bool = False,
) -> tuple[int, int, list[str]]:
    """Revalidate each candidate immediately before deleting it and return (deleted, freed, errors)."""
    deleted = 0
    freed = 0
    errors: list[str] = []
    home = home_dir or Path.home()
    lock_ctx = (
        _steam_transaction_lock(paths.shortcuts_vdf)
        if paths is not None
        else _file_lock(home / ".local" / "share" / "DeckInstall" / "cleanup.lock")
    )
    with lock_ctx:
        fresh_by_path = {
            c.path.resolve(): c
            for c in find_cleanup_candidates(
                paths,
                extra_external_roots=extra_external_roots,
                include_system_caches=include_system_caches,
                home_dir=home_dir,
            )
        }
        for item in candidates or []:
            if not item.path.exists():
                continue
            try:
                resolved = item.path.resolve()
            except OSError as exc:
                errors.append(f"{item.label}: {exc}")
                continue
            fresh = fresh_by_path.get(resolved)
            if fresh is None:
                errors.append(f"{item.label}: skipped for safety (no longer orphan/safe).")
                continue
            if fresh.has_saves and not fresh.saves_verified_migrated and not allow_unverified_saves:
                errors.append(f"{item.label}: skipped for safety (unvalidated saves).")
                continue
            try:
                if item.path.is_dir() and not item.path.is_symlink():
                    shutil.rmtree(item.path)
                    if fresh.category == "Prefix Cache" and item.path.name.lower() == "temp":
                        item.path.mkdir(parents=True, exist_ok=True)
                else:
                    item.path.unlink()
                if item.label == "common/SteamLinuxRuntime_sniper" and paths is not None:
                    acf = paths.steam_root / "steamapps" / "appmanifest_1628350.acf"
                    if acf.is_file() and not acf.is_symlink():
                        acf.unlink()
                deleted += 1
                freed += item.size_bytes
            except OSError as exc:
                errors.append(f"{item.label}: {exc}")
    return deleted, freed, errors
