import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from _factories import (
    VIDEO_HASH,
    VIDEO_PATH,
    make_batch,
    make_transect,
    make_video,
    seed_pass,
    seed_survey_run,
    write_v0_2_0_database,
)

from deepreefmap_gui.survey.models import (
    BatchItem,
    Campaign,
    RunRecord,
    Site,
    SurveyBatch,
    TransectPass,
)
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SYNC_SECTIONS, SurveyStore
from deepreefmap_gui.survey.video_probe import UNKNOWN, YES


def test_site_crud_round_trip(store):
    site = Site(name="Kadda Dabali", country="Djibouti", latitude=11.6, longitude=43.1)
    store.add_site(site)
    assert store.get_site(site.id) == site
    site.region = "Gulf of Tadjoura"
    site.updated_at = "2000-01-01T00:00:00+00:00"
    store.update_site(site)
    stored = store.get_site(site.id)
    assert stored.region == "Gulf of Tadjoura"
    assert stored.updated_at > "2000-01-01T00:00:00+00:00"


def test_site_names_are_unique_case_insensitively(store):
    store.add_site(Site(name="Japanese Garden"))
    with pytest.raises(sqlite3.IntegrityError):
        store.add_site(Site(name="japanese garden"))


def test_list_sites_orders_by_name(store):
    store.add_site(Site(name="Wall"))
    store.add_site(Site(name="Reef"))
    assert [s.name for s in store.list_sites()] == ["Reef", "Wall"]


def test_campaign_crud_round_trip(store):
    campaign = Campaign(name="2025_10_eritrea", begin_date="2025-10-01", end_date="2025-10-20")
    store.add_campaign(campaign)
    assert store.get_campaign(campaign.id) == campaign
    campaign.end_date = "2025-10-25"
    campaign.updated_at = "2000-01-01T00:00:00+00:00"
    store.update_campaign(campaign)
    stored = store.get_campaign(campaign.id)
    assert stored.end_date == "2025-10-25"
    assert stored.updated_at > "2000-01-01T00:00:00+00:00"


def test_campaign_names_are_unique_case_insensitively(store):
    store.add_campaign(Campaign(name="2025_10_eritrea"))
    with pytest.raises(sqlite3.IntegrityError):
        store.add_campaign(Campaign(name="2025_10_ERITREA"))


def test_list_campaigns_puts_the_newest_expedition_first(store):
    store.add_campaign(Campaign(name="2024_04_fiji", begin_date="2024-04-02"))
    store.add_campaign(Campaign(name="2025_10_eritrea", begin_date="2025-10-01"))
    assert [c.name for c in store.list_campaigns()] == ["2025_10_eritrea", "2024_04_fiji"]


def test_the_default_campaign_is_remembered_until_it_is_withdrawn(store):
    """A tombstoned campaign answers None: new work must not be attributed to a
    campaign the registry has removed."""
    campaign = Campaign(name="2025_10_eritrea")
    store.add_campaign(campaign)
    store.set_default_campaign(campaign.id)
    assert store.default_campaign_id() == campaign.id

    campaign.deleted_at = campaign.updated_at
    store.update_campaign(campaign)

    assert store.default_campaign_id() is None


def test_a_pass_names_a_campaign_and_a_quality(store):
    campaign = Campaign(name="2025_10_eritrea")
    store.add_campaign(campaign)
    _, _, pass_ = seed_pass(store)
    pass_.campaign_id = campaign.id
    pass_.quality = "very_good"
    pass_.upside_down = True
    store.update_pass(pass_)
    stored = store.get_pass(pass_.id)
    assert (stored.campaign_id, stored.quality, stored.upside_down) == (
        campaign.id, "very_good", True,
    )


def test_the_quality_scale_is_enforced_by_the_column_as_well(store):
    """A row arriving from the registry is written straight into the table, so
    the column carries the same scale the model validates."""
    _, _, pass_ = seed_pass(store)
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "UPDATE transect_pass SET quality = 'brilliant' WHERE id = ?", (str(pass_.id),)
        )
    conn.close()


def test_editing_a_syncable_row_moves_its_updated_at(store):
    """Last-write-wins compares updated_at, so an edit that leaves it alone is
    an edit the registry would discard."""
    _, video, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    stale = "2000-01-01T00:00:00+00:00"
    conn = sqlite3.connect(store.path)
    with conn:
        for table, row_id in (("video_asset", video.id), ("transect_pass", pass_.id),
                              ("run_record", run.id)):
            conn.execute(
                f"UPDATE {table} SET updated_at = ? WHERE id = ?", (stale, str(row_id))
            )
    conn.close()

    store.update_video(store.get_video(video.id))
    store.update_pass(store.get_pass(pass_.id))
    store.set_run_status(run.id, "succeeded")

    assert store.get_video(video.id).updated_at > stale
    assert store.get_pass(pass_.id).updated_at > stale
    assert store.get_run(run.id).updated_at > stale


def test_transect_crud_round_trip(store):
    transect = make_transect()
    store.add_transect(transect)
    assert store.get_transect(transect.id) == transect
    transect.end_lat = -17.501
    before = transect.updated_at
    transect.updated_at = "2000-01-01T00:00:00+00:00"
    store.update_transect(transect)
    stored = store.get_transect(transect.id)
    assert stored.end_lat == -17.501
    assert stored.updated_at >= before
    store.delete_transect(transect.id)
    assert store.get_transect(transect.id) is None


def test_transect_names_are_unique_per_site_and_case_insensitively(store):
    """Scenario: two reefs each have a line the divers call T1.

    Expected behaviour: both are accepted, and a second T1 on either is not,
    however it is capitalised. Unassigned lines are held to the same rule, which
    is the store's own doing: see the test below.
    """
    reef, wall = Site(name="Reef"), Site(name="Wall")
    store.add_site(reef)
    store.add_site(wall)
    store.add_transect(make_transect(site_id=reef.id))
    store.add_transect(make_transect(site_id=wall.id))
    with pytest.raises(sqlite3.IntegrityError):
        store.add_transect(make_transect("t1", site_id=reef.id))
    store.add_transect(make_transect(site_id=None))
    with pytest.raises(sqlite3.IntegrityError):
        store.add_transect(make_transect("T1", site_id=None))
    assert len(store.list_transects()) == 3


def test_the_column_lets_two_unassigned_lines_share_a_name(store):
    """Scenario: two laptops each filed a T1 against no site, and both are pulled.

    Expected behaviour: the table takes them. A site of None is not a site, so a
    unique index cannot tie the pair together, and the registry's own partial
    index has the same hole. A stricter column here would refuse rows the server
    has already accepted, which is a sync that can never finish.
    """
    store.add_transect(make_transect(site_id=None))
    twin = make_transect(site_id=None)
    conn = sqlite3.connect(store.path)
    with conn:
        conn.execute(
            "INSERT INTO transect (id, name, description, start_lat, start_lon, end_lat,"
            " end_lon, created_at, updated_at) VALUES (?, ?, '', 1, 2, 3, 4, ?, ?)",
            (str(twin.id), twin.name, twin.created_at, twin.updated_at),
        )
    conn.close()
    assert [t.name for t in store.list_transects()] == ["T1", "T1"]


def test_a_tombstoned_transect_name_is_free_again(store):
    site = Site(name="Reef")
    store.add_site(site)
    first = make_transect(site_id=site.id)
    store.add_transect(first)
    first.deleted_at = "2026-08-01T00:00:00+00:00"
    store.update_transect(first)
    store.add_transect(make_transect(site_id=site.id))


def test_list_transects_orders_by_name(store):
    store.add_transect(make_transect("B"))
    store.add_transect(make_transect("A"))
    assert [t.name for t in store.list_transects()] == ["A", "B"]


def test_upsert_video_dedups_by_hash(store):
    first = store.upsert_video(make_video())
    again = make_video()
    again.path = "/moved/GX010001.MP4"
    again.duration_s = 61.5
    second = store.upsert_video(again)
    assert second.id == first.id
    assert len(store.list_videos()) == 1
    stored = store.get_video(first.id)
    assert stored.path == "/moved/GX010001.MP4"
    assert stored.duration_s == 61.5


def test_upsert_video_without_hash_falls_back_to_the_path(store):
    """Hashing needs the file to be readable, and in the field it often is not,
    so the path is what holds a clip on an unplugged drive to one row."""
    store.upsert_video(make_video(content_hash=None))
    store.upsert_video(make_video(content_hash=None))
    assert len(store.list_videos()) == 1

    store.upsert_video(make_video(content_hash=None, path="/data/elsewhere.MP4"))
    assert len(store.list_videos()) == 2


def test_pass_filters(store):
    transect, video, pass_ = seed_pass(store)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    second = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        batch_id=batch.id,
        begin_s=70.0,
        end_s=120.0,
        direction="reverse",
    )
    store.add_pass(second)
    assert len(store.list_passes()) == 2
    assert store.list_passes(transect_id=transect.id) == [pass_, second]
    assert store.list_passes(batch_id=batch.id) == [second]


def test_delete_transect_with_passes_is_blocked(store):
    transect, _, _ = seed_pass(store)
    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete_transect(transect.id)
    assert store.get_transect(transect.id) is not None


def test_run_status_stamps_lifecycle(store):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    store.set_run_status(run.id, "running")
    started = store.get_run(run.id)
    assert started.status == "running"
    assert started.started_at is not None
    assert started.finished_at is None
    store.set_run_status(run.id, "failed", error="boom")
    finished = store.get_run(run.id)
    assert finished.status == "failed"
    assert finished.error == "boom"
    assert finished.finished_at is not None


def test_reconcile_marks_non_terminal_runs_interrupted(store):
    """Pending and running rows are leftovers once the process is gone; a
    terminal succeeded/failed/cancelled row is a finished decision and stays."""
    _, _, pass_ = seed_pass(store)
    running = RunRecord(pass_id=pass_.id, run_dir_name="running")
    pending = RunRecord(pass_id=pass_.id, run_dir_name="pending")
    done = RunRecord(pass_id=pass_.id, run_dir_name="done")
    for record in (running, pending, done):
        store.add_run(record)
    store.set_run_status(running.id, "running")
    store.set_run_status(done.id, "succeeded")

    assert store.reconcile_interrupted_runs() == 2
    assert store.get_run(running.id).status == "interrupted"
    assert store.get_run(pending.id).status == "interrupted"
    assert store.get_run(done.id).status == "succeeded"
    reconciled = store.get_run(running.id)
    assert reconciled.finished_at is not None
    assert reconciled.error


def test_reopening_a_store_reconciles_stale_runs(tmp_path):
    """Scenario: a crash left a run 'running'.

    Expected behaviour: the next open flips it to interrupted, so it reads as
    work to redo rather than live work that blocks the pass forever.
    """
    path = tmp_path / "survey.db"
    store = SurveyStore(path)
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    store.set_run_status(run.id, "running")
    store.close()

    reopened = SurveyStore(path)
    assert reopened.get_run(run.id).status == "interrupted"


def test_a_newer_schema_is_refused_rather_than_opened_blindly(tmp_path):
    """Scenario: an update was rolled back, leaving a newer survey.db behind.

    Expected behaviour: opening it raises a readable error, rather than running
    an empty migration slice and reading columns this build does not understand.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    path = tmp_path / "survey.db"
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_schema_version() + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="schema v"):
        SurveyStore(path)


def test_run_status_rejects_unknown_run_and_status(store):
    _, _, pass_ = seed_pass(store)
    with pytest.raises(KeyError):
        store.set_run_status(uuid.uuid4(), "running")
    with pytest.raises(ValueError):
        run = RunRecord(pass_id=pass_.id, run_dir_name="run")
        store.add_run(run)
        store.set_run_status(run.id, "exploded")


def test_runs_for_transect_joins_passes(store):
    transect, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    assert store.runs_for_transect(transect.id) == [store.get_run(run.id)]
    assert store.runs_for_pass(pass_.id) == [store.get_run(run.id)]


def test_run_lookup_by_dir_name_and_delete(store):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    assert store.run_by_dir_name("t1__p01") == store.get_run(run.id)
    assert store.run_by_dir_name("missing") is None
    store.delete_run(run.id)
    assert store.get_run(run.id) is None


def test_json_export_import_round_trip(store, tmp_path):
    site, campaign = Site(name="Reef"), Campaign(name="2025_10_eritrea")
    store.add_site(site)
    store.add_campaign(campaign)
    _, _, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    pass_.campaign_id = campaign.id
    pass_.quality = "good"
    store.update_pass(pass_)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=batch.id))
    doc_path = tmp_path / "survey.json"
    store.export_json(doc_path)

    fresh = SurveyStore(tmp_path / "fresh.db")
    fresh.import_json(doc_path)
    assert fresh.list_sites() == store.list_sites()
    assert fresh.list_campaigns() == store.list_campaigns()
    assert fresh.list_transects() == store.list_transects()
    assert fresh.list_videos() == store.list_videos()
    assert fresh.list_passes() == store.list_passes()
    assert fresh.list_runs() == store.list_runs()
    assert fresh.list_all_batch_items() == store.list_all_batch_items()

    fresh.import_json(doc_path)
    assert len(fresh.list_passes()) == 1
    assert len(fresh.list_all_batch_items()) == 1


def write_manifest(out_root, run_dir_name, block, video_path="/data/GX010001.MP4"):
    run_dir = out_root / run_dir_name
    run_dir.mkdir(parents=True)
    manifest = {
        "name": run_dir_name,
        "input_videos": [video_path],
        "video_hashes": ["ab" * 16],
        "video_sizes": [1024],
        "video_mtimes": ["2026-07-20T09:00:00+00:00"],
        "run_timestamp": "2026-07-20T09:05:00+00:00",
        "survey": block,
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))


def test_rebuild_from_scan_restores_everything(store, tmp_path):
    transect, video, pass_ = seed_pass(store, direction="reverse")
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    out_root = tmp_path / "out"
    write_manifest(out_root, "t1__p01", survey_manifest_block(run, pass_, transect, batch))
    (out_root / "plain_run").mkdir()
    (out_root / "plain_run" / "run_manifest.json").write_text(json.dumps({"name": "plain"}))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert (report.transects, report.videos, report.batches, report.passes, report.runs) == (
        1, 1, 1, 1, 1,
    )
    assert report.skipped == ["plain_run"]
    restored = fresh.get_transect(transect.id)
    assert restored.name == transect.name
    assert restored.start_lat == transect.start_lat
    restored_pass = fresh.get_pass(pass_.id)
    assert restored_pass.direction == "reverse"
    assert restored_pass.end_s == 60.0
    restored_run = fresh.get_run(run.id)
    assert restored_run.status == "succeeded"
    assert restored_run.run_dir_name == "t1__p01"
    assert restored_run.batch_id == batch.id
    assert [i.pass_id for i in fresh.list_batch_items(batch.id)] == [pass_.id]

    again = fresh.rebuild_from_scan(out_root)
    assert (again.transects, again.runs) == (0, 0)
    assert len(fresh.list_all_batch_items()) == 1


def test_store_reopens_existing_database(tmp_path):
    path = tmp_path / "survey.db"
    first = SurveyStore(path)
    first.add_transect(make_transect())
    first.close()
    second = SurveyStore(path)
    assert len(second.list_transects()) == 1


def test_worker_thread_writes_are_visible(store):
    _, _, pass_ = seed_pass(store)
    errors = []

    def worker():
        try:
            run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
            store.add_run(run)
            store.set_run_status(run.id, "running")
            store.set_run_status(run.id, "succeeded")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert not errors
    assert [r.status for r in store.runs_for_pass(pass_.id)] == ["succeeded"]


@pytest.mark.parametrize(
    "corruption, why",
    [
        ({"transect": {"id": "not-a-uuid"}}, "unparseable id"),
        ({"pass": {"direction": "sideways"}}, "invalid enum"),
        ({"pass": {"begin_s": "soon"}}, "wrong type"),
        ({"transect": {"start_lat": 999.0}}, "out-of-range coordinate"),
    ],
)
def test_rebuild_skips_a_corrupt_survey_block_without_losing_the_rest(
    store, tmp_path, corruption, why
):
    """Scenario: one run dir's manifest was hand-edited or half-written.

    Expected behaviour: that run is skipped and the others still restore. The
    module docstring calls the database rebuildable from disk, and the broad
    except in rebuild_from_scan is the whole of that promise.
    """
    transect, video, pass_ = seed_pass(store)
    good = RunRecord(pass_id=pass_.id, run_dir_name="good")
    bad = RunRecord(pass_id=pass_.id, run_dir_name="bad")
    store.add_run(good)
    store.add_run(bad)

    out_root = tmp_path / "out"
    block = survey_manifest_block(good, pass_, transect, None)
    write_manifest(out_root, "good", block)

    broken = json.loads(json.dumps(block))
    for section, patch in corruption.items():
        if patch is None:
            broken[section] = None
        else:
            broken[section].update(patch)
    write_manifest(out_root, "bad", broken)

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)

    assert "bad" in report.skipped, f"a manifest with an {why} was accepted"
    assert fresh.run_by_dir_name("good") is not None
    assert fresh.run_by_dir_name("bad") is None


def test_rebuild_skips_an_unreadable_manifest(store, tmp_path):
    out_root = tmp_path / "out"
    (out_root / "truncated").mkdir(parents=True)
    (out_root / "truncated" / "run_manifest.json").write_text('{"survey": {')

    report = SurveyStore(tmp_path / "rebuilt.db").rebuild_from_scan(out_root)
    assert report.skipped == ["truncated"]


def test_delete_pass_removes_it(store):
    _transect, _video, pass_ = seed_pass(store)
    assert len(store.list_passes()) == 1
    store.delete_pass(pass_.id)
    assert store.list_passes() == []
    assert store.get_pass(pass_.id) is None


def test_deleting_a_pass_takes_its_cart_rows_with_it(store):
    """A cart row is a plan to process the pass, so it goes when the pass does."""
    _transect, _video, pass_ = seed_pass(store)
    first, second = SurveyBatch(name="Day 1"), SurveyBatch(name="Day 2")
    for batch in (first, second):
        store.add_batch(batch)
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))

    store.delete_pass(pass_.id)

    assert store.list_all_batch_items() == []
    assert store.passes_in_batch(first.id) == []
    assert store.get_batch(second.id) is not None


def test_deleting_a_pass_with_a_run_says_why_it_cannot(store):
    """A run is history: it holds the pass, and the refusal reads as a sentence."""
    _transect, _video, pass_ = seed_pass(store)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))

    with pytest.raises(ValueError, match="recorded runs"):
        store.delete_pass(pass_.id)

    assert store.get_pass(pass_.id) is not None
    assert [i.pass_id for i in store.list_batch_items(batch.id)] == [pass_.id]


def test_deleting_a_video_takes_the_sections_cut_from_it(store):
    _transect, video, pass_ = seed_pass(store)

    assert store.delete_video(video.id) == 1

    assert store.get_video(video.id) is None
    assert store.get_pass(pass_.id) is None


def test_deleting_a_video_with_a_run_says_why_it_cannot(store):
    _transect, video, pass_ = seed_pass(store)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))

    with pytest.raises(ValueError, match="recorded runs"):
        store.delete_video(video.id)

    assert store.get_video(video.id) is not None
    assert store.get_pass(pass_.id) is not None


def test_deleting_one_chapter_leaves_the_rest_of_the_swim(store):
    """A pass spanning two clips is still a pass over the clip that remains."""
    transect, video, _ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    chaptered = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        extra_video_ids=[second.id],
        begin_s=0.0,
        end_s=600.0,
    )
    store.add_pass(chaptered)

    assert store.delete_video(video.id) == 1

    stored = store.get_pass(chaptered.id)
    assert stored.video_ids() == [second.id]


def test_a_carried_forward_cart_keeps_the_order_it_was_filled_in(tmp_path):
    """Scenario: a v0.2.0 survey.db, whose cart rows carry no processing order.

    Expected behaviour: each row's position backfills from the order the cart
    was filled in, which is the order the table showed before there was one, so
    an upgrade does not shuffle a day's work. The passes come back without a
    held column, which nothing records any more.
    """
    transect_id, video_id, batch_id = (uuid.uuid4() for _ in range(3))
    pass_ids = [uuid.uuid4() for _ in range(3)]
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO survey_batch VALUES (?,?,?,?)",
            (str(batch_id), "Day 1", "survey_preset", now),
        )
        for index, pass_id in enumerate(pass_ids):
            conn.execute(
                "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
                " begin_s, end_s, notes, created_at, extra_video_ids, held)"
                f" VALUES (?,?,?,?,'forward',{index},{index + 1},'',?,'[]',0)",
                (str(pass_id), str(transect_id), str(video_id), str(batch_id), now),
            )
    conn.close()

    store = SurveyStore(db_path)
    items = store.list_batch_items(batch_id)
    assert [i.pass_id for i in items] == pass_ids
    assert [i.position for i in items] == [0, 1, 2]
    assert all(i.overrides == {} for i in items)
    assert not hasattr(store.get_pass(pass_ids[0]), "held")


def test_a_carried_forward_survey_reaches_the_cart_cascade(tmp_path):
    """Scenario: a v0.2.0 survey.db, whose cart rows would restrict on pass delete.

    Expected behaviour: it opens at v7 with its cart membership intact, and
    deleting a pass that sits in a cart succeeds instead of failing on the
    foreign key.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    transect_id, video_id, batch_id, pass_id = (uuid.uuid4() for _ in range(4))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO survey_batch VALUES (?,?,?,?)",
            (str(batch_id), "Day 1", "survey_preset", now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,?,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), str(batch_id), now),
        )
    conn.close()

    store = SurveyStore(db_path)
    assert [i.pass_id for i in store.list_batch_items(batch_id)] == [pass_id]
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version()

    store.delete_pass(pass_id)
    assert store.list_all_batch_items() == []


def test_list_passes_combines_both_filters(store):
    """Filtering by transect and batch at once is an AND, not the last one set."""
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    transect_a, video, pass_a = seed_pass(store)
    transect_b = make_transect("T2")
    store.add_transect(transect_b)
    pass_b = TransectPass(
        transect_id=transect_b.id, video_id=video.id, begin_s=0.0, end_s=60.0,
        batch_id=batch.id,
    )
    store.add_pass(pass_b)

    assert len(store.list_passes(transect_id=transect_b.id)) == 1
    assert len(store.list_passes(batch_id=batch.id)) == 1
    # T1 has no pass in this batch, so the AND is empty.
    assert store.list_passes(transect_id=transect_a.id, batch_id=batch.id) == []
    assert len(store.list_passes(transect_id=transect_b.id, batch_id=batch.id)) == 1


def test_batches_are_readable_after_being_added(store):
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    assert store.get_batch(batch.id) == batch
    assert [b.name for b in store.list_batches()] == ["Day 1"]
    assert store.get_batch(uuid.uuid4()) is None


def test_pass_chapters_round_trip(store):
    transect, video, _ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    chaptered = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        extra_video_ids=[second.id],
        begin_s=0.0,
        end_s=600.0,
    )
    store.add_pass(chaptered)
    assert store.get_pass(chaptered.id).video_ids() == [video.id, second.id]

    chaptered.extra_video_ids = []
    store.update_pass(chaptered)
    assert store.get_pass(chaptered.id).extra_video_ids == []


def _normalised_ddl(sql):
    return re.sub(r"\s*([,()])\s*", r"\1", " ".join((sql or "").replace('"', "").split()))


def database_shape(db_path):
    """Every object in a database, in a form two builds can be compared in.

    Identifier quoting is stripped and space around punctuation collapsed: a
    table SQLite rebuilt and renamed carries quotes the same table created
    outright does not, and ADD COLUMN splices its column in before the closing
    paren rather than reflowing the statement.
    """
    conn = sqlite3.connect(db_path)
    try:
        objects = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        shape = {
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
            "objects": [(kind, name, _normalised_ddl(sql)) for kind, name, sql in objects],
        }
        for kind, name, _sql in objects:
            if kind != "table":
                continue
            shape[f"columns:{name}"] = conn.execute(f"PRAGMA table_info({name})").fetchall()
            shape[f"keys:{name}"] = conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()
    finally:
        conn.close()
    return shape


def test_a_database_older_than_v0_2_0_is_refused(tmp_path):
    """Scenario: a survey.db predating the oldest version this build carries forward.

    Expected behaviour: it refuses to open and names the version that can bring
    the database forward, rather than running a script written for a shape it
    does not have.
    """
    db_path = tmp_path / "ancient.db"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("PRAGMA user_version = 1")
    conn.close()

    with pytest.raises(RuntimeError, match="0.2.0"):
        SurveyStore(db_path)


def test_a_carried_forward_survey_matches_a_fresh_one(tmp_path):
    """Scenario: one machine upgraded from v0.2.0, another was installed today.

    Expected behaviour: the two schemas are identical. A difference between them
    only ever shows up on whichever kind of machine the developer does not have.
    """
    carried = write_v0_2_0_database(tmp_path / "carried.db")
    SurveyStore(carried).close()
    fresh = tmp_path / "fresh.db"
    SurveyStore(fresh).close()

    assert database_shape(carried) == database_shape(fresh)


def test_a_database_written_before_optional_transects_migrates(tmp_path):
    """Scenario: a survey.db from the build where every pass needed a transect.

    Expected behaviour: it opens, its passes keep the transects they had, their
    runs survive the table rebuild, and a new pass may now name none. The rebuild
    is the only way SQLite can relax NOT NULL, so it is worth proving against a
    database written the old way rather than one this build made.
    """
    transect_id, video_id, pass_id, run_id = (uuid.uuid4() for _ in range(4))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,NULL,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), now),
        )
        conn.execute(
            "INSERT INTO run_record VALUES (?,?,?,?,?,?,'',?)",
            (str(run_id), str(pass_id), "t1__p01", "succeeded", now, now, now),
        )
    conn.close()

    store = SurveyStore(db_path)
    kept = store.get_pass(pass_id)
    assert kept is not None
    assert kept.transect_id == transect_id
    assert kept.extra_video_ids == []
    assert store.get_run(run_id) is not None
    assert store.runs_for_transect(transect_id) != []

    unfiled = TransectPass(transect_id=None, video_id=video_id, begin_s=0.0, end_s=30.0)
    store.add_pass(unfiled)
    assert store.get_pass(unfiled.id).transect_id is None


def test_a_pass_that_names_no_transect_is_counted_against_none(store):
    """Scenario: a clip is cut without naming a transect, which the schema allows.

    Expected behaviour: it is tallied against no transect, and the null group is
    never read as a transect id.
    """
    transect, video, _assigned = seed_pass(store)
    unfiled = TransectPass(transect_id=None, video_id=video.id, begin_s=0.0, end_s=30.0)
    store.add_pass(unfiled)
    store.add_run(RunRecord(pass_id=unfiled.id, run_dir_name="unfiled"))

    counts = store.transect_usage_counts()
    assert set(counts) == {transect.id}
    assert counts[transect.id] == (1, 0)


def test_rebuild_restores_a_pass_that_named_no_transect(store, tmp_path):
    """Scenario: a clip was processed with the transect skipped.

    Expected behaviour: its manifest carries a null transect block, and a rebuild
    restores the pass unassigned rather than skipping the run or inventing a
    transect to hang it on.
    """
    _transect, _video, pass_ = seed_pass(store)
    pass_.transect_id = None
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="unfiled")
    store.add_run(run)

    out_root = tmp_path / "out"
    write_manifest(out_root, "unfiled", survey_manifest_block(run, pass_, None, None))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert report.skipped == []
    assert report.passes == 1
    restored = fresh.get_pass(pass_.id)
    assert restored is not None
    assert restored.transect_id is None


def test_current_cart_is_the_newest_batch_only_while_it_has_run_nothing(store):
    assert store.current_cart() is None

    _, _, pass_ = seed_pass(store)
    first = SurveyBatch(name="Day 1")
    store.add_batch(first)
    assert store.current_cart() == first

    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=first.id))
    assert store.batch_run_count(first.id) == 1
    assert store.current_cart() is None

    second = SurveyBatch(name="Day 2")
    store.add_batch(second)
    assert store.current_cart() == second


def test_an_older_empty_batch_behind_a_started_one_is_not_the_cart(store):
    _, _, pass_ = seed_pass(store)
    abandoned = SurveyBatch(name="Day 1")
    store.add_batch(abandoned)
    started = SurveyBatch(name="Day 2")
    store.add_batch(started)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=started.id))
    assert store.current_cart() is None


def test_a_pass_can_be_a_member_of_two_batches_but_of_one_only_once(store):
    _, _, pass_ = seed_pass(store)
    first = SurveyBatch(name="Day 1")
    second = SurveyBatch(name="Day 2")
    store.add_batch(first)
    store.add_batch(second)

    store.add_batch_item(BatchItem(batch_id=first.id, pass_id=pass_.id))
    store.add_batch_item(BatchItem(batch_id=first.id, pass_id=pass_.id))
    store.add_batch_item(BatchItem(batch_id=second.id, pass_id=pass_.id))
    assert len(store.list_batch_items(first.id)) == 1
    assert [p.id for p in store.passes_in_batch(second.id)] == [pass_.id]

    store.remove_batch_item(first.id, pass_.id)
    assert store.list_batch_items(first.id) == []
    assert len(store.list_batch_items(second.id)) == 1


def test_passes_in_batch_keeps_the_order_the_cart_was_filled_in(store):
    transect, video, first = seed_pass(store)
    later = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=70.0, end_s=120.0
    )
    earlier = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=130.0, end_s=180.0
    )
    store.add_pass(later)
    store.add_pass(earlier)
    batch = SurveyBatch(name="Day 1")
    store.add_batch(batch)
    for pass_ in (earlier, first, later):
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    assert [p.id for p in store.passes_in_batch(batch.id)] == [
        earlier.id, first.id, later.id
    ]


def test_carrying_a_survey_forward_gives_every_run_its_session(tmp_path):
    """Scenario: a v0.2.0 survey.db, where the session lived only on the pass.

    Expected behaviour: each run's batch_id is backfilled from its pass, and a
    pass that had a session gains a batch_item row whose id from_row can parse
    back.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    transect_id, video_id, batch_id, pass_id, run_id = (uuid.uuid4() for _ in range(5))
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO transect VALUES (?,?,'',?,?,?,?,?,?,?,?)",
            (str(transect_id), "T1", -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
        conn.execute(
            "INSERT INTO survey_batch VALUES (?,?,?,?)",
            (str(batch_id), "Day 1", "survey_preset", now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, held)"
            " VALUES (?,?,?,?,'forward',0,90,'',?,'[]',0)",
            (str(pass_id), str(transect_id), str(video_id), str(batch_id), now),
        )
        conn.execute(
            "INSERT INTO run_record VALUES (?,?,?,?,?,?,'',?)",
            (str(run_id), str(pass_id), "t1__p01", "succeeded", now, now, now),
        )
    conn.close()

    store = SurveyStore(db_path)
    assert store.get_run(run_id).batch_id == batch_id
    items = store.list_batch_items(batch_id)
    assert [i.pass_id for i in items] == [pass_id]
    assert isinstance(items[0].id, uuid.UUID)
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version()


def test_carrying_a_survey_forward_leaves_every_clip_unprobed(tmp_path):
    """Scenario: a v0.2.0 survey.db, from before clips carried container metadata.

    Expected behaviour: the clip keeps everything it already knew, and the two
    tri-states read 'unknown' rather than 'no', since nothing has looked at the
    file yet.
    """
    from deepreefmap_gui.survey.store import latest_schema_version

    video_id = uuid.uuid4()
    now = "2026-08-01T00:00:00+00:00"
    db_path = write_v0_2_0_database(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO video_asset VALUES (?,?,?,?,?,?,?,?,?)",
            (str(video_id), "GX010001.MP4", "/data/GX010001.MP4", "ab" * 16, 1024, now, 90.0,
             30.0, now),
        )
    conn.close()

    restored = SurveyStore(db_path).get_video(video_id)
    assert restored is not None
    assert restored.hash == "ab" * 16
    assert restored.duration_s == 90.0
    assert restored.created_at == now
    assert restored.captured_at is None
    assert restored.probed_at is None
    assert (restored.gravity, restored.gps) == (UNKNOWN, UNKNOWN)
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == latest_schema_version()


def test_rebuild_repeats_cleanly_and_keeps_what_a_probe_learned(store, tmp_path):
    """Scenario: a manifest records the clip's path, hash and size, nothing else.

    Expected behaviour: rebuilding a second time adds no rows, and a clip that
    has since been probed keeps its capture time and probe stamp rather than
    being flattened back to what the manifest knows.
    """
    out_root = tmp_path / "out"
    seed_survey_run(store, out_root, "t1__p01")

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    first = fresh.rebuild_from_scan(out_root)
    assert (first.videos, first.passes, first.runs, first.skipped) == (1, 1, 1, [])

    probed = fresh.list_videos()[0]
    probed.captured_at = "2026-06-01T09:15:00+00:00"
    probed.probed_at = "2026-08-01T00:00:00+00:00"
    probed.gravity = YES
    fresh.update_video(probed)

    second = fresh.rebuild_from_scan(out_root)
    assert (second.videos, second.passes, second.runs, second.skipped) == (0, 0, 0, [])
    assert len(fresh.list_videos()) == 1
    kept = fresh.get_video(probed.id)
    assert kept.captured_at == "2026-06-01T09:15:00+00:00"
    assert kept.probed_at == "2026-08-01T00:00:00+00:00"
    assert kept.gravity == YES


def test_rebuild_restores_every_chapter_of_a_pass(store, tmp_path):
    """A pass that spanned GoPro chapters comes back naming all of them."""
    transect, video, pass_ = seed_pass(store)
    second = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    pass_.extra_video_ids = [second.id]
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    out_root = tmp_path / "out"
    run_dir = out_root / "t1__p01"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "input_videos": [video.path, second.path],
        "video_hashes": [video.hash, second.hash],
        "survey": survey_manifest_block(run, pass_, transect, None),
    }))

    fresh = SurveyStore(tmp_path / "rebuilt.db")
    report = fresh.rebuild_from_scan(out_root)
    assert report.videos == 2
    restored = fresh.get_pass(pass_.id)
    assert [fresh.get_video(v).file_name for v in restored.video_ids()] == [
        "GX010001.MP4", "GX020001.MP4"
    ]


def test_deleting_a_session_takes_its_records_and_leaves_the_shared(store):
    """The session's cart rows and run records go; footage and transects stay."""
    batch = make_batch(store)
    transect, video, pass_ = seed_pass(store, batch=batch)
    store.add_run(
        RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=batch.id)
    )
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p02"))
    assert {r.run_dir_name for r in store.runs_in_batch(batch.id)} == {
        "t1__p01",
        "t1__p02",
    }

    store.delete_batch(batch.id)

    assert store.get_batch(batch.id) is None
    assert store.list_all_batch_items() == []
    assert store.list_runs() == []
    surviving = store.get_pass(pass_.id)
    assert surviving is not None
    assert surviving.batch_id is None
    assert store.get_transect(transect.id) is not None
    assert store.get_video(video.id) is not None


def test_deleting_a_session_leaves_other_sessions_runs(store):
    first, second = make_batch(store, "Day 1"), make_batch(store, "Day 2")
    _t, _v, pass_ = seed_pass(store, batch=first)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="one", batch_id=first.id))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="two", batch_id=second.id))

    store.delete_batch(second.id)

    assert [r.run_dir_name for r in store.list_runs()] == ["one"]
    assert store.get_batch(first.id) is not None


# --- Tombstones ---


def test_a_deleted_pass_is_gone_from_every_list(store):
    """A soft delete has to read as a delete: the row stays for the registry, and
    nothing in the app may still see it."""
    batch = make_batch(store)
    transect, video, pass_ = seed_pass(store, batch=batch)

    store.delete_pass(pass_.id)

    assert store.get_pass(pass_.id) is None
    assert store.list_passes() == []
    assert store.list_passes(transect_id=transect.id) == []
    assert store.list_passes(video_id=video.id) == []
    assert store.passes_in_batch(batch.id) == []
    assert store.list_all_batch_items() == []
    assert store.pass_with_window(video.id, 0.0, 60.0) is None
    assert store.transect_usage_counts() == {}
    assert store.holds_id("passes", pass_.id)


def test_a_deleted_clip_is_gone_from_every_lookup(store):
    _transect, video, _pass = seed_pass(store)

    assert store.delete_video(video.id) == 1

    assert store.get_video(video.id) is None
    assert store.list_videos() == []
    assert store.find_video_by_hash(video.hash) is None
    assert store.find_video_by_path(video.path) is None


def test_a_deleted_run_is_gone_from_every_run_list(store):
    batch = make_batch(store)
    transect, _video, pass_ = seed_pass(store, batch=batch)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", batch_id=batch.id)
    store.add_run(run)
    store.set_run_status(run.id, "succeeded")

    store.delete_run(run.id)

    assert store.get_run(run.id) is None
    assert store.list_runs() == []
    assert store.runs_for_pass(pass_.id) == []
    assert store.runs_for_transect(transect.id) == []
    assert store.runs_in_batch(batch.id) == []
    assert store.run_by_dir_name("t1__p01") is None
    assert store.succeeded_pass_ids() == set()
    assert store.batch_run_count(batch.id) == 0
    assert store.transect_usage_counts() == {transect.id: (1, 0)}


def test_a_deleted_transects_name_is_free_again(store):
    site = Site(name="Reef")
    store.add_site(site)
    first = make_transect(site_id=site.id)
    store.add_transect(first)

    store.delete_transect(first.id)

    assert store.list_transects() == []
    store.add_transect(make_transect(site_id=site.id))
    assert len(store.list_transects()) == 1


def test_a_deleted_clips_hash_is_free_again(store):
    """Re-adding the file makes a new clip rather than reviving the tombstone: the
    row the registry was told about is not what the operator asked back."""
    first = store.upsert_video(make_video())
    store.delete_video(first.id)

    second = store.upsert_video(make_video())

    assert second.id != first.id
    assert [v.id for v in store.list_videos()] == [second.id]


def test_re_adding_a_deleted_clip_brings_back_none_of_its_sections(store):
    """Scenario: a clip is deleted, and the same file is added again.

    Expected behaviour: the clip comes back under a fresh id with nothing cut
    from it. Adding the file is the undo a deleted clip has, and it undoes the
    clip alone, which is why a bulk sweep takes only clips with no sections.
    """
    _transect, video, pass_ = seed_pass(store)
    store.delete_video(video.id)

    again = store.upsert_video(make_video())

    assert again.id != video.id
    assert store.list_passes(video_id=again.id) == []
    assert store.get_pass(pass_.id) is None


def test_the_delete_guards_still_refuse_in_the_same_words(store):
    transect, video, pass_ = seed_pass(store)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))

    with pytest.raises(ValueError, match="This pass has recorded runs and cannot be removed."):
        store.delete_pass(pass_.id)
    with pytest.raises(ValueError, match="This clip has recorded runs and cannot be removed."):
        store.delete_video(video.id)
    with pytest.raises(
        ValueError, match="This transect has passes recorded against it and cannot be deleted."
    ):
        store.delete_transect(transect.id)

    assert store.get_pass(pass_.id) is not None
    assert store.get_video(video.id) is not None
    assert store.get_transect(transect.id) is not None


def test_a_tombstoned_pass_no_longer_holds_its_transect(store):
    transect, _video, pass_ = seed_pass(store)
    store.delete_pass(pass_.id)

    store.delete_transect(transect.id)

    assert store.get_transect(transect.id) is None


def test_a_merged_away_duplicate_does_not_answer_for_the_keeper(store):
    """Scenario: two rows for one clip are merged, then the file is scanned again.

    Expected behaviour: the scan lands on the keeper. The loser is a tombstone
    that still carries the hash, so every read of the hash has to skip it.
    """
    keeper = store.upsert_video(make_video())
    # Written straight in: the deduplicating upsert will not make a duplicate,
    # so the state under test has to be built.
    loser = make_video()
    store._add("video_asset", loser)

    assert store.merge_videos(keeper.id, [loser.id]) == 0

    assert store.find_video_by_hash(VIDEO_HASH).id == keeper.id
    assert store.upsert_video(make_video()).id == keeper.id
    assert [v.id for v in store.list_videos()] == [keeper.id]


def test_a_rescan_does_not_undo_a_deleted_run(store, tmp_path):
    """The manifest outlives a metadata-only delete, and the tombstone is what
    stops the next rebuild from arguing with it."""
    transect, _pass, run = seed_survey_run(store, tmp_path / "out", "t1__p01")
    store.delete_run(run.id)

    report = store.rebuild_from_scan(tmp_path / "out")

    assert (report.runs, report.passes, report.transects) == (0, 0, 0)
    assert store.list_runs() == []


# --- Sync ---


def test_changed_since_carries_tombstones_and_takes_either_name(store):
    """A delete only travels as a row, so the push document has to include one.

    The watermark here is the stamp the pass had a moment before it was deleted,
    which is the same second: an exclusive comparison would lose the delete.
    """
    transect, _video, pass_ = seed_pass(store)
    watermark = store.get_pass(pass_.id).updated_at
    store.delete_pass(pass_.id)

    changed = store.changed_since("passes", watermark)

    assert [p.id for p in changed] == [pass_.id]
    assert changed[0].deleted_at is not None
    assert store.changed_since("transect_pass", watermark) == changed
    assert [t.id for t in store.changed_since("transects")] == [transect.id]
    assert store.changed_since("transects", "2099-01-01T00:00:00+00:00") == []


def test_changed_since_refuses_a_section_that_does_not_sync(store):
    with pytest.raises(KeyError):
        store.changed_since("batches")


def test_apply_from_server_inserts_what_this_device_has_never_seen(store):
    pulled = {
        "id": str(uuid.uuid4()),
        "name": "Japanese Garden",
        "country": "Djibouti",
        "region": None,
        "description": "",
        "latitude": 11.6,
        "longitude": 43.1,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "deleted_at": None,
        "device_id": None,
        "server_seq": 4102,
    }

    result = store.apply_from_server("sites", [pulled])

    assert (result.received, result.inserted, result.applied) == (1, 1, 1)
    stored = store.get_site(uuid.UUID(pulled["id"]))
    assert stored.name == "Japanese Garden"


def test_apply_from_server_keeps_the_newer_of_the_two_copies(store):
    site = Site(name="Reef", updated_at="2026-08-10T00:00:00+00:00")
    store.add_site(site)

    stale = {"id": str(site.id), "name": "Stale", "updated_at": "2026-08-01T00:00:00+00:00"}
    same = {"id": str(site.id), "name": "Tied", "updated_at": site.updated_at}
    assert store.apply_from_server("sites", [stale, same]).skipped == [site.id, site.id]
    assert store.get_site(site.id).name == "Reef"

    fresh = {"id": str(site.id), "name": "Fresher", "updated_at": "2026-08-20T00:00:00+00:00"}
    result = store.apply_from_server("sites", [fresh])

    assert (result.updated, result.skipped) == (1, [])
    assert store.get_site(site.id).name == "Fresher"


def test_apply_from_server_lands_a_tombstone(store):
    site = Site(name="Reef", updated_at="2026-08-01T00:00:00+00:00")
    store.add_site(site)

    store.apply_from_server("sites", [{
        "id": str(site.id),
        "deleted_at": "2026-08-20T00:00:00+00:00",
        "updated_at": "2026-08-20T00:00:00+00:00",
    }])

    assert store.get_site(site.id) is None
    assert store.list_sites() == []
    assert store.changed_since("sites")[0].deleted_at == "2026-08-20T00:00:00+00:00"


def test_apply_from_server_leaves_what_only_this_device_knows(store):
    """The registry holds no path, so a pulled clip row carries none. Writing the
    whole row would blank the one thing that finds the file again."""
    video = store.upsert_video(make_video(updated_at="2026-08-10T00:00:00+00:00"))

    store.apply_from_server("videos", [{
        "id": str(video.id),
        "file_name": "renamed.MP4",
        "updated_at": "2026-08-20T00:00:00+00:00",
    }])

    stored = store.get_video(video.id)
    assert (stored.file_name, stored.path) == ("renamed.MP4", VIDEO_PATH)
    assert stored.hash == VIDEO_HASH


def test_apply_from_server_gives_an_unseen_clip_no_path_at_all(store):
    """A clip only the registry knows has no location on this device, and an empty
    path never matches another row."""
    pulled_id = uuid.uuid4()
    store.apply_from_server("videos", [{
        "id": str(pulled_id),
        "file_name": "GX090001.MP4",
        "hash": "ff" * 16,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }])

    assert store.get_video(pulled_id).path == ""
    assert store.find_video_by_path("") is None


def test_apply_from_server_skips_a_row_it_cannot_build_and_names_it(store):
    """Scenario: the registry sends a site with no name, which the model requires.

    Expected behaviour: the row is named and left out, the rest of the section
    lands, and the caller is free to move the cursor past the page.
    """
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()

    result = store.apply_from_server("sites", [
        {"id": str(bad_id), "description": "", "updated_at": "2026-08-01T00:00:00+00:00"},
        {"id": str(good_id), "name": "Reef", "updated_at": "2026-08-01T00:00:00+00:00"},
    ])

    assert result.received == 2
    assert result.inserted == 1
    assert [named for named, _why in result.unreadable] == [str(bad_id)]
    assert store.get_site(good_id).name == "Reef"
    assert store.get_site(bad_id) is None


def test_record_run_provenance_lands_on_the_row_and_keeps_the_empty_map(store):
    """Scenario: a run finishes and its manifest values are copied onto the row.

    Expected behaviour: the dict columns keep the difference between "nothing
    departed" (an empty map) and "nothing recorded" (None), because an audit
    reads those as two different facts.
    """
    _transect, _video, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    store.record_run_provenance(run.id, {
        "gui_version": "0.9.0",
        "library_version": "1.2.3",
        "preset_deviations": {},
        "model_revisions": None,
        "run_duration_s": 12.5,
        "not_a_column": "ignored",
    })

    stored = store.get_run(run.id)
    assert stored.gui_version == "0.9.0"
    assert stored.library_version == "1.2.3"
    assert stored.preset_deviations == {}
    assert stored.model_revisions is None
    assert stored.run_duration_s == 12.5
    assert stored.updated_at >= run.updated_at


def test_apply_from_server_skips_a_row_carrying_a_value_the_model_refuses(store):
    _transect, _video, pass_ = seed_pass(store)

    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = store.apply_from_server("passes", [{
        "id": str(pass_.id),
        "direction": "sideways",
        "updated_at": soon,
    }])

    assert result.applied == 0
    assert result.unreadable[0][0] == str(pass_.id)
    assert "sideways" in result.unreadable[0][1]
    assert store.get_pass(pass_.id).direction == "forward"


def test_apply_from_server_skips_a_row_whose_value_is_null(store):
    """JSON carries an explicit null where a model expects a string, and the
    model reaches for a string method on it."""
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()

    result = store.apply_from_server("sites", [
        {"id": str(bad_id), "name": None, "updated_at": "2026-07-01T10:00:00+00:00"},
        {"id": str(good_id), "name": "Reef", "updated_at": "2026-07-01T10:00:00+00:00"},
    ])

    assert result.inserted == 1
    assert [named for named, _why in result.unreadable] == [str(bad_id)]
    assert store.get_site(good_id).name == "Reef"


def test_apply_from_server_names_a_row_with_no_readable_id_by_where_it_sat(store):
    result = store.apply_from_server("sites", [{"name": "Nameless"}])

    assert result.unreadable[0][0] == "sites[0]"
    assert store.list_sites() == []


def test_dependency_closure_is_a_closed_set_in_foreign_key_order(store):
    """The registry refuses a child whose parent it has never seen, so a run
    travels with its pass, that pass's clips, its transect and that transect's
    site, whether or not any of them changed."""
    site, campaign = Site(name="Reef"), Campaign(name="2025_10_eritrea")
    store.add_site(site)
    store.add_campaign(campaign)
    transect, video, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    chapter = store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path="/data/GX020001.MP4")
    )
    pass_.campaign_id = campaign.id
    pass_.extra_video_ids = [chapter.id]
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)

    closure = store.dependency_closure("runs", [run.id])

    assert list(closure) == [s for s in SYNC_SECTIONS if s in closure]
    assert list(closure) == ["sites", "campaigns", "transects", "videos", "passes", "runs"]
    assert [s.id for s in closure["sites"]] == [site.id]
    assert [c.id for c in closure["campaigns"]] == [campaign.id]
    assert [t.id for t in closure["transects"]] == [transect.id]
    assert {v.id for v in closure["videos"]} == {video.id, chapter.id}
    assert [p.id for p in closure["passes"]] == [pass_.id]
    assert [r.id for r in closure["runs"]] == [run.id]


def test_dependency_closure_carries_a_tombstoned_parent(store):
    """A deleted clip is still the row the pass points at, so it has to be pushed
    with it or the registry refuses the pass."""
    _transect, video, pass_ = seed_pass(store, transect=None)
    conn = sqlite3.connect(store.path)
    with conn:
        conn.execute(
            "UPDATE video_asset SET deleted_at = ?, updated_at = ? WHERE id = ?",
            ("2026-08-20T00:00:00+00:00", "2026-08-20T00:00:00+00:00", str(video.id)),
        )
    conn.close()

    closure = store.dependency_closure("passes", [pass_.id])

    assert [v.id for v in closure["videos"]] == [video.id]
    assert closure["videos"][0].deleted_at is not None


def test_dependency_closure_skips_an_id_that_is_not_here(store):
    assert store.dependency_closure("runs", [uuid.uuid4()]) == {}


def test_sync_state_is_machine_local_and_stays_out_of_the_document(store, tmp_path):
    store.set_sync_state("server_url", "https://registry.example/api")
    store.set_sync_state("cursor", "4821")

    assert store.sync_state("cursor") == "4821"
    store.set_sync_state("cursor", "4830")
    assert store.sync_state("cursor") == "4830"
    store.set_sync_state("cursor", None)
    assert store.sync_state("cursor") is None
    assert store.sync_state("never_written") is None

    doc_path = tmp_path / "survey.json"
    store.export_json(doc_path)
    assert "sync_state" not in doc_path.read_text()
    assert "registry.example" not in doc_path.read_text()


def test_apply_from_server_ignores_a_column_the_model_no_longer_carries(store):
    """Scenario: a registry from before the created_by column was removed.

    Expected behaviour: the stray key is dropped and the row lands, because a
    schema difference in a column nobody reads must never strand a pull.
    """
    pulled = {
        "id": str(uuid.uuid4()),
        "name": "Reef",
        "description": "",
        "created_by": "auth0|abc",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }

    result = store.apply_from_server("sites", [pulled])

    assert (result.received, result.inserted) == (1, 1)
    assert store.get_site(uuid.UUID(pulled["id"])).name == "Reef"
