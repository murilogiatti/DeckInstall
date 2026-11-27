"""--doctor report and Steam variants (native / Flatpak, several at once; Snap is unsupported)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deckinstall import doctor, steam_paths


def _make_steam(home: Path, rel: str, logged_in: bool = True) -> Path:
    root = home / rel
    (root / "userdata").mkdir(parents=True)
    if logged_in:
        (root / "userdata" / "12345").mkdir()
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(steam_paths.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(steam_paths.STEAM_ROOT_ENV, raising=False)
    monkeypatch.setattr(steam_paths, "_PROC_ROOT", tmp_path / "proc")
    return tmp_path


def test_variant_of_each_root(home: Path) -> None:
    assert steam_paths.steam_variant(home / ".local/share/Steam") == "native"
    assert steam_paths.steam_variant(home / ".var/app/com.valvesoftware.Steam/.local/share/Steam") == "flatpak"
    assert steam_paths.steam_variant(home / "snap/steam/common/.local/share/Steam") == "snap"
    assert steam_paths.steam_command(home / ".var/app/com.valvesoftware.Steam/.steam/steam")[:2] == ["flatpak", "run"]


def test_snap_steam_is_not_used_but_reported(home: Path) -> None:
    snap = _make_steam(home, "snap/steam/common/.local/share/Steam")
    assert steam_paths.find_steam_roots() == []
    assert steam_paths.find_steam_root() is None
    assert steam_paths.unsupported_snap_steam() == snap
    checks = {c["name"]: c for c in doctor._steam()}
    assert checks["steam_snap"]["status"] == doctor.WARN
    assert "not supported" in checks["steam_snap"]["detail"]


def test_all_steams_listed_once(home: Path) -> None:
    native = _make_steam(home, ".local/share/Steam")
    (home / ".steam").mkdir()
    (home / ".steam" / "steam").symlink_to(native)  # same install seen twice
    flat = _make_steam(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    assert steam_paths.find_steam_roots() == [native, flat]


def test_env_override_picks_the_second_steam(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_steam(home, ".local/share/Steam")
    flat = _make_steam(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    monkeypatch.setenv(steam_paths.STEAM_ROOT_ENV, str(flat))
    assert steam_paths.find_steam_root() == flat


def test_bad_env_override_is_ignored(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    native = _make_steam(home, ".local/share/Steam")
    monkeypatch.setenv(steam_paths.STEAM_ROOT_ENV, str(home / "nowhere"))
    assert steam_paths.find_steam_root() == native


def test_running_steam_wins(home: Path) -> None:
    _make_steam(home, ".local/share/Steam")
    flat = _make_steam(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    proc = home / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"/app/bin/steam\0-silent\0")
    assert steam_paths.find_steam_root() == flat


def test_logged_in_steam_wins_without_running_one(home: Path) -> None:
    _make_steam(home, ".local/share/Steam", logged_in=False)
    flat = _make_steam(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    assert steam_paths.find_steam_root() == flat


def test_doctor_isolates_a_crashing_section(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[dict[str, str]]:
        raise RuntimeError("probe broke")

    monkeypatch.setattr(doctor, "_SECTIONS", (("app", doctor._app), ("gpu", boom)))
    report = doctor.collect_report()
    assert report["app"][0]["detail"].startswith("DeckInstall ")
    assert report["gpu"][0]["status"] == doctor.FAIL
    assert doctor.worst_status(report) == doctor.FAIL
    assert "[FAIL] gpu: check crashed" in doctor.format_text(report)


def test_doctor_json_and_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        doctor, "_SECTIONS", (("x", lambda: [doctor._check("fuse", doctor.WARN, "missing /dev/fuse")]),)
    )
    assert doctor.main(["--doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data == {"x": [{"name": "fuse", "status": "warn", "detail": "missing /dev/fuse"}]}


def test_doctor_fuse_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(doctor.Path, "exists", lambda self: False)
    (check,) = doctor._fuse()
    assert check["status"] == doctor.WARN
    assert "--appimage-extract-and-run" in check["detail"]


def test_doctor_full_report_runs(home: Path) -> None:
    report = doctor.collect_report()
    assert set(report) == {name for name, _ in doctor._SECTIONS}
    for checks in report.values():
        for check in checks:
            assert check["status"] in {doctor.OK, doctor.WARN, doctor.FAIL, doctor.INFO}
    assert report["steam"][0]["detail"].startswith("no Steam installation found")


def test_clean_child_env_drops_appimage_paths() -> None:
    from deckinstall.fsutil import clean_child_env

    env = clean_child_env(
        {
            "APPDIR": "/tmp/.mount_X",
            "APPIMAGE": "/home/u/DeckInstall.AppImage",
            "PATH": "/tmp/.mount_X:/tmp/.mount_X/usr/bin:/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/.mount_X/usr/lib",
            "PYTHONHOME": "/tmp/.mount_X/usr",
            "HOME": "/home/u",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u"}


def test_prime_preset_follows_the_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import launch_presets, runtime_manager

    def profile(nvidia: bool) -> runtime_manager.GpuProfile:
        return runtime_manager.GpuProfile(
            has_nvidia=nvidia, has_amd=True, has_intel=False, is_hybrid=True, is_wayland=False,
            nvidia_driver_present=nvidia,
        )

    monkeypatch.setattr(runtime_manager, "detect_gpu_profile", lambda: profile(False))
    opts = launch_presets.apply_presets("", {"prime": True})
    assert opts == "DRI_PRIME=1 %command%"
    assert launch_presets.parse_presets(opts)["prime"]
    monkeypatch.setattr(runtime_manager, "detect_gpu_profile", lambda: profile(True))
    opts = launch_presets.apply_presets(opts, {"prime": True})
    assert "DRI_PRIME" not in opts and "__NV_PRIME_RENDER_OFFLOAD=1" in opts


def test_debug_log_preset_round_trip() -> None:
    from deckinstall import launch_presets

    opts = launch_presets.apply_presets("%command% -x", {"debug_log": True})
    assert opts == "UMU_LOG=1 PROTON_LOG=1 %command% -x"
    assert launch_presets.active_flags(opts) == ["debug_log"]
    assert launch_presets.apply_presets(opts, {"debug_log": False}) == "%command% -x"


def test_shareable_report_masks_personal_data() -> None:
    words = [("/home/alice", "~"), ("alice", "<user>"), ("Alice Gamer", "<steam account 1>"),
             ("123456", "<steam account 1>"), ("alice-laptop", "<host>")]
    words.sort(key=lambda kv: -len(kv[0]))
    text = (
        "native: /home/alice/.local/share/Steam (2 user(s)) — Alice Gamer (123456), bob (9)\n"
        "host alice-laptop, user alice, size 1234567 bytes, id 76561198000000000, malice=1"
    )
    out = doctor.mask_text(text, words)
    assert "alice" not in out.replace("malice", "") and "Alice" not in out and "123456 " not in out
    assert "~/.local/share/Steam" in out and "<steam account 1>" in out and "<host>" in out
    assert "1234567 bytes" in out  # numbers that only contain an id are kept
    assert "<steamid>" in out and "malice=1" in out


def test_shareable_text_uses_the_live_values(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    report = {"app": [{"name": "home", "status": doctor.INFO, "detail": f"config at {home}/.config"}]}
    out = doctor.shareable_text(report)
    assert str(home) not in out and "~/.config" in out and "masked" in out
