"""Read what a clip's container says about itself: when it was shot, how big, and
whether the camera recorded a gravity vector.

Gravity is the reason this exists. The mapping backends align a reconstruction
with the ``GRAV`` stream a GoPro writes into its telemetry track, so whether a
clip carries one decides how the run comes out, and the user needs to know that
before processing rather than after. The library reads that stream through
``py_gpmf_parser``, which is published for ``linux-x86_64`` only, so asking it
would answer "no gravity" on the Windows and macOS builds regardless of the file.

An MP4 says all of it in its own index. Walking the atoms costs a handful of
seeks and about 7 KB of reads, well under a millisecond on a 2.5 GB clip, needs
no decoder and no dependency, and behaves the same on every platform.

Nothing here raises. A file that cannot be parsed reports ``readable=False`` and
says ``UNKNOWN`` rather than ``NO``: "the camera recorded no gravity" and "we
could not tell" are different facts and the interface shows them differently.
"""

from __future__ import annotations

import importlib.util
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

logger = logging.getLogger(__name__)

YES = "yes"
NO = "no"
UNKNOWN = "unknown"

SOURCE_CONTAINER = "container"
SOURCE_MTIME = "mtime"

# QuickTime counts seconds from 1904, not 1970.
_QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def gravity_telemetry_available() -> bool:
    """Whether GoPro gravity telemetry can be read here.

    A spec lookup, never an import. py-gpmf-parser publishes Linux x86_64 wheels
    only, so this is False on macOS and Windows unless a wheel was built for them.
    """
    return importlib.util.find_spec("py_gpmf_parser") is not None

# Cameras that do not set a creation time write zero, and files that mistake the
# epoch land decades out. Anything outside this window is not a shooting date.
_EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)

# A well-formed clip is a few thousand atoms. A budget is what stops a corrupt
# one walking us in circles, since a zero-length atom is otherwise a loop.
_MAX_ATOMS = 100_000

# One second of GoPro telemetry is a few KB. Needing more than this to find a
# four-character key means the offsets are not pointing at GPMF.
_MAX_PAYLOAD = 1 << 20

_GRAVITY_KEY = b"GRAV"
_GPS_KEY = b"GPS5"


@dataclass(frozen=True, slots=True)
class VideoMeta:
    """What the container knows. Every field is optional because a file may lie."""

    captured_at: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    gravity: str = UNKNOWN
    gps: str = UNKNOWN
    readable: bool = False

    @property
    def resolution(self) -> str | None:
        """``1920x1080``, or None when the video track did not say."""
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


class _Atoms:
    """A bounded walk over one file's atom tree."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.budget = _MAX_ATOMS

    def children(self, start: int, end: int) -> Iterator[tuple[str, int, int]]:
        """Yield ``(kind, body_start, atom_end)`` for each atom in a range."""
        pos = start
        while pos + 8 <= end and self.budget > 0:
            self.budget -= 1
            self.handle.seek(pos)
            header = self.handle.read(8)
            if len(header) < 8:
                return
            size, raw_kind = struct.unpack(">I4s", header)
            body = pos + 8
            if size == 1:
                extended = self.handle.read(8)
                if len(extended) < 8:
                    return
                size = struct.unpack(">Q", extended)[0]
                body = pos + 16
            elif size == 0:
                size = end - pos
            if size < body - pos or pos + size > end:
                return
            yield raw_kind.decode("latin1", "replace"), body, pos + size
            pos += size

    def find(self, start: int, end: int, path: tuple[str, ...]) -> tuple[int, int] | None:
        """Follow a path of atom kinds, returning the last one's body range."""
        for kind, body, stop in self.children(start, end):
            if kind != path[0]:
                continue
            if len(path) == 1:
                return body, stop
            found = self.find(body, stop, path[1:])
            if found:
                return found
        return None


def _read(handle: BinaryIO, offset: int, count: int) -> bytes:
    handle.seek(offset)
    return handle.read(count)


def _movie_header(atoms: _Atoms, start: int, end: int) -> tuple[str | None, float | None]:
    """The capture time and duration from ``mvhd``."""
    found = atoms.find(start, end, ("mvhd",))
    if not found:
        return None, None
    body, _ = found
    version = _read(atoms.handle, body, 4)[:1]
    if not version:
        return None, None
    if version[0] == 1:
        chunk = atoms.handle.read(28)
        if len(chunk) < 28:
            return None, None
        created, _modified, timescale, ticks = struct.unpack(">QQIQ", chunk)
    else:
        chunk = atoms.handle.read(16)
        if len(chunk) < 16:
            return None, None
        created, _modified, timescale, ticks = struct.unpack(">IIII", chunk)

    captured = None
    if created:
        moment = _QT_EPOCH + timedelta(seconds=created)
        if _EARLIEST <= moment < _LATEST:
            captured = moment.isoformat()
    duration = ticks / timescale if timescale and ticks else None
    return captured, duration


def _sample_format(atoms: _Atoms, start: int, end: int) -> tuple[str, int] | None:
    """The first sample entry's four-character format and where its body begins."""
    found = atoms.find(start, end, ("mdia", "minf", "stbl", "stsd"))
    if not found:
        return None
    body, _ = found
    entry = _read(atoms.handle, body + 8, 8)
    if len(entry) < 8:
        return None
    return entry[4:].decode("latin1", "replace"), body + 16


def _visual_size(handle: BinaryIO, entry_body: int) -> tuple[int | None, int | None]:
    """Coded width and height, past the sample entry's fixed preamble."""
    raw = _read(handle, entry_body + 24, 4)
    if len(raw) < 4:
        return None, None
    width, height = struct.unpack(">HH", raw)
    return width or None, height or None


def _first_sample(atoms: _Atoms, start: int, end: int) -> bytes | None:
    """The track's first sample, located through its size and chunk tables."""
    sizes = atoms.find(start, end, ("mdia", "minf", "stbl", "stsz"))
    chunks = atoms.find(start, end, ("mdia", "minf", "stbl", "stco"))
    wide = False
    if not chunks:
        chunks = atoms.find(start, end, ("mdia", "minf", "stbl", "co64"))
        wide = True
    if not sizes or not chunks:
        return None

    header = _read(atoms.handle, sizes[0] + 4, 8)
    if len(header) < 8:
        return None
    uniform, count = struct.unpack(">II", header)
    if uniform:
        size = uniform
    elif count:
        raw = atoms.handle.read(4)
        if len(raw) < 4:
            return None
        size = struct.unpack(">I", raw)[0]
    else:
        return None

    raw = _read(atoms.handle, chunks[0] + 4, 12 if wide else 8)
    if len(raw) < (12 if wide else 8):
        return None
    offset = struct.unpack(">Q", raw[4:12])[0] if wide else struct.unpack(">I", raw[4:8])[0]
    return _read(atoms.handle, offset, min(size, _MAX_PAYLOAD))


def _payload_keys(payload: bytes) -> set[bytes]:
    """The four-character keys in one GPMF payload.

    GPMF is key, type, item size, repeat, then that many bytes padded to four. A
    type of zero is a nested container, so not skipping its length is what walks
    into it, which is where the streams themselves live.
    """
    keys: set[bytes] = set()
    at = 0
    while at + 8 <= len(payload):
        key = payload[at : at + 4]
        if not key.isalnum():
            break
        keys.add(key)
        kind = payload[at + 4]
        item = payload[at + 5]
        repeat = struct.unpack(">H", payload[at + 6 : at + 8])[0]
        at += 8 + (0 if kind == 0 else item * repeat)
        at += -at % 4
    return keys


def _telemetry(atoms: _Atoms, start: int, end: int) -> tuple[str, str]:
    """Gravity and GPS availability from a GoPro metadata track's first payload."""
    payload = _first_sample(atoms, start, end)
    if payload is None:
        return UNKNOWN, UNKNOWN
    keys = _payload_keys(payload)
    return (
        YES if _GRAVITY_KEY in keys else NO,
        YES if _GPS_KEY in keys else NO,
    )


def _handler(atoms: _Atoms, start: int, end: int) -> str | None:
    found = atoms.find(start, end, ("mdia", "hdlr"))
    if not found:
        return None
    raw = _read(atoms.handle, found[0] + 8, 4)
    return raw.decode("latin1", "replace") if len(raw) == 4 else None


def probe_metadata(path: Path) -> VideoMeta:
    """Read a clip's container. Never raises: an unreadable file reports nothing."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return _probe(handle, size)
    except (OSError, ValueError, struct.error) as exc:
        logger.debug("Could not read the container of %s: %s", path, exc)
        return VideoMeta()


def _probe(handle: BinaryIO, size: int) -> VideoMeta:
    atoms = _Atoms(handle)
    # Some GoPro firmware writes moov after mdat, so the top level is scanned to
    # the end of the file. Stopping early would blank the date for exactly the
    # cameras this is for.
    moov = atoms.find(0, size, ("moov",))
    if not moov:
        return VideoMeta()

    captured, duration = _movie_header(atoms, *moov)
    width = height = None
    codec = None
    gravity = gps = NO

    for kind, body, stop in atoms.children(*moov):
        if kind != "trak":
            continue
        handler = _handler(atoms, body, stop)
        entry = _sample_format(atoms, body, stop)
        if entry is None:
            continue
        fourcc, entry_body = entry
        if handler == "vide" and codec is None:
            codec = fourcc
            width, height = _visual_size(handle, entry_body)
        elif handler == "meta" and fourcc == "gpmd":
            gravity, gps = _telemetry(atoms, body, stop)

    return VideoMeta(
        captured_at=captured,
        duration_s=duration,
        width=width,
        height=height,
        codec=codec,
        gravity=gravity,
        gps=gps,
        readable=True,
    )


def capture_datetime(meta: VideoMeta, mtime: str | None) -> tuple[str | None, str | None]:
    """Pick the best capture time available, and say where it came from.

    A re-encoded or trimmed clip loses its embedded creation time, so the file's
    own timestamp stands in. That is a worse answer, not a wrong one, and the
    source is returned so the interface can mark it as the estimate it is.
    """
    if meta.captured_at:
        return meta.captured_at, SOURCE_CONTAINER
    if mtime:
        return mtime, SOURCE_MTIME
    return None, None
