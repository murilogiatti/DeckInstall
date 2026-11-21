"""Ludusavi "home" saves only in allowed folders, covered by safety snapshots; Steam cloud only into its own app."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from deckinstall import ludusavi as lds  # noqa: E402
from deckinstall import save_sync  # noqa: E402
from deckinstall.i18n import tr  # noqa: E402
from deckinstall.ui_dialogs import save_sync_dialog as ssd  # noqa: E402

ALLOWED = ".local/share/Game/save.dat"
BLOCKED = (
    ".ssh/authorized_keys",
    ".config/autostart/evil.desktop",
    ".config/autostart/run",
    ".bashrc",
    ".local/share/Game/launcher.desktop",
    ".local/share/applications/x.desktop",
    ".local/bin/tool",
    "Syncthing/Game/save.dat",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(save_sync, "_home_root", lambda: h)
    return h


def _zip(path: Path, entries: dict[str, bytes], executable: tuple[str, ...] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100755 if name in executable else 0o100644) << 16
            zf.writestr(info, data)
    return path


def _unix_backup(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    folder = tmp_path / "linux-backup" / name
    lines = [f"name: {name}", "drives:", '  drive-0: ""', "backups:", '  - name: "."', '    when: "2025-12-04T15:02:54Z"',
             "    os: linux", "    files:"]
    for original, data in files.items():
        dst = folder / "drive-0" / original.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        lines += [f"      {original}:", f"        hash: {hashlib.sha1(data).hexdigest()}", f"        size: {len(data)}"]  # noqa: S324
    lines += ["    registry:", "      hash: ~", "    children: []"]
    (folder / "mapping.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_home_entry_allowed_lists() -> None:
    ok = (ALLOWED, ".config/MyGame/slot", ".renpy/game-123/persistent", "Documents/My Games/G/s.sav",
          ".paradoxinteractive/Stellaris/save.sav", ".godot/app_userdata/G/s.save", ".local/share/godot/app/s")
    for rel in ok:
        assert save_sync.home_entry_allowed(tuple(rel.split("/"))), rel
    bad = (*BLOCKED, ".gnupg/pubring.kbx", ".config/systemd/user/x.timer", ".config/environment.d/x.conf",
           ".local/share/keyrings/login.keyring", ".password-store/x.gpg", ".mozilla/firefox/p/key4.db",
           ".config/google-chrome/Default/Cookies", ".config/chromium/x", ".config/fish/config.fish",
           ".profile", ".bash_profile", ".zshrc", ".pam_environment", ".config/G/run.sh", ".config/G/x.service",
           ".config/settings.ini", ".local/share/x", "../etc/passwd", ".config/git/config",
           ".config/nvim/init.lua", ".local/share/flatpak/overrides/global", ".local/share/kservices5/x/y",
           ".config/Code/User/settings.json")
    for rel in bad:
        assert not save_sync.home_entry_allowed(tuple(rel.split("/"))), rel
    assert not save_sync.home_entry_allowed(tuple(ALLOWED.split("/")), 0o100755)


def test_import_keeps_only_allowed_home_files(tmp_path: Path) -> None:
    files = {f"/home/deck/{rel}": rel.encode() for rel in (ALLOWED, *BLOCKED)}
    folder = _unix_backup(tmp_path, "Game", files)
    snaps = tmp_path / "snaps"
    snap, stored, skipped, _ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "x", dest_root=snaps)
    assert snap is not None and (stored, skipped) == (1, len(BLOCKED))
    with zipfile.ZipFile(snap) as zf:
        assert zf.namelist() == [f"snap/home/{ALLOWED}"]
    assert save_sync.snapshot_home_files(snap.parent) == (ALLOWED,)

    # The direct import into an installed game follows the same list.
    home = tmp_path / "deckhome"
    home.mkdir()
    direct = _unix_backup(tmp_path / "d", "Game", {f"{home}/{rel}": rel.encode() for rel in (ALLOWED, *BLOCKED)})
    res = lds.import_ludusavi(direct, tmp_path / "pfx", "Game", home=home, dest_root=snaps)
    assert (res.restored, res.skipped) == (1, len(BLOCKED))
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()) == [ALLOWED]


def test_restore_refuses_blocked_and_executable_home_files(tmp_path: Path, home: Path) -> None:
    entries = {f"snap/home/{rel}": b"x" for rel in (ALLOWED, *BLOCKED, ".local/share/Game/tool")}
    snap = _zip(tmp_path / "snaps" / "game" / "s.zip", entries, executable=("snap/home/.local/share/Game/tool",))
    count, _safety = save_sync.restore_snapshot_safely(snap, tmp_path / "pfx", "Game", dest_root=tmp_path / "snaps")
    assert count == 1
    written = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())
    assert written == [ALLOWED]


def test_sync_zip_refuses_blocked_home_files(tmp_path: Path, home: Path) -> None:
    for rel in BLOCKED:
        assert save_sync._parse_sync_zip_entry(f"home/{rel}") is None
        assert save_sync._parse_sync_zip_entry(f"snap/home/{rel}") is None
    assert save_sync._parse_sync_zip_entry(f"home/{ALLOWED}") == ("home", tuple(ALLOWED.split("/")))

    sync_root = tmp_path / "sync"
    latest = _zip(
        sync_root / "game" / "latest.zip",
        {f"home/{rel}": b"x" for rel in (ALLOWED, *BLOCKED, ".config/Game/run")},
        executable=("home/.config/Game/run",),
    )
    (sync_root / "game" / "meta.json").write_text(json.dumps({"sha256": save_sync.file_sha256(latest)}))
    count, _ = save_sync.pull_game_saves("Game", tmp_path / "pfx", sync_root, snapshots_root=tmp_path / "snaps")
    assert count == 1
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()) == [ALLOWED]


def test_home_overwrite_is_covered_by_safety_snapshot_and_later_snapshots(tmp_path: Path, home: Path) -> None:
    snaps = tmp_path / "snaps"
    folder = _unix_backup(tmp_path, "Game", {f"/home/deck/{ALLOWED}": b"from-backup"})
    snap, _, _, _ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "x", dest_root=snaps)
    assert snap is not None
    (home / ALLOWED).parent.mkdir(parents=True)
    (home / ALLOWED).write_bytes(b"mine")
    prefix = tmp_path / "pfx"  # native game: no prefix

    skipped: list[str] = []
    count, safety = save_sync.restore_snapshot_safely(snap, prefix, "Game", dest_root=snaps, skipped=skipped)
    assert (count, skipped) == (1, []) and safety is not None
    assert (home / ALLOWED).read_bytes() == b"from-backup"
    with zipfile.ZipFile(safety) as zf:
        assert zf.read(f"snap/home/{ALLOWED}") == b"mine"

    # The previous content comes back from the safety snapshot.
    save_sync.restore_snapshot_safely(safety, prefix, "Game", dest_root=snaps)
    assert (home / ALLOWED).read_bytes() == b"mine"

    # Later snapshots hold the home file too, and its changes change the fingerprint.
    later = save_sync.auto_snapshot(prefix, "Game", "manual", dest_root=snaps)
    assert later is not None
    with zipfile.ZipFile(later) as zf:
        assert zf.read(f"snap/home/{ALLOWED}") == b"mine"
    (home / ALLOWED).write_bytes(b"played-more")
    newer = save_sync.auto_snapshot(prefix, "Game", "manual", dest_root=snaps)
    assert newer is not None and newer != later
    assert prefix.exists() is False


def test_home_file_not_in_safety_snapshot_is_skipped_and_reported(tmp_path: Path, home: Path) -> None:
    snap = _zip(tmp_path / "snaps" / "game" / "s.zip", {f"snap/home/{ALLOWED}": b"new"})
    (home / ALLOWED).mkdir(parents=True)  # a folder where the file should go: never replaced
    skipped: list[str] = []
    count, _ = save_sync.restore_snapshot_safely(snap, tmp_path / "pfx", "Game", dest_root=tmp_path / "snaps",
                                                 skipped=skipped)
    assert (count, skipped) == (0, [ALLOWED])
    assert (home / ALLOWED).is_dir()


def _cloud_import(tmp_path: Path) -> tuple[Path, Path]:
    folder = _unix_backup(tmp_path, "Cloud", {"/home/deck/.local/share/Steam/userdata/1/311340/remote/slot": b"saved"})
    snap, _, _, _ = lds.import_ludusavi_to_snapshot(folder, home=tmp_path / "x", dest_root=tmp_path / "snaps")
    assert snap is not None
    assert save_sync.snapshot_steamcloud_appid(snap) == "311340"
    return snap, tmp_path / "userdata" / "999" / "remote"


def test_steam_cloud_of_another_app_is_refused(tmp_path: Path) -> None:
    snap, cloud = _cloud_import(tmp_path)
    cloud.mkdir(parents=True)
    (cloud / "slot").write_bytes(b"other-game")
    with pytest.raises(save_sync.SteamCloudAppMismatch):
        save_sync.restore_snapshot_safely(snap, tmp_path / "pfx", "Cloud", dest_root=tmp_path / "snaps",
                                          extra_dirs=(("steamcloud", cloud),), steam_appid=999)
    assert (cloud / "slot").read_bytes() == b"other-game"

    t = ssd.SaveTarget("Cloud", tmp_path / "pfx", None, 999, None, "steam", (("steamcloud", cloud),))
    entry = save_sync.SaveArchiveEntry(snap, "save_snapshot", "", "cloud", 0.0, 0)
    with mock.patch.object(ssd, "is_steam_running", return_value=False), pytest.raises(RuntimeError) as err:
        ssd._restore_job(t, entry)
    assert str(err.value) == tr("saves.steamcloud_appid_mismatch", snap="311340", target=999)

    right = tmp_path / "userdata" / "311340" / "remote"
    t = ssd.SaveTarget("Cloud", tmp_path / "pfx", None, 311340, None, "steam", (("steamcloud", right),))
    with mock.patch.object(ssd, "is_steam_running", return_value=False):
        res = ssd._restore_job(t, entry)
    assert isinstance(res, tuple)
    count, _safety, skipped = res
    assert (count, skipped) == (1, 0) and (right / "slot").read_bytes() == b"saved"


def test_steam_cloud_restore_refused_while_steam_runs(tmp_path: Path) -> None:
    snap, _ = _cloud_import(tmp_path)
    cloud = tmp_path / "userdata" / "311340" / "remote"
    t = ssd.SaveTarget("Cloud", tmp_path / "pfx", None, 311340, None, "steam", (("steamcloud", cloud),))
    entry = save_sync.SaveArchiveEntry(snap, "save_snapshot", "", "cloud", 0.0, 0)
    with mock.patch.object(ssd, "is_steam_running", return_value=True), \
            mock.patch.object(ssd, "restore_snapshot_safely") as restore, pytest.raises(RuntimeError) as err:
        ssd._restore_job(t, entry)
    assert str(err.value) == tr("saves.steam_running")
    restore.assert_not_called()

    # A snapshot without Steam cloud files is not held back by an open Steam.
    plain = _zip(tmp_path / "snaps" / "cloud" / "p.zip", {"snap/users/steamuser/Documents/s.sav": b"x"})
    entry = save_sync.SaveArchiveEntry(plain, "save_snapshot", "", "cloud", 0.0, 0)
    with mock.patch.object(ssd, "is_steam_running", return_value=True), \
            mock.patch.object(ssd, "restore_snapshot_safely", return_value=(1, None)) as restore:
        assert ssd._restore_job(t, entry) == (1, None, 0)
    restore.assert_called_once()
