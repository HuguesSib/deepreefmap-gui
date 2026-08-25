"""How long a session of queued passes will take, before any of it has run.

Each pass is predicted from its own frame count and this machine's learned
per-stage rates, then summed. `RunEtaEstimator` is the engine: given frames,
priors and an expected point count, its remaining time before any progress has
been reported is the sum of every stage's prior estimate.

The ladder below ends in no number rather than an invented one. A machine with
no history for the chosen models has no basis for a figure that would read like
a measurement.

Qt-free, so the prediction can be tested without a window.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from deepreefmap_gui.profiling.eta import FRAMES, STAGES, RunEtaEstimator
from deepreefmap_gui.profiling.run_history import (
    ProfileEntry,
    history_key,
    load_expected_frames,
    load_expected_points,
    load_priors,
    load_profile_entries,
)

# How the seconds for a pass were arrived at. Carried so the caller can hedge its
# wording: a figure scaled from another resolution deserves less confidence than
# one measured at the settings about to be used.
BASIS_EXACT = "exact"
BASIS_SCALED = "scaled"
BASIS_PER_FRAME = "per_frame"
BASIS_NONE = "none"

# Below this a pass has not run long enough for its own progress to say anything
# about how the rest of the batch will go.
_MIN_FRACTION_TO_CALIBRATE = 0.05

# A finished pass corrects the passes still to come, but only so far: one pass
# that hit a cached frame set or thrashed once should not rescale the evening.
_MIN_CALIBRATION = 0.25
_MAX_CALIBRATION = 4.0


@dataclass(frozen=True)
class PassSpec:
    """What one queued pass will run, in the terms its cost depends on."""

    key: str
    frames: int
    mapping_backend: str
    seg_model: str
    width: int
    height: int
    fps: int

    @property
    def pixels(self) -> int:
        return max(1, self.width * self.height)


@dataclass(frozen=True)
class PassPrediction:
    key: str
    seconds: float | None
    basis: str


@dataclass(frozen=True)
class BatchPrediction:
    passes: list[PassPrediction]
    total_s: float | None
    predicted_count: int
    unknown_count: int

    def seconds_for(self, key: str) -> float | None:
        return next((p.seconds for p in self.passes if p.key == key), None)

    def coverage_clause(self) -> str | None:
        """How much of the queue the total actually covers, or None if all of it.

        Carried beside every figure derived from `total_s`: a partial sum shown
        as the whole answer reads as a shorter evening than the one ahead.
        """
        if not self.unknown_count:
            return None
        return f"covering {self.predicted_count} of {len(self.passes)} passes"


def coverage_clause(predicted: int, total: int) -> str | None:
    """The same clause for a live remainder, which counts only pending passes."""
    if predicted >= total:
        return None
    return f"covering {predicted} of {total} passes"


def expected_points_for(key: str, frames: int, *, path: Path | None = None) -> int | None:
    """The stored point count, scaled to a pass of `frames`.

    The median is taken over runs of whatever length, so feeding it in unscaled
    prices the four point-driven stages of a long pass as a median-length one.
    Scaled the way the donor branch already scales (`_scaled_points`).
    """
    points = load_expected_points(key, path)
    if points is None:
        return None
    recorded = load_expected_frames(key, path)
    if not recorded or frames <= 0:
        return points
    return int(points * frames / recorded)


def _predict_from(
    spec: PassSpec, priors: dict[str, float], expected_points: int | None
) -> float | None:
    """A whole run from priors alone: every stage's estimate, nothing measured yet."""
    estimator = RunEtaEstimator(
        frames=spec.frames, priors=priors, expected_points=expected_points
    )
    return estimator.total_remaining_s(0.0)


def _nearest_donor(spec: PassSpec, entries: list[ProfileEntry]) -> ProfileEntry | None:
    """The recorded configuration closest to this one, on the same models.

    Closest by resolution first and frame rate second: the per-frame stages scale
    with pixels, so a donor at another size is a rescaling, while one at another
    frame rate is mostly the same work per frame.
    """
    same = [
        entry
        for entry in entries
        if entry.mapping_backend == spec.mapping_backend and entry.seg_model == spec.seg_model
    ]
    if not same:
        return None
    return min(
        same,
        key=lambda e: (
            abs(math.log(e.pixels / spec.pixels)),
            abs(e.fps - spec.fps),
        ),
    )


def _scaled_priors(spec: PassSpec, donor: ProfileEntry) -> dict[str, float]:
    """The donor's rates, with the per-frame ones moved to this resolution.

    Only the frame-driven stages are rescaled. The point-driven ones are already
    expressed per point, and the point count is scaled instead -- rescaling both
    would apply the same ratio twice.
    """
    ratio = spec.pixels / donor.pixels
    frame_driven = {s.key for s in STAGES if s.driver == FRAMES}
    return {
        key: value * ratio if key in frame_driven else value
        for key, value in donor.priors.items()
    }


def _scaled_points(spec: PassSpec, donor: ProfileEntry) -> int | None:
    if donor.points is None or not donor.frames:
        return None
    per_frame = donor.points / donor.frames
    return int(per_frame * spec.frames * (spec.pixels / donor.pixels))


def _per_frame_seconds(spec: PassSpec, path: Path | None) -> float | None:
    """The coarsest rung: what a frame has cost on this machine, however measured."""
    from deepreefmap_gui.profiling.run_history import group_recorded_runs

    rows = [r for r in group_recorded_runs(path) if r.get("seconds_per_frame")]
    if not rows:
        return None
    same = [
        r
        for r in rows
        if (r.get("params") or {}).get("mapping_backend") == spec.mapping_backend
        and (r.get("params") or {}).get("segmentation_model") == spec.seg_model
    ]
    # Same models only. A per-frame rate borrowed from another backend or
    # segmenter is not an approximation of this pass, it is a different
    # measurement wearing this pass's label -- and it was shown at the same
    # confidence as one taken at these very settings.
    if not same:
        return None
    return statistics.median(float(r["seconds_per_frame"]) for r in same)


def predict_pass_seconds(spec: PassSpec, *, path: Path | None = None) -> PassPrediction:
    """What this pass should cost, and how confidently that was arrived at."""
    if spec.frames <= 0:
        return PassPrediction(spec.key, None, BASIS_NONE)

    key = history_key(spec.mapping_backend, spec.seg_model, spec.width, spec.height, spec.fps)
    priors = load_priors(key, path)
    if priors:
        seconds = _predict_from(spec, priors, expected_points_for(key, spec.frames, path=path))
        if seconds is not None:
            return PassPrediction(spec.key, seconds, BASIS_EXACT)

    donor = _nearest_donor(spec, load_profile_entries(path))
    if donor is not None:
        seconds = _predict_from(
            spec, _scaled_priors(spec, donor), _scaled_points(spec, donor)
        )
        if seconds is not None:
            return PassPrediction(spec.key, seconds, BASIS_SCALED)

    per_frame = _per_frame_seconds(spec, path)
    if per_frame:
        return PassPrediction(spec.key, per_frame * spec.frames, BASIS_PER_FRAME)

    return PassPrediction(spec.key, None, BASIS_NONE)


def predict_batch(specs: list[PassSpec], *, path: Path | None = None) -> BatchPrediction:
    """Every queued pass, and the total of the ones there is a basis for.

    The total covers the predicted passes only, and the count of the others is
    carried beside it: a partial sum presented as the whole answer would read as
    a shorter evening than the one ahead.
    """
    predictions = [predict_pass_seconds(spec, path=path) for spec in specs]
    known = [p.seconds for p in predictions if p.seconds is not None]
    return BatchPrediction(
        passes=predictions,
        total_s=sum(known) if known else None,
        predicted_count=len(known),
        unknown_count=len(predictions) - len(known),
    )


class BatchEtaTracker:
    """The batch's remaining time, corrected by what the batch itself measures.

    A prediction made before the run is the starting point, not the answer. As
    passes finish, the ratio of what they actually cost to what was predicted
    rescales the passes still to come -- once, at a pass boundary, rather than
    continuously, so the total does not lurch every time a progress event lands.
    """

    def __init__(self, prediction: BatchPrediction) -> None:
        self._prediction = prediction
        self._order = [p.key for p in prediction.passes]
        self._index: int | None = None
        self._actual: dict[str, float] = {}
        self._ratios: list[float] = []
        self._pass_percent = 0
        self._pass_remaining_s: float | None = None

    @property
    def calibration(self) -> float:
        """What this machine is actually costing, against what was predicted."""
        if not self._ratios:
            return 1.0
        return max(_MIN_CALIBRATION, min(_MAX_CALIBRATION, statistics.median(self._ratios)))

    def start_pass(self, index: int) -> None:
        self._index = index
        self._pass_percent = 0
        self._pass_remaining_s = None

    def finish_pass(self, index: int, seconds: float) -> None:
        """Fold what a finished pass really cost into the calibration."""
        if not 0 <= index < len(self._order):
            return
        key = self._order[index]
        self._actual[key] = seconds
        predicted = self._prediction.seconds_for(key)
        if predicted:
            self._ratios.append(seconds / predicted)

    def set_pass_percent(self, percent: int) -> None:
        self._pass_percent = max(0, min(100, percent))

    def set_pass_remaining(self, remaining_s: float | None) -> None:
        """The run's own live figure for the pass in flight, when it has one."""
        # Separate from the percent because both arrive on the same progress
        # event: folded into one setter, whichever landed second wiped the other
        # and the batch line alternated between two figures every tick.
        if remaining_s is not None:
            self._pass_remaining_s = remaining_s

    def set_pass_progress(self, percent: int, remaining_s: float | None) -> None:
        self.set_pass_percent(percent)
        self.set_pass_remaining(remaining_s)

    def pending_coverage(self) -> tuple[int, int]:
        """How many of the passes still to run the remainder actually covers."""
        keys = self._order if self._index is None else self._order[self._index :]
        priced = sum(1 for key in keys if self._prediction.seconds_for(key) is not None)
        return priced, len(keys)

    def remaining_s(self) -> float | None:
        """Seconds left across the pass in flight and every pass after it.

        Covers the passes there is a basis for; `pending_coverage` says how many
        that is, so the shortfall is stated rather than summed as zero.
        """
        if self._index is None:
            return self._scaled_total(self._order)
        pending = self._order[self._index + 1 :]
        rest = self._scaled_total(pending)
        current = self._current_remaining()
        if current is None:
            return rest
        return current + (rest or 0.0)

    def _current_remaining(self) -> float | None:
        """The running pass, live if it has said enough, predicted until then."""
        if self._pass_remaining_s is not None:
            return self._pass_remaining_s
        if self._index is None or self._index >= len(self._order):
            return None
        predicted = self._prediction.seconds_for(self._order[self._index])
        if predicted is None:
            return None
        scaled = predicted * self.calibration
        if self._pass_percent >= 100 * _MIN_FRACTION_TO_CALIBRATE:
            return scaled * (1.0 - self._pass_percent / 100.0)
        return scaled

    def _scaled_total(self, keys: list[str]) -> float | None:
        factor = self.calibration
        known = [
            self._prediction.seconds_for(key) for key in keys
        ]
        present = [value * factor for value in known if value is not None]
        return sum(present) if present else None
