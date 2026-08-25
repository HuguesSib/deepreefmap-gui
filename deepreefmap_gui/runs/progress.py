"""The two progress bars, and the phase weights that make them mean something.

A stage bar shows the step running now, a total bar shows the run. The total is weighted, not a
count of steps, because the steps are nothing like equal: the PCA inside the ortho build can be
most of the wall time on a large reef, while four viewer phases together are seconds. The weights
in `_RECON_PHASES` and `_LOAD_PHASES` are measured shares, which is why the same phase key carries
different weights in each: the scene-file write is a rounding error in a multi-minute run and a
real fraction of a seconds-long load.

Three rules the numbers exist to keep:

- **Forward only.** `ProgressModel` promotes every phase it skipped past and never lowers a
  percent, so a bar cannot rewind when a stage reports out of order.
- **One continuous fill per stage the user recognises.** Mapping and cloud building are each
  several sub-phases; `_subphase_spans` gives each a slice of the stage bar sized by the same
  weights, so an indeterminate tail step holds at its slice start rather than leaving the stage
  pinned at 100% while it is still working.
- **100% means finished.** A phase weighted below the bar's rounding shows the run as complete
  while the last write is still running, which is why `scene_save` carries more than its cost.

The `_*_STAGE_TO_PHASE` tables are the seam with the library: the orchestrator and the run loader
emit their own stage strings, and mapping them here (rather than renaming stages there) keeps a
library change from silently freezing a bar. `profiling/eta.py` reads the same phases for the ETA.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from deepreefmap_gui.core.theme import CARD_HOVER_GUARD_MS, PRIMARY
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.profiling.eta import (
    RunEtaEstimator,
    format_duration,
    format_remaining,
    stage_for_phase,
    stage_plain_label_for_phase,
)


@dataclass
class RunProgress:
    """Where the run in flight has got to, as numbers rather than as widgets.

    Holding it here lets the one funnel in `_apply_progress` feed the queue row,
    the bottom bar and the hover breakdown without knowing which of them exist.
    """

    stage_percent: int | None = None  # None while a step reports no fraction
    total_percent: int = 0
    # Per coarse stage, so a stage reported as several sub-phases reads as one
    # continuous fill rather than several resets.
    stage_fill: dict[str, float] = field(default_factory=dict)


class ProgressModel:
    """Weighted phase model driving the total bar; forward-only, so it never rewinds."""

    def __init__(self, phases: list[tuple[str, float]]) -> None:
        self._phases = phases
        self._idx_by_key = {k: i for i, (k, _) in enumerate(phases)}
        self._total_weight = sum(w for _, w in phases) or 1.0
        self._percents: dict[str, float] = {k: 0.0 for k, _ in phases}
        self._max_idx = -1

    def update(self, key: str, cur: int, tot: int) -> int:
        """Record progress for `key`. Returns the new total percent (0-100)."""
        idx = self._idx_by_key.get(key)
        if idx is not None:
            if idx > self._max_idx:
                # Promote the previously-active phase (if any) and every phase we
                # skipped past, since they're all done. `max(0, ...)` handles the
                # initial state where _max_idx == -1.
                for i in range(max(0, self._max_idx), idx):
                    self._percents[self._phases[i][0]] = 100.0
                self._max_idx = idx
            if tot > 0:
                frac = max(0.0, min(1.0, float(cur) / float(tot)))
                new_pct = 100.0 * frac
                self._percents[key] = max(self._percents[key], new_pct)
        return self.total_percent()

    def total_percent(self) -> int:
        s = sum(self._percents[k] / 100.0 * w for k, w in self._phases)
        return int(round(s / self._total_weight * 100))

    def reset(self) -> None:
        for k in self._percents:
            self._percents[k] = 0.0
        self._max_idx = -1


# ortho_pca carries the biggest individual weight because build_ortho_outputs is
# dominated by sklearn's PCA.fit_transform over the full cloud. On large reefs
# (10M+ points, the 3.5GB dataset) that one step can be ~60% of total wall time.
_RECON_PHASES: list[tuple[str, float]] = [
    ("startup", 1.0),
    ("preprocess", 18.0),
    # Mapping is three real steps the user waits through: window inference, the
    # float64 pose re-anchor, and the resume-npz write. Splitting them stops the
    # bar pinning at 100% for minutes after the last inference window. Shares
    # reflect the measured post-optimisation run (inference ~87s, align ~55s,
    # save now seconds once the npz is uncompressed).
    ("mapping", 15.0),
    ("mapping_align", 8.0),
    ("mapping_save", 2.0),
    ("outputs", 2.0),
    ("cloud_concat", 2.0),
    ("cloud_replace", 10.0),
    ("cloud_voxel", 1.0),
    ("ortho_pca", 12.0),
    ("ortho_sort", 4.0),
    ("ortho_aggregate", 4.0),
    ("ortho_cover", 2.0),
    ("viewer_index_cloud", 1.0),
    ("viewer_index_classes", 4.0),
    ("viewer_actors", 1.0),
    ("viewer_frustums", 3.0),
    ("viewer_camera", 1.0),
    ("viewer_upload", 6.0),
    ("viewer_finalise", 1.0),
    ("ortho_save", 2.0),
    # The scene file is the last write, but no longer a slow one: it holds the
    # cloud index rather than a copy of every frame, so it is ~0.5s against a
    # multi-minute run. Deliberately larger than that share, because the bar
    # rounds to whole percent: below ~2 the total reads 100% while the write is
    # still running, and 100% has to mean finished.
    ("scene_save", 2.0),
]

# Some coarse stages are reported as several sub-phases but must read as one
# continuous 0-100 detail bar (and one estimator fraction), not several resets or
# a pin at 100%. Each sub-phase drives a slice sized by the same weights the total
# bar uses, so the fill flows straight through, and an indeterminate tail step
# holds at its slice start instead of leaving the stage stuck "done" while it runs.

# Mapping: window inference, pose re-anchor, resume save.
_MAPPING_PHASE_KEYS: tuple[str, ...] = ("mapping", "mapping_align", "mapping_save")

# Cloud: the per-frame unprojection loop (reported as "outputs") then the tail of
# concatenate, replacement-radius lexsort, voxel reduce. Without this split the
# cheap per-frame loop drove the stage to 100% and the long lexsort read "~0s
# left"; the tail owns most of the weight, matching where the wall time goes.
_CLOUD_PHASE_KEYS: tuple[str, ...] = ("outputs", "cloud_concat", "cloud_replace", "cloud_voxel")

# The per-item loop that legitimately carries a `cur/tot`; the other sub-phases
# report raw point totals or nothing and would read as noise on the status line.
_COUNTED_SUBPHASES: frozenset[str] = frozenset({"mapping", "outputs"})


def _subphase_spans(keys: tuple[str, ...]) -> dict[str, tuple[float, float]]:
    weights = dict(_RECON_PHASES)
    total = sum(weights[k] for k in keys) or 1.0
    spans: dict[str, tuple[float, float]] = {}
    acc = 0.0
    for key in keys:
        share = weights[key] / total
        spans[key] = (acc, acc + share)
        acc += share
    return spans


# Ortho: PCA, the cell lexsort, the grid aggregate, the cover tally. Every one
# of these reports by message with no count, so without spans the stage never
# earns a fraction at all and its countdown is a frozen constant.
_ORTHO_PHASE_KEYS: tuple[str, ...] = ("ortho_pca", "ortho_sort", "ortho_aggregate", "ortho_cover")

# The writes, then the viewer's own setup, in emission order. Several of these
# report n/n; without spans the first one to do so pins the whole stage at 1.0
# and the rest of the viewer setup runs with no estimate.
_VIEWER_PHASE_KEYS: tuple[str, ...] = (
    "ortho_save",
    "viewer_index_cloud",
    "viewer_index_classes",
    "viewer_actors",
    "viewer_frustums",
    "viewer_camera",
    "viewer_upload",
    "viewer_finalise",
)

_MAPPING_SUBPHASE_SPANS = _subphase_spans(_MAPPING_PHASE_KEYS)
_CLOUD_SUBPHASE_SPANS = _subphase_spans(_CLOUD_PHASE_KEYS)
_ORTHO_SUBPHASE_SPANS = _subphase_spans(_ORTHO_PHASE_KEYS)
_VIEWER_SUBPHASE_SPANS = _subphase_spans(_VIEWER_PHASE_KEYS)
# One lookup over every fine phase shown as part of a continuous stage fill.
_SUBPHASE_SPANS = {
    **_MAPPING_SUBPHASE_SPANS,
    **_CLOUD_SUBPHASE_SPANS,
    **_ORTHO_SUBPHASE_SPANS,
    **_VIEWER_SUBPHASE_SPANS,
}


# cloud_concat / cloud_replace / cloud_voxel are the silent post-frame steps
# inside build_semantic_reference_cloud (concatenate, replacement-radius
# lexsort, optional voxel reduce). On a 3.5GB dataset cloud_replace alone is
# multi-second wall time, hence the chunky weight. The ortho_* phases come
# from the live ortho preview built at the end of _apply_loaded_run.
_LOAD_PHASES: list[tuple[str, float]] = [
    ("manifest", 1.0),
    ("mapping_load", 6.0),
    ("frames_load", 18.0),
    ("cloud_build", 15.0),
    ("cloud_concat", 3.0),
    ("cloud_replace", 12.0),
    ("cloud_voxel", 2.0),
    ("ortho_pca", 8.0),
    ("ortho_sort", 2.0),
    ("ortho_aggregate", 1.0),
    ("ortho_cover", 1.0),
    ("viewer_index_cloud", 2.0),
    ("viewer_index_classes", 5.0),
    ("viewer_actors", 1.0),
    ("viewer_frustums", 4.0),
    ("viewer_camera", 1.0),
    ("viewer_upload", 17.0),
    ("viewer_finalise", 1.0),
    # Only runs made before the pipeline started writing its own scene file reach
    # this; for those it is the last thing between "loaded" and idle, and it runs
    # after the viewer is up rather than alongside it. Weighted far higher than
    # its counterpart on the reconstruction bar for the same ~0.5s of work: a
    # load is seconds, not minutes, so the same write is a real share of it.
    ("scene_save", 8.0),
]

# Stages `save_scene_file` emits, all folded onto the one scene_save phase so the
# write reads as a single continuous fill. `scene_frames` is the per-frame tick
# that gives the bar (and the ETA's measured rate) its resolution.
_SCENE_SAVE_STAGES: tuple[str, ...] = (
    "scene_index",
    "scene_meta",
    "scene_fci",
    "scene_done",
)

# Maps setup_progress messages from qt_viewer to phase keys.
_SETUP_MESSAGE_TO_PHASE: dict[str, str] = {
    "Indexing point cloud": "viewer_index_cloud",
    "Indexing cloud": "viewer_index_cloud",
    "Indexing classes": "viewer_index_classes",
    "Preparing class actors": "viewer_actors",
    "Building camera frustums": "viewer_frustums",
    "Fitting camera": "viewer_camera",
    "Uploading class points": "viewer_upload",
    "Finalising viewer": "viewer_finalise",
}

# Maps view-run loader stage strings to phase keys. The `cloud_*` variants
# are emitted by run_loader's stage_cb after the per-frame loop reports
# N/N, so the bars don't freeze during concatenation / replacement /
# voxelization.
_LOAD_STAGE_TO_PHASE: dict[str, str] = {
    "manifest": "manifest",
    "classes": "manifest",
    "mapping": "mapping_load",
    "frames": "frames_load",
    "cloud": "cloud_build",
    "cloud_concatenating": "cloud_concat",
    "cloud_replacing": "cloud_replace",
    # The replacement-radius lexsort is the dominant cost of cloud_replace
    # on multi-million-point clouds; route its sub-steps to the same phase
    # so the total bar reflects them under cloud_replace's weight.
    "cloud_replacing_keys": "cloud_replace",
    "cloud_replacing_sort": "cloud_replace",
    "cloud_replacing_select": "cloud_replace",
    "cloud_voxelizing": "cloud_voxel",
    "geometry": "cloud_build",
    **dict.fromkeys(_SCENE_SAVE_STAGES, "scene_save"),
}


class ProgressBarsMixin(MixinBase):
    """DeepReefMapWindow methods that drive the per-step + unified progress bars."""

    def _ensure_status_tick_timer(self) -> QTimer:
        # Mixins have no __init__, so the ticker is created lazily on first use.
        timer = getattr(self, "_status_tick_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(1000)
            timer.timeout.connect(self._render_status)
            self._status_tick_timer = timer
        return timer

    def _ensure_timing_popup(self):
        popup = getattr(self, "_timing_popup", None)
        if popup is None:
            from deepreefmap_gui.core.hover_card import install_dismiss_filter
            from deepreefmap_gui.runs.timing_popup import TimingPopup

            popup = TimingPopup(self)
            self._timing_popup = popup
            install_dismiss_filter(self._survey_pass_table, self._hide_timing_popup)
        return popup

    def _ensure_timing_guard_timer(self) -> QTimer:
        """The poll that takes the breakdown down when no Leave arrives."""
        timer = getattr(self, "_timing_guard_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(CARD_HOVER_GUARD_MS)
            timer.timeout.connect(self._guard_timing_popup)
            self._timing_guard_timer = timer
        return timer

    def _guard_timing_popup(self) -> None:
        """Hide the breakdown once the cursor is off the table it describes."""
        from deepreefmap_gui.core.hover_card import cursor_on

        popup = getattr(self, "_timing_popup", None)
        table = self._survey_pass_table
        if popup is None or not popup.isVisible():
            self._ensure_timing_guard_timer().stop()
            return
        if not table.window().isActiveWindow() or not cursor_on(QCursor.pos(), table):
            self._hide_timing_popup()

    def _progress_sinks(self) -> list:
        """Every widget mirroring the run in flight.

        The viewer's placeholder panel has one, and the Run
        step's batch card, and a window under construction has neither.
        """
        return [
            sink
            for sink in (
                getattr(self, "_progress_panel", None),
                getattr(self, "_batch_progress", None),
            )
            if sink is not None
        ]

    def _begin_progress(self, model: ProgressModel, spec=None) -> None:
        """Switch the active progress model and start the run from zero.

        `spec` is the PassSpec of the queued pass about to run, when there is
        one. Without it the estimator reads the form, which a batch has already
        restored to the session's values.
        """
        model.reset()
        self._active_progress_model = model
        self._run_progress = RunProgress()
        self._set_progress_widgets_visible(True)
        self._status_base_text = ""
        self._status_count_text = ""
        self._status_phase_key = None
        self._status_stage_key = None
        self._status_phase_started = time.monotonic()
        # ETA only applies to a reconstruction; a cached-run load has its own model.
        self._eta = self._new_run_estimator(spec) if model is self._recon_model else None
        self._ensure_status_tick_timer().start()

    def _form_pass_spec(self):
        """A PassSpec for the run the form is currently describing."""
        from deepreefmap_gui.profiling.batch_estimate import PassSpec
        from deepreefmap_gui.profiling.run_history import seg_key

        return PassSpec(
            key="form",
            frames=0,
            mapping_backend=self._map_combo.currentText(),
            seg_model=seg_key(
                self._seg_combo.currentText(),
                bool(getattr(self, "_skip_seg_check", None) and self._skip_seg_check.isChecked()),
            ),
            width=self._proc_width_spin.value(),
            height=self._proc_height_spin.value(),
            fps=self._fps_spin.value(),
        )

    def _new_run_estimator(self, spec=None) -> RunEtaEstimator:
        """Estimator seeded from this machine's history for the pass about to run."""
        from deepreefmap_gui.profiling.batch_estimate import expected_points_for
        from deepreefmap_gui.profiling.run_history import history_key, load_priors

        try:
            if spec is None:
                spec = self._form_pass_spec()
            key = history_key(
                spec.mapping_backend, spec.seg_model, spec.width, spec.height, spec.fps
            )
            priors = load_priors(key)
            # Scaled to this pass's length: an unscaled median prices the
            # point-driven stages of a long pass as a median-length one.
            expected_points = expected_points_for(key, spec.frames)
            frames = spec.frames
        except Exception:
            priors = {}
            expected_points = None
            frames = 0
        return RunEtaEstimator(frames=frames, priors=priors, expected_points=expected_points)

    def _set_progress_widgets_visible(self, visible: bool) -> None:
        """Progress readouts belong to a run in flight; idle shows none of them."""
        self._eta_total_label.setVisible(visible)
        self._bottom_progress_bar.setVisible(visible)

    def _reset_progress(self) -> None:
        self._run_progress = RunProgress()
        self._bottom_progress_bar.setValue(0)
        self._eta_total_label.setText("")
        self._set_progress_widgets_visible(False)
        self._active_progress_model = None
        self._eta = None
        popup = getattr(self, "_timing_popup", None)
        if popup is not None:
            popup.hide()
        timer = getattr(self, "_status_tick_timer", None)
        if timer is not None:
            timer.stop()
        self._status_base_text = ""
        self._status_count_text = ""
        self._status_phase_key = None
        self._status_stage_key = None
        for sink in self._progress_sinks():
            sink.set_idle("No run in progress.")

    def _render_status(self) -> None:
        """Recompose the status label: colored stage + label, then a metrics line."""
        self._render_eta()
        base = getattr(self, "_status_base_text", "")
        if not base:
            return
        now = time.monotonic()
        started = getattr(self, "_status_phase_started", None)
        # Second line: count · stage elapsed · stage remainder. Kept off the
        # first line so the label text never jumps as the numbers grow. The
        # remainder is prior-first then live (same blend as the total), and
        # stage-scoped so it never masquerades as the whole-run total.
        parts: list[str] = []
        count = getattr(self, "_status_count_text", "")
        if count:
            parts.append(count)
        if started is not None:
            parts.append(format_duration(now - started))
        est = getattr(self, "_eta", None)
        stage_left = est.current_stage_remaining(now) if est is not None else None
        if stage_left is not None:
            parts.append(f"{format_remaining(stage_left)} left")
        elif est is not None and est.is_finishing(now):
            # The counter is spent but the stage is not: the last write, the
            # viewer upload and the scene file all run on past 100%.
            parts.append("finishing")
        metrics = " · ".join(parts)
        # Color the active coarse stage so the left text names it (and the stage
        # name is dropped from the bars). Plain-language here so the diver reads
        # what is happening. The engineer stage names stay in the hover breakdown.
        # During a reconstruction take it from the estimator's running stage
        # (monotonic), so a late viewer-setup event can't regress the token to a
        # finished stage while a later save is still running. Fall back to the fine
        # phase key when there is no estimator (cached load).
        stage = est.running_stage_plain_label() if est is not None else None
        if not stage:
            stage = stage_plain_label_for_phase(getattr(self, "_status_phase_key", "") or "")
        if stage:
            first = f'<b><span style="color:{PRIMARY}">{stage}</span></b> · {base}'
        else:
            first = base
        text = f"{first}<br>{metrics}" if metrics else first
        self._status_label.setText(text)
        for sink in self._progress_sinks():
            sink.set_status_html(text)

    def _render_eta(self) -> None:
        """Refresh the visible overall-estimate label and the breakdown popup."""
        est = getattr(self, "_eta", None)
        if est is None:
            return
        now = time.monotonic()
        visible = est.visible_remaining(now)
        # Overall estimate shown plainly rather than buried in the hover. Without
        # a figure, say which of the three reasons applies: no timings for these
        # models on this machine, a run past every estimate but not yet over, or
        # a stage the run has yet to give anything to go on.
        if visible is not None:
            eta_text = f"{format_remaining(visible)} left"
        elif est.learning():
            eta_text = "no timings for these models yet — learning from this run"
        elif est.is_finishing(now):
            eta_text = "finishing…"
        else:
            eta_text = "estimating…"
        self._eta_total_label.setText(eta_text)
        for sink in self._progress_sinks():
            sink.set_eta(eta_text)
            sink.set_eta_seconds(visible)
        popup = getattr(self, "_timing_popup", None)
        if popup is not None and popup.isVisible():
            popup.set_rows(est.stage_rows(now), est.total_remaining_s(now), est.has_history)

    def _hide_timing_popup(self) -> None:
        timer = getattr(self, "_timing_guard_timer", None)
        if timer is not None:
            timer.stop()
        popup = getattr(self, "_timing_popup", None)
        if popup is not None:
            popup.hide()

    def _on_queue_row_hover(self, table_row: int, global_rect) -> None:
        """Show the stage-by-stage breakdown for the row being processed.

        Only that row: the breakdown describes the run in flight, and shown
        against a queued or finished row it would be a plausible reading of the
        wrong pass, which is worse than showing nothing.
        """
        est = getattr(self, "_eta", None)
        if est is None or global_rect is None or table_row < 0:
            self._hide_timing_popup()
            return
        if table_row != self._running_table_row():
            self._hide_timing_popup()
            return
        popup = self._ensure_timing_popup()
        now = time.monotonic()
        popup.set_rows(est.stage_rows(now), est.total_remaining_s(now), est.has_history)
        self._anchor_timing_popup(global_rect)
        popup.show()
        self._ensure_timing_guard_timer().start()

    def _anchor_timing_popup(self, global_rect) -> None:
        """Place the breakdown against the row, and on the screen."""
        from deepreefmap_gui.core.hover_card import place_near_widget

        popup = self._ensure_timing_popup()
        place_near_widget(
            popup, self._survey_pass_table, anchor_rect=global_rect, prefer="below"
        )

    def _apply_progress(
        self,
        phase_key: str,
        label: str,
        current: int = 0,
        total: int = 0,
        flush: bool = False,
    ) -> None:
        """Update the per-step bar/label and the unified total bar."""
        # Reset the stage stopwatch when the coarse stage changes, so elapsed is
        # scoped to the same stage as the remainder beside it and the elapsed the
        # hover breakdown reports. Restarting it per fine phase made one line
        # carry two different scopes and read as one countdown.
        now = time.monotonic()
        self._status_phase_key = phase_key
        coarse_key = stage_for_phase(phase_key) or phase_key
        if getattr(self, "_status_stage_key", None) != coarse_key:
            self._status_stage_key = coarse_key
            self._status_phase_started = now

        # Mapping and cloud fold several sub-phases into one monotonic 0-100 fill
        # sized by weight, computed once so the detail bar and hover breakdown agree.
        # An indeterminate tail step then holds at its slice start instead of pinning
        # the stage at 100% and reading "~0s left". Per coarse stage, so cloud does
        # not inherit mapping's finished fill.
        span = _SUBPHASE_SPANS.get(phase_key)
        stage_combined: float | None = None
        if span is not None:
            coarse = stage_for_phase(phase_key) or phase_key
            lo, hi = span
            within = min(1.0, current / total) if total > 0 else 0.0
            combined = 100.0 * (lo + (hi - lo) * within)
            fills = self._run_progress.stage_fill
            stage_combined = max(fills.get(coarse, 0.0), combined)
            fills[coarse] = stage_combined

        est = getattr(self, "_eta", None)
        if est is not None and stage_for_phase(phase_key) is not None:
            # The preprocess total is the selected frame count, the size the
            # per-frame stages scale with; capture it for pending predictions.
            # `> 1` and latched: the stage-completed pulse reports 1/1, not the
            # frame count, and arrives just as mapping is about to be priced.
            if phase_key == "preprocess" and total > 1:
                est.frames = max(est.frames, total)
            if stage_combined is not None:
                # Feed the estimator the same combined fill the detail bar shows
                # (0-100), not the raw per-sub-phase fraction. Otherwise the hover
                # bar snaps to 100% when the determinate loop ends and its remainder
                # reads 0s while the indeterminate tail still runs.
                est.update(phase_key, int(round(stage_combined)), 100, now)
            else:
                est.update(phase_key, current, total, now)

        # Stage fill as a percentage, or None where the step reports no fraction
        # at all. The frame count is kept out of the base text so it can sit on
        # the metrics line rather than in the middle of the label.
        run = self._run_progress
        if stage_combined is not None:
            run.stage_percent = int(round(stage_combined))
            self._status_base_text = label
            # Only the per-item loop carries a meaningful count; the tail sub-phases
            # report raw point totals or nothing and would read as noise.
            self._status_count_text = (
                f"{current}/{total}" if phase_key in _COUNTED_SUBPHASES and total > 1 else ""
            )
        elif total > 1:
            run.stage_percent = int(round(100.0 * current / total))
            self._status_base_text = label
            self._status_count_text = f"{current}/{total}"
        elif total == 1:
            run.stage_percent = 100
            self._status_base_text = label
            self._status_count_text = ""
        else:
            run.stage_percent = None
            self._status_base_text = label
            self._status_count_text = ""
        self._render_status()

        if self._active_progress_model is not None:
            pct = self._active_progress_model.update(
                phase_key,
                current if total > 0 else 0,
                total if total > 0 else 1,
            )
            self._run_progress.total_percent = pct
            self._bottom_progress_bar.setValue(pct)
            for sink in self._progress_sinks():
                sink.set_percent(pct)

        if flush:
            QApplication.processEvents()
