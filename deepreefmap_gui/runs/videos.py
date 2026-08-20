"""Videos: the footage itself, grouped by the day it was shot.

The destination a dive day starts and ends at. Clips arrive here, are cut into
sections, get a transect, and go in the cart; afterwards the same rows show what
came of them. Browse is for finished runs, so a clip appears in exactly one
place in the app and this is it.

The library it lists comes from ``survey/catalogue.py::video_library``, the
grouping and the section strips from ``survey/video_groups.py``, and the widgets
from ``runs/video_rows.py``. What lives here is the page: which clips are shown,
what the buttons do, and keeping the answers off the thread that paints them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.reveal import reveal_in_file_manager
from deepreefmap_gui.core.theme import (
    BORDER,
    ERROR,
    GUTTER,
    PRIMARY,
    RADIUS_SM,
    SPACE_MD,
    SPACE_SM,
    SPLIT_MIN_TOTAL,
    WEIGHT_SEMIBOLD,
)
from deepreefmap_gui.core.widgets import (
    EmptyState,
    FilterChips,
    clip_outcome_color,
    confirm,
    muted_label,
    section_column,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.runs.section_detail import SectionDetailPanel, section_window
from deepreefmap_gui.runs.video_detail import VideoDetailPanel
from deepreefmap_gui.runs.video_rows import (
    DELETE_ARM_MS,
    KEEPS_FILE_NOTE,
    VideoLibraryList,
    VideoListHeader,
)
from deepreefmap_gui.survey import catalogue, statuses
from deepreefmap_gui.survey.catalogue import LINK_LINKED, LINK_MISSING, VideoLibraryEntry
from deepreefmap_gui.survey.models import TransectPass
from deepreefmap_gui.survey.video_groups import (
    DEFAULT_PERIOD,
    DEFAULT_SORT_COLUMN,
    DEFAULT_SORT_DESCENDING,
    PERIODS,
    SORT_COLUMNS,
    group_by_period,
    pass_status,
    sort_groups,
)

logger = logging.getLogger(__name__)

# Chip order is the order work moves through: nothing done, part done, broken,
# finished. "All" first so the unfiltered view is where the eye starts.
_CLIP_FILTERS = (
    ("all", "All", PRIMARY),
    *((spec.key, spec.label, clip_outcome_color(spec.key)) for spec in statuses.CLIP_OUTCOMES),
)

_SEARCH_WIDTH = 240

# The clip list is the page; the detail pane describes whichever row is picked.
_DETAIL_SHARE = 0.30
_DETAIL_MIN_WIDTH = 260

_PERIOD_TOOLTIP = (
    "How far apart two clips have to be shot to be filed separately. A card off "
    "one dive day reads as one group by day."
)

# The sort order lands in QSettings as one of these words rather than a bool:
# some backends hand a stored bool back as the string "false", which is truthy.
_ORDER_ASCENDING, _ORDER_DESCENDING = "ascending", "descending"

_HIDDEN_TOOLTIP = (
    "Clips hidden on this machine. Hiding is a view of the library rather than a "
    "fact about it, so nothing is removed and nobody else sees the difference."
)


# The two bulk actions ask in the button, the way a clip row's own trash does:
# one press arms, the second acts, and DELETE_ARM_MS stands it back down. A
# whole library is more than a gesture is worth, so above this many clips the
# question goes back into a dialog.
_BULK_CONFIRM_ABOVE = 20

# There is no tick column and no select mode, so the bar says how to pick more
# than one clip. Nothing else on the page does.
_PICK_HINT = "Ctrl-click or shift-click to pick more than one clip."

# Named for what it does rather than for the "Not processed" chip, whose count
# is wider: a clip cut into sections and never run reads as part processed and
# is not swept, because re-importing the file does not bring its trims back.
_SWEEP_IDLE = "Remove clips with no sections"


def _sections_phrase(count: int) -> str:
    return f"{count} section" if count == 1 else f"{count} sections"


def _clips_phrase(count: int) -> str:
    return f"{count} clip" if count == 1 else f"{count} clips"


def _delete_label(count: int) -> str:
    """What the bulk delete offers to do, which follows what is picked."""
    if count < 1:
        return "Delete"
    return "Delete clip" if count == 1 else f"Delete all {count}"


def _sweep_label(count: int) -> str:
    return _SWEEP_IDLE if count < 1 else f"Remove {_clips_phrase(count)} with no sections"


def _delete_report(deleted: list[VideoLibraryEntry], refused: int) -> str:
    """What the delete did, and what it would not do.

    A refusal is named rather than passed over: a status line reporting only
    the clips that went claims a success the other half of the set never had.
    """
    kept = (
        f" {_clips_phrase(refused)} kept: a run was made from "
        f"{'it' if refused == 1 else 'them'}, so delete those in Browse first."
        if refused
        else ""
    )
    if not deleted:
        return f"{kept.strip()} {KEEPS_FILE_NOTE}"
    if len(deleted) == 1:
        clip = deleted[0]
        sections = f" and {_sections_phrase(len(clip.passes))}" if clip.passes else ""
        head = f"{clip.video.file_name}{sections} removed from the library."
    else:
        head = f"{_clips_phrase(len(deleted))} removed from the library."
    return f"{head}{kept} {KEEPS_FILE_NOTE}"


class VideoLibraryMixin(MixinBase):
    """DeepReefMapWindow methods that build and drive the Videos destination."""

    _video_clip_filter: str = "all"
    _video_period: str = DEFAULT_PERIOD
    _video_sort_column: str = DEFAULT_SORT_COLUMN
    _video_sort_descending: bool = DEFAULT_SORT_DESCENDING
    _selected_pass_id: str | None = None
    _video_split_user_sized: bool = False
    _video_split_applying: bool = False

    def _build_video_library(self) -> QWidget:
        self._video_entries: list[VideoLibraryEntry] = []
        # Keyed by path rather than clip id, so an answer survives the library
        # being rebuilt on the next scan.
        self._clip_link_cache: dict[str, str] = {}
        self._clip_link_scan_running = False
        # Paths a single-clip recheck is already asking about, so a double click
        # on a sleeping drive queues one stat rather than two.
        self._clip_link_rechecking: set[str] = set()
        self._video_period = str(
            self._settings.value("video_group_period", DEFAULT_PERIOD) or DEFAULT_PERIOD
        )
        stored_column = str(self._settings.value("video_sort_column", DEFAULT_SORT_COLUMN) or "")
        self._video_sort_column = (
            stored_column if stored_column in SORT_COLUMNS else DEFAULT_SORT_COLUMN
        )
        stored_order = str(self._settings.value("video_sort_order", "") or "")
        if stored_order in (_ORDER_ASCENDING, _ORDER_DESCENDING):
            self._video_sort_descending = stored_order == _ORDER_DESCENDING
        self._hidden_clip_ids = self._load_hidden_clips()

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        top_row = QHBoxLayout()
        top_row.setSpacing(GUTTER)
        top_row.addWidget(QLabel("Group"))
        self._video_period_chips = FilterChips(PERIODS)
        self._video_period_chips.setToolTip(_PERIOD_TOOLTIP)
        self._video_period_chips.set_current(self._video_period)
        self._video_period_chips.changed.connect(self._on_video_period_changed)
        top_row.addWidget(self._video_period_chips)
        self._video_search = QLineEdit()
        self._video_search.setPlaceholderText("Search clips…")
        self._video_search.setClearButtonEnabled(True)
        self._video_search.setMaximumWidth(_SEARCH_WIDTH)
        self._video_search.textChanged.connect(lambda *_: self._rebuild_video_list())
        top_row.addWidget(self._video_search)
        self._video_chips = FilterChips(_CLIP_FILTERS)
        self._video_chips.setToolTip(
            "Where each clip stands: whether every section cut from it has been "
            "processed, and whether any of them failed."
        )
        self._video_chips.changed.connect(self._on_video_filter_changed)
        top_row.addWidget(self._video_chips)
        # Only there when something is hidden: a checkbox offering to reveal
        # nothing is a control that has to be read to be dismissed.
        self._video_hidden_check = QCheckBox()
        self._video_hidden_check.setToolTip(_HIDDEN_TOOLTIP)
        self._video_hidden_check.toggled.connect(lambda *_: self._rebuild_video_list())
        self._video_hidden_check.setVisible(False)
        top_row.addWidget(self._video_hidden_check)
        top_row.addStretch(1)
        self._video_add_btn = QPushButton("Add videos…")
        self._video_add_btn.clicked.connect(self._on_video_add_clicked)
        top_row.addWidget(self._video_add_btn)
        layout.addLayout(top_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(SPACE_SM)

        column, column_layout = section_column("Footage")
        self._video_header = VideoListHeader()
        self._video_header.set_sort(self._video_sort_column, self._video_sort_descending)
        self._video_header.sort_changed.connect(self._on_video_sort_changed)
        column_layout.addWidget(self._video_header)
        self._video_list = VideoLibraryList()
        self._video_list.follow_header(self._video_header)
        self._video_list.activated.connect(self._on_video_activated)
        self._video_list.play_requested.connect(self._on_video_play)
        self._video_list.reveal_requested.connect(self._on_video_reveal)
        self._video_list.new_section_requested.connect(self._on_video_new_section_for)
        self._video_list.span_clicked.connect(self._select_section)
        self._video_list.hide_requested.connect(self._on_video_hide)
        self._video_list.delete_unused_requested.connect(self._on_video_delete_unused)
        self._video_list.delete_requested.connect(self._on_video_delete)
        self._video_list.section_activated.connect(self._select_section)
        self._video_list.section_add_to_cart.connect(self._on_video_pass_to_cart)
        self._video_list.section_retrim.connect(self._on_section_retrim)
        self._video_list.section_reassign.connect(self._on_section_reassign)
        self._video_list.section_delete.connect(self._on_section_delete)
        self._video_list.section_open_transect.connect(self._open_transect_page)
        self._video_list.selection_changed.connect(self._refresh_video_actions)
        self._video_stack = QStackedWidget()
        self._video_stack.addWidget(self._video_list)
        self._video_stack.addWidget(
            EmptyState("No footage yet", "Drop clips here, or use Add videos…")
        )
        # The stack, not the list inside it: Qt finds a drop target by walking up
        # from the widget under the cursor, and an empty library hides the list
        # behind the empty state that advertises the drop.
        self._video_stack.setAcceptDrops(True)
        self._video_stack.installEventFilter(self)
        column_layout.addWidget(self._video_stack, 1)
        column_layout.addWidget(self._build_video_actions())
        split.addWidget(column)

        # The clip above the section it holds, so drilling in never costs sight
        # of what was drilled into: the cut list stays on screen beside the runs
        # made from the cut.
        detail = QSplitter(Qt.Orientation.Vertical)
        detail.setHandleWidth(SPACE_SM)
        detail.setMinimumWidth(_DETAIL_MIN_WIDTH)

        self._video_detail = VideoDetailPanel()
        self._video_detail.queue_requested.connect(self._on_video_new_section)
        self._video_detail.pass_activated.connect(self._select_section)
        self._video_detail.add_to_cart_requested.connect(self._on_video_pass_to_cart)
        self._video_detail.retrim_requested.connect(self._on_section_retrim)
        self._video_detail.reassign_requested.connect(self._on_section_reassign)
        self._video_detail.delete_requested.connect(self._on_section_delete)
        self._video_detail.open_transect_requested.connect(self._open_transect_page)
        self._video_detail.reveal_requested.connect(self._on_video_reveal)
        self._video_detail.archive_requested.connect(self._archive_video)
        # The pane keeps its place with nothing selected: a right half that
        # disappears re-lays the page every time a clip is picked or dropped.
        self._video_detail_stack = QStackedWidget()
        self._video_detail_stack.addWidget(
            EmptyState("No clip selected", "Pick a clip to see its sections and runs.")
        )
        self._video_detail_stack.addWidget(self._video_detail)
        detail.addWidget(self._video_detail_stack)

        self._section_detail = SectionDetailPanel()
        self._section_detail.retrim_requested.connect(self._on_section_retrim)
        self._section_detail.reassign_requested.connect(self._on_section_reassign)
        self._section_detail.delete_requested.connect(self._on_section_delete)
        self._section_detail.run_activated.connect(self._on_section_run_activated)
        self._section_detail.setVisible(False)
        detail.addWidget(self._section_detail)
        split.addWidget(detail)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.splitterMoved.connect(self._on_video_split_moved)
        # The window's eventFilter delegates resizes to
        # _video_split_event_filter, the same route Browse's splitter takes.
        split.installEventFilter(self)
        self._video_split = split
        layout.addWidget(split, 1)
        return page

    def _apply_video_split_sizes(self) -> None:
        """Divide the page between the clip list and the detail pane.

        Set outright rather than left to stretch factors: a splitter only
        shares out the space above each pane's minimum, so the detail pane sat
        at its 260px floor however wide the window got, and the section card's
        buttons truncated to fit it.
        """
        if getattr(self, "_video_split_user_sized", False):
            return
        total = self._video_split.width()
        if total < SPLIT_MIN_TOTAL:
            total = sum(self._video_split.sizes()) or 1200
        detail = max(_DETAIL_MIN_WIDTH, int(total * _DETAIL_SHARE))
        self._video_split_applying = True
        try:
            self._video_split.setSizes([max(1, total - detail), detail])
        finally:
            self._video_split_applying = False

    def _video_split_event_filter(self, obj, event) -> None:
        """Re-divide the page when its splitter is resized.

        Guarded on the splitter existing: the filter is installed on the
        window, which receives events while the page is still being built.
        """
        if obj is getattr(self, "_video_split", None) and event.type() == QEvent.Type.Resize:
            self._apply_video_split_sizes()

    def _on_video_split_moved(self, *_args) -> None:
        """A dragged handle is a decision; stop overriding it on every resize."""
        if not getattr(self, "_video_split_applying", False):
            self._video_split_user_sized = True

    # --- acting on more than one clip ----------------------------------------

    def _build_video_actions(self) -> QWidget:
        """What can be done to the picked clips, under the list they are picked in.

        Under the Footage column rather than in the filter row, so a destructive
        button is nowhere near the chips a user clicks through all day.
        """
        bar = QWidget()
        bar.setObjectName("videoActionBar")
        bar.setStyleSheet(
            f"QWidget#videoActionBar {{ border: 1px solid {BORDER};"
            f" border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        self._video_finding = muted_label(_PICK_HINT)
        row.addWidget(self._video_finding, 1)

        self._video_clear_btn = QPushButton(_SWEEP_IDLE)
        self._video_clear_btn.setEnabled(False)
        self._video_clear_btn.clicked.connect(self._on_video_sweep_clicked)
        row.addWidget(self._video_clear_btn)

        self._video_delete_btn = QPushButton(_delete_label(0))
        self._video_delete_btn.setEnabled(False)
        self._video_delete_btn.clicked.connect(self._on_video_delete_clicked)
        row.addWidget(self._video_delete_btn)

        # Owned by the window rather than by either button, so whichever is
        # armed can be stood down from anywhere the target set moves.
        self._video_arm = QTimer(self)
        self._video_arm.setSingleShot(True)
        self._video_arm.setInterval(DELETE_ARM_MS)
        self._video_arm.timeout.connect(self._refresh_video_actions)
        self._video_armed: str | None = None
        return bar

    def _selected_clips(self) -> list[VideoLibraryEntry]:
        """The picked clips, in the order the list shows them."""
        picked = self._video_list.selection()
        return [c for c in self._visible_clips() if str(c.video.id) in picked]

    def _sweepable_clips(self) -> list[VideoLibraryEntry]:
        """The clips on screen with nothing cut from them and nothing made from them.

        Orphans only. Deleting is records-only and adding the file again mints a
        fresh clip, so a swept orphan is recoverable; the sections cut from a
        clip are not, and those trim windows are the user's own work.

        Drawn from what is listed, so the count is what is on screen and a
        hidden clip is never swept out from under the user.
        """
        return [c for c in self._visible_clips() if c.orphan and not c.runs]

    def _refresh_video_actions(self) -> None:
        """Relabel both bulk actions, disarmed.

        Disarming first is the contract a two-press button lives by: the set the
        first press armed is not the set the second would act on.
        """
        if getattr(self, "_video_delete_btn", None) is None:
            return
        self._video_armed = None
        self._video_arm.stop()
        self._paint_video_actions()

    def _paint_video_actions(self) -> None:
        """Each button says what it would do, and the armed one asks first."""
        picked = len(self._selected_clips())
        sweepable = len(self._sweepable_clips())
        armed = self._video_armed if self._video_arm.isActive() else None
        self._video_delete_btn.setText(
            f"Click again to delete {picked}" if armed == "delete" else _delete_label(picked)
        )
        self._video_clear_btn.setText(
            f"Click again to remove {sweepable}" if armed == "sweep" else _sweep_label(sweepable)
        )
        for name, button in (
            ("delete", self._video_delete_btn),
            ("sweep", self._video_clear_btn),
        ):
            button.setStyleSheet(
                f"color: {ERROR}; font-weight: {WEIGHT_SEMIBOLD};" if armed == name else ""
            )
        self._video_delete_btn.setEnabled(bool(picked))
        self._video_clear_btn.setEnabled(bool(sweepable))
        self._video_finding.setText(
            _PICK_HINT if not picked else f"{_clips_phrase(picked)} picked."
        )

    def _video_action_armed(self, name: str) -> bool:
        """True on the second press of an action, arming it on the first."""
        if self._video_armed == name and self._video_arm.isActive():
            self._refresh_video_actions()
            return True
        self._video_armed = name
        self._video_arm.start()
        self._paint_video_actions()
        return False

    def _on_video_delete_clicked(self) -> None:
        """Delete every picked clip, once the button has been asked twice."""
        clips = self._selected_clips()
        if not clips:
            return
        if len(clips) > _BULK_CONFIRM_ABOVE:
            self._refresh_video_actions()
            if confirm(
                self,
                "Delete clips",
                f"Remove {_clips_phrase(len(clips))} from the library? {KEEPS_FILE_NOTE}",
            ):
                self._delete_clips(clips)
            return
        if self._video_action_armed("delete"):
            self._delete_clips(clips)

    def _on_video_sweep_clicked(self) -> None:
        """Clear out the clips nothing was ever cut from."""
        clips = self._sweepable_clips()
        if not clips:
            return
        if len(clips) > _BULK_CONFIRM_ABOVE:
            self._refresh_video_actions()
            if confirm(
                self,
                "Remove clips",
                f"Remove {_clips_phrase(len(clips))} with no sections from the "
                f"library? {KEEPS_FILE_NOTE}",
            ):
                self._delete_clips(clips)
            return
        if self._video_action_armed("sweep"):
            self._delete_clips(clips)

    # --- the library ---------------------------------------------------------

    def _refresh_video_library(self, store=None) -> None:
        """Re-read the library and repaint. Called by whatever rescanned the root."""
        if store is None:
            store = self._try_survey_store()
        self._video_entries = self._load_video_entries(store)
        for clip in self._video_entries:
            clip.link_state = self._clip_link_cache.get(clip.video.path, catalogue.LINK_UNKNOWN)
        self._start_clip_link_scan()
        self._rebuild_video_list()
        self._refresh_storage_bars()

    def _load_video_entries(self, store) -> list[VideoLibraryEntry]:
        """Every clip the survey has imported, runs or no runs."""
        if store is None:
            return []
        try:
            return catalogue.video_library(
                store.list_videos(), store.list_passes(), store.list_runs()
            )
        except Exception:
            logger.exception("Could not list the video library")
            return []

    # --- hidden clips --------------------------------------------------------

    def _load_hidden_clips(self) -> set[str]:
        """Which clips this machine keeps out of the list.

        QSettings rather than the database: hiding a clip says nothing about the
        survey, only about which of it one reader wants to look at today, and a
        colleague opening the same root should see every clip in it.
        """
        stored = self._settings.value("video_hidden_ids", [])
        if isinstance(stored, str):
            # Some backends hand a one-element list back as a bare string.
            stored = [stored] if stored else []
        if not isinstance(stored, (list, tuple)):
            return set()
        return {str(value) for value in stored}

    def _save_hidden_clips(self) -> None:
        self._settings.setValue("video_hidden_ids", sorted(self._hidden_clip_ids))

    def _on_video_hide(self, video_id: str) -> None:
        """Hide the clip, or put it back when it is already hidden."""
        clip = self._clip_by_id(video_id)
        if clip is None:
            return
        if video_id in self._hidden_clip_ids:
            self._hidden_clip_ids.discard(video_id)
            note = f"{clip.video.file_name} is back in the list."
        else:
            self._hidden_clip_ids.add(video_id)
            note = f"{clip.video.file_name} hidden. Show hidden brings it back."
            if self._video_list.selected == video_id:
                self._video_list.set_selected(None)
        self._save_hidden_clips()
        self._rebuild_video_list()
        self._refresh_video_detail()
        self._status_label.setText(note)

    def _refresh_hidden_control(self) -> None:
        hidden = len(self._hidden_clip_ids)
        self._video_hidden_check.setVisible(bool(hidden))
        self._video_hidden_check.setText(f"Show hidden ({hidden})")
        if not hidden and self._video_hidden_check.isChecked():
            self._video_hidden_check.setChecked(False)

    def _visible_clips(self) -> list[VideoLibraryEntry]:
        clips = getattr(self, "_video_entries", [])
        if self._hidden_clip_ids and not self._video_hidden_check.isChecked():
            clips = [c for c in clips if str(c.video.id) not in self._hidden_clip_ids]
        if self._video_clip_filter != "all":
            clips = [c for c in clips if c.outcome == self._video_clip_filter]
        needle = self._video_search.text().strip().lower()
        if needle:
            clips = [c for c in clips if needle in c.video.file_name.lower()]
        return clips

    def _rebuild_video_list(self) -> None:
        if getattr(self, "_video_list", None) is None:
            return
        self._refresh_hidden_control()
        clips = self._visible_clips()
        groups = sort_groups(
            group_by_period(clips, self._video_period),
            self._video_sort_column,
            descending=self._video_sort_descending,
        )
        self._video_list.set_groups(
            groups,
            self._transect_name_for,
            in_cart=self._cart_pass_ids().__contains__,
            hidden=self._hidden_clip_ids.__contains__,
        )
        self._video_stack.setCurrentIndex(0 if clips else 1)
        self._refresh_video_chips()
        self._refresh_video_actions()
        self._refresh_video_detail()
        self._refresh_section_state()

    def _refresh_video_chips(self) -> None:
        clips = getattr(self, "_video_entries", [])
        counts = {option[0]: 0 for option in _CLIP_FILTERS}
        counts["all"] = len(clips)
        for clip in clips:
            counts[clip.outcome] = counts.get(clip.outcome, 0) + 1
        self._video_chips.set_counts(counts)

    def _refresh_video_detail(self) -> None:
        if self._selected_pass_id:
            clip = self._clip_for_pass(self._selected_pass_id)
            if clip is not None:
                self._show_clip_detail(clip)
                self._video_detail.select_section(self._selected_pass_id)
                self._fill_section_detail(clip, self._selected_pass_id)
                return
            # The cut it was showing is gone, so nothing below the clip stands.
            self._selected_pass_id = None
        self._section_detail.setVisible(False)
        clip = self._selected_clip()
        if clip is None:
            self._video_detail.clear()
            self._video_detail_stack.setCurrentIndex(0)
            return
        self._show_clip_detail(clip)
        self._video_detail.select_section(None)

    def _show_clip_detail(self, clip: VideoLibraryEntry) -> None:
        self._video_detail_stack.setCurrentIndex(1)
        self._video_detail.show_entry(
            clip,
            self._transect_name_for,
            in_cart=self._cart_pass_ids().__contains__,
            # Hidden clips included: a chapter being out of the list does not
            # stop the section that spans it wanting a frame from it.
            assets={
                entry.video.id: entry.video
                for entry in getattr(self, "_video_entries", [])
            },
        )
        self._video_detail.set_archive_state(self._archive_state_for_video(clip.video.id))
        # First card of the session asks the registry; the answer repaints it.
        self._maybe_refresh_archive_badges()

    def _paint_archive_badges(self) -> None:
        """Repaint the archive badges from a fresh probe, on the cards showing."""
        detail = getattr(self, "_video_detail", None)
        if detail is not None and detail.entry is not None:
            detail.set_archive_state(self._archive_state_for_video(detail.entry.video.id))
        run_detail = getattr(self, "_run_detail", None)
        entry = run_detail.entry if run_detail is not None else None
        if run_detail is not None and entry is not None and entry.db_run is not None:
            run_detail.set_archive_state(self._archive_state_for_run(entry.db_run.id))

    def _selected_clip(self) -> VideoLibraryEntry | None:
        return self._clip_by_id(self._video_list.selected)

    def _clip_by_id(self, video_id: str | None) -> VideoLibraryEntry | None:
        if not video_id:
            return None
        for clip in getattr(self, "_video_entries", []):
            if str(clip.video.id) == video_id:
                return clip
        return None

    def _transect_name_for(self, transect_id) -> str | None:
        if transect_id is None:
            return None
        store = self._try_survey_store()
        if store is None:
            return None
        try:
            transect = store.get_transect(transect_id)
        except Exception:
            logger.exception("Could not read the transect for a section")
            return None
        return transect.name if transect is not None else None

    def _start_clip_link_scan(self) -> None:
        """Ask, off the paint path, whether each clip's file is still there.

        A stat is cheap until the drive is asleep or the mount is gone, and both
        are ordinary in the field, so this never runs on the GUI thread. Until it
        answers, a clip's state is "unknown" and no link icon is painted at all
        rather than a hopeful one.
        """
        if self._clip_link_scan_running:
            return
        paths = {clip.video.path for clip in getattr(self, "_video_entries", [])}
        todo = [p for p in paths if p not in self._clip_link_cache]
        if not todo:
            return
        self._clip_link_scan_running = True
        entries = list(self._video_entries)

        def worker() -> None:
            try:
                states = catalogue.resolve_link_states(entries)
            finally:
                # Cleared here rather than where the states are applied, so a
                # single-clip recheck arriving through the same signal cannot
                # report a scan finished that is still walking the list.
                self._clip_link_scan_running = False
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_clip_links_done.emit(states)

        threading.Thread(target=worker, daemon=True, name="clip-link-scan").start()

    def _recheck_clip_link(self, video_id: str) -> None:
        """Ask again, now, whether one clip's file is where the library says.

        The scan above answers a path once and keeps the answer for the session,
        which is right for a list of hundreds and wrong for the clip in front of
        you: drives are unplugged and plugged back in all day in the field, and
        the row a user just picked is exactly the one whose answer should be
        current. One stat, off the GUI thread like the rest.
        """
        clip = self._clip_by_id(video_id)
        if clip is None:
            return
        path = clip.video.path
        in_flight = self._clip_link_rechecking
        if path in in_flight:
            return
        in_flight.add(path)

        def worker() -> None:
            try:
                states = catalogue.resolve_link_states([clip])
            finally:
                in_flight.discard(path)
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_clip_links_done.emit(states)

        threading.Thread(target=worker, daemon=True, name="clip-link-recheck").start()

    def _apply_clip_link_states(self, states: dict) -> None:
        self._clip_link_cache.update(states)
        for clip in getattr(self, "_video_entries", []):
            clip.link_state = self._clip_link_cache.get(clip.video.path, catalogue.LINK_UNKNOWN)
        self._rebuild_video_list()
        self._refresh_storage_bars()

    def _repair_video_identity(self, store) -> None:
        """Give each clip one row, its hash, and a reading of its container.

        Inline rather than on a worker thread, beside the rebuild_from_scan that
        already reads every manifest: it runs once per root, only touches rows
        that are missing something, and both imohash and the container read
        sample a file rather than reading it through, so the cost is a stat and a
        few seeks per clip. A thread would buy little and would put a second
        writer on a database the window is reading, which SurveyStore's
        thread-local connections make exactly the kind of thing worth not having.
        """
        from deepreefmap_gui.survey.video_repair import repair_video_identity

        try:
            report = repair_video_identity(store)
        except Exception:
            logger.exception("Could not repair the video library")
            return
        if report.summary():
            self._status_label.setText(report.summary())

    # --- filters -------------------------------------------------------------

    def _on_video_period_changed(self, key: str) -> None:
        self._video_period = key
        # A reader's preference on this machine, not a fact about the survey, so
        # it goes to QSettings rather than the database.
        self._settings.setValue("video_group_period", key)
        self._rebuild_video_list()

    def _on_video_filter_changed(self, key: str) -> None:
        self._video_clip_filter = key
        self._rebuild_video_list()

    def _on_video_sort_changed(self, column: str, descending: bool) -> None:
        self._video_sort_column = column
        self._video_sort_descending = descending
        # A reader's preference on this machine, beside the grouping period.
        self._settings.setValue("video_sort_column", column)
        self._settings.setValue(
            "video_sort_order", _ORDER_DESCENDING if descending else _ORDER_ASCENDING
        )
        self._rebuild_video_list()

    # --- one clip ------------------------------------------------------------

    def _on_video_activated(self, video_id: str) -> None:
        self._video_list.set_selected(video_id)
        self._selected_pass_id = None
        self._section_detail.setVisible(False)
        self._refresh_video_detail()
        # The clip a user just picked is the one they are about to play, cut or
        # relocate, so its link state is worth a fresh stat.
        self._recheck_clip_link(video_id)

    def _select_section(self, pass_id: str) -> bool:
        """Show one cut, wherever it was picked: the tree, the clip card, the strip.

        One entry point for all three so they cannot disagree about what is
        selected, and so a span click opens the clip it belongs to.

        Answers False for a section that is no longer there, which is how going
        back to a deleted one is told from going back to a live one.
        """
        clip = self._clip_for_pass(pass_id)
        if clip is None:
            return False
        self._selected_pass_id = pass_id
        self._video_list.expand(str(clip.video.id))
        self._video_list.set_selected_section(pass_id)
        # Through the one filler, so the pane highlights the same row the list
        # does and reads the cart the same way it does.
        self._show_clip_detail(clip)
        self._video_detail.select_section(pass_id)
        self._fill_section_detail(clip, pass_id)
        self._video_list.reveal(pass_id)
        return True

    def _open_section_in_videos(self, pass_id: uuid.UUID) -> None:
        """Edit a section where a section is defined.

        The cart shows a pass's transect, direction and trim but no longer edits
        any of them: they describe the swim, not the plan to process it, and
        they are set here. Clicking one over there lands on it here.
        """
        self._go_to_section("videos")
        if not self._select_section(str(pass_id)):
            # The section has gone since whatever offered it was drawn, which a
            # chart or a cart row outliving a delete is how it happens.
            self._status_label.setText("That section is no longer in the survey.")

    def _pass_by_id(self, store, pass_id: str):
        """The section an id names, and None when the id names nothing.

        The ids come off row widgets, which is a wide enough door that a
        malformed one has to be an answer of None rather than an exception in
        front of the user.
        """
        try:
            wanted = uuid.UUID(str(pass_id))
        except (ValueError, TypeError, AttributeError):
            return None
        return store.get_pass(wanted)

    def _clip_for_pass(self, pass_id: str) -> VideoLibraryEntry | None:
        for clip in getattr(self, "_video_entries", []):
            if any(str(p.id) == pass_id for p in clip.passes):
                return clip
        return None

    def _fill_section_detail(self, clip: VideoLibraryEntry, pass_id: str) -> None:
        pass_ = next((p for p in clip.passes if str(p.id) == pass_id), None)
        if pass_ is None:
            self._section_detail.setVisible(False)
            return
        runs = [r for r in clip.runs if str(r.pass_id) == pass_id]
        sizes = getattr(self, "_run_size_cache", {})
        self._section_detail.show_section(
            pass_,
            clip_name=clip.video.file_name,
            transect_name=self._transect_name_for(pass_.transect_id),
            status=pass_status(runs),
            runs=runs,
            session_name=self._session_name_for,
            in_cart=self._pass_in_current_cart(pass_id),
            output_bytes=sum(sizes.get(r.run_dir_name, 0) for r in runs),
        )
        self._section_detail.setVisible(True)

    def _session_name_for(self, batch_id) -> str | None:
        if batch_id is None:
            return None
        store = self._try_survey_store()
        if store is None:
            return None
        try:
            batch = store.get_batch(batch_id)
        except Exception:
            logger.exception("Could not read the session for a run")
            return None
        return batch.name if batch is not None else None

    def _on_video_add_clicked(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add videos",
            str(self._settings.value("last_video_dir", "")),
            "Videos (*.mp4 *.mov *.avi *.mkv);;All files (*)",
        )
        if paths:
            # Footage arrives a card at a time, so the next batch of clips is
            # almost always in the folder the last one came from.
            self._settings.setValue("last_video_dir", str(Path(paths[0]).parent))
        self._add_video_paths(paths)

    def _on_video_new_section(self) -> None:
        clip = self._video_detail.entry
        if clip is not None:
            self._new_section_from_clip(clip)

    def _on_video_new_section_for(self, video_id: str) -> None:
        clip = self._clip_by_id(video_id)
        if clip is not None:
            self._new_section_from_clip(clip)

    def _new_section_from_clip(self, clip: VideoLibraryEntry) -> None:
        """Cut a section from a clip and file it against a transect.

        The cart is not touched: making a section and choosing to run it are two
        decisions, and the cart control on the row is where the second is made.

        Scrub first (the window is the section's identity), then the assign step,
        where a transect is a choice rather than a requirement. A clip that
        cannot be scrubbed cuts nothing: a section is a window the user chose,
        never a whole clip queued on their behalf.
        """
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        duration = clip.video.duration_s or 0.0
        if clip.link_state == LINK_MISSING:
            self._status_label.setText(
                "Cannot cut a section: the video file is missing. Add it again "
                "from where it lives now."
            )
            return
        if clip.link_state != LINK_LINKED:
            self._status_label.setText(
                "Cannot cut a section: the video file has not been checked yet."
            )
            return
        if duration <= 0.0:
            self._status_label.setText(
                "Cannot cut a section: the clip's length is unknown."
            )
            return
        store = self._try_survey_store()
        if store is None:
            self._status_label.setText(
                "Cannot cut a section: the survey database is unavailable."
            )
            return
        scrub = VideoScrubDialog(
            clip.video.path, duration, 0.0, duration, parent=self, fps=clip.video.fps
        )
        if scrub.exec() != QDialog.DialogCode.Accepted:
            return
        begin_s, end_s = scrub.time_range()
        # Two sections over the same footage would produce two runs of one swim
        # that nothing tells apart, and it is how a clip ends up with a spurious
        # whole-length section beside the real one.
        already = store.pass_with_window(clip.video.id, begin_s, end_s)
        if already is not None:
            self._status_label.setText(
                f"{clip.video.file_name} already has a section over that window."
            )
            self._select_section(str(already.id))
            return
        assign = self._transect_picker(store)
        accepted = assign.exec() == QDialog.DialogCode.Accepted
        # Leaving for the Transects page is not cancelling: the window is already
        # chosen, so the section is written unfiled and filed later.
        if not accepted and not assign.left_for_page:
            return
        transect_id, direction = assign.choice() if accepted else (None, "forward")
        # The dialog's pick when it was answered; the survey's remembered
        # default when the section is written unfiled on the way out.
        campaign_id = assign.campaign_choice() if accepted else store.default_campaign_id()
        pass_ = TransectPass(
            transect_id=transect_id,
            video_id=clip.video.id,
            begin_s=begin_s,
            end_s=end_s,
            direction=direction,
            campaign_id=campaign_id,
        )
        store.add_pass(pass_)
        self._refresh_video_library()
        if accepted:
            self._select_section(str(pass_.id))

    def _cart_pass_ids(self) -> set[str]:
        """The current cart's passes, read once per repaint.

        Asked per row it was two queries a section, which is a query per section
        of the whole library every time the page repaints.
        """
        store = self._try_survey_store()
        if store is None:
            return set()
        try:
            cart = store.current_cart()
            if cart is None:
                return set()
            return {str(item.pass_id) for item in store.list_batch_items(cart.id)}
        except Exception:
            logger.exception("Could not read the cart")
            return set()

    def _pass_in_current_cart(self, pass_id_str: object) -> bool:
        return str(pass_id_str) in self._cart_pass_ids()

    def _missing_clips_for(self, pass_) -> list[str]:
        """The chapters of a section whose files the last scan could not find.

        Read off the library's cached link states rather than by stat'ing here:
        the answer is already known, and a drive that has gone to sleep must not
        be woken on the thread that paints the window.
        """
        names = []
        for video_id in pass_.video_ids():
            clip = self._clip_by_id(str(video_id))
            if clip is not None and clip.link_state == LINK_MISSING:
                names.append(clip.video.file_name)
        return names

    def _refresh_cart_marks(self) -> None:
        """Repaint the Videos page after the cart changed somewhere else.

        The cart is shown on every section row, so a pass taken out of it on the
        Process page has to stop claiming to be in it here. Cheap: the library
        itself is not re-read, only the rows re-filled from what is cached.
        """
        if getattr(self, "_video_list", None) is None:
            return
        self._rebuild_video_list()

    def _on_video_pass_to_cart(self, pass_id_str: str) -> None:
        """The cart control on a section, from a row or the pane's list.

        One control, both ways: a section that is not in the cart goes in, and
        one that is comes out. The button is green while it is in, so clicking
        the green is how a section is taken back out again.
        """
        try:
            pass_id = uuid.UUID(pass_id_str)
        except (ValueError, AttributeError, TypeError):
            return
        store = self._try_survey_store()
        if store is None:
            self._status_label.setText("Survey database unavailable; cannot queue.")
            return
        pass_ = store.get_pass(pass_id)
        if pass_ is None:
            return
        if pass_id_str in self._cart_pass_ids():
            self._take_pass_out_of_cart(pass_id)
            self._status_label.setText(
                f"Took the {section_window(pass_)} section out of the cart."
            )
            return
        # A section whose footage is not on disk would fail the moment the cart
        # was checked out, so it never gets in. Refused here rather than at the
        # run, where a whole session stops for one clip on an unplugged drive.
        missing = self._missing_clips_for(pass_)
        if missing:
            self._status_label.setText(
                f"{', '.join(missing)} cannot be found, so this section cannot be "
                "processed. Add the footage again from where it lives now."
            )
            return
        self._add_pass_to_cart(pass_id)

    def _on_section_retrim(self, pass_id: str) -> None:
        """Move a section's window. The trim is metadata, so it stays editable.

        A section that has already run keeps its runs: each one recorded the
        window it actually processed, so the history stays true even once the
        section is asking for a different one next time.
        """
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        clip = self._clip_for_pass(pass_id)
        store = self._try_survey_store()
        if clip is None or store is None:
            return
        pass_ = self._pass_by_id(store, pass_id)
        if pass_ is None:
            return
        duration = clip.video.duration_s or 0.0
        if clip.link_state != LINK_LINKED or duration <= 0.0:
            self._status_label.setText(f"{clip.video.file_name} cannot be scrubbed.")
            return
        dialog = VideoScrubDialog(
            clip.video.path,
            duration,
            pass_.begin_s,
            pass_.end_s,
            parent=self,
            fps=clip.video.fps,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        pass_.begin_s, pass_.end_s = dialog.time_range()
        store.update_pass(pass_)
        self._refresh_video_library()
        self._select_section(pass_id)
        # A cart row is this section, not a copy of it: the cart shows the new
        # window, and the run made from it processes the new window too.
        self._refresh_survey_batch_tab()

    def _transect_picker(self, store, **kwargs):
        """The map-and-list dialog both filing steps use, wired to the page.

        Its arrow leaves for the Transects page, rejecting the dialog on the way
        so no choice made in it is half applied. Whether the section survives the
        trip is the caller's business: a section being reassigned already exists,
        and one being cut is written unfiled rather than thrown away.
        """
        from deepreefmap_gui.simple.transect_picker import TransectPickerDialog

        dialog = TransectPickerDialog(self, store, **kwargs)
        dialog.open_transect_requested.connect(self._open_transect_page)
        return dialog

    def _on_section_reassign(self, pass_id: str) -> None:
        """Change which transect a section belongs to, or its direction."""
        store = self._try_survey_store()
        if store is None:
            return
        pass_ = self._pass_by_id(store, pass_id)
        if pass_ is None:
            return
        dialog = self._transect_picker(
            store,
            transect_id=pass_.transect_id,
            direction=pass_.direction,
            ok_label="Save",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        pass_.transect_id, pass_.direction = dialog.choice()
        store.update_pass(pass_)
        self._refresh_video_library()
        self._select_section(pass_id)
        self._refresh_survey_batch_tab()

    def _on_section_delete(self, pass_id: str) -> None:
        """Remove a section, unless something was made from it.

        A section with runs is the record of what those runs processed, so it
        cannot quietly disappear from under them. Deleting the runs is Browse's
        job, and doing it there leaves the section deletable here.
        """
        store = self._try_survey_store()
        if store is None:
            return
        pass_ = self._pass_by_id(store, pass_id)
        if pass_ is None:
            return
        runs = store.runs_for_pass(pass_.id)
        if runs:
            count = f"{len(runs)} run{'' if len(runs) == 1 else 's'}"
            self._status_label.setText(
                f"This section has {count}. Delete them in Browse first."
            )
            return
        if not confirm(
            self,
            "Delete section",
            f"Remove the {section_window(pass_)} section from "
            f"{self._video_detail.entry.video.file_name if self._video_detail.entry else 'this clip'}?",
        ):
            return
        try:
            store.delete_pass(pass_.id)
        except ValueError as exc:
            self._status_label.setText(str(exc))
            return
        self._selected_pass_id = None
        self._section_detail.setVisible(False)
        self._refresh_video_library()
        self._refresh_survey_batch_tab()
        self._status_label.setText("Section deleted.")

    def _on_section_run_activated(self, run_dir_name: str) -> None:
        """Open what this section produced, the same as Open does in Browse."""
        root = Path(self._out_root_input.text()).expanduser()
        self._load_run_from_dir(root / run_dir_name)

    def _on_video_delete_unused(self, video_id: str) -> None:
        """Drop every section of a clip that nothing was ever made from.

        The same rule ``_on_section_delete`` applies one at a time: a section
        with runs is the record of what those runs processed and stays. What
        this is for is a clip carrying sections nobody cut on purpose, where
        deleting them one by one is the only thing standing in the way.
        """
        store = self._try_survey_store()
        clip = self._clip_by_id(video_id)
        if store is None or clip is None:
            return
        doomed = [p for p in clip.passes if not store.runs_for_pass(p.id)]
        if not doomed:
            self._status_label.setText("Every section of this clip has runs.")
            return
        if not confirm(
            self,
            "Delete sections",
            f"Remove {_sections_phrase(len(doomed))} from {clip.video.file_name}? "
            "Nothing has been made from them.",
        ):
            return
        removed = 0
        for pass_ in doomed:
            try:
                store.delete_pass(pass_.id)
            except ValueError as exc:
                logger.warning("Could not delete section %s: %s", pass_.id, exc)
                continue
            removed += 1
        self._selected_pass_id = None
        self._section_detail.setVisible(False)
        self._refresh_video_library()
        self._refresh_survey_batch_tab()
        self._status_label.setText(f"Deleted {_sections_phrase(removed)}.")

    def _on_video_delete(self, video_id: str) -> None:
        """The trash on one clip's row, which its own second press has armed."""
        clip = self._clip_by_id(video_id)
        if clip is not None:
            self._delete_clips([clip])

    def _delete_clips(self, clips: list[VideoLibraryEntry]) -> None:
        """Take clips out of the library, with the sections cut from them.

        The record only: the footage stays where it is, and adding the file
        again brings the clip back. A run is the record of what it processed,
        so a clip anything was made from stays until Browse has taken those
        runs, the same rule ``_on_section_delete`` applies one level down.

        The store refuses a clip a chaptered pass reaches from another clip too,
        so every refusal is counted and reported rather than assumed away.
        """
        store = self._try_survey_store()
        if store is None or not clips:
            return
        deleted: list[VideoLibraryEntry] = []
        refused = 0
        for clip in clips:
            if clip.runs:
                refused += 1
                continue
            try:
                store.delete_video(clip.video.id)
            except ValueError as exc:
                logger.warning("Could not delete clip %s: %s", clip.video.id, exc)
                refused += 1
                continue
            deleted.append(clip)
        if deleted:
            # A hidden clip that no longer exists would keep counting towards
            # "Show hidden (n)" forever: nothing else prunes that list.
            gone = {str(clip.video.id) for clip in deleted}
            if gone & self._hidden_clip_ids:
                self._hidden_clip_ids -= gone
                self._save_hidden_clips()
            self._video_list.set_selected(None)
            self._selected_pass_id = None
            self._section_detail.setVisible(False)
        self._refresh_video_library()
        self._refresh_survey_batch_tab()
        self._status_label.setText(_delete_report(deleted, refused))

    def _on_video_reveal(self, video_id: str) -> None:
        """Show the clip itself in the file manager, selected rather than merely near.

        Rechecks the link on the way: the folder is where you look when a clip
        has gone missing, and the answer is often that it is back.
        """
        clip = self._clip_by_id(video_id)
        if clip is None:
            return
        self._recheck_clip_link(video_id)
        if not reveal_in_file_manager(Path(clip.video.path)):
            self._status_label.setText("The file manager could not be opened.")

    def _on_video_play(self, video_id: str) -> None:
        clip = self._clip_by_id(video_id)
        if clip is not None:
            self._play_clip(clip)

    def _play_clip(self, clip: VideoLibraryEntry) -> None:
        """Play the footage. The trim dialog already decodes and seeks this file."""
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        if clip.link_state != LINK_LINKED:
            self._status_label.setText(f"{clip.video.file_name} cannot be found.")
            return
        duration = clip.video.duration_s or 0.0
        dialog = VideoScrubDialog(
            clip.video.path,
            duration,
            0.0,
            duration,
            self,
            fps=clip.video.fps,
            trim=False,
        )
        dialog.exec()

    # A clip whose file has moved is not relocated from here: Add videos… on the
    # file's new home matches it by checksum and repoints the clip it already
    # has, sections and runs included. One way in for footage, and no second
    # flow whose only job is to check the two files are the same one.
