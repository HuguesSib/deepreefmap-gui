"""GUI-side cached-run loading with scene-file fast path.

Wraps the library's ``load_cached_run`` (which no longer knows about scene files)
with the GUI's zarr scene-file cache: a completed run loads from an up-to-date
scene file when present, otherwise takes the library slow path and regenerates
the scene file in the background for next time.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np
from deepreefmap.config.classes import ClassConfig
from deepreefmap.pipeline import resume as resume_mod
from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult, SemanticPointCloud
from deepreefmap.pipeline.run_loader import (
    SEMANTIC_MODE,
    load_cached_run,
)

from deepreefmap_gui.io.atomic import atomic_write_json
from deepreefmap_gui.io.classes_default import resolve_classes_path
from deepreefmap_gui.io.lazy_frames import FrameAccessor, RunDirFrameAccessor
from deepreefmap_gui.io.scene_file import (
    SCENE_FILE_SUFFIX,
    SCHEMA_VERSION,
    LazyFrameBatch,
    ProgressCB,
    find_scene_file,
    load_scene_file,
    prune_other_scene_files,
    save_scene_file,
    scene_file_name,
    tmp_write_in_progress,
)

if TYPE_CHECKING:
    from deepreefmap.pointcloud.filters import PointFilterConfig
    from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

logger = logging.getLogger(__name__)


@dataclass
class GuiLoadedRun:
    """A loaded run plus the GUI-only scene-file fields the viewer wires from."""

    run_dir: Path
    manifest: dict[str, Any]
    classes_config: ClassConfig
    frame_batch: FrameBatch | LazyFrameBatch
    mapping_result: MappingSequenceResult
    output_files: list[str]
    mode: str = SEMANTIC_MODE
    reference_cloud: SemanticPointCloud = field(default_factory=SemanticPointCloud.empty)
    geometry_xyz: np.ndarray | None = None
    geometry_rgb: np.ndarray | None = None
    world_points_warning: str | None = None
    from_scene_file: bool = False
    # Typed as the protocol, not a concrete class: the fast path now reads frames
    # from the run dir, so this holds a RunDirFrameAccessor whose close() is a
    # no-op. Kept so the window's teardown keeps one place to release whatever
    # the loaded run is holding.
    scene_accessor: FrameAccessor | None = None
    final_cloud_index: FinalCloudIndex | None = None
    # Schema of the scene file this run came from, so an older one can be
    # upgraded in place. 0 when the run did not come from a scene file.
    scene_schema_version: int = 0


def load_run(
    run_dir: Path,
    *,
    point_filter_config: PointFilterConfig | None = None,
    regenerate_scene_file: bool = True,
) -> GuiLoadedRun:
    """Load a completed reconstruction folder.

    Prefers an up-to-date scene file, otherwise takes the library slow path.

    ``regenerate_scene_file`` starts the rebuild on a background thread before
    returning. The GUI passes False and calls ``generate_scene_file_async``
    itself once the viewer is up, so the write neither competes with the point
    upload nor reports progress into the middle of the load's own bars.
    """
    run_dir = Path(run_dir)

    # --- Fast path: try scene file ---
    scene_path = find_scene_file(run_dir)
    if scene_path is not None:
        try:
            loaded = _load_from_scene_file(scene_path, run_dir)
            if loaded is not None:
                logger.info("Loaded from scene file (fast path): %s", scene_path)
                _upgrade_scene_file(run_dir, loaded)
                return loaded
            logger.info("Scene file stale or incompatible, falling back to slow path")
        except Exception:
            logger.warning("Scene file load failed, falling back to slow path", exc_info=True)

    # Clean up .tmp files left by an interrupted background generation -- but not
    # one that is still being written. Reaching here means the scene file was
    # missing or unusable, which is exactly the state a run is in *while* its
    # first scene file is being generated, so the run this is called for is the
    # likeliest one to have a live write in flight. Deleting it made the write
    # fail into generate_scene_file_async's swallowed handler, leaving that run
    # on the slow path for good.
    for tmp in run_dir.glob("*" + SCENE_FILE_SUFFIX + ".tmp"):
        if tmp_write_in_progress(tmp):
            logger.debug("Leaving in-progress scene temp file alone: %s", tmp)
            continue
        try:
            tmp.unlink()
            logger.info("Cleaned up stale temp file: %s", tmp)
        except OSError:
            pass

    # --- Slow path (library loader) ---
    _heal_manifest_classes(run_dir)
    result = _wrap_loaded_run(load_cached_run(run_dir, point_filter_config=point_filter_config))

    if regenerate_scene_file and scene_file_pending(result):
        generate_scene_file_async(run_dir, result)

    return result


def _heal_manifest_classes(run_dir: Path) -> None:
    """Repoint the manifest at the bundled classes YAML when its own is gone.

    A manifest records the classes YAML by path, and that path dies easily: a
    reinstalled venv, a run dir copied off the field laptop, an older app
    version that wrote it relative. The library's loader raises on a dangling
    path. The bundled YAML is the file nearly every run used anyway, so a run
    with defaults keeps opening. A missing custom YAML still fails loudly
    rather than silently recolouring someone's classes.
    """
    manifest_path = run_dir / "run_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = manifest.get("classes", "configs/classes_coralscapes.yaml")
        path = Path(str(recorded))
        if path.exists() or (run_dir / path).exists():
            return
        if path.name != "classes_coralscapes.yaml":
            return
        manifest["classes"] = str(resolve_classes_path(None))
        atomic_write_json(manifest_path, manifest)
        logger.info("Manifest classes path was dangling; repointed to the bundled YAML")
    except Exception:
        # The loader will surface whatever is actually wrong with the manifest.
        pass


def scene_file_pending(result: GuiLoadedRun) -> bool:
    """Whether this run still owes a scene file. Geometry-only runs never do."""
    return (
        not result.from_scene_file
        and result.mode == SEMANTIC_MODE
        and result.reference_cloud is not None
        and len(result.reference_cloud) > 0
    )


def _load_from_scene_file(scene_path: Path, run_dir: Path) -> GuiLoadedRun | None:
    """Fast path: the cloud index from the scene, everything else from the run dir.

    The scene file holds only what the run directory cannot cheaply reproduce.
    Frames come from the PNG caches and the mapping arrays from the npz, both
    read-only and both cheaper than a copy inside the scene.
    """
    scene = load_scene_file(scene_path, run_dir=run_dir)
    if scene is None:
        return None

    mapping_result = resume_mod.load_mapping_result(run_dir)
    if mapping_result is None:
        # Same outcome as a stale scene: fall back to the library loader, which
        # raises its own clear error for a run missing its mapping artifact.
        logger.info("Scene file usable but mapping_outputs.npz is not, falling back")
        return None

    accessor = RunDirFrameAccessor(
        run_dir, scene.frame_indices, scene.clip_counts, scene.image_size
    )
    fb = LazyFrameBatch(accessor, mapping_result.intrinsics)

    return GuiLoadedRun(
        run_dir=run_dir,
        manifest=scene.manifest,
        classes_config=scene.classes_config,
        frame_batch=fb,
        mapping_result=mapping_result,
        output_files=scene.manifest.get("output_files", []),
        mode=scene.run_mode,
        from_scene_file=True,
        scene_accessor=accessor,
        final_cloud_index=scene.final_cloud_index,
        world_points_warning=_world_points_fallback_warning(scene.manifest, mapping_result),
        scene_schema_version=scene.schema_version,
    )


def _upgrade_scene_file(run_dir: Path, loaded: GuiLoadedRun) -> None:
    """Rewrite an older scene file in the current schema, then drop the old one.

    A schema-1 scene carries the frame pixels and the mapping arrays, which is
    most of its size and none of its value. Everything the new one needs was
    just read out of it, so this is a rewrite of ~31 MB rather than a rebuild,
    and it reclaims the rest of the file the first time a run is opened.

    Best-effort: the run is already loaded and usable, so a failure here is
    logged and forgotten rather than allowed to fail the load.
    """
    if loaded.final_cloud_index is None or loaded.scene_schema_version >= SCHEMA_VERSION:
        return
    try:
        out = run_dir / scene_file_name(loaded.manifest, run_dir)
        save_scene_file(
            out,
            manifest=loaded.manifest,
            classes_config=loaded.classes_config,
            mapping_result=loaded.mapping_result,
            frame_batch=loaded.frame_batch,  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass
            final_cloud_index=loaded.final_cloud_index,
            run_dir=run_dir,
        )
        prune_other_scene_files(run_dir, keep=out)
        logger.info(
            "Upgraded scene file from schema %d to %d: %s",
            loaded.scene_schema_version, SCHEMA_VERSION, out.name,
        )
    except Exception:
        logger.warning("Scene file upgrade failed, leaving the old one alone", exc_info=True)


def _world_points_fallback_warning(
    manifest: dict[str, Any], mapping_result: MappingSequenceResult | None
) -> str | None:
    """Detect a LoGeR run whose cloud silently fell back to depth-unprojection.

    An older ``mapping_outputs.npz`` may carry no ``world_points``, so a resumed
    run rebuilds geometry from depth+pose: degraded, not failed. A recorded
    ``geometry_source == "depth_unprojection"`` is intentional and not flagged.
    """
    if str(manifest.get("mapping_backend", "")) not in {"loger", "loger_star"}:
        return None
    if getattr(mapping_result, "world_points", None) is not None:
        return None
    if manifest.get("geometry_source") == "depth_unprojection":
        return None
    msg = (
        "LoGeR run loaded without per-point world geometry (mapping_outputs.npz "
        "predates world_points persistence); the cloud uses the depth-unprojection "
        "fallback. Re-run the reconstruction for full LoGeR geometry."
    )
    logger.warning(msg)
    return msg


def _wrap_loaded_run(loaded) -> GuiLoadedRun:
    """Re-express a library ``LoadedRun`` as a ``GuiLoadedRun`` (scene fields default)."""
    return GuiLoadedRun(
        run_dir=loaded.run_dir,
        manifest=loaded.manifest,
        classes_config=loaded.classes_config,
        frame_batch=loaded.frame_batch,
        mapping_result=loaded.mapping_result,
        output_files=loaded.output_files,
        mode=loaded.mode,
        reference_cloud=loaded.reference_cloud,
        geometry_xyz=loaded.geometry_xyz,
        geometry_rgb=loaded.geometry_rgb,
        world_points_warning=_world_points_fallback_warning(loaded.manifest, loaded.mapping_result),
    )


def write_scene_file(
    run_dir: Path,
    *,
    manifest: dict,
    classes_config: ClassConfig,
    mapping_result: MappingSequenceResult,
    frame_batch: FrameBatch,
    reference_cloud: SemanticPointCloud,
    progress_cb: ProgressCB | None = None,
) -> Path:
    """Build the cloud index and write the run's scene file. Returns its path.

    The index is rebuilt here rather than borrowed from the viewer: the viewer
    builds its own on the GUI thread, and sharing one across the two would mean
    synchronising a structure neither owns. The cost is one extra pass over the
    cloud, which the caller reports as part of the scene_save stage.
    """
    from deepreefmap.pointcloud.final_cloud_index import build_final_cloud_index

    if progress_cb is not None:
        progress_cb("scene_index", 0, 1)
    frame_order = [int(f.frame_index) for f in frame_batch.frames]
    fci = build_final_cloud_index(reference_cloud, frame_order, classes_config.id_to_color)
    if progress_cb is not None:
        progress_cb("scene_index", 1, 1)

    out = run_dir / scene_file_name(manifest, run_dir)
    save_scene_file(
        out,
        manifest=manifest,
        classes_config=classes_config,
        mapping_result=mapping_result,
        frame_batch=frame_batch,
        final_cloud_index=fci,
        run_dir=run_dir,
        progress_cb=progress_cb,
    )
    _write_web_cloud_or_warn(
        run_dir, fci, frame_order, classes_config, reference_cloud, mapping_result
    )
    prune_other_scene_files(run_dir, keep=out)
    return out


def _write_web_cloud_or_warn(
    run_dir, fci, frame_order, classes_config, cloud, mapping_result
) -> None:
    """Write the browser export beside the scene, never failing the run for it.

    Reuses the index the scene write just built, so the cost is serialisation.
    The archive walks the run directory, so once written the file uploads like
    any other output.
    """
    from deepreefmap_gui.io.web_cloud import WEB_CLOUD_FILENAME, camera_track, write_web_cloud

    try:
        write_web_cloud(
            run_dir / WEB_CLOUD_FILENAME,
            fci,
            frame_order,
            classes_config.id_to_name,
            classes_config.id_to_color,
            has_confidence=getattr(cloud, "confidence", None) is not None,
            cameras=camera_track(mapping_result, frame_order),
        )
    except Exception:
        logger.warning("Could not write the web cloud for %s", run_dir, exc_info=True)


# A scene is tens of megabytes against a run directory of gigabytes, so this
# only refuses on a drive already out of room.
_SCENE_MIN_FREE_BYTES = 1024**3


def write_scene_file_from_run_data(
    run_dir: Path, data: dict, manifest: dict, *, progress_cb: ProgressCB | None = None
) -> Path | None:
    """Write the scene file straight from a finished run's ``set_data`` payload.

    ``manifest`` is the merged one, not the file on disk: the scene embeds it and
    is read back in place of it, so it has to carry the run name and survey block.

    Geometry-only runs carry no reference cloud and get no scene file.
    """
    cloud = data.get("reference_cloud")
    if cloud is None or len(cloud) == 0:
        return None
    # Before the index is built, so a full drive costs nothing.
    try:
        free = shutil.disk_usage(run_dir).free
    except OSError:
        free = None
    if free is not None and free < _SCENE_MIN_FREE_BYTES:
        logger.warning(
            "Skipping scene file for %s: %.1f GB free", run_dir, free / 1024**3
        )
        return None
    return write_scene_file(
        run_dir,
        manifest=manifest,
        classes_config=data["classes_config"],
        mapping_result=data["mapping_result"],
        frame_batch=data["frame_batch"],
        reference_cloud=cloud,
        progress_cb=progress_cb,
    )


def generate_scene_file_async(
    run_dir: Path,
    result: GuiLoadedRun,
    *,
    progress_cb: ProgressCB | None = None,
    on_done: Callable[[], None] | None = None,
) -> threading.Thread:
    """Rebuild a pre-existing run's scene file on a daemon thread.

    Only reached by runs reconstructed before the pipeline started writing its
    own scene file; a fresh run already has one by the time it is first opened.
    """

    def _worker() -> None:
        try:
            out = write_scene_file(
                run_dir,
                manifest=result.manifest,
                classes_config=result.classes_config,
                mapping_result=result.mapping_result,
                frame_batch=result.frame_batch,  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass
                reference_cloud=result.reference_cloud,
                progress_cb=progress_cb,
            )
            logger.info("Scene file generated for next load: %s", out)
        except Exception:
            logger.warning("Background scene file generation failed", exc_info=True)
        finally:
            if on_done is not None:
                on_done()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread
