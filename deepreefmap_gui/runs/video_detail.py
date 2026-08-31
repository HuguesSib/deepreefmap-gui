"""What one clip is, and what became of the footage.

Footage outlives the runs cut from it: a card copied off the camera is a fact of
the day's diving whether or not anything has been processed from it yet. This is
the pane that says so, beside the list on the Videos page.

It lists the passes cut from the clip; picking one fills the pass card
below with what became of that cut.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.theme import ERROR, PRIMARY, SPACE_SM, SUCCESS, WARNING
from deepreefmap_gui.core.widgets import (
    clip_outcome_color,
    muted_label,
)
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.runs.video_rows import (
    ARCHIVE_CLIP,
    ARCHIVE_CLIP_TOOLTIP,
    NEW_SECTION_GLYPH,
    SectionList,
    apply_link_state,
    archive_button,
)
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_MISSING,
    VideoLibraryEntry,
)
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.statuses import clip_spec

UNAVAILABLE = "Video unavailable"

NEW_SECTION_TOOLTIP = "Cut a pass from this clip and add it to the cart."
NO_FILE_TOOLTIP = "The video file cannot be found. Add it again from where it lives now."


def clip_facts(entry: VideoLibraryEntry) -> str:
    """The line under a clip's name: how much of the survey hangs off it."""
    from deepreefmap_gui.profiling.system_probe import format_bytes

    video = entry.video
    bits = []
    if video.duration_s:
        total = int(round(video.duration_s))
        bits.append(f"{total // 60}m {total % 60:02d}s")
    if video.size_bytes:
        bits.append(format_bytes(video.size_bytes))
    if entry.pass_count:
        bits.append(f"{entry.pass_count} pass{'es' if entry.pass_count != 1 else ''}")
    if entry.run_count:
        bits.append(f"{entry.run_count} run{'s' if entry.run_count != 1 else ''}")
    return "  ·  ".join(bits)


def _short_date(stamp: str | None) -> str:
    """The date out of an ISO timestamp. The time of day says nothing here."""
    return (stamp or "").split("T")[0] or "unknown"


def _link_line(entry: VideoLibraryEntry) -> str:
    """The path, and whether the file is still at the end of it.

    Said in words rather than only in an icon: a clip whose file has moved is
    read here, and an icon in the rail is not a sentence.
    """
    if entry.link_state == LINK_MISSING:
        return f"{entry.video.path}  (not found)"
    return entry.video.path


class VideoDetailPanel(DetailCard):
    """A titled card describing the selected clip."""

    queue_requested = Signal()
    reveal_requested = Signal(str)
    pass_activated = Signal(str)
    add_to_cart_requested = Signal(str)
    archive_requested = Signal(str)
    details_requested = Signal(str)
    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    rename_requested = Signal(str)
    delete_requested = Signal(str)
    open_transect_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body

        # A clip whose file is gone can do nothing at all, so it is said in
        # words at the top of the card rather than left to an icon and a path
        # that elides in a narrow pane.
        self.unavailable = QLabel(UNAVAILABLE)
        self.unavailable.setStyleSheet(f"color: {ERROR};")
        self.unavailable.setVisible(False)
        self.title_row.addWidget(self.unavailable)

        # Whether the file is still there, said where the clip is named. It is
        # live in every state: the folder is where you go to find out what
        # became of a clip that has gone missing.
        self.link_btn = QToolButton()
        self.link_btn.setAccessibleName("Show in folder")
        self.link_btn.clicked.connect(self._emit_reveal)
        self.add_title_button(self.link_btn)

        # What a person knows about the clip: camera, rig position, review.
        self.details_btn = QToolButton()
        self.details_btn.setText("Details")
        self.details_btn.setAccessibleName("Clip details")
        self.details_btn.setProperty("quiet", "true")
        self.details_btn.setToolTip(
            "Clip details:\n• Camera and rig position\n• Upside-down mounting\n• Review verdict"
        )
        self.details_btn.clicked.connect(self._emit_details)
        self.add_title_button(self.details_btn)

        # What the registry holds of this clip, painted only from a live probe.
        self.archive_state = QLabel("")
        self.archive_state.setVisible(False)
        self.title_row.addWidget(self.archive_state)

        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(SPACE_SM)
        heading_row.addWidget(muted_label("Passes cut from this clip"))
        heading_row.addStretch(1)
        # Beside the pass rows' own actions rather than in the card's title: the
        # same glyph in the same place as the pass pane's upload button, and the
        # same place the clip's own row carries it. On request only, so a metered
        # field uplink is never spent by accident.
        self.archive_btn = archive_button(ARCHIVE_CLIP, ARCHIVE_CLIP_TOOLTIP)
        self.archive_btn.clicked.connect(self._emit_archive)
        # Hidden until a registry is enrolled: a button that sends a clip
        # nowhere is worse than no button.
        self.archive_btn.setVisible(False)
        heading_row.addWidget(self.archive_btn)
        # Cutting a section belongs to the list it adds to, not to the bottom of
        # the card: the same + the clip's own row carries, in the same blue.
        self.queue_btn = QToolButton()
        self.queue_btn.setText(NEW_SECTION_GLYPH)
        self.queue_btn.setAccessibleName("New pass")
        self.queue_btn.setProperty("quiet", "true")
        self.queue_btn.setProperty("pad", "none")
        self.queue_btn.clicked.connect(self.queue_requested)
        self._set_queue_available(True)
        heading_row.addWidget(self.queue_btn)
        layout.addLayout(heading_row)

        # The same rows the list nests under each clip, so a section offers the
        # same four things wherever it is read. It says its own emptiness, so
        # the dark well stays on screen when there is nothing in it.
        self.pass_list = SectionList()
        self.pass_list.activated.connect(self.pass_activated)
        self.pass_list.add_to_cart_requested.connect(self.add_to_cart_requested)
        self.pass_list.retrim_requested.connect(self.retrim_requested)
        self.pass_list.rename_requested.connect(self.rename_requested)
        self.pass_list.reassign_requested.connect(self.reassign_requested)
        self.pass_list.delete_requested.connect(self.delete_requested)
        self.pass_list.open_transect_requested.connect(self.open_transect_requested)
        layout.addWidget(self.pass_list, 1)

        self._entry: VideoLibraryEntry | None = None

    def _set_queue_available(self, available: bool) -> None:
        """Cutting decodes the file, so a clip that is gone cuts nothing.

        Red rather than greyed: a disabled button shows no tooltip, and the
        reason is the whole of what the user needs at that point.
        """
        self.queue_btn.setStyleSheet(f"QToolButton {{ color: {PRIMARY if available else ERROR}; }}")
        self.queue_btn.setToolTip(NEW_SECTION_TOOLTIP if available else NO_FILE_TOOLTIP)

    def _emit_reveal(self) -> None:
        if self._entry is not None:
            self.reveal_requested.emit(str(self._entry.video.id))

    def _emit_archive(self) -> None:
        if self._entry is not None:
            self.archive_requested.emit(str(self._entry.video.id))

    def _emit_details(self) -> None:
        if self._entry is not None:
            self.details_requested.emit(str(self._entry.video.id))

    def set_archive_state(self, state: str | None) -> None:
        """Paint what the registry holds of this clip, or nothing when unknown.

        Unknown covers offline, not enrolled, and a clip never offered: all
        three are states where a badge would claim more than anybody checked.
        """
        faces = {
            "archived": ("On server", SUCCESS, "Archived and verified on the registry."),
            "pending": ("Uploading…", WARNING, "Offered to the registry, not verified yet."),
            "failed": ("Archive failed", ERROR, "The registry could not verify the upload. Archive again."),
        }
        face = faces.get(state or "")
        if face is None:
            self.archive_state.setVisible(False)
            return
        text, colour, tip = face
        self.archive_state.setText(text)
        self.archive_state.setStyleSheet(f"color: {colour};")
        self.archive_state.setToolTip(tip)
        self.archive_state.setVisible(True)

    @property
    def entry(self) -> VideoLibraryEntry | None:
        return self._entry

    def set_queue_enabled(self, enabled: bool) -> None:
        self.queue_btn.setEnabled(enabled)

    def show_entry(
        self,
        entry: VideoLibraryEntry,
        transect_name: Callable[[Any], str | None],
        *,
        campaign_name: Callable[[Any], str | None] = lambda _id: None,
        in_cart: Callable[[str], bool] = lambda _pass_id: False,
        assets: Mapping[uuid.UUID, VideoAsset] | None = None,
    ) -> None:
        """Describe one clip. ``transect_name`` resolves a pass's transect id,
        and ``campaign_name`` its campaign id.

        ``assets`` is the rest of the library, passed through so a pass cut
        across chapters can still find the files its frames come from.
        """
        self.title.setText(entry.video.file_name)
        self.set_status(clip_spec(entry.outcome).label, clip_outcome_color(entry.outcome))

        rows = [("File", _link_line(entry))]
        facts = clip_facts(entry)
        if facts:
            rows.append(("Footage", facts))
        # Added and last processed, because the question a library gets asked is
        # which card this came off and whether it has been done since.
        rows.append(("Added", _short_date(entry.video.created_at)))
        last_run = entry.last_run_at
        rows.append(("Last processed", _short_date(last_run) if last_run else "never"))
        # The checksum is what makes a clip recognisable when it turns up again
        # somewhere else, so its absence is worth as much space as its value.
        rows.append(("Checksum", f"#{entry.video.hash[:8]}" if entry.video.hash else "none yet"))
        self.facts.set_rows(rows)

        apply_link_state(self.link_btn, entry.link_state)
        self.unavailable.setVisible(entry.link_state == LINK_MISSING)
        self._set_queue_available(entry.link_state == LINK_LINKED)
        self.pass_list.set_sections(
            entry, transect_name, campaign_name=campaign_name, in_cart=in_cart, assets=assets
        )
        self._entry = entry

    def select_section(self, pass_id: str | None) -> None:
        """Highlight the pass the page is showing below, or none."""
        self.pass_list.set_selected(pass_id)

    def set_server_connected(self, connected: bool) -> None:
        """Offer the archive button only where there is a registry to send to."""
        self.archive_btn.setVisible(connected)

    def clear(self) -> None:
        super().clear()
        self.pass_list.set_selected(None)
        self._entry = None
