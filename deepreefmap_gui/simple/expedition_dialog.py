"""The expedition's missing data as a checklist, each line a way to the editor that fills it."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.survey.completeness import (
    CLIP,
    EXPEDITION,
    EXPEDITION_DATES,
    LEVELS,
    PASS,
    TRANSECT,
    Gap,
    capture_span,
    missing_data,
)
from deepreefmap_gui.survey.store import SurveyStore

LEVEL_TITLES = {
    EXPEDITION: "Expedition",
    TRANSECT: "Transects",
    PASS: "Passes",
    CLIP: "Clips",
}
GAP_ROLE = Qt.ItemDataRole.UserRole
COMPLETE_NOTE = "Nothing missing: every transect, pass and clip of this expedition is filled in."
OPEN_HINT = "Double-click a line to open where it is filled in."


class MissingDataDialog(QDialog):
    """Everything an expedition still lacks, grouped by level.

    ``open_gap`` is the window's way to the right editor; the dialog stays open
    and refreshes when it returns, so a list can be worked down in one sitting.
    """

    def __init__(
        self,
        parent: QWidget | None,
        store: SurveyStore,
        campaign_id: uuid.UUID,
        open_gap: Callable[[Gap], None],
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._campaign_id = campaign_id
        self._open_gap = open_gap
        campaign = store.get_campaign(campaign_id)
        self.setWindowTitle(f"Missing data: {campaign.name if campaign else 'expedition'}")
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        self.summary = muted_label("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Record", "Missing"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setAccessibleName("Missing data by level")
        self.tree.itemActivated.connect(self._on_activated)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.dates_btn = QPushButton("Use clip dates for the expedition")
        self.dates_btn.setProperty("quiet", "true")
        self.dates_btn.setToolTip("Set the first and last day from the clips' recording times.")
        self.dates_btn.clicked.connect(self._fill_dates)
        actions.addWidget(self.dates_btn)
        actions.addStretch(1)
        # Non-modal: transects and passes are edited on their own pages, so the
        # list is refreshed by hand when coming back to it.
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setProperty("quiet", "true")
        self.refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_btn)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.refresh()

    def gaps(self) -> list[Gap]:
        return missing_data(self._store, self._campaign_id)

    def refresh(self) -> None:
        gaps = self.gaps()
        self.tree.clear()
        for level in LEVELS:
            at_level = [g for g in gaps if g.level == level]
            if not at_level:
                continue
            group = QTreeWidgetItem([f"{LEVEL_TITLES[level]} ({len(at_level)})", ""])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(group)
            for gap in at_level:
                item = QTreeWidgetItem([gap.subject, gap.item])
                item.setData(0, GAP_ROLE, gap)
                item.setToolTip(0, gap.subject)
                group.addChild(item)
            # The upper levels are short and decide the lower ones; clips can run
            # to hundreds of lines, so they start folded.
            group.setExpanded(level != CLIP)
        self.summary.setText(COMPLETE_NOTE if not gaps else f"{len(gaps)} item(s) missing. {OPEN_HINT}")
        begin, end = capture_span(self._store, self._campaign_id)
        dates_missing = any(g.code == EXPEDITION_DATES for g in gaps)
        self.dates_btn.setVisible(dates_missing and begin is not None)
        if begin is not None:
            self.dates_btn.setText(f"Use clip dates for the expedition ({begin} to {end})")

    def _fill_dates(self) -> None:
        campaign = self._store.get_campaign(self._campaign_id)
        begin, end = capture_span(self._store, self._campaign_id)
        if campaign is None or begin is None:
            return
        campaign.begin_date = campaign.begin_date or begin
        campaign.end_date = campaign.end_date or end
        self._store.update_campaign(campaign)
        self.refresh()

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        gap = item.data(0, GAP_ROLE)
        if isinstance(gap, Gap):
            self._open_gap(gap)
            self.refresh()
