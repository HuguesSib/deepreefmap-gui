"""SQLite persistence for survey data.

One database per output root (``<out_root>/survey.db``). Run directories stay
self-describing through their manifests, so a lost or stale database can always
be rebuilt with :meth:`SurveyStore.rebuild_from_scan`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepreefmap_gui.survey.backup import write_backup
from deepreefmap_gui.survey.models.batch_item import BatchItem
from deepreefmap_gui.survey.models.camera import CameraCalibration, CameraProfile
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.convert import (
    build_document,
    from_row,
    parse_document,
    to_row,
)
from deepreefmap_gui.survey.models.exporters import load_survey_json, save_survey_json
from deepreefmap_gui.survey.models.notification import Notification
from deepreefmap_gui.survey.models.run_record import RUN_STATUSES, TERMINAL_STATUSES, RunRecord
from deepreefmap_gui.survey.models.server_preset import ServerPreset
from deepreefmap_gui.survey.models.site import Site
from deepreefmap_gui.survey.models.survey_batch import SurveyBatch
from deepreefmap_gui.survey.models.transect import Transect
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset

logger = logging.getLogger(__name__)

SURVEY_DB_NAME = "survey.db"

# In the same key-value table as the sync position: a device-local working
# default, while the attribution it produces lands on the pass rows and syncs.
DEFAULT_CAMPAIGN_KEY = "survey.default_campaign"

# One key per section, holding the newest stamp the registry has accepted from
# it. The sync engine owns what they mean; the store needs the prefix because no
# stamp it mints may land at or under one. See _stamp.
WATERMARK_PREFIX = "sync.push_watermark."

# Left on a run row the process abandoned. Short on purpose: it shows in the run
# list and the pass status, so it reads as a fact, not a stack trace.
_INTERRUPTED_REASON = "The app closed before this run finished."

@dataclass(frozen=True)
class Migration:
    """One forward step, applied to any database stamped below ``version``."""

    version: int
    name: str
    sql: str


SCHEMA_VERSION = 6

# The schema as it stands, written whole into a new database.
_BASELINE = """
CREATE TABLE transect (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    start_lat REAL NOT NULL,
    start_lon REAL NOT NULL,
    end_lat REAL NOT NULL,
    end_lon REAL NOT NULL,
    length_m REAL,
    depth_m REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- The tri-states default to 'unknown' rather than 'no': "the camera recorded no
-- gravity" is a different fact from "nobody looked". probed_at is what the
-- backfill selects on, so an unprobed row starts NULL.
CREATE TABLE video_asset (
    id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    path TEXT NOT NULL,
    hash TEXT,
    size_bytes INTEGER,
    mtime TEXT,
    duration_s REAL,
    fps REAL,
    created_at TEXT NOT NULL,
    captured_at TEXT,
    captured_source TEXT,
    width INTEGER,
    height INTEGER,
    codec TEXT,
    probed_at TEXT,
    gravity TEXT NOT NULL DEFAULT 'unknown',
    gps TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX video_asset_hash ON video_asset(hash);
CREATE TABLE survey_batch (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- transect_id is nullable: footage worth processing is not always footage laid
-- against a tape, and such a run simply reports no tape length and is not
-- scaled. extra_video_ids is the GoPro chapters that follow the first video, a
-- JSON array because the list is short, ordered, and only ever read whole.
-- held belongs to the pass, not the session: a batch left half-run overnight
-- comes back with the same passes held.
CREATE TABLE transect_pass (
    id TEXT PRIMARY KEY,
    transect_id TEXT REFERENCES transect(id),
    video_id TEXT NOT NULL REFERENCES video_asset(id),
    batch_id TEXT REFERENCES survey_batch(id),
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    begin_s REAL NOT NULL,
    end_s REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    extra_video_ids TEXT NOT NULL DEFAULT '[]',
    held INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_record (
    id TEXT PRIMARY KEY,
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    run_dir_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    batch_id TEXT REFERENCES survey_batch(id)
);
-- Worklist membership is a table of its own so a pass can be ordered in
-- several sessions.
CREATE TABLE batch_item (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES survey_batch(id),
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, pass_id)
);
"""

# Schema version -> the script that brings it to SCHEMA_VERSION, for every
# version below the baseline that a build ever wrote. The steps that produced
# them were flattened into _BASELINE; these scripts are what a database stamped
# mid-flattening still needs.
#
# Every version from _OLDEST_SUPPORTED up to SCHEMA_VERSION must appear here.
# Flattening again without adding an entry leaves databases at the versions in
# between with no way forward and no way back, so _assert_chain_is_contiguous
# refuses to import.
# transect_id becomes nullable: footage worth processing is not always footage
# laid against a tape. SQLite cannot relax NOT NULL in place, so the table is
# rebuilt; foreign keys are off for the duration, set in _migrate.
_PASS_TRANSECT_NULLABLE = """
CREATE TABLE transect_pass_new (
    id TEXT PRIMARY KEY,
    transect_id TEXT REFERENCES transect(id),
    video_id TEXT NOT NULL REFERENCES video_asset(id),
    batch_id TEXT REFERENCES survey_batch(id),
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    begin_s REAL NOT NULL,
    end_s REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    extra_video_ids TEXT NOT NULL DEFAULT '[]',
    held INTEGER NOT NULL DEFAULT 0
);
INSERT INTO transect_pass_new
    SELECT id, transect_id, video_id, batch_id, direction, begin_s, end_s,
           notes, created_at, extra_video_ids, held
    FROM transect_pass;
DROP TABLE transect_pass;
ALTER TABLE transect_pass_new RENAME TO transect_pass;
"""

# The session a run ran in, and cart membership as a table of its own so a pass
# can be ordered in several sessions. Both backfill from the pass, the only
# session either had, minting ids in the 8-4-4-4-12 form from_row parses back.
_RUN_SESSION_AND_CART = """
ALTER TABLE run_record ADD COLUMN batch_id TEXT REFERENCES survey_batch(id);
UPDATE run_record SET batch_id =
    (SELECT batch_id FROM transect_pass WHERE transect_pass.id = run_record.pass_id);
CREATE TABLE batch_item (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES survey_batch(id),
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, pass_id)
);
INSERT INTO batch_item (id, batch_id, pass_id, created_at)
    SELECT printf('%s-%s-%s-%s-%s',
                  lower(hex(randomblob(4))), lower(hex(randomblob(2))),
                  lower(hex(randomblob(2))), lower(hex(randomblob(2))),
                  lower(hex(randomblob(6)))),
           batch_id, id, created_at
    FROM transect_pass WHERE batch_id IS NOT NULL;
"""

# What Videos reads out of each file itself. The tri-states default to 'unknown'
# rather than 'no': "the camera recorded no gravity" is a different fact from
# "nobody looked".
_VIDEO_PROBE_COLUMNS = """
ALTER TABLE video_asset ADD COLUMN captured_at TEXT;
ALTER TABLE video_asset ADD COLUMN captured_source TEXT;
ALTER TABLE video_asset ADD COLUMN width INTEGER;
ALTER TABLE video_asset ADD COLUMN height INTEGER;
ALTER TABLE video_asset ADD COLUMN codec TEXT;
ALTER TABLE video_asset ADD COLUMN probed_at TEXT;
ALTER TABLE video_asset ADD COLUMN gravity TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE video_asset ADD COLUMN gps TEXT NOT NULL DEFAULT 'unknown';
"""

_CARRY_FORWARD = {
    # What 0.2.0 wrote.
    3: _PASS_TRANSECT_NULLABLE + _RUN_SESSION_AND_CART + _VIDEO_PROBE_COLUMNS,
    # 4 and 5 were never released -- they are what builds between 0.2.0 and the
    # baseline wrote, and each is the version above it minus the step it already
    # has. Composing them this way is what keeps the three in step.
    4: _RUN_SESSION_AND_CART + _VIDEO_PROBE_COLUMNS,
    5: _VIDEO_PROBE_COLUMNS,
}

_OLDEST_CARRIED = min(_CARRY_FORWARD)


def _assert_chain_is_contiguous() -> None:
    """Every version below the baseline must have a way up to it.

    A database can only be opened at a version this reaches, so a gap is not a
    tidy-up waiting to happen: it strands whoever is sitting on that version
    with no route forward and none back. Checked at import so the gap is a
    failed test rather than a field laptop that will not open its survey.
    """
    missing = [v for v in range(_OLDEST_CARRIED, SCHEMA_VERSION) if v not in _CARRY_FORWARD]
    if missing:
        raise AssertionError(
            f"survey schema versions {missing} have no carry-forward to "
            f"v{SCHEMA_VERSION}. A database stamped at one could not be opened."
        )


_assert_chain_is_contiguous()

# The sections this device authors, and the table behind each. Only these can
# owe the registry anything, so only these are marked when a person edits one.
_PENDING_TABLES: dict[str, str] = {
    "sites": "site",
    "campaigns": "campaign",
    "transects": "transect",
    "videos": "video_asset",
    "passes": "transect_pass",
    "runs": "run_record",
}

# What migration 15 marked, before sites and campaigns could be made here.
_PENDING_TABLES_15: dict[str, str] = {
    "transects": "transect",
    "videos": "video_asset",
    "passes": "transect_pass",
    "runs": "run_record",
}


def _pending_push_triggers(tables: Mapping[str, str]) -> str:
    """Mark every locally written row of an authored table, whatever wrote it.

    Triggers rather than calls in the write helpers: a merge, a session delete
    and a run's status all bump ``updated_at`` with SQL of their own, and one
    forgotten call is a row that never reaches the registry again. The update
    trigger fires only on a stamp that actually moved, so adopting a pulled
    chapter order stays what it is -- the registry's data, not an edit to send
    back.
    """
    return "".join(
        f"""
        CREATE TRIGGER pending_push_{table}_insert AFTER INSERT ON {table}
        BEGIN
            INSERT OR IGNORE INTO pending_push (section, row_id)
            VALUES ('{section}', new.id);
        END;
        CREATE TRIGGER pending_push_{table}_update AFTER UPDATE OF updated_at ON {table}
        WHEN new.updated_at IS NOT old.updated_at
        BEGIN
            INSERT OR IGNORE INTO pending_push (section, row_id)
            VALUES ('{section}', new.id);
        END;
        """
        for section, table in tables.items()
    )


def _pending_push_backfill(tables: Mapping[str, str]) -> str:
    """Everything the previous build would have called unpushed, marked once.

    That build read a stamp at or past the section's watermark as a local edit,
    so that is what the marks start as. An empty table would instead tell a
    laptop mid-season that it owed the registry nothing.

    At the watermark and not merely past it, matching the inclusive comparison
    that build used. changed_since is exclusive now, so a row edited in the same
    second the last push was accepted in would have neither a mark nor a stamp
    to carry it, and would be the one row this laptop never sent. The row that
    earned the watermark is marked too and goes once more, which the registry
    answers with a skip.
    """
    return "".join(
        f"""
        INSERT OR IGNORE INTO pending_push (section, row_id)
        SELECT '{section}', id FROM {table}
        WHERE updated_at >= COALESCE(
            (SELECT value FROM sync_state WHERE key = '{WATERMARK_PREFIX}{section}'), '');
        """
        for section, table in tables.items()
    )


# Steps taken after the baseline was cut. Appended to, never renumbered. Both
# the fresh path and the carry-forward path land on SCHEMA_VERSION first, so
# every step here runs on either kind of database.
_MIGRATIONS: list[Migration] = [
    # A cart row is a plan to process a pass, so it cannot outlive the pass.
    # Without the cascade, deleting a pass that sits in any cart fails on the
    # foreign key and the pass cannot be removed at all. SQLite cannot alter a
    # foreign key in place, so batch_item is rebuilt; nothing references
    # batch_item, so the rename repoints no other table. run_record.pass_id
    # stays restricting: a run is history, and deleting the pass under it would
    # leave a finished run naming nothing.
    Migration(
        7,
        "batch_item cascades on pass delete",
        """
        CREATE TABLE batch_item_new (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES survey_batch(id),
            pass_id TEXT NOT NULL REFERENCES transect_pass(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            UNIQUE (batch_id, pass_id)
        );
        INSERT INTO batch_item_new (id, batch_id, pass_id, created_at)
            SELECT id, batch_id, pass_id, created_at FROM batch_item;
        DROP TABLE batch_item;
        ALTER TABLE batch_item_new RENAME TO batch_item;
        """,
    ),
    # Everything that has asked for attention, and what became of it. No foreign
    # keys: a notification outlives whatever provoked it, and a log that
    # disappeared when the pass it complained about was deleted would lose
    # exactly the entry somebody went looking for.
    Migration(
        8,
        "notification log",
        """
        CREATE TABLE notification (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('condition', 'event')),
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'blocker')),
            scope TEXT NOT NULL CHECK (scope IN ('survey', 'machine')),
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT '',
            subject_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            read_at TEXT,
            dismissed_at TEXT
        );
        -- One open episode per condition. The centre keeps the open set in
        -- memory, so a store reopened under a second window would otherwise
        -- leave two rows for one fault and resolve only one of them.
        CREATE UNIQUE INDEX notification_open_condition
            ON notification(fingerprint)
            WHERE resolved_at IS NULL AND kind = 'condition';
        CREATE INDEX notification_created ON notification(created_at);
        """,
    ),
    # The cart carries the plan: what order its passes run in, and which of them
    # depart from the session's settings. Both belong to the membership rather
    # than to the pass, because the next session may plan the same pass
    # differently. position backfills from rowid, which is the order the cart was
    # filled in and the order the table showed until now.
    #
    # held goes in the same step. A pass is now either in the cart or out of it,
    # so a third state meaning "in the cart but skipped every time" has nothing
    # left to describe, and transect_pass is rebuilt without it.
    Migration(
        9,
        "cart rows carry their order and their settings; passes are no longer held",
        """
        CREATE TABLE batch_item_new (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES survey_batch(id),
            pass_id TEXT NOT NULL REFERENCES transect_pass(id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0,
            overrides TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE (batch_id, pass_id)
        );
        INSERT INTO batch_item_new (id, batch_id, pass_id, position, overrides, created_at)
            SELECT id, batch_id, pass_id,
                   (SELECT COUNT(*) FROM batch_item AS earlier
                     WHERE earlier.batch_id = batch_item.batch_id
                       AND earlier.rowid < batch_item.rowid),
                   '{}', created_at
            FROM batch_item;
        DROP TABLE batch_item;
        ALTER TABLE batch_item_new RENAME TO batch_item;

        CREATE TABLE transect_pass_new (
            id TEXT PRIMARY KEY,
            transect_id TEXT REFERENCES transect(id),
            video_id TEXT NOT NULL REFERENCES video_asset(id),
            batch_id TEXT REFERENCES survey_batch(id),
            direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
            begin_s REAL NOT NULL,
            end_s REAL NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            extra_video_ids TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO transect_pass_new
            SELECT id, transect_id, video_id, batch_id, direction, begin_s, end_s,
                   notes, created_at, extra_video_ids
            FROM transect_pass;
        DROP TABLE transect_pass;
        ALTER TABLE transect_pass_new RENAME TO transect_pass;
        """,
    ),
    # A section's own name, as opposed to its run directory's. Not backfilled:
    # empty means unnamed, and the default is produced on read.
    #
    # After the rebuild in 9, which lists its columns and would drop this.
    Migration(
        10,
        "sections carry a name of their own",
        """
        ALTER TABLE transect_pass ADD COLUMN label TEXT NOT NULL DEFAULT '';
        """,
    ),
    # What the metadata registry remembers, in the shape it remembers it. Rows
    # carry client-minted ids, an updated_at that decides last-write-wins, and a
    # deleted_at so a delete travels as a fact rather than as an absence. Cart
    # rows, the notification log and everything device-local (paths, timings,
    # probes) are deliberately not here: laptops compute, the server remembers.
    #
    # server_seq is not a column. It is the server's own monotonic cursor, and
    # this side stores the last one it saw machine-locally.
    Migration(
        11,
        "syncable rows carry sites, campaigns and their sync stamps",
        """
        CREATE TABLE site (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            country TEXT,
            region TEXT,
            description TEXT NOT NULL DEFAULT '',
            latitude REAL,
            longitude REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            created_by TEXT,
            device_id TEXT
        );
        -- Case-insensitive and tombstone-aware, matching the registry's partial
        -- unique index: a deleted site's name is free to use again.
        CREATE UNIQUE INDEX site_name_lower ON site(LOWER(name)) WHERE deleted_at IS NULL;

        CREATE TABLE campaign (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            begin_date TEXT,
            end_date TEXT,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            created_by TEXT,
            device_id TEXT
        );
        CREATE UNIQUE INDEX campaign_name_lower
            ON campaign(LOWER(name)) WHERE deleted_at IS NULL;

        -- transect.name was unique globally, which sync cannot hold: two sites
        -- may each have a "T1" and pulling the second would be refused. SQLite
        -- cannot drop a UNIQUE in place, so the table is rebuilt.
        CREATE TABLE transect_new (
            id TEXT PRIMARY KEY,
            site_id TEXT REFERENCES site(id),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            start_lat REAL NOT NULL,
            start_lon REAL NOT NULL,
            start_accuracy_m REAL,
            end_lat REAL NOT NULL,
            end_lon REAL NOT NULL,
            end_accuracy_m REAL,
            length_m REAL,
            depth_m REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            created_by TEXT,
            device_id TEXT
        );
        INSERT INTO transect_new
            (id, name, description, start_lat, start_lon, end_lat, end_lon,
             length_m, depth_m, created_at, updated_at)
            SELECT id, name, description, start_lat, start_lon, end_lat, end_lon,
                   length_m, depth_m, created_at, updated_at
            FROM transect;
        DROP TABLE transect;
        ALTER TABLE transect_new RENAME TO transect;

        -- The old constraint was case-sensitive, so 'T1' and 't1' may both be
        -- here and the new index would refuse to be built at all. The earliest
        -- row keeps the name; the rest take their own id, which reads as the
        -- collision it is instead of stranding the laptop on a failed migration.
        UPDATE transect SET name = name || ' (' || SUBSTR(id, 1, 8) || ')'
        WHERE EXISTS (
            SELECT 1 FROM transect AS earlier
            WHERE LOWER(earlier.name) = LOWER(transect.name)
              AND earlier.rowid < transect.rowid
        );
        CREATE UNIQUE INDEX transect_site_name_lower
            ON transect(site_id, LOWER(name)) WHERE deleted_at IS NULL;

        -- quality needs a CHECK and SQLite cannot add one in place. video_id and
        -- extra_video_ids stay exactly as they are: the registry's pass_video
        -- join table is a wire shape, and the sync layer orders video_ids() into
        -- it rather than this side keeping two records of one relationship.
        CREATE TABLE transect_pass_new (
            id TEXT PRIMARY KEY,
            transect_id TEXT REFERENCES transect(id),
            campaign_id TEXT REFERENCES campaign(id),
            video_id TEXT NOT NULL REFERENCES video_asset(id),
            batch_id TEXT REFERENCES survey_batch(id),
            direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
            upside_down INTEGER NOT NULL DEFAULT 0,
            begin_s REAL NOT NULL,
            end_s REAL NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            quality TEXT CHECK (quality IS NULL OR quality IN
                ('excellent', 'very_good', 'good', 'meh', 'bad', 'very_bad')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            created_by TEXT,
            device_id TEXT,
            extra_video_ids TEXT NOT NULL DEFAULT '[]'
        );
        INSERT INTO transect_pass_new
            (id, transect_id, video_id, batch_id, direction, begin_s, end_s,
             label, notes, created_at, updated_at, extra_video_ids)
            SELECT id, transect_id, video_id, batch_id, direction, begin_s, end_s,
                   label, notes, created_at, created_at, extra_video_ids
            FROM transect_pass;
        DROP TABLE transect_pass;
        ALTER TABLE transect_pass_new RENAME TO transect_pass;

        ALTER TABLE video_asset ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE video_asset ADD COLUMN deleted_at TEXT;
        ALTER TABLE video_asset ADD COLUMN created_by TEXT;
        ALTER TABLE video_asset ADD COLUMN device_id TEXT;
        ALTER TABLE run_record ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE run_record ADD COLUMN deleted_at TEXT;
        ALTER TABLE run_record ADD COLUMN created_by TEXT;
        ALTER TABLE run_record ADD COLUMN device_id TEXT;
        -- Backfilled rather than left empty: last-write-wins compares these, and
        -- an epoch stamp would offer every existing row to the server as the
        -- oldest thing it has ever seen.
        UPDATE video_asset SET updated_at = created_at;
        UPDATE run_record SET updated_at = created_at;

        -- A tombstone keeps its hash, so the index that finds a clip by content
        -- skips it: a merged-away duplicate must not answer for the keeper.
        DROP INDEX video_asset_hash;
        CREATE INDEX video_asset_hash ON video_asset(hash) WHERE deleted_at IS NULL;

        -- Where this device is in the conversation: the last server cursor, the
        -- push watermark, the device id and the server url. It lives here rather
        -- than in QSettings because two output roots are two devices' worth of
        -- data, and it is never a document section and never pushed.
        CREATE TABLE sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    # Provenance and timings copied onto the run row when it finishes, so a
    # pruned run directory no longer degrades the next push to nulls. The dict
    # columns hold JSON text; NULL there means "nothing recorded", which is a
    # different fact from an empty object.
    Migration(
        12,
        "runs carry their provenance, clips their full-file digest",
        """
        ALTER TABLE run_record ADD COLUMN gui_version TEXT;
        ALTER TABLE run_record ADD COLUMN library_version TEXT;
        ALTER TABLE run_record ADD COLUMN segmentation_model TEXT;
        ALTER TABLE run_record ADD COLUMN mapping_backend TEXT;
        ALTER TABLE run_record ADD COLUMN taxonomy_version INTEGER;
        ALTER TABLE run_record ADD COLUMN taxonomy_hash TEXT;
        ALTER TABLE run_record ADD COLUMN model_revisions TEXT;
        ALTER TABLE run_record ADD COLUMN preset_name TEXT;
        ALTER TABLE run_record ADD COLUMN preset_version INTEGER;
        ALTER TABLE run_record ADD COLUMN preset_hash TEXT;
        ALTER TABLE run_record ADD COLUMN preset_deviations TEXT;
        ALTER TABLE run_record ADD COLUMN run_duration_s REAL;
        ALTER TABLE run_record ADD COLUMN stage_durations TEXT;
        ALTER TABLE run_record ADD COLUMN stage_peaks TEXT;

        """,
    ),
    # Presets the registry publishes, pulled whole. Pull-only, so the rows
    # carry the registry's stamps and nothing here ever pushes one back.
    Migration(
        13,
        "the registry's presets are pulled into their own table",
        """
        CREATE TABLE server_preset (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            settings TEXT NOT NULL DEFAULT '{}',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            created_by TEXT,
            device_id TEXT
        );
        """,
    ),
    # The grain a peak is comparable at, which the local timing profile has
    # always keyed on and the registry now collates at too. The two sizes are
    # pixel counts the run processed at, never the name of a resolution preset.
    # Existing rows stay NULL: the values were only ever in the manifest, and
    # reading every run directory to backfill would be a scan of the disk.
    Migration(
        14,
        "runs carry the configuration they ran at",
        """
        ALTER TABLE run_record ADD COLUMN processing_width INTEGER;
        ALTER TABLE run_record ADD COLUMN processing_height INTEGER;
        ALTER TABLE run_record ADD COLUMN fps INTEGER;
        ALTER TABLE run_record ADD COLUMN preprocess_batch_size INTEGER;
        """,
    ),
    # Which rows a person here has changed since the registry last answered for
    # them. Until now that was inferred from updated_at against the push
    # watermark, which cannot tell a row a pull has just written from one
    # somebody typed: every pulled row looked like a local edit, and every sync
    # of a quiet survey reported conflicts nobody had caused.
    #
    # Sites and campaigns get no trigger. They only ever come down, and the
    # registry refuses them on the way back, so a mark on one would name a debt
    # that could never be paid.
    Migration(
        15,
        "local edits are marked rather than inferred from their stamps",
        """
        CREATE TABLE pending_push (
            section TEXT NOT NULL,
            row_id TEXT NOT NULL,
            PRIMARY KEY (section, row_id)
        );
        """
        + _pending_push_triggers(_PENDING_TABLES_15)
        + _pending_push_backfill(_PENDING_TABLES_15),
    ),
    # The registry position a row was last seen at, which a push sends back as
    # base_seq so the registry can tell what this laptop changed. Sites and
    # campaigns can be made here now, so they are marked like the rest.
    Migration(
        16,
        "rows carry the registry position they were last seen at",
        """
        ALTER TABLE site ADD COLUMN head_seq INTEGER;
        ALTER TABLE campaign ADD COLUMN head_seq INTEGER;
        ALTER TABLE transect ADD COLUMN head_seq INTEGER;
        ALTER TABLE video_asset ADD COLUMN head_seq INTEGER;
        ALTER TABLE transect_pass ADD COLUMN head_seq INTEGER;
        ALTER TABLE run_record ADD COLUMN head_seq INTEGER;
        """
        + _pending_push_triggers({"sites": "site", "campaigns": "campaign"}),
    ),
    # The catalogue as the field data needs it, matching the registry: a site name
    # unique within its country, a transect that may lack GPS ends, a pass whose
    # direction may be unrecorded and which knows the day it was swum, a clip that
    # knows its camera and its review, a run that knows its scale. Every table
    # carries the console's validation stamp. created_by was never read.
    #
    # SQLite cannot relax NOT NULL or drop a CHECK in place, so transect and
    # transect_pass are rebuilt, and their pending-push triggers with them.
    Migration(
        17,
        "the catalogue carries validation, clip review, run scale and optional ends",
        """
        DROP INDEX IF EXISTS site_name_lower;
        CREATE UNIQUE INDEX site_country_name_lower
            ON site(LOWER(COALESCE(country, '')), LOWER(name)) WHERE deleted_at IS NULL;
        ALTER TABLE site ADD COLUMN validated_at TEXT;
        ALTER TABLE site ADD COLUMN validated_by TEXT;
        ALTER TABLE site DROP COLUMN created_by;
        ALTER TABLE campaign ADD COLUMN validated_at TEXT;
        ALTER TABLE campaign ADD COLUMN validated_by TEXT;
        ALTER TABLE campaign DROP COLUMN created_by;

        CREATE TABLE transect_new (
            id TEXT PRIMARY KEY,
            site_id TEXT REFERENCES site(id),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            start_lat REAL,
            start_lon REAL,
            start_accuracy_m REAL,
            end_lat REAL,
            end_lon REAL,
            end_accuracy_m REAL,
            length_m REAL,
            depth_m REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            device_id TEXT,
            head_seq INTEGER,
            validated_at TEXT,
            validated_by TEXT
        );
        INSERT INTO transect_new
            (id, site_id, name, description, start_lat, start_lon, start_accuracy_m,
             end_lat, end_lon, end_accuracy_m, length_m, depth_m, created_at,
             updated_at, deleted_at, device_id, head_seq)
            SELECT id, site_id, name, description, start_lat, start_lon, start_accuracy_m,
                   end_lat, end_lon, end_accuracy_m, length_m, depth_m, created_at,
                   updated_at, deleted_at, device_id, head_seq
            FROM transect;
        DROP TABLE transect;
        ALTER TABLE transect_new RENAME TO transect;
        CREATE UNIQUE INDEX transect_site_name_lower
            ON transect(site_id, LOWER(name)) WHERE deleted_at IS NULL;

        CREATE TABLE transect_pass_new (
            id TEXT PRIMARY KEY,
            transect_id TEXT REFERENCES transect(id),
            campaign_id TEXT REFERENCES campaign(id),
            video_id TEXT NOT NULL REFERENCES video_asset(id),
            batch_id TEXT REFERENCES survey_batch(id),
            direction TEXT CHECK (direction IS NULL OR direction IN ('forward', 'reverse')),
            begin_s REAL NOT NULL,
            end_s REAL NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            quality TEXT CHECK (quality IS NULL OR quality IN
                ('excellent', 'very_good', 'good', 'meh', 'bad', 'very_bad')),
            surveyed_on TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            device_id TEXT,
            extra_video_ids TEXT NOT NULL DEFAULT '[]',
            head_seq INTEGER,
            validated_at TEXT,
            validated_by TEXT
        );
        INSERT INTO transect_pass_new
            (id, transect_id, campaign_id, video_id, batch_id, direction, begin_s, end_s,
             label, notes, quality, created_at, updated_at, deleted_at, device_id,
             extra_video_ids, head_seq)
            SELECT id, transect_id, campaign_id, video_id, batch_id, direction, begin_s,
                   end_s, label, notes, quality, created_at, updated_at, deleted_at,
                   device_id, extra_video_ids, head_seq
            FROM transect_pass;
        DROP TABLE transect_pass;
        ALTER TABLE transect_pass_new RENAME TO transect_pass;

        ALTER TABLE video_asset ADD COLUMN camera_label TEXT;
        ALTER TABLE video_asset ADD COLUMN rig_position TEXT
            CHECK (rig_position IS NULL OR rig_position IN ('left', 'centre', 'right'));
        ALTER TABLE video_asset ADD COLUMN upside_down INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE video_asset ADD COLUMN review TEXT NOT NULL DEFAULT 'unreviewed'
            CHECK (review IN ('unreviewed', 'usable', 'excluded'));
        ALTER TABLE video_asset ADD COLUMN notes TEXT NOT NULL DEFAULT '';
        ALTER TABLE video_asset ADD COLUMN validated_at TEXT;
        ALTER TABLE video_asset ADD COLUMN validated_by TEXT;
        ALTER TABLE video_asset DROP COLUMN created_by;

        ALTER TABLE run_record ADD COLUMN camera_profile TEXT;
        ALTER TABLE run_record ADD COLUMN pixel_size_m REAL;
        ALTER TABLE run_record ADD COLUMN scale_type TEXT;
        ALTER TABLE run_record ADD COLUMN transect_length_m REAL;
        ALTER TABLE run_record ADD COLUMN crop_width_m REAL;
        ALTER TABLE run_record ADD COLUMN preset_id TEXT;
        ALTER TABLE run_record ADD COLUMN validated_at TEXT;
        ALTER TABLE run_record ADD COLUMN validated_by TEXT;
        ALTER TABLE run_record DROP COLUMN created_by;

        ALTER TABLE server_preset DROP COLUMN created_by;
        """
        + _pending_push_triggers({"transects": "transect", "passes": "transect_pass"}),
    ),
    Migration(
        18,
        "presets carry the registry position they were last seen at",
        "ALTER TABLE server_preset ADD COLUMN head_seq INTEGER;",
    ),
    Migration(
        19,
        "a session is identified by when it began, not by a name",
        # A session is this workstation's queue and never reaches the registry,
        # so nothing outside the cart ever read the name. Dropping the column
        # discards a hand-typed one; the default was always the day's date, so
        # for almost every session the label is unchanged.
        "ALTER TABLE survey_batch DROP COLUMN name;",
    ),
    Migration(
        20,
        "a transect records the depth at each end",
        """
        ALTER TABLE transect ADD COLUMN start_depth_m REAL;
        ALTER TABLE transect ADD COLUMN end_depth_m REAL;
        """,
    ),
    # The registry's camera profiles and their calibrations, pulled whole the way
    # presets are. Never authored here: a laptop publishes what it calibrated
    # through the registry's upload endpoint and reads it back on the next pull.
    Migration(
        21,
        "the registry's camera profiles are pulled into their own tables",
        """
        CREATE TABLE camera_profile (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            device_id TEXT,
            head_seq INTEGER
        );

        CREATE TABLE camera_calibration (
            id TEXT PRIMARY KEY,
            camera_profile_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            document TEXT NOT NULL DEFAULT '{}',
            image_width INTEGER,
            image_height INTEGER,
            reprojection_error_px REAL,
            registered_frames INTEGER,
            source_clip TEXT NOT NULL DEFAULT '',
            calibrated_at TEXT,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            device_id TEXT,
            head_seq INTEGER
        );
        CREATE INDEX camera_calibration_profile_idx
            ON camera_calibration (camera_profile_id);
        """,
    ),
    # Which measurement of the lens a run was rectified with, where the device
    # knew one. The run directory carries the document itself.
    Migration(
        22,
        "a run names the calibration it was rectified with",
        "ALTER TABLE run_record ADD COLUMN camera_calibration_id TEXT;",
    ),
    # Which calibration the registry deploys for a rig. Null follows the newest,
    # which is what a profile no curator has deployed has always done.
    Migration(
        23,
        "a camera profile names the calibration it deploys",
        "ALTER TABLE camera_profile ADD COLUMN current_calibration_id TEXT;",
    ),
    Migration(
        24,
        "run performance observations",
        "ALTER TABLE run_record ADD COLUMN performance_observation TEXT;",
    ),
]


def _one_second_later(stamp: str) -> str:
    """The next second, in whatever shape the stamp arrived in.

    An unparseable stamp is suffixed instead, so a session rebuilt from a
    manifest that held something unexpected still ends up with its own label.
    """
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return f"{stamp} "
    return (moment + timedelta(seconds=1)).isoformat(timespec="seconds")


# The registry's sections and the table behind each, in the order a push
# document must present them. pass_video and cover_row are absent on purpose:
# this side keeps a pass's chapters on the pass and its cover in the run
# directory, so the sync layer derives those two rather than reading a table.
SYNC_SECTIONS: dict[str, str] = {
    "sites": "site",
    "campaigns": "campaign",
    "transects": "transect",
    "videos": "video_asset",
    "passes": "transect_pass",
    # Pull-only, ahead of the runs that name the preset they ran under. Never
    # authored here: the contract keeps it out of the push sections.
    "presets": "server_preset",
    "camera_profiles": "camera_profile",
    "camera_calibrations": "camera_calibration",
    "runs": "run_record",
}

_TOMBSTONED_TABLES = frozenset(SYNC_SECTIONS.values())

_SYNC_MODELS: dict[str, type] = {
    "site": Site,
    "campaign": Campaign,
    "transect": Transect,
    "video_asset": VideoAsset,
    "transect_pass": TransectPass,
    "run_record": RunRecord,
    "server_preset": ServerPreset,
    "camera_profile": CameraProfile,
    "camera_calibration": CameraCalibration,
}

# Which attribute of a row names which parent section, for building a closed push
# document. extra_video_ids is in there with video_id: on the wire both become
# pass_video rows, and every chapter is a parent the registry has to hold first.
_SYNC_PARENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "sites": (),
    "campaigns": (),
    "transects": (("site_id", "sites"),),
    "videos": (),
    "passes": (
        ("transect_id", "transects"),
        ("campaign_id", "campaigns"),
        ("video_id", "videos"),
        ("extra_video_ids", "videos"),
    ),
    "runs": (("pass_id", "passes"),),
    "presets": (),
}

# What a pulled row cannot carry and the model has no default for. The registry
# does not hold a path, and a clip this device has never seen has no location.
# What a registry row that cannot be built raises. AttributeError is in here for
# an explicit null in a field a model calls a string method on.
_UNREADABLE_ROW = (TypeError, ValueError, KeyError, AttributeError)

_WIRE_DEFAULTS: dict[str, dict[str, Any]] = {"videos": {"path": ""}}


def latest_schema_version() -> int:
    """The version this build writes, and the highest it can read."""
    return max([SCHEMA_VERSION, *(m.version for m in _MIGRATIONS)])


def oldest_supported_version() -> int:
    """The lowest version this build can carry forward."""
    return _OLDEST_CARRIED


def can_open(version: int) -> bool:
    """Whether a database stamped at this version has a route to the present.

    The one definition of openable, so health checks and recovery cannot drift
    apart from what the store will actually do. Version 0 is a file sqlite has
    only just brought into being, which the baseline writes whole.
    """
    return version == 0 or _OLDEST_CARRIED <= version <= latest_schema_version()


def _no_route_forward(version: int) -> str:
    """Why a database cannot be opened, and the one thing that would help.

    Imported here rather than at module scope: schema_history reads this
    module's version numbers, so the dependency only runs one way at import.
    """
    from deepreefmap_gui.survey.schema_history import newest_release_reading

    opener = newest_release_reading(version)
    advice = (
        f" Open it once with {opener.version}, which brings it to "
        f"v{opener.reads_up_to}."
        if opener is not None
        else ""
    )
    return (
        f"survey.db is schema v{version}, which this build cannot carry forward "
        f"(it reads v{_OLDEST_CARRIED} and up).{advice}"
    )


def resolved_path(path: str) -> str | None:
    """A path in the one form two records of the same file can be compared in.

    Never touches the filesystem: resolve(strict=False) is pure string work, so
    a clip on an unplugged drive still compares equal to itself. Returns None
    for an empty path, which is not a location and must not match another.
    """
    if not path:
        return None
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return path


def _section_of(name: str) -> str:
    """The section behind either a section name or the table it is stored in."""
    if name in SYNC_SECTIONS:
        return name
    for section, table in SYNC_SECTIONS.items():
        if table == name:
            return section
    raise KeyError(f"{name!r} is not a syncable section or table")


def _live(table: str) -> str:
    """The tombstone filter for a table, ready to AND into a WHERE clause.

    Sessions, cart rows and the notification log never sync, so they are deleted
    outright and have nothing to filter. The constant keeps the generic readers
    from having to know which kind of table they were handed.
    """
    return "deleted_at IS NULL" if table in _TOMBSTONED_TABLES else "1 = 1"


def _from_wire(section: str, incoming: Any) -> tuple[dict[str, Any], set[str]]:
    """A pulled row as a plain dict, with the names of the fields it carried.

    A mapping is what a pull returns. A model is what a re-push or a test hands
    over, and every one of its fields counts as carried.
    """
    if not isinstance(incoming, Mapping):
        return to_row(incoming), {f.name for f in fields(incoming)}
    carried = {name for name in incoming if name != "server_seq"}
    return (
        {**_WIRE_DEFAULTS.get(section, {}), **{k: incoming[k] for k in carried}},
        carried,
    )


# How far ahead of this machine's clock an incoming stamp may sit. Generous,
# because it only has to catch stamps that would win every comparison forever,
# not ordinary skew the registry already clamps on push.
_STAMP_TOLERANCE = timedelta(hours=24)


def _check_stamp(value: Any) -> None:
    """Refuse an ``updated_at`` that would break last-write-wins.

    Stamps are compared as strings, so one that does not parse, or one from the
    far future, would beat every honest stamp on every sync. Raises ValueError,
    which the apply loop records as an unreadable row.
    """
    if value is None:
        return
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment > datetime.now(timezone.utc) + _STAMP_TOLERANCE:
        raise ValueError(f"updated_at {value!r} is in the future")


def _ids_of(value: Any) -> list[uuid.UUID]:
    """Every id a foreign-key attribute names: none, one, or a list of them."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _next_second(stamp: str) -> str:
    """One second past a stamp, or the stamp unchanged if it cannot be read."""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment + timedelta(seconds=1)).isoformat(timespec="seconds")


def _insert_sql(table: str, row: dict[str, Any]) -> str:
    columns = ", ".join(row)
    params = ", ".join(f":{c}" for c in row)
    return f"INSERT INTO {table} ({columns}) VALUES ({params})"


def _update_sql(table: str, row: dict[str, Any]) -> str:
    sets = ", ".join(f"{c} = :{c}" for c in row if c != "id")
    return f"UPDATE {table} SET {sets} WHERE id = :id"


def _patch_sql(
    table: str, row: dict[str, Any], carried: set[str]
) -> tuple[str, dict[str, Any]]:
    """An update over the fields a pulled row actually carried, and nothing else.

    The device-local columns a registry has never held -- a clip's path, a run's
    session -- survive an update this way, rather than being written back as the
    defaults the wire supplied for them.
    """
    patch = {k: v for k, v in row.items() if k in carried}
    patch["id"] = row["id"]
    patch["updated_at"] = row["updated_at"]
    return _update_sql(table, patch), patch


def _row_name(row: Mapping[str, Any]) -> str:
    """What to call a row in a message: its name where it has one, else its id."""
    return str(row.get("name") or row["id"])


def _blocked_only_by_each_other(
    conn: sqlite3.Connection, table: str, refused: Sequence[_Landing]
) -> list[_Landing]:
    """The refused rows whose way is blocked by nothing but the others.

    Follow what holds each row's name. A chain that ends at a line no row here
    is rewriting cannot be freed by anything on the page, and every row along it
    is stuck behind that same line, so the chain drops out a link at a time.
    What survives is a permutation of names, which lands once they are parked
    out of the way first.
    """
    holders = {
        str(landing.row_id): SurveyStore._name_holder(conn, table, landing.row)
        for landing in refused
    }
    cycling = set(holders)
    while True:
        stuck = {
            key
            for key, holder in holders.items()
            if key in cycling and (holder is None or holder not in cycling)
        }
        if not stuck:
            break
        cycling -= stuck
    return [landing for landing in refused if str(landing.row_id) in cycling]


# What each table's unique index is fought over: the columns that scope the name,
# with the name itself compared case-insensitively. Every one of them is partial
# on ``deleted_at IS NULL``, so a tombstone collides with nothing. A table absent
# from here carries no unique index a pulled row can land on.
_UNIQUE_NAME_SCOPE: dict[str, tuple[str, ...]] = {
    "site": ("country",),
    "campaign": (),
    "transect": ("site_id",),
}


@dataclass
class _Landing:
    """One pulled row's write, kept so a refused name can be offered again.

    A page carries whatever a curator did between two syncs, and two of those
    things are ordinary: renaming a line to a name another line is about to give
    up, and swapping two names outright. Neither can land on the first attempt in
    every order they can arrive in, so the statement is built once and re-run.
    """

    row_id: uuid.UUID
    row: dict[str, Any]
    statement: str
    values: dict[str, Any]
    inserting: bool

    @property
    def carries_a_name(self) -> bool:
        """Whether this write sets the name, which is what makes parking it safe.

        A row that collided on the site it moved to rather than on the name it
        carries would keep the parked value, since the write that follows never
        touches the column.
        """
        return "name" in self.values


@dataclass(frozen=True)
class Collision:
    """A pulled row this database would not take.

    ``name`` is what the operator calls it, so a report can say which line
    rather than which id. The row is kept whole because the pull cursor steps
    past it for good: this copy is the only one left of what the registry sent.

    ``holder`` is the live row already carrying the name, and None where the
    database refused the row for some other reason of its own -- a column it
    left empty, a value outside an allowed set. Both are set aside the same way,
    and they are not the same thing to tell somebody: only one of them is fixed
    by renaming something.
    """

    section: str
    row_id: uuid.UUID
    name: str
    row: dict[str, Any]
    holder: uuid.UUID | None = None


@dataclass
class ApplyResult:
    """What one pulled section did on landing, for the caller's report."""

    received: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: list[uuid.UUID] = field(default_factory=list)
    # Rows no model could be built from, as (what names the row, what went wrong).
    # The id where the row carried a readable one, section and index where it did
    # not.
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    # Rows a unique index refused. Not applied, not lost, and never retried: the
    # local row holding that name is not going away by itself.
    collided: list[Collision] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return self.inserted + self.updated


@dataclass
class RebuildReport:
    sites: int = 0
    campaigns: int = 0
    transects: int = 0
    videos: int = 0
    batches: int = 0
    passes: int = 0
    runs: int = 0
    skipped: list[str] = field(default_factory=list)


class SurveyStore:
    """Thread-confined sqlite access: each thread gets its own connection.

    The GUI thread and the batch worker only run short transactions, so WAL plus
    a busy timeout is all the coordination needed.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        # The newest stamp this store has issued, shared across its threads so
        # two of them cannot walk backwards past each other. See _stamp.
        self._stamp_lock = threading.Lock()
        self._issued = ""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        # A store is opened once per output root, on the GUI thread, before any
        # batch touches it, so opening is the one moment where every non-terminal
        # row is certain to be a leftover rather than live work. The count is
        # kept because the window reports it, and this is the only moment it can
        # be told apart from a run that is genuinely under way.
        self.interrupted_at_open = self.reconcile_interrupted_runs()
        self.prune_notifications()

    @property
    def path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        """Close the calling thread's connection (worker threads close their own)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One transaction over everything run inside it, nestable.

        sqlite3 commits when a connection used as a context manager exits, so
        two nested ``with conn:`` blocks silently split what should be one
        atomic write. A depth counter keeps the outermost block the only one
        that commits or rolls back, which is what lets a whole pulled page --
        every section, the held rows, the cursor -- land or vanish together.
        """
        conn = self._conn()
        depth = getattr(self._local, "tx_depth", 0)
        if depth:
            self._local.tx_depth = depth + 1
            try:
                yield conn
            finally:
                self._local.tx_depth = depth
            return
        self._local.tx_depth = 1
        try:
            with conn:
                yield conn
        finally:
            self._local.tx_depth = 0

    def _migrate(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA journal_mode = WAL")
        # Foreign keys off for the duration. A migration that rebuilds a table
        # (the only way SQLite can relax a NOT NULL) has to drop it while
        # run_record still references it, and the pragma is a no-op inside a
        # transaction, so it has to be set out here.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._run_migrations(conn)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        # Logged, never raised. Orphan rows predate the migration -- a database
        # written with foreign keys off can carry them -- and refusing to open a
        # survey over one would strand a field laptop on the thing least worth
        # stopping for. The log is enough to find it.
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            logger.warning(
                "survey.db has %d row(s) referencing something that is not there", len(broken)
            )

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        latest = latest_schema_version()
        # A database stamped newer than this build knows must not be opened: it
        # would run nothing and then read a schema whose columns this code does
        # not understand. This happens after an update is rolled back, so it
        # needs to say what to do, not fail obscurely later.
        if version > latest:
            raise RuntimeError(
                f"survey.db is schema v{version}, but this build knows up to "
                f"v{latest}. Update the app to open this survey."
            )
        if version == latest:
            return
        if version == 0:
            # The database _conn just brought into being: nothing to protect,
            # and the baseline writes the schema whole.
            self._apply(conn, _BASELINE, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        elif version < SCHEMA_VERSION:
            carry = _CARRY_FORWARD.get(version)
            if carry is None:
                # Nothing has been written and no backup taken: a refusal must
                # leave the database exactly as it was found, and must not drop
                # a .bak beside it that no build can use.
                raise RuntimeError(_no_route_forward(version))
            self._backup_before_migrating(version)
            self._apply(conn, carry, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        else:
            self._backup_before_migrating(version)
        for migration in _MIGRATIONS:
            if migration.version > version:
                self._apply(conn, migration.sql, migration.version)

    @staticmethod
    def _apply(conn: sqlite3.Connection, script: str, version: int) -> None:
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")

    def _backup_before_migrating(self, version: int) -> None:
        """Copy the database in the shape the previous build wrote it.

        Migrating is one-way -- an older build refuses a database stamped past
        what it knows -- so this copy is what a rolled-back update restores,
        instead of piecing the survey back together from run manifests. A backup
        that cannot be taken is not a reason to refuse to migrate; write_backup
        logs and returns None.
        """
        write_backup(self._db_path, version)

    def _watermark_floor(self) -> str:
        """The newest stamp the registry has already accepted from this device.

        Nothing minted here may land at or under it: a push document is built
        from the rows stamped past the watermark, so such a row is one no push
        would ever offer again.
        """
        row = self._conn().execute(
            "SELECT MAX(value) AS newest FROM sync_state WHERE key LIKE ?",
            (f"{WATERMARK_PREFIX}%",),
        ).fetchone()
        return "" if row is None else (row["newest"] or "")

    def _stamp(self) -> str:
        """A stamp for a local edit, which never runs backwards.

        Stamps decide last-write-wins and the push watermark is one of them, so a
        laptop whose clock jumps back would write rows underneath the watermark
        and quietly drop them from every push that followed. A clock behind the
        watermark gets the second after it instead, which keeps each edit moving
        forwards until the real clock catches up.
        """
        with self._stamp_lock:
            stamp = utc_now_iso()
            floor = self._watermark_floor()
            if floor and stamp <= floor:
                stamp = _next_second(floor)
            self._issued = max(stamp, self._issued)
            return self._issued

    def _add(self, table: str, model: Any) -> None:
        row = to_row(model)
        floor = self._watermark_floor() if table in _TOMBSTONED_TABLES else ""
        if floor and str(row["updated_at"]) <= floor:
            # Moved forward, never replaced: a caller that set a stamp on purpose
            # keeps it, and a clock that has jumped back cannot file a new row
            # under the push watermark where nothing would offer it again.
            row["updated_at"] = model.updated_at = self._stamp()
        with self._conn() as conn:
            conn.execute(_insert_sql(table, row), row)

    def _update(self, table: str, model: Any) -> None:
        row = to_row(model)
        with self.transaction() as conn:
            cursor = conn.execute(_update_sql(table, row), row)
        if cursor.rowcount == 0:
            raise KeyError(f"No {table} row with id {row['id']}")

    def _get(self, table: str, cls: type, item_id: uuid.UUID) -> Any:
        row = self._conn().execute(
            f"SELECT * FROM {table} WHERE id = ? AND {_live(table)}", (str(item_id),)
        ).fetchone()
        return from_row(cls, row) if row is not None else None

    def _list(self, table: str, cls: type, order_by: str) -> list[Any]:
        rows = self._conn().execute(
            f"SELECT * FROM {table} WHERE {_live(table)} ORDER BY {order_by}"
        ).fetchall()
        return [from_row(cls, r) for r in rows]

    def _tombstone(self, table: str, item_id: uuid.UUID) -> None:
        """Stamp a row deleted and leave it where it is.

        A replicated row is deleted by being marked, never removed: a hard delete
        leaves nothing to push, and the next pull would bring the row back.
        Re-deleting an already tombstoned row is a no-op, so the original stamp
        stands rather than being moved forward.
        """
        now = self._stamp()
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {table} SET deleted_at = ?, updated_at = ? "
                f"WHERE id = ? AND deleted_at IS NULL",
                (now, now, str(item_id)),
            )

    def holds_id(self, section_or_table: str, item_id: uuid.UUID) -> bool:
        """Whether this id is in the table at all, tombstone included.

        The get_* readers hide a tombstone, so anything that inserts a row under
        an id it was given -- restoring from a manifest, adopting an orphan run --
        has to ask this instead, or it collides on the primary key.
        """
        table = SYNC_SECTIONS[_section_of(section_or_table)]
        row = self._conn().execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (str(item_id),)
        ).fetchone()
        return row is not None

    # --- Sites ---

    def add_site(self, site: Site) -> None:
        self._add("site", site)

    def update_site(self, site: Site) -> None:
        site.updated_at = self._stamp()
        self._update("site", site)

    def get_site(self, site_id: uuid.UUID) -> Site | None:
        return self._get("site", Site, site_id)

    def list_sites(self) -> list[Site]:
        return self._list("site", Site, "name")

    # --- Campaigns ---

    def add_campaign(self, campaign: Campaign) -> None:
        self._add("campaign", campaign)

    def update_campaign(self, campaign: Campaign) -> None:
        campaign.updated_at = self._stamp()
        self._update("campaign", campaign)

    def get_campaign(self, campaign_id: uuid.UUID) -> Campaign | None:
        return self._get("campaign", Campaign, campaign_id)

    def get_campaign_for_reference(self, campaign_id: uuid.UUID) -> Campaign | None:
        """The trip a pass was recorded on, retired or not.

        The counterpart of get_transect_for_reference, for the same reason: a
        pass filed against a campaign the registry has since withdrawn still
        says which trip it belongs to, and a row that answered None would print
        as unfiled work.
        """
        return self._row_including_deleted("campaign", campaign_id)

    def list_campaigns(self) -> list[Campaign]:
        # Newest expedition first: the one being worked is the one just begun.
        return self._list("campaign", Campaign, "begin_date DESC, name")

    def default_campaign_id(self) -> uuid.UUID | None:
        """The expedition new sections are filed under, until it is changed.

        A working default rather than a per-section question: a trip files weeks
        of sections under one campaign, so it is asked once and remembered here,
        beside the survey it describes. A campaign that has since been withdrawn
        answers None rather than attributing new work to a tombstone.
        """
        stored = self.sync_state(DEFAULT_CAMPAIGN_KEY)
        if stored is None:
            return None
        try:
            campaign_id = uuid.UUID(stored)
        except ValueError:
            return None
        campaign = self.get_campaign(campaign_id)
        return campaign_id if campaign is not None and campaign.deleted_at is None else None

    def set_default_campaign(self, campaign_id: uuid.UUID | None) -> None:
        self.set_sync_state(
            DEFAULT_CAMPAIGN_KEY, None if campaign_id is None else str(campaign_id)
        )

    # --- Transects ---

    def add_transect(self, transect: Transect) -> None:
        self._refuse_a_taken_name(transect)
        self._add("transect", transect)

    def update_transect(self, transect: Transect) -> None:
        self._refuse_a_taken_name(transect)
        transect.updated_at = self._stamp()
        self._update("transect", transect)

    def _refuse_a_taken_name(self, transect: Transect) -> None:
        """Refuse a name a live transect on the same site already carries.

        transect_site_name_lower cannot say this. It is the registry's index, and
        there a site of NULL differs from every other NULL, so two unassigned
        lines called T1 satisfy it -- which they must, or a pull carrying both
        could not land. The name a person is typing is a different question, and
        it is answered here. A tombstone is not asking.
        """
        if transect.deleted_at is not None:
            return
        row = self._conn().execute(
            "SELECT 1 FROM transect WHERE id != ? AND deleted_at IS NULL "
            "AND site_id IS ? AND LOWER(name) = LOWER(?)",
            (
                str(transect.id),
                None if transect.site_id is None else str(transect.site_id),
                transect.name,
            ),
        ).fetchone()
        if row is not None:
            raise sqlite3.IntegrityError(f"transect name {transect.name!r} is taken on this site")

    def delete_transect(self, transect_id: uuid.UUID) -> None:
        """Tombstone a transect that nothing was swum against.

        The passes are the record of what was swum here, and a run manifest
        already written names this transect, so a transect that still has one is
        refused. Asked of the passes rather than of a foreign key: the row stays,
        so the constraint that used to answer this never fires.
        """
        if self.list_passes(transect_id=transect_id):
            raise ValueError(
                "This transect has passes recorded against it and cannot be deleted."
            )
        self._tombstone("transect", transect_id)

    def get_transect(self, transect_id: uuid.UUID) -> Transect | None:
        """A transect somebody may choose, which a retired one is not."""
        return self._get("transect", Transect, transect_id)

    def get_transect_for_reference(self, transect_id: uuid.UUID) -> Transect | None:
        """The line a pass was swum on, retired or not.

        Two questions were being answered by one method. "Which transects may a
        person pick?" hides a retired one, and rightly. "What line was this pass
        swum on?" cannot: the tape length is what scales the reconstruction and
        the identity is what the manifest records. delete_transect refuses to
        retire a line that has passes, but a tombstone pulled from the registry
        lands as a column with no such check, and answering None there turned
        finished field work into unscaled science.
        """
        return self._row_including_deleted("transect", transect_id)

    def list_transects(self) -> list[Transect]:
        return self._list("transect", Transect, "name")

    def list_transects_for_reference(self) -> list[Transect]:
        """Every line with work recorded against it, retired ones included.

        The list form of get_transect_for_reference, and the same division:
        list_transects answers "which line may a person choose". Analysis asks
        the other question, and a swim made before the registry withdrew its line
        is still work that happened. Leaving it out drops finished science from
        the export, from the cover the registry is sent, and from the only view
        the diver has of it.

        A retired line nothing was swum on is left out: it is neither choosable
        nor evidence of anything.
        """
        rows = self._conn().execute(
            """
            SELECT * FROM transect
            WHERE deleted_at IS NULL
               OR id IN (SELECT transect_id FROM transect_pass
                          WHERE transect_id IS NOT NULL AND deleted_at IS NULL)
            ORDER BY name
            """
        ).fetchall()
        return [from_row(Transect, row) for row in rows]

    def transect_usage_counts(self) -> dict[uuid.UUID, tuple[int, int]]:
        """(passes, runs) per transect, for every transect that has either.

        Two grouped queries rather than a count per row: the plan list is
        rebuilt on every keystroke while a transect is being typed.

        A pass need not name a transect, so both queries drop the null group.
        """
        counts: dict[uuid.UUID, tuple[int, int]] = {}
        conn = self._conn()
        for row in conn.execute(
            """
            SELECT transect_id, COUNT(*) AS n
            FROM transect_pass
            WHERE transect_id IS NOT NULL AND deleted_at IS NULL
            GROUP BY transect_id
            """
        ):
            counts[uuid.UUID(row["transect_id"])] = (row["n"], 0)
        for row in conn.execute(
            """
            SELECT transect_pass.transect_id AS transect_id, COUNT(*) AS n
            FROM run_record
            JOIN transect_pass ON transect_pass.id = run_record.pass_id
            WHERE transect_pass.transect_id IS NOT NULL
              AND transect_pass.deleted_at IS NULL
              AND run_record.deleted_at IS NULL
            GROUP BY transect_pass.transect_id
            """
        ):
            transect_id = uuid.UUID(row["transect_id"])
            counts[transect_id] = (counts.get(transect_id, (0, 0))[0], row["n"])
        return counts

    # --- Videos ---

    def upsert_video(self, asset: VideoAsset) -> VideoAsset:
        """Insert ``asset``, or refresh and return the row that is already this clip.

        Matched on the content hash, and on the resolved path when there is no
        hash. The path fallback carries clips that cannot be hashed, off an
        unplugged drive or otherwise unreadable, which would otherwise insert a
        fresh row on every add.

        A path is only ever a fallback: two different files can sit at one path
        over time, so a hash wins when there is one on both sides.
        """
        existing = self.find_video_by_hash(asset.hash) if asset.hash else None
        if existing is None and not asset.hash:
            existing = self.find_video_by_path(asset.path)
            # Only adopt an unhashed row: one that already knows its hash is a
            # different, identified clip that happens to have lived here.
            if existing is not None and existing.hash:
                existing = None
        if existing is None:
            self._add("video_asset", asset)
            return asset
        existing.overlay_from(asset)
        self.update_video(existing)
        return existing

    def find_video_by_hash(
        self, content_hash: str | None, include_deleted: bool = False
    ) -> VideoAsset | None:
        """The clip with this content hash, tombstones excluded by default.

        ``include_deleted`` is for the rebuild path only, which needs the id a
        manifest's clip was filed under so it does not mint a second row for it.
        """
        if not content_hash:
            return None
        live = "" if include_deleted else " AND deleted_at IS NULL"
        row = self._conn().execute(
            f"SELECT * FROM video_asset WHERE hash = ?{live}", (content_hash,)
        ).fetchone()
        return from_row(VideoAsset, row) if row is not None else None

    def find_video_by_path(self, path: str) -> VideoAsset | None:
        """The clip recorded at this path, matched on the resolved location.

        Resolved on both sides so a relative path, a symlinked mount and the
        absolute path of the same file are one clip rather than three.
        """
        wanted = resolved_path(path)
        if wanted is None:
            return None
        for video in self.list_videos():
            if resolved_path(video.path) == wanted:
                return video
        return None

    def get_video(self, video_id: uuid.UUID) -> VideoAsset | None:
        return self._get("video_asset", VideoAsset, video_id)

    def list_videos(self) -> list[VideoAsset]:
        return self._list("video_asset", VideoAsset, "created_at, file_name")

    def update_video(self, asset: VideoAsset) -> None:
        asset.updated_at = self._stamp()
        self._update("video_asset", asset)

    def set_video_hash(self, video_id: object, digest: str) -> None:
        """Record a digest computed elsewhere, without stamping the row edited.

        Identifying a file changes nothing about the clip, so ``updated_at``
        stays: bumping it would queue the row for the next push over a value the
        archive already carries on its own call.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE video_asset SET hash = ? WHERE id = ?", (digest, str(video_id))
            )

    def merge_videos(self, keeper_id: uuid.UUID, loser_ids: list[uuid.UUID]) -> int:
        """Fold duplicate clip rows into one, and repoint what pointed at them.

        Returns the number of passes moved. Both ends of a pass are repointed:
        ``video_id`` and the ``extra_video_ids`` a chaptered recording carries,
        or the second half of a swim the camera split at 4 GB would go on
        naming a row that no longer exists.

        A pass that would end up naming the keeper twice keeps it once. That is
        not hypothetical: a chaptered pass whose chapters were separately
        unhashed has every chapter merging into the same keeper, and
        TransectPass refuses to hold the same video in both places.
        """
        losers = {lid for lid in loser_ids if lid != keeper_id}
        if not losers:
            return 0
        moved = 0
        for pass_ in self.list_passes():
            ids = pass_.video_ids()
            if not losers.intersection(ids):
                continue
            remapped: list[uuid.UUID] = []
            for video_id in ids:
                mapped = keeper_id if video_id in losers else video_id
                if mapped not in remapped:
                    remapped.append(mapped)
            pass_.video_id = remapped[0]
            pass_.extra_video_ids = remapped[1:]
            self.update_pass(pass_)
            moved += 1
        # A tombstone keeps its hash, and every read of the hash is filtered on
        # deleted_at, so re-scanning the same file lands on the keeper.
        now = self._stamp()
        with self._conn() as conn:
            conn.executemany(
                "UPDATE video_asset SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                [(now, now, str(lid)) for lid in losers],
            )
        return moved

    def delete_video(self, video_id: uuid.UUID) -> int:
        """Tombstone a clip, with the sections cut from it.

        Only the record goes: the file on disk is never touched. A run is the
        record of what it processed, so a clip any run reaches is refused
        whole, the way ``delete_pass`` refuses a section with runs.

        A chaptered pass the clip is only one chapter of keeps its other
        chapters and merely loses this one, since the rest of the swim is still
        there to play. Returns the number of passes deleted.
        """
        passes = [p for p in self.list_passes() if video_id in p.video_ids()]
        if any(self.runs_for_pass(p.id) for p in passes):
            raise ValueError("This clip has recorded runs and cannot be removed.")
        deleted = 0
        for pass_ in passes:
            remaining = [vid for vid in pass_.video_ids() if vid != video_id]
            if not remaining:
                self.delete_pass(pass_.id)
                deleted += 1
                continue
            pass_.video_id = remaining[0]
            pass_.extra_video_ids = remaining[1:]
            self.update_pass(pass_)
        self._tombstone("video_asset", video_id)
        return deleted

    # --- Batches ---

    def add_batch(self, batch: SurveyBatch) -> None:
        """Store a session, keeping its start distinct from every other one's.

        The start is the whole of a session's identity now, and a cart minted
        the instant an order begins can land on the same second as it. The later
        one is advanced until it is free, which is what suffixing a repeated
        name used to do: a label two sessions share identifies neither.
        """
        with self._conn() as conn:
            taken = {
                row[0] for row in conn.execute("SELECT created_at FROM survey_batch")
            }
        while batch.created_at in taken:
            batch.created_at = _one_second_later(batch.created_at)
        self._add("survey_batch", batch)

    def get_batch(self, batch_id: uuid.UUID) -> SurveyBatch | None:
        return self._get("survey_batch", SurveyBatch, batch_id)

    def list_batches(self) -> list[SurveyBatch]:
        return self._list("survey_batch", SurveyBatch, "created_at DESC")

    def batch_run_count(self, batch_id: uuid.UUID) -> int:
        """How many runs this session has placed. Zero means it is still a cart."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM run_record "
            "WHERE batch_id = ? AND deleted_at IS NULL",
            (str(batch_id),),
        ).fetchone()
        return row["n"]

    def runs_in_batch(self, batch_id: uuid.UUID) -> list[RunRecord]:
        """Every run the session placed, including ones recorded on the pass only.

        The fallback matches RunEntry.session_id: an early run row carries no
        batch_id of its own and belongs to the session its pass was catalogued
        in. delete_batch removes the same set, so what this lists is what goes.
        """
        rows = self._conn().execute(
            """
            SELECT * FROM run_record
            WHERE deleted_at IS NULL
              AND (batch_id = ?
                   OR (batch_id IS NULL AND pass_id IN
                       (SELECT id FROM transect_pass WHERE batch_id = ?)))
            ORDER BY created_at
            """,
            (str(batch_id), str(batch_id)),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def delete_batch(self, batch_id: uuid.UUID) -> None:
        """Forget a session: its cart rows and run records go, everything shared
        stays. Passes catalogued in it survive with no session of their own, and
        clips and transects are never touched here.

        The session and its cart rows are removed outright, because neither ever
        syncs. Its runs are replicated, so those are tombstoned: the delete has
        to travel, or the next pull hands them back.
        """
        key = str(batch_id)
        now = self._stamp()
        with self._conn() as conn:
            conn.execute("DELETE FROM batch_item WHERE batch_id = ?", (key,))
            conn.execute(
                """
                UPDATE run_record SET deleted_at = ?, updated_at = ?
                WHERE deleted_at IS NULL
                  AND (batch_id = ?
                       OR (batch_id IS NULL AND pass_id IN
                           (SELECT id FROM transect_pass WHERE batch_id = ?)))
                """,
                (now, now, key, key),
            )
            # batch_id is device-local, so releasing a row is not an edit the
            # registry needs to hear about and updated_at stays where it is. The
            # tombstones are released too: the session row is about to go, and
            # nothing may still name it.
            conn.execute("UPDATE run_record SET batch_id = NULL WHERE batch_id = ?", (key,))
            conn.execute(
                "UPDATE transect_pass SET batch_id = NULL WHERE batch_id = ?", (key,)
            )
            conn.execute("DELETE FROM survey_batch WHERE id = ?", (key,))

    def current_cart(self) -> SurveyBatch | None:
        """The newest session, only while it has run nothing.

        Only the newest: an older empty session behind a started one is
        abandoned, not the cart. rowid breaks a same-second created_at tie.
        """
        row = self._conn().execute(
            "SELECT * FROM survey_batch ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        batch = from_row(SurveyBatch, row)
        return batch if self.batch_run_count(batch.id) == 0 else None

    # --- Batch items ---

    def add_batch_item(self, item: BatchItem) -> None:
        """Add a pass to a session's worklist; already a member is a no-op.

        A new row lands at the end of the processing order, which is where a
        thing added to a queue belongs until it is dragged somewhere else.
        """
        row = to_row(item)
        with self._conn() as conn:
            last = conn.execute(
                "SELECT MAX(position) FROM batch_item WHERE batch_id = ?",
                (row["batch_id"],),
            ).fetchone()[0]
            row["position"] = 0 if last is None else int(last) + 1
            conn.execute(
                _insert_sql("batch_item", row).replace("INSERT", "INSERT OR IGNORE", 1), row
            )

    def remove_batch_item(self, batch_id: uuid.UUID, pass_id: uuid.UUID) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM batch_item WHERE batch_id = ? AND pass_id = ?",
                (str(batch_id), str(pass_id)),
            )

    def has_batch_item(self, batch_id: uuid.UUID, pass_id: uuid.UUID) -> bool:
        """Whether this pass is still ordered in this session.

        The worker asks between passes: a row taken out of the cart while the
        session runs must not be processed, and this is what says so.
        """
        row = self._conn().execute(
            "SELECT 1 FROM batch_item WHERE batch_id = ? AND pass_id = ?",
            (str(batch_id), str(pass_id)),
        ).fetchone()
        return row is not None

    def set_batch_item_positions(
        self, batch_id: uuid.UUID, pass_ids: Sequence[uuid.UUID]
    ) -> None:
        """Write the processing order, as the passes are given, from zero."""
        with self._conn() as conn:
            conn.executemany(
                "UPDATE batch_item SET position = ? WHERE batch_id = ? AND pass_id = ?",
                [
                    (index, str(batch_id), str(pass_id))
                    for index, pass_id in enumerate(pass_ids)
                ],
            )

    def set_batch_item_overrides(
        self, batch_id: uuid.UUID, pass_id: uuid.UUID, overrides: Mapping[str, Any]
    ) -> None:
        """Store what this pass alone changes about the session's settings."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE batch_item SET overrides = ? WHERE batch_id = ? AND pass_id = ?",
                (json.dumps(dict(overrides), sort_keys=True), str(batch_id), str(pass_id)),
            )

    def list_batch_items(self, batch_id: uuid.UUID) -> list[BatchItem]:
        # position is the processing order; rowid breaks a tie between rows that
        # were written before there was an order, or in the same drag.
        rows = self._conn().execute(
            "SELECT * FROM batch_item WHERE batch_id = ? ORDER BY position, rowid",
            (str(batch_id),),
        ).fetchall()
        return [from_row(BatchItem, r) for r in rows]

    def list_all_batch_items(self) -> list[BatchItem]:
        rows = self._conn().execute("SELECT * FROM batch_item ORDER BY rowid").fetchall()
        return [from_row(BatchItem, r) for r in rows]

    def passes_in_batch(self, batch_id: uuid.UUID) -> list[TransectPass]:
        """The session's worklist, in the order it will be processed."""
        rows = self._conn().execute(
            """
            SELECT transect_pass.* FROM transect_pass
            JOIN batch_item ON batch_item.pass_id = transect_pass.id
            WHERE batch_item.batch_id = ? AND transect_pass.deleted_at IS NULL
            ORDER BY batch_item.position, batch_item.rowid
            """,
            (str(batch_id),),
        ).fetchall()
        return [from_row(TransectPass, r) for r in rows]

    # --- Passes ---

    def add_pass(self, pass_: TransectPass) -> None:
        self._add("transect_pass", pass_)

    def pass_with_window(
        self, video_id: uuid.UUID, begin_s: float, end_s: float | None
    ) -> TransectPass | None:
        """An existing section of this clip covering the same window, if there is one.

        A guard for the cut, not for ``add_pass``: restoring a run's pass from a
        manifest has to insert whatever window that run processed, duplicate or
        not, or the run is left describing a section nothing records. Windows are
        compared to a tenth of a second, well under a frame at any usable fps.
        """
        for stored in self.list_passes(video_id=video_id):
            if abs(stored.begin_s - begin_s) > 0.1:
                continue
            if stored.end_s is None or end_s is None:
                if stored.end_s is end_s:
                    return stored
                continue
            if abs(stored.end_s - end_s) <= 0.1:
                return stored
        return None

    def update_pass(self, pass_: TransectPass) -> None:
        pass_.updated_at = self._stamp()
        self._update("transect_pass", pass_)

    def set_pass_chapters(self, pass_: TransectPass) -> None:
        """Write a pass's chapter list exactly as given, keeping its stamp.

        A chapter order adopted from a pull is the registry's own data, not a
        local edit: stamping it would mark the pass pending and re-push an
        unchanged row on every sync.
        """
        self._update("transect_pass", pass_)

    def delete_pass(self, pass_id: uuid.UUID) -> None:
        """Tombstone a pass and take out the cart rows that ordered it.

        Only a run stops it. The cart rows go for good rather than being marked:
        they never sync, and the cascade that used to remove them cannot fire on
        a row that stays.
        """
        if self.runs_for_pass(pass_id):
            raise ValueError("This pass has recorded runs and cannot be removed.")
        with self._conn() as conn:
            conn.execute("DELETE FROM batch_item WHERE pass_id = ?", (str(pass_id),))
        self._tombstone("transect_pass", pass_id)

    def get_pass(self, pass_id: uuid.UUID) -> TransectPass | None:
        return self._get("transect_pass", TransectPass, pass_id)

    def list_passes(
        self,
        transect_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
        video_id: uuid.UUID | None = None,
        campaign_id: uuid.UUID | None = None,
    ) -> list[TransectPass]:
        clauses: list[str] = ["deleted_at IS NULL"]
        params: list[str] = []
        if transect_id is not None:
            clauses.append("transect_id = ?")
            params.append(str(transect_id))
        if campaign_id is not None:
            clauses.append("campaign_id = ?")
            params.append(str(campaign_id))
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(str(batch_id))
        if video_id is not None:
            clauses.append("video_id = ?")
            params.append(str(video_id))
        where = " AND ".join(clauses)
        # created_at is second-precision, so passes queued in one action share it.
        # rowid breaks the tie by insertion order, which is the order the user
        # built the table in, and which a pass's first run-dir name is numbered
        # from (later attempts derive from that recorded name).
        rows = self._conn().execute(
            f"SELECT * FROM transect_pass WHERE {where} ORDER BY created_at, rowid", params
        ).fetchall()
        return [from_row(TransectPass, r) for r in rows]

    # --- Runs ---

    def add_run(self, run: RunRecord) -> None:
        self._add("run_record", run)

    def set_run_status(self, run_id: uuid.UUID, status: str, error: str = "") -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {status!r}")
        # updated_at is the sync stamp and takes the guarded one; started_at and
        # finished_at are when the work happened, and a guard that nudged those
        # would make the run list read wrong.
        sets = "status = ?, error = ?, updated_at = ?"
        params: list[Any] = [status, error, self._stamp()]
        if status == "running":
            sets += ", started_at = ?"
            params.append(utc_now_iso())
        elif status in TERMINAL_STATUSES:
            sets += ", finished_at = ?"
            params.append(utc_now_iso())
        params.append(str(run_id))
        with self._conn() as conn:
            cursor = conn.execute(f"UPDATE run_record SET {sets} WHERE id = ?", params)
        if cursor.rowcount == 0:
            raise KeyError(f"No run_record row with id {run_id}")

    def reconcile_interrupted_runs(self) -> int:
        """Mark every non-terminal run row as interrupted; return how many moved.

        A crash or a quit-with-batch-running never gets to stamp a terminal
        status, so a row stays "running" (or "pending") forever, reading as live
        work that blocks nothing from being re-run. Reconciling on open turns
        those into a terminal, non-success state the gate treats as remaining.
        """
        non_terminal = [s for s in RUN_STATUSES if s not in TERMINAL_STATUSES]
        placeholders = ", ".join("?" for _ in non_terminal)
        with self._conn() as conn:
            cursor = conn.execute(
                f"UPDATE run_record SET status = ?, finished_at = ?, error = ?, "
                f"updated_at = ? WHERE deleted_at IS NULL AND status IN ({placeholders})",
                [
                    "interrupted", utc_now_iso(), _INTERRUPTED_REASON, self._stamp(),
                    *non_terminal,
                ],
            )
        if cursor.rowcount:
            logger.info("Reconciled %d interrupted run(s) in %s", cursor.rowcount, self._db_path)
        return cursor.rowcount

    def delete_run(self, run_id: uuid.UUID) -> None:
        """Tombstone a run record.

        The bytes are a separate decision: catalogue.delete_run_data removes the
        directory, and on a full field laptop it still does. A tombstone is about
        the metadata row, not about keeping the output.
        """
        self._tombstone("run_record", run_id)

    def get_run(self, run_id: uuid.UUID) -> RunRecord | None:
        return self._get("run_record", RunRecord, run_id)

    def list_server_presets(self) -> list[ServerPreset]:
        """The registry's presets, newest version of each name first in its group."""
        rows = self._conn().execute(
            "SELECT * FROM server_preset WHERE deleted_at IS NULL "
            "ORDER BY LOWER(name), version DESC"
        ).fetchall()
        return [from_row(ServerPreset, r) for r in rows]

    def get_server_preset(self, name: str, version: int) -> ServerPreset | None:
        row = self._conn().execute(
            "SELECT * FROM server_preset WHERE LOWER(name) = LOWER(?) AND version = ? "
            "AND deleted_at IS NULL",
            (name, int(version)),
        ).fetchone()
        return from_row(ServerPreset, row) if row is not None else None

    def list_camera_profiles(self) -> list[CameraProfile]:
        """The registry's camera profiles, by name."""
        rows = self._conn().execute(
            "SELECT * FROM camera_profile WHERE deleted_at IS NULL ORDER BY LOWER(name)"
        ).fetchall()
        return [from_row(CameraProfile, r) for r in rows]

    def camera_profile_by_name(self, name: str) -> CameraProfile | None:
        """The profile row of this name, tombstoned or not.

        Tombstones included deliberately: a materialised file is only taken back
        on evidence of withdrawal, and the tombstone is that evidence.
        """
        row = self._conn().execute(
            "SELECT * FROM camera_profile WHERE LOWER(name) = LOWER(?) "
            "ORDER BY (deleted_at IS NULL) DESC LIMIT 1",
            (name,),
        ).fetchone()
        return from_row(CameraProfile, row) if row is not None else None

    def camera_calibration(self, calibration_id: uuid.UUID) -> CameraCalibration | None:
        """One live measurement by id, whichever version it is."""
        row = self._conn().execute(
            "SELECT * FROM camera_calibration WHERE id = ? AND deleted_at IS NULL",
            (str(calibration_id),),
        ).fetchone()
        return from_row(CameraCalibration, row) if row is not None else None

    def newest_camera_calibration(self, profile_id: uuid.UUID) -> CameraCalibration | None:
        """The latest measurement of one profile, deployed where none is named."""
        row = self._conn().execute(
            "SELECT * FROM camera_calibration WHERE camera_profile_id = ? "
            "AND deleted_at IS NULL ORDER BY version DESC LIMIT 1",
            (str(profile_id),),
        ).fetchone()
        return from_row(CameraCalibration, row) if row is not None else None

    def record_run_provenance(self, run_id: uuid.UUID, provenance: Mapping[str, Any]) -> None:
        """Copy a finished run's provenance onto its row.

        The manifest stays the source at the moment of writing; the row is the
        durable copy, so pruning the run directory later loses nothing. Unknown
        keys are ignored so a newer manifest never breaks an older build.
        """
        run = self.get_run(run_id)
        if run is None:
            return
        for name, value in provenance.items():
            if hasattr(run, name):
                setattr(run, name, value)
        run.updated_at = self._stamp()
        self._update("run_record", run)

    def run_by_dir_name(self, run_dir_name: str) -> RunRecord | None:
        row = self._conn().execute(
            "SELECT * FROM run_record WHERE run_dir_name = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (run_dir_name,),
        ).fetchone()
        return from_row(RunRecord, row) if row is not None else None

    def runs_for_pass(self, pass_id: uuid.UUID) -> list[RunRecord]:
        rows = self._conn().execute(
            "SELECT * FROM run_record WHERE pass_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at, rowid",
            (str(pass_id),),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def runs_for_transect(self, transect_id: uuid.UUID) -> list[RunRecord]:
        rows = self._conn().execute(
            """
            SELECT run_record.* FROM run_record
            JOIN transect_pass ON transect_pass.id = run_record.pass_id
            WHERE transect_pass.transect_id = ?
              AND transect_pass.deleted_at IS NULL
              AND run_record.deleted_at IS NULL
            ORDER BY run_record.created_at
            """,
            (str(transect_id),),
        ).fetchall()
        return [from_row(RunRecord, r) for r in rows]

    def succeeded_pass_ids(self, batch_id: uuid.UUID | None = None) -> set[uuid.UUID]:
        """Passes with at least one successful run, in one query.

        The Run table asks on every repaint, so it cannot afford a query per
        row. Scoped to a session when ``batch_id`` is given: a pass re-ordered
        in a new cart has succeeded before, but not yet in that session.
        """
        sql = (
            "SELECT DISTINCT pass_id FROM run_record "
            "WHERE status = 'succeeded' AND deleted_at IS NULL"
        )
        params: list[str] = []
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(str(batch_id))
        rows = self._conn().execute(sql, params).fetchall()
        return {uuid.UUID(row["pass_id"]) for row in rows}

    def list_runs(self) -> list[RunRecord]:
        return self._list("run_record", RunRecord, "created_at")

    # --- Notifications ---

    def add_notification(self, note: Notification) -> None:
        self._add("notification", note)

    def update_notification(self, note: Notification) -> None:
        self._update("notification", note)

    def open_notifications(self) -> list[Notification]:
        """Every episode still running, oldest first, so the centre can adopt them."""
        rows = self._conn().execute(
            "SELECT * FROM notification WHERE resolved_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [from_row(Notification, r) for r in rows]

    def list_notifications(
        self, limit: int = 500, severity: str = "", scope: str = ""
    ) -> list[Notification]:
        """The log, newest first."""
        sql = "SELECT * FROM notification WHERE 1 = 1"
        params: list[Any] = []
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [from_row(Notification, r) for r in rows]

    def resolve_notification(self, note_id: uuid.UUID, at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notification SET resolved_at = ?, updated_at = ? WHERE id = ?",
                (at, at, str(note_id)),
            )

    def prune_notifications(self, keep: int = 2000) -> int:
        """Drop all but the newest ``keep`` resolved rows, and return how many went.

        The only table here that grows without anybody asking it to: a condition
        that flickers over a long field season writes an episode each time. Open
        rows are never pruned, however old, because they are still true.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM notification WHERE id IN (
                    SELECT id FROM notification WHERE resolved_at IS NOT NULL
                    ORDER BY created_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (keep,),
            )
        return cursor.rowcount

    # --- Sync ---

    def sync_state(self, key: str) -> str | None:
        """One machine-local sync setting: a cursor, a watermark, a url, an id."""
        row = self._conn().execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def set_sync_state(self, key: str, value: str | None) -> None:
        """Write a sync setting, or forget it when ``value`` is None."""
        with self.transaction() as conn:
            if value is None:
                conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
                return
            conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, utc_now_iso()),
            )

    def pending_push_ids(self, section: str) -> set[uuid.UUID]:
        """Rows a person here has changed since the registry last answered.

        The one answer to "did somebody type this", which a stamp cannot give: a
        pull writes rows stamped newer than anything local, and reading those as
        local edits reported a conflict on every sync of a quiet survey.
        """
        rows = self._conn().execute(
            "SELECT row_id FROM pending_push WHERE section = ?", (_section_of(section),)
        ).fetchall()
        return {uuid.UUID(row["row_id"]) for row in rows}

    def clear_pending_push(self, section: str, ids: Iterable[uuid.UUID]) -> None:
        """Forget the marks on rows the registry has now answered for.

        Answered covers a refusal and a collision as well as a write: all three
        are terminal, and a mark kept over one would re-offer the same row on
        every sync for as long as the survey lasted.
        """
        name = _section_of(section)
        with self.transaction() as conn:
            conn.executemany(
                "DELETE FROM pending_push WHERE section = ? AND row_id = ?",
                [(name, str(row_id)) for row_id in ids],
            )

    def changed_since(self, section: str, since: str | None = None) -> list[Any]:
        """Every row of a section still owed to the registry, tombstones included.

        This is what a push document is built from, so a tombstone has to be
        here: a delete only travels as a row. ``since`` is the local push
        watermark and None means everything. Takes either a section name
        (``passes``) or the table behind it (``transect_pass``).

        Strictly after the watermark, and the pending marks alongside it. The
        watermark is a stamp the registry accepted, so a row carrying it is a row
        it already holds, and an inclusive comparison had a laptop whose clock
        had drifted re-offering the same rows on every sync forever. What the
        boundary used to carry -- an edit made in the second the push was
        accepted in -- is carried explicitly now, by a mark that stands until
        the registry answers for it.
        """
        table = SYNC_SECTIONS[_section_of(section)]
        sql = f"SELECT * FROM {table}"
        params: list[Any] = []
        if since is not None:
            sql += (
                " WHERE updated_at > ? "
                "OR id IN (SELECT row_id FROM pending_push WHERE section = ?)"
            )
            params.extend([since, _section_of(section)])
        rows = self._conn().execute(f"{sql} ORDER BY updated_at, rowid", params).fetchall()
        return [from_row(_SYNC_MODELS[table], r) for r in rows]

    def count_changed_since(self, section: str, since: str | None = None) -> int:
        """How many rows are waiting to push, without loading any of them.

        The same set changed_since builds, counted in SQL: the badge polls this
        on a timer, which is why it must stay a count and never a row load.
        """
        table = SYNC_SECTIONS[_section_of(section)]
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        params: list[Any] = []
        if since is not None:
            sql += (
                " WHERE updated_at > ? "
                "OR id IN (SELECT row_id FROM pending_push WHERE section = ?)"
            )
            params.extend([since, _section_of(section)])
        return int(self._conn().execute(sql, params).fetchone()["n"])

    def apply_from_server(
        self, section: str, rows: Iterable[Mapping[str, Any] | Any]
    ) -> ApplyResult:
        """Land pulled rows: the registry's copy stands unless this laptop owes it one.

        A row that is not here is inserted. A row with a pending mark keeps its
        local edit and takes only the registry position, so the next push is
        judged against it and the registry merges the two. A row already seen at
        this position is skipped. A tombstone lands like any other row, because
        deleted_at is a column. Only the fields the pulled row actually carries
        are written, so the device-local ones -- a clip's path, a run's session --
        survive an update from a server that has never held them.

        Sections have to be applied in SYNC_SECTIONS order: foreign keys are on,
        and a child whose parent has not landed yet is refused.

        A row no model can be built from is named in ``unreadable`` and skipped,
        so the rest of the page lands and the cursor moves on. So is a row whose
        ``updated_at`` does not parse or sits in the future: last-write-wins
        compares stamps as strings, so a garbage or far-future stamp would win
        every comparison forever.

        The two integrity errors are told apart by _missing_parent rather than by
        reading what sqlite wrote in English. A child whose parent has not
        arrived yet takes the page down, because the page is asked for again and
        lands whole once the parent is there. A row a unique index refuses is
        held back and offered again, twice: once after the rest of the page has
        landed, in case a later row freed the name, and once with the group's
        names parked out of the way, which is what lets a swap or a rotation of
        names land. Only what still cannot go in is named in ``collided``.
        """
        section = _section_of(section)
        table = SYNC_SECTIONS[section]
        cls = _SYNC_MODELS[table]
        result = ApplyResult()
        with self.transaction() as conn:
            refused: list[_Landing] = []
            for index, incoming in enumerate(rows):
                result.received += 1
                try:
                    wire, carried = _from_wire(section, incoming)
                    row_id = str(wire["id"])
                    _check_stamp(wire.get("updated_at"))
                except _UNREADABLE_ROW as exc:
                    result.unreadable.append((f"{section}[{index}]", str(exc)))
                    continue
                stored = conn.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (row_id,)
                ).fetchone()
                # Merged over the stored row so the model is validated whole, and
                # so a row that arrives partial says nothing about the rest.
                merged = wire if stored is None else {**dict(stored), **wire}
                try:
                    row = to_row(from_row(cls, merged))
                except _UNREADABLE_ROW as exc:
                    result.unreadable.append((row_id, str(exc)))
                    continue
                if stored is not None and self._keeps_local(conn, section, table, stored, row):
                    result.skipped.append(uuid.UUID(row["id"]))
                    continue
                statement, values = (
                    (_insert_sql(table, row), row)
                    if stored is None
                    else _patch_sql(table, row, carried)
                )
                landing = _Landing(
                    row_id=uuid.UUID(row_id),
                    row=dict(row),
                    statement=statement,
                    values=values,
                    inserting=stored is None,
                )
                if not self._land(conn, table, section, landing, result):
                    refused.append(landing)
            refused = self._land_freed_names(conn, table, section, refused, result)
            refused = self._land_swapped_names(conn, table, section, refused, result)
            for landing in refused:
                holder = self._name_holder(conn, table, landing.row)
                logger.warning(
                    "Setting aside %s %s: %s",
                    section,
                    landing.row_id,
                    "the name is taken here" if holder else "this database refused it",
                )
                result.collided.append(
                    Collision(
                        section,
                        landing.row_id,
                        _row_name(landing.row),
                        landing.row,
                        None if holder is None else uuid.UUID(holder),
                    )
                )
        return result

    @staticmethod
    def _keeps_local(
        conn: sqlite3.Connection,
        section: str,
        table: str,
        stored: sqlite3.Row,
        row: Mapping[str, Any],
    ) -> bool:
        """Whether a stored row stands against the pulled one.

        A local edit still owed to the registry stands, and adopts the pulled
        position so the push it is waiting for is judged against it. Otherwise
        the later registry position wins, and where the two positions are the
        same or unknown, the later stamp.
        """
        incoming = row.get("head_seq")
        pending = conn.execute(
            "SELECT 1 FROM pending_push WHERE section = ? AND row_id = ?",
            (section, str(row["id"])),
        ).fetchone()
        if pending is not None:
            if incoming is not None:
                conn.execute(
                    f"UPDATE {table} SET head_seq = ? WHERE id = ?", (incoming, row["id"])
                )
            return True
        seen = stored["head_seq"]
        if incoming is not None and seen is not None and int(incoming) != int(seen):
            return int(incoming) < int(seen)
        return str(row["updated_at"]) <= str(stored["updated_at"] or "")

    def set_head_seq(self, section: str, positions: Mapping[uuid.UUID, int]) -> None:
        """Record the registry position rows were acknowledged at. Not an edit."""
        table = SYNC_SECTIONS[_section_of(section)]
        with self.transaction() as conn:
            conn.executemany(
                f"UPDATE {table} SET head_seq = ? WHERE id = ?",
                [(seq, str(row_id)) for row_id, seq in positions.items()],
            )

    @staticmethod
    def _land(
        conn: sqlite3.Connection,
        table: str,
        section: str,
        landing: _Landing,
        result: ApplyResult,
    ) -> bool:
        """Write one pulled row, answering False where a unique index refused it.

        A missing parent still raises: the page is asked for again and lands
        whole once the parent is there, while a name this database is already
        using would fail identically on every attempt at that page forever.
        """
        try:
            conn.execute(landing.statement, landing.values)
        except sqlite3.IntegrityError:
            if SurveyStore._missing_parent(conn, table, landing.row):
                raise
            return False
        if landing.inserting:
            result.inserted += 1
        else:
            result.updated += 1
        # The registry's own data, whatever the row was before it: a mark left
        # here would offer the registry back what it just sent.
        conn.execute(
            "DELETE FROM pending_push WHERE section = ? AND row_id = ?",
            (section, str(landing.row_id)),
        )
        return True

    def _land_freed_names(
        self,
        conn: sqlite3.Connection,
        table: str,
        section: str,
        refused: list[_Landing],
        result: ApplyResult,
    ) -> list[_Landing]:
        """Offer the refused rows again until an attempt frees nothing.

        Where a row sits in a page must not decide whether it lands. A page that
        renames B onto a name A holds and then renames A away has freed that
        name by the time it is over, so B is owed the answer it would have had
        if it had arrived second.
        """
        while refused:
            still = [
                landing
                for landing in refused
                if not self._land(conn, table, section, landing, result)
            ]
            if len(still) == len(refused):
                return still
            refused = still
        return refused

    def _land_swapped_names(
        self,
        conn: sqlite3.Connection,
        table: str,
        section: str,
        refused: list[_Landing],
        result: ApplyResult,
    ) -> list[_Landing]:
        """Land the rows that are in nothing's way but each other's.

        A curator swapping two line names is an ordinary morning's work, and it
        arrives as two rows each holding the name the other wants, so neither can
        go first. Their names are parked on their own ids, which the primary key
        guarantees nothing else carries, and then written over. Anything that
        turns out to be blocked from outside the group takes the whole group back
        with it, because a half-applied permutation would leave a line named
        after its id.
        """
        cycling = _blocked_only_by_each_other(conn, table, refused)
        if not cycling:
            return refused
        staged = ApplyResult()
        conn.execute("SAVEPOINT swapped_names")
        landed = self._park_names(conn, table, cycling) and all(
            self._land(conn, table, section, landing, staged) for landing in cycling
        )
        if not landed:
            conn.execute("ROLLBACK TO swapped_names")
            conn.execute("RELEASE swapped_names")
            return refused
        conn.execute("RELEASE swapped_names")
        result.inserted += staged.inserted
        result.updated += staged.updated
        parked = {landing.row_id for landing in cycling}
        return [landing for landing in refused if landing.row_id not in parked]

    @staticmethod
    def _park_names(
        conn: sqlite3.Connection, table: str, cycling: Sequence[_Landing]
    ) -> bool:
        """Move every stored name in the group onto its own id, or none of them.

        Answering False rather than raising, because a database whose data
        happens to defeat this must not take the page down over it.
        """
        try:
            for landing in cycling:
                if not landing.inserting and landing.carries_a_name:
                    conn.execute(
                        f"UPDATE {table} SET name = id WHERE id = ?", (str(landing.row_id),)
                    )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _name_holder(
        conn: sqlite3.Connection, table: str, row: Mapping[str, Any]
    ) -> str | None:
        """The id of the live row already carrying this row's name, if there is one.

        Which row is in the way, not merely that something is. Asked so a set of
        rows waiting only on each other can be told apart from one waiting on a
        line the page never mentions, which no amount of retrying will free.
        """
        scope = _UNIQUE_NAME_SCOPE.get(table)
        if scope is None or row.get("deleted_at") is not None:
            return None
        scoped = "".join(f" AND {column} IS ?" for column in scope)
        found = conn.execute(
            f"SELECT id FROM {table} WHERE id != ? AND deleted_at IS NULL "
            f"AND LOWER(name) = LOWER(?){scoped}",
            (str(row["id"]), row.get("name"), *(row.get(column) for column in scope)),
        ).fetchone()
        return None if found is None else str(found["id"])

    def points_at(self, section: str, row: Mapping[str, Any]) -> list[uuid.UUID]:
        """Every parent id a row of this section names.

        Which column points where is declared beside the tables, so it is
        answered here. The sync engine asks in order to hold back a child whose
        parent it has just set aside, rather than letting the foreign key take
        a whole page down.
        """
        found: list[uuid.UUID] = []
        for attribute, _parent in _SYNC_PARENTS[_section_of(section)]:
            for value in _ids_of(row.get(attribute)):
                try:
                    found.append(uuid.UUID(str(value)))
                except ValueError:
                    logger.warning("Ignoring an unreadable %s parent %r", section, value)
        return found

    def parents_are_here(self, section: str, row: Mapping[str, Any]) -> bool:
        """Whether everything this row points at is already in the database.

        apply_from_server lets a missing parent take the page down, because the
        page comes back and lands whole once the parent is there. A row offered
        again on its own has no page behind it, so its caller has to ask first.
        """
        table = SYNC_SECTIONS[_section_of(section)]
        return not self._missing_parent(self._conn(), table, row)

    @staticmethod
    def _missing_parent(
        conn: sqlite3.Connection, table: str, row: Mapping[str, Any]
    ) -> bool:
        """Whether this row names a parent the database does not hold yet.

        How the two integrity errors are told apart. Asked of the table's own
        foreign keys rather than of a map kept alongside them, so a column added
        later is covered without anybody remembering to say so, and never of the
        message sqlite wrote, which is prose in one language.
        """
        for key in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            value = row.get(key["from"])
            if value is None:
                continue
            parent = conn.execute(
                f"SELECT 1 FROM {key['table']} WHERE {key['to'] or 'id'} = ?", (str(value),)
            ).fetchone()
            if parent is None:
                return True
        return False

    def dependency_closure(
        self, section: str, ids: Iterable[uuid.UUID]
    ) -> dict[str, list[Any]]:
        """The named rows plus every ancestor they need, in foreign-key order.

        The registry refuses a child whose parent it has never seen, so a push
        document has to be a closed set. Ancestors come whether or not they have
        changed, and a tombstoned one comes too: it is still the row the child
        points at. Empty sections are left out.
        """
        found: dict[str, dict[uuid.UUID, Any]] = {s: {} for s in SYNC_SECTIONS}
        pending = [(_section_of(section), item_id) for item_id in ids]
        while pending:
            current, item_id = pending.pop()
            if item_id in found[current]:
                continue
            model = self._row_including_deleted(SYNC_SECTIONS[current], item_id)
            if model is None:
                continue
            found[current][item_id] = model
            for attribute, parent in _SYNC_PARENTS[current]:
                pending.extend(
                    (parent, parent_id) for parent_id in _ids_of(getattr(model, attribute))
                )
        return {
            name: sorted(rows.values(), key=lambda m: (m.created_at, str(m.id)))
            for name, rows in found.items()
            if rows
        }

    def _row_including_deleted(self, table: str, item_id: uuid.UUID) -> Any:
        """One row as its model, tombstone and all: what sync reads, not what the app does."""
        row = self._conn().execute(
            f"SELECT * FROM {table} WHERE id = ?", (str(item_id),)
        ).fetchone()
        return from_row(_SYNC_MODELS[table], row) if row is not None else None

    # --- Documents ---

    def export_json(self, path: Path) -> None:
        doc = build_document(
            sites=self.list_sites(),
            campaigns=self.list_campaigns(),
            # Retired lines that still carry passes, or the document would name
            # a transect it does not contain and could not be imported whole.
            transects=self.list_transects_for_reference(),
            videos=self.list_videos(),
            batches=self.list_batches(),
            passes=self.list_passes(),
            runs=self.list_runs(),
            batch_items=self.list_all_batch_items(),
        )
        save_survey_json(path, doc)

    def import_json(self, path: Path) -> None:
        """Merge a survey document; rows whose ids already exist are left alone."""
        sections = parse_document(load_survey_json(path))
        tables = {
            "sites": "site",
            "campaigns": "campaign",
            "transects": "transect",
            "videos": "video_asset",
            "batches": "survey_batch",
            "passes": "transect_pass",
            "batch_items": "batch_item",
            "runs": "run_record",
        }
        with self._conn() as conn:
            for section, models in sections.items():
                table = tables[section]
                for model in models:
                    row = to_row(model)
                    conn.execute(_insert_sql(table, row).replace("INSERT", "INSERT OR IGNORE", 1), row)

    # --- Rebuild from manifests ---

    def rebuild_from_scan(self, out_root: Path) -> RebuildReport:
        """Recreate survey rows from run manifests; existing rows are kept as-is.

        A tombstone counts as existing and is never revived: the delete is a fact
        the registry has been told, and a rescan of footage still on disk must not
        argue with it.

        The notification log is not among them: manifests do not carry it, so a
        rebuilt database starts with an empty history. It is a record of what the
        app said, not of what the survey is.
        """
        report = RebuildReport()
        for manifest_path in sorted(out_root.glob("*/run_manifest.json")):
            run_dir_name = manifest_path.parent.name
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report.skipped.append(run_dir_name)
                continue
            survey = manifest.get("survey")
            if not isinstance(survey, dict):
                report.skipped.append(run_dir_name)
                continue
            try:
                self._restore_run(run_dir_name, manifest, survey, report)
            except (KeyError, TypeError, ValueError, sqlite3.Error):
                logger.warning("Could not restore run %s", run_dir_name, exc_info=True)
                report.skipped.append(run_dir_name)
        return report

    def _restore_run(
        self,
        run_dir_name: str,
        manifest: dict[str, Any],
        survey: dict[str, Any],
        report: RebuildReport,
    ) -> None:
        # A run written without a transect has no transect block to restore.
        transect_snapshot = survey.get("transect")
        transect_id = (
            self._restore_transect(transect_snapshot, report) if transect_snapshot else None
        )
        batch_id = self._restore_batch(survey, report)
        video_ids = self._restore_videos(manifest, report)
        pass_id = self._restore_pass(survey["pass"], transect_id, video_ids, batch_id, report)
        if batch_id is not None:
            # The manifest only knows the session the run executed in, so the
            # rebuilt pass's origin and membership default to that session.
            self.add_batch_item(BatchItem(batch_id=batch_id, pass_id=pass_id))
        run_id = uuid.UUID(survey["run_id"])
        if not self.holds_id("runs", run_id):
            self.add_run(RunRecord(
                id=run_id,
                pass_id=pass_id,
                run_dir_name=run_dir_name,
                status="succeeded",
                started_at=manifest.get("run_timestamp"),
                batch_id=batch_id,
            ))
            report.runs += 1

    def _restore_transect(self, snapshot: dict[str, Any], report: RebuildReport) -> uuid.UUID:
        transect_id = uuid.UUID(snapshot["id"])
        if not self.holds_id("transects", transect_id):
            # A line the run was swum on after it had been retired comes back
            # retired. It still names and scales the run, and reviving it would
            # put a withdrawn line back in front of whoever withdrew it.
            self.add_transect(Transect(
                id=transect_id,
                name=snapshot["name"],
                start_lat=snapshot.get("start_lat"),
                start_lon=snapshot.get("start_lon"),
                end_lat=snapshot.get("end_lat"),
                end_lon=snapshot.get("end_lon"),
                site_id=self._restore_site(snapshot.get("site"), report),
                length_m=snapshot.get("length_m"),
                depth_m=snapshot.get("depth_m"),
                start_depth_m=snapshot.get("start_depth_m"),
                end_depth_m=snapshot.get("end_depth_m"),
                deleted_at=snapshot.get("deleted_at"),
            ))
            report.transects += 1
        return transect_id

    def _restore_site(self, snapshot: Any, report: RebuildReport) -> uuid.UUID | None:
        if not isinstance(snapshot, dict) or not snapshot.get("id"):
            return None
        site_id = uuid.UUID(snapshot["id"])
        if not self.holds_id("sites", site_id):
            self.add_site(Site(
                id=site_id,
                name=snapshot.get("name") or "Recovered site",
                country=snapshot.get("country"),
                region=snapshot.get("region"),
            ))
            report.sites += 1
        return site_id

    def _restore_campaign(self, snapshot: Any, report: RebuildReport) -> uuid.UUID | None:
        if not isinstance(snapshot, dict) or not snapshot.get("id"):
            return None
        campaign_id = uuid.UUID(snapshot["id"])
        if not self.holds_id("campaigns", campaign_id):
            self.add_campaign(Campaign(
                id=campaign_id,
                name=snapshot.get("name") or "Recovered campaign",
                begin_date=snapshot.get("begin_date"),
                end_date=snapshot.get("end_date"),
            ))
            report.campaigns += 1
        return campaign_id

    def _restore_batch(self, survey: dict[str, Any], report: RebuildReport) -> uuid.UUID | None:
        raw = survey.get("batch_id")
        if not raw:
            return None
        batch_id = uuid.UUID(raw)
        if self.get_batch(batch_id) is None:
            # created_at is restored, not defaulted: it is the session's whole
            # identity now, so letting it fall to "now" would give every session
            # rebuilt in one scan the same label.
            created = survey.get("batch_created_at")
            self.add_batch(SurveyBatch(
                id=batch_id,
                preset_name=survey.get("preset_name") or "survey_preset",
                **({"created_at": str(created)} if created else {}),
            ))
            report.batches += 1
        return batch_id

    def _restore_videos(self, manifest: dict[str, Any], report: RebuildReport) -> list[uuid.UUID]:
        """Every input clip of the run, in order: a pass may span GoPro chapters."""
        paths = manifest.get("input_videos") or [""]
        hashes = manifest.get("video_hashes") or []
        sizes = manifest.get("video_sizes") or []
        mtimes = manifest.get("video_mtimes") or []

        def at(values: list, index: int) -> Any:
            return values[index] if index < len(values) else None

        video_ids = []
        for index, path in enumerate(paths):
            content_hash = at(hashes, index)
            existing = self.find_video_by_hash(content_hash, include_deleted=True)
            if existing is not None:
                video_ids.append(existing.id)
                continue
            asset = VideoAsset(
                file_name=Path(path).name or "unknown",
                path=path,
                hash=content_hash,
                size_bytes=at(sizes, index),
                mtime=at(mtimes, index),
            )
            self._add("video_asset", asset)
            report.videos += 1
            video_ids.append(asset.id)
        return video_ids

    def _restore_pass(
        self,
        snapshot: dict[str, Any],
        transect_id: uuid.UUID | None,
        video_ids: list[uuid.UUID],
        batch_id: uuid.UUID | None,
        report: RebuildReport,
    ) -> uuid.UUID:
        pass_id = uuid.UUID(snapshot["id"])
        if not self.holds_id("passes", pass_id):
            self.add_pass(TransectPass(
                id=pass_id,
                transect_id=transect_id,
                video_id=video_ids[0],
                extra_video_ids=video_ids[1:],
                batch_id=batch_id,
                direction=snapshot.get("direction"),
                begin_s=snapshot["begin_s"],
                end_s=snapshot["end_s"],
                campaign_id=self._restore_campaign(snapshot.get("campaign"), report),
                surveyed_on=snapshot.get("surveyed_on"),
                quality=snapshot.get("quality"),
            ))
            report.passes += 1
        return pass_id
