import uuid
from dataclasses import fields

import pytest
from _factories import VIDEO_PATH, make_transect, make_video

from deepreefmap_gui.survey.models import (
    PASS_QUALITIES,
    BatchItem,
    Campaign,
    RunRecord,
    Site,
    SurveyBatch,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.models.convert import (
    build_document,
    from_row,
    parse_document,
    survey_manifest_block,
    to_row,
)
from deepreefmap_gui.survey.models.transect import mean_depth_m
from deepreefmap_gui.survey.video_probe import NO, UNKNOWN, YES


def make_pass(transect, video, **overrides):
    values = {"transect_id": transect.id, "video_id": video.id, "begin_s": 0.0, "end_s": 60.0}
    values.update(overrides)
    return TransectPass(**values)


def test_transect_rejects_bad_coordinates():
    with pytest.raises(ValueError):
        make_transect(start_lat=91.0)
    with pytest.raises(ValueError):
        make_transect(end_lon=-181.0)


def test_transect_rejects_empty_name_and_negative_lengths():
    with pytest.raises(ValueError):
        make_transect(name="  ")
    with pytest.raises(ValueError):
        make_transect(length_m=-1.0)
    with pytest.raises(ValueError):
        make_transect(depth_m=-0.5)


def test_geodesic_length_close_to_metric():
    # 0.001 degrees of longitude at the equator is ~111.2 m.
    t = make_transect(start_lat=0.0, start_lon=0.0, end_lat=0.0, end_lon=0.001)
    assert 110.0 < t.geodesic_length_m() < 112.0


def test_pass_rejects_bad_trim_and_direction():
    transect, video = make_transect(), make_video()
    with pytest.raises(ValueError):
        make_pass(transect, video, begin_s=-1.0)
    with pytest.raises(ValueError):
        make_pass(transect, video, begin_s=30.0, end_s=30.0)
    with pytest.raises(ValueError):
        make_pass(transect, video, direction="sideways")


def test_pass_rejects_a_quality_outside_the_scale():
    transect, video = make_transect(), make_video()
    make_pass(transect, video, quality=None)
    for quality in PASS_QUALITIES:
        make_pass(transect, video, quality=quality)
    with pytest.raises(ValueError):
        make_pass(transect, video, quality="brilliant")


def test_site_rejects_an_empty_name_and_impossible_coordinates():
    Site(name="Reef", latitude=None, longitude=None)
    with pytest.raises(ValueError):
        Site(name="  ")
    with pytest.raises(ValueError):
        Site(name="Reef", latitude=91.0)
    with pytest.raises(ValueError):
        Site(name="Reef", longitude=-181.0)


def test_campaign_rejects_an_empty_name_and_a_backwards_window():
    Campaign(name="2025_10_eritrea")
    with pytest.raises(ValueError):
        Campaign(name=" ")
    with pytest.raises(ValueError):
        Campaign(name="2025_10_eritrea", begin_date="2025-10-20", end_date="2025-10-01")


def test_transect_rejects_a_negative_fix_accuracy():
    with pytest.raises(ValueError):
        make_transect(start_accuracy_m=-1.0)
    with pytest.raises(ValueError):
        make_transect(end_accuracy_m=-1.0)


def test_pass_duration_is_the_trimmed_window():
    """What the ETA and the repeatability stats divide by."""
    transect, video = make_transect(), make_video()
    assert make_pass(transect, video, begin_s=12.5, end_s=42.5).duration_s() == 30.0


def test_a_session_is_labelled_by_when_it_began():
    """A session is a local queue, so nobody names it; its start identifies it."""
    assert SurveyBatch(created_at="2026-07-01T09:00").label == "2026-07-01 09:00:00"


def test_a_session_with_an_unreadable_start_still_labels_itself():
    """Rebuilt from a manifest, the stamp is whatever that file held."""
    assert SurveyBatch(created_at="not a time").label == "not a time"


def test_run_record_rejects_unknown_status():
    with pytest.raises(ValueError):
        RunRecord(pass_id=uuid.uuid4(), run_dir_name="run", status="exploded")
    with pytest.raises(ValueError):
        RunRecord(pass_id=uuid.uuid4(), run_dir_name=" ")


def test_row_round_trip_preserves_every_model():
    site, campaign = Site(name="Reef"), Campaign(name="2025_10_eritrea")
    transect, video = make_transect(site_id=site.id), make_video()
    batch = SurveyBatch(created_at="2026-07-01T09:00")
    pass_ = make_pass(
        transect, video, batch_id=batch.id, campaign_id=campaign.id, direction="reverse",
        quality="meh", surveyed_on="2026-07-01",
    )
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01__20260720-0900", batch_id=batch.id)
    item = BatchItem(batch_id=batch.id, pass_id=pass_.id)
    for model in (site, campaign, transect, video, batch, pass_, run, item):
        row = to_row(model)
        assert all(not isinstance(v, uuid.UUID) for v in row.values())
        assert from_row(type(model), row) == model


def test_document_round_trip():
    site, campaign = Site(name="Reef"), Campaign(name="2025_10_eritrea")
    transect, video = make_transect(site_id=site.id), make_video()
    batch = SurveyBatch(created_at="2026-07-01T09:00")
    pass_ = make_pass(transect, video, batch_id=batch.id, campaign_id=campaign.id)
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    item = BatchItem(batch_id=batch.id, pass_id=pass_.id)
    doc = build_document(
        sites=[site], campaigns=[campaign], transects=[transect], videos=[video],
        batches=[batch], passes=[pass_], runs=[run], batch_items=[item],
    )
    sections = parse_document(doc)
    assert sections["sites"] == [site]
    assert sections["campaigns"] == [campaign]
    assert sections["transects"] == [transect]
    assert sections["videos"] == [video]
    assert sections["batches"] == [batch]
    assert sections["passes"] == [pass_]
    assert sections["batch_items"] == [item]
    assert sections["runs"] == [run]


def test_a_document_written_before_batch_items_still_parses():
    transect, video = make_transect(), make_video()
    pass_ = make_pass(transect, video)
    doc = build_document(
        transects=[transect], videos=[video], batches=[], passes=[pass_], runs=[]
    )
    del doc["batch_items"]
    assert parse_document(doc)["batch_items"] == []


def test_document_rejects_unknown_schema_version():
    with pytest.raises(ValueError):
        parse_document({"schema_version": 999})


def test_manifest_block_snapshots_pass_and_transect():
    transect, video = make_transect(), make_video()
    batch = SurveyBatch(created_at="2026-07-01T09:00")
    pass_ = make_pass(transect, video, batch_id=batch.id, direction="reverse")
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    block = survey_manifest_block(run, pass_, transect, batch)
    assert block["run_id"] == str(run.id)
    assert block["batch_created_at"] == "2026-07-01T09:00"
    assert block["pass"] == {
        "id": str(pass_.id),
        "direction": "reverse",
        "begin_s": 0.0,
        "end_s": 60.0,
        "surveyed_on": None,
        "quality": None,
        "campaign": None,
    }
    assert block["transect"]["name"] == "T1"
    assert block["transect"]["start_lat"] == -17.5
    assert survey_manifest_block(run, pass_, transect, None)["batch_id"] is None


def test_manifest_block_records_model_versions():
    transect, video = make_transect(), make_video()
    pass_ = make_pass(transect, video)
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    versions = {"EPFL-ECEO/segformer-b5-finetuned-coralscapes-1024-1024": "a" * 40}

    recorded = survey_manifest_block(run, pass_, transect, None, model_versions=versions)
    assert recorded["provenance"]["model_versions"] == versions

    # Omitted, not written empty, when nothing was resolved.
    plain = survey_manifest_block(run, pass_, transect, None)
    assert "model_versions" not in plain["provenance"]


def test_pass_chapters_read_in_playing_order():
    """A GoPro splits a long swim at about 4 GB, so a pass may name several files."""
    transect, video = make_transect(), make_video()
    second = make_video("cd" * 16, file_name="GX020001.MP4")
    pass_ = make_pass(transect, video, extra_video_ids=[second.id], end_s=600.0)
    assert pass_.video_ids() == [video.id, second.id]

    row = to_row(pass_)
    # The chapters share one sqlite column, so they travel as a JSON array.
    assert isinstance(row["extra_video_ids"], str)
    assert from_row(TransectPass, row) == pass_


def test_pass_rejects_a_chapter_that_repeats_its_first_video():
    transect, video = make_transect(), make_video()
    with pytest.raises(ValueError):
        make_pass(transect, video, extra_video_ids=[video.id])


def test_every_video_field_is_covered_by_a_carry_over_group():
    """Scenario: a new column is added to VideoAsset and to nothing else.

    Expected behaviour: this fails. Both carry-over policies are written in terms
    of the groups, so a field in none of them is dropped whenever two rows for the
    same clip are folded into one, which is silent and only shows as lost data.
    """
    grouped = (
        VideoAsset.IDENTITY_FIELDS
        + VideoAsset.LOCATION_FIELDS
        + VideoAsset.CARRIED_FIELDS
        + VideoAsset.TRISTATE_FIELDS
        + VideoAsset.SYNC_FIELDS
        + VideoAsset.REVIEW_FIELDS
    )
    assert len(set(grouped)) == len(grouped), "a field is named in two groups"
    assert set(grouped) == {f.name for f in fields(VideoAsset)}


def test_overlay_takes_the_newer_reading_but_not_its_ignorance():
    """Upsert semantics: the clip just described off disk wins where it knows."""
    stored = make_video(duration_s=60.0, codec="hvc1", gravity=YES)
    described = make_video(path="/moved/GX010001.MP4", duration_s=61.0)
    stored.overlay_from(described)
    assert stored.path == "/moved/GX010001.MP4"
    assert stored.duration_s == 61.0
    assert stored.codec == "hvc1"
    assert stored.gravity == YES


def test_fill_keeps_the_keeper_and_takes_only_what_it_lacks():
    """Merge semantics: where the survivor lives is never a duplicate's to say."""
    keeper = make_video(duration_s=60.0, gravity=NO)
    loser = make_video(
        path="/elsewhere/GX010001.MP4", duration_s=61.0, fps=30.0, gravity=YES, gps=YES
    )
    keeper.fill_from(loser)
    assert keeper.path == VIDEO_PATH
    assert keeper.duration_s == 60.0
    assert keeper.fps == 30.0
    assert keeper.gravity == NO
    assert keeper.gps == YES


def test_a_video_document_written_before_container_metadata_still_imports():
    video = make_video()
    doc = build_document(transects=[], videos=[video], batches=[], passes=[], runs=[])
    for name in VideoAsset.CARRIED_FIELDS + VideoAsset.TRISTATE_FIELDS:
        doc["videos"][0].pop(name, None)
    restored = parse_document(doc)["videos"][0]
    assert restored.hash is None
    assert restored.captured_at is None
    assert (restored.gravity, restored.gps) == (UNKNOWN, UNKNOWN)
    assert restored.id == video.id


def test_a_document_written_before_chapters_still_parses():
    """A survey exported by an earlier build has no extra_video_ids to read."""
    transect, video = make_transect(), make_video()
    pass_ = make_pass(transect, video)
    doc = build_document(
        transects=[transect], videos=[video], batches=[], passes=[pass_], runs=[]
    )
    for row in doc["passes"]:
        del row["extra_video_ids"]
    assert parse_document(doc)["passes"] == [pass_]


def test_direction_reads_the_same_wherever_it_is_shown() -> None:
    """One spelling, so eleven surfaces cannot each invent their own."""
    from deepreefmap_gui.survey.models import (
        PASS_DIRECTIONS,
        direction_arrow,
        direction_text,
    )

    assert direction_text("forward") == "→ Forward"
    assert direction_text("reverse") == "← Reverse"
    assert {direction_arrow(d) for d in PASS_DIRECTIONS} == {"→", "←"}
    # Every value the store permits has a spelling, and nothing else does.
    for d in PASS_DIRECTIONS:
        assert direction_arrow(d) and direction_text(d)
    for absent in ("", None, "sideways", "  "):
        assert direction_arrow(absent) == "" and direction_text(absent) == ""


def test_transect_rejects_a_negative_end_depth():
    with pytest.raises(ValueError):
        make_transect(start_depth_m=-1.0)
    with pytest.raises(ValueError):
        make_transect(end_depth_m=-1.0)


def test_mean_depth_needs_both_ends():
    assert mean_depth_m(5.0, 11.0) == 8.0  # (5 + 11) / 2
    assert mean_depth_m(5.0, None) is None
    assert mean_depth_m(None, 11.0) is None
