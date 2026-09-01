"""Putting the registry's calibrations where the pipeline looks for them.

A profile the console publishes reaches this laptop as two rows: the profile and
its calibrations. A run resolves a profile by name against
``camera_profiles_dir()``, so a pulled calibration is of no use until it is a file
in there. This writes the newest calibration of every profile the registry holds,
and leaves alone anything calibrated or imported here: a laptop's own measurement
of its own rig is not something a sync should quietly replace.

Qt-free, and safe to call when nothing has ever synced: with no rows there is
nothing to write.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepreefmap_gui.camera.profiles import camera_profiles_dir

logger = logging.getLogger(__name__)

# Written beside a materialised profile, naming the calibration it came from, so a
# later pull can tell its own file from one somebody made here.
MARKER_SUFFIX = ".from-registry"


def materialised_from(directory: Path, name: str) -> str:
    """The calibration id a profile file was written from, or an empty string."""
    marker = directory / f"{name}{MARKER_SUFFIX}"
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def materialise_pulled(store) -> list[str]:
    """Write the registry's newest calibration per profile, and say what changed.

    A name held by a local calibration is skipped: this laptop measured that rig
    and the file it wrote is the one its runs were rectified with.
    """
    try:
        profiles = store.list_camera_profiles()
    except Exception:
        logger.warning("Could not read the registry's camera profiles", exc_info=True)
        return []
    directory = camera_profiles_dir()
    written: list[str] = []
    for profile in profiles:
        calibration = store.newest_camera_calibration(profile.id)
        if calibration is None or not calibration.document:
            continue
        path = directory / f"{profile.name}.json"
        came_from = materialised_from(directory, profile.name)
        if path.exists() and not came_from:
            logger.debug("Leaving the local camera profile %s alone", profile.name)
            continue
        if came_from == str(calibration.id):
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(calibration.document, indent=2), encoding="utf-8")
            (directory / f"{profile.name}{MARKER_SUFFIX}").write_text(
                str(calibration.id), encoding="utf-8"
            )
        except OSError:
            logger.warning("Could not write the camera profile %s", profile.name, exc_info=True)
            continue
        written.append(profile.name)
    _remove_withdrawn(directory, profiles, store)
    if written:
        logger.info("Wrote camera profiles from the registry: %s", ", ".join(written))
    return written


def _remove_withdrawn(directory: Path, profiles, store) -> None:
    """Take back a materialised profile the registry no longer stands behind.

    Only files carrying a marker are touched: the marker is what says this
    laptop did not make them. And only on evidence of withdrawal, a tombstoned
    profile or one whose calibrations were all tombstoned. A name this survey's
    store simply does not hold is left alone: the profiles directory is
    machine-wide and another survey's sync may have written it.
    """
    for marker in directory.glob(f"*{MARKER_SUFFIX}"):
        name = marker.name.removesuffix(MARKER_SUFFIX)
        if not _withdrawn(store, name):
            continue
        try:
            (directory / f"{name}.json").unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove the withdrawn profile %s", name, exc_info=True)
            continue
        logger.info("Removed the withdrawn camera profile %s", name)


def _withdrawn(store, name: str) -> bool:
    try:
        row = store.camera_profile_by_name(name)
    except Exception:
        logger.warning("Could not read the camera profile %s", name, exc_info=True)
        return False
    if row is None:
        return False
    if row.deleted_at:
        return True
    return store.newest_camera_calibration(row.id) is None
