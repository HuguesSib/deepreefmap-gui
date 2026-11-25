"""What a person knows about a clip that no probe can read."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QWidget,
)

from deepreefmap_gui.survey.models.video_asset import VIEWS, VideoAsset

RIG_POSITIONS = (("left", "Left"), ("centre", "Centre"), ("right", "Right"))
REVIEWS = (
    ("unreviewed", "Unreviewed"),
    ("usable", "Usable"),
    ("excluded", "Excluded"),
)


class ClipDetailsDialog(QDialog):
    """Camera, rig position, view, mounting and the review verdict on one clip."""

    def __init__(
        self,
        parent: QWidget | None,
        video: VideoAsset,
        note: str = "",
        known_cameras: Sequence[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Clip details: {video.file_name}")
        form = QFormLayout(self)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            form.addRow(hint)
        # Editable: the names already in the survey are offered so a rig keeps one
        # spelling per camera, and a new camera is still just typed in.
        self.camera_input = QComboBox()
        self.camera_input.setEditable(True)
        self.camera_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.camera_input.addItems(list(dict.fromkeys(known_cameras)))
        self.camera_input.setCurrentText(video.camera_label or "")
        if (line := self.camera_input.lineEdit()) is not None:
            line.setPlaceholderText("GoPro_3, cam1")
        self.camera_input.setToolTip("The camera's name on the rig, as the field team labels it.")
        form.addRow("Camera", self.camera_input)
        self.rig_input = QComboBox()
        self.rig_input.addItem("Not recorded", None)
        for code, label in RIG_POSITIONS:
            self.rig_input.addItem(label, code)
        self.rig_input.setCurrentIndex(max(0, self.rig_input.findData(video.rig_position)))
        self.rig_input.setToolTip("Where the camera sat relative to the diver.")
        form.addRow("Rig position", self.rig_input)
        self.view_input = QComboBox()
        for code, label in VIEWS:
            self.view_input.addItem(label, code)
        self.view_input.setCurrentIndex(max(0, self.view_input.findData(video.view)))
        self.view_input.setToolTip(
            "Which way the camera looks. Down-looking clips are described like the rest but never processed."
        )
        form.addRow("View", self.view_input)
        self.upside_down_input = QCheckBox("Mounted upside down")
        self.upside_down_input.setChecked(video.upside_down)
        form.addRow("", self.upside_down_input)
        self.review_input = QComboBox()
        for code, label in REVIEWS:
            self.review_input.addItem(label, code)
        self.review_input.setCurrentIndex(max(0, self.review_input.findData(video.review)))
        form.addRow("Review", self.review_input)
        self.notes_input = QPlainTextEdit(video.notes)
        form.addRow("Notes", self.notes_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def apply_to(self, video: VideoAsset) -> None:
        video.camera_label = self.camera_input.currentText().strip() or None
        video.rig_position = self.rig_input.currentData()
        video.view = str(self.view_input.currentData() or "oblique")
        video.upside_down = self.upside_down_input.isChecked()
        video.review = str(self.review_input.currentData() or "unreviewed")
        video.notes = self.notes_input.toPlainText().strip()
