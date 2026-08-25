"""`profiling/eta.py` learns and projects per-stage timings keyed by stage name;
`profiling/instrumentation.py` is what emits those durations from a run's timing
marks. The two key sets are written out independently and drift doesn't raise: a
stage the estimator knows but instrumentation never times just has no history,
so the ETA quietly degrades to weight-based projection.

Matching key sets is not enough on its own. The orchestrator names only four
stages and distinguishes everything inside "outputs" by message, so a span can be
declared, agree with eta.py, and still never be reachable because nothing emits
the mark that opens it. That is how cloud/ortho/save_view went unmeasured. These
tests pin both halves: the names agree, and every name has a live producer.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.profiling.eta import STAGE_MESSAGE_TO_PHASE, STAGES, stage_for_phase
from deepreefmap_gui.profiling.instrumentation import (
    STAGE_SPANS,
    WRITER_DRIVEN_STAGES,
    _MarkingViewer,
    durations_from_marks,
)


class _Clock:
    """Monotonic stand-in so marks are ordered without depending on wall time."""

    def __init__(self) -> None:
        self.marks: dict[str, float] = {"start": 0.0}
        self._t = 0.0

    def mark(self, name: str) -> None:
        self._t += 1.0
        self.marks[name] = self._t


# One report per coarse stage, in pipeline order, using the exact stage names and
# message strings the library emits (verified against deepreefmap's orchestrator).
_RUN_REPORTS: tuple[tuple[str, str | None], ...] = (
    ("startup", "Loading camera + segmentation + mapping backends"),
    ("preprocess", "Rectifying + segmenting + masking"),
    ("mapping", "3D mapping pipeline in progress"),
    ("outputs", "Building semantic cloud"),
    ("outputs", "Computing PCA projection"),
    ("outputs", "Saving ortho image"),
)


def test_timed_spans_match_eta_stages() -> None:
    timed = {stage for _, _, stage in STAGE_SPANS}
    assert timed == {spec.key for spec in STAGES}, (
        "instrumentation.py STAGE_SPANS and eta.py STAGES disagree; a stage only "
        "one side names loses its history and falls back to a weight-based guess"
    )


def test_every_message_routes_to_a_known_stage() -> None:
    """A typo'd message key silently stops opening its stage."""
    spans = {stage for _, _, stage in STAGE_SPANS}
    for message, phase in STAGE_MESSAGE_TO_PHASE.items():
        coarse = stage_for_phase(phase)
        assert coarse in spans, f"{message!r} -> {phase!r} folds onto unknown stage {coarse!r}"


def test_a_full_run_measures_every_stage_the_orchestrator_drives() -> None:
    """Drive a real _MarkingViewer with the orchestrator's own call sequence.

    Marking on the stage name alone left cloud, ortho and save_view permanently
    empty, because the orchestrator reports all three as "outputs" and only the
    message tells them apart.
    """
    clock = _Clock()
    viewer = _MarkingViewer(None, clock)
    for stage, message in _RUN_REPORTS:
        viewer.set_stage(stage, "running", message)
    viewer.mark_outputs_ready("/tmp/out", [])

    measured = set(durations_from_marks(clock.marks))
    expected = {stage for _, _, stage in STAGE_SPANS} - WRITER_DRIVEN_STAGES
    assert measured == expected


@pytest.mark.parametrize("stage", sorted(WRITER_DRIVEN_STAGES))
def test_writer_driven_stages_are_declared_by_eta_too(stage: str) -> None:
    """These are not orchestrator stages, so they are the ones most likely to be
    dropped from one side. The estimator reserving weight for a stage nothing
    times makes every prediction long by that share."""
    assert stage in {spec.key for spec in STAGES}


def test_a_scene_write_closes_the_last_span() -> None:
    """mark_outputs_ready opens scene_save; only the scene writer closes it.

    instrumented_reconstruction places `scene_end` after its writer returns, so
    a run given no writer leaves the span open rather than recording a zero.
    """
    clock = _Clock()
    viewer = _MarkingViewer(None, clock)
    viewer.mark_outputs_ready("/tmp/out", [])
    assert "scene_save" not in durations_from_marks(clock.marks)

    clock.mark("scene_end")

    assert "scene_save" in durations_from_marks(clock.marks)


def test_cloud_span_survives_an_ortho_first_report() -> None:
    """A resumed run can report an ortho message before any plain cloud one.

    Marking ortho before cloud would give the cloud span a negative width, which
    durations_from_marks drops -- losing the stage rather than zeroing it.
    """
    clock = _Clock()
    viewer = _MarkingViewer(None, clock)
    viewer.set_stage("mapping", "completed", "Loaded from cache")
    viewer.set_stage("outputs", "running", "Computing PCA projection")
    viewer.set_stage("outputs", "running", "Saving ortho image")
    viewer.mark_outputs_ready("/tmp/out", [])

    assert "cloud" in durations_from_marks(clock.marks)


def test_the_save_span_opens_when_the_viewer_gets_the_data() -> None:
    """The viewer's setup runs alongside the writes, not after them.

    Opening the span on the first write left the hand-off outside it, so a prior
    of seconds was fitted for a wait of about a minute.
    """
    clock = _Clock()
    viewer = _MarkingViewer(None, clock)
    viewer.set_stage("outputs", "running", "Computing PCA projection")
    viewer.set_data(xyz=None)
    handed_off = clock.marks["save"]
    viewer.set_stage("outputs", "running", "Saving semantic cloud")
    viewer.mark_outputs_ready("/tmp/out", [])
    assert clock.marks["save"] == handed_off
    assert durations_from_marks(clock.marks)["save_view"] > 0


def test_a_branch_that_opens_no_span_does_not_swallow_the_stage_before_it() -> None:
    """Geometry-only writes its outputs without any ortho or save message.

    Its cloud span then has a begin and no end, and is dropped -- taking the wall
    clock it covered out of the profile and making every run predicted from that
    key too fast.
    """
    from deepreefmap_gui.profiling.instrumentation import RunInstrumentation

    instr = RunInstrumentation.__new__(RunInstrumentation)
    instr.marks = {"start": 0.0, "preprocess": 1.0, "mapping": 2.0, "cloud": 3.0, "end": 30.0}
    instr.backfill_unopened_stages()
    durations = durations_from_marks(instr.marks)
    assert durations["cloud"] == 27.0
    assert durations["ortho"] == 0.0
    assert durations["save_view"] == 0.0
