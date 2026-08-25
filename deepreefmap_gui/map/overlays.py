"""Transect overlay model, pixel-space hit testing, and the survey overlay set.

Every map that draws the surveyed transects (the Browse rail and the transect
analysis pane) draws the same ones the same way, so the set is built here
rather than once per page.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

ENDPOINT_HIT_PX = 12.0
LINE_HIT_PX = 8.0


@dataclass
class OverlayTransect:
    id: str
    start: tuple[float, float]
    end: tuple[float, float]
    color: QColor
    selected: bool = False
    # Drawn beside the line so a transect is identifiable without clicking it;
    # the tooltip carries the detail that would clutter the map.
    label: str = ""
    tooltip: str = ""


def transect_status_color(statuses: list[str]) -> QColor:
    """Grey none, red any failure, green all succeeded, amber in between."""
    if not statuses:
        return QColor(128, 128, 128)
    if any(status == "failed" for status in statuses):
        return QColor(200, 70, 60)
    if all(status == "succeeded" for status in statuses):
        return QColor(70, 170, 90)
    return QColor(220, 160, 40)


def transect_tooltip(store, transect, runs: list) -> str:
    """What has actually been surveyed here, without opening the transect."""
    from deepreefmap_gui.survey.catalogue import parse_run_timestamp

    passes = store.list_passes(transect_id=transect.id)
    videos = {video_id for p in passes for video_id in p.video_ids()}
    done = sum(1 for run in runs if run.status == "succeeded")
    failed = sum(1 for run in runs if run.status == "failed")
    lines = [f"<b>{transect.name}</b>"]
    # Said before the counts, because it changes what they are a record of: this
    # line is no longer one anything new can be filed against.
    if transect.deleted_at:
        lines.append("Retired in the registry")
    lines.append(f"{len(videos)} video{'s' if len(videos) != 1 else ''}"
                 f" · {len(passes)} pass{'es' if len(passes) != 1 else ''}")
    if runs:
        summary = f"{done} of {len(runs)} run{'s' if len(runs) != 1 else ''} succeeded"
        if failed:
            summary += f", {failed} failed"
        lines.append(summary)
        # Parsed rather than compared as text: the two fields can carry a UTC
        # offset or not, and "...T10:00:00+00:00" sorts after "...T23:00:00"
        # as a string while being the earlier instant.
        stamps = [parse_run_timestamp(run.started_at or run.created_at) for run in runs]
        latest = max((s for s in stamps if s is not None), default=None)
        if latest is not None:
            lines.append(f"Last run {latest.date().isoformat()}")
    else:
        lines.append("Not processed yet")
    if transect.length_m:
        lines.append(f"{transect.length_m:g} m tape")
    return "<br>".join(lines)


def transect_overlays(store, selected_id: uuid.UUID | None, transects=None) -> list[OverlayTransect]:
    """The transects to draw, coloured by how their runs went.

    The caller decides which lines, because the two maps that use this are
    asking different questions. A picker draws what somebody may choose, so a
    retired line has no business on it. Browse draws where the work was done,
    and a run filed against a line the registry has since withdrawn still
    happened somewhere.
    """
    overlays = []
    for transect in (store.list_transects() if transects is None else transects):
        ends = transect.end_points()
        if ends is None:
            continue
        runs = store.runs_for_transect(transect.id)
        overlays.append(OverlayTransect(
            id=str(transect.id),
            start=ends[0],
            end=ends[1],
            color=transect_status_color([run.status for run in runs]),
            selected=transect.id == selected_id,
            label=transect.name,
            tooltip=transect_tooltip(store, transect, runs),
        ))
    return overlays


def segment_distance_px(point: QPointF, a: QPointF, b: QPointF) -> float:
    """Distance from ``point`` to the segment a-b, in pixels."""
    ax, ay = a.x(), a.y()
    dx, dy = b.x() - ax, b.y() - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(point.x() - ax, point.y() - ay)
    t = ((point.x() - ax) * dx + (point.y() - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(point.x() - (ax + t * dx), point.y() - (ay + t * dy))
