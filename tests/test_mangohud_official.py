"""MangoHud always comes from the official GitHub build, installed per user (no root, any distro)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from deckinstall import runtime_manager as rm


def _layer(name: str, lib: str) -> bytes:
    return json.dumps(
        {"file_format_version": "1.0.0", "layer": {"name": name, "library_path": f"/usr/lib/mangohud/{lib}/libMangoHud.so"}}
    ).encode()


def _release_tarball(path: Path) -> None:
    """MangoHud-x.tar.gz -> MangoHud/MangoHud-package.tar -> ./usr/..., like the upstream release."""
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as tf:
        files = {
            "./usr/lib/mangohud/lib64/libMangoHud.so": b"ELF64",
            "./usr/lib/mangohud/lib32/libMangoHud.so": b"ELF32",
            "./usr/share/vulkan/implicit_layer.d/MangoHud.x86_64.json": _layer("VK_LAYER_MANGOHUD_overlay_x86_64", "lib64"),
            "./usr/share/vulkan/implicit_layer.d/MangoHud.x86.json": _layer("VK_LAYER_MANGOHUD_overlay_x86", "lib32"),
        }
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("MangoHud/MangoHud-package.tar")
        info.size = len(inner.getvalue())
        tf.addfile(info, io.BytesIO(inner.getvalue()))


@pytest.fixture()
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    tarball = tmp_path / "MangoHud-0.8.4.r0.g992103e.tar.gz"
    _release_tarball(tarball)
    assets = [
        {"name": "MangoHud-v0.8.4-Source-DFSG.tar.xz", "browser_download_url": "https://github.com/x/dfsg"},
        {"name": tarball.name, "browser_download_url": "https://github.com/x/bin", "size": tarball.stat().st_size,
         "digest": "sha256:" + hashlib.sha256(tarball.read_bytes()).hexdigest()},  # GitHub's asset digest
    ]
    monkeypatch.setattr(rm, "_fetch_json", lambda url, timeout=20: {"tag_name": "v0.8.4", "assets": assets})
    monkeypatch.setattr(rm, "_download_file", lambda url, dest, label, cb=None: dest.write_bytes(tarball.read_bytes()))
    return tarball


def test_installs_official_build_as_user_layers(release: Path) -> None:
    assert not rm.official_mangohud_installed()
    mh = rm.ensure_mangohud()
    assert (mh / "lib64" / "libMangoHud.so").read_bytes() == b"ELF64"
    assert (mh / "lib32" / "libMangoHud.so").read_bytes() == b"ELF32"
    layer_dir = Path.home() / ".local" / "share" / "vulkan" / "implicit_layer.d"
    x64 = json.loads((layer_dir / "MangoHud.x86_64.json").read_text())
    assert x64["layer"]["name"] == "VK_LAYER_MANGOHUD_overlay_x86_64"  # same name: wins over a distro layer
    assert x64["layer"]["library_path"] == str(mh / "lib64" / "libMangoHud.so")
    assert rm.official_mangohud_installed()
    assert rm._load_versions()["mangohud_installed_version"] == "v0.8.4"


def test_already_installed_only_rewrites_layers(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rm.ensure_mangohud()
    (Path.home() / ".local/share/vulkan/implicit_layer.d/MangoHud.x86.json").unlink()
    monkeypatch.setattr(rm, "_fetch_json", lambda *a, **k: pytest.fail("no network when already installed"))
    rm.ensure_mangohud()
    assert rm.official_mangohud_installed()


def test_incomplete_install_is_repaired_from_the_release(release: Path) -> None:
    mh = rm.ensure_mangohud()
    (mh / "lib32" / "libMangoHud.so").unlink()
    (mh / "layers" / "MangoHud.x86.json").unlink()
    assert rm.ensure_mangohud() == mh
    assert rm.official_mangohud_installed()


def test_failed_layer_update_restores_runtime_and_both_manifests(
    release: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mh = rm.ensure_mangohud()
    lib = mh / "lib64" / "libMangoHud.so"
    lib.write_bytes(b"previous-version")
    layers = rm.get_user_vulkan_layer_dir()
    x64 = layers / "MangoHud.x86_64.json"
    x86 = layers / "MangoHud.x86.json"
    x64.write_bytes(x64.read_bytes() + b"\n")
    expected = {p: p.read_bytes() for p in (x64, x86)}
    write = rm.atomic_write_text

    def fail_second_layer(path: Path, data: str, **kwargs: object) -> None:
        if path == x86:
            raise OSError("cannot update layer")
        write(path, data, **kwargs)

    monkeypatch.setattr(rm, "atomic_write_text", fail_second_layer)
    with pytest.raises(OSError, match="cannot update layer"):
        rm.ensure_mangohud(force_update=True)
    assert lib.read_bytes() == b"previous-version"
    assert all(p.read_bytes() == original for p, original in expected.items())


def test_fresh_mangohud_version_cache_avoids_release_lookup(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = rm.mangohud_versions()
    monkeypatch.setattr(rm, "_fetch_json", lambda *a, **k: pytest.fail("cache should supply the release tag"))
    assert rm.mangohud_versions() == first


def test_layer_pointing_elsewhere_is_not_official(release: Path) -> None:
    rm.ensure_mangohud()
    path = Path.home() / ".local/share/vulkan/implicit_layer.d/MangoHud.x86_64.json"
    data = json.loads(path.read_text())
    data["layer"]["library_path"] = "/usr/lib/x86_64-linux-gnu/mangohud/libMangoHud.so"
    path.write_text(json.dumps(data))
    assert not rm.official_mangohud_installed()


def test_never_from_the_distro_package_manager() -> None:
    assert rm.get_system_package_install_cmd("mangohud") is None


def test_versions_and_uninstall_keep_a_foreign_layer(release: Path) -> None:
    assert rm.mangohud_versions() == ("", "v0.8.4")
    rm.ensure_mangohud()
    assert rm.mangohud_versions() == ("v0.8.4", "v0.8.4")
    layer_dir = Path.home() / ".local/share/vulkan/implicit_layer.d"
    foreign = json.loads((layer_dir / "MangoHud.x86.json").read_text())
    foreign["layer"]["library_path"] = "/usr/lib/mangohud/lib32/libMangoHud.so"  # someone else's layer
    (layer_dir / "MangoHud.x86.json").write_text(json.dumps(foreign))
    assert rm.uninstall_mangohud()
    assert not rm.get_mangohud_dir().exists()
    assert not (layer_dir / "MangoHud.x86_64.json").exists()
    assert (layer_dir / "MangoHud.x86.json").exists()
    assert "mangohud_installed_version" not in rm._load_versions()


def test_update_runtimes_installs_mangohud_when_missing(release: Path) -> None:
    from deckinstall.ui_workers import RuntimeUpdateWorker

    worker = RuntimeUpdateWorker()
    assert worker._update_mangohud() == "+MangoHud v0.8.4"
    assert rm.official_mangohud_installed()
    assert worker._update_mangohud() == "MangoHud v0.8.4"  # up to date: nothing downloaded


def test_release_without_any_checksum_is_refused_and_nothing_installed(
    release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = [{"name": release.name, "browser_download_url": "https://github.com/x/bin", "size": release.stat().st_size}]
    monkeypatch.setattr(rm, "_fetch_json", lambda url, timeout=20: {"tag_name": "v0.8.4", "assets": assets})
    with pytest.raises(RuntimeError, match="No checksum published"):
        rm.ensure_mangohud()
    assert not rm.official_mangohud_installed()
