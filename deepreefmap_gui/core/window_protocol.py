"""Declare the surface the DeepReefMapWindow mixins share, so mypy can resolve
cross-mixin `self._foo` references."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    import threading
    import uuid
    from pathlib import Path

    from deepreefmap.config.classes import ClassConfig
    from deepreefmap.pipeline.artifacts import SemanticPointCloud
    from deepreefmap.pointcloud.grid_ortho import OrthoGrid
    from PySide6.QtCore import QSettings, QTimer, Signal
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QGridLayout,
        QGroupBox,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QSlider,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QToolButton,
        QTreeWidget,
        QVBoxLayout,
        QWidget,
    )

    from deepreefmap_gui.core.spinner import BusySpinner, SpinnerStopButton
    from deepreefmap_gui.core.storage_bar import StorageBars
    from deepreefmap_gui.io.lazy_frames import FrameAccessor
    from deepreefmap_gui.map.slippy_map import SlippyMapWidget
    from deepreefmap_gui.models.packs_ui import PackProgressDialog
    from deepreefmap_gui.notify.center import NotificationCenter
    from deepreefmap_gui.notify.widgets import BellButton, NotificationPopover
    from deepreefmap_gui.profiling.batch_estimate import BatchPrediction
    from deepreefmap_gui.profiling.eta import RunEtaEstimator
    from deepreefmap_gui.runs.progress import ProgressModel, RunProgress
    from deepreefmap_gui.runs.run_table import RunTable
    from deepreefmap_gui.runs.sunburst import SunburstWidget
    from deepreefmap_gui.runs.timing_popup import TimingPopup
    from deepreefmap_gui.simple.batch import PassTable
    from deepreefmap_gui.simple.cart import CartButton
    from deepreefmap_gui.simple.section_state import SectionState
    from deepreefmap_gui.survey.health import SurveyDbHealth
    from deepreefmap_gui.survey.models import Notification, SurveyBatch
    from deepreefmap_gui.survey.preset import ActivePreset
    from deepreefmap_gui.survey.store import SurveyStore
    from deepreefmap_gui.system.log_view import LogView
    from deepreefmap_gui.viewer.pick_tooltip import PickCard
    from deepreefmap_gui.viewer.point_cloud import QtPointCloudViewer

    # QWidget, not QMainWindow: DeepReefMapWindow lists QMainWindow first among
    # its bases, so a QMainWindow base here breaks C3 linearisation.
    class MixinBase(QWidget):
        # --- plain state -------------------------------------------------
        _classes_config: ClassConfig
        _classes_path: Path | None
        _active_run_dir: Path | None
        _settings: QSettings
        _playback_timer: QTimer
        _pipeline_thread: threading.Thread | None
        # Set by the survey runner, read by the run controls and by closeEvent.
        # Both readers go through hasattr/getattr because it only exists while a
        # batch is in flight; declared so the three sites agree on its type.
        _pause_event: threading.Event
        _central_vsplitter: QSplitter
        _active_progress_model: ProgressModel | None
        _status_tick_timer: QTimer
        _status_base_text: str
        _status_count_text: str
        _status_phase_key: str | None
        _status_phase_started: float
        _active_run_manifest: dict | None
        _results_output_dir: Path | None
        _app_mode: str
        _survey_store_obj: SurveyStore | None
        _notify: NotificationCenter
        _notify_bell: BellButton
        _notify_popover: NotificationPopover | None
        _survey_rows: list
        # Which section Videos has picked out. Read by the navigation history,
        # which restores the selection as well as the page.
        _selected_pass_id: str | None
        _survey_preset: dict | None
        _active_preset: ActivePreset | None
        _survey_cancel_event: threading.Event | None
        _survey_worker_running: bool
        # Single-file, but declared anyway: each is assigned an empty literal in
        # its mixin, which mypy cannot infer an element type for on its own.
        _survey_transects: list
        _survey_batch: SurveyBatch | None
        # The queue's predicted cost, keyed on the shape of the queue that
        # produced it: recomputed on every row mutation, and it reads a file.
        _batch_prediction_cache: tuple[tuple, BatchPrediction] | None
        _survey_running_batch: SurveyBatch | None
        _cart_button: CartButton
        _analysis_covers: list
        _analysis_all_covers: list
        _analysis_provenance_label: QLabel
        _downloading: set[str]
        _download_cancel_requested: set[str]
        _download_errors: dict[str, str]
        _delete_armed: dict[str, QPushButton]
        _model_actions: dict[str, QWidget]
        _model_rows: dict[str, QWidget]
        _last_model_states: list
        _pack_progress_dialog: PackProgressDialog | None
        _run_warnings: list[str]

        # --- composite widgets / models ----------------------------------
        _viewer: QtPointCloudViewer
        _log_view: LogView
        _cover_sunburst: SunburstWidget
        _load_model: ProgressModel
        _recon_model: ProgressModel

        # --- ortho preview state -----------------------------------------
        _ortho_cloud: SemanticPointCloud | None
        _base_ortho_grid: OrthoGrid | None
        _current_ortho_grid: OrthoGrid | None
        _ortho_classes_config: ClassConfig | None

        # --- viewer / legend / pick state --------------------------------
        _pick_card: PickCard | None
        _last_pick_payload: dict | None
        _pick_card_pinned_pos: tuple[int, int] | None
        _overlay_controls_collapsed: bool
        _overlay_controls_run_ready: bool
        _legend_sort_mode: str
        _legend_sort_ascending: bool
        _legend_sort_connected: bool
        _legend_order_cache: list[int] | None
        _legend_toggles: dict[int, QCheckBox]
        _scene_accessor: FrameAccessor | None
        _run_log_file_handler: logging.FileHandler | None
        _available_releases: list[dict]
        _current_version_str: str

        # --- checkboxes --------------------------------------------------
        _refine_intrinsics_check: QCheckBox
        _require_gravity_check: QCheckBox
        _skip_seg_check: QCheckBox
        _tsdf_check: QCheckBox
        _update_show_all: QCheckBox

        # --- buttons -----------------------------------------------------
        _setup_shortcut_btn: QPushButton
        _setup_survey_btn: QPushButton
        _manage_models_btn: QPushButton
        _discover_btn: QPushButton
        # Built by the run form, read by the Models tab. Its readers keep their
        # hasattr guards: _refresh_model_status runs from a daemon thread and can
        # reach the Models tab before the form has built these.
        _seg_status_btn: QPushButton
        _map_status_btn: QPushButton
        _models_group: QGroupBox
        _hf_auth_btn: QPushButton
        _pause_btn: QPushButton
        _spinner_stop: SpinnerStopButton
        _gpu_indicator: QLabel
        _gpu_indicator_box: QWidget
        _gpu_spinner: BusySpinner
        _out_root_widget: QWidget
        _output_group: QGroupBox
        _results_empty: QWidget
        _update_btn: QPushButton

        # --- spin boxes --------------------------------------------------
        _batch_size_spin: QSpinBox
        _fps_spin: QSpinBox
        _grid_bins_spin: QSpinBox
        _proc_height_spin: QSpinBox
        _proc_width_spin: QSpinBox
        _rr_est_frames_spin: QSpinBox
        _scs_height_spin: QSpinBox
        _scs_width_spin: QSpinBox
        _crop_width: QDoubleSpinBox
        _results_crop_width: QDoubleSpinBox
        _results_transect_length: QDoubleSpinBox
        _rr_factor_spin: QDoubleSpinBox
        _rr_override_spin: QDoubleSpinBox

        # --- sliders -----------------------------------------------------
        _frame_slider: QSlider
        _results_crop_slider: QSlider
        _results_transect_slider: QSlider

        # --- labels ------------------------------------------------------
        _cover_label: QLabel
        _hf_auth_icon: QLabel
        _hf_auth_label: QLabel
        _ortho_rgb_preview: QLabel
        _ortho_seg_preview: QLabel
        _view_facts: QLabel
        _capacity_caption: QLabel
        _capacity_detail: QLabel
        _capacity_advice: QLabel
        # One graded pool each, keyed "ram" / "vram".
        _capacity_rows: dict[str, Any]
        _status_label: QLabel
        _model_cache_label: QLabel
        _update_status_label: QLabel
        _update_version_label: QLabel
        _warnings_label: QLabel
        _warnings_label_running: QLabel

        # --- run form, borrowed by the run settings dialog ------------------
        _setup_page: QWidget
        _env_list_container: QWidget
        _env_list_layout: QVBoxLayout

        # --- data section --------------------------------------------------
        _data_panel: QWidget
        _data_tree: QTreeWidget
        _data_run_table: RunTable
        _data_map: SlippyMapWidget
        _data_refresh_timer: QTimer
        _data_entries: list
        _data_store_ok: bool
        _run_size_cache: dict[str, int]
        _storage_bars: StorageBars
        _storage_scan_running: bool
        _storage_timer: QTimer
        _video_entries: list
        _clip_link_cache: dict[str, str]
        _run_size_stale: set[str]

        # --- storage page --------------------------------------------------
        _storage_root: str | None
        _storage_scan_id: int
        _storage_page_scanning: bool
        _storage_inventory: Any
        _storage_breakdowns: dict
        # (output root, measured bytes per footage minute); None until measured.
        _footage_rate_cache: tuple[Path, float | None] | None
        # The root a footage-rate walk is running for, so it is not started twice.
        _footage_rate_pending: Path | None

        # --- survey mode -------------------------------------------------
        _view_bar: QWidget
        _view_info_open: bool
        _survey_preset_label: QLabel
        _survey_start_btn: QPushButton
        _survey_results_btn: QPushButton
        # Setup: the panels it borrows from their homes, the slots it
        # lends them to, and the two advisories its header button reports.
        _models_page: QWidget
        _system_page: QWidget
        _updates_page: QWidget
        _machine_models_host: QWidget
        _machine_system_host: QWidget
        _machine_updates_host: QWidget
        _machine_out_root_host: QWidget
        _machine_nav_button: QToolButton
        _memory_advisory: str
        _update_available: str
        _sys_timer: QTimer
        _hf_auth_user: str | None
        _analysis_transect_combo: QComboBox

        # --- combos / line edits -----------------------------------------
        _map_combo: QComboBox
        _profile_combo: QComboBox
        _resolution_preset_combo: QComboBox
        _seg_combo: QComboBox
        _update_version_combo: QComboBox
        _out_root_input: QLineEdit
        _scs_checkpoint_input: QLineEdit

        # --- containers / layouts ----------------------------------------
        _crop_box: QGroupBox
        _results_group: QGroupBox
        _results_page: QWidget
        _models_grid: QGridLayout
        _simple_stack: QStackedWidget
        _work_hsplitter: QSplitter
        _run_progress: RunProgress
        _bottom_progress_bar: QProgressBar
        # Built by the cart, read by the progress mixin: the running pass reports
        # on its own row, and the stage breakdown is anchored to that row's cell.
        _survey_pass_table: PassTable
        _log_toggle_btn: QToolButton
        _eta_total_label: QLabel
        _eta: RunEtaEstimator | None
        _timing_popup: TimingPopup

        # --- signals (defined as class attrs on DeepReefMapWindow) --------
        _sig_update_check_done = Signal(str, object, object)
        _sig_model_status_done = Signal(object, object)
        _sig_status_text = Signal(str)
        _sig_hf_auth_done = Signal(object, str)
        _sig_download_progress = Signal(str, int)
        _sig_pack_progress = Signal(str, str, "qint64", "qint64")  # type: ignore[arg-type]
        _sig_pack_done = Signal(bool, str)
        _sig_run_loaded = Signal(object, str, str, int)
        _sig_load_progress = Signal(str, int, int)
        _sig_scene_file_done = Signal()
        _sig_qc_render_progress = Signal(int, int)
        _sig_qc_render_done = Signal(bool, str)
        _sig_discovery_done = Signal(object, object)
        _sig_survey_progress = Signal(int, int, str)
        _sig_survey_pass_done = Signal(int, float)
        _sig_survey_done = Signal(int, int, str)
        _sig_run_sizes_done = Signal(object)
        _sig_clip_links_done = Signal(object)
        _sig_videos_probed = Signal(object)
        _sig_storage_usage = Signal(object)
        _sig_storage_page = Signal(object)
        _sig_envs_done = Signal(object)
        _sig_shortcut_done = Signal(object)
        _sig_gpu_probe_done = Signal()
        _sig_footage_rate = Signal(object, object)
        _sig_notify = Signal(object)
        _sig_enrol_done = Signal(object, object)
        _sig_sync_progress = Signal(str)
        _sig_sync_done = Signal(object, object)
        _sig_sync_badge = Signal(object)
        _sig_archive_progress = Signal(str)
        _sig_archive_plan = Signal(object)
        _sig_archive_done = Signal(object)
        _sig_archive_states = Signal(object)

        # --- cross-mixin methods -----------------------------------------
        # Each is tagged with the mixin that defines it. The list is flat, and
        # tests/core/test_window_protocol_sync.py checks these declarations against
        # the mixins, so it is annotated in place rather than regrouped. Mixin to
        # file is tabled in the package docstring, deepreefmap_gui/__init__.py.
        def _add_run_warning(self, message: str) -> None: ...  # RunLoadingMixin
        def _add_video_paths(self, paths: list[str]) -> None: ...  # SimpleBatchMixin
        def _on_videos_probed(self, probed: list) -> None: ...  # SimpleBatchMixin
        def _apply_progress(  # ProgressBarsMixin
            self,
            phase_key: str,
            label: str,
            current: int = 0,
            total: int = 0,
            flush: bool = False,
        ) -> None: ...
        def _auto_load_run(self, run_dir: Path) -> None: ...  # RunLoadingMixin
        def _begin_progress(self, model: ProgressModel) -> None: ...  # ProgressBarsMixin
        def _progress_sinks(self) -> list: ...  # ProgressBarsMixin
        def _build_legend(self) -> None: ...  # ViewerControlsMixin
        def _build_model_status_button(  # ModelManagementMixin
            self, combo: QComboBox
        ) -> QPushButton: ...
        def _build_system_panel(self, layout: object) -> None: ...  # SystemPanelMixin
        def _refresh_recorded_runs(self) -> None: ...  # SystemPanelMixin
        def _refresh_system_gauges(self) -> None: ...  # SystemPanelMixin
        def _update_memory_profile_warning(self) -> None: ...  # FormPanelMixin
        def _paint_gpu_indicator(self, gpu: object) -> None: ...  # FormPanelMixin
        def _check_for_update(self) -> None: ...  # VersionCheckMixin
        def _measure_envs(self) -> None: ...  # VersionCheckMixin
        def _refresh_envs(self) -> None: ...  # VersionCheckMixin
        def _apply_envs(self, info: dict) -> None: ...  # VersionCheckMixin
        def _on_delete_environment(  # VersionCheckMixin
            self, path: str, version: str
        ) -> None: ...
        def _clear_run_warnings(self) -> None: ...  # RunLoadingMixin
        def _collect_run_settings(self) -> dict: ...  # FormPanelMixin
        def _update_gated_warning(self) -> None: ...  # FormPanelMixin
        def _gpu_only_mapper(self) -> str: ...  # FormPanelMixin
        def _refresh_model_status(self) -> None: ...  # ModelManagementMixin
        def _build_data_panel(self) -> QWidget: ...  # BrowseMixin
        def _refresh_section_state(self) -> None: ...  # InterfaceShellMixin
        def _refresh_browse_state(self) -> None: ...  # InterfaceShellMixin
        def _focus_browse_on_session(self, batch_id: uuid.UUID) -> None: ...  # BrowseMixin
        def _set_scope_transect(self, transect_id: uuid.UUID | None) -> None: ...  # BrowseMixin
        def _refresh_data_manager(self) -> None: ...  # BrowseMixin
        def _load_run_from_dir(self, path: Path) -> None: ...  # BrowseMixin
        def _build_video_library(self) -> QWidget: ...  # VideoLibraryMixin
        def _refresh_video_library(self, store=None) -> None: ...  # VideoLibraryMixin
        def _repair_video_identity(self, store) -> None: ...  # VideoLibraryMixin
        def _pass_in_current_cart(self, pass_id_str: object) -> bool: ...  # VideoLibraryMixin
        def _apply_clip_link_states(self, states: dict) -> None: ...  # VideoLibraryMixin
        def _refresh_storage_bars(self) -> None: ...  # FormPanelMixin
        def _apply_storage_usage(self, volumes: object) -> None: ...  # FormPanelMixin
        def _build_storage_page(self) -> QWidget: ...  # StorageMixin
        def _open_storage_page(self, root: str) -> None: ...  # StorageMixin
        def _refresh_storage_page(self) -> None: ...  # StorageMixin
        def _apply_storage_page_scan(self, payload: object) -> None: ...  # StorageMixin
        def _sync_storage_buttons(self) -> None: ...  # StorageMixin
        def _recheck_clip_link(self, video_id: str) -> None: ...  # VideoLibraryMixin
        def _set_storage_compact(self, running: bool) -> None: ...  # RunLoadingMixin
        def _request_data_refresh(self) -> None: ...  # BrowseMixin
        def _apply_run_sizes(self, sizes: dict) -> None: ...  # BrowseMixin
        def _clear_run_facts(self) -> None: ...  # InterfaceShellMixin
        def _refresh_run_warnings_view(self) -> None: ...  # ViewerControlsMixin
        def _required_model_names(self) -> set[str]: ...  # ModelManagementMixin
        def _reset_progress(self) -> None: ...  # ProgressBarsMixin
        def _snapshot_form_settings(self) -> dict[str, Any]: ...  # InterfaceShellMixin
        def _restore_form_settings(self, snapshot: dict[str, Any]) -> None: ...  # InterfaceShellMixin
        def _collect_preset_from_form(self) -> dict[str, Any]: ...  # InterfaceShellMixin
        def _populate_form_from_preset(self, preset: dict[str, Any]) -> None: ...  # InterfaceShellMixin
        def _adopt_form_as_preset(self) -> None: ...  # InterfaceShellMixin
        def _reload_active_preset(self) -> None: ...  # InterfaceShellMixin
        def _on_choose_server_preset(self) -> None: ...  # InterfaceShellMixin
        def _restore_standard_settings(self) -> None: ...  # InterfaceShellMixin
        def _survey_deviations(self) -> dict: ...  # InterfaceShellMixin
        def _on_edit_run_settings(self) -> None: ...  # SimpleBatchMixin
        def _build_bottom_bar(self) -> QWidget: ...  # FormPanelMixin
        def _render_status(self) -> None: ...  # ProgressBarsMixin
        def _end_run_controls(self) -> None: ...  # RunLoadingMixin
        def _begin_run_controls(self) -> None: ...  # RunLoadingMixin
        def _run_in_flight(self) -> bool: ...  # RunLoadingMixin
        def _reveal_legend_overlay(self) -> None: ...  # ViewerControlsMixin
        def _set_app_mode(self, mode: str) -> None: ...  # ViewerControlsMixin
        def _build_plan_page(self) -> QWidget: ...  # SimplePlanMixin
        def _build_simple_run_page(self) -> QWidget: ...  # SimpleBatchMixin
        def _build_analysis_page(self) -> QWidget: ...  # SimpleAnalysisMixin
        def _build_simple_shell(self) -> QWidget: ...  # InterfaceShellMixin
        def _build_readiness_view(self) -> QWidget: ...  # SimpleSetupMixin
        def _build_out_root_block(self) -> QWidget: ...  # SimpleSetupMixin
        def _build_machine_page(self) -> QWidget: ...  # SimpleMachineMixin
        def _build_machine_nav_button(self) -> QToolButton: ...  # SimpleMachineMixin
        def _build_server_page(self) -> QWidget: ...  # ServerPageMixin
        def _build_server_nav_button(self) -> QToolButton: ...  # ServerPageMixin
        def _refresh_server_page(self) -> None: ...  # ServerPageMixin
        def _refresh_sync_badge(self) -> None: ...  # ServerPageMixin
        def _archive_video(self, video_id: str) -> None: ...  # ServerPageMixin
        def _archive_run(self, run_id: object) -> None: ...  # ServerPageMixin
        def _refresh_archive_badges(self) -> None: ...  # ServerPageMixin
        def _maybe_refresh_archive_badges(self) -> None: ...  # ServerPageMixin
        def _apply_archive_states(self, states: object) -> None: ...  # ServerPageMixin
        def _archive_state_for_video(self, video_id: object) -> str | None: ...  # ServerPageMixin
        def _archive_state_for_run(self, run_id: object) -> str | None: ...  # ServerPageMixin
        def _paint_archive_badges(self) -> None: ...  # VideoLibraryMixin
        def _apply_sync_badge(self, state: object) -> None: ...  # ServerPageMixin
        def _on_sync_badge_clicked(self) -> None: ...  # ServerPageMixin
        def _host_machine_panels(self) -> None: ...  # SimpleMachineMixin
        def _machine_verdict(self) -> SectionState: ...  # SimpleMachineMixin
        def _refresh_activity_view(self) -> None: ...  # SimpleMachineMixin
        def _set_machine_view(self, view: str) -> None: ...  # SimpleMachineMixin
        def _build_notification_bell(self) -> BellButton: ...  # NotificationCenterMixin
        def _notify_post(self, payload: dict) -> Notification: ...  # NotificationCenterMixin
        def _refresh_notification_bell(self) -> None: ...  # NotificationCenterMixin
        def _rebind_notification_log(  # NotificationCenterMixin
            self, store: SurveyStore | None
        ) -> None: ...
        def _sync_system_gauges_running(self) -> None: ...  # SimpleMachineMixin
        def _refresh_machine_button(self) -> None: ...  # SimpleMachineMixin
        def _refresh_update_notice(self) -> None: ...  # SimpleMachineMixin
        def _refresh_readiness_view(self) -> None: ...  # SimpleSetupMixin
        def _on_footage_rate(self, out_root: object, rate: object) -> None: ...  # SimpleSetupMixin
        def _current_setup_checks(self) -> list: ...  # SimpleSetupMixin
        def _initial_simple_section(self) -> str: ...  # SimpleSetupMixin
        def _reveal_memory_detail(self) -> None: ...  # InterfaceShellMixin
        def _simple_peak_frames(self, fps: int) -> int | None: ...  # SimpleBatchMixin
        def _survey_missing_models(self) -> list[str]: ...  # SimpleBatchMixin
        def _download_model(self, model_name: str) -> None: ...  # ModelManagementMixin
        def _set_simple_section(self, name: str) -> None: ...  # InterfaceShellMixin
        def _current_section(self) -> str: ...  # InterfaceShellMixin
        def _set_log_panel_visible(self, visible: bool) -> None: ...  # RunFormPanelMixin
        def _table_row_of(self, model_index: int) -> int: ...  # SimpleBatchMixin
        def _running_table_row(self) -> int: ...  # SimpleBatchMixin
        def _on_queue_row_hover(self, table_row: int, global_rect) -> None: ...  # ProgressBarsMixin

        def _enter_view_mode(self, run_dir: Path) -> None: ...  # InterfaceShellMixin
        def _go_to_section(self, name: str) -> None: ...  # InterfaceShellMixin
        def _update_work_area(self) -> None: ...  # InterfaceShellMixin
        def _survey_store(self) -> SurveyStore: ...  # InterfaceShellMixin
        def _try_survey_store(self) -> SurveyStore | None: ...  # InterfaceShellMixin
        def _survey_db_health(self) -> SurveyDbHealth: ...  # InterfaceShellMixin
        def check_survey_database(self) -> None: ...  # InterfaceShellMixin
        def _browse_output_root(self) -> None: ...  # FormPanelMixin
        def _refresh_transect_list(self, select_id: uuid.UUID | None = None) -> None: ...  # SimplePlanMixin
        def _select_transect_row(self, id_str: str) -> None: ...  # SimplePlanMixin
        def _on_transect_selected(self) -> None: ...  # SimplePlanMixin
        def _open_transect_page(self, transect_id: object = None) -> None: ...  # SimplePlanMixin
        def _refresh_cart_marks(self) -> None: ...  # VideoLibraryMixin
        def _open_section_in_videos(self, pass_id: uuid.UUID) -> None: ...  # VideoLibraryMixin
        def _select_section(self, pass_id: str) -> bool: ...  # VideoLibraryMixin
        def _transect_name_for(self, transect_id: object) -> str | None: ...  # VideoLibraryMixin
        def _selected_transect_id(self) -> uuid.UUID | None: ...  # SimplePlanMixin
        def _refresh_survey_analysis(self) -> None: ...  # SimpleAnalysisMixin
        def _refresh_survey_batch_tab(self) -> None: ...  # SimpleBatchMixin
        def _add_pass_to_cart(self, pass_id: uuid.UUID) -> None: ...  # SimpleBatchMixin
        def _take_pass_out_of_cart(self, pass_id: uuid.UUID) -> None: ...  # SimpleBatchMixin
        def _cart_add(self, pass_id: uuid.UUID) -> None: ...  # SimpleBatchMixin
        def _refresh_survey_transect_names(self) -> None: ...  # SimpleBatchMixin
        def _rows_over_memory(self) -> int: ...  # SimpleBatchMixin
        def _refresh_settings_cells(self) -> None: ...  # SimpleBatchMixin
        def _refresh_survey_pass_statuses(self) -> None: ...  # SimpleBatchMixin
        def _recompute_survey_start(self) -> None: ...  # SimpleBatchMixin
        def _survey_preset_summary(self) -> str: ...  # SimpleBatchMixin
        def _on_survey_progress(self, index: int, total: int, name: str) -> None: ...  # SimpleBatchMixin
        def _on_survey_done(self, ok: int, total: int, last_error: str) -> None: ...  # SimpleBatchMixin
        def _set_ortho_sources(  # ResultsMixin
            self,
            cloud: SemanticPointCloud | None,
            base_grid: OrthoGrid | None,
            classes_config: ClassConfig | None,
        ) -> None: ...
        def _set_semantic_only_controls_visible(self, visible: bool) -> None: ...  # ViewerControlsMixin
        def _show_results(self, output_dir: str) -> None: ...  # ResultsMixin
        def _show_run_facts(self, manifest: dict) -> None: ...  # InterfaceShellMixin
        def _show_viewer_controls(self) -> None: ...  # ViewerControlsMixin
        def _set_overlay_controls_visible(self, visible: bool) -> None: ...  # ViewerControlsMixin

        # event handlers invoked across mixins
        def _on_discover_clicked(self) -> None: ...  # ModelManagementMixin
        def _on_export_cover_csv(self) -> None: ...  # ResultsMixin
        def _on_export_current_frame(self) -> None: ...  # ResultsMixin
        def _on_export_ortho_npz(self) -> None: ...  # ResultsMixin
        def _on_export_ortho_png(self) -> None: ...  # ResultsMixin
        def _on_export_qc_video(self) -> None: ...  # ResultsMixin
        def _on_export_zip(self) -> None: ...  # ResultsMixin
        def _on_hf_auth_button(self) -> None: ...  # ModelManagementMixin
        def _open_model_library(self) -> None: ...  # ModelLibraryMixin
        def _on_export_models(self) -> None: ...  # ModelLibraryMixin
        def _on_import_model_pack(self) -> None: ...  # ModelLibraryMixin
        def _on_pack_progress(  # ModelLibraryMixin
            self, phase: str, label: str, current: int, total: int
        ) -> None: ...
        def _on_pack_done(self, ok: bool, message: str) -> None: ...  # ModelLibraryMixin
        def _on_pause_toggled(self, paused: bool) -> None: ...  # RunLoadingMixin
        def _on_required_models_changed(self, _value: object = "") -> None: ...  # ModelManagementMixin
        def _on_results_crop_slider_changed(self, value: int) -> None: ...  # ResultsMixin
        def _on_results_crop_width_changed(self, value: float) -> None: ...  # ResultsMixin
        def _on_results_transect_length_changed(self, value: float) -> None: ...  # ResultsMixin
        def _on_results_transect_slider_changed(self, value: int) -> None: ...  # ResultsMixin
        def _on_stop_clicked(self) -> None: ...  # RunLoadingMixin
        def _on_sunburst_selection(self, class_ids: list) -> None: ...  # ViewerControlsMixin
        def _on_toggle_shortcut(self) -> None: ...  # SimpleSetupMixin
        def _on_toggle_show_all_versions(self, _checked: bool) -> None: ...  # VersionCheckMixin
        def _on_update(self) -> None: ...  # VersionCheckMixin
        def _on_viewer_control_changed(self) -> None: ...  # ViewerControlsMixin

        @staticmethod
        def _format_cover_html(cover: dict) -> str: ...  # ResultsMixin

        @staticmethod
        def _sanitize_run_name(name: str) -> str: ...  # FormPanelMixin


else:
    MixinBase = object
