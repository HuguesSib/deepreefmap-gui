"""The missing-data checklist: grouped by level, and filled in where it stands."""

import pytest
from _factories import make_transect, make_video
from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QMessageBox

from deepreefmap_gui.simple.expedition_dialog import (
    COMPLETE_NOTE,
    GAP_ROLE,
    ChecklistActions,
    MissingDataDialog,
)
from deepreefmap_gui.survey.completeness import CLIP_REVIEW, CLIP_RIG, PASS_UNTRIMMED
from deepreefmap_gui.survey.models import TransectPass
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.store import SurveyStore

pytestmark = pytest.mark.usefixtures("qapp")


class Spy:
    """The window's side of the checklist, recorded."""

    def __init__(self, store, *, locked=()):
        self.store = store
        self.trimmed = []
        self.queued = []
        self.locked = set(locked)

    def actions(self, trim_to=None):
        def trim(pass_id):
            self.trimmed.append(pass_id)
            if trim_to is not None:
                pass_ = self.store.get_pass(pass_id)
                pass_.begin_s, pass_.end_s = trim_to
                self.store.update_pass(pass_)

        return ChecklistActions(
            trim=trim, queue=self.queued.append, read_only=lambda row: getattr(row, "id", None) in self.locked
        )


@pytest.fixture
def store(tmp_path):
    return SurveyStore(tmp_path / "survey.db")


@pytest.fixture
def expedition(store):
    """One campaign, one transect, two whole-clip passes, nothing else known."""
    campaign = Campaign(name="2026_11_JO_EXPE")
    store.add_campaign(campaign)
    transect = make_transect("VC_6m_01", start_lat=None, start_lon=None, end_lat=None, end_lon=None, length_m=None)
    store.add_transect(transect)
    passes = []
    for n in (1, 2):
        video = store.upsert_video(
            make_video(f"{n}" * 32, file_name=f"GX01000{n}.MP4", path=f"/d/{n}.MP4", duration_s=100.0)
        )
        pass_ = TransectPass(
            transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=100.0,
            direction="forward", campaign_id=campaign.id, label="pass_01_control",
        )
        store.add_pass(pass_)
        passes.append(pass_)
    return campaign, transect, passes


def _select(dialog, lines):
    """Cursor on the first, then the rest added, the way a shift-click picks them."""
    dialog.tree.setCurrentItem(lines[0])
    for line in lines:
        line.setSelected(True)


def _lines(dialog, code):
    return [line for line in dialog.lines() if line.data(0, GAP_ROLE).code == code]


def _answer(monkeypatch, fill):
    """Stand in for the small form: ``fill`` sets the fields, the form says OK."""

    def ask(self, title, rows, note=""):
        fill(dict(rows))
        return True

    monkeypatch.setattr(MissingDataDialog, "_ask", ask)


def test_a_complete_expedition_says_nothing_is_missing(store):
    campaign = Campaign(name="done", begin_date="2026-11-20", end_date="2026-11-27")
    store.add_campaign(campaign)
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())

    assert dialog.lines() == []
    assert dialog.summary.text() == COMPLETE_NOTE


def test_dates_are_filled_without_leaving_the_list(store, expedition, monkeypatch):
    campaign, _t, _p = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    _select(dialog, dialog.lines()[:1])

    def fill(fields):
        fields["First day"].setDate(QDate(2026, 11, 20))
        fields["Last day"].setDate(QDate(2026, 11, 27))

    _answer(monkeypatch, fill)
    dialog.fill_selected()

    stored = store.get_campaign(campaign.id)
    assert (stored.begin_date, stored.end_date) == ("2026-11-20", "2026-11-27")
    assert not any(line.text(1) == "start and end dates" for line in dialog.lines())
    # The cursor is on what comes next (here the next kind, folded), so Enter
    # carries on down the list.
    assert dialog.tree.currentItem().text(0) == "start and end coordinates (1)"


def test_lines_of_one_kind_selected_together_are_filled_at_once(store, expedition, monkeypatch):
    campaign, _t, passes = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    reviews = _lines(dialog, CLIP_REVIEW)
    _select(dialog, reviews)
    assert dialog.fill_btn.text() == "Fill in review for 2…"

    monkeypatch.setattr(
        MissingDataDialog, "_choose", lambda self, title, label, choices, note="": "usable"
    )
    dialog.fill_selected()

    assert {store.get_video(p.video_id).review for p in passes} == {"usable"}
    assert _lines(dialog, CLIP_REVIEW) == []


def test_enter_fills_the_line_under_the_cursor(store, expedition, monkeypatch):
    campaign, transect, _p = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    length = next(line for line in dialog.lines() if line.text(1) == "tape length")
    _select(dialog, [length])
    _answer(monkeypatch, lambda fields: fields["Tape length"].setValue(42.5))

    dialog.tree.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))

    assert store.get_transect(transect.id).length_m == 42.5


def test_coordinates_are_read_as_the_gps_gives_them(store, expedition, monkeypatch):
    campaign, transect, _p = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    line = next(line for line in dialog.lines() if line.text(1) == "start and end coordinates")
    _select(dialog, [line])

    def fill(fields):
        fields["Start"].setText("29°27.07'N 34°58.39'E")
        fields["End"].setText("29.4520 34.9740")

    _answer(monkeypatch, fill)
    dialog.fill_selected()

    stored = store.get_transect(transect.id)
    assert stored.start_lat == pytest.approx(29.45117, abs=1e-4)
    assert (stored.end_lat, stored.end_lon) == (29.452, 34.974)


def test_trimming_opens_each_pass_in_turn(store, expedition):
    campaign, _t, passes = expedition
    spy = Spy(store)
    dialog = MissingDataDialog(None, store, campaign.id, spy.actions(trim_to=(12.0, 80.0)))
    untrimmed = _lines(dialog, PASS_UNTRIMMED)
    _select(dialog, untrimmed)

    dialog.fill_selected()

    assert spy.trimmed == [p.id for p in passes]
    assert all((store.get_pass(p.id).begin_s, store.get_pass(p.id).end_s) == (12.0, 80.0) for p in passes)
    assert _lines(dialog, PASS_UNTRIMMED) == []


def test_closing_the_scrubber_ends_the_round(store, expedition):
    campaign, _t, passes = expedition
    spy = Spy(store)
    dialog = MissingDataDialog(None, store, campaign.id, spy.actions(trim_to=None))  # nothing saved
    untrimmed = _lines(dialog, PASS_UNTRIMMED)
    _select(dialog, untrimmed)

    dialog.fill_selected()

    assert spy.trimmed == [passes[0].id]  # the second one was never opened


def test_a_row_from_another_laptop_is_left_alone(store, expedition, monkeypatch):
    campaign, _t, passes = expedition
    locked_clip = passes[0].video_id
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store, locked={locked_clip}).actions())
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a))
    monkeypatch.setattr(MissingDataDialog, "_choose", lambda self, title, label, choices, note="": "usable")
    reviews = _lines(dialog, CLIP_REVIEW)
    _select(dialog, reviews)

    dialog.fill_selected()

    assert store.get_video(locked_clip).review == "unreviewed"
    assert store.get_video(passes[1].video_id).review == "usable"
    assert told


def test_queuing_hands_the_passes_to_the_window(store, expedition, monkeypatch):
    campaign, _t, passes = expedition
    spy = Spy(store)
    dialog = MissingDataDialog(None, store, campaign.id, spy.actions())
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    not_run = [line for line in dialog.lines() if line.text(1) == "not processed yet"]
    _select(dialog, not_run)

    dialog.fill_selected()

    assert spy.queued == [p.id for p in passes]


def test_a_cancelled_choice_changes_nothing(store, expedition, monkeypatch):
    campaign, _t, passes = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    monkeypatch.setattr(MissingDataDialog, "_ask", lambda self, title, rows, note="": False)
    quality = next(line for line in dialog.lines() if line.text(1) == "quality")
    _select(dialog, [quality])

    dialog.fill_selected()

    assert store.get_pass(passes[0].id).quality is None


def test_a_kind_branch_fills_every_line_under_it(store, expedition, monkeypatch):
    """Selecting "review (2)" is selecting every clip's review."""
    campaign, _t, passes = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    branch = _lines(dialog, CLIP_REVIEW)[0].parent()
    assert branch.text(0) == "review (2)"
    _select(dialog, [branch])
    assert dialog.fill_btn.text() == "Fill in review for 2…"
    monkeypatch.setattr(MissingDataDialog, "_choose", lambda self, title, label, choices, note="": "excluded")

    dialog.fill_selected()

    assert {store.get_video(p.video_id).review for p in passes} == {"excluded"}


def test_after_a_whole_kind_the_cursor_is_on_the_next_kind(store, expedition, monkeypatch):
    campaign, _t, _p = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    rig = _lines(dialog, CLIP_RIG)[0].parent()
    _select(dialog, [rig])
    monkeypatch.setattr(MissingDataDialog, "_choose", lambda self, title, label, choices, note="": "left")

    dialog.fill_selected()

    assert dialog.tree.currentItem().text(0) == "review (2)"


@pytest.mark.real_modal
def test_the_real_form_hands_back_what_was_typed(store, expedition):
    """No stand-in for the form: its fields must still be readable after OK."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDateEdit

    campaign, _t, _p = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    _select(dialog, dialog.lines()[:1])

    def press_ok():
        form = QApplication.activeModalWidget()
        first, last = form.findChildren(QDateEdit)
        first.setDate(QDate(2026, 11, 20))
        last.setDate(QDate(2026, 11, 27))
        form.accept()

    QTimer.singleShot(0, press_ok)
    dialog.fill_selected()

    stored = store.get_campaign(campaign.id)
    assert (stored.begin_date, stored.end_date) == ("2026-11-20", "2026-11-27")


def test_the_rig_question_can_mark_clips_as_looking_down(store, expedition, monkeypatch):
    campaign, _t, passes = expedition
    dialog = MissingDataDialog(None, store, campaign.id, Spy(store).actions())
    _select(dialog, _lines(dialog, CLIP_RIG))
    monkeypatch.setattr(MissingDataDialog, "_choose", lambda self, title, label, choices, note="": "down")

    dialog.fill_selected()

    assert {store.get_video(p.video_id).view for p in passes} == {"down"}
    assert {store.get_video(p.video_id).rig_position for p in passes} == {None}
    assert _lines(dialog, CLIP_RIG) == [] and _lines(dialog, PASS_UNTRIMMED) == []
