"""The per-drive storage page: what it lists, and what it refuses to delete.

Deletes here arm on the first click and happen on the second, so every test of
one has to press twice. That is the point of the pattern and the reason it is
tested rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

from _factories import write_run_tree
from PySide6.QtCore import Qt

from deepreefmap_gui.profiling.volumes import VolumeUsage
from deepreefmap_gui.storage import rows as rows_mod
from deepreefmap_gui.storage.inventory import MountInventory, MountRun
from deepreefmap_gui.storage.tiers import TIER_CACHE, TIER_WORKING, measure_run

GB = 1024**3


def usage(root: str) -> VolumeUsage:
    return VolumeUsage(
        root=root, label=Path(root).name or root, total_bytes=500 * GB,
        free_bytes=250 * GB, video_bytes=100 * GB, output_bytes=50 * GB,
    )


def show_drive(window, root: str) -> None:
    """Put a drive's button on the bar, then open its page."""
    window._storage_bars.set_volumes([usage(root)])
    window._open_storage_page(root)


def seed_page(window, out_root: Path) -> MountRun:
    """One measured run on the page, without going near a worker thread."""
    run_dir = write_run_tree(out_root)
    run = MountRun(
        dir_name=run_dir.name, run_dir=run_dir, display_name=run_dir.name,
        status="succeeded", breakdown=measure_run(run_dir),
    )
    window._storage_inventory = MountInventory(
        root=str(out_root), holds_out_root=True, runs=(run,)
    )
    window._storage_breakdowns[run.dir_name] = run.breakdown
    window._fill_storage_lists()
    return run


def run_row(window):
    return window._storage_runs.topLevelItem(0)


def tier_row(window, tier: str):
    for item in rows_mod.walk(window._storage_runs):
        if item.data(0, rows_mod.ROLE_TIER) == tier:
            return item
    raise AssertionError(f"no {tier} row")


def tick(item) -> None:
    item.setCheckState(0, Qt.CheckState.Checked)


def test_the_page_builds_with_nothing_to_show(window) -> None:
    window._set_simple_section("storage")
    assert window._current_section() == "storage"
    assert window._storage_runs.topLevelItemCount() == 0


def test_opening_a_drive_lights_its_button_and_no_destination(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))

    assert window._current_section() == "storage"
    assert window._storage_bars.selected_root() == str(out_root)
    assert not any(b.isChecked() for b in window._simple_nav_buttons.values())


def test_pressing_the_lit_drive_again_goes_back_to_browse(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    window._open_storage_page(str(out_root))

    assert window._current_section() == "browse"
    assert window._storage_bars.selected_root() is None


def test_leaving_the_section_unlights_the_drive(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    window._set_simple_section("videos")

    assert window._storage_bars.selected_root() is None


def test_a_scan_from_a_drive_nobody_is_looking_at_any_more_is_dropped(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    stale = window._storage_scan_id - 1

    window._apply_storage_page_scan((stale, MountInventory(root="/somewhere")))

    assert window._storage_inventory is None


def test_one_tier_leaves_the_run_row_part_ticked(window, out_root) -> None:
    """The parent says at a glance that some of this run is spoken for."""
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)

    tick(tier_row(window, TIER_WORKING))

    assert run_row(window).checkState(0) == Qt.CheckState.PartiallyChecked


def test_ticking_the_run_row_takes_the_whole_folder(window, out_root) -> None:
    """The bulk choice, so nobody has to expand a run to delete all of it."""
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run = seed_page(window, out_root)

    tick(run_row(window))

    assert tier_row(window, TIER_CACHE).checkState(0) == Qt.CheckState.Checked
    ticked, count, grave = rows_mod.selected_bytes(window._storage_runs)
    # Counted once as the folder, not again as each tier under it.
    assert (ticked, count, grave) == (run.breakdown.total_bytes, 1, True)

    window._storage_delete_btn.click()
    window._storage_delete_btn.click()
    assert not run.run_dir.exists()


def test_the_free_bar_totals_what_is_ticked_and_re_reads_rather_than_adding_up(
    window, out_root
) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run = seed_page(window, out_root)

    tick(tier_row(window, TIER_CACHE))
    ticked, count, _ = rows_mod.selected_bytes(window._storage_runs)
    assert (ticked, count) == (run.breakdown.tier_bytes(TIER_CACHE), 1)

    tier_row(window, TIER_CACHE).setCheckState(0, Qt.CheckState.Unchecked)
    assert rows_mod.selected_bytes(window._storage_runs) == (0, 0, False)
    assert window._storage_finding.text() == "Nothing to free."


def test_a_grave_choice_says_so_before_it_is_armed(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)

    tick(tier_row(window, TIER_CACHE))
    assert window._storage_warning.text() == ""

    tick(tier_row(window, TIER_WORKING))
    assert "cannot be opened or resumed" in window._storage_warning.text()


def test_one_click_arms_and_deletes_nothing(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run = seed_page(window, out_root)
    tick(tier_row(window, TIER_CACHE))

    window._storage_delete_btn.click()

    assert window._storage_delete_btn.text() == "Click again to delete"
    assert list(run.run_dir.glob("*.scene.zarr.zip"))


def test_the_second_click_deletes(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run = seed_page(window, out_root)
    tick(tier_row(window, TIER_CACHE))

    window._storage_delete_btn.click()
    window._storage_delete_btn.click()

    assert not list(run.run_dir.glob("*.scene.zarr.zip"))
    assert (run.run_dir / "run_manifest.json").exists()
    assert window._storage_delete_btn.text() == "Delete selected"


def test_changing_the_selection_disarms(window, out_root) -> None:
    """The second click must never land on a selection nobody armed."""
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run = seed_page(window, out_root)
    tick(tier_row(window, TIER_CACHE))
    window._storage_delete_btn.click()

    tick(tier_row(window, TIER_WORKING))
    window._storage_delete_btn.click()

    assert measure_run(run.run_dir).openable
    assert window._storage_delete_btn.text() == "Click again to delete"


def test_a_run_in_flight_takes_the_action_away(window, out_root, monkeypatch) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)
    monkeypatch.setattr(type(window), "_run_in_flight", lambda self: True)

    tick(tier_row(window, TIER_CACHE))

    assert not window._storage_delete_btn.isEnabled()
    assert window._storage_warning.text() == "Wait for the current run to finish."


def test_the_run_the_viewer_is_holding_is_offered_no_tiers(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    run_dir = write_run_tree(out_root)
    window._active_run_dir = run_dir
    window._storage_inventory = MountInventory(
        root=str(out_root), holds_out_root=True,
        runs=(MountRun(dir_name=run_dir.name, run_dir=run_dir, display_name=run_dir.name,
                       status="succeeded", breakdown=measure_run(run_dir)),),
    )
    window._fill_storage_lists()

    (row,) = [window._storage_runs.topLevelItem(0)]
    assert row.childCount() == 0
    assert row.text(rows_mod.COL_DETAIL) == rows_mod.OPEN_IN_VIEWER_STATUS
    assert "viewer" in row.toolTip(rows_mod.COL_DETAIL)


def test_a_drive_that_has_gone_leaves_the_lists_saying_so(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)

    window._storage_bars.set_volumes([])
    window._refresh_storage_header()

    assert window._storage_runs_stack.currentIndex() == 1
    assert window._storage_clips_stack.currentIndex() == 1


def test_an_empty_list_stops_reserving_a_share_of_the_page(window, out_root) -> None:
    """Expected behaviour: a drive with runs but no clips gives the whole page to
    the runs, and the clip card shrinks to its message.

    Both cards used to claim a fixed share whatever they held, and a stacked
    layout takes its minimum from its tallest page, so "No clips on this drive"
    was drawn down half the window.
    """
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)
    layout = window._storage_lists_layout

    assert layout.stretch(layout.indexOf(window._storage_runs_card)) > 0
    assert layout.stretch(layout.indexOf(window._storage_clips_card)) == 0


def test_a_drive_with_nothing_on_it_reserves_nothing_for_either_list(window, out_root) -> None:
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    window._storage_inventory = MountInventory(root=str(out_root), holds_out_root=True)
    window._fill_storage_lists()
    layout = window._storage_lists_layout

    assert layout.stretch(layout.indexOf(window._storage_runs_card)) == 0
    assert layout.stretch(layout.indexOf(window._storage_clips_card)) == 0


def test_the_grave_warning_is_hidden_until_there_is_one(window, out_root) -> None:
    """It is the longest sentence on the page, so it wraps onto its own line and
    is not drawn at all until the selection has earned it."""
    out_root.mkdir(exist_ok=True)
    show_drive(window, str(out_root))
    seed_page(window, out_root)

    assert not window._storage_warning.isVisibleTo(window)
    assert window._storage_warning.wordWrap()
