"""Scenario: a run is made with a profile calibrated on this laptop, and read
back somewhere else.

Expected behaviour: the run directory carries the calibration it used, so the
reconstruction can be reproduced and a curator can tell two profiles of the same
name apart. The reproduction script stages that copy where the library looks.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
from deepreefmap.camera.intrinsics import CameraProfile

from deepreefmap_gui.camera.profiles import RUN_PROFILE_NAME, camera_profiles_dir, copy_profile_into_run, save_profile
from deepreefmap_gui.runs.run_command import write_run_command_script


def _profile(name: str) -> CameraProfile:
    return CameraProfile(
        name=name,
        image_size=(1920, 1440),
        k=np.array([[1035.0, 0.0, 960.0], [0.0, 1035.0, 720.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="RADIAL",
        radial={"fx": 1035.0, "fy": 1035.0, "cx": 960.0, "cy": 720.0, "k1": 0.01, "k2": 0.0},
        diagnostics={"n_input_frames": 100},
    )


def test_the_run_carries_the_calibration_it_used(tmp_path):
    save_profile(_profile("field_cam"), camera_profiles_dir())
    run_dir = tmp_path / "20260901-101500"

    recorded = copy_profile_into_run("field_cam", run_dir)

    written = run_dir / RUN_PROFILE_NAME
    assert recorded["camera_profile_file"] == RUN_PROFILE_NAME
    assert json.loads(written.read_text())["name"] == "field_cam"
    assert json.loads(written.read_text())["rectified_pinhole"]["image_size"] == [1920, 1440]


def test_the_digest_is_of_the_bytes_on_disk(tmp_path):
    """A curator checks the file, not a value only this app can rebuild."""
    save_profile(_profile("field_cam"), camera_profiles_dir())
    run_dir = tmp_path / "run"

    recorded = copy_profile_into_run("field_cam", run_dir)

    on_disk = hashlib.sha256((run_dir / RUN_PROFILE_NAME).read_bytes()).hexdigest()
    assert recorded["camera_profile_sha256"] == on_disk


def test_a_profile_that_cannot_be_read_does_not_lose_the_run(tmp_path):
    run_dir = tmp_path / "run"

    assert copy_profile_into_run("no_such_profile", run_dir) == {}


def test_the_script_stages_the_profile_where_the_library_looks(tmp_path):
    """The CLI validates the name before the orchestrator and exits 1, and the
    library resolves it against a CWD-relative ./camera_profiles."""
    script = write_run_command_script(
        tmp_path, ["deepreefmap", "reconstruct", "--camera-profile", "field_cam"],
        camera_profile_name="field_cam",
    )

    body = script.read_text()
    assert "mkdir -p camera_profiles" in body
    assert f'cp -n "{RUN_PROFILE_NAME}" "camera_profiles/field_cam.json"' in body
    assert body.index("mkdir -p") < body.index("deepreefmap")


def test_a_run_with_no_recorded_profile_gets_no_preamble(tmp_path):
    script = write_run_command_script(tmp_path, ["deepreefmap", "reconstruct"])

    assert "camera_profiles" not in script.read_text()
