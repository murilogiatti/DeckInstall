"""Gaming Mode detection reads /proc: a stray gamescope on the Desktop must not count."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from deckinstall import steam_paths


def _fake_proc(root: Path, procs: dict[int, tuple[str, int]], uid: int | None = None) -> Path:
    owner = os.geteuid() if uid is None else uid
    for pid, (name, ppid) in procs.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "status").write_text(
            f"Name:\t{name}\nPPid:\t{ppid}\nUid:\t{owner}\t{owner}\t{owner}\t{owner}\n",
            encoding="utf-8",
        )
    (root / "self").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("XDG_CURRENT_DESKTOP", "GAMESCOPE_WAYLAND_DISPLAY", "SteamGamepadUI"):
        monkeypatch.delenv(key, raising=False)


def _use(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(steam_paths, "_PROC_ROOT", root)
    steam_paths.invalidate_process_cache()


def test_stray_gamescope_on_desktop_is_not_gaming_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None
) -> None:
    # Desktop: plasma -> steam.sh -> steam; a game wrapped in gamescope runs elsewhere.
    root = _fake_proc(
        tmp_path / "proc",
        {
            100: ("plasmashell", 1),
            200: ("bash", 100),
            201: ("steam", 200),
            300: ("gamescope", 100),
            301: ("game.exe", 300),
        },
    )
    _use(monkeypatch, root)
    assert steam_paths.is_gamescope_session() is False
    assert steam_paths.is_steam_running() is True


def test_steam_under_gamescope_is_gaming_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None) -> None:
    root = _fake_proc(tmp_path / "proc", {100: ("gamescope", 1), 200: ("bash", 100), 201: ("steam", 200)})
    _use(monkeypatch, root)
    assert steam_paths.is_gamescope_session() is True


@pytest.mark.parametrize("comm", ["gamescope-sessi", "gamescope-session"])
def test_session_process_is_gaming_mode(
    comm: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None
) -> None:
    # /proc truncates "gamescope-session-plus" to 15 chars: "gamescope-sessi".
    root = _fake_proc(tmp_path / "proc", {100: (comm, 1), 200: ("steam", 1)})
    _use(monkeypatch, root)
    assert steam_paths.is_gamescope_session() is True


def test_other_users_processes_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None) -> None:
    root = _fake_proc(tmp_path / "proc", {100: ("gamescope-sessi", 1), 201: ("steam", 1)}, uid=os.geteuid() + 1)
    _use(monkeypatch, root)
    assert steam_paths.is_gamescope_session() is False
    assert steam_paths.is_steam_running() is False


def test_env_marks_gaming_mode_without_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None) -> None:
    _use(monkeypatch, _fake_proc(tmp_path / "proc", {}))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    assert steam_paths.is_gamescope_session() is True


def test_cache_expires_and_invalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env: None) -> None:
    root = _fake_proc(tmp_path / "proc", {201: ("steam", 1)})
    _use(monkeypatch, root)
    clock = {"now": 1000.0}
    monkeypatch.setattr(steam_paths.time, "monotonic", lambda: clock["now"])
    assert steam_paths.is_steam_running() is True
    (root / "201" / "status").unlink()
    (root / "201").rmdir()
    clock["now"] += 1.0
    assert steam_paths.is_steam_running() is True  # still cached
    clock["now"] += 1.5
    assert steam_paths.is_steam_running() is False  # expired
    _fake_proc(root, {202: ("steam", 1)})
    assert steam_paths.is_steam_running() is False  # cached again
    steam_paths.invalidate_process_cache()
    assert steam_paths.is_steam_running() is True


def test_shutdown_and_start_invalidate_cache() -> None:
    from deckinstall import steam_vdf

    with (
        mock.patch.object(steam_vdf, "invalidate_process_cache") as inval,
        mock.patch.object(steam_vdf, "is_steam_running", return_value=False),
    ):
        assert steam_vdf.shutdown_steam() is True
        assert inval.call_count >= 2
        inval.reset_mock()
        with mock.patch.object(steam_vdf, "is_gamescope_session", return_value=True):
            assert steam_vdf.start_steam() is False
        assert inval.call_count >= 2
