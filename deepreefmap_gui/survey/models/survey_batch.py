"""A group of passes queued and executed together (typically one field day)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from deepreefmap_gui.survey.models.common import utc_now_iso


def session_label(created_at: str | None) -> str:
    """What a session is called: when it was started, in local time.

    A session is one workstation's queue, not a survey fact -- it never leaves
    this machine and the registry has no table for it. Nothing about it needs a
    name a person chose, and a chosen name was one more field to fill in before
    any work could start. The moment it began identifies it, orders it, and is
    never two sessions' answer at once.

    Seconds are shown, not just the minute: a cart is minted the moment passes
    are queued against a running order, so two sessions can begin within the
    same minute and a label that could not tell them apart would be no identity
    at all.
    """
    if not created_at:
        return "Unknown session"
    try:
        return datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(created_at)


@dataclass(slots=True)
class SurveyBatch:
    preset_name: str = "survey_preset"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def label(self) -> str:
        return session_label(self.created_at)
