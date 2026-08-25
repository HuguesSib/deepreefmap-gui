"""A named reef location, which many transects and many expeditions share."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class Site:
    """Where the work happened, as a place rather than a line.

    ``latitude``/``longitude`` are a representative point, not a boundary: enough
    to put the site on a map, with the survey lines' own fixes on Transect. The
    field spreadsheets pack this into one ``place`` string, eg. ``djibouti kadda
    dabali japanese garden``, which splits into a country and a site name.
    """

    name: str
    country: str | None = None
    region: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    # The registry position this row was last seen at, sent back as base_seq.
    head_seq: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Site name must not be empty")
        if self.latitude is not None and not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"Latitude out of range: {self.latitude}")
        if self.longitude is not None and not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"Longitude out of range: {self.longitude}")
