"""Connecting and disconnecting, against a registry that never leaves the process."""

from __future__ import annotations

import pytest

from deepreefmap_gui.server.enrolment import connect, forget
from deepreefmap_gui.server.state import LAST_SYNC_KEY, SYNC_ERROR_KEY
from deepreefmap_gui.survey.store import SYNC_SECTIONS
from deepreefmap_gui.sync import client, credentials
from deepreefmap_gui.sync.connect_code import ConnectCodeError
from deepreefmap_gui.sync.engine import (
    CONTRACT_SECTIONS_KEY,
    CONTRACT_VERSION_KEY,
    CURSOR_KEY,
    WATERMARK_PREFIX,
)

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64


@pytest.fixture
def registry(monkeypatch):
    """Answer every enrolment, and remember which address was asked."""
    asked: dict[str, object] = {}

    def _request(self, method, path, body=None, authorise=True, declare_sections=True, timeout=None):
        asked["url"] = self.base_url
        asked["path"] = path
        asked["body"] = body
        return {
            "device_id": "device-1",
            "token": asked.get("token", TOKEN),
            "enrolled_by": "Kim Nguyen",
        }

    monkeypatch.setattr(client.SyncClient, "_request", _request)
    return asked


@pytest.fixture
def hostname(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "reef-laptop")
    return "reef-laptop"


def test_the_address_comes_out_of_the_code_and_nowhere_else(connect_code, registry):
    """Nothing in this repository knows a server address until a code is pasted."""
    connected = connect(connect_code(url="https://reef.example.org"))

    assert registry["url"] == "https://reef.example.org"
    assert registry["path"] == "/enrol"
    assert connected.base_url == "https://reef.example.org"
    assert connected.device_id == "device-1"


def test_a_spent_code_leaves_a_credential_behind(connect_code, registry):
    connected = connect(connect_code())

    held = credentials.load()
    assert held is not None
    assert held.token == TOKEN
    assert held.base_url == "https://reef.example.org"
    assert connected.base_url == "https://reef.example.org"


def test_the_stored_identity_is_the_registrys_own(connect_code, registry):
    """The registry keys the device row on the id it minted, so that is the id
    worth showing: an admin can match it to the web interface's device list."""
    connect(connect_code())

    held = credentials.load()
    assert held is not None
    assert held.device_id == "device-1"


def test_the_registry_is_told_what_this_device_is(connect_code, registry, hostname):
    connect(connect_code())

    body = registry["body"]
    assert body["device_name"] == hostname
    assert body["platform"]
    assert body["gui_version"]


def test_the_name_is_sent_once_and_never_again(connect_code, registry, monkeypatch):
    """Renaming is a web interface action: attribution is not the device's to edit."""
    sent: list[str] = []

    def record(self, method, path, body=None, authorise=True, declare_sections=True, timeout=None):
        if body and "device_name" in body:
            sent.append(path)
        return {"device_id": "device-1", "token": TOKEN}

    monkeypatch.setattr(client.SyncClient, "_request", record)
    connect(connect_code())
    credentials.load()

    assert sent == ["/enrol"]


def test_who_onboarded_the_device_is_reported_for_audit(connect_code, registry):
    connected = connect(connect_code())

    assert connected.enrolled_by == "Kim Nguyen"


def test_the_device_enrols_under_the_machines_own_name(connect_code, registry, hostname):
    """Nothing here collects a name: the device starts as the machine, and any
    renaming happens in the web interface."""
    connected = connect(connect_code())

    assert connected.device_name == hostname


def test_a_plain_http_code_is_refused_before_the_network(connect_code, registry):
    """The dialog's disabled button is UI: the library is where the secret would
    cross the wire, so the refusal has to live here too."""
    with pytest.raises(ConnectCodeError, match="unencrypted"):
        connect(connect_code(url="http://192.168.1.10:8080"))

    assert registry == {}
    assert credentials.load() is None


def test_a_loopback_code_enrols_over_plain_http(connect_code, registry):
    connected = connect(connect_code(url="http://localhost:8080"))

    assert connected.base_url == "http://localhost:8080"


def test_a_string_that_is_not_a_code_never_reaches_the_network(connect_code, registry):
    with pytest.raises(ConnectCodeError):
        connect("paste me")

    assert registry == {}
    assert credentials.load() is None


def test_an_enrolment_with_no_token_stores_nothing(connect_code, registry):
    registry["token"] = ""

    with pytest.raises(client.SyncError):
        connect(connect_code())

    assert credentials.load() is None


def test_disconnecting_forgets_the_position_as_well_as_the_token(connect_code, registry, store):
    """Scenario: a laptop is moved from one registry to another.

    Expected behaviour: the cursor goes with the token, and so does everything
    negotiated. A cursor is one registry's own sequence, so carrying it across
    would skip every row the next registry wrote below that number, and the next
    registry has said nothing about a contract yet.
    """
    connect(connect_code())
    store.set_sync_state(CURSOR_KEY, "4830")
    store.set_sync_state(LAST_SYNC_KEY, "2026-01-01T00:00:00Z")
    store.set_sync_state(SYNC_ERROR_KEY, "This device is no longer enrolled.")
    store.set_sync_state(CONTRACT_VERSION_KEY, "1")
    store.set_sync_state(CONTRACT_SECTIONS_KEY, "campaigns,sites")
    for section in SYNC_SECTIONS:
        store.set_sync_state(f"{WATERMARK_PREFIX}{section}", "2026-01-01T00:00:00Z")

    forget(store)

    assert credentials.load() is None
    assert store.sync_state(CURSOR_KEY) is None
    assert store.sync_state(LAST_SYNC_KEY) is None
    assert store.sync_state(SYNC_ERROR_KEY) is None
    assert store.sync_state(CONTRACT_VERSION_KEY) is None
    assert store.sync_state(CONTRACT_SECTIONS_KEY) is None
    assert all(store.sync_state(f"{WATERMARK_PREFIX}{s}") is None for s in SYNC_SECTIONS)


def test_the_device_id_survives_a_disconnection(connect_code, registry, store):
    """The registry's id for this installation outlives the token: a reconnection
    to the same registry is the same device, not a new one."""
    connect(connect_code())
    held = credentials.device_id()

    forget(store)

    assert credentials.device_id() == held == "device-1"
