"""Every QThread worker must catch exceptions in run(), so a failure never kills the thread silently."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "deckinstall"
MODULES = ["ui_workers.py", "ui_workers_library.py", "ui_workers_maint.py", "ui_workers_adopt.py"]


def _catches_all(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    return t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))


def _run_methods() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for name in MODULES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(isinstance(b, ast.Name) and b.id == "QThread" for b in node.bases):
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run":
                    found.append((f"{name}:{node.name}", item))
    return found


@pytest.mark.parametrize("label,run", _run_methods(), ids=lambda v: v if isinstance(v, str) else "run")
def test_run_catches_exceptions(label: str, run: ast.FunctionDef) -> None:
    """run() = optional guards/returns + a try whose handlers include `except Exception`."""
    body = [n for n in run.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    tries = [n for n in body if isinstance(n, ast.Try)]
    assert tries, f"{label}.run has no try/except"
    assert any(_catches_all(h) for t in tries for h in t.handlers), f"{label}.run does not catch Exception"
    # Nothing that can fail may run outside the try (emit/isinstance/local assignments are fine).
    for node in (n for n in body if not isinstance(n, ast.Try)):
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            fn = call.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name in ("emit", "isinstance"), f"{label}.run calls {name}() outside its try (line {call.lineno})"


def test_byte_size_signals_carry_more_than_2_gib(tmp_path) -> None:
    """A 57 GB game made DirStatsWorker.ready (int, int) raise OverflowError and never fire."""
    from PySide6.QtWidgets import QApplication

    from deckinstall.ui_workers_library import DirStatsWorker
    from deckinstall.ui_workers_maint import MaintDeleteWorker

    app = QApplication.instance() or QApplication([])
    big = 57_388_175_265
    got: list = []
    worker = DirStatsWorker(tmp_path, tmp_path / "x.exe", False)
    worker.ready.connect(lambda count, size: got.append(size))
    worker.ready.emit(1, big)
    maint = MaintDeleteWorker(None, [], False, False)
    maint.done.connect(lambda deleted, freed, *_: got.append(freed))
    maint.done.emit(1, big, [], [], "")
    app.processEvents()
    assert got == [big, big]
