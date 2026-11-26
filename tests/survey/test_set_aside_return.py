"""A survey an older build set aside is found when it holds work the current one lacks."""

import shutil

from _factories import make_transect

from deepreefmap_gui.survey.backup import set_aside
from deepreefmap_gui.survey.recovery import (
    bring_back,
    bring_back_question,
    keep_set_aside,
    set_aside_survey_with_more,
)
from deepreefmap_gui.survey.store import SurveyStore


def _add(path, *names):
    store = SurveyStore(path)
    for name in names:
        store.add_transect(make_transect(name))
    store.close()


def _names(path):
    store = SurveyStore(path)
    try:
        return sorted(t.name for t in store.list_transects())
    finally:
        store.close()


def _rolled_back(tmp_path):
    """What an older build leaves: its old backup in place, the newer file aside."""
    db = tmp_path / "survey.db"
    _add(db, "T1")
    shutil.copy(db, tmp_path / "early.db")
    _add(db, "T2")
    set_aside(db, 25)
    shutil.copy(tmp_path / "early.db", db)
    return db


def test_the_survey_an_older_build_moved_aside_is_found_and_put_back(tmp_path):
    db = _rolled_back(tmp_path)

    found = set_aside_survey_with_more(db)
    assert found is not None and (found.version, found.only_there, found.only_here) == (25, 1, 0)
    assert "Put it back?" in bring_back_question(found)

    bring_back(found, db)

    assert _names(db) == ["T1", "T2"]
    assert list(tmp_path.glob("survey.db.replaced.*"))
    assert set_aside_survey_with_more(db) is None  # nothing to offer twice


def test_work_done_after_the_rollback_is_counted_and_kept(tmp_path):
    db = tmp_path / "survey.db"
    _add(db, "T1")
    shutil.copy(db, tmp_path / "early.db")
    _add(db, "T2", "T4")
    set_aside(db, 25)
    shutil.copy(tmp_path / "early.db", db)
    _add(db, "T3")  # done on the restored copy

    found = set_aside_survey_with_more(db)
    assert (found.only_there, found.only_here) == (2, 1)
    assert "kept beside it" in bring_back_question(found)
    bring_back(found, db)

    replaced = next(tmp_path.glob("survey.db.replaced.*"))
    assert "T3" in _names(replaced)


def test_a_set_aside_copy_with_nothing_new_is_left_alone(tmp_path):
    db = tmp_path / "survey.db"
    _add(db, "T1")
    shutil.copy(db, tmp_path / "survey.db.schema-v25")
    _add(db, "T2")

    assert set_aside_survey_with_more(db) is None


def test_a_set_aside_copy_this_build_cannot_read_is_left_alone(tmp_path):
    db = _rolled_back(tmp_path)
    next(tmp_path.glob("survey.db.schema-v25*")).rename(tmp_path / "survey.db.schema-v999")

    assert set_aside_survey_with_more(db) is None


def test_a_copy_declined_once_is_not_offered_again(tmp_path):
    db = _rolled_back(tmp_path)

    keep_set_aside(set_aside_survey_with_more(db))

    assert set_aside_survey_with_more(db) is None
    assert list(tmp_path.glob("survey.db.schema-v25*.kept"))
