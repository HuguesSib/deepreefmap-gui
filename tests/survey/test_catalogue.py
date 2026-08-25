import json

import pytest
from _factories import (
    make_batch,
    make_transect,
    make_video,
    seed_pass,
    seed_survey_run,
    write_run,
)

from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import UNASSIGNED_TITLE
from deepreefmap_gui.survey.models import RunRecord
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def out_root(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    return root


def scan(out_root, store=None):
    entries = catalogue.scan_out_root(out_root)
    if store is not None:
        catalogue.reconcile(entries, store)
    return entries


def test_scan_orders_newest_first(out_root):
    write_run(out_root, "older", run_timestamp="2026-07-01T10:00:00+00:00")
    write_run(out_root, "newer", run_timestamp="2026-07-02T10:00:00+00:00")
    assert [e.dir_name for e in catalogue.scan_out_root(out_root)] == ["newer", "older"]


def test_group_key_falls_back_from_pass_to_footage(out_root, store):
    seed_survey_run(store, out_root, "with_pass")
    write_run(out_root, "adhoc_a", begin_s=0.0, end_s=60.0)
    write_run(out_root, "adhoc_b", begin_s=0.0, end_s=60.0)
    write_run(out_root, "adhoc_other_window", begin_s=60.0, end_s=120.0)
    entries = {e.dir_name: e for e in scan(out_root, store)}
    assert catalogue.group_key(entries["with_pass"])[0] == "pass"
    assert catalogue.group_key(entries["adhoc_a"]) == catalogue.group_key(entries["adhoc_b"])
    assert catalogue.group_key(entries["adhoc_a"]) != catalogue.group_key(
        entries["adhoc_other_window"]
    )


def test_manifest_pass_id_groups_without_database(out_root, store):
    seed_survey_run(store, out_root, "original")
    fresh = SurveyStore(out_root / "fresh.db")
    entries = scan(out_root, fresh)
    assert catalogue.group_key(entries[0]) == ("pass", str(entries[0].manifest_pass_id))


def test_transects_facet_puts_unassigned_first(out_root, store):
    seed_survey_run(store, out_root, "assigned")
    write_run(out_root, "loose")
    groups = catalogue.transects_facet(scan(out_root, store), store.list_transects())
    assert groups[0].title == UNASSIGNED_TITLE
    assert [e.dir_name for e in groups[0].entries] == ["loose"]
    assert groups[1].title == "T1"
    assert groups[1].children[0].entries[0].dir_name == "assigned"


def test_transects_facet_lists_transects_without_runs(out_root, store):
    store.add_transect(make_transect("Empty"))
    groups = catalogue.transects_facet(scan(out_root, store), store.list_transects())
    assert [g.title for g in groups] == ["Empty"]
    assert groups[0].all_entries() == []


def test_sessions_facet_gathers_a_day_across_transects(out_root, store):
    """The session is the only container that spans transects, which is the
    whole reason it earns a facet of its own."""
    batch = make_batch(store, "2026-07-01")
    seed_survey_run(store, out_root, "north", transect=make_transect("North"), batch=batch)
    seed_survey_run(store, out_root, "south", transect=make_transect("South"), batch=batch)

    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["2026-07-01"]
    assert sorted(e.dir_name for e in groups[0].all_entries()) == ["north", "south"]
    assert catalogue.session_summary(groups[0]) == "2 runs · 2 transects"


def test_sessions_facet_orders_newest_first(out_root, store):
    older = make_batch(store, "2026-06-01")
    newer = make_batch(store, "2026-07-01")
    seed_survey_run(
        store, out_root, "old_run", batch=older, run_timestamp="2026-06-01T10:00:00+00:00"
    )
    seed_survey_run(
        store,
        out_root,
        "new_run",
        transect=make_transect("Other"),
        batch=newer,
        run_timestamp="2026-07-01T10:00:00+00:00",
    )
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["2026-07-01", "2026-06-01"]


def test_sessions_facet_surfaces_runs_with_no_session_first(out_root, store):
    """Runs from before sessions were recorded still have to be reachable."""
    batch = make_batch(store)
    seed_survey_run(store, out_root, "filed", batch=batch)
    write_run(out_root, "loose")
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert groups[0].title == catalogue.UNFILED_SESSION_TITLE
    assert [e.dir_name for e in groups[0].entries] == ["loose"]


def test_sessions_facet_lists_a_session_with_no_runs(out_root, store):
    make_batch(store, "Empty day")
    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    assert [g.title for g in groups] == ["Empty day"]
    assert groups[0].all_entries() == []


def test_session_key_agrees_from_the_manifest_and_the_database(out_root, store):
    """Both sides of the join have to land on one key or a session splits in two.

    A run folder copied off another machine has a manifest and no row here, so
    the manifest fallback is the case that matters.
    """
    batch = make_batch(store)
    seed_survey_run(store, out_root, "run_a", batch=batch)
    fresh = SurveyStore(out_root / "fresh.db")
    entry = scan(out_root, fresh)[0]
    assert entry.db_pass is None
    assert entry.session_id == batch.id
    assert catalogue.session_group_key(entry.session_id) == catalogue.session_group_key(batch.id)


def test_a_rerun_in_a_later_session_files_under_that_session(out_root, store):
    """The run's own session outranks the pass's: a rerun is the same pass
    ordered again, so its attempt belongs to the day it was placed."""
    first = make_batch(store, "2026-07-01")
    transect, pass_, _run = seed_survey_run(store, out_root, "attempt1", batch=first)
    second = make_batch(store, "2026-07-02")
    rerun = RunRecord(
        pass_id=pass_.id, run_dir_name="attempt2", status="succeeded", batch_id=second.id
    )
    store.add_run(rerun)
    write_run(
        out_root, "attempt2",
        run_timestamp="2026-07-02T10:00:00+00:00",
        survey=survey_manifest_block(rerun, pass_, transect, second),
    )

    groups = catalogue.sessions_facet(scan(out_root, store), store.list_batches())
    by_title = {g.title: [e.dir_name for e in g.all_entries()] for g in groups}
    assert by_title == {"2026-07-01": ["attempt1"], "2026-07-02": ["attempt2"]}


def test_run_entry_carries_its_session_name(out_root, store):
    batch = make_batch(store, "Day 1")
    seed_survey_run(store, out_root, "run_a", batch=batch)
    entry = scan(out_root, store)[0]
    assert entry.db_session_name == "Day 1"
    assert entry.session_name == "Day 1"


@pytest.mark.parametrize(
    "recorded, shown",
    [("failed", "failed"), ("cancelled", "cancelled"), ("interrupted", "succeeded")],
)
def test_entry_status_lets_a_recorded_failure_override_the_manifest(
    out_root, store, recorded, shown
):
    """Scenario: the run wrote its manifest, then the batch recorded a failure
    (the fold-in step raised) or the app died holding the row.

    Expected behaviour: failed and cancelled override the manifest; interrupted
    does not, because reconcile-on-open stamps complete runs too.
    """
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="late", status=recorded)
    store.add_run(run)
    write_run(out_root, "late")
    entry = scan(out_root, store)[0]
    assert catalogue.entry_status(entry) == shown


def test_reconcile_database_wins_and_records_move(out_root, store):
    transect, pass_, _run = seed_survey_run(store, out_root, "run1")
    elsewhere = make_transect("Elsewhere")
    store.add_transect(elsewhere)
    pass_.transect_id = elsewhere.id
    store.update_pass(pass_)
    entry = scan(out_root, store)[0]
    assert entry.transect_name == "Elsewhere"
    assert entry.moved_from == "T1"


def test_reconcile_still_names_a_line_the_registry_retired(out_root, store):
    """Scenario: a curator retires the line a finished run was swum on.

    Expected behaviour: the run is still filed under it. Reading the retired
    line as absent files the run as belonging to no transect, and Browse then
    asks somebody to assign it to a line nothing lists any more.
    """
    from datetime import datetime, timedelta, timezone

    transect, _pass, _run = seed_survey_run(store, out_root, "run1")
    retired_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    store.apply_from_server("transects", [
        {"id": str(transect.id), "deleted_at": retired_at, "updated_at": retired_at}
    ])

    entry = scan(out_root, store)[0]

    assert entry.db_transect_name == "T1"
    assert entry.moved_from is None


def test_rename_run_rewrites_manifest_in_place(out_root):
    run_dir = write_run(out_root, "run1")
    manifest = catalogue.rename_run(run_dir, "  reef north  ")
    assert manifest["name"] == "reef north"
    on_disk = json.loads((run_dir / "run_manifest.json").read_text())
    assert on_disk["name"] == "reef north"
    assert not (run_dir / "run_manifest.json.tmp").exists()


def test_delete_run_refuses_outside_root(out_root, tmp_path, store):
    stray = tmp_path / "elsewhere" / "run1"
    stray.mkdir(parents=True)
    (stray / "run_manifest.json").write_text("{}")
    with pytest.raises(ValueError):
        catalogue.delete_run(out_root, stray, store)
    nested = write_run(out_root, "real")
    (nested / "no_manifest").mkdir()
    with pytest.raises(ValueError):
        catalogue.delete_run(out_root, nested / "no_manifest", store)


def test_delete_run_removes_directory_and_row(out_root, store):
    _t, _p, run = seed_survey_run(store, out_root, "doomed")
    catalogue.delete_run(out_root, out_root / "doomed", store)
    assert not (out_root / "doomed").exists()
    assert store.get_run(run.id) is None


def test_assign_moves_pass_with_sibling_reruns(out_root, store):
    transect, pass_, _run = seed_survey_run(store, out_root, "take1")
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="take2", status="succeeded"))
    write_run(out_root, "take2")
    target = make_transect("Target")
    store.add_transect(target)
    entries = scan(out_root, store)
    catalogue.assign_to_transect(store, [e for e in entries if e.dir_name == "take1"], target.id)
    assert all(e.transect_name == "Target" for e in scan(out_root, store))


def test_assign_adopts_adhoc_runs_as_one_pass(out_root, store):
    write_run(out_root, "adhoc_a")
    write_run(out_root, "adhoc_b")
    target = make_transect("Target")
    store.add_transect(target)
    catalogue.assign_to_transect(store, scan(out_root, store), target.id)
    passes = store.list_passes(transect_id=target.id)
    assert len(passes) == 1
    assert {r.run_dir_name for r in store.runs_for_pass(passes[0].id)} == {"adhoc_a", "adhoc_b"}


def test_assign_derives_window_from_frames_when_untrimmed(out_root, store):
    write_run(out_root, "untrimmed", begin_s=None, end_s=None, frames_processed=900, fps=3)
    target = make_transect("Target")
    store.add_transect(target)
    catalogue.assign_to_transect(store, scan(out_root, store), target.id)
    pass_ = store.list_passes(transect_id=target.id)[0]
    assert pass_.begin_s == 0.0
    assert pass_.end_s == pytest.approx(300.0)


def test_assign_refuses_a_run_whose_time_range_cannot_be_recovered(out_root, store):
    """Both the trim and the frame-count fallback are absent, so there is no
    window to give the pass. Adopting it anyway would write begin == end, which
    TransectPass rejects further down with a message naming neither run."""
    write_run(out_root, "no_window", begin_s=None, end_s=None, frames_processed=None, fps=None)
    target = make_transect("Target")
    store.add_transect(target)

    with pytest.raises(ValueError, match="no_window"):
        catalogue.assign_to_transect(store, scan(out_root, store), target.id)

    assert store.list_passes(transect_id=target.id) == []


def test_assign_keeps_manifest_ids_so_rebuild_stays_idempotent(out_root, store):
    seed_survey_run(store, out_root, "copied")
    fresh = SurveyStore(out_root / "fresh.db")
    target = make_transect("Target")
    fresh.add_transect(target)
    entries = scan(out_root, fresh)
    catalogue.assign_to_transect(fresh, entries, target.id)
    report = fresh.rebuild_from_scan(out_root)
    assert report.transects == 1 and report.passes == 0 and report.runs == 0
    entry = scan(out_root, fresh)[0]
    assert entry.transect_name == "Target"
    assert entry.moved_from == "T1"


def test_scan_incomplete_runs_surfaces_recorded_crash(out_root, store):
    _t, _v, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="crashed", status="running")
    store.add_run(run)
    (out_root / "crashed").mkdir()
    incomplete = catalogue.scan_incomplete_runs(out_root, store, set())
    assert [e.dir_name for e in incomplete] == ["crashed"]
    assert incomplete[0].incomplete
    catalogue.reconcile(incomplete, store)
    assert incomplete[0].status_label == "running"


def test_scan_incomplete_runs_detects_run_log_without_record(out_root):
    half = out_root / "halfrun"
    half.mkdir()
    (half / "run.log").write_text("started")
    incomplete = catalogue.scan_incomplete_runs(out_root, None, set())
    assert [e.dir_name for e in incomplete] == ["halfrun"]
    assert incomplete[0].status_label == "incomplete"


def test_scan_incomplete_runs_ignores_complete_known_and_bare_dirs(out_root, store):
    write_run(out_root, "done")
    (out_root / "random").mkdir()
    incomplete = catalogue.scan_incomplete_runs(out_root, store, {"done"})
    assert incomplete == []


def test_delete_run_dir_removes_manifestless_dir_and_row(out_root, store):
    _t, _v, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="crashed", status="failed")
    store.add_run(run)
    (out_root / "crashed").mkdir()
    catalogue.delete_run_dir(out_root, out_root / "crashed", store)
    assert not (out_root / "crashed").exists()
    assert store.get_run(run.id) is None


def test_delete_run_dir_refuses_outside_root(out_root, tmp_path, store):
    stray = tmp_path / "elsewhere" / "run1"
    stray.mkdir(parents=True)
    with pytest.raises(ValueError):
        catalogue.delete_run_dir(out_root, stray, store)


def test_video_library_flags_orphans(out_root, store):
    _t, video, _pass = seed_pass(store)
    orphan = store.upsert_video(
        make_video(content_hash="ff" * 16, file_name="orphan.mp4", path="/data/orphan.mp4")
    )
    by_id = {
        e.video.id: e
        for e in catalogue.video_library(
            store.list_videos(), store.list_passes(), store.list_runs()
        )
    }
    assert by_id[video.id].pass_count == 1
    assert not by_id[video.id].orphan
    assert by_id[orphan.id].orphan


def test_group_stats_ranges(out_root):
    write_run(out_root, "small", run_duration_s=100.0, semantic_reference_points=1_000)
    write_run(out_root, "large", run_duration_s=300.0, semantic_reference_points=9_000)
    entries = catalogue.scan_out_root(out_root)
    entries[0].size_bytes = 5
    stats = catalogue.group_stats(entries)
    assert stats.run_count == 2
    assert stats.total_bytes == 5
    assert stats.duration_range == (100.0, 300.0)
    assert stats.point_range == (1_000, 9_000)


def test_library_counts_a_pass_against_every_chapter(store):
    """The second half of a swim the camera split is not an unused clip."""
    transect, video, pass_ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    pass_.extra_video_ids = [second.id]
    store.update_pass(pass_)

    entries = {e.video.file_name: e for e in catalogue.video_library(
        store.list_videos(), store.list_passes()
    )}
    assert entries["GX020001.MP4"].pass_count == 1
    assert not entries["GX020001.MP4"].orphan


def test_a_run_record_without_a_folder_surfaces_as_data_removed(out_root, store):
    _transect, _video, pass_ = seed_pass(store)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="gone", status="succeeded"))
    write_run(out_root, "still_here")
    entries = catalogue.scan_out_root(out_root)
    entries += catalogue.missing_run_entries(
        out_root, store, {e.dir_name for e in entries}
    )
    catalogue.reconcile(entries, store)
    removed = [e for e in entries if e.data_missing]
    assert [e.dir_name for e in removed] == ["gone"]
    assert removed[0].db_run is not None
    assert catalogue.entry_status(removed[0]) == "succeeded"


def test_deleting_run_data_keeps_the_record(out_root, store):
    _transect, _video, pass_ = seed_pass(store)
    run_dir = write_run(out_root, "r1")
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="r1", status="succeeded"))
    catalogue.delete_run_data(out_root, run_dir)
    assert not run_dir.exists()
    assert store.run_by_dir_name("r1") is not None


def test_run_data_outside_the_output_root_is_refused(out_root, tmp_path):
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    with pytest.raises(ValueError):
        catalogue.delete_run_data(out_root, stray)
