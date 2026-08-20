"""The interface shell: destinations, and the shared survey store accessor.

One interface, and every panel in it is a single widget lent to the destination
that shows it: Browse into the shell, the model library and system panel into
Setup, the results panel into View mode, the run form into the settings dialog.
Two copies of any of them would disagree the moment a download or a path edit
landed against only one.

The header holds three peer destinations and no sequence: run_gate() treats a
pass with no transect as fine, so transects are not a precondition for
processing. Anything wrong with one of them is named once, in the alert box.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    browse_icon,
    cart_icon,
    transects_icon,
    videos_icon,
)
from deepreefmap_gui.core.theme import (
    BORDER,
    CARD_BG,
    DISABLED_FG,
    GUTTER,
    ON_ACCENT,
    PAGE_MARGIN,
    SPACE_SM,
    SPACE_XS,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import muted_label, utility_button_qss
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.notify.conditions import conditions_from_state
from deepreefmap_gui.runs.run_cards import run_facts_line
from deepreefmap_gui.runs.run_detail import RunDetailPanel
from deepreefmap_gui.simple.cart import CartButton
from deepreefmap_gui.simple.section_state import (
    SectionState,
    browse_state,
    run_gate,
    transects_state,
    videos_state,
)
from deepreefmap_gui.survey.catalogue import LINK_MISSING
from deepreefmap_gui.survey.health import (
    SETTLED,
    SurveyDbHealth,
    SurveyDbState,
    inspect_survey_db,
)
from deepreefmap_gui.survey.models.notification import WARNING as NOTIFY_WARNING
from deepreefmap_gui.survey.preset import (
    ActivePreset,
    OrgPreset,
    OverrideResult,
    clear_machine_override,
    describe_keys,
    deviations_from_org,
    load_active_preset,
    load_machine_override,
    registry_preset,
    save_machine_override,
)
from deepreefmap_gui.survey.store import SURVEY_DB_NAME, SurveyStore, latest_schema_version

logger = logging.getLogger(__name__)

# Which server preset this survey chose, stored beside the sync cursor because
# the runs it shapes live in that survey. Absent means the standard settings.
SERVER_PRESET_KEY = "preset.server_selection"

# Peers, not steps. None is a prerequisite for another: a pass with no transect
# processes perfectly well, so ordering them as a sequence would claim a
# dependency the gate does not enforce.
#
# Every one of them stays reachable while a batch runs. A batch takes tens of
# minutes, and planning the next transect or reading a finished run is exactly
# what that time is for; each job carries its own snapshot of the settings and
# the transect it was checked out with, so nothing edited here reaches the pass
# in flight. The few actions that would are refused where they are taken.
#
# Videos leads because it is where a day starts and where most of it is spent:
# the footage comes off the camera, sections are cut from it, and the cart is
# filled from those. Planning a transect is the rarer act, and a pass files
# against one whenever it is planned.
DESTINATIONS = ("videos", "transects", "process", "browse")

# The glyph that says what a destination holds. Constant per destination: what
# it currently has to report is the badge's job, and an icon that changed with
# state would leave the pill with no stable identity to recognise it by.
_DESTINATION_ICONS = {
    "transects": transects_icon,
    "videos": videos_icon,
    "process": cart_icon,
    "browse": browse_icon,
}

# What each destination pill says. The process pill reads Cart: the queue is
# the cart, checkout is Start processing.
_DESTINATION_LABELS = {
    "transects": "Transects",
    "videos": "Videos",
    "process": "Cart",
    "browse": "Browse",
}

# One line per destination, said in the terms of the work rather than the widget.
_DESTINATION_TIPS = {
    "transects": "The lines you survey, and what repeat passes of each one found.",
    "videos": "The footage itself: every clip, when it was shot, and what has been cut from it.",
    "process": "The cart: sections queued for the next session, and the batch as it runs.",
    "browse": "Every run so far, grouped however you need to read it.",
}

# Every destination the stack can show, in stack order. Machine, view, storage
# and server are appended last, and none of them is a destination: machine and
# server are utilities you visit and leave, storage belongs to a drive button,
# and view is where an opened run goes, reached by opening one. Everything is
# keyed by name, so the destinations may be reordered freely.
SIMPLE_SECTIONS = (*DESTINATIONS, "machine", "view", "storage", "server")

# Sections no destination pill owns. Machine and server are utilities you visit
# and leave, and storage belongs to a drive button at the foot of the window,
# which is what lights while its page is open.
NON_DESTINATIONS = ("machine", "storage", "server")

# What the info panel takes when it is open. Wide enough for the metadata block
# without eating into the cloud, which is what View mode is for.
VIEW_INFO_WIDTH = 340

_DESTINATION_ICON_PX = 16

# The same control as Log and Setup at the other end of the band: everything in
# the header is a button, so everything in the header is drawn as one.
#
# Not segmented_qss: a joined segmented control is what this app uses for filters
# and facets, and navigation in that shape reads as another filter.
_DESTINATION_QSS = utility_button_qss() + (
    f" QToolButton:disabled {{ color: {DISABLED_FG}; background: transparent;"
    f" border-color: {BORDER}; }}"
)

# Bars that top a page and are separated from it by a hairline. Object-name
# scoped: an unscoped `border-bottom` on the container is inherited by every
# child, which drew a stray underline beneath each label and button in the row.
_BAR_QSS = (
    f"QWidget {{ background-color: {CARD_BG}; }}"
    f" QWidget#simpleBar {{ border-bottom: 1px solid {BORDER}; }}"
)


# Preset key -> run-form widget attribute, one entry per settings widget. The
# settings dialog can edit the whole run form, so the preset snapshots all of it.
# Per-run inputs (video, run name, trim, transect length, output root) are not
# settings and never appear here.
#
# The resolution preset comes before the explicit width/height so restoring a
# Custom size wins over the native size the combo recomputes.
_PRESET_FIELD_WIDGETS = (
    ("fps", "_fps_spin"),
    ("segmentation_name", "_seg_combo"),
    ("mapping_name", "_map_combo"),
    ("camera_profile_name", "_profile_combo"),
    ("transect_crop_width", "_crop_width"),
    ("enable_tsdf", "_tsdf_check"),
    ("skip_segmentation", "_skip_seg_check"),
    ("resolution_preset", "_resolution_preset_combo"),
    ("processing_width", "_proc_width_spin"),
    ("processing_height", "_proc_height_spin"),
    ("preprocess_batch_size", "_batch_size_spin"),
    ("grid_bins", "_grid_bins_spin"),
    ("require_gravity_telemetry", "_require_gravity_check"),
    ("replacement_radius_factor", "_rr_factor_spin"),
    ("replacement_radius_estimation_frames", "_rr_est_frames_spin"),
    ("replacement_radius_override", "_rr_override_spin"),
    ("loger_window_size", "_loger_window_spin"),
    ("loger_overlap_size", "_loger_overlap_spin"),
    ("loger_model_path", "_loger_model_path_input"),
    ("refine_intrinsics_from_mapper", "_refine_intrinsics_check"),
    ("scs_target_width", "_scs_width_spin"),
    ("scs_target_height", "_scs_height_spin"),
    ("scs_checkpoint_path", "_scs_checkpoint_input"),
)

# Processing size follows the segmentation model's native size unless the user
# picks Custom, so a non-Custom preset stores null and leaves the form alone.
_NATIVE_SIZE_KEYS = ("processing_width", "processing_height")


def _widget_value(widget: QWidget) -> Any:
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QLineEdit):
        return widget.text().strip()
    raise TypeError(f"Unsupported form widget: {type(widget).__name__}")


def _set_widget_value(widget: QWidget, value: Any) -> None:
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        widget.setValue(value)
    elif isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
    elif isinstance(widget, QComboBox):
        widget.setCurrentText(str(value))
    elif isinstance(widget, QLineEdit):
        widget.setText(str(value))
    else:
        raise TypeError(f"Unsupported form widget: {type(widget).__name__}")


class InterfaceShellMixin(MixinBase):
    """DeepReefMapWindow methods for the workspace shell and the survey store."""

    _survey_store_obj: SurveyStore | None = None
    # Why the survey database could not be opened, when it could not be. None
    # until something has looked; read it through _survey_db_health.
    _survey_health: SurveyDbHealth | None = None
    # The configuration as persisted: organisation preset plus this machine's
    # allow-listed changes. _survey_preset is what the run will use, which can
    # additionally hold a session-only edit the override would not keep.
    _active_preset: ActivePreset | None = None
    _form_defaults: dict[str, Any]
    _simple_nav_buttons: dict[str, QToolButton]
    _destination_group: QButtonGroup
    _section_state_cache: tuple | None = None
    # False until the video library and the run archive have been read once.
    # Until then an empty verdict means "not looked yet", not "nothing wrong".
    _survey_loaded: bool = False
    _work_area_state: tuple[bool, str, bool] | None = None

    def _reveal_memory_detail(self) -> None:
        """Where the memory warning sends you: the readiness rows, which carry
        the same sentence without the system panel's jargon."""
        self._set_simple_section("machine")
        self._set_machine_view("readiness")

    def _idle_status_text(self) -> str:
        return (
            "Ready. Add videos under Videos, cut sections to process, and mark "
            "out transects to compare them against."
        )

    def _refresh_browse_state(self) -> None:
        """Cache Browse's count from entries the data manager already scanned."""
        entries = getattr(self, "_data_entries", None) or []
        unfiled = sum(1 for entry in entries if entry.transect_id is None)
        self._browse_state = browse_state(len(entries), unfiled)
        self._refresh_section_state()

    def _adopt_form_as_preset(self) -> None:
        """Persist the settings this machine is allowed to change, and only those.

        The organisation preset owns the method: which models run, at what
        resolution, over how many frames. Those decide the numbers a survey
        reports, so one diver's curiosity must not rewrite them for every dive
        after. Only MACHINE_OVERRIDABLE_KEYS describe the computer rather than
        the method, and only those are written back.

        A locked preset (an administrator named one) puts the rest of the form
        back to standard, because an authoritative configuration that the next
        run would silently ignore is not authoritative. An unlocked one leaves
        the edit in place for this session and says it will not be kept.
        """
        # With no readable organisation preset there is nothing to measure a
        # deviation against, so there is nothing meaningful to write either.
        if self._active_preset is None:
            return
        org = self._active_preset.org
        try:
            result = save_machine_override(self._collect_preset_from_form(), org)
        except OSError as exc:
            logger.warning("Could not save the settings for this machine: %s", exc)
            self._status_label.setText(f"Settings not saved: {exc}")
            return
        self._active_preset = ActivePreset(org=org, overrides=result.saved)
        if result.refused and org.locked:
            self._populate_form_from_preset(self._active_preset.settings)
        # What the run will use, which is the form: a session-only deviation the
        # organisation preset would not keep still reaches the pipeline.
        self._survey_preset = self._collect_preset_from_form()
        self._announce_preset_save(result, org)
        self._survey_preset_label.setText(self._survey_preset_summary())

    def _announce_preset_save(self, result: OverrideResult, org: OrgPreset) -> None:
        """Say what was kept and what was not, naming the settings in plain words."""
        if result.refused and org.locked:
            self._status_label.setText(
                f"{org.name} sets {describe_keys(result.refused)}, so that went back to standard."
            )
        elif result.refused:
            self._status_label.setText(
                f"{describe_keys(result.refused)} changed for now, but the standard"
                " settings come back next launch."
            )
        elif result.saved:
            self._status_label.setText(
                f"Saved for this machine: {describe_keys(result.saved)}."
            )

    def _survey_deviations(self) -> dict[str, Any]:
        """Form settings that differ from the organisation preset.

        Read from the form rather than the saved override, because a batch runs
        from _collect_run_settings(): a session-only edit changes the run just as
        much as a saved one, so it is named and recorded just the same.
        """
        if self._active_preset is None:
            return {}
        return deviations_from_org(
            self._collect_preset_from_form(), self._active_preset.org
        )

    def _reload_active_preset(self) -> None:
        """Re-resolve the organisation preset and this machine's changes.

        A malformed admin file is caught here rather than allowed to abort window
        construction: the gate then blocks with a reason the diver can read,
        instead of the app refusing to open on a field laptop.

        A server preset the operator selected takes the organisation slot,
        unless an administrator's file is in force: a mandate outranks a menu.
        """
        try:
            self._active_preset = load_active_preset()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self._active_preset = None
            self._survey_preset = None
            logger.warning("Settings unavailable: %s", exc)
            return
        server = self._selected_server_preset()
        if server is not None and not self._active_preset.org.locked:
            self._active_preset = ActivePreset(
                org=server, overrides=load_machine_override(server)
            )
        self._survey_preset = self._active_preset.settings

    def _selected_server_preset(self) -> OrgPreset | None:
        """The server preset this survey chose, or None when it chose none.

        None also covers a selection the registry has since removed: the run
        must not silently keep stale settings under a name the console deleted,
        so the standard comes back and the label says which settings are live.
        """
        self._server_preset_withdrawn = ""
        store = self._try_survey_store()
        if store is None:
            return None
        raw = store.sync_state(SERVER_PRESET_KEY)
        if not raw:
            return None
        try:
            wanted = json.loads(raw)
            row = store.get_server_preset(str(wanted["name"]), int(wanted["version"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring an unreadable server preset selection")
            return None
        if row is None:
            logger.info("The selected server preset is no longer on the registry")
            # Kept for the settings label: falling back to the standard is
            # right, doing it without a word is not.
            self._server_preset_withdrawn = (
                f"{wanted.get('name')} (v{wanted.get('version')})"
            )
            return None
        return registry_preset(row.name, row.version, row.settings)

    def _on_choose_server_preset(self) -> None:
        """Offer the presets the registry publishes, additional to the standard."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        if self._active_preset is not None and self._active_preset.org.locked:
            self._status_label.setText(
                f"{self._active_preset.org.name} is set by your organisation, "
                "so server presets cannot replace it."
            )
            return
        store = self._try_survey_store()
        presets = store.list_server_presets() if store is not None else []
        if not presets:
            self._status_label.setText(
                "No presets from the server yet. They arrive with a sync once "
                "the web console publishes one."
            )
            return
        labels = [
            f"{p.label} · {p.description}" if p.description else p.label for p in presets
        ]
        from PySide6.QtWidgets import QInputDialog

        chosen, accepted = QInputDialog.getItem(
            self,
            "Server presets",
            "Published by your organisation's registry:",
            labels,
            0,
            False,
        )
        if not accepted or store is None:
            return
        preset = presets[labels.index(chosen)]
        store.set_sync_state(
            SERVER_PRESET_KEY,
            json.dumps({"name": preset.name, "version": preset.version}),
        )
        self._reload_active_preset()
        if self._survey_preset is not None:
            self._populate_form_from_preset(self._survey_preset)
        self._recompute_survey_start()
        self._status_label.setText(
            f"Settings are now {preset.label}, from the server."
        )
        self._survey_preset_label.setText(self._survey_preset_summary())

    def _restore_standard_settings(self) -> None:
        """Drop this machine's changes and go back to the organisation preset."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        # Ask before restoring, or the answer is always "nothing changed".
        deviated = bool(self._survey_deviations())
        had_override = clear_machine_override()
        store = self._try_survey_store()
        had_server = False
        if store is not None and store.sync_state(SERVER_PRESET_KEY):
            store.set_sync_state(SERVER_PRESET_KEY, None)
            had_server = True
        self._reload_active_preset()
        if self._survey_preset is not None:
            self._populate_form_from_preset(self._survey_preset)
        else:
            # No readable preset to restore from, so the fresh-window values are
            # the only known-good state left to offer.
            self._reset_form_defaults()
        self._recompute_survey_start()
        if self._active_preset is None:
            self._status_label.setText(
                "Settings could not be read. The form has reverted to its defaults."
            )
        elif had_override or deviated or had_server:
            self._status_label.setText(
                f"Settings are back to {self._active_preset.org.label}."
            )
        else:
            self._status_label.setText(
                f"Already on {self._active_preset.org.label}; nothing to restore."
            )

    def _load_standard_into_form(self) -> None:
        """Load the organisation standard into the form without persisting.

        The run settings dialog borrows the live form, so its Restore button must
        only touch widgets: the caller's snapshot undoes it on Cancel, and OK
        persists the result, which clears the machine override once the form is
        back on the standard. The persistent restore is the Run page's own button.
        """
        if self._active_preset is not None:
            self._populate_form_from_preset(self._active_preset.org.settings)
        else:
            self._reset_form_defaults()

    def _activate_interface(self) -> None:
        """Bring the shell up once everything it reads has been built.

        There is one interface, so these refreshes belong to construction rather
        than to a mode change.
        """
        # The form is never shown outside the run settings dialog, but the batch
        # runs from _collect_run_settings(), which reads exactly these widgets.
        # Left on the constructor defaults, a run would ignore the preset.
        if self._survey_preset is not None:
            self._populate_form_from_preset(self._survey_preset)
        self._update_memory_profile_warning()
        if getattr(self, "_app_mode", "SETUP") == "SETUP":
            self._status_label.setText(self._idle_status_text())
        self._host_machine_panels()
        self._refresh_transect_list()
        self._refresh_survey_batch_tab()
        self._refresh_survey_analysis()
        self._refresh_data_manager()
        self._refresh_readiness_view()
        self._sync_system_gauges_running()
        self._update_work_area()
        # Everything the verdicts read has now been read at least once, so an
        # empty verdict from here on means "nothing wrong", not "not looked yet".
        self._survey_loaded = True
        self._refresh_section_state()

    def _build_simple_shell(self) -> QWidget:
        """Three destinations over one page stack.

        Transects are the lines you survey, Process is where a session's videos
        are queued and watched, and Browse is everything produced so far. You
        move between them freely: none is a step you finish, so none of them
        gates another.

        One band, one switch, one thing filled at a time. Utilities sit at the
        far end, bordered rather than filled, so "where am I working" and "what
        can I go and check" never look like the same kind of control.
        """
        shell = QWidget()
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Built here so it exists before the first _sync_destination_chrome, but
        # added to the window's central column rather than to this shell: in View
        # mode the shell is squeezed to zero width, and a Back button inside it
        # would go with it.
        self._build_view_bar()

        header = QWidget()
        header.setObjectName("simpleBar")
        header.setStyleSheet(_BAR_QSS)
        self._simple_header = header
        nav = QHBoxLayout(header)
        nav.setContentsMargins(PAGE_MARGIN, SPACE_XS, PAGE_MARGIN, SPACE_XS)
        nav.setSpacing(SPACE_XS)

        self._simple_stack = QStackedWidget()
        self._simple_nav_buttons = {}
        self._section_state_cache = None

        # Pages are built before the buttons that select them: Process builds
        # the start button, and Transects builds the list its badge counts.
        pages = {
            "transects": self._build_plan_page(),
            "videos": self._build_video_library(),
            "process": self._build_simple_run_page(),
            "browse": self._build_browse_page(),
            "machine": self._build_machine_page(),
            "storage": self._build_storage_page(),
            "server": self._build_server_page(),
            "view": self._build_view_info_page(),
        }

        nav_group = QButtonGroup(shell)
        nav_group.setExclusive(True)
        self._destination_group = nav_group
        for name in DESTINATIONS:
            # The process pill is the cart, so it carries the cart's count badge.
            if name == "process":
                self._cart_button = CartButton()
                btn: QToolButton = self._cart_button
            else:
                btn = QToolButton()
            btn.setText(_DESTINATION_LABELS[name])
            btn.setCheckable(True)
            btn.setStyleSheet(_DESTINATION_QSS)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(_DESTINATION_ICON_PX, _DESTINATION_ICON_PX))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(_DESTINATION_TIPS[name])
            nav_group.addButton(btn)
            # Every pill but the cart sits at the left with the work; the cart
            # goes to the far right, past the utilities, behind a divider.
            if name != "process":
                nav.addWidget(btn)
            btn.toggled.connect(partial(self._on_simple_nav_toggled, name))
            self._simple_nav_buttons[name] = btn

        nav.addStretch(1)
        # Utilities, at the far end from the work. Bordered rather than filled
        # (see utility_button_qss): these are places you visit and leave, not
        # where you are working. What a destination holds is shown on the
        # destination; everything that is wrong with any of them is behind the
        # bell, which is empty unless something is.
        nav.addWidget(self._build_notification_bell())
        nav.addWidget(self._log_toggle_btn)
        nav.addWidget(self._build_server_nav_button())
        nav.addWidget(self._build_machine_nav_button())
        # The cart last, split from the utilities: it is a destination, badged
        # with what the next session holds.
        nav.addSpacing(SPACE_SM)
        divider = QWidget()
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background: {BORDER};")
        nav.addWidget(divider)
        nav.addSpacing(SPACE_SM)
        nav.addWidget(self._cart_button)
        layout.addWidget(header)

        for name in SIMPLE_SECTIONS:
            self._simple_stack.addWidget(pages[name])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        body_layout.setSpacing(GUTTER)
        body_layout.addWidget(self._simple_stack, 1)
        layout.addWidget(body, 1)

        self._set_simple_section(self._initial_simple_section())
        self._refresh_section_state()
        return shell

    def _build_browse_page(self) -> QWidget:
        """Browse: the run archive, and the detail of whatever is selected in it.

        The whole page is the browse panel. Transect comparison is one page of
        its detail pane, shown when a transect is what is selected.
        """
        return self._build_data_panel()

    def _build_view_bar(self) -> QWidget:
        """The way out of View mode, and the switch for the metadata beside it.

        Full window width, so it survives the info column collapsing to nothing.
        """
        self._view_info_open = False
        bar = QWidget()
        bar.setObjectName("simpleBar")
        bar.setStyleSheet(_BAR_QSS)
        row = QHBoxLayout(bar)
        row.setContentsMargins(PAGE_MARGIN, SPACE_SM, PAGE_MARGIN, SPACE_SM)
        row.setSpacing(GUTTER)

        # A breadcrumb, not a way out. The header above stays put with Browse
        # lit, so an opened run reads as a place inside Browse rather than a
        # mode that has taken the window; the crumb says where inside.
        crumb = QPushButton("Browse")
        crumb.setProperty("quiet", "true")
        crumb.setCursor(Qt.CursorShape.PointingHandCursor)
        crumb.setToolTip("Back to the run list.")
        crumb.clicked.connect(lambda: self._set_simple_section("browse"))
        row.addWidget(crumb)
        row.addWidget(muted_label("›"))

        self._view_title = QLabel("")
        font = self._view_title.font()
        font.setWeight(QFont.Weight.DemiBold)
        self._view_title.setFont(font)
        row.addWidget(self._view_title)

        # The run's size, beside its name rather than in a band of its own. Two
        # facts and a warning: everything else the manifest holds is a row in
        # the Info pane, and the bar is what the cloud is not using.
        self._view_facts = muted_label("")
        row.addWidget(self._view_facts)

        row.addStretch(1)

        self._view_info_btn = QToolButton()
        self._view_info_btn.setText("Info")
        self._view_info_btn.setCheckable(True)
        self._view_info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_info_btn.setToolTip("Show what this run is, beside the cloud.")
        self._view_info_btn.setStyleSheet(_DESTINATION_QSS)
        self._view_info_btn.toggled.connect(self._on_view_info_toggled)
        row.addWidget(self._view_info_btn)

        bar.setVisible(False)
        bar.installEventFilter(self)
        self._view_bar = bar
        self._view_bar_row = row
        return bar

    def _set_view_bar_visible(self, visible: bool) -> None:
        if hasattr(self, "_view_bar"):
            self._view_bar.setVisible(visible)

    def _show_run_facts(self, manifest: dict) -> None:
        """Name what the open run measured, in the bar above the cloud."""
        text = run_facts_line(manifest)
        self._view_facts.setText(text)
        self._view_facts.setToolTip(text)
        # On the bar as well, because the facts are hidden at the width where
        # they are most worth asking for.
        self._view_bar.setToolTip(text)
        self._fit_view_facts()

    def _clear_run_facts(self) -> None:
        self._view_facts.setText("")
        self._view_facts.setToolTip("")
        self._view_bar.setToolTip("")

    def _fit_view_facts(self) -> None:
        """Drop the facts from a bar too narrow to hold them and the name.

        The name is what identifies the run and Info is the way to everything
        else, so the facts are what goes when the three cannot fit. Measured
        rather than gated on a written width: how much room the name wants is
        the run's name, and a long one is exactly when the facts have to go.
        """
        row = self._view_bar_row
        margins = row.contentsMargins()
        spent = margins.left() + margins.right()
        for index in range(row.count()):
            item = row.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is None or widget is self._view_facts:
                continue
            spent += widget.sizeHint().width() + row.spacing()
        facts = self._view_facts
        facts.setVisible(
            bool(facts.text())
            and self._view_bar.width() - spent >= facts.sizeHint().width() + row.spacing()
        )

    def _view_bar_event_filter(self, obj, event) -> None:
        """Refit the bar's facts when it is resized.

        Guarded on the bar existing: the filter is installed on the window, so
        it sees events raised while the window is still being built.
        """
        if obj is getattr(self, "_view_bar", None) and event.type() == QEvent.Type.Resize:
            self._fit_view_facts()

    def _on_view_info_toggled(self, checked: bool) -> None:
        self._view_info_open = checked
        self._update_work_area()

    def _enter_view_mode(self, run_dir: Path) -> None:
        """Point View mode at the run now on screen, and go there.

        The catalogue entry carries what the info panel shows, but a run opened
        from outside the output root has none; the title still names it, and the
        panel stays on whatever it last described rather than showing another
        run's facts under this one's name.
        """
        entry = next(
            (e for e in getattr(self, "_data_entries", []) if e.run_dir == run_dir),
            None,
        )
        if entry is not None:
            self._view_title.setText(entry.display_name)
            self._view_detail.show_entry(entry)
        else:
            manifest = getattr(self, "_active_run_manifest", None) or {}
            self._view_title.setText(manifest.get("name") or run_dir.name)
            self._view_detail.clear()
        self._set_simple_section("view")

    def _build_view_info_page(self) -> QWidget:
        """What the run on screen is, and what it produced, beside the cloud.

        The whole point of View mode is the viewport, so this column is off by
        default and the Info button in the view bar brings it back. It reuses the
        Browse detail pane's filler, so the two cannot describe one run
        differently, and it takes the results panel: the ortho, the benthic
        cover, the transect crop and the exports all act on the run you are
        looking at, which is the only place they were ever about.

        Scrolls as one column rather than per section: the metadata is short and
        the results are long, and two scrollbars side by side in a 340px column
        is more chrome than content.
        """
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)
        self._view_detail = RunDetailPanel()
        self._view_detail.cover.set_classes_config(self._classes_config)
        layout.addWidget(self._view_detail)
        layout.addWidget(self._results_page)
        layout.addStretch(1)

        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setWidget(column)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return page

    def _current_section(self) -> str:
        index = self._simple_stack.currentIndex()
        if 0 <= index < len(SIMPLE_SECTIONS):
            return SIMPLE_SECTIONS[index]
        return DESTINATIONS[0]

    def _sync_destination_chrome(self) -> None:
        """Light the pill the live section belongs to, and recolour its glyph.

        The icon has to be repainted per state because a checked pill is filled
        with the accent and its ink is near-black, while an unchecked one is
        transparent over the dark shell: one colour cannot serve both.
        """
        section = self._current_section()
        # The header stays through View. An opened run is a page inside Browse,
        # so hiding the destination switch to show it made the app look like it
        # had changed mode, and left no way to Transects or Process without
        # first backing out of a cloud.
        self._simple_header.setVisible(True)
        self._set_view_bar_visible(section == "view")
        if section in NON_DESTINATIONS:
            # Not a destination, so no pill should own it.
            #
            # Exclusivity is lifted first: an exclusive QButtonGroup refuses to
            # let its one checked member be unchecked, so this silently did
            # nothing and Browse stayed lit while Setup was on screen.
            self._destination_group.setExclusive(False)
            for button in self._simple_nav_buttons.values():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            self._destination_group.setExclusive(True)
        else:
            # View is a page inside Browse, so Browse is what it lights: the
            # cloud you are looking at came from the archive you are still in.
            lit = "browse" if section == "view" else section
            button = self._simple_nav_buttons[lit]
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)
        for name, button in self._simple_nav_buttons.items():
            ink = QColor(ON_ACCENT if button.isChecked() else WINDOW_TEXT)
            button.setIcon(_DESTINATION_ICONS[name](_DESTINATION_ICON_PX, ink))
        # Guarded: the shell is built before the bottom bar, and this runs once
        # from _build_simple_shell while the drive buttons do not exist yet.
        if hasattr(self, "_storage_bars"):
            self._sync_storage_buttons()

    def _videos_verdict(self) -> SectionState:
        """What the footage has to report, counted off the library as it stands."""
        clips = getattr(self, "_video_entries", [])
        missing = sum(1 for clip in clips if clip.link_state == LINK_MISSING)
        return videos_state(len(clips), missing)

    def _refresh_section_state(self) -> None:
        """Paint the header from the cached verdicts: tooltips, and the alert.

        A pure painter: it reads attributes other code has already computed and
        never touches the store or the filesystem, because it is called from
        paths that run on every keystroke.
        """
        if not hasattr(self, "_simple_nav_buttons"):
            return
        states = {
            "transects": getattr(self, "_plan_state", None) or transects_state(0, False),
            "videos": self._videos_verdict(),
            "process": getattr(self, "_survey_gate", None) or run_gate(
                pass_count=0,
                unassigned=0,
                remaining=0,
                failed=0,
                has_preset=True,
                missing_models=[],
            ),
            "browse": getattr(self, "_browse_state", None) or browse_state(0, 0),
        }
        machine = self._machine_verdict() if hasattr(self, "_machine_button") else None
        # The cause and the number behind the count, not the sentence: a reworded
        # reason is the same fault, and repainting for it would churn the log.
        key = tuple((name, s.state, s.count, s.cause, s.n) for name, s in states.items()) + (
            (machine.state, machine.cause, machine.n) if machine is not None else (),
        )
        if self._section_state_cache == key:
            return
        self._section_state_cache = key
        for name, verdict in states.items():
            self._simple_nav_buttons[name].setToolTip(
                "\n".join(filter(None, [_DESTINATION_TIPS[name], verdict.count, verdict.reason]))
            )
        conditions = conditions_from_state(states, machine, getattr(self, "_survey_health", None))
        if self._notify.reconcile(conditions, authoritative=self._survey_loaded):
            self._refresh_notification_bell()

    def _go_to_section(self, name: str) -> None:
        """Follow a link to another destination, remembering where it came from.

        The one entry point that records history. Choosing a destination from the
        header does not: that is a fresh start, not a step to unwind.
        """
        self._remember_place()
        self._set_simple_section(name)
        self._remember_place()

    # --- Back and forward ----------------------------------------------------

    def _navigation_history(self):
        history = getattr(self, "_nav_history", None)
        if history is None:
            from deepreefmap_gui.simple.navigation import NavigationHistory

            history = NavigationHistory()
            self._nav_history = history
        return history

    def _current_place(self):
        from deepreefmap_gui.simple.navigation import Place

        return Place(self._current_section(), self._current_selection())

    def _current_selection(self) -> str | None:
        """What is picked out on the page now, so returning restores it too."""
        section = self._current_section()
        if section == "videos":
            return self._selected_pass_id
        if section == "transects":
            chosen = self._selected_transect_id()
            return str(chosen) if chosen is not None else None
        return None

    def _remember_place(self) -> None:
        if getattr(self, "_nav_restoring", False):
            return
        self._navigation_history().push(self._current_place())

    def _go_back(self) -> bool:
        return self._follow(self._navigation_history().back)

    def _go_forward(self) -> bool:
        return self._follow(self._navigation_history().forward)

    def _follow(self, step) -> bool:
        """Take a history step, skipping any whose target has since gone."""
        history = self._navigation_history()
        place = step()
        while place is not None and not self._restore_place(place):
            place = history.drop_current()
        return place is not None

    def _restore_place(self, place) -> bool:
        """Go back to a remembered place. False when it is no longer there."""
        if place.section not in SIMPLE_SECTIONS:
            return False
        self._nav_restoring = True
        try:
            self._set_simple_section(place.section)
            if place.selection is None:
                return True
            if place.section == "videos":
                return self._select_section(place.selection)
            if place.section == "transects":
                self._open_transect_page(place.selection)
        finally:
            self._nav_restoring = False
        return True

    def _navigation_event_filter(self, obj, event) -> bool:
        """Back and forward, from the mouse's side buttons or Alt+arrow.

        Filtered rather than overridden: the press lands on whichever child
        widget is under the cursor. A text field has first claim on Alt+Left, so
        the shortcut stands down for whatever the key was headed to.
        """
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.BackButton:
                return self._go_back()
            if event.button() == Qt.MouseButton.ForwardButton:
                return self._go_forward()
            return False
        if etype != QEvent.Type.KeyPress:
            return False
        if not event.modifiers() & Qt.KeyboardModifier.AltModifier:
            return False
        if isinstance(obj, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
            return False
        if event.key() == Qt.Key.Key_Left:
            return self._go_back()
        if event.key() == Qt.Key.Key_Right:
            return self._go_forward()
        return False

    def _on_simple_nav_toggled(self, name: str, checked: bool) -> None:
        if checked:
            self._set_simple_section(name)

    def _set_simple_section(self, name: str) -> None:
        if name not in SIMPLE_SECTIONS:
            raise ValueError(f"Unknown simple section: {name!r}")
        if name in DESTINATIONS:
            button = self._simple_nav_buttons[name]
            if not button.isChecked():
                button.blockSignals(True)
                button.setChecked(True)
                button.blockSignals(False)
        if name == "machine":
            self._refresh_readiness_view()
        self._simple_stack.setCurrentIndex(SIMPLE_SECTIONS.index(name))
        self._sync_destination_chrome()
        if name == "storage":
            self._refresh_storage_page()
        if name == "server":
            self._refresh_server_page()
        # The gauges poll at 1 Hz, so they run only while they are on screen.
        self._sync_system_gauges_running()
        self._update_work_area()

    def _update_work_area(self) -> None:
        """The one place that decides whether the viewer pane shows and how the
        work splitter divides, from (app_mode, section)."""
        if not hasattr(self, "_work_hsplitter"):
            return
        section = (
            self._current_section() if hasattr(self, "_simple_stack") else DESTINATIONS[0]
        )
        # The point cloud gets a destination of its own rather than half of
        # Browse. Browsing wants the whole window for the table, and looking at
        # a cloud wants the whole window for the cloud; sharing served neither.
        show_viewer = section == "view"
        info_open = getattr(self, "_view_info_open", False)
        state = (show_viewer, section, info_open)
        if getattr(self, "_work_area_state", None) == state:
            return
        self._work_area_state = state
        self._viewer.setVisible(show_viewer)
        total = max(self._work_hsplitter.width(), 800)
        if not show_viewer:
            self._work_hsplitter.setSizes([total, 0])
        else:
            # View mode gives the viewport everything the info panel is not using.
            left = min(VIEW_INFO_WIDTH, total // 3) if info_open else 0
            self._work_hsplitter.setSizes([left, total - left])

    def _snapshot_form_settings(self) -> dict[str, Any]:
        """Every settings widget's current value, keyed by attribute name.

        Widget values rather than a preset dict: the preset drops the processing
        size when the resolution is not Custom, which is right for storage and
        wrong for an undo, since cancelling has to put back exactly what was
        there.
        """
        return {attr: _widget_value(getattr(self, attr)) for _, attr in _PRESET_FIELD_WIDGETS}

    def _restore_form_settings(self, snapshot: dict[str, Any]) -> None:
        for _, attr in _PRESET_FIELD_WIDGETS:
            _set_widget_value(getattr(self, attr), snapshot[attr])

    def _capture_form_defaults(self) -> None:
        """Snapshot fresh-window values of every settings field, so the run
        settings dialog can offer Reset defaults."""
        self._form_defaults = self._snapshot_form_settings()

    def _collect_preset_from_form(self) -> dict[str, Any]:
        preset: dict[str, Any] = {}
        custom_size = self._resolution_preset_combo.currentText() == "Custom"
        for key, attr in _PRESET_FIELD_WIDGETS:
            value = _widget_value(getattr(self, attr))
            if key == "transect_crop_width":
                value = value or None
            elif key in _NATIVE_SIZE_KEYS and not custom_size:
                value = None
            preset[key] = value
        return preset

    def _populate_form_from_preset(self, preset: dict[str, Any]) -> None:
        """Fill the form from a settings bag, one key at a time.

        A key that will not go into its widget is skipped rather than allowed to
        abandon the loop half way. Values reaching here have been coerced
        against `preset_schema`, but a stored preset written by an older build
        has not, and a form left part filled is a run configured as nobody asked.
        """
        for key, attr in _PRESET_FIELD_WIDGETS:
            value = preset.get(key)
            if key == "transect_crop_width" and value is None:
                value = 0.0
            elif key in _NATIVE_SIZE_KEYS and value is None:
                continue
            try:
                _set_widget_value(getattr(self, attr), value)
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring unusable preset value for %s: %r", key, value)

    def _reset_form_defaults(self) -> None:
        self._restore_form_settings(self._form_defaults)

    def _survey_store(self) -> SurveyStore:
        """Store keyed to the current output root; reopened when the root changes.

        Held fixed for as long as a batch runs. The worker was handed this store
        and writes each pass status through it, and SurveyStore's connections are
        thread-local, so closing it here would not touch the worker's own
        connection: the batch would carry on writing to a database the window no
        longer reads. The batch owns the root it started under.
        """
        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        store = self._survey_store_obj
        if store is not None and store.path != db_path and self._survey_worker_running:
            logger.warning(
                "Output root changed to %s while a batch is running; keeping %s until it ends",
                db_path,
                store.path,
            )
            return store
        if store is None or store.path != db_path:
            if store is not None:
                store.close()
            store = SurveyStore(db_path)
            self._survey_store_obj = store
            # Episodes belong to the survey they were about, and nothing in the
            # new root has been read yet, so the next reconcile must not clear
            # what it cannot see.
            self._survey_loaded = False
            self._rebind_notification_log(store)
            if store.interrupted_at_open:
                self._notify_post(
                    {
                        "fingerprint": "runs.interrupted",
                        "title": f"{store.interrupted_at_open} run(s) were left unfinished",
                        "body": "The app closed before they finished. They can be started again.",
                        "severity": NOTIFY_WARNING,
                        "section": "browse",
                    }
                )
        self._survey_health = SurveyDbHealth(SurveyDbState.OK, db_path, latest_schema_version())
        return store

    def _try_survey_store(self) -> SurveyStore | None:
        """The store, or None with the reason recorded in ``_survey_health``.

        Opening happens while the window is still being built, so a database this
        build cannot read -- one left by a newer version after a rollback, a
        corrupt file, an output root on an unplugged drive -- would otherwise
        take the whole launch down before there is anything to show the error in.
        Every caller that runs during construction goes through here.

        An open store is its own verdict, so a root that has already been opened
        is not re-inspected: refreshes arrive by the handful whenever the output
        folder changes, and the answer can only change when the root changes or
        the store is dropped, both of which clear ``_survey_store_obj``.

        A verdict of "will not open" is held on to just as firmly. Retrying it
        cannot succeed -- nothing has changed since the last attempt -- and each
        attempt logs a traceback and takes a backup, so the handful of refreshes
        becomes a flood. check_survey_database clears the verdict once recovery
        has actually altered something.
        """
        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        open_store = self._survey_store_obj
        health = self._survey_health
        if (
            open_store is not None
            and open_store.path == db_path
            and health is not None
            and health.state is SurveyDbState.OK
        ):
            return open_store
        # Only a settled verdict is taken on trust: an unwritable location or a
        # corrupt file can be fixed while the app is open.
        if (
            health is not None
            and health.path == db_path
            and health.state in SETTLED
        ):
            return None
        health = inspect_survey_db(db_path)
        if not health.openable:
            self._survey_health = health
            return None
        try:
            return self._survey_store()
        except Exception as exc:
            # inspect_survey_db opens read-only and cannot see everything that
            # can go wrong in a read-write open, so this is the backstop.
            logger.exception("Survey database unavailable at %s", db_path)
            self._survey_health = SurveyDbHealth(
                SurveyDbState.CORRUPT, db_path, latest_schema_version(), detail=str(exc)
            )
            return None

    def check_survey_database(self) -> None:
        """Put the survey database back if it will not open. Called after show().

        After, not during, construction: the window has to exist and be visible
        before anything modal, and declining has to leave a usable app rather
        than an aborted launch.

        A rolled-back update is undone without asking: automatic_recovery names
        the route that restores the survey exactly, and taking it needs nothing
        from the user. The choice is put to them only when there is no such
        route or it fails, because then every route left costs something, and
        which cost is acceptable is not ours to decide.
        """
        health = self._survey_db_health()
        if health.openable:
            return
        from deepreefmap_gui.survey.recovery import (
            RecoveryKind,
            apply_recovery,
            automatic_recovery,
        )
        from deepreefmap_gui.survey.recovery_dialog import SurveyRecoveryDialog

        out_root = Path(self._out_root_input.text()).expanduser()
        automatic = automatic_recovery(health)
        if automatic is not None:
            try:
                message = apply_recovery(automatic, health, out_root)
            except Exception as exc:
                logger.exception("Automatic survey recovery failed")
                QMessageBox.warning(
                    self,
                    "Survey database",
                    f"The backup could not be restored: {exc}",
                )
                # A half-done restore has moved files about, so what to offer
                # next is decided from what is on disk now, not from the verdict
                # that led here.
                self._survey_health = health = inspect_survey_db(health.path)
                if health.openable:
                    self._after_recovery(
                        "The backup could not be restored, so this is a new "
                        "survey database. The previous one is kept beside it."
                    )
                    return
            else:
                self._after_recovery(message)
                return

        dialog = SurveyRecoveryDialog(health, out_root, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        option = dialog.selected()
        if option is None:
            return
        if option.kind is RecoveryKind.CHOOSE_FOLDER:
            self._browse_output_root()
            return
        try:
            message = apply_recovery(option, health, out_root)
        except Exception as exc:
            logger.exception("Survey recovery failed")
            QMessageBox.critical(
                self, "Survey database", f"That did not work: {exc}"
            )
            return
        self._after_recovery(message)

    def _after_recovery(self, message: str) -> None:
        """Take up the database recovery just put in place, and say what happened.

        The store was never opened, so there is nothing to close; dropping the
        verdict is what makes the next read re-inspect.
        """
        self._survey_health = None
        self._survey_store_obj = None
        self._activate_interface()
        self._status_label.setText(message)

    def _survey_db_health(self) -> SurveyDbHealth:
        """The last verdict, asking the database if nothing has looked yet."""
        health = getattr(self, "_survey_health", None)
        if health is None:
            self._try_survey_store()
            health = getattr(self, "_survey_health", None)
        assert health is not None
        return health
