"""A run-settings preset the registry publishes, pulled whole.

Never authored on a device: the contract keeps the section pull-only. The
settings bag is stored as it arrived; which keys this build can use is decided
where a preset is turned into an organisation preset.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from deepreefmap_gui.survey.models.common import utc_now_iso


@dataclass(slots=True)
class ServerPreset:
    name: str
    version: int = 1
    settings: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    head_seq: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A server preset must carry a name")
        self.version = int(self.version)
        if self.version < 1:
            raise ValueError("A server preset version counts from 1")
        if not isinstance(self.settings, dict):
            raise ValueError("Server preset settings must be a mapping")

    @property
    def label(self) -> str:
        return f"{self.name} (v{self.version})"
