"""How long a clip is, and at what rate, by decoding it.

`survey/video_probe.py` answers the same question from the container's own index
in under a millisecond, and is what everything should ask first. It reads MP4
atoms and nothing else, so a clip written by something other than a GoPro, or one
whose index is where it did not expect, comes back with no duration at all. This
is the fallback: open the file with the decoder and ask.

It costs an open and two property reads, which on a large clip off an SD card is
long enough to be worth keeping off the GUI thread.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def decoded_length(path: Path | str) -> tuple[float, float] | None:
    """``(duration_s, fps)``, or None when the file cannot be decoded."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return None
    try:
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
    finally:
        capture.release()
    if not fps or fps <= 0 or not frames or frames <= 0:
        return None
    return float(frames) / float(fps), float(fps)
