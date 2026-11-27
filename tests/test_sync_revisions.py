"""Shared save folder, format 2: immutable revisions, conflicts kept, nothing restored unless verified.

Two devices are simulated with two replicas of the shared folder and a "transport" that copies
files between them (any order, partial copies), like Syncthing, rclone, rsync or SFTP would.
"""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from deckinstall import save_sync
from deckinstall import save_sync_repo as repo
from deckinstall.save_sync import plan_sync_all, pull_game_saves, push_game_saves

GAME = "Hollow Test"
REL = "drive_c/users/steamuser/Documents/HollowTest/save.dat"


def _save(prefix: Path, data: bytes) -> Path:
    path = prefix / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _action(prefix: Path, root: Path) -> save_sync.SyncPlanItem:
    return plan_sync_all([(GAME, prefix)], root)[0]


def _transport(src: Path, dst: Path, only: list[Path] | None = None) -> None:
    """Copy what src has and dst lacks (never deletes; may copy just some files)."""
    for path in sorted(src.rglob("*")):
        if path.is_dir() or "/.staging/" in str(path):
            continue
        if only is not None and path not in only:
            continue
        target = dst / path.relative_to(src)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _revs(root: Path) -> list[Path]:
    return sorted(root.rglob("revs/*.zip"))


@pytest.fixture
def two(tmp_path: Path) -> dict[str, Path]:
    a_root, b_root = tmp_path / "replica_a", tmp_path / "replica_b"
    a_root.mkdir()
    b_root.mkdir()
    return {"a_root": a_root, "b_root": b_root, "a": tmp_path / "dev_a", "b": tmp_path / "dev_b",
            "snaps": tmp_path / "snaps"}


def _start_both(two: dict[str, Path]) -> None:
    """A pushes v1, it reaches B, B restores it: both share one base."""
    _save(two["a"], b"v1")
    assert push_game_saves(GAME, two["a"], two["a_root"]) is not None
    _transport(two["a_root"], two["b_root"])
    two["b"].mkdir(exist_ok=True)
    assert _action(two["b"], two["b_root"]).action == "PULL"
    assert pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"])[0] == 1
    assert _action(two["b"], two["b_root"]).action == "IN_SYNC"


def test_offline_edits_on_both_devices_keep_both_versions(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"A-offline")
    _save(two["b"], b"B-offline")
    push_game_saves(GAME, two["a"], two["a_root"])
    push_game_saves(GAME, two["b"], two["b_root"])
    _transport(two["a_root"], two["b_root"])
    _transport(two["b_root"], two["a_root"])

    for dev, root in (("a", "a_root"), ("b", "b_root")):
        item = _action(two[dev], two[root])
        assert item.action == "CONFLICT" and len(item.heads) == 2
        assert {h.device_label for h in item.heads}  # who made each version is shown
    assert len(_revs(two["a_root"])) == 3  # v1 and both branches: nothing overwritten

    # A plain push cannot hide B's version...
    with pytest.raises(repo.SyncRemoteChanged):
        push_game_saves(GAME, two["a"], two["a_root"])
    # ...the user's choice (keep A's) makes a revision that replaces both branches.
    push_game_saves(GAME, two["a"], two["a_root"], resolve_heads=True)
    assert _action(two["a"], two["a_root"]).action == "IN_SYNC"
    merge = repo.scan_game_folder(next(two["a_root"].glob("*/revs")).parent).heads()
    assert len(merge) == 1 and len(merge[0].parents) == 2
    _transport(two["a_root"], two["b_root"])
    # B's version was part of the decision (the new revision replaces it), and B did not change
    # anything since: B simply follows the decision.
    assert _action(two["b"], two["b_root"]).action == "PULL"
    pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"])
    assert (two["b"] / REL).read_bytes() == b"A-offline"
    assert _action(two["b"], two["b_root"]).action == "IN_SYNC"


def test_same_saves_made_independently_are_not_a_conflict(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"same")
    _save(two["b"], b"same")
    push_game_saves(GAME, two["a"], two["a_root"])
    push_game_saves(GAME, two["b"], two["b_root"])
    _transport(two["a_root"], two["b_root"])
    _transport(two["b_root"], two["a_root"])
    assert _action(two["a"], two["a_root"]).action == "IN_SYNC"
    assert _action(two["b"], two["b_root"]).action == "IN_SYNC"


def test_the_plan_is_checked_again_before_publishing(two: dict[str, Path]) -> None:
    """The confirmed bug: a push planned earlier overwrote a newer version from another device."""
    _start_both(two)
    _save(two["a"], b"A-new")
    assert _action(two["a"], two["a_root"]).action == "PUSH"  # planned...
    _save(two["b"], b"B-newer")
    push_game_saves(GAME, two["b"], two["b_root"])
    _transport(two["b_root"], two["a_root"])  # ...then B's version arrives
    with pytest.raises(repo.SyncRemoteChanged):
        push_game_saves(GAME, two["a"], two["a_root"])
    assert len(repo.scan_game_folder(next(two["a_root"].glob("*/revs")).parent).heads()) == 1


def test_out_of_order_delivery_and_missing_parent(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"v2")
    push_game_saves(GAME, two["a"], two["a_root"])
    _save(two["a"], b"v3")
    push_game_saves(GAME, two["a"], two["a_root"])
    newest = max(_revs(two["a_root"]), key=lambda p: repo.read_revision(p).created_at)
    _transport(two["a_root"], two["b_root"], only=[newest])  # v3 arrives, v2 not yet
    item = _action(two["b"], two["b_root"])
    # v3's parent v2 has not arrived: B cannot prove v3 continues its base, so it never applies it
    assert item.action == "CONFLICT" and "still arriving" in item.reason
    _transport(two["a_root"], two["b_root"])  # late v2 arrives: it is an ancestor, not a new head
    assert _action(two["b"], two["b_root"]).action == "PULL"
    pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"])
    assert (two["b"] / REL).read_bytes() == b"v3"
    assert _action(two["b"], two["b_root"]).action == "IN_SYNC"

    stranger = two["a"].parent / "dev_c"
    _save(stranger, b"unrelated")
    assert _action(stranger, two["b_root"]).action == "CONFLICT"  # no shared base: ask


def test_partial_transfer_is_waiting_and_never_restored(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"v2" * 50_000)
    pushed = push_game_saves(GAME, two["a"], two["a_root"])
    assert pushed is not None
    dst = two["b_root"] / pushed.relative_to(two["a_root"])
    dst.write_bytes(pushed.read_bytes()[: pushed.stat().st_size // 2])  # half copied
    item = _action(two["b"], two["b_root"])
    assert item.action == "IN_SYNC" and item.waiting == 1 and "still arriving" in item.reason
    before = (two["b"] / REL).read_bytes()
    with pytest.raises(repo.SyncRemoteChanged):  # the plan saw no new version; a forced pull is refused
        pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"], rev_id=dst.stem)
    assert (two["b"] / REL).read_bytes() == before
    shutil.copy2(pushed, dst)  # transfer finished
    assert _action(two["b"], two["b_root"]).action == "PULL"


def test_damaged_revision_is_refused_before_anything_changes(two: dict[str, Path]) -> None:
    _save(two["a"], b"good")
    pushed = push_game_saves(GAME, two["a"], two["a_root"])
    assert pushed is not None
    with zipfile.ZipFile(pushed, "a") as zf:  # content no longer matches the manifest
        zf.writestr("save/users/steamuser/Documents/HollowTest/extra.dat", b"x")
    two["b"].mkdir()
    with pytest.raises(RuntimeError, match="incomplete or damaged"):
        pull_game_saves(GAME, two["b"], two["a_root"], snapshots_root=two["snaps"])
    assert not (two["b"] / "drive_c").exists()


def test_publishing_never_replaces_an_existing_revision(tmp_path: Path) -> None:
    game_dir = tmp_path / "root" / "game"
    (game_dir / repo.REVS_DIR).mkdir(parents=True)
    rev_id = repo.new_rev_id()
    existing = game_dir / repo.REVS_DIR / f"{rev_id}.zip"
    existing.write_bytes(b"someone else's revision")
    tmp = repo.staging_file(game_dir)
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr("save/users/x/save.dat", b"data")
        digest, count, total = repo.save_entries_digest(zf)
        zf.writestr(repo.REV_MANIFEST, repo.revision_manifest(rev_id, [], "s", digest, count, total, "G"))
    with pytest.raises(FileExistsError):
        repo.publish_revision(game_dir, tmp, rev_id)
    assert existing.read_bytes() == b"someone else's revision"
    assert not tmp.exists()


def test_unmounted_or_replaced_sync_folder_is_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "media" / "sd" / "Sync"
    root.parent.mkdir(parents=True)
    prefix = tmp_path / "pfx"
    _save(prefix, b"v1")
    push_game_saves(GAME, prefix, root)  # first setup creates the folder and its marker
    assert (root / repo.ROOT_MARKER).is_file()

    shutil.move(root, tmp_path / "elsewhere")  # the card is not mounted
    assert _action(prefix, root).action == "UNAVAILABLE"
    with pytest.raises(repo.SyncFolderUnavailable):
        push_game_saves(GAME, prefix, root)
    assert not root.exists()  # used to be created again, empty
    root.mkdir()  # an empty mount point, or another folder in its place
    assert _action(prefix, root).action == "UNAVAILABLE"
    with pytest.raises(repo.SyncFolderUnavailable):
        pull_game_saves(GAME, prefix, root)
    assert list(root.iterdir()) == []


def test_interrupted_push_leaves_the_previous_version(two: dict[str, Path]) -> None:
    _save(two["a"], b"v1")
    first = push_game_saves(GAME, two["a"], two["a_root"])
    _save(two["a"], b"v2")
    with mock.patch.object(save_sync, "_write_save_entries", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            push_game_saves(GAME, two["a"], two["a_root"])
    with mock.patch.object(repo, "publish_revision", side_effect=OSError(errno.ENOSPC, "No space left")):
        with pytest.raises(OSError):
            push_game_saves(GAME, two["a"], two["a_root"])
    assert _revs(two["a_root"]) == [first]
    assert not [p for p in two["a_root"].rglob(".staging/*") if p.is_file()]
    assert _action(two["a"], two["a_root"]).action == "PUSH"  # nothing was recorded as sent


def test_interrupted_pull_keeps_local_saves_and_base(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"v2")
    push_game_saves(GAME, two["a"], two["a_root"])
    _transport(two["a_root"], two["b_root"])
    with mock.patch.object(save_sync, "_restore_saves_archive", side_effect=OSError(errno.ENOSPC, "full")):
        with pytest.raises((OSError, RuntimeError)):
            pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"])
    assert (two["b"] / REL).read_bytes() == b"v1"
    assert _action(two["b"], two["b_root"]).action == "PULL"  # still to do, not marked as done
    assert not list(repo.local_staging_dir().glob("*.zip"))  # private copy cleaned up


def test_restored_bytes_equal_the_pushed_bytes(two: dict[str, Path]) -> None:
    payload = bytes(range(256)) * 4096
    _save(two["a"], payload)
    push_game_saves(GAME, two["a"], two["a_root"])
    _transport(two["a_root"], two["b_root"])
    _save(two["b"], b"old")
    pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"],
                    rev_id=_action(two["b"], two["b_root"]).heads[0].rev_id)
    assert hashlib.sha256((two["b"] / REL).read_bytes()).digest() == hashlib.sha256(payload).digest()


def test_transport_leftovers_are_ignored(two: dict[str, Path]) -> None:
    _save(two["a"], b"v1")
    pushed = push_game_saves(GAME, two["a"], two["a_root"])
    assert pushed is not None
    revs = pushed.parent
    for junk in (f"{pushed.stem}.sync-conflict-20260101-120000-ABCDEF.zip", f".{pushed.name}.tmp",
                 "~syncthing~x.zip.tmp", f"{pushed.name}.partial", ".rclone-part.zip"):
        (revs / junk).write_bytes(b"junk")
    state = repo.scan_game_folder(revs.parent)
    assert list(state.revisions) == [pushed.stem] and state.waiting == []


def test_legacy_layout_is_read_imported_without_changes_and_old_client_writes_become_a_branch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sync"
    root.mkdir()
    old = tmp_path / "old_client"
    _save(old, b"legacy-v1")
    # what v0.35 wrote: <slug>/latest.zip + meta.json
    with mock.patch.object(repo, "open_sync_root"), mock.patch.object(save_sync, "_push_parents",
                                                                         return_value=([], None)):
        pushed = push_game_saves(GAME, old, root)
    assert pushed is not None
    game_dir = pushed.parent.parent
    shutil.move(pushed, game_dir / "latest.zip")
    shutil.rmtree(game_dir / "revs")
    fp = save_sync.compute_prefix_saves_fingerprint(old, game_name=GAME)
    meta = {"game_name": GAME, "updated_at": 1_700_000_000.0, "file_count": 1, "fingerprint_digest": fp.digest,
            "sha256": hashlib.sha256((game_dir / "latest.zip").read_bytes()).hexdigest()}
    (game_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    legacy_bytes = (game_dir / "latest.zip").read_bytes()

    new = tmp_path / "new_client"
    new.mkdir()
    assert _action(new, root).action == "PULL"
    pull_game_saves(GAME, new, root, snapshots_root=tmp_path / "snaps")
    assert (new / REL).read_bytes() == b"legacy-v1"
    _save(new, b"new-v2")
    assert _action(new, root).action == "PUSH"
    push_game_saves(GAME, new, root)
    head = repo.scan_game_folder(game_dir).heads()
    assert len(head) == 1 and head[0].parents[0].startswith(repo.LEGACY_PREFIX)
    assert (game_dir / "latest.zip").read_bytes() == legacy_bytes  # originals kept

    # an old client still writes latest.zip: it becomes an independent version, not an overwrite
    data = bytearray(legacy_bytes)
    (game_dir / "latest.zip").write_bytes(bytes(data) + b"")  # same bytes: nothing new
    assert _action(new, root).action == "IN_SYNC"
    with zipfile.ZipFile(game_dir / "latest.zip", "w") as zf:
        zf.writestr("save/users/steamuser/Documents/HollowTest/save.dat", b"old-client-v3")
    meta["sha256"] = hashlib.sha256((game_dir / "latest.zip").read_bytes()).hexdigest()
    meta["fingerprint_digest"] = "f" * 64
    (game_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    item = _action(new, root)
    assert item.action == "CONFLICT" and len(item.heads) == 2


def test_legacy_archive_that_does_not_match_its_meta_is_not_trusted(tmp_path: Path) -> None:
    game_dir = tmp_path / "sync" / "hollow-test"
    game_dir.mkdir(parents=True)
    with zipfile.ZipFile(game_dir / "latest.zip", "w") as zf:
        zf.writestr("save/users/steamuser/Documents/HollowTest/save.dat", b"half")
    (game_dir / "meta.json").write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
    prefix = tmp_path / "pfx"
    _save(prefix, b"mine")
    item = _action(prefix, tmp_path / "sync")
    assert item.action == "CONFLICT" and "could not be verified" in item.reason
    with pytest.raises(RuntimeError):
        pull_game_saves(GAME, prefix, tmp_path / "sync")
    assert (prefix / REL).read_bytes() == b"mine"


def test_a_lost_legacy_parent_is_not_reported_as_still_arriving(tmp_path: Path) -> None:
    state = repo.RepoState()
    rev = repo.Revision(rev_id="a" * 32, path=tmp_path / "x.zip", parents=(repo.LEGACY_PREFIX + "b" * 32,),
                        save_id="s", content_digest="c" * 64, file_count=1, uncompressed_bytes=1, created_at=1.0)
    other = repo.Revision(rev_id="d" * 32, path=tmp_path / "y.zip", parents=(), save_id="s",
                          content_digest="e" * 64, file_count=1, uncompressed_bytes=1, created_at=2.0)
    state.revisions = {rev.rev_id: rev, other.rev_id: other}
    assert not save_sync._ancestry_still_arriving(state, rev.parents)
    assert save_sync._ancestry_still_arriving(state, ["f" * 32])
    action, reason = save_sync._plan_action(state, state.distinct_heads(), True, None, tmp_path, tmp_path, "g")
    assert action == "CONFLICT" and "still arriving" not in reason


def test_cleanup_keeps_heads_recent_ancestors_and_history(two: dict[str, Path]) -> None:
    _start_both(two)  # B synced v1 and stays offline from now on
    for i in range(2, 8):
        _save(two["a"], f"v{i}".encode())
        push_game_saves(GAME, two["a"], two["a_root"])
    game_dir = next(two["a_root"].glob("*/revs")).parent
    before = repo.scan_game_folder(game_dir)
    head = before.heads()[0]
    plan = repo.plan_cleanup(game_dir)
    assert len(plan.remove) == 7 - 1 - repo.KEEP_ANCESTORS  # v1..v4 go; v5, v6 and the head v7 stay
    assert head.rev_id not in {r.rev_id for r in plan.remove}
    freed = repo.apply_cleanup(plan)
    assert freed == plan.freed_bytes > 0
    after = repo.scan_game_folder(game_dir)
    assert set(after.revisions) == set(before.revisions)  # history kept (manifests of removed ones)
    assert sum(1 for r in after.revisions.values() if r.pruned) == len(plan.remove)
    assert [h.rev_id for h in after.heads()] == [head.rev_id]
    assert repo.plan_cleanup(game_dir).remove == ()  # nothing more to do

    # B comes back: its base (v1) was cleaned up, but the history proves v7 continues it
    _transport(two["a_root"], two["b_root"])
    for stale in two["b_root"].rglob("revs/*.zip"):
        if (stale.with_name(stale.stem + repo.PRUNED_SUFFIX)).exists():
            stale.unlink()  # the transport propagates the deletion
    assert _action(two["b"], two["b_root"]).action == "PULL"
    pull_game_saves(GAME, two["b"], two["b_root"], snapshots_root=two["snaps"])
    assert (two["b"] / REL).read_bytes() == b"v7"


def test_cleanup_never_touches_conflicting_branches(two: dict[str, Path]) -> None:
    _start_both(two)
    _save(two["a"], b"A")
    _save(two["b"], b"B")
    push_game_saves(GAME, two["a"], two["a_root"])
    push_game_saves(GAME, two["b"], two["b_root"])
    _transport(two["b_root"], two["a_root"])
    game_dir = next(two["a_root"].glob("*/revs")).parent
    assert repo.plan_cleanup(game_dir).remove == ()  # two heads + their shared parent
    assert _action(two["a"], two["a_root"]).action == "CONFLICT"


def test_renamed_game_keeps_its_shared_history(tmp_path: Path) -> None:
    root = tmp_path / "sync"
    prefix = tmp_path / "pfx"
    game_dir = tmp_path / "Games" / "Game"
    game_dir.mkdir(parents=True)
    _save(prefix, b"v1")
    first = push_game_saves("Old Name", prefix, root, game_dir=game_dir)
    assert first is not None
    target = save_sync.SyncGameTarget("New Name", prefix, game_dir=game_dir)
    item = plan_sync_all([target], root)[0]
    assert item.action == "IN_SYNC" and item.slug == first.parent.parent.name  # same folder, same history
    _save(prefix, b"v2")
    pushed = push_game_saves("New Name", prefix, root, game_dir=game_dir)
    assert pushed is not None and pushed.parent == first.parent


def test_same_name_different_game_is_never_mixed_silently(tmp_path: Path) -> None:
    root = tmp_path / "sync"
    a, b = tmp_path / "a", tmp_path / "b"
    _save(a, b"doom-2016")
    push_game_saves("Doom", a, root, steam_appid=379720)
    b.mkdir()
    item = plan_sync_all([save_sync.SyncGameTarget("Doom", b, steam_appid=2280)], root)[0]
    assert item.action == "CONFLICT" and "379720" in item.reason  # it used to be PULL (wrong game's saves)
    _save(b, b"doom-1993")
    with pytest.raises(repo.SyncRemoteChanged):
        push_game_saves("Doom", b, root, steam_appid=2280)
    # the same game on another device (same AppID) follows normally
    c = tmp_path / "c"
    c.mkdir()
    assert plan_sync_all([save_sync.SyncGameTarget("Doom", c, steam_appid=379720)], root)[0].action == "PULL"


def test_linked_history_does_not_follow_a_replaced_folder(tmp_path: Path) -> None:
    root = tmp_path / "sync"
    a = tmp_path / "a"
    _save(a, b"mine")
    pushed = push_game_saves(GAME, a, root)
    assert pushed is not None
    game_dir = pushed.parent.parent
    shutil.rmtree(game_dir)  # someone else's game with this name takes the folder
    other = tmp_path / "other"
    _save(other, b"theirs")
    push_game_saves(GAME, other, root)
    assert _action(a, root).action == "CONFLICT"
    assert "different save history" in _action(a, root).reason


def test_unchanged_auto_snapshot_writes_nothing(tmp_path: Path) -> None:
    prefix = tmp_path / "pfx"
    _save(prefix, b"slot" * 10_000)
    snaps = tmp_path / "snaps"
    first = save_sync.auto_snapshot(prefix, GAME, "after-play", dest_root=snaps)
    assert first is not None
    with mock.patch.object(save_sync.tempfile, "mkstemp", side_effect=AssertionError("wrote a temp file")):
        assert save_sync.auto_snapshot(prefix, GAME, "after-play", dest_root=snaps) == first
    _save(prefix, b"changed")
    second = save_sync.auto_snapshot(prefix, GAME, "after-play", dest_root=snaps)
    assert second is not None and second != first and len(list(snaps.rglob("*.zip"))) == 2
