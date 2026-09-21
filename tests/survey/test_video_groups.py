from __future__ import annotations

import uuid
from datetime import date

import pytest
from _factories import make_video

from deepreefmap_gui.survey.catalogue import VideoLibraryEntry
from deepreefmap_gui.survey.models import RunRecord, TransectPass
from deepreefmap_gui.survey.video_groups import (
    ALL_KEY,
    DEFAULT_PERIOD,
    PERIOD_KEYS,
    SORT_LENGTH,
    SORT_NAME,
    SORT_RECORDED,
    SORT_SIZE,
    UNDATED_KEY,
    UNDATED_TITLE,
    DateGroup,
    group_by_period,
    pass_status,
    sort_clips,
    sort_groups,
    timeline_spans,
)

TODAY = date(2026, 8, 6)


def clip(
    name: str = "GX010001.MP4",
    *,
    captured_at: str | None = None,
    mtime: str | None = None,
    duration_s: float | None = None,
    size_bytes: int | None = None,
    passes: list[TransectPass] | None = None,
    runs: list[RunRecord] | None = None,
) -> VideoLibraryEntry:
    video = make_video(
        file_name=name,
        captured_at=captured_at,
        mtime=mtime,
        duration_s=duration_s,
        size_bytes=size_bytes,
    )
    passes = passes or []
    runs = runs or []
    return VideoLibraryEntry(
        video=video,
        pass_count=len(passes),
        run_count=len(runs),
        passes=passes,
        runs=runs,
    )


def make_pass(begin_s: float = 0.0, end_s: float = 60.0, **overrides) -> TransectPass:
    return TransectPass(
        **{
            "transect_id": None,
            "video_id": uuid.uuid4(),
            "begin_s": begin_s,
            "end_s": end_s,
            **overrides,
        }
    )


def make_run(pass_: TransectPass, status: str, created_at: str) -> RunRecord:
    return RunRecord(
        pass_id=pass_.id,
        run_dir_name=f"run-{created_at}",
        status=status,
        created_at=created_at,
    )


def titles(groups) -> list[str]:
    return [g.title for g in groups]


def test_default_period_is_a_known_key():
    assert DEFAULT_PERIOD in PERIOD_KEYS


def test_days_read_relatively_then_absolutely():
    groups = group_by_period(
        [
            clip("a.MP4", captured_at="2026-08-06T09:00:00+00:00"),
            clip("b.MP4", captured_at="2026-08-05T09:00:00+00:00"),
            clip("c.MP4", captured_at="2026-08-02T09:00:00+00:00"),
            clip("d.MP4", captured_at="2026-07-12T09:00:00+00:00"),
        ],
        "day",
        today=TODAY,
    )
    assert titles(groups) == ["Today", "Yesterday", "Sunday", "12 July 2026"]
    assert [g.key for g in groups] == ["2026-08-06", "2026-08-05", "2026-08-02", "2026-07-12"]


def test_clips_shot_the_same_day_share_a_group():
    groups = group_by_period(
        [
            clip("a.MP4", captured_at="2026-08-06T09:00:00+00:00"),
            clip("b.MP4", captured_at="2026-08-06T16:00:00+00:00"),
        ],
        "day",
        today=TODAY,
    )
    assert len(groups) == 1
    assert [e.video.file_name for e in groups[0].entries] == ["a.MP4", "b.MP4"]


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("week", ["This week", "Last week", "Week of 6 July 2026"]),
        ("month", ["This month", "Last month", "June 2026"]),
        ("year", ["This year", "Last year", "2023"]),
    ],
)
def test_period_titles(period, expected):
    """Expected behaviour: each period names the two most recent buckets
    relatively and dates anything older."""
    stamps = {
        "week": ("2026-08-04", "2026-07-30", "2026-07-08"),
        "month": ("2026-08-01", "2026-07-02", "2026-06-03"),
        "year": ("2026-01-04", "2025-05-05", "2023-06-06"),
    }[period]
    groups = group_by_period(
        [clip(f"{s}.MP4", captured_at=f"{s}T09:00:00+00:00") for s in stamps],
        period,
        today=TODAY,
    )
    assert titles(groups) == expected


def test_all_period_is_one_group_newest_first():
    groups = group_by_period(
        [
            clip("old.MP4", captured_at="2020-01-01T09:00:00+00:00"),
            clip("new.MP4", captured_at="2026-08-06T09:00:00+00:00"),
        ],
        "all",
        today=TODAY,
    )
    assert [g.key for g in groups] == [ALL_KEY]
    assert [e.video.file_name for e in groups[0].entries] == ["new.MP4", "old.MP4"]


def test_mtime_fills_in_for_a_clip_with_no_capture_date():
    groups = group_by_period(
        [clip("a.MP4", mtime="2026-08-05T09:00:00+00:00")], "day", today=TODAY
    )
    assert titles(groups) == ["Yesterday"]


def test_undated_clips_trail_the_dated_ones():
    groups = group_by_period(
        [
            clip("undated.MP4"),
            clip("dated.MP4", captured_at="2020-01-01T09:00:00+00:00"),
        ],
        "day",
        today=TODAY,
    )
    assert groups[-1].key == UNDATED_KEY
    assert groups[-1].title == UNDATED_TITLE
    assert [e.video.file_name for e in groups[-1].entries] == ["undated.MP4"]


def test_unknown_period_is_rejected():
    with pytest.raises(ValueError, match="period must be one of"):
        group_by_period([], "fortnight", today=TODAY)


def test_spans_normalise_against_the_clip_length():
    passes = [make_pass(60.0, 90.0), make_pass(0.0, 30.0)]
    spans = timeline_spans(clip(duration_s=120.0, passes=passes))
    assert [(s.begin, s.end) for s in spans] == [(0.0, 0.25), (0.5, 0.75)]


def test_a_section_running_past_the_clip_end_is_clamped():
    spans = timeline_spans(clip(duration_s=100.0, passes=[make_pass(50.0, 400.0)]))
    assert (spans[0].begin, spans[0].end) == (0.5, 1.0)


def test_unknown_duration_gives_no_spans():
    assert timeline_spans(clip(duration_s=None, passes=[make_pass()])) == []
    assert timeline_spans(clip(duration_s=0.0, passes=[make_pass()])) == []


def test_span_status_comes_from_the_latest_run():
    pass_ = make_pass(0.0, 60.0)
    runs = [
        make_run(pass_, "failed", "2026-08-01T10:00:00+00:00"),
        make_run(pass_, "succeeded", "2026-08-02T10:00:00+00:00"),
    ]
    span = timeline_spans(clip(duration_s=60.0, passes=[pass_], runs=runs))[0]
    assert span.status == "succeeded"
    assert span.run_count == 2


def test_a_section_with_no_runs_is_ready():
    pass_ = make_pass()
    span = timeline_spans(clip(duration_s=60.0, passes=[pass_]))[0]
    assert (span.status, span.run_count) == ("ready", 0)


def test_pass_status_matches_the_latest_run_regardless_of_order():
    pass_ = make_pass()
    late = make_run(pass_, "succeeded", "2026-08-02T10:00:00+00:00")
    early = make_run(pass_, "failed", "2026-08-01T10:00:00+00:00")
    assert pass_status([late, early]) == "succeeded"
    assert pass_status([]) == "ready"


def names(entries) -> list[str]:
    return [e.video.file_name for e in entries]


def test_clips_sort_by_the_asked_column():
    shorter = clip("b.MP4", duration_s=30.0, size_bytes=200, captured_at="2026-08-06T09:00:00+00:00")
    longer = clip("a.MP4", duration_s=60.0, size_bytes=100, captured_at="2026-08-05T09:00:00+00:00")

    assert names(sort_clips([shorter, longer], SORT_NAME)) == ["a.MP4", "b.MP4"]
    assert names(sort_clips([shorter, longer], SORT_LENGTH)) == ["b.MP4", "a.MP4"]
    assert names(sort_clips([shorter, longer], SORT_SIZE)) == ["a.MP4", "b.MP4"]
    assert names(sort_clips([longer, shorter], SORT_RECORDED, descending=True)) == [
        "b.MP4",
        "a.MP4",
    ]


def test_a_clip_missing_the_fact_sinks_to_the_end_both_ways():
    known = clip("known.MP4", size_bytes=100)
    unknown = clip("unknown.MP4")

    for descending in (False, True):
        ordered = sort_clips([unknown, known], SORT_SIZE, descending=descending)
        assert names(ordered) == ["known.MP4", "unknown.MP4"]


def test_sorting_stays_within_the_groups():
    newer = DateGroup(key="new", title="Today", entries=[clip("z.MP4"), clip("a.MP4")])
    older = DateGroup(key="old", title="Yesterday", entries=[clip("m.MP4")])

    by_name = sort_groups([newer, older], SORT_NAME)

    assert [g.key for g in by_name] == ["new", "old"]
    assert names(by_name[0].entries) == ["a.MP4", "z.MP4"]
    assert names(by_name[1].entries) == ["m.MP4"]


def test_an_unknown_sort_column_is_refused():
    with pytest.raises(ValueError):
        sort_clips([clip()], "colour")
