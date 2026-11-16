"""Read-only scanner for games installed via Lutris, Heroic, and Bottles."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExternalLauncherGame:
    """Discovered game from an external Linux game launcher."""

    source: str  # "lutris", "heroic", "bottles"
    name: str
    exe_path: Path
    game_dir: Path
    prefix_dir: Path | None
    runner: str  # "wine" or "linux"
    launch_cmd: list[str] = field(default_factory=list)


def _strip_yaml_comment(line: str) -> str:
    """Remove inline YAML comments outside single/double quotes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _unquote_yaml_scalar(value: str) -> str:
    """Strip surrounding matching quotes and normalize null-like YAML scalars."""
    val = value.strip()
    if len(val) >= 2 and (
        (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
    ):
        return val[1:-1]
    if val.lower() in ("null", "~"):
        return ""
    return val


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse indentation-based YAML key-value mappings without PyYAML."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in text.splitlines():
        cleaned = _strip_yaml_comment(raw_line).rstrip()
        if not cleaned.strip():
            continue
        stripped = cleaned.lstrip(" ")
        if stripped.startswith("- "):
            # Skip YAML list items gracefully without breaking parent mapping.
            continue
        if ":" not in stripped:
            continue

        indent = len(cleaned) - len(stripped)
        key, _, rest = stripped.partition(":")
        key = _unquote_yaml_scalar(key)
        if not key:
            continue
        val_str = rest.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if not val_str:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _unquote_yaml_scalar(val_str)

    return root


def _read_json_safe(path: Path) -> Any:
    """Read and decode a JSON file, returning None on any I/O or parse error."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_runner(raw_runner: str, exe_path: Path) -> str:
    """Map launcher runner string to 'wine' or 'linux'."""
    low = raw_runner.strip().lower()
    if low == "linux":
        return "linux"
    if low in ("wine", "proton", "umu", "wine-ge", "ge-proton"):
        return "wine"
    return "wine" if exe_path.suffix.lower() == ".exe" else "linux"


def _resolve_existing_dir(raw_path: str | Path | None) -> Path | None:
    """Return Path if raw_path points to an existing directory, else None."""
    if not raw_path:
        return None
    p = Path(str(raw_path)).expanduser()
    try:
        return p if p.is_dir() else None
    except OSError:
        return None


def _resolve_existing_file(raw_path: str | Path | None, base_dir: Path | None = None) -> Path | None:
    """Return Path if raw_path (optionally relative to base_dir) is an existing file."""
    if not raw_path:
        return None
    raw_str = str(raw_path).strip()
    if not raw_str:
        return None
    cand = Path(raw_str).expanduser()
    if not cand.is_absolute() and base_dir is not None:
        cand = base_dir / cand
    try:
        return cand if cand.is_file() else None
    except OSError:
        return None


def _lutris_entry_from_yaml(
    yml_path: Path,
    *,
    is_flatpak: bool = False,
) -> ExternalLauncherGame | None:
    """Parse a single Lutris game .yml file into ExternalLauncherGame."""
    try:
        data = _parse_simple_yaml(yml_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None

    game_section = data.get("game")
    game_dict = game_section if isinstance(game_section, dict) else {}

    exe_raw = game_dict.get("exe") or data.get("exe")
    working_dir_raw = game_dict.get("working_dir") or data.get("working_dir")
    base_dir = _resolve_existing_dir(working_dir_raw)
    exe_path = _resolve_existing_file(exe_raw, base_dir=base_dir)
    if exe_path is None:
        return None

    prefix_raw = game_dict.get("prefix") or data.get("prefix")
    prefix_dir = _resolve_existing_dir(prefix_raw)
    game_dir = base_dir or exe_path.parent
    name = str(data.get("name") or "").strip() or yml_path.stem
    slug = str(data.get("slug") or "").strip() or yml_path.stem
    runner = _normalize_runner(str(data.get("runner") or ""), exe_path)

    if is_flatpak:
        launch_cmd = ["flatpak", "run", "net.lutris.Lutris", f"lutris:rungame/{slug}"]
    else:
        launch_cmd = ["lutris", f"lutris:rungame/{slug}"]

    return ExternalLauncherGame(
        source="lutris",
        name=name,
        exe_path=exe_path,
        game_dir=game_dir,
        prefix_dir=prefix_dir,
        runner=runner,
        launch_cmd=launch_cmd,
    )


def _scan_lutris(home: Path) -> list[ExternalLauncherGame]:
    """Scan Lutris game YAML configs and pga.db in read-only mode."""
    results: list[ExternalLauncherGame] = []
    seen_exes: set[Path] = set()

    def _add(item: ExternalLauncherGame | None) -> None:
        if item is None:
            return
        try:
            resolved = item.exe_path.resolve()
        except OSError:
            resolved = item.exe_path
        if resolved in seen_exes:
            return
        seen_exes.add(resolved)
        results.append(item)

    config_roots = [
        (home / ".config" / "lutris" / "games", False),
        (home / ".var" / "app" / "net.lutris.Lutris" / "config" / "lutris" / "games", True),
    ]
    for games_dir, is_flatpak in config_roots:
        if not games_dir.is_dir():
            continue
        try:
            yml_files = sorted(games_dir.glob("*.yml"))
        except OSError:
            continue
        for yml_path in yml_files:
            if yml_path.is_file():
                _add(_lutris_entry_from_yaml(yml_path, is_flatpak=is_flatpak))

    db_roots = [
        (home / ".local" / "share" / "lutris" / "pga.db", home / ".config" / "lutris" / "games", False),
        (
            home / ".var" / "app" / "net.lutris.Lutris" / "data" / "lutris" / "pga.db",
            home / ".var" / "app" / "net.lutris.Lutris" / "config" / "lutris" / "games",
            True,
        ),
    ]
    for db_path, games_dir, is_flatpak in db_roots:
        if not db_path.is_file():
            continue
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cols_rows = conn.execute("PRAGMA table_info(games)").fetchall()
            cols = {str(r["name"]) for r in cols_rows}
            if not cols:
                continue
            select_cols = [
                c
                for c in ("name", "slug", "runner", "directory", "configpath", "installed", "exe", "prefix")
                if c in cols
            ]
            if not select_cols:
                continue
            query = f"SELECT {', '.join(select_cols)} FROM games"
            if "installed" in cols:
                query += " WHERE installed = 1"
            for row in conn.execute(query):
                row_dict = dict(row)
                cfg = str(row_dict.get("configpath") or "").strip()
                if cfg:
                    cfg_path = Path(cfg)
                    if not cfg_path.is_absolute():
                        cfg_path = games_dir / (cfg if cfg.endswith(".yml") else f"{cfg}.yml")
                    if cfg_path.is_file():
                        _add(_lutris_entry_from_yaml(cfg_path, is_flatpak=is_flatpak))
                        continue

                dir_path = _resolve_existing_dir(row_dict.get("directory"))
                exe_path = _resolve_existing_file(row_dict.get("exe"), base_dir=dir_path)
                if exe_path is None:
                    continue
                name = str(row_dict.get("name") or "").strip() or exe_path.stem
                slug = str(row_dict.get("slug") or "").strip()
                prefix_dir = _resolve_existing_dir(row_dict.get("prefix"))
                runner = _normalize_runner(str(row_dict.get("runner") or ""), exe_path)
                if slug:
                    launch_cmd = (
                        ["flatpak", "run", "net.lutris.Lutris", f"lutris:rungame/{slug}"]
                        if is_flatpak
                        else ["lutris", f"lutris:rungame/{slug}"]
                    )
                else:
                    launch_cmd = []
                _add(
                    ExternalLauncherGame(
                        source="lutris",
                        name=name,
                        exe_path=exe_path,
                        game_dir=dir_path or exe_path.parent,
                        prefix_dir=prefix_dir,
                        runner=runner,
                        launch_cmd=launch_cmd,
                    )
                )
        except (sqlite3.Error, OSError):
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    return results


def _heroic_game_config(config_root: Path, app_name: str) -> dict[str, Any]:
    """Read Heroic GamesConfig/<app_name>.json if available."""
    if not app_name:
        return {}
    raw = _read_json_safe(config_root / "GamesConfig" / f"{app_name}.json")
    if not isinstance(raw, dict):
        return {}
    nested = raw.get(app_name)
    return nested if isinstance(nested, dict) else raw


def _heroic_build_game(
    config_root: Path,
    *,
    is_flatpak: bool,
    app_name: str,
    title: str,
    install_path: str,
    executable: str,
    platform: str,
) -> ExternalLauncherGame | None:
    """Construct ExternalLauncherGame from a Heroic installed entry + GamesConfig."""
    game_cfg = _heroic_game_config(config_root, app_name)
    exe_candidate = str(game_cfg.get("targetExe") or executable or "").strip()
    base_dir = _resolve_existing_dir(install_path)
    exe_path = _resolve_existing_file(exe_candidate, base_dir=base_dir)
    if exe_path is None:
        return None

    prefix_dir = _resolve_existing_dir(game_cfg.get("winePrefix"))
    runner = _normalize_runner(platform, exe_path)
    name = title.strip() or app_name or exe_path.stem
    if app_name:
        launch_cmd = (
            ["flatpak", "run", "com.heroicgameslauncher.hgl", f"heroic://launch/{app_name}"]
            if is_flatpak
            else ["heroic", f"heroic://launch/{app_name}"]
        )
    else:
        launch_cmd = []

    return ExternalLauncherGame(
        source="heroic",
        name=name,
        exe_path=exe_path,
        game_dir=base_dir or exe_path.parent,
        prefix_dir=prefix_dir,
        runner=runner,
        launch_cmd=launch_cmd,
    )


def _scan_heroic(home: Path) -> list[ExternalLauncherGame]:
    """Scan native and Flatpak Heroic Games Launcher installs."""
    results: list[ExternalLauncherGame] = []
    seen_exes: set[Path] = set()

    def _add(item: ExternalLauncherGame | None) -> None:
        if item is None:
            return
        try:
            resolved = item.exe_path.resolve()
        except OSError:
            resolved = item.exe_path
        if resolved in seen_exes:
            return
        seen_exes.add(resolved)
        results.append(item)

    roots = [
        (home / ".config" / "heroic", False),
        (home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic", True),
    ]
    for config_root, is_flatpak in roots:
        if not config_root.is_dir():
            continue

        # 1. Epic / Legendary
        leg_raw = _read_json_safe(config_root / "legendaryConfig" / "legendary" / "installed.json")
        if isinstance(leg_raw, dict):
            for key, entry in leg_raw.items():
                if not isinstance(entry, dict):
                    continue
                app_name = str(entry.get("app_name") or key or "")
                _add(
                    _heroic_build_game(
                        config_root,
                        is_flatpak=is_flatpak,
                        app_name=app_name,
                        title=str(entry.get("title") or ""),
                        install_path=str(entry.get("install_path") or ""),
                        executable=str(entry.get("executable") or ""),
                        platform=str(entry.get("platform") or ""),
                    )
                )

        # 2. GOG
        gog_raw = _read_json_safe(config_root / "gog_store" / "installed.json")
        gog_entries: list[Any] = []
        if isinstance(gog_raw, dict) and isinstance(gog_raw.get("installed"), list):
            gog_entries = gog_raw["installed"]
        elif isinstance(gog_raw, list):
            gog_entries = gog_raw
        for entry in gog_entries:
            if not isinstance(entry, dict):
                continue
            _add(
                _heroic_build_game(
                    config_root,
                    is_flatpak=is_flatpak,
                    app_name=str(entry.get("appName") or entry.get("app_name") or ""),
                    title=str(entry.get("title") or ""),
                    install_path=str(entry.get("install_path") or ""),
                    executable=str(entry.get("executable") or ""),
                    platform=str(entry.get("platform") or ""),
                )
            )

        # 3. Amazon / Nile
        nile_raw = _read_json_safe(config_root / "nile_config" / "nile" / "installed.json")
        nile_entries: list[Any] = []
        if isinstance(nile_raw, list):
            nile_entries = nile_raw
        elif isinstance(nile_raw, dict):
            nile_entries = list(nile_raw.values())
        for entry in nile_entries:
            if not isinstance(entry, dict):
                continue
            _add(
                _heroic_build_game(
                    config_root,
                    is_flatpak=is_flatpak,
                    app_name=str(entry.get("id") or entry.get("app_name") or ""),
                    title=str(entry.get("title") or ""),
                    install_path=str(entry.get("path") or entry.get("install_path") or ""),
                    executable=str(entry.get("executable") or ""),
                    platform=str(entry.get("platform") or ""),
                )
            )

        # 4. Sideloaded apps
        side_raw = _read_json_safe(config_root / "sideload_apps" / "library.json")
        side_entries: list[Any] = []
        if isinstance(side_raw, dict) and isinstance(side_raw.get("games"), list):
            side_entries = side_raw["games"]
        elif isinstance(side_raw, list):
            side_entries = side_raw
        for entry in side_entries:
            if not isinstance(entry, dict):
                continue
            install_info = entry.get("install")
            install_dict = install_info if isinstance(install_info, dict) else {}
            _add(
                _heroic_build_game(
                    config_root,
                    is_flatpak=is_flatpak,
                    app_name=str(entry.get("app_name") or ""),
                    title=str(entry.get("title") or ""),
                    install_path=str(entry.get("folder_name") or install_dict.get("install_path") or ""),
                    executable=str(install_dict.get("executable") or entry.get("executable") or ""),
                    platform=str(install_dict.get("platform") or entry.get("platform") or ""),
                )
            )

    return results


def _resolve_bottles_exe(raw_path: str, bottle_dir: Path) -> Path | None:
    """Resolve a Bottles External_Programs path (POSIX or C:\\... inside drive_c)."""
    val = raw_path.strip()
    if not val:
        return None
    if len(val) >= 3 and val[1] == ":" and val[2] in ("\\", "/"):
        rel = val[3:].replace("\\", "/").lstrip("/")
        cand = bottle_dir / "drive_c" / rel
        return cand if cand.is_file() else None
    return _resolve_existing_file(val)


def _scan_bottles(home: Path) -> list[ExternalLauncherGame]:
    """Scan native and Flatpak Bottles bottle.yml External_Programs entries."""
    results: list[ExternalLauncherGame] = []
    seen_exes: set[Path] = set()

    roots = [
        (home / ".local" / "share" / "bottles" / "bottles", False),
        (home / ".var" / "app" / "com.usebottles.bottles" / "data" / "bottles" / "bottles", True),
    ]
    for bottles_root, is_flatpak in roots:
        if not bottles_root.is_dir():
            continue
        try:
            subdirs = sorted(bottles_root.iterdir())
        except OSError:
            continue
        for bottle_dir in subdirs:
            if not bottle_dir.is_dir() or bottle_dir.is_symlink():
                continue
            yml_path = bottle_dir / "bottle.yml"
            if not yml_path.is_file():
                continue
            try:
                data = _parse_simple_yaml(yml_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue

            bottle_name = str(data.get("Name") or "").strip() or bottle_dir.name
            ext_progs = data.get("External_Programs")
            if not isinstance(ext_progs, dict):
                continue
            for prog_entry in ext_progs.values():
                if not isinstance(prog_entry, dict):
                    continue
                exe_path = _resolve_bottles_exe(str(prog_entry.get("path") or ""), bottle_dir)
                if exe_path is None:
                    continue
                try:
                    resolved = exe_path.resolve()
                except OSError:
                    resolved = exe_path
                if resolved in seen_exes:
                    continue
                seen_exes.add(resolved)

                prog_name = str(prog_entry.get("name") or "").strip() or exe_path.stem
                game_dir = _resolve_existing_dir(prog_entry.get("folder")) or exe_path.parent
                if is_flatpak:
                    launch_cmd = [
                        "flatpak",
                        "run",
                        "--command=bottles-cli",
                        "com.usebottles.bottles",
                        "run",
                        "-b",
                        bottle_name,
                        "-p",
                        prog_name,
                    ]
                else:
                    launch_cmd = ["bottles-cli", "run", "-b", bottle_name, "-p", prog_name]

                results.append(
                    ExternalLauncherGame(
                        source="bottles",
                        name=prog_name,
                        exe_path=exe_path,
                        game_dir=game_dir,
                        prefix_dir=bottle_dir,
                        runner="wine",
                        launch_cmd=launch_cmd,
                    )
                )
    return results


def scan_external_launcher_games(home_dir: Path | None = None) -> list[ExternalLauncherGame]:
    """Read-only scan of Lutris, Heroic, and Bottles games with existing executables."""
    home = home_dir or Path.home()
    games: list[ExternalLauncherGame] = []
    games.extend(_scan_lutris(home))
    games.extend(_scan_heroic(home))
    games.extend(_scan_bottles(home))
    return games
