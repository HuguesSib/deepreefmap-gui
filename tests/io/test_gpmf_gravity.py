"""Gravity read from the MP4 itself, where py-gpmf-parser has no wheel."""

import struct

import numpy as np
from _factories import _atom

from deepreefmap_gui.io import gpmf_gravity
from deepreefmap_gui.io.gpmf_gravity import read_gravity_stream


def _klv(key: bytes, kind: bytes, item: int, repeat: int, body: bytes) -> bytes:
    body += bytes(-len(body) % 4)
    return key + kind + bytes([item]) + struct.pack(">H", repeat) + body


def _payload(vectors: list[tuple[int, int, int]], scale: int = 1000) -> bytes:
    grav = b"".join(struct.pack(">hhh", *v) for v in vectors)
    strm = _klv(b"SCAL", b"s", 2, 1, struct.pack(">h", scale)) + _klv(b"GRAV", b"s", 6, len(vectors), grav)
    devc = _klv(b"DVID", b"L", 4, 1, struct.pack(">I", 1)) + _klv(b"STRM", b"\x00", 1, len(strm), strm)
    return _klv(b"DEVC", b"\x00", 1, len(devc), devc)


def _write_clip(path, payloads: list[bytes], seconds_each: int = 1) -> None:
    """Two one-second GPMF samples in one chunk, as a GoPro lays them out."""
    timescale = 1000
    mdhd = _atom(b"mdhd", bytes(4), struct.pack(">IIII", 0, 0, timescale, seconds_each * timescale * len(payloads)),
                 bytes(4))
    stsz = _atom(b"stsz", bytes(4), struct.pack(">II", 0, len(payloads)),
                 b"".join(struct.pack(">I", len(p)) for p in payloads))
    stsc = _atom(b"stsc", bytes(4), struct.pack(">IIII", 1, 1, len(payloads), 1))
    stts = _atom(b"stts", bytes(4), struct.pack(">III", 1, len(payloads), seconds_each * timescale))
    entry = _atom(b"gpmd", bytes(6), struct.pack(">H", 1))

    def build(offset: int) -> bytes:
        stco = _atom(b"stco", bytes(4), struct.pack(">II", 1, offset))
        hdlr = _atom(b"hdlr", bytes(8), b"meta", bytes(12), b"\x00")
        stbl = _atom(b"stbl", _atom(b"stsd", bytes(4), struct.pack(">I", 1), entry), stsz, stsc, stts, stco)
        return _atom(b"moov", _atom(b"trak", _atom(b"mdia", mdhd, hdlr, _atom(b"minf", stbl))))

    ftyp = _atom(b"ftyp", b"isom", struct.pack(">I", 512), b"isom")
    moov = build(len(ftyp) + len(build(0)) + 8)
    path.write_bytes(ftyp + moov + _atom(b"mdat", *payloads))


def test_gravity_is_scaled_and_timed_across_samples(tmp_path):
    clip = tmp_path / "GX010001.MP4"
    _write_clip(clip, [_payload([(0, 0, 1000), (0, 1000, 0)]), _payload([(1000, 0, 0), (0, 0, -1000)])])

    vectors, times = read_gravity_stream(clip)

    np.testing.assert_allclose(vectors, [[0, 0, 1], [0, 1, 0], [1, 0, 0], [0, 0, -1]])
    np.testing.assert_allclose(times, [0.0, 0.5, 1.0, 1.5])


def test_a_clip_without_gravity_says_so(tmp_path):
    clip = tmp_path / "GX010002.MP4"
    _write_clip(clip, [_klv(b"DEVC", b"\x00", 1, 8, _klv(b"DVID", b"L", 4, 1, bytes(4)))])
    assert read_gravity_stream(clip) is None
    junk = tmp_path / "junk.MP4"
    junk.write_bytes(b"not a video")
    assert read_gravity_stream(junk) is None


def test_deepreefmap_reads_through_it_when_the_parser_is_missing(tmp_path, monkeypatch):
    from deepreefmap.telemetry import gopro

    clip = tmp_path / "GX010003.MP4"
    _write_clip(clip, [_payload([(0, 0, 1000), (0, 0, 1000)])])
    monkeypatch.setattr(gpmf_gravity.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(gopro, "_extract_gravity_stream", gopro._extract_gravity_stream)

    assert gpmf_gravity.install()
    grav, times = gopro._extract_gravity_stream(clip)

    np.testing.assert_allclose(grav, [[0, 0, 1], [0, 0, 1]])
