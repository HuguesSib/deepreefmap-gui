"""One drive's page: what the survey put here, and what of it can go.

Reached only by pressing that drive's button at the foot of the window, so it is
a section rather than a destination: nothing in the header lights, and the
button that opened it is what says where you are. Pressing it again goes back.

Nothing here asks in a dialog. A delete arms on the first click and happens on
the second, the way a clip is deleted under Videos, because clearing a season of
caches is a run of clicks and a run of modals is the whole cost of the job.

The database is never what is deleted. A run keeps its record and shows in
Browse as a run whose data is gone; a clip keeps its hash, its length and every
section cut from it, and simply reads as missing footage from then on.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.reveal import reveal_in_file_manager
from deepreefmap_gui.core.storage_bar import (
    TALL_BAR_HEIGHT,
    VolumeBar,
    alert_colour,
    volume_headline,
    volume_rows,
)
from deepreefmap_gui.core.theme import (
    BORDER,
    ERROR,
    GUTTER,
    RADIUS_SM,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    WEIGHT_SEMIBOLD,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import (
    EmptyState,
    SectionHeader,
    enable_sorting,
    muted_label,
    page_scroll_area,
    secondary_label,
    section_card,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.profiling.volumes import VolumeUsage
from deepreefmap_gui.runs.video_rows import DELETE_ARM_MS
from deepreefmap_gui.storage import inventory as inventory_mod
from deepreefmap_gui.storage import rows as rows_mod
from deepreefmap_gui.storage.inventory import MountInventory
from deepreefmap_gui.storage.reclaim import (
    Reclaimed,
    ReclaimError,
    delete_input_clip,
    delete_other,
    delete_run_folder,
    delete_tier,
)
from deepreefmap_gui.storage.tiers import RunBreakdown

logger = logging.getLogger(__name__)

RUNS_TITLE = "Runs"
CLIPS_TITLE = "Videos"

NOTHING_TO_FREE = "Nothing to free."
FREE_SELECTED = "Delete selected"
FREE_ARMED = "Click again to delete"
GRAVE_WARNING = "Runs deleted this way cannot be opened or resumed again. Their records stay."
RUN_IN_FLIGHT = "Wait for the current run to finish."

NO_RUNS = "No runs on this drive."
NO_RUNS_HINT = "Runs are written to the output folder, which is set under Setup."
NO_CLIPS = "No clips on this drive."
NO_CLIPS_HINT = "Add footage under Videos."
DISCONNECTED = "This drive is not connected."
DISCONNECTED_HINT = "Plug it back in, or pick another drive from the bar below."

ELSEWHERE = "Runs are written to {name}."
REVEAL_FAILED = "The file manager could not be opened."


class StorageMixin(MixinBase):
    """DeepReefMapWindow methods for the per-drive storage page."""

    _storage_root: str | None = None
    # Bumped whenever the page changes what it is looking at, so a scan that
    # lands after somebody has moved on is dropped rather than painted.
    _storage_scan_id: int = 0
    # Not _storage_scan_running, which the bottom bar's own scan owns.
    _storage_page_scanning: bool = False
    _storage_inventory: MountInventory | None = None
    _storage_breakdowns: dict[str, RunBreakdown]

    # --- building -----------------------------------------------------------

    def _build_storage_page(self) -> QWidget:
        """The header, the one action, and the two lists it acts on."""
        self._storage_breakdowns = {}
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        layout.addWidget(self._build_storage_header())
        layout.addWidget(self._build_storage_free_bar())

        self._storage_runs_stack = QStackedWidget()
        self._storage_runs = self._make_tree(
            rows_mod.RUN_COLUMNS, rows_mod._RUN_COLUMN_SPEC, settings_key="storage_runs"
        )
        self._storage_runs.itemChanged.connect(self._on_storage_item_changed)
        self._storage_runs.itemClicked.connect(self._on_storage_row_clicked)
        self._storage_runs_stack.addWidget(self._storage_runs)
        self._storage_runs_stack.addWidget(EmptyState(NO_RUNS, NO_RUNS_HINT))
        # Runs take the larger share of what is left: there are more of them,
        # each can expand into four rows, and the clip list is one row a clip.
        self._storage_runs_card = self._card_around(RUNS_TITLE, self._storage_runs_stack)
        layout.addWidget(self._storage_runs_card, 3)

        self._storage_clips_stack = QStackedWidget()
        self._storage_clips = self._make_tree(
            rows_mod.CLIP_COLUMNS, rows_mod._CLIP_COLUMN_SPEC, settings_key="storage_clips"
        )
        self._storage_clips.itemChanged.connect(self._on_storage_item_changed)
        self._storage_clips.itemClicked.connect(self._on_storage_row_clicked)
        self._storage_clips_stack.addWidget(self._storage_clips)
        self._storage_clips_stack.addWidget(EmptyState(NO_CLIPS, NO_CLIPS_HINT))
        self._storage_clips_card = self._card_around(CLIPS_TITLE, self._storage_clips_stack)
        layout.addWidget(self._storage_clips_card, 2)
        self._storage_lists_layout = layout

        return page_scroll_area(column)

    @staticmethod
    def _card_around(title: str, inner: QWidget) -> QWidget:
        card, layout = section_card(title)
        layout.addWidget(inner)
        return card

    @staticmethod
    def _make_tree(columns, spec, *, settings_key: str) -> QTreeWidget:
        tree = rows_mod.StorageTree(columns, spec, settings_key=settings_key)
        tree.setMinimumHeight(180)
        enable_sorting(tree, None)
        return tree

    def _build_storage_header(self) -> QWidget:
        card, layout = section_card()
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        self._storage_title = SectionHeader("")
        title_row.addWidget(self._storage_title)
        title_row.addStretch(1)
        self._storage_headline = QLabel("")
        title_row.addWidget(self._storage_headline)
        layout.addLayout(title_row)

        self._storage_path = secondary_label()
        self._storage_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(self._storage_path)

        self._storage_bar = VolumeBar(height=TALL_BAR_HEIGHT, describe=False)
        layout.addWidget(self._storage_bar)

        self._storage_legend = QGridLayout()
        self._storage_legend.setContentsMargins(0, SPACE_XS, 0, 0)
        self._storage_legend.setHorizontalSpacing(SPACE_MD)
        self._storage_legend.setVerticalSpacing(SPACE_XS)
        layout.addLayout(self._storage_legend)

        self._storage_note = muted_label()
        self._storage_note.setWordWrap(True)
        layout.addWidget(self._storage_note)
        return card

    def _build_storage_free_bar(self) -> QWidget:
        """The one action on the page, and what it is about to do."""
        bar = QWidget()
        bar.setObjectName("storageFreeBar")
        bar.setStyleSheet(
            f"QWidget#storageFreeBar {{ border: 1px solid {BORDER};"
            f" border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout()
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        self._storage_finding = QLabel(NOTHING_TO_FREE)
        self._storage_finding.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._storage_finding, 1)

        self._storage_delete_btn = QPushButton(FREE_SELECTED)
        self._storage_delete_btn.setEnabled(False)
        self._storage_delete_btn.clicked.connect(self._on_storage_delete_clicked)
        row.addWidget(self._storage_delete_btn)

        # Under the row, wrapped: it is the longest sentence on the page and it
        # only appears once the button is armed, so a line of its own costs
        # nothing and keeps the bar off the width of two sentences plus a button.
        self._storage_warning = muted_label()
        self._storage_warning.setWordWrap(True)
        self._storage_warning.setVisible(False)
        stack = QVBoxLayout()
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(SPACE_XS)
        stack.addLayout(row)
        stack.addWidget(self._storage_warning)
        bar.setLayout(stack)

        # Owned by the window rather than the button, so it can be stopped from
        # anywhere the selection changes underneath it.
        self._storage_arm = QTimer(self)
        self._storage_arm.setSingleShot(True)
        self._storage_arm.setInterval(DELETE_ARM_MS)
        self._storage_arm.timeout.connect(self._paint_storage_action)
        return bar

    # --- navigation ---------------------------------------------------------

    def _open_storage_page(self, root: str) -> None:
        """Go to this drive's page, or back to Results if it is already showing."""
        if self._storage_root == root and self._current_section() == "storage":
            self._set_simple_section("browse")
            return
        self._storage_root = root
        self._storage_scan_id += 1
        self._storage_inventory = None
        self._set_simple_section("storage")

    def _sync_storage_buttons(self) -> None:
        """Light the drive button whose page is showing, and no other."""
        bars = getattr(self, "_storage_bars", None)
        if bars is None:
            return
        showing = self._current_section() == "storage"
        bars.set_selected_root(self._storage_root if showing else None)

    # --- scanning -----------------------------------------------------------

    def _refresh_storage_page(self) -> None:
        """Re-read the drive on screen, off the thread painting it.

        The store is read here, on the GUI thread, and the worker is handed
        plain data: SurveyStore is thread-confined, and a worker that opened one
        would leave a connection behind on every scan.
        """
        root = self._storage_root
        if root is None or not hasattr(self, "_storage_runs"):
            return
        self._refresh_storage_header()
        if self._storage_page_scanning:
            return

        out_root = Path(self._out_root_input.text()).expanduser()
        entries = list(getattr(self, "_data_entries", None) or [])
        clips = list(getattr(self, "_video_entries", None) or [])
        known = dict(self._storage_breakdowns)
        self._storage_scan_id += 1
        scan_id = self._storage_scan_id
        self._storage_page_scanning = True

        def worker() -> None:
            try:
                found = inventory_mod.read_mount(
                    root, out_root, entries=entries, clips=clips, known=known
                )
            except Exception:
                logger.exception("Could not read the drive %s", root)
                found = MountInventory(root=root)
            finally:
                self._storage_page_scanning = False
            try:
                self._sig_storage_page.emit((scan_id, found))
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the drive was read")

        threading.Thread(target=worker, daemon=True, name="storage-page-scan").start()

    def _apply_storage_page_scan(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        scan_id, found = payload
        # Somebody has switched drives or left the page since this was asked for.
        if scan_id != self._storage_scan_id or not isinstance(found, MountInventory):
            return
        self._storage_inventory = found
        for run in found.runs:
            if run.breakdown is not None:
                self._storage_breakdowns[run.dir_name] = run.breakdown
        self._fill_storage_lists()

    def _fill_storage_lists(self) -> None:
        found = self._storage_inventory
        if found is None:
            return
        open_run = None
        active = getattr(self, "_active_run_dir", None)
        if active is not None:
            open_run = Path(active).name

        for tree in (self._storage_runs, self._storage_clips):
            tree.blockSignals(True)
        rows_mod.fill_runs(self._storage_runs, found.runs, found.others, open_run=open_run)
        rows_mod.fill_clips(self._storage_clips, found.clips)
        for tree in (self._storage_runs, self._storage_clips):
            tree.blockSignals(False)

        has_runs = bool(found.runs or found.others)
        has_clips = bool(found.clips)
        self._storage_runs_stack.setCurrentIndex(0 if has_runs else 1)
        self._storage_clips_stack.setCurrentIndex(0 if has_clips else 1)
        # An empty list keeps its heading and its message, but stops reserving a
        # share of the page: a stacked layout takes its minimum from its tallest
        # page, so the empty one inherited the tree's own 180px floor and half
        # the window went to saying there was nothing on the drive.
        self._set_list_share(self._storage_runs_card, 3 if has_runs else 0)
        self._set_list_share(self._storage_clips_card, 2 if has_clips else 0)
        self._refresh_storage_selection()

    def _set_list_share(self, card: QWidget, share: int) -> None:
        """How much of the page's height one list card claims."""
        self._storage_lists_layout.setStretchFactor(card, share)
        card.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding if share else QSizePolicy.Policy.Maximum,
        )

    # --- the header ---------------------------------------------------------

    def _storage_volume(self) -> VolumeUsage | None:
        bars = getattr(self, "_storage_bars", None)
        if bars is None:
            return None
        for button in bars.buttons:
            volume = button.usage()
            if volume is not None and volume.root == self._storage_root:
                return volume
        return None

    def _refresh_storage_header(self) -> None:
        """Repeat the hover card's figures at the top of the page it opened."""
        volume = self._storage_volume()
        if volume is None:
            # The drive has gone. The last figures stay on screen rather than
            # blanking, and the lists say what has happened.
            self._storage_runs_stack.setCurrentIndex(1)
            self._storage_clips_stack.setCurrentIndex(1)
            for stack in (self._storage_runs_stack, self._storage_clips_stack):
                empty = stack.widget(1)
                if isinstance(empty, EmptyState):
                    empty.set_text(DISCONNECTED, DISCONNECTED_HINT)
            return

        self._storage_title.setText(volume.label)
        self._storage_headline.setText(volume_headline(volume))
        self._storage_headline.setStyleSheet(
            f"color: {alert_colour(volume) or WINDOW_TEXT}; font-weight: {WEIGHT_SEMIBOLD};"
        )
        self._storage_path.setText(volume.root)
        self._storage_bar.set_usage(volume)
        self._fill_storage_legend(volume)

        notes = []
        if volume.unmeasured_items:
            notes.append(
                f"{volume.unmeasured_items} items of unknown size, counted under other used."
            )
        found = self._storage_inventory
        if found is not None and not found.holds_out_root:
            notes.append(ELSEWHERE.format(name=Path(self._out_root_input.text()).name))
        self._storage_note.setText(" ".join(notes))
        self._storage_note.setVisible(bool(notes))

    def _fill_storage_legend(self, volume: VolumeUsage) -> None:
        while self._storage_legend.count():
            item = self._storage_legend.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        figures = volume_rows(volume)
        for row, (colour, label, size, percent) in enumerate(figures):
            # Hollow for free, which is the groove with nothing painted over it.
            swatch = QLabel("□" if row == len(figures) - 1 else "■")
            swatch.setStyleSheet(f"color: {colour};")
            self._storage_legend.addWidget(swatch, row, 0)
            self._storage_legend.addWidget(QLabel(label), row, 1)
            value = QLabel(size)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._storage_legend.addWidget(value, row, 2)
            share = muted_label(percent)
            share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._storage_legend.addWidget(share, row, 3)
        total = muted_label(f"Free: {format_bytes(volume.free_bytes)} of {format_bytes(volume.total_bytes)}")
        self._storage_legend.addWidget(total, self._storage_legend.rowCount(), 1, 1, 3)
        self._storage_legend.setColumnStretch(1, 1)

    # --- selection and arming ----------------------------------------------

    def _on_storage_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        # Qt's own auto-tristate propagates the tick both ways, so the parent
        # row is the bulk choice and needs no cascade of ours.
        if column == 0:
            self._refresh_storage_selection()

    def _on_storage_row_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """The folder button on a row: show what it names, wherever that is.

        Every row that names something on disk carries one, including the rows
        this page refuses to delete. Being told no is a good deal less useful
        than being shown where the thing is.
        """
        if column != rows_mod.COL_OPEN:
            return
        target = rows_mod.reveal_target(item)
        if target is None:
            return
        if not reveal_in_file_manager(Path(target)):
            self._status_label.setText(REVEAL_FAILED)

    def _refresh_storage_selection(self) -> None:
        """Total what is ticked, and disarm: the armed selection has changed."""
        self._disarm_storage()
        run_bytes, run_rows, grave = rows_mod.selected_bytes(self._storage_runs)
        clip_bytes, clip_rows, _ = rows_mod.selected_bytes(self._storage_clips)
        total, count = run_bytes + clip_bytes, run_rows + clip_rows

        partial = any(not run.measured for run in (self._storage_inventory.runs if self._storage_inventory else ()))
        if not count:
            self._storage_finding.setText(NOTHING_TO_FREE)
        else:
            lead = "At least " if partial else ""
            self._storage_finding.setText(
                f"{lead}{format_bytes(total)} can be freed. Selected: {count} items"
            )
        warning = GRAVE_WARNING if grave else ""
        self._storage_delete_btn.setEnabled(bool(count) and not self._run_in_flight())
        if self._run_in_flight():
            warning = RUN_IN_FLIGHT
        self._storage_warning.setText(warning)
        # Hidden when it says nothing, or its wrapped line reserves height on
        # every drive that has nothing to warn about.
        self._storage_warning.setVisible(bool(warning))

    def _disarm_storage(self) -> None:
        if self._storage_arm.isActive():
            self._storage_arm.stop()
        self._paint_storage_action()

    def _paint_storage_action(self) -> None:
        """The button asks the question a dialog would, and answers it itself."""
        armed = self._storage_arm.isActive()
        self._storage_delete_btn.setText(FREE_ARMED if armed else FREE_SELECTED)
        self._storage_delete_btn.setStyleSheet(
            f"color: {ERROR}; font-weight: {WEIGHT_SEMIBOLD};" if armed else ""
        )
        for tree in (self._storage_runs, self._storage_clips):
            tree.blockSignals(True)
            rows_mod.set_armed(tree, armed)
            tree.blockSignals(False)

    # --- deleting -----------------------------------------------------------

    def _on_storage_delete_clicked(self) -> None:
        """Arm on the first click, delete on the second."""
        if not self._storage_arm.isActive():
            self._storage_arm.start()
            self._paint_storage_action()
            return
        self._storage_arm.stop()
        self._paint_storage_action()
        self._run_storage_deletes()

    def _run_storage_deletes(self) -> None:
        if self._run_in_flight():
            return
        out_root = Path(self._out_root_input.text()).expanduser()
        found = self._storage_inventory
        if found is None:
            return
        runs = {run.dir_name: run for run in found.runs}
        others = {item.label: item for item in found.others}
        clips = {str(clip.video_id): clip for clip in found.clips}
        store = self._try_survey_store()

        freed = Reclaimed()
        touched_runs: set[str] = set()
        deleted_clips: list[str] = []
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            done: set[str] = set()
            for item in rows_mod.walk(self._storage_runs):
                if item.checkState(0) != Qt.CheckState.Checked:
                    continue
                # Parents come first, so a run already taken whole does not have
                # its tiers deleted again out from under it.
                parent = item.parent()
                if parent is not None and parent.data(0, rows_mod.ROLE_RUN) in done:
                    continue
                dir_name = item.data(0, rows_mod.ROLE_RUN)
                if item.childCount() and dir_name is not None:
                    done.add(dir_name)
                freed += self._delete_one(out_root, item, runs, others, store, touched_runs)
            for item in rows_mod.walk(self._storage_clips):
                if item.checkState(0) != Qt.CheckState.Checked:
                    continue
                clip = clips.get(item.data(0, rows_mod.ROLE_CLIP))
                if clip is None or store is None:
                    continue
                try:
                    freed += delete_input_clip(clip, store, confirmed_name=item.text(0))
                except ReclaimError as exc:
                    logger.warning("Refused to delete %s: %s", item.text(0), exc)
                    continue
                deleted_clips.append(str(clip.video_id))
                self._clip_link_cache.pop(clip.path, None)
        finally:
            QGuiApplication.restoreOverrideCursor()

        self._after_storage_delete(freed, touched_runs, deleted_clips)

    def _delete_one(
        self,
        out_root: Path,
        item: QTreeWidgetItem,
        runs: dict,
        others: dict,
        store,
        touched: set[str],
    ) -> Reclaimed:
        """One ticked row, dispatched on what kind of row it turned out to be."""
        tier = item.data(0, rows_mod.ROLE_TIER)
        dir_name = item.data(0, rows_mod.ROLE_RUN)
        try:
            # A ticked run row is the whole folder. That is what its tick means
            # on screen: every tier under it is ticked with it.
            if tier is None and dir_name in runs:
                touched.add(dir_name)
                return delete_run_folder(out_root, runs[dir_name].run_dir)
            if tier is not None and dir_name in runs:
                run = runs[dir_name]
                if run.breakdown is None:
                    return Reclaimed()
                touched.add(dir_name)
                return delete_tier(out_root, run.run_dir, tier, run.breakdown)
            label = item.data(0, rows_mod.ROLE_ITEM)
            if label in others:
                return delete_other(out_root, others[label], store)
        except (ReclaimError, OSError, ValueError) as exc:
            logger.warning("Refused to delete %s: %s", item.text(0), exc)
        return Reclaimed()

    def _after_storage_delete(
        self, freed: Reclaimed, touched_runs: set[str], deleted_clips: list[str]
    ) -> None:
        """Put every cached figure that just went stale back in play.

        Order matters: the run sizes have to be dropped before the catalogue is
        re-read, and the clip link answers before the library is rebuilt, or the
        page redraws from what was true a moment ago.
        """
        for dir_name in touched_runs:
            self._storage_breakdowns.pop(dir_name, None)
            if not (Path(self._out_root_input.text()).expanduser() / dir_name).exists():
                self._run_size_cache.pop(dir_name, None)
            else:
                self._run_size_stale.add(dir_name)

        for video_id in deleted_clips:
            # The scan keeps a path's answer for the session, so the entry has
            # to go before the recheck can mean anything.
            self._recheck_clip_link(video_id)

        store = self._try_survey_store()
        if deleted_clips and store is not None:
            self._refresh_video_library(store)
        self._refresh_data_manager()
        self._refresh_storage_page()

        if freed.freed_bytes:
            source = "runs" if touched_runs else "files"
            self._status_label.setText(
                f"Freed {format_bytes(freed.freed_bytes)} from {freed.items} {source}."
            )
