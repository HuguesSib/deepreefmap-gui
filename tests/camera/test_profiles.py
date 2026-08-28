"""Scenario: a profile calibrated on this machine sits in the user data dir, and a
run that names it must find it through the library's own lookup.

Expected behaviour: enumerating binds the library's profile directory to ours, so
the library's ``CameraProfile.load`` (what the orchestrator calls) resolves the
name, and bundled profiles stay visible beside it.
"""

from __future__ import annotations

import json

import numpy as np
from deepreefmap.camera import intrinsics
from deepreefmap.camera.intrinsics import CameraProfile

from deepreefmap_gui.camera.profiles import (
    available_profile_names,
    bind_profiles_dir,
    camera_profiles_dir,
    load_profile,
    load_profile_file,
    profile_payload,
    save_profile,
)


def _profile(name: str) -> CameraProfile:
    return CameraProfile(
        name=name,
        image_size=(64, 48),
        k=np.array([[60.0, 0.0, 32.0], [0.0, 60.0, 24.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="SIMPLE_RADIAL",
        radial={"fx": 60.0, "fy": 60.0, "cx": 32.0, "cy": 24.0, "k1": 0.01, "k2": 0.0},
        diagnostics={"n_input_frames": 12},
    )


def test_a_saved_profile_is_listed_beside_the_bundled_ones():
    save_profile(_profile("field_cam"), camera_profiles_dir())

    names = available_profile_names()

    assert "field_cam" in names
    assert "gopro_hero_10" in names


def test_the_library_lookup_resolves_a_profile_saved_here():
    save_profile(_profile("field_cam"), camera_profiles_dir())
    bind_profiles_dir()

    loaded = CameraProfile.load("field_cam")

    assert loaded.name == "field_cam"
    assert loaded.image_size == (64, 48)
    assert camera_profiles_dir() == intrinsics.CAMERA_PROFILE_DIR


def test_the_bundled_profile_still_loads_after_binding():
    assert load_profile("gopro_hero_10").name == "gopro_hero_10"


def test_saved_json_matches_what_the_library_writes(tmp_path, monkeypatch):
    profile = _profile("same")
    monkeypatch.setattr(intrinsics, "CAMERA_PROFILE_DIR", tmp_path / "theirs")
    theirs = json.loads(profile.save().read_text())

    ours = json.loads(save_profile(profile, tmp_path / "ours").read_text())

    assert ours == theirs == profile_payload(profile)
    reloaded = load_profile_file(tmp_path / "ours" / "same.json")
    assert reloaded.radial == profile.radial
    assert np.allclose(reloaded.k, profile.k)
