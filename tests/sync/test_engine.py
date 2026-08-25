"""The engine against a registry that never leaves the process."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from _factories import make_batch, make_transect, make_video, seed_pass, seed_survey_run
from deepreefmap.config.classes import load_classes

from deepreefmap_gui.notify.center import NotificationCenter
from deepreefmap_gui.survey.models import RunRecord, Site
from deepreefmap_gui.survey.models.notification import SURVEY, WARNING
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import wire
from deepreefmap_gui.sync.client import ConflictError, ServerUnreachableError
from deepreefmap_gui.sync.contract import PULL_SECTIONS, PUSH_SECTIONS
from deepreefmap_gui.sync.engine import (
    AUTHORED_SECTIONS,
    CONFLICT_DISCARDED,
    CONFLICT_OVERWRITTEN,
    CONFLICT_REFUSED,
    CONTRACT_SECTIONS_KEY,
    CURSOR_KEY,
    HELD_ATTEMPTS,
    HELD_GIVEN_UP,
    HELD_KEY,
    PASS_WITHOUT_VIDEOS,
    PULL_LIMIT,
    PULL_NAME_TAKEN,
    PULL_PARENT_SET_ASIDE,
    PULL_ROW_REFUSED,
    PULL_STALLED,
    PUSH_CONFLICTED,
    PUSH_UNACCOUNTED,
    RUN_PASS_DELETED,
    RUN_WITHOUT_PASS,
    SECTION_NOT_UNDERSTOOD,
    SECTION_REFUSED,
    SET_ASIDE_DISCARDED,
    TRANSECT_WITHOUT_SITE,
    UNREADABLE_ROW,
    WATERMARK_PREFIX,
    SyncEngine,
)

# Stamps either side of anything the store writes during a test. LATER is an
# hour ahead of now: newer than every local edit, and still inside the future
# tolerance the apply path allows a pulled stamp.
EARLIER = "2000-01-01T00:00:00+00:00"
LATER = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
# Newer again, for a second pull that has to beat the first one's rows.
MUCH_LATER = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

# The full section set, agreed explicitly. The vendored contract pulls only the
# ancestor sections and _asked_for drops what was never agreed, so the tests
# that exercise the pass, run and chapter machinery declare the wider set a
# future contract would.
WIDE_PULL = tuple(wire.WIRE_SECTIONS)


class FakeRegistry:
    """Answers like the registry and remembers everything it was told.

    ``skipped`` names rows it claims to hold a newer copy of, per section.
    ``refused`` does the same for rows it says another origin owns, and
    ``conflicted`` for rows its own database would not take at all.
    ``unaccounted`` names a section it answers about without saying what became of
    the rows, which is the shape of a half-answer the engine must not trust.
    A section devices do not author is answered like the real registry answers
    it: read as reference, every id refused, nothing written.
    """

    def __init__(
        self, pages=(), fail=None, skipped=None, refused=None, conflicted=None,
        unaccounted=(), cursor=4830,
    ):
        self._pages = list(pages)
        self._fail = fail
        self._skipped = skipped or {}
        self._refused = refused or {}
        self._conflicted = conflicted or {}
        self._unaccounted = set(unaccounted)
        self._cursor = cursor
        self.pulls: list[int | None] = []
        self.pushes: list[dict] = []
        self.calls: list[str] = []

    def pull(self, since=None, limit=PULL_LIMIT):
        self.calls.append("pull")
        self.pulls.append(since)
        if not self._pages:
            return page(since, {})
        answer = self._pages.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def push(self, sections):
        self.calls.append("push")
        self.pushes.append(dict(sections))
        if self._fail is not None:
            raise self._fail
        return {
            "cursor": self._cursor,
            "sections": {name: self._outcome(name, rows) for name, rows in sections.items()},
        }

    def _outcome(self, name, rows):
        if name in self._unaccounted:
            return {"received": len(rows), "applied": 0, "skipped": []}
        if name not in PUSH_SECTIONS:
            return {
                "received": len(rows),
                "applied": 0,
                "skipped": [],
                "refused": [str(row["id"]) for row in rows],
            }
        skipped = [str(row_id) for row_id in self._skipped.get(name, ())]
        refused = [str(row_id) for row_id in self._refused.get(name, ())]
        conflicted = [str(row_id) for row_id in self._conflicted.get(name, ())]
        return {
            "received": len(rows),
            "applied": len(rows) - len(skipped) - len(refused) - len(conflicted),
            "skipped": skipped,
            "refused": refused,
            "conflicted": conflicted,
        }


def page(cursor, sections, has_more=False, omitted=()):
    return {
        "contract_version": 1,
        "cursor": cursor,
        "has_more": has_more,
        "sections": sections,
        "omitted_sections": list(omitted),
    }


def sync_fields(**overrides):
    return {
        "created_at": EARLIER,
        "updated_at": LATER,
        "deleted_at": None,
        "device_id": None,
        "server_seq": 4102,
        **overrides,
    }


def site_row(site_id, name, **overrides):
    return {"id": str(site_id), "name": name, "description": "", **sync_fields(**overrides)}


def transect_row(transect_id, name="T1", **overrides):
    return {
        "id": str(transect_id),
        "site_id": None,
        "name": name,
        "description": "",
        "start_lat": -17.5,
        "start_lon": 177.1,
        "end_lat": -17.5005,
        "end_lon": 177.1005,
        "length_m": 50.0,
        **sync_fields(**overrides),
    }


def video_row(video_id, file_name="GX010001.MP4", **overrides):
    return {
        "id": str(video_id),
        "hash": None,
        "file_name": file_name,
        "gravity": "unknown",
        "gps": "unknown",
        **sync_fields(**overrides),
    }


def pass_row(pass_id, transect_id=None, **overrides):
    return {
        "id": str(pass_id),
        "transect_id": None if transect_id is None else str(transect_id),
        "campaign_id": None,
        "begin_s": 0.0,
        "end_s": 60.0,
        "direction": "forward",
        "upside_down": False,
        "label": "",
        "notes": "",
        "quality": None,
        **sync_fields(**overrides),
    }


def chapter_row(pass_id, video_id, ordinal, **overrides):
    return {
        "id": str(wire.pass_video_id(pass_id, video_id)),
        "pass_id": str(pass_id),
        "video_id": str(video_id),
        "ordinal": ordinal,
        **sync_fields(**overrides),
    }


def run_row(run_id, pass_id, run_dir_name="t1__p01", **overrides):
    return {
        "id": str(run_id),
        "pass_id": str(pass_id),
        "run_dir_name": run_dir_name,
        "status": "succeeded",
        "started_at": None,
        "finished_at": None,
        "error": "",
        **sync_fields(**overrides),
    }


# --- Push ---


def test_a_push_carries_the_whole_dependency_closure(store, tmp_path):
    """The registry refuses a child whose parent it has never seen, so the document
    is a closed set in foreign-key order."""
    site = Site(name="Reef")
    store.add_site(site)
    transect, video, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    chapter = store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path="/data/b.MP4"))
    pass_.extra_video_ids = [chapter.id]
    store.update_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01")
    store.add_run(run)
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).push()

    sent = registry.pushes[0]
    assert list(sent) == ["sites", "transects", "videos", "passes", "pass_videos", "runs"]
    assert [row["id"] for row in sent["sites"]] == [str(site.id)]
    assert [row["id"] for row in sent["transects"]] == [str(transect.id)]
    assert {row["id"] for row in sent["videos"]} == {str(video.id), str(chapter.id)}
    assert [(row["video_id"], row["ordinal"]) for row in sent["pass_videos"]] == [
        (str(video.id), 0), (str(chapter.id), 1),
    ]
    # The site is read as reference and refused, which is accounted, not applied.
    assert report.applied == report.sent - len(sent["sites"])
    assert report.refused == []


def test_a_push_records_a_watermark_per_section(store, tmp_path):
    _transect, _video, pass_ = seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path)

    report = engine.push()

    assert engine.watermark("passes") == store.get_pass(pass_.id).updated_at
    assert report.watermarks["passes"] == engine.watermark("passes")
    assert engine.watermark("runs") is None


def test_a_section_whose_rows_all_predate_its_watermark_sends_nothing(store, tmp_path):
    """A watermark is a push the registry accounted for, which is also what
    clears the marks the local edits left, so both are set up here."""
    seed_pass(store)
    for section in SYNC_SECTIONS:
        store.set_sync_state(f"{WATERMARK_PREFIX}{section}", LATER)
        store.clear_pending_push(section, store.pending_push_ids(section))
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).push()

    assert registry.pushes == []
    assert report.sections == {}


def test_a_refused_push_leaves_every_watermark_alone(store, tmp_path):
    seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(fail=ConflictError("unknown parent")), out_root=tmp_path)

    with pytest.raises(ConflictError):
        engine.push()

    assert [engine.watermark(section) for section in SYNC_SECTIONS] == [None] * len(SYNC_SECTIONS)


def test_a_half_answered_section_holds_its_watermark(store, tmp_path):
    seed_pass(store)
    notifications = NotificationCenter()
    engine = SyncEngine(
        store, FakeRegistry(unaccounted=("passes",)), out_root=tmp_path,
        notifications=notifications,
    )

    report = engine.push()

    assert engine.watermark("passes") is None
    assert "passes" not in report.watermarks
    assert engine.watermark("videos") is not None
    # Held is not the same as fine: a section stuck like this re-sends its rows
    # on every sync, and only the registry can end that, so the operator hears.
    assert report.unaccounted == ("passes",)
    assert PUSH_UNACCOUNTED in {note.fingerprint for note in notifications.active()}


def test_a_row_the_registry_already_held_newer_is_reported_not_raised(store, tmp_path):
    transect, _video, _pass = seed_pass(store)
    notifications = NotificationCenter()
    registry = FakeRegistry(skipped={"transects": [transect.id]})
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.push()

    assert report.skipped == [transect.id]
    note = notifications.active()[0]
    assert (note.fingerprint, note.severity, note.scope) == (CONFLICT_DISCARDED, WARNING, SURVEY)
    # Accounted for, so the section moves on: the pull that follows brings the
    # registry's copy down, and re-offering this row forever would say nothing new.
    assert engine.watermark("transects") is not None


def test_a_skip_that_was_not_a_local_edit_is_not_a_conflict(store, tmp_path):
    """Scenario: a survey in step, pushed again because one run was recorded.

    Expected behaviour: the ancestors the closure drags in and the derived
    chapter rows come back skipped, and none of that is reported. Warning about a
    survey where nothing is wrong would train the reader to ignore the warning.
    """
    site = Site(name="Reef")
    store.add_site(site)
    transect, video, pass_ = seed_pass(store, transect=make_transect(site_id=site.id))
    notifications = NotificationCenter()
    ancestors = {"sites": [site.id], "transects": [transect.id], "videos": [video.id],
                 "passes": [pass_.id], "pass_videos": [wire.pass_video_id(pass_.id, video.id)]}
    SyncEngine(store, FakeRegistry(), out_root=tmp_path, notifications=notifications).push()
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="t1__p01"))
    registry = FakeRegistry(skipped=ancestors)

    report = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications).push()

    sent = registry.pushes[0]
    assert set(sent) == {*ancestors, "runs"}, "the whole closure went with the run"
    assert report.applied == 1, "and the registry skipped every ancestor"
    assert report.skipped == []
    assert notifications.active() == []


def test_a_survey_nobody_has_touched_since_the_last_push_sends_nothing(store, tmp_path):
    """Scenario: sync pressed twice with nothing typed in between.

    Expected behaviour: the second push has no document to build. Re-offering the
    rows the first push earned its watermark with is what a laptop whose clock
    had drifted did on every sync, forever.
    """
    seed_pass(store)
    SyncEngine(store, FakeRegistry(), out_root=tmp_path).push()
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).push()

    assert registry.pushes == []
    assert report.sections == {}


def test_the_push_cursor_is_reported_and_never_adopted(store, tmp_path):
    """It is the registry's position after our own write, so pulling from it would
    skip every row another device wrote before it."""
    seed_pass(store)
    engine = SyncEngine(store, FakeRegistry(cursor=9999), out_root=tmp_path)

    assert engine.push().cursor == 9999
    assert engine.cursor() is None


def test_a_local_delete_travels_as_a_row(store, tmp_path):
    _transect, _video, pass_ = seed_pass(store)
    store.delete_pass(pass_.id)
    registry = FakeRegistry()

    SyncEngine(store, registry, out_root=tmp_path).push()

    sent = registry.pushes[0]
    assert [row["deleted_at"] for row in sent["passes"]] == [
        wire.to_wire_time(store.changed_since("passes")[0].deleted_at)
    ]
    assert sent["pass_videos"][0]["deleted_at"] is not None


def test_a_run_pushes_the_provenance_and_the_cover_from_its_directory(store, tmp_path):
    out_root = tmp_path / "out"
    classes_config = load_classes()
    _transect, _pass, run = seed_survey_run(
        store,
        out_root,
        "t1__p01",
        batch=make_batch(store),
        deepreefmap_version="1.2.3",
        segmentation_model="segformer-b2",
        mapping_backend="loger_star",
        enable_tsdf=False,
    )
    first = load_classes().classes[0]
    (out_root / "t1__p01" / "benthic_cover.json").write_text(json.dumps({
        "classes": {str(first.id): {"name": first.name, "count": 30.0, "fraction": 0.3}},
        "denominator": 100.0,
    }))
    registry = FakeRegistry()

    SyncEngine(
        store, registry, out_root=out_root, classes_config=classes_config
    ).push()

    sent = registry.pushes[0]
    assert list(sent) == [name for name in wire.WIRE_SECTIONS if name in sent]
    assert sent["runs"][0]["library_version"] == "1.2.3"
    assert sent["runs"][0]["segmentation_model"] == "segformer-b2"
    assert sent["runs"][0]["taxonomy_hash"]
    cover = sent["cover_rows"]
    assert {row["estimator"] for row in cover} == {wire.PER_PASS}
    assert {row["run_id"] for row in cover} == {str(run.id)}
    assert {row["metric_source"] for row in cover} == {"unprojected"}
    assert all(row["denominator"] == 100.0 for row in cover)


def test_cover_stays_behind_when_no_taxonomy_was_supplied(store, tmp_path):
    """Group names are taxonomy data, so a run cannot claim any without one."""
    out_root = tmp_path / "out"
    seed_survey_run(store, out_root, "t1__p01")
    registry = FakeRegistry()

    SyncEngine(store, registry, out_root=out_root).push()

    assert wire.COVER_ROWS not in registry.pushes[0]


# --- Pull ---


def test_a_pull_lands_rows_and_records_where_it_got_to(store, tmp_path):
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(4821, {"sites": [site_row(site_id, "Japanese Garden")]})])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert store.get_site(site_id).name == "Japanese Garden"
    assert (report.pages, report.cursor) == (1, 4821)
    assert store.sync_state(CURSOR_KEY) == "4821"
    assert engine.cursor() == 4821
    assert registry.pulls == [None]


def test_an_interrupted_pull_resumes_from_the_page_it_failed_on(store, tmp_path):
    first, second = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(100, {"sites": [site_row(first, "Reef")]}, has_more=True),
        ServerUnreachableError("offline"),
    ])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    with pytest.raises(ServerUnreachableError):
        engine.pull()

    assert engine.cursor() == 100
    assert store.get_site(first) is not None

    resumed = FakeRegistry(pages=[page(200, {"sites": [site_row(second, "Wall")]})])
    SyncEngine(store, resumed, out_root=tmp_path).pull()

    assert resumed.pulls == [100]
    assert engine.cursor() == 200
    assert {s.name for s in store.list_sites()} == {"Reef", "Wall"}


def test_a_multi_page_pull_keeps_asking_while_there_is_more(store, tmp_path):
    ids = [uuid.uuid4() for _ in range(3)]
    registry = FakeRegistry(pages=[
        page(10, {"sites": [site_row(ids[0], "One")]}, has_more=True),
        page(20, {"sites": [site_row(ids[1], "Two")]}, has_more=True),
        page(30, {"sites": [site_row(ids[2], "Three")]}),
    ])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert registry.pulls == [None, 10, 20]
    assert (report.pages, report.cursor) == (3, 30)
    assert report.sections["sites"].inserted == 3


def test_a_pull_stops_when_the_cursor_stops_moving(store, tmp_path):
    """A registry claiming more rows at a cursor it will not advance would loop
    forever."""
    registry = FakeRegistry(pages=[page(10, {}, has_more=True), page(10, {}, has_more=True)])
    notifications = NotificationCenter()

    report = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications
    ).pull()

    assert (report.pages, report.cursor) == (2, 10)
    # Breaking off is right, and it must not read as a completed sync: the
    # registry still holds rows this pull never saw.
    assert report.stalled and report.stopped
    assert PULL_STALLED in {note.fingerprint for note in notifications.active()}


def test_a_tombstone_from_the_registry_removes_the_row_here(store, tmp_path):
    site = Site(name="Reef", updated_at=EARLIER)
    store.add_site(site)
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site.id, "Reef", deleted_at=LATER)],
    })])

    SyncEngine(store, registry, out_root=tmp_path).pull()

    assert store.get_site(site.id) is None
    assert store.changed_since("sites")[0].deleted_at is not None


def test_last_write_wins_in_both_directions(store, tmp_path):
    site = Site(name="Reef", updated_at="2026-08-10T00:00:00+00:00")
    store.add_site(site)
    stale = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site.id, "Stale", updated_at="2026-08-01T00:00:00Z")],
    })])

    kept = SyncEngine(store, stale, out_root=tmp_path).pull()

    assert store.get_site(site.id).name == "Reef"
    assert kept.kept == (site.id,)

    fresh = FakeRegistry(pages=[page(11, {"sites": [site_row(site.id, "Fresher")]})])
    landed = SyncEngine(store, fresh, out_root=tmp_path).pull()

    assert store.get_site(site.id).name == "Fresher"
    assert landed.kept == ()


def test_an_overwritten_local_edit_raises_a_notification(store, tmp_path):
    """Scenario: a row edited here since the last push is edited again elsewhere.

    Expected behaviour: the registry's copy wins on the stamp, and the operator is
    told rather than left to notice their typing has gone.
    """
    transect = make_transect()
    store.add_transect(transect)
    notifications = NotificationCenter()
    store.set_sync_state(f"{WATERMARK_PREFIX}transects", EARLIER)
    transect.name = "Renamed here"
    store.update_transect(transect)
    registry = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(transect.id, "Somebody else's name")],
    })])

    report = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications
    ).pull()

    assert report.overwritten == (transect.id,)
    assert store.get_transect(transect.id).name == "Somebody else's name"
    assert CONFLICT_OVERWRITTEN in {note.fingerprint for note in notifications.active()}


def test_an_untouched_row_the_registry_updates_is_not_a_conflict(store, tmp_path):
    site = Site(name="Reef")
    store.add_site(site)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path)
    engine.push()
    registry = FakeRegistry(pages=[page(10, {"sites": [site_row(site.id, "Renamed")]})])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.overwritten == ()
    assert store.get_site(site.id).name == "Renamed"


# --- Pull: a pass and its chapters ---


def test_a_pass_arrives_with_its_chapters_and_lands_whole(store, tmp_path):
    transect_id, pass_id = uuid.uuid4(), uuid.uuid4()
    videos = [uuid.uuid4(), uuid.uuid4()]
    registry = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(transect_id)],
        "videos": [video_row(videos[0]), video_row(videos[1], "GX020001.MP4")],
        "passes": [pass_row(pass_id, transect_id)],
        "pass_videos": [chapter_row(pass_id, videos[1], 1), chapter_row(pass_id, videos[0], 0)],
    })])

    report = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    landed = store.get_pass(pass_id)
    assert landed.video_ids() == videos
    assert report.passes_without_videos == ()


def test_chapters_arriving_a_page_late_still_complete_the_pass(store, tmp_path):
    """Chapters sort after their pass, so the two split across a page boundary."""
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"videos": [video_row(video_id)], "passes": [pass_row(pass_id)]}, has_more=True),
        page(20, {"pass_videos": [chapter_row(pass_id, video_id, 0)]}),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_pass(pass_id).video_ids() == [video_id]
    assert report.passes_without_videos == ()
    assert report.cursor == 20


def test_a_chapter_list_for_a_pass_already_here_is_adopted(store, tmp_path):
    _transect, video, pass_ = seed_pass(store)
    chapter = store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path="/data/b.MP4"))
    registry = FakeRegistry(pages=[page(10, {
        "pass_videos": [
            chapter_row(pass_.id, chapter.id, 0),
            chapter_row(pass_.id, video.id, 1),
        ],
    })])

    SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_pass(pass_.id).video_ids() == [chapter.id, video.id]


def test_a_pass_the_registry_holds_with_no_footage_is_reported(store, tmp_path):
    """A pass has to name a video here, so one with no chapters cannot be recorded.
    It is left out and said out loud, rather than stalling every later page behind
    it."""
    pass_id = uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {"passes": [pass_row(pass_id)]})])
    engine = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications, pull_sections=WIDE_PULL
    )

    report = engine.pull()

    assert report.passes_without_videos == (pass_id,)
    assert store.get_pass(pass_id) is None
    assert engine.cursor() == 10
    assert PASS_WITHOUT_VIDEOS in {note.fingerprint for note in notifications.active()}


def test_a_run_whose_pass_arrived_deleted_is_dropped_and_named(store, tmp_path):
    """Scenario: a pass deleted in the registry, which does not delete its runs
    with it, arriving at a device that never held either.

    Expected behaviour: the pass is dropped rather than waited on, since nothing
    is coming for it and there is nothing here to remove. Its runs cannot be
    recorded against anything, so they are dropped too and said out loud.
    """
    pass_id, run_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "passes": [pass_row(pass_id, deleted_at=LATER)],
        "runs": [run_row(run_id, pass_id)],
    })])
    engine = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications, pull_sections=WIDE_PULL
    )

    report = engine.pull()

    assert report.runs_pass_deleted == (run_id,)
    assert (report.passes_without_videos, report.runs_without_passes) == ((), ())
    assert store.get_run(run_id) is None
    assert engine.cursor() == 10
    assert store.sync_state(HELD_KEY) is None
    fingerprints = {note.fingerprint for note in notifications.active()}
    assert RUN_PASS_DELETED in fingerprints
    assert PASS_WITHOUT_VIDEOS not in fingerprints


def test_a_held_run_lands_once_its_pass_does(store, tmp_path):
    transect, video, pass_ = seed_pass(store)
    later_pass, run_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"runs": [run_row(run_id, later_pass)]}, has_more=True),
        page(20, {
            "passes": [pass_row(later_pass, transect.id)],
            "pass_videos": [chapter_row(later_pass, video.id, 0)],
        }),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert report.runs_without_passes == ()
    assert store.get_run(run_id).pass_id == later_pass


# --- Pull: rows held across syncs ---


def test_a_held_pass_lands_on_the_sync_its_chapters_arrive_on(store, tmp_path):
    """A pass is held in the database, not in the pull that found it: the cursor
    has already stepped past it and nothing would bring it down again."""
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    first = FakeRegistry(pages=[page(10, {"passes": [pass_row(pass_id)]})])

    held = SyncEngine(store, first, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert held.passes_without_videos == (pass_id,)
    assert store.sync_state(HELD_KEY) is not None

    second = FakeRegistry(pages=[page(20, {
        "videos": [video_row(video_id)],
        "pass_videos": [chapter_row(pass_id, video_id, 0)],
    })])
    landed = SyncEngine(store, second, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_pass(pass_id).video_ids() == [video_id]
    assert landed.passes_without_videos == ()
    assert store.sync_state(HELD_KEY) is None


def test_a_held_run_survives_an_interrupted_pull(store, tmp_path):
    transect, video, _pass = seed_pass(store)
    later_pass, run_id = uuid.uuid4(), uuid.uuid4()
    interrupted = FakeRegistry(pages=[
        page(10, {"runs": [run_row(run_id, later_pass)]}, has_more=True),
        ServerUnreachableError("offline"),
    ])

    with pytest.raises(ServerUnreachableError):
        SyncEngine(store, interrupted, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    resumed = FakeRegistry(pages=[page(20, {
        "passes": [pass_row(later_pass, transect.id)],
        "pass_videos": [chapter_row(later_pass, video.id, 0)],
    })])
    report = SyncEngine(store, resumed, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_run(run_id).pass_id == later_pass
    assert report.runs_without_passes == ()


def test_chapters_arriving_before_their_pass_are_kept(store, tmp_path):
    """A pass edited after its chapters sorts behind them, so the join rows come
    first and have nothing to fold onto yet."""
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"pass_videos": [chapter_row(pass_id, video_id, 0)]}, has_more=True),
        page(20, {"videos": [video_row(video_id)], "passes": [pass_row(pass_id)]}),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_pass(pass_id).video_ids() == [video_id]
    assert report.passes_without_videos == ()


def test_a_dependency_that_never_arrives_is_given_up_on(store, tmp_path):
    """Scenario: a run whose pass the registry has never sent, over ten syncs that
    each reached the end of what the registry had.

    Expected behaviour: it is retried until the count runs out, then dropped and
    said out loud, since a row held forever is a row nobody will ever look at.
    """
    pass_id, run_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    first = FakeRegistry(pages=[page(10, {"runs": [run_row(run_id, pass_id)]})])
    report = SyncEngine(
        store, first, out_root=tmp_path, notifications=notifications, pull_sections=WIDE_PULL
    ).pull()

    for _ in range(HELD_ATTEMPTS - 2):
        assert report.runs_without_passes == (run_id,)
        report = SyncEngine(
            store, FakeRegistry(), out_root=tmp_path, notifications=notifications
        ).pull()

    final = SyncEngine(
        store, FakeRegistry(), out_root=tmp_path, notifications=notifications
    ).pull()

    assert final.given_up == (run_id,)
    assert final.runs_without_passes == ()
    assert store.sync_state(HELD_KEY) is None
    assert HELD_GIVEN_UP in {note.fingerprint for note in notifications.active()}


def test_an_unfinished_pull_does_not_count_against_a_held_row(store, tmp_path):
    """Only a pull that reached the end knows the registry is holding nothing for
    the row, so a page that stopped early cannot spend one of its tries."""
    pass_id, run_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {"runs": [run_row(run_id, pass_id)]}, has_more=True),
                                   ServerUnreachableError("offline")])

    with pytest.raises(ServerUnreachableError):
        SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert json.loads(store.sync_state(HELD_KEY))["runs"][str(run_id)]["attempts"] == 0


def test_a_held_pass_and_its_held_run_are_both_reported(store, tmp_path):
    """A run waiting on a pass that is itself waiting is the commonest shape of
    this, so it is the one the reader most needs both halves of."""
    pass_id, run_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "passes": [pass_row(pass_id)],
        "runs": [run_row(run_id, pass_id)],
    })])

    report = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications, pull_sections=WIDE_PULL
    ).pull()

    assert (report.passes_without_videos, report.runs_without_passes) == ((pass_id,), (run_id,))
    fingerprints = {note.fingerprint for note in notifications.active()}
    assert {PASS_WITHOUT_VIDEOS, RUN_WITHOUT_PASS} <= fingerprints


def test_the_held_rows_and_the_cursor_land_together(store, tmp_path, monkeypatch):
    """One transaction: a crash between the held rows and the cursor cannot leave
    one written without the other, so nothing is ever stepped over unremembered."""
    original = store.set_sync_state

    def explode_after_cursor(key, value):
        original(key, value)
        if key == CURSOR_KEY and value is not None:
            raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(store, "set_sync_state", explode_after_cursor)
    registry = FakeRegistry(pages=[page(10, {"passes": [pass_row(uuid.uuid4())]})])

    with pytest.raises(sqlite3.OperationalError):
        SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.sync_state(HELD_KEY) is None
    assert store.sync_state(CURSOR_KEY) is None


# --- Pull: a section this build cannot read ---


def test_an_unknown_section_stops_the_pull_where_it_stands(store, tmp_path):
    """Scenario: a registry newer than this app, sending a section it has no
    reading of.

    Expected behaviour: what the page carried that it does understand lands, and
    the cursor stays put. The cursor is a high-water mark over one sequence shared
    by every table, so advancing it would step over the unread rows for good.
    """
    site_id = uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Japanese Garden")],
        "quadrats": [{"id": str(uuid.uuid4())}],
    }, has_more=True)])
    engine = SyncEngine(
        store,
        registry,
        out_root=tmp_path,
        notifications=notifications,
        pull_sections=(*WIDE_PULL, "quadrats"),
    )

    report = engine.pull()

    assert store.get_site(site_id).name == "Japanese Garden"
    assert report.unknown_sections == ("quadrats",)
    assert (report.pages, engine.cursor()) == (1, None)
    assert SECTION_NOT_UNDERSTOOD in {note.fingerprint for note in notifications.active()}

    again = FakeRegistry(pages=[page(10, {"sites": [site_row(site_id, "Japanese Garden")]})])
    SyncEngine(store, again, out_root=tmp_path).pull()

    assert again.pulls == [None], "the same page is offered again"


def test_the_derived_sections_are_not_a_section_this_build_cannot_read(store, tmp_path):
    """cover_rows has nowhere to land here and pass_videos is read on its own, so
    neither is a section the app is missing."""
    _transect, video, pass_ = seed_pass(store)
    registry = FakeRegistry(pages=[page(10, {
        "pass_videos": [chapter_row(pass_.id, video.id, 0)],
        "cover_rows": [{"id": str(uuid.uuid4()), "run_id": str(uuid.uuid4())}],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL)

    report = engine.pull()

    assert report.unknown_sections == ()
    assert engine.cursor() == 10


def test_a_section_the_pull_did_not_ask_for_is_dropped(store, tmp_path):
    """Scenario: the registry answers a pull with a section it is never asked to serve.

    Expected behaviour: it is dropped rather than written. The registry serves sites,
    campaigns and transects, and a run reaching this database off a pull is either a
    registry that has changed its mind or one that is not the registry at all.
    """
    site_id = uuid.uuid4()
    video_id = uuid.uuid4()
    # A video has no parent, so nothing but the filter stops it landing.
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Harat")],
        "videos": [video_row(video_id)],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, pull_sections=("sites",))

    report = engine.pull()

    assert report.sections["sites"].applied == 1
    assert "videos" not in report.sections
    assert store.get_video(video_id) is None
    # Not "unknown": this build reads videos perfectly well, it just did not ask.
    assert report.unknown_sections == ()
    assert engine.cursor() == 10


def test_a_build_that_negotiated_nothing_still_lands_the_contract_sections(store, tmp_path):
    """An engine with no agreed set falls back to the vendored contract's pull
    sections, so the ancestors still land."""
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {"sites": [site_row(site_id, "Harat")]})])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert report.sections["sites"].applied == 1


def test_a_section_the_registry_withheld_is_reported_without_stopping_the_pull(
    store, tmp_path
):
    """Scenario: the registry holds a kind of record this build never declared.

    Expected behaviour: the pull runs to the end and the cursor advances. The rows
    are not for this version, so there is nothing to come back for until the app
    is updated, which is when the cursor resets and re-pulls anyway.
    """
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[
        page(10, {"sites": [site_row(site_id, "Japanese Garden")]}, omitted=["moorings"]),
    ])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert report.omitted_sections == ("moorings",)
    assert report.unknown_sections == ()
    assert engine.cursor() == 10


def test_the_same_withheld_section_across_pages_is_named_once(store, tmp_path):
    registry = FakeRegistry(pages=[
        page(10, {}, has_more=True, omitted=["moorings"]),
        page(20, {}, omitted=["moorings", "quadrats"]),
    ])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.omitted_sections == ("moorings", "quadrats")


def test_a_registry_that_withholds_nothing_reports_nothing(store, tmp_path):
    registry = FakeRegistry(pages=[page(10, {})])

    assert SyncEngine(store, registry, out_root=tmp_path).pull().omitted_sections == ()


# --- Pull: a row this build cannot read ---


def test_a_malformed_row_is_left_out_and_the_rest_of_the_page_lands(store, tmp_path):
    """Scenario: a site with no name, which the model requires, ahead of a transect
    on the same page.

    Expected behaviour: the row is named and the page moves on. Failing here would
    fail the same page on every sync, and everything behind it would never land.
    """
    bad_id, transect_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [{"id": str(bad_id), "description": "", **sync_fields()}],
        "transects": [transect_row(transect_id)],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert [named for named, _why in report.unreadable] == [str(bad_id)]
    assert store.get_transect(transect_id) is not None
    assert store.get_site(bad_id) is None
    assert engine.cursor() == 10
    assert UNREADABLE_ROW in {note.fingerprint for note in notifications.active()}


def test_a_row_carrying_a_value_the_model_refuses_is_skipped(store, tmp_path):
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "videos": [video_row(video_id)],
        "passes": [pass_row(pass_id, direction="sideways")],
        "pass_videos": [chapter_row(pass_id, video_id, 0)],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL)

    report = engine.pull()

    assert [named for named, _why in report.unreadable] == [str(pass_id)]
    assert store.get_pass(pass_id) is None
    assert store.get_video(video_id) is not None
    assert engine.cursor() == 10


# --- Pull: the section set the cursor was reached under ---


def test_the_cursor_resets_when_the_agreed_sections_widen(store, tmp_path):
    """A section this device did not ask for has been stepped over by a cursor that
    counts every table, so the only way back to those rows is from zero."""
    store.set_sync_state(CURSOR_KEY, "500")
    store.set_sync_state(CONTRACT_SECTIONS_KEY, "passes,sites")
    registry = FakeRegistry(pages=[page(600, {})])
    engine = SyncEngine(
        store, registry, out_root=tmp_path, pull_sections=("sites", "passes", "runs")
    )

    engine.pull()

    assert registry.pulls == [None]
    assert engine.stored_sections() == ("passes", "runs", "sites")


def test_a_narrower_or_equal_section_set_leaves_the_cursor_alone(store, tmp_path):
    store.set_sync_state(CURSOR_KEY, "500")
    store.set_sync_state(CONTRACT_SECTIONS_KEY, "passes,sites")
    same = FakeRegistry(pages=[page(600, {})])
    SyncEngine(store, same, out_root=tmp_path, pull_sections=("sites", "passes")).pull()

    assert same.pulls == [500]

    narrower = FakeRegistry(pages=[page(700, {})])
    SyncEngine(store, narrower, out_root=tmp_path, pull_sections=("sites",)).pull()

    assert narrower.pulls == [600]
    assert store.sync_state(CONTRACT_SECTIONS_KEY) == "passes,sites"


def test_a_build_with_nothing_negotiating_for_it_never_resets(store, tmp_path):
    store.set_sync_state(CURSOR_KEY, "500")
    registry = FakeRegistry(pages=[page(600, {})])

    SyncEngine(store, registry, out_root=tmp_path).pull()

    assert registry.pulls == [500]


# --- Pull: a page is one transaction ---


def test_a_page_that_fails_part_way_lands_nothing_and_converges(store, tmp_path):
    """Scenario: the sections ahead of runs land, then runs raises.

    Expected behaviour: nothing of the page stays and the cursor is not written,
    so the registry offers the whole page again and it lands whole. A survey a
    failed sync has touched would otherwise hold half a page nobody asked for.
    """
    transect_id, pass_id, video_id, run_id = (uuid.uuid4() for _ in range(4))
    sections = {
        "transects": [transect_row(transect_id)],
        "videos": [video_row(video_id)],
        "passes": [pass_row(pass_id, transect_id)],
        "pass_videos": [chapter_row(pass_id, video_id, 0)],
        "runs": [run_row(run_id, pass_id)],
    }
    landing = store.apply_from_server

    def fail_on_runs(name, rows):
        if name == "runs":
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        return landing(name, rows)

    broken = SyncEngine(
        store,
        FakeRegistry(pages=[page(10, sections)]),
        out_root=tmp_path,
        pull_sections=WIDE_PULL,
    )
    broken._store = _Wrapped(store, fail_on_runs)

    with pytest.raises(sqlite3.IntegrityError):
        broken.pull()

    assert store.get_pass(pass_id) is None
    assert store.get_transect(transect_id) is None
    assert store.get_run(run_id) is None
    assert store.sync_state(CURSOR_KEY) is None

    registry = FakeRegistry(pages=[page(10, sections)])
    report = SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert registry.pulls == [None], "the same page again, whole"
    assert store.get_run(run_id).pass_id == pass_id
    assert len(store.list_transects()) == 1
    assert store.get_pass(pass_id).video_ids() == [video_id]
    assert report.kept == (), "nothing had landed, so nothing is an equal-stamp skip"
    assert report.sections["runs"].inserted == 1
    assert store.sync_state(CURSOR_KEY) == "10"


class _Wrapped:
    """The store with one section's landing replaced."""

    def __init__(self, store, apply_from_server):
        self._store = store
        self.apply_from_server = apply_from_server

    def __getattr__(self, name):
        return getattr(self._store, name)


# --- Both halves ---


def test_sync_pulls_before_it_pushes(store, tmp_path):
    """A conflict is then discovered before the local edit is offered, and the copy
    being pushed is as fresh as it can be."""
    seed_pass(store)
    registry = FakeRegistry()

    report = SyncEngine(store, registry, out_root=tmp_path).sync()

    assert registry.calls == ["pull", "push"]
    assert report.push.sent > 0
    assert report.pull.pages == 1


def test_a_round_trip_leaves_both_sides_holding_the_same_survey(store, tmp_path):
    """Scenario: a laptop pulls a site and a transect, files a pass and a run
    against them, and pushes the lot back."""
    site_id, transect_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Japanese Garden")],
        "transects": [transect_row(transect_id, site_id=str(site_id))],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path)
    engine.pull()

    video = store.upsert_video(make_video())
    _transect, _video, pass_ = seed_pass(store, transect=store.get_transect(transect_id), video=video)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", status="succeeded")
    store.add_run(run)

    report = engine.push()

    sent = registry.pushes[0]
    assert [row["id"] for row in sent["sites"]] == [str(site_id)]
    assert [row["id"] for row in sent["transects"]] == [str(transect_id)]
    assert [row["id"] for row in sent["runs"]] == [str(run.id)]
    assert report.skipped == []
    assert engine.cursor() == 10


# --- Sections this device does not author ---


def test_a_lone_ancestor_edit_pushes_nothing_and_owes_nothing(store, tmp_path):
    """A site with no changed descendants is not a document: ancestors only travel
    with the rows that need them, and never earn a watermark of their own."""
    store.add_site(Site(name="Reef"))
    registry = FakeRegistry()
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.push()

    assert registry.pushes == []
    assert report.sections == {}
    assert engine.watermark("sites") is None
    assert "sites" not in AUTHORED_SECTIONS


def test_a_refused_local_edit_advances_the_watermark_and_is_reported(store, tmp_path):
    """Scenario: a transect another device recorded, edited here.

    Expected behaviour: the refusal is terminal, so the watermark moves rather
    than re-offering the row on every sync forever, and the operator is told
    where the edit can actually be made.
    """
    transect, _video, _pass = seed_pass(store)
    notifications = NotificationCenter()
    registry = FakeRegistry(refused={"transects": [transect.id]})
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.push()

    assert report.refused == [transect.id]
    assert engine.watermark("transects") is not None
    fingerprints = [note.fingerprint for note in notifications.active()]
    assert fingerprints.count(CONFLICT_REFUSED) == 1


def test_a_pulled_section_this_device_authors_is_refused_whole(store, tmp_path):
    """Scenario: a registry answering a pull with a section devices author.

    Expected behaviour: nothing of it is written, whatever it carries. The rows
    made on this laptop have exactly one writer, and it is this laptop.
    """
    video_id = uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {"videos": [video_row(video_id)]})])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.refused_sections == ("videos",)
    assert store.get_video(video_id) is None
    assert engine.cursor() == 10
    assert SECTION_REFUSED in {note.fingerprint for note in notifications.active()}


# --- Pull: stamps that would break last-write-wins ---


def test_a_garbage_stamp_is_quarantined_not_landed(store, tmp_path):
    """A stamp compared as a string would let lexical garbage win every
    comparison forever, so the row is unreadable rather than a winner."""
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Reef", updated_at="zzz-not-a-time")],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert store.get_site(site_id) is None
    assert len(report.unreadable) == 1
    assert engine.cursor() == 10


def test_a_far_future_stamp_is_quarantined(store, tmp_path):
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    site_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Reef", updated_at=far)],
    })])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert store.get_site(site_id) is None
    assert [why for _named, why in report.unreadable] == [f"updated_at {far!r} is in the future"]


def test_adopting_a_chapter_order_does_not_mark_the_pass_pending(store, tmp_path):
    """The adopted order is the registry's data, so the pass keeps its stamp and
    the next push has nothing new to say about it."""
    _transect, video, pass_ = seed_pass(store)
    chapter = store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path="/data/b.MP4"))
    before = store.get_pass(pass_.id).updated_at
    registry = FakeRegistry(pages=[page(10, {
        "pass_videos": [
            chapter_row(pass_.id, chapter.id, 0),
            chapter_row(pass_.id, video.id, 1),
        ],
    })])

    SyncEngine(store, registry, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    landed = store.get_pass(pass_.id)
    assert landed.video_ids() == [chapter.id, video.id]
    assert landed.updated_at == before


def test_a_published_preset_lands_in_its_own_table(store, tmp_path):
    """Presets are the one section the registry authors for devices to read."""
    preset_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "presets": [{
            "id": str(preset_id),
            "name": "Deep reef",
            "version": 2,
            "settings": {"fps": 4, "mapping_name": "loger_star"},
            "description": "Slower descent sites",
            **sync_fields(),
        }],
    })])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.sections["presets"].inserted == 1
    landed = store.list_server_presets()
    assert [(p.name, p.version) for p in landed] == [("Deep reef", 2)]
    assert landed[0].settings == {"fps": 4, "mapping_name": "loger_star"}
    assert store.get_server_preset("deep REEF", 2) is not None


# --- Pull: a name a live row here already holds ---


def one_note(notifications, fingerprint):
    """The single active notification carrying this fingerprint."""
    found = [note for note in notifications.active() if note.fingerprint == fingerprint]
    assert len(found) == 1, f"{fingerprint} was posted {len(found)} time(s)"
    return found[0]


def test_a_pulled_transect_whose_name_is_taken_is_set_aside(store, tmp_path):
    """Scenario: a curator renames one of a site's lines to a name a line on this
    laptop already carries, which is what swapping two names looks like halfway.

    Expected behaviour: the colliding row is set aside whole and everything else
    on the page lands. The alternative is what shipped: the unique index takes
    the page down, the cursor never moves, and every sync from then on replays
    the same page and fails on it identically.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs, other = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "transects": [
            transect_row(theirs, "Reef Wall", site_id=str(site.id)),
            transect_row(other, "Sand Chute", site_id=str(site.id)),
        ],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert store.get_transect(theirs) is None
    assert store.get_transect(ours.id).name == "Reef Wall", "nothing here changed"
    assert store.get_transect(other) is not None, "the rest of the page landed"
    assert engine.cursor() == 10, "and the laptop is not stuck on this page"
    assert [(c.section, c.row_id, c.name) for c in report.collided] == [
        ("transects", theirs, "Reef Wall")
    ]
    assert "Reef Wall" in one_note(notifications, PULL_NAME_TAKEN).body


def test_a_row_set_aside_is_still_there_after_a_restart(store, tmp_path):
    """The cursor has stepped past it for good, so this copy is the only one
    left, and the diver is rarely watching at the moment it happens."""
    site = Site(name="Reef")
    store.add_site(site)
    store.add_transect(make_transect("Reef Wall", site_id=site.id))
    theirs = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
    })])
    SyncEngine(store, registry, out_root=tmp_path).pull()

    kept = SyncEngine(store, FakeRegistry(), out_root=tmp_path).quarantined()

    assert [(entry["section"], entry["id"], entry["name"]) for entry in kept] == [
        ("transects", str(theirs), "Reef Wall")
    ]
    assert kept[0]["row"]["length_m"] == 50.0, "the registry's row is kept whole"


def test_the_same_collision_twice_is_one_row_set_aside(store, tmp_path):
    """A registry that edits the colliding line again sends it again, and a
    record growing a line per sync would be unreadable by the season's end."""
    site = Site(name="Reef")
    store.add_site(site)
    store.add_transect(make_transect("Reef Wall", site_id=site.id))
    row = transect_row(uuid.uuid4(), "Reef Wall", site_id=str(site.id))
    for cursor in (10, 20):
        SyncEngine(
            store,
            FakeRegistry(pages=[page(cursor, {"transects": [row]})]),
            out_root=tmp_path,
        ).pull()

    assert len(SyncEngine(store, FakeRegistry(), out_root=tmp_path).quarantined()) == 1


def test_a_page_that_frees_the_name_it_collided_on_sets_nothing_aside(store, tmp_path):
    """Scenario: one page renames B onto the name A carries and then renames A
    away, in that order, which is what a curator tidying two lines looks like.

    Expected behaviour: both land, nothing is set aside, and no notification is
    posted. Judging B where it sat in the page had the laptop keep the old pair
    for good while telling the diver to rename something no record carried.
    """
    site = Site(name="Reef")
    store.add_site(site)
    first = make_transect("T1", site_id=site.id)
    second = make_transect("T2", site_id=site.id)
    store.add_transect(first)
    store.add_transect(second)
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "transects": [
            transect_row(second.id, "T1", site_id=str(site.id)),
            transect_row(first.id, "T3", site_id=str(site.id)),
        ],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.collided == ()
    assert (store.get_transect(second.id).name, store.get_transect(first.id).name) == ("T1", "T3")
    assert engine.quarantined() == []
    assert PULL_NAME_TAKEN not in {note.fingerprint for note in notifications.active()}


def test_two_lines_whose_names_the_registry_swapped_both_land(store, tmp_path):
    """A curator swapping two names is ordinary work and has to converge. Each
    row holds the name the other wants, so no single retry unpicks it, and
    setting both aside left the laptop mirror-imaged against the registry."""
    site = Site(name="Reef")
    store.add_site(site)
    first = make_transect("T1", site_id=site.id)
    second = make_transect("T2", site_id=site.id)
    store.add_transect(first)
    store.add_transect(second)
    registry = FakeRegistry(pages=[page(10, {
        "transects": [
            transect_row(first.id, "T2", site_id=str(site.id)),
            transect_row(second.id, "T1", site_id=str(site.id)),
        ],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path)

    report = engine.pull()

    assert report.collided == ()
    assert (store.get_transect(first.id).name, store.get_transect(second.id).name) == ("T2", "T1")
    assert engine.quarantined() == []


def test_a_row_set_aside_lands_on_the_sync_after_the_name_is_freed(store, tmp_path):
    """The obstacle is a name, and what frees it happens in the web console
    where this device cannot watch, so the entry is offered again after every
    pull rather than left to sit for the rest of the season."""
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs = uuid.uuid4()
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
        })]),
        out_root=tmp_path,
    ).pull()

    freeing = FakeRegistry(pages=[page(20, {
        "transects": [
            transect_row(ours.id, "Sand Chute", site_id=str(site.id), updated_at=MUCH_LATER),
        ],
    })])
    engine = SyncEngine(store, freeing, out_root=tmp_path)
    report = engine.pull()

    assert report.set_aside_cleared == (theirs,)
    assert store.get_transect(theirs).name == "Reef Wall"
    assert engine.quarantined() == []


def test_a_row_the_database_refuses_for_its_own_reasons_is_said_differently(store, tmp_path):
    """Scenario: a registry sends a site carrying no created_at, which this
    database's NOT NULL refuses just as flatly as a duplicate name.

    Expected behaviour: it is set aside like any other, and reported as what it
    is. Telling the diver a record here carries the same name would send them
    looking for a record that does not exist, over a name nothing is using.
    """
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(uuid.uuid4(), "Reef", created_at=None)],
    })])
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert [row.holder for row in report.collided] == [None]
    assert engine.cursor() == 10
    posted = {note.fingerprint for note in notifications.active()}
    assert PULL_ROW_REFUSED in posted
    assert PULL_NAME_TAKEN not in posted


def test_renaming_the_line_here_also_lets_a_set_aside_row_land(store, tmp_path):
    """The other half of the same fix. A diver who reads the notification and
    renames the line on this laptop has freed the name just as surely as a
    curator would have, and a sync that carried nothing new must still notice.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs = uuid.uuid4()
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
        })]),
        out_root=tmp_path,
    ).pull()

    ours.name = "Reef Wall north"
    store.update_transect(ours)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path)
    report = engine.pull()

    assert report.set_aside_cleared == (theirs,)
    assert store.get_transect(theirs).name == "Reef Wall"
    assert engine.quarantined() == []


# --- Pull: a set-aside row the copy here has outlived ---


def test_a_set_aside_row_this_laptop_has_outlived_is_discarded_not_landed(store, tmp_path):
    """Scenario: the registry's rename of a line was set aside on a name clash,
    and the line here has been edited since, so last-write-wins throws the
    registry's copy away rather than recording it.

    Expected behaviour: it is reported as discarded. Counting it among the rows
    that finally landed told the diver the registry's version was now in the
    survey at the moment it was being deleted.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    store.add_transect(make_transect("Sand Chute", site_id=site.id))
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(ours.id, "Sand Chute", site_id=str(site.id))],
        })]),
        out_root=tmp_path,
    ).pull()
    assert len(SyncEngine(store, FakeRegistry(), out_root=tmp_path).quarantined()) == 1

    # A watermark ahead of the clock is how an edit made now comes to outrank an
    # hour-old pull: _stamp refuses to mint a stamp under one.
    store.set_sync_state(f"{WATERMARK_PREFIX}transects", LATER)
    ours.name = "Reef Wall north"
    store.update_transect(ours)
    notifications = NotificationCenter()
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.set_aside_discarded == (ours.id,)
    assert report.set_aside_cleared == ()
    assert store.get_transect(ours.id).name == "Reef Wall north", "the edit here stands"
    assert engine.quarantined() == [], "and the registry's copy is not kept for ever"
    assert SET_ASIDE_DISCARDED in {note.fingerprint for note in notifications.active()}


def test_a_row_the_store_could_not_read_is_no_proof_the_registry_s_copy_landed(
    store, tmp_path
):
    """Scenario: the same discard, except the registry also sends that line again
    in a shape no model can be built from, so it goes nowhere.

    Expected behaviour: still reported as discarded. What separates a landing
    from a discard is the ids this pull actually put in, and a row the store
    could not read is not one of them. Counting it made an unreadable arrival
    stand as proof that the copy being deleted had gone in instead.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    store.add_transect(make_transect("Sand Chute", site_id=site.id))
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(ours.id, "Sand Chute", site_id=str(site.id))],
        })]),
        out_root=tmp_path,
    ).pull()

    store.set_sync_state(f"{WATERMARK_PREFIX}transects", LATER)
    ours.name = "Reef Wall north"
    store.update_transect(ours)
    # A missing key would be refilled from the stored row by the merge, so the
    # unreadable shape has to be a value no model can take.
    unreadable = transect_row(ours.id, "Reef Wall west", site_id=str(site.id))
    unreadable["length_m"] = "wide"
    engine = SyncEngine(
        store,
        FakeRegistry(pages=[page(20, {"transects": [unreadable]})]),
        out_root=tmp_path,
    )

    report = engine.pull()

    assert [named for named, _why in report.unreadable] == [str(ours.id)]
    assert report.set_aside_discarded == (ours.id,)
    assert report.set_aside_cleared == ()
    assert store.get_transect(ours.id).name == "Reef Wall north", "the edit here stands"


def test_a_set_aside_row_the_registry_itself_replaced_is_cleared_not_discarded(store, tmp_path):
    """The other way a set-aside entry stops being applicable: the registry sent
    the same line again, newer, and that copy landed. Its data is in the survey,
    so the stale entry is spent rather than thrown away, and nobody needs
    warning about a laptop that discarded nothing."""
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    store.add_transect(make_transect("Sand Chute", site_id=site.id))
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(ours.id, "Sand Chute", site_id=str(site.id))],
        })]),
        out_root=tmp_path,
    ).pull()

    engine = SyncEngine(store, FakeRegistry(pages=[page(20, {
        "transects": [
            transect_row(ours.id, "Reef Wall west", site_id=str(site.id), updated_at=MUCH_LATER),
        ],
    })]), out_root=tmp_path)
    report = engine.pull()

    assert report.set_aside_cleared == (ours.id,)
    assert report.set_aside_discarded == ()
    assert store.get_transect(ours.id).name == "Reef Wall west"
    assert engine.quarantined() == []


def test_a_set_aside_site_this_laptop_has_outlived_is_discarded_too(store, tmp_path):
    """Scenario: the same thing one section up. The registry's rename of a site
    was set aside on a name clash, and the site here has been edited since.

    Expected behaviour: it is reported as discarded, exactly as a line is. The
    split was read off the pending-push marks, and those cover only the four
    sections this device authors, so on a site -- one of the three the contract
    pulls and never marks -- a copy being thrown away reported itself as one
    that had finally landed. Nothing calls update_site today, which is what made
    this worth closing rather than leaving for whoever writes the first caller.
    """
    ours = Site(name="Reef")
    store.add_site(ours)
    store.add_site(Site(name="Lagoon"))
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {"sites": [site_row(ours.id, "Lagoon")]})]),
        out_root=tmp_path,
    ).pull()
    assert len(SyncEngine(store, FakeRegistry(), out_root=tmp_path).quarantined()) == 1

    store.set_sync_state(f"{WATERMARK_PREFIX}transects", LATER)
    ours.name = "Reef North"
    store.update_site(ours)
    notifications = NotificationCenter()
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path, notifications=notifications)

    report = engine.pull()

    assert report.set_aside_discarded == (ours.id,)
    assert report.set_aside_cleared == ()
    assert store.get_site(ours.id).name == "Reef North", "the edit here stands"
    assert engine.quarantined() == []
    assert SET_ASIDE_DISCARDED in {note.fingerprint for note in notifications.active()}
    assert store.pending_push_ids("sites") == set(), "and no mark was what proved it"


def test_a_set_aside_site_the_registry_itself_replaced_is_cleared_not_discarded(
    store, tmp_path
):
    """The other half, on the same unmarked section: the registry sent that site
    again, newer, and that copy landed. Its data is in the survey, so the stale
    entry is spent rather than thrown away. Without this the split would have
    inverted the other way and warned about a laptop that discarded nothing."""
    ours = Site(name="Reef")
    store.add_site(ours)
    store.add_site(Site(name="Lagoon"))
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {"sites": [site_row(ours.id, "Lagoon")]})]),
        out_root=tmp_path,
    ).pull()

    engine = SyncEngine(store, FakeRegistry(pages=[page(20, {
        "sites": [site_row(ours.id, "Reef West", updated_at=MUCH_LATER)],
    })]), out_root=tmp_path)
    report = engine.pull()

    assert report.set_aside_cleared == (ours.id,)
    assert report.set_aside_discarded == ()
    assert store.get_site(ours.id).name == "Reef West"
    assert engine.quarantined() == []


# --- Pull: a child of a row that could not be taken ---


def test_a_pass_naming_a_set_aside_transect_waits_instead_of_stalling(store, tmp_path):
    """Scenario: a page carries a transect whose name a line here already holds,
    and a pass swum on that transect.

    Expected behaviour: the transect is set aside, the pass waits with it, and
    the cursor still moves. Letting the pass go in behind a parent that never
    landed fails the foreign key, rolls the page back whole, and restores the
    permanent deadlock the setting-aside exists to end: no cursor, no
    quarantine, and no notification either.
    """
    site = Site(name="Reef")
    store.add_site(site)
    store.add_transect(make_transect("Reef Wall", site_id=site.id))
    theirs, pass_id, video_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    registry = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
        "videos": [video_row(video_id)],
        "passes": [pass_row(pass_id, theirs)],
        "pass_videos": [chapter_row(pass_id, video_id, 0)],
    })])
    engine = SyncEngine(
        store, registry, out_root=tmp_path, notifications=notifications, pull_sections=WIDE_PULL
    )

    report = engine.pull()

    assert engine.cursor() == 10
    assert report.behind_set_aside == (pass_id,)
    assert store.get_pass(pass_id) is None
    assert [entry["id"] for entry in engine.quarantined()] == [str(theirs)]
    assert PULL_PARENT_SET_ASIDE in {note.fingerprint for note in notifications.active()}
    assert report.passes_without_videos == (), "the footage arrived; the parent did not"


def test_a_result_whose_section_is_waiting_waits_with_it(store, tmp_path):
    """Scenario: the same page again, with the result of processing that pass on
    it as well.

    Expected behaviour: all three wait together and the cursor still moves. Only
    the pass was held, so the result went in against a section that was not
    there, failed the foreign key and took the page down: the same deadlock, one
    generation further from the row that caused it.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs, pass_id, video_id, run_id = (uuid.uuid4() for _ in range(4))
    engine = SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
            "videos": [video_row(video_id)],
            "passes": [pass_row(pass_id, theirs)],
            "pass_videos": [chapter_row(pass_id, video_id, 0)],
            "runs": [run_row(run_id, pass_id)],
        })]),
        out_root=tmp_path,
        pull_sections=WIDE_PULL,
    )

    report = engine.pull()

    assert engine.cursor() == 10
    assert set(report.behind_set_aside) == {pass_id, run_id}
    assert report.runs_without_passes == (), "its section arrived; its transect did not"
    assert not store.holds_id("runs", run_id)

    ours.name = "Sand Chute"
    store.update_transect(ours)
    SyncEngine(store, FakeRegistry(), out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.holds_id("runs", run_id), "and all three come in on the sync that frees it"
    assert store.sync_state(HELD_KEY) is None


def test_a_pass_lands_once_the_name_blocking_its_transect_is_freed(store, tmp_path):
    """The pass waits like any other orphan, so the page that frees the name has
    to bring both in without the registry sending either of them again."""
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs, pass_id, video_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "transects": [transect_row(theirs, "Reef Wall", site_id=str(site.id))],
            "videos": [video_row(video_id)],
            "passes": [pass_row(pass_id, theirs)],
            "pass_videos": [chapter_row(pass_id, video_id, 0)],
        })]),
        out_root=tmp_path,
        pull_sections=WIDE_PULL,
    ).pull()

    freeing = FakeRegistry(pages=[page(20, {
        "transects": [
            transect_row(ours.id, "Sand Chute", site_id=str(site.id), updated_at=MUCH_LATER),
        ],
    })])
    report = SyncEngine(store, freeing, out_root=tmp_path, pull_sections=WIDE_PULL).pull()

    assert store.get_transect(theirs) is not None
    assert store.get_pass(pass_id) is not None
    assert report.behind_set_aside == ()
    assert store.sync_state(HELD_KEY) is None


def test_a_line_behind_a_set_aside_site_outlives_the_holding_pen(store, tmp_path):
    """Scenario: a site arrives carrying a name a site here already holds, so it
    is set aside, and a line naming that site comes down with it. Nobody frees
    the name for a fortnight of syncs.

    Expected behaviour: the line waits for exactly as long as its site is kept,
    and what the diver is told stays true throughout. Re-deriving the reason on
    each page read the site as one the registry had never sent, so the pen gave
    the line up at HELD_ATTEMPTS while the record kept its site for ever: the
    cursor had stepped past that line on the first sync, and there was no copy
    of it left anywhere but the pen it had just been dropped from.
    """
    ours = Site(name="Reef")
    store.add_site(ours)
    theirs, line = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    report = SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "sites": [site_row(theirs, "Reef")],
            "transects": [transect_row(line, "Wall", site_id=str(theirs))],
        })]),
        out_root=tmp_path,
        notifications=notifications,
        pull_sections=PULL_SECTIONS,
    ).pull()

    for _ in range(HELD_ATTEMPTS + 1):
        assert report.behind_set_aside == (line,)
        assert report.transects_without_sites == (), "its site did arrive"
        assert report.given_up == ()
        report = SyncEngine(
            store,
            FakeRegistry(),
            out_root=tmp_path,
            notifications=notifications,
            pull_sections=PULL_SECTIONS,
        ).pull()

    posted = {note.fingerprint for note in notifications.active()}
    assert PULL_PARENT_SET_ASIDE in posted
    assert TRANSECT_WITHOUT_SITE not in posted, "the site is here, in the set-aside record"
    assert HELD_GIVEN_UP not in posted, "and nothing that is still being kept was given up"

    ours.name = "Reef South"
    store.update_site(ours)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path, pull_sections=PULL_SECTIONS)
    freed = engine.pull()

    assert freed.set_aside_cleared == (theirs,)
    assert store.get_transect(line).site_id == theirs, "the line came in with its site"
    assert engine.quarantined() == []
    assert store.sync_state(HELD_KEY) is None


def test_a_line_behind_a_set_aside_site_is_never_dropped_for_space(
    store, tmp_path, monkeypatch
):
    """Scenario: the same set-aside site, with more lines naming it than the
    holding pen has room for.

    Expected behaviour: not one of them is dropped while their site is still on
    record, and the two notices agree with each other. The pen and the record
    are bounded by different numbers, so the pen filled first and gave lines up
    for space while the record kept the site they were waiting on for ever. The
    cursor stepped past those lines on the first sync, so the pen held the only
    copy of them, and the pull said "neither is given up on" in one notice and
    named the rows it had given up in the next.
    """
    monkeypatch.setattr("deepreefmap_gui.sync.engine.HELD_MAX", 3)
    ours = Site(name="Reef")
    store.add_site(ours)
    theirs = uuid.uuid4()
    lines = [uuid.uuid4() for _ in range(5)]
    notifications = NotificationCenter()
    report = SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "sites": [site_row(theirs, "Reef")],
            "transects": [
                transect_row(line, f"Wall {number}", site_id=str(theirs))
                for number, line in enumerate(lines)
            ],
        })]),
        out_root=tmp_path,
        notifications=notifications,
        pull_sections=PULL_SECTIONS,
    ).pull()

    assert set(report.behind_set_aside) == set(lines)
    assert report.given_up == (), "the pen holds them all while their site is kept"
    posted = {note.fingerprint for note in notifications.active()}
    assert PULL_PARENT_SET_ASIDE in posted
    assert HELD_GIVEN_UP not in posted, "so the two notices cannot contradict each other"

    ours.name = "Reef South"
    store.update_site(ours)
    engine = SyncEngine(store, FakeRegistry(), out_root=tmp_path, pull_sections=PULL_SECTIONS)
    engine.pull()

    assert [line for line in lines if store.get_transect(line) is None] == []
    assert engine.quarantined() == []
    assert store.sync_state(HELD_KEY) is None


def test_a_row_given_up_on_is_never_one_still_waiting_on_a_set_aside_parent(
    store, tmp_path, monkeypatch
):
    """The pen can hold both kinds at once, and only one of them is ever given
    up on, so the two lists the pull reports have to stay disjoint. A row in
    both would be told to keep waiting by one notice and written off by the
    next."""
    monkeypatch.setattr("deepreefmap_gui.sync.engine.HELD_MAX", 1)
    ours = Site(name="Reef")
    store.add_site(ours)
    theirs, orphan = uuid.uuid4(), uuid.uuid4()
    blocked = [uuid.uuid4() for _ in range(3)]
    report = SyncEngine(
        store,
        FakeRegistry(pages=[page(10, {
            "sites": [site_row(theirs, "Reef")],
            "transects": [
                transect_row(orphan, "Chute", site_id=str(uuid.uuid4())),
                *(
                    transect_row(line, f"Wall {number}", site_id=str(theirs))
                    for number, line in enumerate(blocked)
                ),
            ],
        })]),
        out_root=tmp_path,
        pull_sections=PULL_SECTIONS,
    ).pull()

    assert set(report.behind_set_aside) == set(blocked)
    assert report.given_up == (orphan,), "the one whose site nothing here is keeping"
    assert set(report.given_up).isdisjoint(report.behind_set_aside)


# --- Pull: a transect waiting for its site ---


def test_a_transect_that_arrives_before_its_site_is_held(store, tmp_path):
    """Sites sort ahead of transects within a page but not across pages, so a
    site edited after its lines comes down behind them. The foreign key would
    take the page down, and the page would come back identical on every sync."""
    site_id, transect_id = uuid.uuid4(), uuid.uuid4()
    notifications = NotificationCenter()
    first = FakeRegistry(pages=[page(10, {
        "transects": [transect_row(transect_id, "Reef Wall", site_id=str(site_id))],
    })])

    held = SyncEngine(store, first, out_root=tmp_path, notifications=notifications).pull()

    assert held.transects_without_sites == (transect_id,)
    assert store.get_transect(transect_id) is None
    assert store.sync_state(HELD_KEY) is not None
    assert TRANSECT_WITHOUT_SITE in {note.fingerprint for note in notifications.active()}

    second = FakeRegistry(pages=[page(20, {"sites": [site_row(site_id, "Reef")]})])
    landed = SyncEngine(store, second, out_root=tmp_path).pull()

    assert store.get_transect(transect_id).site_id == site_id
    assert landed.transects_without_sites == ()
    assert store.sync_state(HELD_KEY) is None


def test_a_transect_lands_with_the_site_it_arrives_beside(store, tmp_path):
    """The ordinary case, which must not start waiting a sync for its parent."""
    site_id, transect_id = uuid.uuid4(), uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {
        "sites": [site_row(site_id, "Reef")],
        "transects": [transect_row(transect_id, "Reef Wall", site_id=str(site_id))],
    })])

    report = SyncEngine(store, registry, out_root=tmp_path).pull()

    assert report.transects_without_sites == ()
    assert store.get_transect(transect_id).site_id == site_id


# --- Push: a row the registry's own database would not take ---


def test_a_conflicted_row_is_accounted_for_and_reported(store, tmp_path):
    """Scenario: the registry answers that its database refused a transect,
    which is what a name another record there already carries looks like.

    Expected behaviour: the section is fully accounted for, so its watermark
    moves and the row stops being re-offered on every sync forever. The diver
    hears that it did not land, rather than that the registry is misbehaving.
    """
    transect, _video, _pass = seed_pass(store)
    notifications = NotificationCenter()
    registry = FakeRegistry(conflicted={"transects": [transect.id]})
    engine = SyncEngine(store, registry, out_root=tmp_path, notifications=notifications)

    report = engine.push()

    assert report.conflicted == [transect.id]
    assert report.unaccounted == ()
    assert engine.watermark("transects") is not None
    assert PUSH_UNACCOUNTED not in {note.fingerprint for note in notifications.active()}
    note = one_note(notifications, PUSH_CONFLICTED)
    assert (note.severity, note.scope) == (WARNING, SURVEY)


def test_a_conflicted_row_stops_being_offered_until_it_changes(store, tmp_path):
    """Re-sending cannot help, so the mark goes with the watermark. Changing what
    made it conflict marks it again, which is the one thing that can help."""
    transect, _video, _pass = seed_pass(store)
    registry = FakeRegistry(conflicted={"transects": [transect.id]})
    SyncEngine(store, registry, out_root=tmp_path).push()

    quiet = FakeRegistry()
    assert SyncEngine(store, quiet, out_root=tmp_path).push().sections == {}

    transect.name = "Reef Wall"
    store.update_transect(transect)
    again = FakeRegistry()
    SyncEngine(store, again, out_root=tmp_path).push()

    assert [row["name"] for row in again.pushes[0]["transects"]] == ["Reef Wall"]


# --- Pull: what a person typed, as opposed to what a pull wrote ---


def test_a_row_a_pull_wrote_is_not_read_as_a_local_edit(store, tmp_path):
    """A pulled row carries a stamp newer than anything local, so a device
    reading the edit off the stamp found one where nobody had typed anything:
    the registry skipping back its own row came out as a conflict every sync."""
    transect_id = uuid.uuid4()
    registry = FakeRegistry(pages=[page(10, {"transects": [transect_row(transect_id)]})])
    SyncEngine(store, registry, out_root=tmp_path).pull()
    notifications = NotificationCenter()
    pushing = FakeRegistry(skipped={"transects": [transect_id]})

    report = SyncEngine(
        store, pushing, out_root=tmp_path, notifications=notifications
    ).push()

    assert store.pending_push_ids("transects") == set()
    assert report.skipped == []
    assert notifications.active() == []


def test_a_pull_over_a_row_nobody_touched_is_not_an_overwrite(store, tmp_path):
    """The registry's copy replacing an untouched row is a download, not a
    conflict. Warning about one on every sync teaches the reader to ignore the
    warnings that matter."""
    transect_id = uuid.uuid4()
    first = FakeRegistry(pages=[page(10, {"transects": [transect_row(transect_id)]})])
    SyncEngine(store, first, out_root=tmp_path).pull()
    second = FakeRegistry(pages=[page(20, {
        "transects": [transect_row(transect_id, "Renamed there", updated_at=MUCH_LATER)],
    })])
    notifications = NotificationCenter()

    report = SyncEngine(
        store, second, out_root=tmp_path, notifications=notifications
    ).pull()

    assert store.get_transect(transect_id).name == "Renamed there"
    assert report.overwritten == ()
    assert notifications.active() == []


def test_the_marks_are_only_read_for_sections_that_carry_them():
    """A mark exists only where the store puts a trigger, and the pull's
    overwritten report is the one inference still resting on one. Asked about a
    section outside that table it would answer "nobody typed this" for ever,
    silently and without ever being wrong out loud, which is exactly how the
    set-aside split came to call a discarded site one that had landed."""
    from deepreefmap_gui.survey.store import _PENDING_TABLES

    assert set(AUTHORED_SECTIONS) == set(_PENDING_TABLES)
