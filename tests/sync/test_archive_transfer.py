"""Transfer scheduling and recovery across real file boundaries."""

import hashlib
import threading
from dataclasses import replace

from deepreefmap_gui.sync.archive import ArchiveJob, run_archive
from deepreefmap_gui.sync.archive_client import ArchiveError
from deepreefmap_gui.sync.archive_transfer import retry


class Store:
    def __init__(self):
        self.parts = {}
        self.uploaded = []
        self.completed = []
        self.complete = False

    def archive_initiate(self, payload):
        return {
            "status": "complete" if self.complete else "pending",
            "object_id": "object",
            "part_size_bytes": 8,
            "parts_done": list(self.parts),
            "uploaded_parts": [
                {"part_number": n, "size_bytes": len(b), "etag": hashlib.md5(b, usedforsecurity=False).hexdigest()}
                for n, b in self.parts.items()
            ],
        }

    def archive_upload_part(self, object_id, number, chunk):
        self.uploaded.append(number)
        self.parts[number] = chunk
        return hashlib.md5(chunk, usedforsecurity=False).hexdigest()

    def archive_complete(self, object_id, parts):
        self.completed.append(object_id)
        self.complete = True
        return {"status": "complete"}


def job(tmp_path, content=None):
    if content is None:
        content = bytes(range(40))
    path = tmp_path / "clip.mp4"
    path.write_bytes(content)
    return ArchiveJob(path.name, path, "identity", len(content), "video", mtime_ns=path.stat().st_mtime_ns)


def progress(*args):
    pass


def test_transfer_repairs_corrupt_receipts_and_skips_valid_parts(tmp_path):
    item = job(tmp_path)
    store = Store()
    store.parts = {1: b"damaged!", 2: bytes(range(8, 16))}
    report = run_archive(store, [item], progress)
    assert report.archived == 1
    assert sorted(store.uploaded) == [1, 3, 4, 5]
    assert b"".join(store.parts[n] for n in sorted(store.parts)) == item.path.read_bytes()


def test_transfer_does_not_trust_legacy_part_numbers(tmp_path):
    item = job(tmp_path, b"12345678")
    store = Store()
    store.archive_initiate = lambda _: {
        "status": "pending",
        "object_id": "object",
        "part_size_bytes": 8,
        "parts_done": [1],
    }
    assert run_archive(store, [item], progress).archived == 1
    assert store.uploaded == [1]


def test_transfer_overlaps_four_parts_and_bounds_workers(tmp_path):
    item = job(tmp_path, bytes(range(32)))
    store = Store()
    barrier = threading.Barrier(4)
    threads = set()
    original = store.archive_upload_part

    def upload(object_id, number, chunk):
        threads.add(threading.get_ident())
        barrier.wait(timeout=3)
        return original(object_id, number, chunk)

    store.archive_upload_part = upload
    assert run_archive(store, [item], progress).archived == 1
    assert len(threads) == 4


def test_transfer_integrity_failure_pauses_and_retains_queue(tmp_path):
    item = job(tmp_path)
    store = Store()
    store.archive_upload_part = lambda *_: "wrong"
    report = run_archive(store, [item], progress)
    assert report.paused
    assert report.remaining == [item]
    assert not store.completed


def test_transfer_cancel_from_progress_stops_new_parts(tmp_path):
    item = job(tmp_path)
    store = Store()
    cancel = threading.Event()

    def state(item, phase):
        if phase == "uploading":
            cancel.set()

    report = run_archive(store, [item], progress, cancel_event=cancel, on_state=state)
    assert report.cancelled
    assert report.remaining == [item]
    assert not store.uploaded


def test_transfer_changed_file_is_not_sent(tmp_path):
    item = job(tmp_path)
    item.path.write_bytes(b"changed")
    store = Store()
    report = run_archive(store, [item], progress)
    assert report.failed
    assert not store.uploaded
    assert report.remaining == [item]


def test_transfer_duplicate_content_registers_both_artifact_links(tmp_path):
    first = replace(job(tmp_path), kind="artifact", run_id="run", relpath="one")
    second = replace(first, relpath="two", label="two")
    store = Store()
    report = run_archive(store, [first, second], progress)
    assert report.archived == report.already == 1
    assert len(store.uploaded) == 5


def test_retry_stops_at_three_attempts_without_sleeping():
    attempts = []

    class Clock:
        def is_set(self):
            return False

        def wait(self, delay):
            return False

    def operation():
        attempts.append(1)
        raise ArchiveError("offline")

    try:
        retry(operation, Clock())
    except ArchiveError:
        pass
    assert len(attempts) == 3


def test_transfer_repeated_storage_errors_pause_unstarted_jobs(tmp_path):
    first = job(tmp_path)
    jobs = [replace(first, content_hash=f"hash-{n}", label=f"clip-{n}") for n in range(8)]
    store = Store()

    def fail(_):
        raise ArchiveError("Storage unavailable", status=503)

    class FastEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()

    store.archive_initiate = fail
    report = run_archive(store, jobs, progress, cancel_event=FastEvent())
    assert report.paused
    assert 3 <= len(report.failed) <= 6
    assert len(report.remaining) == 8
    assert not store.uploaded


def test_successful_negotiation_does_not_hide_repeated_upload_failures(tmp_path):
    first = job(tmp_path, b"12345678")
    jobs = [replace(first, content_hash=f"hash-{n}", label=f"clip-{n}") for n in range(12)]
    store = Store()

    def fail(*_):
        raise ArchiveError("Upload storage unavailable", status=503)

    class FastEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()

    store.archive_upload_part = fail
    report = run_archive(store, jobs, progress, cancel_event=FastEvent())
    assert report.paused
    assert 3 <= len(report.failed) < len(jobs)
    assert not store.completed
