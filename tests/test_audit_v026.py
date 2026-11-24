"""v0.26.1 audit fixes: restore never writes executables or session-startup files, Exec= escaping, checksums."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from deckinstall import cleanup, runtime_manager, save_sync, standalone  # noqa: E402
from deckinstall.fsutil import desktop_exec_quote, desktop_exec_tokens  # noqa: E402


@pytest.mark.parametrize(
    "parts",
    [
        (".local", "share", "gnome-shell", "extensions", "x@y", "extension.js"),
        (".config", "openbox", "autostart"),
        (".config", "lxsession", "LXDE", "autostart"),
        (".config", "awesome", "rc.lua"),
        (".config", "qtile", "config.py"),
        (".config", "niri", "config.kdl"),
        (".config", "Game", "run.py"),
    ],
)
def test_home_blocks_session_code(parts: tuple[str, ...]) -> None:
    assert not save_sync.home_entry_allowed(parts)


@pytest.mark.parametrize(
    "parts",
    [
        (".local", "share", "MyGame", "save1.dat"),
        (".config", "MyGame", "settings.ini"),
        (".renpy", "Game", "persistent"),
    ],
)
def test_home_still_allows_saves(parts: tuple[str, ...]) -> None:
    assert save_sync.home_entry_allowed(parts)


def _make_game(tmp_path: Path) -> tuple[Path, Path]:
    game = tmp_path / "game"
    (game / "saves").mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"orig")
    pfx = tmp_path / "pfx"
    (pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    return game, pfx


def test_restore_never_writes_executables(tmp_path: Path) -> None:
    game, pfx = _make_game(tmp_path)
    z = tmp_path / "a.zip"
    startup = "users/steamuser/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/x.exe"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("game/Game.exe", b"PWNED")
        f.writestr("game/saves/helper.dll", b"X")
        f.writestr(startup, b"X")
        f.writestr("game/saves/slot1.sav", b"SAVE")
        f.writestr("users/steamuser/Documents/My Games/G/s.dat", b"S")
        info = zipfile.ZipInfo("game/saves/tool")
        info.external_attr = 0o755 << 16
        f.writestr(info, b"#!/bin/sh")
    save_sync._restore_saves_archive(z, pfx, game)
    assert (game / "Game.exe").read_bytes() == b"orig"
    assert not (game / "saves" / "helper.dll").exists()
    assert not (game / "saves" / "tool").exists()
    assert not list((pfx / "drive_c").rglob("x.exe"))
    assert (game / "saves" / "slot1.sav").read_bytes() == b"SAVE"
    assert list((pfx / "drive_c").rglob("s.dat"))


NAMES = ["100% Orange Juice", "Game $HOME `x` back\\slash", 'Quote " game', "Plain Game", "NoSpace"]


@pytest.mark.parametrize("name", NAMES)
def test_desktop_exec_roundtrip(name: str) -> None:
    script = Path(f"/h/{name}/launch.sh")
    tokens = desktop_exec_tokens(f"Exec={desktop_exec_quote(script)}"[5:])
    assert tokens == [str(script)]
    assert standalone._desktop_quote(script) == desktop_exec_quote(script)


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-validate missing")
@pytest.mark.parametrize("name", NAMES)
def test_desktop_exec_validates(tmp_path: Path, name: str) -> None:
    df = tmp_path / "t.desktop"
    df.write_text(
        "[Desktop Entry]\nType=Application\nName=T\nExec="
        + desktop_exec_quote(Path(f"/h/{name}/launch.sh")) + "\n"
    )
    r = subprocess.run(["desktop-file-validate", str(df)], capture_output=True, text=True)
    assert r.returncode == 0 and "invalid field code" not in r.stdout + r.stderr


def test_orphan_check_understands_escaped_exec(tmp_path: Path) -> None:
    game = tmp_path / "100% Orange $Juice"
    game.mkdir()
    script = game / "launch.sh"
    script.write_text("#!/bin/sh\n")
    df = tmp_path / "deckinstall-x.desktop"
    df.write_text(f"[Desktop Entry]\nExec={desktop_exec_quote(script)}\nPath={game}\n")
    assert cleanup._is_orphan_desktop_file(df) is False
    assert standalone._desktop_points_to_game_dir(df, game)


def test_umu_run_requires_checksum(tmp_path: Path) -> None:
    f = tmp_path / "umu-run"
    f.write_bytes(b"x" * 10)
    with pytest.raises(RuntimeError, match="checksum"):
        runtime_manager._verify_release_asset_integrity(
            {"assets": []}, {"name": "umu-run", "size": 10}, f, tmp_path, label="umu-run", require_checksum=True,
        )
