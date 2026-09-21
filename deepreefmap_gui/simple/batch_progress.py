"""The session total, under the queue it totals.

Fed the same per-run signals as the viewer's ProgressPanel, but scoped to the
whole batch: the bar spans every queued pass and the estimate carries on past
the one being processed.

The estimate is built before the batch starts, from each pass's own length and
this machine's learned rates (profiling/batch_estimate.py), and calibrated by
what the batch measures as it goes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from deepreefmap_gui.core.theme import PRIMARY, TEXT_SECONDARY
from deepreefmap_gui.core.widgets import secondary_label, section_card
from deepreefmap_gui.profiling.batch_estimate import (
    BatchEtaTracker,
    BatchPrediction,
    coverage_clause,
)
from deepreefmap_gui.profiling.eta import format_remaining


class BatchProgressCard(QWidget):
    """Overall progress of a survey batch, sized in passes rather than frames."""

    pass_percent_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, layout = section_card()
        outer.addWidget(card)

        self._heading = QLabel()
        self._heading.setStyleSheet(f"color: {PRIMARY}; font-weight: bold;")
        layout.addWidget(self._heading)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)

        self._eta = secondary_label()
        layout.addWidget(self._eta)

        self._detail = QLabel()
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._detail)

        self._index = 0
        self._total = 0
        self._pass_percent = 0
        self._prediction: BatchPrediction | None = None
        self._tracker: BatchEtaTracker | None = None
        self.set_idle("No batch in progress.")

    # --- Batch shape ---

    def set_batch_plan(self, prediction: BatchPrediction) -> None:
        """What this batch holds, and what each pass in it is expected to cost."""
        self._prediction = prediction
        self._tracker = BatchEtaTracker(prediction)
        self._total = len(prediction.passes)

    def set_batch_context(self, index: int, total: int, name: str) -> None:
        self._index = index
        self._total = total
        self._pass_percent = 0
        if self._tracker is not None:
            self._tracker.start_pass(index - 1)
        self._heading.setText(f"Processing pass {index} of {total} · {name}")
        self._heading.setVisible(True)
        self._bar.setVisible(True)
        self._render()

    def pass_finished(self, index: int, seconds: float) -> None:
        """What a pass really cost, which rescales the passes still to come."""
        if self._tracker is not None:
            self._tracker.finish_pass(index - 1, seconds)
            self._render()

    def clear_batch_context(self) -> None:
        self._index = 0
        self._heading.setVisible(False)

    # --- Live run signals ---

    def set_percent(self, percent: int) -> None:
        self._pass_percent = max(0, min(100, int(percent)))
        self.pass_percent_changed.emit(self._pass_percent)
        if self._tracker is not None:
            self._tracker.set_pass_percent(self._pass_percent)
        self._render()

    def set_eta_seconds(self, seconds: float | None) -> None:
        if self._tracker is not None:
            self._tracker.set_pass_remaining(seconds)
        self._render()

    def set_eta(self, text: str) -> None:
        """The pass-scoped wording; the card shows a batch-scoped one instead."""

    def set_status_html(self, text: str) -> None:
        self._detail.setText(text)

    def set_idle(self, message: str) -> None:
        self._heading.setVisible(False)
        self._bar.setValue(0)
        self._bar.setVisible(False)
        self._eta.setText(self._planned_text())
        self._detail.setText(f'<span style="color: {TEXT_SECONDARY};">{message}</span>')
        self._index = 0
        self._pass_percent = 0

    # --- Rendering ---

    def _pass_weights(self) -> list[float]:
        """Each pass's share of the session, by predicted seconds.

        Equal weight only where a pass has no figure. Counting passes instead
        put a 67% bar above "40m left" on a queue of 120/120/2400s.
        """
        prediction = self._prediction
        if prediction is None or prediction.total_s is None:
            return [1.0] * self._total
        weights = [prediction.seconds_for(p.key) for p in prediction.passes]
        known = [w for w in weights if w]
        typical = (sum(known) / len(known)) if known else 1.0
        return [float(w) if w else typical for w in weights]

    def _batch_percent(self) -> int:
        if self._total <= 0 or self._index <= 0:
            return 0
        weights = self._pass_weights()
        if len(weights) != self._total:
            weights = [1.0] * self._total
        total = sum(weights) or 1.0
        done = sum(weights[: self._index - 1]) + weights[self._index - 1] * self._pass_percent / 100.0
        return int(round(100.0 * done / total))

    def batch_remaining_s(self) -> float | None:
        """Seconds left across every pass still to process, or None if unknown."""
        return self._tracker.remaining_s() if self._tracker is not None else None

    def _planned_text(self) -> str:
        """What the queue is expected to cost, before any of it has run."""
        prediction = self._prediction
        if prediction is None or prediction.total_s is None:
            return ""
        text = f"{format_remaining(prediction.total_s)} for this session"
        clause = prediction.coverage_clause()
        return f"{text}, {clause}" if clause else text

    def _render(self) -> None:
        self._bar.setValue(self._batch_percent())
        remaining = self.batch_remaining_s()
        if remaining is None:
            self._eta.setText("No timings for these models yet.")
            return
        text = f"{format_remaining(remaining)} left for the batch"
        if self._tracker is not None:
            # The same caveat the planned figure carries: passes with no basis
            # contribute nothing to this sum and must not read as free.
            clause = coverage_clause(*self._tracker.pending_coverage())
            if clause:
                text = f"{text}, {clause}"
        self._eta.setText(text)
