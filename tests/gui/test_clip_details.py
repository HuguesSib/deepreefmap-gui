"""The clip details dialog offers the camera names the survey already uses."""

import pytest
from _factories import make_video

from deepreefmap_gui.runs.clip_details import ClipDetailsDialog

pytestmark = pytest.mark.usefixtures("qapp")


def test_known_cameras_are_offered_and_the_clip_keeps_its_own():
    video = make_video(camera_label="Cam3")
    dialog = ClipDetailsDialog(None, video, known_cameras=["Cam1", "Cam2", "Cam3"])

    offered = [dialog.camera_input.itemText(i) for i in range(dialog.camera_input.count())]
    assert offered == ["Cam1", "Cam2", "Cam3"]
    assert dialog.camera_input.currentText() == "Cam3"


def test_a_new_camera_can_still_be_typed():
    video = make_video(camera_label=None)
    dialog = ClipDetailsDialog(None, video, known_cameras=["Cam1"])

    dialog.camera_input.setEditText("  Cam9  ")
    dialog.apply_to(video)

    assert video.camera_label == "Cam9"


def test_a_blank_camera_is_none():
    video = make_video(camera_label=None)
    dialog = ClipDetailsDialog(None, video, known_cameras=["Cam1"])

    dialog.camera_input.setEditText("")
    dialog.apply_to(video)

    assert video.camera_label is None


def test_a_clip_can_be_marked_as_looking_down():
    video = make_video()
    dialog = ClipDetailsDialog(None, video)
    assert dialog.view_input.currentData() == "oblique"

    dialog.view_input.setCurrentIndex(dialog.view_input.findData("down"))
    dialog.apply_to(video)

    assert video.view == "down"
