"""The archive upload queue: what this laptop offers the registry's blob store.

Two kinds of content travel. Every clip the survey knows about with a readable
file goes up as a `video` blob, and every file inside a succeeded run's
directory goes up as an `artifact` under that run. The store is addressed by
imohash, the same sampled identity a clip already carries from ingest, so
planning a pass never reads a file end to end and re-running the queue costs
one initiate per archived file and sends nothing twice.

Integrity of the bytes is the store's own: S3 answers every part with the MD5
it computed of what it stored, and a part whose ETag disagrees with the buffer
just sent is retried rather than assembled.

No Qt here, deliberately: the Server page runs this on a worker thread and
marshals progress back through signals, the same shape as `engine.py`.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepreefmap_gui.io.video_hash import hash_video
from deepreefmap_gui.survey.models import RunRecord, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.client import ServerUnreachableError, SyncError

logger = logging.getLogger(__name__)

# One PUT moves a whole part, 32 MiB at the server's default, and a field
# uplink can be slow enough that the client timeout is what would kill it.
UPLOAD_TIMEOUT = 600.0

# Presigned URLs lapse. Re-initiating costs one request and mints a fresh set,
# so the loop does that before a part could outlive the batch it came in. The
# margin covers a part already in flight when the check is made.
PRESIGN_MARGIN = 30.0
DEFAULT_PRESIGN_TTL = 900.0

# A clip that cannot finish in this many rounds of fresh URLs is not going to.
MAX_PRESIGN_ROUNDS = 40

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


class ArchiveTransport(Protocol):
    """The two calls one pass over the queue makes on a registry client."""

    def archive_initiate(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def archive_complete(
        self, object_id: str, parts: Sequence[dict[str, Any]]
    ) -> dict[str, Any]: ...


class PresignExpiredError(SyncError):
    """A part URL lapsed before its turn. The caller re-initiates and resumes."""


def upload_part(url: str, chunk: bytes) -> str:
    """PUT one part's bytes to its presigned URL, returning the ETag it answered.

    The URL is not under the registry's address and carries its own auth in the
    query string, so no bearer token travels here. The ETag comes back quoted,
    and the completion call wants it bare.
    """
    # S310: the URL was presigned by the registry this device is enrolled with.
    request = urllib.request.Request(url, data=chunk, method="PUT")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:  # noqa: S310
            etag = response.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        # 403 is what a lapsed signature looks like from here.
        if exc.code == 403:
            raise PresignExpiredError("The part URL is no longer valid.") from exc
        raise SyncError(f"The blob store refused a part ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServerUnreachableError(f"Cannot reach the blob store: {exc}.") from exc
    if not etag:
        raise SyncError("The blob store answered a part without an ETag.")
    etag = etag.strip('"')
    # The store's own checksum of what it stored. Comparing it to the buffer in
    # hand is the whole integrity check, and it costs no second read.
    expected = hashlib.md5(chunk, usedforsecurity=False).hexdigest()
    if etag != expected:
        raise SyncError("The blob store stored a part that differs from the one sent.")
    return etag


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
) -> ArchiveReport:
    """One pass over the queue. A job that fails is recorded and the rest still run."""
    report = ArchiveReport()
    total = len(jobs)
    for done, job in enumerate(jobs):
        if cancel_event is not None and cancel_event.is_set():
            report.cancelled = True
            break
        progress(f"Archiving {job.label}…", done, total)
        try:
            _send_one(client, job, report, progress, done, total)
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
) -> None:
    """Offer one file, uploading whatever the registry says is still missing.

    Presigned URLs lapse well inside the time a 4 GB clip takes on a field
    uplink, so the loop re-initiates when the current batch is close to expiry
    and resumes from the parts the registry reports. That also makes a lapse
    that happens anyway survivable: the URL answers 403, and the next round
    mints a fresh one for the same part.
    """
    for attempt in range(MAX_PRESIGN_ROUNDS):
        answer = client.archive_initiate(job.initiate_payload())
        minted_at = time.monotonic()
        status = answer.get("status")
        if status == STATUS_COMPLETE:
            # Nothing left to send: either dedup, or an earlier round finished it.
            if attempt:
                report.archived += 1
            else:
                report.already += 1
            return
        part_size = int(answer.get("part_size_bytes") or 0)
        part_urls = answer.get("part_urls") or []
        if status != STATUS_PENDING or part_size < 1:
            raise SyncError(f"The registry answered an unusable upload state ({status}).")
        if not part_urls:
            # Every part is stored already; assemble and stop.
            client.archive_complete(str(answer["object_id"]), [])
            report.archived += 1
            return

        ttl = float(answer.get("presign_ttl_seconds") or DEFAULT_PRESIGN_TTL)
        # One part may run the whole upload timeout, so a URL is only started
        # while the batch still has room for that plus a margin.
        usable = ttl - UPLOAD_TIMEOUT - PRESIGN_MARGIN
        if usable <= 0:
            usable = ttl / 2

        if _upload_batch(job, part_urls, part_size, minted_at, usable, progress, done, total):
            client.archive_complete(str(answer["object_id"]), [])
            report.archived += 1
            return
        # Ran out of signature life. Re-initiate and carry on where S3 got to.
    raise SyncError(f"Could not finish {job.label} before its upload URLs kept lapsing.")


def _upload_batch(
    job: ArchiveJob,
    part_urls: Sequence[Mapping[str, Any]],
    part_size: int,
    minted_at: float,
    usable: float,
    progress: ProgressFn,
    done: int,
    total: int,
) -> bool:
    """PUT this batch of parts, returning whether it got through all of them."""
    with job.path.open("rb") as handle:
        for sent, part in enumerate(part_urls):
            if time.monotonic() - minted_at >= usable:
                return False
            number = int(part["part_number"])
            progress(
                f"Archiving {job.label} (part {sent + 1} of {len(part_urls)})…",
                done,
                total,
            )
            # Only the missing parts were presigned, so the offset comes from
            # the part number rather than from read position.
            handle.seek((number - 1) * part_size)
            try:
                upload_part(str(part["url"]), handle.read(part_size))
            except PresignExpiredError:
                return False
    return True
