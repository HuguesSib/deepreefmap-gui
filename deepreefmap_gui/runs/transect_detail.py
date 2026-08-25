"""What a transect's runs are, and the way through to the transect itself."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton, QWidget

from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import STATUS_COLORS, muted_label
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.survey.catalogue import FacetGroup, entry_status


class TransectDetailPanel(DetailCard):
    """A titled card summarising the selected transect's runs."""

    open_transect_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        note = muted_label("Open in Transects for cover, passes and the map.")
        note.setWordWrap(True)
        self.body.addWidget(note)
        self.body.addStretch(1)
        self.open_btn = QPushButton("Open in Transects")
        self.open_btn.clicked.connect(self.open_transect_requested)
        self.add_actions(self.open_btn)

    def show_group(self, group: FacetGroup) -> None:
        entries = group.all_entries()
        statuses = [entry_status(e) for e in entries]
        failed = sum(1 for s in statuses if s == "failed")
        if failed:
            self.set_status(f"{failed} of {len(statuses)} failed", STATUS_COLORS.get("failed", TEXT_MUTED))
        elif statuses:
            self.set_status(f"{len(statuses)} processed", STATUS_COLORS.get("succeeded", TEXT_MUTED))
        else:
            self.set_status("Nothing processed yet", TEXT_MUTED)

        self.title.setText(group.title)
        # A rerun is another run of the same pass, so the two counts differ.
        passes = len(group.children) or len(entries)
        self.facts.set_rows(
            [
                ("Passes", str(passes)),
                ("Runs", str(len(entries))),
            ]
        )

    def clear(self) -> None:
        super().clear()
        self.set_status("", TEXT_MUTED)
