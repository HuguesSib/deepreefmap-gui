"""Transects: create, edit and import the lines a survey runs over."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.fonts import BASE_POINT_SIZE, MONO_FONT_FAMILY
from deepreefmap_gui.core.icons import ICON_MD, ICON_SM, check_icon, copy_icon, crosshair_icon
from deepreefmap_gui.core.theme import (
    BORDER,
    GUTTER,
    PRIMARY,
    RADIUS,
    SPLIT_MIN_TOTAL,
    TEXT_DIM,
)
from deepreefmap_gui.core.widgets import (
    SCOPE_FILTERS,
    ColumnSpec,
    EmptyState,
    FilterChips,
    SortableTreeItem,
    enable_sorting,
    install_column_sizer,
    muted_label,
    secondary_label,
    section_card,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.map.overlays import OverlayTransect
from deepreefmap_gui.map.slippy_map import SlippyMapWidget
from deepreefmap_gui.simple.section_state import transects_state
from deepreefmap_gui.survey.models import (
    Transect,
    compass_point,
    haversine_m,
    initial_bearing_deg,
)
from deepreefmap_gui.survey.models.exporters import save_transects_csv
from deepreefmap_gui.survey.models.importers import (
    build_transect,
    import_transects_csv,
    import_transects_gpx,
    parse_latlon,
)

logger = logging.getLogger(__name__)

# A trailing spacer column absorbs the slack instead of the name column, so the
# figures stay beside the transect they belong to however wide the window is.
PLAN_COLUMNS = ("Transect", "Length", "Depth", "Passes", "Runs", "")
PLAN_SPACER_COLUMN = len(PLAN_COLUMNS) - 1

# Content-sized columns let one long transect name push Depth, Passes and Runs
# clean off the viewport, so the figures get their reading width first and the
# name takes what is left. Length is the one that drops on a narrow pane; its
# value, like every other, is in `transect_tooltip`.
_PLAN_COLUMN_SPEC = ColumnSpec(
    fixed={2: 64, 3: 62, 4: 56},
    weights={0: 3, PLAN_SPACER_COLUMN: 1},
    minimums={0: 140, PLAN_SPACER_COLUMN: 0},
    optional=((1, 78),),
)
# The transect a click on the map is about to draw, and the one being typed into
# the form, share this row id: neither exists in the store yet.
DRAFT_ID = "draft"
# How much of the map a transect fills when it is picked from the list. Short of
# the whole viewport so the reef either side of it stays on screen.
FOCUS_FILL = 0.6

# Wide enough for the cover chart and its six-column table.
PLAN_ANALYSIS_MIN_WIDTH = 460

# How much of the page width the analysis column takes, and how much of the
# working column's height the transect list takes under the map and the form.
_PLAN_ANALYSIS_SHARE = 0.34
_PLAN_LIST_SHARE = 0.42
_PLAN_LIST_MIN_HEIGHT = 220

_PLAN_SCOPE_TOOLTIP = (
    "In view lists only the transects the map is showing, and follows the map "
    "as it is panned and zoomed. The transect being edited stays listed either "
    "way, so the form and the list cannot disagree."
)


def transect_length_text(length_m: float | None, geodesic_m: float) -> str:
    """The one length worth showing for a transect.

    A typed tape length is the cable actually laid on the reef and is what the
    run is scaled to, so it supersedes the straight-line distance between the
    GPS endpoints; the geodesic only stands in when no tape length was recorded.
    """
    if length_m:
        return f"{length_m:g} m tape"
    return f"{geodesic_m:.0f} m GPS"


def bearing_text(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Heading from start to end, as a diver would be briefed to swim it."""
    bearing = initial_bearing_deg(lat1, lon1, lat2, lon2)
    return f"{bearing:03.0f}° {compass_point(bearing)}"


def transect_geometry_text(transect: Transect) -> str:
    """The derived line under the coordinate fields: how far, and which way."""
    return (
        f"{transect.geodesic_length_m():.0f} m between the GPS ends  ·  heading "
        f"{bearing_text(transect.start_lat, transect.start_lon, transect.end_lat, transect.end_lon)}"
    )


def transect_row_columns(transect: Transect, passes: int, runs: int) -> list[str]:
    """One row of the transect table, column by column.

    Derived from the dataclass and two counts with no per-row store query,
    because the table is rebuilt on every keystroke while a transect is typed.
    """
    return [
        transect.name,
        transect_length_text(transect.length_m, transect.geodesic_length_m()),
        f"{transect.depth_m:g} m" if transect.depth_m else "—",
        str(passes) if passes else "—",
        str(runs) if runs else "—",
        "",
    ]


def transect_row_values(transect: Transect, passes: int, runs: int) -> dict[int, object]:
    """The sort value behind each cell of `transect_row_columns`.

    "9 m" sorts above "12 m" as text, so the numeric columns carry their raw
    numbers. An absent value is left out entirely, which sinks the cell under
    SortableTreeItem's contract.
    """
    values: dict[int, object] = {
        0: transect.name.lower(),
        1: transect.length_m or transect.geodesic_length_m(),
    }
    if transect.depth_m:
        values[2] = transect.depth_m
    if passes:
        values[3] = passes
    if runs:
        values[4] = runs
    return values


def transect_tooltip(transect: Transect, passes: int = 0, runs: int = 0) -> str:
    """Coordinates, heading and notes, which the row itself has no room for."""
    lines = [
        f"<b>{transect.name}</b>",
        f"Start {transect.start_lat:.5f}, {transect.start_lon:.5f}",
        f"End {transect.end_lat:.5f}, {transect.end_lon:.5f}",
        transect_geometry_text(transect),
    ]
    if transect.length_m:
        lines.append(f"{transect.length_m:g} m tape laid")
    if transect.depth_m:
        lines.append(f"{transect.depth_m:g} m deep")
    lines.append(f"{passes} pass(es) assigned, {runs} processed")
    if transect.description:
        lines.append(f"<i>{transect.description}</i>")
    return "<br>".join(lines)


def next_transect_name(existing: Iterable[str], stem: str = "Transect") -> str:
    """First ``stem N`` not already taken.

    Names are unique in the store, so a new transect arrives already named and
    saveable; the field worker renames it only if they have a better name.
    """
    used = {name.strip().casefold() for name in existing}
    number = 1
    while f"{stem} {number}".casefold() in used:
        number += 1
    return f"{stem} {number}"


def _field_label(text: str, top: bool = False) -> QLabel:
    """Muted, right-aligned caption, so the column of inputs reads as one edge."""
    label = muted_label(text)
    align = Qt.AlignmentFlag.AlignRight | (
        Qt.AlignmentFlag.AlignTop if top else Qt.AlignmentFlag.AlignVCenter
    )
    label.setAlignment(align)
    return label


def _coord_edit() -> QLineEdit:
    """Coordinate field, set in the mono face so digits line up between the two
    ends and a transposed decimal point is visible."""
    edit = QLineEdit()
    edit.setPlaceholderText("lat, lon")
    font = QFont(MONO_FONT_FAMILY)
    font.setPointSize(BASE_POINT_SIZE - 1)
    edit.setFont(font)
    return edit


def _framed(inner: QWidget) -> QWidget:
    """Put a hairline and rounded corners around a widget that paints its own
    content, so the map stops bleeding into the page background."""
    frame = QWidget()
    frame.setObjectName("mapFrame")
    frame.setStyleSheet(
        f"QWidget#mapFrame {{ border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.addWidget(inner)
    return frame


class OptionalMetresSpinBox(QDoubleSpinBox):
    """Metres, or nothing recorded.

    The unset state is a dash rather than a confident 0.0 m, dimmed so an
    unrecorded depth does not read as measured data.
    """

    def __init__(self, maximum: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0.0, maximum)
        self.setDecimals(1)
        self.setSuffix(" m")
        self.setSpecialValueText("—")
        self.valueChanged.connect(lambda _: self._restyle())
        self._restyle()

    def _restyle(self) -> None:
        unset = self.value() <= self.minimum()
        self.setStyleSheet(f"color: {TEXT_DIM};" if unset else "")


class NotesEdit(QPlainTextEdit):
    """Multi-line notes that commit on focus-out.

    QPlainTextEdit has no editingFinished, and the transect form autosaves on
    field exit, so the signal is supplied here.
    """

    editing_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Anything worth remembering about this transect")
        self.setTabChangesFocus(True)
        # A range, not a fixed height, so the form gives room back to the map and
        # the list on a short window.
        self.setMinimumHeight(48)
        self.setMaximumHeight(96)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusOutEvent(event)
        self.editing_finished.emit()


class SimplePlanMixin(MixinBase):
    """DeepReefMapWindow methods for the transect planning tab."""

    _transect_form_id: uuid.UUID | None = None
    _pick_stage: str | None = None
    _transect_editing: bool = False
    _plan_map_fitted: bool = False
    _plan_list_rebuilding: bool = False
    _plan_visible_ids: tuple[str, ...] = ()
    # All by default: the map arrives fitted to the whole survey, so In view
    # would start as a filter that filters nothing and only begins to mean
    # something once the map has been moved.
    _plan_scope_filter: str = "all"

    def _build_plan_page(self) -> QWidget:
        """The map beside the transect editor. The archive is Browse's."""
        page = QSplitter(Qt.Orientation.Horizontal)
        page.setHandleWidth(GUTTER)
        work = QSplitter(Qt.Orientation.Vertical)
        work.setHandleWidth(GUTTER)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setHandleWidth(GUTTER)

        map_pane = QWidget()
        map_layout = QVBoxLayout(map_pane)
        map_layout.setContentsMargins(0, 0, 0, 0)
        self._plan_map = SlippyMapWidget()
        self._plan_map.map_clicked.connect(self._on_plan_map_clicked)
        self._plan_map.transect_clicked.connect(self._on_plan_map_transect_clicked)
        self._plan_map.transect_endpoint_moved.connect(self._on_plan_endpoint_moved)
        self._plan_view_timer = QTimer(self)
        self._plan_view_timer.setSingleShot(True)
        self._plan_view_timer.setInterval(60)
        self._plan_view_timer.timeout.connect(self._apply_plan_view_change)
        self._plan_map.view_changed.connect(self._on_plan_view_changed)
        # Tiles reach the disk cache by being drawn, so browsing a site before
        # leaving is what keeps it drawable at sea. Nothing to press.
        map_layout.addWidget(_framed(self._plan_map), 1)

        transects_group, group_layout = section_card("Transects")
        # The map as a filter over the list, rather than as a second copy of it.
        self._plan_scope_chips = FilterChips(SCOPE_FILTERS)
        self._plan_scope_chips.setToolTip(_PLAN_SCOPE_TOOLTIP)
        self._plan_scope_chips.set_current(self._plan_scope_filter)
        self._plan_scope_chips.changed.connect(self._on_plan_scope_changed)
        self._plan_scope_chips.setVisible(False)
        scope_row = QHBoxLayout()
        scope_row.setContentsMargins(0, 0, 0, 0)
        scope_row.addWidget(self._plan_scope_chips)
        scope_row.addStretch(1)
        group_layout.addLayout(scope_row)
        self._transect_list = QTreeWidget()
        self._transect_list.setColumnCount(len(PLAN_COLUMNS))
        self._transect_list.setHeaderLabels(list(PLAN_COLUMNS))
        self._transect_list.setRootIsDecorated(False)
        self._transect_list.setUniformRowHeights(True)
        self._transect_list.setAllColumnsShowFocus(True)
        # The numeric columns are the point of the table, so a long name gives way
        # to an ellipsis rather than pushing the counts off the right edge.
        self._transect_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        header_item = self._transect_list.headerItem()
        header_item.setToolTip(3, "Video segments assigned to this transect")
        header_item.setToolTip(4, "Reconstructions produced from them")
        for column in range(1, PLAN_SPACER_COLUMN):
            header_item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        enable_sorting(self._transect_list)
        install_column_sizer(self._transect_list, _PLAN_COLUMN_SPEC, settings_key="transects")
        self._transect_list.currentItemChanged.connect(lambda *_: self._on_transect_selected())
        # The empty state stands in for the list until there is something in it,
        # so a fresh install says how to get started instead of showing a void.
        self._transect_stack = QStackedWidget()
        self._transect_stack.addWidget(self._transect_list)
        self._transect_stack.addWidget(
            EmptyState(
                "No transects yet",
                "Add one with New, or Import… a CSV or GPX file.",
            )
        )
        group_layout.addWidget(self._transect_stack, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        new_btn = QPushButton("New")
        new_btn.setProperty("cta", "true")
        new_btn.clicked.connect(self._on_transect_new)
        # A saved transect's endpoints are fixed until this is pressed, so a
        # stray drag cannot move a survey position with nothing to undo it.
        self._transect_edit_btn = QPushButton("Edit")
        self._transect_edit_btn.setCheckable(True)
        self._transect_edit_btn.setToolTip(
            "Unlock this transect so its ends can be dragged on the map. "
            "Press again to save and lock it."
        )
        self._transect_edit_btn.toggled.connect(self._on_transect_edit_toggled)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_transect_delete)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._on_transects_import)
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self._on_transects_export)
        # Creating and deleting a transect is a different kind of act from
        # moving the whole set in and out of a file, so the two groups separate.
        buttons.addWidget(self._transect_edit_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(import_btn)
        buttons.addWidget(export_btn)
        group_layout.addLayout(buttons)

        details, details_layout = section_card("Details")
        # New sits at the top of the card it fills. At the far corner of the
        # table card below, the eye had to travel bottom-left to top-right to
        # follow one action to its effect.
        new_btn.setText("New transect")
        new_row = QHBoxLayout()
        new_row.addStretch(1)
        new_row.addWidget(new_btn)
        details_layout.addLayout(new_row)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        details_layout.addLayout(grid)
        details_layout.addStretch(1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.addWidget(_field_label("Name"), 0, 0)
        self._tr_name_input = QLineEdit()
        grid.addWidget(self._tr_name_input, 0, 1)
        # Which reef the line is on. Sites come down from the registry, so with
        # none pulled the combo holds only "No site" and changes nothing; names
        # are unique per site, so two reefs can each have a T1.
        grid.addWidget(_field_label("Site"), 0, 2)
        self._tr_site_combo = QComboBox()
        self._tr_site_combo.setToolTip(
            "The site this transect belongs to, from the registry's site list."
        )
        grid.addWidget(self._tr_site_combo, 0, 3)
        self._refresh_site_choices()

        # One box per end takes a coordinate straight off a GPS, pasted or
        # typed, in either "lat lon" or "lat, lon" form. Copying one back out is
        # an action inside the field, so the row stays full width and the button
        # only appears once there is something to copy.
        self._tr_start_coord = _coord_edit()
        self._tr_end_coord = _coord_edit()
        grid.addWidget(_field_label("Start"), 1, 0)
        grid.addWidget(self._tr_start_coord, 1, 1, 1, 2)
        grid.addWidget(_field_label("End"), 2, 0)
        grid.addWidget(self._tr_end_coord, 2, 1, 1, 2)
        self._coord_copy_actions = {
            which: self._add_copy_action(which) for which in ("start", "end")
        }
        for edit in (self._tr_start_coord, self._tr_end_coord):
            edit.editingFinished.connect(self._on_coords_edited)

        # Drawing is the primary way to place a transect and typing the fallback,
        # so the tool stands beside the pair of fields it fills, spanning both.
        self._pick_both_btn = QToolButton()
        self._pick_both_btn.setIcon(crosshair_icon(ICON_MD))
        self._pick_both_btn.setAccessibleName("Draw the transect on the map")
        self._pick_both_btn.setCheckable(True)
        self._pick_both_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._pick_both_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._pick_both_btn.setToolTip(
            "Draw the transect on the map: click its start, then its end. "
            "Drag either end afterwards to adjust it."
        )
        self._pick_both_btn.toggled.connect(self._on_pick_both_toggled)
        self._sync_map_pick_mode()
        grid.addWidget(self._pick_both_btn, 1, 3, 2, 1)

        # Length and heading are read off the two endpoints, so they are stated
        # rather than entered: a transect drawn the wrong way round shows it here.
        self._tr_geometry = secondary_label("")
        self._tr_geometry.setWordWrap(True)
        self._tr_geometry.setToolTip(
            "Straight-line distance and compass heading from the start point to the end point."
        )
        grid.addWidget(self._tr_geometry, 3, 1, 1, 3)
        self._refresh_geometry_readout()

        self._tr_length = OptionalMetresSpinBox(500.0)
        self._tr_length.setToolTip(
            "Tape length measured underwater. When set it is what the run is "
            "scaled to, in place of the distance between the GPS endpoints."
        )
        self._tr_depth = OptionalMetresSpinBox(100.0)
        self._tr_depth.setToolTip("Depth of the transect. Leave unset if not recorded.")
        grid.addWidget(_field_label("Length"), 4, 0)
        grid.addWidget(self._tr_length, 4, 1)
        grid.addWidget(_field_label("Depth"), 4, 2)
        grid.addWidget(self._tr_depth, 4, 3)

        grid.addWidget(_field_label("Notes", top=True), 5, 0, Qt.AlignmentFlag.AlignTop)
        self._tr_description = NotesEdit()
        grid.addWidget(self._tr_description, 5, 1, 1, 3)

        # No Save button: a new transect shows as a live draft row in the list
        # and commits itself the moment name and both endpoints are complete;
        # later edits commit on field exit.
        self._tr_name_input.textChanged.connect(self._on_draft_changed)
        self._tr_name_input.editingFinished.connect(self._maybe_autosave)
        # Typing a coordinate redraws the line as it is typed; committing it is
        # left to editingFinished, so a half-typed longitude never reaches the
        # store as a saved position.
        for edit in (self._tr_start_coord, self._tr_end_coord):
            edit.textChanged.connect(self._on_coords_typed)
        self._tr_length.editingFinished.connect(self._maybe_autosave)
        self._tr_depth.editingFinished.connect(self._maybe_autosave)
        self._tr_description.editing_finished.connect(self._maybe_autosave)
        # activated, not currentIndexChanged: only a person's pick commits, so
        # refilling the combo after a pull cannot save the form by side effect.
        self._tr_site_combo.activated.connect(lambda _: self._maybe_autosave())

        # Map and form on top, the list and what it found beneath: the form
        # belongs beside the map it draws on, and both lower panes are tables
        # that need width.
        top.addWidget(map_pane)
        top.addWidget(details)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 0)
        details.setMinimumWidth(360)

        # Map and form over the list on the left, and what a transect found down
        # the full height of the right. The chart plus its six-column table need
        # roughly 420px of height, which a bottom-right quarter of the page does
        # not have, so they were below the fold until the window was resized.
        page.addWidget(work)
        analysis_scroll = QScrollArea()
        analysis_scroll.setWidgetResizable(True)
        analysis_scroll.setWidget(self._build_analysis_page())
        analysis_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        analysis_scroll.setMinimumWidth(PLAN_ANALYSIS_MIN_WIDTH)
        self._plan_analysis_scroll = analysis_scroll
        page.addWidget(analysis_scroll)

        work.addWidget(top)
        work.addWidget(transects_group)
        transects_group.setMinimumHeight(_PLAN_LIST_MIN_HEIGHT)
        self._plan_split = page
        self._plan_work_split = work
        # Divided from the live width on every resize, not once here: the page
        # lives in a QStackedWidget and is 0px wide until its section is shown.
        page.installEventFilter(self)
        self._apply_plan_split_sizes()
        page.splitterMoved.connect(self._on_plan_split_moved)
        work.splitterMoved.connect(self._on_plan_split_moved)
        # No list refresh here: refreshes happen when the interface is built,
        # so opening the store (which creates survey.db) waits until then.
        return page

    def _apply_plan_split_sizes(self) -> None:
        """Divide the page between the working column and the analysis column.

        Set outright rather than left to stretch factors: a splitter only shares
        out what is above each pane's minimum, and the analysis needs its whole
        height for the chart and the cover table to both be on screen.
        """
        if getattr(self, "_plan_split_user_sized", False):
            return
        self._plan_split_applying = True
        try:
            total = self._plan_split.width()
            if total < SPLIT_MIN_TOTAL:
                total = sum(self._plan_split.sizes()) or 1200
            analysis = max(PLAN_ANALYSIS_MIN_WIDTH, int(total * _PLAN_ANALYSIS_SHARE))
            self._plan_split.setSizes([max(1, total - analysis), analysis])

            height = self._plan_work_split.height()
            if height < SPLIT_MIN_TOTAL:
                height = sum(self._plan_work_split.sizes()) or 800
            listed = max(_PLAN_LIST_MIN_HEIGHT, int(height * _PLAN_LIST_SHARE))
            self._plan_work_split.setSizes([max(1, height - listed), listed])
        finally:
            self._plan_split_applying = False

    def _plan_split_event_filter(self, obj, event) -> None:
        """Re-divide the page when its splitter is resized.

        Guarded on the splitter existing: the filter is installed on the window,
        which receives events while the page is still being built.
        """
        if obj is getattr(self, "_plan_split", None) and event.type() == QEvent.Type.Resize:
            self._apply_plan_split_sizes()

    def _on_plan_split_moved(self, *_args) -> None:
        """A dragged handle is a decision; stop overriding it on every resize."""
        if not getattr(self, "_plan_split_applying", False):
            self._plan_split_user_sized = True

    # --- List handling ---

    def _refresh_site_choices(self) -> None:
        """Refill the site combo, keeping whatever is picked. Sites arrive by
        pull, so the choices can grow between visits to this page."""
        combo = self._tr_site_combo
        kept = combo.currentData()
        store = self._try_survey_store()
        sites = store.list_sites() if store is not None else []
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("No site", None)
            for site in sites:
                combo.addItem(site.name, str(site.id))
            if kept:
                combo.setCurrentIndex(max(0, combo.findData(kept)))
        finally:
            combo.blockSignals(False)

    def _form_site_id(self) -> uuid.UUID | None:
        data = self._tr_site_combo.currentData()
        return uuid.UUID(str(data)) if data else None

    def _set_form_site(self, site_id: uuid.UUID | None) -> None:
        index = self._tr_site_combo.findData(str(site_id)) if site_id else 0
        self._tr_site_combo.setCurrentIndex(max(0, index))

    def _refresh_transect_list(self, select_id: uuid.UUID | None = None) -> None:
        store = self._try_survey_store()
        if store is None:
            self._transect_list.clear()
            self._refresh_plan_map()
            return
        self._refresh_site_choices()
        saved = store.list_transects()
        counts = store.transect_usage_counts()
        self._plan_list_rebuilding = True
        # Signals blocked for the whole rebuild: with the rows flat, the first
        # one inserted after a clear becomes current on its own, and letting
        # that reach the form would load a transect nobody picked over the one
        # being typed.
        self._transect_list.blockSignals(True)
        # Sorting suspended for the rebuild: live, every inserted row is sorted
        # into place before its data roles and fonts are set.
        self._transect_list.setSortingEnabled(False)
        try:
            # Overlays first: which rows survive the In view scope is read back
            # off the map, so it has to be holding the current set already.
            self._refresh_plan_map()
            visible = self._plan_map.visible_ids()
            self._plan_visible_ids = () if visible is None else tuple(sorted(visible))
            self._transect_list.clear()
            # Chips before rows: dropping below two transects releases the
            # scope, and the rows have to be built under the scope that leaves.
            self._refresh_plan_scope_chips(saved, visible)
            self._add_transect_rows(self._scoped_transects(saved, visible, select_id), counts)
            draft = self._draft_columns()
            if draft is not None:
                self._add_draft_row(draft)
            self._transect_stack.setCurrentIndex(0 if saved or draft else 1)
        finally:
            self._transect_list.setSortingEnabled(True)
            self._transect_list.blockSignals(False)
            self._plan_list_rebuilding = False
        self._select_transect_row(str(select_id) if select_id is not None else DRAFT_ID)
        # Cached for the Plan badge, which must not query the store itself: this
        # runs on every keystroke while a transect is being typed.
        self._plan_state = transects_state(len(saved), draft is not None)
        self._refresh_section_state()

    def _scoped_transects(
        self,
        saved: list[Transect],
        visible: frozenset[str] | None,
        select_id: uuid.UUID | None,
    ) -> list[Transect]:
        """The transects the active scope leaves.

        ``visible`` is None when the map has no answer yet, and standing aside
        is the only safe reading of that: filtering on it would empty the list
        on a page that has not been laid out. The transect about to be selected
        survives the scope whatever the map says, so picking one and panning off
        it does not delete the row the form is editing.
        """
        if self._plan_scope_filter != "in_view" or visible is None:
            return saved
        kept = str(select_id) if select_id is not None else None
        return [t for t in saved if str(t.id) in visible or str(t.id) == kept]

    def _refresh_plan_scope_chips(
        self, saved: list[Transect], visible: frozenset[str] | None
    ) -> None:
        """Offer the scope only where it filters something.

        Below two transects there is nothing to narrow, and a chip pair over a
        one-row list is a control that cannot change what is on screen. Going
        that way releases the scope with it: a filter still applying from a
        control nobody can see is a row missing for no stated reason.
        """
        chips = getattr(self, "_plan_scope_chips", None)
        if chips is None:
            return
        offered = len(saved) > 1
        chips.setVisible(offered)
        if not offered and self._plan_scope_filter != "all":
            self._plan_scope_filter = "all"
            # Signals blocked: the handler rebuilds the list this is being
            # called from the middle of.
            chips.blockSignals(True)
            try:
                chips.set_current("all")
            finally:
                chips.blockSignals(False)
        in_view = (
            len(saved) if visible is None else len([t for t in saved if str(t.id) in visible])
        )
        chips.set_counts({"in_view": in_view, "all": len(saved)})

    def _on_plan_scope_changed(self, key: str) -> None:
        self._plan_scope_filter = key
        self._refresh_transect_list(select_id=self._transect_form_id)

    def _add_transect_rows(
        self,
        transects: list[Transect],
        counts: dict[uuid.UUID, tuple[int, int]],
    ) -> None:
        """One row per transect, flat.

        No section heading over them: the scope chips above the list already
        name what it is showing and carry the count, and a heading repeating
        both cost a row on a card that is short of them.
        """
        for transect in transects:
            passes, runs = counts.get(transect.id, (0, 0))
            item = SortableTreeItem(
                self._transect_list,
                transect_row_columns(transect, passes, runs),
                transect_row_values(transect, passes, runs),
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(transect.id))
            tooltip = transect_tooltip(transect, passes, runs)
            for column in range(len(PLAN_COLUMNS)):
                item.setToolTip(column, tooltip)
                if 0 < column < PLAN_SPACER_COLUMN:
                    item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)

    def _add_draft_row(self, columns: list[str]) -> QTreeWidgetItem:
        """The transect being composed, italic, at the foot of the list.

        No sort values: a valueless SortableTreeItem sinks below every saved
        row in either direction, which is what keeps it at the foot.
        """
        item = SortableTreeItem(self._transect_list, columns)
        item.setData(0, Qt.ItemDataRole.UserRole, DRAFT_ID)
        font = item.font(0)
        font.setItalic(True)
        for column in range(len(PLAN_COLUMNS)):
            item.setFont(column, font)
            if 0 < column < PLAN_SPACER_COLUMN:
                item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        return item

    def _draft_columns(self) -> list[str] | None:
        """Row for the transect being composed, before it exists in the store;
        None once saved or while the form is empty."""
        if self._transect_form_id is not None:
            return None
        name = self._tr_name_input.text().strip()
        if not (name or self._tr_start_coord.text().strip()):
            return None
        label = name or "New transect"
        # Once both ends parse the draft can say the same things a saved row
        # does, so the length appears while it is still being entered.
        try:
            lat1, lon1, lat2, lon2 = self._form_coordinates()
        except ValueError:
            return [label, "incomplete", "", "", "", ""]
        length = transect_length_text(
            self._tr_length.value() or None, haversine_m(lat1, lon1, lat2, lon2)
        )
        depth = f"{self._tr_depth.value():g} m" if self._tr_depth.value() else "—"
        return [label, length, depth, "—", "—", ""]

    def _select_transect_row(self, id_str: str) -> None:
        """Select the row for ``id_str``, if the active scope is showing it."""
        for item in self._transect_rows():
            if str(item.data(0, Qt.ItemDataRole.UserRole)) == id_str:
                self._transect_list.blockSignals(True)
                try:
                    self._transect_list.setCurrentItem(item)
                finally:
                    self._transect_list.blockSignals(False)
                return

    def _open_transect_page(self, transect_id: object = None) -> None:
        """Go to a transect on the Transects page, from wherever it was named.

        The list is refreshed asking for that row rather than merely selected:
        the scope chips there may be filtering it out, and ``_scoped_transects``
        keeps a transect that was asked for by id.
        """
        self._go_to_section("transects")
        try:
            wanted = uuid.UUID(str(transect_id))
        except (ValueError, TypeError, AttributeError):
            return
        self._refresh_transect_list(select_id=wanted)
        self._on_transect_selected()

    def _transect_rows(self) -> list[QTreeWidgetItem]:
        """Every transect row, in display order."""
        return [
            item
            for index in range(self._transect_list.topLevelItemCount())
            if (item := self._transect_list.topLevelItem(index)) is not None
        ]

    def _on_plan_view_changed(self) -> None:
        """Coalesce the stream of view changes a drag produces.

        Deferred rather than immediate for a second reason: a view change can
        originate in a list selection, and rebuilding the list from inside its
        own selection signal would delete the item that is still emitting.
        """
        if self._plan_list_rebuilding:
            return
        self._plan_view_timer.start()

    def _apply_plan_view_change(self) -> None:
        """Pan and zoom re-decide what the In view scope leaves.

        Under All transects nothing listed changes, so the list is left alone
        and only the counts catch up: rebuilding it on every pan would drop the
        selection for no visible gain.
        """
        seen = self._plan_map.visible_ids()
        visible = () if seen is None else tuple(sorted(seen))
        if visible == self._plan_visible_ids:
            return
        self._plan_visible_ids = visible
        if self._plan_scope_filter == "in_view":
            self._refresh_transect_list(select_id=self._transect_form_id)
        else:
            self._refresh_plan_scope_chips(self._survey_store().list_transects(), seen)

    def _on_draft_changed(self) -> None:
        if self._transect_form_id is None:
            self._refresh_transect_list()

    def _maybe_autosave(self) -> None:
        """Commit silently once the form is complete; incomplete forms stay a
        draft without nagging."""
        if not self._tr_name_input.text().strip():
            return
        try:
            self._form_coordinates()
        except ValueError:
            return
        self._on_transect_save()

    def _selected_transect_id(self) -> uuid.UUID | None:
        item = self._transect_list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None or str(data) == DRAFT_ID:
            return None
        return uuid.UUID(str(data))

    def _on_transect_selected(self, focus_map: bool = True) -> None:
        transect_id = self._selected_transect_id()
        if transect_id is None:
            return
        transect = self._survey_store().get_transect(transect_id)
        if transect is None:
            return
        self._transect_form_id = transect.id
        self._set_pick_armed(False)
        # A transect opened from the list is being looked at, not moved.
        self._set_transect_editing(False)
        self._tr_name_input.setText(transect.name)
        self._tr_start_coord.setText(f"{transect.start_lat:.6f}, {transect.start_lon:.6f}")
        self._tr_end_coord.setText(f"{transect.end_lat:.6f}, {transect.end_lon:.6f}")
        self._tr_length.setValue(transect.length_m or 0.0)
        self._tr_depth.setValue(transect.depth_m or 0.0)
        self._set_form_site(transect.site_id)
        self._tr_description.setPlainText(transect.description)
        self._refresh_plan_map()
        # Picking a transect by name is a request to look at it, so the map goes
        # there rather than leaving the selected line off screen. A transect
        # picked by clicking its line is already on screen and the map holds
        # still, or the click would yank the view out from under the pointer.
        if focus_map:
            self._plan_map.focus_on(
                [(transect.start_lat, transect.start_lon), (transect.end_lat, transect.end_lon)],
                fill=FOCUS_FILL,
            )
        self._set_scope_transect(transect.id)

    # --- Form handling ---

    def _on_transect_new(self) -> None:
        """Start a transect that is already named and already being drawn.

        Naming is the step with nothing to decide (the store only needs the
        name unique), so it is filled in and left selected for anyone who has a
        better one, and the map tool arms itself for the two clicks that matter.
        """
        self._transect_form_id = None
        self._transect_list.setCurrentIndex(QModelIndex())
        for edit in (
            self._tr_start_coord,
            self._tr_end_coord,
            self._tr_description,
        ):
            edit.clear()
        self._tr_length.setValue(0.0)
        self._tr_depth.setValue(0.0)
        existing = [t.name for t in self._survey_store().list_transects()]
        self._tr_name_input.setText(next_transect_name(existing))
        self._tr_name_input.setFocus()
        self._tr_name_input.selectAll()
        self._set_transect_editing(True)
        self._set_pick_armed(True)

    def _coord_edit(self, which: str) -> QLineEdit:
        return self._tr_start_coord if which == "start" else self._tr_end_coord

    def _set_endpoint(self, which: str, lat: float, lon: float) -> None:
        self._coord_edit(which).setText(f"{lat:.6f}, {lon:.6f}")
        self._status_label.setText(f"{which.capitalize()} point set.")
        self._refresh_plan_map()
        self._maybe_autosave()

    def _form_coordinates(self) -> tuple[float, float, float, float]:
        """Both endpoints in decimal degrees, raising if either is blank or unparseable."""
        values: list[float] = []
        for which in ("start", "end"):
            text = self._coord_edit(which).text().strip()
            if not text:
                raise ValueError(f"Missing {which} point")
            try:
                values.extend(parse_latlon(text))
            except ValueError as exc:
                raise ValueError(f"{which.capitalize()} point: {exc}") from None
        return values[0], values[1], values[2], values[3]

    def _on_coords_typed(self) -> None:
        """Redraw from what is in the fields right now, without committing it."""
        self._refresh_plan_map()
        self._refresh_geometry_readout()
        if self._transect_form_id is None:
            self._on_draft_changed()

    def _on_coords_edited(self) -> None:
        self._on_coords_typed()
        self._maybe_autosave()

    def _refresh_geometry_readout(self) -> None:
        """State the length and heading the two fields currently describe."""
        try:
            lat1, lon1, lat2, lon2 = self._form_coordinates()
        except ValueError:
            self._tr_geometry.setText("Length and heading appear once both ends are set")
            return
        self._tr_geometry.setText(
            f"{haversine_m(lat1, lon1, lat2, lon2):.0f} m between the GPS ends  ·  "
            f"heading {bearing_text(lat1, lon1, lat2, lon2)}"
        )

    def _on_transect_save(self) -> None:
        store = self._survey_store()
        try:
            transect = build_transect(
                self._tr_name_input.text(),
                self._tr_start_coord.text(),
                self._tr_end_coord.text(),
                length_m=self._tr_length.value(),
                depth_m=self._tr_depth.value(),
                description=self._tr_description.toPlainText().strip(),
                site_id=self._form_site_id(),
            )
        except ValueError as exc:
            self._status_label.setText(str(exc))
            return
        try:
            if self._transect_form_id is None:
                store.add_transect(transect)
            else:
                transect.id = self._transect_form_id
                store.update_transect(transect)
        except sqlite3.IntegrityError:
            self._status_label.setText(
                f"A transect named {transect.name!r} already exists on that site."
            )
            return
        self._transect_form_id = transect.id
        self._status_label.setText(f"Saved transect {transect.name}.")
        self._refresh_transect_list(select_id=transect.id)
        self._survey_data_changed()

    def _on_transect_delete(self) -> None:
        transect_id = self._selected_transect_id()
        if transect_id is None:
            return
        try:
            self._survey_store().delete_transect(transect_id)
        except ValueError as exc:
            self._status_label.setText(str(exc))
            return
        self._on_transect_new()
        self._refresh_transect_list()
        self._survey_data_changed()

    # --- Import / export ---

    def _on_transects_import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import transects",
            self._out_root_input.text(),
            "Transect files (*.csv *.gpx);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".gpx":
                transects = import_transects_gpx(path)
            else:
                transects = import_transects_csv(path)
        except ValueError as exc:
            self._status_label.setText(f"Import failed: {exc}")
            return
        store = self._survey_store()
        added, skipped = 0, 0
        for transect in transects:
            try:
                store.add_transect(transect)
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        message = f"Imported {added} transect(s)."
        if skipped:
            message += f" Skipped {skipped} already present."
        self._status_label.setText(message)
        self._refresh_transect_list()
        self._survey_data_changed()

    def _on_transects_export(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export transects",
            str(Path(self._out_root_input.text()) / "transects.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        transects = self._survey_store().list_transects()
        save_transects_csv(Path(path_str), transects)
        self._status_label.setText(f"Exported {len(transects)} transect(s).")

    # --- Map ---

    def _refresh_plan_map(self, fit: bool = False) -> None:
        selected = self._transect_form_id
        store = self._try_survey_store()
        counts = store.transect_usage_counts() if store is not None else {}
        try:
            typed = self._form_coordinates()
        except ValueError:
            typed = None
        overlays = []
        for transect in (store.list_transects() if store is not None else []):
            passes, runs = counts.get(transect.id, (0, 0))
            # The selected transect follows the fields as they are typed, so a
            # pasted coordinate lands on the map before it is committed.
            start = (transect.start_lat, transect.start_lon)
            end = (transect.end_lat, transect.end_lon)
            if transect.id == selected and typed is not None:
                start, end = (typed[0], typed[1]), (typed[2], typed[3])
            overlays.append(OverlayTransect(
                id=str(transect.id),
                start=start,
                end=end,
                color=QColor(PRIMARY),
                selected=transect.id == selected,
                label=transect.name,
                tooltip=transect_tooltip(transect, passes, runs),
            ))
        # An unsaved transect previews as soon as both endpoints are filled.
        if selected is None and typed is not None:
            overlays.append(OverlayTransect(
                id=DRAFT_ID,
                start=(typed[0], typed[1]),
                end=(typed[2], typed[3]),
                color=QColor(PRIMARY),
                selected=True,
                label=self._tr_name_input.text().strip(),
            ))
        self._plan_map.set_transects(overlays)
        # Only the transect being edited offers its endpoints to the pointer; the
        # rest are there to be read, hovered and clicked without moving.
        if not self._transect_editing:
            editable = None
        elif selected is not None:
            editable = str(selected)
        else:
            editable = DRAFT_ID
        self._plan_map.set_editable(editable)
        self._sync_transect_edit_enabled()
        if fit or not self._plan_map_fitted:
            self._plan_map.fit_transects()
            self._plan_map_fitted = bool(overlays)

    def _add_copy_action(self, which: str):
        """Copy button living inside the coordinate field, shown once it holds
        something worth copying."""
        edit = self._coord_edit(which)
        action = edit.addAction(copy_icon(ICON_SM), QLineEdit.ActionPosition.TrailingPosition)
        action.setToolTip(f"Copy the {which} coordinates")
        # QLineEdit renders the action as a QToolButton with no text of its own.
        action.setText(f"Copy the {which} coordinates")
        action.triggered.connect(lambda _=False, w=which: self._copy_endpoint(w))
        action.setVisible(False)
        edit.textChanged.connect(lambda text, a=action: a.setVisible(bool(text.strip())))
        return action

    def _copy_endpoint(self, which: str) -> None:
        edit = self._coord_edit(which)
        text = edit.text().strip()
        if not text:
            self._status_label.setText(f"No {which} point to copy.")
            return
        QGuiApplication.clipboard().setText(text)
        self._status_label.setText(f"Copied {which} point {text} to the clipboard.")
        # The status bar is at the far corner of the window from the field that
        # was clicked, so the confirmation is also shown at the field itself and
        # the button briefly becomes a tick.
        QToolTip.showText(
            edit.mapToGlobal(edit.rect().topRight()), "Copied to clipboard", edit
        )
        action = self._coord_copy_actions[which]
        action.setIcon(check_icon(ICON_SM))
        QTimer.singleShot(1200, lambda a=action: a.setIcon(copy_icon(16)))

    def _set_transect_editing(self, on: bool) -> None:
        """Unlock or lock the endpoints without going round the toggled signal."""
        if self._transect_edit_btn.isChecked() == on:
            self._apply_transect_editing(on)
            return
        self._transect_edit_btn.setChecked(on)

    def _on_transect_edit_toggled(self, on: bool) -> None:
        self._apply_transect_editing(on)

    def _apply_transect_editing(self, on: bool) -> None:
        self._transect_editing = on
        self._transect_edit_btn.setText("Save" if on else "Edit")
        if on:
            self._status_label.setText("Drag either end of the transect to move it.")
        else:
            # Leaving edit mode is the save: the draw tool has nothing left to
            # place, and a complete form commits itself.
            self._set_pick_armed(False)
            self._maybe_autosave()
        self._refresh_plan_map()

    def _sync_transect_edit_enabled(self) -> None:
        """The button has nothing to unlock until there is a transect in the form."""
        has_form = self._transect_form_id is not None or bool(
            self._tr_name_input.text().strip()
        )
        self._transect_edit_btn.setEnabled(has_form)

    def _set_pick_armed(self, on: bool) -> None:
        """Arm or disarm the draw tool without going round the toggled signal."""
        if self._pick_both_btn.isChecked() == on:
            self._on_pick_both_toggled(on)
            return
        self._pick_both_btn.setChecked(on)

    def _on_pick_both_toggled(self, on: bool) -> None:
        self._pick_stage = "start" if on else None
        if on:
            # Placing a line is editing it, so the two arm together.
            self._set_transect_editing(True)
            self._status_label.setText("Click the start of the transect.")
        self._plan_map.set_pending_start(None)
        self._sync_map_pick_mode()

    def _sync_map_pick_mode(self) -> None:
        """Crosshair cursor and a narrating tool label whenever a click would
        land somewhere."""
        armed = self._pick_stage is not None
        self._plan_map.set_pick_mode(armed)
        self._pick_both_btn.setText(
            {"start": "Click start", "end": "Click end", None: "Draw"}[self._pick_stage]
        )

    def _on_plan_map_clicked(self, lat: float, lon: float) -> None:
        if self._pick_stage == "start":
            self._set_endpoint("start", lat, lon)
            self._pick_stage = "end"
            self._plan_map.set_pending_start((lat, lon))
            self._status_label.setText("Now click the end of the transect.")
            self._sync_map_pick_mode()
            return
        if self._pick_stage == "end":
            self._set_pick_armed(False)
            self._set_endpoint("end", lat, lon)

    def _on_plan_map_transect_clicked(self, transect_id: str) -> None:
        self._select_transect_row(transect_id)
        self._on_transect_selected(focus_map=False)

    def _on_plan_endpoint_moved(self, transect_id: str, which: str, lat: float, lon: float) -> None:
        if self._transect_form_id is None:
            # A line still being drawn has no id yet; its overlay is the draft.
            if transect_id != DRAFT_ID:
                return
            self._coord_edit(which).setText(f"{lat:.6f}, {lon:.6f}")
            self._on_coords_typed()
            self._maybe_autosave()
            return
        if str(self._transect_form_id) != transect_id:
            return
        self._coord_edit(which).setText(f"{lat:.6f}, {lon:.6f}")
        self._on_transect_save()

    def _survey_data_changed(self) -> None:
        """Refresh survey views that mirror the store."""
        self._refresh_survey_transect_names()
        self._refresh_survey_analysis()
        # Creating the transect an unassigned pass was waiting for has to
        # re-evaluate the Run gate, or the batch stays blocked with nothing left
        # to fix.
        self._recompute_survey_start()
