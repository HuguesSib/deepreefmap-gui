"""Process: assign a session's videos to transects as passes and run them."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from deepreefmap.pipeline.artifacts import ReconstructionCancelled
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_SM,
    close_icon,
    direction_arrow_icon,
    grip_icon,
)
from deepreefmap_gui.core.reveal import reveal_in_file_manager
from deepreefmap_gui.core.theme import (
    ERROR,
    GUTTER,
    RADIUS_SM,
    SPACE_SM,
    TEXT_DIM,
    TEXT_MUTED,
    UPDATE,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
)
from deepreefmap_gui.core.widgets import (
    PASS_PERCENT_ROLE,
    ColumnSpec,
    EmptyState,
    NotReadyStrip,
    StatusPillDelegate,
    configure_table,
    confirm,
    install_column_sizer,
    muted_label,
    section_card,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.simple.batch_progress import BatchProgressCard
from deepreefmap_gui.simple.section_state import (
    ATTENTION,
    BLOCKED,
    FIX_HERE,
    FIX_MACHINE,
    FIX_SETTINGS,
    SectionState,
    passes_phrase,
    run_gate,
)
from deepreefmap_gui.survey.catalogue import LINK_MISSING
from deepreefmap_gui.survey.models import (
    INFO,
    BatchItem,
    RunRecord,
    SurveyBatch,
    Transect,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.models.notification import WARNING as NOTIFY_WARNING
from deepreefmap_gui.survey.overrides import (
    effective,
    live_overrides,
    override_diff,
    override_summary,
    override_tooltip,
)
from deepreefmap_gui.survey.preset import (
    MACHINE_OVERRIDABLE_KEYS,
    describe_keys,
    manifest_config_block,
)
from deepreefmap_gui.survey.statuses import status_label
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync import wire

logger = logging.getLogger(__name__)

(
    _COL_HANDLE,
    _COL_VIDEO,
    _COL_RECORDED,
    _COL_LENGTH,
    _COL_SECTION,
    _COL_SETTINGS,
    _COL_STATUS,
    _COL_ACTION,
) = range(8)

# The clip and the section are what identify a row, so they take the slack. There
# is no Name column: an unnamed pass is named after its transect and its number,
# which the section cell already prints, and renaming is on the row's menu.
# Recorded drops first, then Length: the clip name and the section window already
# place the footage in time, and both facts stay in the row tooltip.
_PASS_COLUMNS_FIXED = {_COL_HANDLE: 22, _COL_STATUS: 150, _COL_ACTION: 34}
_PASS_COLUMNS_OPTIONAL = ((_COL_LENGTH, 80), (_COL_RECORDED, 80))
_PASS_COLUMNS_WEIGHTS = {_COL_SECTION: 3, _COL_VIDEO: 2}
_PASS_COLUMNS_MINIMUMS = {_COL_SECTION: 200, _COL_VIDEO: 120}

# What will happen to a pass when processing next starts. Every row is in
# exactly one of these, and the table is grouped in this order. NEXT holds the
# cart assembled while an order runs: those rows belong to the next session.
QUEUED, DONE, NEXT = "queued", "done", "next"
_GROUP_TITLES = {
    QUEUED: "To process",
    DONE: "Already processed",
    NEXT: "Next session",
}
_GROUP_HINTS = {
    QUEUED: "Processing works these, top to bottom. Drag a row to change the order.",
    DONE: "Succeeded once. Process again queues it for the next session.",
    NEXT: "Queued for the next session. Starts once the current one finishes.",
}
# A cart row is either in the session or out of it, so the button on the row is
# the one that takes it out. Nothing is held back any more: a pass you do not
# want processed is one you take out of the cart, and the pass itself, its clip
# and its runs all stay.
_DELETE_HINT = (
    "Take this pass out of the session. The section and its video are kept, "
    "and it can be added to a cart again."
)

# Button text per fix destination, so the strip names the place it goes rather
# than describing the journey. The header entry point uses the same words.
_FIX_ACTIONS = {FIX_MACHINE: "Open Setup", FIX_SETTINGS: "Edit settings…"}

def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _one_sentence(text: str) -> str:
    """First line of an error, short enough for a tooltip or the status line."""
    stripped = (text or "").strip()
    return stripped.splitlines()[0][:200] if stripped else ""


def _diagnose_failure(text: str) -> str:
    """Turn a raw pipeline error into one plain sentence with what to try next.

    A diver cannot act on a Python traceback. Known signatures get advice; the
    fallback keeps the raw first line so nothing is hidden. The raw text is always
    available under "Copy error details".
    """
    low = (text or "").lower()
    if "out of memory" in low or "cuda" in low and "memory" in low:
        return (
            "Out of graphics memory. Retry with a smaller processing size, or a "
            "lower batch size."
        )
    if "no space left" in low or "disk" in low and "full" in low:
        return "Out of disk space. Free space and process the pass again."
    if "not found" in low and ("model" in low or "checkpoint" in low or ".pt" in low):
        return "A required model is not installed. Install it under Setup."
    if any(word in low for word in ("decode", "codec", "corrupt", "unreadable")):
        return "The video could not be read. Check the clip copied off the camera intact."
    return _one_sentence(text) or "The run failed. No cause was recorded."


_SESSION_NAME_TOOLTIP = (
    "What to call this set of passes, usually a dive or a day. Every run records "
    "it, so Browse can group them and a copied output folder can be traced back."
)


def _unused_batch_name(wanted: str, taken: set[str]) -> str:
    """`wanted`, or the first "(2)", "(3)"… nobody else has.

    Sessions are told apart by name on the page and in the run archive, so two
    of them called the same thing cannot be told apart at all.
    """
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted} ({n})" in taken:
        n += 1
    return f"{wanted} ({n})"


def _rough_batch_time(total_seconds: float | None) -> str | None:
    """A plain "about N hours" for a session that has not started yet.

    Takes the predicted total rather than a pass count: a queue of thirty-second
    sections and a queue of ten-minute ones are not the same evening, and
    counting passes said they were.
    """
    if not total_seconds:
        return None
    if total_seconds < 5400:
        return f"about {max(1, round(total_seconds / 60))} minutes"
    hours = round(total_seconds / 3600)
    return f"about {hours} hour{'' if hours == 1 else 's'}"


def _import_summary(file_name: str, queued: int, skipped: int, unmatched: int) -> str:
    """What a CSV import did, including the rows it could not place."""
    parts = [f"Queued {queued} pass(es) from {file_name}."]
    if skipped:
        parts.append(f"{skipped} video(s) could not be opened and were excluded.")
    if unmatched:
        parts.append(f"{unmatched} named a transect that is not yet planned.")
    return " ".join(parts)


def _pass_number(run_dir_name: str) -> int | None:
    """The `pNN` ordinal baked into a survey run-dir slug, if one is present."""
    for token in run_dir_name.split("__"):
        if len(token) > 1 and token[0] == "p" and token[1:].isdigit():
            return int(token[1:])
    return None


def _failed_pass_label(transect: Transect | None, run_dir_name: str) -> str:
    """Name a failed pass by transect and pass number, never by the run-dir slug."""
    name = transect.name if transect is not None else "Unassigned"
    number = _pass_number(run_dir_name)
    return f"{name} pass {number}" if number is not None else name


def _button_column_width(parent: QWidget, labels: Iterable[str]) -> int:
    """Width a cell button needs for the widest label it can hold.

    Measured against a real button of the same kind, so the column follows the
    font the machine is actually running: the padding, the border and the
    stylesheet are all in the figure, and none of them are constants here.

    Both dresses of the button are measured, because a row marked amber carries
    its own stylesheet and its own padding with it, and the column has to hold
    the wider of the two rather than whichever one happened to be on screen.
    """
    probe = QPushButton(parent)
    probe.setProperty("quiet", "true")
    probe.setVisible(False)
    widest = 0
    for warned in (False, True):
        _style_warning_cell(probe, ok=not warned, filled=False)
        for label in labels:
            probe.setText(label)
            probe.ensurePolished()
            widest = max(widest, probe.sizeHint().width())
    probe.deleteLater()
    # Room for the view's own cell margins either side, which the button never
    # gets and which are what turned "Default settings" into "efault setting".
    return widest + 2 * SPACE_SM


def _style_warning_cell(button: QPushButton, *, ok: bool, filled: bool = True) -> None:
    """Mark a cell that needs a second look.

    ``filled`` is for a cell that has to look wrong from across the room. The
    outlined variant is for one that is merely worth checking, and which may be
    right: filling every row of a genuinely one-way survey turns the table amber
    and says nothing. A pass filed against no transect takes the outlined
    variant, because that is a choice rather than an omission.

    A per-widget stylesheet replaces the global button rule outright, so both
    variants restate the padding and radius they displace.
    """
    if ok:
        button.setStyleSheet("")
        return
    background = f"background-color: {WARN_BG};" if filled else ""
    button.setStyleSheet(
        f"QPushButton {{ {background} color: {WARN_TEXT};"
        f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px;"
        " padding: 4px 8px; text-align: left; }"
    )


def _style_missing_cell(button: QPushButton) -> None:
    """Mark the cell of a pass whose footage is not there.

    A dashed red outline rather than the amber the other notices use: the rest
    are worth a look, and this one cannot run at all. The notification centre
    counts these, but a count in the corner does not say which rows, which is
    the question asked while looking at the table.
    """
    button.setStyleSheet(
        f"QPushButton {{ color: {ERROR}; border: 1px dashed {ERROR};"
        f" border-radius: {RADIUS_SM}px; padding: 4px 8px; text-align: left; }}"
    )


def _probe_video(path: str) -> tuple[float, float] | None:
    """(duration_s, fps) via cv2, or None when the file cannot be decoded.

    Opening and measuring a 4 GB clip off an SD card takes long enough to freeze
    the window, which is why _add_video_paths hands this to a worker thread.
    """
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    try:
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
    finally:
        cap.release()
    if not fps or fps <= 0 or not frames or frames <= 0:
        return None
    return float(frames) / float(fps), float(fps)


def _clip_time(mtime: str | None) -> str:
    """When the clip was recorded, in local time.

    A GoPro's mtime is the moment recording stopped, which is what a diver
    matches against their slate.
    """
    if not mtime:
        return "time unknown"
    try:
        stamp = datetime.fromisoformat(mtime)
    except ValueError:
        return "time unknown"
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%H:%M")


def _span_length(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "length unknown"
    total = int(round(seconds))
    return f"{total}s" if total < 60 else f"{total // 60}m {total % 60:02d}s"


def _clip_name(videos: list[VideoAsset]) -> str:
    """The clip, and how many chapters follow it.

    A column each for the name, the time and the length. Run together in one
    cell they read as a single unpunctuated string, and a card of GX01nnnn.MP4
    files is hard enough to tell apart already.
    """
    name = videos[0].file_name
    if len(videos) > 1:
        name += f" +{len(videos) - 1} chapter{'' if len(videos) == 2 else 's'}"
    return name


def _clip_tooltip(videos: list[VideoAsset]) -> str:
    """The files behind the row, and what "+n chapters" on it means.

    A GoPro splits a recording at about 4 GB, so a long swim arrives as several
    files. They are one recording and the pass covers them played back to back,
    which is what the count on the name is saying.
    """
    paths = "\n".join(video.path for video in videos)
    if len(videos) == 1:
        return paths
    return (
        f"One recording the camera split into {len(videos)} files. The pass "
        f"covers them played back to back.\n{paths}"
    )


def _total_duration_s(videos: list[VideoAsset]) -> float | None:
    """Length of the chapters played back to back, or None when any is unknown."""
    if any(video.duration_s is None for video in videos):
        return None
    return sum(video.duration_s or 0.0 for video in videos)


@dataclass
class _PassRow:
    videos: list[VideoAsset]
    begin_s: float
    end_s: float
    direction: str = "forward"
    transect_id: uuid.UUID | None = None
    pass_id: uuid.UUID | None = None
    # The section's own name. Empty means it has never been renamed, and the
    # generated default stands in.
    label: str = ""
    # What this pass alone changes about the session's settings, as stored on
    # its cart row. Empty for a pass that runs on the session's settings.
    overrides: dict = field(default_factory=dict)
    # A row of the next session's cart, shown under its own divider while an
    # order runs. Display state, not persisted: membership lives in batch_item.
    in_cart: bool = False

    @property
    def video(self) -> VideoAsset:
        """The first chapter, which is the pass's video identity."""
        return self.videos[0]

    def total_duration_s(self) -> float | None:
        return _total_duration_s(self.videos)


def _clip_sort_key(row: _PassRow) -> tuple[int, str]:
    """Order by recording time, with unreadable timestamps kept at the bottom."""
    return (1, "") if not row.video.mtime else (0, row.video.mtime)


@dataclass
class _SurveyJob:
    run: RunRecord
    pass_: TransectPass
    # None for a pass processed without one. The run is then unscaled and carries
    # no transect block in its manifest.
    transect: Transect | None
    videos: list[VideoAsset]
    dir_name: str
    # What the survey calls this section, which is what the run is reported and
    # read back under. The directory keeps its own unique, filesystem-safe name.
    label: str = ""
    # The run kwargs and the configuration identity for this pass alone, both
    # read off the form at checkout with the row's overrides applied. Per job
    # rather than per batch, because a row may run on settings of its own.
    settings: dict = field(default_factory=dict)
    config: dict | None = None


class PassTable(QTableWidget):
    """The cart's table, whose queued rows are dragged into a processing order.

    Qt's own internal move takes the items and leaves the cell widgets behind,
    and four of these columns are widgets, so the drop is intercepted and
    reported instead: the window reorders its own rows and repaints the table.
    """

    rows_moved = Signal(int, int)
    # Row under the cursor and its status cell in global coordinates, or
    # (-1, None) when the pointer is elsewhere. A table has no per-cell hover
    # signal of its own, and the breakdown has to be anchored to the row it
    # describes rather than left to float wherever the pointer happens to be.
    status_hovered = Signal(int, object)

    def __init__(self, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(0, columns, parent)
        self.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.verticalHeader().setSectionsMovable(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        index = self.indexAt(event.position().toPoint())
        if not index.isValid() or index.column() != _COL_STATUS:
            self.status_hovered.emit(-1, None)
            return
        rect = self.visualRect(index)
        self.status_hovered.emit(
            index.row(), QRect(self.viewport().mapToGlobal(rect.topLeft()), rect.size())
        )

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.status_hovered.emit(-1, None)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        source = self.currentRow()
        target = self.rowAt(int(event.position().y()))
        # Past the last row lands after it, which is what dropping into the
        # empty space below a short list means.
        if target < 0:
            target = self.rowCount() - 1
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if source >= 0 and target >= 0 and source != target:
            self.rows_moved.emit(source, target)


class SimpleBatchMixin(MixinBase):
    """DeepReefMapWindow methods for the Process destination."""

    # Set while the table is being filled, because filling a cell emits the same
    # signal a rename does.
    _survey_table_rebuilding: bool = False

    def _build_simple_run_page(self) -> QWidget:
        """Process: the session's passes, and what will be done to them."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._survey_rows = []
        # Table row -> index into _survey_rows, with None for a group heading.
        self._survey_table_index: list[int | None] = []
        self._survey_transects = []
        self._survey_batch = None
        # The order currently running, distinct from _survey_batch, which a cart
        # minted mid-run takes over.
        self._survey_running_batch = None
        self._survey_cancel_event = None
        self._survey_worker_running = False
        # Which job the worker is on, and the pass behind each job, so a percent
        # arriving from the pipeline can find the row it belongs to.
        self._survey_running_index: int | None = None
        self._survey_job_pass_ids: list[uuid.UUID] = []
        self._survey_pass_percent = 0
        self._reload_active_preset()

        layout.setSpacing(GUTTER)

        # Above everything, so a laptop that cannot run says so before the user
        # fills a queue with it. _recompute_survey_start owns its text.
        self._survey_not_ready = NotReadyStrip()
        self._survey_not_ready.action_clicked.connect(self._on_survey_fix_blocker)
        layout.addWidget(self._survey_not_ready)

        # Batch identity and the settings every pass in it will run under, as
        # one block: they answer the same question, "what is about to happen".
        header_card, header_layout = section_card()
        name_row = QHBoxLayout()
        name_row.setSpacing(SPACE_SM)
        name_row.addWidget(QLabel("Session"))
        self._survey_batch_name = QLineEdit(datetime.now().strftime("%Y-%m-%d"))  # noqa: DTZ005 (local time is intended: this is a user-facing default name)
        # Defaulting to today's date makes this look like decoration, so the
        # tooltip says what the name is actually for. It is written into every
        # run's manifest, which is what lets a copied output folder rebuild the
        # session it came from, and it is what groups those runs in Browse.
        self._survey_batch_name.setToolTip(_SESSION_NAME_TOOLTIP)
        name_row.addWidget(self._survey_batch_name, 1)
        self._survey_clear_cart_btn = QPushButton("Clear cart")
        self._survey_clear_cart_btn.setToolTip(
            "Take every pass out of this session's cart. The passes and their "
            "video files are kept and can be added to a cart again."
        )
        self._survey_clear_cart_btn.clicked.connect(self._on_survey_clear_cart)
        name_row.addWidget(self._survey_clear_cart_btn)
        header_layout.addLayout(name_row)
        # Only while an order runs and a next cart exists: names where new
        # additions are going, since the table above belongs to the order.
        self._survey_next_cart_label = QLabel("")
        self._survey_next_cart_label.setWordWrap(True)
        self._survey_next_cart_label.setStyleSheet(f"color: {UPDATE};")
        self._survey_next_cart_label.setVisible(False)
        header_layout.addWidget(self._survey_next_cart_label)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(SPACE_SM)
        self._survey_preset_label = QLabel(self._survey_preset_summary())
        self._survey_preset_label.setWordWrap(True)
        self._survey_preset_label.setStyleSheet(f"color: {TEXT_MUTED};")
        preset_row.addWidget(self._survey_preset_label, 1)
        self._survey_settings_btn = QPushButton("Edit settings…")
        self._survey_settings_btn.setProperty("quiet", "true")
        self._survey_settings_btn.setToolTip("Change any run setting for this session.")
        self._survey_settings_btn.clicked.connect(self._on_edit_run_settings)
        preset_row.addWidget(self._survey_settings_btn)
        # Beside the name of the settings, because that is where you look when you
        # want to know whether this machine is still on the standard.
        self._survey_restore_btn = QPushButton("Restore standard settings")
        self._survey_restore_btn.setProperty("quiet", "true")
        self._survey_restore_btn.setToolTip(
            "Discard this machine's changes and return to the standard settings."
        )
        self._survey_restore_btn.clicked.connect(self._restore_standard_settings)
        preset_row.addWidget(self._survey_restore_btn)
        self._survey_server_preset_btn = QPushButton("Server presets…")
        self._survey_server_preset_btn.setProperty("quiet", "true")
        self._survey_server_preset_btn.setToolTip(
            "Use a preset your organisation published on the registry. "
            "Additional to the standard settings, never a replacement."
        )
        self._survey_server_preset_btn.clicked.connect(self._on_choose_server_preset)
        preset_row.addWidget(self._survey_server_preset_btn)
        self._survey_audit_btn = QPushButton("Settings history…")
        self._survey_audit_btn.setProperty("quiet", "true")
        self._survey_audit_btn.setToolTip("Which settings every processed run actually used.")
        self._survey_audit_btn.clicked.connect(self._on_show_config_audit)
        preset_row.addWidget(self._survey_audit_btn)
        header_layout.addLayout(preset_row)
        layout.addWidget(header_card)

        # The session total. Built here because the table below is wired to it,
        # but added under that table: it is the sum of those rows, and each row
        # carries its own progress now.
        self._batch_progress = BatchProgressCard()
        self._batch_progress.pass_percent_changed.connect(self._on_pass_percent)

        # One column per heading, counted from the headings themselves: a table
        # built wider than its labels ends with columns Qt names "9" and "10".
        headings = [
            "",
            "Clip",
            "Recorded",
            "Length",
            "Transect + section",
            "Settings",
            "Status",
            "",
        ]
        self._survey_pass_table = PassTable(len(headings))
        # Half the columns hold cell widgets, which paint their own background,
        # so the alternate row fill would stop halfway across a row.
        configure_table(self._survey_pass_table, headings, alternating=False)
        self._survey_pass_table.verticalHeader().setDefaultSectionSize(34)
        # A day is dozens of clips, and the settings actions act on a run of
        # them at once: select the rows, set them once.
        self._survey_pass_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._survey_pass_table.setItemDelegateForColumn(_COL_STATUS, StatusPillDelegate(self))
        self._survey_pass_table.itemSelectionChanged.connect(self._recompute_row_actions)
        self._survey_pass_table.rows_moved.connect(self._on_survey_rows_moved)
        # The running row's percentage is a summary; hovering it gives the
        # stage-by-stage detail that used to live in the window's top corner.
        self._survey_pass_table.status_hovered.connect(self._on_queue_row_hover)
        # Scrolling moves the rows out from under a popup anchored to one of
        # them, which would leave the breakdown pointing at a different pass.
        self._survey_pass_table.verticalScrollBar().valueChanged.connect(
            lambda _value: self._on_queue_row_hover(-1, None)
        )
        # Seeing the result is what you actually want after processing, so the
        # row you processed opens it. Only the plain-item columns get the signal;
        # the rest hold cell widgets that eat the click.
        self._survey_pass_table.cellDoubleClicked.connect(self._on_survey_pass_activated)
        # A failed pass keeps its error on the row (tooltip). Right-click copies
        # the full text so it can be pasted into a bug report.
        self._survey_pass_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._survey_pass_table.customContextMenuRequested.connect(self._on_survey_pass_menu)
        # Nothing here is edited in place: renaming is on the row's menu, and a
        # double-click opens the run the row produced.
        self._survey_pass_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h_header = self._survey_pass_table.horizontalHeader()
        # This table cannot sort by a header click: half its columns are cell
        # widgets Qt's sort will not move, the groups sit under spanned heading
        # rows, and _survey_table_index maps table rows to model rows by
        # position. The order it does have is the one the rows are dragged into.
        h_header.setSectionsClickable(False)
        h_header.setHighlightSections(False)
        # Measured from the labels it takes, not guessed: a fixed width wide
        # enough for "Default settings" in one font clips it in a larger one.
        self._pass_columns = ColumnSpec(
            fixed={
                **_PASS_COLUMNS_FIXED,
                _COL_SETTINGS: _button_column_width(
                    self._survey_pass_table,
                    (override_summary({}), override_summary({"a": 1, "b": 2})),
                ),
            },
            weights=_PASS_COLUMNS_WEIGHTS,
            minimums=_PASS_COLUMNS_MINIMUMS,
            optional=_PASS_COLUMNS_OPTIONAL,
        )
        self._pass_column_sizer = install_column_sizer(
            self._survey_pass_table, self._pass_columns, settings_key="passes"
        )
        # Footage is imported under Videos and staged from there, so this table
        # takes no drops from outside: a clip dropped here would arrive with no
        # window cut from it and no transect, which is the state Videos exists
        # to fill in. The only drag it knows is a row moved within itself.

        self._survey_table_stack = QStackedWidget()
        self._survey_table_stack.addWidget(self._survey_pass_table)
        self._survey_table_stack.addWidget(
            EmptyState(
                "No videos in this session",
                "Cut sections from your clips in Browse to queue them here.",
            )
        )
        passes_card, passes_layout = section_card("Passes")
        passes_layout.addWidget(self._survey_table_stack, 1)
        passes_layout.addWidget(self._batch_progress)
        # How much of the batch is behind you. Sits with the table rather than in
        # the status bar because it is what you read before deciding to stop.
        self._survey_standing_label = QLabel("")
        self._survey_standing_label.setWordWrap(True)
        self._survey_standing_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._survey_standing_label.setVisible(False)
        passes_layout.addWidget(self._survey_standing_label)
        # Outcome of the last batch, kept on the page rather than written to the
        # status bar, which the next thing to happen overwrites.
        self._survey_summary_label = QLabel("")
        self._survey_summary_label.setWordWrap(True)
        self._survey_summary_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._survey_summary_label.setVisible(False)
        passes_layout.addWidget(self._survey_summary_label)

        # The actions live inside the card, under the table they act on, and the
        # row is labelled with what it acts on so no button has to say "selected"
        # in its own label.
        #
        # Nothing here edits a section any more. Transect, direction and trim
        # belong to the swim and are set under Videos; the cart decides the
        # order, the settings, and what is in it.
        selection_row = QHBoxLayout()
        selection_row.setSpacing(SPACE_SM)
        self._survey_selection_label = muted_label("With the selected rows")
        selection_row.addWidget(self._survey_selection_label)
        self._survey_bulk_settings_btn = QPushButton("Settings…")
        self._survey_bulk_settings_btn.setToolTip(
            "Change the run settings for every selected pass, leaving the rest "
            "of the session on its own settings."
        )
        self._survey_bulk_settings_btn.clicked.connect(self._on_survey_bulk_settings)
        selection_row.addWidget(self._survey_bulk_settings_btn)
        self._survey_copy_settings_btn = QPushButton("Copy settings from…")
        self._survey_copy_settings_btn.setToolTip(
            "Give the selected passes the settings another pass or an earlier "
            "run used."
        )
        self._survey_copy_settings_btn.clicked.connect(self._on_survey_copy_settings)
        selection_row.addWidget(self._survey_copy_settings_btn)
        self._survey_remove_btn = QPushButton("Remove from session")
        self._survey_remove_btn.setToolTip(
            "Take every selected pass out of this session's cart. The passes "
            "and their video files are kept."
        )
        self._survey_remove_btn.clicked.connect(self._on_survey_remove_pass)
        selection_row.addWidget(self._survey_remove_btn)
        selection_row.addStretch(1)
        self._survey_sort_btn = QPushButton("Sort by time")
        self._survey_sort_btn.setProperty("quiet", "true")
        self._survey_sort_btn.setToolTip(
            "Put every row in the order its clip was recorded, which a dragged "
            "row then departs from."
        )
        self._survey_sort_btn.clicked.connect(self._on_survey_sort_by_time)
        selection_row.addWidget(self._survey_sort_btn)
        passes_layout.addLayout(selection_row)

        # Starting belongs with the table it consumes, not in a page footer
        # where it would share a position and a fill with navigation. The label
        # names what it will do and how much of it. Stopping is the bottom bar's.
        start_row = QHBoxLayout()
        start_row.setSpacing(SPACE_SM)
        start_row.addStretch(1)
        # Its own quiet button rather than the start button changing into it.
        # A control that swaps between "commit the machine for hours" and "go
        # and look at what came out" teaches nothing about either.
        self._survey_results_btn = QPushButton("See the results")
        self._survey_results_btn.setProperty("quiet", "true")
        self._survey_results_btn.setToolTip("Open what this session has produced so far.")
        self._survey_results_btn.setVisible(False)
        self._survey_results_btn.clicked.connect(partial(self._go_to_section, "browse"))
        start_row.addWidget(self._survey_results_btn)
        self._survey_start_btn = QPushButton("Start processing")
        self._survey_start_btn.setProperty("cta", "true")
        self._survey_start_btn.setEnabled(False)
        self._survey_start_btn.clicked.connect(self._on_survey_start)
        start_row.addWidget(self._survey_start_btn)
        passes_layout.addLayout(start_row)

        layout.addWidget(passes_card, 1)
        self._recompute_row_actions()
        return page

    def _set_batch_editing_enabled(self, enabled: bool) -> None:
        """Freeze the running order while it is processed; the next cart stays live.

        A settings edit mid-run would never reach the pass in flight, so an
        order row's settings freeze. Cart rows are the next session's and stay
        editable throughout. Taking a row out stays open on both: the worker
        re-reads the cart between passes, so a row removed before it starts is
        one the session no longer processes.

        The three section cells never freeze. They edit nothing here: they open
        the section under Videos, which is worth doing while a batch runs.
        """
        for widget in (
            self._survey_batch_name,
            self._survey_clear_cart_btn,
            self._survey_settings_btn,
            self._survey_audit_btn,
        ):
            widget.setEnabled(enabled)
        # Through _model_index rather than the raw index list: a repaint arriving
        # between a row mutation and the table rebuild must not index a row that
        # is gone.
        for table_row in range(self._survey_pass_table.rowCount()):
            model_index = self._model_index(table_row)
            if model_index is None:
                continue
            row = self._survey_rows[model_index]
            settings = self._survey_pass_table.cellWidget(table_row, _COL_SETTINGS)
            if settings is not None:
                settings.setEnabled(enabled or row.in_cart)

    def _recompute_row_actions(self) -> None:
        """Every action under the table acts on the selection."""
        running = self._survey_worker_running
        selection = self._selected_survey_rows()
        has_selection = bool(selection) and not running
        self._survey_remove_btn.setEnabled(has_selection)
        self._survey_bulk_settings_btn.setEnabled(has_selection)
        self._survey_copy_settings_btn.setEnabled(has_selection)
        self._survey_sort_btn.setEnabled(len(self._survey_rows) > 1 and not running)
        self._survey_table_stack.setCurrentIndex(0 if self._survey_rows else 1)
        # Dragging reorders what is still to run, so it has nothing to do while
        # the session that would run it is already running.
        self._survey_pass_table.setDragEnabled(not running)
        self._set_batch_editing_enabled(not running)

    def _selected_survey_rows(self) -> list[int]:
        """Indices of every selected row, in table order.

        Read from the selection rather than currentRow(): the bulk actions are
        the reason the table is multi-select in the first place.
        """
        table_rows = {index.row() for index in self._survey_pass_table.selectedIndexes()}
        models = {self._model_index(row) for row in table_rows}
        return sorted(index for index in models if index is not None)

    def _on_survey_pass_activated(self, table_row: int, _column: int) -> None:
        """Open the run this pass produced, over in Browse.

        The point cloud lives in Browse and nowhere else, so activating a finished
        row travels there rather than opening a viewer beside the queue.
        """
        if self._run_in_flight():
            self._status_label.setText("Unavailable while processing.")
            return
        index = self._model_index(table_row)
        if index is None:
            return
        row = self._survey_rows[index]
        if row.pass_id is None:
            self._status_label.setText("Pass not yet processed.")
            return
        runs = self._survey_store().runs_for_pass(row.pass_id)
        # The last run that actually succeeded, not simply the last one: a pass
        # retried after a failure would otherwise open a directory with no
        # manifest in it.
        succeeded = [run for run in runs if run.status == "succeeded"]
        if not succeeded:
            last = runs[-1] if runs else None
            if last is not None and last.status == "failed":
                self._status_label.setText(f"Pass failed: {_diagnose_failure(last.error)}")
            else:
                self._status_label.setText("Pass has no successful run to open.")
            return
        run_dir = Path(self._out_root_input.text()).expanduser() / succeeded[-1].run_dir_name
        if not run_dir.is_dir():
            self._status_label.setText(f"Run folder is missing: {run_dir.name}")
            return
        # Land in Browse first, so the run that just finished is opened from the
        # archive it now belongs to. The load itself enters View mode when the
        # cloud is on screen, which is where the viewer pane is revealed.
        self._go_to_section("browse")
        self._auto_load_run(run_dir)

    def _on_edit_run_settings(self) -> None:
        """Open the real run form in a dialog, and keep the edit only on OK."""
        from deepreefmap_gui.simple.settings_dialog import RunSettingsDialog

        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        # The output root is the one thing left in the form that is not a
        # setting, and Setup is where it is edited. The group goes rather
        # than the row inside it, because Setup has borrowed the controls
        # out of it and an empty titled frame says nothing.
        per_run: list[QWidget] = [self._output_group]
        # The dialog edits the live form widgets, so abandoning the edit means
        # putting the values back here rather than dropping a pending copy.
        before = self._snapshot_form_settings()
        dialog = RunSettingsDialog(self, self._setup_page, per_run)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.restore_form()
        if accepted:
            self._adopt_form_as_preset()
        else:
            # Cancel, Escape and the window's close button all land here.
            self._restore_form_settings(before)
        self._recompute_survey_start()

    def _survey_preset_summary(self) -> str:
        """Name the settings this batch will run under, then say how they differ.

        The configuration has an identity worth citing in a report, so the name
        and version lead. The technical line still follows, read from the form
        rather than the preset dict: the batch runs from _collect_run_settings(),
        so the label must describe the values the run will actually use.
        """
        if self._survey_preset is None or self._active_preset is None:
            return "Settings could not be loaded. Correct the settings file before processing."
        org = self._active_preset.org
        lines = [f"Settings: {org.label}"]
        if org.locked:
            lines[0] += ", set by your organisation"
        elif org.source == "server":
            lines[0] += ", from the server"
        # Split by what this machine is allowed to keep rather than by what is
        # already on disk: the answer must not change depending on whether the
        # settings dialog has closed yet. The page carries it, not the status
        # bar, because the next thing to happen overwrites the status bar.
        deviations = self._survey_deviations()
        machine = [key for key in deviations if key in MACHINE_OVERRIDABLE_KEYS]
        organisation = [key for key in deviations if key not in MACHINE_OVERRIDABLE_KEYS]
        if machine:
            lines.append(f"Changed on this machine: {describe_keys(machine)}.")
        if organisation:
            lines.append(
                f"Changed for this session only: {describe_keys(organisation)}."
                f" {org.name} sets these, so they go back to standard next launch."
            )
        # What a registry preset named and this build ignored: the value that
        # was dropped is the one the author meant, so applying the rest without
        # saying so would misdescribe the batch.
        if org.degraded:
            lines.append(org.dropped_summary)
        withdrawn = getattr(self, "_server_preset_withdrawn", "")
        if withdrawn:
            lines.append(
                f"The selected server preset {withdrawn} is no longer on the "
                "registry, so the standard settings are in force."
            )
        s = self._collect_run_settings()
        lines.append(
            f"{s['segmentation_name']} + {s['mapping_name']}"
            f" @ {s['fps']} fps, {s['camera_profile_name']}"
        )
        return "\n".join(lines)

    def _on_show_config_audit(self) -> None:
        """List what every processed run under this output root actually used."""
        from deepreefmap_gui.simple.config_audit_dialog import ConfigAuditDialog
        from deepreefmap_gui.survey.config_audit import audit_out_root

        if self._active_preset is None:
            self._status_label.setText("The settings could not be read.")
            return
        org = self._active_preset.org
        rows = audit_out_root(Path(self._out_root_input.text()).expanduser(), org)
        ConfigAuditDialog(self, rows, org).exec()

    # --- Batch and table state ---

    def _ensure_cart_batch(self) -> SurveyBatch:
        """The cart: the newest un-started session, minted when first needed.

        A started order is closed to new members, so a fresh session is minted
        and adopted in its place; the running order stays reachable on
        _survey_running_batch.
        """
        store = self._survey_store()
        batch = self._survey_batch
        if batch is not None:
            running = self._survey_running_batch
            started = store.batch_run_count(batch.id) > 0 or (
                running is not None and batch.id == running.id
            )
            if not started:
                return batch
        name = self._survey_batch_name.text().strip()
        # A cart minted under a started order must not inherit its name.
        if not name or (batch is not None and name == batch.name):
            name = datetime.now().strftime("%Y-%m-%d")  # noqa: DTZ005 (local time is intended: this is a user-facing default name)
        # The date fallback is what the running order was probably named after,
        # so uniqueness is enforced against every session rather than one.
        name = _unused_batch_name(name, {b.name for b in store.list_batches()})
        cart = SurveyBatch(name=name)
        # Name the configuration on the batch too, so a folder rebuilt from
        # manifests alone still knows which settings the day was run under.
        if self._active_preset is not None:
            cart.preset_name = self._active_preset.org.name
        store.add_batch(cart)
        self._survey_batch = cart
        if not self._survey_worker_running:
            self._survey_batch_name.setText(cart.name)
        return cart

    def _update_cart_button(self) -> None:
        """The badge: queued, un-held passes of the current cart.

        During a run that is the Next session rows. A continuable order counts
        zero: its remaining passes are an order's, not a cart's.
        """
        button = getattr(self, "_cart_button", None)
        if button is None:
            return  # pages are built before the header button exists
        states = self._survey_row_states()
        if self._survey_worker_running:
            count = states.count(NEXT)
        else:
            store = self._try_survey_store()
            batch = self._survey_batch
            is_cart = (
                batch is not None
                and store is not None
                and store.batch_run_count(batch.id) == 0
            )
            count = states.count(QUEUED) if is_cart else 0
        button.set_count(count)

    def _cart_add(self, pass_id: uuid.UUID) -> None:
        """Record cart membership without repainting; already a member is a no-op."""
        store = self._survey_store()
        batch = self._ensure_cart_batch()
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_id))
        # A pass with no origin session adopts this one.
        pass_ = store.get_pass(pass_id)
        if pass_ is not None and pass_.batch_id is None:
            pass_.batch_id = batch.id
            store.update_pass(pass_)

    def _add_pass_to_cart(self, pass_id: uuid.UUID) -> None:
        """Queue a pass for the next session, from anywhere in the app.

        Trim, direction and transect ride on the pass itself; settings are
        per-order and read at checkout.
        """
        self._cart_add(pass_id)
        self._refresh_survey_batch_tab()
        self._status_label.setText("Added to the cart.")

    def _take_pass_out_of_cart(self, pass_id: uuid.UUID) -> None:
        """Un-cart a pass from anywhere in the app.

        The other half of _add_pass_to_cart, so a cart control elsewhere can be
        one control rather than an add beside a delete. The pass, its clip and
        its runs all stay; only the membership goes.
        """
        store = self._try_survey_store()
        cart = store.current_cart() if store is not None else None
        if store is None or cart is None:
            return
        store.remove_batch_item(cart.id, pass_id)
        self._refresh_survey_batch_tab()

    def _on_survey_clear_cart(self) -> None:
        """Empty the current session's cart in one action.

        Un-carts membership only: the passes, their videos and the session all
        stay in the store, so a cleared pass can be carted again from Transects
        or Browse. Repainting goes through _refresh_survey_batch_tab, which is
        the one place _survey_table_index is rebuilt in step with _survey_rows.
        """
        store = self._try_survey_store()
        batch = self._survey_batch
        if store is None or batch is None:
            return
        items = store.list_batch_items(batch.id)
        if not items:
            self._status_label.setText("The cart is already empty.")
            return
        if not confirm(
            self,
            "Clear the cart?",
            f"Take {passes_phrase(len(items))} out of '{batch.name}'? The "
            "passes are kept and can be added to a cart again.",
        ):
            return
        for item in items:
            store.remove_batch_item(batch.id, item.pass_id)
        self._refresh_survey_batch_tab()
        self._status_label.setText(
            f"Cleared {passes_phrase(len(items))} from the cart."
        )

    def _refresh_survey_batch_tab(self) -> None:
        """Rebuild the pass table from the store.

        Shows the running order (with the next cart's rows under their own
        divider) while a batch runs; else the current cart; else the newest
        order, which stays continuable while it has queued items.
        """
        store = self._try_survey_store()
        if store is None:
            self._survey_transects = []
            self._rebuild_survey_table()
            self._recompute_survey_start()
            return
        self._survey_transects = store.list_transects()
        shown: SurveyBatch | None
        if self._survey_worker_running and self._survey_running_batch is not None:
            shown = self._survey_running_batch
            cart = store.current_cart()
        else:
            if self._survey_batch is None:
                cart = store.current_cart()
                batches = store.list_batches() if cart is None else []
                self._survey_batch = cart if cart is not None else (
                    batches[0] if batches else None
                )
                if self._survey_batch is not None:
                    self._survey_batch_name.setText(self._survey_batch.name)
            shown = self._survey_batch
            cart = None  # no divider when nothing is running
        self._survey_rows = []
        self._survey_pass_table.setRowCount(0)
        if shown is not None:
            self._survey_rows.extend(self._rows_for_batch(store, shown))
        if cart is not None and (shown is None or cart.id != shown.id):
            for row in self._rows_for_batch(store, cart):
                row.in_cart = True
                self._survey_rows.append(row)
        self._refresh_next_cart_label(cart)
        self._rebuild_survey_table()
        self._recompute_survey_start()
        # Every cart change comes through here, and the Videos page marks each
        # section that is in it. Without this a pass taken out of the cart here
        # goes on claiming to be in it there until the page is rebuilt.
        self._refresh_cart_marks()

    def _rows_for_batch(self, store: SurveyStore, batch: SurveyBatch) -> list[_PassRow]:
        """The session's worklist as table rows, in the order it will be processed."""
        overrides = {
            item.pass_id: item.overrides for item in store.list_batch_items(batch.id)
        }
        rows = []
        for pass_ in store.passes_in_batch(batch.id):
            # A chapter the library has lost is dropped rather than faked, so
            # the row cannot claim a file the run would fail to open.
            videos = [store.get_video(video_id) for video_id in pass_.video_ids()]
            if any(video is None for video in videos):
                continue
            rows.append(_PassRow(
                videos=[video for video in videos if video is not None],
                begin_s=pass_.begin_s,
                end_s=pass_.end_s,
                direction=pass_.direction,
                transect_id=pass_.transect_id,
                pass_id=pass_.id,
                label=pass_.label,
                overrides=dict(overrides.get(pass_.id, {})),
            ))
        return rows

    def _refresh_next_cart_label(self, cart: SurveyBatch | None) -> None:
        """Say which session an addition joins while another one is running.

        The table holds rows from two sessions at that point, told apart only by
        a group heading under one name field.
        """
        if cart is None:
            self._survey_next_cart_label.setVisible(False)
            self._survey_batch_name.setReadOnly(False)
            self._survey_batch_name.setToolTip(_SESSION_NAME_TOOLTIP)
            return
        count = len(self._survey_store().list_batch_items(cart.id))
        self._survey_next_cart_label.setText(
            f"Adding to <b>{cart.name}</b>, which starts once this session "
            f"finishes. {passes_phrase(count)} queued so far."
        )
        self._survey_next_cart_label.setVisible(True)
        # The field names the session being processed, and editing it while
        # additions go to a different one renames the wrong thing.
        self._survey_batch_name.setReadOnly(True)
        self._survey_batch_name.setToolTip(
            "The session being processed. The next one is named when this "
            "finishes."
        )

    def _refresh_survey_transect_names(self) -> None:
        """Re-read the transects and repaint the names the rows show.

        Transects page calls this when one is renamed or added: the cart shows
        the name, it does not choose it.
        """
        store = self._try_survey_store()
        self._survey_transects = store.list_transects() if store is not None else []
        for index in range(len(self._survey_rows)):
            self._refresh_row_widgets(index)

    def _transect_cell_text(self, transect_id: uuid.UUID | None) -> str:
        """The transect a pass is filed against, or that it is filed against none.

        "No transect" rather than a blank: the pass runs either way, so this is
        a choice with a consequence you can read. What it costs is comparison --
        a pass filed against no transect cannot be set beside repeat passes of
        the same place.
        """
        if transect_id is None:
            return "No transect"
        return next(
            (t.name for t in self._survey_transects if t.id == transect_id),
            "Unknown transect",
        )

    # --- Table shape ---

    def _survey_row_states(self) -> list[str]:
        """Which group each row belongs to, from one query rather than per row."""
        if not self._survey_rows:
            return []
        store = self._try_survey_store()
        shown = (
            self._survey_running_batch
            if self._survey_worker_running and self._survey_running_batch is not None
            else self._survey_batch
        )
        # Succeeded within the shown session only: a pass re-ordered in a new
        # cart has succeeded before, but not yet in that session, and DONE
        # would silently drop it from the next batch.
        succeeded = (
            store.succeeded_pass_ids(shown.id)
            if store is not None and shown is not None
            else set()
        )
        states = []
        for row in self._survey_rows:
            if row.in_cart:
                states.append(NEXT)
            elif row.pass_id is not None and row.pass_id in succeeded:
                states.append(DONE)
            else:
                states.append(QUEUED)
        return states

    def _model_index(self, table_row: int) -> int | None:
        """The pass behind a table row, or None for a group heading.

        Bounds-checked on both sides: an index read between a row mutation and
        the rebuild must never reach past _survey_rows.
        """
        if not 0 <= table_row < len(self._survey_table_index):
            return None
        index = self._survey_table_index[table_row]
        if index is not None and not 0 <= index < len(self._survey_rows):
            return None
        return index

    def _table_row_of(self, model_index: int) -> int:
        """Where a pass currently sits in the table; -1 if it is not shown."""
        try:
            return self._survey_table_index.index(model_index)
        except ValueError:
            return -1

    def _rebuild_survey_table(self) -> None:
        """Repaint the whole table, grouped by what the next batch will do.

        A batch left running overnight is read at a glance from the groups: what
        is still to run, and what is already finished.
        """
        # The rows the breakdown could be anchored to are about to be replaced.
        self._on_queue_row_hover(-1, None)
        self._survey_table_rebuilding = True
        try:
            self._rebuild_survey_table_rows()
        finally:
            self._survey_table_rebuilding = False

    def _rebuild_survey_table_rows(self) -> None:
        table = self._survey_pass_table
        keep = set(self._selected_survey_rows())
        current = self._model_index(table.currentRow())
        table.clearSpans()
        table.setRowCount(0)
        self._survey_table_index = []
        states = self._survey_row_states()
        for state in (QUEUED, DONE, NEXT):
            members = [index for index, value in enumerate(states) if value == state]
            # Only the groups that have something in them: an empty "Held back"
            # heading is a permanent reminder of a feature, not information.
            if not members:
                continue
            self._append_group_heading(state, len(members))
            for index in members:
                self._append_survey_cells(index, state)
        self._refresh_survey_pass_statuses()
        selection = table.selectionModel()
        for model_index in keep:
            row = self._table_row_of(model_index)
            if row >= 0:
                table.selectRow(row)
        if current is not None and self._table_row_of(current) >= 0 and selection is not None:
            table.setCurrentCell(self._table_row_of(current), _COL_VIDEO)

    def _append_group_heading(self, state: str, count: int) -> None:
        table = self._survey_pass_table
        index = table.rowCount()
        table.insertRow(index)
        self._survey_table_index.append(None)
        item = QTableWidgetItem(f"{_GROUP_TITLES[state]}  ({count})")
        item.setToolTip(_GROUP_HINTS[state])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = item.font()
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(font)
        item.setForeground(QColor(TEXT_MUTED if state == QUEUED else TEXT_DIM))
        table.setItem(index, 0, item)
        table.setSpan(index, 0, 1, table.columnCount())

    def _section_cell_text(self, row: _PassRow) -> str:
        """The section on one line: where it was swum, which way, and what of it.

        One button rather than three, because the three are one thing and they
        go to one place. Split across three cells the window was also the one
        that ended up too narrow to read.
        """
        return " · ".join((
            self._transect_cell_text(row.transect_id),
            f"{_mmss(row.begin_s)}-{_mmss(row.end_s)}",
        ))

    def _section_cell(self, row: _PassRow) -> QPushButton:
        """The section, as a cell that opens the section rather than editing it.

        A transect, a direction and a window are facts about the swim, and the
        swim is described under Videos.
        """
        button = QPushButton(self._section_cell_text(row))
        button.setProperty("quiet", "true")
        # The direction as a coloured arrow rather than a word in the middle of
        # the line. Set as an icon, never a stylesheet: _refresh_row_notices owns
        # this button's stylesheet.
        button.setIcon(direction_arrow_icon(row.direction))
        button.setIconSize(QSize(ICON_SM, ICON_SM))
        if row.pass_id is None:
            button.setEnabled(False)
        else:
            button.clicked.connect(partial(self._open_section_in_videos, row.pass_id))
        return button

    def _append_survey_cells(self, model_index: int, state: str) -> None:
        row = self._survey_rows[model_index]
        table = self._survey_pass_table
        index = table.rowCount()
        table.insertRow(index)
        self._survey_table_index.append(model_index)

        # A grip only on the rows that have an order to change. On the others it
        # would offer a move that is refused on the drop.
        handle = QTableWidgetItem()
        if state == QUEUED:
            handle.setIcon(grip_icon())
            handle.setToolTip("Drag to change when this pass is processed.")
        handle.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        table.setItem(index, _COL_HANDLE, handle)

        video_item = QTableWidgetItem(_clip_name(row.videos))
        # Carries the name, and the two facts whose own columns drop on a narrow
        # window, so nothing is only reachable through a column that may be gone.
        video_item.setToolTip(
            "\n".join((
                f"{self._row_label(row)}. Right-click the row to rename it.",
                _clip_tooltip(row.videos),
                f"Recorded {_clip_time(row.video.mtime)}",
                f"Section runs {_span_length(row.end_s - row.begin_s)} of "
                f"{_span_length(row.total_duration_s())}",
            ))
        )
        video_item.setFlags(video_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(index, _COL_VIDEO, video_item)

        recorded_item = QTableWidgetItem(_clip_time(row.video.mtime))
        recorded_item.setFlags(recorded_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        recorded_item.setForeground(QColor(TEXT_MUTED))
        table.setItem(index, _COL_RECORDED, recorded_item)

        # The section, not the clip: this is what decides the pass's runtime and
        # its memory.
        length_item = QTableWidgetItem(_span_length(row.end_s - row.begin_s))
        length_item.setFlags(length_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        length_item.setForeground(QColor(TEXT_MUTED))
        length_item.setToolTip(
            f"Section length. The clip it is cut from runs "
            f"{_span_length(row.total_duration_s())}."
        )
        table.setItem(index, _COL_LENGTH, length_item)

        table.setCellWidget(index, _COL_SECTION, self._section_cell(row))

        settings_btn = QPushButton()
        settings_btn.setProperty("quiet", "true")
        settings_btn.clicked.connect(partial(self._on_survey_row_settings, model_index))
        table.setCellWidget(index, _COL_SETTINGS, settings_btn)
        self._paint_settings_cell(model_index)

        status_item = QTableWidgetItem("")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(index, _COL_STATUS, status_item)

        # An icon rather than a glyph in the label: the font a field laptop
        # falls back to had no ✕ in it, and the button came out blank.
        delete_btn = QPushButton()
        delete_btn.setIcon(close_icon(ICON_SM))
        delete_btn.setProperty("quiet", "true")
        delete_btn.setToolTip(_DELETE_HINT)
        delete_btn.clicked.connect(partial(self._remove_rows, [model_index]))
        table.setCellWidget(index, _COL_ACTION, delete_btn)

    def _append_survey_row(self, row: _PassRow) -> None:
        """Add a pass to the batch and put it in the group it belongs to."""
        self._survey_rows.append(row)
        self._rebuild_survey_table()

    def _refresh_row_widgets(self, index: int) -> None:
        """Repaint one row's cells from the row it belongs to."""
        row = self._survey_rows[index]
        table = self._survey_pass_table
        table_row = self._table_row_of(index)
        if table_row < 0:
            return
        cell = table.cellWidget(table_row, _COL_SECTION)
        if isinstance(cell, QPushButton):
            cell.setText(self._section_cell_text(row))
        self._paint_settings_cell(index)

    # --- Row actions ---

    def _add_video_paths(self, paths: list[str]) -> None:
        """Register clips in the library, decoding them off the GUI thread.

        One card of GoPro clips is dozens of multi-gigabyte files and cv2 has to
        open every one to learn its length, so probing on the GUI thread froze
        the window for as long as the card took to read. Importing only records
        the clips: a pass is cut from a clip in Browse, never minted here.
        """
        if not paths:
            return
        self._status_label.setText(f"Reading {len(paths)} video(s)…")

        def worker() -> None:
            probed = [(path, _probe_video(path)) for path in paths]
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_videos_probed.emit(probed)

        self._video_probe_thread = threading.Thread(target=worker, daemon=True)
        self._video_probe_thread.start()

    def _on_videos_probed(self, probed: list) -> None:
        """Record every readable clip in the library, then report the import once."""
        readable = [(path, result) for path, result in probed if result is not None]
        store = self._survey_store()
        imported: set[uuid.UUID] = set()
        relinked = 0
        for path, (duration_s, fps) in readable:
            asset = VideoAsset.from_path(Path(path))
            asset.duration_s = duration_s
            asset.fps = fps
            # The library is keyed by content hash, so the same recording picked
            # from two folders lands once -- and a known clip added from a new
            # place is the same clip moved, so upsert repoints its path. Sections
            # and runs reference the clip by id and notice nothing.
            prior = store.find_video_by_hash(asset.hash)
            stored = store.upsert_video(asset)
            if prior is not None and prior.path != asset.path:
                relinked += 1
            else:
                imported.add(stored.id)
        parts = []
        if imported:
            parts.append(
                f"Imported {len(imported)} clip{'' if len(imported) == 1 else 's'}. "
                "Cut sections from them in Browse to process them."
            )
        if relinked:
            parts.append(
                f"Relinked {relinked} known clip{'' if relinked == 1 else 's'} "
                "to where they live now."
            )
        if imported or relinked:
            self._refresh_data_manager()
        skipped = len(probed) - len(readable)
        if skipped:
            parts.append(f"Skipped {skipped} unreadable video(s).")
        if parts:
            self._status_label.setText(" ".join(parts))

    # --- The processing order ---

    def _on_survey_sort_by_time(self) -> None:
        """Put the rows in the order the day happened, which a drag departs from."""
        ordered = sorted(self._survey_rows, key=_clip_sort_key)
        if ordered == self._survey_rows:
            return
        self._survey_rows = ordered
        self._persist_row_order()
        self._rebuild_survey_table()
        self._recompute_survey_start()

    def _on_survey_rows_moved(self, source_row: int, target_row: int) -> None:
        """Take a row out of the processing order and put it back somewhere else.

        Only within "To process". A finished pass has no order left to change,
        and a row of the next session belongs to a cart this order is not.
        """
        source = self._model_index(source_row)
        target = self._model_index(target_row)
        if source is None or target is None or source == target:
            return
        states = self._survey_row_states()
        if states[source] != QUEUED or states[target] != QUEUED:
            self._status_label.setText("Only the passes still to process can be reordered.")
            return
        self._survey_rows.insert(target, self._survey_rows.pop(source))
        self._persist_row_order()
        self._rebuild_survey_table()
        self._recompute_survey_start()
        self._status_label.setText("Changed the processing order.")

    def _persist_row_order(self) -> None:
        """Write the table's order onto the cart rows behind it.

        Both sessions in view are written separately: while an order runs the
        table shows it above the next cart, and each owns the order of its own
        rows.
        """
        store = self._try_survey_store()
        if store is None:
            return
        for batch, rows in (
            (self._shown_batch(), [r for r in self._survey_rows if not r.in_cart]),
            (self._next_cart_batch(), [r for r in self._survey_rows if r.in_cart]),
        ):
            pass_ids = [row.pass_id for row in rows if row.pass_id is not None]
            if batch is not None and pass_ids:
                store.set_batch_item_positions(batch.id, pass_ids)

    def _shown_batch(self) -> SurveyBatch | None:
        """The session the table's main body belongs to."""
        if self._survey_worker_running and self._survey_running_batch is not None:
            return self._survey_running_batch
        return self._survey_batch

    def _next_cart_batch(self) -> SurveyBatch | None:
        """The cart filled while an order runs, or None when nothing is running."""
        store = self._try_survey_store()
        if store is None or not self._survey_worker_running:
            return None
        return store.current_cart()

    def _batch_of_row(self, row: _PassRow) -> SurveyBatch | None:
        """Which session's cart this row is a member of."""
        return self._next_cart_batch() if row.in_cart else self._shown_batch()

    # --- Taking a pass out, and putting a finished one back ---

    def _on_survey_remove_pass(self) -> None:
        """Take every selected pass out of the session."""
        self._remove_rows(self._selected_survey_rows())

    def _remove_rows(self, indices: list[int]) -> None:
        """Un-cart these rows, and nothing more.

        The passes, their runs and their videos all stay in the store: this is
        Clear cart, one row at a time. It works mid-run too, because the worker
        re-reads the cart before each pass, so a row taken out before its pass
        starts is one the session no longer processes.
        """
        store = self._try_survey_store()
        if store is None or not indices:
            return
        removed = 0
        for index in indices:
            if not 0 <= index < len(self._survey_rows):
                continue
            row = self._survey_rows[index]
            batch = self._batch_of_row(row)
            if batch is None or row.pass_id is None:
                continue
            store.remove_batch_item(batch.id, row.pass_id)
            removed += 1
        if not removed:
            return
        self._refresh_survey_batch_tab()
        self._status_label.setText(f"Took {passes_phrase(removed)} out of the session.")

    def _process_rows_again(self, indices: list[int]) -> None:
        """Order finished passes again, as part of the next session."""
        carted = 0
        for index in indices:
            if not 0 <= index < len(self._survey_rows):
                continue
            pass_id = self._survey_rows[index].pass_id
            if pass_id is not None:
                self._cart_add(pass_id)
                carted += 1
        if not carted:
            return
        self._refresh_survey_batch_tab()
        self._status_label.setText(f"Added {passes_phrase(carted)} to the cart.")

    # --- Settings for one pass ---

    def _session_settings(self) -> dict:
        """What the session runs on: the form, as a preset dict.

        Read from the form rather than the saved preset for the same reason the
        summary label is: the batch runs from the form, so a row's overrides
        have to be measured against the form too.
        """
        return self._collect_preset_from_form()

    def _row_settings(self, row: _PassRow) -> dict:
        """What this one pass will run on."""
        return effective(self._session_settings(), row.overrides)

    def _row_fit(self, row: _PassRow, profile=None):
        """Grade what this row will actually run against this machine, or None.

        Per row rather than per session: a row with a frame rate or a resolution
        of its own is exactly the row whose grade differs from the rest, and the
        warning belongs on the settings that caused it.
        """
        from deepreefmap_gui.profiling.memory_estimate import fit_for_pass
        from deepreefmap_gui.profiling.run_history import history_key, load_expected_peaks
        from deepreefmap_gui.profiling.system_probe import probe_system

        seconds = row.end_s - row.begin_s
        if seconds <= 0:
            return None
        settings = self._row_settings(row)
        fps = int(settings.get("fps") or self._fps_spin.value())
        # A preset carries no processing size unless the resolution is Custom,
        # so the form's own spins are what a native-size row will run at.
        width = int(settings.get("processing_width") or self._proc_width_spin.value())
        height = int(settings.get("processing_height") or self._proc_height_spin.value())
        mapping = str(settings.get("mapping_name") or self._map_combo.currentText())
        seg = str(settings.get("segmentation_name") or self._seg_combo.currentText())
        batch_size = int(
            settings.get("preprocess_batch_size") or self._batch_size_spin.value()
        )
        try:
            machine = probe_system(wait_for_gpu=False) if profile is None else profile
            return fit_for_pass(
                machine,
                seconds=seconds,
                fps=fps,
                width=width,
                height=height,
                mapping_backend=mapping,
                seg_model=seg,
                batch_size=batch_size,
                recorded=load_expected_peaks(
                    history_key(mapping, seg, width, height, fps),
                    gpu_name=machine.gpu.name,
                    batch_size=batch_size,
                ),
            )
        except Exception:
            # The grade is advice. A probe that cannot answer must not stop the
            # table from painting.
            return None

    def _row_for_pass(self, pass_id) -> _PassRow | None:
        return next((row for row in self._survey_rows if row.pass_id == pass_id), None)

    def _row_label(self, row: _PassRow) -> str:
        """What this section is called: its own name, or the generated default.

        The default is produced on read, so an unnamed row follows the current
        generator rather than carrying an older one.
        """
        from deepreefmap_gui.survey.labels import pass_label

        stored = (getattr(row, "label", "") or "").strip()
        if stored:
            return stored
        subject = row.transect_id
        peers = [r for r in self._survey_rows if r.transect_id == subject] if subject else [
            r for r in self._survey_rows if r.transect_id is None and r.video.id == row.video.id
        ]
        number = next((i for i, r in enumerate(peers, start=1) if r is row), 1)
        return pass_label(
            row,
            transect_name=self._transect_name_for(subject) if subject else None,
            clip_name=row.video.file_name,
            number=number,
        )

    def _on_survey_rename(self, index: int) -> None:
        """Rename a section from the row's menu.

        Empty means the derived name back, and a name another section already has
        is refused with the one it got instead.
        """
        from PySide6.QtWidgets import QInputDialog

        from deepreefmap_gui.survey.labels import taken_labels, unique_label

        row = self._survey_rows[index]
        store = self._try_survey_store()
        if store is None or row.pass_id is None:
            return
        pass_ = store.get_pass(row.pass_id)
        if pass_ is None:
            return
        typed, accepted = QInputDialog.getText(
            self,
            "Rename section",
            "What this section is called. Clear it for the derived name.",
            text=self._row_label(row),
        )
        if not accepted:
            return
        wanted = typed.strip()
        if not wanted:
            # An emptied field is a request for the default back, not a request
            # for a nameless section.
            pass_.label = ""
        else:
            pass_.label = unique_label(
                wanted, taken_labels(store.list_passes(), exclude=row.pass_id)
            )
            if pass_.label != wanted:
                self._status_label.setText(
                    f"Another section is already called {wanted!r}; "
                    f"this one is {pass_.label!r}."
                )
        store.update_pass(pass_)
        row.label = pass_.label
        self._rebuild_survey_table()

    def _pass_spec(self, row: _PassRow):
        """What this row will run, in the terms its runtime depends on.

        Reads the settings exactly as _row_fit does, so the time estimate and the
        memory grade are answering about the same run.
        """
        from deepreefmap_gui.profiling.batch_estimate import PassSpec

        seconds = row.end_s - row.begin_s
        settings = self._row_settings(row)
        fps = int(settings.get("fps") or self._fps_spin.value())
        # Zero frames rather than dropped: BatchEtaTracker indexes by job
        # position, so a dropped spec shifts every later pass out of step.
        return PassSpec(
            key=str(row.pass_id),
            frames=int(max(0.0, seconds) * max(1, fps)),
            mapping_backend=str(settings.get("mapping_name") or self._map_combo.currentText()),
            seg_model=str(settings.get("segmentation_name") or self._seg_combo.currentText()),
            width=int(settings.get("processing_width") or self._proc_width_spin.value()),
            height=int(settings.get("processing_height") or self._proc_height_spin.value()),
            fps=max(1, fps),
        )

    def _survey_pass_specs(self, rows: list[_PassRow | None] | None = None) -> list:
        """One spec per row, in row order, including the ones that cannot be costed.

        A row with no pass or no window becomes a zero-frame spec, which
        predict_batch reports as unknown.
        """
        from deepreefmap_gui.profiling.batch_estimate import PassSpec

        if rows is None:
            rows = list(self._survey_remaining_rows())
        specs = []
        for index, row in enumerate(rows):
            spec = self._pass_spec(row) if row is not None else None
            specs.append(
                spec
                if spec is not None
                else PassSpec(
                    key=f"unknown-{index}",
                    frames=0,
                    mapping_backend="",
                    seg_model="",
                    width=0,
                    height=0,
                    fps=1,
                )
            )
        return specs

    def _survey_batch_prediction(self, rows: list[_PassRow | None] | None = None):
        """What the queued passes are expected to cost, cached on their shape.

        Every row mutation funnels through _recompute_survey_start, and this reads
        a JSON profile off disk, so the cache is load-bearing rather than an
        optimisation.
        """
        from deepreefmap_gui.profiling.batch_estimate import predict_batch

        specs = self._survey_pass_specs(rows)
        signature = tuple(
            (s.key, s.frames, s.mapping_backend, s.seg_model, s.width, s.height, s.fps)
            for s in specs
        )
        cached = getattr(self, "_batch_prediction_cache", None)
        if cached is not None and cached[0] == signature:
            return cached[1]
        prediction = predict_batch(specs)
        self._batch_prediction_cache = (signature, prediction)
        return prediction

    def _refresh_settings_cells(self) -> None:
        """Repaint every row's settings button, probing the machine once."""
        from deepreefmap_gui.profiling.system_probe import probe_system

        if not self._survey_rows:
            return
        try:
            profile = probe_system(wait_for_gpu=False)
        except Exception:
            profile = None
        for index in range(len(self._survey_rows)):
            self._paint_settings_cell(index, profile)

    def _paint_settings_cell(self, index: int, profile=None) -> None:
        """Say what this row changes, and warn when what it will run does not fit."""
        table_row = self._table_row_of(index)
        button = (
            self._survey_pass_table.cellWidget(table_row, _COL_SETTINGS)
            if table_row >= 0
            else None
        )
        if not isinstance(button, QPushButton):
            return
        row = self._survey_rows[index]
        overrides = live_overrides(row.overrides, self._session_settings())
        button.setText(override_summary(overrides))
        tooltip = override_tooltip(overrides)
        # The memory verdict rides here rather than in a column of its own: the
        # settings are what would fix it, so the warning is on the control that
        # opens them.
        fit = self._row_fit(row, profile)
        if fit is not None and not fit.fits:
            tooltip = f"{fit.headline}. {fit.detail} {fit.advice}\n{tooltip}"
        button.setToolTip(tooltip)
        # Outlined rather than filled: the grade is advice, the run is not
        # blocked by it, and a filled amber on every row of a long session
        # reads as a table of errors.
        _style_warning_cell(button, ok=fit is None or fit.fits, filled=False)
        self._widen_settings_column(button)

    def _widen_settings_column(self, button: QPushButton) -> None:
        """Keep the column at least as wide as the button now needs.

        A label centred in a button that is too narrow is clipped at both ends.
        The width cannot be a constant: it follows the machine's UI font, and the
        amber variant brings its own padding with it. Routed through the sizer
        rather than set directly, or a repaint would read as a dragged column.
        """
        self._pass_column_sizer.widen_fixed(
            _COL_SETTINGS, button.sizeHint().width() + SPACE_SM
        )

    def _rows_over_memory(self) -> int:
        """How many queued passes this machine cannot give what they ask for."""
        from deepreefmap_gui.profiling.system_probe import probe_system

        try:
            profile = probe_system(wait_for_gpu=False)
        except Exception:
            return 0
        return sum(
            1
            for row in self._survey_remaining_rows()
            if (fit := self._row_fit(row, profile)) is not None and not fit.fits
        )

    def _on_survey_row_settings(self, index: int) -> None:
        """Edit the run settings for one pass."""
        self._edit_row_settings([index], "Settings for this pass")

    def _on_survey_bulk_settings(self) -> None:
        """Edit the run settings for every selected pass at once."""
        indices = self._selected_survey_rows()
        if not indices:
            self._status_label.setText("Select the rows you want to change first.")
            return
        self._edit_row_settings(indices, f"Settings for {passes_phrase(len(indices))}")

    def _edit_row_settings(self, indices: list[int], title: str) -> None:
        """Edit some rows' settings, in the dialog the session's settings use.

        The dialog edits the live run form, so the session's own values are
        snapshotted and put back whatever happens here. What comes out is the
        difference, which is what the cart rows carry: change the session later
        and every setting these passes did not override follows it.
        """
        from deepreefmap_gui.simple.settings_dialog import RunSettingsDialog

        if self._survey_worker_running or not indices:
            return
        session = self._session_settings()
        first = self._survey_rows[indices[0]]
        before = self._snapshot_form_settings()
        self._populate_form_from_preset(effective(session, first.overrides))
        dialog = RunSettingsDialog(
            self,
            self._setup_page,
            [self._output_group],
            title=title,
            reset_label="Use the session's settings",
            on_reset=partial(self._populate_form_from_preset, session),
        )
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.restore_form()
        overrides = (
            override_diff(self._collect_preset_from_form(), session) if accepted else None
        )
        # Always: the session's settings are not what was being edited here.
        self._restore_form_settings(before)
        if overrides is not None:
            self._write_overrides(indices, overrides)

    def _write_overrides(self, indices: list[int], overrides: dict) -> None:
        """Give these rows the settings they depart from the session on."""
        store = self._try_survey_store()
        if store is None:
            return
        written = 0
        for index in indices:
            if not 0 <= index < len(self._survey_rows):
                continue
            row = self._survey_rows[index]
            batch = self._batch_of_row(row)
            if batch is None or row.pass_id is None:
                continue
            row.overrides = dict(overrides)
            store.set_batch_item_overrides(batch.id, row.pass_id, row.overrides)
            self._refresh_row_widgets(index)
            written += 1
        if not written:
            return
        self._recompute_survey_start()
        self._status_label.setText(
            f"{describe_keys(overrides)} changed for {passes_phrase(written)}."
            if overrides
            else f"{passes_phrase(written)} back on the session's settings."
        )

    def _on_survey_copy_settings(self) -> None:
        """Give the selected rows settings that already exist somewhere.

        Typing the same three changes into six rows is how a session ends up
        with five of them right, so the sources are offered as a list: another
        pass in this cart, or what a run under this output root actually used.
        """
        indices = self._selected_survey_rows()
        if not indices:
            self._status_label.setText("Select the rows you want to change first.")
            return
        menu = QMenu(self)
        menu.addAction(
            "The session's settings", partial(self._write_overrides, indices, {})
        )
        session = self._session_settings()
        # A source already in the selection is offered like any other: giving
        # its settings to the rest is the common move, and it keeps its own.
        # One entry per set of settings, however many rows carry it.
        offered: list[dict] = []
        for row in self._survey_rows:
            overrides = live_overrides(row.overrides, session)
            if not overrides or overrides in offered:
                continue
            offered.append(overrides)
            menu.addAction(
                f"{_clip_name(row.videos)}: {describe_keys(overrides)}",
                partial(self._write_overrides, indices, dict(row.overrides)),
            )
        for label, deviations in self._recent_run_settings():
            menu.addAction(label, partial(self._write_overrides, indices, deviations))
        button = self._survey_copy_settings_btn
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _recent_run_settings(self) -> list[tuple[str, dict]]:
        """What recent runs changed about the standard settings, newest first.

        Read from the manifests, which is the only record of what a run actually
        used. Identical sets are offered once, and only a handful, because this
        is a menu rather than the settings history the audit dialog shows.
        """
        from deepreefmap_gui.survey.config_audit import audit_out_root

        if self._active_preset is None:
            return []
        try:
            rows = audit_out_root(
                Path(self._out_root_input.text()).expanduser(), self._active_preset.org
            )
        except OSError:
            return []
        offered: list[tuple[str, dict]] = []
        seen: list[dict] = []
        for row in rows:
            if not row.deviations or row.deviations in seen:
                continue
            seen.append(row.deviations)
            offered.append(
                (f"{row.display_name}: {row.changed_summary}", dict(row.deviations))
            )
            if len(offered) == 5:
                break
        return offered

    def _single_direction_transects(self) -> dict[uuid.UUID, str]:
        """Transects every pass of which runs the same way, and why that is odd.

        Repeat passes are normally swum out and back, and nothing downstream can
        tell a deliberate one-way survey from a row of directions nobody set.
        """
        directions: dict[uuid.UUID, list[str]] = {}
        for row in self._survey_rows:
            if row.transect_id is not None:
                directions.setdefault(row.transect_id, []).append(row.direction)
        names = {transect.id: transect.name for transect in self._survey_transects}
        flagged = {}
        for transect_id, values in directions.items():
            if len(values) < 2 or len(set(values)) != 1:
                continue
            name = names.get(transect_id, "Unnamed transect")
            flagged[transect_id] = (
                f"All {len(values)} passes of {name} are set to {values[0]}. "
                "Repeat passes are usually swum out and back."
            )
        return flagged

    def _missing_clip_paths(self) -> set[str]:
        """Clips the library has looked for and could not find.

        Read off the library's cached link states rather than by stat'ing here:
        this runs on every repaint, and a drive that has gone to sleep must not
        be woken on the thread that paints the window. A path nobody has asked
        about yet counts as present, since "not checked" is not evidence of
        absence.
        """
        return {
            clip.video.path
            for clip in getattr(self, "_video_entries", [])
            if clip.link_state == LINK_MISSING
        }

    def _refresh_row_notices(self) -> None:
        """Say what is worth a second look about each section, on its own cell.

        Three marks, in one place because they compete for the same cell. Red
        and dashed for footage that is not there, which cannot run at all; amber
        and outlined for a pass filed against no transect, which runs but
        unscaled; and nothing at all for a transect swum one way only, which is
        worth reading in the tooltip but not worth turning every row of a
        genuinely one-way survey amber.
        """
        one_way = self._single_direction_transects()
        missing_paths = self._missing_clip_paths()
        for index, row in enumerate(self._survey_rows):
            table_row = self._table_row_of(index)
            if table_row < 0:
                continue
            cell = self._survey_pass_table.cellWidget(table_row, _COL_SECTION)
            if not isinstance(cell, QPushButton):
                continue
            missing = [v.file_name for v in row.videos if v.path in missing_paths]
            # Names the direction the arrow icon shows, so it reaches a reader who
            # sees neither the glyph nor its colour.
            notes = [
                f"Swum {row.direction} along the transect, "
                "and what part of the clip that was."
            ]
            if missing:
                _style_missing_cell(cell)
                notes.append(
                    f"{', '.join(missing)} cannot be found, so this pass cannot "
                    "run. Plug the drive back in, or add the footage again from "
                    "where it lives now."
                )
            else:
                _style_warning_cell(cell, ok=row.transect_id is not None, filled=False)
                if row.transect_id is None:
                    notes.append(
                        "Filed against no transect, so it runs unscaled and is "
                        "left out of the repeatability comparison."
                    )
            if row.transect_id in one_way:
                notes.append(one_way[row.transect_id])
            notes.append("Click to open this section under Videos.")
            cell.setToolTip("\n".join(notes))

    # --- Run gating and execution ---

    def _survey_missing_models(self) -> list[str]:
        """Required-but-uncached models, judged against what the run will load.

        _required_model_names() reads the run form (mapping, segmentation, the
        DPT backbone), the same widgets _collect_run_settings() reads and the
        batch runs from, so the gate cannot block on a model the run would not
        load nor pass one it would. Iterating all_known_models() rather than the
        hardcoded catalogue means a model discovered this session is gated too.

        Answered from what _refresh_model_status verified on its worker thread,
        never by verifying here: this runs from the readiness rows and the cart
        gate, several times per repaint and while the window is still being
        built, and verifying walks every model's snapshot. Until the first
        refresh lands nothing is reported missing -- the same choice the
        graphics row makes, for the same reason: a model merely not yet checked
        must not read as a model absent. _apply_model_status repaints both.
        """
        if self._survey_preset is None:
            return []
        required = self._required_model_names()
        return sorted(
            info.name for info, cached in self._last_model_states
            if info.name in required and not cached
        )

    def _simple_peak_seconds(self) -> float | None:
        """Length of the longest pass still to run, for the memory grade.

        A batch is many passes, but they run one at a time, so the pass that
        peaks memory is simply the longest. Passes already done are excluded:
        they set no peak the next batch has to survive. None when nothing is
        queued. The per-row grade on each settings button is the finer answer;
        this is what the capacity readout shows for the session as a whole.
        """
        rows = getattr(self, "_survey_rows", [])
        try:
            rows = self._survey_remaining_rows() or rows
        except Exception:
            # Row states need a store; without one, grade every row rather than
            # losing the grade.
            pass
        spans = [row.end_s - row.begin_s for row in rows if row.end_s > row.begin_s]
        return max(spans) if spans else None

    def _simple_peak_frames(self, fps: int) -> int | None:
        seconds = self._simple_peak_seconds()
        if seconds is None:
            return None
        return int(seconds * max(1, fps)) or None

    def _survey_remaining_rows(self) -> list[_PassRow]:
        """The passes the next batch would actually process, in table order."""
        states = self._survey_row_states()
        return [
            row
            for row, state in zip(self._survey_rows, states, strict=True)
            if state == QUEUED and row.pass_id is not None
        ]

    def _survey_failed_count(self) -> int:
        """Passes whose most recent run failed and has not since succeeded."""
        # _recompute_survey_start must repaint even when the store cannot open.
        store = self._try_survey_store()
        if store is None:
            return 0
        failed = 0
        for row in self._survey_rows:
            if row.pass_id is None:
                continue
            runs = store.runs_for_pass(row.pass_id)
            if runs and not any(run.status == "succeeded" for run in runs):
                failed += any(run.status == "failed" for run in runs)
        return failed

    def _rows_without_footage(self) -> int:
        """Queued passes naming a video the Videos page could not find.

        The gate's count of what the rows mark individually.
        """
        missing = self._missing_clip_paths()
        if not missing:
            return 0
        return sum(
            1
            for row in self._survey_rows
            if any(video.path in missing for video in row.videos)
        )

    def _recompute_survey_start(self) -> None:
        """The Run step's one verdict, applied through a single exit.

        The badge, the count in the header and the forward button all read this,
        so they cannot disagree. Every row mutation funnels through here, which
        is why it must not return early before the repaint.
        """
        # Row actions and the empty state follow the table from one place.
        self._recompute_row_actions()
        self._refresh_row_notices()
        # The per-row memory grade, which the session's settings change: a lower
        # frame rate here can put every warned row back inside the machine.
        self._refresh_settings_cells()
        # And what the queue as it now stands would cost. Answered before Start
        # rather than after it: how long the evening is decides whether to run
        # the batch at all, or to trim it first.
        if not self._survey_worker_running:
            self._batch_progress.set_batch_plan(self._survey_batch_prediction())
            self._batch_progress.set_idle("No batch in progress.")
        # Keep the summary label on the same source as the gate and the run: all
        # three derive from the form, so the label cannot claim settings the run
        # would not use.
        self._survey_preset_label.setText(self._survey_preset_summary())
        # Restoring is only an action when there is something to restore, and the
        # tooltip says which of the two situations you are in.
        deviated = bool(self._survey_deviations())
        self._survey_restore_btn.setEnabled(deviated and not self._survey_worker_running)
        self._survey_restore_btn.setToolTip(
            "Discard this machine's changes and return to the standard settings."
            if deviated
            else "Already on the standard settings."
        )
        self._refresh_batch_standing()
        # Before the running early-return, or the badge freezes for the batch.
        self._update_cart_button()
        if self._survey_worker_running:
            self._survey_start_btn.setEnabled(False)
            self._refresh_section_state()
            return

        unassigned = sum(1 for row in self._survey_rows if row.transect_id is None)
        # Assigned to a transect that has no tape length: runs, but unscaled.
        lengths = {t.id: t.length_m for t in self._survey_transects}
        unscaled = sum(
            1
            for row in self._survey_rows
            if row.transect_id is not None and lengths.get(row.transect_id) is None
        )
        remaining = self._survey_remaining_rows() if self._survey_rows else []
        missing = self._survey_missing_models() if self._survey_preset is not None else []
        gate = run_gate(
            pass_count=len(self._survey_rows),
            missing_files=self._rows_without_footage(),
            unassigned=unassigned,
            remaining=len(remaining),
            failed=self._survey_failed_count() if self._survey_rows else 0,
            has_preset=self._survey_preset is not None,
            missing_models=missing,
            gpu_only_mapper=self._gpu_only_mapper(),
            unscaled=unscaled,
        )
        self._survey_gate = gate
        self._paint_not_ready_strip(gate)

        self._set_survey_start_text(len(remaining))
        # Only once there is something to look at: on an empty session this
        # would greet a diver who had just arrived with a route to results, as
        # though the day's work were already done.
        self._survey_results_btn.setVisible(bool(self._survey_processed_count()))
        if gate.state == BLOCKED:
            self._survey_start_btn.setEnabled(False)
            self._status_label.setText(gate.reason)
        elif gate.state == ATTENTION and gate.reason:
            # A warning still leaves the session runnable, so the button carries
            # no sign of it. Without this the failed-pass warning lives only in
            # the nav badge and the tooltip.
            self._status_label.setText(gate.reason)
            self._survey_start_btn.setEnabled(bool(remaining) or bool(self._survey_rows))
        else:
            # Nothing left to process is not an error, so the button stays put
            # and simply has nothing to do.
            self._survey_start_btn.setEnabled(bool(remaining))
        self._survey_start_btn.setToolTip(gate.reason)
        self._refresh_section_state()
        # The queued passes drive both the memory grade and the setup step's
        # models row, so re-read them whenever the batch changes.
        self._update_memory_profile_warning()
        self._refresh_readiness_view()

    def _paint_not_ready_strip(self, gate: SectionState) -> None:
        """Show the blocker only when getting to it needs leaving this page.

        A missing model or a missing graphics card is not fixable from here at
        all, which is what the strip is for.
        """
        if gate.state != BLOCKED or gate.fix == FIX_HERE:
            self._survey_not_ready.clear()
            return
        self._survey_not_ready.show_blocker(gate.reason, _FIX_ACTIONS[gate.fix])

    def _on_survey_fix_blocker(self) -> None:
        """Go where the live gate says the blocker is fixed."""
        gate = getattr(self, "_survey_gate", None)
        if gate is None:
            return
        if gate.fix == FIX_MACHINE:
            self._set_simple_section("machine")
            # The view is sticky, so naming the destination is not enough: every
            # blocker that routes here is explained on the readiness rows, and
            # each of those rows carries the button that fixes it.
            self._set_machine_view("readiness")
        elif gate.fix == FIX_SETTINGS:
            self._on_edit_run_settings()

    def _set_survey_start_text(self, count: int) -> None:
        """Name what the button will do, and to how much.

        A session that has already run some of its passes is continued, not
        started: the word is the promise that the finished ones stay done and the
        half-processed one picks up from its cached frames. The count is what
        turns a vague commitment into a decision, so it is in the label rather
        than only in the table above.

        A disabled button still has to be readable. With every pass behind you
        the button has no action left, and saying so beats offering to start
        something there is none of; See the results beside it is the move.
        """
        if not count and self._survey_rows:
            self._survey_start_btn.setText("Nothing left to process")
            return
        verb = "Continue processing" if self._survey_processed_count() else "Start processing"
        # No count on an empty session: "(0 passes)" reads as a quantity when
        # what it means is that nothing has been queued yet.
        self._survey_start_btn.setText(
            f"{verb} ({passes_phrase(count)})" if count else verb
        )

    def _survey_processed_count(self) -> int:
        """Passes in this batch that have already been through a run of any outcome."""
        store = self._try_survey_store()
        if store is None:
            return 0
        return sum(
            1
            for row in self._survey_rows
            if row.pass_id is not None and store.runs_for_pass(row.pass_id)
        )

    def _refresh_batch_standing(self) -> None:
        """Say how much of the batch is behind you, so stopping is safe to do.

        Without this a resumed batch looks identical to a fresh one: the button
        counts what is left but nothing counts what is done.
        """
        states = self._survey_row_states()
        done = sum(1 for state in states if state == DONE)
        remaining = len(self._survey_remaining_rows())
        if not states or not done:
            self._survey_standing_label.setVisible(False)
            return
        text = f"{done} done · {remaining} remaining"
        if done and remaining:
            text += ". Processing again continues where it stopped."
        self._survey_standing_label.setText(text)
        self._survey_standing_label.setVisible(True)

    def _confirm_batch_space(self, pass_count: int) -> bool:
        """Pre-flight the disk before a batch: only interrupt when it may not fit.

        The estimate is deliberately rough (see setup.ROUGH_PASS_BYTES), so a
        comfortable margin proceeds silently and a tight one asks first, rather
        than nagging on every run.
        """
        import shutil

        from deepreefmap_gui.simple.setup import estimate_batch_disk

        out_root = Path(self._out_root_input.text()).expanduser()
        try:
            out_root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(out_root).free
        except OSError:
            return True  # can't reach the drive, so don't stand in the way
        estimate = estimate_batch_disk(pass_count, free)
        if estimate.fits:
            return True

        from deepreefmap_gui.profiling.system_probe import format_bytes

        time_str = _rough_batch_time(self._survey_batch_prediction().total_s)
        opening = f"{pass_count} pass{'' if pass_count == 1 else 'es'} queued"
        opening += f", {time_str}." if time_str else "."
        return confirm(
            self,
            "Insufficient disk space",
            f"{opening}\n\n"
            f"Estimated {format_bytes(estimate.need_bytes)} required against "
            f"{format_bytes(estimate.free_bytes)} free. Processing may stop part "
            "way and leave passes unfinished.\n\nStart anyway?",
        )

    def _pass_dir_name(
        self, pass_: TransectPass, transect: Transect | None, store: SurveyStore
    ) -> str:
        """A directory of its own for every attempt at a pass.

        The first attempt is named ``{stem}__pNN__{passid8}``. A later attempt
        derives from the first run's recorded name and appends ``__rNN``, so a
        renamed transect or a deleted sibling cannot move a pass that already
        ran. Attempts never share a directory: repeats are the reproducibility
        data, and each keeps its own log and manifest. Resume speed relies on
        seed_run_dir_from_match scanning every sibling, earlier attempts
        included, and hard-linking matching frames.

        A pass with no transect is named after its clip instead, which is the
        only thing it has to be recognised by in a folder listing.
        """
        prior = store.runs_for_pass(pass_.id)
        if prior:
            base = re.sub(r"__r\d+$", "", prior[0].run_dir_name)
            out_root = Path(self._out_root_input.text()).expanduser()
            attempt = len(prior) + 1
            while True:
                name = f"{base}__r{attempt:02d}"
                taken = store.run_by_dir_name(name)
                if taken is None and not (out_root / name).exists():
                    return name
                attempt += 1
        if transect is not None:
            siblings = store.list_passes(transect_id=pass_.transect_id)
            stem = transect.name
        else:
            siblings = [p for p in store.list_passes() if p.transect_id is None]
            video = store.get_video(pass_.video_id)
            stem = Path(video.file_name).stem if video is not None else "unassigned"
        number = next(
            (index for index, sibling in enumerate(siblings, start=1) if sibling.id == pass_.id),
            1,
        )
        return self._sanitize_run_name(f"{stem}__p{number:02d}__{pass_.id.hex[:8]}")

    def _on_survey_start(self) -> None:
        if self._survey_preset is None or self._survey_worker_running:
            return
        remaining = self._survey_remaining_rows()
        # Confirm before any run records are written, so a Cancel leaves the
        # batch exactly as it was.
        if not remaining or not self._confirm_batch_space(len(remaining)):
            return
        store = self._survey_store()
        # The cart, or an order being continued after an interruption.
        batch = self._survey_batch if self._survey_batch is not None else self._ensure_cart_batch()
        # Each job's settings are read off the form with that row's overrides
        # applied, so the form is borrowed here and handed back below.
        form_before = self._snapshot_form_settings()
        session_settings = self._session_settings()
        jobs = []
        for row in remaining:
            assert row.pass_id is not None
            pass_ = store.get_pass(row.pass_id)
            if pass_ is None:
                continue
            transect = (
                store.get_transect(pass_.transect_id)
                if pass_.transect_id is not None
                else None
            )
            dir_name = self._pass_dir_name(pass_, transect, store)
            run = RunRecord(pass_id=pass_.id, run_dir_name=dir_name, batch_id=batch.id)
            store.add_run(run)
            settings, config = self._checkout_settings(row, session_settings)
            jobs.append(_SurveyJob(
                run=run,
                pass_=pass_,
                transect=transect,
                videos=list(row.videos),
                dir_name=dir_name,
                label=self._row_label(row),
                settings=settings,
                config=config,
            ))
        # Whatever each row's settings were read through, the form goes back to
        # the session's own values before anything else looks at it.
        self._restore_form_settings(form_before)
        if not jobs:
            return
        self._survey_job_pass_ids = [job.pass_.id for job in jobs]
        self._survey_running_index = None
        # Held apart from _survey_batch, which a cart minted mid-run takes over.
        self._survey_running_batch = batch
        # Predicted in job order, so the tracker's index matches the worker's.
        self._batch_progress.set_batch_plan(
            self._survey_batch_prediction([self._row_for_pass(job.pass_.id) for job in jobs])
        )
        self._batch_progress.set_idle("Starting…")
        self._survey_worker_running = True
        # Share the window's cancel and pause events so the bottom-bar transport
        # controls drive a survey batch exactly as they drive a single run.
        self._survey_cancel_event = threading.Event()
        self._cancel_event = self._survey_cancel_event
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._survey_start_btn.setEnabled(False)
        self._begin_run_controls()
        self._recompute_row_actions()
        self._refresh_survey_pass_statuses()
        self._set_app_mode("RUNNING")
        out_root = Path(self._out_root_input.text()).expanduser()
        self._pipeline_thread = threading.Thread(
            target=self._run_survey_worker,
            args=(jobs, out_root, store, batch, self._pause_event),
            daemon=True,
        )
        self._pipeline_thread.start()

    def _checkout_settings(self, row: _PassRow, session: dict) -> tuple[dict, dict | None]:
        """The run kwargs and the configuration identity for one pass.

        Read from the form on the GUI thread, with this row's overrides applied:
        a survey run honours every setting, not only the ones the preset
        carries, and the manifest has to record what the pass actually ran on
        rather than what the session as a whole was set to.

        The session's own settings are passed in rather than read here. Reading
        them off the form would read the row before, and every pass after the
        first one to override a setting would inherit it.
        """
        self._populate_form_from_preset(effective(session, row.overrides))
        settings = self._collect_run_settings()
        config = (
            manifest_config_block(self._active_preset.org, self._survey_deviations())
            if self._active_preset is not None
            else None
        )
        return settings, config

    def _run_survey_worker(
        self,
        jobs: list[_SurveyJob],
        out_root: Path,
        store: SurveyStore,
        batch: SurveyBatch,
        pause_event: threading.Event,
    ) -> None:
        import shutil

        from deepreefmap_gui.models.cache import resolve_model_versions
        from deepreefmap_gui.profiling.instrumentation import instrumented_reconstruction
        from deepreefmap_gui.runs.seeding import seed_from_settings
        from deepreefmap_gui.simple.setup import ROUGH_PASS_BYTES
        from deepreefmap_gui.system.log_view import close_run_log_file, open_run_log_file

        ok = 0
        last_error = ""
        disk_stopped = False
        # The outer try exists so _sig_survey_done fires whatever happens; a
        # dead worker with no done signal leaves the page frozen until restart.
        try:
            for index, job in enumerate(jobs, start=1):
                settings = job.settings
                # Which model versions this pass ran against. These are
                # HuggingFace commit revisions read off the cache, so the call
                # is disk-only and safe on this worker thread. Per pass rather
                # than per batch: a row may override the method it runs.
                version_names = [settings.get("mapping_name")]
                if not settings.get("skip_segmentation"):
                    version_names.append(settings.get("segmentation_name"))
                model_versions = resolve_model_versions(n for n in version_names if n)
                # Hold between passes too, so pausing doesn't let the next one start.
                pause_event.wait()
                cancel_event = self._survey_cancel_event
                if cancel_event is not None and cancel_event.is_set():
                    store.set_run_status(job.run.id, "cancelled")
                    continue
                # Re-read the cart: taking a row out of a running session only
                # works if the worker looks. Two queries per multi-minute job.
                current = store.get_pass(job.pass_.id)
                if current is None or not store.has_batch_item(batch.id, job.pass_.id):
                    store.set_run_status(
                        job.run.id, "cancelled",
                        error="Taken out of the session before this pass started.",
                    )
                    continue
                try:
                    free_bytes = shutil.disk_usage(out_root).free
                except OSError:
                    free_bytes = None
                if free_bytes is not None and free_bytes < ROUGH_PASS_BYTES:
                    # Stop cleanly: mark this pass and every one after it
                    # not-started, so the batch runs again from here once space
                    # is freed. The reason goes on each row: cancelled evades
                    # the failure count, so the rows must say it themselves.
                    disk_stopped = True
                    for pending in jobs[index - 1:]:
                        store.set_run_status(
                            pending.run.id, "cancelled",
                            error="Ran out of disk space before this pass started.",
                        )
                    break
                log_handler = None
                try:
                    # The transect name reads as a place, not the run-dir slug:
                    # the panel already carries the "pass N of M" number, so the
                    # name need not. A pass with no transect is named by its
                    # clip, which is the only thing it has to be recognised by.
                    label = job.transect.name if job.transect else job.videos[0].file_name
                    self._sig_survey_progress.emit(index, len(jobs), label)
                    pass_started = time.monotonic()
                    store.set_run_status(job.run.id, "running")
                    out_dir = out_root / job.dir_name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    # Before anything else in the pass can fail: a pass that dies
                    # early is the one whose log gets read.
                    log_handler = open_run_log_file(out_dir)
                    # Published on the window as well as held here, so closing
                    # the window mid-pass detaches it. The handler is on the
                    # root logger, and one left attached goes on writing into a
                    # run directory nothing is running in any more.
                    self._run_log_file_handler = log_handler
                    # A retry gets a fresh directory; seeding hard-links
                    # prepared frames from any sibling with the same clip and
                    # settings, earlier attempts of this pass included.
                    seeded = seed_from_settings(
                        out_dir,
                        out_root,
                        settings,
                        [Path(video.path) for video in job.videos],
                        job.pass_.begin_s,
                        job.pass_.end_s,
                    )
                    if seeded is not None:
                        # Say the afternoon is not being spent again, so a diver
                        # watching a retry knows preparation was skipped.
                        self._sig_status_text.emit(
                            f"Pass {index} of {len(jobs)}: reusing prepared frames "
                            "from an earlier attempt."
                        )
                    instrumented_reconstruction(
                        video_paths=[video.path for video in job.videos],
                        output_dir=out_dir,
                        transect_length=job.transect.length_m if job.transect else None,
                        begin_s=job.pass_.begin_s,
                        end_s=job.pass_.end_s,
                        # The name, not the folder: the manifest carries this and
                        # Browse reads the finished run back under it.
                        run_name=job.label or job.dir_name,
                        viewer=self._viewer,
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                        scene_writer=self._write_scene_file,
                        manifest_extra={
                            "survey": survey_manifest_block(
                                job.run, job.pass_, job.transect, batch,
                                config=job.config, model_versions=model_versions,
                            )
                        },
                        **settings,
                    )
                    store.set_run_status(job.run.id, "succeeded")
                    # The manifest is complete now, timings included, so this is
                    # the one moment its provenance can be copied onto the row.
                    store.record_run_provenance(
                        job.run.id, wire.run_provenance(out_root, job.dir_name)
                    )
                    ok += 1
                    # Only a pass that ran to the end says anything about what
                    # the rest of the batch will cost.
                    self._sig_survey_pass_done.emit(index, time.monotonic() - pass_started)
                except ReconstructionCancelled:
                    store.set_run_status(job.run.id, "cancelled")
                except Exception as exc:
                    logger.exception("Pass %s failed", job.dir_name)
                    last_error = f"{job.dir_name}: {exc}"
                    store.set_run_status(job.run.id, "failed", error=str(exc)[:300])
                finally:
                    # Closed per pass, not per batch: the next pass opens its
                    # own, and a handler left attached would keep writing into
                    # the previous pass's directory.
                    if log_handler is not None:
                        close_run_log_file(log_handler)
                    self._run_log_file_handler = None
            if disk_stopped and not last_error:
                last_error = "Ran out of disk space before every pass finished."
        except Exception as exc:
            logger.exception("Batch worker failed between passes")
            if not last_error:
                last_error = str(exc)
        finally:
            self._sig_survey_done.emit(ok, len(jobs), last_error[:300])

    def _write_scene_file(self, output_dir: Path, data: dict, manifest: dict) -> None:
        """Cache the finished pass for fast reopen, reporting the `scene_save` stage.

        Called by ``instrumented_reconstruction`` on the batch worker thread once
        the pass has returned, so the stage is timed and its progress lands on the
        bars the rest of the pass used.
        """
        from deepreefmap_gui.runs.loaded_run import write_scene_file_from_run_data

        write_scene_file_from_run_data(
            output_dir, data, manifest, progress_cb=self._sig_load_progress.emit
        )

    def _on_survey_progress(self, index: int, total: int, name: str) -> None:
        self._status_label.setText(f"Processing pass {index} of {total}: {name}")
        # Fresh estimator per pass so the ETA does not blend across passes. The
        # batch card spans them instead, from the median of past runs.
        self._begin_progress(self._recon_model)
        # `_on_load_progress` drops every report while this is set, which would
        # stall each pass at the last percent through its scene write.
        self._load_cancelled = False
        for sink in self._progress_sinks():
            sink.set_batch_context(index, total, name)
        self._survey_running_index = index - 1
        self._refresh_survey_pass_statuses()

    def _on_survey_pass_done(self, index: int, seconds: float) -> None:
        """Fold a finished pass's real cost into the session estimate."""
        self._batch_progress.pass_finished(index, seconds)

    def _on_survey_done(self, ok: int, total: int, last_error: str) -> None:
        self._survey_worker_running = False
        # These passes are the newest evidence of what a run costs, so both
        # estimates built on past runs are recomputed rather than kept: the
        # footage capacity on disk, and the memory grade, which reads the peaks
        # the batch just recorded instead of an analytic guess.
        self._footage_rate_cache = None
        # Also the in-flight marker, or a walk that started before these runs
        # existed would be taken for a measurement of them.
        self._footage_rate_pending = None
        self._batch_prediction_cache = None
        self._update_memory_profile_warning()
        self._end_run_controls()
        self._reset_progress()
        for sink in self._progress_sinks():
            sink.clear_batch_context()
        self._survey_running_index = None
        self._survey_job_pass_ids = []
        self._recompute_row_actions()
        self._set_app_mode("SETUP")
        # The outcome goes on the page and stays there. The status bar is the
        # wrong home for it: the next thing that happens overwrites it, and a
        # finished batch is exactly when the user walks away from the laptop.
        # Failures name themselves by transect/pass. The last raw error stays out
        # of the way on the failed row's own tooltip.
        failed = self._failed_pass_labels()
        summary = f"{ok} of {total} pass{'' if total == 1 else 'es'} succeeded"
        if failed:
            summary += " · Failed: " + ", ".join(failed)
        elif last_error:
            # A batch-level stop (e.g. ran out of disk) leaves the remaining
            # passes cancelled, with no failed row to carry the reason.
            summary += " · " + last_error
        if ok:
            summary += " · double-click a row to open its run"
        self._survey_summary_label.setText(summary)
        self._survey_summary_label.setVisible(True)
        if ok == total:
            self._status_label.setText(f"Session complete: {ok}/{total} pass(es) succeeded.")
        elif failed:
            self._status_label.setText(
                f"Session finished: {ok}/{total} succeeded. Failed: {', '.join(failed)}."
            )
        elif last_error:
            self._status_label.setText(
                f"Session finished: {ok}/{total} succeeded. {last_error}"
            )
        else:
            self._status_label.setText(f"Session finished: {ok}/{total} succeeded.")
        # The same sentence the page carries, kept where it survives the next
        # thing that happens. A batch is walked away from, and the page it
        # finished on is not necessarily the one it is found on.
        self._notify_post(
            {
                "fingerprint": "batch.finished" if ok == total else "batch.failed",
                "title": self._status_label.text(),
                "body": summary if summary != self._status_label.text() else "",
                "severity": INFO if ok == total else NOTIFY_WARNING,
                "section": "browse" if ok else "process",
            }
        )
        # The order + next-cart view gives way to the cart when one was
        # assembled mid-run, else to the finished order.
        self._refresh_survey_batch_tab()
        self._refresh_data_manager()
        self._refresh_survey_analysis()
        if ok:
            self._show_session_results()
        # The order is over; whatever session is current now is the cart.
        self._survey_running_batch = None
        self._survey_next_cart_label.setVisible(False)

    def _show_session_results(self) -> None:
        """Land on what the session produced, rather than on a button offering it.

        A batch is walked away from, so the state it is found in should be its
        results. Filtered to this session, because the archive of everything is
        the wrong answer to "how did that go".

        Never while another run is loading in the viewer, and never over an
        explicit move: the queue is frozen during a batch, so anywhere the user
        has navigated to since is somewhere they chose.
        """
        # The order that ran, not whatever cart holds _survey_batch by now.
        batch = self._survey_running_batch or self._survey_batch
        if batch is None or self._current_section() != "process":
            return
        self._go_to_section("browse")
        self._focus_browse_on_session(batch.id)

    def _running_table_row(self) -> int:
        """Which table row the pass being processed sits on, or -1 if none is.

        The running index counts jobs, not model rows: the table is grouped and
        reorderable, so the two only coincide by accident.
        """
        index = self._survey_running_index
        if index is None or not 0 <= index < len(self._survey_job_pass_ids):
            return -1
        pass_id = self._survey_job_pass_ids[index]
        for model_index, row in enumerate(self._survey_rows):
            if row.pass_id == pass_id:
                return self._table_row_of(model_index)
        return -1

    def _running_status_item(self) -> QTableWidgetItem | None:
        """The status cell of the pass being processed, if one is."""
        table_row = self._running_table_row()
        return None if table_row < 0 else self._survey_pass_table.item(table_row, _COL_STATUS)

    def _on_pass_percent(self, percent: int) -> None:
        """Move the running row's own progress, so the queue shows where it is."""
        self._survey_pass_percent = percent
        item = self._running_status_item()
        if item is None:
            return
        item.setData(PASS_PERCENT_ROLE, percent)
        item.setText(f"Running {percent}%")
        # No tooltip: hovering this cell raises the stage breakdown itself, and a
        # Qt tooltip lands on top of it.

    def _refresh_survey_pass_statuses(self) -> None:
        store = self._try_survey_store()
        if store is None:
            return
        for index, row in enumerate(self._survey_rows):
            item = self._survey_pass_table.item(self._table_row_of(index), _COL_STATUS)
            if item is None:
                continue
            # Cleared for every row and re-applied below to the one in flight, so
            # a finished pass does not keep the fill it had while running.
            item.setData(PASS_PERCENT_ROLE, None)
            if row.pass_id is None:
                item.setText("")
                item.setToolTip("")
                continue
            runs = store.runs_for_pass(row.pass_id)
            last = runs[-1] if runs else None
            item.setText(status_label(last.status) if last is not None else "")
            # A failed pass carries its cause on the row, where it survives the
            # next event, rather than flashing once in the shared status bar. A
            # succeeded pass flags any non-fatal quality warning the same way.
            if last is not None and last.status == "failed":
                item.setToolTip(_diagnose_failure(last.error))
            elif last is not None and last.status == "succeeded":
                warnings = self._pass_quality_warnings(last.run_dir_name)
                if warnings:
                    item.setText("Succeeded ⚠")
                    item.setToolTip("\n".join(warnings))
                else:
                    item.setToolTip("")
            else:
                item.setToolTip("")
        if self._survey_worker_running:
            self._on_pass_percent(self._survey_pass_percent)

    def _pass_quality_warnings(self, run_dir_name: str) -> list[str]:
        """Non-fatal warnings a succeeded run recorded in its manifest, if any."""
        path = Path(self._out_root_input.text()).expanduser() / run_dir_name / "run_manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        warnings = manifest.get("quality_warnings") if isinstance(manifest, dict) else None
        return [str(w) for w in warnings] if isinstance(warnings, list) else []

    def _survey_pass_error(self, row: _PassRow) -> str:
        """The full error of a pass's latest run when it failed, else the empty string."""
        if row.pass_id is None:
            return ""
        runs = self._survey_store().runs_for_pass(row.pass_id)
        last = runs[-1] if runs else None
        return last.error if last is not None and last.status == "failed" else ""

    def _failed_pass_labels(self) -> list[str]:
        """Passes whose latest run failed, named by transect and pass number."""
        store = self._survey_store()
        labels: list[str] = []
        for row in self._survey_rows:
            if row.pass_id is None:
                continue
            runs = store.runs_for_pass(row.pass_id)
            if not runs or any(run.status == "succeeded" for run in runs):
                continue
            if runs[-1].status != "failed":
                continue
            transect = store.get_transect(row.transect_id) if row.transect_id else None
            labels.append(_failed_pass_label(transect, runs[-1].run_dir_name))
        return labels

    def _on_survey_pass_menu(self, pos) -> None:
        """Right-click for what the row buttons do not carry: reruns and errors."""
        index = self._model_index(self._survey_pass_table.rowAt(pos.y()))
        if index is None:
            return
        # Right-clicking outside the selection acts on the row under the cursor,
        # which is what every file manager does.
        if index not in self._selected_survey_rows():
            self._survey_pass_table.selectRow(self._table_row_of(index))
        menu = QMenu(self)
        states = self._survey_row_states()
        selected = self._selected_survey_rows() or [index]
        # Processing again is rare enough to live here rather than take a column
        # from every row: it puts a finished pass into the next session's cart.
        done = [i for i in selected if states[i] == DONE]
        if done:
            menu.addAction(
                f"Process {passes_phrase(len(done))} again",
                partial(self._process_rows_again, done),
            )
        # One row at a time: a name identifies one section.
        menu.addAction("Rename section…", partial(self._on_survey_rename, index))
        error = self._survey_pass_error(self._survey_rows[index])
        if error:
            menu.addAction("Copy error details", partial(self._copy_pass_error, error))
        # The row carries one truncated line of the failure; the log carries the
        # traceback, and the run that could not be loaded is exactly the one
        # whose log cannot be reached by opening it.
        run_dir = self._survey_pass_run_dir(self._survey_rows[index])
        if run_dir is not None:
            if (run_dir / "run.log").exists():
                menu.addAction("Show the run log", partial(self._show_pass_log, run_dir))
            menu.addAction("Show the run folder", partial(reveal_in_file_manager, run_dir))
        if menu.isEmpty():
            return
        menu.exec(self._survey_pass_table.viewport().mapToGlobal(pos))

    def _survey_pass_run_dir(self, row: _PassRow) -> Path | None:
        """Where this pass's latest run wrote, whether or not it finished."""
        if row.pass_id is None:
            return None
        try:
            runs = self._survey_store().runs_for_pass(row.pass_id)
        except Exception:
            return None
        if not runs:
            return None
        out_root = Path(self._out_root_input.text()).expanduser()
        run_dir = out_root / runs[-1].run_dir_name
        return run_dir if run_dir.is_dir() else None

    def _show_pass_log(self, run_dir: Path) -> None:
        """Put that run's log in the log panel and open it."""
        if not self._log_view.show_file(run_dir / "run.log", title=run_dir.name):
            self._status_label.setText(f"Could not read the log in {run_dir.name}.")
            return
        self._set_log_panel_visible(True)

    def _copy_pass_error(self, error: str) -> None:
        QGuiApplication.clipboard().setText(error)
        self._status_label.setText("Copied the error details to the clipboard.")
