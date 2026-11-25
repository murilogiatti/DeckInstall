"""Library cover grid: the same games as the table, as Steam-style covers (easier on a handheld).

The table stays the model: the grid shows its visible rows in its order, and picking a cover
selects that row, so every Library action (Play, Configure, Uninstall...) works unchanged.
Covers come from files already on disk (Steam's grid folder, Steam's library cache); a game
without one gets a tile with its initials. Nothing is downloaded here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QListWidget, QListWidgetItem

if TYPE_CHECKING:
    from .steam_paths import SteamPaths
    from .ui import MainWindow

COVER = QSize(120, 180)  # Steam's 600x900 capsule ratio
VIEW_KEY = "library/view"
_KEY_ROLE = Qt.ItemDataRole.UserRole + 1  # same role the table uses for the row key
_cache: dict[tuple[str, int], QPixmap] = {}


def cover_path(kind: str, obj: object, paths: SteamPaths | None) -> Path | None:
    """A portrait cover already on disk for this Library row, if any."""
    candidates: list[Path] = []
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
    return pix


def _cover_pixmap(path: Path | None, name: str) -> QPixmap:
    if path is not None:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            key = None
        if key is not None:
            cached = _cache.get(key)
            if cached is None:
                raw = QPixmap(str(path))
                if not raw.isNull():
                    cached = raw.scaled(
                        COVER, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    ).copy(0, 0, COVER.width(), COVER.height())
                    _cache[key] = cached
            if cached is not None:
                return cached
    return _initials_tile(name)


def build_library_grid(win: MainWindow) -> QListWidget:
    grid = QListWidget()
    grid.setObjectName("LibraryGrid")
    grid.setViewMode(QListView.ViewMode.IconMode)
    grid.setIconSize(COVER)
    grid.setGridSize(QSize(COVER.width() + 28, COVER.height() + 52))
    grid.setResizeMode(QListView.ResizeMode.Adjust)
    grid.setMovement(QListView.Movement.Static)
    grid.setWordWrap(True)
    grid.setUniformItemSizes(True)
    grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    grid.setVisible(False)
    grid.currentItemChanged.connect(lambda cur, _prev: _select_row(win, cur))
    grid.itemDoubleClicked.connect(lambda _item: win._open_configure_game_dialog())
    grid.itemActivated.connect(lambda _item: win._open_configure_game_dialog())  # Enter / A button
    win.library_grid = grid
    return grid


def _select_row(win: MainWindow, item: QListWidgetItem | None) -> None:
    if item is None:
        return
    key = item.data(_KEY_ROLE)
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


def sync_views(win: MainWindow, empty: bool) -> None:
    """Show the table or the grid (none when the Library is empty) and refill the grid."""
    as_grid = grid_mode(win) and not empty
    win.shortcuts_list.setVisible(not empty and not as_grid)
    win.library_grid.setVisible(as_grid)
    for btn, view in ((win.btn_view_list, "list"), (win.btn_view_grid, "grid")):
        btn.setChecked(("grid" if grid_mode(win) else "list") == view)
    if as_grid:
        rebuild_grid(win)


def rebuild_grid(win: MainWindow) -> None:
    table = win.shortcuts_list
    grid = win.library_grid
    current_key = None
    cur = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
    if cur is not None:
        current_key = cur.data(_KEY_ROLE)
    grid.blockSignals(True)
    grid.clear()
    select: QListWidgetItem | None = None
    for row in range(table.rowCount()):
        if table.isRowHidden(row):
            continue
        cell = table.item(row, 0)
        if cell is None:
            continue
        data = cell.data(Qt.ItemDataRole.UserRole)
        kind, obj = data if isinstance(data, tuple) and len(data) == 2 else ("", None)
        name = cell.text()
        item = QListWidgetItem(QIcon(_cover_pixmap(cover_path(kind, obj, win.paths), name)), name)
        item.setData(_KEY_ROLE, cell.data(_KEY_ROLE))
        item.setToolTip(cell.toolTip())
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        grid.addItem(item)
        if current_key is not None and item.data(_KEY_ROLE) == current_key:
            select = item
    if select is not None:
        grid.setCurrentItem(select)
    grid.blockSignals(False)
