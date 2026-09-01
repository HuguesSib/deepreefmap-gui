"""Every camera profile this computer can run with, and where each came from.

Qt-free: the page renders what this reports, and the tests read it without a
window. A profile is bundled with the library, calibrated here, or imported from
another machine; only the last two can be deleted, and only they can be exported
for a laptop that has no way to calibrate the rig itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from deepreefmap_gui.camera.profiles import (
    available_profile_names,
    camera_profiles_dir,
    load_profile,
    load_profile_file,
    profile_payload,
    save_profile,
)


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
    imported: bool = False
    # The registry calibration this file was written from, when it came from
    # there rather than from a calibration or an import on this machine.
    from_registry: str = ""
    # A registry copy standing in front of a bundled profile of the same name
    # that says something different. The registry's is the one runs use.
    shadows_bundled: bool = False
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


def _bundled_document(name: str) -> dict | None:
    """The profile of this name the pipeline ships, where it ships one.

    Read from the package rather than through the library's resolver, which
    answers with the file in ``camera_profiles_dir`` when there is one.
    """
    try:
        from importlib.resources import files

        resource = files("deepreefmap.resources.camera_profiles") / f"{name}.json"
        return json.loads(resource.read_text(encoding="utf-8"))
    except Exception:
        return None


def _entry(name: str, directory: Path) -> ProfileEntry:
    from deepreefmap_gui.camera.registry import materialised_from

    local_path = directory / f"{name}.json"
    local = local_path.is_file()
    from_registry = materialised_from(directory, name) if local else ""
    try:
        profile = load_profile(name)
    except Exception as exc:
        return ProfileEntry(
            name=name,
            local=local,
            path=local_path if local else None,
            from_registry=from_registry,
            error=str(exc),
        )
    diagnostics = profile.diagnostics or {}
    registered = None
    if diagnostics.get("n_registered_images") is not None:
        registered = (
            int(diagnostics["n_registered_images"]),
            int(diagnostics.get("n_input_frames") or 0),
        )
    error = diagnostics.get("mean_reprojection_error_px")
    # Calibrated here or brought here: a calibration writes its previews and its
    # log beside the profile, and an imported file arrives on its own.
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
        imported=local
        and not from_registry
        and not (directory / f"{name}_diagnostics").is_dir(),
        from_registry=from_registry,
        shadows_bundled=bool(from_registry)
        and _bundled_document(name) not in (None, profile_payload(profile)),
    )


def list_profiles() -> list[ProfileEntry]:
    """Bundled profiles and those calibrated here, calibrated ones first.

    Calibrated first because they are the ones somebody made and may want to
    check or discard; the bundled set is the same on every machine.
    """
    directory = camera_profiles_dir()
    entries = [_entry(name, directory) for name in available_profile_names()]
    return sorted(entries, key=lambda entry: (not entry.local, entry.name))


def import_profile(path: Path, *, name: str | None = None) -> ProfileEntry:
    """Bring a profile file onto this machine, under `name` or its own.

    Reads it before writing it, so a file that is not a profile is refused here
    rather than at the start of a run. Raises FileExistsError when the name is
    taken by different content: two laptops calibrating one rig both produce
    `hero12_dome`, and the caller offers a new name rather than overwriting a
    calibration somebody made.
    """
    profile = load_profile_file(path)
    if name:
        profile = replace(profile, name=name)
    directory = camera_profiles_dir()
    target = directory / f"{profile.name}.json"
    if target.is_file():
        if json.loads(target.read_text(encoding="utf-8")) == profile_payload(profile):
            return _entry(profile.name, directory)
        raise FileExistsError(profile.name)
    save_profile(profile, directory)
    return _entry(profile.name, directory)


def export_profile(entry: ProfileEntry, target: Path) -> Path:
    """Write a profile out as the file another machine imports."""
    profile = load_profile(entry.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile_payload(profile), indent=2), encoding="utf-8")
    return target


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
