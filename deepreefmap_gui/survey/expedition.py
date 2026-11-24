"""An expedition folder as a workspace: read its tree, file its footage, place its results.

The field team sorts footage into a tree before anyone opens the app::

    2026_11_JO_EXPE/                      the expedition (a campaign)
      VC_6m_01/                           a transect
        pass_01_control/                  a pass: one condition, every camera
          fwd/  *.MP4  or  RAW/*.MP4      the clips swum forward
          bwd/  ...                       and back
        colour_charts/  context/  ...     footage of the transect that is not a pass

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
import re
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
# GoPro names a recording GX<chapter:2><number:4>; HERO cameras before 6 used GOPR/GP.
_GOPRO = re.compile(r"G[XHL](?P<chapter>\d{2})(?P<number>\d{4})", re.IGNORECASE)

ROLE_TAG = "[{role}]"


def role_tag(role: str) -> str:
    """The marker a non-pass clip carries in its notes until the registry has a field for it."""
    return ROLE_TAG.format(role=role)


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
    unreadable: list[str] = field(default_factory=list)

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
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable")
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
                for clips in _group_chapters(_direction_clips(direction_dir)):
                    folder.recordings.append(
                        Recording(child.name, DIRECTIONS[direction_dir.name.lower()], clips)
                    )
            continue
        clips = _videos(child)
        if clips:
            folder.other[child.name] = clips
    clips_here = _videos(directory)
    if clips_here:
        folder.other[""] = clips_here
    if not folder.recordings and not folder.other:
        return None
    # Only a folder with pass folders is a transect; anything else is loose footage.
    if not folder.recordings:
        for role, clips in folder.other.items():
            key = f"{directory.name}/{role}" if role else directory.name
            layout.loose[key] = clips
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


def _transect(store, folder: TransectFolder, report: ImportReport) -> Transect:
    found = next((t for t in store.list_transects() if t.name.lower() == folder.name.lower()), None)
    if found is not None:
        if found.depth_m is None and folder.depth_m is not None:
            found.depth_m = folder.depth_m
            store.update_transect(found)
        return found
    transect = Transect(name=folder.name, depth_m=folder.depth_m)
    store.add_transect(transect)
    report.transects_created.append(folder.name)
    return transect


def _clip(store, path: Path, probe: Probe, report: ImportReport, *, note_tag: str | None = None) -> VideoAsset | None:
    measured = probe(path)
    if measured is None:
        report.unreadable.append(str(path))
        return None
    asset = VideoAsset.from_path(path)
    asset.duration_s, asset.fps = measured
    # The folder above a clip here is fwd, RAW or colour_charts, never a camera,
    # so the camera comes from the file name or stays unknown.
    asset.camera_label = camera_from_name(path.name)
    prior = store.find_video_by_hash(asset.hash) if asset.hash else store.find_video_by_path(str(path))
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
    if note_tag and note_tag not in stored.notes:
        stored.notes = f"{note_tag} {stored.notes}".strip()
        changed = True
    if changed:
        store.update_video(stored)
    return stored


def import_expedition(store, layout: ExpeditionLayout, probe: Probe | None = None) -> ImportReport:
    """File everything the layout describes. Safe to run again after the tree changes."""
    probe = probe or _decoded_length
    report = ImportReport()
    campaign = _campaign(store, layout.name, report)
    store.set_default_campaign(campaign.id)

    for folder in layout.transects:
        transect = _transect(store, folder, report)
        for role, clips in folder.other.items():
            for path in clips:
                _clip(store, path, probe, report, note_tag=role_tag(role or transect.name))
        for recording in folder.recordings:
            videos = [_clip(store, path, probe, report) for path in recording.clips]
            if any(v is None for v in videos):
                continue  # a recording with an unreadable chapter is not a whole swim
            first = videos[0]
            if store.list_passes(video_id=first.id):
                report.passes_kept += 1
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

    for role, clips in layout.loose.items():
        for path in clips:
            _clip(store, path, probe, report, note_tag=role_tag(role) if role else None)
    return report


# --- placing results -----------------------------------------------------------


def _direction_dir(clip_path: Path) -> Path | None:
    """The fwd/bwd folder a clip sits in (directly or under RAW), when the tree says so."""
    parent = clip_path.parent
    if parent.name.upper() == RAW_DIR:
        parent = parent.parent
    if parent.name.lower() in DIRECTIONS and _PASS_DIR.match(parent.parent.name):
        return parent
    return None


def results_dir_for(out_root: Path, first_clip: VideoAsset, attempt: int) -> str | None:
    """``VC_6m_01/pass_01_control/fwd/RESULTS/Cam1__r01``, relative to ``out_root``.

    None when the clip is not in an expedition tree under this root, and the
    caller names the run the way it always has. The camera names the run when it
    is known, so the four cameras of one pass land side by side; the clip's own
    name otherwise.
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
    return (relative / RESULTS_DIR / f"{stem}__r{attempt:02d}").as_posix()


def is_expedition_root(root: Path) -> bool:
    """Whether a folder looks like an expedition tree: some transect with a pass folder."""
    return any(_PASS_DIR.match(grand.name) for child in _subdirs(root) for grand in _subdirs(child))
