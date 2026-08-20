"""QPainter chart widgets for the survey analysis tab."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import DIRECTION_COLORS

# The chart's own box, outside the plot: the y-axis labels, the top legend, and
# the rotated class names along the bottom.
_MARGIN_LEFT, _MARGIN_RIGHT, _MARGIN_TOP, _MARGIN_BOTTOM = 44, 8, 26, 70

# Legend geometry, shared by the painter and the hit test so a click on a key
# lands on the series it names.
_SWATCH, _SWATCH_GAP, _KEY_GAP = 10, 14, 8

# A bar narrower than this cannot be clicked with any confidence, so a crowded
# chart is read through its legend instead.
_MIN_CLICKABLE_BAR = 4.0


def pass_color(direction: str, index: int) -> QColor:
    """A pass in its direction's colour, darkening with each repeat of it."""
    base = QColor(DIRECTION_COLORS.get(direction, TEXT_MUTED))
    return QColor.fromHsv(base.hue(), base.saturation(), max(110, 230 - index * 25))


@dataclass(frozen=True)
class _Bar:
    """One drawn bar and what a click on it means."""

    rect: QRectF
    key: str
    series: str
    label: str
    fraction: float
    colour: QColor


class GroupedBarChart(QWidget):
    """Cover per class: one bar per class pooled, or one bar per pass beside it.

    Pooled is the default because it is the estimate; the per-pass series are the
    spread it rests on. In per-pass mode a bar and its legend key both emit
    ``series_clicked`` with the key the caller supplied for that series.
    """

    series_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._labels: list[str] = []
        self._series: list[tuple[str, dict[str, float], QColor]] = []
        self._keys: list[str] = []
        self._spread: dict[str, tuple[float, float]] = {}
        self._aggregate = False
        self._passes = 0
        self._empty_text = "No completed passes yet"
        self.setMinimumHeight(240)
        self.setMouseTracking(True)

    # --- what to draw -------------------------------------------------------

    def set_data(
        self,
        labels: list[str],
        series: list[tuple[str, dict[str, float], QColor]],
        *,
        keys: list[str] | None = None,
    ) -> None:
        """One bar per series in each label's group.

        ``keys`` names each series for ``series_clicked``; without it the chart is
        inert to clicks.
        """
        self._labels = list(labels)
        self._series = list(series)
        self._keys = list(keys or [])
        self._spread = {}
        self._aggregate = False
        self._passes = len(series)
        self.update()

    def set_aggregate(
        self,
        labels: list[str],
        values: dict[str, float],
        *,
        spread: dict[str, tuple[float, float]],
        colours: dict[str, QColor],
        passes: int,
    ) -> None:
        """One bar per label, with a whisker spanning the passes behind it.

        ``spread`` may be empty, which is what a single pass means: a zero-length
        whisker would read as perfect agreement.
        """
        self._labels = list(labels)
        self._series = [
            (label, {label: values.get(label, 0.0)}, colours.get(label, QColor(TEXT_MUTED)))
            for label in self._labels
        ]
        self._keys = []
        self._spread = dict(spread)
        self._aggregate = True
        self._passes = passes
        self.update()

    # --- geometry, recomputed rather than cached from the last paint ---------

    def _plot_rect(self) -> QRectF:
        return QRectF(
            _MARGIN_LEFT,
            _MARGIN_TOP,
            max(1, self.width() - _MARGIN_LEFT - _MARGIN_RIGHT),
            max(1, self.height() - _MARGIN_TOP - _MARGIN_BOTTOM),
        )

    def _axis_top(self) -> float:
        """The y-axis maximum, high enough for the tallest whisker as well as bar."""
        heights = [
            values.get(label, 0.0)
            for _name, values, _colour in self._series
            for label in self._labels
        ]
        heights += [high for _low, high in self._spread.values()]
        return max(0.05, math.ceil(max(heights, default=0.0) * 20) / 20)

    def _bars(self) -> list[_Bar]:
        if not self._labels or not self._series:
            return []
        plot = self._plot_rect()
        top = self._axis_top()
        aggregated = self._aggregate
        per_group = 1 if aggregated else len(self._series)
        group_width = plot.width() / len(self._labels)
        bar_width = group_width * 0.8 / per_group
        bars: list[_Bar] = []
        for label_index, label in enumerate(self._labels):
            group_left = plot.left() + label_index * group_width + group_width * 0.1
            drawn = (
                [(label_index, self._series[label_index])]
                if aggregated
                else list(enumerate(self._series))
            )
            for slot, (series_index, (name, values, _colour)) in enumerate(drawn):
                fraction = values.get(label, 0.0)
                height = plot.height() * (fraction / top)
                bars.append(
                    _Bar(
                        rect=QRectF(
                            group_left + slot * bar_width,
                            plot.bottom() - height,
                            max(1.0, bar_width - 1),
                            height,
                        ),
                        key=self._keys[series_index] if series_index < len(self._keys) else "",
                        series=name,
                        label=label,
                        fraction=fraction,
                        colour=self._series[series_index][2],
                    )
                )
        return bars

    def _legend_keys(self) -> list[tuple[QRectF, str, str, QColor]]:
        """Each legend entry's whole clickable box, with the series it names."""
        if self._aggregate:
            return []
        plot = self._plot_rect()
        metrics = self.fontMetrics()
        entries: list[tuple[QRectF, str, str, QColor]] = []
        x = plot.left()
        for index, (name, _values, colour) in enumerate(self._series):
            text_width = metrics.horizontalAdvance(name) + _KEY_GAP
            entries.append(
                (
                    QRectF(x, 2, _SWATCH_GAP + text_width, 18),
                    self._keys[index] if index < len(self._keys) else "",
                    name,
                    colour,
                )
            )
            x += _SWATCH_GAP + text_width + _KEY_GAP
        return entries

    def _at(self, pos: QPointF) -> _Bar | None:
        return next((bar for bar in self._bars() if bar.rect.contains(pos)), None)

    def _key_at(self, pos: QPointF) -> str | None:
        for rect, key, _name, _colour in self._legend_keys():
            if rect.contains(pos):
                return key
        return None

    # --- pointer ------------------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        bar = self._at(event.position())
        if bar is not None:
            where = f"{bar.series}, {bar.label}" if self._keys else bar.label
            self.setToolTip(f"{where}: {bar.fraction * 100:.1f}%")
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            key = self._key_at(event.position())
            if key is None:
                bar = self._at(event.position())
                key = bar.key if bar is not None and bar.rect.width() >= _MIN_CLICKABLE_BAR else None
            if key:
                self.series_clicked.emit(key)
        super().mousePressEvent(event)

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_color = self.palette().windowText().color()
        if not self._labels or not self._series:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            return

        plot = self._plot_rect()
        top = self._axis_top()
        self._paint_axes(painter, plot, top, text_color)
        for bar in self._bars():
            painter.fillRect(bar.rect, bar.colour)
        self._paint_whiskers(painter, plot, top, text_color)
        self._paint_class_labels(painter, plot, text_color)
        self._paint_legend(painter, plot, text_color)

    def _paint_axes(
        self, painter: QPainter, plot: QRectF, top: float, text_color: QColor
    ) -> None:
        grid_color = QColor(text_color)
        grid_color.setAlpha(50)
        steps = 4
        for step in range(steps + 1):
            value = top * step / steps
            y = plot.bottom() - plot.height() * step / steps
            painter.setPen(grid_color)
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(text_color)
            painter.drawText(
                QRectF(0, y - 8, _MARGIN_LEFT - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value * 100:.0f}%",
            )

    def _paint_whiskers(
        self, painter: QPainter, plot: QRectF, top: float, text_color: QColor
    ) -> None:
        if not self._spread:
            return
        ink = QColor(text_color)
        ink.setAlpha(170)
        painter.setPen(QPen(ink, 1))
        for bar in self._bars():
            span = self._spread.get(bar.label)
            if span is None:
                continue
            low, high = span
            centre = bar.rect.center().x()
            y_low = plot.bottom() - plot.height() * (low / top)
            y_high = plot.bottom() - plot.height() * (high / top)
            cap = min(4.0, bar.rect.width() / 3.0)
            painter.drawLine(int(centre), int(y_low), int(centre), int(y_high))
            for y in (y_low, y_high):
                painter.drawLine(int(centre - cap), int(y), int(centre + cap), int(y))

    def _paint_class_labels(self, painter: QPainter, plot: QRectF, text_color: QColor) -> None:
        group_width = plot.width() / len(self._labels)
        painter.setPen(text_color)
        for label_index, label in enumerate(self._labels):
            group_left = plot.left() + label_index * group_width + group_width * 0.1
            painter.save()
            painter.translate(group_left + group_width * 0.4, plot.bottom() + 6)
            painter.rotate(-45)
            painter.drawText(
                QRectF(-_MARGIN_BOTTOM, 0, _MARGIN_BOTTOM, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                painter.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideRight, _MARGIN_BOTTOM
                ),
            )
            painter.restore()

    def _paint_legend(self, painter: QPainter, plot: QRectF, text_color: QColor) -> None:
        if self._aggregate:
            if self._spread:
                painter.setPen(QColor(TEXT_MUTED))
                painter.drawText(
                    QRectF(plot.left(), 2, plot.width(), 18),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    f"whisker: lowest to highest of {self._passes} passes",
                )
            return
        for rect, _key, name, colour in self._legend_keys():
            painter.fillRect(QRectF(rect.left(), 6, _SWATCH, _SWATCH), colour)
            painter.setPen(text_color)
            painter.drawText(
                QRectF(rect.left() + _SWATCH_GAP, rect.top(), rect.width(), rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name,
            )
