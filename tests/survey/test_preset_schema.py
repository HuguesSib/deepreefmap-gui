"""The run-setting schema: that it describes every setting, and coerces sanely.

The first test is the one that matters. The table is hand-maintained beside a
form that declares the same constraints as Qt widget arguments, so the only
thing standing between the two is this assertion.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.survey.preset import MACHINE_OVERRIDABLE_KEYS, PRESET_KEYS
from deepreefmap_gui.survey.preset_schema import (
    FIELDS,
    KIND_ENUM,
    coerce_settings,
)


def test_the_schema_describes_exactly_the_settings_a_preset_carries():
    assert set(FIELDS) == PRESET_KEYS


def test_the_schema_agrees_on_which_settings_a_machine_may_override():
    overridable = {key for key, field in FIELDS.items() if field.machine_overridable}
    assert overridable == set(MACHINE_OVERRIDABLE_KEYS)


def test_the_two_path_settings_are_not_publishable():
    """They name a location on one laptop's disk, so a registry publishing one
    would hand every other device a path that does not exist there."""
    unpublishable = {key for key, field in FIELDS.items() if not field.publishable}
    assert unpublishable == {"loger_model_path", "scs_checkpoint_path"}


def test_every_enumeration_names_a_source_the_module_can_resolve():
    for key, field in FIELDS.items():
        if field.kind == KIND_ENUM:
            assert field.choices, f"{key} is an enumeration with no source"


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"fps": 120}, 60),
        ({"fps": 0}, 1),
        ({"fps": 5.4}, 5),
        ({"fps": "12"}, 12),
    ],
)
def test_numbers_are_coerced_and_clamped_to_what_the_form_accepts(settings, expected):
    kept, dropped = coerce_settings(settings)

    assert kept["fps"] == expected
    assert dropped == []


@pytest.mark.parametrize("value", ["five", None, [], float("inf"), True])
def test_a_value_that_is_not_a_number_is_dropped(value):
    kept, dropped = coerce_settings({"grid_bins": value})

    if value is None:
        assert kept == {"grid_bins": None}, "None is how a size says 'the native one'"
    else:
        assert kept == {} and [key for key, _ in dropped] == ["grid_bins"]


def test_a_boolean_survives_as_itself_and_as_a_legible_string():
    kept, dropped = coerce_settings({"enable_tsdf": True, "skip_segmentation": "false"})

    assert kept == {"enable_tsdf": True, "skip_segmentation": False}
    assert dropped == []


def test_a_number_where_a_boolean_belongs_is_dropped():
    """1 would pass `bool()` and read as deliberate, which it is not."""
    kept, dropped = coerce_settings({"enable_tsdf": 1})

    assert kept == {} and [key for key, _ in dropped] == ["enable_tsdf"]


def test_an_unknown_setting_is_dropped_and_named():
    kept, dropped = coerce_settings({"coral_iq": 11})

    assert kept == {}
    assert dropped == [("coral_iq", "this version has no such setting")]


def test_a_resolution_preset_outside_the_fixed_set_is_dropped():
    kept, dropped = coerce_settings({"resolution_preset": "Enormous"})

    assert kept == {} and [key for key, _ in dropped] == ["resolution_preset"]


def test_a_path_published_to_the_registry_never_applies_here() -> None:
    """The console's editor offers no path fields, but the API stores settings
    opaquely, so a hand-crafted publish could carry one machine's disk layout."""
    kept, dropped = coerce_settings(
        {"loger_model_path": "/home/kim/ckpts/latest.pt", "fps": 5}
    )

    assert kept == {"fps": 5}
    assert dropped == [
        ("loger_model_path", "a path on another machine's disk never applies here")
    ]


def test_a_null_path_field_is_not_worth_reporting() -> None:
    kept, dropped = coerce_settings({"scs_checkpoint_path": None})

    assert kept == {"scs_checkpoint_path": None}
    assert dropped == []
