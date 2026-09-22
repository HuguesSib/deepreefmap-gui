"""The Server page: what this device is enrolled with, and the sync it drives.

A Setup view rather than a destination. The destinations are where the work is
(footage, transects, the cart, the archive) and this is a place you visit to
check a connection and leave again, so it lives on the Setup page's segmented
control beside Models and Updates. The sync badge at the foot of the window is
the day-to-day face of the connection; pressing it lands here when something
needs reading. The old section name still routes: `_set_simple_section` maps it
to Setup plus this view, because persisted notifications carry it.

Both network calls run on a worker thread and report back over the window's
signals. Sync conflicts are not reported on this page at all: the engine posts them
to the notification centre, where everything else wrong with the survey is already
read, and pressing one brings the reader back here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QWidget,
)

from deepreefmap_gui.core import sync_badge
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
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.models.cache_ui import MODELS_SECTION
from deepreefmap_gui.notify.widgets import relative_age
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.server import enrolment as enrolment_mod
from deepreefmap_gui.server import reachability
from deepreefmap_gui.server.connect_ui import ConnectDialog
from deepreefmap_gui.server.state import (
    DEVICE_NAME_KEY,
    DISCLAIMER,
    DISCLAIMER_TITLE,
    ENROLLED_BY_KEY,
    LAST_SYNC_KEY,
    SECTION_LABELS,
    SERVER_SECTION,
    SYNC_ERROR_KEY,
    SYNC_ERROR_KIND_KEY,
    Failure,
    ServerState,
    SyncOutcome,
    default_device_name,
    describe_failure,
    half_note,
    heartbeat_report,
    read_agreed_contract,
    read_state,
    remember_agreed_contract,
    summarise,
)
from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.survey.models.notification import INFO, MACHINE, SURVEY, WARNING
from deepreefmap_gui.survey.preset import (
    registry_preset,
    remember_assignment,
    resolved_identity,
)
from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync.archive import ArchiveJob, ArchivePlan, ArchiveReport, TransferProgress
from deepreefmap_gui.sync.contract import READ_SECTIONS
from deepreefmap_gui.sync.engine import PullReport, PushReport, SyncEngine

logger = logging.getLogger(__name__)

PAGE_TITLE = "Server"
PAGE_CAPTION = "Share this survey's records with a registry."

NOT_CONNECTED = "Not connected."
NOT_CONNECTED_HINT = "Paste a connect code to join a registry."

DEVICE_CARD = "This device"
ATTRIBUTION_NOTE = "Uploads are attributed to this name. Rename it in the web interface."

REFERENCE_NOTE = (
    "Sites and campaigns are shared with the registry. One made here is sent up; "
    "a change to one the console owns is sent as a proposal."
)
ONBOARDED_BY = "Onboarded by"
# Whether the registry is answering at all, asked apart from any sync.
SERVER_STATUS = "Server status"
# Records the registry holds that could not be taken, by name.
SET_ASIDE = "Not taken"

CONNECT = "Connect to server"
RECONNECT = "Connect again"
SYNC_NOW = "Sync now"
DISCONNECT = "Disconnect"

# Said beside the button as well as on it.
DISCONNECT_NOTE = "Forgets the token on this laptop. Revoke the device in the web interface."

SESSION_RUNNING = "Wait for the current session to finish, then sync."

PULLING = "Pulling changes (page {page})…"
SENDING = "Sending {rows} row(s)…"

ARCHIVE_NOW = "Archive to server"
ARCHIVE_TOOLTIP = "Send original clips and finished run outputs to the registry's archive."
PLANNING_ARCHIVE = "Working out what to archive…"
CANCEL_ARCHIVE = "Cancel archive"
CANCELLING_ARCHIVE = "Finishing the file in flight…"
# One episode per pass over the queue: re-archiving resumes server-side, so the
# same fingerprint updating in place is the right shape for a retry.
ARCHIVE_FAILED = "archive.upload_failed"

# Said when an archive is asked for on a laptop that never enrolled.
ARCHIVE_NOT_CONNECTED = "Connect this laptop to a registry before archiving."

# The upload gauge: permille steps, and the width of the model download bar.
GAUGE_STEPS = 1000
GAUGE_WIDTH = 150

DOWNLOAD_NOW = "Download now"
# One episode per preset identity: a new version naming new models is new news.
PRESET_MODELS_MISSING = "presets.models_missing"


class ConflictNotifier:
    """The engine's conflict sink, delivered to the bell on the GUI thread.

    The engine posts from the worker thread and the notification centre belongs to
    the GUI one, so everything goes through `_sig_notify`, the one route a worker
    has to the bell. The pass is stamped here: a conflict is read on the Server
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
    """DeepReefMapWindow methods for the Server pass and the sync it runs."""

    _server_syncing: bool = False
    _server_archiving: bool = False
    _connect_dialog: ConnectDialog | None = None
    # The client the running sync is using, kept for the version it learned.
    _sync_client: Any | None = None
    # The archive flow between its two workers: the client the plan was built
    # for, and the plan awaiting confirmation or upload.
    _archive_pause_requested: bool = False
    _archive_session: int = 0
    _archive_retry_builder: Any = None
    _archive_builder: Any = None
    _archive_video_faces: dict[str, str] = {}
    _archive_item_faces: dict[str, str] = {}
    _archive_client: Any | None = None
    # The run a row-level press is archiving, and how its row is dressed until
    # the registry is asked again: run id to (state, tooltip note).
    _archive_focus_run: str | None = None
    _archive_run_faces: dict[str, tuple[str, str | None]] = {}
    _archive_plan_pending: ArchivePlan | None = None
    # What the last sync found the resolved server preset still missing, kept
    # for the notice strip's Download now press.
    _preset_missing_models: tuple[str, ...] = ()

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

        # Its own strip rather than a message through the sync one, which every
        # exchange rewrites: the offer has to stand until it is taken or the
        # models arrive some other way.
        self._preset_models_notice = NoticeStrip()
        self._preset_models_notice.action_clicked.connect(self._download_missing_preset_models)
        body.addWidget(self._preset_models_notice)

        self._server_empty = EmptyState(NOT_CONNECTED, NOT_CONNECTED_HINT)
        body.addWidget(self._server_empty)

        # The terms of the exchange, on the page where it is agreed to. Gone
        # after the first sync: whoever kept syncing has read it.
        self._server_disclaimer_card, disclaimer_layout = section_card(DISCLAIMER_TITLE)
        for paragraph in DISCLAIMER:
            line = muted_label(paragraph)
            line.setWordWrap(True)
            disclaimer_layout.addWidget(line)
        body.addWidget(self._server_disclaimer_card)

        self._server_device_card, device_layout = section_card(DEVICE_CARD)
        self._server_device_label = QLabel("")
        self._server_device_label.setStyleSheet(f"font-size: {FONT_LG}; font-weight: {WEIGHT_SEMIBOLD};")
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

        # The shared catalogue, whichever end authored it: sites and campaigns
        # travel both ways, and are chosen on the Transects page and wherever a
        # pass is filed.
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

        # The upload's own gauge: the text beside the spinner names the file in
        # flight, this says how much of the queue's bytes have landed and how
        # fast they move. Permille rather than percent, so a long queue still
        # visibly creeps between one file and the next.
        self._server_archive_bar = QProgressBar()
        self._server_archive_bar.setRange(0, GAUGE_STEPS)
        self._server_archive_bar.setTextVisible(False)
        self._server_archive_bar.setFixedWidth(GAUGE_WIDTH)
        row.addWidget(self._server_archive_bar)
        self._server_archive_bytes_label = muted_label("")
        row.addWidget(self._server_archive_bytes_label)
        self._reset_archive_gauge()

        # Beside the progress it stops: an upload on a field uplink can be hours
        # of PUTs, and until this existed nothing could end it early.
        self._server_archive_cancel_btn = QPushButton(CANCEL_ARCHIVE)
        self._server_archive_cancel_btn.setProperty("quiet", "true")
        self._server_archive_cancel_btn.setVisible(False)
        self._server_archive_cancel_btn.clicked.connect(self._on_archive_cancel)
        row.addWidget(self._server_archive_cancel_btn)
        self._server_archive_pause_btn = QPushButton("Pause archive")
        self._server_archive_pause_btn.setVisible(False)
        self._server_archive_pause_btn.clicked.connect(self._on_archive_pause)
        row.addWidget(self._server_archive_pause_btn)

        self._server_connect_btn = QPushButton(CONNECT)
        self._server_connect_btn.setProperty("cta", "true")
        self._server_connect_btn.clicked.connect(self._on_connect_server)
        row.addWidget(self._server_connect_btn)

        self._server_sync_btn = QPushButton(SYNC_NOW)
        self._server_sync_btn.setToolTip(
            "Take everything the registry has for this survey, then offer everything edited here."
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

    # --- painting -----------------------------------------------------------

    def _refresh_server_page(self) -> None:
        """Re-read the credential and the sync position, and paint them."""
        if not hasattr(self, "_server_facts"):
            return
        state = read_state(self._try_survey_store(), self._server_device_name(), self._server_enrolled_by())
        connected = state.connected
        self._server_empty.setVisible(not connected)
        # Before enrolment and up to the first sync, whichever comes first for
        # this survey: last_sync is per survey database, so a fresh output root
        # shows the terms again.
        self._server_disclaimer_card.setVisible(not state.last_sync)
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
            self._server_facts.set_rows(_fact_rows(state, self._server_reachability()))
            self._server_waiting.set_rows(
                [(SECTION_LABELS.get(section, section), str(count)) for section, count in state.pending.items()]
            )
        # Asked here as well as on the badge's cadence, so a page opened to find
        # out what is wrong is not reading a minute-old answer.
        self._probe_server()
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
            self._reset_archive_gauge()

    def _reset_archive_gauge(self) -> None:
        """Empty the upload gauge, so the next pass never opens on the last one's fill."""
        self._server_archive_bar.setValue(0)
        self._server_archive_bar.setVisible(False)
        self._server_archive_bytes_label.setText("")
        self._server_archive_bytes_label.setVisible(False)

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
        # Read on the GUI thread: the store accessor touches widgets. The store
        # itself is safe to hand over, its connections are per thread.
        store = self._try_survey_store()
        self._server_syncing = True
        self._server_notice.clear()
        self._preset_models_notice.clear()
        self._set_server_busy(True, PULLING.format(page=1))
        self._refresh_sync_badge()

        def worker() -> None:
            # Two halves, two try blocks. A pull that cannot land a page of
            # somebody else's data must not take the push with it: the records
            # made on this laptop exist nowhere else, and a page that fails the
            # same way every sync would strand them for the rest of the season.
            heartbeat = _heartbeat(client, store)
            pulled: PullReport | None = None
            pull_failure: Failure | None = None
            try:
                pulled = engine.pull()
            except Exception as exc:
                logger.warning("The pull did not finish: %s", exc)
                pull_failure = describe_failure(exc)
            pushed: PushReport | None = None
            push_failure: Failure | None = None
            performance_waiting = 0
            try:
                pushed = engine.push()
            except Exception as exc:
                logger.warning("The push did not finish: %s", exc)
                push_failure = describe_failure(exc)
            try:
                performance_waiting = _sync_performance_history(client, heartbeat)
            except Exception as exc:
                logger.warning("Performance history did not sync: %s", exc)
                if push_failure is None:
                    push_failure = describe_failure(exc)
                    pushed = None
            outcome = SyncOutcome(
                pulled,
                pushed,
                pull_failure,
                push_failure,
                performance_waiting=performance_waiting,
            )
            try:
                self._sig_sync_done.emit(outcome, None)
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
            pull_sections=READ_SECTIONS,
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

        if self._archive_retry_builder is not None:
            self._archive_with_plan(self._archive_retry_builder)
        else:
            self._archive_with_plan(
                lambda store, root: archive.archive_plan(store, root, self._archive_cancel), confirm_first=True
            )

    def _archive_video(self, video_id: str) -> None:
        """Offer one clip, from its own card. Same worker, a plan of one."""
        from deepreefmap_gui.sync import archive

        session = self._archive_session
        self._archive_with_plan(
            lambda store, _out_root: archive.archive_plan_for_video(store, video_id, self._archive_cancel)
        )
        if self._server_archiving and self._archive_session != session:
            self._archive_video_faces = {**self._archive_video_faces, str(video_id): "preparing"}
            self._paint_archive_badges()

    def _archive_run(self, run_id: object) -> None:
        """Offer one run's outputs, from its own card."""
        from deepreefmap_gui.sync import archive

        if run_id is None:
            return
        session = self._archive_session
        self._archive_with_plan(
            lambda store, out_root: archive.archive_plan_for_run(store, out_root, str(run_id), self._archive_cancel)
        )
        if not self._server_archiving or self._archive_session == session:
            return
        # Progress is shown on the run's own row, where the press was made.
        self._archive_focus_run = str(run_id)
        self._set_archive_run_face(str(run_id), "uploading")

    def _archive_with_plan(self, plan_builder: Callable[..., object], *, confirm_first: bool = False) -> None:
        """Plan on a worker, then upload on another, with a confirm in between.

        Planning hashes files, so it stays off the GUI thread; but what it found
        comes back here before anything is sent, so the whole-survey queue can
        say what it weighs and be declined, and every upload can be cancelled.
        """
        if self._server_syncing or self._server_archiving:
            self._set_simple_section(SERVER_SECTION)
            self._server_notice.show_notice("A server operation is active. Follow its progress here.")
            return
        # Same guard as a sync: a running batch is still writing into the run
        # directories this would be hashing and reading.
        if self._survey_worker_running:
            self._server_blocker.show_blocker(SESSION_RUNNING)
            return
        from deepreefmap_gui.sync import credentials
        from deepreefmap_gui.sync.archive_client import ArchiveClient

        store = self._try_survey_store()
        if store is None:
            return
        try:
            held = credentials.load()
        except Exception as exc:
            self._on_archive_done(describe_failure(exc))
            return
        if held is None:
            # Reachable from the run and clip cards, whose Archive actions do
            # not hide with the Server page's buttons. The Connect offer lives
            # on the Server page, so the press lands there.
            self._set_simple_section(SERVER_SECTION)
            self._refresh_server_page()
            self._server_blocker.show_blocker(ARCHIVE_NOT_CONNECTED, CONNECT)
            return
        # No `agreed` here: archive responses carry no contract stamp, and a
        # client that has adopted one refuses unstamped bodies.
        client = ArchiveClient(held.base_url, held.token)
        self._start_archive_plan(client, store, plan_builder, confirm_first)

    def _start_archive_plan(
        self, client: Any, store: SurveyStore, plan_builder: Callable[..., object], confirm_first: bool
    ) -> None:
        out_root = store.path.parent
        self._archive_pause_requested = False
        self._archive_session += 1
        session = self._archive_session
        self._archive_builder = plan_builder
        self._archive_retry_builder = None
        self._archive_item_faces = {}
        self._archive_cancel = threading.Event()
        self._server_archive_btn.setText(ARCHIVE_NOW)
        self._server_archiving = True
        self._archive_client = client
        self._archive_confirm_first = confirm_first
        self._archive_plan_pending = None
        self._server_notice.clear()
        self._set_server_busy(True, PLANNING_ARCHIVE)
        self._server_archive_cancel_btn.setVisible(True)
        self._server_archive_cancel_btn.setEnabled(True)
        self._server_archive_cancel_btn.setText(CANCEL_ARCHIVE)
        self._server_archive_pause_btn.setVisible(True)
        self._server_archive_pause_btn.setEnabled(True)

        def worker() -> None:
            try:
                result: object = plan_builder(store, out_root)
            except Exception as exc:
                logger.warning("Archive planning failed: %s", exc)
                result = describe_failure(exc)
            try:
                self._sig_archive_plan.emit((session, result))
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive plan was built")

        threading.Thread(target=worker, daemon=True, name="registry-archive-plan").start()

    def _on_archive_plan_ready(self, result: object) -> None:
        """Accept the current session's plan and start its transfer workers."""
        if isinstance(result, tuple):
            session, result = result
            if session != self._archive_session:
                return
        if self._archive_cancel.is_set():
            self._archive_retry_builder = self._archive_builder
            self._on_archive_done(ArchiveReport(cancelled=True))
            self._server_archive_btn.setText("Resume archive")
            return
        if isinstance(result, Failure):
            self._on_archive_done(result)
            return
        if not self._server_archiving:
            return
        if not isinstance(result, ArchivePlan):
            self._on_archive_done(describe_failure(RuntimeError("Archive planning returned no plan")))
            return
        store = self._try_survey_store()
        if store is not None:
            # Written here rather than by the planner: the planner runs on a
            # worker thread, and the GUI thread writes this same database.
            try:
                for video_id, digest in result.hash_backfills:
                    store.set_video_hash(video_id, digest)
            except Exception as exc:
                self._on_archive_done(describe_failure(exc))
                return
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
            self._on_archive_done(ArchiveReport(cancelled=True, remaining=result.jobs))
            return
        self._start_archive_transfer(result)

    def _start_archive_transfer(self, result: ArchivePlan) -> None:
        from deepreefmap_gui.sync import archive

        client = self._archive_client
        if client is None:
            self._on_archive_done(ArchiveReport(cancelled=True, remaining=result.jobs))
            return
        session = self._archive_session
        jobs = result.jobs
        for job in jobs:
            self._on_archive_item((session, job, "queued"), repaint=False)
        self._paint_archive_badges()
        self._archive_cancel = threading.Event()
        self._server_archive_cancel_btn.setText(CANCEL_ARCHIVE)
        self._server_archive_cancel_btn.setEnabled(True)
        self._server_archive_cancel_btn.setVisible(True)

        def report(text: str, done: int, total: int) -> None:
            try:
                self._sig_archive_progress.emit((session, f"{text} ({done} of {total} verified)"))
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive finished")

        def on_bytes(reading: object) -> None:
            try:
                self._sig_archive_bytes.emit((session, reading))
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive finished")

        def worker() -> None:
            try:
                outcome: object = archive.run_archive(
                    client, jobs, report, cancel_event=self._archive_cancel, on_bytes=on_bytes,
                    on_state=lambda job, phase: self._sig_archive_item.emit((session, job, phase)),
                )
            except Exception as exc:
                logger.warning("Archive failed: %s", exc)
                outcome = describe_failure(exc)
            try:
                self._sig_archive_done.emit((session, outcome))
            except (RuntimeError, TypeError):
                logger.debug("The window closed before the archive finished")

        threading.Thread(target=worker, daemon=True, name="registry-archive").start()

    def _on_archive_pause(self) -> None:
        self._archive_pause_requested = True
        self._server_archive_pause_btn.setEnabled(False)
        self._on_archive_cancel()

    def _on_archive_cancel(self) -> None:
        """Stop after the file in flight: its parts resume server-side anyway."""
        event = getattr(self, "_archive_cancel", None)
        if event is not None:
            event.set()
        client = self._archive_client
        if client is not None and hasattr(client, "cancel"):
            threading.Thread(target=client.cancel, daemon=True, name="archive-cancel").start()
        self._server_archive_cancel_btn.setEnabled(False)
        self._server_archive_cancel_btn.setText(CANCELLING_ARCHIVE)

    def _on_archive_item(self, event: object, *, repaint: bool = True) -> None:
        if not isinstance(event, tuple) or len(event) != 3:
            return
        session, job, phase = event
        if not isinstance(job, ArchiveJob) or not isinstance(phase, str):
            return
        if session != self._archive_session or not self._server_archiving:
            return
        self._archive_item_faces[str(job.path)] = phase
        if job.video_id is not None:
            self._archive_video_faces = {**self._archive_video_faces, job.video_id: phase}
        if job.run_id is not None:
            plan = self._archive_plan_pending
            faces = [self._archive_item_faces.get(str(item.path), "queued")
                     for item in plan.jobs if item.run_id == job.run_id] if plan else [phase]
            settled = "archived" if all(face == "archived" for face in faces) else phase
            if settled == "archived" and any(face != "archived" for face in faces):
                settled = "uploading"
            for failure in ("failed", "paused", "cancelled"):
                if failure in faces:
                    settled = failure
                    break
            self._archive_run_faces = {**self._archive_run_faces, job.run_id: (settled, None)}
        now = time.monotonic()
        if repaint and now - getattr(self, "_archive_last_paint", 0) >= 0.2:
            self._archive_last_paint = now
            self._paint_archive_badges()

    def _on_archive_progress(self, text: object) -> None:
        if isinstance(text, tuple) and len(text) == 2:
            session, text = text
            if session != self._archive_session:
                return
        if self._server_archiving and isinstance(text, str):
            self._set_server_busy(True, text)

    def _on_archive_bytes(self, reading: object) -> None:
        """A byte reading from the upload worker, painted as the gauge.

        Delivered over `_sig_archive_bytes` rather than called: the meter is fed
        on the upload thread, and every widget touched here belongs to this one.
        Readings that arrive after the pass has been reported are dropped, or a
        gauge already emptied would fill again behind the summary.
        """
        if isinstance(reading, tuple):
            session, reading = reading
            if session != self._archive_session:
                return
        if not self._server_archiving or not isinstance(reading, TransferProgress):
            return
        total = reading.total_bytes
        filled = int(GAUGE_STEPS * reading.done_bytes / total) if total else GAUGE_STEPS
        self._server_archive_bar.setValue(min(filled, GAUGE_STEPS))
        text = f"{format_bytes(reading.done_bytes)} of {format_bytes(total)}"
        if reading.speed_bps:
            text += f" · {format_bytes(reading.speed_bps)}/s"
        self._server_archive_bytes_label.setText(text)
        self._server_archive_bar.setVisible(True)
        self._server_archive_bytes_label.setVisible(True)

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
                states: object = archive.probe_archive_states(client, store.list_videos(), store.list_runs())
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
        # The registry's account replaces this device's own; offline, the local
        # answer stands until it can be checked.
        if self._archive_states is not None and not self._server_archiving:
            self._archive_run_faces = {key: value for key, value in self._archive_run_faces.items()
                                       if value[0] in {"failed", "paused", "cancelled"}}
            self._archive_video_faces = {key: value for key, value in self._archive_video_faces.items()
                                         if value in {"failed", "paused", "cancelled"}}
        self._paint_archive_badges()

    def _archive_state_for_video(self, video_id: object) -> str | None:
        face = self._archive_video_faces.get(str(video_id))
        if face is not None:
            return face
        states = getattr(self, "_archive_states", None)
        return None if states is None else states.videos.get(str(video_id))

    def _archive_state_for_run(self, run_id: object) -> str | None:
        face = self._archive_run_faces.get(str(run_id))
        if face is not None:
            return face[0]
        states = getattr(self, "_archive_states", None)
        return None if states is None else states.runs.get(str(run_id))

    def _archive_note_for_run(self, run_id: object) -> str | None:
        face = self._archive_run_faces.get(str(run_id))
        return None if face is None else face[1]

    def _set_archive_run_face(self, run_id: str, state: str | None, note: str | None = None) -> None:
        """Dress one run's row and card from this device's own archive attempt.

        The face holds until the registry answers a probe, which is the account
        that outranks it.
        """
        faces = dict(self._archive_run_faces)
        if state is None:
            faces.pop(run_id, None)
        else:
            faces[run_id] = (state, note)
        self._archive_run_faces = faces
        self._paint_archive_badges()

    def _settle_archive_run_face(self, result: object, plan: ArchivePlan | None) -> None:
        run_id = self._archive_focus_run
        self._archive_focus_run = None
        if run_id is None:
            return
        if isinstance(result, Failure):
            self._set_archive_run_face(run_id, "failed", f"{result.title}. {result.detail}")
            return
        if not isinstance(result, ArchiveReport) or result.cancelled:
            self._set_archive_run_face(run_id, None)
            return
        if result.failed:
            label, reason = result.failed[0]
            self._set_archive_run_face(run_id, "failed", f"{label}: {reason}")
            return
        # A plan with nothing to send and a reason why is a run that could not
        # be archived, not one that was.
        if plan is not None and not plan.jobs and plan.skipped:
            label, reason = plan.skipped[0]
            self._set_archive_run_face(run_id, "failed", f"{label}: {reason}")
            return
        self._set_archive_run_face(run_id, "archived")

    def _on_archive_done(self, result: object) -> None:
        if isinstance(result, tuple):
            session, result = result
            if session != self._archive_session:
                return
        self._server_archiving = False
        self._set_server_busy(False)
        # What the plan left out belongs on the same line as what was sent, or
        # "Archived 12" reads as "archived everything".
        plan = getattr(self, "_archive_plan_pending", None)
        self._archive_plan_pending = None
        if isinstance(result, ArchiveReport) and self._archive_pause_requested:
            result.paused = True
            result.cancelled = False
        self._settle_archive_faces(result)
        self._server_archive_pause_btn.setVisible(False)
        client = self._archive_client
        self._archive_client = None
        if client is not None and hasattr(client, "close"):
            client.close()
        self._server_archive_cancel_btn.setVisible(False)
        if not isinstance(result, ArchiveReport) or not (result.paused or result.cancelled):
            self._settle_archive_run_face(result, plan)
        if isinstance(result, ArchiveReport) and plan is not None:
            result.skipped = list(plan.skipped)
        if isinstance(result, Failure):
            self._archive_retry_builder = self._archive_builder
            self._server_archive_btn.setText("Retry archive")
            active = {"preparing", "queued", "uploading", "verifying"}
            self._archive_video_faces = {
                key: "failed" if value in active else value for key, value in self._archive_video_faces.items()
            }
            self._paint_archive_badges()
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
        self._offer_archive_retry(result)
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

    def _settle_archive_faces(self, result: object) -> None:
        if not isinstance(result, ArchiveReport) or not (result.paused or result.cancelled):
            return
        phase = "paused" if result.paused else "cancelled"
        active = {"preparing", "queued", "uploading", "verifying", "cancelled"}
        self._archive_video_faces = {
            key: phase if value in active else value for key, value in self._archive_video_faces.items()
        }
        self._archive_run_faces = {
            key: (phase, value[1]) if value[0] in active else value for key, value in self._archive_run_faces.items()
        }
        self._paint_archive_badges()

    def _offer_archive_retry(self, result: ArchiveReport) -> None:
        if result.remaining and self._archive_builder is not None:
            builder = self._archive_builder
            paths = {job.path for job in result.remaining}

            def rebuild(store, out_root):
                plan = builder(store, out_root)
                plan.jobs = [job for job in plan.jobs if job.path in paths]
                return plan

            self._archive_retry_builder = rebuild
            self._server_archive_btn.setText("Resume archive" if result.paused or result.cancelled else "Retry failed")

    # --- the status-bar badge -------------------------------------------------

    def _refresh_sync_badge(self) -> None:
        """Re-read the registry state for the badge, off the thread painting it.

        The read is a credential file plus one COUNT per authored pass, but
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
        threading.Thread(target=self._read_sync_badge, args=(store,), name="sync-badge", daemon=True).start()

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
        # Shown only once an enrolment exists, or when the credential is there
        # but unreadable, which is worth a face rather than silence.
        # Disconnecting hides it again on the next repaint.
        shown = self._sync_badge_state is not None and (
            self._sync_badge_state.connected or bool(self._sync_badge_state.fault)
        )
        badge.setVisible(shown)
        badge.show_face(self._badge_face(self._sync_badge_state))
        if getattr(self, "_sync_badge_rerun", False):
            self._sync_badge_rerun = False
            self._refresh_sync_badge()
        # Every archive control in the app hangs off the same credential this
        # badge does, so they are offered and withdrawn together, and enrolling
        # or disconnecting reaches them without leaving the page.
        self._refresh_archive_affordances(
            self._sync_badge_state is not None and self._sync_badge_state.connected
        )
        # The badge state is read from disk and says nothing about the network,
        # so the address it just produced is what the probe is aimed at.
        self._probe_server()

    # --- is the registry answering -------------------------------------------

    def _server_reachability(self) -> reachability.Reachability:
        """The last answer from the registry probe, unasked until one lands."""
        return getattr(self, "_server_reach", reachability.UNCHECKED)

    def _probe_server(self, force: bool = False) -> None:
        """Ask whether the registry knows this device, off the thread painting the badge.

        Only for an enrolled laptop: the probe carries this installation's own
        token, and one that never joined a registry has nothing to be refused
        or unavailable. Not while an exchange is in flight either, since a sync
        is the better evidence and reports on the same fields when it lands.
        """
        state = getattr(self, "_sync_badge_state", None)
        if not isinstance(state, ServerState) or not state.connected:
            return
        if self._server_syncing or self._server_archiving:
            return
        if getattr(self, "_server_probe_running", False):
            return
        if not force and not self._server_reachability().stale(time.monotonic()):
            return
        self._server_probe_running = True
        threading.Thread(target=self._read_server_reachability, name="server-probe", daemon=True).start()

    def _read_server_reachability(self) -> None:
        reading = reachability.probe()
        try:
            self._sig_server_reach.emit(reading)
        except (RuntimeError, TypeError):
            logger.debug("The window closed before the registry answered")

    def _apply_server_reachability(self, reading: object) -> None:
        self._server_probe_running = False
        if not isinstance(reading, reachability.Reachability):
            return
        self._server_reach = reading
        badge = getattr(self, "_sync_badge", None)
        if badge is not None:
            badge.show_face(self._badge_face(getattr(self, "_sync_badge_state", None)))
        self._refresh_server_page()

    def _badge_face(self, state: ServerState | None) -> sync_badge.SyncBadgeFace:
        if self._server_syncing:
            return sync_badge.SYNCING
        if state is None or state.fault:
            return sync_badge.FAULT if state is not None else sync_badge.NOT_CONNECTED
        if not state.connected:
            return sync_badge.NOT_CONNECTED
        # Ahead of the stored sync fault, which is what happened last time: a
        # registry that is not answering now explains the fault as well as the
        # rows, and a laptop carried out of wifi is owed that answer and not a
        # failure it can do nothing about.
        reach = self._server_reachability()
        if reach.unavailable:
            return sync_badge.unavailable_face(reach.detail)
        # The registry answered and will not have this laptop. Read now rather
        # than at the next sync, because nothing waiting here is going anywhere
        # until somebody issues a fresh connect code.
        if reach.denied:
            return sync_badge.rejected_face(reach.detail)
        # Before the probe has answered, the last sync is the only evidence there
        # is, and a sync that got no answer already said the server was down.
        # Wording that as a fault would tell a diver something is wrong with
        # their laptop over what is almost always a boat out of range.
        if state.sync_fault and state.sync_fault_unreachable and not reach.answered:
            return sync_badge.unavailable_face(state.sync_fault)
        # Ahead of the pending count: rows waiting behind a failed sync are not
        # going anywhere until whatever it said is read.
        if state.sync_fault:
            return sync_badge.fault_face(state.sync_fault)
        if state.waiting:
            breakdown = ", ".join(
                f"{count} {SECTION_LABELS.get(name, name).lower()}" for name, count in sorted(state.pending.items())
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
        """One sync's two halves, however many of them got to run.

        ``reports`` carries both halves once the worker reached them.
        ``failure`` is for an exchange that never started, which is a credential
        this page could not read.
        """
        self._server_syncing = False
        self._set_server_busy(False)
        # Before anything branches: a sync that failed halfway may still have been
        # told which contract it was running under, and that answer keeps.
        self._remember_agreed_contract()
        outcome = reports if isinstance(reports, SyncOutcome) else None
        blocker = outcome.blocker if outcome is not None else failure
        if outcome is None and not isinstance(blocker, Failure):
            return
        store = self._try_survey_store()
        if store is not None:
            # Written before anything repaints: the badge reads from disk, so an
            # unwritten failure is a green badge on the next tick.
            store.set_sync_state(
                SYNC_ERROR_KEY,
                f"{blocker.title}. {blocker.detail}" if isinstance(blocker, Failure) else None,
            )
            # Beside the words, so the badge can pick a face from it on a launch
            # that happens before the first probe has answered.
            store.set_sync_state(
                SYNC_ERROR_KIND_KEY,
                blocker.kind or None if isinstance(blocker, Failure) else None,
            )
            # Only a sync that ran both halves dates the survey: the badge's
            # "synced N minutes ago" must not stand for half an exchange.
            if outcome is not None and outcome.complete:
                store.set_sync_state(LAST_SYNC_KEY, utc_now_iso())
        if isinstance(blocker, Failure):
            # A sync that failed is the moment the link is most in question, so
            # the probe is re-asked rather than left to its interval: it decides
            # whether the badge says the registry is unreachable or that it
            # answered and refused.
            self._probe_server(force=True)
            self._server_notice.clear()
            # The page refresh first: it paints the persisted message without an
            # action, and this blocker carries the reconnect offer over it.
            self._refresh_server_page()
            self._server_blocker.show_blocker(
                " ".join(filter(None, [f"{blocker.title}. {blocker.detail}", half_note(outcome)])),
                RECONNECT if blocker.reconnect else "",
            )
        else:
            self._server_blocker.clear()
            self._refresh_server_page()
        self._refresh_sync_badge()
        if outcome is None:
            return
        # A sync proves the registry is reachable, which is when the archive
        # badges are worth asking about.
        self._refresh_archive_badges()
        if blocker is None:
            message = summarise(outcome.pull, outcome.push)
            if outcome.performance_waiting:
                message += (
                    f" {outcome.performance_waiting} performance observation(s) are waiting "
                    "for a registry that supports performance history."
                )
            self._server_notice.show_notice(message)
        # After the pull has landed, so it sees the preset row and the
        # assignment in whichever order the registry delivered them.
        self._offer_preset_model_downloads(store)
        # After the pull for the same reason: a preset naming a profile that
        # arrived in this same exchange must find the file on disk.
        if store is not None:
            self._write_registry_camera_profiles(store)
        # A pull rewrites the survey underneath every list drawn from it, and
        # every box that offers a choice out of it.
        self._refresh_site_choices()
        self._refresh_transect_list()
        self._refresh_data_manager()
        self._refresh_survey_analysis()

    def _offer_preset_model_downloads(self, store: SurveyStore | None) -> None:
        """Offer to fetch the models the resolved server preset still needs.

        Run after every successful sync, which covers both orderings: the
        heartbeat can record an assignment before the preset row has been
        pulled, and the row landing on a later pull re-raises the offer.

        The strip alone would not reach anybody. The sync that raises it is
        usually run from the status-bar badge rather than from this page, so
        the offer also tints Setup's Models segment, and the notification it
        posts names that view rather than settling for Setup and reopening
        whichever view was last on screen.
        """
        needed = self._preset_models_needed(store)
        if needed is None:
            self._preset_missing_models = ()
            self._preset_models_notice.clear()
        else:
            name, version, missing = needed
            self._preset_missing_models = missing
            self._preset_models_notice.show_notice(
                f"{name} (v{version}) needs {len(missing)} model(s) this laptop has "
                f"not downloaded: {', '.join(missing)}.",
                DOWNLOAD_NOW,
            )
            self._notify_post(
                {
                    "fingerprint": f"{PRESET_MODELS_MISSING}.{name}.{version}",
                    "title": f"{name} (v{version}) names models that are not downloaded",
                    "body": (f"Missing: {', '.join(missing)}. Download them before running a session."),
                    "severity": WARNING,
                    "scope": MACHINE,
                    "section": MODELS_SECTION,
                }
            )
        self._refresh_models_segment()

    def _preset_models_needed(self, store: SurveyStore | None) -> tuple[str, int, tuple[str, ...]] | None:
        """The resolved server preset, and the weights it names that are not here.

        Answered from what _refresh_model_status verified on its worker thread,
        never by verifying here, for the reasons _survey_missing_models gives:
        until the first refresh lands, nothing is offered. The run gate stays
        the backstop either way.
        """
        if store is None or not self._last_model_states:
            return None
        identity = resolved_identity(store)
        if identity is None:
            return None
        name, version = identity
        row = store.get_server_preset(name, version)
        if row is None:
            return None
        from deepreefmap_gui.models.cache import required_model_names

        required = required_model_names(registry_preset(name, version, row.settings).settings)
        missing = tuple(
            sorted(
                info.name
                for info, cached in self._last_model_states
                if info.name in required and not cached and info.name not in self._downloading
            )
        )
        return (name, version, missing) if missing else None

    def _download_missing_preset_models(self) -> None:
        """Start the offered downloads through the model library's own path.

        _download_model carries the free-disk refusal, the progress rendering
        and the retry-with-reason, so the offer inherits all three.
        """
        for model_name in self._preset_missing_models:
            self._download_model(model_name)
        self._preset_missing_models = ()
        self._preset_models_notice.clear()
        self._refresh_models_segment()

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
    """The shared sites and campaigns, one row each, or nothing to hide the card.

    Named rather than counted: a diver checking this page wants to see that the
    reef they are about to file against actually came down. The usage on the end
    of each line answers the next question, which is whether anything is filed
    against it yet.
    """
    if store is None:
        return []
    transects = store.list_transects()
    passes = store.list_passes()
    rows: list[tuple[str, str]] = []
    for site in store.list_sites():
        where = ", ".join(part for part in (site.region, site.country) if part)
        lines = sum(1 for transect in transects if transect.site_id == site.id)
        rows.append((site.name, _detail(where or "Site", _counted(lines, "transect"))))
    for campaign in store.list_campaigns():
        span = " to ".join(part for part in (campaign.begin_date, campaign.end_date) if part)
        filed = sum(1 for pass_ in passes if pass_.campaign_id == campaign.id)
        rows.append((campaign.name, _detail(span or "Campaign", _counted(filed, "pass"))))
    return rows


def _counted(count: int, noun: str) -> str:
    plural = f"{noun}es" if noun.endswith("s") else f"{noun}s"
    return f"{count} {noun if count == 1 else plural}"


def _detail(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


def _device_rows(state: ServerState) -> list[tuple[str, str]]:
    """The installation's own identity. `Onboarded by` is absent unless reported."""
    rows = [("Device id", state.device_id or "Unknown")]
    if state.enrolled_by:
        rows.append((ONBOARDED_BY, state.enrolled_by))
    return rows


def _fact_rows(state: ServerState, reach: reachability.Reachability) -> list[tuple[str, str]]:
    """The connection and the position, as the page lists them."""
    age = relative_age(state.last_sync, utc_now_iso()) if state.last_sync else ""
    if not state.last_sync:
        last = "Never"
    elif age in ("", "just now"):
        last = f"Just now ({state.last_sync})"
    else:
        last = f"{age} ago ({state.last_sync})"
    rows = [
        ("Server", state.base_url),
        (SERVER_STATUS, _status_value(reach)),
        ("Last sync", last),
        ("Pulled up to", "Nothing yet" if state.cursor is None else str(state.cursor)),
        ("Waiting to send", f"{state.waiting} row(s)"),
    ]
    if state.set_aside:
        rows.append((SET_ASIDE, _set_aside_value(state.set_aside)))
    return rows


def _status_value(reach: reachability.Reachability) -> str:
    """What the registry said last time it was asked, and how long ago.

    Both halves are shown because either alone misleads: a verdict with no age
    reads as current when the laptop has been asleep, and an age with no verdict
    says nothing about what came back.
    """
    label = reachability.STATE_LABELS.get(reach.state, reach.state)
    if not reach.asked:
        return f"{label}. {reach.detail}"
    age = relative_age(reach.checked_at, utc_now_iso())
    when = "just now" if age in ("", "just now") else f"checked {age} ago"
    return f"{label}. {reach.detail} ({when})"


def _set_aside_value(names: Sequence[str], shown: int = 4) -> str:
    """The names, capped, and where they stand.

    Nearly always a name a record here already carries, but not always, and the
    row cannot tell which from the name alone, so it says only what is true of
    both: the registry holds these and this survey would not write them.

    Capped because the list survives every sync until somebody acts on it, and a
    registry disagreeing about a season's worth of lines would otherwise push
    everything under it off the page.
    """
    listed = ", ".join(names[:shown])
    if len(names) > shown:
        listed += f", and {len(names) - shown} more"
    return f"{listed} (held by the registry, not accepted here)"


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
    if report.paused:
        line += f" Paused with {len(report.remaining)} file(s) remaining. Resume when the cause is resolved."
    if report.cancelled:
        line += " Stopped on request; archive again to send the rest."
    if report.skipped:
        label, reason = report.skipped[0]
        line += f" {len(report.skipped)} item(s) were left out ({label}: {reason})."
    return line


def _heartbeat(client: object, store: SurveyStore | None) -> Mapping[str, Any] | None:
    """Best-effort self-report before the sync proper.

    Never fatal: the sync matters more than the courtesy, and a registry too
    old to know the route answers 404, which is also fine. The report names the
    preset this machine runs under, and the answer names the registry's
    assigned one, kept beside the sync cursor as this survey's default. An old
    registry answers with no body, which says nothing about an assignment and
    so clears nothing.
    """
    if client is None:
        return None
    try:
        # The store sits at the survey output root, which is the disk a run
        # fills, so its free space is the one worth reporting.
        report = heartbeat_report(store.path.parent if store is not None else None)
        identity = resolved_identity(store)
        if identity is not None:
            report["active_preset_name"], report["active_preset_version"] = identity
        answer = client.heartbeat(report)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.info("Heartbeat not delivered: %s", exc)
        return None
    if store is None or not isinstance(answer, Mapping) or "assigned_preset" not in answer:
        return answer if isinstance(answer, Mapping) else None
    try:
        remember_assignment(store, answer.get("assigned_preset"))
    except Exception:
        logger.info("Could not record the assigned preset", exc_info=True)
    return answer


def _sync_performance_history(client: object, heartbeat: Mapping[str, Any] | None) -> int:
    """Send the global journal, returning the count waiting on older registries."""
    from deepreefmap_gui.paths import run_timings_path
    from deepreefmap_gui.profiling.performance_journal import (
        acknowledge,
        import_legacy,
        pending,
    )
    from deepreefmap_gui.sync import credentials

    held = credentials.load()
    if held is None:
        raise RuntimeError("This installation is not connected to a registry")
    import_legacy(run_timings_path())
    first = pending(held.base_url, held.device_id, 100)
    if not heartbeat or heartbeat.get("performance_observations_version") != 1:
        return len(first)
    batch = first
    while batch:
        response = client.upload_performance_observations(batch)  # type: ignore[attr-defined]
        rejected = response.get("rejected") or []
        if rejected:
            detail = "; ".join(
                f"{item.get('id', '?')}: {item.get('reason', 'rejected')}" for item in rejected
            )
            from deepreefmap_gui.sync.client import RejectedError

            raise RejectedError(f"Performance history was rejected: {detail}")
        accepted = [str(value) for key in ("accepted", "already_present") for value in response.get(key, [])]
        if not accepted:
            raise RuntimeError("The registry acknowledged no performance observations")
        acknowledge(held.base_url, held.device_id, accepted)
        batch = pending(held.base_url, held.device_id, 100)
    return 0
