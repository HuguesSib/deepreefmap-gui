"""The two settings layers: an organisation preset, and what one machine may change.

The invariant most of this file guards: editing settings on a field laptop writes
back only the allow-listed keys, never a whole copy of the organisation preset.
"""

import json

import pytest
import yaml

from deepreefmap_gui.survey.preset import (
    MACHINE_OVERRIDABLE_KEYS,
    PRESET_KEYS,
    SERVER_PRESET_KEY,
    ActivePreset,
    OrgPreset,
    _bundled_defaults,
    _bundled_text,
    clear_machine_override,
    describe_keys,
    deviations_from_org,
    load_active_preset,
    load_machine_override,
    load_org_preset,
    load_survey_preset,
    manifest_config_block,
    parse_machine_override,
    parse_preset,
    preset_content_hash,
    registry_preset,
    remember_assignment,
    resolved_identity,
    save_machine_override,
)

SAMPLE = {
    **_bundled_defaults(),
    "fps": 4,
    "segmentation_name": "segformer-b2",
    "mapping_name": "scsfmlearner",
    "transect_crop_width": None,
    "skip_segmentation": True,
}

# One allow-listed key and one the organisation owns, so a test can tell the two
# apart without hardcoding the whole split.
MACHINE_KEY = "preprocess_batch_size"
ORG_KEY = "grid_bins"


def preset_yaml(*, schema_version: int = 3, **overrides) -> str:
    """The shipped preset text, with keys overridden.

    Serialised by yaml.safe_dump rather than by hand: an f-string renders None as
    the bare token `None`, which YAML reads back as the *string* "None", so the
    two nullable size keys silently round-tripped as strings.
    """
    data = {**_bundled_defaults(), **overrides}
    return yaml.safe_dump({"schema_version": schema_version, **data}, sort_keys=False)


VALID = _bundled_text()

# What a machine that last ran the seven-key schema still has on disk.
V1_FILE = """
schema_version: 1
fps: 3
segmentation_name: segformer-b2
mapping_name: scsfmlearner
camera_profile_name: gopro_hero_10
transect_crop_width: 2.0
enable_tsdf: true
skip_segmentation: false
"""


@pytest.fixture
def org() -> OrgPreset:
    return load_org_preset()


@pytest.fixture
def machine_path(tmp_path, monkeypatch):
    """Redirect the machine override to a writable temp path."""
    target = tmp_path / "survey_preset.yaml"
    monkeypatch.setattr("deepreefmap_gui.survey.preset.survey_preset_path", lambda: target)
    monkeypatch.delenv("DEEPREEFMAP_SURVEY_PRESET", raising=False)
    return target


def test_bundled_preset_loads():
    """No machine override and no admin file: the shipped defaults are what load.

    The autouse fixture in conftest.py is what makes "no override" true; without
    it this reads the developer's real ~/.local/share/deepreefmap preset.
    """
    preset = load_survey_preset()
    assert set(preset) == PRESET_KEYS
    assert isinstance(preset["fps"], int)
    assert preset == _bundled_defaults()


def test_bundled_preset_names_and_versions_itself(org):
    assert org.name == "Standard reef survey"
    assert org.version == 1
    assert org.label == "Standard reef survey (v1)"
    assert not org.locked
    assert org.source == "bundled"


def test_admin_file_wins_and_locks(tmp_path, monkeypatch):
    """Scenario: an administrator publishes the blessed configuration.

    Expected behaviour: it is what loads, it is named as theirs, and it is locked.
    """
    admin = tmp_path / "preset.yaml"
    admin.write_text(preset_yaml(fps=3, preset_name="Reef Watch 2026", preset_version=4))
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    org = load_org_preset()
    assert org.settings["fps"] == 3
    assert org.label == "Reef Watch 2026 (v4)"
    assert org.locked
    assert org.source == "admin"
    assert load_survey_preset()["fps"] == 3


def test_unnamed_admin_file_does_not_borrow_the_bundled_name(tmp_path, monkeypatch):
    admin = tmp_path / "preset.yaml"
    admin.write_text(preset_yaml(schema_version=2, fps=3))
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    org = load_org_preset()
    assert org.name != "Standard reef survey"
    assert org.version == 1
    assert org.locked


def test_content_hash_ignores_comments_but_follows_values(tmp_path, monkeypatch):
    admin = tmp_path / "preset.yaml"
    admin.write_text("# a comment nobody hashes\n" + preset_yaml())
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    assert load_org_preset().content_hash == preset_content_hash(_bundled_defaults())

    admin.write_text(preset_yaml(fps=11))
    assert load_org_preset().content_hash != preset_content_hash(_bundled_defaults())


def test_parse_rejects_wrong_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        parse_preset(preset_yaml(schema_version=99))


def test_corrupt_machine_override_is_quarantined_and_ignored(machine_path, org):
    """An override that will not parse must not block the survey.

    It is moved aside so the evidence survives, and the organisation preset stands
    alone rather than leaving the survey with no settings at all.
    """
    machine_path.write_text("schema_version: 999\nfps: 3\n")

    assert load_survey_preset() == _bundled_defaults()
    assert not machine_path.exists()
    aside = machine_path.with_name(machine_path.name + ".corrupt-1")
    assert "schema_version: 999" in aside.read_text()


def test_quarantine_does_not_clobber_an_earlier_copy(machine_path, tmp_path):
    (tmp_path / "survey_preset.yaml.corrupt-1").write_text("older evidence")
    machine_path.write_text("not: [valid yaml")

    load_survey_preset()
    assert (tmp_path / "survey_preset.yaml.corrupt-1").read_text() == "older evidence"
    assert (tmp_path / "survey_preset.yaml.corrupt-2").exists()


def test_bad_admin_file_still_raises(tmp_path, monkeypatch):
    """The admin file is a deliberate choice, so a bad one is an error to surface,
    not a copy to quarantine."""
    admin = tmp_path / "preset.yaml"
    admin.write_text("schema_version: 99\n")
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    with pytest.raises(ValueError, match="schema_version"):
        load_survey_preset()


def test_parse_rejects_missing_and_unknown_keys():
    with pytest.raises(ValueError, match="missing"):
        parse_preset("schema_version: 3\nfps: 5\n")
    with pytest.raises(ValueError, match="unknown"):
        parse_preset(VALID + "\nsurprise: true\n")


def test_v1_admin_file_upgrades_keeping_its_own_choices():
    """Scenario: a settings file that last ran the seven-key schema.

    Expected behaviour: its settings survive, the new keys arrive as shipped.
    """
    preset = parse_preset(V1_FILE)
    assert set(preset) == PRESET_KEYS
    assert preset["fps"] == 3
    assert preset["mapping_name"] == "scsfmlearner"
    assert preset["enable_tsdf"] is True
    # grid_bins postdates schema 1, so it arrives from the shipped defaults.
    # Pinned as a literal: comparing to _bundled_defaults() would also pass if
    # the shipped file were empty.
    assert preset["grid_bins"] == 2000


def test_native_processing_size_stays_null_by_default():
    """A null size means follow the segmentation model, so the bundled preset
    must not pin numbers that would override it."""
    defaults = _bundled_defaults()
    assert defaults["processing_width"] is None
    assert defaults["processing_height"] is None


# --- Machine override ---


def test_save_keeps_only_the_allow_listed_change(machine_path, org):
    """Scenario: a diver changes both a machine setting and one the organisation
    owns.

    Expected behaviour: the machine setting persists, the other is refused, and
    the file holds a two-line override rather than a copy of the whole preset.
    """
    settings = {**org.settings, MACHINE_KEY: 1, ORG_KEY: 99}
    result = save_machine_override(settings, org)

    assert result.saved == {MACHINE_KEY: 1}
    assert result.refused == {ORG_KEY: 99}
    on_disk = yaml.safe_load(machine_path.read_text())
    assert on_disk == {"schema_version": 3, "overrides": {MACHINE_KEY: 1}}

    assert load_survey_preset()[MACHINE_KEY] == 1
    assert load_survey_preset()[ORG_KEY] == org.settings[ORG_KEY]


def test_saving_nothing_new_removes_the_file(machine_path, org):
    """A machine that agrees with the standard keeps no file, so it follows the
    organisation preset from then on rather than pinning today's values."""
    save_machine_override({**org.settings, MACHINE_KEY: 1}, org)
    assert machine_path.exists()

    result = save_machine_override(dict(org.settings), org)
    assert result.saved == {}
    assert result.path is None
    assert not machine_path.exists()


def test_override_that_the_standard_caught_up_with_disappears(machine_path, org):
    """An override recording a value the organisation preset now ships is not a
    deviation any more, so it stops being reported as one."""
    machine_path.write_text(
        yaml.safe_dump({"schema_version": 3, "overrides": {MACHINE_KEY: org.settings[MACHINE_KEY]}})
    )
    assert load_machine_override(org) == {}


def test_hand_edited_override_of_a_locked_key_is_ignored(machine_path, org, caplog):
    machine_path.write_text(
        yaml.safe_dump({"schema_version": 3, "overrides": {MACHINE_KEY: 1, ORG_KEY: 99}})
    )
    assert load_machine_override(org) == {MACHINE_KEY: 1}
    assert ORG_KEY in caplog.text


def test_override_with_an_unknown_key_is_rejected(org):
    with pytest.raises(ValueError, match="unknown"):
        parse_machine_override(
            yaml.safe_dump({"schema_version": 3, "overrides": {"surprise": True}}), org
        )


def test_save_rejects_a_partial_or_surprising_preset(org):
    with pytest.raises(ValueError, match="unknown"):
        save_machine_override({**SAMPLE, "surprise": True}, org)
    with pytest.raises(ValueError, match="missing"):
        save_machine_override({k: v for k, v in SAMPLE.items() if k != "fps"}, org)


def test_clear_machine_override_reports_whether_there_was_one(machine_path, org):
    assert not clear_machine_override()
    save_machine_override({**org.settings, MACHINE_KEY: 1}, org)
    assert clear_machine_override()
    assert load_survey_preset() == org.settings


def test_schema_2_machine_file_upgrades_to_an_allow_listed_override(machine_path, org):
    """Scenario: a laptop configured before the two-layer split, holding a whole
    copy of the preset with two edits in it.

    Expected behaviour: the machine keeps the change it is allowed to keep, and
    the organisation preset takes back the one it owns.
    """
    machine_path.write_text(preset_yaml(schema_version=2, **{MACHINE_KEY: 1, ORG_KEY: 99}))

    assert load_machine_override(org) == {MACHINE_KEY: 1}
    settings = load_survey_preset()
    assert settings[MACHINE_KEY] == 1
    assert settings[ORG_KEY] == org.settings[ORG_KEY]


def test_upgraded_machine_file_saves_back_in_the_new_shape(machine_path, org):
    machine_path.write_text(preset_yaml(schema_version=2, **{MACHINE_KEY: 1}))
    save_machine_override(load_survey_preset(), org)
    assert yaml.safe_load(machine_path.read_text()) == {
        "schema_version": 3,
        "overrides": {MACHINE_KEY: 1},
    }


def test_schema_1_machine_file_loses_the_keys_it_never_owned(machine_path, org):
    """A seven-key file predates the allow-list entirely. mapping_name is on it,
    so it survives; fps is not, so the standard takes it back."""
    machine_path.write_text(V1_FILE)
    settings = load_survey_preset()
    assert settings["mapping_name"] == "scsfmlearner"
    assert settings["fps"] == org.settings["fps"]


def test_retired_key_in_an_old_file_does_not_quarantine_it(machine_path, org):
    machine_path.write_text(preset_yaml(schema_version=2, **{MACHINE_KEY: 1}) + "\nretired: 1\n")
    assert load_machine_override(org) == {MACHINE_KEY: 1}
    assert machine_path.exists()


def test_allow_list_is_a_subset_of_the_settings():
    assert MACHINE_OVERRIDABLE_KEYS <= PRESET_KEYS


# --- What gets said and recorded ---


def test_deviations_name_every_difference_not_just_the_allowed_ones(org):
    settings = {**org.settings, MACHINE_KEY: 1, ORG_KEY: 99}
    assert deviations_from_org(settings, org) == {MACHINE_KEY: 1, ORG_KEY: 99}
    assert deviations_from_org(dict(org.settings), org) == {}


def test_describe_keys_reads_as_english_and_deduplicates():
    assert describe_keys(["preprocess_batch_size"]) == "frames processed at once"
    # Two settings, one plain-language name: saying it twice would read as noise.
    assert describe_keys(["loger_model_path", "scs_checkpoint_path"]) == "processing model file"
    assert describe_keys(["loger_window_size"]) == "loger window size"


def test_manifest_config_block_pins_identity_and_deviation(org):
    block = manifest_config_block(org, {MACHINE_KEY: 1})
    assert block["preset_name"] == org.name
    assert block["preset_version"] == org.version
    assert block["preset_hash"] == org.content_hash
    assert block["preset_source"] == "bundled"
    assert block["locked"] is False
    assert block["deviated"] is True
    assert block["deviations"] == {MACHINE_KEY: 1}


def test_manifest_config_block_of_a_standard_run(org):
    block = manifest_config_block(org, {})
    assert block["deviated"] is False
    assert block["deviations"] == {}


def test_active_preset_layers_the_override_over_the_organisation(org):
    active = ActivePreset(org=org, overrides={MACHINE_KEY: 1})
    assert active.settings[MACHINE_KEY] == 1
    assert active.settings[ORG_KEY] == org.settings[ORG_KEY]
    assert set(active.settings) == PRESET_KEYS


def test_load_active_preset_reports_both_layers(machine_path, org):
    save_machine_override({**org.settings, MACHINE_KEY: 1}, org)
    active = load_active_preset()
    assert active.overrides == {MACHINE_KEY: 1}
    assert active.org.name == org.name
    assert active.settings[MACHINE_KEY] == 1


def test_a_registry_preset_keeps_known_keys_and_fills_the_rest():
    preset = registry_preset("Deep reef", 2, {"fps": 4, "coral_iq": 11})

    assert preset.source == "server"
    assert not preset.locked
    assert preset.settings["fps"] == 4
    assert "coral_iq" not in preset.settings
    assert preset.settings["mapping_name"], "unnamed keys take the shipped defaults"
    assert preset.label == "Deep reef (v2)"
    assert preset.degraded, "a key this build has no field for is worth reporting"


def test_a_registry_preset_clamps_a_number_the_form_would_refuse():
    """Scenario: a console publishes fps 120 against a form that stops at 60.

    Expected behaviour: the value is clamped rather than raising out of
    `setValue`, which would leave the form half populated and, because the
    selection is replayed at launch, stop the window constructing at all.
    """
    preset = registry_preset("Fast", 1, {"fps": 120})

    assert preset.settings["fps"] == 60
    assert not preset.degraded, "clamping is not a loss worth naming"


def test_a_registry_preset_drops_a_model_this_build_cannot_offer():
    """Substituting the default would run the survey under a model the preset
    does not name, and stamp the run with the preset's identity regardless."""
    preset = registry_preset("Future", 1, {"segmentation_name": "coralscapes-vit-xl-dpt"})

    assert preset.degraded
    assert [key for key, _ in preset.dropped] == ["segmentation_name"]
    assert "coral identification model" in preset.dropped_summary
    assert preset.settings["segmentation_name"] != "coralscapes-vit-xl-dpt"


def test_a_registry_preset_drops_a_value_of_the_wrong_type():
    preset = registry_preset("Broken", 1, {"fps": "five", "enable_tsdf": "true"})

    assert [key for key, _ in preset.dropped] == ["fps"]
    assert preset.settings["enable_tsdf"] is True, "a legible string still counts"


def _publish(store, name, version, settings=None):
    from deepreefmap_gui.survey.models.server_preset import ServerPreset

    store._add("server_preset", ServerPreset(name=name, version=version, settings=settings or {"fps": 3}))


def test_resolved_identity_is_the_organisation_preset_by_default(store):
    assert resolved_identity(store) == ("Standard reef survey", 1)
    assert resolved_identity(None) == ("Standard reef survey", 1)


def test_resolved_identity_follows_the_registry_assignment(store):
    _publish(store, "Deep reef", 2)
    remember_assignment(store, {"id": "p-1", "name": "Deep reef", "version": 2})

    assert resolved_identity(store) == ("Deep reef", 2)

    remember_assignment(store, None)
    assert resolved_identity(store) == ("Standard reef survey", 1)


def test_resolved_identity_prefers_an_explicit_selection_over_the_assignment(store):
    _publish(store, "Assigned", 1)
    _publish(store, "Chosen", 3)
    remember_assignment(store, {"id": "p-1", "name": "Assigned", "version": 1})
    store.set_sync_state(SERVER_PRESET_KEY, json.dumps({"name": "Chosen", "version": 3}))

    assert resolved_identity(store) == ("Chosen", 3)


def test_resolved_identity_falls_back_when_the_named_row_is_missing(store):
    remember_assignment(store, {"id": "p-1", "name": "Never pulled", "version": 1})

    assert resolved_identity(store) == ("Standard reef survey", 1)


def test_resolved_identity_ignores_the_store_under_an_admin_file(store, tmp_path, monkeypatch):
    """A mandate outranks a menu: the admin file's identity is what runs, so it
    is what the heartbeat must report."""
    _publish(store, "Deep reef", 2)
    remember_assignment(store, {"id": "p-1", "name": "Deep reef", "version": 2})
    admin = tmp_path / "preset.yaml"
    admin.write_text(preset_yaml(preset_name="Reef Watch 2026", preset_version=4))
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))

    assert resolved_identity(store) == ("Reef Watch 2026", 4)


def test_resolved_identity_is_none_when_the_settings_cannot_be_read(store, tmp_path, monkeypatch):
    admin = tmp_path / "broken.yaml"
    admin.write_text("fps: [")
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))

    assert resolved_identity(store) is None
