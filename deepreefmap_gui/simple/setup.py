"""The readiness check, shown before planning on a machine that is not ready.

Four rows say whether this machine can process a dive: the graphics card,
memory, the models it needs, and disk space. Each failing row carries the one
action that fixes it. When they pass, the check stops leading on launch.

Memory is advisory: it fails against the session currently queued rather than
against the machine, and processing still starts.

It is the first view of the Setup destination, which is why the page is
a bare card rather than a titled one: simple/machine.py owns the heading and the
switch that reaches the other views.

The verdict functions are pure and Qt-free so the pass/fail logic is tested
without a window, the same split progress.py uses for the step badges.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_SM,
    browse_icon,
    cog_icon,
    download_icon,
    drive_icon,
    folder_icon,
    link_icon,
    refresh_icon,
)
from deepreefmap_gui.core.theme import (
    ERROR,
    GUTTER,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
)
from deepreefmap_gui.core.widgets import NoticeStrip, section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.models.cache import GPU_ONLY_BACKENDS
from deepreefmap_gui.packaging.shortcuts import (
    ShortcutResult,
    ShortcutState,
    ShortcutStatus,
    install_shortcut,
    remove_shortcut,
    shortcut_status,
)
from deepreefmap_gui.profiling.system_probe import GPU_NONE, format_bytes, probe_system
from deepreefmap_gui.survey.health import SurveyDbHealth

logger = logging.getLogger(__name__)

# Fallback per-pass size, used only until this machine has processed a run of its
# own. Nothing measured it: a pass leaves frame caches, a point cloud and a
# manifest behind, and the total depends on clip length, fps and resolution. It
# is deliberately generous, because warning early beats filling the disk halfway
# through a batch nobody is watching. measure_bytes_per_footage_minute replaces
# it with a figure from real output as soon as there is one.
ROUGH_PASS_BYTES = 3 * 1024**3

# Sizing a run means walking every cached frame in it, and this runs whenever the
# setup page repaints, so only the most recent runs are measured.
_SIZED_RUNS = 5

# A status row's status glyph, and the gap between it and the text, so anything
# that has to line up with that text can be indented by the sum.
_ROW_ICON_WIDTH = 18
_ROW_GAP = 10
_ROW_TEXT_INDENT = _ROW_ICON_WIDTH + _ROW_GAP

# Where the footage estimate stops being a figure and becomes a reassurance.
# Well beyond any single field season, so the only readings it suppresses are
# the ones a near-zero measured rate produced.
_FOOTAGE_CEILING_HOURS = 500


def measure_bytes_per_footage_minute(out_root: Path, limit: int = _SIZED_RUNS) -> float | None:
    """Output bytes per minute of footage, measured from recent runs.

    None until this machine has processed something, which is the honest answer
    before then: per-pass size varies with clip length, fps and resolution, so
    there is nothing to extrapolate from. Manifests carry the frame count and the
    rate they were sampled at, which is the footage duration the run consumed.
    """
    from deepreefmap_gui.survey.catalogue import dir_size_bytes

    try:
        manifests = sorted(out_root.glob("*/run_manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    rates: list[float] = []
    for manifest_path in manifests[:limit]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frames = int(manifest.get("frames_processed") or 0)
            fps = float(manifest.get("fps") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if frames <= 0 or fps <= 0:
            continue
        size = dir_size_bytes(manifest_path.parent)
        if size <= 0:
            continue
        rates.append(size / (frames / fps / 60))
    return statistics.median(rates) if rates else None


@dataclass(frozen=True)
class SetupCheck:
    """One setup row: whether it passes, its heading, and a plain-language line.

    An advisory row can fail without the machine being unready: processing still
    runs, it may just not finish.
    """

    key: str
    ok: bool
    title: str
    detail: str
    advisory: bool = False
    # What the row's action offers right now, for a row whose action changes with
    # its state. Empty keeps the label the button was built with.
    action_label: str = ""
    # Whether there is anything to press. A row's actions stay reachable once it
    # passes -- settings, past surveys and the model store are all things a diver
    # goes to look at on a machine with nothing wrong with it, and hiding them
    # left a fully set-up laptop with no way in. False is for the rows that
    # genuinely offer nothing: a menu entry this build cannot write, or one an
    # installer owns and would break if we touched it.
    actionable: bool = True


def graphics_check(*, gpu_name: str | None, requires_gpu: bool, pending: bool = False) -> SetupCheck:
    """Graphics card row. Passes unless the chosen method needs a card and none exists."""
    if pending:
        # Counting the cards costs seconds on a cold driver, so the window opens
        # before the answer exists. Advisory, so an unfinished probe cannot be
        # what holds a session back; the row repaints when it lands.
        return SetupCheck("graphics", True, "Graphics card", "Looking for a graphics card…", advisory=True)
    if gpu_name is not None:
        return SetupCheck("graphics", True, "Graphics card", gpu_name)
    if requires_gpu:
        return SetupCheck(
            "graphics",
            False,
            "Graphics card",
            "None detected, and the selected method requires one. Choose the standard method in settings.",
        )
    return SetupCheck(
        "graphics",
        True,
        "Graphics card",
        "None detected. Processing runs on the CPU.",
    )


def models_check(missing_models: list[str]) -> SetupCheck:
    """Models row. Passes when nothing the current settings need is absent."""
    if not missing_models:
        return SetupCheck("models", True, "Models", "All required models are installed.")
    names = ", ".join(missing_models)
    count = len(missing_models)
    noun = "model" if count == 1 else "models"
    return SetupCheck(
        "models",
        False,
        "Models",
        f"{count} required {noun} not installed ({names}). Import from a USB drive, or download.",
    )


def _coarse_footage(minutes: float) -> str:
    """Round a capacity to the precision it actually carries.

    Extrapolated from a handful of runs, so a figure like "157h 51m" would claim
    an accuracy the estimate does not have. Past a few days of footage it stops
    quoting a number at all: the extrapolation is from a rate measured over
    minutes, and "612490 hours" is not a capacity, it is a division.
    """
    if minutes < 90:
        return f"about {max(5, round(minutes / 5) * 5)} minutes"
    hours = minutes / 60
    if hours < 10:
        return f"about {hours:.0f} hours"
    if hours > _FOOTAGE_CEILING_HOURS:
        return f"well over {_FOOTAGE_CEILING_HOURS} hours"
    return f"about {round(hours / 10) * 10} hours"


def space_check(free_bytes: int, min_free_bytes: int, bytes_per_footage_minute: float | None = None) -> SetupCheck:
    """Disk space row, sized in footage where this machine has runs to measure."""
    free = format_bytes(free_bytes)
    if free_bytes < min_free_bytes:
        return SetupCheck(
            "space",
            False,
            "Disk space",
            f"{free} free, below the {format_bytes(min_free_bytes)} required. Delete old surveys to make room.",
        )
    if bytes_per_footage_minute:
        capacity = _coarse_footage(free_bytes / bytes_per_footage_minute)
        return SetupCheck(
            "space",
            True,
            "Disk space",
            f"{free} free, {capacity} of footage at recent run sizes.",
        )
    # Deliberately no capacity figure: nothing has been processed here to size
    # against, and ROUGH_PASS_BYTES is a fallback for blocking a doomed batch,
    # not a number to quote at someone.
    return SetupCheck("space", True, "Disk space", f"{free} free. Capacity is estimated once a run is recorded.")


def memory_check(
    *,
    total_ram_bytes: int,
    vram_bytes: int | None,
    swap_bytes: int = 0,
    advisory: str = "",
) -> SetupCheck:
    """Memory row. Advisory: a session graded short of memory still runs.

    Swap is reported alongside the RAM because the grade counts it: a machine
    told it has 27.5 GB, then warned about a 56 GB run it can actually finish,
    has been shown two figures that do not add up.
    """
    if advisory:
        return SetupCheck("memory", False, "Memory", advisory, advisory=True)
    parts = [f"{format_bytes(total_ram_bytes)} of memory"]
    if swap_bytes:
        parts.append(f"{format_bytes(swap_bytes)} of swap")
    if vram_bytes:
        parts.append(f"{format_bytes(vram_bytes)} on the graphics card")
    if len(parts) == 1:
        return SetupCheck("memory", True, "Memory", f"{parts[0]}.")
    return SetupCheck("memory", True, "Memory", f"{', '.join(parts[:-1])}, and {parts[-1]}.")


def survey_check(health: SurveyDbHealth) -> SetupCheck:
    """Survey database row. Blocking: a run that cannot be recorded loses its
    provenance, and nothing in Transects, Process or Browse works without it."""
    if health.openable:
        # "Repair" is what the action does when the row has failed; on a healthy
        # database the same button re-runs the check, and says so.
        return SetupCheck(
            "survey",
            True,
            "Survey database",
            f"Reading and writing {health.path}.",
            action_label="Check again",
        )
    return SetupCheck(
        "survey",
        False,
        "Survey database",
        health.detail or f"{health.path} could not be opened.",
        action_label="Repair…",
    )


def shortcut_check(status: ShortcutStatus) -> SetupCheck:
    """Applications-menu row. Advisory: a missing shortcut stops nothing.

    A shortcut somebody else put there -- an installer, a disk image -- is
    reported without an action. Offering to remove it would delete the entry its
    uninstaller expects to find.
    """
    if status.state is ShortcutState.UNSUPPORTED:
        return SetupCheck("shortcut", True, "Applications menu", status.detail, advisory=True, actionable=False)
    if not status.owned and status.state in (ShortcutState.CURRENT, ShortcutState.UNKNOWN):
        return SetupCheck(
            "shortcut",
            True,
            "Applications menu",
            "Installed, and managed by the installer.",
            actionable=False,
        )
    if status.state is ShortcutState.ABSENT:
        return SetupCheck(
            "shortcut",
            False,
            "Applications menu",
            "Not in the applications menu.",
            advisory=True,
            action_label="Add",
        )
    if status.state is ShortcutState.STALE:
        return SetupCheck(
            "shortcut",
            False,
            "Applications menu",
            "The menu entry points at a moved copy of DeepReefMap.",
            advisory=True,
            action_label="Update",
        )
    return SetupCheck(
        "shortcut",
        True,
        "Applications menu",
        f"Opens from the applications menu ({status.location}).",
        action_label="Remove",
    )


def evaluate_setup(
    *,
    gpu_name: str | None,
    requires_gpu: bool,
    gpu_pending: bool = False,
    missing_models: list[str],
    free_bytes: int,
    min_free_bytes: int,
    total_ram_bytes: int = 0,
    vram_bytes: int | None = None,
    swap_bytes: int = 0,
    memory_advisory: str = "",
    bytes_per_footage_minute: float | None = None,
    survey_health: SurveyDbHealth | None = None,
    shortcut_status: ShortcutStatus | None = None,
) -> list[SetupCheck]:
    """The setup rows, in the order they are shown."""
    checks = [
        graphics_check(gpu_name=gpu_name, requires_gpu=requires_gpu, pending=gpu_pending),
        memory_check(
            total_ram_bytes=total_ram_bytes,
            vram_bytes=vram_bytes,
            swap_bytes=swap_bytes,
            advisory=memory_advisory,
        ),
        models_check(missing_models),
        space_check(free_bytes, min_free_bytes, bytes_per_footage_minute),
    ]
    if survey_health is not None:
        checks.append(survey_check(survey_health))
    if shortcut_status is not None:
        checks.append(shortcut_check(shortcut_status))
    return checks


def setup_ready(checks: list[SetupCheck]) -> bool:
    """Advisory rows are excluded: they report a risk, not a missing requirement."""
    return all(check.ok for check in checks if not check.advisory)


@dataclass(frozen=True)
class BatchDiskEstimate:
    """Rough disk a session needs, next to what is free, for the pre-flight summary."""

    pass_count: int
    need_bytes: int
    free_bytes: int

    @property
    def fits(self) -> bool:
        return self.free_bytes >= self.need_bytes


def estimate_batch_disk(pass_count: int, free_bytes: int, per_pass_bytes: int = ROUGH_PASS_BYTES) -> BatchDiskEstimate:
    return BatchDiskEstimate(pass_count, pass_count * per_pass_bytes, free_bytes)


_ROW_TITLES = {
    "graphics": "Graphics card",
    "memory": "Memory",
    "models": "Models",
    "space": "Disk space",
    "survey": "Survey database",
    "shortcut": "Applications menu",
}


def _set_cta(button: QPushButton, on: bool) -> None:
    """Turn a button's filled-CTA styling on or off, repolishing so it takes."""
    button.setProperty("cta", "true" if on else None)
    button.style().unpolish(button)
    button.style().polish(button)


# Wide enough for the glyph and the button's own padding, narrow enough that six
# of them down the page read as one column of controls rather than six buttons.
_COMPACT_ACTION_WIDTH = 34
_UNBOUNDED = 16777215  # QWIDGETSIZE_MAX


def _row_action(label: str, icon, *, cta_when_unmet: bool = False) -> QPushButton:
    """One readiness-row action, carrying both the shapes it is shown in.

    Spelled out while the row needs fixing, because a diver has to be able to
    read what to do. A glyph once the row passes, because the action is still
    worth reaching -- settings, past surveys, the model store -- but a page of
    ticks with six sentence-long buttons beside them reads as six problems.
    """
    button = QPushButton(label)
    button.setIcon(icon)
    button.setIconSize(QSize(ICON_SM, ICON_SM))
    button.setProperty("full_label", label)
    if cta_when_unmet:
        button.setProperty("cta_when_unmet", True)
    return button


def _paint_row_action(button: QPushButton, *, label: str, compact: bool) -> None:
    """Switch one action between its spelled-out and glyph-only shapes."""
    button.setText("" if compact else label)
    # The label survives as the tooltip for the mouse and as the accessible name
    # for everything else: a glyph on its own is a guess, and a tooltip is not a
    # name a keyboard or a screen reader can reach.
    button.setToolTip(label if compact else "")
    button.setAccessibleName(label)
    if compact:
        button.setFixedWidth(_COMPACT_ACTION_WIDTH)
    else:
        button.setMinimumWidth(0)
        button.setMaximumWidth(_UNBOUNDED)


_TICK = f'<span style="color:{SUCCESS}; font-weight:bold;">&#10003;</span>'
_CROSS = f'<span style="color:{ERROR}; font-weight:bold;">&#10007;</span>'
# An advisory failure: the machine is not missing anything, the session queued
# against it is large.
_ALERT = f'<span style="color:{WARNING}; font-weight:bold;">!</span>'


class SimpleSetupMixin(MixinBase):
    """DeepReefMapWindow methods for the first-run laptop setup step."""

    def _build_readiness_view(self) -> QWidget:
        """Readiness: four status rows, the actions that fix them, and where
        results are saved.

        The models row carries the two provisioning actions, so a diver never
        has to leave the page to get the models a survey needs.
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GUTTER)
        # The width the page is capped and centred at is the destination's, so
        # the card sits under the switch that opened it rather than starting a
        # measure of its own.

        # A newer release, said on the view that opens first. Above the card
        # rather than in it: the checks are about this computer, and this is
        # about the software on it.
        self._setup_update_strip = NoticeStrip()
        self._setup_update_strip.action_clicked.connect(lambda: self._set_machine_view("updates"))
        outer.addWidget(self._setup_update_strip)

        # Untitled: the page it sits on is already headed Setup, and the
        # segmented control above says which of its views this is.
        card, card_layout = section_card()
        intro = QLabel("What processing a dive needs, and whether it is here.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        card_layout.addWidget(intro)

        self._setup_check_rows: dict[str, tuple[QLabel, QLabel, list[QWidget]]] = {}

        graphics_settings = _row_action("Open settings", cog_icon())
        graphics_settings.clicked.connect(self._on_edit_run_settings)
        card_layout.addWidget(self._build_setup_row("graphics", [graphics_settings]))

        memory_settings = _row_action("Open settings", cog_icon())
        memory_settings.clicked.connect(self._on_edit_run_settings)
        card_layout.addWidget(self._build_setup_row("memory", [memory_settings]))

        self._setup_usb_btn = _row_action("Import from USB drive…", folder_icon())
        self._setup_usb_btn.clicked.connect(self._on_setup_import_pack)
        self._setup_download_btn = _row_action(
            "Download models (requires internet)", download_icon(ICON_SM), cta_when_unmet=True
        )
        self._setup_download_btn.clicked.connect(self._on_setup_download_models)
        card_layout.addWidget(self._build_setup_row("models", [self._setup_usb_btn, self._setup_download_btn]))

        self._setup_drives_btn = _row_action("Drives", drive_icon())
        self._setup_drives_btn.clicked.connect(self._on_setup_show_drives)
        space_browse = _row_action("Open past surveys", browse_icon())
        space_browse.clicked.connect(lambda: self._go_to_section("browse"))
        card_layout.addWidget(self._build_setup_row("space", [self._setup_drives_btn, space_browse]))

        self._setup_survey_btn = _row_action("Repair…", refresh_icon(ICON_SM, TEXT_MUTED))
        self._setup_survey_btn.clicked.connect(self._on_setup_check_survey)
        card_layout.addWidget(self._build_setup_row("survey", [self._setup_survey_btn]))

        # Its label changes with what the entry currently is, so the row carries
        # the verb rather than the button carrying a fixed one.
        self._setup_shortcut_btn = _row_action("Add", link_icon(ICON_SM, TEXT_MUTED))
        self._setup_shortcut_btn.clicked.connect(self._on_toggle_shortcut)
        card_layout.addWidget(self._build_setup_row("shortcut", [self._setup_shortcut_btn]))

        # The folder every run lands in, directly under the row that measures
        # the disk it sits on. It decides which disk that is, and it is the
        # first thing a diver setting up a field laptop has to point somewhere
        # with room on it, so it belongs on the page that leads on launch rather
        # than a view along.
        card_layout.addWidget(self._build_out_root_block())

        # Inside the card and at its foot, so the summary and the way out sit
        # with the checks they describe. Loose underneath, with the page's empty
        # space below them, they read as a second unrelated thing.
        card_layout.addStretch(1)
        footer = QHBoxLayout()
        self._setup_summary = QLabel("")
        self._setup_summary.setWordWrap(True)
        self._setup_summary.setStyleSheet(f"color: {TEXT_MUTED};")
        footer.addWidget(self._setup_summary, 1)
        self._setup_continue_btn = QPushButton("Go to Transects →")
        self._setup_continue_btn.setProperty("cta", "true")
        self._setup_continue_btn.clicked.connect(self._on_setup_continue)
        footer.addWidget(self._setup_continue_btn)
        card_layout.addLayout(footer)

        outer.addWidget(card)
        outer.addStretch(1)

        self._refresh_readiness_view()
        return page

    def _build_out_root_block(self) -> QWidget:
        """The slot the output root controls are lent to, and a line saying what
        lands there.

        Indented to the depth of the row text above it so it reads as a
        continuation of disk space rather than a fifth check with a missing tick.
        """
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(_ROW_TEXT_INDENT, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_XS)
        self._machine_out_root_host = QWidget()
        host_layout = QVBoxLayout(self._machine_out_root_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._machine_out_root_host)
        caption = QLabel("Runs and the survey database are written under this folder.")
        caption.setWordWrap(True)
        caption.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(caption)
        return block

    def _build_setup_row(self, key: str, actions: list[QWidget]) -> QWidget:
        """One status row: a tick or cross, a heading and a line, then its actions."""
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, SPACE_XS, 0, SPACE_XS)
        outer.setSpacing(_ROW_GAP)

        icon = QLabel(_TICK)
        icon.setFixedWidth(_ROW_ICON_WIDTH)
        outer.addWidget(icon)

        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel("")
        title.setStyleSheet("font-weight: 600;")
        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {TEXT_MUTED};")
        detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(title)
        text.addWidget(detail)
        outer.addLayout(text, 1)

        # Titles are static; the check functions own the same headings so the
        # painter and the verdict never disagree.
        for action in actions:
            outer.addWidget(action)

        self._setup_check_rows[key] = (icon, detail, actions)
        # Seed the static title once from a passing check of the same key.
        title.setText(_ROW_TITLES[key])
        return row

    def _current_setup_checks(self) -> list[SetupCheck]:
        """Probe the machine and the current settings into the three verdicts."""
        from deepreefmap_gui.models.cache import _MIN_FREE_BYTES

        out_root = Path(self._out_root_input.text()).expanduser()
        profile = probe_system(out_root, wait_for_gpu=False)
        gpu_resolved = profile.gpu.resolved
        gpu_name = profile.gpu.name if gpu_resolved and profile.gpu.kind != GPU_NONE else None
        # The form, not the preset. _gpu_only_mapper and _required_model_names
        # both read these widgets, and the batch runs from them via
        # _collect_run_settings, so reading the preset here left the readiness
        # rows judging a different configuration from the one the Run gate
        # blocks on. They agree today; nothing was keeping them that way.
        mapping = self._map_combo.currentText()
        return evaluate_setup(
            gpu_name=gpu_name,
            requires_gpu=mapping in GPU_ONLY_BACKENDS,
            gpu_pending=not gpu_resolved,
            missing_models=self._survey_missing_models(),
            free_bytes=profile.disk_free_bytes,
            min_free_bytes=_MIN_FREE_BYTES,
            total_ram_bytes=profile.total_ram_bytes,
            vram_bytes=profile.gpu.total_vram_bytes if profile.gpu.has_distinct_vram else None,
            swap_bytes=profile.total_swap_bytes,
            memory_advisory=getattr(self, "_memory_advisory", ""),
            bytes_per_footage_minute=self._footage_size_rate(out_root),
            survey_health=self._survey_db_health(),
            shortcut_status=self._current_shortcut_status(),
        )

    def _current_shortcut_status(self) -> ShortcutStatus:
        """Re-read every repaint, so an entry removed outside the app is noticed.

        Reading is filesystem work on Linux and macOS. On Windows it shells out
        to PowerShell, so the answer is cached until something acts on it --
        repainting the page must not cost a subprocess each time.
        """
        cached = getattr(self, "_shortcut_status_cache", None)
        if cached is None:
            cached = shortcut_status()
            self._shortcut_status_cache = cached
        return cached

    def _on_toggle_shortcut(self) -> None:
        """Add, re-point or remove the applications-menu entry.

        On a worker thread: writing a Start Menu shortcut goes through
        PowerShell, whose cold start behind real-time antivirus scanning is
        routinely several seconds, and a window frozen that long reads as a
        crash.
        """
        status = self._current_shortcut_status()
        adding = status.state in (ShortcutState.ABSENT, ShortcutState.STALE)
        self._setup_shortcut_btn.setEnabled(False)
        self._setup_shortcut_btn.setText("Working…")

        def work() -> None:
            result = install_shortcut() if adding else remove_shortcut()
            self._sig_shortcut_done.emit(result)

        threading.Thread(target=work, daemon=True).start()

    def _on_shortcut_done(self, result: ShortcutResult) -> None:
        self._shortcut_status_cache = result.status
        self._setup_shortcut_btn.setEnabled(True)
        self._refresh_readiness_view()
        self._status_label.setText(result.message)
        if not result.ok:
            # Reported, not swallowed: a shortcut that silently fails to appear
            # is indistinguishable from one the desktop has not noticed yet.
            QMessageBox.warning(self, "Applications menu", result.message)

    def _footage_size_rate(self, out_root: Path) -> float | None:
        """Measured output bytes per footage minute, or None until the walk lands.

        Measured on a worker thread, once per output root: it walks the frame
        caches of several runs, which is a third of a second of disk on this
        machine, and the page asking for it repaints on every cart change and
        while the window is still being built. The row reads "not measured yet"
        in the meantime, which is also what it says on a machine that has never
        processed anything. _on_survey_done clears the cache, so a finished batch
        is measured in without re-walking on every keystroke.
        """
        cache = getattr(self, "_footage_rate_cache", None)
        if cache is not None and cache[0] == out_root:
            return cache[1]
        if getattr(self, "_footage_rate_pending", None) != out_root:
            self._footage_rate_pending = out_root

            def work() -> None:
                rate = measure_bytes_per_footage_minute(out_root)
                try:
                    self._sig_footage_rate.emit(out_root, rate)
                except RuntimeError:
                    pass  # window destroyed while the walk ran

            threading.Thread(target=work, name="footage-rate", daemon=True).start()
        return None

    def _on_footage_rate(self, out_root: object, rate: object) -> None:
        """Take a measurement only if it is still the one being waited for.

        A walk outlives the root that started it: the output root can be edited
        while it runs, and _on_survey_done clears both cache and marker so the
        finished batch is measured in. Either way an older walk landing last
        would otherwise install a figure for a root nobody is looking at, and
        clear the marker so the next repaint starts a second concurrent walk.
        """
        assert isinstance(out_root, Path)
        assert rate is None or isinstance(rate, float)
        if getattr(self, "_footage_rate_pending", None) != out_root:
            return
        self._footage_rate_pending = None
        self._footage_rate_cache = (out_root, rate)
        self._refresh_readiness_view()

    def _refresh_readiness_view(self) -> None:
        """Repaint the rows from a fresh probe, and record readiness once reached."""
        if not hasattr(self, "_setup_check_rows"):
            return
        checks = self._current_setup_checks()
        by_key = {check.key: check for check in checks}
        for key, (icon, detail, actions) in self._setup_check_rows.items():
            check = by_key[key]
            if check.ok:
                icon.setText(_TICK)
            else:
                icon.setText(_ALERT if check.advisory else _CROSS)
            detail.setText(check.detail)
            for action in actions:
                action.setVisible(check.actionable)
                if not isinstance(action, QPushButton):
                    continue
                label = check.action_label or str(action.property("full_label") or "")
                # Spelled out while the row needs fixing, a glyph once it passes.
                _paint_row_action(action, label=label, compact=check.ok)
                # Emphasis follows the verdict even though the action does not:
                # a filled button on a row with a tick pulls a diver towards
                # something that does not need doing, and away from the way on.
                if action.property("cta_when_unmet"):
                    _set_cta(action, not check.ok)
        ready = setup_ready(checks)
        unmet = sum(1 for check in checks if not check.ok and not check.advisory)
        self._setup_summary.setText(
            "All requirements met."
            if ready
            else f"{unmet} requirement{'' if unmet == 1 else 's'} not met. Processing will not run until fixed."
        )
        # When something is broken the loudest button on the page should be the
        # one that fixes it, not the one that carries on past it.
        _set_cta(self._setup_continue_btn, ready)
        if ready:
            # Once the machine can run, readiness stops leading on launch.
            self._settings.setValue("setup_complete", True)
        # The header button says the same thing from the other side of the
        # window, so it is repainted from the same verdict rather than its own.
        self._refresh_machine_button()

    def _initial_simple_section(self) -> str:
        """Lead to readiness on first launch, unless the laptop is already ready.

        Otherwise the footage: Videos is where a day starts, and it is the one
        destination that says something useful about a survey with nothing in
        it yet.
        """
        if str(self._settings.value("setup_complete", "false")).lower() == "true":
            return "videos"
        if setup_ready(self._current_setup_checks()):
            self._settings.setValue("setup_complete", True)
            return "videos"
        return "machine"

    def _on_setup_continue(self) -> None:
        # Acknowledging the check is itself a reason to stop leading with it:
        # the diver has seen the state and chosen to move on.
        self._settings.setValue("setup_complete", True)
        self._set_simple_section("videos")

    def _setup_drive_list(self) -> list:
        """Every drive worth listing: the ones the survey uses, and the one it
        writes to.

        The output root leads, because it is the drive this row is about, and on
        a laptop set up this morning it is the only one there is anything to say
        about. The rest come from the foot of the window's own measurements
        rather than a second scan, so the two never disagree.
        """
        from deepreefmap_gui.profiling.volumes import volume_for_path

        bars = getattr(self, "_storage_bars", None)
        measured = bars.volumes() if bars is not None else []
        out_root = Path(self._out_root_input.text()).expanduser()
        try:
            here = volume_for_path(str(out_root))
        except Exception:
            here = None
        if here is None:
            return measured
        # The measured copy of the same drive knows what the survey put on it,
        # so it wins; this only fills in a drive nothing has been written to yet.
        for volume in measured:
            if volume.root == here.root:
                return [volume] + [v for v in measured if v.root != here.root]
        return [here, *measured]

    def _on_setup_show_drives(self) -> None:
        """List the drives under the button, each one a way into its own page."""
        from deepreefmap_gui.core.storage_bar import VolumeListPopup

        popup = getattr(self, "_setup_drives_popup", None)
        if popup is None:
            popup = VolumeListPopup(self)
            popup.volume_clicked.connect(self._open_storage_page)
            self._setup_drives_popup = popup
        popup.show_volumes(self._setup_drive_list(), under=self._setup_drives_btn)

    def _on_setup_check_survey(self) -> None:
        """Re-run the database check from its row, and say what came back.

        check_survey_database is silent when the database is fine, which is right
        on launch and wrong for a button: a press with no answer reads as a dead
        control on a row that now offers one in both states.
        """
        self.check_survey_database()
        health = self._survey_db_health()
        self._status_label.setText(
            f"Survey database is readable and writable ({health.path})."
            if health.openable
            else health.detail or "The survey database could not be opened."
        )
        self._refresh_readiness_view()

    def _on_setup_import_pack(self) -> None:
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        self._on_import_model_pack()

    def _on_setup_download_models(self) -> None:
        """Download the models the current settings need, signing in first if asked."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        missing = self._survey_missing_models()
        if not missing:
            self._status_label.setText("All required models are already installed.")
            self._refresh_readiness_view()
            return
        from deepreefmap_gui.models.cache import all_known_models

        catalogue = {info.name: info for info in all_known_models()}
        needs_account = self._hf_auth_user is None and any(
            catalogue[name].gated for name in missing if name in catalogue
        )
        if needs_account:
            # Fold the sign-in into the one download action.
            self._status_label.setText("Some models require a free Hugging Face account. Sign in, then download again.")
            self._on_hf_auth_button()
            return
        for name in missing:
            self._download_model(name)
        self._status_label.setText(f"Downloading {len(missing)} model(s)…")
