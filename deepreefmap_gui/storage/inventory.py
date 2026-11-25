"""Everything the survey put on one drive, in the shape a page can list it.

The storage page answers one question per drive: what is here, and which of it
can go. That splits three ways. Runs are the output directories, each already
tiered by tiers.py. Clips are the footage, which lives wherever it was imported
from and so is filtered by the same volume_root the accounting uses. Everything
else is the residue nobody chose to keep: folders a run died inside before it
wrote a manifest, scene temp files a save left behind, databases an upgrade set
aside.

``classify_mount`` is pure and takes the facts as arguments the way
``group_by_volume`` takes sizes, so a test describes a mount rather than building
one. ``read_mount`` is the gathering half: one scandir of the output root, one
measure per run nobody has measured yet, and no database connection at all,
because it runs on a worker thread and SurveyStore is thread-confined.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from deepreefmap_gui.profiling.volumes import IsMountFn, volume_root
from deepreefmap_gui.storage.tiers import RunBreakdown, measure_run, tree_bytes
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_UNKNOWN,
    RunEntry,
    VideoLibraryEntry,
    holds_survey_footage,
)

logger = logging.getLogger(__name__)

KIND_ABORTED_RUN, KIND_ORPHAN_DIR = "aborted_run", "orphan_dir"
KIND_SCENE_TMP, KIND_DB_PARTIAL, KIND_DB_SET_ASIDE = "scene_tmp", "db_partial", "db_set_aside"
KIND_UNKNOWN_FILE = "unknown_file"

# What each kind of residue is, said once, so the page carries no vocabulary of
# its own. Only the kinds the app itself leaves behind get an explanation; a
# folder nobody here wrote gets the honest one.
KIND_DETAIL = {
    KIND_ABORTED_RUN: "Stopped before it finished. Nothing to open, nothing to resume.",
    KIND_ORPHAN_DIR: "Not written by this app.",
    KIND_SCENE_TMP: "Left behind by a save that was interrupted.",
    KIND_DB_PARTIAL: "A backup that never finished being written.",
    KIND_DB_SET_ASIDE: "A database set aside during an upgrade. Its work is still in there.",
    KIND_UNKNOWN_FILE: "Not written by this app.",
}

_MANIFEST = "run_manifest.json"
_RESUME_KEY = os.path.join(".cache", "preprocess.json")

# The survey database and the backups that are the way back from a bad upgrade.
# Never offered: the whole set is under a megabyte, and it is the only copy of
# the catalogue an app one version older can read.
_DB_KEPT_SUFFIXES = (".bak", "-wal", "-shm")


@dataclass(frozen=True, slots=True)
class MountItem:
    """One row of residue: what it is, why it is here, and what it weighs."""

    kind: str
    label: str
    path: Path
    size_bytes: int = 0

    @property
    def detail(self) -> str:
        return KIND_DETAIL.get(self.kind, KIND_DETAIL[KIND_UNKNOWN_FILE])


@dataclass(frozen=True, slots=True)
class MountRun:
    """One run directory on this drive, tiered when it has been measured."""

    dir_name: str
    run_dir: Path
    display_name: str
    status: str
    # No manifest and no resume key: nothing to open and nothing to finish.
    aborted: bool = False
    breakdown: RunBreakdown | None = None
    # A total from Browse's own scan, shown while the tiers are still being
    # walked so a row has a size from the first paint.
    total_hint: int | None = None

    @property
    def total_bytes(self) -> int:
        if self.breakdown is not None:
            return self.breakdown.total_bytes
        return self.total_hint or 0

    @property
    def measured(self) -> bool:
        return self.breakdown is not None


@dataclass(frozen=True, slots=True)
class MountClip:
    """One imported clip whose file sits on this drive."""

    video_id: uuid.UUID
    file_name: str
    path: str
    size_bytes: int | None
    link_state: str
    pass_count: int
    succeeded_passes: int

    @property
    def deletable(self) -> bool:
        """Whether the original file may go.

        Only footage the survey has already got something out of. A clip with no
        finished run is the only copy of a dive, and no amount of disk pressure
        makes deleting it the right offer.
        """
        return self.link_state == LINK_LINKED and self.succeeded_passes > 0


@dataclass(frozen=True, slots=True)
class MountInventory:
    """One drive, as the storage page lists it."""

    root: str
    holds_out_root: bool = False
    runs: tuple[MountRun, ...] = ()
    clips: tuple[MountClip, ...] = ()
    others: tuple[MountItem, ...] = ()
    unmeasured_items: int = 0


def _succeeded_passes(entry: VideoLibraryEntry) -> int:
    """Distinct passes of this clip that finished.

    Distinct, and against pass_count rather than run_count: a rerun of one pass
    is another run of the same work, so counting runs reads "5 of 4 sections".
    """
    return len({run.pass_id for run in entry.runs if run.status == "succeeded"})


def clips_on(
    root: str, clips: Iterable[VideoLibraryEntry], *, ismount: IsMountFn = os.path.ismount
) -> tuple[MountClip, ...]:
    """The clips whose file resolves onto this drive, in name order."""
    found = []
    for entry in clips:
        if volume_root(entry.video.path, ismount=ismount) != root:
            continue
        found.append(
            MountClip(
                video_id=entry.video.id,
                file_name=entry.video.file_name,
                path=entry.video.path,
                size_bytes=entry.video.size_bytes,
                link_state=entry.link_state or LINK_UNKNOWN,
                pass_count=entry.pass_count,
                succeeded_passes=_succeeded_passes(entry),
            )
        )
    return tuple(sorted(found, key=lambda c: c.file_name.lower()))


def classify_mount(
    root: str,
    out_root: Path,
    *,
    entries: Sequence[RunEntry],
    clips: Sequence[VideoLibraryEntry],
    children: Sequence[tuple[str, bool, int]],
    breakdowns: Mapping[str, RunBreakdown],
    ismount: IsMountFn = os.path.ismount,
) -> MountInventory:
    """Sort one drive's contents into runs, clips and residue.

    ``children`` is the output root's own listing as (name, is_dir, size), which
    the caller has already walked. A run directory is one this app wrote and the
    catalogue knows, directly under the output root or in an expedition's
    RESULTS folders; anything else directly under the root is residue and is
    listed but never pre-selected, except a folder holding footage, which is
    not listed at all.
    """
    holds_out_root = volume_root(str(out_root), ismount=ismount) == root
    by_name = {entry.dir_name: entry for entry in entries if not entry.data_missing}

    runs: list[MountRun] = []
    others: list[MountItem] = []
    if holds_out_root:
        for name, is_dir, size in children:
            entry = by_name.get(name)
            if is_dir and entry is not None:
                run_dir = out_root / name
                runs.append(
                    MountRun(
                        dir_name=name,
                        run_dir=run_dir,
                        display_name=entry.display_name or name,
                        status=entry.status_label,
                        aborted=_aborted(run_dir),
                        breakdown=breakdowns.get(name),
                        total_hint=entry.size_bytes,
                    )
                )
                continue
            # Under an expedition root the transects are direct children too;
            # they are the footage, never residue to clear away.
            if is_dir and holds_survey_footage(out_root / name):
                continue
            item = _residue(out_root, name, is_dir=is_dir, size=size)
            if item is not None:
                others.append(item)
        # Runs inside an expedition tree's RESULTS folders, named by their path.
        for name, entry in by_name.items():
            if "/" not in name:
                continue
            run_dir = out_root / name
            if not run_dir.is_dir():
                continue
            runs.append(
                MountRun(
                    dir_name=name,
                    run_dir=run_dir,
                    display_name=entry.display_name or name,
                    status=entry.status_label,
                    aborted=_aborted(run_dir),
                    breakdown=breakdowns.get(name),
                    total_hint=entry.size_bytes,
                )
            )

    return MountInventory(
        root=root,
        holds_out_root=holds_out_root,
        runs=tuple(runs),
        clips=clips_on(root, clips, ismount=ismount),
        others=tuple(others),
    )


def _aborted(run_dir: Path) -> bool:
    """A folder a run died in before it could be opened or resumed.

    A crash that still left its resume key behind is a run somebody can finish,
    so it stays a run however unfinished it looks.
    """
    return not (run_dir / _MANIFEST).exists() and not (run_dir / _RESUME_KEY).exists()


def _residue(out_root: Path, name: str, *, is_dir: bool, size: int) -> MountItem | None:
    """What an entry of the output root is, when it is not a run this app knows.

    Returns None for the things that are never anybody's to delete here: the
    live database, its sidecars, and the version backups that are the only way
    back from an upgrade.
    """
    path = out_root / name
    if is_dir:
        kind = KIND_ABORTED_RUN if _aborted(path) else KIND_ORPHAN_DIR
        return MountItem(kind=kind, label=name, path=path, size_bytes=size)
    if name.endswith(".tmp"):
        return MountItem(kind=KIND_SCENE_TMP, label=name, path=path, size_bytes=size)
    if name.endswith(".partial"):
        return MountItem(kind=KIND_DB_PARTIAL, label=name, path=path, size_bytes=size)
    if ".schema-v" in name or name.endswith(".unreadable"):
        return MountItem(kind=KIND_DB_SET_ASIDE, label=name, path=path, size_bytes=size)
    if name.endswith(_DB_KEPT_SUFFIXES) or name.endswith(".db"):
        return None
    return MountItem(kind=KIND_UNKNOWN_FILE, label=name, path=path, size_bytes=size)


def scan_children(out_root: Path) -> tuple[list[tuple[str, bool, int]], int]:
    """List the output root once, as (name, is_dir, size) plus what refused.

    Directory sizes are the entry's own, not the tree's: a run's real weight
    comes from its breakdown, and residue is sized by the walk below.
    """
    children: list[tuple[str, bool, int]] = []
    unmeasured = 0
    try:
        listing = list(os.scandir(out_root))
    except OSError:
        logger.debug("Could not read the output root %s", out_root, exc_info=True)
        return children, unmeasured
    for child in listing:
        try:
            is_dir = child.is_dir(follow_symlinks=False)
            size = child.stat(follow_symlinks=False).st_size
        except OSError:
            unmeasured += 1
            continue
        children.append((child.name, is_dir, size))
    return children, unmeasured


def read_mount(
    root: str,
    out_root: Path,
    *,
    entries: Sequence[RunEntry],
    clips: Sequence[VideoLibraryEntry],
    known: Mapping[str, RunBreakdown],
) -> MountInventory:
    """Walk one drive and describe it. Blocking, so call it off the GUI thread."""
    children, unmeasured = scan_children(out_root)
    inventory = classify_mount(
        root,
        out_root,
        entries=entries,
        clips=clips,
        children=children,
        breakdowns=known,
        ismount=os.path.ismount,
    )

    measured = dict(known)
    for run in inventory.runs:
        if run.dir_name not in measured:
            measured[run.dir_name] = measure_run(run.run_dir)

    runs = tuple(
        MountRun(
            dir_name=run.dir_name,
            run_dir=run.run_dir,
            display_name=run.display_name,
            status=run.status,
            aborted=run.aborted,
            breakdown=measured.get(run.dir_name),
            total_hint=run.total_hint,
        )
        for run in inventory.runs
    )
    others = tuple(
        MountItem(
            kind=item.kind,
            label=item.label,
            path=item.path,
            size_bytes=_residue_bytes(item),
        )
        for item in inventory.others
    )
    return MountInventory(
        root=inventory.root,
        holds_out_root=inventory.holds_out_root,
        runs=runs,
        clips=inventory.clips,
        others=others,
        unmeasured_items=unmeasured + sum(run.breakdown.unmeasured_items for run in runs if run.breakdown),
    )


def _residue_bytes(item: MountItem) -> int:
    """A residue row's real weight: a folder is its whole tree, a file its own."""
    if not item.path.is_dir():
        return item.size_bytes
    return item.size_bytes + tree_bytes(item.path)[0]
