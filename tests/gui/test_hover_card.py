"""Where a hover card lands, given an anchor near an edge of the screen.

Scenario: the cards this places follow controls that sit in the corners of the
window: the drive buttons at the foot, the running row of a queue. A card
placed at a fixed offset from those spills off the screen, which is what these
cover: for every corner, the card has to end up whole and on screen.

Expected behaviour: clamping alone is not enough. A card asked to sit above an
anchor at the top of the screen has a negative y, and clamping would pin it over
the anchor it describes; it has to flip below instead.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from deepreefmap_gui.core.hover_card import (
    CARD_GAP,
    apply_hover_card_flags,
    cursor_on,
    install_dismiss_filter,
    place_near_cursor,
    place_near_widget,
)


@pytest.fixture
def card(qapp) -> QWidget:
    widget = QLabel("a card with some width to it\nand a second line")
    apply_hover_card_flags(widget)
    widget.adjustSize()
    return widget


def bounds() -> QRect:
    return QGuiApplication.primaryScreen().availableGeometry()


def corners(size: QSize) -> list[QPoint]:
    """One anchor point hard against each corner of the screen, and the middle."""
    area = bounds()
    return [
        QPoint(area.left(), area.top()),
        QPoint(area.right() - size.width(), area.top()),
        QPoint(area.left(), area.bottom() - size.height()),
        QPoint(area.right() - size.width(), area.bottom() - size.height()),
        area.center(),
    ]


def test_a_card_placed_against_a_widget_stays_on_screen(card, qapp) -> None:
    anchor = QWidget()
    anchor.resize(120, 24)
    for point in corners(anchor.size()):
        rect = QRect(point, anchor.size())
        place_near_widget(card, anchor, anchor_rect=rect)
        assert bounds().contains(card.geometry()), f"anchored at {point}"


def test_a_card_placed_near_the_cursor_stays_on_screen(card, qapp) -> None:
    for point in corners(QSize(1, 1)):
        place_near_cursor(card, point)
        assert bounds().contains(card.geometry()), f"cursor at {point}"


def test_no_room_above_puts_the_card_below_rather_than_over_the_anchor(card, qapp) -> None:
    """Clamping instead of flipping would cover the thing being described."""
    area = bounds()
    anchor = QWidget()
    anchor.resize(120, 24)
    rect = QRect(QPoint(area.center().x(), area.top()), anchor.size())

    place_near_widget(card, anchor, anchor_rect=rect, prefer="above")

    assert card.geometry().top() >= rect.bottom()


def test_no_room_below_puts_the_card_above(card, qapp) -> None:
    area = bounds()
    anchor = QWidget()
    anchor.resize(120, 24)
    rect = QRect(QPoint(area.center().x(), area.bottom() - 24), anchor.size())

    place_near_widget(card, anchor, anchor_rect=rect, prefer="below")

    assert card.geometry().bottom() <= rect.top()


def test_the_flags_let_a_plain_widget_paint_its_own_border(card) -> None:
    """A QWidget ignores a stylesheet box model without WA_StyledBackground."""
    from PySide6.QtCore import Qt

    assert card.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert card.windowFlags() & Qt.WindowType.FramelessWindowHint


# --- dismissal --------------------------------------------------------------

def test_the_cursor_is_judged_by_where_the_anchor_is_not_by_a_stale_flag(qapp) -> None:
    """Scenario: the pointer leaves the window without a Leave reaching the
    anchor, so `WA_UnderMouse` stays set on it.

    Expected behaviour: `cursor_on` answers from geometry and says no.
    """
    from PySide6.QtCore import Qt

    anchor = QWidget()
    anchor.resize(80, 20)
    anchor.move(300, 300)
    anchor.show()
    anchor.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)

    assert anchor.underMouse()
    assert not cursor_on(QPoint(4000, 4000), anchor)
    assert cursor_on(anchor.mapToGlobal(QPoint(40, 10)), anchor)


def test_the_gap_a_card_is_placed_across_still_counts_as_on_the_anchor(qapp) -> None:
    """A card sits CARD_GAP clear of its anchor, so a strict rect would take the
    card down as the pointer crossed toward it."""
    anchor = QWidget()
    anchor.resize(80, 20)
    anchor.move(300, 300)
    anchor.show()
    just_outside = anchor.mapToGlobal(QPoint(40, 20 + CARD_GAP - 1))

    assert cursor_on(just_outside, anchor)
    assert not cursor_on(anchor.mapToGlobal(QPoint(40, 20 + CARD_GAP * 3)), anchor)


def test_a_hidden_anchor_is_never_under_the_cursor(qapp) -> None:
    anchor = QWidget()
    anchor.resize(80, 20)
    assert not cursor_on(QPoint(0, 0), anchor)


@pytest.mark.parametrize(
    "event_type",
    [
        QEvent.Type.Leave,
        QEvent.Type.WindowDeactivate,
        QEvent.Type.WindowStateChange,
    ],
)
def test_every_way_of_losing_the_pointer_hides_the_card(qapp, event_type) -> None:
    owner = QWidget()
    owner.show()
    hidden: list[bool] = []
    install_dismiss_filter(owner, lambda: hidden.append(True))

    QApplication.sendEvent(owner.window(), QEvent(event_type))

    assert hidden == [True]


def test_a_hidden_window_is_left_to_its_own_hide_event(qapp) -> None:
    """Filtering Hide reaches widgets already part-way through teardown, and a
    widget holding a card takes it down in `hideEvent` regardless."""
    owner = QWidget()
    owner.show()
    hidden: list[bool] = []
    install_dismiss_filter(owner, lambda: hidden.append(True))

    QApplication.sendEvent(owner.window(), QEvent(QEvent.Type.Hide))

    assert hidden == []


def test_a_filter_stops_watching_a_window_its_owner_did_not_outlive(qapp) -> None:
    """Scenario: the owner is gone but the window it was in is not, which is what
    a discarded __dict__ leaves behind.

    Expected behaviour: the filter comes off. There is no card left to take
    down and no state to find one with, so answering for that window again only
    reaches further into a widget that cannot answer.
    """
    owner = QWidget()
    owner.show()
    hidden: list[bool] = []
    alive = [True]

    def hide() -> None:
        if not alive[0]:
            raise AttributeError("_card")
        hidden.append(True)

    handler = install_dismiss_filter(owner, hide)
    QApplication.sendEvent(owner.window(), QEvent(QEvent.Type.Leave))
    assert hidden == [True]

    alive[0] = False
    QApplication.sendEvent(owner.window(), QEvent(QEvent.Type.Leave))
    alive[0] = True
    QApplication.sendEvent(owner.window(), QEvent(QEvent.Type.Leave))

    assert hidden == [True]
    assert handler._watching is None


def test_the_filter_consumes_nothing_it_watches(qapp) -> None:
    """Everything it acts on is an event somebody else is also handling."""
    owner = QWidget()
    owner.show()
    handler = install_dismiss_filter(owner, lambda: None)

    assert handler.eventFilter(owner, QEvent(QEvent.Type.Leave)) is False
