"""What the Cameras view reads: which profiles exist, and which were made here.

The bundled set is package data and must never be presented as deletable, so the
distinction is tested on the values rather than through the page.
"""

from __future__ import annotations

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile

from deepreefmap_gui.camera.inventory import delete_profile, list_profiles
from deepreefmap_gui.camera.profiles import camera_profiles_dir, save_profile


def _profile(name: str, **diagnostics) -> CameraProfile:
    return CameraProfile(
        name=name,
        image_size=(1920, 1440),
        k=np.array([[1035.0, 0.0, 960.0], [0.0, 1035.0, 720.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="RADIAL",
        radial={"fx": 1035.0, "fy": 1035.0, "cx": 960.0, "cy": 720.0, "k1": 0.01, "k2": 0.0},
        diagnostics=dict(diagnostics) or None,
    )


def _named(name: str):
    return next(entry for entry in list_profiles() if entry.name == name)


def test_a_bundled_profile_is_listed_and_not_local():
    entry = _named("gopro_hero_10")

    assert not entry.local
    assert entry.path is None
    assert entry.resolution == "1920 x 1080"


def test_a_calibrated_profile_carries_where_it_came_from():
    save_profile(
        _profile(
            "field_cam",
            n_input_frames=100,
            n_registered_images=78,
            mean_reprojection_error_px=0.62,
            source_video="GX010042.MP4",
        ),
        camera_profiles_dir(),
    )

    entry = _named("field_cam")

    assert entry.local
    assert entry.registered == (78, 100)
    assert entry.reprojection_error_px == pytest.approx(0.62)
    assert entry.source_video == "GX010042.MP4"
    assert entry.focal_px == pytest.approx(1035.0)


def test_the_ones_made_here_are_listed_first():
    save_profile(_profile("aaa_bundled_would_sort_first"), camera_profiles_dir())

    assert list_profiles()[0].name == "aaa_bundled_would_sort_first"


def test_deleting_takes_the_profile_and_its_previews():
    save_profile(_profile("field_cam"), camera_profiles_dir())
    previews = camera_profiles_dir() / "field_cam_diagnostics"
    previews.mkdir(parents=True, exist_ok=True)
    (previews / "compare_000.png").write_bytes(b"")

    delete_profile(_named("field_cam"))

    assert not (camera_profiles_dir() / "field_cam.json").exists()
    assert not previews.exists()
    assert "field_cam" not in {entry.name for entry in list_profiles()}


def test_a_bundled_profile_refuses_to_be_deleted():
    with pytest.raises(ValueError, match="bundled"):
        delete_profile(_named("gopro_hero_10"))


def test_a_calibrated_profile_shows_its_preview():
    save_profile(_profile("field_cam"), camera_profiles_dir())
    previews = camera_profiles_dir() / "field_cam_diagnostics"
    previews.mkdir(parents=True, exist_ok=True)
    (previews / "compare_000.png").write_bytes(b"")

    assert _named("field_cam").preview == previews / "compare_000.png"
