"""COLMAP camera calibration with progress, cancellation and a chosen output dir.

The library's ``calibrate_camera_profile`` is one blocking call that writes to
``./camera_profiles`` and reports nothing while COLMAP runs. This is the same
pipeline (sample frames, extract, match, map, undistort, previews) with a
progress callback between stages and the output directory as a parameter. The
camera parsing is the library's.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import imageio.v3 as iio
import numpy as np
from deepreefmap.camera.colmap_calibration import _camera_model_debug, _parse_colmap_camera
from deepreefmap.camera.intrinsics import CameraProfile
from deepreefmap.camera.rectification import Rectifier

from deepreefmap_gui.camera import colmap_output
from deepreefmap_gui.camera.profiles import load_profile_file, save_profile

logger = logging.getLogger(__name__)

# Reported to the progress callback in this order. Sampling counts the frames it
# writes; the three COLMAP stages count what COLMAP itself says it has done,
# read off its console output.
CALIBRATION_STAGES = ("sampling", "extracting", "matching", "reconstructing", "diagnostics")

ProgressCallback = Callable[[str, int, int], None]


class CalibrationError(RuntimeError):
    """Calibration ran but produced no usable profile."""


class CalibrationCancelled(Exception):  # noqa: N818
    """Raised from a progress callback to stop between stages."""


def _sample_video_frames(
    video_path: Path,
    out_dir: Path,
    n_frames: int,
    fps: int,
    begin_s: float | None,
    end_s: float | None,
    report: ProgressCallback,
) -> list[Path]:
    meta = iio.immeta(video_path)
    src_fps = float(meta.get("fps", fps))
    duration = float(meta.get("duration", 0.0) or 0.0)
    start_t = max(0.0, begin_s if begin_s is not None else 0.0)
    end_t = end_s if end_s is not None else duration
    if duration > 0.0:
        end_t = min(end_t, duration)
    if end_t <= start_t:
        raise CalibrationError(f"Invalid calibration timestamp range: begin={start_t}, end={end_t}")

    start_idx = int(round(start_t * src_fps))
    end_idx = int(round(end_t * src_fps)) if duration > 0.0 or end_s is not None else None
    stride = max(1, int(round(src_fps / max(1, fps))))
    selected: list[np.ndarray] = []
    for idx, frame in enumerate(iio.imiter(video_path)):
        if idx < start_idx:
            continue
        if end_idx is not None and idx > end_idx:
            break
        if idx % stride != 0:
            continue
        selected.append(frame)
        report("sampling", len(selected), n_frames)
        if len(selected) >= n_frames:
            break
    if not selected:
        raise CalibrationError("No frames found in video at requested sampling rate.")
    out_paths: list[Path] = []
    for i, frame in enumerate(selected):
        p = out_dir / f"{i:06d}.png"
        cv2.imwrite(str(p), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        out_paths.append(p)
    return out_paths


def calibrate_camera_profile(
    video: Path,
    name: str,
    *,
    output_dir: Path,
    n_frames: int = 100,
    fps: int = 10,
    begin_s: float | None = None,
    end_s: float | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Calibrate from a clip and write ``<output_dir>/<name>.json``.

    A ``<name>_diagnostics`` folder beside the profile holds the sparse model and
    raw|rectified previews. ``progress_callback(stage, done, total)`` is called
    through ``CALIBRATION_STAGES``; raising ``CalibrationCancelled`` from it stops
    the run with nothing written.
    """

    def report(stage: str, done: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback(stage, done, total)

    # The log is the record of a calibration: what was asked for, what COLMAP was
    # given, and what came back. It is what somebody reads when a profile looks
    # wrong weeks later, and what they quote when asking why it failed.
    logger.info("Calibrating %r from %s", name, video)
    logger.info(
        "Window %s to %s, sampling %d fps, up to %d frames",
        f"{begin_s:.1f} s" if begin_s is not None else "start",
        f"{end_s:.1f} s" if end_s is not None else "end of clip",
        fps,
        n_frames,
    )
    logger.info(
        "The same calibration outside the app: deepreefmap calibrate %s --name %s "
        "--n-frames %d --fps %d%s%s",
        video,
        name,
        n_frames,
        fps,
        f" --begin {begin_s:g}" if begin_s is not None else "",
        f" --end {end_s:g}" if end_s is not None else "",
    )
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="drm_calib_") as tmp:
        tmp_dir = Path(tmp)
        image_dir = tmp_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        sample_paths = _sample_video_frames(video, image_dir, n_frames, fps, begin_s, end_s, report)
        sample_img = cv2.imread(str(sample_paths[0]))
        if sample_img is None:
            raise CalibrationError(f"Failed to read sample frame: {sample_paths[0]}")
        h, w = sample_img.shape[:2]
        logger.info("Sampled %d frames at %dx%d in %.1f s", len(sample_paths), w, h, time.monotonic() - started)

        try:
            import pycolmap
        except Exception as exc:
            raise CalibrationError("Calibration requires pycolmap to be installed and importable.") from exc

        database_path = tmp_dir / "database.db"
        sparse_path = tmp_dir / "sparse"
        sparse_path.mkdir(parents=True, exist_ok=True)

        reader_options = pycolmap.ImageReaderOptions()
        if hasattr(reader_options, "camera_model"):
            reader_options.camera_model = "RADIAL"
        # pycolmap API differs by version: camera_mode on the call, or
        # single_camera on the reader options.
        extract_kwargs: dict[str, object] = {
            "database_path": str(database_path),
            "image_path": str(image_dir),
            "reader_options": reader_options,
        }
        if hasattr(pycolmap, "CameraMode"):
            extract_kwargs["camera_mode"] = pycolmap.CameraMode.SINGLE
        elif hasattr(reader_options, "single_camera"):
            reader_options.single_camera = True
        logger.info(
            "pycolmap %s, one RADIAL camera over %s",
            getattr(pycolmap, "__version__", "version unknown"),
            image_dir,
        )

        # COLMAP reports how far it is through each stage on its own stderr, and
        # nowhere else. Read there, the two long stages get a real count instead
        # of a bar that only spins, and a reconstruction thrown away says so while
        # it is happening rather than as a failure at the end.
        stage = ""
        watched = len(sample_paths)

        def on_colmap_line(line: str) -> None:
            # Never raises: this runs on the thread draining COLMAP's pipe, and a
            # pipe that stops being read blocks COLMAP itself. Cancellation is a
            # question the calling thread asks between stages.
            try:
                _handle_colmap_line(line)
            except Exception:
                logger.debug("Could not read a COLMAP line", exc_info=True)

        def _handle_colmap_line(line: str) -> None:
            event = colmap_output.parse_line(line)
            if event is None:
                logger.debug("%s", line)
                return
            if event.kind == colmap_output.WARNING:
                logger.warning("COLMAP: %s", event.text)
                return
            if event.kind == colmap_output.EXTRACTED and stage == "extracting":
                report("extracting", event.done, event.total)
            elif event.kind == colmap_output.MATCHED and stage == "matching":
                report("matching", event.done, event.total)
            elif event.kind == colmap_output.REGISTERED:
                report("reconstructing", event.done, watched)
            elif event.kind == colmap_output.DISCARDED:
                logger.info("COLMAP discarded a reconstruction: %s", event.text)
            elif event.kind == colmap_output.KEPT:
                logger.info("COLMAP kept a reconstruction")
        # No subprocess and no CLI: pycolmap runs COLMAP's C++ in this process, so
        # these three calls are the whole of what COLMAP is asked to do. Its own
        # console output goes to the app's stdout, which the log window captures.
        with colmap_output.capture_stderr(on_colmap_line):
            stage = "extracting"
            report("extracting", 0, watched)
            stage_at = time.monotonic()
            logger.info("extract_features(database=%s, images=%s)", database_path, image_dir)
            pycolmap.extract_features(**extract_kwargs)  # type: ignore[arg-type]
            logger.info("Features extracted in %.1f s", time.monotonic() - stage_at)

            stage = "matching"
            report("matching", 0, watched)
            stage_at = time.monotonic()
            logger.info("match_sequential(database=%s)", database_path)
            pycolmap.match_sequential(database_path=str(database_path))
            logger.info("Frames matched in %.1f s", time.monotonic() - stage_at)

            stage = "reconstructing"
            report("reconstructing", 0, watched)
            stage_at = time.monotonic()
            logger.info(
                "incremental_mapping(database=%s, images=%s, out=%s)", database_path, image_dir, sparse_path
            )
            maps = pycolmap.incremental_mapping(
                database_path=str(database_path), image_path=str(image_dir), output_path=str(sparse_path)
            )
        if not maps:
            raise CalibrationError("COLMAP mapping failed: no reconstruction produced.")
        logger.info("Reconstructed %d model(s) in %.1f s", len(maps), time.monotonic() - stage_at)

        best_rec = max(maps.values(), key=lambda rec: len(rec.images))
        logger.info("Registered %d of %d frames", len(best_rec.images), len(sample_paths))
        if len(best_rec.images) < max(10, len(sample_paths) // 3):
            raise CalibrationError(
                f"Calibration failed quality gate: only {len(best_rec.images)} registered images "
                f"out of {len(sample_paths)}."
            )
        report("diagnostics")
        cam = next(iter(best_rec.cameras.values()))
        try:
            model_name, distorted, k_dist, dist = _parse_colmap_camera(cam)
        except Exception as exc:
            raise CalibrationError(f"Failed to parse COLMAP camera: {_camera_model_debug(cam)}") from exc

        if model_name in {"RADIAL", "SIMPLE_RADIAL"}:
            k_rect, roi = cv2.getOptimalNewCameraMatrix(
                k_dist, dist, (w, h), alpha=0.0, newImgSize=(w, h), centerPrincipalPoint=True
            )
            k_rect = np.asarray(k_rect, dtype=np.float32)
        elif model_name == "OPENCV_FISHEYE":
            k_rect = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                k_dist, dist.reshape(4, 1), (w, h), R=np.eye(3, dtype=np.float32), balance=0.0, new_size=(w, h)
            ).astype(np.float32)
            roi = (0, 0, w, h)
        else:
            k_rect = k_dist.astype(np.float32)
            roi = (0, 0, w, h)

        mean_reproj = None
        if hasattr(best_rec, "compute_mean_reprojection_error"):
            try:
                mean_reproj = float(best_rec.compute_mean_reprojection_error())
            except Exception:
                mean_reproj = None

        diagnostics = {
            "n_input_frames": len(sample_paths),
            "n_registered_images": len(best_rec.images),
            "mean_reprojection_error_px": mean_reproj,
            "camera_model": model_name,
            "rectification_mode": "none" if model_name in {"PINHOLE", "SIMPLE_PINHOLE"} else "undistort",
            "source_video": video.name,
            "sampling_fps": fps,
            "begin_s": begin_s,
            "end_s": end_s,
            "valid_roi_xywh": [int(v) for v in roi],
        }
        profile = CameraProfile(
            name=name,
            image_size=(w, h),
            k=k_rect,
            distorted_model=model_name,
            radial=distorted,
            diagnostics=diagnostics,
        )
        saved_path = save_profile(profile, output_dir)
        logger.info(
            "Camera %s, mean reprojection error %s, rectified focal length %.1f px",
            model_name,
            f"{mean_reproj:.2f} px" if mean_reproj is not None else "not reported",
            float(k_rect[0][0]),
        )
        logger.info("Wrote %s in %.1f s", saved_path, time.monotonic() - started)
        rectifier = Rectifier(profile)

        diagnostics_dir = output_dir / f"{name}_diagnostics"
        sparse_diag_dir = diagnostics_dir / "sparse"
        sparse_diag_dir.mkdir(parents=True, exist_ok=True)
        try:
            best_rec.write(str(sparse_diag_dir))
        except Exception:
            pass
        for p in sample_paths[:4]:
            shutil.copy2(p, diagnostics_dir / f"raw_{p.name}")
            raw_bgr = cv2.imread(str(p))
            if raw_bgr is None:
                continue
            raw_rgb = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2RGB)
            rect_rgb = rectifier.rectify(raw_rgb)
            side_by_side = np.concatenate([raw_rgb, rect_rgb], axis=1)
            cv2.imwrite(str(diagnostics_dir / f"compare_{p.name}"), cv2.cvtColor(side_by_side, cv2.COLOR_RGB2BGR))
        return saved_path


def verify_camera_profile(name: str, output_dir: Path) -> dict[str, object]:
    """Reload a profile from ``output_dir`` and list the previews beside it."""
    profile = load_profile_file(output_dir / f"{name}.json")
    diagnostics_dir = output_dir / f"{name}_diagnostics"
    previews = sorted(str(p) for p in diagnostics_dir.glob("*.png")) if diagnostics_dir.exists() else []
    return {
        "profile": name,
        "image_size": profile.image_size,
        "k": profile.k.tolist(),
        "radial": profile.radial,
        "diagnostics": profile.diagnostics,
        "diagnostic_previews": previews[:4],
        "diagnostics_dir_exists": diagnostics_dir.exists(),
    }
