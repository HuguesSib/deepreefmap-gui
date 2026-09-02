"""Where camera profiles live on this machine.

The library resolves a profile name against ``intrinsics.CAMERA_PROFILE_DIR``, a
CWD-relative default, and then its bundled resources. A packaged binary has no
useful CWD, so the app repoints that directory at a user-writable location
before every enumeration and load. The orchestrator loads by name in this
process, so the same binding is what a run sees.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import platformdirs
from deepreefmap.camera import intrinsics
from deepreefmap.camera.intrinsics import CameraProfile

logger = logging.getLogger(__name__)


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
    """Write ``<directory>/<name>.json`` in the library's format.

    A calibration or an import under this name makes the file this laptop's
    own, so any registry marker on it is cleared: the next pull must not
    replace a local measurement, and a run made with it must not be attributed
    to the registry calibration the file used to be.
    """
    from deepreefmap_gui.camera.registry import MARKER_SUFFIX

    intrinsics.validate_profile_name(profile.name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile.name}.json"
    path.write_text(json.dumps(profile_payload(profile), indent=2), encoding="utf-8")
    (directory / f"{profile.name}{MARKER_SUFFIX}").unlink(missing_ok=True)
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


RUN_PROFILE_NAME = "camera_profile.json"


def copy_profile_into_run(name: str, run_dir: Path) -> dict[str, str]:
    """Write the calibration a run is about to use into its own output directory.

    A run manifest records the profile's NAME and nothing else, and the name
    resolves against a directory on one laptop. Without the document beside the
    outputs, a reconstruction cannot be reproduced anywhere else, a curator
    cannot tell two calibrations called `gopro_hero_10` apart, and deleting the
    profile takes the only record of what the run was rectified with.

    Returns the manifest fields naming what was written, or an empty dict when
    there was nothing to write. A profile the registry published carries its
    calibration id too, which is what ties the run to the measurement a curator
    can open. Never raises: losing the record must not lose the run.
    """
    from deepreefmap_gui.camera.registry import materialised_from

    try:
        profile = load_profile(name)
        payload = json.dumps(profile_payload(profile), indent=2)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / RUN_PROFILE_NAME
        path.write_text(payload, encoding="utf-8")
        # Hashed over the bytes on disk rather than the dict, so the digest is of
        # the file a reader can check rather than of a value only we can rebuild.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        # Read now rather than at push time: the file this run was rectified with
        # is the one sitting there at launch, and a later pull may replace it.
        calibration_id = materialised_from(camera_profiles_dir(), name)
    except Exception:
        logger.warning("Could not copy the camera profile into %s", run_dir, exc_info=True)
        return {}
    recorded = {"camera_profile_file": RUN_PROFILE_NAME, "camera_profile_sha256": digest}
    if calibration_id:
        recorded["camera_calibration_id"] = calibration_id
    return recorded


def run_profile_document(run_dir: Path) -> dict | None:
    """The calibration a finished or abandoned run was rectified with.

    Written at launch by `copy_profile_into_run`, so a run that crashed still
    says what it used. Returns None where a run recorded nothing, which is every
    run made before the profile was copied in. Never raises.
    """
    try:
        return json.loads((run_dir / RUN_PROFILE_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
