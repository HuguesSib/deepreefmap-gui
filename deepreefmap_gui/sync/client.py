"""HTTP client for the metadata registry: enrolment, pull and push.

stdlib urllib, as elsewhere in this package. Every call is one attempt: sync is
driven by the operator or by a timer, so a retry here would only stack requests.

Every request declares the contract range and the sections this build reads. The
registry stamps the version the exchange ran at, and the client adopts it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from deepreefmap_gui.sync.connect_code import ConnectCode
from deepreefmap_gui.sync.contract import (
    CONTRACT_RANGE,
    CONTRACT_VERSION,
    MIN_CONTRACT_VERSION,
    READ_SECTIONS,
)

logger = logging.getLogger(__name__)

# What this build declares on every request: the versions it reads, and the
# sections it will land off a pull. A push declares its sections by carrying them.
CONTRACT_HEADER = "Deepreefmap-Contract"
SECTIONS_HEADER = "Deepreefmap-Sections"

DEFAULT_TIMEOUT = 15.0
# What the probe asks: who the registry thinks this device is. The registry also
# serves an unauthenticated liveness route beside `/api`, but that one answers
# the same for a device whose token has been revoked, and the probe exists to
# tell those two apart.
PING_PATH = "/me"
# Shorter than a data call on purpose. The question is whether anything is
# there, and a probe that hangs for fifteen seconds has already answered it.
PING_TIMEOUT = 5.0
# Enrolment mints a token behind argon2, so it is slower than a data call.
ENROL_TIMEOUT = 30.0
# One PUT moves a whole archive part, 32 MiB at the server's default, and a
# field uplink can be slow enough that the client timeout is what would kill it.
UPLOAD_TIMEOUT = 600.0
PULL_LIMIT = 1000
MAX_PULL_LIMIT = 5000

Sections = Mapping[str, Sequence[Mapping[str, Any]]]


class SyncError(RuntimeError):
    """Any failed exchange with the registry."""


class ServerUnreachableError(SyncError):
    """No answer at all: offline, wrong address, or DNS failure."""


class DeviceRevokedError(SyncError):
    """The server rejected this device's token."""


class EnrolmentRejectedError(SyncError):
    """The connect code was refused: unknown, expired, or already spent."""


class AccessDeniedError(SyncError):
    """Authenticated, but not allowed to do this."""


class ConflictError(SyncError):
    """A pushed row references a parent the server does not have, or collides on a name."""


class NotFoundError(SyncError):
    """The registry has no such record."""


class RejectedError(SyncError):
    """The server refused the document as malformed."""


class ContractMismatchError(SyncError):
    """The server speaks a different contract version than this app."""


class ServerFaultError(SyncError):
    """The server failed on its own side.

    Carries the status, because 500 and 502 are different news: the first is a
    registry that ran and broke, the second a gateway with nothing behind it.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Enrolment:
    """What a spent connect code returns. The token is shown once and then stored."""

    device_id: str
    token: str = field(repr=False)
    # Who onboarded this installation. Audit only: the token's rights are the
    # server's fixed device capability set, not theirs.
    enrolled_by: str = ""
    # The durable name this installation goes by, chosen in the web console when
    # the connect code was minted. Empty from a registry old enough to echo none.
    device_name: str = ""


class SyncClient:
    """One registry, one credential.

    `base_url` is whatever the connect code carried, with or without its `/api`
    suffix, since an operator copying an address from a browser gets either.

    `agreed` is the contract version this registry has already stamped, None
    where it has never stamped one. It is a fact about the registry rather than
    about the client, so the caller loads it and stores it again afterwards.

    `server_max` is the top of the range the registry declared on its most
    recent answer, learned fresh on every response rather than persisted: a
    registry that has just been upgraded must not be pushed to under the older
    version a stored value would name.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        agreed: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agreed = agreed
        self.server_max: int | None = None
        self._token = token
        self._timeout = timeout

    def enrol(
        self,
        code: ConnectCode,
        device_name: str,
        platform: str | None = None,
        gui_version: str | None = None,
        library_version: str | None = None,
    ) -> Enrolment:
        """Trade a connect code for this device's long-lived token.

        The name in the body is only for registries old enough to still read
        it: a current one names the device from the connect code and answers
        with the name adopted.
        """
        body = {
            "code": code.secret,
            "device_name": device_name,
            "platform": platform,
            "gui_version": gui_version,
            "library_version": library_version,
        }
        payload = self._request(
            "POST",
            "/enrol",
            body=body,
            authorise=False,
            declare_sections=False,
            timeout=ENROL_TIMEOUT,
        )
        return Enrolment(
            device_id=str(payload.get("device_id", "")),
            token=str(payload.get("token", "")),
            enrolled_by=str(payload.get("enrolled_by") or ""),
            device_name=str(payload.get("device_name") or ""),
        )

    def pull(self, since: int | None = None, limit: int = PULL_LIMIT) -> dict[str, Any]:
        """One page of changed rows. Keep calling while `has_more` is true."""
        params: dict[str, str] = {"limit": str(min(max(limit, 1), MAX_PULL_LIMIT))}
        if since is not None:
            params["since"] = str(since)
        return self._request("GET", f"/sync/pull?{urllib.parse.urlencode(params)}")

    def push(self, sections: Sections) -> dict[str, Any]:
        """Apply a closed document. `contract_version` is stamped here, not by callers.

        The version declared is the negotiated one, `min` of the two maximums,
        which is what the registry checks the document against. Declaring this
        build's own maximum instead would be refused by every older registry.
        """
        document = {"contract_version": self._declared_version(), "sections": sections}
        return self._request("POST", "/sync/push", body=document)

    def _declared_version(self) -> int:
        """The version this exchange runs under: neither side's maximum alone.

        `server_max` comes from the header on the answer just received, so a
        registry upgraded since the last sync is pushed to under its new
        version rather than a stale agreement.
        """
        known = [CONTRACT_VERSION]
        if self.server_max is not None:
            known.append(self.server_max)
        elif self.agreed is not None:
            known.append(self.agreed)
        return min(known)

    def heartbeat(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """Report this device's software and static hardware, onto its own row.

        A courtesy, not a precondition: callers treat any failure here as
        non-fatal and go on to sync. The answer names the registry's assigned
        preset. Its body is unstamped like the archive responses, so it is not
        held to a contract version; the range header still travels and is
        checked on errors.
        """
        return self._request(
            "POST", "/sync/heartbeat", body=dict(report), verify_contract=False
        )

    def ping(self) -> dict[str, Any]:
        """Ask the registry who it thinks this device is. Raises like any other exchange.

        Authenticated, and that is the point: one request separates a registry
        that is down from one that is up and no longer accepts this
        installation, which look identical from a route that takes no token.
        The answer names the device, and its body carries no contract stamp, so
        it is not held to a version; the range header still travels and is
        checked on errors.
        """
        return self._request("GET", PING_PATH, verify_contract=False, timeout=PING_TIMEOUT)

    def archive_initiate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Begin or resume one content-addressed upload.

        `complete` back is the dedup answer: the content is archived already and
        nothing need travel. Archive responses are not stamped with a contract
        version, so callers build a client without `agreed` for them.
        """
        return self._request("POST", "/archive/initiate", body=dict(payload))

    def archive_upload_part(self, object_id: str, part_number: int, chunk: bytes) -> str:
        """PUT one part's bytes to the registry, returning the MD5 it answered.

        A raw octet-stream body under the device token, so this is the one
        authorised call that does not go through `_request`. The timeout is the
        upload's own: a part can take minutes on a field uplink.
        """
        if not self._token:
            raise DeviceRevokedError("This installation is not connected to a registry yet.")
        url = self._url(f"/archive/{object_id}/parts/{part_number}")
        headers = {
            "Accept": "application/json",
            CONTRACT_HEADER: CONTRACT_RANGE,
            "Content-Type": "application/octet-stream",
            "Authorization": f"Bearer {self._token}",
        }
        # S310: the URL comes from the pasted connect code, already checked to be
        # http or https by connect_code.decode_connect_code.
        request = urllib.request.Request(url, data=chunk, headers=headers, method="PUT")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:  # noqa: S310
                self._learn_range(response.headers.get(CONTRACT_HEADER))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, authorise=True) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            unreachable = f"Cannot reach {self.base_url}: {exc}. Check the network and try again."
            raise ServerUnreachableError(unreachable) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServerFaultError(f"{url} did not answer with JSON.") from exc
        etag = str(payload.get("etag") or "") if isinstance(payload, Mapping) else ""
        if not etag:
            raise SyncError("The registry answered a part without an ETag.")
        return etag

    def archive_complete(
        self, object_id: str, parts: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Assemble the uploaded parts into the finished object."""
        return self._request(
            "POST", f"/archive/{object_id}/complete", body={"parts": [dict(p) for p in parts]}
        )

    def archive_probe(self, hashes: Sequence[str]) -> dict[str, Any]:
        """The archived state of many contents at once, keyed by hash.

        A hash absent from the answer has never been offered, which is an
        answer rather than an error, unlike `archive_by_hash`'s 404.
        """
        return self._request("POST", "/archive/probe", body={"hashes": list(hashes)})

    def archive_runs_probe(self, run_ids: Sequence[str]) -> dict[str, Any]:
        """Per run, how many artefacts the registry holds and how they stand."""
        return self._request(
            "POST", "/archive/runs-probe", body={"run_ids": [str(r) for r in run_ids]}
        )

    def archive_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """The archived state of this content, or None where nothing is."""
        try:
            return self._request("GET", f"/archive/by-hash/{content_hash}")
        except NotFoundError:
            return None

    def archive_download(self, object_id: str) -> str:
        """A short-lived signed fetch link on the registry for a completed object.

        A registry that does not know its public address answers a relative
        path, joined here onto the address this client already talks to.
        """
        url = str(self._request("GET", f"/archive/{object_id}/download").get("url", ""))
        return self._url(url) if url.startswith("/") else url

    def _url(self, path: str) -> str:
        prefix = "" if self.base_url.endswith("/api") else "/api"
        return f"{self.base_url}{prefix}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        authorise: bool = True,
        declare_sections: bool = True,
        verify_contract: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        # Declared here rather than per call, so no route can be added without it.
        headers = {"Accept": "application/json", CONTRACT_HEADER: CONTRACT_RANGE}
        if declare_sections:
            headers[SECTIONS_HEADER] = ",".join(READ_SECTIONS)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if authorise:
            if not self._token:
                raise DeviceRevokedError("This installation is not connected to a registry yet.")
            headers["Authorization"] = f"Bearer {self._token}"
        # S310: the URL comes from the pasted connect code, already checked to be
        # http or https by connect_code.decode_connect_code.
        request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:  # noqa: S310
                self._learn_range(response.headers.get(CONTRACT_HEADER))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, authorise) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            unreachable = f"Cannot reach {self.base_url}: {exc}. Check the network and try again."
            raise ServerUnreachableError(unreachable) from exc
        # A 204 carries nothing to read, and nothing to verify: the contract
        # range still travels in the response header and is checked on errors.
        if not raw.strip():
            logger.info("%s %s ok", method, path.split("?", maxsplit=1)[0])
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServerFaultError(f"{url} did not answer with JSON.") from exc
        if not isinstance(payload, dict):
            raise ServerFaultError(f"{url} answered with {type(payload).__name__}, not an object.")
        if verify_contract:
            self._verify_contract(payload)
        logger.info("%s %s ok", method, path.split("?", maxsplit=1)[0])
        return payload

    def _learn_range(self, header: str | None) -> None:
        """Adopt the registry's declared maximum from any answer it gives."""
        bounds = _parse_range(header)
        if bounds is not None:
            self.server_max = bounds[1]

    def _verify_contract(self, payload: Mapping[str, Any]) -> None:
        """Check the version a response was served under, and adopt it.

        The stamp is what this exchange agreed on, `min` of the two maximums, so
        any version inside this build's own range is a version it can read, and
        the stamp moves up on its own when the registry's maximum does. Only a
        stamp outside the range is a refusal.

        Before anything has been agreed an unstamped response is tolerated,
        because contract 1 shipped before the registry stamped anything. After
        that, silence is a registry that has gone backwards, and tolerating it
        would leave a sync running under a version nobody named.
        """
        stamped = payload.get("contract_version")
        if stamped is None:
            if self.agreed is None:
                return
            raise ContractMismatchError(
                f"This app speaks metadata contract {self.agreed} and the registry did not "
                "say which it speaks. Update the registry before syncing."
            )
        if not MIN_CONTRACT_VERSION <= stamped <= CONTRACT_VERSION:
            raise ContractMismatchError(
                f"This app speaks metadata contract {CONTRACT_RANGE} and the registry served "
                f"this under {stamped}. Update whichever is older before syncing."
            )
        self.agreed = int(stamped)

    def _http_error(self, exc: urllib.error.HTTPError, authorise: bool) -> SyncError:
        # Before the body, and whatever the status: a proxy can rewrite a body and
        # cannot drop this, and a range with nothing in common explains the rest.
        self._learn_range(exc.headers.get(CONTRACT_HEADER))
        disjoint = _disjoint_range(exc.headers.get(CONTRACT_HEADER))
        if disjoint is not None:
            return disjoint
        detail = _error_detail(exc)
        status = exc.code
        if status == 401 and not authorise:
            return EnrolmentRejectedError(
                f"The connect code was not accepted: {detail}. Ask whoever runs the registry for a fresh code."
            )
        if status == 401:
            return DeviceRevokedError(
                f"This device's access has been revoked: {detail}. Connect it again with a new "
                "connect code from the registry's web interface."
            )
        if status == 403:
            return AccessDeniedError(f"The registry refused this request: {detail}")
        if status == 409:
            return ConflictError(f"The registry rejected the document: {detail}")
        if status == 400:
            return RejectedError(f"The registry rejected the document: {detail}")
        if status == 404:
            return NotFoundError(f"The registry has no such record: {detail}")
        if status >= 500:
            return ServerFaultError(f"The registry failed on its own side ({status}): {detail}", status)
        return SyncError(f"Unexpected response {status} from the registry: {detail}")


def enrol(
    code: ConnectCode,
    device_name: str,
    platform: str | None = None,
    gui_version: str | None = None,
    library_version: str | None = None,
) -> Enrolment:
    """Enrol against the address inside the code, so no address is configured here."""
    return SyncClient(code.base_url).enrol(
        code,
        device_name,
        platform=platform,
        gui_version=gui_version,
        library_version=library_version,
    )


def _disjoint_range(header: str | None) -> ContractMismatchError | None:
    """The registry's own range, when it shares no version with this build's.

    Every response carries it, errors included, so this is the one reading that
    survives a proxy rewriting the body. An absent or malformed value says
    nothing and is left to the status code.
    """
    bounds = _parse_range(header)
    if bounds is None:
        return None
    low, high = bounds
    if low <= CONTRACT_VERSION and high >= MIN_CONTRACT_VERSION:
        return None
    return ContractMismatchError(
        f"This app speaks metadata contract {CONTRACT_RANGE} and the registry speaks "
        f"{low}-{high}. Update whichever is older before syncing."
    )


def _parse_range(header: str | None) -> tuple[int, int] | None:
    """``min-max``, or a bare version as a range of one."""
    if not header:
        return None
    low, _, high = header.strip().partition("-")
    try:
        bounds = (int(low), int(high or low))
    except ValueError:
        logger.warning("Ignoring an unreadable contract header %r", header)
        return None
    return bounds if bounds[0] <= bounds[1] else None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's `error` string, or its status line where there is no JSON body."""
    try:
        document = json.load(exc)
    except Exception:
        return exc.reason if isinstance(exc.reason, str) else str(exc.code)
    if isinstance(document, dict) and isinstance(document.get("error"), str):
        return document["error"]
    return str(exc.code)
