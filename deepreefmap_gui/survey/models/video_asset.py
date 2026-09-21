"""An input video identified by content hash."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from deepreefmap_gui.io.video_hash import describe_video
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.video_probe import UNKNOWN, VideoMeta, capture_datetime, probe_metadata


def container_fields(meta: VideoMeta, mtime: str | None) -> dict[str, object]:
    """The columns one container read fills, so a backfill cannot fill fewer.

    ``probed_at`` is stamped even when nothing could be read: a file that is not
    an MP4 will not become one, and re-reading it every time the library opens
    would cost the same nothing repeatedly.
    """
    captured_at, captured_source = capture_datetime(meta, mtime)
    return {
        "captured_at": captured_at,
        "captured_source": captured_source,
        "width": meta.width,
        "height": meta.height,
        "codec": meta.codec,
        "gravity": meta.gravity,
        "gps": meta.gps,
        "probed_at": utc_now_iso(),
    }


@dataclass(slots=True)
class VideoAsset:
    """One clip on disk. Identity is ``hash``; ``path`` is advisory and may go stale."""

    file_name: str
    path: str
    hash: str | None = None
    size_bytes: int | None = None
    mtime: str | None = None
    duration_s: float | None = None
    fps: float | None = None
    captured_at: str | None = None
    captured_source: str | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    probed_at: str | None = None
    gravity: str = UNKNOWN
    gps: str = UNKNOWN
    # What a person knows about the clip and no probe can read: which camera of
    # the rig shot it, where that camera sat, whether it was mounted upside down,
    # and whether the footage is any good.
    camera_label: str | None = None
    rig_position: str | None = None
    upside_down: bool = False
    review: str = "unreviewed"
    notes: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    deleted_at: str | None = None
    device_id: uuid.UUID | None = None
    # The registry position this row was last seen at, sent back as base_seq.
    head_seq: int | None = None
    validated_at: str | None = None
    validated_by: str | None = None

    # Every field belongs to exactly one group, and the two carry-over policies
    # below are written in terms of the groups rather than a list per call site.
    # A field left out of all four is a field silently dropped when two rows for
    # one clip are folded together, so a test asserts the four cover the model.
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = ("id", "created_at")
    LOCATION_FIELDS: ClassVar[tuple[str, ...]] = ("file_name", "path")
    CARRIED_FIELDS: ClassVar[tuple[str, ...]] = (
        "hash",
        "size_bytes",
        "mtime",
        "duration_s",
        "fps",
        "captured_at",
        "captured_source",
        "width",
        "height",
        "codec",
        "probed_at",
    )
    # Tri-states, not optionals: 'unknown' is a value, so it needs the same
    # "only when the other side knows better" rule that None gets above.
    TRISTATE_FIELDS: ClassVar[tuple[str, ...]] = ("gravity", "gps")
    # The surviving row's own sync history, so folding a duplicate in never
    # adopts a stamp that belongs to a row about to disappear.
    SYNC_FIELDS: ClassVar[tuple[str, ...]] = (
        "updated_at",
        "deleted_at",
        "device_id",
        "head_seq",
        "validated_at",
        "validated_by",
    )
    # A person's word on the clip. A probe never touches these, and a merge takes
    # them only where the keeper has none.
    REVIEW_FIELDS: ClassVar[tuple[str, ...]] = (
        "camera_label",
        "rig_position",
        "upside_down",
        "review",
        "notes",
    )
    REVIEW_DEFAULTS: ClassVar[dict[str, object]] = {
        "camera_label": None,
        "rig_position": None,
        "upside_down": False,
        "review": "unreviewed",
        "notes": "",
    }

    @classmethod
    def from_path(cls, path: Path) -> VideoAsset:
        """Describe a clip on disk: hash it, and read what its container says.

        ``fps`` still needs a decoder, so callers fill it, and they may replace
        ``duration_s`` with what the decoder counted. The container's own figure
        stands in meanwhile, which is what a clip cv2 cannot open is left with.
        """
        info = describe_video(path)
        content_hash = info.get("hash")
        size_bytes = info.get("size_bytes")
        mtime = info.get("mtime")
        meta = probe_metadata(path)
        return cls(
            file_name=path.name,
            path=str(path),
            # Field cards are copied one camera per folder, so the folder is the
            # camera's name until somebody says otherwise.
            camera_label=path.parent.name or None,
            hash=content_hash if isinstance(content_hash, str) else None,
            size_bytes=size_bytes if isinstance(size_bytes, int) else None,
            mtime=mtime if isinstance(mtime, str) else None,
            duration_s=meta.duration_s,
            **container_fields(meta, mtime if isinstance(mtime, str) else None),  # type: ignore[arg-type]
        )

    def overlay_from(self, other: VideoAsset) -> None:
        """Take everything ``other`` knows, including where the file now lives.

        Upsert semantics: ``other`` is the clip just described off disk, so it is
        the more recent reading of the same file and wins wherever it has one.
        """
        for name in self.LOCATION_FIELDS + self.CARRIED_FIELDS:
            value = getattr(other, name)
            if value is not None:
                setattr(self, name, value)
        for name in self.TRISTATE_FIELDS:
            value = getattr(other, name)
            if value != UNKNOWN:
                setattr(self, name, value)

    def fill_from(self, other: VideoAsset) -> None:
        """Take only what this row lacks, and never where it lives.

        Merge semantics: ``self`` is the row that survives, so its own readings
        stand. A duplicate written later often learned something the keeper never
        did, and that would otherwise be lost with the row.
        """
        for name in self.CARRIED_FIELDS:
            if getattr(self, name) is None:
                value = getattr(other, name)
                if value is not None:
                    setattr(self, name, value)
        for name in self.REVIEW_FIELDS:
            if getattr(self, name) == self.REVIEW_DEFAULTS[name]:
                setattr(self, name, getattr(other, name))
        for name in self.TRISTATE_FIELDS:
            if getattr(self, name) == UNKNOWN:
                value = getattr(other, name)
                if value != UNKNOWN:
                    setattr(self, name, value)
