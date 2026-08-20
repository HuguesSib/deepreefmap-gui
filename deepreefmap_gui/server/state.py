"""What the Server page reads, and how it words what went wrong.

Two facts make a connection: a credential, which is per machine, and a position,
which is per survey database. So the address and the device id come from
`sync/credentials.py`, while the pull cursor, the push watermarks and
the time of the last sync come from the database under the output root. Switching
output root switches the position and keeps the credential.

Qt-free: the page only paints what is here.
"""

from __future__ import annotations

import logging
import platform
import socket
from dataclasses import dataclass, field

from deepreefmap_gui.survey.store import SurveyStore
from deepreefmap_gui.sync import client, credentials
from deepreefmap_gui.sync.connect_code import ConnectCodeError
from deepreefmap_gui.sync.credentials import CredentialsError
from deepreefmap_gui.sync.engine import (
    AUTHORED_SECTIONS,
    CONTRACT_VERSION_KEY,
    CURSOR_KEY,
    WATERMARK_PREFIX,
    PullReport,
    PushReport,
)

logger = logging.getLogger(__name__)

# The section key the page is registered under, and where a sync conflict sends
# a reader who presses the notification.
SERVER_SECTION = "server"

# Beside the cursor, in the survey database: it dates that database's position,
# not the machine's.
LAST_SYNC_KEY = "sync.last_sync_at"

# What the last sync said when it failed, cleared by the next success. The
# status-bar badge repaints from disk on a timer, so a failure that is not
# written here vanishes on its next repaint and the badge claims all is well.
SYNC_ERROR_KEY = "sync.last_error"

# In QSettings rather than the survey, because what this laptop calls itself is
# about the laptop. A colleague opening the same output root has their own.
DEVICE_NAME_KEY = "sync_device_name"

# Who onboarded this installation, as the registry reported it at enrolment.
ENROLLED_BY_KEY = "sync_enrolled_by"

# What each section is called in this app's words: the registry says video_asset
# and transect_pass, the interface says clip and section.
SECTION_LABELS = {
    "sites": "Sites",
    "campaigns": "Campaigns",
    "transects": "Transects",
    "videos": "Clips",
    "passes": "Sections",
    "runs": "Runs",
    "presets": "Presets",
}

NOTHING_TO_SYNC = "Nothing to sync: the registry already has everything from here."

# Said when the registry held sections back because this build never asked for
# them. Their names would mean nothing to a diver, so it counts them instead.
OMITTED_SECTIONS = (
    "The registry keeps {kinds} kind(s) of record this version of the app cannot read. "
    "Everything else synced. Update the app when you are next at a desk."
)

# Said after any failure that leaves work undone. True of all of them: a push is
# one transaction, and both halves resume from where they stopped.
RETRY_LATER = "Nothing was lost. The next sync sends whatever this one did not."


@dataclass(frozen=True)
class ServerState:
    """The connection and the position, as one thing the page can paint."""

    connected: bool = False
    base_url: str = ""
    device_id: str = ""
    # What uploads from this installation are attributed to.
    device_name: str = ""
    # Audit only, and empty unless the registry reported it.
    enrolled_by: str = ""
    cursor: int | None = None
    last_sync: str | None = None
    # Per section, and only what is actually waiting: a section with nothing to
    # send is absent rather than zero.
    pending: dict[str, int] = field(default_factory=dict)
    # Why the credential could not be read, when it could not be.
    fault: str = ""
    # What the last sync said when it failed, until one succeeds. A revoked
    # token is still a readable credential, so `connected` alone would paint
    # a healthy badge over a connection the registry has refused.
    sync_fault: str = ""
    # Whether a survey database was open to be read. Without one the pending
    # counts are empty because nothing was counted, not because nothing waits.
    has_survey: bool = False

    @property
    def waiting(self) -> int:
        return sum(self.pending.values())


@dataclass(frozen=True)
class Failure:
    """A failed exchange, in the words the page shows."""

    title: str
    detail: str
    # True when only a fresh connect code fixes it, so the page offers one.
    reconnect: bool = False


def read_state(
    store: SurveyStore | None, device_name: str = "", enrolled_by: str = ""
) -> ServerState:
    """The whole Server page in one read. Never raises: a fault is a field."""
    try:
        held = credentials.load()
    except CredentialsError as exc:
        return ServerState(device_name=device_name, fault=str(exc))
    if held is None:
        return ServerState(device_name=device_name)
    return ServerState(
        connected=True,
        base_url=held.base_url,
        device_id=held.device_id,
        device_name=device_name,
        enrolled_by=enrolled_by,
        cursor=read_cursor(store),
        last_sync=store.sync_state(LAST_SYNC_KEY) if store is not None else None,
        pending=pending_rows(store) if store is not None else {},
        sync_fault=(store.sync_state(SYNC_ERROR_KEY) or "") if store is not None else "",
        has_survey=store is not None,
    )


def read_cursor(store: SurveyStore | None) -> int | None:
    if store is None:
        return None
    stored = store.sync_state(CURSOR_KEY)
    if stored is None:
        return None
    try:
        return int(stored)
    except ValueError:
        logger.warning("Ignoring unreadable pull cursor %r", stored)
        return None


def read_agreed_contract(store: SurveyStore | None) -> int | None:
    """The contract version this registry has stamped, or None until one has.

    Carried across sessions so the tolerance for an unstamped response ends for
    good once a registry has answered with one.
    """
    stored = store.sync_state(CONTRACT_VERSION_KEY) if store is not None else None
    if stored is None:
        return None
    try:
        return int(stored)
    except ValueError:
        logger.warning("Ignoring an unreadable agreed contract version %r", stored)
        return None


def remember_agreed_contract(store: SurveyStore | None, version: int | None) -> None:
    """Keep what the registry stamped, so the next session starts knowing it."""
    if store is None or version is None:
        return
    store.set_sync_state(CONTRACT_VERSION_KEY, str(version))


def pending_rows(store: SurveyStore) -> dict[str, int]:
    """Rows edited here since each section was last accepted, per section.

    Strictly after the watermark, matching the engine: the watermark is the stamp
    the registry accepted, so a row carrying it is the row that was accepted.
    Counting it as waiting would leave every synced survey owing something.
    Authored sections only: a pulled site is the registry's data, not a debt.
    Counted in SQL rather than loaded, because the status-bar badge reads this
    on a timer.
    """
    counts: dict[str, int] = {}
    for section in AUTHORED_SECTIONS:
        watermark = store.sync_state(f"{WATERMARK_PREFIX}{section}")
        waiting = store.count_changed_since(section, watermark)
        if waiting:
            counts[section] = waiting
    return counts


def summarise(pull: PullReport, push: PushReport) -> str:
    """One line for the page: what came down, what went up, what was refused.

    Sections the registry withheld are said here rather than as a blocker. The
    sync did everything it could, and a blocker reads as work that did not land.
    """
    if not pull.applied and not push.sent and not pull.stopped and not pull.omitted_sections:
        return NOTHING_TO_SYNC
    line = f"Pulled {pull.applied} row(s), sent {push.applied} row(s)."
    skipped = len(push.skipped)
    if skipped:
        line += f" The registry already held {skipped} of ours newer."
    if pull.overwritten:
        line += f" {len(pull.overwritten)} edit(s) made here were replaced."
    if pull.stopped:
        line += " The pull stopped early, so the registry still has more waiting."
    if pull.omitted_sections:
        line += f" {OMITTED_SECTIONS.format(kinds=len(pull.omitted_sections))}"
    return line


def describe_failure(exc: BaseException) -> Failure:
    """Say what went wrong in a sentence somebody can act on.

    Three outcomes need different actions and so read differently: an
    unreachable registry is a retry, a revoked device needs a new connect code,
    and a contract mismatch needs one side of the software updated. The client's
    own messages already name the address and the two contract versions, so they
    are shown rather than rewritten.
    """
    if isinstance(exc, ConnectCodeError):
        return Failure("That is not a usable connect code", str(exc))
    if isinstance(exc, CredentialsError):
        return Failure("The device credentials could not be stored", str(exc))
    if isinstance(exc, client.ServerUnreachableError):
        return Failure("The registry did not answer", f"{exc} {RETRY_LATER}")
    if isinstance(exc, client.EnrolmentRejectedError):
        return Failure("The connect code was refused", str(exc), reconnect=True)
    if isinstance(exc, client.DeviceRevokedError):
        return Failure("This device is no longer enrolled", str(exc), reconnect=True)
    if isinstance(exc, client.ContractMismatchError):
        return Failure("This app and the registry disagree on the metadata contract", str(exc))
    if isinstance(exc, client.AccessDeniedError):
        return Failure("The registry refused this device", str(exc))
    if isinstance(exc, (client.ConflictError, client.RejectedError)):
        return Failure("The registry would not take this document", f"{exc} {RETRY_LATER}")
    if isinstance(exc, client.ServerFaultError):
        return Failure("The registry failed on its own side", f"{exc} {RETRY_LATER}")
    return Failure("The sync did not finish", f"{exc} {RETRY_LATER}")


def default_device_name() -> str:
    """This machine's own name, which is what the operator would have typed."""
    host = socket.gethostname().split(".")[0].strip()
    return host or f"{platform.system()} laptop"


def platform_name() -> str:
    """What the registry records as this device's platform."""
    return f"{platform.system()} {platform.machine()}".strip()


def library_version() -> str:
    """The reconstruction library this device runs, for the device row."""
    from deepreefmap import __version__

    return __version__


def heartbeat_report() -> dict[str, object]:
    """What a device says about itself: software, and static hardware only.

    Free space, available RAM and the disk path stay off the wire. They are an
    activity trace of one person's laptop, and a disk path can embed a username.
    """
    from deepreefmap_gui.packaging.releases import current_version
    from deepreefmap_gui.profiling.system_probe import probe_system

    profile = probe_system(wait_for_gpu=False).to_dict()
    gpu = profile.get("gpu") or {}
    return {
        "gui_version": current_version(),
        "library_version": library_version(),
        "platform": platform_name(),
        "system_profile": {
            "os_name": profile.get("os_name"),
            "os_release": profile.get("os_release"),
            "cpu_logical": profile.get("cpu_logical"),
            "cpu_physical": profile.get("cpu_physical"),
            "total_ram_bytes": profile.get("total_ram_bytes"),
            "total_swap_bytes": profile.get("total_swap_bytes"),
            "disk_total_bytes": profile.get("disk_total_bytes"),
            "gpu": {
                "kind": gpu.get("kind"),
                "name": gpu.get("name"),
                "total_vram_bytes": gpu.get("total_vram_bytes"),
            },
        },
    }
