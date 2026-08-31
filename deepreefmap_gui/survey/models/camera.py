"""Camera profiles and their calibrations, as the registry publishes them.

Never authored on a device: both sections are pull-only, the way presets are. A
laptop that calibrates a rig publishes it through the registry's upload endpoint,
which returns rows that come back down on the next pull like any other.

A profile names a rig. A calibration measures it, and versions of one profile
coexist rather than overwrite: a housing change invalidates the last measurement
without invalidating the runs made under it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class CameraProfile:
    """A named rig: a body, a lens mode, a housing, a resolution."""

    name: str
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    head_seq: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A camera profile must carry a name")


@dataclass(slots=True)
class CameraCalibration:
    """One measurement of one profile, and the document a run is rectified with."""

    camera_profile_id: uuid.UUID
    document: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    image_width: int | None = None
    image_height: int | None = None
    reprojection_error_px: float | None = None
    registered_frames: int | None = None
    source_clip: str = ""
    calibrated_at: str | None = None
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    head_seq: int | None = None

    def __post_init__(self) -> None:
        self.version = int(self.version)
        if self.version < 1:
            raise ValueError("A calibration version counts from 1")
        if not isinstance(self.document, dict):
            raise ValueError("A calibration document must be a mapping")

    @property
    def resolution(self) -> str:
        if not self.image_width or not self.image_height:
            return ""
        return f"{self.image_width}x{self.image_height}"
