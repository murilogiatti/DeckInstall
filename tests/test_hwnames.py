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
