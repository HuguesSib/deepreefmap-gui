"""The rows the Videos page is a list of: one clip each, and what was cut from it.

Innermost first: a strip painting a clip's passes along its length, a row
describing one clip, a row describing one pass, the scrolling list of date
groups those rows sit in, a bare list of one clip's pass rows for the detail
pane, and the header row naming the clip columns and sorting them. A clip's
pass rows sit directly under it and are shown by its disclosure chevron, so
the three levels the app has (clip, pass, run) read as the nesting they are.

The list builds a widget per clip, which suits a field season and would not suit
tens of thousands of them. The escape hatch is a QListView over a model with a
delegate: the strip's paint code moves across unchanged, since it already draws
from a rect and a list of spans rather than from its own children.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
    SignalInstance,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.fonts import tabular
from deepreefmap_gui.core.icons import (
    DEFAULT_INK,
    ICON_SM,
    broken_link_icon,
    cart_icon,
    check_icon,
    chevron_down_icon,
    chevron_right_icon,
    close_icon,
    direction_arrow_icon,
    icon_pixmap,
    link_icon,
    pencil_icon,
    play_icon,
    status_dot_icon,
    trash_icon,
    upload_icon,
)
from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BORDER,
    BRIGHT_TEXT,
    CARD_BG,
    CONTROL_HEIGHT,
    DISABLED_FG,
    ERROR,
    GROOVE,
    HEADER_PAD_V,
    PRIMARY,
    RADIUS_SM,
    SELECTION_BG,
    SELECTION_CONTROL_BG,
    SELECTION_CONTROL_BORDER,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    WARNING,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import (
    PILL_PROGRESS_ALPHA,
    PILL_TINT_ALPHA,
    STATUS_COLORS,
    EmptyState,
    SectionHeader,
    icon_button,
    muted_label,
    secondary_label,
)
from deepreefmap_gui.io.frame_grab import shared_frame_grabber
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey import statuses
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_MISSING,
    VideoLibraryEntry,
    preview_points,
)
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass, direction_phrase
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.video_groups import (
    DEFAULT_SORT_COLUMN,
    DEFAULT_SORT_DESCENDING,
    SORT_GRAVITY,
    SORT_LENGTH,
    SORT_NAME,
    SORT_RECORDED,
    SORT_SIZE,
    DateGroup,
    Span,
    capture_moment,
    pass_status,
    span_for_pass,
    timeline_spans,
)
from deepreefmap_gui.survey.video_probe import NO, SOURCE_CONTAINER, YES

# The strip is one of the app's bars, so it takes bar_qss's geometry: the same
# height, and the round ends MeterBar paints at half that height.
STRIP_BAR_HEIGHT = BAR_HEIGHT
STRIP_HEIGHT = STRIP_BAR_HEIGHT

# A hairline, the weight MeterBar's hatching is drawn at.
LINE_WIDTH = 2.0

# A ten-second section of an hour-long clip is under a pixel wide. Painted at
# least this wide so it can be seen and hit.
MIN_SPAN_WIDTH = SPACE_SM

# Pitch of the hatching over a clip whose length is unknown.
HATCH_PITCH = SPACE_SM

# Column widths in characters rather than pixels, so a column follows the user's
# font size instead of holding a measure that no longer fits it. Each column is
# wide enough for its header label and a sort arrow as well as its values.
NAME_CHARS = 30  # a GoPro file name, with room for the ones that are not
RECORDED_CHARS = 10  # "~14:32", "Recorded ▼"
LENGTH_CHARS = 9  # "12m 03s", "Length ▼"
SIZE_CHARS = 9  # "1015 MB", "Size ▼"
GRAVITY_CHARS = 9  # "Gravity ▼", over a cell holding only a dot
WINDOW_CHARS = 22  # "0:00-11:51 · 11m 51s"
TRANSECT_CHARS = 22  # a transect name, or "Unassigned"
# What the clip pane's chip is allowed to shrink to before the pane itself has
# to give: enough that an elided name is still a name rather than an ellipsis.
TRANSECT_MIN_CHARS = 6
RUNS_CHARS = 9  # "12 runs"

# What the clip name is allowed to shrink to. A GoPro name is told apart by its
# two ends, so below this an elided one stops identifying its clip.
NAME_MIN_CHARS = 26

# Below this the strip stops being a timeline and becomes a coloured smear.
SECTIONS_MIN_WIDTH = 180

# The name identifies the row and the strip summarises it, so a wider window
# feeds the name first. Qt's own stretch splits slack equally, which is what gave
# the strip the width and left the name frozen at NAME_CHARS.
NAME_WEIGHT, SECTIONS_WEIGHT = 3, 2

# One row: the smallest comfortable click target and not a pixel more. The list
# is a whole field season of clips, so every pixel of padding costs one less on
# screen, and the row's own buttons are told to fit rather than to pad.
ROW_HEIGHT = CONTROL_HEIGHT

# The disclosure column keeps its width on a clip with nothing to disclose, so
# the file names stay in one column all the way down the list.
DISCLOSURE_WIDTH = ICON_SM + SPACE_XS

# A section row starts where its clip's name does. That alignment is what makes
# the nesting read without drawing a connecting line for it.
SECTION_INDENT = SPACE_SM + DISCLOSURE_WIDTH + SPACE_SM + ICON_SM

# The play, cut and delete buttons that close every clip row, plus the archive
# button when there is a server to press it against.
TRAILING_BUTTONS_WIDTH = ICON_SM * 3 + SPACE_XS * 6
ARCHIVE_BUTTON_WIDTH = ICON_SM + SPACE_XS * 2

UNASSIGNED_NAME = "Unassigned"

# What turns a click on a clip row into a change to the set of picked clips
# rather than a move of the detail pane. The list's own rows, no tick column and
# no mode: the gesture is the one every file manager already teaches.
PICK_MODIFIERS = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier

# What the chip says when a section has not been filed. Not an error: a section
# runs perfectly well unfiled, so this is an invitation in the accent colour
# rather than a warning in the red one.
SET_TRANSECT = "Set transect"
SET_TRANSECT_TOOLTIP = "Not filed against a transect. Click to pick one."
CHANGE_TRANSECT_TOOLTIP = "Click to file this pass somewhere else."

# A tick, held for this long, so a click on the cart is answered where it was
# made rather than only by the count in the far corner of the window.
CART_ACK_MS = 1200

# The list takes a drop, and nothing else in the app says so.
DROP_HINT = "Drop clips here to import them"

# What the clip pane's section list says before anything has been cut.
EMPTY_TITLE = "Not cut into passes yet"
EMPTY_NOTE = "Use + to process part or all of it."

IN_CART_TOOLTIP = "In the cart. Click to take it back out."

# The section menu, named once so the page and the tests read the same words.
# The row's own buttons do the first four; the menu is the right-click copy of
# them, plus the one action that has nowhere on the row to live.
MENU_ADD_TO_CART = "Add to cart"
MENU_REMOVE_FROM_CART = "Take out of the cart"
MENU_RETRIM = "Adjust trim…"
MENU_REASSIGN = "Change transect…"
MENU_OPEN_TRANSECT = "Show on the Transects page"
# An unfiled section has no transect to show, but the page is still where one is
# drawn or imported, so the action goes there and says that instead of greying
# out and leaving no way to reach it.
MENU_OPEN_TRANSECTS_PAGE = "Open the Transects page"
MENU_DELETE = "Delete pass"
NO_TRANSECT_TOOLTIP = "This pass is not filed against a transect."
RETRIM_TOOLTIP = "Move this pass's window."
TRIM_UNLINKED_TOOLTIP = "The video file cannot be found. Add it again from where it lives now."
CART_UNLINKED_TOOLTIP = TRIM_UNLINKED_TOOLTIP
DELETE_BLOCKED_TOOLTIP = "This pass has runs. Delete them in Browse first."

# A clip row's own menu. Hiding is a view of the library rather than a fact
# about it, so it sits beside the destructive item rather than looking like one.
MENU_HIDE = "Hide clip"
MENU_UNHIDE = "Unhide clip"
MENU_HIDE_TOOLTIP = "Take this clip out of the list. Show hidden brings it back."
MENU_UNHIDE_TOOLTIP = "Put this clip back in the list."
MENU_DELETE_UNUSED = "Delete passes with no runs"
NO_UNUSED_TOOLTIP = "Every pass of this clip has been processed, or there are none."
MENU_DELETE_CLIP = "Delete clip"

# A trash can beside a video reads as deleting the footage, and this does not.
# Every place the action is named says so, since which one the user is looking
# at when the doubt arrives is not something the row gets to choose.
KEEPS_FILE_NOTE = "The video file itself is not deleted."
DELETE_CLIP_TOOLTIP = f"Take this clip out of the library. {KEEPS_FILE_NOTE}"
DELETE_CLIP_BLOCKED_TOOLTIP = f"This clip has runs. Delete them in Browse first. {KEEPS_FILE_NOTE}"

# The clip delete asks in the button rather than in a dialog: one click arms it,
# the second does it, and it disarms itself. Clearing a library of bad imports
# is a dozen deletes in a row, and a dozen modal dialogs is the whole cost of
# the job. Long enough to read the changed icon, short enough that a click on
# the next row is never the one that lands.
DELETE_ARM_MS = 3000
DELETE_CLIP_ARMED_TOOLTIP = f"Click again to remove it. {KEEPS_FILE_NOTE}"

# The clip row's "cut a new section": a single glyph rather than an icon, since
# the meaning is the character and drawing it would leave the icon layer with a
# shape it has no other use for.
NEW_SECTION_GLYPH = "+"

UNKNOWN_LENGTH_TOOLTIP = "Length unknown; passes cannot be drawn."
NO_SECTIONS_TOOLTIP = "No passes yet. Use + to cut one."
GRAVITY_UNKNOWN_TOOLTIP = "Gravity not read yet."
ESTIMATED_DATE_NOTE = "Recording date taken from the file timestamp; the clip carries none."
MISSING_FILE_NOTE = "Not found. Add videos… from its new home relinks it."
HIDDEN_NOTE = "Hidden on this machine. Shown while Show hidden is on."


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def length_label(duration_s: float | None) -> str:
    """A clip's length as "12m 03s", or nothing when it was never read."""
    if not duration_s or duration_s <= 0:
        return ""
    total = int(round(duration_s))
    return f"{total // 60}m {total % 60:02d}s"


def capture_label(video: VideoAsset) -> str:
    """The time of day the clip was shot, marked "~" when it is the file's own.

    A re-encoded clip carries no recording time, so its mtime stands in. That is
    a worse answer rather than a wrong one, and a diver reading a day's footage
    in order needs to know which of the two they are looking at.
    """
    stamp = capture_moment(video)
    if stamp is None:
        return ""
    shown = stamp.astimezone().strftime("%H:%M")
    return shown if video.captured_source == SOURCE_CONTAINER else f"~{shown}"


def size_label(size_bytes: int | None) -> str:
    """A clip's size on disk, or nothing when it was never read."""
    return format_bytes(size_bytes) if size_bytes else ""


def window_label(pass_: TransectPass) -> str:
    """A pass's time window, which is what tells two passes of one clip apart."""
    return f"{_clock(pass_.begin_s)}-{_clock(pass_.end_s)}"


def section_length_label(pass_: TransectPass) -> str:
    """How long the pass runs for, in the clip row's own words.

    The clip says how long the recording is; a pass is a part of it, and how
    much of it was cut is the figure that decides what a run will cost. Beside
    the window rather than in a column of its own, because the two answer one
    question between them.
    """
    if pass_.end_s is None:
        return ""
    seconds = int(round(max(0.0, pass_.end_s - pass_.begin_s)))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def preview_times(pass_: TransectPass) -> tuple[float, float, float]:
    """The three moments of a pass a hover preview shows.

    The same three ``catalogue.preview_points`` resolves onto chapters, but as
    the pass's own times: what a caption has to say is where in the window
    the frame came from, not where in some chapter file it was found.
    """
    return pass_.begin_s, (pass_.begin_s + pass_.end_s) / 2.0, pass_.end_s


def run_label(count: int) -> str:
    """ "3 runs", and nothing at all until a pass has been processed once."""
    if count < 1:
        return ""
    return f"{count} run" if count == 1 else f"{count} runs"


def section_facts(entry: VideoLibraryEntry) -> list[tuple[TransectPass, str, int]]:
    """Each pass of a clip in time order, with its status and its run count.

    The status comes from ``video_groups``, which is where that rule lives: the
    strip and the rows describe the same pass, and a second copy of the rule
    is how the two come to disagree. A pass whose clip has no readable length
    gets no span, and still gets a row.
    """
    spans = {span.pass_id: span for span in timeline_spans(entry)}
    runs: dict[object, list[RunRecord]] = {}
    for run in entry.runs:
        runs.setdefault(run.pass_id, []).append(run)
    facts = []
    for pass_ in sorted(entry.passes, key=lambda p: (p.begin_s, p.end_s)):
        span = spans.get(str(pass_.id))
        if span is not None:
            facts.append((pass_, span.status, span.run_count))
            continue
        mine = runs.get(pass_.id, [])
        facts.append((pass_, pass_status(mine), len(mine)))
    return facts


def section_strip_lead(widget: QWidget, name_width: int) -> int:
    """The gap that puts a pass row's strip under its clip row's.

    Derived from the constants both rows are built from plus the clip name's
    current width, never from measured pixels, so the two stay aligned at any UI
    font size and at any window width. A clip row leads with the disclosure, the
    link glyph, the name and four figure columns; a pass row leads with its
    indent, a status dot, the window, the transect chip and the run count.
    """
    cw = widget.fontMetrics().averageCharWidth()
    clip = (
        # A left margin, then one gap after each of the disclosure, the link, the
        # name and the four figure columns.
        SPACE_SM * 8
        + DISCLOSURE_WIDTH
        + ICON_SM
        + name_width
        + cw * (RECORDED_CHARS + LENGTH_CHARS + SIZE_CHARS + GRAVITY_CHARS)
    )
    section = (
        # SECTION_INDENT already carries two of these, then one gap after each of
        # the dot, the window, the chip, the arrow, the run count and this spacer.
        SPACE_SM * 8 + DISCLOSURE_WIDTH + ICON_SM * 3 + cw * (WINDOW_CHARS + TRANSECT_CHARS + RUNS_CHARS)
    )
    return max(0, clip - section)


def clip_name_width(available: int, char_width: int, *, archiving: bool = False) -> int:
    """How wide the clip name column is in a row ``available`` px across.

    ``archiving`` is whether the rows are carrying their archive button, which
    only appears once there is a server to send a clip to.

    The figures and the buttons take theirs first, then the name and the passes
    strip share what is left by weight, each held above its floor. The strip is
    not sized here: it takes the remainder through its layout stretch, so the two
    always add up to the row exactly.
    """
    fixed = char_width * (RECORDED_CHARS + LENGTH_CHARS + SIZE_CHARS + GRAVITY_CHARS)
    trailing = TRAILING_BUTTONS_WIDTH + (ARCHIVE_BUTTON_WIDTH if archiving else 0)
    spent = fixed + DISCLOSURE_WIDTH + ICON_SM + trailing + SPACE_SM * 10
    slack = max(0, available - spent - SECTIONS_MIN_WIDTH)
    share = slack * NAME_WEIGHT // (NAME_WEIGHT + SECTIONS_WEIGHT)
    return max(char_width * NAME_MIN_CHARS, share)


ARCHIVE_CLIP = "Send this clip to the server"
ARCHIVE_CLIP_TOOLTIP = (
    "Send this clip's original file to the registry's archive. On request only, "
    "so a metered field uplink is never spent by accident."
)
# What the registry holds of a clip, as the glyph says it. The same vocabulary
# the run rows use, so one icon means one thing across the app.
ARCHIVE_FACES = {
    "archived": "Already on the server.",
    "uploading": "Sending to the server...",
    "failed": "The registry could not verify the upload. Press to send it again.",
}


def archive_button(name: str, tooltip: str) -> QToolButton:
    """The upload glyph, as a row action rather than a labelled button."""
    return icon_button(upload_icon(), name, tooltip)


def paint_archive_button(button: QToolButton, state: str | None, tooltip: str) -> None:
    """Dress an archive button as what the registry holds, if it has been asked."""
    told = ARCHIVE_FACES.get(state or "")
    if told is None:
        button.setIcon(upload_icon(color=QColor(DEFAULT_INK)))
        button.setToolTip(tooltip)
        return
    # A tick for content already up, the upload glyph in the colour of what is
    # still to do.
    if state == "archived":
        button.setIcon(check_icon())
    else:
        button.setIcon(upload_icon(color=QColor(ERROR if state == "failed" else WARNING)))
    button.setToolTip(told)


def _fixed_width(label: QLabel, chars: int, *, figures: bool = False) -> None:
    """Reserve ``chars`` characters' worth of width for a row cell.

    Measured against a digit rather than ``averageCharWidth``, which is a mean
    dominated by lowercase letters and materially narrower than a figure: every
    numeric column sized that way was short of what it held. ``figures`` also
    gives the label tabular digits, so a column of times and sizes lines up
    down the list instead of shifting with whichever numbers a row happens to
    carry.
    """
    if figures:
        label.setFont(tabular(label.font()))
    label.setFixedWidth(label.fontMetrics().horizontalAdvance("0" * chars))


def _selectable(widget: QWidget, name: str) -> None:
    """Give a row the list's selection fill, keyed off a ``selected`` property.

    Everything written on a selected row is written in white. The muted greys
    the rows are made of are chosen against the page's dark ground, and none of
    them survives being laid over the selection blue: the tones are close
    enough that a value simply stops being readable. Set from the row rather
    than by each label, because a colour a widget sets on itself outranks one
    an ancestor sets on its descendants, and the app's tone rules live at the
    application level, which this outranks.

    Pixmaps are beyond a stylesheet's reach: icons are redrawn in the row's ink
    by ``set_selected`` on each row class.
    """
    widget.setObjectName(name)
    # A bare QWidget takes its background from the palette and ignores the
    # stylesheet's, which leaves the selection fill invisible.
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(
        f'QWidget#{name}[selected="true"] {{ background-color: {SELECTION_BG};'
        f" border-radius: {RADIUS_SM}px; }}"
        f'QWidget#{name}[selected="true"] QLabel {{ color: {BRIGHT_TEXT}; }}'
        # A quiet button is drawn on nothing, and on the selection fill nothing
        # is exactly what it reads as, so it keeps a dark ground under itself.
        f'QWidget#{name}[selected="true"] QToolButton {{'
        f" background-color: {SELECTION_CONTROL_BG};"
        f" border: 1px solid {SELECTION_CONTROL_BORDER}; color: {BRIGHT_TEXT}; }}"
        # Unavailable stays unavailable: the rule above would otherwise paint a
        # dead control in the same white as a live one.
        f'QWidget#{name}[selected="true"] QToolButton:disabled,'
        f' QWidget#{name}[selected="true"] QToolButton[dead="true"] {{'
        f" color: {DISABLED_FG}; border-color: {BORDER}; }}"
        # A mark you click rather than a button keeps its own nothing: boxing
        # the disclosure chevron on selection makes it read as an action.
        f'QWidget#{name}[selected="true"] QToolButton[bare="true"] {{'
        f" background-color: transparent; border: none; }}"
    )
    widget.setProperty("selected", False)


def _set_selected(widget: QWidget, chosen: bool) -> None:
    if widget.property("selected") == chosen:
        return
    widget.setProperty("selected", chosen)
    # A property a stylesheet selects on is only re-read on a repolish, and the
    # rules above select on the row to reach its children, so the children are
    # repolished too or they keep the colours of the state the row has left.
    for target in (widget, *widget.findChildren(QWidget)):
        style = target.style()
        style.unpolish(target)
        style.polish(target)


def _quiet_button(glyph: str, name: str, tooltip: str) -> QToolButton:
    """One of a row's trailing single-glyph buttons."""
    button = QToolButton()
    button.setText(glyph)
    button.setAccessibleName(name)
    button.setToolTip(tooltip)
    button.setProperty("quiet", "true")
    button.setProperty("pad", "none")
    return button


def apply_link_state(button: QToolButton, link_state: str, ink: QColor | None = None) -> None:
    """Dress a button as the clip's link: joined, broken, or not yet asked.

    Clickable in every state, including unknown: revealing is how you find out
    what became of a clip, and a state nobody has read yet is no reason to
    withhold the folder. Shared by the row and the clip pane, which say the same
    thing about the same clip in two places. A broken link keeps its red on a
    selected row: that colour is the fact, not the decoration.
    """
    if link_state == LINK_LINKED:
        button.setIcon(link_icon(color=ink) if ink is not None else link_icon())
        button.setToolTip("Show in folder")
    elif link_state == LINK_MISSING:
        button.setIcon(broken_link_icon())
        button.setToolTip(f"{MISSING_FILE_NOTE}\nShow the folder it was in.")
    else:
        button.setIcon(QIcon())
        button.setToolTip("Show in folder")


def set_button_dead(button: QToolButton, dead: bool) -> None:
    """Mark a row button that cannot act: no hover fill, still enabled for its tooltip."""
    value = "true" if dead else "false"
    if button.property("dead") == value:
        return
    button.setProperty("dead", value)
    style = button.style()
    style.unpolish(button)
    style.polish(button)


class SectionStrip(QWidget):
    """Where a clip's passes sit along its length, as one of the app's bars."""

    span_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._spans: list[Span] = []
        self._duration = 0.0
        self._names: dict[str, str] = {}

    def set_spans(
        self,
        spans: Sequence[Span],
        duration_s: float | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        """The passes, and the clip length they were normalised against.

        Without a length there is no scale to place them on, so the strip says
        so rather than spreading the passes it was given over a guess.
        """
        self._duration = float(duration_s or 0.0)
        self._spans = list(spans) if self._duration > 0 else []
        self._names = dict(names or {})
        self.setToolTip(self._resting_tooltip())
        self.update()

    def _resting_tooltip(self) -> str:
        """What the strip says when the pointer is not over a pass."""
        if self._duration <= 0:
            return UNKNOWN_LENGTH_TOOLTIP
        return "" if self._spans else NO_SECTIONS_TOOLTIP

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def span_at(self, x: float) -> str | None:
        """The pass whose pass covers ``x``, or None over bare groove."""
        for span, rect in self._span_rects():
            if rect.left() <= x <= rect.right():
                return span.pass_id
        return None

    def _bar_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self.width()), float(STRIP_BAR_HEIGHT))

    def _span_rects(self) -> list[tuple[Span, QRectF]]:
        track = self._bar_rect()
        rects = []
        for span in self._spans:
            left = track.left() + track.width() * span.begin
            width = max(MIN_SPAN_WIDTH, track.width() * (span.end - span.begin))
            width = min(width, max(MIN_SPAN_WIDTH, track.right() - left))
            rects.append((span, QRectF(left, track.top(), width, track.height())))
        return rects

    def _span_colour(self, span: Span) -> QColor:
        return QColor(STATUS_COLORS.get(span.status, TEXT_MUTED))

    def _span_tooltip(self, span: Span) -> str:
        name = self._names.get(span.pass_id) or UNASSIGNED_NAME
        window = f"{_clock(span.begin * self._duration)}-{_clock(span.end * self._duration)}"
        # How many times this pass has been run is part of reading the bar,
        # and the clip row has no column that says it.
        facts = [name, window, statuses.status_label(span.status)]
        if span.run_count:
            facts.append(run_label(span.run_count))
        return "  ·  ".join(facts)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pass_id = self.span_at(event.position().x())
        span = next((s for s in self._spans if s.pass_id == pass_id), None)
        self.setToolTip(self._span_tooltip(span) if span else self._resting_tooltip())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pass_id = self.span_at(event.position().x())
            if pass_id is not None:
                self.span_clicked.emit(pass_id)
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._bar_rect()
        radius = track.height() / 2.0
        groove = QPainterPath()
        groove.addRoundedRect(track, radius, radius)
        painter.fillPath(groove, QColor(GROOVE))
        if self._duration <= 0:
            self._paint_unknown(painter, groove, track)
        elif not self._spans:
            self._paint_uncut(painter, track, radius)
        for span, rect in self._span_rects():
            self._paint_span(painter, span, rect, radius)
        painter.end()

    def _paint_uncut(self, painter: QPainter, track: QRectF, radius: float) -> None:
        """A dashed red edge round a clip nothing has been cut from.

        A clip with its passes scrolled off screen and a clip with none at all
        painted the same bare groove, and the second is the one holding up a
        day's processing. The dashes say the outline is where a pass would go
        rather than a pass itself.
        """
        pen = QPen(QColor(ERROR))
        pen.setWidthF(LINE_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Inset by half the pen, or the stroke is clipped by the widget's edge.
        painter.drawRoundedRect(track.adjusted(1.0, 1.0, -1.0, -1.0), radius, radius)

    def _paint_span(self, painter: QPainter, span: Span, rect: QRectF, radius: float) -> None:
        colour = self._span_colour(span)
        fill = QColor(colour)
        # A section not started yet is a claim about the future, so it is tinted
        # at the weight a chip is rather than the weight a filled bar is.
        idle = statuses.status_tone(span.status) == statuses.TONE_IDLE
        fill.setAlpha(PILL_TINT_ALPHA if idle else PILL_PROGRESS_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        pen = QPen(colour)
        pen.setWidthF(LINE_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

    def _paint_unknown(self, painter: QPainter, groove: QPainterPath, track: QRectF) -> None:
        """Hatching, as MeterBar draws a figure it cannot report.

        In TEXT_DIM rather than MeterBar's ERROR: a length nobody has read is
        not a fault, it is an answer the library does not have yet.
        """
        painter.setClipPath(groove)
        pen = QPen(QColor(TEXT_DIM))
        pen.setWidthF(LINE_WIDTH)
        painter.setPen(pen)
        x = track.left() - track.height()
        while x < track.right() + track.height():
            painter.drawLine(QPointF(x, track.bottom()), QPointF(x + track.height(), track.top()))
            x += HATCH_PITCH


class VideoRow(QWidget):
    """One clip: where it is, when it was shot, and what has been cut from it."""

    play_requested = Signal(str)
    reveal_requested = Signal(str)
    new_section_requested = Signal(str)
    expand_toggled = Signal(str, bool)
    activated = Signal(str)
    # The same press as ``activated``, with the modifiers held during it, so the
    # list can read a ctrl or shift click as a change to a set of clips.
    clicked = Signal(str, object)
    span_clicked = Signal(str)
    hide_requested = Signal(str)
    delete_unused_requested = Signal(str)
    delete_requested = Signal(str)
    archive_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _selectable(self, "videoRow")
        self.setFixedHeight(ROW_HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 0, SPACE_SM, 0)
        row.setSpacing(SPACE_SM)

        self.chevron = QToolButton()
        self.chevron.setCheckable(True)
        self.chevron.setFixedWidth(DISCLOSURE_WIDTH)
        self.chevron.setAccessibleName("Passes")
        self.chevron.setToolTip("Show the passes cut from this clip.")
        self.chevron.setProperty("bare", "true")
        self.chevron.toggled.connect(self._on_chevron)
        row.addWidget(self.chevron)

        # The link state and the way to the file are one control: the question a
        # broken link raises is "where did it go", and the answer is the folder.
        self.link_btn = QToolButton()
        self.link_btn.setFixedWidth(ICON_SM)
        self.link_btn.setAccessibleName("Show in folder")
        self.link_btn.setProperty("quiet", "true")
        self.link_btn.setProperty("pad", "none")
        self.link_btn.clicked.connect(lambda: self._emit(self.reveal_requested))
        row.addWidget(self.link_btn)

        self._name = secondary_label()
        _fixed_width(self._name, NAME_CHARS)
        row.addWidget(self._name)
        self._name_width = self._name.width()

        self._recorded = muted_label()
        _fixed_width(self._recorded, RECORDED_CHARS, figures=True)
        row.addWidget(self._recorded)

        # Length and size are figures, so they right-align under their header
        # cells and their digits line up down the list.
        self._length = muted_label()
        self._length.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _fixed_width(self._length, LENGTH_CHARS, figures=True)
        row.addWidget(self._length)

        self._size = muted_label()
        self._size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _fixed_width(self._size, SIZE_CHARS, figures=True)
        row.addWidget(self._size)

        # A dot and nothing else. The word "Gravity" beside a green dot in a
        # column headed Gravity was the same fact three times over.
        self._gravity = QLabel()
        self._gravity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _fixed_width(self._gravity, GRAVITY_CHARS)
        row.addWidget(self._gravity)

        self.strip = SectionStrip()
        self.strip.span_clicked.connect(self.span_clicked)
        row.addWidget(self.strip, 1)

        self.play_btn = QToolButton()
        self.play_btn.setIcon(play_icon())
        self.play_btn.setAccessibleName("Play")
        self.play_btn.setToolTip("Play")
        self.play_btn.setProperty("quiet", "true")
        self.play_btn.setProperty("pad", "none")
        self.play_btn.clicked.connect(lambda: self._emit(self.play_requested))
        row.addWidget(self.play_btn)

        self.new_section_btn = _quiet_button(NEW_SECTION_GLYPH, "Cut a new pass", "Cut a new pass")
        self.new_section_btn.clicked.connect(lambda: self._emit(self.new_section_requested))
        row.addWidget(self.new_section_btn)

        # Beside the row's other actions rather than in the detail pane's title:
        # sending a clip up is something done to the clip, like playing it or
        # cutting from it. Hidden until there is a server to send it to.
        self.archive_btn = archive_button(ARCHIVE_CLIP, ARCHIVE_CLIP_TOOLTIP)
        self.archive_btn.setVisible(False)
        self.archive_btn.clicked.connect(lambda: self._emit(self.archive_requested))
        row.addWidget(self.archive_btn)

        # Live whatever the clip's state: the handler is what refuses, and the
        # tooltip carries the reason. A disabled button shows no tooltip.
        self.delete_btn = icon_button(trash_icon(), MENU_DELETE_CLIP, DELETE_CLIP_TOOLTIP)
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        row.addWidget(self.delete_btn)
        # A text glyph measures narrower than an icon, and the sections column has
        # to end where a section row's own strip does.
        self.new_section_btn.setFixedWidth(self.delete_btn.sizeHint().width())
        # Owned by the row, so a list rebuilt under an armed button takes the
        # timer down with the row rather than firing into a deleted widget.
        self._delete_arm = QTimer(self)
        self._delete_arm.setSingleShot(True)
        self._delete_arm.setInterval(DELETE_ARM_MS)
        self._delete_arm.timeout.connect(self._apply_delete_icon)

        self._entry: VideoLibraryEntry | None = None
        self._hidden = False
        self._selected = False
        self._sync_chevron()

    @property
    def entry(self) -> VideoLibraryEntry | None:
        return self._entry

    @property
    def video_id(self) -> str:
        return "" if self._entry is None else str(self._entry.video.id)

    @property
    def expanded(self) -> bool:
        return self.chevron.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        """Open or close the clip without reporting it back as a user's click."""
        blocked = self.chevron.blockSignals(True)
        self.chevron.setChecked(expanded and self._has_sections())
        self.chevron.blockSignals(blocked)
        self._sync_chevron()

    def _has_sections(self) -> bool:
        return self._entry is not None and bool(self._entry.passes)

    def set_selected(self, chosen: bool) -> None:
        """Take the selection fill, and put the row's ink on top of it.

        The icons are pixmaps, so no stylesheet can lighten them: they are
        redrawn in the ink the row is currently written in. The ones that mean
        something on their own -- a broken link, a gravity dot -- keep their
        own colour, since that is the fact they are there to carry.

        A no-op when the row is already in that state: the list repaints every
        row on every scan, and redrawing a season's icons for one changed row is
        the whole cost of a background refresh.
        """
        if chosen == self._selected:
            return
        _set_selected(self, chosen)
        self._selected = chosen
        self._sync_chevron()
        if self._entry is not None:
            self._set_link(self._entry)
        ink = self._ink()
        self.play_btn.setIcon(play_icon(color=ink) if ink is not None else play_icon())
        self._apply_delete_icon()

    def _ink(self) -> QColor | None:
        """White while the row is selected, and each glyph's own colour otherwise.

        None rather than a shade: off selection every icon keeps the weight it
        was drawn at, and the link mark is quieter than the play button on
        purpose.
        """
        return QColor(BRIGHT_TEXT) if self._selected else None

    def _sync_chevron(self) -> None:
        """Blank and dead on a clip with no passes, so the column stays aligned."""
        has_sections = self._has_sections()
        self.chevron.setEnabled(has_sections)
        # Muted while the row is not selected: a disclosure mark is not one of
        # the row's facts, and drawn at the weight of one it competes with them.
        ink = self._ink() or QColor(TEXT_MUTED)
        if not has_sections:
            self.chevron.setIcon(QIcon())
        elif self.chevron.isChecked():
            self.chevron.setIcon(chevron_down_icon(color=ink))
        else:
            self.chevron.setIcon(chevron_right_icon(color=ink))

    def _on_chevron(self, expanded: bool) -> None:
        self._sync_chevron()
        if self._entry is not None:
            self.expand_toggled.emit(self.video_id, expanded)

    def _emit(self, signal: SignalInstance) -> None:
        if self._entry is not None:
            signal.emit(self.video_id)

    def _on_delete_clicked(self) -> None:
        """Arm on the first click, delete on the second.

        The question a confirmation dialog asks is asked by the button itself,
        so a run of deletes is a run of clicks rather than a run of dialogs.
        """
        if self._delete_arm.isActive():
            self._delete_arm.stop()
            self._apply_delete_icon()
            self._emit(self.delete_requested)
            return
        if self._entry is not None and self._entry.run_count:
            return
        self._delete_arm.start()
        self._apply_delete_icon()

    def _apply_delete_icon(self) -> None:
        """A trash can, or a tick asking whether that is really meant."""
        if self._delete_arm.isActive():
            self.delete_btn.setIcon(check_icon(color=QColor(ERROR)))
            self.delete_btn.setToolTip(DELETE_CLIP_ARMED_TOOLTIP)
            return
        runs = 0 if self._entry is None else self._entry.run_count
        ink = QColor(DISABLED_FG) if runs else self._ink() or QColor(DEFAULT_INK)
        self.delete_btn.setIcon(trash_icon(color=ink))
        set_button_dead(self.delete_btn, bool(runs))
        self.delete_btn.setToolTip(DELETE_CLIP_BLOCKED_TOOLTIP if runs else DELETE_CLIP_TOOLTIP)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Report the press, and treat a bare one as picking this clip.

        A ctrl or shift click is about the set of clips rather than about this
        one, so it reports itself and leaves the detail pane where it was.
        """
        if event.button() == Qt.MouseButton.LeftButton and self._entry is not None:
            modifiers = event.modifiers()
            self.clicked.emit(self.video_id, modifiers)
            if not modifiers & PICK_MODIFIERS:
                self.activated.emit(self.video_id)
        super().mousePressEvent(event)

    def unused_sections(self) -> int:
        """Passes of this clip nothing was ever made from, so nothing needs them."""
        if self._entry is None:
            return 0
        used = {str(run.pass_id) for run in self._entry.runs}
        return sum(1 for pass_ in self._entry.passes if str(pass_.id) not in used)

    def menu(self) -> QMenu:
        """What can be done with the clip itself, as the pass rows offer too."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        hide = menu.addAction(MENU_UNHIDE if self._hidden else MENU_HIDE)
        hide.setToolTip(MENU_UNHIDE_TOOLTIP if self._hidden else MENU_HIDE_TOOLTIP)
        hide.triggered.connect(lambda *_: self.hide_requested.emit(self.video_id))
        menu.addSeparator()
        unused = self.unused_sections()
        delete = menu.addAction(MENU_DELETE_UNUSED)
        delete.setEnabled(unused > 0)
        delete.setToolTip(
            f"{unused} section{'' if unused == 1 else 's'} of this clip have produced nothing."
            if unused
            else NO_UNUSED_TOOLTIP
        )
        delete.triggered.connect(lambda *_: self.delete_unused_requested.emit(self.video_id))
        runs = 0 if self._entry is None else self._entry.run_count
        delete_clip = menu.addAction(MENU_DELETE_CLIP)
        delete_clip.setToolTip(DELETE_CLIP_BLOCKED_TOOLTIP if runs else DELETE_CLIP_TOOLTIP)
        delete_clip.triggered.connect(lambda *_: self.delete_requested.emit(self.video_id))
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        if self._entry is not None:
            self.menu().exec(event.globalPos())

    def set_name_width(self, width: int) -> None:
        """Take the width the list computed for the name column, and re-elide to it."""
        if width == self._name_width:
            return
        self._name_width = width
        self._name.setFixedWidth(width)
        self._apply_name_text()

    def _apply_name_text(self) -> None:
        """A GoPro name is told apart by its two ends, so it elides from the middle.

        Re-run whenever the column changes width: elided once at fill time, a
        widened column kept the ellipsis it no longer needed.
        """
        if self._entry is None:
            return
        self._name.setText(
            self._name.fontMetrics().elidedText(
                self._entry.video.file_name,
                Qt.TextElideMode.ElideMiddle,
                self._name_width,
            )
        )

    def set_entry(
        self,
        entry: VideoLibraryEntry,
        transect_name: Callable[[Any], str | None],
        *,
        hidden: bool = False,
    ) -> None:
        """Describe one clip. ``transect_name`` resolves a pass's transect id."""
        self._entry = entry
        self._hidden = hidden
        video = entry.video
        self._set_link(entry)
        self._apply_name_text()
        self._recorded.setText(capture_label(video))
        self._length.setText(length_label(video.duration_s))
        self._size.setText(size_label(video.size_bytes))
        self._set_gravity(video)
        self.strip.set_spans(
            timeline_spans(entry),
            video.duration_s,
            {str(pass_.id): transect_name(pass_.transect_id) or UNASSIGNED_NAME for pass_ in entry.passes},
        )
        # Playing and cutting both decode the file, so they need the file, and
        # an unknown link state is not yet a yes. Revealing stays live: the
        # folder is where you go to find out what became of a missing clip.
        self.play_btn.setEnabled(entry.link_state == LINK_LINKED)
        self.new_section_btn.setEnabled(entry.link_state == LINK_LINKED)
        # A row refilled under an armed button describes some other clip by the
        # time the second click lands, so the arming does not survive the fill.
        self._delete_arm.stop()
        self._apply_delete_icon()
        self.set_expanded(self.expanded)
        self.setToolTip(self._row_tooltip(entry))

    def _row_tooltip(self, entry: VideoLibraryEntry) -> str:
        lines = [entry.video.path]
        if self._hidden:
            lines.append(HIDDEN_NOTE)
        if entry.link_state == LINK_MISSING:
            lines.append(MISSING_FILE_NOTE)
        if capture_label(entry.video).startswith("~"):
            lines.append(ESTIMATED_DATE_NOTE)
        return "\n".join(lines)

    def _set_link(self, entry: VideoLibraryEntry) -> None:
        apply_link_state(self.link_btn, entry.link_state, ink=self._ink())

    def _set_gravity(self, video: VideoAsset) -> None:
        """Whether the camera recorded a gravity vector, and silence when unread.

        Same honesty rule the link icon follows: an unread clip gets no mark,
        because "no gravity" is a fact about the footage and this is not it.
        """
        if video.gravity == YES:
            self._gravity.setPixmap(_dot(SUCCESS))
            self._gravity.setToolTip("The camera recorded a gravity vector.")
        elif video.gravity == NO:
            self._gravity.setPixmap(_dot(ERROR))
            self._gravity.setToolTip("No gravity vector in this clip's telemetry.")
        else:
            self._gravity.clear()
            self._gravity.setToolTip(GRAVITY_UNKNOWN_TOOLTIP)

    @property
    def gravity_dot(self) -> QPixmap | None:
        return self._gravity.pixmap() or None

    @property
    def gravity_tooltip(self) -> str:
        return self._gravity.toolTip()

    @property
    def link_tooltip(self) -> str:
        return self.link_btn.toolTip()


def _dot(colour: str) -> QPixmap:
    return icon_pixmap(status_dot_icon(colour), ICON_SM)


class TransectChip(QToolButton):
    """Where a pass is filed and which way it was swum, and the way to change it.

    A label that is also the control: the fact and the way to edit it are the
    same thing, so there is no second button competing with it for the row.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("quiet", "true")
        self.setProperty("pad", "none")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Transect")
        self._full = ""
        self._colour = TEXT_MUTED
        self._selected = False

    @property
    def full_text(self) -> str:
        """What the chip says before the width of the row gets at it."""
        return self._full

    def set_assignment(self, name: str | None, direction: str | None) -> None:
        if name:
            self._full = name
            self._colour = TEXT_MUTED
            self.setToolTip(f"{name}, {direction}. {CHANGE_TRANSECT_TOOLTIP}")
        else:
            self._full = SET_TRANSECT
            self._colour = PRIMARY
            self.setToolTip(SET_TRANSECT_TOOLTIP)
        self._apply_colour()
        self._apply_elide()

    def set_selected(self, chosen: bool) -> None:
        """White on the selection fill.

        Set here rather than left to the row's stylesheet: a colour a widget
        sets on itself outranks one an ancestor sets on its descendants, so the
        chip would go on painting itself muted grey on the blue.
        """
        self._selected = chosen
        self._apply_colour()

    def _apply_colour(self) -> None:
        colour = BRIGHT_TEXT if self._selected else self._colour
        self.setStyleSheet(f"QToolButton {{ color: {colour}; text-align: left; }}")

    def _apply_elide(self) -> None:
        room = max(0, self.width() - SPACE_SM)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, room)
        if shown != self.text():
            self.setText(shown)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()


class SectionRow(QWidget):
    """One pass of a clip: its window, where it is filed, and what came of it.

    Indented under the clip it was cut from, because a pass only means
    anything as part of that clip. The same row serves the clip pane, where
    ``compact`` drops the columns a third of a page has no room for.

    The trailing buttons stay enabled in every state. A disabled QToolButton
    takes no mouse events and so shows no tooltip, which would leave "why can I
    not delete this" answerable only by clicking; the handlers refuse and say
    why, and the tooltip says why first.
    """

    activated = Signal(str)
    add_to_cart_requested = Signal(str)
    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    delete_requested = Signal(str)
    open_transect_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        _selectable(self, "sectionRow")
        self.setFixedHeight(ROW_HEIGHT)
        self._compact = compact

        row = QHBoxLayout(self)
        row.setContentsMargins(0 if compact else SECTION_INDENT, 0, SPACE_SM, 0)
        row.setSpacing(SPACE_XS if compact else SPACE_SM)

        self._dot_label = QLabel()
        self._dot_label.setFixedWidth(ICON_SM)
        row.addWidget(self._dot_label)

        # The window leads: it is the section's identity, and the only thing
        # that tells two sections of one clip apart before they are filed.
        self._window = secondary_label()
        if not compact:
            _fixed_width(self._window, WINDOW_CHARS, figures=True)
        else:
            # No column here: the pane is a third of the page, and a column
            # wide enough for the longest window a clip could have spends the
            # difference on a gap in front of the transect. Every section of one
            # clip writes its window to much the same length anyway, so they
            # line up without being made to.
            self._window.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(self._window)

        self.transect_chip = TransectChip()
        self.transect_chip.clicked.connect(lambda: self._emit(self.reassign_requested))
        if compact:
            # The chip takes what the pane leaves it and elides; in the wide
            # list it holds a column so the names line up down the page. The
            # floor is set here rather than left to the button's own text, or a
            # long transect name makes the row wider than the pane and pushes
            # the buttons off the end of it.
            self.transect_chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.transect_chip.setMinimumWidth(self.fontMetrics().averageCharWidth() * TRANSECT_MIN_CHARS)
            row.addWidget(self.transect_chip, 1)
        else:
            self.transect_chip.setFixedWidth(self.fontMetrics().averageCharWidth() * TRANSECT_CHARS)
            row.addWidget(self.transect_chip)

        # An arrow rather than the word: which way a swim went is one bit, and
        # "Forward" spelled out took a column the clip pane cannot spare. The
        # tooltip still says it in words.
        self._direction = QLabel()
        self._direction.setFixedWidth(ICON_SM)
        row.addWidget(self._direction)

        self._runs = muted_label()
        _fixed_width(self._runs, RUNS_CHARS, figures=True)
        self._runs.setVisible(not compact)
        row.addWidget(self._runs)

        # In the wide list the section gets its own strip, on the same axis as the
        # clip's above it, so which part of the clip a section is can be seen
        # rather than read. Mouse-transparent: the row's own click selects it and
        # its tooltip carries the frame preview.
        self.strip: SectionStrip | None = None
        if not compact:
            self._strip_lead = QLabel()
            self._strip_lead.setFixedWidth(section_strip_lead(self, self.fontMetrics().averageCharWidth() * NAME_CHARS))
            row.addWidget(self._strip_lead)
            self.strip = SectionStrip()
            self.strip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.addWidget(self.strip, 1)

        # Nothing to press: a pass is not archived, its runs are. It reserves the
        # width the clip row's archive button takes so the two strips keep ending
        # on the same pixel, which is what makes a position along one mean the
        # same as along the other.
        self._archive_gap = QWidget()
        self._archive_gap.setFixedWidth(ARCHIVE_BUTTON_WIDTH)
        self._archive_gap.setVisible(False)
        row.addWidget(self._archive_gap)

        self.cart_btn = icon_button(cart_icon(), MENU_ADD_TO_CART, MENU_ADD_TO_CART)
        self.cart_btn.clicked.connect(self._on_cart_clicked)
        row.addWidget(self.cart_btn)
        # Owned by the row, so a list rebuilt under a pending tick takes the
        # timer down with the row rather than firing into a deleted widget.
        self._cart_ack = QTimer(self)
        self._cart_ack.setSingleShot(True)
        self._cart_ack.setInterval(CART_ACK_MS)
        self._cart_ack.timeout.connect(self._apply_cart_icon)

        self.trim_btn = icon_button(pencil_icon(), MENU_RETRIM, RETRIM_TOOLTIP)
        self.trim_btn.clicked.connect(lambda: self._emit(self.retrim_requested))
        row.addWidget(self.trim_btn)

        self.delete_btn = icon_button(trash_icon(), MENU_DELETE, MENU_DELETE)
        self.delete_btn.clicked.connect(lambda: self._emit(self.delete_requested))
        row.addWidget(self.delete_btn)

        self._pass: TransectPass | None = None
        self._run_count = 0
        self._in_cart = False
        self._available = True
        self._selected = False
        # What the tooltip says before any frame has been decoded, and what it
        # goes on saying underneath them once they have.
        self._plain_tooltip = ""
        self._preview: list[tuple[str, float]] | None = None
        self._preview_key = ""
        self._preview_wired = False

    def set_name_width(self, width: int) -> None:
        """Follow the clip name's column, so this row's strip stays under its clip's."""
        if self.strip is None:
            return
        self._strip_lead.setFixedWidth(section_strip_lead(self, width))

    def set_server_connected(self, connected: bool) -> None:
        """Reserve the clip rows' archive slot, so both strips still line up."""
        self._archive_gap.setVisible(connected)

    @property
    def pass_id(self) -> str:
        return "" if self._pass is None else str(self._pass.id)

    @property
    def section(self) -> TransectPass | None:
        return self._pass

    @property
    def transect_id(self) -> str:
        if self._pass is None or self._pass.transect_id is None:
            return ""
        return str(self._pass.transect_id)

    def set_section(
        self,
        pass_: TransectPass,
        *,
        transect_name: str | None,
        status: str,
        run_count: int = 0,
        in_cart: bool = False,
        available: bool = True,
        preview: list[tuple[str, float]] | None = None,
        duration_s: float | None = None,
    ) -> None:
        """Describe one section. ``status`` comes from ``section_facts``.

        ``available`` is the clip's file being findable. A link nobody has
        checked yet counts as available: not knowing is not the same as knowing
        it is gone, and marking every unchecked clip in red says the library is
        broken every time the app opens.

        ``preview`` is where the section's frames can be grabbed from, out of
        ``catalogue.preview_points``. None where there is no file to read, and
        the row then says in words what it cannot show in pictures.

        ``duration_s`` is the clip's length, which is what places this section
        along it. Without one the strip is hidden: the row is the section, so
        neither "nothing cut from this" nor "length unknown" is a statement it
        can make.
        """
        self._pass = pass_
        self._run_count = run_count
        self._in_cart = in_cart
        self._available = available
        self._dot_label.setPixmap(_dot(STATUS_COLORS.get(status, TEXT_MUTED)))
        if self.strip is not None:
            span = span_for_pass(pass_, duration_s, status, run_count)
            self.strip.setVisible(span is not None)
            # There is no span without a duration, so this only restates the length.
            if span is not None and duration_s is not None:
                self.strip.set_spans(
                    [span],
                    float(duration_s),
                    {str(pass_.id): transect_name or UNASSIGNED_NAME},
                )
        length = section_length_label(pass_)
        self._window.setText(f"{window_label(pass_)} · {length}" if length else window_label(pass_))
        self.transect_chip.set_assignment(transect_name, pass_.direction)
        self._direction.setToolTip(f"Swum {direction_phrase(pass_.direction)} along the transect.")
        self._apply_icons()
        self._runs.setText(run_label(run_count))
        self.delete_btn.setToolTip(DELETE_BLOCKED_TOOLTIP if run_count else MENU_DELETE)
        # Trimming decodes the file, so a clip whose file is gone cannot be
        # trimmed. Marked in red rather than greyed out: a disabled button shows
        # no tooltip, and the reason is the whole of what the user needs.
        self.trim_btn.setToolTip(RETRIM_TOOLTIP if available else TRIM_UNLINKED_TOOLTIP)
        # The window is not repeated here: the row shows it, and once frames
        # arrive each one is captioned with where in the window it came from.
        self._plain_tooltip = f"{transect_name or UNASSIGNED_NAME}  ·  {statuses.status_label(status)}"
        self._preview = preview if available else None
        self._preview_key = f"{pass_.id}@{pass_.begin_s:.2f}-{pass_.end_s:.2f}"
        self.setToolTip(self._plain_tooltip)

    def event(self, event: QEvent) -> bool:
        """Decode the preview only when a tooltip is actually being asked for.

        A pass a cursor never rests on costs nothing, which is what makes it
        affordable to do this for every row in a season's worth of clips.
        """
        if event.type() == QEvent.Type.ToolTip:
            self._request_preview()
        return super().event(event)

    def _request_preview(self) -> None:
        if not self._preview:
            return
        grabber = shared_frame_grabber()
        if not self._preview_wired:
            grabber.frames_ready.connect(self._on_frames_ready)
            self._preview_wired = True
        frames = grabber.request(self._preview_key, self._preview)
        if frames is not None:
            self.setToolTip(self._preview_tooltip(frames))

    def _on_frames_ready(self, key: str, frames: list[str | None]) -> None:
        if key != self._preview_key:
            return
        rich = self._preview_tooltip(frames)
        self.setToolTip(rich)
        # The plain tooltip is already on screen by the time the frames land, so
        # it is replaced where it stands rather than after a leave and a return.
        if self.underMouse():
            QToolTip.showText(QCursor.pos(), rich, self)

    def _preview_tooltip(self, frames: Sequence[str | None]) -> str:
        """The frames in a row, each captioned, with the plain line beneath."""
        if self._pass is None:
            return self._plain_tooltip
        cells = [
            f"<td align='center'><img src='{QUrl.fromLocalFile(jpeg).toString()}'><br>"
            f"<span style='color:{TEXT_MUTED}'>{_clock(t_s)}</span></td>"
            for jpeg, t_s in zip(frames, preview_times(self._pass), strict=False)
            if jpeg
        ]
        if not cells:
            return self._plain_tooltip
        return (
            f"<table cellspacing='6'><tr>{''.join(cells)}</tr></table>"
            f"<div align='center' style='color:{TEXT_MUTED}'>"
            f"{html.escape(self._plain_tooltip)}</div>"
        )

    def _emit(self, signal: SignalInstance) -> None:
        """Nothing at all until the row describes a pass.

        A pass can span two clips and so has a row under each; a row the list
        built and has not filled yet knows no pass, and a click on it used to
        reach the page as an empty id.
        """
        if self._pass is not None:
            signal.emit(self.pass_id)

    def set_selected(self, chosen: bool) -> None:
        """Take the selection fill, and put the row's ink on top of it.

        The icons are pixmaps, so no stylesheet can lighten them: they are
        redrawn in the ink the row is currently written in.
        """
        _set_selected(self, chosen)
        self._selected = chosen
        self.transect_chip.set_selected(chosen)
        self._apply_icons()

    def _ink(self) -> QColor:
        """What a glyph with no meaning of its own is drawn in."""
        return QColor(BRIGHT_TEXT if self._selected else DEFAULT_INK)

    def _apply_icons(self) -> None:
        ink = self._ink()
        self.trim_btn.setIcon(pencil_icon(color=QColor(ERROR) if not self._available else ink))
        self.delete_btn.setIcon(trash_icon(color=ink))
        if self._pass is not None:
            # Its own colour, except on a selected row: neither direction colour
            # clears AA contrast on the selection fill, so there the arrow alone
            # carries the direction.
            self._direction.setPixmap(
                icon_pixmap(
                    direction_arrow_icon(
                        self._pass.direction,
                        ICON_SM,
                        QColor(BRIGHT_TEXT) if self._selected else None,
                    ),
                    ICON_SM,
                    self.devicePixelRatio(),
                )
            )
        self._apply_cart_icon()

    def _apply_cart_icon(self) -> None:
        """In the cart, out of it, or not addable at all. Left alone under a tick."""
        if self._cart_ack.isActive():
            return
        if not self._available:
            self.cart_btn.setIcon(cart_icon(color=QColor(ERROR)))
            self.cart_btn.setToolTip(CART_UNLINKED_TOOLTIP)
            return
        self.cart_btn.setIcon(cart_icon(color=QColor(SUCCESS)) if self._in_cart else cart_icon(color=self._ink()))
        self.cart_btn.setToolTip(IN_CART_TOOLTIP if self._in_cart else MENU_ADD_TO_CART)

    def _on_cart_clicked(self) -> None:
        """Answer the click where it was made, then ask the page to do it.

        The count in the header is the far corner of the window, and a row that
        looks identical after a click reads as a click that missed. A tick for
        going in, a cross for coming back out. No answer at all when the file is
        missing: the page is about to refuse.
        """
        if self._pass is None:
            return
        if self._available:
            self.cart_btn.setIcon(close_icon() if self._in_cart else check_icon())
            self._cart_ack.start()
        self.add_to_cart_requested.emit(self.pass_id)

    def menu(self) -> QMenu:
        """The pass's actions, as the right click offers them.

        Built fresh each time rather than kept: what a pass allows depends on
        runs and on the cart, both of which move while the row sits there.
        """
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        # One entry that names the move it will make, rather than an "Add to
        # cart" greyed out on everything already in it.
        cart = menu.addAction(MENU_REMOVE_FROM_CART if self._in_cart else MENU_ADD_TO_CART)
        cart.triggered.connect(lambda *_: self.add_to_cart_requested.emit(self.pass_id))
        menu.addAction(MENU_RETRIM).triggered.connect(lambda *_: self.retrim_requested.emit(self.pass_id))
        menu.addAction(MENU_REASSIGN).triggered.connect(lambda *_: self.reassign_requested.emit(self.pass_id))
        # The one action with nowhere on the row to live: it leaves the page
        # altogether, which is not something a row's own buttons should look
        # like they do.
        open_transect = menu.addAction(MENU_OPEN_TRANSECT if self.transect_id else MENU_OPEN_TRANSECTS_PAGE)
        if not self.transect_id:
            open_transect.setToolTip(NO_TRANSECT_TOOLTIP)
        open_transect.triggered.connect(lambda *_: self.open_transect_requested.emit(self.transect_id))
        menu.addSeparator()
        delete = menu.addAction(MENU_DELETE)
        # A section with runs stands for something that happened. Letting it
        # vanish would leave those runs describing a swim nothing records.
        delete.setEnabled(self._run_count == 0)
        if self._run_count:
            delete.setToolTip(DELETE_BLOCKED_TOOLTIP)
        delete.triggered.connect(lambda *_: self.delete_requested.emit(self.pass_id))
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        if self._pass is not None:
            self.menu().exec(event.globalPos())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pass is not None:
            self.activated.emit(self.pass_id)
        super().mousePressEvent(event)


class SectionList(QScrollArea):
    """The passes of one clip, as rows, for the clip pane beside the list.

    The same rows the Videos list nests under each clip, in their compact form:
    the pane is a third of the page, and a pass should not be one thing here
    and another thing there.
    """

    activated = Signal(str)
    add_to_cart_requested = Signal(str)
    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    delete_requested = Signal(str)
    open_transect_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._body_layout = QVBoxLayout()
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addStretch(1)
        outer.addLayout(self._body_layout)
        # Inside the panel rather than swapped for it: the dark well is what
        # says a list belongs here, and a card that loses it when the list is
        # empty reads as a card with nothing missing.
        self.empty = EmptyState(EMPTY_TITLE, EMPTY_NOTE)
        outer.addWidget(self.empty, 1)
        self.setWidget(body)
        self._rows: dict[str, SectionRow] = {}
        self._order: list[str] = []
        self._selected: str | None = None

    def rows(self) -> dict[str, SectionRow]:
        return dict(self._rows)

    @property
    def selected(self) -> str | None:
        return self._selected

    def set_sections(
        self,
        entry: VideoLibraryEntry,
        transect_name: Callable[[Any], str | None] = lambda _id: None,
        *,
        in_cart: Callable[[str], bool] = lambda _pass_id: False,
        assets: Mapping[uuid.UUID, VideoAsset] | None = None,
    ) -> None:
        """Fill the pane from one clip, rebuilding only when the passes change.

        ``assets`` is every clip in the library, for resolving a pass that
        spans chapters onto the files behind it. Without it only this clip can
        be reached, and a chaptered pass gets no hover preview.
        """
        facts = section_facts(entry)
        known = {entry.video.id: entry.video} if assets is None else assets
        order = [str(pass_.id) for pass_, _, _ in facts]
        if order != self._order:
            self._rebuild(order)
        for pass_, status, run_count in facts:
            self._rows[str(pass_.id)].set_section(
                pass_,
                transect_name=transect_name(pass_.transect_id),
                status=status,
                run_count=run_count,
                in_cart=bool(in_cart(str(pass_.id))),
                available=entry.link_state != LINK_MISSING,
                preview=preview_points(pass_, known),
            )
        self.empty.setVisible(not facts)
        self._paint_selection()

    def set_selected(self, pass_id: str | None) -> None:
        self._selected = pass_id
        self._paint_selection()

    def _rebuild(self, order: list[str]) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Unparented as well as deleted: a deferred delete only runs on
                # the way back out to the event loop, and until then the old row
                # goes on painting over the one that replaced it.
                widget.setParent(None)
                widget.deleteLater()
        self._rows = {}
        for pass_id in order:
            row = SectionRow(compact=True)
            row.activated.connect(self._on_activated)
            row.add_to_cart_requested.connect(self.add_to_cart_requested)
            row.retrim_requested.connect(self.retrim_requested)
            row.reassign_requested.connect(self.reassign_requested)
            row.delete_requested.connect(self.delete_requested)
            row.open_transect_requested.connect(self.open_transect_requested)
            self._body_layout.addWidget(row)
            self._rows[pass_id] = row
        self._body_layout.addStretch(1)
        self._order = list(order)

    def _on_activated(self, pass_id: str) -> None:
        self.set_selected(pass_id)
        self.activated.emit(pass_id)

    def _paint_selection(self) -> None:
        for pass_id, row in self._rows.items():
            row.set_selected(pass_id == self._selected)


# The direction arrows the legend's hand-rolled sort headers already show.
SORT_ASC_GLYPH, SORT_DESC_GLYPH = "▲", "▼"

# Gravity's column is dots rather than values, so its heading has to say which
# way round the sort goes; the rest read plainly enough from their own titles.
_SORT_TOOLTIPS = {SORT_GRAVITY: "Sort by gravity: ascending brings the clips without one to the top."}


class _HeaderCell(QLabel):
    """One column heading. A sortable one takes a click and shows the hand."""

    clicked = Signal()

    def __init__(self, title: str, *, sortable: bool, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._sortable = sortable
        if sortable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setProperty("sortable", "true")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._sortable:
            self.clicked.emit()
        super().mousePressEvent(event)


class VideoListHeader(QWidget):
    """The clip columns over the list, sorting whichever one is clicked.

    Not a QHeaderView: the rows under it are widgets in a scroll area rather
    than cells of a view, so this restates ``QHeaderView::pass`` from
    ``core/theme.py`` out of the same tokens and must stay in step with it.
    Its cells hold the widths the rows do, which is what lines a heading up
    over its column. Passes keep their own grid and are not sorted from
    here: the columns describe clips only.
    """

    sort_changed = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoListHeader")
        # A bare QWidget ignores stylesheet backgrounds without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#videoListHeader {{ background-color: {CARD_BG};"
            f" border-bottom: 1px solid {BORDER}; }}"
            f" QWidget#videoListHeader QLabel {{ color: {TEXT_MUTED}; font-weight: 600; }}"
            f' QWidget#videoListHeader QLabel[sortable="true"]:hover'
            f" {{ color: {WINDOW_TEXT}; }}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, HEADER_PAD_V, SPACE_SM, HEADER_PAD_V)
        row.setSpacing(SPACE_SM)

        # Two blank spacers holding the disclosure and link columns, so Name
        # starts exactly where a row's file name does.
        for width in (DISCLOSURE_WIDTH, ICON_SM):
            spacer = QLabel()
            spacer.setFixedWidth(width)
            row.addWidget(spacer)

        self._cells: dict[str, _HeaderCell] = {}
        self._titles: dict[str, str] = {}
        self._add_cell(row, "Name", SORT_NAME, NAME_CHARS)
        self._add_cell(row, "Recorded", SORT_RECORDED, RECORDED_CHARS)
        self._add_cell(row, "Length", SORT_LENGTH, LENGTH_CHARS, right=True)
        self._add_cell(row, "Size", SORT_SIZE, SIZE_CHARS, right=True)

        self._add_cell(row, "Gravity", SORT_GRAVITY, GRAVITY_CHARS)
        row.addWidget(_HeaderCell("Passes", sortable=False), 1)

        self._column = DEFAULT_SORT_COLUMN
        self._descending = DEFAULT_SORT_DESCENDING
        self._relabel()

    def _add_cell(self, row: QHBoxLayout, title: str, column: str, chars: int, *, right: bool = False) -> None:
        cell = _HeaderCell(title, sortable=True)
        _fixed_width(cell, chars)
        if right:
            cell.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cell.setToolTip(_SORT_TOOLTIPS.get(column, f"Sort by {title.lower()} (click again to reverse)"))
        cell.clicked.connect(lambda column=column: self.sort_by(column))
        row.addWidget(cell)
        self._cells[column] = cell
        self._titles[column] = title

    def set_name_width(self, width: int) -> None:
        """Keep the Name heading over the column it names."""
        cell = self._cells.get(SORT_NAME)
        if cell is not None:
            cell.setFixedWidth(width)

    @property
    def column(self) -> str:
        return self._column

    @property
    def descending(self) -> bool:
        return self._descending

    def cell(self, column: str) -> QLabel:
        return self._cells[column]

    def sort_by(self, column: str) -> None:
        """What a click does: a new column sorts ascending, the current one reverses."""
        descending = not self._descending if column == self._column else False
        self.set_sort(column, descending)
        self.sort_changed.emit(column, descending)

    def set_sort(self, column: str, descending: bool) -> None:
        """Show a sort chosen elsewhere, without reporting it back as a click."""
        self._column = column
        self._descending = descending
        self._relabel()

    def _relabel(self) -> None:
        arrow = SORT_DESC_GLYPH if self._descending else SORT_ASC_GLYPH
        for column, cell in self._cells.items():
            title = self._titles[column]
            cell.setText(f"{title} {arrow}" if column == self._column else title)


# What a refresh compares to decide whether the list has to be rebuilt: each
# group, its clips, whether each clip is open, and the sections under it.
_GroupShape = tuple[str, tuple[tuple[str, bool, tuple[str, ...]], ...]]


class VideoLibraryList(QScrollArea):
    """Every imported clip, under the date it was shot, over the passes cut from it."""

    play_requested = Signal(str)
    reveal_requested = Signal(str)
    new_section_requested = Signal(str)
    activated = Signal(str)
    span_clicked = Signal(str)
    hide_requested = Signal(str)
    delete_unused_requested = Signal(str)
    delete_requested = Signal(str)
    archive_requested = Signal(str)
    section_activated = Signal(str)
    section_add_to_cart = Signal(str)
    section_retrim = Signal(str)
    section_reassign = Signal(str)
    section_delete = Signal(str)
    section_open_transect = Signal(str)
    # Which clips are picked has changed. What acts on a set of clips listens
    # here, so a scan that takes a row away cannot leave an action aimed at it.
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        # Rows touch. A day's clips read as one block, and the date headers are
        # what separates them; a gap between every row bought nothing and cost a
        # row's worth of footage every eight rows.
        self._body_layout.setSpacing(0)
        self.setWidget(body)
        self._body = body
        self._shape: list[_GroupShape] = []
        self._groups: list[DateGroup] = []
        self._rows: dict[str, VideoRow] = {}
        # A pass can span several clips, and it gets a row under each of them,
        # so one pass id owns a list rather than a widget. Keyed by the widget
        # alone it was the last chapter's row that answered to the id and every
        # earlier one that was left blank, with no pass to act on.
        self._sections: dict[str, list[SectionRow]] = {}
        self._sections_by_video: dict[str, list[SectionRow]] = {}
        self._expanded: set[str] = set()
        self._name_width = 0
        self._archiving = False
        self._header: VideoListHeader | None = None
        self._selected: str | None = None
        self._selected_section: str | None = None
        # Every picked clip, the row a shift range runs from, and the set the
        # last ``selection_changed`` reported.
        self._selection: set[str] = set()
        self._anchor: str | None = None
        self._reported: set[str] = set()
        # Outlives every rebuild, so the one line telling the user the list takes
        # a drop is not something a refresh can take away.
        self.drop_hint = muted_label(DROP_HINT)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(self.drop_hint)

    @property
    def selected(self) -> str | None:
        return self._selected

    @property
    def selected_section(self) -> str | None:
        return self._selected_section

    def selection(self) -> set[str]:
        """Every picked clip, which is the one clip of a plain click."""
        return set(self._selection)

    def rows(self) -> dict[str, VideoRow]:
        return dict(self._rows)

    def sections(self) -> dict[str, SectionRow]:
        """One row per pass: the first, where a pass spans several clips."""
        return {pass_id: rows[0] for pass_id, rows in self._sections.items() if rows}

    def follow_header(self, header: VideoListHeader) -> None:
        """Keep the header's Name column the same width as the rows' own."""
        self._header = header
        self._apply_name_width()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._apply_name_width()

    def set_server_connected(self, connected: bool) -> None:
        """Show or hide the archive button on every clip row, and its pass rows' gap.

        Both together, always: the clip and pass strips are aligned from the
        constants the rows are built from, so a button on one and not the other
        pulls the two out of step.
        """
        if connected == self._archiving:
            return
        self._archiving = connected
        for row in self._rows.values():
            row.archive_btn.setVisible(connected)
        for rows in self._sections_by_video.values():
            for section in rows:
                section.set_server_connected(connected)
        self._name_width = -1
        self._apply_name_width()

    def _apply_name_width(self) -> None:
        """Give the clip name its share of the row, and the strip the remainder.

        Pushed to every live row rather than left to layout stretch, which splits
        slack equally and so handed the strip the width a wider window bought.
        """
        available = self.viewport().width()
        if available <= 0:
            return
        width = clip_name_width(
            available, self.fontMetrics().averageCharWidth(), archiving=self._archiving
        )
        if width == self._name_width:
            return
        self._name_width = width
        if self._header is not None:
            self._header.set_name_width(width)
        for row in self._rows.values():
            row.set_name_width(width)
        for rows in self._sections_by_video.values():
            for section in rows:
                section.set_name_width(width)

    def set_groups(
        self,
        groups: Sequence[DateGroup],
        transect_name: Callable[[Any], str | None] = lambda _id: None,
        *,
        in_cart: Callable[[str], bool] = lambda _pass_id: False,
        hidden: Callable[[str], bool] = lambda _video_id: False,
    ) -> None:
        """Fill the list. Rebuilt only when its shape changes.

        The page refreshes this on every scan, and a rebuild under the cursor
        loses the row being clicked and scrolls the list back to the top. The
        shape carries the passes and which clips are open as well as the
        clips, or a pass cut a moment ago would never get a row.
        """
        self._groups = list(groups)
        shape = self._shape_of(self._groups)
        if shape != self._shape:
            self._rebuild(self._groups)
            self._shape = shape
        self._prune_selection()
        # Built once for the whole list: a section spanning chapters needs the
        # clips either side of the one its row sits under.
        assets = {entry.video.id: entry.video for group in self._groups for entry in group.entries}
        for group in self._groups:
            for entry in group.entries:
                video_id = str(entry.video.id)
                row = self._rows[video_id]
                row.set_entry(entry, transect_name, hidden=bool(hidden(video_id)))
                # A rebuild makes fresh rows, which start at the char-count width.
                if self._name_width:
                    row.set_name_width(self._name_width)
                row.set_expanded(video_id in self._expanded)
                # Filled through this clip's own rows rather than by pass id: a
                # pass spanning two clips has a row under each, and each row
                # answers for the chapter it sits under.
                for (pass_, status, run_count), section in zip(
                    section_facts(entry),
                    self._sections_by_video.get(video_id, []),
                    strict=False,
                ):
                    section.set_section(
                        pass_,
                        transect_name=transect_name(pass_.transect_id),
                        status=status,
                        run_count=run_count,
                        in_cart=bool(in_cart(str(pass_.id))),
                        available=entry.link_state != LINK_MISSING,
                        preview=preview_points(pass_, assets),
                        duration_s=entry.video.duration_s,
                    )
        self._apply_expansion()
        self._apply_selection()

    def expand(self, video_id: str) -> None:
        """Open a clip from outside, as clicking its chevron would."""
        row = self._rows.get(video_id)
        if row is not None:
            row.set_expanded(True)
        self._set_expanded(video_id, True)

    def set_selected(self, video_id: str | None) -> None:
        """Pick one clip and no other, which is what a plain click does."""
        self._selected = video_id
        self._anchor = video_id
        self._selection = {video_id} if video_id else set()
        # One thing at a time: a clip and a section describe different levels,
        # and two highlights would leave the detail pane's subject ambiguous.
        self._selected_section = None
        self._apply_selection()

    def set_selected_section(self, pass_id: str | None) -> None:
        """Pick one pass, and let go of every clip.

        A picked pass is a level down from a picked clip, so a set of clips
        left standing under it would be a target the user cannot see.
        """
        self._selected_section = pass_id
        self._selected = None
        self._anchor = None
        self._selection = set()
        self._apply_selection()

    def reveal(self, pass_id: str) -> None:
        """Scroll a section into view.

        Arriving from another page lands on a list of a season's clips, and a
        highlight below the fold is no answer at all. The clip has to be open
        first, which is _select_section's job, or the row is not in the layout
        yet and there is nothing to scroll to.
        """
        rows = self._sections.get(pass_id)
        if rows:
            self.ensureWidgetVisible(rows[0], 0, SPACE_MD)

    def _shape_of(self, groups: Sequence[DateGroup]) -> list[_GroupShape]:
        return [
            (
                group.key,
                tuple(
                    (
                        str(entry.video.id),
                        str(entry.video.id) in self._expanded,
                        tuple(str(pass_.id) for pass_, _, _ in section_facts(entry)),
                    )
                    for entry in group.entries
                ),
            )
            for group in groups
        ]

    def _rebuild(self, groups: Iterable[DateGroup]) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.drop_hint:
                widget.deleteLater()
        self._rows = {}
        self._sections = {}
        self._sections_by_video = {}
        for group in groups:
            self._body_layout.addWidget(SectionHeader(group.title))
            for entry in group.entries:
                self._add_clip(entry)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(self.drop_hint)
        self._prune_selection()

    def _add_clip(self, entry: VideoLibraryEntry) -> None:
        video_id = str(entry.video.id)
        row = VideoRow()
        row.play_requested.connect(self.play_requested)
        row.reveal_requested.connect(self.reveal_requested)
        row.new_section_requested.connect(self.new_section_requested)
        row.span_clicked.connect(self.span_clicked)
        row.hide_requested.connect(self.hide_requested)
        row.delete_unused_requested.connect(self.delete_unused_requested)
        row.delete_requested.connect(self.delete_requested)
        row.archive_requested.connect(self.archive_requested)
        row.activated.connect(self._on_activated)
        row.clicked.connect(self._on_clicked)
        row.expand_toggled.connect(self._set_expanded)
        row.archive_btn.setVisible(self._archiving)
        self._body_layout.addWidget(row)
        self._rows[video_id] = row

        sections = []
        for pass_, _status, _run_count in section_facts(entry):
            section = SectionRow()
            section.activated.connect(self._on_section_activated)
            section.add_to_cart_requested.connect(self.section_add_to_cart)
            section.retrim_requested.connect(self.section_retrim)
            section.reassign_requested.connect(self.section_reassign)
            section.delete_requested.connect(self.section_delete)
            section.open_transect_requested.connect(self.section_open_transect)
            section.set_server_connected(self._archiving)
            self._body_layout.addWidget(section)
            self._sections.setdefault(str(pass_.id), []).append(section)
            sections.append(section)
        self._sections_by_video[video_id] = sections

    def _set_expanded(self, video_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded.add(video_id)
        else:
            self._expanded.discard(video_id)
        self._apply_expansion()
        # The shape carries which clips are open, so it is recorded here too.
        # Left alone, the next scan would read the list as changed and rebuild
        # it under the cursor.
        self._shape = self._shape_of(self._groups)

    def _apply_expansion(self) -> None:
        for video_id, sections in self._sections_by_video.items():
            visible = video_id in self._expanded
            for section in sections:
                section.setVisible(visible)

    def _on_activated(self, video_id: str) -> None:
        self.set_selected(video_id)
        self.activated.emit(video_id)

    def _on_clicked(self, video_id: str, modifiers: Qt.KeyboardModifier) -> None:
        """Read a click on a clip row as what it does to the set of picked clips.

        Ctrl adds one clip or takes it back out, shift takes everything between
        the last picked row and this one, and a bare click starts again from
        here. The bare case is left to ``_on_activated``, which the same press
        raises, so picking a clip and describing it stay one step.

        A shift click with nothing picked yet has no range to take, so it picks
        the one row, which is where the next range will run from.
        """
        if not modifiers & PICK_MODIFIERS:
            return
        if modifiers & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None:
            self._selection = self._range(self._anchor, video_id)
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            self._selection ^= {video_id}
            self._anchor = video_id
        else:
            self._selection = {video_id}
            self._anchor = video_id
        self._apply_selection()

    def _range(self, anchor: str, video_id: str) -> set[str]:
        """The clips from ``anchor`` to ``video_id`` in the order they are listed.

        Over the clips alone: a pass row sits inside the range on screen and
        is a level down from what is being picked, so it is not part of one.
        """
        order = [str(entry.video.id) for group in self._groups for entry in group.entries]
        if anchor not in order or video_id not in order:
            return {video_id}
        first, last = sorted((order.index(anchor), order.index(video_id)))
        return set(order[first : last + 1])

    def _on_section_activated(self, pass_id: str) -> None:
        self.set_selected_section(pass_id)
        self.section_activated.emit(pass_id)

    def _prune_selection(self) -> None:
        """Drop picked clips that no longer have a row.

        Rows are destroyed and rebuilt whenever the list's shape changes, which
        a background scan does every time it finds a clip or a pass. A set
        left holding ids nobody can see is a delete aimed at nothing.
        """
        self._selection &= set(self._rows)
        if self._anchor is not None and self._anchor not in self._rows:
            self._anchor = None

    def _apply_selection(self) -> None:
        """Paint what is picked, and say so when the set of clips has moved."""
        for video_id, row in self._rows.items():
            row.set_selected(video_id in self._selection)
        for pass_id, sections in self._sections.items():
            for section in sections:
                section.set_selected(pass_id == self._selected_section)
        if self._selection != self._reported:
            self._reported = set(self._selection)
            self.selection_changed.emit()
