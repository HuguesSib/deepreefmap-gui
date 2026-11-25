"""The expedition's missing data as a checklist that is worked down where it stands.

Every line is something an import could not read off the folders. Pressing
Enter on a line (or double-clicking it) asks for exactly that value, writes it,
and moves to the next line, so the list is filled without leaving it. Lines of
the same kind selected together are filled at once: one review verdict for a
whole pass's clips, one tape length for every transect that lacks it. A trim
opens the clip in the scrubber, one pass after another.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.widgets import muted_label
from deepreefmap_gui.runs.clip_details import REVIEWS, RIG_POSITIONS
from deepreefmap_gui.survey.completeness import (
    CLIP,
    CLIP_CAMERA,
    CLIP_REVIEW,
    CLIP_RIG,
    EXPEDITION,
    EXPEDITION_DATES,
    LEVELS,
    PASS,
    PASS_DIRECTION,
    PASS_NOT_RUN,
    PASS_QUALITY,
    PASS_RUN_FAILED,
    PASS_TRANSECT,
    PASS_UNTRIMMED,
    TRANSECT,
    TRANSECT_COORDINATES,
    TRANSECT_DEPTH,
    TRANSECT_LENGTH,
    TRANSECT_SITE,
    Gap,
    capture_span,
    missing_data,
)
from deepreefmap_gui.survey.models.importers import parse_latlon
from deepreefmap_gui.survey.models.transect_pass import PASS_DIRECTIONS, PASS_QUALITIES
from deepreefmap_gui.survey.models.video_asset import VIEW_DOWN
from deepreefmap_gui.survey.store import SurveyStore

LEVEL_TITLES = {
    EXPEDITION: "Expedition",
    TRANSECT: "Transects",
    PASS: "Passes",
    CLIP: "Clips",
}
GAP_ROLE = Qt.ItemDataRole.UserRole
COMPLETE_NOTE = "Nothing missing: every transect, pass and clip of this expedition is filled in."
FILL_HINT = (
    "Select a line and press Enter (or double-click) to fill it in here. "
    'Select a kind ("review (294)") to fill all of its lines at once.'
)
FILL_LABEL = "Fill in…"
COORDINATE_HINT = "29.4512 34.9731  or  29°27.07'N 34°58.39'E"
LOCKED_NOTE = "Some of these were made on another laptop and were left as they are."


@dataclass(frozen=True)
class ChecklistActions:
    """What the checklist needs from the window, beyond the store.

    ``trim`` opens a pass in the scrubber and saves the window chosen there;
    ``queue`` puts a pass in the processing queue; ``read_only`` says whether a
    row belongs to another laptop and must not be changed here.
    """

    trim: Callable[[uuid.UUID], None]
    queue: Callable[[uuid.UUID], None]
    read_only: Callable[[object], bool]


class _Cancelled:
    """A choice that was not made, told apart from a choice of None."""


CANCELLED = _Cancelled()


class _GapTree(QTreeWidget):
    """The list, with Enter meaning "fill in what is selected"."""

    def __init__(self, on_enter: Callable[[], None]) -> None:
        super().__init__()
        self._on_enter = on_enter

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_enter()
            return
        super().keyPressEvent(event)


class MissingDataDialog(QDialog):
    """Everything an expedition still lacks, grouped by level, filled in place."""

    def __init__(
        self,
        parent: QWidget | None,
        store: SurveyStore,
        campaign_id: uuid.UUID,
        actions: ChecklistActions,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._campaign_id = campaign_id
        self._actions = actions
        campaign = store.get_campaign(campaign_id)
        self.setWindowTitle(f"Missing data: {campaign.name if campaign else 'expedition'}")
        self.resize(820, 560)

        layout = QVBoxLayout(self)
        self.summary = muted_label("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = _GapTree(self.fill_selected)
        self.tree.setHeaderLabels(["Record", "Missing"])
        self.tree.setColumnWidth(0, 470)
        self.tree.setAccessibleName("Missing data by level")
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda *_: self.fill_selected())
        self.tree.itemSelectionChanged.connect(self._refresh_fill_button)
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.fill_btn = QPushButton(FILL_LABEL)
        self.fill_btn.setProperty("cta", "true")
        self.fill_btn.clicked.connect(self.fill_selected)
        row.addWidget(self.fill_btn)
        self.dates_btn = QPushButton("Use clip dates for the expedition")
        self.dates_btn.setProperty("quiet", "true")
        self.dates_btn.setToolTip("Set the first and last day from the clips' recording times.")
        self.dates_btn.clicked.connect(self._fill_dates_from_clips)
        row.addWidget(self.dates_btn)
        row.addStretch(1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setProperty("quiet", "true")
        self.refresh_btn.setToolTip("Read the list again after changes made elsewhere in the app.")
        self.refresh_btn.clicked.connect(lambda: self.refresh())  # noqa: PLW0108 (clicked passes a bool)
        row.addWidget(self.refresh_btn)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.refresh()

    # --- the list --------------------------------------------------------------

    def gaps(self) -> list[Gap]:
        return missing_data(self._store, self._campaign_id)

    def refresh(self, *, select_row: int | None = None, kinds: bool = False) -> None:
        """Rebuild the list; ``select_row`` puts the cursor back where work left off."""
        # Levels start open and kinds start folded, so the first view is the
        # summary; whatever was opened or folded since is kept across refreshes.
        folded: set = set()
        opened: set = set()
        for old in self._groups():
            if not old.isExpanded():
                folded.add(old.data(0, GAP_ROLE))
            for branch in _children(old):
                if branch.isExpanded():
                    opened.add((old.data(0, GAP_ROLE), branch.data(0, GAP_ROLE)))
        gaps = self.gaps()
        self.tree.clear()
        for level in LEVELS:
            at_level = [g for g in gaps if g.level == level]
            if not at_level:
                continue
            group = QTreeWidgetItem([f"{LEVEL_TITLES[level]} ({len(at_level)})", ""])
            group.setData(0, GAP_ROLE, level)
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(group)
            # One branch per kind of gap, so "every clip's review" is one line
            # to select: Enter on it fills the whole branch at once.
            by_kind: dict[str, list[Gap]] = {}
            for gap in at_level:
                by_kind.setdefault(gap.code, []).append(gap)
            for code, of_kind in by_kind.items():
                kind = QTreeWidgetItem([f"{of_kind[0].item} ({len(of_kind)})", "all of these"])
                kind.setData(0, GAP_ROLE, code)
                kind.setToolTip(0, f"Select to fill in {of_kind[0].item} for all {len(of_kind)} at once.")
                group.addChild(kind)
                for gap in of_kind:
                    item = QTreeWidgetItem([gap.subject, gap.item])
                    item.setData(0, GAP_ROLE, gap)
                    item.setToolTip(0, gap.subject)
                    kind.addChild(item)
                kind.setExpanded((level, code) in opened)
            group.setExpanded(level not in folded)
        self.summary.setText(COMPLETE_NOTE if not gaps else f"{len(gaps)} item(s) missing. {FILL_HINT}")
        begin, end = capture_span(self._store, self._campaign_id)
        self.dates_btn.setVisible(any(g.code == EXPEDITION_DATES for g in gaps) and begin is not None)
        if begin is not None:
            self.dates_btn.setText(f"Use clip dates for the expedition ({begin} to {end})")
        if select_row is not None:
            self._select_at(self.kinds() if kinds else self.lines(), select_row)
        self._refresh_fill_button()

    def lines(self) -> list[QTreeWidgetItem]:
        """Every gap line, top to bottom, folded branches included."""
        return [line for kind in self.kinds() for line in _children(kind)]

    def kinds(self) -> list[QTreeWidgetItem]:
        """Every kind branch, top to bottom."""
        return [kind for group in self._groups() for kind in _children(group)]

    def _groups(self) -> list[QTreeWidgetItem]:
        items = (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
        return [item for item in items if item is not None]

    def _select_at(self, items: list[QTreeWidgetItem], row: int) -> None:
        if not items:
            return
        item = items[min(row, len(items) - 1)]
        # A line folded away under its kind is not somewhere to leave the
        # cursor: the kind it sits in is the next thing to do.
        parent = item.parent()
        if isinstance(item.data(0, GAP_ROLE), Gap) and parent is not None and not parent.isExpanded():
            item = parent
        self.tree.clearSelection()
        self.tree.setCurrentItem(item)
        item.setSelected(True)

    def selected_gaps(self) -> list[Gap]:
        """The selected lines of one kind: the kind of the line the cursor is on.

        A selected kind branch stands for every line under it.
        """
        gaps: list[Gap] = []
        for item in self.tree.selectedItems():
            data = item.data(0, GAP_ROLE)
            if isinstance(data, Gap):
                gaps.append(data)
            elif item.parent() is not None:  # a kind branch
                gaps.extend(item.child(k).data(0, GAP_ROLE) for k in range(item.childCount()))
        if not gaps:
            return []
        current = self.tree.currentItem()
        lead = current.data(0, GAP_ROLE) if current is not None else None
        code = lead.code if isinstance(lead, Gap) else lead if lead in {g.code for g in gaps} else gaps[0].code
        return list(dict.fromkeys(g for g in gaps if g.code == code))

    def _refresh_fill_button(self) -> None:
        gaps = self.selected_gaps()
        self.fill_btn.setEnabled(bool(gaps))
        if not gaps:
            self.fill_btn.setText(FILL_LABEL)
        elif len(gaps) == 1:
            self.fill_btn.setText(f"Fill in {gaps[0].item}…")
        else:
            self.fill_btn.setText(f"Fill in {gaps[0].item} for {len(gaps)}…")

    # --- filling ---------------------------------------------------------------

    def fill_selected(self) -> None:
        """Ask for the selected lines' value, write it, and move on to the next line."""
        gaps = self.selected_gaps()
        filler = _FILLERS.get(gaps[0].code) if gaps else None
        if filler is None:
            return
        current = self.tree.currentItem()
        whole_kind = current is not None and not isinstance(current.data(0, GAP_ROLE), Gap)
        if whole_kind:
            row = next((i for i, kind in enumerate(self.kinds()) if kind is current), 0)
        else:
            row = next((i for i, line in enumerate(self.lines()) if line.data(0, GAP_ROLE) == gaps[0]), 0)
        if filler(self, gaps):
            # The same place again: what was just filled has gone, so it now
            # holds the next thing to do, and Enter carries on down the list.
            self.refresh(select_row=row, kinds=whole_kind)
        self.tree.setFocus()

    def _editable(self, rows: Sequence) -> list:
        """The rows this laptop may change. Any left alone are said to be."""
        present = [row for row in rows if row is not None]
        mine = [row for row in present if not self._actions.read_only(row)]
        if len(mine) < len(present):
            QMessageBox.information(self, "Made on another laptop", LOCKED_NOTE)
        return mine

    def _ask(self, title: str, rows: Sequence[tuple[str, QWidget]], note: str = "") -> bool:
        """A small form: a note, the fields, OK and Cancel."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            form.addRow(hint)
        for label, field in rows:
            form.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if rows:
            rows[0][1].setFocus()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        # The caller reads the fields after this returns, but the form owns
        # them and goes with it, so take them back first.
        for _label, field in rows:
            field.setParent(None)
        dialog.deleteLater()
        return accepted

    def _choose(self, title: str, label: str, choices: Sequence[tuple[str, object]], note: str = "") -> object:
        """One pick from a list, or CANCELLED."""
        combo = QComboBox()
        for text, value in choices:
            combo.addItem(text, value)
        return combo.currentData() if self._ask(title, [(label, combo)], note) else CANCELLED

    @staticmethod
    def _for(gaps: Sequence[Gap]) -> str:
        return gaps[0].subject if len(gaps) == 1 else f"{len(gaps)} selected"

    # Expedition ----------------------------------------------------------------

    def _fill_dates(self, gaps: Sequence[Gap]) -> bool:
        campaigns = self._editable([self._store.get_campaign(gaps[0].record_id)])
        if not campaigns:
            return False
        campaign = campaigns[0]
        suggested_begin, suggested_end = capture_span(self._store, campaign.id)
        begin = _date_edit(campaign.begin_date or suggested_begin)
        end = _date_edit(campaign.end_date or suggested_end)
        note = "Suggested from the clips' recording times." if suggested_begin else ""
        if not self._ask(f"Dates of {campaign.name}", [("First day", begin), ("Last day", end)], note):
            return False
        first = begin.date().toString("yyyy-MM-dd")
        last = end.date().toString("yyyy-MM-dd")
        if last < first:  # ISO days order as text
            QMessageBox.warning(self, "Dates", "The last day comes before the first.")
            return False
        campaign.begin_date, campaign.end_date = first, last
        self._store.update_campaign(campaign)
        return True

    def _fill_dates_from_clips(self) -> None:
        campaigns = self._editable([self._store.get_campaign(self._campaign_id)])
        begin, end = capture_span(self._store, self._campaign_id)
        if not campaigns or begin is None:
            return
        campaign = campaigns[0]
        campaign.begin_date = campaign.begin_date or begin
        campaign.end_date = campaign.end_date or end
        self._store.update_campaign(campaign)
        self.refresh()

    # Transects -----------------------------------------------------------------

    def _transects(self, gaps: Sequence[Gap]) -> list:
        return self._editable([self._store.get_transect(g.record_id) for g in gaps])

    def _fill_coordinates(self, gaps: Sequence[Gap]) -> bool:
        """Both ends of each line in one form, asked again until they read."""
        filled = False
        for transect in self._transects(gaps):  # a line's ends are its own
            start, end = QLineEdit(), QLineEdit()
            for edit, (lat, lon) in (
                (start, (transect.start_lat, transect.start_lon)),
                (end, (transect.end_lat, transect.end_lon)),
            ):
                edit.setPlaceholderText(COORDINATE_HINT)
                edit.setMinimumWidth(edit.fontMetrics().horizontalAdvance(COORDINATE_HINT) + 24)
                if lat is not None and lon is not None:
                    edit.setText(f"{lat:.6f} {lon:.6f}")
            note = "Where the tape starts and ends, as the GPS gives them."
            while True:
                if not self._ask(f"Ends of {transect.name}", [("Start", start), ("End", end)], note):
                    return filled
                try:
                    transect.start_lat, transect.start_lon = parse_latlon(start.text())
                    transect.end_lat, transect.end_lon = parse_latlon(end.text())
                    break
                except ValueError as exc:
                    QMessageBox.warning(self, "Coordinates", f"Could not read that: {exc}")
            self._store.update_transect(transect)
            filled = True
        return filled

    def _fill_number(self, gaps: Sequence[Gap], attr: str, label: str, default: float, maximum: float) -> bool:
        transects = self._transects(gaps)
        if not transects:
            return False
        spin = QDoubleSpinBox()
        spin.setRange(0.1, maximum)
        spin.setDecimals(1)
        spin.setSuffix(" m")
        spin.setValue(getattr(transects[0], attr) or default)
        if not self._ask(f"{label} of {self._for(gaps)}", [(label, spin)]):
            return False
        for transect in transects:
            setattr(transect, attr, spin.value())
            self._store.update_transect(transect)
        return True

    def _fill_length(self, gaps: Sequence[Gap]) -> bool:
        return self._fill_number(gaps, "length_m", "Tape length", 50.0, 1000.0)

    def _fill_depth(self, gaps: Sequence[Gap]) -> bool:
        return self._fill_number(gaps, "depth_m", "Depth", 6.0, 200.0)

    def _fill_site(self, gaps: Sequence[Gap]) -> bool:
        from deepreefmap_gui.simple.catalogue_dialogs import SiteDialog

        transects = self._transects(gaps)
        if not transects:
            return False
        combo = QComboBox()
        for site in self._store.list_sites():
            combo.addItem(f"{site.name} ({site.country})" if site.country else site.name, site.id)
        new_btn = QPushButton("New site…")
        holder = QWidget()
        inner = QHBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(combo, 1)
        inner.addWidget(new_btn)

        def new_site() -> None:
            dialog = SiteDialog(self, self._store)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.site is not None:
                combo.addItem(dialog.site.name, dialog.site.id)
                combo.setCurrentIndex(combo.count() - 1)

        new_btn.clicked.connect(new_site)
        if combo.count() == 0:
            new_site()
        if combo.count() == 0 or not self._ask(f"Site of {self._for(gaps)}", [("Site", holder)]):
            return False
        for transect in transects:
            transect.site_id = combo.currentData()
            try:
                self._store.update_transect(transect)
            except (sqlite3.IntegrityError, ValueError) as exc:
                QMessageBox.warning(self, "Site", f"{transect.name}: {exc}")
        return True

    # Passes --------------------------------------------------------------------

    def _passes(self, gaps: Sequence[Gap]) -> list:
        return self._editable([self._store.get_pass(g.record_id) for g in gaps])

    def _set_on_passes(self, gaps: Sequence[Gap], attr: str, value: object) -> bool:
        if value is CANCELLED:
            return False
        for pass_ in self._passes(gaps):
            setattr(pass_, attr, value)
            self._store.update_pass(pass_)
        return True

    def _fill_pass_transect(self, gaps: Sequence[Gap]) -> bool:
        choices = [(t.name, t.id) for t in sorted(self._store.list_transects(), key=lambda t: t.name.lower())]
        if not choices:
            QMessageBox.information(self, "Transect", "There is no transect yet: draw one on the Transects page.")
            return False
        value = self._choose(f"Transect of {self._for(gaps)}", "Transect", choices)
        return self._set_on_passes(gaps, "transect_id", value)

    def _fill_direction(self, gaps: Sequence[Gap]) -> bool:
        choices = [(direction.capitalize(), direction) for direction in PASS_DIRECTIONS]
        value = self._choose(f"Direction of {self._for(gaps)}", "Direction", choices)
        return self._set_on_passes(gaps, "direction", value)

    def _fill_quality(self, gaps: Sequence[Gap]) -> bool:
        from deepreefmap_gui.simple.transect_picker import QUALITY_LABELS

        choices = [(QUALITY_LABELS[quality], quality) for quality in PASS_QUALITIES]
        note = "How the swim went, on the field sheets' scale."
        value = self._choose(f"Quality of {self._for(gaps)}", "Quality", choices, note)
        return self._set_on_passes(gaps, "quality", value)

    def _fill_trim(self, gaps: Sequence[Gap]) -> bool:
        """Each pass in the scrubber in turn; one left as it was ends the round."""
        trimmed = False
        for pass_ in self._passes(gaps):
            before = (pass_.begin_s, pass_.end_s)
            self._actions.trim(pass_.id)
            after = self._store.get_pass(pass_.id)
            if after is None or (after.begin_s, after.end_s) == before:
                break
            trimmed = True
        return trimmed

    def _fill_queue(self, gaps: Sequence[Gap]) -> bool:
        passes = self._passes(gaps)
        if not passes:
            return False
        answer = QMessageBox.question(self, "Process", f"Add {len(passes)} pass(es) to the processing queue?")
        if answer == QMessageBox.StandardButton.Yes:
            for pass_ in passes:
                self._actions.queue(pass_.id)
        # Queued is not processed: the lines stay until the runs exist.
        return False

    # Clips ---------------------------------------------------------------------

    def _videos(self, gaps: Sequence[Gap]) -> list:
        return self._editable([self._store.get_video(g.record_id) for g in gaps])

    def _set_on_videos(self, gaps: Sequence[Gap], attr: str, value: object) -> bool:
        if value is CANCELLED:
            return False
        for video in self._videos(gaps):
            setattr(video, attr, value)
            self._store.update_video(video)
        return True

    def _fill_camera(self, gaps: Sequence[Gap]) -> bool:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItems(self._store.camera_labels())
        combo.setCurrentText("")
        if (line := combo.lineEdit()) is not None:
            line.setPlaceholderText("Cam1")
        note = "The camera's name on the rig. Pick one already used or type a new one."
        if not self._ask(f"Camera of {self._for(gaps)}", [("Camera", combo)], note):
            return False
        name = combo.currentText().strip()
        return bool(name) and self._set_on_videos(gaps, "camera_label", name)

    def _fill_rig(self, gaps: Sequence[Gap]) -> bool:
        # A down-looking camera is offered here too, since that is where someone
        # going through the rig's cameras says so; it marks the clip, not a side.
        choices = [*((label, code) for code, label in RIG_POSITIONS), ("Looks down (not processed)", VIEW_DOWN)]
        note = "Down-looking clips keep their details but are left out of processing."
        value = self._choose(f"Rig position of {self._for(gaps)}", "Rig position", choices, note)
        if value == VIEW_DOWN:
            return self._set_on_videos(gaps, "view", VIEW_DOWN)
        return self._set_on_videos(gaps, "rig_position", value)

    def _fill_review(self, gaps: Sequence[Gap]) -> bool:
        choices = [(label, code) for code, label in REVIEWS if code != "unreviewed"]
        value = self._choose(f"Review of {self._for(gaps)}", "Review", choices)
        return self._set_on_videos(gaps, "review", value)


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    children = (item.child(i) for i in range(item.childCount()))
    return [child for child in children if child is not None]


def _date_edit(value: str | None) -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    edit.setDate(QDate.fromString(value, "yyyy-MM-dd") if value else QDate.currentDate())
    return edit


_FILLERS: dict[str, Callable[[MissingDataDialog, Sequence[Gap]], bool]] = {
    EXPEDITION_DATES: MissingDataDialog._fill_dates,
    TRANSECT_COORDINATES: MissingDataDialog._fill_coordinates,
    TRANSECT_LENGTH: MissingDataDialog._fill_length,
    TRANSECT_DEPTH: MissingDataDialog._fill_depth,
    TRANSECT_SITE: MissingDataDialog._fill_site,
    PASS_TRANSECT: MissingDataDialog._fill_pass_transect,
    PASS_DIRECTION: MissingDataDialog._fill_direction,
    PASS_UNTRIMMED: MissingDataDialog._fill_trim,
    PASS_QUALITY: MissingDataDialog._fill_quality,
    PASS_NOT_RUN: MissingDataDialog._fill_queue,
    PASS_RUN_FAILED: MissingDataDialog._fill_queue,
    CLIP_CAMERA: MissingDataDialog._fill_camera,
    CLIP_RIG: MissingDataDialog._fill_rig,
    CLIP_REVIEW: MissingDataDialog._fill_review,
}
