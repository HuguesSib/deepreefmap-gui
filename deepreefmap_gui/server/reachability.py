"""Whether the registry is answering this device, asked apart from any sync.

A sync proves the link works, but only at the moment one runs, and runs are
minutes or hours apart. Between them the badge would go on saying "Synced" over
a laptop that walked out of wifi an hour ago, or one whose device has since been
revoked in the console. So the connection is asked about on its own.

The probe is a GET on the registry's identity route, carrying this installation's
own token. Authenticated on purpose, and under `/api` rather than the liveness
route beside it: `/healthz` is not published past the ingress, and it answers the
same for a device the registry has stopped accepting. One request separates a
server that is down from one that is up and refusing this laptop, and those two
need different people to fix them.

What it still cannot say is whether a sync will succeed: a rejected row and a row
waiting on a parent are the sync's news to break.

Qt-free, like `state.py`: the page and the badge only paint what is here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from deepreefmap_gui.survey.models.common import utc_now_iso
from deepreefmap_gui.sync import client, credentials

logger = logging.getLogger(__name__)

# Nothing has been asked yet, or there is no credential to ask with.
UNKNOWN = "unknown"
# Answered, and still knows this device.
ONLINE = "online"
# Answered, and will not have this device: revoked in the console, or a token
# that is no longer good. Nothing a retry fixes.
DENIED = "denied"
# Something at that address answered, but not with an identity: a registry that
# is running and unwell, one older than this route, or one whose contract this
# app cannot speak.
DEGRADED = "degraded"
# Nothing answered at all: no network, no DNS, or nothing listening.
OFFLINE = "offline"

# What a reverse proxy answers when the registry behind it is not running or
# not responding. The app is reached through one, so this is what "the server is
# off" looks like far more often than a refused connection is.
GATEWAY_STATUSES = frozenset({502, 503, 504})

# How stale an answer may be before it is asked again. The status bar refreshes
# every 15s and a probe is a request to somebody's server, so the badge repaints
# from a cached answer far more often than it earns a new one.
PROBE_INTERVAL_S = 60.0

# What the Server page prints beside "Server status". The round trip is reported
# because it is the one number that says how usable the link is: a registry
# answering in four seconds will not carry an archive today.
ANSWERED = "Answered in {ms} ms and knows this device."
REFUSED = "Answered in {ms} ms and would not have this device. {detail}"
MISMATCH = "Answered in {ms} ms under a metadata contract this app cannot speak. {detail}"
NO_ROUTE = "Answered in {ms} ms, but has no identity route. {detail}"
UNWELL = "Answered in {ms} ms, but not with an identity. {detail}"
NO_ANSWER = "{detail}"
NOTHING_BEHIND = "A gateway answered in {ms} ms with nothing serving behind it. {detail}"
NEVER_ASKED = "Not checked yet."
NOT_ENROLLED = "This laptop is not connected to a registry."

STATE_LABELS = {
    UNKNOWN: "Unknown",
    ONLINE: "Online",
    DENIED: "Refusing this device",
    DEGRADED: "Answering, but unwell",
    OFFLINE: "Unavailable",
}


@dataclass(frozen=True)
class Reachability:
    """One answer from the identity route, and when it was given."""

    state: str = UNKNOWN
    detail: str = NEVER_ASKED
    # Round trip of the probe itself, absent where nothing came back.
    latency_ms: int | None = None
    checked_at: str = ""
    # Beside `checked_at`, which is wall-clock and is for the reader. Staleness
    # is measured on the monotonic clock so a laptop waking with a corrected
    # system time does not decide its last probe happened in the future.
    _at: float = 0.0

    @property
    def asked(self) -> bool:
        return self.state != UNKNOWN

    @property
    def answered(self) -> bool:
        """Whether anything at all is at that address."""
        return self.state in (ONLINE, DENIED, DEGRADED)

    @property
    def unavailable(self) -> bool:
        return self.state == OFFLINE

    @property
    def denied(self) -> bool:
        """Whether the registry answered and refused this installation."""
        return self.state == DENIED

    def stale(self, now: float, interval: float = PROBE_INTERVAL_S) -> bool:
        """Whether this answer has aged out, measured on the monotonic clock."""
        return not self.asked or now - self._at >= interval


# Nothing asked yet: the state a window starts in, and what a page paints before
# the first probe lands.
UNCHECKED = Reachability()


def probe() -> Reachability:
    """Ask whether the registry is up and still knows this device. Never raises.

    Every outcome is a state rather than an exception because the caller is a
    status bar: there is nothing here that should stop a refresh, and the words
    a failure carries are what the Server page shows. The credential is loaded
    here rather than handed in, so the token stays in this layer.
    """
    try:
        held = credentials.load()
    except credentials.CredentialsError as exc:
        return _reading(UNKNOWN, NO_ANSWER, None, time.monotonic(), detail=str(exc))
    if held is None:
        return Reachability(detail=NOT_ENROLLED)

    started = time.monotonic()
    registry = client.SyncClient(held.base_url, token=held.token, timeout=client.PING_TIMEOUT)
    try:
        registry.ping()
    except client.ServerUnreachableError as exc:
        logger.info("Registry did not answer: %s", exc)
        return _reading(OFFLINE, NO_ANSWER, None, started, detail=str(exc))
    except (client.DeviceRevokedError, client.AccessDeniedError) as exc:
        logger.info("Registry refused this device: %s", exc)
        return _reading(DENIED, REFUSED, _elapsed_ms(started), started, detail=str(exc))
    except client.ContractMismatchError as exc:
        return _reading(DEGRADED, MISMATCH, _elapsed_ms(started), started, detail=str(exc))
    except client.ServerFaultError as exc:
        # 502, 503 and 504 are a proxy reporting that the registry itself is not
        # up. Something answered, but nothing that could ever serve a sync, so
        # this is the server being down rather than the server being unwell.
        if exc.status in GATEWAY_STATUSES:
            logger.info("Nothing serving behind the registry's address: %s", exc)
            return _reading(OFFLINE, NOTHING_BEHIND, _elapsed_ms(started), started, detail=str(exc))
        logger.info("Registry failed on its own side: %s", exc)
        return _reading(DEGRADED, UNWELL, _elapsed_ms(started), started, detail=str(exc))
    except client.NotFoundError as exc:
        # It is up: something served a 404, which a dead address cannot do. Only
        # this route is missing, which is a registry older than the probe.
        return _reading(DEGRADED, NO_ROUTE, _elapsed_ms(started), started, detail=str(exc))
    except Exception as exc:
        logger.info("Registry answered the probe with a fault: %s", exc)
        return _reading(DEGRADED, UNWELL, _elapsed_ms(started), started, detail=str(exc))
    return _reading(ONLINE, ANSWERED, _elapsed_ms(started), started)


def _reading(state: str, template: str, latency: int | None, started: float, detail: str = "") -> Reachability:
    return Reachability(
        state=state,
        detail=template.format(ms=latency, detail=detail),
        latency_ms=latency,
        checked_at=utc_now_iso(),
        _at=started,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
