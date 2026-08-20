"""The system panel: its gauges, its recorded history, and the memory grade.

Colour assertions go through core.theme rather than hex literals, so a palette
change moves the theme test and these together instead of failing here for a
reason that has nothing to do with the panel.

Where the panel is shown, and when its 1 Hz poll runs, belongs to Setup
and is covered in tests/gui/test_machine_page.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deepreefmap_gui.core.theme import BLOCK, SUCCESS, UPDATE


def _recorded_runs_text(window) -> str:
    """Caption + every child label text + every meter bar stylesheet, concatenated."""
    from PySide6.QtWidgets import QLabel, QProgressBar

    combo = window._recorded_runs_filter_combo
    parts = [window._recorded_runs_caption.text()]
    parts += [combo.itemText(i) for i in range(combo.count())]
    parts += [w.text() for w in window._recorded_runs_container.findChildren(QLabel)]
    parts += [w.styleSheet() for w in window._recorded_runs_container.findChildren(QProgressBar)]
    return " ".join(parts)


def test_gauges_reflect_a_sampled_utilisation(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "sample_utilisation",
        lambda: probe.Utilisation(
            ram_used_bytes=8 * 1024**3, ram_total_bytes=32 * 1024**3, ram_percent=25.0,
            cpu_percent=40.0, vram_used_bytes=None, vram_total_bytes=None,
            swap_used_bytes=2 * 1024**3, swap_total_bytes=8 * 1024**3,
        ),
    )
    window._refresh_system_gauges()
    ram_bar, ram_label = window._sys_gauges["ram"]
    assert ram_bar.value() == 25
    assert "8.0 GB" in ram_label.text()
    # No distinct VRAM -> the gauge reads as shared, not a fake percentage.
    assert "shared" in window._sys_gauges["vram"][1].text()
    # Swap gauge reflects the sample (2/8 GB = 25%).
    swap_bar, swap_label = window._sys_gauges["swap"]
    assert swap_bar.value() == 25
    assert "2.0 GB" in swap_label.text()


def test_machine_specs_line_reports_gpu_and_cores(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "probe_system",
        lambda *a, **k: probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=16, cpu_physical=8,
            total_ram_bytes=64 * 1024**3, available_ram_bytes=48 * 1024**3,
            total_swap_bytes=8 * 1024**3, free_swap_bytes=8 * 1024**3,
            gpu=probe.GpuInfo(probe.GPU_CUDA, "RTX 4090", 24 * 1024**3, 20 * 1024**3),
            disk_total_bytes=1000 * 1024**3, disk_free_bytes=400 * 1024**3, disk_path="/",
        ),
    )
    window._refresh_disk_gauge()  # also populates the static specs line
    text = window._machine_specs_label.text()
    assert "RTX 4090" in text
    assert "16 logical / 8 physical" in text
    # No inferred capacity claim: we report hardware, we do not benchmark.
    assert "should handle" not in text


def _low_ram_profile(probe):
    return probe.SystemProfile(
        os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
        total_ram_bytes=32 * 1024**3, available_ram_bytes=6 * 1024**3,
        total_swap_bytes=0, free_swap_bytes=0,
        gpu=probe.GpuInfo(probe.GPU_NONE, "CPU only", None, None),
        disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
    )


def _queue_pass(window, *, seconds: float, fps: int) -> None:
    """Give the grade a pass to read: it sizes the longest one queued."""
    window._survey_rows = [SimpleNamespace(begin_s=0.0, end_s=seconds)]
    window._fps_spin.setValue(fps)


def test_a_memory_risk_shows_the_capacity_readout(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=378.0, fps=5)
    window._update_memory_profile_warning()

    assert not window._capacity_advice.isHidden()
    # A whole sentence in a narrow column, so it wraps rather than clipping.
    assert window._capacity_advice.wordWrap()
    # The pass the figures were modelled on is named in the units it was set up in.
    assert "5 FPS" in window._capacity_caption.text()
    assert "Modelled on a pass" in window._capacity_caption.text()
    # What the machine can do is stated whether or not the pass fits.
    assert "up to about" in window._capacity_detail.text()
    assert window._memory_advisory


def test_the_readout_names_a_setting_that_would_fit(window, monkeypatch) -> None:
    """A field worker needs a change to make, not only a number to read."""
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=1419.0, fps=5)
    window._update_memory_profile_warning()

    advice = window._capacity_advice.text()
    assert "FPS to" in advice or "trim" in advice


def test_choosing_a_lighter_method_regrades_the_readout(window, monkeypatch) -> None:
    """The readout offers a lighter mapping method as the fix for a pass that
    will not fit, so taking that offer has to move the number it is read from."""
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=378.0, fps=5)
    window._map_combo.setCurrentText("loger_star")
    heavy = window._capacity_rows["ram"].message.text()

    window._map_combo.setCurrentText("scsfmlearner")

    assert window._capacity_rows["ram"].message.text() != heavy


def test_swap_is_reported_as_a_cost_in_speed_not_a_warning(window, monkeypatch) -> None:
    """A machine with a swapfile large enough to finish the pass is not failing."""
    import deepreefmap_gui.profiling.system_probe as probe

    def swapped(*_a, **_k):
        base = _low_ram_profile(probe)
        return probe.SystemProfile(
            **{**base.to_dict(), "gpu": base.gpu,
               "total_swap_bytes": 32 * 1024**3, "free_swap_bytes": 32 * 1024**3}
        )

    monkeypatch.setattr(probe, "probe_system", swapped)
    _queue_pass(window, seconds=378.0, fps=5)
    window._update_memory_profile_warning()

    memory = window._capacity_rows["ram"].message.text()
    assert "runs from swap" in memory and "slower" in memory
    # No advice line, no advisory on Setup: nothing here needs the user's attention.
    assert window._capacity_advice.isHidden()
    assert window._memory_advisory == ""


def test_the_bar_carries_what_other_applications_hold(window, monkeypatch) -> None:
    """The run is drawn against the whole pool, stacked on top of the part
    something else is already in, so what is left of the track is what is
    actually spare. This machine has 32 GB with 6 GB of it free, so most of the
    track is held."""
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=378.0, fps=5)
    window._update_memory_profile_warning()

    ram = window._current_fit().verdict.resources[0]
    pool = ram.budget_bytes + ram.held_bytes
    row = window._capacity_rows["ram"]
    held = row.bar.held_percent()
    assert held == pytest.approx(100 * ram.held_bytes / pool, abs=0.5)
    assert held > 50  # 6 GB free of 32 GB: the machine is mostly spoken for
    assert "Other applications" in row.bar.toolTip()
    # Every part of the track is named under it, in the order it is painted:
    # what is already taken first, then what a run would add to it.
    legend = row.legend.text()
    assert legend.index("Other applications") < legend.index("Needed")


def test_a_machine_with_nothing_else_running_has_no_held_share(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "probe_system",
        lambda *a, **k: probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
            total_ram_bytes=64 * 1024**3, available_ram_bytes=64 * 1024**3,
            total_swap_bytes=0, free_swap_bytes=0,
            gpu=probe.GpuInfo(probe.GPU_NONE, "CPU only", None, None),
            disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
        ),
    )
    _queue_pass(window, seconds=378.0, fps=5)
    window._update_memory_profile_warning()

    row = window._capacity_rows["ram"]
    assert row.bar.held_percent() == 0.0
    assert "Other applications" not in row.bar.toolTip()
    assert "Other applications" not in row.legend.text()
    assert "Free" in row.legend.text()


def test_memory_and_the_card_are_graded_on_separate_bars(window, monkeypatch) -> None:
    """A laptop with plenty of memory and a small card: the run fits one pool
    and not the other, and each says so on its own track. Merging them hid the
    card that refused the run behind memory that comfortably took it."""
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "probe_system",
        lambda *a, **k: probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=16, cpu_physical=8,
            total_ram_bytes=32 * 1024**3, available_ram_bytes=28 * 1024**3,
            total_swap_bytes=0, free_swap_bytes=0,
            gpu=probe.GpuInfo(probe.GPU_CUDA, "RTX 3070 Laptop", 8 * 1024**3, 8 * 1024**3),
            disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
        ),
    )
    _queue_pass(window, seconds=120.0, fps=5)
    window._map_combo.setCurrentText("loger_star")
    window._update_memory_profile_warning()

    memory, graphics = window._capacity_rows["ram"], window._capacity_rows["vram"]
    assert memory.isVisibleTo(window._setup_page)
    assert graphics.isVisibleTo(window._setup_page)
    assert "Fits" in memory.message.text()
    assert SUCCESS in memory.bar.styleSheet()
    assert "loger_star" in graphics.message.text()
    assert "more than" in graphics.message.text()
    assert BLOCK in graphics.bar.styleSheet()
    assert not memory.icon.pixmap().isNull()
    assert not graphics.icon.pixmap().isNull()


def test_a_machine_without_a_card_still_shows_both_bars(window, monkeypatch) -> None:
    """The graphics row states there is no card rather than disappearing: a row
    that vanishes reads as a row that passed."""
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=120.0, fps=5)
    window._update_memory_profile_warning()

    graphics = window._capacity_rows["vram"]
    assert "No graphics card" in graphics.message.text()
    assert graphics.legend.text() == ""


def test_capacity_is_unavailable_until_a_pass_is_queued(window) -> None:
    window._survey_rows = []  # no length is knowable yet
    window._update_memory_profile_warning()

    assert window._capacity_advice.isHidden()
    assert window._memory_advisory == ""
    assert "Add a pass" in window._capacity_detail.text()


def test_the_capacity_colour_tracks_warn_against_block(window, monkeypatch) -> None:
    """Amber and red have to stay distinguishable: one says the pass is close to
    the limit, the other says it is expected to run out and stop."""
    import deepreefmap_gui.profiling.system_probe as probe

    def profile(total_gb):
        return probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
            total_ram_bytes=total_gb * 1024**3, available_ram_bytes=total_gb * 1024**3,
            total_swap_bytes=0, free_swap_bytes=0,
            gpu=probe.GpuInfo(probe.GPU_NONE, "CPU only", None, None),
            disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
        )

    _queue_pass(window, seconds=378.0, fps=5)
    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: profile(40))
    window._update_memory_profile_warning()
    assert UPDATE in window._capacity_advice.styleSheet()
    assert BLOCK not in window._capacity_advice.styleSheet()

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: profile(22))
    window._update_memory_profile_warning()
    assert BLOCK in window._capacity_advice.styleSheet()


def test_recorded_runs_summary_shows_peak_and_risk(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "summarise_recorded_runs",
        lambda *a, **k: [{
            "key": "loger_star|seg|1376x768|3fps",
            "params": {"fps": 3, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star", "segmentation_model": "coralscapes-vit-b-dpt"},
            "frames": 1134, "points": 14_000_000, "run_seconds": 430.0,
            "peak_ram_bytes": 30 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": False,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    text = _recorded_runs_text(window)
    assert "1134 frames" in text
    assert "loger_star" in text
    # The segmentation model is now shown alongside the mapping backend.
    assert "coralscapes-vit-b-dpt" in text
    # Separate meters for RAM, swap and VRAM are rendered.
    assert "RAM" in text and "Swap" in text and "VRAM" in text
    # Swap predates capture on this run -> shown as "not recorded", not a fake 0%.
    assert "not recorded" in text
    # 30/32 GB = ~94% -> the RAM meter is coloured red, no separate text label.
    assert BLOCK in text


def test_recorded_runs_summary_shows_swap_spill(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "summarise_recorded_runs",
        lambda *a, **k: [{
            "key": "loger_star|seg|1376x768|5fps",
            "params": {"fps": 5, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star"},
            "frames": 1890, "points": 30_000_000, "run_seconds": 905.0,
            "peak_ram_bytes": 31 * 1024**3, "peak_swap_bytes": 8 * 1024**3, "swap_recorded": True,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    text = _recorded_runs_text(window)
    # Committed 39 GB > 32 GB RAM: the swap meter is populated and the tag is red.
    assert "swap" in text.lower()
    assert "not recorded" not in text
    assert BLOCK in text
    # The median wall-clock and its per-frame throughput are shown.
    assert "Time" in text
    assert "s/frame" in text


def test_recorded_runs_group_shows_run_count(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [{
            "params": {"fps": 5, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star", "segmentation_model": "seg"},
            "frames": 1890, "count": 3, "run_seconds": 905, "seconds_per_frame": 905 / 1890,
            "peak_ram_bytes": 30 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": True,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    assert "3 runs" in _recorded_runs_text(window)


def test_recorded_runs_summary_empty_state(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(history, "group_recorded_runs", lambda *a, **k: [])
    window._refresh_recorded_runs()
    assert "None yet" in window._recorded_runs_caption.text()
    assert window._recorded_runs_filter_row.isHidden()


def _group(mapping, seg, fps, frames):
    return {
        "params": {"fps": fps, "processing_width": 1376, "processing_height": 768,
                   "mapping_backend": mapping, "segmentation_model": seg},
        "frames": frames, "count": 1, "run_seconds": 100, "seconds_per_frame": 0.1,
        "peak_ram_bytes": 20 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": True,
        "peak_vram_bytes": 10 * 1024**3,
        "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
        "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
    }


def _group_titles(window):
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in window._recorded_runs_container.findChildren(QLabel)]


def test_recorded_runs_filter_defaults_to_most_recent_combination(make_window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    # Patch before building: the window populates the filter from real machine
    # history during construction, and a later reload preserves that selection.
    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [
            _group("loger_star", "coralscapes-vit-b-dpt", 1, 378),
            _group("scsfmlearner", "coralscapes-vit-b-dpt", 3, 785),
        ],
    )
    window = make_window()

    # Default selection is the newest combination; only its group renders and the
    # redundant per-group model subtitle is dropped.
    assert window._recorded_runs_filter_combo.currentData() == ("loger_star", "coralscapes-vit-b-dpt")
    titles = " ".join(_group_titles(window))
    assert "378 frames" in titles
    assert "785 frames" not in titles
    assert "scsfmlearner" not in titles


def test_recorded_runs_filter_all_shows_every_group_with_subtitle(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [
            _group("loger_star", "coralscapes-vit-b-dpt", 1, 378),
            _group("scsfmlearner", "coralscapes-vit-b-dpt", 3, 785),
        ],
    )
    window._refresh_recorded_runs()
    window._recorded_runs_filter_combo.setCurrentIndex(0)  # "All combinations"

    titles = " ".join(_group_titles(window))
    assert "378 frames" in titles and "785 frames" in titles
    # Under "All" the model subtitle returns so groups stay distinguishable.
    assert "scsfmlearner" in titles
