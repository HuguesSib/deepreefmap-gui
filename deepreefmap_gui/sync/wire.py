"""Translation between the desktop's rows and the registry's wire shape.

Every difference between the two models is handled here: the fields that never
leave the device, the join table the registry keeps for a pass's chapters, the
cover rows it keeps that this side holds only as JSON in a run directory, and the
timestamp format. Nothing here talks to a network or to sqlite, so all of it is
testable on plain dicts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

from deepreefmap_gui.survey.analysis import LongCoverRow
from deepreefmap_gui.survey.models.convert import to_row
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import contract

logger = logging.getLogger(__name__)

PASS_VIDEOS = "pass_videos"
COVER_ROWS = "cover_rows"
PRESETS = "presets"
CAMERA_PROFILES = "camera_profiles"
CAMERA_CALIBRATIONS = "camera_calibrations"

# The only estimator that travels. The pooled figure is a pure function of the
# per-pass counts, denominators and the latest-run-per-pass rule, so storing it
# centrally would invite two disagreeing numbers.
PER_PASS = "per_pass"

# Foreign-key order, which is the order a push document has to present. The two
# derived sections sit behind the rows they hang off: chapters after their pass,
# cover after its run.
WIRE_SECTIONS: tuple[str, ...] = (
    "sites",
    "campaigns",
    "transects",
    "videos",
    "passes",
    PASS_VIDEOS,
    # Registry-published run settings, pull-only, ahead of the runs that name
    # the preset they ran under.
    PRESETS,
    # The lenses, pull-only too, and ahead of the runs that name the calibration
    # they were rectified with. A calibration follows the profile it measures.
    CAMERA_PROFILES,
    CAMERA_CALIBRATIONS,
    "runs",
    COVER_ROWS,
)


def _assert_sections_match_the_contract() -> None:
    """The vendored artefact and this module must name the same sections.

    The section list is what the client declares it can read, and it decides which
    rows the registry sends. An artefact vendored out of step with this module
    would have the app asking for a section it cannot land, or silently declining
    one it can. Checked at import so a mis-vendored file is a failed start-up
    rather than a half-applied sync on a boat.
    """
    if tuple(contract.SECTIONS) == WIRE_SECTIONS:
        return
    unreadable = sorted(set(contract.SECTIONS) - set(WIRE_SECTIONS))
    unpublished = sorted(set(WIRE_SECTIONS) - set(contract.SECTIONS))
    raise AssertionError(
        f"the vendored contract lists {contract.SECTIONS} and this build reads "
        f"{WIRE_SECTIONS}. Sections it names that cannot be read: {unreadable}. "
        f"Sections read here that it does not name: {unpublished}."
    )


_assert_sections_match_the_contract()

# Fields that stay on the device. A path and an mtime describe this laptop's disk,
# so sending them would put absolute paths in a shared registry; probed_at is
# local workflow. video_id and extra_video_ids leave as pass_video rows instead.
#
# A pass keeps its batch_id here because a pass belongs to many sessions over its
# life -- the column names only the latest, which would be a fact about this
# device's queue. A run's batch_id does travel, from contract 3: a run happened
# once, in one session, and which runs went through together is provenance the
# console groups by.
_DEVICE_LOCAL: dict[str, tuple[str, ...]] = {
    "videos": ("path", "mtime", "probed_at"),
    "passes": ("batch_id", "video_id", "extra_video_ids"),
}

# Every column the registry types as a timestamp. campaign.begin_date and
# end_date are days, not moments, and are deliberately not in here.
_TIMESTAMPS = frozenset({
    "created_at",
    "updated_at",
    "deleted_at",
    "started_at",
    "finished_at",
    "captured_at",
    "validated_at",
})

# Fixed namespaces, so a derived id is the same id on every device and across
# every push: uuid5 makes it a function of what it describes rather than of when
# it was built, which is what makes re-pushing idempotent.
_PASS_VIDEO_NAMESPACE = uuid.UUID("6b1f4a52-0f8e-5c7d-9a3b-2ad4c8e17f01")
_COVER_ROW_NAMESPACE = uuid.UUID("c04e7d19-3b52-5f68-8d21-9e7b41ac6d02")

_PROVENANCE_FIELDS = (
    "gui_version",
    "library_version",
    "segmentation_model",
    "mapping_backend",
    "processing_width",
    "processing_height",
    "fps",
    "preprocess_batch_size",
    "taxonomy_version",
    "taxonomy_hash",
    "model_revisions",
    "preset_name",
    "preset_version",
    "preset_hash",
    "preset_deviations",
    "run_duration_s",
    "stage_durations",
    "stage_peaks",
    "performance_observation",
    "camera_profile",
    "camera_calibration_id",
    "pixel_size_m",
    "scale_type",
    "transect_length_m",
    "crop_width_m",
)


# --- Identity ---


def pass_video_id(pass_id: Any, video_id: Any) -> uuid.UUID:
    """The join row's id, derived from the pair it joins."""
    return uuid.uuid5(_PASS_VIDEO_NAMESPACE, f"{pass_id}:{video_id}")


def cover_row_id(run_id: Any, level: str, class_group: str, estimator: str) -> uuid.UUID:
    """The cover row's id, derived from the registry's own unique key for it."""
    return uuid.uuid5(_COVER_ROW_NAMESPACE, f"{run_id}:{level}:{class_group}:{estimator}")


# --- Timestamps ---


def to_wire_time(value: str | None) -> str | None:
    """A stored stamp as the registry writes them: UTC, RFC 3339, ``Z`` suffix."""
    moment = _parse_time(value)
    return value if moment is None else moment.isoformat().replace("+00:00", "Z")


def from_wire_time(value: str | None) -> str | None:
    """A wire stamp as ``utc_now_iso`` writes them: UTC, ``+00:00``.

    Last-write-wins compares these as strings, so a stamp carrying an offset would
    compare wrongly against everything this side has ever written.
    """
    moment = _parse_time(value)
    return value if moment is None else moment.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    """The moment a stamp names, or None for an empty or unreadable one."""
    if not value:
        return None
    text = str(value)
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Leaving unreadable timestamp %r as it stands", value)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _restamp(row: dict[str, Any], convert: Callable[[str | None], str | None]) -> dict[str, Any]:
    return {k: convert(v) if k in _TIMESTAMPS else v for k, v in row.items()}


# --- Outbound ---


def rows_to_wire(section: str, models: Iterable[Any]) -> list[dict[str, Any]]:
    """Desktop models as wire rows for one of the sections backed by a table.

    ``device_id`` travels even though the registry stamps it from the credential
    and discards what it was sent, because pull output is guaranteed to be valid
    push input field for field. Provenance is the device, nothing personal:
    the schema carries no ``created_by``.
    """
    if section not in SYNC_SECTIONS:
        raise KeyError(f"{section!r} is not a section with a table behind it")
    dropped = _DEVICE_LOCAL.get(section, ())
    return [_outbound(to_row(model), dropped) for model in models]


def _outbound(row: dict[str, Any], dropped: tuple[str, ...]) -> dict[str, Any]:
    """One row as the registry reads it: ``head_seq`` travels as ``base_seq``."""
    out = {k: v for k, v in row.items() if k not in dropped and k != "head_seq"}
    out["base_seq"] = row.get("head_seq")
    return _restamp(out, to_wire_time)


def run_rows_to_wire(runs: Sequence[RunRecord], out_root: Path) -> list[dict[str, Any]]:
    """Run rows with their provenance, from the row and failing that the manifest.

    The row is the durable copy, written when the run finished. Anything it does
    not hold is looked for in the run directory, degrading to nulls where that
    has been pruned. Taken from the model rather than the encoded row so the JSON
    fields travel as objects, not text.

    Error strings and deviating paths are scrubbed on the way out: a pipeline
    error routinely embeds an absolute path, and an absolute path routinely
    embeds a username. The registry is shared, so it gets neither.
    """
    wire_rows = []
    for run, row in zip(runs, rows_to_wire("runs", runs), strict=True):
        stored = {name: getattr(run, name) for name in _PROVENANCE_FIELDS}
        if any(value is None for value in stored.values()):
            # Field by field, not all or nothing. A row stamped by a build that
            # knew only some of these columns is not a legacy row, so reading it
            # whole left the columns that build never wrote pushing as nulls
            # while the manifest beside it held every one of them.
            from_manifest = run_provenance(out_root, run.run_dir_name)
            stored = {
                name: value if value is not None else from_manifest[name]
                for name, value in stored.items()
            }
        merged = {**row, **stored}
        if merged.get("error"):
            merged["error"] = scrub_home_paths(str(merged["error"]))
        if isinstance(merged.get("preset_deviations"), dict):
            merged["preset_deviations"] = _scrub_deviations(merged["preset_deviations"])
        if merged.get("performance_observation"):
            merged["performance_observation"] = _performance_for_wire(merged["performance_observation"])
        wire_rows.append(merged)
    return wire_rows


# Home directories on the three platforms field laptops run. The username is
# the segment after the prefix, which is exactly the part that must not travel.
_HOME_PREFIXES = re.compile(
    r'(?:[A-Za-z]:[\\/]Users[\\/][^\\/\s:*?"<>|]+|/home/[^/\s]+|/Users/[^/\s]+)'
)


def scrub_home_paths(text: str) -> str:
    """Every home-directory prefix in ``text`` replaced with ``~``."""
    return _HOME_PREFIXES.sub("~", text)


def _scrub_deviations(deviations: dict[str, Any]) -> dict[str, Any]:
    """Deviating values that are paths reduced to their file names.

    A machine-local checkpoint path says nothing to the registry beyond which
    file was used, and the rest of it describes somebody's disk.
    """
    scrubbed: dict[str, Any] = {}
    for key, value in deviations.items():
        if isinstance(value, str) and (
            value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            scrubbed[key] = PurePath(value.replace("\\", "/")).name
        else:
            scrubbed[key] = value
    return scrubbed


def _performance_for_wire(value: Any) -> Any:
    """Return observations with local paths replaced by distinct opaque labels."""
    if isinstance(value, dict):
        return {key: _performance_for_wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_performance_for_wire(item) for item in value]
    if isinstance(value, str) and (value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", value)):
        name = PurePath(value.replace("\\", "/")).name
        digest = hashlib.sha256(value.encode()).hexdigest()[:16]
        return f"{name} ({digest})"
    return value


def pass_video_rows(pass_: TransectPass) -> list[dict[str, Any]]:
    """A pass's chapters as the registry's join table, ordinal zero-based.

    The relationship is the pass's, so the rows carry the pass's own stamps and go
    to a tombstone with it.
    """
    stamps = {
        "created_at": to_wire_time(pass_.created_at),
        "updated_at": to_wire_time(pass_.updated_at),
        "deleted_at": to_wire_time(pass_.deleted_at),
    }
    return [
        {
            "id": str(pass_video_id(pass_.id, video_id)),
            "pass_id": str(pass_.id),
            "video_id": str(video_id),
            "ordinal": ordinal,
            **stamps,
        }
        for ordinal, video_id in enumerate(pass_.video_ids())
    ]


def cover_rows_to_wire(
    rows: Iterable[LongCoverRow],
    runs: Mapping[str, RunRecord],
    out_root: Path,
) -> list[dict[str, Any]]:
    """Per-pass cover as wire rows, for the runs travelling in the same document.

    A row for a run the registry has never seen is a 409, so anything outside
    ``runs`` is left for the push that carries its run. Cover is a function of the
    run, so a row takes the run's stamps: a recomputed figure lands only when the
    run row itself is newer, which is the same limitation the run's own provenance
    columns already have.
    """
    sources: dict[str, str | None] = {}
    wire_rows = []
    for row in rows:
        if row.estimator != PER_PASS:
            continue
        run = runs.get(row.run_id)
        if run is None:
            continue
        if row.run_dir_name not in sources:
            sources[row.run_dir_name] = metric_source(out_root, row.run_dir_name)
        wire_rows.append({
            "id": str(cover_row_id(row.run_id, row.level, row.group, row.estimator)),
            "run_id": row.run_id,
            "level": row.level,
            # LongCoverRow calls these two `group` and `count`.
            "class_group": row.group,
            "estimator": row.estimator,
            "fraction": row.fraction,
            "point_count": row.count,
            "denominator": row.denominator,
            "metric_source": sources[row.run_dir_name],
            "created_at": to_wire_time(run.created_at),
            "updated_at": to_wire_time(run.updated_at),
            "deleted_at": to_wire_time(run.deleted_at),
        })
    return wire_rows


# --- Run manifests ---


def run_provenance(out_root: Path, run_dir_name: str) -> dict[str, Any]:
    """What produced a run, from the manifest in its directory.

    A pruned or half-written run directory reads as nulls rather than stopping
    the whole push.
    """
    return provenance_from_manifest(_manifest(out_root, run_dir_name))


def provenance_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What produced a run, from its manifest, as nulls where it cannot be read.

    Software identity and the configuration the run ran at sit at the manifest's
    top level, written by ``profiling.instrumentation``; taxonomy and preset
    identity sit under ``survey.provenance``, written by
    ``convert.survey_manifest_block``. This is the reading the row is stamped
    from when a run finishes. A manifest written before any of these keys existed
    degrades to nulls.

    Takes the document rather than a directory because a run that died may never
    have written one, and ``instrumentation.failed_run_manifest`` builds the same
    shape in memory so its peaks reach the row through this same reading.
    """
    provenance: dict[str, Any] = dict.fromkeys(_PROVENANCE_FIELDS)
    provenance["library_version"] = _text(manifest.get("deepreefmap_version"))
    provenance["segmentation_model"] = _text(manifest.get("segmentation_model"))
    provenance["mapping_backend"] = _text(manifest.get("mapping_backend"))

    # What the run actually processed at: the resolution presets resolve to real
    # pixel counts in the form before launch, and these are the launch parameters
    # the pipeline was handed. The same four are the local timing profile's key,
    # so a registry peak and a local estimate stay comparable. A run's frame rate
    # is whole wherever it is set, which is why it reads as one here; a clip's own
    # fps is a different quantity and travels as a float on the video row.
    provenance["processing_width"] = _whole(manifest.get("processing_width"))
    provenance["processing_height"] = _whole(manifest.get("processing_height"))
    provenance["fps"] = _whole(manifest.get("fps"))
    provenance["preprocess_batch_size"] = _whole(manifest.get("preprocess_batch_size"))

    survey = _block(manifest, "survey")
    block = _block(survey, "provenance")
    config = _block(block, "config")
    provenance["gui_version"] = _text(block.get("gui_version"))
    provenance["taxonomy_version"] = _whole(block.get("taxonomy_version"))
    provenance["taxonomy_hash"] = _text(block.get("taxonomy_hash"))
    provenance["model_revisions"] = block.get("model_versions") or None
    provenance["preset_name"] = _text(config.get("preset_name")) or _text(survey.get("preset_name"))
    provenance["preset_version"] = _whole(config.get("preset_version"))
    provenance["preset_hash"] = _text(config.get("preset_hash"))
    # An empty deviations map means "nothing departed from the preset", which is a
    # different fact from a run that recorded no configuration at all.
    if "deviations" in config:
        provenance["preset_deviations"] = config["deviations"]
    provenance["run_duration_s"] = _seconds(manifest.get("run_duration_s"))
    provenance["stage_durations"] = _block(manifest, "stage_durations") or None
    provenance["stage_peaks"] = _block(manifest, "stage_peaks") or None
    provenance["performance_observation"] = _block(manifest, "performance_observation") or None
    # The scale the cover was measured at. The tape length and crop width are the
    # ones the run used, which may differ from the transect's current reading.
    provenance["camera_profile"] = _text(manifest.get("camera_profile"))
    provenance["camera_calibration_id"] = _text(manifest.get("camera_calibration_id"))
    provenance["pixel_size_m"] = _seconds(manifest.get("pixel_size_m"))
    provenance["scale_type"] = _text(manifest.get("scale_type"))
    transect = _block(manifest, "transect")
    provenance["transect_length_m"] = _seconds(transect.get("length"))
    provenance["crop_width_m"] = _seconds(transect.get("crop_width"))
    return provenance


def metric_source(out_root: Path, run_dir_name: str) -> str | None:
    """Which cloud the run's cover was measured on, or None when unrecorded.

    Read from the manifest's ``enable_tsdf``: fusion is what swaps the metric
    cloud. A run that enabled fusion and produced no fused points fell back to
    the unprojected cloud, and nothing in the manifest says so.
    """
    manifest = _manifest(out_root, run_dir_name)
    if "enable_tsdf" not in manifest:
        return None
    return "tsdf" if manifest["enable_tsdf"] else "unprojected"


def _manifest(out_root: Path, run_dir_name: str) -> dict[str, Any]:
    path = out_root / run_dir_name / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.info("No readable run manifest for %s", run_dir_name)
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _block(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return str(value) if value else None


def _whole(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Inbound ---


def rows_from_wire(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Wire rows as this side names things, each left as partial as it arrived.

    Partial matters: the store writes only the fields a row carried, so a clip's
    path and a run's session survive an update from a registry that holds neither.
    ``server_seq`` lands as ``head_seq``, the position the row was seen at.
    """
    return [_inbound(row) for row in rows]


def _inbound(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "server_seq"}
    if row.get("server_seq") is not None:
        out["head_seq"] = int(row["server_seq"])
    return _restamp(out, from_wire_time)


def push_outcomes(response: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each section's answer in one shape, whichever contract the registry spoke.

    Contract 1 answered with an ``applied`` count and bare id lists under
    ``skipped``, ``refused`` and ``conflicted``. Contract 2 answers with the
    ledger's buckets: ``applied`` as acknowledgements carrying the row's new
    position, and ``superseded``, ``proposed`` and ``rejected`` each naming a
    reason. The keys here are the ledger's; a stale supersession is a skip.
    """
    outcomes = {}
    for name, answer in (response.get("sections") or {}).items():
        if isinstance(answer.get("applied"), list):
            acks = {
                str(ack.get("id")): int(ack.get("seq", 0)) for ack in answer["applied"]
            }
            superseded = answer.get("superseded") or ()
            outcomes[name] = {
                "received": int(answer.get("received", 0)),
                "acks": acks,
                "skipped": [str(r.get("id")) for r in superseded if r.get("reason") == "stale"],
                "refused": [str(r.get("id")) for r in superseded if r.get("reason") != "stale"],
                "proposed": [str(r.get("id")) for r in answer.get("proposed") or ()],
                "rejected": [str(r.get("id")) for r in answer.get("rejected") or ()],
            }
            continue
        outcomes[name] = {
            "received": int(answer.get("received", 0)),
            "acks": {},
            "applied": int(answer.get("applied", 0)),
            "skipped": [str(v) for v in answer.get("skipped") or ()],
            "refused": [str(v) for v in answer.get("refused") or ()],
            "proposed": [],
            "rejected": [str(v) for v in answer.get("conflicted") or ()],
        }
    return outcomes


def unknown_sections(sections: Mapping[str, Any]) -> tuple[str, ...]:
    """Section names in a page that this build has no reading of, sorted.

    A section named with nothing in it is not data, so it is not in here: only a
    section carrying rows can cost the caller anything by being passed over.
    """
    return tuple(sorted(
        name
        for name, rows in sections.items()
        if name not in WIRE_SECTIONS and isinstance(rows, (list, tuple)) and rows
    ))


def fold_pass_videos(
    pass_rows: Iterable[Mapping[str, Any]],
    pass_video_rows_in: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[uuid.UUID]]]:
    """Collapse the join table back onto the passes it belongs to.

    Returns the pass rows with ``video_id`` and ``extra_video_ids`` filled, and the
    chapter lists whose pass was not among them, keyed by pass id, for the caller
    to apply to a pass it already holds.

    A pass with no live chapter row gets neither field, so a pass this device
    already has keeps the chapters it knows. A pass it has never seen cannot be
    built at all, because the model has no representation for a pass with no
    video, and the caller reports it rather than inventing one.
    """
    by_pass: dict[str, list[Mapping[str, Any]]] = {}
    for row in pass_video_rows_in:
        by_pass.setdefault(str(row["pass_id"]), []).append(row)
    folded = []
    for row in pass_rows:
        video_ids = video_ids_from_pass_videos(by_pass.pop(str(row["id"]), []))
        chapters = (
            {
                "video_id": str(video_ids[0]),
                "extra_video_ids": [str(v) for v in video_ids[1:]],
            }
            if video_ids
            else {}
        )
        folded.append({**row, **chapters})
    return folded, {
        pass_id: video_ids_from_pass_videos(rows) for pass_id, rows in by_pass.items()
    }


def video_ids_from_pass_videos(rows: Iterable[Mapping[str, Any]]) -> list[uuid.UUID]:
    """The chapters of one pass, in playing order.

    A tombstoned row is a chapter that was taken off the pass. Ordinals only
    order, so a gap in them means nothing; a repeat should be impossible and is
    broken by row id so two devices reading the same rows agree. A clip named
    twice keeps its first place, because the pass model refuses a chapter that is
    also its first video.
    """
    live = [row for row in rows if not row.get("deleted_at")]
    ordered = sorted(live, key=lambda row: (_whole(row.get("ordinal")) or 0, str(row.get("id", ""))))
    video_ids: list[uuid.UUID] = []
    for row in ordered:
        video_id = uuid.UUID(str(row["video_id"]))
        if video_id not in video_ids:
            video_ids.append(video_id)
    return video_ids
