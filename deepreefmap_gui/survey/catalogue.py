"""Reconcile run folders on disk with the survey database for browsing.

The filesystem is the source of truth for which runs exist; the database is
the source of truth for how they relate to transects. Manifests are never
rewritten here: they snapshot what the run believed when it executed, and
rebuild_from_scan depends on that staying intact.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from deepreefmap_gui.io.atomic import atomic_write_json
from deepreefmap_gui.survey.jobs import clip_states
from deepreefmap_gui.survey.models.campaign import Campaign
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.site import Site
from deepreefmap_gui.survey.models.survey_batch import SurveyBatch, session_label
from deepreefmap_gui.survey.models.transect import Transect
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.statuses import (
    CLIP_FAILED,
    CLIP_PENDING,
    CLIP_PROCESSED,
    CLIP_UNPROCESSED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOME_UNFINISHED,
    status_outcome,
)
from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)

UNASSIGNED_TITLE = "Not assigned yet"

# Runs from before sessions were recorded, or copied in from elsewhere without
# a manifest that names one. Not an error: they process and compare like any
# other, they just cannot be read as a day's work.
UNFILED_SESSION_TITLE = "No session recorded"

# Neither a site nor a campaign is required of a pass, so these buckets are the
# ordinary case for a laptop that has never met a registry, not a backlog.
NO_SITE_TITLE = "No site recorded"
NO_CAMPAIGN_TITLE = "No campaign recorded"

# Re-exported from statuses.py under the names the browser uses for them.
RUN_SUCCEEDED, RUN_FAILED, RUN_UNFINISHED = (
    OUTCOME_SUCCEEDED,
    OUTCOME_FAILED,
    OUTCOME_UNFINISHED,
)
VIDEO_UNPROCESSED, VIDEO_PENDING, VIDEO_FAILED, VIDEO_PROCESSED = (
    CLIP_UNPROCESSED,
    CLIP_PENDING,
    CLIP_FAILED,
    CLIP_PROCESSED,
)


def entry_status(entry: RunEntry) -> str:
    """The run's own status. A directory holding a manifest is a finished run.

    The database overrides the manifest only for ``failed`` and ``cancelled``:
    a failure can be recorded after the manifest is written. ``interrupted``
    does not override, because reconcile-on-open stamps complete runs too.
    """
    if entry.incomplete or entry.data_missing:
        return entry.status_label
    run = entry.db_run
    if run is not None and run.status in ("failed", "cancelled"):
        return run.status
    return "succeeded"


def entry_outcome(entry: RunEntry) -> str:
    """Which filter bucket a run falls into."""
    return status_outcome(entry_status(entry))


@dataclass(slots=True)
class RunEntry:
    """One run directory plus everything known about it from manifest and database."""

    run_dir: Path
    dir_name: str
    manifest: dict
    display_name: str
    sort_key: float
    video_hashes: list[str]
    video_name: str | None
    begin_s: float | None
    end_s: float | None
    duration_s: float | None
    points: int | None
    manifest_run_id: uuid.UUID | None
    manifest_pass_id: uuid.UUID | None
    manifest_transect_id: uuid.UUID | None
    manifest_transect_name: str | None
    manifest_direction: str | None
    manifest_batch_id: uuid.UUID | None = None
    # Older manifests carry the session's typed name; newer ones its start.
    manifest_batch_name: str | None = None
    manifest_batch_created_at: str | None = None
    db_run: RunRecord | None = None
    db_pass: TransectPass | None = None
    db_transect_name: str | None = None
    db_campaign_name: str | None = None
    db_site_name: str | None = None
    db_session_created_at: str | None = None
    # When the footage was shot, as opposed to when the run was made. Only the
    # database knows it: the manifest records which clips went in, not when they
    # were recorded.
    db_captured_at: str | None = None
    moved_from: str | None = None
    size_bytes: int | None = None
    # A run directory that never wrote a manifest: crashed, cancelled, or still
    # in flight. Carries an empty manifest, so the fields above stay at defaults.
    incomplete: bool = False
    # A run record whose directory is gone: the outputs were deleted, here or
    # in a file manager, and only the history remains. run_dir points at where
    # the directory would be, so nothing may read through it.
    data_missing: bool = False

    @property
    def status_label(self) -> str:
        """What to show for an incomplete run: the recorded status, or a generic
        marker when the database has no row for it."""
        if self.db_run is not None:
            return self.db_run.status
        return "incomplete"

    @property
    def transect_id(self) -> uuid.UUID | None:
        if self.db_pass is not None:
            return self.db_pass.transect_id
        return self.manifest_transect_id

    @property
    def campaign_id(self) -> uuid.UUID | None:
        """The trip this run's swim belongs to. No manifest records one."""
        return self.db_pass.campaign_id if self.db_pass is not None else None

    @property
    def campaign_name(self) -> str | None:
        return self.db_campaign_name

    @property
    def site_name(self) -> str | None:
        """The reef, which a run reaches through its transect rather than directly."""
        return self.db_site_name

    @property
    def session_id(self) -> uuid.UUID | None:
        """The session this run was queued in, database first then manifest.

        The run's own session outranks the pass's, which only says where the
        pass was first catalogued. The manifest is the last resort: a run
        folder copied off another machine has no row here until
        rebuild_from_scan writes one.
        """
        if self.db_run is not None and self.db_run.batch_id is not None:
            return self.db_run.batch_id
        if self.db_pass is not None and self.db_pass.batch_id is not None:
            return self.db_pass.batch_id
        return self.manifest_batch_id

    @property
    def session_name(self) -> str | None:
        started = self.db_session_created_at or self.manifest_batch_created_at
        if started:
            return session_label(started)
        # A run written before sessions were identified by their start still
        # carries whatever it was called.
        return self.manifest_batch_name

    @property
    def transect_name(self) -> str | None:
        return self.db_transect_name or self.manifest_transect_name

    @property
    def direction(self) -> str | None:
        if self.db_pass is not None:
            return self.db_pass.direction
        return self.manifest_direction

    @property
    def recorded_at(self) -> datetime | None:
        """When the clip was shot, aware, or None when nothing recorded it."""
        return parse_run_timestamp(self.db_captured_at)


@dataclass(slots=True)
class GroupStats:
    run_count: int
    total_bytes: int | None
    duration_range: tuple[float, float] | None
    point_range: tuple[int, int] | None


@dataclass(slots=True)
class FacetGroup:
    key: tuple
    title: str
    entries: list[RunEntry] = field(default_factory=list)
    children: list[FacetGroup] = field(default_factory=list)

    def all_entries(self) -> list[RunEntry]:
        collected = list(self.entries)
        for child in self.children:
            collected.extend(child.all_entries())
        return collected


def parse_run_timestamp(ts: object) -> datetime | None:
    """An ISO-8601 run timestamp as an aware datetime, or None if unusable.

    A timestamp with no offset is read as UTC, which is what writes it: the
    pipeline uses datetime.now(timezone.utc) and the survey store records the
    same value. Left alone, fromisoformat gives a naive datetime whose
    .timestamp() assumes local time, so an older naive manifest sorts against a
    newer offset-carrying one by the local UTC offset -- hours out, and in the
    wrong direction depending on which side of UTC the machine sits.
    """
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def run_sort_key(manifest: dict, mtime: float) -> float:
    """Prefer the recorded run timestamp; fall back to the manifest file mtime."""
    parsed = parse_run_timestamp(manifest.get("run_timestamp"))
    return parsed.timestamp() if parsed is not None else mtime


def run_duration_s(manifest: dict) -> float | None:
    """Total wall-clock run time; sums stage_durations for pre-field manifests."""
    v = manifest.get("run_duration_s")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    stages = manifest.get("stage_durations") or {}
    total = sum(s for s in stages.values() if isinstance(s, (int, float)))
    return total if total > 0 else None


def scan_out_root(out_root: Path) -> list[RunEntry]:
    """Every child directory with a run manifest, newest first."""
    entries: list[RunEntry] = []
    if not out_root.is_dir():
        return entries
    for child in out_root.iterdir():
        manifest_path = child / "run_manifest.json"
        if not (child.is_dir() and manifest_path.exists()):
            continue
        manifest: dict = {}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Unreadable manifest in %s", child)
        entries.append(_entry_from_manifest(child, manifest, manifest_path.stat().st_mtime))
    entries.sort(key=lambda e: e.sort_key, reverse=True)
    return entries


def _entry_from_manifest(run_dir: Path, manifest: dict, mtime: float) -> RunEntry:
    name = manifest.get("name")
    # The name the person gave it, alone. The folder is machine-safe and
    # unique but says nothing, and it is one hover away in the row's tooltip
    # and spelled out in the detail pane.
    display = str(name).strip() if isinstance(name, str) and name.strip() else run_dir.name
    videos = manifest.get("input_videos") or []
    survey = _dict_or_empty(manifest.get("survey"))
    pass_block = _dict_or_empty(survey.get("pass"))
    transect_block = _dict_or_empty(survey.get("transect"))
    points = manifest.get("semantic_reference_points") or manifest.get("metric_points")
    return RunEntry(
        run_dir=run_dir,
        dir_name=run_dir.name,
        manifest=manifest,
        display_name=display,
        sort_key=run_sort_key(manifest, mtime),
        video_hashes=[h for h in (manifest.get("video_hashes") or []) if h],
        video_name=Path(videos[0]).name if videos else None,
        begin_s=_as_float(manifest.get("begin_s")),
        end_s=_as_float(manifest.get("end_s")),
        duration_s=run_duration_s(manifest),
        points=int(points) if isinstance(points, (int, float)) else None,
        manifest_run_id=_as_uuid(survey.get("run_id")),
        manifest_pass_id=_as_uuid(pass_block.get("id")),
        manifest_transect_id=_as_uuid(transect_block.get("id")),
        manifest_transect_name=transect_block.get("name"),
        manifest_direction=pass_block.get("direction"),
        manifest_batch_id=_as_uuid(survey.get("batch_id")),
        manifest_batch_name=survey.get("batch_name"),
        manifest_batch_created_at=survey.get("batch_created_at"),
    )


def scan_incomplete_runs(
    out_root: Path, store: SurveyStore | None, known: set[str]
) -> list[RunEntry]:
    """Child directories that look like runs but never wrote a manifest.

    ``scan_out_root`` skips these, which hides crashed or interrupted runs from
    the browser. A folder counts as an incomplete run when the database still has
    a run record for it (the survey batch writes one before the pipeline starts)
    or when it holds a ``run.log`` a run left behind. ``known`` names the folders
    already surfaced as complete runs, so nothing is listed twice.
    """
    entries: list[RunEntry] = []
    if not out_root.is_dir():
        return entries
    records = {r.run_dir_name for r in store.list_runs()} if store is not None else set()
    for child in out_root.iterdir():
        if not child.is_dir() or child.name in known:
            continue
        if (child / "run_manifest.json").exists():
            continue
        if child.name not in records and not (child / "run.log").exists():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append(_incomplete_entry(child, mtime))
    return entries


def missing_run_entries(
    out_root: Path, store: SurveyStore, known: set[str]
) -> list[RunEntry]:
    """Run records whose directory is gone.

    The record is the run's history and outlives its outputs, so a folder
    deleted here or in a file manager still leaves a row to show. ``known``
    names the folders already surfaced, complete or incomplete.
    """
    entries: list[RunEntry] = []
    for run in store.list_runs():
        if run.run_dir_name in known:
            continue
        try:
            if (out_root / run.run_dir_name).is_dir():
                continue
        except OSError:
            continue
        entries.append(_missing_entry(out_root, run))
    return entries


def _missing_entry(out_root: Path, run: RunRecord) -> RunEntry:
    stamp = parse_run_timestamp(run.started_at) or parse_run_timestamp(run.created_at)
    return RunEntry(
        run_dir=out_root / run.run_dir_name,
        dir_name=run.run_dir_name,
        manifest={},
        display_name=run.run_dir_name,
        sort_key=stamp.timestamp() if stamp is not None else 0.0,
        video_hashes=[],
        video_name=None,
        begin_s=None,
        end_s=None,
        duration_s=None,
        points=None,
        manifest_run_id=None,
        manifest_pass_id=None,
        manifest_transect_id=None,
        manifest_transect_name=None,
        manifest_direction=None,
        data_missing=True,
    )


def _incomplete_entry(run_dir: Path, mtime: float) -> RunEntry:
    return RunEntry(
        run_dir=run_dir,
        dir_name=run_dir.name,
        manifest={},
        display_name=run_dir.name,
        sort_key=mtime,
        video_hashes=[],
        video_name=None,
        begin_s=None,
        end_s=None,
        duration_s=None,
        points=None,
        manifest_run_id=None,
        manifest_pass_id=None,
        manifest_transect_id=None,
        manifest_transect_name=None,
        manifest_direction=None,
        incomplete=True,
    )


def _dict_or_empty(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _as_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def reconcile(entries: list[RunEntry], store: SurveyStore) -> None:
    """Attach database rows to scanned entries. The database wins over the
    manifest snapshot; a disagreement records where the run was originally filed."""
    runs = {r.run_dir_name: r for r in store.list_runs()}
    passes = {p.id: p for p in store.list_passes()}
    transects = {t.id: t for t in store.list_transects()}
    batches = {b.id: b for b in store.list_batches()}
    videos = {v.id: v for v in store.list_videos()}
    sites = {s.id: s for s in store.list_sites()}
    campaigns = {c.id: c for c in store.list_campaigns()}
    for entry in entries:
        run = runs.get(entry.dir_name)
        if run is None:
            continue
        entry.db_run = run
        pass_ = passes.get(run.pass_id)
        session_id = run.batch_id or (pass_.batch_id if pass_ is not None else None)
        batch = batches.get(session_id) if session_id is not None else None
        entry.db_session_created_at = batch.created_at if batch is not None else None
        if pass_ is None:
            continue
        entry.db_pass = pass_
        video = videos.get(pass_.video_id)
        if video is not None:
            entry.db_captured_at = video.captured_at or video.mtime
        transect_id = pass_.transect_id
        if transect_id is not None and transect_id not in transects:
            # A line the registry has retired still named the swim this run
            # processed. Leaving it blank files the run as belonging to no
            # transect, and Browse then asks somebody to assign it to a line
            # nothing lists any more. Looked up once per line, into the same map.
            retired = store.get_transect_for_reference(transect_id)
            if retired is not None:
                transects[transect_id] = retired
        transect = transects.get(transect_id) if transect_id is not None else None
        entry.db_transect_name = transect.name if transect is not None else None
        # Site and campaign are resolved the same way and for the same reason: a
        # row the registry has withdrawn still names work that happened.
        site_id = transect.site_id if transect is not None else None
        if site_id is not None and site_id not in sites:
            withdrawn = store.get_site(site_id)
            if withdrawn is not None:
                sites[site_id] = withdrawn
        site = sites.get(site_id) if site_id is not None else None
        entry.db_site_name = site.name if site is not None else None
        campaign_id = pass_.campaign_id
        if campaign_id is not None and campaign_id not in campaigns:
            retired_trip = store.get_campaign_for_reference(campaign_id)
            if retired_trip is not None:
                campaigns[campaign_id] = retired_trip
        campaign = campaigns.get(campaign_id) if campaign_id is not None else None
        entry.db_campaign_name = campaign.name if campaign is not None else None
        if (
            entry.manifest_transect_name
            and entry.db_transect_name
            and entry.db_transect_name != entry.manifest_transect_name
        ):
            entry.moved_from = entry.manifest_transect_name


def group_key(entry: RunEntry) -> tuple:
    """Runs of the same footage and trim share a key, so reruns stay together."""
    if entry.db_pass is not None:
        return ("pass", str(entry.db_pass.id))
    if entry.manifest_pass_id is not None:
        return ("pass", str(entry.manifest_pass_id))
    video = entry.video_hashes[0] if entry.video_hashes else None
    return ("adhoc", video, _rounded(entry.begin_s), _rounded(entry.end_s))


def _rounded(value: float | None) -> float | None:
    # Two decimals, matching reproducibility_groups in survey/analysis.py.
    return None if value is None else round(value, 2)


def group_stats(entries: list[RunEntry]) -> GroupStats:
    sizes = [e.size_bytes for e in entries if e.size_bytes is not None]
    durations = [e.duration_s for e in entries if e.duration_s is not None]
    points = [e.points for e in entries if e.points is not None]
    return GroupStats(
        run_count=len(entries),
        total_bytes=sum(sizes) if sizes else None,
        duration_range=(min(durations), max(durations)) if durations else None,
        point_range=(min(points), max(points)) if points else None,
    )


def transects_facet(
    entries: list[RunEntry], transects: Iterable[Transect] = ()
) -> list[FacetGroup]:
    """Transect groups over pass groups, with unassigned runs surfaced first.

    ``transects`` may list known transects so ones without runs still appear.
    """
    by_transect: dict[str, FacetGroup] = {}
    unassigned = FacetGroup(key=("unassigned",), title=UNASSIGNED_TITLE)
    for transect in transects:
        by_transect[transect.name] = FacetGroup(key=("transect", str(transect.id)), title=transect.name)
    for entry in entries:
        name = entry.transect_name
        if name is None:
            unassigned.entries.append(entry)
            continue
        group = by_transect.get(name)
        if group is None:
            tid = str(entry.transect_id) if entry.transect_id else name
            group = by_transect[name] = FacetGroup(key=("transect", tid), title=name)
        _child_for(group, group_key(entry), _pass_title(entry)).entries.append(entry)
    groups = [g for name, g in sorted(by_transect.items())]
    if unassigned.entries:
        groups.insert(0, unassigned)
    return groups


def _named_facet(
    entries: list[RunEntry],
    kind: str,
    name_of: Callable[[RunEntry], str | None],
    id_of: Callable[[RunEntry], uuid.UUID | None],
    known: Iterable[tuple[uuid.UUID, str]],
    unassigned_title: str,
) -> list[FacetGroup]:
    """Runs grouped under one catalogue record, flat.

    Flat rather than nested under a pass group the way transects_facet is: a
    site or a campaign gathers work from several lines, and a second level of
    the same pass headings under each would say nothing the transect facet does
    not say better.
    """
    by_name: dict[str, FacetGroup] = {}
    unassigned = FacetGroup(key=("unassigned",), title=unassigned_title)
    for record_id, name in known:
        by_name[name] = FacetGroup(key=(kind, str(record_id)), title=name)
    for entry in entries:
        title = name_of(entry)
        if title is None:
            unassigned.entries.append(entry)
            continue
        group = by_name.get(title)
        if group is None:
            # A record the caller did not list, which a withdrawn one is: its
            # own id keys the group where there is one, its name where there
            # is not, the way transects_facet keys a retired line.
            own_id = id_of(entry)
            group = by_name[title] = FacetGroup(key=(kind, str(own_id) if own_id else title), title=title)
        group.entries.append(entry)
    groups = [g for _name, g in sorted(by_name.items())]
    if unassigned.entries:
        groups.insert(0, unassigned)
    return groups


def sites_facet(entries: list[RunEntry], sites: Iterable[Site] = ()) -> list[FacetGroup]:
    """Site groups, with runs on no site surfaced first.

    ``sites`` may list known sites so ones without runs still appear.
    """
    return _named_facet(
        entries,
        "site",
        lambda e: e.site_name,
        lambda _e: None,
        ((site.id, site.name) for site in sites),
        NO_SITE_TITLE,
    )


def campaigns_facet(entries: list[RunEntry], campaigns: Iterable[Campaign] = ()) -> list[FacetGroup]:
    """Campaign groups, with runs on no campaign surfaced first."""
    return _named_facet(
        entries,
        "campaign",
        lambda e: e.campaign_name,
        lambda e: e.campaign_id,
        ((campaign.id, campaign.name) for campaign in campaigns),
        NO_CAMPAIGN_TITLE,
    )


def session_group_key(batch_id: uuid.UUID | None) -> tuple:
    """The facet key a run is filed under by session.

    A two-sided join: a run reaches this from its pass or from its manifest, and
    a session reaches it from the store, so both have to arrive at the same key
    or one session would appear twice.
    """
    return ("unfiled_session",) if batch_id is None else ("session", str(batch_id))


def sessions_facet(
    entries: list[RunEntry], batches: Iterable[SurveyBatch] = ()
) -> list[FacetGroup]:
    """Session groups over pass groups, newest session first.

    The session is the only container that spans transects: everything else here
    groups by one line or one pass, so a day's work had no way to be read as a
    day's work. It is recorded on every run already, in the manifest and on the
    pass, and this is what finally shows it.

    ``batches`` may list known sessions so one whose runs have all been deleted
    still appears, the same courtesy transects_facet extends to a transect with
    no runs.
    """
    known = {b.id: b for b in batches}
    by_session: dict[tuple, FacetGroup] = {}
    unfiled = FacetGroup(key=session_group_key(None), title=UNFILED_SESSION_TITLE)
    for batch in known.values():
        by_session[session_group_key(batch.id)] = FacetGroup(
            key=session_group_key(batch.id), title=batch.label
        )
    for entry in entries:
        batch_id = entry.session_id
        if batch_id is None:
            unfiled.entries.append(entry)
            continue
        key = session_group_key(batch_id)
        group = by_session.get(key)
        if group is None:
            named = known.get(batch_id)
            title = named.label if named is not None else entry.session_name
            group = by_session[key] = FacetGroup(key=key, title=title or str(batch_id))
        _child_for(group, group_key(entry), _pass_title(entry)).entries.append(entry)
    # Newest first: a session is a day's work, and the one you want is almost
    # always the one you just did. Sessions with no runs sort by name alone,
    # which puts a freshly created one at the top under the date default.
    groups = sorted(by_session.values(), key=_session_sort_key, reverse=True)
    if unfiled.entries:
        groups.insert(0, unfiled)
    return groups


def _session_sort_key(group: FacetGroup) -> tuple:
    entries = group.all_entries()
    return (max(e.sort_key for e in entries) if entries else 0.0, group.title)


def session_summary(group: FacetGroup) -> str:
    """"6 runs · 3 transects", the two counts that say what a session covered."""
    entries = group.all_entries()
    transects = {e.transect_name for e in entries if e.transect_name}
    parts = [_plural(len(entries), "run")]
    if transects:
        parts.append(_plural(len(transects), "transect"))
    return " · ".join(parts)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# The window label for a run that took the whole clip.
_WHOLE_VIDEO = "whole video"


# Whether the file a clip names is still where the survey last saw it. Checked
# off the paint path, so "unknown" is the honest answer until it has been: an
# entry that assumed "linked" would show a link on a clip that is not there.
LINK_LINKED, LINK_MISSING, LINK_UNKNOWN = "linked", "missing", "unknown"


@dataclass(slots=True)
class VideoLibraryEntry:
    """One imported clip, with how much of the survey hangs off it.

    A video the browser can show even with no runs: an orphan is a clip imported
    but never turned into a pass, which the run-oriented facets cannot surface.
    """

    video: VideoAsset
    pass_count: int
    run_count: int
    # The passes cut from this clip and every run they produced, so a detail
    # pane can show what became of the footage without re-querying per row.
    passes: list[TransectPass] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    # Whether the file is still there. Filled in by a background pass rather
    # than at construction, because this is built while the rail is being drawn
    # and a stat per clip on a sleeping external drive is not a paint-time cost.
    link_state: str = LINK_UNKNOWN
    queued_pass_ids: set[str] = field(default_factory=set)

    @property
    def orphan(self) -> bool:
        return self.pass_count == 0

    @property
    def last_run_at(self) -> str | None:
        """When this clip was last processed, whatever the outcome."""
        return max((run.created_at for run in self.runs), default=None)


    @property
    def outcome(self) -> str:
        """Where this clip stands: unprocessed, failing, or done.

        A clip is processed when every pass's latest attempt succeeded and
        no pass has queued or active work.
        """
        if self.orphan:
            return VIDEO_UNPROCESSED
        states = clip_states(self)
        if any(state in {"failed", "interrupted", "incomplete"} for state in states):
            return VIDEO_FAILED
        if states and all(state == "succeeded" for state in states):
            return VIDEO_PROCESSED
        return VIDEO_PENDING


def resolve_link_states(entries: Iterable[VideoLibraryEntry]) -> dict[str, str]:
    """Stat each clip once and say whether it is still there.

    Keyed by path rather than by clip id so the answer survives the library
    being rebuilt, which happens on every refresh. Blocking, and meant for a
    worker thread: a missing network mount can take seconds to admit it.
    """
    states: dict[str, str] = {}
    for entry in entries:
        path = entry.video.path
        if path in states:
            continue
        if not path:
            states[path] = LINK_UNKNOWN
            continue
        try:
            states[path] = LINK_LINKED if Path(path).is_file() else LINK_MISSING
        except OSError:
            # A path that cannot even be asked about is not the same as one that
            # answered no, and offering to relocate a clip on a mount that is
            # merely slow would be wrong.
            states[path] = LINK_UNKNOWN
    return states


# The end of a window is the last frame of it, and a seek that lands exactly on
# it can fall past the end of the file and read nothing.
_END_NUDGE_S = 0.04


def _chapter_at(
    chapters: list[VideoAsset], t_s: float
) -> tuple[str, float] | None:
    """Which chapter holds a time, and where in that chapter it falls.

    The last chapter takes anything left over: its length is the one that need
    not be known, since nothing comes after it to be pushed along.
    """
    offset = 0.0
    for index, asset in enumerate(chapters):
        if index == len(chapters) - 1:
            return asset.path, max(0.0, t_s - offset)
        duration = asset.duration_s
        if not duration or duration <= 0:
            return None
        if t_s < offset + duration:
            return asset.path, t_s - offset
        offset += duration
    return None


def preview_points(
    pass_: TransectPass, assets: Mapping[uuid.UUID, VideoAsset]
) -> list[tuple[str, float]] | None:
    """Where to grab the start, middle and end frames of a section.

    ``begin_s`` and ``end_s`` are offsets into the pass's chapters played back to
    back, so each has to be walked onto the chapter that actually holds it.
    None when a chapter is unknown or its length is, since a frame from the
    wrong part of the footage is worse than no frame at all.
    """
    chapters = []
    for video_id in pass_.video_ids():
        asset = assets.get(video_id)
        if asset is None or not asset.path:
            return None
        chapters.append(asset)
    end = max(pass_.begin_s, pass_.end_s - _END_NUDGE_S)
    points = []
    for t_s in (pass_.begin_s, (pass_.begin_s + pass_.end_s) / 2.0, end):
        placed = _chapter_at(chapters, t_s)
        if placed is None:
            return None
        points.append(placed)
    return points


def video_library(
    videos: Iterable[VideoAsset],
    passes: Iterable[TransectPass],
    runs: Iterable[RunRecord] = (),
) -> list[VideoLibraryEntry]:
    """Every imported clip with its pass and run counts, orphans included.

    Orphan detection is the point: a clip referenced by no pass never appears in
    the transect or video facets, which only know clips that produced a run.
    """
    passes = list(passes)
    # Every chapter of a pass counts, or the second half of a swim the camera
    # split at 4 GB would read as a clip nothing has ever used.
    passes_per_video: Counter[uuid.UUID] = Counter()
    videos_by_pass: dict[uuid.UUID, list[uuid.UUID]] = {}
    for p in passes:
        videos_by_pass[p.id] = p.video_ids()
        passes_per_video.update(videos_by_pass[p.id])
    runs_per_video: Counter[uuid.UUID] = Counter()
    passes_by_video: dict[uuid.UUID, list[TransectPass]] = {}
    runs_by_video: dict[uuid.UUID, list[RunRecord]] = {}
    for p in passes:
        for video_id in videos_by_pass[p.id]:
            passes_by_video.setdefault(video_id, []).append(p)
    for run in runs:
        for video_id in videos_by_pass.get(run.pass_id, []):
            runs_per_video[video_id] += 1
            runs_by_video.setdefault(video_id, []).append(run)
    return [
        VideoLibraryEntry(
            video=video,
            pass_count=passes_per_video.get(video.id, 0),
            run_count=runs_per_video.get(video.id, 0),
            passes=passes_by_video.get(video.id, []),
            runs=runs_by_video.get(video.id, []),
        )
        for video in videos
    ]


def _child_for(parent: FacetGroup, key: tuple, title: str) -> FacetGroup:
    for child in parent.children:
        if child.key == key:
            return child
    child = FacetGroup(key=key, title=title)
    parent.children.append(child)
    return child


def _window_title(entry: RunEntry) -> str:
    if entry.begin_s is None and entry.end_s is None:
        label = _WHOLE_VIDEO
    else:
        begin = f"{entry.begin_s:g}" if entry.begin_s is not None else "0"
        end = f"{entry.end_s:g}" if entry.end_s is not None else "end"
        label = f"{begin}-{end} s"
    name = entry.transect_name
    return f"{label} · {name}" if name else label


def _pass_title(entry: RunEntry) -> str:
    """The clip a pass was cut from, and the window only where there is one.

    An untrimmed pass used to spell out "whole video", which is the same phrase
    on most rows, and the direction, which the rail draws as an arrow. Both took
    width from the clip name, which is the part that says which pass this is.
    """
    parts = [entry.video_name or "unknown video"]
    if entry.begin_s is not None or entry.end_s is not None:
        parts.append(_window_title(entry).split(" · ")[0])
    return " · ".join(parts)


def dir_size_bytes(run_dir: Path) -> int:
    total = 0
    for p in run_dir.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def rename_run(run_dir: Path, new_name: str) -> dict:
    """Set the display name in the run manifest, atomically."""
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = new_name.strip()
    atomic_write_json(manifest_path, manifest)
    return manifest


def delete_run(out_root: Path, run_dir: Path, store: SurveyStore | None) -> None:
    """Remove a finished run directory and its database row. Only direct children
    of the output root that carry a manifest are ever deleted."""
    if not (run_dir.resolve() / "run_manifest.json").exists():
        raise ValueError(f"{run_dir} has no run manifest")
    delete_run_dir(out_root, run_dir, store)


def delete_run_dir(out_root: Path, run_dir: Path, store: SurveyStore | None) -> None:
    """Remove a run directory that may have no manifest (a crashed run) plus any
    database row. The direct-child guard still holds: only folders sitting
    directly under the output root are ever removed."""
    delete_run_data(out_root, run_dir)
    if store is not None:
        run = store.run_by_dir_name(run_dir.resolve().name)
        if run is not None:
            store.delete_run(run.id)


def require_run_dir(out_root: Path, run_dir: Path) -> Path:
    """The resolved run directory, once it is proven to be one.

    The one gate everything that deletes run data goes through, so a bug that
    hands over a path from somewhere else cannot reach a filesystem call. Direct
    children of the output root only: a run directory is never nested, and
    anything deeper is somebody's own folder.
    """
    resolved = run_dir.resolve()
    if resolved.parent != out_root.resolve():
        raise ValueError(f"{run_dir} is not directly under {out_root}")
    return resolved


def delete_run_data(out_root: Path, run_dir: Path) -> None:
    """Remove a run directory and nothing else: the database row stays, so the
    run lives on as a data-removed record. Only direct children of the output
    root are ever removed."""
    shutil.rmtree(require_run_dir(out_root, run_dir))


def assign_to_transect(
    store: SurveyStore,
    entries: list[RunEntry],
    transect_id: uuid.UUID,
    direction: str | None = "forward",
) -> None:
    """File runs under a transect. Runs with a database pass are moved (sibling
    reruns of the pass move with them); runs the database has never seen are
    adopted with manifest ids kept, so a later rebuild_from_scan stays idempotent."""
    updated: set[uuid.UUID] = set()
    orphans: dict[tuple, list[RunEntry]] = {}
    for entry in entries:
        if entry.db_pass is not None:
            if entry.db_pass.transect_id != transect_id and entry.db_pass.id not in updated:
                entry.db_pass.transect_id = transect_id
                store.update_pass(entry.db_pass)
                updated.add(entry.db_pass.id)
        else:
            orphans.setdefault(group_key(entry), []).append(entry)
    for group in orphans.values():
        _adopt_group(store, group, transect_id, direction)


def ensure_pass_for_entry(
    store: SurveyStore,
    entry: RunEntry,
    transect_id: uuid.UUID | None = None,
    direction: str | None = "forward",
) -> TransectPass:
    """The database pass behind a run entry, created from the manifest if missing.

    Manifest ids are kept so a later rebuild_from_scan stays idempotent.
    ``transect_id`` assigns only when given; None leaves the pass as it is.
    Raises ValueError when an ad-hoc run's time window cannot be recovered.
    """
    pass_ = entry.db_pass
    if pass_ is None and entry.manifest_pass_id is not None:
        pass_ = store.get_pass(entry.manifest_pass_id)
    if pass_ is None:
        manifest = entry.manifest
        videos = manifest.get("input_videos") or [""]
        hashes = manifest.get("video_hashes") or [None]
        sizes = manifest.get("video_sizes") or [None]
        mtimes = manifest.get("video_mtimes") or [None]
        video = store.upsert_video(VideoAsset(
            file_name=Path(videos[0]).name or "unknown",
            path=videos[0],
            hash=hashes[0],
            size_bytes=sizes[0],
            mtime=mtimes[0],
        ))
        begin, end = _pass_window(entry)
        # A tombstoned section still owns its manifest id, so what is adopted here
        # takes a fresh one rather than colliding on the primary key.
        pass_id = entry.manifest_pass_id
        if pass_id is not None and store.holds_id("passes", pass_id):
            pass_id = None
        pass_ = TransectPass(
            id=pass_id or uuid.uuid4(),
            transect_id=transect_id,
            video_id=video.id,
            begin_s=begin,
            end_s=end,
            direction=entry.manifest_direction or direction,
        )
        store.add_pass(pass_)
    elif transect_id is not None and pass_.transect_id != transect_id:
        pass_.transect_id = transect_id
        store.update_pass(pass_)
    return pass_


def _adopt_group(
    store: SurveyStore,
    group: list[RunEntry],
    transect_id: uuid.UUID,
    direction: str | None,
) -> None:
    pass_ = ensure_pass_for_entry(store, group[0], transect_id, direction)
    for entry in group:
        if store.run_by_dir_name(entry.dir_name) is None:
            run_id = entry.manifest_run_id
            # holds_id rather than get_run: a tombstoned row still owns the id,
            # and reusing it would collide on the primary key.
            if run_id is not None and store.holds_id("runs", run_id):
                run_id = None
            store.add_run(RunRecord(
                id=run_id or uuid.uuid4(),
                pass_id=pass_.id,
                run_dir_name=entry.dir_name,
                status="succeeded",
                started_at=entry.manifest.get("run_timestamp"),
                batch_id=entry.manifest_batch_id,
            ))


def _pass_window(entry: RunEntry) -> tuple[float, float]:
    """A pass needs a concrete time range; untrimmed runs derive it from the
    processed frame count."""
    begin = entry.begin_s or 0.0
    if entry.end_s is not None and entry.end_s > begin:
        return begin, entry.end_s
    frames = entry.manifest.get("frames_processed")
    fps = entry.manifest.get("fps")
    if isinstance(frames, (int, float)) and isinstance(fps, (int, float)) and fps > 0:
        return begin, begin + float(frames) / float(fps)
    raise ValueError(f"Cannot work out the video time range for {entry.dir_name}")
