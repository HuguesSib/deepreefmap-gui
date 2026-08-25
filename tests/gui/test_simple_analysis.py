import json

import pytest
from _factories import seed_pass

from deepreefmap_gui.map.overlays import transect_overlays
from deepreefmap_gui.survey.models import RunRecord


@pytest.fixture
def analysis_window(window, out_root):
    store = window._survey_store()
    _transect, _video, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01", status="succeeded")
    store.add_run(run)
    cls = window._classes_config.classes[0]
    run_dir = out_root / "t1__p01"
    run_dir.mkdir()
    (run_dir / "benthic_cover.json").write_text(json.dumps({
        "classes": {str(cls.id): {"name": cls.name, "count": 30.0, "fraction": 0.3}},
        "denominator": 100.0,
    }))
    window._refresh_survey_analysis()
    return window


def test_analysis_populates_chart_and_table(analysis_window):
    """The runs behind these numbers are the list beside the pane, not a second copy."""
    w = analysis_window
    assert w._analysis_transect_combo.count() == 1
    assert len(w._analysis_covers) == 1
    assert w._analysis_stats_table.rowCount() >= 1
    assert w._analysis_stats_table.item(0, 1).text() == "30.0%"
    assert not hasattr(w, "_analysis_runs_list")


def test_analysis_stats_table_declares_its_sort(analysis_window):
    table = analysis_window._analysis_stats_table
    header = table.horizontalHeader()
    assert table.isSortingEnabled()
    assert header.property("sortable") == "true"
    assert header.isSortIndicatorShown()


def test_analysis_export_csv(analysis_window, tmp_path, monkeypatch):
    out_path = tmp_path / "repeat.csv"
    monkeypatch.setattr(
        "deepreefmap_gui.simple.analysis.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(out_path), "")),
    )
    analysis_window._on_analysis_export_csv()
    content = out_path.read_text()
    assert "mean_fraction" in content
    assert "t1__p01" in content


def test_analysis_level_switch_recomputes(analysis_window):
    w = analysis_window
    w._analysis_level_combo.setCurrentText("coarse")
    assert len(w._analysis_covers) == 1
    assert pytest.approx(sum(w._analysis_covers[0].cover.values())) == 0.3


def test_analysis_map_labels_and_counts_each_transect(analysis_window):
    """Transects are identifiable on the map, with their survey effort on hover."""
    window = analysis_window
    window._refresh_survey_analysis()
    overlays = transect_overlays(window._survey_store(), None)
    assert overlays
    overlay = overlays[0]
    assert overlay.label == "T1"
    assert "T1" in overlay.tooltip
    assert "1 video" in overlay.tooltip
    assert "1 pass" in overlay.tooltip
    assert "succeeded" in overlay.tooltip


def test_unprocessed_transect_says_so(analysis_window):
    from deepreefmap_gui.survey.models import Transect

    window = analysis_window
    window._survey_store().add_transect(
        Transect(
            name="Untouched",
            start_lat=-17.6,
            start_lon=177.2,
            end_lat=-17.6005,
            end_lon=177.2005,
        )
    )
    window._refresh_survey_analysis()
    overlays = transect_overlays(window._survey_store(), None)
    tooltips = {o.label: o.tooltip for o in overlays}
    assert "Not processed yet" in tooltips["Untouched"]


# --- pooled by default, per pass on request ---------------------------------

def test_the_chart_shows_the_estimate_before_it_shows_the_passes(analysis_window):
    """Scenario: a transect with processed passes is opened.

    Expected behaviour: one bar per class carrying the pooled estimate, not one
    bar per pass. Several series side by side said nothing about which to trust.
    """
    analysis_window._on_analysis_mode_changed("pooled")
    chart = analysis_window._analysis_chart

    assert chart._aggregate
    assert len(chart._bars()) == len(chart._labels)
    # And the sentence beside it no longer claims the bars are per-pass.
    assert "each pass" not in analysis_window._analysis_estimate_label.text()


def test_the_toggle_puts_every_pass_back_on_its_own(analysis_window, out_root):
    w = analysis_window
    w._analysis_mode_chips.set_current("passes")
    w._on_analysis_mode_changed("passes")

    chart = w._analysis_chart
    assert not chart._aggregate
    assert len(chart._series) == len(w._analysis_covers)
    assert chart._keys == [str(c.pass_id) for c in w._analysis_covers]
    assert "each pass" in w._analysis_estimate_label.text()


def test_the_mode_is_remembered_for_next_time(analysis_window):
    """A reader's preference, so it belongs to the machine rather than the survey."""
    analysis_window._on_analysis_mode_changed("passes")

    assert analysis_window._settings.value("analysis_chart_mode") == "passes"


def test_one_pass_is_told_it_has_no_spread(analysis_window):
    """A zero-length whisker reads as perfect agreement between passes there is
    only one of."""
    analysis_window._on_analysis_mode_changed("pooled")
    assert len(analysis_window._analysis_covers) == 1

    assert analysis_window._analysis_chart._spread == {}
    assert "no spread" in analysis_window._analysis_estimate_label.text()


def test_clicking_a_pass_opens_where_its_section_is_described(analysis_window, monkeypatch):
    w = analysis_window
    w._on_analysis_mode_changed("passes")
    opened = []
    monkeypatch.setattr(w, "_open_section_in_videos", opened.append)

    w._analysis_chart.series_clicked.emit(str(w._analysis_covers[0].pass_id))

    assert opened == [w._analysis_covers[0].pass_id]


def test_a_pooled_bar_opens_nothing_because_it_is_every_pass(analysis_window, monkeypatch):
    w = analysis_window
    w._on_analysis_mode_changed("pooled")
    opened = []
    monkeypatch.setattr(w, "_open_section_in_videos", opened.append)

    w._analysis_chart.series_clicked.emit(str(w._analysis_covers[0].pass_id))

    assert opened == []


def test_a_retired_line_keeps_its_place_in_analysis(analysis_window):
    """Scenario: the registry retires the line whose passes are already
    processed and shown here.

    Expected behaviour: it stays in the picker, marked retired, and its numbers
    stay readable. Listing only what somebody may choose emptied the pane of
    work that had already been done, with nothing on screen saying why.
    """
    from datetime import datetime, timedelta, timezone

    window = analysis_window
    store = window._survey_store()
    transect = store.list_transects()[0]
    retired_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    store.apply_from_server("transects", [{
        "id": str(transect.id),
        "deleted_at": retired_at,
        "updated_at": retired_at,
    }])

    window._refresh_survey_analysis()

    assert window._analysis_transect_combo.count() == 1
    assert window._analysis_transect_combo.itemText(0) == "T1 (retired)"
    assert len(window._analysis_covers) == 1
