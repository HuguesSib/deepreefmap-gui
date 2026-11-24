"""What an expedition still lacks, level by level, so nobody has to go looking.

An import fills what the folders say and nothing else. The rest (where a
transect lies, when the trip ran, which camera shot a clip, where a pass starts
and stops) is somebody's job, and this is the list of those jobs. Each gap names
the record it belongs to, so the interface can open the right editor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

EXPEDITION = "expedition"
TRANSECT = "transect"
PASS = "pass"
CLIP = "clip"
LEVELS = (EXPEDITION, TRANSECT, PASS, CLIP)

# A window within this much of the whole recording is taken as never trimmed.
_UNTRIMMED_TOLERANCE_S = 0.5


@dataclass(frozen=True, slots=True)
class Gap:
    level: str
    subject: str  # what the record is called, as the person knows it
    item: str  # what is missing, in a few words
    record_id: uuid.UUID


def capture_span(store: Any, campaign_id: uuid.UUID) -> tuple[str | None, str | None]:
    """First and last capture day of the expedition's clips, to suggest its dates.

    The camera's own stamp is used when any clip has one. A file date is a copy
    date as often as a recording date, so it only stands in when nothing better
    exists.
    """
    from deepreefmap_gui.survey.video_probe import SOURCE_CONTAINER

    stamped: list[str] = []
    copied: list[str] = []
    videos = {v.id: v for v in store.list_videos()}
    for pass_ in store.list_passes(campaign_id=campaign_id):
        for video_id in [pass_.video_id, *pass_.extra_video_ids]:
            video = videos.get(video_id)
            if video is None or not video.captured_at:
                continue
            day = video.captured_at.split("T")[0]
            (stamped if video.captured_source == SOURCE_CONTAINER else copied).append(day)
    days = stamped or copied
    return (min(days), max(days)) if days else (None, None)


def _untrimmed(pass_: Any, videos: dict[uuid.UUID, Any]) -> bool:
    total = sum((videos[v].duration_s or 0.0) for v in [pass_.video_id, *pass_.extra_video_ids] if v in videos)
    return pass_.begin_s <= 0.0 and total > 0 and abs(pass_.end_s - total) <= _UNTRIMMED_TOLERANCE_S


def missing_data(store: Any, campaign_id: uuid.UUID) -> list[Gap]:
    """Every gap in one expedition, expedition first, then transects, passes, clips."""
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        return []
    passes = store.list_passes(campaign_id=campaign_id)
    videos = {v.id: v for v in store.list_videos()}
    transects = {t.id: t for t in store.list_transects()}

    gaps: list[Gap] = []
    if not campaign.begin_date or not campaign.end_date:
        gaps.append(Gap(EXPEDITION, campaign.name, "start and end dates", campaign.id))

    seen_transects: list[uuid.UUID] = []
    for pass_ in passes:
        if pass_.transect_id and pass_.transect_id not in seen_transects:
            seen_transects.append(pass_.transect_id)
    for transect_id in seen_transects:
        transect = transects.get(transect_id)
        if transect is None:
            continue
        if transect.end_points() is None:
            gaps.append(Gap(TRANSECT, transect.name, "start and end coordinates", transect.id))
        if transect.length_m is None:
            gaps.append(Gap(TRANSECT, transect.name, "tape length", transect.id))
        if transect.depth_m is None:
            gaps.append(Gap(TRANSECT, transect.name, "depth", transect.id))
        if transect.site_id is None:
            gaps.append(Gap(TRANSECT, transect.name, "site", transect.id))

    seen_clips: list[uuid.UUID] = []
    for pass_ in passes:
        transect = transects.get(pass_.transect_id) if pass_.transect_id else None
        first = videos.get(pass_.video_id)
        camera = (first.camera_label if first else None) or (first.file_name if first else "?")
        subject = " · ".join(
            part for part in (transect.name if transect else "Unassigned", pass_.label, pass_.direction, camera) if part
        )
        if pass_.transect_id is None:
            gaps.append(Gap(PASS, subject, "transect", pass_.id))
        if pass_.direction is None:
            gaps.append(Gap(PASS, subject, "direction", pass_.id))
        if _untrimmed(pass_, videos):
            gaps.append(Gap(PASS, subject, "start and end of the swim (not trimmed)", pass_.id))
        if pass_.quality is None:
            gaps.append(Gap(PASS, subject, "quality", pass_.id))
        runs = store.runs_for_pass(pass_.id)
        if not runs:
            gaps.append(Gap(PASS, subject, "not processed yet", pass_.id))
        elif runs[-1].status == "failed":
            gaps.append(Gap(PASS, subject, "last run failed", pass_.id))
        for video_id in [pass_.video_id, *pass_.extra_video_ids]:
            if video_id not in seen_clips:
                seen_clips.append(video_id)

    for video_id in seen_clips:
        video = videos.get(video_id)
        if video is None:
            continue
        if not video.camera_label:
            gaps.append(Gap(CLIP, video.file_name, "camera", video.id))
        if not video.rig_position:
            gaps.append(Gap(CLIP, video.file_name, "rig position", video.id))
        if video.review == "unreviewed":
            gaps.append(Gap(CLIP, video.file_name, "review", video.id))
    return gaps
