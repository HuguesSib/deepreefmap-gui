"""The window opens whatever state the survey database is in.

Scenario throughout: the app was rolled back to an older version, or the output
root moved to a drive that is not there, and the database in it cannot be
opened. Building the window is where SurveyStore is first constructed, so
anything that raises there used to take the whole launch down -- with the
traceback going to a terminal nobody was running the app from, and on the
packaged Windows build to os.devnull.

Expected behaviour, in every case below: a window, a readable reason, and a way
out.
"""

from __future__ import annotations

import sqlite3

import pytest

from deepreefmap_gui.survey.health import SurveyDbState
from deepreefmap_gui.survey.store import SURVEY_DB_NAME, SurveyStore, latest_schema_version


@pytest.fixture
def rolled_back_root(out_root):
    """An output root holding a database stamped past what this build reads."""
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / SURVEY_DB_NAME
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_schema_version() + 1}")
    conn.commit()
    conn.close()
    return out_root


@pytest.fixture
def corrupt_root(out_root):
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / SURVEY_DB_NAME).write_bytes(b"not a database, just some bytes")
    return out_root


def _window_on(make_window, root, monkeypatch):
    monkeypatch.setenv("DEEPREEFMAP_OUT_ROOT", str(root))
    window = make_window()
    window._out_root_input.setText(str(root))
    window._activate_interface()
    return window


def test_a_database_from_a_newer_build_does_not_stop_the_window(
    make_window, rolled_back_root, monkeypatch
):
    window = _window_on(make_window, rolled_back_root, monkeypatch)
    assert window.isVisible() or window is not None
    assert window._survey_db_health().state is SurveyDbState.TOO_NEW


def test_a_corrupt_database_does_not_stop_the_window(make_window, corrupt_root, monkeypatch):
    window = _window_on(make_window, corrupt_root, monkeypatch)
    assert window._survey_db_health().state is SurveyDbState.CORRUPT


def test_an_unopenable_database_blocks_the_readiness_row(
    make_window, rolled_back_root, monkeypatch
):
    """It has to be visible somewhere the user looks, not only in the log."""
    window = _window_on(make_window, rolled_back_root, monkeypatch)
    window._refresh_readiness_view()

    checks = {c.key: c for c in window._current_setup_checks()}
    survey = checks["survey"]
    assert not survey.ok
    assert not survey.advisory, "an unopenable survey is a blocker, not a warning"
    assert survey.action_label == "Repair…"
    assert "newer version" in survey.detail

    _icon, detail, actions = window._setup_check_rows["survey"]
    assert "newer version" in detail.text()
    assert not actions[0].isHidden()


def test_the_pages_that_need_the_survey_come_up_empty_rather_than_raising(
    make_window, rolled_back_root, monkeypatch
):
    """Each of these refreshers used to construct the store unguarded, and each
    runs during _activate_interface."""
    window = _window_on(make_window, rolled_back_root, monkeypatch)
    window._refresh_transect_list()
    window._refresh_survey_batch_tab()
    window._refresh_survey_analysis()
    window._refresh_data_manager()

    assert window._transect_list.topLevelItemCount() == 0
    assert window._analysis_transect_combo.count() == 0
    assert not window._data_store_ok


def test_nothing_is_written_to_a_database_that_cannot_be_opened(
    make_window, rolled_back_root, monkeypatch
):
    """Declining recovery must leave the newer version's data exactly as it is."""
    path = rolled_back_root / SURVEY_DB_NAME
    before = path.read_bytes()
    _window_on(make_window, rolled_back_root, monkeypatch)
    assert path.read_bytes() == before


def test_a_healthy_database_reports_ready(make_window, out_root, monkeypatch):
    window = _window_on(make_window, out_root, monkeypatch)
    window._refresh_readiness_view()
    checks = {c.key: c for c in window._current_setup_checks()}
    assert checks["survey"].ok
    # The action stays, re-running the check rather than offering a repair for
    # a database that does not need one.
    assert checks["survey"].action_label == "Check again"


def test_an_open_survey_is_not_inspected_again(make_window, out_root, monkeypatch):
    """Refreshes arrive by the handful whenever the output folder changes, and
    the verdict cannot change while the same store stays open."""
    window = _window_on(make_window, out_root, monkeypatch)
    window._try_survey_store()

    calls = []
    import deepreefmap_gui.simple.mode as mode

    real = mode.inspect_survey_db
    monkeypatch.setattr(
        mode, "inspect_survey_db", lambda path: calls.append(path) or real(path)
    )
    for _ in range(3):
        assert window._try_survey_store() is not None
    assert calls == []


@pytest.fixture
def too_old_root(out_root):
    """An output root holding a database older than this build carries forward."""
    from deepreefmap_gui.survey.store import oldest_supported_version

    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / SURVEY_DB_NAME
    SurveyStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {oldest_supported_version() - 1}")
    conn.commit()
    conn.close()
    return out_root


def test_a_database_older_than_this_build_carries_does_not_stop_the_window(
    make_window, too_old_root, monkeypatch
):
    window = _window_on(make_window, too_old_root, monkeypatch)
    assert window._survey_db_health().state is SurveyDbState.TOO_OLD


def test_a_database_that_will_not_open_is_not_tried_again(
    make_window, too_old_root, monkeypatch
):
    """Expected behaviour: one verdict, held.

    Nothing changes between attempts, so a retry can only fail the same way --
    and each one logged a traceback and dropped a backup beside the database.
    The handful of refreshes that arrive on every output-folder change turned
    that into a flooded log and a .bak rewritten in a loop.
    """
    from deepreefmap_gui.survey import backup as bk

    window = _window_on(make_window, too_old_root, monkeypatch)
    path = too_old_root / SURVEY_DB_NAME
    window._try_survey_store()

    calls = []
    import deepreefmap_gui.simple.mode as mode

    real = mode.inspect_survey_db
    monkeypatch.setattr(
        mode, "inspect_survey_db", lambda p: calls.append(p) or real(p)
    )
    for _ in range(5):
        assert window._try_survey_store() is None

    assert calls == []
    assert bk.list_backups(path) == [], "a refused open must leave no copy behind"


def test_recovering_reopens_the_survey(make_window, rolled_back_root, monkeypatch):
    """After recovery the window goes back to reading the database rather than
    needing a restart."""
    from deepreefmap_gui.survey.health import inspect_survey_db
    from deepreefmap_gui.survey.recovery import RecoveryKind, apply_recovery, recovery_options

    window = _window_on(make_window, rolled_back_root, monkeypatch)
    health = window._survey_db_health()
    option = next(
        o for o in recovery_options(health, rolled_back_root) if o.kind is RecoveryKind.REBUILD
    )
    apply_recovery(option, health, rolled_back_root)

    window._survey_health = None
    window._survey_store_obj = None
    assert window._survey_db_health().state is SurveyDbState.OK
    assert inspect_survey_db(rolled_back_root / SURVEY_DB_NAME).openable
    window._refresh_readiness_view()
    assert {c.key: c for c in window._current_setup_checks()}["survey"].ok


def test_a_corrupt_database_replaced_on_disk_is_noticed(
    make_window, corrupt_root, monkeypatch
):
    """Scenario: the file is fixed while the app is open.

    Expected behaviour: the app notices. A verdict about the world around the
    file, rather than about the file's schema stamp, stops being true without
    anything in the app changing -- a drive plugged back in, a share that comes
    back read-write, a corrupt file replaced. Caching those left the app on a
    verdict it could not get out of without a restart.
    """
    window = _window_on(make_window, corrupt_root, monkeypatch)
    assert window._try_survey_store() is None
    assert window._survey_db_health().state is SurveyDbState.CORRUPT

    path = corrupt_root / SURVEY_DB_NAME
    path.unlink()
    SurveyStore(path).close()

    assert window._try_survey_store() is not None


def test_a_rolled_back_database_is_not_re_inspected_on_every_ask(
    make_window, rolled_back_root, monkeypatch
):
    """A schema stamp cannot change while the file does not, so it is taken on trust."""
    from deepreefmap_gui.survey import health as health_mod

    window = _window_on(make_window, rolled_back_root, monkeypatch)
    assert window._try_survey_store() is None

    calls = []
    real = health_mod.inspect_survey_db
    monkeypatch.setattr(
        "deepreefmap_gui.simple.mode.inspect_survey_db",
        lambda p: calls.append(p) or real(p),
    )
    for _ in range(5):
        assert window._try_survey_store() is None

    assert calls == []


def test_a_survey_an_older_build_set_aside_is_offered_back_at_open(make_window, out_root, monkeypatch):
    """The drive went to a laptop with an older build and came back rolled back."""
    import shutil

    from PySide6.QtWidgets import QMessageBox

    from deepreefmap_gui.survey.backup import set_aside

    out_root.mkdir(parents=True, exist_ok=True)
    db = out_root / SURVEY_DB_NAME
    store = SurveyStore(db)
    store.close()
    shutil.copy(db, out_root / "early.db")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE campaign SET updated_at = '2098-01-01T00:00:00+00:00'")
        conn.execute(
            "INSERT INTO transect (id, name, created_at, updated_at) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'LATER', '2098-01-01T00:00:00+00:00', "
            "'2098-01-01T00:00:00+00:00')"
        )
    set_aside(db, latest_schema_version())
    shutil.copy(out_root / "early.db", db)
    asked = []

    def say_yes(*args, **kwargs):
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", say_yes)

    window = _window_on(make_window, out_root, monkeypatch)
    window.check_survey_database()

    assert [t.name for t in window._survey_store().list_transects()] == ["LATER"]
    assert asked and "Put it back?" in asked[0]
