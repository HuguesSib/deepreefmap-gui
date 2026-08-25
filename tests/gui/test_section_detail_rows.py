"""The list of sessions a pass has run in, read down rather than across."""

from __future__ import annotations

import pytest

from deepreefmap_gui.runs.section_detail import RunRow
from deepreefmap_gui.survey.models import RunRecord


@pytest.fixture
def run():
    return RunRecord(
        pass_id=None, run_dir_name="20260825-101500", status="succeeded",
        created_at="2026-08-25T10:15:00+00:00",
    )


def test_a_session_named_after_its_day_does_not_print_the_date_twice(qapp, run) -> None:
    """Sessions default to the day they ran, so the created date beside the name
    was the same string said again."""
    row = RunRow(run, "2026-08-25", "")

    assert row.label.text() == "2026-08-25"
    assert row.date.text() == ""


def test_a_session_named_something_else_keeps_its_date(qapp, run) -> None:
    row = RunRow(run, "Reef survey", "")

    assert row.date.text() == "2026-08-25"


def test_the_outcome_and_date_hold_the_same_position_whatever_the_row_says(qapp, run) -> None:
    """Expected behaviour: the columns are measured against the widest word each
    can ever hold, so a failed run and a succeeded one line up down the list."""
    failed = RunRecord(
        pass_id=None, run_dir_name="20260811-090000", status="failed",
        created_at="2026-08-11T09:00:00+00:00",
    )
    succeeded = RunRow(run, "Reef survey", "")
    unsucceeded = RunRow(failed, "Reef survey", "")

    assert succeeded.outcome.width() == unsucceeded.outcome.width()
    assert succeeded.date.width() == unsucceeded.date.width()


def test_the_outcome_is_the_word_the_rest_of_the_app_uses(qapp, run) -> None:
    """Not the raw status: every other surface labels it, and this one printed
    the database value."""
    from deepreefmap_gui.survey import statuses

    assert RunRow(run, "Reef survey", "").outcome.text() == statuses.status_label("succeeded")


def test_a_long_session_name_gives_way_rather_than_the_figures(qapp, run) -> None:
    """Expected behaviour: the name elides, the outcome and the date do not.

    Shown rather than merely resized: Qt delivers no resize event to a hidden
    widget, and the elide follows the width the label was actually drawn at.
    """
    row = RunRow(run, "A session name far longer than the pane it has to fit into", "")
    row.resize(400, 24)
    row.show()
    qapp.processEvents()
    try:
        assert row.label.text().endswith("…")
        assert row.date.text() == "2026-08-25"
    finally:
        row.hide()
