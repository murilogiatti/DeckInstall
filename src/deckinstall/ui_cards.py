"""Reusable UI card widgets, layouts, and formatting/parsing helpers for DeckInstall."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
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
    dlg.destroyed.connect(lambda: state.__setitem__("open", False))
    return lambda: state["open"]


class _BusyGuardedDialog(QDialog):
    def __init__(self, parent: QWidget, is_busy: Callable[[], bool]) -> None:
        super().__init__(parent)
        self._is_busy = is_busy

    def reject(self) -> None:
        if not self._is_busy():
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_busy():
            event.ignore()
        else:
            super().closeEvent(event)


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


def clamp_dialog_size(
    dialog: QDialog, width: int, height: int, parent: QWidget | None = None
) -> None:
    """Resize dialog to (width, height), clamping to the available screen geometry (e.g. 1024x640 on Deck)."""
    screen = parent.screen() if parent is not None else QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        max_w = max(320, int(avail.width() * 0.96))
        max_h = max(240, int(avail.height() * 0.92))
        dialog.setMaximumSize(max_w, max_h)
        dialog.resize(min(width, max_w), min(height, max_h))
    else:
        dialog.resize(width, height)


def show_text_dialog(parent: QWidget | None, title: str, summary: str, details: str) -> QDialog:
    """Non-blocking dialog sized to the screen: a summary on top and a scrollable, wrapped text below."""
    parent = _dialog_parent(parent) if parent is not None else None
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    lay = QVBoxLayout(dlg)
    head = QLabel(summary, dlg)
    head.setObjectName("SectionTitle")
    head.setWordWrap(True)
    lay.addWidget(head)
    txt = QPlainTextEdit(dlg)
    txt.setReadOnly(True)
    txt.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    txt.setPlainText(details)
    lay.addWidget(txt, 1)
    btn = QPushButton(tr("proton_mgr.btn_close"), dlg)
    btn.clicked.connect(dlg.accept)
    lay.addWidget(btn)
    screen = parent.screen() if parent is not None else QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        dlg.setMaximumSize(int(avail.width() * 0.95), int(avail.height() * 0.9))
        dlg.resize(min(680, int(avail.width() * 0.9)), min(480, int(avail.height() * 0.8)))
    dlg.finished.connect(dlg.deleteLater)
    dlg.open()
    return dlg


def _resolve_asset_path(filename: str) -> Path | None:
    """Locate an asset: package data (pip), the repository checkout, or next to the AppImage binary."""
    candidates = [
        Path(__file__).resolve().parent / "assets" / filename,
        Path(__file__).resolve().parent.parent.parent / "assets" / filename,
        Path(sys.executable).resolve().parent / "assets" / filename,
    ]
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
    """Drag-and-drop card for setup.exe, .msi or game folder selection."""

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


# Wrappers that the Steam client itself also uses (steamrt64/pressure-vessel), so the name
# alone says nothing: they only count when pointing at a Wine/Proton prefix.
_WINE_WRAPPER_NAMES = frozenset(
    {"umu-run", "pressure-vessel-wrap", "pv-bwrap", "srt-bwrap", "pressure-vessel-adverb"}
)
_WINE_PREFIX_MARKERS = ("/compatdata/", "/pfx/", "/pfx", "/prefixes/", "/deckinstall/")


def _proc_is_wine_game(args: list[str], environ_has_prefix: bool = False) -> bool:
    """Decide from a lowercase argv whether a process is a real Wine/Proton game."""
    if any(a.endswith(".exe") for a in args):
        return True
    names = {Path(a).name for a in args[:2]}
    if "wineserver" in names:
        return True
    if not names & _WINE_WRAPPER_NAMES:
        return False
    joined = " ".join(args)
    # Wrapper without a prefix is the Steam client's own runtime helper: not a game.
    return environ_has_prefix or any(m in joined for m in _WINE_PREFIX_MARKERS)


_PROC_ROOT = Path("/proc")


def _is_wine_game_running(proc_root: Path | str | None = None) -> bool:
    """Return True if a real Windows game (.exe, wineserver or a wrapper on a Wine prefix) is running."""
    proc = Path(proc_root) if proc_root is not None else _PROC_ROOT
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
            env_prefix = False
            if Path(args[0]).name in _WINE_WRAPPER_NAMES:
                try:
                    env_prefix = b"WINEPREFIX=" in (p / "environ").read_bytes()
                except OSError:
                    env_prefix = False
            if _proc_is_wine_game(args, env_prefix):
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
    """Format worker exceptions, localizing InsufficientSpaceError and Gaming Mode refusals."""
    from .steam_vdf import SteamGamingModeError

    if isinstance(exc, SteamGamingModeError):
        return tr("steam.gaming_mode_refuse")
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
        "Orphan Game Cache": tr("cleanup.cat_game_cache"),
        "GPU Shader Cache": tr("maint.cat_gpu_shader"),
        "Steam Logs": tr("cleanup.cat_logs"),
        "Trash": tr("cleanup.cat_trash"),
        "App Cache": tr("cleanup.cat_cache"),
        "Old Proton/Runtime": tr("cleanup.cat_runtime"),
        "Empty Game Folder": tr("cleanup.cat_empty_game"),
        "Unregistered Game Folder": tr("cleanup.cat_unknown_game"),
        "Orphan Game Prefix": tr("cleanup.cat_game_prefix"),
        "Orphan Game Logs": tr("cleanup.cat_game_logs"),
        "Legacy Game Log": tr("cleanup.cat_legacy_logs"),
        "Runtime Data": tr("cleanup.cat_runtime_data"),
        "Runtime Residue": tr("cleanup.cat_runtime_residue"),
        "Imported Save Backup": tr("cleanup.cat_imported_backup"),
        "Duplicate Save Snapshot": tr("cleanup.cat_duplicate_snapshot"),
        "Orphan Desktop": tr("maint.cat_orphan_desktop"),
    }
    cat_disp = cat_map.get(cand.category, cand.category)
    hint = f" ({cand.game_hint})" if cand.game_hint else ""
    return f"[{cat_disp}] {cand.label}{hint} — {size_str}{save_tag}"


def shortcut_target_button(label: str, selected: bool = False) -> QPushButton:
    """A pill for one choice among a few (e.g. the mode: Steam or Standalone)."""
    button = QPushButton(label)
    button.setObjectName("FilterPill")
    button.setCheckable(True)
    button.setAutoDefault(False)
    button.setChecked(selected)
    return button


# Install > shortcuts: the last choice of each launcher box.
SHORTCUT_MENU_KEY = "install/shortcut_menu"
SHORTCUT_DESKTOP_KEY = "install/shortcut_desktop"


def shortcut_check(label: str, selected: bool = False) -> QCheckBox:
    """A check box for a shortcut made when the install finishes (each one on or off by itself)."""
    box = QCheckBox(label)
    box.setChecked(selected)
    return box


class FlowLayout(QLayout):
    """Responsive wrapping layout so action buttons never clip horizontally."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list = []

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


_MESSAGE_ICONS = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✕"}  # never color alone


class MessageBar(QFrame):
    """Inline message (KDE HIG "inline message"): info, success, warning or error, with optional
    action buttons and a close button. Used instead of a modal box for news that needs no answer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MessageBar")
        self.setProperty("level", "info")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(10)
        self._icon = QLabel()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.TextFormat.PlainText)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        self._close = QPushButton("✕")
        self._close.setAccessibleName(tr("proton_mgr.btn_close"))
        self._close.setFixedWidth(40)
        self._close.clicked.connect(self.dismiss)
        self._icon.setStyleSheet("font-size: 16px; font-weight: 700;")
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._text, 1)
        row.addLayout(self._actions)
        row.addWidget(self._close, 0, Qt.AlignmentFlag.AlignVCenter)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self.key = ""  # who posted the message (e.g. "updates"), to replace or clear only its own
        self.setVisible(False)

    def show_message(
        self,
        text: str,
        level: str = "info",
        *,
        actions: tuple[tuple[str, Callable[[], None]], ...] = (),
        key: str = "",
        timeout_ms: int = 0,
    ) -> None:
        """Show text; each action button also closes the bar. timeout_ms > 0 hides it later."""
        self.key = key
        self.setProperty("level", level)
        self.style().unpolish(self)
        self.style().polish(self)
        for child in self.findChildren(QLabel):
            child.style().unpolish(child)
            child.style().polish(child)
        self._icon.setText(_MESSAGE_ICONS.get(level, "ℹ"))
        self._text.setText(text)
        while self._actions.count():
            item = self._actions.takeAt(0)
            if item is not None and (w := item.widget()) is not None:
                w.deleteLater()
        for label, callback in actions:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, cb=callback: self._run_action(cb))
            self._actions.addWidget(btn)
        self._timer.stop()
        if timeout_ms > 0:
            self._timer.start(timeout_ms)
        self.setVisible(True)

    def _run_action(self, callback: Callable[[], None]) -> None:
        self.dismiss()
        callback()

    def text(self) -> str:
        return self._text.text()

    def level(self) -> str:
        return str(self.property("level"))

    def dismiss(self, key: str = "") -> None:
        """Hide the bar; with key, only when the shown message is that key's."""
        if key and key != self.key:
            return
        self._timer.stop()
        self.setVisible(False)


def _make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return card, layout


def section_card(title_text: str, hint_text: str = "") -> tuple[QWidget, QVBoxLayout, QHBoxLayout]:
    """A card with a section title (and room on its right), plus an optional one-line hint."""
    card, layout = _make_card()
    top = QHBoxLayout()
    title = QLabel(title_text)
    title.setObjectName("SectionTitle")
    top.addWidget(title)
    top.addStretch()
    layout.addLayout(top)
    if hint_text:
        hint = QLabel(hint_text)
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
    return card, layout, top


def action_row(layout: QGridLayout, row: int, button: QPushButton, hint_text: str) -> None:
    """A button with what it does next to it: the user picks by purpose, not by guessing."""
    hint = QLabel(hint_text)
    hint.setObjectName("MutedText")
    hint.setWordWrap(True)
    layout.addWidget(button, row, 0)
    layout.addWidget(hint, row, 1)


class ShownWhenEnabledButton(QPushButton):
    """A button that is only on screen while it can be used (Cancel while running, Continue when done)."""

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            # Only its own setEnabled() counts: a busy page disabling its parent must not hide it.
            self.setHidden(self.testAttribute(Qt.WidgetAttribute.WA_ForceDisabled))


def page_layout(title_text: str, subtitle_text: str) -> tuple[QWidget, QVBoxLayout]:
    """Every tab starts the same way: its name, one line on what it is for, then its cards."""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    title = QLabel(title_text)
    title.setObjectName("PageTitle")
    layout.addWidget(title)
    layout.addWidget(note_label(subtitle_text))
    return page, layout


def section_page() -> tuple[QWidget, QVBoxLayout]:
    """A Settings section: no title of its own (the section button above already names it)."""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    return page, layout


def note_label(text: str = "", kind: str = "MutedText") -> QLabel:
    """A wrapped hint or notice. kind: MutedText (hint), WarningText or ErrorText (theme colors,
    readable in light and dark), or UnavailableBanner (a boxed warning)."""
    label = QLabel(text)
    label.setObjectName(kind)
    label.setWordWrap(True)
    return label


def dialog_footer(*right: QPushButton, left: tuple[QPushButton, ...] = ()) -> QHBoxLayout:
    """The bottom row every dialog shares: extra or risky actions on the left; the main action,
    then Close or Cancel, on the right."""
    row = QHBoxLayout()
    for button in left:
        row.addWidget(button)
    row.addStretch()
    for button in right:
        row.addWidget(button)
    return row


def action_grid() -> QGridLayout:
    """Rows of action_row(): buttons on the left, what they do on the right."""
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    grid.setColumnStretch(1, 1)
    return grid


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
