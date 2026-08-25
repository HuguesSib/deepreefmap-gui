"""Every page fits the window it is designed against, without scrolling sideways.

Horizontal scroll on a table or a page is always a layout that did not fit. The
arithmetic test below catches it without a window; the window test catches what
arithmetic cannot see, which is a widget whose own minimum sets a floor the page
was never budgeted for.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.core.theme import MIN_WINDOW_WIDTH

from .conftest import require_torch

# The widths a field laptop is actually driven at, plus one narrower than any of
# them so a spec that only just fits is not mistaken for one that fits.
WIDTHS = (1000, MIN_WINDOW_WIDTH, 1366, 1500)


def _specs():
    """Every ColumnSpec the app installs on a view, by the name of its table."""
    from deepreefmap_gui.core.widgets import ColumnSpec
    from deepreefmap_gui.notify.history_ui import _COLUMN_SPEC as NOTIFICATIONS
    from deepreefmap_gui.runs.run_table import _COLUMN_SPEC as RUNS
    from deepreefmap_gui.simple import batch
    from deepreefmap_gui.simple.analysis import _STATS_COLUMNS as STATS
    from deepreefmap_gui.simple.config_audit_dialog import _COLUMN_SPEC as CONFIG_AUDIT
    from deepreefmap_gui.simple.plan import _PLAN_COLUMN_SPEC as TRANSECTS
    from deepreefmap_gui.storage.rows import _COLUMN_SPEC as STORAGE

    return {
        "runs": RUNS,
        "storage": STORAGE,
        "transects": TRANSECTS,
        "analysis_stats": STATS,
        "notifications": NOTIFICATIONS,
        "config_audit": CONFIG_AUDIT,
        # Built per instance rather than at module scope, so it is reassembled
        # here from the parts the table is given.
        "passes": ColumnSpec(
            fixed=batch._PASS_COLUMNS_FIXED,
            weights=batch._PASS_COLUMNS_WEIGHTS,
            minimums=batch._PASS_COLUMNS_MINIMUMS,
            optional=batch._PASS_COLUMNS_OPTIONAL,
        ),
    }


@pytest.mark.parametrize("name", sorted(_specs()))
@pytest.mark.parametrize("available", WIDTHS)
def test_a_spec_never_budgets_more_than_the_viewport(name: str, available: int) -> None:
    """The columns a viewport is divided into add up to no more than the viewport.

    Every column narrower than the header's floor used to be widened by Qt after
    the division, which spent width the division had already given away and put
    a permanent scrollbar under the table.
    """
    from deepreefmap_gui.core.widgets import fitted_column_widths

    spec = _specs()[name]
    widths = fitted_column_widths(available, spec)
    assert sum(widths.values()) <= available


@pytest.mark.parametrize("name", sorted(_specs()))
def test_no_column_is_narrower_than_its_view_will_draw_it(name: str) -> None:
    """Nothing the division hands out is a width the header will silently raise."""
    from deepreefmap_gui.core.widgets import fitted_column_widths, section_floor

    spec = _specs()[name]
    floor = section_floor(spec)
    for available in WIDTHS:
        for column, width in fitted_column_widths(available, spec).items():
            assert width >= floor, f"{name} column {column} at {available}px"


def test_the_mandatory_columns_fit_the_narrowest_pane_they_are_given() -> None:
    """A table's floor has to fit the pane the page gives it, not the whole window.

    Browse is the tight one: its table shares the page with the group rail and
    the detail pane, so it sees roughly 700px of a 1280px window.
    """
    from deepreefmap_gui.core.widgets import fitted_column_widths
    from deepreefmap_gui.runs.run_table import _COLUMN_SPEC

    assert sum(fitted_column_widths(700, _COLUMN_SPEC).values()) <= 700


def test_torch_is_available_for_the_window_tests() -> None:
    """The window half of this module needs a built window, which needs torch."""
    require_torch()
