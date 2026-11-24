"""The missing-data checklist: grouped by level, each line a way to its editor."""

import pytest

from deepreefmap_gui.simple.expedition_dialog import COMPLETE_NOTE, GAP_ROLE, MissingDataDialog
from deepreefmap_gui.survey.completeness import EXPEDITION
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.store import SurveyStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def store(tmp_path):
    return SurveyStore(tmp_path / "survey.db")


def _groups(dialog):
    return [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]


def test_an_expedition_without_dates_says_so_and_opens_its_editor(store):
    campaign = Campaign(name="2026_11_JO_EXPE")
    store.add_campaign(campaign)
    opened = []
    dialog = MissingDataDialog(None, store, campaign.id, opened.append)

    assert _groups(dialog) == ["Expedition (1)"]
    line = dialog.tree.topLevelItem(0).child(0)
    assert line.text(1) == "start and end dates"
    assert line.data(0, GAP_ROLE).record_id == campaign.id
    dialog._on_activated(line, 0)  # refreshes the list, so the line is gone after
    assert opened[0].level == EXPEDITION


def test_a_complete_expedition_says_nothing_is_missing(store):
    campaign = Campaign(name="done", begin_date="2026-11-20", end_date="2026-11-27")
    store.add_campaign(campaign)
    dialog = MissingDataDialog(None, store, campaign.id, lambda gap: None)

    assert _groups(dialog) == []
    assert dialog.summary.text() == COMPLETE_NOTE
