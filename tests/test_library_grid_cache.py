"""Library cover grid: covers dropped by the LRU cache come back; a missing cover is never retried."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deckinstall import ui_library_grid as g  # noqa: E402


def _wait(app: QApplication, cond, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def _is_cover(model: g.LibraryGridModel, row: int) -> bool:
    key = model.entry_at(row).key  # type: ignore[union-attr]
    ck = model._resolved_cache_key.get(key)
    return ck is not None and ck in g._cache


def test_evicted_covers_reload_once_and_missing_ones_are_not_retried(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    n = g._MAX_PIXMAP_CACHE + 44
    covers: dict[str, Path] = {}
    for i in range(n):
        img = QImage(20, 30, QImage.Format.Format_RGB32)
        img.fill(QColor(i % 256, 50, 50))
        path = tmp_path / f"{i}.png"
        img.save(str(path))
        covers[f"k{i}"] = path
    no_cover = "k-none"
    entries = [g._GridEntry(f"k{i}", f"Game {i}", "", "standalone", object()) for i in range(n)]
    entries.append(g._GridEntry(no_cover, "No Cover", "", "standalone", object()))
    loads: list[str] = []

    def fake_cover_path(_kind: str, _obj: object, _paths: object, key: str = "") -> Path | None:
        loads.append(key)
        return covers.get(key)

    g._cache.clear()
    model = g.LibraryGridModel()
    with mock.patch.object(g, "cover_path", fake_cover_path):
        model.set_entries(entries, None)
        role = Qt.ItemDataRole.DecorationRole
        for row in range(n + 1):  # scroll to the end...
            model.data(model.index(row, 0), role)
            _wait(app, lambda: not model._pending)
        assert len(g._cache) == g._MAX_PIXMAP_CACHE
        assert not _is_cover(model, 0)  # the first covers were dropped from the cache
        first_round = len(loads)
        for row in range(10):  # ...and back to the top
            model.data(model.index(row, 0), role)
            model.data(model.index(row, 0), role)  # a repaint while loading schedules nothing new
        _wait(app, lambda: not model._pending)
        for row in range(10):
            assert _is_cover(model, row), row
        assert len(loads) == first_round + 10  # each dropped cover loaded exactly once more
        model.data(model.index(n, 0), role)  # the game without a cover
        _wait(app, lambda: not model._pending)
        assert loads.count(no_cover) == 1

        # results of an older list (generation) are ignored
        model.set_entries(entries[:5], None)
        stale = model._generation - 1
        model._on_cover_loaded(stale, "k1", ("x", 1), QImage(4, 4, QImage.Format.Format_RGB32))
        assert "k1" not in model._resolved_cache_key
    g._cache.clear()
