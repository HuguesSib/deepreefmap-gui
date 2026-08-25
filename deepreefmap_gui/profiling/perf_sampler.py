"""Background RAM/VRAM sampler: measure real peak memory per pipeline stage.

RAM and swap are this process tree's own; VRAM stays device-wide, because a
graphics figure that understates is an OOM the user cannot catch, and the whole
card is what the run has to fit inside anyway.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSample:
    t: float  # time.monotonic() timestamp, comparable to the orchestrator's stage marks
    ram_bytes: int
    vram_bytes: int | None
    # This run's own pages in swap: secondary RAM once RAM fills. None, never
    # zero, where the platform will not report it, so an unmeasurable machine
    # stays out of the fleet's swap statistics rather than voting nothing into them.
    swap_bytes: int | None = None


class ResourceSampler:
    """Poll memory use on a daemon thread until stopped, mirroring the viser loop pattern."""

    def __init__(self, interval_s: float = 0.5) -> None:
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[ResourceSample] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="drm-resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        from deepreefmap_gui.profiling.system_probe import (
            sample_process_memory,
            sample_utilisation,
        )

        while not self._stop.is_set():
            try:
                util = sample_utilisation()
                # The machine-wide reading is the fallback, not the measurement:
                # it is what a platform that will not report per-process memory
                # leaves us with, and it counts the whole desktop as the run's.
                # Its swap half is null where the machine would not report swap,
                # so an unmeasurable figure stays unmeasurable down this path too.
                mine = sample_process_memory()
                ram, swap = mine or (util.ram_used_bytes, util.swap_used_bytes)
                self.samples.append(
                    ResourceSample(time.monotonic(), ram, util.vram_used_bytes, swap)
                )
            except Exception:
                pass
            # Interruptible sleep: stop() returns promptly instead of waiting a full interval.
            self._stop.wait(self._interval)


def peaks_from_marks(
    samples: list[ResourceSample],
    spans: tuple[tuple[str, str, str], ...],
    marks: dict[str, float],
) -> dict[str, dict[str, int | None]]:
    """Peak RAM/VRAM/swap within each stage span, keyed like `_durations_from_marks`.

    Swap is captured alongside RAM because a run that fills physical RAM pins it near
    100% and shows its real demand as swap, so a stage's true peak is RAM plus swap.
    Both are the run's own (see ResourceSampler), which is what makes them
    comparable to an estimate and to each other across backends.

    Swap and VRAM are null where nothing observed them, and a reader counts an
    observation rather than a key: a machine with no discrete card and a machine
    that will not report per-process swap both have to stay out of the averages
    instead of voting a zero into them.
    """
    peaks: dict[str, dict[str, int | None]] = {}
    for begin, end, stage in spans:
        if begin not in marks or end not in marks or marks[end] < marks[begin]:
            continue
        t0, t1 = marks[begin], marks[end]
        window = [s for s in samples if t0 <= s.t <= t1]
        if not window:
            continue
        vrams = [s.vram_bytes for s in window if s.vram_bytes is not None]
        swaps = [s.swap_bytes for s in window if s.swap_bytes is not None]
        peaks[stage] = {
            "ram_bytes": max(s.ram_bytes for s in window),
            "vram_bytes": max(vrams) if vrams else None,
            "swap_bytes": max(swaps) if swaps else None,
        }
    return peaks
