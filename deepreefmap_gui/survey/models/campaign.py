"""A field expedition: one trip, which visits as many sites as it visits."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class Campaign:
    """An expedition, named as the archive folders are: ``2025_10_eritrea``.

    Independent of site, because one trip visits several, so a pass names a
    campaign and a transect separately. The dates are ISO ``YYYY-MM-DD`` days,
    not timestamps: an expedition is planned in days.
    """

    name: str
    begin_date: str | None = None
    end_date: str | None = None
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    # The registry position this row was last seen at, sent back as base_seq.
    head_seq: int | None = None
    # Stamped by the console; from then on a change made here is a proposal.
    validated_at: str | None = None
    validated_by: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Campaign name must not be empty")
        if self.begin_date and self.end_date and self.end_date < self.begin_date:
            raise ValueError("end_date must not precede begin_date")
