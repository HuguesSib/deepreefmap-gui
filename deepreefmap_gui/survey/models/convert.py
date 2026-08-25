"""Model conversions: sqlite rows, the JSON document, and the manifest survey block."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import fields
from typing import Any, TypeVar, cast, get_args, get_origin, get_type_hints

from deepreefmap_gui.survey.models.batch_item import BatchItem
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.site import Site
from deepreefmap_gui.survey.models.survey_batch import SurveyBatch
from deepreefmap_gui.survey.models.transect import Transect
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset

T = TypeVar("T")

DOCUMENT_SCHEMA_VERSION = 2

# Insert order respects foreign keys: transects need sites, passes need
# transects/campaigns/videos/batches, batch items and runs need passes.
DOCUMENT_SECTIONS: dict[str, type] = {
    "sites": Site,
    "campaigns": Campaign,
    "transects": Transect,
    "videos": VideoAsset,
    "batches": SurveyBatch,
    "passes": TransectPass,
    "batch_items": BatchItem,
    "runs": RunRecord,
}


def to_row(model: Any) -> dict[str, Any]:
    """Flatten a model into a JSON- and sqlite-safe dict (UUIDs become strings)."""
    return {f.name: _encode(getattr(model, f.name)) for f in fields(model)}


def from_row(cls: type[T], row: Mapping[str, Any]) -> T:
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cast(Any, cls)):
        try:
            value = row[f.name]
        except (KeyError, IndexError):
            # A document exported before this field existed, so the default stands.
            continue
        hint = hints[f.name]
        # An optional container keeps its None: for those fields an empty value
        # is a recorded fact and an absent one is not, and collapsing the two
        # would lose the distinction the column exists to carry.
        if value is None and _is_optional(hint):
            kwargs[f.name] = None
            continue
        bare = _strip_none(hint)
        if get_origin(bare) is list:
            value = _decode_list(bare, value)
        elif get_origin(bare) is dict:
            value = _decode_dict(value)
        elif bare is bool:
            # sqlite has no boolean type, so the column comes back as 0 or 1.
            value = bool(value)
        elif value is not None and _accepts_uuid(hint):
            value = uuid.UUID(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def _encode(value: Any) -> Any:
    # A list of ids lands in one sqlite column, so it travels as a JSON array.
    if isinstance(value, list):
        return json.dumps([_encode(item) for item in value])
    # A settings dict travels the same way, as a JSON object.
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value) if isinstance(value, uuid.UUID) else value


def _decode_list(hint: Any, value: Any) -> list[Any]:
    """The JSON array sqlite holds, or an already-parsed list from a document."""
    raw = json.loads(value) if isinstance(value, str) else list(value or [])
    if get_args(hint) == (uuid.UUID,):
        return [item if isinstance(item, uuid.UUID) else uuid.UUID(item) for item in raw]
    return raw


def _decode_dict(value: Any) -> dict[str, Any]:
    """The JSON object sqlite holds, or an already-parsed mapping from a document."""
    raw = json.loads(value) if isinstance(value, str) else dict(value or {})
    return raw if isinstance(raw, dict) else {}


def _accepts_uuid(hint: Any) -> bool:
    return hint is uuid.UUID or uuid.UUID in get_args(hint)


def _is_optional(hint: Any) -> bool:
    return type(None) in get_args(hint)


def _strip_none(hint: Any) -> Any:
    """The hint with None removed, so `dict[...] | None` decodes as its dict."""
    args = tuple(a for a in get_args(hint) if a is not type(None))
    return args[0] if _is_optional(hint) and len(args) == 1 else hint


def build_document(
    *,
    transects: Iterable[Transect],
    videos: Iterable[VideoAsset],
    batches: Iterable[SurveyBatch],
    passes: Iterable[TransectPass],
    runs: Iterable[RunRecord],
    batch_items: Iterable[BatchItem] = (),
    sites: Iterable[Site] = (),
    campaigns: Iterable[Campaign] = (),
) -> dict[str, Any]:
    """One multi-object JSON document holding a whole survey."""
    sections = {
        "sites": sites,
        "campaigns": campaigns,
        "transects": transects,
        "videos": videos,
        "batches": batches,
        "passes": passes,
        "batch_items": batch_items,
        "runs": runs,
    }
    doc: dict[str, Any] = {"schema_version": DOCUMENT_SCHEMA_VERSION}
    for name, models in sections.items():
        doc[name] = [to_row(m) for m in models]
    return doc


def parse_document(doc: Mapping[str, Any]) -> dict[str, list[Any]]:
    version = doc.get("schema_version")
    if version != DOCUMENT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported survey document schema_version: {version}")
    return {
        name: [from_row(cls, row) for row in doc.get(name, [])]
        for name, cls in DOCUMENT_SECTIONS.items()
    }


def run_provenance(
    config: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Software, taxonomy and configuration identity that produced a run.

    Model names and the library version are already at the manifest top level
    (``segmentation_model``, ``mapping_backend``, ``deepreefmap_version``). The
    GUI version and the class-group taxonomy are not, so they are recorded here.

    ``model_versions`` maps each model repo to the HuggingFace commit revision in
    the cache when the run launched. That id is assigned by HuggingFace, not
    generated here, so it resolves back to the exact snapshot at the source. It is
    best-effort provenance: the version present, not a guarantee the run loaded
    it. Pinning the load itself is a library concern and is not done here.

    ``config`` names the organisation preset behind the run and any setting that
    deviated from it (see ``survey.preset.manifest_config_block``). It is omitted
    when unknown rather than written empty, so an audit can tell "nothing changed"
    from "nothing was recorded".
    """
    from deepreefmap_gui.cover import taxonomy_hash, taxonomy_version
    from deepreefmap_gui.packaging.releases import current_version

    block: dict[str, Any] = {
        "gui_version": current_version(),
        "taxonomy_version": taxonomy_version(),
        "taxonomy_hash": taxonomy_hash(),
    }
    if model_versions:
        block["model_versions"] = dict(model_versions)
    if config is not None:
        block["config"] = config
    return block


def survey_manifest_block(
    run: RunRecord,
    pass_: TransectPass,
    transect: Transect | None,
    batch: SurveyBatch | None,
    provenance: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
    site: Site | None = None,
    campaign: Campaign | None = None,
) -> dict[str, Any]:
    """The ``survey`` entry embedded in run_manifest.json.

    Snapshots enough of the pass and transect that a copied output folder can rebuild
    the database from manifests alone (see SurveyStore.rebuild_from_scan), plus a
    provenance block naming the GUI, taxonomy, model versions and configuration
    behind the run's cover numbers.
    """
    return {
        "run_id": str(run.id),
        "batch_id": str(batch.id) if batch else None,
        "batch_name": batch.name if batch else None,
        "preset_name": batch.preset_name if batch else None,
        "provenance": provenance if provenance is not None else run_provenance(config, model_versions),
        "pass": {
            "id": str(pass_.id),
            "direction": pass_.direction,
            "begin_s": pass_.begin_s,
            "end_s": pass_.end_s,
            "surveyed_on": pass_.surveyed_on,
            "quality": pass_.quality,
            "campaign": None if campaign is None else {
                "id": str(campaign.id),
                "name": campaign.name,
                "begin_date": campaign.begin_date,
                "end_date": campaign.end_date,
            },
        },
        # None for a pass run without one. rebuild_from_scan reads the absence
        # rather than a placeholder, so a copied output folder restores the pass
        # unassigned instead of inventing a transect to hang it on.
        #
        # deleted_at is carried because a line retired in the registry still
        # scales the passes already swum on it. Reading the run later, that stamp
        # is the difference between a live transect and one somebody withdrew
        # after the fact, and it is only ever recorded here.
        "transect": None if transect is None else {
            "id": str(transect.id),
            "name": transect.name,
            "site": None if site is None else {
                "id": str(site.id),
                "name": site.name,
                "country": site.country,
                "region": site.region,
            },
            "start_lat": transect.start_lat,
            "start_lon": transect.start_lon,
            "end_lat": transect.end_lat,
            "end_lon": transect.end_lon,
            "length_m": transect.length_m,
            "depth_m": transect.depth_m,
            "deleted_at": transect.deleted_at,
        },
    }
