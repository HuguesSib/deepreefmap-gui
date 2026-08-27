"""Who may change a catalogue row on this laptop.

A row made here is this laptop's until the console validates it. A row another
laptop made is read-only here. A row the console authored can still be edited,
and the registry keeps the change as a proposal for a curator.

Validation ends the conversation: once a curator has stamped a row, it is
changed in the console and nowhere else. The laptop shows what it holds and
offers no edit, rather than sending proposals against a row somebody has
already settled.
"""

from __future__ import annotations

from typing import Any

OTHER_DEVICE = "other_device"
VALIDATED = "validated"
PROPOSAL = "proposal"

READ_ONLY_NOTE = "Made on another laptop, so it cannot be changed here."
VALIDATED_NOTE = "Validated in the console, so it is changed there from now on."
CONSOLE_NOTE = (
    "Made in the console. A change made here is sent as a proposal for a curator "
    "to accept."
)


def lock_state(row: Any, own_device_id: str | None) -> str | None:
    """Why this laptop may not freely change a row, or None when it may.

    OTHER_DEVICE and VALIDATED are both read-only here and differ only in what
    they say; ``read_only`` is the question most callers are asking. PROPOSAL is
    editable, with the change decided by a curator.
    """
    owner = getattr(row, "device_id", None)
    if owner is not None and str(owner) != (own_device_id or ""):
        return OTHER_DEVICE
    if getattr(row, "validated_at", None):
        return VALIDATED
    if owner is None and getattr(row, "head_seq", None) is not None:
        return PROPOSAL
    return None


def read_only(row: Any, own_device_id: str | None) -> bool:
    """Whether this laptop must leave a row alone: another laptop's, or validated."""
    return lock_state(row, own_device_id) in (OTHER_DEVICE, VALIDATED)


def lock_note(row: Any, own_device_id: str | None) -> str:
    """The sentence a form shows for a locked row, or an empty string."""
    return {
        OTHER_DEVICE: READ_ONLY_NOTE,
        VALIDATED: VALIDATED_NOTE,
        PROPOSAL: CONSOLE_NOTE,
    }.get(lock_state(row, own_device_id) or "", "")


def own_device_id() -> str | None:
    """This laptop's registry id, or None when it has never been enrolled."""
    from deepreefmap_gui.sync import credentials

    try:
        held = credentials.load()
    except Exception:
        return None
    return held.device_id or None if held is not None else None
