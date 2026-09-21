"""One traversal of a transect: a time-trimmed segment of one or more videos."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso

PASS_DIRECTIONS = ("forward", "reverse")

# Which way along the tape, as a glyph. The arrow is what carries the direction
# where colour cannot: on a selected row, and for a reader who sees neither.
DIRECTION_ARROWS = {"forward": "\u2192", "reverse": "\u2190"}


def direction_arrow(direction: str | None) -> str:
    """The arrow for ``direction``, or an empty string for anything else."""
    return DIRECTION_ARROWS.get((direction or "").strip().lower(), "")


def direction_text(direction: str | None) -> str:
    """The arrow and the word, as every surface in the app spells it."""
    key = (direction or "").strip().lower()
    arrow = DIRECTION_ARROWS.get(key)
    return f"{arrow} {key.capitalize()}" if arrow else ""


# What an unrecorded direction is called wherever a choice is offered.
DIRECTION_UNRECORDED = "Not recorded"


def direction_phrase(direction: str | None) -> str:
    """The direction inside a sentence: "forward", or "an unrecorded direction"."""
    key = (direction or "").strip().lower()
    return key if key in PASS_DIRECTIONS else "an unrecorded direction"

# The diver's assessment, on the fixed scale that normalises the free text in the
# field spreadsheets ("meh", "good/meh", "very bad"). None is not assessed.
PASS_QUALITIES = ("excellent", "very_good", "good", "meh", "bad", "very_bad")


@dataclass(slots=True)
class TransectPass:
    """One swim, which one video may hold several of, or several videos hold one of.

    A GoPro splits a recording at about 4 GB, so a long swim arrives as
    chapters. ``video_id`` is the first of them and stays the pass's video
    identity for everything that groups by clip; ``extra_video_ids`` holds the
    rest in playing order. ``begin_s`` and ``end_s`` are offsets into the
    chapters played back to back, which is how the pipeline reads a list of
    videos.

    ``transect_id`` may be None. Footage worth processing is not always footage
    laid against a tape: a spot check, a clip from a colleague, a swim nobody
    planned. Such a pass carries no tape length, so its run is left unscaled --
    which is already what a planned transect with no tape reading does.
    """

    transect_id: uuid.UUID | None
    video_id: uuid.UUID
    begin_s: float
    end_s: float
    # None is not recorded, which is a fact of its own.
    direction: str | None = None
    batch_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    quality: str | None = None
    # The day of the swim, ISO ``YYYY-MM-DD``, from the clip's capture stamp
    # unless corrected.
    surveyed_on: str | None = None
    notes: str = ""
    # What a person calls this section. Empty means nobody has named it, which
    # reads as the generated default rather than as a blank -- storing the
    # generated text would freeze today's generator into every old row.
    label: str = ""
    extra_video_ids: list[uuid.UUID] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    # The registry position this row was last seen at, sent back as base_seq.
    head_seq: int | None = None
    validated_at: str | None = None
    validated_by: str | None = None

    def __post_init__(self) -> None:
        if self.direction is not None and self.direction not in PASS_DIRECTIONS:
            raise ValueError(f"direction must be one of {PASS_DIRECTIONS}, got {self.direction!r}")
        if self.quality is not None and self.quality not in PASS_QUALITIES:
            raise ValueError(f"quality must be one of {PASS_QUALITIES}, got {self.quality!r}")
        if self.begin_s < 0:
            raise ValueError(f"begin_s must be >= 0, got {self.begin_s}")
        if self.end_s <= self.begin_s:
            raise ValueError(f"end_s must be greater than begin_s ({self.begin_s})")
        if self.video_id in self.extra_video_ids:
            raise ValueError("extra_video_ids must not repeat video_id")

    def video_ids(self) -> list[uuid.UUID]:
        """Every chapter of the pass, in playing order."""
        return [self.video_id, *self.extra_video_ids]

    def duration_s(self) -> float:
        return self.end_s - self.begin_s
