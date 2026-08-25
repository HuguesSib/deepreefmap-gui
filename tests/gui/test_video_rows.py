"""The Videos page's row widgets: the pass strip, a clip row, a pass row,
and the list they sit in."""

from __future__ import annotations

import uuid

import pytest
from _factories import make_video

from deepreefmap_gui.core.theme import PRIMARY, SPACE_SM
from deepreefmap_gui.runs.video_rows import (
    CART_UNLINKED_TOOLTIP,
    DELETE_BLOCKED_TOOLTIP,
    DELETE_CLIP_ARMED_TOOLTIP,
    DELETE_CLIP_BLOCKED_TOOLTIP,
    DELETE_CLIP_TOOLTIP,
    DROP_HINT,
    GRAVITY_UNKNOWN_TOOLTIP,
    IN_CART_TOOLTIP,
    KEEPS_FILE_NOTE,
    MENU_ADD_TO_CART,
    MENU_DELETE,
    MENU_DELETE_CLIP,
    MENU_DELETE_UNUSED,
    MENU_HIDE,
    MENU_OPEN_TRANSECT,
    MENU_OPEN_TRANSECTS_PAGE,
    MENU_REASSIGN,
    MENU_RETRIM,
    MENU_UNHIDE,
    NO_SECTIONS_TOOLTIP,
    RETRIM_TOOLTIP,
    SET_TRANSECT,
    SORT_ASC_GLYPH,
    SORT_DESC_GLYPH,
    TRIM_UNLINKED_TOOLTIP,
    UNKNOWN_LENGTH_TOOLTIP,
    SectionList,
    SectionRow,
    SectionStrip,
    VideoLibraryList,
    VideoListHeader,
    VideoRow,
    capture_label,
)
from deepreefmap_gui.survey.catalogue import LINK_LINKED, VideoLibraryEntry
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.video_groups import (
    SORT_GRAVITY,
    SORT_NAME,
    SORT_SIZE,
    DateGroup,
    sort_clips,
    sort_groups,
    timeline_spans,
)
from deepreefmap_gui.survey.video_probe import NO, SOURCE_CONTAINER, SOURCE_MTIME, UNKNOWN, YES

STRIP_WIDTH = 400

pytestmark = pytest.mark.usefixtures("qapp")


def make_entry(*, windows=((0.0, 30.0),), runs_per_pass=(), duration_s=120.0, asset=None, **video):
    """One clip with a pass per window, and however many runs each has had.

    ``asset`` reuses an existing clip, so a later entry can stand for the same
    footage after another pass has been cut from it.
    """
    asset = asset if asset is not None else make_video(duration_s=duration_s, **video)
    passes = [
        TransectPass(transect_id=None, video_id=asset.id, begin_s=begin, end_s=end)
        for begin, end in windows
    ]
    runs = []
    # Not strict: a clip's later sections may have had no runs at all.
    for pass_, statuses in zip(passes, runs_per_pass, strict=False):
        runs.extend(
            RunRecord(pass_id=pass_.id, run_dir_name=f"run{n}", status=status)
            for n, status in enumerate(statuses)
        )
    return VideoLibraryEntry(
        video=asset,
        pass_count=len(passes),
        run_count=len(runs),
        passes=passes,
        runs=runs,
        link_state=LINK_LINKED,
    )


def make_strip(entry) -> SectionStrip:
    strip = SectionStrip()
    strip.resize(STRIP_WIDTH, strip.height())
    strip.set_spans(timeline_spans(entry), entry.video.duration_s)
    return strip


def no_name(_transect_id) -> str | None:
    return None


def test_spans_land_in_time_order_along_the_strip() -> None:
    entry = make_entry(windows=((60.0, 90.0), (0.0, 30.0)))
    strip = make_strip(entry)

    assert [span.pass_id for span in strip.spans] == [
        str(entry.passes[1].id),
        str(entry.passes[0].id),
    ]


def test_span_at_answers_with_the_pass_under_that_point() -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    first, second = (str(p.id) for p in entry.passes)
    strip = make_strip(entry)

    # A quarter along is inside the first section, five eighths inside the
    # second, and the gap between them is bare groove.
    assert strip.span_at(STRIP_WIDTH * 0.125) == first
    assert strip.span_at(STRIP_WIDTH * 0.625) == second
    assert strip.span_at(STRIP_WIDTH * 0.45) is None


def test_a_clip_nothing_has_been_cut_from_says_so_rather_than_sitting_empty() -> None:
    entry = make_entry(windows=())
    strip = make_strip(entry)

    assert strip.spans == []
    assert strip.toolTip() == NO_SECTIONS_TOOLTIP
    strip.grab()


def test_a_clip_of_unknown_length_gets_no_spans_and_says_so() -> None:
    """Expected behaviour: no pass is invented from a length nobody read."""
    entry = make_entry(duration_s=None)
    strip = make_strip(entry)

    assert strip.spans == []
    assert strip.toolTip() == UNKNOWN_LENGTH_TOOLTIP
    strip.grab()


def test_clicking_a_span_emits_its_pass_and_bare_groove_emits_nothing() -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    entry = make_entry(windows=((0.0, 30.0),))
    strip = make_strip(entry)
    seen: list[str] = []
    strip.span_clicked.connect(seen.append)

    def click(x: float) -> None:
        strip.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(x, strip.height() / 2),
                QPointF(x, strip.height() / 2),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    click(STRIP_WIDTH * 0.1)
    click(STRIP_WIDTH * 0.9)

    assert seen == [str(entry.passes[0].id)]


def test_a_reprocessed_section_paints_without_running_off_its_own_span() -> None:
    entry = make_entry(
        windows=((0.0, 30.0),), runs_per_pass=(("succeeded", "failed", "succeeded"),)
    )
    strip = make_strip(entry)

    assert strip.spans[0].run_count == 3
    strip.grab()


def test_a_very_short_section_is_still_wide_enough_to_hit() -> None:
    entry = make_entry(windows=((0.0, 0.5),), duration_s=3600.0)
    strip = make_strip(entry)

    assert strip.span_at(SPACE_SM / 2) == str(entry.passes[0].id)


def test_gravity_reads_as_a_fact_only_once_it_has_been_read() -> None:
    """A dot for each of the two answers, and no dot at all for no answer."""
    row = VideoRow()

    row.set_entry(make_entry(gravity=YES), no_name)
    recorded = row.gravity_tooltip
    assert row.gravity_dot is not None

    row.set_entry(make_entry(gravity=NO), no_name)
    assert row.gravity_dot is not None
    assert row.gravity_tooltip != recorded

    row.set_entry(make_entry(gravity=UNKNOWN), no_name)
    assert row.gravity_dot is None
    assert row.gravity_tooltip == GRAVITY_UNKNOWN_TOOLTIP


def test_gravity_sorts_the_clips_without_one_to_the_top() -> None:
    without = make_entry(gravity=NO)
    with_ = make_entry(gravity=YES)
    unread = make_entry(gravity=UNKNOWN)

    ordered = sort_clips([with_, unread, without], SORT_GRAVITY)
    assert [e.video.gravity for e in ordered] == [NO, YES, UNKNOWN]

    # A clip nobody has read has no answer to sort on, so it sinks either way.
    reversed_ = sort_clips([with_, unread, without], SORT_GRAVITY, descending=True)
    assert [e.video.gravity for e in reversed_] == [YES, NO, UNKNOWN]


def test_a_capture_time_standing_in_for_a_missing_one_is_marked() -> None:
    stamp = "2026-07-01T14:32:00+00:00"
    embedded = make_video(captured_at=stamp, captured_source=SOURCE_CONTAINER)
    estimated = make_video(captured_at=stamp, captured_source=SOURCE_MTIME)

    assert not capture_label(embedded).startswith("~")
    assert capture_label(estimated).startswith("~")
    assert capture_label(estimated) == f"~{capture_label(embedded)}"


def test_a_row_carries_the_whole_path_even_when_the_name_is_elided() -> None:
    row = VideoRow()
    entry = make_entry(file_name="GX010001.MP4", path="/very/long/card/path/GX010001.MP4")

    row.set_entry(entry, no_name)

    assert entry.video.path in row.toolTip()
    row.grab()


def test_the_list_keeps_its_rows_when_nothing_about_them_changed() -> None:
    """Expected behaviour: a refresh under the cursor does not replace the row
    being clicked, or scroll the list back to the top."""
    entry = make_entry()
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()

    listing.set_groups(groups, no_name)
    before = listing.rows()[str(entry.video.id)]
    listing.set_groups(groups, no_name)

    assert listing.rows()[str(entry.video.id)] is before


def test_the_list_rebuilds_when_a_clip_arrives() -> None:
    first, second = make_entry(), make_entry()
    listing = VideoLibraryList()

    listing.set_groups([DateGroup(key="d", title="Today", entries=[first])], no_name)
    listing.set_groups(
        [DateGroup(key="d", title="Today", entries=[first, second])], no_name
    )

    assert set(listing.rows()) == {str(first.video.id), str(second.video.id)}


def test_activating_a_row_selects_it_and_reaches_the_list() -> None:
    entry = make_entry()
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=[entry])], no_name)
    seen: list[str] = []
    listing.activated.connect(seen.append)

    listing.rows()[str(entry.video.id)].activated.emit(str(entry.video.id))

    assert seen == [str(entry.video.id)]
    assert listing.selected == str(entry.video.id)


def test_a_span_clicked_on_a_row_reaches_the_list() -> None:
    entry = make_entry()
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=[entry])], no_name)
    seen: list[str] = []
    listing.span_clicked.connect(seen.append)

    listing.rows()[str(entry.video.id)].strip.span_clicked.emit(str(entry.passes[0].id))

    assert seen == [str(entry.passes[0].id)]


def test_a_section_names_its_transect_in_the_hover_text() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    row = VideoRow()
    row.strip.resize(STRIP_WIDTH, row.strip.height())

    row.set_entry(entry, lambda _id: "North reef")

    assert "North reef" in row.strip._span_tooltip(row.strip.spans[0])


def test_an_unfiled_section_says_so_rather_than_naming_nothing() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    row = VideoRow()
    row.set_entry(entry, no_name)

    assert "Unassigned" in row.strip._span_tooltip(row.strip.spans[0])


def test_transect_ids_reach_the_resolver_unchanged() -> None:
    asked: list[object] = []
    transect_id = uuid.uuid4()
    entry = make_entry()
    entry.passes[0].transect_id = transect_id
    row = VideoRow()

    row.set_entry(entry, lambda tid: asked.append(tid) or "T1")

    assert asked == [transect_id]


# --- sections under their clip ---------------------------------------------


def one_group(*entries):
    return [DateGroup(key="d", title="Today", entries=list(entries))]


def make_list(*entries, **kwargs) -> VideoLibraryList:
    listing = VideoLibraryList()
    listing.set_groups(one_group(*entries), no_name, **kwargs)
    return listing


def label_texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in widget.findChildren(QLabel)]


def action(menu, text):
    return next(entry for entry in menu.actions() if entry.text() == text)


def test_sections_stay_hidden_until_their_clip_is_opened() -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = make_list(entry)
    listing.show()

    assert [row.isVisible() for row in listing.sections().values()] == [False, False]

    listing.expand(str(entry.video.id))

    assert [row.isVisible() for row in listing.sections().values()] == [True, True]


def test_an_open_clip_stays_open_across_a_refresh() -> None:
    """Expected behaviour: a scan lands every few seconds, and a pass list
    that shut itself each time could never be read."""
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    video_id = str(entry.video.id)
    listing.rows()[video_id].chevron.setChecked(True)
    before = listing.rows()[video_id]

    listing.set_groups(one_group(entry), no_name)

    assert listing.rows()[video_id] is before
    assert listing.rows()[video_id].expanded


def test_a_section_cut_a_moment_ago_gets_a_row_on_the_next_scan() -> None:
    first = make_entry(windows=((0.0, 30.0),))
    listing = make_list(first)
    second = make_entry(windows=((0.0, 30.0), (60.0, 90.0)), asset=first.video)

    listing.set_groups(one_group(second), no_name)

    assert set(listing.sections()) == {str(p.id) for p in second.passes}


def test_a_clip_with_nothing_cut_from_it_keeps_the_space_but_not_the_chevron() -> None:
    """Expected behaviour: the disclosure column holds its width, so the file
    names stay in one column down the list."""
    row = VideoRow()
    row.set_entry(make_entry(windows=()), no_name)

    assert not row.chevron.isEnabled()
    assert row.chevron.icon().isNull()
    assert row.chevron.width() > 0

    row.set_entry(make_entry(windows=((0.0, 30.0),)), no_name)
    assert not row.chevron.icon().isNull()


def test_a_section_row_leads_with_its_window_then_says_where_it_stands() -> None:
    entry = make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded", "succeeded"),))
    entry.passes[0].direction = "forward"
    listing = VideoLibraryList()
    listing.set_groups(one_group(entry), lambda _id: "North reef")
    row = listing.sections()[str(entry.passes[0].id)]

    texts = label_texts(row)
    # The window and how long it runs for, on one label: the clip above says how
    # long the recording is, and this says how much of it was cut.
    assert "0:00–0:30 · 30s" in texts
    assert "2 runs" in texts
    # Direction is an arrow rather than a word, so it costs an icon's width
    # instead of a column; the tooltip still says it in words.
    assert "forward" in row._direction.toolTip()
    assert not row._direction.pixmap().isNull()
    # The chip is the name and nothing else: an arrow on it read as a link to
    # somewhere, which it was not.
    assert row.transect_chip.full_text == "North reef"
    assert "forward" in row.transect_chip.toolTip()
    row.grab()


def test_an_unfiled_section_invites_a_transect_rather_than_showing_a_blank() -> None:
    """Expected behaviour: not an error. A pass processes unfiled, so the
    chip is an invitation in the accent colour."""
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    chip = listing.sections()[str(entry.passes[0].id)].transect_chip

    assert chip.full_text == SET_TRANSECT
    assert PRIMARY in chip.styleSheet()


def test_a_section_in_the_cart_offers_the_way_back_out() -> None:
    """One entry naming the move it will make, rather than an add greyed out on
    everything already in the cart."""
    from deepreefmap_gui.runs.video_rows import MENU_REMOVE_FROM_CART

    entry = make_entry(windows=((0.0, 30.0),))
    pass_id = str(entry.passes[0].id)
    listing = make_list(entry, in_cart=lambda pid: pid == pass_id)

    menu = listing.sections()[pass_id].menu()

    assert action(menu, MENU_REMOVE_FROM_CART).isEnabled()
    assert [a for a in menu.actions() if a.text() == MENU_ADD_TO_CART] == []
    assert action(menu, MENU_RETRIM).isEnabled()


def test_a_section_that_has_been_run_cannot_be_deleted_from_here() -> None:
    """Expected behaviour: the runs are what makes it undeletable, and the
    tooltip says where to go and delete those."""
    entry = make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),))

    listing = make_list(entry)
    delete = action(listing.sections()[str(entry.passes[0].id)].menu(), MENU_DELETE)

    assert not delete.isEnabled()
    assert delete.toolTip() == DELETE_BLOCKED_TOOLTIP


def test_a_section_with_no_runs_can_be_deleted() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)

    assert action(listing.sections()[str(entry.passes[0].id)].menu(), MENU_DELETE).isEnabled()


@pytest.mark.parametrize(
    ("label", "signal_name"),
    [
        (MENU_ADD_TO_CART, "section_add_to_cart"),
        (MENU_RETRIM, "section_retrim"),
        (MENU_REASSIGN, "section_reassign"),
        (MENU_DELETE, "section_delete"),
    ],
)
def test_a_section_action_names_the_section_it_was_chosen_on(label, signal_name) -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = make_list(entry)
    wanted = str(entry.passes[1].id)
    seen: list[str] = []
    getattr(listing, signal_name).connect(seen.append)

    action(listing.sections()[wanted].menu(), label).trigger()

    assert seen == [wanted]


@pytest.mark.parametrize(
    ("button", "signal_name"),
    [
        ("cart_btn", "section_add_to_cart"),
        ("trim_btn", "section_retrim"),
        ("delete_btn", "section_delete"),
        ("transect_chip", "section_reassign"),
    ],
)
def test_a_sections_own_buttons_name_the_section_they_sit_on(button, signal_name) -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = make_list(entry)
    wanted = str(entry.passes[1].id)
    seen: list[str] = []
    getattr(listing, signal_name).connect(seen.append)

    getattr(listing.sections()[wanted], button).click()

    assert seen == [wanted]


def test_a_carted_or_run_section_says_so_in_the_button_it_would_use() -> None:
    """Expected behaviour: the button stays enabled and explains itself. A
    disabled QToolButton takes no mouse events, so its tooltip never shows."""
    entry = make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),))
    pass_id = str(entry.passes[0].id)
    listing = make_list(entry, in_cart=lambda pid: pid == pass_id)
    row = listing.sections()[pass_id]

    assert row.cart_btn.isEnabled()
    assert row.cart_btn.toolTip() == IN_CART_TOOLTIP
    assert row.delete_btn.isEnabled()
    assert row.delete_btn.toolTip() == DELETE_BLOCKED_TOOLTIP


def test_an_unfiled_section_still_reaches_the_transects_page() -> None:
    """The page is where a transect is drawn, so it cannot need one to be opened."""
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    row = listing.sections()[str(entry.passes[0].id)]
    seen: list[str] = []
    listing.section_open_transect.connect(seen.append)

    unfiled = action(row.menu(), MENU_OPEN_TRANSECTS_PAGE)
    assert unfiled.isEnabled()
    unfiled.trigger()
    assert seen == [""]

    row.section.transect_id = uuid.uuid4()
    seen.clear()
    action(row.menu(), MENU_OPEN_TRANSECT).trigger()

    assert seen == [str(row.section.transect_id)]


def test_the_pane_list_holds_one_compact_row_per_section() -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = SectionList()
    listing.set_sections(entry, lambda _id: "North reef")

    rows = listing.rows()
    assert list(rows) == [str(p.id) for p in entry.passes]
    first = rows[str(entry.passes[0].id)]
    assert first.transect_chip.full_text == "North reef"
    # The runs cell is one of the two a third of a page has no room for.
    assert not first._runs.isVisibleTo(listing)


def test_a_section_spanning_two_clips_is_filled_under_both() -> None:
    """Scenario: a swim the camera split at 4 GB is one pass over two clips,
    so it gets a row under each. Keyed by pass id alone, only the last row
    answered to the id and the first was left blank, with no pass to act on.

    Expected behaviour: both rows describe the pass, and neither emits an
    empty id at the page.
    """
    from deepreefmap_gui.survey.models.video_asset import VideoAsset

    second_clip = VideoAsset(file_name="GX020001.MP4", path="/data/GX020001.MP4", hash="ef" * 16)
    first = make_entry(windows=((0.0, 30.0),))
    shared = first.passes[0]
    shared.extra_video_ids = [second_clip.id]
    second = VideoLibraryEntry(
        video=second_clip,
        pass_count=1,
        run_count=0,
        passes=[shared],
        runs=[],
        link_state=LINK_LINKED,
    )
    listing = make_list(first, second)

    rows = listing.findChildren(SectionRow)
    assert len(rows) == 2
    assert all(row.pass_id == str(shared.id) for row in rows)
    assert all(row.section is not None for row in rows)

    seen: list[str] = []
    listing.section_reassign.connect(seen.append)
    for row in rows:
        row.transect_chip.click()
    assert seen == [str(shared.id), str(shared.id)]


def test_an_unfilled_row_asks_the_page_for_nothing() -> None:
    """A row the list has built and not filled knows no pass, and an empty
    id reaching the page is an unhandled error in front of the user."""
    row = SectionRow()
    seen: list[str] = []
    row.reassign_requested.connect(seen.append)
    row.delete_requested.connect(seen.append)
    row.retrim_requested.connect(seen.append)
    row.add_to_cart_requested.connect(seen.append)

    row.transect_chip.click()
    row.delete_btn.click()
    row.trim_btn.click()
    row.cart_btn.click()

    assert seen == []


def test_a_missing_file_is_marked_on_the_button_that_needs_it() -> None:
    """Trimming decodes the file, so a clip that is gone cannot be trimmed. The
    button says so before the click does."""
    from deepreefmap_gui.survey.catalogue import LINK_MISSING

    entry = make_entry(windows=((0.0, 30.0),))
    entry.link_state = LINK_MISSING
    listing = make_list(entry)
    row = listing.sections()[str(entry.passes[0].id)]

    assert row.trim_btn.toolTip() == TRIM_UNLINKED_TOOLTIP
    # Nothing can be processed from footage that is not there, so the cart is
    # marked too rather than taking it and failing at the run.
    assert row.cart_btn.toolTip() == CART_UNLINKED_TOOLTIP


def test_a_link_nobody_has_checked_yet_is_not_a_missing_one() -> None:
    """Not knowing is not knowing it is gone, and marking every unchecked clip
    in red says the library is broken every time the app opens."""
    from deepreefmap_gui.survey.catalogue import LINK_UNKNOWN

    entry = make_entry(windows=((0.0, 30.0),))
    entry.link_state = LINK_UNKNOWN
    listing = make_list(entry)
    row = listing.sections()[str(entry.passes[0].id)]

    assert row.trim_btn.toolTip() == RETRIM_TOOLTIP
    assert row.cart_btn.toolTip() == MENU_ADD_TO_CART


def test_an_empty_pane_list_keeps_its_well_and_says_it_is_empty() -> None:
    """Expected behaviour: the dark panel is what says a list belongs here, so
    it stays when there is nothing in it."""
    listing = SectionList()
    listing.set_sections(make_entry(windows=()))

    assert listing.rows() == {}
    assert listing.empty.isVisibleTo(listing)

    listing.set_sections(make_entry(windows=((0.0, 30.0),)))
    assert not listing.empty.isVisibleTo(listing)


def test_the_pane_list_rebuilds_only_when_the_sections_change() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = SectionList()
    listing.set_sections(entry)
    was = listing.rows()[str(entry.passes[0].id)]

    listing.set_sections(entry)
    assert listing.rows()[str(entry.passes[0].id)] is was

    grown = make_entry(windows=((0.0, 30.0), (60.0, 90.0)), asset=entry.video)
    listing.set_sections(grown)
    assert len(listing.rows()) == 2


def test_clicking_a_section_selects_it_and_lets_go_of_the_clip() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    pass_id = str(entry.passes[0].id)
    listing.set_selected(str(entry.video.id))
    seen: list[str] = []
    listing.section_activated.connect(seen.append)

    listing.sections()[pass_id].activated.emit(pass_id)

    assert seen == [pass_id]
    assert listing.selected_section == pass_id
    assert listing.selected is None


def test_selecting_a_clip_lets_go_of_the_section() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    listing.set_selected_section(str(entry.passes[0].id))

    listing.set_selected(str(entry.video.id))

    assert listing.selected_section is None


def test_a_clip_row_names_itself_in_the_buttons_beside_it() -> None:
    entry = make_entry()
    listing = make_list(entry)
    video_id = str(entry.video.id)
    row = listing.rows()[video_id]
    revealed: list[str] = []
    cut: list[str] = []
    listing.reveal_requested.connect(revealed.append)
    listing.new_section_requested.connect(cut.append)

    row.link_btn.click()
    row.new_section_btn.click()

    assert revealed == [video_id]
    assert cut == [video_id]


def test_the_clip_menu_offers_to_put_back_a_clip_that_is_already_hidden() -> None:
    row = VideoRow()
    entry = make_entry()

    row.set_entry(entry, no_name)
    assert [action.text() for action in row.menu().actions()][0] == MENU_HIDE

    row.set_entry(entry, no_name, hidden=True)
    assert [action.text() for action in row.menu().actions()][0] == MENU_UNHIDE


def test_only_the_sections_nothing_was_made_from_can_be_swept_up() -> None:
    """Expected behaviour: a pass standing for a run that happened stays."""
    row = VideoRow()

    row.set_entry(make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),)), no_name)
    assert row.unused_sections() == 0
    swept = next(a for a in row.menu().actions() if a.text() == MENU_DELETE_UNUSED)
    assert not swept.isEnabled()

    row.set_entry(make_entry(windows=((0.0, 30.0), (40.0, 60.0))), no_name)
    assert row.unused_sections() == 2
    swept = next(a for a in row.menu().actions() if a.text() == MENU_DELETE_UNUSED)
    assert swept.isEnabled()


def test_deleting_a_clip_asks_in_the_button_rather_than_in_a_dialog() -> None:
    """Scenario: a library full of imports that should never have been made.

    Expected behaviour: the first click arms the button, the second deletes.
    Nothing leaves on one click, and clearing a run of clips costs no dialogs.
    """
    row = VideoRow()
    row.set_entry(make_entry(), no_name)
    asked = []
    row.delete_requested.connect(asked.append)

    row.delete_btn.click()
    assert asked == []
    assert row.delete_btn.toolTip() == DELETE_CLIP_ARMED_TOOLTIP

    row.delete_btn.click()
    assert asked == [row.video_id]
    assert row.delete_btn.toolTip() == DELETE_CLIP_TOOLTIP


def test_an_armed_delete_stands_down_once_it_times_out() -> None:
    row = VideoRow()
    row.set_entry(make_entry(), no_name)
    asked = []
    row.delete_requested.connect(asked.append)

    row.delete_btn.click()
    row._delete_arm.stop()
    row._apply_delete_icon()
    row.delete_btn.click()

    assert asked == []
    assert row.delete_btn.toolTip() == DELETE_CLIP_ARMED_TOOLTIP


def test_an_armed_delete_stands_down_when_the_row_is_refilled() -> None:
    """A rebuilt list may put another clip here, and it is not the one aimed at."""
    row = VideoRow()
    row.set_entry(make_entry(), no_name)
    asked = []
    row.delete_requested.connect(asked.append)

    row.delete_btn.click()
    row.set_entry(make_entry(), no_name)
    row.delete_btn.click()

    assert asked == []


def test_a_clip_says_its_runs_go_first_and_that_the_file_stays() -> None:
    row = VideoRow()

    row.set_entry(make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),)), no_name)
    assert row.delete_btn.isEnabled()
    assert row.delete_btn.toolTip() == DELETE_CLIP_BLOCKED_TOOLTIP
    assert KEEPS_FILE_NOTE in row.delete_btn.toolTip()

    row.set_entry(make_entry(), no_name)
    assert KEEPS_FILE_NOTE in row.delete_btn.toolTip()
    assert KEEPS_FILE_NOTE in action(row.menu(), MENU_DELETE_CLIP).toolTip()


def test_the_list_says_it_takes_a_drop() -> None:
    listing = make_list(make_entry())

    assert listing.drop_hint.text() == DROP_HINT
    listing.grab()


def test_a_section_of_a_clip_of_unknown_length_still_gets_a_row() -> None:
    """Expected behaviour: the strip cannot place it, which is no reason to
    leave the pass itself off the page."""
    entry = make_entry(windows=((0.0, 30.0),), duration_s=None)

    listing = make_list(entry)

    assert set(listing.sections()) == {str(entry.passes[0].id)}
    assert "Queued" in listing.sections()[str(entry.passes[0].id)].toolTip()


def test_a_bare_section_row_paints_before_it_has_been_told_anything() -> None:
    SectionRow().grab()


# --- the header row ----------------------------------------------------------


def click_cell(cell) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    centre = QPointF(cell.width() / 2, cell.height() / 2)
    cell.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            centre,
            centre,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def test_the_clip_facts_sit_in_their_own_columns() -> None:
    """Expected behaviour: recorded, length and size are aligned columns under
    the header cells that name them, not one composite line."""
    entry = make_entry(
        duration_s=723.0,
        size_bytes=int(3.4 * 1024**3),
        captured_at="2026-07-01T14:32:00+00:00",
        captured_source=SOURCE_CONTAINER,
    )
    row = VideoRow()
    row.set_entry(entry, no_name)

    texts = label_texts(row)
    assert capture_label(entry.video) in texts
    assert "12m 03s" in texts
    assert "3.4 GB" in texts
    assert not any("·" in text for text in texts)
    row.grab()


def test_a_header_click_sorts_and_a_second_click_reverses() -> None:
    header = VideoListHeader()
    seen: list[tuple[str, bool]] = []
    header.sort_changed.connect(lambda column, descending: seen.append((column, descending)))

    click_cell(header.cell(SORT_NAME))
    click_cell(header.cell(SORT_NAME))
    click_cell(header.cell(SORT_SIZE))

    assert seen == [(SORT_NAME, False), (SORT_NAME, True), (SORT_SIZE, False)]


def test_the_header_marks_the_active_column_and_its_direction() -> None:
    header = VideoListHeader()
    header.set_sort(SORT_SIZE, True)

    assert header.cell(SORT_SIZE).text() == f"Size {SORT_DESC_GLYPH}"
    assert header.cell(SORT_NAME).text() == "Name"

    header.sort_by(SORT_SIZE)
    assert header.cell(SORT_SIZE).text() == f"Size {SORT_ASC_GLYPH}"
    header.grab()


def test_sorted_groups_rebuild_the_list_in_their_order() -> None:
    """Expected behaviour: the sort reorders clips within their date groups,
    and a group's clips never cross into another group."""
    big = make_entry(file_name="big.mp4", size_bytes=300)
    small = make_entry(file_name="small.mp4", size_bytes=100)
    late = make_entry(file_name="late.mp4", size_bytes=200)
    groups = [
        DateGroup(key="new", title="Today", entries=[big, small]),
        DateGroup(key="old", title="Yesterday", entries=[late]),
    ]
    listing = VideoLibraryList()

    listing.set_groups(sort_groups(groups, SORT_SIZE), no_name)
    assert [row.entry.video.file_name for row in listing.rows().values()] == [
        "small.mp4",
        "big.mp4",
        "late.mp4",
    ]

    listing.set_groups(sort_groups(groups, SORT_SIZE, descending=True), no_name)
    assert [row.entry.video.file_name for row in listing.rows().values()] == [
        "big.mp4",
        "small.mp4",
        "late.mp4",
    ]


# --- Which column a wider window feeds --------------------------------------

def test_a_wider_window_feeds_the_clip_name_before_the_sections_strip(qapp) -> None:
    """Scenario: the window is widened.

    Expected behaviour: the name column grows. The strip held the row's only
    layout stretch, so a name frozen at thirty characters stayed elided while the
    timeline beside it took every pixel a wider window bought.
    """
    entry = make_entry(file_name="10.03.2023_Japanese Garden_T2_C1_F_GX010002.MP4")
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()
    listing.set_groups(groups, no_name)
    listing.show()

    listing.resize(900, 400)
    qapp.processEvents()
    narrow = listing.rows()[str(entry.video.id)]._name.width()

    listing.resize(1600, 400)
    qapp.processEvents()
    wide = listing.rows()[str(entry.video.id)]._name.width()

    assert wide > narrow


def test_the_strip_keeps_a_floor_a_timeline_is_still_readable_at(qapp) -> None:
    from deepreefmap_gui.runs.video_rows import SECTIONS_MIN_WIDTH

    entry = make_entry(windows=((0.0, 30.0), (40.0, 60.0)))
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()
    listing.set_groups(groups, no_name)
    listing.show()
    listing.resize(1600, 400)
    qapp.processEvents()

    row = listing.rows()[str(entry.video.id)]
    assert row.strip.width() >= SECTIONS_MIN_WIDTH


def test_a_widened_name_column_drops_the_ellipsis_it_no_longer_needs(qapp) -> None:
    """Elided once at fill time, a name kept its ellipsis however wide the column
    later became."""
    long_name = "10.03.2023_Japanese Garden_T2_C1_F_GX010002.MP4"
    entry = make_entry(file_name=long_name)
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()
    listing.set_groups(groups, no_name)
    listing.show()

    listing.resize(700, 400)
    qapp.processEvents()
    assert "…" in listing.rows()[str(entry.video.id)]._name.text()

    listing.resize(2000, 400)
    qapp.processEvents()
    assert listing.rows()[str(entry.video.id)]._name.text() == long_name


def test_the_heading_stays_over_the_column_it_names(qapp) -> None:
    from deepreefmap_gui.runs.video_rows import SORT_NAME, VideoListHeader

    entry = make_entry()
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    header = VideoListHeader()
    listing = VideoLibraryList()
    listing.follow_header(header)
    listing.set_groups(groups, no_name)
    listing.show()
    listing.resize(1600, 400)
    qapp.processEvents()

    row = listing.rows()[str(entry.video.id)]
    assert header.cell(SORT_NAME).width() == row._name.width()


# --- Where a section sits along its clip ------------------------------------

def _clip_and_sections(qapp, width=1400, duration_s=120.0, windows=((10.0, 40.0),)):
    """One clip row with its passes open under it, laid out at a real width."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    entry = make_entry(windows=windows, duration_s=duration_s)
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()
    listing.set_groups(groups, no_name)
    listing.expand(str(entry.video.id))
    listing.set_groups(groups, no_name)

    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(listing)
    host.resize(width, 400)
    host.show()
    qapp.processEvents()
    qapp.processEvents()
    clip = listing.rows()[str(entry.video.id)]
    sections = listing._sections_by_video[str(entry.video.id)]
    return listing, clip, sections, host


@pytest.mark.parametrize("width", [1600, 1400, 1100])
def test_a_section_strip_shares_its_clip_s_time_axis(qapp, width) -> None:
    """Scenario: a clip is expanded, at three window widths.

    Expected behaviour: the pass's bar starts and ends on the same pixels as
    its clip's, so a position along one means the same as along the other. This
    is the guard for the next column added to either row.
    """
    _listing, clip, sections, _host = _clip_and_sections(qapp, width=width)
    assert sections

    parent = clip.strip.mapTo(clip.window(), clip.strip.rect().topLeft())
    child = sections[0].strip.mapTo(sections[0].window(), sections[0].strip.rect().topLeft())

    assert child.x() == parent.x()
    assert sections[0].strip.width() == clip.strip.width()


def test_a_section_strip_carries_only_its_own_span(qapp) -> None:
    from deepreefmap_gui.survey.video_groups import timeline_spans

    _listing, clip, sections, _host = _clip_and_sections(
        qapp, windows=((10.0, 40.0), (60.0, 90.0))
    )
    expected = timeline_spans(clip._entry)

    assert len(clip.strip.spans) == 2
    for section, span in zip(sections, expected, strict=True):
        assert len(section.strip.spans) == 1
        assert section.strip.spans[0].begin == pytest.approx(span.begin)
        assert section.strip.spans[0].end == pytest.approx(span.end)


def test_a_clip_of_unknown_length_gives_its_sections_no_strip(qapp) -> None:
    """The row is the pass, so neither "nothing cut from this" nor "length
    unknown" is a statement it can make about itself."""
    _listing, _clip, sections, _host = _clip_and_sections(qapp, duration_s=0.0)

    assert sections
    assert not sections[0].strip.isVisibleTo(sections[0])


def test_the_clip_pane_s_compact_rows_build_no_strip(qapp) -> None:
    """No clip strip beside them to line up with, and the pane is a third of a page."""
    row = SectionRow(compact=True)

    assert row.strip is None


def test_a_section_strip_never_steals_the_row_s_own_click(qapp) -> None:
    """The row's tooltip lazily decodes a three-frame preview and a click selects
    it; a live strip on top would take both."""
    from PySide6.QtCore import Qt

    row = SectionRow()

    assert row.strip is not None
    assert row.strip.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


# --- picking more than one clip ---------------------------------------------


def _picking_list(count: int = 4) -> tuple[VideoLibraryList, list[str]]:
    """A list of clips, and their ids in the order it shows them."""
    entries = [make_entry(file_name=f"clip{n}.mp4") for n in range(count)]
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=entries)], no_name)
    return listing, [str(entry.video.id) for entry in entries]


def _click(listing: VideoLibraryList, video_id: str, modifier) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    listing.rows()[video_id].mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(1.0, 1.0),
            QPointF(1.0, 1.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            modifier,
        )
    )


def test_a_plain_click_picks_one_clip_and_describes_it() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()
    seen: list[str] = []
    listing.activated.connect(seen.append)

    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)
    _click(listing, ids[2], Qt.KeyboardModifier.NoModifier)

    assert listing.selection() == {ids[2]}
    assert listing.selected == ids[2]
    assert seen == [ids[0], ids[2]]


def test_control_click_adds_a_clip_and_takes_it_back_out() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()
    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)

    _click(listing, ids[2], Qt.KeyboardModifier.ControlModifier)
    assert listing.selection() == {ids[0], ids[2]}

    _click(listing, ids[2], Qt.KeyboardModifier.ControlModifier)
    assert listing.selection() == {ids[0]}


def test_control_click_leaves_the_detail_pane_where_it_was() -> None:
    """Picking a set is not picking a clip to read, so ``activated`` stays quiet."""
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()
    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)
    seen: list[str] = []
    listing.activated.connect(seen.append)

    _click(listing, ids[3], Qt.KeyboardModifier.ControlModifier)

    assert seen == []
    assert listing.selected == ids[0]


def test_shift_click_takes_the_range_in_the_order_the_list_shows() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()
    _click(listing, ids[3], Qt.KeyboardModifier.NoModifier)

    _click(listing, ids[1], Qt.KeyboardModifier.ShiftModifier)

    assert listing.selection() == {ids[1], ids[2], ids[3]}


def test_shift_click_with_nothing_picked_yet_takes_that_clip_alone() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()

    _click(listing, ids[2], Qt.KeyboardModifier.ShiftModifier)

    assert listing.selection() == {ids[2]}


def test_a_rebuild_drops_the_clips_that_have_gone_and_keeps_the_rest() -> None:
    """Scenario: a background scan finds a clip has gone, so the rows are rebuilt.

    Expected behaviour: a set holding ids nobody can see is a delete aimed at
    nothing, so the ids without a row are pruned and the change is reported.
    """
    from PySide6.QtCore import Qt

    entries = [make_entry(file_name=f"clip{n}.mp4") for n in range(3)]
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=entries)], no_name)
    ids = [str(entry.video.id) for entry in entries]
    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)
    _click(listing, ids[2], Qt.KeyboardModifier.ControlModifier)
    told: list[set[str]] = []
    listing.selection_changed.connect(lambda: told.append(listing.selection()))

    listing.set_groups([DateGroup(key="d", title="Today", entries=entries[:2])], no_name)

    assert listing.selection() == {ids[0]}
    assert told == [{ids[0]}]


def test_picking_a_section_lets_go_of_every_clip() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()
    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)
    _click(listing, ids[1], Qt.KeyboardModifier.ControlModifier)

    listing.set_selected_section("a-pass")

    assert listing.selection() == set()
    assert listing.selected is None


def test_every_picked_row_paints_itself_picked() -> None:
    from PySide6.QtCore import Qt

    listing, ids = _picking_list()

    _click(listing, ids[0], Qt.KeyboardModifier.NoModifier)
    _click(listing, ids[1], Qt.KeyboardModifier.ControlModifier)

    painted = {
        video_id for video_id, row in listing.rows().items() if row.property("selected")
    }
    assert painted == {ids[0], ids[1]}
