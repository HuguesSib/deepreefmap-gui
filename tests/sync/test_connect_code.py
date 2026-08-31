"""Connect-code decoding: every rejection has its own message, and a valid code round-trips."""

from __future__ import annotations

import base64
import json
import logging

import pytest

from deepreefmap_gui.sync.connect_code import (
    CODE_PREFIX,
    SECRET_HEX_LEN,
    ConnectCode,
    ConnectCodeError,
    ConnectCodeVersionError,
    decode_connect_code,
)


def encode(raw: bytes) -> str:
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_round_trips_a_valid_code(make_code, code_secret) -> None:
    code = decode_connect_code(make_code())
    assert code == ConnectCode(base_url="https://reef.example.org", secret=code_secret)
    assert code.warning is None


def test_accepts_clipboard_whitespace(make_code, code_secret) -> None:
    assert decode_connect_code(f"  {make_code()}\n").secret == code_secret


def test_strips_trailing_slash_from_address(make_code) -> None:
    pasted = make_code("https://reef.example.org/api/")
    assert decode_connect_code(pasted).base_url == "https://reef.example.org/api"


@pytest.mark.parametrize(
    "pasted, message",
    [
        ("drm1.abcd", "starts with"),
        ("", "starts with"),
        (CODE_PREFIX, "empty after its prefix"),
        (CODE_PREFIX + "!!!!", "base64url"),
        (encode(b"not json"), "does not contain JSON"),
        (encode(b'["a"]'), "JSON object"),
        (encode(json.dumps({"code": "a" * SECRET_HEX_LEN}).encode()), "`url`"),
        (encode(json.dumps({"url": "https://reef.example.org"}).encode()), "`code`"),
    ],
)
def test_rejects_malformed_codes(pasted, message) -> None:
    with pytest.raises(ConnectCodeError, match=message):
        decode_connect_code(pasted)


def test_a_newer_version_in_this_family_says_so_rather_than_blaming_the_paste() -> None:
    """A field laptop cannot be updated on demand, so it must name the real fault."""
    with pytest.raises(ConnectCodeVersionError, match="newer version"):
        decode_connect_code("reef2.abcd")


def test_a_version_error_is_still_a_connect_code_error() -> None:
    """The failure reporter catches the base class, so a new subclass stays covered."""
    assert issubclass(ConnectCodeVersionError, ConnectCodeError)


@pytest.mark.parametrize("secret", ["deadbeef", "A" * SECRET_HEX_LEN, "z" * SECRET_HEX_LEN])
def test_rejects_malformed_secret(make_code, secret) -> None:
    with pytest.raises(ConnectCodeError, match="secret is malformed"):
        decode_connect_code(make_code(secret=secret))


@pytest.mark.parametrize(
    "url, message",
    [
        ("ftp://reef.example.org", "http or https"),
        ("reef.example.org", "http or https"),
        ("https://", "no host"),
        ("https://user:pw@reef.example.org", "embeds a username or password"),
    ],
)
def test_rejects_bad_addresses(make_code, url, message) -> None:
    with pytest.raises(ConnectCodeError, match=message):
        decode_connect_code(make_code(url))


@pytest.mark.parametrize(
    "url, insecure",
    [
        ("http://192.168.1.10:8000", True),
        ("http://localhost:8000", False),
        ("http://127.0.0.1:8000", False),
        ("https://reef.example.org", False),
    ],
)
def test_plain_http_warns_off_loopback_rather_than_refusing(make_code, url, insecure) -> None:
    code = decode_connect_code(make_code(url))
    assert code.insecure_transport is insecure
    assert (code.warning is not None) is insecure


def test_secret_stays_out_of_repr_and_logs(make_code, code_secret, caplog) -> None:
    """A decoded code is formatted by UI and log calls, so it must not carry the secret."""
    code = decode_connect_code(make_code())
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("decoded %r", code)
    assert code_secret not in repr(code)
    assert code_secret not in caplog.text
