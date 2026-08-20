"""The run form, the progress bar and the status strip.

The form is never on screen as part of the window: it is built into a hidden
holder and lent to the run settings dialog. Every setting a batch runs under
lives in these widgets, and _collect_run_settings() reads exactly them, so the
batch runs from what the dialog edited whether or not the dialog is open. What
varies per pass (the clips, the trim, the transect) comes from the pass table on
the Run step instead.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import cast

from PySide6.QtCore import (
    QFileSystemWatcher,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QDesktopServices,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import ICON_SM, log_icon
from deepreefmap_gui.core.spinner import BusySpinner, SpinnerStopButton
from deepreefmap_gui.core.storage_bar import StorageBars
from deepreefmap_gui.core.sync_badge import SyncBadge
from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BLOCK,
    BORDER,
    BUTTON,
    CARD_BG,
    ERROR,
    FONT_SM,
    FONT_XL,
    GUTTER,
    PAGE_MARGIN,
    PREVIEW_BG,
    PRIMARY,
    PRIMARY_DARK,
    RADIUS_SM,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_SECONDARY,
    UPDATE,
    WARN_TEXT,
    WEIGHT_BOLD,
    bar_qss,
)
from deepreefmap_gui.core.widgets import (
    EmptyState,
    lent_panel_home,
    muted_label,
    utility_button_qss,
    warning_banner_qss,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.packaging.releases import current_version, pyapp_binary_path
from deepreefmap_gui.profiling.volumes import group_by_volume
from deepreefmap_gui.runs.progress import (
    _LOAD_PHASES,
    _RECON_PHASES,
    ProgressModel,
    RunProgress,
)
from deepreefmap_gui.runs.sunburst import SunburstWidget
from deepreefmap_gui.system.log_view import LogView, install_qt_log_handler

# Transport controls sit in the bottom bar where they are the primary run
# affordance, so they are larger than the old 40px top-bar cluster.
_TRANSPORT_SIZE = 34
_TRANSPORT_ICON = 28

# Fill colours for the stacked progress pair. The total bar takes the darker
# shade so the two are told apart where they meet.
_STAGE_CHUNK = PRIMARY
_TOTAL_CHUNK = PRIMARY_DARK

# Slow enough to be free, often enough that a drive filling up during a long
# batch shows before the run that fails on it.
_STORAGE_REFRESH_MS = 15_000

# How long the bottom strip keeps the graphics card it found. Long enough to read
# after looking away, short enough that the strip is not permanently decorated.
_GPU_INDICATOR_LINGER_MS = 6_000


logger = logging.getLogger(__name__)


def _separator() -> QWidget:
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {BORDER};")
    return line


class CapacityRow(QWidget):
    """One pool's track: what it is called, how full the run would leave it, and
    whether it takes the run.

    Memory and the graphics card each get one. They are separate hardware and a
    run is held to both, so they are never merged into a single track: a bar
    drawn against whichever pool happened to be tighter read as a comfortable
    machine on the run the card could not take.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        from deepreefmap_gui.core.widgets import MeterBar

        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        self.title = QLabel()
        self.title.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: {FONT_SM}; font-weight: {WEIGHT_BOLD};"
        )
        layout.addWidget(self.title)

        self.bar = MeterBar()
        layout.addWidget(self.bar)

        # Names each part of the track, in the order it is painted.
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
        layout.addWidget(self.legend)

        verdict_row = QHBoxLayout()
        verdict_row.setContentsMargins(0, 0, 0, 0)
        verdict_row.setSpacing(SPACE_XS)
        self.icon = QLabel()
        self.icon.setFixedWidth(ICON_SM)
        # Top-aligned: the message wraps to several lines and the mark belongs
        # beside the first of them.
        verdict_row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.message = QLabel()
        self.message.setWordWrap(True)
        # The figures inside it are marked, so the number a setting moves is the
        # part of the line that reads first.
        self.message.setTextFormat(Qt.TextFormat.RichText)
        verdict_row.addWidget(self.message, 1)
        layout.addLayout(verdict_row)

    def set_resource(self, resource, *, held_colour: str) -> None:
        """Paint one graded pool: bar, legend, and the verdict under it."""
        from deepreefmap_gui.core.icons import (
            blocked_icon,
            check_icon,
            icon_pixmap,
            status_dot_icon,
            warning_icon,
        )
        from deepreefmap_gui.core.storage_bar import FREE_SWATCH
        from deepreefmap_gui.profiling.system_probe import format_bytes

        self.title.setText(resource.label)
        colour = {"ok": SUCCESS, "warn": UPDATE, "block": BLOCK, "none": TEXT_MUTED}[
            resource.level
        ]
        draw = {
            "ok": check_icon,
            "warn": warning_icon,
            "block": blocked_icon,
            "none": lambda size: status_dot_icon(TEXT_MUTED, size),
        }[resource.level]
        self.icon.setPixmap(icon_pixmap(draw(ICON_SM), ICON_SM, self.devicePixelRatio()))
        self.message.setStyleSheet(
            f"color: {TEXT_MUTED if resource.level in ('ok', 'none') else colour}; "
            f"font-size: {FONT_SM};"
        )
        self.message.setText(resource.message)

        if not resource.available:
            self.bar.set_unavailable()
            self.bar.setToolTip("")
            self.legend.clear()
            return

        # The whole pool, not just the free part of it: what other applications
        # are already in, then what a run would take on top of that, then what
        # neither has.
        held = resource.held_bytes
        pool = resource.budget_bytes + held
        need = resource.need_bytes
        self.bar.set_level(
            100.0 * need / pool if pool else 0.0,
            colour,
            100.0 * held / pool if pool else 0.0,
        )
        parts = []
        if held:
            parts.append((held_colour, "■", "Other applications", held))
        parts.append((colour, "■", "Needed", need))
        free = max(0, pool - need - held)
        if free:
            parts.append((FREE_SWATCH, "□", "Free", free))
        # A level that runs off the track is clipped there, so the part that did
        # not fit is only readable if it is named.
        over = max(0, need + held - pool)
        if over:
            parts.append((colour, "▸", "Over by", over))
        self.legend.setText(
            "&nbsp;&nbsp;&nbsp;".join(
                f'<span style="color:{swatch};">{mark}</span> {label} '
                f"{format_bytes(value)}"
                for swatch, mark, label, value in parts
            )
        )
        self.bar.setToolTip(
            "\n".join(f"{label}: {format_bytes(value)}" for _, _, label, value in parts)
        )


class FormPanelMixin(MixinBase):
    """DeepReefMapWindow methods that build and drive the run form and the status bars."""

    def _build_form_widgets(self) -> None:
        from deepreefmap.camera.intrinsics import available_profile_names
        from deepreefmap.mapping.registry import list_mapping_backends

        from deepreefmap_gui.models.cache import segmentation_model_names

        profiles = available_profile_names() or ["gopro_hero_10"]
        # Not the library's registry: it imports torch, which costs half a second
        # here and several on a Windows laptop, in the middle of building the
        # window. Nothing on the way to the first frame may pull torch in.
        seg_models = segmentation_model_names()
        map_backends = list_mapping_backends()
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default_root = str(Path(documents or str(Path.home())) / "DeepReefMap")

        (
            setup_layout,
            viewer_layout,
            models_layout,
            system_layout,
            updates_layout,
        ) = self._build_panel_homes()

        self._build_deferred_top_bar_widgets(setup_layout)
        # Memory leads: it is the setting that decides whether the batch runs at
        # all, and the frame rate that moves it is the control beside it. The
        # camera profile is one choice per camera and closes the form.
        self._build_memory_group(setup_layout)
        self._build_model_selection_group(setup_layout, seg_models, map_backends)
        self._build_output_group(setup_layout, default_root)
        self._build_advanced_toggle_and_notices(setup_layout)
        self._build_advanced_panel(setup_layout)
        self._build_input_group(setup_layout, profiles)
        self._build_run_control_buttons()
        self._build_gated_warning(setup_layout)
        self._build_run_warnings_and_log(viewer_layout)
        self._build_progress_widgets()
        self._build_results_group(viewer_layout)
        self._build_models_panel(models_layout)
        self._build_updates_section(updates_layout)

        # Start in SETUP, no run loaded yet. The mode flips to RUNNING when a
        # batch starts and to VIEWING when a past run is selected or a
        # reconstruction completes.
        self._set_app_mode("SETUP")

    def _build_panel_homes(self) -> tuple[QVBoxLayout, ...]:
        """A permanent home for each panel that is shown somewhere else.

        None of these are on screen as built. The run form is lent to the run
        settings dialog; the results, model, system and update panels are lent to
        View mode and to Setup. Every one gets a holder of its own rather than
        sharing a layout, because a panel handed back into a shared layout lands
        wherever that layout happens to append it.

        Parented to the window and hidden: a parentless widget made visible maps
        itself as a top-level window, which flashes an empty titlebar on screen.
        """
        # The run form. RunSettingsDialog takes this page, wraps it in a scroll
        # area and puts it back on close, by both exit paths. Without a holder
        # that outlives the dialog the form would be left parented to something
        # being destroyed, and every later _collect_run_settings() would raise
        # on a deleted C++ object.
        self._form_home = QWidget(self)
        self._form_home.setVisible(False)
        self._form_home_layout = QVBoxLayout(self._form_home)
        self._form_home_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_page = QWidget()
        setup_layout = QVBoxLayout(self._setup_page)
        setup_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        self._form_home_layout.addWidget(self._setup_page)

        # What a finished run produced: quality warnings, the ortho previews,
        # benthic cover, the transect crop and the exports. Shown in View mode's
        # info panel, beside the cloud they all describe.
        self._results_page = QWidget()
        results_layout = QVBoxLayout(self._results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._models_home = QWidget(self)
        self._models_home.setVisible(False)
        models_layout = QVBoxLayout(self._models_home)
        models_layout.setContentsMargins(0, 0, 0, 0)
        models_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._system_home, self._system_page, system_layout = lent_panel_home(self)
        self._build_system_panel(system_layout)
        # The updater is a page of its own on Setup rather than a footnote under
        # the gauges: it is the one thing there that changes the software itself.
        self._updates_home, self._updates_page, updates_layout = lent_panel_home(self)
        return (
            setup_layout,
            results_layout,
            models_layout,
            system_layout,
            updates_layout,
        )

    def _build_deferred_top_bar_widgets(self, setup_layout: QVBoxLayout) -> None:
        # These widgets are owned by the top toolbar but constructed here so
        # initialisation code can reference them before the toolbar is laid out.

        # Log toggle button, checkable so the pressed state mirrors panel
        # visibility. The panel is opened on request only: a batch reports
        # itself on the Run step, and the log is for looking closer.
        #
        # A QToolButton carrying the shared utility stylesheet, because it sits
        # in the header beside Setup and the two have to be the same
        # control. A QPushButton on a hard-coded height was neither.
        self._log_toggle_btn = QToolButton()
        self._log_toggle_btn.setText("Log")
        self._log_toggle_btn.setIcon(log_icon())
        self._log_toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._log_toggle_btn.setToolTip("Show or hide the live log panel")
        self._log_toggle_btn.setCheckable(True)
        self._log_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._log_toggle_btn.setStyleSheet(utility_button_qss())
        self._log_toggle_btn.toggled.connect(self._set_log_panel_visible)

        self._warnings_label_running = QLabel("")
        self._warnings_label_running.setWordWrap(True)
        self._warnings_label_running.setTextFormat(Qt.TextFormat.RichText)
        self._warnings_label_running.setStyleSheet(
            warning_banner_qss()
        )
        self._warnings_label_running.setVisible(False)
        setup_layout.addWidget(self._warnings_label_running)

    def _build_memory_group(self, setup_layout: QVBoxLayout) -> None:
        """What the batch would cost this machine, on the pools it is held to."""
        memory_group = QGroupBox("Memory")
        self._build_capacity_readout(QVBoxLayout(memory_group))
        setup_layout.addWidget(memory_group)

    def _build_input_group(self, setup_layout: QVBoxLayout, profiles: list[str]) -> None:
        """Which lens the footage was shot through: one choice per camera, shared
        by the whole batch, so it closes the form rather than opening it. The
        clips themselves come from the pass table on the Run step."""
        input_group = QGroupBox("Input")
        ig = QVBoxLayout(input_group)
        ig.addWidget(QLabel("Camera profile"))
        self._profile_combo = QComboBox()
        self._profile_combo.addItems(profiles)
        ig.addWidget(self._profile_combo)
        setup_layout.addWidget(input_group)

    def _build_capacity_readout(self, ig: QVBoxLayout) -> None:
        """How much of the machine the longest queued pass would use.

        One row per pool, always both: a run has to fit the memory and the
        graphics card at once, and either of them can be the one that refuses it.
        """
        self._capacity_rows: dict[str, CapacityRow] = {}
        for key in ("ram", "vram"):
            row = CapacityRow()
            self._capacity_rows[key] = row
            ig.addWidget(row)
            ig.addSpacing(SPACE_XS)

        self._capacity_detail = QLabel()
        self._capacity_detail.setWordWrap(True)
        self._capacity_detail.setTextFormat(Qt.TextFormat.RichText)
        self._capacity_detail.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
        ig.addWidget(self._capacity_detail)

        self._capacity_advice = QLabel()
        self._capacity_advice.setWordWrap(True)
        self._capacity_advice.setTextFormat(Qt.TextFormat.RichText)
        self._capacity_advice.setVisible(False)
        self._capacity_advice.linkActivated.connect(lambda _: self._reveal_memory_detail())
        ig.addWidget(self._capacity_advice)

        # Last, and dimmest: what was fed to the model is the footnote to the
        # figures, not the headline over them.
        self._capacity_caption = QLabel()
        self._capacity_caption.setWordWrap(True)
        self._capacity_caption.setTextFormat(Qt.TextFormat.RichText)
        self._capacity_caption.setStyleSheet(f"color: {TEXT_DIM}; font-size: {FONT_SM};")
        ig.addWidget(self._capacity_caption)

    def _build_model_selection_group(
        self, setup_layout: QVBoxLayout, seg_models: list[str], map_backends: list[str]
    ) -> None:
        from deepreefmap.mapping.registry import loger_available

        from deepreefmap_gui.models.loger_hint import LOGER_INSTALL_HINT

        models_group = QGroupBox("Models")
        mg = QVBoxLayout(models_group)

        mg.addWidget(QLabel("Segmentation"))
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(0, 0, 0, 0)
        seg_row.setSpacing(4)
        self._seg_combo = QComboBox()
        self._seg_combo.addItems(seg_models)
        idx = self._seg_combo.findText("coralscapes-vit-b-dpt")
        if idx >= 0:
            self._seg_combo.setCurrentIndex(idx)
        seg_row.addWidget(self._seg_combo, 1)
        self._seg_status_btn = self._build_model_status_button(self._seg_combo)
        seg_row.addWidget(self._seg_status_btn)
        mg.addLayout(seg_row)

        mg.addWidget(QLabel("Mapping"))
        map_row = QHBoxLayout()
        map_row.setContentsMargins(0, 0, 0, 0)
        map_row.setSpacing(4)
        self._map_combo = QComboBox()
        self._map_combo.addItems(map_backends)
        default_map = "loger_star" if loger_available() else "scsfmlearner"
        idx = self._map_combo.findText(default_map)
        if idx >= 0:
            self._map_combo.setCurrentIndex(idx)
        if not loger_available():
            map_model = self._map_combo.model()
            if isinstance(map_model, QStandardItemModel):
                for i in range(self._map_combo.count()):
                    if self._map_combo.itemText(i) in ("loger", "loger_star"):
                        item = map_model.item(i)
                        item.setEnabled(False)
                        item.setData(LOGER_INSTALL_HINT, Qt.ItemDataRole.ToolTipRole)
        map_row.addWidget(self._map_combo, 1)
        self._map_status_btn = self._build_model_status_button(self._map_combo)
        map_row.addWidget(self._map_status_btn)
        mg.addLayout(map_row)

        # How densely the footage is fed to those models, so it reads with them
        # rather than as a property of the camera. It is also the control that
        # moves the memory readout above.
        mg.addWidget(QLabel("Frames per second"))
        fps_row = QHBoxLayout()
        fps_row.setContentsMargins(0, 0, 0, 0)
        fps_row.setSpacing(SPACE_SM)
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(5)
        self._fps_spin.setFixedWidth(80)
        fps_row.addWidget(self._fps_spin)
        fps_row.addWidget(
            muted_label("Frames read from each video, per second of footage."), 1
        )
        mg.addLayout(fps_row)

        setup_layout.addWidget(models_group)

    def _build_output_group(self, setup_layout: QVBoxLayout, default_root: str) -> None:
        output_group = QGroupBox("Output")
        # Not a setting: it is the root a whole survey lands under, which is a
        # property of this computer. The run settings dialog hides the group
        # whole for that reason, which also keeps it from rendering as an empty
        # titled frame once Setup has borrowed the controls out of it.
        self._output_group = output_group
        og = QVBoxLayout(output_group)

        style = self.style()
        # Wrapped in one widget so it can be lent to Setup, which is
        # where the output root is edited. Offering a second place to change it
        # would invite two answers to one question.
        self._out_root_widget = QWidget()
        out_root_layout = QVBoxLayout(self._out_root_widget)
        out_root_layout.setContentsMargins(0, 0, 0, 0)
        out_root_layout.setSpacing(SPACE_XS)
        label_row = QHBoxLayout()
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(4)
        label_row.addWidget(QLabel("Output root"))
        from deepreefmap_gui.core.icons import arrow_right_icon

        root_open_btn = QPushButton()
        root_open_btn.setIcon(arrow_right_icon(18))
        root_open_btn.setFixedSize(26, 24)
        root_open_btn.setToolTip("Open output root in file manager")
        root_open_btn.setAccessibleName("Open output root in file manager")
        root_open_btn.clicked.connect(self._open_output_root)
        label_row.addWidget(root_open_btn)
        label_row.addStretch(1)
        out_root_layout.addLayout(label_row)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(4)
        self._out_root_input = QLineEdit(default_root)
        input_row.addWidget(self._out_root_input, 1)
        root_browse_btn = QPushButton()
        root_browse_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        root_browse_btn.setIconSize(QSize(18, 18))
        root_browse_btn.setFixedSize(28, 28)
        root_browse_btn.setToolTip("Browse for output root folder…")
        root_browse_btn.setAccessibleName("Browse for output root folder")
        root_browse_btn.clicked.connect(self._browse_output_root)
        input_row.addWidget(root_browse_btn)
        root_default_btn = QPushButton()
        root_default_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        root_default_btn.setIconSize(QSize(18, 18))
        root_default_btn.setFixedSize(28, 28)
        root_default_btn.setToolTip("Reset to <Documents>/DeepReefMap")
        root_default_btn.setAccessibleName("Reset the output root to its default")
        root_default_btn.clicked.connect(self._reset_output_root_to_default)
        input_row.addWidget(root_default_btn)
        out_root_layout.addLayout(input_row)
        # An empty holder rather than the group itself, so the controls can be
        # lent to Setup and handed back to the same place. The advanced
        # form and the machine page describe one folder, so they share one set
        # of widgets rather than each carrying their own.
        self._out_root_home = QWidget()
        out_root_home_layout = QVBoxLayout(self._out_root_home)
        out_root_home_layout.setContentsMargins(0, 0, 0, 0)
        out_root_home_layout.addWidget(self._out_root_widget)
        og.addWidget(self._out_root_home)

        setup_layout.addWidget(output_group)

    def _build_advanced_toggle_and_notices(self, setup_layout: QVBoxLayout) -> None:
        self._advanced_toggle = QCheckBox("Advanced settings")
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        setup_layout.addWidget(self._advanced_toggle)

        # The capacity readout below is the only memory verdict, linked through
        # to the readiness rows that repeat it in plainer words. Anything
        # that changes the projected frame count re-grades the run.
        self._fps_spin.valueChanged.connect(self._update_memory_profile_warning)

    def _build_advanced_panel(self, setup_layout: QVBoxLayout) -> None:
        self._advanced_panel = QWidget()
        adv_layout = QVBoxLayout(self._advanced_panel)
        adv_layout.setContentsMargins(12, 0, 0, 0)
        self._build_advanced_transect_crop(adv_layout)
        self._build_advanced_resolution(adv_layout)
        self._build_advanced_batch_and_radius(adv_layout)
        self._build_scs_panel(adv_layout)
        self._build_loger_panel(adv_layout)
        self._advanced_panel.setVisible(False)
        setup_layout.addWidget(self._advanced_panel)

    def _build_advanced_transect_crop(self, adv_layout: QVBoxLayout) -> None:
        # Width only. A pass takes its transect length from the transect it was
        # assigned to on the Plan step, so there is nothing here to set it with.
        adv_layout.addWidget(QLabel("Crop width (m), 0 disables"))
        self._crop_width = QDoubleSpinBox()
        self._crop_width.setRange(0.0, 50.0)
        self._crop_width.setDecimals(2)
        self._crop_width.setSingleStep(0.1)
        self._crop_width.setValue(0.0)
        self._crop_width.setSuffix(" m")
        adv_layout.addWidget(self._crop_width)

    def _build_advanced_resolution(self, adv_layout: QVBoxLayout) -> None:
        from deepreefmap_gui.models.families import model_processing_size

        default_seg = self._seg_combo.currentText()
        self._native_resolution = model_processing_size(default_seg) or (1376, 768)
        self._is_dpt_model = "dpt" in default_seg

        adv_layout.addWidget(QLabel("Processing resolution"))
        self._resolution_preset_combo = QComboBox()
        self._resolution_preset_combo.addItems(["Native", "Half", "Quarter", "Custom"])
        self._resolution_preset_combo.setToolTip(
            "Resolution preset relative to the segmentation model's training resolution."
        )
        adv_layout.addWidget(self._resolution_preset_combo)

        res_row = QHBoxLayout()
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.setSpacing(4)
        w_col = QVBoxLayout()
        w_col.setContentsMargins(0, 0, 0, 0)
        w_col.addWidget(QLabel("Width"))
        self._proc_width_spin = QSpinBox()
        self._proc_width_spin.setRange(256, 3840)
        self._proc_width_spin.setSingleStep(32)
        self._proc_width_spin.setValue(self._native_resolution[0])
        self._proc_width_spin.setEnabled(False)
        w_col.addWidget(self._proc_width_spin)
        res_row.addLayout(w_col, 1)
        h_col = QVBoxLayout()
        h_col.setContentsMargins(0, 0, 0, 0)
        h_col.addWidget(QLabel("Height"))
        self._proc_height_spin = QSpinBox()
        self._proc_height_spin.setRange(256, 2160)
        self._proc_height_spin.setSingleStep(32)
        self._proc_height_spin.setValue(self._native_resolution[1])
        self._proc_height_spin.setEnabled(False)
        h_col.addWidget(self._proc_height_spin)
        res_row.addLayout(h_col, 1)
        adv_layout.addLayout(res_row)

        self._dpt_resolution_warning = QLabel(
            "DPT models have no internal resize, so non-native resolution may reduce accuracy."
        )
        self._dpt_resolution_warning.setWordWrap(True)
        self._dpt_resolution_warning.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM};")
        self._dpt_resolution_warning.setVisible(False)
        adv_layout.addWidget(self._dpt_resolution_warning)

        self._resolution_preset_combo.currentTextChanged.connect(
            self._on_resolution_preset_changed
        )
        self._proc_width_spin.valueChanged.connect(self._on_processing_settings_changed)
        self._proc_height_spin.valueChanged.connect(self._on_processing_settings_changed)

    def _build_advanced_batch_and_radius(self, adv_layout: QVBoxLayout) -> None:
        adv_layout.addWidget(QLabel("Segmentation batch size"))
        self._batch_size_spin = QSpinBox()
        self._batch_size_spin.setRange(1, 16)
        self._batch_size_spin.setValue(4)
        self._batch_size_spin.setToolTip(
            "Frames segmented per GPU batch. Lower values use less VRAM."
        )
        self._batch_size_spin.valueChanged.connect(self._on_processing_settings_changed)
        adv_layout.addWidget(self._batch_size_spin)
        self._vram_auto_label = QLabel()
        self._vram_auto_label.setWordWrap(True)
        self._vram_auto_label.setStyleSheet(f"color: {UPDATE}; font-size: {FONT_SM};")
        adv_layout.addWidget(self._vram_auto_label)
        self._reset_defaults_btn = QPushButton("Reset to defaults")
        self._reset_defaults_btn.clicked.connect(self._reset_advanced_defaults)
        adv_layout.addWidget(self._reset_defaults_btn)
        self._on_processing_settings_changed()
        adv_layout.addWidget(QLabel("Grid bins (ortho resolution)"))
        self._grid_bins_spin = QSpinBox()
        self._grid_bins_spin.setRange(100, 10000)
        self._grid_bins_spin.setSingleStep(100)
        self._grid_bins_spin.setValue(2000)
        self._grid_bins_spin.setToolTip("Number of bins for the ortho projection grid.")
        adv_layout.addWidget(self._grid_bins_spin)
        adv_layout.addWidget(QLabel("Replacement radius factor, 0 = auto"))
        self._rr_factor_spin = QDoubleSpinBox()
        self._rr_factor_spin.setRange(0.0, 10.0)
        self._rr_factor_spin.setDecimals(2)
        self._rr_factor_spin.setSingleStep(0.1)
        self._rr_factor_spin.setValue(0.0)
        self._rr_factor_spin.setToolTip("Multiplier on the auto replacement radius. 0 = use auto estimate.")
        adv_layout.addWidget(self._rr_factor_spin)
        adv_layout.addWidget(QLabel("Replacement radius estimation frames"))
        self._rr_est_frames_spin = QSpinBox()
        self._rr_est_frames_spin.setRange(1, 200)
        self._rr_est_frames_spin.setValue(30)
        self._rr_est_frames_spin.setToolTip(
            "Number of leading depth maps used to estimate the default replacement radius."
        )
        adv_layout.addWidget(self._rr_est_frames_spin)
        adv_layout.addWidget(QLabel("Replacement radius override (m), 0 = auto"))
        self._rr_override_spin = QDoubleSpinBox()
        self._rr_override_spin.setRange(0.0, 10.0)
        self._rr_override_spin.setDecimals(4)
        self._rr_override_spin.setSingleStep(0.001)
        self._rr_override_spin.setValue(0.0)
        self._rr_override_spin.setToolTip("Absolute replacement voxel size in meters. 0 = use auto estimate.")
        adv_layout.addWidget(self._rr_override_spin)
        self._tsdf_check = QCheckBox("Enable TSDF")
        adv_layout.addWidget(self._tsdf_check)
        self._require_gravity_check = QCheckBox("Require gravity telemetry")
        self._require_gravity_check.setToolTip("Fail if GoPro gravity telemetry cannot be loaded.")
        adv_layout.addWidget(self._require_gravity_check)
        self._skip_seg_check = QCheckBox("Skip segmentation")
        adv_layout.addWidget(self._skip_seg_check)

    def _build_scs_panel(self, adv_layout: QVBoxLayout) -> None:
        # SCSfMLearner-specific knobs, only shown when scsfmlearner is selected.
        style = self.style()
        self._scs_panel = QWidget()
        scs_layout = QVBoxLayout(self._scs_panel)
        scs_layout.setContentsMargins(0, 6, 0, 0)
        scs_layout.setSpacing(2)
        scs_layout.addWidget(QLabel("SCSfMLearner width"))
        self._scs_width_spin = QSpinBox()
        self._scs_width_spin.setRange(64, 2048)
        self._scs_width_spin.setSingleStep(32)
        self._scs_width_spin.setValue(512)
        scs_layout.addWidget(self._scs_width_spin)
        scs_layout.addWidget(QLabel("SCSfMLearner height"))
        self._scs_height_spin = QSpinBox()
        self._scs_height_spin.setRange(64, 2048)
        self._scs_height_spin.setSingleStep(32)
        self._scs_height_spin.setValue(256)
        scs_layout.addWidget(self._scs_height_spin)
        scs_layout.addWidget(QLabel("SCSfMLearner checkpoint (optional override)"))
        scs_ckpt_row = QHBoxLayout()
        scs_ckpt_row.setContentsMargins(0, 0, 0, 0)
        scs_ckpt_row.setSpacing(4)
        self._scs_checkpoint_input = QLineEdit()
        self._scs_checkpoint_input.setPlaceholderText("Default: EPFL-ECEO/deepreefmap-sfm-net")
        scs_ckpt_row.addWidget(self._scs_checkpoint_input, 1)
        scs_ckpt_btn = QPushButton()
        scs_ckpt_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        scs_ckpt_btn.setIconSize(QSize(18, 18))
        scs_ckpt_btn.setFixedSize(28, 28)
        scs_ckpt_btn.setToolTip("Browse for a SCSfMLearner .pt checkpoint…")
        scs_ckpt_btn.setAccessibleName("Browse for a SCSfMLearner checkpoint")
        scs_ckpt_btn.clicked.connect(self._browse_scs_checkpoint)
        scs_ckpt_row.addWidget(scs_ckpt_btn)
        scs_layout.addLayout(scs_ckpt_row)
        self._scs_panel.setVisible(False)
        adv_layout.addWidget(self._scs_panel)

    def _build_loger_panel(self, adv_layout: QVBoxLayout) -> None:
        # LoGeR-specific knobs, only shown when a LoGeR backend is selected.
        style = self.style()
        self._loger_panel = QWidget()
        loger_layout = QVBoxLayout(self._loger_panel)
        loger_layout.setContentsMargins(0, 6, 0, 0)
        loger_layout.setSpacing(2)
        loger_layout.addWidget(QLabel("LoGeR window size"))
        self._loger_window_spin = QSpinBox()
        self._loger_window_spin.setRange(1, 256)
        self._loger_window_spin.setValue(32)
        loger_layout.addWidget(self._loger_window_spin)
        loger_layout.addWidget(QLabel("LoGeR overlap size"))
        self._loger_overlap_spin = QSpinBox()
        self._loger_overlap_spin.setRange(0, 64)
        self._loger_overlap_spin.setValue(3)
        loger_layout.addWidget(self._loger_overlap_spin)
        loger_layout.addWidget(QLabel("LoGeR checkpoint (optional override)"))
        loger_ckpt_row = QHBoxLayout()
        loger_ckpt_row.setContentsMargins(0, 0, 0, 0)
        loger_ckpt_row.setSpacing(4)
        self._loger_model_path_input = QLineEdit()
        self._loger_model_path_input.setPlaceholderText("Default: vendored checkpoint")
        loger_ckpt_row.addWidget(self._loger_model_path_input, 1)
        loger_ckpt_btn = QPushButton()
        loger_ckpt_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        loger_ckpt_btn.setIconSize(QSize(18, 18))
        loger_ckpt_btn.setFixedSize(28, 28)
        loger_ckpt_btn.setToolTip("Browse for a LoGeR .pt checkpoint…")
        loger_ckpt_btn.setAccessibleName("Browse for a LoGeR checkpoint")
        loger_ckpt_btn.clicked.connect(self._browse_loger_checkpoint)
        loger_ckpt_row.addWidget(loger_ckpt_btn)
        loger_layout.addLayout(loger_ckpt_row)
        self._refine_intrinsics_check = QCheckBox("Refine intrinsics from mapper")
        loger_layout.addWidget(self._refine_intrinsics_check)
        self._loger_panel.setVisible(False)
        adv_layout.addWidget(self._loger_panel)

    def _build_gated_warning(self, setup_layout: QVBoxLayout) -> None:
        self._gated_warning = QLabel()
        self._gated_warning.setWordWrap(True)
        self._gated_warning.setTextFormat(Qt.TextFormat.RichText)
        self._gated_warning.setOpenExternalLinks(True)
        self._gated_warning.setStyleSheet(warning_banner_qss() + f" font-size: {FONT_SM};")
        self._gated_warning.setVisible(False)
        setup_layout.addWidget(self._gated_warning)

    def _build_run_warnings_and_log(self, viewer_layout: QVBoxLayout) -> None:
        # Sticky banner for non-fatal quality warnings emitted during a run
        # (preprocess detected mostly-background frames, missing transect line,
        # etc.). Cleared at the start of each new run. Lives on the Results
        # tab so it surfaces alongside the results it warns about, plus is
        # mirrored on the running page so the user sees them as they happen.
        self._warnings_label = QLabel("")
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setTextFormat(Qt.TextFormat.RichText)
        self._warnings_label.setStyleSheet(
            warning_banner_qss()
        )
        self._warnings_label.setVisible(False)
        self._run_warnings: list[str] = []
        viewer_layout.addWidget(self._warnings_label)

        # Live log view: lives in a bottom panel built by _build_log_panel and
        # assembled into the main window's vertical splitter in app.py.
        # Construction is here so the panel is ready to receive log lines as
        # soon as the qt log handler installs (next block).
        self._log_view = LogView()

        # The log handler streams every deepreefmap.* log line into the panel.
        # A per-run FileHandler, when one is open, is closed on the run's
        # completion paths and again on window teardown.
        self._qt_log_handler = install_qt_log_handler()
        self._qt_log_handler.line_signal.connect(self._log_view.append_line)

    def _build_progress_widgets(self) -> None:
        # Status label and progress bar are owned by the top toolbar but
        # constructed here so they exist before anything reports into them.
        self._status_label = QLabel("Ready.")
        self._status_label.setWordWrap(True)

        # Where the run has got to, as numbers: the running pass reports on its
        # own queue row, and the strip at the foot carries whatever is running.
        self._run_progress = RunProgress()

        # Overall (all-stages) remaining estimate, kept visible rather than buried
        # in the hover breakdown.
        self._eta_total_label = muted_label("")
        self._eta_total_label.setMinimumWidth(78)

        self._recon_model = ProgressModel(_RECON_PHASES)
        self._load_model = ProgressModel(_LOAD_PHASES)
        self._active_progress_model: ProgressModel | None = None

    def _build_run_control_buttons(self) -> None:
        # Transport controls live at the bottom-right of the window, next to the
        # progress bar they drive. They belong with the progress bar they
        # interrupt, and only exist while a run is in flight. Starting one is not
        # here: the Run step's own button launches the batch.
        from deepreefmap_gui.core.icons import pause_icon

        self._pause_btn = QPushButton()
        self._pause_btn.setProperty("pad", "none")
        self._pause_btn.setIcon(pause_icon(_TRANSPORT_ICON))
        self._pause_btn.setToolTip(
            "Pause at the next safe checkpoint. Long mapping passes may take "
            "time to respond."
        )
        self._pause_btn.setAccessibleName("Pause the reconstruction")
        self._pause_btn.setCheckable(True)
        self._pause_btn.setFixedSize(_TRANSPORT_SIZE, _TRANSPORT_SIZE)
        self._pause_btn.setVisible(False)
        self._pause_btn.toggled.connect(self._on_pause_toggled)

        self._spinner_stop = SpinnerStopButton(size=_TRANSPORT_SIZE)
        self._spinner_stop.setVisible(False)
        self._spinner_stop.clicked.connect(self._on_stop_clicked)

    def _build_results_group(self, viewer_layout: QVBoxLayout) -> None:
        # The legend lives as a floating overlay on the 3D canvas; this dict
        # is populated by _build_legend and queried by _enabled_class_set.
        self._legend_toggles: dict[int, QCheckBox] = {}
        # Legend sort state: column "selected" (visibility), "name", or "size",
        # plus a direction. _legend_order_cache lets _apply_legend_sort skip
        # reflows when the resulting order is unchanged.
        self._legend_sort_mode: str = "selected"
        self._legend_sort_ascending: bool = False
        self._legend_order_cache: list[int] | None = None
        self._legend_sort_connected: bool = False

        self._results_group = QGroupBox("Results")
        self._results_group.setVisible(False)
        res_layout = QVBoxLayout(self._results_group)

        ortho_row = QHBoxLayout()
        ortho_row.setSpacing(4)
        self._ortho_rgb_preview = QLabel("RGB ortho")
        self._ortho_rgb_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ortho_rgb_preview.setMinimumSize(160, 100)
        self._ortho_rgb_preview.setStyleSheet(f"background-color: {PREVIEW_BG}; color: {TEXT_MUTED};")
        ortho_row.addWidget(self._ortho_rgb_preview, 1)
        self._ortho_seg_preview = QLabel("Seg ortho")
        self._ortho_seg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ortho_seg_preview.setMinimumSize(160, 100)
        self._ortho_seg_preview.setStyleSheet(f"background-color: {PREVIEW_BG}; color: {TEXT_MUTED};")
        ortho_row.addWidget(self._ortho_seg_preview, 1)
        res_layout.addLayout(ortho_row)

        crop_box = QGroupBox("Transect crop (live)")
        crop_box.setVisible(False)
        crop_layout = QGridLayout(crop_box)
        crop_layout.addWidget(QLabel("Transect length (m)"), 0, 0)
        self._results_transect_length = QDoubleSpinBox()
        self._results_transect_length.setRange(0.0, 100.0)
        self._results_transect_length.setDecimals(2)
        self._results_transect_length.setSingleStep(0.1)
        self._results_transect_length.setValue(0.0)
        crop_layout.addWidget(self._results_transect_length, 0, 1)
        self._results_transect_slider = QSlider(Qt.Orientation.Horizontal)
        self._results_transect_slider.setRange(0, 10000)
        crop_layout.addWidget(self._results_transect_slider, 1, 0, 1, 2)
        crop_layout.addWidget(QLabel("Crop width (m)"), 2, 0)
        self._results_crop_width = QDoubleSpinBox()
        self._results_crop_width.setRange(0.0, 50.0)
        self._results_crop_width.setDecimals(2)
        self._results_crop_width.setSingleStep(0.1)
        self._results_crop_width.setValue(0.0)
        crop_layout.addWidget(self._results_crop_width, 2, 1)
        self._results_crop_slider = QSlider(Qt.Orientation.Horizontal)
        self._results_crop_slider.setRange(0, 5000)
        crop_layout.addWidget(self._results_crop_slider, 3, 0, 1, 2)
        self._crop_box = crop_box
        res_layout.addWidget(crop_box)

        self._results_transect_length.valueChanged.connect(self._on_results_transect_length_changed)
        self._results_crop_width.valueChanged.connect(self._on_results_crop_width_changed)
        self._results_transect_slider.valueChanged.connect(self._on_results_transect_slider_changed)
        self._results_crop_slider.valueChanged.connect(self._on_results_crop_slider_changed)

        # Two-ring sunburst (outer = fine classes, inner = coarse groups) sits
        # in the band beside the frame stack, so the cover reads as a result
        # rather than as chrome over the cloud. Clicking a slice still drives
        # the legend's class toggles. Updates live with the crop.
        self._cover_sunburst = SunburstWidget()
        self._cover_sunburst.selection_clicked.connect(self._on_sunburst_selection)
        self._cover_sunburst.setVisible(False)
        self._viewer.set_cover_widget(self._cover_sunburst)

        self._cover_label = QLabel()
        self._cover_label.setWordWrap(True)
        res_layout.addWidget(self._cover_label)

        self._open_dir_btn = QPushButton("Open output directory")
        self._open_dir_btn.clicked.connect(self._open_output_dir)
        res_layout.addWidget(self._open_dir_btn)

        exports_grid = QGridLayout()
        exports_grid.setHorizontalSpacing(6)
        exports_grid.setVerticalSpacing(4)
        self._export_ortho_npz_btn = QPushButton("Save ortho (NPZ)")
        self._export_ortho_npz_btn.clicked.connect(self._on_export_ortho_npz)
        exports_grid.addWidget(self._export_ortho_npz_btn, 0, 0)
        self._export_ortho_png_btn = QPushButton("Save ortho preview (PNG)")
        self._export_ortho_png_btn.clicked.connect(self._on_export_ortho_png)
        exports_grid.addWidget(self._export_ortho_png_btn, 0, 1)
        self._export_cover_btn = QPushButton("Save benthic cover (CSV)")
        self._export_cover_btn.clicked.connect(self._on_export_cover_csv)
        exports_grid.addWidget(self._export_cover_btn, 1, 0)
        self._export_zip_btn = QPushButton("Zip output directory")
        self._export_zip_btn.clicked.connect(self._on_export_zip)
        exports_grid.addWidget(self._export_zip_btn, 1, 1)
        self._export_qc_video_btn = QPushButton("Render QC video (MP4)")
        self._export_qc_video_btn.clicked.connect(self._on_export_qc_video)
        exports_grid.addWidget(self._export_qc_video_btn, 2, 0)
        self._export_frame_btn = QPushButton("Save current frame (PNG)")
        self._export_frame_btn.clicked.connect(self._on_export_current_frame)
        exports_grid.addWidget(self._export_frame_btn, 2, 1)
        res_layout.addLayout(exports_grid)
        viewer_layout.addWidget(self._results_group)

        # The group above is hidden until a run loads, which left this tab a
        # blank rectangle -- the one panel in the app that said nothing at all
        # about why it was empty.
        self._results_empty = EmptyState(
            "No run loaded",
            "Start a run, or open a finished one from Browse, to see its ortho, "
            "benthic cover and exports here.",
        )
        viewer_layout.addWidget(self._results_empty, 1)
        viewer_layout.addStretch()

    def _build_models_panel(self, models_layout: QVBoxLayout) -> None:
        # Untitled: Setup's view switch already says Models above it,
        # and a frame whose caption repeats the control that reached it says
        # nothing twice.
        models_group = QGroupBox()
        self._models_layout = QVBoxLayout(models_group)

        auth_row = QHBoxLayout()
        self._hf_auth_label = QLabel("Checking Hugging Face login...")
        self._hf_auth_label.setWordWrap(True)
        auth_row.addWidget(self._hf_auth_label, 1)

        self._hf_auth_icon = QLabel()
        self._hf_auth_icon.setFixedWidth(14)
        self._hf_auth_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth_row.addWidget(self._hf_auth_icon)
        auth_row.addSpacing(6)

        self._hf_auth_btn = QPushButton("Log in...")
        self._hf_auth_btn.setFixedWidth(110)
        self._hf_auth_btn.clicked.connect(self._on_hf_auth_button)
        auth_row.addWidget(self._hf_auth_btn)
        self._models_layout.addLayout(auth_row)

        self._models_layout.addWidget(_separator())

        # Model library: reveal the cache folder, and export/import portable model
        # packs so offline field laptops can be provisioned from a USB stick without
        # re-downloading. Logic lives in models/packs.py + models/packs_ui.py.
        lib_row = QHBoxLayout()
        lib_row.setContentsMargins(0, 0, 0, 0)
        lib_row.setSpacing(6)
        open_lib_btn = QPushButton("Open model folder")
        open_lib_btn.setToolTip("Open the folder where downloaded models are stored")
        open_lib_btn.clicked.connect(self._open_model_library)
        lib_row.addWidget(open_lib_btn)
        self._export_models_btn = QPushButton("Export…")
        self._export_models_btn.setToolTip(
            "Copy downloaded models to a folder or USB drive as a portable pack"
        )
        self._export_models_btn.clicked.connect(self._on_export_models)
        lib_row.addWidget(self._export_models_btn)
        self._import_pack_btn = QPushButton("Import…")
        self._import_pack_btn.setToolTip(
            "Install models from a pack folder or USB drive (no internet needed)"
        )
        self._import_pack_btn.clicked.connect(self._on_import_model_pack)
        lib_row.addWidget(self._import_pack_btn)
        self._models_layout.addLayout(lib_row)

        self._models_layout.addWidget(_separator())

        self._models_grid_host = QWidget()
        self._models_grid = QGridLayout(self._models_grid_host)
        self._models_grid.setContentsMargins(0, 4, 0, 0)
        self._models_grid.setHorizontalSpacing(10)
        self._models_grid.setVerticalSpacing(4)
        self._models_grid.setColumnStretch(0, 1)
        self._models_grid.setColumnStretch(1, 0)
        self._models_layout.addWidget(self._models_grid_host)

        self._discover_btn = QPushButton("Check Hugging Face for new models")
        self._discover_btn.setToolTip(
            "Query the EPFL-ECEO organisation on Hugging Face for newly published "
            "models. Requires an internet connection."
        )
        self._discover_btn.clicked.connect(self._on_discover_clicked)
        self._models_layout.addWidget(self._discover_btn)

        self._hf_auth_user: str | None = None
        self._model_rows: dict[str, QWidget] = {}
        self._model_actions: dict[str, QWidget] = {}
        self._downloading: set[str] = set()
        self._download_cancel_requested: set[str] = set()
        self._delete_armed: dict[str, QPushButton] = {}
        self._last_model_states: list = []
        self._download_errors: dict[str, str] = {}
        self._pack_progress_dialog = None

        self._seg_combo.currentTextChanged.connect(self._on_required_models_changed)
        self._seg_combo.currentTextChanged.connect(self._on_seg_model_changed)
        self._map_combo.currentTextChanged.connect(self._on_required_models_changed)
        self._map_combo.currentTextChanged.connect(self._on_mapping_backend_changed)
        self._skip_seg_check.toggled.connect(self._on_required_models_changed)
        self._out_root_input.textChanged.connect(self._on_output_root_changed)

        # Editing the output root is debounced because the expensive half of
        # reacting to it walks the tree and opens a SurveyStore, and SurveyStore
        # creates its database under whatever path it is handed. Run per
        # keystroke, that leaves a survey.db under every prefix of what the user
        # typed on the way to the path they meant.
        self._out_root_commit_timer = QTimer(self)
        self._out_root_commit_timer.setSingleShot(True)
        self._out_root_commit_timer.setInterval(300)
        self._out_root_commit_timer.timeout.connect(self._commit_output_root)

        # Watch the output root directory so Browse reflects new manifests
        # appearing on disk (e.g. a sibling process completes a run) in addition
        # to user edits of the path text.
        self._out_root_watcher = QFileSystemWatcher(self)
        self._out_root_watcher.directoryChanged.connect(self._on_out_root_dir_changed)

        self._active_run_dir: Path | None = None
        self._active_run_manifest: dict | None = None
        self._load_cancelled = False

        self._base_ortho_grid = None
        self._ortho_cloud = None
        self._ortho_classes_config = None
        self._current_ortho_grid = None
        self._results_output_dir: Path | None = None
        self._ortho_crop_refresh_pending = False

        self._settings = QSettings("ECEO", "deepreefmap")
        saved_root = cast(str, self._settings.value("output_root_dir", "", type=str))
        if saved_root:
            self._out_root_input.setText(saved_root)
        self._refresh_data_manager()
        self._update_out_root_watch()

        # The inline status icons next to the seg/mapping dropdowns surface
        # state without forcing the user to leave the form for common cases.
        #
        # The group is the only child of the tab, which is what lets
        # _host_machine_panels lend it to Setup and take it back: the tab
        # top-aligns its contents already, so there is no trailing stretch for a
        # returning widget to land underneath.
        self._models_group = models_group
        self._models_page = models_group
        models_layout.addWidget(models_group)
        # Queued rather than started here: the first status refresh imports
        # huggingface_hub, httpx and ssl and then makes a request, and a thread
        # holds the GIL through an import as surely as this one would. Off the
        # window's own build, it costs the window nothing. Receiver-bound so a
        # window closed before it fires takes the callback with it.
        QTimer.singleShot(
            0, self, lambda: threading.Thread(target=self._refresh_model_status, daemon=True).start()
        )

    def _build_updates_section(self, updates_layout: QVBoxLayout) -> None:
        self._update_version_label = QLabel(f"Version: <b>{current_version()}</b>")
        self._update_version_label.setWordWrap(True)
        updates_layout.addWidget(self._update_version_label)
        self._update_status_label = QLabel("Checking for updates…")
        self._update_status_label.setWordWrap(True)
        self._update_status_label.setStyleSheet(f"color: {TEXT_MUTED};")
        updates_layout.addWidget(self._update_status_label)
        update_row = QHBoxLayout()
        self._update_version_combo = QComboBox()
        self._update_version_combo.setVisible(False)
        update_row.addWidget(self._update_version_combo, 1)
        self._update_btn = QPushButton("Install")
        self._update_btn.setVisible(False)
        self._update_btn.clicked.connect(self._on_update)
        update_row.addWidget(self._update_btn)
        updates_layout.addLayout(update_row)
        self._update_show_all = QCheckBox("Show older versions (rollback)")
        self._update_show_all.setToolTip(
            "An older version may not be able to open surveys this one has "
            "already saved. A backup is taken before rolling back."
        )
        self._update_show_all.setVisible(False)
        self._update_show_all.toggled.connect(self._on_toggle_show_all_versions)
        updates_layout.addWidget(self._update_show_all)
        self._available_releases: list[dict] = []
        self._current_version_str = current_version()

        # Applications-menu integration is a Setup readiness row rather than a
        # button here: whether the app is in the menu is a fact about this
        # machine, alongside its graphics card and its disk, and a row can say
        # "installed by the installer" where a button could only offer to
        # remove someone else's shortcut.

        self._build_env_section(updates_layout)

        threading.Thread(target=self._check_for_update, daemon=True).start()

        updates_layout.addStretch()

    def _build_env_section(self, layout: QVBoxLayout) -> None:
        """Per-version environments and the model cache total, under Setup's
        Updates view: what an install or a rollback leaves behind belongs beside
        the control that puts it there.

        Nothing to say outside an installed binary, so a dev checkout gets no
        section at all. Environments are never pruned automatically; this is the
        only thing that deletes one.
        """
        if pyapp_binary_path() is None:
            return
        layout.addWidget(QLabel("<b>Installed versions</b>"))
        caption = QLabel(
            "Each version keeps its own environment. Sizes are what deleting frees; "
            "most of an environment is shared with the package cache."
        )
        caption.setWordWrap(True)
        caption.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(caption)

        self._env_list_container = QWidget()
        self._env_list_layout = QVBoxLayout(self._env_list_container)
        self._env_list_layout.setContentsMargins(0, 0, 0, 0)
        self._env_list_layout.addWidget(QLabel("Measuring environments…"))
        layout.addWidget(self._env_list_container)

        self._model_cache_label = QLabel("Measuring downloaded models…")
        self._model_cache_label.setWordWrap(True)
        self._model_cache_label.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._model_cache_label)
        self._manage_models_btn = QPushButton("Manage models")
        self._manage_models_btn.clicked.connect(
            lambda: self._set_machine_view("models")
        )
        layout.addWidget(self._manage_models_btn)

        self._refresh_envs()

    def _build_bottom_bar(self) -> QWidget:
        """Full-width status and progress strip, the way a desktop app reports work.

        The progress bar spans the window above a row of status text, remaining
        estimate, and the transport controls at the right.
        """
        bar = QWidget()
        self._bottom_bar = bar
        bar.setObjectName("bottomBar")
        bar.setStyleSheet(
            f"QWidget {{ background-color: {CARD_BG}; }}"
            f" QWidget#bottomBar {{ border-top: 1px solid {BORDER}; }}"
        )
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._bottom_progress_bar = QProgressBar()
        self._bottom_progress_bar.setRange(0, 100)
        self._bottom_progress_bar.setValue(0)
        self._bottom_progress_bar.setTextVisible(False)
        self._bottom_progress_bar.setFixedHeight(BAR_HEIGHT)
        self._bottom_progress_bar.setStyleSheet(bar_qss(PRIMARY))
        self._bottom_progress_bar.setVisible(False)
        outer.addWidget(self._bottom_progress_bar)

        row = QHBoxLayout()
        row.setContentsMargins(PAGE_MARGIN, 4, PAGE_MARGIN, 4)
        row.setSpacing(GUTTER)
        self._status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        row.addWidget(self._status_label, 1)
        # The graphics card is counted after the window is up, so the strip says
        # so while it happens rather than leaving the readiness row to look
        # simply wrong for the first few seconds. Hidden once it has landed well.
        self._gpu_indicator_box = QWidget()
        gpu_row = QHBoxLayout(self._gpu_indicator_box)
        gpu_row.setContentsMargins(0, 0, 0, 0)
        gpu_row.setSpacing(SPACE_XS)
        self._gpu_spinner = BusySpinner()
        gpu_row.addWidget(self._gpu_spinner)
        self._gpu_indicator = QLabel("")
        self._gpu_indicator.setTextFormat(Qt.TextFormat.RichText)
        gpu_row.addWidget(self._gpu_indicator)
        self._gpu_indicator_box.setVisible(False)
        row.addWidget(self._gpu_indicator_box)
        # The registry link sits beside the drives: the two facts a diver
        # checks before packing up are "did it fit" and "is it on the server".
        self._sync_badge = SyncBadge()
        self._sync_badge.clicked.connect(self._on_sync_badge_clicked)
        row.addWidget(self._sync_badge)
        # Storage rides on this row rather than a band of its own: the bars are
        # BAR_HEIGHT tall inside a row already twice that, so the one number
        # that can stop a run mid-dive costs no height to keep on screen.
        self._storage_bars = StorageBars()
        # Pressing a drive asks the window to go to its page; the window decides
        # where that lands and says afterwards which button is lit.
        self._storage_bars.volume_clicked.connect(self._open_storage_page)
        row.addWidget(self._storage_bars)
        self._eta_total_label.setVisible(False)
        row.addWidget(self._eta_total_label)
        # No prospective command here. Runs are launched as a batch of passes
        # off the survey plan, so a command naming one video and one output
        # directory describes a run nobody is about to start. Browse keeps the
        # retrospective one, built from each run's own manifest.
        row.addWidget(self._pause_btn)
        row.addWidget(self._spinner_stop)
        outer.addLayout(row)

        # Free space moves without the app touching anything, so the bars are
        # polled rather than only refreshed on the events that grow them.
        self._storage_timer = QTimer(self)
        self._storage_timer.setInterval(_STORAGE_REFRESH_MS)
        self._storage_timer.timeout.connect(self._refresh_storage_bars)
        # The badge shares the cadence: its read is one COUNT per section.
        self._storage_timer.timeout.connect(self._refresh_sync_badge)
        self._storage_timer.start()
        self._refresh_storage_bars()
        self._refresh_sync_badge()
        return bar

    def _paint_gpu_indicator(self, gpu: object) -> None:
        """Say where the graphics-card probe has got to, in the bottom strip.

        `gpu` is None while it runs. A card that turns up is reported and then
        got out of the way: Setup's readiness row is where it lives permanently,
        and a strip that keeps a permanent tick teaches the diver to ignore it.
        Nothing found stays up, because that is the state worth acting on.
        """
        from deepreefmap_gui.profiling.system_probe import GPU_NONE, GpuInfo

        box = getattr(self, "_gpu_indicator_box", None)
        if box is None:
            return
        label, spinner = self._gpu_indicator, self._gpu_spinner
        if gpu is None:
            spinner.setVisible(True)
            label.setText(f"<span style='color:{TEXT_MUTED}'>Checking graphics card…</span>")
            box.setToolTip("Loading the GPU libraries and counting the cards on this machine.")
            box.setVisible(True)
            return
        assert isinstance(gpu, GpuInfo)
        if not gpu.resolved:
            box.setVisible(False)
            return
        spinner.setVisible(False)
        if gpu.kind == GPU_NONE:
            label.setText(f"<span style='color:{ERROR}'>✕ No graphics card</span>")
            box.setToolTip(f"{gpu.name}. Processing will run on the CPU, which is far slower.")
            box.setVisible(True)
            return
        label.setText(f"<span style='color:{SUCCESS}'>✓ {gpu.name}</span>")
        box.setToolTip("Graphics card ready.")
        box.setVisible(True)
        # box as the timer's receiver, not just a capture: the window can be
        # closed inside those six seconds, and Qt then drops the callback with
        # the widget instead of firing it at a deleted C++ object.
        QTimer.singleShot(_GPU_INDICATOR_LINGER_MS, box, lambda: box.setVisible(False))

    def _refresh_storage_bars(self) -> None:
        """Re-measure the drives this survey uses, off the thread painting them.

        ``disk_usage`` blocks for seconds on a mount that has gone away, which in
        the field is a card reader somebody unplugged, so the snapshot is taken
        here and the measuring happens on a worker.
        """
        if getattr(self, "_storage_scan_running", False):
            return
        bars = getattr(self, "_storage_bars", None)
        if bars is None:
            return

        videos: list[tuple[str, int | None]] = []
        store = self._try_survey_store()
        if store is not None:
            try:
                videos = [(v.path, v.size_bytes) for v in store.list_videos()]
            except Exception:
                logger.exception("Could not read the clip library for the storage bars")
        field = getattr(self, "_out_root_input", None)
        if field is None:
            return
        root = Path(field.text()).expanduser()
        # The output root goes in at zero bytes so its drive is listed even
        # before anything has been written to it: it is the drive whose free
        # space decides whether the next run can start.
        outputs: list[tuple[str, int | None]] = [(str(root), 0)]
        outputs += [
            (str(root / name), size) for name, size in getattr(self, "_run_size_cache", {}).items()
        ]

        self._storage_scan_running = True
        threading.Thread(
            target=self._scan_storage, args=(videos, outputs), name="storage-scan", daemon=True
        ).start()

    def _scan_storage(
        self, videos: list[tuple[str, int | None]], outputs: list[tuple[str, int | None]]
    ) -> None:
        try:
            # Only clips that are actually there: a file on an unplugged drive
            # takes up none of the space being reported.
            present = [(path, size) for path, size in videos if Path(path).is_file()]
            volumes = group_by_volume(present, outputs)
        except Exception:
            logger.exception("Could not measure the survey's drives")
            volumes = []
        try:
            self._sig_storage_usage.emit(volumes)
        except (RuntimeError, TypeError):
            # A scan outlives a window closed while a mount was answering slowly,
            # and by then there is no C++ object left to deliver to.
            logger.debug("The window closed before its drives were measured")

    def _apply_storage_usage(self, volumes: object) -> None:
        self._storage_scan_running = False
        bars = getattr(self, "_storage_bars", None)
        if bars is not None and isinstance(volumes, list):
            bars.set_volumes(volumes)

    def _build_log_panel(self) -> QWidget:
        """Bottom-of-window panel hosting the live log."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(QLabel("<b>Live log</b>"))
        header_row.addStretch(1)
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("Hide log panel")
        close_btn.setProperty("pad", "none")
        close_btn.setStyleSheet(
            f"QPushButton {{ font-size: {FONT_XL}; font-weight: {WEIGHT_BOLD}; padding: 0;"
            f" border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;"
            f" background: {BUTTON}; }}"
            f" QPushButton:hover {{ background: {SURFACE_HI}; }}"
        )
        close_btn.clicked.connect(lambda: self._set_log_panel_visible(False))
        header_row.addWidget(close_btn)
        layout.addLayout(header_row)
        layout.addWidget(self._log_view, 1)
        panel.setVisible(False)
        self._log_panel = panel
        return panel

    def _set_log_panel_visible(self, visible: bool) -> None:
        if not hasattr(self, "_log_panel"):
            return
        self._log_panel.setVisible(visible)
        # Keep the top-bar toggle in sync without re-triggering this slot.
        if self._log_toggle_btn.isChecked() != visible:
            self._log_toggle_btn.blockSignals(True)
            self._log_toggle_btn.setChecked(visible)
            self._log_toggle_btn.blockSignals(False)
        # When the user re-opens the panel after collapsing the splitter all
        # the way down, give it a reasonable default height again.
        if visible and hasattr(self, "_central_vsplitter"):
            sizes = self._central_vsplitter.sizes()
            if len(sizes) == 2 and sizes[1] < 40:
                total = sum(sizes) or 800
                bottom = max(200, total // 4)
                self._central_vsplitter.setSizes([total - bottom, bottom])
    def _on_advanced_toggled(self, checked: bool) -> None:
        self._advanced_panel.setVisible(checked)

    def _on_mapping_backend_changed(self, _value: object = "") -> None:
        backend = self._map_combo.currentText()
        self._loger_panel.setVisible(backend in ("loger", "loger_star"))
        self._scs_panel.setVisible(backend == "scsfmlearner")
        # The backend is the largest single term in the memory model, and the
        # readout offers switching it as the fix. Leaving the grade stale meant
        # taking that advice appeared to change nothing.
        self._on_processing_settings_changed()

    def _collect_run_settings(self) -> dict:
        """Every run_reconstruction kwarg the form controls, minus the per-run
        inputs (video, output dir, run name, trim, transect length).

        Every pass in a survey batch builds its run from this, so a setting
        edited in the dialog reaches all of them or none.
        """
        settings: dict = {
            "fps": self._fps_spin.value(),
            "segmentation_name": self._seg_combo.currentText(),
            "mapping_name": self._map_combo.currentText(),
            "camera_profile_name": self._profile_combo.currentText(),
            "transect_crop_width": self._crop_width.value() or None,
            "enable_tsdf": self._tsdf_check.isChecked(),
            "skip_segmentation": self._skip_seg_check.isChecked(),
            "classes_path": self._classes_path,
            "processing_width": self._proc_width_spin.value(),
            "processing_height": self._proc_height_spin.value(),
            "preprocess_batch_size": self._batch_size_spin.value(),
            "grid_bins": self._grid_bins_spin.value(),
            "require_gravity_telemetry": self._require_gravity_check.isChecked(),
            "replacement_radius_factor": self._rr_factor_spin.value() or None,
            "replacement_radius_estimation_frames": self._rr_est_frames_spin.value(),
            "replacement_radius_override": self._rr_override_spin.value() or None,
        }
        mapping_name = str(settings["mapping_name"])
        loger_options = self._collect_loger_options(mapping_name)
        if loger_options is not None:
            settings["mapping_options"] = loger_options
            settings["refine_intrinsics_from_mapper"] = self._refine_intrinsics_check.isChecked()
        elif mapping_name == "scsfmlearner":
            scs_opts: dict[str, object] = {
                "target_width": self._scs_width_spin.value(),
                "target_height": self._scs_height_spin.value(),
            }
            scs_ckpt = self._scs_checkpoint_input.text().strip()
            if scs_ckpt:
                scs_opts["checkpoint_path"] = scs_ckpt
            settings["mapping_options"] = scs_opts
        return settings

    def _collect_loger_options(self, mapping_name: str) -> dict | None:
        """Build the LoGeR mapping_options dict from the form, or None for other backends."""
        if mapping_name not in ("loger", "loger_star"):
            return None
        model_path = self._loger_model_path_input.text().strip()
        return {
            "window_size": self._loger_window_spin.value(),
            "overlap_size": self._loger_overlap_spin.value(),
            "model_path": model_path or None,
        }

    def _browse_loger_checkpoint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select LoGeR checkpoint", "", "Checkpoints (*.pt *.pth);;All files (*)"
        )
        if path:
            self._loger_model_path_input.setText(path)

    def _browse_scs_checkpoint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SCSfMLearner checkpoint", "", "Checkpoints (*.pt *.pth);;All files (*)"
        )
        if path:
            self._scs_checkpoint_input.setText(path)

    def _on_seg_model_changed(self, name: str) -> None:
        from deepreefmap_gui.models.families import model_processing_size

        self._native_resolution = model_processing_size(name) or (1376, 768)
        self._is_dpt_model = "dpt" in name
        preset = self._resolution_preset_combo.currentText()
        if preset != "Custom":
            self._apply_resolution_preset(preset)
        self._update_dpt_warning()
        self._on_processing_settings_changed()

    def _on_resolution_preset_changed(self, preset: str) -> None:
        is_custom = preset == "Custom"
        self._proc_width_spin.setEnabled(is_custom)
        self._proc_height_spin.setEnabled(is_custom)
        if not is_custom:
            self._apply_resolution_preset(preset)
        self._update_dpt_warning()
        self._on_processing_settings_changed()

    def _apply_resolution_preset(self, preset: str) -> None:
        nw, nh = self._native_resolution
        divisors = {"Native": 1, "Half": 2, "Quarter": 4}
        d = divisors.get(preset, 1)
        self._proc_width_spin.blockSignals(True)
        self._proc_height_spin.blockSignals(True)
        self._proc_width_spin.setValue(nw // d)
        self._proc_height_spin.setValue(nh // d)
        self._proc_width_spin.blockSignals(False)
        self._proc_height_spin.blockSignals(False)

    def _update_dpt_warning(self) -> None:
        nw, nh = self._native_resolution
        current_w = self._proc_width_spin.value()
        current_h = self._proc_height_spin.value()
        show = self._is_dpt_model and (current_w != nw or current_h != nh)
        self._dpt_resolution_warning.setVisible(show)

    def _on_processing_settings_changed(self) -> None:
        """Re-grade, and repaint everything that reads the grade.

        One memory model answers for both resources, so the batch-size hint comes
        from the same grade as the capacity readout.
        """
        self._update_dpt_warning()
        self._update_memory_profile_warning()

    def _current_fit(self):
        """Grade the longest queued pass against this machine, or None.

        None when nothing is queued or the probe fails; callers fall back to
        reporting the machine's capacity without a run to compare it to.
        """
        from deepreefmap_gui.profiling.memory_estimate import fit_for_pass
        from deepreefmap_gui.profiling.run_history import history_key, load_expected_peaks
        from deepreefmap_gui.profiling.system_probe import probe_system

        seconds = self._simple_peak_seconds()
        if not seconds:
            return None
        fps = self._fps_spin.value()
        w, h = self._proc_width_spin.value(), self._proc_height_spin.value()
        mapping = self._map_combo.currentText()
        seg = self._seg_combo.currentText()
        batch_size = self._batch_size_spin.value()
        profile = probe_system(wait_for_gpu=False)
        return fit_for_pass(
            profile,
            seconds=seconds,
            fps=fps,
            width=w,
            height=h,
            mapping_backend=mapping,
            seg_model=seg,
            batch_size=batch_size,
            # The card and the batch size qualify the VRAM half of the history:
            # a peak from another machine's GPU, or from a different batch size,
            # would move a fixed term it says nothing about.
            recorded=load_expected_peaks(
                history_key(mapping, seg, w, h, fps),
                gpu_name=profile.gpu.name,
                batch_size=batch_size,
            ),
        )

    def _update_memory_profile_warning(self) -> None:
        """Refresh the capacity readout and the advisory Setup reads from it."""
        # Advisory only: a warn or block grade never gates the run.
        if not getattr(self, "_capacity_rows", None):  # form not built yet
            return
        try:
            fit = self._current_fit()
        except Exception:
            fit = None
        self._paint_capacity_readout(fit)
        self._memory_advisory = self._memory_advisory_text(fit)
        self._refresh_machine_button()
        self._refresh_readiness_view()

    def _memory_advisory_text(self, fit) -> str:
        """What Setup says about memory: how many passes, not which one.

        Each cart row carries its own grade on its settings button, where the
        frame rate or the resolution that caused it is a click away. What is
        left for Setup is the count, which is what tells you whether to go and
        look at all.
        """
        if fit is None or fit.fits:
            return ""
        try:
            over = self._rows_over_memory()
        except Exception:
            over = 0
        if over > 1:
            return f"{over} passes may need more than this machine's memory."
        return fit.headline

    def _paint_capacity_readout(self, fit) -> None:
        """Show the longest pass against what this machine can give one run.

        Both pools are drawn every time, each with its own verdict under it, so
        the row that refuses the run is the row the reader is looking at.
        """
        from deepreefmap_gui.profiling.memory_estimate import ResourceFit, format_duration

        if fit is None:
            self._capacity_caption.setText("Memory needed")
            for key, label in (("ram", "Memory"), ("vram", "Graphics memory")):
                self._capacity_rows[key].set_resource(
                    ResourceFit(
                        key=key,
                        label=label,
                        level="none",
                        message="Nothing queued to size yet.",
                    ),
                    held_colour=SURFACE_HI,
                )
            self._capacity_detail.setText("Add a pass to see what it would need.")
            self._capacity_advice.setVisible(False)
            self._vram_auto_label.setVisible(False)
            return

        verdict = fit.verdict
        # Says what was modelled and from what. "Longest pass" alone read as a
        # statistic about the batch rather than as the input to these figures.
        self._capacity_caption.setText(
            f"Modelled on a pass of <b>{format_duration(fit.seconds)}</b> at "
            f"<b>{fit.fps} FPS</b>, the longest queued."
        )
        for resource in verdict.resources:
            self._capacity_rows[resource.key].set_resource(
                resource, held_colour=SURFACE_HI
            )
        # The ceiling is a property of the machine and the settings, not of the
        # queued pass: it is the length at which the tightest stage would fill
        # the pool. A fixed cost has no such length -- it does not fit at one
        # frame -- so quoting one there would be a number that cannot be acted on.
        self._capacity_detail.setText(
            ""
            if verdict.limit_is_fixed
            else (
                f"At <b>{fit.fps} FPS</b> these settings can take a pass of up to "
                f"about <b>{format_duration(fit.max_seconds)}</b> on this machine."
            )
        )
        self._paint_batch_size_hint(fit)
        if fit.fits:
            self._capacity_advice.setVisible(False)
            return
        colour = {"ok": SUCCESS, "warn": UPDATE, "block": BLOCK}[fit.level]
        self._capacity_advice.setStyleSheet(f"color: {colour}; font-size: {FONT_SM};")
        self._capacity_advice.setText(
            f"<b>{fit.headline}.</b> {fit.advice} "
            f'<a href="#system" style="color:{colour};">Setup</a>'
        )
        self._capacity_advice.setVisible(True)

    def _paint_batch_size_hint(self, fit) -> None:
        """Say what the batch size would have to be, when it is what decides.

        Beside the control it names, in Advanced. Silent otherwise: the batch
        size reaches the card only through the segmentation term, so on a run
        the mapping backend already dominates it changes nothing.
        """
        if fit.suggested_batch_size is None:
            self._vram_auto_label.setVisible(False)
            return
        w, h = self._proc_width_spin.value(), self._proc_height_spin.value()
        self._vram_auto_label.setText(
            f"Batch size {self._batch_size_spin.value()} may not fit this card "
            f"at {w}×{h}. {fit.suggested_batch_size} should."
        )
        self._vram_auto_label.setVisible(True)

    def _update_gated_warning(self) -> None:
        seg_name = self._seg_combo.currentText()
        if self._skip_seg_check.isChecked():
            self._gated_warning.setVisible(False)
            return
        from deepreefmap_gui.models.cache import DPT_BACKBONE_MAP

        backbone_name = DPT_BACKBONE_MAP.get(seg_name)
        if not backbone_name:
            self._gated_warning.setVisible(False)
            return
        states = {info.name: (info, cached) for info, cached in self._last_model_states}
        missing_repos: list[str] = []
        for name in (seg_name, backbone_name):
            entry = states.get(name)
            if entry and not entry[1]:
                missing_repos.extend(entry[0].hf_repos)
        if not missing_repos:
            self._gated_warning.setVisible(False)
            return
        links = " ".join(
            f'<a href="https://huggingface.co/{repo}" style="color:{WARN_TEXT}">{repo}</a>'
            for repo in missing_repos
        )
        logged_in = self._hf_auth_user is not None
        can_gated = getattr(self, "_can_read_gated", True)
        if logged_in and not can_gated:
            msg = (
                f"<b>{seg_name}</b> needs gated repos, but your token does not have "
                f"permission to download them. Edit your token at "
                f'<a href="https://huggingface.co/settings/tokens" style="color:{WARN_TEXT}">'
                f"huggingface.co/settings/tokens</a> and enable "
                f"<i>Read access to contents of all public gated repos</i>."
            )
        elif logged_in:
            msg = (
                f"<b>{seg_name}</b> uses gated repos that must be downloaded first. "
                f"Accept the license on each repo page and download under "
                f"Models: {links}"
            )
        else:
            msg = (
                f"<b>{seg_name}</b> requires Hugging Face login. "
                f"Log in under Models, then accept each repo's license "
                f"and download: {links}"
            )
        self._gated_warning.setText(msg)
        self._gated_warning.setVisible(True)

    def _reset_advanced_defaults(self) -> None:
        self._resolution_preset_combo.setCurrentText("Native")
        self._batch_size_spin.setValue(4)
        self._grid_bins_spin.setValue(2000)
        self._rr_factor_spin.setValue(0.0)
        self._rr_est_frames_spin.setValue(30)
        self._rr_override_spin.setValue(0.0)

    def _gpu_available(self) -> bool:
        from deepreefmap_gui.profiling.system_probe import gpu_present

        # Never blocking: this is read from repaints and from the cart gate, both
        # on the GUI thread. Until the probe lands it answers True, and
        # _on_gpu_probe_done re-runs everything that asked.
        return gpu_present(wait=False)

    def _gpu_only_mapper(self) -> str:
        """The chosen mapping method when it needs a card this machine lacks.

        The Run step's gate and the setup step's readiness row both read this,
        so a CPU-only laptop cannot be told it is fine by one and blocked by
        the other.
        """
        from deepreefmap_gui.models.cache import GPU_ONLY_BACKENDS

        mapping = self._map_combo.currentText()
        if mapping in GPU_ONLY_BACKENDS and not self._gpu_available():
            return mapping
        return ""

    def _open_output_dir(self) -> None:
        d = getattr(self, "_results_output_dir", None) or self._viewer._output_dir
        if d and Path(d).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))


    def _browse_output_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select output root directory", self._out_root_input.text()
        )
        if path:
            self._out_root_input.setText(path)

    def _open_output_root(self) -> None:
        root = Path(self._out_root_input.text()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _reset_output_root_to_default(self) -> None:
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default = str(Path(documents or str(Path.home())) / "DeepReefMap")
        self._out_root_input.setText(default)

    @staticmethod
    def _sanitize_run_name(name: str) -> str:
        import re
        from datetime import datetime

        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
        return cleaned.strip("._-") or datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 (local time is intended: this is a user-facing default name)

    def _on_output_root_changed(self, _text: str = "") -> None:
        self._out_root_commit_timer.start()

    def _commit_output_root(self) -> None:
        """Persist the root and rescan it, once the path has stopped changing."""
        self._settings.setValue("output_root_dir", self._out_root_input.text())
        self._refresh_data_manager()
        self._update_out_root_watch()

    def _on_out_root_dir_changed(self, _path: str = "") -> None:
        # Manifests from other processes appear unprompted; the Data refresh is
        # debounced because runs touch the root continuously while writing.
        self._request_data_refresh()

    def _update_out_root_watch(self) -> None:
        """Re-point the watcher so manifests from other processes appear unprompted."""
        if not hasattr(self, "_out_root_watcher"):
            return
        existing = self._out_root_watcher.directories()
        if existing:
            self._out_root_watcher.removePaths(existing)
        root = Path(self._out_root_input.text()).expanduser()
        if root.exists() and root.is_dir():
            self._out_root_watcher.addPath(str(root))
