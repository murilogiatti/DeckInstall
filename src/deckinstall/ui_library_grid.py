"""Library cover grid: the same games as the table, as Steam-style covers (easier on a handheld).

Virtualized via QListView + QAbstractListModel so libraries with 1,000+ games stay instant:
only visible covers are resolved and scaled, off the UI thread (QImage in a worker thread,
converted to QPixmap on the GUI thread) into a bounded LRU cache.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPersistentModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView

if TYPE_CHECKING:
    from .steam_paths import SteamPaths
    from .ui import MainWindow

COVER = QSize(120, 180)  # Steam's 600x900 capsule ratio
VIEW_KEY = "library/view"
_KEY_ROLE = Qt.ItemDataRole.UserRole + 1  # same role the table uses for the row key
_MAX_PIXMAP_CACHE = 256
_MAX_INITIALS_CACHE = 256

_cache: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
_initials_cache: OrderedDict[str, QPixmap] = OrderedDict()
_COVER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deckinstall-cover")


def cover_path(kind: str, obj: object, paths: SteamPaths | None, key: str = "") -> Path | None:
    """A portrait cover already on disk for this Library row, if any."""
    from .library_art import cover_file_id, covers_dir

    candidates: list[Path] = []
    if key and kind in ("standalone", "external"):  # fetched from SteamGridDB by the Library
        own = covers_dir() / str(cover_file_id(key))
        candidates += [own.with_name(own.name + "p.jpg"), own.with_name(own.name + "p.png")]
    if paths is not None:
        appid_u32 = getattr(obj, "appid_u32", None) if kind == "steam" else None
        if isinstance(appid_u32, int):
            candidates += [paths.grid_dir / f"{appid_u32}p.jpg", paths.grid_dir / f"{appid_u32}p.png"]
        steam_appid = getattr(obj, "steam_appid", None)
        if isinstance(steam_appid, int) and steam_appid > 0:
            cache = paths.steam_root / "appcache" / "librarycache"
            candidates += [
                cache / str(steam_appid) / "library_600x900.jpg",
                cache / f"{steam_appid}_library_600x900.jpg",
            ]
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _initials_tile(name: str) -> QPixmap:
    cached = _initials_cache.get(name)
    if cached is not None:
        _initials_cache.move_to_end(name)
        return cached
    pix = QPixmap(COVER)
    hue = sum(map(ord, name)) % 360
    pix.fill(QColor.fromHsl(hue, 70, 70))
    painter = QPainter(pix)
    font = QFont()
    font.setPixelSize(40)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    words = [w for w in name.replace("★", "").split() if w[:1].isalnum()]
    text = "".join(w[0] for w in words[:2]).upper() or "?"
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    _initials_cache[name] = pix
    if len(_initials_cache) > _MAX_INITIALS_CACHE:
        _initials_cache.popitem(last=False)
    return pix


def _cache_put(key: tuple[str, int], pix: QPixmap) -> None:
    _cache[key] = pix
    _cache.move_to_end(key)
    while len(_cache) > _MAX_PIXMAP_CACHE:
        _cache.popitem(last=False)


def _load_scaled_image(path: Path) -> tuple[tuple[str, int], QImage] | None:
    """Thread-safe cover decode + scale into a QImage (called on background pool)."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    raw = QImage(str(path))
    if raw.isNull():
        return None
    scaled = raw.scaled(
        COVER,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    ).copy(0, 0, COVER.width(), COVER.height())
    return (str(path), mtime), scaled


def _cover_pixmap(path: Path | None, name: str) -> QPixmap:
    if path is not None:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            key = None
        if key is not None:
            cached = _cache.get(key)
            if cached is not None:
                _cache.move_to_end(key)
                return cached
            raw = QPixmap(str(path))
            if not raw.isNull():
                cached = raw.scaled(
                    COVER,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ).copy(0, 0, COVER.width(), COVER.height())
                _cache_put(key, cached)
                return cached
    return _initials_tile(name)


@dataclass(frozen=True)
class _GridEntry:
    key: str
    name: str
    tooltip: str
    kind: str
    obj: object


class _GridItemProxy:
    """Minimal compatibility shim for callers/tests inspecting grid.item(row)."""

    def __init__(self, entry: _GridEntry) -> None:
        self._entry = entry

    def text(self) -> str:
        return self._entry.name

    def data(self, role: int) -> Any:
        if role == _KEY_ROLE:
            return self._entry.key
        if role == Qt.ItemDataRole.DisplayRole:
            return self._entry.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._entry.tooltip
        return None


class _CoverBridge(QObject):
    """Marshals decoded QImages from background threads back to the GUI thread."""

    loaded = Signal(int, str, object, object)  # (generation, key, cache_key_or_none, QImage_or_none)


_INVALID_INDEX = QModelIndex()


class LibraryGridModel(QAbstractListModel):
    """Virtualized list model for the Library cover grid."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[_GridEntry] = []
        self._row_by_key: dict[str, int] = {}
        self._resolved_cache_key: dict[str, tuple[str, int] | None] = {}
        self._pending: set[str] = set()
        self._paths: SteamPaths | None = None
        self._generation = 0
        self._bridge = _CoverBridge(self)
        self._bridge.loaded.connect(self._on_cover_loaded)

    def set_entries(self, entries: list[_GridEntry], paths: SteamPaths | None) -> None:
        self.beginResetModel()
        self._generation += 1
        self._entries = entries
        self._row_by_key = {e.key: idx for idx, e in enumerate(entries)}
        self._paths = paths
        self._pending.clear()
        self._resolved_cache_key.clear()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._entries)

    def entry_at(self, row: int) -> _GridEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def row_for_key(self, key: str) -> int:
        return self._row_by_key.get(key, -1)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._entries):
            return None
        entry = self._entries[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.tooltip
        if role == _KEY_ROLE:
            return entry.key
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        if role == Qt.ItemDataRole.DecorationRole:
            cache_key = self._resolved_cache_key.get(entry.key)
            if cache_key is not None:
                pix = _cache.get(cache_key)
                if pix is not None:
                    _cache.move_to_end(cache_key)
                    return QIcon(pix)
            if entry.key not in self._resolved_cache_key and entry.key not in self._pending:
                self._schedule_load(entry)
            return QIcon(_initials_tile(entry.name))
        return None

    def _schedule_load(self, entry: _GridEntry) -> None:
        self._pending.add(entry.key)
        gen = self._generation
        paths = self._paths
        bridge = self._bridge

        def _worker() -> None:
            path = cover_path(entry.kind, entry.obj, paths, entry.key)
            if path is None:
                bridge.loaded.emit(gen, entry.key, None, None)
                return
            result = _load_scaled_image(path)
            if result is None:
                bridge.loaded.emit(gen, entry.key, None, None)
                return
            ck, img = result
            bridge.loaded.emit(gen, entry.key, ck, img)

        _COVER_POOL.submit(_worker)

    def _on_cover_loaded(
        self, gen: int, key: str, cache_key: tuple[str, int] | None, image: QImage | None
    ) -> None:
        if gen != self._generation:
            return
        self._pending.discard(key)
        if cache_key is not None and isinstance(image, QImage) and not image.isNull():
            pix = QPixmap.fromImage(image)
            if not pix.isNull():
                _cache_put(cache_key, pix)
                self._resolved_cache_key[key] = cache_key
                row = self._row_by_key.get(key)
                if row is not None:
                    idx = self.index(row, 0)
                    self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                return
        self._resolved_cache_key[key] = None


class LibraryGridView(QListView):
    """Virtualized QListView with small compatibility helpers (count, item, setCurrentRow)."""

    def __init__(self, model: LibraryGridModel) -> None:
        super().__init__()
        self._grid_model = model
        self.setModel(model)

    def count(self) -> int:
        return self._grid_model.rowCount()

    def item(self, row: int) -> _GridItemProxy | None:
        entry = self._grid_model.entry_at(row)
        return _GridItemProxy(entry) if entry is not None else None

    def setCurrentRow(self, row: int) -> None:  # noqa: N802
        if 0 <= row < self._grid_model.rowCount():
            self.setCurrentIndex(self._grid_model.index(row, 0))
        else:
            self.clearSelection()


def build_library_grid(win: MainWindow) -> LibraryGridView:
    model = LibraryGridModel(win)
    grid = LibraryGridView(model)
    grid.setObjectName("LibraryGrid")
    grid.setViewMode(QListView.ViewMode.IconMode)
    grid.setIconSize(COVER)
    grid.setGridSize(QSize(COVER.width() + 28, COVER.height() + 52))
    grid.setResizeMode(QListView.ResizeMode.Adjust)
    grid.setMovement(QListView.Movement.Static)
    grid.setWordWrap(True)
    grid.setUniformItemSizes(True)
    grid.setLayoutMode(QListView.LayoutMode.Batched)
    grid.setBatchSize(48)
    grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    grid.setVisible(False)
    grid.selectionModel().currentChanged.connect(lambda cur, _prev: _select_row(win, cur))
    grid.activated.connect(lambda _idx: win._open_configure_game_dialog())  # double-click / Enter / A button
    win.library_grid = grid
    return grid


def _select_row(win: MainWindow, index: QModelIndex) -> None:
    if not index.isValid():
        return
    key = index.data(_KEY_ROLE)
    table = win.shortcuts_list
    for row in range(table.rowCount()):
        cell = table.item(row, 0)
        if cell is not None and cell.data(_KEY_ROLE) == key:
            if table.currentRow() != row:
                table.setCurrentCell(row, 0)
            return


def grid_mode(win: MainWindow) -> bool:
    return str(win.settings.value(VIEW_KEY, "list")) == "grid"


def set_view(win: MainWindow, view: str) -> None:
    win.settings.setValue(VIEW_KEY, view)
    win._filter_library_list()


def sync_views(win: MainWindow, empty: bool, *, skip_grid: bool = False) -> None:
    """Show the table or the grid (none when the Library is empty) and update the virtual model."""
    as_grid = grid_mode(win) and not empty
    win.shortcuts_list.setVisible(not empty and not as_grid)
    win.library_grid.setVisible(as_grid)
    for btn, view in ((win.btn_view_list, "list"), (win.btn_view_grid, "grid")):
        btn.setChecked(("grid" if grid_mode(win) else "list") == view)
    if as_grid and not skip_grid:
        rebuild_grid(win)


def rebuild_grid(win: MainWindow) -> None:
    table = win.shortcuts_list
    grid = win.library_grid
    model = grid._grid_model
    current_key: str | None = None
    cur = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
    if cur is not None:
        raw_key = cur.data(_KEY_ROLE)
        current_key = str(raw_key) if raw_key is not None else None
    entries: list[_GridEntry] = []
    for row in range(table.rowCount()):
        if table.isRowHidden(row):
            continue
        cell = table.item(row, 0)
        if cell is None:
            continue
        data = cell.data(Qt.ItemDataRole.UserRole)
        kind, obj = data if isinstance(data, tuple) and len(data) == 2 else ("", None)
        entries.append(
            _GridEntry(
                key=str(cell.data(_KEY_ROLE) or ""),
                name=cell.text(),
                tooltip=cell.toolTip(),
                kind=kind,
                obj=obj,
            )
        )
    sel_model = grid.selectionModel()
    sel_model.blockSignals(True)
    try:
        model.set_entries(entries, win.paths)
        if current_key is not None:
            row_idx = model.row_for_key(current_key)
            if row_idx >= 0:
                grid.setCurrentRow(row_idx)
    finally:
        sel_model.blockSignals(False)
