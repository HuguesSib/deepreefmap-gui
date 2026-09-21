import copy
import json

from deepreefmap_gui.profiling.comparison import (
    comparable,
    distribution,
    history_observations,
    observation,
    record_observation,
    summarize,
)


def sample(frames=100, seconds=100, ram=10):
    manifest = {
        "mode": "semantic",
        "processing_width": 1000,
        "processing_height": 500,
        "fps": 5,
        "preprocess_batch_size": 4,
        "mapping_backend": "mapper",
        "segmentation_model": "segmenter",
        "frames_processed": frames,
        "run_duration_s": seconds,
        "stage_peaks": {"mapping": {"ram_bytes": ram, "swap_bytes": 0}},
        "system_profile": {"total_ram_bytes": 100, "gpu": {"name": "test"}},
        "run_timestamp": "2026-09-21T12:00:00Z",
    }
    manifest["performance_observation"] = observation(manifest, completed=True)
    return manifest


def test_summarize_different_workloads_and_per_run_rates(tmp_path):
    path = tmp_path / "timings.json"
    for manifest in (sample(), sample(200, 400, 30)):
        record_observation(manifest, path)
    rows = history_observations(path)
    groups = summarize(rows)
    assert len(groups) == 1
    assert groups[0]["stats"]["ram"] == {"n": 2, "min": 10, "q1": 15, "median": 20, "q3": 25, "max": 30}
    assert groups[0]["stats"]["seconds_per_frame"]["median"] == 1.5
    assert groups[0]["stats"]["swap"]["median"] == 0
    assert groups[0]["stats"]["vram"]["n"] == 0
    assert summarize(rows, 150)[0]["count"] == 1
    assert not summarize(rows, 201)


def test_comparable_settings_and_hardware(tmp_path):
    path = tmp_path / "timings.json"
    record_observation(sample(), path)
    baseline = history_observations(path)[0]
    candidate = copy.deepcopy(baseline)
    candidate["settings"]["fps"] = 10
    assert comparable(baseline, candidate, "fps")
    assert not comparable(baseline, candidate, "resolution")
    candidate["hardware"]["total_ram_bytes"] = 200
    assert not comparable(baseline, candidate, "fps")
    candidate = copy.deepcopy(baseline)
    candidate["basis"] = "machine"
    assert len(summarize([baseline, candidate])) == 2
    assert not comparable(baseline, candidate, "fps")


def test_failed_cached_and_legacy_observations(tmp_path):
    path = tmp_path / "timings.json"
    failed = sample()
    failed["performance_observation"] = observation(failed, completed=False)
    cached = sample()
    cached["resumed_stages"] = ["mapping"]
    cached["performance_observation"] = observation(cached, completed=True)
    for manifest in (failed, cached):
        record_observation(manifest, path)
    group = summarize(history_observations(path))[0]
    assert group["failed"] == 1
    assert group["stats"]["ram"]["n"] == 1
    assert group["stats"]["seconds_per_frame"]["n"] == 0
    assert {row["timing_note"] for row in group["runs"]} == {"Incomplete run", "Cached or partial execution"}
    path.write_text(
        json.dumps(
            {"legacy": [{"version": 1, "frames": 100, "params": {}, "stage_peaks": {"mapping": {"ram_bytes": 50}}}]}
        )
    )
    legacy = next(row for row in history_observations(path) if not row["known"])
    assert legacy["basis"] == "machine"
    assert not comparable(legacy, legacy, "fps")


def test_distribution_single_missing_and_zero():
    assert distribution([None, -1, float("nan")])["n"] == 0
    assert distribution([0])["median"] == 0
    assert distribution([10])["q1"] == 10


def test_performance_widget_consolidates_and_expands(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton, QTableWidget

    from deepreefmap_gui.system.performance_comparison import PerformanceComparison

    path = tmp_path / "timings.json"
    record_observation(sample(), path)
    record_observation(sample(200, 400, 30), path)
    monkeypatch.setattr(
        "deepreefmap_gui.system.performance_comparison.history_observations", lambda: history_observations(path)
    )
    widget = PerformanceComparison()
    widget.refresh()
    widget.show()
    qapp.processEvents()
    assert widget.configuration.count() == 1
    table = widget.findChild(QTableWidget)
    assert table.rowCount() == 2
    assert not table.isVisible()
    widget.findChild(QPushButton).click()
    assert table.isVisible()
    widget.minimum.setValue(150)
    assert widget.findChild(QTableWidget).rowCount() == 1
    widget.close()


def test_incomplete_metadata_has_no_comparable_configuration(tmp_path):
    path = tmp_path / "timings.json"
    manifest = sample()
    manifest["performance_observation"]["settings"]["preprocess_batch_size"] = None
    record_observation(manifest, path)
    row = history_observations(path)[0]
    assert not row["known"]
    assert not comparable(row, row, "fps")
