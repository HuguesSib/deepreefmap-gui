from PySide6.QtWidgets import QLabel, QRadioButton

from deepreefmap_gui.runs.delete_data_dialog import (
    DeleteChoice,
    DeleteDataDialog,
    DeleteScope,
)


def scope(**overrides) -> DeleteScope:
    base = {
        "title": "Delete run",
        "subject": "Delete from 'run_a'?",
        "data_detail": "The output folder on disk, 2.4 GB.",
        "metadata_detail": "One record, a few kilobytes.",
    }
    base.update(overrides)
    return DeleteScope(**base)


def radios(dialog) -> list[QRadioButton]:
    return dialog.findChildren(QRadioButton)


def test_data_is_the_recommended_choice(qapp):
    dialog = DeleteDataDialog(scope(metadata_present=False))
    assert dialog.selected() is DeleteChoice.DATA


def test_the_record_is_gated_while_data_exists(qapp):
    """A record deleted under living data comes back on rescan, so the choice
    is withheld rather than allowed to undo itself."""
    dialog = DeleteDataDialog(scope(metadata_present=False))
    data, metadata, both = radios(dialog)
    assert data.isEnabled() and both.isEnabled()
    assert not metadata.isEnabled()


def test_the_record_becomes_the_choice_once_data_is_gone(qapp):
    dialog = DeleteDataDialog(scope(data_present=False))
    data, metadata, both = radios(dialog)
    assert not data.isEnabled() and not both.isEnabled()
    assert metadata.isEnabled()
    assert dialog.selected() is DeleteChoice.METADATA


def test_what_is_kept_is_spelled_out(qapp):
    dialog = DeleteDataDialog(
        scope(keeps=("Passes and their trims", "Clips in the library"))
    )
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "Kept either way" in text
    assert "Passes and their trims" in text
    assert "Clips in the library" in text
