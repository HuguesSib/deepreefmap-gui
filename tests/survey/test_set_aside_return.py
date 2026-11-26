"""A survey an older build set aside comes back when it holds the later work."""

import shutil
import sqlite3

from _factories import make_transect

from deepreefmap_gui.survey.backup import set_aside
from deepreefmap_gui.survey.recovery import bring_back, newer_set_aside_survey
from deepreefmap_gui.survey.store import SurveyStore


def _survey(path, names, at="2026-11-26T10:00:00+00:00"):
    store = SurveyStore(path)
    for name in names:
        store.add_transect(make_transect(name))
    store.close()
    with sqlite3.connect(path) as conn:  # work is told apart by its stamps
        conn.executemany("UPDATE transect SET updated_at = ? WHERE name = ?", [(at, n) for n in names])


def _names(path):
    with sqlite3.connect(path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM transect"))


def test_the_later_survey_an_older_build_moved_aside_is_brought_back(tmp_path):
    db = tmp_path / "survey.db"
    _survey(db, ["T1"])
    shutil.copy(db, tmp_path / "early.db")
    _survey(db, ["T2"], at="2098-01-01T00:00:00+00:00")  # later work
    # What an older build does: the newer file aside, its old backup in place.
    (tmp_path / "survey.db.schema-v25").write_bytes(b"")  # an earlier one: the stamped name is used
    set_aside(db, 25)
    shutil.copy(tmp_path / "early.db", db)

    found = newer_set_aside_survey(db)
    assert found is not None and found.version == 25

    message = bring_back(found, db)

    assert _names(db) == ["T1", "T2"]
    assert "replaced" in message
    assert list(tmp_path.glob("survey.db.replaced.*"))
    assert newer_set_aside_survey(db) is None  # nothing to do twice


def test_an_older_set_aside_copy_is_left_alone(tmp_path):
    db = tmp_path / "survey.db"
    _survey(db, ["T1"])
    shutil.copy(db, tmp_path / "survey.db.schema-v25")
    _survey(db, ["T2"], at="2098-01-01T00:00:00+00:00")

    assert newer_set_aside_survey(db) is None


def test_a_set_aside_copy_this_build_cannot_read_is_left_alone(tmp_path):
    db = tmp_path / "survey.db"
    _survey(db, ["T1"])
    shutil.copy(db, tmp_path / "survey.db.schema-v999")
    with sqlite3.connect(tmp_path / "survey.db.schema-v999") as conn:
        conn.execute("UPDATE transect SET updated_at = '2099-01-01T00:00:00+00:00'")

    assert newer_set_aside_survey(db) is None
