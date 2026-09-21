"""Semantic legend, cover sunburst and viewer-overlay controls."""

from __future__ import annotations


def test_overlay_has_reset_button_and_r_shortcut_triggers_view_reset(window) -> None:
    from PySide6.QtGui import QKeySequence, QShortcut

    assert window._reset_view_button is not None
    assert "Reset" in window._reset_view_button.text()

    calls: list[int] = []
    window._viewer.reset_view = lambda: calls.append(1)  # type: ignore[method-assign]

    window._reset_view_button.click()
    assert calls == [1]

    # QShortcut registered for R must exist and, when activated, fire the
    # same reset_view path. We trigger it via `activated.emit()` rather than
    # synthesising a key event, because offscreen windows aren't "active", which
    # would suppress the natural shortcut dispatch.
    r_seq = QKeySequence("R")
    r_shortcuts = [
        s for s in window.findChildren(QShortcut)
        if s.key() == r_seq
    ]
    assert r_shortcuts, "expected an R shortcut on the window"
    r_shortcuts[0].activated.emit()
    assert len(calls) == 2


def _populated_legend(rows: int = 3):
    """A legend on a bare parent, the shape the reorder path is exercised in."""
    from PySide6.QtWidgets import QWidget

    from deepreefmap_gui.viewer.legend import LegendOverlay

    parent = QWidget()
    parent.resize(1200, 800)
    ov = LegendOverlay(parent)
    ids = list(range(1, rows + 1))
    names = {cid: f"class {cid}" for cid in ids}
    colors = {cid: (cid, cid, cid) for cid in ids}
    ov.rebuild(ids, names, colors, on_toggle=lambda: None, class_counts=dict.fromkeys(ids, 10))
    return parent, ov


def test_legend_overlay_reorder_places_rows(qapp) -> None:
    _parent, ov = _populated_legend()
    ov.reorder([3, 1, 2])

    def row_of(cid: int) -> int:
        cb = ov._rows[cid][1]
        return ov._grid.getItemPosition(ov._grid.indexOf(cb))[0]

    assert row_of(3) < row_of(1) < row_of(2)


def test_the_legend_keeps_to_a_corner_of_the_canvas(qapp) -> None:
    """It floats over the cloud, so what it covers is cloud the user came for."""
    parent, ov = _populated_legend(rows=30)
    ov.show()
    ov.reposition()

    assert ov.height() <= parent.height() * 0.55
    assert ov.width() <= 320


def test_collapsing_the_legend_leaves_a_strip_that_still_names_itself(qapp) -> None:
    _parent, ov = _populated_legend()
    ov.show()
    ov.reposition()
    open_height = ov.height()

    ov.toggle_collapsed()

    assert ov.is_collapsed()
    assert not ov._scroll.isVisibleTo(ov)
    assert ov.height() < open_height
    assert ov._title_label.text() == "Classes (3)"

    ov.toggle_collapsed()
    assert ov._scroll.isVisibleTo(ov)


def test_the_sunburst_is_a_result_beside_the_frame_not_legend_chrome(window) -> None:
    """It reads a cover figure off the run; the legend's checkboxes act on the
    cloud. Docked in the overlay it also ate the class list's height budget."""
    legend = window._viewer.legend_overlay
    assert not legend.isAncestorOf(window._cover_sunburst)
    assert window._viewer._cover_band.isAncestorOf(window._cover_sunburst)


def test_the_legend_collapse_state_survives_a_restart(window) -> None:
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    legend = window._viewer.legend_overlay
    try:
        was_collapsed = legend.is_collapsed()
        legend.toggle_collapsed()
        assert settings.value("viewer_legend_collapsed", type=bool) is not was_collapsed
    finally:
        settings.remove("viewer_legend_collapsed")


def test_legend_sort_selected_first_puts_checked_above_unchecked(window) -> None:
    window._build_legend()
    cids = list(window._legend_toggles.keys())
    assert len(cids) >= 3
    window._legend_toggles[cids[0]].setChecked(False)
    window._legend_toggles[cids[1]].setChecked(False)

    order = window._legend_sort_order()
    enabled = window._enabled_class_set()
    last_visible = max(i for i, c in enumerate(order) if c in enabled)
    first_hidden = min(i for i, c in enumerate(order) if c not in enabled)
    assert last_visible < first_hidden


def test_legend_sort_header_click_toggles_direction(window) -> None:
    window._build_legend()
    assert (window._legend_sort_mode, window._legend_sort_ascending) == ("selected", False)

    window._on_legend_sort_clicked("name")  # new column adopts its default (A-Z)
    assert (window._legend_sort_mode, window._legend_sort_ascending) == ("name", True)
    window._on_legend_sort_clicked("name")  # same column flips direction
    assert window._legend_sort_ascending is False
    window._on_legend_sort_clicked("size")  # new column adopts default (largest first)
    assert (window._legend_sort_mode, window._legend_sort_ascending) == ("size", False)


def test_pie_click_toggles_selection(window) -> None:
    window._build_legend()
    cids = list(window._legend_toggles.keys())
    assert len(cids) >= 2

    window._on_deselect_all_classes()
    assert window._enabled_class_set() == frozenset()
    window._on_sunburst_selection([cids[0]])  # add
    assert window._enabled_class_set() == frozenset({cids[0]})
    window._on_sunburst_selection([cids[1]])  # additive, keeps the first
    assert window._enabled_class_set() == frozenset({cids[0], cids[1]})
    window._on_sunburst_selection([cids[0]])  # re-click removes it
    assert window._enabled_class_set() == frozenset({cids[1]})


def test_master_checkbox_select_deselect_and_partial(window) -> None:
    from PySide6.QtCore import Qt

    window._build_legend()
    present = frozenset(window._legend_toggles.keys())
    master = window._viewer.legend_overlay._master_check

    assert window._enabled_class_set() == present  # all on at build
    window._on_master_clicked()  # all -> none
    assert window._enabled_class_set() == frozenset()
    window._on_master_clicked()  # none -> all
    assert window._enabled_class_set() == present

    next(iter(window._legend_toggles.values())).setChecked(False)
    window._update_master_check()
    assert master.checkState() == Qt.CheckState.PartiallyChecked


def test_sunburst_reflects_selection(window, monkeypatch) -> None:
    # The sunburst sync runs through _on_viewer_control_changed, which only acts
    # once the viewer has scene data (as when a run is loaded). Patch via
    # monkeypatch so the class property is restored at teardown and later tests
    # (which may assert has_scene_data is False) aren't affected.
    window._viewer.apply_state = lambda **k: None
    monkeypatch.setattr(type(window._viewer), "has_scene_data", property(lambda self: True))
    window._build_legend()
    sb = window._cover_sunburst

    assert sb._selection_active is False  # all selected at build -> no dimming
    window._on_deselect_all_classes()
    assert sb._selection_active is True and sb._selected_ids == frozenset()
    cids = list(window._legend_toggles.keys())
    window._on_sunburst_selection([cids[0]])
    assert sb._selected_ids == frozenset({cids[0]}) and sb._selection_active is True
    window._on_show_all_classes()
    assert sb._selection_active is False

