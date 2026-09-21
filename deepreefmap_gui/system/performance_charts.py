"""Aligned resource distributions for performance configuration and run rows."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from deepreefmap_gui.core.theme import BLOCK, PRIMARY, SUCCESS, TEXT_MUTED, UPDATE
from deepreefmap_gui.profiling.system_probe import format_bytes

METRICS = {"ram": "RAM", "swap": "Swap", "vram": "VRAM", "seconds_per_frame": "Time / frame"}
CAPACITIES = {"ram": "total_ram_bytes", "swap": "total_swap_bytes", "vram": "total_vram_bytes"}


def metric_text(value: float | None, metric: str) -> str:
    """Return a readable resource or processing-time value."""
    if value is None:
        return "No data"
    return f"{value:.2f} s" if metric == "seconds_per_frame" else format_bytes(int(value))


def metric_scales(groups: list[dict]) -> dict[str, float]:
    """Return shared absolute scales, including observed failures and capacity."""
    scales = dict.fromkeys(METRICS, 1.0)
    for group in groups:
        for metric in METRICS:
            observed = [row.get(metric) or 0 for row in group["runs"]]
            total = group["hardware"].get(CAPACITIES.get(metric, "")) or 0
            scales[metric] = max(scales[metric], total, *observed)
    return scales


def usage_color(value: float, total: float | None) -> QColor:
    """Return a capacity-pressure color, or blue when capacity is unknown."""
    if not total:
        return QColor(PRIMARY)
    fraction = value / total
    return QColor(BLOCK if fraction >= 0.9 else UPDATE if fraction >= 0.5 else SUCCESS)


class MetricCell(QWidget):
    """A median value, capacity fraction and distribution on a shared scale."""

    def __init__(self, stats: dict, metric: str, maximum: float, hardware: dict, parent=None):
        super().__init__(parent)
        self.stats, self.metric_key, self.maximum = stats, metric, maximum
        self.total = hardware.get(CAPACITIES.get(metric, ""))
        self.setMinimumWidth(105)
        self.setFixedHeight(62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(self._description())
        self.setAccessibleName(f"{METRICS[metric]}: {self._description()}")

    def _description(self) -> str:
        stats, metric = self.stats, self.metric_key
        if not stats["n"]:
            return "No eligible timing observations" if metric == "seconds_per_frame" else "No measurements recorded"
        lines = [
            f"Median: {metric_text(stats['median'], metric)}",
            f"Observed: {metric_text(stats['min'], metric)} to {metric_text(stats['max'], metric)}",
            f"{stats['n']} observation(s)",
            f"Column scale: 0 to {metric_text(self.maximum, metric)}",
        ]
        if stats["n"] > 1:
            lines.insert(1, f"Middle 50%: {metric_text(stats['q1'], metric)} to {metric_text(stats['q3'], metric)}")
        if self.total:
            lines.append(f"Capacity at run time: {metric_text(self.total, metric)}")
        return "\n".join(lines)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        median = self.stats["median"]
        painter.setPen(self.palette().text().color() if median is not None else QColor(TEXT_MUTED))
        font = painter.font()
        font.setBold(median is not None)
        painter.setFont(font)
        painter.drawText(
            QRectF(2, 5, self.width() - 4, 23), Qt.AlignmentFlag.AlignLeft, metric_text(median, self.metric_key)
        )
        if median is not None and self.total:
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(
                QRectF(2, 5, self.width() - 4, 23), Qt.AlignmentFlag.AlignRight, f"{100 * median / self.total:.0f}%"
            )
        self._paint_distribution(painter)
        painter.end()

    def _paint_distribution(self, painter: QPainter) -> None:
        width, y = max(1, self.width() - 6), 39
        track = QColor(TEXT_MUTED)
        track.setAlpha(35)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(3, y - 3, width, 6), 3, 3)
        if not self.stats["n"]:
            return

        def position(value):
            return 3 + width * min(1, value / self.maximum)

        color = usage_color(self.stats["median"], self.total)
        tint = QColor(color)
        tint.setAlpha(85)
        painter.fillRect(QRectF(3, y - 3, position(self.stats["median"]) - 3, 6), tint)
        painter.setPen(QPen(color, 2))
        painter.drawLine(int(position(self.stats["min"])), y, int(position(self.stats["max"])), y)
        if self.stats["n"] > 1:
            painter.fillRect(
                QRectF(
                    position(self.stats["q1"]),
                    y - 4,
                    max(2, position(self.stats["q3"]) - position(self.stats["q1"])),
                    8,
                ),
                color,
            )
        painter.setPen(QPen(usage_color(self.stats["max"], self.total), 2))
        painter.drawLine(int(position(self.stats["max"])), y - 7, int(position(self.stats["max"])), y + 7)
        painter.setPen(QPen(self.palette().text().color(), 2))
        painter.drawLine(int(position(self.stats["median"])), y - 6, int(position(self.stats["median"])), y + 6)
