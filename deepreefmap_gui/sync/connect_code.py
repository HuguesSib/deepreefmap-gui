"""Decode the one string that onboards this installation onto a registry.

A connect code is `<prefix><base64url of {"url": …, "code": …}>`, the prefix coming
from the vendored contract. The server address travels inside it, so this repository
ships no address of its own.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from deepreefmap_gui.sync import contract

CODE_PREFIX = contract.CONNECT_CODE_PREFIX
CODE_VERSION = contract.CONNECT_CODE_VERSION
SECRET_HEX_LEN = 64

INSECURE_TRANSPORT_WARNING = (
    "This code points at a plain http address, so the token and your metadata "
    "would travel unencrypted. Ask for an https address: only this machine's "
    "own loopback may use plain http."
)

_SECRET = re.compile(f"^[0-9a-f]{{{SECRET_HEX_LEN}}}$")
_FAMILY = re.compile(rf"^{re.escape(contract.CONNECT_CODE_FAMILY)}(\d+)\.")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

# The shape, for a dialog to show beside the box a code is pasted into.
CODE_SCHEMA = f"{CODE_PREFIX}<the code from the registry's web interface>"


class ConnectCodeError(ValueError):
    """A pasted string that is not a usable connect code."""


class ConnectCodeVersionError(ConnectCodeError):
    """A code in this family, from a version of the registry this build predates."""


@dataclass(frozen=True)
class ConnectCode:
    """A decoded connect code: where to enrol, and the single-use secret to enrol with."""

    base_url: str
    secret: str = field(repr=False)
    insecure_transport: bool = False

    @property
    def warning(self) -> str | None:
        """Operator-facing caveat, or None when the code is unremarkable."""
        return INSECURE_TRANSPORT_WARNING if self.insecure_transport else None


def decode_connect_code(pasted: str) -> ConnectCode:
    """Decode a pasted connect code, or raise `ConnectCodeError` naming the fault."""
    text = pasted.strip()
    if not text.startswith(CODE_PREFIX):
        _refuse_prefix(text)
    encoded = text[len(CODE_PREFIX) :]
    if not encoded:
        raise ConnectCodeError("The connect code is empty after its prefix.")
    try:
        # validate=True so a stray character is reported as bad base64 rather than
        # silently dropped and blamed on the JSON.
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConnectCodeError("The connect code is not valid base64url, so the copy was incomplete.") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectCodeError("The connect code does not contain JSON, so it is not a code from this system.") from exc
    if not isinstance(payload, dict):
        raise ConnectCodeError("The connect code should contain a JSON object with `url` and `code`.")
    for key in ("url", "code"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ConnectCodeError(f"The connect code is missing its `{key}`.")
    secret = payload["code"]
    if not _SECRET.match(secret):
        raise ConnectCodeError(f"The connect code's secret is malformed: expected {SECRET_HEX_LEN} hex characters.")
    base_url, insecure = _server_url(payload["url"])
    return ConnectCode(base_url=base_url, secret=secret, insecure_transport=insecure)


def _refuse_prefix(text: str) -> None:
    """Raise for a string that is not this build's connect code, naming which fault."""
    seen = _FAMILY.match(text)
    if seen and int(seen.group(1)) > CODE_VERSION:
        raise ConnectCodeVersionError(
            "This code was made by a newer version of the registry. Update this app."
        )
    raise ConnectCodeError(
        f"A connect code starts with `{CODE_PREFIX}`. Paste the whole string from the "
        "registry's web interface."
    )


def _server_url(value: str) -> tuple[str, bool]:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ConnectCodeError(f"The connect code's address must be http or https, not `{parts.scheme or value}`.")
    if not parts.hostname:
        raise ConnectCodeError("The connect code's address has no host.")
    if parts.username or parts.password:
        raise ConnectCodeError("The connect code's address embeds a username or password, which this app will not use.")
    insecure = parts.scheme == "http" and parts.hostname not in _LOOPBACK
    return value.rstrip("/"), insecure
