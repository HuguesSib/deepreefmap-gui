"""Scenario: the Cameras view on Setup, where the profiles on this machine are read.

Expected behaviour: one card per profile, the ones calibrated here offering a
delete and the bundled ones not, and a delete that takes the profile off the list.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile
from PySide6.QtWidgets import QPushButton

from deepreefmap_gui.camera import page_ui as module
from deepreefmap_gui.camera.page_ui import BUNDLED, CALIBRATED, CameraProfilesPanel
from deepreefmap_gui.camera.profiles import camera_profiles_dir, profile_payload, save_profile


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
    # What a calibration leaves beside the profile, and what tells it apart from
    # a file imported off a USB stick.
    (camera_profiles_dir() / "field_cam_diagnostics").mkdir(parents=True, exist_ok=True)

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


def test_a_profile_is_exported_as_the_file_another_laptop_imports(panel, monkeypatch, tmp_path):
    save_profile(_profile("field_cam"), camera_profiles_dir())
    panel.refresh()
    target = tmp_path / "field_cam.json"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))

    [b for b in panel.findChildren(QPushButton) if b.text() == "Export…"][0].click()

    assert json.loads(target.read_text())["name"] == "field_cam"


def test_an_imported_profile_appears_on_the_page(panel, monkeypatch, tmp_path):
    source = tmp_path / "hero12.json"
    source.write_text(json.dumps(profile_payload(_profile("hero12")), indent=2))
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), ""))
    seen = []
    panel.changed.connect(lambda: seen.append(True))

    panel._import.click()

    assert "hero12" in _text(panel)
    assert module.IMPORTED in _text(panel)
    assert seen == [True]


def test_a_colliding_import_takes_the_name_the_user_gives(panel, monkeypatch, tmp_path):
    save_profile(_profile("hero12"), camera_profiles_dir())
    theirs = _profile("hero12")
    theirs.k[0][0] = 2048.0
    source = tmp_path / "hero12.json"
    source.write_text(json.dumps(profile_payload(theirs), indent=2))
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), ""))
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *a, **k: ("hero12_dome", True))

    panel._import.click()

    assert "hero12_dome" in _text(panel)
    assert (camera_profiles_dir() / "hero12_dome.json").is_file()


def test_a_refused_rename_imports_nothing(panel, monkeypatch, tmp_path):
    save_profile(_profile("hero12"), camera_profiles_dir())
    theirs = _profile("hero12")
    theirs.k[0][0] = 2048.0
    source = tmp_path / "hero12.json"
    source.write_text(json.dumps(profile_payload(theirs), indent=2))
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), ""))
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *a, **k: ("", False))

    panel._import.click()

    assert len([e for e in module.list_profiles() if e.name.startswith("hero12")]) == 1


def test_a_file_that_is_not_a_profile_is_refused_with_a_message(panel, monkeypatch, tmp_path):
    junk = tmp_path / "notes.json"
    junk.write_text('{"hello": "world"}')
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(junk), ""))
    warned = []
    monkeypatch.setattr(module.QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[-1]))

    panel._import.click()

    assert warned and "not a camera profile" in warned[0]


def test_the_page_says_where_profiles_live_and_how_to_move_them(panel):
    assert str(camera_profiles_dir()) in _text(panel)
    assert "DEEPREEFMAP_CAMERA_PROFILES" in _text(panel)


def test_the_bundled_profile_offers_no_export(panel):
    """Exporting is for a calibration this laptop holds; the bundled one ships
    with every install already."""
    assert [b for b in panel.findChildren(QPushButton) if b.text() == "Export…"] == []


def test_publishing_is_offered_only_once_a_registry_is_enrolled(panel):
    """A control that publishes nowhere is worse than no control, which is how
    every other archive affordance in the app behaves."""
    save_profile(_profile("field_cam"), camera_profiles_dir())
    panel.refresh()

    assert [b for b in panel.findChildren(QPushButton) if b.text() == "Publish"] == []

    panel.set_server_connected(True)

    assert len([b for b in panel.findChildren(QPushButton) if b.text() == "Publish"]) == 2

    panel.set_server_connected(False)

    assert [b for b in panel.findChildren(QPushButton) if b.text() == "Publish"] == []


def test_a_bundled_profile_is_published_to_bring_the_registry_up_to_the_pipeline(panel):
    """The registry learns the profiles the pipeline ships from a laptop running
    it: a newer install is what knows what the current set is."""
    panel.set_server_connected(True)

    assert len([b for b in panel.findChildren(QPushButton) if b.text() == "Publish"]) == 1


def test_a_profile_the_registry_gave_this_laptop_is_neither_published_nor_deleted(panel):
    """It came from there; a copy sent back and a delete undone by the next pull
    are both noise."""
    save_profile(_profile("hero12_dome"), camera_profiles_dir())
    (camera_profiles_dir() / "hero12_dome.from-registry").write_text(
        "11111111-1111-4111-8111-111111111111", encoding="utf-8"
    )
    panel.set_server_connected(True)
    panel.refresh()

    buttons = [b.text() for b in panel.findChildren(QPushButton)]
    assert buttons.count("Publish") == 1, "the bundled one only"
    assert "Delete" not in buttons
    assert module.FROM_REGISTRY in _text(panel)


def test_what_the_registry_answered_is_said_on_the_page(panel):
    panel._on_published({"created": True, "version": 2}, None)
    assert "version 2" in panel._status.text()

    panel._on_published({"created": False, "version": 1}, None)
    assert "already holds" in panel._status.text()

    panel._on_published(None, "the registry is not answering")
    assert "not answering" in panel._status.text()
