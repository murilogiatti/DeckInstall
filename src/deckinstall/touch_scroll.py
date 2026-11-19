"""One-finger kinetic scrolling for every scrollable view (Steam Deck touchscreen)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QAbstractScrollArea, QApplication, QScroller


class TouchScrollFilter(QObject):
    """Grab the touch-scroll gesture on each scroll area the first time it is shown."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        if event.type() == QEvent.Type.Show and isinstance(obj, QAbstractScrollArea):
            enable_touch_scroll(obj)
        return False


def enable_touch_scroll(area: QAbstractScrollArea) -> None:
    viewport = area.viewport()
    if viewport.property("_touchScroll"):
        return
    viewport.setProperty("_touchScroll", True)
    viewport.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
    if isinstance(area, QAbstractItemView):
        area.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        area.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    QScroller.grabGesture(viewport, QScroller.ScrollerGestureType.TouchGesture)


def install_touch_scroll(app: QApplication) -> TouchScrollFilter:
    flt = TouchScrollFilter(app)
    app.installEventFilter(flt)
    return flt
