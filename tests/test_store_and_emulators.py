"""Fase 2.4: OnlineFix/Goldberg/CODEX overrides, STORE=gog from goggame, umu-database download."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import runtime_manager, scanner, standalone  # noqa: E402
from deckinstall.manifest import MANIFEST_V2  # noqa: E402

PE = b"MZ" + b"\x00" * 58 + (64).to_bytes(4, "little") + b"PE\x00\x00"
CSV = (
    "TITLE,STORE,CODENAME,UMU_ID,COMMON ACRONYM (Optional),NOTE (Optional),EXE_STRINGS (Optional)\n"
    "Age of Wonders,gog,1207658883,umu-61500,aow,,\n"
    "Age of Wonders,humble,ageofwonders,umu-61500,aow,,\n"
)


def _game(root: Path, *files: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    exe = root / "Game.exe"
    exe.write_bytes(PE)
    for name in files:
        p = root / name
        if name.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
    return exe


@pytest.mark.parametrize(
    ("files", "kind", "must_have"),
    [
        (("OnlineFix.ini", "OnlineFix64.dll", "winmm.dll"), "onlinefix", "OnlineFix64=n"),
        (("steam_api64.dll", "steam_settings/"), "goldberg", "steam_api64=n"),
        (("steam_api64.dll", "steamclient64.dll", "ColdClientLoader.ini"), "goldberg", "steamclient64=n"),
        (("steam_api64.dll", "steam_emu.ini"), "codex", "steam_api64=n"),
        (("steam_api64.dll",), "", ""),
    ],
)
def test_detect_steam_emulator(tmp_path: Path, files: tuple[str, ...], kind: str, must_have: str) -> None:
    exe = _game(tmp_path / "G", *files)
    found_kind, overrides = scanner.detect_steam_emulator(exe, exe.parent)
    assert found_kind == kind
    assert must_have in overrides


def test_merge_dll_overrides_keeps_user_choice() -> None:
    merged = standalone.merge_dll_overrides("winmm=b;dxgi=n,b", "OnlineFix64=n;winmm=n,b;dnet=n")
    entries = merged.split(";")
    assert "winmm=b" in entries and "winmm=n,b" not in entries
    assert "OnlineFix64=n".lower() in merged.lower() and "dnet=n" in entries and "dxgi=n,b" in entries


def test_install_applies_onlinefix_overrides(tmp_path: Path) -> None:
    exe = _game(tmp_path / "Downloads" / "Game", "OnlineFix.ini", "OnlineFix64.dll")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("Game", exe, exe.parent, winedlloverrides="dxgi=n,b")
    # Wine DLL names are case-insensitive; the override helper stores them in lower case.
    assert "onlinefix64=n" in game.winedlloverrides.lower() and "dxgi=n,b" in game.winedlloverrides
    assert "onlinefix64=n" in game.launch_script.read_text(encoding="utf-8").lower()
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["runtime"]["emulator"] == "onlinefix"
    reloaded = standalone._load_manifest(game.game_dir / MANIFEST_V2)
    assert reloaded is not None and reloaded.meta.get("emulator") == "onlinefix"


def test_gog_game_gets_store_gog_and_gameid(tmp_path: Path) -> None:
    db = runtime_manager.get_umu_database_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text(CSV, encoding="utf-8")
    src = tmp_path / "Downloads" / "Age of Wonders"
    exe = _game(src)
    (src / "goggame-1207658883.info").write_text(json.dumps({"gameId": "1207658883"}), encoding="utf-8")
    assert scanner.gog_product_id(src) == "1207658883"

    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("Age of Wonders", exe, src)
    script = game.launch_script.read_text(encoding="utf-8")
    assert "export GAMEID=umu-61500" in script
    assert "export STORE=gog" in script
    data = json.loads((game.game_dir / MANIFEST_V2).read_text(encoding="utf-8"))
    assert data["source"]["store"] == "gog" and data["source"]["codename"] == "1207658883"

    # Rewriting the launcher later (e.g. new launch options) keeps STORE/GAMEID.
    standalone.update_standalone_launch_options(game.game_dir, launch_options="gamemoderun %command%")
    assert "export STORE=gog" in game.launch_script.read_text(encoding="utf-8")


def test_codename_lookup_needs_matching_store(tmp_path: Path) -> None:
    db = tmp_path / "db.csv"
    db.write_text(CSV, encoding="utf-8")
    assert standalone.resolve_umu_game_id(None, store="gog", codename="1207658883", db_path=db) == ("umu-61500", "gog")
    hit = runtime_manager.lookup_umu_codename("1207658883", "egs", db_path=db)
    assert hit is None


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536) -> list[bytes]:
        return [self.body]


def test_umu_database_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "runtime" / "umu-database.csv"
    monkeypatch.setattr(runtime_manager, "get_umu_database_path", lambda: db)
    monkeypatch.setattr(runtime_manager, "_load_versions", lambda: {})
    monkeypatch.setattr(runtime_manager, "_update_versions", lambda fn: fn({}))
    assert "raw.githubusercontent.com" in runtime_manager.UMU_DATABASE_URL
    assert runtime_manager.UMU_DATABASE_URL.endswith("/umu-database.csv")

    with mock.patch.object(runtime_manager, "_request_with_validated_redirects", return_value=_Resp(CSV.encode())) as req:
        assert runtime_manager._ensure_umu_database_impl() == db
    assert req.call_args.kwargs["allowed_hosts"] == frozenset({"raw.githubusercontent.com"})
    assert db.read_text(encoding="utf-8") == CSV

    # An HTML page (what the old URL returned) is rejected and the cached CSV is kept.
    html = b"<!DOCTYPE html><html><body>umu</body></html>"
    with mock.patch.object(runtime_manager, "_request_with_validated_redirects", return_value=_Resp(html)):
        assert runtime_manager._ensure_umu_database_impl(force_update=True) == db
    assert db.read_text(encoding="utf-8") == CSV

    # A fresh cache is not downloaded again.
    monkeypatch.setattr(runtime_manager, "_load_versions", lambda: {"umu_db_last_check": int(time.time())})
    with mock.patch.object(runtime_manager, "_request_with_validated_redirects") as req:
        assert runtime_manager._ensure_umu_database_impl() == db
    req.assert_not_called()


def test_umu_database_failure_without_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_manager, "get_umu_database_path", lambda: tmp_path / "none" / "umu-database.csv")
    with mock.patch.object(runtime_manager, "_request_with_validated_redirects", side_effect=OSError("offline")):
        assert runtime_manager._ensure_umu_database_impl() is None
    assert not (tmp_path / "none" / "umu-database.csv").exists()
