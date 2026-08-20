"""The Connect dialog: one pasted code, nothing else.

The dialog collects and reports; it never enrols. Enrolment is network I/O, so the
window runs it on a worker thread and drives this through `working()` and
`show_failure()`, which is also why the dialog stays open until it has an answer:
a refused code is fixed by editing what is already typed here.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.spinner import BusySpinner
from deepreefmap_gui.core.theme import ERROR, TEXT_MUTED, WEIGHT_SEMIBOLD
from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.sync.connect_code import CODE_PREFIX, ConnectCodeError, decode_connect_code

TITLE = "Connect to server"

INTRO = "Paste the connect code from the registry's web interface. It enrols this installation, not you."

# The device starts under the machine's own name; naming and renaming are web
# interface actions, so attribution is never mutable from the device it names.
NAME_NOTE = (
    "This computer enrols under its own machine name. Rename it in the "
    "registry's web interface if that names a person."
)

CONNECT = "Connect"
CONNECTING = "Connecting…"

# The address the code carries, shown before the button works.
SERVER_LABEL = "Connects to"
UNREADABLE = "That is not a connect code."


class ConnectDialog(QDialog):
    """Collects a connect code, and reports what the window made of it."""

    # The pasted code, once. Emitted rather than read off the dialog so the code
    # lives in the handler's arguments, not in dialog state.
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setModal(True)

        layout = QVBoxLayout(self)

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._code_edit = QPlainTextEdit()
        # Multiline: a code is a couple of hundred characters and arrives wrapped
        # out of an email or a chat window.
        self._code_edit.setPlaceholderText(f"{CODE_PREFIX}…")
        self._code_edit.setFixedHeight(88)
        self._code_edit.textChanged.connect(self._sync_connect_enabled)
        layout.addWidget(self._code_edit)

        self._server = QLabel("")
        self._server.setWordWrap(True)
        self._server.setVisible(False)
        layout.addWidget(self._server)

        note = muted_label(NAME_NOTE)
        note.setWordWrap(True)
        layout.addWidget(note)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setVisible(False)
        layout.addWidget(self._message)

        busy_row = QHBoxLayout()
        busy_row.setContentsMargins(0, 0, 0, 0)
        self._spinner = BusySpinner()
        self._spinner.setVisible(False)
        busy_row.addWidget(self._spinner)
        self._busy_label = muted_label("")
        busy_row.addWidget(self._busy_label, 1)
        layout.addLayout(busy_row)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._connect_btn = QPushButton(CONNECT)
        self._buttons.addButton(self._connect_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        self._connect_btn.setProperty("cta", "true")
        self._connect_btn.setDefault(True)
        self._connect_btn.setEnabled(False)
        # Not accepted: the dialog closes when the enrolment succeeds, not when
        # the button is pressed.
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self.resize(480, 320)

    def code(self) -> str:
        return self._code_edit.toPlainText().strip()

    def working(self, busy: bool) -> None:
        """Lock the fields while the enrolment is in flight."""
        self._spinner.setVisible(busy)
        self._busy_label.setText(CONNECTING if busy else "")
        self._connect_btn.setText(CONNECTING if busy else CONNECT)
        self._code_edit.setReadOnly(busy)
        if busy:
            self._connect_btn.setEnabled(False)
            self._message.setVisible(False)
        else:
            self._sync_connect_enabled()

    def show_failure(self, title: str, detail: str) -> None:
        """Say why it did not work, and leave what was typed alone to be fixed."""
        self.working(False)
        self._message.setStyleSheet(f"color: {ERROR}; font-weight: {WEIGHT_SEMIBOLD};")
        self._message.setText(f"{title}. {detail}")
        self._message.setVisible(True)

    def _on_connect_clicked(self) -> None:
        code = self.code()
        if not code:
            return
        self.working(True)
        self.submitted.emit(code)

    def _sync_connect_enabled(self) -> None:
        """Decode what has been typed, name the server, and gate the button on it.

        A plain-http address outside this machine is refused: the token would cross
        that network in the clear.
        """
        typed = self.code()
        if not typed:
            self._server.setVisible(False)
            self._connect_btn.setEnabled(False)
            return

        try:
            code = decode_connect_code(typed)
        except ConnectCodeError:
            self._say(UNREADABLE, bad=True)
            self._connect_btn.setEnabled(False)
            return

        if code.insecure_transport:
            self._say(f"{code.base_url}. {code.warning}", bad=True)
            self._connect_btn.setEnabled(False)
            return

        self._say(f"{SERVER_LABEL} {code.base_url}", bad=False)
        self._connect_btn.setEnabled(not self._code_edit.isReadOnly())

    def _say(self, text: str, *, bad: bool) -> None:
        colour = ERROR if bad else TEXT_MUTED
        self._server.setStyleSheet(f"color: {colour};")
        self._server.setText(text)
        self._server.setVisible(True)
