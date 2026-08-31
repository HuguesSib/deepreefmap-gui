"""Reading COLMAP's own console output while it runs.

COLMAP is C++ behind pycolmap, and it reports through glog to file descriptor
2. Python's `sys.stderr` is not in that path, so the app sees nothing unless the
descriptor itself is taken over. `capture_stderr` does that for the duration of a
call; `parse_line` turns the lines into the counts the progress bar needs.

The parsing is deliberately narrow: the lines below are the ones that say how far
along a stage is, or that a reconstruction has just been thrown away. Everything
else travels to the log unread.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# `I20260831 16:11:11.366571 140560648103616 incremental_pipeline.cc:563] => text`
_GLOG = re.compile(r"^([IWEF])\d{8} [\d:.]+ \d+ [\w.]+:\d+\]\s*(.*)$")
_EXTRACTED = re.compile(r"Processed file \[(\d+)/(\d+)\]")
_MATCHED = re.compile(r"Processing image \[(\d+)/(\d+)\]")
_REGISTERING = re.compile(r"Registering image #\d+ \(num_reg_frames=(\d+)\)")
_DISCARDED = re.compile(r"Discarding reconstruction due to (.+?)\.?$")

EXTRACTED = "extracted"
MATCHED = "matched"
REGISTERED = "registered"
DISCARDED = "discarded"
KEPT = "kept"
WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ColmapEvent:
    """Something worth showing, read off one line of COLMAP's output."""

    kind: str
    done: int = 0
    total: int = 0
    text: str = ""


def parse_line(line: str) -> ColmapEvent | None:
    """The event one line carries, or None for the rest of the chatter."""
    match = _GLOG.match(line.strip())
    if match is None:
        return None
    severity, message = match.group(1), match.group(2)

    found = _EXTRACTED.search(message)
    if found:
        return ColmapEvent(EXTRACTED, int(found.group(1)), int(found.group(2)))
    found = _MATCHED.search(message)
    if found:
        return ColmapEvent(MATCHED, int(found.group(1)), int(found.group(2)))
    found = _REGISTERING.search(message)
    if found:
        return ColmapEvent(REGISTERED, int(found.group(1)))
    found = _DISCARDED.search(message)
    if found:
        return ColmapEvent(DISCARDED, text=found.group(1))
    if "Keeping successful reconstruction" in message:
        return ColmapEvent(KEPT)
    if severity in "WEF":
        return ColmapEvent(WARNING, text=message)
    return None


@contextmanager
def capture_stderr(on_line: Callable[[str], None]) -> Iterator[None]:
    """Send everything written to file descriptor 2 to `on_line` for the duration.

    The descriptor is process-wide, so this is held only around a COLMAP call and
    the real stderr is restored whatever happens. A reader thread drains the pipe:
    COLMAP writes megabytes, and a pipe nobody reads fills up and blocks it.
    """
    saved = os.dup(2)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def drain() -> None:
        with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as pipe:
            for line in pipe:
                text = line.rstrip("\n")
                if text:
                    on_line(text)

    reader = threading.Thread(target=drain, name="colmap-output", daemon=True)
    reader.start()
    try:
        yield
    finally:
        # Restoring first closes the write end, which ends the reader's loop.
        os.dup2(saved, 2)
        os.close(saved)
        reader.join(timeout=5.0)
