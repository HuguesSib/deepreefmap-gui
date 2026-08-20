"""The Server section in the shell: what it says, and what a sync does to it.

No test here reaches the network. The registry is a fake object standing in for
`SyncClient`, so the real engine, the real store and the real signals run.
"""

from __future__ import annotations

import time

import pytest
from _factories import make_transect

from deepreefmap_gui.server.page_ui import (
    NOT_CONNECTED,
    ONBOARDED_BY,
    SESSION_RUNNING,
)
from deepreefmap_gui.server.state import (
    DEVICE_NAME_KEY,
    ENROLLED_BY_KEY,
    LAST_SYNC_KEY,
    NOTHING_TO_SYNC,
    SERVER_SECTION,
    SYNC_ERROR_KEY,
)
from deepreefmap_gui.simple.mode import DESTINATIONS, NON_DESTINATIONS, SIMPLE_SECTIONS
from deepreefmap_gui.sync import client as client_mod
from deepreefmap_gui.sync import contract, credentials
from deepreefmap_gui.sync.engine import CONFLICT_DISCARDED, CONTRACT_VERSION_KEY

SERVER_URL = "https://reef.example.org"
TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64
CURSOR = 4830
AGREED = contract.CONTRACT_VERSION


class FakeRegistry:
    """Answers like the registry, and records the order it was asked in."""

    def __init__(self, base_url="", token="", fail=None, skipped=None, omitted=()):
        self.base_url = base_url
        self.token = token
        # What the real client learns from the stamp on a response.
        self.agreed = None
        self._fail = fail
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
        self.pushed.append(dict(sections))
        return {
            "cursor": CURSOR,
            "sections": {
                name: self._outcome(name, rows) for name, rows in sections.items()
            },
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

    def build(*, fail=None, skipped=None, omitted=()):
        def factory(base_url, token=None, timeout=None, agreed=None):
            fake = FakeRegistry(base_url, token or "", fail=fail, skipped=skipped, omitted=omitted)
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


def test_the_server_page_is_a_utility_section_not_a_destination(window):
    """Scenario: the Server page is registered in the shell.

    Expected behaviour: it is a section like Setup and storage, so no pill owns it
    and none is lit while it is open. The four destinations are where the work is;
    this is a connection you check and leave.
    """
    assert SERVER_SECTION in SIMPLE_SECTIONS
    assert SERVER_SECTION in NON_DESTINATIONS
    assert SERVER_SECTION not in DESTINATIONS
    assert list(window._simple_nav_buttons) == list(DESTINATIONS)

    window._set_simple_section(SERVER_SECTION)

    assert window._current_section() == SERVER_SECTION
    assert not any(b.isChecked() for b in window._simple_nav_buttons.values())


def test_the_header_button_goes_there(window):
    window._server_nav_button.click()

    assert window._current_section() == SERVER_SECTION


def test_an_unconnected_install_offers_only_the_connect_button(window):
    window._set_simple_section(SERVER_SECTION)

    assert window._server_empty.isVisibleTo(window)
    assert window._server_connect_btn.isVisibleTo(window)
    assert not window._server_sync_btn.isVisibleTo(window)
    assert not window._server_disconnect_btn.isVisibleTo(window)
    assert NOT_CONNECTED in window._server_empty._message.text()


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


def test_a_successful_connection_reports_the_server_it_found(window, qapp, monkeypatch, caplog):
    """Scenario: a connect code is pasted and accepted.

    Expected behaviour: the page names the address decoded out of the code and the
    store the token went to, and the code itself is neither shown back nor logged.
    """
    from deepreefmap_gui.server import enrolment as enrolment_mod

    secret = "ab" * 32
    pasted = f"drm1.{secret}"

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


def test_a_refused_code_is_reported_on_the_page_when_the_dialog_has_gone(
    window, qapp, monkeypatch
):
    """The dialog can be cancelled mid-enrolment, and the answer still arrives."""
    from deepreefmap_gui.server import enrolment as enrolment_mod

    def fake_connect(code):
        raise client_mod.EnrolmentRejectedError("that code has already been used")

    monkeypatch.setattr(enrolment_mod, "connect", fake_connect)
    window._set_simple_section(SERVER_SECTION)
    window._start_enrolment("drm1.whatever")

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


def test_a_revoked_device_is_asked_to_connect_again(window, qapp, registry):
    enrol_this_device()
    registry(fail=client_mod.DeviceRevokedError("this device's access has been revoked"))
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    assert window._server_blocker._action.text() == "Connect again"


def test_a_contract_mismatch_names_both_versions(window, qapp, registry):
    enrol_this_device()
    registry(
        fail=client_mod.ContractMismatchError(
            "This app speaks metadata contract 1 and the registry speaks 2."
        )
    )
    window._set_simple_section(SERVER_SECTION)

    window._on_sync_now()
    assert settle(qapp, lambda: not window._server_syncing)

    reason = window._server_blocker._reason.text()
    assert "contract 1" in reason and "speaks 2" in reason
    # A fresh connect code fixes nothing here: one side needs updating.
    assert window._server_blocker._action.text() == ""


def test_a_registry_that_stops_saying_which_contract_it_speaks_is_refused(
    window, qapp, registry
):
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

    assert window._current_section() == SERVER_SECTION


def test_disconnecting_forgets_the_token_and_says_only_that(window, monkeypatch):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)
    monkeypatch.setattr("deepreefmap_gui.server.page_ui.confirm", lambda *a: True)

    window._on_disconnect_server()

    assert credentials.load() is None
    assert window._server_empty.isVisibleTo(window)
    assert "does not revoke the device" in window._server_disconnect_btn.toolTip()


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


def test_the_archive_button_says_it_only_sends_on_request(window):
    enrol_this_device()
    window._set_simple_section(SERVER_SECTION)

    assert window._server_archive_btn.isVisibleTo(window)
    tooltip = window._server_archive_btn.toolTip()
    assert "original clips" in tooltip and "finished run" in tooltip
    assert "until this is pressed" in tooltip


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


def test_archiving_sends_the_queue_and_reports_what_landed(
    window, qapp, registry, tmp_path, accept_confirms
):
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


def test_what_the_plan_left_out_is_said_with_what_landed(
    window, qapp, registry, tmp_path, accept_confirms
):
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
    assert settle(qapp, lambda: window._server_archive_cancel_btn.isVisibleTo(window)
                  or not window._server_archiving)
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


def test_an_archive_that_cannot_reach_the_registry_is_a_retry(
    window, qapp, registry, tmp_path, accept_confirms
):
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
        lambda: getattr(window, "_sync_badge_state", None) is not None
        and window._sync_badge_state.connected,
    )

    window._on_sync_badge_clicked()
    assert settle(qapp, lambda: not window._server_syncing)

    assert made[0].calls == ["pull", "push"]
    assert settle(qapp, lambda: "Synced" in window._sync_badge._label.text())


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
    assert rows["Japanese Garden"] == "Djibouti"
    assert rows["2026_08_fiji"] == "2026-08-01"


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
    assert window._current_section() == SERVER_SECTION
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


def test_the_badge_opens_the_server_page_when_not_connected(window, qapp):
    window._refresh_sync_badge()
    assert settle(qapp, lambda: getattr(window, "_sync_badge_state", None) is not None)
    assert "No registry" in window._sync_badge._label.text()

    window._on_sync_badge_clicked()

    assert window._current_section() == SERVER_SECTION


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
    store.upsert_video(
        make_video("cd" * 16, file_name="GX020001.MP4", path=str(other_file))
    )
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
