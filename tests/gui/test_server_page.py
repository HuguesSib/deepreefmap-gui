"""The Server pass in the shell: what it says, and what a sync does to it.

No test here reaches the network. The registry is a fake object standing in for
`SyncClient`, so the real engine, the real store and the real signals run.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from _factories import make_transect

from deepreefmap_gui.server import reachability
from deepreefmap_gui.server.page_ui import (
    GAUGE_STEPS,
    NOT_CONNECTED,
    ONBOARDED_BY,
    SERVER_STATUS,
    SESSION_RUNNING,
    SET_ASIDE,
    _set_aside_value,
)
from deepreefmap_gui.server.state import (
    DEVICE_NAME_KEY,
    ENROLLED_BY_KEY,
    LAST_SYNC_KEY,
    NOTHING_TO_SYNC,
    PULL_LANDED,
    PUSH_LANDED,
    SERVER_SECTION,
    SYNC_ERROR_KEY,
    SYNC_ERROR_KIND_KEY,
    UNREACHABLE_KIND,
)
from deepreefmap_gui.simple.machine import MACHINE_VIEWS
from deepreefmap_gui.simple.mode import DESTINATIONS, NON_DESTINATIONS, SIMPLE_SECTIONS
from deepreefmap_gui.sync import client as client_mod
from deepreefmap_gui.sync import contract, credentials
from deepreefmap_gui.sync.connect_code import CODE_PREFIX
from deepreefmap_gui.sync.engine import (
    CONFLICT_DISCARDED,
    CONTRACT_VERSION_KEY,
    QUARANTINE_KEY,
)

SERVER_URL = "https://reef.example.org"
TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64
CURSOR = 4830
AGREED = contract.CONTRACT_VERSION


class FakeRegistry:
    """Answers like the registry, and records the order it was asked in."""

    def __init__(self, base_url="", token="", fail=None, push_fail=None, skipped=None, omitted=()):
        self.base_url = base_url
        self.token = token
        # What the real client learns from the stamp on a response.
        self.agreed = None
        self._fail = fail
        self._push_fail = push_fail
        self._skipped = skipped or {}
        self._omitted = list(omitted)
        self.calls: list[str] = []
        self.pushed: list[dict] = []
        self.initiated: list[dict] = []

    def pull(self, since=None, limit=1000):
        self.calls.append("pull")
        if self._fail is not None:
            raise self._fail
        self.agreed = AGREED
        return {
            "contract_version": AGREED,
            "cursor": CURSOR,
            "has_more": False,
            "sections": {},
            "omitted_sections": self._omitted,
        }

    def push(self, sections):
        self.calls.append("push")
        if self._push_fail is not None:
            raise self._push_fail
        self.pushed.append(dict(sections))
        return {
            "cursor": CURSOR,
            "sections": {name: self._outcome(name, rows) for name, rows in sections.items()},
        }

    def _outcome(self, name, rows):
        skipped = [str(row_id) for row_id in self._skipped.get(name, ())]
        return {
            "received": len(rows),
            "applied": len(rows) - len(skipped),
            "skipped": skipped,
        }

    def archive_initiate(self, payload):
        self.calls.append("archive_initiate")
        if self._fail is not None:
            raise self._fail
        self.initiated.append(dict(payload))
        # The dedup answer: the queue runs for real, and nothing travels.
        return {"object_id": f"o-{len(self.initiated)}", "status": "complete"}

    def archive_complete(self, object_id, parts):
        self.calls.append("archive_complete")
        return {"object_id": object_id, "status": "uploaded"}


@pytest.fixture(autouse=True)
def _forget_device_identity(qapp):
    """QSettings outlives a test, and both keys decide what the page says."""
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    for key in (DEVICE_NAME_KEY, ENROLLED_BY_KEY):
        settings.remove(key)
    yield
    for key in (DEVICE_NAME_KEY, ENROLLED_BY_KEY):
        settings.remove(key)


@pytest.fixture
def registry(monkeypatch):
    """The one registry every sync in this file talks to."""
    made: list[FakeRegistry] = []

    def build(*, fail=None, push_fail=None, skipped=None, omitted=()):
        def factory(base_url, token=None, timeout=None, agreed=None):
            fake = FakeRegistry(
                base_url,
                token or "",
                fail=fail,
                push_fail=push_fail,
                skipped=skipped,
                omitted=omitted,
            )
            fake.agreed = agreed
            made.append(fake)
            return fake

        monkeypatch.setattr(client_mod, "SyncClient", factory)
        return made

    return build


def enrol_this_device():
    credentials.save(SERVER_URL, TOKEN)


def settle(qapp, ready, timeout=5.0):
    """Deliver queued signals until a worker's result has landed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if ready():
            return True
        time.sleep(0.01)
    return False


def facts(window) -> dict[str, str]:
    return _rows(window._server_facts)


def device_facts(window) -> dict[str, str]:
    return _rows(window._server_device_facts)


def _rows(listing) -> dict[str, str]:
    values = [label.text() for label in listing._values]
    return dict(zip(listing._keys, values, strict=True))


def on_server_page(window) -> bool:
    """Whether the Server view is what the window is showing."""
    return window._current_section() == "machine" and window._machine_view == SERVER_SECTION


def tinted(window, view: str) -> bool:
    """Whether Setup's segment for `view` is painted as having something waiting."""
    from deepreefmap_gui.core.theme import WARNING

    return WARNING in window._machine_view_buttons[view].styleSheet()


def one_note(window, fingerprint: str):
    """The single active notification carrying this fingerprint."""
    found = [note for note in window._notify.active() if note.fingerprint == fingerprint]
    assert len(found) == 1, f"{fingerprint} was posted {len(found)} time(s)"
    return found[0]


def test_the_server_page_is_a_setup_view_not_a_section(window):
    """Scenario: the Server page lives on Setup's segmented control.

    Expected behaviour: no pass, pill or header button of its own; the old
    pass name still routes there because persisted notifications carry it.
    """
    assert SERVER_SECTION in MACHINE_VIEWS
    assert SERVER_SECTION not in SIMPLE_SECTIONS
    assert SERVER_SECTION not in NON_DESTINATIONS
    assert SERVER_SECTION not in DESTINATIONS
    assert not hasattr(window, "_server_nav_button")

    window._set_simple_section(SERVER_SECTION)

    assert on_server_page(window)
    assert not any(b.isChecked() for b in window._simple_nav_buttons.values())


def test_the_setup_segment_goes_there(window):
    window._set_simple_section("machine")

    window._machine_view_buttons[SERVER_SECTION].click()

    assert on_server_page(window)
    assert window._server_empty.isVisibleTo(window)


def test_an_unconnected_install_offers_only_the_connect_button(window):
    window._set_simple_section(SERVER_SECTION)

    assert window._server_empty.isVisibleTo(window)
    assert window._server_connect_btn.isVisibleTo(window)
    assert not window._server_sync_btn.isVisibleTo(window)
    assert not window._server_disconnect_btn.isVisibleTo(window)
    assert NOT_CONNECTED in window._server_empty._message.text()


def test_the_disclaimer_shows_until_the_first_sync(window, qapp, registry):
    """The terms of the exchange are read before it happens: unconnected and
    connected-but-unsynced both show them, the first sync retires them."""
    window._set_simple_section(SERVER_SECTION)
    assert window._server_disclaimer_card.isVisibleTo(window)

    enrol_this_device()
    window._refresh_server_page()
    assert window._server_disclaimer_card.isVisibleTo(window)

    registry()
    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert not window._server_disclaimer_card.isVisibleTo(window)


def test_a_connected_install_names_the_server_and_the_position(window):
    """Where the token is kept is not something an operator can act on, so the page
    lists the connection and the sync position and nothing else."""
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    shown = facts(window)
    assert shown["Server"] == SERVER_URL
    assert shown["Last sync"] == "Never"
    assert shown["Pulled up to"] == "Nothing yet"
    assert window._server_disconnect_note.isVisibleTo(window)


def test_the_device_name_is_shown_as_the_attribution(window):
    """Uploads are attributed to this name, so the page leads with it."""
    enrol_this_device()
    window._settings.setValue(DEVICE_NAME_KEY, "Dive laptop")
    window._set_simple_section(SERVER_SECTION)

    assert window._server_device_label.text() == "Dive laptop"
    assert "This device" not in facts(window)


def test_a_registry_that_reports_no_one_shows_no_onboarder(window):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    assert ONBOARDED_BY not in device_facts(window)


def test_the_page_counts_what_is_waiting_to_go(window):
    enrol_this_device()
    window._survey_store().add_transect(make_transect(name="Reef Wall"))

    window._set_simple_section(SERVER_SECTION)

    assert window._server_waiting_card.isVisibleTo(window)
    assert facts(window)["Waiting to send"] == "1 row(s)"


def test_a_record_this_laptop_would_not_take_stays_readable_on_the_page(window):
    """Scenario: a pull set the registry's copy aside because a line here
    carries its name, days ago, while the diver was in the water.

    Expected behaviour: the page still names it. The notification that
    announced it has long gone, and the two records go on differing until
    somebody renames one of them.
    """
    enrol_this_device()
    window._survey_store().set_sync_state(
        QUARANTINE_KEY, json.dumps([{"section": "transects", "id": str(uuid.uuid4()), "name": "Reef Wall", "row": {}}])
    )

    window._set_simple_section(SERVER_SECTION)

    assert facts(window)[SET_ASIDE] == "Reef Wall (held by the registry, not accepted here)"


def test_a_long_list_of_untaken_records_is_capped():
    """It survives every sync until somebody acts on it, so an argumentative
    registry must not push the rest of the page off the bottom."""
    value = _set_aside_value(["T1", "T2", "T3", "T4", "T5", "T6"])

    assert value.startswith("T1, T2, T3, T4, and 2 more")
    assert "T5" not in value


def test_nothing_set_aside_leaves_the_row_out(window):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    assert SET_ASIDE not in facts(window)


def test_a_successful_connection_reports_the_server_it_found(window, qapp, monkeypatch, caplog):
    """Scenario: a connect code is pasted and accepted.

    Expected behaviour: the page names the address decoded out of the code and the
    store the token went to, and the code itself is neither shown back nor logged.
    """
    from deepreefmap_gui.server import enrolment as enrolment_mod

    secret = "ab" * 32
    pasted = f"{CODE_PREFIX}{secret}"

    def fake_connect(code):
        assert code == pasted
        credentials.save(SERVER_URL, TOKEN, device_id="device-1")
        return enrolment_mod.Connected(
            base_url=SERVER_URL,
            device_id="device-1",
            device_name="Dive laptop",
            enrolled_by="Kim Nguyen",
        )

    monkeypatch.setattr(enrolment_mod, "connect", fake_connect)
    window._set_simple_section(SERVER_SECTION)
    with caplog.at_level("DEBUG"):
        window._start_enrolment(pasted)
        assert settle(qapp, lambda: window._server_notice.isVisibleTo(window))

    message = window._server_notice._message.text()
    assert SERVER_URL in message
    assert secret not in message
    assert secret not in caplog.text
    assert window._settings.value(DEVICE_NAME_KEY) == "Dive laptop"
    assert window._settings.value(ENROLLED_BY_KEY) == "Kim Nguyen"
    assert window._server_device_label.text() == "Dive laptop"
    assert device_facts(window)[ONBOARDED_BY] == "Kim Nguyen"


def test_a_refused_code_is_reported_on_the_page_when_the_dialog_has_gone(window, qapp, monkeypatch):
    """The dialog can be cancelled mid-enrolment, and the answer still arrives."""
    from deepreefmap_gui.server import enrolment as enrolment_mod

    def fake_connect(code):
        raise client_mod.EnrolmentRejectedError("that code has already been used")

    monkeypatch.setattr(enrolment_mod, "connect", fake_connect)
    window._set_simple_section(SERVER_SECTION)
    window._start_enrolment(f"{CODE_PREFIX}whatever")

    assert settle(qapp, lambda: window._server_blocker.isVisibleTo(window))
    assert "already been used" in window._server_blocker._reason.text()
    assert window._server_blocker._action.text() == "Connect again"


def test_a_sync_pulls_before_it_pushes_and_reports_both(window, qapp, registry):
    enrol_this_device()
    window._survey_store().add_transect(make_transect(name="Reef Wall"))
    made = registry()
    window._set_simple_section(SERVER_SECTION)
    seen: list[str] = []
    window._sig_sync_progress.connect(seen.append)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert made[0].calls == ["pull", "push"]
    assert made[0].token == TOKEN
    assert made[0].pushed[0]["transects"][0]["name"] == "Reef Wall"
    assert [text.split(" (")[0] for text in seen] == ["Pulling changes", "Sending 1 row(s)…"]
    assert "sent 1 row(s)" in window._server_notice._message.text()
    assert window._survey_store().sync_state(LAST_SYNC_KEY)
    assert facts(window)["Pulled up to"] == str(CURSOR)
    assert facts(window)["Waiting to send"] == "0 row(s)"


def test_a_survey_the_registry_already_has_says_so(window, qapp, registry):
    enrol_this_device()
    registry()
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._server_notice._message.text() == NOTHING_TO_SYNC


def test_a_session_in_flight_holds_the_sync_back(window, registry):
    """A pull rewrites the rows the batch worker is writing pass statuses into."""
    enrol_this_device()
    made = registry()
    window._set_simple_section(SERVER_SECTION)
    window._survey_worker_running = True

    window._on_sync_now()

    assert made == []
    assert window._server_blocker._reason.text() == SESSION_RUNNING


def test_an_offline_registry_is_a_retry_and_not_a_reconnection(window, qapp, registry):
    enrol_this_device()
    registry(fail=client_mod.ServerUnreachableError("Cannot reach the registry: timed out"))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert "timed out" in window._server_blocker._reason.text()
    # A retry, so the page does not offer a new connect code for it.
    assert window._server_blocker._action.text() == ""
    assert window._server_sync_btn.isEnabled()


def test_a_pull_that_fails_still_lets_the_push_run(window, qapp, registry):
    """Scenario: the registry cannot answer a pull, and this laptop is holding a
    day of field records nothing else has a copy of.

    Expected behaviour: the push runs anyway, and the message says which half
    landed. Running both halves under one try meant a pull that failed the same
    way every sync kept the day's work on the laptop for the rest of the season.
    """
    enrol_this_device()
    window._survey_store().add_transect(make_transect(name="Reef Wall"))
    made = registry(fail=client_mod.ServerUnreachableError("Cannot reach the registry: timed out"))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert made[0].calls[:2] == ["pull", "push"]
    assert made[0].pushed[0]["transects"][0]["name"] == "Reef Wall"
    reason = window._server_blocker._reason.text()
    assert "timed out" in reason
    assert PUSH_LANDED in reason
    # Half an exchange does not date the survey: the badge would read as synced.
    assert window._survey_store().sync_state(LAST_SYNC_KEY) is None


def test_a_push_that_fails_says_the_pull_still_landed(window, qapp, registry):
    """The other half of the same fact, and the one the reader is owed most:
    the records made here did not leave, whatever else arrived."""
    enrol_this_device()
    window._survey_store().add_transect(make_transect(name="Reef Wall"))
    registry(push_fail=client_mod.ServerFaultError("the registry failed on its own side"))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    reason = window._server_blocker._reason.text()
    assert "failed on its own side" in reason
    assert PULL_LANDED in reason
    assert window._survey_store().sync_state(SYNC_ERROR_KEY)


def test_a_revoked_device_is_asked_to_connect_again(window, qapp, registry):
    enrol_this_device()
    registry(fail=client_mod.DeviceRevokedError("this device's access has been revoked"))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._server_blocker._action.text() == "Connect again"


def test_a_contract_mismatch_names_both_versions(window, qapp, registry):
    enrol_this_device()
    registry(fail=client_mod.ContractMismatchError("This app speaks metadata contract 1 and the registry speaks 2."))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    reason = window._server_blocker._reason.text()
    assert "contract 1" in reason and "speaks 2" in reason
    # A fresh connect code fixes nothing here: one side needs updating.
    assert window._server_blocker._action.text() == ""


def test_a_registry_that_stops_saying_which_contract_it_speaks_is_refused(window, qapp, registry):
    """Once a registry has stamped, silence from it is a registry gone backwards."""
    enrol_this_device()
    registry(
        fail=client_mod.ContractMismatchError(
            f"This app speaks metadata contract {AGREED} and the registry did not say "
            "which it speaks. Update the registry before syncing."
        )
    )
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    reason = window._server_blocker._reason.text()
    assert "did not say which it speaks" in reason
    assert window._server_blocker._action.text() == ""


def test_the_agreed_contract_is_kept_and_handed_to_the_next_sync(window, qapp, registry):
    """There is no handshake call, so the stamp on a pull is the whole negotiation."""
    enrol_this_device()
    made = registry()
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._survey_store().sync_state(CONTRACT_VERSION_KEY) == str(AGREED)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    # The archive badge probe builds clients of its own, so pick the syncs.
    synced = [client for client in made if "pull" in client.calls]
    assert synced[1].agreed == AGREED


def test_a_withheld_section_is_a_warning_and_not_a_blocker(window, qapp, registry):
    """The sync did everything it could, and a blocker reads as work that did not land."""
    enrol_this_device()
    registry(omitted=["moorings", "quadrats"])
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    message = window._server_notice._message.text()
    assert "2 kind(s) of record this version of the app cannot read" in message
    assert not window._server_blocker.isVisibleTo(window)


def test_a_row_the_registry_held_newer_reaches_the_bell(window, qapp, registry):
    """Scenario: a local edit is refused because the registry has a newer copy.

    Expected behaviour: the engine's conflict report arrives at the notification
    centre from the worker thread, and pressing it lands on this page. A modal
    would interrupt whoever is mid-dive.
    """
    enrol_this_device()
    transect = make_transect(name="Reef Wall")
    window._survey_store().add_transect(transect)
    registry(skipped={"transects": [transect.id]})
    window._set_simple_section(SERVER_SECTION)
    window._set_simple_section("videos")

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    posted = {note.fingerprint: note for note in window._notify.active()}
    assert CONFLICT_DISCARDED in posted
    assert posted[CONFLICT_DISCARDED].section == SERVER_SECTION

    window._on_notification_activated(posted[CONFLICT_DISCARDED].section)

    assert on_server_page(window)


def test_disconnecting_forgets_the_token_and_says_only_that(window, monkeypatch):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)
    monkeypatch.setattr("deepreefmap_gui.server.page_ui.confirm", lambda *a: True)

    window._on_disconnect_server()

    assert credentials.load() is None
    assert window._server_empty.isVisibleTo(window)
    assert "Revoke the device in the web interface" in window._server_disconnect_btn.toolTip()


def test_disconnecting_is_refusable(window, monkeypatch):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)
    monkeypatch.setattr("deepreefmap_gui.server.page_ui.confirm", lambda *a: False)

    window._on_disconnect_server()

    assert credentials.load() is not None


# --- the archive queue ---


def test_an_unconnected_install_offers_no_archive_button(window):
    window._set_simple_section(SERVER_SECTION)

    assert not window._server_archive_btn.isVisibleTo(window)
    assert not window._server_archive_btn.isEnabled()


def test_the_archive_button_says_what_it_sends(window):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    assert window._server_archive_btn.isVisibleTo(window)
    tooltip = window._server_archive_btn.toolTip()
    assert "original clips" in tooltip and "finished run" in tooltip


@pytest.fixture
def accept_confirms(monkeypatch):
    """Answer the whole-survey archive's size question with yes."""
    from deepreefmap_gui.server import page_ui

    asked: list[str] = []

    def yes(parent, title, text):
        asked.append(text)
        return True

    monkeypatch.setattr(page_ui, "confirm", yes)
    return asked


def test_archiving_sends_the_queue_and_reports_what_landed(window, qapp, registry, tmp_path, accept_confirms):
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    window._survey_store().upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    made = registry()
    window._set_simple_section(SERVER_SECTION)

    window._server_archive_btn.click()
    assert settle(qapp, lambda: not window._server_archiving)

    assert made[0].calls == ["archive_initiate"]
    assert made[0].initiated[0]["kind"] == "video"
    assert len(made[0].initiated[0]["content_hash"]) == 32
    message = window._server_notice._message.text()
    assert message == "Archived 0 file(s), 1 already on the server."
    assert window._server_archive_btn.isEnabled()
    # The confirmation said what the queue weighs before anything travelled.
    assert len(accept_confirms) == 1
    assert "1 file(s)" in accept_confirms[0]
    assert "MB" in accept_confirms[0]


def test_declining_the_size_question_sends_nothing(window, qapp, registry, tmp_path, monkeypatch):
    from deepreefmap_gui.server import page_ui
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    window._survey_store().upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    made = registry()
    monkeypatch.setattr(page_ui, "confirm", lambda *a: False)
    window._set_simple_section(SERVER_SECTION)

    window._on_archive_now()
    assert settle(qapp, lambda: not window._server_archiving)

    assert made[0].calls == []
    assert window._server_archive_btn.isEnabled()


def test_what_the_plan_left_out_is_said_with_what_landed(window, qapp, registry, tmp_path, accept_confirms):
    """A clip whose file has moved must not vanish from the summary: "archived
    the rest" and "archived everything" read the same without it."""
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    store = window._survey_store()
    store.upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    store.upsert_video(VideoAsset(file_name="GX010002.MP4", path=str(tmp_path / "gone.MP4")))
    registry()
    window._set_simple_section(SERVER_SECTION)

    window._server_archive_btn.click()
    assert settle(qapp, lambda: not window._server_archiving)

    message = window._server_notice._message.text()
    assert "1 item(s) were left out" in message
    assert "GX010002.MP4" in message


def test_a_cancelled_archive_says_it_stopped(window, qapp, registry, tmp_path, accept_confirms):
    from deepreefmap_gui.server.page_ui import CANCEL_ARCHIVE
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    window._survey_store().upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    registry()
    window._set_simple_section(SERVER_SECTION)

    window._server_archive_btn.click()
    # The moment the upload worker starts, stopping it is offered; pressing it
    # marks the queue cancelled before the next job is taken.
    assert settle(qapp, lambda: window._server_archive_cancel_btn.isVisibleTo(window) or not window._server_archiving)
    if window._server_archiving:
        assert window._server_archive_cancel_btn.text() == CANCEL_ARCHIVE
        window._on_archive_cancel()
        assert window._archive_cancel.is_set()
    assert settle(qapp, lambda: not window._server_archiving)
    assert not window._server_archive_cancel_btn.isVisibleTo(window)


def test_the_hash_computed_while_planning_lands_without_an_edit_stamp(
    window, qapp, registry, tmp_path, accept_confirms
):
    """The digest identifies the file; it does not edit the clip, so the row
    must not be re-queued for the next metadata push over it."""
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    store = window._survey_store()
    asset = VideoAsset(file_name=clip.name, path=str(clip))
    store.upsert_video(asset)
    before = store.get_video(asset.id).updated_at
    registry()
    window._set_simple_section(SERVER_SECTION)

    window._server_archive_btn.click()
    assert settle(qapp, lambda: not window._server_archiving)

    stored = store.get_video(asset.id)
    assert stored.hash
    assert stored.updated_at == before


def test_the_first_card_of_the_session_probes_the_archive(window, qapp, registry):
    """An enrolled machine must not show a blank badge that reads as "not on the
    server" until its first sync."""
    enrol_this_device()
    registry()

    window._maybe_refresh_archive_badges()
    assert settle(qapp, lambda: not window._archive_badge_scan_running)

    # One attempt per session: a second ask changes nothing until a sync or
    # archive resets the answer.
    window._maybe_refresh_archive_badges()
    assert window._archive_probe_attempted


def test_a_session_in_flight_holds_the_archive_back(window, registry):
    enrol_this_device()
    made = registry()
    window._set_simple_section(SERVER_SECTION)
    window._survey_worker_running = True

    window._on_archive_now()

    assert made == []
    assert window._server_blocker._reason.text() == SESSION_RUNNING


def test_an_archive_that_cannot_reach_the_registry_is_a_retry(window, qapp, registry, tmp_path, accept_confirms):
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    window._survey_store().upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    registry(fail=client_mod.ServerUnreachableError("Cannot reach the registry: timed out"))
    window._set_simple_section(SERVER_SECTION)

    window._on_archive_now()
    assert settle(qapp, lambda: not window._server_archiving)

    # The queue keeps going per file, so an unreachable registry lands as a
    # failure count and a notification rather than as a blocker.
    assert "1 failed" in window._server_notice._message.text()
    posted = {note.fingerprint for note in window._notify.active()}
    assert "archive.upload_failed" in posted


def test_the_upload_gauge_paints_bytes_and_speed(window):
    """Scenario: an archive on a field uplink showed only which file was in flight.

    Expected behaviour: a bar fills with the queue's bytes, in the units the rest
    of the app reads in, and says how fast they are moving.
    """
    from deepreefmap_gui.sync.archive import TransferProgress

    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)
    window._server_archiving = True

    window._on_archive_bytes(TransferProgress(512 * 1024**2, 1024**3, 2 * 1024**2))

    assert window._server_archive_bar.isVisibleTo(window)
    assert window._server_archive_bar.value() == GAUGE_STEPS // 2
    assert window._server_archive_bytes_label.text() == "512 MB of 1.0 GB · 2 MB/s"


def test_the_gauge_empties_when_the_pass_ends(window):
    """A bar left at yesterday's fill reads as an upload that is already running."""
    from deepreefmap_gui.sync.archive import TransferProgress

    window._server_archiving = True
    window._on_archive_bytes(TransferProgress(1024**3, 1024**3, None))
    assert window._server_archive_bar.value() == GAUGE_STEPS

    window._server_archiving = False
    window._set_server_busy(False)

    assert not window._server_archive_bar.isVisibleTo(window)
    assert window._server_archive_bar.value() == 0
    assert window._server_archive_bytes_label.text() == ""


def test_a_reading_that_arrives_after_the_pass_paints_nothing(window):
    """The worker's last readings and its report race up the same queue, so an
    emptied gauge must not fill again behind the summary."""
    from deepreefmap_gui.sync.archive import TransferProgress

    window._server_archiving = True
    window._on_archive_bytes(TransferProgress(512 * 1024**2, 1024**3, None))
    window._server_archiving = False
    window._set_server_busy(False)

    window._on_archive_bytes(TransferProgress(1024**3, 1024**3, None))

    assert not window._server_archive_bar.isVisibleTo(window)
    assert window._server_archive_bar.value() == 0


def test_an_archive_fills_the_gauge_and_clears_it(window, qapp, registry, tmp_path, accept_confirms):
    """Scenario: the whole route from the upload thread to the widget.

    Expected behaviour: readings reach the gauge over the window's own signal,
    the last one has the whole queue on the server, and the gauge is empty and
    hidden by the time the summary is painted.
    """
    from deepreefmap_gui.survey.models import VideoAsset

    enrol_this_device()
    clip = tmp_path / "GX010001.MP4"
    clip.write_bytes(b"reef footage")
    window._survey_store().upsert_video(VideoAsset(file_name=clip.name, path=str(clip)))
    registry()
    window._set_simple_section(SERVER_SECTION)
    filled: list[int] = []
    window._sig_archive_bytes.connect(lambda _: filled.append(window._server_archive_bar.value()))
    readings = []
    window._sig_archive_bytes.connect(readings.append)

    window._server_archive_btn.click()
    assert settle(qapp, lambda: not window._server_archiving)

    assert readings[0].done_bytes == 0
    assert readings[-1].done_bytes == readings[-1].total_bytes == clip.stat().st_size
    assert filled[-1] == GAUGE_STEPS
    assert not window._server_archive_bar.isVisibleTo(window)
    assert window._server_archive_bar.value() == 0


def test_a_card_archive_stays_where_it_was_pressed(window, qapp, registry):
    """The run's own row and card carry the answer, so the press does not move
    the reader to the Server page. Its summary is still written there."""
    enrol_this_device()
    registry()
    window._set_simple_section("videos")

    window._archive_run("no-such-run")
    assert settle(qapp, lambda: not window._server_archiving)

    assert not on_server_page(window)
    assert window._current_section() == "videos"
    assert "Archived 0 file(s)" in window._server_notice._message.text()


def test_an_unenrolled_archive_says_to_connect_first(window):
    """The Connect offer lives on the Server page, so an archive pressed
    without a registry lands there."""
    from deepreefmap_gui.server.page_ui import ARCHIVE_NOT_CONNECTED, CONNECT

    window._set_simple_section("videos")

    window._archive_run("some-run")

    assert on_server_page(window)
    assert window._server_blocker._reason.text() == ARCHIVE_NOT_CONNECTED
    assert window._server_blocker._action.text() == CONNECT


# --- the preset model offer ---

PRESET_SETTINGS = {
    "mapping_name": "scsfmlearner",
    "segmentation_name": "segformer-b2",
    "skip_segmentation": False,
}


def model_states(*cached_names):
    """What _refresh_model_status would have delivered from its worker."""
    from deepreefmap_gui.models.cache import ALL_MODELS

    return [(info, info.name in cached_names) for info in ALL_MODELS]


def assign_preset(window, name="Expedition standard", version=2, settings=None):
    """Land the assignment as a heartbeat would, and the row as a pull would."""
    import json

    from deepreefmap_gui.survey.models.server_preset import ServerPreset
    from deepreefmap_gui.survey.preset import ASSIGNED_PRESET_KEY

    store = window._survey_store()
    store.set_sync_state(ASSIGNED_PRESET_KEY, json.dumps({"name": name, "version": version}))
    if settings is not None:
        store._add("server_preset", ServerPreset(name=name, version=version, settings=settings))
    return store


def test_a_sync_offers_to_download_what_the_assigned_preset_needs(window, qapp, registry, monkeypatch):
    enrol_this_device()
    assign_preset(window, settings=PRESET_SETTINGS)
    window._last_model_states = model_states()
    registry()
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    strip = window._preset_models_notice
    assert strip.isVisibleTo(window)
    message = strip._message.text()
    assert "Expedition standard" in message
    assert "scsfmlearner" in message and "segformer-b2" in message
    posted = {note.fingerprint for note in window._notify.active()}
    assert "presets.models_missing.Expedition standard.2" in posted

    asked: list[str] = []
    monkeypatch.setattr(window, "_download_model", asked.append)
    strip._action.click()

    assert asked == ["scsfmlearner", "segformer-b2"]
    assert not strip.isVisibleTo(window)


def test_a_preset_whose_models_are_cached_offers_nothing(window):
    enrol_this_device()
    store = assign_preset(window, settings=PRESET_SETTINGS)
    window._last_model_states = model_states("scsfmlearner", "segformer-b2")
    window._set_simple_section(SERVER_SECTION)

    window._offer_preset_model_downloads(store)

    assert not window._preset_models_notice.isVisibleTo(window)


def test_the_offer_waits_for_the_preset_row_to_be_pulled(window):
    """The heartbeat can assign a preset before the row itself has come down,
    so the offer holds its tongue until a pull lands it."""
    enrol_this_device()
    store = assign_preset(window)
    window._last_model_states = model_states()
    window._set_simple_section(SERVER_SECTION)

    window._offer_preset_model_downloads(store)
    assert not window._preset_models_notice.isVisibleTo(window)

    assign_preset(window, settings=PRESET_SETTINGS)
    window._offer_preset_model_downloads(store)
    assert window._preset_models_notice.isVisibleTo(window)


def test_the_offer_reaches_a_reader_who_is_not_on_the_server_page(window, qapp, registry):
    """Scenario: a sync run from the status-bar badge, which is the usual way.

    Expected behaviour: the strip on the Server page is not the only telling.
    Setup's Models segment is tinted, and the message the bell carries opens
    the model library rather than whichever Setup view was last on screen.
    """
    from deepreefmap_gui.models.cache_ui import MODELS_SECTION

    enrol_this_device()
    assign_preset(window, settings=PRESET_SETTINGS)
    window._last_model_states = model_states()
    registry()
    window._set_simple_section("videos")

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._current_section() == "videos"
    assert tinted(window, "models")
    note = one_note(window, "presets.models_missing.Expedition standard.2")
    assert note.section == MODELS_SECTION

    window._on_notification_activated(note.section)

    assert window._current_section() == "machine"
    assert window._machine_view == "models"


def test_taking_the_offer_puts_the_models_segment_back(window, monkeypatch):
    enrol_this_device()
    store = assign_preset(window, settings=PRESET_SETTINGS)
    window._last_model_states = model_states()
    window._offer_preset_model_downloads(store)
    assert tinted(window, "models")

    monkeypatch.setattr(window, "_download_model", lambda name: None)
    window._preset_models_notice._action.click()

    assert not tinted(window, "models")


def test_unchecked_models_are_not_offered_as_missing(window):
    """Until the first status refresh lands, nothing has been verified, and a
    model merely not yet checked must not read as a model absent."""
    enrol_this_device()
    store = assign_preset(window, settings=PRESET_SETTINGS)
    window._last_model_states = []
    window._set_simple_section(SERVER_SECTION)

    window._offer_preset_model_downloads(store)

    assert not window._preset_models_notice.isVisibleTo(window)


# --- the status-bar badge ---


def test_the_badge_counts_what_is_waiting(window, qapp):
    enrol_this_device()
    window._survey_store().add_transect(make_transect())

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "1 to send" in window._sync_badge._label.text())
    assert "1 transects" in window._sync_badge.toolTip()


def test_the_badge_syncs_on_press_when_it_can(window, qapp, registry):
    enrol_this_device()
    window._survey_store().add_transect(make_transect())
    made = registry()
    window._refresh_sync_badge()
    assert settle(
        qapp,
        lambda: getattr(window, "_sync_badge_state", None) is not None and window._sync_badge_state.connected,
    )

    window._on_sync_badge_clicked()
    assert settle(qapp, lambda: not window._server_syncing)

    assert made[0].calls == ["pull", "push"]
    assert settle(qapp, lambda: "Synced" in window._sync_badge._label.text())


def test_the_badge_says_the_server_is_unavailable_when_nothing_answers(window, qapp, server_probe):
    """Scenario: the laptop is enrolled and carried out of wifi.

    Expected behaviour: the badge names the server rather than counting rows.
    Nothing here is lost and nothing the diver did is wrong, so the fault the
    last sync recorded is not the answer to lead with.
    """
    enrol_this_device()
    window._survey_store().add_transect(make_transect())
    window._survey_store().set_sync_state(SYNC_ERROR_KEY, "The registry did not answer. Cannot reach it.")
    server_probe(reachability.OFFLINE, "Cannot reach https://reef.example.org: no route.")

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "Server unavailable" in window._sync_badge._label.text())
    assert "no route" in window._sync_badge.toolTip()


def test_the_badge_reads_a_refusal_the_registry_has_not_been_asked_for_yet(window, qapp, server_probe):
    """Scenario: the device was revoked in the console between syncs.

    Expected behaviour: the probe carries the device token, so this is known
    before the next sync runs, and it is not called unavailable: the registry
    is up, and only a fresh connect code fixes it.
    """
    enrol_this_device()
    server_probe(reachability.DENIED, "Answered in 12 ms and would not have this device. Access revoked.")

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "Reconnect needed" in window._sync_badge._label.text())
    assert "revoked" in window._sync_badge.toolTip()
    # Neither of the other two faces: the server is up, and no sync is at fault.
    assert "Server unavailable" not in window._sync_badge._label.text()
    assert "Sync fault" not in window._sync_badge._label.text()


def test_a_sync_that_got_no_answer_is_not_worded_as_a_fault(window, qapp):
    """Scenario: the app is reopened after a sync that never reached the server,
    before the first probe of this session has answered.

    Expected behaviour: the stored failure already said nothing answered, so the
    badge says the server is unavailable rather than that a sync is at fault.
    """
    enrol_this_device()
    store = window._survey_store()
    store.set_sync_state(SYNC_ERROR_KEY, "The registry did not answer. Cannot reach it.")
    store.set_sync_state(SYNC_ERROR_KIND_KEY, UNREACHABLE_KIND)

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "Server unavailable" in window._sync_badge._label.text())
    assert "Sync fault" not in window._sync_badge._label.text()


def test_a_sync_the_registry_refused_is_still_a_fault(window, qapp):
    """The other half: a registry that answered and rejected the document is a
    fault, and must not be softened into a server being unavailable."""
    enrol_this_device()
    window._survey_store().set_sync_state(SYNC_ERROR_KEY, "The registry would not take this document.")

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "Sync fault" in window._sync_badge._label.text())


def test_the_recorded_failure_kind_survives_the_sync(window, qapp, registry):
    """What the badge reads next launch is written by the sync that failed."""
    from deepreefmap_gui.sync import client as client_module

    enrol_this_device()
    registry(fail=client_module.ServerUnreachableError("Cannot reach the registry."))
    window._survey_store().add_transect(make_transect())

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._survey_store().sync_state(SYNC_ERROR_KIND_KEY) == UNREACHABLE_KIND


def test_a_registry_answering_leaves_the_badge_counting_rows(window, qapp, server_probe):
    enrol_this_device()
    window._survey_store().add_transect(make_transect())
    server_probe(reachability.ONLINE, "Answered in 12 ms.")

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "1 to send" in window._sync_badge._label.text())


def test_a_registry_without_an_identity_route_is_not_called_unavailable(window, qapp, server_probe):
    """Expected behaviour: a 404 proves something is there, so an older registry
    must not read as a server that has gone away."""
    enrol_this_device()
    server_probe(reachability.DEGRADED, "Answered in 12 ms, but has no identity route.")

    window._refresh_sync_badge()

    assert settle(qapp, lambda: getattr(window, "_sync_badge_state", None) is not None)
    assert "Server unavailable" not in window._sync_badge._label.text()


def test_the_connection_card_reports_what_the_server_said(window, qapp, server_probe):
    enrol_this_device()
    server_probe(reachability.OFFLINE, "Cannot reach https://reef.example.org: no route.")
    window._set_simple_section(SERVER_SECTION)

    window._refresh_sync_badge()
    assert settle(qapp, lambda: "Unavailable" in facts(window).get(SERVER_STATUS, ""))

    status = facts(window)[SERVER_STATUS]
    assert "no route" in status
    assert "just now" in status


def test_the_connection_card_says_when_the_server_has_not_been_asked(window):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    window._refresh_server_page()

    assert facts(window)[SERVER_STATUS].startswith("Unknown")


def test_pulled_sites_and_campaigns_are_listed_by_name(window):
    """The card names what came down: a diver about to file against a reef wants
    to see that the reef actually arrived, not a count of rows."""
    from deepreefmap_gui.survey.models import Campaign, Site

    enrol_this_device()
    store = window._survey_store()
    store.add_site(Site(name="Japanese Garden", country="Djibouti"))
    store.add_campaign(Campaign(name="2026_08_fiji", begin_date="2026-08-01"))

    window._set_simple_section(SERVER_SECTION)

    assert window._server_reference_card.isVisibleTo(window)
    rows = _rows(window._server_reference)
    assert rows["Japanese Garden"] == "Djibouti, 0 transects"
    assert rows["2026_08_fiji"] == "2026-08-01, 0 passes"


def test_the_reference_card_hides_until_something_has_been_pulled(window):
    enrol_this_device()

    window._set_simple_section(SERVER_SECTION)

    assert not window._server_reference_card.isVisibleTo(window)


def test_the_badge_does_not_claim_synced_with_no_survey_open(window, qapp, monkeypatch):
    """Enrolled but no output root: nothing was counted, which is not the same
    answer as everything having been sent."""
    enrol_this_device()
    monkeypatch.setattr(window, "_try_survey_store", lambda: None)

    window._refresh_sync_badge()

    assert settle(qapp, lambda: "Connected" in window._sync_badge._label.text())
    assert "Synced" not in window._sync_badge._label.text()
    assert "Open an output folder" in window._sync_badge.toolTip()


def test_a_transient_blocker_clears_on_the_next_page_refresh(window, registry):
    """The session-running message describes a moment, not a stored fault, so a
    repaint after the session must not keep showing it."""
    enrol_this_device()
    registry()
    window._set_simple_section(SERVER_SECTION)
    window._survey_worker_running = True
    window._on_sync_now()
    assert window._server_blocker._reason.text() == SESSION_RUNNING

    window._survey_worker_running = False
    window._refresh_server_page()

    assert not window._server_blocker.isVisibleTo(window)


def test_a_failed_sync_keeps_the_badge_faulted_across_repaints(window, qapp, registry):
    """Scenario: the registry revoked this device while the laptop was away.

    Expected behaviour: the badge shows the fault rather than a stale green tick.
    The badge repaints from disk on a timer, so the fault has to survive a
    re-read, and a press lands on the Server page instead of another doomed sync.
    """
    enrol_this_device()
    made = registry(fail=client_mod.DeviceRevokedError("access has been revoked"))
    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    window._refresh_sync_badge()
    assert settle(qapp, lambda: "Sync fault" in window._sync_badge._label.text())
    assert "access has been revoked" in window._sync_badge.toolTip()

    pulls_before = sum(fake.calls.count("pull") for fake in made)
    window._on_sync_badge_clicked()

    assert sum(fake.calls.count("pull") for fake in made) == pulls_before
    assert on_server_page(window)
    assert "access has been revoked" in window._server_blocker._reason.text()


def test_a_successful_sync_clears_the_badge_fault(window, qapp, registry):
    enrol_this_device()
    window._survey_store().set_sync_state(SYNC_ERROR_KEY, "The registry did not answer.")
    registry()

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._survey_store().sync_state(SYNC_ERROR_KEY) is None
    window._refresh_sync_badge()
    assert settle(qapp, lambda: "Synced" in window._sync_badge._label.text())


def test_the_badge_hides_until_an_enrolment_exists(window, qapp):
    """An unconnected status row does not advertise a registry nobody joined:
    the Server segment on Setup is the way in. Disconnecting, or a revocation
    that forgets the token, takes the badge away again on the next repaint."""
    from deepreefmap_gui.server import enrolment as enrolment_mod

    window._refresh_sync_badge()
    assert settle(qapp, lambda: getattr(window, "_sync_badge_state", None) is not None)
    assert not window._sync_badge.isVisibleTo(window)

    enrol_this_device()
    window._refresh_sync_badge()
    assert settle(qapp, lambda: window._sync_badge.isVisibleTo(window))

    enrolment_mod.forget(window._try_survey_store())
    window._refresh_sync_badge()
    assert settle(qapp, lambda: not window._sync_badge.isVisibleTo(window))


def test_a_single_clip_is_archived_from_its_id(window, qapp, registry, tmp_path):
    """Scenario: the clip card's Archive button, on one clip of two.

    Expected behaviour: exactly that clip is offered, and the notice counts it
    as already on the server, since the fake registry answers the dedup case.
    """
    from _factories import make_video

    enrol_this_device()
    clip_file = tmp_path / "GX010001.MP4"
    clip_file.write_bytes(b"reef " * 100)
    other_file = tmp_path / "GX020001.MP4"
    other_file.write_bytes(b"wall " * 100)
    store = window._survey_store()
    wanted = store.upsert_video(make_video("ab" * 16, path=str(clip_file)))
    store.upsert_video(make_video("cd" * 16, file_name="GX020001.MP4", path=str(other_file)))
    made = registry()

    window._archive_video(str(wanted.id))
    assert settle(qapp, lambda: not window._server_archiving)

    assert len(made[0].initiated) == 1
    assert made[0].initiated[0]["kind"] == "video"
    assert "1 already on the server" in window._server_notice._message.text()


def test_the_probe_paints_the_clip_badge(window, qapp):
    from deepreefmap_gui.sync.archive import ArchiveStates

    detail = window._video_detail

    class Entry:
        def __init__(self, video):
            self.video = video

    from _factories import make_video

    video = window._survey_store().upsert_video(make_video("ab" * 16))
    detail._entry = Entry(video)

    window._apply_archive_states(ArchiveStates(videos={str(video.id): "archived"}))

    assert detail.archive_state.isVisibleTo(detail)
    assert "On server" in detail.archive_state.text()

    window._apply_archive_states(None)

    assert not detail.archive_state.isVisibleTo(detail)
