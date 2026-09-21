"""Every status the interface can show: its label, colour role and filter bucket.

One row per status. The store validates against it, core/widgets.py colours from
it, and the browser builds its filters from it.

Plain dataclasses rather than an enum: this package supports Python 3.10, where
a str-mixin enum formats as ``RunStatus.FAILED`` rather than as its value, and
these keys are written to the database.
"""

from __future__ import annotations

from dataclasses import dataclass

# Colour roles. A status names a role, not a colour: the palette is a Qt-layer
# concern and this package is Qt-free.
TONE_GOOD = "good"  # finished, nothing to do
TONE_BAD = "bad"  # it went wrong
TONE_BUSY = "busy"  # in flight, or stopped short and worth redoing
TONE_IDLE = "idle"  # not started
TONE_QUIET = "quiet"  # stopped on purpose, so nothing is owed

TONES = (TONE_GOOD, TONE_BAD, TONE_BUSY, TONE_IDLE, TONE_QUIET)

# Filter buckets for the browser: what came of a run, coarser than its status.
OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_UNFINISHED = "succeeded", "failed", "unfinished"

RUN_OUTCOMES = (OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_UNFINISHED)


@dataclass(frozen=True)
class StatusSpec:
    """One status: its display label, colour role, filter bucket, and whether a
    RunRecord may carry it."""

    key: str
    label: str
    tone: str
    outcome: str
    persisted: bool = True

    def __post_init__(self) -> None:
        if self.tone not in TONES:
            raise ValueError(f"Unknown tone: {self.tone!r}")
        if self.outcome not in RUN_OUTCOMES:
            raise ValueError(f"Unknown outcome: {self.outcome!r}")


STATUSES: tuple[StatusSpec, ...] = (
    StatusSpec("pending", "Pending", TONE_IDLE, OUTCOME_UNFINISHED),
    StatusSpec("running", "Running", TONE_BUSY, OUTCOME_UNFINISHED),
    StatusSpec("succeeded", "Succeeded", TONE_GOOD, OUTCOME_SUCCEEDED),
    StatusSpec("failed", "Failed", TONE_BAD, OUTCOME_FAILED),
    StatusSpec("cancelled", "Cancelled", TONE_QUIET, OUTCOME_UNFINISHED),
    # A crash or a quit mid-run. The startup sweep reconciles rows stuck
    # non-terminal to this.
    StatusSpec("interrupted", "Interrupted", TONE_BUSY, OUTCOME_UNFINISHED),
    # Presentation states do not add persisted run statuses.
    StatusSpec("ready", "Ready", TONE_IDLE, OUTCOME_UNFINISHED, persisted=False),
    StatusSpec("queued", "In queue", TONE_IDLE, OUTCOME_UNFINISHED, persisted=False),
    # A run directory the database has no row for: the same event as interrupted
    # from the diver's side, so the same tone.
    StatusSpec("incomplete", "Incomplete", TONE_BUSY, OUTCOME_UNFINISHED, persisted=False),
)

_BY_KEY = {spec_.key: spec_ for spec_ in STATUSES}

DISPLAY_STATUSES = tuple(spec_.key for spec_ in STATUSES)

# What the store validates a RunRecord against.
RUN_STATUSES = tuple(spec_.key for spec_ in STATUSES if spec_.persisted)

# A run in any other persisted state was still going when the process stopped.
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def spec(status: str) -> StatusSpec:
    """The row for ``status``, or incomplete's row if this build does not know it."""
    return _BY_KEY.get(status, _BY_KEY["incomplete"])


def status_tone(status: str) -> str:
    return spec(status).tone


def status_label(status: str) -> str:
    return spec(status).label


def status_outcome(status: str) -> str:
    return spec(status).outcome


# --- Clips -------------------------------------------------------------------

# What is left to do with one piece of footage: a clip is several passes, and its
# state is what they add up to.
CLIP_UNPROCESSED, CLIP_PENDING, CLIP_FAILED, CLIP_PROCESSED = (
    "unprocessed",
    "pending",
    "failed",
    "processed",
)


@dataclass(frozen=True)
class ClipSpec:
    """One clip outcome, in the same tones the run statuses use."""

    key: str
    label: str
    tone: str


CLIP_OUTCOMES: tuple[ClipSpec, ...] = (
    ClipSpec(CLIP_UNPROCESSED, "To organise", TONE_IDLE),
    ClipSpec(CLIP_PENDING, "To process", TONE_IDLE),
    ClipSpec(CLIP_FAILED, "Needs attention", TONE_BAD),
    ClipSpec(CLIP_PROCESSED, "Complete", TONE_GOOD),
)

_CLIP_BY_KEY = {spec_.key: spec_ for spec_ in CLIP_OUTCOMES}


def clip_spec(outcome: str) -> ClipSpec:
    return _CLIP_BY_KEY[outcome]
