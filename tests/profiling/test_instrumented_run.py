"""instrumented_reconstruction: the wrapper every run actually goes through.

Called from simple/batch.py, once per pass. The other timing tests hand-build
mark dicts and call durations_from_marks directly, which is how the missing
ortho/save marks went unnoticed. These drive the real wrapper with a stubbed
orchestrator and assert on what lands in run_manifest.json and the timing
profile.
"""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.profiling.instrumentation import (
    WRITER_DRIVEN_STAGES,
    RunInstrumentation,
    apply_manifest_timings,
    instrumented_reconstruction,
)


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return d


def _fake_run(**manifest_fields):
    """An orchestrator stub that reports stages like the real one and writes a manifest."""

    def run(*, viewer, output_dir, **_kwargs):
        viewer.start_run("run", str(output_dir))
        viewer.set_stage("startup", "running", "Loading camera + segmentation + mapping backends")
        viewer.set_stage("preprocess", "running", "Rectifying + segmenting + masking")
        viewer.set_stage("mapping", "running", "3D mapping pipeline in progress")
        viewer.update_progress("outputs", current=1, total=2, message="Building semantic cloud")
        viewer.set_stage("outputs", "running", "Computing PCA projection")
        viewer.set_stage("outputs", "running", "Saving ortho image")
        manifest = {
            "mode": "semantic",
            "mapping_backend": "loger_star",
            "segmentation_model": "coralscapes-vit-b-dpt",
            "processing_width": 1376,
            "processing_height": 768,
            "fps": 5,
            "frames_processed": 120,
            **manifest_fields,
        }
        (output_dir / "run_manifest.json").write_text(json.dumps(manifest))
        viewer.set_data(
            frame_batch="frames",
            mapping_result="mapping",
            reference_cloud="cloud",
            classes_config="classes",
        )
        viewer.mark_outputs_ready(str(output_dir), [])

    return run


def test_a_run_folds_timings_and_name_into_the_manifest(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    instrumented_reconstruction(
        output_dir=out_dir,
        run_name="reef north",
        manifest_extra={"survey": {"pass": {"direction": "forward"}}},
        viewer=None,
    )

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert manifest["name"] == "reef north"
    assert manifest["survey"]["pass"]["direction"] == "forward"
    assert manifest["mode"] == "semantic"          # the run's own fields survive
    assert manifest["run_duration_s"] >= 0.0
    assert manifest["system_profile"]["os_name"]

    # The whole point: every stage the pipeline can report is timed.
    measured = set(manifest["stage_durations"])
    assert measured == {"startup", "preprocess", "mapping", "cloud", "ortho", "save_view"}
    assert "scene_save" in WRITER_DRIVEN_STAGES and "scene_save" not in measured


def test_the_configuration_the_run_launched_with_wins(out_dir, timings, monkeypatch) -> None:
    """The grain both the timing profile and the registry collate peaks at.

    The launch parameters are the resolved pixel size the form settled on, so
    they are what a peak is comparable under. The pipeline records no batch size
    at all, and anything it wrote for the other three is superseded here.
    """
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        _fake_run(processing_width=99, processing_height=99, fps=99),
    )

    instrumented_reconstruction(
        output_dir=out_dir,
        viewer=None,
        processing_width=688,
        processing_height=384,
        fps=4,
        preprocess_batch_size=8,
    )

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert (manifest["processing_width"], manifest["processing_height"]) == (688, 384)
    assert (manifest["fps"], manifest["preprocess_batch_size"]) == (4, 8)


def test_a_scene_writer_completes_the_stage_breakdown(out_dir, timings, monkeypatch) -> None:
    """The last span, and the one the ETA reserves the most weight for.

    Without a writer scene_save is never closed, so the estimator holds back a
    share of every prediction for a stage that produces no history to learn from.
    """
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    seen: list[tuple] = []

    def writer(output_dir, data, manifest):
        seen.append((output_dir, sorted(data), manifest))

    instrumented_reconstruction(output_dir=out_dir, viewer=None, scene_writer=writer)

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert set(manifest["stage_durations"]) == {
        "startup", "preprocess", "mapping", "cloud", "ortho", "save_view", "scene_save"
    }
    (_dir, payload, _manifest), = seen
    assert _dir == out_dir
    assert payload == ["classes_config", "frame_batch", "mapping_result", "reference_cloud"]


def test_the_writer_is_handed_the_merged_manifest_not_the_pipeline_s(
    out_dir, timings, monkeypatch
) -> None:
    """The scene file embeds this manifest and is read back in place of the one
    on disk, so a scene written before the merge comes back with no run name and
    no survey block -- the run would lose the transect it belongs to."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    handed: list[dict] = []

    instrumented_reconstruction(
        output_dir=out_dir,
        run_name="reef north",
        manifest_extra={"survey": {"pass": {"direction": "forward"}}},
        viewer=None,
        scene_writer=lambda _d, _data, manifest: handed.append(manifest),
    )

    (manifest,) = handed
    assert manifest["name"] == "reef north"
    assert manifest["survey"]["pass"]["direction"] == "forward"
    assert manifest["mode"] == "semantic"


def test_the_writer_reads_the_payload_the_run_produced_not_the_viewer_s(
    out_dir, timings, monkeypatch
) -> None:
    """set_data reaches the viewer through a queued signal, so the viewer's copy
    may not be indexed yet when the run ends. The proxy captures it inline."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    captured: dict = {}

    class SlowViewer:
        def __getattr__(self, name):
            return lambda *a, **k: None

    instrumented_reconstruction(
        output_dir=out_dir,
        viewer=SlowViewer(),
        scene_writer=lambda _d, data, _m: captured.update(data),
    )

    assert captured["reference_cloud"] == "cloud"


def test_a_failed_scene_write_does_not_fail_the_run(out_dir, timings, monkeypatch) -> None:
    """The scene file is a cache. Losing it must not lose the run's outputs,
    its manifest timings, or its history entry."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    def boom(_output_dir, _data, _manifest):
        raise OSError("No space left on device")

    instrumented_reconstruction(output_dir=out_dir, viewer=None, scene_writer=boom)

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert manifest["run_duration_s"] >= 0.0
    # The span stays open rather than recording a bogus duration for a failed write.
    assert "scene_save" not in manifest["stage_durations"]
    assert json.loads(timings.read_text())


def test_a_geometry_only_run_needs_no_writer(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    instrumented_reconstruction(output_dir=out_dir, viewer=None, scene_writer=None)

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert "scene_save" not in manifest["stage_durations"]


def test_quality_warnings_reach_the_manifest(out_dir, timings, monkeypatch) -> None:
    """The live viewer's warning list is in memory and cleared by the next pass
    of a batch, so the manifest is where a warning survives the night."""
    warning = "Background class dominates 9/10 frames."

    def run(*, viewer, output_dir, **_kwargs):
        viewer.set_stage("preprocess", "warning", warning)
        viewer.set_stage("preprocess", "warning", warning)  # repeats collapse
        (output_dir / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))
        viewer.set_data(frame_batch="frames")  # so the scene re-fold runs

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", run)
    instrumented_reconstruction(
        output_dir=out_dir,
        viewer=None,
        # The scene re-fold rewrites the manifest, so warnings must survive it.
        scene_writer=lambda _d, _data, _m: None,
    )

    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert manifest["quality_warnings"] == [warning]


def test_a_clean_run_writes_no_quality_warnings_key(out_dir, timings, monkeypatch) -> None:
    """An absent key reads as a clean run; an empty list would read as recorded."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    instrumented_reconstruction(output_dir=out_dir, viewer=None)
    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert "quality_warnings" not in manifest


def test_a_run_is_recorded_into_the_timing_profile(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())

    instrumented_reconstruction(output_dir=out_dir, viewer=None)

    stored = json.loads(timings.read_text())
    assert len(stored) == 1
    entry = next(iter(stored.values()))[0]
    assert entry["frames"] == 120
    assert set(entry["stage_durations"]) >= {"preprocess", "mapping", "ortho"}


def test_the_viewer_proxy_forwards_everything_to_the_real_viewer(out_dir, timings, monkeypatch) -> None:
    """The proxy wraps the viewer, so a dropped delegation blanks the UI mid-run."""
    seen: list[str] = []

    class Recorder:
        def __getattr__(self, name):
            def record(*a, **k):
                seen.append(name)
            return record

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    instrumented_reconstruction(output_dir=out_dir, viewer=Recorder())

    assert seen.count("start_run") == 1
    assert seen.count("set_stage") == 5
    assert seen.count("update_progress") == 1
    assert seen.count("set_data") == 1
    assert seen.count("mark_outputs_ready") == 1


def test_a_headless_run_needs_no_viewer(out_dir, timings, monkeypatch) -> None:
    """Batch runs pass viewer=None; the proxy must no-op rather than crash."""
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    instrumented_reconstruction(output_dir=out_dir, viewer=None)
    assert (out_dir / "run_manifest.json").exists()


def test_the_sampler_is_stopped_even_when_the_run_raises(out_dir, timings, monkeypatch) -> None:
    """Otherwise the polling thread outlives the run, for the life of the process."""
    created: list[RunInstrumentation] = []
    real_init = RunInstrumentation.__init__

    def spy(self, output_dir):
        real_init(self, output_dir)
        created.append(self)

    monkeypatch.setattr(RunInstrumentation, "__init__", spy)

    def boom(**_kwargs):
        raise RuntimeError("mapping backend died")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", boom)

    with pytest.raises(RuntimeError):
        instrumented_reconstruction(output_dir=out_dir, viewer=None)

    assert created and created[0]._sampler._thread is None


def test_a_failed_run_reports_the_peaks_it_reached(out_dir, timings, monkeypatch, sampled) -> None:
    """Scenario: a pass gets as far as mapping, and the backend exhausts memory.

    Expected behaviour: every stage it reached carries a duration and a peak,
    the stage it died in included, and the stages after it are absent. That
    partial trace is the whole account of what the run cost: a run that dies
    writes no manifest for the peaks to be folded into.
    """
    def dying_run(*, viewer, output_dir, **_kwargs):
        viewer.set_stage("startup", "running", "Loading camera + segmentation + mapping backends")
        viewer.set_stage("preprocess", "running", "Rectifying + segmenting + masking")
        viewer.set_stage("mapping", "running", "3D mapping pipeline in progress")
        sampled()
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", dying_run)
    measured: dict = {}

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        instrumented_reconstruction(
            output_dir=out_dir,
            viewer=None,
            on_failure=measured.update,
            manifest_extra={"survey": {"pass": {"direction": "forward"}}},
            segmentation_name="segformer-b2",
            mapping_name="loger_star",
            processing_width=688,
            processing_height=384,
            fps=4,
            preprocess_batch_size=8,
        )

    assert set(measured["stage_durations"]) == {"startup", "preprocess", "mapping"}
    assert "mapping" in measured["stage_peaks"]
    assert measured["stage_peaks"]["mapping"]["ram_bytes"] > 0
    assert "cloud" not in measured["stage_peaks"]
    assert measured["run_duration_s"] >= 0.0
    assert measured["system_profile"]["os_name"]
    # The grain the registry pools peaks at. Without it the failed run forms a
    # group of its own instead of sitting beside the runs it should be compared
    # with, which is the same question the pipeline never got to answer.
    assert (measured["segmentation_model"], measured["mapping_backend"]) == (
        "segformer-b2", "loger_star",
    )
    assert (measured["processing_width"], measured["processing_height"]) == (688, 384)
    assert (measured["fps"], measured["preprocess_batch_size"]) == (4, 8)
    assert measured["survey"]["pass"]["direction"] == "forward"
    # Nothing on disk: a manifest beside a half-finished run reads as one that loads.
    assert not (out_dir / "run_manifest.json").exists()


def test_a_skipped_segmentation_failure_names_the_model_the_pipeline_would_have(
    out_dir, timings, monkeypatch
) -> None:
    """The manifest spells a geometry-only run `__skip__`, and the row is grouped
    on that string, so the failure path has to spell it the same way."""
    def dying_run(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", dying_run)
    measured: dict = {}

    with pytest.raises(RuntimeError):
        instrumented_reconstruction(
            output_dir=out_dir,
            viewer=None,
            on_failure=measured.update,
            segmentation_name="segformer-b2",
            skip_segmentation=True,
        )

    assert measured["segmentation_model"] == "__skip__"


def test_a_run_that_dies_before_any_stage_records_what_little_it_has(
    out_dir, timings, monkeypatch
) -> None:
    """Nothing but the run's own start is marked, so the report is thin, not absent."""
    def dying_run(**_kwargs):
        raise RuntimeError("mapping backend died")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", dying_run)
    measured: dict = {}

    with pytest.raises(RuntimeError, match="mapping backend died"):
        instrumented_reconstruction(output_dir=out_dir, viewer=None, on_failure=measured.update)

    assert set(measured["stage_durations"]) == {"startup"}
    assert set(measured["stage_peaks"]) <= {"startup"}
    assert measured["run_duration_s"] >= 0.0


def test_a_run_that_finishes_reports_no_failure(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", _fake_run())
    measured: dict = {}

    instrumented_reconstruction(output_dir=out_dir, viewer=None, on_failure=measured.update)

    assert measured == {}
    manifest = json.loads((out_dir / "run_manifest.json").read_text())
    assert set(manifest["stage_durations"]) == {
        "startup", "preprocess", "mapping", "cloud", "ortho", "save_view"
    }


def test_a_broken_failure_report_leaves_the_run_s_own_error_standing(
    out_dir, timings, monkeypatch
) -> None:
    """The cause is what the diver needs off the failed row. Provenance is a bonus,
    and a second failure recording it must not stand in for the first."""
    def dying_run(**_kwargs):
        raise RuntimeError("CUDA out of memory")

    def broken(_manifest):
        raise OSError("database is locked")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", dying_run)

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        instrumented_reconstruction(output_dir=out_dir, viewer=None, on_failure=broken)


def test_a_run_that_wrote_no_manifest_records_nothing(out_dir, timings, monkeypatch) -> None:
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    instrumented_reconstruction(output_dir=out_dir, viewer=None)
    assert not (out_dir / "run_manifest.json").exists()
    assert not timings.exists()


def test_apply_manifest_timings_leaves_an_unreadable_manifest_alone(out_dir) -> None:
    (out_dir / "run_manifest.json").write_text("{not json")
    instr = RunInstrumentation(out_dir)
    try:
        assert apply_manifest_timings(out_dir, instr) is None
    finally:
        instr.stop()
