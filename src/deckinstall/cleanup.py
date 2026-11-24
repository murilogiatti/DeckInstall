"""Maintenance cleanup: declarative rules that find leftovers safe to review for removal.

Each rule belongs to one group shown in the Maintenance page:
games (leftovers of DeckInstall/Steam games), runtimes (old Proton/runtimes),
launchers (prefixes left by other game launchers) and game caches/logs.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .standalone import StandaloneGame


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
    review_only: bool = False


# Imported after CleanupCandidate: steam_vdf re-exports it and may import this module mid-load.
from .fsutil import desktop_exec_tokens, file_sha256  # noqa: E402
from .manifest import find_manifest  # noqa: E402
from .steam_paths import SteamPaths, _read_compat_tool_id, dir_size_bytes  # noqa: E402
from .steam_vdf import (  # noqa: E402
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
    get_compat_tool_map,
)

GROUP_ORDER = ("games", "runtimes", "launchers", "system")
GROUP_DEFAULT_CHECKED = {"games": True, "runtimes": False, "launchers": True, "system": False}

_PREFIX_SAFE_CACHE_RELPATHS = (
    "AppData/Local/Temp",
    "AppData/Local/NVIDIA/DXCache",
    "AppData/Local/AMD/DxCache",
    "AppData/Local/D3DSCache",
)

# A finder yields (candidate, min_size): min_size None = size already known,
# otherwise the directory is sized later and dropped if smaller than min_size.
_Found = tuple[CleanupCandidate, int | None]


@dataclass(frozen=True)
class CleanupRule:
    """One declarative cleanup rule: what it finds, its UI group, and when it runs."""

    id: str
    group: str
    finder: Callable[[_ScanContext], Iterator[_Found]]
    deep: bool = False  # include optional game caches and runtime maintenance


def _plain(category: str, label: str, path: Path, size_bytes: int = 0) -> CleanupCandidate:
    return CleanupCandidate(category=category, label=label, path=path, size_bytes=size_bytes, has_saves=False)


# --- shared helpers ---------------------------------------------------------------------------


def _standalone_proton_tools(games: Iterable[StandaloneGame]) -> set[str]:
    tools: set[str] = set()
    for game in games:
        if game.proton_name:
            tools.add(game.proton_name)
        prev = str(game.meta.get("proton_previous") or "").strip()
        if prev:
            tools.add(prev)
    return tools


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
    tokens = desktop_exec_tokens(exec_val)
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
        selected_paths: set[Path] | None,
    ) -> None:
        from .standalone import get_legacy_games_dir, list_standalone_games

        self.paths = paths
        self.selected_paths = selected_paths
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
        self.games_dir = (home_dir / "Games") if home_dir is not None else get_legacy_games_dir()
        self.standalone_prefixes_dir = self.games_dir / ".prefixes"
        self.standalone_games = list_standalone_games()
        self.custom_home_games = []
        if self._custom_home:
            self.custom_home_games = [
                *list_standalone_games(self.games_dir),
                *list_standalone_games(self.home / "DeckInstall" / "Games"),
            ]
        self.standalone_prefix_paths = self._protected_standalone_prefixes()
        self.active_external_prefixes = self._active_external_prefixes()
        if extra_external_roots is not None:
            self.prefix_roots = list(extra_external_roots)
        else:
            self.prefix_roots = list(external_prefix_roots(include_configured=False)) + [self.standalone_prefixes_dir]
        self.seen_prefixes: set[Path] = set()

        self._lock = threading.Lock()
        self._sha256_cache: dict[Path, tuple[int, int, str]] = {}
        self._save_index: dict[Path, dict[tuple[str, str], tuple[Path, int]] | None] = {}

    def _active_external_prefixes(self) -> set[Path]:
        """Collect resolved Wine prefixes currently used by Heroic, Lutris, Bottles or Hydra."""
        active: set[Path] = set()
        try:
            home_resolved = self.home.resolve()
        except OSError:
            home_resolved = self.home

        def _add_prefix(raw: object) -> None:
            if isinstance(raw, Path):
                text = str(raw)
            elif isinstance(raw, str):
                text = raw.strip()
            else:
                return
            if not text:
                return
            p = Path(text)
            if p == self.home or p == Path("/"):
                return
            try:
                resolved = p.resolve()
            except OSError:
                resolved = Path(os.path.abspath(p))
            if resolved != home_resolved and resolved != Path("/"):
                active.add(resolved)

        try:
            from .launcher_scan import (
                _HEROIC_CONFIGS,
                _expand_home,
                _lutris_db_rows,
                _lutris_games_dir,
                _parse_simple_yaml,
                _read_json_safe,
                bottles_roots,
                scan_external_launcher_games,
            )

            for ext in scan_external_launcher_games(self.home):
                if ext.prefix_dir is not None:
                    _add_prefix(ext.prefix_dir)

            # Empty bottles (before adding External_Programs) have bottle.yml and belong to Bottles.
            for bottles_root, _ in bottles_roots(self.home):
                if not bottles_root.is_dir():
                    continue
                try:
                    subdirs = list(bottles_root.iterdir())
                except OSError:
                    continue
                for bdir in subdirs:
                    if bdir.is_dir() and not bdir.is_symlink() and (bdir / "bottle.yml").is_file():
                        _add_prefix(bdir)

            # Heroic defaults and per-game configs (covers prefixes whose game lives on an unmounted disk).
            for rel in _HEROIC_CONFIGS:
                hroot = self.home / rel
                cfg = _read_json_safe(hroot / "config.json")
                defaults = cfg.get("defaultSettings") if isinstance(cfg, dict) else None
                if isinstance(defaults, dict):
                    for key in ("defaultWinePrefix", "winePrefix"):
                        val = defaults.get(key)
                        if isinstance(val, str) and val.strip():
                            _add_prefix(_expand_home(val, self.home))
                try:
                    game_cfgs = sorted((hroot / "GamesConfig").glob("*.json"))[:2000]
                except OSError:
                    game_cfgs = []
                for gcfg_path in game_cfgs:
                    raw = _read_json_safe(gcfg_path)
                    if not isinstance(raw, dict):
                        continue
                    for entry in [raw, *(v for v in raw.values() if isinstance(v, dict))]:
                        val = entry.get("winePrefix")
                        if isinstance(val, str) and val.strip():
                            _add_prefix(_expand_home(val, self.home))

            # Lutris SQLite and YAML configs (protects prefixes when game folders are unmounted).
            for db_path in (
                self.home / ".local" / "share" / "lutris" / "pga.db",
                self.home / ".var" / "app" / "net.lutris.Lutris" / "data" / "lutris" / "pga.db",
            ):
                for row in _lutris_db_rows(db_path):
                    val = row.get("prefix")
                    if isinstance(val, str) and val.strip():
                        _add_prefix(_expand_home(val, self.home))
            for games_dir in (
                _lutris_games_dir(self.home),
                self.home / ".var" / "app" / "net.lutris.Lutris" / "config" / "lutris" / "games",
                self.home / ".var" / "app" / "net.lutris.Lutris" / "data" / "lutris" / "games",
            ):
                try:
                    ymls = sorted(games_dir.glob("*.yml"))[:2000]
                except OSError:
                    continue
                for yml in ymls:
                    try:
                        data = _parse_simple_yaml(yml.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
                    game_sec = data.get("game")
                    raw_pfx = game_sec.get("prefix") if isinstance(game_sec, dict) else None
                    for val in (raw_pfx, data.get("prefix")):
                        if isinstance(val, str) and val.strip():
                            _add_prefix(_expand_home(val, self.home))
        except Exception:
            pass
        try:
            from .adopt import _scan_hydra

            for cand in _scan_hydra(self.home):
                if cand.prefix_dir is not None:
                    _add_prefix(cand.prefix_dir)
        except Exception:
            pass
        return active

    def _protected_standalone_prefixes(self) -> set[Path]:
        protected: set[Path] = set()
        games_dir = self.games_dir
        # Protect the prefixes of every library game, not only the ones under games_dir.
        for game in (*self.standalone_games, *self.custom_home_games):
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
        games = self.custom_home_games if self._custom_home else self.standalone_games
        tools = _standalone_proton_tools(games)
        if self.paths is not None:
            tools.update(_active_compat_tools(self.paths.config_vdf, self.active_shortcuts | self.installed_steam))
        return tools

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
            if any(
                p != resolved and p.is_relative_to(resolved)
                for p in (ctx.standalone_prefix_paths | ctx.active_external_prefixes)
            ):
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
    orphans = [child for child in _compatdata_children(ctx) if _is_active_appid_dir(ctx, child) is False]
    from .steam_appinfo import steam_app_names

    # Steam's app cache still knows the names of uninstalled store games.
    known_names = steam_app_names(paths.steam_root, (int(c.name) for c in orphans if c.name.isdigit()))
    for child in orphans:
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
                game_hint=matched_game or known_names.get(int(child.name), "") or _infer_prefix_game_name(child),
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


def _find_orphan_game_caches(ctx: _ScanContext) -> Iterator[_Found]:
    """DeckInstall shader caches (<library>/Caches/<id>) whose game is gone; absent disks keep theirs."""
    from .standalone import find_orphan_shader_caches

    if ctx._custom_home:
        found = find_orphan_shader_caches(ctx.games_dir)
    else:
        found = find_orphan_shader_caches()
    for entry in found:
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


def _find_old_compat_tools(ctx: _ScanContext) -> Iterator[_Found]:
    """Find superseded compatibility tools that have no current or rollback reference."""
    from .library import list_unavailable_games
    from .proton_policy import newest_ge_proton
    from .runtime_manager import superseded_managed_compat_tools
    from .steam_paths import find_steam_root

    steam_root = ctx.paths.steam_root if ctx.paths else find_steam_root()
    if list_unavailable_games() or (steam_root is not None and (ctx.paths is None or not ctx.shortcuts_readable)):
        return
    try:
        mappings = get_compat_tool_map(ctx.paths.config_vdf, strict=True) if ctx.paths else {}
    except (OSError, ValueError):
        return

    # Retain mappings for disconnected Steam libraries as well as the games
    # currently visible. AppID 0 describes Steam's default compatibility tool.
    protected = ctx.active_tools | set(mappings.values())
    current = newest_ge_proton()
    for child in superseded_managed_compat_tools():
        if current is not None and child.name == current.name:
            continue
        tool_id = _read_compat_tool_id(child / "compatibilitytool.vdf", child.name)
        if child.name not in protected and tool_id not in protected:
            yield _plain("Old Proton/Runtime", f"{child.parent.name}/{child.name}", child), 0


def _find_imported_save_backups(ctx: _ScanContext) -> Iterator[_Found]:
    """Find backup zips whose mapped restore destinations still hold all their contents."""
    from .save_sync import list_save_archives
    from .steam_vdf import DEFAULT_SAVES_DIR, save_backup_matches_installed_games

    if not ctx.shortcuts_readable:
        return
    archives = list_save_archives(
        saves_dir=ctx.home / "Games" / "saves" if ctx._custom_home else DEFAULT_SAVES_DIR,
        legacy_backups_dir=ctx.home / "Backups" / "DeckInstall",
    )
    for archive in archives:
        if archive.kind != "backup" or not _safe_scan_root(archive.path.parent):
            continue
        if ctx.selected_paths is not None and archive.path not in ctx.selected_paths:
            continue
        if save_backup_matches_installed_games(ctx.paths, archive.path):
            yield _plain("Imported Save Backup", archive.label, archive.path, archive.size_bytes), None


def _find_duplicate_save_snapshots(ctx: _ScanContext) -> Iterator[_Found]:
    from .ludusavi import find_import_duplicates

    root = ctx.home / "Games" / "saves" / "snapshots" if ctx._custom_home else None
    for archive in find_import_duplicates(root):
        try:
            yield _plain("Duplicate Save Snapshot", archive.name, archive, archive.stat().st_size), None
        except OSError:
            continue


def _find_runtime_leftovers(ctx: _ScanContext) -> Iterator[_Found]:
    from .runtime_manager import find_runtime_leftovers

    if not ctx._custom_home:
        for path in find_runtime_leftovers():
            yield _plain("Runtime Residue", path.name, path), 0


def _find_unused_umu_database(ctx: _ScanContext) -> Iterator[_Found]:
    """Offer the package's regenerable metadata after portable umu-run was removed."""
    from .runtime_manager import get_umu_database_path, get_umu_run_path

    if ctx._custom_home:
        return
    database = get_umu_database_path()
    if _safe_scan_root(database.parent) and not get_umu_run_path().exists():
        if database.is_file() and not database.is_symlink():
            yield _plain("Runtime Data", database.name, database, database.stat().st_size), None


def _find_library_leftovers(ctx: _ScanContext) -> Iterator[_Found]:
    """Inspect the managed library layout and protect every known game reference."""
    from . import library as lib
    from .standalone import game_logs_root, get_games_dir, shortcut_game_dir

    if not ctx.shortcuts_readable:
        return
    libraries = lib.discover_libraries() if not ctx._custom_home else []
    roots = {item.root for item in libraries if item.available}
    internal = ctx.home / lib.LIBRARY_DIRNAME if ctx._custom_home else lib.internal_library_root()
    roots.add(internal)
    games = ctx.custom_home_games if ctx._custom_home else ctx.standalone_games
    game_dirs = {game.game_dir.resolve() for game in games}
    other_paths = ctx.standalone_prefix_paths | ctx.active_external_prefixes
    live_ids = {item["id"] for item in lib.list_unavailable_games(libraries) if item["id"]}
    for entry in ctx.all_user_entries:
        try:
            game_dirs.add(shortcut_game_dir(entry).resolve())
        except (OSError, ValueError):
            pass
    for root in roots:
        data = lib.read_library_file(root)
        if data is None:
            continue
        for entry in data.get("games") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("id"):
                live_ids.add(str(entry["id"]))
            if entry.get("dir"):
                game_dirs.add((root / str(entry["dir"])).resolve())
    for game in games:
        if game.uid:
            live_ids.add(game.uid)
        if game.cache_dir is not None:
            live_ids.add(game.cache_dir.name)

    logs = ctx.data_home / lib.LIBRARY_DIRNAME / "logs" if ctx._custom_home else game_logs_root()
    game_roots = {r / lib.GAMES for r in roots if lib.read_library_file(r) is not None}
    if not ctx._custom_home and get_games_dir() != ctx.games_dir:
        game_roots.add(get_games_dir())
    for games_root in game_roots:
        if not _safe_scan_root(games_root):
            continue
        for folder in games_root.iterdir():
            if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
                continue
            resolved = folder.resolve()
            if any(path.is_relative_to(resolved) or resolved.is_relative_to(path) for path in game_dirs):
                continue
            if find_manifest(folder) is not None:
                continue
            if next(folder.iterdir(), None) is None:
                yield _plain("Empty Game Folder", folder.name, folder), None
            else:
                # May be an imported game, a repack or a manifest damaged in
                # another tool. Display it for inspection only.
                yield replace(_plain("Unregistered Game Folder", folder.name, folder), review_only=True), 0

    for root in roots:
        if lib.read_library_file(root) is None:
            continue
        prefixes = root / lib.PREFIXES
        if not _safe_scan_root(prefixes):
            continue
        for folder in prefixes.iterdir():
            if not _managed_leftover_name(folder, live_ids):
                continue
            resolved = folder.resolve()
            if any(path.is_relative_to(resolved) or resolved.is_relative_to(path) for path in other_paths):
                continue
            has_saves, _ = ctx.collect_orphan_saves(folder)
            item = _plain("Orphan Game Prefix", folder.name, folder)
            yield replace(item, has_saves=has_saves), 0

    if _safe_scan_root(logs):
        for folder in logs.iterdir():
            if _managed_leftover_name(folder, live_ids):
                yield _plain("Orphan Game Logs", folder.name, folder), 0
            elif folder.suffix == ".log" and not folder.is_symlink() and folder.is_file():
                item = _plain("Legacy Game Log", folder.name, folder, folder.stat().st_size)
                yield replace(item, review_only=True), None


def _safe_scan_root(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink() and path.absolute() == path.resolve()
    except (OSError, RuntimeError):
        return False


def _managed_leftover_name(folder: Path, live_ids: set[str]) -> bool:
    from uuid import UUID

    if folder.name in live_ids or folder.is_symlink() or not folder.is_dir():
        return False
    try:
        return str(UUID(folder.name)) == folder.name
    except ValueError:
        return False


RULES: tuple[CleanupRule, ...] = (
    CleanupRule("launcher_prefixes", "launchers", _find_launcher_prefixes),
    CleanupRule("imported_save_backups", "games", _find_imported_save_backups),
    CleanupRule("duplicate_save_snapshots", "games", _find_duplicate_save_snapshots),
    CleanupRule("library_leftovers", "games", _find_library_leftovers),
    CleanupRule("library_prefixes", "games", _find_library_prefixes),
    CleanupRule("compatdata_backups", "games", _find_compatdata_backups),
    CleanupRule("prefix_caches", "system", _find_prefix_caches, deep=True),
    CleanupRule("orphan_compatdata", "games", _find_orphan_compatdata),
    CleanupRule("orphan_shadercache", "games", _find_orphan_shadercache),
    CleanupRule("orphan_game_caches", "games", _find_orphan_game_caches),
    CleanupRule("steam_logs", "system", _find_steam_logs),
    CleanupRule("orphan_desktop", "games", _find_orphan_desktop_files),
    CleanupRule("old_compat_tools", "runtimes", _find_old_compat_tools, deep=True),
    CleanupRule("runtime_leftovers", "runtimes", _find_runtime_leftovers, deep=True),
    CleanupRule("unused_umu_database", "runtimes", _find_unused_umu_database, deep=True),
)


# --- public API --------------------------------------------------------------------------------


def find_cleanup_candidates(
    paths: SteamPaths | None = None,
    extra_external_roots: list[Path] | None = None,
    include_system_caches: bool = False,
    home_dir: Path | None = None,
    *,
    selected_paths: set[Path] | None = None,
) -> list[CleanupCandidate]:
    """Run every cleanup rule and return candidates sorted by size (largest first)."""
    ctx = _ScanContext(paths, extra_external_roots, home_dir, selected_paths)
    candidates: list[CleanupCandidate] = []
    pending_sized: list[tuple[CleanupCandidate, int]] = []
    for rule in RULES:
        if rule.deep and not include_system_caches:
            continue
        for cand, min_size in rule.finder(ctx):
            if selected_paths is not None and cand.path not in selected_paths:
                continue
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
    if cand.review_only or cand.category in ("Orphan Game Prefix", "Imported Save Backup", "Duplicate Save Snapshot"):
        return False
    if not GROUP_DEFAULT_CHECKED.get(cand.group, True):
        return False
    if cand.category == "Old Proton/Runtime":
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
    *,
    backed_up_paths: set[Path] | None = None,
) -> tuple[int, int, list[str]]:
    """Revalidate each candidate immediately before deleting it and return (deleted, freed, errors)."""
    if not candidates:
        return 0, 0, []
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
                selected_paths={item.path for item in candidates},
            )
        }
        for item in candidates:
            if not item.path.exists():
                continue
            try:
                resolved = item.path.resolve()
            except OSError as exc:
                errors.append(f"{item.label}: {exc}")
                continue
            fresh = fresh_by_path.get(resolved)
            if fresh is None or fresh.review_only or fresh.category != item.category:
                errors.append(f"{item.label}: skipped for safety (no longer orphan/safe).")
                continue
            can_remove_unverified = allow_unverified_saves and (backed_up_paths is None or resolved in backed_up_paths)
            if fresh.has_saves and not fresh.saves_verified_migrated and not can_remove_unverified:
                errors.append(f"{item.label}: skipped for safety (unvalidated saves).")
                continue
            try:
                if fresh.category == "Empty Game Folder":
                    item.path.rmdir()
                elif fresh.category == "Imported Save Backup":
                    from .steam_vdf import remove_imported_save_backup

                    if not remove_imported_save_backup(paths, item.path):
                        errors.append(f"{item.label}: backup or restore destinations changed.")
                        continue
                elif fresh.category == "Duplicate Save Snapshot":
                    from .ludusavi import remove_import_duplicate

                    if not remove_import_duplicate(item.path):
                        errors.append(f"{item.label}: duplicate snapshot changed.")
                        continue
                elif fresh.category == "Runtime Residue":
                    from .runtime_manager import remove_runtime_leftover

                    if not remove_runtime_leftover(item.path):
                        errors.append(f"{item.label}: runtime workspace changed.")
                        continue
                elif fresh.category == "Old Proton/Runtime":
                    from .runtime_manager import remove_compat_tool

                    remove_compat_tool(item.path, managed_only=True)
                elif item.path.is_dir() and not item.path.is_symlink():
                    shutil.rmtree(item.path)
                    if fresh.category == "Prefix Cache" and item.path.name.lower() == "temp":
                        item.path.mkdir(parents=True, exist_ok=True)
                else:
                    item.path.unlink()
                deleted += 1
                freed += fresh.size_bytes
            except (OSError, ValueError) as exc:
                errors.append(f"{item.label}: {exc}")
    return deleted, freed, errors
