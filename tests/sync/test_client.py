"""The registry client against a loopback server.

Nothing here reaches a network: every response is served by a local HTTPServer, and
the address under test is 127.0.0.1. A test that asserted a failure would otherwise
pass offline for the wrong reason.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deepreefmap_gui.sync import client as client_mod
from deepreefmap_gui.sync.client import (
    CONTRACT_HEADER,
    CONTRACT_RANGE,
    CONTRACT_VERSION,
    READ_SECTIONS,
    SECTIONS_HEADER,
    AccessDeniedError,
    ConflictError,
    ContractMismatchError,
    DeviceRevokedError,
    EnrolmentRejectedError,
    RejectedError,
    ServerFaultError,
    ServerUnreachableError,
    SyncClient,
    enrol,
)
from deepreefmap_gui.sync.connect_code import decode_connect_code

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64


class FakeRegistry:
    """Programmed responses per path, plus the requests that arrived."""

    def __init__(self) -> None:
        self.base_url = ""
        self.responses: dict[str, tuple[int, dict, dict]] = {}
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def reply(self, path: str, status: int, body: dict, headers: dict | None = None) -> None:
        self.responses[path] = (status, body, headers or {})


@pytest.fixture
def registry():
    fake = FakeRegistry()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self, body):
            path = self.path.split("?")[0]
            fake.requests.append((self.command, self.path, body, dict(self.headers)))
            status, payload, headers = fake.responses.get(
                path, (404, {"error": f"no route {path}"}, {})
            )
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._serve(None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self._serve(json.loads(self.rfile.read(length) or b"null"))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_client(registry, token=TOKEN, suffix="", agreed=None):
    return SyncClient(registry.base_url + suffix, token=token, timeout=5.0, agreed=agreed)


def serve_pull(registry, **overrides):
    """A pull page the client will accept, for tests about something else."""
    page = {
        "contract_version": CONTRACT_VERSION,
        "cursor": 40,
        "has_more": False,
        "sections": {},
        "omitted_sections": [],
    }
    registry.reply("/api/sync/pull", 200, {**page, **overrides})


def test_enrol_spends_the_code_and_returns_a_token(registry, make_code, code_secret) -> None:
    registry.reply(
        "/api/enrol",
        200,
        {"device_id": "d-1", "token": TOKEN, "enrolled_by": "sub-1", "device_name": "Reef 3"},
    )
    code = decode_connect_code(make_code(registry.base_url))

    enrolment = make_client(registry, token=None).enrol(
        code, "Field laptop 2", platform="linux", gui_version="0.3.3", library_version="1.1.0"
    )

    assert (enrolment.device_id, enrolment.token, enrolment.enrolled_by) == ("d-1", TOKEN, "sub-1")
    assert enrolment.device_name == "Reef 3"
    method, path, body, headers = registry.requests[-1]
    assert (method, path) == ("POST", "/api/enrol")
    assert body == {
        "code": code_secret,
        "device_name": "Field laptop 2",
        "platform": "linux",
        "gui_version": "0.3.3",
        "library_version": "1.1.0",
    }
    assert "Authorization" not in headers


def test_module_enrol_uses_the_address_inside_the_code(registry, make_code) -> None:
    """Expected behaviour: the app is never configured with a server address."""
    registry.reply("/api/enrol", 200, {"device_id": "d-1", "token": TOKEN, "enrolled_by": "sub-1"})

    enrolment = enrol(decode_connect_code(make_code(registry.base_url)), "Laptop")

    assert enrolment.token == TOKEN


def test_heartbeat_returns_the_answer_without_demanding_a_stamp(registry) -> None:
    """Scenario: a client that has already agreed a contract heartbeats a
    registry whose heartbeat body carries no `contract_version`.

    Expected behaviour: the answer comes back rather than being refused as
    unstamped, like the archive responses, so the assigned preset it names can
    be kept.
    """
    registry.reply(
        "/api/sync/heartbeat",
        200,
        {"assigned_preset": {"id": "p-1", "name": "Deep reef", "version": 2}},
    )
    client = make_client(registry, agreed=CONTRACT_VERSION)

    answer = client.heartbeat({"gui_version": "0.3.3"})

    assert answer["assigned_preset"]["name"] == "Deep reef"
    assert registry.requests[-1][2] == {"gui_version": "0.3.3"}


def test_performance_observations_use_the_independent_endpoint(registry) -> None:
    registry.reply(
        "/api/performance/observations",
        200,
        {"accepted": ["o-1"], "already_present": [], "rejected": []},
    )
    client = make_client(registry, agreed=CONTRACT_VERSION)
    rows = [{"id": "o-1", "observation": {}, "stage_peaks": {}, "source": "legacy"}]

    answer = client.upload_performance_observations(rows)

    assert answer["accepted"] == ["o-1"]
    method, path, body, _headers = registry.requests[-1]
    assert (method, path) == ("POST", "/api/performance/observations")
    assert body == {"observations": rows}


def test_pull_and_push_carry_the_bearer_token(registry) -> None:
    serve_pull(registry, sections={"sites": []})
    registry.reply(
        "/api/sync/push",
        200,
        {
            "contract_version": CONTRACT_VERSION,
            "cursor": 41,
            "sections": {"sites": {"received": 1, "applied": 1, "skipped": []}},
        },
    )
    client = make_client(registry)

    assert client.pull(since=10, limit=25)["cursor"] == 40
    assert client.push({"sites": [{"id": "s-1"}]})["cursor"] == 41

    assert all(headers["Authorization"] == f"Bearer {TOKEN}" for *_, headers in registry.requests)
    pull_path = next(path for method, path, _, _ in registry.requests if path.startswith("/api/sync/pull"))
    assert "since=10" in pull_path and "limit=25" in pull_path
    push_body = registry.requests[-1][2]
    assert push_body == {"contract_version": CONTRACT_VERSION, "sections": {"sites": [{"id": "s-1"}]}}


def test_push_declares_the_negotiated_version_not_this_builds_maximum(registry) -> None:
    """Scenario: this build speaks a newer contract than the registry does.

    Expected behaviour: the push document names the version the exchange settled
    on. The registry checks the document against `min` of the two maximums, so
    declaring this build's own maximum would make every push a 400 the moment
    the two sides differ. Asserted on the negotiation itself rather than through
    a fake registry, because only one contract version exists to serve today.
    """
    client = make_client(registry)

    client._learn_range(f"{client_mod.MIN_CONTRACT_VERSION}-{CONTRACT_VERSION + 3}")
    assert client._declared_version() == CONTRACT_VERSION, "never above what this build speaks"

    client._learn_range(f"{client_mod.MIN_CONTRACT_VERSION}-{CONTRACT_VERSION}")
    assert client._declared_version() == CONTRACT_VERSION

    client.server_max = CONTRACT_VERSION - 1
    assert client._declared_version() == CONTRACT_VERSION - 1, "follow an older registry down"


def test_push_follows_a_registry_that_has_been_upgraded(registry) -> None:
    """The range on the answer is read every time, so a registry that gained a
    version since the last sync is pushed to under the new one, not a stored
    agreement that is now stale."""
    serve_pull(registry, contract_version=CONTRACT_VERSION)
    registry.reply(
        "/api/sync/push", 200, {"contract_version": CONTRACT_VERSION, "sections": {}}
    )
    client = make_client(registry)
    client.agreed = client_mod.MIN_CONTRACT_VERSION

    client.pull()
    client.push({"sites": []})

    assert registry.requests[-1][2]["contract_version"] == CONTRACT_VERSION


def test_every_call_declares_the_contract_and_the_sections(registry) -> None:
    """Built in one place, so no route can be added that forgets one.

    The declared set is what a pull will land, not every section the contract names:
    the registry reads it to decide what to withhold, and naming upload-only sections
    would have it report them as withheld on every page.
    """
    serve_pull(registry)
    registry.reply("/api/sync/push", 200, {"contract_version": CONTRACT_VERSION, "sections": {}})
    client = make_client(registry)

    client.pull()
    client.push({"sites": []})

    for _method, _path, _body, headers in registry.requests:
        assert headers[CONTRACT_HEADER] == CONTRACT_RANGE
        assert headers[SECTIONS_HEADER] == ",".join(READ_SECTIONS)


def test_enrolment_declares_the_contract_and_no_sections(registry, make_code) -> None:
    """A section list narrows a pull. Minting a token has nothing to narrow."""
    registry.reply("/api/enrol", 200, {"device_id": "d-1", "token": TOKEN})

    make_client(registry, token=None).enrol(decode_connect_code(make_code(registry.base_url)), "Laptop")

    headers = registry.requests[-1][3]
    assert headers[CONTRACT_HEADER] == CONTRACT_RANGE
    assert SECTIONS_HEADER not in headers


def test_pull_limit_is_clamped_to_the_server_maximum(registry) -> None:
    serve_pull(registry)

    make_client(registry).pull(limit=client_mod.MAX_PULL_LIMIT * 2)

    assert f"limit={client_mod.MAX_PULL_LIMIT}" in registry.requests[-1][1]


def test_base_url_already_ending_in_api_is_not_doubled(registry) -> None:
    serve_pull(registry)

    make_client(registry, suffix="/api").pull()

    assert registry.requests[-1][1].startswith("/api/sync/pull")


@pytest.mark.parametrize(
    "status, expected, message",
    [
        (401, DeviceRevokedError, "revoked"),
        (403, AccessDeniedError, "refused"),
        (409, ConflictError, "rejected"),
        (400, RejectedError, "rejected"),
        (500, ServerFaultError, "own side"),
        (503, ServerFaultError, "own side"),
    ],
)
def test_http_status_maps_to_its_exception(registry, status, expected, message) -> None:
    registry.reply("/api/sync/pull", status, {"error": "server said so"})

    with pytest.raises(expected, match=message) as raised:
        make_client(registry).pull()
    assert "server said so" in str(raised.value)


def test_revoked_device_message_says_what_to_do(registry) -> None:
    registry.reply("/api/sync/pull", 401, {"error": "Device revoked"})

    with pytest.raises(DeviceRevokedError, match="new connect code"):
        make_client(registry).pull()


def test_rejected_connect_code_is_not_reported_as_a_revoked_device(registry, make_code) -> None:
    registry.reply("/api/enrol", 401, {"error": "Invalid or expired connect code"})

    with pytest.raises(EnrolmentRejectedError, match="fresh code"):
        make_client(registry, token=None).enrol(decode_connect_code(make_code(registry.base_url)), "Laptop")


def test_contract_mismatch_names_both_versions(registry) -> None:
    serve_pull(registry, contract_version=CONTRACT_VERSION + 1)

    with pytest.raises(ContractMismatchError) as raised:
        make_client(registry).pull()
    assert str(CONTRACT_VERSION) in str(raised.value)
    assert str(CONTRACT_VERSION + 1) in str(raised.value)


def test_the_first_stamp_is_adopted_as_the_agreed_version(registry) -> None:
    """There is no handshake call: the agreed version arrives on ordinary work."""
    serve_pull(registry)
    client = make_client(registry)

    client.pull()

    assert client.agreed == CONTRACT_VERSION


def test_an_unstamped_response_is_tolerated_before_anything_is_agreed(registry) -> None:
    """Contract 1 shipped before the registry stamped, so silence is not a fault yet."""
    registry.reply("/api/sync/pull", 200, {"cursor": 3, "has_more": False, "sections": {}})
    client = make_client(registry)

    assert client.pull()["cursor"] == 3
    assert client.agreed is None


def test_an_unstamped_response_is_refused_once_a_registry_has_stamped(registry) -> None:
    registry.reply("/api/sync/pull", 200, {"cursor": 3, "has_more": False, "sections": {}})

    with pytest.raises(ContractMismatchError) as raised:
        make_client(registry, agreed=CONTRACT_VERSION).pull()

    assert "did not say which it speaks" in str(raised.value)
    assert str(CONTRACT_VERSION) in str(raised.value)


def test_a_stamp_outside_this_builds_range_names_both(registry) -> None:
    serve_pull(registry, contract_version=CONTRACT_VERSION + 2)

    with pytest.raises(ContractMismatchError) as raised:
        make_client(registry, agreed=CONTRACT_VERSION).pull()

    assert str(CONTRACT_VERSION) in str(raised.value)
    assert str(CONTRACT_VERSION + 2) in str(raised.value)


def test_the_agreed_version_follows_the_registry_up_inside_our_range(registry, monkeypatch) -> None:
    """Scenario: this build speaks a range, and the registry's maximum rises to
    meet more of it, so the version the two agree on rises with it.

    Expected behaviour: the higher stamp is adopted, not refused. It is a version
    this build reads, and refusing it would leave Disconnect as the only way out
    of a legitimate registry upgrade.
    """
    later = CONTRACT_VERSION + 1
    # A build whose range reaches one past what the registry served last time.
    monkeypatch.setattr(client_mod, "CONTRACT_VERSION", later)
    serve_pull(registry, contract_version=later)
    client = make_client(registry, agreed=CONTRACT_VERSION)

    client.pull()

    assert client.agreed == later


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500])
def test_a_disjoint_range_in_the_header_raises_whatever_the_status(registry, status) -> None:
    """The header survives a proxy rewriting the body, so it is read first."""
    ahead = f"{CONTRACT_VERSION + 3}-{CONTRACT_VERSION + 4}"
    registry.reply(
        "/api/sync/pull", status, {"error": "server said so"}, {CONTRACT_HEADER: ahead}
    )

    with pytest.raises(ContractMismatchError) as raised:
        make_client(registry).pull()

    assert ahead in str(raised.value)
    assert CONTRACT_RANGE in str(raised.value)


def test_an_overlapping_range_in_the_header_leaves_the_status_to_speak(registry) -> None:
    registry.reply(
        "/api/sync/pull",
        403,
        {"error": "server said so"},
        {CONTRACT_HEADER: f"{client_mod.MIN_CONTRACT_VERSION}-{CONTRACT_VERSION + 9}"},
    )

    with pytest.raises(AccessDeniedError):
        make_client(registry).pull()


def test_archive_by_hash_answers_none_where_nothing_is_archived(registry) -> None:
    """A 404 here is an answer, not a fault: the content has never been sent."""
    missing = "a" * 32
    registry.reply(
        f"/api/archive/by-hash/{missing}", 404, {"error": "Nothing archived under this hash"}
    )

    assert make_client(registry).archive_by_hash(missing) is None


def test_archive_by_hash_returns_the_objects_state(registry) -> None:
    held = "b" * 32
    registry.reply(
        f"/api/archive/by-hash/{held}",
        200,
        {"object_id": "o-1", "status": "complete", "completed_at": "2026-08-01T00:00:00Z"},
    )

    answer = make_client(registry).archive_by_hash(held)

    assert answer is not None and answer["status"] == "complete"


def test_archive_initiate_and_complete_post_where_the_registry_listens(registry) -> None:
    registry.reply(
        "/api/archive/initiate",
        200,
        {"object_id": "o-1", "status": "pending", "part_size_bytes": 4, "parts_done": []},
    )
    registry.reply("/api/archive/o-1/complete", 200, {"object_id": "o-1", "status": "uploaded"})
    client = make_client(registry)

    initiated = client.archive_initiate({"sha256": "c" * 64, "size_bytes": 4, "kind": "video"})
    completed = client.archive_complete("o-1", [{"part_number": 1, "etag": "abc"}])

    assert (initiated["status"], completed["status"]) == ("pending", "uploaded")
    method, path, body, _ = registry.requests[-2]
    assert (method, path, body["kind"]) == ("POST", "/api/archive/initiate", "video")
    method, path, body, _ = registry.requests[-1]
    assert (method, path) == ("POST", "/api/archive/o-1/complete")
    assert body == {"parts": [{"part_number": 1, "etag": "abc"}]}


def test_archive_download_joins_a_relative_fetch_link_onto_the_registry(registry) -> None:
    """A registry without a configured public address answers a relative path,
    and the fetch route lives under `/api` like every other."""
    link = "/archive/o-1/fetch?expires=1&sig=x"
    registry.reply("/api/archive/o-1/download", 200, {"url": link})

    assert make_client(registry).archive_download("o-1") == f"{registry.base_url}/api{link}"
    assert make_client(registry, suffix="/api").archive_download("o-1") == (
        f"{registry.base_url}/api{link}"
    )


def test_archive_download_passes_an_absolute_url_through(registry) -> None:
    link = "https://reef.example/api/archive/o-1/fetch?expires=1&sig=x"
    registry.reply("/api/archive/o-1/download", 200, {"url": link})

    assert make_client(registry).archive_download("o-1") == link


def test_ping_asks_who_the_registry_thinks_this_device_is(registry) -> None:
    """Expected behaviour: authenticated, so the answer separates a server that
    is down from one that is up and no longer accepts this installation."""
    identity = {"is_device": True, "device_id": "d-1", "device_name": "Reef 3"}
    registry.reply("/api/me", 200, identity, {CONTRACT_HEADER: f"1-{CONTRACT_VERSION + 2}"})

    client = make_client(registry)
    assert client.ping() == identity

    method, path, _body, headers = registry.requests[-1]
    assert (method, path) == ("GET", "/api/me")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    # The range travels on every answer, so a probe keeps the client current.
    assert client.server_max == CONTRACT_VERSION + 2


def test_ping_reports_a_revoked_device(registry) -> None:
    registry.reply("/api/me", 401, {"detail": "unknown device"})

    with pytest.raises(DeviceRevokedError, match="revoked"):
        make_client(registry).ping()


def test_ping_reports_a_registry_that_is_not_listening() -> None:
    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        SyncClient("http://127.0.0.1:1", token=TOKEN, timeout=2.0).ping()


def test_offline_server_raises_unreachable() -> None:
    # Port 1 on loopback: nothing listens, so this is a connection refusal, not a lookup.
    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        SyncClient("http://127.0.0.1:1", token=TOKEN, timeout=2.0).pull()


def test_missing_token_is_reported_before_any_request(registry) -> None:
    with pytest.raises(DeviceRevokedError, match="not connected"):
        make_client(registry, token=None).pull()
    assert registry.requests == []


def test_token_never_appears_in_logs(registry, caplog) -> None:
    serve_pull(registry)

    with caplog.at_level(logging.DEBUG):
        make_client(registry).pull()

    assert TOKEN not in caplog.text
