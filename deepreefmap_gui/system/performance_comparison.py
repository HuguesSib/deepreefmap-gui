"""Selected configuration, measured alternatives and expandable evidence."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.profiling.comparison import comparable, history_observations, summarize
from deepreefmap_gui.profiling.system_probe import format_bytes


def config_label(group: dict) -> str:
    """Return the visible processing configuration."""
    settings = group["settings"]
    text = (
        f"{settings.get('processing_width', '?')} × {settings.get('processing_height', '?')} · "
        f"{settings.get('fps', '?')} fps · batch {settings.get('preprocess_batch_size', '?')} · "
        f"{settings.get('mapping_backend', '?')} · {settings.get('segmentation_model', '?')}"
    )
    return text + (" · legacy" if not group["known"] else "")


def metric_text(value: float | None, metric: str) -> str:
    """Return a memory or per-frame timing value."""
    if value is None:
        return "Not recorded"
    return f"{value:.2f} s/frame" if metric == "seconds_per_frame" else format_bytes(int(value))


class RangePlot(QWidget):
    """Median and quartile bands sharing one scale."""

    def __init__(self, rows: list[tuple[str, dict]], parent=None):
        super().__init__(parent)
        self.rows = rows
        self.setFixedHeight(max(45, len(rows) * 42))
        self.setAccessibleName("Median marker, middle 50 percent band, observed range")

    def paintEvent(self, event):
        painter = QPainter(self)
        maximum = max((stats["max"] or 0 for _, stats in self.rows), default=1) or 1
        width = max(1, self.width() - 230)
        for index, (label, stats) in enumerate(self.rows):
            y = index * 42 + 20
            painter.setPen(self.palette().text().color())
            painter.drawText(0, y + 4, label)
            if not stats["n"]:
                painter.drawText(220, y + 4, "Not recorded")
                continue

            def x(value):
                return 220 + int(width * value / maximum)

            painter.setPen(QPen(QColor("#8196a8"), 2))
            painter.drawLine(x(stats["min"]), y, x(stats["max"]), y)
            if stats["n"] > 1:
                painter.setPen(QPen(QColor("#469dff"), 9))
                painter.drawLine(x(stats["q1"]), y, x(stats["q3"]), y)
            painter.setPen(QPen(self.palette().text().color(), 3))
            painter.drawLine(x(stats["median"]), y - 8, x(stats["median"]), y + 8)
        painter.end()


class WorkloadPlot(QWidget):
    """Frame count versus measured peak memory for individual runs."""

    def __init__(self, rows: list[dict], metric: str, parent=None):
        super().__init__(parent)
        self.points = [
            (row.get("frames"), row.get(metric)) for row in rows if row.get("frames") and row.get(metric) is not None
        ]
        self.setMinimumHeight(180)
        self.setAccessibleName(f"Frame count versus {metric.upper()} peak")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().text().color())
        painter.drawText(12, 18, "Peak memory versus frames")
        if not self.points:
            painter.drawText(12, 48, "No paired observations")
            return
        max_x = max(point[0] for point in self.points) or 1
        max_y = max(point[1] for point in self.points) or 1
        painter.drawLine(60, 35, 60, 145)
        painter.drawLine(60, 145, self.width() - 20, 145)
        painter.drawText(0, 32, format_bytes(int(max_y)))
        painter.drawText(60, 170, f"0 to {max_x:,} frames")
        painter.setPen(QPen(QColor("#469dff"), 7))
        for frames, memory in self.points:
            painter.drawPoint(60 + int((self.width() - 90) * frames / max_x), 145 - int(105 * memory / max_y))
        painter.end()


class PerformanceComparison(QWidget):
    """Compact comparison of configurations in recent local history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.groups = []
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(QLabel("Performance comparisons · recent local history (up to 10 runs per history key)"))
        form = QFormLayout()
        self.configuration = QComboBox()
        form.addRow("Configuration", self.configuration)
        self.parameter = QComboBox()
        for label, key in (
            ("Resolution", "resolution"),
            ("Input framerate", "fps"),
            ("Batch size", "batch"),
            ("Models", "models"),
        ):
            self.parameter.addItem(label, key)
        form.addRow("Compare parameter", self.parameter)
        self.metric_combo = QComboBox()
        for label, key in (
            ("RAM", "ram"),
            ("Processing speed", "seconds_per_frame"),
            ("Swap", "swap"),
            ("VRAM", "vram"),
        ):
            self.metric_combo.addItem(label, key)
        form.addRow("Comparison metric", self.metric_combo)
        workload = QHBoxLayout()
        self.minimum, self.maximum = QSpinBox(), QSpinBox()
        for spin in (self.minimum, self.maximum):
            spin.setRange(0, 2_000_000_000)
            spin.setSpecialValueText("Any")
            spin.valueChanged.connect(self.refresh_view)
            workload.addWidget(spin)
        form.addRow("Frames, minimum / maximum", workload)
        layout.addLayout(form)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.body)
        for combo in (self.configuration, self.parameter, self.metric_combo):
            combo.currentIndexChanged.connect(self.refresh_view)

    def refresh(self, active_settings: dict | None = None):
        previous = self.configuration.currentData()
        self.rows = history_observations()
        self.groups = summarize(self.rows)
        self.configuration.blockSignals(True)
        self.configuration.clear()
        for group in self.groups:
            self.configuration.addItem(config_label(group), group["key"])
            self.configuration.setItemData(
                self.configuration.count() - 1, json.dumps(group["settings"], indent=2), Qt.ItemDataRole.ToolTipRole
            )
        index = self.configuration.findData(previous)
        if index < 0 and active_settings:
            index = next(
                (
                    i
                    for i, group in enumerate(self.groups)
                    if all(
                        group["settings"].get(key) == value
                        for key, value in active_settings.items()
                        if key in group["settings"]
                    )
                ),
                -1,
            )
        self.configuration.setCurrentIndex(max(index, 0) if self.groups else -1)
        self.configuration.blockSignals(False)
        self.refresh_view()

    def refresh_view(self):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        if self.maximum.value() and self.minimum.value() > self.maximum.value():
            self.body_layout.addWidget(QLabel("Minimum frames exceeds maximum frames."))
            return
        groups = summarize(self.rows, self.minimum.value(), self.maximum.value())
        selected = next((group for group in groups if group["key"] == self.configuration.currentData()), None)
        if selected is None:
            self.body_layout.addWidget(QLabel("No observations for this configuration and workload."))
            return
        self._summary(selected)
        parameter, metric = self.parameter.currentData(), self.metric_combo.currentData()
        alternatives = [
            group for group in groups if group["key"] != selected["key"] and comparable(selected, group, parameter)
        ]
        chart_rows = [("Selected", selected["stats"][metric])]
        for index, group in enumerate(alternatives):
            chart_rows.append((f"Alternative {index + 1}", group["stats"][metric]))
            self.body_layout.addWidget(QLabel(config_label(group) + f" · {group['completed']} completed"))
        self.body_layout.addWidget(RangePlot(chart_rows))
        for label, stats in chart_rows:
            self.body_layout.addWidget(
                QLabel(f"{label}: {metric_text(stats['median'], metric)} · {stats['n']} observations")
            )
        self.body_layout.addWidget(
            QLabel(
                "Median marker · middle 50% band · observed range"
                if alternatives
                else "No comparable runs. Other settings and hardware must match."
            )
        )
        self._evidence(selected, metric)

    def _summary(self, group):
        workload = group["workload"]
        frames = f"{workload['min']:,.0f} to {workload['max']:,.0f} frames" if workload["n"] else "Frames not recorded"
        self.body_layout.addWidget(
            QLabel(
                f"{group['completed']} completed · {group['failed']} failed · {frames} · "
                f"{group['basis']} RAM/swap; whole-card VRAM"
            )
        )
        if not group["known"]:
            self.body_layout.addWidget(
                QLabel("Legacy settings: comparison and full-run timing eligibility are unknown.")
            )
        for metric, stats in group["stats"].items():
            name = {"ram": "RAM", "swap": "Swap", "vram": "VRAM", "seconds_per_frame": "Processing speed"}[metric]
            text = f"{name}: {metric_text(stats['median'], metric)}"
            if stats["n"]:
                text += f" · highest {metric_text(stats['max'], metric)} · {stats['n']} observations"
                if stats["n"] > 1:
                    text += f" · middle 50% {metric_text(stats['q1'], metric)} to {metric_text(stats['q3'], metric)}"
                total = group["hardware"].get(
                    {"ram": "total_ram_bytes", "swap": "total_swap_bytes", "vram": "total_vram_bytes"}.get(metric, "")
                )
                if total:
                    text += f" · median {100 * stats['median'] / total:.0f}% of capacity"
            label = QLabel(text)
            label.setWordWrap(True)
            self.body_layout.addWidget(label)

    def _evidence(self, group, metric):
        button = QPushButton("Explore evidence")
        button.setCheckable(True)
        self.body_layout.addWidget(button)
        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.addWidget(WorkloadPlot(group["runs"], metric if metric in ("ram", "swap", "vram") else "ram"))
        table = QTableWidget(len(group["runs"]), 8)
        table.setHorizontalHeaderLabels(
            ["Recorded", "Status", "Frames", "RAM", "Swap", "VRAM", "Seconds", "Timing evidence"]
        )
        for i, row in enumerate(group["runs"]):
            values = [
                row.get("recorded_at") or "Unknown",
                row["status"],
                row.get("frames"),
                metric_text(row["ram"], "ram"),
                metric_text(row["swap"], "swap"),
                metric_text(row["vram"], "vram"),
                row.get("duration_s"),
                row.get("timing_note"),
            ]
            for j, value in enumerate(values):
                table.setItem(i, j, QTableWidgetItem(str(value) if value is not None else "Not recorded"))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.resizeColumnsToContents()
        layout.addWidget(table)
        detail.hide()
        button.toggled.connect(detail.setVisible)
        self.body_layout.addWidget(detail)
