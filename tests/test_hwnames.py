"""Short CPU/GPU names for MangoHud."""

from __future__ import annotations

import pytest

from deckinstall.hwnames import shorten_cpu, shorten_gpu


@pytest.mark.parametrize(
    ("raw", "short"),
    [
        ("AMD Ryzen 5 5600H with Radeon Graphics", "R5-5600H"),
        ("AMD Custom APU 0932", "APU-0932"),
        ("12th Gen Intel(R) Core(TM) i7-12700H", "i7-12700H"),
        ("Intel(R) Core(TM) Ultra 7 155H", "U7-155H"),
    ],
)
def test_cpu(raw: str, short: str) -> None:
    assert shorten_cpu(raw) == short


@pytest.mark.parametrize(
    ("raw", "short"),
    [
        ("GA107M [GeForce RTX 3050 Mobile]", "RTX-3050"),
        ("Navi 23 [Radeon RX 6600M]", "RX-6600M"),
        ("VanGogh [AMD Custom GPU 0405]", "GPU-0405"),
        ("Radeon RX 7900 XT", "RX-7900-XT"),
    ],
)
def test_gpu(raw: str, short: str) -> None:
    assert shorten_gpu(raw) == short


def _drm(tmp_path, nodes) -> str:
    """Fake /sys/class/drm: renderD node -> (PCI address, driver)."""
    for node, (addr, driver) in nodes.items():
        dev = tmp_path / "devices" / addr
        (dev / "drv").mkdir(parents=True)
        (tmp_path / "drivers" / driver).mkdir(parents=True, exist_ok=True)
        (tmp_path / node).mkdir()
        (tmp_path / node / "device").symlink_to(dev)
        (dev / "driver").symlink_to(tmp_path / "drivers" / driver)
    return str(tmp_path)


def test_mangohud_gpus_follow_render_nodes_and_drivers(tmp_path) -> None:
    from deckinstall import hwnames

    root = _drm(tmp_path, {
        "renderD129": ("0000:06:00.0", "amdgpu"),
        "renderD128": ("0000:01:00.0", "nvidia"),
        "renderD130": ("0000:07:00.0", "nouveau"),  # MangoHud skips this driver: no number
    })
    assert hwnames._mangohud_gpus(root) == ["0000:01:00.0", "0000:06:00.0"]


def test_gpu_text_names_every_gpu_in_mangohud_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import hwnames

    monkeypatch.setattr(hwnames, "_gpus", lambda: [
        ("0000:01:00.0", "GA107M [GeForce RTX 3050 Mobile]"),
        ("0000:06:00.0", "Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]"),
    ])
    monkeypatch.setattr(hwnames, "_mangohud_gpus", lambda: ["0000:06:00.0", "0000:01:00.0"])
    hwnames.mangohud_gpu_text.cache_clear()
    assert hwnames.mangohud_gpu_text() == "Radeon-Vega+RTX-3050"  # GPU0 is whatever renderD128 is
    monkeypatch.setattr(hwnames, "_gpus", lambda: [("0000:04:00.0", "VanGogh [AMD Custom GPU 0405]")])
    monkeypatch.setattr(hwnames, "_mangohud_gpus", lambda: ["0000:04:00.0"])
    hwnames.mangohud_gpu_text.cache_clear()
    assert hwnames.mangohud_gpu_text() == "GPU-0405"  # Steam Deck: one GPU
    hwnames.mangohud_gpu_text.cache_clear()


def test_level_2_is_the_full_top_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import hwnames, steam_vdf

    monkeypatch.setattr(hwnames, "cpu_short_name", lambda: "R5-5600H")
    monkeypatch.setattr(hwnames, "mangohud_gpu_text", lambda: "RTX-3050+Radeon-Vega")
    old = "MANGOHUD=1 MANGOHUD_CONFIG=legacy_layout=0,horizontal,hud_compact,gpu_text=RTX-3050,gpu_list=0 %command%"
    opts = steam_vdf.apply_launch_presets(old, False, True, False, False, False, mangohud_level=2)
    assert opts.count("MANGOHUD_CONFIG") == 1 and "gpu_list" not in opts
    cfg = opts.split("MANGOHUD_CONFIG=")[1].split()[0].split(",")
    for key in ("fps", "frame_timing", "gpu_power", "cpu_power", "vram", "ram", "battery", "battery_watt"):
        assert key in cfg
    assert "gpu_text=RTX-3050+Radeon-Vega" in cfg and "cpu_text=R5-5600H" in cfg
