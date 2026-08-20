"""Shared widgets from core/widgets.py.

Scenario: a widget that is made visible before a layout adopts it has no parent
at that moment, so Qt maps it as a real top-level window -- an empty titlebar
box that flashes on screen and disappears once the layout reparents it. The
window build constructs several EmptyStates, so one misordered line flashes
several times per launch.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget


@pytest.fixture
def toplevel_shows(monkeypatch):
    """Record every widget made visible while it is still parentless."""
    seen: list[str] = []
    original = QWidget.setVisible

    def record(self, visible=True, *args, **kwargs):
        if visible and self.isWindow():
            seen.append(f"{type(self).__name__}: {self.text()!r}" if hasattr(self, "text") else type(self).__name__)
        return original(self, visible, *args, **kwargs)

    monkeypatch.setattr(QWidget, "setVisible", record)
    return seen


@pytest.mark.parametrize("hint", ["a hint", ""])
def test_empty_state_never_flashes_a_toplevel_window(qapp, toplevel_shows, hint) -> None:
    from deepreefmap_gui.core.widgets import EmptyState

    EmptyState("No runs here yet", hint)

    assert toplevel_shows == []


def test_empty_state_hint_visibility_follows_the_text(qapp) -> None:
    from deepreefmap_gui.core.widgets import EmptyState

    state = EmptyState("message", "")
    assert not state._hint.isVisibleTo(state)

    state.set_text("message", "now there is a hint")
    assert state._hint.isVisibleTo(state)

    state.set_text("message", "")
    assert not state._hint.isVisibleTo(state)


def _tree_column0(tree) -> list[str]:
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def test_sortable_tree_item_orders_by_value_not_text(qapp) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget

    from deepreefmap_gui.core.widgets import SortableTreeItem, enable_sorting

    tree = QTreeWidget()
    tree.setColumnCount(2)
    SortableTreeItem(tree, ["short", "9 m"], {0: "short", 1: 9.0})
    SortableTreeItem(tree, ["long", "12 m"], {0: "long", 1: 12.0})
    # No values at all: the row a draft uses, pinned to the foot either way.
    SortableTreeItem(tree, ["draft", ""])

    enable_sorting(tree, 1)
    assert _tree_column0(tree) == ["short", "long", "draft"]

    tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert _tree_column0(tree) == ["long", "short", "draft"]


def test_enable_sorting_flags_the_header_for_the_theme(qapp) -> None:
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.core.widgets import enable_sorting

    table = QTableWidget(0, 2)
    header = table.horizontalHeader()
    assert header.property("sortable") is None

    enable_sorting(table)
    assert header.property("sortable") == "true"
    assert header.isSortIndicatorShown()
    assert table.isSortingEnabled()


# --- column widths ----------------------------------------------------------

def _spec():
    from deepreefmap_gui.core.widgets import ColumnSpec

    return ColumnSpec(
        fixed={1: 88, 2: 112},
        weights={0: 3, 3: 1},
        minimums={0: 140, 3: 80},
        optional=((4, 76),),
    )


def test_a_viewport_is_divided_by_weight_and_never_overspent(qapp) -> None:
    from deepreefmap_gui.core.widgets import fitted_column_widths

    widths = fitted_column_widths(1000, _spec())

    assert sum(widths.values()) <= 1000
    # 3:1 on what is left after the fixed and optional columns.
    assert widths[0] == widths[3] * 3
    assert widths[0] > widths[3]


def test_an_optional_column_goes_before_an_identifying_one_is_squeezed(qapp) -> None:
    from deepreefmap_gui.core.widgets import fitted_column_widths

    assert 4 in fitted_column_widths(1000, _spec())

    narrow = fitted_column_widths(430, _spec())
    assert 4 not in narrow
    assert narrow[0] >= 140 and narrow[3] >= 80


def test_below_every_floor_the_floors_win_and_the_table_scrolls(qapp) -> None:
    """A column shrunk past reading is not a column, so the width is overspent
    on purpose and the scrollbar is the honest answer."""
    from deepreefmap_gui.core.widgets import fitted_column_widths

    widths = fitted_column_widths(200, _spec())

    assert widths[0] == 140 and widths[3] == 80
    assert sum(widths.values()) > 200


def _sized_table(qapp, width=1000):
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.core.widgets import configure_table, install_column_sizer

    table = QTableWidget(0, 5)
    configure_table(table, ["Name", "Status", "Created", "Kind", "Extra"])
    sizer = install_column_sizer(table, _spec())
    table.resize(width, 200)
    table.show()
    qapp.processEvents()
    qapp.processEvents()
    return table, sizer


def test_every_column_can_be_dragged(qapp) -> None:
    from PySide6.QtWidgets import QHeaderView

    table, _sizer = _sized_table(qapp)
    header = table.horizontalHeader()

    modes = {header.sectionResizeMode(c) for c in range(table.columnCount())}
    assert modes == {QHeaderView.ResizeMode.Interactive}
    assert not header.stretchLastSection()


def test_a_dragged_column_survives_the_window_being_resized(qapp) -> None:
    """Scenario: a column is widened to read a long name, then the window is
    resized.

    Expected behaviour: the width stays. Refitting every column on every resize
    undid the drag before it could be used.
    """
    table, _sizer = _sized_table(qapp)
    table.horizontalHeader().resizeSection(0, 420)

    table.resize(700, 200)
    qapp.processEvents()
    qapp.processEvents()

    assert table.columnWidth(0) == 420


def test_resetting_gives_the_columns_back_to_the_viewport(qapp) -> None:
    table, sizer = _sized_table(qapp)
    fitted = table.columnWidth(0)
    table.horizontalHeader().resizeSection(0, 420)
    assert table.columnWidth(0) == 420

    sizer.reset()
    qapp.processEvents()

    assert table.columnWidth(0) == fitted


def test_dragged_widths_are_remembered_by_heading_not_by_position(qapp, tmp_path) -> None:
    """Stored by name so a changed column set drops what no longer applies
    instead of resizing whichever column moved into that index."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.core.widgets import configure_table, install_column_sizer

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)

    def build(headings):
        table = QTableWidget(0, len(headings))
        configure_table(table, headings)
        install_column_sizer(table, _spec(), settings_key="probe", settings=settings)
        table.resize(1000, 200)
        table.show()
        qapp.processEvents()
        qapp.processEvents()
        return table

    first = build(["Name", "Status", "Created", "Kind", "Extra"])
    first.horizontalHeader().resizeSection(0, 420)

    again = build(["Name", "Status", "Created", "Kind", "Extra"])
    assert again.columnWidth(0) == 420

    renamed = build(["Title", "Status", "Created", "Kind", "Extra"])
    assert renamed.columnWidth(0) != 420


# --- direction --------------------------------------------------------------

def test_the_pill_delegate_finds_its_colour_whichever_word_carries_it(qapp) -> None:
    """A status pill's key leads its decoration ("Succeeded ⚠"); a direction's
    trails it ("→ Forward")."""
    from deepreefmap_gui.core.widgets import DIRECTION_COLORS, STATUS_COLORS

    assert next((w for w in ["succeeded", "⚠"] if w in STATUS_COLORS), None) == "succeeded"
    assert next((w for w in ["→", "forward"] if w in DIRECTION_COLORS), None) == "forward"


def test_a_settings_key_alone_is_enough_to_persist(qapp) -> None:
    """Call sites name the table and nothing else; the store defaults to the
    application's own settings."""
    from PySide6.QtWidgets import QTableWidget

    from deepreefmap_gui.core.widgets import configure_table, install_column_sizer

    table = QTableWidget(0, 5)
    configure_table(table, ["Name", "Status", "Created", "Kind", "Extra"])
    sizer = install_column_sizer(table, _spec(), settings_key="probe-default-store")

    assert sizer._settings is not None
