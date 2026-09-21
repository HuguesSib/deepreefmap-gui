"""Archive wire checks using a controlled HTTP transport."""

import base64
import hashlib

import httpx
import pytest

from deepreefmap_gui.sync.archive_client import ArchiveClient, ArchiveError


def test_archive_upload_sends_exact_bytes_with_md5():
    data = bytes(range(256)) * 1000

    def handle(request):
        assert request.read() == data
        expected = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
        assert request.headers["content-md5"] == expected
        assert int(request.headers["content-length"]) == len(data)
        return httpx.Response(200, json={"etag": hashlib.md5(data, usedforsecurity=False).hexdigest()})

    client = ArchiveClient("https://registry.test/api", "token")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        assert client.archive_upload_part("object", 1, data) == hashlib.md5(data, usedforsecurity=False).hexdigest()
    finally:
        client.close()


def test_archive_storage_incompatibility_is_not_retried():
    client = ArchiveClient("https://registry.test/api", "token")
    client._http.close()
    client._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(502, json={"error": "unsupported storage", "code": "archive_storage_incompatible"})
        )
    )
    try:
        with pytest.raises(ArchiveError) as caught:
            client.archive_initiate({})
        assert not caught.value.retryable
    finally:
        client.close()
