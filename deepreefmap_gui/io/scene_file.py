"""Zarr-based quick-load scene file for the Qt viewer.

Caches only what a run directory cannot cheaply produce again: the pre-computed
FinalCloudIndex, the class table (a custom YAML is referenced by path, not value,
so the run dir alone cannot rebuild it), and the frame metadata needed to open the
PNG caches. Skips the 1-2 min reference-cloud build on load.

Deliberately does *not* store frame pixels or the mapping arrays. Both already sit
beside it in the run directory in a form that is smaller and no slower to read:
PNG beats Blosc on the frames by ~1.5x, and the uncompressed mapping_outputs.npz
reads at disk speed. Caching them made the scene file 46% of the run directory for
about 1% of the value. See runs/loaded_run.py::_load_from_scene_file for the read
side, which pairs this file with RunDirFrameAccessor and resume.load_mapping_result.

The run directory is read-only input here: this module creates, replaces and prunes
scene files and touches nothing the pipeline wrote.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from deepreefmap_gui.io.lazy_frames import FrameAccessor, LazyFrameBatch

if TYPE_CHECKING:
    from deepreefmap.config.classes import ClassConfig
    from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult
    from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

logger = logging.getLogger(__name__)

__all__ = [
    "LEGACY_SCENE_SUFFIXES",
    "SCENE_FILE_SUFFIX",
    "FrameAccessor",
    "LazyFrameBatch",
    "LoadedScene",
    "compute_source_fingerprint",
    "find_scene_file",
    "fingerprint_matches",
    "load_scene_file",
    "prune_other_scene_files",
    "save_scene_file",
    "scene_file_name",
    "tmp_write_in_progress",
]

SCENE_FILE_SUFFIX = ".scene.zarr.zip"
# 2 dropped the frame pixels and the mapping group, which together were 99% of
# the file. 1 is still readable so an existing scene can be upgraded in place
# from its own index rather than rebuilt from the run dir.
SCHEMA_VERSION = 2
MIN_SCHEMA = 1
MAX_SCHEMA = 2

ProgressCB = Callable[[str, int, int], None]

# Scene .tmp files this process is writing right now. A scene write runs on a
# daemon thread while the run stays open, so the same run can be opened again
# mid-write; without this the second open deletes the first one's output.
_ACTIVE_TMP_PATHS: set[Path] = set()
_ACTIVE_TMP_LOCK = threading.Lock()

# A .tmp touched more recently than this is treated as another process's live
# write rather than debris. Generous against the gaps between chunk flushes on a
# slow disk; the only cost of being wrong is one abandoned file left one open
# longer, against deleting a write that was still running.
_TMP_ABANDONED_AFTER_S = 300.0


def tmp_write_in_progress(tmp_path: Path) -> bool:
    """Whether a scene .tmp is being written and must not be deleted.

    True while this process holds it, and while any process has touched it
    recently enough that it cannot be assumed abandoned.
    """
    with _ACTIVE_TMP_LOCK:
        if tmp_path in _ACTIVE_TMP_PATHS:
            return True
    try:
        age = time.time() - tmp_path.stat().st_mtime
    except OSError:
        return False
    return age < _TMP_ABANDONED_AFTER_S


def scene_file_name(manifest: dict[str, Any] | None = None, run_dir: Path | None = None) -> str:
    """Build a descriptive scene filename from run metadata."""
    name = ""
    if manifest:
        name = str(manifest.get("name", "")).strip()
    if not name and run_dir is not None:
        name = run_dir.name
    if not name:
        name = "scene"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name).strip("_")
    if not safe:
        safe = "scene"
    return safe + SCENE_FILE_SUFFIX


LEGACY_SCENE_SUFFIXES = (".drm.zarr.zip",)


def find_scene_file(run_dir: Path) -> Path | None:
    """Find a scene file in a run directory, or None."""
    candidates = sorted(run_dir.glob("*" + SCENE_FILE_SUFFIX))
    if candidates:
        return candidates[0]
    for suffix in LEGACY_SCENE_SUFFIXES:
        legacy = sorted(run_dir.glob("*" + suffix))
        if legacy:
            return legacy[0]
    return None


def prune_other_scene_files(run_dir: Path, *, keep: Path) -> None:
    """Drop scene files superseded by ``keep``, once it is safely in place.

    ``scene_file_name`` derives from the manifest's run name, which is only
    filled in after the run, so a regenerated scene can land under a different
    name and leave the old one behind. Globs only the GUI-written suffixes, so
    nothing the pipeline produced can match: the run directory is read-only to
    everything here except the scene file itself.
    """
    for suffix in (SCENE_FILE_SUFFIX, *LEGACY_SCENE_SUFFIXES):
        for path in run_dir.glob("*" + suffix):
            if path == keep:
                continue
            try:
                freed = path.stat().st_size
                path.unlink()
                logger.info(
                    "Removed superseded scene file %s (%.0f MB reclaimed)",
                    path.name, freed / 2**20,
                )
            except OSError:
                logger.debug("Could not remove superseded scene file %s", path, exc_info=True)

# ---------------------------------------------------------------------------
# Blosc compressor shared across all datasets
# ---------------------------------------------------------------------------

def _compressor():
    from numcodecs import Blosc
    return Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)


# ---------------------------------------------------------------------------
# Source fingerprint: cheap staleness detection
# ---------------------------------------------------------------------------

# `apply_manifest_timings` rewrites these after the scene is written, and none of
# them describe the data the scene holds, so none may invalidate it.
_VOLATILE_MANIFEST_KEYS = frozenset(
    {"stage_durations", "stage_peaks", "run_duration_s", "system_profile", "performance_observation"}
)


def _manifest_data_hash(manifest_path: Path) -> str:
    """Hash what the manifest says about the run, ignoring how long it took."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(manifest, dict):
        return ""
    stable = {k: v for k, v in manifest.items() if k not in _VOLATILE_MANIFEST_KEYS}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def compute_source_fingerprint(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "run_manifest.json"
    mapping_path = run_dir / "mapping_outputs.npz"

    manifest_hash = ""
    manifest_data_hash = ""
    if manifest_path.exists():
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_data_hash = _manifest_data_hash(manifest_path)

    mapping_size = 0
    mapping_mtime = 0.0
    if mapping_path.exists():
        st = mapping_path.stat()
        mapping_size = st.st_size
        mapping_mtime = st.st_mtime

    frame_count = 0
    total_bytes = 0
    for subdir in ("frames", "labels", "masks"):
        d = run_dir / subdir
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    frame_count += 1
                    total_bytes += f.stat().st_size

    return {
        "manifest_sha256": manifest_hash,
        "manifest_data_sha256": manifest_data_hash,
        "mapping_npz_size": mapping_size,
        "mapping_npz_mtime": mapping_mtime,
        "frame_count": frame_count,
        "frames_total_bytes": total_bytes,
    }


def fingerprint_matches(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    # A scene storing only `manifest_sha256` is matched on it: nothing rewrote
    # its manifest after the write, so those bytes are still the right question.
    manifest_key = (
        "manifest_data_sha256" if "manifest_data_sha256" in stored else "manifest_sha256"
    )
    for key in (manifest_key, "mapping_npz_size", "frame_count", "frames_total_bytes"):
        if stored.get(key) != current.get(key):
            return False
    return True


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_scene_file(
    path: Path,
    *,
    manifest: dict[str, Any],
    classes_config: "ClassConfig",
    mapping_result: "MappingSequenceResult",
    frame_batch: "FrameBatch",
    final_cloud_index: "FinalCloudIndex",
    run_dir: Path | None = None,
    progress_cb: ProgressCB | None = None,
) -> None:
    import zarr

    # numcodecs spells the class JSON; aliased so it does not read as the json
    # module two lines below, and PascalCase because it is a class.
    from numcodecs import JSON as JSONCodec  # noqa: N811

    def _emit(stage: str, cur: int, tot: int) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage, cur, tot)
            except Exception:
                pass

    tmp_path = path.parent / (path.name + ".tmp")
    with _ACTIVE_TMP_LOCK:
        _ACTIVE_TMP_PATHS.add(tmp_path)
    try:
        # `with`, so a failure anywhere below closes the archive before the
        # cleanup unlinks it. An open ZipStore holds the file on Windows, where
        # the unlink would then fail and leave the .tmp behind for good.
        with zarr.ZipStore(str(tmp_path), mode="w") as store:
            root = zarr.group(store=store, overwrite=True)

            _emit("scene_meta", 0, 2)

            # --- root attrs ---
            from deepreefmap import __version__ as _current_version
            root.attrs["schema_version"] = SCHEMA_VERSION
            root.attrs["deepreefmap_version"] = _current_version
            if run_dir is not None:
                root.attrs["source_fingerprint"] = compute_source_fingerprint(run_dir)

            # --- /meta ---
            meta = root.require_group("meta")
            meta.attrs["run_name"] = manifest.get("name", "")
            meta.attrs["mode"] = manifest.get("mode", "semantic")
            # Not read back (the mapping result comes from the npz now); kept so
            # the file still says which mapping produced the index it holds.
            meta.attrs["scale_type"] = str(mapping_result.scale_type)
            meta.create_dataset(
                "manifest",
                data=np.array(json.dumps(manifest), dtype=object),
                object_codec=JSONCodec(),
            )
            _save_frame_meta(meta, frame_batch)

            _emit("scene_meta", 1, 2)

            # --- /classes ---
            _save_classes(root, classes_config)
            _emit("scene_meta", 2, 2)

            # --- /final_cloud_index ---
            _save_fci(root, final_cloud_index)
            _emit("scene_fci", 1, 1)

        os.replace(str(tmp_path), str(path))
        _emit("scene_done", 1, 1)

    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        with _ACTIVE_TMP_LOCK:
            _ACTIVE_TMP_PATHS.discard(tmp_path)


def _save_classes(root, classes_config: "ClassConfig") -> None:
    # numcodecs spells the class JSON; aliased so it does not read as the json
    # module two lines below, and PascalCase because it is a class.
    from numcodecs import JSON as JSONCodec  # noqa: N811

    g = root.require_group("classes")
    ids = np.array([c.id for c in classes_config.classes], dtype=np.int32)
    colors = np.array([list(c.color) for c in classes_config.classes], dtype=np.uint8)
    names = np.array([c.name for c in classes_config.classes], dtype=object)
    roles = np.array([json.dumps(sorted(c.roles)) for c in classes_config.classes], dtype=object)

    g.create_dataset("ids", data=ids, compressor=_compressor())
    g.create_dataset("colors", data=colors, compressor=_compressor())
    g.create_dataset("names", data=names, object_codec=JSONCodec())
    g.create_dataset("roles", data=roles, object_codec=JSONCodec())
    g.attrs["classes_path"] = "" if classes_config.path is None else str(classes_config.path)


def _save_frame_meta(meta, fb: "FrameBatch") -> None:
    """Enough to reopen the run's PNG caches, without the pixels.

    A few KB. The alternative was deriving these from the library's preprocess
    sidecar, but two of three real runs on disk have none, so the scene file
    carries them and stays self-describing.
    """
    comp = _compressor()
    frames = fb.frames
    indices = np.array([f.frame_index for f in frames], dtype=np.int32)
    meta.create_dataset("frame_indices", data=indices, compressor=comp)
    meta.create_dataset("clip_counts", data=np.array(fb.clip_counts, dtype=np.int32), compressor=comp)
    width, height = fb.image_size
    meta.attrs["n_frames"] = len(frames)
    meta.attrs["image_width"] = int(width)
    meta.attrs["image_height"] = int(height)


def _save_fci(root, fci: "FinalCloudIndex") -> None:
    comp = _compressor()
    g = root.require_group("final_cloud_index")

    g.create_dataset("frame_order", data=np.array(fci.frame_order, dtype=np.int32), compressor=comp)

    class_ids = list(fci.class_ids)
    g.create_dataset("class_ids", data=np.array(class_ids, dtype=np.int32), compressor=comp)

    # CSR-style: concatenate per-class arrays, store offsets
    offsets = [0]
    xyz_parts, rgb_parts, semrgb_parts, conf_parts = [], [], [], []
    for cid in class_ids:
        xyz_parts.append(fci.xyz_by_class[cid])
        rgb_parts.append(fci.rgb_by_class[cid])
        semrgb_parts.append(fci.semrgb_by_class[cid])
        conf_parts.append(fci.conf_by_class[cid])
        offsets.append(offsets[-1] + len(fci.xyz_by_class[cid]))

    g.create_dataset("class_offsets", data=np.array(offsets, dtype=np.int64), compressor=comp)

    if xyz_parts:
        g.create_dataset("xyz", data=np.concatenate(xyz_parts).astype(np.float32), compressor=comp)
        g.create_dataset("rgb", data=np.concatenate(rgb_parts).astype(np.uint8), compressor=comp)
        g.create_dataset("semrgb", data=np.concatenate(semrgb_parts).astype(np.uint8), compressor=comp)
        g.create_dataset("conf", data=np.concatenate(conf_parts).astype(np.float32), compressor=comp)
    else:
        g.create_dataset("xyz", data=np.zeros((0, 3), dtype=np.float32), compressor=comp)
        g.create_dataset("rgb", data=np.zeros((0, 3), dtype=np.uint8), compressor=comp)
        g.create_dataset("semrgb", data=np.zeros((0, 3), dtype=np.uint8), compressor=comp)
        g.create_dataset("conf", data=np.zeros((0,), dtype=np.float32), compressor=comp)

    # Flatten prefix_end arrays: (n_classes, n_steps)
    n_steps = len(fci.frame_order)
    if class_ids and n_steps > 0:
        pe_flat = np.concatenate([fci.prefix_end_by_class[cid] for cid in class_ids])
    else:
        pe_flat = np.zeros(0, dtype=np.int64)
    g.create_dataset("prefix_end_flat", data=pe_flat.astype(np.int64), compressor=comp)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

@dataclass
class LoadedScene:
    manifest: dict[str, Any]
    classes_config: "ClassConfig"
    final_cloud_index: "FinalCloudIndex"
    run_mode: str
    # Enough to build a RunDirFrameAccessor over the run's PNGs. The pixels and
    # the mapping arrays are not in the file; the caller reads them from the run
    # directory, which is where the pipeline already put them.
    frame_indices: np.ndarray
    clip_counts: tuple[int, ...]
    image_size: tuple[int, int]
    schema_version: int


def load_scene_file(
    path: Path,
    *,
    run_dir: Path | None = None,
    progress_cb: ProgressCB | None = None,
) -> LoadedScene | None:
    """Load a scene file.  Returns None if the file is stale or incompatible."""
    import zarr

    def _emit(stage: str, cur: int, tot: int) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage, cur, tot)
            except Exception:
                pass

    # `with`: everything is materialised into numpy here, so no handle outlives
    # the call. That is only true since the frame pixels left the file -- before,
    # the store had to stay open for the lazy per-chunk reads.
    with zarr.ZipStore(str(path), mode="r") as store:
        root = zarr.open_group(store=store, mode="r")

        # --- schema check ---
        version = int(root.attrs.get("schema_version", 0))
        if version < MIN_SCHEMA or version > MAX_SCHEMA:
            logger.info(
                "Scene file schema %d outside supported range [%d, %d], will regenerate",
                version, MIN_SCHEMA, MAX_SCHEMA,
            )
            return None

        # --- fingerprint check ---
        if run_dir is not None:
            stored_fp = root.attrs.get("source_fingerprint")
            if stored_fp is not None:
                current_fp = compute_source_fingerprint(run_dir)
                if not fingerprint_matches(stored_fp, current_fp):
                    logger.info("Scene file source fingerprint mismatch, will regenerate")
                    return None

        _emit("scene_open", 1, 1)

        # --- /meta ---
        meta = root["meta"]
        manifest_raw = meta["manifest"][()]
        if isinstance(manifest_raw, np.ndarray):
            manifest_raw = manifest_raw.item()
        manifest = json.loads(manifest_raw) if isinstance(manifest_raw, str) else manifest_raw
        run_mode = str(meta.attrs.get("mode", "semantic"))
        frame_indices, clip_counts, image_size = _load_frame_meta(root)

        # --- /classes ---
        _emit("scene_classes", 0, 1)
        classes_config = _load_classes(root)
        _emit("scene_classes", 1, 1)

        # --- /final_cloud_index ---
        _emit("scene_cloud_index", 0, 1)
        fci = _load_fci(root)
        _emit("scene_cloud_index", 1, 1)

    return LoadedScene(
        manifest=manifest,
        classes_config=classes_config,
        final_cloud_index=fci,
        run_mode=run_mode,
        frame_indices=frame_indices,
        clip_counts=clip_counts,
        image_size=image_size,
        schema_version=version,
    )


def _load_frame_meta(root) -> tuple[np.ndarray, tuple[int, ...], tuple[int, int]]:
    """Frame metadata, from /meta on schema 2 or the old /frames group on 1."""
    group = root["meta"] if "frame_indices" in root["meta"] else root.get("frames")
    if group is None or "frame_indices" not in group:
        return np.zeros(0, dtype=np.int32), (), (0, 0)
    indices = np.asarray(group["frame_indices"][:], dtype=np.int64)
    clip_counts = tuple(int(x) for x in group["clip_counts"][:])
    size = (int(group.attrs.get("image_width", 0)), int(group.attrs.get("image_height", 0)))
    return indices, clip_counts, size


def _load_classes(root) -> "ClassConfig":
    from deepreefmap.config.classes import ClassConfig, SemanticClass

    g = root["classes"]
    ids = g["ids"][:]
    colors = g["colors"][:]
    names = g["names"][:]
    roles = g["roles"][:]
    classes_path_attr = str(g.attrs.get("classes_path", ""))
    classes_path = Path(classes_path_attr) if classes_path_attr else None

    classes = []
    for i in range(len(ids)):
        role_list = json.loads(roles[i]) if isinstance(roles[i], str) else roles[i]
        classes.append(SemanticClass(
            id=int(ids[i]),
            name=str(names[i]),
            color=(int(colors[i, 0]), int(colors[i, 1]), int(colors[i, 2])),
            roles=frozenset(role_list),
        ))
    return ClassConfig(classes=tuple(classes), path=classes_path)


def _load_fci(root) -> "FinalCloudIndex":
    from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

    g = root["final_cloud_index"]
    frame_order = tuple(int(x) for x in g["frame_order"][:])
    class_ids_arr = g["class_ids"][:]
    class_ids = tuple(int(x) for x in class_ids_arr)
    offsets = g["class_offsets"][:]

    xyz_all = g["xyz"][:]
    rgb_all = g["rgb"][:]
    semrgb_all = g["semrgb"][:]
    conf_all = g["conf"][:]

    n_steps = len(frame_order)
    pe_flat = g["prefix_end_flat"][:]

    xyz_by_class: dict[int, np.ndarray] = {}
    rgb_by_class: dict[int, np.ndarray] = {}
    semrgb_by_class: dict[int, np.ndarray] = {}
    conf_by_class: dict[int, np.ndarray] = {}
    prefix_end_by_class: dict[int, np.ndarray] = {}

    for i, cid in enumerate(class_ids):
        lo, hi = int(offsets[i]), int(offsets[i + 1])
        xyz_by_class[cid] = xyz_all[lo:hi]
        rgb_by_class[cid] = rgb_all[lo:hi]
        semrgb_by_class[cid] = semrgb_all[lo:hi]
        conf_by_class[cid] = conf_all[lo:hi]
        pe_lo = i * n_steps
        pe_hi = pe_lo + n_steps
        prefix_end_by_class[cid] = pe_flat[pe_lo:pe_hi]

    return FinalCloudIndex(
        frame_order=frame_order,
        class_ids=class_ids,
        xyz_by_class=xyz_by_class,
        rgb_by_class=rgb_by_class,
        semrgb_by_class=semrgb_by_class,
        conf_by_class=conf_by_class,
        prefix_end_by_class=prefix_end_by_class,
    )


