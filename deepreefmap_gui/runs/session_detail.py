"""What one session is: the passes queued together, and what they ran under.

A session is the only container in the model that spans transects, and it was
the one with nowhere to appear. Every run records it already, on its pass and in
its manifest, which is also what lets a copied output folder be traced back to
the day it came from.

Shown in Browse when the grouping is by session and a session is selected.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPushButton, QWidget

from deepreefmap_gui.core.icons import status_dot_icon
from deepreefmap_gui.core.theme import TEXT_MUTED
from deepreefmap_gui.core.widgets import STATUS_COLORS, muted_label
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.survey.catalogue import FacetGroup, entry_status, session_summary


def _outcome(entries: list) -> tuple[str, str]:
    """A session's verdict, taken from the worst of its runs."""
    statuses = [entry_status(entry) for entry in entries]
    if not statuses:
        return "Nothing processed", "queued"
    failed = sum(1 for s in statuses if s == "failed")
    if failed:
        return f"{failed} of {len(statuses)} failed", "failed"
    unfinished = sum(1 for s in statuses if s != "succeeded")
    if unfinished:
        return f"{unfinished} unfinished", "running"
    return "All succeeded", "succeeded"


class SessionDetailPanel(DetailCard):
    """A titled card describing the selected session."""

    audit_requested = Signal()
    delete_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body

        layout.addWidget(muted_label("Passes processed in this session"))
        self.pass_list = QListWidget()
        self.pass_list.setAlternatingRowColors(True)
        # Elide rather than scroll: the outcome sits at the end of the line.
        self.pass_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.pass_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.pass_list, 1)

        self.audit_btn = QPushButton("Settings used…")
        self.audit_btn.setToolTip("The settings each run used, against the standard.")
        self.audit_btn.clicked.connect(self.audit_requested)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setToolTip("Remove this session's output data, its records, or both")
        self.delete_btn.clicked.connect(self.delete_requested)
        self.add_actions(self.audit_btn, self.delete_btn)

        self._entries: list = []

    @property
    def entries(self) -> list:
        return self._entries

    def show_group(self, group: FacetGroup) -> None:
        entries = group.all_entries()
        self._entries = entries
        # Unfiled runs have no session row to delete; the group is a shelf,
        # not a thing.
        self.delete_btn.setEnabled(group.key[0] == "session")
        label, status_key = _outcome(entries)
        self.title.setText(group.title)
        self.set_status(label, STATUS_COLORS.get(status_key, TEXT_MUTED))

        rows = [("Covered", session_summary(group))]
        # From a run's manifest, which holds what the session actually ran under.
        preset = next(
            (
                e.manifest.get("survey", {}).get("preset_name")
                for e in entries
                if e.manifest.get("survey", {}).get("preset_name")
            ),
            None,
        )
        if preset:
            rows.append(("Settings", str(preset)))
        transects = sorted({e.transect_name for e in entries if e.transect_name})
        if transects:
            rows.append(("Transects", ", ".join(transects)))
        self.facts.set_rows(rows)

        self.pass_list.clear()
        for entry in sorted(entries, key=lambda e: e.sort_key):
            status = entry_status(entry)
            name = entry.transect_name or "No transect"
            item = QListWidgetItem(f"{name}  ·  {entry.video_name or 'unknown video'}  ·  {status}")
            item.setIcon(status_dot_icon(STATUS_COLORS.get(status, TEXT_MUTED)))
            self.pass_list.addItem(item)

    def clear(self) -> None:
        self._entries = []
        self.title.setText("")
        self.set_status("", TEXT_MUTED)
        self.facts.clear()
        self.pass_list.clear()
