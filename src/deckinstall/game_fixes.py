"""Per-game fixes other tools already keep, offered for any Proton (Valve's included).

- Valve: Proton's own list (`default_compat_config` in its `proton` script) is chosen by the
  game's Steam AppID, so a non-Steam shortcut never gets it. Its options can be given back with
  STEAM_COMPAT_CONFIG, which Valve's Proton and GE-Proton both read.
- umu/protonfixes: one small Python file per game, which umu and GE-Proton run by themselves.
  With Valve's Proton they do not run; most of them only set variables, arguments, DLLs or
  install winetricks packages, which DeckInstall can do with any Proton.

Both are read as source (Python's ast), never executed. Nothing here talks to the network except
`refresh_protonfixes`, which downloads the whole protonfixes archive (it says nothing about which
games are installed).
"""

from __future__ import annotations

import ast
import csv
import io
import logging
import re
import shlex
import shutil
import tarfile
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_text

PROTONFIXES_URL = "https://codeload.github.com/Open-Wine-Components/umu-protonfixes/tar.gz/refs/heads/master"
PROTONFIXES_MAX_AGE_S = 7 * 24 * 3600
_MAX_ARCHIVE_BYTES = 8 << 20
_ARCHIVE_MEMBER = re.compile(r"^[^/]+/(gamefixes-[a-z]+/[A-Za-z0-9_.-]+\.py|umu-database\.csv)$")
_OVERRIDE_ORDER = {"DISABLED": "", "NATIVE": "n", "BUILTIN": "b", "NATIVE_BUILTIN": "n,b", "BUILTIN_NATIVE": "b,n"}
_VERB = re.compile(r"^[A-Za-z0-9_.=-]+$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Fix:
    """One thing to do for a game.

    kind: "env" (KEY=VALUE), "arg" (game argument), "dll" (name=order), "winetricks" (a verb),
    "compat" (a Proton compat option, given through STEAM_COMPAT_CONFIG) or "advanced" (a step only
    umu/GE-Proton can do: shown, never applied). source: "valve" or "umu".
    """

    kind: str
    value: str
    source: str


# --- Valve: Proton's own per-game list -----------------------------------------------------------


def _appid_in(test: ast.expr, appid: str) -> bool:
    """`appid in ["123", ...]` (the shape every entry of the list has)."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.In)):
        return False
    if not (isinstance(test.left, ast.Name) and test.left.id == "appid"):
        return False
    seq = test.comparators[0]
    return isinstance(seq, (ast.List, ast.Tuple, ast.Set)) and any(
        isinstance(item, ast.Constant) and item.value == appid for item in seq.elts
    )


def valve_compat_options(proton_script: str, appid: str | int) -> list[str]:
    """The compat options Valve's `proton` script would turn on for this Steam AppID."""
    try:
        tree = ast.parse(proton_script)
    except SyntaxError:
        return []
    wanted = str(appid)
    options: list[str] = []
    for func in ast.walk(tree):
        if not (isinstance(func, ast.FunctionDef) and func.name == "default_compat_config"):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.If) and _appid_in(node.test, wanted):
                for stmt in node.body:
                    call = stmt.value if isinstance(stmt, ast.Expr) else None
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "add"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)
                    ):
                        options.append(call.args[0].value)
    return list(dict.fromkeys(options))


def valve_fixes(proton_dir: Path | None, appid: str | int | None) -> list[Fix]:
    """Valve's options for a game, read from the installed Proton (offline)."""
    if proton_dir is None or not appid:
        return []
    try:
        text = (proton_dir / "proton").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [Fix("compat", option, "valve") for option in valve_compat_options(text, appid)]


# --- umu / protonfixes ---------------------------------------------------------------------------


def protonfixes_cache_dir() -> Path:
    return Path.home() / ".cache" / "DeckInstall" / "protonfixes"


def local_protonfixes(candidates: Iterable[Path]) -> Path | None:
    """A protonfixes folder that is already on disk (inside an installed GE-Proton), newest first."""
    for proton_dir in candidates:
        folder = proton_dir / "protonfixes"
        if (folder / "gamefixes-steam").is_dir():
            return folder
    return None


def refresh_protonfixes(
    cache: Path | None = None,
    url: str = PROTONFIXES_URL,
    opener: Callable[..., object] = urllib.request.urlopen,
    now: float | None = None,
) -> Path | None:
    """Download the protonfixes archive when the cached copy is older than a week; return the
    folder (the old copy when the download fails, None when there is none)."""
    cache = cache or protonfixes_cache_dir()
    stamp = cache / ".fetched"
    try:
        fresh = (now or time.time()) - float(stamp.read_text().strip()) < PROTONFIXES_MAX_AGE_S
    except (OSError, ValueError):
        fresh = False
    if fresh and (cache / "gamefixes-steam").is_dir():
        return cache
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "DeckInstall"})
        with opener(request, timeout=20) as response:  # type: ignore[attr-defined]
            data = response.read(_MAX_ARCHIVE_BYTES + 1)
        if len(data) > _MAX_ARCHIVE_BYTES:
            raise ValueError("protonfixes archive too large")
        staging = cache.with_name(cache.name + ".new")
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                match = _ARCHIVE_MEMBER.match(member.name)
                if not match or not member.isfile():  # only plain files at known places
                    continue
                target = staging / match.group(1)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is not None:
                    target.write_bytes(source.read())
        if not (staging / "gamefixes-steam").is_dir():
            raise ValueError("unexpected protonfixes archive")
        atomic_write_text(staging / ".fetched", str(now or time.time()))
        shutil.rmtree(cache, ignore_errors=True)
        staging.rename(cache)
    except Exception as exc:  # offline, GitHub down, a changed layout: keep what we have
        logging.info("protonfixes not refreshed: %s", exc)
        shutil.rmtree(cache.with_name(cache.name + ".new"), ignore_errors=True)
    return cache if (cache / "gamefixes-steam").is_dir() else None


def umu_id_for(root: Path, store: str, codename: str) -> str:
    """umu's id for a non-Steam game (umu-database.csv: TITLE,STORE,CODENAME,UMU_ID,...)."""
    try:
        with (root / "umu-database.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) > 3 and row[1] == store and row[2] == codename:
                    return row[3]
    except OSError:
        pass
    return ""


def fix_file(root: Path, steam_appid: int | str | None = None, store: str = "", codename: str = "") -> Path | None:
    """The fix file of a game: by Steam AppID, else by store + code through umu's database."""
    if steam_appid:
        path = root / "gamefixes-steam" / f"{steam_appid}.py"
        return path if path.is_file() else None
    if store and codename:
        umu_id = umu_id_for(root, store, codename)
        if umu_id:
            for folder in (f"gamefixes-{store}", "gamefixes-umu"):
                path = root / folder / f"{umu_id}.py"
                if path.is_file():
                    return path
    return None


def _literal(node: ast.expr) -> object:
    if isinstance(node, ast.Attribute) and node.attr in _OVERRIDE_ORDER:  # util.OverrideOrder.NATIVE
        return _OVERRIDE_ORDER[node.attr]
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _exists_condition(test: ast.expr, game_dir: Path | None) -> bool | None:
    """`os.path.exists(install_dir + '/x')` (or isfile/isdir) checked in the game folder;
    None for any other condition."""
    if not (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)):
        return None
    if test.func.attr not in ("exists", "isfile", "isdir") or len(test.args) != 1 or game_dir is None:
        return None
    arg = test.args[0]
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add) and isinstance(arg.right, ast.Constant):
        rel = str(arg.right.value).lstrip("/")
        if ".." in Path(rel).parts:
            return None
        path = game_dir / rel
        checks = {"isdir": path.is_dir, "isfile": path.is_file, "exists": path.exists}
        return checks[test.func.attr]()
    return None


def _util_call(stmt: ast.stmt) -> tuple[str, list[object]] | None:
    call = stmt.value if isinstance(stmt, ast.Expr) else None
    if (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "util"
    ):
        return call.func.attr, [_literal(a) for a in call.args]
    return None


def _translate(stmts: list[ast.stmt], game_dir: Path | None, out: list[Fix]) -> None:
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            cond = _exists_condition(stmt.test, game_dir)
            if cond is None:
                out.append(Fix("advanced", "condition", "umu"))
            else:
                _translate(stmt.body if cond else stmt.orelse, game_dir, out)
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Pass)) or (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)  # a docstring
        ):
            continue  # e.g. install_dir = util.get_game_install_path()
        found = _util_call(stmt)
        if found is None:
            out.append(Fix("advanced", type(stmt).__name__.lower(), "umu"))
            continue
        name, args = found
        if any(a is None for a in args):
            out.append(Fix("advanced", name, "umu"))
        elif name == "protontricks" and isinstance(args[0], str) and _VERB.match(args[0]):
            out.append(Fix("winetricks", args[0], "umu"))
        elif name == "set_environment" and len(args) == 2 and _ENV_KEY.match(str(args[0])):
            out.append(Fix("env", f"{args[0]}={args[1]}", "umu"))
        elif name == "append_argument" and isinstance(args[0], str):
            out.append(Fix("arg", args[0], "umu"))
        elif name == "winedll_override" and len(args) == 2 and isinstance(args[0], str):
            out.append(Fix("dll", f"{args[0]}={args[1]}", "umu"))
        elif name == "disable_fsync":
            out.append(Fix("env", "PROTON_NO_FSYNC=1", "umu"))
        elif name == "disable_esync":
            out.append(Fix("env", "PROTON_NO_ESYNC=1", "umu"))
        elif name == "disable_nvapi":
            out.append(Fix("env", "PROTON_DISABLE_NVAPI=1", "umu"))
        elif name == "set_cpu_topology_limit" and isinstance(args[0], int):
            cores = args[0]
            out.append(Fix("env", f"PROTON_CPU_TOPOLOGY={cores}:{','.join(str(i) for i in range(cores))}", "umu"))
        else:
            out.append(Fix("advanced", name, "umu"))


def umu_fixes(path: Path | None, game_dir: Path | None = None) -> list[Fix]:
    """The steps of a protonfixes file (its main()) as Fix items; game_dir answers the
    "if this file exists in the game" conditions."""
    if path is None:
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    out: list[Fix] = []
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("main", "main_with_id")]
    for node in mains:
        _translate(node.body, game_dir, out)
    if not mains:  # the fix lives in shared code (another module): only umu/GE-Proton can run it
        out.append(Fix("advanced", "shared", "umu"))
    return list(dict.fromkeys(out))


# --- Applying them to the launch options ---------------------------------------------------------


def apply_fixes(opts: str, winedll: str, fixes: Iterable[Fix]) -> tuple[str, str]:
    """Add env, argument, DLL and compat fixes to the launch options (and WINEDLLOVERRIDES);
    winetricks and advanced items are not options and are left out."""
    from .steam_vdf import _format_launch_token, merge_winedlloverride

    try:
        tokens = shlex.split(opts) if opts.strip() else []
    except ValueError:
        tokens = opts.split()
    if "%command%" not in tokens:
        tokens.append("%command%")
    cmd = tokens.index("%command%")
    before, after = tokens[:cmd], tokens[cmd + 1:]
    env: dict[str, str] = {}
    for fix in fixes:
        if fix.kind == "env":
            key, _, value = fix.value.partition("=")
            env[key] = value
        elif fix.kind == "compat":
            env.setdefault("STEAM_COMPAT_CONFIG", "")
            env["STEAM_COMPAT_CONFIG"] = ",".join(filter(None, (env["STEAM_COMPAT_CONFIG"], fix.value)))
        elif fix.kind == "arg" and fix.value not in after:
            after.append(fix.value)
        elif fix.kind == "dll":
            name, _, order = fix.value.partition("=")
            winedll = merge_winedlloverride(winedll, name, order) if order else winedll
    if "STEAM_COMPAT_CONFIG" in env:  # merge with what is already there
        old = next((t.split("=", 1)[1] for t in before if t.startswith("STEAM_COMPAT_CONFIG=")), "")
        merged = [o for o in (old + "," + env["STEAM_COMPAT_CONFIG"]).split(",") if o]
        env["STEAM_COMPAT_CONFIG"] = ",".join(dict.fromkeys(merged))
    # Variables first, then wrappers such as gamemoderun (a variable after a wrapper would be
    # passed to it as an argument).
    first_wrapper = next((i for i, t in enumerate(before) if not _ENV_TOKEN.match(t)), len(before))
    old_env = [t for t in before[:first_wrapper] if t.partition("=")[0] not in env]
    new_env = [f"{k}={v}" for k, v in env.items()]
    tokens = [*old_env, *new_env, *before[first_wrapper:], "%command%", *after]
    return " ".join(_format_launch_token(t) for t in tokens), winedll
