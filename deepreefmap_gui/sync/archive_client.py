"""Pooled, cancellable transport for archive requests."""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import urllib.error
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from deepreefmap_gui.sync.client import CONTRACT_HEADER, CONTRACT_RANGE, SyncClient, SyncError


class ArchiveCancelledError(SyncError):
    """The archive session stopped before its next request."""


class ArchiveError(SyncError):
    """An archive failure with its retry classification."""

    def __init__(self, message: str, *, status: int = 0, code: str = "", retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code not in {"archive_integrity", "archive_storage_incompatible"} and (
            self.status in {0, 408, 409, 429} or self.status >= 500
        )


class ArchiveClient(SyncClient):
    """One pooled archive session, shared by at most four transfer workers."""

    def __init__(self, base_url: str, token: str):
        super().__init__(base_url, token)
        self.cancel_event = threading.Event()
        self.on_sent: Callable[[str, int, int], None] | None = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(600, connect=10, pool=10),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={"Authorization": f"Bearer {token}", CONTRACT_HEADER: CONTRACT_RANGE},
        )

    def close(self) -> None:
        self._http.close()

    def cancel(self) -> None:
        self.cancel_event.set()
        self._http.close()

    def _archive_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self.cancel_event.is_set():
            raise ArchiveCancelledError("Archive cancelled")
        try:
            response = self._http.request(method, self._url(path), **kwargs)
        except RuntimeError as exc:
            if self.cancel_event.is_set():
                raise ArchiveCancelledError("Archive cancelled") from exc
            raise
        except httpx.HTTPError as exc:
            raise ArchiveError(f"Archive connection failed: {exc}") from exc
        self._learn_range(response.headers.get(CONTRACT_HEADER))
        if response.is_error:
            self._raise_archive_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ArchiveError("The registry returned invalid JSON", status=502) from exc
        if not isinstance(payload, dict):
            raise ArchiveError("The registry returned an invalid archive response", status=502)
        return payload

    def _raise_archive_error(self, response: httpx.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        wrapped = urllib.error.HTTPError(
            str(response.url), response.status_code, "", response.headers, io.BytesIO(response.content)
        )
        described = self._http_error(wrapped, authorise=True)
        try:
            delay = min(60.0, max(0.0, float(response.headers.get("retry-after", "0"))))
        except ValueError:
            try:
                when = parsedate_to_datetime(response.headers.get("retry-after", ""))
                delay = min(60.0, max(0.0, (when - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                delay = 0
        raise ArchiveError(str(described), status=response.status_code, code=code, retry_after=delay)

    def archive_initiate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._archive_request("POST", "/archive/initiate", json=dict(payload))

    def archive_complete(self, object_id: str, parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return self._archive_request(
            "POST",
            f"/archive/{object_id}/complete",
            json={"parts": list(parts)},
            timeout=httpx.Timeout(630, connect=10, pool=10),
        )

    def _chunks(self, object_id: str, number: int, chunk: bytes) -> Iterator[bytes]:
        for start in range(0, len(chunk), 64 * 1024):
            if self.cancel_event.is_set():
                raise ArchiveCancelledError("Archive cancelled")
            end = min(start + 64 * 1024, len(chunk))
            yield chunk[start:end]
            if self.on_sent is not None:
                self.on_sent(object_id, number, end)

    def archive_upload_part(self, object_id: str, part_number: int, chunk: bytes) -> str:
        checksum = base64.b64encode(hashlib.md5(chunk, usedforsecurity=False).digest()).decode()
        response = self._archive_request(
            "PUT",
            f"/archive/{object_id}/parts/{part_number}",
            content=self._chunks(object_id, part_number, chunk),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "Content-MD5": checksum,
            },
        )
        etag = str(response.get("etag", "")).strip('"').lower()
        if not etag:
            raise ArchiveError("The registry returned no part receipt", code="archive_integrity")
        return etag
