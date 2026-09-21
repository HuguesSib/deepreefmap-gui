"""Visual comparison of recorded configurations and their individual runs."""

from __future__ import annotations

import json
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import CARD_BG, TEXT_MUTED
from deepreefmap_gui.profiling.comparison import distribution, history_observations, summarize
from deepreefmap_gui.system.performance_charts import METRICS, MetricCell, metric_scales


def model_pair(group: dict) -> tuple:
    """Return the mapping and segmentation models identifying a filter option."""
    settings = group["settings"]
    return settings.get("mapping_backend"), settings.get("segmentation_model")


def config_label(group: dict) -> str:
    """Return the resolution and input sampling rate of a configuration."""
    settings = group["settings"]
    return (
        f"{settings.get('processing_width') or '?'} × {settings.get('processing_height') or '?'}"
        f" · {settings.get('fps') or '?'} fps"
    )


def workload_label(group: dict) -> str:
    """Return the observed frame range without using it as configuration identity."""
    workload = group["workload"]
    if not workload["n"]:
        return "Frame count unknown"
    if workload["min"] == workload["max"]:
        return f"{workload['min']:,.0f} frames"
    return f"{workload['min']:,.0f} to {workload['max']:,.0f} frames"


def muted(text: str) -> QLabel:
    """Return a secondary label using the application palette."""
    label = QLabel(text)
    label.setStyleSheet(f"color: {TEXT_MUTED}")
    return label


def columns(widget: QWidget) -> QGridLayout:
    """Return the same five-column layout for headings, groups and evidence."""
    layout = QGridLayout(widget)
    layout.setContentsMargins(12, 6, 12, 6)
    layout.setHorizontalSpacing(18)
    layout.setColumnMinimumWidth(0, 218)
    layout.setColumnStretch(0, 2)
    for column in range(1, 5):
        layout.setColumnMinimumWidth(column, 105)
        layout.setColumnStretch(column, 1)
    return layout


def add_metrics(layout: QGridLayout, group: dict, scales: dict) -> None:
    """Place all four resource distributions in aligned columns."""
    for column, metric in enumerate(METRICS, 1):
        layout.addWidget(MetricCell(group["stats"][metric], metric, scales[metric], group["hardware"]), 0, column)


class RunEvidence(QFrame):
    """Individual runs with the same visual scales as their configuration group."""

    def __init__(self, group: dict, scales: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("performanceEvidence")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(0)
        for run in sorted(group["runs"], key=lambda row: row.get("frames") or 0):
            layout.addWidget(self._run_row(run, group, scales))
        detail = muted(self._measurement_note(group))
        detail.setWordWrap(True)
        detail.setContentsMargins(12, 4, 12, 0)
        layout.addWidget(detail)

    def _run_row(self, run: dict, group: dict, scales: dict) -> QWidget:
        row = QWidget()
        row.setObjectName("performanceRunRow")
        layout = columns(row)
        label = QWidget()
        text = QVBoxLayout(label)
        text.setContentsMargins(0, 0, 0, 0)
        frames = f"{run['frames']:,} frames" if run.get("frames") else "Frames unknown"
        text.addWidget(QLabel(frames))
        parts = [run["status"]]
        if run.get("duration_s") is not None:
            parts.append(f"{run['duration_s']:.0f} s")
        text.addWidget(muted(" · ".join(parts)))
        label.setToolTip(f"{run.get('recorded_at') or 'Date not recorded'}\n{run.get('timing_note') or ''}")
        layout.addWidget(label, 0, 0)
        single = {
            "stats": {metric: distribution([run.get(metric)]) for metric in METRICS},
            "hardware": group["hardware"],
        }
        add_metrics(layout, single, scales)
        return row

    def _measurement_note(self, group: dict) -> str:
        note = "Memory shows the peak reached in each run. Timing excludes cached, partial and unverified runs."
        if not group["known"]:
            note += " Older runs have incomplete settings or timing metadata."
        return note


class ConfigurationRow(QFrame):
    """One configuration with four resource visuals and a run disclosure."""

    def __init__(self, group: dict, scales: dict, parent=None):
        super().__init__(parent)
        self.group = group
        self.setObjectName("performanceConfiguration")
        self.setStyleSheet(f"QFrame#performanceConfiguration {{ background: {CARD_BG}; border-radius: 6px; }}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.summary = QWidget()
        grid = columns(self.summary)
        grid.addWidget(self._label(), 0, 0)
        add_metrics(grid, group, scales)
        outer.addWidget(self.summary)
        self.evidence = None
        self.scales = scales
        self.disclosure.toggled.connect(self._toggle_evidence)

    def _label(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        title = QLabel(config_label(self.group))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        details = workload_label(self.group)
        batch = self.group["settings"].get("preprocess_batch_size")
        if batch:
            details += f" · batch {batch}"
        layout.addWidget(muted(details))
        self.disclosure = QToolButton()
        self.disclosure.setCheckable(True)
        self.disclosure.setAutoRaise(True)
        self.disclosure.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.disclosure.setArrowType(Qt.ArrowType.RightArrow)
        count = self.group["count"]
        text = f"{count} run" + ("s" if count != 1 else "")
        if self.group["failed"]:
            text += f" · {self.group['failed']} failed"
        self.disclosure.setText(text)
        self.disclosure.setAccessibleName(f"Show runs: {config_label(self.group)}")
        layout.addWidget(self.disclosure, alignment=Qt.AlignmentFlag.AlignLeft)
        widget.setToolTip(json.dumps(self.group["settings"], indent=2))
        return widget

    def _toggle_evidence(self, expanded: bool) -> None:
        if expanded and self.evidence is None:
            self.evidence = RunEvidence(self.group, self.scales, self)
            self.layout().addWidget(self.evidence)
        if self.evidence is not None:
            self.evidence.setVisible(expanded)
        self.disclosure.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)


class PerformanceComparison(QWidget):
    """Aligned configuration groups, with optional filters and run details."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.groups, self.visible_groups = [], [], []
        self.expanded_keys = set()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        self._build_toolbar(layout)
        self._build_filters(layout)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        layout.addWidget(self.body)
        self.legend = muted("Bars: median · band: middle 50% · end tick: highest")
        self.legend.setToolTip(
            "Memory columns share an absolute scale across configurations. Percentages use capacity at run time. "
            "Each configuration can contain different workloads; these are observations, not controlled trials."
        )
        layout.addWidget(self.legend)

    def _build_toolbar(self, layout: QVBoxLayout) -> None:
        toolbar = QHBoxLayout()
        title = QLabel("Recorded performance")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        toolbar.addWidget(title)
        self.models = QComboBox()
        self.models.setAccessibleName("Models")
        self.models.setMinimumWidth(220)
        self.models.currentIndexChanged.connect(self.refresh_view)
        toolbar.addWidget(self.models, 1)
        self.filters_button = QPushButton("Filters")
        self.filters_button.setCheckable(True)
        toolbar.addWidget(self.filters_button)
        layout.addLayout(toolbar)

    def _build_filters(self, layout: QVBoxLayout) -> None:
        self.filters = QWidget()
        filters = QHBoxLayout(self.filters)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.addWidget(QLabel("Frames"))
        self.minimum, self.maximum = QSpinBox(), QSpinBox()
        for label, spin in (("Minimum frames", self.minimum), ("Maximum frames", self.maximum)):
            spin.setRange(0, 2_000_000_000)
            spin.setSpecialValueText("Any")
            spin.setAccessibleName(label)
            spin.setToolTip(label)
            spin.valueChanged.connect(self.refresh_view)
            filters.addWidget(spin)
        filters.addStretch()
        layout.addWidget(self.filters)
        self.filters.hide()
        self.filters_button.toggled.connect(self.filters.setVisible)

    def refresh(self, active_settings: dict | None = None) -> None:
        previous, populated = self.models.currentData(), self.models.count() > 0
        self.rows = history_observations()
        self.groups = summarize(self.rows)
        self.models.blockSignals(True)
        self.models.clear()
        self.models.addItem("All models", None)
        pairs = dict.fromkeys(model_pair(group) for group in self.groups)
        for mapping, segmentation in pairs:
            self.models.addItem(
                f"{mapping or 'Unknown mapping'} · {segmentation or 'Unknown segmentation'}", (mapping, segmentation)
            )
        preferred = previous if populated else model_pair({"settings": active_settings or {}})
        index = next((i for i in range(self.models.count()) if self.models.itemData(i) == preferred), -1)
        if index < 0:
            index = 1 if len(pairs) == 1 else 0
        self.models.setCurrentIndex(index)
        self.models.blockSignals(False)
        self.refresh_view()

    def refresh_view(self) -> None:
        self._clear_groups()
        self.visible_groups = []
        filtered = bool(self.minimum.value() or self.maximum.value())
        self.filters_button.setText("Filters (active)" if filtered else "Filters")
        if self.maximum.value() and self.minimum.value() > self.maximum.value():
            self.body_layout.addWidget(QLabel("Minimum frames exceeds maximum frames."))
            self.legend.hide()
            return
        groups = summarize(self.rows, self.minimum.value(), self.maximum.value())
        selected = self.models.currentData()
        self.visible_groups = [group for group in groups if selected is None or model_pair(group) == selected]
        self.legend.setVisible(bool(self.visible_groups))
        if not self.visible_groups:
            self.body_layout.addWidget(muted("No recorded runs match these filters."))
            return
        scales = metric_scales(self.visible_groups)
        self._add_headings()
        self._add_groups(scales, selected is None)

    def _clear_groups(self) -> None:
        while self.body_layout.count():
            widget = self.body_layout.takeAt(0).widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def _add_headings(self) -> None:
        header = QWidget()
        grid = columns(header)
        grid.addWidget(muted("Configuration"), 0, 0)
        for column, name in enumerate(METRICS.values(), 1):
            grid.addWidget(muted(name), 0, column)
        self.body_layout.addWidget(header)

    def _add_groups(self, scales: dict, show_models: bool) -> None:
        sections = {}
        for group in self.visible_groups:
            key = (model_pair(group), group["basis"], json.dumps(group["hardware"], sort_keys=True))
            sections.setdefault(key, []).append(group)
        for (models, basis, _), groups in sections.items():
            scope = {"machine": "Total system memory", "process": "DeepReefMap memory"}.get(
                basis, "Memory scope unknown"
            )
            if show_models:
                scope = " · ".join(str(model or "Unknown model") for model in models) + " · " + scope
            section = muted(scope)
            section.setContentsMargins(12, 5, 0, 0)
            section.setToolTip("RAM and swap use this measurement scope. VRAM measures the whole graphics card.")
            self.body_layout.addWidget(section)
            for group in sorted(groups, key=self._configuration_order):
                row = ConfigurationRow(group, scales, self.body)
                row.disclosure.toggled.connect(partial(self._remember_expansion, group["key"]))
                self.body_layout.addWidget(row)
                row.disclosure.setChecked(group["key"] in self.expanded_keys)

    @staticmethod
    def _configuration_order(group: dict) -> tuple:
        settings = group["settings"]
        return tuple(
            settings.get(key) or 0
            for key in ("processing_width", "processing_height", "fps", "preprocess_batch_size")
        )

    def _remember_expansion(self, key: str, expanded: bool) -> None:
        if expanded:
            self.expanded_keys.add(key)
        else:
            self.expanded_keys.discard(key)
