"""The Activity view: the log, and the way back from "never show this again"."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from deepreefmap_gui.notify.center_ui import MUTED_KEY
from deepreefmap_gui.simple.machine import MACHINE_VIEWS
from deepreefmap_gui.simple.section_state import CAUSE_UNFILED_RUNS, browse_state
from deepreefmap_gui.survey.models.notification import BLOCKER, MACHINE


@pytest.fixture
def reported(window):
    """A survey with one thing to report, and no mutes left behind."""
    window._browse_state = browse_state(19, 17)
    window._section_state_cache = None
    window._refresh_section_state()
    yield window
    window._settings.remove(MUTED_KEY)


def test_activity_is_a_view_on_setup_rather_than_a_dialog(window):
    """Four ways of looking at one computer, plus the record of what it said."""
    assert "activity" in MACHINE_VIEWS
    window._set_machine_view("activity")
    assert window._machine_view == "activity"


def test_the_log_lists_what_was_reported(reported):
    reported._set_machine_view("activity")
    table = reported._activity_panel._table

    assert table.rowCount() == 1
    assert table.item(0, 2).text() == "17 runs belong to no transect"
    assert table.item(0, 3).text() == "Results"
    assert table.item(0, 4).text() == ""


def test_every_section_a_message_is_filed_against_has_a_where(window):
    """A pass with no label renders a blank Where cell, saying nothing."""
    from deepreefmap_gui.models.cache_ui import MODELS_SECTION
    from deepreefmap_gui.notify.history_ui import _SECTION_LABELS
    from deepreefmap_gui.server.state import SERVER_SECTION
    from deepreefmap_gui.simple.mode import DESTINATIONS

    for section in (*DESTINATIONS, "machine", SERVER_SECTION, MODELS_SECTION):
        assert _SECTION_LABELS.get(section), section


def test_a_message_about_the_registry_says_so_in_the_log(window):
    window._notify_post(
        {
            "fingerprint": "sync.local_edit_overwritten",
            "title": "The registry replaced 1 edited row(s)",
            "section": "server",
        }
    )

    window._set_machine_view("activity")

    assert window._activity_panel._table.item(0, 3).text() == "Server"


def test_a_cleared_episode_says_how_long_it_lasted(reported):
    reported._browse_state = browse_state(19, 0)
    reported._section_state_cache = None
    reported._refresh_section_state()

    reported._set_machine_view("activity")

    assert "cleared after" in reported._activity_panel._table.item(0, 4).text()


def test_the_log_filters_by_severity(reported):
    reported._set_machine_view("activity")
    panel = reported._activity_panel
    panel._severity.setCurrentIndex(
        [panel._severity.itemData(i) for i in range(panel._severity.count())].index(BLOCKER)
    )

    assert panel._table.rowCount() == 0


def test_the_log_separates_the_survey_from_the_computer(reported):
    reported._set_machine_view("activity")
    panel = reported._activity_panel
    panel._scope.setCurrentIndex(
        [panel._scope.itemData(i) for i in range(panel._scope.count())].index(MACHINE)
    )

    assert panel._table.rowCount() == 0


def test_a_silenced_message_can_be_asked_for_again(reported):
    """Expected behaviour: this is the only route back, so it has to work from
    the panel alone, without touching the centre."""
    reported._on_notification_muted(CAUSE_UNFILED_RUNS)
    reported._set_machine_view("activity")
    panel = reported._activity_panel

    buttons = [b for b in panel._muted_card.findChildren(QPushButton) if b.text() == "Show again"]
    assert len(buttons) == 1
    buttons[0].click()

    assert reported._notify.muted() == []
    assert len(reported._notify.active()) == 1
    assert reported._notify_bell._unread == 1


def test_nothing_silenced_says_so(reported):
    reported._set_machine_view("activity")

    said = [w.text() for w in reported._activity_panel._muted_card.findChildren(QLabel)]
    assert any("Nothing is silenced" in text for text in said)
    assert reported._activity_panel._muted_card.findChildren(QPushButton) == []


def test_the_popover_links_to_the_history(reported):
    reported._toggle_notification_popover()
    reported._notify_popover.history_requested.emit()

    assert reported._current_section() == "machine"
    assert reported._machine_view == "activity"
