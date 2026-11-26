"""GoPro gravity read straight from the MP4, on every platform.

deepreefmap reads the ``GRAV`` stream through ``py_gpmf_parser``, which only
publishes Linux x86_64 wheels, so on macOS and Windows every run went without
gravity alignment (and a run that requires it failed). The stream is simple to
read: the GoPro metadata track (``gpmd``) holds one GPMF payload per second or
so, each with a ``GRAV`` array of int16 x/y/z and a ``SCAL`` divisor.

:func:`read_gravity_stream` returns what ``py_gpmf_parser``'s
``extract_data("GRAV")`` returns: scaled vectors in file order and their times
in seconds, the samples of each payload spread evenly over that payload's
duration. :func:`install` puts it behind deepreefmap's reader when the parser is
missing, so the pipeline needs no change. Qt-free.
"""

from __future__ import annotations

import importlib.util
import logging
import struct
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from deepreefmap_gui.survey.video_probe import _Atoms, _handler, _read, _sample_format

logger = logging.getLogger(__name__)

_GRAV = b"GRAV"
_SCAL = b"SCAL"
_STRM = b"STRM"
# int16 is what every GoPro since the HERO6 writes GRAV as; the others are here
# so a firmware that changes the type is read, not misread.
_TYPES = {"s": ">i2", "S": ">u2", "l": ">i4", "L": ">u4", "f": ">f4", "d": ">f8", "b": ">i1", "B": ">u1"}
_MAX_PAYLOAD = 1 << 20


def read_gravity_stream(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """``(Nx3 gravity, N times in s)`` from a GoPro clip, or None when it has none."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return _read_stream(handle, size)
    except (OSError, ValueError, struct.error) as exc:
        logger.debug("Could not read GRAV from %s: %s", path, exc)
        return None


def _read_stream(handle: BinaryIO, size: int) -> tuple[np.ndarray, np.ndarray] | None:
    atoms = _Atoms(handle)
    moov = atoms.find(0, size, ("moov",))
    if not moov:
        return None
    for kind, body, stop in atoms.children(*moov):
        if kind != "trak" or _handler(atoms, body, stop) != "meta":
            continue
        entry = _sample_format(atoms, body, stop)
        if entry is None or entry[0] != "gpmd":
            continue
        samples = _samples(atoms, body, stop)
        if not samples:
            return None
        vectors: list[np.ndarray] = []
        times: list[np.ndarray] = []
        for offset, length, start_s, duration_s in samples:
            grav = _payload_gravity(_read(handle, offset, min(length, _MAX_PAYLOAD)))
            if grav is None or len(grav) == 0:
                continue
            vectors.append(grav)
            times.append(start_s + duration_s * np.arange(len(grav), dtype=np.float64) / len(grav))
        if not vectors:
            return None
        return np.concatenate(vectors).astype(np.float32), np.concatenate(times).astype(np.float32)
    return None


def _samples(atoms: _Atoms, start: int, end: int) -> list[tuple[int, int, float, float]]:
    """``(offset, size, start s, duration s)`` of every sample in a track."""
    handle = atoms.handle
    stbl = atoms.find(start, end, ("mdia", "minf", "stbl"))
    mdhd = atoms.find(start, end, ("mdia", "mdhd"))
    if not stbl or not mdhd:
        return []
    version = _read(handle, mdhd[0], 1)
    timescale = struct.unpack(">I", _read(handle, mdhd[0] + (20 if version == b"\x01" else 12), 4))[0]
    if not timescale:
        return []

    def table(kind: str) -> tuple[int, int] | None:
        return atoms.find(stbl[0], stbl[1], (kind,))

    stsz, stsc, stts = table("stsz"), table("stsc"), table("stts")
    chunk_table, wide = table("stco"), False
    if chunk_table is None:
        chunk_table, wide = table("co64"), True
    if not (stsz and stsc and stts and chunk_table):
        return []

    uniform, count = struct.unpack(">II", _read(handle, stsz[0] + 4, 8))
    sizes = [uniform] * count if uniform else list(struct.unpack(f">{count}I", handle.read(4 * count)))

    (n_chunks,) = struct.unpack(">I", _read(handle, chunk_table[0] + 4, 4))
    raw = handle.read((8 if wide else 4) * n_chunks)
    chunks = list(struct.unpack(f">{n_chunks}{'Q' if wide else 'I'}", raw))

    (n_runs,) = struct.unpack(">I", _read(handle, stsc[0] + 4, 4))
    runs = [struct.unpack(">III", handle.read(12)) for _ in range(n_runs)]

    (n_times,) = struct.unpack(">I", _read(handle, stts[0] + 4, 4))
    durations: list[int] = []
    for _ in range(n_times):
        n, delta = struct.unpack(">II", handle.read(8))
        durations.extend([delta] * n)

    out: list[tuple[int, int, float, float]] = []
    sample = 0
    clock = 0
    for i, (first_chunk, per_chunk, _desc) in enumerate(runs):
        last_chunk = runs[i + 1][0] - 1 if i + 1 < len(runs) else n_chunks
        for chunk in range(first_chunk, last_chunk + 1):
            offset = chunks[chunk - 1]
            for _ in range(per_chunk):
                if sample >= count:
                    return out
                ticks = durations[sample] if sample < len(durations) else (durations[-1] if durations else 0)
                out.append((offset, sizes[sample], clock / timescale, ticks / timescale))
                offset += sizes[sample]
                clock += ticks
                sample += 1
    return out


def _payload_gravity(payload: bytes) -> np.ndarray | None:
    """The scaled GRAV vectors of one GPMF payload, or None if it has none."""
    found: list[np.ndarray] = []
    _walk(payload, 0, len(payload), found)
    return np.concatenate(found) if found else None


def _walk(payload: bytes, at: int, end: int, found: list[np.ndarray]) -> None:
    """Walk one GPMF level; SCAL applies to the GRAV that follows it in its stream."""
    scale: np.ndarray | None = None
    while at + 8 <= end:
        key = payload[at : at + 4]
        if not key.isalnum():
            return
        kind = chr(payload[at + 4])
        item = payload[at + 5]
        repeat = struct.unpack(">H", payload[at + 6 : at + 8])[0]
        body = at + 8
        length = item * repeat
        if payload[at + 4] == 0:
            _walk(payload, body, min(body + length, end), found)
        elif key == _SCAL and kind in _TYPES:
            scale = np.frombuffer(payload, dtype=_TYPES[kind], count=length // np.dtype(_TYPES[kind]).itemsize,
                                  offset=body).astype(np.float64)
        elif key == _GRAV and kind in _TYPES:
            dtype = np.dtype(_TYPES[kind])
            values = np.frombuffer(payload, dtype=dtype, count=length // dtype.itemsize, offset=body)
            vectors = values.astype(np.float64).reshape(-1, 3)
            if scale is not None and scale.size in (1, 3) and np.all(scale != 0):
                vectors = vectors / scale
            found.append(vectors)
        at = body + length
        at += -at % 4


def install() -> bool:
    """Read gravity with this module where deepreefmap has no parser for it.

    Returns whether it was installed. A real ``py_gpmf_parser`` is left in
    charge: it is what the pipeline was tested with.
    """
    if importlib.util.find_spec("py_gpmf_parser") is not None:
        return False
    try:
        from deepreefmap.telemetry import gopro
    except ImportError:
        return False
    if getattr(gopro._extract_gravity_stream, "_deepreefmap_gui", False):
        return True

    def extract(video_path: Any) -> Any:
        stream = read_gravity_stream(Path(video_path))
        if stream is None:
            logger.warning("Gravity telemetry unavailable: no GRAV stream in %s", video_path)
            return None
        return gopro._coerce_gravity_stream(stream)

    extract._deepreefmap_gui = True  # type: ignore[attr-defined]
    gopro._extract_gravity_stream = extract
    return True
