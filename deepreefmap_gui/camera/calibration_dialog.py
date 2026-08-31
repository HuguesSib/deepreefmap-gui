"""Calibrate a camera profile from one clip, without leaving the app.

COLMAP reports nothing while its three C++ stages run, so the dialog shows a
real count while frames are sampled and a named, indeterminate stage after that. Cancel is honoured between
stages, which the tooltip says. The result is reviewed (registered frames,
reprojection error, a raw|rectified preview) before it is kept: the profile is
calibrated into a staging folder and only Save moves it where runs find it.

Every run writes `calibration.log`: the window and sampling asked for, the
COLMAP calls made, what registered and what the camera came out as. It is kept
beside the profile when one is saved, and at `last-calibration.log` in the
profile directory either way, so a failure is still readable after the staging
folder is gone.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from deepreefmap.camera.intrinsics import validate_profile_name
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.camera.calibration import (
    CalibrationCancelled,
    CalibrationError,
    calibrate_camera_profile,
    verify_camera_profile,
)
from deepreefmap_gui.camera.profiles import available_profile_names, camera_profiles_dir
from deepreefmap_gui.core.widgets import muted_label, secondary_label
from deepreefmap_gui.io.video_length import decoded_length
from deepreefmap_gui.survey.video_probe import probe_metadata
from deepreefmap_gui.system.log_view import LogView, QtLogHandler, close_run_log_file, open_run_log_file

logger = logging.getLogger(__name__)

TITLE = "Calibrate camera"
PARALLAX_HINT = (
    "Pick a clip where the camera moves through the scene: COLMAP needs parallax, "
    "and a clip that mostly turns on the spot will not register."
)
STAGE_TEXT = {
    "sampling": "Sampling frames",
    "extracting": "Extracting features",
    "matching": "Matching frames",
    "reconstructing": "Reconstructing",
    "diagnostics": "Writing previews",
}
# Reconstruction counts frames it has placed rather than frames it has been
# through, so it is phrased as a tally and not as progress towards the total.
_TALLY_STAGES = ("reconstructing",)

# Roughly what each stage costs of a whole calibration, measured on a 100 frame
# run: sampling and the previews are quick, the three COLMAP stages are the wait.
# It only has to be close: the point is a bar that keeps moving forwards, not a
# prediction.
_STAGE_SHARE = {
    "sampling": 0.10,
    "extracting": 0.25,
    "matching": 0.30,
    "reconstructing": 0.30,
    "diagnostics": 0.05,
}
_OVERALL_TICKS = 1000
_VIDEO_FILTER = "Videos (*.mp4 *.MP4 *.mov *.MOV);;All files (*)"
LOG_NAME = "calibration.log"
LAST_LOG_NAME = "last-calibration.log"
_PREVIEW_WIDTH = 560


def overall_fraction(stage: str, done: int, total: int) -> float:
    """How far through the whole calibration a stage's own progress puts it.

    A stage nobody weighed counts as finished from the stages before it, which
    keeps an unknown message from dragging the bar backwards.
    """
    if stage not in _STAGE_SHARE:
        return 0.0
    before = 0.0
    for name, share in _STAGE_SHARE.items():
        if name == stage:
            within = (done / total) if total > 0 else 0.0
            return before + share * min(1.0, max(0.0, within))
        before += share
    return before


class CalibrationDialog(QDialog):
    """Clip in, reviewed profile out. ``saved_name`` is the profile kept, if any."""

    _sig_progress = Signal(str, int, int)
    _sig_done = Signal(object, object)

    def __init__(self, parent: QWidget | None = None, initial_video: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.saved_name: str | None = None
        self._staging: Path | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._duration_s = 0.0
        self._log_handler: logging.FileHandler | None = None
        self._qt_log: QtLogHandler | None = None
        self._build()
        self._refresh_scrub_state()
        self._sig_progress.connect(self._on_progress)
        self._sig_done.connect(self._on_done)
        if initial_video is not None:
            self._video.setText(str(initial_video))
            self._on_video_changed()

    # --- Layout ---

    def _build(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        self._video = QLineEdit()
        self._video.setPlaceholderText("A clip from the camera being calibrated")
        self._video.editingFinished.connect(self._on_video_changed)
        browse = QPushButton("Browse…")
        browse.setProperty("quiet", "true")
        browse.clicked.connect(self._on_browse)
        video_row = QHBoxLayout()
        video_row.addWidget(self._video, 1)
        video_row.addWidget(browse)
        form.addRow("Video", video_row)
        self._clip_note = muted_label("")
        form.addRow("", self._clip_note)

        self._name = QLineEdit()
        self._name.setPlaceholderText("gopro_hero_12")
        self._name.setToolTip("Letters, numbers, underscores and hyphens.")
        self._name.textChanged.connect(self._refresh_start_state)
        form.addRow("Profile name", self._name)

        self._frames = QSpinBox()
        self._frames.setRange(20, 400)
        self._frames.setValue(100)
        self._frames.setToolTip("Frames handed to COLMAP. More is slower and rarely better past 150.")
        form.addRow("Frames", self._frames)

        self._fps = QSpinBox()
        self._fps.setRange(1, 30)
        self._fps.setValue(10)
        self._fps.setToolTip("How many frames per second of clip are sampled.")
        form.addRow("Sampling fps", self._fps)

        self._begin = QDoubleSpinBox()
        self._begin.setRange(0.0, 24 * 3600.0)
        self._begin.setDecimals(1)
        self._begin.setSuffix(" s")
        self._end = QDoubleSpinBox()
        self._end.setRange(0.0, 24 * 3600.0)
        self._end.setDecimals(1)
        self._end.setSuffix(" s")
        self._end.setSpecialValueText("end of clip")
        self._end.setToolTip("Trim to the cleanest section. Zero means the end of the clip.")
        # The same picker the clip library trims a pass with: a window is chosen
        # by watching the footage, not by guessing at two numbers.
        self._scrub = QPushButton("Choose…")
        self._scrub.setProperty("quiet", "true")
        self._scrub.clicked.connect(self._on_scrub)
        window = QHBoxLayout()
        window.addWidget(self._begin)
        window.addWidget(QLabel("to"))
        window.addWidget(self._end)
        window.addWidget(self._scrub)
        form.addRow("Window", window)
        root.addLayout(form)

        hint = secondary_label(PARALLAX_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Two bars: the whole calibration, and the stage in hand. One alone is
        # either a bar that restarts five times or a bar that never says what is
        # happening.
        self._overall = QProgressBar()
        self._overall.setRange(0, _OVERALL_TICKS)
        self._overall.setValue(0)
        self._overall.setFormat("%p%")
        root.addWidget(self._overall)

        self._stage = muted_label("")
        root.addWidget(self._stage)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        root.addWidget(self._bar)

        self._result = QWidget()
        result = QVBoxLayout(self._result)
        result.setContentsMargins(0, 0, 0, 0)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        result.addWidget(self._summary)
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setToolTip("Raw frame on the left, rectified through the new profile on the right.")
        result.addWidget(self._preview)
        self._result.hide()
        root.addWidget(self._result)

        self._error = secondary_label("")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        # Folded away by default: it is read when something looks wrong, and the
        # dialog is already tall.
        self._log_toggle = QPushButton("Show log")
        self._log_toggle.setProperty("quiet", "true")
        self._log_toggle.setCheckable(True)
        self._log_toggle.toggled.connect(self._on_log_toggled)
        root.addWidget(self._log_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._log = LogView()
        self._log.setMinimumHeight(180)
        self._log.hide()
        root.addWidget(self._log, 1)

        self._buttons = QDialogButtonBox()
        self._start = self._buttons.addButton("Calibrate", QDialogButtonBox.ButtonRole.ActionRole)
        self._start.setDefault(True)
        self._start.clicked.connect(self._on_start)
        self._cancel_btn = self._buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.ActionRole)
        self._cancel_btn.setToolTip("Stops before the next stage. A stage already running finishes first.")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._save = self._buttons.addButton("Save profile", QDialogButtonBox.ButtonRole.AcceptRole)
        self._save.clicked.connect(self._on_save)
        self._discard = self._buttons.addButton("Discard", QDialogButtonBox.ButtonRole.RejectRole)
        self._discard.clicked.connect(self._on_discard)
        self._close = self._buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self._close.clicked.connect(self.reject)
        root.addWidget(self._buttons)
        self._show_idle()

    # --- States ---

    def _show_idle(self) -> None:
        self._result.hide()
        self._stage.setText("")
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        for button, shown in (
            (self._start, True),
            (self._cancel_btn, False),
            (self._save, False),
            (self._discard, False),
            (self._close, True),
        ):
            button.setVisible(shown)
        self._set_inputs_enabled(True)
        self._refresh_start_state()

    def _show_running(self) -> None:
        self._error.setText("")
        self._result.hide()
        for button, shown in (
            (self._start, False),
            (self._cancel_btn, True),
            (self._save, False),
            (self._discard, False),
            (self._close, False),
        ):
            button.setVisible(shown)
        self._cancel_btn.setEnabled(True)
        self._set_inputs_enabled(False)

    def _show_review(self, report: dict[str, object]) -> None:
        diagnostics = report.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        registered = diagnostics.get("n_registered_images")
        total = diagnostics.get("n_input_frames")
        error = diagnostics.get("mean_reprojection_error_px")
        size = report.get("image_size")
        parts = [f"Registered {registered} of {total} frames"]
        if isinstance(error, (int, float)):
            parts.append(f"mean reprojection error {error:.2f} px")
        if isinstance(size, (tuple, list)) and len(size) == 2:
            parts.append(f"{size[0]}x{size[1]}")
        self._summary.setText(". ".join(parts) + ".")
        previews = report.get("diagnostic_previews")
        compare = [p for p in previews if "compare_" in str(p)] if isinstance(previews, list) else []
        if compare:
            pixmap = QPixmap(str(compare[0]))
            if not pixmap.isNull():
                self._preview.setPixmap(
                    pixmap.scaledToWidth(_PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation)
                )
        self._stage.setText("")
        self._bar.setRange(0, 1)
        self._bar.setValue(1)
        self._result.show()
        for button, shown in (
            (self._start, False),
            (self._cancel_btn, False),
            (self._save, True),
            (self._discard, True),
            (self._close, False),
        ):
            button.setVisible(shown)
        self._save.setDefault(True)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (self._video, self._name, self._frames, self._fps, self._begin, self._end):
            widget.setEnabled(enabled)
        self._refresh_scrub_state(enabled)

    def _refresh_scrub_state(self, enabled: bool = True) -> None:
        """The picker needs a clip whose length is known, and says so when it has none."""
        ready = enabled and self._duration_s > 0.0
        self._scrub.setEnabled(ready)
        self._scrub.setToolTip(
            "Watch the clip and drag the two handles to the section to calibrate from."
            if ready
            else "Pick a clip whose length can be read first."
        )

    def _on_scrub(self) -> None:
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        video = Path(self._video.text().strip())
        if not video.is_file() or self._duration_s <= 0.0:
            return
        end = self._end.value() or self._duration_s
        dialog = VideoScrubDialog(video, self._duration_s, self._begin.value(), end, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        begin_s, end_s = dialog.time_range()
        self._begin.setValue(begin_s)
        # The end box reads its minimum as "end of clip", so a window that runs to
        # the end is left saying so rather than pinned to a duration that a
        # re-probe of the same file might read a frame differently.
        self._end.setValue(0.0 if end_s >= self._duration_s else end_s)

    def _refresh_start_state(self) -> None:
        problem = self._name_problem()
        self._start.setEnabled(problem is None and bool(self._video.text().strip()))
        self._start.setToolTip(problem or "")

    def _name_problem(self) -> str | None:
        name = self._name.text().strip()
        if not name:
            return "Name the profile."
        try:
            validate_profile_name(name)
        except ValueError as exc:
            return str(exc)
        if name in available_profile_names():
            return f"A profile named {name} already exists."
        return None

    # --- Inputs ---

    def _on_browse(self) -> None:
        start = self._video.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose a calibration clip", start, _VIDEO_FILTER)
        if path:
            self._video.setText(path)
            self._on_video_changed()

    def _on_video_changed(self) -> None:
        text = self._video.text().strip()
        if not text:
            self._clip_note.setText("")
            self._refresh_start_state()
            return
        meta = probe_metadata(Path(text))
        notes = []
        # The container is asked first because it costs a millisecond. A clip it
        # cannot read is decoded instead, or the window could never be picked by
        # watching it.
        duration = float(meta.duration_s or 0.0)
        if duration <= 0.0:
            decoded = decoded_length(text)
            duration = decoded[0] if decoded else 0.0
        self._duration_s = duration
        if duration > 0.0:
            notes.append(f"{duration:.0f} s")
            self._end.setMaximum(duration)
            self._begin.setMaximum(duration)
        if meta.resolution:
            notes.append(meta.resolution)
        self._clip_note.setText(", ".join(notes))
        if not self._name.text().strip():
            self._name.setText(Path(text).stem.lower().replace(" ", "_")[:24])
        self._refresh_start_state()
        self._refresh_scrub_state()

    def _start_log(self, diagnostics_dir: Path) -> None:
        """Send this run's lines to the panel and to a file beside the profile.

        The library's own calibration lines are wanted too, so the file handler
        goes on both trees, which is what `open_run_log_file` does.
        """
        self._log.clear()
        self._qt_log = QtLogHandler()
        self._qt_log.line_signal.connect(self._log.append_line)
        logging.getLogger("deepreefmap").addHandler(self._qt_log)
        logging.getLogger("deepreefmap_gui").addHandler(self._qt_log)
        try:
            self._log_handler = open_run_log_file(diagnostics_dir, filename=LOG_NAME)
        except OSError:
            logger.warning("Could not open a calibration log in %s", diagnostics_dir, exc_info=True)
            self._log_handler = None
        self._log.set_current_log_path(Path(self._log_handler.baseFilename) if self._log_handler else None)

    def _stop_log(self) -> None:
        """Detach the handlers, and leave the log where a dropped staging cannot take it."""
        for handler in (self._qt_log,):
            if handler is None:
                continue
            logging.getLogger("deepreefmap").removeHandler(handler)
            logging.getLogger("deepreefmap_gui").removeHandler(handler)
        self._qt_log = None
        if self._log_handler is None:
            return
        written = Path(self._log_handler.baseFilename)
        close_run_log_file(self._log_handler)
        self._log_handler = None
        kept = camera_profiles_dir() / LAST_LOG_NAME
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(written, kept)
        except OSError:
            logger.debug("Could not keep a copy of the calibration log", exc_info=True)
            return
        self._log.set_current_log_path(kept)

    def _on_log_toggled(self, shown: bool) -> None:
        self._log.setVisible(shown)
        self._log_toggle.setText("Hide log" if shown else "Show log")

    # --- The job ---

    def _on_start(self) -> None:
        video = Path(self._video.text().strip())
        if not video.is_file():
            self._error.setText(f"No such file: {video}")
            return
        name = self._name.text().strip()
        staging = camera_profiles_dir() / ".staging" / name
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        self._staging = staging
        self._cancel.clear()
        begin = float(self._begin.value()) or None
        end = float(self._end.value()) or None
        n_frames = int(self._frames.value())
        fps = int(self._fps.value())
        self._overall.setValue(0)
        self._start_log(staging / f"{name}_diagnostics")
        self._show_running()
        self._sig_progress.emit("sampling", 0, n_frames)

        def report(stage: str, done: int, total: int) -> None:
            if self._cancel.is_set():
                raise CalibrationCancelled
            self._sig_progress.emit(stage, done, total)

        def worker() -> None:
            try:
                calibrate_camera_profile(
                    video, name, n_frames=n_frames, fps=fps, begin_s=begin, end_s=end,
                    output_dir=staging, progress_callback=report,
                )
                self._sig_done.emit(verify_camera_profile(name, staging), None)
            except CalibrationCancelled:
                self._sig_done.emit(None, None)
            except CalibrationError as exc:
                self._sig_done.emit(None, str(exc))
            except Exception as exc:
                logger.exception("Calibration failed")
                self._sig_done.emit(None, f"{type(exc).__name__}: {exc}")

        self._thread = threading.Thread(target=worker, name="calibration", daemon=True)
        self._thread.start()

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        self._stage.setText(self._stage_text(stage, done, total))
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        else:
            self._bar.setRange(0, 0)
        # Never backwards: COLMAP drops a reconstruction it cannot grow and
        # starts the count again, which is progress through the stage even though
        # the tally fell.
        ticks = round(overall_fraction(stage, done, total) * _OVERALL_TICKS)
        self._overall.setValue(max(self._overall.value(), ticks))

    @staticmethod
    def _stage_text(stage: str, done: int, total: int) -> str:
        """The stage, and how far into it COLMAP says it is."""
        label = STAGE_TEXT.get(stage, stage)
        if total <= 0:
            return label
        if stage in _TALLY_STAGES:
            return f"{label}: {done} of {total} frames placed"
        return f"{label}: {done} of {total}"

    def _on_done(self, report: object, error: object) -> None:
        self._thread = None
        if error:
            logger.error("Calibration failed: %s", error)
        self._stop_log()
        if report is None:
            self._drop_staging()
            self._show_idle()
            self._error.setText(str(error) if error else "")
            return
        self._show_review(report)  # type: ignore[arg-type]

    def _on_cancel(self) -> None:
        self._cancel.set()
        self._cancel_btn.setEnabled(False)
        self._stage.setText("Stopping after this stage")

    # --- Keeping or dropping the result ---

    def _on_save(self) -> None:
        if self._staging is None:
            return
        name = self._staging.name
        home = camera_profiles_dir()
        home.mkdir(parents=True, exist_ok=True)
        for item in (f"{name}.json", f"{name}_diagnostics"):
            target = home / item
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(self._staging / item), str(target))
        self._drop_staging()
        self.saved_name = name
        self.accept()

    def _on_discard(self) -> None:
        self._drop_staging()
        self._show_idle()

    def _drop_staging(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None

    def running(self) -> bool:
        return self._thread is not None

    def reject(self) -> None:
        if self.running():
            self._on_cancel()
            return
        self._drop_staging()
        super().reject()
