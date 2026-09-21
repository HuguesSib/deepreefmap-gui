"""Getting back to where a link took you from.

Scenario: the Transect + pass button on a cart row jumps to that pass
under Videos. That is the right thing to do, and until now there was no way
back: the mouse's back button did nothing, and there was no history in the app
at all.

Expected behaviour: back returns to the page *and* the row that was selected,
because arriving at the cart with nothing picked out is half a return. Choosing
a destination from the header is a fresh start rather than a step to unwind.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.simple.navigation import NavigationHistory, Place

pytestmark = pytest.mark.usefixtures("qapp")


# --- The history itself ---


def test_a_new_place_discards_what_was_ahead_of_it():
    """"Back, back, then off somewhere else" behaves as it does everywhere."""
    history = NavigationHistory()
    for name in ("videos", "process", "browse"):
        history.push(Place(name))

    history.back()
    history.back()
    assert history.current() == Place("videos")

    history.push(Place("transects"))
    assert not history.can_go_forward
    assert history.back() == Place("videos")


def test_arriving_where_you_already_are_is_not_a_step():
    history = NavigationHistory()
    history.push(Place("videos", "abc"))
    history.push(Place("videos", "abc"))
    assert len(history) == 1


def test_the_selection_is_part_of_where_you_were():
    history = NavigationHistory()
    history.push(Place("process"))
    history.push(Place("videos", "pass-1"))

    assert history.back() == Place("process")
    assert history.forward() == Place("videos", "pass-1")


def test_a_place_that_is_gone_is_dropped_rather_than_returned_to():
    """A deleted pass must not cost two presses to get past."""
    history = NavigationHistory()
    history.push(Place("process"))
    history.push(Place("videos", "deleted"))
    history.back()

    history.forward()
    assert history.current() == Place("videos", "deleted")
    assert history.drop_current() == Place("process")
    assert len(history) == 1


def test_history_does_not_grow_without_limit():
    history = NavigationHistory()
    for index in range(500):
        history.push(Place("videos", str(index)))
    assert len(history) <= 50


# --- Wired to the window ---


def test_following_a_link_can_be_undone(window):
    window._set_simple_section("process")
    window._go_to_section("videos")
    assert window._current_section() == "videos"

    assert window._go_back()
    assert window._current_section() == "process"

    assert window._go_forward()
    assert window._current_section() == "videos"


def test_choosing_a_destination_is_not_a_history_step(window):
    """Otherwise back undoes choices nobody asked it to undo."""
    window._set_simple_section("process")
    window._go_to_section("videos")
    # Straight to Browse from the header, the way the pills do it.
    window._set_simple_section("browse")

    window._go_back()
    assert window._current_section() == "process"


def test_a_deleted_section_is_dropped_from_the_history(window, monkeypatch):
    """It must not be restored, and must not cost an extra press to get past."""
    window._set_simple_section("process")
    window._go_to_section("videos")
    window._selected_pass_id = "no-longer-here"
    window._go_to_section("browse")
    depth = len(window._navigation_history())

    # The section has since been deleted, so Videos cannot show it.
    monkeypatch.setattr(window, "_select_section", lambda pass_id: False)

    window._go_back()

    # Landed somewhere real, without the dead entry being restored...
    assert window._selected_pass_id == "no-longer-here"  # nothing re-selected it
    assert len(window._navigation_history()) == depth - 1
    # ...and the way further back is still open.
    window._go_back()
    assert window._current_section() == "process"


def test_the_mouse_side_buttons_drive_it(window):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window._set_simple_section("process")
    window._go_to_section("videos")

    def press(button):
        return QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(0, 0),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )

    assert window._navigation_event_filter(window, press(Qt.MouseButton.BackButton))
    assert window._current_section() == "process"

    assert window._navigation_event_filter(window, press(Qt.MouseButton.ForwardButton))
    assert window._current_section() == "videos"


def test_alt_left_goes_back_but_not_out_of_a_text_field(window):
    """A text field has first claim on the shortcut it uses for word jumps."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    window._set_simple_section("process")
    window._go_to_section("videos")

    def alt_left():
        return QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier
        )

    # Headed for a field: left alone. Any QLineEdit will do -- the filter stands
    # down on the widget class, not on which field it happens to be.
    from PySide6.QtWidgets import QLineEdit

    assert not window._navigation_event_filter(QLineEdit(window), alt_left())
    assert window._current_section() == "videos"

    # Headed anywhere else: it navigates.
    assert window._navigation_event_filter(window, alt_left())
    assert window._current_section() == "process"
