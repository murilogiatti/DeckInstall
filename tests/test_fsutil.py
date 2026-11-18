from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deckinstall import fsutil  # noqa: E402
from deckinstall.steam_paths import read_library_paths, resolve_proton_dir  # noqa: E402


def test_atomic_write_replaces_without_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "f.txt"
    fsutil.atomic_write_text(target, "one")
    fsutil.atomic_write_bytes(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["f.txt"]


def test_atomic_copy_and_sha256(tmp_path: Path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    dst = tmp_path / "out" / "b.bin"
    fsutil.atomic_copy_file(src, dst)
    assert fsutil.file_sha256(dst) == hashlib.sha256(b"data").hexdigest()


def test_is_within_strict_and_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "root"
    child = parent / "x"
    child.mkdir(parents=True)
    link = parent / "link"
    link.symlink_to(child)
    assert fsutil.is_within(child, parent)
    assert fsutil.is_within(parent, parent)
    assert not fsutil.is_within(parent, parent, strict=True)
    assert not fsutil.is_within(link, parent, strict=True)
    assert not fsutil.is_within(tmp_path, parent)


def test_slug_and_sanitize() -> None:
    assert fsutil.sanitize_dir_name("A:B/C") == "A B C"
    assert fsutil.sanitize_dir_name("***") == "Game"
    assert fsutil.slugify("Half-Life 2!") == "half-life-2"


def test_remove_path_or_tree_and_merge_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f").write_text("x")
    dst = tmp_path / "dst"
    (dst / "d").mkdir(parents=True)
    fsutil.copy_or_move_dir_contents(src, dst, move=False)
    assert (dst / "d" / "f").read_text() == "x"
    fsutil.remove_path_or_tree(dst)
    assert not dst.exists()


def test_read_library_paths_and_resolve_proton(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    lib = tmp_path / "lib2"
    (steam / "steamapps").mkdir(parents=True)
    proton = lib / "steamapps" / "common" / "Proton 9.0"
    proton.mkdir(parents=True)
    (proton / "proton").write_text("")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n"0"\n{{\n"path" "{steam}"\n}}\n"1"\n{{\n"path" "{lib}"\n}}\n}}\n'
    )
    assert read_library_paths(steam) == [steam, lib]
    assert resolve_proton_dir("proton_9", steam) == proton
    assert resolve_proton_dir("nope_1", steam) is None
