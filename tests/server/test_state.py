"""What the Server page reads, and how it words a failure."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from _factories import make_transect

from deepreefmap_gui.server.state import (
    NOTHING_TO_SYNC,
    RETRY_LATER,
    SYNC_ERROR_KEY,
    default_device_name,
    describe_failure,
    pending_rows,
    read_agreed_contract,
    read_state,
    remember_agreed_contract,
    set_aside_names,
    summarise,
)
from deepreefmap_gui.survey.models import Site
from deepreefmap_gui.sync import client, credentials
from deepreefmap_gui.sync.connect_code import CODE_PREFIX, ConnectCodeError
from deepreefmap_gui.sync.engine import (
    CONTRACT_VERSION_KEY,
    CURSOR_KEY,
    WATERMARK_PREFIX,
    PullReport,
    PushReport,
    SectionPush,
    SyncEngine,
)

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64
BEFORE_ANY_CLOCK = "2000-01-01T00:00:00+00:00"


def test_an_unconnected_install_reads_as_empty_rather_than_failing(store):
    state = read_state(store)

    assert not state.connected
    assert state.base_url == ""
    assert state.waiting == 0


def test_a_connected_install_reports_the_address_it_was_given(store):
    credentials.save("https://reef.example.org", TOKEN)
    store.set_sync_state(CURSOR_KEY, "4830")

    state = read_state(store, device_name="Dive laptop")

    assert state.connected
    assert state.base_url == "https://reef.example.org"
    assert state.device_id == credentials.device_id()
    assert state.device_name == "Dive laptop"
    assert state.cursor == 4830


def test_the_onboarder_is_carried_separately_from_the_device_name(store):
    """Two facts: what uploads are attributed to, and who set the laptop up."""
    credentials.save("https://reef.example.org", TOKEN)

    state = read_state(store, device_name="Dive laptop", enrolled_by="Kim Nguyen")

    assert state.device_name == "Dive laptop"
    assert state.enrolled_by == "Kim Nguyen"


def test_an_unreadable_credential_is_a_fault_rather_than_a_crash(store, monkeypatch):
    monkeypatch.setattr(
        credentials, "load", lambda: (_ for _ in ()).throw(credentials.CredentialsError("mode 644"))
    )

    state = read_state(store)

    assert not state.connected
    assert "mode 644" in state.fault


def test_the_last_syncs_failure_is_read_back_beside_the_credential(store):
    """A revoked token is still a readable credential, so the failure has to be
    its own field or the badge paints a healthy connection."""
    credentials.save("https://reef.example.org", TOKEN)
    store.set_sync_state(SYNC_ERROR_KEY, "This device is no longer enrolled.")

    state = read_state(store)

    assert state.connected
    assert state.sync_fault == "This device is no longer enrolled."


def test_the_row_that_earned_the_watermark_is_not_still_waiting(store):
    """Scenario: a section is pushed, and its watermark is the stamp it earned.

    Expected behaviour: nothing is waiting. Counting the row that carries the
    watermark would leave every synced survey owing one row for ever. The push
    that earned the watermark also cleared the mark the edit left, so the
    fixture does both.
    """
    transect = make_transect()
    store.add_transect(transect)
    store.set_sync_state(f"{WATERMARK_PREFIX}transects", transect.updated_at)
    store.clear_pending_push("transects", [transect.id])

    assert pending_rows(store) == {}

    store.set_sync_state(f"{WATERMARK_PREFIX}transects", BEFORE_ANY_CLOCK)

    assert pending_rows(store) == {"transects": 1}


def test_a_site_made_here_is_waiting_like_any_authored_row(store):
    store.add_site(Site(name="Reef"))

    assert pending_rows(store) == {"sites": 1}


def test_a_sync_that_moved_nothing_says_so():
    assert summarise(PullReport(), PushReport()) == NOTHING_TO_SYNC


def test_a_summary_counts_both_directions_and_what_was_refused():
    site = Site(name="Reef")
    pull = PullReport(overwritten=(site.id,))
    push = PushReport(
        sections={"sites": SectionPush(received=2, applied=1, skipped=(site.id,))}
    )

    line = summarise(pull, push)

    assert "sent 1 row(s)" in line
    assert "already held 1" in line
    assert "1 edit(s) made here were replaced" in line


def test_a_set_aside_record_that_landed_reads_differently_to_one_discarded():
    """Two endings, and only one of them puts the registry's data in the survey.
    Counting a discard among the rows that finally landed announced an arrival
    at the moment the registry's copy was being thrown away."""
    landed = summarise(PullReport(set_aside_cleared=(uuid.uuid4(),)), PushReport())
    assert "1 record(s) set aside earlier finally landed" in landed
    assert "discarded" not in landed

    dropped = summarise(PullReport(set_aside_discarded=(uuid.uuid4(),)), PushReport())
    assert dropped != NOTHING_TO_SYNC
    assert "1 record(s) set aside earlier were discarded" in dropped
    assert "this laptop holds a newer copy" in dropped
    assert "finally landed" not in dropped


def test_an_unreachable_registry_is_a_retry():
    failure = describe_failure(client.ServerUnreachableError("Cannot reach https://reef: timed out"))

    assert not failure.reconnect
    assert "https://reef" in failure.detail
    assert RETRY_LATER in failure.detail


def test_a_revoked_device_asks_for_a_reconnection():
    """A 401 is the one failure a retry cannot fix, so the page offers a new code."""
    failure = describe_failure(client.DeviceRevokedError("access has been revoked"))

    assert failure.reconnect
    assert failure.detail == "access has been revoked"


def test_a_contract_mismatch_names_both_versions():
    failure = describe_failure(
        client.ContractMismatchError(
            "This app speaks metadata contract 1 and the registry speaks 2. Update whichever is older."
        )
    )

    assert not failure.reconnect
    assert "1" in failure.detail and "2" in failure.detail


def test_a_registry_that_says_nothing_is_a_mismatch_and_not_a_reconnection():
    """A fresh connect code fixes nothing: one side of the software is old."""
    failure = describe_failure(
        client.ContractMismatchError(
            "This app speaks metadata contract 1 and the registry did not say which it "
            "speaks. Update the registry before syncing."
        )
    )

    assert not failure.reconnect
    assert "did not say which it speaks" in failure.detail


def test_nothing_is_agreed_until_a_registry_has_stamped(store):
    assert read_agreed_contract(store) is None
    assert read_agreed_contract(None) is None


def test_the_agreed_version_survives_the_session_that_learned_it(store):
    remember_agreed_contract(store, 1)

    assert store.sync_state(CONTRACT_VERSION_KEY) == "1"
    assert read_agreed_contract(store) == 1


def test_an_unreadable_agreed_version_reads_as_nothing_agreed(store):
    store.set_sync_state(CONTRACT_VERSION_KEY, "one")

    assert read_agreed_contract(store) is None


def test_a_withheld_section_is_said_in_the_summary_rather_than_as_a_failure():
    line = summarise(PullReport(omitted_sections=("moorings", "quadrats")), PushReport())

    assert line != NOTHING_TO_SYNC
    assert "2 kind(s) of record this version of the app cannot read" in line
    assert "Everything else synced" in line


def test_a_bad_connect_code_is_reported_as_the_code_and_not_the_server():
    failure = describe_failure(ConnectCodeError(f"A connect code starts with `{CODE_PREFIX}`."))

    assert "connect code" in failure.title
    assert CODE_PREFIX in failure.detail


def test_the_device_name_defaults_to_this_machine(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "reef-laptop.local")

    assert default_device_name() == "reef-laptop"


def test_what_the_registry_sent_and_could_not_be_taken_is_readable(store):
    """Scenario: a pull set a record aside because a line here carries its name,
    and the diver was on the boat when the notification went by.

    Expected behaviour: the page can still name it. The two copies go on
    differing until somebody renames one, and a record nothing can show is one
    nobody will ever act on.
    """
    site = Site(name="Reef")
    store.add_site(site)
    ours = make_transect("Reef Wall", site_id=site.id)
    store.add_transect(ours)
    theirs = uuid.uuid4()
    engine = SyncEngine(
        store,
        _RegistrySending([{
            "id": str(theirs),
            "site_id": str(site.id),
            "name": "Reef Wall",
            "description": "",
            "start_lat": -17.5,
            "start_lon": 177.1,
            "end_lat": -17.5005,
            "end_lon": 177.1005,
            "length_m": 50.0,
            "created_at": BEFORE_ANY_CLOCK,
            "updated_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "deleted_at": None,
        }]),
        out_root=store.path.parent,
    )
    engine.pull()
    credentials.save("https://reef.example.org", TOKEN)

    assert read_state(store).set_aside == ("Reef Wall",)
    assert set_aside_names(None) == (), "no survey open is not an empty quarantine"


class _RegistrySending:
    """One page of transects and then nothing, which is all this needs."""

    def __init__(self, transects):
        self._pages = [{"cursor": 10, "has_more": False, "sections": {"transects": transects}}]

    def pull(self, since=None, limit=1000):
        return self._pages.pop(0) if self._pages else {
            "cursor": 10, "has_more": False, "sections": {}
        }

    def push(self, sections):
        return {"cursor": 10, "sections": {}}
