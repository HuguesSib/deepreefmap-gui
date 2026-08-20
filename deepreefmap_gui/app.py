"""The window, and the process it runs in.

`DeepReefMapWindow` builds no feature of its own. It fuses the 21 mixins listed in its bases
(the feature-to-file table is in `deepreefmap_gui/__init__.py`), owns the frame they fill in
(splitters, the view bar, the central layout, the shortcuts), and shuts everything down in
`closeEvent`. Read `__init__` in order, not in parts: the form widgets are built first because
the toolbar, the settings dialog and Setup are all wired to them, and `_activate_interface`
is last because it populates every page and re-divides the splitter.

## Signals

Every `_sig_*` is declared here rather than in the mixin that emits it, because `Signal` has to be
a class attribute of a QObject subclass and `DeepReefMapWindow` is the only one in the fusion:
`MixinBase` is `object` at runtime, and PySide6 refuses a second QObject base. The protocol in
`core/window_protocol.py` restates them so mypy can see them from a mixin, and
`tests/core/test_window_protocol_sync.py` compares the two lists.

They exist because a worker thread cannot touch a widget. The pattern is the same everywhere: a
daemon thread does the slow thing (a download in `models/cache_ui.py`, a reconstruction or a run
load in `runs/loading.py`, a batch in `simple/batch.py`, a disk walk in `runs/browse.py`, a
release check in `update/version.py`) and emits; the connection made in `__init__` below delivers
it as a queued call on the GUI thread, where the `_on_*`/`_apply_*` slot in the owning mixin runs.
`QTimer.singleShot` from a worker thread would silently do nothing, so it is never the answer.

Two signals are deliberately not connected here. `_sig_qc_render_progress` and
`_sig_qc_render_done` are connected per export in `runs/results.py`, because their slots close
over a QProgressDialog that only exists for that render; `closeEvent` disconnects them before
releasing handles so a render still in flight cannot drive a widget Qt is about to destroy.

## launch()

Order matters more than the code suggests. The Wayland/OpenGL environment variables and the
`QSurfaceFormat` have to be set before the QApplication exists (VTK 9 needs a 3.2 core profile,
and macOS exposes nothing above 2.1 any other way), fonts and theme before the window, and the
SIGINT heartbeat after it, so Ctrl-C reaches the interpreter while `exec()` blocks in C++.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, cast

from deepreefmap.config.classes import ClassConfig
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QSurfaceFormat
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.widgets import confirm
from deepreefmap_gui.form.panel import FormPanelMixin
from deepreefmap_gui.models.cache_ui import ModelManagementMixin
from deepreefmap_gui.models.packs_ui import ModelLibraryMixin
from deepreefmap_gui.notify.center_ui import NotificationCenterMixin
from deepreefmap_gui.runs.browse import BrowseMixin
from deepreefmap_gui.runs.loading import RunLoadingMixin
from deepreefmap_gui.runs.past_runs import PastRunsMixin
from deepreefmap_gui.runs.progress import ProgressBarsMixin
from deepreefmap_gui.runs.progress_panel import ProgressPanel
from deepreefmap_gui.runs.results import ResultsMixin
from deepreefmap_gui.runs.videos import VideoLibraryMixin
from deepreefmap_gui.server.page_ui import ServerPageMixin
from deepreefmap_gui.simple.analysis import SimpleAnalysisMixin
from deepreefmap_gui.simple.batch import SimpleBatchMixin
from deepreefmap_gui.simple.machine import SimpleMachineMixin
from deepreefmap_gui.simple.mode import InterfaceShellMixin
from deepreefmap_gui.simple.plan import SimplePlanMixin
from deepreefmap_gui.simple.setup import SimpleSetupMixin
from deepreefmap_gui.storage.page import StorageMixin
from deepreefmap_gui.system.system_tab import SystemPanelMixin
from deepreefmap_gui.update.version import VersionCheckMixin
from deepreefmap_gui.viewer.controls import ViewerControlsMixin

logger = logging.getLogger(__name__)


class DeepReefMapWindow(
    QMainWindow,
    BrowseMixin,
    FormPanelMixin,
    InterfaceShellMixin,
    ModelLibraryMixin,
    ModelManagementMixin,
    NotificationCenterMixin,
    PastRunsMixin,
    ProgressBarsMixin,
    ResultsMixin,
    RunLoadingMixin,
    ServerPageMixin,
    SimpleAnalysisMixin,
    SimpleBatchMixin,
    SimpleMachineMixin,
    SimplePlanMixin,
    SimpleSetupMixin,
    StorageMixin,
    SystemPanelMixin,
    VideoLibraryMixin,
    ViewerControlsMixin,
    VersionCheckMixin,
):
    _sig_update_check_done = Signal(str, object, object)
    _sig_model_status_done = Signal(object, object)
    _sig_status_text = Signal(str)
    _sig_hf_auth_done = Signal(object, str)
    _sig_download_progress = Signal(str, int)
    # Byte counts, so qint64: a 15 GB pack overflows Qt's 32-bit int.
    _sig_pack_progress = Signal(str, str, "qint64", "qint64")  # type: ignore[arg-type]
    _sig_pack_done = Signal(bool, str)
    _sig_run_loaded = Signal(object, str, str, int)
    _sig_load_progress = Signal(str, int, int)
    _sig_scene_file_done = Signal()
    _sig_qc_render_progress = Signal(int, int)
    _sig_qc_render_done = Signal(bool, str)
    _sig_discovery_done = Signal(object, object)
    _sig_survey_progress = Signal(int, int, str)
    # One-based pass index and what it really cost, so the session estimate is
    # corrected by the batch it is estimating rather than only by past ones.
    _sig_survey_pass_done = Signal(int, float)
    _sig_survey_done = Signal(int, int, str)
    _sig_run_sizes_done = Signal(object)
    _sig_clip_links_done = Signal(object)
    _sig_videos_probed = Signal(object)
    _sig_storage_usage = Signal(object)
    _sig_storage_page = Signal(object)
    # Installed versions of the app, not the user's run data: the three storage
    # signals above measure drives, this one measures PyApp environments.
    _sig_envs_done = Signal(object)
    _sig_shortcut_done = Signal(object)
    # The graphics card, once counted. Everything that grades a run against this
    # machine opens before the answer exists, so each of them repaints on this.
    _sig_gpu_probe_done = Signal()
    # Output bytes per footage minute, measured by walking recent runs off
    # the GUI thread. Carries the root it measured, so a stale walk is dropped.
    _sig_footage_rate = Signal(object, object)
    # What a worker thread has to say, as data: a new kind of message should not
    # need a new signal here.
    _sig_notify = Signal(object)
    # The registry, off the GUI thread. Each pair carries the outcome and the
    # failure, one of them None: a sync fails as readily as it succeeds, and both
    # land in the same slot.
    _sig_enrol_done = Signal(object, object)
    _sig_sync_progress = Signal(str)
    _sig_sync_done = Signal(object, object)
    _sig_sync_badge = Signal(object)
    # The archive queue, on the same worker-and-signal shape as the sync. One
    # object rather than a pair: the slot tells a report from a Failure itself.
    _sig_archive_progress = Signal(str)
    _sig_archive_plan = Signal(object)
    _sig_archive_done = Signal(object)
    _sig_archive_states = Signal(object)

    def __init__(self, classes_config: ClassConfig, classes_path: Path | None) -> None:
        super().__init__()
        self._classes_config = classes_config
        self._classes_path = classes_path
        self._pipeline_thread: threading.Thread | None = None
        self._playback_timer = QTimer(self)
        self._playback_timer.timeout.connect(self._on_playback_tick)

        self._sig_update_check_done.connect(self._apply_update_check)
        self._sig_shortcut_done.connect(self._on_shortcut_done)
        self._sig_envs_done.connect(self._apply_envs)
        self._sig_model_status_done.connect(self._apply_model_status)
        # The lambda is load-bearing, not noise: _status_label is built later, by
        # _build_form_widgets(). Binding self._status_label.setText here would
        # resolve the attribute at connect time and raise AttributeError.
        self._sig_status_text.connect(lambda t: self._status_label.setText(t))  # noqa: PLW0108
        self._sig_hf_auth_done.connect(self._on_hf_auth_done)
        self._sig_download_progress.connect(self._on_download_progress)
        self._sig_pack_progress.connect(self._on_pack_progress)
        self._sig_pack_done.connect(self._on_pack_done)
        self._sig_run_loaded.connect(self._apply_loaded_run)
        self._sig_load_progress.connect(self._on_load_progress)
        self._sig_scene_file_done.connect(self._on_scene_file_done)
        self._sig_discovery_done.connect(self._on_discovery_done)
        self._sig_survey_progress.connect(self._on_survey_progress)
        self._sig_survey_pass_done.connect(self._on_survey_pass_done)
        self._sig_survey_done.connect(self._on_survey_done)
        self._sig_run_sizes_done.connect(self._apply_run_sizes)
        self._sig_clip_links_done.connect(self._apply_clip_link_states)
        self._sig_videos_probed.connect(self._on_videos_probed)
        self._sig_storage_usage.connect(self._apply_storage_usage)
        self._sig_storage_page.connect(self._apply_storage_page_scan)
        self._sig_notify.connect(self._notify_post)
        self._sig_enrol_done.connect(self._on_enrol_done)
        self._sig_sync_progress.connect(self._on_sync_progress)
        self._sig_sync_done.connect(self._on_sync_done)
        self._sig_sync_badge.connect(self._apply_sync_badge)
        self._sig_archive_progress.connect(self._on_archive_progress)
        self._sig_archive_plan.connect(self._on_archive_plan_ready)
        self._sig_archive_done.connect(self._on_archive_done)
        self._sig_archive_states.connect(self._apply_archive_states)
        self._sig_gpu_probe_done.connect(self._on_gpu_probe_done)
        self._sig_footage_rate.connect(self._on_footage_rate)

        self.setWindowTitle("DeepReefMap")
        # Open at ~90% of the available screen, capped at the comfortable
        # 1400x900 target. Small laptops / low resolutions then get a window
        # that fits their screen instead of one clipped by the panels or the
        # taskbar. availableGeometry excludes docks and taskbars.
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            init_w = min(1400, int(avail.width() * 0.9))
            init_h = min(900, int(avail.height() * 0.9))
        else:
            init_w, init_h = 1400, 900
        self.resize(init_w, init_h)
        # Explicit floor so the window can always be shrunk back after the user
        # enlarges it. Without this, Qt's computed minimumSize follows whichever
        # child currently sizes the widest, and the window gets stuck at that
        # width.
        self.setMinimumSize(720, 480)

        from deepreefmap_gui.viewer.point_cloud import QtPointCloudViewer

        self._viewer = QtPointCloudViewer(
            class_colors=classes_config.id_to_color,
            class_names=classes_config.id_to_name,
        )
        self._viewer.set_status_callback(self._on_viewer_status)
        self._progress_panel = ProgressPanel()
        self._viewer.set_placeholder_widget(self._progress_panel)
        self._viewer.point_picked.connect(self._on_point_picked)
        self._viewer.point_picked_clear.connect(self._on_point_picked_clear)
        self._viewer.canvas_resized.connect(self._on_canvas_resized)
        self._viewer.frustum_picked.connect(self._on_frustum_picked)
        self._pick_card = None
        self._last_pick_payload = None
        self._pick_card_pinned_pos: tuple[int, int] | None = None
        self._build_pick_mode_overlay()

        # Build the form widgets first: they are what the toolbar, the settings
        # dialog and Setup are all wired to, and none of them can be laid
        # out before the widgets they reference exist.
        self._build_form_widgets()
        self._capture_form_defaults()
        log_panel = self._build_log_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_simple_shell())
        splitter.addWidget(self._viewer)
        # The shell takes the window and the viewer nothing, until View mode
        # opens a run. _update_work_area owns the division from then on; this is
        # only the state before it has been asked anything.
        splitter.setSizes([self.width(), 0])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)
        self._work_hsplitter = splitter

        # Vertical splitter places the live log as a togglable section at the
        # bottom of the window, alongside the form + 3D viewer above it. Hidden
        # initially; the Log button drives visibility.
        self._central_vsplitter = QSplitter(Qt.Orientation.Vertical)
        self._central_vsplitter.addWidget(splitter)
        self._central_vsplitter.addWidget(log_panel)
        # Log takes ~a quarter of the height when shown, proportional to the
        # window so it doesn't dwarf a short window or vanish in a tall one.
        _log_h = max(180, self.height() // 4)
        self._central_vsplitter.setSizes([self.height() - _log_h, _log_h])
        self._central_vsplitter.setStretchFactor(0, 1)
        self._central_vsplitter.setStretchFactor(1, 0)
        self._central_vsplitter.setChildrenCollapsible(True)
        self._central_vsplitter.setHandleWidth(6)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        # Spans the window rather than riding in the simple shell, which View
        # mode squeezes to nothing to give the viewport the full width.
        central_layout.addWidget(self._view_bar)
        central_layout.addWidget(self._central_vsplitter, 1)
        # Status and progress span the whole window under everything else, so
        # they read the same from every section and survive log-panel resizing.
        central_layout.addWidget(self._build_bottom_bar())
        self.setCentralWidget(central)

        # Last: it populates every page and re-divides the splitter, so the
        # whole window has to exist first. Guarded because it applies the stored
        # preset, whose content came from a registry: no settings bag, however
        # wrong, may be able to stop this window being constructed.
        try:
            self._activate_interface()
        except Exception:
            logger.exception("Could not populate the interface from stored settings")
        self._install_shortcuts()

    def start_gpu_probe(self) -> None:
        """Count the graphics card on a worker thread, and repaint when it lands.

        Importing torch and asking it how many devices there are is by far the
        most expensive thing this app does before it can be used: half a second
        for the import, and seconds again for the first enumeration, because the
        GPU driver initialises inside it (3.3 s on the ROCm build). Everything
        that reads the answer copes with not having it, so none of it is worth a
        window that is not on screen yet.

        Called once the event loop is running, never from __init__: torch's
        import is Python bytecode holding the GIL, so a probe started during
        construction starves the very build it was moved off.
        """
        from deepreefmap_gui.profiling.system_probe import start_gpu_probe

        self._paint_gpu_indicator(None)

        def done(_info: object) -> None:
            try:
                self._sig_gpu_probe_done.emit()
            except RuntimeError:
                pass  # window destroyed while the probe ran

        start_gpu_probe(done)

    def _on_gpu_probe_done(self) -> None:
        """Repaint everything that graded this machine before the card was known."""
        from deepreefmap_gui.profiling.system_probe import probe_system

        self._paint_gpu_indicator(probe_system(wait_for_gpu=False).gpu)
        # _update_memory_profile_warning carries the readiness view and the Setup
        # button with it; the other two are the cart's own grades and its gate.
        # Nothing is posted to the bell from here: a machine that cannot process
        # is a condition, and it already reaches the bell through
        # _machine_verdict -> conditions_from_state -> reconcile, which
        # deduplicates it across launches and clears it when a card turns up.
        self._update_memory_profile_warning()
        self._refresh_settings_cells()
        self._recompute_survey_start()

    def _install_shortcuts(self) -> None:
        """Keyboard routes to the destinations that otherwise need a mouse.

        The app is driven on a laptop trackpad, which makes a keyboard route to
        the things you reach constantly worth having. Deliberately few: one per
        destination, no chords, nothing that shadows a text field's own keys.

        QAction on the window rather than QShortcut so the binding survives focus
        moving into a child, and so a menu bar can adopt them later without the
        bindings being defined twice.
        """
        from PySide6.QtGui import QAction, QKeySequence

        bindings = (
            ("Show or hide the log", "Ctrl+L", self._log_toggle_btn.click),
            ("Go to Browse", "Ctrl+B", self._activate_browse),
            ("Run settings", "Ctrl+,", self._activate_settings),
            ("Setup", "F1", self._activate_machine),
            ("Quit", QKeySequence.StandardKey.Quit, self.close),
        )
        for name, key, slot in bindings:
            action = QAction(name, self)
            action.setShortcut(QKeySequence(key) if isinstance(key, str) else key)
            action.triggered.connect(slot)
            self.addAction(action)

    def _activate_browse(self) -> None:
        self._set_simple_section("browse")

    def _activate_settings(self) -> None:
        self._on_edit_run_settings()

    def _activate_machine(self) -> None:
        self._set_simple_section("machine")

    def eventFilter(self, obj, event):
        # QObject owns eventFilter earlier in the MRO than the mixins, so the
        # drop handling lives here on the concrete window and delegates in.
        if self._data_drop_event_filter(obj, event):
            return True
        if self._navigation_event_filter(obj, event):
            return True
        # Observes rather than consumes: the splitter still has to lay itself
        # out, this only re-divides it afterwards.
        self._data_split_event_filter(obj, event)
        self._video_split_event_filter(obj, event)
        self._plan_split_event_filter(obj, event)
        self._view_bar_event_filter(obj, event)
        return super().eventFilter(obj, event)

    # --- teardown -----------------------------------------------------------

    def _stop_window_timers(self) -> None:
        """Stop the timers this window owns.

        Qt destroys them with their parent, but destruction happens after the
        close returns; a tick landing in between runs a slot against widgets
        that are already going away.
        """
        for attr in (
            "_playback_timer",
            "_sys_timer",
            "_status_tick_timer",
            "_data_refresh_timer",
            "_out_root_commit_timer",
            "_storage_timer",
        ):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    logger.debug("Timer %s was already destroyed", attr, exc_info=True)

    def _release_handles(self) -> None:
        """Close what Qt's parent-child ownership does not cover.

        Three handles are held by plain Python attributes rather than QObjects,
        so nothing releases them when the window is destroyed. Two hold an OS
        resource outright: the survey store is a live SQLite connection and the
        run log is an open file.

        The frame accessor holds none, now that the scene file is closed before
        load_scene_file returns and the pixels are read per-frame from the run
        directory. It is still closed here because close() is part of the
        FrameAccessor protocol and this is the only place a window-held accessor
        would be released; an implementation backed by an archive rather than a
        directory would need it.
        """
        accessor = getattr(self, "_scene_accessor", None)
        if accessor is not None:
            accessor.close()
            self._scene_accessor = None

        store = getattr(self, "_survey_store_obj", None)
        if store is not None:
            try:
                store.close()
            except Exception:
                logger.debug("Survey store did not close cleanly", exc_info=True)
            self._survey_store_obj = None

        # The pass that was running owns this and closes it when it unwinds, but
        # the window can go first. A file handler left on the root logger keeps
        # writing into a finished run's directory.
        handler = getattr(self, "_run_log_file_handler", None)
        if handler is not None:
            from deepreefmap_gui.system.log_view import close_run_log_file

            close_run_log_file(handler)
            self._run_log_file_handler = None

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # RunLoadingMixin._run_in_flight: the survey batch worker is what holds
        # _pipeline_thread, so this asks whether a batch is still processing.
        if self._run_in_flight():
            if not confirm(
                self,
                "Quit DeepReefMap",
                "A reconstruction is still running. Quitting stops it and the "
                "run will be incomplete.\n\nQuit anyway?",
            ):
                event.ignore()
                return
            # Signalled, not joined. The worker threads are daemons, and a join
            # on a mid-pipeline reconstruction would freeze the UI for minutes
            # at the point the user has just asked to leave. They are left to
            # die with the process, which is why the warning is logged.
            logger.warning("Quitting with a reconstruction in flight; it will be abandoned")

        for attr in ("_cancel_event", "_survey_cancel_event"):
            cancel = getattr(self, attr, None)
            if cancel is not None:
                cancel.set()
        # Set, not cleared: a worker parked on the pause gate has to be released
        # before it can observe the cancel it was just given.
        pause = getattr(self, "_pause_event", None)
        if pause is not None:
            pause.set()

        self._stop_window_timers()
        # Before the handles go: these close over a QProgressDialog parented to
        # this window, so a render still in flight would drive a widget Qt is
        # about to destroy.
        self._disconnect_qc_render_handlers()
        self._release_handles()
        super().closeEvent(event)



def prefer_portal_file_dialogs() -> None:
    # QFileDialog only draws a native dialog when a platform theme plugin offers
    # one. The PySide6 wheel bundles its own Qt and ships no desktop-specific
    # theme, so Qt finds nothing on KDE/GNOME/XFCE alike and falls back to its
    # own picker. The portal theme is bundled, and it degrades to that same
    # picker when no xdg-desktop-portal is running.
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("QT_QPA_PLATFORMTHEME"):
        return
    import PySide6

    themes = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platformthemes"
    # Absent under a distro-packaged PySide6, which uses the system Qt plugins
    # and so already has a working native theme.
    if not (themes / "libqxdgdesktopportal.so").exists():
        return
    os.environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"


def _install_crash_dialog() -> None:
    """Route uncaught exceptions to a dialog as well as the log.

    Without this an exception that escapes a slot prints to stderr and no
    further. From a desktop launcher that is the session journal; in the
    packaged Windows build bootstrap points stderr at os.devnull, so the app
    misbehaves in complete silence. A field laptop has to be able to report what
    went wrong without someone starting it from a terminal.
    """

    def show(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is None:
            return
        try:
            box = QMessageBox(QMessageBox.Icon.Critical, "DeepReefMap", str(exc) or exc_type.__name__)
            box.setInformativeText(
                "The app hit a problem it did not expect. It may keep working; if "
                "it does not, restart it and send the details below."
            )
            box.setDetailedText("".join(traceback.format_exception(exc_type, exc, tb)))
            box.exec()
        except Exception:
            logger.exception("Could not show the crash dialog")

    sys.excepthook = show
    # A worker thread dying otherwise leaves no trace at all: threading prints
    # to stderr, which in the packaged build goes nowhere.
    threading.excepthook = lambda args: show(
        args.exc_type, args.exc_value or args.exc_type(), args.exc_traceback
    )


def launch(classes_path: Path | None = None, view_run_dir: Path | None = None) -> None:
    from deepreefmap.config.classes import load_classes

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # After basicConfig: the root StreamHandler keeps the real stderr, so
    # redirected writes can't loop back through it.
    from deepreefmap_gui.system.log_view import redirect_std_streams_to_logging

    redirect_std_streams_to_logging()
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ.setdefault("QT_OPENGL", "desktop")
    prefer_portal_file_dialogs()
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    # VTK 9's OpenGL2 backend needs >=3.2 core. macOS only exposes >2.1 through a
    # forward-compatible core profile, which Qt sets for a core-profile request.
    fmt.setVersion(3, 2)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setApplicationName("DeepReefMap")
    QApplication.setApplicationDisplayName("DeepReefMap")
    qt_app = cast(QApplication, QApplication.instance() or QApplication(sys.argv))
    from deepreefmap_gui.core.fonts import apply_app_fonts
    from deepreefmap_gui.core.theme import apply_theme

    apply_app_fonts(qt_app)
    apply_theme(qt_app)
    from importlib import resources
    icon_path = resources.files("deepreefmap_gui.resources").joinpath("icon.png")
    qt_app.setWindowIcon(QIcon(str(icon_path)))
    _install_crash_dialog()
    classes_config = load_classes(classes_path) if classes_path is not None else load_classes()
    window = DeepReefMapWindow(classes_config, classes_path)
    window.show()
    # Queued, so the window is on screen and painted first, and bound to the
    # window so closing it before the timer fires drops the callback.
    QTimer.singleShot(0, window, window.start_gpu_probe)
    window.check_survey_database()
    if view_run_dir is not None:
        QTimer.singleShot(100, lambda: window._auto_load_run(view_run_dir))

    # Qt's exec() blocks in C++, so Python's SIGINT handler can't fire until
    # the event loop yields. A no-op timer wakes the interpreter every 200 ms.
    # Parent it to qt_app so PySide6 won't garbage-collect the C++ side.
    _sigint_count = 0

    def _on_sigint(*_: object) -> None:
        nonlocal _sigint_count
        _sigint_count += 1
        if _sigint_count >= 2:
            os._exit(1)
        qt_app.closeAllWindows()
        qt_app.quit()

    signal.signal(signal.SIGINT, _on_sigint)
    sigint_heartbeat = QTimer(qt_app)
    sigint_heartbeat.start(200)
    sigint_heartbeat.timeout.connect(lambda: None)

    sys.exit(qt_app.exec())
