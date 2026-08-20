"""Filing a section: the map, the list, the direction, and a new transect."""

import pytest
from _factories import make_transect

from deepreefmap_gui.simple.transect_picker import (
    DRAW_HINT,
    DRAW_HINT_DONE,
    DRAW_HINT_END,
    EDIT_LATER_NOTE,
    MAP_HINT,
    MAP_HINT_EMPTY,
    OPEN_PAGE_EMPTY_LABEL,
    OPEN_PAGE_LABEL,
    UNASSIGNED_NOTE,
    TransectPickerDialog,
)
from deepreefmap_gui.survey.store import SurveyStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def store(tmp_path):
    return SurveyStore(tmp_path / "survey.db")


def test_a_section_can_be_filed_nowhere(store):
    """Unassigned stays a valid answer: a section is a cutout of a video first."""
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)

    assert dialog.choice() == (None, "forward")


def test_picking_from_the_list_answers_with_that_transect(store):
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store)

    dialog.list.setCurrentRow(1)
    dialog.direction.setCurrentIndex(1)

    assert dialog.choice() == (transect.id, "reverse")


def test_clicking_the_line_on_the_map_picks_it(store):
    """The whole reason there is a map: a transect is a place, not a name."""
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store)

    dialog.map.transect_clicked.emit(str(transect.id))

    assert dialog.selected_transect_id() == transect.id


def test_direction_reads_as_the_heading_it_means(store):
    """"Forward" says nothing about the water until there is a line to swim."""
    store.add_transect(make_transect("T1", start_lat=-17.5, start_lon=177.1,
                                    end_lat=-17.5, end_lon=177.2))
    dialog = TransectPickerDialog(None, store)
    dialog.list.setCurrentRow(1)

    labels = [dialog.direction.itemText(i) for i in range(dialog.direction.count())]
    assert labels[0].startswith("Forward (090°")
    assert labels[1].startswith("Reverse (270°")


def test_the_section_it_is_editing_opens_on_its_own_answer(store):
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store, transect_id=transect.id, direction="reverse")

    assert dialog.choice() == (transect.id, "reverse")


def test_a_new_transect_is_named_and_drawn_with_two_clicks(store):
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()

    assert dialog.name_input.text() == "Transect 1"
    assert dialog.map._pick_mode

    dialog.name_input.setText("Reef edge")
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._on_map_clicked(-17.5005, 177.1005)
    dialog.length_input.setValue(25.0)
    dialog._save_new_transect()

    stored = store.list_transects()
    assert [t.name for t in stored] == ["Reef edge"]
    assert stored[0].length_m == 25.0
    # Saving files the section against it too, or the two clicks bought nothing.
    assert dialog.choice() == (stored[0].id, "forward")
    assert not dialog.map._pick_mode


def test_a_half_drawn_transect_names_what_is_missing(store):
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._save_new_transect()

    assert "Missing end point" in dialog.error.text()
    assert store.list_transects() == []


def test_a_duplicate_name_is_refused_rather_than_crashing(store):
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    dialog.name_input.setText("T1")
    dialog._on_map_clicked(-17.5, 177.1)
    dialog._on_map_clicked(-17.5005, 177.1005)
    dialog._save_new_transect()

    assert "already exists" in dialog.error.text()
    assert len(store.list_transects()) == 1


def test_the_arrow_hands_the_transect_to_the_page_and_stands_down(store):
    """Leaving for the page abandons the filing, so nothing is half applied."""
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store, transect_id=transect.id)
    seen = []
    dialog.open_transect_requested.connect(seen.append)

    dialog._on_open_page()

    assert seen == [str(transect.id)]
    assert dialog.result() == int(TransectPickerDialog.DialogCode.Rejected)


def test_the_arrow_opens_the_page_with_nothing_picked(store):
    """A survey with no transects is when that page is needed most.

    The arrow used to be dead until something was picked, which made the page
    that transects are drawn and imported on unreachable from the one dialog
    that asks for a transect.
    """
    dialog = TransectPickerDialog(None, store)
    seen: list[str] = []
    dialog.open_transect_requested.connect(seen.append)

    assert dialog.open_btn.isEnabled()
    dialog.open_btn.click()

    assert seen == [""]
    assert dialog.left_for_page
    assert dialog.result() == int(TransectPickerDialog.DialogCode.Rejected)


def test_the_arrow_names_where_it_goes(store):
    """It leads somewhere different with a transect picked than without one."""
    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)

    assert dialog.open_btn.text() == OPEN_PAGE_EMPTY_LABEL
    assert "kept, unfiled" in dialog.open_btn.toolTip()

    dialog.list.setCurrentRow(1)

    assert dialog.open_btn.text() == OPEN_PAGE_LABEL
    assert "ends can be dragged" in dialog.open_btn.toolTip()


def test_the_map_says_it_can_be_clicked_before_anything_is_armed(store):
    """Nothing else on this dialog announces the map is a control.

    Picking only becomes visible once "New transect…" arms it, so until then a
    map with lines on it reads as a picture of where the survey is.
    """
    dialog = TransectPickerDialog(None, store)
    assert dialog.map_hint.text() == MAP_HINT_EMPTY

    store.add_transect(make_transect("T1"))
    dialog = TransectPickerDialog(None, store)
    assert dialog.map_hint.text() == MAP_HINT
    assert "Click one" in dialog.map_hint.text()


def test_drawing_says_which_click_comes_next(store):
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    assert dialog.draw_hint.text() == DRAW_HINT

    dialog._on_map_clicked(-17.5, 177.1)
    assert dialog.draw_hint.text() == DRAW_HINT_END

    dialog._on_map_clicked(-17.51, 177.11)
    assert dialog.draw_hint.text() == DRAW_HINT_DONE

    dialog._end_new_transect()
    assert dialog.draw_hint.text() == ""
    assert dialog.map_hint.text() == MAP_HINT_EMPTY


def test_filing_a_section_says_it_can_be_undone(store):
    """The hesitation this dialog causes is about permanence, so it answers it."""
    transect = make_transect("T1")
    store.add_transect(transect)
    dialog = TransectPickerDialog(None, store)

    assert EDIT_LATER_NOTE in dialog.note.text()
    assert UNASSIGNED_NOTE in dialog.note.text()

    dialog.list.setCurrentRow(1)
    assert EDIT_LATER_NOTE in dialog.note.text()
    assert UNASSIGNED_NOTE not in dialog.note.text()


def test_the_campaign_combo_only_exists_once_campaigns_have_been_pulled(store):
    from deepreefmap_gui.survey.models import Campaign

    without = TransectPickerDialog(None, store)
    assert without.campaign.count() == 0

    store.add_campaign(Campaign(name="2026_08_fiji"))
    with_campaigns = TransectPickerDialog(None, store)

    assert [with_campaigns.campaign.itemText(i) for i in range(with_campaigns.campaign.count())] == [
        "No campaign",
        "2026_08_fiji",
    ]


def test_the_campaign_picked_is_remembered_as_the_default(store):
    from deepreefmap_gui.survey.models import Campaign

    campaign = Campaign(name="2026_08_fiji")
    store.add_campaign(campaign)
    dialog = TransectPickerDialog(None, store)
    dialog.campaign.setCurrentIndex(dialog.campaign.findData(str(campaign.id)))

    assert dialog.campaign_choice() == campaign.id
    assert store.default_campaign_id() == campaign.id

    again = TransectPickerDialog(None, store)
    assert again.campaign.currentData() == str(campaign.id)


def test_a_new_transect_drawn_here_carries_the_picked_site(store):
    from deepreefmap_gui.survey.models import Site

    site = Site(name="Japanese Garden")
    store.add_site(site)
    dialog = TransectPickerDialog(None, store)
    dialog._start_new_transect()
    dialog.name_input.setText("T1")
    dialog.start_input.setText("-17.5, 177.1")
    dialog.end_input.setText("-17.5005, 177.1005")
    dialog.site_combo.setCurrentIndex(dialog.site_combo.findData(str(site.id)))

    dialog._save_new_transect()

    assert store.list_transects()[0].site_id == site.id
