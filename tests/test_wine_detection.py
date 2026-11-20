"""Detection of running Windows games must ignore the Steam client's own runtime helpers."""

from __future__ import annotations

from pathlib import Path

from deckinstall.ui_cards import _is_wine_game_running


def _fake_proc(root: Path, pid: int, argv: list[str], environ: str = "") -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")
    (d / "environ").write_bytes(environ.encode())


def test_steam_client_runtime_is_not_a_game(tmp_path: Path) -> None:
    steam = "/home/deck/.local/share/Steam/steamrt64/pv-runtime/steam-runtime-sniper"
    _fake_proc(tmp_path, 101, [f"{steam}/usr/libexec/srt-bwrap", "--args", "7"])
    _fake_proc(tmp_path, 102, ["/home/deck/.local/share/Steam/ubuntu12_64/steamwebhelper", "--type=zygote"])
    _fake_proc(tmp_path, 103, [f"{steam}/pressure-vessel/bin/pressure-vessel-wrap", "--steam-runtime"])
    assert _is_wine_game_running(tmp_path) is False


def test_exe_is_a_game(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 201, ["Z:\\\\games\\\\Foo\\\\Foo.exe"])
    assert _is_wine_game_running(tmp_path) is True


def test_wrapper_on_wine_prefix_is_a_game(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 301, ["/usr/bin/pv-bwrap", "--bind", "/home/deck/.steam/compatdata/123/pfx", "/pfx"])
    assert _is_wine_game_running(tmp_path) is True


def test_umu_run_with_wineprefix_env_is_a_game(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 401, ["/usr/bin/umu-run", "game"], environ="WINEPREFIX=/x/y\x00")
    assert _is_wine_game_running(tmp_path) is True


def test_wineserver_is_a_game(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 501, ["/opt/proton/files/bin/wineserver", "-p"])
    assert _is_wine_game_running(tmp_path) is True


def test_missing_proc_root(tmp_path: Path) -> None:
    assert _is_wine_game_running(tmp_path / "nope") is False
