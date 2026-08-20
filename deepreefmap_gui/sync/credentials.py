"""Where this installation keeps its device token, its server address and its identity.

The token goes to the OS keyring where one answers without asking for a password,
and to a 0600 file where none does. Which one holds it is recorded beside the device
id, so a read goes to one place rather than probing both.

Nothing here is touched until the Server page is opened, so a laptop that never syncs
never meets a credential store at all.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "deepreefmap-gui"
KEYRING_USERNAME = "device_token"

BACKEND_KEYRING = "keyring"
BACKEND_FILE = "file"

# A locked collection answers with a password dialog rather than an error, so a
# call that has not returned by now is abandoned.
KEYRING_TIMEOUT_S = 2.0

class CredentialsError(RuntimeError):
    """The stored credentials cannot be read or written safely."""


@dataclass(frozen=True)
class Credentials:
    """This device's registry credentials, as one thing the client can be built from."""

    base_url: str
    token: str = field(repr=False)
    device_id: str = ""


def device_path() -> Path:
    """Device identity and server address, overridable for tests."""
    override = os.environ.get("DEEPREEFMAP_SYNC_DEVICE")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "sync_device.json"


def token_path() -> Path:
    """Where the device token is kept, overridable for tests."""
    override = os.environ.get("DEEPREEFMAP_SYNC_TOKEN")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "sync_token.json"


def device_id() -> str:
    """This installation's device UUID, minted once and then permanent.

    A fallback for credential files written before the registry's own id was
    stored at enrolment: the registry keys the device row on the id it minted,
    so `save` records that one and this is never consulted again.
    """
    document = _read_device()
    existing = document.get("device_id")
    if isinstance(existing, str) and existing:
        return existing
    minted = str(uuid.uuid4())
    document["device_id"] = minted
    _write_device(document)
    return minted


# The function, before `save`'s parameter shadows the name.
_local_device_id = device_id


def save(base_url: str, token: str, device_id: str = "") -> str:
    """Persist the credentials from an enrolment, returning the store used.

    `device_id` is the registry's own id for this device, from the enrolment
    response. It is the id the registry's device list shows, so an admin can
    match this installation to a row there; a locally minted one cannot be.
    """
    document = _read_device()
    document["device_id"] = device_id or _local_device_id()
    document["base_url"] = base_url
    document["token_backend"] = _store_token(token)
    _write_device(document)
    return str(document["token_backend"])


def token_backend() -> str:
    """Which store holds this device's token."""
    recorded = _read_device().get("token_backend")
    return recorded if recorded in (BACKEND_KEYRING, BACKEND_FILE) else BACKEND_FILE


def load() -> Credentials | None:
    """The stored credentials, or None when this installation is not connected."""
    document = _read_device()
    base_url = document.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    token = _read_token(token_backend())
    if token is None:
        return None
    identity = document.get("device_id")
    return Credentials(
        base_url=base_url,
        token=token,
        device_id=identity if isinstance(identity, str) else "",
    )


def connected() -> bool:
    return load() is not None


def forget() -> None:
    """Drop the local credentials. Revocation itself happens server-side, in the web interface."""
    keyring = _keyring()
    if keyring is not None:
        _attempt(lambda: keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME))
    _forget_token_file()
    document = _read_device()
    document.pop("base_url", None)
    document.pop("token_backend", None)
    _write_device(document)


def _read_device() -> dict[str, Any]:
    path = device_path()
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"Cannot read {path}: {exc}") from exc
    return document if isinstance(document, dict) else {}


def _write_device(document: dict[str, Any]) -> None:
    """Replace the identity file atomically.

    This file is the device's identity: corrupt it and the laptop re-enrols as
    a new device, orphaning everything the old one uploaded. A crash or a full
    disk mid-write must leave the old file, never half of a new one.
    """
    path = device_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.tmp")
    scratch.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def _store_token(token: str) -> str:
    """Write the token to the keyring where one answers, else to a 0600 file."""
    keyring = _keyring()
    if keyring is not None:
        stored, _ = _attempt(lambda: keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token))
        if stored:
            _forget_token_file()
            return BACKEND_KEYRING
        logger.info("The keyring did not take the device token; using a private file")
    _write_token_file(token)
    return BACKEND_FILE


def _keyring() -> Any:
    """The `keyring` module where it answers without a password prompt, else None."""
    if sys.platform == "darwin":
        # Unsigned bundle: the Keychain treats each build as a new application.
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        logger.info("Keyring unavailable: %s", exc)
        return None
    # keyring.backends.fail is how the library says no service is running.
    if type(backend).__module__.startswith("keyring.backends.fail"):
        return None
    return keyring


def _attempt(call: Callable[[], Any]) -> tuple[bool, Any]:
    """Run a keyring call on a daemon thread, giving up rather than waiting on a prompt."""
    outcome: list[Any] = []
    def run() -> None:
        try:
            outcome.append(call())
        except Exception as exc:
            logger.info("Keyring call failed: %s", exc)

    worker = threading.Thread(target=run, daemon=True, name="keyring")
    worker.start()
    worker.join(KEYRING_TIMEOUT_S)
    if not outcome:
        logger.warning("The keyring did not answer within %ss", KEYRING_TIMEOUT_S)
        return False, None
    return True, outcome[0]


def _read_token(backend: str) -> str | None:
    if backend == BACKEND_KEYRING:
        keyring = _keyring()
        if keyring is None:
            raise CredentialsError(
                "This device's token is in the operating system keyring, which is not "
                "answering. Unlock it and reopen this page, or connect the device again."
            )
        read, token = _attempt(lambda: keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME))
        if not read:
            raise CredentialsError(
                "The operating system keyring did not answer. Unlock it and reopen this "
                "page, or connect the device again."
            )
        return str(token) if token else None
    path = token_path()
    if not path.exists():
        return None
    _require_private(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"Cannot read {path}: {exc}") from exc
    token = document.get("token") if isinstance(document, dict) else None
    return str(token) if token else None


def _write_token_file(token: str) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a private scratch file and renamed over the old one, so a
    # crash mid-write cannot truncate the only copy of the token. Created
    # empty at 0600 first, so the token is never in a readable file.
    scratch = path.with_suffix(".json.tmp")
    descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"token": token}, handle)
    os.chmod(scratch, 0o600)
    os.replace(scratch, path)
    _require_private(path)


def _forget_token_file() -> None:
    token_path().unlink(missing_ok=True)


def _require_private(path: Path) -> None:
    """Refuse a token file anyone else on the machine can read."""
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CredentialsError(
            f"{path} is readable by other users (mode {mode:o}). Run `chmod 600 {path}` or reconnect this device."
        )
