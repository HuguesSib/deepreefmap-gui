"""Everything this survey has reported, and the messages somebody silenced.

The one route back from "never show this again", which is why it is a view on a
page rather than a checkbox in a dialog.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import GUTTER, SPACE_SM, TEXT_MUTED
from deepreefmap_gui.core.widgets import (
    ColumnSpec,
    configure_table,
    install_column_sizer,
    muted_label,
    section_card,
)
from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    INFO,
    MACHINE,
    SURVEY,
    Notification,
)
from deepreefmap_gui.survey.models.notification import WARNING as SEVERITY_WARNING

_COLUMNS = ("When", "Severity", "What", "Where", "Cleared")

# The message is what a row is read for; the rest are stamps and short labels.
_COLUMN_SPEC = ColumnSpec(
    fixed={0: 128, 1: 88, 3: 120, 4: 112},
    weights={2: 1},
    minimums={2: 200},
)

# Label, then the value handed to the centre. "" means no filter.
_SEVERITY_CHOICES = (
    ("All", ""),
    ("Blocking", BLOCKER),
    ("Attention", SEVERITY_WARNING),
    ("Information", INFO),
)
_SCOPE_CHOICES = (("All", ""), ("This survey", SURVEY), ("This computer", MACHINE))

_SEVERITY_LABELS = {BLOCKER: "Blocking", SEVERITY_WARNING: "Attention", INFO: "Information"}
_SECTION_LABELS = {
    "transects": "Transects",
    "videos": "Videos",
    "process": "Cart",
    "browse": "Browse",
    "machine": "Setup",
}


class NotificationHistoryPanel(QWidget):
    """The log, and the messages this reader has silenced."""

    filters_changed = Signal()
    unmuted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GUTTER)

        card, layout = section_card("Activity")
        layout.addWidget(
            muted_label(
                "Everything this survey has reported. Cleared entries are kept so a "
                "problem that came and went can still be found."
            )
        )

        filters = QHBoxLayout()
        filters.setSpacing(SPACE_SM)
        self._severity = self._chooser(_SEVERITY_CHOICES, "Show only messages of one severity.")
        self._scope = self._chooser(_SCOPE_CHOICES, "Facts about this survey, or this computer.")
        filters.addWidget(QLabel("Severity"))
        filters.addWidget(self._severity)
        filters.addWidget(QLabel("About"))
        filters.addWidget(self._scope)
        filters.addStretch(1)
        layout.addLayout(filters)

        self._table = QTableWidget(0, len(_COLUMNS))
        configure_table(self._table, _COLUMNS)
        install_column_sizer(self._table, _COLUMN_SPEC, settings_key="notifications")
        layout.addWidget(self._table, 1)
        outer.addWidget(card, 1)

        self._muted_card, self._muted_layout = section_card("Silenced messages")
        outer.addWidget(self._muted_card)

    def _chooser(self, choices, tip: str) -> QComboBox:
        box = QComboBox()
        for label, value in choices:
            box.addItem(label, value)
        box.setToolTip(tip)
        box.currentIndexChanged.connect(self.filters_changed)
        return box

    def filters(self) -> tuple[str, str]:
        return self._severity.currentData(), self._scope.currentData()

    def set_history(self, notes: list[Notification], now: str) -> None:
        self._table.setRowCount(len(notes))
        for row, note in enumerate(notes):
            cells = (
                _stamp(note.created_at),
                _SEVERITY_LABELS.get(note.severity, note.severity),
                note.title,
                _SECTION_LABELS.get(note.section, ""),
                _cleared(note, now),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if note.body:
                    item.setToolTip(note.body)
                self._table.setItem(row, column, item)

    def set_muted(self, muted: list[tuple[str, str]]) -> None:
        while self._muted_layout.count() > 1:
            item = self._muted_layout.takeAt(1)
            old = item.widget() if item is not None else None
            if old is not None:
                old.setParent(None)
                old.deleteLater()
        if not muted:
            self._muted_layout.addWidget(
                muted_label("Nothing is silenced. Messages you clear here would say so.")
            )
            return
        for fingerprint, title in muted:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(SPACE_SM)
            label = QLabel(title)
            label.setStyleSheet(f"color: {TEXT_MUTED};")
            line.addWidget(label, 1)
            button = QPushButton("Show again")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("quiet", "true")
            button.clicked.connect(_emitter(self.unmuted, fingerprint))
            line.addWidget(button)
            self._muted_layout.addWidget(row)


def _emitter(signal, value: str) -> Callable[[], None]:
    return lambda: signal.emit(value)


def _stamp(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return iso


def _cleared(note: Notification, now: str) -> str:
    """How long an episode lasted, or blank while it is still true."""
    if note.resolved_at is None:
        return ""
    try:
        lasted = (
            datetime.fromisoformat(note.resolved_at) - datetime.fromisoformat(note.created_at)
        ).total_seconds()
    except ValueError:
        return "cleared"
    if lasted < 60:
        return "cleared after under a minute"
    if lasted < 3600:
        return f"cleared after {int(lasted // 60)} min"
    if lasted < 86400:
        return f"cleared after {int(lasted // 3600)} h"
    return f"cleared after {int(lasted // 86400)} d"
