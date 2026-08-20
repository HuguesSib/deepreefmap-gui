"""The registry link's state at the foot of the window, as a control.

One glance answers "is this laptop's survey on the server", the way the storage
bars answer "will the next run fit". Pressing it acts on what it shows: a sync
when one is what is needed, the Server page when connecting or reading a fault
is. The window decides which; this widget only paints and reports the press.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QWidget

from deepreefmap_gui.core.theme import (
    ERROR,
    PRIMARY,
    RADIUS,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    TEXT_MUTED,
    WARNING,
)
from deepreefmap_gui.core.widgets import muted_label


@dataclass(frozen=True)
class SyncBadgeFace:
    """What the badge shows, decided by the window and painted here."""

    text: str
    colour: str
    tooltip: str = ""


# The faces, worded once. The count is formatted where the state is read.
NOT_CONNECTED = SyncBadgeFace(
    "○ No registry",
    TEXT_MUTED,
    "This laptop is not connected to a registry. Press to connect.",
)
SYNCING = SyncBadgeFace("↕ Syncing…", PRIMARY, "Talking to the registry now.")
# Enrolled, but nothing to grade: without an open survey there are no rows to
# count, so claiming "Synced" would be a verdict on data never examined.
NO_SURVEY = SyncBadgeFace(
    "○ Connected",
    TEXT_MUTED,
    "Connected to the registry. Open an output folder to see what is waiting to sync.",
)
FAULT = SyncBadgeFace(
    "✕ Sync fault",
    ERROR,
    "The last sync did not finish. Press to read what happened.",
)


def fault_face(detail: str) -> SyncBadgeFace:
    """The fault face carrying what the failed sync actually said."""
    if not detail:
        return FAULT
    return SyncBadgeFace(FAULT.text, FAULT.colour, f"{detail} Press to open the Server page.")


def waiting_face(count: int, breakdown: str) -> SyncBadgeFace:
    detail = f" ({breakdown})" if breakdown else ""
    return SyncBadgeFace(
        f"↑ {count} to send",
        WARNING,
        f"{count} row(s) recorded here are not on the registry yet{detail}. Press to sync.",
    )


def synced_face(age: str) -> SyncBadgeFace:
    when = f" Last sync {age} ago." if age else ""
    return SyncBadgeFace("✓ Synced", SUCCESS, f"The registry has everything from here.{when}")


class SyncBadge(QAbstractButton):
    """A one-line state and a press, chrome matching the storage buttons.

    A bare QAbstractButton for the same reason VolumeButton is one: the hover
    and focus chrome is painted here, and nothing may draw behind the label.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, SPACE_XS, SPACE_SM, SPACE_XS)
        row.setSpacing(0)
        self._label = muted_label()
        self._label.setTextFormat(Qt.TextFormat.RichText)
        # Without this the label eats the press the badge exists to receive.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self._label)
        self._row = row
        self.show_face(NOT_CONNECTED)

    def show_face(self, face: SyncBadgeFace) -> None:
        self._label.setText(f"<span style='color:{face.colour}'>{face.text}</span>")
        self.setToolTip(face.tooltip)
        # Screen readers get the words, not the glyph.
        self.setAccessibleName(f"Registry: {face.text.lstrip('○↕✕✓↑ ')}")
        self.setAccessibleDescription(face.tooltip)
        # The status text beside this takes every pixel it is offered, so the
        # badge holds its own width or its caption is elided mid-word.
        margins = self._row.contentsMargins()
        self.setMinimumWidth(self._label.sizeHint().width() + margins.left() + margins.right())

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return self._row.sizeHint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.underMouse():
            painter.setBrush(QColor(SURFACE_HI))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(PRIMARY), 1))
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        painter.end()
