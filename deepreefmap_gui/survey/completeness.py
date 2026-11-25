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

from deepreefmap_gui.survey.models.video_asset import VIEW_DOWN

EXPEDITION = "expedition"
TRANSECT = "transect"
PASS = "pass"
CLIP = "clip"
LEVELS = (EXPEDITION, TRANSECT, PASS, CLIP)

# The gaps, by code.
EXPEDITION_DATES = "expedition_dates"
TRANSECT_COORDINATES = "transect_coordinates"
TRANSECT_LENGTH = "transect_length"
TRANSECT_DEPTH = "transect_depth"
TRANSECT_SITE = "transect_site"
PASS_TRANSECT = "pass_transect"
PASS_DIRECTION = "pass_direction"
PASS_UNTRIMMED = "pass_untrimmed"
PASS_QUALITY = "pass_quality"
PASS_NOT_RUN = "pass_not_run"
PASS_RUN_FAILED = "pass_run_failed"
CLIP_CAMERA = "clip_camera"
CLIP_RIG = "clip_rig"
CLIP_REVIEW = "clip_review"

# A window within this much of the whole recording is taken as never trimmed.
_UNTRIMMED_TOLERANCE_S = 0.5


@dataclass(frozen=True, slots=True)
class Gap:
    level: str
    subject: str  # what the record is called, as the person knows it
    item: str  # what is missing, in a few words
    record_id: uuid.UUID
    code: str = ""  # which gap, for code to act on rather than reading ``item``


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


def is_untrimmed(pass_: Any, videos: dict[uuid.UUID, Any]) -> bool:
    """Whether a pass still spans its whole recording, as an import leaves it."""
    total = sum((videos[v].duration_s or 0.0) for v in pass_.video_ids() if v in videos)
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
        gaps.append(Gap(EXPEDITION, campaign.name, "start and end dates", campaign.id, EXPEDITION_DATES))

    seen_transects: list[uuid.UUID] = []
    for pass_ in passes:
        if pass_.transect_id and pass_.transect_id not in seen_transects:
            seen_transects.append(pass_.transect_id)
    for transect_id in seen_transects:
        transect = transects.get(transect_id)
        if transect is None:
            continue
        if transect.end_points() is None:
            gaps.append(Gap(TRANSECT, transect.name, "start and end coordinates", transect.id, TRANSECT_COORDINATES))
        if transect.length_m is None:
            gaps.append(Gap(TRANSECT, transect.name, "tape length", transect.id, TRANSECT_LENGTH))
        if transect.depth_m is None:
            gaps.append(Gap(TRANSECT, transect.name, "depth", transect.id, TRANSECT_DEPTH))
        if transect.site_id is None:
            gaps.append(Gap(TRANSECT, transect.name, "site", transect.id, TRANSECT_SITE))

    # Each clip named by where it was swum, so a clip list of GX0100…s can be
    # told apart and picked out a pass at a time.
    seen_clips: dict[uuid.UUID, str] = {}
    for pass_ in passes:
        transect = transects.get(pass_.transect_id) if pass_.transect_id else None
        first = videos.get(pass_.video_id)
        camera = (first.camera_label if first else None) or (first.file_name if first else "?")
        subject = " · ".join(
            part for part in (transect.name if transect else "Unassigned", pass_.label, pass_.direction, camera) if part
        )
        if pass_.transect_id is None:
            gaps.append(Gap(PASS, subject, "transect", pass_.id, PASS_TRANSECT))
        if pass_.direction is None:
            gaps.append(Gap(PASS, subject, "direction", pass_.id, PASS_DIRECTION))
        if first is not None and first.view == VIEW_DOWN:
            pass  # a down-looking camera is never processed, so no trim or run is owed
        else:
            if is_untrimmed(pass_, videos):
                gaps.append(Gap(PASS, subject, "start and end of the swim (not trimmed)", pass_.id, PASS_UNTRIMMED))
            runs = store.runs_for_pass(pass_.id)
            if not runs:
                gaps.append(Gap(PASS, subject, "not processed yet", pass_.id, PASS_NOT_RUN))
            elif runs[-1].status == "failed":
                gaps.append(Gap(PASS, subject, "last run failed", pass_.id, PASS_RUN_FAILED))
        if pass_.quality is None:
            gaps.append(Gap(PASS, subject, "quality", pass_.id, PASS_QUALITY))
        where = " · ".join(
            part for part in (transect.name if transect else "Unassigned", pass_.label, pass_.direction) if part
        )
        for video_id in pass_.video_ids():
            video = videos.get(video_id)
            if video is not None and video_id not in seen_clips:
                seen_clips[video_id] = f"{where} · {video.file_name}"

    for video_id, subject in seen_clips.items():
        video = videos[video_id]
        if not video.camera_label:
            gaps.append(Gap(CLIP, subject, "camera", video.id, CLIP_CAMERA))
        if not video.rig_position and video.view != VIEW_DOWN:
            gaps.append(Gap(CLIP, subject, "rig position", video.id, CLIP_RIG))
        if video.review == "unreviewed":
            gaps.append(Gap(CLIP, subject, "review", video.id, CLIP_REVIEW))
    return gaps
