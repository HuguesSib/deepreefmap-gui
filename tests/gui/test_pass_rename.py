"""Renaming a pass, from each place that now offers it.

A pass's name is pushed to the registry, so this is the one edit on these pages
whose result the console shows. The dialog is stubbed: what is under test is
what reaches the store and what the page says afterwards, not Qt's input box.
"""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtWidgets import QInputDialog, QMenu

from deepreefmap_gui.runs import pass_rename
from deepreefmap_gui.survey.models import TransectPass
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "survey.db")


@pytest.fixture
def answers(monkeypatch):
    """Make the rename dialog answer with whatever the test queues up."""

    def reply(typed: str | None):
        monkeypatch.setattr(
            QInputDialog,
            "getText",
            staticmethod(lambda *a, **k: ("" if typed is None else typed, typed is not None)),
        )

    return reply


def a_pass(store, label: str = "") -> TransectPass:
    """A pass over a clip the store knows, since the pass has a key into it."""
    from _factories import make_video

    video = make_video(content_hash=f"h{uuid.uuid4().hex}")
    video = store.upsert_video(video)
    pass_ = TransectPass(
        transect_id=None, video_id=video.id, begin_s=0.0, end_s=60.0, label=label
    )
    store.add_pass(pass_)
    return pass_


def test_a_typed_name_reaches_the_store(qapp, store, answers) -> None:
    pass_ = a_pass(store)
    answers("Bommie flats")

    assert pass_rename.rename_pass(None, store, pass_.id) is None
    assert store.get_pass(pass_.id).label == "Bommie flats"


def test_cancelling_changes_nothing(qapp, store, answers) -> None:
    pass_ = a_pass(store, "Reef edge")
    answers(None)

    assert pass_rename.rename_pass(None, store, pass_.id) is None
    assert store.get_pass(pass_.id).label == "Reef edge"


def test_clearing_the_field_asks_for_the_derived_name_back(qapp, store, answers) -> None:
    """Empty is a request for the generated name, not for a nameless pass.

    The generated text is never stored: storing it would freeze today's
    generator into every old row.
    """
    pass_ = a_pass(store, "Reef edge")
    answers("   ")

    pass_rename.rename_pass(None, store, pass_.id)
    assert store.get_pass(pass_.id).label == ""


def test_a_name_another_pass_holds_is_taken_and_said(qapp, store, answers) -> None:
    a_pass(store, "Reef edge")
    second = a_pass(store)
    answers("Reef edge")

    said = pass_rename.rename_pass(None, store, second.id)

    assert store.get_pass(second.id).label == "Reef edge 2"
    assert "already called" in said and "Reef edge 2" in said


def test_a_pass_that_has_gone_is_not_renamed(qapp, store, answers) -> None:
    answers("Anything")
    assert pass_rename.rename_pass(None, store, uuid.uuid4()) is None


def test_the_note_says_what_the_pass_is_now_called() -> None:
    """The rows on Videos carry the transect and the window, never the name, so
    the status line is the only place a rename shows."""
    assert "Bommie flats" in pass_rename.renamed_note("Bommie flats")
    assert "derived" in pass_rename.renamed_note("")


def test_every_place_a_pass_is_shown_offers_the_rename(qapp) -> None:
    """Scenario: the rename used to be reachable from the cart alone.

    Expected behaviour: the pass row, the clip's pass list and the pass detail
    pane each carry it. Checked on the widgets rather than through a window, so
    a page that stops relaying the signal fails here rather than going quiet.
    """
    from deepreefmap_gui.runs.section_detail import SectionDetailPanel
    from deepreefmap_gui.runs.video_rows import SectionList, SectionRow, VideoLibraryList

    for owner in (SectionRow, SectionList, SectionDetailPanel):
        assert hasattr(owner, "rename_requested"), owner.__name__
    assert hasattr(VideoLibraryList, "section_rename")

    panel = SectionDetailPanel()
    # The menu is held: an unreferenced QMenu is collected and takes the very
    # QActions being read down with it.
    menu = QMenu(panel)
    labels = [a.text() for a in panel._fill_section_actions(menu).values()]
    assert pass_rename.RENAME_ACTION in labels
