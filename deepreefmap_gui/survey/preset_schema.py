"""What each run setting is, so a preset from elsewhere can be checked against it.

The run form has always held this knowledge, but only as arguments to Qt widget
constructors, which nothing else can read. A preset arriving from the registry
was therefore taken on trust: an out-of-range number reached `setValue` and
raised, and a model name nothing offers was handed to `setCurrentText` on a
combo that is not editable, which silently kept the previous selection. One of
those stops a laptop starting, the other reports a run under a preset that does
not describe it.

So the table below is the same knowledge written where it can be used. It is
hand-maintained and a test asserts it covers `PRESET_KEYS` exactly, which is the
cheapest thing that cannot drift silently. Folding the form's own widget
construction onto it is worth doing and is not needed for it to be useful here.

Enumerations are deliberately not literals. Which models, backends and camera
profiles exist depends on what this build ships and what this machine has, so
they resolve when asked rather than when imported.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_PATH = "path"


@dataclass(frozen=True)
class PresetField:
    """One run setting: what it accepts, and what the interface calls it."""

    key: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    decimals: int | None = None
    unit: str = ""
    # Which enumeration supplies the legal values, for `kind == KIND_ENUM`.
    choices: str = ""
    # The `mapping_name` values this field is used with, empty meaning always.
    applies_when: tuple[str, ...] = ()
    # A device may differ from the organisation on this one.
    machine_overridable: bool = False
    # A path on one machine's disk, so the registry must never publish a value.
    publishable: bool = True


_FIELDS: tuple[PresetField, ...] = (
    PresetField("fps", KIND_INT, minimum=1, maximum=60),
    PresetField("segmentation_name", KIND_ENUM, choices="segmentation"),
    PresetField(
        "mapping_name", KIND_ENUM, choices="mapping", machine_overridable=True
    ),
    PresetField(
        "camera_profile_name", KIND_ENUM, choices="camera", machine_overridable=True
    ),
    # Zero disables the crop, which is why the floor is 0 rather than a width.
    PresetField(
        "transect_crop_width",
        KIND_FLOAT,
        minimum=0.0,
        maximum=50.0,
        step=0.1,
        decimals=2,
        unit="m",
    ),
    PresetField("enable_tsdf", KIND_BOOL),
    PresetField("skip_segmentation", KIND_BOOL),
    PresetField("resolution_preset", KIND_ENUM, choices="resolution"),
    # None means the model's own native size, so these two are optional.
    PresetField("processing_width", KIND_INT, minimum=256, maximum=3840, step=32),
    PresetField("processing_height", KIND_INT, minimum=256, maximum=2160, step=32),
    PresetField(
        "preprocess_batch_size", KIND_INT, minimum=1, maximum=16, machine_overridable=True
    ),
    PresetField("grid_bins", KIND_INT, minimum=100, maximum=10000, step=100),
    PresetField("require_gravity_telemetry", KIND_BOOL),
    PresetField(
        "replacement_radius_factor",
        KIND_FLOAT,
        minimum=0.0,
        maximum=10.0,
        step=0.1,
        decimals=2,
    ),
    PresetField(
        "replacement_radius_estimation_frames", KIND_INT, minimum=1, maximum=200
    ),
    PresetField(
        "replacement_radius_override",
        KIND_FLOAT,
        minimum=0.0,
        maximum=10.0,
        step=0.001,
        decimals=4,
        unit="m",
    ),
    PresetField(
        "loger_window_size",
        KIND_INT,
        minimum=1,
        maximum=256,
        applies_when=("loger", "loger_star"),
    ),
    PresetField(
        "loger_overlap_size",
        KIND_INT,
        minimum=0,
        maximum=64,
        applies_when=("loger", "loger_star"),
    ),
    PresetField(
        "loger_model_path",
        KIND_PATH,
        applies_when=("loger", "loger_star"),
        machine_overridable=True,
        publishable=False,
    ),
    PresetField("refine_intrinsics_from_mapper", KIND_BOOL),
    PresetField(
        "scs_target_width",
        KIND_INT,
        minimum=64,
        maximum=2048,
        step=32,
        applies_when=("scsfmlearner",),
    ),
    PresetField(
        "scs_target_height",
        KIND_INT,
        minimum=64,
        maximum=2048,
        step=32,
        applies_when=("scsfmlearner",),
    ),
    PresetField(
        "scs_checkpoint_path",
        KIND_PATH,
        applies_when=("scsfmlearner",),
        machine_overridable=True,
        publishable=False,
    ),
)

FIELDS: Mapping[str, PresetField] = {field.key: field for field in _FIELDS}

# Fixed because the form's own combo is fixed: these are display sizes, not a
# discovered capability.
RESOLUTION_PRESETS = ("Native", "Half", "Quarter", "Custom")


def choices_for(name: str) -> tuple[str, ...]:
    """The legal values of one enumeration, as this build and machine see them.

    Imports are local: this module is read by code that must not drag the model
    catalogue or a camera profile scan in with it.
    """
    if name == "resolution":
        return RESOLUTION_PRESETS
    try:
        if name == "segmentation":
            from deepreefmap_gui.models.cache import segmentation_model_names

            return tuple(segmentation_model_names())
        if name == "mapping":
            from deepreefmap.mapping.registry import list_mapping_backends

            return tuple(list_mapping_backends())
        if name == "camera":
            from deepreefmap.camera.intrinsics import available_profile_names

            return tuple(available_profile_names())
    except Exception:
        logger.warning("Cannot enumerate %s choices", name, exc_info=True)
    return ()


def coerce_settings(
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Settings this build can actually apply, and what it had to drop.

    Numbers are coerced and clamped, because a value near the edge is what the
    author meant. An enumeration is not: substituting a default for a model name
    nobody offers would run the survey under a model the preset does not name,
    which is the failure this exists to prevent. Those are dropped and reported,
    so the interface can say what was ignored instead of quietly proceeding.
    """
    kept: dict[str, Any] = {}
    dropped: list[tuple[str, str]] = []
    for key, value in settings.items():
        field = FIELDS.get(key)
        if field is None:
            dropped.append((key, "this version has no such setting"))
            continue
        # The console publishes no path fields, but the API stores settings
        # opaquely, so the refusal has to live where the value would land.
        if not field.publishable and value is not None:
            dropped.append((key, "a path on another machine's disk never applies here"))
            continue
        coerced, problem = _coerce(field, value)
        if problem:
            dropped.append((key, problem))
            continue
        kept[key] = coerced
    return kept, dropped


def _coerce(field: PresetField, value: Any) -> tuple[Any, str]:
    if field.kind == KIND_BOOL:
        return _coerce_bool(value)
    if field.kind in (KIND_INT, KIND_FLOAT):
        return _coerce_number(field, value)
    if field.kind == KIND_ENUM:
        return _coerce_enum(field, value)
    if value is None or isinstance(value, str):
        return value, ""
    return None, f"expected a path, got {type(value).__name__}"


def _coerce_bool(value: Any) -> tuple[Any, str]:
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true", ""
    return None, f"expected true or false, got {value!r}"


def _coerce_number(field: PresetField, value: Any) -> tuple[Any, str]:
    # None is how the two resolution fields say "the model's own size".
    if value is None:
        return None, ""
    # bool is an int in Python and would sail through as 0 or 1.
    if isinstance(value, bool):
        return None, f"expected a number, got {value!r}"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"expected a number, got {value!r}"
    if number != number or number in (float("inf"), float("-inf")):
        return None, f"expected a finite number, got {value!r}"
    if field.minimum is not None:
        number = max(number, field.minimum)
    if field.maximum is not None:
        number = min(number, field.maximum)
    if field.kind == KIND_INT:
        return int(round(number)), ""
    if field.decimals is not None:
        number = round(number, field.decimals)
    return number, ""


def _coerce_enum(field: PresetField, value: Any) -> tuple[Any, str]:
    if not isinstance(value, str):
        return None, f"expected a name, got {type(value).__name__}"
    allowed = choices_for(field.choices)
    # An empty enumeration is this machine knowing nothing, not the value being
    # wrong, so the value stands and the form decides what it can offer.
    if not allowed or value in allowed:
        return value, ""
    return None, f"{value!r} is not available here"
