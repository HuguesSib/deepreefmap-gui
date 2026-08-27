"""Making and changing a site or a campaign in the field.

Both arrive at the registry unvalidated, for a curator to check; until then they
are this laptop's own rows like any other. A row the console authored stays
editable, and the change is sent as a proposal; a row another laptop made, or
one a curator has validated, is read-only here.

The combo helpers live here too, because every box that offers a site or a
campaign also offers to make one.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.theme import ERROR
from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.survey.models import Campaign, Site
from deepreefmap_gui.survey.ownership import lock_note, own_device_id, read_only
from deepreefmap_gui.survey.store import SurveyStore

SITE_NOTE = "A named place on a reef."
CAMPAIGN_NOTE = "One trip, named like 2025_10_eritrea."

NO_SITE = "No site"
NO_CAMPAIGN = "No campaign"

EDIT_SITE = "Edit this site"
EDIT_CAMPAIGN = "Edit this campaign"
# What the disabled button says when the box is on its blank row.
PICK_FIRST = "Pick one first."


class _CatalogueDialog(QDialog):
    def __init__(self, parent: QWidget | None, store: SurveyStore, title: str, note: str) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle(title)
        self.form = QFormLayout(self)
        hint = QLabel(note)
        hint.setWordWrap(True)
        self.form.addRow(hint)
        self.lock = muted_label("")
        self.lock.setWordWrap(True)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {ERROR};")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)

    def _finish(self, row: object | None, inputs: tuple[QLineEdit, ...]) -> None:
        """Close the form, and say who may change the row it was opened on.

        The inputs are named here rather than found by walking the layout: a
        subclass knows which of its widgets carry the row's fields.
        """
        self.form.addRow(self.lock)
        self.form.addRow(self.error)
        self.form.addRow(self.buttons)
        if row is None:
            self.lock.hide()
            return
        note = lock_note(row, own_device_id())
        self.lock.setText(note)
        self.lock.setVisible(bool(note))
        if not read_only(row, own_device_id()):
            return
        for line in inputs:
            line.setReadOnly(True)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setEnabled(False)

    def _save(self) -> None:
        raise NotImplementedError


class SiteDialog(_CatalogueDialog):
    """Name, country and region; the map point is the console's to add.

    Opened on a site to change it, or on nothing to make one. Editing replaces
    the row's three named fields and keeps the rest: a site carries a point, a
    description and its registry position, none of which this form shows.
    """

    def __init__(self, parent: QWidget | None, store: SurveyStore, site: Site | None = None) -> None:
        super().__init__(parent, store, "Edit site" if site is not None else "New site", SITE_NOTE)
        self._editing = site
        self.name_input = QLineEdit(site.name if site is not None else "")
        self.form.addRow("Name", self.name_input)
        self.country_input = QLineEdit(site.country or "" if site is not None else "")
        self.form.addRow("Country", self.country_input)
        self.region_input = QLineEdit(site.region or "" if site is not None else "")
        self.form.addRow("Region", self.region_input)
        self._finish(site, (self.name_input, self.country_input, self.region_input))
        self.site: Site | None = None

    def _save(self) -> None:
        name = self.name_input.text().strip()
        country = self.country_input.text().strip() or None
        region = self.region_input.text().strip() or None
        try:
            if self._editing is not None:
                # Replaced rather than rebuilt: a site carries a point, a
                # description and its registry position, none of them on show
                # here, and all of them lost by a fresh Site().
                site = replace(self._editing, name=name, country=country, region=region)
                self._store.update_site(site)
            else:
                site = Site(name=name, country=country, region=region)
                self._store.add_site(site)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        except sqlite3.IntegrityError:
            self.error.setText("A site of that name already exists in that country.")
            return
        self.site = site
        self.accept()


class CampaignDialog(_CatalogueDialog):
    """Name and the days it spans."""

    def __init__(self, parent: QWidget | None, store: SurveyStore, campaign: Campaign | None = None) -> None:
        title = "Edit campaign" if campaign is not None else "New campaign"
        super().__init__(parent, store, title, CAMPAIGN_NOTE)
        self._editing = campaign
        self.name_input = QLineEdit(campaign.name if campaign is not None else "")
        self.form.addRow("Name", self.name_input)
        self.begin_input = QLineEdit(campaign.begin_date or "" if campaign is not None else "")
        self.begin_input.setPlaceholderText("YYYY-MM-DD")
        self.form.addRow("First day", self.begin_input)
        self.end_input = QLineEdit(campaign.end_date or "" if campaign is not None else "")
        self.end_input.setPlaceholderText("YYYY-MM-DD")
        self.form.addRow("Last day", self.end_input)
        self._finish(campaign, (self.name_input, self.begin_input, self.end_input))
        self.campaign: Campaign | None = None

    def _save(self) -> None:
        try:
            name = self.name_input.text().strip()
            begin = _day(self.begin_input.text())
            end = _day(self.end_input.text())
            if self._editing is not None:
                campaign = replace(self._editing, name=name, begin_date=begin, end_date=end)
                self._store.update_campaign(campaign)
            else:
                campaign = Campaign(name=name, begin_date=begin, end_date=end)
                self._store.add_campaign(campaign)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        except sqlite3.IntegrityError:
            self.error.setText("A campaign of that name already exists.")
            return
        self.campaign = campaign
        self.accept()


def _day(text: str) -> str | None:
    """A YYYY-MM-DD day, or None for blank; anything else is refused."""
    from datetime import date

    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        raise ValueError(f"Not a day: {cleaned!r}. Use YYYY-MM-DD.") from None


# --- The boxes that offer them ----------------------------------------------


def _refill(combo: QComboBox, blank: str, rows: list, selected: uuid.UUID | None, keep: bool) -> None:
    """Restate a combo's rows, holding the selection across the rebuild.

    Signals are blocked because a refill is not a person's pick: the forms
    behind these boxes save on ``activated``, and a rebuild that fired it would
    save a form nobody touched.
    """
    wanted = str(selected) if selected is not None else (combo.currentData() if keep else None)
    combo.blockSignals(True)
    try:
        combo.clear()
        combo.addItem(blank, None)
        for row in rows:
            combo.addItem(row.name, str(row.id))
        if wanted is not None:
            combo.setCurrentIndex(max(0, combo.findData(str(wanted))))
    finally:
        combo.blockSignals(False)


def refill_sites(combo: QComboBox, store: SurveyStore, selected: uuid.UUID | None = None) -> None:
    """The known sites, blank first. Without a selection the current one is kept."""
    _refill(combo, NO_SITE, store.list_sites(), selected, keep=selected is None)


def refill_campaigns(combo: QComboBox, store: SurveyStore, selected: uuid.UUID | None = None) -> None:
    """The known campaigns, blank first. Without a selection the current one is kept."""
    _refill(combo, NO_CAMPAIGN, store.list_campaigns(), selected, keep=selected is None)


def combo_id(combo: QComboBox) -> uuid.UUID | None:
    """What a catalogue combo is pointing at, or None on the blank row."""
    data = combo.currentData()
    return uuid.UUID(str(data)) if data else None


def arm_edit(button: QToolButton, row: object | None, tooltip: str) -> None:
    """Offer the edit beside a catalogue box, where there is one to offer.

    A row another laptop authored, or one the console has validated, is not
    editable here, and the button says which rather than disappearing: a control
    that comes and goes is harder to read than one that explains itself.
    """
    if row is None:
        button.setEnabled(False)
        button.setToolTip(PICK_FIRST)
        return
    note = lock_note(row, own_device_id())
    button.setEnabled(not read_only(row, own_device_id()))
    button.setToolTip(note or tooltip)
