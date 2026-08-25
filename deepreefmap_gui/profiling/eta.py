"""Remaining-time estimation for a reconstruction run."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

# Cost driver per coarse stage. The pipeline is strictly sequential, so remaining
# time is the running stage plus the sum of the pending ones. The shapes:
# - preprocess/mapping are per-frame Python loops, so linear in frame count.
# - the cloud replacement radius and ortho cell sort are `np.lexsort`, O(N log N).
# - ortho PCA is a fixed 2-component fit, dominated by an O(N) covariance pass.
FIXED = "fixed"
FRAMES = "frames"
POINTS = "points"
POINTS_NLOGN = "points_nlogn"


@dataclass(frozen=True)
class StageSpec:
    key: str
    label: str  # engineer name, kept for the hover breakdown and timing popup
    driver: str
    weight: float  # relative share used only as a first-run fallback prior
    # Plain phrase for the status line, so a diver reads what is happening rather
    # than a pipeline stage name. The engineer `label` stays for the breakdown.
    plain: str


# Coarse stages the user sees in the breakdown. Weights are the aggregated
# `_RECON_PHASES` shares (gui/runs/progress.py) and are used only when there is no
# per-machine history yet.
STAGES: tuple[StageSpec, ...] = (
    StageSpec("startup", "Startup", FIXED, 1.0, plain="Getting ready"),
    StageSpec("preprocess", "Preprocess", FRAMES, 18.0, plain="Reading the video"),
    StageSpec("mapping", "Mapping", FRAMES, 25.0, plain="Working out the 3D shape"),
    StageSpec("cloud", "Cloud", POINTS_NLOGN, 13.0, plain="Building the point cloud"),
    StageSpec("ortho", "Ortho", POINTS, 22.0, plain="Building the flat map"),
    StageSpec("save_view", "Save + view", POINTS, 7.0, plain="Saving"),
    # The scene .zarr.zip now stores only the cloud index, so this is the index
    # build plus a ~30 MB write. Measured at 0.5s against runs of 186-585s on
    # this machine; it was 3.6-15.8s while the file also carried every frame.
    # Only a first-run prior either way -- recorded history overrides it.
    StageSpec("scene_save", "Scene file", POINTS, 1.0, plain="Preparing for fast reopen"),
)

_STAGE_BY_KEY = {s.key: s for s in STAGES}

# Fine per-step phase keys (gui/runs/progress.py) folded onto the coarse stages above.
_PHASE_TO_STAGE = {
    "startup": "startup",
    "preprocess": "preprocess",
    "mapping": "mapping",
    # Align + resume-save are shown as their own bars on the total, but fold back
    # onto the one learnable "mapping" stage: we have no separate history for them
    # and the coarse status label should stay "Mapping" throughout.
    "mapping_align": "mapping",
    "mapping_save": "mapping",
    "outputs": "cloud",
    "cloud_concat": "cloud",
    "cloud_replace": "cloud",
    "cloud_voxel": "cloud",
    "ortho_pca": "ortho",
    "ortho_sort": "ortho",
    "ortho_aggregate": "ortho",
    "ortho_cover": "ortho",
    "viewer_index_cloud": "save_view",
    "viewer_index_classes": "save_view",
    "viewer_actors": "save_view",
    "viewer_frustums": "save_view",
    "viewer_camera": "save_view",
    "viewer_upload": "save_view",
    "viewer_finalise": "save_view",
    "ortho_save": "save_view",
    "scene_save": "scene_save",
}

# Maps the per-stage `set_stage(stage, status, message)` text to a finer phase
# key. The orchestrator only names four stages (startup, preprocess, mapping,
# outputs) and distinguishes everything inside "outputs" by message, so this is
# the only way to tell the cloud, ortho and save steps apart.
#
# Lives here rather than in runs/progress.py because profiling/instrumentation.py
# needs it to place timing marks and must stay importable without Qt.
STAGE_MESSAGE_TO_PHASE: dict[str, str] = {
    "Aligning poses to world frame": "mapping_align",
    "Saving depth + points for resume": "mapping_save",
    "Mapping complete": "mapping_save",
    "Concatenating point arrays": "cloud_concat",
    "Applying replacement radius": "cloud_replace",
    "Replacement radius: computing voxel keys": "cloud_replace",
    "Replacement radius: sorting points": "cloud_replace",
    "Replacement radius: selecting representatives": "cloud_replace",
    "Reducing by voxel size": "cloud_voxel",
    "Computing PCA projection": "ortho_pca",
    "Sorting points into cells": "ortho_sort",
    "Aggregating ortho grid": "ortho_aggregate",
    "Computing benthic cover": "ortho_cover",
    "Saving semantic cloud": "ortho_save",
    "Saving TSDF cloud": "ortho_save",
    "Saving ortho image": "ortho_save",
    "Saving cover report": "ortho_save",
    "Writing run manifest": "ortho_save",
    "Saving outputs": "ortho_save",
    "Saving scene file": "scene_save",
    "Building geometry cloud": "outputs",
    "Generating outputs": "outputs",
}

# Only trust the live extrapolation once a stage has made enough progress that its
# rate has settled. Extrapolating from 1% done wildly overshoots.
_MIN_FRAC_FOR_LIVE = 0.08
# Over [_MIN_FRAC_FOR_LIVE, _LIVE_HANDOVER_FRAC] the estimate glides from the prior
# to the stage's own measured rate, so it does not snap (e.g. halve) the instant
# the library reports its first real numbers.
_LIVE_HANDOVER_FRAC = 0.4
# A running stage whose fraction is exhausted (the mapping tail after the last
# window, folded sub-phases pinned at 1.0) has no extrapolation left; showing
# "~0s left" there is a lie, so the remainder is withheld instead.
_FRAC_EXHAUSTED = 0.999
# Once a stage has run this many times longer than its prior predicted with no
# measurable progress, the prior is falsified and its remainder is withheld.
_PRIOR_OVERRUN_FACTOR = 1.5
# The live estimate is re-derived on decile boundaries, not on every event: the
# underlying rate does not move that fast, and a figure that changes on each
# report reads as noise rather than as a countdown.
_DECILE = 0.1
# A run's own evidence corrects the stages still to come, but only so far -- one
# stage that hit a cache or thrashed once should not rescale the whole run. Same
# band the batch tracker clamps its cross-pass calibration to.
MIN_CALIBRATION = 0.25
MAX_CALIBRATION = 4.0


def stage_for_phase(phase_key: str) -> str | None:
    """Coarse stage a fine progress phase belongs to, or None if unmapped."""
    return _PHASE_TO_STAGE.get(phase_key)


def stage_plain_label_for_phase(phase_key: str) -> str | None:
    """Plain phrase for the coarse stage a fine phase belongs to (the status line)."""
    stage = _PHASE_TO_STAGE.get(phase_key)
    return _STAGE_BY_KEY[stage].plain if stage else None


def format_duration(seconds: float) -> str:
    """Render a duration as `<1s`, `37s`, `2m 14s`, or `1h 03m`."""
    if 0 < seconds < 1:
        return "<1s"
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def format_remaining(seconds: float) -> str:
    """Render a remainder coarsely, rounded up: `~15s`, `~2m 30s`, `~11m 00s`.

    Estimates carry no second-level precision, so the display shouldn't either;
    the coarse buckets also stop the figure flapping between renders.
    """
    if seconds < 60:
        step = 5
    elif seconds < 600:
        step = 30
    else:
        step = 60
    return f"~{format_duration(max(step, math.ceil(seconds / step) * step))}"


def _nlogn(n: float) -> float:
    return n * math.log(n) if n > 1 else 0.0


def driver_denominator(driver: str, frames: int, points: int | None) -> float | None:
    """Size that a stage's cost scales with, or None when it isn't known yet."""
    # Shared by the live estimator and the history fitter so a stored constant and a
    # live prediction divide by the same quantity.
    if driver == FIXED:
        return 1.0
    if driver == FRAMES:
        # frames == 0 means the count isn't known yet, same as points == None.
        return float(frames) if frames > 0 else None
    if points is None:
        return None
    return float(points) if driver == POINTS else _nlogn(points)


@dataclass
class _StageRun:
    state: str = "pending"  # pending | running | done | skipped
    started_at: float | None = None
    ended_at: float | None = None
    frac: float = 0.0
    frac0: float | None = None  # fraction at the first determinate event
    frac0_at: float | None = None  # wall clock at that first determinate event
    # The (fraction, wall clock) pair the live rate is fitted on, re-latched each
    # time the stage crosses into a new decile and held between crossings.
    rate_frac: float | None = None
    rate_at: float | None = None
    rate_decile: float = -1.0

    def latch_rate(self, now: float) -> None:
        """Re-fit the live rate if this report crossed into a new decile."""
        decile = math.floor(self.frac / _DECILE)
        if decile <= self.rate_decile:
            return
        self.rate_decile = decile
        self.rate_frac = self.frac
        self.rate_at = now

    def elapsed(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, end - self.started_at)

    def earning(self, now: float) -> float:
        """Seconds since the stage began earning fraction.

        Not `elapsed`: a stage can spend minutes on indeterminate sub-steps
        before its first counted event, and charging that head to the fraction
        earned afterwards inflates the measured rate by the ratio of the two.
        """
        if self.frac0_at is None:
            return 0.0
        return max(0.0, now - self.frac0_at)


@dataclass
class StageRow:
    key: str
    label: str
    state: str
    seconds: float | None  # None on a pending stage with no basis yet ("estimating…")
    predicted: bool
    remaining: float | None = None  # live remainder for the running stage
    frac: float = 0.0  # 0..1 fill for the hover bar (done=1, running=live, pending=0)


@dataclass
class RunEtaEstimator:
    """Live remaining-time estimate for one reconstruction run."""

    frames: int
    # Stage key -> seconds per unit of driver, learned from this machine's past runs
    # (run_history.py). Absent keys fall back to weight-based projection.
    priors: dict[str, float] = field(default_factory=dict)
    points: int | None = None
    expected_points: int | None = None
    _runs: dict[str, _StageRun] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._runs = {s.key: _StageRun() for s in STAGES}
        self._order = [s.key for s in STAGES]

    @property
    def has_history(self) -> bool:
        """True when this machine has learned timings for the selected backends."""
        return bool(self.priors)

    def set_points(self, points: int) -> None:
        """Supply the true point count once mapping has produced the cloud."""
        self.points = points

    def update(self, phase_key: str, current: int, total: int, now: float) -> None:
        stage = stage_for_phase(phase_key)
        if stage is None:
            return
        run = self._runs[stage]
        # Any earlier stage is finished the moment a later one reports, since the
        # pipeline is sequential. One still pending never ran at all -- a
        # geometry-only pass reports no ortho -- and is skipped rather than left
        # pending, where its prior would sit in the total for work that will
        # never happen.
        idx = self._order.index(stage)
        for k in self._order[:idx]:
            prev = self._runs[k]
            if prev.state == "running":
                prev.state = "done"
                prev.ended_at = now
            elif prev.state == "pending":
                prev.state = "skipped"
        if run.state in ("pending", "skipped"):
            run.state = "running"
            run.started_at = now
        elif run.state == "done":
            # Reopened: viewer setup arriving after the scene write has started is
            # late, not absent, and dropping it strands the stage with no estimate.
            run.state = "running"
            run.ended_at = None
        if total <= 0:
            # Indeterminate sub-steps (align prep, the resume save) carry no
            # fraction; zeroing frac here would blank the remainder mid-stage.
            return
        # Sub-phases fold onto one stage but count different units (mapping
        # windows, then re-anchor points), so a later sub-phase restarting at 0
        # must not drag the stage's fraction backwards. Same monotonic fill as
        # the visible mapping bar.
        frac = max(run.frac, current / total)
        if run.frac0 is None:
            # The extrapolation baseline: only progress earned after this point
            # counts, so a stage entered mid-slice doesn't divide by unearned frac.
            run.frac0 = frac
            run.frac0_at = now
        run.frac = frac
        run.latch_rate(now)

    def _driver_value(self, spec: StageSpec) -> float | None:
        # Point-driven stages have no true N until mapping ends, so fall back to
        # the historical N from comparable runs; set_points supplies the real one.
        points = self.points if self.points is not None else self.expected_points
        return driver_denominator(spec.driver, self.frames, points)

    def _completed_seconds_per_weight(self, now: float) -> float | None:
        """Seconds-per-weight calibrated from stages already finished this run.

        FIXED-driver stages are excluded: `startup` is model and CUDA loading,
        which relates to no weight and to nothing that follows it.
        """
        num = den = 0.0
        for spec in STAGES:
            if spec.driver == FIXED:
                continue
            run = self._runs[spec.key]
            if run.state == "done":
                num += run.elapsed(now)
                den += spec.weight
        return num / den if den > 0 else None

    def _prior_seconds_per_weight(self) -> float | None:
        """Seconds-per-weight implied by the stored priors, across the stages."""
        ratios: list[float] = []
        for spec in STAGES:
            driver = self._driver_value(spec)
            const = self.priors.get(spec.key)
            if const is not None and driver is not None and spec.weight > 0:
                ratios.append((const * driver) / spec.weight)
        return statistics.median(ratios) if ratios else None

    def _seconds_per_weight(self, now: float) -> float | None:
        """A per-weight rate for stages that lack their own driver-based prior.

        Keeps a point-driven stage from reading 0 before its point count is known.
        Where both a measured and a prior-derived rate exist, the measured one is
        clamped against the prior rather than replacing it outright: one fast
        stage is not evidence that the rest of the run is proportionally fast.
        """
        measured = self._completed_seconds_per_weight(now)
        expected = self._prior_seconds_per_weight()
        if measured is None:
            return expected
        if expected is None:
            return measured
        return min(max(measured, expected * MIN_CALIBRATION), expected * MAX_CALIBRATION)

    def calibration(self, now: float) -> float:
        """What this run is costing against what its priors predicted.

        Applied to the stages still to come, so a machine slower than its stored
        rates does not show a total that rises while work completes. Clamped to
        the same band the batch tracker uses across passes.
        """
        ratios: list[float] = []
        for spec in STAGES:
            run = self._runs[spec.key]
            if run.state != "done":
                continue
            driver = self._driver_value(spec)
            const = self.priors.get(spec.key)
            if const is None or driver is None:
                continue
            predicted = const * driver
            if predicted > 0:
                ratios.append(run.elapsed(now) / predicted)
        if not ratios:
            return 1.0
        return max(MIN_CALIBRATION, min(MAX_CALIBRATION, statistics.median(ratios)))

    def _prior_estimate(self, spec: StageSpec, now: float) -> float | None:
        driver = self._driver_value(spec)
        const = self.priors.get(spec.key)
        if const is not None and driver is not None:
            return const * driver * self.calibration(now)
        spw = self._seconds_per_weight(now)
        if spw is not None:
            return spw * spec.weight
        return None

    def _live_remaining(self, spec: StageSpec, now: float) -> float | None:
        """Remainder from this stage's own throughput, or None if not yet reliable.

        Purely measured, so it is safe to show on a first run. Evaluated against
        the wall clock at query time (average time per unit of earned fraction,
        scaled by the fraction left), so it counts down between sparse progress
        events and grows honestly when the stage stalls, instead of freezing at
        whatever the rate was at the last event.
        """
        run = self._runs[spec.key]
        if run.frac0 is None:
            return None
        # The rate is re-fitted only when the stage crosses into a new decile, so
        # it does not move on every report. Still evaluated against the wall
        # clock at query time, so it counts on between sparse events and grows
        # honestly when the stage stalls rather than freezing at the last rate.
        #
        # `earning`, not `elapsed`: a stage can spend minutes on indeterminate
        # sub-steps before its first counted event, and charging that head to the
        # fraction earned after it inflates the rate by the ratio of the two.
        if run.rate_frac is None:
            return None
        delta = run.rate_frac - run.frac0
        earning = run.earning(now)
        if delta >= _MIN_FRAC_FOR_LIVE and earning > 0:
            return max(0.0, earning * (1.0 - run.frac) / delta)
        return None

    def _live_confidence(self, spec: StageSpec) -> float:
        """0 at the live threshold, ramping to 1 by the handover fraction."""
        run = self._runs[spec.key]
        delta = run.frac - run.frac0 if run.frac0 is not None else 0.0
        if delta <= _MIN_FRAC_FOR_LIVE:
            return 0.0
        if delta >= _LIVE_HANDOVER_FRAC:
            return 1.0
        return (delta - _MIN_FRAC_FOR_LIVE) / (_LIVE_HANDOVER_FRAC - _MIN_FRAC_FOR_LIVE)

    def _running_remaining(self, spec: StageSpec, now: float) -> float | None:
        """Remaining for the running stage: prior first, gliding into the live rate."""
        run = self._runs[spec.key]
        if run.frac >= _FRAC_EXHAUSTED:
            # Folded tail work (align, transfer, saves) after the fraction is
            # spent: no extrapolation left, so no number rather than "~0s left".
            return None
        live = self._live_remaining(spec, now)
        full = self._prior_estimate(spec, now)
        prior = max(0.0, full * (1.0 - run.frac)) if full is not None else None
        if live is None:
            if full is not None and run.elapsed(now) > _PRIOR_OVERRUN_FACTOR * full:
                return None
            return prior
        if prior is None:
            return live
        w = self._live_confidence(spec)
        return w * live + (1.0 - w) * prior

    def running_stage_label(self) -> str | None:
        """Engineer label of the coarse stage currently running, or None if none is."""
        spec = self._running_stage_spec()
        return spec.label if spec is not None else None

    def running_stage_plain_label(self) -> str | None:
        """Plain phrase of the coarse stage currently running, for the status line."""
        spec = self._running_stage_spec()
        return spec.plain if spec is not None else None

    def _running_stage_spec(self) -> StageSpec | None:
        # The furthest-along running stage wins: a late fold-back event (viewer setup
        # arriving after the scene-file save started) can leave an earlier stage
        # marked running, and the pipeline is sequential, so the later one is current.
        for spec in reversed(STAGES):
            if self._runs[spec.key].state == "running":
                return spec
        return None

    def current_stage_remaining(self, now: float) -> float | None:
        """Remainder for whichever stage is running, or None with no signal at all."""
        for spec in STAGES:
            if self._runs[spec.key].state == "running":
                return self._running_remaining(spec, now)
        return None

    def visible_remaining(self, now: float) -> float | None:
        """The whole-run figure for the always-visible total slot, or None."""
        # Withheld without history: the pending stages have no seed, so a total would
        # be a guess masquerading as a countdown. The running stage still shows its
        # own measured figure -- see `current_stage_remaining`.
        return self.total_remaining_s(now) if self.has_history else None

    def learning(self) -> bool:
        """True when this machine has no timings for these models yet.

        The stage figure is still offered -- it is measured from this run -- but
        the whole-run total is not, and the wording has to say which is which.
        """
        return not self.has_history

    def is_finishing(self, now: float) -> bool:
        """True when the run is past every estimate but not yet over.

        A counter reaching its end is not the stage ending: the last write, the
        viewer upload and the scene file all run on past 100%. Saying so beats
        both a countdown that has nothing left to count and a blank.
        """
        for spec in STAGES:
            run = self._runs[spec.key]
            if run.state == "running":
                return self._running_remaining(spec, now) is None or run.frac >= _FRAC_EXHAUSTED
        return False

    def total_remaining_s(self, now: float) -> float | None:
        """The whole run, or None when any stage still to come has no figure.

        A stage yields no figure for three different reasons -- no basis, a
        falsified prior, nothing left to extrapolate -- and only the third is
        worth zero. Summing the other two as zero is how the headline drops by
        minutes with no progress, so the total is withheld instead.
        """
        remaining = 0.0
        for spec in STAGES:
            run = self._runs[spec.key]
            if run.state in ("done", "skipped"):
                continue
            if run.state == "running":
                if run.frac >= _FRAC_EXHAUSTED:
                    # Fraction spent, stage still going: nothing left to add, and
                    # the run reads as finishing rather than as unknown.
                    continue
                part = self._running_remaining(spec, now)
            else:
                part = self._prior_estimate(spec, now)
            if part is None:
                return None
            remaining += part
        return remaining

    def stage_rows(self, now: float) -> list[StageRow]:
        rows: list[StageRow] = []
        for spec in STAGES:
            run = self._runs[spec.key]
            if run.state == "done":
                rows.append(StageRow(spec.key, spec.label, "done", run.elapsed(now), False, frac=1.0))
            elif run.state == "skipped":
                # This mode never ran it; a figure here would be an estimate for
                # work that will not happen.
                rows.append(StageRow(spec.key, spec.label, "skipped", None, False))
            elif run.state == "running":
                rows.append(StageRow(
                    spec.key, spec.label, "running", run.elapsed(now), False,
                    remaining=self._running_remaining(spec, now), frac=run.frac,
                ))
            else:
                # None (no basis yet) is preserved, not coerced to 0, so the popup
                # can render "estimating…" rather than a misleading "0s".
                est = self._prior_estimate(spec, now)
                rows.append(StageRow(spec.key, spec.label, "pending", est, True))
        return rows
