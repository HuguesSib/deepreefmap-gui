"""Calibrate a camera profile from one clip, without leaving the app.

The library's COLMAP calibrator is one blocking call that reports nothing while
its three C++ stages run, so the dialog shows a real count while frames are
sampled and a named, indeterminate stage after that. Cancel is honoured between
stages, which the tooltip says. The result is reviewed (registered frames,
reprojection error, a raw|rectified preview) before it is kept: the profile is
calibrated into a staging folder and only Save moves it where runs find it.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from deepreefmap.camera.colmap_calibration import (
    CalibrationCancelled,
    CalibrationError,
    calibrate_camera_profile,
    verify_camera_profile,
)
from deepreefmap.camera.intrinsics import available_profile_names, validate_profile_name
from deepreefmap.paths import camera_profiles_dir
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

from deepreefmap_gui.core.widgets import muted_label, secondary_label
from deepreefmap_gui.survey.video_probe import probe_metadata

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
_VIDEO_FILTER = "Videos (*.mp4 *.MP4 *.mov *.MOV);;All files (*)"
_PREVIEW_WIDTH = 560


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
        self._build()
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
        window = QHBoxLayout()
        window.addWidget(self._begin)
        window.addWidget(QLabel("to"))
        window.addWidget(self._end)
        form.addRow("Window", window)
        root.addLayout(form)

        hint = secondary_label(PARALLAX_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)

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
        if meta.duration_s:
            notes.append(f"{meta.duration_s:.0f} s")
            self._end.setMaximum(float(meta.duration_s))
            self._begin.setMaximum(float(meta.duration_s))
        if meta.resolution:
            notes.append(meta.resolution)
        self._clip_note.setText(", ".join(notes))
        if not self._name.text().strip():
            self._name.setText(Path(text).stem.lower().replace(" ", "_")[:24])
        self._refresh_start_state()

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
                self._sig_done.emit(verify_camera_profile(name, output_dir=staging), None)
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
        self._stage.setText(STAGE_TEXT.get(stage, stage))
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        else:
            self._bar.setRange(0, 0)

    def _on_done(self, report: object, error: object) -> None:
        self._thread = None
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
