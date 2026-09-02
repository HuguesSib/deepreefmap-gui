"""Scenario: the console publishes a camera profile and this laptop pulls it.

Expected behaviour: the calibration becomes a file in the profiles directory,
because that is the only place a run resolves a profile name against. A profile
this laptop calibrated itself is never replaced by one from the registry.
"""

from __future__ import annotations

import json
import uuid

import numpy as np
import pytest
from deepreefmap.camera.intrinsics import CameraProfile as LibraryProfile

from deepreefmap_gui.camera.inventory import list_profiles
from deepreefmap_gui.camera.profiles import camera_profiles_dir, copy_profile_into_run, save_profile
from deepreefmap_gui.camera.registry import materialise_pulled, materialised_from
from deepreefmap_gui.survey.models import CameraCalibration, CameraProfile
from deepreefmap_gui.survey.store import SurveyStore


def document(name: str, focal: float = 1243.0) -> dict:
    return {
        "name": name,
        "source": "colmap_radial_v1",
        "distorted": {
            "model": "RADIAL",
            "params": {"fx": focal, "fy": focal, "cx": 960.0, "cy": 540.0, "k1": 0.36, "k2": 0.24},
        },
        "rectified_pinhole": {
            "image_size": [1920, 1080],
            "K": [[focal, 0.0, 959.5], [0.0, focal, 539.5], [0.0, 0.0, 1.0]],
        },
    }


def local_profile(name: str) -> LibraryProfile:
    return LibraryProfile(
        name=name,
        image_size=(1920, 1440),
        k=np.array([[1035.0, 0.0, 960.0], [0.0, 1035.0, 720.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        distorted_model="RADIAL",
        radial={"fx": 1035.0, "fy": 1035.0, "cx": 960.0, "cy": 720.0, "k1": 0.01, "k2": 0.0},
        diagnostics={"n_input_frames": 100},
    )


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    held = SurveyStore(tmp_path / "survey.db")
    yield held
    held.close()


def publish(store: SurveyStore, name: str, *, version: int = 1, focal: float = 1243.0) -> uuid.UUID:
    """What a pull lands: a profile row and a calibration row."""
    held = {p.name: p for p in store.list_camera_profiles()}
    profile = held.get(name) or CameraProfile(name=name)
    calibration = CameraCalibration(
        camera_profile_id=profile.id,
        document=document(name, focal),
        version=version,
        image_width=1920,
        image_height=1080,
    )
    if name not in held:
        store._add("camera_profile", profile)
    store._add("camera_calibration", calibration)
    return calibration.id


def deploy(store: SurveyStore, name: str, calibration_id: uuid.UUID | None) -> None:
    """What a curator's choice looks like once it has been pulled down."""
    profile = next(p for p in store.list_camera_profiles() if p.name == name)
    profile.current_calibration_id = calibration_id
    store._update("camera_profile", profile)


def test_a_pulled_calibration_becomes_a_file_a_run_can_resolve(store):
    publish(store, "hero12_dome")

    written = materialise_pulled(store)

    assert written == ["hero12_dome"]
    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["rectified_pinhole"]["image_size"] == [1920, 1080]
    assert "hero12_dome" in {entry.name for entry in list_profiles()}


def test_the_newest_version_is_the_one_written(store):
    publish(store, "hero12_dome", version=1, focal=1243.0)
    materialise_pulled(store)
    publish(store, "hero12_dome", version=2, focal=1301.0)

    materialise_pulled(store)

    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["distorted"]["params"]["fx"] == 1301.0


def test_writing_again_with_nothing_new_writes_nothing(store):
    publish(store, "hero12_dome")
    materialise_pulled(store)

    assert materialise_pulled(store) == []


def test_a_profile_calibrated_here_is_never_replaced(store):
    """This laptop measured that rig, and the file it wrote is what its runs were
    rectified with."""
    save_profile(local_profile("hero12_dome"), camera_profiles_dir())
    publish(store, "hero12_dome")

    assert materialise_pulled(store) == []
    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["rectified_pinhole"]["image_size"] == [1920, 1440]


def test_a_materialised_profile_names_the_calibration_it_came_from(store):
    calibration_id = publish(store, "hero12_dome")

    materialise_pulled(store)

    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(calibration_id)


def test_a_survey_that_never_synced_has_nothing_to_write(store):
    assert materialise_pulled(store) == []


def test_a_run_names_the_calibration_the_registry_gave_it(store, tmp_path):
    """The id is what ties a run to a measurement a curator can open."""
    calibration_id = publish(store, "gopro_hero_10")
    materialise_pulled(store)

    recorded = copy_profile_into_run("gopro_hero_10", tmp_path / "run")

    assert recorded["camera_calibration_id"] == str(calibration_id)


def test_a_locally_calibrated_profile_names_no_registry_calibration(tmp_path):
    save_profile(local_profile("field_cam"), camera_profiles_dir())

    recorded = copy_profile_into_run("field_cam", tmp_path / "run")

    assert recorded["camera_profile_file"]
    assert "camera_calibration_id" not in recorded


def test_a_registry_copy_says_where_it_came_from(store):
    publish(store, "hero12_dome")
    materialise_pulled(store)

    entry = next(e for e in list_profiles() if e.name == "hero12_dome")

    assert entry.from_registry
    assert not entry.imported
    assert not entry.shadows_bundled


def test_a_registry_copy_of_a_bundled_name_says_it_stands_in_front_of_it(store):
    """The pipeline ships gopro_hero_10; a published calibration of that name is
    what runs here resolve, and the page has to say so."""
    publish(store, "gopro_hero_10", focal=999.0)
    materialise_pulled(store)

    entry = next(e for e in list_profiles() if e.name == "gopro_hero_10")

    assert entry.shadows_bundled


def test_calibrating_over_a_registry_name_makes_the_file_this_laptops_own(store, tmp_path):
    """The marker goes with the overwrite: the next pull must not replace a
    local measurement, and a run must not be attributed to the registry
    calibration the file used to be."""
    publish(store, "hero12_dome")
    materialise_pulled(store)

    save_profile(local_profile("hero12_dome"), camera_profiles_dir())

    assert materialised_from(camera_profiles_dir(), "hero12_dome") == ""
    assert materialise_pulled(store) == [], "the pull replaced a local calibration"
    recorded = copy_profile_into_run("hero12_dome", tmp_path / "run")
    assert "camera_calibration_id" not in recorded


def test_a_withdrawn_calibration_takes_its_file_back(store):
    publish(store, "hero12_dome")
    materialise_pulled(store)
    profile = store.camera_profile_by_name("hero12_dome")
    calibration = store.newest_camera_calibration(profile.id)
    calibration.deleted_at = "2026-09-01T10:00:00+00:00"
    store._update("camera_calibration", calibration)

    materialise_pulled(store)

    assert not (camera_profiles_dir() / "hero12_dome.json").exists()
    assert materialised_from(camera_profiles_dir(), "hero12_dome") == ""


def test_a_name_this_survey_never_pulled_is_left_alone(store):
    """The profiles directory is machine-wide; another survey's sync wrote it."""
    other = SurveyStore(store.path.parent / "other.db")
    try:
        publish(other, "hero12_dome")
        materialise_pulled(other)

        materialise_pulled(store)

        assert (camera_profiles_dir() / "hero12_dome.json").exists()
    finally:
        other.close()


def test_the_calibration_the_registry_deploys_is_the_one_written(store):
    """Publishing stages a measurement; only deploying puts it on the laptops."""
    first = publish(store, "hero12_dome", focal=1243.0)
    publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", first)

    materialise_pulled(store)

    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["distorted"]["params"]["fx"] == 1243.0
    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(first)


def test_deploying_a_newer_calibration_replaces_the_file(store):
    first = publish(store, "hero12_dome", focal=1243.0)
    second = publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", first)
    materialise_pulled(store)

    deploy(store, "hero12_dome", second)
    written = materialise_pulled(store)

    assert written == ["hero12_dome"]
    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["distorted"]["params"]["fx"] == 1301.0
    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(second)


def test_deploying_an_older_calibration_takes_the_laptop_back(store):
    """Rolling back is deploying backwards, and a run made after it says so."""
    first = publish(store, "hero12_dome", focal=1243.0)
    second = publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", second)
    materialise_pulled(store)

    deploy(store, "hero12_dome", first)
    materialise_pulled(store)

    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["distorted"]["params"]["fx"] == 1243.0
    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(first)


def test_a_run_names_the_calibration_the_registry_deploys(store, tmp_path):
    first = publish(store, "hero12_dome", focal=1243.0)
    publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", first)
    materialise_pulled(store)

    recorded = copy_profile_into_run("hero12_dome", tmp_path / "run")

    assert recorded["camera_calibration_id"] == str(first)


def test_a_profile_that_deploys_nothing_still_takes_the_newest(store):
    publish(store, "hero12_dome", focal=1243.0)
    second = publish(store, "hero12_dome", version=2, focal=1301.0)

    materialise_pulled(store)

    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(second)


def test_a_deployment_withdrawn_behind_our_back_falls_back_to_the_newest(store):
    """A laptop is never stranded by a measurement the registry took away."""
    first = publish(store, "hero12_dome", focal=1243.0)
    second = publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", first)
    withdrawn = store.camera_calibration(first)
    withdrawn.deleted_at = "2026-09-01T00:00:00Z"
    store._update("camera_calibration", withdrawn)

    materialise_pulled(store)

    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(second)


def test_a_deployment_this_laptop_has_not_pulled_falls_back_to_the_newest(store):
    newest = publish(store, "hero12_dome", focal=1243.0)
    deploy(store, "hero12_dome", uuid.uuid4())

    materialise_pulled(store)

    assert materialised_from(camera_profiles_dir(), "hero12_dome") == str(newest)


def test_a_locally_calibrated_profile_is_left_alone_when_a_deployment_moves(store):
    first = publish(store, "hero12_dome", focal=1243.0)
    second = publish(store, "hero12_dome", version=2, focal=1301.0)
    deploy(store, "hero12_dome", first)
    save_profile(local_profile("hero12_dome"), camera_profiles_dir())

    deploy(store, "hero12_dome", second)
    written = materialise_pulled(store)

    assert written == []
    on_disk = json.loads((camera_profiles_dir() / "hero12_dome.json").read_text())
    assert on_disk["rectified_pinhole"]["image_size"] == [1920, 1440]
