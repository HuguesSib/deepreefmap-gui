"""The run settings dialog borrows the window's one run form.

The invariant every test guards: the form goes back to its holder on the window,
whatever way the dialog closes. Left inside a dialog that is going away, every
later _collect_run_settings() would raise on a deleted C++ object.

The rest of the file follows what an edit made here is allowed to change. The
dialog is the only route to the run settings now, so the split between what this
machine owns and what the organisation owns is decided on this path.
"""

from PySide6.QtWidgets import QDialog, QDialogButtonBox

from deepreefmap_gui.simple.settings_dialog import RunSettingsDialog
from deepreefmap_gui.survey.preset import PRESET_KEYS


def per_run_widgets(window):
    """What _on_edit_run_settings hides: the output root, which Setup owns."""
    return [window._output_group]


def edit_then(window, monkeypatch, result, edit):
    """Drive one dialog session: make an edit, then close with `result`."""

    def _exec(_dialog):
        edit()
        return result

    monkeypatch.setattr(RunSettingsDialog, "exec", _exec)
    window._on_edit_run_settings()


def edit_in_dialog(window, monkeypatch, change):
    """Drive a dialog session that ends in OK, so the edit is adopted."""
    edit_then(window, monkeypatch, QDialog.DialogCode.Accepted, change)


def test_dialog_borrows_the_form_and_hands_it_back(window):
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    assert dialog.isAncestorOf(window._setup_page)
    assert not window._output_group.isVisibleTo(dialog)

    dialog.restore_form()
    assert window._form_home.isAncestorOf(window._setup_page)
    assert not window._output_group.isHidden()


def test_every_exit_path_restores_the_form(window):
    for close in (lambda d: d.accept(), lambda d: d.reject(), lambda d: d.close()):
        dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
        close(dialog)
        assert window._form_home.isAncestorOf(window._setup_page)


def test_restoring_twice_is_harmless(window):
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    dialog.restore_form()
    dialog.restore_form()
    assert window._form_home.isAncestorOf(window._setup_page)


def test_settings_edited_in_the_dialog_reach_the_run(window, monkeypatch):
    edit_in_dialog(window, monkeypatch, lambda: window._grid_bins_spin.setValue(1234))
    assert window._form_home.isAncestorOf(window._setup_page)
    assert window._survey_preset["grid_bins"] == 1234
    assert window._collect_run_settings()["grid_bins"] == 1234


def test_only_machine_settings_are_written_back(window, monkeypatch, machine_preset_path):
    """Expected behaviour: the dialog persists what the computer owns and nothing
    else, so editing settings cannot rewrite the organisation preset. An
    allow-listed setting describes the computer, so it survives a restart."""
    import yaml

    def change():
        window._batch_size_spin.setValue(1)
        window._grid_bins_spin.setValue(1234)

    edit_in_dialog(window, monkeypatch, change)
    assert window._survey_preset["preprocess_batch_size"] == 1
    assert yaml.safe_load(machine_preset_path.read_text())["overrides"] == {"preprocess_batch_size": 1}


def test_an_organisation_setting_changes_for_this_batch_only(window, monkeypatch, machine_preset_path):
    """Scenario: a curious diver changes the method in the run settings dialog.

    Expected behaviour: this session runs what they typed, but nothing is written
    back, so the next launch measures the way the organisation asked. Writing the
    whole preset here is what used to let one dive rebrand the machine for good.
    """

    def change():
        window._fps_spin.setValue(3)
        window._seg_combo.setCurrentText("segformer-b2")

    edit_in_dialog(window, monkeypatch, change)
    assert window._survey_preset["fps"] == 3
    assert not machine_preset_path.exists()
    label = window._survey_preset_label.text()
    assert "Changed for this session only" in label
    assert "Back to standard next launch" in label


def test_restore_standard_settings_drops_the_machine_override(window, monkeypatch, machine_preset_path):
    standard = window._active_preset.org.settings["preprocess_batch_size"]
    edit_in_dialog(window, monkeypatch, lambda: window._batch_size_spin.setValue(1))
    assert machine_preset_path.exists()

    window._restore_standard_settings()
    assert not machine_preset_path.exists()
    assert window._batch_size_spin.value() == standard
    assert window._survey_deviations() == {}
    assert "back to Standard reef survey (v1)" in window._status_label.text()


def test_restore_says_so_when_there_is_nothing_to_restore(window):
    window._restore_standard_settings()
    assert "nothing to restore" in window._status_label.text()


def test_restore_is_refused_mid_batch(window, monkeypatch, machine_preset_path):
    edit_in_dialog(window, monkeypatch, lambda: window._batch_size_spin.setValue(1))
    window._survey_worker_running = True
    window._restore_standard_settings()
    assert machine_preset_path.exists()
    assert "Unavailable while processing" in window._status_label.text()


def test_cancel_abandons_the_edit(window, monkeypatch):
    """The dialog edits the live form, so Cancel has to put the values back."""
    before = window._grid_bins_spin.value()
    edit_then(
        window,
        monkeypatch,
        QDialog.DialogCode.Rejected,
        lambda: window._grid_bins_spin.setValue(1234),
    )
    assert window._grid_bins_spin.value() == before
    assert window._collect_run_settings()["grid_bins"] == before
    assert window._survey_preset["grid_bins"] == before


def test_cancel_does_not_write_the_preset_file(window, monkeypatch, tmp_path):
    target = tmp_path / "cancelled.yaml"
    monkeypatch.setattr("deepreefmap_gui.survey.preset.survey_preset_path", lambda: target)
    edit_then(
        window,
        monkeypatch,
        QDialog.DialogCode.Rejected,
        lambda: window._fps_spin.setValue(window._fps_spin.value() + 1),
    )
    assert not target.exists()


def test_cancel_undoes_reset_defaults(window, monkeypatch):
    """Reset writes into the same live form, so it is part of the edit."""
    window._grid_bins_spin.setValue(1234)
    edit_then(window, monkeypatch, QDialog.DialogCode.Rejected, window._reset_form_defaults)
    assert window._grid_bins_spin.value() == 1234


def test_cancel_restores_a_custom_processing_size(window, monkeypatch):
    """The preset stores no size unless the resolution is Custom, so an undo
    built on the preset would lose a Custom size on the way back."""
    window._resolution_preset_combo.setCurrentText("Custom")
    window._proc_width_spin.setValue(800)
    window._proc_height_spin.setValue(600)

    def switch_to_native():
        window._resolution_preset_combo.setCurrentText("Native")

    edit_then(window, monkeypatch, QDialog.DialogCode.Rejected, switch_to_native)
    assert window._resolution_preset_combo.currentText() == "Custom"
    assert (window._proc_width_spin.value(), window._proc_height_spin.value()) == (800, 600)


def test_the_dialog_offers_ok_cancel_and_reset(window):
    dialog = RunSettingsDialog(window, window._setup_page, per_run_widgets(window))
    box = dialog.findChild(QDialogButtonBox)
    offered = {box.standardButton(button) for button in box.buttons()}
    assert offered == {
        QDialogButtonBox.StandardButton.Ok,
        QDialogButtonBox.StandardButton.Cancel,
        QDialogButtonBox.StandardButton.Reset,
    }
    dialog.restore_form()


def test_settings_cannot_be_edited_mid_batch(window):
    window._survey_worker_running = True
    window._on_edit_run_settings()
    assert window._form_home.isAncestorOf(window._setup_page)
    assert "Unavailable while processing" in window._status_label.text()


def test_preset_covers_every_settings_field(window):
    assert set(window._collect_preset_from_form()) == PRESET_KEYS


def test_reset_restores_defaults(window):
    window._batch_size_spin.setValue(window._batch_size_spin.value() + 1)
    window._loger_model_path_input.setText("/tmp/custom.pt")
    window._reset_form_defaults()
    assert window._batch_size_spin.value() == window._form_defaults["_batch_size_spin"]
    assert window._loger_model_path_input.text() == ""


def test_collect_and_populate_round_trip(window):
    window._fps_spin.setValue(3)
    window._seg_combo.setCurrentText("segformer-b2")
    window._map_combo.setCurrentText("scsfmlearner")
    window._grid_bins_spin.setValue(1234)
    window._loger_model_path_input.setText("/tmp/custom.pt")
    window._require_gravity_check.setChecked(True)
    preset = window._collect_preset_from_form()
    window._reset_form_defaults()
    window._populate_form_from_preset(preset)
    assert window._collect_preset_from_form() == preset
    assert window._grid_bins_spin.value() == 1234


def test_native_processing_size_is_not_pinned(window):
    """A non-Custom resolution preset must not freeze the model's native size."""
    window._resolution_preset_combo.setCurrentText("Native")
    preset = window._collect_preset_from_form()
    assert preset["processing_width"] is None
    assert preset["processing_height"] is None


def test_custom_processing_size_round_trips(window):
    window._resolution_preset_combo.setCurrentText("Custom")
    window._proc_width_spin.setValue(800)
    window._proc_height_spin.setValue(600)
    preset = window._collect_preset_from_form()
    assert (preset["processing_width"], preset["processing_height"]) == (800, 600)
    window._reset_form_defaults()
    window._populate_form_from_preset(preset)
    assert (window._proc_width_spin.value(), window._proc_height_spin.value()) == (800, 600)


def test_crop_width_zero_means_disabled(window):
    window._crop_width.setValue(0.0)
    assert window._collect_preset_from_form()["transect_crop_width"] is None
    window._populate_form_from_preset({**window._collect_preset_from_form(), "transect_crop_width": 1.5})
    assert window._crop_width.value() == 1.5
