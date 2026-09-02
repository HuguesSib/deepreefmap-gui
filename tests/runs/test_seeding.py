"""Scenario: a run reuses the frames an earlier attempt at the same clip prepared.

Expected behaviour: only when the earlier attempt was rectified with the same
calibration. The library's preprocess key names the camera profile but not the
measurement behind it, so a registry rotation leaves the key matching while the
frames on disk no longer belong to it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile as LibraryProfile
from deepreefmap.pipeline import resume as resume_mod

from deepreefmap_gui.camera.profiles import camera_profiles_dir, copy_profile_into_run, save_profile
from deepreefmap_gui.io.classes_default import resolve_classes_path
from deepreefmap_gui.runs.seeding import preprocess_key_for_settings, seed_run_dir_from_match

PROFILE = "field_cam"


def profile(focal: float = 1035.0) -> LibraryProfile:
    return LibraryProfile(
        name=PROFILE,
        image_size=(1920, 1080),
        k=np.array(
            [[focal, 0.0, 960.0], [0.0, focal, 540.0], [0.0, 0.0, 1.0]], dtype=np.float32
        ),
        distorted_model="RADIAL",
        radial={"fx": focal, "fy": focal, "cx": 960.0, "cy": 540.0, "k1": 0.01, "k2": 0.0},
    )


@pytest.fixture
def settings() -> dict:
    return {
        "fps": 3,
        "camera_profile_name": PROFILE,
        "skip_segmentation": False,
        "segmentation_name": "segformer-b2",
        "classes_path": None,
        "processing_width": 640,
        "processing_height": 360,
    }


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not really a video")
    return path


def prior_run(root, video, settings, *, name: str = "20260101-000000", record: bool = True):
    """An earlier attempt at the same clip, with frames ready to be reused."""
    run = root / name
    for dirname in ("frames", "labels", "masks"):
        (run / dirname).mkdir(parents=True)
        (run / dirname / "000000.png").write_bytes(b"data")
    key = preprocess_key_for_settings(settings, [video], None, None)
    resume_mod.write_sidecar(run, resume_mod.STAGE_PREPROCESS, key)
    if record:
        copy_profile_into_run(PROFILE, run)
    return run


def seed(root, settings, video, name: str = "20260102-000000"):
    out = root / name
    out.mkdir()
    key = preprocess_key_for_settings(settings, [video], None, None)
    return out, seed_run_dir_from_match(out, root, key, settings["camera_profile_name"])


def test_a_matching_attempt_of_the_same_calibration_is_reused(tmp_path, settings, video):
    save_profile(profile(), camera_profiles_dir())
    root = tmp_path / "runs"
    prior = prior_run(root, video, settings)

    out, seeded = seed(root, settings, video)

    assert seeded == prior
    assert (out / "frames" / "000000.png").exists()


def test_a_rotated_calibration_is_not_reused(tmp_path, settings, video):
    """The registry replacing a profile behind a stable name is the whole hazard:
    the key still matches, and those frames were rectified with the old lens."""
    save_profile(profile(), camera_profiles_dir())
    root = tmp_path / "runs"
    prior_run(root, video, settings)
    save_profile(profile(focal=1301.0), camera_profiles_dir())

    out, seeded = seed(root, settings, video)

    assert seeded is None
    assert not (out / "frames").exists()


def test_an_attempt_that_recorded_no_calibration_is_not_reused(tmp_path, settings, video):
    """It cannot show what it was rectified with, so it cannot be shown to match."""
    save_profile(profile(), camera_profiles_dir())
    root = tmp_path / "runs"
    prior_run(root, video, settings, record=False)

    out, seeded = seed(root, settings, video)

    assert seeded is None
    assert not (out / "frames").exists()


def test_a_profile_that_will_not_load_seeds_nothing(tmp_path, settings, video):
    root = tmp_path / "runs"
    save_profile(profile(), camera_profiles_dir())
    prior_run(root, video, settings)
    (camera_profiles_dir() / f"{PROFILE}.json").write_text("not json", encoding="utf-8")

    out, seeded = seed(root, settings, video)

    assert seeded is None
    assert not (out / "frames").exists()


def test_a_profile_the_registry_wrote_matches_the_copy_a_run_kept(tmp_path, settings, video):
    """A pull writes the registry's document and a run keeps the library's copy of
    it. Same measurement, different writers, so the comparison is of documents."""
    save_profile(profile(), camera_profiles_dir())
    root = tmp_path / "runs"
    prior = prior_run(root, video, settings)
    held = json.loads((camera_profiles_dir() / f"{PROFILE}.json").read_text())
    (camera_profiles_dir() / f"{PROFILE}.json").write_text(
        json.dumps(held, indent=4), encoding="utf-8"
    )

    _, seeded = seed(root, settings, video)

    assert seeded == prior


def test_the_classes_and_resolution_still_key_the_cache(tmp_path, settings, video):
    save_profile(profile(), camera_profiles_dir())
    root = tmp_path / "runs"
    prior_run(root, video, settings)
    settings["processing_width"] = 1280

    _, seeded = seed(root, settings, video)

    assert seeded is None
    assert resolve_classes_path(None) is not None
