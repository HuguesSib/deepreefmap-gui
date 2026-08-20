"""Browser-ready binary export of the final reference cloud ('drmw' format).

One self-contained file the web console renders with fetch, TypedArray views
and three.js. It lives here rather than in the library deliberately: the
library's review cycle is long and its other users have no browser, while this
file only exists so the web console can draw what the desktop viewer draws.

The layout mirrors `FinalCloudIndex`: per-class points sorted by timeline rank
plus per-class prefix-end tables, so a timeline scrub is a draw-range change
and a class toggle is a draw-call toggle. The index the scene writer already
built is reused, so the export costs serialisation and no second pass.

Layout, all little-endian:

- 8-byte magic ``DRMWEB01``
- uint32 length of the JSON header in bytes
- UTF-8 JSON header, space-padded so the binary section starts on a 4-byte
  boundary
- the buffers back to back, each starting on a 4-byte boundary

Header ``buffers[*].byte_offset`` is relative to the end of the header, so the
header's own length never feeds back into the offsets it declares. Because the
binary section starts 4-byte aligned, every offset is also 4-byte aligned in
the file, which is what ``Float32Array`` views over a fetched ``ArrayBuffer``
require.

Semantic colours are not shipped: the viewer recolours from the ``classes``
table in the header.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

WEB_CLOUD_FILENAME = "cloud_web.drmw"

_MAGIC = b"DRMWEB01"
_FORMAT = "drmw"
_VERSION = 1

_DTYPES: dict[str, np.dtype[Any]] = {
    "float32": np.dtype("<f4"),
    "uint8": np.dtype("<u1"),
}


def _padding(n: int) -> int:
    return (-n) % 4


def write_web_cloud(
    path: Path,
    index: "FinalCloudIndex",
    frame_order: Sequence[int],
    class_names: Mapping[int, str],
    class_colours: Mapping[int, tuple[int, int, int]],
    *,
    has_confidence: bool,
) -> None:
    """Write an already-built index as a 'drmw' file.

    `frame_order` lists source frame indices in timeline order, the same list
    the index was built over. What the file holds is exactly what the desktop
    viewer draws, median-distance cap included, because it is the same index.
    """

    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    conf_parts: list[np.ndarray] = []
    per_class: list[dict[str, object]] = []
    classes: list[dict[str, object]] = []
    point_offset = 0
    for cid in index.class_ids:
        n = int(index.xyz_by_class[cid].shape[0])
        xyz_parts.append(index.xyz_by_class[cid])
        rgb_parts.append(index.rgb_by_class[cid])
        conf_parts.append(index.conf_by_class[cid])
        classes.append(
            {
                "id": cid,
                "name": class_names.get(cid, f"class_{cid}"),
                "colour": [int(c) for c in class_colours.get(cid, (128, 128, 128))],
            }
        )
        per_class.append(
            {
                "class_id": cid,
                "point_offset": point_offset,
                "point_count": n,
                "prefix_end": [int(x) for x in index.prefix_end_by_class[cid]],
            }
        )
        point_offset += n

    def _concat(parts: list[np.ndarray], empty_shape: tuple[int, ...], dtype: str) -> np.ndarray:
        stacked = np.concatenate(parts) if parts else np.zeros(empty_shape, dtype=_DTYPES[dtype])
        return np.ascontiguousarray(stacked, dtype=_DTYPES[dtype])

    arrays = [
        ("xyz", "float32", _concat(xyz_parts, (0, 3), "float32")),
        ("rgb", "uint8", _concat(rgb_parts, (0, 3), "uint8")),
    ]
    if has_confidence:
        arrays.append(("conf", "float32", _concat(conf_parts, (0,), "float32")))

    payload = bytearray()
    buffer_entries: list[dict[str, object]] = []
    for name, dtype_name, arr in arrays:
        payload += b"\x00" * _padding(len(payload))
        data = arr.tobytes(order="C")
        buffer_entries.append(
            {
                "name": name,
                "dtype": dtype_name,
                "byte_offset": len(payload),
                "byte_length": len(data),
            }
        )
        payload += data

    header = {
        "format": _FORMAT,
        "version": _VERSION,
        "point_count": point_offset,
        "frame_count": len(frame_order),
        "frame_order": [int(x) for x in frame_order],
        "has_confidence": has_confidence,
        "classes": classes,
        "per_class": per_class,
        "buffers": buffer_entries,
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    header_bytes += b" " * _padding(len(_MAGIC) + 4 + len(header_bytes))

    with open(path, "wb") as fh:
        fh.write(_MAGIC)
        fh.write(struct.pack("<I", len(header_bytes)))
        fh.write(header_bytes)
        fh.write(payload)


def write_web_cloud_from_scene(scene_path: Path, out_path: Path, *, run_dir: Path) -> bool:
    """Backfill the browser export from a run's scene file.

    The writer normally runs inside `write_scene_file`, so a run reconstructed
    before the export existed has a scene and no drmw: archived, it is blind in
    the web console's viewer. The scene holds the same index the writer would
    have been handed, so this rebuilds the export without re-reading the run.
    Returns False when the scene cannot be loaded (stale fingerprint included).
    """
    from deepreefmap_gui.io.scene_file import load_scene_file

    scene = load_scene_file(scene_path, run_dir=run_dir)
    if scene is None:
        return False
    fci = scene.final_cloud_index
    # The scene does not say whether the mapper produced confidence; an index
    # built without it carries constant fill, so the header flag is read off
    # the arrays it describes.
    has_confidence = any(
        arr.size > 1 and float(arr.min()) != float(arr.max())
        for arr in fci.conf_by_class.values()
    )
    write_web_cloud(
        out_path,
        fci,
        fci.frame_order,
        scene.classes_config.id_to_name,
        scene.classes_config.id_to_color,
        has_confidence=has_confidence,
    )
    return True


def read_web_cloud(path: Path) -> tuple[dict[str, object], dict[str, np.ndarray]]:
    """Read a 'drmw' file back into its header dict and named numpy views.

    Views are zero-copy over the file bytes: `xyz` and `rgb` come back Nx3,
    `conf` 1-D. Mirrors what a browser does with TypedArray views, so tests
    exercise the exact contract the console relies on.
    """
    raw = Path(path).read_bytes()
    if raw[: len(_MAGIC)] != _MAGIC:
        raise ValueError(f"{path} is not a drmw file (bad magic)")
    (header_len,) = struct.unpack_from("<I", raw, len(_MAGIC))
    data_start = len(_MAGIC) + 4 + header_len
    header = json.loads(raw[len(_MAGIC) + 4 : data_start].decode("utf-8"))
    if header.get("format") != _FORMAT or int(header.get("version", -1)) != _VERSION:
        raise ValueError(f"{path}: unsupported drmw header {header.get('format')!r} v{header.get('version')!r}")

    views: dict[str, np.ndarray] = {}
    for buf in header["buffers"]:
        dtype = _DTYPES[str(buf["dtype"])]
        count = int(buf["byte_length"]) // dtype.itemsize
        arr = np.frombuffer(raw, dtype=dtype, count=count, offset=data_start + int(buf["byte_offset"]))
        name = str(buf["name"])
        views[name] = arr.reshape(-1, 3) if name in ("xyz", "rgb") else arr
    return header, views
