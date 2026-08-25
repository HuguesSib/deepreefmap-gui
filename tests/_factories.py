"""Builders for the fixtures more than one test directory needs.

Not a conftest: these are plain functions, imported wherever they are needed so
the same transect coordinates, the same HuggingFace cache layout and the same
scene shape describe the same thing everywhere. Anything used by a single file
stays in that file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import numpy as np
from deepreefmap.config.classes import ClassConfig, SemanticClass
from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult, PreparedFrame
from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

from deepreefmap_gui.survey.models import (
    BatchItem,
    RunRecord,
    SurveyBatch,
    Transect,
    TransectPass,
    VideoAsset,
)
from deepreefmap_gui.survey.models.convert import survey_manifest_block
from deepreefmap_gui.survey.store import SurveyStore

# --- survey ------------------------------------------------------------------

# One reef, one clip, reused everywhere. length_m is the tape reading and is
# deliberately independent of the endpoints (they are ~77 m apart), which is what
# the real workflow produces: the tape is measured, the fixes are GPS.
VIDEO_HASH = "ab" * 16
VIDEO_NAME = "GX010001.MP4"
VIDEO_PATH = "/data/GX010001.MP4"


def make_transect(name: str = "T1", **overrides) -> Transect:
    return Transect(**{
        "name": name,
        "start_lat": -17.5,
        "start_lon": 177.1,
        "end_lat": -17.5005,
        "end_lon": 177.1005,
        "length_m": 50.0,
        **overrides,
    })


def make_video(content_hash: str | None = VIDEO_HASH, **overrides) -> VideoAsset:
    return VideoAsset(**{
        "file_name": VIDEO_NAME,
        "path": VIDEO_PATH,
        "hash": content_hash,
        **overrides,
    })


def clip_pass(
    store: SurveyStore,
    *paths: Path | str,
    duration_s: float = 60.0,
    fps: float = 30.0,
) -> TransectPass:
    """Register clips on disk and file one unassigned pass over the lot.

    The shape a cut section leaves behind: importing only registers clips, so
    tests build the pass themselves. Extra paths become chapters, in order.
    """
    videos = []
    for path in paths:
        asset = VideoAsset.from_path(Path(path))
        asset.duration_s = duration_s
        asset.fps = fps
        videos.append(store.upsert_video(asset))
    pass_ = TransectPass(
        transect_id=None,
        video_id=videos[0].id,
        extra_video_ids=[video.id for video in videos[1:]],
        begin_s=0.0,
        end_s=duration_s * len(videos),
    )
    store.add_pass(pass_)
    return pass_


def write_run(root: Path, dir_name: str, **overrides) -> Path:
    """Write a run directory with a manifest, as the pipeline leaves one."""
    manifest = {
        "name": None,
        "mode": "semantic",
        "input_videos": [VIDEO_PATH],
        "video_hashes": [VIDEO_HASH],
        "run_timestamp": "2026-07-01T10:00:00+00:00",
        "begin_s": 0.0,
        "end_s": 60.0,
        "run_duration_s": 120.0,
        "semantic_reference_points": 1_000_000,
    }
    manifest.update(overrides)
    run_dir = root / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))
    return run_dir


def seed_pass(
    store: SurveyStore,
    direction: str = "forward",
    transect: Transect | None = None,
    batch: SurveyBatch | None = None,
    video: VideoAsset | None = None,
):
    """transect -> video -> pass, the chain every survey row hangs off."""
    transect = transect or make_transect()
    if store.get_transect(transect.id) is None:
        store.add_transect(transect)
    video = store.upsert_video(video or make_video())
    pass_ = TransectPass(
        transect_id=transect.id,
        video_id=video.id,
        begin_s=0.0,
        end_s=60.0,
        direction=direction,
        batch_id=batch.id if batch is not None else None,
    )
    store.add_pass(pass_)
    if batch is not None:
        store.add_batch_item(BatchItem(batch_id=batch.id, pass_id=pass_.id))
    return transect, video, pass_


def make_batch(store: SurveyStore, name: str = "2026-07-01") -> SurveyBatch:
    batch = SurveyBatch(name=name)
    store.add_batch(batch)
    return batch


def seed_survey_run(
    store: SurveyStore,
    root: Path,
    dir_name: str,
    transect: Transect | None = None,
    batch: SurveyBatch | None = None,
    direction: str = "forward",
    video: VideoAsset | None = None,
    **manifest_overrides,
):
    """A succeeded run, in the database and on disk with a matching manifest."""
    transect, _video, pass_ = seed_pass(
        store, direction=direction, transect=transect, batch=batch, video=video
    )
    run = RunRecord(
        pass_id=pass_.id,
        run_dir_name=dir_name,
        status="succeeded",
        batch_id=batch.id if batch is not None else None,
    )
    store.add_run(run)
    write_run(
        root,
        dir_name,
        survey=survey_manifest_block(run, pass_, transect, batch),
        **manifest_overrides,
    )
    return transect, pass_, run


# The schema v0.2.0 shipped, stamped user_version 3. Frozen here because the
# store carries a database forward from it rather than rebuilding it step by
# step, so nothing in production states this shape any more.
V0_2_0_SCHEMA = """
CREATE TABLE transect (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    start_lat REAL NOT NULL,
    start_lon REAL NOT NULL,
    end_lat REAL NOT NULL,
    end_lon REAL NOT NULL,
    length_m REAL,
    depth_m REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE video_asset (
    id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    path TEXT NOT NULL,
    hash TEXT,
    size_bytes INTEGER,
    mtime TEXT,
    duration_s REAL,
    fps REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX video_asset_hash ON video_asset(hash);
CREATE TABLE survey_batch (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE transect_pass (
    id TEXT PRIMARY KEY,
    transect_id TEXT NOT NULL REFERENCES transect(id),
    video_id TEXT NOT NULL REFERENCES video_asset(id),
    batch_id TEXT REFERENCES survey_batch(id),
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    begin_s REAL NOT NULL,
    end_s REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    extra_video_ids TEXT NOT NULL DEFAULT '[]',
    held INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_record (
    id TEXT PRIMARY KEY,
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    run_dir_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


# Versions 4 and 5 were never released: they are what builds between v0.2.0 and
# the flattened baseline wrote, and the store still has to carry them forward.
# Stated here as what changed rather than as two more copies of the whole
# schema, and stated independently of the store's own carry-forward scripts --
# a fixture built from those would be agreeing with itself.

# v4 relaxed transect_pass.transect_id: a pass need not name a transect.
V4_SCHEMA = V0_2_0_SCHEMA.replace(
    "transect_id TEXT NOT NULL REFERENCES transect(id),",
    "transect_id TEXT REFERENCES transect(id),",
)
assert V4_SCHEMA != V0_2_0_SCHEMA

# v5 recorded the session a run ran in, and made cart membership its own table.
V5_SCHEMA = V4_SCHEMA + """
ALTER TABLE run_record ADD COLUMN batch_id TEXT REFERENCES survey_batch(id);
CREATE TABLE batch_item (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES survey_batch(id),
    pass_id TEXT NOT NULL REFERENCES transect_pass(id),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, pass_id)
);
"""

LEGACY_SCHEMAS = {3: V0_2_0_SCHEMA, 4: V4_SCHEMA, 5: V5_SCHEMA}


def write_legacy_database(db_path, version: int):
    """A survey.db in the shape the build that stamped ``version`` left it."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.executescript(LEGACY_SCHEMAS[version])
        conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    return db_path


def write_v0_2_0_database(db_path):
    """A survey.db in the shape v0.2.0 left it, with no rows."""
    return write_legacy_database(db_path, 3)


# The format written before the metadata registry existed, populated rather than
# empty: migration 11 rebuilds transect and transect_pass and backfills
# updated_at, and none of that is exercised by a schema with nothing in it.
V10_TIMES = {
    "transect": "2026-08-01T08:00:00+00:00",
    "video": "2026-08-01T09:00:00+00:00",
    "batch": "2026-08-01T10:00:00+00:00",
    "pass": "2026-08-01T11:00:00+00:00",
    "run": "2026-08-01T12:00:00+00:00",
}

V10_TRANSECT_NAMES = ("T1", "T2")


def write_v10_database(db_path):
    """A survey.db at v10 holding one row in every table it has.

    Returns the ids, keyed by table. The shape comes from the store's own
    baseline and steps: v10 is above the baseline, so stopping the list there is
    exactly what the build that stamped it had.
    """
    from deepreefmap_gui.survey import store as store_module

    conn = sqlite3.connect(db_path)
    store_module.SurveyStore._apply(conn, store_module._BASELINE, store_module.SCHEMA_VERSION)
    for migration in store_module._MIGRATIONS:
        if migration.version <= 10:
            store_module.SurveyStore._apply(conn, migration.sql, migration.version)

    ids = {
        "transects": [str(uuid.uuid4()) for _ in V10_TRANSECT_NAMES],
        "videos": [str(uuid.uuid4()) for _ in range(2)],
        "batch": str(uuid.uuid4()),
        "pass": str(uuid.uuid4()),
        "item": str(uuid.uuid4()),
        "run": str(uuid.uuid4()),
        "notification": str(uuid.uuid4()),
    }
    with conn:
        for index, (transect_id, name) in enumerate(zip(ids["transects"], V10_TRANSECT_NAMES, strict=True)):
            conn.execute(
                "INSERT INTO transect (id, name, description, start_lat, start_lon, end_lat,"
                " end_lon, length_m, depth_m, created_at, updated_at)"
                " VALUES (?, ?, 'a line', -17.5, 177.1, -17.5005, 177.1005, 50.0, 8.0, ?, ?)",
                (transect_id, name, V10_TIMES["transect"], f"2026-08-0{index + 2}T08:00:00+00:00"),
            )
        for index, video_id in enumerate(ids["videos"]):
            conn.execute(
                "INSERT INTO video_asset (id, file_name, path, hash, size_bytes, mtime,"
                " duration_s, fps, created_at, captured_at, captured_source, width, height,"
                " codec, probed_at, gravity, gps)"
                f" VALUES (?, 'GX0{index + 1}0001.MP4', '/data/GX0{index + 1}0001.MP4', ?,"
                " 1024, ?, 90.0, 30.0, ?, ?, 'container', 1920, 1080, 'hvc1', ?, 'yes', 'no')",
                (video_id, f"{index + 1:02x}" * 16, V10_TIMES["video"], V10_TIMES["video"],
                 V10_TIMES["video"], V10_TIMES["video"]),
            )
        conn.execute(
            "INSERT INTO survey_batch (id, name, preset_name, created_at) VALUES (?,?,?,?)",
            (ids["batch"], "Day 1", "survey_preset", V10_TIMES["batch"]),
        )
        conn.execute(
            "INSERT INTO transect_pass (id, transect_id, video_id, batch_id, direction,"
            " begin_s, end_s, notes, created_at, extra_video_ids, label)"
            " VALUES (?, ?, ?, ?, 'reverse', 5.5, 65.5, 'surge', ?, ?, 'first swim')",
            (ids["pass"], ids["transects"][0], ids["videos"][0], ids["batch"],
             V10_TIMES["pass"], json.dumps([ids["videos"][1]])),
        )
        conn.execute(
            "INSERT INTO batch_item (id, batch_id, pass_id, position, overrides, created_at)"
            " VALUES (?, ?, ?, 3, '{\"fps\": 4}', ?)",
            (ids["item"], ids["batch"], ids["pass"], V10_TIMES["pass"]),
        )
        conn.execute(
            "INSERT INTO run_record (id, pass_id, run_dir_name, status, started_at,"
            " finished_at, error, created_at, batch_id)"
            " VALUES (?, ?, 't1__p01', 'succeeded', ?, ?, '', ?, ?)",
            (ids["run"], ids["pass"], V10_TIMES["run"], V10_TIMES["run"], V10_TIMES["run"],
             ids["batch"]),
        )
        conn.execute(
            "INSERT INTO notification (id, fingerprint, kind, severity, scope, title,"
            " created_at, updated_at) VALUES (?, 'disk', 'condition', 'warning', 'machine',"
            " 'Disk filling up', ?, ?)",
            (ids["notification"], V10_TIMES["run"], V10_TIMES["run"]),
        )
    conn.close()
    return ids


# --- video containers --------------------------------------------------------

# Enough MP4 to answer video_probe, written as bytes so no clip has to be
# committed. The GPMF payload carries real keys but no meaningful samples: the
# probe only looks for the keys, and a fixture that decoded would be pretending.

_QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _atom(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", len(body) + 8) + kind + body


def _gpmf_payload(*, gravity: bool = True, gps: bool = True) -> bytes:
    parts = [b"DEVC" + bytes([0, 1]) + struct.pack(">H", 0)]
    if gravity:
        parts.append(b"GRAV" + b"s" + bytes([6]) + struct.pack(">H", 2) + bytes(12))
    if gps:
        parts.append(b"GPS5" + b"l" + bytes([20]) + struct.pack(">H", 1) + bytes(20))
    return b"".join(parts)


def _track(handler: bytes, sample_entry: bytes, *tables: bytes) -> bytes:
    hdlr = _atom(b"hdlr", bytes(4), bytes(4), handler, bytes(12), b"\x00")
    stsd = _atom(b"stsd", bytes(4), struct.pack(">I", 1), sample_entry)
    stbl = _atom(b"stbl", stsd, *tables)
    return _atom(b"trak", _atom(b"mdia", hdlr, _atom(b"minf", stbl)))


def write_test_mp4(
    path: Path,
    *,
    created_at: datetime | None = None,
    duration_s: float | None = 12.0,
    codec: bytes = b"hvc1",
    width: int = 1920,
    height: int = 1080,
    telemetry: bool = True,
    gravity: bool = True,
    gps: bool = True,
    moov_last: bool = False,
    uniform_sizes: bool = True,
    truncate_to: int | None = None,
) -> Path:
    """Write a minimal MP4. moov_last mirrors the firmware that writes it there."""
    seconds = int((created_at - _QT_EPOCH).total_seconds()) if created_at else 0
    timescale = 1000
    ticks = int((duration_s or 0) * timescale)
    mvhd = _atom(
        b"mvhd",
        bytes(4),
        struct.pack(">IIII", seconds, seconds, timescale, ticks),
        bytes(80),
    )

    visual = _atom(codec, bytes(6), struct.pack(">H", 1), bytes(16), struct.pack(">HH", width, height))
    tracks = [_track(b"vide", visual)]

    payload = _gpmf_payload(gravity=gravity, gps=gps) if telemetry else b""
    if telemetry:
        if uniform_sizes:
            stsz = _atom(b"stsz", bytes(4), struct.pack(">II", len(payload), 1))
        else:
            stsz = _atom(
                b"stsz", bytes(4), struct.pack(">II", 0, 1), struct.pack(">I", len(payload))
            )

        def build(offset: int) -> bytes:
            stco = _atom(b"stco", bytes(4), struct.pack(">II", 1, offset))
            meta = _track(b"meta", _atom(b"gpmd", bytes(6), struct.pack(">H", 1)), stsz, stco)
            return _atom(b"moov", mvhd, *tracks, meta)
    else:

        def build(offset: int) -> bytes:
            return _atom(b"moov", mvhd, *tracks)

    ftyp = _atom(b"ftyp", b"isom", struct.pack(">I", 512), b"isomavc1")
    mdat = _atom(b"mdat", payload)
    if moov_last:
        moov = build(len(ftyp) + 8)
        blob = ftyp + mdat + moov
    else:
        # The chunk offset is absolute, and moov sits in front of the data it
        # points at, so its own length has to be known first. Nothing about the
        # offset changes that length, which is what makes two passes enough.
        moov = build(len(ftyp) + len(build(0)) + 8)
        blob = ftyp + moov + mdat

    if truncate_to is not None:
        blob = blob[:truncate_to]
    path.write_bytes(blob)
    return path


# --- HuggingFace cache -------------------------------------------------------

CACHE_COMMIT = "0" * 40


def make_profile(
    *,
    gpu_name: str | None = "GPU",
    free: int = 50 * 1024**3,
    total_ram: int = 16 * 1024**3,
    total_swap: int = 0,
    vram: int | None = 8 * 1024**3,
):
    """A stand-in for probe_system's result, exposing what the setup rows read."""
    from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_NONE, GpuInfo

    gpu = (
        GpuInfo(GPU_CUDA, gpu_name, vram, vram)
        if gpu_name
        else GpuInfo(GPU_NONE, "CPU only", None, None)
    )
    return SimpleNamespace(
        gpu=gpu,
        disk_free_bytes=free,
        total_ram_bytes=total_ram,
        total_swap_bytes=total_swap,
    )


def repo_commit(repo_id: str) -> str:
    """A commit hash unique to one repo.

    delete_revisions resolves a hash across the whole cache, so two repos sharing
    one are deleted together and which of them survives depends on scan order.
    """
    return hashlib.sha1(repo_id.encode()).hexdigest()


def write_cache_repo(cache_root, repo_id, files, *, commit=CACHE_COMMIT, use_symlinks=True):
    """Lay down an HF-cache repo: blobs/ + snapshots/<commit>/ + refs/main.

    Snapshot entries are relative symlinks into ../../blobs/<sha> (the real cache
    layout) unless use_symlinks=False, which writes real files (the Windows
    layout). Pass commit=repo_commit(repo_id) where several repos share a cache
    and must not share a revision.
    """
    repo_dir = cache_root / f"models--{repo_id.replace('/', '--')}"
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(commit)
    blobs = repo_dir / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    snap = repo_dir / "snapshots" / commit
    snap.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        data = content if isinstance(content, bytes) else content.encode()
        blob = blobs / hashlib.sha256(data).hexdigest()
        blob.write_bytes(data)
        dest = snap / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if use_symlinks:
            os.symlink(os.path.relpath(blob, dest.parent), dest)
        else:
            dest.write_bytes(data)
    return repo_dir


# --- scenes ------------------------------------------------------------------

# The toy class table the scene tests share. Ids are the segmentation model's own,
# so they are not 0..n-1.
SCENE_CLASSES = {1: ("reef", (10, 20, 30)), 5: ("sand", (200, 200, 100))}


def make_classes_config(class_ids=(1,)) -> ClassConfig:
    return ClassConfig(
        classes=[
            SemanticClass(
                id=c, name=SCENE_CLASSES[c][0], color=SCENE_CLASSES[c][1], roles=frozenset()
            )
            for c in class_ids
        ],
        path=None,
    )


class Scene(NamedTuple):
    frame_batch: FrameBatch
    mapping: MappingSequenceResult
    cloud_index: FinalCloudIndex | None


def make_scene(
    *,
    frame_indices=(0, 1, 2),
    size=(6, 4),
    class_ids=(1,),
    clip_counts=None,
    points_by_class=None,
    seed=0,
) -> Scene:
    """The three objects a scene file is written from, at whatever shape a test needs.

    frame_indices are the source video's frame numbers, so they need not be
    contiguous. size is (width, height). points_by_class maps a class id to its
    point count and leaves cloud_index None when omitted, for the tests that let
    the writer build the index themselves. No pixels reach a scene file, so the
    images are filler and only the counts, indices and class ids are load-bearing.
    """
    rng = np.random.default_rng(seed)
    width, height = size
    frame_indices = tuple(frame_indices)
    n = len(frame_indices)
    frames = tuple(
        PreparedFrame(
            frame_index=index,
            image_rgb=rng.integers(0, 255, (height, width, 3), dtype=np.uint8),
            labels=rng.choice(class_ids, size=(height, width)).astype(np.uint8),
            keep_mask=(rng.random((height, width)) > 0.5).astype(np.uint8),
        )
        for index in frame_indices
    )
    intrinsics = np.array([[100.0, 0, width / 2], [0, 100.0, height / 2], [0, 0, 1]])
    frame_batch = FrameBatch(
        frames=frames,
        intrinsics=intrinsics,
        image_size=(width, height),
        clip_counts=clip_counts if clip_counts is not None else (n,),
    )
    mapping = MappingSequenceResult(
        frame_indices=np.asarray(frame_indices, dtype=np.int32),
        depth_maps=rng.random((n, height, width)).astype(np.float32),
        poses_w_c=np.repeat(np.eye(4)[None], n, axis=0),
        intrinsics=intrinsics,
        world_points=rng.random((n, height, width, 3)).astype(np.float32),
        confidence=rng.random((n, height, width)).astype(np.float32),
        scale_type="metric",
    )
    cloud_index = None
    if points_by_class is not None:
        counts = dict(points_by_class)
        cloud_index = FinalCloudIndex(
            frame_order=tuple(frame_indices),
            class_ids=tuple(counts),
            xyz_by_class={c: rng.random((k, 3)).astype(np.float32) for c, k in counts.items()},
            rgb_by_class={
                c: rng.integers(0, 255, (k, 3), dtype=np.uint8) for c, k in counts.items()
            },
            semrgb_by_class={
                c: rng.integers(0, 255, (k, 3), dtype=np.uint8) for c, k in counts.items()
            },
            conf_by_class={c: rng.random(k).astype(np.float32) for c, k in counts.items()},
            prefix_end_by_class={
                c: np.linspace(0, k, n, dtype=np.int64) for c, k in counts.items()
            },
        )
    return Scene(frame_batch, mapping, cloud_index)


# --- window stubs ------------------------------------------------------------


class FakeAccessor:
    """A FrameAccessor stand-in that records close()."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


KB = 1024


def write_run_tree(root: Path, name: str = "20260520-155637", *, frames: int = 3) -> Path:
    """A run directory holding one of everything the pipeline leaves behind."""
    run_dir = root / name
    for folder, size in (("frames", 4 * KB), ("labels", 2 * KB), ("masks", KB)):
        (run_dir / folder).mkdir(parents=True)
        for index in range(frames):
            (run_dir / folder / f"{index:08d}.png").write_bytes(b"x" * size)
    (run_dir / ".cache").mkdir()
    (run_dir / ".cache" / "preprocess.json").write_text('{"key": "abc"}')
    (run_dir / ".cache" / "mapping.json").write_text('{"key": "abc"}')
    (run_dir / "mapping_outputs.npz").write_bytes(b"m" * 8 * KB)
    (run_dir / "run_manifest.json").write_text("{}")
    (run_dir / "run.log").write_text("started\n")
    (run_dir / "semantic_reference_cloud.ply").write_bytes(b"p" * 6 * KB)
    (run_dir / "ortho.npz").write_bytes(b"o" * KB)
    (run_dir / "ortho.png").write_bytes(b"o" * KB)
    (run_dir / "benthic_cover.json").write_text("{}")
    (run_dir / f"{name}.scene.zarr.zip").write_bytes(b"z" * 5 * KB)
    return run_dir


def as_pushed(store) -> None:
    """Every mark cleared, as if the last push had been accepted whole."""
    from deepreefmap_gui.survey.store import SYNC_SECTIONS

    for section in SYNC_SECTIONS:
        store.clear_pending_push(section, store.pending_push_ids(section))
