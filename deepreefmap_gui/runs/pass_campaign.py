"""Filing a pass under a campaign, from wherever a pass is shown.

A campaign is the trip the swim belongs to, and it reaches the registry:
``campaign_id`` is a pushed column, so the console groups by what was chosen
here. It could only be answered inside the transect picker, which is the dialog
for cutting a pass rather than for looking at one afterwards.

Nothing local reads the answer, so a pass with no campaign is a pass, not an
error, and the box opens on that blank row.
"""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QWidget

from deepreefmap_gui.core.widgets import ok_cancel_row
from deepreefmap_gui.simple.catalogue_dialogs import CampaignDialog, combo_id, refill_campaigns
from deepreefmap_gui.survey.models import TransectPass
from deepreefmap_gui.survey.store import SurveyStore

CAMPAIGN_ACTION = "Set campaign…"
_TITLE = "Set campaign"
_NEW = "New campaign…"


class CampaignChoiceDialog(QDialog):
    """One box and a way to add to it.

    ``chosen`` is read after the dialog closes rather than returned, so one
    dialog can file a whole selection.
    """

    def __init__(self, parent: QWidget | None, store: SurveyStore, selected: uuid.UUID | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle(_TITLE)
        form = QFormLayout(self)
        self.campaign = QComboBox()
        self.campaign.setToolTip("The trip this pass was recorded on.")
        form.addRow("Campaign", self.campaign)
        refill_campaigns(self.campaign, store, selected)
        buttons = ok_cancel_row(self)
        new = buttons.addButton(_NEW, buttons.ButtonRole.ActionRole)
        new.setProperty("quiet", "true")
        new.clicked.connect(self._on_new)
        form.addRow(buttons)

    def _on_new(self) -> None:
        dialog = CampaignDialog(self, self._store)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.campaign is not None:
            refill_campaigns(self.campaign, self._store, dialog.campaign.id)

    def chosen(self) -> uuid.UUID | None:
        return combo_id(self.campaign)


def set_campaign(parent: QWidget, store: SurveyStore, pass_id: uuid.UUID) -> str | None:
    """Ask which campaign this pass belongs to and store the answer.

    Returns a sentence for the status bar, or None when the dialog was
    cancelled or the answer was the one already recorded.
    """
    pass_ = store.get_pass(pass_id)
    if pass_ is None:
        return None
    dialog = CampaignChoiceDialog(parent, store, pass_.campaign_id)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return file_pass(store, pass_, dialog.chosen())


def file_pass(store: SurveyStore, pass_: TransectPass, campaign_id: uuid.UUID | None) -> str | None:
    """Record one pass against one campaign, saying what changed."""
    if pass_.campaign_id == campaign_id:
        return None
    pass_.campaign_id = campaign_id
    store.update_pass(pass_)
    if campaign_id is None:
        return "This pass is no longer filed under a campaign."
    campaign = store.get_campaign_for_reference(campaign_id)
    return f"This pass is filed under {campaign.name!r}." if campaign is not None else None
