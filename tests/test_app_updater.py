"""Unit tests for AppImage self-updating in deckinstall.app_updater."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deckinstall import app_updater  # noqa: E402


def _make_streaming_response(chunks: list[bytes], total_size: int | None = None) -> mock.MagicMock:
    """Build a mock requests.Response supporting context-manager streaming."""
    size = sum(len(c) for c in chunks) if total_size is None else total_size
    resp = mock.MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.headers = {"Content-Length": str(size)}
    resp.iter_content.return_value = iter(chunks)
    return resp


class AppUpdaterTests(unittest.TestCase):
    """Tests for semver parsing, writability, update checks, SHA-256 download, and relaunch."""

    def test_parse_semver(self) -> None:
        self.assertGreater(
            app_updater.parse_semver("v0.8.0"),
            app_updater.parse_semver("0.7.7"),
        )
        self.assertGreater(
            app_updater.parse_semver("0.7.10"),
            app_updater.parse_semver("0.7.7"),
        )
        self.assertEqual(
            app_updater.parse_semver("v0.7.7"),
            app_updater.parse_semver("0.7.7"),
        )

    def test_can_replace_appimage(self) -> None:
        self.assertEqual(app_updater.can_replace_appimage(None), (False, "not_appimage"))

        target = Path("/tmp/DeckInstall.AppImage")
        with mock.patch("deckinstall.app_updater.os.access", return_value=False):
            self.assertEqual(app_updater.can_replace_appimage(target), (False, "not_writable"))

        with mock.patch("deckinstall.app_updater.os.access", return_value=True):
            self.assertEqual(app_updater.can_replace_appimage(target), (True, ""))

    def test_get_running_appimage_accepts_extensionless_and_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            extensionless = base / "DeckInstall"
            extensionless.write_bytes(b"appimage")
            symlink_path = base / "DeckInstallLink"
            symlink_path.symlink_to(extensionless)

            with mock.patch.dict(os.environ, {"APPIMAGE": str(extensionless)}, clear=False):
                self.assertEqual(app_updater.get_running_appimage(), extensionless)

            with mock.patch.dict(os.environ, {"APPIMAGE": str(symlink_path)}, clear=False):
                self.assertIsNone(app_updater.get_running_appimage())

            with mock.patch.dict(os.environ, {"APPIMAGE": "DeckInstall"}, clear=False):
                self.assertIsNone(app_updater.get_running_appimage())

            with mock.patch.dict(os.environ, {"APPIMAGE": str(base / "missing")}, clear=False):
                self.assertIsNone(app_updater.get_running_appimage())

            with mock.patch.dict(os.environ, {"APPIMAGE": ""}, clear=False):
                self.assertIsNone(app_updater.get_running_appimage())

    def test_check_app_update_finds_x86_64_asset_and_digest(self) -> None:
        valid_x64_sha = "a" * 64
        valid_arm_sha = "1" * 64
        release_payload = {
            "tag_name": "v0.9.0",
            "body": "Release notes for 0.9.0",
            "html_url": "https://github.com/murilogiatti/DeckInstall/releases/tag/v0.9.0",
            "assets": [
                {
                    "name": "DeckInstall-0.9.0-aarch64.AppImage",
                    "browser_download_url": "https://github.com/murilogiatti/DeckInstall/releases/download/v0.9.0/aarch64.AppImage",
                    "digest": f"sha256:{valid_arm_sha}",
                },
                {
                    "name": "DeckInstall-0.9.0-x86_64.AppImage",
                    "browser_download_url": "https://objects.githubusercontent.com/DeckInstall-0.9.0-x86_64.AppImage",
                    "digest": f"sha256:{valid_x64_sha}",
                },
            ],
        }
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = release_payload

        with tempfile.TemporaryDirectory() as tmp:
            appimage_path = Path(tmp) / "DeckInstall.AppImage"
            appimage_path.write_bytes(b"current-appimage")

            with (
                mock.patch("deckinstall.app_updater.__version__", "0.8.0"),
                mock.patch("deckinstall.app_updater.requests.get", return_value=mock_resp) as mock_get,
                mock.patch("deckinstall.app_updater.platform.machine", return_value="x86_64"),
                mock.patch.dict(os.environ, {"APPIMAGE": str(appimage_path)}, clear=False),
                mock.patch("deckinstall.app_updater.os.access", return_value=True),
            ):
                info = app_updater.check_app_update()

        mock_get.assert_called_once()
        self.assertEqual(info.current_version, "0.8.0")
        self.assertEqual(info.latest_version, "0.9.0")
        self.assertTrue(info.has_update)
        self.assertEqual(info.asset_name, "DeckInstall-0.9.0-x86_64.AppImage")
        self.assertEqual(
            info.download_url,
            "https://objects.githubusercontent.com/DeckInstall-0.9.0-x86_64.AppImage",
        )
        self.assertEqual(info.expected_sha256, valid_x64_sha)
        self.assertEqual(info.release_notes, "Release notes for 0.9.0")
        self.assertEqual(
            info.html_url,
            "https://github.com/murilogiatti/DeckInstall/releases/tag/v0.9.0",
        )
        self.assertTrue(info.is_appimage)
        self.assertEqual(info.appimage_path, appimage_path)
        self.assertTrue(info.can_self_update)
        self.assertEqual(info.cannot_update_reason, "")

    def test_download_and_replace_appimage_sha256_verify_and_mismatch(self) -> None:
        old_bytes = b"old-appimage-bytes"
        new_chunks = [b"new-appimage-", b"payload-bytes"]
        new_bytes = b"".join(new_chunks)
        expected_sha = hashlib.sha256(new_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            target = tmp_dir / "DeckInstall.AppImage"
            target.write_bytes(old_bytes)
            target.chmod(0o644)

            valid_info = app_updater.AppUpdateInfo(
                current_version="0.8.0",
                latest_version="0.9.0",
                has_update=True,
                download_url="https://github.com/murilogiatti/DeckInstall/releases/download/v0.9.0/DeckInstall-0.9.0-x86_64.AppImage",
                asset_name="DeckInstall-0.9.0-x86_64.AppImage",
                expected_sha256=expected_sha,
                release_notes="",
                html_url=app_updater.GITHUB_RELEASES_URL,
                is_appimage=True,
                appimage_path=target,
                can_self_update=True,
                cannot_update_reason="",
            )

            progress_events: list[int] = []
            valid_resp = _make_streaming_response(new_chunks)
            with mock.patch("deckinstall.app_updater.requests.get", return_value=valid_resp):
                result = app_updater.download_and_replace_appimage(
                    valid_info,
                    progress_cb=progress_events.append,
                )

            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), new_bytes)
            self.assertTrue(stat.S_IMODE(target.stat().st_mode) & stat.S_IXUSR)
            self.assertEqual(progress_events[-1], 100)
            self.assertFalse(any(p.name.endswith(".tmp") for p in tmp_dir.iterdir()))

            # Mismatched SHA-256: must raise, keep existing file untouched, and clean up .tmp.
            target.write_bytes(old_bytes)
            bad_info = app_updater.AppUpdateInfo(
                current_version="0.8.0",
                latest_version="0.9.0",
                has_update=True,
                download_url="https://github.com/murilogiatti/DeckInstall/releases/download/v0.9.0/DeckInstall-0.9.0-x86_64.AppImage",
                asset_name="DeckInstall-0.9.0-x86_64.AppImage",
                expected_sha256="0" * 64,
                release_notes="",
                html_url=app_updater.GITHUB_RELEASES_URL,
                is_appimage=True,
                appimage_path=target,
                can_self_update=True,
                cannot_update_reason="",
            )
            bad_resp = _make_streaming_response(new_chunks)
            with mock.patch("deckinstall.app_updater.requests.get", return_value=bad_resp):
                with self.assertRaises(RuntimeError):
                    app_updater.download_and_replace_appimage(bad_info)

            self.assertEqual(target.read_bytes(), old_bytes)
            self.assertFalse(any(p.name.endswith(".tmp") for p in tmp_dir.iterdir()))

    def test_download_and_replace_appimage_fails_closed_when_sha256_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "DeckInstall.AppImage"
            target.write_bytes(b"existing-appimage")
            info = app_updater.AppUpdateInfo(
                current_version="0.8.0",
                latest_version="0.9.0",
                has_update=True,
                download_url="https://github.com/murilogiatti/DeckInstall/releases/download/v0.9.0/DeckInstall-0.9.0-x86_64.AppImage",
                asset_name="DeckInstall-0.9.0-x86_64.AppImage",
                expected_sha256="",
                release_notes="",
                html_url=app_updater.GITHUB_RELEASES_URL,
                is_appimage=True,
                appimage_path=target,
                can_self_update=True,
                cannot_update_reason="",
            )
            with mock.patch("deckinstall.app_updater.requests.get") as mock_get:
                with self.assertRaises(RuntimeError):
                    app_updater.download_and_replace_appimage(info)
            mock_get.assert_not_called()
            self.assertEqual(target.read_bytes(), b"existing-appimage")

    def test_build_clean_relaunch_env_strips_appimage_vars_and_mount_paths(self) -> None:
        fake_env = {
            "APPIMAGE": "/home/deck/Applications/DeckInstall.AppImage",
            "APPDIR": "/tmp/.mount_Deck123",
            "ARGV0": "./DeckInstall.AppImage",
            "OWD": "/home/deck",
            "PATH": "/tmp/.mount_Deck123/usr/bin:/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/.mount_Deck123/lib:/usr/lib",
            "HOME": "/home/deck",
        }
        with mock.patch.dict(os.environ, fake_env, clear=True):
            cleaned = app_updater.build_clean_relaunch_env()

        for key in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):
            self.assertNotIn(key, cleaned)
        self.assertEqual(cleaned["PATH"], "/usr/bin")
        self.assertEqual(cleaned["LD_LIBRARY_PATH"], "/usr/lib")
        self.assertEqual(cleaned["HOME"], "/home/deck")

    def test_relaunch_appimage_spawns_detached_process(self) -> None:
        target = Path("/home/deck/DeckInstall.AppImage")
        with (
            mock.patch.dict(os.environ, {"APPDIR": "/tmp/.mount_Deck123", "PATH": "/tmp/.mount_Deck123/usr/bin:/usr/bin"}, clear=True),
            mock.patch.object(sys, "argv", ["DeckInstall.AppImage", "--minimized"]),
            mock.patch("deckinstall.app_updater.subprocess.Popen") as mock_popen,
        ):
            app_updater.relaunch_appimage(target)

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], [str(target), "--minimized"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("APPDIR", kwargs["env"])
        self.assertEqual(kwargs["env"]["PATH"], "/usr/bin")


if __name__ == "__main__":
    unittest.main()
