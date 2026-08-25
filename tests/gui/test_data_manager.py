import json
from pathlib import Path

import pytest
from _factories import (
    make_batch,
    make_transect,
    make_video,
    seed_survey_run,
    write_run,
)
from _qt_wait import wait_until
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QSizePolicy

from deepreefmap_gui.runs.browse import _DETAIL_MIN_WIDTH, _DETAIL_SHARE
from deepreefmap_gui.runs.delete_data_dialog import DeleteChoice
from deepreefmap_gui.runs.run_detail import OrthoDialog
from deepreefmap_gui.runs.run_table import COL_NAME, COL_POINTS, COL_SIZE, COL_STATUS
from deepreefmap_gui.simple.mode import DESTINATIONS
from deepreefmap_gui.survey.catalogue import UNASSIGNED_TITLE
from deepreefmap_gui.survey.models import Transect
from deepreefmap_gui.survey.store import SurveyStore


def listed_runs(window) -> list[str]:
    """Run names in the order the table currently shows them."""
    table = window._data_run_table
    return [table.item(row, COL_NAME).text() for row in range(table.rowCount())]


def cell(window, row: int, column: int) -> str:
    return window._data_run_table.item(row, column).text()


def select_run(window, row: int) -> None:
    window._data_run_table.setCurrentCell(row, COL_NAME)


def row_of(window, name: str) -> int:
    return listed_runs(window).index(name)


def choose_delete(monkeypatch, choice) -> None:
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.DeleteDataDialog.ask",
        staticmethod(lambda scope, parent=None: choice),
    )


class _FakeMime:
    def __init__(self, urls):
        self._urls = urls

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return self._urls


class _FakeDropEvent:
    """Duck-typed drag/drop event, so the filter can be tested without a real drag."""

    def __init__(self, etype, urls):
        self._type = etype
        self._mime = _FakeMime([QUrl.fromLocalFile(u) for u in urls])
        self.accepted = False

    def type(self):
        return self._type

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self.accepted = True


def write_crashed_run(root: Path, dir_name: str) -> Path:
    """A run folder with no manifest: the shape a crash or a kill leaves behind."""
    run_dir = root / dir_name
    run_dir.mkdir(parents=True)
    (run_dir / "run.log").write_text("started\n", encoding="utf-8")
    return run_dir


def write_survey_run(root: Path, dir_name: str, transect: Transect | None = None) -> Transect:
    """Seed a survey run through a store the window will reopen at root/survey.db.

    The window owns its own connection, so this one is closed before handing back.
    """
    store = SurveyStore(root / "survey.db")
    transect, _pass, _run = seed_survey_run(store, root, dir_name, transect=transect)
    store.close()
    return transect


def test_runs_facet_lists_all_runs(out_root, make_window):
    write_run(out_root, "run_a")
    write_run(out_root, "run_b", run_timestamp="2026-07-02T10:00:00+00:00")
    window = make_window()
    assert window._data_facet == "runs"
    assert listed_runs(window) == ["run_b", "run_a"]


def test_transects_facet_groups_and_buckets_unassigned(out_root, make_window):
    write_survey_run(out_root, "assigned")
    write_run(out_root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    window._data_facet_buttons["transects"].click()
    titles = [window._data_tree.topLevelItem(i).text(0) for i in range(window._data_tree.topLevelItemCount())]
    assert titles[0].startswith(UNASSIGNED_TITLE)
    assert any(t.startswith("T1") for t in titles)


def write_session_runs(root: Path, name: str, dirs: list[str]) -> None:
    """Several runs queued together, as one session's worth of passes."""
    store = SurveyStore(root / "survey.db")
    batch = make_batch(store, name)
    for index, dir_name in enumerate(dirs):
        seed_survey_run(store, root, dir_name, transect=make_transect(f"T{index}"), batch=batch)
    store.close()


def test_sessions_facet_groups_a_day_and_describes_it(out_root, make_window):
    write_session_runs(out_root, "2026-07-01", ["north", "south"])
    window = make_window()
    window._data_facet_buttons["sessions"].click()

    assert window._data_tree.topLevelItemCount() == 1
    assert window._data_tree.topLevelItem(0).text(0).startswith("2026-07-01")

    window._data_tree.setCurrentItem(window._data_tree.topLevelItem(0))
    assert window._data_detail_stack.currentWidget() is window._session_detail
    assert window._session_detail.title.text() == "2026-07-01"
    assert len(window._session_detail.entries) == 2
    assert window._session_detail.pass_list.count() == 2


def test_selecting_a_pass_under_a_session_shows_that_run(out_root, make_window):
    """A leaf is one pass, so the run pane owns it rather than the session pane."""
    write_session_runs(out_root, "2026-07-01", ["north", "south"])
    window = make_window()
    window._data_facet_buttons["sessions"].click()
    leaf = window._data_tree.topLevelItem(0).child(0)
    window._data_tree.setCurrentItem(leaf)
    assert window._data_detail_stack.currentWidget() is window._run_detail


def test_tree_selection_filters_run_list(out_root, make_window):
    write_session_runs(out_root, "2026-07-01", ["north", "south"])
    window = make_window()
    window._data_facet_buttons["sessions"].click()
    assert len(listed_runs(window)) == 2
    child = window._data_tree.topLevelItem(0).child(0)
    window._data_tree.setCurrentItem(child)
    assert len(listed_runs(window)) == 1


def test_open_routes_through_auto_load(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "run_a")
    window = make_window()
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    select_run(window, 0)
    window._on_data_open_clicked()
    assert loaded == [run_dir]
    assert "points" in window._view_facts.text()


def test_browse_is_the_destination_holding_the_run_browser(make_window):
    """One widget, one name: the destination called Browse is the run browser."""
    window = make_window()
    assert list(window._simple_nav_buttons) == list(DESTINATIONS)
    assert window._simple_nav_buttons["browse"].text() == "Browse"

    window._set_simple_section("browse")
    assert window._simple_stack.currentWidget().isAncestorOf(window._data_panel)


def test_rename_updates_manifest_and_card(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "run_a")
    window = make_window()
    select_run(window, 0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("reef north", True)),
    )
    window._on_data_rename_clicked()
    on_disk = json.loads((run_dir / "run_manifest.json").read_text())
    assert on_disk["name"] == "reef north"
    assert listed_runs(window) == ["reef north"]


def test_rename_refuses_a_name_another_run_already_has(out_root, make_window, monkeypatch):
    """Expected behaviour: a name in use is asked again, pre-filled with a free
    variant, so a second Enter always gets somewhere.

    Two runs called the same thing cannot be told apart in the row reporting one
    of them.
    """
    write_run(out_root, "run_a")
    write_run(out_root, "run_b", name="reef north")
    window = make_window()
    select_run(window, row_of(window, "run_a"))
    asked = []

    def answer(_parent, _title, _prompt, text="", **_kw):
        asked.append(text)
        return ("reef north", True) if len(asked) == 1 else (text, True)

    monkeypatch.setattr("deepreefmap_gui.runs.browse.QInputDialog.getText", staticmethod(answer))
    window._on_data_rename_clicked()

    assert asked[1] == "reef north 2"
    assert sorted(listed_runs(window)) == ["reef north", "reef north 2"]


def test_the_detail_pane_renames_the_run_it_is_showing(out_root, make_window, monkeypatch):
    write_run(out_root, "run_a")
    window = make_window()
    select_run(window, 0)
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("reef north", True)),
    )
    assert window._run_detail.menu_actions["rename"].isEnabled()
    window._run_detail.menu_actions["rename"].trigger()

    assert listed_runs(window) == ["reef north"]
    assert window._run_detail.title.text() == "reef north"


def test_a_crashed_run_offers_no_rename(out_root, make_window):
    """The name lives in the manifest, and a crashed run never wrote one."""
    write_crashed_run(out_root, "died")
    window = make_window()
    select_run(window, 0)
    assert not window._run_detail.menu_actions["rename"].isEnabled()


def test_the_table_columns_carry_the_survey_metadata(out_root, make_window):
    """Expected behaviour: direction and the moment the footage was shot are
    columns that sort, not fragments of a run name."""
    from deepreefmap_gui.runs.run_table import COL_DIRECTION, COL_RECORDED

    store = SurveyStore(out_root / "survey.db")
    seed_survey_run(
        store,
        out_root,
        "swim",
        direction="reverse",
        video=make_video(captured_at="2026-07-21T08:14:00+00:00"),
    )
    store.close()
    window = make_window()
    window._data_run_table.resize(1600, 400)

    row = row_of(window, "swim")
    # The arrow travels with the word wherever direction is shown.
    assert cell(window, row, COL_DIRECTION) == "← Reverse"
    assert cell(window, row, COL_RECORDED).startswith("2026-07-21")


def test_delete_removes_run_after_confirmation(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "doomed")
    window = make_window()
    select_run(window, 0)
    choose_delete(monkeypatch, DeleteChoice.BOTH)
    window._on_data_delete_clicked()
    assert not run_dir.exists()
    assert listed_runs(window) == []


def test_deleting_only_the_data_keeps_the_run_listed(out_root, make_window, monkeypatch):
    """The record is the run's history, so the row survives the folder."""
    write_survey_run(out_root, "kept_record")
    run_dir = out_root / "kept_record"
    window = make_window()
    select_run(window, 0)
    choose_delete(monkeypatch, DeleteChoice.DATA)
    window._on_data_delete_clicked()
    assert not run_dir.exists()
    assert listed_runs(window) == ["kept_record"]
    entry = window._data_entries[0]
    assert entry.data_missing
    assert cell(window, 0, COL_SIZE) == "removed"


def test_a_record_only_delete_forgets_a_data_removed_run(out_root, make_window, monkeypatch):
    write_survey_run(out_root, "fading")
    window = make_window()
    select_run(window, 0)
    choose_delete(monkeypatch, DeleteChoice.DATA)
    window._on_data_delete_clicked()
    select_run(window, 0)
    choose_delete(monkeypatch, DeleteChoice.METADATA)
    window._on_data_delete_clicked()
    assert listed_runs(window) == []
    store = SurveyStore(out_root / "survey.db")
    assert store.run_by_dir_name("fading") is None


def test_a_data_removed_run_refuses_to_open(out_root, make_window, monkeypatch):
    write_survey_run(out_root, "gone")
    window = make_window()
    select_run(window, 0)
    choose_delete(monkeypatch, DeleteChoice.DATA)
    window._on_data_delete_clicked()
    select_run(window, 0)
    window._on_data_open_clicked()
    assert "removed" in window._status_label.text()
    assert window._active_run_dir is None


def test_delete_refuses_open_run(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "active")
    window = make_window()
    window._active_run_dir = run_dir
    select_run(window, 0)
    warnings = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QMessageBox.information",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    window._on_data_delete_clicked()
    assert run_dir.exists()
    assert warnings


def test_assign_moves_loose_run_under_transect(out_root, make_window, monkeypatch):
    transect = write_survey_run(out_root, "assigned")
    write_run(out_root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    select_run(window, row_of(window, "loose"))
    monkeypatch.setattr(window, "_ask_assign_target", lambda transects: (transect.id, "forward"))
    window._on_data_assign_clicked()
    entries = {e.dir_name: e for e in window._data_entries}
    assert entries["loose"].transect_name == "T1"


def test_size_scan_slot_updates_label_and_cards(out_root, make_window):
    write_run(out_root, "run_a")
    window = make_window()
    window._apply_run_sizes({"run_a": 2_000_000_000})
    assert "1.9 GB" in window._data_disk_label.text()
    assert "on disk" in window._data_disk_label.text()
    # The run count belongs to the group header beside this label, not to both.
    assert "run" not in window._data_disk_label.text()
    assert cell(window, 0, COL_SIZE) == "1.9 GB"


def test_a_watch_refresh_keeps_the_sizes_it_has(out_root, make_window):
    """Scenario: the output folder changes under a window that is showing sizes.

    Expected behaviour: the size is re-measured, but the number already on screen
    stays there while that happens rather than blanking out and coming back.
    """
    write_run(out_root, "run_a")
    window = make_window()
    window._apply_run_sizes({"run_a": 2_000_000_000})

    window._on_data_watch_refresh()

    assert cell(window, 0, COL_SIZE) == "1.9 GB"
    assert "1.9 GB" in window._data_disk_label.text()
    assert wait_until(lambda: not window._data_sizes_scan_running)
    assert window._data_entries[0].size_bytes is not None


def test_refreshing_does_not_disturb_the_folder_it_watches(out_root, make_window):
    """Scenario: the window watches the output root so runs finished elsewhere
    appear unprompted.

    Expected behaviour: refreshing writes nothing into that folder. Anything it
    left there would wake the watcher, which refreshes again, forever.
    """
    write_run(out_root, "run_a")
    window = make_window()
    window._refresh_data_manager()
    # Folder mtime, not its listing: a file created and deleted inside one
    # refresh leaves the listing identical and still wakes the watcher.
    before = out_root.stat().st_mtime_ns

    for _ in range(3):
        window._refresh_data_manager()

    assert out_root.stat().st_mtime_ns == before


def test_rescan_reruns_the_manifest_rebuild(out_root, make_window, monkeypatch):
    """Scenario: a colleague's run is copied in while the app is open.

    Expected behaviour: the once-per-session rebuild gate does not re-link it,
    but Rescan resets the gate and reads the folder back.
    """
    write_run(out_root, "first")
    window = make_window()

    calls = []
    real = SurveyStore.rebuild_from_scan

    def spy(self, scanned_root):
        calls.append(scanned_root)
        return real(self, scanned_root)

    monkeypatch.setattr(SurveyStore, "rebuild_from_scan", spy)
    window._refresh_data_manager()
    assert calls == []  # gated to once per root

    window._on_data_rescan_clicked()
    assert calls == [out_root]
    assert "Rescanned" in window._status_label.text()


def test_results_tab_has_no_rename_row(make_window):
    window = make_window()
    assert not hasattr(window, "_rename_btn")
    assert not hasattr(window, "_rename_edit")


# --- T1.1 open run folder from disk ---


def test_open_run_folder_routes_through_auto_load(tmp_path, make_window, monkeypatch):
    external = write_run(tmp_path / "external", "run_x")
    window = make_window()
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: str(external)),
    )
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    window._on_data_open_folder_clicked()
    assert loaded == [external]


def test_open_run_folder_refused_while_running(make_window, monkeypatch):
    window = make_window()
    monkeypatch.setattr(window, "_run_in_flight", lambda: True)
    steps = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: steps.append("dialog") or ""),
    )
    monkeypatch.setattr(window, "_auto_load_run", lambda p: steps.append("load"))
    window._on_data_open_folder_clicked()
    assert steps == []
    assert "Wait for processing" in window._status_label.text()


# --- T1.2 show in folder ---


def test_show_in_folder_opens_the_run_dir(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "run_a")
    window = make_window()
    opened = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    select_run(window, 0)
    window._on_data_show_in_folder_clicked()
    assert opened == [str(run_dir)]


# --- T1.4 drag and drop ---


def test_drag_enter_with_urls_is_accepted(make_window):
    window = make_window()
    event = _FakeDropEvent(QEvent.Type.DragEnter, ["/data/reef.mp4"])
    assert window._data_drop_event_filter(window._data_run_table, event) is True
    assert event.accepted


def test_dropped_video_registers_a_clip_without_a_pass(tmp_path, make_window, monkeypatch):
    clip = tmp_path / "reef.mp4"
    clip.write_bytes(b"x" * 4096)
    window = make_window()
    monkeypatch.setattr("deepreefmap_gui.simple.batch._probe_video", lambda _p: (60.0, 30.0))
    store = window._survey_store()
    window._handle_data_drop([clip])
    # Probing runs on a worker thread, so the clip arrives with a queued signal.
    assert wait_until(lambda: len(store.list_videos()) == 1)
    assert "Imported 1 clip" in window._status_label.text()
    assert store.list_passes() == []
    assert window._survey_rows == []


def test_dropped_run_folder_opens_it(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "dropped")
    window = make_window()
    loaded = []
    monkeypatch.setattr(window, "_auto_load_run", loaded.append)
    window._handle_data_drop([run_dir])
    assert loaded == [run_dir]


def test_dropped_unsupported_file_reports(tmp_path, make_window):
    junk = tmp_path / "notes.txt"
    junk.write_text("hi")
    window = make_window()
    window._handle_data_drop([junk])
    assert "Drop video files, a folder of them, or a run folder" in window._status_label.text()


# --- T1.5 failed / incomplete runs ---


def test_incomplete_run_is_listed_distinctly_and_deletable(out_root, make_window, monkeypatch):
    write_run(out_root, "done")
    crashed = out_root / "crashed"
    crashed.mkdir(parents=True)
    (crashed / "run.log").write_text("boom")
    window = make_window()

    assert "crashed" in listed_runs(window)
    entry = next(e for e in window._data_entries if e.dir_name == "crashed")
    assert entry.incomplete
    row = row_of(window, "crashed")
    assert cell(window, row, COL_STATUS) == "Incomplete"

    select_run(window, row)
    choose_delete(monkeypatch, DeleteChoice.BOTH)
    window._on_data_delete_clicked()
    assert not crashed.exists()


def test_a_run_that_cannot_be_opened_can_still_be_read(out_root, make_window):
    """An incomplete run has no outputs to load, and its log is the only account.

    The one path that pointed the panel at a run.log ran after a run loaded, so
    it never fired for the runs whose log is worth reading.
    """
    crashed = out_root / "crashed"
    crashed.mkdir(parents=True)
    (crashed / "run.log").write_text("Traceback: it stopped here", encoding="utf-8")
    window = make_window()

    select_run(window, row_of(window, "crashed"))
    detail = window._run_detail
    assert detail.log_btn.isVisibleTo(detail)
    assert not detail.open_btn.isVisibleTo(detail)

    detail.log_btn.click()

    assert "it stopped here" in window._log_view._text.toPlainText()


def test_the_log_is_the_call_to_action_on_a_run_that_cannot_open(out_root, make_window):
    """One filled button per pane. A run with no opener has the log instead,
    and a run that opens keeps its log in the menu rather than beside it."""
    crashed = write_crashed_run(out_root, "died")
    (crashed / "run.log").write_text("stopped\n", encoding="utf-8")
    fine = write_run(out_root, "fine")
    (fine / "run.log").write_text("finished\n", encoding="utf-8")
    window = make_window()
    detail = window._run_detail

    select_run(window, row_of(window, "died"))
    assert detail.log_btn.isVisibleTo(detail)
    assert detail.log_btn.property("cta") == "true"
    assert not detail.open_btn.isVisibleTo(detail)
    assert not detail.menu_actions["show_log"].isVisible()

    select_run(window, row_of(window, "fine"))
    assert not detail.log_btn.isVisibleTo(detail)
    assert detail.open_btn.property("cta") == "true"
    assert detail.menu_actions["show_log"].isEnabled()


def test_the_action_row_fits_the_pane_at_its_floor(out_root, make_window, qapp):
    """Scenario: four labelled buttons in a 260px pane, each one elided.

    Expected behaviour: a primary and a More… menu, both laid out at the width
    they asked for. The floor is the narrowest the pane ever gets, so a row that
    fits here fits the 340px View column too.
    """
    write_run(out_root, "run_a")
    window = make_window()
    window.show()
    # Narrow enough that the detail pane sits on its declared floor.
    window.resize(760, 700)
    qapp.processEvents()
    window._set_simple_section("browse")
    qapp.processEvents()
    select_run(window, 0)
    qapp.processEvents()

    detail = window._run_detail
    assert detail.width() == _DETAIL_MIN_WIDTH
    shown = [b for b in (detail.open_btn, detail.log_btn, detail.more_btn) if b.isVisible()]
    assert detail.more_btn in shown
    for button in shown:
        assert button.width() >= button.sizeHint().width()


def test_the_menu_carries_what_the_row_no_longer_holds(out_root, make_window):
    write_run(out_root, "run_a")
    window = make_window()
    select_run(window, 0)
    assert list(window._run_detail.menu_actions) == [
        "rename",
        "archive",
        "copy_command",
        "show_log",
    ]


def test_a_run_whose_data_went_cannot_have_its_command_copied(out_root, make_window, monkeypatch):
    """The command is built from a manifest, and the manifest went with the data."""
    write_survey_run(out_root, "kept_record")
    window = make_window()
    select_run(window, 0)
    copy = window._run_detail.menu_actions["copy_command"]
    assert copy.isEnabled()

    choose_delete(monkeypatch, DeleteChoice.DATA)
    window._on_data_delete_clicked()
    select_run(window, 0)
    assert not copy.isEnabled()
    assert "gone" in copy.toolTip()


def test_archive_is_offered_only_on_a_recorded_finished_run(out_root, make_window):
    """The registry catalogues artefacts against the run row, so a folder-only
    run has nothing to hang them off."""
    write_run(out_root, "folder_only")
    write_survey_run(out_root, "recorded")
    window = make_window()
    archive = window._run_detail.menu_actions["archive"]

    select_run(window, row_of(window, "folder_only"))
    assert not archive.isEnabled()
    assert "survey database" in archive.toolTip()

    select_run(window, row_of(window, "recorded"))
    assert archive.isEnabled()
    assert "registry's archive" in archive.toolTip()


def test_a_failed_run_cannot_be_archived(out_root, make_window):
    store = SurveyStore(out_root / "survey.db")
    _transect, _pass, run = seed_survey_run(store, out_root, "went_wrong")
    store.set_run_status(run.id, "failed", "ran out of memory")
    store.close()
    window = make_window()

    select_run(window, 0)
    archive = window._run_detail.menu_actions["archive"]
    assert not archive.isEnabled()
    assert "finished run" in archive.toolTip()


def test_a_run_whose_data_went_cannot_be_archived(out_root, make_window, monkeypatch):
    write_survey_run(out_root, "kept_record")
    window = make_window()
    select_run(window, 0)
    archive = window._run_detail.menu_actions["archive"]
    assert archive.isEnabled()

    choose_delete(monkeypatch, DeleteChoice.DATA)
    window._on_data_delete_clicked()
    select_run(window, 0)

    assert not archive.isEnabled()
    assert "output data was removed" in archive.toolTip()


# --- T1.7 multi-select actions ---


def _select_rows(window, names):
    for row in range(window._data_run_table.rowCount()):
        selected = window._data_run_table.item(row, COL_NAME).text() in names
        for column in range(window._data_run_table.columnCount()):
            window._data_run_table.item(row, column).setSelected(selected)


def test_multi_select_delete_removes_every_selected_run(out_root, make_window, monkeypatch):
    write_run(out_root, "a")
    write_run(out_root, "b")
    write_run(out_root, "c")
    window = make_window()
    window._data_run_table.selectAll()
    choose_delete(monkeypatch, DeleteChoice.BOTH)
    window._on_data_delete_clicked()
    assert listed_runs(window) == []


def test_multi_select_delete_refused_when_one_is_open(out_root, make_window, monkeypatch):
    run_a = write_run(out_root, "a")
    write_run(out_root, "b")
    window = make_window()
    window._active_run_dir = run_a
    window._data_run_table.selectAll()
    warnings = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QMessageBox.information",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    window._on_data_delete_clicked()
    assert warnings
    assert run_a.exists()


def test_multi_select_assign_moves_all_selected(out_root, make_window, monkeypatch):
    transect = write_survey_run(out_root, "assigned")
    write_run(out_root, "loose_a", video_hashes=["cd" * 16])
    write_run(out_root, "loose_b", video_hashes=["ef" * 16])
    window = make_window()
    _select_rows(window, {"loose_a", "loose_b"})
    monkeypatch.setattr(window, "_ask_assign_target", lambda transects: (transect.id, "forward"))
    window._on_data_assign_clicked()
    entries = {e.dir_name: e for e in window._data_entries}
    assert entries["loose_a"].transect_name == "T1"
    assert entries["loose_b"].transect_name == "T1"


def test_a_selected_tree_group_can_be_assigned_without_a_table_selection(out_root, make_window, monkeypatch):
    """Scenario: a whole group of loose runs is selected in the rail, nothing
    in the table.

    Expected behaviour: assign acts on the group's runs. The docstring always
    promised this; the group branch was unreachable before.
    """
    transect = write_survey_run(out_root, "assigned")
    write_run(out_root, "loose_a", video_hashes=["cd" * 16])
    write_run(out_root, "loose_b", video_hashes=["cd" * 16])
    window = make_window()
    window._data_facet_buttons["transects"].click()
    unassigned = window._data_tree.topLevelItem(0)
    window._data_tree.setCurrentItem(unassigned)
    window._data_run_table.clearSelection()

    targets = {e.dir_name for e in window._data_assign_targets()}
    assert targets == {"loose_a", "loose_b"}

    monkeypatch.setattr(window, "_ask_assign_target", lambda transects: (transect.id, "forward"))
    window._on_data_assign_clicked()
    entries = {e.dir_name: e for e in window._data_entries}
    assert entries["loose_a"].transect_name == "T1"
    assert entries["loose_b"].transect_name == "T1"


def test_assigning_from_browse_reaches_the_process_table(out_root, make_window, monkeypatch):
    """Scenario: a run is assigned to a transect from Browse while its pass sits
    in the Process table.

    Expected behaviour: the cart row updates too. It held a stale copy before,
    and showed the old transect until the page was rebuilt.
    """
    store = SurveyStore(out_root / "survey.db")
    batch = make_batch(store)
    _t1, pass_, _run = seed_survey_run(store, out_root, "assigned", batch=batch)
    other = make_transect("T2")
    store.add_transect(other)
    store.close()

    window = make_window()
    assert any(row.pass_id == pass_.id for row in window._survey_rows)
    monkeypatch.setattr(window, "_ask_assign_target", lambda transects: (other.id, "forward"))
    select_run(window, row_of(window, "assigned"))
    window._on_data_assign_clicked()

    row = next(r for r in window._survey_rows if r.pass_id == pass_.id)
    assert row.transect_id == other.id, "the cart kept its stale copy"
    assert window._survey_store().get_pass(pass_.id).transect_id == other.id


def test_run_record_names_the_session_in_tooltip_and_detail(out_root, make_window):
    """The table has no Session column, so the tooltip and the detail pane are
    where a run's session is read."""
    from deepreefmap_gui.runs.run_cards import format_run_metadata
    from deepreefmap_gui.runs.run_detail import run_fact_rows

    store = SurveyStore(out_root / "survey.db")
    batch = make_batch(store, "Day 1")
    seed_survey_run(store, out_root, "filed", batch=batch)
    store.close()
    window = make_window()
    entry = next(e for e in window._data_entries if e.dir_name == "filed")

    assert "Session: Day 1" in format_run_metadata(entry)
    rows = dict(run_fact_rows(entry))
    assert rows["Session"] == "Day 1"

    write_run(out_root, "loose", video_hashes=["cd" * 16])
    window._refresh_data_manager()
    loose = next(e for e in window._data_entries if e.dir_name == "loose")
    assert dict(run_fact_rows(loose))["Session"] == "No session recorded"


def test_the_detail_pane_says_when_the_run_was_made(out_root, make_window):
    """The run table has a Created column, and this pane is the only other place
    the fact is reachable."""
    from deepreefmap_gui.runs.run_detail import run_fact_rows

    write_run(out_root, "stamped")
    window = make_window()
    entry = next(e for e in window._data_entries if e.dir_name == "stamped")

    assert dict(run_fact_rows(entry))["Created"].startswith("2026-07-01")

    write_run(out_root, "unstamped", run_timestamp=None)
    window._refresh_data_manager()
    bare = next(e for e in window._data_entries if e.dir_name == "unstamped")
    assert dict(run_fact_rows(bare))["Created"] == "—"


def test_a_rerun_names_its_attempt_in_the_tooltip(out_root, make_window):
    from deepreefmap_gui.runs.run_cards import format_run_metadata

    write_run(out_root, "T1__p01__abcd1234__r02")
    window = make_window()
    entry = next(e for e in window._data_entries)
    assert "Attempt: 2" in format_run_metadata(entry)


def test_add_to_cart_requeues_a_finished_run(out_root, make_window):
    """Scenario: a finished run should be reprocessed.

    Expected behaviour: Add to cart queues its pass into a fresh session. The
    pass carries trim, direction and transect, so nothing is copied; the badge
    moves with it.
    """
    store = SurveyStore(out_root / "survey.db")
    batch = make_batch(store)
    _t, pass_, _run = seed_survey_run(store, out_root, "done_run", batch=batch)
    store.close()

    window = make_window()
    select_run(window, row_of(window, "done_run"))
    window._on_data_add_to_cart_clicked()

    store = window._survey_store()
    cart = store.current_cart()
    assert cart is not None
    assert cart.id != batch.id
    assert [i.pass_id for i in store.list_batch_items(cart.id)] == [pass_.id]
    assert window._cart_button._count == 1
    assert "cart" in window._status_label.text().lower()


def test_add_to_cart_adopts_an_adhoc_run_unassigned(out_root, make_window):
    """A run the database has never seen becomes a pass with no transect --
    a pass is a cutout first, filing it is optional."""
    write_run(out_root, "loose", begin_s=10.0, end_s=50.0)
    window = make_window()
    select_run(window, row_of(window, "loose"))
    window._on_data_add_to_cart_clicked()

    store = window._survey_store()
    passes = store.list_passes()
    assert len(passes) == 1
    adopted = passes[0]
    assert adopted.transect_id is None
    assert (adopted.begin_s, adopted.end_s) == (10.0, 50.0)
    cart = store.current_cart()
    assert [p.id for p in store.passes_in_batch(cart.id)] == [adopted.id]
    # The pass took the cart as its origin session.
    assert adopted.batch_id == cart.id


def test_status_chips_count_and_filter_runs(out_root, make_window):
    """Scenario: a batch left two finished runs and one that crashed.

    Expected behaviour: the chip says how many of each before it is clicked, and
    clicking one narrows the list to those.
    """
    write_run(out_root, "good_a")
    write_run(out_root, "good_b")
    write_crashed_run(out_root, "crashed")
    window = make_window()
    chips = window._data_status_chips
    assert chips._buttons["all"].text().endswith("3")
    assert chips._buttons["succeeded"].text().endswith("2")
    assert chips._buttons["unfinished"].text().endswith("1")

    chips.set_current("unfinished")
    assert listed_runs(window) == ["crashed"]
    chips.set_current("succeeded")
    assert len(listed_runs(window)) == 2


def test_search_narrows_the_run_list(out_root, make_window):
    write_run(out_root, "north_reef")
    write_run(out_root, "south_lagoon")
    window = make_window()
    window._data_search.setText("lagoon")
    assert listed_runs(window) == ["south_lagoon"]
    window._data_search.setText("")
    assert len(listed_runs(window)) == 2


def test_filtered_empty_state_says_why(out_root, make_window):
    write_run(out_root, "north_reef")
    window = make_window()
    window._data_search.setText("nothing matches this")
    assert window._data_run_stack.currentWidget() is window._data_empty_state
    assert "No runs match" in window._data_empty_state._message.text()


def tree_row(window, title):
    for index in range(window._data_tree.topLevelItemCount()):
        item = window._data_tree.topLevelItem(index)
        if item.text(0).startswith(title):
            return item
    raise AssertionError(f"no {title} row in the rail")


def pick_tree_transect(window, title="T1"):
    """Click the transect's row in the rail, the way the user reaches it.

    Both signals, because Qt only emits the selection one when the selection
    actually moves and a click on the already-selected row still has to land.
    The row is not returned: picking one rebuilds the tree that holds it.
    """
    item = tree_row(window, title)
    window._data_tree.setCurrentItem(item)
    window._data_tree.itemClicked.emit(item, 0)


def test_detail_pane_follows_the_selection(out_root, make_window):
    """Nothing transect-shaped appears until a transect is what is selected."""
    write_survey_run(out_root, "assigned")
    window = make_window()

    select_run(window, 0)
    assert window._data_detail_stack.currentIndex() == 1
    assert "assigned" in window._run_detail.title.text()

    window._data_facet_buttons["transects"].click()
    pick_tree_transect(window)
    assert window._data_detail_stack.currentIndex() == 2


def test_the_rail_and_the_map_reach_the_same_card(out_root, make_window):
    """Scenario: a run of the transect is selected, then the transect is picked.

    Expected behaviour: the transect card either way. The run table re-selected
    its run across the rebuild and a run outranks a transect in the pane, so
    picking in the rail used to leave the run card up while the identical click
    on the map showed the transect one.
    """
    transect = write_survey_run(out_root, "assigned")
    window = make_window()
    window._data_facet_buttons["transects"].click()

    window._on_data_map_transect_clicked(str(transect.id))
    assert window._data_detail_stack.currentIndex() == 2

    select_run(window, 0)
    assert window._data_detail_stack.currentIndex() == 1
    pick_tree_transect(window)
    assert window._data_detail_stack.currentIndex() == 2


def test_picking_a_pass_keeps_the_pass_and_names_its_transect(out_root, make_window):
    """A leaf row is a run, not the transect above it, but it still points the
    comparison at that transect."""
    transect = write_survey_run(out_root, "assigned")
    window = make_window()
    window._data_facet_buttons["transects"].click()
    pick_tree_transect(window)
    group = tree_row(window, "T1")
    assert group.childCount() == 1

    window._data_tree.setCurrentItem(group.child(0))
    assert window._data_selected_key[0] == "pass"
    assert window._scope_transect_id == transect.id


def test_a_transect_known_only_by_name_does_not_raise(out_root, make_window):
    """A run naming a transect the store has never heard of is filed under that
    name, so its key holds no id to point anything at."""
    write_run(out_root, "named", survey={"transect": {"name": "Ghost reef"}})
    window = make_window()
    window._data_facet_buttons["transects"].click()
    pick_tree_transect(window, "Ghost reef")
    assert listed_runs(window) == ["named"]


def test_unfinished_run_detail_carries_its_reason(out_root, make_window):
    """The status bar loses a failure on the next event; the pane keeps it."""
    write_crashed_run(out_root, "crashed")
    window = make_window()
    select_run(window, 0)
    assert not window._run_detail.error.isHidden()
    assert "did not finish" in window._run_detail.error.text()
    # A crashed run cannot be opened, only inspected on disk.
    assert not window._data_open_btn.isEnabled()
    assert window._data_show_btn.isEnabled()


# --- Sorting, and the map that drives the transect facet ---


def test_columns_sort_by_value_not_by_their_formatting(out_root, make_window):
    """ "988k pts" is smaller than "1.2M pts", and every string comparison disagrees."""
    write_run(out_root, "small", semantic_reference_points=988_000)
    write_run(out_root, "large", semantic_reference_points=1_200_000)
    window = make_window()

    table = window._data_run_table
    table.sortItems(COL_POINTS, Qt.SortOrder.AscendingOrder)
    assert listed_runs(window) == ["small", "large"]
    table.sortItems(COL_POINTS, Qt.SortOrder.DescendingOrder)
    assert listed_runs(window) == ["large", "small"]


def test_runs_missing_a_fact_sort_last_in_both_directions(out_root, make_window):
    """A blank is not a zero: it sinks whichever way the column is pointed."""
    write_run(out_root, "counted", semantic_reference_points=500)
    write_crashed_run(out_root, "crashed")
    window = make_window()

    table = window._data_run_table
    for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
        table.sortItems(COL_POINTS, order)
        assert listed_runs(window)[-1] == "crashed"


def test_map_click_narrows_the_table_to_that_transect(out_root, make_window):
    transect = write_survey_run(out_root, "assigned")
    write_run(out_root, "loose", video_hashes=["cd" * 16])
    window = make_window()
    window._data_facet_buttons["transects"].click()
    window._data_scope_chips.set_current("all")
    assert len(listed_runs(window)) == 2

    window._on_data_map_transect_clicked(str(transect.id))
    assert listed_runs(window) == ["assigned"]
    assert window._data_selected_key == ("transect", str(transect.id))


def two_sites(root: Path) -> None:
    """Two transects an ocean apart, one run each, so a viewport can separate them."""
    write_survey_run(
        root,
        "fiji",
        Transect(name="Fiji", start_lat=-17.5, start_lon=177.1, end_lat=-17.5005, end_lon=177.1005, length_m=50.0),
    )
    write_survey_run(
        root,
        "azores",
        Transect(name="Azores", start_lat=38.5, start_lon=-28.6, end_lat=38.5005, end_lon=-28.6005, length_m=50.0),
    )


def browse_by_transect(make_window):
    window = make_window()
    window._data_facet_buttons["transects"].click()
    window._data_map.resize(400, 300)
    return window


def look_at(window, lat: float, lon: float, zoom: float = 14) -> None:
    """Move the Browse map and let the coalesced view change land."""
    window._data_map.set_view(lat, lon, zoom)
    window._apply_data_view_change()


def test_browse_lists_only_the_runs_the_map_is_showing(out_root, make_window):
    """Scenario: a survey spanning two sites, browsed by transect.

    Expected behaviour: the map is the filter, the same way it is in Plan, and
    panning to one site leaves the other site's runs behind.
    """
    two_sites(out_root)
    window = browse_by_transect(make_window)
    assert sorted(listed_runs(window)) == ["azores", "fiji"]

    look_at(window, -17.5, 177.1)
    assert listed_runs(window) == ["fiji"]
    look_at(window, 38.5, -28.6)
    assert listed_runs(window) == ["azores"]


def test_all_transects_brings_back_the_runs_off_screen(out_root, make_window):
    two_sites(out_root)
    window = browse_by_transect(make_window)
    look_at(window, -17.5, 177.1)
    assert listed_runs(window) == ["fiji"]

    window._data_scope_chips.set_current("all")
    assert sorted(listed_runs(window)) == ["azores", "fiji"]
    window._data_scope_chips.set_current("in_view")
    assert listed_runs(window) == ["fiji"]


def test_scope_chips_count_what_each_side_would_list(out_root, make_window):
    two_sites(out_root)
    window = browse_by_transect(make_window)
    look_at(window, -17.5, 177.1)
    chips = window._data_scope_chips
    assert chips._buttons["in_view"].text().endswith("1")
    assert chips._buttons["all"].text().endswith("2")


def test_the_in_view_count_follows_the_map_while_showing_all(out_root, make_window):
    """Switching back has to be an informed choice, so the count keeps moving."""
    two_sites(out_root)
    window = browse_by_transect(make_window)
    window._data_scope_chips.set_current("all")
    look_at(window, -17.5, 177.1)
    assert window._data_scope_chips._buttons["in_view"].text().endswith("1")
    look_at(window, 0.0, 0.0)
    assert window._data_scope_chips._buttons["in_view"].text().endswith("0")


def test_runs_with_no_transect_are_not_in_view(out_root, make_window):
    """A run assigned to nothing is nowhere on the map, so only All lists it."""
    write_survey_run(out_root, "assigned")
    write_run(out_root, "loose", video_hashes=["cd" * 16])
    window = browse_by_transect(make_window)
    assert listed_runs(window) == ["assigned"]

    window._data_scope_chips.set_current("all")
    assert sorted(listed_runs(window)) == ["assigned", "loose"]


def test_the_map_scope_appears_only_where_it_decides_anything(out_root, make_window):
    write_survey_run(out_root, "assigned")
    window = make_window()
    chips = window._data_scope_chips
    assert not chips.isVisibleTo(window._data_panel)

    window._data_facet_buttons["transects"].click()
    assert chips.isVisibleTo(window._data_panel)
    window._data_facet_buttons["sessions"].click()
    assert not chips.isVisibleTo(window._data_panel)


def test_a_transect_picked_in_the_tree_outranks_the_map(out_root, make_window):
    """Panning away from a chosen transect must not empty the list underneath.

    The chips stay on screen through the pick, because pressing one is how the
    pick is undone: hiding them exactly when something was selected left no way
    back to In view at all.
    """
    two_sites(out_root)
    window = browse_by_transect(make_window)
    window._on_data_map_transect_clicked(str(window._data_map._transects[0].id))
    picked = listed_runs(window)
    assert len(picked) == 1

    look_at(window, 0.0, 0.0)
    assert listed_runs(window) == picked
    assert window._data_scope_chips.isVisibleTo(window._data_panel)


def test_all_transects_releases_a_picked_transect(out_root, make_window):
    """Scenario: a transect is picked, then All transects is pressed.

    Expected behaviour: the pick is released and the map scope is live again.
    The rail has no All node to click, so the chip is the only way out; without
    it In view was unreachable for the rest of the session.
    """
    two_sites(out_root)
    window = browse_by_transect(make_window)
    window._on_data_map_transect_clicked(str(window._data_map._transects[0].id))
    assert window._data_selected_key is not None

    window._data_scope_chips.set_current("all")
    assert window._data_selected_key is None
    assert sorted(listed_runs(window)) == ["azores", "fiji"]

    look_at(window, -17.5, 177.1)
    window._data_scope_chips.set_current("in_view")
    assert listed_runs(window) == ["fiji"]


def test_empty_state_blames_the_map_when_the_map_is_the_reason(out_root, make_window):
    two_sites(out_root)
    window = browse_by_transect(make_window)
    look_at(window, 0.0, 0.0)
    assert window._data_run_stack.currentWidget() is window._data_empty_state
    assert "part of the map" in window._data_empty_state._message.text()


def test_the_map_draws_transects_only_while_grouping_by_them(out_root, make_window):
    """Grouping by session says nothing about where anything is."""
    write_survey_run(out_root, "assigned")
    window = make_window()

    window._data_facet_buttons["transects"].click()
    assert window._data_map.isVisibleTo(window._data_rail)
    assert [o.label for o in window._data_map._transects] == ["T1"]

    window._data_facet_buttons["sessions"].click()
    assert not window._data_map.isVisibleTo(window._data_rail)


def test_opening_a_run_lands_in_view_mode(out_root, make_window, monkeypatch):
    """Browsing and viewing each get the whole window, never half of it each."""
    run_dir = write_run(out_root, "run_a")
    window = make_window()
    monkeypatch.setattr(window._viewer, "_ensure_plotter", lambda: None)

    window._enter_view_mode(run_dir)
    assert window._current_section() == "view"
    assert window._viewer.isVisibleTo(window)
    assert "run_a" in window._view_title.text()
    assert "run_a" in window._view_detail.title.text()

    window._set_simple_section("browse")
    assert window._viewer.isHidden()


def detail_share(window) -> float:
    _rail, table, detail = window._data_split.sizes()
    return detail / (table + detail)


def select_transect(window, name: str) -> None:
    """Pick a transect group in the Browse rail by its displayed name."""
    window._data_run_table.setCurrentCell(-1, -1)
    for index in range(window._data_tree.topLevelItemCount()):
        item = window._data_tree.topLevelItem(index)
        if item.text(0).startswith(name):
            window._data_tree.setCurrentItem(item)
    window._update_data_actions()


def test_the_table_gets_the_bulk_of_browse(out_root, make_window):
    """The table is the page; the detail pane holds one run's facts beside it."""
    write_run(out_root, "run_a")
    window = make_window()
    window._data_split.resize(1200, 600)

    select_run(window, 0)
    window._apply_data_split_sizes(rail_visible=False)
    assert detail_share(window) == pytest.approx(0.30, abs=0.02)


def test_an_empty_detail_pane_gives_its_width_back_to_the_table(out_root, make_window):
    """Scenario: Browse is open with nothing selected.

    Expected behaviour: the pane that would say "Nothing selected" takes no
    width at all, rather than holding a third of the window for a sentence while
    the table beside it elides its names.
    """
    write_run(out_root, "run_a")
    window = make_window()
    window._data_split.resize(1200, 600)
    window._apply_data_split_sizes(rail_visible=False)

    assert window._data_split.sizes()[2] == 0
    assert window._data_detail_stack.isHidden()

    select_run(window, 0)
    assert detail_share(window) > 0.2
    assert not window._data_detail_stack.isHidden()


def test_every_grouping_gives_the_detail_pane_the_same_share(out_root, make_window):
    """Scenario: the transect pane used to hold a chart and a stats table, so it
    claimed close to half the page while every other pane took a third.

    Expected behaviour: one share. The chart moved to Transects, which is the
    one place a transect is drawn, and what is left here is a summary the size
    of every other summary.
    """
    write_survey_run(out_root, "assigned")
    window = make_window()
    window._data_split.resize(1200, 600)

    window._data_facet_buttons["transects"].click()
    select_transect(window, "T1")
    assert detail_share(window) == pytest.approx(_DETAIL_SHARE, abs=0.02)

    window._data_facet_buttons["runs"].click()
    select_run(window, 0)
    assert detail_share(window) == pytest.approx(_DETAIL_SHARE, abs=0.02)


def test_a_dragged_handle_survives_the_next_resize(out_root, make_window):
    """Re-dividing on every resize must not undo a deliberate drag."""
    write_run(out_root, "run_a")
    window = make_window()
    window._data_split.resize(1200, 600)
    select_run(window, 0)
    window._apply_data_split_sizes(rail_visible=False)

    window._on_data_split_moved()
    window._data_split.setSizes([0, 400, 800])
    window._apply_data_split_sizes(rail_visible=False)
    assert window._data_split.sizes()[2] > window._data_split.sizes()[1]


def test_detail_pane_shows_the_ortho_a_run_produced(out_root, make_window):
    """Expected behaviour: the strip appears for a run that wrote one, and the
    band it sits in keeps its height for a run that did not.

    Hiding the label instead pulled everything below it up the pane, so arrowing
    between a run with an ortho and one without moved the row being read.
    """
    run_dir = write_run(out_root, "with_ortho")
    write_run(out_root, "no_ortho")
    QImage(60, 20, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()

    select_run(window, row_of(window, "with_ortho"))
    assert not window._run_detail.ortho.isHidden()
    assert not window._run_detail.ortho.pixmap().isNull()
    band = window._run_detail.ortho.height()

    select_run(window, row_of(window, "no_ortho"))
    assert window._run_detail.ortho.pixmap().isNull()
    assert window._run_detail.ortho.height() == band


@pytest.mark.parametrize("available", [1440, 1080, 900, 830])
def test_the_run_table_fits_its_columns_rather_than_scrolling(available):
    """Expected behaviour: the columns divide the width they are given.

    Sized to their contents they overflowed instead, so a run with a long clip
    name put a horizontal scrollbar under the one table the page is for. 830px
    is about what Browse leaves the table with the rail open and a run selected.
    """
    from deepreefmap_gui.runs.run_table import COL_NAME, COL_TRANSECT, COL_VIDEO, column_widths

    widths = column_widths(available)
    assert sum(widths.values()) <= available
    # Name identifies the row, so it takes the largest share of the slack.
    assert widths[COL_NAME] > widths[COL_VIDEO] >= widths[COL_TRANSECT]


def test_the_secondary_columns_appear_only_where_there_is_room_for_them():
    """Expected behaviour: Direction and Recorded are dropped, widest last in,
    rather than squeezing the columns that say which run this is.

    A pane wide enough shows both. The 830px Browse leaves with the rail open
    shows neither, and the tooltip is where they are read there.
    """
    from deepreefmap_gui.runs.run_table import COL_DIRECTION, COL_RECORDED, column_widths

    assert {COL_DIRECTION, COL_RECORDED} <= set(column_widths(1440))
    assert COL_DIRECTION in column_widths(900)
    assert COL_RECORDED not in column_widths(900)
    assert not {COL_DIRECTION, COL_RECORDED} & set(column_widths(830))


def test_a_window_too_narrow_for_the_columns_keeps_them_readable():
    """A column shrunk past reading is not a column, so below the floors' total
    the floors win and the table scrolls rather than eliding everything away."""
    from deepreefmap_gui.runs.run_table import COL_NAME, COL_VIDEO, column_widths

    widths = column_widths(400)
    assert widths[COL_NAME] == 140
    assert widths[COL_VIDEO] == 100
    assert sum(widths.values()) > 400


def test_a_tooltip_points_at_the_column_it_was_opened_over(out_root, make_window):
    """Expected behaviour: hovering Video lifts the Input line, hovering Size
    lifts Disk, and every other fact stays where it was.

    The tooltip lists a dozen facts and the pointer is already resting on the one
    the reader wants, so finding it again in the list is work worth saving them.
    """
    from deepreefmap_gui.runs.run_table import COL_SIZE, COL_VIDEO

    write_run(out_root, "a_run", input_videos=["/data/GX_ONE.MP4"])
    window = make_window()
    table = window._data_run_table

    video_tip = table.item(0, COL_VIDEO).toolTip()
    size_tip = table.item(0, COL_SIZE).toolTip()

    assert "<b>Video: GX_ONE.MP4" in video_tip
    assert "<b>Video: GX_ONE.MP4" not in size_tip
    # Lifted, not filtered: the rest of the block is why the tooltip is useful.
    assert "Mode: semantic" in video_tip and "Mode: semantic" in size_tip


def test_the_tooltip_carries_a_line_for_every_column(out_root, make_window):
    """Expected behaviour: a line per column, present even when the run has no
    value for it.

    The tooltip is opened over a column in order to read that column, so a line
    that quietly disappears is the one answer it must never give. Transect and
    Status had no line at all, and the rest vanished when null.
    """
    from deepreefmap_gui.runs.run_table import COL_NAME, COL_TRANSECT

    write_run(out_root, "bare", input_videos=[], semantic_reference_points=None)
    window = make_window()
    tooltip = window._data_run_table.item(0, COL_NAME).toolTip()

    for label in ("Status", "Created", "Transect", "Video", "Frames", "Points", "Runtime", "Size"):
        assert f"{label}:" in tooltip, f"{label} missing from {tooltip}"
    missing = "—"
    assert "Transect: Not assigned yet" in tooltip
    assert f"Video: {missing}" in tooltip
    assert f"Points: {missing}" in tooltip

    # And the column that had no line at all is now emphasised like the rest.
    assert "<b>Transect:" in window._data_run_table.item(0, COL_TRANSECT).toolTip()


def test_numeric_headers_line_up_with_their_digits(window):
    """A right-aligned column under a centred header shares no edge with it."""
    from deepreefmap_gui.runs.run_table import COL_POINTS

    header = window._data_run_table.horizontalHeaderItem(COL_POINTS)
    assert header.textAlignment() & Qt.AlignmentFlag.AlignRight


def _fact_keys(window) -> list[str]:
    grid = window._run_detail.facts._grid
    return [
        grid.itemAtPosition(row, 0).widget().text()
        for row in range(grid.rowCount())
        if grid.itemAtPosition(row, 0) is not None
    ]


def _fact_values(window) -> dict[str, str]:
    grid = window._run_detail.facts._grid
    rows = {}
    for row in range(grid.rowCount()):
        key = grid.itemAtPosition(row, 0)
        value = grid.itemAtPosition(row, 1)
        if key is not None and value is not None:
            rows[key.widget().text()] = value.widget().text()
    return rows


def test_the_detail_pane_shows_the_same_fields_for_every_run(out_root, make_window):
    """Scenario: one run recorded everything, the next crashed before it
    recorded anything.

    Expected behaviour: the same rows in the same order for both, so arrowing
    down the table does not move the row being read out from under the cursor.
    """
    write_run(out_root, "complete", frames_processed=900, fps=5, camera_profile="gopro11")
    write_run(
        out_root,
        "sparse",
        semantic_reference_points=None,
        run_duration_s=None,
        frames_processed=None,
        input_videos=[],
        video_hashes=[],
    )
    window = make_window()

    select_run(window, row_of(window, "complete"))
    complete_keys = _fact_keys(window)

    select_run(window, row_of(window, "sparse"))
    assert _fact_keys(window) == complete_keys

    # Absent facts say so rather than dropping their row.
    values = _fact_values(window)
    assert values["Points"] == "—"
    assert values["Runtime"] == "—"


def test_a_run_that_fell_back_to_depth_says_so(out_root, make_window):
    """The fallback is materially weaker geometry and the manifest is the only
    place that records it, so the pane flags it rather than reading like any
    other success."""
    write_run(out_root, "world", geometry_source="world_points")
    write_run(out_root, "fallback", geometry_source="depth_unprojection")
    window = make_window()

    select_run(window, row_of(window, "world"))
    assert _fact_values(window)["Geometry"] == "world points (full)"

    select_run(window, row_of(window, "fallback"))
    assert _fact_values(window)["Geometry"].startswith("⚠")


def test_the_pane_shows_the_cover_a_run_measured(out_root, make_window):
    run_dir = write_run(out_root, "with_cover")
    (run_dir / "benthic_cover.json").write_text(
        json.dumps(
            {
                "classes": {
                    "1": {"name": "hard coral", "count": 60, "fraction": 0.6},
                    "2": {"name": "sand", "count": 40, "fraction": 0.4},
                },
                "denominator": 100.0,
            }
        )
    )
    write_run(out_root, "no_cover")
    window = make_window()

    select_run(window, row_of(window, "with_cover"))
    legend = " ".join(line.text() for line in window._run_detail.cover._legend)
    assert "hard coral" in legend and "60%" in legend

    # A run that measured none keeps the block's height rather than collapsing
    # everything below it.
    height = window._run_detail.cover.height()
    select_run(window, row_of(window, "no_cover"))
    assert window._run_detail.cover.height() == height


def test_the_ortho_never_widens_the_detail_pane(out_root, make_window):
    """A pixmap's size hint is its own width; the pane must not follow it."""
    run_dir = write_run(out_root, "wide_ortho")
    QImage(2000, 400, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()

    select_run(window, 0)
    before = window._data_split.sizes()
    window._run_detail.show_entry(window._data_entries[0])
    assert window._data_split.sizes() == before

    strip = window._run_detail.ortho
    assert strip.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert strip.pixmap().width() <= window._run_detail.width()


def test_clicking_the_ortho_opens_it_full_size(out_root, make_window, monkeypatch):
    run_dir = write_run(out_root, "clickable")
    QImage(800, 200, QImage.Format.Format_RGB32).save(str(run_dir / "ortho.png"))
    window = make_window()
    select_run(window, 0)

    opened = []

    def record(dialog):
        opened.append(dialog)

    monkeypatch.setattr(OrthoDialog, "exec", record)
    window._run_detail.ortho.clicked.emit()
    assert len(opened) == 1
    assert opened[0].windowTitle() == "clickable"
    # Opens fitted, with the full-resolution ortho behind it to zoom into.
    assert opened[0].view.pixmap().width() == 800
    assert opened[0].view.is_fitted()


def test_an_outcome_is_the_same_chip_wherever_it_is_read(out_root, make_window):
    """One shape and one colour per outcome: the chip you press to filter is the
    chip the pane then shows."""
    from deepreefmap_gui.core.widgets import (
        PILL_TINT_ALPHA,
        STATUS_COLORS,
        StatusChip,
        tinted,
    )

    write_run(out_root, "finished-run")
    window = make_window()
    select_run(window, 0)

    chip = window._run_detail.status
    assert isinstance(chip, StatusChip)
    assert chip.text() == "Succeeded"
    succeeded = STATUS_COLORS["succeeded"]
    assert succeeded in chip.styleSheet()
    assert tinted(succeeded, PILL_TINT_ALPHA) in chip.styleSheet()

    # The filter that finds a failed run is drawn in a failed run's colour.
    filter_chip = window._data_status_chips._buttons["failed"]
    assert STATUS_COLORS["failed"] in filter_chip.styleSheet()


def test_session_delete_takes_runs_and_record_together(out_root, make_window, monkeypatch):
    store = SurveyStore(out_root / "survey.db")
    batch = make_batch(store)
    seed_survey_run(store, out_root, "day1_run", batch=batch)
    store.close()
    window = make_window()
    window._data_facet_buttons["sessions"].click()
    window._data_selected_key = ("session", str(batch.id))
    choose_delete(monkeypatch, DeleteChoice.BOTH)
    window._on_data_session_delete()
    assert not (out_root / "day1_run").exists()
    fresh = window._survey_store()
    assert fresh.get_batch(batch.id) is None
    assert fresh.list_runs() == []
    assert fresh.list_passes() != []
