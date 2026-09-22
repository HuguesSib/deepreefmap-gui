"""Videos: the footage, what has been cut from it, and whether it is still there."""

from pathlib import Path

from _factories import make_transect, write_test_mp4

from deepreefmap_gui.core.icons import DEFAULT_INK
from deepreefmap_gui.core.theme import DISABLED_FG, ERROR
from deepreefmap_gui.runs.section_detail import (
    ARCHIVE_RUN_TOOLTIP,
    ARCHIVE_UNFINISHED,
    ARCHIVING,
)
from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS
from deepreefmap_gui.survey.catalogue import (
    VIDEO_FAILED,
    VIDEO_PENDING,
    VIDEO_PROCESSED,
    VIDEO_UNPROCESSED,
)
from deepreefmap_gui.survey.models import RunRecord, TransectPass, VideoAsset


def _seed(store, name: str, *, passes: int = 0, statuses: tuple[str, ...] = ()) -> VideoAsset:
    video = store.upsert_video(VideoAsset(file_name=name, path=f"/data/{name}", hash=name * 4, duration_s=60.0))
    transect = make_transect(name.replace(".", "_"))
    store.add_transect(transect)
    made = []
    for index in range(passes):
        pass_ = TransectPass(transect_id=transect.id, video_id=video.id, begin_s=index * 10.0, end_s=index * 10 + 5)
        store.add_pass(pass_)
        made.append(pass_)
    for pass_, status in zip(made, statuses, strict=False):
        run = RunRecord(pass_id=pass_.id, run_dir_name=f"{name}_{status}", status=status)
        store.add_run(run)
    return video


def _seed_at(store, name: str, path: Path) -> VideoAsset:
    return store.upsert_video(VideoAsset(file_name=name, path=str(path), hash=name * 4, duration_s=60.0))


def show_videos(window) -> None:
    window._refresh_video_library()


def resolve_links(window) -> None:
    """Run the link scan's work inline, since the real one is on a worker."""
    from deepreefmap_gui.survey import catalogue

    window._apply_clip_link_states(catalogue.resolve_link_states(window._video_entries))


def select_clip(window, file_name: str) -> None:
    clip = next(c for c in window._video_entries if c.video.file_name == file_name)
    window._on_video_activated(str(clip.video.id))


def listed_names(window) -> list[str]:
    return [row.entry.video.file_name for row in window._video_list.rows().values()]


def test_videos_is_a_destination_of_its_own(window):
    """Scenario: clips used to be a grouping inside the page built for runs.

    Expected behaviour: footage has its own destination, and Browse no longer
    offers a video grouping, so a clip lives in exactly one place.
    """
    window._set_simple_section("videos")
    assert window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("videos")
    assert window._simple_nav_buttons["videos"].isChecked()
    assert "videos" not in window._data_facet_buttons


def test_every_imported_clip_is_listed(window):
    store = window._survey_store()
    _seed(store, "one.mp4")
    _seed(store, "two.mp4", passes=1)

    show_videos(window)
    assert sorted(listed_names(window)) == ["one.mp4", "two.mp4"]


def test_outcome_reads_what_is_still_owed(window):
    store = window._survey_store()
    _seed(store, "untouched.mp4")
    _seed(store, "part.mp4", passes=2, statuses=("succeeded",))
    _seed(store, "broken.mp4", passes=1, statuses=("failed",))
    _seed(store, "done.mp4", passes=1, statuses=("succeeded",))

    show_videos(window)
    outcomes = {c.video.file_name: c.outcome for c in window._video_entries}
    assert outcomes == {
        "untouched.mp4": VIDEO_UNPROCESSED,
        "part.mp4": VIDEO_PENDING,
        "broken.mp4": VIDEO_FAILED,
        "done.mp4": VIDEO_PROCESSED,
    }


def test_filters_and_search_narrow_the_list(window):
    store = window._survey_store()
    _seed(store, "alpha.mp4")
    _seed(store, "beta.mp4", passes=1, statuses=("succeeded",))

    show_videos(window)
    window._on_video_filter_changed("complete")
    assert listed_names(window) == ["beta.mp4"]

    window._on_video_filter_changed("all")
    window._video_search.setText("alph")
    assert listed_names(window) == ["alpha.mp4"]


def test_an_empty_library_says_so(window):
    show_videos(window)
    assert window._video_stack.currentIndex() == 1


def test_an_empty_library_keeps_the_pages_panels(window):
    """Expected behaviour: the page keeps its shape, columns and detail pane included."""
    show_videos(window)

    assert not window._video_header.isHidden()
    assert window._video_detail_stack.currentIndex() == 0
    assert not window._video_detail_stack.isHidden()


def test_clips_are_grouped_by_the_day_they_were_shot(window):
    store = window._survey_store()
    store.upsert_video(
        VideoAsset(
            file_name="may.mp4",
            path="/data/may.mp4",
            hash="a" * 32,
            captured_at="2023-05-17T08:27:16+00:00",
        )
    )
    store.upsert_video(
        VideoAsset(
            file_name="june.mp4",
            path="/data/june.mp4",
            hash="b" * 32,
            captured_at="2023-06-02T09:00:00+00:00",
        )
    )

    show_videos(window)
    from deepreefmap_gui.survey.video_groups import group_by_period

    groups = group_by_period(window._video_entries, window._video_period)
    assert [g.entries[0].video.file_name for g in groups] == ["june.mp4", "may.mp4"]


def test_the_grouping_period_is_remembered(window):
    show_videos(window)
    window._on_video_period_changed("month")

    assert window._video_period == "month"
    assert window._settings.value("video_group_period") == "month"


def _seed_shot(store, name: str, *, captured_at: str, size_bytes: int) -> VideoAsset:
    return store.upsert_video(
        VideoAsset(
            file_name=name,
            path=f"/data/{name}",
            hash=name * 4,
            duration_s=60.0,
            size_bytes=size_bytes,
            captured_at=captured_at,
        )
    )


def test_a_header_click_sorts_clips_within_their_date_groups(window):
    from deepreefmap_gui.survey.video_groups import SORT_NAME, SORT_SIZE

    store = window._survey_store()
    _seed_shot(store, "big.mp4", captured_at="2023-05-17T08:00:00+00:00", size_bytes=300)
    _seed_shot(store, "small.mp4", captured_at="2023-05-17T09:00:00+00:00", size_bytes=100)
    _seed_shot(store, "earlier.mp4", captured_at="2023-05-16T08:00:00+00:00", size_bytes=200)

    show_videos(window)
    window._on_video_period_changed("day")
    window._video_header.set_sort(SORT_NAME, False)
    window._video_header.sort_by(SORT_SIZE)

    # earlier.mp4 sits between the two by size, and stays last: it belongs to
    # the older group, and the sort works within the groups.
    assert listed_names(window) == ["small.mp4", "big.mp4", "earlier.mp4"]

    window._video_header.sort_by(SORT_SIZE)
    assert listed_names(window) == ["big.mp4", "small.mp4", "earlier.mp4"]


def test_the_sort_holds_through_a_library_refresh(window):
    from deepreefmap_gui.survey.video_groups import SORT_NAME, SORT_SIZE

    store = window._survey_store()
    _seed_shot(store, "b.mp4", captured_at="2023-05-17T08:00:00+00:00", size_bytes=100)
    _seed_shot(store, "a.mp4", captured_at="2023-05-17T09:00:00+00:00", size_bytes=300)
    _seed_shot(store, "c.mp4", captured_at="2023-05-17T10:00:00+00:00", size_bytes=200)

    show_videos(window)
    window._video_header.set_sort(SORT_SIZE, False)
    window._video_header.sort_by(SORT_NAME)
    assert listed_names(window) == ["a.mp4", "b.mp4", "c.mp4"]

    show_videos(window)
    assert listed_names(window) == ["a.mp4", "b.mp4", "c.mp4"]


def test_the_sort_choice_is_remembered(window, make_window):
    from deepreefmap_gui.survey.video_groups import SORT_SIZE

    show_videos(window)
    window._video_header.set_sort(SORT_SIZE, False)
    window._video_header.sort_by(SORT_SIZE)
    try:
        assert window._settings.value("video_sort_column") == "size"
        assert window._settings.value("video_sort_order") == "descending"

        second = make_window()
        assert second._video_sort_column == SORT_SIZE
        assert second._video_sort_descending is True
        assert second._video_header.column == SORT_SIZE
        assert second._video_header.descending is True
    finally:
        window._settings.remove("video_sort_column")
        window._settings.remove("video_sort_order")


def test_link_state_says_whether_the_footage_is_still_there(window, tmp_path):
    """Scenario: footage lives on a card that gets pulled between dives.

    Expected behaviour: the clip stays, and says it cannot be found rather than
    disappearing or claiming to be fine.
    """
    store = window._survey_store()
    present = tmp_path / "here.mp4"
    present.write_bytes(b"x" * 32)
    _seed_at(store, "here.mp4", present)
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")

    show_videos(window)
    resolve_links(window)
    states = {c.video.file_name: c.link_state for c in window._video_entries}
    assert states == {"here.mp4": "linked", "gone.mp4": "missing"}


def test_missing_footage_is_reported_in_the_header(window, tmp_path):
    store = window._survey_store()
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    show_videos(window)
    resolve_links(window)

    verdict = window._videos_verdict()
    assert verdict.state == "attention"
    assert "1 clip" in verdict.reason


def test_a_clip_added_from_its_new_home_keeps_its_sections(window, tmp_path):
    """Scenario: a card is copied to a second disk and the first one is gone.

    Expected behaviour: adding the file again matches it by checksum and
    repoints the clip already in the library, which is why the pane offers no
    relocate button of its own.
    """
    store = window._survey_store()
    was = tmp_path / "gone" / "GX010001.MP4"
    was.parent.mkdir()
    moved = tmp_path / "new_home" / "GX010001.MP4"
    moved.parent.mkdir()
    moved.write_bytes(b"the same footage" * 64)
    original = store.upsert_video(
        VideoAsset(
            file_name="GX010001.MP4",
            path=str(was),
            hash=VideoAsset.from_path(moved).hash,
            duration_s=60.0,
        )
    )
    pass_ = TransectPass(transect_id=None, video_id=original.id, begin_s=0.0, end_s=10.0)
    store.add_pass(pass_)

    window._on_videos_probed([(str(moved), (60.0, 30.0))])

    assert store.get_video(original.id).path == str(moved)
    assert [p.id for p in store.list_passes(video_id=original.id)] == [pass_.id]


def test_revealing_a_clip_selects_the_file_itself(window, tmp_path, monkeypatch):
    """The folder was never the point: a card holds hundreds of clips."""
    store = window._survey_store()
    clip = tmp_path / "here.mp4"
    clip.write_bytes(b"x" * 32)
    _seed_at(store, "here.mp4", clip)

    revealed = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.videos.reveal_in_file_manager",
        lambda path: revealed.append(path) or True,
    )
    show_videos(window)
    entry = next(c for c in window._video_entries if c.video.file_name == "here.mp4")
    window._on_video_reveal(str(entry.video.id))

    assert revealed == [clip]


def test_picking_a_clip_asks_again_whether_its_file_is_there(window, tmp_path, monkeypatch):
    """Scenario: a clip's drive was unplugged when the app opened, then plugged back in.

    Expected behaviour: the session-long cached answer is not the last word.
    Picking the row re-stats that one file, so the row stops claiming a clip is
    missing once it is not.
    """
    from deepreefmap_gui.survey import catalogue

    store = window._survey_store()
    clip = tmp_path / "back.mp4"
    _seed_at(store, "back.mp4", clip)
    show_videos(window)
    resolve_links(window)
    entry = next(c for c in window._video_entries if c.video.file_name == "back.mp4")
    assert entry.link_state == catalogue.LINK_MISSING

    clip.write_bytes(b"x" * 32)
    # Off the worker: the recheck's own thread reports through the same slot.
    monkeypatch.setattr(
        "deepreefmap_gui.runs.videos.threading.Thread",
        lambda target, **kwargs: type("Now", (), {"start": staticmethod(target)})(),
    )
    select_clip(window, "back.mp4")

    refreshed = next(c for c in window._video_entries if c.video.file_name == "back.mp4")
    assert refreshed.link_state == catalogue.LINK_LINKED


def test_hiding_a_clip_takes_it_out_of_the_list_without_touching_the_survey(window):
    """Scenario: a season's footage, and only today's dive matters.

    Expected behaviour: hiding is this machine's view of the library. The clip
    leaves the list, the database still has it, and Show hidden brings it back.
    """
    store = window._survey_store()
    _seed(store, "keep.mp4")
    hidden = _seed(store, "gone.mp4")
    show_videos(window)

    window._on_video_hide(str(hidden.id))

    assert listed_names(window) == ["keep.mp4"]
    assert store.get_video(hidden.id) is not None
    assert window._video_hidden_check.text() == "Show hidden (1)"
    assert not window._video_hidden_check.isHidden()

    window._video_hidden_check.setChecked(True)
    assert sorted(listed_names(window)) == ["gone.mp4", "keep.mp4"]

    # The same menu entry puts it back, so hiding is never a one-way door.
    window._on_video_hide(str(hidden.id))
    window._video_hidden_check.setChecked(False)
    assert sorted(listed_names(window)) == ["gone.mp4", "keep.mp4"]


def test_sections_nothing_was_made_from_can_be_swept_off_a_clip(window, monkeypatch):
    """Expected behaviour: the sweep spares any pass a run stands on."""
    monkeypatch.setattr("deepreefmap_gui.runs.videos.confirm", lambda *a, **k: True)
    store = window._survey_store()
    video = _seed(store, "swept.mp4", passes=3, statuses=("succeeded",))
    show_videos(window)

    window._on_video_delete_unused(str(video.id))

    kept = store.list_passes(video_id=video.id)
    assert len(kept) == 1
    assert store.runs_for_pass(kept[0].id)


def test_a_clip_with_runs_refuses_to_leave_the_library(window):
    """The runs record what this footage was, so the clip stays until they go."""
    store = window._survey_store()
    video = _seed(store, "kept.mp4", passes=1, statuses=("succeeded",))
    show_videos(window)

    window._on_video_delete(str(video.id))

    assert store.get_video(video.id) is not None
    assert "Results" in window._status_label.text()


def test_a_clip_nothing_was_made_from_goes_with_its_sections(window):
    store = window._survey_store()
    video = _seed(store, "dropped.mp4", passes=2)
    _seed(store, "kept.mp4")
    show_videos(window)

    window._on_video_delete(str(video.id))

    assert store.get_video(video.id) is None
    assert store.list_passes(video_id=video.id) == []
    assert listed_names(window) == ["kept.mp4"]
    assert "video file" in window._status_label.text()


def test_a_deleted_clip_stops_counting_as_hidden(window):
    """Expected behaviour: nothing else prunes the hidden list, so the delete does."""
    store = window._survey_store()
    video = _seed(store, "hidden.mp4")
    show_videos(window)
    window._on_video_hide(str(video.id))

    window._on_video_delete(str(video.id))

    assert str(video.id) not in window._hidden_clip_ids
    assert window._video_hidden_check.isHidden()


def test_a_new_section_is_cut_scrubbed_and_filed_but_not_carted(window, tmp_path, monkeypatch):
    """The whole reason the page exists: footage in, a filed pass out.

    Expected behaviour: the cart stays as the user left it. Cutting a pass
    and choosing to run it are two decisions, and only the row's cart control
    makes the second.
    """
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010099.MP4")
    _seed_at(store, "GX010099.MP4", clip_path)
    transect = make_transect("Reef A")
    store.add_transect(transect)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (5.0, 25.0),
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.transect_picker.TransectPickerDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.transect_picker.TransectPickerDialog.choice",
        lambda self: (transect.id, "forward"),
    )

    show_videos(window)
    resolve_links(window)
    select_clip(window, "GX010099.MP4")
    window._on_video_new_section()

    passes = store.list_passes()
    assert [(p.begin_s, p.end_s, p.transect_id) for p in passes] == [(5.0, 25.0, transect.id)]
    assert not window._pass_in_current_cart(passes[0].id)
    assert window._selected_pass_id == str(passes[0].id)

    # Cutting the same window again is how a clip ends up with two sections
    # over one swim, so it is refused and the existing one is shown instead.
    window._on_video_new_section()
    assert len(store.list_passes()) == 1
    assert "already has a pass" in window._status_label.text()
    assert window._selected_pass_id == str(passes[0].id)


# A section is a window the user chose, so a clip that cannot be scrubbed
# reports why instead of queueing itself whole.


def test_an_unchecked_clip_cuts_nothing_and_says_why(window):
    from types import SimpleNamespace

    from deepreefmap_gui.survey.catalogue import LINK_UNKNOWN

    store = window._survey_store()
    video = store.upsert_video(
        VideoAsset(
            file_name="GX010001.MP4",
            path="/data/GX010001.MP4",
            hash="ab" * 16,
            duration_s=120.0,
        )
    )
    clip = SimpleNamespace(video=video, link_state=LINK_UNKNOWN)
    window._new_section_from_clip(clip)
    assert store.list_passes() == []
    assert "not been checked" in window._status_label.text()


def test_a_clip_of_unknown_length_cuts_nothing_and_says_why(window):
    from types import SimpleNamespace

    from deepreefmap_gui.survey.catalogue import LINK_LINKED

    store = window._survey_store()
    video = store.upsert_video(VideoAsset(file_name="GX010001.MP4", path="/data/GX010001.MP4", hash="cd" * 16))
    clip = SimpleNamespace(video=video, link_state=LINK_LINKED)
    window._new_section_from_clip(clip)
    assert store.list_passes() == []
    assert "length is unknown" in window._status_label.text()


def test_a_missing_clip_cuts_nothing_and_says_why(window):
    from types import SimpleNamespace

    from deepreefmap_gui.survey.catalogue import LINK_MISSING

    store = window._survey_store()
    video = store.upsert_video(
        VideoAsset(
            file_name="GX010001.MP4",
            path="/gone/GX010001.MP4",
            hash="cd" * 16,
            duration_s=120.0,
        )
    )
    clip = SimpleNamespace(video=video, link_state=LINK_MISSING)
    window._new_section_from_clip(clip)
    assert store.list_passes() == []
    assert "file is missing" in window._status_label.text()


def _seed_run(store, pass_, status: str = "succeeded", batch=None):
    from deepreefmap_gui.survey.models import RunRecord

    run = RunRecord(
        pass_id=pass_.id,
        run_dir_name=f"run_{status}_{pass_.begin_s:g}",
        status=status,
        batch_id=batch.id if batch is not None else None,
    )
    store.add_run(run)
    return run


def _cut(store, video, begin=0.0, end=30.0, transect=None):
    from deepreefmap_gui.survey.models import TransectPass

    pass_ = TransectPass(
        transect_id=transect.id if transect is not None else None,
        video_id=video.id,
        begin_s=begin,
        end_s=end,
    )
    store.add_pass(pass_)
    return pass_


def test_a_section_shows_the_sessions_it_has_run_in(window):
    """Scenario: the clip pane used to read "Evan #1 · forwar…14 · interrupted",
    which is a cut and a run's outcome on one line.

    Expected behaviour: the cut is its own thing, and picking it says which
    sessions have run it.
    """
    from _factories import make_batch

    store = window._survey_store()
    video = _seed(store, "GX010050.MP4")
    pass_ = _cut(store, video)
    batch = make_batch(store, "2026-07-21")
    _seed_run(store, pass_, "succeeded", batch)

    show_videos(window)
    window._select_section(str(pass_.id))

    assert not window._section_detail.isHidden()
    assert window._section_detail.pass_ is not None
    assert window._section_detail.run_list.count() == 1
    assert "2026-07-21" in window._section_detail.run_rows()[0].text()


def test_a_cart_row_opens_its_section_here(window):
    """Scenario: the transect, direction or trim of a cart row is clicked.

    Expected behaviour: the Videos page comes forward with that pass picked
    and scrolled to, because this is where a pass is described.
    """
    store = window._survey_store()
    video = _seed(store, "GX010051.MP4")
    pass_ = _cut(store, video)
    window._refresh_data_manager()

    window._go_to_section("process")
    window._open_section_in_videos(pass_.id)

    assert window._current_section() == "videos"
    assert window._selected_pass_id == str(pass_.id)
    assert window._video_list.selected_section == str(pass_.id)


def test_the_section_card_files_its_actions_under_one_menu(window):
    """Scenario: four full-width buttons in a 260px pane truncated every label.

    Expected behaviour: the occasional actions go under More…, the same shape as
    Browse, and the cart is not among them: the pass's own row carries that
    control and shows its state on it.
    """
    store = window._survey_store()
    video = _seed(store, "GX010060.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    window._select_section(str(pass_.id))

    panel = window._section_detail
    assert not hasattr(panel, "cart_btn")
    labels = [a.text() for a in panel.more_btn.menu().actions() if not a.isSeparator()]
    assert labels == ["Rename pass…", "Adjust trim…", "Change transect…", "Set campaign…", "Delete pass"]


def test_the_filing_fact_opens_the_dialog_that_sets_it(window):
    """Transect and direction are one fact and one dialog, so the fact is the
    way in rather than a menu entry that repeats it."""
    store = window._survey_store()
    video = _seed(store, "GX010062.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    window._select_section(str(pass_.id))

    panel = window._section_detail
    # The window answers this signal by opening the picker itself, and what is
    # under test is that the fact reaches the signal at all.
    panel.reassign_requested.disconnect(window._on_section_reassign)
    asked = []
    panel.reassign_requested.connect(asked.append)
    panel.facts.link_activated.emit("filing")
    assert asked == [str(pass_.id)]


def test_menu_actions_act_on_the_shown_section(window):
    store = window._survey_store()
    video = _seed(store, "GX010061.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    window._select_section(str(pass_.id))

    panel = window._section_detail
    panel.retrim_requested.disconnect(window._on_section_retrim)
    fired = []
    panel.retrim_requested.connect(fired.append)
    panel.menu_actions["retrim"].trigger()
    assert fired == [str(pass_.id)]


def test_the_delete_gate_survives_the_move_into_the_menu(window):
    store = window._survey_store()
    video = _seed(store, "GX010062.MP4")
    kept = _cut(store, video, 0.0, 10.0)
    _seed_run(store, kept)
    free = _cut(store, video, 20.0, 30.0)

    show_videos(window)
    delete = window._section_detail.menu_actions["delete"]

    window._select_section(str(kept.id))
    assert not delete.isEnabled()
    assert "Results" in delete.toolTip()

    window._select_section(str(free.id))
    assert delete.isEnabled()
    assert "Browse" not in delete.toolTip()


def test_each_finished_run_row_carries_an_archive_icon(window):
    """Scenario: sending one run's outputs meant finding it in Browse.

    Expected behaviour: every finished run row wears the archive icon in the
    pass rows' style, and pressing one offers exactly that run."""
    store = window._survey_store()
    video = _seed(store, "GX010070.MP4")
    pass_ = _cut(store, video)
    store.add_run(
        RunRecord(
            pass_id=pass_.id,
            run_dir_name="older",
            status="succeeded",
            created_at="2026-07-01T10:00:00+00:00",
        )
    )
    newest = RunRecord(
        pass_id=pass_.id,
        run_dir_name="newer",
        status="succeeded",
        created_at="2026-08-01T10:00:00+00:00",
    )
    store.add_run(newest)

    show_videos(window)
    window._select_section(str(pass_.id))

    panel = window._section_detail
    rows = panel.run_rows()
    assert [row.run.run_dir_name for row in rows] == ["newer", "older"]
    assert all(row.archivable and not row.archive_btn.icon().isNull() for row in rows)
    assert rows[0].archive_btn.toolTip() == ARCHIVE_RUN_TOOLTIP
    panel.archive_run_requested.disconnect(window._archive_run)
    asked = []
    panel.archive_run_requested.connect(asked.append)
    rows[0].archive_btn.click()
    assert asked == [newest.id]


def _icon_ink(icon) -> str:
    """The colour the icon's glyph is drawn in, read off its centre-ish pixels."""
    from PySide6.QtGui import QColor

    image = icon.pixmap(16, 16).toImage()
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if colour.alpha() > 200:
                return QColor(colour.red(), colour.green(), colour.blue()).name()
    return ""


class _ArchiveRegistry:
    """Answers the archive handshake with "already held" and refuses a probe."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def archive_initiate(self, payload):
        return {"object_id": "o-1", "status": "complete"}

    def archive_complete(self, object_id, parts):
        return {"object_id": object_id, "status": "uploaded"}


def _settle(qapp, ready, timeout=5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if ready():
            return True
        time.sleep(0.01)
    return False


def test_an_archive_press_stays_on_the_row_that_made_it(window, qapp, monkeypatch):
    """Scenario: the archive icon on a run row is pressed.

    Expected behaviour: the page does not change; the row shows the upload in
    progress, then the tick once the registry has the outputs."""
    from deepreefmap_gui.sync import archive_client
    from deepreefmap_gui.sync import credentials

    store = window._survey_store()
    video = _seed(store, "GX010073.MP4")
    pass_ = _cut(store, video)
    run = _seed_run(store, pass_)
    run_dir = store.path.parent / run.run_dir_name
    run_dir.mkdir()
    (run_dir / "cloud.ply").write_bytes(b"ply")
    credentials.save("https://reef.example.org", "drmd_" + "0" * 16 + "_" + "1" * 64)
    monkeypatch.setattr(archive_client, "ArchiveClient", _ArchiveRegistry)

    show_videos(window)
    window._select_section(str(pass_.id))
    before = window._current_section()
    row = window._section_detail.run_rows()[0]
    row.archive_btn.click()

    assert window._current_section() == before
    assert window._server_archiving
    assert row.archive_btn.toolTip() == ARCHIVING
    assert _settle(qapp, lambda: not window._server_archiving)
    # The pass pane is rebuilt on the way, so the row is read afresh.
    row = window._section_detail.run_rows()[0]
    assert "Outputs on server" in row.archive_btn.toolTip()


def test_no_archive_control_is_offered_without_a_registry(window):
    """Scenario: a laptop that has never been connected to a registry.

    Expected behaviour: nothing anywhere offers to send footage or outputs to a
    server, on the clip rows, in either detail pane, or in the run card's menu.
    """
    store = window._survey_store()
    video = _seed(store, "GX010080.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)

    show_videos(window)
    window._select_section(str(pass_.id))

    row = window._video_list.rows()[str(video.id)]
    assert not row.archive_btn.isVisibleTo(row)
    assert not window._video_detail.archive_btn.isVisibleTo(window._video_detail)
    run_row = window._section_detail.run_rows()[0]
    assert not run_row.archive_btn.isVisibleTo(run_row)
    assert not window._run_detail.menu_actions["archive"].isVisible()


def test_connecting_a_registry_offers_them_all(window):
    """The other half: enrolling puts the same controls back, without the pane
    being rebuilt or the page left."""
    from deepreefmap_gui.sync import credentials

    store = window._survey_store()
    video = _seed(store, "GX010081.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)
    show_videos(window)
    window._select_section(str(pass_.id))

    credentials.save("https://reef.example.org", "drmd_" + "0" * 16 + "_" + "1" * 64)
    window._refresh_archive_affordances()

    row = window._video_list.rows()[str(video.id)]
    assert any(action.text() == "Send this clip to the server" for action in row.menu().actions())
    assert window._video_detail.archive_btn.isVisibleTo(window._video_detail)
    run_row = window._section_detail.run_rows()[0]
    assert run_row.archive_btn.isVisibleTo(run_row)
    assert window._run_detail.menu_actions["archive"].isVisible()


def test_a_pass_shown_after_connecting_carries_the_button(window):
    """A rebuilt row must not arrive without the button the window has already
    said there is a server for."""
    from deepreefmap_gui.sync import credentials

    store = window._survey_store()
    video = _seed(store, "GX010082.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)
    credentials.save("https://reef.example.org", "drmd_" + "0" * 16 + "_" + "1" * 64)

    show_videos(window)
    window._select_section(str(pass_.id))

    run_row = window._section_detail.run_rows()[0]
    assert run_row.archive_btn.isVisibleTo(run_row)


def test_a_run_whose_outputs_are_gone_says_so_on_its_row(window, qapp, monkeypatch):
    from deepreefmap_gui.sync import client as client_mod
    from deepreefmap_gui.sync import credentials

    store = window._survey_store()
    video = _seed(store, "GX010074.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)
    credentials.save("https://reef.example.org", "drmd_" + "0" * 16 + "_" + "1" * 64)
    monkeypatch.setattr(client_mod, "SyncClient", _ArchiveRegistry)

    show_videos(window)
    window._select_section(str(pass_.id))
    row = window._section_detail.run_rows()[0]
    row.archive_btn.click()
    assert _settle(qapp, lambda: not window._server_archiving)

    row = window._section_detail.run_rows()[0]
    assert "output directory is gone" in row.archive_btn.toolTip()
    assert _icon_ink(row.archive_btn.icon()) == ERROR.lower()


def test_the_archive_icon_is_dead_only_where_it_cannot_act(window):
    store = window._survey_store()
    video = _seed(store, "GX010075.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_, "failed")
    _seed_run(store, pass_)

    show_videos(window)
    window._select_section(str(pass_.id))

    by_status = {row.run.status: row.archive_btn for row in window._section_detail.run_rows()}
    dead = by_status["failed"]
    live = by_status["succeeded"]
    assert dead.property("dead") == "true" and dead.isEnabled()
    assert _icon_ink(dead.icon()) == DISABLED_FG.lower()
    assert live.property("dead") == "false"
    assert _icon_ink(live.icon()) == DEFAULT_INK.lower()


def test_an_unfinished_run_row_shows_a_dead_archive_icon(window):
    """A run that did not finish left nothing to send, but the icon stays put and
    says so: an icon that vanishes reads as a feature that is missing."""
    store = window._survey_store()
    video = _seed(store, "GX010071.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_, "failed")

    show_videos(window)
    window._select_section(str(pass_.id))

    panel = window._section_detail
    rows = panel.run_rows()
    assert len(rows) == 1
    assert not rows[0].archivable
    assert rows[0].archive_btn.toolTip() == ARCHIVE_UNFINISHED
    panel.archive_run_requested.disconnect(window._archive_run)
    asked = []
    panel.archive_run_requested.connect(asked.append)
    rows[0].archive_btn.click()
    assert asked == []


def test_the_probe_dresses_the_run_rows_archive_icon(window):
    from deepreefmap_gui.sync.archive import ArchiveStates

    store = window._survey_store()
    video = _seed(store, "GX010072.MP4")
    pass_ = _cut(store, video)
    run = _seed_run(store, pass_)

    show_videos(window)
    window._select_section(str(pass_.id))
    row = window._section_detail.run_rows()[0]
    assert row.archive_btn.toolTip() == ARCHIVE_RUN_TOOLTIP

    window._apply_archive_states(ArchiveStates(runs={str(run.id): "archived"}))
    assert "Outputs on server" in row.archive_btn.toolTip()

    window._apply_archive_states(ArchiveStates(runs={str(run.id): "failed"}))
    assert "could not verify" in row.archive_btn.toolTip()

    window._apply_archive_states(None)
    assert row.archive_btn.toolTip() == ARCHIVE_RUN_TOOLTIP


def test_the_detail_pane_takes_its_share_of_the_page(window, qapp):
    """Scenario: the pane sat at its 260px floor however wide the window got.

    Expected behaviour: it gets its declared share of the splitter, floored at
    the minimum width.
    """
    from deepreefmap_gui.runs.videos import _DETAIL_MIN_WIDTH, _DETAIL_SHARE

    window._set_simple_section("videos")
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()

    total = window._video_split.width()
    expected = max(_DETAIL_MIN_WIDTH, int(total * _DETAIL_SHARE))
    assert window._video_split.sizes()[1] == expected


def test_picking_a_section_keeps_its_clip_on_screen(window):
    """The cut list stays visible beside the runs, which is the drill-down."""
    store = window._survey_store()
    video = _seed(store, "GX010051.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    window._select_section(str(pass_.id))

    assert not window._video_detail.isHidden()
    assert window._video_detail.entry.video.file_name == "GX010051.MP4"


def test_a_section_with_runs_refuses_to_be_deleted(window):
    """It records what those runs processed, so it cannot go out from under them."""
    store = window._survey_store()
    video = _seed(store, "GX010052.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)

    show_videos(window)
    window._on_section_delete(str(pass_.id))

    assert store.get_pass(pass_.id) is not None
    assert "Results" in window._status_label.text()


def test_a_section_nothing_was_made_from_can_be_deleted(window, monkeypatch):
    store = window._survey_store()
    video = _seed(store, "GX010053.MP4")
    pass_ = _cut(store, video)
    monkeypatch.setattr("deepreefmap_gui.runs.videos.confirm", lambda *a, **k: True)

    show_videos(window)
    window._on_section_delete(str(pass_.id))

    assert store.get_pass(pass_.id) is None


def test_retrimming_writes_the_new_window_back(window, tmp_path, monkeypatch):
    """The trim is metadata, so it stays editable after a pass has been run."""
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010054.MP4")
    video = _seed_at(store, "GX010054.MP4", clip_path)
    pass_ = _cut(store, video, 0.0, 30.0)
    _seed_run(store, pass_)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (4.0, 9.0),
    )
    show_videos(window)
    resolve_links(window)
    window._on_section_retrim(str(pass_.id))

    moved = store.get_pass(pass_.id)
    assert (moved.begin_s, moved.end_s) == (4.0, 9.0)


def test_reassigning_moves_the_section_to_another_transect(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    video = _seed(store, "GX010055.MP4")
    pass_ = _cut(store, video)
    other = make_transect("Reef B")
    store.add_transect(other)

    monkeypatch.setattr(
        "deepreefmap_gui.simple.transect_picker.TransectPickerDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.transect_picker.TransectPickerDialog.choice",
        lambda self: (other.id, "reverse"),
    )
    show_videos(window)
    window._on_section_reassign(str(pass_.id))

    moved = store.get_pass(pass_.id)
    assert moved.transect_id == other.id
    assert moved.direction == "reverse"


def test_a_clip_that_is_there_offers_to_be_cut(window, tmp_path):
    store = window._survey_store()
    _seed_at(store, "here.mp4", write_test_mp4(tmp_path / "here.mp4"))

    show_videos(window)
    resolve_links(window)
    select_clip(window, "here.mp4")

    panel = window._video_detail
    assert not panel.unavailable.isVisibleTo(panel)
    assert panel.queue_btn.isEnabled()


def test_the_video_list_keeps_actionable_pass_rows(window):
    """Scenario: the pane's passes were text in a list, actionable only by
    right click.

    Expected behaviour: one row per pass, each carrying the transect chip and
    the three buttons the wide list's rows carry.
    """
    store = window._survey_store()
    video = _seed(store, "GX010070.MP4")
    first = _cut(store, video, 0.0, 10.0)
    second = _cut(store, video, 20.0, 30.0)

    show_videos(window)
    select_clip(window, "GX010070.MP4")

    rows = window._video_list.sections()
    assert set(rows) == {str(first.id), str(second.id)}
    row = rows[str(first.id)]
    assert row.transect_chip.full_text == "Set transect"
    assert row.cart_btn.isEnabled() and row.trim_btn.isEnabled() and row.delete_btn.isEnabled()


def test_the_pane_rows_reach_the_pages_handlers(window, monkeypatch):
    store = window._survey_store()
    video = _seed(store, "GX010071.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    select_clip(window, "GX010071.MP4")
    row = window._video_list.sections()[str(pass_.id)]

    seen = []
    for name in ("_on_section_retrim", "_on_section_reassign", "_on_section_delete"):
        monkeypatch.setattr(window, name, lambda pass_id, name=name: seen.append((name, pass_id)))
    # Bound after the connection was made, so the row is re-wired to the patches.
    panel = row
    panel.retrim_requested.disconnect()
    panel.reassign_requested.disconnect()
    panel.delete_requested.disconnect()
    panel.retrim_requested.connect(window._on_section_retrim)
    panel.reassign_requested.connect(window._on_section_reassign)
    panel.delete_requested.connect(window._on_section_delete)

    row.trim_btn.click()
    row.transect_chip.click()
    row.delete_btn.click()
    assert seen == [
        ("_on_section_retrim", str(pass_.id)),
        ("_on_section_reassign", str(pass_.id)),
        ("_on_section_delete", str(pass_.id)),
    ]

    row.cart_btn.click()
    assert window._pass_in_current_cart(pass_.id)


def test_the_card_shows_a_missing_file_and_offers_its_folder(window, tmp_path):
    """Scenario: the pane said "(not found)" in a path that elides, and the trim
    button looked live until the status bar refused it.

    Expected behaviour: the broken link is beside the clip's name, clickable,
    and the pass's trim button is marked in the meantime.
    """
    store = window._survey_store()
    video = _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    pass_ = _cut(store, video)
    revealed = []
    show_videos(window)
    resolve_links(window)
    select_clip(window, "gone.mp4")

    panel = window._video_detail
    assert not panel.link_btn.icon().isNull()
    assert "Not found" in panel.link_btn.toolTip()
    assert panel.unavailable.isVisibleTo(panel)
    # Cutting and trimming both decode the file, so both are marked in red
    # rather than greyed out, which would hide the reason with the tooltip.
    assert not panel.queue_btn.isEnabled()
    row = window._video_list.sections()[str(pass_.id)]
    assert "cannot be found" in row.trim_btn.toolTip()

    panel.reveal_requested.connect(revealed.append)
    panel.link_btn.click()
    assert revealed == [str(video.id)]


def test_a_section_with_no_footage_cannot_be_carted_or_run(window, tmp_path):
    """Scenario: a pass whose clip is on an unplugged drive went into the
    cart, and the session stopped on it after everything before it had run.

    Expected behaviour: the cart refuses it, and a cart that already holds one
    blocks the run rather than failing partway through it.
    """
    store = window._survey_store()
    video = _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    pass_ = _cut(store, video)

    show_videos(window)
    resolve_links(window)
    window._on_video_pass_to_cart(str(pass_.id))

    assert not window._pass_in_current_cart(pass_.id)
    assert "cannot be found" in window._status_label.text()

    # Carted while the drive was plugged in, then unplugged: the gate is what
    # stops the session, since the cart cannot un-add itself.
    window._add_pass_to_cart(pass_.id)
    assert window._survey_gate.state == "blocked"
    assert "cannot be found" in window._survey_gate.reason
    assert not window._survey_start_btn.isEnabled()


def _cart_marks(window, pass_id: str) -> tuple[bool, bool]:
    """Whether the wide row and the pane's row each say this is in the cart."""
    wide = window._video_list.sections()[pass_id]
    window._select_section(pass_id)
    pane = window._section_detail
    from deepreefmap_gui.runs.video_rows import IN_CART_TOOLTIP

    return wide.cart_btn.toolTip() == IN_CART_TOOLTIP, pane.queue_btn.text() == "Remove from queue"


def test_list_and_inspector_agree_on_queue_membership(window):
    """Scenario: a row went on reading "In cart" after the cart was cleared on
    the Process page, and the two lists disagreed with each other.

    Expected behaviour: cart membership is a live view in both, because every
    cart change repaints the page.
    """
    store = window._survey_store()
    video = _seed(store, "GX010080.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    select_clip(window, "GX010080.MP4")
    window._video_list.expand(str(video.id))
    assert _cart_marks(window, str(pass_.id)) == (False, False)

    window._on_video_pass_to_cart(str(pass_.id))
    assert _cart_marks(window, str(pass_.id)) == (True, True)
    assert "Added" in window._status_label.text()

    # Clicking the same control again is how a section comes back out.
    window._on_video_pass_to_cart(str(pass_.id))
    assert _cart_marks(window, str(pass_.id)) == (False, False)
    assert "out of the queue" in window._status_label.text()

    window._on_video_pass_to_cart(str(pass_.id))
    cart = store.current_cart()
    store.remove_batch_item(cart.id, pass_.id)
    window._refresh_survey_batch_tab()
    assert _cart_marks(window, str(pass_.id)) == (False, False)


def test_picking_a_section_links_the_row_and_inspector(window):
    """Scenario: the pane filled itself down a second path that neither
    highlighted the row nor read the cart, so picking a pass lit it in the
    list and left the pane's copy of it looking untouched.

    Expected behaviour: one filler, so the two cannot disagree.
    """
    store = window._survey_store()
    video = _seed(store, "GX010090.MP4")
    first = _cut(store, video, 0.0, 10.0)
    second = _cut(store, video, 20.0, 30.0)
    window._add_pass_to_cart(first.id)

    show_videos(window)
    window._select_section(str(first.id))

    pane = window._video_list.sections()
    assert pane[str(first.id)].property("selected") is True
    assert pane[str(second.id)].property("selected") is False
    assert window._video_list.selected_section == str(first.id)
    assert _cart_marks(window, str(first.id)) == (True, True)


def test_a_sections_transect_can_be_opened_on_the_transects_page(window):
    store = window._survey_store()
    video = _seed(store, "GX010072.MP4")
    transect = make_transect("Reef C")
    store.add_transect(transect)
    pass_ = _cut(store, video, transect=transect)

    show_videos(window)
    select_clip(window, "GX010072.MP4")
    row = window._video_list.sections()[str(pass_.id)]
    action = next(a for a in row.menu().actions() if a.text() == "Show on the Transects page")
    action.trigger()

    assert window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("transects")
    assert window._selected_transect_id() == transect.id


def test_dropping_a_clip_on_the_list_imports_it(window, tmp_path, monkeypatch):
    """Nothing else on the page advertises this, so it had better work."""
    clip = write_test_mp4(tmp_path / "GX010056.MP4")
    added = []
    monkeypatch.setattr(window, "_add_video_paths", added.extend)

    window._handle_data_drop([clip])
    assert added == [str(clip)]


def test_a_fresh_install_accepts_dropped_clips(window, tmp_path, monkeypatch):
    """The empty state says "Drop clips here", and used not to mean it.

    The drop handler was attached to the clip list, which is hidden behind the
    empty state while the library is empty. Qt looks for a drop target by walking
    up from the widget under the cursor and never descends into a hidden sibling,
    so the one install being told to drop footage in was the one that could not.
    """
    window._rebuild_video_list()
    shown = window._video_stack.currentWidget()
    assert shown is not window._video_list  # the empty state is what is on screen

    # Qt starts at the widget under the cursor and walks up until something
    # accepts drops, so at least one ancestor of what is on screen has to. The
    # handler is the window's, reached through installEventFilter.
    target = shown
    while target is not None and not target.acceptDrops():
        target = target.parentWidget()

    assert target is not None, "nothing above the empty state accepts a dropped clip"
    assert target.isVisible() or target is window._video_stack


def test_leaving_for_the_transects_page_keeps_the_section(window, tmp_path, monkeypatch):
    """The arrow's tooltip promises the pass is kept, unfiled.

    Written against the caller rather than the dialog: the dialog sets the flag
    correctly and always did, and a test that only checked the dialog passed
    while the pass was being thrown away.
    """
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010042.MP4")
    _seed_at(store, "GX010042.MP4", clip_path)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (11.0, 47.0),
    )

    def leave_for_page(self):
        # What pressing the arrow does: mark the exit and reject.
        self.left_for_page = True
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("deepreefmap_gui.simple.transect_picker.TransectPickerDialog.exec", leave_for_page)

    show_videos(window)
    resolve_links(window)
    select_clip(window, "GX010042.MP4")
    window._on_video_new_section()

    passes = store.list_passes()
    assert [(p.begin_s, p.end_s, p.transect_id) for p in passes] == [(11.0, 47.0, None)]


def test_cancelling_the_picker_still_cuts_nothing(window, tmp_path, monkeypatch):
    """Keeping the pass on the way out must not turn Cancel into a save."""
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010043.MP4")
    _seed_at(store, "GX010043.MP4", clip_path)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (1.0, 9.0),
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.transect_picker.TransectPickerDialog.exec",
        lambda self: QDialog.DialogCode.Rejected,
    )

    show_videos(window)
    resolve_links(window)
    select_clip(window, "GX010043.MP4")
    window._on_video_new_section()

    assert store.list_passes() == []


# --- picking clips, and the two bulk actions under the list ------------------


def pick_clips(window, *file_names: str) -> None:
    """Pick clips the way the list is picked: a click, then ctrl-clicks."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    for index, name in enumerate(file_names):
        clip = next(c for c in window._video_entries if c.video.file_name == name)
        row = window._video_list.rows()[str(clip.video.id)]
        row.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(1.0, 1.0),
                QPointF(1.0, 1.0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier if index == 0 else Qt.KeyboardModifier.ControlModifier,
            )
        )


def test_the_delete_button_says_how_many_clips_it_would_take(window):
    store = window._survey_store()
    _seed(store, "one.mp4")
    _seed(store, "two.mp4")
    show_videos(window)

    assert window._video_delete_btn.text() == "Delete"
    assert not window._video_delete_btn.isEnabled()

    pick_clips(window, "one.mp4")
    assert window._video_delete_btn.text() == "Delete clip"
    assert window._video_delete_btn.isEnabled()

    pick_clips(window, "one.mp4", "two.mp4")
    assert window._video_delete_btn.text() == "Delete all 2"


def test_one_press_arms_the_bulk_delete_and_takes_nothing(window):
    store = window._survey_store()
    video = _seed(store, "one.mp4")
    show_videos(window)
    pick_clips(window, "one.mp4")

    window._video_delete_btn.click()

    assert window._video_delete_btn.text() == "Click again to delete 1"
    assert store.get_video(video.id) is not None


def test_the_second_press_deletes_every_picked_clip(window):
    store = window._survey_store()
    first = _seed(store, "one.mp4")
    second = _seed(store, "two.mp4")
    _seed(store, "three.mp4")
    show_videos(window)
    pick_clips(window, "one.mp4", "two.mp4")

    window._video_delete_btn.click()
    window._video_delete_btn.click()

    assert store.get_video(first.id) is None
    assert store.get_video(second.id) is None
    assert listed_names(window) == ["three.mp4"]
    assert window._video_delete_btn.text() == "Delete"


def test_changing_the_pick_disarms_the_bulk_delete(window):
    """The second press must never land on a set nobody armed."""
    store = window._survey_store()
    video = _seed(store, "one.mp4")
    _seed(store, "two.mp4")
    show_videos(window)
    pick_clips(window, "one.mp4")
    window._video_delete_btn.click()

    pick_clips(window, "two.mp4")
    window._video_delete_btn.click()

    assert store.get_video(video.id) is not None
    assert window._video_delete_btn.text() == "Click again to delete 1"


def test_a_mixed_pick_deletes_the_clean_clips_and_says_what_it_kept(window):
    store = window._survey_store()
    clean = _seed(store, "clean.mp4", passes=1)
    used = _seed(store, "used.mp4", passes=1, statuses=("succeeded",))
    show_videos(window)
    pick_clips(window, "clean.mp4", "used.mp4")

    window._video_delete_btn.click()
    window._video_delete_btn.click()

    assert store.get_video(clean.id) is None
    assert store.get_video(used.id) is not None
    assert "1 clip kept" in window._status_label.text()
    assert "Results" in window._status_label.text()


def test_the_sweep_takes_the_clips_nothing_was_cut_from(window):
    store = window._survey_store()
    orphan = _seed(store, "orphan.mp4")
    cut = _seed(store, "cut.mp4", passes=1)
    show_videos(window)

    assert window._video_clear_btn.text() == "Remove 1 clip with no passes"
    window._video_clear_btn.click()
    window._video_clear_btn.click()

    assert store.get_video(orphan.id) is None
    assert store.get_video(cut.id) is not None


def test_a_clip_with_sections_and_no_runs_is_never_swept(window):
    """Re-importing the file mints a fresh clip but restores none of its trims."""
    store = window._survey_store()
    video = _seed(store, "trimmed.mp4", passes=2)
    show_videos(window)

    assert window._video_clear_btn.text() == "Remove clips with no passes"
    assert not window._video_clear_btn.isEnabled()
    assert store.get_video(video.id) is not None


def test_the_sweep_counts_only_what_is_on_screen(window):
    store = window._survey_store()
    _seed(store, "alpha.mp4")
    _seed(store, "beta.mp4")
    show_videos(window)
    assert window._video_clear_btn.text() == "Remove 2 clips with no passes"

    window._video_search.setText("alph")
    assert window._video_clear_btn.text() == "Remove 1 clip with no passes"

    window._video_search.setText("")
    window._on_video_filter_changed("complete")
    assert not window._video_clear_btn.isEnabled()


def test_the_sweep_leaves_a_hidden_clip_where_it_is(window):
    store = window._survey_store()
    hidden = _seed(store, "hidden.mp4")
    _seed(store, "shown.mp4")
    show_videos(window)
    window._on_video_hide(str(hidden.id))

    assert window._video_clear_btn.text() == "Remove 1 clip with no passes"
    window._video_clear_btn.click()
    window._video_clear_btn.click()

    assert store.get_video(hidden.id) is not None
    assert listed_names(window) == []


def test_clicking_video_reveals_passes_for_queueing(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    store = window._survey_store()
    video = _seed_at(store, "here.mp4", write_test_mp4(tmp_path / "here.mp4"))
    pass_ = _cut(store, video)
    show_videos(window)
    resolve_links(window)
    row = window._video_list.rows()[str(video.id)]
    assert not row.expanded
    QTest.mouseClick(row._name, Qt.MouseButton.LeftButton)
    assert row.expanded
    assert window._video_detail.entry.video.id == video.id
    assert window._video_inspector.currentIndex() == 0
    section = window._video_list.sections()[str(pass_.id)]
    assert not section.isHidden()
    section.cart_btn.click()
    assert window._pass_in_current_cart(str(pass_.id))
    window._video_list.sections()[str(pass_.id)].cart_btn.click()
    assert not window._pass_in_current_cart(str(pass_.id))


def test_pass_inspector_returns_to_video_with_passes_on_left(window):
    store = window._survey_store()
    video = _seed(store, "here.mp4")
    pass_ = _cut(store, video)
    show_videos(window)
    select_clip(window, "here.mp4")
    window._video_list.sections()[str(pass_.id)].activated.emit(str(pass_.id))
    assert window._video_inspector.currentWidget() is window._section_detail
    window._section_detail.back_btn.click()
    assert window._selected_pass_id is None
    assert window._video_inspector.currentIndex() == 0
    assert window._video_detail.entry.video.id == video.id
    assert not window._video_list.sections()[str(pass_.id)].isHidden()


def test_selecting_another_video_collapses_previous_passes(window):
    store = window._survey_store()
    first = _seed(store, "first.mp4")
    second = _seed(store, "second.mp4")
    first_pass = _cut(store, first)
    second_pass = _cut(store, second)
    show_videos(window)
    select_clip(window, "first.mp4")
    assert not window._video_list.sections()[str(first_pass.id)].isHidden()
    select_clip(window, "second.mp4")
    rows = window._video_list.rows()
    assert not rows[str(first.id)].expanded
    assert rows[str(second.id)].expanded
    assert window._video_list.sections()[str(first_pass.id)].isHidden()
    assert not window._video_list.sections()[str(second_pass.id)].isHidden()
    window._refresh_video_library()
    assert window._video_list.sections()[str(first_pass.id)].isHidden()
    assert not window._video_list.sections()[str(second_pass.id)].isHidden()
