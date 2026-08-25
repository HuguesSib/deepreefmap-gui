import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepreefmap_gui.profiling.instrumentation import (
    RunInstrumentation,
    apply_manifest_timings,
    durations_from_marks,
)


def test_durations_from_marks_reports_completed_spans_only() -> None:
    # A full run: startup 2s, preprocess 10s, mapping 30s, cloud 5s, ortho 8s, save 3s.
    marks = {"start": 0.0, "preprocess": 2.0, "mapping": 12.0, "cloud": 42.0, "ortho": 47.0, "save": 55.0, "end": 58.0}
    durations = durations_from_marks(marks)
    assert durations == {
        "startup": 2.0, "preprocess": 10.0, "mapping": 30.0,
        "cloud": 5.0, "ortho": 8.0, "save_view": 3.0,
    }
    # Geometry-only shortcut never reaches ortho, so those spans are omitted.
    partial = durations_from_marks({"start": 0.0, "preprocess": 1.0, "mapping": 3.0, "cloud": 9.0, "end": 12.0})
    assert set(partial) == {"startup", "preprocess", "mapping"}


def test_total_seconds_spans_start_to_latest_mark() -> None:
    timing = SimpleNamespace(marks={"start": 5.0, "preprocess": 7.0, "end": 58.0})
    assert RunInstrumentation.total_seconds(timing) == 53.0
    timing.marks["scene_end"] = 115.0
    assert RunInstrumentation.total_seconds(timing) == 110.0


def test_durations_from_marks_includes_scene_save_tail() -> None:
    # The scene save (end -> scene_end) is the previously-untimed tail.
    marks = {"start": 0.0, "preprocess": 2.0, "mapping": 12.0, "cloud": 42.0,
             "ortho": 47.0, "save": 55.0, "end": 58.0, "scene_end": 115.0}
    durations = durations_from_marks(marks)
    assert durations["save_view"] == 3.0
    assert durations["scene_save"] == 57.0


def _marker(marks: dict) -> SimpleNamespace:
    """The two attributes close_open_stage touches, marking at a fixed moment."""
    stub = SimpleNamespace(marks=marks)
    stub.mark = lambda name: marks.__setitem__(name, 99.0)
    return stub


def test_closing_the_open_stage_measures_the_one_a_run_died_in() -> None:
    """A span needs both its marks, so the stage that killed a run is the only
    stage with neither a duration nor a peak unless it is closed where it stopped."""
    marks = {"start": 0.0, "preprocess": 2.0, "mapping": 12.0}
    RunInstrumentation.close_open_stage(_marker(marks))
    assert durations_from_marks(marks)["mapping"] == 87.0


def test_a_run_that_marked_every_stage_gains_no_closing_mark() -> None:
    marks = {"start": 0.0, "preprocess": 2.0, "mapping": 12.0, "cloud": 42.0,
             "ortho": 47.0, "save": 55.0, "end": 58.0, "scene_end": 115.0}
    RunInstrumentation.close_open_stage(_marker(marks))
    assert marks["scene_end"] == 115.0
    assert durations_from_marks(marks)["scene_save"] == 57.0


def test_apply_manifest_timings_folds_measurements_into_manifest(tmp_path: Path) -> None:
    (tmp_path / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))
    instr = SimpleNamespace(
        stage_durations=lambda: {"preprocess": 10.0, "mapping": 30.0},
        stage_peaks=lambda: {"mapping": {"rss": 123}},
        total_seconds=lambda: 40.0,
        system_profile={"os_name": "Linux"},
    )
    manifest = apply_manifest_timings(tmp_path, instr)
    assert manifest is not None
    on_disk = json.loads((tmp_path / "run_manifest.json").read_text())
    assert on_disk["stage_durations"] == {"preprocess": 10.0, "mapping": 30.0}
    assert on_disk["stage_peaks"] == {"mapping": {"rss": 123}}
    assert on_disk["run_duration_s"] == 40.0
    assert on_disk["system_profile"] == {"os_name": "Linux"}
    assert on_disk["mode"] == "semantic"


def test_apply_manifest_timings_without_manifest_returns_none(tmp_path: Path) -> None:
    instr = SimpleNamespace()
    assert apply_manifest_timings(tmp_path, instr) is None


def test_a_failed_manifest_rewrite_leaves_the_run_loadable(tmp_path: Path, monkeypatch) -> None:
    """Expected behaviour: folding timings in is an update to the file that makes
    the finished run loadable at all, so a failure costs the timings, not the run.
    """
    manifest_path = tmp_path / "run_manifest.json"
    manifest_path.write_text(json.dumps({"mode": "semantic", "frames_processed": 120}))
    instr = SimpleNamespace(
        stage_durations=lambda: {"mapping": 30.0},
        stage_peaks=dict,
        total_seconds=lambda: 40.0,
        system_profile={},
    )
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError):
        apply_manifest_timings(tmp_path, instr)

    assert json.loads(manifest_path.read_text()) == {"mode": "semantic", "frames_processed": 120}
    assert [p.name for p in tmp_path.iterdir()] == ["run_manifest.json"]
