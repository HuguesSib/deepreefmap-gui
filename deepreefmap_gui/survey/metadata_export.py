"""One flat table of everything entered or read about the footage.

A row is one clip inside one pass: the pass's filing (campaign, site, transect,
direction, quality, comment, window), the clip's own facts (what its container
said, which camera shot it, the review verdict), and what processing made of the
pass so far. A clip that no pass uses yet still gets a row, with the pass
columns blank, so the export is also the inventory of what is waiting.

The column names are the contract: they are what a spreadsheet, an R script or
the next import reads, so they change only by adding columns at the end and
bumping ``METADATA_SCHEMA_VERSION``.
"""

from __future__ import annotations

import csv
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from deepreefmap_gui.survey.labels import pass_label
from deepreefmap_gui.survey.models import TransectPass, VideoAsset

METADATA_SCHEMA_VERSION = 1

METADATA_COLUMNS = [
    "schema_version",
    # Where the swim belongs.
    "campaign",
    "site",
    "transect",
    "pass_label",
    "direction",
    "quality",
    "surveyed_on",
    "begin_s",
    "end_s",
    "pass_comment",
    # Which clip, in which order when a pass spans GoPro chapters.
    "clip_ordinal",
    "file_name",
    "path",
    "captured_at",
    "captured_source",
    "duration_s",
    "fps",
    "width",
    "height",
    "codec",
    "gravity",
    "gps",
    "size_bytes",
    # What a person said about the clip.
    "camera_label",
    "rig_position",
    "view",
    "upside_down",
    "review",
    "clip_notes",
    # What processing made of the pass.
    "runs",
    "latest_run_dir",
    "latest_run_status",
    # Ids last, for joining back to the database rather than for reading.
    "campaign_id",
    "transect_id",
    "pass_id",
    "video_id",
    "video_hash",
]


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _pass_numbers(passes: Iterable[TransectPass]) -> dict[uuid.UUID, int]:
    """Each pass's ordinal among the passes of its transect, as labels count them."""
    counters: dict[uuid.UUID | None, int] = {}
    numbers: dict[uuid.UUID, int] = {}
    for pass_ in passes:
        counters[pass_.transect_id] = counters.get(pass_.transect_id, 0) + 1
        numbers[pass_.id] = counters[pass_.transect_id]
    return numbers


def _clip_columns(video: VideoAsset | None, ordinal: int | None) -> dict[str, Any]:
    if video is None:
        return {"clip_ordinal": ordinal}
    return {
        "clip_ordinal": ordinal,
        "file_name": video.file_name,
        "path": video.path,
        "captured_at": video.captured_at,
        "captured_source": video.captured_source,
        "duration_s": video.duration_s,
        "fps": video.fps,
        "width": video.width,
        "height": video.height,
        "codec": video.codec,
        "gravity": video.gravity,
        "gps": video.gps,
        "size_bytes": video.size_bytes,
        "camera_label": video.camera_label,
        "rig_position": video.rig_position,
        "view": video.view,
        "upside_down": video.upside_down,
        "review": video.review,
        "clip_notes": video.notes,
        "video_id": str(video.id),
        "video_hash": video.hash,
    }


def build_metadata_rows(store: Any) -> list[dict[str, Any]]:
    """Every pass's clips, then every clip no pass uses, as flat rows."""
    videos: Mapping[uuid.UUID, VideoAsset] = {v.id: v for v in store.list_videos()}
    transects = {t.id: t for t in store.list_transects_for_reference()}
    sites = {s.id: s.name for s in store.list_sites()}
    campaigns = {c.id: c.name for c in store.list_campaigns()}
    passes = store.list_passes()
    numbers = _pass_numbers(passes)

    rows: list[dict[str, Any]] = []
    used: set[uuid.UUID] = set()
    for pass_ in passes:
        transect = transects.get(pass_.transect_id) if pass_.transect_id else None
        first = videos.get(pass_.video_id)
        runs = store.runs_for_pass(pass_.id)
        latest = runs[-1] if runs else None
        shared = {
            "campaign": campaigns.get(pass_.campaign_id) if pass_.campaign_id else None,
            "site": sites.get(transect.site_id) if transect and transect.site_id else None,
            "transect": transect.name if transect else None,
            "pass_label": pass_label(
                pass_,
                transect_name=transect.name if transect else None,
                clip_name=first.file_name if first else "",
                number=numbers[pass_.id],
            ),
            "direction": pass_.direction,
            "quality": pass_.quality,
            "surveyed_on": pass_.surveyed_on,
            "begin_s": pass_.begin_s,
            "end_s": pass_.end_s,
            "pass_comment": pass_.notes,
            "runs": len(runs),
            "latest_run_dir": latest.run_dir_name if latest else None,
            "latest_run_status": latest.status if latest else None,
            "campaign_id": str(pass_.campaign_id) if pass_.campaign_id else None,
            "transect_id": str(pass_.transect_id) if pass_.transect_id else None,
            "pass_id": str(pass_.id),
        }
        for ordinal, video_id in enumerate([pass_.video_id, *pass_.extra_video_ids], start=1):
            used.add(video_id)
            rows.append({**shared, **_clip_columns(videos.get(video_id), ordinal)})

    rows.extend(_clip_columns(video, None) for video in videos.values() if video.id not in used)
    return rows


def save_metadata_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    """Write the rows under the fixed header. Returns how many were written."""
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(METADATA_COLUMNS)
        for row in rows:
            values = {**row, "schema_version": METADATA_SCHEMA_VERSION}
            writer.writerow([_cell(values.get(name)) for name in METADATA_COLUMNS])
            count += 1
    return count
