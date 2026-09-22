import json
from types import SimpleNamespace

from deepreefmap_gui.profiling import performance_journal as journal
from deepreefmap_gui.server.page_ui import _sync_performance_history


def configure_journal(tmp_path, monkeypatch):
    path = tmp_path / "performance.sqlite3"
    monkeypatch.setenv("DEEPREEFMAP_PERFORMANCE_JOURNAL", str(path))
    return path


def legacy_entry(ram, frames=100):
    return {
        "version": 1,
        "frames": frames,
        "params": {"fps": 5, "mapping_backend": "map", "segmentation_model": "seg"},
        "stage_peaks": {"mapping": {"ram_bytes": ram, "swap_bytes": 0}},
        "system_profile": {"total_ram_bytes": 100},
    }


def test_legacy_import_keeps_repeated_runs_and_is_restart_safe(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    timings = tmp_path / "run_timings.json"
    timings.write_text(json.dumps({"configuration": [legacy_entry(10), legacy_entry(10)]}))

    journal.import_legacy(timings)
    first = journal.observations()
    journal.import_legacy(timings)
    assert len(first) == 2
    assert journal.observations() == first
    assert len({row["id"] for row in first}) == 2
    assert all(row["source"] == "legacy" for row in first)


def test_legacy_source_is_imported_once_when_capped_history_changes(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    timings = tmp_path / "run_timings.json"
    first_entry = legacy_entry(10)
    second_entry = legacy_entry(20)
    timings.write_text(json.dumps({"configuration": [first_entry, second_entry]}))
    journal.import_legacy(timings)
    initial = journal.observations()

    timings.write_text(json.dumps({"configuration": [second_entry, legacy_entry(30)]}))
    journal.import_legacy(timings)

    assert journal.observations() == initial


def test_acknowledgements_are_scoped_to_server_and_device(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    payload = {
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "run_id": None,
        "observation": {"recorded_at": None},
        "stage_peaks": {},
        "source": "legacy",
    }
    journal.store(payload)
    assert journal.pending("https://one.example/api", "device-a") == [payload]
    journal.acknowledge("https://one.example/api", "device-a", [payload["id"]])
    assert not journal.pending("https://one.example/api", "device-a")
    assert journal.pending("https://one.example/api", "device-b") == [payload]
    assert journal.pending("https://two.example/api", "device-a") == [payload]


def test_store_preserves_a_run_link_when_legacy_import_lacks_it(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    payload = {
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "run_id": "bbbbbbbb-2222-4222-8222-222222222222",
        "observation": {"recorded_at": None},
        "stage_peaks": {},
        "source": "device",
    }
    journal.store(payload)

    journal.store({**payload, "run_id": None})

    assert journal.observations() == [payload]


def test_sync_uploads_once_and_acknowledges_retries(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    timings = tmp_path / "run_timings.json"
    timings.write_text(json.dumps({"configuration": [legacy_entry(10)]}))
    monkeypatch.setattr("deepreefmap_gui.paths.run_timings_path", lambda: timings)
    monkeypatch.setattr(
        "deepreefmap_gui.sync.credentials.load",
        lambda: SimpleNamespace(base_url="https://registry.example/api", device_id="device-a"),
    )

    class Client:
        def __init__(self):
            self.calls = []

        def upload_performance_observations(self, rows):
            self.calls.append(rows)
            return {"accepted": [row["id"] for row in rows], "already_present": [], "rejected": []}

    client = Client()
    capability = {"performance_observations_version": 1}
    assert _sync_performance_history(client, capability) == 0
    assert _sync_performance_history(client, capability) == 0
    assert len(client.calls) == 1


def test_old_registry_leaves_history_pending(tmp_path, monkeypatch):
    configure_journal(tmp_path, monkeypatch)
    timings = tmp_path / "run_timings.json"
    timings.write_text(json.dumps({"configuration": [legacy_entry(10)]}))
    monkeypatch.setattr("deepreefmap_gui.paths.run_timings_path", lambda: timings)
    monkeypatch.setattr(
        "deepreefmap_gui.sync.credentials.load",
        lambda: SimpleNamespace(base_url="https://old.example/api", device_id="device-a"),
    )

    assert _sync_performance_history(object(), {}) == 1
    assert len(journal.pending("https://old.example/api", "device-a")) == 1
