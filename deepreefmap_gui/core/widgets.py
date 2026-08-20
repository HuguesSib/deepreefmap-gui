"""Small shared building blocks: section cards, empty states, tables, dialogs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BORDER,
    BORDER_STRONG,
    BUTTON,
    CARD_BG,
    CONTROL_HEIGHT,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    ERROR,
    FONT_SM,
    GROOVE,
    GUTTER,
    IDLE,
    ON_ACCENT,
    PRIMARY,
    RADIUS,
    RADIUS_SM,
    READING_WIDTH,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    TABLE_ROW_HEIGHT,
    TEXT_DIM,
    TEXT_MUTED,
    UPDATE,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
    WARNING,
    WEIGHT_SEMIBOLD,
    WINDOW_TEXT,
    bar_qss,
)
from deepreefmap_gui.survey import statuses
from deepreefmap_gui.survey.models.transect_pass import direction_text


def section_title_font(label: QLabel) -> None:
    """The one weight a section title is set in."""
    font = label.font()
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    label.setStyleSheet(f"color: {TEXT_MUTED};")


def tone_label(text: str = "", tone: str = "muted", parent: QWidget | None = None) -> QLabel:
    """A label carrying one of the app's text roles rather than a colour.

    The tone is a QSS property (see the ``QLabel[tone=...]`` rules in theme.py),
    so the colour lives with the palette. Prefer this to
    ``setStyleSheet(f"color: {...}")``, which opts the label out of the theme.
    """
    label = QLabel(text, parent)
    label.setProperty("tone", tone)
    return label


def muted_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    """A caption or field label beside the thing it describes."""
    return tone_label(text, "muted", parent)


def secondary_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    """A readout: a shade brighter than a caption, dimmer than body text."""
    return tone_label(text, "secondary", parent)


class SectionHeader(QLabel):
    """A section title, at the app's one title weight and colour."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        section_title_font(self)


def section_card(title: str = "", *, spacing: int = SPACE_SM) -> tuple[QWidget, QVBoxLayout]:
    """A titled panel that sits above the shell, and the layout to fill it.

    Returns the card and its content layout, so callers add their widgets to the
    layout without needing to know how the title is built.
    """
    card = QWidget()
    card.setObjectName("sectionCard")
    # Object-name scoped so the fill lands on the card only: an unscoped rule
    # would cascade into every child widget's background too. Views inside a
    # card drop their own border, which would otherwise double the card's.
    card.setStyleSheet(
        f"QWidget#sectionCard {{ background-color: {CARD_BG};"
        f" border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
        " QWidget#sectionCard QAbstractItemView { border: none; }"
    )
    outer = QVBoxLayout(card)
    outer.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_MD)
    outer.setSpacing(spacing)
    if title:
        outer.addWidget(SectionHeader(title))
    return card, outer


def section_column(title: str = "", *, spacing: int = SPACE_SM) -> tuple[QWidget, QVBoxLayout]:
    """A titled pane that *is* the page, rather than a card sitting on one.

    The same shape as `section_card` without the fill, the border and the
    margins. An item view already draws itself as a recessed BASE panel with its
    own hairline, so wrapping one in a card draws the frame twice and spends
    24px of margin restating it -- which is why `section_card` has to suppress
    the inner border to look right at all.

    Use this for the pane holding a page's primary content, and `section_card`
    for a pane describing one selected thing. A page whose every pane is a card
    has no figure and no ground: the raised fill stops reading as "this is
    raised" and just makes the window lighter.
    """
    column = QWidget()
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    if title:
        layout.addWidget(SectionHeader(title))
    return column, layout


# How much of a centred page's width the column claims before its cap stops it.
# Equal stretch either side would split the window three ways and leave the
# column at a third of the space rather than at its cap, so the column asks for
# far more than the margins and the cap is what actually decides its width.
_COLUMN_STRETCH = 20


def centred_column(
    max_width: int = READING_WIDTH, *, spacing: int = GUTTER
) -> tuple[QWidget, QVBoxLayout]:
    """A page whose content is capped at a readable measure and centred on it.

    Returns the page and the layout its content goes in. Content pinned to the
    left of a wide window leaves a screen's worth of empty space beside it, and
    the eye has to track the whole way across to get from a sentence to the
    control that acts on it.
    """
    page = QWidget()
    row = QHBoxLayout(page)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    column = QWidget()
    column.setMaximumWidth(max_width)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    row.addStretch(1)
    row.addWidget(column, _COLUMN_STRETCH)
    row.addStretch(1)
    return page, layout


def lent_panel_home(parent: QWidget) -> tuple[QWidget, QWidget, QVBoxLayout]:
    """A permanent holder for a panel that lives somewhere else, and the panel.

    Two widgets rather than one because the page is lent to a destination and
    handed back; the home is the empty holder it returns to. Parented and hidden,
    because a parentless widget made visible maps itself as a top-level window.
    """
    # No layout-level AlignTop: it shrinks the layout to its size hint and wraps
    # word-wrapped labels narrow. Panels that need top alignment end with a
    # stretch of their own.
    home = QWidget(parent)
    home.setVisible(False)
    home_layout = QVBoxLayout(home)
    home_layout.setContentsMargins(0, 0, 0, 0)
    page = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(0, 0, 0, 0)
    home_layout.addWidget(page)
    return home, page, page_layout


def segmented_qss(*, first: bool, last: bool, alert: str = "") -> str:
    """One button of a joined segmented control, filled when it is the live one.

    The segments share a seam: only the outermost corners round, and every
    segment after the first drops its left border so the row reads as one
    control rather than as several loose pills.

    `alert` tints the segment in the colour given, for a view that has something
    waiting on it. Only while it is unselected: once it is the live segment the
    view itself says what is waiting, and a second colour on the control would be
    competing with the accent that marks which segment is open.
    """
    corners = []
    if first:
        corners.append(f"border-top-left-radius: {RADIUS}px;")
        corners.append(f"border-bottom-left-radius: {RADIUS}px;")
    else:
        corners.append("border-left: none;")
    if last:
        corners.append(f"border-top-right-radius: {RADIUS}px;")
        corners.append(f"border-bottom-right-radius: {RADIUS}px;")
    rest = (
        f" background: {tinted(alert, PILL_TINT_ALPHA)}; color: {alert};"
        f" font-weight: {WEIGHT_SEMIBOLD};"
        if alert
        else f" background: {BUTTON}; color: {WINDOW_TEXT};"
    )
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: 0; {' '.join(corners)}"
        f" padding: {SPACE_XS}px {SPACE_MD}px; min-height: {CONTROL_HEIGHT - 2 * SPACE_XS}px;"
        f"{rest} }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; }}"
        f" QToolButton:checked {{ background: {PRIMARY}; color: {ON_ACCENT};"
        f" font-weight: {WEIGHT_SEMIBOLD}; }}"
        f" QToolButton:disabled {{ color: {TEXT_DIM}; background: transparent; }}"
    )


def fact_link(text: str, href: str) -> str:
    """A fact that is also the way to change it.

    Values are labels rather than controls, so a fact that has an action behind
    it says so as a link: the reader follows the same word they were reading
    rather than hunting for a button that repeats it.
    """
    return f'<a href="{href}" style="color: {PRIMARY}; text-decoration: none;">{text}</a>'


def _plain(value: str) -> str:
    """Whatever a value says, without the markup a link is written in."""
    return re.sub(r"<[^>]+>", "", value)


class KeyValueList(QWidget):
    """Facts about one thing, as aligned label/value rows.

    A grid rather than one rich-text paragraph with bold run-in labels: the
    values line up, so a run's numbers can be compared down the column instead of
    hunted for inside a sentence.
    """

    # Which fact was clicked, by the href fact_link gave it.
    link_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, wrap: bool = True) -> None:
        super().__init__(parent)
        self._wrap = wrap
        self._keys: list[str] = []
        self._values: list[QLabel] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(SPACE_MD)
        self._grid.setVerticalSpacing(SPACE_XS)
        self._grid.setColumnStretch(1, 1)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        """Fill the list. Rebuilt only when the *keys* change.

        A caller showing a fixed set of fields calls this once per selection with
        the same keys every time, and rewriting the values in place means the
        layout never reflows. Rebuilding unconditionally is what let a pane grow
        and shrink under the cursor as the table was arrowed through.
        """
        if [key for key, _ in rows] == self._keys:
            for (_, value), label in zip(rows, self._values, strict=True):
                self._fill(label, value)
            return

        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._keys = [key for key, _ in rows]
        self._values = []
        for row, (key, value) in enumerate(rows):
            name = QLabel(key)
            name.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
            name.setAlignment(
                Qt.AlignmentFlag.AlignRight
                | (Qt.AlignmentFlag.AlignTop if self._wrap else Qt.AlignmentFlag.AlignVCenter)
            )
            self._grid.addWidget(name, row, 0)
            shown = secondary_label()
            shown.setWordWrap(self._wrap)
            if not self._wrap:
                # Ignored, not Preferred: a long value's own width hint would
                # otherwise widen the whole pane to fit it and shove the table
                # aside, which is the same reason the ortho strip is Ignored.
                shown.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            shown.linkActivated.connect(self.link_activated)
            self._fill(shown, value)
            self._grid.addWidget(shown, row, 1)
            self._values.append(shown)

    def _fill(self, label: QLabel, value: str) -> None:
        label.setText(value)
        # The tooltip carries what a one-line row cannot show in full, in the
        # words the row says rather than the markup a link is written in.
        label.setToolTip("" if self._wrap else _plain(value))

    def clear(self) -> None:
        self.set_rows([])


# The map viewport as a filter. Shared so the Plan list and the Browse run list
# offer the same two words for the same idea.
SCOPE_FILTERS = (
    ("in_view", "In view"),
    ("all", "All transects"),
)


def utility_button_qss(right_padding: int = SPACE_SM) -> str:
    """A header utility control: bordered and quiet rather than filled.

    Distinct on purpose from the workspace pills beside it. Those say where you
    are working; a utility is somewhere you visit and leave, or a panel you
    toggle. ``right_padding`` reserves room for anything painted after the
    label, so the text does not shift as it comes and goes.
    """
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;"
        f" background: {BUTTON}; color: {WINDOW_TEXT};"
        f" padding: {SPACE_XS}px {right_padding}px {SPACE_XS}px {SPACE_SM}px;"
        f" min-height: {CONTROL_HEIGHT}px; }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; }}"
        f" QToolButton:checked {{ background: {PRIMARY}; color: {ON_ACCENT};"
        f" font-weight: {WEIGHT_SEMIBOLD}; border-color: transparent; }}"
    )


# How strongly a chip tints its own background, out of 255. Low because the text
# on it is the same colour as the fill; test_design_system.py holds the floor.
PILL_TINT_ALPHA = 36
PILL_PROGRESS_ALPHA = 96
# The selected filter chip's outline.
PILL_BORDER_ALPHA = 110


def tinted(colour: str, alpha: int) -> str:
    """``colour`` as an rgba() string, so a stylesheet keeps the theme token it
    came from instead of carrying a second, transparent hex."""
    c = QColor(colour)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


# Shorter than CONTROL_HEIGHT: a chip is a label you may be able to press, not a
# control in a form, and at the full height a row of them reads as buttons.
CHIP_HEIGHT = CONTROL_HEIGHT - 2 * SPACE_XS


def chip_qss(colour: str, *, interactive: bool) -> str:
    """One chip: an outcome, or the filter that selects it.

    A pill tinted from its own ink, never filled solid: a solid fill is what a
    pressed button looks like, and both sit on the same screens.
    """
    shape = (
        f" border-radius: {CHIP_HEIGHT // 2}px; padding: 0px {SPACE_MD}px;"
        f" min-height: {CHIP_HEIGHT}px;"
    )
    lit = (
        f" background: {tinted(colour, PILL_TINT_ALPHA)}; color: {colour};"
        f" font-weight: {WEIGHT_SEMIBOLD};"
    )
    if not interactive:
        return f"QLabel {{ border: 1px solid transparent;{shape}{lit} }}"
    # Only the chip in force carries its outcome's colour.
    return (
        f"QToolButton {{ border: 1px solid {BORDER};{shape}"
        f" background: transparent; color: {TEXT_MUTED}; }}"
        f" QToolButton:hover {{ color: {WINDOW_TEXT}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; color: {WINDOW_TEXT}; }}"
        f" QToolButton:checked {{ border-color: {tinted(colour, PILL_BORDER_ALPHA)};{lit} }}"
    )


class StatusChip(QLabel):
    """An outcome, in the shape and colour the tables paint it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status("", TEXT_MUTED)

    def set_status(self, text: str, colour: str = TEXT_MUTED) -> None:
        self.setText(text)
        self.setStyleSheet(chip_qss(colour, interactive=False))
        self.setVisible(bool(text))


class FilterChips(QWidget):
    """A row of exclusive filters, each carrying its own count.

    The count is the point: a chip reading "Failed 3" answers the question
    before it is clicked, and a chip reading "Failed 0" says not to bother. Empty
    chips stay visible rather than disappearing, so the row does not reflow under
    the cursor as a batch runs.
    """

    changed = Signal(str)

    def __init__(
        self,
        options: Sequence[tuple[str, ...]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)
        self._labels = {key: title for key, title, *_ in options}
        self._buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, title, *rest in options:
            button = QToolButton()
            button.setText(title)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A filter selecting an outcome takes that outcome's colour once it
            # is in force. Filters that select anything else take the accent.
            button.setStyleSheet(chip_qss(rest[0] if rest else PRIMARY, interactive=True))
            button.toggled.connect(
                lambda checked, k=key: self.changed.emit(k) if checked else None
            )
            group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = button
        first = options[0][0]
        self._buttons[first].setChecked(True)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, button in self._buttons.items():
            count = counts.get(key)
            button.setText(
                self._labels[key] if count is None else f"{self._labels[key]}  {count}"
            )

    def current(self) -> str:
        return next(k for k, b in self._buttons.items() if b.isChecked())

    def set_current(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)


class EmptyState(QWidget):
    """What an empty pane says: what is missing, and what fills it."""

    def __init__(self, message: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        layout.setSpacing(SPACE_XS)
        layout.addStretch(1)

        self._message = QLabel(message)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._message)

        self._hint = QLabel(hint)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {TEXT_DIM};")
        # Parented before shown: setVisible on a parentless widget maps it as a
        # top-level window, which flashes an empty titlebar box on screen.
        layout.addWidget(self._hint)
        self._hint.setVisible(bool(hint))

        layout.addStretch(1)

    def set_text(self, message: str, hint: str = "") -> None:
        self._message.setText(message)
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))


def warning_banner_qss() -> str:
    """The app's one warning surface, for a label that is its own banner.

    Three panels carried a character-for-character copy of this, each spelling
    its own 6px padding and 3px radius -- a radius the theme does not have.
    """
    return (
        f"background-color: {WARN_BG}; color: {WARN_TEXT};"
        f" border: 1px solid {WARN_BORDER};"
        f" padding: {SPACE_SM}px; border-radius: {RADIUS_SM}px;"
    )


class NotReadyStrip(QWidget):
    """The one thing blocking a page, next to the button that goes and fixes it.

    Directions the page cannot follow ("switch modes, then find the tab") are
    not an action, so the destination is a button. Hidden whenever nothing
    blocks, which is why it sits above the content rather than inside it.
    """

    action_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notReadyStrip")
        self.setStyleSheet(
            f"QWidget#notReadyStrip {{ background-color: {WARN_BG};"
            f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        self._reason = QLabel("")
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color: {WARN_TEXT};")
        row.addWidget(self._reason, 1)

        self._action = QPushButton("")
        self._action.clicked.connect(self.action_clicked)
        row.addWidget(self._action)

        self.setVisible(False)

    def show_blocker(self, reason: str, action: str = "") -> None:
        """Name the blocker. An empty action means it is fixed on this page."""
        self._reason.setText(reason)
        self._action.setText(action)
        self._action.setVisible(bool(action))
        self.setVisible(bool(reason))

    def clear(self) -> None:
        self._reason.setText("")
        self.setVisible(False)


class NoticeStrip(QWidget):
    """News about a page, next to the one action that follows from it.

    NotReadyStrip's shape for something that is not a blocker: an update
    waiting, a result ready. Tinted from its own ink rather than filled, so it
    reads as a note on the page rather than as a warning about it.
    """

    action_clicked = Signal()

    def __init__(self, colour: str = UPDATE, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("noticeStrip")
        # A bare QWidget paints no stylesheet background without this, so the
        # tint and the border it is drawn against never appear.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#noticeStrip {{ background-color: {tinted(colour, PILL_TINT_ALPHA)};"
            f" border: 1px solid {tinted(colour, PILL_BORDER_ALPHA)};"
            f" border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color: {colour}; font-weight: {WEIGHT_SEMIBOLD};")
        row.addWidget(self._message, 1)

        self._action = QPushButton("")
        self._action.clicked.connect(self.action_clicked)
        row.addWidget(self._action)

        self.setVisible(False)

    def show_notice(self, message: str, action: str = "") -> None:
        """Say the news. An empty message hides the strip."""
        self._message.setText(message)
        self._action.setText(action)
        self._action.setVisible(bool(action))
        self.setVisible(bool(message))

    def clear(self) -> None:
        self._message.setText("")
        self.setVisible(False)


def ok_cancel_row(dialog: QDialog) -> QDialogButtonBox:
    """The Ok/Cancel pair at the foot of a dialog, wired to accept and reject.

    Three dialogs carried a character-for-character copy of this. The box is
    returned rather than added to a layout, because the caller places it and one
    caller keeps the Ok button to gate it on a selection.
    """
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


def confirm(parent: QWidget | None, title: str, text: str) -> bool:
    """Ask a yes/no question, with No as the button Enter picks.

    Every prompt in the app guards something the answer cannot take back: a
    delete, a trim written over every other pass of a transect, a batch started
    on a disk that will not hold it. Most sites spelled that default out by
    hand; the delete prompt took Qt's, which puts the focus ring on Yes.
    """
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def configure_table(
    table: QTableWidget, headers: Sequence[str], *, alternating: bool = True
) -> None:
    """The defaults every table in the app reads under: whole rows, no edit
    triggers, the theme's alternate fill.

    ``alternating`` is for the one table whose cells are widgets: they paint
    their own background, so the stripe would stop halfway across the row.
    """
    table.setHorizontalHeaderLabels(list(headers))
    # Centred, everywhere. A heading is the name of a column, not the first
    # value in it, and left-aligned headings over cells that hold buttons read
    # as though they belong to the cell to their left.
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
    )
    table.verticalHeader().setVisible(False)
    # Row height lives here, not in the QSS item padding: QTableView sizes its
    # rows off defaultSectionSize and ignores that padding, so the stylesheet
    # alone changed how a row looked without fitting another one on screen.
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
    table.setShowGrid(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(alternating)


# --- Column widths ---------------------------------------------------------

# No column narrower than this, whatever its content measures.
MIN_SECTION_WIDTH = 64


@dataclass(frozen=True)
class ColumnSpec:
    """How one table divides its viewport between its columns.

    ``fixed`` are columns whose width is a property of what they hold rather
    than of the window: a status pill, a timestamp, a formatted number.
    ``weights`` are the identifying columns, which share what is left over in
    proportion, each held above its entry in ``minimums``. ``optional`` are
    secondary columns, in the order they earn their width, admitted while it is
    still spare and hidden when it is not.
    """

    fixed: Mapping[int, int]
    weights: Mapping[int, int]
    minimums: Mapping[int, int]
    optional: Sequence[tuple[int, int]] = ()


def fitted_column_widths(available: int, spec: ColumnSpec) -> dict[int, int]:
    """How a viewport of ``available`` px divides between the columns it can hold.

    A share that falls under a column's floor is clamped to it and the
    *remainder* is re-divided among the columns still flexing, rather than every
    column being clamped independently: doing that over-spends the viewport by
    the size of each bump and puts back the scrollbar this exists to avoid.

    Columns left out are absent from the result, not zero-width. On a window too
    narrow to hold even the mandatory ones at their floors the floors win and the
    table scrolls, because a column shrunk past reading is not a column.
    """
    widths = dict(spec.fixed)
    spent = sum(spec.fixed.values()) + sum(spec.minimums.values())
    for column, width in spec.optional:
        if spent + width > available:
            break
        widths[column] = width
        spent += width
    slack = max(0, available - sum(widths.values()))
    flexing = dict(spec.weights)
    while flexing:
        weight_total = sum(flexing.values())
        clamped = next(
            (
                column
                for column, weight in flexing.items()
                if slack * weight // weight_total < spec.minimums[column]
            ),
            None,
        )
        if clamped is None:
            for column, weight in flexing.items():
                widths[column] = slack * weight // weight_total
            break
        widths[clamped] = spec.minimums[clamped]
        slack = max(0, slack - spec.minimums[clamped])
        del flexing[clamped]
    return widths


class ColumnSizer(QObject):
    """Keeps a view's columns fitted to its viewport, and out of the user's way.

    Every column is ``Interactive``, so any of them can be dragged. A column the
    user drags is pinned: later refits leave it at the width it was given and
    re-divide what is left among the rest. ``settings_key`` persists those
    choices by heading text, so a changed column set drops what no longer
    applies.
    """

    def __init__(
        self,
        view: QTableWidget | QTreeWidget,
        spec: ColumnSpec,
        *,
        settings_key: str | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__(view)
        self._view = view
        self._spec = spec
        self._settings_key = settings_key
        self._settings = settings
        self._pinned: dict[int, int] = {}
        self._applying = False

        header = self._header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(MIN_SECTION_WIDTH)
        for column in range(self._column_count()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        # A widened column has to be reachable, so the scrollbar is offered.
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_menu)
        header.sectionResized.connect(self._on_section_resized)
        # Coalesced and deferred: a filter on the view runs before
        # QAbstractScrollArea lays the viewport out, so refitting inline would
        # read the width the viewport is about to stop having.
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(0)
        self._refit_timer.timeout.connect(self.apply)
        view.installEventFilter(self)
        self._restore()
        self.apply()

    def _header(self) -> QHeaderView:
        view = self._view
        return view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()

    def _column_count(self) -> int:
        view = self._view
        return view.columnCount() if hasattr(view, "columnCount") else 0

    def _heading(self, column: int) -> str:
        model = self._view.model()
        value = model.headerData(column, Qt.Orientation.Horizontal) if model else None
        return str(value) if value else f"column{column}"

    def apply(self) -> None:
        """Refit every column that is not pinned to the viewport's width."""
        # Resizing sections can lay the viewport out again, which arrives back
        # here through the filter.
        if self._applying:
            return
        view = self._view
        available = view.viewport().width()
        if available <= 0:
            return
        reserved = sum(self._pinned.values())
        spec = self._spec
        fixed = {c: w for c, w in spec.fixed.items() if c not in self._pinned}
        weights = {c: w for c, w in spec.weights.items() if c not in self._pinned}
        optional = tuple((c, w) for c, w in spec.optional if c not in self._pinned)
        minimums = {c: spec.minimums[c] for c in weights}
        widths = fitted_column_widths(
            max(0, available - reserved),
            ColumnSpec(
                fixed=fixed,
                weights=weights,
                minimums=minimums,
                optional=optional,
            ),
        )
        widths.update(self._pinned)
        header = self._header()
        self._applying = True
        try:
            for column, _width in spec.optional:
                self._set_hidden(column, column not in widths)
            for column, width in widths.items():
                header.resizeSection(column, width)
        finally:
            self._applying = False

    def _set_hidden(self, column: int, hidden: bool) -> None:
        self._view.setColumnHidden(column, hidden)

    def _on_section_resized(self, column: int, _old: int, new: int) -> None:
        if self._applying or new <= 0:
            return
        self._pinned[column] = new
        self._store()

    def _on_header_menu(self, pos) -> None:
        menu = QMenu(self._view)
        menu.addAction("Reset column widths", self.reset)
        menu.exec(self._header().mapToGlobal(pos))

    def widen_fixed(self, column: int, width: int) -> None:
        """Raise a fixed column's width to ``width``, for a cell that measured itself.

        Only ever grows, so a table of rows in different states does not shuffle
        as they repaint, and a column the user has dragged is left alone.
        """
        if column in self._pinned or self._spec.fixed.get(column, 0) >= width:
            return
        self._spec = replace(self._spec, fixed={**self._spec.fixed, column: width})
        self.apply()

    def reset(self) -> None:
        """Forget every dragged width and go back to fitting the viewport."""
        self._pinned.clear()
        self._store()
        self.apply()

    def _store(self) -> None:
        if self._settings_key is None or self._settings is None:
            return
        self._settings.setValue(
            f"columns/{self._settings_key}",
            {self._heading(c): w for c, w in self._pinned.items()},
        )

    def _restore(self) -> None:
        if self._settings_key is None or self._settings is None:
            return
        stored = self._settings.value(f"columns/{self._settings_key}")
        if not isinstance(stored, dict):
            return
        by_heading = {self._heading(c): c for c in range(self._column_count())}
        for heading, width in stored.items():
            column = by_heading.get(str(heading))
            if column is None:
                continue
            try:
                self._pinned[column] = int(width)
            except (TypeError, ValueError):
                continue

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            try:
                if watched is self._view:
                    self._refit_timer.start()
            except (AttributeError, RuntimeError):
                # Reached after the view it fits has been torn down, when there
                # is nothing left to fit. Raising here corrupts Qt's dispatch.
                pass
        return False


class _SizerHost(Protocol):
    """A view once its sizer is attached."""

    column_sizer: ColumnSizer


def install_column_sizer(
    view: QTableWidget | QTreeWidget,
    spec: ColumnSpec,
    *,
    settings_key: str | None = None,
    settings: QSettings | None = None,
) -> ColumnSizer:
    """Fit ``view``'s columns to its viewport, and keep them user-resizable.

    Held on the view in ``column_sizer``: PySide6 discards a subclass instance's
    ``__dict__`` while C++ still references it, and the resurrected wrapper's
    ``eventFilter`` then has no state to work from.

    A ``settings_key`` alone is enough to persist dragged widths: the store
    defaults to the application's own settings, so call sites name the table
    and nothing else.
    """
    if settings_key is not None and settings is None:
        settings = QSettings("ECEO", "deepreefmap")
    sizer = ColumnSizer(view, spec, settings_key=settings_key, settings=settings)
    cast("_SizerHost", view).column_sizer = sizer
    return sizer


def _sorts_before(mine: object, theirs: object, descending: bool) -> bool:
    """The one ordering both sortable item classes share.

    A row with no value sinks to the bottom in *both* directions. Qt sorts with
    ``__lt__`` and reverses for descending, so a plain comparison would float
    the blanks to the top of a descending sort; the caller reads the current
    order off its header and passes it in to cancel that out.
    """
    if (mine is None) != (theirs is None):
        return descending if mine is None else not descending
    if mine is None:
        return False
    try:
        return mine < theirs  # type: ignore[operator]
    except TypeError:
        return str(mine) < str(theirs)


class SortableItem(QTableWidgetItem):
    """A cell that sorts by a value rather than by its formatted text.

    "1.2M pts" above "988k pts", "1.6 GB" above "1015 MB": the display strings
    order wrongly under every string comparison, so the raw number rides along.
    """

    def __init__(self, text: str, value: object = None) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if not isinstance(other, SortableItem):
            return super().__lt__(other)
        table = self.tableWidget()
        descending = (
            table is not None
            and table.horizontalHeader().sortIndicatorOrder()
            == Qt.SortOrder.DescendingOrder
        )
        return _sorts_before(self._value, other._value, descending)


class SortableTreeItem(QTreeWidgetItem):
    """A row that sorts by per-column values, under SortableItem's contract.

    ``values`` maps column index to sort value. A column with no entry sinks in
    both directions, which is also how a whole valueless row stays pinned to
    the foot of its tree whatever the sort.
    """

    def __init__(
        self,
        tree: QTreeWidget,
        columns: Sequence[str],
        values: Mapping[int, object] | None = None,
    ) -> None:
        super().__init__(tree, list(columns))
        self._values = dict(values or {})

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        if not isinstance(other, SortableTreeItem):
            return super().__lt__(other)
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        descending = (
            tree is not None
            and tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        )
        return _sorts_before(self._values.get(column), other._values.get(column), descending)


def enable_sorting(
    view: QTableWidget | QTreeWidget,
    column: int | None = 0,
    order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
) -> None:
    """Turn on click-to-sort, with the indicator shown from the start.

    The ``sortable`` property is what the theme keys the header hover affordance
    on, so only headers that really sort light up under the pointer. Call this
    after the rows are in place: enabling sorting sorts there and then, and
    cells landing in a live sort scatter across half-built rows.

    Pass ``column=None`` to keep the rows in the order the caller built them.
    Headers still sort on click, but nothing sorts until then.
    """
    header = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
    header.setProperty("sortable", "true")
    # Re-polish so the property is read even on a header styled before this ran.
    header.style().unpolish(header)
    header.style().polish(header)
    header.setSortIndicatorShown(True)
    if column is None:
        # Parking the indicator on no section makes the enable below a no-op
        # sort, so a caller-built order (say newest first) survives it.
        header.setSortIndicator(-1, order)
        view.setSortingEnabled(True)
    else:
        view.setSortingEnabled(True)
        view.sortByColumn(column, order)


# The palette for survey/statuses.py's colour roles. That table is Qt-free; this
# is the one place it meets the theme.
TONE_COLORS = {
    statuses.TONE_GOOD: SUCCESS,
    statuses.TONE_BAD: ERROR,
    statuses.TONE_BUSY: WARNING,
    statuses.TONE_IDLE: IDLE,
    statuses.TONE_QUIET: TEXT_DIM,
}

# Every status the interface can show. Public: the tables, the detail panes and
# the filter chips all read it, so one status has one colour.
STATUS_COLORS = {
    status: TONE_COLORS[statuses.status_tone(status)] for status in statuses.DISPLAY_STATUSES
}


# The one place the Qt-free direction vocabulary meets the theme.
DIRECTION_COLORS = {
    "forward": DIRECTION_FORWARD,
    "reverse": DIRECTION_REVERSE,
}


def direction_html(direction: str | None) -> str:
    """The direction as rich text, for the fact rows that render markup."""
    if direction is None:
        return ""
    text = direction_text(direction)
    if not text:
        return ""
    colour = DIRECTION_COLORS[direction.strip().lower()]
    return f'<span style="color: {colour}; font-weight: {WEIGHT_SEMIBOLD}">{text}</span>'


def clip_outcome_color(outcome: str) -> str:
    """The colour for a clip's own state, from the roles the runs use."""
    return TONE_COLORS[statuses.clip_spec(outcome).tone]


# Percent complete of the pass a status cell describes, 0-100. Absent on every
# row but the one being processed, which is what makes the pill a progress bar.
PASS_PERCENT_ROLE = Qt.ItemDataRole.UserRole + 1

class StatusPillDelegate(QStyledItemDelegate):
    """Paints a run status as a tinted pill, readable across the table.

    A delegate rather than a cell widget so the QTableWidgetItem stays the
    source of truth for the status text. The running row's pill doubles as its
    progress bar: PASS_PERCENT_ROLE fills it from the left.
    """

    def __init__(
        self, parent: QWidget | None = None, *, colours: Mapping[str, str] = STATUS_COLORS
    ) -> None:
        super().__init__(parent)
        self._colours = colours

    def paint(self, painter, option: QStyleOptionViewItem, index) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            super().paint(painter, option, index)
            return

        # Selection and the alternating row fill still come from the style.
        blank = QStyleOptionViewItem(option)
        self.initStyleOption(blank, index)
        blank.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, blank, painter, option.widget)

        # Whichever word is a key, so a decorated pill keeps its colour whether the
        # decoration leads ("→ Forward") or trails ("Succeeded ⚠"). Case-insensitive
        # so a title-cased "Failed" pill still reads red.
        key = next((w for w in text.lower().split() if w in self._colours), "")
        color = QColor(self._colours.get(key, TEXT_MUTED))
        metrics = option.fontMetrics
        pad_x = SPACE_SM
        width = min(metrics.horizontalAdvance(text) + pad_x * 2, option.rect.width() - SPACE_SM)
        height = metrics.height() + 4
        pill = QRectF(
            option.rect.left() + 4,
            option.rect.center().y() - height / 2 + 1,
            max(1, width),
            height,
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Light enough that the accent text keeps its contrast against the tint:
        # the pill fill is drawn from the same colour as the text on top of it,
        # so every point of alpha here is a point of legibility lost.
        fill = QColor(color)
        fill.setAlpha(PILL_TINT_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, height / 2, height / 2)

        # A running pass fills its own pill rather than growing a second widget in
        # the row: the pill is already the thing the eye goes to for that pass.
        percent = index.data(PASS_PERCENT_ROLE)
        if percent is not None:
            done = QColor(color)
            done.setAlpha(PILL_PROGRESS_ALPHA)
            painter.setBrush(done)
            painter.save()
            clip = QRectF(pill)
            clip.setWidth(pill.width() * max(0.0, min(100.0, float(percent))) / 100.0)
            painter.setClipRect(clip)
            painter.drawRoundedRect(pill, height / 2, height / 2)
            painter.restore()

        painter.setPen(color)
        painter.drawText(
            pill,
            Qt.AlignmentFlag.AlignCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(pill.width()) - pad_x),
        )
        painter.restore()


class MeterBar(QProgressBar):
    """A thin utilisation bar that can also say it has nothing to report.

    Qt draws a range of (0, 0) as a busy animation, so an unknown figure reads
    as activity. Unavailable is painted instead as hazard hatching: static, and
    obviously not a level.

    A share of the track can already be held by something other than what the
    bar measures -- memory another application is sitting in, say. It is drawn
    first, in the same grey the drive bars give to space they do not account
    for, and the level stacks on top of it: the resource is taken before this
    run asks for any, so what is left of the track is what is genuinely free.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(False)
        self.setFixedHeight(BAR_HEIGHT)
        self._unavailable = False
        self._held_percent = 0.0
        self._colour = GROOVE

    def set_level(self, percent: float, colour: str, held_percent: float = 0.0) -> None:
        self._unavailable = False
        self._held_percent = max(0.0, min(100.0, held_percent))
        self._colour = colour
        self.setRange(0, 100)
        self.setValue(int(round(max(0.0, min(100.0, percent)))))
        self.setStyleSheet(bar_qss(colour))
        self.update()

    def held_percent(self) -> float:
        return self._held_percent

    def set_unavailable(self) -> None:
        self._unavailable = True
        self._held_percent = 0.0
        self.setRange(0, 100)
        self.setValue(0)
        self.setStyleSheet(bar_qss(GROOVE))
        self.update()

    def _paint_stacked(self) -> None:
        """What is already held, then the level on top of it.

        Painted rather than left to the stylesheet's chunk, which only ever
        starts at the left edge. Segments are square-cornered inside the
        groove's rounding, as the drive bars are, so the two meet with no seam
        and neither leaves a lit corner past the ends. A level that runs off the
        track is clipped there, which is the fact: there is no room for it.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(self.rect())
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))
        painter.setClipPath(path)
        start = 0.0
        for share, colour in ((self._held_percent, SURFACE_HI), (float(self.value()), self._colour)):
            width = track.width() * share / 100.0
            painter.fillRect(
                QRectF(track.left() + start, track.top(), width, track.height()), QColor(colour)
            )
            start += width
        painter.end()

    def paintEvent(self, event) -> None:
        if not self._unavailable:
            if self._held_percent > 0:
                self._paint_stacked()
            else:
                super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(self.rect())
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))
        painter.setClipPath(path)
        pen = QPen(QColor(ERROR))
        pen.setWidthF(2.0)
        painter.setPen(pen)
        # Diagonals at a fixed pitch, offset so each starts off the left edge.
        pitch = 7.0
        x = -track.height()
        while x < track.width() + track.height():
            painter.drawLine(QPointF(x, track.bottom()), QPointF(x + track.height(), track.top()))
            x += pitch
        painter.end()
