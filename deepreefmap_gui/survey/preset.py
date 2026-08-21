"""Two-layer survey settings: an organisation preset, and a per-machine override.

The organisation preset is the blessed configuration. It is read-only, carries a
name, a version and a content hash, and comes either from the bundled resource or
from an admin-provided file named by ``$DEEPREEFMAP_SURVEY_PRESET``. An admin file
is treated as authoritative: the preset it holds is locked.

The machine override is the small set of settings one laptop is allowed to differ
on. Only the keys in ``MACHINE_OVERRIDABLE_KEYS`` may be written there, so a
curious diver cannot permanently rebrand a field machine's
method. Everything else follows the organisation preset, and a deviation is named
to the user and recorded in the run manifest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from deepreefmap_gui.survey.store import SurveyStore

from deepreefmap_gui.io.atomic import atomic_write_text
from deepreefmap_gui.paths import survey_preset_path
from deepreefmap_gui.survey.preset_schema import coerce_settings

logger = logging.getLogger(__name__)

# Format of the preset YAML files this module reads and writes (the bundled
# survey_preset.yaml, an admin's DEEPREEFMAP_SURVEY_PRESET file, and the
# machine-override file). Bump when a file older than this needs migrating.
# Unrelated to the registry's published preset schema, which is
# preset_schema.PRESET_SCHEMA_VERSION.
PRESET_FILE_SCHEMA_VERSION = 3

# Which server preset this survey chose, stored beside the sync cursor because
# the runs it shapes live in that survey. Absent means no choice was made.
SERVER_PRESET_KEY = "preset.server_selection"

# The preset the registry assigned to this device, from the heartbeat answer.
# The default when the survey chose nothing itself; an explicit selection and
# an admin file both outrank it.
ASSIGNED_PRESET_KEY = "preset.server_assignment"

# Schema 1 held only the seven core settings. Schema 2 held every setting, as a
# whole copy of the preset per machine. Either is upgraded on read.
_SUPPORTED_VERSIONS = (1, 2, 3)

# Ceiling on quarantine copies, so a preset rewritten broken on every launch
# cannot fill the data dir. Corruption that survives this many attempts keeps
# the file in place and just logs, rather than spinning forever.
_MAX_QUARANTINE = 100

# A snapshot of every run-form setting, so the settings dialog can offer the full form
# and have the choices survive a restart. Per-pass values (transect length,
# begin/end trim) come from the survey database, never from here.
PRESET_KEYS = {
    "fps",
    "segmentation_name",
    "mapping_name",
    "camera_profile_name",
    "transect_crop_width",
    "enable_tsdf",
    "skip_segmentation",
    "resolution_preset",
    "processing_width",
    "processing_height",
    "preprocess_batch_size",
    "grid_bins",
    "require_gravity_telemetry",
    "replacement_radius_factor",
    "replacement_radius_estimation_frames",
    "replacement_radius_override",
    "loger_window_size",
    "loger_overlap_size",
    "loger_model_path",
    "refine_intrinsics_from_mapper",
    "scs_target_width",
    "scs_target_height",
    "scs_checkpoint_path",
}

# Identity, not settings: kept out of PRESET_KEYS so a preset dict stays a plain
# bag of run settings everywhere downstream.
PRESET_META_KEYS = ("preset_name", "preset_version")

# What one machine may differ on. The test is whether the key describes the
# computer rather than the method: anything that moves the cover numbers stays
# with the organisation, because repeat passes are only comparable if every
# laptop measured them the same way.
#
#   mapping_name          a machine with no graphics card cannot run the standard
#                         method at all, so it may fall back to one that works
#   preprocess_batch_size how many frames this machine's memory holds at once
#   camera_profile_name   which camera this team actually dives with
#   loger_model_path      where the weights sit on this machine's disk
#   scs_checkpoint_path   likewise
#
# Resolution is the deliberate omission. It is just as memory-driven as the batch
# size, but it changes the numbers, so it is the organisation's call and the
# memory grade warns instead.
MACHINE_OVERRIDABLE_KEYS = frozenset({
    "mapping_name",
    "preprocess_batch_size",
    "camera_profile_name",
    "loger_model_path",
    "scs_checkpoint_path",
})

# Plain-language names for the settings the interface has to talk about. Keys with
# names that already read as English fall through to _prettify.
_KEY_LABELS = {
    "mapping_name": "processing method",
    "segmentation_name": "coral identification model",
    "camera_profile_name": "camera",
    "preprocess_batch_size": "frames processed at once",
    "fps": "frames per second",
    "transect_crop_width": "transect width",
    "loger_model_path": "processing model file",
    "scs_checkpoint_path": "processing model file",
    "resolution_preset": "image resolution",
    "processing_width": "image width",
    "processing_height": "image height",
    "grid_bins": "map detail",
    "enable_tsdf": "surface fusion",
    "skip_segmentation": "skipping coral identification",
    "require_gravity_telemetry": "requiring camera tilt data",
    "refine_intrinsics_from_mapper": "camera lens refinement",
}

# An admin file that names itself is what the user sees. One that does not still
# must not borrow the bundled preset's name, or the audit would call two
# different configurations the same thing.
_UNNAMED_ADMIN_PRESET = "Organisation settings"
_UNNAMED_BUNDLED_PRESET = "Standard settings"


@dataclass(frozen=True)
class OrgPreset:
    """The blessed configuration: read-only settings plus the identity to cite."""

    name: str
    version: int
    settings: dict[str, Any]
    locked: bool
    # Named at construction for presets the registry published, because the
    # locked flag alone cannot tell those apart from the bundled default.
    origin: str = ""
    # Settings this build could not apply, as (key, why). Only a registry preset
    # can carry any: the bundled and administrator files are written against this
    # build's own form. A preset with entries here is offered and reported, never
    # silently patched, because the value that was dropped is the one the author
    # meant and substituting for it would misdescribe the run.
    dropped: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return f"{self.name} (v{self.version})"

    @property
    def content_hash(self) -> str:
        return preset_content_hash(self.settings)

    @property
    def degraded(self) -> bool:
        return bool(self.dropped)

    @property
    def dropped_summary(self) -> str:
        """One line naming what was ignored, for the interface to show."""
        if not self.dropped:
            return ""
        named = ", ".join(f"{describe_key(key)} ({why})" for key, why in self.dropped)
        return f"This registry preset names settings this version cannot apply: {named}."

    @property
    def source(self) -> str:
        if self.origin:
            return self.origin
        return "admin" if self.locked else "bundled"


@dataclass(frozen=True)
class ActivePreset:
    """The organisation preset with whatever this machine is allowed to change."""

    org: OrgPreset
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def settings(self) -> dict[str, Any]:
        return {**self.org.settings, **self.overrides}


@dataclass(frozen=True)
class OverrideResult:
    """What a save kept, what it would not keep, and where it landed."""

    saved: dict[str, Any]
    refused: dict[str, Any]
    path: Path | None


def preset_content_hash(settings: Mapping[str, Any]) -> str:
    """Short digest of the settings themselves, so a comment edit is not a change."""
    canonical = json.dumps({k: settings[k] for k in sorted(settings)}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def describe_key(key: str) -> str:
    return _KEY_LABELS.get(key, key.replace("_", " "))


def describe_keys(keys: Iterable[str]) -> str:
    """Plain-language list of settings, deduplicated: two paths share one name."""
    seen: list[str] = []
    for key in sorted(keys):
        name = describe_key(key)
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


def _bundled_text() -> str:
    bundled = resources.files("deepreefmap_gui.resources").joinpath("configs/survey_preset.yaml")
    return bundled.read_text(encoding="utf-8")


def _bundled_defaults() -> dict[str, Any]:
    """The shipped settings, read without validation so upgrades can lean on them."""
    data = yaml.safe_load(_bundled_text())
    data.pop("schema_version", None)
    for key in PRESET_META_KEYS:
        data.pop(key, None)
    return data


def load_org_preset() -> OrgPreset:
    """Resolve the blessed configuration: an admin file if one is named, else bundled.

    ``$DEEPREEFMAP_SURVEY_PRESET`` is the admin hook. Its presence means an
    administrator chose this configuration deliberately, so the result is locked:
    settings outside the allow-list cannot be overridden on the machine. A bad
    file there is an error to surface rather than something to paper over, and the
    same goes for the bundled resource, where a parse failure is a packaging fault.
    """
    override = os.environ.get("DEEPREEFMAP_SURVEY_PRESET")
    if override:
        text = Path(override).read_text(encoding="utf-8")
        name, version = parse_preset_identity(text, default_name=_UNNAMED_ADMIN_PRESET)
        return OrgPreset(name=name, version=version, settings=parse_preset(text), locked=True)
    text = _bundled_text()
    name, version = parse_preset_identity(text, default_name=_UNNAMED_BUNDLED_PRESET)
    return OrgPreset(name=name, version=version, settings=parse_preset(text), locked=False)


def registry_preset(name: str, version: int, settings: Mapping[str, Any]) -> OrgPreset:
    """A preset the registry published, as this build can run it.

    Nothing published to a registry is trusted here. A console can write any
    JSON into a preset, and this build may be older or newer than the one that
    wrote it, so every value is checked against `preset_schema` before it can
    reach a widget. Numbers are clamped to what the form accepts. A name no
    enumeration here offers is dropped rather than substituted, because a run
    under a different model than the preset names is worse than one that says
    it could not run at all.

    Keys the preset does not name take the shipped defaults, so the result is
    always a whole preset. Never locked: the server preset is additional to the
    standard, not an administrator's mandate.
    """
    known, dropped = coerce_settings(settings)
    if dropped:
        logger.warning(
            "Registry preset %s v%s: ignoring %s",
            name,
            version,
            "; ".join(f"{key} ({why})" for key, why in dropped),
        )
    return OrgPreset(
        name=name,
        version=int(version),
        settings={**_bundled_defaults(), **known},
        locked=False,
        origin="server",
        dropped=tuple(dropped),
    )


def load_machine_override(org: OrgPreset) -> dict[str, Any]:
    """The allow-listed settings this machine changed, or empty when it changed none.

    A file that cannot be read or parsed is moved aside and ignored, so one bad
    file on a field laptop never leaves the survey with no settings and every
    batch blocked.
    """
    path = survey_preset_path()
    if not path.is_file():
        return {}
    try:
        return parse_machine_override(path.read_text(encoding="utf-8"), org)
    except (OSError, ValueError, yaml.YAMLError):
        _quarantine_preset(path)
        return {}


def load_active_preset() -> ActivePreset:
    """The configuration a run will use: organisation preset plus machine changes."""
    org = load_org_preset()
    return ActivePreset(org=org, overrides=load_machine_override(org))


def load_survey_preset() -> dict[str, Any]:
    """The effective run settings, flat, as every form and gate consumes them."""
    return load_active_preset().settings


def _quarantine_preset(path: Path) -> None:
    """Rename an unreadable machine override aside, never clobbering an earlier copy.

    Matches the timing-profile quarantine in spirit: keep the evidence rather
    than let the next save overwrite it, but bound the copies so repeated
    corruption cannot grow without limit.
    """
    for n in range(1, _MAX_QUARANTINE + 1):
        aside = path.with_name(f"{path.name}.corrupt-{n}")
        if aside.exists():
            continue
        try:
            os.replace(path, aside)
        except OSError:
            logger.warning("Survey preset at %s is unreadable", path, exc_info=True)
        else:
            logger.warning("Survey preset at %s was unreadable; moved to %s", path, aside)
        return
    logger.warning(
        "Survey preset at %s is unreadable, and %d quarantine slots are taken; leaving it in place",
        path,
        _MAX_QUARANTINE,
    )


def deviations_from_org(settings: Mapping[str, Any], org: OrgPreset) -> dict[str, Any]:
    """Every setting that differs from the organisation preset, allow-listed or not.

    Used for what the user is told and what the manifest records, so a
    session-only edit counts exactly like a saved one: both change the run.
    """
    return {
        key: settings[key]
        for key in sorted(PRESET_KEYS & set(settings))
        if settings[key] != org.settings.get(key)
    }


def save_machine_override(settings: Mapping[str, Any], org: OrgPreset) -> OverrideResult:
    """Persist only the allow-listed settings this machine changed.

    Takes a whole preset and writes a subset, deliberately. The caller reads the
    entire run form, and the split between what a machine owns and what the
    organisation owns belongs here, in one place, rather than in each caller.
    """
    unknown = set(settings) - PRESET_KEYS
    if unknown:
        raise ValueError(f"Survey preset has unknown keys: {', '.join(sorted(unknown))}")
    missing = PRESET_KEYS - set(settings)
    if missing:
        raise ValueError(f"Survey preset is missing keys: {', '.join(sorted(missing))}")

    deviations = deviations_from_org(settings, org)
    saved = {k: v for k, v in deviations.items() if k in MACHINE_OVERRIDABLE_KEYS}
    refused = {k: v for k, v in deviations.items() if k not in MACHINE_OVERRIDABLE_KEYS}

    path = survey_preset_path()
    if not saved:
        # Nothing left to remember. Drop the file rather than leave an empty one,
        # so the machine tracks the organisation preset from here on.
        _unlink_quietly(path)
        return OverrideResult(saved={}, refused=refused, path=None)
    text = yaml.safe_dump(
        {"schema_version": PRESET_FILE_SCHEMA_VERSION, "overrides": saved}, sort_keys=False
    )
    atomic_write_text(path, text)
    return OverrideResult(saved=saved, refused=refused, path=path)


def clear_machine_override() -> bool:
    """Drop this machine's changes so it follows the organisation preset again.

    True when a file was actually removed, so the caller can tell "restored" from
    "there was nothing to restore".
    """
    return _unlink_quietly(survey_preset_path())


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not remove the machine settings at %s", path, exc_info=True)
        return False
    return True


def _schema_version(data: dict[str, Any]) -> int:
    version = data.pop("schema_version", None)
    if version not in _SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported survey preset schema_version: {version}")
    return int(version)


def _as_mapping(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Survey preset must be a YAML mapping.")
    return data


def parse_preset_identity(text: str, *, default_name: str) -> tuple[str, int]:
    """Name and version of an organisation preset, defaulted for older files."""
    data = _as_mapping(text)
    name = data.get("preset_name")
    version = data.get("preset_version")
    return (
        str(name) if isinstance(name, str) and name.strip() else default_name,
        int(version) if isinstance(version, int) else 1,
    )


def parse_preset(text: str) -> dict[str, Any]:
    """Strict full-preset parse, for the bundled resource and admin files.

    Returns settings only: identity comes from parse_preset_identity, so a preset
    dict stays a plain bag of run settings for the form to expand.
    """
    data = _as_mapping(text)
    version = _schema_version(data)
    for key in PRESET_META_KEYS:
        data.pop(key, None)
    if version < PRESET_FILE_SCHEMA_VERSION:
        # Keep the file's own choices, take the shipped value for everything
        # the older schema had no field for.
        data = {**_bundled_defaults(), **data}
    missing = PRESET_KEYS - set(data)
    if missing:
        raise ValueError(f"Survey preset is missing keys: {', '.join(sorted(missing))}")
    unknown = set(data) - PRESET_KEYS
    if unknown:
        raise ValueError(f"Survey preset has unknown keys: {', '.join(sorted(unknown))}")
    return data


def parse_machine_override(text: str, org: OrgPreset) -> dict[str, Any]:
    """The allow-listed deviations a machine file asks for.

    Schema 1 and 2 stored a whole copy of the preset per machine, which is what
    let one curious edit rewrite the method for every dive after. Such a
    file is read as a snapshot: the allow-listed settings that differ are kept as
    the machine's own, and the rest returns to the organisation preset.
    """
    data = _as_mapping(text)
    _schema_version(data)
    if "overrides" in data:
        raw = data.get("overrides") or {}
        if not isinstance(raw, dict):
            raise ValueError("Machine settings 'overrides' must be a YAML mapping.")
    else:
        # A retired key in an old snapshot is not worth quarantining a whole
        # file over, so unknown keys are dropped here rather than rejected.
        raw = {k: v for k, v in data.items() if k in PRESET_KEYS}
    unknown = set(raw) - PRESET_KEYS
    if unknown:
        raise ValueError(f"Machine settings have unknown keys: {', '.join(sorted(unknown))}")

    deviations = deviations_from_org(raw, org)
    kept = {k: v for k, v in deviations.items() if k in MACHINE_OVERRIDABLE_KEYS}
    ignored = sorted(set(deviations) - set(kept))
    if ignored:
        logger.warning(
            "Machine settings changed %s, which %s owns; using the standard values",
            ", ".join(ignored),
            org.label,
        )
    return kept


def remember_assignment(store: SurveyStore, assigned: Mapping[str, Any] | None) -> None:
    """Keep the registry's assigned preset beside the sync cursor, or clear it.

    Kept even when the named preset has not been pulled into this survey yet:
    resolution copes with a row that is not here, and the next pull may land it.
    """
    if assigned is None:
        store.set_sync_state(ASSIGNED_PRESET_KEY, None)
        return
    store.set_sync_state(
        ASSIGNED_PRESET_KEY,
        json.dumps({"name": str(assigned["name"]), "version": int(assigned["version"])}),
    )


def resolved_identity(store: SurveyStore | None) -> tuple[str, int] | None:
    """Name and version of the org preset this machine currently runs under.

    Mirrors the interface's own resolution: an admin file when one is in force,
    else the survey's server selection, else the registry's assignment, else the
    organisation preset itself. A selection or assignment whose row is not in
    the store falls back to the organisation preset, exactly as the run would.
    None when the settings cannot be read at all.
    """
    try:
        active = load_active_preset()
    except (OSError, ValueError, yaml.YAMLError):
        return None
    org = active.org
    if not org.locked and store is not None:
        raw = store.sync_state(SERVER_PRESET_KEY) or store.sync_state(ASSIGNED_PRESET_KEY)
        if raw:
            row = None
            try:
                wanted = json.loads(raw)
                row = store.get_server_preset(str(wanted["name"]), int(wanted["version"]))
            except (ValueError, KeyError, TypeError):
                logger.warning("Ignoring an unreadable server preset reference")
            if row is not None:
                return (row.name, int(row.version))
    return (org.name, org.version)


def manifest_config_block(org: OrgPreset, deviations: Mapping[str, Any]) -> dict[str, Any]:
    """Which configuration produced a run, for the manifest's provenance block.

    Records identity rather than the whole preset: the name, version and content
    hash pin what was blessed, and the deviations say where the run left it.
    """
    return {
        "preset_name": org.name,
        "preset_version": org.version,
        "preset_hash": org.content_hash,
        "preset_source": org.source,
        "locked": org.locked,
        "deviated": bool(deviations),
        "deviations": {key: deviations[key] for key in sorted(deviations)},
    }
