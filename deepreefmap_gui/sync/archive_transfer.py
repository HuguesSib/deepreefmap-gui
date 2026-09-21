"""Bounded archive scheduling, receipt verification, and retry recovery."""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from deepreefmap_gui.sync.archive import ArchiveJob, ArchiveReport, TransferMeter
from deepreefmap_gui.sync.archive_client import ArchiveCancelledError, ArchiveError
from deepreefmap_gui.sync.client import DeviceRevokedError, ServerFaultError, ServerUnreachableError

logger = logging.getLogger(__name__)
WORKERS = 4


@dataclass
class Upload:
    job: ArchiveJob
    phase: str = "initiate"
    object_id: str = ""
    part_size: int = 0
    receipts: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_part: int = 1
    count: int = 0
    active: int = 0
    failed: bool = False
    complete: bool = False


def check_file(job: ArchiveJob) -> None:
    stat = job.path.stat()
    if stat.st_size != job.size_bytes or (job.mtime_ns is not None and stat.st_mtime_ns != job.mtime_ns):
        raise OSError(f"{job.label} changed since planning; retry to rebuild its archive plan")


def retryable(exc: Exception) -> bool:
    return (isinstance(exc, ArchiveError) and exc.retryable) or isinstance(
        exc, (ServerUnreachableError, ServerFaultError)
    )


def retry(operation, cancel, renegotiate=None):
    for attempt in range(3):
        if cancel.is_set():
            raise ArchiveCancelledError("Archive cancelled")
        try:
            return operation()
        except Exception as exc:
            if not retryable(exc) or attempt == 2:
                raise
            delay = max(getattr(exc, "retry_after", 0), 2**attempt + random.uniform(0, 0.25))
            if cancel.wait(delay):
                raise ArchiveCancelledError("Archive cancelled") from exc
            if renegotiate is not None:
                renegotiate()
    raise AssertionError("Retry loop exhausted")


def send_part(client, upload: Upload, number: int, cancel) -> tuple[int, bool]:
    job = upload.job
    check_file(job)
    with job.path.open("rb") as handle:
        handle.seek((number - 1) * upload.part_size)
        chunk = handle.read(upload.part_size)
    check_file(job)
    expected = min(upload.part_size, job.size_bytes - (number - 1) * upload.part_size)
    if len(chunk) != expected:
        raise OSError(f"{job.label} changed while reading")
    digest = hashlib.md5(chunk, usedforsecurity=False).hexdigest()
    stored = upload.receipts.get(number, {})
    if stored.get("size_bytes") == len(chunk) and str(stored.get("etag", "")).strip('"').lower() == digest:
        return len(chunk), False
    etag = retry(lambda: client.archive_upload_part(upload.object_id, number, chunk), cancel)
    if str(etag).strip('"').lower() != digest:
        raise ArchiveError("The registry stored a part that differs from the one sent.", code="archive_integrity")
    return len(chunk), True


class Coordinator:
    """Owns all queue state while workers execute individual requests."""

    def __init__(self, client, jobs, progress, cancel, on_bytes, on_state):
        self.client = client
        self.jobs = list(jobs)
        self.pending = deque(self.jobs)
        self.uploads: list[Upload] = []
        self.futures: dict[Future, tuple[Upload, str]] = {}
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.on_bytes = on_bytes
        self.on_state = on_state
        self.report = ArchiveReport()
        self.meter = TransferMeter(sum(job.size_bytes for job in jobs))
        self.failures = 0
        self._last_progress = 0.0
        self._progress_lock = threading.Lock()
        if hasattr(client, "cancel_event"):
            client.cancel_event = self.cancel
            client.on_sent = self.sent

    def state(self, upload: Upload, phase: str) -> None:
        if self.on_state is not None:
            self.on_state(upload.job, phase)
        self.progress(
            f"{phase.capitalize()}: {upload.job.label}", self.report.archived + self.report.already, len(self.jobs)
        )

    def sent(self, object_id: str, number: int, count: int) -> None:
        with self._progress_lock:
            now = time.monotonic()
            if now - self._last_progress < 0.2:
                return
            self._last_progress = now
            self.progress(
                f"Sending part {number}: {count / (1024 * 1024):.1f} MiB sent; awaiting receipt",
                self.report.archived + self.report.already,
                len(self.jobs),
            )

    def account(self, count: int, travelled: bool) -> None:
        reading = self.meter.account(count, travelled=travelled)
        if self.on_bytes is not None:
            self.on_bytes(reading)

    def activate(self) -> None:
        hashes = {
            upload.job.content_hash
            for upload in self.uploads
            if not upload.complete and (not upload.failed or upload.active)
        }
        for _ in range(len(self.pending)):
            if sum(not u.complete and not u.failed for u in self.uploads) >= WORKERS:
                break
            job = self.pending.popleft()
            if job.content_hash in hashes:
                self.pending.append(job)
                continue
            hashes.add(job.content_hash)
            self.uploads.append(Upload(job))

    def schedule(self, pool) -> None:
        self.activate()
        for upload in self.uploads:
            if len(self.futures) >= WORKERS:
                return
            if upload.failed or upload.complete:
                continue
            self.schedule_upload(pool, upload)

    def schedule_upload(self, pool, upload: Upload) -> None:
        if upload.phase == "initiate" and upload.active == 0:
            self.state(upload, "preparing")
            self.submit(pool, upload, "initiate", self.initiate, upload)
        elif upload.phase == "parts":
            if upload.next_part <= upload.count:
                number = upload.next_part
                upload.next_part += 1
                self.state(upload, "uploading")
                self.submit(pool, upload, "part", send_part, self.client, upload, number, self.cancel)
            elif upload.active == 0:
                self.state(upload, "verifying")
                self.submit(pool, upload, "complete", self.complete, upload)

    def complete(self, upload: Upload):
        check_file(upload.job)
        return retry(lambda: self.client.archive_complete(upload.object_id, []), self.cancel)

    def initiate(self, upload: Upload):
        check_file(upload.job)
        return retry(lambda: self.client.archive_initiate(upload.job.initiate_payload()), self.cancel)

    def submit(self, pool, upload, phase, operation, *args) -> None:
        upload.active += 1
        self.futures[pool.submit(operation, *args)] = (upload, phase)

    def receive(self, future: Future) -> None:
        upload, phase = self.futures.pop(future)
        upload.active -= 1
        try:
            value = future.result()
            if phase == "initiate":
                self.initiated(upload, value)
            elif phase == "part":
                if value[1]:
                    self.failures = 0
                self.account(*value)
            else:
                if value.get("status") != "complete":
                    raise ArchiveError("The registry did not verify the object", code="archive_integrity")
                self.failures = 0
                self.report.archived += 1
                upload.complete = True
                self.state(upload, "archived")
        except Exception as exc:
            self.failed(upload, exc)

    def initiated(self, upload: Upload, value) -> None:
        if value.get("status") == "complete":
            self.report.already += 1
            upload.complete = True
            self.account(upload.job.size_bytes, False)
            self.state(upload, "archived")
            return
        size = int(value.get("part_size_bytes") or 0)
        if value.get("status") != "pending" or not 0 < size <= 32 * 1024 * 1024:
            raise ArchiveError("The registry returned an unusable upload state", code="archive_integrity")
        upload.object_id = str(value["object_id"])
        upload.part_size = size
        upload.count = (upload.job.size_bytes + size - 1) // size
        upload.receipts = {int(p["part_number"]): p for p in value.get("uploaded_parts", [])}
        upload.phase = "parts"

    def failed(self, upload: Upload, exc: Exception) -> None:
        if isinstance(exc, ArchiveCancelledError):
            self.report.cancelled = True
            upload.failed = True
            return
        if not upload.failed:
            self.report.failed.append((upload.job.label, str(exc)))
            logger.warning("Archive of %s failed: %s", upload.job.label, exc)
            self.state(upload, "failed")
        upload.failed = True
        fatal = isinstance(exc, DeviceRevokedError) or (
            isinstance(exc, ArchiveError)
            and (exc.status in {401, 403} or exc.code in {"archive_integrity", "archive_storage_incompatible"})
        )
        if retryable(exc):
            self.failures += 1
        if fatal or self.failures >= 3:
            self.report.paused = True

    def run(self) -> ArchiveReport:
        self.account(0, False)
        with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="archive") as pool:
            while True:
                stopped = self.cancel.is_set() or self.report.paused or self.report.cancelled
                if not stopped:
                    before = len(self.futures)
                    self.schedule(pool)
                    if len(self.futures) > before and len(self.futures) < WORKERS:
                        continue
                if not self.futures:
                    break
                ready, _ = wait(self.futures, return_when=FIRST_COMPLETED)
                for future in ready:
                    self.receive(future)
                ready.clear()
                del future
        self.report.cancelled |= self.cancel.is_set()
        self.report.remaining = [u.job for u in self.uploads if not u.complete] + list(self.pending)
        if self.report.paused or self.report.cancelled:
            phase = "paused" if self.report.paused else "cancelled"
            for job in self.report.remaining:
                if self.on_state is not None:
                    self.on_state(job, phase)
        return self.report


def transfer_archive(client, jobs, progress, cancel=None, on_bytes=None, on_state=None):
    return Coordinator(client, jobs, progress, cancel, on_bytes, on_state).run()
