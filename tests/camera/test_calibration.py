from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from deepreefmap_gui.camera.calibration import (
    CALIBRATION_STAGES,
    CalibrationCancelled,
    CalibrationError,
    calibrate_camera_profile,
    verify_camera_profile,
)

WIDTH, HEIGHT = 64, 48


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    rng = np.random.default_rng(0)
    frames = rng.integers(0, 255, size=(12, HEIGHT, WIDTH, 3), dtype=np.uint8)
    path = tmp_path / "clip.mp4"
    iio.imwrite(path, frames, fps=10, codec="libx264", macro_block_size=1)
    return path


class _Camera:
    model_name = "SIMPLE_RADIAL"
    params = [60.0, WIDTH / 2, HEIGHT / 2, 0.01]


class _Reconstruction:
    def __init__(self, registered: int) -> None:
        self.images = list(range(registered))
        self.cameras = {0: _Camera()}

    def compute_mean_reprojection_error(self) -> float:
        return 0.42

    def write(self, path: str) -> None:
        Path(path).joinpath("cameras.bin").write_bytes(b"")


@pytest.fixture
def fake_pycolmap(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """A pycolmap that registers every image, or as many as a test asks for."""
    calls: list[str] = []
    fake = types.SimpleNamespace(registered=None, calls=calls)

    class ImageReaderOptions:
        camera_model = "SIMPLE_RADIAL"

    def extract_features(**kwargs: object) -> None:
        calls.append("extract")

    def match_sequential(**kwargs: object) -> None:
        calls.append("match")

    def incremental_mapping(image_path: str, **kwargs: object) -> dict[int, _Reconstruction]:
        calls.append("map")
        n_images = len(list(Path(image_path).glob("*.png")))
        registered = n_images if fake.registered is None else fake.registered
        return {0: _Reconstruction(registered)}

    module = types.ModuleType("pycolmap")
    module.ImageReaderOptions = ImageReaderOptions  # type: ignore[attr-defined]
    module.CameraMode = types.SimpleNamespace(SINGLE=1)  # type: ignore[attr-defined]
    module.extract_features = extract_features  # type: ignore[attr-defined]
    module.match_sequential = match_sequential  # type: ignore[attr-defined]
    module.incremental_mapping = incremental_mapping  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pycolmap", module)
    return fake


def test_calibration_writes_the_profile_and_previews_to_the_output_dir(clip, tmp_path, fake_pycolmap):
    out = tmp_path / "profiles"
    seen: list[tuple[str, int, int]] = []

    saved = calibrate_camera_profile(
        clip, "bench", n_frames=12, fps=10, output_dir=out, progress_callback=lambda *a: seen.append(a)
    )

    assert saved == out / "bench.json"
    assert fake_pycolmap.calls == ["extract", "match", "map"]
    stages = [stage for stage, _, _ in seen]
    assert stages[0] == "sampling" and stages[-4:] == list(CALIBRATION_STAGES[1:])
    assert ("sampling", 12, 12) in seen
    report = verify_camera_profile("bench", out)
    assert report["image_size"] == (WIDTH, HEIGHT)
    assert report["diagnostics_dir_exists"] is True
    assert len(report["diagnostic_previews"]) == 4


def test_too_few_registered_images_is_a_calibration_error(clip, tmp_path, fake_pycolmap):
    fake_pycolmap.registered = 2
    with pytest.raises(CalibrationError, match="quality gate"):
        calibrate_camera_profile(clip, "weak", n_frames=12, fps=10, output_dir=tmp_path / "profiles")
    assert not (tmp_path / "profiles").exists()


def test_cancelling_between_stages_writes_nothing(clip, tmp_path, fake_pycolmap):
    def cancel_before_matching(stage: str, done: int, total: int) -> None:
        if stage == "matching":
            raise CalibrationCancelled

    with pytest.raises(CalibrationCancelled):
        calibrate_camera_profile(
            clip, "gone", n_frames=12, fps=10, output_dir=tmp_path / "profiles",
            progress_callback=cancel_before_matching,
        )
    assert fake_pycolmap.calls == ["extract"]
    assert not (tmp_path / "profiles").exists()
    assert not list(Path(tempfile.gettempdir()).glob("drm_calib_*"))


def test_an_empty_window_is_a_calibration_error(clip, tmp_path, fake_pycolmap):
    with pytest.raises(CalibrationError, match="timestamp range"):
        calibrate_camera_profile(clip, "empty", begin_s=5.0, end_s=1.0, output_dir=tmp_path / "profiles")
