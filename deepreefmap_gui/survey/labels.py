"""What a pass is called, as opposed to what its folder is called.

Generated when a pass is staged and editable from then on. Independent of the
run directory, which stays unique and filesystem-safe: renaming a pass never
moves a directory.

Names are unique. Two passes called the same thing cannot be told apart in the
row that reports one of them failing.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

# Trailing " 2", " 3" used to make a repeated name unique.
_SUFFIXED = re.compile(r"^(?P<stem>.*?)(?: (?P<n>\d+))?$")


def default_label(*, transect_name: str | None, clip_name: str, number: int) -> str:
    """The name a pass starts with: where it was swum, and which pass it is.

    A pass with no transect is named after its clip, which is the only thing
    it has to be recognised by. The number counts passes of the same subject, so
    two swims of one transect do not arrive with one name between them.
    """
    stem = (transect_name or "").strip() or Path(clip_name).stem or "Pass"
    return f"{stem} pass {number:02d}"


def unique_label(wanted: str, taken: set[str]) -> str:
    """`wanted`, or the first " 2", " 3"… that nobody else is using.

    Counts from an existing suffix rather than appending, so "Reef 2" becomes
    "Reef 3" and not "Reef 2 2".
    """
    cleaned = " ".join(wanted.split()) or "Pass"
    if cleaned not in taken:
        return cleaned
    match = _SUFFIXED.match(cleaned)
    stem = (match.group("stem") if match else cleaned) or cleaned
    start = int(match.group("n")) if match and match.group("n") else 1
    n = start + 1
    while f"{stem} {n}" in taken:
        n += 1
    return f"{stem} {n}"


def pass_label(pass_, *, transect_name: str | None, clip_name: str, number: int) -> str:
    """A pass's own name, or the generated one when it has never been named."""
    return pass_.label.strip() or default_label(
        transect_name=transect_name, clip_name=clip_name, number=number
    )


def taken_labels(passes, *, exclude: uuid.UUID | None = None) -> set[str]:
    """Names already spoken for, so a rename can be checked against them."""
    return {
        p.label.strip()
        for p in passes
        if p.label.strip() and (exclude is None or p.id != exclude)
    }
