"""Tests for the declarative cleanup rule registry and a few rule finders."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import cleanup, library, runtime_manager  # noqa: E402
from deckinstall.cleanup import (  # noqa: E402
    GROUP_DEFAULT_CHECKED,
    GROUP_ORDER,
    RULES,
    CleanupCandidate,
    find_cleanup_candidates,
    is_default_checked,
)


def _scan(home: Path, roots: list[Path] | None = None, deep: bool = True) -> list[CleanupCandidate]:
    with mock.patch.object(runtime_manager, "list_ge_proton_dirs", return_value=[]):
        return find_cleanup_candidates(
            None, extra_external_roots=roots or [], include_system_caches=deep, home_dir=home
        )


def test_registry_covers_every_group_and_system_is_unchecked() -> None:
    assert set(GROUP_ORDER) == {"games", "runtimes", "launchers", "system"}
    assert {rule.group for rule in RULES} == set(GROUP_ORDER)
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert GROUP_DEFAULT_CHECKED["system"] is False
    for rule in RULES:
        if rule.group == "system":
            assert rule.default_checked is False
    assert all(r.deep for r in RULES if r.group == "runtimes")


def test_default_checked_respects_group() -> None:
    base = dict(label="x", path=Path("/nonexistent"), size_bytes=10, has_saves=False)
    assert is_default_checked(CleanupCandidate(category="Trash", group="system", **base)) is False
    assert is_default_checked(CleanupCandidate(category="Compatdata Órfão", group="games", **base)) is True
    assert is_default_checked(CleanupCandidate(category="Old Proton/Runtime", group="runtimes", **base)) is False
    # Candidates built without a group keep the old per-kind semantics.
    assert is_default_checked(CleanupCandidate(category="Trash", **base)) is True
    unverified = CleanupCandidate(category="Compatdata Órfão", group="games", **{**base, "has_saves": True})
    assert is_default_checked(unverified) is False


def test_ignores_non_game_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mesa = home / ".cache" / "mesa_shader_cache"
    mesa.mkdir(parents=True)
    (mesa / "a.bin").write_bytes(b"x" * 64)
    small_app = home / ".cache" / "small-app"
    small_app.mkdir()
    with (small_app / "c").open("wb") as cache:
        cache.truncate(6 * 1024 * 1024)
    (home / ".cache" / "fontconfig").mkdir()

    found = {c.path: c for c in _scan(home)}
    assert mesa not in found
    assert small_app not in found
    assert home / ".cache" / "fontconfig" not in found

    assert mesa not in {c.path for c in _scan(home, deep=False)}


def test_prefix_roots_split_into_launchers_and_games(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hydra_root = tmp_path / "hydra"
    launcher_pfx = hydra_root / "OldGame"
    (launcher_pfx / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    lib_prefixes = home / "Games" / ".prefixes"
    leftover = lib_prefixes / "RemovedGame"
    (leftover / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (leftover / "drive_c" / "dummy.txt").write_text("x", encoding="utf-8")

    # Default roots: launcher roots first, then the library's own Games/.prefixes.
    with mock.patch.object(cleanup, "external_prefix_roots", return_value=[hydra_root]):
        found = {c.path: c for c in find_cleanup_candidates(None, home_dir=home)}
    assert found[launcher_pfx].group == "launchers"
    assert found[launcher_pfx].category == "Prefixo Externo"
    assert found[leftover].group == "games"


def test_cleanup_preserves_save_backups(tmp_path: Path) -> None:
    import os

    home = tmp_path / "home"
    backups = home / "Backups" / "DeckInstall"
    backups.mkdir(parents=True)
    zips = []
    for idx in range(4):
        z = backups / f"saves-0{idx}.zip"
        z.write_bytes(b"PK")
        os.utime(z, (1_700_000_000 + idx, 1_700_000_000 + idx))
        zips.append(z)
    found = {c.path: c for c in _scan(home, deep=False)}
    assert all(z not in found for z in zips)


def test_unregistered_game_folders_require_proven_safe_paths(tmp_path: Path) -> None:
    from uuid import uuid4

    home = tmp_path / "home"
    lib = library.ensure_library(home / library.LIBRARY_DIRNAME, internal=True)
    empty = lib.games_dir / "Empty"
    empty.mkdir()
    unknown = lib.games_dir / "Imported"
    unknown.mkdir()
    (unknown / "data").write_bytes(b"game")
    parent = lib.games_dir / "Publisher"
    nested_game = parent / "Registered title"
    nested_game.mkdir(parents=True)
    uid = str(uuid4())
    library.record_game(lib.root, uid, "Registered title", nested_game)
    current_prefix = lib.prefixes_dir / uid
    current_prefix.mkdir()
    orphan = lib.prefixes_dir / str(uuid4())
    orphan.mkdir()
    linked = lib.games_dir / "Symbolic"
    linked.symlink_to(empty, target_is_directory=True)

    candidates = {cand.path: cand for cand in _scan(home)}
    assert candidates[empty].category == "Empty Game Folder"
    assert candidates[unknown].review_only
    assert parent not in candidates
    assert current_prefix not in candidates
    assert linked not in candidates
    assert not is_default_checked(candidates[orphan])

    # A folder that gained files after detection can no longer be deleted.
    (empty / "new_file").write_bytes(b"installed since scanning")
    deleted, _, errors = cleanup.delete_cleanup_candidates_checked(
        candidates=[candidates[empty], candidates[unknown]],
        home_dir=home,
        extra_external_roots=[],
    )
    assert deleted == 0 and len(errors) == 2
    assert (empty / "new_file").is_file() and (unknown / "data").is_file()

    (empty / "new_file").unlink()
    deleted, _, errors = cleanup.delete_cleanup_candidates_checked(
        candidates=[candidates[empty]], home_dir=home, extra_external_roots=[],
    )
    assert deleted == 1 and not errors and not empty.exists()


def test_runtime_cleanup_preserves_manual_and_current_builds(tmp_path: Path) -> None:
    from deckinstall import steam_paths

    tools = runtime_manager.get_compat_tools_dir()
    tools.mkdir()
    builds = [tools / f"GE-Proton11-{version}" for version in range(1, 5)]
    for build in builds:
        build.mkdir()
        (build / "proton").write_bytes(b"release")
    for managed in builds[1:]:
        (managed / ".deckinstall-managed").write_text(managed.name)
    with mock.patch.object(steam_paths, "find_steam_root", return_value=None):
        found = {
            item.path for item in find_cleanup_candidates(include_system_caches=True)
            if item.group == "runtimes"
        }
    assert found == {builds[1], builds[2]}


def test_imported_backup_requires_every_save_and_rechecks_before_removal(tmp_path: Path) -> None:
    from zipfile import ZipFile

    from deckinstall import standalone, steam_vdf

    source = tmp_path / "Game"
    source.mkdir()
    exe = source / "game.exe"
    exe.write_bytes(b"MZ")
    with mock.patch.object(standalone, "resolve_game_icon", return_value=None):
        game = standalone.install_standalone_game("Game", exe, source)
    live = game.prefix_dir / "drive_c" / "users" / "steamuser" / "Documents" / "save"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"first")
    archive, _, _ = steam_vdf.backup_all_saves(None)
    partial = archive.with_name("partial.zip")
    with ZipFile(archive) as original, ZipFile(partial, "w") as out:
        for name in original.namelist():
            out.writestr(name, original.read(name))
        out.writestr("other-backup/save", b"only in the archive")

    assert steam_vdf.save_backup_matches_installed_games(None, archive)
    assert not steam_vdf.save_backup_matches_installed_games(None, partial)
    candidates = {
        item.path: item for item in find_cleanup_candidates()
        if item.category == "Imported Save Backup"
    }
    assert archive in candidates and partial not in candidates
    assert not is_default_checked(candidates[archive])

    live.write_bytes(b"other")
    deleted, _, errors = cleanup.delete_cleanup_candidates_checked(candidates=[candidates[archive]])
    assert deleted == 0 and errors and archive.is_file()
    live.write_bytes(b"first")
    deleted, _, errors = cleanup.delete_cleanup_candidates_checked(candidates=[candidates[archive]])
    assert deleted == 1 and not errors and not archive.exists() and partial.is_file()


def test_cleanup_removes_managed_runtime_workspaces_but_keeps_unreplaced_backups() -> None:
    import tempfile

    base = runtime_manager.get_runtime_dir()
    base.mkdir(parents=True)
    unfinished = Path(tempfile.mkdtemp(dir=base, prefix="deckinstall_umu_"))
    previous = Path(tempfile.mkdtemp(dir=base, prefix=".mangohud_old_"))
    (unfinished / "download").write_bytes(b"package")
    (previous / "working").write_bytes(b"keep")

    found = {candidate.path: candidate for candidate in find_cleanup_candidates(include_system_caches=True)}
    assert found[unfinished].category == "Runtime Residue"
    assert previous not in found
    deleted, _, errors = cleanup.delete_cleanup_candidates_checked(
        candidates=[found[unfinished]], include_system_caches=True,
    )
    assert deleted == 1 and not errors and not unfinished.exists()
    assert previous.is_dir()


def test_restored_snapshot_can_be_removed_only_while_game_files_match() -> None:
    from deckinstall import save_sync

    prefix = Path.home() / "prefix"
    live = prefix / "drive_c" / "users" / "steamuser" / "Documents" / "progress"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"verified")
    archive = save_sync.auto_snapshot(prefix, "Game", "test")
    assert archive is not None
    assert save_sync.restored_snapshot_matches(archive, prefix, None)
    live.write_bytes(b"progress changed")
    assert not save_sync.remove_restored_snapshot(archive, prefix, None)
    assert archive.is_file()
    live.write_bytes(b"verified")
    assert save_sync.remove_restored_snapshot(archive, prefix, None)
    assert not archive.exists() and live.read_bytes() == b"verified"


def test_compat_cleanup_keeps_latest_for_each_build_variant() -> None:
    from deckinstall import steam_paths

    root = runtime_manager.get_compat_tools_dir()
    old, current, another_variant = (
        root / f"proton-cachyos-{build}" for build in ("11.0-2-slr", "11.0-10-slr", "11.0-1-native")
    )
    for tool in (old, current, another_variant):
        tool.mkdir(parents=True)
        (tool / "proton").write_bytes(b"release")
        (tool / ".deckinstall-managed").write_text(tool.name)
    with mock.patch.object(steam_paths, "find_steam_root", return_value=None):
        candidates = {
            item.path: item for item in find_cleanup_candidates(include_system_caches=True)
            if item.category == "Old Proton/Runtime"
        }
        assert set(candidates) == {old}
        deleted, _, errors = cleanup.delete_cleanup_candidates_checked(
            candidates=list(candidates.values()), include_system_caches=True,
        )
    assert deleted == 1 and not errors and not old.exists()
    assert current.is_dir() and another_variant.is_dir()
