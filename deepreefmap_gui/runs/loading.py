"""Opening a run that was already processed, and the controls that stop one.

Loading is the read-only path: a cached run directory or a scene file goes out
to a worker thread and comes back as a cloud on screen. The stop and pause
controls sit here too, though they act on a run in flight, because they are the
same two buttons: with no pipeline running, stop cancels a load instead.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.profiling.eta import STAGE_MESSAGE_TO_PHASE as _STAGE_MESSAGE_TO_PHASE
from deepreefmap_gui.runs.progress import _LOAD_STAGE_TO_PHASE

if TYPE_CHECKING:
    from deepreefmap.postproc.ortho_outputs import TransectCropParams

    from deepreefmap_gui.runs.loaded_run import GuiLoadedRun

logger = logging.getLogger(__name__)

# Manifests predating the grid_bins run param fall back to the value the ortho
# builder itself defaults to, so those runs preview as they always did.
_DEFAULT_GRID_BINS = 2000


def _manifest_grid_bins(manifest: dict) -> int:
    """The ortho bin count the run was published with."""
    raw = manifest.get("grid_bins")
    if not isinstance(raw, int) or raw <= 0:
        return _DEFAULT_GRID_BINS
    return raw


def _manifest_transect_crop(manifest: dict) -> TransectCropParams | None:
    """The transect crop the run was published with, if one was applied."""
    from deepreefmap.postproc.ortho_outputs import TransectCropParams

    transect = manifest.get("transect")
    if not isinstance(transect, dict) or not transect.get("applied"):
        return None
    length = transect.get("length")
    width = transect.get("crop_width")
    if not isinstance(length, (int, float)) or not isinstance(width, (int, float)):
        return None
    return TransectCropParams(transect_length_m=float(length), crop_width_m=float(width))


class RunLoadingMixin(MixinBase):
    """DeepReefMapWindow methods for loading cached runs and driving the transport."""

    # Bumped per load so a result that arrives after a newer load started can be
    # told apart from the current one. Ordering is not enough on its own: a run
    # opened first can finish last, because the slow path takes minutes where a
    # scene-file hit takes under a second.
    _load_generation = 0

    def _cancel_load(self) -> None:
        # Soft cancel: the worker thread can't be interrupted mid-read, but
        # we set a flag so _apply_loaded_run drops the result when it eventually
        # arrives. The thread is a daemon and will exit with the process.
        self._load_cancelled = True
        self._spinner_stop.setVisible(False)
        self._reset_progress()
        self._status_label.setText("Load cancelled.")

    def _run_in_flight(self) -> bool:
        thread = getattr(self, "_pipeline_thread", None)
        return thread is not None and thread.is_alive()

    def _end_run_controls(self) -> None:
        """Return the transport controls to idle. Start is never shown: the Run
        step's own button is what launches a batch."""
        self._spinner_stop.setVisible(False)
        self._pause_btn.setVisible(False)
        self._set_storage_compact(False)

    def _begin_run_controls(self) -> None:
        """Raise pause and the stop spinner while work is in flight."""
        self._spinner_stop.set_stopping(False)
        self._spinner_stop.setVisible(True)
        self._pause_btn.setVisible(True)
        self._pause_btn.setEnabled(True)
        self._pause_btn.setChecked(False)
        self._set_storage_compact(True)

    def _set_storage_compact(self, running: bool) -> None:
        """Narrow the storage bars to the drive being written to while a run works.

        Free space matters most mid-run, so they stay rather than being hidden,
        but four drives beside a live estimate is more than the row can hold.
        """
        bars = getattr(self, "_storage_bars", None)
        if bars is not None:
            bars.set_compact(running)

    def _on_stop_clicked(self) -> None:
        # The spinner is shared between a live pipeline run and a cached-run
        # load. A running pipeline owns the cancel/pause events; otherwise the
        # click aborts a cached load.
        pipeline_running = (
            getattr(self, "_pipeline_thread", None) is not None
            and self._pipeline_thread is not None
            and self._pipeline_thread.is_alive()
        )
        if not pipeline_running:
            self._cancel_load()
            return
        cancel = getattr(self, "_cancel_event", None)
        if cancel is not None:
            cancel.set()
        # Set, not cleared: a worker parked on the pause gate has to be released
        # before it can observe the cancel it was just given.
        pause = getattr(self, "_pause_event", None)
        if pause is not None:
            pause.set()
        self._spinner_stop.set_stopping(True)
        self._pause_btn.setEnabled(False)
        # Route through the status base text so the elapsed-time ticker keeps
        # this message rather than reverting it on its next tick.
        self._status_base_text = "Stopping reconstruction…"
        self._status_count_text = ""
        self._render_status()

    def _on_pause_toggled(self, paused: bool) -> None:
        if not hasattr(self, "_pause_event") or self._pause_event is None:
            return
        from deepreefmap_gui.core.icons import pause_icon, play_icon

        if paused:
            self._pause_event.clear()
            self._pause_btn.setIcon(play_icon())
            self._pause_btn.setToolTip("Resume the reconstruction")
            self._status_label.setText("Reconstruction paused.")
        else:
            self._pause_event.set()
            self._pause_btn.setIcon(pause_icon())
            self._pause_btn.setToolTip("Pause the reconstruction at the next safe checkpoint.")
            self._status_label.setText("Reconstruction resumed.")

    def _auto_load_run(self, run_dir: Path) -> None:
        self._load_cancelled = False
        self._load_generation += 1
        self._status_label.setText(f"Loading run from {run_dir.name}…")
        self._begin_progress(self._load_model)
        # No stage fraction until the first stage callback arrives.
        self._run_progress.stage_percent = None
        self._spinner_stop.set_stopping(False)
        self._spinner_stop.setVisible(True)
        threading.Thread(
            target=self._load_run_worker,
            args=(run_dir, self._load_generation),
            daemon=True,
        ).start()

    def _load_run_worker(self, run_dir: Path, generation: int) -> None:
        try:
            from deepreefmap_gui.runs.loaded_run import load_run

            # The library loader and scene-file reader no longer emit per-step
            # progress, so the load shows the indeterminate bar set in
            # _auto_load_run rather than a staged breakdown.
            #
            # The scene-file rebuild is deferred to _apply_loaded_run: started
            # here it would run against the viewer's point upload and report into
            # the middle of the load's own phases.
            result = load_run(run_dir, regenerate_scene_file=False)
            self._sig_run_loaded.emit(result, str(run_dir), "", generation)
        except Exception as exc:
            logger.exception("Failed to load cached run")
            self._sig_run_loaded.emit(None, str(run_dir), str(exc)[:300], generation)

    _STAGE_LABELS = {
        "manifest": "Reading manifest",
        "classes": "Loading classes",
        "mapping": "Loading mapping outputs",
        "frames": "Loading frames",
        "cloud": "Building semantic cloud",
        "cloud_concatenating": "Concatenating point arrays",
        "cloud_replacing": "Applying replacement radius",
        "cloud_replacing_keys": "Replacement radius: computing voxel keys",
        "cloud_replacing_sort": "Replacement radius: sorting points",
        "cloud_replacing_select": "Replacement radius: selecting representatives",
        "cloud_voxelizing": "Reducing by voxel size",
        "geometry": "Loading geometry cloud",
        "scene_open": "Opening scene file",
        "scene_classes": "Reading class config",
        "scene_cloud_index": "Reading point cloud index",
        "scene_mapping": "Reading mapping data",
        # The write side. save_scene_file and load_scene_file use disjoint stage
        # names, so these never collide with the "Reading …" labels above.
        "scene_index": "Indexing cloud for scene file",
        "scene_meta": "Writing scene metadata",
        "scene_fci": "Writing point cloud to scene file",
        "scene_done": "Scene file written",
    }

    def _start_deferred_scene_file(self, run_dir: Path, result: GuiLoadedRun) -> bool:
        """Rebuild a missing scene file now the viewer is up. True if one started.

        Runs made before the pipeline wrote its own scene file still need one.
        The bars stay up for it: it is the last phase of _LOAD_PHASES, so the
        fill carries straight on from "Finalising viewer" rather than resetting
        and then jumping back to life.
        """
        from deepreefmap_gui.runs.loaded_run import generate_scene_file_async, scene_file_pending

        if self._load_cancelled or not scene_file_pending(result):
            return False
        self._status_label.setText("Building scene file…")
        generate_scene_file_async(
            run_dir,
            result,
            progress_cb=self._sig_load_progress.emit,
            on_done=self._sig_scene_file_done.emit,
        )
        return True

    def _on_scene_file_done(self) -> None:
        self._reset_progress()

    def _on_load_progress(self, stage: str, cur: int, tot: int) -> None:
        if self._load_cancelled:
            return
        label = self._STAGE_LABELS.get(stage, stage)
        phase_key = _LOAD_STAGE_TO_PHASE.get(stage, stage)
        self._apply_progress(phase_key, label, current=cur, total=tot)

    def _apply_loaded_run(
        self,
        result: GuiLoadedRun | None,
        run_dir_str: str,
        error: str,
        generation: int,
    ) -> None:
        import time as _time

        from deepreefmap.pipeline.run_loader import GEOMETRY_ONLY_MODE

        _t0 = _time.monotonic()

        # Superseded by a later load. Release what it opened and leave the
        # window alone -- the bars and the spinner belong to that later load
        # now, so resetting them here would blank a load still in progress.
        if generation != self._load_generation:
            logger.info("Dropping superseded load of %s", run_dir_str)
            if result is not None and result.scene_accessor is not None:
                result.scene_accessor.close()
            return

        self._spinner_stop.setVisible(False)

        if self._load_cancelled:
            self._reset_progress()
            if result is not None and result.scene_accessor is not None:
                result.scene_accessor.close()
            return

        run_dir = Path(run_dir_str)
        if error or result is None:
            self._status_label.setText(f"Error loading run: {error}")
            self._reset_progress()
            return

        if hasattr(self, "_scene_accessor") and self._scene_accessor is not None:
            self._scene_accessor.close()
            self._scene_accessor = None
        self._scene_accessor = getattr(result, "scene_accessor", None)

        self._apply_progress("viewer_index_cloud", "Setting up viewer", 0, 0, flush=True)

        _t1 = _time.monotonic()
        if result.mode == GEOMETRY_ONLY_MODE:
            fb = result.frame_batch
            mr = result.mapping_result
            if fb is not None and mr is not None and result.geometry_xyz is not None:
                self._viewer.load_geometry_scene(
                    fb,
                    mr,
                    result.geometry_xyz,
                    result.geometry_rgb,  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass; geometry arrays optional
                )
                self._show_viewer_controls()
                self._set_semantic_only_controls_visible(False)
                self._on_viewer_control_changed()
            else:
                self._viewer.show_point_cloud(result.geometry_xyz, result.geometry_rgb)  # type: ignore[arg-type]  # geometry arrays are optional on a loaded run
        elif getattr(result, "from_scene_file", False) and result.final_cloud_index is not None:
            fb = result.frame_batch
            mr = result.mapping_result
            if fb is not None and mr is not None:
                self._viewer.load_scene_data_indexed(
                    fb,
                    mr,
                    result.final_cloud_index,
                    self._classes_config,  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass
                )
                _t2 = _time.monotonic()
                logger.info("[timing] load_scene_data_indexed: %.3fs", _t2 - _t1)
                self._build_legend()
                _t3 = _time.monotonic()
                logger.info("[timing] _build_legend: %.3fs", _t3 - _t2)
                self._show_viewer_controls()
                self._on_viewer_control_changed()
                _t4 = _time.monotonic()
                logger.info("[timing] _on_viewer_control_changed (initial apply_state): %.3fs", _t4 - _t3)
        else:
            cloud = result.reference_cloud
            fb = result.frame_batch
            mr = result.mapping_result
            if cloud is not None and fb is not None and mr is not None:
                self._viewer.load_scene_data(fb, mr, cloud, self._classes_config)  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass
                _t2 = _time.monotonic()
                logger.info("[timing] load_scene_data: %.3fs", _t2 - _t1)
                self._build_legend()
                _t3 = _time.monotonic()
                logger.info("[timing] _build_legend: %.3fs", _t3 - _t2)
                self._show_viewer_controls()
                self._on_viewer_control_changed()
                _t4 = _time.monotonic()
                logger.info("[timing] _on_viewer_control_changed (initial apply_state): %.3fs", _t4 - _t3)
            elif cloud is not None:
                self._viewer.show_point_cloud(cloud.xyz, cloud.rgb)

        _t5 = _time.monotonic()

        # For scene-file loads the reference_cloud is empty, so reconstruct it
        # from the FinalCloudIndex to let the ortho builder run.
        ortho_cloud = getattr(result, "reference_cloud", None)
        ortho_classes = getattr(result, "classes_config", self._classes_config)
        if (
            getattr(result, "from_scene_file", False)
            and result.final_cloud_index is not None
            and (ortho_cloud is None or len(ortho_cloud) == 0)
        ):
            from deepreefmap.pointcloud.final_cloud_index import reconstruct_cloud_from_index

            ortho_cloud = reconstruct_cloud_from_index(result.final_cloud_index)
            ortho_classes = result.classes_config

        # Build the live ortho preview BEFORE finalising.
        if result.mode != GEOMETRY_ONLY_MODE and ortho_cloud is not None and len(ortho_cloud) > 1:
            try:
                from deepreefmap.postproc.ortho_outputs import build_ortho_outputs

                def _ortho_load_progress(message: str) -> None:
                    phase = _STAGE_MESSAGE_TO_PHASE.get(message, "ortho_pca")
                    self._apply_progress(phase, message, 0, 0, flush=True)

                outputs = build_ortho_outputs(
                    ortho_cloud,
                    ortho_classes,
                    bins=_manifest_grid_bins(result.manifest),
                    crop=_manifest_transect_crop(result.manifest),
                    progress=_ortho_load_progress,
                )
                self._set_ortho_sources(ortho_cloud, outputs.grid, ortho_classes)
                self._cover_label.setText(self._format_cover_html(outputs.cover))
                self._cover_sunburst.set_cover(outputs.cover, ortho_classes)
            except Exception:
                logger.exception("Failed to build ortho preview for cached run")
            self._results_group.setVisible(True)
            self._results_empty.setVisible(False)

        _t6 = _time.monotonic()
        logger.info("[timing] ortho build: %.3fs", _t6 - _t5)

        self._apply_progress("viewer_finalise", "Finalising viewer", 1, 1)
        if not self._start_deferred_scene_file(run_dir, result):
            self._reset_progress()

        self._active_run_dir = run_dir
        self._active_run_manifest = result.manifest
        display = result.manifest.get("name") or run_dir.name
        warning = getattr(result, "world_points_warning", None)
        if warning:
            self._status_label.setText(f"⚠ Loaded '{display}': {warning}")
        else:
            self._status_label.setText(f"Loaded run '{display}' from {run_dir}")
        self._show_run_facts(result.manifest)

        ortho_path = run_dir / "ortho.png"
        if ortho_path.exists():
            self._show_results(str(run_dir))
        else:
            # No ortho (e.g. geometry-only run), so only the output dir is
            # tracked. Still reveal the legend if this run built one (a
            # semantic run without an ortho).
            self._results_output_dir = run_dir
            self._reveal_legend_overlay()

        # Make the run.log from this past run openable.
        log_path = run_dir / "run.log"
        self._log_view.set_current_log_path(log_path if log_path.exists() else None)
        self._set_app_mode("VIEWING")
        # Every open path funnels through here (the run table, a dropped folder,
        # --view, a finished batch), so View mode is entered once, at the point
        # the cloud is actually on screen rather than when it was asked for.
        self._enter_view_mode(run_dir)

    def _add_run_warning(self, message: str) -> None:
        if message in self._run_warnings:
            return
        self._run_warnings.append(message)
        html = "<b>Quality warnings:</b><br>" + "<br>".join(f"• {w}" for w in self._run_warnings)
        self._warnings_label.setText(html)
        self._warnings_label.setVisible(True)
        self._refresh_run_warnings_view()

    def _clear_run_warnings(self) -> None:
        self._run_warnings = []
        self._warnings_label.setText("")
        self._warnings_label.setVisible(False)
        self._refresh_run_warnings_view()
