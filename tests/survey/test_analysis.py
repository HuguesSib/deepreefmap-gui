import json
from datetime import datetime, timedelta, timezone

import pytest
from _factories import VIDEO_HASH, make_transect, make_video
from deepreefmap.config.classes import load_classes

from deepreefmap_gui.survey.analysis import (
    aggregated_cover_chart,
    assemble_transect_covers,
    collate_long_format,
    cover_labels,
    latest_run_per_pass,
    pooled_transect_cover,
    repeatability_stats,
    reproducibility_groups,
)
from deepreefmap_gui.survey.models import RunRecord, TransectPass


@pytest.fixture(scope="module")
def classes_config():
    return load_classes()


def seed_transect(store):
    transect = make_transect()
    store.add_transect(transect)
    return transect


def seed_run(store, out_root, transect, run_dir_name, fractions, *,
             content_hash=VIDEO_HASH, begin_s=0.0, end_s=60.0, status="succeeded",
             classes_config=None):
    """One pass + run whose run dir holds a benthic_cover.json with the given
    fine-class fractions (mapped onto the first N configured classes)."""
    video = store.upsert_video(make_video(content_hash))
    pass_ = TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=begin_s, end_s=end_s
    )
    store.add_pass(pass_)
    run = RunRecord(pass_id=pass_.id, run_dir_name=run_dir_name, status=status)
    store.add_run(run)
    run_dir = out_root / run_dir_name
    run_dir.mkdir(parents=True)
    classes = {}
    # strict=False on purpose: `fractions` covers only the leading classes.
    for cls, fraction in zip(classes_config.classes, fractions, strict=False):
        classes[str(cls.id)] = {"name": cls.name, "count": fraction * 100, "fraction": fraction}
    (run_dir / "benthic_cover.json").write_text(
        json.dumps({"classes": classes, "denominator": 100.0})
    )
    return pass_, run


def test_assemble_reads_succeeded_runs_only(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3, 0.5], classes_config=classes_config)
    seed_run(store, out_root, transect, "run_b", [0.4, 0.4], status="failed",
             classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    assert len(covers) == 1
    assert covers[0].run_dir_name == "run_a"
    assert covers[0].video_hash == VIDEO_HASH
    assert pytest.approx(sum(covers[0].cover.values())) == 0.8


def test_missing_cover_file_is_skipped(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3], classes_config=classes_config)
    (out_root / "run_a" / "benthic_cover.json").unlink()
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config)
    assert covers == []


def test_repeatability_stats_math(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3], classes_config=classes_config)
    seed_run(store, out_root, transect, "run_b", [0.5], classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    label = classes_config.classes[0].name
    stats = repeatability_stats(covers)[label]
    assert stats["mean"] == pytest.approx(0.4)
    assert stats["std"] == pytest.approx(0.1414, abs=1e-3)
    assert stats["range"] == pytest.approx(0.2)
    # Pinned, not re-derived from the returned std: cv = std / mean is the
    # formula under test, so computing it here would assert nothing.
    assert stats["cv"] == pytest.approx(0.353553, abs=1e-6)
    # The endpoints and the count, so a chart can draw the spread rather than
    # only state its width.
    assert (stats["min"], stats["max"], stats["n"]) == pytest.approx((0.3, 0.5, 2.0))


def test_one_pass_has_no_spread_rather_than_a_spread_of_zero(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "only", [0.3], classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    label = classes_config.classes[0].name

    stats = repeatability_stats(covers)[label]
    assert stats["n"] == 1.0
    assert stats["min"] == stats["max"] == pytest.approx(0.3)
    assert stats["std"] == 0.0

    # And the chart is told there is none, so it draws no whisker: a zero-length
    # one reads as perfect agreement between passes there are none of.
    aggregate = aggregated_cover_chart(covers)
    assert aggregate.n == 1
    assert aggregate.spread == {}


def test_the_aggregate_pools_the_estimate_and_brackets_it_with_the_passes(
    store, tmp_path, classes_config
):
    """Scenario: two passes of one transect disagree about a class.

    Expected behaviour: one bar at the count-weighted pool, whiskered by the
    lowest and highest single pass. The two are different estimators, so the
    whisker has to bracket the bar rather than equal it.
    """
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3], classes_config=classes_config)
    seed_run(store, out_root, transect, "run_b", [0.5], classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    label = classes_config.classes[0].name

    aggregate = aggregated_cover_chart(covers)

    assert aggregate.n == 2
    assert label in aggregate.labels
    low, high = aggregate.spread[label]
    assert (low, high) == pytest.approx((0.3, 0.5))
    assert low <= aggregate.values[label] <= high


def test_the_aggregate_drops_the_classes_the_chart_calls_noise(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3], classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")

    everything = aggregated_cover_chart(covers, minimum_fraction=0.0)
    trimmed = aggregated_cover_chart(covers, minimum_fraction=0.9)

    assert everything.labels
    assert trimmed.labels == []


def test_reproducibility_groups_by_hash_and_trim(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.3], classes_config=classes_config)
    seed_run(store, out_root, transect, "run_b", [0.32], classes_config=classes_config)
    seed_run(store, out_root, transect, "run_c", [0.5], begin_s=60.0, end_s=120.0,
             classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    groups = reproducibility_groups(covers)
    assert len(groups) == 1
    assert {c.run_dir_name for c in groups[0]} == {"run_a", "run_b"}


def test_cover_labels_orders_by_mean_and_filters(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run(store, out_root, transect, "run_a", [0.1, 0.6, 0.001],
             classes_config=classes_config)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    labels = cover_labels(covers, minimum_fraction=0.005)
    names = [cls.name for cls in classes_config.classes[:3]]
    assert labels[0] == names[1]
    assert names[2] not in labels


def _write_cover(run_dir, classes_config, counts, denom):
    run_dir.mkdir(parents=True, exist_ok=True)
    classes = {}
    # strict=False: `counts` covers only the leading classes.
    for cls, count in zip(classes_config.classes, counts, strict=False):
        classes[str(cls.id)] = {
            "name": cls.name,
            "count": float(count),
            "fraction": (count / denom if denom else 0.0),
        }
    (run_dir / "benthic_cover.json").write_text(
        json.dumps({"classes": classes, "denominator": float(denom)})
    )


def seed_run_counts(store, out_root, transect, run_dir_name, counts, denom, *,
                    classes_config, pass_=None, begin_s=0.0, end_s=60.0,
                    created_at=None, status="succeeded", content_hash=VIDEO_HASH):
    """A run with explicit per-class counts and denominator, optionally a rerun
    of an existing pass (pass the pass_ back in)."""
    if pass_ is None:
        video = store.upsert_video(make_video(content_hash))
        pass_ = TransectPass(
            transect_id=transect.id, video_id=video.id, begin_s=begin_s, end_s=end_s
        )
        store.add_pass(pass_)
    kwargs = {"pass_id": pass_.id, "run_dir_name": run_dir_name, "status": status}
    if created_at is not None:
        kwargs["created_at"] = created_at
    run = RunRecord(**kwargs)
    store.add_run(run)
    _write_cover(out_root / run_dir_name, classes_config, counts, denom)
    return pass_, run


def test_pooled_cover_is_count_weighted_not_mean_of_fractions(store, tmp_path, classes_config):
    """A short pass must not swing the estimate the way an average of ratios does."""
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    # Pass A saw a lot and is 90% class0. Pass B saw little and is 1% class0.
    seed_run_counts(store, out_root, transect, "run_a", [90.0], 100.0,
                    classes_config=classes_config, begin_s=0.0, end_s=60.0)
    seed_run_counts(store, out_root, transect, "run_b", [10.0], 1000.0,
                    classes_config=classes_config, begin_s=60.0, end_s=120.0)
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    pooled = pooled_transect_cover(covers, expected_passes=2)
    label = classes_config.classes[0].name

    assert pooled.cover[label] == pytest.approx(100.0 / 1100.0)
    assert pooled.denominator == pytest.approx(1100.0)
    assert pooled.counts[label] == pytest.approx(100.0)
    assert pooled.contributing_passes == 2
    assert pooled.expected_passes == 2
    # The discredited unweighted mean would have read ~0.455, not ~0.091.
    mean_of_ratios = (0.9 + 0.01) / 2
    assert pooled.cover[label] != pytest.approx(mean_of_ratios)


def test_assemble_dedupes_to_latest_run_per_pass(store, tmp_path, classes_config):
    """A rerun of one pass replaces its predecessor, it does not add a data point."""
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    pass_, _old = seed_run_counts(
        store, out_root, transect, "run_old", [90.0], 100.0,
        classes_config=classes_config, created_at="2026-07-01T10:00:00+00:00",
    )
    seed_run_counts(
        store, out_root, transect, "run_new", [10.0], 100.0,
        classes_config=classes_config, pass_=pass_,
        created_at="2026-07-01T11:00:00+00:00",
    )
    deduped = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    assert [c.run_dir_name for c in deduped] == ["run_new"]

    every = assemble_transect_covers(
        store, out_root, transect.id, classes_config, level="fine", dedupe=False
    )
    assert {c.run_dir_name for c in every} == {"run_old", "run_new"}

    label = classes_config.classes[0].name
    pooled = pooled_transect_cover(deduped, expected_passes=1)
    # Only the latest run counts: 10/100, not the pooled (90+10)/200 of both.
    assert pooled.cover[label] == pytest.approx(0.1)
    assert pooled.contributing_passes == 1


def test_reproducibility_needs_the_undeduped_reruns(store, tmp_path, classes_config):
    """Reproducibility is the spread between reruns, exactly what dedupe strips."""
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    pass_, _first = seed_run_counts(
        store, out_root, transect, "run_1", [30.0], 100.0,
        classes_config=classes_config, created_at="2026-07-01T10:00:00+00:00",
    )
    seed_run_counts(
        store, out_root, transect, "run_2", [32.0], 100.0,
        classes_config=classes_config, pass_=pass_,
        created_at="2026-07-01T11:00:00+00:00",
    )
    every = assemble_transect_covers(
        store, out_root, transect.id, classes_config, level="fine", dedupe=False
    )
    groups = reproducibility_groups(every)
    assert len(groups) == 1
    assert {c.run_dir_name for c in groups[0]} == {"run_1", "run_2"}

    deduped = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    assert reproducibility_groups(deduped) == []


def test_latest_run_per_pass_keeps_pass_order(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run_counts(store, out_root, transect, "p1", [10.0], 100.0,
                    classes_config=classes_config, begin_s=0.0, end_s=60.0)
    seed_run_counts(store, out_root, transect, "p2", [20.0], 100.0,
                    classes_config=classes_config, begin_s=60.0, end_s=120.0)
    every = assemble_transect_covers(
        store, out_root, transect.id, classes_config, level="fine", dedupe=False
    )
    assert [c.run_dir_name for c in latest_run_per_pass(every)] == ["p1", "p2"]


def test_pooled_reports_partial_pass_coverage(store, tmp_path, classes_config):
    """Two passes defined, one processed: the estimate says 1 of 2."""
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run_counts(store, out_root, transect, "run_a", [40.0], 100.0,
                    classes_config=classes_config, begin_s=0.0, end_s=60.0)
    # A second pass with no succeeded run at all.
    video = store.upsert_video(make_video())
    store.add_pass(TransectPass(
        transect_id=transect.id, video_id=video.id, begin_s=60.0, end_s=120.0
    ))
    covers = assemble_transect_covers(store, out_root, transect.id, classes_config, level="fine")
    expected = len(store.list_passes(transect_id=transect.id))
    pooled = pooled_transect_cover(covers, expected_passes=expected)
    assert (pooled.contributing_passes, pooled.expected_passes) == (1, 2)


def test_collate_long_format_rows_and_provenance(store, tmp_path, classes_config):
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run_counts(store, out_root, transect, "run_a", [90.0, 10.0], 100.0,
                    classes_config=classes_config, begin_s=0.0, end_s=60.0)
    seed_run_counts(store, out_root, transect, "run_b", [10.0], 1000.0,
                    classes_config=classes_config, begin_s=60.0, end_s=120.0)
    rows = collate_long_format(
        store, out_root, classes_config, levels=("fine",)
    )
    per_pass = [r for r in rows if r.estimator == "per_pass"]
    pooled = [r for r in rows if r.estimator == "pooled"]
    assert per_pass and pooled

    label0 = classes_config.classes[0].name
    a_row = next(r for r in per_pass if r.run_dir_name == "run_a" and r.group == label0)
    assert a_row.count == pytest.approx(90.0)
    assert a_row.denominator == pytest.approx(100.0)
    assert a_row.fraction == pytest.approx(0.9)
    assert a_row.expected_passes == 2

    pooled0 = next(r for r in pooled if r.group == label0)
    assert pooled0.fraction == pytest.approx(100.0 / 1100.0)
    assert pooled0.pass_id == ""
    assert pooled0.begin_s is None

    # Provenance rides every row so a number can be traced to what produced it.
    assert a_row.gui_version
    assert a_row.taxonomy_version == 1
    assert len(a_row.taxonomy_hash) == 64


def test_a_retired_line_keeps_its_results_in_the_long_format(store, tmp_path, classes_config):
    """Scenario: the registry retires a line after the passes on it were
    processed, which is what a curator tidying an old site looks like here.

    Expected behaviour: its rows are still collated. This is what the export
    writes and what the registry is sent as cover, so reading the picker's list
    had a retirement quietly delete finished science from both.
    """
    transect = seed_transect(store)
    out_root = tmp_path / "out"
    seed_run_counts(store, out_root, transect, "run_a", [90.0, 10.0], 100.0,
                    classes_config=classes_config)
    retired_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    store.apply_from_server("transects", [{
        "id": str(transect.id),
        "deleted_at": retired_at,
        "updated_at": retired_at,
    }])

    rows = collate_long_format(store, out_root, classes_config, levels=("fine",))

    assert store.list_transects() == []
    assert {row.transect_id for row in rows} == {str(transect.id)}
    assert [row for row in rows if row.estimator == "per_pass"]
