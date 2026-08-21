"""The pre-sync heartbeat: what it reports, and what it keeps of the answer."""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.server.page_ui import _heartbeat
from deepreefmap_gui.survey.preset import ASSIGNED_PRESET_KEY
from deepreefmap_gui.survey.preset_schema import PRESET_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _isolate_user_data_preset(tmp_path, monkeypatch):
    """Keep the active-preset resolution off the developer's real data dir."""
    monkeypatch.delenv("DEEPREEFMAP_SURVEY_PRESET", raising=False)
    monkeypatch.setattr(
        "deepreefmap_gui.survey.preset.survey_preset_path",
        lambda: tmp_path / "no-user-preset" / "survey_preset.yaml",
    )


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.reports: list[dict] = []

    def heartbeat(self, report):
        self.reports.append(dict(report))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def stored_assignment(store):
    raw = store.sync_state(ASSIGNED_PRESET_KEY)
    return json.loads(raw) if raw else None


def test_the_report_names_the_schema_and_the_active_preset(store):
    client = FakeClient({"assigned_preset": None})

    _heartbeat(client, store)

    report = client.reports[0]
    assert report["preset_schema_version"] == PRESET_SCHEMA_VERSION
    assert report["active_preset_name"] == "Standard reef survey"
    assert report["active_preset_version"] == 1


def test_an_assignment_in_the_answer_is_kept(store):
    client = FakeClient(
        {"assigned_preset": {"id": "p-1", "name": "Deep reef", "version": 2}}
    )

    _heartbeat(client, store)

    assert stored_assignment(store) == {"name": "Deep reef", "version": 2}


def test_a_null_assignment_clears_the_stored_one(store):
    store.set_sync_state(ASSIGNED_PRESET_KEY, json.dumps({"name": "Old", "version": 1}))
    client = FakeClient({"assigned_preset": None})

    _heartbeat(client, store)

    assert stored_assignment(store) is None


def test_an_old_registrys_empty_answer_withdraws_nothing(store):
    """A 204 with no body says nothing about assignments, so the stored one
    must not be read as revoked."""
    store.set_sync_state(ASSIGNED_PRESET_KEY, json.dumps({"name": "Old", "version": 1}))
    client = FakeClient({})

    _heartbeat(client, store)

    assert stored_assignment(store) == {"name": "Old", "version": 1}


def test_a_failed_heartbeat_never_raises_and_changes_nothing(store):
    store.set_sync_state(ASSIGNED_PRESET_KEY, json.dumps({"name": "Old", "version": 1}))

    _heartbeat(FakeClient(RuntimeError("registry down")), store)
    _heartbeat(None, store)

    assert stored_assignment(store) == {"name": "Old", "version": 1}


def test_no_survey_store_still_sends_the_report():
    client = FakeClient({"assigned_preset": {"id": "p-1", "name": "Deep", "version": 1}})

    _heartbeat(client, None)

    assert client.reports and client.reports[0]["preset_schema_version"] == PRESET_SCHEMA_VERSION
