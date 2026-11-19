"""Tests for the declarative cleanup rule registry and a few rule finders."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import cleanup, runtime_manager  # noqa: E402
from deckinstall.cleanup import (  # noqa: E402
    GROUP_DEFAULT_CHECKED,
    GROUP_ORDER,
    RULES,
    CleanupCandidate,
    find_cleanup_candidates,
    is_default_checked,
)


def _scan(home: Path, roots: list[Path] | None = None, deep: bool = True) -> list[CleanupCandidate]:
    with mock.patch.object(runtime_manager, "list_ge_proton11_dirs", return_value=[]):
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


def test_user_cache_rule_groups_as_system(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mesa = home / ".cache" / "mesa_shader_cache"
    mesa.mkdir(parents=True)
    (mesa / "a.bin").write_bytes(b"x" * 64)
    small_app = home / ".cache" / "small-app"
    small_app.mkdir()
    (small_app / "c").write_bytes(b"x" * 64)  # below the 5 MiB threshold
    (home / ".cache" / "fontconfig").mkdir()

    found = {c.path: c for c in _scan(home)}
    assert mesa in found
    assert found[mesa].category == "GPU Shader Cache"
    assert found[mesa].group == "system"
    assert not is_default_checked(found[mesa])
    assert small_app not in found
    assert home / ".cache" / "fontconfig" not in found

    # System caches are only scanned when asked for.
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


def test_old_save_backups_keep_three_newest(tmp_path: Path) -> None:
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
    assert zips[0] in found and found[zips[0]].group == "games"
    assert all(z not in found for z in zips[1:])
