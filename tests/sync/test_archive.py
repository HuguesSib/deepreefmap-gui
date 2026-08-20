"""The archive queue: what is planned, and how one pass over it runs.

Only the part-upload test reaches a socket, and it is a loopback server. The
registry is a fake object standing in for `SyncClient`, so the real plan builder
and the real executor run against real files.
"""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from _factories import seed_pass

from deepreefmap_gui.io.video_hash import hash_video
from deepreefmap_gui.survey.models import RunRecord, VideoAsset
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync import archive
from deepreefmap_gui.sync.archive import (
    ArchiveJob,
    ArchiveReport,
    archive_plan,
    run_archive,
    upload_part,
)
from deepreefmap_gui.sync.client import ServerUnreachableError, SyncError

CLIP_BYTES = b"reef footage " * 64


@pytest.fixture
def store(tmp_path) -> SurveyStore:
    return SurveyStore(tmp_path / "out" / "survey.db")


@pytest.fixture
def out_root(store) -> Path:
    return store.path.parent


def add_clip(store, tmp_path, name="GX010001.MP4", content=CLIP_BYTES, content_hash=None):
    path = tmp_path / name
    path.write_bytes(content)
    asset = VideoAsset(file_name=name, path=str(path), hash=content_hash)
    store.upsert_video(asset)
    return path, asset


def add_succeeded_run(store, out_root, dir_name="run-1", status="succeeded"):
    _, _, pass_ = seed_pass(store)
    run = RunRecord(pass_id=pass_.id, run_dir_name=dir_name, status=status)
    store.add_run(run)
    run_dir = out_root / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run, run_dir


def refuse_to_hash(monkeypatch):
    """Fail any hashing the test says must not happen."""

    def hasher(path):
        raise AssertionError(f"{path.name} must not be re-hashed")

    monkeypatch.setattr(archive, "hash_video", hasher)


# --- planning: videos -----------------------------------------------------------


def test_a_video_without_a_hash_gets_one_computed_and_stored(store, out_root, tmp_path):
    path, asset = add_clip(store, tmp_path)

    plan = archive_plan(store, out_root)

    expected = hash_video(path)
    assert [(job.kind, job.content_hash, job.size_bytes) for job in plan.jobs] == [
        ("video", expected, len(CLIP_BYTES))
    ]
    # The digest is queued, not written: the planner runs on a worker thread,
    # and the GUI thread owns the store's writes.
    assert plan.hash_backfills == [(str(asset.id), expected)]
    assert store.get_video(asset.id).hash is None


def test_a_video_uses_the_identity_hash_it_already_carries(
    store, out_root, tmp_path, monkeypatch
):
    """Ingest hashes every clip, so archiving never reads one to identify it."""
    recorded = "ab" * 16
    add_clip(store, tmp_path, content_hash=recorded)
    refuse_to_hash(monkeypatch)

    plan = archive_plan(store, out_root)

    assert [job.content_hash for job in plan.jobs] == [recorded]
    assert plan.hash_backfills == []


def test_a_clip_whose_file_is_gone_is_left_out(store, out_root, tmp_path):
    path, _ = add_clip(store, tmp_path)
    path.unlink()

    plan = archive_plan(store, out_root)

    assert plan.jobs == []
    assert plan.skipped == [(path.name, "the file is not where the survey last saw it")]


# --- planning: run artefacts ------------------------------------------------------


def test_run_files_are_enumerated_with_their_relpaths(store, out_root):
    run, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "run_manifest.json").write_text("{}")
    (run_dir / "ortho.png").write_bytes(b"png bytes")
    (run_dir / "frames").mkdir()
    (run_dir / "frames" / "0001.png").write_bytes(b"frame bytes")
    (run_dir / ".cache").mkdir()
    (run_dir / ".cache" / "scratch.bin").write_bytes(b"working state")
    (run_dir / ".hidden").write_bytes(b"working state")

    jobs = archive_plan(store, out_root).jobs

    assert {job.relpath for job in jobs} == {
        "run_manifest.json",
        "ortho.png",
        "frames/0001.png",
    }
    assert all(job.kind == "artifact" for job in jobs)
    assert all(job.run_id == str(run.id) for job in jobs)
    assert all(job.label.startswith("run-1/") for job in jobs)


def test_only_succeeded_runs_are_offered(store, out_root):
    _, run_dir = add_succeeded_run(store, out_root, dir_name="run-failed", status="failed")
    (run_dir / "ortho.png").write_bytes(b"png bytes")

    plan = archive_plan(store, out_root)

    assert plan.jobs == []
    assert ("run-failed", "the run did not succeed") in plan.skipped


def test_an_artefact_is_identified_by_its_own_content(store, out_root):
    _, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "ortho.png").write_bytes(b"png bytes")

    jobs = archive_plan(store, out_root).jobs

    digests = {job.relpath: job.content_hash for job in jobs}
    assert digests["ortho.png"] == hash_video(run_dir / "ortho.png")


def test_a_rewritten_artefact_gets_a_new_identity(store, out_root):
    _, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "ortho.png").write_bytes(b"png bytes")
    before = {job.relpath: job.content_hash for job in archive_plan(store, out_root).jobs}

    (run_dir / "ortho.png").write_bytes(b"rewritten later")

    after = {job.relpath: job.content_hash for job in archive_plan(store, out_root).jobs}
    assert after["ortho.png"] != before["ortho.png"]


# --- executing -----------------------------------------------------------------


class FakeArchive:
    """Answers like the registry's archive routes, recording what was asked."""

    def __init__(self, answers):
        self._answers = dict(answers)
        self.initiated: list[dict] = []
        self.completed: list[tuple[str, list[dict]]] = []

    def archive_initiate(self, payload):
        self.initiated.append(dict(payload))
        answer = self._answers[payload["content_hash"]]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def archive_complete(self, object_id, parts):
        self.completed.append((object_id, [dict(p) for p in parts]))
        return {"object_id": object_id, "status": "complete"}


def make_job(tmp_path, content=CLIP_BYTES, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(content)
    return ArchiveJob(
        label=name,
        path=path,
        content_hash=hash_video(path),
        size_bytes=len(content),
        kind="video",
    )


def pending(object_id, part_size, part_numbers, parts_done=()):
    return {
        "object_id": object_id,
        "status": "pending",
        "upload_id": "u-1",
        "part_size_bytes": part_size,
        "parts_done": list(parts_done),
        "presign_ttl_seconds": 900,
        "part_urls": [{"part_number": n, "url": f"https://blobs/{n}"} for n in part_numbers],
    }


def capture_uploads(monkeypatch):
    sent: list[tuple[str, bytes]] = []

    def fake_upload(url, chunk):
        sent.append((url, chunk))
        return f"etag-{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(archive, "upload_part", fake_upload)
    return sent


def no_progress(text, done, total):
    pass


def test_content_the_server_already_holds_uploads_nothing(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: {"object_id": "o-1", "status": "complete"}})
    sent = capture_uploads(monkeypatch)

    report = run_archive(client, [job], no_progress)

    assert (report.archived, report.already, report.failed) == (0, 1, [])
    assert sent == [] and client.completed == []


def test_a_lapsed_signature_reinitiates_and_finishes_the_rest(tmp_path, monkeypatch):
    """Scenario: a 403 on a part URL that outlived its signature.

    Expected behaviour: the pass asks for fresh URLs and carries on from the
    parts the registry says are still missing, rather than failing the file.
    """
    job = make_job(tmp_path, content=b"x" * 24)
    client = FakeArchive({job.content_hash: pending("o-1", 8, [1, 2, 3])})
    client._answers[job.content_hash] = [
        pending("o-1", 8, [1, 2, 3]),
        pending("o-1", 8, [2, 3], parts_done=[1]),
    ]
    answers = iter(client._answers[job.content_hash])
    client.archive_initiate = lambda payload: dict(next(answers))  # type: ignore[method-assign]

    lapsed = {"first": True}

    def fake_upload(url, chunk):
        if lapsed["first"] and url.endswith("/2"):
            lapsed["first"] = False
            raise archive.PresignExpiredError("lapsed")
        return f"etag-{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(archive, "upload_part", fake_upload)

    report = run_archive(client, [job], no_progress)

    assert (report.archived, report.failed) == (1, [])
    assert client.completed == [("o-1", [])]


def test_missing_parts_are_read_at_their_offsets(tmp_path, monkeypatch):
    """Scenario: a prior pass got part 1 up before the connection dropped.

    Expected behaviour: only the parts the server presigned travel, each read at
    the offset its part number names, not wherever the file handle sat.
    """
    part_size = 16
    content = bytes(range(48))
    job = make_job(tmp_path, content=content)
    client = FakeArchive(
        {job.content_hash: pending("o-1", part_size, part_numbers=[2, 3], parts_done=[1])}
    )
    sent = capture_uploads(monkeypatch)

    report = run_archive(client, [job], no_progress)

    assert report.archived == 1
    assert sent == [
        ("https://blobs/2", content[16:32]),
        ("https://blobs/3", content[32:48]),
    ]
    # No receipts: the registry assembles from the store's own part listing,
    # which is the only account that covers parts an earlier attempt sent.
    assert client.completed == [("o-1", [])]


def test_a_failing_job_does_not_stop_the_rest(tmp_path, monkeypatch):
    """A flaky connection costs a retry, never the whole queue."""
    broken = make_job(tmp_path, name="broken.mp4", content=b"one")
    fine = make_job(tmp_path, name="fine.mp4", content=b"two")
    client = FakeArchive(
        {
            broken.content_hash: ServerUnreachableError("Cannot reach the blob store"),
            fine.content_hash: {"object_id": "o-2", "status": "complete"},
        }
    )
    capture_uploads(monkeypatch)

    report = run_archive(client, [broken, fine], no_progress)

    assert report.already == 1
    assert [(label, "blob store" in reason) for label, reason in report.failed] == [
        ("broken.mp4", True)
    ]


def test_a_cancelled_queue_stops_between_jobs(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: {"object_id": "o-1", "status": "complete"}})
    capture_uploads(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()

    report = run_archive(client, [job], no_progress, cancel_event=cancelled)

    assert report == ArchiveReport(cancelled=True)
    assert client.initiated == []


# --- the raw part upload ----------------------------------------------------------


@pytest.fixture
def blob_store():
    """A loopback stand-in for S3: answers PUTs with an ETag, records the bytes."""
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        # None means answer with the MD5 of what arrived, as a store does.
        etag: str | None = None

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append((self.path, body))
            self.send_response(200)
            answer = self.etag
            if answer is None:
                answer = '"%s"' % hashlib.md5(body, usedforsecurity=False).hexdigest()
            if answer:
                self.send_header("ETag", answer)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url, received, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_upload_part_sends_the_bytes_and_strips_the_etag_quotes(blob_store) -> None:
    url, received, _ = blob_store

    etag = upload_part(f"{url}/part-1?sig=xyz", b"part bytes")

    assert etag == hashlib.md5(b"part bytes", usedforsecurity=False).hexdigest()
    assert received == [("/part-1?sig=xyz", b"part bytes")]


def test_upload_part_refuses_a_part_the_store_stored_differently(blob_store) -> None:
    """The store answers each part with the MD5 of what it wrote, so a mismatch
    is corruption in transit and the part is not counted as sent."""
    url, _, handler = blob_store
    handler.etag = '"%s"' % ("0" * 32)

    with pytest.raises(SyncError, match="differs from the one sent"):
        upload_part(f"{url}/part-1", b"part bytes")


def test_upload_part_refuses_an_answer_without_an_etag(blob_store) -> None:
    url, _, handler = blob_store
    handler.etag = ""

    with pytest.raises(SyncError, match="without an ETag"):
        upload_part(f"{url}/part-1", b"part bytes")


def test_upload_part_reports_an_unreachable_blob_store() -> None:
    # Port 1 on loopback: nothing listens, so this is a refusal, not a lookup.
    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        upload_part("http://127.0.0.1:1/part-1", b"part bytes")


# --- single-item plans ---


def test_a_single_clip_plan_carries_that_clip_and_nothing_else(store, out_root, tmp_path):
    _path, wanted = add_clip(store, tmp_path)
    add_clip(store, tmp_path, name="GX020001.MP4", content=b"other " * 40)
    add_succeeded_run(store, out_root)
    (out_root / "run-1" / "ortho.png").write_bytes(b"png")

    jobs = archive.archive_plan_for_video(store, wanted.id).jobs

    assert [job.label for job in jobs] == ["GX010001.MP4"]
    assert jobs[0].kind == archive.KIND_VIDEO


def test_a_single_run_plan_carries_that_run_and_nothing_else(store, out_root, tmp_path):
    add_clip(store, tmp_path)
    run, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "benthic_cover.json").write_text("{}")
    other = RunRecord(pass_id=run.pass_id, run_dir_name="run-2", status="succeeded")
    store.add_run(other)
    other_dir = out_root / "run-2"
    other_dir.mkdir()
    (other_dir / "ortho.png").write_bytes(b"png")

    jobs = archive.archive_plan_for_run(store, out_root, run.id).jobs

    assert {job.run_id for job in jobs} == {str(run.id)}
    assert [job.relpath for job in jobs] == ["benthic_cover.json"]


def test_a_single_item_plan_for_an_unknown_id_is_empty(store, out_root):
    assert archive.archive_plan_for_video(store, "not-a-real-id").jobs == []
    assert archive.archive_plan_for_run(store, out_root, "not-a-real-id").jobs == []


# --- probing ---


class FakeProbeRegistry:
    def __init__(self, blob_states=None, run_states=None):
        self.blob_states = blob_states or {}
        self.run_states = run_states or {}
        self.asked_hashes: list[list[str]] = []
        self.asked_runs: list[list[str]] = []

    def archive_probe(self, hashes):
        self.asked_hashes.append(list(hashes))
        return {"states": self.blob_states}

    def archive_runs_probe(self, run_ids):
        self.asked_runs.append(list(run_ids))
        return {"states": self.run_states}


def test_probe_maps_every_state_a_badge_can_show(store, tmp_path):
    """Scenario: five clips in every server-side state, and four run shapes.

    Expected behaviour: complete reads as archived, failed as failed, anything
    in flight as pending, and a clip the registry has never been offered stays
    unknown, exactly as a clip with no digest does.
    """
    digests = {name: hashlib.sha256(name.encode()).hexdigest() for name in "abcd"}
    clips = [
        add_clip(store, tmp_path, name=f"{name}.MP4", content_hash=digest)[1]
        for name, digest in digests.items()
    ]
    unhashed = add_clip(store, tmp_path, name="e.MP4")[1]
    registry = FakeProbeRegistry(
        blob_states={
            digests["a"]: {"status": "complete"},
            digests["b"]: {"status": "failed"},
            digests["c"]: {"status": "pending"},
        },
        run_states={
            "r-archived": {"artifacts": 3, "complete": 3, "failed": 0},
            "r-partial": {"artifacts": 3, "complete": 1, "failed": 0},
            "r-failed": {"artifacts": 3, "complete": 2, "failed": 1},
        },
    )
    runs = [
        RunRecord(pass_id=clips[0].id, run_dir_name=name, status="succeeded")
        for name in ("r1", "r2", "r3", "r4")
    ]
    ids = {run.run_dir_name: str(run.id) for run in runs}
    registry.run_states = {
        ids["r1"]: {"artifacts": 3, "complete": 3, "failed": 0},
        ids["r2"]: {"artifacts": 3, "complete": 1, "failed": 0},
        ids["r3"]: {"artifacts": 3, "complete": 2, "failed": 1},
    }

    states = archive.probe_archive_states(registry, [*clips, unhashed], runs)

    assert states.videos[str(clips[0].id)] == archive.STATE_ARCHIVED
    assert states.videos[str(clips[1].id)] == archive.STATE_FAILED
    assert states.videos[str(clips[2].id)] == archive.STATE_PENDING
    assert states.videos[str(clips[3].id)] == archive.STATE_UNKNOWN
    assert str(unhashed.id) not in states.videos, "no digest, nothing to ask about"
    assert states.runs[ids["r1"]] == archive.STATE_ARCHIVED
    assert states.runs[ids["r2"]] == archive.STATE_PARTIAL
    assert states.runs[ids["r3"]] == archive.STATE_FAILED
    assert states.runs[ids["r4"]] == archive.STATE_UNKNOWN
    assert registry.asked_hashes == [sorted(set(digests.values()))]


def test_probe_makes_no_calls_with_nothing_to_ask(store):
    registry = FakeProbeRegistry()

    states = archive.probe_archive_states(registry, [], [])

    assert (states.videos, states.runs) == ({}, {})
    assert registry.asked_hashes == []
    assert registry.asked_runs == []


def test_a_run_with_a_scene_but_no_web_cloud_grows_one_at_archive_time(store, out_root):
    """Archive time is when the run travels to where the export is viewed, so a
    run from before the export existed builds one from its scene on the way."""
    from _factories import make_classes_config, make_scene

    from deepreefmap_gui.io.scene_file import save_scene_file
    from deepreefmap_gui.io.web_cloud import WEB_CLOUD_FILENAME

    run, run_dir = add_succeeded_run(store, out_root)
    scene = make_scene(class_ids=(1, 5), points_by_class={1: 7, 5: 4})
    save_scene_file(
        run_dir / "reef.scene.zarr.zip",
        manifest={"name": "reef", "mode": "semantic"},
        classes_config=make_classes_config((1, 5)),
        mapping_result=scene.mapping,
        frame_batch=scene.frame_batch,
        final_cloud_index=scene.cloud_index,
    )

    plan = archive.archive_plan_for_run(store, out_root, run.id)

    assert (run_dir / WEB_CLOUD_FILENAME).is_file()
    assert WEB_CLOUD_FILENAME in {job.relpath for job in plan.jobs}


def test_a_run_that_was_never_opened_says_how_to_get_its_web_view(store, out_root):
    from deepreefmap_gui.io.web_cloud import WEB_CLOUD_FILENAME

    run, run_dir = add_succeeded_run(store, out_root)
    (run_dir / "ortho.png").write_bytes(b"png bytes")

    plan = archive.archive_plan_for_run(store, out_root, run.id)

    assert WEB_CLOUD_FILENAME not in {job.relpath for job in plan.jobs}
    assert (
        f"run-1/{WEB_CLOUD_FILENAME}",
        "open the run once to build its web view first",
    ) in plan.skipped
