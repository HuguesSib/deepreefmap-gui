"""Where camera profiles live on this machine.

The library resolves a profile name against ``intrinsics.CAMERA_PROFILE_DIR``, a
CWD-relative default, and then its bundled resources. A packaged binary has no
useful CWD, so the app repoints that directory at a user-writable location
before every enumeration and load. The orchestrator loads by name in this
process, so the same binding is what a run sees.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import platformdirs
from deepreefmap.camera import intrinsics
from deepreefmap.camera.intrinsics import CameraProfile


def camera_profiles_dir() -> Path:
    """Profiles calibrated here. ``DEEPREEFMAP_CAMERA_PROFILES`` overrides it."""
    override = os.environ.get("DEEPREEFMAP_CAMERA_PROFILES")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "camera_profiles"


def bind_profiles_dir() -> Path:
    """Point the library's profile lookup at ``camera_profiles_dir()``."""
    directory = camera_profiles_dir()
    intrinsics.CAMERA_PROFILE_DIR = directory
    return directory


def available_profile_names() -> list[str]:
    """Bundled profiles plus those calibrated on this machine."""
    bind_profiles_dir()
    return intrinsics.available_profile_names()


def load_profile(name: str) -> CameraProfile:
    bind_profiles_dir()
    return CameraProfile.load(name)


def profile_payload(profile: CameraProfile) -> dict[str, object]:
    """The JSON the library writes for a profile."""
    payload: dict[str, object] = {
        "name": profile.name,
        "source": "colmap_radial_v1",
        "distorted": {"model": str(profile.distorted_model).upper(), "params": profile.radial},
        "rectified_pinhole": {
            "image_size": [int(profile.image_size[0]), int(profile.image_size[1])],
            "K": profile.k.tolist(),
        },
    }
    if profile.diagnostics is not None:
        payload["diagnostics"] = profile.diagnostics
    return payload


def save_profile(profile: CameraProfile, directory: Path) -> Path:
    """Write ``<directory>/<name>.json`` in the library's format."""
    intrinsics.validate_profile_name(profile.name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile.name}.json"
    path.write_text(json.dumps(profile_payload(profile), indent=2), encoding="utf-8")
    return path


def load_profile_file(path: Path) -> CameraProfile:
    """Read a profile from an explicit path, wherever it sits."""
    import numpy as np

    data = json.loads(path.read_text(encoding="utf-8"))
    size = tuple(data["rectified_pinhole"]["image_size"])
    return CameraProfile(
        name=data["name"],
        image_size=(int(size[0]), int(size[1])),
        k=np.array(data["rectified_pinhole"]["K"], dtype=np.float32),
        distorted_model=str(data["distorted"].get("model", "RADIAL")).upper(),
        radial=data["distorted"]["params"],
        diagnostics=data.get("diagnostics"),
    )
