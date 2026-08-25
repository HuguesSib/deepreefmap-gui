"""Per-stage peak memory folding and the background sampler lifecycle."""

from __future__ import annotations

from deepreefmap_gui.profiling.instrumentation import STAGE_SPANS
from deepreefmap_gui.profiling.perf_sampler import ResourceSample, ResourceSampler, peaks_from_marks

# The real spans, not a copy: a hand-written stand-in kept passing when the shape
# of STAGE_SPANS changed under peaks_from_marks' actual callers. Trimmed to the
# first three because the marks below only span that far.
_SPANS = STAGE_SPANS[:3]


def test_peaks_are_the_max_within_each_stage_window() -> None:
    marks = {"start": 0.0, "preprocess": 10.0, "mapping": 20.0, "cloud": 30.0}
    samples = [
        ResourceSample(1.0, 5, 1, 0),      # startup
        ResourceSample(9.0, 7, 2, 0),      # startup (peak)
        ResourceSample(15.0, 40, 9, 1),    # preprocess (peak)
        ResourceSample(25.0, 34, 8, 6),    # mapping (swap peak, thrashing)
        ResourceSample(29.0, 30, 7, 4),    # mapping
    ]
    peaks = peaks_from_marks(samples, _SPANS, marks)
    assert peaks["startup"] == {"ram_bytes": 7, "vram_bytes": 2, "swap_bytes": 0}
    assert peaks["preprocess"] == {"ram_bytes": 40, "vram_bytes": 9, "swap_bytes": 1}
    assert peaks["mapping"] == {"ram_bytes": 34, "vram_bytes": 8, "swap_bytes": 6}


def test_the_sampler_measures_this_run_not_the_whole_desktop(monkeypatch) -> None:
    """Scenario: a busy desktop holding 20 units of RAM and 9 in swap, around a
    run of its own that holds 6 and 1.

    Expected behaviour: the sample is the run's. A machine-wide reading is stored
    as the run's peak and read back by the memory grade, where the desktop's share
    made a light mapping backend record more than a heavy one.
    """
    import threading

    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "sample_utilisation",
        lambda: probe.Utilisation(20, 100, 20.0, 1.0, vram_used_bytes=3, vram_total_bytes=10,
                                  swap_used_bytes=9, swap_total_bytes=100),
    )
    monkeypatch.setattr(probe, "sample_process_memory", lambda: (6, 1))
    sampler = ResourceSampler(interval_s=0.01)
    sampler.start()
    deadline = threading.Event()
    deadline.wait(0.05)
    sampler.stop()

    assert sampler.samples
    assert all(s.ram_bytes == 6 and s.swap_bytes == 1 for s in sampler.samples)
    # VRAM stays device-wide: understating what a card holds is an OOM kill.
    assert all(s.vram_bytes == 3 for s in sampler.samples)


def test_the_machine_reading_is_the_fallback_when_the_process_will_not_report(
    monkeypatch,
) -> None:
    import threading

    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "sample_utilisation",
        lambda: probe.Utilisation(20, 100, 20.0, 1.0, vram_used_bytes=3, vram_total_bytes=10,
                                  swap_used_bytes=9, swap_total_bytes=100),
    )
    monkeypatch.setattr(probe, "sample_process_memory", lambda: None)
    sampler = ResourceSampler(interval_s=0.01)
    sampler.start()
    threading.Event().wait(0.05)
    sampler.stop()

    assert sampler.samples
    assert all(s.ram_bytes == 20 and s.swap_bytes == 9 for s in sampler.samples)


def test_the_fallback_carries_an_unmeasurable_swap_through_as_null(monkeypatch) -> None:
    """Scenario: neither the process nor the machine will report swap.

    Expected behaviour: null all the way to the sample. The machine-wide
    reading is the fallback for RAM, and taking a zero swap from it would put
    the very observation the per-process null exists to keep out back in.
    """
    import threading

    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "sample_utilisation",
        lambda: probe.Utilisation(20, 100, 20.0, 1.0, vram_used_bytes=None,
                                  vram_total_bytes=None, swap_used_bytes=None),
    )
    monkeypatch.setattr(probe, "sample_process_memory", lambda: None)
    sampler = ResourceSampler(interval_s=0.01)
    sampler.start()
    threading.Event().wait(0.05)
    sampler.stop()

    assert sampler.samples
    assert all(s.ram_bytes == 20 and s.swap_bytes is None for s in sampler.samples)


def test_stage_without_samples_or_marks_is_omitted() -> None:
    # Mapping has no marks, and preprocess has no sample landing inside it.
    marks = {"start": 0.0, "preprocess": 10.0}
    samples = [ResourceSample(1.0, 5, None)]
    peaks = peaks_from_marks(samples, _SPANS, marks)
    assert "startup" in peaks
    assert "preprocess" not in peaks and "mapping" not in peaks


def test_vram_none_when_no_sample_reported_it() -> None:
    marks = {"start": 0.0, "preprocess": 10.0}
    samples = [ResourceSample(1.0, 5, None, 0), ResourceSample(2.0, 8, None, 0)]
    peaks = peaks_from_marks(samples, _SPANS, marks)
    assert peaks["startup"] == {"ram_bytes": 8, "vram_bytes": None, "swap_bytes": 0}


def test_swap_none_when_the_platform_would_not_report_it() -> None:
    """Scenario: a Windows or macOS run, where per-process swap is unreadable.

    Expected behaviour: null, exactly as VRAM does on a machine with no card. A
    zero is an observation, and one from every run on those platforms drags the
    fleet's mean swap towards nothing and invents variance around it.
    """
    marks = {"start": 0.0, "preprocess": 10.0}
    samples = [ResourceSample(1.0, 5, None, None), ResourceSample(2.0, 8, None, None)]
    peaks = peaks_from_marks(samples, _SPANS, marks)
    assert peaks["startup"] == {"ram_bytes": 8, "vram_bytes": None, "swap_bytes": None}


def test_a_measured_zero_swap_stays_a_zero() -> None:
    """A run that swapped nothing is a real observation, not a missing one."""
    marks = {"start": 0.0, "preprocess": 10.0}
    samples = [ResourceSample(1.0, 5, None, 0), ResourceSample(2.0, 8, None, None)]
    peaks = peaks_from_marks(samples, _SPANS, marks)
    assert peaks["startup"]["swap_bytes"] == 0


def test_sampler_collects_samples_then_stops(monkeypatch) -> None:
    import threading
    import time

    import deepreefmap_gui.profiling.system_probe as probe

    calls = {"n": 0}

    def fake_util():
        calls["n"] += 1
        return probe.Utilisation(calls["n"], 100, 1.0, 1.0, vram_used_bytes=calls["n"], vram_total_bytes=100)

    monkeypatch.setattr(probe, "sample_utilisation", fake_util)
    sampler = ResourceSampler(interval_s=0.01)
    sampler.start()
    before = threading.active_count()
    # Second start is a no-op (already running), so no second thread appears.
    sampler.start()
    assert threading.active_count() == before

    # Poll for the samples rather than sleeping a fixed span: a loaded runner can
    # miss a 0.01s tick, and a fixed sleep turns that into a flake.
    deadline = time.monotonic() + 5.0
    while len(sampler.samples) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    sampler.stop()

    assert len(sampler.samples) >= 2
    assert sampler._thread is None
    # Samples are monotonically timestamped and carry the mocked figures.
    ts = [s.t for s in sampler.samples]
    assert ts == sorted(ts)
    assert all(s.ram_bytes >= 1 for s in sampler.samples)
