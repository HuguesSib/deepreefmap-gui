import json
import threading
import time
from pathlib import Path

import pytest
from _factories import clip_pass, make_batch, make_transect, seed_pass
from _qt_wait import wait_until
from deepreefmap.pipeline.orchestrator import ReconstructionCancelled
from PySide6.QtWidgets import QMessageBox

from deepreefmap_gui.core.widgets import PASS_PERCENT_ROLE
from deepreefmap_gui.profiling import batch_estimate
from deepreefmap_gui.simple.batch import (
    _COL_ACTION,
    _COL_LENGTH,
    _COL_RECORDED,
    _COL_SECTION,
    _COL_SETTINGS,
    _COL_STATUS,
    _COL_VIDEO,
    _diagnose_failure,
    _rough_batch_time,
)
from deepreefmap_gui.simple.batch_progress import BatchProgressCard
from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS


def test_diagnose_failure_speaks_plainly_and_advises():
    assert "graphics memory" in _diagnose_failure("RuntimeError: CUDA out of memory")
    assert "not installed" in _diagnose_failure("FileNotFoundError: checkpoint not found")
    assert "could not be read" in _diagnose_failure("Failed to decode video stream")
    # An unrecognised error keeps its own first line rather than inventing advice.
    assert _diagnose_failure("Weird thing: 42\nsecond line") == "Weird thing: 42"
    assert _diagnose_failure("") == "The run failed. No cause was recorded."


def test_rough_batch_time_is_silent_without_a_prediction():
    """No history for these models is no basis for a number."""
    assert _rough_batch_time(None) is None
    assert _rough_batch_time(0) is None


def test_rough_batch_time_reads_the_predicted_total():
    assert _rough_batch_time(1200.0) == "about 20 minutes"
    assert _rough_batch_time(7200.0) == "about 2 hours"


@pytest.fixture
def batch_window(window, tmp_path, monkeypatch):
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    monkeypatch.setattr(window, "_survey_missing_models", list)
    return window


def test_cart_table_header_does_not_pretend_to_sort(batch_window):
    """Cell widgets, spanned headings and a positional index rule sorting out,
    so the header must not offer it."""
    table = batch_window._survey_pass_table
    header = table.horizontalHeader()
    assert not table.isSortingEnabled()
    assert not header.sectionsClickable()
    assert header.property("sortable") is None


def test_every_column_of_the_cart_is_named(batch_window):
    """A table built wider than its labels ends with columns Qt names "9" and
    "10", which read as data nobody put there."""
    from PySide6.QtCore import Qt

    table = batch_window._survey_pass_table
    named = [
        table.horizontalHeaderItem(column).text()
        for column in range(table.columnCount())
    ]
    assert named == ["", "Clip", "Recorded", "Length", "Transect + section",
                     "Settings", "Status", ""]
    # Centred: a heading names a column rather than starting it.
    assert table.horizontalHeader().defaultAlignment() & Qt.AlignmentFlag.AlignHCenter


def await_batch(window, qapp, timeout=10.0):
    """Wait for the batch worker, then drain the signals it queued.

    join(timeout=...) returns whether or not the thread finished, so an unchecked
    join turns a hang into a confusing assertion failure further down. Draining
    has to loop as well: each processEvents() pass delivers the queued
    cross-thread signals, and a slot may queue more.
    """
    thread = window._pipeline_thread
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"batch worker still running after {timeout}s"

    deadline = time.monotonic() + timeout
    while window._survey_worker_running and time.monotonic() < deadline:
        qapp.processEvents()
    qapp.processEvents()
    assert not window._survey_worker_running, "batch finished but the UI never caught up"


def add_video(window, tmp_path, monkeypatch, name="GX010001.MP4", duration_s=60.0):
    """One whole-clip pass in the cart, over a real file in tmp_path."""
    path = tmp_path / name
    path.write_bytes(name.encode() * 4096)
    pass_ = clip_pass(window._survey_store(), path, duration_s=duration_s)
    window._add_pass_to_cart(pass_.id)
    return path


def add_videos(window, tmp_path, monkeypatch, names, duration_s=60.0):
    """Several separate recordings, one pass each."""
    return [
        str(add_video(window, tmp_path, monkeypatch, name=name, duration_s=duration_s))
        for name in names
    ]


def add_chaptered_video(window, tmp_path, names, duration_s=60.0):
    """One recording split into chapters, filed as a single pass."""
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(name.encode() * 4096)
        paths.append(str(path))
    pass_ = clip_pass(window._survey_store(), *paths, duration_s=duration_s)
    window._add_pass_to_cart(pass_.id)
    return paths


def import_clips(window, tmp_path, monkeypatch, names=("GX010001.MP4",)):
    """Drop clips on the window, as the Browse import path does."""
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(name.encode() * 4096)
        paths.append(str(path))
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (60.0, 30.0)
    )
    window._add_video_paths(paths)
    return paths


def assign_transect(window, row_index, transect_id=None):
    """File a pass against a transect, as the Videos page does.

    The cart shows the transect and no longer sets it, so the store is where a
    test has to write one.
    """
    store = window._survey_store()
    pass_ = store.get_pass(window._survey_rows[row_index].pass_id)
    pass_.transect_id = transect_id or store.list_transects()[0].id
    store.update_pass(pass_)
    window._refresh_survey_batch_tab()


def set_direction(window, row_index, direction):
    store = window._survey_store()
    pass_ = store.get_pass(window._survey_rows[row_index].pass_id)
    pass_.direction = direction
    store.update_pass(pass_)
    window._refresh_survey_batch_tab()


def retrim(window, row_index, begin_s, end_s):
    store = window._survey_store()
    pass_ = store.get_pass(window._survey_rows[row_index].pass_id)
    pass_.begin_s, pass_.end_s = begin_s, end_s
    store.update_pass(pass_)
    window._refresh_survey_batch_tab()


def row_cell(window, row_index, column):
    return window._survey_pass_table.cellWidget(window._table_row_of(row_index), column)


def click_delete(window, row_index):
    row_cell(window, row_index, _COL_ACTION).click()


def add_second_transect(window, name="T2"):
    window._survey_store().add_transect(
        make_transect(name, start_lat=-17.6, start_lon=177.2, end_lat=-17.6005, end_lon=177.2005)
    )
    window._refresh_survey_batch_tab()


def answer_question(monkeypatch, button):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: button))


def test_a_queued_pass_without_a_transect_is_runnable(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    add_video(batch_window, tmp_path, monkeypatch)
    assert len(batch_window._survey_rows) == 1
    assert batch_window._survey_rows[0].transect_id is None
    assert batch_window._survey_rows[0].end_s == 60.0
    # Runnable as it stands: skipping the transect costs the comparison against
    # repeat passes, not the run.
    assert batch_window._survey_start_btn.isEnabled()
    cell = row_cell(batch_window, 0, _COL_SECTION)
    assert cell.text().startswith("No transect · ")
    # The direction is the button's icon now, not a word in the middle of the line.
    assert not cell.icon().isNull()
    assert cell.styleSheet() != ""
    assert len(batch_window._survey_store().list_passes()) == 1
    assert batch_window._survey_store().list_passes()[0].transect_id is None


def test_importing_a_clip_never_mints_a_pass(batch_window, tmp_path, monkeypatch):
    """Scenario: one clip dropped on the window, with exactly one transect planned.

    Expected behaviour: the clip lands in the library and nothing else happens.
    No pass, no row, and no assignment to the sole transect on the user's behalf.
    """
    store = batch_window._survey_store()
    import_clips(batch_window, tmp_path, monkeypatch)
    assert wait_until(lambda: len(store.list_videos()) == 1), "probe never landed"
    assert store.list_videos()[0].duration_s == 60.0
    assert store.list_passes() == []
    assert batch_window._survey_rows == []
    assert "Imported 1 clip" in batch_window._status_label.text()


def test_assigning_transect_persists_pass(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    add_video(batch_window, tmp_path, monkeypatch)
    # Queued before a transect is chosen, and updated in place when one is.
    assert [p.transect_id for p in batch_window._survey_store().list_passes()] == [None]
    assign_transect(batch_window, 0)
    passes = batch_window._survey_store().list_passes()
    assert len(passes) == 1
    assert passes[0].transect_id is not None
    assert passes[0].begin_s == 0.0
    assert passes[0].end_s == 60.0
    assert passes[0].direction == "forward"
    assert passes[0].batch_id is not None
    assert batch_window._survey_start_btn.isEnabled()
    assert batch_window._survey_start_btn.text() == "Start processing (1 pass)"


def test_the_section_cell_opens_the_section_under_videos(batch_window, tmp_path, monkeypatch):
    """Scenario: the section on a cart row is clicked.

    Expected behaviour: nothing is edited here. The Videos page opens on that
    section, which is the one place a section is described. Transect, direction
    and window are one button, because they are one thing and go to one place.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_open_section_in_videos", opened.append)
    batch_window._refresh_survey_batch_tab()

    cell = row_cell(batch_window, 0, _COL_SECTION)
    assert cell.text() == "T1 · 0:00-1:00"
    assert not cell.icon().isNull()
    assert "forward" in cell.toolTip()
    cell.click()
    assert opened == [batch_window._survey_rows[0].pass_id]


def test_each_pass_leaves_a_log_beside_its_outputs(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """A batch runs unattended for hours and the log view is in memory, so a
    pass that failed overnight has to leave something on disk to read.

    RunDetailPanel looks for run.log in the run directory, so this is the file
    it looks for, not a file of our own choosing.
    """
    import logging

    def fake_run(**kwargs):
        logging.getLogger("deepreefmap").info("a line from inside the pass")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    logs = list(out_root.glob("*/run.log"))
    assert len(logs) == 1
    assert "a line from inside the pass" in logs[0].read_text()


def test_a_finished_pass_leaves_its_own_fast_reopen_cache(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """Scenario: a pass runs to the end in the queue.

    Expected behaviour: its scene file is already on disk, so the run's first
    open takes the fast path instead of rebuilding its cloud. The write is timed,
    so the manifest records the stage as well.
    """
    import numpy as np
    from _factories import make_classes_config, make_scene
    from deepreefmap.pipeline.artifacts import SemanticPointCloud

    from deepreefmap_gui.io.scene_file import find_scene_file, load_scene_file

    class_id = 1
    n_frames = 3

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))
        scene = make_scene(frame_indices=tuple(range(n_frames)), class_ids=(class_id,))
        rng = np.random.default_rng(1)
        kwargs["viewer"].set_data(
            frame_batch=scene.frame_batch,
            mapping_result=scene.mapping,
            classes_config=make_classes_config((class_id,)),
            reference_cloud=SemanticPointCloud(
                xyz=rng.random((20, 3)).astype(np.float32),
                rgb=rng.integers(0, 255, (20, 3), dtype=np.uint8),
                labels=np.full(20, class_id, dtype=np.int32),
                frame_indices=rng.integers(0, n_frames, 20).astype(np.int32),
            ),
        )
        # The mark scene_save is measured from, as the orchestrator leaves it.
        kwargs["viewer"].mark_outputs_ready(kwargs["output_dir"], [])

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(d for d in out_root.iterdir() if d.is_dir())
    scene = find_scene_file(run_dir)
    assert scene is not None, "the queue finished a pass without caching it"
    # Usable, not merely present: the fingerprint has to survive the timings being
    # folded into the manifest after the write.
    assert load_scene_file(scene, run_dir=run_dir) is not None

    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    assert "scene_save" in manifest["stage_durations"]


def test_run_batch_records_success_and_links_manifest(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        # instrumented_reconstruction folds the run name + survey block into the
        # manifest after the run, so give it one to fold into.
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(calls) == 1
    kwargs = calls[0]
    # instrumented_reconstruction wraps the viewer in a stage-marking proxy.
    assert kwargs["viewer"]._inner is batch_window._viewer
    assert kwargs["fps"] == 5
    assert kwargs["begin_s"] == 0.0
    assert kwargs["end_s"] == 60.0
    assert kwargs["transect_length"] == 50.0
    assert kwargs["output_dir"].parent == out_root
    # run_name and the survey block are no longer passed to run_reconstruction;
    # they land in the manifest instrumented_reconstruction writes afterward.
    manifest = json.loads((kwargs["output_dir"] / "run_manifest.json").read_text())
    # The name is the section's, not the folder's: a directory has to be unique
    # and filesystem-safe and reads like it, and this is what Browse shows.
    assert manifest["name"] == batch_window._row_label(batch_window._survey_rows[0])
    assert manifest["name"] != kwargs["output_dir"].name
    survey = manifest["survey"]
    assert survey["transect"]["name"] == "T1"
    assert survey["pass"]["direction"] == "forward"
    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["succeeded"]
    # Nothing left to process, so the start button has no action and says so.
    # The quiet route to the results is there for a return visit; the batch
    # itself has already taken the user to them.
    assert batch_window._survey_start_btn.text() == "Nothing left to process"
    assert not batch_window._survey_start_btn.isEnabled()
    batch_window._set_simple_section("process")
    assert batch_window._survey_results_btn.isVisibleTo(batch_window)
    assert batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS).text() == "Succeeded"


def test_manifest_records_the_configuration_and_any_deviation(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """Scenario: a diver processes a pass after changing one setting.

    Expected behaviour: the manifest names the organisation preset behind the run
    and the setting that deviated, so the number can be audited later.
    """
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    org = batch_window._active_preset.org
    batch_window._batch_size_spin.setValue(1)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(p for p in out_root.iterdir() if p.is_dir() and p.name.startswith("T1__"))
    survey = json.loads((run_dir / "run_manifest.json").read_text())["survey"]
    config = survey["provenance"]["config"]
    assert config["preset_name"] == org.name
    assert config["preset_version"] == org.version
    assert config["preset_hash"] == org.content_hash
    assert config["deviated"] is True
    assert config["deviations"] == {"preprocess_batch_size": 1}
    assert survey["preset_name"] == org.name


def test_manifest_of_a_standard_run_records_no_deviation(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = next(p for p in out_root.iterdir() if p.is_dir() and p.name.startswith("T1__"))
    config = json.loads((run_dir / "run_manifest.json").read_text())["survey"]["provenance"]["config"]
    assert config["deviated"] is False
    assert config["deviations"] == {}


def test_advanced_settings_reach_a_survey_run(batch_window, tmp_path, monkeypatch, qapp):
    """A survey run honours the whole run form, not just the core preset keys."""
    calls = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: calls.append(kwargs),
    )
    batch_window._grid_bins_spin.setValue(1234)
    batch_window._require_gravity_check.setChecked(True)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert calls[0]["grid_bins"] == 1234
    assert calls[0]["require_gravity_telemetry"] is True


def test_a_finished_batch_lands_on_what_it_produced(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a batch is walked away from, so the state it is found in
    should be its results.

    Expected behaviour: Browse, grouped by session and opened on this one. The
    summary is still written to the Process page for when it is returned to.
    """
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    assert batch_window._app_mode == "RUNNING"
    assert batch_window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("process")
    await_batch(batch_window, qapp)
    assert batch_window._app_mode == "SETUP"
    assert batch_window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("browse")
    assert batch_window._data_facet == "sessions"
    assert batch_window._data_detail_stack.currentWidget() is batch_window._session_detail

    summary = batch_window._survey_summary_label
    assert not summary.isHidden()
    assert "1 of 1 pass succeeded" in summary.text()


def test_batch_stops_cleanly_when_the_disk_runs_out(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: the drive fills before the batch can run its first pass.

    Expected behaviour: nothing is processed, the run is left not-started so it
    can be retried once space is freed, and the outcome says why.
    """
    import shutil
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=0, total=0, used=0))
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    ran = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: ran.append(k)
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert ran == []
    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["cancelled"]
    # Cancelled evades the failure count, so the row itself carries the reason.
    assert "disk space" in runs[0].error
    assert "disk space" in batch_window._status_label.text().lower()


def test_failed_run_keeps_pass_remaining(batch_window, tmp_path, monkeypatch, qapp):
    def broken_run(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    runs = batch_window._survey_store().list_runs()
    assert [r.status for r in runs] == ["failed"]
    assert "boom" in runs[0].error
    assert batch_window._survey_start_btn.isEnabled()
    # The pass has been through a run, so pressing again continues the batch
    # rather than starting it.
    assert batch_window._survey_start_btn.text() == "Continue processing (1 pass)"


def test_a_failure_before_the_first_pass_still_ends_the_batch(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Scenario: something outside the per-pass handler raises, before any pass
    starts.

    Expected behaviour: the batch ends, the page un-freezes, and the summary
    says why. Previously the thread died silently and the whole page stayed
    frozen until restart.
    """
    def broken_versions(_names):
        raise OSError("cache unreadable")

    monkeypatch.setattr(
        "deepreefmap_gui.models.cache.resolve_model_versions", broken_versions
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert not batch_window._survey_worker_running
    assert batch_window._app_mode == "SETUP"
    assert "cache unreadable" in batch_window._survey_summary_label.text()


def test_a_retry_lands_in_its_own_directory(batch_window, tmp_path, out_root, monkeypatch, qapp):
    """Scenario: a pass fails, the diver starts processing again.

    Expected behaviour: the retry gets a directory of its own, derived from the
    first attempt's recorded name, and the failed attempt's directory survives
    with its log. Repeats are the reproducibility data.
    """
    def broken_run(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    store = batch_window._survey_store()
    first = store.list_runs()[0]
    first_dir = out_root / first.run_dir_name

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    runs = {r.run_dir_name: r.status for r in store.list_runs()}
    assert runs == {
        first.run_dir_name: "failed",
        f"{first.run_dir_name}__r02": "succeeded",
    }
    assert first_dir.exists()
    assert (out_root / f"{first.run_dir_name}__r02" / "run_manifest.json").exists()


def test_attempt_names_survive_a_transect_rename(batch_window):
    """The stem comes from the first run's recorded name, not the live transect."""
    from _factories import make_video

    from deepreefmap_gui.survey.models import RunRecord, TransectPass

    store = batch_window._survey_store()
    transect = store.list_transects()[0]
    video = store.upsert_video(make_video())
    pass_ = TransectPass(transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=60.0)
    store.add_pass(pass_)
    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="T1__p01__abcd1234", status="failed")
    )
    transect.name = "Renamed"
    store.update_transect(transect)

    assert batch_window._pass_dir_name(pass_, transect, store) == "T1__p01__abcd1234__r02"

    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="T1__p01__abcd1234__r02", status="failed")
    )
    assert batch_window._pass_dir_name(pass_, transect, store) == "T1__p01__abcd1234__r03"


def test_the_worker_skips_a_pass_taken_out_after_checkout(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Taking a not-yet-started row out is the one per-row control a running
    order keeps, and it only works because the worker re-reads the cart."""
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    store = batch_window._survey_store()
    batch = batch_window._survey_batch
    second_pass_id = batch_window._survey_rows[1].pass_id
    calls = []

    def fake_run(**kwargs):
        if not calls:
            store.remove_batch_item(batch.id, second_pass_id)
        calls.append(kwargs)
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(calls) == 1
    statuses = {r.pass_id: r for r in store.list_runs()}
    skipped = statuses[second_pass_id]
    assert skipped.status == "cancelled"
    assert "Taken out of the session" in skipped.error


def test_failed_pass_keeps_its_cause_on_the_row(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a pass fails mid-batch.

    Expected behaviour: the failure stays with the pass. The Status cell reads
    Failed with the cause on hover, the batch summary names it by transect and
    pass rather than dumping the run-dir slug, and the full error is copyable.
    """
    def broken_run(**kwargs):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Failed"
    # The tooltip translates the error into plain advice; the raw text stays
    # available for "Copy error details".
    assert "graphics memory" in item.toolTip()
    assert "CUDA out of memory" in batch_window._survey_pass_error(batch_window._survey_rows[0])

    summary = batch_window._survey_summary_label.text()
    assert "T1 pass 1" in summary
    assert "__p" not in summary  # the run-dir slug never reaches the summary

    assert "CUDA out of memory" in batch_window._survey_pass_error(batch_window._survey_rows[0])


def test_succeeded_pass_flags_a_quality_warning(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a pass succeeds but the camera pointed off-reef.

    Expected behaviour: the Status cell still reads succeeded, marked with a
    warning sign, and the manifest's quality_warnings show on hover.
    """
    warning = "Background class dominates 9/10 frames. Camera may be pointing away from the reef."

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(
            json.dumps({"mode": "semantic", "quality_warnings": [warning]})
        )

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Succeeded ⚠"
    assert warning in item.toolTip()


def test_succeeded_pass_without_warnings_has_a_clean_pill(batch_window, tmp_path, monkeypatch, qapp):
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Succeeded"
    assert item.toolTip() == ""


def test_double_click_failed_pass_reveals_the_cause(batch_window, tmp_path, monkeypatch, qapp):
    def broken_run(**kwargs):
        raise RuntimeError("gravity telemetry missing")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", broken_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "gravity telemetry missing" in batch_window._status_label.text()


def test_survey_progress_line_shows_transect_not_slug(batch_window, tmp_path, monkeypatch, qapp):
    """The live progress line names the place, never the run-dir slug."""
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    seen = []
    # A second slot alongside the real one: the emitted name is what we assert.
    batch_window._sig_survey_progress.connect(lambda _i, _t, name: seen.append(name))
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    assert seen == ["T1"]


def test_remove_after_processing_keeps_the_pass_and_its_run(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Removing un-carts membership only, so a processed pass keeps its run."""
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **k: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    batch_window._survey_pass_table.setCurrentCell(batch_window._table_row_of(0), 0)
    batch_window._on_survey_remove_pass()
    store = batch_window._survey_store()
    assert batch_window._survey_rows == []
    assert len(store.list_passes()) == 1
    assert [r.status for r in store.list_runs()] == ["succeeded"]


def test_remove_from_session_honours_multi_selection(batch_window, tmp_path, monkeypatch):
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    session = batch_window._survey_batch
    batch_window._survey_pass_table.selectAll()
    batch_window._on_survey_remove_pass()
    store = batch_window._survey_store()
    assert batch_window._survey_rows == []
    assert store.list_batch_items(session.id) == []
    # The passes and the session survive; only the membership is gone.
    assert len(store.list_passes()) == 2
    assert store.get_batch(session.id) is not None


def test_the_row_button_takes_one_pass_out(batch_window, tmp_path, monkeypatch):
    """Scenario: one pass of a filled cart is not wanted in this session.

    Expected behaviour: its own button takes it out, with no selection first,
    and takes nothing else with it. The pass and its clip are kept.
    """
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    session = batch_window._survey_batch
    click_delete(batch_window, 0)

    store = batch_window._survey_store()
    assert [row.video.file_name for row in batch_window._survey_rows] == ["GX010002.MP4"]
    assert len(store.list_batch_items(session.id)) == 1
    assert len(store.list_passes()) == 2


# --- The processing order ---


def test_a_dragged_row_changes_the_order_and_keeps_it(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: the last clip of the day is the one worth processing first.

    Expected behaviour: dragging it to the top reorders the queue, the order is
    written to the cart rows, and it survives a rebuild from the store.
    """
    add_videos(
        batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4", "GX010003.MP4"]
    )
    table = batch_window._survey_pass_table
    table.rows_moved.emit(batch_window._table_row_of(2), batch_window._table_row_of(0))
    assert [row.video.file_name for row in batch_window._survey_rows] == [
        "GX010003.MP4", "GX010001.MP4", "GX010002.MP4"
    ]

    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._refresh_survey_batch_tab()
    assert [row.video.file_name for row in batch_window._survey_rows] == [
        "GX010003.MP4", "GX010001.MP4", "GX010002.MP4"
    ]


def test_a_processed_row_cannot_be_dragged_into_the_queue(
    batch_window, tmp_path, monkeypatch
):
    """Order only means something for the passes still to run."""
    from deepreefmap_gui.survey.models import RunRecord

    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    batch_window._survey_store().add_run(
        RunRecord(
            pass_id=batch_window._survey_rows[1].pass_id,
            run_dir_name="run_001",
            status="succeeded",
            batch_id=batch_window._survey_batch.id,
        )
    )
    batch_window._refresh_survey_batch_tab()
    before = [row.video.file_name for row in batch_window._survey_rows]

    table = batch_window._survey_pass_table
    table.rows_moved.emit(batch_window._table_row_of(1), batch_window._table_row_of(0))
    assert [row.video.file_name for row in batch_window._survey_rows] == before
    assert "still to process" in batch_window._status_label.text()


# --- Settings for one pass ---


def settings_cell(window, row_index):
    return row_cell(window, row_index, _COL_SETTINGS)


def test_a_row_without_overrides_says_it_runs_on_the_session(
    batch_window, tmp_path, monkeypatch
):
    add_video(batch_window, tmp_path, monkeypatch)
    assert settings_cell(batch_window, 0).text() == "Default settings"


def test_an_override_is_stored_counted_and_reaches_the_run(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: one long pass is given a lower frame rate than the session.

    Expected behaviour: only that setting is stored, the button counts it, and
    the run made from that row uses it while the session keeps its own.
    """
    add_videos(batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4"])
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    session_fps = batch_window._fps_spin.value()
    batch_window._write_overrides([0], {"fps": session_fps - 2})

    store = batch_window._survey_store()
    item = next(
        i for i in store.list_batch_items(batch_window._survey_batch.id)
        if i.pass_id == batch_window._survey_rows[0].pass_id
    )
    assert item.overrides == {"fps": session_fps - 2}
    assert settings_cell(batch_window, 0).text() == "1 override"
    assert settings_cell(batch_window, 1).text() == "Default settings"
    # The session itself is untouched by a row's settings.
    assert batch_window._fps_spin.value() == session_fps

    seen = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: seen.append(kwargs["fps"]),
    )
    batch_window._on_survey_start()
    batch_window._pipeline_thread.join(timeout=10.0)
    assert seen == [session_fps - 2, session_fps]
    assert batch_window._fps_spin.value() == session_fps


def test_an_override_that_matches_the_session_stops_being_one(
    batch_window, tmp_path, monkeypatch
):
    """A session edited towards a row's override leaves it claiming a
    difference that no longer exists."""
    add_video(batch_window, tmp_path, monkeypatch)
    batch_window._write_overrides([0], {"fps": 3})
    assert settings_cell(batch_window, 0).text() == "1 override"

    batch_window._fps_spin.setValue(3)
    batch_window._recompute_survey_start()
    assert settings_cell(batch_window, 0).text() == "Default settings"


def test_settings_can_be_copied_from_another_pass_and_given_back(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: three passes of one dive need the settings the first one got.

    Expected behaviour: the copy menu offers the pass that carries them, one
    action gives them to the selection, and the session's own settings are
    always on offer to undo it.
    """
    from PySide6.QtWidgets import QMenu

    add_videos(
        batch_window, tmp_path, monkeypatch, ["GX010001.MP4", "GX010002.MP4", "GX010003.MP4"]
    )
    batch_window._write_overrides([0], {"fps": 3})

    actions = {}
    monkeypatch.setattr(QMenu, "exec", lambda self, *a: None)
    monkeypatch.setattr(
        QMenu,
        "addAction",
        lambda self, label, handler=None: actions.setdefault(label, handler),
    )
    batch_window._survey_pass_table.selectAll()
    batch_window._on_survey_copy_settings()

    source = next(label for label in actions if label.startswith("GX010001.MP4"))
    assert "frames per second" in source
    actions[source]()
    assert [row.overrides for row in batch_window._survey_rows] == [{"fps": 3}] * 3

    actions["The session's settings"]()
    assert [row.overrides for row in batch_window._survey_rows] == [{}] * 3


def test_a_pass_too_big_for_the_machine_marks_its_settings(
    batch_window, tmp_path, monkeypatch
):
    """The memory verdict rides on the button that opens what would fix it."""
    class _Fit:
        def __init__(self, fits):
            self.fits = fits

        headline = "Too long to process in one pass"
        detail = "Needs about 40 GB of memory."
        advice = "Set FPS to 3."

    # Both grades are stubbed: what this machine can really give a run is not
    # the subject, and a loaded test box would decide the answer either way.
    monkeypatch.setattr(batch_window, "_row_fit", lambda *a, **k: _Fit(True))
    add_video(batch_window, tmp_path, monkeypatch)
    assert settings_cell(batch_window, 0).styleSheet() == ""

    monkeypatch.setattr(batch_window, "_row_fit", lambda *a, **k: _Fit(False))
    batch_window._refresh_settings_cells()
    cell = settings_cell(batch_window, 0)
    assert cell.styleSheet() != ""
    assert "Set FPS to 3." in cell.toolTip()


def test_clear_cart_click_empties_the_cart_without_crashing(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a filled cart is cleared from the header button.

    Expected behaviour: the table, its row index and the row actions repaint
    together, so nothing indexes a row that is gone.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    session = batch_window._survey_batch
    answer_question(monkeypatch, QMessageBox.StandardButton.Yes)
    batch_window._survey_clear_cart_btn.click()
    store = batch_window._survey_store()
    assert batch_window._survey_rows == []
    assert batch_window._survey_pass_table.rowCount() == 0
    assert store.list_batch_items(session.id) == []
    assert len(store.list_passes()) == 1
    assert store.get_batch(session.id) is not None
    assert not batch_window._survey_remove_btn.isEnabled()
    assert not batch_window._survey_start_btn.isEnabled()


def test_clear_cart_declined_leaves_the_cart_alone(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    session = batch_window._survey_batch
    answer_question(monkeypatch, QMessageBox.StandardButton.No)
    batch_window._survey_clear_cart_btn.click()
    assert len(batch_window._survey_rows) == 1
    assert len(batch_window._survey_store().list_batch_items(session.id)) == 1


def test_clear_cart_leaves_other_sessions_alone(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    session = batch_window._survey_batch
    store = batch_window._survey_store()
    other = make_batch(store, "other-day")
    seed_pass(store, transect=store.list_transects()[0], batch=other)
    answer_question(monkeypatch, QMessageBox.StandardButton.Yes)
    batch_window._survey_clear_cart_btn.click()
    assert store.list_batch_items(session.id) == []
    assert len(store.list_batch_items(other.id)) == 1
    assert len(store.list_passes()) == 2


def test_remove_pass_in_a_cart_takes_its_cart_row_with_it(batch_window, tmp_path, monkeypatch):
    """Scenario: a pass sits in the cart and has never run.

    Expected behaviour: removing it un-carts the row rather than failing on
    the batch_item foreign key, and the pass itself survives in the store.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    row = batch_window._survey_rows[0]
    store = batch_window._survey_store()
    assert [i.pass_id for i in store.list_all_batch_items()] == [row.pass_id]

    batch_window._survey_pass_table.setCurrentCell(batch_window._table_row_of(0), 0)
    batch_window._on_survey_remove_pass()

    assert batch_window._survey_rows == []
    assert store.get_pass(row.pass_id) is not None
    assert store.list_all_batch_items() == []


def test_refresh_restores_batch_from_store(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_name = batch_window._survey_batch_name.text()
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._survey_pass_table.setRowCount(0)
    batch_window._refresh_survey_batch_tab()
    assert len(batch_window._survey_rows) == 1
    assert batch_window._survey_batch_name.text() == batch_name
    assert batch_window._survey_rows[0].transect_id is not None


def test_the_worker_is_handed_the_windows_own_pause_and_cancel_events(
    batch_window, tmp_path, monkeypatch, qapp
):
    """The two objects the transport controls set have to be the two the pass is
    waiting on, or pausing and stopping reach nothing.

    That they release a paused worker rather than wedging it is exercised
    against a batch that is genuinely still running, in
    test_stopping_a_batch_ends_it_before_the_next_pass.
    """
    seen = {}
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: seen.update(kwargs),
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert seen["pause_event"] is batch_window._pause_event
    assert seen["cancel_event"] is batch_window._survey_cancel_event


def test_pause_button_drives_the_survey_pause_event(batch_window, tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **kwargs: None
    )
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    batch_window._on_pause_toggled(True)
    assert not batch_window._pause_event.is_set()
    batch_window._on_pause_toggled(False)
    assert batch_window._pause_event.is_set()


# --- Transport controls over a batch that is still running ---


class _BlockingPass:
    """Stands in for the pipeline, holding each pass open until it is released.

    Records the kwargs it was called with so a test can count the passes that
    ran, and honours the cancel event the way the real pipeline does.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(timeout=5)
        cancel = kwargs.get("cancel_event")
        if cancel is not None and cancel.is_set():
            raise ReconstructionCancelled


@pytest.fixture
def blocking_pass(monkeypatch):
    """A pipeline that stays inside a pass, so a live batch can be inspected."""
    stub = _BlockingPass()
    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", stub)
    yield stub
    # Released whatever the test did with it, so a failed assertion leaves no
    # worker wedged in wait().
    stub.release.set()


def test_a_running_batch_puts_start_out_of_reach(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    """A second launch would share the viewer and overwrite _pipeline_thread, so
    while a batch runs there is no way to start another, and the transport
    controls stand in its place until it ends."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    assert batch_window._survey_start_btn.isEnabled()

    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    assert not batch_window._survey_start_btn.isEnabled()
    assert not batch_window._pause_btn.isHidden()

    blocking_pass.release.set()
    await_batch(batch_window, qapp)

    # The one pass queued has now run, so the transport goes and the start
    # button stays out of reach for want of anything to do rather than because
    # something is in flight.
    assert not batch_window._survey_start_btn.isEnabled()
    assert batch_window._pause_btn.isHidden()


def test_stopping_a_batch_ends_it_before_the_next_pass(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    batch_window._on_stop_clicked()
    blocking_pass.release.set()
    await_batch(batch_window, qapp)

    assert len(blocking_pass.calls) == 1, "the remaining passes ran after the stop"


def test_a_paused_batch_does_not_start_the_next_pass(
    batch_window, tmp_path, monkeypatch, blocking_pass, qapp
):
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    batch_window._on_survey_start()
    assert blocking_pass.entered.wait(timeout=5)

    batch_window._pause_btn.setChecked(True)
    blocking_pass.entered.clear()
    blocking_pass.release.set()
    assert not blocking_pass.entered.wait(timeout=0.5), "a pass started while paused"

    batch_window._pause_btn.setChecked(False)
    await_batch(batch_window, qapp)
    assert len(blocking_pass.calls) == 2


def test_a_stopped_batch_is_not_reported_as_failures(
    batch_window, tmp_path, monkeypatch, qapp
):
    """One of two finishing reads identically whether the diver stopped the batch
    or the other pass blew up, so the outcome has to tell them apart."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)

    def cancel_immediately(**kwargs):
        batch_window._survey_cancel_event.set()
        raise ReconstructionCancelled

    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", cancel_immediately
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    summary = batch_window._survey_summary_label.text()
    assert "0 of 2 passes succeeded" in summary
    assert "Failed" not in summary
    assert "Failed" not in batch_window._status_label.text()


def test_a_finished_batch_lands_in_browse(batch_window, tmp_path, monkeypatch, qapp):
    """What a batch wrote is listed as finished without anyone asking for a
    rescan. The out-root watcher notices the folder on its own, but only as the
    half-built directory the pass started in, so the status is what pins this."""
    from deepreefmap_gui.runs import run_table

    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    table = batch_window._data_run_table
    assert table.rowCount() == 1
    assert table.item(0, run_table.COL_STATUS).text() == "Succeeded"


def test_double_click_opens_the_run_that_succeeded(batch_window, tmp_path, out_root, monkeypatch):
    """Scenario: a pass failed, was retried, and succeeded the second time.

    Expected behaviour: the row opens the successful run. Taking the most
    recent record instead would open a directory with no manifest whenever a
    later attempt failed.
    """
    from deepreefmap_gui.survey.models import RunRecord

    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    store = batch_window._survey_store()
    pass_id = batch_window._survey_rows[0].pass_id

    for name, status in (("run_bad", "failed"), ("run_good", "succeeded")):
        record = RunRecord(pass_id=pass_id, run_dir_name=name)
        store.add_run(record)
        store.set_run_status(record.id, status)
        (out_root / name).mkdir()

    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == [out_root / "run_good"]


def test_double_click_is_refused_while_a_batch_runs(batch_window, tmp_path, monkeypatch):
    """The live run owns the viewer; an old one must not take it away."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    monkeypatch.setattr(batch_window, "_run_in_flight", lambda: True)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "Unavailable while processing" in batch_window._status_label.text()


def test_unprocessed_row_says_so_rather_than_doing_nothing(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    opened = []
    monkeypatch.setattr(batch_window, "_auto_load_run", opened.append)
    batch_window._on_survey_pass_activated(batch_window._table_row_of(0), 0)
    assert opened == []
    assert "no successful run" in batch_window._status_label.text()


def test_gate_summary_and_run_settings_agree(window, monkeypatch):
    """The gate, the summary label and the run all derive from the form.

    With one shared source they cannot disagree: the models the gate blocks on
    are exactly those _collect_run_settings() names, and the label describes the
    same run.
    """
    from deepreefmap_gui.models.cache import all_known_models

    # What _refresh_model_status delivers from its worker thread, which is what
    # the gate reads: verifying the cache itself would walk every snapshot, on
    # the GUI thread, on every repaint. Nothing cached here.
    window._last_model_states = [(info, False) for info in all_known_models()]
    window._skip_seg_check.setChecked(False)
    window._seg_combo.setCurrentText("segformer-b2")
    window._map_combo.setCurrentText("scsfmlearner")
    window._recompute_survey_start()

    settings = window._collect_run_settings()
    required = window._required_model_names()
    # Nothing is cached, so every required model is missing and nothing else is.
    assert set(window._survey_missing_models()) == required
    assert settings["mapping_name"] in required
    assert settings["segmentation_name"] in required
    label = window._survey_preset_label.text()
    assert settings["segmentation_name"] in label
    assert settings["mapping_name"] in label


def test_corrupt_user_preset_falls_back_and_does_not_block(make_window, tmp_path, monkeypatch):
    """A corrupt user preset must not leave the survey with no preset.

    load_survey_preset quarantines the bad file and returns the bundled
    defaults, so _survey_preset is not None and an assigned pass can still run.
    """
    bad = tmp_path / "survey_preset.yaml"
    bad.write_text("schema_version: 999\nfps: 3\n")
    monkeypatch.setattr("deepreefmap_gui.survey.preset.survey_preset_path", lambda: bad)

    window = make_window()
    window._survey_store().add_transect(make_transect())
    window._refresh_survey_batch_tab()
    monkeypatch.setattr(window, "_survey_missing_models", list)

    assert window._survey_preset is not None
    assert not bad.exists()
    assert bad.with_name(bad.name + ".corrupt-1").exists()
    assert "could not be loaded" not in window._survey_preset_label.text()

    add_video(window, tmp_path, monkeypatch)
    # The pass runs unassigned, so the only thing that could still block the
    # button is a missing preset.
    assert window._survey_start_btn.isEnabled()


# --- Acting on a selection ---


def test_pass_table_allows_a_multi_row_selection(batch_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QTableWidget

    add_second_transect(batch_window)
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    table = batch_window._survey_pass_table
    assert table.selectionMode() == QTableWidget.SelectionMode.ExtendedSelection
    assert not batch_window._survey_remove_btn.isEnabled()
    table.selectRow(batch_window._table_row_of(1))
    assert batch_window._selected_survey_rows() == [1]
    assert batch_window._survey_remove_btn.isEnabled()
    assert batch_window._survey_bulk_settings_btn.isEnabled()


def section_cell(window, row_index):
    return row_cell(window, row_index, _COL_SECTION)


def test_one_way_transect_is_read_in_the_tooltip(batch_window, tmp_path, monkeypatch):
    """Nothing downstream can tell a one-way survey from directions nobody set,
    but a survey that really is one-way must not turn the table amber."""
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    flagged = section_cell(batch_window, 0)
    assert "swum out and back" in flagged.toolTip()
    assert flagged.styleSheet() == ""

    set_direction(batch_window, 1, "reverse")
    assert "swum out and back" not in section_cell(batch_window, 0).toolTip()


def test_a_pass_with_no_transect_is_the_one_marked_section(
    batch_window, tmp_path, monkeypatch
):
    add_video(batch_window, tmp_path, monkeypatch)
    assert section_cell(batch_window, 0).styleSheet() != ""
    assign_transect(batch_window, 0)
    assert section_cell(batch_window, 0).styleSheet() == ""


def test_a_pass_whose_footage_is_gone_says_so_on_its_row(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: the drive holding one clip of the session is unplugged.

    Expected behaviour: the notification centre counts them, but a count in the
    corner does not say which rows. The row marks itself, in red and dashed
    rather than the amber the advisory notices use.
    """
    from deepreefmap_gui.survey.catalogue import LINK_MISSING, VideoLibraryEntry

    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    assert "dashed" not in section_cell(batch_window, 0).styleSheet()

    clip = batch_window._survey_rows[0].video
    batch_window._video_entries = [
        VideoLibraryEntry(video=clip, pass_count=1, run_count=0, link_state=LINK_MISSING)
    ]
    batch_window._recompute_survey_start()

    cell = section_cell(batch_window, 0)
    assert "dashed" in cell.styleSheet()
    assert "cannot be found" in cell.toolTip()


# --- Clip identity ---


def test_the_clip_reads_as_a_name_a_time_and_a_length(batch_window, tmp_path, monkeypatch):
    """Run together in one cell the three of them are an unreadable string."""
    add_video(batch_window, tmp_path, monkeypatch, name="GX010012.MP4", duration_s=401.0)
    table_row = batch_window._table_row_of(0)
    table = batch_window._survey_pass_table
    assert table.item(table_row, _COL_VIDEO).text() == "GX010012.MP4"
    assert table.item(table_row, _COL_LENGTH).text() == "6m 41s"
    assert table.item(table_row, _COL_RECORDED).text() != "time unknown"


def test_unreadable_clip_metadata_says_so():
    from deepreefmap_gui.simple.batch import _clip_name, _clip_time, _span_length
    from deepreefmap_gui.survey.models import VideoAsset

    asset = VideoAsset(file_name="clip.mp4", path="/data/clip.mp4")
    assert _clip_name([asset]) == "clip.mp4"
    assert _clip_time(asset.mtime) == "time unknown"
    assert _span_length(asset.duration_s) == "length unknown"


def test_the_length_is_the_section_not_the_clip(batch_window, tmp_path, monkeypatch):
    """Two sections of one clip differ by it, and the pass costs what it spans."""
    add_video(batch_window, tmp_path, monkeypatch, name="GX010002.MP4", duration_s=298.0)
    row = batch_window._survey_rows[0]
    row.begin_s, row.end_s = 22.0, 58.0
    batch_window._rebuild_survey_table()

    table = batch_window._survey_pass_table
    cell = table.item(batch_window._table_row_of(0), _COL_LENGTH)
    assert cell.text() == "36s"
    assert "4m 58s" in cell.toolTip()


def test_sort_by_time_orders_the_day_as_it_happened(batch_window, tmp_path, monkeypatch):
    add_second_transect(batch_window)
    for name in ("GX010002.MP4", "GX010001.MP4", "GX010003.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    rows = batch_window._survey_rows
    rows[0].video.mtime = "2026-07-01T09:30:00+00:00"
    rows[1].video.mtime = "2026-07-01T08:15:00+00:00"
    # A clip whose timestamp could not be read sorts last, not first.
    rows[2].video.mtime = None

    batch_window._on_survey_sort_by_time()
    assert [row.video.file_name for row in batch_window._survey_rows] == [
        "GX010001.MP4", "GX010002.MP4", "GX010003.MP4"
    ]
    assert batch_window._survey_pass_table.item(
        batch_window._table_row_of(2), _COL_RECORDED
    ).text() == "time unknown"
    # The order survives a rebuild, which reads it back from the cart rows.
    batch_window._refresh_survey_batch_tab()
    assert [row.video.file_name for row in batch_window._survey_rows] == [
        "GX010001.MP4", "GX010002.MP4", "GX010003.MP4"
    ]


def test_probing_a_clip_stays_off_the_gui_thread(batch_window, tmp_path, monkeypatch):
    """A card of 4 GB clips must not freeze the window while cv2 reads them."""
    threads = []
    path = tmp_path / "GX010009.MP4"
    path.write_bytes(b"x" * 4096)

    def record(_path):
        threads.append(threading.current_thread())
        return (60.0, 30.0)

    store = batch_window._survey_store()
    monkeypatch.setattr("deepreefmap_gui.simple.batch._probe_video", record)
    batch_window._add_video_paths([str(path)])
    assert wait_until(lambda: len(store.list_videos()) == 1)
    assert threads and threads[0] is not threading.main_thread()
    assert store.list_videos()[0].duration_s == 60.0


def test_unreadable_clips_are_counted_not_imported(batch_window, tmp_path, monkeypatch):
    store = batch_window._survey_store()
    good = tmp_path / "good.MP4"
    bad = tmp_path / "bad.MP4"
    for path in (good, bad):
        path.write_bytes(path.name.encode() * 512)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video",
        lambda path: (60.0, 30.0) if path.endswith("good.MP4") else None,
    )
    batch_window._add_video_paths([str(good), str(bad)])
    assert wait_until(lambda: len(store.list_videos()) == 1)
    assert store.list_videos()[0].file_name == "good.MP4"
    assert "Skipped 1 unreadable" in batch_window._status_label.text()


# --- Retrying a pass ---


def test_a_retry_seeds_from_the_failed_attempts_frames(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """Scenario: a pass fails after preprocessing, and the batch is run again.

    Expected behaviour: the retry gets its own directory but hard-links the
    frames the failed attempt already decoded, so the afternoon is not spent
    twice. Seeding scans every sibling, earlier attempts included.
    """
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.runs.seeding import preprocess_key_for_settings

    dirs = []

    def failing(**kwargs):
        out_dir = kwargs["output_dir"]
        dirs.append(out_dir)
        # Preprocessing completed before the failure: frames plus the sidecar
        # that marks the stage done, which is what seeding matches on.
        clip = batch_window._survey_rows[0].videos[0].path
        for dirname in ("frames", "labels", "masks"):
            (out_dir / dirname).mkdir(parents=True, exist_ok=True)
            (out_dir / dirname / "000000.png").write_bytes(b"data")
        key = preprocess_key_for_settings(
            batch_window._collect_run_settings(), [Path(clip)], 0.0, 60.0
        )
        resume_mod.write_sidecar(out_dir, resume_mod.STAGE_PREPROCESS, key)
        raise RuntimeError("boom")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", failing)
    add_video(batch_window, tmp_path, monkeypatch)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert len(dirs) == 2
    assert dirs[1] == Path(f"{dirs[0]}__r02")
    assert "__p01__" in dirs[0].name
    # The retry's frames arrived by seeding before the pipeline was called.
    assert (dirs[1] / "frames" / "000000.png").read_bytes() == b"data"


def test_survey_worker_seeds_from_a_matching_run(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """A pass of a clip another run already preprocessed skips preprocessing."""
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.runs.seeding import preprocess_key_for_settings

    clip = add_video(batch_window, tmp_path, monkeypatch)
    # A sibling of the run the pass is about to create: seeding only searches
    # the output root.
    prior = out_root / "earlier-run"
    for dirname in ("frames", "labels", "masks"):
        (prior / dirname).mkdir(parents=True)
        (prior / dirname / "000000.png").write_bytes(b"data")
    key = preprocess_key_for_settings(batch_window._collect_run_settings(), [clip], 0.0, 60.0)
    resume_mod.write_sidecar(prior, resume_mod.STAGE_PREPROCESS, key)

    seen = []
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction",
        lambda **kwargs: seen.append(kwargs["output_dir"]),
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert (seen[0] / "frames" / "000000.png").exists()
    sidecar = resume_mod.read_sidecar(seen[0], resume_mod.STAGE_PREPROCESS)
    assert sidecar is not None and sidecar["key"] == key


# --- Chaptered recordings ---


def test_a_chaptered_pass_reads_as_one_row(batch_window, tmp_path, monkeypatch):
    """Scenario: a swim long enough that the camera split it at 4 GB.

    Expected behaviour: one row covering both chapters played back to back,
    not two rows of half a transect each.
    """
    add_chaptered_video(
        batch_window, tmp_path, ["GX010012.MP4", "GX020012.MP4"], duration_s=300.0
    )
    assert len(batch_window._survey_rows) == 1
    row = batch_window._survey_rows[0]
    assert [video.file_name for video in row.videos] == ["GX010012.MP4", "GX020012.MP4"]
    assert (row.begin_s, row.end_s) == (0.0, 600.0)

    table = batch_window._survey_pass_table
    table_row = batch_window._table_row_of(0)
    assert table.item(table_row, _COL_VIDEO).text() == "GX010012.MP4 +1 chapter"
    assert table.item(table_row, _COL_LENGTH).text() == "10m 00s"

    pass_ = batch_window._survey_store().get_pass(row.pass_id)
    assert pass_.video_ids() == [video.id for video in row.videos]


def test_a_chaptered_pass_runs_every_chapter(batch_window, tmp_path, monkeypatch, qapp):
    seen = {}
    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", lambda **kwargs: seen.update(kwargs)
    )
    paths = add_chaptered_video(batch_window, tmp_path, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert seen["video_paths"] == paths
    # begin_s/end_s are offsets into the chapters played back to back.
    assert (seen["begin_s"], seen["end_s"]) == (0.0, 120.0)


def test_reopening_a_batch_restores_the_chapters(batch_window, tmp_path, monkeypatch):
    add_chaptered_video(batch_window, tmp_path, ["GX010012.MP4", "GX020012.MP4"])
    batch_window._survey_batch = None
    batch_window._survey_rows = []
    batch_window._survey_pass_table.setRowCount(0)
    batch_window._refresh_survey_batch_tab()

    assert len(batch_window._survey_rows) == 1
    assert [video.file_name for video in batch_window._survey_rows[0].videos] == [
        "GX010012.MP4", "GX020012.MP4"
    ]


def test_the_same_clip_picked_twice_imports_once(batch_window, tmp_path, monkeypatch):
    """Two folders holding one clip is still one file, so the library names it once."""
    store = batch_window._survey_store()
    copy_dir = tmp_path / "backup"
    copy_dir.mkdir()
    original = tmp_path / "GX010012.MP4"
    original.write_bytes(b"same bytes" * 4096)
    duplicate = copy_dir / "GX010012.MP4"
    duplicate.write_bytes(original.read_bytes())
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (60.0, 30.0)
    )
    batch_window._add_video_paths([str(original), str(duplicate)])
    assert wait_until(lambda: len(store.list_videos()) == 1)
    assert "Imported 1 clip" in batch_window._status_label.text()
    assert store.list_passes() == []


def group_headings(window):
    """The section titles the pass table currently shows, top to bottom."""
    table = window._survey_pass_table
    return [
        table.item(row, 0).text()
        for row in range(table.rowCount())
        if window._model_index(row) is None
    ]


def test_a_processed_pass_leaves_the_batch_until_asked_for_again(
    batch_window, tmp_path, monkeypatch
):
    from deepreefmap_gui.survey.models import RunRecord

    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    row = batch_window._survey_rows[0]
    batch_window._survey_store().add_run(
        RunRecord(
            pass_id=row.pass_id,
            run_dir_name="run_001",
            status="succeeded",
            batch_id=batch_window._survey_batch.id,
        )
    )
    batch_window._rebuild_survey_table()
    batch_window._recompute_survey_start()
    assert group_headings(batch_window) == ["Already processed  (1)"]
    assert batch_window._survey_remaining_rows() == []

    # Process again: the same pass, ordered into a fresh cart.
    first_session = batch_window._survey_batch
    batch_window._process_rows_again([0])
    assert batch_window._survey_batch.id != first_session.id
    assert group_headings(batch_window) == ["To process  (1)"]
    assert len(batch_window._survey_remaining_rows()) == 1
    assert "cart" in batch_window._status_label.text().lower()


def test_a_finished_order_hands_the_page_to_the_cart(
    batch_window, tmp_path, monkeypatch, qapp
):
    """Scenario: passes were added to the cart while the order ran.

    Expected behaviour: once the order finishes, the page shows the cart, ready
    to start, and the order's rows are read in Browse instead.
    """
    def fake_run(**kwargs):
        (kwargs["output_dir"] / "run_manifest.json").write_text(json.dumps({"mode": "semantic"}))

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fake_run)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    order = batch_window._survey_batch
    batch_window._on_survey_start()
    # Queue a rerun of the same pass mid-run; it lands in the next cart.
    batch_window._add_pass_to_cart(batch_window._survey_rows[0].pass_id)
    cart = batch_window._survey_batch
    assert cart.id != order.id
    await_batch(batch_window, qapp)

    assert batch_window._survey_batch.id == cart.id
    assert batch_window._survey_running_batch is None
    assert [row.in_cart for row in batch_window._survey_rows] == [False]
    assert len(batch_window._survey_remaining_rows()) == 1
    assert batch_window._survey_start_btn.isEnabled()


def test_the_cart_badge_follows_the_queue(batch_window, tmp_path, monkeypatch):
    """Adds, removals and checkouts all funnel through _recompute_survey_start,
    which is what keeps the badge honest."""
    assert batch_window._cart_button._count == 0
    add_video(batch_window, tmp_path, monkeypatch)
    assert batch_window._cart_button._count == 1
    click_delete(batch_window, 0)
    assert batch_window._cart_button._count == 0


def test_the_cart_badge_counts_next_session_rows_mid_run(
    batch_window, tmp_path, monkeypatch
):
    add_video(batch_window, tmp_path, monkeypatch)
    order = batch_window._survey_batch
    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = order
    batch_window._survey_job_pass_ids = [batch_window._survey_rows[0].pass_id]
    batch_window._recompute_survey_start()
    # The running order's own queue is not the cart.
    assert batch_window._cart_button._count == 0

    add_video(batch_window, tmp_path, monkeypatch, name="GX010099.MP4")
    assert batch_window._cart_button._count == 1

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None


def _plan(*seconds_each):
    """A prediction of N passes, each expected to cost the seconds given."""
    from deepreefmap_gui.profiling.batch_estimate import BatchPrediction, PassPrediction

    passes = [
        PassPrediction(key=str(i), seconds=value, basis="exact")
        for i, value in enumerate(seconds_each)
    ]
    return BatchPrediction(
        passes=passes,
        total_s=sum(v for v in seconds_each if v is not None) or None,
        predicted_count=sum(1 for v in seconds_each if v is not None),
        unknown_count=sum(1 for v in seconds_each if v is None),
    )


def test_batch_card_spans_the_passes_still_queued():
    """Scenario: pass 2 of 10 is half done, and each pass is predicted at 20 min.

    Expected behaviour: the estimate covers the eight passes after this one, not
    just the remainder of the one in flight.
    """
    card = BatchProgressCard()
    card.set_batch_plan(_plan(*[1200.0] * 10))
    card.set_batch_context(2, 10, "North_reef")
    card.set_percent(50)
    card.set_eta_seconds(600.0)
    assert card.batch_remaining_s() == 600.0 + 8 * 1200.0


def test_the_queue_is_costed_before_the_batch_starts():
    """How long the evening is decides whether to run the batch or trim it first."""
    card = BatchProgressCard()
    card.set_batch_plan(_plan(600.0, 300.0, 900.0))
    assert card.batch_remaining_s() == 1800.0


def test_a_pass_costing_more_than_predicted_lengthens_the_rest():
    """The batch corrects its own estimate, once per pass rather than per tick."""
    card = BatchProgressCard()
    card.set_batch_plan(_plan(100.0, 100.0, 100.0))
    card.set_batch_context(1, 3, "North_reef")
    card.pass_finished(1, 200.0)
    card.set_batch_context(2, 3, "South_reef")

    # Twice as slow as predicted, so the two remaining passes are 200s each.
    assert card.batch_remaining_s() == 400.0


def test_batch_card_says_nothing_without_a_basis():
    card = BatchProgressCard()
    card.set_batch_plan(_plan(None, None))
    card.set_batch_context(1, 2, "North_reef")
    card.set_percent(1)
    assert card.batch_remaining_s() is None


def test_batch_card_counts_the_last_pass_alone():
    card = BatchProgressCard()
    card.set_batch_plan(_plan(1200.0, 1200.0, 1200.0))
    card.set_batch_context(3, 3, "North_reef")
    card.set_eta_seconds(90.0)
    assert card.batch_remaining_s() == 90.0


def test_running_row_carries_its_own_progress(batch_window, tmp_path, monkeypatch):
    """The queue is where a pass reports itself now that the Run step has no viewer."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    pass_id = batch_window._survey_rows[0].pass_id
    batch_window._survey_job_pass_ids = [pass_id]
    batch_window._survey_running_index = 0

    batch_window._on_pass_percent(42)
    item = batch_window._survey_pass_table.item(batch_window._table_row_of(0), _COL_STATUS)
    assert item.text() == "Running 42%"
    assert item.data(PASS_PERCENT_ROLE) == 42
    # Hovering raises the stage breakdown itself, so a tooltip saying to hover
    # lands on top of what it describes.
    assert item.toolTip() == ""

    # A pass that has stopped running loses the fill rather than freezing at it.
    batch_window._survey_running_index = None
    batch_window._refresh_survey_pass_statuses()
    assert item.data(PASS_PERCENT_ROLE) is None


def test_stopping_a_batch_leaves_it_continuable(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: two passes queued, the batch is cancelled before either runs.

    Expected behaviour: nothing is lost. Both stay queued and the button offers to
    continue rather than pretending this is a fresh batch.
    """
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)

    def cancel_immediately(**kwargs):
        batch_window._survey_cancel_event.set()
        raise ReconstructionCancelled

    monkeypatch.setattr(
        "deepreefmap.pipeline.orchestrator.run_reconstruction", cancel_immediately
    )
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    assert [r.status for r in batch_window._survey_store().list_runs()] == [
        "cancelled",
        "cancelled",
    ]
    assert len(batch_window._survey_remaining_rows()) == 2
    assert batch_window._survey_start_btn.text() == "Continue processing (2 passes)"


def test_batch_standing_reports_what_is_behind_you(batch_window, tmp_path, monkeypatch, qapp):
    from deepreefmap_gui.survey.models import RunRecord

    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    assign_transect(batch_window, 1)
    # A fresh batch has nothing behind it, so the line stays out of the way.
    assert not batch_window._survey_standing_label.isVisibleTo(batch_window)

    batch_window._survey_store().add_run(
        RunRecord(
            pass_id=batch_window._survey_rows[1].pass_id,
            run_dir_name="run_001",
            status="succeeded",
            batch_id=batch_window._survey_batch.id,
        )
    )
    batch_window._refresh_survey_batch_tab()
    text = batch_window._survey_standing_label.text()
    assert "1 done" in text
    assert "1 remaining" in text


def test_a_running_order_freezes_its_settings_and_keeps_the_rest_open(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a batch is running and the diver reaches for the table.

    Expected behaviour: what the order will run under can no longer change, and
    neither can its order. Taking a row out stays open, because the worker
    re-reads the cart; so does opening a section under Videos, which edits
    nothing here.
    """
    for name in ("GX010001.MP4", "GX010002.MP4"):
        add_video(batch_window, tmp_path, monkeypatch, name=name)
    assign_transect(batch_window, 0)
    table_row = batch_window._table_row_of(0)
    section_cells = [batch_window._survey_pass_table.cellWidget(table_row, _COL_SECTION)]
    frozen_controls = [
        batch_window._survey_batch_name,
        batch_window._survey_clear_cart_btn,
        batch_window._survey_settings_btn,
        batch_window._survey_audit_btn,
        batch_window._survey_sort_btn,
        batch_window._survey_pass_table.cellWidget(table_row, _COL_SETTINGS),
    ]
    delete = batch_window._survey_pass_table.cellWidget(table_row, _COL_ACTION)
    assert all(w.isEnabled() for w in [*frozen_controls, *section_cells, delete])

    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = batch_window._survey_batch
    batch_window._survey_job_pass_ids = [r.pass_id for r in batch_window._survey_rows]
    batch_window._survey_running_index = 0
    batch_window._recompute_row_actions()
    assert not any(w.isEnabled() for w in frozen_controls)
    assert not batch_window._survey_pass_table.dragEnabled()
    assert all(w.isEnabled() for w in [*section_cells, delete])

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None
    batch_window._recompute_row_actions()
    assert all(w.isEnabled() for w in [*frozen_controls, *section_cells])


def test_passes_queued_mid_run_land_in_the_next_session(
    batch_window, tmp_path, monkeypatch
):
    """Scenario: a batch is running and another section is cut from new footage.

    Expected behaviour: the pass queues into a fresh session, shown under the
    Next session divider, and the running order is untouched.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    order = batch_window._survey_batch
    batch_window._survey_worker_running = True
    batch_window._survey_running_batch = order
    batch_window._survey_job_pass_ids = [batch_window._survey_rows[0].pass_id]

    add_video(batch_window, tmp_path, monkeypatch, name="GX010099.MP4")

    assert batch_window._survey_batch.id != order.id
    states = batch_window._survey_row_states()
    assert states.count("next") == 1
    headings = group_headings(batch_window)
    assert any(h.startswith("Next session") for h in headings)
    assert not batch_window._survey_next_cart_label.isHidden()
    # The label names the session an addition joins and says when it will run.
    # It is the only statement that the table holds rows from two sessions.
    said = batch_window._survey_next_cart_label.text()
    assert batch_window._survey_batch.name in said
    assert "starts once this session finishes" in said
    # Two sessions with one name cannot be told apart by a label that names one.
    assert batch_window._survey_batch.name != order.name
    # The field still names the order being processed, so it cannot be edited
    # into renaming the wrong session.
    assert batch_window._survey_batch_name.isReadOnly()
    # The pending cart never leaks into what the running order will process.
    assert len(batch_window._survey_remaining_rows()) == 1

    batch_window._survey_worker_running = False
    batch_window._survey_running_batch = None


def test_no_settings_label_is_clipped(batch_window, tmp_path, monkeypatch):
    """Scenario: the cart's settings column, holding a button whose label is
    centred in it.

    Expected behaviour: the column fits what the button itself says it needs.
    Too narrow and the label is clipped at both ends rather than elided, which
    is how "Default settings" came out as "efault setting".
    """
    add_video(batch_window, tmp_path, monkeypatch)
    table = batch_window._survey_pass_table
    button = settings_cell(batch_window, 0)

    assert button.text() == "Default settings"
    assert table.columnWidth(_COL_SETTINGS) >= button.sizeHint().width()


def test_the_settings_column_follows_the_machine_s_ui_font(
    batch_window, tmp_path, monkeypatch
):
    """A width in pixels cannot be a constant: the same label is half as wide
    again on a laptop set to a larger interface font, which is where the old
    fixed 130px clipped it."""
    from PySide6.QtGui import QFont

    add_video(batch_window, tmp_path, monkeypatch)
    table = batch_window._survey_pass_table
    button = settings_cell(batch_window, 0)
    before = table.columnWidth(_COL_SETTINGS)

    larger = QFont(button.font())
    larger.setPointSizeF(button.font().pointSizeF() * 1.8)
    button.setFont(larger)
    batch_window._paint_settings_cell(0)

    assert table.columnWidth(_COL_SETTINGS) > before
    assert table.columnWidth(_COL_SETTINGS) >= button.sizeHint().width()


def test_a_moved_clip_relinks_on_readd(batch_window, tmp_path, monkeypatch):
    """A known clip added from a new folder is the same clip moved: its path
    follows, its row and everything referencing it stay put."""
    store = batch_window._survey_store()
    original = tmp_path / "GX010012.MP4"
    original.write_bytes(b"same bytes" * 4096)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.batch._probe_video", lambda _path: (60.0, 30.0)
    )
    batch_window._add_video_paths([str(original)])
    assert wait_until(lambda: len(store.list_videos()) == 1)
    first_id = store.list_videos()[0].id

    drive = tmp_path / "drive"
    drive.mkdir()
    moved = drive / "GX010012.MP4"
    moved.write_bytes(original.read_bytes())
    original.unlink()
    batch_window._add_video_paths([str(moved)])

    assert wait_until(lambda: store.list_videos()[0].path == str(moved))
    assert len(store.list_videos()) == 1
    assert store.list_videos()[0].id == first_id
    assert "Relinked 1 known clip" in batch_window._status_label.text()


def test_a_pass_that_dies_early_still_leaves_a_log(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """The log used to open after seeding, so a failure there wrote nothing.

    A pass that dies early is the one whose log somebody goes looking for.
    """
    def fail_seeding(*args, **kwargs):
        raise RuntimeError("seeding blew up")

    monkeypatch.setattr("deepreefmap_gui.runs.seeding.seed_from_settings", fail_seeding)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    logs = list(out_root.glob("*/run.log"))
    assert len(logs) == 1
    assert "seeding blew up" in logs[0].read_text()


def test_a_failed_pass_offers_its_log_and_its_folder(
    batch_window, tmp_path, out_root, monkeypatch, qapp
):
    """The row carries one truncated line; the log carries the traceback."""
    def fail(**kwargs):
        raise RuntimeError("mapping blew up")

    monkeypatch.setattr("deepreefmap.pipeline.orchestrator.run_reconstruction", fail)
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    batch_window._on_survey_start()
    await_batch(batch_window, qapp)

    run_dir = batch_window._survey_pass_run_dir(batch_window._survey_rows[0])
    assert run_dir is not None and (run_dir / "run.log").exists()

    batch_window._show_pass_log(run_dir)
    assert "mapping blew up" in batch_window._log_view._text.toPlainText()
    assert run_dir.name in batch_window._log_view._heading.text()


def test_the_live_log_is_not_interleaved_with_a_stored_one(qapp, tmp_path):
    """Two runs' records in one pane read as one, and neither would be true."""
    from deepreefmap_gui.system.log_view import LogView

    stored = tmp_path / "run.log"
    stored.write_text("what the failed run said\n", encoding="utf-8")
    view = LogView()
    view.show_file(stored, title="run-a")

    view.append_line("a line from this session")
    assert "a line from this session" not in view._text.toPlainText()

    view.show_live()
    view.append_line("a line from this session")
    assert view._text.toPlainText().strip() == "a line from this session"


def test_going_back_to_the_live_log_stops_offering_the_other_runs_file(qapp, tmp_path):
    """"Open log file" hands the path to the desktop, so a stale one opens the wrong run."""
    from deepreefmap_gui.system.log_view import LogView

    live = tmp_path / "live" / "run.log"
    live.parent.mkdir()
    live.write_text("live\n", encoding="utf-8")
    stored = tmp_path / "old" / "run.log"
    stored.parent.mkdir()
    stored.write_text("old\n", encoding="utf-8")

    view = LogView()
    view.set_current_log_path(live)
    view.show_file(stored, title="old")
    assert view._current_log_path == stored

    view.show_live()
    assert view._current_log_path == live


def test_the_session_estimate_reads_each_section_length(batch_window, tmp_path, monkeypatch):
    """A queue of short sections is not the same evening as a queue of long ones.

    Costing by pass count answered the same for both, which is what made the
    figure useless for deciding whether to start a batch before dinner.
    """
    add_video(batch_window, tmp_path, monkeypatch, name="GX010001.MP4", duration_s=600.0)
    add_video(batch_window, tmp_path, monkeypatch, name="GX010002.MP4", duration_s=600.0)
    short, long = batch_window._survey_rows
    short.begin_s, short.end_s = 0.0, 30.0
    long.begin_s, long.end_s = 0.0, 600.0

    specs = {s.key: s for s in batch_window._survey_pass_specs()}
    assert len(specs) == 2
    frames = sorted(s.frames for s in specs.values())
    assert frames[1] == frames[0] * 20


def test_the_prediction_is_recomputed_only_when_the_queue_changes(
    batch_window, tmp_path, monkeypatch
):
    """It reads a file, and every row mutation funnels through the recompute."""
    add_video(batch_window, tmp_path, monkeypatch)
    calls = []
    real = batch_estimate.predict_batch
    monkeypatch.setattr(
        batch_estimate, "predict_batch", lambda specs, **kw: calls.append(1) or real(specs, **kw)
    )
    batch_window._batch_prediction_cache = None

    batch_window._survey_batch_prediction()
    batch_window._survey_batch_prediction()
    assert len(calls) == 1

    batch_window._survey_rows[0].end_s = 12.0
    batch_window._survey_batch_prediction()
    assert len(calls) == 2


# --- What a section is called ---


def rename(window, row_index, monkeypatch, typed, accepted=True):
    """Rename a section the way its row's menu does."""
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (typed, accepted)))
    window._on_survey_rename(row_index)


def row_name(window, row_index):
    """What the row says it is called, which is its tooltip's first line now that
    the name has no column of its own."""
    table = window._survey_pass_table
    return table.item(window._table_row_of(row_index), _COL_VIDEO).toolTip().splitlines()[0]


def test_a_staged_section_is_named_without_anyone_typing(batch_window, tmp_path, monkeypatch):
    """A folder called Evan_1__p01__a3f9c2d1 is a fine directory and a poor name."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)

    shown = batch_window._row_label(batch_window._survey_rows[0])

    assert shown
    assert "__p" not in shown
    assert "pass" in shown.lower()
    # And it reaches the row, which no longer spends a column on it.
    assert shown in row_name(batch_window, 0)


def test_renaming_a_section_persists(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    row = batch_window._survey_rows[0]

    rename(batch_window, 0, monkeypatch, "North wall drift")

    stored = batch_window._survey_store().get_pass(row.pass_id)
    assert stored.label == "North wall drift"
    assert "North wall drift" in row_name(batch_window, 0)


def test_a_cancelled_rename_changes_nothing(batch_window, tmp_path, monkeypatch):
    add_video(batch_window, tmp_path, monkeypatch)
    row = batch_window._survey_rows[0]

    rename(batch_window, 0, monkeypatch, "Never applied", accepted=False)

    assert batch_window._survey_store().get_pass(row.pass_id).label == ""


def test_two_sections_cannot_share_a_name(batch_window, tmp_path, monkeypatch):
    """Two rows called the same thing cannot be told apart when one of them fails."""
    add_video(batch_window, tmp_path, monkeypatch, name="GX010001.MP4")
    add_video(batch_window, tmp_path, monkeypatch, name="GX010002.MP4")

    rename(batch_window, 0, monkeypatch, "Same name")
    rename(batch_window, 1, monkeypatch, "Same name")

    labels = [
        batch_window._survey_store().get_pass(r.pass_id).label
        for r in batch_window._survey_rows
    ]
    assert labels == ["Same name", "Same name 2"]
    assert "already called" in batch_window._status_label.text()


def test_clearing_the_name_restores_the_generated_one(batch_window, tmp_path, monkeypatch):
    """An emptied field asks for the default back, not for a nameless section."""
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    generated = batch_window._row_label(batch_window._survey_rows[0])

    rename(batch_window, 0, monkeypatch, "Something else")
    rename(batch_window, 0, monkeypatch, "")

    row = batch_window._survey_rows[0]
    assert batch_window._survey_store().get_pass(row.pass_id).label == ""
    assert generated in row_name(batch_window, 0)


# --- What the cart's columns spend their width on ---------------------------

def test_the_clip_and_the_section_take_the_width(batch_window, tmp_path, monkeypatch, qapp):
    """Scenario: a wide window, a long clip name and a long transect name.

    Expected behaviour: both read in full. The name column took the table's only
    stretch, which left the two columns identifying a row pinned at a constant
    and elided, and made the name the one column nobody could drag.
    """
    add_video(batch_window, tmp_path, monkeypatch)
    assign_transect(batch_window, 0)
    table = batch_window._survey_pass_table
    table.resize(1400, 400)
    table.show()
    qapp.processEvents()
    qapp.processEvents()

    assert table.columnWidth(_COL_SECTION) >= 200
    assert table.columnWidth(_COL_VIDEO) >= 120
    assert table.columnWidth(_COL_SECTION) > table.columnWidth(_COL_VIDEO)


def test_a_narrow_window_drops_recorded_before_it_squeezes_a_name(
    batch_window, tmp_path, monkeypatch, qapp
):
    """The clip name and the section window already place the footage in time."""
    add_video(batch_window, tmp_path, monkeypatch)
    table = batch_window._survey_pass_table
    table.resize(640, 400)
    table.show()
    qapp.processEvents()
    qapp.processEvents()

    assert table.isColumnHidden(_COL_RECORDED)
    assert not table.isColumnHidden(_COL_SECTION)
    assert table.columnWidth(_COL_SECTION) >= 200


def test_the_facts_a_dropped_column_carried_stay_on_the_row(
    batch_window, tmp_path, monkeypatch
):
    """Recorded and Length can both be hidden, so neither may be the only place
    its fact is reachable."""
    add_video(batch_window, tmp_path, monkeypatch)
    table = batch_window._survey_pass_table

    tooltip = table.item(batch_window._table_row_of(0), _COL_VIDEO).toolTip()

    assert "Recorded" in tooltip
    assert "Section runs" in tooltip


def test_a_repainted_settings_button_does_not_read_as_a_dragged_column(
    batch_window, tmp_path, monkeypatch, qapp
):
    """It measures itself on every repaint, and a width written straight to the
    header would pin the column and stop every other one refitting."""
    add_video(batch_window, tmp_path, monkeypatch)
    table = batch_window._survey_pass_table
    table.resize(1400, 400)
    table.show()
    qapp.processEvents()
    qapp.processEvents()
    before = table.columnWidth(_COL_SECTION)

    batch_window._rebuild_survey_table()
    qapp.processEvents()
    qapp.processEvents()

    assert table.column_sizer._pinned == {}
    assert table.columnWidth(_COL_SECTION) == before


def _publish_server_preset(store, name="Expedition standard", version=1, settings=None):
    """Land a preset the way a pull would: straight into the store's table."""
    from deepreefmap_gui.survey.models.server_preset import ServerPreset

    preset = ServerPreset(name=name, version=version, settings=settings or {"fps": 3})
    store._add("server_preset", preset)
    return preset


def _pin_server_preset(window, name, version):
    import json as json_mod

    from deepreefmap_gui.simple.mode import SERVER_PRESET_KEY

    window._survey_store().set_sync_state(
        SERVER_PRESET_KEY, json_mod.dumps({"name": name, "version": version})
    )
    window._reload_active_preset()
    window._survey_preset_label.setText(window._survey_preset_summary())


def test_a_degraded_server_preset_is_said_on_the_settings_label(window):
    """A preset naming settings this build cannot apply is offered and reported,
    never silently patched: the dropped value is the one the author meant."""
    store = window._survey_store()
    _publish_server_preset(
        store,
        settings={"fps": 3, "coral_iq": 11, "loger_model_path": "/home/kim/latest.pt"},
    )

    _pin_server_preset(window, "Expedition standard", 1)

    label = window._survey_preset_label.text()
    assert "Expedition standard" in label
    assert "cannot apply" in label
    assert "coral iq" in label  # describe_key speaks without underscores
    assert "another machine's disk" in label


def test_a_withdrawn_server_preset_falls_back_out_loud(window):
    store = window._survey_store()
    _publish_server_preset(store)
    _pin_server_preset(window, "Expedition standard", 1)
    assert "no longer on the registry" not in window._survey_preset_label.text()

    # The console withdraws it; the next sync tombstones the row.
    preset = store.list_server_presets()[0]
    preset.deleted_at = preset.updated_at
    store._update("server_preset", preset)
    _pin_server_preset(window, "Expedition standard", 1)

    label = window._survey_preset_label.text()
    assert "no longer on the registry" in label
    assert "standard settings are in force" in label


def test_every_table_names_a_column_width_store(window):
    """Dragged column widths survive a restart on every page with a table."""
    assert window._pass_column_sizer._settings_key == "passes"
    assert window._transect_list.column_sizer._settings_key == "transects"
