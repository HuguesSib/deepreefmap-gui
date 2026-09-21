"""The vendored contract artefact, and what this build derives from it.

Scenario: the registry publishes `contract/sync-contract.json` and this repo keeps
a copy of it.

Expected behaviour: every constant comes out of that copy, the sections it names
are the sections the wire layer orders, and a row this build sends carries every
column the registry will not take a row without. A copy that has drifted is a
failed test here rather than a sync that stops mid-page in the field.
"""

from __future__ import annotations

import json
import uuid
from importlib import resources

import pytest

from deepreefmap_gui.survey.models import (
    Campaign,
    RunRecord,
    Site,
    Transect,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import connect_code, contract, wire
from deepreefmap_gui.sync.client import CONTRACT_VERSION


@pytest.fixture(scope="module")
def artefact() -> dict:
    """The vendored file, read as a file rather than through the module."""
    raw = resources.files("deepreefmap_gui.sync.contract").joinpath(contract.ARTEFACT)
    return json.loads(raw.read_text(encoding="utf-8"))


def _run_table(artefact: dict) -> dict:
    return next(t for t in artefact["tables"] if t["section"] == "runs")


def one_of_each() -> dict[str, object]:
    """One model per section, filled only where the constructor insists."""
    return {
        "sites": Site(name="Reef Wall"),
        "campaigns": Campaign(name="Autumn 2026"),
        "transects": Transect(
            name="T1", start_lat=-17.5, start_lon=177.1, end_lat=-17.6, end_lon=177.2
        ),
        "videos": VideoAsset(file_name="GX010001.MP4", path="/footage/GX010001.MP4"),
        "passes": TransectPass(
            transect_id=uuid.uuid4(), video_id=uuid.uuid4(), begin_s=0.0, end_s=60.0
        ),
        "runs": RunRecord(pass_id=uuid.uuid4(), run_dir_name="t1__p01"),
    }


def test_every_constant_is_read_out_of_the_artefact(artefact) -> None:
    assert artefact["contract_version"] == contract.CONTRACT_VERSION
    assert artefact["min_contract_version"] == contract.MIN_CONTRACT_VERSION
    assert tuple(artefact["sections"]) == contract.SECTIONS
    assert tuple(artefact["pull_sections"]) == contract.PULL_SECTIONS
    range_ = f"{artefact['min_contract_version']}-{artefact['contract_version']}"
    assert range_ == contract.CONTRACT_RANGE


def test_the_connect_code_prefix_comes_out_of_the_artefact(artefact) -> None:
    """The prefix is repeated in the registry and the console, so nothing here may
    hand-type it: a bump lands in one file and this catches a stale copy."""
    published = artefact["connect_code"]
    assert published["prefix"] == contract.CONNECT_CODE_PREFIX
    assert published["prefix"] == f"{contract.CONNECT_CODE_FAMILY}{contract.CONNECT_CODE_VERSION}."
    assert connect_code.CODE_PREFIX == contract.CONNECT_CODE_PREFIX


def test_the_client_re_exports_the_derived_version() -> None:
    """No version integer is typed by hand anywhere in this repo."""
    assert CONTRACT_VERSION == contract.CONTRACT_VERSION


def test_the_ordered_sections_are_the_ones_the_wire_layer_lands() -> None:
    assert contract.SECTIONS == wire.WIRE_SECTIONS


def test_a_drifted_artefact_refuses_to_import() -> None:
    """The check runs at import, so a mis-vendored file cannot reach a run."""
    original = wire.WIRE_SECTIONS
    try:
        wire.WIRE_SECTIONS = (*original, "moorings")
        with pytest.raises(AssertionError, match="moorings"):
            wire._assert_sections_match_the_contract()
    finally:
        wire.WIRE_SECTIONS = original


def test_the_pull_sections_are_a_subset_of_the_whole() -> None:
    assert set(contract.PULL_SECTIONS) <= set(contract.SECTIONS)


# The pull-only sections never enter a push document, so there is no pushed row to
# hold to the requirement: presets and the lenses are the registry's to publish.
_PULL_ONLY = {"presets", "camera_profiles", "camera_calibrations"}


@pytest.mark.parametrize("section", sorted(set(SYNC_SECTIONS) - _PULL_ONLY))
def test_a_pushed_row_carries_every_column_the_registry_requires(section) -> None:
    """A required column added server-side shows up here, not as a rejected push."""
    row = wire.rows_to_wire(section, [one_of_each()[section]])[0]

    missing = sorted(set(contract.required_columns(section)) - set(row))

    assert missing == []


def test_a_chapter_row_carries_every_column_the_registry_requires() -> None:
    """pass_videos are derived here rather than read from a table of their own."""
    pass_ = TransectPass(
        transect_id=uuid.uuid4(), video_id=uuid.uuid4(), begin_s=0.0, end_s=60.0
    )

    row = wire.pass_video_rows(pass_)[0]

    assert sorted(set(contract.required_columns(wire.PASS_VIDEOS)) - set(row)) == []


def test_a_run_row_fills_the_configuration_columns_the_registry_holds(tmp_path, artefact) -> None:
    """Nullable, so nothing rejects a row that omits them. A column the registry
    holds and this build never fills is a column that stays null forever, and the
    performance grain it exists for collapses to one legacy group.
    """
    columns = {c["name"] for c in _run_table(artefact)["columns"]}
    grain = {"processing_width", "processing_height", "fps", "preprocess_batch_size"}
    assert grain <= columns

    row = wire.run_rows_to_wire([one_of_each()["runs"]], tmp_path)[0]

    assert grain <= set(row)


def test_an_unnamed_section_is_an_error_and_not_an_empty_list() -> None:
    with pytest.raises(KeyError):
        contract.required_columns("moorings")


def test_a_transect_row_carries_the_depth_at_each_end() -> None:
    table = next(t for t in contract.DOCUMENT["tables"] if t["section"] == "transects")
    names = {column["name"] for column in table["columns"]}
    assert {"depth_m", "start_depth_m", "end_depth_m"} <= names


def test_the_registry_derives_the_depth_from_the_two_ends() -> None:
    """So a laptop holding a stale mean is not in conflict with one that has both
    ends, and the figure this app shows is the one the registry will hold."""
    table = next(t for t in contract.DOCUMENT["tables"] if t["section"] == "transects")
    derived = {column["name"] for column in table["columns"] if column.get("derived")}
    assert derived == {"depth_m"}


def test_the_lenses_are_pulled_and_never_pushed() -> None:
    """A calibration is published through the registry's upload endpoint, which
    returns a row that comes back down like any other. Authoring one here would
    make two laptops' measurements of one rig fight over a name."""
    assert {"camera_profiles", "camera_calibrations"} <= set(contract.PULL_SECTIONS)
    assert {"camera_profiles", "camera_calibrations"}.isdisjoint(contract.PUSH_SECTIONS)


def test_a_calibration_row_carries_the_document_a_run_needs() -> None:
    table = next(t for t in contract.DOCUMENT["tables"] if t["section"] == "camera_calibrations")
    names = {column["name"] for column in table["columns"]}

    assert {"camera_profile_id", "version", "document"} <= names


def test_a_profile_row_names_the_calibration_it_deploys() -> None:
    """Which measurement a rig runs under is the registry's to decide, so it has
    to reach the laptop that resolves the name."""
    table = next(t for t in contract.DOCUMENT["tables"] if t["section"] == "camera_profiles")
    names = {column["name"] for column in table["columns"]}

    assert "current_calibration_id" in names
