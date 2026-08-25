"""The wire shape, on plain dicts and dataclasses: no store, no server."""

from __future__ import annotations

import json
import uuid

import pytest
from _factories import make_transect, make_video, write_run

from deepreefmap_gui.cover import taxonomy_hash, taxonomy_version
from deepreefmap_gui.packaging.releases import current_version
from deepreefmap_gui.survey.analysis import LongCoverRow
from deepreefmap_gui.survey.models import RunRecord, SurveyBatch, TransectPass
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import wire


def make_pass(chapters: int = 3, **overrides) -> TransectPass:
    video_ids = [uuid.uuid4() for _ in range(chapters)]
    return TransectPass(**{
        "transect_id": uuid.uuid4(),
        "video_id": video_ids[0],
        "extra_video_ids": video_ids[1:],
        "begin_s": 0.0,
        "end_s": 60.0,
        "batch_id": uuid.uuid4(),
        **overrides,
    })


def make_cover_row(**overrides) -> LongCoverRow:
    return LongCoverRow(**{
        "transect_name": "T1",
        "transect_id": str(uuid.uuid4()),
        "level": "intermediate",
        "group": "hard coral",
        "estimator": wire.PER_PASS,
        "fraction": 0.32,
        "count": 41822.0,
        "denominator": 134045.0,
        "contributing_passes": 2,
        "expected_passes": 3,
        "pass_id": str(uuid.uuid4()),
        "direction": "forward",
        "begin_s": 0.0,
        "end_s": 60.0,
        "run_id": str(uuid.uuid4()),
        "run_dir_name": "t1__p01",
        "deepreefmap_version": "1.2.3",
        "segmentation_model": "segformer-b2",
        "mapping_backend": "loger",
        "gui_version": "0.3.3",
        "taxonomy_version": 4,
        "taxonomy_hash": "ab" * 32,
        **overrides,
    })


def chapter_row(pass_id, video_id, ordinal, **overrides) -> dict:
    return {
        "id": str(wire.pass_video_id(pass_id, video_id)),
        "pass_id": str(pass_id),
        "video_id": str(video_id),
        "ordinal": ordinal,
        "deleted_at": None,
        **overrides,
    }


# --- Sections ---


def test_the_wire_sections_are_the_stores_plus_the_two_it_derives():
    derived = (wire.PASS_VIDEOS, wire.COVER_ROWS)
    assert [s for s in wire.WIRE_SECTIONS if s not in derived] == list(SYNC_SECTIONS)
    assert set(derived) <= set(wire.WIRE_SECTIONS)
    assert wire.WIRE_SECTIONS.index(wire.PASS_VIDEOS) > wire.WIRE_SECTIONS.index("passes")
    assert wire.WIRE_SECTIONS.index(wire.COVER_ROWS) > wire.WIRE_SECTIONS.index("runs")


def test_a_section_with_no_table_behind_it_is_refused():
    with pytest.raises(KeyError):
        wire.rows_to_wire(wire.PASS_VIDEOS, [])


# --- Outbound field mapping ---


def test_a_clip_leaves_its_disk_location_behind():
    """The registry holds no absolute paths, so nothing describing this laptop's
    disk may be sent."""
    video = make_video(probed_at="2026-08-01T00:00:00+00:00", mtime="2026-08-01T00:00:00+00:00")

    row = wire.rows_to_wire("videos", [video])[0]

    assert {"path", "mtime", "probed_at"}.isdisjoint(row)
    assert row["file_name"] == video.file_name
    assert (row["hash"], row["gravity"], row["gps"]) == (video.hash, video.gravity, video.gps)
    assert row["id"] == str(video.id)


def test_a_pass_leaves_its_session_and_its_chapter_columns_behind():
    pass_ = make_pass(campaign_id=uuid.uuid4(), quality="very_good", upside_down=True, label="swim 1")

    row = wire.rows_to_wire("passes", [pass_])[0]

    assert {"batch_id", "video_id", "extra_video_ids"}.isdisjoint(row)
    assert row["campaign_id"] == str(pass_.campaign_id)
    assert (row["quality"], row["upside_down"], row["label"]) == ("very_good", True, "swim 1")


def test_a_transect_carries_its_site_and_both_accuracies():
    transect = make_transect(site_id=uuid.uuid4(), start_accuracy_m=3.5, end_accuracy_m=4.0)

    row = wire.rows_to_wire("transects", [transect])[0]

    assert row["site_id"] == str(transect.site_id)
    assert (row["start_accuracy_m"], row["end_accuracy_m"]) == (3.5, 4.0)


def test_a_run_leaves_the_session_it_ran_in_behind(tmp_path):
    run = RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01", batch_id=uuid.uuid4())

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert "batch_id" not in row
    assert row["run_dir_name"] == "t1__p01"


# --- pass_video ---


def test_chapters_leave_as_ordered_join_rows():
    pass_ = make_pass()

    rows = wire.pass_video_rows(pass_)

    assert [row["video_id"] for row in rows] == [str(v) for v in pass_.video_ids()]
    assert [row["ordinal"] for row in rows] == [0, 1, 2]
    assert {row["pass_id"] for row in rows} == {str(pass_.id)}
    assert all(row["created_at"] == wire.to_wire_time(pass_.created_at) for row in rows)


def test_a_join_row_id_is_the_same_on_every_device_and_every_push():
    pass_ = make_pass()

    first = wire.pass_video_rows(pass_)
    again = wire.pass_video_rows(pass_)

    assert [row["id"] for row in first] == [row["id"] for row in again]
    assert first[0]["id"] == str(wire.pass_video_id(pass_.id, pass_.video_id))
    assert len({row["id"] for row in first}) == 3
    assert wire.pass_video_id(pass_.id, pass_.video_id) != wire.pass_video_id(
        pass_.video_id, pass_.id
    )


def test_a_deleted_pass_takes_its_chapters_with_it():
    pass_ = make_pass(deleted_at="2026-08-20T00:00:00+00:00")

    rows = wire.pass_video_rows(pass_)

    assert {row["deleted_at"] for row in rows} == {"2026-08-20T00:00:00Z"}


# --- pass_video, inbound ---


def test_chapters_come_back_in_ordinal_order():
    pass_id = uuid.uuid4()
    videos = [uuid.uuid4() for _ in range(3)]
    rows = [chapter_row(pass_id, videos[i], i) for i in (2, 0, 1)]

    assert wire.video_ids_from_pass_videos(rows) == [videos[0], videos[1], videos[2]]


def test_a_gap_in_the_ordinals_only_orders():
    pass_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    rows = [chapter_row(pass_id, second, 7), chapter_row(pass_id, first, 0)]

    assert wire.video_ids_from_pass_videos(rows) == [first, second]


def test_a_repeated_ordinal_is_broken_by_row_id():
    """Two rows at one ordinal should be impossible. Whatever arrives, two devices
    reading it have to agree on the order."""
    pass_id = uuid.uuid4()
    rows = [chapter_row(pass_id, uuid.uuid4(), 0) for _ in range(2)]
    expected = [
        uuid.UUID(row["video_id"]) for row in sorted(rows, key=lambda row: row["id"])
    ]

    assert wire.video_ids_from_pass_videos(rows) == expected
    assert wire.video_ids_from_pass_videos(list(reversed(rows))) == expected


def test_a_tombstoned_chapter_is_off_the_pass():
    pass_id = uuid.uuid4()
    kept, dropped = uuid.uuid4(), uuid.uuid4()
    rows = [
        chapter_row(pass_id, kept, 0),
        chapter_row(pass_id, dropped, 1, deleted_at="2026-08-20T00:00:00+00:00"),
    ]

    assert wire.video_ids_from_pass_videos(rows) == [kept]


def test_a_clip_named_twice_keeps_its_first_place():
    """The pass model refuses a chapter that is also its first video."""
    pass_id, video_id = uuid.uuid4(), uuid.uuid4()
    rows = [chapter_row(pass_id, video_id, 0), chapter_row(pass_id, video_id, 1)]

    assert wire.video_ids_from_pass_videos(rows) == [video_id]


def test_no_chapter_rows_at_all_is_an_empty_list():
    assert wire.video_ids_from_pass_videos([]) == []


def test_folding_fills_the_pass_and_hands_back_what_it_could_not_place():
    mine, other = uuid.uuid4(), uuid.uuid4()
    videos = [uuid.uuid4() for _ in range(3)]
    chapters = [
        chapter_row(mine, videos[0], 0),
        chapter_row(mine, videos[1], 1),
        chapter_row(other, videos[2], 0),
    ]

    folded, unattached = wire.fold_pass_videos([{"id": str(mine)}], chapters)

    assert folded == [{
        "id": str(mine),
        "video_id": str(videos[0]),
        "extra_video_ids": [str(videos[1])],
    }]
    assert unattached == {str(other): [videos[2]]}


def test_a_pass_with_no_live_chapters_keeps_neither_field():
    """So the stored chapter list of a pass this device already holds stands, and a
    pass it has never seen cannot be built at all."""
    pass_id = uuid.uuid4()
    chapters = [chapter_row(pass_id, uuid.uuid4(), 0, deleted_at="2026-08-20T00:00:00+00:00")]

    folded, unattached = wire.fold_pass_videos([{"id": str(pass_id), "notes": "surge"}], chapters)

    assert folded == [{"id": str(pass_id), "notes": "surge"}]
    assert unattached == {}


# --- Run provenance ---


def seed_manifest(tmp_path, **overrides):
    transect = make_transect()
    pass_ = make_pass(transect_id=transect.id)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", status="succeeded")
    batch = SurveyBatch(name="Day 1", preset_name="reef_default")
    write_run(
        tmp_path,
        run.run_dir_name,
        deepreefmap_version="1.2.3",
        segmentation_model="segformer-b2",
        mapping_backend="loger_star",
        survey=survey_manifest_block(
            run,
            pass_,
            transect,
            batch,
            config={"preset_name": "reef_default", "deviations": {"fps": 4}},
            model_versions={"EPFL-ECEO/coralscapes-vit-b-dpt": "c0ffee"},
        ),
        **overrides,
    )
    return run


def test_run_provenance_comes_out_of_the_manifest(tmp_path):
    run = seed_manifest(tmp_path)

    provenance = wire.run_provenance(tmp_path, run.run_dir_name)

    assert provenance["library_version"] == "1.2.3"
    assert provenance["segmentation_model"] == "segformer-b2"
    assert provenance["mapping_backend"] == "loger_star"
    assert provenance["gui_version"] == current_version()
    assert provenance["taxonomy_version"] == taxonomy_version()
    assert provenance["taxonomy_hash"] == taxonomy_hash()
    assert provenance["model_revisions"] == {"EPFL-ECEO/coralscapes-vit-b-dpt": "c0ffee"}
    assert provenance["preset_name"] == "reef_default"
    assert provenance["preset_deviations"] == {"fps": 4}


def test_provenance_reads_a_manifest_that_was_never_written(tmp_path):
    """A run that dies leaves no manifest, so what it measured arrives as a
    document in memory rather than as a directory to open."""
    run = seed_manifest(tmp_path)
    on_disk = wire.run_provenance(tmp_path, run.run_dir_name)
    document = json.loads((tmp_path / run.run_dir_name / "run_manifest.json").read_text())

    assert wire.provenance_from_manifest(document) == on_disk
    assert wire.provenance_from_manifest({}) == dict.fromkeys(on_disk)


def test_the_configuration_a_run_processed_at_comes_out_of_the_manifest(tmp_path):
    """The resolved pixel size, never the name of a resolution preset: Native,
    Half and Quarter become numbers in the form before the run starts."""
    run = seed_manifest(
        tmp_path,
        processing_width=1376,
        processing_height=768,
        fps=4,
        preprocess_batch_size=8,
    )

    provenance = wire.run_provenance(tmp_path, run.run_dir_name)

    assert provenance["processing_width"] == 1376
    assert provenance["processing_height"] == 768
    assert provenance["fps"] == 4
    assert provenance["preprocess_batch_size"] == 8


def test_a_manifest_from_before_the_configuration_was_recorded_reads_as_nulls(tmp_path):
    """An older run still pushes everything else it knows."""
    run = seed_manifest(tmp_path)

    provenance = wire.run_provenance(tmp_path, run.run_dir_name)

    assert provenance["gui_version"] == current_version()
    assert (
        provenance["processing_width"],
        provenance["processing_height"],
        provenance["fps"],
        provenance["preprocess_batch_size"],
    ) == (None, None, None, None)


def test_a_pruned_run_directory_degrades_to_nulls(tmp_path):
    """An old run whose folder was reclaimed must not stop the whole push."""
    provenance = wire.run_provenance(tmp_path, "gone")

    assert set(provenance) == set(wire.run_provenance(tmp_path, "gone"))
    assert all(value is None for value in provenance.values())


def test_an_unreadable_manifest_degrades_to_nulls(tmp_path):
    (tmp_path / "t1__p01").mkdir()
    (tmp_path / "t1__p01" / "run_manifest.json").write_text("{not json")

    assert all(value is None for value in wire.run_provenance(tmp_path, "t1__p01").values())


def test_an_unrecorded_configuration_is_not_an_unchanged_one(tmp_path):
    """A preset that recorded no deviations and a run that recorded no preset are
    two different facts."""
    unchanged = seed_manifest(tmp_path)
    write_run(
        tmp_path,
        unchanged.run_dir_name,
        survey=survey_manifest_block(
            unchanged,
            make_pass(),
            None,
            None,
            config={"preset_name": "reef_default", "deviations": {}},
        ),
    )
    assert wire.run_provenance(tmp_path, unchanged.run_dir_name)["preset_deviations"] == {}

    write_run(tmp_path, "t1__p02")
    assert wire.run_provenance(tmp_path, "t1__p02")["preset_deviations"] is None


def test_a_run_row_carries_its_stored_provenance_without_the_manifest(tmp_path):
    """The row is the durable copy: once provenance is recorded on it, pruning
    the run directory costs nothing on the next push."""
    run = RunRecord(
        pass_id=uuid.uuid4(),
        run_dir_name="pruned",
        status="succeeded",
        gui_version="0.9.0",
        library_version="1.2.3",
        segmentation_model="segformer-b2",
        mapping_backend="loger_star",
        processing_width=1376,
        processing_height=768,
        fps=4,
        preprocess_batch_size=8,
        taxonomy_version=2,
        taxonomy_hash="ab" * 32,
        model_revisions={"repo": "c0ffee"},
        preset_name="reef_default",
        preset_version=1,
        preset_hash="deadbeef1234",
        preset_deviations={},
        run_duration_s=321.5,
        stage_durations={"mapping": 200.0},
        stage_peaks={"ram_bytes": 1024},
    )

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["library_version"] == "1.2.3"
    assert row["model_revisions"] == {"repo": "c0ffee"}
    assert row["preset_deviations"] == {}
    assert row["preset_version"] == 1
    assert row["preset_hash"] == "deadbeef1234"
    assert row["run_duration_s"] == 321.5
    assert row["stage_durations"] == {"mapping": 200.0}
    assert row["stage_peaks"] == {"ram_bytes": 1024}
    # The grain the registry collates peaks at, which is only comparable if a
    # peak carries the configuration it was measured under.
    assert (row["processing_width"], row["processing_height"]) == (1376, 768)
    assert (row["fps"], row["preprocess_batch_size"]) == (4, 8)


def test_a_legacy_run_row_still_reads_its_manifest(tmp_path):
    run = seed_manifest(
        tmp_path,
        run_duration_s=88.0,
        stage_durations={"mapping": 60.0},
        processing_width=688,
        processing_height=384,
        fps=2,
        preprocess_batch_size=4,
    )

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["library_version"] == "1.2.3"
    assert row["preset_deviations"] == {"fps": 4}
    assert row["run_duration_s"] == 88.0
    assert row["stage_durations"] == {"mapping": 60.0}
    assert (row["processing_width"], row["processing_height"]) == (688, 384)
    assert (row["fps"], row["preprocess_batch_size"]) == (2, 4)


def test_a_row_stamped_before_the_configuration_columns_is_completed_from_the_manifest(tmp_path):
    """Scenario: a run stamped by a build that knew only some of these columns.

    Expected behaviour: the row wins where it holds a value and the manifest
    beside it fills the rest. Reading the row whole instead pushed the whole
    existing corpus as nulls for the new columns, because one non-null field
    was enough to decide the manifest need not be opened.
    """
    run = seed_manifest(tmp_path, processing_width=1376, fps=4)
    run.gui_version = "0.9.0"

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["gui_version"] == "0.9.0"
    assert (row["processing_width"], row["fps"]) == (1376, 4)


def test_a_stored_field_outranks_the_manifest_beside_it(tmp_path):
    """The row is the durable copy: a manifest edited or re-folded later does not
    rewrite what the run recorded about itself when it finished."""
    run = seed_manifest(tmp_path, processing_width=1376, fps=4)
    run.fps = 2

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["fps"] == 2
    assert row["processing_width"] == 1376


def test_the_metric_source_follows_fusion(tmp_path):
    write_run(tmp_path, "fused", enable_tsdf=True)
    write_run(tmp_path, "plain", enable_tsdf=False)
    write_run(tmp_path, "silent")

    assert wire.metric_source(tmp_path, "fused") == "tsdf"
    assert wire.metric_source(tmp_path, "plain") == "unprojected"
    assert wire.metric_source(tmp_path, "silent") is None
    assert wire.metric_source(tmp_path, "gone") is None


# --- cover_row ---


def test_only_the_per_pass_estimator_travels(tmp_path):
    """The pooled figure is derived from these rows, so storing it centrally would
    invite two disagreeing numbers. Its rows also carry no run id."""
    run = RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01", status="succeeded")
    per_pass = make_cover_row(run_id=str(run.id))
    pooled = make_cover_row(run_id="", estimator="pooled")

    rows = wire.cover_rows_to_wire([per_pass, pooled], {str(run.id): run}, tmp_path)

    assert [row["estimator"] for row in rows] == [wire.PER_PASS]


def test_a_cover_row_renames_group_and_count(tmp_path):
    run = RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01", status="succeeded")
    write_run(tmp_path, run.run_dir_name, enable_tsdf=False)
    source = make_cover_row(run_id=str(run.id))

    row = wire.cover_rows_to_wire([source], {str(run.id): run}, tmp_path)[0]

    assert row["class_group"] == source.group
    assert row["point_count"] == source.count
    assert (row["level"], row["fraction"], row["denominator"]) == (
        source.level, source.fraction, source.denominator,
    )
    assert row["metric_source"] == "unprojected"
    assert row["created_at"] == wire.to_wire_time(run.created_at)
    assert {"group", "count", "transect_name", "gui_version"}.isdisjoint(row)


def test_a_cover_row_id_is_the_registrys_own_unique_key(tmp_path):
    run = RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01", status="succeeded")
    source = make_cover_row(run_id=str(run.id))
    runs = {str(run.id): run}

    first = wire.cover_rows_to_wire([source], runs, tmp_path)[0]["id"]

    assert first == wire.cover_rows_to_wire([source], runs, tmp_path)[0]["id"]
    assert first == str(
        wire.cover_row_id(source.run_id, source.level, source.group, source.estimator)
    )
    assert first != wire.cover_rows_to_wire(
        [make_cover_row(run_id=str(run.id), level="coarse")], runs, tmp_path
    )[0]["id"]


def test_cover_for_a_run_outside_the_document_is_left_behind(tmp_path):
    """A cover row naming a run the registry has never seen is a 409."""
    assert wire.cover_rows_to_wire([make_cover_row()], {}, tmp_path) == []


# --- Timestamps ---


def test_a_stored_stamp_round_trips_through_the_wire():
    stored = "2026-08-01T09:30:00+00:00"

    assert wire.to_wire_time(stored) == "2026-08-01T09:30:00Z"
    assert wire.from_wire_time(wire.to_wire_time(stored)) == stored


def test_a_canonical_wire_stamp_round_trips_back():
    canonical = "2026-08-01T09:30:00Z"

    assert wire.from_wire_time(canonical) == "2026-08-01T09:30:00+00:00"
    assert wire.to_wire_time(wire.from_wire_time(canonical)) == canonical


def test_an_offset_stamp_is_normalised_to_utc():
    """Last-write-wins compares these as strings, so an offset would compare wrongly."""
    assert wire.to_wire_time("2026-08-01T11:30:00+02:00") == "2026-08-01T09:30:00Z"
    assert wire.from_wire_time("2026-08-01T11:30:00+02:00") == "2026-08-01T09:30:00+00:00"


def test_sub_second_precision_survives_both_directions():
    """The registry keeps sub-second stamps, and truncating one would make a newer
    row compare as equal."""
    assert wire.to_wire_time("2026-08-01T09:30:00.500000Z") == "2026-08-01T09:30:00.500000Z"
    assert wire.from_wire_time("2026-08-01T09:30:00.500000Z") == "2026-08-01T09:30:00.500000+00:00"
    assert "2026-08-01T09:30:00.500000+00:00" > "2026-08-01T09:30:00+00:00"


def test_a_stamp_with_no_zone_is_read_as_utc():
    assert wire.to_wire_time("2026-08-01T09:30:00") == "2026-08-01T09:30:00Z"


def test_an_empty_or_unreadable_stamp_is_left_as_it_stands():
    for value in (None, "", "shortly"):
        assert wire.to_wire_time(value) == value
        assert wire.from_wire_time(value) == value


def test_every_stamp_on_a_row_is_converted(tmp_path):
    run = RunRecord(
        pass_id=uuid.uuid4(),
        run_dir_name="t1__p01",
        started_at="2026-08-01T09:00:00+00:00",
        finished_at="2026-08-01T09:05:00+00:00",
        deleted_at="2026-08-01T10:00:00+00:00",
    )

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["started_at"] == "2026-08-01T09:00:00Z"
    assert row["finished_at"] == "2026-08-01T09:05:00Z"
    assert row["deleted_at"] == "2026-08-01T10:00:00Z"
    assert row["created_at"].endswith("Z")
    assert row["updated_at"].endswith("Z")


def test_an_expedition_keeps_its_dates_as_days():
    """begin_date and end_date are days, not moments."""
    from deepreefmap_gui.survey.models import Campaign

    row = wire.rows_to_wire(
        "campaigns", [Campaign(name="2025_10_eritrea", begin_date="2025-10-01", end_date="2025-10-20")]
    )[0]

    assert (row["begin_date"], row["end_date"]) == ("2025-10-01", "2025-10-20")


# --- Unknown sections ---


def test_a_section_this_build_has_no_reading_of_is_named():
    unknown = wire.unknown_sections({
        "sites": [{"id": "a"}],
        "quadrats": [{"id": "b"}],
        "observations": [{"id": "c"}],
    })

    assert unknown == ("observations", "quadrats")


def test_the_two_derived_sections_are_known():
    """Neither has a table behind it here, and both arrive on an ordinary pull."""
    assert wire.unknown_sections({wire.PASS_VIDEOS: [{"id": "a"}], wire.COVER_ROWS: [{}]}) == ()


def test_a_section_named_with_nothing_in_it_is_not_data():
    assert wire.unknown_sections({"quadrats": [], "observations": None}) == ()


# --- Inbound ---


def test_an_inbound_row_drops_the_registrys_cursor_and_keeps_what_it_carried():
    pulled = {
        "id": str(uuid.uuid4()),
        "file_name": "GX010001.MP4",
        "captured_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "server_seq": 4102,
    }

    row = wire.rows_from_wire([pulled])[0]

    assert "server_seq" not in row
    assert row["captured_at"] == "2026-07-01T10:00:00+00:00"
    assert row["updated_at"] == "2026-08-01T00:00:00+00:00"
    assert set(row) == set(pulled) - {"server_seq"}


def test_a_pass_row_survives_the_trip_out_and_back():
    """A pushed pass and the same pass pulled again describe the same swim."""
    pass_ = make_pass(quality="meh", upside_down=True, campaign_id=uuid.uuid4())

    sent = wire.rows_to_wire("passes", [pass_])[0]
    chapters = wire.pass_video_rows(pass_)
    folded, unattached = wire.fold_pass_videos(wire.rows_from_wire([sent]), chapters)

    assert unattached == {}
    landed = folded[0]
    assert landed["video_id"] == str(pass_.video_id)
    assert landed["extra_video_ids"] == [str(v) for v in pass_.extra_video_ids]
    assert landed["updated_at"] == pass_.updated_at
    assert (landed["quality"], landed["upside_down"]) == ("meh", True)


def test_a_cover_row_is_json_the_registry_can_check(tmp_path):
    """Every value has to survive json.dumps: the client sends the document as-is."""
    run = RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01", status="succeeded")
    rows = wire.cover_rows_to_wire([make_cover_row(run_id=str(run.id))], {str(run.id): run}, tmp_path)

    assert json.loads(json.dumps(rows)) == rows


# --- scrubbing ---


def test_an_error_leaves_its_usernames_behind(tmp_path):
    run = RunRecord(
        pass_id=uuid.uuid4(),
        run_dir_name="t1__p01",
        status="failed",
        error=r"open C:\Users\kim.nguyen\footage\GX01.MP4 and /home/kim/out failed",
        gui_version="0.9.0",
    )

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert "kim" not in row["error"]
    assert row["error"] == r"open ~\footage\GX01.MP4 and ~/out failed"


def test_a_deviating_path_travels_as_its_file_name(tmp_path):
    run = RunRecord(
        pass_id=uuid.uuid4(),
        run_dir_name="t1__p01",
        status="succeeded",
        gui_version="0.9.0",
        preset_deviations={
            "loger_model_path": "/home/kim/ckpts/loger_v3.pth",
            "scs_checkpoint_path": r"C:\Users\kim\scs.ckpt",
            "fps": 4,
        },
    )

    row = wire.run_rows_to_wire([run], tmp_path)[0]

    assert row["preset_deviations"] == {
        "loger_model_path": "loger_v3.pth",
        "scs_checkpoint_path": "scs.ckpt",
        "fps": 4,
    }
