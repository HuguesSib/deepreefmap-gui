"""Who may change a catalogue row on this laptop.

The rule decides what every catalogue form offers, so it is tested on its own
rather than only through the pages that ask it.
"""

from __future__ import annotations

import uuid

from deepreefmap_gui.survey.models import Site
from deepreefmap_gui.survey.ownership import (
    CONSOLE_NOTE,
    OTHER_DEVICE,
    PROPOSAL,
    READ_ONLY_NOTE,
    VALIDATED,
    VALIDATED_NOTE,
    lock_note,
    lock_state,
    read_only,
)

MINE = str(uuid.uuid4())


def a_row(**fields) -> Site:
    return Site(name="Japanese Garden", **fields)


def test_a_row_this_laptop_made_is_its_own():
    row = a_row(device_id=uuid.UUID(MINE))

    assert lock_state(row, MINE) is None
    assert not read_only(row, MINE)
    assert lock_note(row, MINE) == ""


def test_a_row_never_sent_anywhere_is_its_own():
    """A laptop that has never met a registry owns everything on it."""
    row = a_row()

    assert lock_state(row, None) is None
    assert not read_only(row, None)


def test_a_row_another_laptop_made_is_read_only():
    row = a_row(device_id=uuid.uuid4())

    assert lock_state(row, MINE) == OTHER_DEVICE
    assert read_only(row, MINE)
    assert lock_note(row, MINE) == READ_ONLY_NOTE


def test_a_console_row_is_editable_and_goes_up_as_a_proposal():
    """Authored in the console but not yet settled: the field may still correct
    it, and the registry keeps the correction for a curator."""
    row = a_row(device_id=None, head_seq=12)

    assert lock_state(row, MINE) == PROPOSAL
    assert not read_only(row, MINE)
    assert lock_note(row, MINE) == CONSOLE_NOTE


def test_a_validated_row_is_still_editable_here():
    """Scenario: a curator has stamped the row, and a depth is measured after.

    Expected behaviour: the laptop offers the edit and says where it goes. The
    registry records a push against a validated row as a proposal.
    """
    row = a_row(device_id=None, head_seq=12, validated_at="2026-08-20T00:00:00+00:00")

    assert lock_state(row, MINE) == VALIDATED
    assert not read_only(row, MINE)
    assert lock_note(row, MINE) == VALIDATED_NOTE


def test_validation_leaves_a_row_this_laptop_made_editable():
    row = a_row(device_id=uuid.UUID(MINE), validated_at="2026-08-20T00:00:00+00:00")

    assert not read_only(row, MINE)
    assert lock_note(row, MINE) == VALIDATED_NOTE


def test_another_laptop_outranks_validation():
    """The nearer reason wins: it is not this laptop's row in the first place,
    which is read-only where validation is not."""
    row = a_row(device_id=uuid.uuid4(), validated_at="2026-08-20T00:00:00+00:00")

    assert lock_state(row, MINE) == OTHER_DEVICE
    assert read_only(row, MINE)


def test_an_unenrolled_laptop_still_reads_another_laptops_row_as_theirs():
    row = a_row(device_id=uuid.uuid4())

    assert read_only(row, None)
