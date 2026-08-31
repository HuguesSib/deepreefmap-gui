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
from deepreefmap_gui.camera.calibration_dialog import CalibrationDialog, overall_fraction
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
        # The dialog opens the log in here before the calibrator runs, so the
        # real one finds the directory already made.
        previews.mkdir(exist_ok=True)
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


def test_the_stage_line_carries_the_count_colmap_reported(qapp):
    """A bar that only spins says nothing about a stage that runs for minutes."""
    assert CalibrationDialog._stage_text("extracting", 34, 100) == "Extracting features: 34 of 100"
    assert CalibrationDialog._stage_text("matching", 91, 100) == "Matching frames: 91 of 100"
    assert CalibrationDialog._stage_text("reconstructing", 12, 100) == "Reconstructing: 12 of 100 frames placed"
    assert CalibrationDialog._stage_text("diagnostics", 0, 0) == "Writing previews"


def test_the_log_records_the_run_and_survives_a_failure(qapp, monkeypatch, clip):
    _install_calibrator(monkeypatch, outcome="error")
    dialog = CalibrationDialog(None, initial_video=clip)
    dialog._name.setText("weak")

    dialog._start.click()
    _wait(qapp, dialog)

    kept = camera_profiles_dir() / module.LAST_LOG_NAME
    assert kept.is_file()
    assert "only 2 registered images" in kept.read_text(encoding="utf-8")


def test_a_kept_profile_keeps_its_log_beside_it(qapp, monkeypatch, clip):
    _install_calibrator(monkeypatch)
    dialog = CalibrationDialog(None, initial_video=clip)
    dialog._name.setText("hero_12")

    dialog._start.click()
    _wait(qapp, dialog)
    dialog._save.click()

    assert (camera_profiles_dir() / "hero_12_diagnostics" / module.LOG_NAME).is_file()


def test_the_window_is_picked_by_watching_the_clip(qapp, monkeypatch, clip):
    """The same picker the clip library trims a pass with, so a calibration window
    is chosen by eye rather than typed as two numbers."""
    opened = {}

    class FakeScrub:
        def __init__(self, video, duration, begin, end, parent=None, **kwargs):
            opened.update(video=video, duration=duration, begin=begin, end=end)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def time_range(self):
            return 12.0, 48.0

    monkeypatch.setattr("deepreefmap_gui.form.video_scrub.VideoScrubDialog", FakeScrub)
    monkeypatch.setattr(module, "decoded_length", lambda _: (120.0, 30.0))
    dialog = CalibrationDialog(None, initial_video=clip)
    assert dialog._scrub.isEnabled(), "picking a clip must offer the picker"

    dialog._scrub.click()

    assert opened["duration"] == 120.0
    assert (dialog._begin.value(), dialog._end.value()) == (12.0, 48.0)


def test_the_picker_waits_for_a_clip_whose_length_is_known(qapp):
    dialog = CalibrationDialog(None)

    assert not dialog._scrub.isEnabled()
    assert "length" in dialog._scrub.toolTip()


def test_a_window_running_to_the_end_stays_saying_so(qapp, monkeypatch, clip):
    class FakeScrub:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def time_range(self):
            return 5.0, 120.0

    monkeypatch.setattr("deepreefmap_gui.form.video_scrub.VideoScrubDialog", FakeScrub)
    monkeypatch.setattr(module, "decoded_length", lambda _: (120.0, 30.0))
    dialog = CalibrationDialog(None, initial_video=clip)

    dialog._scrub.click()

    assert dialog._begin.value() == 5.0
    assert dialog._end.value() == 0.0
    assert dialog._end.text() == "end of clip"


def test_a_clip_the_container_cannot_read_is_decoded_for_its_length(qapp, monkeypatch, clip):
    """The container parser reads MP4 atoms and nothing else, so a clip from
    another camera would otherwise never offer the picker."""
    monkeypatch.setattr(module, "decoded_length", lambda _: (90.0, 25.0))

    dialog = CalibrationDialog(None, initial_video=clip)

    assert dialog._duration_s == 90.0
    assert dialog._scrub.isEnabled()
    assert "90 s" in dialog._clip_note.text()


def test_a_clip_nothing_can_measure_says_why_the_picker_is_off(qapp, monkeypatch, clip):
    monkeypatch.setattr(module, "decoded_length", lambda _: None)

    dialog = CalibrationDialog(None, initial_video=clip)

    assert not dialog._scrub.isEnabled()
    assert "length" in dialog._scrub.toolTip()


def test_the_overall_bar_runs_across_every_stage(qapp):
    """One bar per stage restarts five times and never says how far in the run is."""
    assert overall_fraction("sampling", 0, 100) == 0.0
    assert overall_fraction("sampling", 100, 100) == pytest.approx(0.10)
    assert overall_fraction("extracting", 50, 100) == pytest.approx(0.225)
    assert overall_fraction("matching", 100, 100) == pytest.approx(0.65)
    assert overall_fraction("diagnostics", 1, 1) == pytest.approx(1.0)
    assert overall_fraction("something else", 1, 1) == 0.0


def test_the_overall_bar_does_not_fall_back_when_a_reconstruction_is_dropped(qapp, monkeypatch, clip):
    """COLMAP discards a reconstruction it cannot grow and counts frames again
    from two. The stage bar follows it; the overall bar has still moved on."""
    monkeypatch.setattr(module, "decoded_length", lambda _: (120.0, 30.0))
    dialog = CalibrationDialog(None, initial_video=clip)

    dialog._on_progress("reconstructing", 40, 100)
    high = dialog._overall.value()
    dialog._on_progress("reconstructing", 2, 100)

    assert dialog._overall.value() == high
    assert dialog._bar.value() == 2
