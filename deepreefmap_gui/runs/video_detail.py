"""The selected clip, its actions and source file details."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import folder_icon, play_icon
from deepreefmap_gui.core.theme import CARD_BG, CONTROL_HEIGHT, ERROR, FONT_SM, SPACE_SM, SUCCESS, WARNING
from deepreefmap_gui.core.widgets import (
    KeyValueList,
    SectionHeader,
    clip_outcome_color,
    icon_button,
)
from deepreefmap_gui.runs.clip_details import REVIEWS, RIG_POSITIONS
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
        self._entry: VideoLibraryEntry | None = None
        self.title.setWordWrap(True)
        self.unavailable = QLabel(UNAVAILABLE)
        self.unavailable.setStyleSheet(f"color: {ERROR};")
        self.unavailable.hide()
        self.body.addWidget(self.unavailable)
        self._build_actions()
        self._build_file_details()

    def _build_actions(self) -> None:
        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        self.play_btn = icon_button(play_icon(), "Play video", "Play video")
        self.play_btn.clicked.connect(
            lambda: self.play_requested.emit(str(self._entry.video.id)) if self._entry else None
        )
        self.link_btn = icon_button(folder_icon(), "Show in folder", "Show in folder")
        self.link_btn.clicked.connect(self._emit_reveal)
        for button in (self.play_btn, self.link_btn):
            button.setFixedSize(CONTROL_HEIGHT, CONTROL_HEIGHT)
            actions.addWidget(button)
        actions.addStretch(1)
        self.queue_btn = QPushButton("Cut pass")
        self.queue_btn.setAccessibleName("New pass")
        self.queue_btn.setProperty("cta", "true")
        self.queue_btn.clicked.connect(self.queue_requested)
        self._set_queue_available(True)
        actions.addWidget(self.queue_btn)
        self.body.addLayout(actions)
        self._build_archive_action()

    def _build_archive_action(self) -> None:
        self.archive_btn = archive_button(ARCHIVE_CLIP, ARCHIVE_CLIP_TOOLTIP)
        self.archive_btn.setText("Send to server")
        self.archive_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.archive_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.archive_btn.setMinimumHeight(CONTROL_HEIGHT)
        self.archive_btn.clicked.connect(self._emit_archive)
        self.archive_btn.hide()
        self.body.addWidget(self.archive_btn)
        self.archive_state = QLabel("")
        self.archive_state.hide()
        self.body.addWidget(self.archive_state)

    def _build_file_details(self) -> None:
        details = QWidget()
        details.setObjectName("clipMetadata")
        details.setStyleSheet(f"QWidget#clipMetadata {{ background: {CARD_BG}; }}")
        layout = QVBoxLayout(details)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        heading = QHBoxLayout()
        heading.addWidget(SectionHeader("Clip details"), 1)
        self.details_btn = QPushButton("Edit…")
        self.details_btn.setToolTip("Edit camera, rig position, mounting, review and notes.")
        self.details_btn.setAccessibleName("Edit clip details")
        self.details_btn.clicked.connect(self._emit_details)
        heading.addWidget(self.details_btn)
        layout.addLayout(heading)
        self.clip_metadata = KeyValueList()
        layout.addWidget(self.clip_metadata)
        layout.addWidget(SectionHeader("File details"))
        self.technical = QLabel()
        self.technical.setWordWrap(True)
        self.technical.setTextFormat(Qt.TextFormat.PlainText)
        self.technical.setStyleSheet(f"font-size: {FONT_SM};")
        self.technical.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.technical)
        layout.addStretch(1)
        self.details_area = QScrollArea()
        self.details_area.setWidgetResizable(True)
        self.details_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.details_area.setWidget(details)
        self.body.addWidget(self.details_area, 1)

    def _set_queue_available(self, available: bool) -> None:
        """Enable playback and cutting when the source file is available."""
        self.queue_btn.setEnabled(available)
        self.play_btn.setEnabled(available)
        self.play_btn.setToolTip("Play video" if available else NO_FILE_TOOLTIP)
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
        for phase in ("preparing", "queued", "uploading", "verifying", "paused", "cancelled"):
            faces[phase] = (f"Archive {phase}", WARNING, f"Archive {phase}. Open Server for progress or resume.")
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
        self.clip_metadata.set_rows([
            ("Camera", entry.video.camera_label or "Not recorded"),
            ("Rig position", dict(RIG_POSITIONS).get(entry.video.rig_position or "", "Not recorded")),
            ("Mounting", "Upside down" if entry.video.upside_down else "Upright"),
            ("Review", dict(REVIEWS).get(entry.video.review, entry.video.review)),
            ("Notes", entry.video.notes or "None"),
        ])

        apply_link_state(self.link_btn, entry.link_state)
        self.link_btn.setIcon(folder_icon())
        self.unavailable.setVisible(entry.link_state == LINK_MISSING)
        self._set_queue_available(entry.link_state == LINK_LINKED)
        self._entry = entry

    def set_server_connected(self, connected: bool) -> None:
        """Offer the archive button only where there is a registry to send to."""
        self.archive_btn.setVisible(connected)

    def clear(self) -> None:
        super().clear()
        self._entry = None
