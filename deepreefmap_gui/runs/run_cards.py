"""Run metadata formatting, shared by every widget that describes a run.

Formatting only: the widgets that arrange these strings (the run table, the
detail pane, the view bar) each own their own layout.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from deepreefmap_gui.core.theme import (
    PRIMARY,
    WARNING,
)
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey.catalogue import RunEntry
from deepreefmap_gui.survey.models import direction_text

_GEOMETRY_LABELS = {
    "world_points": "world points (full)",
    "depth_unprojection": "depth-unprojection",
}

# The mapper's estimate of geometry when it cannot solve world points.
DEPTH_FALLBACK = "depth_unprojection"

# A column with nothing in it. The bare token, because test_design_system.py
# fails any other spelling of an em dash in the tree.
_MISSING = "—"


def format_timestamp(value: object) -> str:
    """Render an ISO-8601 run_timestamp as a short local date/time, else passthrough."""
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def related_run_counts(entries: list[tuple[Path, dict]]) -> dict[Path, int]:
    """Per run dir, how many sibling runs share a video_hash with it.

    Old manifests have no video_hashes and never count as related.
    """
    dirs_by_hash: dict[str, set[Path]] = {}
    for run_dir, manifest in entries:
        for h in manifest.get("video_hashes") or []:
            if h:
                dirs_by_hash.setdefault(h, set()).add(run_dir)
    counts: dict[Path, int] = {}
    for run_dir, manifest in entries:
        related: set[Path] = set()
        for h in manifest.get("video_hashes") or []:
            if h:
                related |= dirs_by_hash[h]
        related.discard(run_dir)
        counts[run_dir] = len(related)
    return counts


def geometry_label(manifest: dict) -> str:
    """How a run got its 3D, and a warning when it fell back to depth.

    A mapper that cannot solve world points estimates them by unprojecting
    depth instead. The result is materially weaker and the manifest is the only
    place that records which happened, so a run carrying the fallback says so
    wherever it is described rather than reading like any other success.
    """
    source = manifest.get("geometry_source")
    if not source:
        return ""
    label = _GEOMETRY_LABELS.get(source, str(source))
    return f"⚠ {label}" if source == DEPTH_FALLBACK else label


def points_label(n: int) -> str:
    """A point count at reading precision: "4.6M", "988k", "14"."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def frames_text(manifest: dict) -> str:
    """How many frames a run processed, and at what rate: "1,240 @ 4 fps"."""
    frames = manifest.get("frames_processed")
    if not frames:
        return _MISSING
    text = f"{int(frames):,}"
    fps = manifest.get("fps")
    return f"{text} @ {fps} fps" if fps else text


def run_facts_line(manifest: dict) -> str:
    """The size of an open run, as rich text for the view bar.

    Two facts, because everything else about the run is a row in the detail
    pane beside it and the bar exists to leave room for the cloud. The third
    appears only for a run whose geometry was estimated from depth: that is the
    one manifest fact which changes how the cloud in front of you may be read,
    so it is on screen whether or not the pane is open.
    """
    facts: list[str] = []
    if manifest.get("frames_processed"):
        facts.append(frames_text(manifest))
    points = manifest.get("semantic_reference_points") or manifest.get("metric_points")
    if points:
        facts.append(f"{points_label(int(points))} points")
    if manifest.get("geometry_source") == DEPTH_FALLBACK:
        facts.append(f'<span style="color: {WARNING}">{geometry_label(manifest)}</span>')
    return " · ".join(facts)


def video_details(manifest: dict, index: int = 0) -> list[str]:
    """Short hash, size, and recording date for one input video, where known."""
    details: list[str] = []
    hashes = manifest.get("video_hashes") or []
    sizes = manifest.get("video_sizes") or []
    mtimes = manifest.get("video_mtimes") or []
    if index < len(hashes) and hashes[index]:
        details.append(f"#{str(hashes[index])[:8]}")
    if index < len(sizes) and sizes[index]:
        details.append(format_bytes(float(sizes[index])))
    if index < len(mtimes) and mtimes[index]:
        stamp = format_timestamp(mtimes[index])
        if stamp:
            details.append(stamp)
    return details


def format_trim_range(manifest: dict) -> str | None:
    """The processed slice of the video, shown only when the run was trimmed."""
    begin = manifest.get("begin_s")
    end = manifest.get("end_s")
    if begin is None and end is None:
        return None
    begin_txt = f"{float(begin):.1f}" if begin is not None else "0"
    end_txt = f"{float(end):.1f}s" if end is not None else "end"
    return f"{begin_txt}-{end_txt}"


def _video_line(entry: RunEntry) -> str:
    """The clip, with its checksum and size, and a count of any others."""
    videos = entry.manifest.get("input_videos") or []
    if not videos:
        return _MISSING
    details = video_details(entry.manifest, 0)
    line = Path(videos[0]).name + (f" ({', '.join(details)})" if details else "")
    if len(videos) > 1:
        line += f" (+{len(videos) - 1} more)"
    return line


def recorded_text(entry: RunEntry) -> str:
    """When the footage was shot, in local time. Empty when nothing recorded it."""
    stamp = entry.recorded_at
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M") if stamp is not None else ""


def _column_lines(entry: RunEntry) -> list[str]:
    """One line per column the run table shows, present whether or not it has a value.

    The tooltip is opened over a column in order to read that column, so a line
    quietly missing is the one answer it must never give. These are labelled with
    the column headings themselves, so what is being pointed at is obvious.
    """
    from deepreefmap_gui.survey import catalogue

    manifest = entry.manifest
    frames = manifest.get("frames_processed")
    fps = manifest.get("fps")
    return [
        f"Status: {catalogue.entry_status(entry).capitalize()}",
        f"Created: {format_timestamp(manifest.get('run_timestamp')) or _MISSING}",
        f"Transect: {entry.transect_name or 'Not assigned yet'}",
        f"Direction: {direction_text(entry.direction) or _MISSING}",
        f"Recorded: {recorded_text(entry) or _MISSING}",
        # No column shows the session, so the tooltip is where it lives; in the
        # always-shown block because its absence is a fact too.
        f"Session: {entry.session_name or _MISSING}",
        f"Video: {_video_line(entry)}",
        f"Frames: {f'{int(frames):,}' if frames else _MISSING}"
        + (f" @ {fps} fps" if frames and fps else ""),
        f"Points: {f'{int(entry.points):,}' if entry.points else _MISSING}",
        f"Runtime: {format_duration(entry.duration_s) if entry.duration_s else _MISSING}",
        f"Size: {format_bytes(entry.size_bytes) if entry.size_bytes is not None else _MISSING}",
    ]


def _detail_lines(entry: RunEntry) -> list[str]:
    """How the run was made. No column shows these, so they stay conditional:
    "no camera profile recorded" is a real difference from "not applicable"."""
    manifest = entry.manifest
    lines: list[str] = []
    # Later attempts carry their number in the dir name; the first says nothing.
    attempt = re.search(r"__r(\d+)$", entry.dir_name)
    if attempt:
        lines.append(f"Attempt: {int(attempt.group(1))}")
    for label, key in (("Mode", "mode"), ("Segmentation", "segmentation_model")):
        if manifest.get(key):
            lines.append(f"{label}: {manifest[key]}")
    if manifest.get("mapping_backend"):
        lines.append(f"Mapping: {manifest['mapping_backend']}")
    mopts = manifest.get("mapping_options") or {}
    if mopts.get("window_size") is not None:
        lines.append(
            f"LoGeR window/overlap: {mopts.get('window_size')}/{mopts.get('overlap_size')}"
        )
    if manifest.get("refine_intrinsics_from_mapper"):
        lines.append("Intrinsics: refined from mapper")
    geom = geometry_label(manifest)
    if geom:
        lines.append(f"Geometry: {geom}")
    if manifest.get("camera_profile"):
        lines.append(f"Camera profile: {manifest['camera_profile']}")
    pw, ph = manifest.get("processing_width"), manifest.get("processing_height")
    if pw and ph:
        lines.append(f"Processing size: {pw}×{ph}")
    metric_pts = manifest.get("metric_points")
    if metric_pts:
        lines.append(f"Metric points: {int(metric_pts):,}")
    trim = format_trim_range(manifest)
    if trim:
        lines.append(f"Range: {trim}")
    return lines


def format_run_metadata(entry: RunEntry) -> str:
    """A run's whole record, as the run table's tooltip.

    Two blocks. First every column the table shows, always, so hovering a column
    always has a line to point at. Then the facts no column has room for, which
    are omitted when absent because their absence means something.
    """
    name = (entry.manifest.get("name") or "").strip() or entry.dir_name
    # The folder only when it adds something: an unnamed run takes its timestamp
    # as a name, so spelling both printed the same string twice.
    header = f"<b>{name}</b>"
    if name != entry.dir_name:
        header += f"  <i>({entry.dir_name})</i>"
    detail = _detail_lines(entry)
    blocks = [header, *_column_lines(entry)]
    if detail:
        blocks.append("")
        blocks.extend(detail)
    return "<br>".join(blocks)


def emphasise_line(metadata: str, label: str | None) -> str:
    """Lift the one line a label names out of a run's metadata block.

    A tooltip opened over the Video column lists a dozen facts, and the pointer
    is already resting on the one the reader wants. Finding it again in the list
    is work the interface can do for them.

    Lifted rather than filtered: the surrounding facts are the reason the tooltip
    is worth opening at all, so they stay exactly where they were and the eye is
    pointed at one of them.
    """
    if not label:
        return metadata
    prefix = f"{label}:"
    return "<br>".join(
        f'<span style="color: {PRIMARY}"><b>{line}</b></span>'
        if line.startswith(prefix)
        else line
        for line in metadata.split("<br>")
    )


def provenance_rows(manifest: dict) -> list[tuple[str, str]]:
    """What produced a run's numbers, as detail-pane rows.

    A cover figure is only usable if what made it can be named: the models, the
    class taxonomy those models' outputs were grouped under, and whether the
    settings deviated from the organisation standard. All three are already
    recorded in every manifest by ``survey_manifest_block``; until now none of
    them was shown anywhere, so a figure and its method lived in different files.

    Omitted rather than filled with a placeholder when a manifest predates the
    provenance block: "not recorded" and "nothing changed" are different claims,
    and only one of them is true of an old run.
    """
    survey = manifest.get("survey")
    provenance = survey.get("provenance") if isinstance(survey, dict) else None
    if not isinstance(provenance, dict):
        return []

    rows: list[tuple[str, str]] = []
    models = " + ".join(
        str(manifest[key])
        for key in ("segmentation_model", "mapping_backend")
        if manifest.get(key) and manifest.get(key) != "__skip__"
    )
    if models:
        rows.append(("Models", models))

    taxonomy = provenance.get("taxonomy_version")
    taxonomy_hash = provenance.get("taxonomy_hash")
    if taxonomy:
        # The hash is what actually pins the grouping; the version is what a
        # person can say out loud. Both, because a version can be edited in
        # place and the hash is what would catch it.
        label = str(taxonomy)
        if taxonomy_hash:
            label += f" · #{str(taxonomy_hash)[:8]}"
        rows.append(("Taxonomy", label))

    config = provenance.get("config")
    if isinstance(config, dict):
        rows.append(("Settings", _settings_line(config)))

    versions = [
        v for v in (manifest.get("deepreefmap_version"), provenance.get("gui_version")) if v
    ]
    if versions:
        rows.append(("Version", " · ".join(str(v) for v in versions)))
    return rows


def _settings_line(config: dict) -> str:
    """The preset a run used, and how far it strayed from it."""
    from deepreefmap_gui.survey.preset import describe_keys

    name = str(config.get("preset_name") or "Unnamed")
    deviations = config.get("deviations")
    if isinstance(deviations, dict) and deviations:
        return f"{name}, changed: {describe_keys(list(deviations))}"
    return f"{name}, as standard"


def summarise_run_provenance(run_dir_name: str, out_root: str) -> str:
    """"coralscapes-vit-b-dpt + loger_star, taxonomy v1" for one run, or "".

    Short enough to sit on one line beside a cover figure, and specific enough
    that two runs made differently do not read as the same measurement. Empty
    when the manifest is unreadable or predates the provenance block.
    """
    import json

    path = Path(out_root).expanduser() / run_dir_name / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(manifest, dict):
        return ""
    rows = dict(provenance_rows(manifest))
    parts = [rows[key] for key in ("Models", "Taxonomy") if rows.get(key)]
    if not parts:
        return ""
    # The taxonomy hash pins the grouping but is noise in a sentence; the
    # version is the part a person can compare at a glance.
    return f"{parts[0]}, taxonomy {parts[-1].split(' · ')[0]}" if len(parts) > 1 else parts[0]
