"""The organisation preset as seen from the window: named, authoritative, audited.

Scenario throughout: an administrator publishes the blessed configuration by
pointing DEEPREEFMAP_SURVEY_PRESET at it, which locks it. A field machine may
then differ only on the few settings that describe the computer.
"""

import json

import yaml

from deepreefmap_gui.survey.preset import _bundled_defaults, manifest_config_block


def publish_org_preset(window, tmp_path, monkeypatch, *, name="Reef Watch", version=2, **overrides):
    """Write an admin preset, lock it, and have the window adopt it."""
    admin = tmp_path / "org_preset.yaml"
    admin.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "preset_name": name,
                "preset_version": version,
                **_bundled_defaults(),
                **overrides,
            },
            sort_keys=False,
        )
    )
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    window._reload_active_preset()
    window._populate_form_from_preset(window._survey_preset)
    window._recompute_survey_start()
    return admin


def captured_audit_dialogs(monkeypatch) -> list:
    """Intercept exec() so the audit dialog can be read without showing it."""
    from deepreefmap_gui.simple.config_audit_dialog import ConfigAuditDialog

    shown: list = []

    def capture(dialog) -> int:
        shown.append(dialog)
        return 0

    monkeypatch.setattr(ConfigAuditDialog, "exec", capture)
    return shown


def test_run_step_names_the_active_settings(window):
    label = window._survey_preset_label.text()
    assert "Settings: Standard reef survey (v1)" in label
    # The technical line stays: the models are what a diver is asked about.
    assert window._collect_run_settings()["mapping_name"] in label


def test_locked_settings_say_who_set_them(window, tmp_path, monkeypatch):
    publish_org_preset(window, tmp_path, monkeypatch)
    label = window._survey_preset_label.text()
    assert "Reef Watch (v2)" in label
    assert "set by your organisation" in label


def test_a_deviation_is_named_on_the_run_step(window):
    window._batch_size_spin.setValue(1)
    window._recompute_survey_start()
    assert "Changed on this machine: frames processed at once." in (window._survey_preset_label.text())
    assert window._survey_restore_btn.isEnabled()


def test_restore_is_offered_only_when_something_deviates(window):
    assert not window._survey_restore_btn.isEnabled()
    assert "Already on the standard" in window._survey_restore_btn.toolTip()
    window._batch_size_spin.setValue(1)
    window._recompute_survey_start()
    assert window._survey_restore_btn.isEnabled()


def test_locked_preset_puts_back_an_edit_it_does_not_allow(window, tmp_path, monkeypatch, machine_preset_path):
    """Expected behaviour: an authoritative configuration the next run would
    silently ignore is not authoritative, so the form goes back to standard."""
    publish_org_preset(window, tmp_path, monkeypatch, grid_bins=1500)
    window._grid_bins_spin.setValue(1234)
    window._adopt_form_as_preset()

    assert window._grid_bins_spin.value() == 1500
    assert window._survey_preset["grid_bins"] == 1500
    assert not machine_preset_path.exists()
    assert "map detail went back to standard: set by Reef Watch" in window._status_label.text()


def test_locked_preset_still_allows_a_machine_setting(window, tmp_path, monkeypatch, machine_preset_path):
    publish_org_preset(window, tmp_path, monkeypatch)
    window._batch_size_spin.setValue(1)
    window._adopt_form_as_preset()

    assert yaml.safe_load(machine_preset_path.read_text())["overrides"] == {"preprocess_batch_size": 1}
    assert "Saved for this machine: frames processed at once." in window._status_label.text()


def test_admin_preset_drives_the_run(window, tmp_path, monkeypatch):
    publish_org_preset(window, tmp_path, monkeypatch, fps=2)
    assert window._collect_run_settings()["fps"] == 2


def test_malformed_admin_preset_blocks_the_gate_rather_than_the_app(window, tmp_path, monkeypatch):
    """A field laptop must still open, and say why it cannot process."""
    admin = tmp_path / "org_preset.yaml"
    admin.write_text("not: [valid yaml")
    monkeypatch.setenv("DEEPREEFMAP_SURVEY_PRESET", str(admin))
    window._reload_active_preset()
    window._recompute_survey_start()

    assert window._active_preset is None
    assert window._survey_preset is None
    assert "could not be loaded" in window._survey_preset_label.text()
    assert not window._survey_start_btn.isEnabled()


def test_settings_history_lists_what_past_runs_used(window, out_root, monkeypatch):
    """The audit surface reads each run's own manifest, so it reports history
    rather than reconstructing it from the current settings."""
    org = window._active_preset.org
    for dir_name, deviations in (("run_a", {}), ("run_b", {"preprocess_batch_size": 1})):
        run_dir = out_root / dir_name
        run_dir.mkdir()
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "name": dir_name,
                    "survey": {"provenance": {"config": manifest_config_block(org, deviations)}},
                }
            )
        )

    shown = captured_audit_dialogs(monkeypatch)
    window._on_show_config_audit()

    dialog = shown[0]
    assert len(dialog._rows) == 2
    notes = {row.dir_name: row.note for row in dialog._rows}
    assert notes["run_a"] == "Standard settings."
    assert "frames processed at once" in notes["run_b"]


def test_settings_history_with_no_runs_says_so(window, monkeypatch):
    shown = captured_audit_dialogs(monkeypatch)
    window._on_show_config_audit()
    assert shown[0]._rows == []


def test_settings_history_table_sorts_by_column(window):
    """Scenario: the dialog receives rows newest first, with no date column.

    Expected behaviour: that order is shown untouched, because nothing in the
    table can restore it once an alphabetical sort has run. Clicking a header
    still sorts.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.simple.config_audit_dialog import ConfigAuditDialog
    from deepreefmap_gui.survey.config_audit import STANDARD, ConfigAuditRow

    org = window._active_preset.org
    rows = [
        ConfigAuditRow("run_b", None, org.label, None, {}, STANDARD, "Standard settings."),
        ConfigAuditRow("run_a", None, org.label, None, {}, STANDARD, "Standard settings."),
    ]
    dialog = ConfigAuditDialog(window, rows, org)
    table = dialog.findChild(QTableWidget)
    header = table.horizontalHeader()
    assert header.property("sortable") == "true"
    assert [table.item(r, 0).text() for r in range(2)] == ["run_b", "run_a"]

    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(2)] == ["run_a", "run_b"]
    dialog.deleteLater()
