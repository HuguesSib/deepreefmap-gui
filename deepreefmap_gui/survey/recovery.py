"""Ways back from a survey database this build cannot open, each one measured.

Two routes recover data, and they recover different amounts:

*Restore a backup* is exact. It is the database the previous build last wrote,
copied before the upgrade migrated it. Only work done on the newer version is
lost.

*Rebuild from run folders* reads every ``run_manifest.json`` under the output
root. It recovers what has been **processed**, not what has been **planned** --
see :func:`rebuild_losses` for the specifics, which the dialog shows rather than
summarising as "some data may be lost".

Both counts here are measured, not estimated: the rebuild is dry-run into a
throwaway database first, so the numbers offered are the numbers that will
result.

The remaining two routes recover nothing and do not pretend to: starting a new
database, and working in a different folder. They are offered anyway, because a
survey that cannot be recovered is not a reason to leave someone with a window
that will not open one at all. Nothing here deletes; the database in place is
always renamed. Qt-free.

Only the lossy routes are ever put to the user: :func:`automatic_recovery` names
the one route that is exact, and the window takes it without asking.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from deepreefmap_gui.survey.backup import SurveyBackup, best_backup
from deepreefmap_gui.survey.health import SurveyDbHealth, SurveyDbState

logger = logging.getLogger(__name__)


class RecoveryKind(str, Enum):
    RESTORE_BACKUP = "restore_backup"
    REBUILD = "rebuild"
    START_FRESH = "start_fresh"
    CHOOSE_FOLDER = "choose_folder"


@dataclass(frozen=True)
class RecoveryOption:
    kind: RecoveryKind
    title: str
    detail: str
    # None when nothing was counted (choosing a folder counts nothing).
    counts: RebuildCounts | None = None
    backup: SurveyBackup | None = None
    recommended: bool = False


@dataclass(frozen=True)
class RebuildCounts:
    transects: int = 0
    videos: int = 0
    batches: int = 0
    passes: int = 0
    runs: int = 0
    skipped: int = 0

    def summary(self) -> str:
        parts = [
            f"{self.runs} run{'s' if self.runs != 1 else ''}",
            f"{self.transects} transect{'s' if self.transects != 1 else ''}",
            f"{self.passes} pass{'es' if self.passes != 1 else ''}",
        ]
        text = "Recovers " + ", ".join(parts) + "."
        if self.skipped:
            text += f" {self.skipped} run folder(s) could not be read."
        return text


def rebuild_losses() -> list[str]:
    """What a manifest rebuild cannot bring back, stated concretely.

    Each line is a real consequence of how run manifests are written, not a
    hedge: manifests exist only for runs, they carry a snapshot of the transect
    as it was at run time, and SurveyStore._restore_run stamps every recovered
    run as succeeded.
    """
    return [
        "Transects, passes and clips that were never processed leave no run "
        "folder, so they cannot be recovered.",
        "Runs that failed or were cancelled come back marked as succeeded, "
        "without their finish time or error.",
        "Pass notes, cart order and per-pass settings, and transect "
        "descriptions are not stored in run folders.",
        "Transects recover as they were when the run was processed, so later "
        "corrections to a name or coordinates are lost.",
    ]


def count_rebuild(out_root: Path) -> RebuildCounts:
    """Dry-run the rebuild into a throwaway database and report what it produced.

    rebuild_from_scan only ever inserts rows that are absent, so running it
    against an empty database counts exactly what a real recovery would restore.
    """
    from deepreefmap_gui.survey.store import SurveyStore

    with tempfile.TemporaryDirectory(prefix="drm-rebuild-count-") as tmp:
        try:
            store = SurveyStore(Path(tmp) / "probe.db")
        except (sqlite3.Error, OSError, RuntimeError):
            logger.warning("Could not measure a rebuild of %s", out_root, exc_info=True)
            return RebuildCounts()
        try:
            report = store.rebuild_from_scan(out_root)
        except OSError:
            logger.warning("Could not scan %s", out_root, exc_info=True)
            return RebuildCounts()
        finally:
            store.close()
    return RebuildCounts(
        transects=report.transects,
        videos=report.videos,
        batches=report.batches,
        passes=report.passes,
        runs=report.runs,
        skipped=len(report.skipped),
    )


def _restore_option(health: SurveyDbHealth) -> RecoveryOption | None:
    """Restoring the pre-upgrade backup, when one this build can open exists."""
    from deepreefmap_gui.survey.store import oldest_supported_version

    # Floored as well as capped. known_version is the newest format this build
    # writes, not the whole range it reads, so without the floor a database
    # refused for being too old is answered with a copy of itself.
    backup = best_backup(
        health.path, health.known_version, min_version=oldest_supported_version()
    )
    if backup is None:
        return None
    taken = backup.taken_at.astimezone().strftime("%d %b %Y, %H:%M")
    return RecoveryOption(
        kind=RecoveryKind.RESTORE_BACKUP,
        title="Restore the backup taken before the upgrade",
        detail=(
            f"Saved {taken}, in the format this version reads "
            f"(v{backup.version}). Everything up to that moment comes back "
            f"exactly; work done on the newer version does not."
        ),
        backup=backup,
        recommended=True,
    )


def automatic_recovery(health: SurveyDbHealth) -> RecoveryOption | None:
    """The route to take without asking, or None when the choice is a real one.

    Only one state answers this: a database left by a newer build after a
    rollback, where the backup was taken by that same upgrade moments before it
    migrated. Restoring it is the exact undo of the update that was undone, the
    loss is bounded by that, and the newer build's file is kept beside it.
    Weighing schema versions against each other is not work to hand someone on a
    boat, so that route is simply taken and reported.

    Every other route trades away an unbounded amount: a rebuild loses
    everything never processed, a new database carries nothing over, and a
    different folder walks away from the survey. So does restoring a backup in
    any other state -- against a corrupt file the newest backup may be months
    old. Those stay the user's to choose.
    """
    if health.state is not SurveyDbState.TOO_NEW:
        return None
    return _restore_option(health)


def recovery_options(health: SurveyDbHealth, out_root: Path) -> list[RecoveryOption]:
    """Every route out of this state, in the order the dialog should offer them.

    Restoring a backup comes first when one this build can open exists, because
    it is the only exact route; the rebuild is offered whenever a run folder can
    be read; the two that recover nothing come last.
    """
    options: list[RecoveryOption] = []

    restore_backup = _restore_option(health)
    if restore_backup is not None:
        options.append(restore_backup)

    counts = count_rebuild(out_root)
    if counts.runs or not options:
        options.append(RecoveryOption(
            kind=RecoveryKind.REBUILD,
            title="Rebuild from the run folders",
            detail=counts.summary(),
            counts=counts,
            recommended=not options,
        ))

    # Last of the routes that keep working here, and offered whatever the state:
    # when nothing can be recovered, carrying on is still worth more than a
    # window that will not open a survey. The old file is renamed, never deleted.
    options.append(RecoveryOption(
        kind=RecoveryKind.START_FRESH,
        title="Start a new survey database",
        detail=(
            f"The current one is kept as {_set_aside_name(health)}, so it can be "
            f"opened again by a version that reads it. Nothing in it is carried over."
        ),
    ))

    options.append(RecoveryOption(
        kind=RecoveryKind.CHOOSE_FOLDER,
        title="Use a different output folder",
        detail="Leave this survey untouched and work somewhere else.",
    ))
    return options


def apply_recovery(option: RecoveryOption, health: SurveyDbHealth, out_root: Path) -> str:
    """Carry out a route and return what to tell the user. Never deletes anything.

    The database in place is always moved aside under a name carrying its schema
    version, so the newer build's work stays on disk and is found again if the
    user upgrades back.
    """
    from deepreefmap_gui.survey.backup import restore, set_aside
    from deepreefmap_gui.survey.store import SurveyStore

    if option.kind is RecoveryKind.RESTORE_BACKUP:
        assert option.backup is not None
        restore(option.backup, health.path, health.db_version)
        return (
            f"Restored the survey from {option.backup.path.name}. The newer "
            f"version's database was kept as "
            f"{_set_aside_name(health)}."
        )

    if option.kind is RecoveryKind.REBUILD:
        displaced = set_aside(health.path, health.db_version)
        store = SurveyStore(health.path)
        try:
            report = store.rebuild_from_scan(out_root)
        finally:
            store.close()
        return (
            f"Rebuilt the survey from {report.runs} run folder(s). The previous "
            f"database was kept as {displaced.name}."
        )

    if option.kind is RecoveryKind.START_FRESH:
        displaced = set_aside(health.path, health.db_version)
        SurveyStore(health.path).close()
        return (
            f"Started a new survey database. The previous one was kept as "
            f"{displaced.name}."
        )

    raise ValueError(f"{option.kind} is not applied here")


def _set_aside_name(health: SurveyDbHealth) -> str:
    label = f"schema-v{health.db_version}" if health.db_version is not None else "unreadable"
    return f"{health.path.name}.{label}"


# --- a survey an older build put aside ------------------------------------------

# The tables a person's work lands in; the latest stamp across them is when the
# survey was last worked on.
_ACTIVITY_TABLES = ("run_record", "transect_pass", "video_asset", "batch_item", "transect", "campaign")
_SET_ASIDE = re.compile(r"\.schema-v(\d+)(\.\d{8}T\d{6})?$")


@dataclass(frozen=True)
class SetAsideSurvey:
    path: Path
    version: int
    last_activity: str


def _last_activity(path: Path) -> str | None:
    """The newest created/updated stamp in a survey file, read without touching it."""
    # A set-aside file carries its moved -wal beside it, so read-only first; an
    # exFAT volume can refuse the shared-memory file, and immutable reads the
    # main file alone, which is what is left when the -wal is empty.
    for mode in ("mode=ro", "immutable=1"):
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
        except sqlite3.Error:
            continue
        try:
            stamps: list[str] = []
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in _ACTIVITY_TABLES:
                if table not in tables:
                    continue
                columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                for column in ("updated_at", "created_at"):
                    if column in columns:
                        value = conn.execute(f'SELECT max("{column}") FROM "{table}"').fetchone()[0]
                        if value:
                            stamps.append(str(value))
            return max(stamps) if stamps else ""
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return None


def newer_set_aside_survey(db_path: Path) -> SetAsideSurvey | None:
    """A survey an older build moved aside that holds later work than the one in place.

    An older build that finds a database newer than it reads restores its own
    pre-upgrade backup and moves the newer file aside as ``survey.db.schema-vN``.
    Taken to a machine with that build and back, the expedition drive comes home
    with hours of work out of sight. This finds it, provided this build reads it.
    """
    from deepreefmap_gui.survey.store import can_open

    current = _last_activity(db_path) if db_path.exists() else ""
    if current is None:
        return None
    best: SetAsideSurvey | None = None
    try:
        siblings = list(db_path.parent.glob(f"{db_path.name}.schema-v*"))
    except OSError:
        return None
    for path in siblings:
        match = _SET_ASIDE.search(path.name)
        if match is None or not can_open(int(match.group(1))):
            continue
        activity = _last_activity(path)
        if not activity or activity <= current:
            continue
        if best is None or activity > best.last_activity:
            best = SetAsideSurvey(path, int(match.group(1)), activity)
    return best


def bring_back(found: SetAsideSurvey, db_path: Path) -> str:
    """Put the set-aside survey back in place; the one it replaces is kept. Never deletes."""
    from datetime import datetime, timezone

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    replaced = db_path.with_name(f"{db_path.name}.replaced.{stamp}")
    if db_path.exists():
        source = sqlite3.connect(db_path)
        try:
            dest = sqlite3.connect(replaced)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    # Copied in through sqlite rather than renamed over, so a connection already
    # open on the live file sees the restored content instead of a deleted inode.
    source = sqlite3.connect(f"{found.path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(db_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    logger.info("Brought back %s over %s (kept as %s)", found.path.name, db_path.name, replaced.name)
    return (
        f"An older version of DeepReefMap had replaced this survey with an earlier backup. "
        f"The newer survey ({found.path.name}) is back in place; the one it replaced is "
        f"kept as {replaced.name}."
    )
