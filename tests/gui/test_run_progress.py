"""Run progress: the detail and total bars, the status ticker, the ETA readout,
and the transport controls that appear while a run is live.

Split out of test_spinner.py, which had grown to cover all of this while its name
promised only the spinner widget. The pure ProgressModel maths lives in
tests/runs/test_progress_model.py; everything here needs a real window.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.profiling.eta import STAGES as ETA_STAGES


@pytest.fixture(autouse=True)
def _isolate_run_history(tmp_path, monkeypatch):
    # The estimator seeds priors from run_timings.json; point it at an empty temp
    # file so the status assertions don't depend on this machine's real history.
    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(tmp_path / "run_timings.json"))


def _plain(html: str) -> str:
    import re

    # The status label is two lines joined by <br>; render that as a space.
    return re.sub("<[^>]+>", "", re.sub(r"<br\s*/?>", " ", html)).strip()


def test_status_ticker_appends_elapsed_and_keeps_base(make_window, monkeypatch) -> None:
    import deepreefmap_gui.runs.progress as progress_mod

    window = make_window()
    window._begin_progress(window._recon_model)

    clock = [1000.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])

    window._apply_progress("mapping", "Mapping", current=3, total=10)
    # The coloured stage token is plain language for the diver. The engineer
    # "Mapping" name survives as the base label after it.
    assert _plain(window._status_label.text()) == "Working out the 3D shape · Mapping 3/10 · 0s"

    clock[0] += 74.0
    window._render_status()
    text = _plain(window._status_label.text())
    assert "Mapping 3/10" in text
    assert text.endswith("1m 14s")


def test_status_ticker_resets_per_stage(make_window, monkeypatch) -> None:
    import deepreefmap_gui.runs.progress as progress_mod

    window = make_window()
    window._begin_progress(window._recon_model)

    clock = [500.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])

    window._apply_progress("preprocess", "Preprocessing", current=1, total=5)
    clock[0] += 40.0
    window._apply_progress("mapping", "Mapping", current=1, total=5)
    # A new phase restarts the stopwatch, so elapsed is near zero, not 40s.
    # The line may carry a stage remainder after it, so pick the elapsed field.
    window._render_status()
    parts = _plain(window._status_label.text()).split(" · ")
    assert parts[2] == "0s"


def test_update_progress_zero_total_is_indeterminate(make_window) -> None:
    window = make_window()
    window._begin_progress(window._recon_model)
    # A stage outside the continuous-fill set keeps the barber-pole for a zero
    # (indeterminate) total.
    window._on_viewer_status(
        "update_progress", stage="startup", current=0, total=0, message="Starting reconstruction"
    )
    assert window._run_progress.stage_percent is None


def test_ortho_steps_advance_the_bar_without_a_count(make_window) -> None:
    # Ortho reports by message with no count. Its sub-phases carry weighted spans
    # so the stage still advances; without them its fraction stays 0 for the whole
    # stage and the countdown is a frozen constant.
    window = make_window()
    window._begin_progress(window._recon_model)
    seen = []
    for message in ("Computing PCA projection", "Sorting points into cells", "Aggregating ortho grid"):
        window._on_viewer_status("update_progress", stage="outputs", current=0, total=0, message=message)
        seen.append(window._run_progress.stage_percent)
    assert all(p is not None for p in seen)
    assert seen == sorted(seen)
    assert 0 < window._eta._runs["ortho"].frac < 1.0


def test_mapping_zero_total_holds_the_continuous_bar(make_window) -> None:
    window = make_window()
    window._begin_progress(window._recon_model)
    # Mapping is one continuous 0-100 bar, so its indeterminate sub-steps (prep,
    # GPU transfer, resume save) show a held determinate bar, not a reset.
    window._on_viewer_status(
        "update_progress", stage="mapping", current=0, total=0, message="LoGeR inference"
    )
    assert window._run_progress.stage_percent is not None
    assert "LoGeR inference" in window._status_label.text()


def test_cloud_subphases_hold_one_continuous_bar(make_window) -> None:
    window = make_window()
    window._begin_progress(window._recon_model)
    # The per-frame semantic-cloud loop only fills its slice of the Cloud stage,
    # not the whole bar. The replacement-radius tail owns most of the weight.
    window._on_viewer_status(
        "update_progress", stage="outputs", current=10, total=10, message="Building semantic cloud"
    )
    filled = window._run_progress.stage_percent
    assert filled is not None
    assert 0 < filled < 100  # not pinned at 100 when the cheap loop ends
    # The indeterminate lexsort holds a determinate bar advanced to its slice, so
    # the stage never reads as finished while it is still working.
    window._on_viewer_status(
        "set_stage", stage="outputs", status="running", message="Applying replacement radius"
    )
    assert window._run_progress.stage_percent >= filled


def test_transport_controls_appear_only_while_a_run_is_in_flight(make_window) -> None:
    window = make_window()
    # isHidden, not isVisible: the offscreen test window is never shown on screen.
    assert window._pause_btn.isHidden()
    assert window._spinner_stop.isHidden()
    window._begin_run_controls()
    assert not window._pause_btn.isHidden()
    assert not window._spinner_stop.isHidden()
    window._end_run_controls()
    assert window._pause_btn.isHidden()
    assert window._spinner_stop.isHidden()


def test_bars_carry_no_text_and_overall_estimate_is_visible(make_window, monkeypatch, tmp_path) -> None:
    import deepreefmap_gui.runs.progress as progress_mod

    # Isolate the profile so the total slot's first-run state is deterministic.
    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(tmp_path / "none.json"))
    window = make_window()
    # The strip at the foot is graphical only; the numbers live in the status
    # text, the estimate label and the queue row.
    assert not window._bottom_progress_bar.isTextVisible()

    clock = [0.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window._begin_progress(window._recon_model)
    window._render_eta()
    # No timings for these models on this machine: say so rather than implying a
    # figure is moments away.
    assert "no timings" in window._eta_total_label.text()
    # Give the estimator history and a frame count so the whole-run total is
    # shown, not withheld: without a driver the pending stages have no basis.
    window._eta.priors = {s.key: 0.5 for s in ETA_STAGES}
    window._eta.frames = 100
    window._eta.expected_points = 10_000
    window._apply_progress("mapping", "Mapping", current=1, total=100)
    clock[0] = 20.0
    window._apply_progress("mapping", "Mapping", current=25, total=100)
    assert "left" in window._eta_total_label.text()


def _cell_rect():
    from PySide6.QtCore import QPoint, QRect, QSize

    return QRect(QPoint(80, 120), QSize(110, 24))


def _hover_running_row(window, table_row: int = 0) -> None:
    """Hover the status cell of the pass in flight, as the table's signal does."""
    window._running_table_row = lambda: table_row
    window._on_queue_row_hover(table_row, _cell_rect())


def test_first_run_popup_hides_future_estimates_but_shows_measured(make_window, monkeypatch, tmp_path) -> None:

    import deepreefmap_gui.runs.progress as progress_mod

    # Isolate the timing profile so the host machine's real history can't leak in.
    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(tmp_path / "none.json"))
    clock = [0.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window = make_window()
    window._begin_progress(window._recon_model)
    assert not window._eta.has_history
    window._apply_progress("mapping", "Mapping", current=1, total=100)
    clock[0] = 20.0
    window._apply_progress("mapping", "Mapping", current=25, total=100)
    _hover_running_row(window)
    text = window._timing_popup._label.text()
    assert "learning timings" in text
    assert "running" in text and "left" in text


def test_hover_popup_builds_rows_from_estimator(make_window, monkeypatch) -> None:
    import deepreefmap_gui.runs.progress as progress_mod

    window = make_window()
    window._begin_progress(window._recon_model)
    clock = [100.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window._apply_progress("preprocess", "Preprocess", current=1, total=10)
    clock[0] += 30.0
    window._apply_progress("mapping", "Mapping", current=2, total=10)
    _hover_running_row(window)
    assert window._timing_popup.isVisible()
    window._on_queue_row_hover(-1, None)
    assert not window._timing_popup.isVisible()


def test_the_breakdown_goes_when_the_cursor_leaves_the_table(make_window, monkeypatch) -> None:
    """Scenario: the pointer goes straight off the window, so no leave reaches the
    table the breakdown is anchored to.

    Expected behaviour: the guard takes it down from where the cursor is.
    """
    from PySide6.QtCore import QPoint

    window = make_window()
    window._begin_progress(window._recon_model)
    window._apply_progress("mapping", "Mapping", current=2, total=10)
    _hover_running_row(window)
    assert window._timing_popup.isVisible()
    assert window._timing_guard_timer.isActive()

    monkeypatch.setattr(
        "deepreefmap_gui.runs.progress.QCursor.pos", lambda: QPoint(4000, 4000)
    )
    window._guard_timing_popup()

    assert not window._timing_popup.isVisible()
    assert not window._timing_guard_timer.isActive()


def test_the_breakdown_goes_when_another_application_takes_the_screen(
    make_window, monkeypatch
) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    window = make_window()
    window._begin_progress(window._recon_model)
    window._apply_progress("mapping", "Mapping", current=2, total=10)
    _hover_running_row(window)
    assert window._timing_popup.isVisible()

    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))

    assert not window._timing_popup.isVisible()


def test_the_breakdown_only_describes_the_row_being_processed(make_window) -> None:
    """Anchored to another row it would be a plausible reading of the wrong pass."""
    window = make_window()
    window._begin_progress(window._recon_model)
    window._apply_progress("mapping", "Mapping", current=2, total=10)
    window._running_table_row = lambda: 0

    # Not merely hidden: hovering another row never builds the popup at all.
    window._on_queue_row_hover(1, _cell_rect())
    assert getattr(window, "_timing_popup", None) is None

    window._on_queue_row_hover(0, _cell_rect())
    assert window._timing_popup.isVisible()


def test_progress_readouts_are_hidden_until_a_run_starts(window) -> None:
    """Scenario: an idle window.

    Expected behaviour: nothing reports progress until there is progress.
    """
    assert not window._bottom_progress_bar.isVisibleTo(window)
    assert not window._eta_total_label.isVisibleTo(window)

    window._begin_progress(window._recon_model)
    assert window._bottom_progress_bar.isVisibleTo(window)
    assert window._eta_total_label.isVisibleTo(window)

    window._reset_progress()
    assert not window._bottom_progress_bar.isVisibleTo(window)
    assert not window._eta_total_label.isVisibleTo(window)


def test_bottom_bar_mirrors_total_progress(window) -> None:
    window._begin_progress(window._recon_model)
    window._apply_progress("preprocess", "Preprocess", current=5, total=10)
    assert window._bottom_progress_bar.value() == window._run_progress.total_percent


def test_status_and_transport_live_in_the_bottom_bar(window) -> None:
    """Interrupting a run belongs with the progress bar it interrupts."""
    for widget in (window._status_label, window._pause_btn, window._spinner_stop):
        assert window._bottom_bar.isAncestorOf(widget)


def test_mapping_detail_bar_never_regresses_across_substeps(window) -> None:
    """Drive the real detail bar, not a copy of its formula.

    A real run's reported sequence: inference windows, the indeterminate GPU
    transfer, the re-anchor point-blocks, the indeterminate save, then complete.
    """
    reports = [
        ("mapping", 0, 8),
        ("mapping", 4, 8),
        ("mapping", 8, 8),
        ("mapping", 0, 0),          # GPU transfer, indeterminate
        ("mapping_align", 80, 160),
        ("mapping_align", 160, 160),
        ("mapping_save", 0, 0),     # resume save, indeterminate
        ("mapping_save", 8, 8),     # mapping complete
    ]
    window._begin_progress(window._recon_model)

    values = []
    for phase, cur, tot in reports:
        window._apply_progress(phase, phase, current=cur, total=tot)
        values.append(window._run_progress.stage_percent)

    assert values == sorted(values)      # never snaps back
    assert values[3] == values[2]        # transfer holds at inference's end
    assert values[-1] == 100             # complete fills the whole mapping bar


def test_a_machine_with_no_timings_says_so_and_still_counts_the_stage(make_window, monkeypatch, tmp_path) -> None:
    # Scenario: a model combination this machine has never run. The whole-run
    # total has no basis and stays withheld, but the stage in flight is measured
    # from this run and must still show a figure.
    import deepreefmap_gui.runs.progress as progress_mod

    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(tmp_path / "none.json"))
    window = make_window()
    clock = [0.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window._begin_progress(window._recon_model)
    window._apply_progress("mapping", "Mapping", current=1, total=100)
    clock[0] = 30.0
    window._apply_progress("mapping", "Mapping", current=30, total=100)
    window._render_status()
    assert "no timings" in window._eta_total_label.text()
    assert "left" in _plain(window._status_label.text())


def test_a_spent_counter_reads_as_finishing_not_as_done(make_window, monkeypatch) -> None:
    # A stage can sit at 100% for a while: the last write and the viewer upload
    # run on past the counter. Neither a countdown nor a blank is honest there.
    import deepreefmap_gui.runs.progress as progress_mod

    window = make_window()
    clock = [0.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window._begin_progress(window._recon_model)
    window._apply_progress("viewer_finalise", "Finalising", current=1, total=1)
    clock[0] = 60.0
    window._render_status()
    assert "finishing" in _plain(window._status_label.text())


def test_the_status_stopwatch_is_scoped_to_the_coarse_stage(make_window, monkeypatch) -> None:
    # The elapsed and the remainder sit on one line and must measure the same
    # thing: mapping_align is still mapping, so the clock does not restart.
    import deepreefmap_gui.runs.progress as progress_mod

    window = make_window()
    clock = [0.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: clock[0])
    window._begin_progress(window._recon_model)
    window._apply_progress("mapping", "Mapping", current=1, total=10)
    clock[0] = 40.0
    window._apply_progress("mapping_align", "Aligning poses to world frame", current=1, total=10)
    window._render_status()
    assert "world frame 40s" in _plain(window._status_label.text())
    # A different coarse stage does restart it.
    clock[0] = 41.0
    window._apply_progress("ortho_pca", "Computing PCA projection", current=0, total=0)
    window._render_status()
    assert "projection 0s" in _plain(window._status_label.text())
