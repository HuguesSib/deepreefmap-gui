"""Who may change a catalogue row on this laptop.

A row made here is this laptop's until the console validates it. A row another
laptop made is read-only here. A row the console authored or validated can still
be edited, but the registry keeps the change as a proposal for a curator.
"""

from __future__ import annotations

from typing import Any

OTHER_DEVICE = "other_device"
PROPOSAL = "proposal"

READ_ONLY_NOTE = "Made on another laptop, so it cannot be changed here."
PROPOSAL_NOTE = (
    "Validated in the console. A change made here is sent as a proposal for a "
    "curator to accept."
)
CONSOLE_NOTE = (
    "Made in the console. A change made here is sent as a proposal for a curator "
    "to accept."
)


def lock_state(row: Any, own_device_id: str | None) -> str | None:
    """OTHER_DEVICE for a row this laptop may not change, PROPOSAL for one whose
    change the console decides on, None for a row that is this laptop's own."""
    owner = getattr(row, "device_id", None)
    if owner is not None and str(owner) != (own_device_id or ""):
        return OTHER_DEVICE
    if getattr(row, "validated_at", None):
        return PROPOSAL
    if owner is None and getattr(row, "head_seq", None) is not None:
        return PROPOSAL
    return None


def lock_note(row: Any, own_device_id: str | None) -> str:
    """The sentence a form shows for a locked row, or an empty string."""
    state = lock_state(row, own_device_id)
    if state == OTHER_DEVICE:
        return READ_ONLY_NOTE
    if state == PROPOSAL:
        return PROPOSAL_NOTE if getattr(row, "validated_at", None) else CONSOLE_NOTE
    return ""


def own_device_id() -> str | None:
    """This laptop's registry id, or None when it has never been enrolled."""
    from deepreefmap_gui.sync import credentials

    try:
        held = credentials.load()
    except Exception:
        return None
    return held.device_id or None if held is not None else None
