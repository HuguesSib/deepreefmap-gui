"""Browse: every run in the output root, by run, session, or transect."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import direction_arrow_icon
from deepreefmap_gui.core.theme import (
    GUTTER,
    PRIMARY,
    SPACE_SM,
    SPLIT_MIN_TOTAL,
)
from deepreefmap_gui.core.widgets import (
    SCOPE_FILTERS,
    STATUS_COLORS,
    EmptyState,
    FilterChips,
    secondary_label,
    section_column,
    segmented_qss,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.io.video_files import find_videos, is_run_dir
from deepreefmap_gui.map.overlays import transect_overlays
from deepreefmap_gui.map.slippy_map import SlippyMapWidget
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.delete_data_dialog import (
    DeleteChoice,
    DeleteDataDialog,
    DeleteScope,
)
from deepreefmap_gui.runs.run_cards import points_label, related_run_counts
from deepreefmap_gui.runs.run_detail import RunDetailPanel
from deepreefmap_gui.runs.run_table import COL_NAME, RunTable
from deepreefmap_gui.runs.session_detail import SessionDetailPanel
from deepreefmap_gui.runs.transect_detail import TransectDetailPanel
from deepreefmap_gui.survey import catalogue, statuses
from deepreefmap_gui.survey.catalogue import (
    FacetGroup,
    RunEntry,
)
from deepreefmap_gui.survey.models.transect_pass import PASS_DIRECTIONS

logger = logging.getLogger(__name__)

# How the archive is arranged, in widening order: one run, one day, one line.
# Keys are persisted and test-pinned; only the labels say what each view does.
#
# By session is the container that spans transects. Every run already records
# the session it was queued in, on its pass and in its manifest, but until this
# facet existed there was nowhere for a day's work to appear as a day's work,
# and "run" was left doing double duty for both the queue and its output.
_FACETS = (
    ("runs", "All runs"),
    ("sessions", "By session"),
    ("transects", "By transect"),
)

# Facets whose left rail groups runs into a tree. "runs" has no grouping, so its
# rail is hidden.
_GROUPED_FACETS = ("sessions", "transects")

_RAIL_TITLES = {"sessions": "Sessions", "transects": "Transects"}

# Outcome filters over the listed runs. Counts come from the scan, so a chip
# reading "Failed 3" answers the question without being clicked.
# Built from the status table, so no bucket exists without a filter reaching it.
# Each carries the colour its outcome is painted in everywhere else.
_OUTCOME_TITLES = {
    statuses.OUTCOME_SUCCEEDED: "Completed",
    statuses.OUTCOME_FAILED: "Failed",
    statuses.OUTCOME_UNFINISHED: "Unfinished",
}
_STATUS_FILTERS = (
    ("all", "All", PRIMARY),
    *(
        (outcome, _OUTCOME_TITLES[outcome], STATUS_COLORS[outcome_status])
        for outcome, outcome_status in (
            (statuses.OUTCOME_SUCCEEDED, "succeeded"),
            (statuses.OUTCOME_FAILED, "failed"),
            (statuses.OUTCOME_UNFINISHED, "interrupted"),
        )
    ),
)

_SCOPE_TOOLTIP = (
    "In view lists only the runs assigned to a transect the map is showing, "
    "and follows the map as it is panned and zoomed. Runs with no transect are "
    "not on the map, so they appear under All transects. Either chip releases a "
    "transect picked in the list."
)

# Right-pane pages inside the runs stack.
_RUN_LIST_PAGE, _EMPTY_PAGE = 0, 1

# Detail pane pages: nothing selected, a run, a transect, a session.
_DETAIL_EMPTY, _DETAIL_RUN, _DETAIL_TRANSECT, _DETAIL_SESSION = range(4)

_GROUP_KEY_ROLE = Qt.ItemDataRole.UserRole

# Wide enough for a transect or session name to survive elision. Down from 300
# once the rail stopped being a card and the tree stopped indenting 20px a
# level: 240 now shows more of a name than 300 did, and the 60px is what lets
# the run table hold nine columns without a horizontal scrollbar.
_RAIL_WIDTH = 240

# Below this the rail is not showing names any more, so a remembered width this
# small is a layout artefact rather than a choice the user made.
_RAIL_MIN_WIDTH = 200

# Tall enough to hold a transect and the water around it; the list below it is
# what grows when the rail is dragged wider.
_RAIL_MAP_HEIGHT = 240
_RAIL_MAP_MIN_HEIGHT = 140

# How much of the space left over from the rail the detail pane takes. The run
# table is the page and gets the rest; the pane holds one run's facts and its
# ortho strip, which is not a 50/50 amount of content.
_DETAIL_SHARE = 0.28

# Narrow enough that the proportion still holds on a laptop screen, where
# the rail and the table have already taken their share.
_DETAIL_MIN_WIDTH = 260


def _deleted_summary(data_gone: int, records_gone: int) -> str:
    parts = []
    if data_gone:
        parts.append(f"the data of {data_gone} run{'s' if data_gone != 1 else ''}")
    if records_gone:
        parts.append(f"{records_gone} record{'s' if records_gone != 1 else ''}")
    return f"Deleted {' and '.join(parts)}."


def _key_transect_id(key: tuple | None) -> uuid.UUID | None:
    """The transect a facet key names, when it names one by id.

    A run carrying a transect name but no id is filed under a key holding that
    name (``catalogue.transects_facet``), so the second element is not always
    parseable. Such a group still lists its runs; it just has nothing the rest
    of the survey can be pointed at.
    """
    if key is None or len(key) != 2 or key[0] != "transect":
        return None
    try:
        return uuid.UUID(str(key[1]))
    except ValueError:
        return None


def _entries_in_view(entries: list[RunEntry], visible: frozenset[str]) -> list[RunEntry]:
    """The runs assigned to one of the transects the map is showing.

    A run with no transect is nowhere on the map, so it is not in view either.
    """
    return [e for e in entries if e.transect_id is not None and str(e.transect_id) in visible]


class BrowseMixin(MixinBase):
    """DeepReefMapWindow methods for Browse: the run archive and its detail pane."""

    _data_facet: str = "runs"
    _data_status_filter: str = "all"
    _data_scope_filter: str = "in_view"
    _data_visible_ids: frozenset[str] | None = None
    _data_selected_key: tuple | None = None
    _data_rebuilt_root: Path | None = None
    _data_rail_shown: bool | None = None
    _data_split_user_sized: bool = False
    _data_split_applying: bool = False

    def _build_data_panel(self) -> QWidget:
        self._data_entries: list[RunEntry] = []
        # Keyed by facet key, holding the group itself rather than only its
        # entries: the session pane describes the group (its name, what it
        # covered), so a second dict of groups beside this one would be one
        # more pair of things to keep in step.
        self._data_groups: dict[tuple, FacetGroup] = {}
        self._run_size_cache: dict[str, int] = {}
        # Cached sizes due a re-measure. Held apart from the cache so the last
        # known number stays on screen while the new one is being counted.
        self._run_size_stale: set[str] = set()
        self._data_sizes_scan_running = False

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        # Facets and the disk total sit above the split, so neither disappears
        # with the rail when a facet has no grouping to show.
        top_row = QHBoxLayout()
        top_row.setSpacing(GUTTER)
        facet_row = QHBoxLayout()
        facet_row.setSpacing(0)
        group = QButtonGroup(panel)
        group.setExclusive(True)
        self._data_facet_buttons: dict[str, QToolButton] = {}
        for index, (name, title) in enumerate(_FACETS):
            btn = QToolButton()
            btn.setText(title)
            btn.setCheckable(True)
            # One joined control, so it reads as three views of the same data
            # rather than three unrelated buttons.
            btn.setStyleSheet(
                segmented_qss(first=index == 0, last=index == len(_FACETS) - 1)
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(btn)
            facet_row.addWidget(btn)
            btn.toggled.connect(
                lambda checked, n=name: self._on_data_facet_changed(n) if checked else None
            )
            self._data_facet_buttons[name] = btn
        top_row.addWidget(QLabel("Group"))
        top_row.addLayout(facet_row)
        self._data_search = QLineEdit()
        self._data_search.setPlaceholderText("Search runs…")
        self._data_search.setClearButtonEnabled(True)
        self._data_search.setMaximumWidth(240)
        self._data_search.textChanged.connect(lambda *_: self._rebuild_data_run_list())
        top_row.addWidget(self._data_search)
        self._data_status_chips = FilterChips(_STATUS_FILTERS)
        self._data_status_chips.changed.connect(self._on_data_status_filter_changed)
        top_row.addWidget(self._data_status_chips)
        # Last on the row, because it only means something where there is a map
        # to move: it comes and goes with the transect grouping, and stays put
        # for the whole of it.
        self._data_scope_chips = FilterChips(SCOPE_FILTERS)
        self._data_scope_chips.setToolTip(_SCOPE_TOOLTIP)
        self._data_scope_chips.changed.connect(self._on_data_scope_filter_changed)
        self._data_scope_chips.setVisible(False)
        top_row.addWidget(self._data_scope_chips)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # Disk sits with the group header rather than on the filter row: the
        # filters already fill that row, and squeezing a growing byte count in
        # beside them clipped it on a narrow window.
        self._data_disk_label = secondary_label("")

        self._data_split = QSplitter(Qt.Orientation.Horizontal)
        self._data_split.setHandleWidth(SPACE_SM)

        # "All runs" has no grouping, so the whole rail goes rather than leaving
        # a dead column.
        #
        # A column rather than a card: the rail and the table are the page, and
        # only the detail pane describes a single thing. Three cards side by
        # side made the raised fill the dominant surface, so nothing said which
        # pane to read first.
        self._data_rail, rail_layout = section_column("Group")
        self._data_tree = QTreeWidget()
        self._data_tree.setHeaderHidden(True)
        # Fusion indents 20px a level, and the rail is only 240px wide, so every
        # pixel of indent is a pixel of name that stops eliding. 8px still
        # leaves the disclosure arrow a readable offset.
        self._data_tree.setIndentation(SPACE_SM)
        self._data_tree.setUniformRowHeights(True)
        self._data_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._data_tree.itemSelectionChanged.connect(self._on_data_tree_selection)
        # Clicks as well as selection changes: clicking the row that is already
        # selected changes no selection and emits nothing, so a run picked in
        # the table since could not be got out of the detail pane by pointing at
        # the transect again.
        self._data_tree.itemClicked.connect(lambda *_: self._on_data_tree_selection())
        self._data_tree_stack = QStackedWidget()
        self._data_tree_stack.addWidget(self._data_tree)
        self._data_tree_stack.addWidget(EmptyState("Nothing to group yet"))

        # Transects are places before they are rows, so the rail shows where they
        # are and the list underneath stays for the ones a map cannot hold: the
        # unassigned bucket, and any transect with no coordinates yet.
        self._data_map = SlippyMapWidget()
        self._data_map.setMinimumHeight(_RAIL_MAP_MIN_HEIGHT)
        self._data_map.transect_clicked.connect(self._on_data_map_transect_clicked)
        # Coalesced, because a drag emits a view change per mouse move and each
        # one would otherwise rebuild the table under the cursor.
        self._data_view_timer = QTimer(self)
        self._data_view_timer.setSingleShot(True)
        self._data_view_timer.setInterval(60)
        self._data_view_timer.timeout.connect(self._apply_data_view_change)
        self._data_map.view_changed.connect(self._data_view_timer.start)
        rail_split = QSplitter(Qt.Orientation.Vertical)
        rail_split.setHandleWidth(SPACE_SM)
        rail_split.addWidget(self._data_map)
        rail_split.addWidget(self._data_tree_stack)
        rail_split.setStretchFactor(0, 0)
        rail_split.setStretchFactor(1, 1)
        rail_split.setSizes([_RAIL_MAP_HEIGHT, 400])
        self._data_rail_split = rail_split
        rail_layout.addWidget(rail_split, 1)
        self._data_rail.setMinimumWidth(_RAIL_MIN_WIDTH)
        self._data_split.addWidget(self._data_rail)

        runs_card, runs_layout = section_column()
        header_row = QHBoxLayout()
        self._data_group_header = secondary_label("")
        self._data_group_header.setWordWrap(True)
        header_row.addWidget(self._data_group_header, 1)
        header_row.addWidget(self._data_disk_label)
        runs_layout.addLayout(header_row)

        self._data_run_table = RunTable()
        self._data_run_table.itemDoubleClicked.connect(self._on_data_run_activated)
        self._data_run_table.itemSelectionChanged.connect(self._update_data_actions)
        self._data_run_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._data_run_table.customContextMenuRequested.connect(self._on_data_context_menu)

        self._data_run_stack = QStackedWidget()
        self._data_run_stack.addWidget(self._data_run_table)
        self._data_empty_state = EmptyState("No runs here yet", "Processed passes collect here.")
        self._data_run_stack.addWidget(self._data_empty_state)
        runs_layout.addWidget(self._data_run_stack, 1)

        # Two actions and a menu, rather than a row of six mostly-disabled
        # buttons: opening and finding a run are what you do constantly, the
        # rest are occasional housekeeping.
        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        self._data_open_btn = QPushButton("Open")
        self._data_open_btn.clicked.connect(self._on_data_open_clicked)
        actions.addWidget(self._data_open_btn)
        self._data_show_btn = QPushButton("Show in folder")
        self._data_show_btn.clicked.connect(self._on_data_show_in_folder_clicked)
        actions.addWidget(self._data_show_btn)
        self._data_more_btn = QToolButton()
        self._data_more_btn.setText("More…")
        self._data_more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self._data_more_btn)
        # The whole list, including the two beside it as buttons. A menu that
        # offers only the leftovers makes you learn which half is where, and the
        # right-click menu is built from the same list so neither can go stale.
        self._data_more_actions = self._fill_data_row_actions(more_menu)
        more_menu.addSeparator()
        # Housekeeping that belongs to the panel, not to a selection: a run
        # folder from anywhere on disk, and a re-read of the output root.
        more_menu.addAction("Open run folder…", self._on_data_open_folder_clicked)
        more_menu.addAction("Rescan output folder", self._on_data_rescan_clicked)
        self._data_more_btn.setMenu(more_menu)
        actions.addWidget(self._data_more_btn)
        actions.addStretch(1)
        runs_layout.addLayout(actions)

        # A shared drop handler queues dropped videos as passes and opens a
        # dropped run folder. The stacks, not the views inside them: with nothing
        # listed both swap to an empty state, and a hidden view takes no drops.
        for widget in (self._data_run_stack, self._data_tree_stack):
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)
        self._data_split.addWidget(runs_card)

        # One detail pane, showing whichever kind of thing is selected. Transect
        # analysis lives here rather than under the list, so nothing
        # transect-shaped appears while you are grouped by run.
        self._data_detail_stack = QStackedWidget()
        self._data_detail_stack.addWidget(
            EmptyState("Nothing selected", "Pick a run or a transect to see its detail.")
        )
        self._run_detail = RunDetailPanel()
        self._run_detail.cover.set_classes_config(self._classes_config)
        # Opening the selected run is the pane's own primary action, beside the
        # small Open under the table: the pane is where you decide, so the
        # button belongs where the deciding happens.
        self._run_detail.set_open_action_visible(True)
        self._run_detail.open_requested.connect(self._on_data_open_clicked)
        self._run_detail.log_requested.connect(self._show_run_log)
        self._run_detail.rename_requested.connect(self._on_data_rename_clicked)
        self._run_detail.archive_requested.connect(self._archive_run)
        self._data_detail_stack.addWidget(self._run_detail)

        # A summary and a way through, not a second chart. The transect's cover
        # and repeatability are built once, on the Transects page.
        self._transect_detail = TransectDetailPanel()
        self._transect_detail.open_transect_requested.connect(self._on_data_open_transect)
        self._data_detail_stack.addWidget(self._transect_detail)
        self._session_detail = SessionDetailPanel()
        self._session_detail.audit_requested.connect(self._on_data_session_audit)
        self._session_detail.delete_requested.connect(self._on_data_session_delete)
        self._data_detail_stack.addWidget(self._session_detail)
        self._data_detail_stack.setMinimumWidth(_DETAIL_MIN_WIDTH)
        self._data_split.addWidget(self._data_detail_stack)

        self._data_split.setStretchFactor(0, 0)
        self._data_split.setStretchFactor(1, 7)
        self._data_split.setStretchFactor(2, 3)
        self._data_split.splitterMoved.connect(self._on_data_split_moved)
        self._data_split.installEventFilter(self)
        self._apply_data_split_sizes(rail_visible=False)
        layout.addWidget(self._data_split, 1)
        self._update_data_actions()

        # Debounced refresh: the out-root watcher fires for every file touched
        # at the top level during a run (survey.db-wal churn included). Sizes
        # are re-measured because a running job grows its directory.
        self._data_refresh_timer = QTimer(self)
        self._data_refresh_timer.setSingleShot(True)
        self._data_refresh_timer.setInterval(300)
        self._data_refresh_timer.timeout.connect(self._on_data_watch_refresh)

        self._data_panel = panel
        self._data_facet_buttons["runs"].setChecked(True)
        return panel

    def _request_data_refresh(self) -> None:
        if hasattr(self, "_data_refresh_timer"):
            self._data_refresh_timer.start()

    def _data_out_root(self) -> Path:
        return Path(self._out_root_input.text()).expanduser()

    def _refresh_data_manager(self) -> None:
        if not hasattr(self, "_out_root_input") or not hasattr(self, "_data_panel"):
            return
        root = self._data_out_root()
        entries = catalogue.scan_out_root(root)
        # _try_survey_store records why it could not open in _survey_health,
        # which is what the readiness row and the notification centre read. The except
        # below stays for what can go wrong after a successful open.
        store = self._try_survey_store() if root.is_dir() else None
        if store is not None:
            try:
                if self._data_rebuilt_root != root:
                    store.rebuild_from_scan(root)
                    self._repair_video_identity(store)
                    self._data_rebuilt_root = root
                    # A different root is a different survey, so the map earns a
                    # fresh fit rather than keeping the last one's viewport.
                    self._data_map_fitted = False
                # Crashed runs never wrote a manifest, so scan_out_root skips
                # them; surface them here so they can be seen and cleared.
                entries += catalogue.scan_incomplete_runs(
                    root, store, {e.dir_name for e in entries}
                )
                # And the inverse: records whose folder is gone. The history
                # outlives the outputs, so these still earn a row.
                entries += catalogue.missing_run_entries(
                    root, store, {e.dir_name for e in entries}
                )
                entries.sort(key=lambda e: e.sort_key, reverse=True)
                catalogue.reconcile(entries, store)
            except Exception:
                logger.exception("Survey database unavailable for %s", root)
                store = None
        self._data_entries = entries
        self._data_store_ok = store is not None
        self._refresh_video_library(store)
        live = {e.dir_name for e in entries}
        for name in [n for n in self._run_size_cache if n not in live]:
            del self._run_size_cache[name]
        self._run_size_stale &= live
        for entry in entries:
            entry.size_bytes = self._run_size_cache.get(entry.dir_name)
        self._rebuild_data_tree()
        self._update_data_disk_label()
        self._start_data_size_scan()
        # Guarded: this runs during form construction, before the simple shell
        # that owns the header exists.
        if hasattr(self, "_simple_nav_buttons"):
            self._refresh_browse_state()

    def _on_data_watch_refresh(self) -> None:
        # A run in progress grows its folder, so every size is due a re-measure;
        # marking them stale rather than dropping them keeps the count that is
        # already on screen there until the new one arrives.
        self._run_size_stale.update(self._run_size_cache)
        self._refresh_data_manager()

    def _on_data_rescan_clicked(self) -> None:
        """Re-read the folder, including the manifest rebuild.

        The rebuild runs once per output root per session, so a run dropped in
        from a colleague's drive while the app is open is invisible until this
        clears the gate and reads it back.
        """
        self._data_rebuilt_root = None
        # Asked for, so it may show its work: sizes go blank and are counted again.
        self._run_size_cache.clear()
        self._run_size_stale.clear()
        self._refresh_data_manager()
        self._status_label.setText("Rescanned the output folder.")

    def _rebuild_data_tree(self) -> None:
        tree = self._data_tree
        tree.blockSignals(True)
        try:
            tree.clear()
            self._data_groups = {}
            grouped = self._data_facet in _GROUPED_FACETS
            if not grouped:
                self._data_selected_key = None
            else:
                self._set_rail_title(_RAIL_TITLES.get(self._data_facet, "Group"))
                for facet_group in self._data_facet_groups():
                    self._add_tree_group(facet_group, None)
                self._restore_tree_selection()
                self._data_tree_stack.setCurrentIndex(
                    0 if tree.topLevelItemCount() else 1
                )
        finally:
            tree.blockSignals(False)
        self._set_rail_visible(grouped)
        self._refresh_data_map()
        self._rebuild_data_run_list()

    def _refresh_data_map(self) -> None:
        """Draw the survey's transects, highlighting whichever one is in focus.

        Only the transect facet has a map: the other groupings say nothing about
        where anything is, and an unrelated map beside them would invite clicks
        that change the grouping out from under the list.
        """
        if not hasattr(self, "_data_map"):
            return
        shown = self._data_facet == "transects"
        self._data_map.setVisible(shown)
        if not shown or not getattr(self, "_data_store_ok", False):
            return
        selected = _key_transect_id(self._data_selected_key)
        try:
            overlays = transect_overlays(self._survey_store(), selected)
        except Exception:
            logger.exception("Could not build the Browse transect overlays")
            return
        self._data_map.set_transects(overlays)
        # Fit once per output root: refitting on every selection would yank the
        # map back to the whole survey each time a transect is picked.
        if not getattr(self, "_data_map_fitted", False) and overlays:
            self._data_map.fit_transects()
            self._data_map_fitted = True

    def _on_data_map_transect_clicked(self, transect_id: str) -> None:
        """A click on the map selects that transect's node, and nothing else.

        Routed through the tree rather than filtering directly, so the map, the
        tree and the analysis combo cannot end up pointing at different
        transects.
        """
        try:
            self._set_scope_transect(uuid.UUID(transect_id))
        except ValueError:
            logger.warning("Map click carried an unusable transect id: %r", transect_id)

    def _set_rail_visible(self, visible: bool) -> None:
        """Hide the whole rail, not just the tree inside it.

        Hiding only the tree left a fixed-width column with nothing in it, which
        read as a broken panel rather than as a view without groups.
        """
        # Tracked rather than read back from isVisible(): a widget in a window
        # that has not been shown yet reports False whatever we asked for, so
        # the first hide would look like a no-op and never take effect.
        if getattr(self, "_data_rail_shown", None) == visible:
            # The rail has not moved, but the detail pane's share follows the
            # facet, which can change while the rail stays where it is.
            self._apply_data_split_sizes(rail_visible=visible)
            return
        self._data_rail_shown = visible
        if not visible:
            # Qt collapses a hidden splitter child to zero and does not restore
            # the width by itself, so remember it. Only a width the splitter has
            # actually laid out counts: before the first show it reports a size
            # hint of a hundred-odd pixels, and remembering that pinned the rail
            # narrow enough to elide every name in it for the rest of the session.
            sizes = self._data_split.sizes()
            if sizes and sizes[0] >= _RAIL_MIN_WIDTH:
                self._data_rail_width = sizes[0]
        self._data_rail.setVisible(visible)
        self._apply_data_split_sizes(rail_visible=visible)

    def _apply_data_split_sizes(self, *, rail_visible: bool) -> None:
        """Divide Browse between the rail, the run table, and the detail pane.

        The table is the page: it is what you scan, sort and select in, so it
        takes the bulk. The detail pane only has to hold one run's facts and its
        ortho strip. Grouping by transect is the exception: there the pane holds
        a chart and a stats table, which need closer to half.

        The sizes are set outright rather than left to stretch factors. A
        splitter only shares out the space *above* each pane's minimum hint, and
        the analysis page's hint is ~320px, so stretch alone settled at roughly
        half and half however wide the window got.
        """
        if getattr(self, "_data_split_user_sized", False):
            return
        # The splitter's own width, not the sum of its sizes: during
        # construction the panes have not been laid out and the sum is a
        # placeholder, which is what the fallback is for.
        total = self._data_split.width()
        if total < SPLIT_MIN_TOTAL:
            total = sum(self._data_split.sizes()) or 1200
        rail = getattr(self, "_data_rail_width", _RAIL_WIDTH) if rail_visible else 0
        if self._data_detail_stack.currentIndex() == _DETAIL_EMPTY:
            # Nothing selected, so the table takes the whole width rather than
            # sharing it with a pane that has nothing to say.
            detail = 0
        else:
            share = _DETAIL_SHARE
            detail = max(_DETAIL_MIN_WIDTH, int((total - rail) * share))
        # Every pane gets a size. Handing setSizes fewer entries than the
        # splitter has children leaves the rest at zero, which collapsed the
        # detail pane to a hairline the moment a grouping was picked.
        self._data_split_applying = True
        try:
            self._data_split.setSizes([rail, max(280, total - rail - detail), detail])
        finally:
            self._data_split_applying = False

    def _data_split_event_filter(self, obj, event) -> None:
        """Re-divide Browse when its splitter is resized.

        Guarded on the splitter existing: the filter is installed on the
        application, so it starts receiving events while the window is still
        being built and Browse is one of the last pages assembled.
        """
        if obj is getattr(self, "_data_split", None) and event.type() == QEvent.Type.Resize:
            self._apply_data_split_sizes(rail_visible=bool(self._data_rail_shown))

    def _on_data_split_moved(self, *_args) -> None:
        """A dragged handle is a decision; stop overriding it on every resize."""
        if not getattr(self, "_data_split_applying", False):
            self._data_split_user_sized = True

    def _set_rail_title(self, title: str) -> None:
        label = self._data_rail.findChild(QLabel)
        if label is not None:
            label.setText(title)

    def _on_data_add_to_cart_clicked(self) -> None:
        """Queue the selected runs' passes for the next session.

        The pass carries trim, direction and transect already; reruns of one
        pass collapse to one cart item.
        """
        entries = [e for e in self._data_selected_entries() if not e.incomplete]
        if not entries:
            return
        if not getattr(self, "_data_store_ok", False):
            self._status_label.setText("Survey database unavailable; cannot queue.")
            return
        store = self._survey_store()
        added: set[uuid.UUID] = set()
        skipped = 0
        for entry in entries:
            try:
                pass_ = catalogue.ensure_pass_for_entry(store, entry)
            except ValueError:
                skipped += 1
                continue
            if pass_.id in added:
                continue
            self._cart_add(pass_.id)
            added.add(pass_.id)
        if not added and not skipped:
            return
        if added:
            self._refresh_survey_batch_tab()
            self._refresh_data_manager()
        message = (
            f"Added {len(added)} pass{'' if len(added) == 1 else 'es'} to the cart."
            if added
            else "Nothing was added to the cart."
        )
        if skipped:
            message += f" Skipped {skipped} with no recoverable time range."
        self._status_label.setText(message)

    def _focus_browse_on_session(self, batch_id: uuid.UUID) -> None:
        """Group by session and select this one, so Browse opens on that day."""
        self._data_facet_buttons["sessions"].click()
        key = catalogue.session_group_key(batch_id)
        tree = self._data_tree
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item is not None and item.data(0, _GROUP_KEY_ROLE) == key:
                # The table's selection outranks the group in the detail pane, so
                # it is released or the session's own summary never shows.
                self._data_run_table.clearSelection()
                self._data_run_table.setCurrentCell(-1, -1)
                tree.setCurrentItem(item)
                return

    def _on_data_open_transect(self) -> None:
        """Go to the transect this grouping is filtering by.

        The tree selection already set the scope, so Transects opens on the same
        line rather than on whatever was last picked there.
        """
        self._open_transect_page(_key_transect_id(self._data_selected_key))

    def _data_facet_groups(self) -> list[FacetGroup]:
        if self._data_facet == "sessions":
            return catalogue.sessions_facet(self._data_entries, self._known_sessions())
        if self._data_facet == "transects":
            transects = []
            if getattr(self, "_data_store_ok", False):
                try:
                    transects = self._survey_store().list_transects()
                except Exception:
                    logger.exception("Could not list transects")
            return catalogue.transects_facet(self._data_entries, transects)
        return []

    def _selected_session_group(self) -> FacetGroup | None:
        """The session the tree has selected, or None if that is not what is.

        Keyed on the group rather than the facet, because a pass selected under
        a session is a leaf and belongs to the run pane, not to this one.
        """
        key = self._data_selected_key
        if key is None or key[0] not in ("session", "unfiled_session"):
            return None
        return self._data_groups.get(key)

    def _on_data_session_audit(self) -> None:
        """What the selected session's runs actually ran under.

        Scoped to the session rather than reusing Process's whole-root audit:
        the question a session raises is about that day, and a list of every run
        the machine has ever done does not answer it.
        """
        from deepreefmap_gui.simple.config_audit_dialog import ConfigAuditDialog
        from deepreefmap_gui.survey.config_audit import audit_row

        group = self._selected_session_group()
        if group is None:
            return
        if self._active_preset is None:
            self._status_label.setText("The settings could not be read.")
            return
        org = self._active_preset.org
        rows = [
            audit_row(entry.dir_name, entry.manifest, org)
            for entry in sorted(group.all_entries(), key=lambda e: e.sort_key)
            if entry.manifest
        ]
        ConfigAuditDialog(self, rows, org).exec()

    def _known_sessions(self) -> list:
        """Sessions the store knows, so an emptied one still has a group.

        Guarded the same way the transect list is: Browse opens against whatever
        output root is set, and a root with no readable database is a normal
        thing to point at, not a reason to fail.
        """
        if not getattr(self, "_data_store_ok", False):
            return []
        try:
            return self._survey_store().list_batches()
        except Exception:
            logger.exception("Could not list sessions")
            return []

    def _add_tree_group(self, group: FacetGroup, parent: QTreeWidgetItem | None) -> None:
        count = len(group.all_entries())
        item = QTreeWidgetItem([f"{group.title}  ({count})"])
        item.setData(0, _GROUP_KEY_ROLE, group.key)
        self._data_groups[group.key] = group
        if parent is None:
            self._data_tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        for child in group.children:
            self._add_tree_group(child, item)
        item.setExpanded(True)

    def _restore_tree_selection(self) -> None:
        if self._data_selected_key is None:
            return
        matches = self._data_tree.findItems(
            "", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive
        )
        for item in matches:
            if item.data(0, _GROUP_KEY_ROLE) == self._data_selected_key:
                self._data_tree.setCurrentItem(item)
                return
        self._data_selected_key = None

    def _focus_data_on_transect(self, transect_id: uuid.UUID) -> None:
        """Point the browser at one transect."""
        if not hasattr(self, "_data_tree"):
            return
        # The run table is cleared first so its own restore has nothing to hold
        # on to: it re-selects whatever run was current across a rebuild, and a
        # surviving run of this transect would outrank the transect in the
        # detail pane.
        self._clear_run_table_selection()
        # Facet and key first, then the button: the toggle short-circuits when
        # the facet already matches, so the key survives.
        self._data_facet = "transects"
        self._data_selected_key = ("transect", str(transect_id))
        self._data_facet_buttons["transects"].setChecked(True)
        self._rebuild_data_tree()

    def _clear_run_table_selection(self) -> None:
        table = getattr(self, "_data_run_table", None)
        if table is not None:
            table.clearSelection()
            table.setCurrentCell(-1, -1)

    def _unfocus_data_transect(self) -> None:
        """Back to the whole transect facet, with nothing picked out of it."""
        if not hasattr(self, "_data_tree"):
            return
        self._data_selected_key = None
        self._data_tree.blockSignals(True)
        try:
            self._data_tree.clearSelection()
            # An empty index rather than setCurrentItem: the current row keeps
            # its focus rectangle otherwise, which reads as a selection that no
            # longer filters anything.
            self._data_tree.setCurrentIndex(QModelIndex())
        finally:
            self._data_tree.blockSignals(False)
        self._rebuild_data_tree()

    def _set_scope_transect(
        self, transect_id: uuid.UUID | None, focus: bool = True
    ) -> None:
        """One transect in focus across every widget that has an opinion.

        The Browse page carries a browser tree and an analysis combo that both
        pick a transect; left independent they would reproduce on one page the
        duplication being removed from another.

        ``None`` is the way back out: it releases the tree as well as the combo,
        so the map scope has something to apply to again.

        ``focus=False`` names the transect without moving the tree onto it, for
        a caller whose own selection is already inside that transect and would
        be thrown away by pulling the tree up a level.
        """
        if getattr(self, "_scope_syncing", False):
            return
        self._scope_syncing = True
        try:
            self._scope_transect_id = transect_id
            if focus:
                if transect_id is not None:
                    self._focus_data_on_transect(transect_id)
                elif self._data_facet == "transects":
                    self._unfocus_data_transect()
            combo = getattr(self, "_analysis_transect_combo", None)
            if combo is not None:
                wanted = str(transect_id) if transect_id is not None else None
                for index in range(combo.count()):
                    if combo.itemData(index) == wanted:
                        combo.setCurrentIndex(index)
                        break
        finally:
            self._scope_syncing = False

    def _update_data_actions(self) -> None:
        """Gate each action on the selection, and point the detail pane at it.

        A run with no manifest can only be shown in a folder or deleted, so the
        openers stay off for one.
        """
        current = self._data_selected_entry()
        complete_current = current is not None and not current.incomplete
        self._data_open_btn.setEnabled(complete_current)
        # Showing a crashed run in its folder is exactly how you inspect it.
        self._data_show_btn.setEnabled(current is not None)
        self._gate_data_row_actions(self._data_more_actions)
        self._refresh_data_detail()

    def _refresh_data_detail(self) -> None:
        """Show the selected run, else the selected transect, else nothing.

        The run wins when both are selected: it is the more specific of the two,
        and it is what was clicked last to get here.

        A group holding exactly one run falls through to that run. Without it,
        clicking a leaf of the tree -- a single pass under a transect -- lit the
        row up and left the pane saying nothing was selected, which is not what
        a visible selection means.
        """
        entry = self._data_selected_entry()
        if entry is not None:
            self._run_detail.show_entry(entry, self._related_for(entry))
            self._set_data_detail_page(_DETAIL_RUN)
            # First card of the session asks the registry; the answer repaints it.
            self._maybe_refresh_archive_badges()
            return
        key = self._data_selected_key
        if self._data_facet == "transects" and key is not None and key[0] == "transect":
            group = self._data_groups.get(key)
            if group is not None:
                self._transect_detail.show_group(group)
            self._set_data_detail_page(_DETAIL_TRANSECT)
            return
        session = self._selected_session_group()
        if session is not None:
            self._session_detail.show_group(session)
            self._set_data_detail_page(_DETAIL_SESSION)
            return
        # A group of one is that run: a leaf of the tree is a single pass, and a
        # lit row with nothing in the pane is not what a selection means. Below
        # the transect case, which owns its own page.
        group = self._data_groups.get(key) if key is not None else None
        grouped = group.all_entries() if group is not None else None
        if grouped and len(grouped) == 1:
            self._run_detail.show_entry(grouped[0], self._related_for(grouped[0]))
            self._set_data_detail_page(_DETAIL_RUN)
            self._maybe_refresh_archive_badges()
            return
        # A group of several has no pane of its own, and wants none: selecting it
        # has already filtered the table to exactly those runs, and the pane
        # collapsing hands its width back to them. Picking one opens it.
        self._set_data_detail_page(_DETAIL_EMPTY)

    def _related_for(self, entry: RunEntry) -> int:
        """Sibling runs of the same footage, as last counted for the table."""
        return getattr(self, "_data_related", {}).get(entry.run_dir, 0)

    def _set_data_detail_page(self, page: int) -> None:
        """Switch the detail pane, and re-divide when it comes or goes.

        An empty pane takes no width: holding its 260px minimum to say "Nothing
        selected" costs the run table a third of the window, eliding its names.
        """
        was_empty = self._data_detail_stack.currentIndex() == _DETAIL_EMPTY
        self._data_detail_stack.setCurrentIndex(page)
        self._data_detail_stack.setVisible(page != _DETAIL_EMPTY)
        if was_empty != (page == _DETAIL_EMPTY):
            self._apply_data_split_sizes(rail_visible=bool(self._data_rail_shown))

    def _on_data_facet_changed(self, name: str) -> None:
        # _focus_data_on_transect sets the facet and the key together and then
        # checks the button; without this the check would land here first and
        # throw the key away, rebuilding the tree twice for one selection.
        if name == self._data_facet:
            return
        self._data_facet = name
        self._data_selected_key = None
        self._data_split_user_sized = False
        if hasattr(self, "_data_tree"):
            self._rebuild_data_tree()

    def _on_data_tree_selection(self) -> None:
        items = self._data_tree.selectedItems()
        item = items[0] if items else None
        key = item.data(0, _GROUP_KEY_ROLE) if item is not None else None
        parent = item.parent() if item is not None else None
        parent_key = parent.data(0, _GROUP_KEY_ROLE) if parent is not None else None
        self._data_selected_key = key
        self._refresh_data_map()
        self._rebuild_data_run_list()
        # Picking a transect here drives the comparison below it on the same
        # page. A pass row belongs to a transect too, so it points the
        # comparison at that one -- without pulling the selection up off the
        # pass the click actually landed on.
        transect_id = _key_transect_id(key)
        if transect_id is not None:
            self._set_scope_transect(transect_id)
            return
        parent_id = _key_transect_id(parent_key)
        if parent_id is not None:
            self._set_scope_transect(parent_id, focus=False)

    def _data_grouped_entries(self) -> list[RunEntry]:
        """The runs the current grouping selection covers, before filtering."""
        if self._data_facet == "runs" or self._data_selected_key is None:
            return self._data_entries
        group = self._data_groups.get(self._data_selected_key)
        return group.all_entries() if group is not None else []

    def _data_scope_offered(self) -> bool:
        """Whether the map scope is a choice worth showing.

        Anywhere in the transect facet, including while a transect is picked out
        of it -- All transects is how that pick is undone, and hiding the row
        exactly when something is selected left no way back to In view at all.
        The other facets have no map, so there is nothing to scope by.
        """
        return self._data_facet == "transects"

    def _data_scope_applies(self) -> bool:
        """Whether the map is currently deciding what is listed.

        Only at the top of the facet. A group picked in the tree is an explicit
        choice of what to look at, and panning off it afterwards should not
        empty the list underneath.
        """
        return self._data_scope_offered() and self._data_selected_key is None

    def _data_visible_transect_ids(self) -> frozenset[str] | None:
        """Transects the Browse map is showing, or None when it cannot say."""
        map_widget = getattr(self, "_data_map", None)
        return None if map_widget is None else map_widget.visible_ids()

    def _data_scoped_entries(self) -> list[RunEntry]:
        """The grouping, narrowed to the transects the map is showing."""
        entries = self._data_grouped_entries()
        if not self._data_scope_applies() or self._data_scope_filter != "in_view":
            return entries
        visible = self._data_visible_transect_ids()
        if visible is None:
            return entries
        return _entries_in_view(entries, visible)

    def _data_listed_entries(self) -> list[RunEntry]:
        """What the list shows: the group, narrowed by map, outcome and search."""
        entries = self._data_scoped_entries()
        if self._data_status_filter != "all":
            entries = [
                e for e in entries if catalogue.entry_outcome(e) == self._data_status_filter
            ]
        needle = self._data_search.text().strip().lower()
        if needle:
            entries = [e for e in entries if needle in self._entry_search_text(e)]
        return entries

    @staticmethod
    def _entry_search_text(entry: RunEntry) -> str:
        """Everything about a run worth typing to find it again."""
        return " ".join(
            part.lower()
            for part in (
                entry.display_name,
                entry.dir_name,
                entry.video_name or "",
                entry.transect_name or "",
            )
        )

    def _on_data_status_filter_changed(self, key: str) -> None:
        self._data_status_filter = key
        self._rebuild_data_run_list()

    def _on_data_scope_filter_changed(self, key: str) -> None:
        """Pick a scope, and let go of any transect that was pinned under it.

        Scoping by the viewport inside a single transect says nothing, so a chip
        pressed while one is picked releases it first. That is also the way out
        of a pick: the rail has no All node to click, so All transects is it.
        """
        self._data_scope_filter = key
        if self._data_scope_offered() and self._data_selected_key is not None:
            self._set_scope_transect(None)
        else:
            self._rebuild_data_run_list()

    def _apply_data_view_change(self) -> None:
        """Pan, zoom and resize re-decide which runs the map is showing.

        Rebuilt only when the set of on-screen transects actually changed: the
        table is rebuilt from scratch, and doing that on every pixel of a drag
        would drop the selection under the cursor.
        """
        if not hasattr(self, "_data_run_table") or not self._data_scope_offered():
            return
        visible = self._data_visible_transect_ids()
        if visible == self._data_visible_ids:
            return
        self._data_visible_ids = visible
        if self._data_scope_applies() and self._data_scope_filter == "in_view":
            self._rebuild_data_run_list()
        else:
            # Nothing listed changes, but the chip still has to say how many
            # runs switching to In view would leave.
            self._refresh_data_scope_chips()

    def _refresh_data_scope_chips(self) -> None:
        """Show the map scope wherever it is a choice, with a count on each side.

        Counted over the whole facet rather than over the current pick, because
        while a transect is picked the chips are what releases it: the numbers
        say what pressing one would list, not what is listed now.
        """
        offered = self._data_scope_offered()
        self._data_scope_chips.setVisible(offered)
        if not offered:
            return
        entries = self._data_entries
        visible = self._data_visible_ids
        in_view = len(entries) if visible is None else len(_entries_in_view(entries, visible))
        self._data_scope_chips.set_counts({"in_view": in_view, "all": len(entries)})

    def _refresh_data_status_counts(self) -> None:
        """Count over the grouping, not the whole root: the chips filter what is listed."""
        entries = self._data_scoped_entries()
        counts = {option[0]: 0 for option in _STATUS_FILTERS}
        counts["all"] = len(entries)
        for entry in entries:
            outcome = catalogue.entry_outcome(entry)
            counts[outcome] = counts.get(outcome, 0) + 1
        self._data_status_chips.set_counts(counts)

    def _rebuild_data_run_list(self) -> None:
        # The rebuild is the point at which the list catches up with the map, so
        # this is also where the view the change detector compares against is set.
        self._data_visible_ids = self._data_visible_transect_ids()
        self._refresh_data_scope_chips()
        self._refresh_data_status_counts()
        listed = self._data_listed_entries()
        related = related_run_counts([(e.run_dir, e.manifest) for e in self._data_entries])
        # Kept, because the detail pane names the same number the table's tooltip
        # does and neither should count it for itself.
        self._data_related = related
        self._data_run_table.set_entries(listed, related)
        self._data_group_header.setText(self._data_header_text(listed))
        self._data_group_header.setVisible(bool(listed))
        # An empty list means one of three different things, and saying which is
        # the difference between "nothing here", "nothing matches" and "nothing
        # where you are looking".
        scoped = self._data_scoped_entries()
        key = self._data_selected_key
        if not listed and isinstance(key, tuple) and key and key[0] == "pass":
            # A section that has never run.
            self._data_empty_state.set_text(
                "Not processed yet",
                "It is in the cart."
                if self._pass_in_current_cart(key[1])
                else "Add it to the cart to process it.",
            )
        elif not listed and not scoped and self._data_grouped_entries():
            self._data_empty_state.set_text(
                "No runs on this part of the map",
                "Pan or zoom out, or switch to All transects.",
            )
        elif not listed and scoped:
            self._data_empty_state.set_text(
                "No runs match these filters", "Clear the search or pick a different outcome."
            )
        else:
            self._data_empty_state.set_text("No runs here yet", "Processed passes collect here.")
        self._data_run_stack.setCurrentIndex(_RUN_LIST_PAGE if listed else _EMPTY_PAGE)
        self._update_data_actions()

    def _data_header_text(self, listed: list[RunEntry]) -> str:
        if not listed:
            return ""
        stats = catalogue.group_stats(listed)
        bits = [f"{stats.run_count} run{'s' if stats.run_count != 1 else ''}"]
        if stats.duration_range:
            lo, hi = stats.duration_range
            bits.append(
                f"runtime {format_duration(lo)}"
                + (f" – {format_duration(hi)}" if hi != lo else "")
            )
        if stats.point_range:
            lo_p, hi_p = stats.point_range
            bits.append(
                f"{points_label(lo_p)}–{points_label(hi_p)} points"
                if hi_p != lo_p
                else f"{points_label(hi_p)} points"
            )
        # Disk is deliberately absent: the label at the other end of this row
        # already carries it, and printing it twice on one line read as two
        # different figures that happened to agree.
        return " · ".join(bits)

    def _on_data_run_activated(self, _item) -> None:
        self._on_data_open_clicked()

    def _on_data_open_clicked(self) -> None:
        run_dir = self._data_run_table.current_run_dir()
        if run_dir:
            self._load_run_from_dir(Path(run_dir))

    def _load_run_from_dir(self, path: Path) -> None:
        """Load a run directory into the viewer, guarded against a live run.

        The single seam every open path routes through: the run cards, the
        open-folder action, and a dropped folder all end up here.
        """
        # Browse stays reachable while a batch runs, so opening an old run here
        # would take the viewer away from the run currently streaming into it.
        if self._run_in_flight():
            self._status_label.setText("Wait for processing to finish before opening a run.")
            return
        if not path.is_dir():
            self._status_label.setText("The output data for this run was removed.")
            return
        # Facts first, straight from the manifest, so the click lands instantly
        # even when the load itself takes a while.
        manifest_path = path / "run_manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self._show_run_facts(manifest)
            except Exception:
                self._clear_run_facts()
        self._auto_load_run(path)

    def _on_data_open_folder_clicked(self) -> None:
        """Open a run directory picked from disk, wherever it sits."""
        if self._run_in_flight():
            self._status_label.setText("Wait for processing to finish before opening a run.")
            return
        path = QFileDialog.getExistingDirectory(
            self, "Open run folder", self._out_root_input.text()
        )
        if path:
            self._load_run_from_dir(Path(path))

    # --- Actions ---

    def _data_selected_entry(self) -> RunEntry | None:
        run_dir = self._data_run_table.current_run_dir()
        if run_dir is None:
            return None
        for entry in self._data_entries:
            if str(entry.run_dir) == run_dir:
                return entry
        return None

    def _data_selected_entries(self) -> list[RunEntry]:
        """Every run picked in the table, for the actions that act on many."""
        run_dirs = self._data_run_table.selected_run_dirs()
        return [e for e in self._data_entries if str(e.run_dir) in run_dirs]

    def _on_data_show_in_folder_clicked(self) -> None:
        entry = self._data_selected_entry()
        if entry is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.run_dir)))

    def _data_row_action_specs(self) -> tuple[tuple[str | None, str, object], ...]:
        """Everything that acts on whichever runs are selected, in one list.

        Both the More menu and the row's context menu are built from this. Two
        hand-written menus is how one of them ends up missing the thing you went
        looking for, which is what happened to "Copy run command".

        A None key is a separator.
        """
        return (
            ("open", "Open", self._on_data_open_clicked),
            ("show", "Show in folder", self._on_data_show_in_folder_clicked),
            ("rename", "Rename…", self._on_data_rename_clicked),
            ("assign", "Assign to transect…", self._on_data_assign_clicked),
            ("cart", "Add to cart", self._on_data_add_to_cart_clicked),
            ("copy", "Copy run command", self._on_data_copy_command_clicked),
            (None, "", None),
            ("delete", "Delete…", self._on_data_delete_clicked),
        )

    def _fill_data_row_actions(self, menu: QMenu) -> dict:
        actions = {}
        for key, label, slot in self._data_row_action_specs():
            if key is None:
                menu.addSeparator()
                continue
            actions[key] = menu.addAction(label, slot)
        return actions

    def _gate_data_row_actions(self, actions: dict) -> None:
        """Grey out what the current selection cannot do.

        A run with no manifest can only be shown in a folder or deleted, so the
        openers stay off for one.
        """
        current = self._data_selected_entry()
        selected = self._data_selected_entries()
        on_disk = current is not None and not current.data_missing
        complete = current is not None and on_disk and not current.incomplete
        for key, enabled in (
            ("open", complete),
            ("rename", complete),
            # A crashed run still wrote the command that made it, and that is
            # exactly what a diagnosis starts from.
            ("copy", on_disk),
            ("show", on_disk),
            # Assign works from a table selection or from the selected tree
            # group, so it asks the same source the handler will act on.
            ("assign", bool(self._data_assign_targets())),
            # Deliberately not gated on a running batch: while an order runs is
            # exactly when the next cart matters.
            ("cart", any(not e.incomplete for e in selected)),
            ("delete", bool(selected)),
        ):
            action = actions.get(key)
            if action is not None:
                action.setEnabled(enabled)

    def _on_data_context_menu(self, pos) -> None:
        item = self._data_run_table.itemAt(pos)
        # Leave an existing multi-selection alone: collapsing it to the
        # right-clicked row would throw away the runs the menu is about to act on.
        if item is not None and not item.isSelected():
            self._data_run_table.setCurrentCell(item.row(), COL_NAME)
        menu = QMenu(self._data_run_table)
        self._gate_data_row_actions(self._fill_data_row_actions(menu))
        menu.exec(self._data_run_table.mapToGlobal(pos))

    def _show_run_log(self, run_dir) -> None:
        """Put a stored run's log in the log panel and open it.

        Works on a run that cannot be loaded, which is the point: an incomplete
        run has no outputs to open and its log is the only account of why.
        """
        from pathlib import Path

        run_dir = Path(run_dir)
        if not self._log_view.show_file(run_dir / "run.log", title=run_dir.name):
            self._status_label.setText(f"Could not read the log in {run_dir.name}.")
            return
        self._set_log_panel_visible(True)

    # --- Drag and drop ---

    def _data_drop_event_filter(self, obj, event) -> bool:
        """Filter file drops on the run list, the group tree and the clip library.

        One handler serves them all rather than subclassing each widget. Video
        files queue as passes; a run folder opens. Returns True when the event is
        consumed. DeepReefMapWindow.eventFilter calls this, because QObject owns
        eventFilter earlier in the MRO than this mixin.

        The cart table is deliberately absent: its dropEvent reads currentRow()
        as the source of an internal move, so a file dropped on it would reorder
        the session instead of importing anything.
        """
        etype = event.type()
        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return True
            return False
        if etype == QEvent.Type.Drop and event.mimeData().hasUrls():
            paths = [
                Path(url.toLocalFile())
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            self._handle_data_drop(paths)
            event.acceptProposedAction()
            return True
        return False

    def _handle_data_drop(self, paths: list[Path]) -> None:
        run_dirs = [p for p in paths if p.is_dir() and is_run_dir(p)]
        if run_dirs:
            self._load_run_from_dir(run_dirs[0])
            if len(run_dirs) > 1:
                self._status_label.setText("Dropped several run folders; opened the first.")
            return
        videos, truncated = find_videos(paths)
        if videos:
            # Probing happens off the GUI thread, so the library entries and the
            # count both land in _on_videos_probed rather than here.
            self._add_video_paths([str(p) for p in videos])
            if any(p.is_dir() for p in paths):
                count = f"{len(videos)} clip{'' if len(videos) == 1 else 's'}"
                message = f"Found {count} to import."
                if truncated:
                    message += " That folder was too deep to finish searching."
                self._status_label.setText(message)
            return
        self._status_label.setText("Drop video files, a folder of them, or a run folder here.")

    def _on_data_copy_command_clicked(self) -> None:
        """Put the selected run's terminal equivalent on the clipboard."""
        from deepreefmap_gui.runs.run_command import command_from_manifest

        entry = self._data_selected_entry()
        if entry is None:
            return
        try:
            text = command_from_manifest(entry.manifest, entry.run_dir)
        except Exception as exc:
            self._status_label.setText(f"Could not build the run command: {exc}")
            logger.exception("Failed to build the run command")
            return
        QGuiApplication.clipboard().setText(text)
        self._status_label.setText(f"Copied the command for '{entry.display_name}'.")

    def _taken_run_names(self, keep: RunEntry) -> set[str]:
        """What the other runs are called, so no two arrive with one name between them."""
        return {
            e.display_name.strip()
            for e in self._data_entries
            if e.run_dir != keep.run_dir and e.display_name.strip()
        }

    def _ask_run_name(self, entry: RunEntry) -> str | None:
        """Ask for a name, and keep asking while it is one another run already has.

        The retry is offered pre-filled with the first free variant rather than
        with the rejected text, so accepting the dialog a second time always
        gets somewhere.
        """
        from deepreefmap_gui.survey.labels import unique_label

        taken = self._taken_run_names(entry)
        proposed = (entry.manifest.get("name") or "").strip() or entry.dir_name
        prompt = "Run name:"
        while True:
            text, ok = QInputDialog.getText(
                self, "Rename run", prompt, text=proposed
            )
            if not ok:
                return None
            wanted = " ".join(text.split())
            if not wanted:
                return None
            if wanted not in taken:
                return wanted
            prompt = f"'{wanted}' is already taken by another run. Run name:"
            proposed = unique_label(wanted, taken)

    def _on_data_rename_clicked(self) -> None:
        entry = self._data_selected_entry()
        if entry is None:
            return
        new_name = self._ask_run_name(entry)
        if new_name is None:
            return
        try:
            manifest = catalogue.rename_run(entry.run_dir, new_name)
        except Exception as exc:
            self._status_label.setText(f"Rename failed: {exc}")
            logger.exception("Failed to rename run")
            return
        if self._active_run_dir == entry.run_dir:
            self._active_run_manifest = manifest
            self._show_run_facts(manifest)
        self._status_label.setText(f"Renamed run to '{new_name}'.")
        self._refresh_data_manager()

    def _on_data_session_delete(self) -> None:
        """Remove a session's output data, its records, or both.

        The dialog counts exactly what would go. What is shared stays
        untouched: sections keep their trims, clips stay in the library, and
        transects keep their tape. Records can only be forgotten once the data
        is gone, because a rescan rebuilds them from the manifests otherwise.
        """
        group = self._selected_session_group()
        if group is None or group.key[0] != "session":
            return
        store = self._survey_store() if getattr(self, "_data_store_ok", False) else None
        if store is None:
            return
        batch_id = uuid.UUID(str(group.key[1]))
        batch = store.get_batch(batch_id)
        if batch is None:
            self._status_label.setText("This session has no record to act on.")
            return
        runs = store.runs_in_batch(batch_id)
        cart = store.list_batch_items(batch_id)
        root = self._data_out_root()
        with_data = [r for r in runs if (root / r.run_dir_name).is_dir()]
        if self._active_run_dir is not None and any(
            root / r.run_dir_name == self._active_run_dir for r in with_data
        ):
            QMessageBox.information(
                self,
                "Delete session",
                "One of this session's runs is open. Close it first with the + button.",
            )
            return
        if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
            QMessageBox.information(
                self, "Delete session", "Wait for the current run to finish."
            )
            return
        sizes = [self._run_size_cache.get(r.run_dir_name) for r in with_data]
        known = [s for s in sizes if s is not None]
        size_txt = format_bytes(sum(known)) if known else "size still counting"
        if len(known) not in (0, len(with_data)):
            size_txt = f"at least {size_txt}"
        counts = (
            f"{len(runs)} run{'s' if len(runs) != 1 else ''} and "
            f"{len(cart)} cart item{'s' if len(cart) != 1 else ''}"
        )
        choice = DeleteDataDialog.ask(
            DeleteScope(
                title="Delete session",
                subject=f"Delete from session '{batch.name}' ({counts})?",
                data_detail=(
                    f"The output folders of its {len(with_data)} "
                    f"run{'s' if len(with_data) != 1 else ''} on disk, {size_txt}. "
                    "The records stay, so the session still shows here."
                ),
                metadata_detail=(
                    f"The session, its cart items and its {len(runs)} run "
                    f"record{'s' if len(runs) != 1 else ''}, a few kilobytes. "
                    "Removing records forgets the session ever ran; it frees no "
                    "disk space worth naming."
                    if not with_data
                    else "Available once the output data is removed: while the "
                    "data exists, a rescan would rebuild the records from their "
                    "manifests."
                ),
                keeps=(
                    "Sections and their trims",
                    "Clips in the library",
                    "Transects",
                ),
                data_present=bool(with_data),
                metadata_present=not with_data,
                extra_notes=("This cannot be undone.",),
            ),
            self,
        )
        if choice is None:
            return
        data_gone = 0
        if choice is not DeleteChoice.METADATA:
            for run in with_data:
                try:
                    catalogue.delete_run_data(root, root / run.run_dir_name)
                except Exception as exc:
                    self._status_label.setText(f"Delete failed: {exc}")
                    logger.exception("Failed to delete run data")
                    continue
                self._run_size_cache.pop(run.run_dir_name, None)
                data_gone += 1
        if choice is not DeleteChoice.DATA:
            store.delete_batch(batch_id)
            self._status_label.setText(f"Deleted session '{batch.name}'.")
        elif data_gone:
            self._status_label.setText(
                f"Deleted the data of {data_gone} run{'s' if data_gone != 1 else ''} "
                f"from '{batch.name}'."
            )
        self._refresh_data_manager()

    def _on_data_delete_clicked(self) -> None:
        entries = self._data_selected_entries()
        if not entries:
            return
        if self._active_run_dir is not None and any(
            e.run_dir == self._active_run_dir for e in entries
        ):
            QMessageBox.information(
                self, "Delete run", "One of these runs is open. Close it first with the + button."
            )
            return
        if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
            QMessageBox.information(
                self, "Delete run", "Wait for the current run to finish."
            )
            return
        choice = DeleteDataDialog.ask(self._delete_scope(entries), self)
        if choice is None:
            return
        store = self._survey_store() if getattr(self, "_data_store_ok", False) else None
        data_gone = records_gone = 0
        for entry in entries:
            try:
                if choice is not DeleteChoice.METADATA and not entry.data_missing:
                    catalogue.delete_run_data(self._data_out_root(), entry.run_dir)
                    self._run_size_cache.pop(entry.dir_name, None)
                    data_gone += 1
                if choice is not DeleteChoice.DATA and store is not None:
                    run = entry.db_run or store.run_by_dir_name(entry.dir_name)
                    if run is not None:
                        store.delete_run(run.id)
                        records_gone += 1
            except Exception as exc:
                self._status_label.setText(f"Delete failed: {exc}")
                logger.exception("Failed to delete run")
                continue
        if data_gone or records_gone:
            self._status_label.setText(_deleted_summary(data_gone, records_gone))
        self._refresh_data_manager()

    def _delete_scope(self, entries: list[RunEntry]) -> DeleteScope:
        """One dialog scope for the selection, sizes said where they are known.

        Removing only the record while the data stays is not offered: the next
        rescan rebuilds the row from the run's manifest, so the choice would
        silently undo itself.
        """
        with_data = [e for e in entries if not e.data_missing]
        sizes = [self._run_size_cache.get(e.dir_name) for e in with_data]
        known = [s for s in sizes if s is not None]
        size_txt = format_bytes(sum(known)) if known else "size still counting"
        if len(known) not in (0, len(with_data)):
            size_txt = f"at least {size_txt}"
        if len(entries) == 1:
            subject = f"Delete from '{entries[0].display_name}'?"
        else:
            subject = f"Delete from {len(entries)} runs?"
        records = sum(1 for e in entries if e.db_run is not None)
        return DeleteScope(
            title="Delete run",
            subject=subject,
            data_detail=(
                f"The output folder{'s' if len(with_data) != 1 else ''} on disk, "
                f"{size_txt}. The record stays, so the run still shows here."
            ),
            metadata_detail=(
                f"{records} record{'s' if records != 1 else ''} in the survey "
                "database, a few kilobytes. Removing a record forgets the run "
                "ever happened; it frees no disk space worth naming."
                if not with_data
                else "Available once the output data is removed: while the data "
                "exists, a rescan would rebuild the record from its manifest."
            ),
            data_present=bool(with_data),
            metadata_present=bool(records) and not with_data,
            extra_notes=("This cannot be undone.",),
        )

    def _data_assign_targets(self) -> list[RunEntry]:
        """The whole footage group of every selected run, or the selected tree
        group, so reruns of the same pass move together."""
        # A crashed run has no time window, so it cannot become a pass; leave it
        # out rather than fail the whole assignment.
        selected = [e for e in self._data_selected_entries() if not e.incomplete]
        if selected:
            keys = {catalogue.group_key(e) for e in selected}
            return [e for e in self._data_entries if catalogue.group_key(e) in keys]
        if self._data_selected_key is None:
            return []
        group = self._data_groups.get(self._data_selected_key)
        if group is None:
            return []
        return [e for e in group.all_entries() if not e.incomplete]

    def _ask_assign_target(self, transects: list) -> tuple[uuid.UUID, str] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Assign to transect")
        form = QFormLayout(dialog)
        transect_combo = QComboBox()
        for transect in transects:
            transect_combo.addItem(transect.name, userData=transect.id)
        direction_combo = QComboBox()
        for name in PASS_DIRECTIONS:
            # The lowercase vocabulary as the item's data: the store's CHECK
            # constraint is what the capitalised label would fail.
            direction_combo.addItem(direction_arrow_icon(name), name.capitalize(), name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow("Transect", transect_combo)
        form.addRow("Direction", direction_combo)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return transect_combo.currentData(), direction_combo.currentData()

    def _on_data_assign_clicked(self) -> None:
        entries = self._data_assign_targets()
        if not entries:
            return
        if not getattr(self, "_data_store_ok", False):
            self._status_label.setText("Survey database unavailable; cannot assign.")
            return
        store = self._survey_store()
        transects = store.list_transects()
        if not transects:
            QMessageBox.information(
                self, "Assign to transect", "Create a transect under Transects first."
            )
            return
        target = self._ask_assign_target(transects)
        if target is None:
            return
        transect_id, direction = target
        try:
            catalogue.assign_to_transect(store, entries, transect_id, direction)
        except Exception as exc:
            self._status_label.setText(f"Assign failed: {exc}")
            logger.exception("Failed to assign runs to transect")
            return
        n = len(entries)
        name = next(t.name for t in transects if t.id == transect_id)
        self._status_label.setText(f"Assigned {n} run{'s' if n != 1 else ''} to '{name}'.")
        self._refresh_data_manager()
        self._refresh_transect_list()
        self._refresh_survey_analysis()
        # The Process table holds its own copy of each pass row, and its next
        # write would put the stale transect back over this assignment.
        self._refresh_survey_batch_tab()

    # --- Disk sizes ---

    def _start_data_size_scan(self) -> None:
        if self._data_sizes_scan_running:
            return
        todo = [
            (e.dir_name, e.run_dir)
            for e in self._data_entries
            if not e.data_missing
            and (e.dir_name not in self._run_size_cache or e.dir_name in self._run_size_stale)
        ]
        if not todo:
            return
        self._data_sizes_scan_running = True
        self._run_size_stale.difference_update(name for name, _ in todo)

        def worker() -> None:
            sizes: dict[str, int] = {}
            for name, path in todo:
                try:
                    sizes[name] = catalogue.dir_size_bytes(path)
                except Exception:
                    logger.exception("Could not size %s", path)
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_run_sizes_done.emit(sizes)

        threading.Thread(target=worker, daemon=True, name="run-size-scan").start()

    def _apply_run_sizes(self, sizes: dict) -> None:
        self._data_sizes_scan_running = False
        self._run_size_cache.update(sizes)
        for entry in self._data_entries:
            entry.size_bytes = self._run_size_cache.get(entry.dir_name)
        self._update_data_disk_label()
        self._rebuild_data_run_list()
        # The storage bars attribute these bytes to their drive, so they are one
        # measurement rather than two walks of the same directories.
        self._refresh_storage_bars()
        # Anything marked stale while that scan was in flight was skipped by it.
        if self._run_size_stale:
            self._start_data_size_scan()

    def _update_data_disk_label(self) -> None:
        """What the output folder costs, said once.

        The run count belongs to the group header beside it, so this says the
        size and only qualifies it when some runs have not been measured yet.
        """
        sized = [e for e in self._data_entries if e.size_bytes is not None]
        if not sized:
            self._data_disk_label.setText("")
            return
        total = sum(e.size_bytes for e in sized if e.size_bytes is not None)
        suffix = "" if len(sized) == len(self._data_entries) else " so far"
        self._data_disk_label.setText(f"{format_bytes(total)} on disk{suffix}")
