"""Fase 3.1: Proton "Default (newest)" or pinned per game, previous version, update and pruning."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import proton_policy, runtime_manager, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"


def _proton(name: str) -> Path:
    tool = runtime_manager.get_compat_tools_dir() / name
    tool.mkdir(parents=True, exist_ok=True)
    (tool / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    (tool / ".deckinstall-managed").write_text(f"{name}\n", encoding="utf-8")
    return tool


def _install(tmp_path: Path, name: str, proton: str = "") -> standalone.StandaloneGame:
    src = tmp_path / "Downloads" / name
    src.mkdir(parents=True)
    exe = src / "Game.exe"
    exe.write_bytes(PE)
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        return standalone.install_standalone_game(name, exe, src, proton_name=proton)


def _protonpath(game: standalone.StandaloneGame) -> str:
    for line in game.launch_script.read_text(encoding="utf-8").splitlines():
        if line.startswith("export PROTONPATH="):
            return line.split("=", 1)[1].strip("'")
    return ""


def test_default_game_uses_newest_and_moves_on_update(tmp_path: Path) -> None:
    old = _proton("GE-Proton10-25")
    game = _install(tmp_path, "Alpha")
    assert proton_policy.is_default(game)
    assert _protonpath(game) == str(old)

    new = _proton("GE-Proton11-2")
    pinned = _install(tmp_path, "Beta", proton="GE-Proton10-25")
    assert proton_policy.retarget_default_games(previous_name=old.name) == 1

    reloaded = standalone.list_standalone_games()
    by_name = {g.name: g for g in reloaded}
    assert _protonpath(by_name["Alpha"]) == str(new)
    assert proton_policy.previous_proton_name(by_name["Alpha"]) == old.name
    assert _protonpath(by_name["Beta"]) == str(old)  # pinned games stay put
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["runtime"]["proton"] == {"mode": "default", "name": "", "previous": old.name}
    assert pinned.proton_name == "GE-Proton10-25"


def test_use_previous_pins_and_back_to_default(tmp_path: Path) -> None:
    old = _proton("GE-Proton10-25")
    new = _proton("GE-Proton11-2")
    game = _install(tmp_path, "Alpha")
    assert proton_policy.effective_proton_name(game) == new.name

    with pytest.raises(ValueError):
        proton_policy.use_previous_proton(game.game_dir)

    game = proton_policy.set_game_proton(game.game_dir, old.name)
    assert game.meta["proton_mode"] == "pinned" and _protonpath(game) == str(old)
    assert proton_policy.previous_proton_name(game) == new.name

    game = proton_policy.set_game_proton(game.game_dir, "")
    assert proton_policy.is_default(game) and _protonpath(game) == str(new)
    assert proton_policy.previous_proton_name(game) == old.name

    game = proton_policy.use_previous_proton(game.game_dir)
    assert game.proton_name == old.name and _protonpath(game) == str(old)


def test_batch_pin_unpin(tmp_path: Path) -> None:
    _proton("GE-Proton11-2")
    a = _install(tmp_path, "Alpha")
    b = _install(tmp_path, "Beta")
    changed, errors = proton_policy.set_games_proton([a.game_dir, b.game_dir, tmp_path / "missing"], "GE-Proton11-2")
    assert changed == 2 and len(errors) == 1
    assert all(g.proton_name == "GE-Proton11-2" for g in standalone.list_standalone_games())
    proton_policy.set_games_proton([a.game_dir, b.game_dir], "")
    assert all(proton_policy.is_default(g) for g in standalone.list_standalone_games())


def test_runtime_update_moves_default_games_and_prunes(tmp_path: Path) -> None:
    from deckinstall import ui_workers

    for name in ("GE-Proton10-4", "GE-Proton10-25"):
        _proton(name)
    game = _install(tmp_path, "Alpha")
    _proton("GE-Proton11-2")  # what ensure_umu_proton just installed
    worker = ui_workers.RuntimeUpdateWorker(paths=None)
    msg = worker._after_proton_update("GE-Proton10-25")
    assert "GE-Proton11-2" in msg
    assert _protonpath(standalone.list_standalone_games()[0]).endswith("GE-Proton11-2")
    # Without Steam paths the Steam mappings are unknown, so nothing is deleted.
    assert (runtime_manager.get_compat_tools_dir() / "GE-Proton10-4").is_dir()
    assert game.name == "Alpha"
    assert worker._after_proton_update("GE-Proton11-2") == ""


def test_runtime_update_retains_old_tools_for_review(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from deckinstall import steam_vdf, ui_workers

    for name in ("GE-Proton9-27", "GE-Proton10-4", "GE-Proton10-25", "GE-Proton11-2"):
        _proton(name)
    _install(tmp_path, "Alpha")
    usage = [SimpleNamespace(tool_name="GE-Proton9-27")]
    with mock.patch.object(steam_vdf, "list_compat_tool_usage", return_value=usage):
        msg = ui_workers.RuntimeUpdateWorker(paths=mock.Mock())._after_proton_update("GE-Proton10-25")
    assert "GE-Proton11-2" in msg
    left = sorted(p.name for p in runtime_manager.get_compat_tools_dir().iterdir())
    assert left == ["GE-Proton10-25", "GE-Proton10-4", "GE-Proton11-2", "GE-Proton9-27"]


def test_umu_proton_is_the_default_and_ge_games_move_to_it(tmp_path: Path) -> None:
    """UMU-Proton wins over a GE-Proton already on disk; "Default" games baked with GE move to it at
    the next library load, GE kept as their previous version (one click back). Pinned games stay."""
    ge = _proton("GE-Proton11-14")
    game = _install(tmp_path, "Alpha")
    pinned = _install(tmp_path, "Beta", proton="GE-Proton11-14")
    assert _protonpath(game) == str(ge)  # no UMU-Proton yet: the GE one already installed keeps games running

    umu = _proton("UMU-Proton-11.0-2e-x86_64")
    older = _proton("UMU-Proton-10.0-4")
    assert runtime_manager.list_umu_proton_dirs() == [umu, older]
    assert proton_policy.newest_default_proton() == umu
    assert proton_policy.retarget_stale_default_games() == 1

    by_name = {g.name: g for g in standalone.list_standalone_games()}
    assert _protonpath(by_name["Alpha"]) == str(umu)
    assert proton_policy.previous_proton_name(by_name["Alpha"]) == ge.name
    assert _protonpath(by_name["Beta"]) == str(ge) and pinned.proton_name == ge.name
    assert proton_policy.retarget_stale_default_games() == 0  # once only
    # Every GE-Proton DeckInstall installed is superseded now (Cleanup keeps the ones a game uses).
    assert ge in runtime_manager.superseded_managed_compat_tools()
    assert older in runtime_manager.superseded_managed_compat_tools()
    assert umu not in runtime_manager.superseded_managed_compat_tools()


def test_umu_proton_download_checks_sha512_and_keeps_umu_runs_folder_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import io
    import tarfile

    tag = "UMU-Proton-11.0-3"
    stem = f"{tag}-x86_64"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        for rel, data in (("proton", b"#!/bin/sh\n"), ("compatibilitytool.vdf", b"x"), ("toolmanifest.vdf", b"x"),
                          ("files/bin/wine", b"\x7fELF")):
            info = tarfile.TarInfo(f"{stem}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = payload.getvalue()
    sums = {"good": f"{hashlib.sha512(blob).hexdigest()}  {stem}.tar.gz\n", "bad": "0" * 128 + f"  {stem}.tar.gz\n"}
    monkeypatch.setattr(runtime_manager, "_get_arch", lambda: "x86_64")
    monkeypatch.setattr(runtime_manager, "_fetch_latest_umu_proton_release", lambda: {
        "tag_name": tag,
        "assets": [{"name": f"{stem}{ext}", "browser_download_url": f"https://github.com/x/{ext}"}
                   for ext in (".tar.gz", ".sha512sum")],
    })
    which = {"sum": "bad"}

    def download(url: str, dest: Path, _name: str, **_kw: object) -> None:
        dest.write_bytes(blob if url.endswith(".tar.gz") else sums[which["sum"]].encode())

    monkeypatch.setattr(runtime_manager, "_download_file", download)
    with pytest.raises(RuntimeError, match="SHA-512"):
        runtime_manager.ensure_umu_proton()
    assert runtime_manager.list_umu_proton_dirs() == []
    which["sum"] = "good"
    installed = runtime_manager.ensure_umu_proton()
    assert installed.name == stem  # the folder umu-run uses, so it never downloads the same release again
    assert runtime_manager.is_managed_compat_tool(installed)
    assert runtime_manager.ensure_umu_proton() == installed  # already there: nothing downloaded


def test_remove_lists_every_user_proton_with_its_users_and_rollbacks(tmp_path: Path) -> None:
    """A Proton installed by another program (no DeckInstall mark) is listed and can be removed;
    one kept as the "previous version" says which games lose it."""
    from deckinstall import runtime_uninstall as ru

    ge = _proton("GE-Proton11-14")
    game = _install(tmp_path, "Alpha")  # Default: on the GE one
    umu = _proton("UMU-Proton-11.0-2e-x86_64")
    assert proton_policy.retarget_stale_default_games() == 1  # now on UMU, GE kept as previous
    other = runtime_manager.get_compat_tools_dir() / "GE-Proton11-12"  # e.g. from ProtonUp
    other.mkdir()
    (other / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    items = {i.name: i for i in ru.list_runtimes(None) if i.kind == ru.PROTON}
    assert set(items) == {ge.name, umu.name, other.name}
    assert items[umu.name].users == (game.name,)
    assert items[ge.name].previous_of == (game.name,) and items[ge.name].users == ()
    assert items[other.name].users == () and items[other.name].previous_of == ()
    removed, errors = ru.uninstall([items[other.name]])
    assert removed == [other.name] and errors == [] and not other.exists()
