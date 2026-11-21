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


def test_game_gpu_follows_prime_offload_on_hybrid_laptops(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import hwnames

    hybrid = [
        ("0000:01:00.0", "GA107M [GeForce RTX 3050 Mobile]"),
        ("0000:06:00.0", "Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]"),
    ]
    monkeypatch.setattr(hwnames, "_gpus", lambda: hybrid)
    hwnames.game_gpu.cache_clear()
    assert hwnames.game_gpu(True) == ("RTX-3050", "0000:01:00.0")
    assert hwnames.game_gpu(False) == ("Radeon-Vega", "0000:06:00.0")
    monkeypatch.setattr(hwnames, "_gpus", lambda: [("0000:04:00.0", "VanGogh [AMD Custom GPU 0405]")])
    hwnames.game_gpu.cache_clear()
    assert hwnames.game_gpu(True) == ("GPU-0405", "")  # Steam Deck: one GPU, no pci_dev
    hwnames.game_gpu.cache_clear()


def test_mangohud_config_names_and_pins_the_game_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from deckinstall import hwnames, steam_vdf

    monkeypatch.setattr(hwnames, "cpu_short_name", lambda: "R5-5600H")
    monkeypatch.setattr(hwnames, "game_gpu", lambda prime=False: ("RTX-3050", "0000:01:00.0") if prime else ("Radeon-Vega", "0000:06:00.0"))
    on = steam_vdf.apply_launch_presets("%command%", False, True, False, False, False, mangohud_level=2, prime_offload=True)
    assert "gpu_text=RTX-3050,pci_dev=0000:01:00.0" in on and "__NV_PRIME_RENDER_OFFLOAD=1" in on
    off = steam_vdf.apply_launch_presets(on, False, True, False, False, False, mangohud_level=2, prime_offload=False)
    assert "gpu_text=Radeon-Vega,pci_dev=0000:06:00.0" in off and "RTX-3050" not in off and off.count("MANGOHUD_CONFIG") == 1
