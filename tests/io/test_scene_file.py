"""The .scene.zarr.zip quick-load format.

This is the cache that lets `view-run` skip the 1-2 minute reference-cloud
rebuild, so a silent format regression costs a minute per load and, worse, a
silently wrong cloud. The tests drive real zarr writes and reads on tmp_path --
no mocking -- and check the two guards that decide whether a cached scene is
trusted at all: the schema range and the source fingerprint.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from _factories import make_classes_config, make_scene
from deepreefmap.config.classes import ClassConfig

from deepreefmap_gui.io.scene_file import (
    SCENE_FILE_SUFFIX,
    SCHEMA_VERSION,
    compute_source_fingerprint,
    find_scene_file,
    fingerprint_matches,
    load_scene_file,
    save_scene_file,
    scene_file_name,
)

H, W = 4, 6
FRAME_INDICES = (0, 2, 4)  # non-contiguous on purpose: indices are data, not positions
N_FRAMES = len(FRAME_INDICES)
CLASS_IDS = (1, 5)
POINTS_BY_CLASS = {1: 7, 5: 4}


@pytest.fixture
def classes_config() -> ClassConfig:
    return make_classes_config(CLASS_IDS)


@pytest.fixture
def scene():
    """A small but structurally complete scene: frames, mapping, cloud index."""
    return make_scene(
        frame_indices=FRAME_INDICES,
        size=(W, H),
        class_ids=CLASS_IDS,
        clip_counts=(2, 1),
        points_by_class=POINTS_BY_CLASS,
    )


@pytest.fixture
def run_dir(tmp_path) -> Path:
    """A run directory shaped like one the pipeline leaves behind."""
    d = tmp_path / "20260101-000000"
    (d / "frames").mkdir(parents=True)
    (d / "labels").mkdir()
    (d / "masks").mkdir()
    for i in range(N_FRAMES):
        for sub in ("frames", "labels", "masks"):
            (d / sub / f"{i:06d}.png").write_bytes(b"x" * (10 + i))
    (d / "run_manifest.json").write_text(json.dumps({"name": "reef north", "mode": "semantic"}))
    (d / "mapping_outputs.npz").write_bytes(b"npz" * 100)
    return d


def _save(path, scene, classes_config, manifest=None, run_dir=None, progress_cb=None):
    save_scene_file(
        path,
        manifest=manifest if manifest is not None else {"name": "reef north", "mode": "semantic"},
        classes_config=classes_config,
        mapping_result=scene.mapping,
        frame_batch=scene.frame_batch,
        final_cloud_index=scene.cloud_index,
        run_dir=run_dir,
        progress_cb=progress_cb,
    )


class _BrokenIndex:
    """A cloud index that passes the writer's early sections, then fails in _save_fci."""

    frame_order = FRAME_INDICES
    class_ids = (1,)
    xyz_by_class = {1: np.zeros((2, 3), dtype=np.float32)}

    def __getattr__(self, name):
        raise OSError(28, "No space left on device")


def _save_out_of_space(path, scene, classes_config):
    """A save that dies partway through the cloud index, as a full disk would."""
    with pytest.raises(OSError):
        save_scene_file(
            path,
            manifest={"name": "x"},
            classes_config=classes_config,
            mapping_result=scene.mapping,
            frame_batch=scene.frame_batch,
            final_cloud_index=_BrokenIndex(),
        )


# --- naming and discovery ---------------------------------------------------

@pytest.mark.parametrize(
    "manifest, dir_name, expected",
    [
        ({"name": "reef north"}, None, "reef_north"),
        ({"name": "  "}, "20260101-000000", "20260101-000000"),
        ({}, None, "scene"),
        ({"name": "///"}, None, "scene"),          # sanitised to nothing -> fallback
        ({"name": "a/b:c*d"}, None, "a_b_c_d"),    # path separators cannot survive
    ],
)
def test_scene_file_name_sanitises(manifest, dir_name, expected):
    run_dir = Path(dir_name) if dir_name else None
    assert scene_file_name(manifest, run_dir) == expected + SCENE_FILE_SUFFIX


def test_find_scene_file_prefers_current_suffix_over_legacy(tmp_path):
    (tmp_path / "old.drm.zarr.zip").write_bytes(b"")
    assert find_scene_file(tmp_path).name == "old.drm.zarr.zip"

    (tmp_path / ("new" + SCENE_FILE_SUFFIX)).write_bytes(b"")
    assert find_scene_file(tmp_path).name == "new" + SCENE_FILE_SUFFIX


def test_find_scene_file_returns_none_when_absent(tmp_path):
    assert find_scene_file(tmp_path) is None


# --- fingerprint ------------------------------------------------------------

def test_fingerprint_notices_every_kind_of_source_change(run_dir):
    base = compute_source_fingerprint(run_dir)
    assert fingerprint_matches(base, compute_source_fingerprint(run_dir))

    (run_dir / "run_manifest.json").write_text(json.dumps({"name": "renamed"}))
    assert not fingerprint_matches(base, compute_source_fingerprint(run_dir))

    (run_dir / "run_manifest.json").write_text(json.dumps({"name": "reef north", "mode": "semantic"}))
    (run_dir / "frames" / "999999.png").write_bytes(b"extra")
    assert not fingerprint_matches(base, compute_source_fingerprint(run_dir))


def test_fingerprint_survives_the_timings_being_folded_in(run_dir):
    """Scenario: the scene is written, then `apply_manifest_timings` rewrites the
    manifest to record how long writing it took.

    Expected behaviour: the scene is not stale. Those keys say nothing about the
    data it holds.
    """
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    base = compute_source_fingerprint(run_dir)

    timed = {
        **manifest,
        "stage_durations": {"scene_save": 0.5},
        "stage_peaks": {"scene_save": 1024},
        "run_duration_s": 186.2,
        "system_profile": {"gpu": "none"},
        "performance_observation": {"duration_s": 186.2, "frames": 100},
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(timed))
    assert fingerprint_matches(base, compute_source_fingerprint(run_dir))

    # A fact about the run itself still invalidates.
    (run_dir / "run_manifest.json").write_text(json.dumps({**timed, "mode": "geometry_only"}))
    assert not fingerprint_matches(base, compute_source_fingerprint(run_dir))


def test_a_scene_stored_before_the_content_hash_falls_back_to_the_byte_hash(run_dir):
    """A scene storing only `manifest_sha256` is matched on it: nothing rewrote
    that manifest after the write, so the bytes are still the right question."""
    current = compute_source_fingerprint(run_dir)
    legacy = {k: v for k, v in current.items() if k != "manifest_data_sha256"}

    assert fingerprint_matches(legacy, current)

    (run_dir / "run_manifest.json").write_text(json.dumps({"name": "renamed"}))
    assert not fingerprint_matches(legacy, compute_source_fingerprint(run_dir))


def test_fingerprint_ignores_mapping_mtime(run_dir):
    """mtime is recorded but deliberately not compared: a touch is not a change."""
    base = compute_source_fingerprint(run_dir)
    stale = {**base, "mapping_npz_mtime": base["mapping_npz_mtime"] + 1000.0}
    assert fingerprint_matches(stale, base)


def test_fingerprint_of_an_empty_dir_is_stable(tmp_path):
    fp = compute_source_fingerprint(tmp_path)
    assert fp["manifest_sha256"] == ""
    assert fp["frame_count"] == 0


# --- round trip -------------------------------------------------------------

def test_round_trip_preserves_every_section(tmp_path, scene, classes_config):
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    loaded = load_scene_file(path)
    assert loaded is not None

    assert loaded.manifest["name"] == "reef north"
    assert loaded.run_mode == "semantic"

    assert [c.id for c in loaded.classes_config.classes] == [1, 5]
    assert [c.name for c in loaded.classes_config.classes] == ["reef", "sand"]
    assert loaded.classes_config.id_to_color[1] == (10, 20, 30)

    # Frame metadata, not frame pixels: enough to reopen the run's PNG caches.
    np.testing.assert_array_equal(loaded.frame_indices, FRAME_INDICES)
    assert loaded.clip_counts == (2, 1)
    assert loaded.image_size == (W, H)
    assert loaded.schema_version == SCHEMA_VERSION

    fci = scene.cloud_index
    out = loaded.final_cloud_index
    assert out.frame_order == fci.frame_order
    assert tuple(out.class_ids) == CLASS_IDS
    for cid in CLASS_IDS:
        # The CSR split must hand back exactly the per-class arrays that went in.
        np.testing.assert_allclose(out.xyz_by_class[cid], fci.xyz_by_class[cid])
        np.testing.assert_array_equal(out.rgb_by_class[cid], fci.rgb_by_class[cid])
        np.testing.assert_array_equal(out.semrgb_by_class[cid], fci.semrgb_by_class[cid])
        np.testing.assert_allclose(out.conf_by_class[cid], fci.conf_by_class[cid])
        np.testing.assert_array_equal(out.prefix_end_by_class[cid], fci.prefix_end_by_class[cid])



def test_no_pixels_or_mapping_arrays_are_written(tmp_path, scene, classes_config):
    """The whole point of the format: those two were 99% of the file.

    Both already sit in the run directory in a form that is smaller (PNG beats
    Blosc on frames by ~1.5x) and no slower to read, so caching them cost 46% of
    a run directory for nothing. Asserted on the archive's own key list, because
    a reinstated copy would otherwise only show up as a mysteriously large file.
    """
    import zipfile

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    keys = zipfile.ZipFile(path).namelist()
    assert not [k for k in keys if k.startswith("mapping/")]
    assert not [k for k in keys if k.startswith("frames/")]
    assert [k for k in keys if k.startswith("final_cloud_index/")]


def test_the_file_does_not_grow_with_the_frames(tmp_path, scene, classes_config):
    """Scale-free version of the guard above: 16x the pixels, same file.

    The real runs this came from wrote 1968 MB of frame images into a 3471 MB
    scene file, so a reinstated copy is only visible at full size. Comparing two
    resolutions catches it on a toy scene, where a byte-count ceiling cannot.
    """
    big = make_scene(
        frame_indices=FRAME_INDICES,
        size=(W * 4, H * 4),
        class_ids=CLASS_IDS,
        clip_counts=scene.frame_batch.clip_counts,
        points_by_class=POINTS_BY_CLASS,
        seed=1,
    )

    small_path = tmp_path / ("small" + SCENE_FILE_SUFFIX)
    big_path = tmp_path / ("big" + SCENE_FILE_SUFFIX)
    _save(small_path, scene, classes_config)
    _save(big_path, big, classes_config)

    # Only the image-size attrs differ, a couple of bytes of JSON.
    assert abs(big_path.stat().st_size - small_path.stat().st_size) < 200


def test_progress_is_reported_for_each_stage(tmp_path, scene, classes_config):
    seen: list[str] = []
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, progress_cb=lambda stage, c, t: seen.append(stage))

    assert {"scene_meta", "scene_fci", "scene_done"} <= set(seen)
    assert seen[-1] == "scene_done"

    loaded_stages: list[str] = []
    load_scene_file(path, progress_cb=lambda s, c, t: loaded_stages.append(s))
    assert {"scene_open", "scene_classes", "scene_cloud_index"} <= set(loaded_stages)


def test_a_failing_progress_callback_does_not_abort_the_save(tmp_path, scene, classes_config):
    """The callback drives a progress bar; a UI error must not lose the file."""
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    def boom(stage, cur, tot):
        raise RuntimeError("the viewer went away")

    _save(path, scene, classes_config, progress_cb=boom)
    assert path.exists()
    load_scene_file(path)


# --- staleness guards -------------------------------------------------------

def test_load_rejects_a_scene_whose_run_changed(tmp_path, scene, classes_config, run_dir):
    path = run_dir / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, run_dir=run_dir)

    loaded = load_scene_file(path, run_dir=run_dir)
    assert loaded is not None

    # Reprocessing the run at a different fps changes the frame count.
    (run_dir / "frames" / "999999.png").write_bytes(b"a new frame")
    assert load_scene_file(path, run_dir=run_dir) is None


def test_load_without_a_run_dir_skips_the_fingerprint_check(tmp_path, scene, classes_config, run_dir):
    path = run_dir / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config, run_dir=run_dir)
    (run_dir / "frames" / "999999.png").write_bytes(b"a new frame")

    loaded = load_scene_file(path)
    assert loaded is not None


@pytest.mark.parametrize("version", [0, 99])
def test_load_rejects_schemas_outside_the_supported_range(tmp_path, scene, classes_config, version):
    import zarr

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)

    # Rewrite the version in place, as a future (or ancient) build would have left it.
    store = zarr.ZipStore(str(path), mode="a")
    zarr.open_group(store=store, mode="a").attrs["schema_version"] = version
    store.close()

    assert load_scene_file(path) is None


def test_a_failed_save_leaves_no_partial_file(tmp_path, scene, classes_config):
    """A half-written scene that still loads would be worse than none at all."""
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    _save_out_of_space(path, scene, classes_config)

    assert not path.exists()
    assert not (path.parent / (path.name + ".tmp")).exists()


# --- the archive handle -------------------------------------------------
#
# Every path must close the store, success included: since the frame pixels left
# the file there is nothing to read lazily, so a load materialises everything and
# hands back no handle. A leak here is silent: runs/loaded_run.py swallows the
# exception and regenerates, and on Windows the held handle locks the file so the
# regeneration fails too -- which is why these assert on the archive itself rather
# than on descriptor counts, where POSIX happily unlinks a file that is still open.


@pytest.fixture
def opened_stores(monkeypatch):
    """Every ZipStore scene_file opens, in order, so a test can check it closed."""
    import zarr

    stores = []
    real = zarr.ZipStore

    def spy(*args, **kwargs):
        store = real(*args, **kwargs)
        stores.append(store)
        return store

    monkeypatch.setattr(zarr, "ZipStore", spy)
    return stores


def _is_closed(store) -> bool:
    return store.zf.fp is None


def test_a_load_that_fails_partway_closes_the_archive(
    tmp_path, scene, classes_config, monkeypatch, opened_stores
):
    import deepreefmap_gui.io.scene_file as scene_file

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)
    opened_stores.clear()
    monkeypatch.setattr(
        scene_file, "_load_fci", lambda _root: (_ for _ in ()).throw(ValueError("corrupt"))
    )

    with pytest.raises(ValueError, match="corrupt"):
        load_scene_file(path)

    assert len(opened_stores) == 1
    assert _is_closed(opened_stores[0])


def test_a_rejected_scene_closes_the_archive(tmp_path, scene, classes_config, opened_stores):
    """The early returns were already right; pin them so the try/except wrapped
    around them cannot quietly change the answer."""
    import zarr

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)
    store = zarr.ZipStore(str(path), mode="a")
    zarr.open_group(store=store, mode="a").attrs["schema_version"] = 99
    store.close()
    opened_stores.clear()

    assert load_scene_file(path) is None
    assert len(opened_stores) == 1
    assert _is_closed(opened_stores[0])


def test_a_successful_load_closes_the_archive_too(
    tmp_path, scene, classes_config, opened_stores
):
    """Nothing is read lazily out of a scene file any more, so the success path
    owns the handle like every other path and must not leave the file locked."""
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    _save(path, scene, classes_config)
    opened_stores.clear()

    loaded = load_scene_file(path)

    assert loaded is not None
    assert loaded.final_cloud_index is not None
    assert _is_closed(opened_stores[0])


def test_a_failed_save_closes_the_archive_before_removing_the_file(
    tmp_path, scene, classes_config, opened_stores
):
    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    _save_out_of_space(path, scene, classes_config)

    assert len(opened_stores) == 1
    assert _is_closed(opened_stores[0])


# --- telling a live write from debris -----------------------------------
#
# load_run sweeps leftover .tmp files, and reaches that sweep exactly when the
# scene file is missing or unusable -- the state a run is in while its first
# scene file is being written on a background thread.


def test_a_tmp_this_process_is_writing_is_reported_in_progress(tmp_path, scene, classes_config):
    from deepreefmap_gui.io.scene_file import tmp_write_in_progress

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)
    tmp = path.parent / (path.name + ".tmp")
    seen: list[bool] = []

    def spy(*_args):
        seen.append(tmp_write_in_progress(tmp))

    _save(path, scene, classes_config, progress_cb=spy)

    assert seen and all(seen), "the write was not registered while it ran"
    assert not tmp_write_in_progress(tmp), "the registration outlived the write"


def test_a_failed_write_stops_being_reported_in_progress(tmp_path, scene, classes_config):
    """Otherwise one crashed write pins the path for the life of the process."""
    from deepreefmap_gui.io.scene_file import tmp_write_in_progress

    path = tmp_path / ("s" + SCENE_FILE_SUFFIX)

    _save_out_of_space(path, scene, classes_config)

    assert not tmp_write_in_progress(path.parent / (path.name + ".tmp"))


def test_a_recently_touched_tmp_is_left_for_the_process_writing_it(tmp_path):
    """Another GUI instance on the same run dir is not in this process's registry."""
    from deepreefmap_gui.io.scene_file import tmp_write_in_progress

    tmp = tmp_path / ("s" + SCENE_FILE_SUFFIX + ".tmp")
    tmp.write_bytes(b"partial")

    assert tmp_write_in_progress(tmp)


def test_an_old_tmp_is_debris(tmp_path):
    import os as _os

    from deepreefmap_gui.io.scene_file import _TMP_ABANDONED_AFTER_S, tmp_write_in_progress

    tmp = tmp_path / ("s" + SCENE_FILE_SUFFIX + ".tmp")
    tmp.write_bytes(b"partial")
    stale = time.time() - _TMP_ABANDONED_AFTER_S - 60
    _os.utime(tmp, (stale, stale))

    assert not tmp_write_in_progress(tmp)


def test_a_missing_tmp_is_not_in_progress(tmp_path):
    from deepreefmap_gui.io.scene_file import tmp_write_in_progress

    assert not tmp_write_in_progress(tmp_path / "gone.tmp")
