"""Pull, push, and the conflict reports both produce.

Pull runs before push. It narrows the window in which this laptop is working from
a stale copy, and it means a row two people edited is discovered before the local
edit is offered rather than after it was refused.

Both halves are resumable. The pull cursor is written after each page that landed,
and a section's push watermark moves only once the registry has accounted for
every row that section sent, so an interrupted sync repeats work rather than
losing it.

No Qt here, deliberately: the surface runs this on a worker thread and marshals
the reports back through signals.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepreefmap.config.classes import ClassConfig

from deepreefmap_gui.survey.analysis import collate_long_format
from deepreefmap_gui.survey.models.notification import SURVEY, WARNING
from deepreefmap_gui.survey.store import SYNC_SECTIONS, ApplyResult, SurveyStore
from deepreefmap_gui.sync import contract, wire
from deepreefmap_gui.sync.client import PULL_LIMIT

logger = logging.getLogger(__name__)

# The sections this device authors, the only ones a watermark or a pending count
# means anything for. The rest travel as ancestors of what changed: the registry
# reads them and refuses to write them, so no answer ever advances them.
AUTHORED_SECTIONS: tuple[str, ...] = tuple(
    name for name in SYNC_SECTIONS if name in contract.PUSH_SECTIONS
)

# Machine-local sync position. The cursor is the registry's own monotonic
# server_seq; a watermark is the newest updated_at that section has had accepted.
CURSOR_KEY = "sync.cursor"
WATERMARK_PREFIX = "sync.push_watermark."

# The pull sections this device and the registry last agreed on, sorted and comma
# separated. The cursor is only meaningful against a set: widening it means rows
# the cursor has already stepped over.
CONTRACT_SECTIONS_KEY = "sync.contract.sections"

# The contract version this registry has stamped on a response. Written once one
# has, and read back so an unstamped response afterwards is refused rather than
# tolerated. Beside the cursor because it is one registry's answer, not a fact
# about this machine.
CONTRACT_VERSION_KEY = "sync.contract.version"

# Rows that arrived without the parent they need, carried across syncs. The
# cursor is a high-water mark over one registry-wide sequence, so a row passed
# over is passed over for good and holding it here is the only way back to it.
HELD_KEY = "sync.held"
# Completed pulls a held row is retried over before it is given up on.
HELD_ATTEMPTS = 10
# Held rows kept per section, oldest dropped first, so a registry sending
# nothing but orphans cannot grow the blob without bound.
HELD_MAX = 1000

# A local edit the registry refused, and a local edit its copy replaced. Two
# fingerprints because they are two outcomes: one edit did not land and the other
# was overwritten by somebody else's.
CONFLICT_DISCARDED = "sync.local_edit_discarded"
# The same conflict on a section this device only ever sends. No pull reconciles
# it, so it is worth saying differently rather than promising a download.
CONFLICT_STRANDED = "sync.local_edit_stranded"
CONFLICT_OVERWRITTEN = "sync.local_edit_overwritten"
# A local edit to a row another device owns, which the registry will never take.
CONFLICT_REFUSED = "sync.local_edit_refused"
# A pass the registry holds with no videos, which this side cannot represent.
PASS_WITHOUT_VIDEOS = "sync.pass_without_videos"
# A run whose pass the registry never sent, so it has no parent to hang off.
RUN_WITHOUT_PASS = "sync.run_without_pass"
# A section this build cannot read, which stopped the pull where it stood.
SECTION_NOT_UNDERSTOOD = "sync.section_not_understood"
# A section the registry sent that this device never asked to receive.
SECTION_REFUSED = "sync.section_refused"
# A run whose pass the registry has tombstoned, so it has no section to belong to.
RUN_PASS_DELETED = "sync.run_pass_deleted"
# Rows waited on long enough that they are no longer being waited on.
HELD_GIVEN_UP = "sync.held_given_up"
# A row that arrived in a shape no model here can be built from.
UNREADABLE_ROW = "sync.unreadable_row"
# The registry said more rows were waiting but would not advance the cursor.
PULL_STALLED = "sync.pull_stalled"
# A push response that did not account for every row a section sent.
PUSH_UNACCOUNTED = "sync.push_unaccounted"


class SyncTransport(Protocol):
    """The two calls the engine makes on a registry client.

    Enrolment belongs to the client and to whatever drives onboarding, not to a
    sync run.
    """

    def pull(self, since: int | None = ..., limit: int = ...) -> Mapping[str, Any]: ...

    def push(self, sections: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any]: ...


class ConflictSink(Protocol):
    """Where a conflict report goes.

    `NotificationCenter` is one. A surface running this on a worker thread cannot
    touch the centre directly, so it passes a bridge that emits instead.
    """

    def post(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = ...,
        severity: str = ...,
        scope: str = ...,
    ) -> Any: ...


@dataclass(frozen=True)
class SectionPush:
    """What the registry did with one section of a document."""

    received: int
    applied: int
    # Narrowed to rows edited here since the last accepted push: see _our_edits.
    skipped: tuple[uuid.UUID, ...] = ()
    # Rows the registry will never write from here: another origin's, or a whole
    # section this device does not author. Narrowed like skipped.
    refused: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class PushReport:
    """One push: what the registry took, what it already had newer, where we are."""

    sections: dict[str, SectionPush] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)
    cursor: int | None = None
    # Sections whose watermark was held because the response did not account
    # for every row they sent. Their rows are re-offered on every sync until
    # the registry answers for them.
    unaccounted: tuple[str, ...] = ()

    @property
    def sent(self) -> int:
        return sum(section.received for section in self.sections.values())

    @property
    def applied(self) -> int:
        return sum(section.applied for section in self.sections.values())

    @property
    def skipped(self) -> list[uuid.UUID]:
        """Rows the registry already held a newer copy of: information, not an error."""
        return [row_id for section in self.sections.values() for row_id in section.skipped]

    def skipped_by_direction(self) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Skipped rows split by whether a later pull can bring the winner down.

        Only a section this device also pulls reconciles on its own. For one it
        merely authors, the registry's newer copy stays where it is and the two
        records simply differ, which is a different thing to tell the operator.
        """
        downloadable: list[uuid.UUID] = []
        stranded: list[uuid.UUID] = []
        for name, section in self.sections.items():
            bucket = downloadable if name in contract.PULL_SECTIONS else stranded
            bucket.extend(section.skipped)
        return downloadable, stranded

    @property
    def refused(self) -> list[uuid.UUID]:
        """Locally edited rows the registry will never take, because another device owns them."""
        return [row_id for section in self.sections.values() for row_id in section.refused]


@dataclass(frozen=True)
class PullReport:
    """One pull: what landed, and every place the two copies disagreed."""

    pages: int = 0
    cursor: int | None = None
    sections: dict[str, ApplyResult] = field(default_factory=dict)
    # Rows this device had edited since its last push, whose registry copy won.
    overwritten: tuple[uuid.UUID, ...] = ()
    # Rows the registry sent that lost to the copy held here.
    kept: tuple[uuid.UUID, ...] = ()
    # Passes the registry holds with no live videos. See _apply_page.
    passes_without_videos: tuple[uuid.UUID, ...] = ()
    # Runs whose pass never arrived, so they had nothing to hang off. See _hold_run.
    runs_without_passes: tuple[uuid.UUID, ...] = ()
    # Sections this build cannot read, which is where the pull stopped.
    unknown_sections: tuple[str, ...] = ()
    # Sections the registry held back because this build never asked for them.
    # Nothing was lost and nothing stopped: the rows are simply not for this
    # version. Upload-only sections are absent by design and are not in here.
    omitted_sections: tuple[str, ...] = ()
    # Sections the registry sent that this device never asked to receive. They
    # were ignored whole: what this device authors is only ever written here.
    refused_sections: tuple[str, ...] = ()
    # Runs whose pass the registry has deleted. See _apply_page.
    runs_pass_deleted: tuple[uuid.UUID, ...] = ()
    # Rows held long enough to be given up on, or pushed out by HELD_MAX.
    given_up: tuple[uuid.UUID, ...] = ()
    # The registry said more rows were waiting but would not advance the
    # cursor, so the pull broke off rather than asking for the same page forever.
    stalled: bool = False

    @property
    def applied(self) -> int:
        return sum(result.applied for result in self.sections.values())

    @property
    def unreadable(self) -> list[tuple[str, str]]:
        """Rows no model could be built from, across every section of every page."""
        return [named for result in self.sections.values() for named in result.unreadable]

    @property
    def stopped(self) -> bool:
        """Whether the pull ended before the registry had run out of rows."""
        return bool(self.unknown_sections) or self.stalled


@dataclass
class _PageOutcome:
    """One page's tally, on its way into the pull's."""

    landed: dict[str, ApplyResult] = field(default_factory=dict)
    overwritten: list[uuid.UUID] = field(default_factory=list)
    kept: list[uuid.UUID] = field(default_factory=list)
    runs_pass_deleted: list[uuid.UUID] = field(default_factory=list)
    unknown: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncReport:
    pull: PullReport
    push: PushReport


class SyncEngine:
    """Bidirectional metadata sync for one survey database.

    ``out_root`` is where run directories live, since a run's provenance and its
    cover are read from its manifest and its benthic_cover.json rather than from
    the database. ``classes_config`` is what rolls raw class counts up into the
    groups a cover row names; without one, cover is left out of the document and
    everything else still syncs. ``pull_sections`` is the section set the
    handshake agreed on, which decides whether the cursor still means anything.
    """

    def __init__(
        self,
        store: SurveyStore,
        client: SyncTransport,
        out_root: Path | None = None,
        classes_config: ClassConfig | None = None,
        notifications: ConflictSink | None = None,
        limit: int = PULL_LIMIT,
        pull_sections: Sequence[str] | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._out_root = out_root if out_root is not None else store.path.parent
        self._classes_config = classes_config
        self._notify = notifications
        self._limit = limit
        self._pull_sections = None if pull_sections is None else tuple(pull_sections)

    # --- Position ---

    def cursor(self) -> int | None:
        """The last registry position this device has pulled up to."""
        stored = self._store.sync_state(CURSOR_KEY)
        if stored is None:
            return None
        try:
            return int(stored)
        except ValueError:
            logger.warning("Ignoring unreadable pull cursor %r", stored)
            return None

    def watermark(self, section: str) -> str | None:
        """The newest ``updated_at`` this section has had accepted, or None."""
        return self._store.sync_state(f"{WATERMARK_PREFIX}{section}")

    def stored_sections(self) -> tuple[str, ...]:
        """The pull section set the stored cursor was reached under."""
        raw = self._store.sync_state(CONTRACT_SECTIONS_KEY) or ""
        return tuple(sorted(name for name in (part.strip() for part in raw.split(",")) if name))

    def agreed_sections(self) -> tuple[str, ...]:
        """The section set this pull will be served under.

        Defaults to the stored set, so a build with nothing negotiating on its
        behalf never resets the cursor.
        """
        if self._pull_sections is None:
            return self.stored_sections()
        return tuple(sorted(set(self._pull_sections)))

    def _reset_cursor_if_widened(self) -> None:
        """Pull from zero when the agreed sections now cover more than the cursor did.

        The cursor is a high-water mark over one sequence shared by every table, so
        rows in a section this device did not ask for have been stepped over
        permanently. A re-pull is last-write-wins over what is already here, which
        costs bandwidth and nothing else.
        """
        agreed = self.agreed_sections()
        stored = self.stored_sections()
        if not set(agreed) > set(stored):
            return
        logger.info(
            "Pulling from zero: the contract now covers %s",
            ", ".join(sorted(set(agreed) - set(stored))),
        )
        self._store.set_sync_state(CURSOR_KEY, None)
        self._store.set_sync_state(CONTRACT_SECTIONS_KEY, ",".join(agreed))

    # --- Sync ---

    def sync(self) -> SyncReport:
        """Pull, then push. See the module docstring for why that order."""
        pulled = self.pull()
        return SyncReport(pull=pulled, push=self.push())

    # --- Pull ---

    def pull(self) -> PullReport:
        """Every page the registry has for us, landing each before asking for more.

        The cursor is written after a page lands, so an interrupted pull resumes
        at the page it failed on. Re-landing a page is harmless: last-write-wins
        makes an equal stamp a skip.

        A page naming a section this build cannot read is landed as far as it can
        be and then ends the pull with the cursor where it was, so the rows behind
        that section are still there to be asked for once the app can read them.
        """
        self._reset_cursor_if_widened()
        cursor = self.cursor()
        pages = 0
        sections: dict[str, ApplyResult] = {}
        overwritten: list[uuid.UUID] = []
        kept: list[uuid.UUID] = []
        unknown: tuple[str, ...] = ()
        omitted: set[str] = set()
        refused: set[str] = set()
        deleted_passes: list[uuid.UUID] = []
        stalled = False
        held = self._held()
        # True only where the registry said it had nothing left, which is the one
        # outcome that tells a held row nothing is coming for it.
        exhausted = False
        while True:
            page = self._client.pull(since=cursor, limit=self._limit)
            omitted.update(_omitted_sections(page))
            advanced = _page_cursor(page)
            moved = advanced is not None and (cursor is None or advanced > cursor)
            # One transaction per page: the sections, the held rows and the
            # cursor land together or not at all, so a failure mid-page leaves
            # the survey exactly as the previous page left it.
            with self._store.transaction():
                outcome = self._apply_page(page, held)
                for name, result in outcome.landed.items():
                    sections[name] = _merge(sections.get(name), result)
                self._write_held(held)
                # Never past a section this build cannot read: the rows behind
                # it are still there to be asked for once the app can read them.
                if moved and not outcome.unknown:
                    cursor = advanced
                    self._store.set_sync_state(CURSOR_KEY, str(cursor))
            overwritten.extend(outcome.overwritten)
            kept.extend(outcome.kept)
            deleted_passes.extend(outcome.runs_pass_deleted)
            refused.update(outcome.refused)
            pages += 1
            if outcome.unknown:
                unknown = outcome.unknown
                logger.warning("Stopping the pull at cursor %s: %s", cursor, ", ".join(unknown))
                break
            if not moved and page.get("has_more"):
                logger.warning("Registry reported more rows at cursor %s and did not advance", cursor)
                stalled = True
                break
            if not page.get("has_more"):
                exhausted = True
                break
        given_up = _age_held(held) if exhausted else []
        given_up.extend(_trim_held(held))
        self._write_held(held)
        report = PullReport(
            pages=pages,
            cursor=cursor,
            sections=sections,
            overwritten=tuple(overwritten),
            kept=tuple(kept),
            passes_without_videos=_held_ids(held["passes"]),
            runs_without_passes=_held_ids(held["runs"]),
            unknown_sections=unknown,
            omitted_sections=tuple(sorted(omitted)),
            refused_sections=tuple(sorted(refused)),
            runs_pass_deleted=tuple(deleted_passes),
            given_up=tuple(given_up),
            stalled=stalled,
        )
        self._report_pull_conflicts(report)
        return report

    def _held(self) -> dict[str, dict[str, Any]]:
        """Rows still waiting for a parent, as the last sync left them.

        A pass whose chapters have not arrived is the common one, since its
        pass_video rows sort after it and so often come a page later; its runs wait
        with it or they would break the foreign key. Chapters wait too, for the
        opposite order: a pass edited after its chapters sorts behind them.
        """
        raw = self._store.sync_state(HELD_KEY)
        if raw is None:
            return _no_held()
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable held rows")
            return _no_held()
        return _no_held() if not isinstance(blob, Mapping) else _read_held(blob)

    def _write_held(self, held: Mapping[str, dict[str, Any]]) -> None:
        value = json.dumps(held, sort_keys=True) if any(held.values()) else None
        self._store.set_sync_state(HELD_KEY, value)

    def _asked_for(self, sections: Mapping[str, Any]) -> dict[str, Any]:
        """The page narrowed to the sections this pull declared it reads.

        An engine with no agreed set falls back to the vendored contract's pull
        sections. Either way there is always a gate: a section this device
        authors is only ever written here, whatever a registry chooses to send.
        """
        agreed = self.agreed_sections() or contract.PULL_SECTIONS
        return {name: rows for name, rows in sections.items() if name in agreed}

    def _apply_page(
        self,
        page: Mapping[str, Any],
        held: dict[str, dict[str, Any]],
    ) -> _PageOutcome:
        """Land one page, in foreign-key order, and say where the copies disagreed.

        Only the sections this pull asked for are read. The rest are refused
        whole and reported, never landed.
        """
        offered = page.get("sections") or {}
        raw = self._asked_for(offered)
        outcome = _PageOutcome(
            unknown=wire.unknown_sections(raw),
            refused=tuple(sorted(set(offered) - set(raw))),
        )
        incoming = {
            name: wire.rows_from_wire(raw[name]) for name in SYNC_SECTIONS if raw.get(name)
        }
        # Cover lives in the run directories, so a pulled cover row has nowhere to
        # land. It is the registry's own aggregate of what this device produced.
        chapters = _chapters_by_pass(held, wire.rows_from_wire(raw.get(wire.PASS_VIDEOS) or []))
        folded, unattached = wire.fold_pass_videos(
            _with_held(held["passes"], incoming.get("passes", ())),
            [row for rows in chapters.values() for row in rows],
        )
        attempts = _attempts(held["passes"])
        held["passes"].clear()
        ready = []
        deleted: set[str] = set()
        for row in folded:
            key = str(row["id"])
            if "video_id" in row or self._store.holds_id("passes", uuid.UUID(key)):
                ready.append(row)
            elif row.get("deleted_at"):
                # A deleted pass has no chapters left to wait for and nothing here
                # to remove, so waiting on it would be waiting forever.
                deleted.add(key)
                logger.info("Dropping deleted section %s, which this device never held", key)
            else:
                held["passes"][key] = {"row": row, "attempts": attempts.get(key, 0)}
        if ready or "passes" in incoming:
            incoming["passes"] = ready
        runs = _with_held(held["runs"], incoming.get("runs", ()))
        attempts = _attempts(held["runs"])
        held["runs"].clear()
        if runs:
            landing = {str(row["id"]) for row in incoming.get("passes", ())}
            incoming["runs"] = [
                row
                for row in runs
                if self._run_lands(row, landing, deleted, held["runs"], attempts, outcome)
            ]

        for name in SYNC_SECTIONS:
            rows = incoming.get(name)
            if not rows:
                continue
            arriving = {uuid.UUID(str(row["id"])) for row in rows}
            pending = self._unpushed_ids(name) & arriving
            result = self._store.apply_from_server(name, rows)
            outcome.landed[name] = result
            outcome.kept.extend(result.skipped)
            outcome.overwritten.extend(sorted(pending - set(result.skipped), key=str))
        held["pass_videos"] = self._adopt_chapter_order(unattached, chapters)
        return outcome

    def _run_lands(
        self,
        row: Mapping[str, Any],
        landing: set[str],
        deleted: set[str],
        waiting: dict[str, Any],
        attempts: Mapping[str, int],
        outcome: _PageOutcome,
    ) -> bool:
        """Whether a run can go in now, waiting or dropping it when it cannot.

        A run whose pass this device neither holds nor is about to insert would fail
        the foreign key and take the whole pull down with it, so it waits for a
        later page instead. Runs are the only section this can happen to: every
        other parent is a row the registry sends whole. A run whose pass arrived
        deleted has nowhere to go at all, and the registry does not delete a
        section's results with it, so this is a routine outcome and not a fault.
        """
        pass_id = str(row.get("pass_id"))
        if pass_id in deleted:
            outcome.runs_pass_deleted.append(uuid.UUID(str(row["id"])))
            return False
        if pass_id in landing or self._store.holds_id("passes", uuid.UUID(pass_id)):
            return True
        key = str(row["id"])
        waiting[key] = {"row": dict(row), "attempts": attempts.get(key, 0)}
        return False

    def _adopt_chapter_order(
        self,
        unattached: Mapping[str, list[uuid.UUID]],
        chapters: Mapping[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Apply chapter lists whose pass this device already holds, keep the rest.

        Written whole rather than as a partial row, because the chapters are one
        relationship on this side and two rows on the registry's, so there is no
        incoming stamp that describes only them. The pass keeps its own stamp:
        an adopted order is the registry's data, not a local edit to re-push.

        A list for a pass nobody here has ever seen is returned to be held: its
        pass sorts behind it and comes on a later page.
        """
        waiting: dict[str, list[dict[str, Any]]] = {}
        for pass_key, video_ids in unattached.items():
            pass_ = self._store.get_pass(uuid.UUID(pass_key))
            if pass_ is None and not self._store.holds_id("passes", uuid.UUID(pass_key)):
                waiting[pass_key] = chapters.get(pass_key, [])
                continue
            if pass_ is None or not video_ids or pass_.video_ids() == video_ids:
                continue
            pass_.video_id = video_ids[0]
            pass_.extra_video_ids = list(video_ids[1:])
            self._store.set_pass_chapters(pass_)
        return waiting

    def _unpushed_ids(self, section: str) -> set[uuid.UUID]:
        """Ids edited here since this section was last accepted.

        Strictly after the watermark, not at it: the watermark is the stamp the
        registry accepted, so a row carrying it is the row that was accepted.
        Stamps are second-precision, so an edit made in that same second cannot be
        told from the push, and calling it a conflict would report one on every
        sync of an unedited survey.

        A section this device does not author has nothing unpushed, ever: its rows
        arrived on a pull, and counting them would owe the registry its own data.
        """
        if section not in AUTHORED_SECTIONS:
            return set()
        watermark = self.watermark(section)
        return {
            row.id
            for row in self._store.changed_since(section, watermark)
            if watermark is None or row.updated_at > watermark
        }

    # --- Push ---

    def push(self) -> PushReport:
        """Send everything edited here since the last accepted push.

        The document is a closed set: the registry refuses a child whose parent it
        has never seen, so every row travels with its ancestors whether or not they
        changed. A watermark moves only once the registry has accounted for the
        whole section, so a refused document leaves this device exactly where it
        was.
        """
        sections, watermarks = self.build_document()
        if not sections:
            return PushReport()
        edited = {name: self._unpushed_ids(name) for name in AUTHORED_SECTIONS}
        response = self._client.push(sections)
        outcomes = _push_outcomes(response)
        advanced: dict[str, str] = {}
        unaccounted: list[str] = []
        for name, stamp in watermarks.items():
            outcome = outcomes.get(name)
            if outcome is None or not _accounted(outcome, len(sections[name])):
                logger.warning("Holding the %s watermark: the registry did not account for it", name)
                unaccounted.append(name)
                continue
            self._store.set_sync_state(f"{WATERMARK_PREFIX}{name}", stamp)
            advanced[name] = stamp
        report = PushReport(
            sections=_our_edits(outcomes, edited),
            watermarks=advanced,
            # The registry's cursor after our own write, reported and never stored:
            # adopting it as the pull cursor would skip every row another device
            # wrote before it.
            cursor=_page_cursor(response),
            unaccounted=tuple(unaccounted),
        )
        self._report_push_conflicts(report)
        return report

    def build_document(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """The push sections in foreign-key order, and the watermark each would earn.

        A watermark is the newest stamp among the rows that actually changed, not
        among the ancestors dragged in with them: an unchanged parent travelling as
        a companion must not move a section forward. Only authored sections earn
        one at all: an ancestor section is refused whole on every push, so a
        watermark there would either never advance or mean nothing.
        """
        changed: dict[str, list[Any]] = {}
        watermarks: dict[str, str] = {}
        for name in AUTHORED_SECTIONS:
            rows = self._store.changed_since(name, self.watermark(name))
            if rows:
                changed[name] = rows
                watermarks[name] = max(row.updated_at for row in rows)
        closure = self._closure(changed)
        sections: dict[str, list[dict[str, Any]]] = {}
        for name in SYNC_SECTIONS:
            models = closure.get(name)
            if not models:
                continue
            sections[name] = (
                wire.run_rows_to_wire(models, self._out_root)
                if name == "runs"
                else wire.rows_to_wire(name, models)
            )
            if name == "passes":
                sections[wire.PASS_VIDEOS] = [
                    row for model in models for row in wire.pass_video_rows(model)
                ]
        cover = self._cover_rows(closure)
        if cover:
            sections[wire.COVER_ROWS] = cover
        ordered = {name: sections[name] for name in wire.WIRE_SECTIONS if sections.get(name)}
        return ordered, watermarks

    def _closure(self, changed: Mapping[str, Sequence[Any]]) -> dict[str, list[Any]]:
        """Every changed row plus every ancestor it needs, deduplicated per section."""
        found: dict[str, dict[uuid.UUID, Any]] = {}
        for name, rows in changed.items():
            ancestry = self._store.dependency_closure(name, [row.id for row in rows])
            for section, models in ancestry.items():
                found.setdefault(section, {}).update({model.id: model for model in models})
        return {
            name: sorted(models.values(), key=lambda model: (model.created_at, str(model.id)))
            for name, models in found.items()
        }

    def _cover_rows(self, closure: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
        """Per-pass cover for the runs in this document, read from their run directories."""
        runs = closure.get("runs") or []
        transects = closure.get("transects") or []
        if self._classes_config is None or not runs or not transects:
            return []
        rows = collate_long_format(
            self._store,
            self._out_root,
            self._classes_config,
            transect_ids=[transect.id for transect in transects],
        )
        return wire.cover_rows_to_wire(rows, {str(run.id): run for run in runs}, self._out_root)

    # --- Conflicts ---

    def _report_pull_conflicts(self, report: PullReport) -> None:
        # First, because it is why anything after it is still waiting.
        if report.unknown_sections:
            named = ", ".join(report.unknown_sections)
            self._post(
                SECTION_NOT_UNDERSTOOD,
                "The registry sent data this version cannot read",
                f"It sent a section called {named}. Everything that came before it "
                "was kept, and the sync stopped there rather than passing over it, "
                "so nothing is lost. Update this app to take the rest.",
            )
        if report.stalled:
            self._post(
                PULL_STALLED,
                "The registry stopped answering the pull",
                "It said more rows were waiting but kept answering with the same "
                "page, so the sync broke off rather than asking forever. "
                "Everything that arrived was kept. Try again later; a registry "
                "doing this repeatedly is misbehaving.",
            )
        if report.refused_sections:
            named = ", ".join(report.refused_sections)
            self._post(
                SECTION_REFUSED,
                "The registry sent data this device never asked for",
                f"It sent {named}, which records made on this laptop are the only "
                "authority for. Those rows were ignored whole and nothing here "
                "changed. A registry doing this repeatedly is misconfigured.",
            )
        if report.overwritten:
            self._post(
                CONFLICT_OVERWRITTEN,
                f"The registry replaced {len(report.overwritten)} edited row(s)",
                "Somebody edited these rows more recently, so their version won. "
                "Whatever was typed here since the last sync is no longer in the survey.",
            )
        if report.passes_without_videos:
            self._post(
                PASS_WITHOUT_VIDEOS,
                f"{len(report.passes_without_videos)} section(s) arrived with no footage",
                "The registry holds these sections with no clips listed against them, "
                "and a section without footage cannot be recorded here. They are kept "
                "aside and tried again on every sync, so nothing needs doing.",
            )
        if report.runs_without_passes:
            self._post(
                RUN_WITHOUT_PASS,
                f"{len(report.runs_without_passes)} result(s) arrived with no section",
                "The registry has not sent the sections these results belong to yet. "
                "They are kept aside and tried again on every sync, so nothing needs "
                "doing.",
            )
        if report.runs_pass_deleted:
            self._post(
                RUN_PASS_DELETED,
                f"{len(report.runs_pass_deleted)} result(s) belong to a deleted section",
                "The sections these results were processed from have been removed "
                "from the registry. Nothing on this device changed. Ask whoever "
                "deleted them whether the results should have gone too.",
            )
        if report.given_up:
            logger.warning(
                "Giving up on held rows: %s", ", ".join(str(row_id) for row_id in report.given_up)
            )
            self._post(
                HELD_GIVEN_UP,
                f"{len(report.given_up)} row(s) have been waiting since the last "
                f"{HELD_ATTEMPTS} syncs",
                "The registry has not sent what they belong to, so they are no "
                "longer being tried for. Their ids are in the log.",
            )
        if report.unreadable:
            for named, why in report.unreadable:
                logger.warning("Skipping row %s: %s", named, why)
            self._post(
                UNREADABLE_ROW,
                f"{len(report.unreadable)} row(s) arrived in a shape this app cannot read",
                "They were left out and everything else on the page was kept. Their "
                "ids are in the log. A corrected row comes down on its own, so this "
                "clears itself once the registry holds one.",
            )

    def _report_push_conflicts(self, report: PushReport) -> None:
        if report.unaccounted:
            named = ", ".join(report.unaccounted)
            self._post(
                PUSH_UNACCOUNTED,
                "The registry did not account for everything sent",
                f"Its answer did not say what happened to every row in {named}, "
                "so those rows are treated as unsent and go again on the next "
                "sync. Nothing is lost, but until the registry answers for them "
                "this repeats; a registry doing it every sync is misbehaving.",
            )
        downloadable, stranded = report.skipped_by_direction()
        if downloadable:
            self._post(
                CONFLICT_DISCARDED,
                f"The registry already held {len(downloadable)} row(s) newer than ours",
                "Those edits were not accepted. The next sync brings the registry's "
                "version down, which is what the survey will show.",
            )
        if stranded:
            self._post(
                CONFLICT_STRANDED,
                f"The registry already held {len(stranded)} row(s) newer than ours",
                "Those edits were not accepted, and this app does not download "
                "these rows, so the two records now differ. Make the change in "
                "the web console if it should stand.",
            )
        refused = report.refused
        if refused:
            self._post(
                CONFLICT_REFUSED,
                f"The registry refused {len(refused)} row(s) recorded by another device",
                "These rows were first uploaded from somewhere else, so edits to "
                "them made here never land. Make the change in the web console, "
                "or on the device that recorded them.",
            )

    def _post(self, fingerprint: str, title: str, body: str) -> None:
        logger.info("%s: %s", fingerprint, title)
        if self._notify is not None:
            self._notify.post(
                fingerprint=fingerprint, title=title, body=body, severity=WARNING, scope=SURVEY
            )


def _held_ids(entries: Mapping[str, Any]) -> tuple[uuid.UUID, ...]:
    return tuple(uuid.UUID(key) for key in entries)


def _no_held() -> dict[str, dict[str, Any]]:
    return {"passes": {}, "runs": {}, "pass_videos": {}}


def _read_held(blob: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The held blob as the engine works with it, ignoring anything malformed."""
    held = _no_held()
    for name in ("passes", "runs"):
        entries = blob.get(name)
        if not isinstance(entries, Mapping):
            continue
        for key, entry in entries.items():
            if isinstance(entry, Mapping) and isinstance(entry.get("row"), Mapping):
                held[name][key] = {"row": dict(entry["row"]), "attempts": _count(entry)}
    chapters = blob.get(wire.PASS_VIDEOS)
    if isinstance(chapters, Mapping):
        held[wire.PASS_VIDEOS] = {
            key: list(rows) for key, rows in chapters.items() if isinstance(rows, list)
        }
    return held


def _count(entry: Mapping[str, Any]) -> int:
    try:
        return int(entry.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _attempts(entries: Mapping[str, Any]) -> dict[str, int]:
    return {key: entry["attempts"] for key, entry in entries.items()}


def _with_held(
    entries: Mapping[str, Any], arriving: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Held rows and this page's, one per id, the page's copy winning."""
    rows = {key: entry["row"] for key, entry in entries.items()}
    rows.update({str(row["id"]): dict(row) for row in arriving})
    return list(rows.values())


def _chapters_by_pass(
    held: Mapping[str, dict[str, Any]], arriving: Iterable[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Held chapter rows and this page's, grouped by pass, one per id.

    The page's copy wins, so a chapter taken off a pass replaces the live row
    being held for it.
    """
    rows: dict[str, dict[str, Any]] = {
        str(row["id"]): dict(row)
        for chapters in held[wire.PASS_VIDEOS].values()
        for row in chapters
    }
    rows.update({str(row["id"]): dict(row) for row in arriving})
    by_pass: dict[str, list[dict[str, Any]]] = {}
    for row in rows.values():
        by_pass.setdefault(str(row["pass_id"]), []).append(row)
    return by_pass


def _age_held(held: dict[str, dict[str, Any]]) -> list[uuid.UUID]:
    """Count one completed pull against every held row, and give up at the limit.

    Only a pull that reached the end counts: until then the row it is waiting for
    may still be in a page nobody has asked for yet.
    """
    given_up = []
    for name in ("passes", "runs"):
        for key, entry in list(held[name].items()):
            entry["attempts"] += 1
            if entry["attempts"] >= HELD_ATTEMPTS:
                del held[name][key]
                given_up.append(uuid.UUID(key))
    return given_up


def _trim_held(held: dict[str, dict[str, Any]]) -> list[uuid.UUID]:
    """Drop the oldest of each section down to HELD_MAX.

    A registry sending nothing but orphans would otherwise grow this blob until
    the database is the problem instead.
    """
    dropped = []
    for entries in held.values():
        for key in list(entries)[: max(len(entries) - HELD_MAX, 0)]:
            del entries[key]
            dropped.append(uuid.UUID(key))
    return dropped


def _merge(existing: ApplyResult | None, result: ApplyResult) -> ApplyResult:
    """One section's tally across the pages of one pull."""
    if existing is None:
        return result
    return ApplyResult(
        received=existing.received + result.received,
        inserted=existing.inserted + result.inserted,
        updated=existing.updated + result.updated,
        skipped=[*existing.skipped, *result.skipped],
        unreadable=[*existing.unreadable, *result.unreadable],
    )


def _omitted_sections(page: Mapping[str, Any]) -> list[str]:
    """What the registry says it withheld, ignoring anything that is not a name."""
    listed = page.get("omitted_sections")
    if not isinstance(listed, (list, tuple)):
        return []
    return [str(name) for name in listed if name]


def _page_cursor(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("cursor")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _push_outcomes(response: Mapping[str, Any]) -> dict[str, SectionPush]:
    outcomes = {}
    for name, outcome in (response.get("sections") or {}).items():
        outcomes[name] = SectionPush(
            received=int(outcome.get("received", 0)),
            applied=int(outcome.get("applied", 0)),
            skipped=tuple(_ids(outcome.get("skipped") or ())),
            refused=tuple(_ids(outcome.get("refused") or ())),
        )
    return outcomes


def _our_edits(
    outcomes: Mapping[str, SectionPush], edited: Mapping[str, set[uuid.UUID]]
) -> dict[str, SectionPush]:
    """The same outcomes with every skip that was not a local edit dropped.

    A document carries the ancestors of what changed and re-offers the row sitting
    exactly on each watermark, so a registry in step with this device skips most of
    what it is sent, and refuses whole sections it read as reference. Reporting
    either as a conflict would warn about a survey where nothing is wrong. The
    derived sections are not in ``edited`` at all: their ids are a function of the
    rows they hang off, so an equal stamp there is never news.
    """
    return {
        name: SectionPush(
            received=outcome.received,
            applied=outcome.applied,
            skipped=tuple(
                row_id for row_id in outcome.skipped if row_id in edited.get(name, ())
            ),
            refused=tuple(
                row_id for row_id in outcome.refused if row_id in edited.get(name, ())
            ),
        )
        for name, outcome in outcomes.items()
    }


def _accounted(outcome: SectionPush, sent: int) -> bool:
    """Whether the registry said what became of every row this section sent.

    A skipped row counts as accounted for: the registry has told us it holds a
    newer copy, and the pull that follows brings it down. So does a refused row:
    it belongs to another origin and the answer is terminal, so retrying cannot
    change it. Holding the watermark back for either would re-offer the same row
    on every sync forever.
    """
    settled = outcome.applied + len(outcome.skipped) + len(outcome.refused)
    return outcome.received == sent and settled == sent


def _ids(values: Iterable[Any]) -> list[uuid.UUID]:
    parsed = []
    for value in values:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError:
            logger.warning("Registry reported a skipped row with an unreadable id %r", value)
    return parsed
