"""The Server page: what this device is enrolled with, and the sync it drives.

A section rather than a fourth destination. The destinations are where the work is
(footage, transects, the cart, the archive) and this is a place you visit to check
a connection and leave again, which is what Setup and the storage pages already
are, so it takes their shape: a bordered utility button in the header, no pill,
nothing lit while you are here.

Both network calls run on a worker thread and report back over the window's
signals. Sync conflicts are not reported on this page at all: the engine posts them
to the notification centre, where everything else wrong with the survey is already
read, and pressing one brings the reader back here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QWidget,
)

from deepreefmap_gui.core import sync_badge
from deepreefmap_gui.core.icons import ICON_SM, server_icon
from deepreefmap_gui.core.spinner import BusySpinner
from deepreefmap_gui.core.theme import FONT_LG, SPACE_SM, SUCCESS, WEIGHT_SEMIBOLD
from deepreefmap_gui.core.widgets import (
    EmptyState,
    KeyValueList,
    NoticeStrip,
    NotReadyStrip,
    SectionHeader,
    centred_column,
    confirm,
    muted_label,
    section_card,
    utility_button_qss,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.notify.widgets import relative_age
from deepreefmap_gui.server import enrolment as enrolment_mod
from deepreefmap_gui.server.connect_ui import ConnectDialog
from deepreefmap_gui.server.state import (
    DEVICE_NAME_KEY,
    ENROLLED_BY_KEY,
    LAST_SYNC_KEY,
    SECTION_LABELS,
    SERVER_SECTION,
    SYNC_ERROR_KEY,
    Failure,
    ServerState,
    default_device_name,
    describe_failure,
    heartbeat_report,
    read_agreed_contract,
    read_state,
    remember_agreed_contract,
    summarise,
)
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.notification import INFO, SURVEY, WARNING
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.archive import ArchivePlan, ArchiveReport
from deepreefmap_gui.sync.contract import PULL_SECTIONS
from deepreefmap_gui.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

PAGE_TITLE = "Server"
PAGE_CAPTION = "Share this survey's records with a registry."

NOT_CONNECTED = "Not connected."
NOT_CONNECTED_HINT = "Paste a connect code to join a registry."

DEVICE_CARD = "This device"
ATTRIBUTION_NOTE = "Uploads are attributed to this name. Rename it in the web interface."

REFERENCE_NOTE = (
    "Sites and campaigns are edited in the web interface. Here they are chosen: "
    "a transect names its site on the Transects page, and a section names its "
    "campaign when it is filed."
)
ONBOARDED_BY = "Onboarded by"

CONNECT = "Connect to server"
RECONNECT = "Connect again"
SYNC_NOW = "Sync now"
DISCONNECT = "Disconnect"

# Said beside the button as well as on it: the two things confused here are
# forgetting a token and revoking a device.
DISCONNECT_NOTE = (
    "Disconnect only forgets the token on this laptop. It does not revoke the device: "
    "that is done in the registry's web interface."
)

SESSION_RUNNING = "Wait for the current session to finish, then sync."

PULLING = "Pulling changes (page {page})…"
SENDING = "Sending {rows} row(s)…"

ARCHIVE_NOW = "Archive to server"
ARCHIVE_TOOLTIP = (
    "Send the original clips and every finished run's outputs to the registry's "
    "archive. Nothing is sent until this is pressed."
)
PLANNING_ARCHIVE = "Working out what to archive…"
CANCEL_ARCHIVE = "Cancel archive"
CANCELLING_ARCHIVE = "Finishing the file in flight…"
# One episode per pass over the queue: re-archiving resumes server-side, so the
# same fingerprint updating in place is the right shape for a retry.
ARCHIVE_FAILED = "archive.upload_failed"


class ConflictNotifier:
    """The engine's conflict sink, delivered to the bell on the GUI thread.

    The engine posts from the worker thread and the notification centre belongs to
    the GUI one, so everything goes through `_sig_notify`, the one route a worker
    has to the bell. The section is stamped here: a conflict is read on the Server
    page, so pressing the notification has to land there.
    """

    def __init__(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit

    def post(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = "",
        severity: str = INFO,
        scope: str = SURVEY,
    ) -> None:
        self._emit(
            {
                "fingerprint": fingerprint,
                "title": title,
                "body": body,
                "severity": severity,
                "scope": scope,
                "section": SERVER_SECTION,
            }
        )


class ProgressTransport:
    """The sync client, reporting each exchange as it is made.

    Wrapping the transport rather than instrumenting the engine: a pull is as many
    requests as the registry has pages, and that count only exists out here.
    """

    def __init__(self, client: Any, report: Callable[[str], None]) -> None:
        self._client = client
        self._report = report
        self._pages = 0

    def pull(self, since: int | None = None, limit: int = 1000) -> Mapping[str, Any]:
        self._pages += 1
        self._report(PULLING.format(page=self._pages))
        return self._client.pull(since=since, limit=limit)

    def push(self, sections: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any]:
        self._report(SENDING.format(rows=sum(len(rows) for rows in sections.values())))
        return self._client.push(sections)


class ServerPageMixin(MixinBase):
    """DeepReefMapWindow methods for the Server section and the sync it runs."""

    _server_syncing: bool = False
    _server_archiving: bool = False
    _connect_dialog: ConnectDialog | None = None
    # The client the running sync is using, kept for the version it learned.
    _sync_client: Any | None = None
    # The archive flow between its two workers: the client the plan was built
    # for, and the plan awaiting confirmation or upload.
    _archive_client: Any | None = None
    _archive_plan_pending: ArchivePlan | None = None

    # --- building -----------------------------------------------------------

    def _build_server_page(self) -> QWidget:
        """The connection, the position, and the actions on them."""
        page, body = centred_column()
        body.addWidget(SectionHeader(PAGE_TITLE))
        caption = muted_label(PAGE_CAPTION)
        caption.setWordWrap(True)
        body.addWidget(caption)

        self._server_blocker = NotReadyStrip()
        self._server_blocker.action_clicked.connect(self._on_connect_server)
        body.addWidget(self._server_blocker)

        self._server_notice = NoticeStrip(SUCCESS)
        self._server_notice.action_clicked.connect(self._on_sync_now)
        body.addWidget(self._server_notice)

        self._server_empty = EmptyState(NOT_CONNECTED, NOT_CONNECTED_HINT)
        body.addWidget(self._server_empty)

        self._server_device_card, device_layout = section_card(DEVICE_CARD)
        self._server_device_label = QLabel("")
        self._server_device_label.setStyleSheet(
            f"font-size: {FONT_LG}; font-weight: {WEIGHT_SEMIBOLD};"
        )
        self._server_device_label.setWordWrap(True)
        device_layout.addWidget(self._server_device_label)
        attribution = muted_label(ATTRIBUTION_NOTE)
        attribution.setWordWrap(True)
        device_layout.addWidget(attribution)
        self._server_device_facts = KeyValueList()
        device_layout.addWidget(self._server_device_facts)
        body.addWidget(self._server_device_card)

        self._server_card, card_layout = section_card("Connection")
        self._server_facts = KeyValueList()
        card_layout.addWidget(self._server_facts)
        body.addWidget(self._server_card)

        self._server_waiting_card, waiting_layout = section_card("Waiting to send")
        self._server_waiting = KeyValueList()
        waiting_layout.addWidget(self._server_waiting)
        body.addWidget(self._server_waiting_card)

        # What the registry has sent down, read-only: sites and campaigns are
        # authored in the web interface and only chosen here: on the Transects
        # page and when a section is filed.
        self._server_reference_card, reference_layout = section_card("From the registry")
        reference_note = muted_label(REFERENCE_NOTE)
        reference_note.setWordWrap(True)
        reference_layout.addWidget(reference_note)
        self._server_reference = KeyValueList()
        reference_layout.addWidget(self._server_reference)
        body.addWidget(self._server_reference_card)

        body.addWidget(self._build_server_actions())
        note = muted_label(DISCONNECT_NOTE)
        note.setWordWrap(True)
        self._server_disconnect_note = note
        body.addWidget(note)
        body.addStretch(1)

        holder = QScrollArea()
        holder.setWidgetResizable(True)
        holder.setWidget(page)
        holder.setFrameShape(QScrollArea.Shape.NoFrame)
        holder.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return holder

    def _build_server_actions(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)

        self._server_spinner = BusySpinner()
        self._server_spinner.setVisible(False)
        row.addWidget(self._server_spinner)
        self._server_progress = muted_label("")
        row.addWidget(self._server_progress, 1)

        # Beside the progress it stops: an upload on a field uplink can be hours
        # of PUTs, and until this existed nothing could end it early.
        self._server_archive_cancel_btn = QPushButton(CANCEL_ARCHIVE)
        self._server_archive_cancel_btn.setProperty("quiet", "true")
        self._server_archive_cancel_btn.setVisible(False)
        self._server_archive_cancel_btn.clicked.connect(self._on_archive_cancel)
        row.addWidget(self._server_archive_cancel_btn)

        self._server_connect_btn = QPushButton(CONNECT)
        self._server_connect_btn.setProperty("cta", "true")
        self._server_connect_btn.clicked.connect(self._on_connect_server)
        row.addWidget(self._server_connect_btn)

        self._server_sync_btn = QPushButton(SYNC_NOW)
        self._server_sync_btn.setToolTip(
            "Take everything the registry has for this survey, then offer everything "
            "edited here."
        )
        self._server_sync_btn.clicked.connect(self._on_sync_now)
        row.addWidget(self._server_sync_btn)

        self._server_archive_btn = QPushButton(ARCHIVE_NOW)
        self._server_archive_btn.setToolTip(ARCHIVE_TOOLTIP)
        self._server_archive_btn.clicked.connect(self._on_archive_now)
        row.addWidget(self._server_archive_btn)

        self._server_disconnect_btn = QPushButton(DISCONNECT)
        self._server_disconnect_btn.setToolTip(DISCONNECT_NOTE)
        self._server_disconnect_btn.clicked.connect(self._on_disconnect_server)
        row.addWidget(self._server_disconnect_btn)
        return holder

    def _build_server_nav_button(self) -> QToolButton:
        """Header entry, beside the other utilities and never a destination pill."""
        button = QToolButton()
        button.setText(PAGE_TITLE)
        button.setIcon(server_icon(ICON_SM))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(utility_button_qss())
        button.setToolTip("The registry this survey syncs with, and what is waiting to go.")
        button.clicked.connect(lambda: self._set_simple_section(SERVER_SECTION))
        self._server_nav_button = button
        return button

    # --- painting -----------------------------------------------------------

    def _refresh_server_page(self) -> None:
        """Re-read the credential and the sync position, and paint them."""
        if not hasattr(self, "_server_facts"):
            return
        state = read_state(
            self._try_survey_store(), self._server_device_name(), self._server_enrolled_by()
        )
        connected = state.connected
        self._server_empty.setVisible(not connected)
        self._server_device_card.setVisible(connected)
        self._server_card.setVisible(connected)
        self._server_waiting_card.setVisible(connected and bool(state.pending))
        self._server_connect_btn.setVisible(not connected)
        busy = self._server_syncing or self._server_archiving
        self._server_sync_btn.setVisible(connected)
        self._server_sync_btn.setEnabled(connected and not busy)
        self._server_archive_btn.setVisible(connected)
        self._server_archive_btn.setEnabled(connected and not busy)
        self._server_disconnect_btn.setVisible(connected)
        self._server_disconnect_note.setVisible(connected)
        if state.fault:
            self._server_blocker.show_blocker(state.fault, RECONNECT)
        elif state.sync_fault:
            # The persisted outcome of the last sync, so the page still has the
            # answer after a restart. No action: the message itself says whether
            # a retry or a new code fixes it.
            self._server_blocker.show_blocker(state.sync_fault, "")
        else:
            # A refresh repaints the whole page from disk, so a transient
            # message ("wait for the session to finish") must not outlive the
            # condition it described.
            self._server_blocker.clear()
        if connected:
            self._server_device_label.setText(state.device_name or default_device_name())
            self._server_device_facts.set_rows(_device_rows(state))
            self._server_facts.set_rows(_fact_rows(state))
            self._server_waiting.set_rows(
                [
                    (SECTION_LABELS.get(section, section), str(count))
                    for section, count in state.pending.items()
                ]
            )
        reference = _reference_rows(self._try_survey_store()) if connected else []
        self._server_reference_card.setVisible(bool(reference))
        if reference:
            self._server_reference.set_rows(reference)

    def _server_device_name(self) -> str:
        stored = self._settings.value(DEVICE_NAME_KEY, "")
        return str(stored) if stored else default_device_name()

    def _server_enrolled_by(self) -> str:
        stored = self._settings.value(ENROLLED_BY_KEY, "")
        return str(stored) if stored else ""

    def _set_server_busy(self, busy: bool, text: str = "") -> None:
        self._server_spinner.setVisible(busy)
        self._server_progress.setText(text)
        self._server_sync_btn.setEnabled(not busy)
        self._server_archive_btn.setEnabled(not busy)
        self._server_disconnect_btn.setEnabled(not busy)
        if not busy:
            self._server_archive_cancel_btn.setVisible(False)

    # --- connecting ---------------------------------------------------------

    def _on_connect_server(self) -> None:
        """Open the connect dialog, and enrol whatever it hands over."""
        dialog = ConnectDialog(self)
        dialog.submitted.connect(self._start_enrolment)
        dialog.finished.connect(self._on_connect_dialog_finished)
        self._connect_dialog = dialog
        dialog.exec()
        # Destroyed rather than parked on the window: the field holds a credential.
        dialog.deleteLater()

    def _on_connect_dialog_finished(self) -> None:
        # An enrolment can outlive the dialog: whoever cancelled mid-flight still
        # gets the outcome, on the page instead.
        self._connect_dialog = None

    def _start_enrolment(self, code: str) -> None:
        """Enrol on a worker thread. The code is never logged and never stored."""

        def worker() -> None:
            try:
                connected = enrolment_mod.connect(code)
            except Exception as exc:
                logger.warning("Enrolment failed: %s", exc)
                payload: tuple[object, object] = (None, describe_failure(exc))
            else:
                payload = (connected, None)
            try:
                self._sig_enrol_done.emit(*payload)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the enrolment finished")

        threading.Thread(target=worker, daemon=True, name="registry-enrol").start()

    def _on_enrol_done(self, connected: object, failure: object) -> None:
        dialog = self._connect_dialog
        if isinstance(failure, Failure):
            if dialog is not None:
                dialog.show_failure(failure.title, failure.detail)
            else:
                self._server_blocker.show_blocker(f"{failure.title}. {failure.detail}", RECONNECT)
            return
        if not isinstance(connected, enrolment_mod.Connected):
            return
        self._settings.setValue(DEVICE_NAME_KEY, connected.device_name)
        self._settings.setValue(ENROLLED_BY_KEY, connected.enrolled_by)
        if dialog is not None:
            dialog.accept()
        store = self._try_survey_store()
        if store is not None:
            # A fresh enrolment supersedes whatever the last sync said.
            store.set_sync_state(SYNC_ERROR_KEY, None)
        self._server_blocker.clear()
        self._refresh_server_page()
        self._refresh_sync_badge()
        # A fresh enrolment is a fresh registry to ask about.
        self._archive_probe_attempted = False
        self._maybe_refresh_archive_badges()
        self._server_notice.show_notice(f"Connected to {connected.base_url}.", SYNC_NOW)

    # --- syncing ------------------------------------------------------------

    def _on_sync_now(self) -> None:
        """Pull, then push, on a worker thread."""
        if self._server_syncing or self._server_archiving:
            return
        # A pull rewrites rows the batch worker is writing pass statuses into, so
        # the two never run at once.
        if self._survey_worker_running:
            self._server_blocker.show_blocker(SESSION_RUNNING)
            return
        engine = self._build_sync_engine()
        if engine is None:
            return
        client = self._sync_client
        self._server_syncing = True
        self._server_notice.clear()
        self._set_server_busy(True, PULLING.format(page=1))
        self._refresh_sync_badge()

        def worker() -> None:
            try:
                _heartbeat(client)
                pulled = engine.pull()
                pushed = engine.push()
            except Exception as exc:
                logger.warning("Sync failed: %s", exc)
                payload: tuple[object, object] = (None, describe_failure(exc))
            else:
                payload = ((pulled, pushed), None)
            try:
                self._sig_sync_done.emit(*payload)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the sync finished")

        threading.Thread(target=worker, daemon=True, name="registry-sync").start()

    def _build_sync_engine(self) -> SyncEngine | None:
        """A sync engine for the survey under the current output root, or None.

        None when this device is not enrolled. The page's own state says so, and
        the button that calls this is hidden then.
        """
        from deepreefmap_gui.sync import credentials
        from deepreefmap_gui.sync.client import SyncClient

        store = self._try_survey_store()
        if store is None:
            return None
        try:
            held = credentials.load()
        except Exception as exc:
            self._on_sync_done(None, describe_failure(exc))
            return None
        if held is None:
            self._refresh_server_page()
            return None
        client = SyncClient(held.base_url, held.token, agreed=read_agreed_contract(store))
        self._sync_client = client
        transport = ProgressTransport(client, self._sig_sync_progress.emit)
        return SyncEngine(
            store,
            transport,
            out_root=store.path.parent,
            classes_config=self._classes_config,
            notifications=ConflictNotifier(self._sig_notify.emit),
            # The artefact this build vendored is what the client declared, so
            # the two cannot disagree about which sections were asked for.
            pull_sections=PULL_SECTIONS,
        )

    def _remember_agreed_contract(self) -> None:
        client = self._sync_client
        self._sync_client = None
        if client is not None:
            remember_agreed_contract(self._try_survey_store(), client.agreed)

    def _on_sync_progress(self, text: str) -> None:
        if self._server_syncing:
            self._set_server_busy(True, text)

    # --- archiving ------------------------------------------------------------

    def _on_archive_now(self) -> None:
        """Offer every clip and finished run to the blob archive, on a worker thread.

        On request only: nothing here runs on a timer, so a metered field uplink
        is never spent without someone pressing for it. The whole-survey queue
        confirms with its size first, for the same reason.
        """
        from deepreefmap_gui.sync import archive

        self._archive_with_plan(archive.archive_plan, confirm_first=True)

    def _archive_video(self, video_id: str) -> None:
        """Offer one clip, from its own card. Same worker, a plan of one."""
        from deepreefmap_gui.sync import archive

        self._archive_with_plan(
            lambda store, _out_root: archive.archive_plan_for_video(store, video_id)
        )

    def _archive_run(self, run_id: object) -> None:
        """Offer one run's outputs, from its own card."""
        from deepreefmap_gui.sync import archive

        if run_id is None:
            return
        self._archive_with_plan(
            lambda store, out_root: archive.archive_plan_for_run(store, out_root, str(run_id))
        )

    def _archive_with_plan(
        self, plan_builder: Callable[..., object], *, confirm_first: bool = False
    ) -> None:
        """Plan on a worker, then upload on another, with a confirm in between.

        Planning hashes files, so it stays off the GUI thread; but what it found
        comes back here before anything is sent, so the whole-survey queue can
        say what it weighs and be declined, and every upload can be cancelled.
        """
        if self._server_syncing or self._server_archiving:
            return
        # Same guard as a sync: a running batch is still writing into the run
        # directories this would be hashing and reading.
        if self._survey_worker_running:
            self._server_blocker.show_blocker(SESSION_RUNNING)
            return
        from deepreefmap_gui.sync import credentials
        from deepreefmap_gui.sync.client import SyncClient

        store = self._try_survey_store()
        if store is None:
            return
        try:
            held = credentials.load()
        except Exception as exc:
            self._on_archive_done(describe_failure(exc))
            return
        if held is None:
            self._refresh_server_page()
            return
        # No `agreed` here: archive responses carry no contract stamp, and a
        # client that has adopted one refuses unstamped bodies.
        client = SyncClient(held.base_url, held.token)
        out_root = store.path.parent
        self._server_archiving = True
        self._archive_client = client
        self._archive_confirm_first = confirm_first
        self._archive_plan_pending = None
        self._server_notice.clear()
        self._set_server_busy(True, PLANNING_ARCHIVE)

        def worker() -> None:
            try:
                result: object = plan_builder(store, out_root)
            except Exception as exc:
                logger.warning("Archive planning failed: %s", exc)
                result = describe_failure(exc)
            try:
                self._sig_archive_plan.emit(result)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive plan was built")

        threading.Thread(target=worker, daemon=True, name="registry-archive-plan").start()

    def _on_archive_plan_ready(self, result: object) -> None:
        """Back on the GUI thread with the plan: land the digests, ask, upload."""
        from deepreefmap_gui.profiling.system_probe import format_bytes
        from deepreefmap_gui.sync import archive

        if isinstance(result, Failure):
            self._on_archive_done(result)
            return
        if not isinstance(result, ArchivePlan) or not self._server_archiving:
            return
        store = self._try_survey_store()
        if store is not None:
            # Written here rather than by the planner: the planner runs on a
            # worker thread, and the GUI thread writes this same database.
            for video_id, digest in result.hash_backfills:
                store.set_video_hash(video_id, digest)
        self._archive_plan_pending = result
        if not result.jobs:
            self._on_archive_done(ArchiveReport())
            return
        if self._archive_confirm_first and not confirm(
            self,
            ARCHIVE_NOW,
            f"Send {len(result.jobs)} file(s), about "
            f"{format_bytes(result.total_bytes)}, to the registry's archive? "
            "Files it already holds are skipped without travelling.",
        ):
            self._archive_plan_pending = None
            self._server_archiving = False
            self._set_server_busy(False)
            return
        client = self._archive_client
        if client is None:
            return
        jobs = result.jobs
        self._archive_cancel = threading.Event()
        self._server_archive_cancel_btn.setText(CANCEL_ARCHIVE)
        self._server_archive_cancel_btn.setEnabled(True)
        self._server_archive_cancel_btn.setVisible(True)

        def report(text: str, done: int, total: int) -> None:
            try:
                self._sig_archive_progress.emit(f"{text} ({min(done + 1, total)} of {total})")
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive finished")

        def worker() -> None:
            try:
                outcome: object = archive.run_archive(
                    client, jobs, report, cancel_event=self._archive_cancel
                )
            except Exception as exc:
                logger.warning("Archive failed: %s", exc)
                outcome = describe_failure(exc)
            try:
                self._sig_archive_done.emit(outcome)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive finished")

        threading.Thread(target=worker, daemon=True, name="registry-archive").start()

    def _on_archive_cancel(self) -> None:
        """Stop after the file in flight: its parts resume server-side anyway."""
        event = getattr(self, "_archive_cancel", None)
        if event is not None:
            event.set()
        self._server_archive_cancel_btn.setEnabled(False)
        self._server_archive_cancel_btn.setText(CANCELLING_ARCHIVE)

    def _on_archive_progress(self, text: str) -> None:
        if self._server_archiving:
            self._set_server_busy(True, text)

    # --- what the registry holds, for the badges ------------------------------

    def _maybe_refresh_archive_badges(self) -> None:
        """Probe once per session on the first paint that could use a badge.

        Without this an enrolled machine showed no archive state until its
        first sync or archive, which reads exactly like "not on the server".
        One attempt: offline, retrying on every card click would spend the
        field uplink on probes; a completed sync or archive asks again anyway.
        """
        if getattr(self, "_archive_states", None) is not None:
            return
        if getattr(self, "_archive_probe_attempted", False):
            return
        self._archive_probe_attempted = True
        self._refresh_archive_badges()

    def _refresh_archive_badges(self) -> None:
        """Ask the registry what it holds of this survey, off the GUI thread.

        Enrolled only, and never cached as authoritative: a badge painted from
        yesterday's answer would claim content is safe on a server that may no
        longer hold it. Offline, the maps empty out and no badge is painted.
        """
        from deepreefmap_gui.sync import credentials
        from deepreefmap_gui.sync.client import SyncClient

        if getattr(self, "_archive_badge_scan_running", False):
            return
        store = self._try_survey_store()
        if store is None:
            return
        try:
            held = credentials.load()
        except Exception:
            held = None
        if held is None:
            self._apply_archive_states(None)
            return
        client = SyncClient(held.base_url, held.token)
        self._archive_badge_scan_running = True

        def worker() -> None:
            from deepreefmap_gui.sync import archive

            try:
                states: object = archive.probe_archive_states(
                    client, store.list_videos(), store.list_runs()
                )
            except Exception as exc:
                logger.info("Archive badges not refreshed: %s", exc)
                states = None
            finally:
                self._archive_badge_scan_running = False
            try:
                self._sig_archive_states.emit(states)
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive probe answered")

        threading.Thread(target=worker, daemon=True, name="archive-badges").start()

    def _apply_archive_states(self, states: object) -> None:
        from deepreefmap_gui.sync.archive import ArchiveStates

        self._archive_states = states if isinstance(states, ArchiveStates) else None
        self._paint_archive_badges()

    def _archive_state_for_video(self, video_id: object) -> str | None:
        states = getattr(self, "_archive_states", None)
        return None if states is None else states.videos.get(str(video_id))

    def _archive_state_for_run(self, run_id: object) -> str | None:
        states = getattr(self, "_archive_states", None)
        return None if states is None else states.runs.get(str(run_id))

    def _on_archive_done(self, result: object) -> None:
        self._server_archiving = False
        self._set_server_busy(False)
        # What the plan left out belongs on the same line as what was sent, or
        # "Archived 12" reads as "archived everything".
        plan = getattr(self, "_archive_plan_pending", None)
        self._archive_plan_pending = None
        self._archive_client = None
        if isinstance(result, ArchiveReport) and plan is not None:
            result.skipped = list(plan.skipped)
        if isinstance(result, Failure):
            self._server_notice.clear()
            self._server_blocker.show_blocker(
                f"{result.title}. {result.detail}",
                RECONNECT if result.reconnect else "",
            )
            self._refresh_server_page()
            return
        if not isinstance(result, ArchiveReport):
            return
        self._server_blocker.clear()
        self._refresh_server_page()
        self._refresh_archive_badges()
        self._server_notice.show_notice(summarise_archive(result))
        if result.failed:
            label, reason = result.failed[0]
            self._notify_post(
                {
                    "fingerprint": ARCHIVE_FAILED,
                    "title": f"{len(result.failed)} file(s) did not reach the archive",
                    "body": f"First failure: {label}: {reason} Archive again to resume.",
                    "severity": WARNING,
                    "scope": SURVEY,
                    "section": SERVER_SECTION,
                }
            )

    # --- the status-bar badge -------------------------------------------------

    def _refresh_sync_badge(self) -> None:
        """Re-read the registry state for the badge, off the thread painting it.

        The read is a credential file plus one COUNT per authored section, but
        it still leaves the GUI thread: a store can sit on a mount that has
        gone away, and the badge refreshes on a timer.
        """
        if getattr(self, "_sync_badge", None) is None:
            return
        if getattr(self, "_sync_badge_scan_running", False):
            # Queued rather than dropped: a refresh asked for mid-read describes
            # a state the running read has already missed.
            self._sync_badge_rerun = True
            return
        store = self._try_survey_store()
        self._sync_badge_scan_running = True
        threading.Thread(
            target=self._read_sync_badge, args=(store,), name="sync-badge", daemon=True
        ).start()

    def _read_sync_badge(self, store: SurveyStore | None) -> None:
        try:
            state = read_state(store)
        except Exception:
            logger.exception("Could not read the registry state for the badge")
            state = None
        try:
            self._sig_sync_badge.emit(state)
        except (RuntimeError, TypeError):
            logger.debug("The window closed before the badge state was read")

    def _apply_sync_badge(self, state: object) -> None:
        self._sync_badge_scan_running = False
        badge = getattr(self, "_sync_badge", None)
        if badge is None:
            return
        # Kept beside the badge so the click can act on what is being shown
        # rather than re-reading a state that may have moved since.
        self._sync_badge_state = state if isinstance(state, ServerState) else None
        badge.show_face(self._badge_face(self._sync_badge_state))
        if getattr(self, "_sync_badge_rerun", False):
            self._sync_badge_rerun = False
            self._refresh_sync_badge()

    def _badge_face(self, state: ServerState | None) -> sync_badge.SyncBadgeFace:
        if self._server_syncing:
            return sync_badge.SYNCING
        if state is None or state.fault:
            return sync_badge.FAULT if state is not None else sync_badge.NOT_CONNECTED
        if not state.connected:
            return sync_badge.NOT_CONNECTED
        # Ahead of the pending count: rows waiting behind a failed sync are not
        # going anywhere until whatever it said is read.
        if state.sync_fault:
            return sync_badge.fault_face(state.sync_fault)
        if state.waiting:
            breakdown = ", ".join(
                f"{count} {SECTION_LABELS.get(name, name).lower()}"
                for name, count in sorted(state.pending.items())
            )
            return sync_badge.waiting_face(state.waiting, breakdown)
        # No survey open means nothing was counted, which is not the same
        # answer as everything having been sent.
        if not state.has_survey:
            return sync_badge.NO_SURVEY
        age = relative_age(state.last_sync, utc_now_iso()) if state.last_sync else ""
        return sync_badge.synced_face(age)

    def _on_sync_badge_clicked(self) -> None:
        """Sync when a sync is what is needed, otherwise open the Server page.

        Not enrolled, faulted, or a session running: the page is where the
        answer or the fix lives, so the press lands there. The running-batch
        guard inside _on_sync_now still holds either way.
        """
        state = getattr(self, "_sync_badge_state", None)
        if self._server_syncing:
            return
        ready = (
            isinstance(state, ServerState)
            and state.connected
            and not state.fault
            and not state.sync_fault
            and not self._survey_worker_running
        )
        if ready:
            self._on_sync_now()
            self._refresh_sync_badge()
            return
        self._set_simple_section(SERVER_SECTION)

    def _on_sync_done(self, reports: object, failure: object) -> None:
        self._server_syncing = False
        self._set_server_busy(False)
        # Before anything branches: a sync that failed halfway may still have been
        # told which contract it was running under, and that answer keeps.
        self._remember_agreed_contract()
        if isinstance(failure, Failure):
            self._server_notice.clear()
            store = self._try_survey_store()
            if store is not None:
                # Written before anything repaints: the badge reads from disk,
                # so an unwritten failure is a green badge on the next tick.
                store.set_sync_state(SYNC_ERROR_KEY, f"{failure.title}. {failure.detail}")
            # The page refresh first: it paints the persisted message without an
            # action, and this blocker carries the reconnect offer over it.
            self._refresh_server_page()
            self._server_blocker.show_blocker(
                f"{failure.title}. {failure.detail}",
                RECONNECT if failure.reconnect else "",
            )
            self._refresh_sync_badge()
            return
        if not isinstance(reports, tuple):
            return
        pulled, pushed = reports
        store = self._try_survey_store()
        if store is not None:
            store.set_sync_state(LAST_SYNC_KEY, utc_now_iso())
            store.set_sync_state(SYNC_ERROR_KEY, None)
        self._server_blocker.clear()
        self._refresh_server_page()
        self._refresh_sync_badge()
        # A sync proves the registry is reachable, which is when the archive
        # badges are worth asking about.
        self._refresh_archive_badges()
        self._server_notice.show_notice(summarise(pulled, pushed))
        # A pull rewrites the survey underneath every list drawn from it.
        self._refresh_transect_list()
        self._refresh_data_manager()
        self._refresh_survey_analysis()

    # --- disconnecting ------------------------------------------------------

    def _on_disconnect_server(self) -> None:
        """Forget the token and this survey's sync position, after one question."""
        if not confirm(
            self,
            DISCONNECT,
            f"{DISCONNECT_NOTE}\n\nThe survey stays exactly as it is. Disconnect this laptop?",
        ):
            return
        enrolment_mod.forget(self._try_survey_store())
        self._settings.remove(ENROLLED_BY_KEY)
        self._server_notice.clear()
        self._server_blocker.clear()
        self._refresh_server_page()


def _reference_rows(store: SurveyStore | None) -> list[tuple[str, str]]:
    """The pulled sites and campaigns, one row each, or nothing to hide the card.

    Names, not counts: a diver checking this page wants to see that the reef
    they are about to file against actually came down.
    """
    if store is None:
        return []
    rows: list[tuple[str, str]] = []
    for site in store.list_sites():
        where = ", ".join(part for part in (site.region, site.country) if part)
        rows.append((site.name, where or "Site"))
    for campaign in store.list_campaigns():
        span = " to ".join(part for part in (campaign.begin_date, campaign.end_date) if part)
        rows.append((campaign.name, span or "Campaign"))
    return rows


def _device_rows(state: ServerState) -> list[tuple[str, str]]:
    """The installation's own identity. `Onboarded by` is absent unless reported."""
    rows = [("Device id", state.device_id or "Unknown")]
    if state.enrolled_by:
        rows.append((ONBOARDED_BY, state.enrolled_by))
    return rows


def _fact_rows(state: ServerState) -> list[tuple[str, str]]:
    """The connection and the position, as the page lists them."""
    age = relative_age(state.last_sync, utc_now_iso()) if state.last_sync else ""
    if not state.last_sync:
        last = "Never"
    elif age in ("", "just now"):
        last = f"Just now ({state.last_sync})"
    else:
        last = f"{age} ago ({state.last_sync})"
    return [
        ("Server", state.base_url),
        ("Last sync", last),
        ("Pulled up to", "Nothing yet" if state.cursor is None else str(state.cursor)),
        ("Waiting to send", f"{state.waiting} row(s)"),
    ]


def summarise_archive(report: ArchiveReport) -> str:
    """One line for the notice strip, counting where every file ended up.

    Skipped items are named by count with the first reason spelled out: what the
    plan could not offer is part of where every file ended up.
    """
    parts = [
        f"Archived {report.archived} file(s)",
        f"{report.already} already on the server",
    ]
    if report.failed:
        parts.append(f"{len(report.failed)} failed")
    line = ", ".join(parts) + "."
    if report.cancelled:
        line += " Stopped on request; archive again to send the rest."
    if report.skipped:
        label, reason = report.skipped[0]
        line += f" {len(report.skipped)} item(s) were left out ({label}: {reason})."
    return line


def _heartbeat(client: object) -> None:
    """Best-effort self-report before the sync proper.

    Never fatal: the sync matters more than the courtesy, and a registry too
    old to know the route answers 404, which is also fine.
    """
    if client is None:
        return
    try:
        client.heartbeat(heartbeat_report())  # type: ignore[attr-defined]
    except Exception as exc:
        logger.info("Heartbeat not delivered: %s", exc)
