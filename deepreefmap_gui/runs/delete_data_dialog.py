"""The dialog choosing what to remove from a run or session: data, record, or both."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import TEXT_MUTED, WARNING

__all__ = ["DeleteChoice", "DeleteScope", "DeleteDataDialog"]


class DeleteChoice(Enum):
    DATA = "data"
    METADATA = "metadata"
    BOTH = "both"


@dataclass(frozen=True)
class DeleteScope:
    """What each choice would remove, phrased for one run or a whole session."""

    title: str
    subject: str
    data_detail: str
    metadata_detail: str
    keeps: tuple[str, ...] = ()
    # False once the outputs are already gone, which leaves only the record.
    data_present: bool = True
    metadata_present: bool = True
    extra_notes: tuple[str, ...] = field(default=())


class DeleteDataDialog(QDialog):
    """Pick what goes. Nothing is touched until Delete is pressed."""

    def __init__(self, scope: DeleteScope, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(scope.title)
        self.setModal(True)
        self._group = QButtonGroup(self)
        self._choices: list[DeleteChoice] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        headline = QLabel(scope.subject)
        headline.setWordWrap(True)
        headline.setStyleSheet(f"color: {WARNING}; font-weight: 600;")
        layout.addWidget(headline)

        options = [
            (DeleteChoice.DATA, "Output data", scope.data_detail, scope.data_present),
            (
                DeleteChoice.METADATA,
                "Record only",
                scope.metadata_detail,
                scope.metadata_present,
            ),
            (
                DeleteChoice.BOTH,
                "Both",
                "The data and the record together.",
                scope.data_present,
            ),
        ]
        recommended = DeleteChoice.DATA if scope.data_present else DeleteChoice.METADATA
        for choice, name, detail, enabled in options:
            layout.addWidget(self._build_option(choice, name, detail, enabled, choice is recommended))

        for note in scope.extra_notes:
            label = QLabel(note)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {TEXT_MUTED};")
            layout.addWidget(label)
        if scope.keeps:
            keeps = QLabel("Kept either way:\n" + "\n".join(f"• {line}" for line in scope.keeps))
            keeps.setWordWrap(True)
            keeps.setStyleSheet(f"color: {TEXT_MUTED};")
            layout.addWidget(keeps)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Delete")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_option(self, choice: DeleteChoice, name: str, detail: str, enabled: bool, checked: bool) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        button = QRadioButton(name)
        button.setEnabled(enabled)
        button.setChecked(checked and enabled)
        self._group.addButton(button, len(self._choices))
        self._choices.append(choice)
        box.addWidget(button)

        label = QLabel(detail)
        label.setWordWrap(True)
        label.setEnabled(enabled)
        label.setStyleSheet(f"color: {TEXT_MUTED}; margin-left: 22px;")
        box.addWidget(label)
        return holder

    def selected(self) -> DeleteChoice | None:
        index = self._group.checkedId()
        return self._choices[index] if 0 <= index < len(self._choices) else None

    @staticmethod
    def ask(scope: DeleteScope, parent: QWidget | None = None) -> DeleteChoice | None:
        dialog = DeleteDataDialog(scope, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected()
