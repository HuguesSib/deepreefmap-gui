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


def test_a_validated_row_is_read_only_here():
    """Scenario: a curator has stamped the row in the console.

    Expected behaviour: the laptop stops offering an edit rather than sending a
    proposal against something already settled.
    """
    row = a_row(device_id=None, head_seq=12, validated_at="2026-08-20T00:00:00+00:00")

    assert lock_state(row, MINE) == VALIDATED
    assert read_only(row, MINE)
    assert lock_note(row, MINE) == VALIDATED_NOTE


def test_validation_locks_a_row_this_laptop_made():
    """Authorship does not survive validation: a site made in the field is the
    console's to change once a curator has accepted it."""
    row = a_row(device_id=uuid.UUID(MINE), validated_at="2026-08-20T00:00:00+00:00")

    assert read_only(row, MINE)
    assert lock_note(row, MINE) == VALIDATED_NOTE


def test_another_laptop_outranks_validation_in_what_it_says():
    """Both are read-only, so only the sentence differs. The nearer reason wins:
    it is not this laptop's row in the first place."""
    row = a_row(device_id=uuid.uuid4(), validated_at="2026-08-20T00:00:00+00:00")

    assert lock_state(row, MINE) == OTHER_DEVICE
    assert read_only(row, MINE)


def test_an_unenrolled_laptop_still_reads_another_laptops_row_as_theirs():
    row = a_row(device_id=uuid.uuid4())

    assert read_only(row, None)
