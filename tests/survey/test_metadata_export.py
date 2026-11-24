"""The footage metadata table: one row per clip in a pass, plus clips in no pass."""

import csv

import pytest
from _factories import make_transect, make_video

from deepreefmap_gui.survey.metadata_export import (
    METADATA_COLUMNS,
    METADATA_SCHEMA_VERSION,
    build_metadata_rows,
    save_metadata_csv,
)
from deepreefmap_gui.survey.models import RunRecord, TransectPass
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def store(tmp_path):
    return SurveyStore(tmp_path / "survey.db")


def _filed(store):
    campaign = Campaign(name="2026_11_jordan")
    store.add_campaign(campaign)
    transect = make_transect("JO_VC_6m_1")
    store.add_transect(transect)
    first = store.upsert_video(make_video(
        "a" * 32, file_name="GX010095.MP4", path="/d/GX010095.MP4", camera_label="Cam1"
    ))
    chapter = store.upsert_video(make_video(
        "b" * 32, file_name="GX020095.MP4", path="/d/GX020095.MP4", camera_label="Cam1"
    ))
    pass_ = TransectPass(
        transect_id=transect.id,
        video_id=first.id,
        extra_video_ids=[chapter.id],
        begin_s=12.0,
        end_s=300.0,
        direction="forward",
        campaign_id=campaign.id,
        quality="good",
        notes="Vis 5 m",
    )
    store.add_pass(pass_)
    return pass_, first, chapter


def test_a_pass_over_two_chapters_is_two_rows_in_order(store):
    pass_, first, chapter = _filed(store)

    rows = build_metadata_rows(store)

    assert [r["file_name"] for r in rows] == ["GX010095.MP4", "GX020095.MP4"]
    assert [r["clip_ordinal"] for r in rows] == [1, 2]
    for row in rows:
        assert row["campaign"] == "2026_11_jordan"
        assert row["transect"] == "JO_VC_6m_1"
        assert row["pass_label"] == "JO_VC_6m_1 pass 01"
        assert row["direction"] == "forward"
        assert row["pass_comment"] == "Vis 5 m"
        assert row["quality"] == "good"
        assert row["begin_s"] == 12.0
        assert row["pass_id"] == str(pass_.id)
        assert row["camera_label"] == "Cam1"


def test_a_clip_no_pass_uses_is_still_listed(store):
    _filed(store)
    store.upsert_video(make_video("c" * 32, file_name="GX030001.MP4", path="/d/GX030001.MP4"))

    rows = build_metadata_rows(store)

    waiting = [r for r in rows if r["file_name"] == "GX030001.MP4"]
    assert len(waiting) == 1
    assert waiting[0].get("pass_id") is None
    assert waiting[0]["clip_ordinal"] is None


def test_the_latest_run_is_named(store):
    pass_, *_ = _filed(store)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="JO_VC_6m_1__p01", status="failed"))
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="JO_VC_6m_1__p01__r02", status="succeeded"))

    row = build_metadata_rows(store)[0]

    assert row["runs"] == 2
    assert row["latest_run_dir"] == "JO_VC_6m_1__p01__r02"
    assert row["latest_run_status"] == "succeeded"


def test_the_csv_has_the_fixed_header_and_blank_cells_for_missing(store, tmp_path):
    _filed(store)
    store.upsert_video(make_video("c" * 32, file_name="GX030001.MP4", path="/d/GX030001.MP4"))
    out = tmp_path / "meta.csv"

    written = save_metadata_csv(out, build_metadata_rows(store))

    with out.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == METADATA_COLUMNS
        rows = list(reader)
    assert written == len(rows) == 3
    assert {r["schema_version"] for r in rows} == {str(METADATA_SCHEMA_VERSION)}
    assert rows[0]["upside_down"] == "false"
    assert rows[2]["transect"] == ""


def test_camera_labels_are_offered_once_each_sorted(store):
    store.upsert_video(make_video("a" * 32, path="/d/1.MP4", camera_label="cam2"))
    store.upsert_video(make_video("b" * 32, path="/d/2.MP4", camera_label="Cam1"))
    store.upsert_video(make_video("c" * 32, path="/d/3.MP4", camera_label="cam2"))
    store.upsert_video(make_video("d" * 32, path="/d/4.MP4", camera_label=None))

    assert store.camera_labels() == ["Cam1", "cam2"]
