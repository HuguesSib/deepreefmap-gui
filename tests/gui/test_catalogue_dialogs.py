"""Making and changing a site or a campaign on the laptop.

Both are pushed to the registry, and both carry fields this form does not show,
so an edit that rebuilt the row from its three inputs would quietly discard a
map point and mint a new id. That is what most of this is about.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialogButtonBox

from deepreefmap_gui.simple import catalogue_dialogs
from deepreefmap_gui.simple.catalogue_dialogs import (
    CampaignDialog,
    SiteDialog,
    combo_id,
    refill_campaigns,
    refill_sites,
)
from deepreefmap_gui.survey.models import Campaign, Site
from deepreefmap_gui.survey.ownership import READ_ONLY_NOTE
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")


def a_site(store, **fields) -> Site:
    site = Site(**{"name": "Japanese Garden", "country": "Djibouti", **fields})
    store.add_site(site)
    return site


def save(dialog) -> None:
    dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()


def test_a_new_site_reaches_the_store(qapp, store) -> None:
    dialog = SiteDialog(None, store)
    dialog.name_input.setText("Japanese Garden")
    dialog.country_input.setText("Djibouti")

    save(dialog)

    assert [s.name for s in store.list_sites()] == ["Japanese Garden"]
    assert dialog.site.country == "Djibouti"


def test_an_edit_keeps_what_the_form_does_not_show(qapp, store) -> None:
    """The map point, the description and the identity are all off this form."""
    site = a_site(store, latitude=11.6, longitude=43.1, description="Outer wall")

    dialog = SiteDialog(None, store, site)
    dialog.name_input.setText("Japanese Garden North")
    save(dialog)

    stored = store.get_site(site.id)
    assert stored.name == "Japanese Garden North"
    assert (stored.latitude, stored.longitude) == (11.6, 43.1)
    assert stored.description == "Outer wall"
    assert stored.id == site.id
    assert len(store.list_sites()) == 1


def test_an_edit_opens_on_what_is_recorded(qapp, store) -> None:
    site = a_site(store, region="Gulf of Tadjoura")

    dialog = SiteDialog(None, store, site)

    assert dialog.windowTitle() == "Edit site"
    assert dialog.name_input.text() == "Japanese Garden"
    assert dialog.region_input.text() == "Gulf of Tadjoura"


def test_a_name_another_site_holds_in_that_country_is_reported(qapp, store) -> None:
    """The registry keeps a site name unique within its country, so the same
    reef name on two coasts is fine and the same one twice is not."""
    a_site(store)

    dialog = SiteDialog(None, store)
    dialog.name_input.setText("Japanese Garden")
    dialog.country_input.setText("Djibouti")
    save(dialog)

    assert "already exists" in dialog.error.text()
    assert dialog.site is None
    assert len(store.list_sites()) == 1


def test_an_empty_name_is_refused_with_the_reason(qapp, store) -> None:
    dialog = SiteDialog(None, store)
    dialog.name_input.setText("   ")

    save(dialog)

    assert "empty" in dialog.error.text()
    assert store.list_sites() == []


def test_a_row_another_laptop_made_is_read_only(qapp, store) -> None:
    import uuid

    site = a_site(store)
    site.device_id = uuid.uuid4()

    dialog = SiteDialog(None, store, site)

    assert dialog.lock.text() == READ_ONLY_NOTE
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    assert dialog.name_input.isReadOnly()


def test_a_row_this_laptop_made_says_nothing_about_locks(qapp, store) -> None:
    dialog = SiteDialog(None, store, a_site(store))

    assert dialog.lock.text() == ""
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()


def test_a_new_campaign_reaches_the_store(qapp, store) -> None:
    dialog = CampaignDialog(None, store)
    dialog.name_input.setText("2026_08_fiji")
    dialog.begin_input.setText("2026-08-01")

    save(dialog)

    assert store.list_campaigns()[0].begin_date == "2026-08-01"


def test_a_campaign_edit_keeps_its_identity(qapp, store) -> None:
    campaign = Campaign(name="2026_08_fiji", begin_date="2026-08-01", description="Outer reefs")
    store.add_campaign(campaign)

    dialog = CampaignDialog(None, store, campaign)
    dialog.end_input.setText("2026-08-14")
    save(dialog)

    stored = store.get_campaign(campaign.id)
    assert (stored.begin_date, stored.end_date) == ("2026-08-01", "2026-08-14")
    assert stored.description == "Outer reefs"
    assert stored.id == campaign.id


def test_a_day_that_is_not_a_day_is_refused(qapp, store) -> None:
    dialog = CampaignDialog(None, store)
    dialog.name_input.setText("2026_08_fiji")
    dialog.begin_input.setText("August")

    save(dialog)

    assert "YYYY-MM-DD" in dialog.error.text()
    assert store.list_campaigns() == []


def test_the_boxes_lead_with_a_blank_row(qapp, store) -> None:
    """Neither is required of a transect or a pass, so nothing is preselected."""
    from PySide6.QtWidgets import QComboBox

    a_site(store)
    store.add_campaign(Campaign(name="2026_08_fiji"))

    sites, campaigns = QComboBox(), QComboBox()
    refill_sites(sites, store)
    refill_campaigns(campaigns, store)

    assert (sites.itemText(0), sites.itemData(0)) == (catalogue_dialogs.NO_SITE, None)
    assert (campaigns.itemText(0), campaigns.itemData(0)) == (catalogue_dialogs.NO_CAMPAIGN, None)
    assert combo_id(sites) is None
    assert combo_id(campaigns) is None


def test_a_refill_holds_the_selection_a_person_made(qapp, store) -> None:
    """Sites arrive by pull between visits to a page, so the list is rebuilt
    under a box that already has an answer in it."""
    from PySide6.QtWidgets import QComboBox

    site = a_site(store)
    combo = QComboBox()
    refill_sites(combo, store, site.id)
    assert combo_id(combo) == site.id

    a_site(store, name="Aqaba", country="Jordan")
    refill_sites(combo, store)

    assert combo_id(combo) == site.id
    assert combo.count() == 3


def test_a_refill_does_not_fire_the_signal_a_form_saves_on(qapp, store) -> None:
    """The forms behind these boxes save on ``activated``, and a rebuild that
    fired it would save a form nobody touched."""
    from PySide6.QtWidgets import QComboBox

    a_site(store)
    combo = QComboBox()
    fired = []
    combo.currentIndexChanged.connect(lambda *_: fired.append(1))
    combo.activated.connect(lambda *_: fired.append(1))

    refill_sites(combo, store)

    assert fired == []
