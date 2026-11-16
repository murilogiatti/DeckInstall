"""Unit tests for deckinstall.runtime_manager (Phase 5)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deckinstall import runtime_manager as rm
from deckinstall.steam_paths import SteamPaths

# ---------------------------------------------------------------------------
# 1. _update_versions concurrent RMW under _versions_lock()
# ---------------------------------------------------------------------------


def test_update_versions_concurrent_rmw_no_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple threads updating distinct keys in versions.json do not clobber each other."""
    versions_file = tmp_path / "runtime" / "versions.json"
    monkeypatch.setattr(rm, "get_versions_file", lambda: versions_file)

    num_threads = 12
    barrier = threading.Barrier(num_threads)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5)

            def mutator(data: dict) -> None:
                # Small delay widens the race window if locking is broken.
                time.sleep(0.002)
                data[f"key_{idx}"] = f"val_{idx}"

            rm._update_versions(mutator)
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    assert versions_file.is_file()
    final_data = json.loads(versions_file.read_text(encoding="utf-8"))
    for i in range(num_threads):
        assert final_data[f"key_{i}"] == f"val_{i}"


# ---------------------------------------------------------------------------
# 2. get_steamrt4_dir() respects XDG_DATA_HOME
# ---------------------------------------------------------------------------


def test_get_steamrt4_dir_respects_xdg_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_steamrt4_dir uses $XDG_DATA_HOME/umu/steamrt4 when set, falling back to ~/.local/share."""
    custom_xdg = tmp_path / "custom_xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(custom_xdg))
    assert rm.get_steamrt4_dir() == custom_xdg / "umu" / "steamrt4"

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert rm.get_steamrt4_dir() == Path.home() / ".local" / "share" / "umu" / "steamrt4"


# ---------------------------------------------------------------------------
# 3. install_system_package handles subprocess.TimeoutExpired
# ---------------------------------------------------------------------------


def test_install_system_package_timeout_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """install_system_package returns (False, ...) when pkexec times out."""
    monkeypatch.setattr(rm, "get_system_package_install_cmd", lambda _bin: ["apt-get", "install", "-y", "curl"])
    monkeypatch.setattr(rm.shutil, "which", lambda name: "/usr/bin/pkexec" if name == "pkexec" else None)

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["/usr/bin/pkexec", "apt-get"], timeout=300)

    monkeypatch.setattr(rm.subprocess, "run", raise_timeout)

    ok, msg = rm.install_system_package("curl")
    assert ok is False
    assert "timed out" in msg.lower()
    assert "curl" in msg


# ---------------------------------------------------------------------------
# 4. drain_stream_lines
# ---------------------------------------------------------------------------


def _make_fake_proc(poll_results: list[int | None], wait_rc: int = 0) -> MagicMock:
    """Build a mock Popen[bytes] with a fake stdout fileno/close and controllable poll()."""
    stdout = MagicMock()
    stdout.fileno.return_value = 42
    stdout.close.return_value = None

    proc = MagicMock()
    proc.stdout = stdout
    proc.pid = 12345
    poll_iter = iter(poll_results)
    last_poll = poll_results[-1] if poll_results else 0

    def _poll():
        try:
            return next(poll_iter)
        except StopIteration:
            return last_poll

    proc.poll.side_effect = _poll
    proc.wait.return_value = wait_rc
    return proc


def test_drain_stream_lines_handles_blocking_io_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """drain_stream_lines ignores BlockingIOError on os.read and continues draining."""
    proc = _make_fake_proc([None, None, 0], wait_rc=0)
    monkeypatch.setattr(rm.os, "set_blocking", lambda _fd, _flag: None)

    select_calls = 0

    def fake_select(_r, _w, _x, _timeout):
        nonlocal select_calls
        select_calls += 1
        return ([42], [], [])

    read_chunks: list[bytes | Exception] = [
        BlockingIOError("EAGAIN"),
        b"recovered line\n",
        b"",
    ]
    read_iter = iter(read_chunks)

    def fake_read(_fd: int, _n: int) -> bytes:
        item = next(read_iter)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(rm.select, "select", fake_select)
    monkeypatch.setattr(rm.os, "read", fake_read)

    lines: list[str] = []
    rc = rm.drain_stream_lines(proc, None, lines.append, "cancelled")

    assert rc == 0
    assert lines == ["recovered line"]
    assert select_calls >= 2
    proc.stdout.close.assert_called_once()


def test_drain_stream_lines_drains_fast_exiting_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """When proc.poll() is not None and select returns not-ready, remaining bytes are drained."""
    proc = _make_fake_proc([0], wait_rc=0)
    monkeypatch.setattr(rm.os, "set_blocking", lambda _fd, _flag: None)
    monkeypatch.setattr(rm.select, "select", lambda _r, _w, _x, _t: ([], [], []))

    read_chunks: list[bytes | Exception] = [
        b"fast line 1\nfast line 2\n",
        b"trailing partial",
        BlockingIOError("done"),
    ]
    read_iter = iter(read_chunks)

    def fake_read(_fd: int, _n: int) -> bytes:
        item = next(read_iter)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(rm.os, "read", fake_read)

    lines: list[str] = []
    rc = rm.drain_stream_lines(proc, None, lines.append, "cancelled")

    assert rc == 0
    assert lines == ["fast line 1", "fast line 2", "trailing partial"]


def test_drain_stream_lines_cr_progress_and_split_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """drain_stream_lines emits \\r progress updates and decodes split multi-byte UTF-8 sequences."""
    proc = _make_fake_proc([None, None, None, 0], wait_rc=0)
    monkeypatch.setattr(rm.os, "set_blocking", lambda _fd, _flag: None)
    monkeypatch.setattr(rm.select, "select", lambda _r, _w, _x, _t: ([42], [], []))

    # "café 🚀" -> 'é' is b"\xc3\xa9", '🚀' is b"\xf0\x9f\x9a\x80"
    # Split mid-character across chunks and combine with \r progress updates.
    chunks = [
        b"10%\r50%\rcaf\xc3",
        b"\xa9 \xf0\x9f",
        b"\x9a\x80\r100%\n",
        b"",
    ]
    chunk_iter = iter(chunks)
    monkeypatch.setattr(rm.os, "read", lambda _fd, _n: next(chunk_iter))

    lines: list[str] = []
    rc = rm.drain_stream_lines(proc, None, lines.append, "cancelled")

    assert rc == 0
    assert lines == ["10%", "50%", "café 🚀", "100%"]
    assert all("\ufffd" not in line for line in lines)


# ---------------------------------------------------------------------------
# 5. resolve_game_prefix
# ---------------------------------------------------------------------------


def _make_steam_paths(tmp_path: Path) -> SteamPaths:
    steam_root = tmp_path / "Steam"
    userdata = steam_root / "userdata" / "12345"
    config_dir = userdata / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return SteamPaths(
        steam_root=steam_root,
        user_id="12345",
        userdata_dir=userdata,
        shortcuts_vdf=config_dir / "shortcuts.vdf",
        grid_dir=config_dir / "grid",
        config_vdf=steam_root / "config" / "config.vdf",
        compatdata_dir=steam_root / "steamapps" / "compatdata",
    )


def test_resolve_game_prefix_steam_uses_shortcut_proton_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For 'steam', resolve_game_prefix uses get_shortcut_proton when configured."""
    paths = _make_steam_paths(tmp_path)
    configured_proton = paths.steam_root / "compatibilitytools.d" / "GE-Proton9-20"
    configured_proton.mkdir(parents=True)
    (configured_proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    fallback_proton = paths.steam_root / "steamapps" / "common" / "Proton - Experimental"
    fallback_proton.mkdir(parents=True)
    (fallback_proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    import deckinstall.steam_paths as sp
    import deckinstall.steam_vdf as svdf

    monkeypatch.setattr(svdf, "get_shortcut_proton", lambda _cfg, _appid: "GE-Proton9-20")
    monkeypatch.setattr(rm, "get_compat_tools_dir", lambda: paths.steam_root / "compatibilitytools.d")
    monkeypatch.setattr(sp, "find_proton_binary", lambda _root: fallback_proton / "proton")

    prefix_dir, proton_dir = rm.resolve_game_prefix("steam", SimpleNamespace(appid_u32=999999), paths)
    assert prefix_dir == paths.compatdata_dir / "999999" / "pfx"
    assert proton_dir == configured_proton


def test_resolve_game_prefix_steam_falls_back_to_find_proton_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For 'steam', resolve_game_prefix falls back to find_proton_binary when no shortcut proton is set."""
    paths = _make_steam_paths(tmp_path)
    fallback_proton = paths.steam_root / "steamapps" / "common" / "Proton 9.0"
    fallback_proton.mkdir(parents=True)
    (fallback_proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    import deckinstall.steam_paths as sp
    import deckinstall.steam_vdf as svdf

    monkeypatch.setattr(svdf, "get_shortcut_proton", lambda _cfg, _appid: None)
    monkeypatch.setattr(sp, "find_proton_binary", lambda _root: fallback_proton / "proton")

    prefix_dir, proton_dir = rm.resolve_game_prefix("steam", 888888, paths)
    assert prefix_dir == paths.compatdata_dir / "888888" / "pfx"
    assert proton_dir == fallback_proton


def test_resolve_game_prefix_standalone_searches_all_compat_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For 'standalone', resolve_game_prefix resolves proton_name across compat tool dirs, not only GE-Proton11."""
    extra_compat = tmp_path / "extra_compat"
    custom_proton = extra_compat / "Proton-CachyOS-9"
    custom_proton.mkdir(parents=True)
    (custom_proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    game_prefix = tmp_path / "prefixes" / "mygame"
    (game_prefix / "pfx").mkdir(parents=True)

    import deckinstall.steam_paths as sp

    monkeypatch.setenv("STEAM_EXTRA_COMPAT_TOOLS_PATHS", str(extra_compat))
    monkeypatch.setattr(rm, "get_compat_tools_dir", lambda: tmp_path / "empty_compat")
    monkeypatch.setattr(sp, "find_steam_root", lambda: None)
    monkeypatch.setattr(rm, "list_ge_proton11_dirs", lambda: [])

    game = SimpleNamespace(prefix_dir=game_prefix, proton_name="Proton-CachyOS-9")
    resolved_prefix, resolved_proton = rm.resolve_game_prefix("standalone", game, None)

    assert resolved_prefix == game_prefix / "pfx"
    assert resolved_proton == custom_proton


# ---------------------------------------------------------------------------
# 6. _request_github_with_validated_redirects & _download_file
# ---------------------------------------------------------------------------


def _make_response(status_code: int, headers: dict[str, str] | None = None, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = status_code in (301, 302, 303, 307, 308)
    resp.headers = headers or {}
    resp.iter_content.return_value = [content] if content else []
    resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


@pytest.mark.parametrize(
    "bad_location",
    [
        "https://evil.example.com/payload.tar.gz",
        "http://github.com/owner/repo/releases/download/v1/payload.tar.gz",
    ],
)
def test_request_github_rejects_disallowed_redirects(
    bad_location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_request_github_with_validated_redirects rejects non-GitHub or http:// redirect targets."""
    redir_resp = _make_response(302, {"Location": bad_location})
    monkeypatch.setattr(rm.requests, "get", lambda *_a, **_kw: redir_resp)

    with pytest.raises(ValueError, match="Disallowed URL"):
        rm._request_github_with_validated_redirects(
            "https://github.com/owner/repo/releases/latest/download/app.tar.gz",
            headers={"User-Agent": "DeckInstall"},
            timeout=10,
        )
    redir_resp.close.assert_called_once()


def test_request_github_allows_objects_githubusercontent_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """_request_github_with_validated_redirects allows redirect to objects.githubusercontent.com."""
    calls: list[str] = []
    redir_resp = _make_response(
        302,
        {"Location": "https://objects.githubusercontent.com/github-production-release-asset/123?token=abc"},
    )
    ok_resp = _make_response(200, {"Content-Length": "4"}, b"data")

    def fake_get(url: str, **_kw):
        calls.append(url)
        return redir_resp if len(calls) == 1 else ok_resp

    monkeypatch.setattr(rm.requests, "get", fake_get)

    resp = rm._request_github_with_validated_redirects(
        "https://github.com/owner/repo/releases/download/v1/app.tar.gz",
        headers={"User-Agent": "DeckInstall"},
        timeout=10,
    )
    assert resp is ok_resp
    assert len(calls) == 2
    assert calls[1].startswith("https://objects.githubusercontent.com/")


def test_download_file_rejects_bad_redirect_and_allows_objects_cdn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_download_file rejects non-GitHub redirects and writes content on valid CDN redirects."""
    bad_redir = _make_response(302, {"Location": "https://attacker.invalid/file.tar"})
    monkeypatch.setattr(rm.requests, "get", lambda *_a, **_kw: bad_redir)

    bad_dest = tmp_path / "bad.tar"
    with pytest.raises(ValueError, match="Disallowed URL"):
        rm._download_file("https://github.com/owner/repo/releases/download/v1/bad.tar", bad_dest, "bad")
    assert not bad_dest.exists()

    good_redir = _make_response(302, {"Location": "https://objects.githubusercontent.com/asset/good.tar"})
    good_ok = _make_response(200, {"Content-Length": "11"}, b"hello world")
    seq = iter([good_redir, good_ok])
    monkeypatch.setattr(rm.requests, "get", lambda *_a, **_kw: next(seq))

    good_dest = tmp_path / "good.tar"
    rm._download_file("https://github.com/owner/repo/releases/download/v1/good.tar", good_dest, "good")
    assert good_dest.read_bytes() == b"hello world"


# ---------------------------------------------------------------------------
# 7. ensure_umu_run & ensure_optiscaler fail on digest/size mismatch before extracting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mismatch_kind", ["digest", "size"])
def test_ensure_umu_run_fails_on_integrity_mismatch_before_extract(
    mismatch_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_umu_run raises RuntimeError on SHA-256 digest or size mismatch before calling _safe_extract_tar."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    monkeypatch.setattr(rm, "get_runtime_dir", lambda: runtime_dir)
    monkeypatch.setattr(rm, "get_umu_dir", lambda: runtime_dir / "umu")
    monkeypatch.setattr(rm, "get_umu_run_path", lambda: runtime_dir / "umu" / "umu-run")

    payload = b"fake-umu-tar-payload"
    valid_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    asset = {
        "name": "umu-launcher-1.0-zipapp.tar",
        "browser_download_url": "https://github.com/Open-Wine-Components/umu-launcher/releases/download/1.0/umu-launcher-1.0-zipapp.tar",
        "size": len(payload) if mismatch_kind == "digest" else len(payload) + 100,
        "digest": ("sha256:" + "0" * 64) if mismatch_kind == "digest" else valid_digest,
    }
    monkeypatch.setattr(rm, "_fetch_json", lambda _url: {"tag_name": "1.0", "assets": [asset]})
    monkeypatch.setattr(
        rm,
        "_download_file",
        lambda _url, dest, _label, progress_cb=None: dest.write_bytes(payload),
    )

    extract_called = False

    def fail_if_extracted(*_args, **_kwargs):
        nonlocal extract_called
        extract_called = True
        raise AssertionError("_safe_extract_tar must not be called on integrity failure")

    monkeypatch.setattr(rm, "_safe_extract_tar", fail_if_extracted)

    with pytest.raises(RuntimeError, match="mismatch"):
        rm.ensure_umu_run(force_update=True)
    assert extract_called is False


@pytest.mark.parametrize("mismatch_kind", ["digest", "size"])
def test_ensure_optiscaler_fails_on_integrity_mismatch_before_extract(
    mismatch_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_optiscaler raises RuntimeError on SHA-256 digest or size mismatch before extracting."""
    opti_dir = tmp_path / "runtime" / "optiscaler" / "current"
    monkeypatch.setattr(rm, "get_optiscaler_dir", lambda: opti_dir)

    payload = b"fake-optiscaler-zip-payload"
    valid_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    asset = {
        "name": "OptiScaler_v0.7.zip",
        "browser_download_url": "https://github.com/optiscaler/OptiScaler/releases/download/v0.7/OptiScaler_v0.7.zip",
        "size": len(payload) if mismatch_kind == "digest" else len(payload) + 50,
        "digest": ("sha256:" + "f" * 64) if mismatch_kind == "digest" else valid_digest,
    }
    monkeypatch.setattr(rm, "_fetch_json", lambda _url: {"tag_name": "v0.7", "assets": [asset]})
    monkeypatch.setattr(
        rm,
        "_download_file",
        lambda _url, dest, _label, progress_cb=None: dest.write_bytes(payload),
    )

    import deckinstall.extractor as extractor

    extract_called = False

    def fail_if_extracted(*_args, **_kwargs):
        nonlocal extract_called
        extract_called = True
        raise AssertionError("extract_archive_to_dir must not be called on integrity failure")

    monkeypatch.setattr(extractor, "extract_archive_to_dir", fail_if_extracted)

    with pytest.raises(RuntimeError, match="mismatch"):
        rm.ensure_optiscaler(force_update=True)
    assert extract_called is False


# ---------------------------------------------------------------------------
# 8. fix_bwrap_userns
# ---------------------------------------------------------------------------


def test_fix_bwrap_userns_unconfirmed_does_not_invoke_pkexec(monkeypatch: pytest.MonkeyPatch) -> None:
    """fix_bwrap_userns(confirmed=False) returns (False, ...) without invoking pkexec."""
    run_mock = MagicMock()
    monkeypatch.setattr(rm.subprocess, "run", run_mock)

    ok, msg = rm.fix_bwrap_userns(confirmed=False)
    assert ok is False
    assert "confirmation" in msg.lower()
    run_mock.assert_not_called()


def test_fix_bwrap_userns_confirmed_omits_sysctl_userns_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """fix_bwrap_userns(confirmed=True) installs an AppArmor bwrap profile and never disables kernel userns restriction."""
    monkeypatch.setattr(rm.shutil, "which", lambda name: "/usr/bin/pkexec" if name == "pkexec" else None)

    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        captured_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rm.subprocess, "run", fake_run)

    ok, msg = rm.fix_bwrap_userns(confirmed=True)
    assert ok is True
    assert msg == "OK"
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[:3] == ["/usr/bin/pkexec", "sh", "-c"]
    script = cmd[3]
    assert "/etc/apparmor.d/bwrap" in script
    assert "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" not in script


def test_install_compat_tool_verifies_sha256_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_compat_tool verifies ProtonRelease.sha256_digest and raises RuntimeError on mismatch."""
    compat_dir = tmp_path / "compatibilitytools.d"
    monkeypatch.setattr(rm, "get_compat_tools_dir", lambda: compat_dir)

    def fake_download(_url: str, dest: Path, _label: str, **_kwargs) -> None:
        dest.write_bytes(b"not-the-expected-tarball")

    monkeypatch.setattr(rm, "_download_file", fake_download)

    release = rm.ProtonRelease(
        family_id="ge-proton",
        tag_name="GE-Proton10-1",
        name="GE-Proton10-1",
        asset_name="GE-Proton10-1.tar.gz",
        download_url="https://example.invalid/GE-Proton10-1.tar.gz",
        checksum_url=None,
        sha256_digest="0" * 64,
        size_bytes=len(b"not-the-expected-tarball"),
    )

    with pytest.raises(RuntimeError, match="SHA-256"):
        rm.install_compat_tool(release)


def test_verify_release_asset_integrity_requires_checksum_when_flag_set(tmp_path: Path) -> None:
    """_verify_release_asset_integrity(..., require_checksum=True) raises RuntimeError without checksum."""
    downloaded = tmp_path / "asset.tar.gz"
    downloaded.write_bytes(b"payload")
    release: dict[str, object] = {"assets": []}
    asset: dict[str, object] = {"name": "asset.tar.gz", "size": len(b"payload")}

    with pytest.raises(RuntimeError, match="No checksum metadata"):
        rm._verify_release_asset_integrity(
            release,
            asset,
            downloaded,
            tmp_path,
            label="test-asset",
            require_checksum=True,
        )


def test_resolve_game_prefix_resolves_builtin_steam_proton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_game_prefix('steam', appid, paths) resolves built-in proton_10 to Proton 10.0."""
    from deckinstall import steam_paths, steam_vdf

    steam_root = tmp_path / "Steam"
    lib_root = tmp_path / "ExternalLib"
    proton_dir = lib_root / "steamapps" / "common" / "Proton 10.0"
    proton_dir.mkdir(parents=True)
    (proton_dir / "proton").write_text("#!/bin/sh\n", encoding="utf-8")

    compat_dir = steam_root / "steamapps" / "compatdata"
    compat_dir.mkdir(parents=True)
    config_vdf = steam_root / "config" / "config.vdf"
    config_vdf.parent.mkdir(parents=True)
    config_vdf.write_text("", encoding="utf-8")

    paths = SteamPaths(
        steam_root=steam_root,
        user_id="12345",
        userdata_dir=steam_root / "userdata" / "12345",
        shortcuts_vdf=steam_root / "userdata" / "12345" / "config" / "shortcuts.vdf",
        grid_dir=steam_root / "userdata" / "12345" / "config" / "grid",
        config_vdf=config_vdf,
        compatdata_dir=compat_dir,
    )

    monkeypatch.setattr(steam_vdf, "get_shortcut_proton", lambda _cfg, _appid: "proton_10")
    monkeypatch.setattr(rm, "_resolve_proton_name_in_compat_dirs", lambda _name, _root=None: None)
    monkeypatch.setattr(steam_paths, "_list_steam_library_roots", lambda _root: [steam_root, lib_root])

    prefix_dir, resolved_proton = rm.resolve_game_prefix("steam", 999001, paths)
    assert prefix_dir == compat_dir / "999001" / "pfx"
    assert resolved_proton == proton_dir
