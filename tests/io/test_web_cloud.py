import json
import struct
from pathlib import Path

import numpy as np
import pytest
from deepreefmap.pipeline.artifacts import SemanticPointCloud
from deepreefmap.pointcloud.final_cloud_index import build_final_cloud_index

from deepreefmap_gui.io.web_cloud import (
    WEB_CLOUD_FILENAME,
    read_web_cloud,
    write_web_cloud,
)


def write_from_cloud(path, cloud, frame_order, names=None, colours=None):
    index = build_final_cloud_index(cloud, list(frame_order), dict(colours or CLASS_COLOURS))
    write_web_cloud(
        path,
        index,
        frame_order,
        names or CLASS_NAMES,
        colours or CLASS_COLOURS,
        has_confidence=cloud.confidence is not None,
    )

CLASS_NAMES = {1: "coral", 3: "sand"}
CLASS_COLOURS = {1: (255, 0, 0), 3: (0, 0, 255)}


def _semantic_cloud(with_confidence: bool = True) -> SemanticPointCloud:
    rng = np.random.default_rng(7)
    n = 40
    return SemanticPointCloud(
        xyz=rng.normal(size=(n, 3)).astype(np.float32),
        rgb=rng.integers(0, 256, size=(n, 3), dtype=np.uint8),
        labels=rng.choice([1, 3], size=n).astype(np.int32),
        frame_indices=rng.choice([10, 20, 30], size=n).astype(np.int32),
        confidence=rng.random(n).astype(np.float32) if with_confidence else None,
    )


def test_round_trip_matches_final_cloud_index(tmp_path: Path) -> None:
    cloud = _semantic_cloud()
    frame_order = [10, 20, 30]
    path = tmp_path / WEB_CLOUD_FILENAME
    write_from_cloud(path, cloud, frame_order)

    header, views = read_web_cloud(path)
    index = build_final_cloud_index(cloud, frame_order, CLASS_COLOURS)

    assert header["frame_order"] == frame_order
    assert header["frame_count"] == 3
    assert header["has_confidence"] is True
    assert [c["id"] for c in header["classes"]] == list(index.class_ids)
    assert {c["id"]: c["name"] for c in header["classes"]} == CLASS_NAMES
    assert {c["id"]: tuple(c["colour"]) for c in header["classes"]} == CLASS_COLOURS

    total = 0
    for entry in header["per_class"]:
        cid = entry["class_id"]
        start, count = entry["point_offset"], entry["point_count"]
        assert start == total
        np.testing.assert_array_equal(views["xyz"][start : start + count], index.xyz_by_class[cid])
        np.testing.assert_array_equal(views["rgb"][start : start + count], index.rgb_by_class[cid])
        np.testing.assert_array_equal(views["conf"][start : start + count], index.conf_by_class[cid])
        assert entry["prefix_end"] == [int(x) for x in index.prefix_end_by_class[cid]]
        assert entry["prefix_end"][-1] == count
        total += count
    assert header["point_count"] == total == len(views["xyz"])


def test_buffers_are_four_byte_aligned(tmp_path: Path) -> None:
    path = tmp_path / WEB_CLOUD_FILENAME
    write_from_cloud(path, _semantic_cloud(), [10, 20, 30])

    raw = path.read_bytes()
    assert raw[:8] == b"DRMWEB01"
    (header_len,) = struct.unpack_from("<I", raw, 8)
    data_start = 12 + header_len
    assert data_start % 4 == 0
    header = json.loads(raw[12:data_start].decode("utf-8"))
    for buf in header["buffers"]:
        assert (data_start + buf["byte_offset"]) % 4 == 0


def test_header_byte_offsets_are_consistent(tmp_path: Path) -> None:
    path = tmp_path / WEB_CLOUD_FILENAME
    write_from_cloud(path, _semantic_cloud(), [10, 20, 30])

    raw = path.read_bytes()
    (header_len,) = struct.unpack_from("<I", raw, 8)
    data_start = 12 + header_len
    header = json.loads(raw[12:data_start].decode("utf-8"))

    item_sizes = {"float32": 4, "uint8": 1}
    widths = {"xyz": 3, "rgb": 3, "conf": 1}
    n = header["point_count"]
    end = 0
    for buf in header["buffers"]:
        assert buf["byte_offset"] >= end
        assert buf["byte_length"] == n * widths[buf["name"]] * item_sizes[buf["dtype"]]
        end = buf["byte_offset"] + buf["byte_length"]
    assert data_start + end == len(raw)


def test_confidence_buffer_omitted_when_absent(tmp_path: Path) -> None:
    path = tmp_path / WEB_CLOUD_FILENAME
    write_from_cloud(path, _semantic_cloud(with_confidence=False), [10, 20, 30])

    header, views = read_web_cloud(path)
    assert header["has_confidence"] is False
    assert "conf" not in views
    assert [b["name"] for b in header["buffers"]] == ["xyz", "rgb"]


def test_empty_cloud_round_trips(tmp_path: Path) -> None:
    path = tmp_path / WEB_CLOUD_FILENAME
    write_from_cloud(path, SemanticPointCloud.empty(), [0], names={}, colours={})

    header, views = read_web_cloud(path)
    assert header["point_count"] == 0
    assert header["classes"] == []
    assert views["xyz"].shape == (0, 3)


def test_bad_magic_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / WEB_CLOUD_FILENAME
    path.write_bytes(b"NOTDRMW0" + b"\x00" * 16)
    with pytest.raises(ValueError, match="bad magic"):
        read_web_cloud(path)


def test_backfill_from_a_saved_scene(tmp_path: Path) -> None:
    """A run reconstructed before the export existed can grow one from its
    scene file alone, with no second pass over the run."""
    from _factories import make_classes_config, make_scene

    from deepreefmap_gui.io.scene_file import save_scene_file
    from deepreefmap_gui.io.web_cloud import write_web_cloud_from_scene

    scene = make_scene(class_ids=(1, 5), points_by_class={1: 7, 5: 4})
    classes = make_classes_config((1, 5))
    scene_path = tmp_path / "scene_v9.drm"
    save_scene_file(
        scene_path,
        manifest={"name": "reef", "mode": "semantic"},
        classes_config=classes,
        mapping_result=scene.mapping,
        frame_batch=scene.frame_batch,
        final_cloud_index=scene.cloud_index,
    )
    out = tmp_path / WEB_CLOUD_FILENAME

    assert write_web_cloud_from_scene(scene_path, out, run_dir=tmp_path)

    header, views = read_web_cloud(out)
    assert header["frame_order"] == list(scene.cloud_index.frame_order)
    assert [c["id"] for c in header["classes"]] == list(scene.cloud_index.class_ids)
