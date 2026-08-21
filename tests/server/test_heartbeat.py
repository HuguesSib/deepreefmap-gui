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


@pytest.fixture
def probed_paths(monkeypatch):
    """Record the disk path the probe measures, without faking its answer."""
    from deepreefmap_gui.profiling import system_probe

    real = system_probe.probe_system
    paths: list[object] = []

    def spy(disk_path=None, *, wait_for_gpu=True):
        paths.append(disk_path)
        return real(disk_path, wait_for_gpu=wait_for_gpu)

    monkeypatch.setattr(system_probe, "probe_system", spy)
    return paths


def test_free_disk_is_measured_at_the_survey_output_root(store, probed_paths):
    client = FakeClient({"assigned_preset": None})

    _heartbeat(client, store)

    assert probed_paths == [store.path.parent]
    assert isinstance(client.reports[0]["system_profile"]["disk_free_bytes"], int)


def test_no_survey_store_measures_no_particular_disk(probed_paths):
    _heartbeat(FakeClient({}), None)

    assert probed_paths == [None]


def test_the_profile_carries_exactly_the_agreed_keys(store):
    """Free space travels. The path it was measured at, available RAM and free
    swap stay off the wire: an activity trace of one person's laptop."""
    client = FakeClient({"assigned_preset": None})

    _heartbeat(client, store)

    profile = client.reports[0]["system_profile"]
    assert set(profile) == {
        "os_name",
        "os_release",
        "cpu_logical",
        "cpu_physical",
        "total_ram_bytes",
        "total_swap_bytes",
        "disk_total_bytes",
        "disk_free_bytes",
        "gpu",
    }
    assert set(profile["gpu"]) == {"kind", "name", "total_vram_bytes"}
