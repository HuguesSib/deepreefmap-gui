"""Log console widget plus stdout/stderr capture routed into the GUI."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, SignalInstance
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import FONT_SM_PT, PREVIEW_BG, TEXT_SECONDARY
from deepreefmap_gui.core.widgets import muted_label

logger = logging.getLogger(__name__)

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
_MAX_LINES = 5000


class _LogSignal(QObject):
    # QObject lives in the main thread; emitting the Qt signal cross-thread is
    # the documented thread-safe way to push text into a widget.
    line = Signal(str)


class QtLogHandler(logging.Handler):
    """Logging handler that pumps formatted records into a Qt signal."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        self._signal = _LogSignal()

    @property
    def line_signal(self) -> SignalInstance:
        return self._signal.line

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        try:
            self._signal.line.emit(msg)
        except RuntimeError:
            pass  # signal owner already deleted (window teardown)


class LogView(QWidget):
    """Collapsible log panel: the live log, or a finished run's log file.

    The live log is in memory and belongs to this session. `show_file` puts a
    stored run.log in the same panel rather than handing it to whatever the
    desktop opens .log files with.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_log_path: Path | None = None
        # The live log's own file, held while a stored one is on screen.
        self._live_log_path: Path | None = None
        self._showing_file = False
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        # Says which log is on screen, because a stored one and the live one look
        # identical once they are both just lines of text.
        self._heading = muted_label("")
        toolbar.addWidget(self._heading)
        toolbar.addStretch(1)
        self._live_btn = QPushButton("Back to the live log")
        self._live_btn.clicked.connect(self.show_live)
        self._live_btn.setVisible(False)
        toolbar.addWidget(self._live_btn)
        self._open_log_btn = QPushButton("Open log file")
        self._open_log_btn.clicked.connect(self._open_current_log)
        self._open_log_btn.setEnabled(False)
        toolbar.addWidget(self._open_log_btn)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self.clear)
        toolbar.addWidget(self._clear_btn)
        layout.addLayout(toolbar)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_LINES)
        from deepreefmap_gui.core.fonts import MONO_FONT_FAMILY

        font = QFont(MONO_FONT_FAMILY)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        font.setPointSize(FONT_SM_PT)
        self._text.setFont(font)
        self._text.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {PREVIEW_BG}; color: {TEXT_SECONDARY}; }}"
        )
        layout.addWidget(self._text, 1)

    def append_line(self, text: str) -> None:
        # A stored log is a fixed thing to read; live lines arriving under it
        # would interleave this session's work with another run's record.
        if self._showing_file:
            return
        # Auto-scroll only when the viewport is already at the bottom so the
        # user can scroll up to read earlier output without being yanked back.
        scrollbar = self._text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self._text.appendPlainText(text)
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        self._text.clear()

    def set_current_log_path(self, path: Path | None) -> None:
        self._current_log_path = path
        self._open_log_btn.setEnabled(path is not None)

    def show_file(self, path: Path, *, title: str = "") -> bool:
        """Read a stored run.log into the panel. False if there is nothing to read.

        The last _MAX_LINES of it: a long run's log is megabytes, the panel holds
        a bounded number of lines anyway, and what went wrong is at the end.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("Could not read the run log at %s", path, exc_info=True)
            return False
        lines = text.splitlines()
        # Remembered so going back restores it: the live log has its own current
        # file while a run is going, and leaving a stored run's path behind
        # pointed "Open log file" at the wrong run entirely.
        if not self._showing_file:
            self._live_log_path = self._current_log_path
        self._showing_file = True
        self._text.setPlainText("\n".join(lines[-_MAX_LINES:]))
        self._text.verticalScrollBar().setValue(self._text.verticalScrollBar().maximum())
        self._heading.setText(f"Log of {title or path.parent.name}")
        self._live_btn.setVisible(True)
        self.set_current_log_path(path)
        return True

    def show_live(self) -> None:
        """Go back to this session's own log, and to its own log file."""
        self._showing_file = False
        self._text.clear()
        self._heading.setText("")
        self._live_btn.setVisible(False)
        self.set_current_log_path(self._live_log_path)

    def _open_current_log(self) -> None:
        if self._current_log_path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._current_log_path)))


def install_qt_log_handler(level: int = logging.INFO) -> QtLogHandler:
    """Attach a QtLogHandler to the library and app logger trees and return it."""
    handler = QtLogHandler()
    handler.setLevel(level)
    for root in (logging.getLogger("deepreefmap"), logging.getLogger("deepreefmap_gui")):
        # Replace any previously-installed Qt handler (hot reload during dev).
        for existing in list(root.handlers):
            if isinstance(existing, QtLogHandler):
                root.removeHandler(existing)
        root.addHandler(handler)
        root.setLevel(min(root.level or level, level))
    return handler


class _StreamToLogger(io.TextIOBase):
    """File-like shim that turns stream writes into log records.

    `isatty()` is False so tqdm bars created with `disable=None` stay off and
    don't spam the log with carriage-return redraws.
    """

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, s: str) -> int:
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        line, self._buffer = self._buffer, ""
        self._emit(line)

    def _emit(self, line: str) -> None:
        # Keep the final \r segment only, matching what a terminal would show
        # after in-place redraws.
        line = line.rsplit("\r", maxsplit=1)[-1]
        if line.strip():
            self._logger.log(self._level, line)

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def redirect_std_streams_to_logging() -> None:
    """Route stray stdout/stderr writes into the `deepreefmap` logger tree."""
    # The Windows GUI-subsystem binary has no console at all, so the log window and
    # run log file are the only places this output stays visible. Call after
    # logging.basicConfig, or the root StreamHandler binds to these and loops.
    sys.stdout = _StreamToLogger(logging.getLogger("deepreefmap.stdout"), logging.INFO)
    sys.stderr = _StreamToLogger(logging.getLogger("deepreefmap.stderr"), logging.WARNING)


def open_run_log_file(
    run_dir: Path, level: int = logging.INFO, *, filename: str = "run.log"
) -> logging.FileHandler:
    """Create and attach a FileHandler that captures this run's logs.

    The caller is responsible for passing the returned handler back to
    `close_run_log_file` when the run ends. `filename` names the file: a
    calibration keeps its own record beside the profile it produced.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / filename
    # Append: attempts get directories of their own now, but a legacy directory
    # revisited must not lose the log of what happened to it before.
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    logging.getLogger("deepreefmap").addHandler(fh)
    logging.getLogger("deepreefmap_gui").addHandler(fh)
    return fh


def close_run_log_file(handler: logging.FileHandler | None) -> None:
    if handler is None:
        return
    logging.getLogger("deepreefmap").removeHandler(handler)
    logging.getLogger("deepreefmap_gui").removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass
