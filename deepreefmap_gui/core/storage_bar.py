"""Where a survey's disk space went, as one button per drive.

The bars sit at the foot of the window for the whole session, so a diver filling
a laptop in the field sees it happening rather than finding out when a run dies.
Only the drives the survey names are drawn: see profiling/volumes.py, which does
all the accounting. This module paints, it does not measure.

Each drive is a button, because seeing the problem and being able to act on it
should not be two different places. Hovering one raises a card with the same
figures at a size somebody can read, and clicking one opens that drive's page.

Colour inside the groove means one thing only: this much of the drive holds
that. Free space is therefore never filled, it is the groove showing through. A
drive that is running out says so in its figures and in an outline around the
whole bar, which cannot be mistaken for one more thing taking up room in it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.hover_card import cursor_on, install_dismiss_filter
from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BLOCK,
    BORDER_STRONG,
    CARD_BG,
    CARD_HOVER_DELAY_MS,
    CARD_HOVER_GUARD_MS,
    GROOVE,
    PRIMARY,
    RADIUS,
    SELECTION_BG,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    WARNING,
)
from deepreefmap_gui.core.widgets import muted_label, utility_button_qss
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.profiling.volumes import (
    FULLNESS_FULL,
    FULLNESS_TIGHT,
    VolumeUsage,
    fullness,
    used_percent,
)

if TYPE_CHECKING:
    from deepreefmap_gui.core.volume_card import VolumeCard

# Narrower than this and the three segments stop being separable from each
# other, which is the only thing the bar is for.
BAR_MIN_WIDTH = 72

# How many drives get a button of their own. A field laptop has a system disk
# plus a card reader plus an external, and past that the row is wider than the
# status it shares the foot of the window with.
MAX_BARS = 3

# The bar at reading size, for the hover card and the storage page header.
TALL_BAR_HEIGHT = SPACE_XL

# What a drive in trouble is said in. Carried by the figures and by an outline
# around the whole bar, never by a fill: the only thing colour inside the groove
# is allowed to mean is "this much of the drive holds that". Painting the free
# tail amber said a drive had just been filled by something nobody could name.
_ALERT_COLOUR = {FULLNESS_TIGHT: WARNING, FULLNESS_FULL: BLOCK}

# The segments, in the order they are painted and listed. Their colours are the
# app's roles: what came in, what we made, and what was already there.
_SEGMENTS = (
    (PRIMARY, "Videos", "video_bytes"),
    (SUCCESS, "Outputs", "output_bytes"),
    (SURFACE_HI, "Other used", "other_used_bytes"),
)

# Free space has no fill of its own: it is the groove showing through. The
# legend still needs something to point at, so it gets the groove's own outline.
FREE_SWATCH = BORDER_STRONG


def _share(part: int, total: int) -> str:
    """A percentage that never rounds a real figure away to nothing."""
    percent = 100.0 * part / max(total, 1)
    if 0 < percent < 1:
        return "<1%"
    return f"{percent:.0f}%"


def volume_rows(volume: VolumeUsage) -> list[tuple[str, str, str, str]]:
    """Every figure the bar draws, as (colour, label, bytes, percent).

    The one place the numbers are worded, so the button, the card, the page
    header and the tooltip cannot disagree about what the same drive holds.
    """
    total = volume.total_bytes
    rows = [
        (colour, label, format_bytes(getattr(volume, attr)), _share(getattr(volume, attr), total))
        for colour, label, attr in _SEGMENTS
    ]
    rows.append((FREE_SWATCH, "Free", format_bytes(volume.free_bytes), _share(volume.free_bytes, total)))
    return rows


def volume_headline(volume: VolumeUsage) -> str:
    return f"{used_percent(volume):.0f}% full"


def alert_colour(volume: VolumeUsage) -> str | None:
    """The colour this drive's figures are worth saying in, or None when it is fine."""
    return _ALERT_COLOUR.get(fullness(volume))


def volume_tooltip(volume: VolumeUsage) -> str:
    """The drive, and every figure the bar draws, spelled out."""
    lines = [volume.root]
    lines += [f"{label}: {size}" for _, label, size, _ in volume_rows(volume)[:-1]]
    lines.append(f"Free: {format_bytes(volume.free_bytes)} of {format_bytes(volume.total_bytes)}")
    if volume.unmeasured_items:
        # Those bytes are counted, just not as ours: they sit in "other used",
        # which otherwise reads as though the survey has taken no room at all.
        lines.append(
            f"{volume.unmeasured_items} item(s) of unknown size, counted under other used"
        )
    return "\n".join(lines)


class VolumeBar(QWidget):
    """One drive: clips in, our outputs, everything else, and what is left.

    Painted rather than assembled from styled child widgets, because the four
    parts have to add up to one continuous groove with no seams between them.
    """

    def __init__(
        self, parent: QWidget | None = None, *, height: int = BAR_HEIGHT, describe: bool = True
    ) -> None:
        super().__init__(parent)
        self._usage: VolumeUsage | None = None
        # The card carries the figures in full beside its own copy of the bar,
        # so that copy must not raise a second tooltip over the top of them.
        self._describe = describe
        self.setFixedHeight(height)
        self.setMinimumWidth(BAR_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def usage(self) -> VolumeUsage | None:
        return self._usage

    def set_usage(self, volume: VolumeUsage) -> None:
        self._usage = volume
        if self._describe:
            self.setToolTip(volume_tooltip(volume))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(self.rect())
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))

        volume = self._usage
        total = volume.total_bytes if volume is not None else 0
        if volume is not None and total > 0:
            painter.save()
            # Segments are square-cornered rectangles, so the groove's own
            # rounding is what shapes the ends of the bar. What is left of the
            # track is free space, and it is left as groove: a colour in here
            # says "this much of the drive holds that", and free holds nothing.
            painter.setClipPath(path)
            # Offsets accumulate in fractions of the track rather than in pixels,
            # so rounding cannot open a groove-coloured seam between two segments.
            start = 0.0
            for colour, _, attr in _SEGMENTS:
                end = min(1.0, start + getattr(volume, attr) / total)
                if end > start:
                    left = track.left() + track.width() * start
                    width = track.width() * (end - start)
                    painter.fillRect(
                        QRectF(left, track.top(), width, track.height()), QColor(colour)
                    )
                start = end
            painter.restore()
            alert = alert_colour(volume)
            if alert is not None:
                # An outline rather than a fill. It rings the whole drive, so it
                # cannot be read as another thing taking up room in it.
                painter.setPen(QPen(QColor(alert), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                inset = track.adjusted(0.5, 0.5, -0.5, -0.5)
                painter.drawRoundedRect(inset, radius, radius)
        painter.end()


def _caption(volume: VolumeUsage, *, compact: bool) -> str:
    free = format_bytes(volume.free_bytes)
    return f"{volume.label} {free}" if compact else f"{volume.label}  {free} free"


class VolumeButton(QAbstractButton):
    """One drive's caption and bar, as the control that opens its page.

    A bare QAbstractButton rather than a QToolButton holding the bar: a tool
    button paints its own background under its contents, and the contents here
    are a continuous groove that must not have anything drawn behind it.
    """

    hovered = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(SPACE_XS, SPACE_XS, SPACE_XS, SPACE_XS)
        self._column.setSpacing(SPACE_XS)
        self.caption = muted_label()
        self._column.addWidget(self.caption)
        self.bar = VolumeBar()
        self._column.addWidget(self.bar)
        # Without this, crossing from the caption to the bar fires a leave and an
        # enter, and the card flickers off and back on under the cursor.
        for child in (self.caption, self.bar):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def usage(self) -> VolumeUsage | None:
        return self.bar.usage()

    def show_volume(self, volume: VolumeUsage, *, compact: bool) -> None:
        self.caption.setText(_caption(volume, compact=compact))
        self.bar.set_usage(volume)
        # The status text beside this takes every pixel it is offered, so the
        # button has to hold its own width or its caption is elided to "15.0 GB
        # fr". The caption is the whole reason the button is legible at a glance.
        self.setMinimumWidth(
            self.caption.sizeHint().width() + self._column.contentsMargins().left() * 2
        )
        # No Qt tooltip: the hover card is this button's tooltip, and two of them
        # would arrive over each other. Screen readers get the same words.
        self.setAccessibleName(f"{volume.label}, {volume_headline(volume)}")
        self.setAccessibleDescription(volume_tooltip(volume))

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return self._column.sizeHint()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(False)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(False)
        super().focusOutEvent(event)

    def paintEvent(self, event) -> None:
        """The button's own chrome only. The bar paints itself as a child.

        Painted rather than styled, so this widget stays out of the global
        stylesheet and cannot leak a background onto the groove below it.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            painter.setBrush(QColor(SELECTION_BG))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
            # A rule along the top edge, because the page this opens is up there.
            painter.fillRect(
                QRectF(rect.left(), rect.top(), rect.width(), 2), QColor(PRIMARY)
            )
        elif self.underMouse():
            painter.setBrush(QColor(SURFACE_HI))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(PRIMARY), 1))
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        painter.end()


class VolumeListPopup(QFrame):
    """Every drive at once, one per line, each a way into its own page.

    The foot of the window carries the first few side by side and has room for no
    more. This is the whole list, opened from the readiness row that reports disk
    space, where the question being asked is "which drive, and how full?" rather
    than "how is this run doing?".

    The rows are the same buttons the foot of the window draws, so a drive looks
    like itself wherever it is seen and there is one place the bar is painted.
    """

    volume_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("volumeList")
        self.setStyleSheet(
            f"QFrame#volumeList {{ background-color: {CARD_BG};"
            f" border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS}px; }}"
        )
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(SPACE_SM, SPACE_SM, SPACE_SM, SPACE_SM)
        self._column.setSpacing(SPACE_XS)
        self._buttons: list[VolumeButton] = []
        self._empty = muted_label("No drives measured yet.")
        self._column.addWidget(self._empty)

    @property
    def buttons(self) -> list[VolumeButton]:
        """The drive rows currently listed, in the order they are drawn."""
        return [button for button in self._buttons if button.isVisibleTo(self)]

    def _button(self, index: int) -> VolumeButton:
        while len(self._buttons) <= index:
            button = VolumeButton(self)
            button.setCheckable(False)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            button.clicked.connect(self._on_row_clicked)
            # Before the empty line, which stays last and is hidden whenever
            # there is anything to list.
            self._column.insertWidget(len(self._buttons), button)
            self._buttons.append(button)
        return self._buttons[index]

    def show_volumes(self, volumes: list[VolumeUsage], *, under: QWidget) -> None:
        """List these drives under `under`, kept inside the screen it sits on."""
        for index, volume in enumerate(volumes):
            button = self._button(index)
            button.show_volume(volume, compact=False)
            button.setVisible(True)
        for button in self._buttons[len(volumes) :]:
            button.setVisible(False)
        self._empty.setVisible(not volumes)
        self.adjustSize()
        self.move(self._anchored_at(under))
        self.show()
        self.raise_()

    def _anchored_at(self, under: QWidget) -> QPoint:
        """Just below the control that opened it, nudged back onto the screen.

        A popup opened from a row near the bottom of a laptop screen would
        otherwise list its drives off the end of it.
        """
        point = under.mapToGlobal(QPoint(0, under.height() + SPACE_XS))
        screen = under.screen()
        if screen is None:
            return point
        area = screen.availableGeometry()
        x = min(max(point.x(), area.left()), max(area.right() - self.width(), area.left()))
        y = point.y()
        if y + self.height() > area.bottom():
            # Above the control instead, which is where the room is.
            y = max(area.top(), under.mapToGlobal(QPoint(0, 0)).y() - self.height() - SPACE_XS)
        return QPoint(x, y)

    def _on_row_clicked(self) -> None:
        button = self.sender()
        volume = button.usage() if isinstance(button, VolumeButton) else None
        self.hide()
        if volume is not None:
            self.volume_clicked.emit(volume.root)


class StorageBars(QWidget):
    """The drives this survey uses, side by side, each one a way in.

    Buttons are pooled rather than rebuilt: this is refreshed on a timer, and
    deleting and recreating widgets every tick both churns and makes the foot of
    the window flicker as the layout settles.
    """

    # The root of the drive whose button was pressed. What that opens is the
    # window's business, and what is checked follows from where it went.
    volume_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volumes: list[VolumeUsage] = []
        self._compact = False
        self._buttons: list[VolumeButton] = []
        self._selected: str | None = None
        self._card: VolumeCard | None = None
        self._card_for: VolumeButton | None = None
        self._suppressed: VolumeButton | None = None
        # Which window the dismissal filter is on, so a re-parent moves it rather
        # than stacking a second copy.
        self._dismiss_filter_window: QWidget | None = None

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(SPACE_SM)
        self._overflow = QToolButton()
        self._overflow.setStyleSheet(utility_button_qss())
        self._overflow.setCursor(Qt.CursorShape.PointingHandCursor)
        self._overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._layout.addWidget(self._overflow)
        self._overflow.setVisible(False)

        # A card that appeared the instant a cursor crossed the foot of the
        # window would ambush anybody reaching for the status text below it.
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(CARD_HOVER_DELAY_MS)
        self._hover_timer.timeout.connect(self._show_card)

        # A leave is not guaranteed to arrive: a pointer can go off the window
        # edge or another application can take the screen. So while a card is up,
        # where the cursor actually is decides whether it stays.
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(CARD_HOVER_GUARD_MS)
        self._guard_timer.timeout.connect(self._guard_card)

        self.setVisible(False)

    @property
    def bars(self) -> list[VolumeBar]:
        """The bars currently on screen, in the order they are drawn."""
        return [button.bar for button in self.buttons]

    @property
    def buttons(self) -> list[VolumeButton]:
        """The drive buttons currently on screen, in the order they are drawn."""
        # isVisibleTo, not isVisible: a button is only truly visible once the
        # whole window is, and this has to answer before then.
        return [b for b in self._buttons if b.isVisibleTo(self)]

    @property
    def overflow_button(self) -> QToolButton:
        """The "+2 more" stand-in for the drives with no button of their own."""
        return self._overflow

    def volumes(self) -> list[VolumeUsage]:
        """Every drive last measured, including the ones with no button here."""
        return list(self._volumes)

    def selected_root(self) -> str | None:
        return self._selected

    def set_selected_root(self, root: str | None) -> None:
        """Light the drive whose page is open, or none.

        The only thing that ever checks a button. A press asks the window to go
        somewhere and the window says what is lit when it gets there, so "click
        again to leave" and "navigate away" are the same one mechanism.
        """
        self._selected = root
        self._sync_checks()

    def set_volumes(self, volumes: list[VolumeUsage]) -> None:
        self._volumes = list(volumes)
        self._refresh()

    def set_compact(self, compact: bool) -> None:
        """During a run the foot of the window also carries progress and an ETA."""
        self._compact = compact
        self._refresh()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        try:
            self._hover_timer.stop()
            self._hide_card()
        except (AttributeError, RuntimeError):
            # Reached on a resurrected wrapper, which has no timer and no card
            # to take down: PySide6 discards a subclass instance's __dict__
            # while C++ still references it, and Qt goes on hiding the widget
            # afterwards. Raising here escapes through whatever event filter is
            # on the stack and corrupts Qt's dispatch.
            pass
        super().hideEvent(event)

    def _button(self, index: int) -> VolumeButton:
        while len(self._buttons) <= index:
            button = VolumeButton(self)
            # Inserted before the overflow button, which stays last in the row.
            self._layout.insertWidget(len(self._buttons), button)
            button.clicked.connect(self._on_button_clicked)
            button.hovered.connect(self._on_button_hovered)
            self._buttons.append(button)
        return self._buttons[index]

    def _shown(self) -> list[VolumeUsage]:
        if not self._compact:
            return self._volumes[:MAX_BARS]
        # The one drive worth the width is the one whose page is open, if any:
        # starting a batch should not take the button out from under the page
        # somebody is looking at.
        for volume in self._volumes:
            if volume.root == self._selected:
                return [volume]
        return self._volumes[:1]

    def _refresh(self) -> None:
        shown = self._shown()
        for index, volume in enumerate(shown):
            button = self._button(index)
            button.show_volume(volume, compact=self._compact)
            button.setVisible(True)
        for button in self._buttons[len(shown) :]:
            button.setVisible(False)

        hidden = [v for v in self._volumes if v not in shown]
        self._overflow.setText(f"+{len(hidden)} more" if hidden else "")
        self._overflow.setToolTip(
            "\n".join(f"{v.label}  {format_bytes(v.free_bytes)} free" for v in hidden)
        )
        self._fill_overflow_menu(hidden)
        self._overflow.setVisible(bool(hidden))
        self._sync_checks()
        # A drive that has stopped being reported cannot keep a card on screen.
        if self._card_for is not None and self._card_for not in self.buttons:
            self._hide_card()
        self.setVisible(bool(self._volumes))

    def _fill_overflow_menu(self, hidden: list[VolumeUsage]) -> None:
        """A way into the drives with no button, rebuilt each refresh."""
        menu = self._overflow.menu()
        if menu is None:
            menu = QMenu(self._overflow)
            self._overflow.setMenu(menu)
        menu.clear()
        for volume in hidden:
            action = menu.addAction(f"{volume.label}  {format_bytes(volume.free_bytes)} free")
            action.setCheckable(True)
            action.setChecked(volume.root == self._selected)
            action.triggered.connect(
                lambda _checked=False, root=volume.root: self.volume_clicked.emit(root)
            )

    def _sync_checks(self) -> None:
        for button in self._buttons:
            volume = button.usage()
            button.setChecked(volume is not None and volume.root == self._selected)

    def _on_button_clicked(self) -> None:
        button = self.sender()
        volume = button.usage() if isinstance(button, VolumeButton) else None
        # The press does not decide what is lit: it asks, and set_selected_root
        # answers once the window has gone wherever it is going.
        self._sync_checks()
        self._hide_card()
        # The focus a press leaves behind would raise the card over the page it
        # just opened, so it stays down until the pointer moves away.
        self._suppressed = button if isinstance(button, VolumeButton) else None
        if volume is not None:
            self.volume_clicked.emit(volume.root)

    def _on_button_hovered(self, entered: bool) -> None:
        button = self.sender()
        if not entered:
            self._hover_timer.stop()
            if self._suppressed is button:
                self._suppressed = None
            self._hide_card()
            return
        if not isinstance(button, VolumeButton) or button is self._suppressed:
            return
        # Moving from one drive to the next swaps the card without waiting again:
        # the delay is there to stop it appearing, not to stop it moving.
        showing = self._card is not None and self._card.isVisible()
        self._card_for = button
        if showing:
            self._show_card()
        else:
            self._hover_timer.start()

    def _show_card(self) -> None:
        button = self._card_for
        volume = button.usage() if button is not None else None
        if button is None or volume is None or not button.isVisibleTo(self):
            return
        from deepreefmap_gui.core.volume_card import VolumeCard

        if self._card is None:
            # Parentless: it is a window of its own, and a child here would show
            # up in this widget's children as another bar.
            self._card = VolumeCard()
        self._install_dismissal()
        self._card.show_for(volume, button)
        self._guard_timer.start()

    def _install_dismissal(self) -> None:
        """Watch the window the bars ended up in, so a leave takes the card down."""
        window = self.window()
        if self._dismiss_filter_window is window:
            return
        install_dismiss_filter(self, self._hide_card)
        self._dismiss_filter_window = window

    def _guard_card(self) -> None:
        """Take the card down once the cursor is no longer on its button.

        The card is not asked: it is non-interactive and sits ``CARD_GAP`` clear
        of the button, so a pointer reaching it has left the button first.
        """
        card, button = self._card, self._card_for
        if card is None or not card.isVisible():
            self._guard_timer.stop()
            return
        # The button's window, never the card's: a WA_ShowWithoutActivating
        # window is never active.
        if button is None or not button.window().isActiveWindow():
            self._hide_card()
            return
        # Focus holds the card up for a keyboard reader, who has no pointer to
        # be on the button with.
        if not (cursor_on(QCursor.pos(), button) or button.hasFocus()):
            self._hide_card()

    def _hide_card(self) -> None:
        self._card_for = None
        self._guard_timer.stop()
        if self._card is not None:
            self._card.hide()
