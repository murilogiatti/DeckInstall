"""Native Linux saves from the Ludusavi manifest, and the newest current save time."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deckinstall import save_sync


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(save_sync, "_home_root", lambda: tmp_path)
    return tmp_path


def _entry() -> dict[str, object]:
    return {
        "files": {
            "<xdgData>/Indie Game/saves": {"tags": ["save"], "when": [{"os": "linux"}]},
            "<xdgConfig>/indie/*.cfg": {"tags": ["config"], "when": [{"os": "linux"}]},
            "<home>/.indie/profile.dat": {"when": [{"os": "linux"}]},
            "<winAppData>/Indie Game": {"tags": ["save"], "when": [{"os": "windows"}]},
            "<home>/.ssh": {"when": [{"os": "linux"}]},
        }
    }


def test_linux_save_paths_resolve_under_home(home: Path) -> None:
    saves = home / ".local/share/Indie Game/saves"
    saves.mkdir(parents=True)
    (saves / "slot1.sav").write_text("1", encoding="utf-8")
    (saves / "run.sh").write_text("#!/bin/sh", encoding="utf-8")  # never a save
    (home / ".config/indie").mkdir(parents=True)
    (home / ".config/indie/opts.cfg").write_text("x", encoding="utf-8")  # tagged config, not save
    (home / ".indie").mkdir()
    (home / ".indie/profile.dat").write_text("p", encoding="utf-8")  # .indie is not an allowed root
    (home / ".ssh").mkdir()
    (home / ".ssh/id_rsa").write_text("secret", encoding="utf-8")
    found = save_sync._resolve_manifest_home_paths(_entry())
    assert [(sec, rel.as_posix()) for sec, _src, rel in found] == [
        ("home", ".local/share/Indie Game/saves/slot1.sav")
    ]


def test_windows_only_entry_adds_nothing(home: Path) -> None:
    entry = {"files": {"<home>/Documents/My Games/X": {"when": [{"os": "windows"}]}}}
    assert save_sync._resolve_manifest_home_paths(entry) == []


def test_newest_save_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = tmp_path / "cloud"
    extra.mkdir()
    old, new = extra / "a.sav", extra / "b.sav"
    old.write_text("a", encoding="utf-8")
    new.write_text("b", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (5000, 5000))
    monkeypatch.setattr(save_sync, "EXTRA_SECTIONS", (*save_sync.EXTRA_SECTIONS, "testcloud"))
    got = save_sync.newest_save_mtime(tmp_path / "nopfx", "", extra_dirs=[("testcloud", extra)])
    assert got == 5000
