"""Compare repeated passes of one transect. Shown wherever a transect is."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import GUTTER, TEXT_MUTED
from deepreefmap_gui.core.widgets import (
    ColumnSpec,
    EmptyState,
    FilterChips,
    SortableItem,
    configure_table,
    enable_sorting,
    install_column_sizer,
    muted_label,
    section_card,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.cover import COVER_LEVELS, group_color_for_name
from deepreefmap_gui.runs.run_cards import summarise_run_provenance
from deepreefmap_gui.simple.charts import GroupedBarChart, pass_color
from deepreefmap_gui.survey.analysis import (
    PooledCover,
    aggregated_cover_chart,
    assemble_transect_covers,
    collate_long_format,
    cover_labels,
    latest_run_per_pass,
    pooled_transect_cover,
    repeatability_stats,
    reproducibility_groups,
)
from deepreefmap_gui.survey.models import direction_arrow
from deepreefmap_gui.survey.models.exporters import save_long_format_csv, save_repeatability_csv

logger = logging.getLogger(__name__)

# Below this cover fraction a class is noise in the chart; the CSV keeps everything.
_CHART_MIN_FRACTION = 0.005

_MODE_POOLED, _MODE_PASSES = "pooled", "passes"
_CHART_MODES = ((_MODE_POOLED, "Pooled"), (_MODE_PASSES, "Per pass"))

# The class name takes the slack; the five figures beside it are percentages and
# ratios, which are the same width whatever the pane is.
_STATS_COLUMNS = ColumnSpec(
    fixed={1: 68, 2: 76, 3: 62, 4: 56, 5: 68},
    weights={0: 1},
    minimums={0: 120},
)


class SimpleAnalysisMixin(MixinBase):
    """DeepReefMapWindow methods for a transect's cover and repeatability."""

    def _build_analysis_page(self) -> QWidget:
        """What grows on a transect, and how far its repeat passes disagree.

        Built once and shown on the Transects page, beside the list the transect
        is picked from. Browse groups runs; it does not draw a transect again.
        """
        self._analysis_covers = []
        self._analysis_all_covers = []

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        chart_card, chart_layout = section_card()
        selector = QHBoxLayout()
        selector.setSpacing(6)
        selector.addWidget(QLabel("Transect"))
        self._analysis_transect_combo = QComboBox()
        self._analysis_transect_combo.currentIndexChanged.connect(
            lambda *_: self._on_analysis_transect_changed()
        )
        selector.addWidget(self._analysis_transect_combo, 1)
        # "Detail" rather than "Level": the combo picks how finely the classes
        # are grouped, which the old label said nothing about.
        detail_label = QLabel("Detail")
        detail_tip = (
            "How finely classes are grouped in the chart and the stats: "
            "fine is every class on its own, coarse is broad groups."
        )
        detail_label.setToolTip(detail_tip)
        selector.addWidget(detail_label)
        self._analysis_level_combo = QComboBox()
        self._analysis_level_combo.addItems(list(COVER_LEVELS))
        self._analysis_level_combo.setCurrentText("intermediate")
        self._analysis_level_combo.setToolTip(detail_tip)
        self._analysis_level_combo.currentTextChanged.connect(
            lambda *_: self._refresh_survey_analysis()
        )
        selector.addWidget(self._analysis_level_combo)
        # Pooled by default: the estimate is what the page is for, and one bar per
        # pass was four bars saying nothing about which to trust.
        self._analysis_chart_mode = str(
            self._settings.value("analysis_chart_mode", _MODE_POOLED)
        )
        if self._analysis_chart_mode not in dict(_CHART_MODES):
            self._analysis_chart_mode = _MODE_POOLED
        self._analysis_mode_chips = FilterChips(_CHART_MODES)
        self._analysis_mode_chips.setToolTip(
            "Pooled is the count-weighted estimate with the spread across passes. "
            "Per pass draws each pass on its own, and a bar opens its section."
        )
        self._analysis_mode_chips.set_current(self._analysis_chart_mode)
        self._analysis_mode_chips.changed.connect(self._on_analysis_mode_changed)
        selector.addWidget(self._analysis_mode_chips)
        chart_layout.addLayout(selector)
        # The defensible headline: the count-weighted pool and how many passes
        # it rests on. The per-pass bars below are the spread, not the estimate.
        self._analysis_estimate_label = QLabel("")
        self._analysis_estimate_label.setWordWrap(True)
        self._analysis_estimate_label.setStyleSheet(f"color: {TEXT_MUTED};")
        chart_layout.addWidget(self._analysis_estimate_label)
        # Beside the estimate, not under the table: what produced a number is
        # part of reading it.
        self._analysis_provenance_label = muted_label("")
        self._analysis_provenance_label.setWordWrap(True)
        self._analysis_provenance_label.setVisible(False)
        chart_layout.addWidget(self._analysis_provenance_label)
        self._analysis_chart = GroupedBarChart()
        self._analysis_chart.series_clicked.connect(self._on_analysis_series_clicked)
        chart_layout.addWidget(self._analysis_chart, 1)
        layout.addWidget(chart_card, 3)

        stats_card, stats_layout = section_card("Cover estimate and repeatability by class")
        self._analysis_stats_table = QTableWidget(0, 6)
        # "Cover" is the count-weighted transect estimate. "Pass mean" is the
        # unweighted average of per-pass fractions, kept only as a spread
        # reference so it is never read as the cover figure.
        configure_table(
            self._analysis_stats_table,
            ["Class", "Cover", "Pass mean", "Std", "CV", "Range"],
        )
        # Largest cover first, matching how the chart ranks its bars.
        enable_sorting(self._analysis_stats_table, 1, Qt.SortOrder.DescendingOrder)
        install_column_sizer(self._analysis_stats_table, _STATS_COLUMNS, settings_key="analysis_stats")
        self._analysis_stats_stack = QStackedWidget()
        self._analysis_stats_stack.addWidget(self._analysis_stats_table)
        self._analysis_stats_stack.addWidget(
            EmptyState(
                "No repeatability yet",
                "Process at least two passes of this transect to compare them.",
            )
        )
        stats_layout.addWidget(self._analysis_stats_stack, 1)

        self._analysis_repro_label = QLabel("")
        self._analysis_repro_label.setWordWrap(True)
        self._analysis_repro_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._analysis_repro_label.setVisible(False)
        stats_layout.addWidget(self._analysis_repro_label)
        # This transect's runs are already the rows beside this pane, so there is
        # no second list of them here.
        layout.addWidget(stats_card, 2)

        export_row = QHBoxLayout()
        self._analysis_export_btn = QPushButton("Export repeatability CSV")
        self._analysis_export_btn.clicked.connect(self._on_analysis_export_csv)
        export_row.addWidget(self._analysis_export_btn)
        self._analysis_collated_btn = QPushButton("Export collated cover CSV")
        self._analysis_collated_btn.clicked.connect(self._on_analysis_export_collated)
        export_row.addWidget(self._analysis_collated_btn)
        export_row.addStretch(1)
        layout.addLayout(export_row)
        self._update_analysis_export_button()
        return page

    def _update_analysis_export_button(self) -> None:
        """Say up front that there is nothing to export, rather than after the click."""
        ready = bool(self._analysis_covers)
        self._analysis_export_btn.setEnabled(ready)
        self._analysis_export_btn.setToolTip(
            "Write mean, standard deviation, CV and range per class to a CSV."
            if ready
            else "Nothing to export yet: this transect has no completed passes."
        )
        self._analysis_collated_btn.setEnabled(ready)
        self._analysis_collated_btn.setToolTip(
            "Write one long-format row per transect/pass/class/level, plus the "
            "count-weighted pooled estimate, with GUI and taxonomy provenance."
            if ready
            else "Nothing to export yet: no completed passes."
        )

    def _analysis_transect_id(self) -> uuid.UUID | None:
        data = self._analysis_transect_combo.currentData()
        return uuid.UUID(data) if data else None

    def _on_analysis_transect_changed(self) -> None:
        """Changing the transect here also moves the browser above it."""
        self._refresh_survey_analysis()
        self._set_scope_transect(self._analysis_transect_id())

    def _refresh_survey_analysis(self) -> None:
        store = self._try_survey_store()
        combo = self._analysis_transect_combo
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for transect in (store.list_transects() if store is not None else []):
            combo.addItem(transect.name, str(transect.id))
            if selected is not None and str(transect.id) == selected:
                combo.setCurrentIndex(combo.count() - 1)
        combo.blockSignals(False)

        transect_id = self._analysis_transect_id()
        # Without a store the combo above is empty, so transect_id is already
        # None; naming the store here says so rather than leaving it implied.
        if store is None or transect_id is None:
            self._analysis_covers = []
            self._analysis_all_covers = []
            self._analysis_chart.set_data([], [])
            self._analysis_stats_table.setRowCount(0)
            self._analysis_repro_label.setText("")
            self._analysis_estimate_label.setText("")
            self._refresh_analysis_empty_states()
            return

        out_root = Path(self._out_root_input.text()).expanduser()
        # Read every succeeded run once, then split: the deduped set (one latest
        # run per pass) drives the estimate and the chart, the full set feeds
        # reproducibility, whose whole point is the reruns dedupe removes.
        all_covers = assemble_transect_covers(
            store,
            out_root,
            transect_id,
            self._classes_config,
            level=self._analysis_level_combo.currentText(),
            dedupe=False,
        )
        covers = latest_run_per_pass(all_covers)
        self._analysis_all_covers = all_covers
        self._analysis_covers = covers
        expected = len(store.list_passes(transect_id=transect_id))
        pooled = pooled_transect_cover(covers, expected_passes=expected)
        self._draw_analysis_chart(covers, expected)
        self._analysis_mode_chips.set_counts({_MODE_PASSES: len(covers)})
        self._update_analysis_estimate_label(pooled)
        self._fill_analysis_stats(covers, pooled)
        self._fill_analysis_repro(all_covers)
        self._refresh_analysis_empty_states()

    def _draw_analysis_chart(self, covers: list, expected: int) -> None:
        """Pooled with its spread, or one series per pass when asked for."""
        level = self._analysis_level_combo.currentText()
        if self._analysis_chart_mode == _MODE_POOLED:
            aggregate = aggregated_cover_chart(
                covers, minimum_fraction=_CHART_MIN_FRACTION, expected_passes=expected
            )
            self._analysis_chart.set_aggregate(
                aggregate.labels,
                aggregate.values,
                spread=aggregate.spread,
                colours={
                    label: QColor(*group_color_for_name(self._classes_config, label, level))
                    for label in aggregate.labels
                },
                passes=aggregate.n,
            )
            return
        series = [
            (
                f"{index} {direction_arrow(c.direction)}",
                c.cover,
                pass_color(c.direction, index - 1),
            )
            for index, c in enumerate(covers, start=1)
        ]
        self._analysis_chart.set_data(
            cover_labels(covers, _CHART_MIN_FRACTION),
            series,
            keys=[str(c.pass_id) for c in covers],
        )

    def _on_analysis_mode_changed(self, key: str) -> None:
        """A reader's preference, so it belongs to the machine, not the survey."""
        self._analysis_chart_mode = key
        self._settings.setValue("analysis_chart_mode", key)
        self._refresh_survey_analysis()

    def _on_analysis_series_clicked(self, key: str) -> None:
        """A clicked pass opens where its section is described.

        Inert in pooled mode: a pooled bar is every pass at once, so there is no
        one section for it to be about.
        """
        if self._analysis_chart_mode != _MODE_PASSES:
            return
        cover = next((c for c in self._analysis_covers if str(c.pass_id) == key), None)
        if cover is None:
            return
        self._open_section_in_videos(cover.pass_id)

    def _update_analysis_estimate_label(self, pooled: PooledCover) -> None:
        """State the estimator and how many passes back the number, up front."""
        if not pooled.counts:
            self._analysis_estimate_label.setText("")
            self._analysis_provenance_label.setText("")
            self._analysis_provenance_label.setVisible(False)
            return
        passes = (
            f"{pooled.contributing_passes} of {pooled.expected_passes} "
            f"pass{'es' if pooled.expected_passes != 1 else ''}"
        )
        if self._analysis_chart_mode != _MODE_POOLED:
            tail = "Bars below show each pass, not the estimate."
        elif pooled.contributing_passes < 2:
            tail = "One pass, so the bars have no spread to show."
        else:
            tail = (
                "Bars below are that pool; each whisker spans the lowest and "
                "highest single pass."
            )
        self._analysis_estimate_label.setText(
            f"Transect cover estimate: count-weighted pool of {passes}. {tail}"
        )
        self._update_analysis_provenance()

    def _update_analysis_provenance(self) -> None:
        """Name what produced the number, beside the number.

        A cover figure is only usable if the models and the class taxonomy
        behind it can be named, and both are recorded in every run's manifest.
        Read from the contributing runs rather than from the current settings:
        the question is what made this estimate, not what the next run would use.

        Runs that disagree are said to disagree. A pooled figure built from two
        different taxonomies is not one measurement, and quietly showing the
        first run's version would hide exactly that.
        """
        label = self._analysis_provenance_label
        entries = {
            summarise_run_provenance(cover.run_dir_name, self._out_root_input.text())
            for cover in self._analysis_covers
        }
        entries.discard("")
        label.setVisible(bool(entries))
        if not entries:
            label.setText("")
        elif len(entries) == 1:
            label.setText(f"Produced by {next(iter(entries))}.")
        else:
            label.setText(
                "Passes were not all produced the same way: "
                + "; ".join(sorted(entries))
                + ". Pooling them assumes they are comparable."
            )

    def _refresh_analysis_empty_states(self) -> None:
        """Show each pane's placeholder while it has nothing to say."""
        self._analysis_stats_stack.setCurrentIndex(
            0 if self._analysis_stats_table.rowCount() else 1
        )
        self._update_analysis_export_button()

    def _fill_analysis_stats(self, covers: list, pooled: PooledCover) -> None:
        stats = repeatability_stats(covers)
        labels = cover_labels(covers)
        table = self._analysis_stats_table
        # Sorting is suspended while rows are filled: with it live, each new row
        # is re-sorted into place and the cells of a half-built row scatter.
        table.setSortingEnabled(False)
        table.setRowCount(len(labels))
        for row, label in enumerate(labels):
            entry = stats[label]
            cells = [
                (label, label.lower()),
                (f"{pooled.cover.get(label, 0.0) * 100:.1f}%", pooled.cover.get(label, 0.0)),
                (f"{entry['mean'] * 100:.1f}%", entry["mean"]),
                (f"{entry['std'] * 100:.1f}%", entry["std"]),
                (f"{entry['cv']:.2f}", entry["cv"]),
                (f"{entry['range'] * 100:.1f}%", entry["range"]),
            ]
            for column, (text, value) in enumerate(cells):
                item = SortableItem(text, value)
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(row, column, item)
        table.setSortingEnabled(True)

    def _fill_analysis_repro(self, covers: list) -> None:
        groups = reproducibility_groups(covers)
        self._analysis_repro_label.setVisible(bool(groups))
        if not groups:
            self._analysis_repro_label.setText("")
            return
        lines = []
        for group in groups:
            stats = repeatability_stats(group)
            worst = max((entry["range"] for entry in stats.values()), default=0.0)
            first = group[0]
            lines.append(
                f"#{(first.video_hash or '')[:8]} {first.begin_s:.0f}-{first.end_s:.0f}s: "
                f"{len(group)} runs, max class spread {worst * 100:.1f}%"
            )
        self._analysis_repro_label.setText(
            "Reproducibility (identical footage and trim):\n" + "\n".join(lines)
        )

    def _on_analysis_export_csv(self) -> None:
        covers = self._analysis_covers
        if not covers:
            return
        name = self._analysis_transect_combo.currentText() or "transect"
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export repeatability CSV",
            str(Path(self._out_root_input.text()) / f"{name}_repeatability.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        save_repeatability_csv(
            Path(path_str), cover_labels(covers), repeatability_stats(covers), covers
        )
        self._status_label.setText(f"Exported repeatability CSV for {name}.")

    def _on_analysis_export_collated(self) -> None:
        """Collate every transect into one long-format CSV with provenance."""
        store = self._survey_store()
        out_root = Path(self._out_root_input.text()).expanduser()
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export collated cover CSV",
            str(out_root / "survey_cover_long.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        rows = collate_long_format(store, out_root, self._classes_config)
        save_long_format_csv(Path(path_str), rows)
        transects = len({row.transect_id for row in rows})
        self._status_label.setText(
            f"Exported collated cover CSV: {len(rows)} rows across {transects} transects."
        )
