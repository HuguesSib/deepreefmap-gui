"""COLMAP camera calibration with progress, cancellation and a chosen output dir.

The library's ``calibrate_camera_profile`` is one blocking call that writes to
``./camera_profiles`` and reports nothing while COLMAP runs. This is the same
pipeline (sample frames, extract, match, map, undistort, previews) with a
progress callback between stages and the output directory as a parameter. The
camera parsing is the library's.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import cv2
import imageio.v3 as iio
import numpy as np
from deepreefmap.camera.colmap_calibration import _camera_model_debug, _parse_colmap_camera
from deepreefmap.camera.intrinsics import CameraProfile
from deepreefmap.camera.rectification import Rectifier

from deepreefmap_gui.camera.profiles import load_profile_file, save_profile

# Reported to the progress callback in this order. Only sampling counts frames;
# the COLMAP stages block in C++ and report (stage, 0, 0).
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

    with tempfile.TemporaryDirectory(prefix="drm_calib_") as tmp:
        tmp_dir = Path(tmp)
        image_dir = tmp_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        sample_paths = _sample_video_frames(video, image_dir, n_frames, fps, begin_s, end_s, report)
        sample_img = cv2.imread(str(sample_paths[0]))
        if sample_img is None:
            raise CalibrationError(f"Failed to read sample frame: {sample_paths[0]}")
        h, w = sample_img.shape[:2]

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
        report("extracting")
        pycolmap.extract_features(**extract_kwargs)  # type: ignore[arg-type]
        report("matching")
        pycolmap.match_sequential(database_path=str(database_path))
        report("reconstructing")
        maps = pycolmap.incremental_mapping(
            database_path=str(database_path), image_path=str(image_dir), output_path=str(sparse_path)
        )
        if not maps:
            raise CalibrationError("COLMAP mapping failed: no reconstruction produced.")

        best_rec = max(maps.values(), key=lambda rec: len(rec.images))
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
