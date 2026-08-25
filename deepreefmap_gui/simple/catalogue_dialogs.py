"""Making a site or a campaign in the field.

Both arrive at the registry unvalidated, for a curator to check; until then they
are this laptop's own rows like any other.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from deepreefmap_gui.core.theme import ERROR
from deepreefmap_gui.survey.models import Campaign, Site
from deepreefmap_gui.survey.store import SurveyStore

SITE_NOTE = "A named place on a reef."
CAMPAIGN_NOTE = "One trip, named like 2025_10_eritrea."


class _CatalogueDialog(QDialog):
    def __init__(self, parent: QWidget | None, store: SurveyStore, title: str, note: str) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle(title)
        self.form = QFormLayout(self)
        hint = QLabel(note)
        hint.setWordWrap(True)
        self.form.addRow(hint)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {ERROR};")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)

    def _finish(self) -> None:
        self.form.addRow(self.error)
        self.form.addRow(self.buttons)

    def _save(self) -> None:
        raise NotImplementedError


class NewSiteDialog(_CatalogueDialog):
    """Name, country and region; the map point is the console's to add."""

    def __init__(self, parent: QWidget | None, store: SurveyStore) -> None:
        super().__init__(parent, store, "New site", SITE_NOTE)
        self.name_input = QLineEdit()
        self.form.addRow("Name", self.name_input)
        self.country_input = QLineEdit()
        self.form.addRow("Country", self.country_input)
        self.region_input = QLineEdit()
        self.form.addRow("Region", self.region_input)
        self._finish()
        self.site: Site | None = None

    def _save(self) -> None:
        try:
            site = Site(
                name=self.name_input.text().strip(),
                country=self.country_input.text().strip() or None,
                region=self.region_input.text().strip() or None,
            )
            self._store.add_site(site)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        except sqlite3.IntegrityError:
            self.error.setText("A site of that name already exists in that country.")
            return
        self.site = site
        self.accept()


class NewCampaignDialog(_CatalogueDialog):
    """Name and the days it spans."""

    def __init__(self, parent: QWidget | None, store: SurveyStore) -> None:
        super().__init__(parent, store, "New campaign", CAMPAIGN_NOTE)
        self.name_input = QLineEdit()
        self.form.addRow("Name", self.name_input)
        self.begin_input = QLineEdit()
        self.begin_input.setPlaceholderText("YYYY-MM-DD")
        self.form.addRow("First day", self.begin_input)
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText("YYYY-MM-DD")
        self.form.addRow("Last day", self.end_input)
        self._finish()
        self.campaign: Campaign | None = None

    def _save(self) -> None:
        try:
            campaign = Campaign(
                name=self.name_input.text().strip(),
                begin_date=_day(self.begin_input.text()),
                end_date=_day(self.end_input.text()),
            )
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
