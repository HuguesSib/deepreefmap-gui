"""The run archive as a sortable table, one row per run.

Runs are compared, not read: which pass produced the most points, which one ate
the disk, which transect has three attempts and which has none. A column answers
that at a glance and a click sorts by it, where the prose card it replaces
buried every fact in one wrapped sentence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QTableWidget,
)

from deepreefmap_gui.core.widgets import (
    ColumnSpec,
    SortableItem,
    StatusPillDelegate,
    configure_table,
    enable_sorting,
    fitted_column_widths,
    install_column_sizer,
)
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_cards import (
    emphasise_line,
    format_run_metadata,
    points_label,
    recorded_text,
)
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import RunEntry
from deepreefmap_gui.survey.models import direction_text

COL_NAME, COL_STATUS, COL_CREATED, COL_FRAMES = 0, 1, 2, 3
COL_POINTS, COL_RUNTIME, COL_SIZE = 4, 5, 6
COL_TRANSECT, COL_DIRECTION, COL_RECORDED, COL_VIDEO = 7, 8, 9, 10

_HEADERS = (
    "Name",
    "Status",
    "Created",
    "Frames",
    "Points",
    "Runtime",
    "Size",
    "Transect",
    "Direction",
    "Recorded",
    "Video",
)

# Columns whose width is a property of what they hold rather than of the window:
# a status pill, a fixed-width timestamp, a formatted number. Sizing these to
# content instead let them take the viewport and squeeze Name to an ellipsis,
# and sizing them to the window would only pad digits with air.
_FIXED_WIDTHS = {
    COL_STATUS: 88,
    COL_CREATED: 112,
    COL_FRAMES: 64,
    COL_POINTS: 64,
    COL_RUNTIME: 72,
    COL_SIZE: 72,
}

# Secondary identifiers, shown in the order they earn their width and hidden
# when it is not there. Browse leaves the table around 830px with the rail open,
# which is what the columns above plus the flexing ones already spend; adding
# these unconditionally would put a scrollbar under the page's one table. The
# row's tooltip carries both whether or not a column does.
_OPTIONAL_WIDTHS = ((COL_DIRECTION, 76), (COL_RECORDED, 112))

# What is left over, shared out by weight. Qt's Stretch mode splits slack
# equally, which would hand Transect as much room as Name; the name is what
# identifies a row, so it takes the larger share and the two weaker identifiers
# follow it.
_FLEX_WEIGHTS = {COL_NAME: 3, COL_VIDEO: 2, COL_TRANSECT: 1}

# Below these a column has stopped saying which run, which clip or which line,
# so it holds its floor and the table scrolls instead. That only happens on a
# window too narrow to hold the columns by any arrangement.
_FLEX_MINIMUMS = {COL_NAME: 140, COL_VIDEO: 100, COL_TRANSECT: 80}

# Numbers read right-aligned, which also lines up their digits down the column.
# Their headers follow them, so label and value share an edge.
_NUMERIC_COLUMNS = (COL_FRAMES, COL_POINTS, COL_RUNTIME, COL_SIZE)

_MISSING = "—"

# The tooltip line each column is about, so a tooltip opened over a column points
# at the fact that column shows. The tooltip labels its column block with these
# same headings, so this is very nearly an identity map, and every column but
# Name has an entry: Name is the tooltip's own heading and is already bold.
_TOOLTIP_LABELS = {
    COL_STATUS: "Status",
    COL_CREATED: "Created",
    COL_FRAMES: "Frames",
    COL_POINTS: "Points",
    COL_RUNTIME: "Runtime",
    COL_SIZE: "Size",
    COL_TRANSECT: "Transect",
    COL_DIRECTION: "Direction",
    COL_RECORDED: "Recorded",
    COL_VIDEO: "Video",
}


_COLUMN_SPEC = ColumnSpec(
    fixed=_FIXED_WIDTHS,
    weights=_FLEX_WEIGHTS,
    minimums=_FLEX_MINIMUMS,
    optional=_OPTIONAL_WIDTHS,
)


def column_widths(available: int) -> dict[int, int]:
    """How a viewport of `available` px divides between the columns it can hold."""
    return fitted_column_widths(available, _COLUMN_SPEC)


def _sort_text(value: str | None) -> str | None:
    """Lowercased for a case-blind sort; None when absent, so it sinks."""
    return value.lower() if value else None


def _created_text(entry: RunEntry) -> str:
    try:
        stamp = datetime.fromtimestamp(entry.sort_key, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _recorded_sort_key(entry: RunEntry) -> float | None:
    stamp = entry.recorded_at
    return stamp.timestamp() if stamp is not None else None


def _frame_count(entry: RunEntry) -> int | None:
    frames = entry.manifest.get("frames_processed")
    return int(frames) if isinstance(frames, (int, float)) else None


def _row_tooltip(entry: RunEntry, related: int) -> str:
    """Everything the row knows: a line per column, then the manifest's extras.

    A run that never wrote a manifest gets the column block too, and says why the
    rest is missing. It used to get two lines, so hovering the Size column of a
    crashed run -- the run whose size you are most likely to be checking before
    deleting it -- answered with nothing at all.
    """
    tooltip = format_run_metadata(entry)
    if entry.incomplete:
        tooltip += "<br><br><i>No run manifest: this run did not finish.</i>"
    if entry.data_missing:
        tooltip += "<br><br><i>Output data removed: only the record remains.</i>"
    if entry.moved_from:
        tooltip += f"<br><i>Recorded at run time as: {entry.moved_from}</i>"
    if related:
        tooltip += f"<br><i>{related} related run{'s' if related != 1 else ''}</i>"
    return tooltip


class RunTable(QTableWidget):
    """Every run in the current grouping, one per row.

    Identity is the run directory, carried on the Name cell under ``UserRole``;
    the panel resolves selections back to ``RunEntry`` through it, so no widget
    holds onto an entry that a rescan has replaced.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_HEADERS), parent)
        configure_table(self, _HEADERS)
        # Delete and Assign act on a whole selection, so several runs can be
        # picked at once.
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setWordWrap(False)
        self.setItemDelegateForColumn(COL_STATUS, StatusPillDelegate(self))

        # A long value gives way to an ellipsis rather than to a scrollbar, and
        # elides from the middle: a run name and a GoPro file name are both told
        # apart by their two ends, so dropping the tail of GX_VIDEO_1_OF_2.MP4
        # loses exactly the character that identifies it.
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)

        for column in _NUMERIC_COLUMNS:
            item = self.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
        enable_sorting(self, COL_CREATED, Qt.SortOrder.DescendingOrder)
        install_column_sizer(self, _COLUMN_SPEC, settings_key="runs")

    def current_run_dir(self) -> str | None:
        row = self.currentRow()
        if row < 0:
            return None
        item = self.item(row, COL_NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def selected_run_dirs(self) -> set[str]:
        items = (self.item(i.row(), COL_NAME) for i in self.selectionModel().selectedRows())
        return {
            item.data(Qt.ItemDataRole.UserRole) for item in items if item is not None
        }

    def select_run_dir(self, run_dir: str) -> bool:
        for row in range(self.rowCount()):
            item = self.item(row, COL_NAME)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == run_dir:
                self.setCurrentCell(row, COL_NAME)
                return True
        return False

    def set_entries(self, entries: list[RunEntry], related: dict) -> None:
        """Repaint the whole table, preserving the sort and the selected run."""
        keep = self.current_run_dir()
        # Sorting is suspended while rows are filled: with it live, each new row
        # is re-sorted into place as it lands and the cells of a half-built row
        # scatter across the table.
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._fill_row(row, entry, related.get(entry.run_dir, 0))
        self.setSortingEnabled(True)
        if keep is not None:
            self.select_run_dir(keep)

    def _fill_row(self, row: int, entry: RunEntry, related: int) -> None:
        status = catalogue.entry_status(entry)
        frames = _frame_count(entry)
        cells = (
            (COL_NAME, entry.display_name, entry.display_name.lower()),
            (COL_STATUS, status.capitalize(), status),
            (COL_CREATED, _created_text(entry), entry.sort_key),
            (COL_FRAMES, f"{frames:,}" if frames else "", frames),
            (
                COL_POINTS,
                points_label(entry.points) if entry.points else "",
                entry.points,
            ),
            (
                COL_RUNTIME,
                format_duration(entry.duration_s) if entry.duration_s else "",
                entry.duration_s,
            ),
            (
                COL_SIZE,
                "removed"
                if entry.data_missing
                else format_bytes(entry.size_bytes)
                if entry.size_bytes is not None
                else "",
                0 if entry.data_missing else entry.size_bytes,
            ),
            (COL_TRANSECT, entry.transect_name or "", _sort_text(entry.transect_name)),
            (COL_DIRECTION, direction_text(entry.direction), _sort_text(entry.direction)),
            (COL_RECORDED, recorded_text(entry), _recorded_sort_key(entry)),
            (COL_VIDEO, entry.video_name or "", _sort_text(entry.video_name)),
        )
        tooltip = _row_tooltip(entry, related)
        for column, text, value in cells:
            # An em dash rather than an empty cell: a blank reads as a column
            # that failed to load, where a dash says the run has no such fact.
            item = SortableItem(text or (_MISSING if column != COL_NAME else ""), value)
            # Per cell rather than per row: Qt shows the tooltip belonging to the
            # cell under the pointer, so the column being hovered is already
            # known and needs no mouse tracking of our own.
            item.setToolTip(emphasise_line(tooltip, _TOOLTIP_LABELS.get(column)))
            if column in _NUMERIC_COLUMNS:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            # Identity rides on the Name cell, which every lookup goes through.
            if column == COL_NAME:
                item.setData(Qt.ItemDataRole.UserRole, str(entry.run_dir))
            self.setItem(row, column, item)
