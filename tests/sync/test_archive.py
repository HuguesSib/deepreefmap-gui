"""The archive queue: what is planned, and how one pass over it runs.

Only the part-upload tests reach a socket, and it is a loopback server. The
registry is a fake object standing in for `SyncClient`, so the real plan builder
and the real executor run against real files.
"""

from __future__ import annotations

import hashlib
import json
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
    TransferMeter,
    archive_plan,
    run_archive,
)
from deepreefmap_gui.sync.client import (
    AccessDeniedError,
    ServerUnreachableError,
    SyncClient,
    SyncError,
)

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
        self.uploaded: list[tuple[str, int, bytes]] = []
        self.completed: list[tuple[str, list[dict]]] = []

    def archive_initiate(self, payload):
        self.initiated.append(dict(payload))
        answer = self._answers[payload["content_hash"]]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def archive_upload_part(self, object_id, part_number, chunk):
        self.uploaded.append((object_id, part_number, bytes(chunk)))
        return hashlib.md5(chunk, usedforsecurity=False).hexdigest()

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


def pending(object_id, part_size, parts_done=()):
    return {
        "object_id": object_id,
        "status": "pending",
        "upload_id": "u-1",
        "part_size_bytes": part_size,
        "parts_done": list(parts_done),
    }


def no_progress(text, done, total):
    pass


def test_content_the_server_already_holds_uploads_nothing(tmp_path):
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: {"object_id": "o-1", "status": "complete"}})

    report = run_archive(client, [job], no_progress)

    assert (report.archived, report.already, report.failed) == (0, 1, [])
    assert client.uploaded == [] and client.completed == []


def test_missing_parts_are_read_at_their_offsets(tmp_path):
    """Scenario: a prior pass got part 1 up before the connection dropped.

    Expected behaviour: only the parts the registry lacks travel, each read at
    the offset its part number names, not wherever the file handle sat.
    """
    part_size = 16
    content = bytes(range(48))
    job = make_job(tmp_path, content=content)
    client = FakeArchive({job.content_hash: pending("o-1", part_size, parts_done=[1])})

    report = run_archive(client, [job], no_progress)

    assert report.archived == 1
    assert client.uploaded == [
        ("o-1", 2, content[16:32]),
        ("o-1", 3, content[32:48]),
    ]
    receipts = [
        {"part_number": n, "etag": hashlib.md5(chunk, usedforsecurity=False).hexdigest()}
        for _, n, chunk in client.uploaded
    ]
    assert client.completed == [("o-1", receipts)]


def test_an_upload_with_every_part_stored_is_assembled_without_sending(tmp_path):
    job = make_job(tmp_path, content=b"x" * 24)
    client = FakeArchive({job.content_hash: pending("o-1", 8, parts_done=[1, 2, 3])})

    report = run_archive(client, [job], no_progress)

    assert report.archived == 1
    assert client.uploaded == []
    assert client.completed == [("o-1", [])]


def test_a_part_the_registry_stored_differently_fails_the_file(tmp_path):
    """The registry answers each part with the MD5 of what it wrote, so a
    mismatch is corruption in transit and the file is not assembled."""
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: pending("o-1", 1 << 20)})
    client.archive_upload_part = lambda object_id, number, chunk: "0" * 32  # type: ignore[method-assign]

    report = run_archive(client, [job], no_progress)

    assert client.completed == []
    assert [(label, "differs from the one sent" in reason) for label, reason in report.failed] == [
        ("clip.mp4", True)
    ]


def test_a_failing_job_does_not_stop_the_rest(tmp_path):
    """A flaky connection costs a retry, never the whole queue."""
    broken = make_job(tmp_path, name="broken.mp4", content=b"one")
    fine = make_job(tmp_path, name="fine.mp4", content=b"two")
    client = FakeArchive(
        {
            broken.content_hash: ServerUnreachableError("Cannot reach the registry"),
            fine.content_hash: {"object_id": "o-2", "status": "complete"},
        }
    )

    report = run_archive(client, [broken, fine], no_progress)

    assert report.already == 1
    assert [(label, "Cannot reach" in reason) for label, reason in report.failed] == [
        ("broken.mp4", True)
    ]


def test_a_cancelled_queue_stops_between_jobs(tmp_path):
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: {"object_id": "o-1", "status": "complete"}})
    cancelled = threading.Event()
    cancelled.set()

    report = run_archive(client, [job], no_progress, cancel_event=cancelled)

    assert report == ArchiveReport(cancelled=True)
    assert client.initiated == []


# --- the byte gauge ---------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_the_meter_counts_dedup_into_the_bar_but_not_the_speed():
    """A file the registry already held is on the server without travelling, so
    the clock has to have moved for the absent speed to mean anything."""
    clock = Clock()
    meter = TransferMeter(100, now=clock)
    clock.t = 2.0

    reading = meter.account(60, travelled=False)

    assert (reading.done_bytes, reading.total_bytes, reading.speed_bps) == (60, 100, None)


def test_dedup_stays_out_of_the_speed_a_later_part_reads():
    """Sixty bytes that never travelled would be divided into the same seconds
    as the twenty that did, and read as an uplink the field team does not have."""
    clock = Clock()
    meter = TransferMeter(100, now=clock)
    clock.t = 2.0
    meter.account(60, travelled=False)

    clock.t = 4.0

    assert meter.account(20, travelled=True).speed_bps == 5.0


def test_the_meter_reads_speed_from_travelled_bytes_over_wall_time():
    clock = Clock()
    meter = TransferMeter(100, now=clock)

    clock.t = 2.0
    assert meter.account(20, travelled=True).speed_bps == 10.0
    clock.t = 4.0
    assert meter.account(20, travelled=True).speed_bps == 10.0


def test_a_burst_too_short_to_time_reports_no_speed():
    """Parts a millisecond apart divide into an absurd figure, so they do not.

    The same guard is what keeps a zero span out of the division at all.
    """
    clock = Clock()
    meter = TransferMeter(100, now=clock)

    clock.t = 0.001

    assert meter.account(50, travelled=True).speed_bps is None


def test_the_speed_window_forgets_an_old_burst():
    """A fast first part must stop propping the figure up minutes later."""
    clock = Clock()
    meter = TransferMeter(1000, now=clock)
    clock.t = 1.0
    meter.account(100, travelled=True)
    clock.t = 100.0
    meter.account(10, travelled=True)

    clock.t = 101.0
    reading = meter.account(10, travelled=True)

    assert reading.speed_bps == 10.0


def test_bytes_are_reported_per_part_as_they_land(tmp_path):
    job = make_job(tmp_path, content=b"0123456789")
    client = FakeArchive({job.content_hash: pending("o-1", 4)})
    readings = []

    run_archive(client, [job], no_progress, on_bytes=readings.append)

    assert [r.done_bytes for r in readings] == [0, 0, 4, 8, 10]
    assert readings[-1].total_bytes == 10


def test_parts_a_prior_pass_stored_count_without_travelling(tmp_path):
    job = make_job(tmp_path, content=b"0123456789")
    client = FakeArchive({job.content_hash: pending("o-1", 4, parts_done=[1])})
    readings = []

    run_archive(client, [job], no_progress, on_bytes=readings.append)

    assert [r.done_bytes for r in readings] == [0, 4, 8, 10]
    assert readings[1].speed_bps is None


def test_a_deduplicated_file_fills_the_bar_without_a_speed(tmp_path):
    job = make_job(tmp_path)
    client = FakeArchive({job.content_hash: {"object_id": "o-1", "status": "complete"}})
    readings = []

    run_archive(client, [job], no_progress, on_bytes=readings.append)

    assert readings[-1].done_bytes == readings[-1].total_bytes == job.size_bytes
    assert readings[-1].speed_bps is None


# --- the raw part upload ----------------------------------------------------------

TOKEN = "drmd_" + "0" * 16 + "_" + "1" * 64


@pytest.fixture
def part_store():
    """A loopback registry for part PUTs: answers JSON receipts, records requests."""
    received: list[tuple[str, dict, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        status = 200
        # None means answer the MD5 of what arrived, as the registry does.
        etag: str | None = None

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append((self.path, dict(self.headers), body))
            answer = self.etag
            if answer is None:
                answer = hashlib.md5(body, usedforsecurity=False).hexdigest()
            payload: dict = {"part_number": 2}
            if answer:
                payload["etag"] = answer
            if self.status != 200:
                payload = {"error": "the registry said no"}
            raw = json.dumps(payload).encode()
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SyncClient(f"http://127.0.0.1:{server.server_address[1]}", token=TOKEN)
    try:
        yield client, received, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_upload_part_puts_the_bytes_under_the_device_token(part_store) -> None:
    client, received, _ = part_store

    etag = client.archive_upload_part("o-1", 2, b"part bytes")

    assert etag == hashlib.md5(b"part bytes", usedforsecurity=False).hexdigest()
    path, headers, body = received[0]
    assert path == "/api/archive/o-1/parts/2"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/octet-stream"
    assert body == b"part bytes"


def test_upload_part_refuses_an_answer_without_an_etag(part_store) -> None:
    client, _, handler = part_store
    handler.etag = ""

    with pytest.raises(SyncError, match="without an ETag"):
        client.archive_upload_part("o-1", 2, b"part bytes")


def test_upload_part_reports_a_refused_part(part_store) -> None:
    client, _, handler = part_store
    handler.status = 409

    with pytest.raises(SyncError, match="the registry said no"):
        client.archive_upload_part("o-1", 2, b"part bytes")


def test_upload_part_reports_a_denied_device(part_store) -> None:
    client, _, handler = part_store
    handler.status = 403

    with pytest.raises(AccessDeniedError, match="refused this request"):
        client.archive_upload_part("o-1", 2, b"part bytes")


def test_upload_part_reports_an_unreachable_registry() -> None:
    # Port 1 on loopback: nothing listens, so this is a refusal, not a lookup.
    client = SyncClient("http://127.0.0.1:1", token=TOKEN)

    with pytest.raises(ServerUnreachableError, match="Cannot reach"):
        client.archive_upload_part("o-1", 1, b"part bytes")


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
