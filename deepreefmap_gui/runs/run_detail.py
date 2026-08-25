"""What one run is, and why it failed if it did.

One widget, shown twice: in Browse beside the run table, and in View mode beside
the cloud. A failure reason belongs here rather than in the status bar, which the
next event overwrites: the run that broke is still selected long after the
message that explained it has gone.

``DetailCard``, the card this and the clip pane are both built on, is defined
here as well, so the two open the same way rather than by coincidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QGuiApplication, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QLayoutItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import ICON_SM, check_icon, copy_icon, log_icon
from deepreefmap_gui.core.image_view import ClickableLabel, ImageDialog
from deepreefmap_gui.core.theme import (
    ERROR,
    FONT_LG_PT,
    SPACE_SM,
    SUCCESS,
    TEXT_MUTED,
    WARN_TEXT,
)
from deepreefmap_gui.core.widgets import (
    STATUS_COLORS,
    KeyValueList,
    StatusChip,
    direction_html,
    section_card,
)
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.cover_panel import CoverPanel, load_cover
from deepreefmap_gui.runs.run_cards import (
    format_timestamp,
    format_trim_range,
    frames_text,
    geometry_label,
    points_label,
    provenance_rows,
    recorded_text,
)
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import RunEntry

# Orthos are ~2000px wide and a couple of megabytes; the pane shows them at a
# few hundred. QImageReader decodes straight to this width rather than decoding
# full size and throwing most of it away, which is what makes arrowing down the
# run table stay responsive.
_ORTHO_DECODE_WIDTH = 640

# Bounded because the pane holds one run at a time and the cache only exists to
# make going back to the previous few selections instant.
_ORTHO_CACHE_MAX = 12

# Card padding either side of the strip, so it is sized off the pane rather
# than off its own label.
_ORTHO_H_MARGIN = 44

# The band the ortho is letterboxed into. Constant, because an ortho's height at
# a given width is its aspect ratio, and reserving exactly that meant a tall
# strip and a thin one reserved different heights -- so arrowing down the table
# shuffled everything below the picture. A run with no ortho at all holds the
# same band rather than collapsing it.
_ORTHO_BAND_HEIGHT = 132

# A fact the run does not have. The bare token, because test_design_system.py
# fails any other spelling of an em dash in the tree.
_MISSING = "—"

# Distinct from _MISSING on purpose: provenance_rows argues that "not recorded"
# and "nothing changed" are different claims, and a manifest written before the
# provenance block existed is making the first one.
_NOT_RECORDED = "not recorded"


class OrthoDialog(ImageDialog):
    """The ortho at full resolution, fitted to the window and zoomable.

    Fit to width rather than to the whole window: an ortho is a long thin strip
    of transect, so width is the dimension worth spending on and the height
    scrolls. The wheel zooms in about the cursor from there.
    """

    def __init__(self, path: Path, title: str, parent: QWidget | None = None) -> None:
        super().__init__(QPixmap(str(path)), title, parent)


def _load_ortho(run_dir: Path) -> QPixmap | None:
    """The run's ortho strip, decoded small, or None if it never wrote one."""
    path = run_dir / "ortho.png"
    if not path.is_file():
        return None
    reader = QImageReader(str(path))
    size = reader.size()
    if size.isValid() and size.width() > _ORTHO_DECODE_WIDTH:
        scale = _ORTHO_DECODE_WIDTH / size.width()
        reader.setScaledSize(QSize(_ORTHO_DECODE_WIDTH, max(1, round(size.height() * scale))))
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else None


class DetailCard(QWidget):
    """The card the Browse detail panes are built on: a name, a verdict, facts.

    Browse shows one of these beside the table and which one depends on the
    grouping, so they open the same way whichever thing is selected. Each pane
    adds its own middle to ``self.body`` and closes with an actions row.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, self.body = section_card()

        self.title = QLabel("")
        # One line. A wrapped title is a card that is one row taller for some
        # runs than others, which is the same jump the fact schema exists to
        # stop; the full name is in the tooltip and in the Folder row.
        self.title.setWordWrap(False)
        title_font = self.title.font()
        title_font.setWeight(QFont.Weight.DemiBold)
        title_font.setPointSize(FONT_LG_PT)
        self.title.setFont(title_font)
        # A row rather than a bare label, so a card with a state worth acting on
        # can put the action beside the name instead of inventing a place for it
        # further down.
        self.title_row = QHBoxLayout()
        self.title_row.setContentsMargins(0, 0, 0, 0)
        self.title_row.setSpacing(SPACE_SM)
        self.title_row.addWidget(self.title, 1)
        # Beside the name rather than under it: the verdict belongs to the thing
        # the name names, and on its own line it took a row of a narrow pane to
        # say one word. The stretch on the title keeps it right-aligned, next to
        # whatever single-glyph action the card adds after it.
        self.status = StatusChip()
        self.title_row.addWidget(self.status)
        self.body.addLayout(self.title_row)

        # Non-wrapping: a value that wraps to a second line makes its row taller
        # for one selection than the next, so the pane shuffles as the table is
        # arrowed through. Long values elide to the tooltip instead.
        self.facts = KeyValueList(wrap=False)
        self.body.addWidget(self.facts)

        outer.addWidget(card)

    def set_status(self, text: str, colour: str) -> None:
        """The outcome under the name, as the chip the tables paint it as."""
        self.status.set_status(text, colour)

    def add_title_button(self, button: QAbstractButton) -> None:
        """A single-glyph action in the card's top right, beside the name."""
        button.setProperty("quiet", "true")
        button.setProperty("pad", "none")
        self.title_row.addWidget(button)

    def add_actions(self, primary: QPushButton | None, *secondary: QAbstractButton) -> None:
        """Close the card with what it is for, and quiet actions pushed right.

        Added at the point the pane calls this, so it lands under whatever the
        pane put between the facts and here. The quiet actions are optional: not
        every pane has a second thing worth offering, and an invented one is
        worse than a row with a single button in it. A pane with more than one
        occasional action collapses them into a More… QToolButton instead of
        widening this row past what the pane can hold.

        ``primary`` may be None, for a pane whose main action lives on the row
        the pane describes rather than in the pane. Two controls for one act
        disagree the moment one of them is stale.
        """
        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        if primary is not None:
            primary.setProperty("cta", "true")
            actions.addWidget(primary)
        actions.addStretch(1)
        for button in secondary:
            button.setProperty("quiet", "true")
            actions.addWidget(button)
        self.body.addLayout(actions)

    def clear(self) -> None:
        """Empty the shared header. Panes extend this with their own content."""
        self.title.setText("")
        self.status.setText("")
        self.facts.clear()


def _related_text(related: int) -> str:
    if not related:
        return "none"
    return f"{related} other run{'' if related == 1 else 's'} of this footage"


def run_fact_rows(entry: RunEntry, related: int = 0) -> list[tuple[str, str]]:
    """Every fact the run pane shows, in one fixed order.

    Absent facts say so rather than dropping the row: a run with no points reads
    differently from a manifest that never recorded its models.
    """
    manifest = entry.manifest
    provenance = dict(provenance_rows(manifest))
    return [
        ("Folder", entry.dir_name),
        ("Transect", entry.transect_name or "Not assigned yet"),
        ("Direction", direction_html(entry.direction) or _MISSING),
        ("Session", entry.session_name or "No session recorded"),
        ("Video", entry.video_name or _MISSING),
        ("Recorded", recorded_text(entry) or _MISSING),
        # When the run was made, as against when its footage was shot.
        ("Created", format_timestamp(manifest.get("run_timestamp")) or _MISSING),
        ("Range", format_trim_range(manifest) or "whole video"),
        ("Frames", frames_text(manifest)),
        ("Points", points_label(entry.points) if entry.points else _MISSING),
        ("Runtime", format_duration(entry.duration_s) if entry.duration_s else _MISSING),
        # Sized off the run directory by a background pass, so this is the one
        # row whose value can arrive after the pane has been drawn. It holds its
        # place from the start rather than appearing under the cursor.
        (
            "On disk",
            "removed"
            if entry.data_missing
            else format_bytes(entry.size_bytes)
            if entry.size_bytes is not None
            else "counting…",
        ),
        ("Geometry", geometry_label(manifest) or _MISSING),
        ("Camera", str(manifest.get("camera_profile") or _MISSING)),
        ("Models", provenance.get("Models", _NOT_RECORDED)),
        ("Taxonomy", provenance.get("Taxonomy", _NOT_RECORDED)),
        ("Settings", provenance.get("Settings", _NOT_RECORDED)),
        ("Version", provenance.get("Version", _NOT_RECORDED)),
        ("Related", _related_text(related)),
    ]


class RunDetailPanel(DetailCard):
    """A titled card describing the selected run."""

    open_requested = Signal()
    # The database run id whose outputs to archive, None on a folder-only run.
    archive_requested = Signal(object)
    # The run directory whose run.log to show. A separate signal from
    # open_requested because the runs whose log matters most are the ones that
    # cannot be opened at all.
    log_requested = Signal(object)
    rename_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open_action_allowed = False
        layout = self.body

        # What the run actually produced, which no amount of metadata conveys.
        # Below the facts rather than above them: the facts identify the run, the
        # ortho is what you look at once you know you have the right one.
        self.ortho = ClickableLabel("")
        self.ortho.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        # Ignored horizontally: a pixmap's size hint is its own width, so the
        # thumbnail would otherwise widen the pane to fit itself and shove the
        # run table aside every time a selection changed.
        self.ortho.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.ortho.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ortho.setToolTip("Click to open the ortho at full size")
        self.ortho.clicked.connect(self._open_ortho)
        # Always laid out, never hidden: the band is a constant height whether or
        # not the run wrote a strip, so a run with no ortho does not pull
        # everything below it up the pane.
        self.ortho.setFixedHeight(_ORTHO_BAND_HEIGHT)
        layout.addWidget(self.ortho)
        self._ortho_pixmaps: dict[Path, QPixmap | None] = {}
        self._ortho_source: QPixmap | None = None
        self._ortho_run_dir: Path | None = None
        self._ortho_title = ""

        # What the run found, which is the reason it was made. Below the strip:
        # the facts say which run, the strip says what it looks like, and this
        # says what it is made of.
        self.cover = CoverPanel()
        layout.addWidget(self.cover)
        self._covers: dict[Path, dict | None] = {}

        layout.addStretch(1)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {WARN_TEXT};")
        self.error.setVisible(False)
        layout.addWidget(self.error)

        # What the registry's archive holds of this run, from a live probe only.
        self.archive_state = QLabel("")
        self.archive_state.setVisible(False)
        layout.addWidget(self.archive_state)

        # Opening the run is what this pane is for, and it is the only button in
        # the row that says so. Everything occasional is behind More…: the pane
        # is 260px at its floor, which does not hold four labelled buttons
        # without eliding every one of them.
        self.open_btn = QPushButton("Open run")
        self.open_btn.setToolTip("Load this run into the 3D viewer")
        self.open_btn.clicked.connect(self.open_requested)
        self.open_btn.setVisible(False)

        # A run that failed leaves one truncated line on its record and the whole
        # story in its log. This is the only way to that story: the other route
        # is loading the run, which is the thing that cannot be done. So on a run
        # that cannot open, the log is the pane's main action and stands in the
        # row; on one that can, it is another entry in the menu.
        self.log_btn = QPushButton("Show log")
        self.log_btn.setIcon(log_icon())
        self.log_btn.setToolTip("Show this run's log, including why it stopped")
        self.log_btn.clicked.connect(self._request_log)
        self.log_btn.setVisible(False)
        self.log_btn.setProperty("cta", "true")

        self.more_btn = QToolButton()
        self.more_btn.setText("More…")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_btn)
        # Every entry says why it is off when it is off, so the menu explains
        # itself rather than only refusing.
        menu.setToolTipsVisible(True)
        self.menu_actions = self._fill_run_actions(menu)
        self.menu_actions["copy_command"].setIcon(copy_icon(ICON_SM))
        self.menu_actions["show_log"].setIcon(log_icon())
        self.more_btn.setMenu(menu)

        self.add_actions(self.open_btn, self.more_btn)
        # Into the primary slot beside the opener rather than through
        # add_actions, which marks what it is handed quiet and would undo the
        # call-to-action styling the log carries when it stands there alone.
        item = cast(QLayoutItem, self.body.itemAt(self.body.count() - 1))
        cast(QHBoxLayout, item.layout()).insertWidget(1, self.log_btn)
        self._entry: RunEntry | None = None

    def _run_action_specs(self) -> tuple[tuple[str, str, object], ...]:
        """Everything the menu offers on this run, in one list."""
        return (
            ("rename", "Rename…", self.rename_requested.emit),
            ("archive", "Archive to server…", self._request_archive),
            ("copy_command", "Copy command", self._copy_command),
            ("show_log", "Show log", self._request_log),
        )

    @property
    def entry(self) -> RunEntry | None:
        return self._entry

    def set_archive_state(self, state: str | None, note: str | None = None) -> None:
        """Paint the archive's answer, or nothing when nobody has asked it.

        ``note`` replaces the state's stock tooltip, for a failure's own words.
        """
        faces = {
            "archived": ("Outputs on server", SUCCESS, "Every output archived and verified."),
            "partial": (
                "Outputs partly on server",
                WARN_TEXT,
                "Some outputs are archived. Archive again to send the rest.",
            ),
            "pending": (
                "Outputs uploading…",
                WARN_TEXT,
                "Offered to the registry, not verified yet.",
            ),
            "uploading": ("Archiving…", WARN_TEXT, "Sending this run's outputs to the registry."),
            "failed": (
                "Archive failed",
                ERROR,
                "The registry could not verify an upload. Archive again.",
            ),
        }
        face = faces.get(state or "")
        if face is None:
            self.archive_state.setVisible(False)
            return
        text, colour, tip = face
        self.archive_state.setText(text)
        self.archive_state.setStyleSheet(f"color: {colour};")
        self.archive_state.setToolTip(note or tip)
        self.archive_state.setVisible(True)

    def _request_archive(self) -> None:
        """Offer this run's outputs to the registry's archive.

        Only a run the survey database knows can be archived: the registry
        catalogues artefacts against the run row, and a folder-only run has no
        row to hang them off.
        """
        entry = self._entry
        if entry is not None and entry.db_run is not None:
            self.archive_requested.emit(entry.db_run.id)

    def _fill_run_actions(self, menu: QMenu) -> dict[str, QAction]:
        return {key: menu.addAction(label, slot) for key, label, slot in self._run_action_specs()}

    def set_open_action_visible(self, visible: bool) -> None:
        """Hide the opener where opening means nothing: View mode is already in it."""
        self._open_action_allowed = visible
        self.open_btn.setVisible(
            visible and self._entry is not None and not (self._entry.incomplete or self._entry.data_missing)
        )

    def _request_log(self) -> None:
        if self._entry is not None:
            self.log_requested.emit(self._entry.run_dir)

    def _copy_command(self) -> None:
        from deepreefmap_gui.runs.run_command import command_from_manifest

        if self._entry is None:
            return
        text = command_from_manifest(self._entry.manifest, self._entry.run_dir)
        QGuiApplication.clipboard().setText(text)
        # Anchored on the button the menu dropped from: the menu item itself is
        # gone by the time the clipboard has the command.
        action = self.menu_actions["copy_command"]
        QToolTip.showText(
            self.more_btn.mapToGlobal(self.more_btn.rect().topRight()),
            "Copied to clipboard",
            self.more_btn,
        )
        action.setIcon(check_icon(ICON_SM))
        QTimer.singleShot(1200, lambda: action.setIcon(copy_icon(ICON_SM)))

    def show_entry(self, entry: RunEntry, related: int = 0) -> None:
        # Imported here rather than at module scope: simple.batch reaches back
        # into the window, and importing it eagerly closes an import cycle.
        from deepreefmap_gui.simple.batch import _diagnose_failure

        broken = bool(entry.incomplete or entry.data_missing)
        status = catalogue.entry_status(entry)
        colour = STATUS_COLORS.get(status, TEXT_MUTED)
        self.title.setText(entry.display_name)
        self.set_status(status.capitalize(), colour)
        self.facts.set_rows(run_fact_rows(entry, related))
        self._show_cover(entry.run_dir)
        error = entry.db_run.error if entry.db_run is not None else ""
        if entry.incomplete:
            self.error.setText(_diagnose_failure(error) if error else "This run did not finish and wrote no manifest.")
        elif entry.data_missing:
            self.error.setText("The output data was removed.")
        self.error.setVisible(broken)
        self._entry = entry
        # Unavailable actions stay visible, disabled, with the reason in the tooltip.
        copy = self.menu_actions["copy_command"]
        copy.setEnabled(not entry.data_missing)
        copy.setToolTip(
            "The data this run was made from is gone."
            if entry.data_missing
            else "Copy the command that reproduces this run in a terminal"
        )
        # The name lives in the manifest, so a run that never wrote one has
        # nothing to rename.
        rename = self.menu_actions["rename"]
        rename.setEnabled(not broken)
        rename.setToolTip("This run has no manifest to carry a name." if broken else "Change what this run is called")
        # The registry files artefacts against the run row.
        archive = self.menu_actions["archive"]
        if entry.db_run is None:
            why = "Not in the survey database."
        elif entry.data_missing:
            why = "The output data was removed."
        elif status != "succeeded":
            why = "Only a finished run's outputs can be archived."
        else:
            why = ""
        archive.setEnabled(not why)
        archive.setToolTip(why or "Send this run's outputs to the registry's archive")
        self.open_btn.setVisible(self._open_action_allowed and not broken)
        # Offered for any run that kept one, whatever its status: run.log is kept
        # through every storage-cleanup tier, so a run whose outputs were cleared
        # away still has its record. One control for it, never two: in the row
        # for a run that cannot open, in the menu for one that can.
        has_log = (entry.run_dir / "run.log").exists()
        self.log_btn.setVisible(has_log and broken)
        log = self.menu_actions["show_log"]
        log.setVisible(not broken)
        log.setEnabled(has_log)
        log.setToolTip("Show this run's log, including why it stopped" if has_log else "This run wrote no log.")
        self._show_ortho(entry.run_dir, entry.display_name)

    def _show_ortho(self, run_dir: Path, title: str) -> None:
        if run_dir not in self._ortho_pixmaps:
            if len(self._ortho_pixmaps) >= _ORTHO_CACHE_MAX:
                self._ortho_pixmaps.pop(next(iter(self._ortho_pixmaps)))
            self._ortho_pixmaps[run_dir] = _load_ortho(run_dir)
        self._ortho_source = self._ortho_pixmaps[run_dir]
        self._ortho_run_dir = run_dir
        self._ortho_title = title
        self._rescale_ortho()

    def _show_cover(self, run_dir: Path) -> None:
        """The run's cover report, cached like the ortho beside it.

        One small JSON read per selection, which is why it is cached the same
        way: arrowing back up the table should not re-read what it just read.
        """
        if run_dir not in self._covers:
            if len(self._covers) >= _ORTHO_CACHE_MAX:
                self._covers.pop(next(iter(self._covers)))
            self._covers[run_dir] = load_cover(run_dir)
        self.cover.show_cover(self._covers[run_dir])

    def _rescale_ortho(self) -> None:
        """Letterbox the strip into a band of constant height.

        Driven off this panel's width: the label is width-Ignored, so its own
        width lags a resize by a layout pass.
        """
        if self._ortho_source is None:
            self.ortho.clear()
            return
        available = max(1, self.width() - _ORTHO_H_MARGIN)
        scaled = self._ortho_source.scaled(
            available,
            _ORTHO_BAND_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.ortho.setPixmap(scaled)

    def _open_ortho(self) -> None:
        if self._ortho_run_dir is None or self._ortho_source is None:
            return
        path = self._ortho_run_dir / "ortho.png"
        if not path.is_file():
            return
        OrthoDialog(path, self._ortho_title or path.parent.name, self).exec()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._rescale_ortho()

    def clear(self) -> None:
        super().clear()
        self.error.setVisible(False)
        self._entry = None
        self.open_btn.setVisible(False)
        self.log_btn.setVisible(False)
        self._ortho_source = None
        self._ortho_run_dir = None
        self._rescale_ortho()
        self.cover.show_cover(None)
