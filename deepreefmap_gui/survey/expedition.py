"""An expedition folder as a workspace: read its tree, file its footage, place its results.

The field team sorts footage into a tree before anyone opens the app::

    2026_11_JO_EXPE/                      the expedition (a campaign)
      VC_6m_01/                           a transect
        pass_01_control/                  a pass: one condition, every camera
          fwd/  *.MP4  or  RAW/*.MP4      the clips swum forward
          bwd/  ...                       and back
        colour_charts/  context/  ...     other footage: left out of the library

Everything the tree says is taken from it, so nobody types it again: the
campaign, the transects (with the depth a name like ``VC_6m_01`` carries), and
one app pass per recording, filed under the pass folder's name and direction.
GoPro chapters of one recording (``GX010099``, ``GX020099``) become one pass.

Reading the tree (``scan_expedition``) touches only directory listings, so it
can be shown before anything is written. Applying it (``import_expedition``) is
idempotent: clips are keyed by content hash, a recording that already has a
pass keeps it (and the trim someone gave it), and names match ignoring case.

Results go back into the tree. ``results_dir_for`` places a pass's run in
``<pass>/<fwd|bwd>/RESULTS/`` beside the clips it was made from, relative to the
output root, which is what ``RunRecord.run_dir_name`` has always been.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from deepreefmap_gui.survey.models import Campaign, Transect, TransectPass, VideoAsset

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov"}
DIRECTIONS = {"fwd": "forward", "forward": "forward", "bwd": "reverse", "back": "reverse", "reverse": "reverse"}
RAW_DIR = "RAW"
RESULTS_DIR = "RESULTS"
ANALYSIS_DIR = "ANALYSIS"
# Folders the app writes or that hold no footage of their own.
_SKIPPED = {RESULTS_DIR.lower(), ANALYSIS_DIR.lower()}

_PASS_DIR = re.compile(r"^pass_?(?P<number>\d+)(?:_(?P<condition>.+))?$", re.IGNORECASE)
_DEPTH = re.compile(r"(?:^|_)(?P<depth>\d+(?:[.,]\d+)?)m(?:_|$)", re.IGNORECASE)
_CAMERA = re.compile(r"(?:^|[_\-\s])cam[_\-]?(?P<n>\d+)(?:[_\-\s.]|$)", re.IGNORECASE)
# GoPro names a recording G<X|H|L><chapter:2><number:4> (HERO6 on); older GOPR/GP names stand alone.
_GOPRO = re.compile(r"G[XHL](?P<chapter>\d{2})(?P<number>\d{4})", re.IGNORECASE)

@dataclass(frozen=True, slots=True)
class Recording:
    """One recording in a pass folder: its chapters in order, and how it was swum."""

    pass_dir: str
    direction: str
    clips: tuple[Path, ...]


@dataclass(slots=True)
class TransectFolder:
    name: str
    depth_m: float | None
    recordings: list[Recording] = field(default_factory=list)
    # Footage of the transect that is not a pass, by folder: colour charts, context.
    other: dict[str, list[Path]] = field(default_factory=dict)


@dataclass(slots=True)
class ExpeditionLayout:
    root: Path
    name: str
    transects: list[TransectFolder] = field(default_factory=list)
    # Footage directly under the expedition, or in folders that are not transects.
    loose: dict[str, list[Path]] = field(default_factory=dict)
    # Directories that look like a pass but have no fwd/bwd, worth telling someone.
    warnings: list[str] = field(default_factory=list)

    @property
    def clip_count(self) -> int:
        in_passes = sum(len(r.clips) for t in self.transects for r in t.recordings)
        other = sum(len(v) for t in self.transects for v in t.other.values())
        return in_passes + other + sum(len(v) for v in self.loose.values())


@dataclass(slots=True)
class ImportReport:
    campaign_created: bool = False
    transects_created: list[str] = field(default_factory=list)
    clips_added: int = 0
    clips_relinked: int = 0
    passes_created: int = 0
    passes_kept: int = 0
    # Passes whose clips moved to another pass or direction folder, filed again.
    passes_refiled: int = 0
    # "old → new": transect folders renamed on disk, their passes moved along.
    transects_renamed: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    # Footage outside pass folders: left on disk, kept out of the library.
    skipped_other: int = 0
    # Clips an earlier import took from those folders, removed while still unused.
    removed_other: int = 0
    # Transects whose name another expedition already uses on no site.
    shared_transects: list[str] = field(default_factory=list)
    # (copy, original): files whose content is already a clip somewhere else.
    duplicates: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.campaign_created:
            parts.append("created the expedition")
        if self.transects_created:
            parts.append(f"{len(self.transects_created)} new transect(s)")
        parts.append(f"{self.clips_added} new clip(s)")
        if self.clips_relinked:
            parts.append(f"{self.clips_relinked} relinked")
        parts.append(f"{self.passes_created} new pass(es)")
        if self.passes_kept:
            parts.append(f"{self.passes_kept} already filed")
        if self.passes_refiled:
            parts.append(f"{self.passes_refiled} moved to the pass their folder names")
        if self.transects_renamed:
            parts.append(f"renamed {', '.join(self.transects_renamed)}")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable")
        if self.skipped_other:
            parts.append(f"{self.skipped_other} clip(s) outside pass folders left out")
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} duplicate file(s) left out")
        if self.shared_transects:
            parts.append(
                f"{', '.join(self.shared_transects)} shared with another expedition (give each a site to separate them)"
            )
        if self.removed_other:
            parts.append(f"{self.removed_other} earlier non-pass clip(s) removed from the library")
        return "Imported: " + ", ".join(parts) + "."


# --- reading the tree ----------------------------------------------------------


def _videos(directory: Path) -> list[Path]:
    try:
        return sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith("._")
        )
    except OSError:
        return []


def _subdirs(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def depth_from_name(name: str) -> float | None:
    """``VC_6m_01`` → 6.0; None when the name carries no ``<n>m`` token."""
    match = _DEPTH.search(name)
    return float(match.group("depth").replace(",", ".")) if match else None


def camera_from_name(name: str) -> str | None:
    """``…_Cam1_GX010099.MP4`` → ``Cam1``; None when the file does not say."""
    match = _CAMERA.search(Path(name).stem)
    return f"Cam{int(match.group('n'))}" if match else None


def _group_chapters(clips: list[Path]) -> list[tuple[Path, ...]]:
    """Recordings out of a direction folder: GoPro chapters of one number, per camera.

    Clips that are not GoPro-named stand alone. Chapters are only joined when the
    camera (if the name gives one) agrees, so two cameras' ``GX010034`` in one
    folder after a rename stay two recordings.
    """
    groups: dict[tuple[str | None, str], list[tuple[int, Path]]] = {}
    alone: list[tuple[Path, ...]] = []
    for clip in clips:
        match = _GOPRO.search(clip.stem)
        if match is None:
            alone.append((clip,))
            continue
        key = (camera_from_name(clip.name), match.group("number"))
        groups.setdefault(key, []).append((int(match.group("chapter")), clip))
    chained = [tuple(p for _, p in sorted(members)) for members in groups.values()]
    return sorted(chained + alone, key=lambda rec: rec[0].name)


def _direction_clips(direction_dir: Path) -> list[Path]:
    raw = next((d for d in _subdirs(direction_dir) if d.name.upper() == RAW_DIR), None)
    return _videos(direction_dir) + (_videos(raw) if raw is not None else [])


def _read_transect(directory: Path, layout: ExpeditionLayout) -> TransectFolder | None:
    folder = TransectFolder(name=directory.name, depth_m=depth_from_name(directory.name))
    for child in _subdirs(directory):
        if child.name.lower() in _SKIPPED:
            continue
        if _PASS_DIR.match(child.name):
            directions = [d for d in _subdirs(child) if d.name.lower() in DIRECTIONS]
            if not directions:
                layout.warnings.append(f"{directory.name}/{child.name} has no fwd or bwd folder.")
            for direction_dir in directions:
                for chapters in _group_chapters(_direction_clips(direction_dir)):
                    folder.recordings.append(
                        Recording(child.name, DIRECTIONS[direction_dir.name.lower()], chapters)
                    )
            continue
        role_clips = _videos(child)
        if role_clips:
            folder.other[child.name] = role_clips
    clips_here = _videos(directory)
    if clips_here:
        folder.other[""] = clips_here
    if not folder.recordings and not folder.other:
        return None
    # Only a folder with pass folders is a transect; anything else is loose footage.
    if not folder.recordings:
        for role, found in folder.other.items():
            key = f"{directory.name}/{role}" if role else directory.name
            layout.loose[key] = found
        return None
    return folder


def scan_expedition(root: Path) -> ExpeditionLayout:
    """What the tree says, from directory listings alone."""
    layout = ExpeditionLayout(root=root, name=root.name)
    for child in _subdirs(root):
        if child.name.lower() in _SKIPPED:
            continue
        transect = _read_transect(child, layout)
        if transect is not None:
            layout.transects.append(transect)
    clips_here = _videos(root)
    if clips_here:
        layout.loose[""] = clips_here
    return layout


# --- writing it into the survey --------------------------------------------------


Probe = Callable[[Path], "tuple[float, float] | None"]


def _decoded_length(path: Path) -> tuple[float, float] | None:
    from deepreefmap_gui.io.video_length import decoded_length

    return decoded_length(path)


def _campaign(store, name: str, report: ImportReport) -> Campaign:
    found = next((c for c in store.list_campaigns() if c.name.lower() == name.lower()), None)
    if found is not None:
        return found
    campaign = Campaign(name=name)
    store.add_campaign(campaign)
    report.campaign_created = True
    return campaign


def _transect(store, folder: TransectFolder, campaign: Campaign, report: ImportReport) -> Transect:
    """The transect this folder is, preferring the one this expedition already uses.

    A name like T1 is reused across expeditions and sites, so a same-named line
    is only taken when this campaign already files passes under it, or when no
    other campaign does.
    """
    named = [t for t in store.list_transects() if t.name.lower() == folder.name.lower()]
    users: dict[object, set] = {}
    for pass_ in store.list_passes():
        if pass_.transect_id is not None:
            users.setdefault(pass_.transect_id, set()).add(pass_.campaign_id)
    found = next((t for t in named if campaign.id in users.get(t.id, set())), None)
    if found is None:
        found = next((t for t in named if users.get(t.id, set()) <= {None}), None)
    if found is None:
        # Another expedition's line holds the name on the same (absent) site,
        # and the store keeps one name per site, so it is shared rather than
        # refused; the report says so, since only a site tells the two apart.
        found = next((t for t in named if t.site_id is None), None)
        if found is not None:
            report.shared_transects.append(folder.name)
    if found is not None:
        _drop_empty_twins(store, named, found)
        if found.depth_m is None and folder.depth_m is not None:
            found.depth_m = folder.depth_m
            store.update_transect(found)
        return found
    transect = Transect(name=folder.name, depth_m=folder.depth_m)
    store.add_transect(transect)
    report.transects_created.append(folder.name)
    return transect


def _is_blank(transect: Transect) -> bool:
    """Nothing a person entered: no ends, no length, no site (depth comes from the name)."""
    return transect.end_points() is None and transect.length_m is None and transect.site_id is None


def _drop_empty_twins(store, named: list[Transect], kept: Transect) -> None:
    """Retire same-named transects an earlier import left with nothing on them."""
    for twin in named:
        if twin.id != kept.id and _is_blank(twin) and not store.list_passes(transect_id=twin.id):
            store.delete_transect(twin.id)


# What a renamed transect hands to the one its folder is now called.
_CARRIED = (
    "start_lat", "start_lon", "start_accuracy_m", "end_lat", "end_lon", "end_accuracy_m",
    "length_m", "depth_m", "start_depth_m", "end_depth_m", "site_id",
)


def _follow_the_folder(store, kept, transect: Transect, recording, campaign: Campaign, moved: dict) -> int:
    """File this expedition's passes where their folder now puts them.

    The folders are the record: a clip moved to another transect, pass or
    direction folder takes its pass with it. Returns how many passes moved.
    """
    refiled = 0
    for pass_ in kept:
        if pass_.campaign_id != campaign.id:
            continue
        before = (pass_.transect_id, pass_.label, pass_.direction)
        if pass_.transect_id not in (None, transect.id):
            moved.setdefault(pass_.transect_id, transect.id)
            pass_.transect_id = transect.id
        pass_.label = recording.pass_dir
        pass_.direction = recording.direction or pass_.direction
        if (pass_.transect_id, pass_.label, pass_.direction) != before:
            store.update_pass(pass_)
            refiled += 1
    return refiled


def _retire_renamed(store, moved: dict, folder_names: set[str], report: ImportReport) -> None:
    """An old transect left with no passes gives its details to its new name and goes."""
    for old_id, new_id in moved.items():
        old, new = store.get_transect(old_id), store.get_transect(new_id)
        if old is None or new is None or old.name.lower() in folder_names:
            continue
        if store.list_passes(transect_id=old_id):
            continue  # another expedition still swims it under that name
        store.delete_transect(old_id)
        for attr in _CARRIED:
            if getattr(new, attr) is None and getattr(old, attr) is not None:
                setattr(new, attr, getattr(old, attr))
        if not new.description and old.description:
            new.description = old.description
        try:
            store.update_transect(new)
        except sqlite3.IntegrityError:  # its site already has a line of the new name
            new.site_id = None
            store.update_transect(new)
        report.transects_renamed.append(f"{old.name} → {new.name}")


def _clip(store, path: Path, probe: Probe, report: ImportReport) -> VideoAsset | None:
    asset = VideoAsset.from_path(path)
    prior = store.find_video_by_hash(asset.hash) if asset.hash else store.find_video_by_path(str(path))
    # Same content at another path that still exists is a copy, not a move:
    # repointing the clip would flip it between the two folders on every
    # import, so the copy is left out and named instead.
    if prior is not None and prior.path != str(path) and _another_file(Path(prior.path), path):
        report.duplicates.append((str(path), prior.path))
        return None
    measured = probe(path)
    if measured is None:
        report.unreadable.append(str(path))
        return None
    asset.duration_s, asset.fps = measured
    # The folder above a clip here is fwd or RAW, never a camera, so the camera
    # comes from the file name or stays unknown.
    asset.camera_label = camera_from_name(path.name)
    stored = store.upsert_video(asset)
    if prior is None:
        report.clips_added += 1
    elif prior.path != str(path):
        report.clips_relinked += 1
    changed = False
    # A clip added by hand earlier got its folder as its camera ("fwd", "RAW");
    # the file name is the better answer, but a name someone typed is kept.
    camera = camera_from_name(path.name)
    if camera and stored.camera_label != camera and stored.camera_label in (None, "", path.parent.name):
        stored.camera_label = camera
        changed = True
    if changed:
        store.update_video(stored)
    return stored


def _another_file(known: Path, found: Path) -> bool:
    """Whether a clip's recorded path is a second, existing file, not this one.

    Compared as files rather than strings, so a clip first added through a
    symlink or another spelling of the mount is not its own duplicate.
    """
    try:
        return known.exists() and not os.path.samefile(known, found)
    except OSError:
        return False


def import_expedition(store, layout: ExpeditionLayout, probe: Probe | None = None) -> ImportReport:
    """File everything the layout describes. Safe to run again after the tree changes."""
    probe = probe or _decoded_length
    report = ImportReport()
    campaign = _campaign(store, layout.name, report)
    store.set_default_campaign(campaign.id)

    # Only footage in pass folders is survey footage. Colour charts, context,
    # tilt checks and tests stay on disk but out of the library, where they
    # would only be clips nobody can cut a pass from.
    report.skipped_other = sum(len(c) for f in layout.transects for c in f.other.values()) + sum(
        len(c) for c in layout.loose.values()
    )
    report.removed_other = _forget_unused_role_clips(store, layout)

    moved: dict[uuid.UUID, uuid.UUID] = {}
    for folder in layout.transects:
        transect = _transect(store, folder, campaign, report)
        for recording in folder.recordings:
            maybe = [_clip(store, path, probe, report) for path in recording.clips]
            videos = [v for v in maybe if v is not None]
            if len(videos) < len(maybe):
                continue  # an unreadable or duplicated chapter leaves no whole swim to file
            first = videos[0]
            kept = store.list_passes(video_id=first.id)
            if kept:
                report.passes_kept += 1
                report.passes_refiled += _follow_the_folder(store, kept, transect, recording, campaign, moved)
                continue
            store.add_pass(TransectPass(
                transect_id=transect.id,
                video_id=first.id,
                extra_video_ids=[v.id for v in videos[1:]],
                begin_s=0.0,
                end_s=sum(v.duration_s or 0.0 for v in videos),
                direction=recording.direction,
                campaign_id=campaign.id,
                label=recording.pass_dir,
                surveyed_on=(first.captured_at or "").split("T")[0] or None,
            ))
            report.passes_created += 1
    _retire_renamed(store, moved, {f.name.lower() for f in layout.transects}, report)
    return report


def _forget_unused_role_clips(store, layout: ExpeditionLayout) -> int:
    """Take out the non-pass clips an earlier import added, while nothing uses them.

    Earlier builds imported colour charts, context and loose clips with a
    ``[role]`` tag. A clip is only removed when it is still exactly that: in this
    expedition's folder, outside any pass folder, with no pass, no run, and no
    word from a person beyond the tag. Only the library record goes; the file
    on disk is never touched.
    """
    root = str(layout.root.resolve())
    used = {v for p in store.list_passes() for v in p.video_ids()}
    removed = 0
    for video in store.list_videos():
        path = Path(video.path)
        if video.id in used or not video.notes.startswith("[") or "]" not in video.notes:
            continue
        tag, _, rest = video.notes[1:].partition("]")
        if rest.strip() or any(getattr(video, f) != d for f, d in VideoAsset.REVIEW_DEFAULTS.items() if f != "notes"):
            continue  # somebody has described it
        # Only the tag earlier builds wrote: the clip's own folder name (or
        # "<transect>/<folder>" for a folder of a transect without passes). A
        # note like "[blurry]" is a person's and is left alone.
        if tag != path.parent.name and not tag.endswith("/" + path.parent.name):
            continue
        try:
            inside = str(path.resolve()).startswith(root + "/")
        except OSError:
            inside = False
        if not inside or any(_PASS_DIR.match(part) for part in path.parts):
            continue
        try:
            store.delete_video(video.id)
        except ValueError:
            continue  # a run reaches it, so it stays
        removed += 1
    return removed


# --- placing results -----------------------------------------------------------


def _direction_dir(clip_path: Path) -> Path | None:
    """The fwd/bwd folder a clip sits in (directly or under RAW), when the tree says so."""
    parent = clip_path.parent
    if parent.name.upper() == RAW_DIR:
        parent = parent.parent
    if parent.name.lower() in DIRECTIONS and _PASS_DIR.match(parent.parent.name):
        return parent
    return None


def results_dir_for(out_root: Path, first_clip: VideoAsset, pass_id: uuid.UUID) -> str | None:
    """``VC_6m_01/pass_01_control/fwd/RESULTS/Cam1__1a2b3c4d``, relative to ``out_root``.

    The base name of a pass's runs when its clip sits in an expedition tree
    under this root; None otherwise, and the caller names the run the way it
    always has. The camera (or the clip's own name) comes first so the cameras
    of one pass read side by side; the pass's short id keeps two passes from one
    camera apart, so every attempt number counts attempts at one pass.
    """
    direction_dir = _direction_dir(Path(first_clip.path))
    if direction_dir is None:
        return None
    try:
        relative = direction_dir.resolve().relative_to(out_root.expanduser().resolve())
    except (OSError, ValueError):
        return None
    stem = first_clip.camera_label or Path(first_clip.file_name).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "run"
    return (relative / RESULTS_DIR / f"{stem}__{pass_id.hex[:8]}").as_posix()


def is_expedition_root(root: Path) -> bool:
    """Whether a folder looks like an expedition tree: some transect with a pass folder."""
    return any(_PASS_DIR.match(grand.name) for child in _subdirs(root) for grand in _subdirs(child))
