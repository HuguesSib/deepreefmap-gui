"""App-mode, playback, legend, pick-overlay, and viewer status routing for the main window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, SupportsInt, cast

from PySide6.QtCore import QSettings

from deepreefmap_gui.core.theme import (
    BORDER,
    BRIGHT_TEXT,
    FONT_MD,
    FONT_XS,
    OVERLAY_ACCENT_FILL,
    OVERLAY_BG,
    OVERLAY_BORDER,
    OVERLAY_BORDER_STRONG,
    OVERLAY_FILL,
    OVERLAY_FILL_HI,
    OVERLAY_HANDLE,
    OVERLAY_TEXT,
    PRIMARY,
    RADIUS,
    RADIUS_SM,
    SPACE_XS,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WEIGHT_BOLD,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.profiling.eta import STAGE_MESSAGE_TO_PHASE as _STAGE_MESSAGE_TO_PHASE
from deepreefmap_gui.runs.progress import _SETUP_MESSAGE_TO_PHASE

if TYPE_CHECKING:
    from deepreefmap.config.classes import ClassConfig
    from deepreefmap.pipeline.artifacts import SemanticPointCloud
    from deepreefmap.pointcloud.grid_ortho import OrthoGrid
    from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

# Shared label column in the overlay, so the sliders line up under each other.
_OVERLAY_LABEL_W = 90

# Coarse stage names for the status line. Module level because _on_viewer_status
# runs on every progress callback, and this was being rebuilt each time.
_STAGE_LABELS = {
    "startup": "Startup",
    "preprocess": "Preprocessing",
    "mapping": "Mapping",
    "outputs": "Building outputs",
}


class ViewerControlsMixin(MixinBase):
    """DeepReefMapWindow methods for app mode, playback, legend, and viewer status routing."""

    def _set_app_mode(self, mode: str) -> None:
        """Switch app mode to SETUP / RUNNING / VIEWING."""
        if mode not in ("SETUP", "RUNNING", "VIEWING"):
            raise ValueError(f"Unknown app mode: {mode!r}")
        self._app_mode = mode
        # RUNNING shows the pass table so the batch has somewhere to report.
        # VIEWING deliberately does not move: a run is opened from the pass
        # table or from Browse, and both are places you want to stay.
        #
        # Guarded on the shell existing, because the first _set_app_mode("SETUP")
        # happens inside _build_form_widgets, before there is anything to show.
        if mode == "RUNNING" and hasattr(self, "_simple_stack"):
            self._set_simple_section("process")
        self._update_work_area()

    def _refresh_run_warnings_view(self) -> None:
        """Keep the setup-form warning mirror in sync with the Results-tab one."""
        text = self._warnings_label.text()
        visible = self._warnings_label.isVisible()
        self._warnings_label_running.setText(text)
        self._warnings_label_running.setVisible(visible)

    def _overlay_point_size(self) -> float:
        """The overlay slider holds tenths, because QSlider is integer-only."""
        return self._ov_pt_slider.value() / 10.0

    def _frustums_visible(self) -> bool:
        button = getattr(self, "_ov_frustum_btn", None)
        return button is not None and button.isChecked()

    def _following_camera(self) -> bool:
        button = getattr(self, "_ov_follow_btn", None)
        return button is not None and button.isChecked()

    def _on_viewer_control_changed(self) -> None:
        if not self._viewer.has_scene_data:
            return
        if self._viewer.is_geometry_mode:
            self._viewer.apply_geometry_state(
                timeline_t=self._frame_slider.value(),
                point_size=self._overlay_point_size(),
                frustums_visible=self._frustums_visible(),
            )
            if self._following_camera():
                self._snap_camera_to_current_frame()
            return
        self._viewer.apply_state(
            timeline_t=self._frame_slider.value(),
            accumulate=self._ov_acc_btn.isChecked(),
            enabled_classes=self._enabled_class_set(),
            semantic_colors=self._ov_sem_btn.isChecked(),
            point_size=self._overlay_point_size(),
            min_confidence=self._ov_conf_slider.value() / 100.0,
            frustums_visible=self._frustums_visible(),
        )
        if self._following_camera():
            self._snap_camera_to_current_frame()
        self._apply_legend_sort()
        self._update_master_check()
        self._update_sunburst_selection()

    def _enabled_class_set(self) -> frozenset[int]:
        return frozenset(int(cid) for cid, cb in self._legend_toggles.items() if cb.isChecked())

    def _playback_interval_ms(self) -> int:
        return max(16, int(1000 / max(1, self._ov_fps_spin.value())))

    def _on_play_toggled(self, playing: bool) -> None:
        if playing:
            self._playback_timer.start(self._playback_interval_ms())
        else:
            self._playback_timer.stop()

    def _on_play_fps_changed(self) -> None:
        if self._playback_timer.isActive():
            self._playback_timer.setInterval(self._playback_interval_ms())

    def _on_playback_tick(self) -> None:
        n = self._viewer.n_frames
        if n <= 0:
            return
        nxt = (self._frame_slider.value() + 1) % n
        self._frame_slider.setValue(nxt)

    def _connect_overlay_controls(self) -> None:
        """Wire the overlay's controls to the viewer, once.

        Each control is the only copy of its setting, so there is nothing to
        mirror: a change goes straight to the viewer.
        """
        if getattr(self, "_overlay_controls_connected", False):
            return
        self._overlay_controls_connected = True

        for widget in (self._ov_pt_slider, self._ov_conf_slider):
            widget.valueChanged.connect(lambda _: self._on_viewer_control_changed())
        for button in (self._ov_sem_btn, self._ov_acc_btn, self._ov_frustum_btn):
            button.toggled.connect(lambda _: self._on_viewer_control_changed())
        self._ov_play_btn.toggled.connect(self._on_play_toggled)
        self._ov_fps_spin.valueChanged.connect(lambda _: self._on_play_fps_changed())
        self._ov_follow_btn.toggled.connect(lambda _: self._on_follow_camera_changed())
        self._ov_backoff_slider.valueChanged.connect(lambda _: self._on_follow_camera_changed())
        self._ov_snap_btn.clicked.connect(self._on_view_from_camera)

    def _show_viewer_controls(self) -> None:
        n = self._viewer.n_frames
        self._frame_slider.setRange(0, max(0, n - 1))
        self._frame_slider.setValue(n - 1)
        self._set_semantic_only_controls_visible(True)
        self._connect_overlay_controls()
        self._set_overlay_controls_visible(True)

    def _set_overlay_controls_visible(self, visible: bool) -> None:
        """Show or hide the overlay's display controls with the loaded run.

        The Pick and Reset buttons above them stay: they steer the camera, which
        is worth doing over a live preview as much as over a finished cloud. A
        collapsed overlay keeps the display controls away whatever the run is
        doing.
        """
        self._overlay_controls_run_ready = visible
        shown = visible and not self._overlay_controls_collapsed
        overlay_ctrl = getattr(self, "_overlay_controls_container", None)
        if overlay_ctrl is not None:
            overlay_ctrl.setVisible(shown)
        ctrl_sep = getattr(self, "_overlay_ctrl_sep", None)
        if ctrl_sep is not None:
            ctrl_sep.setVisible(shown)
        hint_row = getattr(self, "_overlay_hint_row", None)
        if hint_row is not None:
            hint_row.setVisible(not self._overlay_controls_collapsed)
        overlay = getattr(self, "_pick_mode_overlay", None)
        if overlay is not None:
            overlay.adjustSize()
            self._reposition_pick_mode_overlay()

    def _toggle_overlay_controls_collapsed(self) -> None:
        """Fold the display controls away, leaving the Pick / Reset strip."""
        self._overlay_controls_collapsed = not self._overlay_controls_collapsed
        self._viewer_settings().setValue(
            "viewer_controls_collapsed", self._overlay_controls_collapsed
        )
        self._apply_overlay_collapse_state()

    def _apply_overlay_collapse_state(self) -> None:
        """Match the chevron and the folded rows to the collapsed flag."""
        from deepreefmap_gui.core.icons import chevron_down_icon, chevron_right_icon

        button = getattr(self, "_overlay_collapse_btn", None)
        if button is not None:
            collapsed = self._overlay_controls_collapsed
            button.setIcon(chevron_right_icon() if collapsed else chevron_down_icon())
            button.setToolTip(
                "Show the display controls (D)" if collapsed
                else "Hide the display controls (D)"
            )
        self._set_overlay_controls_visible(self._overlay_controls_run_ready)

    def _on_legend_collapsed_changed(self, collapsed: bool) -> None:
        self._viewer_settings().setValue("viewer_legend_collapsed", bool(collapsed))

    def _viewer_settings(self) -> QSettings:
        """The window's settings, or a handle of its own.

        The canvas overlays are built before the run form, which is what owns
        ``_settings``.
        """
        settings = getattr(self, "_settings", None)
        return settings if settings is not None else QSettings("ECEO", "deepreefmap")

    def _set_semantic_only_controls_visible(self, visible: bool) -> None:
        """Hide per-class/semantic-only controls for geometry-only runs."""
        ov_sem = getattr(self, "_ov_sem_btn", None)
        if ov_sem is not None:
            ov_sem.setVisible(visible)
        ov_acc = getattr(self, "_ov_acc_btn", None)
        if ov_acc is not None:
            ov_acc.setVisible(visible)
        ov_conf = getattr(self, "_ov_conf_container", None)
        if ov_conf is not None:
            ov_conf.setVisible(visible)
        ov_toggle = getattr(self, "_ov_toggle_container", None)
        if ov_toggle is not None:
            ov_toggle.setVisible(visible)

    def _build_legend(self) -> None:
        cc = self._classes_config
        counts = self._viewer.class_point_counts()
        class_ids = sorted(cc.id_to_name.keys())
        self._legend_toggles = self._viewer.legend_overlay.rebuild(
            class_ids,
            cc.id_to_name,
            cc.id_to_color,
            self._on_viewer_control_changed,
            self._on_solo_class,
            class_counts=counts or None,
        )
        # Connect the sort headers / master checkbox once; rebuilds reuse them.
        overlay = self._viewer.legend_overlay
        if not self._legend_sort_connected:
            overlay.sort_clicked.connect(self._on_legend_sort_clicked)
            overlay.master_clicked.connect(self._on_master_clicked)
            self._legend_sort_connected = True
        overlay.set_sort_indicator(self._legend_sort_mode, self._legend_sort_ascending)
        self._legend_order_cache = None
        # Built hidden; _reveal_legend_overlay shows it once the sunburst cover
        # is ready too, so the list and chart appear together without a flash.
        self._apply_legend_sort()
        self._update_master_check()
        self._update_sunburst_selection()

    def _reveal_legend_overlay(self) -> None:
        """Show the legend overlay only once positioned, so it never flashes an unsettled layout."""
        overlay = getattr(self._viewer, "legend_overlay", None)
        if overlay is None or not self._legend_toggles:
            return
        overlay.reposition()
        overlay.setVisible(True)

    # Direction a column sorts in when first clicked: visible-on-top, A-Z,
    # largest-first respectively.
    _LEGEND_SORT_DEFAULT_ASC = {"selected": False, "name": True, "size": False}

    def _legend_sort_order(self) -> list[int]:
        cc = self._classes_config
        counts = self._viewer.class_point_counts()
        enabled = self._enabled_class_set()
        ids = list(self._legend_toggles.keys())

        def name(cid: int) -> str:
            return cc.id_to_name.get(cid, str(cid)).lower()

        mode = self._legend_sort_mode
        asc = self._legend_sort_ascending
        if mode == "name":
            ids.sort(key=name, reverse=not asc)
        elif mode == "size":
            sign = 1 if asc else -1
            ids.sort(key=lambda c: (sign * int(counts.get(c, 0)), name(c)))
        else:  # "selected": one group on top (A-Z), the other below (A-Z)
            ids.sort(key=lambda c: ((c in enabled) == asc, name(c)))
        return ids

    def _apply_legend_sort(self) -> None:
        """Re-order legend rows for the current sort + selection; no-op when unchanged."""
        overlay = getattr(self._viewer, "legend_overlay", None)
        if overlay is None or not self._legend_toggles:
            return
        order = self._legend_sort_order()
        if order == self._legend_order_cache:
            return
        self._legend_order_cache = order
        overlay.reorder(order)

    def _on_legend_sort_clicked(self, mode: str) -> None:
        # Re-clicking the active column flips direction; a new column adopts its
        # default direction.
        if mode == self._legend_sort_mode:
            self._legend_sort_ascending = not self._legend_sort_ascending
        else:
            self._legend_sort_mode = mode
            self._legend_sort_ascending = self._LEGEND_SORT_DEFAULT_ASC.get(mode, True)
        self._legend_order_cache = None
        self._viewer.legend_overlay.set_sort_indicator(
            self._legend_sort_mode, self._legend_sort_ascending
        )
        self._apply_legend_sort()

    def _on_isolate_class(self, cid: int) -> None:
        if not self._legend_toggles:
            return
        for other_cid, cb in self._legend_toggles.items():
            cb.blockSignals(True)
            cb.setChecked(other_cid == cid)
            cb.blockSignals(False)
        self._on_viewer_control_changed()

    def _on_show_all_classes(self) -> None:
        self._set_all_classes(True)

    def _on_deselect_all_classes(self) -> None:
        self._set_all_classes(False)

    def _set_all_classes(self, checked: bool) -> None:
        if not self._legend_toggles:
            return
        for cb in self._legend_toggles.values():
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        self._on_viewer_control_changed()

    def _on_master_clicked(self) -> None:
        # Clicking the header checkbox shows all unless everything is already
        # shown, in which case it hides all, so one control does both.
        present = frozenset(self._legend_toggles.keys())
        if self._enabled_class_set() == present and present:
            self._on_deselect_all_classes()
        else:
            self._on_show_all_classes()

    def _update_master_check(self) -> None:
        from PySide6.QtCore import Qt

        overlay = getattr(self._viewer, "legend_overlay", None)
        if overlay is None or not self._legend_toggles:
            return
        n = len(self._legend_toggles)
        k = len(self._enabled_class_set())
        if k == 0:
            state = Qt.CheckState.Unchecked
        elif k == n:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        overlay.set_master_check_state(state)

    def _update_sunburst_selection(self) -> None:
        """Mirror the current selection on the sunburst (dim unselected slices)."""
        sunburst = getattr(self, "_cover_sunburst", None)
        if sunburst is None or not self._legend_toggles:
            return
        enabled = self._enabled_class_set()
        present = frozenset(self._legend_toggles.keys())
        sunburst.set_selection(enabled, enabled != present)

    def _on_solo_class(self, cid: int) -> None:
        if self._enabled_class_set() == frozenset({cid}):
            self._on_show_all_classes()
        else:
            self._on_isolate_class(cid)

    def _on_follow_camera_changed(self) -> None:
        if not self._following_camera():
            return
        self._snap_camera_to_current_frame()

    def _on_view_from_camera(self) -> None:
        self._snap_camera_to_current_frame()

    def _snap_camera_to_current_frame(self) -> None:
        if not hasattr(self, "_frame_slider"):
            return
        slider = getattr(self, "_ov_backoff_slider", None)
        backoff = slider.value() / 10.0 if slider is not None else 0.0
        self._viewer.view_from_frame_pose(int(self._frame_slider.value()), backoff_m=backoff)

    def _on_frustum_picked(self, frame_idx: int) -> None:
        if not hasattr(self, "_frame_slider"):
            return
        viewer = self._viewer
        if not hasattr(viewer, "_final_index") or viewer._final_index is None:
            return
        frame_order = viewer._final_index.frame_order
        for t, fid in enumerate(frame_order):
            if int(fid) == int(frame_idx):
                self._frame_slider.setValue(int(t))
                return

    def _on_sunburst_selection(self, class_ids: list) -> None:
        if not self._legend_toggles:
            return
        wanted = [int(c) for c in class_ids if int(c) in self._legend_toggles]
        if not wanted:
            return
        # Toggle the slice's class(es) against the current selection: if they're
        # all already shown, hide them (remove); otherwise show them (add). Lets
        # the user build a query slice by slice, or carve one away.
        enabled = self._enabled_class_set()
        turn_on = not all(cid in enabled for cid in wanted)
        for cid in wanted:
            cb = self._legend_toggles[cid]
            cb.blockSignals(True)
            cb.setChecked(turn_on)
            cb.blockSignals(False)
        self._on_viewer_control_changed()

    def _on_point_picked(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        canvas = self._viewer._canvas_container
        if self._pick_card is None:
            from deepreefmap_gui.viewer.pick_tooltip import PickCard

            self._pick_card = PickCard(canvas)
            self._pick_card.isolate_requested.connect(self._on_isolate_class)
            self._pick_card.show_all_requested.connect(self._on_show_all_classes)
            self._pick_card.goto_frame_requested.connect(self._on_goto_frame)
            self._pick_card.zoom_to_requested.connect(self._on_zoom_to_point)
            self._pick_card.close_requested.connect(self._dismiss_pick)
            self._pick_card.moved.connect(self._on_pick_card_moved)

        self._pick_card.set_payload(payload)
        self._last_pick_payload = dict(payload)
        self._pick_card_pinned_pos = None
        self._refresh_pick_marker()

    def _on_pick_card_moved(self, x: int, y: int) -> None:
        # User drag-relocated the card. Pin to the new spot so subsequent
        # camera/canvas refreshes keep it there, then refresh the leader
        # line so it follows the card to its new anchor target.
        self._pick_card_pinned_pos = (int(x), int(y))
        if self._last_pick_payload is not None:
            self._refresh_pick_marker()

    def _on_goto_frame(self, frame_idx: int) -> None:
        slider = getattr(self, "_frame_slider", None)
        if slider is not None and 0 <= frame_idx <= slider.maximum():
            slider.setValue(int(frame_idx))

    def _on_zoom_to_point(self, xyz: tuple) -> None:
        self._viewer.zoom_to_point((float(xyz[0]), float(xyz[1]), float(xyz[2])))

    def _on_point_picked_clear(self) -> None:
        self._dismiss_pick()

    def _dismiss_pick(self) -> None:
        if self._pick_card is not None:
            self._pick_card.hide()
        self._last_pick_payload = None
        self._pick_card_pinned_pos = None
        try:
            self._viewer.clear_picked_marker()
        except Exception:
            logger.debug("Failed to clear picked-point marker", exc_info=True)

    def _on_canvas_resized(self) -> None:
        self._reposition_pick_mode_overlay()
        if self._last_pick_payload is None or self._pick_card is None:
            return
        self._refresh_pick_marker()

    def _build_pick_mode_overlay(self) -> None:
        """Floating canvas overlay: Pick + Reset tool buttons with shortcuts."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        canvas = self._viewer._canvas_container
        overlay = QWidget(canvas)
        overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        overlay.setObjectName("pick_mode_overlay")
        overlay.setStyleSheet(
            f"""
            QWidget#pick_mode_overlay {{
                background-color: {OVERLAY_BG};
                border: 1px solid {OVERLAY_BORDER};
                border-radius: {RADIUS}px;
            }}
            QWidget#pick_mode_overlay QToolButton {{
                color: {OVERLAY_TEXT};
                background-color: {OVERLAY_FILL};
                border: 1px solid {OVERLAY_BORDER_STRONG};
                border-radius: {RADIUS_SM}px;
                font-size: {FONT_MD};
                font-weight: {WEIGHT_BOLD};
                padding: 6px 10px;
            }}
            QWidget#pick_mode_overlay QToolButton:hover {{
                background-color: {OVERLAY_FILL_HI};
            }}
            QWidget#pick_mode_overlay QToolButton:checked {{
                background-color: {OVERLAY_ACCENT_FILL};
                border: 1px solid {PRIMARY};
                color: {BRIGHT_TEXT};
            }}
            QWidget#pick_mode_overlay QToolButton#ov_secondary {{
                font-size: {FONT_XS};
                font-weight: normal;
                padding: 3px 8px;
                border-radius: {RADIUS_SM}px;
            }}
            QWidget#pick_mode_overlay QLabel#pick_mode_shortcut {{
                color: {TEXT_MUTED};
                font-size: {FONT_XS};
            }}
            QWidget#pick_mode_overlay QSlider::groove:horizontal {{
                height: 4px; background: {BORDER}; border-radius: 2px;
            }}
            QWidget#pick_mode_overlay QSlider::handle:horizontal {{
                background: {OVERLAY_HANDLE}; width: 10px; height: 10px;
                margin: -3px 0; border-radius: 5px;
            }}
            QWidget#pick_mode_overlay QSlider::sub-page:horizontal {{
                background: {PRIMARY}; border-radius: 2px;
            }}
            QWidget#pick_mode_overlay QSpinBox {{
                background: {OVERLAY_FILL}; color: {OVERLAY_TEXT};
                border: 1px solid {OVERLAY_BORDER}; border-radius: {RADIUS_SM}px;
                padding: 1px 2px; font-size: {FONT_XS};
            }}
            QWidget#pick_mode_overlay QCheckBox {{ color: {OVERLAY_TEXT}; font-size: {FONT_XS}; }}
            """
        )
        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(2)

        settings = self._viewer_settings()
        self._overlay_controls_collapsed = bool(
            settings.value("viewer_controls_collapsed", False, type=bool)
        )
        self._overlay_controls_run_ready = False
        btn, reset_btn = self._build_overlay_tool_buttons(overlay, layout)
        self._build_overlay_display_controls(overlay, layout)
        self._apply_overlay_collapse_state()

        legend = self._viewer.legend_overlay
        legend.set_collapsed(bool(settings.value("viewer_legend_collapsed", False, type=bool)))
        legend.collapsed_changed.connect(self._on_legend_collapsed_changed)

        # The timeline is the one display control not on the overlay: it is wide,
        # so the viewer keeps it under the canvas. This is where it joins the rest.
        self._frame_slider = self._viewer.frame_slider
        self._frame_slider.valueChanged.connect(self._on_viewer_control_changed)

        self._overlay_controls_connected = False

        self._pick_mode_overlay = overlay
        self._pick_mode_button = btn
        self._reset_view_button = reset_btn

        def _on_button_toggled(checked: bool) -> None:
            try:
                self._viewer.set_pick_mode(checked)
            except Exception:
                logger.debug("Failed to set pick mode on viewer", exc_info=True)

        def _on_viewer_pick_mode_changed(enabled: bool) -> None:
            if btn.isChecked() == enabled:
                return
            btn.blockSignals(True)
            btn.setChecked(enabled)
            btn.blockSignals(False)

        def _on_reset_clicked() -> None:
            try:
                if self._following_camera():
                    self._snap_camera_to_current_frame()
                else:
                    self._viewer.reset_view()
            except Exception:
                logger.debug("Failed to reset view", exc_info=True)

        btn.toggled.connect(_on_button_toggled)
        self._viewer.pick_mode_changed.connect(_on_viewer_pick_mode_changed)
        reset_btn.clicked.connect(_on_reset_clicked)

        QShortcut(QKeySequence("P"), self).activated.connect(btn.toggle)
        QShortcut(QKeySequence("R"), self).activated.connect(_on_reset_clicked)
        # Bare D and L beside the bare P and R: Ctrl+L is the log panel, so the
        # unmodified letter is free for the legend.
        QShortcut(QKeySequence("D"), self).activated.connect(
            self._toggle_overlay_controls_collapsed
        )
        QShortcut(QKeySequence("L"), self).activated.connect(
            self._viewer.legend_overlay.toggle_collapsed
        )
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(
            lambda: btn.setChecked(False) if btn.isChecked() else None
        )

        overlay.adjustSize()
        overlay.show()
        overlay.raise_()
        self._reposition_pick_mode_overlay()

    def _build_overlay_tool_buttons(
        self, overlay: QWidget, layout: QVBoxLayout
    ) -> tuple[QToolButton, QToolButton]:
        """Pick + Reset tool buttons with their keyboard-shortcut hint row."""
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(SPACE_XS)
        buttons_row.setContentsMargins(0, 0, 0, 0)

        from deepreefmap_gui.core.icons import (
            ICON_SM,
            crosshair_icon,
            refresh_icon,
        )

        btn = QToolButton(overlay)
        btn.setIcon(crosshair_icon(18))
        btn.setText("Pick")
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        btn.setCheckable(True)
        btn.setToolTip(
            "Enter pick mode. In pick mode, left-click a point to inspect it.\n"
            "P toggles, Esc exits."
        )
        buttons_row.addWidget(btn)

        reset_btn = QToolButton(overlay)
        reset_btn.setIcon(refresh_icon(18))
        reset_btn.setText("Reset View")
        reset_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        reset_btn.setToolTip(
            "Reset the 3D view to the default transect-lengthwise orientation.\n"
            "R triggers."
        )
        buttons_row.addWidget(reset_btn, 1)

        collapse_btn = QToolButton(overlay)
        collapse_btn.setObjectName("ov_secondary")
        collapse_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        collapse_btn.setFixedSize(ICON_SM + SPACE_XS, ICON_SM + SPACE_XS)
        collapse_btn.setAccessibleName("Collapse the display controls")
        collapse_btn.clicked.connect(self._toggle_overlay_controls_collapsed)
        buttons_row.addWidget(collapse_btn, 0)
        self._overlay_collapse_btn = collapse_btn

        layout.addLayout(buttons_row)

        # A widget rather than a bare layout: collapsing folds the hints away
        # with the controls they belong to, and only a widget can be hidden.
        hints_row = QWidget(overlay)
        hints_layout = QHBoxLayout(hints_row)
        hints_layout.setSpacing(SPACE_XS)
        hints_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("P  ·  Esc", overlay)
        hint.setObjectName("pick_mode_shortcut")
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        hints_layout.addWidget(hint, 1)
        reset_hint = QLabel("R", overlay)
        reset_hint.setObjectName("pick_mode_shortcut")
        reset_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        hints_layout.addWidget(reset_hint, 1)
        layout.addWidget(hints_row)
        self._overlay_hint_row = hints_row
        return btn, reset_btn

    def _build_overlay_display_controls(self, overlay: QWidget, layout: QVBoxLayout) -> None:
        """Separator plus the (initially hidden) display-controls container."""
        from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

        ctrl_sep = QFrame(overlay)
        ctrl_sep.setFrameShape(QFrame.Shape.HLine)
        ctrl_sep.setStyleSheet(f"color: {OVERLAY_BORDER}; margin: 2px 0;")
        layout.addWidget(ctrl_sep)

        controls_container = QWidget(overlay)
        controls_container.setObjectName("overlay_controls")
        controls_container.setVisible(False)
        ctrl_layout = QVBoxLayout(controls_container)
        ctrl_layout.setContentsMargins(0, 4, 0, 0)
        ctrl_layout.setSpacing(2)

        self._build_overlay_sliders(overlay, ctrl_layout)
        self._build_overlay_toggles(overlay, ctrl_layout)
        self._build_overlay_playback(overlay, ctrl_layout)
        self._build_overlay_camera(overlay, ctrl_layout)

        layout.addWidget(controls_container)
        ctrl_sep.setVisible(False)

        self._overlay_controls_container = controls_container
        self._overlay_ctrl_sep = ctrl_sep

    def _build_overlay_sliders(self, overlay: QWidget, ctrl_layout: QVBoxLayout) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget

        ps_row = QHBoxLayout()
        ps_row.setSpacing(4)
        ps_lbl = QLabel("Point size", overlay)
        ps_lbl.setFixedWidth(_OVERLAY_LABEL_W)
        ps_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_XS};")
        ps_row.addWidget(ps_lbl)
        ov_pt_slider = QSlider(Qt.Orientation.Horizontal, overlay)
        ov_pt_slider.setRange(5, 200)
        ov_pt_slider.setValue(20)
        ov_pt_slider.setFixedHeight(16)
        ps_row.addWidget(ov_pt_slider, 1)
        ov_pt_readout = QLabel("2.0", overlay)
        ov_pt_readout.setFixedWidth(30)
        ov_pt_readout.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: {FONT_XS};")
        ps_row.addWidget(ov_pt_readout)
        ctrl_layout.addLayout(ps_row)

        conf_row = QHBoxLayout()
        conf_row.setSpacing(4)
        conf_lbl = QLabel("Min confidence", overlay)
        conf_lbl.setFixedWidth(_OVERLAY_LABEL_W)
        conf_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_XS};")
        conf_row.addWidget(conf_lbl)
        ov_conf_slider = QSlider(Qt.Orientation.Horizontal, overlay)
        ov_conf_slider.setRange(0, 100)
        ov_conf_slider.setValue(0)
        ov_conf_slider.setFixedHeight(16)
        conf_row.addWidget(ov_conf_slider, 1)
        ov_conf_readout = QLabel("0%", overlay)
        ov_conf_readout.setFixedWidth(30)
        ov_conf_readout.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: {FONT_XS};")
        conf_row.addWidget(ov_conf_readout)
        ov_conf_container = QWidget(overlay)
        conf_row.setContentsMargins(0, 0, 0, 0)
        ov_conf_container.setLayout(conf_row)
        ctrl_layout.addWidget(ov_conf_container)

        # Overlay-only connections.
        ov_pt_slider.valueChanged.connect(
            lambda val: ov_pt_readout.setText(f"{val / 10.0:.1f}")
        )
        ov_conf_slider.valueChanged.connect(
            lambda val: ov_conf_readout.setText(f"{val}%")
        )

        self._ov_pt_slider = ov_pt_slider
        self._ov_conf_slider = ov_conf_slider
        self._ov_conf_container = ov_conf_container

    def _build_overlay_toggles(self, overlay: QWidget, ctrl_layout: QVBoxLayout) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

        ctrl_layout.addSpacing(4)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(4)
        ov_sem_btn = QToolButton(overlay)
        ov_sem_btn.setObjectName("ov_secondary")
        ov_sem_btn.setText("Class colours")
        ov_sem_btn.setCheckable(True)
        ov_sem_btn.setChecked(True)
        ov_sem_btn.setToolTip("On: colour by semantic class. Off: original camera RGB.")
        toggle_row.addWidget(ov_sem_btn, 1)
        ov_acc_btn = QToolButton(overlay)
        ov_acc_btn.setObjectName("ov_secondary")
        ov_acc_btn.setText("All frames")
        ov_acc_btn.setCheckable(True)
        ov_acc_btn.setChecked(True)
        ov_acc_btn.setToolTip("On: show all frames up to current. Off: current frame only.")
        toggle_row.addWidget(ov_acc_btn, 1)
        ov_toggle_container = QWidget(overlay)
        ov_toggle_container.setLayout(toggle_row)
        ctrl_layout.addWidget(ov_toggle_container)

        self._ov_sem_btn = ov_sem_btn
        self._ov_acc_btn = ov_acc_btn
        self._ov_toggle_container = ov_toggle_container

    def _build_overlay_playback(self, overlay: QWidget, ctrl_layout: QVBoxLayout) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QToolButton

        ctrl_layout.addSpacing(4)

        play_row = QHBoxLayout()
        play_row.setSpacing(4)
        ov_play_btn = QToolButton(overlay)
        ov_play_btn.setText("▶")
        ov_play_btn.setCheckable(True)
        ov_play_btn.setToolTip("Play / pause timeline")
        play_row.addWidget(ov_play_btn)
        fps_lbl = QLabel("FPS", overlay)
        fps_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_XS};")
        play_row.addWidget(fps_lbl)
        ov_fps_spin = QSpinBox(overlay)
        ov_fps_spin.setRange(1, 60)
        ov_fps_spin.setValue(8)
        ov_fps_spin.setFixedWidth(52)
        ov_fps_spin.setFixedHeight(22)
        play_row.addWidget(ov_fps_spin)
        ov_frustum_btn = QToolButton(overlay)
        ov_frustum_btn.setObjectName("ov_secondary")
        ov_frustum_btn.setText("Frustums")
        ov_frustum_btn.setCheckable(True)
        ov_frustum_btn.setChecked(True)
        ov_frustum_btn.setToolTip("Show / hide camera frustum wireframes")
        play_row.addWidget(ov_frustum_btn, 1)
        ctrl_layout.addLayout(play_row)

        self._ov_play_btn = ov_play_btn
        self._ov_fps_spin = ov_fps_spin
        self._ov_frustum_btn = ov_frustum_btn

    def _build_overlay_camera(self, overlay: QWidget, ctrl_layout: QVBoxLayout) -> None:
        """Where the 3D view sits relative to the frame: follow, snap, backoff.

        The three act on one thing, so they are a row of their own rather than
        tacked onto playback. Backoff only means anything once the view is on a
        frame pose, which is what the other two put it there for.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QToolButton

        ctrl_layout.addSpacing(4)

        cam_row = QHBoxLayout()
        cam_row.setSpacing(4)
        ov_follow_btn = QToolButton(overlay)
        ov_follow_btn.setObjectName("ov_secondary")
        ov_follow_btn.setText("Follow")
        ov_follow_btn.setCheckable(True)
        ov_follow_btn.setToolTip("Keep the 3D view on the current frame's camera")
        cam_row.addWidget(ov_follow_btn, 1)
        ov_snap_btn = QToolButton(overlay)
        ov_snap_btn.setObjectName("ov_secondary")
        ov_snap_btn.setText("Snap")
        ov_snap_btn.setToolTip("Snap the 3D view to the current frame's camera, once")
        cam_row.addWidget(ov_snap_btn, 1)
        ctrl_layout.addLayout(cam_row)

        backoff_row = QHBoxLayout()
        backoff_row.setSpacing(4)
        backoff_lbl = QLabel("Camera backoff", overlay)
        backoff_lbl.setFixedWidth(_OVERLAY_LABEL_W)
        backoff_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_XS};")
        backoff_row.addWidget(backoff_lbl)
        # Tenths of a metre, matching the point-size slider's ×10 integer trick:
        # QSlider is integer-only and a 0.1 m step is fine enough to frame a shot.
        ov_backoff_slider = QSlider(Qt.Orientation.Horizontal, overlay)
        ov_backoff_slider.setRange(0, 50)
        ov_backoff_slider.setValue(5)
        ov_backoff_slider.setFixedHeight(16)
        backoff_row.addWidget(ov_backoff_slider, 1)
        ov_backoff_readout = QLabel("0.5 m", overlay)
        ov_backoff_readout.setFixedWidth(40)
        ov_backoff_readout.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: {FONT_XS};")
        backoff_row.addWidget(ov_backoff_readout)
        ov_backoff_slider.valueChanged.connect(
            lambda val: ov_backoff_readout.setText(f"{val / 10.0:.1f} m")
        )
        ctrl_layout.addLayout(backoff_row)

        self._ov_follow_btn = ov_follow_btn
        self._ov_snap_btn = ov_snap_btn
        self._ov_backoff_slider = ov_backoff_slider

    def _reposition_pick_mode_overlay(self) -> None:
        overlay = getattr(self, "_pick_mode_overlay", None)
        if overlay is None:
            return
        margin = 8
        overlay.adjustSize()
        overlay.move(margin, margin)
        overlay.raise_()

    def _refresh_pick_marker(self) -> None:
        """Place the pick card (pinned at click) and re-project the leader anchor from world XYZ."""
        from PySide6.QtCore import QPoint

        if self._pick_card is None:
            return
        payload = self._last_pick_payload
        if payload is None:
            return
        canvas = self._viewer._canvas_container
        plotter = getattr(self._viewer, "_plotter", None)

        # Live anchor: project the picked world XYZ to plotter-local Qt pixels
        # (top-origin). Falls back to the click-time screen_xy when no plotter
        # exists yet.
        xyz_world = payload.get("xyz")
        screen_xy = payload.get("screen_xy", (0, 0))
        if xyz_world is not None and plotter is not None:
            disp = self._viewer.world_to_display(
                (float(xyz_world[0]), float(xyz_world[1]), float(xyz_world[2]))
            )
            if disp is not None:
                plotter_h = max(1, plotter.height())
                anchor_plotter_x = int(round(disp[0]))
                anchor_plotter_y = int(round(plotter_h - disp[1]))
            else:
                anchor_plotter_x = int(screen_xy[0])
                anchor_plotter_y = int(screen_xy[1])
        else:
            anchor_plotter_x = int(screen_xy[0])
            anchor_plotter_y = int(screen_xy[1])

        if plotter is not None:
            anchor_canvas = plotter.mapTo(
                canvas, QPoint(anchor_plotter_x, anchor_plotter_y)
            )
            anchor_cx, anchor_cy = anchor_canvas.x(), anchor_canvas.y()
        else:
            anchor_cx, anchor_cy = anchor_plotter_x, anchor_plotter_y

        self._pick_card.adjustSize()
        card_w = self._pick_card.width()
        card_h = self._pick_card.height()
        margin = 8
        offset = 18

        # First-time placement: position the card near the click. On
        # subsequent refreshes (camera-modified, canvas-resize) keep the card
        # where it already is so it doesn't chase the cursor or jitter.
        if self._pick_card_pinned_pos is None:
            init_cx, init_cy = anchor_cx, anchor_cy
            x = init_cx + offset
            if x + card_w > canvas.width() - margin:
                x = init_cx - offset - card_w
            y = init_cy + offset
            if y + card_h > canvas.height() - margin:
                y = init_cy - offset - card_h
            x = max(margin, min(x, max(margin, canvas.width() - card_w - margin)))
            y = max(margin, min(y, max(margin, canvas.height() - card_h - margin)))
            self._pick_card_pinned_pos = (x, y)
        else:
            # Reclamp in case the canvas shrank since the card was placed.
            px, py = self._pick_card_pinned_pos
            px = max(margin, min(px, max(margin, canvas.width() - card_w - margin)))
            py = max(margin, min(py, max(margin, canvas.height() - card_h - margin)))
            self._pick_card_pinned_pos = (px, py)

        x, y = self._pick_card_pinned_pos
        self._pick_card.move(x, y)
        self._pick_card.show()
        self._pick_card.raise_()
        self._viewer.legend_overlay.raise_()

        # VTK display coords are bottom-origin pixels of the plotter.
        anchor_display = None
        leader_display = None
        if plotter is not None:
            plotter_h = max(1, plotter.height())
            anchor_display = (
                float(anchor_plotter_x),
                float(plotter_h - anchor_plotter_y),
            )
            # Closest point on the (pinned) card edge to the live anchor,
            # mapped back to plotter coords.
            card_rect = self._pick_card.geometry()
            cx_target = max(card_rect.left(), min(anchor_cx, card_rect.right()))
            cy_target = max(card_rect.top(), min(anchor_cy, card_rect.bottom()))
            plotter_origin_in_canvas = plotter.mapTo(canvas, QPoint(0, 0))
            tx_plotter = cx_target - plotter_origin_in_canvas.x()
            ty_plotter = cy_target - plotter_origin_in_canvas.y()
            leader_display = (float(tx_plotter), float(plotter_h - ty_plotter))

        xyz = payload.get("xyz", (0.0, 0.0, 0.0))
        color = payload.get("color", (255, 220, 60))
        try:
            self._viewer.set_picked_marker(
                (float(xyz[0]), float(xyz[1]), float(xyz[2])),
                (int(color[0]), int(color[1]), int(color[2])),
                anchor_display=anchor_display,
                leader_target_display=leader_display,
            )
        except Exception:
            logger.exception("Failed to draw picked-point marker")

    def _on_viewer_status(self, event: str, **kwargs: object) -> None:
        if event == "start_run":
            self._clear_run_warnings()
            self._apply_progress("startup", "Starting reconstruction", 0, 0)
        elif event == "set_stage":
            stage = str(kwargs.get("stage", ""))
            status = str(kwargs.get("status", ""))
            message = str(kwargs.get("message", "") or "")
            # Finer phase routing when the orchestrator's set_stage message
            # names a known sub-step (e.g. "Computing PCA projection" inside
            # "outputs"); otherwise fall back to the top-level stage key.
            phase_key = _STAGE_MESSAGE_TO_PHASE.get(message, stage)
            stage_label = _STAGE_LABELS.get(stage, stage)
            label = message or stage_label
            if status == "completed":
                self._apply_progress(phase_key, f"{stage_label} complete", 1, 1)
            elif status == "warning":
                if message:
                    self._add_run_warning(str(message))
            else:
                self._apply_progress(phase_key, label, 0, 0)
        elif event == "update_progress":
            current = int(cast(SupportsInt, kwargs.get("current", 0) or 0))
            total = int(cast(SupportsInt, kwargs.get("total", 0) or 0))
            stage = str(kwargs.get("stage", ""))
            message = str(kwargs.get("message", "") or "")
            # A named sub-step (pose re-anchor, resume save) routes to its own phase
            # so the total bar advances through mapping instead of pinning at 100%;
            # unnamed messages fall back to the coarse stage key.
            phase_key = _STAGE_MESSAGE_TO_PHASE.get(message, stage)
            # The coarse stage is colored into the status line, so the label
            # is just the sub-step message (e.g. "Preparing frames for LoGeR").
            label = message or (_STAGE_LABELS.get(stage, stage) or "Working")
            # total == 0 is a deliberate "indeterminate" signal (e.g. the LoGeR
            # resize/upload prep), so drive the bar rather than dropping the update.
            self._apply_progress(phase_key, label, current, total)
        elif event == "data_ready":
            if self._viewer.has_scene_data:
                if not self._viewer.is_geometry_mode:
                    self._build_legend()
                self._show_viewer_controls()
                if self._viewer.is_geometry_mode:
                    self._set_semantic_only_controls_visible(False)
                self._on_viewer_control_changed()
            # data_ready is the end of a cached-run load, but only the mid-point of
            # a live reconstruction: the scene-file save still follows, and this
            # slot can run after that save has started (load_scene_data pumps
            # events). Only mark "complete" on the load path (no ETA estimator);
            # a reconstruction's real completion is the mark_outputs branch.
            if getattr(self, "_eta", None) is None:
                self._apply_progress("viewer_finalise", "Reconstruction complete", 1, 1)
            ortho_cloud = cast("SemanticPointCloud | None", kwargs.get("ortho_cloud"))
            ortho_grid = cast("OrthoGrid | None", kwargs.get("ortho_grid"))
            # The true point count only exists once the cloud is built; feed it in
            # so the remaining save/view stages estimate from a known size.
            est = getattr(self, "_eta", None)
            if est is not None and ortho_cloud is not None:
                est.set_points(len(ortho_cloud))
            cc = cast("ClassConfig", kwargs.get("classes_config") or self._classes_config)
            # The live ortho preview is a per-run nicety; skip the rebuild churn
            # while a batch marches through passes.
            if (
                ortho_cloud is not None
                and len(ortho_cloud) > 1
                and not getattr(self, "_survey_worker_running", False)
            ):
                try:
                    if ortho_grid is None:
                        from deepreefmap.postproc.ortho_outputs import build_ortho_outputs

                        outputs = build_ortho_outputs(ortho_cloud, cc)
                        ortho_grid = outputs.grid
                        cover = outputs.cover
                    else:
                        from deepreefmap.postproc.benthic_cover import compute_benthic_cover

                        cover = compute_benthic_cover(
                            ortho_grid.labels,
                            classes_config=cc,
                            counts=getattr(ortho_grid, "counts", None),
                        )
                    self._set_ortho_sources(ortho_cloud, ortho_grid, cc)
                    self._cover_label.setText(self._format_cover_html(cover))
                    self._cover_sunburst.set_cover(cover, cc)
                except Exception:
                    logger.exception("Failed to build live ortho preview")
        elif event == "setup_progress":
            message = str(kwargs.get("message", "Setting up viewer"))
            current = int(cast(SupportsInt, kwargs.get("current", 0) or 0))
            total = int(cast(SupportsInt, kwargs.get("total", 0) or 0))
            phase_key = _SETUP_MESSAGE_TO_PHASE.get(message, "viewer_index_cloud")
            # flush=True because viewer-setup happens on the GUI thread: without
            # an explicit processEvents the user sees the bars freeze.
            self._apply_progress(phase_key, message, current, total, flush=True)
        elif event == "mark_outputs":
            # One pass of a batch finished. The batch is the unit of work, so
            # neither this nor a failure ends the run or moves the user: the
            # batch worker carries on to the next pass and _on_survey_done is
            # what tidies up once there are none left.
            self._status_label.setText(f"Outputs saved to {kwargs.get('output_dir', '')}")
            self._refresh_survey_pass_statuses()
        elif event == "fail_run":
            error = kwargs.get("error_message", "unknown error")
            self._status_label.setText(f"Failed: {error}")
            self._refresh_survey_pass_statuses()
