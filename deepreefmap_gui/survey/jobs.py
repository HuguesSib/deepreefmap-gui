"""Presentation states for footage and its processing work."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

from deepreefmap_gui.survey import statuses

if TYPE_CHECKING:
    from deepreefmap_gui.survey.catalogue import VideoLibraryEntry
    from deepreefmap_gui.survey.models.run_record import RunRecord


def pass_state(runs: Sequence[RunRecord], *, queued: bool = False) -> str:
    """Return active execution, queue membership or the latest attempt's state."""
    latest = sorted(runs, key=lambda run: run.created_at or "")[-1] if runs else None
    if latest is not None and latest.status == "running":
        return "running"
    if queued:
        return "queued"
    if latest is None:
        return "ready"
    return latest.status


def clip_states(entry: VideoLibraryEntry) -> list[str]:
    """Return the presentation state of each defined pass."""
    runs: dict[str, list[RunRecord]] = {}
    for run in entry.runs:
        runs.setdefault(str(run.pass_id), []).append(run)
    return [
        pass_state(runs.get(str(pass_.id), []), queued=str(pass_.id) in entry.queued_pass_ids)
        for pass_ in entry.passes
    ]


def job_filters(entry: VideoLibraryEntry) -> set[str]:
    """Return the overlapping job filters that contain a video."""
    states = clip_states(entry)
    filters = {"all"}
    if not entry.passes:
        filters.add("organise")
    elif all(state == "succeeded" for state in states):
        filters.add("complete")
    if any(state not in {"succeeded", "failed"} for state in states):
        filters.add("process")
    if (
        entry.link_state == "missing"
        or any(pass_.transect_id is None for pass_ in entry.passes)
        or any(state in {"failed", "interrupted", "incomplete"} for state in states)
    ):
        filters.add("attention")
    return filters


def clip_summary(entry: VideoLibraryEntry) -> str:
    """Return a readable summary of defined passes and their current work."""
    if not entry.passes:
        return "No passes defined"
    counts = Counter(clip_states(entry))
    labels = {"succeeded": "complete", "ready": "ready to run"}
    return " · ".join(
        f"{count} {labels.get(state, statuses.status_label(state).lower())}"
        for state, count in counts.items()
    )
