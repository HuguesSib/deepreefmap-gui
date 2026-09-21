"""What one clip is, and what became of the footage.

Footage outlives the runs cut from it: a card copied off the camera is a fact of
the day's diving whether or not anything has been processed from it yet. This is
the pane that says so, beside the list on the Videos page.

It lists the passes cut from the clip; picking one fills the pass card
below with what became of that cut.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.theme import ERROR, SPACE_SM, SUCCESS, WARNING
from deepreefmap_gui.core.widgets import (
    clip_outcome_color,
)
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.runs.video_rows import (
    ARCHIVE_CLIP,
    ARCHIVE_CLIP_TOOLTIP,
    apply_link_state,
    archive_button,
)
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_MISSING,
    VideoLibraryEntry,
)
from deepreefmap_gui.survey.statuses import clip_spec

UNAVAILABLE = "Video unavailable"

NEW_SECTION_TOOLTIP = "Cut a pass from this clip and add it to the queue."
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


class VideoDetailPanel(DetailCard):
    """A titled card describing the selected clip."""

    queue_requested = Signal()
    play_requested = Signal(str)
    reveal_requested = Signal(str)
    archive_requested = Signal(str)
    details_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body
        self.title.setWordWrap(True)
        tools_row = QHBoxLayout()

        # A clip whose file is gone can do nothing at all, so it is said in
        # words at the top of the card rather than left to an icon and a path
        # that elides in a narrow pane.
        self.unavailable = QLabel(UNAVAILABLE)
        self.unavailable.setStyleSheet(f"color: {ERROR};")
        self.unavailable.setVisible(False)
        layout.addWidget(self.unavailable)

        # Whether the file is still there, said where the clip is named. It is
        # live in every state: the folder is where you go to find out what
        # became of a clip that has gone missing.
        self.link_btn = QToolButton()
        self.link_btn.setAccessibleName("Show in folder")
        self.link_btn.clicked.connect(self._emit_reveal)
        self.link_btn.setText("Show in folder")
        self.link_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tools_row.addWidget(self.link_btn)

        # What a person knows about the clip: camera, rig position, review.
        self.details_btn = QToolButton()
        self.details_btn.setText("Details")
        self.details_btn.setAccessibleName("Clip details")
        self.details_btn.setProperty("quiet", "true")
        self.details_btn.setToolTip(
            "Clip details:\n• Camera and rig position\n• Upside-down mounting\n• Review verdict"
        )
        self.details_btn.clicked.connect(self._emit_details)
        tools_row.addWidget(self.details_btn)
        tools_row.addStretch(1)
        layout.addLayout(tools_row)

        # What the registry holds of this clip, painted only from a live probe.
        self.archive_state = QLabel("")
        self.archive_state.setVisible(False)
        layout.addWidget(self.archive_state)

        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(SPACE_SM)
        self.play_btn = QPushButton("Play video")
        self.play_btn.clicked.connect(
            lambda: self.play_requested.emit(str(self._entry.video.id)) if self._entry else None
        )
        heading_row.addWidget(self.play_btn)
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
        self.queue_btn.setText("Cut pass")
        self.queue_btn.setAccessibleName("New pass")
        self.queue_btn.setProperty("cta", "true")
        self.queue_btn.clicked.connect(self.queue_requested)
        self._set_queue_available(True)
        heading_row.addWidget(self.queue_btn)
        layout.addLayout(heading_row)

        layout.addStretch(1)

        self.technical_btn = QToolButton()
        self.technical_btn.setText("File details")
        self.technical_btn.setCheckable(True)
        layout.addWidget(self.technical_btn)
        self.technical = QLabel()
        self.technical.setWordWrap(True)
        self.technical.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.technical)
        self.technical.hide()
        self.technical_btn.toggled.connect(self.technical.setVisible)
        self._entry: VideoLibraryEntry | None = None

    def _set_queue_available(self, available: bool) -> None:
        """Enable playback and cutting when the source file is available."""
        self.queue_btn.setEnabled(available)
        self.play_btn.setEnabled(available)
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

    def show_entry(self, entry: VideoLibraryEntry) -> None:
        """Describe the selected clip and its source file."""
        self.title.setText(entry.video.file_name)
        self.set_status(clip_spec(entry.outcome).label, clip_outcome_color(entry.outcome))

        rows = []
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
        self.technical.setText(
            f"File: {entry.video.path}\nChecksum: {entry.video.hash or 'none yet'}"
        )
        self.facts.set_rows(rows)

        apply_link_state(self.link_btn, entry.link_state)
        self.unavailable.setVisible(entry.link_state == LINK_MISSING)
        self._set_queue_available(entry.link_state == LINK_LINKED)
        self._entry = entry

    def set_server_connected(self, connected: bool) -> None:
        """Offer the archive button only where there is a registry to send to."""
        self.archive_btn.setVisible(connected)

    def clear(self) -> None:
        super().clear()
        self._entry = None
