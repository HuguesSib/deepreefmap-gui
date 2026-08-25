"""One execution of a pass through the reconstruction pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.statuses import RUN_STATUSES, TERMINAL_STATUSES

__all__ = ["RUN_STATUSES", "TERMINAL_STATUSES", "RunRecord"]


@dataclass(slots=True)
class RunRecord:
    """Re-running a pass creates a new record; repeats are the reproducibility data.

    ``batch_id`` is the session the attempt ran in; a rerun of the same pass can
    belong to a later session than the pass itself.
    ``run_dir_name`` is relative to the output root so a moved folder keeps working.
    Cover numbers stay in the run directory's benthic_cover.json, never in the database.

    The provenance block is copied out of run_manifest.json when the run
    finishes, so pruning a run directory no longer degrades the next push to
    nulls. None means nothing was recorded; for the dict fields an empty value
    is itself a recorded fact, which is why they are optional rather than empty.

    ``processing_width`` and ``processing_height`` are the pixel size the run
    actually processed at, never the name of a resolution preset: Native, Half
    and Quarter resolve to numbers in the form before the run starts. ``fps`` is
    the rate frames were decoded at, which is a whole number and is not the
    clip's own frame rate on ``VideoAsset``.
    """

    pass_id: uuid.UUID
    run_dir_name: str
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    batch_id: uuid.UUID | None = None
    gui_version: str | None = None
    library_version: str | None = None
    segmentation_model: str | None = None
    mapping_backend: str | None = None
    processing_width: int | None = None
    processing_height: int | None = None
    fps: int | None = None
    preprocess_batch_size: int | None = None
    taxonomy_version: int | None = None
    taxonomy_hash: str | None = None
    model_revisions: dict[str, Any] | None = None
    preset_name: str | None = None
    preset_version: int | None = None
    preset_hash: str | None = None
    preset_deviations: dict[str, Any] | None = None
    run_duration_s: float | None = None
    stage_durations: dict[str, Any] | None = None
    stage_peaks: dict[str, Any] | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    # The registry position this row was last seen at, sent back as base_seq.
    head_seq: int | None = None

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {self.status!r}")
        if not self.run_dir_name.strip():
            raise ValueError("run_dir_name must not be empty")
