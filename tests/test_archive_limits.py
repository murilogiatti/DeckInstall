"""Fase 2.5: no fixed 4 GiB cap; zip-bomb guard by compression ratio; space by declared size."""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import extractor  # noqa: E402

GIB = 1024**3


def test_big_archives_are_no_longer_capped_at_4_gib(tmp_path: Path) -> None:
    archive = tmp_path / "BigGame.7z"
    with archive.open("wb") as fh:
        fh.truncate(20 * GIB)  # sparse: takes no real space
    per_file, total = extractor._archive_limits(archive)
    assert total == 20 * GIB * extractor.MAX_COMPRESSION_RATIO
    assert per_file == total  # no 1 GiB per-file limit either


def test_split_volumes_count_toward_the_budget(tmp_path: Path) -> None:
    for i in (1, 2, 3):
        (tmp_path / f"Game.part{i}.rar").write_bytes(b"r" * 100)
        (tmp_path / f"Other.7z.00{i}").write_bytes(b"s" * 10)
    (tmp_path / "Game.part1.txt").write_bytes(b"x")
    assert [p.name for p in extractor.archive_parts(tmp_path / "Game.part1.rar")] == [
        "Game.part1.rar", "Game.part2.rar", "Game.part3.rar",
    ]
    assert extractor.archive_set_size(tmp_path / "Game.part1.rar") == 300
    assert extractor.archive_set_size(tmp_path / "Other.7z.001") == 30
    single = tmp_path / "Single.zip"
    single.write_bytes(b"z" * 7)
    assert extractor.archive_parts(single) == [single]


def _zip(path: Path, data: bytes) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.bin", data)
    return path


def test_zip_bomb_is_rejected_by_ratio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "MAX_COMPRESSION_RATIO", 20)
    monkeypatch.setattr(extractor, "_MIN_SIZE_BUDGET", 0)
    bomb = _zip(tmp_path / "bomb.zip", b"\x00" * 200_000)  # ~1000x compression
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError, match="more than 20x"):
        extractor._extract_zip_safe(bomb, out)

    normal = _zip(tmp_path / "normal.zip", os.urandom(50_000))  # incompressible, like game data
    extractor._extract_zip_safe(normal, out)
    assert (out / "data.bin").stat().st_size == 50_000


@pytest.mark.skipif(extractor._find_7z_bin() is None, reason="7z not installed")
def test_real_7z_listing_reports_declared_size(tmp_path: Path) -> None:
    import subprocess

    src = tmp_path / "src" / "Game"
    src.mkdir(parents=True)
    (src / "Game.exe").write_bytes(os.urandom(4096))
    (src / "data.pak").write_bytes(os.urandom(20_000))
    archive = tmp_path / "Game.7z"
    subprocess.run([str(extractor._find_7z_bin()), "a", "-bd", str(archive), str(src)], check=True, capture_output=True)

    declared = extractor._precheck_cli_archive(str(extractor._find_7z_bin()), archive)
    assert declared == 4096 + 20_000
    out = tmp_path / "out"
    extractor.extract_archive_to_dir(archive, out)
    assert (out / "Game" / "data.pak").stat().st_size == 20_000
