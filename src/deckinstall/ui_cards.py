"""Reusable UI card widgets, layouts, and formatting/parsing helpers for DeckInstall."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QPoint,
    QProcess,
    QProcessEnvironment,
    QRect,
    QSettings,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QLayout,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cleanup import CleanupCandidate, is_default_checked
from .extractor import InsufficientSpaceError
from .i18n import tr
from .launch_presets import parse_presets
from .scanner import is_native_linux_exe
from .steam_paths import is_gamescope_session
from .steam_vdf import _strip_vdf_quotes


def _dialog_parent(win: QWidget) -> QWidget:
    """Keep nested dialogs above the active modal belonging to this window."""
    active = QApplication.activeModalWidget()
    if active is None:
        return win
    parent: QWidget | None = active
    while parent is not None:
        if parent is win:
            return active
        parent = parent.parentWidget()
    return win


def _dialog_guard(dlg: QDialog) -> Callable[[], bool]:
    """Return a callable that stays True while *dlg* has not finished."""
    state = {"open": True}
    dlg.finished.connect(lambda _r: state.__setitem__("open", False))
    return lambda: state["open"]


class _SortableTableItem(QTableWidgetItem):
    """Table item that sorts by a numeric sort_key when provided."""

    def __init__(self, text: str = "", sort_key: int | float | None = None) -> None:
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other: object) -> bool:
        if (
            isinstance(other, _SortableTableItem)
            and self._sort_key is not None
            and other._sort_key is not None
        ):
            return self._sort_key < other._sort_key
        if isinstance(other, QTableWidgetItem):
            return super().__lt__(other)
        return NotImplemented


def get_game_log_path(game_name: str) -> Path:
    """Return the path to the last execution log file for a game."""
    safe = (
        "".join(c if c.isalnum() or c in "._-" else "_" for c in game_name.strip()).strip("_")
        or "game"
    )
    return Path.home() / ".local" / "share" / "DeckInstall" / "logs" / f"{safe}.log"


def _start_detached_process(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    log_file: Path | None = None,
) -> None:
    """Spawn a fire-and-forget process via QProcess.startDetached without Popen leaks."""
    if not cmd:
        raise ValueError("Empty command")
    proc = QProcess()
    proc.setProgram(cmd[0])
    proc.setArguments(cmd[1:])
    if cwd is not None:
        proc.setWorkingDirectory(str(cwd))
    if env is not None:
        qenv = QProcessEnvironment()
        for k, v in env.items():
            qenv.insert(k, v)
        proc.setProcessEnvironment(qenv)
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            proc.setStandardOutputFile(str(log_file))
            proc.setStandardErrorFile(str(log_file))
        except OSError:
            pass
    if not proc.startDetached():
        raise RuntimeError(f"Failed to launch: {cmd[0]}")


def _resolve_asset_path(filename: str) -> Path | None:
    """Locate an asset file across package resources, checkout layout, and AppImage bundles."""
    candidates = [
        Path(__file__).resolve().parent / "assets" / filename,
        Path(__file__).resolve().parent.parent / "assets" / filename,
        Path(__file__).resolve().parent.parent.parent / "assets" / filename,
        Path(sys.executable).resolve().parent / "assets" / filename,
        Path(sys.prefix) / "share" / "deckinstall" / "assets" / filename,
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / filename)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _pick_file_async(
    parent: QWidget,
    title: str,
    start_dir: Path,
    qt_filter: str,
    on_selected: Callable[[str], None],
) -> None:
    """Open a non-blocking QFileDialog file picker (in-window under Gamescope)."""
    dlg = QFileDialog(parent, title, str(start_dir), qt_filter)
    dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, is_gamescope_session())
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.fileSelected.connect(on_selected)
    dlg.finished.connect(dlg.deleteLater)
    dlg.open()


def _pick_dir_async(
    parent: QWidget,
    title: str,
    start_dir: Path,
    on_selected: Callable[[str], None],
) -> None:
    """Open a non-blocking QFileDialog folder picker (in-window under Gamescope)."""
    dlg = QFileDialog(parent, title, str(start_dir))
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, is_gamescope_session())
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.fileSelected.connect(on_selected)
    dlg.finished.connect(dlg.deleteLater)
    dlg.open()


class DropZoneFrame(QFrame):
    """Drag-and-drop card for archive or setup.exe selection."""

    file_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(
                "QFrame#DropZone { border: 2px dashed #3daee9; "
                "background-color: rgba(61, 174, 233, 0.10); }"
            )

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setStyleSheet("")
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.file_dropped.emit(local)


_WINE_PROC_NAMES = frozenset(
    {
        "umu-run",
        "pressure-vessel-wrap",
        "pv-bwrap",
        "srt-bwrap",
        "wineserver",
    }
)


def _is_wine_game_running() -> bool:
    """Return True if a Windows .exe, umu/pressure-vessel wrapper, or wineserver is running."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    uid = os.getuid()
    for p in proc.iterdir():
        if not (p.name.isascii() and p.name.isdigit()):
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            raw_cmd = (p / "cmdline").read_bytes()
            if not raw_cmd:
                continue
            args = [a.decode("utf-8", errors="ignore").lower() for a in raw_cmd.split(b"\x00") if a]
            if not args:
                continue
            if any(a.endswith(".exe") or Path(a).name in _WINE_PROC_NAMES for a in args):
                return True
        except OSError:
            continue
    return False


def _cleanup_candidate_auto_check(cand: CleanupCandidate) -> bool:
    """Return True if a cleanup candidate is safe to auto-select for 1-click clean."""
    return is_default_checked(cand)


def format_size_bytes(num_bytes: int | float) -> str:
    """Format byte count as MB (< 1024 MB) or GB (>= 1024 MB)."""
    size = max(0.0, float(num_bytes))
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 ** 3):.2f} GB"
    return f"{size / (1024 ** 2):.1f} MB"


def _format_worker_error(exc: Exception) -> str:
    """Format worker exceptions, localizing InsufficientSpaceError."""
    if isinstance(exc, InsufficientSpaceError):
        return tr(
            "error.insufficient_space",
            path=str(exc.target_path),
            required=format_size_bytes(exc.required_bytes),
            available=format_size_bytes(exc.available_bytes),
        )
    return str(exc)


def _format_cleanup_candidate_label(cand: CleanupCandidate) -> str:
    """Format a human-readable list label for a CleanupCandidate."""
    size_str = format_size_bytes(cand.size_bytes)
    if cand.saves_verified_migrated:
        save_tag = tr("cleanup.tag_verified", game=cand.matched_game)
    elif cand.has_saves:
        save_tag = tr("cleanup.tag_unverified")
    elif cand.category in ("Prefixo Externo", "external_prefix") and not cand.matched_game:
        save_tag = tr("cleanup.tag_no_steam")
    elif cand.category in (
        "Prefixo Externo",
        "external_prefix",
        "Compatdata Órfão",
        "orphan_compatdata",
    ):
        save_tag = tr("cleanup.tag_no_saves")
    else:
        save_tag = ""
    cat_map = {
        "Prefixo Externo": tr("cleanup.cat_external"),
        "external_prefix": tr("cleanup.cat_external"),
        "Backup": tr("cleanup.cat_backup"),
        "backup": tr("cleanup.cat_backup"),
        "Compatdata Órfão": tr("cleanup.cat_orphan"),
        "orphan_compatdata": tr("cleanup.cat_orphan"),
        "Orphan Shadercache": tr("cleanup.cat_shadercache"),
        "GPU Shader Cache": tr("maint.cat_gpu_shader"),
        "Steam Logs": tr("cleanup.cat_logs"),
        "Trash": tr("cleanup.cat_trash"),
        "App Cache": tr("cleanup.cat_cache"),
        "Old Proton/Runtime": tr("cleanup.cat_runtime"),
        "Orphan Desktop": tr("maint.cat_orphan_desktop"),
    }
    cat_disp = cat_map.get(cand.category, cand.category)
    hint = f" ({cand.game_hint})" if cand.game_hint else ""
    return f"[{cat_disp}] {cand.label}{hint} — {size_str}{save_tag}"


class FlowLayout(QLayout):
    """Responsive wrapping layout so action buttons never clip horizontally."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list = []

    def __del__(self) -> None:
        try:
            while self._items:
                self.takeAt(0)
        except RuntimeError:
            self._items.clear()

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisibleTo(self.parentWidget()):
                continue
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisibleTo(self.parentWidget()):
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing - 1 > eff.right() and line_height > 0:
                x = eff.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


def _make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return card, layout


def _parse_launch_preset_state(opts: str) -> dict[str, object]:
    """Parse known launch preset flags and values from a LaunchOptions string."""
    return parse_presets(opts)


def _resolve_game_exe_path(kind: str, obj: object) -> Path | None:
    """Resolve the executable Path for a standalone, external, or Steam shortcut entry."""
    if kind in ("standalone", "external"):
        exe = getattr(obj, "exe_path", None)
        return exe if isinstance(exe, Path) else (Path(str(exe)) if exe else None)
    raw_exe = _strip_vdf_quotes(str(getattr(obj, "exe", "") or ""))
    return Path(raw_exe) if raw_exe else None


def _is_native_linux_game(kind: str, obj: object) -> bool:
    """Return True if a game targets a native Linux ELF binary."""
    if kind == "standalone" and bool(getattr(obj, "native_linux", False)):
        return True
    if kind == "external" and str(getattr(obj, "runner", "")) == "linux":
        return True
    exe_path = _resolve_game_exe_path(kind, obj)
    return exe_path is not None and exe_path.is_file() and is_native_linux_exe(exe_path)


def _setting_bool(settings: QSettings, key: str, default: bool = False) -> bool:
    """Read a boolean QSettings value robustly across bool/str/int representations."""
    val = settings.value(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return default
