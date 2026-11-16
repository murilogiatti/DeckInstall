"""Unit tests for safe archive inspection and extraction in deckinstall.extractor."""

from __future__ import annotations

import io
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import extractor  # noqa: E402


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cmd"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestPrecheckCliArchiveTar:
    """Tests for _precheck_cli_archive('tar', ...)."""

    @pytest.mark.parametrize(
        "listing_line",
        [
            "lrwxrwxrwx user/group 0 2026-01-01 00:00 Game/link -> /etc/passwd",
            "hrw-r--r-- user/group 0 2026-01-01 00:00 Game/hard link to Game/other.txt",
            "crw-r--r-- root/root 1,3 2026-01-01 00:00 dev/null",
            "brw-r--r-- root/root 8,0 2026-01-01 00:00 dev/sda",
            "prw-r--r-- user/group 0 2026-01-01 00:00 Game/my_pipe",
            "Drw-r--r-- user/group 0 2026-01-01 00:00 Game/door",
            "-rw-r--r-- user/group 10 2026-01-01 00:00 ../escape.txt",
            "-rw-r--r-- user/group 10 2026-01-01 00:00 Game/../../escape.txt",
            "-rw-r--r-- user/group 10 2026-01-01 00:00 /etc/passwd",
        ],
    )
    def test_tar_rejects_unsafe_entries(self, tmp_path: Path, listing_line: str) -> None:
        archive = tmp_path / "test.tar"
        archive.write_bytes(b"")
        with mock.patch.object(extractor.subprocess, "run", return_value=_completed(stdout=listing_line + "\n")):
            with pytest.raises(RuntimeError):
                extractor._precheck_cli_archive("tar", archive)

    def test_tar_accepts_clean_output(self, tmp_path: Path) -> None:
        archive = tmp_path / "clean.tar"
        archive.write_bytes(b"")
        clean_stdout = (
            "drwxr-xr-x user/group 0 2026-01-01 00:00 Game/\n"
            "-rw-r--r-- user/group 1024 2026-01-01 00:00 Game/game.exe\n"
            "-rw-r--r-- user/group 512 2026-01-01 00:00 Game/data/config.ini\n"
        )
        with mock.patch.object(extractor.subprocess, "run", return_value=_completed(stdout=clean_stdout)):
            extractor._precheck_cli_archive("tar", archive)


class TestPrecheckCliArchiveUnzip:
    """Tests for _precheck_cli_archive('unzip', ...)."""

    def test_unzip_rejects_traversal_path(self, tmp_path: Path) -> None:
        archive = tmp_path / "traversal.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", b"evil")

        with pytest.raises(RuntimeError, match="Unsafe archive member path"):
            extractor._precheck_cli_archive("unzip", archive)

    def test_unzip_rejects_symlink_external_attr(self, tmp_path: Path) -> None:
        archive = tmp_path / "symlink.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("Game/evil_link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, b"/etc/passwd")

        with pytest.raises(RuntimeError, match="Unsafe symlink"):
            extractor._precheck_cli_archive("unzip", archive)

    def test_unzip_accepts_clean_zip(self, tmp_path: Path) -> None:
        archive = tmp_path / "clean.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Game/game.exe", b"MZ")

        extractor._precheck_cli_archive("unzip", archive)


class TestExtractZipSafe:
    """Tests for _extract_zip_safe limits."""

    def test_rejects_exceeding_max_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for i in range(4):
                zf.writestr(f"file_{i}.txt", b"a")

        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_ENTRIES", 3)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="entry count"):
            extractor._extract_zip_safe(archive, dest)

    def test_rejects_exceeding_max_file_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "bigfile.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("big.bin", b"x" * 20)

        monkeypatch.setattr(extractor, "_MAX_ZIP_FILE_BYTES", 10)
        monkeypatch.setattr(extractor, "_MAX_ZIP_TOTAL_BYTES", 100)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="exceeds maximum allowed size"):
            extractor._extract_zip_safe(archive, dest)

    def test_rejects_exceeding_max_total_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "bigtotal.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.bin", b"x" * 8)
            zf.writestr("b.bin", b"y" * 8)

        monkeypatch.setattr(extractor, "_MAX_ZIP_FILE_BYTES", 10)
        monkeypatch.setattr(extractor, "_MAX_ZIP_TOTAL_BYTES", 12)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="total uncompressed size"):
            extractor._extract_zip_safe(archive, dest)


class TestExtractTarSafe:
    """Tests for _extract_tar_safe streaming extraction."""

    def test_skips_symlinks_hardlinks_and_specials(self, tmp_path: Path) -> None:
        archive = tmp_path / "mixed.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            reg = tarfile.TarInfo("Game/good.txt")
            payload = b"hello"
            reg.size = len(payload)
            tf.addfile(reg, io.BytesIO(payload))

            sym = tarfile.TarInfo("Game/sym_link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "good.txt"
            tf.addfile(sym)

            lnk = tarfile.TarInfo("Game/hard_link")
            lnk.type = tarfile.LNKTYPE
            lnk.linkname = "Game/good.txt"
            tf.addfile(lnk)

            fifo = tarfile.TarInfo("Game/special_fifo")
            fifo.type = tarfile.FIFOTYPE
            tf.addfile(fifo)

        dest = tmp_path / "out"
        dest.mkdir()
        extractor._extract_tar_safe(archive, dest)

        assert (dest / "Game" / "good.txt").read_bytes() == b"hello"
        assert not (dest / "Game" / "sym_link").exists()
        assert not (dest / "Game" / "sym_link").is_symlink()
        assert not (dest / "Game" / "hard_link").exists()
        assert not (dest / "Game" / "special_fifo").exists()

    @pytest.mark.parametrize("bad_name", ["../escape.txt", "Game/../../escape.txt", "/abs/escape.txt"])
    def test_rejects_traversal_and_absolute_paths(self, tmp_path: Path, bad_name: str) -> None:
        archive = tmp_path / "bad_path.tar"
        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo(bad_name)
            payload = b"evil"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="Unsafe archive member path"):
            extractor._extract_tar_safe(archive, dest)

    def test_rejects_escape_resolved_outside_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "escape.tar"
        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo("Game/file.txt")
            payload = b"data"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        dest = tmp_path / "out"
        dest.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # Create a symlink inside dest so (dest_resolved / "Game/file.txt").resolve() escapes dest_resolved
        (dest / "Game").symlink_to(outside)

        with pytest.raises(RuntimeError, match="escapes destination"):
            extractor._extract_tar_safe(archive, dest)

    def test_rejects_exceeding_max_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "many.tar"
        with tarfile.open(archive, "w") as tf:
            for i in range(3):
                info = tarfile.TarInfo(f"f{i}.txt")
                info.size = 1
                tf.addfile(info, io.BytesIO(b"a"))

        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_ENTRIES", 2)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="entry count"):
            extractor._extract_tar_safe(archive, dest)

    def test_rejects_exceeding_max_total_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "big.tar"
        with tarfile.open(archive, "w") as tf:
            for i in range(2):
                info = tarfile.TarInfo(f"f{i}.bin")
                data = b"x" * 6
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_TOTAL_BYTES", 10)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError, match="total uncompressed size"):
            extractor._extract_tar_safe(archive, dest)


class TestExtractArchiveToDirStaging:
    """Tests for extract_archive_to_dir staging cleanup on CLI failure or timeout."""

    def test_cli_failure_leaves_dest_clean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "game.7z"
        archive.write_bytes(b"7z-dummy")
        dest_dir = tmp_path / "dest"

        monkeypatch.setattr(extractor, "_find_7z_bin", lambda: "7z")
        monkeypatch.setattr(extractor, "_precheck_cli_archive", lambda _bin, _arc: None)

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            out_arg = next(arg for arg in cmd if arg.startswith("-o"))
            stage_dir = Path(out_arg[2:])
            (stage_dir / "partial.bin").write_bytes(b"partial")
            (stage_dir / "evil_link").symlink_to("/etc/passwd")
            return _completed(stderr="Data error", returncode=2)

        monkeypatch.setattr(extractor.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="Failed to extract"):
            extractor.extract_archive_to_dir(archive, dest_dir)

        assert list(dest_dir.iterdir()) == []
        assert not list(tmp_path.glob(".deckinstall_stage_*"))

    def test_cli_timeout_leaves_dest_clean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "game.7z"
        archive.write_bytes(b"7z-dummy")
        dest_dir = tmp_path / "dest"

        monkeypatch.setattr(extractor, "_find_7z_bin", lambda: "7z")
        monkeypatch.setattr(extractor, "_precheck_cli_archive", lambda _bin, _arc: None)

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            out_arg = next(arg for arg in cmd if arg.startswith("-o"))
            stage_dir = Path(out_arg[2:])
            (stage_dir / "partial.bin").write_bytes(b"partial")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

        monkeypatch.setattr(extractor.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="timed out"):
            extractor.extract_archive_to_dir(archive, dest_dir)

        assert list(dest_dir.iterdir()) == []
        assert not list(tmp_path.glob(".deckinstall_stage_*"))


class TestPrecheckSizeAndEntryLimits:
    """Tests for _MAX_ARCHIVE_ENTRIES, _MAX_ARCHIVE_FILE_BYTES, and _MAX_ARCHIVE_TOTAL_BYTES."""

    def _patch_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_ENTRIES", 2)
        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_FILE_BYTES", 100)
        monkeypatch.setattr(extractor, "_MAX_ARCHIVE_TOTAL_BYTES", 150)

    def test_7z_rejects_entry_count_file_size_and_total_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_limits(monkeypatch)
        archive = tmp_path / "test.7z"
        archive.write_bytes(b"7z")

        too_many = "----------\nPath = a.txt\nSize = 1\nPath = b.txt\nSize = 1\nPath = c.txt\nSize = 1\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_many))
        with pytest.raises(RuntimeError, match="maximum allowed entry count"):
            extractor._precheck_7z_archive("7z", archive)

        too_big_file = "----------\nPath = big.bin\nSize = 101\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_file))
        with pytest.raises(RuntimeError, match="maximum allowed size"):
            extractor._precheck_7z_archive("7z", archive)

        too_big_total = "----------\nPath = a.bin\nSize = 80\nPath = b.bin\nSize = 80\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_total))
        with pytest.raises(RuntimeError, match="maximum allowed total"):
            extractor._precheck_7z_archive("7z", archive)

    def test_unrar_rejects_entry_count_file_size_and_total_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_limits(monkeypatch)
        archive = tmp_path / "test.rar"
        archive.write_bytes(b"rar")

        too_many = "Name: a.txt\nSize: 1\nName: b.txt\nSize: 1\nName: c.txt\nSize: 1\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_many))
        with pytest.raises(RuntimeError, match="maximum allowed entry count"):
            extractor._precheck_unrar_archive("unrar", archive)

        too_big_file = "Name: big.bin\nSize: 101\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_file))
        with pytest.raises(RuntimeError, match="maximum allowed size"):
            extractor._precheck_unrar_archive("unrar", archive)

        too_big_total = "Name: a.bin\nSize: 80\nName: b.bin\nSize: 80\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_total))
        with pytest.raises(RuntimeError, match="maximum allowed total"):
            extractor._precheck_unrar_archive("unrar", archive)

    def test_tar_cli_rejects_entry_count_file_size_and_total_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_limits(monkeypatch)
        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"tar")

        too_many = (
            "-rw-r--r-- u/g 1 2026-01-01 00:00 a.txt\n"
            "-rw-r--r-- u/g 1 2026-01-01 00:00 b.txt\n"
            "-rw-r--r-- u/g 1 2026-01-01 00:00 c.txt\n"
        )
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_many))
        with pytest.raises(RuntimeError, match="maximum allowed entry count"):
            extractor._precheck_tar_cli_archive("tar", archive)

        too_big_file = "-rw-r--r-- u/g 101 2026-01-01 00:00 big.bin\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_file))
        with pytest.raises(RuntimeError, match="maximum allowed size"):
            extractor._precheck_tar_cli_archive("tar", archive)

        too_big_total = (
            "-rw-r--r-- u/g 80 2026-01-01 00:00 a.bin\n"
            "-rw-r--r-- u/g 80 2026-01-01 00:00 b.bin\n"
        )
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_total))
        with pytest.raises(RuntimeError, match="maximum allowed total"):
            extractor._precheck_tar_cli_archive("tar", archive)

    def test_unzip_cli_fallback_rejects_entry_count_file_size_and_total_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_limits(monkeypatch)
        archive = tmp_path / "fallback.zip"
        archive.write_bytes(b"not-a-valid-zip-header")

        too_many = (
            "-rw-r--r-- 2.0 unx 1 b- defN 26-Jan-01 00:00 a.txt\n"
            "-rw-r--r-- 2.0 unx 1 b- defN 26-Jan-01 00:00 b.txt\n"
            "-rw-r--r-- 2.0 unx 1 b- defN 26-Jan-01 00:00 c.txt\n"
        )
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_many))
        with pytest.raises(RuntimeError, match="maximum allowed entry count"):
            extractor._precheck_unzip_archive("unzip", archive)

        too_big_file = "-rw-r--r-- 2.0 unx 101 b- defN 26-Jan-01 00:00 big.bin\n"
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_file))
        with pytest.raises(RuntimeError, match="maximum allowed size"):
            extractor._precheck_unzip_archive("unzip", archive)

        too_big_total = (
            "-rw-r--r-- 2.0 unx 80 b- defN 26-Jan-01 00:00 a.bin\n"
            "-rw-r--r-- 2.0 unx 80 b- defN 26-Jan-01 00:00 b.bin\n"
        )
        monkeypatch.setattr(extractor.subprocess, "run", lambda *_a, **_kw: _completed(stdout=too_big_total))
        with pytest.raises(RuntimeError, match="maximum allowed total"):
            extractor._precheck_unzip_archive("unzip", archive)
