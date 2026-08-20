import pytest
from _factories import make_transect

from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS
from deepreefmap_gui.simple.plan import DRAFT_ID
from deepreefmap_gui.survey.models import RunRecord, TransectPass, VideoAsset
from deepreefmap_gui.survey.models.exporters import save_transects_csv


def type_coord(window, which, text):
    """Type a coordinate the way a field worker pastes one off a GPS."""
    edit = window._tr_start_coord if which == "start" else window._tr_end_coord
    edit.setText(text)
    window._on_coords_edited()


def row_texts(window):
    """Every transect row as a tuple of columns. The list is flat: the scope
    chips above it name what it is showing, so there are no section headings."""
    tree = window._transect_list
    return [
        # The last column is a spacer that absorbs the leftover width.
        tuple(tree.topLevelItem(i).text(c) for c in range(tree.columnCount() - 1))
        for i in range(tree.topLevelItemCount())
    ]


def row_names(window):
    return [row[0] for row in row_texts(window)]


def select_row(window, index):
    """Click the row at ``index``, as the user would."""
    tree = window._transect_list
    tree.setCurrentItem(tree.topLevelItem(index))


def test_transect_autosaves_once_complete(window):
    w = window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5 177.1")
    assert w._survey_store().list_transects() == []
    type_coord(w, "end", "-17.5005, 177.1005")
    transects = w._survey_store().list_transects()
    assert len(transects) == 1
    assert transects[0].name == "T1"
    assert transects[0].start_lat == -17.5
    assert transects[0].end_lon == 177.1005
    w._tr_length.setValue(50.0)
    w._maybe_autosave()
    assert w._survey_store().list_transects()[0].length_m == 50.0
    rows = row_texts(w)
    assert len(rows) == 1
    # A typed tape length is the cable actually laid, so it stands in the row in
    # place of the straight-line distance between the GPS endpoints.
    assert rows[0][:2] == ("T1", "50 m tape")


def test_draft_row_tracks_typing_before_save(window):
    from PySide6.QtCore import Qt

    w = window
    w._refresh_transect_list()
    assert row_texts(w) == []
    w._tr_name_input.setText("Nor")
    assert row_names(w) == ["Nor"]
    item = w._transect_list.currentItem()
    assert item.data(0, Qt.ItemDataRole.UserRole) == "draft"
    assert item.text(0) == "Nor"
    w._tr_name_input.setText("North reef")
    assert row_names(w) == ["North reef"]
    assert w._survey_store().list_transects() == []


def test_out_of_range_coordinate_is_reported(window):
    """The range check that used to live on the Quick field still applies."""
    w = window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5, 177.1")
    type_coord(w, "end", "95.0, 177.1")
    assert w._survey_store().list_transects() == []
    with pytest.raises(ValueError, match="out of range"):
        w._form_coordinates()


def test_garbage_coordinate_never_saves(window):
    w = window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "junk")
    type_coord(w, "end", "-17.5, 177.1")
    assert w._survey_store().list_transects() == []


def test_new_transect_arrives_named_and_ready_to_draw(window):
    """Scenario: New, then the two clicks that place the line.

    Expected behaviour: nothing to name and nothing to arm in between.
    """
    w = window
    w._survey_store().add_transect(make_transect("Transect 1"))
    w._refresh_transect_list()
    w._on_transect_new()
    assert w._tr_name_input.text() == "Transect 2"
    assert w._pick_both_btn.isChecked()
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert [t.name for t in w._survey_store().list_transects()] == ["Transect 1", "Transect 2"]


def test_new_name_skips_numbers_already_taken(window):
    w = window
    for name in ("Transect 1", "Transect 3"):
        w._survey_store().add_transect(make_transect(name))
    w._on_transect_new()
    assert w._tr_name_input.text() == "Transect 2"


def test_copy_endpoint_puts_latlon_on_clipboard(window, monkeypatch):
    # Stubbed clipboard: the real X11 selection is shared with the desktop and
    # races with clipboard managers.
    captured = []

    class _Clipboard:
        def setText(self, text):
            captured.append(text)

    class _App:
        @staticmethod
        def clipboard():
            return _Clipboard()

    monkeypatch.setattr("deepreefmap_gui.simple.plan.QGuiApplication", _App)
    w = window
    w._tr_start_coord.setText("-17.500000, 177.100000")
    w._copy_endpoint("start")
    assert captured == ["-17.500000, 177.100000"]
    w._copy_endpoint("end")
    assert "No end point to copy" in w._status_label.text()


def test_map_click_without_armed_button_is_ignored(window):
    w = window
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._tr_start_coord.text() == ""
    assert w._tr_end_coord.text() == ""


def test_draft_line_appears_once_both_endpoints_set(window):
    w = window
    w._pick_both_btn.setChecked(True)
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert not any(t.id == "draft" for t in w._plan_map._transects)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert any(t.id == "draft" for t in w._plan_map._transects)
    w._tr_name_input.setText("T1")
    w._on_transect_save()
    assert not any(t.id == "draft" for t in w._plan_map._transects)
    assert len(w._plan_map._transects) == 1


def test_duplicate_name_reports_and_keeps_one(window):
    w = window
    w._survey_store().add_transect(make_transect())
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.6 177.2")
    type_coord(w, "end", "-17.6005 177.2005")
    w._on_transect_save()
    assert "already exists" in w._status_label.text()
    assert len(w._survey_store().list_transects()) == 1


def test_edit_selected_transect_updates_row(window):
    w = window
    w._survey_store().add_transect(make_transect())
    w._refresh_transect_list()
    select_row(w, 0)
    assert w._tr_name_input.text() == "T1"
    w._tr_end_coord.setText("-17.502, 177.1005")
    w._on_transect_save()
    stored = w._survey_store().list_transects()
    assert len(stored) == 1
    assert stored[0].end_lat == -17.502


def test_delete_with_passes_is_blocked(window):
    w = window
    store = w._survey_store()
    transect = make_transect()
    video = VideoAsset(file_name="a.mp4", path="/a.mp4", hash="cd" * 16)
    store.add_transect(transect)
    store.upsert_video(video)
    store.add_pass(
        TransectPass(transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=30.0)
    )
    w._refresh_transect_list()
    select_row(w, 0)
    w._on_transect_delete()
    assert "cannot be deleted" in w._status_label.text()
    assert len(store.list_transects()) == 1


def test_import_csv_skips_existing(window, tmp_path, monkeypatch):
    w = window
    existing = make_transect()
    w._survey_store().add_transect(existing)
    csv_path = tmp_path / "in.csv"
    save_transects_csv(csv_path, [existing, make_transect("T2")])
    monkeypatch.setattr(
        "deepreefmap_gui.simple.plan.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(csv_path), "")),
    )
    w._on_transects_import()
    assert "Imported 1" in w._status_label.text()
    assert "Skipped 1" in w._status_label.text()
    assert [t.name for t in w._survey_store().list_transects()] == ["T1", "T2"]


def test_export_csv_round_trip(window, tmp_path, monkeypatch):
    w = window
    w._survey_store().add_transect(make_transect())
    out_path = tmp_path / "out.csv"
    monkeypatch.setattr(
        "deepreefmap_gui.simple.plan.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(out_path), "")),
    )
    w._on_transects_export()
    assert "T1" in out_path.read_text()


def test_pick_both_walks_start_then_end(window):
    """Scenario: a new transect drawn entirely on the map.

    Expected behaviour: one button, two clicks, then it disarms itself.
    """
    w = window
    w._pick_both_btn.setChecked(True)
    assert w._plan_map._pick_mode
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._tr_start_coord.text() == "-17.500000, 177.100000"
    assert w._tr_end_coord.text() == ""
    assert "end" in w._status_label.text().lower()
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert w._tr_end_coord.text() == "-17.500500, 177.100500"
    assert not w._pick_both_btn.isChecked()
    assert not w._plan_map._pick_mode


def test_notes_round_trip_through_the_store(window):
    w = window
    w._tr_name_input.setText("T1")
    type_coord(w, "start", "-17.5, 177.1")
    type_coord(w, "end", "-17.5005, 177.1005")
    w._tr_description.setPlainText("tape run W→E\nviz ~12 m")
    w._maybe_autosave()
    stored = w._survey_store().list_transects()[0]
    assert stored.description == "tape run W→E\nviz ~12 m"
    w._on_transect_new()
    w._refresh_transect_list()
    select_row(w, 0)
    assert w._tr_description.toPlainText() == "tape run W→E\nviz ~12 m"


def test_selecting_a_transect_filters_the_browser(window):
    w = window
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._refresh_transect_list()
    select_row(w, 0)
    assert w._data_facet == "transects"
    assert w._data_selected_key == ("transect", str(transect.id))


def test_a_selected_transect_is_locked_against_dragging(window):
    """Expected behaviour: reading a transect off the map leaves its endpoints
    fixed; only pressing Edit puts them under the pointer."""
    w = window
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._refresh_transect_list()
    select_row(w, 0)

    assert w._plan_map._editable_id is None
    start_px = w._plan_map._px_of(transect.start_lat, transect.start_lon)
    assert w._plan_map._endpoint_at(start_px) is None

    w._transect_edit_btn.setChecked(True)
    assert w._plan_map._editable_id == str(transect.id)
    assert w._plan_map._endpoint_at(start_px) is not None


def test_leaving_edit_mode_saves_and_locks(window):
    w = window
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._refresh_transect_list()
    select_row(w, 0)
    w._transect_edit_btn.setChecked(True)
    assert w._transect_edit_btn.text() == "Save"

    type_coord(w, "end", "-17.4000, 177.2000")
    w._transect_edit_btn.setChecked(False)

    assert w._transect_edit_btn.text() == "Edit"
    assert w._plan_map._editable_id is None
    saved = w._survey_store().get_transect(transect.id)
    assert saved.end_lat == pytest.approx(-17.4)
    assert saved.end_lon == pytest.approx(177.2)


def test_a_new_transect_starts_editable(window):
    w = window
    w._on_transect_new()
    assert w._transect_editing
    assert w._pick_both_btn.isChecked()
    assert w._plan_map._editable_id == DRAFT_ID


def test_dragging_an_end_of_an_uncommitted_draft_moves_it(window):
    """A line drawn before it has a name stays a draft, and its ends still follow
    the pointer rather than being frozen until it is saved."""
    w = window
    w._on_transect_new()
    w._tr_name_input.clear()
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert w._transect_form_id is None
    assert w._plan_map._editable_id == DRAFT_ID

    w._plan_map.transect_endpoint_moved.emit(DRAFT_ID, "end", -17.6, 177.2)
    assert w._tr_end_coord.text() == "-17.600000, 177.200000"


def test_columns_count_the_passes_and_runs_on_each_transect(window):
    w = window
    store = w._survey_store()
    transect = make_transect()
    video = VideoAsset(file_name="a.mp4", path="/a.mp4", hash="cd" * 16)
    store.add_transect(transect)
    store.upsert_video(video)
    pass_ = TransectPass(transect_id=transect.id, video_id=video.id, begin_s=0.0, end_s=30.0)
    store.add_pass(pass_)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="run_001", status="succeeded"))
    w._refresh_transect_list()
    assert row_texts(w)[0][3:] == ("1", "1")


def two_transects(w):
    w._plan_map.resize(400, 300)
    w._survey_store().add_transect(make_transect("Near"))
    w._survey_store().add_transect(
        make_transect("Far", start_lat=10.0, start_lon=20.0, end_lat=10.001, end_lon=20.001)
    )
    w._refresh_transect_list()


def test_in_view_narrows_the_list_to_what_the_map_shows(window):
    """Scenario: two transects far apart, the map on one of them.

    Expected behaviour: In view leaves the one on screen, All transects brings
    the other back. One list either way -- the two used to be separate sections
    and every on-screen transect appeared in both.
    """
    w = window
    two_transects(w)
    w._plan_map.set_view(-17.5, 177.1, 16)
    w._apply_plan_view_change()
    w._plan_scope_chips.set_current("in_view")
    assert row_names(w) == ["Near"]

    w._plan_scope_chips.set_current("all")
    assert sorted(row_names(w)) == ["Far", "Near"]


def test_the_list_shows_everything_at_rest(window):
    """The map arrives fitted to the whole survey, so nothing is filtered yet."""
    w = window
    two_transects(w)
    assert sorted(row_names(w)) == ["Far", "Near"]


def test_panning_away_empties_the_in_view_list(window):
    w = window
    two_transects(w)
    w._plan_map.set_view(-17.5, 177.1, 16)
    w._apply_plan_view_change()
    w._plan_scope_chips.set_current("in_view")
    assert row_names(w) == ["Near"]

    w._plan_map.set_view(40.0, -70.0, 16)
    w._apply_plan_view_change()
    assert row_names(w) == []


def test_the_transect_being_edited_survives_the_scope(window):
    """Panning off the transect in the form must not delete the row it edits."""
    w = window
    two_transects(w)
    near = next(t for t in w._survey_store().list_transects() if t.name == "Near")
    w._plan_scope_chips.set_current("in_view")
    w._refresh_transect_list(select_id=near.id)
    w._plan_map.set_view(40.0, -70.0, 16)
    w._refresh_transect_list(select_id=near.id)
    assert row_names(w) == ["Near"]


def test_the_scope_counts_follow_the_map_while_showing_all(window):
    """Switching to In view has to be an informed choice, so the count moves."""
    w = window
    two_transects(w)
    w._plan_map.set_view(-17.5, 177.1, 16)
    w._apply_plan_view_change()
    assert w._plan_scope_chips._buttons["in_view"].text().endswith("1")
    assert w._plan_scope_chips._buttons["all"].text().endswith("2")


def test_the_scope_is_offered_only_where_it_filters_something(window):
    w = window
    w._plan_map.resize(400, 300)
    w._survey_store().add_transect(make_transect("Near"))
    w._refresh_transect_list()
    chips = w._plan_scope_chips
    assert not chips.isVisibleTo(chips.parentWidget())

    w._survey_store().add_transect(
        make_transect("Far", start_lat=10.0, start_lon=20.0, end_lat=10.001, end_lon=20.001)
    )
    w._refresh_transect_list()
    assert chips.isVisibleTo(chips.parentWidget())


def test_selecting_a_transect_moves_the_map_to_it(window):
    w = window
    w._plan_map.resize(400, 300)
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._plan_map.set_view(40.0, -70.0, 5)
    w._refresh_transect_list()
    select_row(w, 0)
    lat, lon = w._plan_map._center
    assert lat == pytest.approx(transect.start_lat, abs=0.01)
    assert lon == pytest.approx(transect.start_lon, abs=0.01)
    # Short of filling the viewport, so the reef either side stays on screen.
    p1 = w._plan_map._px_of(transect.start_lat, transect.start_lon)
    p2 = w._plan_map._px_of(transect.end_lat, transect.end_lon)
    span = max(abs(p1.x() - p2.x()), abs(p1.y() - p2.y()))
    assert 0 < span <= 0.6 * min(w._plan_map.width(), w._plan_map.height())


def test_typing_a_coordinate_redraws_without_saving_it(window):
    w = window
    transect = make_transect()
    w._survey_store().add_transect(transect)
    w._refresh_transect_list()
    select_row(w, 0)
    w._tr_end_coord.setText("-17.4, 177.3")
    overlay = next(t for t in w._plan_map._transects if t.id == str(transect.id))
    assert overlay.end == pytest.approx((-17.4, 177.3))
    assert w._survey_store().get_transect(transect.id).end_lat == transect.end_lat


def test_geometry_readout_states_length_and_heading(window):
    w = window
    assert "once both ends are set" in w._tr_geometry.text()
    type_coord(w, "start", "0.0, 0.0")
    type_coord(w, "end", "0.0, 0.01")
    text = w._tr_geometry.text()
    assert "1112 m" in text
    assert "090° E" in text


def test_copy_action_appears_only_once_there_is_a_coordinate(window):
    w = window
    assert not w._coord_copy_actions["start"].isVisible()
    w._tr_start_coord.setText("-17.5, 177.1")
    assert w._coord_copy_actions["start"].isVisible()
    w._tr_start_coord.clear()
    assert not w._coord_copy_actions["start"].isVisible()


def test_draw_tool_narrates_the_click_it_wants(window):
    w = window
    assert w._pick_both_btn.text() == "Draw"
    w._pick_both_btn.setChecked(True)
    assert w._pick_both_btn.text() == "Click start"
    w._plan_map.map_clicked.emit(-17.5, 177.1)
    assert w._pick_both_btn.text() == "Click end"
    assert w._plan_map._pending_start == (-17.5, 177.1)
    w._plan_map.map_clicked.emit(-17.5005, 177.1005)
    assert w._pick_both_btn.text() == "Draw"
    assert w._plan_map._pending_start is None


def test_copy_confirms_at_the_field_and_in_the_status_bar(window, monkeypatch):
    captured = []
    shown = []

    class _Clipboard:
        def setText(self, text):
            captured.append(text)

    class _App:
        @staticmethod
        def clipboard():
            return _Clipboard()

    monkeypatch.setattr("deepreefmap_gui.simple.plan.QGuiApplication", _App)
    monkeypatch.setattr(
        "deepreefmap_gui.simple.plan.QToolTip.showText",
        staticmethod(lambda *args: shown.append(args[1])),
    )
    w = window
    w._tr_start_coord.setText("-17.500000, 177.100000")
    w._copy_endpoint("start")
    assert captured == ["-17.500000, 177.100000"]
    assert shown == ["Copied to clipboard"]
    assert "clipboard" in w._status_label.text()


def test_unset_length_and_depth_read_as_nothing_recorded(window):
    w = window
    assert w._tr_length.text() == "—"
    assert w._tr_depth.text() == "—"
    w._tr_depth.setValue(8.5)
    assert w._tr_depth.text() == "8.5 m"


def test_deleting_down_to_one_transect_releases_the_scope(window):
    """A filter still applying from a control nobody can see is a row missing
    for no stated reason."""
    w = window
    two_transects(w)
    w._plan_map.set_view(-17.5, 177.1, 16)
    w._apply_plan_view_change()
    w._plan_scope_chips.set_current("in_view")
    assert row_names(w) == ["Near"]

    far = next(t for t in w._survey_store().list_transects() if t.name == "Far")
    w._survey_store().delete_transect(far.id)
    w._refresh_transect_list()
    assert w._plan_scope_filter == "all"
    assert row_names(w) == ["Near"]


def test_a_transect_is_read_whole_on_one_page(window):
    """Scenario: a transect's map, its form and what its passes found used to be
    built twice, on two pages in two destinations, so nothing showed the line
    whole and the two drawings could disagree.

    Expected behaviour: Transects owns all of it, and Browse's By transect
    grouping is a filter over the archive with a way through to here.
    """
    assert window._plan_map is not None
    assert window._tr_name_input is not None
    assert window._analysis_chart is not None
    assert window._analysis_stats_table is not None
    # The chart is built once, so the widget Browse used to host is this one.
    assert window._analysis_chart.window() is window


@pytest.mark.parametrize("width", [1400, 1280, 1024, 800])
def test_a_long_transect_name_never_pushes_the_figures_off_the_table(window, qapp, width):
    """Scenario: a transect with a long name, at four window widths.

    Expected behaviour: Depth, Passes and Runs stay readable. Sizing every column
    to its content let the name take the viewport and cut the figures off the
    right edge with the horizontal scrollbar turned off, so there was no way to
    reach them.
    """
    from deepreefmap_gui.simple.plan import PLAN_COLUMNS

    window._survey_store().add_transect(
        make_transect("Vatu-i-Ra North Wall repeat 2024-03 deep")
    )
    window.resize(width, 800)
    window.show()
    window._go_to_section("transects")
    window._refresh_transect_list()
    qapp.processEvents()
    qapp.processEvents()

    tree = window._transect_list
    for name in ("Depth", "Passes", "Runs"):
        column = PLAN_COLUMNS.index(name)
        assert not tree.isColumnHidden(column), f"{name} hidden at {width}px"
        assert tree.columnWidth(column) > 0, f"{name} has no width at {width}px"
    # The name gives way instead, down to the width it is still an identifier at.
    assert tree.columnWidth(0) >= 140


def test_length_is_the_column_that_drops_on_a_narrow_pane(window, qapp):
    """Its value is in the row tooltip either way, and Depth was asked for."""
    from deepreefmap_gui.simple.plan import PLAN_COLUMNS

    window._survey_store().add_transect(make_transect("Reef"))
    window.resize(800, 800)
    window.show()
    window._go_to_section("transects")
    window._refresh_transect_list()
    qapp.processEvents()
    qapp.processEvents()

    tree = window._transect_list
    assert tree.isColumnHidden(PLAN_COLUMNS.index("Length"))
    assert not tree.isColumnHidden(PLAN_COLUMNS.index("Depth"))


@pytest.mark.parametrize("size", [(1400, 900), (1366, 768), (1280, 800)])
def test_the_analysis_column_is_on_screen_without_resizing_anything(window, qapp, size):
    """Scenario: the Transects page is opened at a normal window size.

    Expected behaviour: the cover chart, the estimate table and the export
    buttons are all on screen. A splitter asked for more height than the page has
    is scaled down, so the analysis pane landed under its own minimum and the
    table below the fold until the window was resized by hand.
    """
    window.resize(*size)
    window.show()
    qapp.processEvents()
    window._go_to_section("transects")
    qapp.processEvents()
    qapp.processEvents()

    assert window._plan_analysis_scroll.verticalScrollBar().maximum() == 0


def test_a_dragged_plan_handle_survives_the_window_being_resized(window, qapp):
    window.resize(1400, 900)
    window.show()
    window._go_to_section("transects")
    qapp.processEvents()
    window._plan_split.setSizes([500, 700])
    window._on_plan_split_moved()

    window.resize(1200, 800)
    qapp.processEvents()
    qapp.processEvents()

    assert window._plan_split.sizes()[1] > window._plan_split.sizes()[0]


def test_browse_routes_a_transect_through_to_transects(out_root, window):
    """The grouping's detail pane offers the transect rather than redrawing it."""
    from _factories import make_transect, seed_survey_run

    transect = make_transect("Reef North")
    seed_survey_run(window._survey_store(), out_root, "run_a", transect=transect)
    window._refresh_data_manager()
    window._data_facet_buttons["transects"].click()

    tree = window._data_tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.text(0).startswith("Reef North"):
            tree.setCurrentItem(item)
            break
    else:
        raise AssertionError("Reef North is not listed")

    assert window._data_detail_stack.currentWidget() is window._transect_detail
    window._transect_detail.open_btn.click()
    assert window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("transects")


def test_transect_list_sorts_lengths_as_numbers(window):
    """A length sort puts "9 m" before "12 m", which text order gets backwards."""
    from PySide6.QtCore import Qt

    w = window
    store = w._survey_store()
    store.add_transect(make_transect(name="Short", length_m=9.0))
    store.add_transect(make_transect(name="Long", length_m=12.0))
    w._refresh_transect_list()

    tree = w._transect_list
    assert tree.header().property("sortable") == "true"
    assert tree.header().isSortIndicatorShown()

    tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert row_names(w) == ["Short", "Long"]
    tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert row_names(w) == ["Long", "Short"]


def test_draft_row_stays_at_the_foot_under_a_sort(window):
    from PySide6.QtCore import Qt

    w = window
    w._survey_store().add_transect(make_transect(name="Zeta"))
    w._refresh_transect_list()
    w._tr_name_input.setText("Alpha draft")

    w._transect_list.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert row_names(w) == ["Zeta", "Alpha draft"]


def test_a_transect_names_its_site_and_the_name_is_unique_per_site(window):
    """Two reefs can each have a T1; one reef cannot have two."""
    from deepreefmap_gui.survey.models import Site

    w = window
    store = w._survey_store()
    garden, wall = Site(name="Japanese Garden"), Site(name="North Wall")
    store.add_site(garden)
    store.add_site(wall)
    w._refresh_site_choices()

    w._tr_name_input.setText("T1")
    w._tr_site_combo.setCurrentIndex(w._tr_site_combo.findData(str(garden.id)))
    type_coord(w, "start", "-17.5 177.1")
    type_coord(w, "end", "-17.5005, 177.1005")
    assert store.list_transects()[0].site_id == garden.id

    w._on_transect_new()
    w._tr_name_input.setText("T1")
    w._tr_site_combo.setCurrentIndex(w._tr_site_combo.findData(str(wall.id)))
    type_coord(w, "start", "-17.6 177.2")
    type_coord(w, "end", "-17.6005, 177.2005")

    saved = store.list_transects()
    assert len(saved) == 2
    assert {t.site_id for t in saved} == {garden.id, wall.id}


def test_selecting_a_transect_shows_its_site(window):
    from deepreefmap_gui.survey.models import Site

    w = window
    store = w._survey_store()
    site = Site(name="Japanese Garden")
    store.add_site(site)
    store.add_transect(make_transect("T1", site_id=site.id))
    w._refresh_transect_list()

    select_row(w, 0)

    assert w._tr_site_combo.currentData() == str(site.id)
