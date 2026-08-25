"""Every database format the app ever wrote must still have a way to the present.

Scenario: the migration steps were once flattened into a baseline, and only the
released version kept a script bringing it forward. Versions 4 and 5 -- written
by builds between that release and the flattening -- were left with no route
forward and none back, so a survey stamped at one could not be opened at all.

Expected behaviour: any version from the oldest supported up to the newest opens
and lands on the newest, with the same schema a database created today has.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from _factories import (
    LEGACY_SCHEMAS,
    V10_TRANSECT_NAMES,
    write_legacy_database,
    write_v10_database,
)

from deepreefmap_gui.survey import backup as bk
from deepreefmap_gui.survey import store
from deepreefmap_gui.survey.store import (
    SurveyStore,
    can_open,
    latest_schema_version,
    oldest_supported_version,
)

SUPPORTED = list(range(oldest_supported_version(), latest_schema_version() + 1))


def _schema(path):
    """Columns, foreign keys and indexes -- the shape, not its formatting.

    Compared this way because a column added by ALTER TABLE and the same column
    written into a CREATE TABLE are the same column, and only the stored SQL
    text differs.
    """
    conn = sqlite3.connect(path)
    tables = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    shape = {}
    for table in tables:
        shape[table] = {
            "columns": [
                (r[1], r[2].upper(), r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")
            ],
            "foreign_keys": sorted(
                (r[2], r[3], r[4], r[5], r[6])
                for r in conn.execute(f"PRAGMA foreign_key_list({table})")
            ),
        }
    shape["#indexes"] = sorted(
        (r[0], r[1]) for r in conn.execute(
            "SELECT name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    )
    conn.close()
    return shape


def _rows(path):
    """Every row of every table, keyed by table and read as its columns stand.

    Columns a later step adds are not compared: what has to survive a rebuild is
    the values that were already there.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    rows = {}
    for table in tables:
        # sync_state is keyed by setting name rather than by an id.
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        order = "id" if "id" in columns else "rowid"
        rows[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    conn.close()
    return rows


def _stamp(path, version: int) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(f"PRAGMA user_version = {version}")
    conn.close()


def _database_at(path, version: int):
    """A database as the build that stamped ``version`` would have left it.

    Below the baseline the shape is frozen in the fixtures. At or above it, the
    build differed from this one only in where its migration list stopped, so
    the database is reconstructed by stopping there -- reaching in for the
    baseline and the steps, because that truncated list is exactly what such a
    build had and there is no other way to state it.
    """
    if version in LEGACY_SCHEMAS:
        return write_legacy_database(path, version)
    conn = sqlite3.connect(path)
    SurveyStore._apply(conn, store._BASELINE, store.SCHEMA_VERSION)
    for migration in store._MIGRATIONS:
        if migration.version <= version:
            SurveyStore._apply(conn, migration.sql, migration.version)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    conn.close()
    return path


@pytest.mark.parametrize("version", SUPPORTED)
def test_every_supported_version_reaches_the_present(tmp_path, version):
    """The gap this whole module exists for: no version in the range is a dead end."""
    path = tmp_path / "survey.db"
    _database_at(path, version)

    SurveyStore(path).close()

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_schema_version()
    conn.close()


@pytest.mark.parametrize("version", sorted(LEGACY_SCHEMAS))
def test_a_carried_database_ends_up_shaped_like_a_new_one(tmp_path, version):
    """Carrying forward and creating fresh must not produce two different schemas.

    They diverge silently otherwise: a query written against one works, and the
    same query against the other fails only once somebody is holding the older
    database.
    """
    carried = tmp_path / "carried.db"
    fresh = tmp_path / "fresh.db"
    write_legacy_database(carried, version)

    SurveyStore(carried).close()
    SurveyStore(fresh).close()

    assert _schema(carried) == _schema(fresh)


@pytest.mark.parametrize("version", sorted(LEGACY_SCHEMAS))
def test_rows_survive_being_carried_forward(tmp_path, version):
    path = tmp_path / "survey.db"
    write_legacy_database(path, version)
    transect_id, video_id, batch_id, pass_id = (str(uuid.uuid4()) for _ in range(4))
    now = "2026-08-01T00:00:00+00:00"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO transect (id, name, description, start_lat, start_lon, end_lat, "
            "end_lon, length_m, depth_m, created_at, updated_at) "
            "VALUES (?, 'T1', '', 1.0, 2.0, 1.1, 2.1, 50.0, 5.0, ?, ?)",
            (transect_id, now, now),
        )
        conn.execute(
            "INSERT INTO video_asset (id, file_name, path, created_at) "
            "VALUES (?, 'a.mp4', '/clips/a.mp4', ?)",
            (video_id, now),
        )
        conn.execute(
            "INSERT INTO survey_batch (id, name, preset_name, created_at) "
            "VALUES (?, '2026-08-01', 'default', ?)",
            (batch_id, now),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction, "
            "begin_s, end_s, notes, created_at) VALUES (?, ?, ?, ?, 'forward', 0.0, 60.0, '', ?)",
            (pass_id, transect_id, video_id, batch_id, now),
        )
        # From v5 the build kept cart membership itself, so it is written here
        # rather than backfilled from the pass on the way up.
        if version >= 5:
            conn.execute(
                "INSERT INTO batch_item (id, batch_id, pass_id, created_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), batch_id, pass_id, now),
            )
    conn.close()

    store = SurveyStore(path)
    try:
        assert [t.name for t in store.list_transects()] == ["T1"]
        assert len(store.list_passes()) == 1
        assert store.list_passes()[0].batch_id == uuid.UUID(batch_id)
    finally:
        store.close()

    conn = sqlite3.connect(path)
    # Backfilled from the pass below v5, carried across from v5 on. Either way
    # the pass arrives in the present still in its cart.
    assert conn.execute("SELECT COUNT(*) FROM batch_item").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_a_populated_v10_survives_the_sync_columns(tmp_path):
    """Scenario: a field laptop's survey, opened by the first build that syncs.

    Expected behaviour: every row is still there with the values it had. The step
    rebuilds transect and transect_pass, and a rebuild is where a column left out
    of the INSERT is silently emptied instead of failing.
    """
    path = tmp_path / "survey.db"
    ids = write_v10_database(path)
    before = _rows(path)

    store = SurveyStore(path)
    try:
        pass_ = store.get_pass(uuid.UUID(ids["pass"]))
        assert pass_.video_ids() == [uuid.UUID(v) for v in ids["videos"]]
        assert (pass_.direction, pass_.begin_s, pass_.end_s) == ("reverse", 5.5, 65.5)
        assert (pass_.notes, pass_.label) == ("surge", "first swim")
        assert pass_.batch_id == uuid.UUID(ids["batch"])
        # Nothing assessed a pass that predates the scale.
        assert (pass_.quality, pass_.campaign_id, pass_.surveyed_on) == (None, None, None)
        assert store.list_batch_items(uuid.UUID(ids["batch"]))[0].overrides == {"fps": 4}
        assert len(store.list_notifications()) == 1
    finally:
        store.close()

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
    after = _rows(path)
    for table, rows in before.items():
        assert rows, f"{table} is empty, so it proves nothing"
        assert [{c: row[c] for c in rows[0]} for row in after[table]] == rows, table


def test_two_transect_names_differing_only_in_case_both_survive(tmp_path):
    """Scenario: v10's UNIQUE was case-sensitive, so 'T1' and 't1' both got in.

    Expected behaviour: the case-insensitive index can still be built, because
    the later row is disambiguated rather than dropped. Failing here would leave
    the survey unopenable, which is the one outcome worse than a renamed line.
    """
    path = tmp_path / "survey.db"
    ids = write_v10_database(path)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE transect SET name = 't1' WHERE id = ?", (ids["transects"][1],))
    conn.close()

    store = SurveyStore(path)
    try:
        first, second = (store.get_transect(uuid.UUID(t)) for t in ids["transects"])
        assert first.name == V10_TRANSECT_NAMES[0]
        assert second.name.startswith("t1 (")
        assert second.length_m == first.length_m
    finally:
        store.close()


def test_the_sync_stamp_backfills_from_when_the_row_was_written(tmp_path):
    """An empty updated_at would offer every existing row to the registry as the
    oldest thing it has ever seen, and last-write-wins would discard the lot."""
    path = tmp_path / "survey.db"
    write_v10_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    already_stamped = {r["id"]: r["updated_at"] for r in conn.execute("SELECT * FROM transect")}
    conn.close()

    SurveyStore(path).close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for table in ("transect", "video_asset", "transect_pass", "run_record"):
        rows = list(conn.execute(f"SELECT created_at, updated_at FROM {table}"))
        assert all(r["updated_at"] >= r["created_at"] for r in rows), table
    # transect kept a real updated_at of its own, which is not overwritten.
    stamps = {r["id"]: r["updated_at"] for r in conn.execute("SELECT * FROM transect")}
    assert stamps == already_stamped
    conn.close()


def test_a_database_below_the_floor_is_refused_without_being_touched(tmp_path):
    """A refusal must leave no trace: not a changed stamp, not a .bak.

    The backup is for a migration that is about to run. Writing one for a
    migration that cannot run leaves a copy in a format no build reads, and
    doing it on every retry is what turned one failure into a flooded log.
    """
    path = tmp_path / "survey.db"
    write_legacy_database(path, 3)
    _stamp(path, oldest_supported_version() - 1)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="cannot carry forward"):
            SurveyStore(path)

    assert bk.list_backups(path) == []
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == oldest_supported_version() - 1
    conn.close()


def test_the_refusal_names_a_version_that_can_open_it(tmp_path):
    """"Cannot open this" is only actionable with the "open it with" beside it."""
    path = tmp_path / "survey.db"
    write_legacy_database(path, 3)
    _stamp(path, 1)

    with pytest.raises(RuntimeError, match=r"Open it once with 0\.2\.0"):
        SurveyStore(path)


def test_can_open_agrees_with_what_the_storedoes(tmp_path):
    below = oldest_supported_version() - 1
    assert not can_open(below)
    assert not can_open(latest_schema_version() + 1)
    assert all(can_open(v) for v in SUPPORTED)
    # A file sqlite has only just created, which the baseline writes whole.
    assert can_open(0)


def test_a_row_stamped_in_the_second_of_the_last_push_is_still_owed(tmp_path):
    """Scenario: a laptop mid-season on v14, where the push watermark decided
    what was owed and carried the row sitting exactly on it.

    Expected behaviour: the upgrade marks that row, because the comparison it
    lands on is exclusive. Without a mark it is the one row the registry never
    receives, and the backfill exists precisely so a laptop mid-season is not
    told it owes nothing.
    """
    path = tmp_path / "survey.db"
    _database_at(path, 14)
    on_the_watermark = "2026-08-01T09:15:00+00:00"
    conn = sqlite3.connect(path)
    transect_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO transect (id, name, start_lat, start_lon, end_lat, end_lon, "
            "created_at, updated_at) VALUES (?, 'T1', 0, 0, 0, 0, ?, ?)",
            (transect_id, on_the_watermark, on_the_watermark),
        )
        conn.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)",
            (f"{store.WATERMARK_PREFIX}transects", on_the_watermark, on_the_watermark),
        )
    conn.close()

    opened = SurveyStore(path)

    assert opened.pending_push_ids("transects") == {uuid.UUID(transect_id)}
    assert [t.name for t in opened.changed_since("transects", on_the_watermark)] == ["T1"]
    opened.close()
