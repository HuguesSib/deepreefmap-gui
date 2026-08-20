"""The two lists on the storage page, and the words each row is offered in.

Runs are a tree because a run is a choice about a run, not a single act: the
top-level row is the folder and its children are the tiers, each with its own
tick and its own size. The parent carries a tick of its own so the whole folder
can go without anybody expanding it, and reads part-ticked when only some of it
is selected. Videos are a flat list because the only choice there is whether the
original recording stays.

Every run row also carries a make-up bar: one segment per tier, on the run's own
scale. Unexpanded it says at a glance where a run's gigabytes actually are, and
expanded each tier's bar is that same segment on that same scale, so the
children visibly add up to the parent.

Rows are QTreeWidgetItems painted by one delegate rather than per-row widgets. A
field season is hundreds of runs, and a widget per row is hundreds of widgets to
rebuild every time a scan lands.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
)

from deepreefmap_gui.core.icons import folder_icon
from deepreefmap_gui.core.theme import (
    ERROR,
    GROOVE,
    IDLE,
    PRIMARY,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    TEXT_MUTED,
    UPDATE,
    WARNING,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import (
    ColumnSpec,
    SortableTreeItem,
    fitted_column_widths,
    install_column_sizer,
)
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.storage.inventory import MountClip, MountItem, MountRun
from deepreefmap_gui.storage.tiers import (
    ALL_TIERS,
    DELETABLE_TIERS,
    TIER_CACHE,
    TIER_KEEP,
    TIER_RESULTS,
    TIER_UNKNOWN,
    TIER_WORKING,
)

# What each tier is called, and what deleting it costs. Ordered cheapest loss
# first, which is also the order ticking one ticks the ones above it.
TIER_LABELS = {
    TIER_CACHE: "Cache",
    TIER_RESULTS: "Results",
    TIER_WORKING: "Working data",
    TIER_KEEP: "Kept",
    TIER_UNKNOWN: "Not ours",
}

TIER_DETAILS = {
    TIER_CACHE: "Rebuilt when the run is opened.",
    TIER_RESULTS: "The ortho, the cloud and the cover figures. Rebuilt only by re-running.",
    TIER_WORKING: (
        "Frames, labels, masks and the mapping arrays. The run cannot be opened "
        "or resumed again."
    ),
}

# The make-up bar's palette. Distinct from the drive bar's, which is about what
# put the bytes there; this is about what it would cost to take them away, so it
# runs cheap to dear: cache, then deliverables, then the run's working life.
TIER_COLOURS = {
    TIER_CACHE: UPDATE,
    TIER_RESULTS: SUCCESS,
    TIER_WORKING: PRIMARY,
    TIER_KEEP: IDLE,
    TIER_UNKNOWN: SURFACE_HI,
}

# Everything a run holds, in the order the bar paints it.
BAR_TIERS = (*DELETABLE_TIERS, TIER_KEEP, TIER_UNKNOWN)

ABORTED_DETAIL = "Stopped before it finished. Nothing to open, nothing to resume."
OPEN_IN_VIEWER_DETAIL = "Open in the viewer. Close it with the + button."
WHOLE_RUN_DETAIL = "The whole folder. The record stays, so the run still shows in Browse."
OTHERS_TITLE = "Other files in the output folder"
COUNTING = "counting"

CLIP_DELETABLE_DETAIL = (
    "Delete the original file. The clip keeps its sections and shows as missing footage."
)
# Footage nothing has come of yet is the only copy of a dive, so this page will
# not delete it. Saying where it is beats saying no: somebody who really means
# it can do it in the file manager, having had to go and find it first.
CLIP_NO_RUN_DETAIL = (
    "No finished run yet, so this footage is all there is, therefore it cannot be "
    "deleted here. Open the folder and delete it yourself if you are sure."
)
CLIP_MISSING_DETAIL = "The file is not where the survey last saw it, therefore it cannot be deleted."

REVEAL_TIP = "Show this in the file manager."

COL_NAME, COL_SIZE, COL_BAR, COL_DETAIL, COL_OPEN = range(5)
RUN_COLUMNS = ("Run", "On disk", "Make-up", "What it costs", "")
CLIP_COLUMNS = ("Clip", "Size", "", "What it costs", "")

# Roles the page reads back off a ticked row, so nothing has to be looked up by
# its label. A row carrying none of them is not something that can be deleted.
ROLE_RUN = Qt.ItemDataRole.UserRole
ROLE_TIER = Qt.ItemDataRole.UserRole + 1
ROLE_ITEM = Qt.ItemDataRole.UserRole + 2
ROLE_CLIP = Qt.ItemDataRole.UserRole + 3
ROLE_BYTES = Qt.ItemDataRole.UserRole + 4
# The make-up bar: a list of (colour, bytes) and the total they are drawn against.
ROLE_MAKEUP = Qt.ItemDataRole.UserRole + 5
ROLE_SCALE = Qt.ItemDataRole.UserRole + 6
# What the folder button on the row opens. Present on every row that names
# something on disk, including the ones nothing here will delete.
ROLE_REVEAL = Qt.ItemDataRole.UserRole + 7


class MakeUpDelegate(QStyledItemDelegate):
    """Paints a row's make-up bar, on the scale its run was drawn against.

    A tier's bar is its own segment at its own place on the parent's scale, so
    expanding a run does not rescale anything: the children line up under the
    parent's bar rather than each filling its own width.
    """

    def paint(self, painter: QPainter, option, index: QModelIndex | QPersistentModelIndex) -> None:
        segments = index.data(ROLE_MAKEUP)
        scale = index.data(ROLE_SCALE)
        if not segments or not scale:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(option.rect).adjusted(0, SPACE_XS, -SPACE_XS, -SPACE_XS)
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))
        painter.setClipPath(path)
        for colour, offset, size in segments:
            left = track.left() + track.width() * (offset / scale)
            width = track.width() * (size / scale)
            if width > 0:
                painter.fillRect(
                    QRectF(left, track.top(), max(width, 1.0), track.height()), QColor(colour)
                )
        painter.restore()


class StorageTree(QTreeWidget):
    """A list that spends the width it is given rather than compacting.

    Content-sized columns left the run name at the width of its longest visible
    string and the sentence beside it with a screen of slack, so the widths are
    driven the way the run table's are: fixed for the figures, weighted for the
    rest, recomputed whenever the viewport changes.
    """

    # The bar is a fixed reading width: proportional segments are only
    # comparable between rows when every row's track is the same length.
    _FIXED = {COL_SIZE: 92, COL_BAR: 180, COL_OPEN: 32}
    _WEIGHTS = {COL_NAME: 2, COL_DETAIL: 5}
    _MINIMUMS = {COL_NAME: 180, COL_DETAIL: 220}

    def __init__(self, columns: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(len(columns))
        self.setHeaderLabels(list(columns))
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setItemDelegateForColumn(COL_BAR, MakeUpDelegate(self))
        install_column_sizer(self, _COLUMN_SPEC, settings_key="storage")


_COLUMN_SPEC = ColumnSpec(
    fixed=StorageTree._FIXED,
    weights=StorageTree._WEIGHTS,
    minimums=StorageTree._MINIMUMS,
)


def column_widths(available: int) -> dict[int, int]:
    """How a viewport of `available` px divides between the five columns."""
    return fitted_column_widths(available, _COLUMN_SPEC)


def _reveal(item: QTreeWidgetItem, path) -> None:
    """Give a row the folder button that opens where it lives."""
    item.setData(0, ROLE_REVEAL, str(path))
    item.setIcon(COL_OPEN, folder_icon())
    item.setToolTip(COL_OPEN, REVEAL_TIP)


def reveal_target(item: QTreeWidgetItem) -> str | None:
    """What this row's folder button opens, or None where it has none."""
    return item.data(0, ROLE_REVEAL)


def _muted(item: QTreeWidgetItem, column: int) -> None:
    item.setForeground(column, QColor(TEXT_MUTED))


def _tickable(item: QTreeWidgetItem) -> None:
    item.setCheckState(0, Qt.CheckState.Unchecked)


def _make_up(run: MountRun) -> tuple[list[tuple[str, int, int]], int]:
    """The whole run as (colour, offset, size) segments, and the scale to draw them on."""
    segments = []
    offset = 0
    for tier in BAR_TIERS:
        size = run.breakdown.tier_bytes(tier) if run.breakdown is not None else 0
        if size:
            segments.append((TIER_COLOURS[tier], offset, size))
        offset += size
    return segments, max(run.total_bytes, 1)


def _tier_offset(run: MountRun, tier: str) -> int:
    """Where a tier starts along the run's bar, so a child lines up under it."""
    offset = 0
    for other in BAR_TIERS:
        if other == tier:
            break
        offset += run.breakdown.tier_bytes(other) if run.breakdown is not None else 0
    return offset


def fill_runs(
    tree: QTreeWidget,
    runs: tuple[MountRun, ...],
    others: tuple[MountItem, ...],
    *,
    open_run: str | None = None,
) -> None:
    """Rebuild the run list. ``open_run`` is the one the viewer is holding."""
    tree.clear()
    for run in runs:
        parent = SortableTreeItem(
            tree,
            (run.display_name, _size_text(run), "", "", ""),
            {COL_SIZE: run.total_bytes},
        )
        parent.setData(0, ROLE_RUN, run.dir_name)
        parent.setData(0, ROLE_BYTES, run.total_bytes)
        parent.setToolTip(COL_NAME, str(run.run_dir))
        _reveal(parent, run.run_dir)
        _muted(parent, COL_SIZE)
        segments, scale = _make_up(run)
        parent.setData(COL_BAR, ROLE_MAKEUP, segments)
        parent.setData(COL_BAR, ROLE_SCALE, scale)
        parent.setToolTip(COL_BAR, _make_up_tooltip(run))

        if run.dir_name == open_run:
            parent.setText(COL_DETAIL, OPEN_IN_VIEWER_DETAIL)
            _muted(parent, COL_DETAIL)
            continue
        if run.aborted:
            parent.setText(COL_DETAIL, ABORTED_DETAIL)
        else:
            parent.setText(COL_DETAIL, WHOLE_RUN_DETAIL)
        _muted(parent, COL_DETAIL)
        # The parent's own tick is the whole folder, so a run can go without
        # anybody expanding it. Auto-tristate makes it read part-ticked the
        # moment one tier below it is chosen.
        parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsAutoTristate)
        _tickable(parent)
        if run.breakdown is not None:
            _add_tiers(parent, run, scale)

    if others:
        group = QTreeWidgetItem(tree, (OTHERS_TITLE, "", "", "", ""))
        group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate)
        _tickable(group)
        _muted(group, COL_NAME)
        for other in others:
            row = QTreeWidgetItem(
                group, (other.label, format_bytes(other.size_bytes), "", other.detail, "")
            )
            row.setData(0, ROLE_ITEM, other.label)
            _reveal(row, other.path)
            row.setData(0, ROLE_BYTES, other.size_bytes)
            _muted(row, COL_SIZE)
            _muted(row, COL_DETAIL)
            # Never pre-ticked, whatever it looks like: this is the group that
            # holds anything nobody here recognises.
            _tickable(row)
        group.setExpanded(False)


def _add_tiers(parent: QTreeWidgetItem, run: MountRun, scale: int) -> None:
    """One row per tier that can go, each on the parent's own scale."""
    breakdown = run.breakdown
    if breakdown is None:
        return
    for tier in DELETABLE_TIERS:
        size = breakdown.tier_bytes(tier)
        row = QTreeWidgetItem(
            parent, (TIER_LABELS[tier], format_bytes(size), "", TIER_DETAILS[tier], "")
        )
        row.setData(0, ROLE_RUN, run.dir_name)
        row.setData(0, ROLE_TIER, tier)
        row.setData(0, ROLE_BYTES, size)
        row.setData(COL_BAR, ROLE_MAKEUP, [(TIER_COLOURS[tier], _tier_offset(run, tier), size)])
        row.setData(COL_BAR, ROLE_SCALE, scale)
        _muted(row, COL_SIZE)
        row.setForeground(
            COL_DETAIL, QColor(WARNING if tier == TIER_WORKING else TEXT_MUTED)
        )
        _tickable(row)


def _make_up_tooltip(run: MountRun) -> str:
    if run.breakdown is None:
        return ""
    lines = [f"{run.display_name}, {format_bytes(run.total_bytes)}"]
    lines += [
        f"{TIER_LABELS[tier]}: {format_bytes(run.breakdown.tier_bytes(tier))}"
        for tier in ALL_TIERS
        if run.breakdown.tier_bytes(tier)
    ]
    return "\n".join(lines)


def selected_bytes(tree: QTreeWidget) -> tuple[int, int, bool]:
    """What is ticked, as (bytes, rows, whether any run stops being openable).

    A fully ticked parent is the whole folder, so its children are not counted
    again underneath it.
    """
    total = 0
    rows = 0
    grave = False
    for item in walk(tree):
        state = item.checkState(0)
        if state != Qt.CheckState.Checked:
            continue
        parent = item.parent()
        if parent is not None and parent.checkState(0) == Qt.CheckState.Checked:
            continue
        rows += 1
        total += int(item.data(0, ROLE_BYTES) or 0)
        if item.data(0, ROLE_TIER) == TIER_WORKING or item.childCount():
            grave = True
    return total, rows, grave


def walk(tree: QTreeWidget):
    """Every row in the tree, parents before the children under them."""
    stack = [tree.topLevelItem(i) for i in reversed(range(tree.topLevelItemCount()))]
    while stack:
        item = stack.pop()
        if item is None:
            continue
        yield item
        stack.extend(item.child(i) for i in reversed(range(item.childCount())))


def fill_clips(tree: QTreeWidget, clips: tuple[MountClip, ...]) -> None:
    """Rebuild the clip list. Only the original file is ever at stake here."""
    tree.clear()
    for clip in clips:
        size = clip.size_bytes or 0
        row = SortableTreeItem(
            tree, (clip.file_name, format_bytes(size), "", clip_detail(clip), ""), {COL_SIZE: size}
        )
        row.setData(0, ROLE_CLIP, str(clip.video_id))
        row.setData(0, ROLE_BYTES, size)
        row.setToolTip(COL_NAME, clip.path)
        # The sentence is longer than its column on a narrow window, and it is
        # the sentence saying why this clip may or may not go.
        row.setToolTip(COL_DETAIL, clip_detail(clip))
        _reveal(row, clip.path)
        _muted(row, COL_SIZE)
        _muted(row, COL_DETAIL)
        if clip.deletable:
            _tickable(row)
        else:
            row.setForeground(COL_NAME, QColor(TEXT_MUTED))


def clip_detail(clip: MountClip) -> str:
    """Why this clip's file may or may not go, in the terms of the work."""
    if clip.link_state != "linked":
        return CLIP_MISSING_DETAIL
    if clip.succeeded_passes == 0:
        return CLIP_NO_RUN_DETAIL
    if clip.succeeded_passes < clip.pass_count:
        return (
            f"{clip.succeeded_passes} of {clip.pass_count} sections have finished. "
            "The footage is still needed for the rest."
        )
    return CLIP_DELETABLE_DETAIL


def _size_text(run: MountRun) -> str:
    return format_bytes(run.total_bytes) if run.measured or run.total_hint else COUNTING


def set_armed(tree: QTreeWidget, armed: bool) -> None:
    """Name every ticked row in the colour of what is about to happen to it.

    The button says a delete is one click away, but not of what. A season's
    worth of runs is longer than a screen, so the answer has to be on the rows
    themselves rather than in a count at the top.
    """
    for item in walk(tree):
        if item.checkState(0) != Qt.CheckState.Checked:
            continue
        item.setForeground(COL_NAME, QColor(ERROR) if armed else QColor(_name_ink(item)))


def _name_ink(item: QTreeWidgetItem) -> str:
    """A row's ordinary name colour: dimmed for anything not on offer."""
    offered = bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    return WINDOW_TEXT if offered else TEXT_MUTED
