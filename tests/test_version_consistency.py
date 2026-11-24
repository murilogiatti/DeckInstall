"""VERSION, the package version and every version the README shows must agree."""

from __future__ import annotations

import re
from pathlib import Path

from deckinstall import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_package_version_matches_version_file() -> None:
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == __version__


def test_readme_versions_match_version_file() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    current = re.search(r"\*\*Current version:\*\* `([^`]+)`", readme)
    assert current is not None and current.group(1) == version
    shown = set(re.findall(r"DeckInstall-(\d+\.\d+\.\d+)-x86_64\.AppImage", readme))
    tags = set(re.findall(r"\(e\.g\. `v(\d+\.\d+\.\d+)`\)", readme))
    assert shown | tags <= {version}, f"README mentions {sorted(shown | tags)}, VERSION is {version}"
    assert shown, "the Quick Start should show the AppImage file name"
