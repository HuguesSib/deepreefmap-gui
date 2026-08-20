"""The run settings dialog, and the live form it edits.

Rather than keep a second copy of the run form, the dialog borrows the real one
out of the hidden holder that owns it and hands it back when it closes. The form
is never on screen otherwise, so this is the only place its settings are edited,
and every setting stays a single widget with a single value.

Borrowing the live form means the widgets are edited in place, so Cancel cannot
simply drop a pending copy. The caller snapshots the settings before opening and
puts them back when the dialog is rejected, which is why the dialog only reports
its result and never persists anything itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class RunSettingsDialog(QDialog):
    """Hosts the borrowed run form until it is closed."""

    def __init__(
        self,
        window,
        form: QWidget,
        per_run: list[QWidget],
        *,
        title: str = "Run settings",
        reset_label: str = "Restore standard settings",
        on_reset: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._form = form
        self._per_run = per_run
        self._restored = False

        self.setWindowTitle(title)
        self.setModal(True)
        layout = QVBoxLayout(self)

        # The form is taller than most screens, so it scrolls inside the dialog.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Reset
        )
        # Named for what they do to the settings. "OK" and "Cancel" say nothing
        # about whether the edit is kept, which is the only question here.
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Save settings")
        ok.setProperty("cta", "true")
        ok.setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Discard")
        reset = buttons.button(QDialogButtonBox.StandardButton.Reset)
        # For the session, the standard is the organisation preset, not the
        # values a fresh window happens to construct: those two drifted apart
        # the moment a preset shipped, and only one of them is the configuration
        # anybody blessed. For one pass's settings it is the session's own,
        # which is what the caller passes instead.
        reset.setText(reset_label)
        reset.setToolTip(f"Put every setting back: {reset_label.lower()}.")
        # Restore writes into the live form like every other edit here, so Cancel
        # still undoes it, and only OK persists the machine override.
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setToolTip(
            "Close without keeping any of these changes."
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        reset.clicked.connect(
            self._window._load_standard_into_form if on_reset is None else on_reset
        )
        layout.addWidget(buttons)

        # Per-run values come from the pass table on the Run step, so showing
        # them here would invite edits that go nowhere.
        for widget in per_run:
            widget.setVisible(False)
        self._size_to_form(layout, scroll, buttons)

    def _size_to_form(
        self, layout: QVBoxLayout, scroll: QScrollArea, buttons: QDialogButtonBox
    ) -> None:
        """Open at the size the form asks for, up to what the screen allows.

        The scroll area is the fallback for a form taller than the display, not
        the normal way to read it: a settings page that opens already scrolled
        hides whichever section happens to be last.
        """
        form = self._form
        form.adjustSize()
        margins = layout.contentsMargins()
        chrome = (
            margins.top()
            + margins.bottom()
            + layout.spacing()
            + buttons.sizeHint().height()
            + 2 * scroll.frameWidth()
        )
        hint = form.sizeHint()
        # Between the width the form was designed at and the width its longest
        # sentence would like: past that the dialog is wide rather than readable.
        width = max(560, min(hint.width(), 620))
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        if available is not None:
            width = min(width, int(available.width() * 0.9))
        # The wrapped labels are shorter at their natural width than at this one,
        # so the height is asked for at the width the form will actually get.
        inner = width - 2 * scroll.frameWidth() - margins.left() - margins.right()
        form_layout = form.layout()
        wrapped = form_layout.heightForWidth(inner) if form_layout is not None else -1
        height = max(hint.height(), wrapped) + chrome
        if available is not None:
            height = min(height, int(available.height() * 0.9))
        self.resize(width, height)

    def restore_form(self) -> None:
        """Put the form back in its holder. Safe to call more than once."""
        if self._restored:
            return
        self._restored = True
        for widget in self._per_run:
            widget.setVisible(True)
        self._window._form_home_layout.addWidget(self._form)

    def done(self, result: int) -> None:
        # Covers OK, Cancel and Escape: the form must never be left inside a
        # dialog that is going away.
        self.restore_form()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # close() on a dialog that was never shown skips done() entirely.
        self.restore_form()
        super().closeEvent(event)
