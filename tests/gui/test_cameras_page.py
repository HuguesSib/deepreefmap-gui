"""Scenario: the Cameras view on Setup, where the profiles on this machine are read.

Expected behaviour: one card per profile, the ones calibrated here offering a
delete and the bundled ones not, and a delete that takes the profile off the list.
"""

from __future__ import annotations

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile
from PySide6.QtWidgets import QPushButton

from deepreefmap_gui.camera import page_ui as module
from deepreefmap_gui.camera.page_ui import BUNDLED, CALIBRATED, CameraProfilesPanel
from deepreefmap_gui.camera.profiles import camera_profiles_dir, save_profile


def _profile(name: str) -> CameraProfile:
    return CameraProfile(
        name=name,
        image_size=(1920, 1440),
        k=np.array([[1035.0, 0.0, 960.0], [0.0, 1035.0, 720.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="RADIAL",
        radial={"fx": 1035.0, "fy": 1035.0, "cx": 960.0, "cy": 720.0, "k1": 0.01, "k2": 0.0},
        diagnostics={
            "n_input_frames": 100,
            "n_registered_images": 78,
            "mean_reprojection_error_px": 0.62,
            "source_video": "GX010042.MP4",
        },
    )


def _text(panel: CameraProfilesPanel) -> str:
    return " ".join(label.text() for label in panel.findChildren(type(panel._location)))


def _delete_buttons(panel: CameraProfilesPanel) -> list[QPushButton]:
    return [b for b in panel.findChildren(QPushButton) if b.text() == "Delete"]


@pytest.fixture
def panel(qapp):
    widget = CameraProfilesPanel()
    yield widget
    widget.deleteLater()


def test_a_bundled_profile_is_listed_without_a_delete(panel):
    assert "gopro_hero_10" in _text(panel)
    assert BUNDLED in _text(panel)
    assert _delete_buttons(panel) == []


def test_a_calibrated_profile_states_the_clip_and_the_error(panel):
    save_profile(_profile("field_cam"), camera_profiles_dir())

    panel.refresh()

    shown = _text(panel)
    assert CALIBRATED in shown
    assert "GX010042.MP4" in shown
    assert "78 of 100 frames registered" in shown
    assert "0.62 px" in shown
    assert len(_delete_buttons(panel)) == 1


def test_deleting_a_profile_takes_it_off_the_page(panel, monkeypatch):
    save_profile(_profile("field_cam"), camera_profiles_dir())
    panel.refresh()
    monkeypatch.setattr(module, "confirm", lambda *args, **kwargs: True)
    seen = []
    panel.changed.connect(lambda: seen.append(True))

    _delete_buttons(panel)[0].click()

    assert "field_cam" not in _text(panel)
    assert not (camera_profiles_dir() / "field_cam.json").exists()
    assert seen == [True]


def test_a_refused_delete_leaves_the_profile_alone(panel, monkeypatch):
    save_profile(_profile("field_cam"), camera_profiles_dir())
    panel.refresh()
    monkeypatch.setattr(module, "confirm", lambda *args, **kwargs: False)

    _delete_buttons(panel)[0].click()

    assert (camera_profiles_dir() / "field_cam.json").exists()


def test_a_profile_with_a_log_offers_to_open_it(panel, monkeypatch):
    save_profile(_profile("field_cam"), camera_profiles_dir())
    diagnostics = camera_profiles_dir() / "field_cam_diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    (diagnostics / "calibration.log").write_text("Calibrating 'field_cam'\n", encoding="utf-8")
    panel.refresh()
    opened = []
    monkeypatch.setattr(module.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))

    log_buttons = [b for b in panel.findChildren(QPushButton) if b.text() == "Log"]
    log_buttons[0].click()

    assert opened == [str(diagnostics / "calibration.log")]


def test_a_profile_with_no_log_offers_no_button(panel):
    save_profile(_profile("field_cam"), camera_profiles_dir())

    panel.refresh()

    assert [b for b in panel.findChildren(QPushButton) if b.text() == "Log"] == []
