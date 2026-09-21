"""The probe: what each outcome is called, and what it says.

The registry is a fake standing in for `SyncClient`, so nothing here reaches a
network and a test asserting a failure cannot pass offline for the wrong reason.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.server import reachability
from deepreefmap_gui.sync import client as client_mod
from deepreefmap_gui.sync import credentials

SERVER_URL = "https://reef.example.org"
TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64


@pytest.fixture
def enrolled():
    credentials.save(SERVER_URL, TOKEN)


@pytest.fixture
def answering(monkeypatch):
    """Programme what the identity route does, and record how it was asked."""
    asked: list[tuple[str, str | None, float | None]] = []

    def build(raise_with=None):
        class FakeClient:
            def __init__(self, base_url, token=None, timeout=None, agreed=None):
                self.base_url = base_url
                self.token = token
                self.timeout = timeout

            def ping(self):
                asked.append((self.base_url, self.token, self.timeout))
                if raise_with is not None:
                    raise raise_with
                return {"is_device": True, "device_id": "d-1", "device_name": "Reef 3"}

        monkeypatch.setattr(client_mod, "SyncClient", FakeClient)
        return asked

    return build


def test_the_probe_carries_this_device_token(enrolled, answering):
    """Expected behaviour: the question is whether the registry still has this
    installation, which an unauthenticated route answers the same either way."""
    asked = answering()

    reading = reachability.probe()

    assert reading.state == reachability.ONLINE
    assert asked == [(SERVER_URL, TOKEN, client_mod.PING_TIMEOUT)]
    assert reading.latency_ms is not None
    assert "knows this device" in reading.detail
    assert reading.checked_at


def test_nothing_answering_is_unavailable(enrolled, answering):
    answering(raise_with=client_mod.ServerUnreachableError(f"Cannot reach {SERVER_URL}: no route."))

    reading = reachability.probe()

    assert reading.state == reachability.OFFLINE
    assert reading.unavailable
    assert not reading.answered
    assert "Cannot reach" in reading.detail
    assert reading.latency_ms is None


def test_a_revoked_device_is_refused_not_unavailable(enrolled, answering):
    """Scenario: the device was revoked in the console since the last sync.

    Expected behaviour: the registry is up, so this is not a network to wait
    out. It reads as a refusal, which needs a fresh connect code.
    """
    answering(raise_with=client_mod.DeviceRevokedError("This device's access has been revoked."))

    reading = reachability.probe()

    assert reading.state == reachability.DENIED
    assert reading.denied
    assert reading.answered
    assert not reading.unavailable
    assert "would not have this device" in reading.detail
    assert "revoked" in reading.detail


def test_a_forbidden_device_is_refused_too(enrolled, answering):
    answering(raise_with=client_mod.AccessDeniedError("The registry refused this request: not permitted"))

    assert reachability.probe().state == reachability.DENIED


def test_a_registry_speaking_another_contract_is_answering_but_unwell(enrolled, answering):
    answering(raise_with=client_mod.ContractMismatchError("This app speaks metadata contract 1-3."))

    reading = reachability.probe()

    assert reading.state == reachability.DEGRADED
    assert not reading.denied
    assert "cannot speak" in reading.detail


def test_a_registry_with_no_identity_route_is_still_up(enrolled, answering):
    """Scenario: a registry older than this route answers the probe with 404.

    Expected behaviour: something served that, so the address is not dead, and
    the badge must not tell a diver the server is unavailable.
    """
    answering(raise_with=client_mod.NotFoundError("The registry has no such record: no route /me"))

    reading = reachability.probe()

    assert reading.state == reachability.DEGRADED
    assert reading.answered
    assert not reading.unavailable
    assert "no identity route" in reading.detail


def test_a_registry_failing_on_its_own_side_is_answering_but_unwell(enrolled, answering):
    """A 500 came out of the registry's own code, so it is up and broken."""
    answering(raise_with=client_mod.ServerFaultError("The registry failed on its own side (500): boom", 500))

    reading = reachability.probe()

    assert reading.state == reachability.DEGRADED
    assert not reading.unavailable
    assert "500" in reading.detail


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_with_nothing_behind_it_is_the_server_being_down(enrolled, answering, status):
    """Scenario: the registry is stopped, and the proxy in front of it answers.

    Expected behaviour: the address is served but the registry is not running,
    which is the server being off. Reading it as "unwell" would leave the badge
    saying a sync is at fault over a server somebody has simply turned off.
    """
    answering(raise_with=client_mod.ServerFaultError(f"The registry failed on its own side ({status}): x", status))

    reading = reachability.probe()

    assert reading.state == reachability.OFFLINE
    assert reading.unavailable
    assert "nothing serving behind it" in reading.detail


def test_a_laptop_with_no_credential_claims_nothing(answering):
    asked = answering()

    reading = reachability.probe()

    assert reading.state == reachability.UNKNOWN
    assert not reading.asked
    assert reading.detail == reachability.NOT_ENROLLED
    assert asked == []


def test_an_answer_ages_out_and_is_asked_again():
    unchecked = reachability.UNCHECKED
    assert unchecked.stale(now=0.0)
    assert not unchecked.asked

    fresh = reachability.Reachability(state=reachability.ONLINE, _at=100.0)
    assert not fresh.stale(now=100.0 + reachability.PROBE_INTERVAL_S - 1)
    assert fresh.stale(now=100.0 + reachability.PROBE_INTERVAL_S)
