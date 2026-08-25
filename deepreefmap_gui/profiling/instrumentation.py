"""Stage timing marks and resource sampling for one reconstruction run."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from deepreefmap_gui.profiling.eta import STAGE_MESSAGE_TO_PHASE, stage_for_phase
from deepreefmap_gui.profiling.perf_sampler import ResourceSampler, peaks_from_marks

logger = logging.getLogger(__name__)

# Coarse pipeline stages, in order, that the timing marks bracket. The GUI reads
# these durations back to build a per-machine cost profile
# (profiling/run_history.py), so the keys match profiling/eta.py's stage keys.
STAGE_SPANS: tuple[tuple[str, str, str], ...] = (
    ("start", "preprocess", "startup"),
    ("preprocess", "mapping", "preprocess"),
    ("mapping", "cloud", "mapping"),
    ("cloud", "ortho", "cloud"),
    ("ortho", "save", "ortho"),
    ("save", "end", "save_view"),
    ("end", "scene_end", "scene_save"),
)

# The mark that opens each coarse stage, so a stage can be marked by name rather
# than by every caller knowing the mark vocabulary.
_STAGE_BEGIN_MARK: dict[str, str] = {stage: begin for begin, _end, stage in STAGE_SPANS}

# scene_save is the only stage the orchestrator does not drive: the scene file is
# written by the caller's `scene_writer` after run_reconstruction returns, because
# it needs the manifest the run just wrote. A run given no writer (headless, or a
# geometry-only pass with no cloud to cache) measures the other six and leaves
# this one absent.
WRITER_DRIVEN_STAGES: frozenset[str] = frozenset({"scene_save"})


def durations_from_marks(marks: dict[str, float]) -> dict[str, float]:
    """Wall-clock seconds per coarse stage from sequential monotonic marks."""
    durations: dict[str, float] = {}
    for begin, end, stage in STAGE_SPANS:
        if begin in marks and end in marks and marks[end] >= marks[begin]:
            durations[stage] = marks[end] - marks[begin]
    return durations


class RunInstrumentation:
    """One run's stage marks, memory sampling and machine profile.

    The real per-stage peaks land in the manifest and feed the pre-run memory
    check. A run that dies writes no manifest, so ``failed_run_manifest`` reads
    the same figures straight off here instead.
    """

    def __init__(self, output_dir: Path) -> None:
        from deepreefmap_gui.profiling.system_probe import probe_system

        self.marks: dict[str, float] = {"start": time.monotonic()}
        self.system_profile: dict = probe_system(output_dir).to_dict()
        self._sampler = ResourceSampler()
        self._sampler.start()

    def mark(self, name: str) -> None:
        self.marks[name] = time.monotonic()

    def close_open_stage(self) -> None:
        """End the stage still in flight, so a run that died is measured up to there.

        A span needs both marks before it has a duration or a peak, and the stage
        a run dies in is the one that never gets its closing mark. Without this
        the single most interesting stage of a failed run -- the one that
        exhausted memory -- is the one stage with no figures at all.
        """
        open_ends = [
            end for begin, end, _stage in STAGE_SPANS if begin in self.marks and end not in self.marks
        ]
        if open_ends:
            # The last one: only one stage is genuinely running, and a gap earlier
            # in the chain is a mark that was missed, not a stage still going.
            self.mark(open_ends[-1])

    def stage_durations(self) -> dict[str, float]:
        return durations_from_marks(self.marks)

    def total_seconds(self) -> float:
        """Wall-clock seconds from run start to the latest mark."""
        return max(self.marks.values()) - self.marks["start"]

    def stage_peaks(self) -> dict[str, dict[str, int | None]]:
        return peaks_from_marks(self._sampler.samples, STAGE_SPANS, self.marks)

    def stop(self) -> None:
        """Join the sampler thread, which otherwise polls for the life of the process."""
        self._sampler.stop()


def apply_manifest_timings(
    output_dir: Path,
    instr: RunInstrumentation,
    *,
    run_name: str | None = None,
    manifest_extra: dict | None = None,
) -> dict | None:
    """Fold measured durations/peaks (plus name + survey block) into run_manifest.json.

    ``run_reconstruction`` no longer stamps the run name or extra manifest blocks
    itself, so this is where they persist. Returns the merged manifest, or None
    when the run wrote no manifest to fold into.
    """
    import json

    from deepreefmap_gui.io.atomic import atomic_write_json

    manifest_path = output_dir / "run_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if run_name is not None:
        manifest["name"] = run_name
    manifest.update(manifest_extra or {})
    manifest["stage_durations"] = instr.stage_durations()
    manifest["stage_peaks"] = instr.stage_peaks()
    manifest["run_duration_s"] = instr.total_seconds()
    manifest["system_profile"] = instr.system_profile
    # Atomic: this rewrites the manifest the run just produced, and a truncated
    # one makes the finished run unloadable rather than merely untimed.
    atomic_write_json(manifest_path, manifest)
    return manifest


class _MarkingViewer:
    """Wrap the run viewer so stage transitions drive instrumentation marks.

    Records marks using the same names durations_from_marks expects; inner may be
    None (headless/batch), in which case viewer methods are no-ops.

    The orchestrator names only four stages and reports everything inside
    "outputs" by message, so the cloud/ortho/save boundaries are recovered from
    the message through the same routing table the progress bars use. Marking on
    the stage name alone left four of the seven STAGE_SPANS permanently empty.
    """

    _STAGE_TO_MARK = {"preprocess": "preprocess", "mapping": "mapping", "outputs": "cloud"}

    def __init__(self, inner, instr):
        self._inner = inner
        self._instr = instr
        # The set_data payload, captured on the pipeline thread. The viewer's own
        # copy arrives through a queued signal and may not have been indexed yet
        # when the run ends, so the scene writer reads it from here instead.
        self.data: dict | None = None
        # Quality warnings the pipeline raised, bound for the manifest: the
        # live viewer's list is cleared by the next pass of a batch.
        self.warnings: list[str] = []

    def _mark_once(self, name) -> None:
        if name and name not in self._instr.marks:
            self._instr.mark(name)

    def _mark_for(self, stage, message) -> None:
        """The begin-mark of whichever coarse stage this report belongs to.

        Stage name first: an ortho message can be the first thing seen inside
        "outputs", and marking ortho before cloud would give the cloud span a
        negative width, which durations_from_marks then drops.
        """
        self._mark_once(self._STAGE_TO_MARK.get(stage))
        phase = STAGE_MESSAGE_TO_PHASE.get(message or "")
        coarse = stage_for_phase(phase) if phase else None
        self._mark_once(_STAGE_BEGIN_MARK.get(coarse) if coarse else None)

    def start_run(self, run_label, output_dir):
        if self._inner is not None:
            self._inner.start_run(run_label, output_dir)

    def set_stage(self, stage, status, message=None):
        self._mark_for(stage, message)
        if status == "warning" and message and message not in self.warnings:
            self.warnings.append(message)
        if self._inner is not None:
            self._inner.set_stage(stage, status, message)

    def update_progress(self, *a, **k):
        # The cloud loop reports through update_progress, not set_stage, so the
        # message has to be inspected here too.
        stage = a[0] if a else k.get("stage")
        self._mark_for(stage, k.get("message"))
        if self._inner is not None:
            self._inner.update_progress(*a, **k)

    def set_data(self, **k):
        self.data = k
        if self._inner is not None:
            self._inner.set_data(**k)

    def mark_outputs_ready(self, output_dir, output_files):
        if "end" not in self._instr.marks:
            self._instr.mark("end")
        if self._inner is not None:
            self._inner.mark_outputs_ready(output_dir, output_files)

    def fail_run(self, stage, error_message):
        if self._inner is not None:
            self._inner.fail_run(stage, error_message)

    def close(self):
        if self._inner is not None:
            self._inner.close()

    def wait_forever(self):
        if self._inner is not None:
            self._inner.wait_forever()


def _geometry_source(output_dir: Path) -> str | None:
    """Whether the saved mapping carries world points, without loading them.

    A mapping npz saved without world geometry holds an empty placeholder array,
    so the zip member's size separates the two cases at header cost.
    """
    import zipfile

    npz_path = output_dir / "mapping_outputs.npz"
    if not npz_path.exists():
        return None
    try:
        with zipfile.ZipFile(npz_path) as zf:
            info = zf.getinfo("world_points.npy")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    return "world_points" if info.file_size > 256 else "depth_unprojection"


def _scale_type(output_dir: Path) -> str | None:
    import numpy as np

    npz_path = output_dir / "mapping_outputs.npz"
    if not npz_path.exists():
        return None
    try:
        with np.load(npz_path) as data:
            if "scale_type" in data.files:
                return str(data["scale_type"])
    except Exception:
        logger.warning("Could not read scale_type from %s", npz_path, exc_info=True)
    return None


def _run_identity_extra(output_dir: Path, kwargs: dict, run_started_at: str) -> dict:
    """Identity and launch-parameter fields for the run manifest.

    The library's manifest records only what it needs to reload a run. Which
    clips went in (hashed for related-run grouping), every launch parameter, and
    the library version are the app's to record: it launched the run in-process
    and knows them all. Never raises: losing provenance must not lose the run.
    """
    try:
        from deepreefmap import __version__ as deepreefmap_version

        from deepreefmap_gui.io.video_hash import describe_videos

        video_paths = [Path(p) for p in kwargs.get("video_paths", [])]
        metas = describe_videos(video_paths)
        transect_length = kwargs.get("transect_length")
        transect_crop_width = kwargs.get("transect_crop_width")
        extra: dict = {
            "input_videos": [str(p) for p in video_paths],
            "video_hashes": [m["hash"] for m in metas],
            "video_sizes": [m["size_bytes"] for m in metas],
            "video_mtimes": [m["mtime"] for m in metas],
            "fps": kwargs.get("fps"),
            "begin_s": kwargs.get("begin_s"),
            "end_s": kwargs.get("end_s"),
            "processing_width": kwargs.get("processing_width"),
            "processing_height": kwargs.get("processing_height"),
            "grid_bins": kwargs.get("grid_bins"),
            "replacement_radius_factor": kwargs.get("replacement_radius_factor"),
            "replacement_radius_estimation_frames": kwargs.get("replacement_radius_estimation_frames"),
            "replacement_radius_override": kwargs.get("replacement_radius_override"),
            "enable_tsdf": bool(kwargs.get("enable_tsdf", False)),
            "mapping_options": dict(kwargs.get("mapping_options") or {}),
            "refine_intrinsics_from_mapper": bool(kwargs.get("refine_intrinsics_from_mapper", False)),
            "deepreefmap_version": deepreefmap_version,
            "run_timestamp": run_started_at,
            "transect": {
                "length": transect_length,
                "crop_width": transect_crop_width,
                "applied": transect_length is not None and transect_crop_width is not None,
            },
        }
        geometry_source = _geometry_source(output_dir)
        if geometry_source is not None:
            extra["geometry_source"] = geometry_source
        scale_type = _scale_type(output_dir)
        if scale_type is not None:
            extra["scale_type"] = scale_type
        return extra
    except Exception:
        logger.warning("Failed to build run identity fields", exc_info=True)
        return {}


def _record_run_command(output_dir: Path, kwargs: dict) -> dict:
    """The CLI equivalent of this run, dropped in the run dir and returned for
    the manifest.

    Written before the pipeline starts so a run that crashes or is cancelled
    still leaves behind what it was asked to do. Never raises: losing the record
    must not lose the run.
    """
    from deepreefmap_gui.runs.run_command import (
        build_reconstruct_argv,
        format_command,
        write_run_command_script,
    )

    try:
        argv = build_reconstruct_argv(kwargs)
        write_run_command_script(output_dir, argv)
        return {"cli_argv": argv, "cli_command": format_command(argv)}
    except Exception:
        logger.warning("Failed to record the run command", exc_info=True)
        return {}


def failed_run_manifest(
    output_dir: Path,
    instr: RunInstrumentation,
    kwargs: dict,
    extra: dict,
    run_started_at: str,
) -> dict:
    """The manifest a run that died would have written, assembled from memory.

    A failed run may have left no manifest on disk at all, so the only route out
    for the resources it reached is this one, and they are the figures worth
    keeping: the stage that exhausted memory is the stage that killed the run.
    Built in the shape ``apply_manifest_timings`` writes, so one reader serves
    both, and left as partial as the run was -- nothing after the stage it died
    in was ever measured, which is itself the signal.

    Never written to disk. A manifest beside a half-finished run would read as a
    run that can be loaded.
    """
    instr.close_open_stage()
    manifest = dict(extra)
    manifest.update(_run_identity_extra(output_dir, kwargs, run_started_at))
    # From the launch parameters, not from the pipeline, which never reached the
    # point of recording them. Spelled the way it would have spelled them.
    manifest["segmentation_model"] = (
        "__skip__" if kwargs.get("skip_segmentation") else kwargs.get("segmentation_name")
    )
    manifest["mapping_backend"] = kwargs.get("mapping_name")
    manifest["stage_durations"] = instr.stage_durations()
    manifest["stage_peaks"] = instr.stage_peaks()
    manifest["run_duration_s"] = instr.total_seconds()
    manifest["system_profile"] = instr.system_profile
    return manifest


def instrumented_reconstruction(
    *,
    run_name: str | None = None,
    manifest_extra: dict | None = None,
    cached_stages: "frozenset[str] | None" = None,
    scene_writer: "Callable[[Path, dict, dict], None] | None" = None,
    on_failure: "Callable[[dict], None] | None" = None,
    **kwargs,
) -> None:
    """run_reconstruction with stage timing + memory sampling, folded into the
    run manifest and the local run-history profile afterwards.

    ``run_name`` and ``manifest_extra`` are written to the manifest after the run
    (``run_reconstruction`` no longer accepts them). Stage marks are captured via
    a viewer proxy since the orchestrator no longer emits ``on_mark`` callbacks.

    ``scene_writer`` is called with the output dir, the run's ``set_data``
    payload and the merged manifest once the pipeline is done, and is what makes
    the scene_save span measurable. It runs inside the sampled window so its
    memory peak is recorded too, and a failure is logged rather than raised: the
    scene file is a cache, and losing it must not lose the run.

    ``cached_stages`` names the coarse stages this run read from a seeded cache.
    Their spans measure a hard-link rather than the work, so they are recorded
    in the manifest as ``resumed_stages`` and withheld from the timing profile;
    the rest of the run is still worth learning from.

    ``on_failure`` is handed the partial manifest of a run that raised, before
    the exception carries on. The manifest on disk cannot carry it -- a run that
    dies may never write one -- and a run that hit the ceiling is the one whose
    resource trace is worth having. It decides nothing: a cancellation raises
    through here too, and the caller is what tells the two apart.
    """
    from datetime import datetime, timezone

    from deepreefmap.pipeline.orchestrator import run_reconstruction

    from deepreefmap_gui.io.classes_default import resolve_classes_path
    from deepreefmap_gui.profiling.run_history import record_run_from_manifest

    run_started_at = datetime.now(timezone.utc).isoformat()
    output_dir = Path(kwargs["output_dir"])
    instr = RunInstrumentation(output_dir)
    proxy = _MarkingViewer(kwargs.pop("viewer", None), instr)
    extra = dict(manifest_extra or {})
    if cached_stages:
        extra["resumed_stages"] = sorted(cached_stages)
    extra.update(_record_run_command(output_dir, kwargs))
    # The library's preprocess cache key reads the classes file directly, so a
    # None (bundled default) must become the packaged copy's absolute path
    # before the run rather than a repo-relative path resolved against cwd.
    kwargs["classes_path"] = resolve_classes_path(kwargs.get("classes_path"))
    # The pipeline's manifest does not carry the batch size, and a VRAM peak is
    # only comparable to an estimate made at the same one.
    if "preprocess_batch_size" in kwargs:
        extra.setdefault("preprocess_batch_size", int(kwargs["preprocess_batch_size"]))
    manifest: dict | None = None
    try:
        run_reconstruction(viewer=proxy, **kwargs)
        # Into the extra block both folds share, so the post-scene re-fold
        # keeps them. Only when there are any: an absent key reads as clean.
        if proxy.warnings:
            extra["quality_warnings"] = list(proxy.warnings)
        extra.update(_run_identity_extra(output_dir, kwargs, run_started_at))
        # Fold the run name, survey block and timings in before the scene file is
        # written: the scene embeds the manifest and is read back in place of it,
        # so a scene built from the raw pipeline manifest would come back missing
        # the name the user gave the run and the survey block that files it.
        manifest = apply_manifest_timings(
            output_dir, instr, run_name=run_name, manifest_extra=extra
        )
        if scene_writer is not None and proxy.data is not None and manifest is not None:
            try:
                scene_writer(output_dir, proxy.data, manifest)
            except Exception:
                logger.warning("Scene file generation failed", exc_info=True)
            else:
                instr.mark("scene_end")
                # Re-fold so the manifest on disk carries the scene_save duration
                # and peak; the copy inside the scene file predates them.
                manifest = apply_manifest_timings(
                    output_dir, instr, run_name=run_name, manifest_extra=extra
                )
    except Exception:
        if on_failure is not None:
            try:
                on_failure(failed_run_manifest(output_dir, instr, kwargs, extra, run_started_at))
            except Exception:
                # Guarded because the run's own error is the one the user needs:
                # losing it to a second failure here would leave a pass that says
                # only that provenance could not be recorded.
                logger.warning("Could not record what the failed run measured", exc_info=True)
        raise
    finally:
        instr.stop()
    if manifest is not None:
        record_run_from_manifest(manifest, cached_stages=cached_stages)
