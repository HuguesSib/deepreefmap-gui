"""The archive upload queue: what this laptop offers the registry's blob store.

Two kinds of content travel. Every clip the survey knows about with a readable
file goes up as a `video` blob, and every file inside a succeeded run's
directory goes up as an `artifact` under that run. The store is addressed by
imohash, the same sampled identity a clip already carries from ingest, so
planning a pass never reads a file end to end and re-running the queue costs
one initiate per archived file and sends nothing twice.

Every part travels through the registry itself, under the device token, so no
address outside the registry's own is ever contacted. The registry answers each
part with the MD5 of what it stored, and a part whose ETag disagrees with the
buffer just sent fails the file rather than being assembled.

No Qt here, deliberately: the Server page runs this on a worker thread and
marshals progress back through signals, the same shape as `engine.py`.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepreefmap_gui.io.video_hash import hash_video
from deepreefmap_gui.survey.models import RunRecord, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.client import SyncError

logger = logging.getLogger(__name__)

KIND_VIDEO = "video"
KIND_ARTIFACT = "artifact"

STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

# What the registry holds of one clip or one run, as the badges read it.
STATE_ARCHIVED = "archived"
STATE_PARTIAL = "partial"
STATE_PENDING = "pending"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"

# The step's text, then jobs done and jobs total.
ProgressFn = Callable[[str, int, int], None]

# The speed reads over this much recent travel, so the figure holds still
# enough to read rather than jumping with every part.
_SPEED_WINDOW_S = 5.0
# ...and under this much span it is not read at all. Two parts a millisecond
# apart divide into an absurd figure, and a queue that empties that fast never
# needed a speed on it.
_SPEED_MIN_SPAN_S = 0.5


@dataclass(frozen=True)
class TransferProgress:
    """How much of the queue's content has landed, and how fast it is moving.

    ``done_bytes`` measures content on the server, so a file the registry
    already held lands in it whole without travelling. ``speed_bps`` reads only
    bytes that actually travelled this pass, so those files cannot spike it.
    None until enough has travelled over enough time to measure.
    """

    done_bytes: int
    total_bytes: int
    speed_bps: float | None = None


BytesFn = Callable[[TransferProgress], None]


class _Landed(Protocol):
    """Records bytes now on the server, saying whether they travelled to get there."""

    def __call__(self, count: int, *, travelled: bool) -> None: ...


class TransferMeter:
    """Byte progress across one pass over the queue, with a smoothed speed.

    Qt-free like the rest of this module: the worker feeds it and forwards each
    reading over a signal. ``now`` is injectable so the smoothing is testable
    without waiting.
    """

    def __init__(self, total_bytes: int, now: Callable[[], float] = time.monotonic) -> None:
        self._total = int(total_bytes)
        self._done = 0
        self._now = now
        # (moment, travelled bytes so far). Anchored at zero so the first part's
        # reading divides by the time it really took to arrive.
        self._travelled = 0
        self._samples: deque[tuple[float, int]] = deque([(now(), 0)])

    def account(self, count: int, *, travelled: bool) -> TransferProgress:
        """Record ``count`` more bytes on the server, and say where that leaves us."""
        self._done += int(count)
        if travelled and count:
            self._travelled += int(count)
            moment = self._now()
            self._samples.append((moment, self._travelled))
            # At least two always stay, or there is no span left to divide by.
            while len(self._samples) > 2 and moment - self._samples[0][0] > _SPEED_WINDOW_S:
                self._samples.popleft()
        return self.reading()

    def reading(self) -> TransferProgress:
        """The gauge's whole answer: bytes landed, bytes wanted, and the speed."""
        first, sent_by_first = self._samples[0]
        last, sent_by_last = self._samples[-1]
        span = last - first
        speed = None
        if span >= _SPEED_MIN_SPAN_S and sent_by_last > sent_by_first:
            speed = (sent_by_last - sent_by_first) / span
        return TransferProgress(self._done, self._total, speed)


class ArchiveTransport(Protocol):
    """The three calls one pass over the queue makes on a registry client."""

    def archive_initiate(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def archive_upload_part(self, object_id: str, part_number: int, chunk: bytes) -> str: ...

    def archive_complete(
        self, object_id: str, parts: Sequence[dict[str, Any]]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ArchiveJob:
    """One file to offer: a clip, or one artefact inside a run directory."""

    label: str
    path: Path
    content_hash: str
    size_bytes: int
    kind: str
    run_id: str | None = None
    relpath: str | None = None

    def initiate_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
        }
        if self.kind == KIND_ARTIFACT:
            payload["run_id"] = self.run_id
            payload["relpath"] = self.relpath
        return payload


@dataclass
class ArchiveReport:
    """What one pass over the queue did.

    ``archived`` counts files sent and assembled this pass. Failures carry their
    reason per file because the queue keeps going: re-running resumes
    server-side, so a flaky connection costs a retry rather than the whole batch.
    ``skipped`` comes from the plan: what was never offered, and why. A queue
    stopped by its cancel event says so, or "archived 3" reads as "archived all".
    """

    archived: int = 0
    already: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class ArchivePlan:
    """What a pass over the survey would send, and what it had to leave out.

    ``skipped`` names every item the plan dropped and why, because a plan that
    silently shrinks reads as "everything is on the server" once it finishes.
    ``hash_backfills`` are digests computed here for rows that had none; the
    caller writes them back on its own thread, so planning never writes to the
    store from the archive worker while the GUI thread is in the same file.
    """

    jobs: list[ArchiveJob] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    hash_backfills: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(job.size_bytes for job in self.jobs)


# --- planning -----------------------------------------------------------------


def archive_plan(store: SurveyStore, out_root: Path) -> ArchivePlan:
    """Everything worth archiving: the clips, then each succeeded run's files."""
    plan = ArchivePlan()
    for video in store.list_videos():
        _plan_video(plan, video)
    for run in store.list_runs():
        _plan_run(plan, run, out_root)
    return plan


def archive_plan_for_video(store: SurveyStore, video_id: object) -> ArchivePlan:
    """One clip's job and nothing else. Empty when its file cannot be read."""
    wanted = str(video_id)
    plan = ArchivePlan()
    for video in store.list_videos():
        if str(video.id) == wanted:
            _plan_video(plan, video)
    return plan


def archive_plan_for_run(store: SurveyStore, out_root: Path, run_id: object) -> ArchivePlan:
    """One run's artefacts and nothing else. Empty unless it succeeded and kept its directory."""
    wanted = str(run_id)
    plan = ArchivePlan()
    for run in store.list_runs():
        if str(run.id) == wanted:
            _plan_run(plan, run, out_root)
    return plan


def _plan_video(plan: ArchivePlan, video: VideoAsset) -> None:
    """This clip's job, where its file can still be read.

    Identity is the row's own ``hash``, so a blob and its registry row meet on a
    value both already hold. A row without one is hashed here and the digest
    queued for the caller to write back: imohash samples the file, so this
    costs nothing even for a 4 GB chapter.
    """
    path = Path(video.path)
    try:
        size_bytes = path.stat().st_size
        readable = path.is_file()
    except OSError:
        readable = False
    if not readable:
        plan.skipped.append((video.file_name, "the file is not where the survey last saw it"))
        return
    digest = video.hash
    if not digest:
        digest = hash_video(path)
        if not digest:
            plan.skipped.append((video.file_name, "the file could not be read to identify it"))
            return
        plan.hash_backfills.append((str(video.id), digest))
    plan.jobs.append(
        ArchiveJob(
            label=video.file_name,
            path=path,
            content_hash=digest,
            size_bytes=size_bytes,
            kind=KIND_VIDEO,
        )
    )


def _plan_run(plan: ArchivePlan, run: RunRecord, out_root: Path) -> None:
    if run.status != "succeeded":
        plan.skipped.append((run.run_dir_name, "the run did not succeed"))
        return
    run_dir = out_root / run.run_dir_name
    if not run_dir.is_dir():
        plan.skipped.append((run.run_dir_name, "its output directory is gone"))
        return
    _backfill_web_cloud(plan, run_dir, run.run_dir_name)
    _plan_run_dir(plan, run_dir, run.run_dir_name, str(run.id))


def _backfill_web_cloud(plan: ArchivePlan, run_dir: Path, run_dir_name: str) -> None:
    """Build the browser export for a run reconstructed before it existed.

    Written beside the scene here, at archive time, because this is the moment
    the run travels to where the export is viewed. A run without one arrives in
    the web console with no cloud to draw, which reads as a broken upload.
    """
    from deepreefmap_gui.io.scene_file import find_scene_file
    from deepreefmap_gui.io.web_cloud import WEB_CLOUD_FILENAME, write_web_cloud_from_scene

    label = f"{run_dir_name}/{WEB_CLOUD_FILENAME}"
    if (run_dir / WEB_CLOUD_FILENAME).exists():
        return
    scene = find_scene_file(run_dir)
    if scene is None:
        # The scene generates on first open; until then there is nothing to
        # build the export from, and the run's other files still travel.
        plan.skipped.append((label, "open the run once to build its web view first"))
        return
    try:
        built = write_web_cloud_from_scene(scene, run_dir / WEB_CLOUD_FILENAME, run_dir=run_dir)
    except Exception:
        logger.warning("Could not backfill the web cloud for %s", run_dir, exc_info=True)
        built = False
    if not built:
        plan.skipped.append((label, "the web view could not be built from the saved scene"))


def _plan_run_dir(plan: ArchivePlan, run_dir: Path, run_dir_name: str, run_id: str) -> None:
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        # Hidden entries are working state, .cache/ included, never outputs.
        if any(part.startswith(".") for part in rel.parts):
            continue
        relpath = rel.as_posix()
        label = f"{run_dir_name}/{relpath}"
        try:
            stat = path.stat()
        except OSError as exc:
            logger.info("Cannot read %s: %s", path, exc)
            plan.skipped.append((label, "the file could not be read"))
            continue
        digest = hash_video(path)
        if not digest:
            plan.skipped.append((label, "the file could not be read to identify it"))
            continue
        plan.jobs.append(
            ArchiveJob(
                label=label,
                path=path,
                content_hash=digest,
                size_bytes=stat.st_size,
                kind=KIND_ARTIFACT,
                run_id=run_id,
                relpath=relpath,
            )
        )


# --- probing --------------------------------------------------------------------


class ProbeTransport(Protocol):
    """The two bulk lookups one badge refresh makes on a registry client."""

    def archive_probe(self, hashes: Sequence[str]) -> dict[str, Any]: ...

    def archive_runs_probe(self, run_ids: Sequence[str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ArchiveStates:
    """What the registry holds right now, keyed by clip id and by run id.

    Built from a live probe and never stored: a badge painted from anything
    older would claim content is safe on a server that may no longer hold it.
    """

    videos: dict[str, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)


def probe_archive_states(
    client: ProbeTransport,
    videos: Sequence[VideoAsset],
    runs: Sequence[RunRecord],
) -> ArchiveStates:
    """Ask the registry what it holds of these clips and runs, in two calls.

    A clip with no recorded hash cannot be asked about, so it stays unknown
    rather than being hashed here: this runs on every list refresh.
    """
    hashes = {str(video.id): video.hash for video in videos if video.hash}
    blob_states: Mapping[str, Any] = {}
    if hashes:
        answer = client.archive_probe(sorted(set(hashes.values())))
        found = answer.get("states")
        blob_states = found if isinstance(found, Mapping) else {}
    video_states = {
        video_id: _clip_state(blob_states.get(digest))
        for video_id, digest in hashes.items()
    }
    run_ids = [str(run.id) for run in runs]
    run_states: dict[str, str] = {}
    if run_ids:
        answer = client.archive_runs_probe(run_ids)
        found = answer.get("states")
        counts = found if isinstance(found, Mapping) else {}
        run_states = {run_id: _run_state(counts.get(run_id)) for run_id in run_ids}
    return ArchiveStates(videos=video_states, runs=run_states)


def _clip_state(state: object) -> str:
    """One blob's probe entry as a badge state. Absent means never offered."""
    if not isinstance(state, Mapping):
        return STATE_UNKNOWN
    status = state.get("status")
    if status == STATUS_COMPLETE:
        return STATE_ARCHIVED
    if status == STATUS_FAILED:
        return STATE_FAILED
    return STATE_PENDING


def _run_state(counts: object) -> str:
    """One run's artefact tallies as a badge state. Absent means never offered."""
    if not isinstance(counts, Mapping):
        return STATE_UNKNOWN
    artifacts = int(counts.get("artifacts") or 0)
    complete = int(counts.get("complete") or 0)
    failed = int(counts.get("failed") or 0)
    if artifacts > 0 and complete == artifacts:
        return STATE_ARCHIVED
    # Failure first: a run part-archived with one refused blob needs acting on,
    # and "partial" reads as merely unfinished.
    if failed > 0:
        return STATE_FAILED
    if complete > 0:
        return STATE_PARTIAL
    return STATE_PENDING if artifacts > 0 else STATE_UNKNOWN


# --- executing ------------------------------------------------------------------


def run_archive(
    client: ArchiveTransport,
    jobs: Sequence[ArchiveJob],
    progress: ProgressFn,
    cancel_event: Any = None,
    on_bytes: BytesFn | None = None,
) -> ArchiveReport:
    """One pass over the queue. A job that fails is recorded and the rest still run.

    ``on_bytes`` gets a `TransferProgress` reading as content lands on the
    server: once up front, then after every deduplicated file and every part
    sent, so a gauge can show bytes and speed rather than a file count.
    """
    report = ArchiveReport()
    total = len(jobs)
    meter = TransferMeter(sum(job.size_bytes for job in jobs))

    def landed(count: int, *, travelled: bool) -> None:
        reading = meter.account(count, travelled=travelled)
        if on_bytes is not None:
            on_bytes(reading)

    if on_bytes is not None:
        on_bytes(meter.reading())
    for done, job in enumerate(jobs):
        if cancel_event is not None and cancel_event.is_set():
            report.cancelled = True
            break
        progress(f"Archiving {job.label}…", done, total)
        try:
            _send_one(client, job, report, progress, done, total, landed)
        except Exception as exc:
            logger.warning("Archive of %s failed: %s", job.label, exc)
            report.failed.append((job.label, str(exc)))
    return report


def _send_one(
    client: ArchiveTransport,
    job: ArchiveJob,
    report: ArchiveReport,
    progress: ProgressFn,
    done: int,
    total: int,
    landed: _Landed,
) -> None:
    """Offer one file, uploading whatever the registry says is still missing.

    The registry names the parts it already stores, so a pass interrupted mid
    file resumes from there on the next initiate rather than starting over.
    """
    answer = client.archive_initiate(job.initiate_payload())
    if answer.get("status") == STATUS_COMPLETE:
        report.already += 1
        # On the server already, which is what the gauge measures. Not
        # travelled, so the dedup cannot spike the speed.
        landed(job.size_bytes, travelled=False)
        return
    part_size = int(answer.get("part_size_bytes") or 0)
    if answer.get("status") != STATUS_PENDING or part_size < 1:
        raise SyncError(f"The registry answered an unusable upload state ({answer.get('status')}).")
    object_id = str(answer["object_id"])
    count = (job.size_bytes + part_size - 1) // part_size
    stored = {int(n) for n in answer.get("parts_done") or []}
    missing = [number for number in range(1, count + 1) if number not in stored]
    # Parts a prior pass stored are on the server before this one sends a byte.
    missing_bytes = sum(
        min(part_size, job.size_bytes - (number - 1) * part_size) for number in missing
    )
    landed(job.size_bytes - missing_bytes, travelled=False)
    parts: list[dict[str, Any]] = []
    with job.path.open("rb") as handle:
        for sent, number in enumerate(missing):
            progress(
                f"Archiving {job.label} (part {sent + 1} of {len(missing)})…",
                done,
                total,
            )
            # Only the missing parts travel, so the offset comes from the part
            # number rather than from read position.
            handle.seek((number - 1) * part_size)
            chunk = handle.read(part_size)
            etag = client.archive_upload_part(object_id, number, chunk)
            # The registry's own checksum of what it stored. Comparing it to
            # the buffer in hand is the whole integrity check, and it costs no
            # second read.
            if etag != hashlib.md5(chunk, usedforsecurity=False).hexdigest():
                raise SyncError("The registry stored a part that differs from the one sent.")
            parts.append({"part_number": number, "etag": etag})
            landed(len(chunk), travelled=True)
    client.archive_complete(object_id, parts)
    report.archived += 1
