"""The session card: the bar and the clock beneath it must be one quantity.

Scenario: a queue whose passes are nothing like equal in length.
"""

from __future__ import annotations

from deepreefmap_gui.profiling.batch_estimate import (
    BASIS_EXACT,
    BatchPrediction,
    PassPrediction,
)
from deepreefmap_gui.simple.batch_progress import BatchProgressCard


def _plan(*seconds: float | None) -> BatchPrediction:
    passes = [
        PassPrediction(key=str(i), seconds=value, basis=BASIS_EXACT)
        for i, value in enumerate(seconds)
    ]
    known = [s for s in seconds if s is not None]
    return BatchPrediction(
        passes=passes,
        total_s=sum(known) if known else None,
        predicted_count=len(known),
        unknown_count=len(seconds) - len(known),
    )


def test_the_bar_is_weighted_by_length_not_by_pass_count(qapp) -> None:
    # Counting passes put a 67% bar above "40m left" on a 44-minute session.
    card = BatchProgressCard()
    card.set_batch_plan(_plan(120.0, 120.0, 2400.0))
    card.set_batch_context(3, 3, "the long one")
    assert card._bar.value() < 15


def test_equal_passes_still_read_as_thirds(qapp) -> None:
    card = BatchProgressCard()
    card.set_batch_plan(_plan(100.0, 100.0, 100.0))
    card.set_batch_context(3, 3, "third")
    assert 60 <= card._bar.value() <= 70


def test_the_live_line_says_how_much_of_the_queue_it_covers(qapp) -> None:
    card = BatchProgressCard()
    card.set_batch_plan(_plan(100.0, None, None))
    card.set_batch_context(1, 3, "first")
    assert "covering 1 of 3 passes" in card._eta.text()
