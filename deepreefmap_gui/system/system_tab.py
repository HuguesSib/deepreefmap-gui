"""Performance panel: live RAM/VRAM/CPU/disk gauges and what past runs cost.

Reads system_probe, the same source the pre-flight check uses, so the numbers the
user sees match the ones the guard decides on.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QVBoxLayout,
)

from deepreefmap_gui.core.theme import (
    BLOCK,
    FONT_SM,
    TEXT_MUTED,
    UPDATE,
)
from deepreefmap_gui.core.widgets import MeterBar, muted_label
from deepreefmap_gui.core.window_protocol import MixinBase


def _util_color(percent: float) -> str:
    """Green/amber/orange/red banding shared by every utilisation bar."""
    if percent >= 90.0:
        return BLOCK
    if percent >= 75.0:
        return "#e07030"
    if percent >= 50.0:
        return UPDATE
    return "#4caf7d"



class SystemPanelMixin(MixinBase):
    """Builds and drives the system panel. Gauges tick only while it is on screen."""

    def _build_system_panel(self, layout: object) -> None:
        assert isinstance(layout, QVBoxLayout)
        intro = QLabel("<b>Live system usage</b>")
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self._sys_gauges: dict[str, tuple[MeterBar, QLabel]] = {}
        for row, (key, name) in enumerate(
            (("ram", "RAM"), ("swap", "Swap"), ("vram", "VRAM"), ("cpu", "CPU"), ("disk", "Disk"))
        ):
            gauge_name = muted_label(name)
            grid.addWidget(gauge_name, row, 0)
            bar = MeterBar()
            grid.addWidget(bar, row, 1)
            value = QLabel("n/a")
            value.setMinimumWidth(150)
            value.setStyleSheet(f"color: {TEXT_MUTED};")
            grid.addWidget(value, row, 2)
            self._sys_gauges[key] = (bar, value)
        layout.addLayout(grid)

        # Static machine specs the gauges do not cover.
        self._machine_specs_label = QLabel("")
        self._machine_specs_label.setTextFormat(Qt.TextFormat.RichText)
        self._machine_specs_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._machine_specs_label)

        # Recorded configurations share one comparison view beneath live usage.
        runs_divider = QFrame()
        runs_divider.setFrameShape(QFrame.Shape.HLine)
        runs_divider.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(runs_divider)
        from deepreefmap_gui.system.performance_comparison import PerformanceComparison

        self._performance_comparison = PerformanceComparison(self)
        layout.addWidget(self._performance_comparison)
        self._refresh_recorded_runs()

        layout.addStretch()

        # 1 Hz gauge tick, run only while the gauges are on screen.
        self._sys_timer = QTimer(self)
        self._sys_timer.setInterval(1000)
        self._sys_timer.timeout.connect(self._refresh_system_gauges)

    def _refresh_recorded_runs(self) -> None:
        """Refresh the selected configuration and its comparison evidence."""
        active = getattr(self, "_active_preset", None)
        settings = dict(active.settings) if active else {}
        if hasattr(self, "_grid_bins_spin"):
            settings.update(self._collect_run_settings())
        settings["mapping_backend"] = settings.pop("mapping_name", None)
        settings["segmentation_model"] = (
            "__skip__" if settings.get("skip_segmentation") else settings.pop("segmentation_name", None)
        )
        settings["mode"] = "geometry_only" if settings.get("skip_segmentation") else "semantic"
        self._performance_comparison.refresh(settings)

    def _refresh_system_gauges(self) -> None:
        from deepreefmap_gui.profiling.system_probe import format_bytes, sample_utilisation

        try:
            util = sample_utilisation()
        except Exception:
            return
        ram_text = f"{format_bytes(util.ram_used_bytes)} / {format_bytes(util.ram_total_bytes)}"
        self._set_gauge("ram", util.ram_percent, ram_text)
        self._set_gauge("cpu", util.cpu_percent, f"{util.cpu_percent:.0f}%")
        if util.swap_percent is not None:
            self._set_gauge(
                "swap",
                util.swap_percent,
                f"{format_bytes(util.swap_used_bytes)} / {format_bytes(util.swap_total_bytes)}",
            )
        else:
            self._set_gauge("swap", None, "none")
        if util.vram_percent is not None:
            self._set_gauge(
                "vram",
                util.vram_percent,
                f"{format_bytes(util.vram_used_bytes)} / {format_bytes(util.vram_total_bytes)}",
            )
        else:
            self._set_gauge("vram", None, "shared / n/a")
        self._refresh_disk_gauge()

    def _refresh_disk_gauge(self) -> None:
        from deepreefmap_gui.profiling.system_probe import format_bytes, probe_system

        try:
            # On a repaint timer, so it shows the card as soon as it is counted
            # rather than stalling the gauges until it is.
            profile = probe_system(wait_for_gpu=False)
        except Exception:
            return
        total = profile.disk_total_bytes
        used_pct = 100.0 * (total - profile.disk_free_bytes) / total if total else None
        self._set_gauge("disk", used_pct, f"{format_bytes(profile.disk_free_bytes)} free / {format_bytes(total)}")
        self._set_machine_specs(profile)

    def _set_machine_specs(self, profile: object) -> None:
        """One muted line of static hardware the gauges don't already show."""
        from deepreefmap_gui.profiling.system_probe import GPU_MPS, SystemProfile, format_bytes

        assert isinstance(profile, SystemProfile)
        gpu = profile.gpu
        if gpu.has_distinct_vram:
            gpu_text = f"{gpu.name} · {format_bytes(gpu.total_vram_bytes)}"
        elif gpu.kind == GPU_MPS:
            gpu_text = f"{gpu.name} (unified memory)"
        else:
            gpu_text = gpu.name
        cores = f"{profile.cpu_logical} logical / {profile.cpu_physical or '?'} physical cores"
        self._machine_specs_label.setText(
            f"<span style='color:{TEXT_MUTED}; font-size:{FONT_SM}'>{gpu_text}<br>"
            f"{cores} · {profile.os_name} {profile.os_release}</span>"
        )

    def _set_gauge(self, key: str, percent: float | None, text: str) -> None:
        bar, value = self._sys_gauges[key]
        if percent is None:
            bar.set_unavailable()
        else:
            bar.set_level(percent, _util_color(percent))
        value.setText(text)
