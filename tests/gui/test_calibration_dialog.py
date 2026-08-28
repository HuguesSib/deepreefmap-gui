"""Scenario: a camera the bundled profiles do not cover is calibrated from a clip
inside the app, reviewed, and kept where runs find it.

Expected behaviour: the dialog drives the calibrator on a worker thread,
shows the review, and Save moves the profile out of staging into the user's
profile directory, where the form's combo picks it up. A failure is shown in
place, and a cancel or discard leaves nothing behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile
from PySide6.QtWidgets import QDialog

from deepreefmap_gui.camera import calibration_dialog as module
from deepreefmap_gui.camera.calibration import CalibrationError
from deepreefmap_gui.camera.calibration_dialog import CalibrationDialog
from deepreefmap_gui.camera.profiles import available_profile_names, camera_profiles_dir, save_profile

STAGES = ("sampling", "extracting", "matching", "reconstructing", "diagnostics")


def _fake_profile(name: str) -> CameraProfile:
    return CameraProfile(
        name=name,
        image_size=(64, 48),
        k=np.array([[60.0, 0.0, 32.0], [0.0, 60.0, 24.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="SIMPLE_RADIAL",
        radial={"fx": 60.0, "fy": 60.0, "cx": 32.0, "cy": 24.0, "k1": 0.01, "k2": 0.0},
        diagnostics={"n_input_frames": 12, "n_registered_images": 11, "mean_reprojection_error_px": 0.42},
    )


def _install_calibrator(monkeypatch, outcome: str = "ok") -> list[tuple[str, int, int]]:
    """Stand in for the calibrator: writes what a real run writes, or fails."""
    seen: list[tuple[str, int, int]] = []

    def calibrate(video, name, *, output_dir, progress_callback, **kwargs):
        for stage in STAGES:
            progress_callback(stage, 12 if stage == "sampling" else 0, 12 if stage == "sampling" else 0)
            seen.append((stage, 0, 0))
        if outcome == "error":
            raise CalibrationError("only 2 registered images out of 12")
        path = save_profile(_fake_profile(name), output_dir)
        previews = output_dir / f"{name}_diagnostics"
        previews.mkdir()
        (previews / "compare_000000.png").write_bytes(b"")
        return path

    monkeypatch.setattr(module, "calibrate_camera_profile", calibrate)
    return seen


def _wait(qapp, dialog: CalibrationDialog) -> None:
    for _ in range(200):
        qapp.processEvents()
        if not dialog.running():
            break
    qapp.processEvents()


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "GX010042.MP4"
    path.write_bytes(b"\0" * 64)
    return path


def test_a_kept_profile_lands_where_runs_find_it(qapp, monkeypatch, clip):
    _install_calibrator(monkeypatch)
    dialog = CalibrationDialog(None, initial_video=clip)
    assert dialog._name.text() == "gx010042"
    dialog._name.setText("hero_12")
    assert dialog._start.isEnabled()

    dialog._start.click()
    _wait(qapp, dialog)

    assert dialog._save.isVisible() or dialog._result.isVisibleTo(dialog)
    assert "Registered 11 of 12 frames" in dialog._summary.text()
    assert "0.42 px" in dialog._summary.text()
    assert (camera_profiles_dir() / ".staging" / "hero_12" / "hero_12.json").exists()

    dialog._save.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.saved_name == "hero_12"
    assert json.loads((camera_profiles_dir() / "hero_12.json").read_text())["name"] == "hero_12"
    assert (camera_profiles_dir() / "hero_12_diagnostics" / "compare_000000.png").exists()
    assert not (camera_profiles_dir() / ".staging" / "hero_12").exists()
    assert "hero_12" in available_profile_names()


def test_a_failed_calibration_is_shown_and_leaves_nothing(qapp, monkeypatch, clip):
    _install_calibrator(monkeypatch, outcome="error")
    dialog = CalibrationDialog(None, initial_video=clip)
    dialog._name.setText("weak")

    dialog._start.click()
    _wait(qapp, dialog)

    assert "2 registered images" in dialog._error.text()
    assert dialog._start.isVisibleTo(dialog)
    assert not (camera_profiles_dir() / ".staging" / "weak").exists()
    assert "weak" not in available_profile_names()


def test_discard_drops_the_staged_profile(qapp, monkeypatch, clip):
    _install_calibrator(monkeypatch)
    dialog = CalibrationDialog(None, initial_video=clip)
    dialog._name.setText("tried")
    dialog._start.click()
    _wait(qapp, dialog)

    dialog._discard.click()

    assert dialog.saved_name is None
    assert not (camera_profiles_dir() / ".staging").exists() or not any((camera_profiles_dir() / ".staging").iterdir())
    assert "tried" not in available_profile_names()


def test_cancel_is_honoured_between_stages(qapp, monkeypatch, clip):
    reached: list[str] = []
    dialog = CalibrationDialog(None, initial_video=clip)

    def calibrate(video, name, *, output_dir, progress_callback, **kwargs):
        # Cancel lands while the first stage runs; the next report sees it.
        for stage in STAGES:
            progress_callback(stage, 0, 0)
            reached.append(stage)
            dialog._cancel.set()
        raise AssertionError("ran past a cancel")

    monkeypatch.setattr(module, "calibrate_camera_profile", calibrate)
    dialog._name.setText("halfway")
    dialog._start.click()
    _wait(qapp, dialog)

    assert reached == ["sampling"]
    assert dialog._start.isVisibleTo(dialog)
    assert dialog._error.text() == ""


def test_the_name_must_be_new_and_well_formed(qapp, clip):
    dialog = CalibrationDialog(None, initial_video=clip)
    dialog._name.setText("gopro_hero_10")
    assert not dialog._start.isEnabled()
    assert "already exists" in dialog._start.toolTip()
    dialog._name.setText("bad name!")
    assert not dialog._start.isEnabled()
    dialog._name.setText("hero_13")
    assert dialog._start.isEnabled()


def test_the_form_picks_up_a_profile_kept_by_the_dialog(window, monkeypatch, clip):
    _install_calibrator(monkeypatch)
    save_profile(_fake_profile("field_cam"), camera_profiles_dir())

    window._reload_camera_profiles(select="field_cam")

    assert window._profile_combo.currentText() == "field_cam"
    assert "gopro_hero_10" in [window._profile_combo.itemText(i) for i in range(window._profile_combo.count())]
