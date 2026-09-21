"""Field workflow states, assignment and layout regressions."""

import os
from pathlib import Path

import pytest
from _factories import make_transect
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractScrollArea

from deepreefmap_gui.core.fonts import apply_app_fonts
from deepreefmap_gui.core.theme import apply_theme
from deepreefmap_gui.survey.jobs import job_filters, pass_state
from deepreefmap_gui.survey.models import Campaign, RunRecord, Site, TransectPass, VideoAsset


def seed_work(window):
    store = window._survey_store()
    site = Site(name="Japanese Garden")
    store.add_site(site)
    campaign = Campaign(name="September survey")
    store.add_campaign(campaign)
    transect = make_transect("T1", site_id=site.id)
    store.add_transect(transect)
    video = store.upsert_video(VideoAsset(file_name="GX010002.MP4", path="/field/GX010002.MP4", duration_s=298))
    passes = []
    for begin, end, status in ((22, 58, "succeeded"), (82, 114, "failed")):
        pass_ = TransectPass(transect_id=transect.id, video_id=video.id, begin_s=begin, end_s=end)
        store.add_pass(pass_)
        store.add_run(RunRecord(pass_id=pass_.id, run_dir_name=f"run_{begin}", status=status))
        passes.append(pass_)
    window._refresh_video_library()
    return video, passes, campaign


def test_job_state_uses_latest_attempt_and_actual_queue(window):
    video, passes, _ = seed_work(window)
    store = window._survey_store()
    store.add_run(RunRecord(pass_id=passes[1].id, run_dir_name="retry", status="succeeded", created_at="2099-01-01"))
    window._refresh_video_library()
    entry = next(entry for entry in window._video_entries if entry.video.id == video.id)
    assert "complete" in job_filters(entry)
    entry.queued_pass_ids.add(str(passes[0].id))
    assert "complete" not in job_filters(entry)
    assert "process" in job_filters(entry)
    assert pass_state([]) == "ready"
    assert pass_state([], queued=True) == "queued"


def test_campaign_selector_writes_and_clears_assignment(window):
    _, passes, campaign = seed_work(window)
    window._select_section(str(passes[0].id))
    panel = window._section_detail
    panel.campaign_combo.setCurrentIndex(panel.campaign_combo.findData(str(campaign.id)))
    panel.campaign_combo.activated.emit(panel.campaign_combo.currentIndex())
    assert window._survey_store().get_pass(passes[0].id).campaign_id == campaign.id
    panel.campaign_combo.setCurrentIndex(0)
    panel.campaign_combo.activated.emit(0)
    assert window._survey_store().get_pass(passes[0].id).campaign_id is None
    assert "Japanese Garden" in panel.site_label.text()


def test_campaign_selector_respects_locked_pass(window, monkeypatch):
    _, passes, campaign = seed_work(window)
    monkeypatch.setattr(window, "_refuse_locked", lambda _pass: True)
    window._set_pass_campaign(str(passes[0].id), str(campaign.id))
    assert window._survey_store().get_pass(passes[0].id).campaign_id is None


def test_metadata_filter_retains_sibling_passes_and_filters_results(window):
    video, passes, campaign = seed_work(window)
    window._set_pass_campaign(str(passes[0].id), str(campaign.id))
    combo = window._video_campaign_filter
    combo.setCurrentIndex(combo.findData(str(campaign.id)))
    assert set(window._video_list.rows()) == {str(video.id)}
    assert set(window._video_list.sections()) == {str(pass_.id) for pass_ in passes}
    window._refresh_data_manager()
    result_filter = window._data_campaign_filter
    result_filter.setCurrentIndex(result_filter.findData(str(campaign.id)))
    assert [entry.dir_name for entry in window._data_listed_entries()] == ["run_22"]
    result_filter.setCurrentIndex(result_filter.findData("unassigned"))
    assert [entry.dir_name for entry in window._data_listed_entries()] == ["run_82"]


def test_latest_failure_remains_attention_with_older_success(window):
    video, passes, _ = seed_work(window)
    window._survey_store().add_run(RunRecord(
        pass_id=passes[0].id, run_dir_name="failed_retry", status="failed", created_at="2099-01-01"
    ))
    window._refresh_video_library()
    entry = next(entry for entry in window._video_entries if entry.video.id == video.id)
    assert "attention" in job_filters(entry)
    assert "complete" not in job_filters(entry)
    assert len(entry.runs) == 3


def test_results_columns_and_optional_map_remain_accessible(window, qapp):
    from deepreefmap_gui.runs.run_table import COL_VIDEO

    seed_work(window)
    window._refresh_data_manager()
    window._set_simple_section("browse")
    window.resize(1400, 930)
    window.show()
    qapp.processEvents()
    table = window._data_run_table
    assert table.isColumnHidden(COL_VIDEO)
    window._data_technical_action.setChecked(True)
    assert not table.isColumnHidden(COL_VIDEO)
    window._data_map_toggle.setChecked(True)
    assert window._data_facet == "transects"
    assert not window._data_map.isHidden()
    window._data_map_toggle.setChecked(False)
    assert window._data_map.isHidden()
    assert not window._data_scope_applies()


def test_pass_overflow_reaches_rename_handler(window, monkeypatch):
    from deepreefmap_gui.runs import pass_rename

    _, passes, _ = seed_work(window)
    seen = []
    monkeypatch.setattr(pass_rename, "rename_pass", lambda parent, store, pass_id: seen.append(pass_id))
    row = window._video_list.sections()[str(passes[0].id)]
    menu = row.menu()
    action = next(action for action in menu.actions() if action.text() == pass_rename.RENAME_ACTION)
    action.trigger()
    assert seen == [passes[0].id]


def test_filtering_out_selected_pass_clears_inspector(window):
    _, passes, _ = seed_work(window)
    window._select_section(str(passes[0].id))
    window._video_chips.set_current("organise")
    assert window._selected_pass_id is None
    assert window._video_stack.currentIndex() == 2
    assert window._video_detail_stack.currentIndex() == 0


@pytest.mark.parametrize("width,height,points", [(1280, 800, 11), (1400, 930, 11), (1280, 800, 13)])
def test_field_layout_keeps_timelines_and_actions_visible(make_window, qapp, width, height, points):
    palette, font, stylesheet = QPalette(qapp.palette()), QFont(qapp.font()), qapp.styleSheet()
    try:
        apply_app_fonts(qapp)
        larger = QFont(qapp.font())
        larger.setPointSize(points)
        qapp.setFont(larger)
        apply_theme(qapp)
        window = make_window()
        video, passes, _ = seed_work(window)
        window._set_simple_section("videos")
        window._select_section(str(passes[1].id))
        window.resize(width, height)
        window.show()
        qapp.processEvents()
        qapp.processEvents()
        row = window._video_list.rows()[str(video.id)]
        assert len(row.strip.spans) == 2
        assert row.strip.width() >= 100
        assert row.more_btn.geometry().right() < row.width()
        assert window.width() == width
        assert window._video_inspector.currentWidget() is window._section_detail
        row.strip.setFocus()
        QTest.keyClick(row.strip, Qt.Key.Key_Left)
        assert window._selected_pass_id == str(passes[0].id)
        qapp.processEvents()
        section = window._video_list.sections()[str(passes[0].id)]
        assert section.strip.mapTo(window, section.strip.rect().topLeft()).x() == row.strip.mapTo(
            window, row.strip.rect().topLeft()
        ).x()
        assert section.strip.width() == row.strip.width()
        status = window._section_detail.status
        assert status.mapTo(window, status.rect().topRight()).x() < window.width()
        output = os.environ.get("DEEPREEFMAP_LAYOUT_CAPTURE")
        if output:
            window.grab().save(str(Path(output) / f"videos-{width}-{points}.png"))
            window._on_video_activated(str(video.id))
            qapp.processEvents()
            window.grab().save(str(Path(output) / f"clip-{width}-{points}.png"))
            window._set_simple_section("browse")
            qapp.processEvents()
            window.grab().save(str(Path(output) / f"results-{width}-{points}.png"))
        for destination in ("transects", "process", "browse"):
            window._set_simple_section(destination)
            qapp.processEvents()
            qapp.processEvents()
            if output:
                window.grab().save(str(Path(output) / f"{destination}-{width}-{points}.png"))
            assert window.width() == width, destination
            for area in window.findChildren(QAbstractScrollArea):
                bar = area.horizontalScrollBar()
                if area.isVisible() and bar.isVisible():
                    assert bar.maximum() == 0, (destination, type(area).__name__, area.objectName(), area.width())
    finally:
        qapp.setStyleSheet(stylesheet)
        qapp.setPalette(palette)
        qapp.setFont(font)
