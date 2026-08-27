"""Filing a pass under a campaign, from each place that offers it.

The choice is pushed to the registry, so what is under test is what reaches the
store and what the page says afterwards. The dialog is stubbed: Qt's combo is
not what could go wrong here.
"""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtWidgets import QDialog, QMenu

from deepreefmap_gui.runs import pass_campaign
from deepreefmap_gui.survey.models import Campaign, Site, TransectPass
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")


@pytest.fixture
def answers(monkeypatch):
    """Make the campaign dialog answer with whatever the test queues up."""

    def reply(campaign_id: uuid.UUID | None, *, accepted: bool = True):
        monkeypatch.setattr(
            pass_campaign.CampaignChoiceDialog,
            "exec",
            lambda self: QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected,
        )
        monkeypatch.setattr(pass_campaign.CampaignChoiceDialog, "chosen", lambda self: campaign_id)

    return reply


def a_pass(store, campaign_id: uuid.UUID | None = None) -> TransectPass:
    """A pass over a clip the store knows, since the pass has a key into it."""
    from _factories import make_video

    video = store.upsert_video(make_video(content_hash=f"h{uuid.uuid4().hex}"))
    pass_ = TransectPass(
        transect_id=None, video_id=video.id, begin_s=0.0, end_s=60.0, campaign_id=campaign_id
    )
    store.add_pass(pass_)
    return pass_


def a_campaign(store, name: str = "2026_08_fiji") -> Campaign:
    campaign = Campaign(name=name)
    store.add_campaign(campaign)
    return campaign


def test_a_chosen_campaign_reaches_the_store(qapp, store, answers) -> None:
    campaign = a_campaign(store)
    pass_ = a_pass(store)
    answers(campaign.id)

    said = pass_campaign.set_campaign(None, store, pass_.id)

    assert store.get_pass(pass_.id).campaign_id == campaign.id
    assert campaign.name in said


def test_cancelling_changes_nothing(qapp, store, answers) -> None:
    campaign = a_campaign(store)
    pass_ = a_pass(store, campaign.id)
    answers(None, accepted=False)

    assert pass_campaign.set_campaign(None, store, pass_.id) is None
    assert store.get_pass(pass_.id).campaign_id == campaign.id


def test_the_blank_row_takes_a_pass_back_out_of_its_campaign(qapp, store, answers) -> None:
    """A pass belongs to no campaign until somebody says otherwise, and may go
    back to that: neither the pipeline nor the registry requires one."""
    campaign = a_campaign(store)
    pass_ = a_pass(store, campaign.id)
    answers(None)

    said = pass_campaign.set_campaign(None, store, pass_.id)

    assert store.get_pass(pass_.id).campaign_id is None
    assert "no longer" in said


def test_choosing_the_campaign_already_recorded_says_nothing(qapp, store, answers) -> None:
    campaign = a_campaign(store)
    pass_ = a_pass(store, campaign.id)
    answers(campaign.id)

    assert pass_campaign.set_campaign(None, store, pass_.id) is None


def test_a_pass_that_has_gone_is_not_filed(qapp, store, answers) -> None:
    answers(uuid.uuid4())
    assert pass_campaign.set_campaign(None, store, uuid.uuid4()) is None


def test_a_withdrawn_campaign_still_names_the_trip(qapp, store) -> None:
    """Scenario: the console retires a campaign that already has work filed
    under it, and the tombstone arrives on the next pull.

    Expected behaviour: the pane still says which trip, rather than reading as
    unfiled work.
    """
    campaign_id = uuid.uuid4()
    store.apply_from_server("campaigns", [_pulled_campaign(campaign_id, seq=5)])
    pass_ = a_pass(store)
    pass_campaign.file_pass(store, pass_, campaign_id)

    store.apply_from_server(
        "campaigns", [_pulled_campaign(campaign_id, seq=7, deleted_at="2026-08-07T00:00:00+00:00")]
    )

    assert store.get_campaign(campaign_id) is None
    assert store.get_campaign_for_reference(campaign_id).name == "2026_08_fiji"


def _pulled_campaign(campaign_id: uuid.UUID, *, seq: int, deleted_at: str | None = None) -> dict:
    """One campaign row as the registry sends it, console-authored."""
    return {
        "id": str(campaign_id),
        "name": "2026_08_fiji",
        "begin_date": None,
        "end_date": None,
        "description": "",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": f"2026-08-0{seq}T00:00:00+00:00",
        "deleted_at": deleted_at,
        "device_id": None,
        "server_seq": seq,
    }


def test_every_place_a_pass_is_shown_offers_the_campaign(qapp) -> None:
    """Checked on the widgets rather than through a window, so a page that stops
    relaying the signal fails here rather than going quiet."""
    from deepreefmap_gui.runs.section_detail import SectionDetailPanel
    from deepreefmap_gui.runs.video_rows import SectionList, SectionRow, VideoLibraryList

    for owner in (SectionRow, SectionList, SectionDetailPanel):
        assert hasattr(owner, "campaign_requested"), owner.__name__
    assert hasattr(VideoLibraryList, "section_campaign")

    panel = SectionDetailPanel()
    # The menu is held: an unreferenced QMenu is collected and takes the very
    # QActions being read down with it.
    menu = QMenu(panel)
    labels = [a.text() for a in panel._fill_section_actions(menu).values()]
    assert pass_campaign.CAMPAIGN_ACTION in labels


def test_the_dialog_offers_the_blank_row_and_every_campaign(qapp, store) -> None:
    a_campaign(store, "2026_08_fiji")
    a_campaign(store, "2025_10_eritrea")

    dialog = pass_campaign.CampaignChoiceDialog(None, store)

    assert dialog.campaign.itemText(0) == "No campaign"
    assert dialog.campaign.itemData(0) is None
    assert dialog.campaign.count() == 3
    assert dialog.chosen() is None


def test_the_dialog_opens_on_the_campaign_the_pass_already_has(qapp, store) -> None:
    a_campaign(store, "2025_10_eritrea")
    campaign = a_campaign(store, "2026_08_fiji")

    dialog = pass_campaign.CampaignChoiceDialog(None, store, campaign.id)

    assert dialog.chosen() == campaign.id


def test_sites_and_campaigns_group_the_runs_filed_under_them(qapp, store) -> None:
    """Both facets bucket work that names neither, which is the ordinary case
    for a laptop that has never met a registry."""
    from deepreefmap_gui.survey import catalogue

    campaign = a_campaign(store)
    site = Site(name="Japanese Garden")
    store.add_site(site)

    entry = _bare_entry("run_001")
    entry.db_campaign_name = campaign.name
    entry.db_site_name = site.name
    entries = [entry]

    campaigns = catalogue.campaigns_facet(entries, [campaign])
    sites = catalogue.sites_facet(entries, [site])

    assert [g.title for g in campaigns] == [campaign.name]
    assert len(campaigns[0].entries) == 1
    assert [g.title for g in sites] == [site.name]

    unfiled = catalogue.campaigns_facet([_bare_entry("run_002")], [campaign])
    assert unfiled[0].title == catalogue.NO_CAMPAIGN_TITLE
    assert catalogue.sites_facet([_bare_entry("run_003")], [site])[0].title == catalogue.NO_SITE_TITLE


def _bare_entry(dir_name: str):
    """A scanned run with no database row attached yet."""
    from deepreefmap_gui.survey import catalogue

    return catalogue.RunEntry(
        run_dir=None,
        dir_name=dir_name,
        manifest={},
        display_name=dir_name,
        sort_key=0.0,
        video_hashes=[],
        video_name=None,
        begin_s=None,
        end_s=None,
        duration_s=None,
        points=None,
        manifest_run_id=None,
        manifest_pass_id=None,
        manifest_transect_id=None,
        manifest_transect_name=None,
        manifest_direction=None,
    )


def test_a_pass_row_says_which_trip_it_belongs_to(qapp, store) -> None:
    """The row's widths line its strip up with the clip's above it, so the trip
    is in the tooltip rather than in a column of its own."""
    from deepreefmap_gui.runs.video_rows import NO_CAMPAIGN_NAME, SectionRow

    campaign = a_campaign(store)
    pass_ = a_pass(store, campaign.id)

    row = SectionRow()
    row.set_section(pass_, transect_name="T1", campaign_name=campaign.name, status="succeeded")
    assert campaign.name in row.toolTip()

    row.set_section(pass_, transect_name="T1", campaign_name=None, status="succeeded")
    assert NO_CAMPAIGN_NAME in row.toolTip()
