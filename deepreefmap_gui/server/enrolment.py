"""Onboard this installation, and forget it again.

`connect` is the whole flow behind the Connect button: decode the pasted code,
enrol against the address inside it, and write the token to whichever credential
store this machine has. It talks to the network, so it runs on a worker thread and
never on the GUI one.

Qt-free, and it never logs the pasted code: the code is a credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from deepreefmap_gui.packaging.releases import current_version
from deepreefmap_gui.server.state import (
    LAST_SYNC_KEY,
    SYNC_ERROR_KEY,
    default_device_name,
    library_version,
    platform_name,
)
from deepreefmap_gui.survey.store import SYNC_SECTIONS, SurveyStore
from deepreefmap_gui.sync import client, credentials
from deepreefmap_gui.sync.connect_code import ConnectCodeError, decode_connect_code
from deepreefmap_gui.sync.engine import (
    CONTRACT_SECTIONS_KEY,
    CONTRACT_VERSION_KEY,
    CURSOR_KEY,
    HELD_KEY,
    WATERMARK_PREFIX,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Connected:
    """A spent connect code, as the page reports it.

    The address is in here because the app never had one configured: until this
    ran, nothing on this machine knew which registry it was about to join.
    """

    base_url: str
    device_id: str
    device_name: str
    # The human who minted the code, as the registry reports them. Shown as
    # audit, never as attribution.
    enrolled_by: str = ""


def connect(pasted: str) -> Connected:
    """Trade a pasted connect code for a stored device credential.

    The device enrols under the machine's own name, sent here and nowhere else.
    Naming and renaming are web interface actions, so attribution is never
    mutable from the device it names.
    """
    code = decode_connect_code(pasted)
    # The dialog already refuses this with its button, but the secret and the
    # token cross the network here, so the refusal cannot live only in the UI.
    if code.insecure_transport:
        raise ConnectCodeError(code.warning or "")
    name = default_device_name()
    enrolment = client.enrol(
        code,
        name,
        platform=platform_name(),
        gui_version=current_version(),
        library_version=library_version(),
    )
    if not enrolment.token:
        raise client.SyncError(
            "The registry accepted the code but returned no token, so nothing was stored."
        )
    backend = credentials.save(code.base_url, enrolment.token, device_id=enrolment.device_id)
    logger.info("Device token stored in the %s", backend)
    logger.info("Enrolled with %s as device %s", code.base_url, enrolment.device_id)
    return Connected(
        base_url=code.base_url,
        device_id=enrolment.device_id,
        device_name=name,
        enrolled_by=enrolment.enrolled_by,
    )


def forget(store: SurveyStore | None) -> None:
    """Drop the token and this survey's sync position.

    The position goes with it. A cursor is one registry's own sequence, so
    keeping it across a reconnection would silently skip every row the next
    registry wrote before that number, and the watermarks would hold back rows
    it has never seen. What was negotiated goes too: the agreed version and the
    section set describe the registry being left, and the next one has said
    nothing yet. So do the held rows: they are orphans of the old registry's
    pages, and the next registry re-sends whatever they were waiting for.
    Revocation itself happens server-side, in the web interface: this only
    forgets locally.
    """
    credentials.forget()
    if store is None:
        return
    for key in (
        CURSOR_KEY,
        LAST_SYNC_KEY,
        SYNC_ERROR_KEY,
        CONTRACT_VERSION_KEY,
        CONTRACT_SECTIONS_KEY,
        HELD_KEY,
    ):
        store.set_sync_state(key, None)
    for section in SYNC_SECTIONS:
        store.set_sync_state(f"{WATERMARK_PREFIX}{section}", None)
