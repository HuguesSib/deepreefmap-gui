"""Shared image widgets: a click-to-open label and a zoomable image view.

The view is a QGraphicsView rather than a scrolled QLabel because a scene keeps
the image geometry and the scroll offsets in one place, which is what makes
zooming about the cursor a couple of lines rather than the tile arithmetic the
map widget had to hand-roll.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# Matched to the map widget's wheel handling so zooming feels the same
# everywhere in the app.
_MAX_NOTCHES_PER_EVENT = 3.0
_ZOOM_PER_NOTCH = 1.25

# Ceiling relative to the image's own pixels: past this you are looking at
# interpolation, not data.
_MAX_ZOOM_VS_NATIVE = 8.0


class ClickableLabel(QLabel):
    """A label that reports left clicks, for click-to-open thumbnails."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ZoomableImageView(QGraphicsView):
    """An image fitted to the view width, zoomable with the wheel.

    Fit-to-width is the resting state: an ortho is a long thin strip and a
    video frame is wider than it is tall, so width is the dimension worth
    spending on and the height scrolls. Wheeling zooms about the cursor;
    double-clicking returns to fit.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        # NoAnchor: the wheel handler pins the cursor point itself, because it
        # has to rebuild the transform from the fit scale rather than scale the
        # existing one.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        # 1.0 means "fitted"; user zoom multiplies the fit scale.
        self._zoom = 1.0

    # --- Image ---

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Show ``pixmap``, keeping the current zoom if it is the same size.

        Preserving zoom is what makes a popup usable while scrubbing frames:
        the image changes underneath, the magnification does not. A different
        size means a different image, so the zoom no longer means anything.
        """
        previous = self._item.pixmap()
        same_size = not previous.isNull() and previous.size() == pixmap.size()
        # Rebuilding the transform drops the scroll offsets, so remember where
        # the view was looking: scrubbing frames while zoomed in on a corner of
        # the reef should keep you on that corner.
        centre = self.mapToScene(self.viewport().rect().center())
        self._item.setPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        if same_size:
            self._apply_zoom()
            self.centerOn(centre)
        else:
            self.reset_zoom()

    def pixmap(self) -> QPixmap:
        return self._item.pixmap()

    # --- Zoom ---

    def zoom_factor(self) -> float:
        """User zoom relative to fit-to-width; 1.0 is fitted."""
        return self._zoom

    def is_fitted(self) -> bool:
        return self._zoom == 1.0

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._apply_zoom()

    def _fit_scale(self) -> float:
        pixmap = self._item.pixmap()
        if pixmap.isNull() or pixmap.width() <= 0:
            return 1.0
        return max(self.viewport().width(), 1) / pixmap.width()

    def _apply_zoom(self) -> None:
        scale = self._fit_scale() * self._zoom
        self.resetTransform()
        self.scale(scale, scale)

    def max_zoom(self) -> float:
        fit = self._fit_scale()
        if fit <= 0:
            return _MAX_ZOOM_VS_NATIVE
        # Never let the ceiling fall below 1.0, or an image narrower than the
        # viewport (already blown up by the fit) could not be zoomed at all.
        return max(1.0, _MAX_ZOOM_VS_NATIVE / fit)

    def zoom_by_notches(self, notches: float, anchor=None) -> None:
        """Zoom by ``notches`` wheel detents, keeping ``anchor`` fixed."""
        if self._item.pixmap().isNull():
            return
        notches = max(-_MAX_NOTCHES_PER_EVENT, min(_MAX_NOTCHES_PER_EVENT, notches))
        if notches == 0:
            return
        target = self._zoom * (_ZOOM_PER_NOTCH**notches)
        target = max(1.0, min(self.max_zoom(), target))
        if target == self._zoom:
            return
        point = self.viewport().rect().center() if anchor is None else anchor.toPoint()
        scene_point = self.mapToScene(point)
        self._zoom = target
        self._apply_zoom()
        # Scroll back by however far the anchored scene point drifted.
        drift = self.mapFromScene(scene_point) - point
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() + drift.x())
        vbar.setValue(vbar.value() + drift.y())

    def wheelEvent(self, event) -> None:
        if self._item.pixmap().isNull():
            super().wheelEvent(event)
            return
        event.accept()
        self.zoom_by_notches(event.angleDelta().y() / 120.0, event.position())

    def mouseDoubleClickEvent(self, event) -> None:
        self.reset_zoom()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        # Re-fit only while fitted: a resize should never yank a zoomed view.
        if self.is_fitted():
            self._apply_zoom()


class ImageDialog(QDialog):
    """One image at full resolution, fitted to the window and zoomable."""

    def __init__(
        self,
        pixmap: QPixmap,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = ZoomableImageView(self)
        layout.addWidget(self.view)

        screen = QGuiApplication.primaryScreen()
        bounds = screen.availableSize() if screen is not None else QSize(1280, 800)
        if not pixmap.isNull():
            self.resize(
                min(pixmap.width(), int(bounds.width() * 0.9)),
                min(pixmap.height(), int(bounds.height() * 0.9)),
            )
        self.view.set_pixmap(pixmap)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.view.set_pixmap(pixmap)
