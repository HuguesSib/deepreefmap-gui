"""Predicting a session before it runs, and correcting it while it does.

Scenario: a queue of sections, cut to different lengths and possibly running
different models, on a laptop that may or may not have processed anything like
them before.

Expected behaviour: each pass is costed from its own frame count and this
machine's learned rates, and where there is no basis the answer is no number
rather than a plausible one.
"""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.profiling.batch_estimate import (
    BASIS_EXACT,
    BASIS_NONE,
    BASIS_SCALED,
    BatchEtaTracker,
    PassSpec,
    predict_batch,
    predict_pass_seconds,
)
from deepreefmap_gui.profiling.run_history import history_key

_BACKEND = "loger_star"
_SEG = "coralscapes-vit-b-dpt"


def spec(key="a", frames=1000, width=1376, height=768, fps=5, backend=_BACKEND, seg=_SEG):
    return PassSpec(
        key=key,
        frames=frames,
        mapping_backend=backend,
        seg_model=seg,
        width=width,
        height=height,
        fps=fps,
    )


def write_profile(path, *runs):
    """A timings file holding one recorded run per entry given."""
    profile: dict[str, list[dict]] = {}
    for run in runs:
        key = history_key(
            run["backend"], run["seg"], run["width"], run["height"], run["fps"]
        )
        profile.setdefault(key, []).append(
            {
                "version": 1,
                "stage_durations": run["durations"],
                "frames": run["frames"],
                "points": run.get("points", 2_000_000),
                "params": {
                    "mapping_backend": run["backend"],
                    "segmentation_model": run["seg"],
                    "processing_width": run["width"],
                    "processing_height": run["height"],
                    "fps": run["fps"],
                },
                "stage_peaks": run.get("peaks", {"mapping": {"ram_bytes": 8 * 1024**3}}),
            }
        )
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def a_run(**kw):
    run = {
        "backend": _BACKEND,
        "seg": _SEG,
        "width": 1376,
        "height": 768,
        "fps": 5,
        "frames": 1000,
        "durations": {
            "startup": 4.0,
            "preprocess": 120.0,
            "mapping": 200.0,
            "cloud": 60.0,
            "ortho": 90.0,
            "save_view": 30.0,
            "scene_save": 2.0,
        },
    }
    run.update(kw)
    return run


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "run_timings.json"


def test_a_machine_with_no_history_says_so_rather_than_guessing(profile):
    """Zero would read as instant, and a made-up figure as a measurement."""
    prediction = predict_batch([spec("a"), spec("b")], path=profile)

    assert prediction.total_s is None
    assert prediction.predicted_count == 0
    assert prediction.unknown_count == 2
    assert all(p.basis == BASIS_NONE for p in prediction.passes)


def test_a_recorded_config_predicts_its_own_settings_exactly(profile):
    write_profile(profile, a_run())

    got = predict_pass_seconds(spec(frames=1000), path=profile)

    assert got.basis == BASIS_EXACT
    # The recorded run totalled 506s over its stages; the same shape costs the same.
    assert got.seconds == pytest.approx(506.0, rel=0.1)


def test_a_longer_pass_costs_more_than_a_shorter_one(profile):
    """The whole reason for this: a queue is not N copies of a median run."""
    write_profile(profile, a_run())

    short = predict_pass_seconds(spec(key="s", frames=200), path=profile).seconds
    long = predict_pass_seconds(spec(key="l", frames=4000), path=profile).seconds

    assert short is not None and long is not None
    assert long > short * 4


def test_another_resolution_is_scaled_from_the_nearest_recorded_one(profile):
    write_profile(profile, a_run())

    got = predict_pass_seconds(spec(width=688, height=384), path=profile)

    assert got.basis == BASIS_SCALED
    # A quarter of the pixels, so the per-frame stages cost less than at full size.
    full = predict_pass_seconds(spec(), path=profile).seconds
    assert got.seconds is not None and full is not None
    assert got.seconds < full


def test_a_different_model_is_not_costed_from_another_models_runs(profile):
    """A per-frame rate from another backend is a different measurement.

    Not an approximation of this pass, and it was shown at the same confidence
    as one taken at these very settings.
    """
    write_profile(profile, a_run())

    got = predict_pass_seconds(spec(backend="scsfmlearner", seg="segformer-b2"), path=profile)

    assert got.basis == BASIS_NONE
    assert got.seconds is None


def test_a_batch_totals_what_it_can_and_counts_what_it_cannot(profile):
    """A partial sum shown as the whole answer reads as a shorter evening."""
    write_profile(profile, a_run())
    known = spec(key="known", frames=1000)
    # An empty window has nothing to cost, whatever the history says.
    unknown = spec(key="unknown", frames=0)

    prediction = predict_batch([known, unknown], path=profile)

    assert prediction.predicted_count == 1
    assert prediction.unknown_count == 1
    assert prediction.total_s == pytest.approx(prediction.seconds_for("known"))


# --- Correcting the estimate while the batch runs ---


def make_tracker(seconds_each=(100.0, 100.0, 100.0)):
    from deepreefmap_gui.profiling.batch_estimate import BatchPrediction, PassPrediction

    passes = [
        PassPrediction(key=str(i), seconds=value, basis=BASIS_EXACT)
        for i, value in enumerate(seconds_each)
    ]
    return BatchEtaTracker(
        BatchPrediction(
            passes=passes,
            total_s=sum(seconds_each),
            predicted_count=len(passes),
            unknown_count=0,
        )
    )


def test_before_anything_starts_the_total_is_the_prediction():
    tracker = make_tracker()
    assert tracker.remaining_s() == pytest.approx(300.0)


def test_a_pass_finishing_early_rescales_the_ones_still_to_come():
    """Corrected once at a pass boundary, so the figure does not lurch mid-pass."""
    tracker = make_tracker()
    tracker.start_pass(0)
    tracker.finish_pass(0, 50.0)  # half what was predicted
    tracker.start_pass(1)

    assert tracker.calibration == pytest.approx(0.5)
    # The pass in flight and the one after it, both at the measured rate.
    assert tracker.remaining_s() == pytest.approx(100.0)


def test_one_freak_pass_cannot_rescale_the_whole_evening():
    """A pass that reused prepared frames is not evidence about the rest."""
    tracker = make_tracker()
    tracker.start_pass(0)
    tracker.finish_pass(0, 0.5)

    assert tracker.calibration >= 0.25


def test_the_live_remainder_of_the_running_pass_is_preferred():
    tracker = make_tracker()
    tracker.start_pass(0)
    tracker.set_pass_progress(50, remaining_s=20.0)

    # 20s left of this one, plus the two that have not started.
    assert tracker.remaining_s() == pytest.approx(220.0)


def test_the_last_pass_is_all_that_is_left_on_the_last_pass():
    tracker = make_tracker()
    tracker.start_pass(2)
    tracker.set_pass_progress(50, remaining_s=30.0)

    assert tracker.remaining_s() == pytest.approx(30.0)


def test_a_queue_reads_the_profile_a_bounded_number_of_times(profile, monkeypatch):
    """Scenario: thirty queued passes, twenty recorded configurations.

    Expected behaviour: the file is parsed once, not once per pass per config.
    _recompute_survey_start runs on every row mutation, and the signature cache
    above misses exactly then, so an unbounded read count is felt as a stall on
    every keystroke.
    """
    import json

    from deepreefmap_gui.profiling import run_history

    write_profile(profile, *[a_run(frames=100 * (i + 1)) for i in range(20)])
    run_history._LOADED.clear()

    reads = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda raw: reads.append(1) or real(raw))

    predict_batch([spec(key=str(i), frames=500 + i) for i in range(30)], path=profile)

    assert len(reads) == 1


def test_a_recorded_run_is_not_served_from_the_stale_profile(profile):
    """The cache must never outlive the file it was read from."""
    from deepreefmap_gui.profiling.run_history import load_priors, record_run

    write_profile(profile, a_run())
    key = history_key(_BACKEND, _SEG, 1376, 768, 5)
    assert load_priors(key, profile)

    record_run(
        "brand|new|640x480|3fps",
        {"mapping": 10.0},
        frames=50,
        points=None,
        path=profile,
    )

    assert load_priors("brand|new|640x480|3fps", profile)


def test_the_point_count_is_scaled_to_the_pass_being_costed(profile):
    """A 900-frame history must not price a 2700-frame pass as 900 frames."""
    write_profile(profile, a_run(frames=900))
    short = predict_pass_seconds(spec(frames=900), path=profile).seconds
    long = predict_pass_seconds(spec(frames=2700), path=profile).seconds
    assert short is not None and long is not None
    # The point-driven stages carry roughly half the weight, so a 3x pass must
    # cost well over twice a 1x one; unscaled points held them flat.
    assert long > 2.5 * short


def test_the_planned_total_names_the_passes_it_could_not_cost(profile):
    write_profile(profile, a_run())
    prediction = predict_batch([spec(key="known"), spec(key="unknown", frames=0)], path=profile)
    assert prediction.coverage_clause() == "covering 1 of 2 passes"


def test_a_fully_costed_batch_carries_no_caveat(profile):
    write_profile(profile, a_run())
    prediction = predict_batch([spec(key="a"), spec(key="b")], path=profile)
    assert prediction.coverage_clause() is None


def test_a_percent_report_does_not_wipe_the_live_pass_remainder(profile):
    """Both arrive on one progress event; whichever landed second used to win."""
    write_profile(profile, a_run())
    prediction = predict_batch([spec(key="a"), spec(key="b")], path=profile)
    tracker = BatchEtaTracker(prediction)
    tracker.start_pass(0)
    tracker.set_pass_remaining(900.0)
    with_live = tracker.remaining_s()
    tracker.set_pass_percent(50)
    assert tracker.remaining_s() == pytest.approx(with_live)


def test_the_live_remainder_states_how_much_of_the_queue_it_covers(profile):
    write_profile(profile, a_run())
    specs = [spec(key="a")] + [spec(key=f"u{i}", frames=0) for i in range(4)]
    tracker = BatchEtaTracker(predict_batch(specs, path=profile))
    tracker.start_pass(0)
    assert tracker.pending_coverage() == (1, 5)
