"""Every camera profile this computer can run with, and where each came from.

Qt-free: the page renders what this reports, and the tests read it without a
window. A profile is either bundled with the library or calibrated here, and
only the second kind can be deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from deepreefmap_gui.camera.profiles import available_profile_names, camera_profiles_dir, load_profile


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    """One profile, read for display."""

    name: str
    local: bool
    path: Path | None
    image_size: tuple[int, int] | None = None
    focal_px: float | None = None
    camera_model: str = ""
    source_video: str = ""
    registered: tuple[int, int] | None = None
    reprojection_error_px: float | None = None
    preview: Path | None = None
    log: Path | None = None
    error: str = ""

    @property
    def resolution(self) -> str:
        if self.image_size is None:
            return ""
        return f"{self.image_size[0]} x {self.image_size[1]}"


def _log(directory: Path, name: str) -> Path | None:
    """The record the calibration wrote, where the profile still has one."""
    path = directory / f"{name}_diagnostics" / "calibration.log"
    return path if path.is_file() else None


def _preview(directory: Path, name: str) -> Path | None:
    """The raw|rectified pair the calibration wrote beside the profile."""
    diagnostics = directory / f"{name}_diagnostics"
    if not diagnostics.is_dir():
        return None
    return next(iter(sorted(diagnostics.glob("compare_*"))), None)


def _entry(name: str, directory: Path) -> ProfileEntry:
    local_path = directory / f"{name}.json"
    local = local_path.is_file()
    try:
        profile = load_profile(name)
    except Exception as exc:
        return ProfileEntry(name=name, local=local, path=local_path if local else None, error=str(exc))
    diagnostics = profile.diagnostics or {}
    registered = None
    if diagnostics.get("n_registered_images") is not None:
        registered = (
            int(diagnostics["n_registered_images"]),
            int(diagnostics.get("n_input_frames") or 0),
        )
    error = diagnostics.get("mean_reprojection_error_px")
    return ProfileEntry(
        name=name,
        local=local,
        path=local_path if local else None,
        image_size=(int(profile.image_size[0]), int(profile.image_size[1])),
        focal_px=float(profile.k[0][0]),
        camera_model=str(profile.distorted_model),
        source_video=str(diagnostics.get("source_video") or ""),
        registered=registered,
        reprojection_error_px=float(error) if error is not None else None,
        preview=_preview(directory, name) if local else None,
        log=_log(directory, name) if local else None,
    )


def list_profiles() -> list[ProfileEntry]:
    """Bundled profiles and those calibrated here, calibrated ones first.

    Calibrated first because they are the ones somebody made and may want to
    check or discard; the bundled set is the same on every machine.
    """
    directory = camera_profiles_dir()
    entries = [_entry(name, directory) for name in available_profile_names()]
    return sorted(entries, key=lambda entry: (not entry.local, entry.name))


def delete_profile(entry: ProfileEntry) -> None:
    """Remove a locally calibrated profile and the previews beside it.

    Bundled profiles are package data and are refused: deleting one would take
    it from every survey on this machine until the app was reinstalled.
    """
    if not entry.local or entry.path is None:
        raise ValueError(f"{entry.name} is bundled with the application, so it cannot be deleted here")
    directory = entry.path.parent
    entry.path.unlink(missing_ok=True)
    diagnostics = directory / f"{entry.name}_diagnostics"
    if diagnostics.is_dir():
        import shutil

        shutil.rmtree(diagnostics, ignore_errors=True)
