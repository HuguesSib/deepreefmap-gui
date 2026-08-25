"""Setup: one destination for everything about the computer you are on.

Readiness, models, performance and updates are four views of one subject, so they
get one destination and a segmented control inside it rather than four header
entries competing with the work.

The header button reports the machine before it is opened, in two slots and no
more: what is stopping work, and whether an update is waiting. Its verdict comes
from machine_state() in section_state.py, which is the same Qt-free vocabulary
the destinations use and is computed from the same checks the Run gate blocks on, so
the header cannot claim the machine is fine while Process refuses to start.
"""

from __future__ import annotations

import logging
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_SM,
    blocked_icon,
    cog_icon,
    download_icon,
    warning_icon,
)
from deepreefmap_gui.core.theme import (
    GUTTER,
    SPACE_SM,
    SPACE_XS,
    UPDATE,
    WARNING,
)
from deepreefmap_gui.core.widgets import (
    SectionHeader,
    centred_column,
    section_card,
    segmented_qss,
    utility_button_qss,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.notify.history_ui import NotificationHistoryPanel
from deepreefmap_gui.simple.section_state import ATTENTION, BLOCKED, SectionState, machine_state
from deepreefmap_gui.survey.models.common import utc_now_iso

logger = logging.getLogger(__name__)

# Left to right in the order they matter: what stops a run, what a run needs
# installed, what the machine does while it runs, the software itself, and the
# record of everything the app has had to say.
MACHINE_VIEWS = ("readiness", "models", "performance", "updates", "server", "activity")

_VIEW_LABELS = {
    "readiness": "Readiness",
    "models": "Models",
    "performance": "Performance",
    "updates": "Updates",
    "server": "Server",
    "activity": "Activity",
}

_VIEW_TIPS = {
    "readiness": "Whether this computer can process a dive, and how to fix it if not.",
    "models": "The models installed here, and how to add or remove them.",
    "performance": "Live usage of this machine, and what past runs cost it.",
    "updates": "The version installed here, and any newer one available.",
    "server": "The registry this survey syncs with, and what is waiting to go.",
    "activity": "Everything this survey has reported, and anything you silenced.",
}

# The header button's badge size, and the gap between two of them.
_BADGE_PX = ICON_SM
_BADGE_GAP = SPACE_XS


class MachineButton(QToolButton):
    """Header entry to Setup, carrying its verdict before it is opened.

    Two badge slots at most. The first is the highest-severity thing standing in
    the way, the second is an available update, and neither appears when there
    is nothing to say: a badge that is always lit is a badge nobody reads.

    An update is never painted red. It is a chore, not a blocker, and it has its
    own silhouette so the two are distinguishable without relying on colour.

    The badges are painted rather than set as an icon because QToolButton puts
    its one icon beside the text, and these belong after it: the label is what
    the button is, the badges are what it currently has to report.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._badges: list[QIcon] = []
        self.setText("Setup")
        self.setIcon(cog_icon(_BADGE_PX))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_verdict(SectionState("ok", "Ready"), "")

    def set_verdict(self, verdict: SectionState, update_version: str) -> None:
        """Repaint the badges, and say the same thing in words for a reader."""
        badges = []
        if verdict.state == BLOCKED:
            badges.append(blocked_icon(_BADGE_PX))
        elif verdict.state == ATTENTION:
            badges.append(warning_icon(_BADGE_PX))
        if update_version:
            badges.append(download_icon(_BADGE_PX, QColor(UPDATE)))
        self._badges = badges
        # The badges are painted into reserved padding rather than over the
        # label, so the text does not shift as they come and go.
        reserved = SPACE_SM + len(badges) * (_BADGE_PX + _BADGE_GAP)
        self.setStyleSheet(utility_button_qss(reserved))
        self.setToolTip(verdict.reason)
        # One phrase per glyph, so the name a screen reader announces and the
        # badges a sighted user sees are the same two facts in the same order.
        name = f"Setup: {verdict.count}"
        if update_version:
            name += f", version {update_version} available"
        self.setAccessibleName(name)
        self.setAccessibleDescription(verdict.reason)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._badges:
            return
        painter = QPainter(self)
        count = len(self._badges)
        width = count * _BADGE_PX + (count - 1) * _BADGE_GAP
        x = self.width() - SPACE_SM - width
        y = (self.height() - _BADGE_PX) // 2
        for badge in self._badges:
            badge.paint(painter, int(x), y, _BADGE_PX, _BADGE_PX)
            x += _BADGE_PX + _BADGE_GAP
        painter.end()


class SimpleMachineMixin(MixinBase):
    """DeepReefMapWindow methods for the Setup destination and its button."""

    _machine_view: str = "readiness"

    def _build_machine_page(self) -> QWidget:
        """One page, its views, and a segmented control to pick between them.

        A segmented control rather than tabs or another header entry: these are
        ways of looking at one computer, not places to go, and the component is
        the one Browse already uses for the same job. The Server view lives
        here too since the sync badge took over saying whether the registry is
        healthy: a configuration surface belongs with the rest of Setup, not
        behind a header button of its own.
        """
        # The whole destination is one centred column: heading, switch and views
        # share an edge, so the switch stays over the view it opens instead of
        # sitting a window's width away from it.
        page, outer = centred_column()

        header = QHBoxLayout()
        header.setSpacing(GUTTER)
        header.addWidget(SectionHeader("Setup"))
        header.addStretch(1)
        # The segments share a seam, so they get a row of their own at zero
        # spacing. Added straight to the header, they inherit the gap it puts
        # between the title and the control, and the joined edges the stylesheet
        # draws are pulled apart into loose pills.
        switch = QHBoxLayout()
        switch.setSpacing(0)
        self._machine_view_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(page)
        group.setExclusive(True)
        for index, view in enumerate(MACHINE_VIEWS):
            button = QToolButton()
            button.setText(_VIEW_LABELS[view])
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(_VIEW_TIPS[view])
            button.setStyleSheet(segmented_qss(first=index == 0, last=index == len(MACHINE_VIEWS) - 1))
            button.clicked.connect(partial(self._set_machine_view, view))
            group.addButton(button)
            switch.addWidget(button)
            self._machine_view_buttons[view] = button
        header.addLayout(switch)
        outer.addLayout(header)

        self._machine_stack = QStackedWidget()
        views = {
            "readiness": self._build_readiness_view(),
            "models": self._build_machine_host("_machine_models_host"),
            "performance": self._build_machine_host("_machine_system_host"),
            "updates": self._build_machine_updates_view(),
            "server": self._build_server_page(),
            "activity": self._build_activity_view(),
        }
        for view in MACHINE_VIEWS:
            self._machine_stack.addWidget(views[view])
        outer.addWidget(self._machine_stack, 1)

        self._set_machine_view("readiness")
        self._refresh_update_notice()
        return page

    def _build_machine_host(self, attribute: str) -> QWidget:
        """A scrolling slot for a panel that is lent here from its home.

        The panels are long and the window is not, so each view scrolls on its
        own rather than the destination scrolling as one and carrying the
        segmented control off the top of the screen with it.
        """
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        setattr(self, attribute, host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _build_machine_updates_view(self) -> QWidget:
        """The updater, on a card the width of what it has to say.

        A view of its own rather than a footnote under the gauges: everything
        else on this destination describes the computer, and this one changes the
        software running on it.
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GUTTER)

        card, card_layout = section_card()
        self._machine_updates_host = QWidget()
        host_layout = QVBoxLayout(self._machine_updates_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(self._machine_updates_host)
        outer.addWidget(card)
        outer.addStretch(1)
        return page

    def _build_machine_nav_button(self) -> MachineButton:
        """Header entry point, and the machine's own status line.

        Never the only place a blocker shows: the Run step names the same
        problem beside the button that is refusing to start, which is where
        somebody trying to work will be looking.
        """
        button = MachineButton()
        button.clicked.connect(lambda: self._set_simple_section("machine"))
        self._machine_nav_button = button
        return button

    def _host_machine_panels(self) -> None:
        """Take the panels out of their homes and onto this page.

        The homes still exist because the run settings dialog borrows the form
        out of one of them, and because a widget needs somewhere to be returned
        to. Moving rather than rebuilding: two copies of the model library would
        disagree the moment a download finished against only one of them.
        """
        for widget_attr, host_attr in (
            ("_models_page", "_machine_models_host"),
            ("_system_page", "_machine_system_host"),
            ("_updates_page", "_machine_updates_host"),
            ("_out_root_widget", "_machine_out_root_host"),
        ):
            widget = getattr(self, widget_attr)
            host = getattr(self, host_attr)
            layout = host.layout()
            if layout is not None and widget.parentWidget() is not host:
                layout.addWidget(widget)

    def _refresh_update_notice(self) -> None:
        """Say a release is waiting on the destination the badge opens.

        The header badge gets somebody here, and then the page it lands on is
        Readiness, which knows nothing about updates: the badge looked like it
        had brought them nowhere. So the segment that holds the release is
        tinted, and the strip above the checks names the version and opens it.
        """
        version = getattr(self, "_update_available", "")
        button = getattr(self, "_machine_view_buttons", {}).get("updates")
        if button is not None:
            index = MACHINE_VIEWS.index("updates")
            button.setStyleSheet(
                segmented_qss(
                    first=index == 0,
                    last=index == len(MACHINE_VIEWS) - 1,
                    alert=UPDATE if version else "",
                )
            )
        strip = getattr(self, "_setup_update_strip", None)
        if strip is not None:
            strip.show_notice(
                f"DeepReefMap {version} is available." if version else "",
                "See what's new",
            )

    def _refresh_models_segment(self) -> None:
        """Tint Models while the server preset names weights this laptop lacks.

        For the same reason the Updates segment is tinted: the offer to fetch
        them is painted on the Server view, and the sync that raises it is
        usually run from the status-bar badge, which leaves that page unseen.
        """
        button = getattr(self, "_machine_view_buttons", {}).get("models")
        if button is None:
            return
        index = MACHINE_VIEWS.index("models")
        button.setStyleSheet(
            segmented_qss(
                first=index == 0,
                last=index == len(MACHINE_VIEWS) - 1,
                alert=WARNING if getattr(self, "_preset_missing_models", ()) else "",
            )
        )

    def _set_machine_view(self, view: str) -> None:
        if view not in MACHINE_VIEWS:
            raise ValueError(f"Unknown machine view: {view!r}")
        self._machine_view = view
        for name, button in self._machine_view_buttons.items():
            button.blockSignals(True)
            button.setChecked(name == view)
            button.blockSignals(False)
        self._machine_stack.setCurrentIndex(MACHINE_VIEWS.index(view))
        self._sync_system_gauges_running()
        if view == "server":
            self._refresh_server_page()
        # Read on opening rather than kept live: the log is a record, and a table
        # that repainted under the cursor as conditions came and went would be
        # the one surface here that will not hold still.
        if view == "activity":
            self._refresh_activity_view()

    def _build_activity_view(self) -> QWidget:
        self._activity_panel = NotificationHistoryPanel()
        self._activity_panel.filters_changed.connect(self._refresh_activity_view)
        self._activity_panel.unmuted.connect(self._on_activity_unmuted)
        return self._activity_panel

    def _refresh_activity_view(self) -> None:
        panel = getattr(self, "_activity_panel", None)
        if panel is None:
            return
        severity, scope = panel.filters()
        panel.set_history(self._notify.history(severity=severity, scope=scope), utc_now_iso())
        panel.set_muted(self._notify.muted())

    def _on_activity_unmuted(self, fingerprint: str) -> None:
        self._notify.unmute(fingerprint)
        self._refresh_activity_view()
        self._refresh_notification_bell()

    def _sync_system_gauges_running(self) -> None:
        """The 1 Hz gauge poll runs only while the gauges are on screen."""
        timer = getattr(self, "_sys_timer", None)
        if timer is None:
            return
        if self._current_section() == "machine" and self._machine_view == "performance":
            self._refresh_system_gauges()
            self._refresh_recorded_runs()
            timer.start()
        else:
            timer.stop()

    def _machine_verdict(self) -> SectionState:
        """This computer's verdict, from the checks the Run gate blocks on."""
        checks = self._current_setup_checks()
        return machine_state(
            # Advisory rows are not requirements: the memory row reports a risk
            # the queued session carries, and it is named in the sentence below.
            unmet=sum(1 for check in checks if not check.ok and not check.advisory),
            advisory=getattr(self, "_memory_advisory", ""),
            update_version=getattr(self, "_update_available", ""),
        )

    def _refresh_machine_button(self) -> None:
        """Repaint the header button from a fresh verdict.

        Called from the readiness repaint, the update check and the memory
        grade, which is every input the verdict has.
        """
        button = getattr(self, "_machine_nav_button", None)
        if button is None:
            return
        button.set_verdict(self._machine_verdict(), getattr(self, "_update_available", ""))
        # The machine's verdict is one of the conditions the bell carries, and
        # this is the only place it changes. The cache key makes the call cheap.
        self._refresh_section_state()
