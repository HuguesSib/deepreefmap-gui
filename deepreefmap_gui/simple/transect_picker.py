"""Filing a section: which transect it belongs to, and which way it was swum.

A transect is a place, so it is picked on a map rather than out of a combo box.
The map is the same widget the Transects page draws, holding the same overlays,
which is the whole of why this dialog can stay small: everything the small map
does not do is one click away on the page it borrows from.

A transect stays optional. A section is first a cutout of a video, and it
processes perfectly well without ever being filed.
"""

from __future__ import annotations

import sqlite3
import uuid

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import ICON_SM, crosshair_icon, direction_arrow_icon
from deepreefmap_gui.core.theme import ERROR, SPACE_SM, TREE_ROW_PAD_V
from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.map.overlays import OverlayTransect, transect_overlays
from deepreefmap_gui.map.slippy_map import SlippyMapWidget
from deepreefmap_gui.survey.models import (
    PASS_DIRECTIONS,
    Transect,
    compass_point,
    initial_bearing_deg,
)
from deepreefmap_gui.survey.models.importers import build_transect
from deepreefmap_gui.survey.store import SurveyStore

TRANSECT_ID_ROLE = Qt.ItemDataRole.UserRole

UNASSIGNED_LABEL = "Unassigned"
UNASSIGNED_NOTE = "Processes and shows up in Browse, but is not part of any transect."

# The map is a means of choosing here, not a page of its own, so it gets the
# smaller half of a dialog rather than the larger half of a window.
MAP_SIZE = QSize(360, 300)
LIST_WIDTH = 240

DRAW_HINT = "Click the start of the tape on the map, then its end."
DRAW_HINT_END = "Now click the end of the tape."
DRAW_HINT_DONE = "Save transect files it, and this section with it."

# What the map is, said where the map is. Nothing else on this dialog announces
# that it can be clicked, and a map that only responds once a button has been
# pressed reads as a picture until then.
MAP_HINT = "Transects are the lines on this map. Click one to file this section against it."
MAP_HINT_EMPTY = "No transects yet. New transect… draws one on the map."

# A choice made here is not a commitment. Said plainly, because filing a section
# is the step people hesitate over, and both halves of it are undoable from a
# page that is one click away.
EDIT_LATER_NOTE = (
    "Both can be changed later: a transect's ends, depth and notes on the "
    "Transects page, and which transect this section belongs to from Videos."
)

NEW_TRANSECT_TOOLTIP = (
    "Draw a transect on the map: click its start, then its end. The tape length "
    "and the name can be typed beside it."
)

OPEN_PAGE_LABEL = "Open in Transects ↗"
OPEN_PAGE_TOOLTIP = (
    "Leave this dialog and show the transect on the Transects page, where the "
    "ends can be dragged and the depth and notes filled in."
)

# With nothing picked the arrow still goes somewhere, and says so: the page is
# where transects are imported from a CSV or GPX file, which is how most surveys
# get theirs. The section is kept unfiled rather than discarded on the way.
OPEN_PAGE_EMPTY_LABEL = "Transects page ↗"
OPEN_PAGE_EMPTY_TOOLTIP = (
    "Leave this dialog and open the Transects page, where transects are drawn "
    "or imported. This section is kept, unfiled, and can be filed afterwards."
)


def direction_label(direction: str, transect: Transect | None) -> str:
    """"Forward", and the heading it means once there is a line to mean it on."""
    shown = direction.capitalize()
    if transect is None:
        return shown
    start = (transect.start_lat, transect.start_lon)
    end = (transect.end_lat, transect.end_lon)
    first, second = (start, end) if direction == "forward" else (end, start)
    bearing = initial_bearing_deg(*first, *second)
    return f"{shown} ({bearing:03.0f}° {compass_point(bearing)})"


class TransectPickerDialog(QDialog):
    """Pick the transect a section belongs to, and the direction it was swum.

    Answers through ``choice()``: (transect_id | None, direction), the same
    contract the call sites had before there was a map in here.
    """

    open_transect_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None,
        store: SurveyStore,
        *,
        transect_id: uuid.UUID | None = None,
        direction: str | None = None,
        ok_label: str = "Save",
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._transects: list[Transect] = []
        self._drawing = False
        self._pending_start: tuple[float, float] | None = None
        # The arrow rejects the dialog on its way out, which is indistinguishable
        # from Cancel to the caller. This is what tells them apart, so a section
        # cut but not yet filed can be kept rather than lost on the trip.
        self.left_for_page = False
        self.setWindowTitle("File this section")

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.setSpacing(SPACE_SM)

        # The hint sits under the map rather than in the side column: it is
        # about the map, and it is the only thing that says the map is a control.
        map_column = QVBoxLayout()
        map_column.setSpacing(SPACE_SM)
        self.map = SlippyMapWidget()
        self.map.setMinimumSize(MAP_SIZE)
        self.map.transect_clicked.connect(self._on_map_transect_clicked)
        self.map.map_clicked.connect(self._on_map_clicked)
        map_column.addWidget(self.map, 1)
        self.map_hint = muted_label("")
        self.map_hint.setWordWrap(True)
        map_column.addWidget(self.map_hint)
        top.addLayout(map_column, 1)

        side = QVBoxLayout()
        side.setSpacing(SPACE_SM)
        self.list = QListWidget()
        self.list.setMinimumWidth(LIST_WIDTH)
        self.list.currentItemChanged.connect(lambda *_: self._on_selection_changed())
        side.addWidget(self.list, 1)

        self.direction = QComboBox()
        self.direction.setToolTip(
            "Which way the tape was swum. It is what tells two passes of one "
            "transect apart when their results are compared."
        )
        direction_row = QFormLayout()
        direction_row.setContentsMargins(0, 0, 0, 0)
        direction_row.addRow("Direction", self.direction)
        # Only where the registry has sent expeditions to choose from: a survey
        # that has never synced has no campaigns, and an empty combo would be a
        # question with no answers.
        self.campaign = QComboBox()
        self.campaign.setToolTip(
            "The expedition this section was recorded on. Remembered as the "
            "default for the next section."
        )
        self._campaigns = self._store.list_campaigns()
        if self._campaigns:
            self.campaign.addItem("No campaign", None)
            for campaign in self._campaigns:
                self.campaign.addItem(campaign.name, str(campaign.id))
            default = self._store.default_campaign_id()
            if default is not None:
                index = self.campaign.findData(str(default))
                self.campaign.setCurrentIndex(max(0, index))
            direction_row.addRow("Campaign", self.campaign)
        side.addLayout(direction_row)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(SPACE_SM)
        self.new_btn = QPushButton("New transect…")
        self.new_btn.setProperty("quiet", "true")
        # The same crosshair the Transects page draws with, so the two read as
        # one action in two places rather than two ways of doing something.
        self.new_btn.setIcon(crosshair_icon(ICON_SM))
        self.new_btn.setToolTip(NEW_TRANSECT_TOOLTIP)
        self.new_btn.clicked.connect(self._start_new_transect)
        buttons_row.addWidget(self.new_btn)
        self.open_btn = QPushButton(OPEN_PAGE_LABEL)
        self.open_btn.setProperty("quiet", "true")
        self.open_btn.setToolTip(OPEN_PAGE_TOOLTIP)
        self.open_btn.clicked.connect(self._on_open_page)
        buttons_row.addWidget(self.open_btn)
        buttons_row.addStretch(1)
        side.addLayout(buttons_row)

        side.addWidget(self._build_new_form())

        self.note = muted_label("")
        self.note.setWordWrap(True)
        side.addWidget(self.note)
        top.addLayout(side)
        layout.addLayout(top)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_label)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._fill(selected=transect_id)
        self._fill_directions(direction or PASS_DIRECTIONS[0])
        self.map.fit_transects()

    # --- the new-transect strip ---------------------------------------------

    def _build_new_form(self) -> QWidget:
        """The few fields a transect needs to exist, and no more.

        Depth, notes and moving the ends are the Transects page's job; a picker
        that grew them would be that page again, differently.
        """
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(0, 0, 0, 0)
        self.name_input = QLineEdit()
        form.addRow("Name", self.name_input)
        # Only where the registry has sent sites: names are unique per site, so
        # a T1 on one reef can coexist with a T1 on another.
        self.site_combo = QComboBox()
        self._sites = self._store.list_sites()
        if self._sites:
            self.site_combo.addItem("No site", None)
            for site in self._sites:
                self.site_combo.addItem(site.name, str(site.id))
            form.addRow("Site", self.site_combo)
        self.start_input = QLineEdit()
        self.start_input.setPlaceholderText("-17.5005, 177.1005")
        form.addRow("Start point", self.start_input)
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText("-17.5010, 177.1010")
        form.addRow("End point", self.end_input)
        # Directly under the two boxes it is filling, which is where the eye
        # already is once drawing has started.
        self.draw_hint = muted_label("")
        self.draw_hint.setWordWrap(True)
        form.addRow(self.draw_hint)
        self.length_input = QDoubleSpinBox()
        self.length_input.setRange(0.0, 10_000.0)
        self.length_input.setSuffix(" m")
        self.length_input.setToolTip(
            "The tape reading. Without it the transect's runs are not scaled."
        )
        form.addRow("Tape length", self.length_input)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {ERROR};")
        form.addRow(self.error)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.cancel_new_btn = QPushButton("Cancel")
        self.cancel_new_btn.setProperty("quiet", "true")
        self.cancel_new_btn.clicked.connect(self._end_new_transect)
        save_row.addWidget(self.cancel_new_btn)
        self.save_new_btn = QPushButton("Save transect")
        self.save_new_btn.clicked.connect(self._save_new_transect)
        save_row.addWidget(self.save_new_btn)
        form.addRow(save_row)
        self._new_panel = panel
        panel.setVisible(False)
        return panel

    def _start_new_transect(self) -> None:
        from deepreefmap_gui.simple.plan import next_transect_name

        self._drawing = True
        self._pending_start = None
        self.name_input.setText(next_transect_name([t.name for t in self._transects]))
        self.start_input.clear()
        self.end_input.clear()
        self.length_input.setValue(0.0)
        self.error.setText("")
        self._new_panel.setVisible(True)
        self.new_btn.setEnabled(False)
        # Picking is armed straight away: the fields are typeable, but the point
        # of doing this on a map is that nobody has to type a coordinate.
        self.map.set_pick_mode(True)
        self.map.set_pending_start(None)
        self.draw_hint.setText(DRAW_HINT)
        self.map_hint.setText(DRAW_HINT)
        self.name_input.setFocus()

    def _end_new_transect(self) -> None:
        self._drawing = False
        self._pending_start = None
        self._new_panel.setVisible(False)
        self.new_btn.setEnabled(True)
        self.map.set_pick_mode(False)
        self.map.set_pending_start(None)
        self.draw_hint.setText("")
        self._refresh_note()

    def _save_new_transect(self) -> None:
        site_data = self.site_combo.currentData()
        try:
            transect = build_transect(
                self.name_input.text(),
                self.start_input.text(),
                self.end_input.text(),
                length_m=self.length_input.value(),
                site_id=uuid.UUID(str(site_data)) if site_data else None,
            )
            self._store.add_transect(transect)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        except sqlite3.IntegrityError:
            self.error.setText(
                f"A transect named {self.name_input.text().strip()!r} already "
                "exists on that site."
            )
            return
        self._end_new_transect()
        self._fill(selected=transect.id)
        self.map.focus_on([(transect.start_lat, transect.start_lon),
                           (transect.end_lat, transect.end_lon)])

    # --- the map -------------------------------------------------------------

    def _on_map_clicked(self, lat: float, lon: float) -> None:
        """Two clicks make a line: the first is the start, the second the end."""
        if not self._drawing:
            return
        if self._pending_start is None:
            self._pending_start = (lat, lon)
            self.start_input.setText(f"{lat:.6f}, {lon:.6f}")
            self.map.set_pending_start((lat, lon))
            self.draw_hint.setText(DRAW_HINT_END)
            self.map_hint.setText(DRAW_HINT_END)
            return
        self.end_input.setText(f"{lat:.6f}, {lon:.6f}")
        self.map.set_pending_start(None)
        self._pending_start = None
        self.draw_hint.setText(DRAW_HINT_DONE)
        self.map_hint.setText(DRAW_HINT_DONE)

    def _on_map_transect_clicked(self, transect_id: str) -> None:
        if self._drawing:
            return
        self._select(transect_id)

    # --- the list ------------------------------------------------------------

    def _fill(self, selected: uuid.UUID | None) -> None:
        self._transects = self._store.list_transects()
        self.list.blockSignals(True)
        try:
            self.list.clear()
            self._add_row(
                UNASSIGNED_LABEL, "Not filed against any line", "", UNASSIGNED_NOTE
            )
            for transect in self._transects:
                self._add_row(
                    transect.name, self._subtitle(transect), str(transect.id), ""
                )
        finally:
            self.list.blockSignals(False)
        self._select(str(selected) if selected is not None else "")

    def _add_row(self, title: str, subtitle: str, transect_id: str, tooltip: str) -> None:
        """One entry, two lines, every row the same height.

        The height is set here rather than left to Qt: an item's own hint counts
        the text and not the padding the stylesheet adds, so the selection fill
        was drawn taller than the row and bled over its neighbours.
        """
        item = QListWidgetItem(f"{title}\n{subtitle}")
        item.setData(TRANSECT_ID_ROLE, transect_id)
        if tooltip:
            item.setToolTip(tooltip)
        metrics = self.list.fontMetrics()
        item.setSizeHint(QSize(0, metrics.lineSpacing() * 2 + 2 * TREE_ROW_PAD_V))
        self.list.addItem(item)

    def _subtitle(self, transect: Transect) -> str:
        from deepreefmap_gui.simple.plan import bearing_text, transect_length_text

        return (
            f"{transect_length_text(transect.length_m, transect.geodesic_length_m())}"
            f"  ·  {bearing_text(transect.start_lat, transect.start_lon, transect.end_lat, transect.end_lon)}"
        )

    def _select(self, transect_id: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is not None and str(item.data(TRANSECT_ID_ROLE)) == transect_id:
                self.list.setCurrentItem(item)
                return
        self.list.setCurrentRow(0)

    def _on_selection_changed(self) -> None:
        self._refresh_overlays()
        self._fill_directions(self.direction.currentData() or PASS_DIRECTIONS[0])
        self._refresh_note()

    def _refresh_overlays(self) -> None:
        chosen = self.selected_transect_id()
        overlays: list[OverlayTransect] = transect_overlays(self._store, chosen)
        self.map.set_transects(overlays)

    def _fill_directions(self, keep: str) -> None:
        """Relabel forward and reverse with the headings they mean.

        "Forward" on its own says nothing about the water; against a line with
        two ends it is a compass bearing, which is what a diver was briefed on.
        """
        transect = self.selected_transect()
        self.direction.blockSignals(True)
        try:
            self.direction.clear()
            for name in PASS_DIRECTIONS:
                self.direction.addItem(
                    direction_arrow_icon(name), direction_label(name, transect), name
                )
            index = self.direction.findData(keep)
            self.direction.setCurrentIndex(max(0, index))
        finally:
            self.direction.blockSignals(False)

    def _refresh_note(self) -> None:
        if self._drawing:
            return
        transect = self.selected_transect()
        self.open_btn.setText(OPEN_PAGE_LABEL if transect else OPEN_PAGE_EMPTY_LABEL)
        self.open_btn.setToolTip(
            OPEN_PAGE_TOOLTIP if transect else OPEN_PAGE_EMPTY_TOOLTIP
        )
        self.map_hint.setText(MAP_HINT if self._transects else MAP_HINT_EMPTY)
        # The standing note always says the choice is reversible; being
        # unassigned is the extra thing worth saying when nothing is picked.
        parts = [UNASSIGNED_NOTE] if transect is None else []
        parts.append(EDIT_LATER_NOTE)
        self.note.setText(" ".join(parts))

    def _on_open_page(self) -> None:
        """Hand the transect to the page that can do everything to it.

        Open with nothing picked too. A survey with no transects yet is exactly
        when somebody needs that page, and refusing the trip until one exists
        made the way to make one the one thing that could not be reached.
        """
        transect_id = self.selected_transect_id()
        self.left_for_page = True
        self.open_transect_requested.emit("" if transect_id is None else str(transect_id))
        self.reject()

    # --- the answer ----------------------------------------------------------

    def selected_transect_id(self) -> uuid.UUID | None:
        item = self.list.currentItem()
        data = str(item.data(TRANSECT_ID_ROLE)) if item is not None else ""
        return uuid.UUID(data) if data else None

    def selected_transect(self) -> Transect | None:
        wanted = self.selected_transect_id()
        if wanted is None:
            return None
        return next((t for t in self._transects if t.id == wanted), None)

    def choice(self) -> tuple[uuid.UUID | None, str]:
        return self.selected_transect_id(), str(
            self.direction.currentData() or PASS_DIRECTIONS[0]
        )

    def campaign_choice(self) -> uuid.UUID | None:
        """The expedition picked here, remembered as the survey's new default."""
        data = self.campaign.currentData()
        chosen = uuid.UUID(str(data)) if data else None
        self._store.set_default_campaign(chosen)
        return chosen
