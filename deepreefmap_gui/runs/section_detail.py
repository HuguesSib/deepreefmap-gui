"""What one pass of a clip is, and which sessions have run it.

A pass outlives any one attempt at it: the same cutout can be processed in
several sessions, and comparing those repeats is the point of processing it more
than once. The clip pane says what footage exists; this says what has been asked
of one piece of it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core.fonts import tabular
from deepreefmap_gui.core.icons import (
    DEFAULT_INK,
    ICON_SM,
    check_icon,
    icon_pixmap,
    status_dot_icon,
    upload_icon,
)
from deepreefmap_gui.core.theme import (
    DISABLED_FG,
    ERROR,
    SPACE_SM,
    SPACE_XS,
    TEXT_MUTED,
    WARNING,
)
from deepreefmap_gui.core.widgets import (
    STATUS_COLORS,
    ElidingLabel,
    direction_html,
    fact_link,
    muted_label,
)
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.pass_rename import RENAME_ACTION
from deepreefmap_gui.runs.run_detail import DetailCard
from deepreefmap_gui.runs.video_rows import icon_button, set_button_dead
from deepreefmap_gui.survey import statuses
from deepreefmap_gui.survey.models import RunRecord, TransectPass

# The href behind the transect-and-direction fact. Both are set in one dialog,
# so the fact that shows them is the way into it.
_FILING_LINK = "filing"

_NO_SESSION = "No session recorded"

ARCHIVE_RUN = "Archive this run's outputs"
ARCHIVE_RUN_TOOLTIP = "Send this run's outputs to the registry's archive."
# Why the icon is dead on a run that never finished. The other reasons a run
# cannot be archived (no database row, outputs cleared away) belong to the run
# card in Browse, which can see them; a row here always has its record.
ARCHIVE_UNFINISHED = "Only a finished run's outputs can be archived."
ARCHIVING = "Archiving…"

# What the registry holds of one run, as its icon says it.
_ARCHIVE_FACES = {
    "archived": "Outputs on server. Press to verify them again.",
    "partial": "Some outputs are on the server. Press to send the rest.",
    "pending": "Offered to the registry, not verified yet. Press to resume.",
    "uploading": ARCHIVING,
    "failed": "The registry could not verify an upload. Press to archive again.",
}


def section_window(pass_: TransectPass) -> str:
    """The pass's own name: where it starts and stops in the clip."""
    end = pass_.end_s
    tail = "end" if end is None else f"{int(end) // 60}:{int(end) % 60:02d}"
    return f"{int(pass_.begin_s) // 60}:{int(pass_.begin_s) % 60:02d}-{tail}"


def _length(pass_: TransectPass) -> str:
    if pass_.end_s is None:
        return "to the end of the clip"
    seconds = int(round(max(0.0, pass_.end_s - pass_.begin_s)))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _short_date(stamp: str | None) -> str:
    """The day a run was made, in local time.

    Local rather than the stored UTC, so it agrees with the session label it is
    compared against; the two disagreed either side of midnight UTC and the row
    then printed the same day twice.
    """
    if not stamp:
        return "unknown"
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return stamp.split("T")[0] or "unknown"


# Every outcome a run can end in, so the column is measured against the widest
# word it will ever hold rather than against the row in front of it.
_OUTCOMES = ("succeeded", "failed", "unfinished", "running", "queued")


def _outcome_width(label: QLabel) -> int:
    """Wide enough for whichever outcome a run can end in, so the column holds still."""
    metrics = label.fontMetrics()
    return max(metrics.horizontalAdvance(statuses.status_label(s)) for s in _OUTCOMES) + SPACE_XS


def _date_width(label: QLabel) -> int:
    """One date's width, from a specimen rather than from the row in front."""
    return label.fontMetrics().horizontalAdvance("2026-08-25") + SPACE_XS


class RunRow(QWidget):
    """One session's attempt at this pass, with its own archive control.

    The trailing button follows the pass rows' icon-button convention (cart,
    trim, delete): one glyph wearing the probe's answer, with the words in the
    tooltip. A run that cannot be archived keeps the glyph in the disabled ink
    and says why in the tooltip rather than hiding it, and the press does
    nothing. It stays enabled for the reason SectionRow gives: a disabled
    QToolButton takes no mouse events, so the tooltip explaining the refusal
    would never reach the pointer that went looking for it.
    """

    activated = Signal(str)
    archive_requested = Signal(object)

    def __init__(self, run: RunRecord, session: str, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.run = run
        self._session = session
        self._date = _short_date(run.created_at)
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 0, SPACE_SM, 0)
        row.setSpacing(SPACE_SM)
        dot = QLabel()
        dot.setFixedWidth(ICON_SM)
        dot.setPixmap(icon_pixmap(status_dot_icon(STATUS_COLORS.get(run.status, TEXT_MUTED))))
        row.addWidget(dot)
        # Three fields at their own positions rather than one joined string: the
        # outcome and the date used to start wherever the words before them
        # ended, so no two rows agreed and the list could not be read down.
        # Ignored rather than Preferred: a session is named by whoever ran it,
        # and one long name made the row wider than the pane it sits in, which
        # carried the archive button off the right-hand edge of a list that has
        # no horizontal scrollbar to go looking for it with.
        self.label = ElidingLabel(session)
        row.addWidget(self.label, 1)

        self.outcome = QLabel(statuses.status_label(run.status))
        self.outcome.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.outcome.setFixedWidth(_outcome_width(self.outcome))
        self.outcome.setStyleSheet(f"color: {STATUS_COLORS.get(run.status, TEXT_MUTED)};")
        row.addWidget(self.outcome)

        # A session is identified by when it began, so its label already opens
        # with the day it ran; printing that again beside it said the same thing
        # twice. startswith, not equality: the label carries a time as well.
        self.date = QLabel("" if session.startswith(self._date) else self._date)
        self.date.setFont(tabular(self.date.font()))
        self.date.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Nothing reserved when there is nothing to show: within one list every
        # session is named the same way, so the column is either there for all
        # of them or for none, and an empty one only costs the name its width.
        self.date.setFixedWidth(_date_width(self.date) if self.date.text() else 0)
        self.date.setStyleSheet(f"color: {TEXT_MUTED};")
        row.addWidget(self.date)

        self.setToolTip(tooltip)
        self.archive_btn = icon_button(upload_icon(), ARCHIVE_RUN, ARCHIVE_RUN_TOOLTIP)
        self.archive_btn.clicked.connect(self._on_archive_clicked)
        row.addWidget(self.archive_btn)
        self.set_archive_state(None)

    def text(self) -> str:
        """The whole line, however much of it the pane has room to draw."""
        return "  ".join(
            part for part in (self._session, self.outcome.text(), self.date.text()) if part
        )

    @property
    def archivable(self) -> bool:
        """A run that did not finish wrote no outputs to send."""
        return self.run.status == "succeeded"

    def set_archive_state(self, state: str | None, note: str | None = None) -> None:
        """Dress the button as what the registry holds of this run, if asked.

        ``note`` replaces the state's stock tooltip, for a failure's own words.
        """
        set_button_dead(self.archive_btn, not self.archivable)
        if not self.archivable:
            self.archive_btn.setIcon(upload_icon(color=QColor(DISABLED_FG)))
            self.archive_btn.setToolTip(ARCHIVE_UNFINISHED)
            return
        told = _ARCHIVE_FACES.get(state or "")
        if told is None:
            self.archive_btn.setIcon(upload_icon(color=QColor(DEFAULT_INK)))
            self.archive_btn.setToolTip(ARCHIVE_RUN_TOOLTIP)
            return
        # A tick for content already up, the upload glyph in the colour of what
        # is still to do. The same vocabulary the run card in Browse paints.
        if state == "archived":
            self.archive_btn.setIcon(check_icon())
        else:
            self.archive_btn.setIcon(upload_icon(color=QColor(ERROR if state == "failed" else WARNING)))
        self.archive_btn.setToolTip(note or told)

    def _on_archive_clicked(self) -> None:
        if self.archivable:
            self.archive_requested.emit(self.run.id)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.activated.emit(self.run.run_dir_name)
        super().mouseDoubleClickEvent(event)


class SectionDetailPanel(DetailCard):
    """A titled card describing one pass and the sessions that ran it."""

    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    delete_requested = Signal(str)
    rename_requested = Signal(str)
    run_activated = Signal(str)
    # The database run id whose outputs to archive, from whichever row was pressed.
    archive_run_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.body

        layout.addWidget(muted_label("Sessions this pass has run in"))

        self.run_list = QListWidget()
        self.run_list.setAlternatingRowColors(True)
        self.run_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.run_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The list is always the list, empty or not, the same way the clip's own
        # section list is. Swapping it for a full-pane empty state moved every
        # control below it and left the pane a different shape for a section
        # that had run and one that had not.
        layout.addWidget(self.run_list, 1)

        # A menu rather than a row of buttons the pane cannot hold without
        # truncating every label. The cart is not among them: the section's own
        # row carries that, and two cart controls on one screen disagree the
        # moment one of them is stale. Archiving is not either: each run row
        # above carries its own icon, in the style of the section rows.
        self.more_btn = QToolButton()
        self.more_btn.setText("More…")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        # The delete gate explains itself in a tooltip, so tooltips must show.
        menu.setToolTipsVisible(True)
        self.menu_actions = self._fill_section_actions(menu)
        self.more_btn.setMenu(menu)
        self.add_actions(None, self.more_btn)
        # The filing fact is the way into the dialog that sets it.
        self.facts.link_activated.connect(self._on_fact_link)

        self._pass: TransectPass | None = None
        self._run_rows: list[RunRow] = []

    def _section_action_specs(self) -> tuple[tuple[str | None, str, object], ...]:
        """Everything the menu offers on this pass, in one list.

        A None key is a separator.
        """
        return (
            ("rename", RENAME_ACTION, self._emit_rename),
            ("retrim", "Adjust trim…", self._emit_retrim),
            ("reassign", "Change transect…", self._emit_reassign),
            (None, "", None),
            ("delete", "Delete pass", self._emit_delete),
        )

    def _fill_section_actions(self, menu: QMenu) -> dict[str, QAction]:
        actions = {}
        for key, label, slot in self._section_action_specs():
            if key is None:
                menu.addSeparator()
                continue
            actions[key] = menu.addAction(label, slot)
        return actions

    @property
    def pass_(self) -> TransectPass | None:
        return self._pass

    def _pass_id(self) -> str:
        return "" if self._pass is None else str(self._pass.id)

    def _emit_rename(self) -> None:
        self.rename_requested.emit(self._pass_id())

    def _emit_retrim(self) -> None:
        self.retrim_requested.emit(self._pass_id())

    def _emit_reassign(self) -> None:
        self.reassign_requested.emit(self._pass_id())

    def _emit_delete(self) -> None:
        self.delete_requested.emit(self._pass_id())

    def run_rows(self) -> list[RunRow]:
        return list(self._run_rows)

    def paint_archive_states(
        self,
        state_for_run: Callable[[object], str | None],
        note_for_run: Callable[[object], str | None] | None = None,
    ) -> None:
        """Dress each run row's archive icon from the probe's answers."""
        for row in self._run_rows:
            note = None if note_for_run is None else note_for_run(row.run.id)
            row.set_archive_state(state_for_run(row.run.id), note)

    def _on_fact_link(self, href: str) -> None:
        if href == _FILING_LINK:
            self._emit_reassign()

    def show_section(
        self,
        pass_: TransectPass,
        *,
        clip_name: str,
        transect_name: str | None,
        status: str,
        runs: list[RunRecord],
        session_name,
        in_cart: bool,
        output_bytes: int = 0,
    ) -> None:
        """Describe one pass. ``session_name`` resolves a run's batch id."""
        self.title.setText(section_window(pass_))
        self.set_status(status, STATUS_COLORS.get(status, TEXT_MUTED))
        # Transect and direction on one row, as one link. They are set together
        # in one dialog, and which way a swim went means nothing without the
        # line it went along. Cart membership is not here at all: the section's
        # own row carries that control and shows its state on it.
        filing = f"{transect_name or 'Unassigned'} · {direction_html(pass_.direction)}"
        rows = [
            ("Clip", clip_name),
            ("Transect", fact_link(filing, _FILING_LINK)),
            ("Length", _length(pass_)),
        ]
        # What this cut has cost so far, which is the figure worth having when
        # deciding whether to run it again.
        made = f"{len(runs)} run{'' if len(runs) == 1 else 's'}" if runs else "none yet"
        if output_bytes:
            made += f" · {format_bytes(output_bytes)}"
        rows.append(("Runs", made))
        self.facts.set_rows(rows)

        self.run_list.clear()
        self._run_rows = []
        # Newest first: the question asked of a repeated section is what happened
        # last time, not what happened first.
        for run in sorted(runs, key=lambda r: r.created_at or "", reverse=True):
            name = session_name(run.batch_id) or _NO_SESSION
            row = RunRow(run, name, run.error or run.run_dir_name)
            row.activated.connect(self.run_activated)
            row.archive_requested.connect(self.archive_run_requested)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.run_list.addItem(item)
            self.run_list.setItemWidget(item, row)
            self._run_rows.append(row)
        if not runs:
            # In the list rather than instead of it, so the pane keeps its shape
            # and the empty case is answered where the answer would appear.
            empty = QListWidgetItem(
                "Not processed yet. Add it to the cart to run it."
                if not in_cart
                else "Not processed yet. It is in the cart for the next session."
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setForeground(QColor(TEXT_MUTED))
            self.run_list.addItem(empty)

        # A section with runs is the record of what they processed, so it cannot
        # go while they are still there.
        delete = self.menu_actions["delete"]
        delete.setEnabled(not runs)
        delete.setToolTip(
            "This pass has runs. Delete them in Browse first." if runs else "Delete this pass from the clip."
        )
        self._pass = pass_

    def clear(self) -> None:
        super().clear()
        self.run_list.clear()
        self._run_rows = []
        self._pass = None
